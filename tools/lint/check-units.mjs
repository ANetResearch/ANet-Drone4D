#!/usr/bin/env node
// UNIT-01 (AWR-03 §5.4; AWR-18 §13.1): wire field names follow the unit suffix dictionary packages/contracts/rt/units.json.
// Checks property names in packages/contracts/**/*.schema.json, command arguments in rt/commands.json and field paths in
// env/presets.json, plus identifiers and string keys in python/awr/** (excluding the generated contracts package):
//   - denied names (speed_ms ...) and denied suffixes (_sec, _kmh, _m_s ...) are violations;
//   - a property annotated with "unit" must carry the registered suffix for that unit (pos and units.json exceptions aside);
//   - `_ms` is milliseconds only: a `_ms` property annotated with any other unit is a violation.
// Usage: node tools/lint/check-units.mjs
import { Reporter, isMain, lineCol, readText, run, walk } from './_common.mjs'

const UNITS = JSON.parse(readText('packages/contracts/rt/units.json'))
const SUFFIXES = Object.entries(UNITS.suffixes).sort((a, b) => b[0].length - a[0].length)
const DENIED_SUFFIXES = Object.keys(UNITS.denied_suffixes)
const DENIED_NAMES = UNITS.denied_names
const EXCEPT = new Set(UNITS.exceptions.map((e) => e.field))
const suffixOf = (name) => SUFFIXES.find(([s]) => name.endsWith(s))

export function checkName(name, unit) {
  if (DENIED_NAMES[name]) return `${name}: write ${DENIED_NAMES[name]}`
  const d = DENIED_SUFFIXES.find((s) => name.endsWith(s))
  if (d) return `${name}: ${UNITS.denied_suffixes[d]}`
  if (unit !== undefined && name !== 'pos' && !EXCEPT.has(name)) {
    const s = suffixOf(name)
    if (!s) return `${name} has unit ${unit} but no registered suffix`
    if (s[1].unit !== unit) return `${name}: suffix ${s[0]} means ${s[1].unit}, schema says ${unit}${s[0] === '_ms' ? ' (_ms is milliseconds only)' : ''}`
  }
  return null
}

function walkSchema(node, cb, path = '') {
  if (Array.isArray(node)) return node.forEach((n, i) => walkSchema(n, cb, `${path}[${i}]`))
  if (!node || typeof node !== 'object') return
  if (node.properties && typeof node.properties === 'object') {
    for (const [k, v] of Object.entries(node.properties)) cb(k, v && typeof v === 'object' ? v.unit : undefined, `${path}.${k}`)
  }
  for (const [k, v] of Object.entries(node)) if (v && typeof v === 'object') walkSchema(v, cb, `${path}/${k}`)
}

if (isMain(import.meta.url)) {
  await run('check-units', async () => {
    const R = new Reporter('check-units')
    const jsonFiles = walk('packages/contracts', (r) => r.endsWith('.json') && !r.includes('/golden/') && !r.includes('/fixtures/') && !r.includes('/gen/') &&
      !r.endsWith('package.json') && !r.endsWith('rt/units.json'))
    for (const f of jsonFiles) {
      const text = readText(f)
      const doc = JSON.parse(text)
      const report = (name, unit) => {
        const msg = checkName(name, unit)
        if (!msg) return
        const i = text.indexOf(`"${name}"`)
        const [l, c] = i < 0 ? [1, 1] : lineCol(text, i)
        R.add(f, l, c, 'UNIT-01', msg)
      }
      walkSchema(doc, (k, unit) => report(k, unit))
      if (f.endsWith('env/presets.json')) for (const fl of doc.fields) report(fl.path.split('.').pop(), undefined)
    }
    const pyFiles = walk('python/awr', (r) => r.endsWith('.py') && !r.startsWith('python/awr/contracts/'))
    const IDENT = new RegExp(`\\b(${Object.keys(DENIED_NAMES).join('|')}|[a-z][a-z0-9_]*(?:${DENIED_SUFFIXES.map((s) => s.replace(/_/g, '_')).join('|')}))\\b`, 'g')
    for (const f of pyFiles) {
      const text = readText(f)
      if (text === null) continue
      const code = text.replace(/#[^\n]*/g, (m) => ' '.repeat(m.length))
      for (const m of code.matchAll(IDENT)) {
        const msg = checkName(m[1], undefined)
        if (!msg) continue
        const [l, c] = lineCol(code, m.index)
        R.add(f, l, c, 'UNIT-01', msg)
      }
    }
    R.finish({ json: jsonFiles.length, python: pyFiles.length })
  })
}
