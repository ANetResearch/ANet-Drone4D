// M06-AC-010 scanner (tests/m06/lint/prod-bundle-scan.mjs): finds the test-switch identifiers and query parsing of
// `tier`, refuses test builds, and passes a clean production text. D2.1 (M15-FR-236, M15-AC-227; request
// d2-WP-12c-to-M06 item 4): the deployment admin secret in any text file of the build (a random secret in a temporary
// file, never printed), the identity endpoint in one chunk only, and no hidden internal entry in the first-screen chunks.
import { spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
// @ts-expect-error plain ESM script without type declarations
import { dynamicImports, entryScripts, firstScreen, scanDist, scanIdentity, scanSecret, scanText, staticImports } from './lint/prod-bundle-scan.mjs'

const SCAN = fileURLToPath(new URL('./lint/prod-bundle-scan.mjs', import.meta.url))

function dist(files: Record<string, string>): string {
  const d = mkdtempSync(join(tmpdir(), 'm06-scan-'))
  mkdirSync(join(d, 'assets'))
  for (const [k, v] of Object.entries(files)) writeFileSync(join(d, 'assets', k), v)
  return d
}

describe('production bundle scan (M06-AC-010)', () => {
  it('finds forbidden identifiers and tier parsing', () => {
    expect(scanText('let a=q.get(`tier`);b.allowFallback=1').map((h: [string, string]) => h[0]).sort()).toEqual(['allowFallback', 'get(`tier`)'])
    expect(scanText('this.tier=e;let t=e.tier===`S`')).toEqual([])
    // M05 switches (?fixedB, ?pcInject) and the M06 injection are test-build only too (M06-to-M05 item 2; FX-WEB1)
    expect(scanText('p.fixedB=1;p.pcInject=.1;f.perfInject=`busyMs:5`').map((h: [string, string]) => h[0]).sort()).toEqual(['fixedB', 'pcInject', 'perfInject'])
  })
  it('classifies dist directories', () => {
    expect(scanDist(dist({ 'index-a.js': 'let x=1' })).kind).toBe('clean')
    expect(scanDist(dist({ 'index-a.js': 'o.finishForBench()' })).kind).toBe('hits')
    expect(scanDist(dist({ 'index-a.js': 'window.__vp={}' })).kind).toBe('test-build')
    expect(scanDist(join(tmpdir(), 'm06-scan-none')).kind).toBe('missing')
  })
})

/** a dist with index.html and assets (paths relative to the dist) */
function site(files: Record<string, string>): string {
  const d = mkdtempSync(join(tmpdir(), 'm06-site-'))
  mkdirSync(join(d, 'assets'))
  for (const [k, v] of Object.entries(files)) writeFileSync(join(d, k), v)
  return d
}
const HTML = '<!doctype html><head><script>early()</script><script type="module" crossorigin src="/assets/index-a.js"></script>'
  + '<link rel="modulepreload" crossorigin href="/assets/landing-b.js"></head>'

describe('first screen and identity containment (M15-FR-236)', () => {
  it('reads entry scripts, static and dynamic imports of minified chunks', () => {
    expect(entryScripts(HTML)).toEqual(['/assets/index-a.js', '/assets/landing-b.js'])
    expect(entryScripts('<script src="/x.js"></script><link rel="stylesheet" href="/a.css">')).toEqual([])
    const t = 'import{n as e}from"./landing-b.js";import"./boot-c.js";export*from"./x-d.js";const m=()=>import("./main-e.js")'
    expect(staticImports(t).sort()).toEqual(['./boot-c.js', './landing-b.js', './x-d.js'])
    expect(dynamicImports(t)).toEqual(['./main-e.js'])
  })

  it('the viewer first screen: entry closure plus the static closure of the dynamically imported main chunk', () => {
    const d = site({
      'index.html': HTML,
      'assets/index-a.js': 'import{a}from"./landing-b.js";const go=()=>import("./main-e.js");const api=()=>import("./apidocs-f.js")',
      'assets/landing-b.js': 'import"./boot-c.js";export const a=1',
      'assets/boot-c.js': 'export const b=2',
      'assets/main-e.js': 'import{u}from"./ui-g.js";u()',
      'assets/ui-g.js': 'export const u=()=>fetch("/api/auth/identity");const lazy=()=>import("./runs-h.js")',
      'assets/runs-h.js': 'export const R=()=>({"data-internal-entry":""})',
      'assets/apidocs-f.js': 'export const x=1',
    })
    const fs = firstScreen(d)
    expect(([...fs.all] as string[]).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))).toEqual(['assets/boot-c.js', 'assets/index-a.js', 'assets/landing-b.js', 'assets/main-e.js', 'assets/ui-g.js'])
    const r = scanIdentity(d)
    expect(r.identity).toEqual(['assets/ui-g.js'])
    expect(r.marked).toEqual(['assets/runs-h.js'])
    expect(r.hits).toEqual([])
    expect(scanDist(d).kind).toBe('clean')
    // a hidden entry in the first screen, and identity code copied into a second chunk
    writeFileSync(join(d, 'assets/ui-g.js'), 'export const u=()=>fetch("/api/auth/identity");export const R=()=>({"data-internal-entry":""})')
    writeFileSync(join(d, 'assets/apidocs-f.js'), 'export const y=()=>fetch("/api/auth/identity")')
    const h = scanIdentity(d).hits.map((x: string[]) => `${x[0]} ${x[1]}`).sort()
    expect(h).toEqual(['assets/apidocs-f.js identity-code', 'assets/ui-g.js identity-code', 'assets/ui-g.js internal-entry'])
    expect(scanDist(d).kind).toBe('hits')
  })

  it('REL-04 in the bundle: a random secret from a temporary file is found by file and offset, never printed', () => {
    const secret = `t-${randomBytes(12).toString('hex')}`
    const keyDir = mkdtempSync(join(tmpdir(), 'm06-secret-'))
    const keyFile = join(keyDir, 'admin.secret')
    writeFileSync(keyFile, `${secret}\n`)
    const d = site({ 'index.html': HTML, 'assets/index-a.js': 'export const a=1', 'assets/landing-b.js': 'export const b=1', 'build-info.json': '{}' })
    expect(scanSecret(d, secret)).toEqual([])
    const ok = spawnSync(process.execPath, [SCAN, d, '--secret-file', keyFile], { encoding: 'utf8' })
    expect(ok.status).toBe(0)
    expect(ok.stdout).toContain('REL-04: scanned')
    writeFileSync(join(d, 'assets/landing-b.js'), `export const b=${JSON.stringify(secret)}`)
    writeFileSync(join(d, 'build-info.json'), JSON.stringify({ note: secret }))
    expect(scanSecret(d, secret).map((h: string[]) => h[0]).sort()).toEqual(['assets/landing-b.js', 'build-info.json'])
    const bad = spawnSync(process.execPath, [SCAN, d, '--secret-file', keyFile], { encoding: 'utf8' })
    expect(bad.status).toBe(1)
    expect(bad.stdout).toContain('assets/landing-b.js REL-04')
    expect(`${bad.stdout}${bad.stderr}`).not.toContain(secret)
    // short values are not a secret (REL-04 minimum length)
    expect(scanSecret(d, 'abc')).toEqual([])
  })
})
