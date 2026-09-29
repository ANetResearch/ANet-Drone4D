#!/usr/bin/env node
// Ajv 8 strict structural check of World Package JSON files (M03-AC-002, DATA-AC-002; AWR-16 §15.1).
// Owner M00 (tools/contracts). Adopted from the M03 draft tests/world/ajv_check_world.mjs (request M03-to-M00 item 1);
// called by `make validate` (mk/m03.mk) and tests/world/test_ajv_worlds.py.
// Usage: node tools/contracts/check-world.mjs worlds/<id> [...]; exit 0 = every document valid, 1 = invalid, 2 = tool error.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import Ajv2020 from 'ajv/dist/2020.js'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const CONTRACTS = process.env.AWR_CONTRACTS_DIR ?? join(ROOT, 'packages', 'contracts')

const ajv = new Ajv2020({ strict: true, allErrors: true, strictTuples: false, strictRequired: false, allowUnionTypes: true })
ajv.addKeyword({ keyword: 'x-note' })
ajv.addKeyword({ keyword: 'unit' })
ajv.addFormat('date-time', /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/)
const ids = new Map()
for (const sub of ['schemas/world', 'env']) {
  for (const n of readdirSync(join(CONTRACTS, sub))) {
    if (!n.endsWith('.schema.json')) continue
    const s = JSON.parse(readFileSync(join(CONTRACTS, sub, n), 'utf8'))
    ids.set(n, s.$id)
    ajv.addSchema(s)
  }
}

const check = (schema, file) => {
  const v = ajv.getSchema(ids.get(schema))
  const doc = JSON.parse(readFileSync(file, 'utf8'))
  return v(doc) ? [] : (v.errors ?? []).map((e) => `${file}: ${e.instancePath} ${e.message}`)
}

let bad = 0
let docs = 0
try {
  for (const w of process.argv.slice(2)) {
    if (!existsSync(join(w, 'world.json'))) continue
    const world = JSON.parse(readFileSync(join(w, 'world.json'), 'utf8'))
    const jobs = [['world.schema.json', join(w, 'world.json')], ['coordinate.schema.json', join(w, 'coordinate.json')]]
    for (const L of world.layers ?? []) {
      const p = join(w, L.href)
      if (L.type === 'pointcloud' && L.role === 'visual') for (const r of L.roots ?? []) jobs.push(['pointcloud-metadata.schema.json', join(w, r.href, 'metadata.json')])
      else if (L.type === 'pointcloud') jobs.push(['pointcloud-source.schema.json', p])
      else if (L.type === 'terrain') jobs.push(['grid.schema.json', p])
      else if (L.type === 'class-table') jobs.push(['class-table.schema.json', p])
      else if (L.type === 'semantic-zones') jobs.push(['zones.schema.json', p])
      else if (L.type === 'environment') jobs.push(['env_world.schema.json', p])
    }
    if (existsSync(join(w, 'qa', 'report.json'))) jobs.push(['qa-report.schema.json', join(w, 'qa', 'report.json')])
    for (const [schema, file] of jobs) {
      docs += 1
      if (!existsSync(file) || !statSync(file).isFile()) { console.log(`${file}: missing`); bad += 1; continue }
      const errs = check(schema, file)
      if (errs.length) { bad += 1; for (const e of errs.slice(0, 10)) console.log(e) }
    }
  }
} catch (e) {
  console.error(`check-world: tool error: ${e}`)
  process.exit(2)
}
console.log(`${docs} documents, ${bad} invalid`)
process.exit(bad ? 1 : 0)
