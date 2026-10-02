// Drone LOD tiers in one frame (M06-FR-033..035, AC-025, AC-026; ADR-022; FX-WEB1 task 4): FakeSource with 200 vehicles on
// the ground ring (vehicle a at radius 50 + 20 (a mod 10) m, z = 0, about 7 m above the Shenzhen DTM there), the camera
// 4.3 m from vehicle 0 looking out along +x, so the same frame holds the P600 hero model (screen radius > 48 px; GET
// /models/p600.glb, the copy bundled with the web build, AWR-17 §5.1), low-poly instances (> 4 px) on the near ring and
// screen-space markers beyond. Asserts the three buckets are populated on screen, the hero glb was served (not the SPA
// index.html) and the frame keeps the pass plan without a compile after the reveal (the glb arrives after the shader
// zoo, and hiding layers compiles nothing either); writes the frame (other layers hidden) and one crop per tier
// (AWR_SHOTS_DIR, else the test output directory) for the visual review.
import { expect, test } from '@playwright/test'
import { frames, openWorld, perf, watch } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

type Vp = { __vp: { drones(): { n: number; heroN: number; lowN: number; markers: number; glyphs: number } | null; project(e: number, n: number, u: number): [number, number] | null
  viewportRect(): { x: number; y: number; w: number; h: number } | null
  vpSession: { rig: { lookAtEnu(e: number[], t: number[], tr: boolean): void }
    drones: { poses: { n: number; pos: Float32Array }; layer: { buckets: { bucket: Uint8Array; rpx: Float32Array } } } | null } } }
type Pc = { __pc: { layers: { setVisible(id: string, on: boolean): void } } }

/** CSS px of one on-screen vehicle per bucket (0 marker, 1 low-poly, 2 hero): the largest on screen (screen radius) */
async function onScreenByBucket(page: import('@playwright/test').Page): Promise<Record<number, [number, number] | null>> {
  return page.evaluate(() => {
    const vp = (window as unknown as Vp).__vp
    const d = vp.vpSession.drones!
    const r = vp.viewportRect()!
    const out: Record<number, [number, number] | null> = { 0: null, 1: null, 2: null }
    const best: Record<number, number> = { 0: -1, 1: -1, 2: -1 }
    for (let i = 0; i < d.poses.n; i++) {
      const b = d.layer.buckets.bucket[i]
      if (b > 2) continue
      const s = vp.project(d.poses.pos[3 * i], d.poses.pos[3 * i + 1], d.poses.pos[3 * i + 2])
      if (!s || s[0] < 60 || s[1] < 60 || s[0] > r.w - 60 || s[1] > r.h - 60) continue
      const c = d.layer.buckets.rpx[i]
      if (c > best[b]) {
        best[b] = c
        out[b] = [s[0] + r.x, s[1] + r.y]
      }
    }
    return out
  })
}

for (const tier of ['S', 'B'] as const) {
  test(`hero model, low-poly instances and markers are distinct in one frame (Tier ${tier})`, async ({ page }, info) => {
    test.setTimeout(240_000)
    const w = watch(page)
    const glb: { url: string; status: number }[] = []
    page.on('response', (r) => {
      if (/\.glb(\?|$)/.test(r.url())) glb.push({ url: r.url(), status: r.status() })
    })
    await openWorld(page, srv!.url, `source=fake&fakeN=200&fakeGround=1&envPeriodS=0&chrome=0${tier === 'B' ? '&tier=B' : ''}`)
    await page.waitForFunction(() => ((window as unknown as Vp).__vp.drones()?.n ?? 0) >= 150, null, { timeout: 90_000 })
    // a clean frame for the review: only the vehicles over the ground grid (the point cloud, zone walls, precipitation,
    // sensor frustums, trails and labels would sit between the camera and the vehicles of this close-up)
    await page.evaluate(() => {
      const l = (window as unknown as Pc).__pc.layers
      for (const id of ['pointcloud', 'zones', 'environment', 'frustums', 'trails', 'labels']) l.setVisible(id, false)
    })
    await page.evaluate(() => (window as unknown as Vp).__vp.vpSession.rig.lookAtEnu([46.5, -2.0, 1.6], [50, 0, 0.2], false))
    await page.waitForFunction(() => {
      const d = (window as unknown as Vp).__vp.drones()
      return !!d && d.heroN >= 1 && d.lowN >= 1 && d.markers >= 1
    }, null, { timeout: 60_000, polling: 250 })
    await frames(page, 30)
    const d = (await page.evaluate(() => (window as unknown as Vp).__vp.drones()))!
    const at = await onScreenByBucket(page)
    test.info().annotations.push({ type: 'buckets', description: `${JSON.stringify(d)} on screen ${JSON.stringify(at)}` })
    expect(d.heroN).toBeGreaterThanOrEqual(1)
    expect(d.heroN).toBeLessThanOrEqual(tier === 'S' ? 2 : 6) // hero cap per tier (M06 §6.8)
    expect(d.lowN).toBeGreaterThanOrEqual(3)
    expect(d.markers).toBeGreaterThanOrEqual(10)
    for (const b of [0, 1, 2]) expect(at[b], `a bucket-${b} vehicle on screen`).not.toBeNull()
    // the hero geometry came from the bundled glb (GLTFLoader rejects the SPA fallback)
    const hero = glb.find((g) => g.url.includes('/models/p600.glb'))
    expect(hero?.status, JSON.stringify(glb)).toBe(200)
    expect(await perf<number>(page, 'gpu.planMismatches')).toBe(0)
    expect(await perf<number>(page, 'gpu.compiledAfterReveal'), 'the late glb swap compiles nothing').toBe(0)
    const dir = process.env.AWR_SHOTS_DIR
    const file = (s: string): string => (dir ? `${dir}/fx-web1-drone-tiers-${tier}${s}.png` : info.outputPath(`drone-tiers-${tier}${s}.png`))
    await page.screenshot({ path: file('') })
    const size = { hero: 360, low: 160, marker: 120 }
    for (const [b, name] of [[2, 'hero'], [1, 'low'], [0, 'marker']] as const) {
      const [x, y] = at[b]!
      const s = size[name]
      await page.screenshot({ path: file(`-${name}`), clip: { x: Math.max(0, x - s / 2), y: Math.max(0, y - s / 2), width: s, height: s } })
    }
    expect(w.errors).toEqual([])
    expect(w.m06).toEqual([])
  })
}
