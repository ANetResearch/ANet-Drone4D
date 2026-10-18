// 公开只读构建的文案运行时（AWR-14 §21.14；按 lab 分片，键前缀 lab.public.*）。Owner: M15.
// 与落地页的 LandingI18n.ts 同一做法：只带本构建的两份小词典（./i18n/zh-CN.json、./i18n/en.json），不加载应用的完整词典
// （app/i18n，约 2500 键）；格式化沿用 app/i18n/plural.ts 的 formatMessage（{name} 与 plural）。语言选择保存在 localStorage
// （不可用时跟随浏览器语言），与 D2 落地页使用同一个键，两处切换语言互相沿用。
import * as React from 'react'
import { formatMessage } from '@/app/i18n/plural'
import ZH from './i18n/zh-CN.json'
import EN from './i18n/en.json'

export type LabLocale = 'zh-CN' | 'en'
type Dict = Readonly<Record<string, string>>
export const LAB_DICTS: Readonly<Record<LabLocale, Dict>> = { 'zh-CN': ZH, en: EN }
/** 与落地页（app/demo/Landing.tsx）共用的语言键 */
export const LAB_LANG_KEY = 'awr.demo.lang'

let locale: LabLocale = 'zh-CN'
const listeners = new Set<() => void>()

export function t(key: string, params?: Record<string, string | number>): string {
  const raw = LAB_DICTS[locale][key] ?? LAB_DICTS['zh-CN'][key]
  if (raw === undefined) {
    if (import.meta.env.DEV) console.warn(`lab-public i18n: missing key ${key}`)
    return key
  }
  return formatMessage(raw, params, locale)
}

export const getLabLocale = (): LabLocale => locale

export function setLabLocale(l: LabLocale, persist = false): void {
  locale = l
  if (typeof document !== 'undefined') document.documentElement.lang = l
  if (persist) {
    try {
      globalThis.localStorage?.setItem(LAB_LANG_KEY, l)
    } catch {
      // storage unavailable (private mode): the choice lasts for this page only
    }
  }
  for (const cb of listeners) cb()
}

export function initialLabLocale(): LabLocale {
  try {
    const v = globalThis.localStorage?.getItem(LAB_LANG_KEY)
    if (v === 'zh-CN' || v === 'en') return v
  } catch {
    // storage unavailable: follow the browser language
  }
  return /^zh\b/i.test(globalThis.navigator?.language ?? 'zh-CN') ? 'zh-CN' : 'en'
}

const subscribe = (cb: () => void): (() => void) => {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
/** t bound to the current locale; re-renders when the locale changes */
export function useLabT(): typeof t {
  React.useSyncExternalStore(subscribe, getLabLocale, getLabLocale)
  return t
}
