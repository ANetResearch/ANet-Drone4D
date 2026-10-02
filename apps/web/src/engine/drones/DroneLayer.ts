// DroneLayer (M06 §6.9, §6.10, §9.1; FR-030..040; AWR-03 §3.8, §8.5; ADR-046). Owner: M06.
// Drones phase, every frame, zero allocation:
//   1. poses at tRender from the M12 interpolation ring (sampleSwarm); the Third/FPV focus vehicle at tFocus;
//      vehicles missing from a non-empty roster are dropped (removal takes effect on the next frame, FR-040);
//   2. per-agent marks (selected, primary, hover, red entity, FPV-hidden), alert level (critical: ALERT flag, ELAND,
//      FAILSAFE, CRASHED; warning: CORRECTING, HOLD, RTL, LANDING; or levels injected by M09/M15) and staleness (HOLD
//      or data older than input.staleAfterMs);
//   3. screen radii every frame, buckets at 10 Hz (marker / low-poly / hero, caps per tier, PerfGovernor step 4);
//   4. marker texture, low-poly instances per model, hero instances and the red-entity hull;
//   5. glyphs of the drones (red entity, other critical, warning, selection, focus double ring, hover, stale);
//   6. CPU trail history for every vehicle, GPU trail slots for the selected vehicles (halo + 2 px) and the focus set;
//   7. focus set at 4 Hz with incremental 30 Hz subscriptions.
// World phase (after m13.gimbal): sensor frustums of the selected vehicle (Tier S) or up to 16 (Tier B/A).
import { Group, Matrix4, Quaternion, Vector3, type PerspectiveCamera } from 'three'
import { FlightFlags, FlightState } from '@awr/contracts/enums'
import { INPUT } from '@/lib/tokens/input.gen'
import { MOTION } from '@/lib/tokens/motion.gen'
import type { FrameCtx, RenderBackendView } from '../loop'
import { newDronePoseSoA, type DronePoseSoA } from '../time/interpRing'
import { BUCKET, BUCKETS, Buckets, type BucketInputs } from './buckets'
import { FocusSet } from './focusSet'
import { FRUSTUM, FrustumLayer, type SensorsApi } from './frustums'
import { GLYPH, GlyphClass, GlyphLayer, Palette, Shape } from './glyph/GlyphLayer'
import { makeLowPolyGeometry } from './lowpoly'
import { MARKER, MarkerBatch, MarkerStyle } from './markers'
import { HeroBatch, LowPolyBatch } from './models'
import { TrailBatch, TRAIL_STYLES } from './trails/TrailBatch'
import { TrailRing } from './trails/TrailRing'
import { heroOf, onHeroReady } from './vehicleModels'
import { makeHeroPlaceholderGeometry } from './lowpoly'

export const DRONES = {
  capacity: 1024,
  rVisM: 0.6,
  selectRingMinPx: 24,
  selectRingScale: 1.3,
  alertRingPx: 20,
  focusHz: 4,
  trailSlotsS: 16, trailSegsS: 256, trailSlotsBA: 64, trailSegsBA: 1024,
  selectedTrailSlots: 4,
  markerCssPx: MARKER.cssPx,
  lowCapS: BUCKETS.lowCapS, lowCapBA: BUCKETS.lowCapBA, lowPx: BUCKETS.lowPx, hyst: 0.15, bucketHz: BUCKETS.hz,
} as const

/** mark bits keyed by agentNo */
export const MARK = { SELECTED: 1, RED: 2, HIDDEN: 4, PRIMARY: 8, HOVER: 16, STALE: 32, FOCUSSET: 64 } as const

const CRITICAL_FS = new Uint8Array(16)
const WARNING_FS = new Uint8Array(16)
for (const s of [FlightState.ELAND, FlightState.FAILSAFE, FlightState.CRASHED]) CRITICAL_FS[s] = 1
for (const s of [FlightState.CORRECTING, FlightState.HOLD, FlightState.RTL, FlightState.LANDING]) WARNING_FS[s] = 1

/** alert level from Lite32/Full64 flight state and flags: 2 critical, 1 warning, 0 none (AWR-17 §6.5 red criterion) */
export function alertLevelOf(fs: number, flags: number): number {
  if ((flags & FlightFlags.ALERT) !== 0 || CRITICAL_FS[fs & 15] === 1) return 2
  return WARNING_FS[fs & 15] === 1 ? 1 : 0
}

/** the interpolation view of M12 used here (M12 §7.1); sampleOne and setFocus are optional until M12 ships them */
export interface InterpView {
  sampleSwarm(tS: number, out: DronePoseSoA): void
  sampleOne?(agentNo: number, tS: number, out: DronePoseSoA, o: number): boolean
  setFocus?(agentNo: number): void
}
/** roster view of M11 */
export interface RosterLike { readonly size: number; get(agentNo: number): { id: string; model: string; kind: string } | undefined; idOf(agentNo: number): string | undefined }

export interface DroneLayerOptions {
  be: RenderBackendView
  capacity?: number
  interp: InterpView
  roster: () => RosterLike | null
  subscribe: (topic: string, rate: number) => () => void
  sensors?: () => SensorsApi | null
  motionTier?: () => 'full' | 'lite' | 'reduced' | 'off'
  /**
   * true while the simulation clock is frozen (TIME state PAUSED or STEPPING) and TIME itself is fresh: no new samples
   * arrive because nothing moves, so the vehicles are not marked STALE ("signal delay"); a stale TIME (connection
   * degraded) still marks them (FX-WEB2-to-M06-M12 item 1; FX-WEB1)
   */
  frozen?: () => boolean
}

export class DroneLayer {
  readonly root = new Group()
  readonly trailRoot = new Group()
  readonly frustumRoot = new Group()
  readonly markers: MarkerBatch
  readonly glyphs: GlyphLayer
  readonly buckets: Buckets
  readonly focus: FocusSet
  readonly trails: TrailRing
  readonly trailFocus: TrailBatch
  readonly trailSel: TrailBatch
  readonly trailHalo: TrailBatch
  readonly frustums: FrustumLayer
  readonly hero: HeroBatch
  private readonly low = new Map<string, LowPolyBatch>()
  readonly poses: DronePoseSoA
  private readonly focusPose: DronePoseSoA
  readonly mark = new Uint8Array(65536)
  readonly alert = new Uint8Array(65536)
  private readonly extAlert = new Uint8Array(65536)
  private readonly modelIdx = new Uint8Array(65536)
  private readonly models: string[] = ['p600', 'x500']
  private readonly lowCount: Int32Array
  private readonly m4 = new Matrix4()
  private readonly q = new Quaternion()
  private readonly v = new Vector3()
  private readonly one = new Vector3(1, 1, 1)
  private readonly tier: 'A' | 'B' | 'S'
  private lastFocusSetMs = Number.NEGATIVE_INFINITY
  private readonly binputs: BucketInputs
  private readonly lowList: LowPolyBatch[] = []
  selected: Int32Array = new Int32Array(0)
  primary = -1
  hover = -1
  redOwner = -1
  /** focus vehicle of Third/FPV (-1 none) and whether it is drawn (FPV hides it) */
  focusAgent = -1
  focusMode: 'none' | 'third' | 'fpv' = 'none'
  /** PerfGovernor limits */
  lowCap: number
  trailSlots: number
  trailSegs: number
  frustumCap: number
  frustumFade = 1
  heroesAvailable = false
  drawn = 0
  private readonly selFlag = new Uint8Array(65536)
  private readonly colorOf = (a: number): number => (a === this.redOwner ? 1 : 0)
  private offHero: () => void

  constructor(readonly o: DroneLayerOptions) {
    const cap = o.capacity ?? DRONES.capacity
    const S = o.be.tier === 'S'
    this.tier = o.be.tier
    this.poses = newDronePoseSoA(cap)
    this.focusPose = newDronePoseSoA(1)
    this.markers = new MarkerBatch(cap)
    this.glyphs = new GlyphLayer(S ? GLYPH.capS : GLYPH.capBA)
    this.buckets = new Buckets(cap, o.be.tier)
    this.lowCap = this.buckets.lowCap
    this.focus = new FocusSet({ idOf: (a) => o.roster()?.idOf(a), subscribe: o.subscribe }, 32, cap)
    this.trails = new TrailRing(cap)
    this.trailSlots = S ? DRONES.trailSlotsS : DRONES.trailSlotsBA
    this.trailSegs = S ? DRONES.trailSegsS : DRONES.trailSegsBA
    this.trailFocus = new TrailBatch(this.trailSlots, this.trailSegs, TRAIL_STYLES.focus)
    this.trailSel = new TrailBatch(DRONES.selectedTrailSlots, this.trailSegs, TRAIL_STYLES.selected)
    this.trailHalo = new TrailBatch(DRONES.selectedTrailSlots, this.trailSegs, TRAIL_STYLES.halo)
    this.frustumCap = S ? FRUSTUM.capS : FRUSTUM.capBA
    this.frustums = new FrustumLayer(S ? FRUSTUM.capS : FRUSTUM.capBA)
    this.lowCount = new Int32Array(this.models.length)
    for (let i = 0; i < this.models.length; i++) {
      const b = new LowPolyBatch(makeLowPolyGeometry(S ? 's' : 'full'), S ? BUCKETS.lowCapS : BUCKETS.lowCapBA, `DroneLowPoly.${this.models[i]}`)
      this.low.set(this.models[i], b)
      this.lowList.push(b)
      this.root.add(b.mesh)
    }
    const h = heroOf('p600')
    this.heroesAvailable = h.state === 'ready'
    this.hero = new HeroBatch(h.geometry ?? makeHeroPlaceholderGeometry(), S ? BUCKETS.heroCapS : BUCKETS.heroCapBA)
    this.offHero = onHeroReady((model) => {
      const hm = heroOf(model)
      if (model === 'p600' && hm.geometry) {
        this.hero.setGeometry(hm.geometry)
        this.heroesAvailable = true
      }
    })
    this.root.add(this.markers.mesh, this.hero.mesh, this.hero.hull)
    this.root.name = 'DroneLayer'
    this.trailRoot.add(this.trailHalo.mesh, this.trailSel.mesh, this.trailFocus.mesh)
    this.trailRoot.name = 'TrailLayer'
    this.frustumRoot.add(this.frustums.edges, this.frustums.fill)
    this.frustumRoot.name = 'SensorLayer'
    const mark = this.mark
    const alert = this.alert
    const heroOk = (): boolean => this.heroesAvailable
    const kindOk = this.modelIdx
    this.binputs = {
      mark, alert,
      rVisOf: () => DRONES.rVisM,
      heroOf: (i: number) => heroOk() && kindOk[this.poses.agentNo[i]] === 0,
    }
  }

  // ---------------------------------------------------------------- state from bindings
  setHighlights(selected: ArrayLike<number>, primary: number, hover: number): void {
    for (let i = 0; i < this.selected.length; i++) this.selFlag[this.selected[i]] = 0
    if (this.selected.length !== selected.length) this.selected = new Int32Array(selected.length)
    for (let i = 0; i < selected.length; i++) {
      this.selected[i] = selected[i]
      this.selFlag[selected[i] & 0xffff] = 1
    }
    this.primary = primary
    this.hover = hover
  }
  setRedOwner(agentNo: number): void {
    this.redOwner = agentNo
  }
  /** alert levels from the alarm store (M09/M15), merged with the telemetry-derived level */
  setAlertLevel(agentNo: number, level: number): void {
    this.extAlert[agentNo & 0xffff] = level
  }
  setFocus(agentNo: number, mode: 'none' | 'third' | 'fpv'): void {
    this.focusAgent = agentNo
    this.focusMode = agentNo >= 0 ? mode : 'none'
  }
  /** PerfGovernor step 4 */
  setLowCap(n: number): void {
    this.lowCap = n
    this.buckets.lowCap = n
    this.buckets.lastMs = Number.NEGATIVE_INFINITY
  }
  /** PerfGovernor step 1: slots (0 = selected only) and segments per slot */
  setTrailLimits(slots: number, segs: number): void {
    this.trailSlots = slots
    this.trailSegs = segs
    this.trailFocus.setLimits(slots, segs, this.trails)
    this.trailSel.setLimits(DRONES.selectedTrailSlots, segs, this.trails)
    this.trailHalo.setLimits(DRONES.selectedTrailSlots, segs, this.trails)
  }

  poseIndexOf(agentNo: number): number {
    const p = this.poses
    for (let i = 0; i < p.n; i++) if (p.agentNo[i] === agentNo) return i
    return -1
  }

  // ---------------------------------------------------------------- drones phase
  /** static browsing (route world != session world, M06-FR-030): no vehicle is sampled, drawn, labelled or trailed */
  suppressed = false

  update(ctx: FrameCtx): void {
    const p = this.poses
    this.o.interp.sampleSwarm(ctx.tRenderS, p)
    if (this.suppressed) p.n = 0
    this.dropRemoved()
    const roster = this.o.roster()
    // focus vehicle at tFocus (ADR-046)
    if (this.focusAgent >= 0 && this.o.interp.sampleOne) {
      const fi = this.poseIndexOf(this.focusAgent)
      if (fi >= 0 && this.o.interp.sampleOne(this.focusAgent, ctx.tFocusS, this.focusPose, 0)) this.copyPose(this.focusPose, 0, p, fi)
    }
    const n = p.n
    const mark = this.mark
    const staleS = INPUT.staleAfterMs / 1000
    const frozen = this.o.frozen?.() === true
    for (let i = 0; i < n; i++) {
      const a = p.agentNo[i]
      let m = 0
      if (this.selFlag[a]) m |= MARK.SELECTED
      if (a === this.primary) m |= MARK.PRIMARY | MARK.SELECTED
      if (a === this.hover) m |= MARK.HOVER
      if (a === this.redOwner) m |= MARK.RED
      if (a === this.focusAgent && this.focusMode === 'fpv') m |= MARK.HIDDEN
      if (!frozen && (p.hold[i] || p.ageS[i] > staleS)) m |= MARK.STALE
      if (this.focus.has(a)) m |= MARK.FOCUSSET
      mark[a] = m
      this.alert[a] = Math.max(alertLevelOf(p.state[i], p.flags[i]), this.extAlert[a])
      const e = roster?.get(a)
      this.modelIdx[a] = e && e.kind !== 'uav' && e.kind !== '' ? 2 : e && /x500/i.test(e.model) ? 1 : 0
    }
    const cam = ctx.camera as PerspectiveCamera | null
    if (cam && n > 0) {
      this.buckets.radii(p, cam, Math.max(1, ctx.cssH), this.binputs)
      if (ctx.nowMs - this.buckets.lastMs >= 1000 / BUCKETS.hz - 1) this.buckets.assign(p, this.binputs, ctx.nowMs)
      else this.buckets.refresh(p, this.binputs)
    }
    this.writeInstances(ctx)
    this.writeGlyphs(ctx)
    this.writeTrails(ctx)
    if (ctx.nowMs - this.lastFocusSetMs >= 1000 / DRONES.focusHz - 1) {
      this.lastFocusSetMs = ctx.nowMs
      this.focus.update(n, p.agentNo, this.buckets.rpx, ctx.nowMs, this.selFlag)
    }
    this.drawn = n
  }

  private copyPose(src: DronePoseSoA, si: number, dst: DronePoseSoA, di: number): void {
    for (let k = 0; k < 3; k++) {
      dst.pos[3 * di + k] = src.pos[3 * si + k]
      dst.vel[3 * di + k] = src.vel[3 * si + k]
    }
    for (let k = 0; k < 4; k++) dst.quat[4 * di + k] = src.quat[4 * si + k]
    dst.hold[di] = src.hold[si]
    dst.sampleT[di] = src.sampleT[si]
    dst.ageS[di] = src.ageS[si]
  }

  /** vehicles missing from a non-empty roster are removed in place (trail rows and buckets recycled) */
  private dropRemoved(): void {
    const roster = this.o.roster()
    if (!roster || roster.size === 0) return
    const p = this.poses
    let w = 0
    for (let i = 0; i < p.n; i++) {
      const a = p.agentNo[i]
      if (!roster.get(a)) {
        this.forget(a)
        continue
      }
      if (w !== i) {
        p.agentNo[w] = a
        this.copyPose(p, i, p, w)
        p.state[w] = p.state[i]
        p.flags[w] = p.flags[i]
        p.battery[w] = p.battery[i]
        p.clamped[w] = p.clamped[i]
      }
      w++
    }
    p.n = w
  }

  private readonly forgotten = new Uint8Array(65536)
  private forget(a: number): void {
    if (this.forgotten[a]) return
    this.forgotten[a] = 1
    this.trails.clear(a)
    this.buckets.forget(a)
    this.mark[a] = 0
  }

  private writeInstances(ctx: FrameCtx): void {
    const p = this.poses
    const n = p.n
    const bk = this.buckets.bucket
    this.lowCount.fill(0)
    let heroN = 0
    let hull = false
    for (let i = 0; i < n; i++) {
      const a = p.agentNo[i]
      const b = n > 0 ? bk[i] : BUCKET.MARKER
      const m = this.mark[a]
      this.forgotten[a] = 0
      const x = p.pos[3 * i]
      const y = p.pos[3 * i + 1]
      const z = p.pos[3 * i + 2]
      let style: number = (m & MARK.RED) !== 0 ? MarkerStyle.Red : (m & MARK.STALE) !== 0 ? MarkerStyle.Stale : MarkerStyle.Normal
      // unknown entity kinds render as generic markers (FR-084)
      const model = b === BUCKET.HIDDEN || this.modelIdx[a] === 2 ? -1 : b
      if (b === BUCKET.HIDDEN) style = MarkerStyle.Hidden
      else if (model === BUCKET.LOW || model === BUCKET.HERO) style = MarkerStyle.Hidden
      this.markers.set(i, x, y, z, style)
      if (model !== BUCKET.LOW && model !== BUCKET.HERO) continue
      this.v.set(x, y, z)
      this.q.set(p.quat[4 * i], p.quat[4 * i + 1], p.quat[4 * i + 2], p.quat[4 * i + 3])
      this.m4.compose(this.v, this.q, this.one)
      if (b === BUCKET.HERO && heroN < this.hero.cap) {
        this.hero.setAt(heroN++, this.m4)
        if ((m & MARK.RED) !== 0 && ctx.camera) {
          const cam = ctx.camera
          const d = Math.max(cam.near, this.v.set(x, z, -y).distanceTo(cam.position))
          this.hero.setHull(true, this.m4, (d * 2 * Math.tan((cam.fov * Math.PI) / 360)) / Math.max(1, ctx.cssH))
          hull = true
        }
        continue
      }
      const mi = Math.min(this.modelIdx[a], 1)
      const batch = this.low.get(this.models[mi])!
      const k = this.lowCount[mi]
      if (k >= batch.cap) continue
      batch.setAt(k, this.m4)
      this.lowCount[mi] = k + 1
    }
    this.markers.commit(n, ctx.dpr, ctx.dbW, ctx.dbH)
    for (let i = 0; i < this.models.length; i++) this.low.get(this.models[i])!.commit(this.lowCount[i])
    this.hero.commit(heroN)
    if (!hull) this.hero.setHull(false)
  }

  private writeGlyphs(ctx: FrameCtx): void {
    const g = this.glyphs
    g.begin()
    const p = this.poses
    const rpx = this.buckets.rpx
    const full = (this.o.motionTier?.() ?? 'lite') === 'full'
    const breath = full ? 0.75 + 0.25 * Math.cos((2 * Math.PI * ctx.nowMs) / MOTION.pulseMs) : 1
    for (let i = 0; i < p.n; i++) {
      const a = p.agentNo[i]
      const m = this.mark[a]
      if ((m & MARK.HIDDEN) !== 0) continue
      const x = p.pos[3 * i]
      const y = p.pos[3 * i + 1]
      const z = p.pos[3 * i + 2]
      const ring = Math.max(DRONES.selectRingMinPx, 2 * rpx[i] * DRONES.selectRingScale)
      const lvl = this.alert[a]
      const sel = (m & MARK.SELECTED) !== 0
      if ((m & MARK.RED) !== 0) {
        g.push(GlyphClass.Red, x, y, z, ring, lvl === 2 && !sel ? Shape.Octagon : Shape.Ring, 2, Palette.R500, breath)
        if (sel && lvl === 2) g.push(GlyphClass.Red, x, y, z, ring + 6, Shape.Ring, 1, Palette.G50)
      } else if (lvl === 2) {
        g.push(GlyphClass.Critical, x, y, z, sel ? ring : DRONES.alertRingPx, Shape.Octagon, sel ? 2 : 1.5, Palette.R500)
        if (sel) g.push(GlyphClass.Critical, x, y, z, ring + 6, Shape.Ring, 1, Palette.G50)
      } else if (lvl === 1) {
        g.push(GlyphClass.Warning, x, y, z, DRONES.alertRingPx, Shape.Triangle, 1.5, Palette.R500)
      }
      if (sel && (m & MARK.RED) === 0 && lvl !== 2) {
        g.push(GlyphClass.Selection, x, y, z, ring, Shape.Ring, 2, Palette.G50)
        // focus vehicle when the red went to a critical elsewhere: double ring (inner 2 px + outer 1 px, 3 px apart)
        if ((m & MARK.PRIMARY) !== 0 && this.redOwner >= 0) g.push(GlyphClass.Selection, x, y, z, ring + 6 + 3, Shape.Ring, 1, Palette.G50)
      }
      if ((m & MARK.HOVER) !== 0 && !sel) g.push(GlyphClass.Selection, x, y, z, Math.max(16, ring * 0.8), Shape.Ring, 1, Palette.G50)
      if ((m & MARK.STALE) !== 0) g.push(GlyphClass.Stale, x, y, z, DRONES.alertRingPx, Shape.Dashed, 1.5, Palette.G500)
    }
  }

  private readonly slotTmp = new Int32Array(64)
  private writeTrails(ctx: FrameCtx): void {
    const p = this.poses
    for (let i = 0; i < p.n; i++) this.trails.append(p.agentNo[i], p.pos[3 * i], p.pos[3 * i + 1], p.pos[3 * i + 2], p.sampleT[i] / 1000)
    // selected slots (primary first)
    const selN = this.suppressed ? 0 : Math.min(this.selected.length, DRONES.selectedTrailSlots)
    for (let s = 0; s < DRONES.selectedTrailSlots; s++) {
      const want = s < selN ? this.selectedOrdered(s) : -1
      if (this.trailSel.slotAgent[s] !== want) {
        if (want < 0) {
          this.trailSel.release(s)
          this.trailHalo.release(s)
        } else {
          const col = want === this.redOwner ? 1 : 0
          this.trailSel.assign(s, want, col, this.trails)
          this.trailHalo.assign(s, want, 0, this.trails)
        }
      }
    }
    // focus-set slots: members minus selected, up to the governor limit
    let k = 0
    for (let j = 0; j < this.focus.size && k < this.trailFocus.activeSlots && k < this.slotTmp.length; j++) {
      const a = this.focus.members[j]
      if (!this.selFlag[a] && a !== this.primary) this.slotTmp[k++] = a
    }
    for (let s = 0; s < this.trailFocus.activeSlots; s++) {
      const want = s < k ? this.slotTmp[s] : -1
      const cur = this.trailFocus.slotAgent[s]
      if (cur === want) continue
      if (want < 0) this.trailFocus.release(s)
      else this.trailFocus.assign(s, want, want === this.redOwner ? 1 : 0, this.trails)
    }
    const nowRel = Number.isNaN(this.trails.blockStartS) ? 0 : ctx.tRenderS - this.trails.blockStartS
    this.trailSel.sync(this.trails, nowRel, this.colorOf)
    this.trailHalo.sync(this.trails, nowRel, zero)
    this.trailFocus.sync(this.trails, nowRel, this.colorOf)
    const dpr = ctx.dpr > 0 ? ctx.dpr : 1
    const near = (ctx.camera as PerspectiveCamera | null)?.near ?? 0
    this.trailSel.setView(ctx.dbW, ctx.dbH, near)
    this.trailHalo.setView(ctx.dbW, ctx.dbH, near)
    this.trailFocus.setView(ctx.dbW, ctx.dbH, near)
    this.trailSel.setWidth(TRAIL_STYLES.selected.widthCss, dpr)
    this.trailHalo.setWidth(TRAIL_STYLES.halo.widthCss, dpr)
    this.trailFocus.setWidth(TRAIL_STYLES.focus.widthCss, dpr)
  }

  private selectedOrdered(s: number): number {
    if (this.primary >= 0) {
      if (s === 0) return this.primary
      let k = 0
      for (let i = 0; i < this.selected.length; i++) {
        if (this.selected[i] === this.primary) continue
        if (++k === s) return this.selected[i]
      }
      return -1
    }
    return this.selected[s] ?? -1
  }

  // ---------------------------------------------------------------- world phase: frustums
  updateFrustums(ctx: FrameCtx): void {
    const api = this.o.sensors?.() ?? null
    const f = this.frustums
    f.fade = this.frustumFade
    f.begin()
    if (api && this.frustumCap > 0) {
      const p = this.poses
      // selected vehicles first (primary), then the focus set on Tier B/A
      for (let pass = 0; pass < 2; pass++) {
        for (let i = 0; i < p.n && f.count < Math.min(this.frustumCap, f.cap); i++) {
          const a = p.agentNo[i]
          const m = this.mark[a]
          const want = pass === 0 ? (m & MARK.SELECTED) !== 0 : this.tier !== 'S' && (m & MARK.FOCUSSET) !== 0 && (m & MARK.SELECTED) === 0
          if (!want) continue
          const list = api.sensorsOf(a)
          for (let s = 0; s < list.length; s++) {
            if (list[s].kind !== 0 && list[s].kind !== 2) continue
            f.add(api, list[s], p.pos[3 * i], p.pos[3 * i + 1], p.pos[3 * i + 2], p.quat[4 * i], p.quat[4 * i + 1], p.quat[4 * i + 2], p.quat[4 * i + 3], ctx.tRenderS)
          }
        }
        if (this.frustumCap <= 1) break
      }
    }
    f.commit()
  }

  // ---------------------------------------------------------------- epoch / reset
  clearTrails(): void {
    this.trails.clearAll()
    for (let s = 0; s < this.trailSel.maxSlots; s++) {
      this.trailSel.release(s)
      this.trailHalo.release(s)
    }
    for (let s = 0; s < this.trailFocus.maxSlots; s++) this.trailFocus.release(s)
  }
  clearProducer(idBase: number, idCount: number): void {
    this.trails.clearRange(idBase, idCount)
    for (const b of [this.trailSel, this.trailHalo, this.trailFocus]) {
      for (let s = 0; s < b.maxSlots; s++) if (b.slotAgent[s] >= idBase && b.slotAgent[s] < idBase + idCount) b.release(s)
    }
  }

  drawCountDrones(): number {
    let n = this.markers.drawCount() + this.hero.drawCount()
    for (let i = 0; i < this.lowList.length; i++) n += this.lowList[i].drawCount()
    return n
  }
  drawCountTrails(): number {
    return this.trailFocus.drawCount() + this.trailSel.drawCount() + this.trailHalo.drawCount()
  }
  drawCountFrustums(): number {
    return this.frustums.drawCount()
  }
  lowBatches(): readonly LowPolyBatch[] {
    return this.lowList
  }

  dispose(): void {
    this.offHero()
    this.focus.clear()
    this.markers.dispose()
    this.glyphs.dispose()
    for (const b of this.low.values()) b.dispose()
    this.hero.dispose()
    this.trailFocus.dispose()
    this.trailSel.dispose()
    this.trailHalo.dispose()
    this.frustums.dispose()
  }
}

const zero = (): number => 0
