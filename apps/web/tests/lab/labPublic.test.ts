// /lab/ 公开只读构建与演示构建剔除（扩展点 15；AWR-05 §7.9、ADR-129；AWR-14 §21.2.3、§21.14.2；D3-AC-01、D3-AC-21）。
// 构建本身（VITE_AWR_LAB_ONLY=1 产物扫描、D2 演示构建逐字节对拍）由 tests/d3/deploy/test_scan_public.py 与
// `make lab-build`、`make demo-parity` 执行；这里检查纯函数：静态制品索引的解析与只读请求约束、路由、文案分片与
// vite.config.ts 的构建期剔除规则。
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  envSwitch, ENGINE_ACTORS_RE, LAB_BRAND_FILES, LAB_INTERNAL_RE, LAB_PUBLIC_FORBIDDEN_RE, LAB_PUBLIC_SRC_RE, LAB_TAILWIND_EXCLUDE, stripEngineActors,
  stripLabGlobs, stripLabKeys,
} from '../../vite.config.ts'
import { LAB_ALLOWED_PREFIXES, LAB_BASE, LAB_STATIC_INDEX } from '@/app/lab-public/flags'
import { composeRoutes, matchLabRoute, matchPattern, type LabPublicRoute } from '@/app/lab-public/router'
import {
  findSample, isAllowedLabUrl, isSafeRelPath, LabStaticError, labGet, loadStaticIndex, parseStaticIndex, sampleFiles, sampleRuns,
} from '@/app/lab-public/staticIndex'
import { LAB_DICTS } from '@/app/lab-public/i18n'

const WEB = resolve(import.meta.dirname, '../..')
const MB = 1024 * 1024

/** M27 §7.8 的示例索引加 AWR-17 §18.8.5 的 files_bytes_total、features 与制品 bytes */
function sampleIndex(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 'drone4d.lab_static_index.v1',
    version: '2026.11.03-1',
    built_unix_ns: '1793000000000000000',
    license: { files: ['LICENSE', 'NOTICE'], attribution: ['Mitko @bg3d（CC-BY-4.0）', 'OpenStreetMap contributors（ODbL-1.0）'] },
    worlds: [{ id: 'oryahovo-crop1', pointcloud: 'worlds/oryahovo-crop1/pc/', bytes: 0 }],
    events: [{
      id: 'abc-crossing-001', version: 1, world_id: 'oryahovo-crop1', tape: 'events/abc-crossing-001/tape.bin', tape_json: 'events/abc-crossing-001/tape.json',
      runs: [{
        run_id: 'run-abcdefghij', policy: 'track_share', n_uav: 3, obs_level: 'L2', metrics: 'runs/run-abcdefghij/metrics.json',
        videos: [{ uav: 'uav-01', cam: 'cam0', path: 'runs/run-abcdefghij/video/uav-01_cam0.mp4', bytes: 12 * MB, sha256: 'a'.repeat(64) },
          { uav: 'uav-01', cam: 'thermal0', path: 'runs/run-abcdefghij/video/uav-01_thermal0.mp4', bytes: 65 * MB }],
        slices: [{ event_id: 'ev-encounter', t_from_s: 85.0, t_to_s: 95.0, rrd: 'runs/run-abcdefghij/slices/ev-encounter.rrd', bytes: 40 * MB }],
        basemap: 'events/abc-crossing-001/basemap.rrd', lidar_compare: 'runs/run-abcdefghij/compare/',
      }],
    }],
    files_sha256: 'SHA256SUMS',
    files_bytes_total: 117 * MB,
    features: { inspect: true, compare: false, leaderboard: false, submit: false },
    ...over,
  }
}

function code(fn: () => unknown): string {
  try {
    fn()
  } catch (e) {
    return e instanceof LabStaticError ? e.code : `other:${String(e)}`
  }
  return 'none'
}

describe('static artifact index (lab_static_index.v1)', () => {
  it('parses the index and builds versioned URLs', () => {
    const idx = parseStaticIndex(sampleIndex())
    expect(idx.version).toBe('2026.11.03-1')
    expect(idx.url('runs/run-abcdefghij/video/uav-01_cam0.mp4')).toBe('/lab-static/v/2026.11.03-1/runs/run-abcdefghij/video/uav-01_cam0.mp4')
    expect(idx.features).toEqual({ inspect: true, compare: false })
    expect(idx.filesBytesTotal).toBe(117 * MB)
    const runs = sampleRuns(idx)
    expect(runs.map((r) => [r.runId, r.eventId, r.worldId, r.nUav, r.obsLevel])).toEqual([['run-abcdefghij', 'abc-crossing-001', 'oryahovo-crop1', 3, 'L2']])
    expect(findSample(idx, 'run-abcdefghij')?.policy).toBe('track_share')
    expect(findSample(idx, 'run-zzzzzzzzzz')).toBeNull()
  })

  it('marks files above 64 MB as not hosted on the site (AWR-14 §21.14.2 rule 2)', () => {
    const files = sampleFiles(parseStaticIndex(sampleIndex()).events[0].runs[0])
    expect(files.map((f) => [f.kind, f.hosted])).toEqual([['video', true], ['video', false], ['slice', true], ['basemap', true]])
    expect(files[0].sha256).toBe('a'.repeat(64))
  })

  it('rejects a wrong schema, version or run id', () => {
    expect(code(() => parseStaticIndex(sampleIndex({ schema: 'drone4d.lab_static_index.v2' })))).toBe('static.schema')
    expect(code(() => parseStaticIndex(null))).toBe('static.schema')
    expect(code(() => parseStaticIndex(sampleIndex({ version: 'current' })))).toBe('static.version')
    const ev = sampleIndex().events as { runs: { run_id: string }[] }[]
    ev[0].runs[0].run_id = 'run-1'
    expect(code(() => parseStaticIndex(sampleIndex({ events: ev })))).toBe('static.schema')
  })

  it('rejects paths that leave the version directory', () => {
    for (const p of ['../index.json', '/etc/passwd', 'http://example.org/x', 'runs/../../x', 'a\\b', './x', '']) {
      expect(isSafeRelPath(p), p).toBe(false)
      const ev = sampleIndex().events as Record<string, unknown>[]
      ev[0].tape = p
      expect(code(() => parseStaticIndex(sampleIndex({ events: ev }))), p).toBe('static.path')
    }
    expect(isSafeRelPath('worlds/oryahovo-crop1/pc/')).toBe(true)
    expect(code(() => parseStaticIndex(sampleIndex()).url('../x'))).toBe('static.path')
  })

  it('refuses an index that offers a leaderboard or submissions (R-D3-46, D3-AC-21)', () => {
    expect(code(() => parseStaticIndex(sampleIndex({ features: { leaderboard: true } })))).toBe('static.feature')
    expect(code(() => parseStaticIndex(sampleIndex({ features: { submit: true } })))).toBe('static.feature')
  })
})

describe('read-only requests (AWR-14 §21.14.2 rule 1, UX-NFR-045)', () => {
  it('allows only same-origin GET under /lab/ and /lab-static/', async () => {
    expect(LAB_ALLOWED_PREFIXES).toEqual(['/lab/', '/lab-static/'])
    for (const u of ['/lab-static/index.json', '/lab-static/v/2026.11.03-1/a.mp4', '/lab/brand/avatar-96.png']) expect(isAllowedLabUrl(u), u).toBe(true)
    for (const u of ['/api/bench/v1/runs', '/api/sandbox/v1/capacity', '//evil.example/lab/', 'https://drone4d.agentnetwork.org.cn/lab-static/index.json',
      '/lab-static/../api/x', '/lab', '/worlds/synthcity/world.json']) expect(isAllowedLabUrl(u), u).toBe(false)
    const seen: [string, RequestInit | undefined][] = []
    const fake = (url: string, init?: RequestInit): Promise<Response> => {
      seen.push([url, init])
      return Promise.resolve(new Response('{}'))
    }
    await labGet('/lab-static/index.json', {}, fake)
    expect(seen[0][1]?.method).toBe('GET')
    expect(seen[0][1]?.credentials).toBe('same-origin')
    await expect(labGet('/api/bench/v1/session', {}, fake)).rejects.toMatchObject({ code: 'static.request' })
    await expect(labGet('/lab-static/index.json', { method: 'POST' } as RequestInit, fake)).rejects.toMatchObject({ code: 'static.request' })
    expect(seen).toHaveLength(1)
  })

  it('reads the index with no-cache and reports HTTP errors', async () => {
    const calls: [string, RequestInit | undefined][] = []
    const ok = (url: string, init?: RequestInit): Promise<Response> => {
      calls.push([url, init])
      return Promise.resolve(new Response(JSON.stringify(sampleIndex()), { headers: { 'content-type': 'application/json' } }))
    }
    const idx = await loadStaticIndex(ok)
    expect(idx.version).toBe('2026.11.03-1')
    expect(calls).toEqual([[LAB_STATIC_INDEX, expect.objectContaining({ cache: 'no-cache', method: 'GET' })]])
    await expect(loadStaticIndex(() => Promise.resolve(new Response('', { status: 404 })))).rejects.toMatchObject({ code: 'static.http', detail: '404' })
    await expect(loadStaticIndex(() => Promise.resolve(new Response('<html>')))).rejects.toMatchObject({ code: 'static.json' })
    await expect(loadStaticIndex(() => Promise.reject(new TypeError('network')))).rejects.toMatchObject({ code: 'static.http' })
  })
})

describe('routes of the public build', () => {
  const r = (id: string, pattern: string): LabPublicRoute => ({ id, pattern, title: () => id, render: () => null })
  it('matches patterns with parameters', () => {
    expect(matchPattern('/lab/samples/:sid', '/lab/samples/run-abcdefghij')).toEqual({ sid: 'run-abcdefghij' })
    expect(matchPattern('/lab', '/lab/')).toEqual({})
    expect(matchPattern('/lab/samples', '/lab/samples/')).toEqual({})
    expect(matchPattern('/lab/samples/:sid', '/lab/samples')).toBeNull()
    expect(matchPattern('/lab/samples/:sid', '/lab/samples/%E0%A4%A')).toBeNull()
  })
  it('lets an extension route override a built-in route with the same pattern', () => {
    const routes = composeRoutes([r('labSampleRun', '/lab/samples/:sid')], [r('labPublicHome', '/lab'), r('labPublicSample', '/lab/samples/:sid')])
    expect(matchLabRoute(routes, '/lab/samples/run-abcdefghij')?.route.id).toBe('labSampleRun')
    expect(matchLabRoute(routes, '/lab/')?.route.id).toBe('labPublicHome')
    expect(matchLabRoute(routes, '/lab/leaderboard')).toBeNull()
  })
  it('refuses routes outside /lab or without the lab id prefix', () => {
    expect(() => composeRoutes([r('samples', '/lab/samples')], [])).toThrow(/lab prefix/)
    expect(() => composeRoutes([r('labX', '/leaderboard')], [])).toThrow(/outside/)
    expect(() => composeRoutes([r('labX', '/lab/x'), r('labX', '/lab/y')], [])).toThrow(/twice/)
  })
})

/** files under dir (recursive) */
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
}

describe('copy and sources of the public build (D3-AC-21)', () => {
  it('has the same lab.public.* keys in both languages', () => {
    const zh = Object.keys(LAB_DICTS['zh-CN']).sort()
    expect(Object.keys(LAB_DICTS.en).sort()).toEqual(zh)
    expect(zh.every((k) => k.startsWith('lab.public.'))).toBe(true)
  })
  it('never names a leaderboard, submission, board, registry or bench path, nor a score', () => {
    const forbidden = ['/leaderboard', '/submissions', '/board', '/registry', '/api/', 'new WebSocket', 'EventSource(']
    const srcs = walk(resolve(WEB, 'src/app/lab-public')).filter((p) => /\.(tsx?|json|html)$/.test(p))
    expect(srcs.length).toBeGreaterThan(5)
    for (const p of srcs) {
      const text = readFileSync(p, 'utf8')
      for (const s of forbidden) expect(text.includes(s), `${p}: ${s}`).toBe(false)
      expect(/from '@\/(net|app\/router|app\/routes|app\/demo|ui\/lab)\b/.test(text), p).toBe(false)
    }
    const words = Object.values(LAB_DICTS['zh-CN']).join('\n') + Object.values(LAB_DICTS.en).join('\n')
    expect(/得分[^。]*显示|主得分|排行榜|提交入口/.test(words.replace('页面不显示得分', ''))).toBe(false)
    expect(/leaderboard|main score|submission/i.test(Object.values(LAB_DICTS.en).join('\n').replace('no scores are shown', ''))).toBe(false)
  })
})

describe('extension point 15 build switches (vite.config.ts)', () => {
  it('accepts only 0 and 1 (default 0)', () => {
    expect(envSwitch('X', undefined)).toBe(false)
    expect(envSwitch('X', '')).toBe(false)
    expect(envSwitch('X', '0')).toBe(false)
    expect(envSwitch('X', '1')).toBe(true)
    expect(() => envSwitch('X', 'true')).toThrow(/expected 0 or 1/)
  })

  it('keeps the D3 internal workspace out of the demo build', () => {
    for (const f of ['src/ui/lab/LabBoardPage.tsx', 'src/ui/lab/wall/WallTile.tsx', 'src/app/routes/labBoard.tsx', 'src/app/routes/lab/worlds.route.ts',
      'src/app/i18n/lab/worlds.zh-CN.json', 'src/stores/lab/playhead.ts', 'src/net/bench.ts']) expect(LAB_INTERNAL_RE.test(`/w/apps/web/${f}`), f).toBe(true)
    for (const f of ['src/ui/layout/AppShell.tsx', 'src/app/routes/world.tsx', 'src/net/api.ts', 'src/app/i18n/zh-CN.json', 'src/ui/lf/LfTable.tsx']) {
      expect(LAB_INTERNAL_RE.test(`/w/apps/web/${f}`), f).toBe(false)
    }
    expect(ENGINE_ACTORS_RE.test('/w/apps/web/src/engine/actors/tape/decode.ts')).toBe(true)
    expect(LAB_PUBLIC_SRC_RE.test('/w/apps/web/src/app/lab-public/App.tsx')).toBe(true)
    expect(LAB_PUBLIC_SRC_RE.test('/w/apps/web/src/app/lab-public/flags.ts')).toBe(false)
  })

  it('keeps write entries, API clients and the application out of the /lab/ build (AWR-14 §21.2.3 rule 1)', () => {
    for (const f of ['src/net/bench.ts', 'src/net/rt/client.ts', 'src/net/api.ts', 'src/app/routes/labBoard.tsx', 'src/app/router/table.ts', 'src/app/demo/Landing.tsx',
      'src/main.tsx', 'src/stores/identity.ts', 'src/ui/lab/LabSubmissionsPage.tsx', 'src/ui/lab/LabLogin.tsx', 'src/ui/lab/board.ts', 'src/ui/lab/submissions.ts',
      'src/ui/lab/useBenchSession.ts', 'src/ui/lab/editor/EventEditor.tsx', 'src/ui/lab/fleet/Fleet.tsx', 'src/ui/lab/eval/Truth.tsx', 'src/ui/lab/runs/RunsTable.tsx']) {
      expect(LAB_PUBLIC_FORBIDDEN_RE.test(`/w/apps/web/${f}`), f).toBe(true)
    }
    for (const f of ['src/app/lab-public/App.tsx', 'src/ui/components/ui/button.tsx', 'src/ui/lf/LfTable.tsx', 'src/ui/lab/wall/WallTile.tsx', 'src/stores/lab/playhead.ts',
      'src/engine/actors/tape/decode.ts', 'src/app/i18n/plural.ts', 'src/lib/demo.ts']) {
      expect(LAB_PUBLIC_FORBIDDEN_RE.test(`/w/apps/web/${f}`), f).toBe(false)
    }
  })

  it('drops lab.* keys from the application dictionaries without reordering the rest', () => {
    const src = JSON.stringify({ 'hud.title': '', 'lab.board.title': 'x', 'lab.login.submit': 'y', 'layer.lidar': 'LiDAR' }, null, 1)
    expect(JSON.parse(stripLabKeys(src))).toEqual({ 'hud.title': '', 'layer.lidar': 'LiDAR' })
    expect(Object.keys(JSON.parse(stripLabKeys(src)))).toEqual(['hud.title', 'layer.lidar'])
    const zh = readFileSync(resolve(WEB, 'src/app/i18n/zh-CN.json'), 'utf8')
    expect(Object.keys(JSON.parse(stripLabKeys(zh))).some((k) => k.startsWith('lab.'))).toBe(false)
  })

  it('rewrites route globs to exclude lab routes and empties globs of lab directories', () => {
    const table = readFileSync(resolve(WEB, 'src/app/router/table.ts'), 'utf8')
    const out = stripLabGlobs(table)
    expect(out).not.toBeNull()
    expect(out).toContain(`import.meta.glob<{ route: RouteDef }>(['../routes/*.tsx', '!../routes/lab*.tsx', '!../routes/lab/**']`)
    expect(stripLabGlobs(`const m = import.meta.glob('../routes/lab/*.route.ts', { eager: true })`)).toBe(
      `const m = import.meta.glob('../routes/lab/__awr_lab_stripped__/*.none', { eager: true })`)
    expect(stripLabGlobs(`const m = import.meta.glob('./lab/*.json')`)).toContain('__awr_lab_stripped__')
    expect(stripLabGlobs(`const m = import.meta.glob('../shaders/*.glsl')`)).toBeNull()
  })

  it('drops the D3 engine re-exports from the engine facade', () => {
    const facade = readFileSync(resolve(WEB, 'src/engine/index.ts'), 'utf8')
    const out = stripEngineActors(facade)
    if (facade.includes('./actors/')) {
      expect(out).not.toBeNull()
      expect(out).not.toContain('./actors/')
      expect(out?.split('\n').length).toBe(facade.split('\n').length - facade.split('\n').filter((l) => l.includes("from './actors/")).length)
    }
    expect(stripEngineActors("export * from './drones/index'\n")).toBeNull()
  })

  it('excludes the D3 sources from the Tailwind candidate scan of the demo build', () => {
    for (const p of ['src/ui/lab', 'src/app/routes/lab*.tsx', 'src/app/lab-public', 'src/stores/lab', 'src/net/bench.ts']) expect(LAB_TAILWIND_EXCLUDE).toContain(p)
  })

  it('serves only the brand files the /lab/ template references', () => {
    const html = readFileSync(resolve(WEB, 'src/app/lab-public/index.html'), 'utf8')
    const refs = [...html.matchAll(/%BASE_URL%brand\/([\w.-]+)/g)].map((m) => m[1])
    expect(refs.length).toBeGreaterThan(0)
    for (const f of refs) expect(LAB_BRAND_FILES).toContain(f)
    expect(html).toContain('<meta charset="UTF-8" />')
    expect(html).toContain('src="/src/app/lab-public/entry.tsx"')
    expect(LAB_BASE).toBe('/lab/')
  })
})
