// Boot progress model and report sink (R-D2-01; AWR-04 §10.1, ADR-108; M15-FR-201, UX-FR-101, UX-FR-102; AWR-14 §20.3).
// Five weighted segments: bundle 0.15 (application chunks), world 0.10 (world.json and the hierarchy files), points 0.40
// (first-screen Range bytes), warming 0.30 (0.6 shader programs + 0.4 uiWarm sub-phases) and reveal 0.05 (every boot gate
// resolved). The bar only shows the gates of the D1 BootController, it never gates the reveal itself.
// Rules: every segment only grows (a smaller estimate keeps the larger value) and the shown total never decreases; a model
// without the bundle segment (world switch and static browsing) renormalises the remaining weights.
// This module has no imports, so the engine (M05 first-screen bytes, M06 shader zoo counts) can report into it through
// reportBootProgress() without depending on the UI (AWR-03 §4.2), and the demo entry can start the bundle watcher before
// the application chunk is downloaded.

export type BootSeg = 'bundle' | 'world' | 'points' | 'warming' | 'reveal'
/** sub-parts of the warming segment (AWR-14 §20.3.2: 0.6 shader programs, 0.4 uiWarm sub-phases) */
export type WarmPart = 'shaders' | 'ui'

export const BOOT_SEGS: readonly BootSeg[] = ['bundle', 'world', 'points', 'warming', 'reveal']
export const SWITCH_SEGS: readonly BootSeg[] = ['world', 'points', 'warming', 'reveal']
export const BOOT_WEIGHTS: Readonly<Record<BootSeg, number>> = { bundle: 0.15, world: 0.1, points: 0.4, warming: 0.3, reveal: 0.05 }
export const WARM_SPLIT: Readonly<Record<WarmPart, number>> = { shaders: 0.6, ui: 0.4 }
/** phase text keys (i18n); the reveal segment has none */
export const BOOT_PHASE_KEY: Readonly<Record<BootSeg, string | null>> = {
  bundle: 'boot.phase.bundle', world: 'boot.phase.world', points: 'boot.phase.points', warming: 'boot.phase.warming', reveal: null,
}
/** write limit (<= 10 Hz) and the smallest change worth a write (half a pixel of the 320 px bar) */
export const BOOT_WRITE_MIN_MS = 100
export const BOOT_WRITE_MIN_STEP = 0.005

const clamp01 = (x: number): number => (Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0)

/** first-screen byte counts for the points phase text (AWR-14 §20.3.2) */
export interface PointsBytes { received: number; total: number }

/**
 * Pure aggregation of the segment fractions: monotone per segment, monotone in total, renormalised over the active
 * segments, and quantised to the segment boundaries for reduced motion.
 */
export class BootProgressModel {
  readonly f: Record<BootSeg, number> = { bundle: 0, world: 0, points: 0, warming: 0, reveal: 0 }
  readonly warm: Record<WarmPart, number> = { shaders: 0, ui: 0 }
  bytes: PointsBytes | null = null
  shown = 0
  active: readonly BootSeg[]

  constructor(active: readonly BootSeg[] = BOOT_SEGS) {
    this.active = active
  }

  /** a segment fraction; true when the segment grew */
  report(seg: BootSeg, x: number): boolean {
    const v = clamp01(x)
    if (v <= this.f[seg]) return false
    this.f[seg] = v
    return true
  }

  /** a warming sub-part; the warming segment is 0.6 shaders + 0.4 uiWarm */
  reportWarm(part: WarmPart, x: number): boolean {
    const v = clamp01(x)
    if (v <= this.warm[part]) return false
    this.warm[part] = v
    return this.report('warming', WARM_SPLIT.shaders * this.warm.shaders + WARM_SPLIT.ui * this.warm.ui)
  }

  /** weighted total over the active segments (0..1) */
  total(): number {
    let s = 0
    let w = 0
    for (const k of this.active) {
      s += BOOT_WEIGHTS[k] * this.f[k]
      w += BOOT_WEIGHTS[k]
    }
    return w > 0 ? s / w : 0
  }

  /** total of the completed segments only (the reduced-motion jump points) */
  quantised(): number {
    let s = 0
    let w = 0
    for (const k of this.active) {
      if (this.f[k] >= 1) s += BOOT_WEIGHTS[k]
      w += BOOT_WEIGHTS[k]
    }
    return w > 0 ? s / w : 0
  }

  /** the value to show now: never below the last shown value */
  next(reduced = false): number {
    if (this.f.reveal >= 1) return 1
    return Math.max(this.shown, reduced ? this.quantised() : this.total())
  }

  /** the first active segment that is not complete (its phase text is shown), null when all are */
  firstIncomplete(): BootSeg | null {
    for (const k of this.active) if (this.f[k] < 1) return k
    return null
  }

  /** start again with another segment set (a new loading layer) */
  reset(active: readonly BootSeg[] = this.active): void {
    this.active = active
    for (const k of BOOT_SEGS) this.f[k] = 0
    this.warm.shaders = 0
    this.warm.ui = 0
    this.bytes = null
    this.shown = 0
  }
}

// ------------------------------------------------------------ report sink (shared by the engine and the UI)
export interface BootReport { seg: BootSeg | null; part: WarmPart | null; x: number; bytes: PointsBytes | null }
type Sink = (r: BootReport) => void
const sinks = new Set<Sink>()
/** reports that arrived before the first sink was ever attached (engine modules may report before the mask bar starts);
 * later reports without a sink (between two loading layers) are dropped, never replayed into another bar */
const early: BootReport[] = []
const EARLY_MAX = 64
let everAttached = false

function emit(r: BootReport): void {
  if (sinks.size === 0) {
    if (!everAttached && early.length < EARLY_MAX) early.push(r)
    return
  }
  for (const s of sinks) s(r)
}

/** report the fraction of a segment (M05: 'world', 'points' with bytes; M06: warming through reportWarmPart) */
export function reportBootProgress(seg: BootSeg, x: number, bytes?: PointsBytes): void {
  emit({ seg, part: null, x, bytes: bytes ?? null })
}

/** report one part of the warming segment ('shaders': compiled programs / zoo size; 'ui': uiWarm sub-phases) */
export function reportWarmPart(part: WarmPart, x: number): void {
  emit({ seg: null, part, x, bytes: null })
}

/** attach a sink; reports that arrived before the first sink are replayed to it */
export function onBootProgress(cb: Sink): () => void {
  sinks.add(cb)
  everAttached = true
  if (early.length) {
    const xs = early.splice(0)
    for (const r of xs) cb(r)
  }
  return () => {
    sinks.delete(cb)
  }
}

/** apply one report to a model (the DOM writer and the loading layer share this) */
export function applyReport(m: BootProgressModel, r: BootReport): boolean {
  if (r.part) return m.reportWarm(r.part, r.x)
  if (!r.seg) return false
  if (r.seg === 'points' && r.bytes) m.bytes = r.bytes
  return m.report(r.seg, r.x)
}

// ------------------------------------------------------------ bundle watcher (Resource Timing; AWR-14 §20.3.2 bundle row)
export interface BootManifest { chunks: readonly { href: string; bytes: number }[]; total: number }

/** bundle fraction from the completed script resources: by bytes when a build manifest exists, else by count */
export function bundleFraction(done: ReadonlySet<string>, known: readonly string[], manifest: BootManifest | null, doneBytes: number): number {
  if (manifest && manifest.total > 0) {
    let b = 0
    for (const c of manifest.chunks) if (done.has(pathOf(c.href))) b += c.bytes
    return clamp01(Math.max(b, doneBytes) / manifest.total)
  }
  if (known.length === 0) return 0
  let n = 0
  for (const k of known) if (done.has(k)) n++
  return clamp01(n / known.length)
}

export function pathOf(href: string): string {
  try {
    return new URL(href, typeof location !== 'undefined' ? location.href : 'http://localhost/').pathname
  } catch {
    return href
  }
}
