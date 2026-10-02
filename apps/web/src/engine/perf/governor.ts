// PerfGovernor: budget arbitration (ADR-041; M06 §6.17, FR-075, FR-076; AWR-18 §4.7). Owner: M06.
// evaluate() at 1 Hz in the governor phase, skipped (and saturation timers cleared) on frozen frames:
//   Tier S:   CAS held at the quality floor >= 2 s, >= 2 s since the last step, not at the end -> degrade one sub-step;
//             sub-steps whose change is not on screen (GovernorKnob.visible false) chain into the next one in the same
//             evaluation, so the 2 s spacing applies between visible steps (FX2-R3, ADR-067)
//   Tier B/A: the CAS outer loop first walks down to the lowest allowed rung; only then the same rule applies
//   any tier: CAS at the ceiling (or B >= 0.9 hi) for >= 10 s and >= 10 s since the last step -> restore one sub-step
//             (invisible restore sub-steps chain the same way)
// Knobs are applied in step order (1 trails, 2 frustums, 3 labels, 4 low-poly cap, 5 environment [M07], 6 motion cap,
// 7 point-cloud floor release [cas.setFloorOverride]); the sub-levels of a knob run consecutively; restoring walks back
// in exactly the reverse order. Each change pushes governor.history, emits 'governor.step' {step, dir, reasonKey} (only
// when the knob reports a visible change, GovernorKnob.visible) and sets the summary label key. Pure and clock-injectable (Vitest pseudo clock, apps/web/tests/perf).
import { events, type FrameCtx, type Tier } from '../loop'
import { pushGovernorHistory, type AwrPerf } from './probe'

export type GovernorStepNo = 1 | 2 | 3 | 4 | 5 | 6 | 7
export interface GovernorKnob {
  step: GovernorStepNo
  id: string
  /** number of levels including the initial level 0 (M06 §6.17 table) */
  levels: number
  apply(level: number): void
  /** i18n key of the HUD and Toast text (perf.governor.<id> when omitted) */
  labelKey?: string
  /**
   * whether the change to `level` (just applied) shows on screen now; false: the step is applied and recorded but no
   * 'governor.step' event is emitted, so no Toast (FX2-R2: capping trails, frustums, labels or low-poly instances that are
   * not on screen changes nothing, while the first Toast of a page costs the compositor a 0.2-0.5 s pipeline compile on
   * SwiftShader). Omitted: always visible.
   */
  visible?(level: number): boolean
}
/** CAS view of M05 (M05 §6.8.5): times in ms, points, rung index */
export interface CasState { atFloorSinceMs: number; atCeilSinceMs: number; B: number; lo: number; hi: number; rung: number }
export interface CasHandle {
  state(): Readonly<CasState>
  setFloorOverride(lo: number | null): void
  setTarget?(targetMs: number, tailK: number): void
}

export const GOVERNOR = { evalHz: 1, degradeAfterMs: 2000, degradeGapMs: 2000, restoreAfterMs: 10000, restoreGapMs: 10000, highFrac: 0.9, maxChain: 16 } as const

interface KnobState { k: GovernorKnob; level: number }

export class PerfGovernor {
  private readonly knobs: KnobState[] = []
  private cas: CasHandle | null = null
  private lastStepMs = Number.NEGATIVE_INFINITY
  private highSinceMs = Number.NaN
  private lastEvalMs = Number.NEGATIVE_INFINITY
  /** step number of the last applied change (0 when everything is at level 0) */
  step = 0
  labelKey: string | null = null
  state: 'NOMINAL' | 'DEGRADED' | 'FLOOR_RELEASED' = 'NOMINAL'
  private builtinFloor: KnobState | null = null

  constructor(private readonly o: { tier: () => Tier; lowestAllowedRung: () => number; perf?: AwrPerf | null; now?: () => number }) {}

  registerKnob(k: GovernorKnob): () => void {
    const s: KnobState = { k, level: 0 }
    if (k.step === 7 && this.builtinFloor) {
      const b = this.knobs.indexOf(this.builtinFloor)
      if (b >= 0) this.knobs.splice(b, 1)
      this.builtinFloor = null
    }
    const i = this.knobs.findIndex((x) => x.k.id === k.id)
    if (i >= 0) this.knobs.splice(i, 1)
    this.knobs.push(s)
    this.knobs.sort((a, b) => a.k.step - b.k.step)
    return () => {
      const j = this.knobs.indexOf(s)
      if (j < 0) return
      if (s.level > 0) k.apply(0)
      this.knobs.splice(j, 1)
    }
  }

  /** CAS of the point cloud (M05); also installs the built-in step 7 knob when M05 registers none */
  setCas(cas: CasHandle | null): void {
    // the host calls this every governor tick: the same handle must keep the step-7 knob and its level (re-creating it
    // at level 0 would strand a released floor override, FX-WEB1)
    if (cas === this.cas) return
    this.cas = cas
    if (this.builtinFloor) {
      const i = this.knobs.indexOf(this.builtinFloor)
      if (i >= 0) this.knobs.splice(i, 1)
      this.builtinFloor = null
    }
    if (cas && !this.knobs.some((x) => x.k.step === 7)) {
      const s: KnobState = {
        level: 0,
        k: { step: 7, id: 'pc.floor', levels: 2, labelKey: 'perf.governor.pc.floor', apply: (l) => cas.setFloorOverride(l > 0 ? cas.state().lo : null) },
      }
      this.builtinFloor = s
      this.knobs.push(s)
    }
  }

  /** true once the point-cloud CAS is attached (M05 services.cas) */
  get hasCas(): boolean {
    return this.cas !== null
  }
  /** knob ids and levels in step order (tests, HUD) */
  knobLevels(): { step: number; id: string; level: number; levels: number }[] {
    return this.knobs.map((x) => ({ step: x.k.step, id: x.k.id, level: x.level, levels: x.k.levels }))
  }

  levelOf(id: string): number {
    return this.knobs.find((x) => x.k.id === id)?.level ?? 0
  }

  /** governor phase; the 1 Hz cadence is enforced here (the task may run every frame) */
  evaluate(ctx: FrameCtx): void {
    const now = this.o.now ? this.o.now() : ctx.nowMs
    if (ctx.frozen) {
      this.highSinceMs = Number.NaN
      return
    }
    if (now - this.lastEvalMs < 1000 / GOVERNOR.evalHz - 1) return
    this.lastEvalMs = now
    const cas = this.cas
    if (!cas) return
    const s = cas.state()
    const high = s.hi > 0 && s.B >= GOVERNOR.highFrac * s.hi
    if (high) {
      if (Number.isNaN(this.highSinceMs)) this.highSinceMs = now
    } else this.highSinceMs = Number.NaN
    const floorOk = this.o.tier() === 'S' || s.rung <= this.o.lowestAllowedRung()
    const atFloor = Number.isFinite(s.atFloorSinceMs) && s.atFloorSinceMs >= GOVERNOR.degradeAfterMs
    if (floorOk && atFloor && now - this.lastStepMs >= GOVERNOR.degradeGapMs && this.canDegrade()) {
      // a sub-step whose change is not on screen (GovernorKnob.visible false) has no effect to wait for: the next one
      // follows in the same evaluation, until a visible step is taken (FX2-R3, ADR-067)
      for (let k = 0; k < GOVERNOR.maxChain && this.degradeOne(now, 'floor'); k++) if (this.lastVisible) break
      return
    }
    const ceil = (Number.isFinite(s.atCeilSinceMs) && s.atCeilSinceMs >= GOVERNOR.restoreAfterMs) ||
      (!Number.isNaN(this.highSinceMs) && now - this.highSinceMs >= GOVERNOR.restoreAfterMs)
    if (ceil && now - this.lastStepMs >= GOVERNOR.restoreGapMs && this.canRestore()) {
      for (let k = 0; k < GOVERNOR.maxChain && this.restoreOne(now, 'ceil'); k++) if (this.lastVisible) break
    }
  }

  /** whether the last applied change was reported visible by its knob */
  lastVisible = true

  canDegrade(): boolean {
    return this.knobs.some((x) => x.level < x.k.levels - 1)
  }
  canRestore(): boolean {
    return this.knobs.some((x) => x.level > 0)
  }

  degradeOne(now: number, reason: string): boolean {
    for (const x of this.knobs) {
      if (x.level >= x.k.levels - 1) continue
      x.level++
      x.k.apply(x.level)
      this.changed(now, x, -1, reason)
      return true
    }
    return false
  }

  restoreOne(now: number, reason: string): boolean {
    for (let i = this.knobs.length - 1; i >= 0; i--) {
      const x = this.knobs[i]
      if (x.level <= 0) continue
      x.level--
      x.k.apply(x.level)
      this.changed(now, x, 1, reason)
      return true
    }
    return false
  }

  /** undo everything (world switch, tests) */
  resetAll(): void {
    for (const x of this.knobs) {
      if (x.level > 0) x.k.apply(0)
      x.level = 0
    }
    this.step = 0
    this.labelKey = null
    this.state = 'NOMINAL'
    if (this.o.perf) {
      this.o.perf.governor.step = 0
      this.o.perf.governor.state = 'NOMINAL'
    }
  }

  private changed(now: number, x: KnobState, dir: -1 | 1, reason: string): void {
    this.lastStepMs = now
    const any = this.knobs.some((k) => k.level > 0)
    const last = [...this.knobs].reverse().find((k) => k.level > 0)
    this.step = any && last ? last.k.step : 0
    this.labelKey = any && last ? (last.k.labelKey ?? `perf.governor.${last.k.id}`) : null
    this.state = !any ? 'NOMINAL' : this.knobs.some((k) => k.k.step === 7 && k.level > 0) ? 'FLOOR_RELEASED' : 'DEGRADED'
    const key = x.k.labelKey ?? `perf.governor.${x.k.id}`
    const visible = !x.k.visible || x.k.visible(x.level)
    this.lastVisible = visible
    const p = this.o.perf
    if (p) {
      p.governor.step = this.step
      p.governor.state = this.state
      pushGovernorHistory(p, now, x.k.step, dir, `${reason}:${x.k.id}:${x.level}${visible ? '' : ':hidden'}`)
    }
    if (visible) events.emit('governor.step', { step: x.k.step, dir, reasonKey: key, level: x.level, id: x.k.id })
  }
}
