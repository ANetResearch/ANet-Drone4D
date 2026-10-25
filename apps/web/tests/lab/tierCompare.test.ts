// /lab/compare 快速仿真与高保真仿真对照（D3-AC-35 验证方法列"Playwright lab/compare"；UX-AC-097"两运行共用播放头；规格不一致时提示；
// 差异图表与评测文件一致"；AWR-05 §5.3.6 档间差异报告 `tier_compare.json`）。
// 覆盖：`.npz` 解码与 numpy 逐值对拍（夹具由 fixtures/tier_compare/gen.py 以 `CaseRun.save` 写出：ZIP 存储方式 + force_zip64 本地头，另有
// `np.savez_compressed` 的压缩方式）、CRC-32 与格式拒绝；轨迹构建、两档规格核对（机体集合、样本数、时刻逐位）、位置差与位置 RMS（与
// `compare_case` 同式，对拍 numpy 值）；P09 实测报告（dmax，2026-10-19）的解析、容差行与报告自洽性、采样一致性与 M25 逐字节行；URL 与制品路径；
// 页面以预置查询缓存的静态渲染（用例表、两档轨迹与共用播放头、位置差与报告 RMS 核对、指标差带、指标与接口表、采样一致性、规格不一致/判定
// 不一致/RMS 不一致三类提示、缺少轨迹、角色与缺失状态、英文界面）与路由、文案分片登记。设 AWR_F13_REAL_DIR 为 P09 输出目录（含 9 个用例的
// 两档 `.npz` 与 tier_compare.json）时，另对全部真实用例做轨迹解码与位置 RMS 核对。
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale, t } from '@/app/i18n'
import { route } from '@/app/routes/lab/compare.route'
import { BenchError } from '@/net/bench'
import { crc32, NpzError, parseNpy, readNpz, zipEntries } from '@/ui/lab/compare/npz'
import {
  alignTracks, checkPosRms, decodeTrack, diffRows, fmtMetric, fmtNum, m25Rows, metricParts, metricUnit, parseTierCompare, parseTierCompareSearch, planExtent,
  plotIndices, plotStride, POS_RMS_CHECK_M, posDiff, posRmsOf, REPORT_FILE, resolveCase, resolveUav, sampleAt, samplingRows, STATE_N, tierCompareArtifactPath,
  TierCompareDocError, tightestCase, trackFile, TrackError, trackFromArrays, type TierTrack,
} from '@/ui/lab/compare/tierCompare'
import { tierCompareDocKey, TierComparePage, tierCompareTrackKey } from '@/ui/lab/compare/TierComparePage'

// 静态渲染读取 store 的初始状态（useSyncExternalStore 的服务端快照）：以同一 store 形状、初始播放头 0.148 s 的副本替换 lab 播放头
vi.mock('@/stores/lab/playhead', async () => {
  const { create } = await import('zustand')
  const real = await vi.importActual<typeof import('@/stores/lab/playhead')>('@/stores/lab/playhead')
  return { usePlayhead: create<import('@/stores/lab/playhead').PlayheadState>(() => ({ ...real.usePlayhead.getState(), tS: 0.148, durationS: 0.4 })) }
})

const WEB = join(import.meta.dirname, '..', '..')
const FIX = join(import.meta.dirname, 'fixtures', 'tier_compare')
const RID = 'p09-tier-compare'
interface Expect { n: number; step_ns: number; cases: Record<string, { uavs: string[]; pos_rms_m: Record<string, number>; pos_diff_m: Record<string, Record<string, number>>; row37: Record<string, number[]> }> }
const EXP = JSON.parse(readFileSync(join(FIX, 'expect.json'), 'utf8')) as Expect
const P09 = JSON.parse(readFileSync(join(FIX, 'report.p09.json'), 'utf8')) as Record<string, unknown> & { cases: Record<string, unknown>[] }
const bytes = (f: string): ArrayBuffer => {
  const b = readFileSync(join(FIX, f))
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
}
const track = (c: string, tier: 'fast' | 'realistic') => decodeTrack(bytes(trackFile(c, tier)), tier)
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T

/** P09 报告的副本：指定用例的 pos_rms_m 与 diff.pos_rms_m 换成夹具轨迹的 numpy 值（夹具轨迹与报告同式对拍） */
function reportFor(caseId: string, edit?: (c: Record<string, unknown>) => void): Record<string, unknown> {
  const doc = clone(P09)
  const c = doc.cases.find((x) => x.id === caseId)!
  const rms = EXP.cases[caseId]!.pos_rms_m
  c.pos_rms_m = { ...rms }
  const diff = c.diff as Record<string, number>
  if ('pos_rms_m' in diff) diff.pos_rms_m = Math.max(...Object.values(rms))
  edit?.(c)
  return doc
}

/** 时刻或样本被改动的轨迹副本（规格不一致的输入） */
function altered(tr: TierTrack, how: 'time' | 'samples' | 'uavs'): TierTrack {
  if (how === 'time') {
    const tNs = tr.tNs.slice()
    tNs[50] = tNs[50]! + 1n
    return { ...tr, tNs }
  }
  if (how === 'samples') {
    const n = tr.n - 1
    return { ...tr, n, tNs: tr.tNs.slice(0, n), tS: tr.tS.slice(0, n), state: new Map([...tr.state].map(([u, s]) => [u, s.slice(0, n * STATE_N)])) }
  }
  return { ...tr, state: new Map([...tr.state].map(([u, s]) => [`${u}x`, s])) }
}

describe('.npz decoding (npz.ts) against numpy', () => {
  it('reads np.savez archives written by CaseRun.save (stored, zip64 local headers) value for value', async () => {
    const buf = bytes('hover.fast.npz')
    const es = zipEntries(buf)
    expect(es.map((e) => [e.name, e.method])).toEqual([['t_ns.npy', 0], ['s_u1.npy', 0]])
    const a = await readNpz(buf)
    const t = a.get('t_ns')!
    const s = a.get('s_u1')!
    expect(t.dtype).toBe('i8')
    expect(t.shape).toEqual([EXP.n])
    expect([...(t.data as BigInt64Array)].map(Number)).toEqual(Array.from({ length: EXP.n }, (_, i) => i * EXP.step_ns))
    expect(s.dtype).toBe('f8')
    expect(s.shape).toEqual([EXP.n, 16])
    expect([...(s.data as Float64Array).subarray(37 * 16, 38 * 16)]).toEqual(EXP.cases.hover!.row37.u1)
  })

  it('reads np.savez_compressed (deflate) to the same arrays', async () => {
    const z = await readNpz(bytes('hover.fast.deflate.npz'))
    const s = await readNpz(bytes('hover.fast.npz'))
    expect(zipEntries(bytes('hover.fast.deflate.npz')).every((e) => e.method === 8)).toBe(true)
    expect([...(z.get('s_u1')!.data as Float64Array)]).toEqual([...(s.get('s_u1')!.data as Float64Array)])
    expect([...(z.get('t_ns')!.data as BigInt64Array)]).toEqual([...(s.get('t_ns')!.data as BigInt64Array)])
  })

  it('checks CRC-32 and rejects unsupported .npy layouts', async () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
    const b = new Uint8Array(bytes('hover.fast.npz').slice(0))
    const e = zipEntries(b.buffer).find((x) => x.name === 's_u1.npy')!
    b[e.local + 30 + 8 + 20 + 200] ^= 0xff                       // 本地头 30 字节 + 名称 8 字节 + ZIP64 扩展 20 字节之后的数据区
    await expect(readNpz(b.buffer)).rejects.toMatchObject({ code: 'crc' })
    await expect(readNpz(new ArrayBuffer(64))).rejects.toBeInstanceOf(NpzError)
    const npy = (descr: string, fortran = 'False', shape = '(2,)') => {
      const head = `{'descr': '${descr}', 'fortran_order': ${fortran}, 'shape': ${shape}, }`.padEnd(118, ' ') + '\n'
      const out = new Uint8Array(10 + head.length + 16)
      out.set([0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59, 1, 0])
      new DataView(out.buffer).setUint16(8, head.length, true)
      out.set(new TextEncoder().encode(head), 10)
      new DataView(out.buffer).setFloat64(10 + head.length, 1.5, true)
      return out
    }
    expect([...(parseNpy(npy('<f8')).data as Float64Array)]).toEqual([1.5, 0])
    expect(() => parseNpy(npy('>f8'))).toThrow(NpzError)
    expect(() => parseNpy(npy('<f8', 'True'))).toThrow(/Fortran/)
    expect(() => parseNpy(npy('<f8', 'False', '(3,)'))).toThrow(/past the end/)
  })
})

describe('trajectories and the pose comparison (tierCompare.ts)', () => {
  it('builds both tiers, checks the grid and recomputes the position RMS exactly as compare_case does', async () => {
    for (const [cid, ex] of Object.entries(EXP.cases)) {
      const [a, b] = await Promise.all([track(cid, 'fast'), track(cid, 'realistic')])
      expect([...a.state.keys()]).toEqual(ex.uavs)
      expect(a.tS[1]).toBe(0.004)
      expect(alignTracks(a, b)).toEqual({ ok: true, reason: null, n: EXP.n, uavs: ex.uavs })
      for (const u of ex.uavs) {
        expect(Math.abs(posRmsOf(a, b, u)! - ex.pos_rms_m[u]!)).toBeLessThanOrEqual(1e-15)
        const d = posDiff(a, b, u)
        for (const [i, v] of Object.entries(ex.pos_diff_m[u]!)) expect(d[Number(i)]).toBeCloseTo(v, 14)
      }
    }
  })

  it('reports grid mismatches: vehicle set, sample count and bitwise times', async () => {
    const [a, b] = await Promise.all([track('hover', 'fast'), track('hover', 'realistic')])
    expect(alignTracks(a, altered(b, 'time'))).toMatchObject({ ok: false, reason: 'time' })
    expect(alignTracks(a, altered(b, 'samples'))).toMatchObject({ ok: false, reason: 'samples' })
    expect(alignTracks(a, altered(b, 'uavs'))).toMatchObject({ ok: false, reason: 'uavs' })
    expect(posRmsOf(a, altered(b, 'samples'), 'u1')).toBeNull()
    expect(() => trackFromArrays(new Map(), 'fast')).toThrow(TrackError)
    const arr = await readNpz(bytes('hover.fast.npz'))
    const s = arr.get('s_u1')!
    expect(() => trackFromArrays(new Map([['t_ns', arr.get('t_ns')!], ['s_u1', { ...s, shape: [EXP.n, 8] }]]), 'fast')).toThrow(/float64/)
  })

  it('checks the chart against the report position RMS within 1e-9 m', async () => {
    const [a, b] = await Promise.all([track('contact_crash', 'fast'), track('contact_crash', 'realistic')])
    const ok = resolveCase(parseTierCompare(reportFor('contact_crash')), 'contact_crash')
    const r = checkPosRms(ok, a, b)
    expect(r.ok).toBe(true)
    expect(r.rows.map((x) => x.uav)).toEqual(['wall', 'drop'])
    expect(Math.max(...r.rows.map((x) => x.absDiff!))).toBeLessThanOrEqual(POS_RMS_CHECK_M)
    expect(r.diffOk).toBeNull()                                     // 该用例的容差项不含 pos_rms_m
    const off = resolveCase(parseTierCompare(reportFor('contact_crash', (c) => { (c.pos_rms_m as Record<string, number>).drop! += 2e-9 })), 'contact_crash')
    expect(checkPosRms(off, a, b).ok).toBe(false)
    const [ha, hb] = await Promise.all([track('hover', 'fast'), track('hover', 'realistic')])
    const hv = resolveCase(parseTierCompare(reportFor('hover', (c) => { (c.diff as Record<string, number>).pos_rms_m = 0.5 })), 'hover')
    expect(checkPosRms(hv, ha, hb)).toMatchObject({ ok: false, diffOk: false })
  })

  it('decimates deterministically, finds the playhead sample and frames the plan view', () => {
    expect(plotStride(11251)).toBe(8)
    const ix = plotIndices(11251)
    expect(ix[0]).toBe(0)
    expect(ix[ix.length - 1]).toBe(11250)
    expect(ix.length).toBeLessThanOrEqual(1500 + 1)
    const tS = Float64Array.from({ length: 5 }, (_, i) => i * 0.004)
    expect([sampleAt(tS, -1), sampleAt(tS, 0.0079), sampleAt(tS, 0.008), sampleAt(tS, 9)]).toEqual([0, 1, 2, 4])
    const s = new Float64Array(2 * STATE_N)
    s.set([3, 4, 5], 0)
    s.set([3, 4.5, 6], STATE_N)
    const ex = planExtent([s])
    expect(ex.e[1] - ex.e[0]).toBeCloseTo(2, 12)                    // 最小跨度 2 m
    expect((ex.n[0] + ex.n[1]) / 2).toBeCloseTo(4.25, 12)
  })
})

describe('tier_compare.json (P09 report, dmax 2026-10-19)', () => {
  it('parses all nine cases with their tolerance rows, consistent with diff ≤ tol', () => {
    const doc = parseTierCompare(P09)
    expect(doc.cases.map((c) => c.id)).toEqual(['takeoff_land', 'hover', 'velocity_step', 'velocity_limit', 'yaw_step', 'waypoints', 'orbit', 'contact_crash', 'wind_hover'])
    expect(doc.summary).toEqual({ cases: 9, interfacePass: 9, tolPass: 9, pass: 9 })
    expect(doc.pass).toBe(true)
    expect(doc.vehicle).toBe('x500')
    expect(doc.clockProfile).toEqual({ name: 'd1_250', mainHz: 250, physicsHz: 250 })
    expect(doc.windMode).toBe('field')
    expect(doc.toleranceFrozen).toBe(false)
    for (const c of doc.cases) {
      expect(c.consistent, c.id).toBe(true)
      expect(c.rows.every((r) => r.recomputed && r.consistent)).toBe(true)
      expect(c.iface.pass).toBe(true)
    }
    const orbit = resolveCase(doc, 'orbit')
    expect(orbit.rows.map((r) => [r.key, r.tol])).toEqual([['pos_rms_m', 1.5], ['radius_rms_m', 0.5], ['arrival_s_per_100m', 1.0]])
    expect(orbit.rows[0]!.diff).toBe(1.1100690169152925)
    expect(resolveCase(doc, 'velocity_limit').iface.limitCap).toEqual({ capMps: 12.3, fastMaxMps: 11.86883387176771, realisticMaxMps: 11.004134701288969, pass: true })
    expect(resolveCase(doc, 'waypoints').metrics.fast.arrival_s).toEqual([9.884, 19.908, 29.98, 39.836])
    expect(resolveCase(doc, 'contact_crash').uavs).toEqual(['wall', 'drop'])
    expect(tightestCase(doc)?.id).toBe('velocity_limit')                      // 0.855 m/s / 1.0 m/s
    expect(resolveCase(doc, 'nope').id).toBe('takeoff_land')
    expect(resolveUav(resolveCase(doc, 'contact_crash'), 'drop')).toBe('drop')
    expect(resolveUav(resolveCase(doc, 'contact_crash'), 'u9')).toBe('wall')
  })

  it('lists the sampling consistency and M25 bitwise checks with independent threshold checks', () => {
    const doc = parseTierCompare(P09)
    const rows = samplingRows(doc.sampling)
    const by = (k: string) => rows.find((r) => r.key === k)!
    expect(rows.every((r) => r.ok !== false)).toBe(true)
    expect(by('pose.grid')).toMatchObject({ value: 7501, ok: true })
    expect(by('pose.pos')).toMatchObject({ uav: 'u1', value: 2.8367051235766993e-7, limit: 1e-6, ok: true })
    expect(by('lidar.point')).toMatchObject({ limit: 0.001, ok: true })
    expect(by('lidar.frames').value).toBe('300 / 300')
    expect(by('lidar.points').value).toBe(4203660)
    expect(by('imu.acc')).toMatchObject({ value: 0, limit: 1e-5, ok: true })
    const m = m25Rows(doc.m25)
    expect(m.map((r) => r.uav)).toEqual(['eo', 'lidar0', 'lidar0_imu', 'pose_history'])
    expect(m.every((r) => r.ok)).toBe(true)
    // 篡改：位置差超过阈值或两次摘要不同时判定不通过（不只取报告的 pass）
    const bad = clone(P09) as unknown as { sampling_consistency: { pose_history: { uavs: { u1: { pos_max_m: number } } } }; m25_bytewise: { runs: { sha256: Record<string, string> }[] } }
    bad.sampling_consistency.pose_history.uavs.u1.pos_max_m = 2e-6
    bad.m25_bytewise.runs[1]!.sha256.eo = 'f'.repeat(64)
    const d2 = parseTierCompare(bad)
    expect(samplingRows(d2.sampling).find((r) => r.key === 'pose.pos')!.ok).toBe(false)
    expect(m25Rows(d2.m25).find((r) => r.uav === 'eo')!.ok).toBe(false)
  })

  it('flags a within verdict that disagrees with diff ≤ tol, and rejects malformed reports', () => {
    const rows = diffRows({ a_m: 0.6, b_s: null }, { a_m: 0.5, b_s: 1, c: 'x' }, { a_m: true, b_s: false })
    expect(rows.map((r) => [r.key, r.recomputed, r.consistent])).toEqual([['a_m', false, false], ['b_s', false, true]])
    const doc = parseTierCompare(reportFor('hover', (c) => { (c.within as Record<string, boolean>).drift_max_m = false }))
    expect(resolveCase(doc, 'hover').consistent).toBe(false)
    expect(() => parseTierCompare({ ...P09, schema: 'drone4d.phys.phys_compare.v1' })).toThrow(TierCompareDocError)
    expect(() => parseTierCompare({ ...P09, cases: [] })).toThrow(/no cases/)
    expect(() => parseTierCompare({ ...P09, cases: [{ ...P09.cases[0], id: '../x' }] })).toThrow(TierCompareDocError)
    expect(() => parseTierCompare({ ...P09, cases: [P09.cases[0], P09.cases[0]] })).toThrow(/duplicate/)
    expect(parseTierCompare({ ...P09, tolerance_status: 'frozen (ADR-208)' }).toleranceFrozen).toBe(true)
  })

  it('parses the URL, builds BN-54 artifact paths and formats metrics with their units', () => {
    expect(parseTierCompareSearch(new URLSearchParams('run=p09-x&case=orbit&uav=u1&t=12.5'))).toEqual({ run: 'p09-x', caseId: 'orbit', uav: 'u1', t: 12.5 })
    expect(parseTierCompareSearch(new URLSearchParams('b=run-k2m4q7xz3a&case=../x&t=-1'))).toEqual({ run: 'run-k2m4q7xz3a', caseId: null, uav: null, t: null })
    expect(parseTierCompareSearch(new URLSearchParams('run=../etc'))).toMatchObject({ run: null })
    expect(tierCompareArtifactPath('run x', REPORT_FILE)).toBe('/runs/run%20x/artifacts/tier_compare/tier_compare.json')
    expect(trackFile('orbit', 'realistic')).toBe('orbit.realistic.npz')
    expect(['pos_rms_m', 'arrival_s_per_100m', 'v_steady_mps', 'yaw_rate_end_rps', 'overshoot_pct', 'tilt_deg', 'wall.t_crash_s', 'crash_kind'].map(metricUnit))
      .toEqual(['m', 's/100 m', 'm/s', 'rad/s', '%', '°', 's', ''])
    expect(metricParts('drop.crash_pos_m')).toEqual({ prefix: 'drop', base: 'crash_pos_m' })
    expect([fmtNum(0), fmtNum(2.8367e-7), fmtNum(17.848), fmtNum(0.009542190188), fmtNum(null)]).toEqual(['0', '2.84e-7', '17.85', '0.009542', '—'])
    expect(fmtMetric([30, -39.757, null])).toBe('30, -39.76, —')
    expect(fmtMetric('IMPACT')).toBe('IMPACT')
  })
})

// ---------------------------------------------------------------------------------------------------- 页面
const TRACKS: Record<string, Record<'fast' | 'realistic', TierTrack>> = {}
beforeEach(async () => {
  for (const c of Object.keys(EXP.cases)) {
    if (TRACKS[c]) continue
    TRACKS[c] = { fast: await track(c, 'fast'), realistic: await track(c, 'realistic') }
  }
})

interface ClientOpts { role?: string; doc?: Record<string, unknown>; docError?: Error; caseId?: string; real?: TierTrack; trackError?: Error }
function client(o: ClientOpts = {}): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchInterval: false } } })
  qc.setQueryData(['lab', 'session'], { principal: 'alice', role: o.role ?? 'researcher', projects: ['d3'], via: 'cookie' })
  if (o.docError) {
    qc.getQueryCache().build(qc, { queryKey: tierCompareDocKey(RID) }).setState({ status: 'error', error: o.docError, fetchStatus: 'idle', errorUpdateCount: 1 })
    return qc
  }
  const caseId = o.caseId ?? 'hover'
  qc.setQueryData(tierCompareDocKey(RID), parseTierCompare(o.doc ?? reportFor(caseId)))
  const tr = TRACKS[caseId]
  if (o.trackError) {
    for (const tier of ['fast', 'realistic'] as const) {
      qc.getQueryCache().build(qc, { queryKey: tierCompareTrackKey(RID, caseId, tier) }).setState({ status: 'error', error: o.trackError, fetchStatus: 'idle', errorUpdateCount: 1 })
    }
  } else if (tr) {
    qc.setQueryData(tierCompareTrackKey(RID, caseId, 'fast'), tr.fast)
    qc.setQueryData(tierCompareTrackKey(RID, caseId, 'realistic'), o.real ?? tr.realistic)
  }
  return qc
}
const render = (el: ReactElement, o?: ClientOpts) => renderToStaticMarkup(h(QueryClientProvider, { client: client(o) }, el))
const page = (q: string) => h(TierComparePage, { params: {}, search: new URLSearchParams(q) })
const attrs = (html: string, name: string) => [...html.matchAll(new RegExp(`${name}="([^"]*)"`, 'g'))].map((m) => m[1]!)

describe('/lab/compare page', () => {
  afterEach(() => setLocale('zh-CN'))

  it('shows both trajectories on a shared playhead and checks the difference chart against the report', () => {
    const html = render(page(`run=${RID}&case=hover`))
    expect(html).toContain('data-view="lab-compare"')
    expect(attrs(html, 'data-compare-case')).toEqual(['hover'])
    expect(attrs(html, 'data-compare-align')).toEqual(['ok'])
    expect(html).not.toContain('data-compare-mismatch')
    // 两档轨迹：快速仿真实线、高保真仿真虚线，播放头标记与三张图的竖线在同一样本（0.148 s → 第 37 个样本）
    expect(attrs(html, 'data-track')).toEqual(['realistic', 'fast'])
    expect(attrs(html, 'data-track-points')).toEqual([String(EXP.n), String(EXP.n)])
    expect(html).toMatch(/data-track="realistic"[^>]*|stroke-dasharray="5 3"/)
    expect(attrs(html, 'data-mark')).toEqual(['realistic', 'fast'])
    expect(attrs(html, 'data-chart-playhead')).toEqual(['37', '37'])
    expect(attrs(html, 'data-compare-playhead')).toEqual(['37'])
    expect(Number(attrs(html, 'data-compare-readout')[0])).toBeCloseTo(EXP.cases.hover!.pos_diff_m.u1!['37']!, 14)
    expect(html).toContain(t('lab.compare.readout', { fast: '-2.76, 9.61, 5.37', real: '-2.70, 9.62, 5.36', d: '0.05837' }))
    // 差异图表与报告一致：由轨迹重算的位置 RMS 与 pos_rms_m 之差 ≤ 1e-9 m，参考线为报告值
    expect(attrs(html, 'data-rms-check-ok')).toEqual(['true'])
    expect(Number(attrs(html, 'data-rms-check-max')[0])).toBeLessThanOrEqual(POS_RMS_CHECK_M)
    expect(attrs(html, 'data-chart-ref')).toEqual([String(EXP.cases.hover!.pos_rms_m.u1)])
    expect(attrs(html, 'data-series')).toEqual(['fast', 'realistic', 'diff'])
  })

  it('lists the cases, the tolerance bands, the metric and interface tables and the sampling checks', () => {
    const html = render(page(`run=${RID}&case=orbit`), { caseId: 'orbit', doc: P09 })
    expect(attrs(html, 'data-row-key').slice(0, 9)).toEqual(['takeoff_land', 'hover', 'velocity_step', 'velocity_limit', 'yaw_step', 'waypoints', 'orbit', 'contact_crash', 'wind_hover'])
    expect(attrs(html, 'data-compare-summary')).toEqual(['9/9'])
    expect(attrs(html, 'data-compare-tightest')).toEqual(['velocity_limit'])
    expect(attrs(html, 'data-tolerance-status')).toEqual(['initial'])
    expect(attrs(html, 'data-report-pass')).toEqual(['true'])
    expect(attrs(html, 'data-band-key')).toEqual(['pos_rms_m', 'radius_rms_m', 'arrival_s_per_100m'])
    expect(attrs(html, 'data-band-tol')).toEqual(['1.5', '0.5', '1'])
    expect(attrs(html, 'data-band-within')).toEqual(['true', 'true', 'true'])
    expect(attrs(html, 'data-band-consistent')).toEqual(['true', 'true', 'true'])
    expect(html).toContain(t('lab.compare.case.orbit'))
    expect(html).toContain(t('lab.compare.metric.t_turn_s'))
    expect(html).toContain('27.65 s')                                         // 高保真仿真绕圈时刻
    expect(html).toContain('AIRBORNE')
    const checks = attrs(html, 'data-check-row')
    expect(checks).toEqual(['pose.grid', 'pose.pos', 'pose.att', 'pose.bitwise', 'lidar.point', 'lidar.frames', 'lidar.points', 'imu.acc', 'imu.gyro', 'imu.blocks',
      'm25.stream', 'm25.stream', 'm25.stream', 'm25.stream'])
    expect(attrs(html, 'data-check-ok').filter((v) => v === 'false')).toEqual([])
    // 未缓存该用例的轨迹（查询未执行）：轨迹区显示加载，表格仍由报告给出
    expect(attrs(html, 'data-compare-tracks')).toEqual(['loading'])
    expect(html).not.toContain('data-rms-check-ok="true"')
  })

  it('selects one vehicle of a multi-vehicle case from the URL', () => {
    const html = render(page(`run=${RID}&case=contact_crash&uav=drop`), { caseId: 'contact_crash' })
    expect(attrs(html, 'data-compare-uav')).toEqual(['drop'])
    expect(html).toContain('data-compare-uav-select')
    expect(attrs(html, 'data-chart-ref')).toEqual([String(EXP.cases.contact_crash!.pos_rms_m.drop)])
    expect(attrs(html, 'data-rms-check-ok')).toEqual(['true'])
    expect(attrs(html, 'data-band-key')).toEqual(['t_crash_s', 'crash_pos_m'])
  })

  it('warns when the specs differ, when the report verdicts disagree and when the RMS does not match', () => {
    const tm = render(page(`run=${RID}&case=hover`), { real: altered(TRACKS.hover!.realistic, 'time') })
    expect(attrs(tm, 'data-compare-align')).toEqual(['time'])
    expect(attrs(tm, 'data-compare-mismatch')).toEqual(['time'])
    expect(tm).toContain(t('lab.compare.mismatch'))
    expect(tm).toContain(t('lab.compare.mismatchReason.time'))
    const uv = render(page(`run=${RID}&case=hover`), { real: altered(TRACKS.hover!.realistic, 'uavs') })
    expect(attrs(uv, 'data-compare-mismatch')).toEqual(['uavs'])
    const inc = render(page(`run=${RID}&case=hover`), { doc: reportFor('hover', (c) => { (c.within as Record<string, boolean>).drift_max_m = false }) })
    expect(attrs(inc, 'data-compare-inconsistent')).toEqual(['drift_max_m'])
    expect(attrs(inc, 'data-band-consistent')).toEqual(['true', 'false'])
    const rms = render(page(`run=${RID}&case=hover`), { doc: reportFor('hover', (c) => { (c.pos_rms_m as Record<string, number>).u1! += 1e-6 }) })
    expect(attrs(rms, 'data-rms-check-ok')).toEqual(['false'])
    expect(rms).toContain('data-compare-rms-mismatch')
    // 超出容差的差值：判定为未通过，差带上为红色强调点
    const out = render(page(`run=${RID}&case=hover`), {
      doc: reportFor('hover', (c) => {
        (c.diff as Record<string, number>).drift_max_m = 0.7;
        (c.within as Record<string, boolean>).drift_max_m = false
        c.tol_pass = false
        c.pass = false
      }),
    })
    expect(attrs(out, 'data-band-within')).toEqual(['true', 'false'])
    expect(out).toContain('class="lf-hero"')
    expect(out).toContain(t('lab.compare.caseFail'))
  })

  it('handles missing trajectories, roles, a missing or malformed report and English copy', () => {
    const miss = render(page(`run=${RID}&case=hover`), { trackError: new BenchError(404, 305, 'NOT_FOUND', 'missing') })
    expect(attrs(miss, 'data-compare-tracks')).toEqual(['missing'])
    expect(miss).toContain('data-band-key')
    const inv = render(page(`run=${RID}`), { role: 'invited' })
    expect(inv).toContain(t('lab.compare.roleDenied'))
    expect(inv).not.toContain('data-compare-case')
    expect(render(page(''))).toContain(t('lab.compare.noRun'))
    expect(render(page(`run=${RID}`), { docError: new BenchError(404, 305, 'NOT_FOUND', 'missing') })).toContain(t('lab.compare.missing'))
    expect(render(page(`run=${RID}`), { docError: new TierCompareDocError('schema') })).toContain(t('lab.compare.invalid'))
    expect(render(page(`run=${RID}`), { docError: new BenchError(502, null, null, 'bad gateway') })).toContain(t('lab.compare.unavailable'))
    setLocale('en')
    const en = render(page(`run=${RID}&case=hover`))
    expect(en).toContain('Physics tier comparison (Fast and Gazebo Sim)')
    expect(en).toContain('Hover for 15 s')
    expect(en).toContain('Fast: solid line; Realistic: dashed line')
  })

  it('registers the lab-compare route shard and a paired zh-CN/en text shard under lab.compare.*', () => {
    expect(route).toMatchObject({ id: 'lab-compare', pattern: '/lab/compare', layer: 'overlay' })
    expect(route.component).toBe(TierComparePage)
    const load = (l: string) => JSON.parse(readFileSync(join(WEB, `src/app/i18n/lab/compare.${l}.json`), 'utf8')) as Record<string, string>
    const zh = load('zh-CN')
    const e = load('en')
    expect(Object.keys(e).toSorted()).toEqual(Object.keys(zh).toSorted())
    for (const k of Object.keys(zh)) expect(k.startsWith('lab.compare.'), k).toBe(true)
    for (const v of [...Object.values(zh), ...Object.values(e)]) {
      expect(v).not.toMatch(/[!！~～]|2\.0|Drone4D v2|封板/)
      expect(v.trim()).toBe(v)
    }
    expect(zh['lab.compare.docTitle']).toBe('两档对照 · 事件沙盘 · ANet Drone4D')
    expect(e['lab.compare.docTitle']).toBe('Tier comparison · Event Lab · ANet Drone4D')
    // 每个 P09 用例与容差项都有文案
    for (const c of P09.cases) {
      expect(zh[`lab.compare.case.${String(c.id)}`], String(c.id)).toBeTruthy()
      for (const k of Object.keys(c.tol as object)) expect(zh[`lab.compare.metric.${k}`], k).toBeTruthy()
    }
    for (const f of ['TierComparePage.tsx', 'charts.tsx', 'tierCompare.ts', 'npz.ts']) {
      const src = readFileSync(join(WEB, 'src/ui/lab/compare', f), 'utf8').split('\n').filter((l) => !/^\s*(\/\/|\/\*\*|\*)/.test(l)).join('\n')
      expect(src.replace(/\/\/.*$/gm, ''), f).not.toMatch(/[一-鿿]/)
    }
  })
})

// ---------------------------------------------------------------------------------------------------- 真实 P09 制品（可选）
const REAL = process.env.AWR_F13_REAL_DIR ?? ''
describe.skipIf(!REAL || !existsSync(join(REAL, REPORT_FILE)))('P09 artifacts (AWR_F13_REAL_DIR)', () => {
  it('decodes every case of both tiers and matches pos_rms_m of the report within 1e-9 m', async () => {
    const doc = parseTierCompare(JSON.parse(readFileSync(join(REAL, REPORT_FILE), 'utf8')))
    const read = (f: string) => {
      const b = readFileSync(join(REAL, f))
      return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer
    }
    for (const c of doc.cases) {
      const a = await decodeTrack(read(trackFile(c.id, 'fast')), 'fast')
      const b = await decodeTrack(read(trackFile(c.id, 'realistic')), 'realistic')
      const al = alignTracks(a, b)
      expect(al.ok, c.id).toBe(true)
      expect(al.n, c.id).toBe(c.iface.grid?.samples)
      const r = checkPosRms(c, a, b)
      expect(r.ok, `${c.id} ${JSON.stringify(r.rows)}`).toBe(true)
    }
  }, 60_000)
})
