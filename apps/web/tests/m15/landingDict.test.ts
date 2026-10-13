// D2.2-FX-landing (confirms an adversarial-review finding, D2.2-R-landing §2.3): the landing page's eager surface
// (Landing.tsx, LandingHeader.tsx, LandingSections.tsx) and its capacity line used only a few dozen keys out of the
// full ~2500-key zh-CN.json/en.json, but importing the shared app/i18n module pulled both complete dictionaries
// (~230 KB raw) into the landing page's first-load chunk regardless (about 92% of that chunk's bytes, by the
// review's own string-statistics method). virtual:awr-landing-i18n (vite.config.ts landingI18nPlugin) now filters
// the same two files down to landingKeys.ts's allowlist at build time instead.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { landingI18nDict, LANDING_I18N_ID } from '../../vite.config.ts'
import { LANDING_I18N_KEYS } from '@/app/demo/landingKeys'
import { CAPS_DELIVERED, CAPS_NEXT, MEDIA_FLIGHT, MEDIA_SHOTS, MEDIA_WIDE, ROADMAP, STACK_ROWS } from '@/app/demo/LandingContent'

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

  it('the allowlist has not drifted from LandingContent.ts\'s real capability, media, stack and roadmap ids', () => {
    const expected = new Set(LANDING_I18N_KEYS)
    for (const c of [...CAPS_DELIVERED, ...CAPS_NEXT]) {
      expect(expected.has(`landing.cap.${c.id}.title`), `landing.cap.${c.id}.title not in the landing allowlist`).toBe(true)
      expect(expected.has(`landing.cap.${c.id}.body`), `landing.cap.${c.id}.body not in the landing allowlist`).toBe(true)
      expect(expected.has(`landing.status.${c.status}`), `landing.status.${c.status} not in the landing allowlist`).toBe(true)
    }
    for (const m of [MEDIA_FLIGHT, ...MEDIA_SHOTS, ...MEDIA_WIDE]) {
      expect(expected.has(`landing.media.${m.key}`), `landing.media.${m.key} not in the landing allowlist`).toBe(true)
    }
    for (const row of STACK_ROWS) {
      expect(expected.has(`landing.stack.${row}`), `landing.stack.${row} not in the landing allowlist`).toBe(true)
      expect(expected.has(`landing.stack.${row}Value`), `landing.stack.${row}Value not in the landing allowlist`).toBe(true)
    }
    for (const r of ROADMAP) {
      expect(expected.has(`landing.road.${r.id}.name`), `landing.road.${r.id}.name not in the landing allowlist`).toBe(true)
      expect(expected.has(`landing.road.${r.id}.body`), `landing.road.${r.id}.body not in the landing allowlist`).toBe(true)
      expect(expected.has(`landing.status.${r.status}`), `landing.status.${r.status} not in the landing allowlist`).toBe(true)
    }
  })
})
