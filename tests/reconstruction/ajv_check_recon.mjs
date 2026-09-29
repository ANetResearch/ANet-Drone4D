#!/usr/bin/env node
// Ajv 8 strict structural check of Recon IR sessions (M01-AC-005: Python jsonschema and Ajv agree on the 12 mutations).
// Draft of tools/contracts/check-recon.mjs (request M01-to-M00). Schemas: packages/contracts/recon when it contains the
// merged M01 layout (session.schema.json), otherwise the staged drafts in python/awr/reconstruction/ir/schemas.
// Usage: node tests/reconstruction/ajv_check_recon.mjs <session_dir> [...]
// Prints one JSON line per session: {"dir": ..., "docs": {"session.json": true|false, ..., "frames.jsonl": true|false}}.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import Ajv2020 from 'ajv/dist/2020.js'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const CONTRACTS = process.env.AWR_CONTRACTS_DIR ?? join(ROOT, 'packages', 'contracts')
const MERGED = join(CONTRACTS, 'recon')
const RECON = existsSync(join(MERGED, 'session.schema.json')) ? MERGED : join(ROOT, 'python', 'awr', 'reconstruction', 'ir', 'schemas')

const ajv = new Ajv2020({ strict: true, allErrors: true, strictTuples: false, strictRequired: false, allowUnionTypes: true })
ajv.addKeyword({ keyword: 'x-note' })
ajv.addKeyword({ keyword: 'unit' })
const ids = new Map()
for (const dir of [join(CONTRACTS, 'schemas', 'world'), RECON]) {
  for (const n of readdirSync(dir)) {
    if (!n.endsWith('.schema.json')) continue
    const s = JSON.parse(readFileSync(join(dir, n), 'utf8'))
    ids.set(n, s.$id)
    ajv.addSchema(s)
  }
}
const DOCS = { 'session.json': 'session.schema.json', 'engine.json': 'engine.schema.json', 'rig.json': 'rig.schema.json',
  'cameras.json': 'cameras.schema.json', 'alignment.json': 'alignment.schema.json', 'qa.json': 'qa.schema.json' }
try {
  for (const d of process.argv.slice(2)) {
    const out = {}
    for (const [f, s] of Object.entries(DOCS)) {
      const p = join(d, f)
      if (!existsSync(p)) { out[f] = false; continue }
      out[f] = ajv.getSchema(ids.get(s))(JSON.parse(readFileSync(p, 'utf8'))) === true
    }
    const fv = ajv.getSchema(ids.get('frame.schema.json'))
    const rows = existsSync(join(d, 'frames.jsonl')) ? readFileSync(join(d, 'frames.jsonl'), 'utf8').split('\n').filter((l) => l.trim()) : []
    out['frames.jsonl'] = rows.every((l) => fv(JSON.parse(l)) === true)
    console.log(JSON.stringify({ dir: d, docs: out }))
  }
} catch (e) {
  console.error(`ajv_check_recon: tool error: ${e}`)
  process.exit(2)
}
