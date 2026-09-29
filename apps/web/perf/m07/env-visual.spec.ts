// env-visual (M07-AC-020, M07-AC-021 functional parts, M07-AC-045 counters): on Tier S with a rain preset and the
// arrows on, environment draws <= 3 (plus the interim 2D-cloud quad while M06 has no sky() hook), rain vertices
// <= 6 x 1424 and N_live = floor(N_cap rain_k), arrows exactly 6 x 576 vertices; __perf.env and
// __perf.layers.environment are readable; the version switches only when tRender crosses t_apply_ns.
import { expect, test } from '@playwright/test'
import { envState, m07Server, openEnv, pageErrors } from './common'

const srv = m07Server(3)

test('Tier S budgets, counters and version switch timing', async ({ page }) => {
  await openEnv(page, srv.get().url, '&tier=S')
  await page.evaluate(() => {
    const e = (window as unknown as { __env: { injectPreset(id: string, s: number): number; env: { sub: { arrows: boolean } } } }).__env
    e.env.sub.arrows = true
    e.injectPreset('heavyRain', 0)
  })
  // SwiftShader runs a few frames per second while the world streams: wait for the damped step switch to settle
  await page.waitForFunction(() => {
    const e = (window as unknown as { __env: { env: { store: { damper: { active: boolean } } }; state(): { perf: { live: { rain: number } } } } }).__env
    return !e.env.store.damper.active && e.state().perf.live.rain > 0
  }, null, { timeout: 60_000 })
  const s = await envState(page)
  expect(s.state).toBe('SYNCED')
  expect(s.perf.live.arrows).toBe(576)
  expect(s.perf.live.rain).toBe(Math.floor(1424 * s.rainK))
  expect(s.perf.live.rain + s.perf.live.snow + s.perf.live.dust + s.perf.live.arrows).toBeLessThanOrEqual(2000)
  expect(s.perf.draws).toBeLessThanOrEqual(4)
  const layer = await page.evaluate(() => (window as unknown as { __perf: { layers: { environment: { draws: number; verts: number } }; env: { quality: string } } }).__perf)
  expect(layer.layers.environment.draws).toBe(s.perf.draws)
  expect(layer.env.quality).toBe('low')
  // a keyframe whose t_apply is later than tRender stays queued until tRender crosses it
  const r = await page.evaluate(async () => {
    const e = (window as unknown as { __env: { state(): { version: number; tRenderNs: number }; injectPreset(id: string, s: number): number } }).__env
    const v0 = e.state().version
    const v1 = e.injectPreset('fog', 0)
    const seen = [e.state().version]
    const t0 = performance.now()
    while (e.state().version !== v1 && performance.now() - t0 < 30_000) await new Promise((ok) => setTimeout(ok, 100))
    seen.push(e.state().version)
    return { v0, v1, seen }
  })
  expect(r.seen[0]).toBe(r.v0)
  expect(r.seen[1]).toBe(r.v1)
  expect(pageErrors(page)).toEqual([])
})
