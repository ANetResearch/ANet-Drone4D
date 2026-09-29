// Number and unit formatting, the only implementation (M15-FR-105; AWR-15 §5.3-§5.4; AWR-03 §5.3, §5.4, §5.7).
// One cached Intl.NumberFormat per shape (en-US, ASCII digits); negative numbers use U+2212; unknown values (NaN, null,
// undefined, sentinel u8 255 or u16 0xFFFF) are an em dash; a value and its unit are separated by one space except
// for % and degrees. Domain labels (SIM T+ prefixes, wall-clock labels, "synthetic" badges) are added by the UI.
export const UNKNOWN = '—'
export const MINUS = '−'
const nf = new Map<string, Intl.NumberFormat>()
function fmtNum(v: number, min: number, max = min, group = false): string {
  const key = `${min}:${max}:${group ? 1 : 0}`
  let f = nf.get(key)
  if (!f) {
    f = new Intl.NumberFormat('en-US', { minimumFractionDigits: min, maximumFractionDigits: max, useGrouping: group, signDisplay: 'never' })
    nf.set(key, f)
  }
  const s = f.format(v)
  return v < 0 && s !== f.format(0) ? MINUS + s : s
}
const bad = (v: number | null | undefined): v is null | undefined => v === null || v === undefined || !Number.isFinite(v)
const sig3 = (v: number): number => (v === 0 ? 0 : Number(v.toPrecision(3)))
const pad3 = (d: number): string => String(d).padStart(3, '0')

export type AltRef = 'AGL' | 'MSL' | 'world'

export const fmt = {
  /** 82.3 m AGL */
  alt: (m: number | null | undefined, ref: AltRef = 'AGL'): string => {
    return bad(m) ? UNKNOWN : `${fmtNum(m, 1)} m ${ref}`
  },
  /** 7.2 m/s */
  speed: (mps: number | null | undefined): string => {
    return bad(mps) ? UNKNOWN : `${fmtNum(mps, 1)} m/s`
  },
  /** heading_deg = (90 - psi_enu_deg) mod 360, integer, 3 digits: 045 degrees (AWR-03 §5.3) */
  heading: (yawRad: number | null | undefined): string => {
    if (bad(yawRad)) return UNKNOWN
    const deg = Math.round((((90 - (yawRad * 180) / Math.PI) % 360) + 360) % 360) % 360
    return `${pad3(deg)}°`
  },
  /** wind direction (from), integer 3 digits */
  dirDeg: (deg: number | null | undefined): string => {
    if (bad(deg)) return UNKNOWN
    return `${pad3(Math.round(((deg % 360) + 360) % 360) % 360)}°`
  },
  /** 78% (u8 255 is unknown) */
  pct: (v: number | null | undefined, digits = 0): string => {
    return bad(v) || v === 255 ? UNKNOWN : `${fmtNum(v, digits)}%`
  },
  /** visibility as MOR: 850 m (3 significant digits) below 10 km, 12.0 km above (ADR-023) */
  mor: (m: number | null | undefined): string => {
    if (bad(m) || m < 0) return UNKNOWN
    return m < 10000 ? `${fmtNum(sig3(m), 0)} m` : `${fmtNum(m / 1000, 1)} km`
  },
  /** 22.0 mm/h */
  mmh: (v: number | null | undefined): string => {
    return bad(v) ? UNKNOWN : `${fmtNum(v, 1)} mm/h`
  },
  /** point counts with SI suffix, 3 significant digits: 4.82M, 350K, 25.0K */
  pts: (n: number | null | undefined): string => {
    if (bad(n)) return UNKNOWN
    const a = Math.abs(n)
    const sign = n < 0 ? MINUS : ''
    if (a < 1000) return sign + fmtNum(Math.round(a), 0)
    const [div, suf] = a < 1e6 ? [1e3, 'K'] : a < 1e9 ? [1e6, 'M'] : [1e9, 'G']
    const x = sig3(a / div)
    const digits = x >= 100 ? 0 : x >= 10 ? 1 : 2
    return `${sign}${fmtNum(x, digits)}${suf}`
  },
  /** binary bytes, 3 significant digits: 512 MiB, 1.25 GiB */
  bytes: (b: number | null | undefined): string => {
    if (bad(b) || b < 0) return UNKNOWN
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    let i = 0
    let x = b
    while (x >= 1024 && i < units.length - 1) {
      x /= 1024
      i++
    }
    if (i === 0) return `${fmtNum(x, 0)} B`
    const s = sig3(x)
    return `${fmtNum(s, s >= 100 ? 0 : s >= 10 ? 1 : 2)} ${units[i]}`
  },
  /** durations in ms: frame times 1 decimal by default, latencies with digits = 0 */
  ms: (v: number | null | undefined, digits = 1): string => {
    return bad(v) ? UNKNOWN : `${fmtNum(v, digits)} ms`
  },
  /** seconds: 3.2 s */
  sec: (v: number | null | undefined, digits = 1): string => {
    return bad(v) ? UNKNOWN : `${fmtNum(v, digits)} s`
  },
  /** frame rate: 30 fps */
  fps: (v: number | null | undefined): string => {
    return bad(v) ? UNKNOWN : `${fmtNum(v, 0)} fps`
  },
  /** plain counts; grouping only from 10000 (ISO 80000-1 practice, AWR-15 §5.4) */
  count: (n: number | null | undefined): string => {
    if (bad(n)) return UNKNOWN
    return fmtNum(Math.round(n), 0, 0, Math.abs(n) >= 10000)
  },
  /** simulation time from the session start: T+00:12:05.2 (nanoseconds as a number, exact to 2^53 ns) */
  simTime: (ns: number | null | undefined): string => {
    if (bad(ns) || ns < 0) return UNKNOWN
    const tenths = Math.floor(ns / 1e8)
    const s = Math.floor(tenths / 10)
    const h = Math.floor(s / 3600)
    const mm = Math.floor((s % 3600) / 60)
    const ss = s % 60
    return `T+${String(h).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}.${tenths % 10}`
  },
  /** wall clock (local time of day) from epoch milliseconds: 14:32:05 */
  wallTime: (ms: number | null | undefined): string => {
    if (bad(ms)) return UNKNOWN
    const d = new Date(ms)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
  },
  /** local ENU: E 120.45 · N −33.10 · U 82.30 */
  enu: (e: number | null | undefined, n: number | null | undefined, u: number | null | undefined): string => {
    if (bad(e) || bad(n) || bad(u)) return UNKNOWN
    return `E ${fmtNum(e, 2)} · N ${fmtNum(n, 2)} · U ${fmtNum(u, 2)}`
  },
  /** latitude, longitude with 7 decimals; synthetic anchors must show the "schematic" badge next to it (AWR-03 §5.1) */
  lla: (lat: number | null | undefined, lon: number | null | undefined): string => {
    if (bad(lat) || bad(lon)) return UNKNOWN
    return `${fmtNum(lat, 7)}, ${fmtNum(lon, 7)}`
  },
  /** data age: STALE 3.2 S (AWR-03 §5.7) */
  stale: (ageS: number | null | undefined): string => {
    return bad(ageS) ? 'STALE' : `STALE ${fmtNum(ageS, 1)} S`
  },
  /** raw number with fixed decimals and U+2212 */
  num: (v: number | null | undefined, digits = 0): string => {
    return bad(v) ? UNKNOWN : fmtNum(v, digits)
  },
} as const

export type Fmt = typeof fmt
