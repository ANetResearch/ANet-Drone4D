// M06-AC-010 scanner (tests/m06/lint/prod-bundle-scan.mjs): finds the test-switch identifiers and query parsing of
// `tier`, refuses test builds, and passes a clean production text.
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
// @ts-expect-error plain ESM script without type declarations
import { scanDist, scanText } from './lint/prod-bundle-scan.mjs'

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
