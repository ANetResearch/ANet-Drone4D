#!/usr/bin/env node
// Production bundle scan for the test switches (M06-FR-011, M06-AC-010; ADR-044; AWR-18 §9.1 item 4). Owner: M06.
// A production build (no VITE_AWR_TEST_SWITCHES) must not contain the identifiers of the test switches nor a `tier`
// query parsing branch: TEST_SWITCHES is a compile-time constant and every switch is read behind it.
//   node apps/web/tests/m06/lint/prod-bundle-scan.mjs [distDir]        (default apps/web/dist; `make scan-m06-bundle`)
// Exit 0 clean, 1 forbidden identifiers found, 2 not a production build (test hooks present) or no bundle.
// Output: `<asset> <identifier> ...context...` per hit.
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const ROOT = resolve(HERE, '../../../../..')

/** identifiers that must not survive in a production bundle (M06-AC-010) */
export const FORBIDDEN = ['allowFallback', 'finishForBench', 'perfInject', 'fixedB', 'pcInject', 'selftestNoFix', 'get(`tier`)', 'get("tier")', "get('tier')"]
/** markers of a test build (test hooks of M06 and M05) */
export const TEST_MARKERS = ['__vp', '__pc']

/** hits of one asset text: [identifier, context] */
export function scanText(text) {
  const hits = []
  for (const id of FORBIDDEN) {
    let i = text.indexOf(id)
    while (i >= 0 && hits.length < 50) {
      hits.push([id, text.slice(Math.max(0, i - 60), i + id.length + 40).replace(/\s+/g, ' ')])
      i = text.indexOf(id, i + id.length)
    }
  }
  return hits
}

/** scan a dist directory: { kind: 'clean' | 'hits' | 'test-build' | 'missing', hits: [asset, id, context][] } */
export function scanDist(dist) {
  const assets = join(dist, 'assets')
  if (!existsSync(assets)) return { kind: 'missing', hits: [] }
  const files = readdirSync(assets).filter((f) => f.endsWith('.js'))
  if (files.length === 0) return { kind: 'missing', hits: [] }
  const hits = []
  for (const f of files) {
    const text = readFileSync(join(assets, f), 'utf8')
    if (TEST_MARKERS.some((m) => text.includes(`${m}=`) || text.includes(`${m} =`) || text.includes(`.${m}`))) return { kind: 'test-build', hits: [[f, 'test hooks', '']] }
    for (const [id, ctx] of scanText(text)) hits.push([f, id, ctx])
  }
  return { kind: hits.length ? 'hits' : 'clean', hits }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const dist = resolve(process.argv[2] ?? join(ROOT, 'apps/web/dist'))
  const r = scanDist(dist)
  if (r.kind === 'missing') {
    console.error(`prod-bundle-scan: no bundle under ${dist}/assets (run vite build without VITE_AWR_TEST_SWITCHES first)`)
    process.exit(2)
  }
  if (r.kind === 'test-build') {
    console.error(`prod-bundle-scan: ${dist} is a test build (${r.hits[0][0]} has test hooks); build without VITE_AWR_TEST_SWITCHES`)
    process.exit(2)
  }
  for (const [f, id, ctx] of r.hits) console.log(`${f} ${id} ...${ctx}...`)
  if (r.kind === 'hits') {
    console.error(`prod-bundle-scan: ${r.hits.length} forbidden identifier(s) in the production bundle (M06-AC-010)`)
    process.exit(1)
  }
  console.log(`prod-bundle-scan: clean (${dist})`)
}
