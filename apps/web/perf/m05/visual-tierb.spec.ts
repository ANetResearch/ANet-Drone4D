// Visual review of the point cloud on Tier B at a fixed high budget (FX-WEB1 task 5; M05-FR-029, FR-030, FR-032; ADR-011,
// ADR-063): six cities, ?tier=B&fixedB=2000000 with the quality locked at `high` (rung 5: tau 1 px, rs 1, 8 EDL taps;
// the software device's auto ceiling is `soft`, so the lock is the "same as the user picking High" switch), static
// browsing (no backend), an oblique mid-range view of the tallest building (the flight60 peak). After streaming
// converges the spec records the frame statistics, the coverage and the interior-hole fraction of the cloud target
// (background pixels inside a covered surface), and writes the screenshot (AWR_SHOTS_DIR, else the test output
// directory). It asserts the invariants (Tier B, EDL bound and active, no compile after the reveal, pass plan, drawn <=
// B, no failed node, the ADR-063 point-size cap) and a loose hole bound; the look itself is reviewed by eye.
// Opt-in (a 2M-point budget on SwiftShader takes 1-2 min per city): M05_VISUAL=1 [M05_VISUAL_CITIES=a,b]
// [M05_VISUAL_COLOR=normal|hag|class|height] [AWR_SHOTS_DIR=<dir>].
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { expectNoErrors, openWorld, perf, m05Server, watch } from './common'

const srv = m05Server(14)
const CITIES = (process.env.M05_VISUAL_CITIES ?? 'shenzhen,newyork,shanghai,suzhou,sanfrancisco,chicago').split(',')
// colour mode override for the review (height, hag, normal, class); default: the world's default mode
const MODE = process.env.M05_VISUAL_COLOR ?? ''
const BENCH = resolve(import.meta.dirname, '../../public/bench/flight60')

type Pc = { __pc: { engine: { edlMaterial: { uniforms: { strength: { value: number } } } | null; stats(): Record<string, number | string> }; layers: { setQuality(q: number): void; setColorMode(m: string): void } } }

for (const city of CITIES) {
  test(`Tier B high-density view of ${city}`, async ({ page }, info) => {
    test.skip(process.env.M05_VISUAL !== '1', 'visual review run: M05_VISUAL=1')
    test.setTimeout(420_000)
    const w = watch(page)
    // oblique mid-range view of the tallest building (flight60 peak P): 700 m back along the approach direction
    // dirIn = P / |P| (AWR-18 §8.6), 380 m above 0.35 h, aimed at 0.3 h of the building (about -27 deg)
    const meta = JSON.parse(readFileSync(`${BENCH}/${city}.json`, 'utf8')) as { peak: { x_m: number; y_m: number; h_m: number } }
    const P = meta.peak
    const r = Math.hypot(P.x_m, P.y_m)
    const dirIn = r > 50 ? [P.x_m / r, P.y_m / r] : [Math.SQRT1_2, Math.SQRT1_2]
    const eye = [P.x_m - 700 * dirIn[0], P.y_m - 700 * dirIn[1], 0.35 * P.h_m + 380]
    const target = [P.x_m, P.y_m, 0.3 * P.h_m]
    await openWorld(page, srv.get().url, city, '?tier=B&fixedB=2000000&chrome=0')
    await page.evaluate((m) => {
      const l = (window as unknown as Pc).__pc.layers
      l.setQuality(5)
      if (m) l.setColorMode(m)
    }, MODE)
    await page.evaluate(([e, t]) => (window as unknown as { __vp: { vpSession: { rig: { lookAtEnu(a: number[], b: number[], c: boolean): void } } } }).__vp.vpSession.rig.lookAtEnu(e, t, false), [eye, target])
    await page.evaluate(async () => {
      const pc = (window as unknown as Pc).__pc
      const t0 = performance.now()
      let since = -1
      while (performance.now() - t0 < 300_000) {
        await new Promise((r) => requestAnimationFrame(r))
        const s = pc.engine.stats()
        if (s.inflight || s.queued || s.pendingUploadPts || (s.progress as number) < 0.999) since = -1
        else if (since < 0) since = performance.now()
        else if (performance.now() - since > 2500) return
      }
    })
    await page.waitForTimeout(600)
    const st = await page.evaluate(async () => {
      const w = window as unknown as Pc & { __vp: { vpSession: { be: { tier: string; cloudScale: number; cloudTarget(): { width: number; height: number }; readPixels(rt: unknown, x: number, y: number, w: number, h: number, o: Uint8Array): Promise<Uint8Array> } } } }
      const be = w.__vp.vpSession.be
      const rt = be.cloudTarget()
      const W = Math.round(rt.width * be.cloudScale)
      const H = Math.round(rt.height * be.cloudScale)
      const px = new Uint8Array(rt.width * rt.height * 4)
      await be.readPixels(rt, 0, 0, rt.width, rt.height, px)
      // interior holes: background pixels with >= 6 of their 8 neighbours covered (cracks in a surface, not the sky or
      // the ground outside the data), over the covered pixels of the cloud sub-viewport
      const bgAt = (x: number, y: number): boolean => {
        const i = 4 * (y * rt.width + x)
        return px[i] + px[i + 1] + px[i + 2] < 12
      }
      let holes = 0
      let covered = 0
      for (let y = 1; y < H - 1; y++) for (let x = 1; x < W - 1; x++) {
        if (!bgAt(x, y)) {
          covered++
          continue
        }
        let c = 0
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) if ((dx || dy) && !bgAt(x + dx, y + dy)) c++
        if (c >= 6) holes++
      }
      const s = w.__pc.engine.stats()
      return { tier: be.tier, W, H, holes: holes / Math.max(1, covered), coverage: covered / ((W - 2) * (H - 2)), drawn: s.drawn as number, B: s.B as number, limitedBy: s.limitedBy as string, maxPx: s.maxPxEff as number, rung: s.rungName as string,
        rs: s.rsEff as number, achieved: s.achievedErrPx as number, edl: w.__pc.engine.edlMaterial?.uniforms.strength.value ?? 0 }
    })
    info.annotations.push({ type: 'stats', description: JSON.stringify(st) })
    expect(st.tier).toBe('B')
    expect(st.rung).toBe('high')
    expect(st.edl, 'EDL active (rung high has 8 taps)').toBeGreaterThan(0)
    expect(st.drawn).toBeGreaterThan(100_000)
    expect(st.drawn).toBeLessThanOrEqual(2_000_000)
    expect(['headroom', 'error', 'complete']).toContain(st.limitedBy) // dense: tau-limited, not budget-limited
    expect(st.maxPx, 'ADR-063 cap: never above the rung maxPx').toBeLessThanOrEqual(8 + 1e-6)
    expect(st.coverage, 'the city fills a good part of the view').toBeGreaterThan(0.25)
    expect(st.holes, 'interior holes (background through a covered surface)').toBeLessThan(0.05)
    expect(await perf<number>(page, 'pc.budgetViolations')).toBe(0)
    expect(await perf<number>(page, 'pc.failed')).toBe(0)
    expect(await perf<number>(page, 'gpu.planMismatches')).toBe(0)
    expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(0)
    const dir = process.env.AWR_SHOTS_DIR
    const tag = MODE ? `${city}-${MODE}` : city
    await page.screenshot({ path: dir ? `${dir}/fx-web1-tierB-${tag}.png` : info.outputPath(`tierB-${tag}.png`) })
    expectNoErrors(w)
  })
}
