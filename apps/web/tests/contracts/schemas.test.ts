// All contract schemas compile under Ajv 2020 strict mode, and the contract data and fixtures validate (D1-AC-13; AWR-16 §15)
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import Ajv2020 from 'ajv/dist/2020.js'
import { contractsPath, readJson } from './util'

const root = contractsPath('')
const files: string[] = []
const walk = (u: URL, rel: string) => {
  for (const n of readdirSync(u)) {
    const p = new URL(n, u)
    if (statSync(p).isDirectory()) {
      if (!['gen', 'node_modules', 'fixtures', 'golden'].includes(n)) walk(new URL(`${n}/`, u), `${rel}${n}/`)
    } else if (n.endsWith('.schema.json')) files.push(`${rel}${n}`)
  }
}
walk(root, '')

const ajv = new Ajv2020({ strict: true, allErrors: true, strictTuples: false, strictRequired: false, allowUnionTypes: true })
ajv.addKeyword({ keyword: 'x-note' })
ajv.addKeyword({ keyword: 'unit' })
ajv.addFormat('date-time', /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/)
const ids = new Map<string, string>()
for (const f of files) {
  const s = JSON.parse(readFileSync(contractsPath(f), 'utf8')) as { $id: string }
  ids.set(f, s.$id)
  ajv.addSchema(s)
}

const validate = (schemaRel: string, data: unknown): string[] => {
  const v = ajv.getSchema(ids.get(schemaRel)!)!
  return v(data) ? [] : (v.errors ?? []).map((e) => `${e.instancePath} ${e.message}`)
}

describe('schemas', () => {
  it('at least 70 schemas', () => expect(files.length).toBeGreaterThanOrEqual(70))

  it.each(files)('%s compiles in strict mode', (f) => {
    expect(() => ajv.getSchema(ids.get(f)!)).not.toThrow()
  })
})

const DATA: [string, string][] = [
  ['rt/layouts.json', 'rt/layouts.schema.json'], ['rt/enums.json', 'rt/enums.schema.json'], ['rt/commands.json', 'rt/commands.schema.json'],
  ['rt/reasons.json', 'rt/reasons.schema.json'], ['rt/topics.json', 'rt/topics.schema.json'], ['rt/units.json', 'rt/units.schema.json'],
  ['rt/rng_streams.json', 'rt/rng_streams.schema.json'], ['rt/safety_codes.json', 'rt/safety_codes.schema.json'], ['rt/caps/mock.json', 'rt/caps.schema.json'],
  ['bus/keys.json', 'bus/keys.schema.json'], ['env/presets.json', 'env/presets.schema.json'], ['env/env_world_defaults.json', 'env/env_world_defaults.schema.json'],
  ['rec/mcap_channels.json', 'rec/mcap_channels.schema.json'], ['agent/capability_catalog.json', 'agent/capability_catalog.schema.json'],
  ['classes/anet-classes-v1.json', 'schemas/world/class-table.schema.json'],
  ['fixtures/world/shenzhen/world.json', 'schemas/world/world.schema.json'], ['fixtures/world/shenzhen/coordinate.json', 'schemas/world/coordinate.schema.json'],
  ['fixtures/world/sanfrancisco/pointcloud-metadata.json', 'schemas/world/pointcloud-metadata.schema.json'],
  ['fixtures/world/sanfrancisco/dtm_10m.json', 'schemas/world/grid.schema.json'], ['fixtures/scenario/s1-shenzhen-facade.json', 'scenario/scenario.schema.json'],
]

describe('data files and fixtures validate (Ajv)', () => {
  it.each(DATA)('%s', (data, schema) => {
    expect(validate(schema, readJson(data))).toEqual([])
  })

  it('negative: coordinate px4Boundary is a const', () => {
    const c = readJson<Record<string, Record<string, string>>>('fixtures/world/shenzhen/coordinate.json')
    c.conventions.px4Boundary = 'NED/FRD at gateway only'
    expect(validate('schemas/world/coordinate.schema.json', c).length).toBeGreaterThan(0)
  })
})
