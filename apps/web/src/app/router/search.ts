// Query parameters (M15-FR-004; AWR-14 §2.2): one pure parser per parameter; invalid values are ignored (undefined).
// `chrome=0` works in every build (canvas-only baseline, AWR-18 §9.5); tier, rb, allowFallback and motion only in dev and
// test builds; unknown parameters of other modules are preserved when the router writes the URL back.
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { LIMITS } from '@/lib/tokens/input.gen'

export type CamParam = 'orbit' | 'free' | 'third' | 'fpv' | 'bird'
export type SettingsTab = 'general' | 'render' | 'motion' | 'shortcuts' | 'account' | 'about'
export const WORLD_ID = /^[a-z0-9-]{1,63}$/

const CAMS: readonly CamParam[] = ['orbit', 'free', 'third', 'fpv', 'bird']
const TABS: readonly SettingsTab[] = ['general', 'render', 'motion', 'shortcuts', 'account', 'about']
const PANELS = ['perf', 'events', 'charts', 'mission', 'mission-edit', 'agents', 'timeline'] as const

export const parseCam = (q: URLSearchParams): CamParam | undefined => {
  const v = q.get('cam')
  return v && (CAMS as readonly string[]).includes(v) ? (v as CamParam) : undefined
}
export const parseSel = (q: URLSearchParams): string[] | undefined => {
  const v = q.get('sel')
  if (!v) return undefined
  const ids = v.split(',').filter((s) => /^[a-z0-9][a-z0-9_-]{0,63}$/i.test(s))
  return ids.length && ids.length <= LIMITS.selectionUrlMax ? ids : undefined
}
export const parsePanel = (q: URLSearchParams): string | undefined => {
  const v = q.get('panel')
  return v && (PANELS as readonly string[]).includes(v) ? v : undefined
}
export const parseSettings = (q: URLSearchParams): SettingsTab | undefined => {
  const v = q.get('settings')
  return v && (TABS as readonly string[]).includes(v) ? (v as SettingsTab) : undefined
}
export const parseT = (q: URLSearchParams): number | undefined => {
  const v = Number(q.get('t'))
  return q.has('t') && Number.isFinite(v) && v >= 0 ? v : undefined
}
export const parseChrome = (q: URLSearchParams): boolean => q.get('chrome') !== '0'
export const parseTier = (q: URLSearchParams): 'A' | 'B' | 'S' | undefined => {
  if (!TEST_SWITCHES) return undefined
  const v = q.get('tier')
  return v === 'A' || v === 'B' || v === 'S' ? v : undefined
}
