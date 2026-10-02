#!/usr/bin/env node
// Threshold-table coverage (AWR-18 §1.3 item 4; M16-to-M00 item 2): apps/web/perf/thresholds.json mirrors the numeric
// thresholds of AWR-18; G1 checks that every D1 P0 PERF-AC of the 18 acceptance table has an executable judgement.
// Rules (output `<file>:<line>:<col> <rule> <message>`; exit 0 pass, 1 violations, 2 tool error; summary in runs/lint/):
//   THR-01  a thresholds.json entry is malformed: op in <= >= < > ==, numeric value, unit, kind listed in `kinds`,
//           status frozen | provisional, non-empty source and ac[];
//   THR-02  an entry cites an acceptance id that does not exist (PERF-AC-* in the 18 table, D1-AC-* in AWR-03 §8.4);
//   THR-03  a D1 P0 PERF-AC (priority P0 without a later version tag such as "P0（V0.3）") has no threshold entry, no
//           registered harness case (acIds of perf/harness/cases/*.mjs and perf/<dir>/cases.mjs, or a registered case
//           that runs a spec named in the method column) and no G1 command (vitest, pytest, make, the harness selftest,
//           a lint or a *.test.ts in the table's method column);
//   THR-04  (reported; an error with --strict) a D1 P0 PERF-AC is judged only by assertions (a case or a G1 command) and
//           has no thresholds.json entry: the entry list for M16 to complete before G1 enforces --strict.
// Usage: node tools/ci/check-thresholds.mjs [--strict] [--json]
import { existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { ROOT, Reporter, isMain, readText, run } from '../lint/_common.mjs'

const THRESHOLDS = 'apps/web/perf/thresholds.json'
const DOC18 = 'docs/18-性能与测试方案.md'
const DOC03 = 'docs/03-设计基线与决策记录.md'
const OPS = new Set(['<=', '>=', '<', '>', '=='])
const STATUS = new Set(['frozen', 'provisional'])
const AC_RE = /^(PERF-AC-\d{3}|D1-AC-\d{2}[ab]?)$/
const G1_RE = /\b(vitest|pytest|make |selftest|lint|check-|--check)\b|\.test\.ts\b/

/** rows of the PERF-AC table in AWR-18: { id, name, method, priority, line } */
export function perfAcRows(text) {
  const out = []
  text.split('\n').forEach((l, i) => {
    const m = /^\| (PERF-AC-\d{3}) \|/.exec(l)
    if (!m) return
    const c = l.trim().replace(/^\||\|$/g, '').split('|').map((x) => x.trim())
    out.push({ id: m[1], name: c[1] ?? '', method: c[3] ?? '', priority: c[5] ?? '', line: i + 1 })
  })
  return out
}

/** D1 scope of a priority cell: P0 without a later version tag ("P0（B、S）/ P1（A）" counts, "P0（V0.3）" does not) */
export function isD1P0(priority) {
  if (!/^P0/.test(priority)) return false
  const tags = [...priority.matchAll(/V(\d+)\.(\d+)/g)].map((m) => Number(m[1]) * 100 + Number(m[2]))
  return tags.every((v) => v <= 1)
}

export function d1AcIds(text) {
  return new Set([...text.matchAll(/^\| (D1-AC-\d{2}[ab]?) \|/gm)].map((m) => m[1]))
}

/** line of `"key":` in the thresholds file (for the report position) */
function lineOf(text, key) {
  const i = text.indexOf(`"${key}"`)
  return i < 0 ? 1 : text.slice(0, i).split('\n').length
}

/** spec file names a method cell runs (`x.spec.ts`; "flight60 ..." means perf/flight60.spec.ts) */
export function methodSpecs(method) {
  const out = new Set([...method.matchAll(/([\w.-]+\.spec\.ts)/g)].map((m) => m[1]))
  if (/\bflight60\b/.test(method)) out.add('flight60.spec.ts')
  return out
}

/** acIds of every registered harness case (loaded without the registry validation, which lint-m16-registry owns);
 * the spec base names of the registered pw cases are returned under the key '@specs' */
export async function registryAcIds(perfDir = join(ROOT, 'apps', 'web', 'perf')) {
  const files = []
  const core = join(perfDir, 'harness', 'cases')
  if (existsSync(core)) for (const f of readdirSync(core).sort()) if (f.endsWith('.mjs') && !f.startsWith('_')) files.push(join(core, f))
  for (const d of readdirSync(perfDir).sort()) {
    const c = join(perfDir, d, 'cases.mjs')
    if (d !== 'harness' && statSync(join(perfDir, d)).isDirectory() && existsSync(c)) files.push(c)
  }
  const ids = new Map()
  const specs = new Map()
  for (const f of files) {
    const mod = await import(pathToFileURL(f).href)
    for (const c of mod.default ?? mod.cases ?? []) {
      for (const a of c.acIds ?? []) ids.set(a, [...(ids.get(a) ?? []), c.id])
      if (c.spec) {
        const b = c.spec.split('/').pop()
        specs.set(b, [...(specs.get(b) ?? []), c.id])
      }
    }
  }
  ids.set('@specs', specs)
  return ids
}

/** all checks; returns { violations: [{line, rule, msg}], assertionOnly: [...] } */
export function checkThresholds({ thrText, doc18, doc03, caseAcs, strict = false }) {
  const v = []
  let thr
  try {
    thr = JSON.parse(thrText)
  } catch (e) {
    return { violations: [{ line: 1, rule: 'THR-01', msg: `thresholds.json does not parse: ${e.message}` }], assertionOnly: [] }
  }
  const rows = perfAcRows(doc18)
  const perfIds = new Set(rows.map((r) => r.id))
  const d1Ids = d1AcIds(doc03)
  const kinds = new Set(Object.keys(thr.kinds ?? {}))
  const covered = new Set()
  for (const [key, e] of Object.entries(thr.entries ?? {})) {
    const line = lineOf(thrText, key)
    const bad = []
    if (!OPS.has(e.op)) bad.push(`op ${JSON.stringify(e.op)}`)
    if (typeof e.value !== 'number' || !Number.isFinite(e.value)) bad.push('value is not a number')
    if (typeof e.unit !== 'string' || !e.unit) bad.push('unit missing')
    if (!kinds.has(e.kind)) bad.push(`kind ${JSON.stringify(e.kind)} not in kinds`)
    if (!STATUS.has(e.status)) bad.push(`status ${JSON.stringify(e.status)}`)
    if (typeof e.source !== 'string' || !e.source) bad.push('source missing')
    if (!Array.isArray(e.ac) || !e.ac.length) bad.push('ac[] empty')
    if (bad.length) v.push({ line, rule: 'THR-01', msg: `${key}: ${bad.join('; ')}` })
    for (const a of Array.isArray(e.ac) ? e.ac : []) {
      covered.add(a)
      if (!AC_RE.test(a) || (a.startsWith('PERF-AC-') && !perfIds.has(a)) || (a.startsWith('D1-AC-') && !d1Ids.has(a))) {
        v.push({ line, rule: 'THR-02', msg: `${key}: unknown acceptance id ${a} (18 PERF-AC table / AWR-03 §8.4)` })
      }
    }
  }
  const assertionOnly = []
  for (const r of rows) {
    if (!isD1P0(r.priority) || covered.has(r.id)) continue
    const specs = caseAcs.get('@specs') ?? new Map()
    const cases = [...new Set([...(caseAcs.get(r.id) ?? []), ...[...methodSpecs(r.method)].flatMap((sp) => specs.get(sp) ?? [])])]
    const g1 = G1_RE.test(r.method)
    if (!cases.length && !g1) {
      v.push({ line: r.line, rule: 'THR-03', msg: `${r.id} ${r.name} (${r.priority}): no thresholds.json entry, no registered case and no G1 command`, doc: true })
      continue
    }
    assertionOnly.push({ id: r.id, name: r.name, by: cases.length ? cases.join(',') : 'G1' })
    if (strict) v.push({ line: r.line, rule: 'THR-04', msg: `${r.id} ${r.name}: judged by assertions only (${cases.length ? cases.join(',') : 'G1'}); add a thresholds.json entry`, doc: true })
  }
  return { violations: v, assertionOnly, p0: rows.filter((r) => isD1P0(r.priority)).length, entries: Object.keys(thr.entries ?? {}).length }
}

if (isMain(import.meta.url)) {
  await run('check-thresholds', async () => {
    const strict = process.argv.includes('--strict')
    const R = new Reporter('check-thresholds')
    const thrText = readText(THRESHOLDS)
    const doc18 = readText(DOC18)
    const doc03 = readText(DOC03)
    if (thrText === null || doc18 === null || doc03 === null) throw new Error('thresholds.json, 18 or 03 is missing')
    const r = checkThresholds({ thrText, doc18, doc03, caseAcs: await registryAcIds(), strict })
    for (const x of r.violations) R.add(x.doc ? DOC18 : THRESHOLDS, x.line, 1, x.rule, x.msg)
    if (!strict && r.assertionOnly.length) {
      R.note(`THR-04 (reported): ${r.assertionOnly.length} P0 PERF-AC judged by assertions only, no thresholds.json entry: `
        + r.assertionOnly.map((x) => `${x.id}[${x.by}]`).join(' '))
    }
    if (process.argv.includes('--json')) console.log(JSON.stringify(r, null, 1))
    R.finish({ p0: r.p0, entries: r.entries, assertion_only: r.assertionOnly.length })
  })
}
