#!/usr/bin/env node
// aggregate-bench (M16-FR-074; M16-AC-042; AWR-18 §11.5): groups the real-GPU reports uploaded through /bench
// (runs/perf-reports/*.json, awr.perf.report.v1 with gate 'bench') by device class and GPU; when a class has >= 3 reports
// from >= 2 distinct devices it prints a freezing candidate table (median and interquartile range of every metric),
// otherwise the class is marked "设计阈值，待固化". Only registered fields are read (13 §13.4: no fingerprinting beyond
// the device class, renderer and adapter).
// Usage: node perf/report/aggregate-bench.mjs [--dir runs/perf-reports] [--json] | --selftest
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { parseArgs } from 'node:util'

const ROOT = resolve(import.meta.dirname, '../../../..')
export const MIN_REPORTS = 3
export const MIN_DEVICES = 2

function unwrap(d) {
  return d?.schema === 'awr.perf.report.v1' ? d : d?.report?.schema === 'awr.perf.report.v1' ? d.report : null
}

export function loadReports(dir) {
  const out = []
  let names = []
  try {
    names = readdirSync(dir)
  } catch {
    return out
  }
  for (const n of names.filter((x) => x.endsWith('.json')).sort()) {
    try {
      const r = unwrap(JSON.parse(readFileSync(join(dir, n), 'utf8')))
      if (r && r.gate === 'bench') out.push(r)
    } catch {
      // unreadable upload
    }
  }
  return out
}

const gpuOf = (r) => String(r.env?.adapter_info?.description ?? r.env?.adapter_info?.device ?? r.env?.renderer ?? 'unknown')
const deviceOf = (r) => [gpuOf(r), r.env?.cpu_model ?? '', r.env?.cores ?? '', (r.env?.canvas_css ?? []).join('x')].join('|')

function q(sorted, p) {
  if (!sorted.length) return null
  const i = (sorted.length - 1) * p
  const lo = Math.floor(i)
  return sorted[lo] + (sorted[Math.min(sorted.length - 1, lo + 1)] - sorted[lo]) * (i - lo)
}

export function aggregate(reports) {
  const groups = new Map()
  for (const r of reports) {
    const k = `${r.env?.device_class ?? 'unknown'} · ${gpuOf(r)}`
    if (!groups.has(k)) groups.set(k, [])
    groups.get(k).push(r)
  }
  const byClass = new Map()
  for (const r of reports) {
    const c = r.env?.device_class ?? 'unknown'
    if (!byClass.has(c)) byClass.set(c, [])
    byClass.get(c).push(r)
  }
  const classes = []
  for (const [cls, rs] of byClass) {
    const devices = new Set(rs.map(deviceOf)).size
    const ready = rs.length >= MIN_REPORTS && devices >= MIN_DEVICES
    const metrics = {}
    if (ready) {
      const vals = new Map()
      for (const r of rs) for (const c of r.cases ?? []) for (const m of c.metrics ?? []) {
        if (typeof m.median !== 'number') continue
        const key = `${c.scene ?? c.id}:${m.key}`
        if (!vals.has(key)) vals.set(key, [])
        vals.get(key).push(m.median)
      }
      for (const [k, v] of vals) {
        const s = [...v].sort((a, b) => a - b)
        metrics[k] = { n: s.length, median: q(s, 0.5), iqr: q(s, 0.75) - q(s, 0.25) }
      }
    }
    classes.push({ device_class: cls, reports: rs.length, devices, status: ready ? '可固化候选' : '设计阈值，待固化', metrics })
  }
  return { groups: [...groups].map(([k, rs]) => ({ group: k, reports: rs.length, devices: new Set(rs.map(deviceOf)).size })), classes }
}

function fakeReport(i, device, cls = 'igpu', p95 = 18) {
  return { schema: 'awr.perf.report.v1', run_id: `b20260929-0000${i}0-abc${i}`, gate: 'bench', kind: 'bench', git: { sha: 'x' },
    build: { mode: 'production', contracts: '1.0.0' }, env: { cpu_model: device, cores: 8, device_class: cls, backend_tier: 'B',
      renderer: `ANGLE (${device})`, canvas_css: [1280, 720] },
    cases: [{ id: 'bench.flight60', ac_ids: ['PERF-AC-066'], scene: 'pc', priority: 'P1', layer: 'ext', status: 'PASS',
      metrics: [{ key: 'frame_p95_ms', unit: 'ms', runs: [p95], median: p95, threshold: null, status: 'NA' }], load: { pre: [], max: [], mean: [] } }],
    summary: { p0_pass: 0, p0_total: 0, p1_pass: 1, p1_total: 1 } }
}

/** M16-AC-042: three simulated uploads from one device do not qualify; a fourth from another device does */
export function selftest() {
  const dir = mkdtempSync(join(tmpdir(), 'awr-bench-agg-'))
  try {
    for (let i = 1; i <= 3; i++) writeFileSync(join(dir, `r${i}.json`), JSON.stringify(fakeReport(i, 'device-a', 'igpu', 17 + i)))
    const a = aggregate(loadReports(dir))
    const ig = a.classes.find((c) => c.device_class === 'igpu')
    if (!ig || ig.reports !== 3 || ig.status !== '设计阈值，待固化') throw new Error(`one device must not qualify: ${JSON.stringify(ig)}`)
    writeFileSync(join(dir, 'r4.json'), JSON.stringify(fakeReport(4, 'device-b', 'igpu', 20)))
    writeFileSync(join(dir, 'r5.json'), JSON.stringify(fakeReport(5, 'device-c', 'dgpu', 8)))
    const b = aggregate(loadReports(dir))
    const ig2 = b.classes.find((c) => c.device_class === 'igpu')
    if (ig2.status !== '可固化候选' || Math.abs(ig2.metrics['pc:frame_p95_ms'].median - 19.5) > 1e-9) throw new Error(`candidate expected: ${JSON.stringify(ig2)}`)
    if (b.classes.find((c) => c.device_class === 'dgpu').status !== '设计阈值，待固化') throw new Error('dgpu with one report must not qualify')
    return true
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values } = parseArgs({ options: { dir: { type: 'string' }, json: { type: 'boolean' }, selftest: { type: 'boolean' } } })
  if (values.selftest) {
    selftest()
    console.log('aggregate-bench selftest: OK')
    process.exit(0)
  }
  const a = aggregate(loadReports(values.dir ?? join(ROOT, 'runs', 'perf-reports')))
  if (values.json) console.log(JSON.stringify(a, null, 1))
  else for (const c of a.classes) console.log(`${c.device_class.padEnd(10)} reports ${c.reports} devices ${c.devices}: ${c.status}`)
}
