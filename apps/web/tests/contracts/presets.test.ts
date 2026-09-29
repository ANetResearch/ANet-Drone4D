// presets.json bytes, sha256 and field order; env golden headers (M07-AC-001; D1-AC-13 contract part)
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { FIELD_PATHS, NF, PRESETS, PRESETS_JSON, PRESETS_SHA256, PRESET_IDS, F_ATMOSPHERE_MOR_BG_M } from '@awr/contracts/presets'
import { contractsPath, readJson, sha256Hex } from './util'

describe('presets', () => {
  it('embedded bytes equal presets.json and the sha256 matches', () => {
    const raw = readFileSync(contractsPath('env/presets.json'), 'utf8')
    expect(PRESETS_JSON).toBe(raw)
    expect(sha256Hex(raw)).toBe(PRESETS_SHA256)
  })

  it('field order and indices', () => {
    const doc = readJson<{ fields: { path: string }[] }>('env/presets.json')
    expect(FIELD_PATHS).toEqual(doc.fields.map((f) => f.path))
    expect(NF).toBe(21)
    expect(FIELD_PATHS[F_ATMOSPHERE_MOR_BG_M]).toBe('atmosphere.mor_bg_m')
    expect(PRESET_IDS.length).toBe(12)
    expect(PRESETS.presets.map((p: { id: string }) => p.id)).toEqual([...PRESET_IDS])
  })

  it.each(['conventions', 'profile', 'derive', 'eval_env', 'optical_depth', 'gust', 'sector_slots', 'isa', 'anchors'])(
    'env golden %s is bound to these presets', (g) => {
      const d = readJson<{ presets_sha256: string; fields: string[]; cases: unknown[] }>(`env/golden/${g}.json`)
      expect(d.presets_sha256).toBe(PRESETS_SHA256)
      expect(d.fields).toEqual([...FIELD_PATHS])
      expect(d.cases.length).toBeGreaterThan(0)
    })
})
