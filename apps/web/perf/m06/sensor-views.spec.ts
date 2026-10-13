// D2 sensor views of the FPV camera (WP-11; AWR-04 §10.5, §10.7; AWR-14 §20.10.5; D2-AC-21, D2-AC-25, UX-AC-060, UX-AC-062).
// Owner: M06. On FakeSource (synthcity, flying ring), test build:
//   1. Tier S and Tier B: FPV on a vehicle, then EO, night, LWIR, FPV: every switch compiles no program (gpu.programs and
//      compiledAfterReveal unchanged), the pass plan equals the draws (planMismatches 0), the composite runs only in the
//      sensor views, the FPV projection carries the reversed-depth z row; the switch time (set to the second presented
//      frame) is recorded and asserted <= 300 ms only under M06_PERF=1 (acceptance phase, exclusive lock).
//   2. Tier S night: the illuminator brightens the night view (cone and range from the payload, here a test override);
//      LWIR emitters (a 31 deg C person and a car with a hot core, 12-25 m ahead) are the hot spots, white-hot puts them
//      above the background and black-hot below; no red pixel in the LWIR view while a red-owned vehicle is in view
//      (the EO view shows it red when it is in view: sanity of the red source).
//   3. availability and the B-key cycle (FPV -> EO -> night -> LWIR -> FPV), leaving FPV returns to FPV, LWIR digital
//      zoom 1-4 (display only), the capture-box projection, no console WebGL error.
import { expect, test, type Page } from '@playwright/test'
import { BOOT_MS, decodePng, firstVehicle, frames, openWorld, PERF, perf, watch, type Img, type SvHooks, type VpHooks } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

const SWITCH_MS = 300
type H = SvHooks & VpHooks
const Q = 'source=fake&fakeWorld=synthcity&fakeN=6'

function glWatch(page: Page): string[] {
  const gl: string[] = []
  page.on('console', (m) => {
    const t = m.text()
    if ((m.type() === 'warning' || m.type() === 'error') && /GL_INVALID|WebGL: |M06-E00[67]/.test(t)) gl.push(t.slice(0, 240))
  })
  return gl
}

async function enterFpv(page: Page): Promise<string> {
  const id = await firstVehicle(page)
  await page.evaluate((i) => (window as unknown as H).__vp.select([i]), id)
  const r = await page.evaluate(() => (window as unknown as H).__vp.setMode('fpv'))
  expect(r.ok, `fpv: ${r.reason}`).toBe(true)
  await frames(page, 10)
  return id
}

/** switch the view and wait for the second presented frame; returns the elapsed ms and the program delta */
async function switchTo(page: Page, m: 'fpv' | 'eo' | 'nir' | 'lwir'): Promise<{ ms: number; dp: number; ok: boolean; reason?: string }> {
  return page.evaluate(async (mm) => {
    const w = window as unknown as H & { __perf: { gpu: { programs: number } } }
    const p0 = w.__perf.gpu.programs
    const t0 = performance.now()
    const r = w.__vp.sensorView.set(mm)
    await new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(() => ok(null))))
    return { ms: performance.now() - t0, dp: w.__perf.gpu.programs - p0, ok: r.ok, reason: r.reason }
  }, m)
}

interface Clip { x: number; y: number; width: number; height: number; cx: number; cy: number }
async function centreClip(page: Page): Promise<Clip> {
  const r = (await page.evaluate(() => (window as unknown as H).__vp.viewportRect()))!
  // around the canvas centre (the optical axis in FPV), clear of the toolbars, the fleet rail, the HUD and the toasts
  const mx = Math.round(r.x + r.w / 2)
  const my = Math.round(r.y + r.h / 2)
  const x = Math.max(0, mx - 280)
  const y = Math.max(0, my - 190)
  return { x, y, width: 560, height: 380, cx: mx - x, cy: my - y }
}
async function shot(page: Page, clip: Clip): Promise<Img> {
  await frames(page, 3)
  return decodePng(await page.screenshot({ clip: { x: clip.x, y: clip.y, width: clip.width, height: clip.height } }))
}
const luma = (img: Img, x0: number, y0: number, w: number, h: number): number => {
  let s = 0
  for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) {
    const q = 4 * (y * img.w + x)
    s += 0.2126 * img.px[q] + 0.7152 * img.px[q + 1] + 0.0722 * img.px[q + 2]
  }
  return s / (w * h)
}
const redPx = (img: Img): number => {
  let n = 0
  for (let q = 0; q < img.px.length; q += 4) if (img.px[q] >= 96 && img.px[q] - Math.max(img.px[q + 1], img.px[q + 2]) >= 48) n++
  return n
}

for (const tier of ['S', 'B'] as const) {
  test(`Tier ${tier}: sensor views switch without compiling, the plan holds, reversed FPV depth (D2-AC-21)`, async ({ page }) => {
    test.setTimeout(240_000)
    const w = watch(page)
    const gl = glWatch(page)
    await openWorld(page, srv!.url, `${Q}${tier === 'B' ? '&tier=B' : ''}`, 'synthcity', Math.max(BOOT_MS, 120_000))
    expect(await perf<string>(page, 'meta.tier')).toBe(tier)
    await enterFpv(page)
    const proj = await page.evaluate(() => (window as unknown as H).__vp.sensorView.projection())
    expect(proj!.reversed).toBe(true)
    // three's reversed perspective z row: m[10] = n / (f - n) > 0, m[14] = f n / (f - n) > 0
    expect(proj!.m[10]).toBeGreaterThan(0)
    expect(proj!.m[14]).toBeGreaterThan(0)
    const atReveal = await perf<number>(page, 'gpu.programsAtReveal')
    const mm0 = await perf<number>(page, 'gpu.planMismatches')
    const times: string[] = []
    for (const m of ['eo', 'nir', 'lwir', 'fpv', 'lwir', 'eo', 'fpv'] as const) {
      const r = await switchTo(page, m)
      expect(r.ok, `${m}: ${r.reason}`).toBe(true)
      expect(r.dp, `programs compiled by switching to ${m}`).toBe(0)
      times.push(`${m} ${r.ms.toFixed(0)} ms`)
      if (PERF) expect(r.ms, `switch to ${m}`).toBeLessThanOrEqual(SWITCH_MS)
      await frames(page, 6)
      const pass = await page.evaluate(() => (window as unknown as H).__vp.sensorView.pass())
      expect(pass!.active).toBe(m !== 'fpv')
      if (m !== 'fpv') expect(pass!.mode).toBe({ eo: 0, nir: 1, lwir: 2 }[m])
      expect(pass!.rt).not.toBeNull()
    }
    test.info().annotations.push({ type: 'switch time', description: times.join(', ') })
    expect(await perf<number>(page, 'gpu.programs')).toBe(atReveal)
    expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(0)
    expect(await perf<number>(page, 'gpu.planMismatches')).toBe(mm0)
    expect(gl).toEqual([])
    expect(w.errors).toEqual([])
    expect(w.m06).toEqual([])
  })
}

test('Tier S: night and thermal looks; no red pixel in LWIR (D2-AC-25, UX-AC-062)', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  const gl = glWatch(page)
  await openWorld(page, srv!.url, Q, 'synthcity', Math.max(BOOT_MS, 120_000))
  const id = await enterFpv(page)
  const clip = await centreClip(page)
  // a red-owned vehicle in view of the FPV camera (the one nearest the image centre), when one is
  const red = await page.evaluate((focus) => {
    const vp = (window as unknown as H).__vp
    let best: string | null = null
    let bd = Infinity
    for (const r of vp.roster()) {
      if (r.id === focus) continue
      const p = vp.dronePose(r.id)
      const s = p ? vp.project(p[0], p[1], p[2]) : null
      if (!s) continue
      const d = Math.hypot(s[0] - 640, s[1] - 360)
      if (d < bd) {
        bd = d
        best = r.id
      }
    }
    return best
  }, id)
  if (red) await page.evaluate((r) => (window as unknown as H & { __vp: { setRedOwner(id: string | null): void } }).__vp.setRedOwner(r), red)
  expect((await switchTo(page, 'eo')).ok).toBe(true)
  const eo = await shot(page, clip)
  test.info().annotations.push({ type: 'red owner in view', description: `${red ?? 'none'}: EO red pixels ${redPx(eo)}` })
  expect((await switchTo(page, 'lwir')).ok).toBe(true)
  for (const pal of ['white_hot', 'black_hot'] as const) {
    await page.evaluate((p) => (window as unknown as H).__vp.sensorView.setPalette(p), pal)
    expect(redPx(await shot(page, clip)), `red pixels in the LWIR view (${pal})`).toBe(0)
  }
  if (red) await page.evaluate(() => (window as unknown as H & { __vp: { setRedOwner(id: string | null): void } }).__vp.setRedOwner(null))

  // night: passive light gone, one illuminator on; then thermal emitters ahead of the camera
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setEnv({ illumLx: 0.002, tAirC: 12, tSurfaceC: 9, tSkyC: -30, sunEnu: [0.3, 0.2, -0.5] }))
  expect((await switchTo(page, 'nir')).ok).toBe(true)
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setIlluminators([]))
  const dark = luma(await shot(page, clip), 0, 0, clip.width, clip.height)
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setIlluminators([{ on: true, divergenceRad: 0.9, rangeM: 150 }]))
  await frames(page, 4)
  expect((await page.evaluate(() => (window as unknown as H).__vp.sensorView.pass()))!.illuminators).toBe(1)
  const lit = luma(await shot(page, clip), 0, 0, clip.width, clip.height)
  test.info().annotations.push({ type: 'night view mean luma', description: `illuminator off ${dark.toFixed(1)}, on ${lit.toFixed(1)}` })
  expect(lit).toBeGreaterThan(dark + 3)

  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setEmittersAhead([
    [12, 0, 0, 0.6, 0.6, 1.8, 0, 31, Number.NaN, 0.97, 1],
    [25, 4, -1, 4.5, 1.8, 1.5, 0.4, Number.NaN, 25, 0.9, 0.3],
  ]))
  expect((await switchTo(page, 'lwir')).ok).toBe(true)
  const centre = async (): Promise<{ c: number; all: number }> => {
    // the person sits on the optical axis: the image centre of the canvas (the clip is centred on it)
    const img = await shot(page, clip)
    return { c: luma(img, clip.cx - 3, clip.cy - 3, 6, 6), all: luma(img, 0, 0, clip.width, clip.height) }
  }
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setPalette('white_hot'))
  const wh = await centre()
  const pass = await page.evaluate(() => (window as unknown as H).__vp.sensorView.pass())
  expect(pass!.emitters).toBeGreaterThanOrEqual(1)
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setPalette('black_hot'))
  const bh = await centre()
  test.info().annotations.push({ type: 'LWIR centre / mean luma', description: `white-hot ${wh.c.toFixed(0)} / ${wh.all.toFixed(0)}, black-hot ${bh.c.toFixed(0)} / ${bh.all.toFixed(0)}, range ${pass!.range.map((x) => x.toFixed(1)).join('..')}` })
  expect(wh.c).toBeGreaterThan(wh.all + 20)
  expect(bh.c).toBeLessThan(bh.all - 20)
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setEmittersAhead([]))
  expect(gl).toEqual([])
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('Tier S: availability, B cycle, leaving FPV, digital zoom, capture box', async ({ page }) => {
  test.setTimeout(180_000)
  const w = watch(page)
  await openWorld(page, srv!.url, Q, 'synthcity', Math.max(BOOT_MS, 120_000))
  // outside FPV the views are refused
  expect((await page.evaluate(() => (window as unknown as H).__vp.sensorView.set('eo'))).reason).toBe('not_fpv')
  await enterFpv(page)
  const av = await page.evaluate(() => (window as unknown as H).__vp.sensorView.available())
  expect(av).toEqual({ fpv: true, eo: true, nir: true, lwir: true })
  const seq: string[] = []
  for (let i = 0; i < 4; i++) seq.push(await page.evaluate(() => (window as unknown as H).__vp.sensorView.cycle()))
  expect(seq).toEqual(['eo', 'nir', 'lwir', 'fpv'])
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.set('lwir'))
  const info = await page.evaluate(() => (window as unknown as H).__vp.sensorView.info())
  expect(info.kind).toBe(2)
  expect(info.sensorName).not.toBeNull()
  await page.evaluate(() => (window as unknown as H).__vp.sensorView.setDigitalZoom(9))
  expect((await page.evaluate(() => (window as unknown as H).__vp.sensorView.info())).digitalZoom).toBe(4)
  // the capture box of a point on the optical axis contains the image centre
  const box = await page.evaluate(() => {
    const vp = (window as unknown as H).__vp
    const c = vp.camera()!.pose
    const e = c.eye_enu_m
    const t = c.target_enu_m
    const d = [t[0] - e[0], t[1] - e[1], t[2] - e[2]]
    const l = Math.hypot(d[0], d[1], d[2])
    return vp.sensorView.projectBox([e[0] + (d[0] / l) * 30, e[1] + (d[1] / l) * 30, e[2] + (d[2] / l) * 30], [2, 2, 2], 0)
  })
  expect(box).not.toBeNull()
  // leaving FPV returns the view to FPV
  await page.evaluate(() => (window as unknown as H).__vp.setMode('orbit'))
  await frames(page, 3)
  expect((await page.evaluate(() => (window as unknown as H).__vp.sensorView.info())).mode).toBe('fpv')
  expect((await page.evaluate(() => (window as unknown as H).__vp.sensorView.pass()))!.active).toBe(false)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})
