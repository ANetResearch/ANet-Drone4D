#!/usr/bin/env node
// Production bundle scan (M06-FR-011, M06-AC-010; ADR-044; AWR-18 §9.1 item 4; D2.1 M15-FR-236, M15-AC-227, ADR-123).
// Owner: M06.
//   1. test switches: a production build (no VITE_AWR_TEST_SWITCHES) must not contain the identifiers of the test switches
//      nor a `tier` query parsing branch: TEST_SWITCHES is a compile-time constant and every switch is read behind it.
//   2. REL-04 in the bundle (AWR-04 §15.9.5 item 2): when this machine has the deployment admin secret (the internal
//      identity passphrase: --secret-file, else AWR_ADMIN_SECRET_FILE, else ~/.config/awr/admin.secret, the rule and the
//      reader of tools/lint/check-release.mjs), its first line must not appear verbatim in any text file of the build;
//      compared in memory, never printed (only the file and the byte offset are reported). Tests use a temporary file with a
//      random value.
//   3. identity code (request d2-WP-12c-to-M06 item 4): the identity endpoint literal `/api/auth/identity` lives in one
//      chunk only, the one of stores/identity (no copy of identity code elsewhere); and the viewer's first-screen chunks
//      (the static import closure of the entry scripts of index.html, the landing page in the demo build, plus the static
//      closure of the application entry `main-*` that the entry imports dynamically) carry no hidden internal entry: the
//      page roots of the entries the internal identity restores are marked `data-internal-entry` (M15) and must sit in
//      lazy chunks. The count of marked chunks is reported.
//   node apps/web/tests/m06/lint/prod-bundle-scan.mjs [distDir] [--secret-file <path>]
//        (default apps/web/dist; `make scan-m06-bundle`, `make demo-build`, tools/deploy/emax/build-local.sh)
// Exit 0 clean, 1 findings, 2 not a production build (test hooks present) or no bundle.
// Output: `<asset> <identifier> ...context...` per hit (the secret never: `<file> REL-04 offset <n>`).
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, posix, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { deploySecret, DEPLOY_SECRET_MIN } from '../../../../../tools/lint/check-release.mjs'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const ROOT = resolve(HERE, '../../../../..')

/** identifiers that must not survive in a production bundle (M06-AC-010) */
export const FORBIDDEN = ['allowFallback', 'finishForBench', 'perfInject', 'fixedB', 'pcInject', 'selftestNoFix', 'get(`tier`)', 'get("tier")', "get('tier')"]
/** markers of a test build (test hooks of M06 and M05) */
export const TEST_MARKERS = ['__vp', '__pc']
/** the identity endpoint (stores/identity.ts IDENTITY_PATH, R79 and R80) */
export const IDENTITY_LITERAL = '/api/auth/identity'
/** the page-root marker of the hidden entries the internal identity restores (M15-FR-236) */
export const INTERNAL_MARKER = 'data-internal-entry'
/** text files of a build that the secret scan reads */
const TEXT_EXT = /\.(html|js|mjs|css|json|webmanifest|txt|map|svg|xml)$/i

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

/** files under dir (recursive), paths relative to dir with forward slashes */
function walk(dir, base = dir, out = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name)
    if (e.isDirectory()) walk(p, base, out)
    else if (e.isFile()) out.push(relative(base, p).split('\\').join('/'))
  }
  return out
}

/** REL-04 in the bundle: [file, 'REL-04', 'offset <n>'] for every text file holding the secret (value never returned) */
export function scanSecret(dist, secret) {
  if (!secret || secret.length < DEPLOY_SECRET_MIN || !existsSync(dist)) return []
  const hits = []
  for (const f of walk(dist)) {
    if (!TEXT_EXT.test(f)) continue
    const i = readFileSync(join(dist, f), 'utf8').indexOf(secret)
    if (i >= 0) hits.push([f, 'REL-04', `offset ${i}: the deployment admin secret (internal identity passphrase); rotate it with install.sh --admin-secret-file`])
  }
  return hits
}

/** module scripts and module preloads of an HTML page (paths as written: "/assets/x.js") */
export function entryScripts(html) {
  const out = new Set()
  for (const m of html.matchAll(/<script\b([^>]*)>/gi)) {
    const a = m[1]
    const src = /\bsrc=["']([^"']+)["']/i.exec(a)
    if (src && /\btype=["']module["']/i.test(a)) out.add(src[1])
  }
  for (const m of html.matchAll(/<link\b([^>]*)>/gi)) {
    const a = m[1]
    const href = /\bhref=["']([^"']+)["']/i.exec(a)
    if (href && /\brel=["']modulepreload["']/i.test(a)) out.add(href[1])
  }
  return [...out].filter((p) => /\.m?js$/.test(p))
}

/** relative chunk specifiers a chunk imports statically: import"./x.js", import{...}from"./x.js", export*from"./x.js" */
export function staticImports(text) {
  const out = new Set()
  for (const m of text.matchAll(/(?:\bfrom|\bimport)\s*(["'`])(\.{1,2}\/[^"'`\s]+?\.m?js)\1/g)) out.add(m[2])
  return [...out]
}

/** relative chunk specifiers a chunk imports dynamically: import("./x.js") */
export function dynamicImports(text) {
  const out = new Set()
  for (const m of text.matchAll(/\bimport\(\s*(["'`])(\.{1,2}\/[^"'`\s]+?\.m?js)\1\s*\)/g)) out.add(m[2])
  return [...out]
}

/** the static import closure of chunk files (dist-relative paths) */
function closure(dist, roots) {
  const seen = new Set()
  const stack = [...roots]
  while (stack.length) {
    const f = stack.pop()
    if (seen.has(f) || !existsSync(join(dist, f))) continue
    seen.add(f)
    const text = readFileSync(join(dist, f), 'utf8')
    for (const s of staticImports(text)) stack.push(posix.normalize(posix.join(posix.dirname(f), s)))
  }
  return seen
}

/**
 * the viewer's first-screen chunks: the static closure of index.html's entry scripts (the landing page in the demo build)
 * and of the application entry (`main-*`) when the entry closure imports it dynamically; dist-relative paths
 */
export function firstScreen(dist) {
  const html = join(dist, 'index.html')
  if (!existsSync(html)) return { entry: [], app: [], all: new Set() }
  const entry = entryScripts(readFileSync(html, 'utf8')).map((p) => p.replace(/^\.?\//, ''))
  const first = closure(dist, entry)
  const app = []
  for (const f of first) {
    for (const s of dynamicImports(readFileSync(join(dist, f), 'utf8'))) {
      const p = posix.normalize(posix.join(posix.dirname(f), s))
      if (/^main[-.]/.test(posix.basename(p))) app.push(p)
    }
  }
  const all = new Set([...first, ...closure(dist, app)])
  return { entry, app, all }
}

/**
 * identity containment: the chunks holding the identity endpoint literal, the chunks holding the internal-entry marker
 * and the first-screen ones among them; hits [file, rule, context]
 */
export function scanIdentity(dist) {
  const assets = join(dist, 'assets')
  const js = existsSync(assets) ? readdirSync(assets).filter((f) => /\.m?js$/.test(f)).map((f) => `assets/${f}`) : []
  const identity = []
  const marked = []
  for (const f of js) {
    const text = readFileSync(join(dist, f), 'utf8')
    if (text.includes(IDENTITY_LITERAL)) identity.push(f)
    if (text.includes(INTERNAL_MARKER)) marked.push(f)
  }
  const fs = firstScreen(dist)
  const hits = []
  if (identity.length > 1) for (const f of identity) hits.push([f, 'identity-code', `${IDENTITY_LITERAL} in ${identity.length} chunks; only the stores/identity chunk may hold it`])
  for (const f of marked) if (fs.all.has(f)) hits.push([f, 'internal-entry', `${INTERNAL_MARKER} in a first-screen chunk; the hidden entries load lazily (M15-FR-236)`])
  return { identity, marked, firstScreen: [...fs.all].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)), hits }
}

/**
 * scan a dist directory: { kind: 'clean' | 'hits' | 'test-build' | 'missing', hits: [asset, id, context][], notes };
 * opts.secret: the deployment admin secret value (null: no REL-04 scan)
 */
export function scanDist(dist, opts = {}) {
  const assets = join(dist, 'assets')
  if (!existsSync(assets)) return { kind: 'missing', hits: [], notes: [] }
  const files = readdirSync(assets).filter((f) => f.endsWith('.js'))
  if (files.length === 0) return { kind: 'missing', hits: [], notes: [] }
  const hits = []
  for (const f of files) {
    const text = readFileSync(join(assets, f), 'utf8')
    if (TEST_MARKERS.some((m) => text.includes(`${m}=`) || text.includes(`${m} =`) || text.includes(`.${m}`))) return { kind: 'test-build', hits: [[f, 'test hooks', '']], notes: [] }
    for (const [id, ctx] of scanText(text)) hits.push([f, id, ctx])
  }
  const notes = []
  if (opts.secret) {
    hits.push(...scanSecret(dist, opts.secret))
    notes.push('REL-04: scanned the build for the deployment admin secret')
  } else notes.push('REL-04: no deployment admin secret on this machine; not scanned')
  const id = scanIdentity(dist)
  hits.push(...id.hits)
  notes.push(`identity: ${IDENTITY_LITERAL} in ${id.identity.length} chunk(s); first screen ${id.firstScreen.length} chunk(s); `
    + `${INTERNAL_MARKER} in ${id.marked.length} chunk(s)`)
  return { kind: hits.length ? 'hits' : 'clean', hits, notes }
}

function isMainModule() {
  return !!process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
}

if (isMainModule()) {
  const args = process.argv.slice(2)
  const si = args.indexOf('--secret-file')
  const secretFile = si >= 0 ? args[si + 1] : null
  const pos = args.filter((a, i) => !a.startsWith('--') && !(si >= 0 && i === si + 1))
  const dist = resolve(pos[0] ?? join(ROOT, 'apps/web/dist'))
  if (secretFile && !existsSync(secretFile)) {
    console.error(`prod-bundle-scan: --secret-file ${secretFile} does not exist`)
    process.exit(2)
  }
  const secret = deploySecret(secretFile)
  const r = scanDist(dist, { secret: secret?.value ?? null })
  if (r.kind === 'missing') {
    console.error(`prod-bundle-scan: no bundle under ${dist}/assets (run vite build without VITE_AWR_TEST_SWITCHES first)`)
    process.exit(2)
  }
  if (r.kind === 'test-build') {
    console.error(`prod-bundle-scan: ${dist} is a test build (${r.hits[0][0]} has test hooks); build without VITE_AWR_TEST_SWITCHES`)
    process.exit(2)
  }
  for (const [f, id, ctx] of r.hits) console.log(`${f} ${id} ...${ctx}...`)
  for (const n of r.notes) console.log(`prod-bundle-scan: ${n}`)
  if (r.kind === 'hits') {
    console.error(`prod-bundle-scan: ${r.hits.length} finding(s) in the production bundle (M06-AC-010, REL-04, M15-FR-236)`)
    process.exit(1)
  }
  console.log(`prod-bundle-scan: clean (${dist})`)
}
