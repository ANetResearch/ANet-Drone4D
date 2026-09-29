#!/usr/bin/env node
// Icon inventory verification (M15-FR-062, AC-038; d03 §4.1-§4.2; ADR-030):
//   1. every lucide name in tools/shadcn/icons/inventory.mjs exists in the installed lucide and is canonical (not an alias);
//   2. every custom icon parses and stays inside the 24 grid with a 1 px stroke margin;
//   3. every whitelisted morph pair has a morphicons plan; the report lists the subpath difference and the max residual
//      (rule 3 of d03 §4.2: max res <= 0.5 or pure rotation; rule 4, the contact sheet, stays a manual review).
// Exit 1 on missing or non-canonical names, a custom icon out of grid, or duplicate keys.
// Usage: node tools/shadcn/icons/verify.mjs [--json]
import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { INVENTORY, CUSTOM } from './inventory.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
const LUCIDE = join(ROOT, 'node_modules', 'lucide')
const pascal = (kebab) => kebab.split('-').map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join('')

const canonical = new Set(readdirSync(join(LUCIDE, 'dist', 'esm', 'icons')).filter((n) => n.endsWith('.mjs')).map((n) => pascal(n.slice(0, -4))))
const aliasOf = new Map()
const table = join(LUCIDE, 'dist', 'esm', 'iconsAndAliases.mjs')
if (existsSync(table)) {
  for (const m of readFileSync(table, 'utf8').matchAll(/export \{([^}]+)\} from '\.\/icons\/([a-z0-9-]+)\.mjs'/g)) {
    const canon = pascal(m[2])
    for (const part of m[1].split(',')) {
      const name = part.replace(/default as/, '').trim()
      if (name && name !== canon) aliasOf.set(name, canon)
    }
  }
}
const L = await import(join(LUCIDE, 'dist', 'esm', 'lucide.mjs'))
const { buildPlan, resampleIcon, iconToCubics } = await import(join(ROOT, 'node_modules', 'morphicons', 'dist', 'index.js'))

const errors = []
const keys = new Set()
const custom = (n) => (n.startsWith('custom:') ? n.slice(7) : Object.hasOwn(CUSTOM, n) ? n : null)
const resolve = (n) => (custom(n) ? CUSTOM[custom(n)] : L[n])
for (const [, key, name, partner] of INVENTORY) {
  if (keys.has(key)) errors.push(`duplicate key ${key}`)
  keys.add(key)
  for (const n of [name, partner]) {
    if (!n || custom(n)) continue
    if (aliasOf.has(n)) errors.push(`${key}: ${n} is an alias of ${aliasOf.get(n)} (ICON-03)`)
    else if (!canonical.has(n)) errors.push(`${key}: ${n} is not a lucide icon`)
  }
}
for (const [n, node] of Object.entries(CUSTOM)) {
  let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9
  for (const { pts } of iconToCubics(node)) {
    for (let i = 0; i < pts.length; i += 2) {
      mnx = Math.min(mnx, pts[i]); mxx = Math.max(mxx, pts[i]); mny = Math.min(mny, pts[i + 1]); mxy = Math.max(mxy, pts[i + 1])
    }
  }
  if (!(mnx - 1 >= 0 && mny - 1 >= 0 && mxx + 1 <= 24 && mxy + 1 <= 24)) errors.push(`custom ${n} leaves the 24 grid`)
}
const pairs = []
const seen = new Set()
for (const [, key, name, partner, mode, spring] of INVENTORY) {
  if (mode !== 'morph') continue
  const id = [name, partner].sort().join('|')
  if (seen.has(id)) continue
  seen.add(id)
  const A = resolve(name)
  const B = resolve(partner)
  const plan = buildPlan(resampleIcon(A), resampleIcon(B))
  const res = Math.max(...plan.items.map((i) => i.res))
  const theta = Math.max(...plan.items.map((i) => Math.abs(i.theta))) * 180 / Math.PI
  pairs.push({ key, pair: `${name}->${partner}`, spring: spring ?? 'snappy', subA: iconToCubics(A).length, subB: iconToCubics(B).length,
    maxRes: Number(res.toFixed(3)), maxThetaDeg: Number(theta.toFixed(1)) })
}
const geometries = new Set(INVENTORY.map((r) => r[2]))
const summary = { keys: keys.size, geometries: geometries.size, morphPairs: pairs.length, errors }
if (process.argv.includes('--json')) console.log(JSON.stringify({ ...summary, pairs }, null, 1))
else {
  console.table(pairs)
  console.log(`icons verify: ${keys.size} keys, ${geometries.size} geometries, ${pairs.length} morph pairs, ${errors.length} error(s)`)
  for (const e of errors) console.error(`  ${e}`)
}
process.exit(errors.length ? 1 : 0)
