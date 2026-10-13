// Dataset line of the WORLD group and the ECCV 2022 BibTeX (ADR-109 rule 3; D2-AC-24): the generated city says "free to
// redistribute", the six UrbanScene3D cities say "used under authorization of the dataset rights holders" whether or not
// their world.json was rebuilt after ADR-109 (packages built before it carry no dataset.terms and the old licence text),
// any other dataset keeps the generic line. The BibTeX shown in About and on the World Hub cards is the entry of the
// release documents (NOTICE, THIRD_PARTY_NOTICES.md, both READMEs).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { t } from '@/app/i18n'
import { URBANSCENE_BIBTEX, URBANSCENE_DATASET, datasetNoteKey } from '@/lib/demo'

const ROOT = resolve(import.meta.dirname, '../../../..')

describe('dataset note of the WORLD group', () => {
  it('picks the line from redistribution, terms and the dataset name', () => {
    expect(datasetNoteKey({ name: 'ANet Synthetic City', redistribution: true })).toBe('world.datasetFree')
    expect(datasetNoteKey({ name: URBANSCENE_DATASET, redistribution: false, terms: '经数据集权利方授权使用 (ADR-109)' })).toBe('world.datasetAuthorized')
    // a package built before ADR-109: no terms, licence "non-commercial research only"; the authorization still applies
    expect(datasetNoteKey({ name: 'UrbanScene3D', redistribution: false })).toBe('world.datasetAuthorized')
    expect(datasetNoteKey({ name: 'Other', redistribution: false, terms: 'site terms' })).toBe('world.datasetAuthorized')
    expect(datasetNoteKey({ name: 'Other', redistribution: false, terms: '  ' })).toBe('world.dataset')
    expect(datasetNoteKey({ name: 'Other' })).toBe('world.dataset')
    expect(datasetNoteKey(null)).toBe('world.dataset')
    expect(datasetNoteKey(undefined)).toBe('world.dataset')
  })

  it('words the authorized line without "research use"', () => {
    const line = t('world.datasetAuthorized', { name: 'UrbanScene3D', version: 'v0.0.1' })
    expect(line).toBe('数据 UrbanScene3D v0.0.1 · 经数据集权利方授权使用')
    expect(line).not.toContain('科研')
    expect(t('about.dataSummary')).toContain('经数据集权利方授权')
    expect(t('about.dataSummary')).not.toContain('科研')
  })
})

describe('UrbanScene3D BibTeX', () => {
  it('is the entry of every release document', () => {
    expect(URBANSCENE_BIBTEX.startsWith('@inproceedings{UrbanScene3D,')).toBe(true)
    for (const f of ['NOTICE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'README.zh-CN.md']) {
      const text = readFileSync(resolve(ROOT, f), 'utf8')
      expect(text, f).toContain(URBANSCENE_BIBTEX)
      expect(text, f).toContain('经数据集权利方授权使用')
    }
  })
})
