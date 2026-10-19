// 样例运行页的文案（lab 分片 lab.public.run.*，./i18n/zh-CN.json 与 ./i18n/en.json）。Owner: M15.
// 与公开构建外壳（../i18n.ts）同一做法：语言随外壳的语言选择（getLabLocale，切换时重渲染），格式化沿用 app/i18n/plural.ts；本分片
// 缺键时回退外壳词典（lab.public.*）。数据检视面板外壳文案取 ui/lab/inspect/text.ts 的独立翻译函数（不加载应用词典）。
import { formatMessage } from '@/app/i18n/plural'
import { inspectText } from '@/ui/lab/inspect/text'
import type { InspectT } from '@/ui/lab/inspect/InspectPanel'
import { getLabLocale, t as labT, useLabT, type LabLocale } from '../i18n'
import EN from './i18n/en.json'
import ZH from './i18n/zh-CN.json'

export const SAMPLE_DICTS: Readonly<Record<LabLocale, Readonly<Record<string, string>>>> = { 'zh-CN': ZH, en: EN }

export type SampleT = (key: string, params?: Record<string, string | number>) => string

export function sampleT(key: string, params?: Record<string, string | number>): string {
  const locale = getLabLocale()
  const raw = SAMPLE_DICTS[locale][key] ?? SAMPLE_DICTS['zh-CN'][key]
  return raw === undefined ? labT(key, params) : formatMessage(raw, params, locale)
}

/** 当前语言的样例页文案函数；语言切换时重渲染 */
export function useSampleT(): SampleT {
  useLabT()
  return sampleT
}

/** 数据检视面板外壳文案（随外壳语言） */
export function useInspectT(): InspectT {
  useLabT()
  const locale = getLabLocale()
  // inspectText 每次返回新函数：按语言缓存，避免面板因 t 变化而重渲染
  return INSPECT_T[locale]
}
const INSPECT_T: Readonly<Record<LabLocale, InspectT>> = { 'zh-CN': inspectText('zh-CN'), en: inspectText('en') }
