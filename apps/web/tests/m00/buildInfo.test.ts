// Build info and boot markup (D2.1 WP-16; AWR-04 §15.4, §15.5.2, §15.8.7; ADR-118, ADR-119; 17 §17.20.1; D2-AC-40 unit part).
// The vite plugin itself runs in `vite build` (checked by tools/deploy/emax/build-local.sh and tests/ops); here the pure helpers
// and the static index.html markup that M15 writes into.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { BUILD_RE, bootManifest, buildDisplay, buildMeta, collectBuildInfo, fillBootVersion, type AwrBuildInfo } from '../../vite.config.ts'

const WEB = resolve(import.meta.dirname, '../..')
const noGit = () => null
const NOW = new Date('2026-10-12T08:30:00.123Z')

describe('collectBuildInfo', () => {
  it('takes every field from the build environment (build-local.sh)', () => {
    const b = collectBuildInfo(
      { AWR_BUILD_CHANNEL: 'demo', AWR_BUILD_SHA: '3f9a1c2d4e', AWR_BUILD_DIRTY: 'false', AWR_BUILD_TIME: '2026-10-12T08:30:00Z', AWR_RELEASE: '20261012-083000-3f9a1c2d4e' },
      { demo: true, version: '0.2.0', git: noGit },
    )
    expect(b).toEqual({ version: '0.2.0', channel: 'demo', commit: '3f9a1c2d4e', dirty: false, built_at: '2026-10-12T08:30:00Z', release: '20261012-083000-3f9a1c2d4e' })
  })

  it('falls back to read-only git, the clock and local-<commit>', () => {
    const calls: string[][] = []
    const git = (args: string[]) => {
      calls.push(args)
      return args[0] === 'rev-parse' ? '3f9a1c2d4e5' : ' M apps/web/index.html'
    }
    const b = collectBuildInfo({}, { demo: false, version: '0.2.0', git, now: NOW })
    expect(b).toEqual({ version: '0.2.0', channel: 'local', commit: '3f9a1c2d4e', dirty: true, built_at: '2026-10-12T08:30:00Z', release: 'local-3f9a1c2d4e' })
    expect(calls).toEqual([['rev-parse', '--short=10', 'HEAD'], ['status', '--porcelain']])
  })

  it('without git: nogit, never dirty; CI and demo builds pick their channel', () => {
    expect(collectBuildInfo({}, { demo: false, version: '0.2.0', git: noGit, now: NOW })).toMatchObject({ commit: 'nogit', dirty: false, release: 'local-nogit', channel: 'local' })
    expect(collectBuildInfo({ CI: 'true' }, { demo: false, version: '0.2.0', git: noGit, now: NOW }).channel).toBe('ci')
    expect(collectBuildInfo({}, { demo: true, version: '0.2.0', git: noGit, now: NOW }).channel).toBe('demo')
  })

  it('honours SOURCE_DATE_EPOCH (reproducible builds)', () => {
    const b = collectBuildInfo({ SOURCE_DATE_EPOCH: '1791800000' }, { demo: true, version: '0.2.0', git: noGit, now: NOW })
    expect(b.built_at).toBe(new Date(1791800000 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z'))
  })

  it('rejects malformed values instead of shipping them', () => {
    const opts = { demo: true, version: '0.2.0', git: noGit, now: NOW }
    expect(() => collectBuildInfo({ AWR_BUILD_CHANNEL: 'prod' }, opts)).toThrow(/AWR_BUILD_CHANNEL/)
    expect(() => collectBuildInfo({ AWR_BUILD_SHA: 'xyz' }, opts)).toThrow(/AWR_BUILD_SHA/)
    expect(() => collectBuildInfo({ AWR_RELEASE: 'latest' }, opts)).toThrow(/AWR_RELEASE/)
    expect(() => collectBuildInfo({ AWR_BUILD_TIME: '2026-10-12' }, opts)).toThrow(/AWR_BUILD_TIME/)
    expect(() => collectBuildInfo({ AWR_BUILD_DIRTY: 'maybe' }, opts)).toThrow(/AWR_BUILD_DIRTY/)
    expect(() => collectBuildInfo({}, { ...opts, version: '0.2' })).toThrow(/X\.Y\.Z/)
  })

  it('release ids of the deploy kit and of local builds match 17 §17.20.1', () => {
    for (const ok of ['20261012-083000-3f9a1c2d4e', '20261012-083000-3f9a1c2d4e-dirty', '20261012-083000-nogit', 'local-3f9a1c2d4e', 'local-nogit']) {
      expect(BUILD_RE.release.test(ok), ok).toBe(true)
    }
    for (const bad of ['20261012-0830-3f9a1c2d4e', 'local-', '20261012-083000-3F9A1C2D4E']) expect(BUILD_RE.release.test(bad), bad).toBe(false)
  })
})

describe('display and meta', () => {
  const b: AwrBuildInfo = { version: '0.2.0', channel: 'demo', commit: '3f9a1c2d4e', dirty: false, built_at: '2026-10-12T08:30:00Z', release: '20261012-083000-3f9a1c2d4e' }
  it('boot-screen text and meta content', () => {
    expect(buildDisplay(b)).toBe('V0.2.0-demo · 3f9a1c2 · 2026-10-12')
    expect(buildDisplay({ ...b, dirty: true })).toBe('V0.2.0-demo · 3f9a1c2-dirty · 2026-10-12')
    expect(buildDisplay({ ...b, commit: 'nogit', channel: 'local' })).toBe('V0.2.0-local · nogit · 2026-10-12')
    expect(buildMeta(b)).toBe('0.2.0-demo+3f9a1c2d4e')
    expect(buildMeta({ ...b, dirty: true })).toBe('0.2.0-demo+3f9a1c2d4e-dirty')
  })
})

describe('index.html boot markup (filled by M00, written by M15)', () => {
  const html = readFileSync(resolve(WEB, 'index.html'), 'utf8')
  const mask = html.slice(html.indexOf('<div id="boot-mask"'), html.indexOf('<div id="root">'))

  it('the version placeholder is filled statically, before the track, inside the mask', () => {
    const b: AwrBuildInfo = { version: '0.2.0', channel: 'demo', commit: 'nogit', dirty: false, built_at: '2026-10-12T08:30:00Z', release: '20261012-083000-nogit' }
    const out = fillBootVersion(html, b)
    expect(out).toContain('<div class="boot-version">V0.2.0-demo · nogit · 2026-10-12</div>')
    expect(mask.indexOf('class="boot-version"')).toBeGreaterThan(mask.indexOf('class="boot-badge"'))
    expect(mask.indexOf('class="boot-version"')).toBeLessThan(mask.indexOf('class="boot-progress"'))
  })

  it('five segments in phase order, first child of the track keeps the D2 bar writers working, capacity line present', () => {
    expect([...mask.matchAll(/data-seg="([a-z]+)"/g)].map((m) => m[1])).toEqual(['bundle', 'world', 'points', 'warming', 'reveal'])
    expect(mask).toMatch(/<div class="boot-progress"><i class="boot-track"><i data-seg="bundle">/)
    expect(mask).toMatch(/<div class="boot-capacity" hidden><\/div>/)
    // segment widths follow the weights of AWR-04 §10.1 (0.15, 0.10, 0.40, 0.30, 0.05)
    const grow = Object.fromEntries([...html.matchAll(/\[data-seg="([a-z]+)"\] \{ flex-grow: (\d+); \}/g)].map((m) => [m[1], Number(m[2])]))
    expect(grow).toEqual({ bundle: 15, world: 10, points: 40, warming: 30, reveal: 5 })
  })

  it('mobile and iOS meta (AWR-04 §15.8.7), no hex colour in the critical CSS', () => {
    expect(html).toContain('content="width=device-width, initial-scale=1, viewport-fit=cover"')
    expect(html).toContain('name="apple-mobile-web-app-capable" content="yes"')
    expect(html).toContain('name="apple-mobile-web-app-status-bar-style"')
    expect(html).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
  })
})

describe('bootManifest', () => {
  it('covers the entry chunk, the main chunk and their static imports, not lazy chunks', () => {
    const bundle = {
      'assets/index.js': { type: 'chunk' as const, fileName: 'assets/index.js', isEntry: true, imports: ['assets/react.js'], code: 'a'.repeat(10) },
      'assets/react.js': { type: 'chunk' as const, fileName: 'assets/react.js', imports: [], code: 'b'.repeat(20) },
      'assets/main.js': { type: 'chunk' as const, fileName: 'assets/main.js', facadeModuleId: '/w/src/main.tsx', imports: ['assets/three.js', 'assets/react.js'], code: 'c'.repeat(30) },
      'assets/three.js': { type: 'chunk' as const, fileName: 'assets/three.js', imports: [], code: 'd'.repeat(40) },
      'assets/apidocs.js': { type: 'chunk' as const, fileName: 'assets/apidocs.js', imports: ['assets/react.js'], code: 'e'.repeat(50) },
      'assets/index.css': { type: 'asset' as const, fileName: 'assets/index.css' },
    }
    const m = bootManifest(bundle)
    expect(m.chunks.map((c) => c.href)).toEqual(['/assets/index.js', '/assets/main.js', '/assets/react.js', '/assets/three.js'])
    expect(m.total).toBe(100)
  })
})
