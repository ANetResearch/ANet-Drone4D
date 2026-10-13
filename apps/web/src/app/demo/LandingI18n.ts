// Minimal, self-contained i18n runtime for the landing page's eager surface only (D2.2-FX-landing). The shared
// app/i18n runtime (zh-CN.json + en.json, ~2500 keys each) is the right tool for the full application and for the
// lazily loaded "open my sandbox" Sheet (SandboxLaunchSheet, StartSheet.tsx keep importing it directly), but the
// landing page itself used only a few dozen of those keys while still shipping both full dictionaries in its
// first-load chunk (~230 KB raw, about 92% of that chunk's bytes). virtual:awr-landing-i18n (vite.config.ts
// landingI18nPlugin) filters the same zh-CN.json/en.json down to landingKeys.ts's allowlist at build time, the same
// way virtual:awr-boot-i18n already does for the early boot module's capacity line, so the two full dictionaries
// stay the single source of truth and are never duplicated by hand. Switching the language here repaints only the
// landing page; the shared app/i18n locale is synced once, right before the sandbox Sheet lazily loads (Landing.tsx
// openSheet), so the Sheet and the rest of the application open already in the chosen language.
import * as React from 'react'
import { formatMessage } from '@/app/i18n/plural'
import { capacityTexts } from '@/app/boot/capacityText'
import LANDING_DICT from 'virtual:awr-landing-i18n'

export { bootCapacityFetch } from '@/app/boot/capacityText'
export type { CapacityLine } from '@/app/boot/capacityText'

export type Locale = 'zh-CN' | 'en'
type Dict = Readonly<Record<string, string>>
const DICTS: Record<Locale, Dict> = LANDING_DICT

let locale: Locale = 'zh-CN'
let dict: Dict = DICTS['zh-CN']
const listeners = new Set<() => void>()

export function t(key: string, params?: Record<string, string | number>): string {
  const raw = dict[key] ?? DICTS['zh-CN'][key]
  if (raw === undefined) {
    if (import.meta.env.DEV) console.warn(`landing i18n: missing key ${key}`)
    return key
  }
  return formatMessage(raw, params, locale)
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
  React.useSyncExternalStore(subscribe, getLocale, getLocale)
  return t
}

// capacity texts (R-D2-31, R-D2-32) over the local t: same formatting as app/i18n/capacity.ts (which binds the same
// pure capacityTexts() to the shared app/i18n runtime for the application and the lazy start Sheet)
const TEXTS = capacityTexts(t)
/** the text of one capacity response (or of a failed request: null) */
export const capacityLine = TEXTS.capacityLine
/** the "open my sandbox" button and Sheet hint: "needs queue - position y + 1 - about t" (null: no queue) */
export const sandboxQueueHint = TEXTS.sandboxQueueHint
