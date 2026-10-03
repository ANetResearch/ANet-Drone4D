// Playwright fixture of the M16 performance specs (M16 §6.7.1, §9.2; AWR-18 §3.3, §8.5 common rules):
//   perfPage.open / waitReveal / waitBenchDone / eval / saveSnapshot / assertBinding / assertNoPageErrors / writeMetrics.
// The harness passes the case through the environment: AWR_PERF_CASE_PARAMS (JSON), AWR_PERF_RUN_DIR (snapshot
// directory) and AWR_PERF_BASE (api or static server). Run directly (`npx playwright test perf/flight60.spec.ts`) the specs
// use the config baseURL and write to runs/playwright/perf-debug; that mode is for debugging only (AWR-18 §3).
// Waiting polls every 1000 ms and the page is evaluated only at the start and the end of a sampling window
// (M16-NFR-001). pageerror "WebGPU is not available" is ignored (18 §8.5).
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { test as base, expect, type Page } from '@playwright/test'

export const ROOT = resolve(import.meta.dirname, '../../../..')
export const WEB = resolve(import.meta.dirname, '../..')

export interface CaseParams {
  city?: string
  scene?: 'pc' | 'full'
  n?: number
  source?: 'live' | 'fake'
  net?: string
  tier?: string
  fixedB?: number
  perfInject?: string
  quality?: number
  minutes?: number
  blocks?: number
  storm?: string
  grep?: string
  [k: string]: unknown
}

export function caseParams(): CaseParams {
  try {
    return JSON.parse(process.env.AWR_PERF_CASE_PARAMS ?? '{}') as CaseParams
  } catch {
    return {}
  }
}

export function runDir(): string {
  const d = process.env.AWR_PERF_RUN_DIR ?? join(ROOT, 'runs', 'playwright', 'perf-debug')
  mkdirSync(d, { recursive: true })
  return d
}

/** base URL of the system under test: harness backend, otherwise the Playwright webServer */
export function baseUrl(fallback?: string): string {
  const b = process.env.AWR_PERF_BASE ?? fallback ?? `http://127.0.0.1:${4173 + 10 * (Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0)}`
  return b.replace(/\/$/, '')
}

const sha256 = (p: string): string => createHash('sha256').update(readFileSync(p)).digest('hex')
const IGNORED = [/WebGPU is not available/i]

export type AwrPerfHandle = {
  load: { revealAt: number; ttfp: number }
  bench: { done: boolean; flightT: number; mode: string; pairs?: Record<string, { n: number }> }
  snapshot(o?: { rings?: boolean }): Record<string, unknown>
  reset(scope?: string): void
  mark(name: string): void
  inject?: (o: { busyMs?: number; renderBusyMs?: number }) => void
  frame: { count: number }
  gpu: { programs: number; rtAllocs: number }
  meta: { drawingBuffer: [number, number]; mode: string; tier: string; deviceClass: string }
  governor: { step: number; history: { t: number; step: number; dir: number; reason: string }[] }
  cas: { B: number; B_floor: number; index: number }
}
type WinPerf = { __perf?: AwrPerfHandle }

export class PerfPage {
  readonly errors: string[] = []
  readonly consoleErrors: string[] = []
  constructor(readonly page: Page, readonly base: string) {
    page.on('pageerror', (e) => {
      const msg = `${e.name}: ${e.message}`
      if (!IGNORED.some((re) => re.test(msg))) this.errors.push(msg)
    })
    page.on('console', (m) => {
      if (m.type() === 'error' && /GL_|WebGL|context lost/i.test(m.text())) this.consoleErrors.push(m.text())
    })
  }

  async open(path: string): Promise<void> {
    await this.page.goto(`${this.base}${path}`, { waitUntil: 'domcontentloaded' })
  }

  /** __perf.load.revealAt > 0 (boot mask lifted), polled every 1000 ms */
  async waitReveal(timeoutMs = 90_000): Promise<void> {
    await this.page.waitForFunction(() => {
      const p = (window as unknown as WinPerf).__perf
      return !!p && !!p.load && p.load.revealAt > 0
    }, null, { polling: 1000, timeout: timeoutMs })
  }

  /**
   * the PerfGovernor walk after the reveal is over: at least minAfterRevealMs since the reveal and no step in the last
   * quietMs, or capMs since the reveal (FX2-R5, ADR-076: the operation windows of D1-AC-25 measure first-use costs, the
   * governor's steps are judged by D1-AC-03b/04). Returns the ms waited since the reveal.
   */
  async waitGovernorSettled(o: { minAfterRevealMs?: number; quietMs?: number; capMs?: number } = {}): Promise<number> {
    const a = { minAfterRevealMs: o.minAfterRevealMs ?? 3000, quietMs: o.quietMs ?? 6000, capMs: o.capMs ?? 45_000 }
    await this.page.waitForFunction((x) => {
      const p = (window as unknown as WinPerf).__perf
      if (!p || !p.load || !(p.load.revealAt > 0)) return false
      const now = performance.now()
      const h = p.governor?.history ?? []
      const last = h.length ? h[h.length - 1].t : p.load.revealAt
      const since = now - p.load.revealAt
      return (since >= x.minAfterRevealMs && now - last >= x.quietMs) || since >= x.capMs
    }, a, { polling: 250, timeout: a.capMs + 30_000 })
    return this.page.evaluate(() => performance.now() - ((window as unknown as WinPerf).__perf?.load.revealAt ?? 0))
  }

  async waitBenchDone(timeoutMs: number): Promise<void> {
    await this.page.waitForFunction(() => {
      const p = (window as unknown as WinPerf).__perf
      return !!p && !!p.bench && p.bench.done === true
    }, null, { polling: 1000, timeout: timeoutMs })
  }

  eval<T>(fn: (p: AwrPerfHandle) => T): Promise<T> {
    return this.page.evaluate(`(${fn.toString()})(window.__perf)`) as Promise<T>
  }

  /** __perf.snapshot({ rings: true }) -> <dir>/snapshot.json (awr.perf.v1; the harness validates it with Ajv) */
  async saveSnapshot(dir = runDir(), name = 'snapshot.json'): Promise<Record<string, unknown>> {
    const snap = await this.page.evaluate(() => (window as unknown as WinPerf).__perf!.snapshot({ rings: true }))
    writeFileSync(join(dir, name), JSON.stringify(snap))
    return snap
  }

  /** PR-8 / PERF-E009: flight60 json bound to the world coordinate.json and to its .bin (checked on disk before opening) */
  assertBinding(worldId: string): void {
    const wd = join(process.env.AWR_WORLDS_DIR ?? join(ROOT, 'worlds'), worldId)
    if (!existsSync(join(wd, 'world.json'))) throw new Error(`PERF-E009 world ${worldId} not built`)
    const w = JSON.parse(readFileSync(join(wd, 'world.json'), 'utf8')) as { coordinate?: { href?: string } }
    const coord = join(wd, w.coordinate?.href ?? 'coordinate.json')
    for (const dir of [join(WEB, 'dist', 'bench', 'flight60'), join(WEB, 'public', 'bench', 'flight60')]) {
      const j = join(dir, `${worldId}.json`)
      if (!existsSync(j)) continue
      const f = JSON.parse(readFileSync(j, 'utf8')) as { coordinate_sha256: string; bin_sha256: string }
      if (f.coordinate_sha256 !== sha256(coord)) throw new Error(`PERF-E009 ${j}: coordinate_sha256 mismatch`)
      if (f.bin_sha256 !== sha256(join(dir, `${worldId}.bin`))) throw new Error(`PERF-E009 ${j}: bin_sha256 mismatch`)
    }
  }

  assertNoPageErrors(): void {
    expect(this.errors, `PERF-E003 pageerror: ${this.errors.join(' | ')}`).toEqual([])
    expect(this.consoleErrors, `PERF-E004 GL error: ${this.consoleErrors.join(' | ')}`).toEqual([])
  }

  /** merge values into <runDir>/metrics.json (source 'script' metrics of the CaseDef) */
  writeMetrics(values: Record<string, number | null>, dir = runDir()): void {
    const f = join(dir, 'metrics.json')
    const cur = existsSync(f) ? (JSON.parse(readFileSync(f, 'utf8')) as Record<string, unknown>) : {}
    writeFileSync(f, JSON.stringify({ ...cur, ...values }))
  }

  /** max rAF interval over `ms` measured inside the page (warm-up and layout operations, not flight60 sampling) */
  maxGap(ms: number): Promise<number> {
    return this.page.evaluate((dur) => new Promise<number>((ok) => {
      let last = performance.now()
      let worst = 0
      const t0 = last
      const step = (now: number): void => {
        worst = Math.max(worst, now - last)
        last = now
        if (now - t0 < dur) requestAnimationFrame(step)
        else ok(worst)
      }
      requestAnimationFrame(step)
    }), ms)
  }
}

export const test = base.extend<{ perfPage: PerfPage }>({
  perfPage: async ({ page, baseURL }, provide) => {
    await provide(new PerfPage(page, baseUrl(process.env.AWR_PERF_BASE ? undefined : baseURL)))
  },
})
export { expect }

/** percentile (nearest rank, 18 §2.5) over a plain array */
export function pctl(a: number[], p: number): number | null {
  if (!a.length) return null
  const s = [...a].sort((x, y) => x - y)
  return s[Math.min(s.length - 1, Math.floor(p * s.length))]
}

/** steady-window intervals (t in (2, 60] s) of a snapshot */
export function steady(snap: Record<string, unknown>): number[] {
  const f = (snap.frame ?? {}) as { interval?: number[]; t?: number[] }
  const out: number[] = []
  const iv = f.interval ?? []
  const t = f.t ?? []
  for (let i = 0; i < Math.min(iv.length, t.length); i++) if (t[i] > 2 && t[i] <= 60) out.push(iv[i])
  return out
}
