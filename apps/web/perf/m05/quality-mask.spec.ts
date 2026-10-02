// M05-FR-054 / M06 qualityMask (AWR-18 §4.5 item 2; D1-AC-05 functional part): a test build with ?quality=1 during the
// flight60 scene=pc bench appends __perf.quality.samples[k] = {t, pose, mask} at t = 2.5 + 5k s; M06 renders the point
// channel again into the coverage target of the point-pass size and packs its alpha to 1 bit per pixel. Functional smoke:
// the first samples get a mask of ceil(W H / 8) bytes with some coverage and some background, the pose is finite, and
// the coverage pass compiles nothing after the reveal (its target is warmed under the mask). The hole rate against the
// full reference (M16 perf/quality.spec.ts) is a performance-phase case.
import { expect, test } from '@playwright/test'
import { expectNoErrors, m05Server, openWorld, watch } from './common'

const srv = m05Server(13)

test('coverage masks of the quality samples (flight60 scene=pc, ?quality=1)', async ({ page }) => {
  test.setTimeout(180_000)
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen', '?bench=flight60&scene=pc&chrome=0&quality=1')
  await page.waitForFunction(() => {
    const q = (window as unknown as { __perf: { quality: { samples: { mask: Uint8Array | null }[] } } }).__perf.quality
    return q.samples.length >= 2 && q.samples[0].mask !== null && q.samples[1].mask !== null
  }, null, { timeout: 120_000 })
  const r = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { quality: { samples: { t: number; pose: Float64Array; mask: Uint8Array }[] }; gpu: { compiledAfterReveal: number } } }).__perf
    const cv = document.querySelector('[data-viewport] canvas') as HTMLCanvasElement
    return {
      samples: p.quality.samples.slice(0, 2).map((s) => {
        let on = 0
        for (const b of s.mask) for (let k = 0; k < 8; k++) on += (b >> k) & 1
        return { t: s.t, poseOk: Array.from(s.pose).every(Number.isFinite), bytes: s.mask.length, on }
      }),
      db: [cv.width, cv.height],
      compiled: p.gpu.compiledAfterReveal,
    }
  })
  const px = r.db[0] * r.db[1] // Tier S: the point pass is the drawing buffer
  for (const [k, s] of r.samples.entries()) {
    expect(s.t).toBeGreaterThanOrEqual(2.5 + 5 * k)
    expect(s.poseOk).toBe(true)
    expect(s.bytes).toBe(Math.ceil(px / 8))
    expect(s.on, 'some coverage').toBeGreaterThan(0.01 * px)
    expect(s.on, 'some background').toBeLessThan(px)
  }
  expect(r.compiled, 'no program compiled after the reveal').toBe(0)
  expectNoErrors(w)
})
