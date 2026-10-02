// M05-AC-027 (D1-ext, P1; PRD-FR-017): point picking end to end through M06's Picker (pickAt 'point', the ID pass in the
// render phase into the 5 x 5 raster-px pickRT, asynchronous read-back) and M05's PointPicker (pick sub-table, decode from
// the CPU cache). Shenzhen, static browsing (no backend), home view after streaming converged:
//   * 20 click positions on the city (a grid over the view; misses on the sky are skipped): each pick returns a point
//     whose projection lies inside the 5 x 5 raster window around the click, widened by the drawn point radius;
//   * known points: clicking again exactly on the projection of each returned point returns that point within its
//     node's point spacing (the fixture is the decoded point itself, AC-027 "误差 ≤ 该节点点间距"), unless the ID pass
//     legitimately resolved a point in front of it (depth test: a nearer point whose square covers the same pixel);
//   * a real mouse click fills the viewport's point info (facade pick.point, event 'pick.point');
//   * the pass plan holds on the pick frames (render.calls == plan, the ID pass counted once) and no program compiles.
// Click-to-result p95 <= 200 ms and "20 consecutive clicks without a > 50 ms long task" are recorded here and asserted
// only under the performance lock (M05_PERF=1).
import { expect, test } from '@playwright/test'
import { PERF, expectNoErrors, frames, openWorld, m05Server, watch } from './common'

const srv = m05Server(12)

interface Hit { worldId: string; pointEnu: number[]; classIdx: number; className: string; hagM: number | null; spacingM: number }
type Vp = { __vp: { pointAt(x: number, y: number): Promise<Hit | null>; pointPick(): Hit | null; project(e: number, n: number, u: number): [number, number] | null } }

test('point pick returns the clicked point within its node spacing (M05-AC-027)', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  // converge: nothing in flight or queued for 2 s
  await page.evaluate(async () => {
    const pc = (window as unknown as { __pc: { stats(): { inflight: number; queued: number; pendingUploadPts: number } } }).__pc
    const t0 = performance.now()
    let since = -1
    while (performance.now() - t0 < 60_000) {
      await new Promise((r) => requestAnimationFrame(r))
      const s = pc.stats()
      if (s.inflight || s.queued || s.pendingUploadPts) since = -1
      else if (since < 0) since = performance.now()
      else if (performance.now() - since > 2000) return
    }
  })
  const probe0 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { gpu: { planMismatches: number; compiledAfterReveal: number } } }).__perf
    return { mism: p.gpu.planMismatches, compiled: p.gpu.compiledAfterReveal }
  })
  const geo = await page.evaluate(() => {
    const vp = (window as unknown as { __vp: { vpSession: { cssW: number; cssH: number; be: { tier: string; cloudScale: number }; host: HTMLElement } } }).__vp.vpSession
    const r = vp.host.getBoundingClientRect()
    const s = (window as unknown as { __pc: { stats(): { maxPxEff: number } } }).__pc.stats()
    const rung = (window as unknown as { __perf: { cas: { index: number } } }).__perf.cas.index
    return { w: vp.cssW, h: vp.cssH, left: r.left, top: r.top, tier: vp.be.tier, cloudScale: vp.be.cloudScale, maxPx: s.maxPxEff, rung, dbW: (document.querySelector('[data-viewport] canvas') as HTMLCanvasElement).width }
  })
  // the drawn point size is clamped to maxPxEff, which follows the rung's maxPx / maxPxSparse with 300 ms smoothing (dense
  // vs budget-limited or streaming frames, M05 §6.7.3): its upper bound over the run is the rung's maxPxSparse (g02 §7.2)
  const MAX_PX_SPARSE = [16, 16, 12, 12, 8, 8, 8]
  const maxPx = Math.max(geo.maxPx, MAX_PX_SPARSE[Math.min(MAX_PX_SPARSE.length - 1, Math.max(0, geo.rung))])
  // CSS px per raster px of the point pass (Tier S raster = drawing buffer, DPR 0.5)
  const cssPerRaster = geo.w / (geo.tier === 'S' ? geo.dbW : Math.round(geo.dbW * geo.cloudScale))
  const tolCss = (2.5 + 0.5 * maxPx + 1) * cssPerRaster
  const cands: [number, number][] = []
  for (let j = 0; j < 6; j++) for (let i = 0; i < 8; i++) cands.push([Math.round(geo.w * (0.15 + 0.1 * i)), Math.round(geo.h * (0.3 + 0.1 * j))])
  const hits: { x: number; y: number; p: Hit; ms: number }[] = []
  const longTasks = await page.evaluate(() => {
    const out: number[] = []
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) out.push(e.duration)
    }).observe({ type: 'longtask', buffered: false })
    ;(window as unknown as { __fxLong: number[] }).__fxLong = out
    return out.length
  })
  expect(longTasks).toBe(0)
  for (const [x, y] of cands) {
    if (hits.length >= 20) break
    const r = await page.evaluate(async ([cx, cy]) => {
      const t0 = performance.now()
      const p = await (window as unknown as Vp).__vp.pointAt(cx, cy)
      return { p, ms: performance.now() - t0 }
    }, [x, y])
    if (r.p) hits.push({ x, y, p: r.p, ms: r.ms })
  }
  expect(hits.length, 'picks on the city').toBeGreaterThanOrEqual(20)
  const eye = await page.evaluate(() => (window as unknown as { __vp: { camera(): { pose: { eye_enu_m: number[] } } } }).__vp.camera().pose.eye_enu_m)
  const eyeDist = (q: number[]): number => Math.hypot(q[0] - eye[0], q[1] - eye[1], q[2] - eye[2])
  let rePickOk = 0
  let occluded = 0
  const worst: string[] = []
  for (const h of hits) {
    const [e, n, u] = h.p.pointEnu
    expect(h.p.worldId).toBe('shenzhen')
    expect(h.p.spacingM).toBeGreaterThan(0)
    const s = await page.evaluate(([a, b, c]) => (window as unknown as Vp).__vp.project(a, b, c), [e, n, u])
    expect(s, 'picked point projects on screen').not.toBeNull()
    const d = Math.hypot(s![0] - h.x, s![1] - h.y)
    expect(d, `pick at (${h.x}, ${h.y}) -> projected (${s![0].toFixed(1)}, ${s![1].toFixed(1)})`).toBeLessThanOrEqual(tolCss)
    // known point: click exactly on its projection
    const again = await page.evaluate(([cx, cy]) => (window as unknown as Vp).__vp.pointAt(cx, cy), [s![0], s![1]])
    const err = again ? Math.hypot(again.pointEnu[0] - e, again.pointEnu[1] - n, again.pointEnu[2] - u) : Number.POSITIVE_INFINITY
    const s2 = again ? await page.evaluate(([a, b, c]) => (window as unknown as Vp).__vp.project(a, b, c), again.pointEnu) : null
    const inWindow = s2 !== null && Math.hypot(s2[0] - s![0], s2[1] - s![1]) <= tolCss
    if (err <= h.p.spacingM) rePickOk++
    else if (again && inWindow && eyeDist(again.pointEnu) < eyeDist(h.p.pointEnu)) occluded++
    else worst.push(`${err.toFixed(2)} m > spacing ${h.p.spacingM.toFixed(2)} m, eye ${again ? eyeDist(again.pointEnu).toFixed(1) : '-'} vs ${eyeDist(h.p.pointEnu).toFixed(1)} m, re-pick projects ${s2 ? Math.hypot(s2[0] - s![0], s2[1] - s![1]).toFixed(1) : '-'} css px away (tol ${tolCss.toFixed(1)}), spacing ${again?.spacingM.toFixed(2)} m`)
  }
  test.info().annotations.push({ type: 'repick', description: `${rePickOk} within spacing, ${occluded} occluded by a nearer point` })
  expect(worst, 'known points re-picked within their node spacing (or behind a nearer point)').toEqual([])
  expect(rePickOk, 'some known points come back exactly').toBeGreaterThan(0)
  // a real click fills the point info of the viewport
  await page.mouse.click(geo.left + hits[0].x, geo.top + hits[0].y)
  await page.waitForFunction(() => (window as unknown as Vp).__vp.pointPick() !== null, null, { timeout: 10_000 })
  const info = await page.evaluate(() => (window as unknown as Vp).__vp.pointPick())
  expect(info!.className.length).toBeGreaterThan(0)
  await frames(page, 5)
  const probe1 = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { gpu: { planMismatches: number; compiledAfterReveal: number } } }).__perf
    return { mism: p.gpu.planMismatches, compiled: p.gpu.compiledAfterReveal, long: (window as unknown as { __fxLong: number[] }).__fxLong }
  })
  expect(probe1.mism - probe0.mism, 'render.calls == pass plan on the pick frames').toBe(0)
  expect(probe1.compiled - probe0.compiled, 'no program compiled by the ID pass after the reveal').toBe(0)
  const ms = hits.map((h) => h.ms).sort((a, b) => a - b)
  const p95 = ms[Math.min(ms.length - 1, Math.ceil(0.95 * ms.length) - 1)]
  test.info().annotations.push({ type: 'pick_ms_p95', description: p95.toFixed(1) }, { type: 'long_tasks', description: probe1.long.map((x) => x.toFixed(0)).join(',') })
  if (PERF) {
    expect(p95).toBeLessThanOrEqual(200)
    expect(probe1.long.filter((x) => x > 50)).toEqual([])
  }
  expectNoErrors(w)
})
