// Public demo build hygiene of the experimental scene target authoring (vite.config.ts TARGET_LIFE_STUBS,
// stripTargetLifeKeys): the stubbed modules keep their export names, the dictionaries lose the authoring keys and the
// T7–T13 template names, and nothing else changes.
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { stripLabKeys, stripTargetLifeKeys, TARGET_LIFE_KEY_RE, TARGET_LIFE_STUBS, targetLifeStub } from '../../vite.config.ts'

const SRC = resolve(__dirname, '../../src')

describe('target-life public build strip', () => {
  it('stubs only existing modules and keeps their exported names', () => {
    for (const [rel, stub] of Object.entries(TARGET_LIFE_STUBS)) {
      const file = resolve(SRC, rel)
      expect(existsSync(file), rel).toBe(true)
      const src = readFileSync(file, 'utf8')
      const names = [...stub.matchAll(/export (?:function|const) (\w+)/g)].map((m) => m[1])
      expect(names.length, rel).toBeGreaterThan(0)
      for (const n of names) expect(src, `${rel} exports ${n}`).toMatch(new RegExp(`export (function|const) ${n}\\b`))
      expect(stub).not.toMatch(/target-life|deepseek|DeepSeek/)
      expect(targetLifeStub(file)).toBe(stub)
      expect(targetLifeStub(file.split('/').join('\\'))).toBe(stub)
    }
    expect(targetLifeStub(resolve(SRC, 'ui/panels/targets/TargetsPanel.tsx'))).toBeNull()
  })

  it('removes the authoring keys and the experimental template names from both dictionaries', () => {
    for (const lang of ['en', 'zh-CN']) {
      const raw = readFileSync(resolve(SRC, `app/i18n/${lang}.json`), 'utf8')
      const before = JSON.parse(stripLabKeys(raw)) as Record<string, string>
      const after = JSON.parse(stripTargetLifeKeys(stripLabKeys(raw))) as Record<string, string>
      const removed = Object.keys(before).filter((k) => !(k in after))
      expect(removed.length).toBeGreaterThan(0)
      expect(removed.every((k) => TARGET_LIFE_KEY_RE.test(k))).toBe(true)
      expect(Object.values(after).some((v) => /DeepSeek/.test(v))).toBe(false)
      for (const k of ['sandbox.template.t1_point_watch', 'sandbox.template.t6_manual.short', 'targets.rehearsal.noRecipe']) expect(after[k], k).toBeDefined()
      for (const k of ['sandbox.template.t7_target_appearance', 'sandbox.template.t13_all_vehicle_types.short']) expect(after[k], k).toBeUndefined()
      expect(Object.keys(after)).toEqual(Object.keys(before).filter((k) => !TARGET_LIFE_KEY_RE.test(k)))
    }
  })
})
