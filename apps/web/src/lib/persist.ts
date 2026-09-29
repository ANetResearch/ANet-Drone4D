// localStorage persistence with full fault tolerance (M15-FR-032, NFR-015; AWR-14 §3.6): unavailable storage, quota
// errors, corrupt JSON and version mismatches all fall back to defaults silently (M15-E001, dev log only); values are
// clamped on read; writes are debounced (INPUT.persistDebounceMs) and flushed immediately when the page is hidden.
import { INPUT } from './tokens/input.gen'

export function loadSlice<T extends { v: number }>(key: string, v: number, def: T, clamp: (x: T) => T = (x) => x): T {
  try {
    const raw = globalThis.localStorage?.getItem(key)
    if (!raw) return def
    const obj = JSON.parse(raw) as T
    if (!obj || typeof obj !== 'object' || obj.v !== v) return def
    return clamp({ ...def, ...obj })
  } catch (e) {
    if (import.meta.env.DEV) console.info(`M15-E001 persist read ${key}`, e)
    return def
  }
}

export function saveSlice(key: string, value: unknown): boolean {
  try {
    globalThis.localStorage?.setItem(key, JSON.stringify(value))
    return true
  } catch (e) {
    if (import.meta.env.DEV) console.info(`M15-E001 persist write ${key}`, e)
    return false
  }
}

/** Debounced writer: returns schedule() and flush(); flush runs on visibilitychange (hidden) and pagehide. */
export function saveSliceDebounced(key: string, getter: () => unknown, ms: number = INPUT.persistDebounceMs) {
  let timer: ReturnType<typeof setTimeout> | null = null
  const flush = () => {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
      saveSlice(key, getter())
    }
  }
  const schedule = () => {
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      saveSlice(key, getter())
    }, ms)
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flush()
    })
    globalThis.addEventListener?.('pagehide', flush)
  }
  return { schedule, flush }
}

export const clampNum = (x: unknown, lo: number, hi: number, def: number): number =>
  typeof x === 'number' && Number.isFinite(x) ? Math.min(hi, Math.max(lo, x)) : def
