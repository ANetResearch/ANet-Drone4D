// LabelLayer: one DOM overlay with a preallocated element pool (M06 §6.13, FR-066, FR-067, AC-047; AWR-15 §10.10;
// ADR-029; r14 §3.8). Owner: M06.
// world phase (layout): candidates (selected, red entity, alerts, hover, focus set, GoTo readout, hovered zone; <= 64)
// are projected with the current camera, dropped outside the frustum (|x|, |y| > 1.05 NDC or behind), sorted by
// priority and distance, and placed on the 96 x 28 px grid (2 x 1 cells) up to the cap (S 16, B/A 48; PerfGovernor
// step 3 -> 8/4 or 24/12). overlay phase (DOM): only transform is written per frame, and only when the position moved
// by >= 0.5 px (translate3d + translate(-50 %, -100 %) so no layout reads); display changes only on state changes; text
// (id, secondary line, icon <use href="#awr-icon-<key>">) is written in batches at 4 Hz (Tier S) or 10 Hz.
// Element structure (created once): div.lbl > svg.ic > use, span.id, span.sub. Colours are theme tokens only (label bg
// --hud, ring foreground/10, the red entity ring --brand = r500); no backdrop-filter, no emoji: names come sanitised.
import { MOTION } from '@/lib/tokens/motion.gen'
import type { FrameCtx } from '../loop'
import { Declutter, DECLUTTER } from './declutter'

export const LABEL_PRIO = { selected: 7, red: 6, critical: 5, warning: 4, hover: 3, goto: 2, other: 1 } as const
export const LabelIcon = { None: 0, Critical: 1, Warning: 2, Hold: 3, Goto: 4 } as const
const ICON_KEYS = ['', 'alert.critical', 'alert.warning', 'state.hold', 'cmd.goto'] as const
export const LabelKind = { Drone: 0, Goto: 1, Zone: 2 } as const
export const LABELS = { maxCandidates: 64, offsetPx: 12, moveEpsPx: 0.5, maxPool: DECLUTTER.capBA } as const

/** texts of one label, produced by the viewport binding (sanitised names, M15 formatter) */
export interface LabelText { id: string; sub: string }
export type LabelTextFn = (kind: number, key: number, expanded: boolean, out: LabelText) => void

/** candidate sink filled by the viewport binding every frame (zero allocation) */
export class LabelCandidates {
  n = 0
  readonly kind = new Uint8Array(LABELS.maxCandidates)
  readonly key = new Int32Array(LABELS.maxCandidates)
  readonly pos = new Float64Array(3 * LABELS.maxCandidates)
  readonly prio = new Uint8Array(LABELS.maxCandidates)
  readonly icon = new Uint8Array(LABELS.maxCandidates)
  readonly red = new Uint8Array(LABELS.maxCandidates)
  readonly expanded = new Uint8Array(LABELS.maxCandidates)
  readonly dist = new Float32Array(LABELS.maxCandidates)
  readonly sx = new Float32Array(LABELS.maxCandidates)
  readonly sy = new Float32Array(LABELS.maxCandidates)
  clear(): void {
    this.n = 0
  }
  add(kind: number, key: number, x: number, y: number, z: number, prio: number, icon: number, red: boolean, expanded: boolean): boolean {
    if (this.n >= LABELS.maxCandidates) return false
    const i = this.n++
    this.kind[i] = kind
    this.key[i] = key
    this.pos[3 * i] = x
    this.pos[3 * i + 1] = y
    this.pos[3 * i + 2] = z
    this.prio[i] = prio
    this.icon[i] = icon
    this.red[i] = red ? 1 : 0
    this.expanded[i] = expanded ? 1 : 0
    return true
  }
}

interface Slot {
  el: HTMLDivElement
  use: SVGUseElement
  svg: SVGSVGElement
  idEl: HTMLSpanElement
  subEl: HTMLSpanElement
  shown: boolean
  x: number
  y: number
  kind: number
  key: number
  icon: number
  red: number
  expanded: number
  text: LabelText
}

const SVG_NS = 'http://www.w3.org/2000/svg'
const BASE = 'lbl pointer-events-none absolute left-0 top-0 flex h-5 items-center gap-1 whitespace-nowrap rounded-md bg-hud px-1.5 font-mono text-hud-sub text-foreground ring-1 will-change-transform'

export class LabelLayer {
  readonly cand = new LabelCandidates()
  private readonly declutter = new Declutter(LABELS.maxCandidates)
  private readonly accepted = new Int32Array(LABELS.maxPool)
  acceptedN = 0
  private readonly slots: Slot[] = []
  private container: HTMLElement | null = null
  cap: number
  private lastTextMs = Number.NEGATIVE_INFINITY
  private offsetPx: number = LABELS.offsetPx
  private textFn: LabelTextFn = (_k, key, _e, out) => {
    out.id = String(key)
    out.sub = ''
  }
  /** DOM writes of the last overlay pass (transform, display, text) for the tests and __perf.ui.labels */
  readonly writes = { transform: 0, display: 0, text: 0 }
  private readonly tmp = new Float32Array(2)
  private readonly p3 = new Float64Array(3)

  constructor(readonly tier: 'A' | 'B' | 'S', private readonly project: (enu: ArrayLike<number>, out: Float32Array) => boolean) {
    this.cap = tier === 'S' ? DECLUTTER.capS : DECLUTTER.capBA
  }

  setTextFn(fn: LabelTextFn): void {
    this.textFn = fn
    for (const s of this.slots) s.key = -0x7fffffff
  }

  /** PerfGovernor step 3 */
  setCap(n: number): void {
    this.cap = Math.max(0, Math.min(this.slots.length || LABELS.maxPool, n))
  }

  /** create the element pool inside the overlay container (LabelHost) */
  mount(container: HTMLElement): void {
    this.unmount()
    this.container = container
    const n = this.tier === 'S' ? DECLUTTER.capS : DECLUTTER.capBA
    try {
      const v = getComputedStyle(container).getPropertyValue('--space-label-offset').trim()
      const px = v.endsWith('rem') ? Number.parseFloat(v) * 16 : Number.parseFloat(v)
      if (Number.isFinite(px) && px > 0) this.offsetPx = px
    } catch {
      /* keep the default */
    }
    const doc = container.ownerDocument
    for (let i = 0; i < n; i++) {
      const el = doc.createElement('div')
      el.className = `${BASE} ring-foreground/10`
      el.style.display = 'none'
      el.dataset.label = ''
      const svg = doc.createElementNS(SVG_NS, 'svg')
      svg.setAttribute('class', 'ic size-3 shrink-0')
      svg.setAttribute('aria-hidden', 'true')
      svg.style.display = 'none'
      svg.style.color = 'var(--icon-alert)'
      const use = doc.createElementNS(SVG_NS, 'use')
      svg.appendChild(use)
      const idEl = doc.createElement('span')
      idEl.className = 'id'
      const subEl = doc.createElement('span')
      subEl.className = 'sub text-muted-foreground'
      el.append(svg, idEl, subEl)
      container.appendChild(el)
      this.slots.push({ el, use, svg, idEl, subEl, shown: false, x: Number.NaN, y: Number.NaN, kind: -1, key: -0x7fffffff, icon: 0, red: 0, expanded: 0, text: { id: '', sub: '' } })
    }
    this.cap = Math.min(this.cap, n)
  }

  unmount(): void {
    for (const s of this.slots) s.el.remove()
    this.slots.length = 0
    this.container = null
  }

  /** world phase: project, sort and place the candidates collected in `cand` */
  layout(ctx: FrameCtx): void {
    const c = this.cand
    let m = 0
    for (let i = 0; i < c.n; i++) {
      this.p3[0] = c.pos[3 * i]
      this.p3[1] = c.pos[3 * i + 1]
      this.p3[2] = c.pos[3 * i + 2]
      if (!this.project(this.p3, this.tmp)) continue
      // compact in place (visible candidates first)
      c.sx[m] = this.tmp[0]
      c.sy[m] = this.tmp[1] - this.offsetPx
      if (m !== i) {
        c.kind[m] = c.kind[i]
        c.key[m] = c.key[i]
        c.prio[m] = c.prio[i]
        c.icon[m] = c.icon[i]
        c.red[m] = c.red[i]
        c.expanded[m] = c.expanded[i]
        c.dist[m] = c.dist[i]
        c.pos[3 * m] = c.pos[3 * i]
        c.pos[3 * m + 1] = c.pos[3 * i + 1]
        c.pos[3 * m + 2] = c.pos[3 * i + 2]
      }
      m++
    }
    this.acceptedN = this.declutter.place(m, c.sx, c.sy, c.prio, c.dist, ctx.cssW, ctx.cssH, Math.min(this.cap, this.slots.length || this.cap), this.accepted)
  }

  /** overlay phase: DOM writes (transform per frame, text at 4 / 10 Hz) */
  write(ctx: FrameCtx): void {
    const w = this.writes
    w.transform = 0
    w.display = 0
    w.text = 0
    const c = this.cand
    const textTick = ctx.nowMs - this.lastTextMs >= (ctx.tier === 'S' ? MOTION.telemetryTextIntervalMs.S : MOTION.telemetryTextIntervalMs.B) - 1
    if (textTick) this.lastTextMs = ctx.nowMs
    for (let k = 0; k < this.slots.length; k++) {
      const s = this.slots[k]
      if (k >= this.acceptedN) {
        if (s.shown) {
          s.el.style.display = 'none'
          s.shown = false
          s.key = -0x7fffffff
          w.display++
        }
        continue
      }
      const i = this.accepted[k]
      const changed = s.kind !== c.kind[i] || s.key !== c.key[i] || s.expanded !== c.expanded[i]
      if (changed || textTick) {
        this.textFn(c.kind[i], c.key[i], c.expanded[i] === 1, s.text)
        if (s.idEl.textContent !== s.text.id) {
          s.idEl.textContent = s.text.id
          w.text++
        }
        if (s.subEl.textContent !== s.text.sub) {
          s.subEl.textContent = s.text.sub
          w.text++
        }
        s.kind = c.kind[i]
        s.key = c.key[i]
        s.expanded = c.expanded[i]
      }
      if (s.icon !== c.icon[i]) {
        s.icon = c.icon[i]
        if (s.icon === 0) s.svg.style.display = 'none'
        else {
          s.use.setAttribute('href', `#awr-icon-${ICON_KEYS[s.icon]}`)
          s.svg.style.display = ''
        }
        w.text++
      }
      if (s.red !== c.red[i]) {
        s.red = c.red[i]
        s.el.className = `${BASE} ${s.red ? 'ring-brand' : 'ring-foreground/10'}`
      }
      const x = Math.round(c.sx[i])
      const y = Math.round(c.sy[i])
      if (!s.shown) {
        s.el.style.display = ''
        s.shown = true
        w.display++
      }
      if (!(Math.abs(x - s.x) < LABELS.moveEpsPx && Math.abs(y - s.y) < LABELS.moveEpsPx)) {
        s.x = x
        s.y = y
        s.el.style.transform = `translate3d(${x}px,${y}px,0) translate(-50%,-100%)`
        w.transform++
      }
    }
  }

  /** texts currently shown (tests) */
  shown(): { id: string; sub: string; x: number; y: number }[] {
    return this.slots.filter((s) => s.shown).map((s) => ({ id: s.idEl.textContent ?? '', sub: s.subEl.textContent ?? '', x: s.x, y: s.y }))
  }

  get poolSize(): number {
    return this.slots.length
  }
  get mounted(): boolean {
    return this.container !== null
  }
}
