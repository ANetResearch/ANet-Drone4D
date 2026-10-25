// D2.2-FX-landing (confirms an adversarial-review finding, D2.2-R-landing §2.3): the landing page's eager surface
// (Landing.tsx, LandingHeader.tsx, LandingSections.tsx) and its capacity line used only a few dozen keys out of the
// full ~2500-key zh-CN.json/en.json, but importing the shared app/i18n module pulled both complete dictionaries
// (~230 KB raw) into the landing page's first-load chunk regardless (about 92% of that chunk's bytes, by the
// review's own string-statistics method). virtual:awr-landing-i18n (vite.config.ts landingI18nPlugin) now filters
// the same two files down to landingKeys.ts's allowlist at build time instead.
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { landingI18nDict, LANDING_I18N_ID } from '../../vite.config.ts'
import { LANDING_I18N_KEYS, LANDING_LAB_I18N_KEYS } from '@/app/demo/landingKeys'
import { LAB_MEDIA, LAB_POINTS, MEDIA_FLIGHT, MEDIA_SHOTS, MEDIA_WIDE, NAV, NAV_KEY, sectionIndex, STORY } from '@/app/demo/LandingContent'

const WEB = resolve(import.meta.dirname, '../..')
const zhCN = JSON.parse(readFileSync(resolve(WEB, 'src/app/i18n/zh-CN.json'), 'utf8')) as Record<string, string>
const en = JSON.parse(readFileSync(resolve(WEB, 'src/app/i18n/en.json'), 'utf8')) as Record<string, string>

describe('landing i18n dictionary stays trimmed and in sync with the real content (D2.2-R-landing §2.3)', () => {
  it('virtual module id matches the LandingI18n.ts import', () => {
    expect(LANDING_I18N_ID).toBe('virtual:awr-landing-i18n')
  })

  it('filters both full dictionaries down to the allowlist, dropping the other ~2450 keys of each', () => {
    const dict = landingI18nDict(zhCN, en, LANDING_I18N_KEYS)
    expect(Object.keys(dict['zh-CN']).sort()).toEqual([...LANDING_I18N_KEYS].sort())
    expect(Object.keys(dict.en).sort()).toEqual([...LANDING_I18N_KEYS].sort())
    // the allowlist is "a few dozen" keys, nowhere near the ~2500 of the full dictionaries
    expect(LANDING_I18N_KEYS.length).toBeLessThan(200)
    expect(Object.keys(zhCN).length).toBeGreaterThan(2000)
  })

  it('every allowlisted key actually exists, and has non-empty text, in both full dictionaries', () => {
    for (const k of LANDING_I18N_KEYS) {
      expect(zhCN[k], `zh-CN.json missing ${k}`).toBeDefined()
      expect(en[k], `en.json missing or empty ${k}`).toBeTruthy()
    }
  })

  it('throws instead of silently shipping an empty string for a key missing from a dictionary', () => {
    const { 'landing.slogan': _drop, ...incomplete } = en
    expect(() => landingI18nDict(zhCN, incomplete, LANDING_I18N_KEYS)).toThrow(/missing key/)
  })

  it('the allowlist has not drifted from LandingContent.ts\'s real story chapters and media ids', () => {
    const expected = new Set(LANDING_I18N_KEYS)
    for (const c of STORY) {
      for (const f of ['eyebrow', 'title', 'body', 'alt']) {
        expect(expected.has(`landing.story.${c.id}.${f}`), `landing.story.${c.id}.${f} not in the landing allowlist`).toBe(true)
      }
      c.points.forEach((status, i) => {
        expect(expected.has(`landing.story.${c.id}.p${i + 1}`), `landing.story.${c.id}.p${i + 1} not in the landing allowlist`).toBe(true)
        expect(expected.has(`landing.status.${status}`), `landing.status.${status} not in the landing allowlist`).toBe(true)
      })
      expect(expected.has(`landing.story.${c.id}.p${c.points.length + 1}`), `landing.story.${c.id} lists more points than it has`).toBe(false)
    }
    for (const m of [MEDIA_FLIGHT, ...MEDIA_SHOTS, ...MEDIA_WIDE]) {
      expect(expected.has(`landing.media.${m.key}`), `landing.media.${m.key} not in the landing allowlist`).toBe(true)
    }
  })
})

// V0.3.1 (extension point 15, ADR-129): the Event Lab entries of the demo build with VITE_AWR_LAB_PUBLIC=1
describe('Event Lab entries of the landing page (VITE_AWR_LAB_PUBLIC=1, V0.3.1)', () => {
  const LAB_ONLY_KEYS = LANDING_LAB_I18N_KEYS.filter((k) => !LANDING_I18N_KEYS.includes(k))

  it('adds the Event Lab keys only to the switched allowlist; the default allowlist carries none of them', () => {
    expect(LANDING_I18N_KEYS.every((k) => LANDING_LAB_I18N_KEYS.includes(k))).toBe(true)
    expect(LAB_ONLY_KEYS.length).toBeGreaterThan(0)
    for (const k of LAB_ONLY_KEYS) expect(/^landing\.(lab\.|nav\.lab|media\.lab-)/.test(k), k).toBe(true)
    expect(LANDING_I18N_KEYS.some((k) => /^landing\.(lab\.|nav\.lab|media\.lab-)/.test(k))).toBe(false)
    const dict = landingI18nDict(zhCN, en, LANDING_LAB_I18N_KEYS)
    expect(Object.keys(dict.en).sort()).toEqual([...LANDING_LAB_I18N_KEYS].sort())
    expect(LANDING_LAB_I18N_KEYS.length).toBeLessThan(200)
  })

  it('covers every Event Lab media caption, point and navigation label, with no addresses in the texts (K9 D3 scan)', () => {
    const lab = new Set(LANDING_LAB_I18N_KEYS)
    for (const m of LAB_MEDIA) {
      expect(lab.has(`landing.media.${m.key}`), m.key).toBe(true)
      expect(m.file.startsWith('lab/') && existsSync(resolve(WEB, 'public/demo', m.file)), m.file).toBe(true)
    }
    LAB_POINTS.forEach((_icon, i) => {
      for (const f of ['title', 'body']) expect(lab.has(`landing.lab.p${i + 1}.${f}`)).toBe(true)
    })
    expect(lab.has(`landing.lab.p${LAB_POINTS.length + 1}.title`)).toBe(false)
    expect(lab.has(NAV_KEY['event-lab'])).toBe(true)
    for (const k of LAB_ONLY_KEYS) {
      for (const d of [zhCN, en]) expect(d[k]!.includes('/lab/'), k).toBe(false)
    }
  })

  it('keeps the attribution of the CC BY 4.0 world mesh complete in both languages', () => {
    for (const d of [zhCN, en]) {
      const a = d['landing.lab.attribution']!
      expect(a).toContain('oryahovo - crop #1 - metashape texture problems')
      for (const p of ['{author}', '{source}', '{license}']) expect(a).toContain(p)
    }
    expect(en['landing.lab.attribution']).toMatch(/modified/)
    expect(zhCN['landing.lab.attribution']).toMatch(/修改/)
  })

  it('numbers the sections in reading order with and without the Event Lab section', () => {
    expect(NAV).toEqual(['story', 'showcase', 'open-source']) // tests run without VITE_AWR_LAB_PUBLIC
    expect([sectionIndex('showcase', false), sectionIndex('open-source', false)]).toEqual(['02', '04'])
    expect([sectionIndex('event-lab', true), sectionIndex('showcase', true), sectionIndex('open-source', true)]).toEqual(['02', '03', '05'])
  })
})
