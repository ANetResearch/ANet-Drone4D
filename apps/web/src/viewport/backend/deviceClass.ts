// Device capability class (ADR-044; AWR-03 §3.5; M06 §6.2.2, FR-003, FR-013). Owner: M06.
// software: UNMASKED_RENDERER or adapter info names a software rasteriser (the WebGL string wins; only when it is
// masked does isFallbackAdapter decide). iGPU: integrated families by name, or the 300 ms microbench slower than the
// threshold. dGPU: everything else, +1 start rung when the microbench shows headroom. Results are cached per renderer
// string and browser major version for 30 days (localStorage awr.render.v1), together with the start-rung memory of
// FR-013 (CAS held at the lowest allowed rung for > 30 s in a session lowers the next start rung by one).
import type { DeviceClass, Tier } from '@/engine'

export const SOFTWARE_RE = /swiftshader|llvmpipe|softpipe|software|basic render/i
const IGPU_FAMILY: readonly RegExp[] = [
  /intel.*(?:hd|uhd|iris|xe) graphics/i, /radeon\(tm\) graphics/i, /radeon.*vega \d+ graphics/i, /apple m\d/i, /mali/i, /adreno/i, /powervr/i,
]
const INTEL_ARC_IGPU = /intel.*arc.*graphics/i
const INTEL_ARC_DGPU = /arc(?:\(tm\))?\s*a\d/i

export const MICROBENCH = { budgetMs: 300, runs: 5, points: 1_000_000, sizePx: 2, w: 960, h: 540, igpuMs: 4.0, dgpuPlusMs: 1.5, abortMs: 100 } as const
export const START_RUNG: Readonly<Record<DeviceClass, number>> = { software: 0, iGPU: 3, dGPU: 4 }
export const CACHE_KEY = 'awr.render.v1'
export const CACHE_TTL_MS = 30 * 24 * 3600 * 1000
export const PREF_FLOOR_MS = 30_000

export interface AdapterText { vendor?: string; architecture?: string; description?: string; isFallbackAdapter?: boolean }

export function adapterText(a: AdapterText | null | undefined): string {
  return a ? `${a.vendor ?? ''} ${a.architecture ?? ''} ${a.description ?? ''}`.trim() : ''
}

export function isSoftware(rendererName: string, adapter: AdapterText | null): boolean {
  if (rendererName !== '') return SOFTWARE_RE.test(rendererName) || SOFTWARE_RE.test(adapterText(adapter))
  return SOFTWARE_RE.test(adapterText(adapter)) || adapter?.isFallbackAdapter === true
}

/** iGPU family heuristic on the renderer string plus adapter info; false means "unknown, run the microbench" */
export function isIgpuFamily(text: string): boolean {
  if (INTEL_ARC_IGPU.test(text) && !INTEL_ARC_DGPU.test(text)) return true
  return IGPU_FAMILY.some((re) => re.test(text))
}

/** classification by name only: software, iGPU (family), or null (hardware of unknown class: microbench) */
export function classifyByName(rendererName: string, adapter: AdapterText | null): DeviceClass | null {
  if (isSoftware(rendererName, adapter)) return 'software'
  if (isIgpuFamily(`${rendererName} ${adapterText(adapter)}`)) return 'iGPU'
  return null
}

/** microbench median t_1M (ms) -> class and start-rung bonus (ADR-044; thresholds provisional until /bench data) */
export function classifyByMicrobench(t1M: number): { deviceClass: DeviceClass; bonus: number } {
  if (!(t1M <= MICROBENCH.igpuMs)) return { deviceClass: 'iGPU', bonus: 0 }
  return { deviceClass: 'dGPU', bonus: t1M <= MICROBENCH.dgpuPlusMs ? 1 : 0 }
}

export function startRungFor(dc: DeviceClass, bonus = 0, memoryMinus = 0): number {
  return Math.max(0, Math.min(5, START_RUNG[dc] + bonus - memoryMinus))
}
export const lowestRungFor = (tier: Tier): number => (tier === 'S' ? 0 : 2)
/** canvas DPR (M06 §6.5): software 0.5 fixed; iGPU min(dpr, 1.5); dGPU min(dpr, 2) */
export function dprFor(dc: DeviceClass, devicePixelRatio: number): number {
  return dc === 'software' ? 0.5 : dc === 'iGPU' ? Math.min(devicePixelRatio, 1.5) : Math.min(devicePixelRatio, 2)
}
/** back-compat helper of the D1-MS3 tests */
export function classifyRenderer(name: string): { software: boolean; deviceClass: DeviceClass } {
  if (SOFTWARE_RE.test(name)) return { software: true, deviceClass: 'software' }
  return { software: false, deviceClass: isIgpuFamily(name) ? 'iGPU' : 'dGPU' }
}

// ------------------------------------------------------------------ cache and start-rung memory (FR-013)
export interface DeviceCache {
  rendererKey: string
  t1M: number | null
  deviceClass: DeviceClass
  ts: number
  /** start rung minus one after a session held at the floor > 30 s; tierA=false demotes a Tier A preference to B */
  startMinus: number
  demoteA: boolean
}
interface StorageLike { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem(k: string): void }
function store(): StorageLike | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}
export function browserMajor(ua: string = typeof navigator !== 'undefined' ? navigator.userAgent : ''): string {
  const m = /(?:Chrome|Firefox|Version)\/(\d+)/.exec(ua)
  return m ? m[1] : '0'
}
export function rendererKey(rendererName: string, adapter: AdapterText | null, ua?: string): string {
  return `${rendererName || adapterText(adapter)}|${browserMajor(ua)}`
}
export function readCache(key: string, now = Date.now(), s: StorageLike | null = store()): DeviceCache | null {
  if (!s) return null
  try {
    const raw = s.getItem(CACHE_KEY)
    if (!raw) return null
    const c = JSON.parse(raw) as DeviceCache
    if (c.rendererKey !== key || !(now - c.ts < CACHE_TTL_MS)) return null
    return { ...c, startMinus: c.startMinus ?? 0, demoteA: c.demoteA ?? false }
  } catch {
    return null
  }
}
export function writeCache(c: DeviceCache, s: StorageLike | null = store()): void {
  try {
    s?.setItem(CACHE_KEY, JSON.stringify(c))
  } catch {
    /* quota or privacy mode: the cache is an optimisation */
  }
}
export function clearCache(s: StorageLike | null = store()): void {
  try {
    s?.removeItem(CACHE_KEY)
  } catch {
    /* ignore */
  }
}
/** FR-013: the CAS floor total of this session exceeded 30 s -> next start rung - 1 (Tier A: next start on Tier B) */
export function rememberFloorHeld(c: DeviceCache | null, key: string, deviceClass: DeviceClass, tier: Tier, now = Date.now(), s: StorageLike | null = store()): DeviceCache {
  const n: DeviceCache = c ?? { rendererKey: key, t1M: null, deviceClass, ts: now, startMinus: 0, demoteA: false }
  if (tier === 'A') n.demoteA = true
  else n.startMinus = Math.min(START_RUNG[deviceClass] + 1, (n.startMinus ?? 0) + 1)
  n.ts = now
  writeCache(n, s)
  return n
}
