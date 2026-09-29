// M05-AC-025 (P1; AWR-18 §4.6): the camera jumps to 5 key poses of the flight60 path (overview, transit start, orbit
// middle, follow start, climb-out end) and stays; convergence = nothing in flight or queued and B changing < 5 % over 2 s
// (Tier S). Functional smoke records the times; the 3 s bound is asserted under the performance lock only.
import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { PERF, expectNoErrors, look, openWorld, m05Server, watch } from './common'

const srv = m05Server(10)
const KEY_T = [0, 6, 32, 40, 59.9]

test('shenzhen: convergence after jumps to the 5 key poses (M05-AC-025)', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  const b = readFileSync(resolve(import.meta.dirname, '../../public/bench/flight60/shenzhen.bin'))
  const fl = new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength))
  await openWorld(page, srv.get().url, 'shenzhen')
  const times: number[] = []
  for (const t of KEY_T) {
    const o = 6 * Math.round(t * 60)
    await look(page, [fl[o], fl[o + 1], fl[o + 2]], [fl[o + 3], fl[o + 4], fl[o + 5]])
    const ms = await page.evaluate(async () => {
      const pc = (window as unknown as { __pc: { stats(): { inflight: number; queued: number; B: number } } }).__pc
      const t0 = performance.now()
      let stableSince = -1
      let lastB = pc.stats().B
      while (performance.now() - t0 < 20_000) {
        await new Promise((r) => requestAnimationFrame(r))
        const s = pc.stats()
        const quiet = s.inflight === 0 && s.queued === 0 && Math.abs(s.B - lastB) / Math.max(1, lastB) < 0.05
        if (!quiet) {
          stableSince = -1
          lastB = s.B
        } else if (stableSince < 0) stableSince = performance.now()
        else if (performance.now() - stableSince >= 2000) return stableSince - t0
      }
      return Number.NaN
    })
    times.push(ms)
  }
  test.info().annotations.push({ type: 'converge_ms', description: times.map((x) => x.toFixed(0)).join(', ') })
  for (const t of times) expect(Number.isFinite(t)).toBe(true)
  if (PERF) for (const t of times) expect(t).toBeLessThanOrEqual(3000)
  expectNoErrors(w)
})
