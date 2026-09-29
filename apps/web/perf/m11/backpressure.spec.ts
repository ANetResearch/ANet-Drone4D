// M11-AC-015 (client side of the backpressure chain, AWR-17 §6.9 L0/L1; r27 §3.1.3): with the main thread busy 70 ms per
// frame (about 14 fps, __perf.inject) the rt.worker keeps only the latest record per channel, acks after consumption and
// the credit window holds the data plane, so the display delay stays bounded and does not grow: __perf.net.ageMs p95
// <= 150 ms over 8 s and |median(last 2 s) - median(first 2 s)| <= 20 ms; the control plane keeps working (LIVE).
// Data source: FakeSource N = 1000 (32 KB Lite32 per swarm frame) in the worker; the gateway half of AC-015 (30 Hz x
// 32 KiB, slot_overwrites) is measured with the real gateway by the acceptance harness.
// Performance case: runs only under the performance protocol (AWR-18 §3): AWR_PERF=1 and a test build in M11_DIST.
import type { Server } from 'node:http'
import { expect, test } from '@playwright/test'
import { PORT, startStaticServer, stopServer } from './staticServer'

test.skip(process.env.AWR_PERF !== '1', 'performance case: AWR_PERF=1 under the performance protocol')
let server: Server | null = null
test.use({ baseURL: `http://127.0.0.1:${PORT}` })
test.beforeAll(async () => {
  server = await startStaticServer()
})
test.afterAll(async () => {
  await stopServer(server)
})

interface PerfNet { ageMs: { buf: Float64Array | Record<string, number>; n: number } }
interface PerfWin { __perf?: { net?: PerfNet; inject?: (o: { busyMs?: number }) => void }; __vp?: { conn(): string } }

test('main thread busy 70 ms per frame: display delay bounded and not growing', async ({ page }) => {
  await page.goto('/world/shenzhen?source=fake&fakeN=1000')
  await page.waitForFunction(() => (window as unknown as PerfWin).__vp?.conn() === 'LIVE', null, { timeout: 60_000 })
  await page.waitForFunction(() => ((window as unknown as PerfWin).__perf?.net?.ageMs.n ?? 0) > 20, null, { timeout: 30_000 })
  await page.evaluate(() => (window as unknown as PerfWin).__perf!.inject!({ busyMs: 70 }))
  await page.waitForTimeout(1000) // settle into the slow cadence
  const n0 = await page.evaluate(() => (window as unknown as PerfWin).__perf!.net!.ageMs.n)
  await page.waitForTimeout(8000)
  const samples = await page.evaluate((from) => {
    const r = (window as unknown as PerfWin).__perf!.net!.ageMs
    const buf = r.buf as Float64Array
    const out: number[] = []
    for (let i = from; i < r.n; i++) out.push(buf[i & (buf.length - 1)])
    return out
  }, n0)
  await page.evaluate(() => (window as unknown as PerfWin).__perf!.inject!({ busyMs: 0 }))
  expect(samples.length).toBeGreaterThan(40) // about 14 fps x 8 s
  const sorted = [...samples].sort((a, b) => a - b)
  const p95 = sorted[Math.floor(0.95 * (sorted.length - 1))]
  const med = (xs: number[]): number => [...xs].sort((a, b) => a - b)[xs.length >> 1]
  const k = Math.floor(samples.length / 4)
  const drift = Math.abs(med(samples.slice(-k)) - med(samples.slice(0, k)))
  test.info().annotations.push({ type: 'measure', description: `ageMs p95 ${p95.toFixed(1)} ms, drift ${drift.toFixed(1)} ms, n ${samples.length}` })
  expect(p95).toBeLessThanOrEqual(150)
  expect(drift).toBeLessThanOrEqual(20)
  expect(await page.evaluate(() => (window as unknown as PerfWin).__vp!.conn())).toBe('LIVE')
})
