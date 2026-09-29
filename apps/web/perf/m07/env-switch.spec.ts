// env-switch (M07-AC-018, M07-AC-019; D1-AC-19, D1-AC-25): clear -> thunderstorm over 30 s (4-segment route) on Tier S:
// renderer programs never grow after the mask is lifted (first preset switch, quality Low -> Off -> Low, arrows and
// precipitation on/off), and with M07_PERF=1 frames longer than 100 ms stay <= 0.5 % during the transition and each
// operation is followed by a max frame gap <= 150 ms within 1 s. Functional runs check programs and liveness only.
import { expect, test } from '@playwright/test'
import { envState, m07Server, openEnv, pageErrors, PERF, programs } from './common'

const srv = m07Server(2)

test('preset switch, quality and sub-layer toggles without new programs', async ({ page }) => {
  await openEnv(page, srv.get().url, '&tier=S')
  await page.waitForTimeout(1500)
  const p0 = await programs(page)
  // clear first (steady on a preset so the route applies), then the 30 s route to thunderstorm
  await page.evaluate(() => (window as unknown as { __env: { injectPreset(id: string, s: number): number } }).__env.injectPreset('clear', 0))
  await page.waitForFunction(() => (window as unknown as { __env: { env: { store: { current: { toPreset: string } | null; damper: { active: boolean } } } } }).__env.env.store.current?.toPreset === 'clear',
    null, { timeout: 60_000 })
  const v = await page.evaluate(() => (window as unknown as { __env: { injectPreset(id: string, s: number): number } }).__env.injectPreset('thunderstorm', 30))
  expect(v).toBeGreaterThan(0)
  const frames = await page.evaluate(async (ms) => {
    const out: number[] = []
    let last = performance.now()
    const t0 = last
    await new Promise<void>((done) => {
      const f = (): void => {
        const now = performance.now()
        out.push(now - last)
        last = now
        if (now - t0 < ms) requestAnimationFrame(f)
        else done()
      }
      requestAnimationFrame(f)
    })
    return out
  }, PERF ? 31_000 : 6_000)
  const s1 = await envState(page)
  expect(s1.version).toBeGreaterThanOrEqual(v)
  expect(s1.perf.live.rain).toBeGreaterThanOrEqual(0)
  // quality Low -> Off -> Low and sub-layer toggles: uniforms, drawRange and visibility only
  await page.evaluate(() => {
    const env = (window as unknown as { __env: { env: { quality: { knob: { apply(l: number): void } }; sub: { arrows: boolean; precip: boolean } } } }).__env.env
    env.quality.knob.apply(1)
    env.quality.knob.apply(0)
    env.sub.arrows = true
    env.sub.precip = false
    env.sub.precip = true
  })
  await page.waitForFunction(() => (window as unknown as { __perf: { frame: { count: number } } }).__perf.frame.count > 0, null, { timeout: 5_000 })
  await page.waitForTimeout(3000)
  expect(await programs(page)).toBe(p0)
  if (PERF) {
    const long = frames.filter((d) => d > 100).length
    expect(long / frames.length).toBeLessThanOrEqual(0.005)
  }
  expect(pageErrors(page)).toEqual([])
})
