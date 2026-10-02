// Soak (M16-FR-058; D1-AC-29; PERF-AC-045; AWR-18 §7.5, §8.8): soak-shenzhen (S1 + 200 x500 orbiters) for `minutes` (30);
// during it: switch the viewed world 3 times (static browsing), weather preset 5 times, overlay toggles 50 times.
// Every 5 s: JS heap (CDP Performance.getMetrics), api and sim-core RSS (/proc/<pid>/status VmRSS, pids from
// GET /api/sys/procs), the point-cloud GPU pool (__perf pc.residentPts, pc.poolRows) and the page's reconnect counter.
// Judged here (metrics.json, source 'script'): heap_growth_pct and rss_growth_pct are medians of the last `windowMin` (5)
// minutes over the first ones (18 §7.5; rss_growth_pct = the larger of api and sim-core, FX2-R3-gateway: before this the
// harness only had the two end points of its own sampler); unexpected_reconnects sums every page load (a world switch is a
// new page, its counter starts at 0). Reported: per-process RSS growth, GPU pool high-water per window and whether it rises
// monotonically (18 §8.8 "GPU 池高水位不单调上升"), process restarts. Series go to soak-series.json.
// Frame cadence (D1-AC-03b): after the soak the same browser runs one flight60 on the full scene (live fleet) and that
// snapshot is the run's snapshot.json, so the harness extracts the frame metrics from it; the soak page's own snapshot is
// snapshot-soak.json.
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { runFlight60 } from './fixtures/flight'
import { hotkey, palette } from './fixtures/ui'
import { caseParams, expect, pctl, runDir, test, type PerfPage } from './fixtures/perf'

type Sample = { t: number; heap: number; api: number | null; sim: number | null; pool: number | null; rows: number | null }

function rssMb(pid: number | undefined): number | null {
  if (!pid) return null
  try {
    const kb = Number(/VmRSS:\s*(\d+)/.exec(readFileSync(`/proc/${pid}/status`, 'utf8'))?.[1])
    return Number.isFinite(kb) ? kb / 1024 : null
  } catch {
    return null
  }
}

async function viewerToken(base: string): Promise<string | null> {
  try {
    const r = await fetch(`${base}/api/auth/token`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ role: 'viewer', principal_hint: 'MSIXTEENSOAKVIEWERAAAAAA' }),
    })
    return r.ok ? ((await r.json()) as { token: string }).token : null
  } catch {
    return null
  }
}

async function corePids(base: string, token: string | null): Promise<Record<string, number>> {
  if (!token) return {}
  try {
    const r = await fetch(`${base}/api/sys/procs`, { headers: { authorization: `Bearer ${token}` } })
    if (!r.ok) return {}
    const items = ((await r.json()) as { items?: { name: string; pid?: number }[] }).items ?? []
    return Object.fromEntries(items.filter((x) => x.pid && (x.name === 'api' || x.name === 'sim-core')).map((x) => [x.name, x.pid!]))
  } catch {
    return {}
  }
}

/** pc.residentPts, pc.poolRows and net.reconnects of the current page (a few numbers, no snapshot allocation) */
async function pageCounters(pp: PerfPage): Promise<{ pool: number | null; rows: number | null; reconnects: number }> {
  try {
    return await pp.page.evaluate(() => {
      const p = (window as unknown as { __perf?: { pc?: { residentPts?: number; poolRows?: number }; net?: { reconnects?: number } } }).__perf
      return { pool: p?.pc?.residentPts ?? null, rows: p?.pc?.poolRows ?? null, reconnects: p?.net?.reconnects ?? 0 }
    })
  } catch {
    return { pool: null, rows: null, reconnects: 0 }
  }
}

const growth = (first: number[], last: number[]): number | null => {
  const a = pctl(first, 0.5)
  const b = pctl(last, 0.5)
  return a !== null && b !== null && a > 0 ? 100 * (b / a - 1) : null
}

test('soak 30 min', async ({ perfPage }) => {
  const p = caseParams()
  const minutes = Number(p.minutes ?? 30)
  const windowS = 60 * Math.min(Number(p.windowMin ?? 5), minutes / 3)
  test.setTimeout((minutes + 8) * 60_000)
  const page = perfPage.page
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Performance.enable')
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  const token = await viewerToken(perfPage.base)
  let pids = await corePids(perfPage.base, token)
  const pidsAtStart = { ...pids }
  const restarts: { t: number; name: string; from: number; to: number }[] = []
  const series: Sample[] = []
  let reconnectsDone = 0 // reconnects of page loads already left
  let reconnectsCur = 0
  const t0 = Date.now()
  const end = t0 + minutes * 60_000
  const leavePage = async (): Promise<void> => {
    reconnectsDone += (await pageCounters(perfPage)).reconnects
    reconnectsCur = 0
  }
  let k = 0
  while (Date.now() < end) {
    const t = (Date.now() - t0) / 1000
    const m = (await cdp.send('Performance.getMetrics')) as { metrics: { name: string; value: number }[] }
    let api = rssMb(pids.api)
    let sim = rssMb(pids['sim-core'])
    if (api === null || sim === null) {
      // a core process restarted (the harness also declares the run invalid, PERF-E008): follow the new pid
      const now = await corePids(perfPage.base, token)
      for (const name of ['api', 'sim-core']) {
        if (now[name] && pids[name] && now[name] !== pids[name]) restarts.push({ t, name, from: pids[name], to: now[name] })
      }
      pids = { ...pids, ...now }
      api ??= rssMb(pids.api)
      sim ??= rssMb(pids['sim-core'])
    }
    const pc = await pageCounters(perfPage)
    reconnectsCur = pc.reconnects
    series.push({ t, heap: m.metrics.find((x) => x.name === 'JSHeapUsedSize')?.value ?? Number.NaN, api, sim, pool: pc.pool, rows: pc.rows })
    k++
    if (k % 60 === 20 && k < 200) {
      await leavePage()
      await perfPage.open('/world/shanghai')
      await perfPage.waitReveal()
      await page.waitForTimeout(3000)
      await leavePage()
      await perfPage.open('/world/shenzhen')
      await perfPage.waitReveal()
    } else if (k % 36 === 7) {
      await palette(page, '切换天气').catch(() => {})
    } else if (k % 7 === 3) {
      await hotkey(page, 'Control+KeyB')
    }
    await page.waitForTimeout(5000)
  }
  const tEnd = (Date.now() - t0) / 1000
  const first = series.filter((s) => s.t <= windowS)
  const last = series.filter((s) => s.t >= tEnd - windowS)
  const col = (rows: Sample[], key: 'heap' | 'api' | 'sim'): number[] =>
    rows.map((s) => s[key]).filter((v): v is number => v !== null && Number.isFinite(v))
  const heapGrowth = growth(col(first, 'heap'), col(last, 'heap'))
  const apiGrowth = growth(col(first, 'api'), col(last, 'api'))
  const simGrowth = growth(col(first, 'sim'), col(last, 'sim'))
  const rssGrowth = apiGrowth === null && simGrowth === null ? null : Math.max(apiGrowth ?? -Infinity, simGrowth ?? -Infinity)
  // GPU pool high-water per window of windowS seconds (18 §8.8: must not rise monotonically over the soak)
  const hw: number[] = []
  for (let w0 = 0; w0 < tEnd; w0 += windowS) {
    const v = series.filter((s) => s.t >= w0 && s.t < w0 + windowS && s.pool !== null).map((s) => s.pool!)
    if (v.length) hw.push(Math.max(...v))
  }
  const hwMonotonic = hw.length >= 3 && hw.every((v, i) => i === 0 || v > hw[i - 1])
  const hwGrowth = hw.length >= 2 && hw[0] > 0 ? 100 * (hw[hw.length - 1] / hw[0] - 1) : null
  await perfPage.saveSnapshot(undefined, 'snapshot-soak.json')
  await leavePage()
  const reconnects = reconnectsDone + reconnectsCur
  writeFileSync(join(runDir(), 'soak-series.json'), JSON.stringify({
    minutes, window_s: windowS, pids_at_start: pidsAtStart, pids_at_end: pids, restarts, gpu_pool_hw_per_window: hw, series,
  }))
  perfPage.writeMetrics({
    heap_growth_pct: heapGrowth, rss_growth_pct: rssGrowth, rss_api_growth_pct: apiGrowth, rss_sim_growth_pct: simGrowth,
    rss_api_first_mb: pctl(col(first, 'api'), 0.5), rss_api_last_mb: pctl(col(last, 'api'), 0.5),
    rss_sim_first_mb: pctl(col(first, 'sim'), 0.5), rss_sim_last_mb: pctl(col(last, 'sim'), 0.5),
    gpu_pool_hw_growth_pct: hwGrowth, gpu_pool_hw_monotonic: hwMonotonic ? 1 : 0, core_restarts: restarts.length,
    unexpected_reconnects: reconnects,
  })
  // frame cadence after the soak (D1-AC-03b on the full scene with the live soak fleet): snapshot.json for the harness
  await runFlight60(perfPage, { city: 'shenzhen', scene: 'full', source: 'live' })
  expect(heapGrowth).not.toBeNull()
  expect(heapGrowth!).toBeLessThanOrEqual(20)
  expect(rssGrowth, 'rss_growth_pct (api, sim-core RSS medians)').not.toBeNull()
  expect(rssGrowth!).toBeLessThanOrEqual(10)
  expect(hwMonotonic, `GPU pool high-water per window ${hw.join(', ')}`).toBe(false)
  expect(reconnects).toBe(0)
  perfPage.assertNoPageErrors()
})
