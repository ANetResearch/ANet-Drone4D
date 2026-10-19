// 数据检视面板外壳文案的独立翻译函数：/lab/ 公开只读构建（F07 样例页）不加载应用词典（app/i18n，约 2500 键），以本函数直接读取
// lab 文案分片 app/i18n/lab/inspect.<locale>.json（与内部构建的应用词典是同一份文件），格式化沿用 app/i18n/plural.ts。
import { formatMessage } from '@/app/i18n/plural'
import EN from '@/app/i18n/lab/inspect.en.json'
import ZH from '@/app/i18n/lab/inspect.zh-CN.json'
import type { InspectT } from './InspectPanel'

export type InspectLocale = 'zh-CN' | 'en'
const DICTS: Readonly<Record<InspectLocale, Readonly<Record<string, string>>>> = { 'zh-CN': ZH, en: EN }

/** 按语言取外壳文案；缺键时回退中文，再回退键名 */
export function inspectText(locale: InspectLocale): InspectT {
  return (key, params) => formatMessage(DICTS[locale][key] ?? DICTS['zh-CN'][key] ?? key, params, locale)
}
