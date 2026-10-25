// M15-FR-102..104 (AWR-14 §13): every key used in app/** and ui/** exists in zh-CN.json, en has the same key set with
// fallback to zh-CN, plural and parameter formatting, reason code texts; components hold no CJK literals (I18N-01).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { hasKey, I18N_KEYS, reasonText, setLocale, t } from '@/app/i18n'
import { formatMessage } from '@/app/i18n/plural'
import { LAB_DICTS } from '@/app/lab-public/i18n'
import { SAMPLE_DICTS } from '@/app/lab-public/samples/text'

const SRC = join(import.meta.dirname, '..', '..', 'src')
function files(dir: string): string[] {
  const out: string[] = []
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) out.push(...files(p))
    else if (/\.tsx?$/.test(n)) out.push(p)
  }
  return out
}
const UI_FILES = [...files(join(SRC, 'app')), ...files(join(SRC, 'ui'))].filter((f) => !f.includes('/components/ui/'))
// the public read-only build (app/lab-public, AWR-14 §21.14) ships its own lab.public.* shard instead of the app dictionary
// 样例运行页（lab-public/samples）另带 lab.public.run.* 分片，缺键时回退外壳分片（samples/text.ts 的 sampleT）
const LAB_PUBLIC = join(SRC, 'app', 'lab-public')
const LAB_SAMPLES = join(LAB_PUBLIC, 'samples')
const keyDefined = (f: string, k: string): boolean => {
  if (f.startsWith(LAB_SAMPLES + '/') && SAMPLE_DICTS['zh-CN'][k] !== undefined) return true
  return f.startsWith(LAB_PUBLIC + '/') ? LAB_DICTS['zh-CN'][k] !== undefined : hasKey(k)
}

describe('dictionaries', () => {
  it('define every static key used by t()', () => {
    const missing: string[] = []
    for (const f of UI_FILES) {
      const src = readFileSync(f, 'utf8')
      for (const m of src.matchAll(/\bt\('([a-zA-Z0-9_.-]+)'/g)) if (!keyDefined(f, m[1])) missing.push(`${m[1]} (${f.slice(SRC.length + 1)})`)
    }
    expect(missing).toEqual([])
  })

  it('keep en and zh-CN key sets aligned (reason codes come from the generated file)', () => {
    const zh = new Set(I18N_KEYS.zh.filter((k) => !/^reason\.\d+\./.test(k)))
    const en = new Set(I18N_KEYS.en)
    expect([...zh].filter((k) => !en.has(k))).toEqual([])
    expect([...en].filter((k) => !zh.has(k))).toEqual([])
  })

  it('fall back to zh-CN for untranslated en entries', () => {
    const zhTitle = t('panel.world.title')
    setLocale('en')
    try {
      expect(t('panel.world.title')).toBe(zhTitle)
      expect(zhTitle).not.toBe('')
      expect(t('brand.product')).toBe('ANet4D')
    } finally {
      setLocale('zh-CN')
    }
  })

  it('returns the key for a missing entry', () => {
    expect(t('no.such.key')).toBe('no.such.key')
  })

  it('hold no CJK literals in app/** and ui/** sources (I18N-01)', () => {
    const offenders = UI_FILES.filter((f) => /[㐀-鿿]/.test(readFileSync(f, 'utf8').replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '')))
    expect(offenders.map((f) => f.slice(SRC.length + 1))).toEqual([])
  })
})

describe('formatMessage', () => {
  it('substitutes parameters', () => {
    expect(formatMessage('{a} and {b}', { a: 1, b: 'x' }, 'en')).toBe('1 and x')
    expect(formatMessage('no params', undefined, 'en')).toBe('no params')
  })

  it('selects plural forms with # as the number', () => {
    const msg = '{n, plural, =0 {none} one {# drone} other {# drones}}'
    expect(formatMessage(msg, { n: 0 }, 'en')).toBe('none')
    expect(formatMessage(msg, { n: 1 }, 'en')).toBe('1 drone')
    expect(formatMessage(msg, { n: 5 }, 'en')).toBe('5 drones')
  })

  it('formats the zh-CN parameterised texts', () => {
    expect(t('reason.unknown', { code: 999 })).toContain('999')
    expect(t('drones.battery', { pct: 78 })).toContain('78')
  })
})

describe('reasonText', () => {
  it('uses generated texts for known codes and the unknown template otherwise', () => {
    const known = I18N_KEYS.zh.find((k) => /^reason\.\d+\.short$/.test(k))
    expect(known).toBeDefined()
    const code = Number(known!.split('.')[1])
    expect(reasonText(code).short).toBe(t(known!))
    expect(reasonText(99_999).short).toContain('99999')
  })
})
