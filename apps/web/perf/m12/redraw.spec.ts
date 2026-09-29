// M12-AC-062 (performance case, ADR-033 protocol only): the track model's per-column aggregation over 500,000 markers at
// 1920 px stays within the 1.5 ms redraw budget (and ColumnAgg within 4 ms per slice); measured in the page on Tier S
// through window.__timeline.track (test build). The canvas part of M12-AC-017 (LfTimelineTrack draw, <= 4 Hz on Tier S)
// is M15's renderer and joins this case once the TimelineBar reads `timelineTrack` (request M12-to-M15).
import type { Server } from 'node:http'
import { expect, test } from '@playwright/test'
import { PORT, startStaticServer, stopServer } from '../m11/staticServer'
import { needTestBuild } from './common'

let server: Server | null = null
test.use({ baseURL: `http://127.0.0.1:${PORT}` })
test.beforeAll(async () => {
  server = await startStaticServer()
})
test.afterAll(async () => {
  await stopServer(server)
})

test('@perf column aggregation of 500k markers at 1920 px', async ({ page }) => {
  await page.goto('/world/shenzhen?source=fake&fakeN=1')
  await needTestBuild(page)
  const r = await page.evaluate(() => {
    const tl = (window as unknown as { __timeline: { track: {
      addMarker(t: number, l: number, m: number, a: number, s: number): boolean
      columns(t0: number, t1: number, w: number): { n: number }
      clearMarkers(): void
    } } }).__timeline
    const tr = tl.track
    tr.clearMarkers()
    for (let i = 0; i < 500_000; i++) tr.addMarker(i * 7.2, i % 50 === 0 ? 2 : 0, i % 50 === 0 ? 3 : 1, 0xffff, i)
    const ms: number[] = []
    for (let k = 0; k < 40; k++) {
      const t0 = performance.now()
      tr.columns(k * 0.001, 3600 + k * 0.001, 1920)
      ms.push(performance.now() - t0)
    }
    ms.sort((a, b) => a - b)
    tr.clearMarkers()
    return { p95: ms[Math.floor(0.95 * ms.length)], max: ms[ms.length - 1] }
  })
  test.info().annotations.push({ type: 'perf', description: JSON.stringify(r) })
  expect(r.p95).toBeLessThanOrEqual(1.5)
  expect(r.max).toBeLessThanOrEqual(4)
})
