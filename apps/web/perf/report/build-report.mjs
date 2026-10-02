#!/usr/bin/env node
// build-report (M16-FR-070; AWR-18 §11.2): runs/perf/<runId>/<case>/result.json (awr.perf.result.v1) -> report.json
// (awr.perf.report.v1, validated with Ajv against packages/contracts/perf/perf-report.schema.json).
//   * metrics[].key are 18 §11.2 registered names only (CaseDef dotted keys mapped, extras dropped);
//   * gate G2d / G2w -> 'G2'; local runs report as 'G2';
//   * source=fake cases are listed with status NA ("合成数据，不参与门禁", M16-AC-020, AC-021);
//   * summary counts P0 and P1 passes and lists regressions and waivers.
// Also writes report.meta.json next to it: data sources (world.json dataset) and the fidelity statement for the footer
// (M16-FR-010, FR-011); the report schema has no place for them.
// Usage: node perf/report/build-report.mjs --run <runId | path> [--gate G3] [--kind suite]
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { parseArgs } from 'node:util'

const ROOT = resolve(import.meta.dirname, '../../../..')
const GATE = { G2d: 'G2', G2w: 'G2', G3: 'G3', G4: 'G4', G5: 'G5', bench: 'bench', local: 'G2' }

export function runPath(run) {
  return run.includes('/') ? resolve(run) : join(process.env.AWR_RUNS_DIR ?? join(ROOT, 'runs'), 'perf', run)
}

export function readResults(dir) {
  const out = []
  for (const n of readdirSync(dir).sort()) {
    const p = join(dir, n, 'result.json')
    if (statSync(join(dir, n)).isDirectory() && existsSync(p)) out.push(JSON.parse(readFileSync(p, 'utf8')))
  }
  return out
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** map one result.json to a report case (18 §11.2) */
export function reportCase(r) {
  const fake = r.source === 'fake'
  const metrics = (r.metrics ?? []).filter((m) => m.report_key).map((m) => ({
    // the schema allows lower-case keys only; 18 §11.2 layer_<id>_ms with the camel-case group id groundSky (ACC-1)
    key: m.report_key.toLowerCase(), unit: m.unit, runs: (m.runs ?? []).map(num), median: num(m.median),
    threshold: m.threshold ? { op: m.threshold.op, value: m.threshold.value } : null,
    status: fake ? 'NA' : m.status, ...(m.baseline !== undefined ? { baseline: num(m.baseline) } : {}),
    ...(m.delta !== undefined ? { delta: num(m.delta) } : {}),
  }))
  const scene = r.case?.params?.scene ?? null
  return {
    id: r.case.id, ac_ids: r.case.acIds ?? [], world_id: r.case.world ?? null, scene: scene === 'pc' || scene === 'full' ? scene : null,
    priority: r.case.priority, layer: r.case.layer, repeats: Math.max(1, (r.runs ?? []).length), status: fake ? 'NA' : r.status,
    metrics, load: { pre: (r.runs ?? []).map((x) => x.load?.pre ?? 0), max: (r.runs ?? []).map((x) => x.load?.max ?? 0),
      mean: (r.runs ?? []).map((x) => x.load?.mean ?? 0) },
    errors: (r.errors ?? []).map((e) => ({ code: String(e.code), message: String(e.message).slice(0, 500) })),
  }
}

export function summary(cases, results = []) {
  const pass = (c) => c.status === 'PASS' || c.status === 'WARN'
  const p0 = cases.filter((c) => c.priority === 'P0' && c.status !== 'NA')
  const p1 = cases.filter((c) => c.priority === 'P1' && c.status !== 'NA')
  const regressions = results.flatMap((r) => (r.metrics ?? []).filter((m) => m.regression === true).map((m) => `${r.case.id}:${m.report_key ?? m.key}`))
  const waivers = cases.filter((c) => c.status === 'WAIVED').flatMap((c) => c.ac_ids.map((a) => `${c.id}:${a}`))
  return { p0_pass: p0.filter(pass).length, p0_total: p0.length, p1_pass: p1.filter(pass).length, p1_total: p1.length,
    p1_pass_rate_pct: p1.length ? Math.round((1000 * p1.filter((c) => pass(c) || c.status === 'WAIVED').length) / p1.length) / 10 : 100,
    regressions, waivers }
}

/** data sources of the worlds involved (world.json dataset, M16-FR-010) and the fidelity statement (M16-FR-011) */
export function footerMeta(results, worldsDir = process.env.AWR_WORLDS_DIR ?? join(ROOT, 'worlds')) {
  const ids = [...new Set(results.map((r) => r.case.world).filter(Boolean))]
  const datasets = {}
  for (const id of ids.length ? ids : ['shenzhen']) {
    const p = join(worldsDir, id, 'world.json')
    if (!existsSync(p)) continue
    const w = JSON.parse(readFileSync(p, 'utf8'))
    const c = JSON.parse(readFileSync(join(worldsDir, id, w.coordinate?.href ?? 'coordinate.json'), 'utf8'))
    datasets[id] = { name: w.dataset?.name, version: w.dataset?.version, citation: w.dataset?.citation, license: w.dataset?.license,
      url: w.dataset?.url, anchor: c.anchor?.kind, north: c.trueNorth?.confidence }
  }
  const fake = results.some((r) => r.source === 'fake')
  return { datasets, fidelity: { backend: 'Mock L1（PX4-lite）', vehicle: 'p600_mid360', vehicle_status: '参数未辨识', simulated: true,
    synthetic_source: fake } }
}

async function validator() {
  const { default: Ajv2020 } = await import('ajv/dist/2020.js')
  const schema = JSON.parse(readFileSync(join(ROOT, 'packages', 'contracts', 'perf', 'perf-report.schema.json'), 'utf8'))
  return new Ajv2020({ strict: false, allErrors: true }).compile(schema)
}

export async function buildReport(dir, { gate, kind = 'suite' } = {}) {
  const results = readResults(dir)
  const man = existsSync(join(dir, 'manifest.json')) ? JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf8')) : {}
  const cases = results.map(reportCase)
  const fp = results[0]?.fingerprint ?? {}
  const snapMeta = findSnapMeta(dir)
  const rid = /^[pb][0-9]{8}-[0-9]{6}-[0-9a-f]{4,12}$/.test(man.run_id ?? '') ? man.run_id : dir.split('/').pop()
  const report = {
    schema: 'awr.perf.report.v1', run_id: rid, gate: GATE[gate ?? man.gate ?? 'local'] ?? 'G2', kind,
    git: { sha: man.git?.sha ?? 'nogit', branch: man.git?.branch ?? 'none', dirty: !!man.git?.dirty },
    build: { id: snapMeta?.build ?? 'unknown', mode: snapMeta?.mode ?? 'production', contracts: contractsVersion() },
    env: { cpu_model: fp.cpu_model ?? 'unknown', cores: fp.cores ?? 0, chrome: fp.chrome ?? 'unknown', flags: fp.flags ?? [],
      node: fp.node ?? process.version, python: fp.python ?? 'unknown', device_class: String(snapMeta?.deviceClass ?? 'software').toLowerCase(),
      backend_tier: snapMeta?.tier ?? 'S', renderer: snapMeta?.renderer ?? 'SwiftShader', dpr: snapMeta?.dpr ?? 1,
      canvas_css: snapMeta?.canvasCss ?? [1280, 720] },
    cases,
    summary: summary(cases, results),
  }
  const v = await validator()
  if (!v(report)) throw new Error(`PERF-E011 report fails awr.perf.report.v1: ${JSON.stringify(v.errors?.slice(0, 3))}`)
  writeFileSync(join(dir, 'report.json'), JSON.stringify(report, null, 1))
  writeFileSync(join(dir, 'report.meta.json'), JSON.stringify(footerMeta(results), null, 1))
  return report
}

function contractsVersion() {
  try {
    return JSON.parse(readFileSync(join(ROOT, 'packages', 'contracts', 'package.json'), 'utf8')).version
  } catch {
    return 'unknown'
  }
}

function findSnapMeta(dir) {
  for (const c of readdirSync(dir)) {
    const d = join(dir, c)
    if (!statSync(d).isDirectory()) continue
    for (const r of readdirSync(d)) {
      const s = join(d, r, 'snapshot.json')
      if (existsSync(s)) {
        try {
          return JSON.parse(readFileSync(s, 'utf8')).meta
        } catch {
          // truncated snapshot
        }
      }
    }
  }
  return null
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values } = parseArgs({ options: { run: { type: 'string' }, gate: { type: 'string' }, kind: { type: 'string', default: 'suite' } } })
  if (!values.run) {
    console.error('usage: build-report.mjs --run <runId|dir>')
    process.exit(2)
  }
  buildReport(runPath(values.run), { gate: values.gate, kind: values.kind }).then((r) => {
    console.log(`report.json: ${r.cases.length} cases, P0 ${r.summary.p0_pass}/${r.summary.p0_total}, P1 ${r.summary.p1_pass}/${r.summary.p1_total}`)
  }, (e) => {
    console.error(e.message ?? e)
    process.exit(1)
  })
}
