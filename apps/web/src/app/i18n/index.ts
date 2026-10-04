// i18n runtime (M15-FR-102, FR-103; AWR-14 §13.1, §13.7): t(key, params) with {name} and plural syntax, locales zh-CN and
// en with the fallback chain en -> zh-CN; D1 ships zh-CN only. Missing keys render the key name and warn in dev builds
// (M15-E002). Components under app/** and ui/** hold no CJK literals (lint I18N-01): all texts live in the JSON files.
import { useSyncExternalStore } from 'react'
import { DEMO_PUBLIC } from '@/lib/demo'
import { uxDefect } from '@/ui/testing/uxProbe'
import zhCN from './zh-CN.json'
import en from './en.json'
import reasonsZh from './reasons.zh-CN.gen.json'
import { formatMessage } from './plural'

export type Locale = 'zh-CN' | 'en'
type Dict = Readonly<Record<string, string>>
// public demo build (ADR-083): every read-only reason and the viewer role read "the public demo is read-only", and the
// live-rewind hint no longer points at the recordings list (not offered there); the keys stay the same, so every guard,
// Tooltip and notice that compares or shows them works unchanged
const DEMO_ALIASES: Readonly<Record<string, string>> = {
  'hint.readOnly': 'demo.readOnly', 'detail.readOnly': 'demo.readOnly', 'hint.needSeat': 'demo.readOnly',
  'hint.replaySeat': 'demo.readOnly', 'role.viewer': 'demo.role', 'role.viewerSeatTaken': 'demo.role',
  'hint.liveNoRewind': 'demo.liveNoRewind',
}
function withDemo(d: Dict): Dict {
  if (!DEMO_PUBLIC) return d
  const out: Record<string, string> = { ...d }
  for (const [k, v] of Object.entries(DEMO_ALIASES)) if (d[v]) out[k] = d[v]
  return out
}
const ZH: Dict = withDemo({ ...(zhCN as Dict), ...(reasonsZh as Dict) })
const DICTS: Record<Locale, Dict> = { 'zh-CN': ZH, en: withDemo(en as Dict) }

let locale: Locale = 'zh-CN'
let dict: Dict = ZH
const fallback: Dict = ZH
const listeners = new Set<() => void>()

export function t(key: string, params?: Record<string, string | number>): string {
  // an empty string in a locale file means "not translated yet": fall back to zh-CN
  const own = dict[key]
  const raw = own !== undefined && own !== '' ? own : fallback[key]
  if (raw === undefined) {
    if (import.meta.env.DEV) console.warn(`M15-E002 missing i18n key ${key}`)
    uxDefect('i18n.missing')
    return key
  }
  return formatMessage(raw, params, locale)
}

export function hasKey(key: string): boolean {
  return dict[key] !== undefined || fallback[key] !== undefined
}

export function setLocale(l: Locale): void {
  locale = l
  dict = DICTS[l]
  if (typeof document !== 'undefined') document.documentElement.lang = l
  for (const cb of listeners) cb()
}
export const getLocale = (): Locale => locale

const subscribe = (cb: () => void) => {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
/** t bound to the current locale; re-renders when the locale changes */
export function useT(): typeof t {
  useSyncExternalStore(subscribe, getLocale, getLocale)
  return t
}

/** short text and remedy of a reason code (AWR-17 §8); unknown codes get reason.unknown */
export function reasonText(code: number): { short: string; remedy: string } {
  const k = `reason.${code}.short`
  if (!hasKey(k)) return { short: t('reason.unknown', { code }), remedy: '' }
  return { short: t(k), remedy: t(`reason.${code}.remedy`) }
}

export const I18N_KEYS = { zh: Object.keys(ZH), en: Object.keys(en as Dict) }
