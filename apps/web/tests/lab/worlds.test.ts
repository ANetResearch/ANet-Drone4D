// D3-AC-51（页面部分）：/lab/worlds、/lab/worlds/:wid、/lab/events、/lab/runs（状态机、取消、重试、原因码显示），以及 lab 路由分片
// （app/routes/lab/*.route.ts）与 lab 文案分片（app/i18n/lab/<feature>.<locale>.json）。页面以预置查询缓存的静态渲染检查；
// 取消与重试请求以替换的 fetch 检查方法、路径、CSRF 头与 problem+json 错误映射。Playwright `lab/roles` 的端到端覆盖由 F09 交付。
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { hasKey, I18N_KEYS, setLocale, t } from '@/app/i18n'
import en from '@/app/i18n/en.json'
import zhCN from '@/app/i18n/zh-CN.json'
import type { RouteDef } from '@/app/router/router'
import { BenchError } from '@/net/bench'
import { cancelAll, requestCancel, requestRetry } from '@/ui/lab/runs/actions'
import { RunDetailBody, runQueryKey } from '@/ui/lab/runs/RunDetail'
import { RunsPage, runsQueryKey } from '@/ui/lab/runs/RunsPage'
import {
  cancellable, filterPatch, hasFilter, hotFailed, parseRunFilter, pollInterval, pruneSelection, reasonLine, restartOf, runPhase, runsQuery,
  RUNS_POLL_MS, type RunDetail, type RunItem,
} from '@/ui/lab/runs/runs'
import { EventDetailBody, EventsPage, eventQueryKey, eventsQuery } from '@/ui/lab/worlds/EventsPage'
import { WorldDetailPage, worldQuery, type WorldDoc } from '@/ui/lab/worlds/WorldDetailPage'
import { WORLDS_QUERY, WorldsPage } from '@/ui/lab/worlds/WorldsPage'
import {
  eventQuery, extentM, fmtFixed, fmtUnixNs, gateOrder, issueText, LAB_ID_RE, levelingDelta, levelingWarn, metricKey, parseEventFilter, parseWorldTab,
  qaMessage, shortHash, type EventDetail, type EventItem, type WorldItem, type WorldSummary,
} from '@/ui/lab/worlds/worlds'

const WEB = join(import.meta.dirname, '..', '..')
const T0 = 1_792_000_000_000_000_000                                         // 2026-10-15 UTC 左右（ns）

// ── 夹具（形状与 python/awr/bench/api/worlds.py、runs.py 的响应一致；世界摘要取自 crop 世界包的真实 world_summary） ──────────

const CROP: WorldSummary = {
  id: 'oryahovo-crop1', name: 'Oryahovo town centre (photogrammetry)', nameZh: '奥里亚霍沃镇中心（摄影测量）', role: null, profile: 'd3-glb',
  contentVersion: 'efad268468de', scaleStatus: 'landmark', metric: { ok: true, rankedOk: true, reason: null, missing: [] },
  registration: { method: 'osm-road-correlation', unitsToMeters: 8.37412, yawDeg: 168.036, leveledDeg: 1.249, rmseM: 1.9,
    leveling: { primary: 'dem', demDeg: 1.1, wallDeg: 1.25, deltaDeg: 0.15, planesMaxSlopeDeg: 0.8 } },
  trueNorth: { yawOffsetDeg: 0, confidence: 'verified', evidence: ['registration osm-road-correlation'] },
  anchor: { latDeg: 43.735251, lonDeg: 23.9612923, hMslM: 115.2, uncertaintyM: { horizontal: 5, vertical: 5 } },
  bounds: { min: [-1255.801, -850.385, -98.482], max: [1255.638, 849.491, 139.475] },
  sensorHints: { texelM: 0.1869, meshEdgeP50M: 1.884, textureLimitedRangeM: { '720p_hfov60': 207.1, '1080p_hfov84': 199.2 }, bakedSunAzElDeg: null, bakedSunConfidence: 'unknown' },
  quality: { coverageFrac: 0.962, nodataFrac: 0.038, holeFrac: 0.004, stretchedFrac: 0.011, multilayerFrac: 0.002 },
  layers: [{ id: 'pointcloud.visual', type: 'pointcloud', format: 'potree2/anet-q16@1', bytes: 60_027_852 }, { id: 'terrain.dtm', type: 'terrain', format: 'f32-grid@1', bytes: null }],
  anchorsCount: { auto: 12, review: 3, confirmed: 40 },
  license: { dataset: 'CC-BY-4.0', citation: 'Mitko (https://sketchfab.com/bg3d), CC-BY-4.0',
    notice: 'Modified from the original: metric calibration, levelling and derived assets.',
    derivedFrom: [{ name: 'OpenStreetMap extract', version: '2026-09', license: 'ODbL-1.0', use: 'registration and anchors', attribution: 'OpenStreetMap contributors' }] },
  qa: { status: 'pass', messages: ['V-GLB-06 snap distance warning', { level: 'W', code: 'V-GLB-15', message: 'levelling within tolerance' }] },
  build: { createdAt: '2026-10-17T15:27:09+00:00', generatorVersion: '0.1.0', profile: 'd3-glb' },
}

const world = (id: string, o: Partial<WorldItem> = {}): WorldItem => ({
  world_id: id, name: `World ${id}`, name_zh: null, role: null, profile: 'd3-glb', content_version: 'efad268468de', scale_status: 'landmark',
  metric_ok: true, ranked_ok: true, metric_reason: null, bounds_m: { xmin: -1255.801, xmax: 1255.638, ymin: -850.385, ymax: 849.491 },
  layers_count: 10, license_id: 'CC-BY-4.0', qa_status: 'pass', events_count: 2, ...o,
})

const reason = (code: number, name: string, message: string, remedy: string) => ({ code, name, message, remedy, detail: null })

const ev = (id: string, o: Partial<EventItem> = {}): EventItem => ({
  event_id: id, version: 2, split: 'dev', status: 'compiled', strict: true, name: `event ${id}`, event_type: 'rendezvous_handoff',
  world: { id: 'oryahovo-crop1', content_version: 'cv-1' }, duration_s: 360, n_scripted: 2, n_background: 40,
  tape_content_sha256: 'c'.repeat(64), reason: null, updated_unix_ns: T0, versions: 2, ...o,
})

const EVENT_DETAIL: EventDetail = {
  ...ev('def-gate', { status: 'infeasible', version: 1, reason: reason(612, 'EVENT_INFEASIBLE', '门 2 不可行', '采纳建议时刻或调整锚点'), tape_content_sha256: null }),
  gates: [
    { gate: 1, status: 'pass', errors: [], warnings: [], duration_ms: 3 },
    { gate: 2, status: 'fail', errors: [{ code: 'E-INFEASIBLE', reason: 612, path: '/actors/0/route/1', message_zh: '路段不可达', message_en: 'Segment infeasible' }], warnings: [] },
  ],
  segments: [{ actor: 'person_A', len_m: 120, avail_s: 90, need_mps: 1.3333 }], retime: { mode: 'minimal' }, tape: null, truth_summary: null, provenance: null,
  published: null, artifacts: [{ name: 'compile_report.json', bytes: 2048 }], latest_version: 1, versions: [{ version: 1, status: 'infeasible' }],
}

function run(id: string, state: string, o: Partial<RunItem> = {}): RunItem {
  const terminal = ['COMPLETED', 'FAILED', 'CANCELLED'].includes(state)
  return {
    run_id: id, event_id: 'crop-handoff-001', state, has_frames: false, created_unix_ns: T0, kind: 'bench', world_id: 'oryahovo-crop1',
    policy: 'track_share', physics: 'realistic', ranked: false, evaluation: null,
    managed: true, terminal, attempt: 1, restart_count: 0, queue_position: null, blocked_by: [], reason: null, cancel: null,
    actions: { cancel: state === 'QUEUED' || state === 'PREPARING' || state === 'RUNNING', retry: state === 'FAILED' || state === 'CANCELLED' },
    experiment_id: 'xp-1', tenant: 'd3', priority_class: 'research', split: 'dev', submitted_by: 'alice',
    queued_unix_ns: T0, started_unix_ns: terminal || state !== 'QUEUED' ? T0 + 1e9 : null, ended_unix_ns: terminal ? T0 + 60e9 : null, updated_unix_ns: T0 + 60e9,
    ...o,
  }
}

const RUNS: RunItem[] = [
  run('run-aaaaaaaaaa', 'QUEUED', { queue_position: 3, blocked_by: ['gpu'] }),
  run('run-bbbbbbbbbb', 'PREPARING', { restart_count: 1 }),
  run('run-cccccccccc', 'RUNNING', { cancel: { requested_unix_ns: T0, by: 'alice', deadline_unix_ns: T0 + 10e9 }, actions: { cancel: false, retry: false } }),
  run('run-dddddddddd', 'FAILED', { ended_unix_ns: T0 + 50e9, reason: reason(687, 'RECORDING_INTEGRITY_FAILED', '录制完整性校验未通过', '重试运行') }),
  run('run-eeeeeeeeee', 'FAILED', { ended_unix_ns: T0 + 90e9, attempt: 2, reason: reason(665, 'AGENT_CRASHED', 'Agent 退出、握手超时或连接中断超出宽限', '检查 Agent 日志') }),
  run('run-ffffffffff', 'CANCELLED'),
  run('run-gggggggggg', 'COMPLETED', { evaluation: { components: { ec: 0.8, eu: 0.5, ao: 0.4, ce: 0.1 }, ranked_valid: true, invalid_reasons: [] } }),
  { ...run('cap-0001', 'SUCCEEDED'), managed: false, terminal: true, attempt: null, kind: 'capture', has_frames: true, actions: { cancel: false, retry: false } },
]

const RUN_DETAIL: RunDetail = {
  ...run('run-eeeeeeeeee', 'FAILED', { attempt: 2, reason: reason(665, 'AGENT_CRASHED', 'Agent 退出、握手超时或连接中断超出宽限', '检查 Agent 日志') }),
  seed_commit: 'f'.repeat(64),
  attempts: [
    { attempt: 1, state: 'CANCELLED', restart_count: 0, dir: '.attempts/1', started_unix_ns: T0, ended_unix_ns: T0 + 5e9, reason: null },
    { attempt: 2, state: 'FAILED', restart_count: 2, dir: null, started_unix_ns: T0 + 6e9, ended_unix_ns: T0 + 90e9, reason: reason(665, 'AGENT_CRASHED', 'Agent 退出', '检查 Agent 日志') },
  ],
  transitions: [
    { attempt: 1, from: null, to: 'QUEUED', reason_code: null, actor: 'alice', t_unix_ns: T0 },
    { attempt: 1, from: 'QUEUED', to: 'CANCELLED', reason_code: null, actor: 'alice', t_unix_ns: T0 + 5e9 },
    { attempt: 2, from: 'PREPARING', to: 'FAILED', reason_code: 665, actor: 'run-controller', t_unix_ns: T0 + 90e9 },
  ],
}

function client(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchInterval: false } } })
  qc.setQueryData(['lab', 'session'], { principal: 'alice', role: 'researcher', projects: ['d3'], via: 'cookie' })
  qc.setQueryData(WORLDS_QUERY, { items: [world('oryahovo-crop1', { name_zh: '奥里亚霍沃镇中心', role: 'event-main' }),
    world('crop-relative', { scale_status: 'relative', metric_ok: false, ranked_ok: false, metric_reason: 'scaleStatus relative', events_count: 0 })] })
  const doc: WorldDoc = { world_id: 'oryahovo-crop1', summary: CROP, events: [ev('abc-crossing'), ev('def-gate', { status: 'infeasible', reason: EVENT_DETAIL.reason })] }
  qc.setQueryData(worldQuery('oryahovo-crop1'), doc)
  const warn: WorldDoc = { world_id: 'tilted', events: [], summary: { ...CROP, id: 'tilted', scaleStatus: 'assumed',
    registration: { ...CROP.registration!, method: 'manual', leveling: { ...CROP.registration!.leveling, demDeg: 0.4, wallDeg: 1.2, deltaDeg: 0.8 } } } }
  qc.setQueryData(worldQuery('tilted'), warn)
  qc.setQueryData(eventsQuery(parseEventFilter(new URLSearchParams())), { items: [ev('abc-crossing'), EVENT_DETAIL, ev('ghi-test', { split: 'test', status: 'published' })].map((e) => ({ ...e, versions: 1 })), total: 3 })
  qc.setQueryData(eventQueryKey('def-gate', null), EVENT_DETAIL)
  qc.setQueryData(runsQueryKey(runsQuery(parseRunFilter(new URLSearchParams()))), { items: RUNS, next_cursor: 'eyJvIjogMTAwfQ', total: 108 })
  qc.setQueryData(runQueryKey('run-eeeeeeeeee'), RUN_DETAIL)
  qc.setQueryData(runQueryKey('cap-0001'), { ...RUNS[7] })
  return qc
}

const render = (el: ReactElement) => renderToStaticMarkup(h(QueryClientProvider, { client: client() }, el))
const page = (C: (p: { params: Record<string, string>; search: URLSearchParams }) => ReactElement | null, params: Record<string, string> = {}, q = '') =>
  render(h(C, { params, search: new URLSearchParams(q) }))
const text = (html: string) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ')
const headers = (html: string) => [...html.matchAll(/<th[^>]*>(.*?)<\/th>/g)].map((x) => x[1].replace(/<[^>]+>/g, ''))
/** `data-row-key="<k>"` 所在行的 HTML */
function rowHtml(html: string, key: string): string {
  const i = html.indexOf(`data-row-key="${key}"`)
  expect(i, key).toBeGreaterThan(0)
  return html.slice(i, html.indexOf('</tr>', i))
}

afterEach(() => setLocale('zh-CN'))

// ── 路由分片 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('lab route shards (app/routes/lab/*.route.ts)', () => {
  const shards = import.meta.glob<{ route: RouteDef }>('../../src/app/routes/lab/*.route.ts', { eager: true })

  it('each shard exports one lab-prefixed route with a unique id and pattern under /lab', () => {
    const routes = Object.values(shards).map((m) => m.route)
    expect(routes.map((r) => r.id).toSorted()).toEqual(expect.arrayContaining(['lab-events', 'lab-runs', 'lab-world', 'lab-worlds']))
    expect(new Set(routes.map((r) => r.id)).size).toBe(routes.length)
    expect(new Set(routes.map((r) => r.pattern)).size).toBe(routes.length)
    for (const r of routes) {
      expect(r.id.startsWith('lab-'), r.id).toBe(true)
      expect(r.pattern.startsWith('/lab/'), r.pattern).toBe(true)
      expect(r.layer).toBe('overlay')
      expect(r.component, r.id).toBeTypeOf('function')
    }
  })

  it('the superseded app/routes/labRuns.tsx is gone, so /lab/runs has exactly one definition', () => {
    const flat = readdirSync(join(WEB, 'src/app/routes')).filter((f) => /^lab.*\.tsx$/.test(f))
    expect(flat).not.toContain('labRuns.tsx')
    const flatPatterns = flat.map((f) => /pattern: '([^']+)'/.exec(readFileSync(join(WEB, 'src/app/routes', f), 'utf8'))?.[1])
    for (const m of Object.values(shards)) expect(flatPatterns).not.toContain(m.route.pattern)
  })

  describe('registration', () => {
    beforeEach(() => vi.resetModules())
    afterEach(() => vi.unstubAllEnvs())

    // 路由表 eager 导入全部页面模块，首次导入较慢
    it('stays out of the table until delivered (ext routes, D2 builds unchanged)', { timeout: 120_000 }, async () => {
      vi.stubEnv('VITE_AWR_EXT_ROUTES', '')
      const { installRoutes } = await import('@/app/router/table')
      const { matchPath } = await import('@/app/router/router')
      const ids = installRoutes().map((r) => r.id)
      expect(ids.filter((id) => id.startsWith('lab-'))).toEqual([])
      expect(matchPath('/lab/worlds')).toBeNull()
    })

    it('registers every shard when the `lab` group is delivered, and single ids otherwise', { timeout: 120_000 }, async () => {
      vi.stubEnv('VITE_AWR_EXT_ROUTES', 'lab,lab-login')
      const { installRoutes } = await import('@/app/router/table')
      const { matchPath } = await import('@/app/router/router')
      const ids = installRoutes().map((r) => r.id)
      for (const id of ['lab-worlds', 'lab-world', 'lab-events', 'lab-runs', 'lab-login']) expect(ids, id).toContain(id)
      expect(ids).not.toContain('lab-board')                                  // 旧式 lab*.tsx 路由仍按 id 交付
      expect(matchPath('/lab/worlds')?.route.id).toBe('lab-worlds')
      expect(matchPath('/lab/worlds/oryahovo-crop1')).toMatchObject({ route: { id: 'lab-world' }, params: { wid: 'oryahovo-crop1' } })
      expect(matchPath('/lab/events', '?world=x&eid=abc')?.route.id).toBe('lab-events')
      expect(matchPath('/lab/runs', '?state=FAILED')?.route.id).toBe('lab-runs')
      vi.resetModules()
      vi.stubEnv('VITE_AWR_EXT_ROUTES', 'lab-runs')
      const t2 = await import('@/app/router/table')
      const ids2 = t2.installRoutes().map((r) => r.id)
      expect(ids2).toContain('lab-runs')
      expect(ids2).not.toContain('lab-worlds')
    })
  })
})

// ── 文案分片 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('lab i18n shards (app/i18n/lab/<feature>.<locale>.json)', () => {
  const dir = join(WEB, 'src/app/i18n/lab')
  const files = readdirSync(dir).filter((f) => f.endsWith('.json'))
  const load = (f: string) => JSON.parse(readFileSync(join(dir, f), 'utf8')) as Record<string, string>
  const features = [...new Set(files.map((f) => f.replace(/\.(zh-CN|en)\.json$/, '')))]

  it('pairs a zh-CN and an en file per feature with the same key set', () => {
    for (const f of files) expect(f, f).toMatch(/^[a-z][a-z0-9-]*\.(zh-CN|en)\.json$/)
    expect(features).toEqual(expect.arrayContaining(['runs', 'worlds']))
    for (const x of features) {
      const zh = Object.keys(load(`${x}.zh-CN.json`)).toSorted()
      const e = Object.keys(load(`${x}.en.json`)).toSorted()
      expect(e, x).toEqual(zh)
    }
  })

  it('keeps every shard key under lab.*, unique across shards and absent from the application dictionaries', () => {
    const seen = new Map<string, string>()
    for (const f of files.filter((x) => x.endsWith('.zh-CN.json'))) {
      for (const k of Object.keys(load(f))) {
        expect(k.startsWith('lab.'), `${f}: ${k}`).toBe(true)
        expect(seen.get(k), `${k} in ${f} and ${seen.get(k)}`).toBeUndefined()
        seen.set(k, f)
        expect(k in zhCN, `${k} also in zh-CN.json`).toBe(false)
        expect(k in en, `${k} also in en.json`).toBe(false)
      }
    }
    expect(seen.size).toBeGreaterThan(250)
  })

  it('merges the shards into the runtime dictionaries of both locales', () => {
    expect(hasKey('lab.worlds.title') && hasKey('lab.runs.state.FAILED') && hasKey('lab.reason.687')).toBe(true)
    expect(I18N_KEYS.zh).toContain('lab.events.gate.2')
    expect(I18N_KEYS.en).toContain('lab.events.gate.2')
    expect(t('lab.runs.state.CANCELLED')).toBe('已取消')
    setLocale('en')
    expect(t('lab.runs.state.CANCELLED')).toBe('Cancelled')
    expect(t('lab.runs.batchTitle', { n: 3 })).toBe('Cancel 3 runs?')
    expect(t('lab.runs.batchTitle', { n: 1 })).toBe('Cancel 1 run?')
  })

  it('uses the §21.15.4 state names and §21.15.5 button texts, with no colloquial or D4-naming slips', () => {
    const zh = { ...load('runs.zh-CN.json'), ...load('worlds.zh-CN.json') }
    const e = { ...load('runs.en.json'), ...load('worlds.en.json') }
    expect(['QUEUED', 'PREPARING', 'RUNNING', 'FINALIZING', 'EVALUATING', 'COMPLETED', 'FAILED', 'CANCELLED'].map((s) => zh[`lab.runs.state.${s}`]))
      .toEqual(['排队中', '准备中', '运行中', '收尾中', '评测中', '已完成', '失败', '已取消'])
    expect(['draft', 'queued', 'compiling', 'compiled', 'infeasible', 'failed', 'published', 'deprecated'].map((s) => zh[`lab.events.status.${s}`]))
      .toEqual(['草稿', '排队中', '编译中', '已编译', '不可行', '编译失败', '已发布', '已停用'])
    expect([zh['lab.runs.op.cancel'], zh['lab.runs.op.retry'], e['lab.runs.op.cancel'], e['lab.runs.op.retry']]).toEqual(['取消运行', '重试运行', 'Cancel run', 'Retry run'])
    expect(zh['lab.runs.batchBody']).toBe('已开始的运行将在 10 s 内终止，已写入的录制保留 14 天。')
    for (const v of [...Object.values(zh), ...Object.values(e)]) {
      expect(v).not.toMatch(/Drone4D 2\.0|跑一下|停掉|一键|开跑|上帝视角|[\u{1F300}-\u{1FAFF}]/u)
    }
    // 英文 sentence case：按钮与列标题不出现逐词大写（专有名词与缩写除外）
    for (const k of ['lab.runs.op.cancel', 'lab.runs.op.retry', 'lab.runs.op.open', 'lab.worlds.col.version', 'lab.events.col.scripted']) expect(e[k], k).toMatch(/^[A-Z][^A-Z]*$/)
  })
})

// ── 纯函数 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

describe('world helpers', () => {
  it('flags a levelling difference above 0.5° (V-GLB-15) and derives it when only both tilts are recorded', () => {
    const base = { primary: 'dem', demDeg: null, wallDeg: null, deltaDeg: null, planesMaxSlopeDeg: null }
    expect(levelingDelta({ ...base, deltaDeg: -0.8 })).toBe(0.8)
    expect(levelingWarn({ ...base, deltaDeg: 0.8 })).toBe(true)
    expect(levelingWarn({ ...base, deltaDeg: 0.5 })).toBe(false)
    expect(levelingDelta({ ...base, demDeg: 1.1, wallDeg: 1.75 })).toBeCloseTo(0.65, 9)
    expect(levelingWarn({ ...base, demDeg: 1.1, wallDeg: 1.75 })).toBe(true)
    expect(levelingDelta(base)).toBeNull()
    expect(levelingWarn(null)).toBe(false)
  })
  it('parses tabs, metric states, extents, ids and QA messages', () => {
    expect(parseWorldTab('calibration')).toBe('calibration')
    expect(parseWorldTab('gaussian')).toBe('overview')
    expect(parseWorldTab(null)).toBe('overview')
    expect([metricKey(true, true), metricKey(true, false), metricKey(false, false)]).toEqual(['ranked', 'metric', 'none'])
    expect(extentM(world('x').bounds_m)?.map((v) => Math.round(v))).toEqual([2511, 1700])
    expect(extentM({ xmin: null, xmax: 1, ymin: 0, ymax: 1 })).toBeNull()
    expect(LAB_ID_RE.test('oryahovo-crop1')).toBe(true)
    expect(LAB_ID_RE.test('../etc')).toBe(false)
    expect(qaMessage({ level: 'W', code: 'V-GLB-15', message: 'ok' })).toBe('W · V-GLB-15 · ok')
    expect(qaMessage(3)).toBe('')
  })
  it('formats UTC times, hashes and signed numbers', () => {
    expect(fmtUnixNs(Date.UTC(2026, 9, 18, 14, 32, 5) * 1e6)).toBe('2026-10-18 14:32:05 UTC')
    expect(fmtUnixNs(null)).toBe('—')
    expect(shortHash('c'.repeat(64))).toBe('cccccccccccc')
    expect(fmtFixed(-1.5, 1)).toBe('−1.5')
    expect(fmtFixed(-2, 1)).toBe('−2.0')
  })
  it('builds event list queries and orders failing gates first', () => {
    const f = parseEventFilter(new URLSearchParams('world=oryahovo-crop1&status=bogus&q=%20cross%20'))
    expect(f).toEqual({ world: 'oryahovo-crop1', status: null, q: ' cross ' })
    expect(eventQuery(f)).toBe('?world=oryahovo-crop1&q=cross')
    expect(eventQuery(parseEventFilter(new URLSearchParams('status=infeasible')))).toBe('?status=infeasible')
    expect(eventQuery(parseEventFilter(new URLSearchParams()))).toBe('')
    expect(gateOrder(EVENT_DETAIL.gates).map((g) => g.gate)).toEqual([2, 1])
    const issue = EVENT_DETAIL.gates[1].errors[0]
    expect(issueText(issue, true)).toBe('E-INFEASIBLE · 路段不可达')
    expect(issueText(issue, false)).toBe('E-INFEASIBLE · Segment infeasible')
  })
})

describe('run helpers (M27 §6.3; AWR-14 §21.6)', () => {
  it('round-trips URL filters into BN-42 queries (xp → experiment) and drops unknown states', () => {
    const f = parseRunFilter(new URLSearchParams('event=crop-handoff-001&xp=xp-1&state=FAILED,bogus,CANCELLED&ranked=true&cursor=abc'))
    expect(f).toEqual({ event: 'crop-handoff-001', xp: 'xp-1', policy: null, state: ['FAILED', 'CANCELLED'], ranked: true, cursor: 'abc' })
    expect(runsQuery(f)).toBe('?event=crop-handoff-001&experiment=xp-1&state=FAILED%2CCANCELLED&ranked=true&cursor=abc&limit=100')
    expect(runsQuery(parseRunFilter(new URLSearchParams()))).toBe('?limit=100')
    expect(hasFilter(f)).toBe(true)
    expect(hasFilter(parseRunFilter(new URLSearchParams('cursor=x')))).toBe(false)
    // 筛选变化回到第一页
    expect(filterPatch({ state: ['RUNNING'] })).toEqual({ state: 'RUNNING', cursor: null })
    expect(filterPatch({ ranked: false, event: null })).toEqual({ ranked: 'false', event: null, cursor: null })
  })
  it('derives the progress phase: queue position, cancellation countdown and overdue', () => {
    expect(runPhase(RUNS[0], T0)).toEqual({ kind: 'queued', position: 3, blocked: ['gpu'] })
    expect(runPhase(RUNS[2], T0 + 3.2e9)).toEqual({ kind: 'cancelling', remainS: 7, overdue: false })
    expect(runPhase(RUNS[2], T0 + 11e9)).toMatchObject({ kind: 'cancelling', overdue: true, remainS: 0 })
    expect(runPhase(RUNS[1], T0)).toEqual({ kind: 'active' })
    expect(runPhase(RUNS[3], T0)).toEqual({ kind: 'done' })
    expect(runPhase(RUNS[7], T0)).toEqual({ kind: 'done' })
    expect(restartOf(RUNS[1])).toBe(1)
    expect(restartOf({ state: 'RUNNING', restart_count: 2 })).toBe(0)
  })
  it('picks the newest FAILED row as the only hot row and keeps selections to cancellable runs', () => {
    expect(hotFailed(RUNS)).toBe('run-eeeeeeeeee')
    expect(hotFailed(RUNS.filter((r) => r.state !== 'FAILED'))).toBeNull()
    expect(cancellable(RUNS).map((r) => r.run_id)).toEqual(['run-aaaaaaaaaa', 'run-bbbbbbbbbb'])
    expect([...pruneSelection(new Set(['run-aaaaaaaaaa', 'run-cccccccccc', 'run-ffffffffff', 'gone']), RUNS)]).toEqual(['run-aaaaaaaaaa'])
  })
  it('polls every 5 s while a run is unfinished', () => {
    expect(pollInterval(RUNS)).toBe(RUNS_POLL_MS)
    expect(RUNS_POLL_MS).toBe(5000)
    expect(pollInterval(RUNS.filter((r) => r.terminal))).toBe(30_000)
    expect(pollInterval(undefined)).toBe(30_000)
  })
  it('prefers the §21.15.6 short text, falls back to the server text (zh) or the reason name (en)', () => {
    const dict = { has: hasKey, t }
    expect(reasonLine(RUNS[3].reason!, true, dict)).toEqual({ code: 687, name: 'RECORDING_INTEGRITY_FAILED', short: '录制完整性校验未通过', remedy: '重试运行' })
    const r665 = RUNS[4].reason!
    expect(reasonLine(r665, true, dict).short).toBe(r665.message)
    expect(reasonLine(r665, false, dict)).toEqual({ code: 665, name: 'AGENT_CRASHED', short: 'AGENT_CRASHED', remedy: '' })
    setLocale('en')
    expect(reasonLine(RUNS[3].reason!, false, dict).short).toBe('Recording integrity verification failed')
  })
})

describe('cancel and retry requests (BN-44, BN-45)', () => {
  const calls: { url: string; init: RequestInit }[] = []
  const reply = (status: number, body?: unknown) => new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  afterEach(() => {
    vi.unstubAllGlobals()
    calls.length = 0
  })
  const stub = (fn: (url: string) => Response) => vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return fn(url)
  }))

  it('sends DELETE and POST with the CSRF header and the session cookie, never a token', async () => {
    stub((u) => (u.endsWith('/retry') ? reply(202, { run_id: 'run-eeeeeeeeee', attempt: 3 }) : reply(204)))
    await requestCancel('run-aaaaaaaaaa')
    expect(await requestRetry('run-eeeeeeeeee')).toBe(3)
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([['DELETE', '/api/bench/v1/runs/run-aaaaaaaaaa'], ['POST', '/api/bench/v1/runs/run-eeeeeeeeee/retry']])
    for (const c of calls) {
      expect(c.init.credentials).toBe('same-origin')
      expect((c.init.headers as Record<string, string>)['X-D3-CSRF']).toBe('1')
      expect(c.url).not.toMatch(/token|d3bt_/)
    }
  })

  it('maps 409 680 and 429 681 to BenchError with the reason code and why', async () => {
    stub((u) => (u.endsWith('/retry') ? reply(429, { code: 681, name: 'RUN_QUOTA_EXCEEDED', message: 'quota', detail: { why: 'queue_full' } })
      : reply(409, { code: 680, name: 'RUN_STATE_CONFLICT', message: 'state', detail: { why: 'terminal', state: 'COMPLETED' } })))
    const e1 = await requestCancel('run-gggggggggg').catch((x: unknown) => x)
    expect(e1).toBeInstanceOf(BenchError)
    expect(e1).toMatchObject({ status: 409, code: 680, reasonName: 'RUN_STATE_CONFLICT', detail: { why: 'terminal' } })
    const e2 = await requestRetry('run-eeeeeeeeee').catch((x: unknown) => x)
    expect(e2).toMatchObject({ status: 429, code: 681 })
    expect(t('lab.runs.rejectedWhy', { id: 'run-gggggggggg', code: 680, name: 'RUN_STATE_CONFLICT', why: 'terminal' }))
      .toBe('运行 run-gggggggggg 的操作被拒绝：680 RUN_STATE_CONFLICT（terminal）')
  })

  it('cancels a selection one run after another and reports rejected ones', async () => {
    const order: string[] = []
    const r = await cancelAll(['a', 'b', 'c'], async (id) => {
      order.push(id)
      if (id === 'b') throw new BenchError(409, 680, 'RUN_STATE_CONFLICT', 'x', { why: 'terminal' })
    })
    expect(order).toEqual(['a', 'b', 'c'])
    expect(r.ok).toEqual(['a', 'c'])
    expect(r.rejected.map((x) => x.id)).toEqual(['b'])
  })
})

// ── 页面（静态渲染） ──────────────────────────────────────────────────────────────────────────────────────────────

describe('/lab/worlds and /lab/worlds/:wid (§21.3.2)', () => {
  it('lists worlds with metric status; the uncalibrated world is the hot cell', () => {
    const html = page(WorldsPage)
    expect(html).toContain('data-figure="lab-worlds-table"')
    expect(headers(html)).toEqual(['世界 id', '名称', '角色', '内容版本', '尺度等级', '米制状态', '范围', '事件', '图层', '许可', 'QA'])
    const crop = rowHtml(html, 'oryahovo-crop1')
    expect(text(crop)).toContain('奥里亚霍沃镇中心')
    expect(text(crop)).toContain('事件主场景')
    expect(text(crop)).toContain('可用于排名运行')
    expect(text(crop)).toContain('2511 × 1700 m')
    const rel = rowHtml(html, 'crop-relative')
    expect(rel).toMatch(/data-hot=""[^>]*>\s*<span data-world-metric="none"/)
    expect(text(rel)).toContain('未完成米制标定')
    expect(html.match(/data-hot=""/g)).toHaveLength(1)
  })

  it('shows the overview tab with extent, area, anchor and the events on the world', () => {
    const html = page(WorldDetailPage, { wid: 'oryahovo-crop1' })
    expect(html).toContain('data-world-tabs="overview"')
    for (const k of ['overview', 'calibration', 'quality', 'layers', 'sensors', 'license']) expect(html).toContain(`data-world-tab="${k}"`)
    const s = text(html)
    expect(s).toContain('2511.4 × 1699.9 m')
    expect(s).toContain('4.27 km²')
    expect(s).toContain('−98.5 至 139.5 m')
    expect(s).toContain('43.735251°, 23.961292° · 海拔 115.2 m · 水平不确定度 5.0 m')
    expect(s).toContain('自动 12 · 待复核 3 · 已确认 40')
    expect(html).toContain('data-figure="lab-world-events"')
    expect(s).toContain('def-gate')
  })

  it('shows calibration numbers with units and conditions, without a warning at 0.15°', () => {
    const html = page(WorldDetailPage, { wid: 'oryahovo-crop1' }, 'tab=calibration')
    const s = text(html)
    expect(s).toContain('8.374 m/单位（OSM 道路配准，残差 1.90 m）')
    expect(s).toContain('0.150°')
    expect(s).toContain('0.00°（置信度：verified）')
    expect(html).not.toContain('data-world-leveling-warn')
    expect(html).not.toContain('data-world-scale-assumed')
  })

  it('warns in red when the two levelling methods differ by more than 0.5°, and explains an assumed scale', () => {
    for (const loc of ['zh-CN', 'en'] as const) {
      setLocale(loc)
      const html = page(WorldDetailPage, { wid: 'tilted' }, 'tab=calibration')
      expect(html).toMatch(/class="[^"]*border-destructive[^"]*"[^>]*data-world-leveling-warn=""/)
      expect(html).toContain('data-icon=""')
      expect(text(html)).toContain(loc === 'zh-CN' ? '调平两法差值 0.80° 超过 0.5°，需要控制点复核。' : 'differ by 0.80°, which exceeds 0.5°. Control-point review is required.')
      expect(html).toContain('data-world-scale-assumed=""')
      expect(html).toMatch(/data-warn=""[^>]*>0\.800°/)
    }
  })

  it('renders quality, layers, sensor hints and license tabs from the summary', () => {
    const q = text(page(WorldDetailPage, { wid: 'oryahovo-crop1' }, 'tab=quality'))
    expect(q).toContain('96.2%')
    expect(q).toContain('W · V-GLB-15 · levelling within tolerance')
    const l = page(WorldDetailPage, { wid: 'oryahovo-crop1' }, 'tab=layers')
    expect(headers(l)).toEqual(['图层 id', '类型', '格式', '体积'])
    expect(text(l)).toContain('potree2/anet-q16@1')
    const s = page(WorldDetailPage, { wid: 'oryahovo-crop1' }, 'tab=sensors')
    expect(text(s)).toContain('纹理受限距离（1080p_hfov84） 199.2 m')
    expect(s).toContain('data-world-sensor-note=""')
    expect(text(s)).toContain('超过纹理受限距离时，背景细节由纹理分辨率决定，与相机分辨率无关。')
    const lic = text(page(WorldDetailPage, { wid: 'oryahovo-crop1' }, 'tab=license'))
    expect(lic).toContain('CC-BY-4.0')
    expect(lic).toContain('Mitko (https://sketchfab.com/bg3d)')
    expect(lic).toContain('OpenStreetMap contributors')
    expect(lic).toContain('ODbL-1.0')
  })
})

describe('/lab/events (§21.4.1)', () => {
  it('lists the latest version per event with status names, mode, counts and short tape hashes', () => {
    const html = page(EventsPage)
    expect(headers(html)).toEqual(['事件 id', '名称', '世界', '最新版本', '状态', '编译模式', '时长 (s)', '脚本演员', '背景演员', '磁带哈希', '更新于'])
    const ok = text(rowHtml(html, 'abc-crossing'))
    expect(ok).toContain('已编译')
    expect(ok).toContain('strict')
    expect(ok).toContain('cccccccccccc')
    expect(ok).toContain('360.0')
    const bad = rowHtml(html, 'def-gate')
    expect(bad).toContain('data-hot=""')
    expect(text(bad)).toContain('不可行 · 612')
    expect(text(rowHtml(html, 'ghi-test'))).toContain('test 划分')
    expect(text(html)).toContain('共 3 个事件')
  })

  it('shows the compilation detail: reason code with short text and remedy, the failing gate first, segments and publication', () => {
    const html = render(h(EventDetailBody, { eid: 'def-gate', v: null }))
    expect(html).toContain('data-reason="612"')
    const s = text(html)
    expect(s).toContain('612 EVENT_INFEASIBLE')
    expect(s).toContain('路线在给定时刻内不可行')
    expect(s).toContain('处理建议：采纳建议时刻或调整锚点')
    expect(html.indexOf('data-gate="2"')).toBeLessThan(html.indexOf('data-gate="1"'))
    expect(html).toMatch(/data-gate="2" data-gate-status="fail" class="[^"]*border-destructive/)
    expect(s).toContain('E-INFEASIBLE · 路段不可达 /actors/0/route/1')
    expect(headers(html)).toEqual(expect.arrayContaining(['演员', '路段长度 (m)', '可用时间 (s)', '所需速度 (m/s)']))
    expect(s).toContain('1.33')
    expect(s).toContain('该版本没有磁带。')
    expect(s).toContain('未发布')
  })
})

describe('/lab/runs (§21.6: state machine, cancel, retry, reason codes)', () => {
  for (const loc of ['zh-CN', 'en'] as const) {
    it(`shows every state with its display name (${loc})`, () => {
      setLocale(loc)
      const html = page(RunsPage)
      const names = loc === 'zh-CN' ? ['排队中', '准备中', '运行中', '失败', '已取消', '已完成'] : ['Queued', 'Preparing', 'Running', 'Failed', 'Cancelled', 'Completed']
      for (const [i, id] of ['run-aaaaaaaaaa', 'run-bbbbbbbbbb', 'run-cccccccccc', 'run-dddddddddd', 'run-ffffffffff', 'run-gggggggggg'].entries()) {
        expect(rowHtml(html, id)).toContain(`>${names[i]}</span>`)
      }
      expect(rowHtml(html, 'cap-0001')).toContain('data-run-unmanaged=""')
    })
  }

  it('renders restarts, queue position, the cancellation deadline and reason codes with remedies', () => {
    const html = page(RunsPage)
    expect(text(rowHtml(html, 'run-bbbbbbbbbb'))).toContain('第 1 次重启')
    expect(text(rowHtml(html, 'run-aaaaaaaaaa'))).toContain('排队第 3 位 · 等待：gpu')
    expect(rowHtml(html, 'run-cccccccccc')).toContain('data-run-phase="cancelling"')
    const failed = rowHtml(html, 'run-dddddddddd')
    expect(failed).toContain('data-run-reason="687"')
    expect(text(failed)).toContain('687 RECORDING_INTEGRITY_FAILED · 录制完整性校验未通过')
    expect(failed).toContain('title="处理建议：重试运行"')
    expect(failed).toMatch(/class="[^"]*border-destructive[^"]*"/)
    expect(text(rowHtml(html, 'run-eeeeeeeeee'))).toContain('#2')
  })

  it('marks the newest FAILED state cell as the only hot cell', () => {
    const html = page(RunsPage)
    expect(html.match(/data-hot=""/g)).toHaveLength(1)
    expect(rowHtml(html, 'run-eeeeeeeeee')).toMatch(/data-hot=""[^>]*><span class="inline-flex[^"]*" data-run-state="FAILED"/)
  })

  it('offers selection only for runs the server lets this user cancel, plus a row menu everywhere', () => {
    const html = page(RunsPage)
    expect([...html.matchAll(/data-run-select="([^"]+)"/g)].map((m) => m[1])).toEqual(['run-aaaaaaaaaa', 'run-bbbbbbbbbb'])
    expect(html.match(/data-run-menu="/g)).toHaveLength(RUNS.length)
    expect(html).toMatch(/disabled=""[^>]*data-runs-batch-cancel="0"|data-runs-batch-cancel="0"[^>]*disabled=""/)
    expect(text(html)).toContain('取消所选运行（0）')
    expect(text(html)).toContain('共 108 个运行')
    expect(text(html)).toContain('下一页')
  })

  it('renders the status sheet with attempts, transitions, reason and actions', () => {
    const html = render(h(RunDetailBody, { rid: 'run-eeeeeeeeee', onAsk: () => undefined }))
    expect(html).toContain('data-figure="lab-run-attempts"')
    expect(html).toContain('data-figure="lab-run-transitions"')
    expect(html).toContain('data-run-detail-retry=""')
    expect(html).not.toContain('data-run-detail-cancel')
    const s = text(html)
    expect(s).toContain('665 AGENT_CRASHED')
    expect(s).toContain('处理建议：检查 Agent 日志')
    expect(s).toContain('ffffffffffff')
    expect(rowHtml(html, '2')).toContain('data-run-reason="665"')
    expect(s).toContain('run-controller')
    const legacy = render(h(RunDetailBody, { rid: 'cap-0001' }))
    expect(legacy).toContain('data-run-unmanaged-note=""')
    expect(legacy).not.toContain('lab-run-attempts')
  })
})
