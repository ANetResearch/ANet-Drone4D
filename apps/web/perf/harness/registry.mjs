// Case registry (M16-FR-041; M16 §6.7.3; AWR-18 §1.3). Core cases: perf/harness/cases/*.mjs (files starting with '_'
// are helpers); module cases:
// perf/<dir>/cases.mjs exporting CaseDef[] (default export), loaded in directory-name order. Refuses to start
// (PERF-E017, exit 2) on a duplicate id, empty acIds, a missing spec, an unknown threshold key or a metric key that
// cannot be mapped to an 18 §11.2 registered name (unless the metric is flagged `extra: true`).
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { reportKey } from './keys.mjs'

export const PERF_DIR = resolve(import.meta.dirname, '..')
export const WEB_DIR = resolve(PERF_DIR, '..')
export const ROOT = resolve(WEB_DIR, '../..')
const KINDS = new Set(['pw', 'py', 'pytest', 'shell'])
const BUILDS = new Set(['production', 'test', 'profiling', 'none'])
const GATES = new Set(['G2d', 'G2w', 'G3', 'G4'])
const BACKENDS = new Set(['live', 'fake', 'none', 'tool'])

export class RegistryError extends Error {
  constructor(errors) {
    super(`PERF-E017 CASE_REGISTRY_INVALID:\n  ${errors.join('\n  ')}`)
    this.errors = errors
    this.code = 'PERF-E017'
    this.exitCode = 2
  }
}

export function loadThresholds(path = join(PERF_DIR, 'thresholds.json')) {
  return JSON.parse(readFileSync(path, 'utf8'))
}

/** registry sources in load order: harness/cases/*.mjs, then perf/<dir>/cases.mjs by directory name */
export function registryFiles(perfDir = PERF_DIR) {
  const core = join(perfDir, 'harness', 'cases')
  const files = existsSync(core) ? readdirSync(core).filter((f) => f.endsWith('.mjs') && !f.startsWith('_')).sort().map((f) => join(core, f)) : []
  for (const d of readdirSync(perfDir).sort()) {
    const p = join(perfDir, d)
    if (d === 'harness' || !statSync(p).isDirectory()) continue
    const c = join(p, 'cases.mjs')
    if (existsSync(c)) files.push(c)
  }
  return files
}

/** validate CaseDef[]; returns the error list (empty when valid) */
export function validateCases(cases, thresholds, { root = ROOT, webDir = WEB_DIR } = {}) {
  const errs = []
  const seen = new Map()
  const th = thresholds?.entries ?? {}
  for (const c of cases) {
    const where = `${c.id ?? '<no id>'} (${c._file ?? '?'})`
    if (!c.id || typeof c.id !== 'string') errs.push(`${where}: missing id`)
    else if (seen.has(c.id)) errs.push(`${where}: duplicate id (also in ${seen.get(c.id)})`)
    else seen.set(c.id, c._file)
    if (!Array.isArray(c.acIds) || !c.acIds.length) errs.push(`${where}: acIds is empty`)
    if (!KINDS.has(c.kind)) errs.push(`${where}: unknown kind ${c.kind}`)
    if (!BUILDS.has(c.build)) errs.push(`${where}: unknown build ${c.build}`)
    if (!['P0', 'P1', 'P2'].includes(c.priority)) errs.push(`${where}: bad priority ${c.priority}`)
    if (!['core', 'ext'].includes(c.layer)) errs.push(`${where}: bad layer ${c.layer}`)
    if (!Array.isArray(c.gates) || c.gates.some((g) => !GATES.has(g))) errs.push(`${where}: bad gates ${c.gates}`)
    if (!c.backend || !BACKENDS.has(c.backend.kind)) errs.push(`${where}: bad backend ${JSON.stringify(c.backend)}`)
    if (![1, 3].includes(c.runs)) errs.push(`${where}: runs must be 1 or 3`)
    if (c.kind === 'pw') {
      if (!c.spec) errs.push(`${where}: pw case without spec`)
      else {
        const p = c.spec.startsWith('tests/') ? join(root, c.spec) : join(webDir, c.spec)
        if (!existsSync(p)) errs.push(`${where}: spec not found ${c.spec}`)
      }
    } else if (!Array.isArray(c.cmd) || !c.cmd.length) errs.push(`${where}: ${c.kind} case without cmd`)
    for (const m of c.metrics ?? []) {
      if (!m.key) errs.push(`${where}: metric without key`)
      else if (!m.extra && reportKey(m.key) === null) errs.push(`${where}: metric ${m.key} is not an 18 §11.2 registered name`)
      if (m.threshold && !(m.threshold in th)) errs.push(`${where}: threshold ${m.threshold} not in thresholds.json`)
    }
  }
  return errs
}

/** module directories skipped for a local debugging run (AWR_PERF_REGISTRY_SKIP=m14,m15); never honoured by gates */
export function skippedDirs(env = process.env) {
  return new Set(String(env.AWR_PERF_REGISTRY_SKIP ?? '').split(',').map((x) => x.trim()).filter(Boolean))
}

/** load and validate every registry file; throws RegistryError */
export async function loadRegistry({ perfDir = PERF_DIR, thresholds = loadThresholds(join(perfDir, 'thresholds.json')), root = ROOT,
  webDir = resolve(perfDir, '..'), skip = new Set() } = {}) {
  const cases = []
  const errs = []
  for (const f of registryFiles(perfDir)) {
    const dir = f.slice(perfDir.length + 1).split('/')[0]
    if (skip.has(dir)) continue
    let mod
    try {
      mod = await import(pathToFileURL(f).href)
    } catch (e) {
      errs.push(`${f}: cannot import (${e?.message ?? e})`)
      continue
    }
    const list = mod.default ?? mod.cases
    if (!Array.isArray(list)) {
      errs.push(`${f}: default export is not a CaseDef[]`)
      continue
    }
    for (const c of list) cases.push({ ...c, _file: f.slice(perfDir.length + 1) })
  }
  errs.push(...validateCases(cases, thresholds, { root, webDir }))
  if (errs.length) throw new RegistryError(errs)
  return cases
}

/** select cases by exact id, a glob ('flight60.*.pc'), a prefix ('storm') or an npm alias with overrides */
export function selectCases(cases, pattern, o = {}) {
  if (!pattern) return []
  const exact = cases.find((c) => c.id === pattern)
  if (exact) return [exact]
  if (pattern === 'flight60' && (o.city || o.scene)) {
    const id = `flight60.${o.city ?? 'shenzhen'}.${o.source === 'fake' ? 'fake' : (o.scene ?? 'pc')}`
    return cases.filter((c) => c.id === id)
  }
  if (pattern === 'ladder' && o.n) return cases.filter((c) => c.id === `ladder.front.n${o.n}`)
  if (pattern === 'net' && o.net) {
    const nets = String(o.net).split(',')
    return cases.filter((c) => nets.some((w) => c.id === `net.${w}`))
  }
  const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^]*')}(\\.|$)`)
  return cases.filter((c) => re.test(c.id))
}
