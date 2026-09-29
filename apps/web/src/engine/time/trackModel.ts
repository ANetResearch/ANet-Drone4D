// Timeline track data model (M12 §6.5 "轨道数据模型", §7.1 TrackModel, §8.3; FR-017 to FR-019, FR-023, FR-024,
// FR-027). Owner: M12. Pure TS (no React, no ui/**): M15's LfTimelineTrack only reads it (M15 §7.1.6).
//   MarkerStore  fixed columns t (f64 ms from the run start), level u8, marker u8 (class | SUPERSEDED), agentNo u16
//                (0xFFFF none), mseq u32 (.evx write order in replay, global event seq low 32 bits live); kept sorted by
//                t; cap 500,000 (INFO markers are dropped first when full, FR-018).
//   ColumnAgg    per pixel column of the current view: the highest-priority class, the count, the representative index
//                (latest marker of that class) and the highest level; buffers reused, recomputed only when the view,
//                the width or the data version changed. O(markers in view).
//   SeriesStore  the selected vehicle's altitude (m) at 4 Hz live, or the .ovw marked-vehicle track (1 Hz) in replay.
//   RangeSet     rerun (rollback), decimated, gap, stalled, loaded ranges.
//   TickPlan     main, sub and tiny ticks from the r15 §3.14 scale table (labels >= 64 px apart, layers >= 3 px).
//   heroIdx      the one red marker: the RedArbiter winner among unacknowledged critical markers (ADR-032).
// `version` increments on every data change so canvases redraw only when dirty.
import { arbitrate, RED_NONE, type RedCandidate, type RedState } from '@/lib/redArbiter'
import { MARKER_CLASS_MASK, MARKER_PRIORITY, MARKER_SUPERSEDED, MarkerClass } from './markers'
import { TIME_PARAMS as P } from './params'

export type RangeKind = 'rerun' | 'decimated' | 'gap' | 'stalled' | 'loaded'
export interface TrackRange { kind: RangeKind; t0S: number; t1S: number }
export interface ColumnAgg {
  n: number
  /** highest-priority marker class per column (MarkerClass; 0 = empty) */
  maxClass: Uint8Array
  /** highest event level per column (0..3; 255 = empty) */
  maxLevel: Uint8Array
  count: Uint32Array
  /** index into markers of the column's representative (latest of the winning class), -1 when empty */
  repIdx: Int32Array
}
export interface MarkerColumns {
  n: number
  t: Float64Array
  level: Uint8Array
  marker: Uint8Array
  agentNo: Uint16Array
  mseq: Uint32Array
}

const SCALES = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400]
const isMultiple = (a: number, b: number): boolean => {
  const r = a / b
  return Math.abs(r - Math.round(r)) < 1e-9
}

/** r15 §3.14 tick plan (seconds); sub and tiny only when their spacing is >= 3 px */
export function ticks(spanS: number, widthPx: number, labelPx = 64, smallestPx = 7): { main: number; sub?: number; tiny?: number } {
  const span = Math.max(1e-9, spanS)
  const w = Math.max(1, widthPx)
  const ideal = Math.min(labelPx / w, 1) * span
  const main = SCALES.find((s) => s > ideal) ?? SCALES[SCALES.length - 1]
  let sub: number | undefined
  for (let i = SCALES.length - 1; i >= 0; i--) {
    const s = SCALES[i]
    if (s < main && isMultiple(main, s)) {
      sub = s
      break
    }
  }
  const ref = sub ?? main
  let tiny: number | undefined
  for (const s of SCALES) {
    if (s < ref && isMultiple(ref, s) && (w * s) / span >= smallestPx) {
      tiny = s
      break
    }
  }
  const px = (s: number | undefined): number => (s === undefined ? 0 : (w * s) / span)
  return { main, sub: px(sub) >= 3 ? sub : undefined, tiny: px(tiny) >= 3 ? tiny : undefined }
}

export class TrackModel {
  version = 0
  readonly markers: MarkerColumns = {
    n: 0, t: new Float64Array(4096), level: new Uint8Array(4096), marker: new Uint8Array(4096),
    agentNo: new Uint16Array(4096), mseq: new Uint32Array(4096),
  }
  readonly series = { n: 0, t: new Float64Array(1024), v: new Float32Array(1024), unit: 'm' as const }
  ranges: TrackRange[] = []
  /** bookmark times (s), sorted; drawn as L5 */
  bookmarksS: number[] = []
  heroIdx = -1
  private red: RedState = RED_NONE
  private readonly agg: ColumnAgg = { n: 0, maxClass: new Uint8Array(0), maxLevel: new Uint8Array(0), count: new Uint32Array(0), repIdx: new Int32Array(0) }
  private aggKey = { t0: Number.NaN, t1: Number.NaN, w: -1, v: -1 }
  /** acknowledgement of critical markers (M15 alarm centre); default: nothing acknowledged */
  isAcked: (mseq: number) => boolean = () => false
  /** the last critical marker's arrival (wall ms) for the arbitration's recency */
  private lastCriticalWallMs = 0

  ticks(spanS: number, widthPx: number): { main: number; sub?: number; tiny?: number } {
    return ticks(spanS, widthPx)
  }

  private touch(): void {
    this.version++
  }

  private growMarkers(need: number): void {
    const m = this.markers
    if (need <= m.t.length) return
    const cap = Math.min(P.markersCap, Math.max(need, m.t.length * 2))
    const g = <T extends Float64Array | Uint8Array | Uint16Array | Uint32Array>(a: T): T => {
      const b = new (a.constructor as new (n: number) => T)(cap)
      b.set(a.subarray(0, m.n))
      return b
    }
    m.t = g(m.t)
    m.level = g(m.level)
    m.marker = g(m.marker)
    m.agentNo = g(m.agentNo)
    m.mseq = g(m.mseq)
  }

  /** drop INFO markers (oldest first) until 10 % of the cap is free; if none, the oldest markers */
  private makeRoom(): void {
    const m = this.markers
    const target = Math.floor(P.markersCap * 0.9)
    let drop = m.n - target
    let w = 0
    for (let r = 0; r < m.n; r++) {
      if (drop > 0 && m.level[r] === 0) {
        drop--
        continue
      }
      if (w !== r) this.copyMarker(r, w)
      w++
    }
    m.n = w
    if (m.n > target) {
      const k = m.n - target
      m.t.copyWithin(0, k, m.n)
      m.level.copyWithin(0, k, m.n)
      m.marker.copyWithin(0, k, m.n)
      m.agentNo.copyWithin(0, k, m.n)
      m.mseq.copyWithin(0, k, m.n)
      m.n -= k
    }
    this.heroIdx = -1
  }

  private copyMarker(from: number, to: number): void {
    const m = this.markers
    m.t[to] = m.t[from]
    m.level[to] = m.level[from]
    m.marker[to] = m.marker[from]
    m.agentNo[to] = m.agentNo[from]
    m.mseq[to] = m.mseq[from]
  }

  /** first index with t >= tMs (binary search) */
  lowerBound(tMs: number): number {
    const t = this.markers.t
    let lo = 0
    let hi = this.markers.n
    while (lo < hi) {
      const mid = (lo + hi) >>> 1
      if (t[mid] < tMs) lo = mid + 1
      else hi = mid
    }
    return lo
  }
  /** first index with t > tMs */
  upperBound(tMs: number): number {
    const t = this.markers.t
    let lo = 0
    let hi = this.markers.n
    while (lo < hi) {
      const mid = (lo + hi) >>> 1
      if (t[mid] <= tMs) lo = mid + 1
      else hi = mid
    }
    return lo
  }

  /** add one marker (live event batch); markers of class NONE are ignored; returns false when dropped */
  addMarker(tMs: number, level: number, marker: number, agentNo: number, mseq: number, wallMs = 0): boolean {
    if ((marker & MARKER_CLASS_MASK) === MarkerClass.NONE) return false
    const m = this.markers
    if (m.n >= P.markersCap) {
      if (level === 0) return false
      this.makeRoom()
    }
    this.growMarkers(m.n + 1)
    let i = m.n
    if (m.n > 0 && tMs < m.t[m.n - 1]) {
      i = this.upperBound(tMs)
      m.t.copyWithin(i + 1, i, m.n)
      m.level.copyWithin(i + 1, i, m.n)
      m.marker.copyWithin(i + 1, i, m.n)
      m.agentNo.copyWithin(i + 1, i, m.n)
      m.mseq.copyWithin(i + 1, i, m.n)
      if (this.heroIdx >= i) this.heroIdx++
    }
    m.t[i] = tMs
    m.level[i] = level
    m.marker[i] = marker
    m.agentNo[i] = agentNo
    m.mseq[i] = mseq >>> 0
    m.n++
    if ((marker & MARKER_CLASS_MASK) === MarkerClass.CRITICAL) this.lastCriticalWallMs = wallMs
    this.touch()
    return true
  }

  /** bulk replace from an .evx index (records already in t order; superseded kept with the flag) */
  loadEvx(n: number, t: Float64Array, level: Uint8Array, marker: Uint8Array, agentNo: Uint16Array, mseq: Uint32Array): void {
    const m = this.markers
    m.n = 0
    let keep = 0
    for (let i = 0; i < n; i++) if ((marker[i] & MARKER_CLASS_MASK) !== MarkerClass.NONE) keep++
    this.growMarkers(Math.min(keep, P.markersCap))
    for (let i = 0; i < n && m.n < P.markersCap; i++) {
      if ((marker[i] & MARKER_CLASS_MASK) === MarkerClass.NONE) continue
      const j = m.n++
      m.t[j] = t[i]
      m.level[j] = level[i]
      m.marker[j] = marker[i]
      m.agentNo[j] = agentNo[i]
      m.mseq[j] = mseq[i]
    }
    // .evx is written in arrival order; merged producers can interleave by a few ms: sort if needed
    let sorted = true
    for (let i = 1; i < m.n; i++) if (m.t[i] < m.t[i - 1]) sorted = false
    if (!sorted) this.sortMarkers()
    this.heroIdx = -1
    this.red = RED_NONE
    this.touch()
  }

  private sortMarkers(): void {
    const m = this.markers
    const idx = new Uint32Array(m.n)
    for (let i = 0; i < m.n; i++) idx[i] = i
    idx.sort((a, b) => m.t[a] - m.t[b] || a - b)
    const t = m.t.slice(0, m.n)
    const l = m.level.slice(0, m.n)
    const k = m.marker.slice(0, m.n)
    const a = m.agentNo.slice(0, m.n)
    const s = m.mseq.slice(0, m.n)
    for (let i = 0; i < m.n; i++) {
      const j = idx[i]
      m.t[i] = t[j]
      m.level[i] = l[j]
      m.marker[i] = k[j]
      m.agentNo[i] = a[j]
      m.mseq[i] = s[j]
    }
  }

  clearMarkers(): void {
    this.markers.n = 0
    this.heroIdx = -1
    this.red = RED_NONE
    this.touch()
  }

  /** mark markers in [t0, t1] (ms) with mseq older than the rollback as superseded (live rollback, FR-036 mirror) */
  markSuperseded(t0Ms: number, t1Ms: number): void {
    const m = this.markers
    for (let i = this.lowerBound(t0Ms), hi = this.upperBound(t1Ms); i < hi; i++) m.marker[i] |= MARKER_SUPERSEDED
    this.touch()
  }

  /** per-column aggregation of the view [t0S, t1S] over widthPx columns (buffers reused) */
  columns(t0S: number, t1S: number, widthPx: number): ColumnAgg {
    const W = Math.max(0, Math.floor(widthPx))
    const k = this.aggKey
    if (k.t0 === t0S && k.t1 === t1S && k.w === W && k.v === this.version) return this.agg
    k.t0 = t0S
    k.t1 = t1S
    k.w = W
    k.v = this.version
    const a = this.agg
    if (a.maxClass.length < W) {
      a.maxClass = new Uint8Array(W)
      a.maxLevel = new Uint8Array(W)
      a.count = new Uint32Array(W)
      a.repIdx = new Int32Array(W)
    }
    a.n = W
    a.maxClass.fill(0, 0, W)
    a.maxLevel.fill(255, 0, W)
    a.count.fill(0, 0, W)
    a.repIdx.fill(-1, 0, W)
    const span = (t1S - t0S) * 1000
    if (W === 0 || !(span > 0)) return a
    const m = this.markers
    const t0 = t0S * 1000
    const lo = this.lowerBound(t0)
    const hi = this.upperBound(t1S * 1000)
    for (let i = lo; i < hi; i++) {
      let c = Math.floor(((m.t[i] - t0) / span) * W)
      if (c >= W) c = W - 1
      if (c < 0) continue
      a.count[c]++
      const cls = m.marker[i] & MARKER_CLASS_MASK
      const cur = a.maxClass[c]
      if (a.repIdx[c] < 0 || MARKER_PRIORITY[cls] >= MARKER_PRIORITY[cur]) {
        a.maxClass[c] = cls
        a.repIdx[c] = i
      }
      const lv = m.level[i]
      if (a.maxLevel[c] === 255 || lv > a.maxLevel[c]) a.maxLevel[c] = lv
    }
    return a
  }

  /** marker index nearest to tS within tolS (hit test of a click, ±6 px converted by the caller); -1 when none */
  nearest(tS: number, tolS: number): number {
    const m = this.markers
    const lo = this.lowerBound((tS - tolS) * 1000)
    const hi = this.upperBound((tS + tolS) * 1000)
    let best = -1
    let bestD = Number.POSITIVE_INFINITY
    for (let i = lo; i < hi; i++) {
      const d = Math.abs(m.t[i] / 1000 - tS)
      if (d < bestD || (d === bestD && MARKER_PRIORITY[m.marker[i] & MARKER_CLASS_MASK] > MARKER_PRIORITY[m.marker[best] & MARKER_CLASS_MASK])) {
        best = i
        bestD = d
      }
    }
    return best
  }

  /**
   * next (dir 1) or previous (dir -1) jump target after/before tS: a bookmark or a >= WARNING marker (FR-026, 14 §6.10);
   * returns the time in seconds or NaN
   */
  nextMarkTime(tS: number, dir: 1 | -1, eps = 1e-3): number {
    const m = this.markers
    let best = Number.NaN
    if (dir > 0) {
      for (let i = this.upperBound((tS + eps) * 1000); i < m.n; i++) {
        if (m.level[i] >= 2 && (m.marker[i] & MARKER_SUPERSEDED) === 0) {
          best = m.t[i] / 1000
          break
        }
      }
      for (const b of this.bookmarksS) if (b > tS + eps && !(b >= best)) {
        best = b
        break
      }
    } else {
      for (let i = this.lowerBound((tS - eps) * 1000) - 1; i >= 0; i--) {
        if (m.level[i] >= 2 && (m.marker[i] & MARKER_SUPERSEDED) === 0) {
          best = m.t[i] / 1000
          break
        }
      }
      for (let i = this.bookmarksS.length - 1; i >= 0; i--) {
        const b = this.bookmarksS[i]
        if (b < tS - eps) {
          if (!(b <= best)) best = b
          break
        }
      }
    }
    return best
  }

  // ------------------------------------------------------------------ series
  /** append one altitude sample (ms, m) in time order; older than the last is ignored */
  pushSeries(tMs: number, v: number): void {
    const s = this.series
    if (s.n > 0 && tMs <= s.t[s.n - 1]) return
    if (s.n >= s.t.length) {
      if (s.t.length < P.seriesCap) {
        const cap = Math.min(P.seriesCap, s.t.length * 2)
        const t = new Float64Array(cap)
        const w = new Float32Array(cap)
        t.set(s.t)
        w.set(s.v)
        s.t = t
        s.v = w
      } else {
        const k = s.n >> 1
        s.t.copyWithin(0, k, s.n)
        s.v.copyWithin(0, k, s.n)
        s.n -= k
      }
    }
    s.t[s.n] = tMs
    s.v[s.n] = v
    s.n++
    this.touch()
  }
  /** replace the series (replay: .ovw marked-vehicle track) */
  setSeries(t: Float64Array, v: Float32Array, n: number): void {
    const s = this.series
    if (s.t.length < n) {
      s.t = new Float64Array(n)
      s.v = new Float32Array(n)
    }
    s.t.set(t.subarray(0, n))
    s.v.set(v.subarray(0, n))
    s.n = n
    this.touch()
  }
  clearSeries(): void {
    this.series.n = 0
    this.touch()
  }

  setRanges(r: TrackRange[]): void {
    this.ranges = r
    this.touch()
  }
  setBookmarks(tS: number[]): void {
    this.bookmarksS = tS.slice().sort((a, b) => a - b)
    this.touch()
  }

  // ------------------------------------------------------------------ one red (ADR-032)
  /**
   * Re-evaluate the HERO marker (every INPUT.redEvalIntervalMs by the caller): candidates are the unacknowledged,
   * non-superseded critical markers; the arbitration keeps the current owner for its dwell and otherwise picks the
   * latest. Returns true when heroIdx changed.
   */
  evalHero(nowWallMs: number): boolean {
    const m = this.markers
    let latest = -1
    for (let i = m.n - 1; i >= 0; i--) {
      if ((m.marker[i] & (MARKER_CLASS_MASK | MARKER_SUPERSEDED)) === MarkerClass.CRITICAL && !this.isAcked(m.mseq[i])) {
        latest = i
        break
      }
    }
    const cands: RedCandidate[] = []
    const cur = this.red.owner
    if (cur) {
      const i = this.indexOfMseq(Number(cur.id))
      if (i >= 0 && i !== latest && (m.marker[i] & (MARKER_CLASS_MASK | MARKER_SUPERSEDED)) === MarkerClass.CRITICAL && !this.isAcked(m.mseq[i])) {
        cands.push({ entity: cur, level: 'critical', rank: 3, tLastWallMs: this.red.ownerSinceMs })
      }
    }
    if (latest >= 0) cands.push({ entity: { kind: 'point', id: String(m.mseq[latest]) }, level: 'critical', rank: 3, tLastWallMs: Math.max(this.lastCriticalWallMs, nowWallMs) })
    this.red = arbitrate(this.red, cands, nowWallMs)
    const idx = this.red.owner ? this.indexOfMseq(Number(this.red.owner.id)) : -1
    if (idx !== this.heroIdx) {
      this.heroIdx = idx
      this.touch()
      return true
    }
    return false
  }

  private indexOfMseq(mseq: number): number {
    const m = this.markers
    const h = this.heroIdx
    if (h >= 0 && h < m.n && m.mseq[h] === mseq) return h
    for (let i = m.n - 1; i >= 0; i--) if (m.mseq[i] === mseq) return i
    return -1
  }
}
