// Report generation and compliance (M16-AC-030, 031, 042; PERF-AC-054): result.json -> report.json (Ajv, awr.perf.report.v1)
// -> degraded report.html (--plain, table.log) -> static compliance checks; /bench aggregation self-test.
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { aggregate, selftest } from '../../perf/report/aggregate-bench.mjs'
import { buildReport } from '../../perf/report/build-report.mjs'
import { MAX_BYTES, htmlText, staticErrors } from '../../perf/report/check-report.mjs'
import { footerLines, render } from '../../perf/report/render.mjs'

const dir = mkdtempSync(join(tmpdir(), 'awr-m16-report-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

function result(id: string, status: string, priority: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { schema: 'awr.perf.result.v1', gate: 'G2d', source: 'live', status, errors: [], extra: {},
    fingerprint: { cpu_model: 'test cpu', cores: 8, chrome: 'Chrome 151', flags: ['--use-angle=swiftshader'], node: 'v22', python: '3.12' },
    case: { id, kind: 'pw', params: { city: 'shenzhen', scene: 'pc' }, acIds: ['D1-AC-03a'], priority, layer: 'core', owner: 'M16', world: 'shenzhen' },
    runs: [1, 2, 3].map((i) => ({ index: i, load: { pre: 1, max: 2, mean: 1.5 } })),
    metrics: [{ key: 'frame.p95_ms', report_key: 'frame_p95_ms', unit: 'ms', runs: [45, 48, 49], median: 48, dispersion: 0.08,
      threshold: { op: '<=', value: 50 }, gating: true, status: status === 'FAIL' ? 'FAIL' : 'PASS' },
    { key: 'toast_count', report_key: null, unit: 'count', runs: [1], median: 1, threshold: null, gating: false, status: 'NA' }], ...extra }
}

describe('report pipeline', () => {
  it('builds a schema-valid report and a compliant plain HTML', async () => {
    const run = join(dir, 'p20260929-030000-abcdef1')
    for (const [id, st, pr] of [['flight60.shenzhen.pc', 'PASS', 'P0'], ['latency', 'FAIL', 'P0'], ['soak', 'PASS', 'P1']] as const) {
      mkdirSync(join(run, id), { recursive: true })
      writeFileSync(join(run, id, 'result.json'), JSON.stringify(result(id, st, pr)))
    }
    writeFileSync(join(run, 'manifest.json'), JSON.stringify({ run_id: 'p20260929-030000-abcdef1', gate: 'G2d', git: { sha: 'abcdef1234', branch: 'main', dirty: false } }))
    const rep = (await buildReport(run)) as { gate: string; summary: { p0_pass: number; p0_total: number }; cases: { metrics: { key: string }[] }[] }
    expect(rep.gate).toBe('G2')
    expect(rep.summary).toMatchObject({ p0_pass: 1, p0_total: 2 })
    expect(rep.cases.flatMap((c) => c.metrics.map((m) => m.key))).not.toContain('toast_count')
    const file = await render(run, { plain: true })
    const html = readFileSync(file, 'utf8')
    expect(staticErrors(rep, html, statSync(file).size)).toEqual([])
    expect(statSync(file).size).toBeLessThan(MAX_BYTES)
    expect(htmlText(html)).toContain('流畅性测试报告')
    expect(html).toContain('var(--border)')                           // colours come from the theme tokens inlined at run time
  })
  it('flags emoji, unregistered keys, missing footer and non self-contained pages', () => {
    const rep = { gate: 'G9', cases: [{ id: 'x', metrics: [{ key: 'toast_count' }] }] }
    const errs = staticErrors(rep, `<html><body><script src="x.js"></script>ok \u{2705}</body></html>`, 10)
    expect(errs.join(' | ')).toMatch(/gate G9/)
    expect(errs.join(' | ')).toMatch(/unregistered metric key/)
    expect(errs.join(' | ')).toMatch(/EMOJI-01/)
    expect(errs.join(' | ')).toMatch(/self-contained/)
    expect(errs.join(' | ')).toMatch(/data-report-footer/)
  })
  it('footer follows the fixed format of M16 §6.3', () => {
    const lines = footerLines({ run_id: 'p20260929-031502-3f7a9c2', env: { device_class: 'software' }, cases: [{ world_id: 'shenzhen' }] },
      { datasets: { shenzhen: { name: 'UrbanScene3D', version: 'virtual_cities-sampled (GitHub Release v0.0.1)',
        citation: 'Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022', anchor: 'synthetic' } },
      fidelity: { backend: 'Mock L1（PX4-lite）', vehicle: 'p600_mid360', vehicle_status: '参数未辨识' } })
    expect(lines[0]).toBe('数据：UrbanScene3D（Lin et al., ECCV 2022）虚拟城市采样点云 v0.0.1，科研用途；许可摘要见 world.json。')
    expect(lines[1]).toContain('坐标：示意锚点（synthetic），不得用于真实导航。')
    expect(lines[2]).toBe('PERF · SHENZHEN · WEBGL2 SOFTWARE · run p20260928-031502-3f7a9c2'.replace('0928', '0929'))
  })
})

describe('/bench aggregation (M16-AC-042)', () => {
  it('needs >= 3 reports from >= 2 devices per class', () => {
    expect(selftest()).toBe(true)
    expect(aggregate([]).classes).toEqual([])
  })
})
