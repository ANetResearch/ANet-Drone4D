// D2.1 field view, quality profiles, touch and power save on the viewport (WP-11b; AWR-04 §15.7.7, §15.8, §15.11; ADR-121,
// ADR-122, ADR-125). Owner: M06. Test build, FakeSource synthcity, canvas only (?chrome=0: the M06 contract without the UI
// shell; the toolbar, legend and readout card are M15's), synthetic field data through window.__vp.field (dev/fieldDemo.ts):
//   1. D2-AC-53 (desktop, Tier S): none -> acoustic -> visual -> thermal -> radar -> none switch through the perception store,
//      every switch rebuilds the field at once (<= 300 ms asserted only under M06_PERF=1) and compiles no program; the field
//      draws at most two draws, within the Tier S budget (3072 points, 32 circles); the pixels it changes lean to no hue;
//      volumes (all, selected), the physical reference, the readout-row highlight and the governor levels 1-4.
//   2. D2-AC-49 (quality rows): phone 390 x 844 at DPR 3 and tablet 1180 x 820 at DPR 2 with touch and an Adreno renderer
//      string (deviceClass = mobile, Tier B by name): form factor, profile, DPR <= 2 and the drawing-buffer pixel limit,
//      the phone's 30 fps, caps (low-poly, hero, labels), start rung, the field budget.
//   3. D2-AC-48 (viewport part): phone touch (CDP touch events, Tier S): one-finger rotation, pinch zoom, two-finger pan,
//      twist, long press (500 ms) -> 'viewport.longpress', tap selects a vehicle, double tap focuses; the page neither
//      scrolls nor zooms.
//   4. D2-AC-44 (viewport part): suspension renders nothing (frame count still for 2 s), requestFrame renders one frame,
//      wake-on-input renders while dragging and stops within 2.5 s, resuming restarts.
//   5. FPV drag (M15-FR-218 interface, request d2-WP-12b-to-M06 item 5) and the touch edge band (AWR-04 §15.16): in the
//      FPV camera a one-finger touch drag and a mouse drag emit fpv.drag start, move, end with the angles of the FPV
//      projection and select nothing; outside FPV nothing is emitted; a touch that starts within 16 px of the viewport
//      edge moves no camera.
import { expect, test, type Browser, type Page } from '@playwright/test'
import { BOOT_MS, decodePng, firstVehicle, frames, openWorld, PERF, perf, watch, type Img, type VpHooks } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

const Q = 'source=fake&fakeWorld=synthcity&fakeN=6&chrome=0'
const SWITCH_MS = 300
const ADRENO = 'ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)'

type FieldStats = { kind: string; uavAnchors: number; bodyAnchors: number; points: number; inShell: number; circles: number; segments: number; pMin: number; pMax: number }
type H = VpHooks & {
  __vp: VpHooks['__vp'] & {
    selected(): string[]
    field: {
      stats(): FieldStats | null
      renderer(): { mode: string; rebuilds: number; lastSwitchMs: number; level: number; budget: { totalPoints: number; circles: number }; visible: boolean; draws: number;
        anchors: { kind: string; id: string; points: number }[] } | null
      view(kind: string, o?: { volumes?: string; physicalRef?: boolean }): Promise<string>
      targets(n: number, select?: boolean): Promise<string[]>
      highlight(id: string | null): void
      setLevel(l: number | null): void
    }
    quality(): { formFactor: string; mobile: boolean; profile: Record<string, unknown> | null; pointSizeMode: string; dpr: number | null; drawingBuffer: [number, number] | null
      qualityFps: number; startRung: number; lowCap: number | null; heroCap: number | null; labelsCap: number | null }
    loop: { suspend(on: boolean): void; requestFrame(): void; wakeOnInput(on: boolean): void; stats(): { frameNo: number; suspended: boolean; suspendedFrames: number } }
    touch: { longPresses(): { x: number; y: number }[] }
    fpvDrags(clear?: boolean): { dxRad: number; dyRad: number; phase: string }[]
    fpvRadPerPx(): [number, number]
  }
}

function glWatch(page: Page): string[] {
  const gl: string[] = []
  page.on('console', (m) => {
    const t = m.text()
    if ((m.type() === 'warning' || m.type() === 'error') && /GL_INVALID|WebGL: |M06-E00[67]|THREE\./.test(t)) gl.push(t.slice(0, 240))
  })
  return gl
}

async function shot(page: Page): Promise<Img> {
  await frames(page, 4)
  return decodePng(await page.screenshot())
}
/** pixels changed between two screenshots and the largest red lean (r - max(g, b)) among them */
function changed(a: Img, b: Img): { n: number; redLean: number } {
  let n = 0
  let lean = 0
  for (let i = 0; i < a.px.length; i += 4) {
    const d = Math.max(Math.abs(a.px[i] - b.px[i]), Math.abs(a.px[i + 1] - b.px[i + 1]), Math.abs(a.px[i + 2] - b.px[i + 2]))
    if (d <= 12) continue
    n++
    lean = Math.max(lean, b.px[i] - Math.max(b.px[i + 1], b.px[i + 2]))
  }
  return { n, redLean: lean }
}

/** switch the view and wait until the renderer rebuilt for it; returns ms from the call to the rebuild and the program delta */
async function view(page: Page, kind: string, o: { volumes?: string; physicalRef?: boolean } = {}): Promise<{ ms: number; dp: number }> {
  return page.evaluate(async ([k, oo]) => {
    const w = window as unknown as H & { __perf: { gpu: { programs: number } } }
    const p0 = w.__perf.gpu.programs
    const t0 = performance.now()
    await w.__vp.field.view(k, oo)
    for (let i = 0; i < 600; i++) {
      const r = w.__vp.field.renderer()
      const s = w.__vp.field.stats()
      if (r && s && s.kind === k && r.lastSwitchMs >= t0) return { ms: r.lastSwitchMs - t0, dp: w.__perf.gpu.programs - p0 }
      await new Promise((ok) => requestAnimationFrame(() => ok(null)))
    }
    return { ms: Number.POSITIVE_INFINITY, dp: w.__perf.gpu.programs - p0 }
  }, [kind, o] as const)
}

test('D2-AC-53 field views, volumes and governor levels (Tier S, desktop)', async ({ page }) => {
  test.setTimeout(360_000)
  const w = watch(page)
  const gl = glWatch(page)
  await openWorld(page, srv!.url, Q, 'synthcity', Math.max(BOOT_MS, 120_000))
  const id = await firstVehicle(page)
  expect(await page.evaluate(() => (window as unknown as H).__vp.field.renderer()?.mode)).toBe('glpoint')
  const programs0 = await perf<number>(page, 'gpu.programs')
  const compiled0 = await perf<number>(page, 'gpu.compiledAfterReveal')
  // look down at the first vehicle from 250 m
  const p = (await page.evaluate((i) => (window as unknown as H).__vp.dronePose(i), id))!
  await page.evaluate(([x, y, z]) => (window as unknown as H).__vp.vpSession.rig!.lookAtEnu([x, y - 220, z + 160], [x, y, z]), p)
  await frames(page, 20)
  // the PerfGovernor's field knob (step 1) degrades first on a loaded SwiftShader: pin level 0 for the measurements
  await page.evaluate(() => (window as unknown as H).__vp.field.setLevel(0))
  const base = await shot(page)
  const times: Record<string, number> = {}
  for (const k of ['acoustic', 'visual', 'thermal', 'radar']) {
    const r = await view(page, k)
    times[k] = Math.round(r.ms)
    expect(r.ms, k).toBeLessThan(5000)
    if (PERF) expect(r.ms, k).toBeLessThanOrEqual(SWITCH_MS)
    expect(r.dp, k).toBe(0)
    const st = (await page.evaluate(() => (window as unknown as H).__vp.field.stats()))!
    const rr = (await page.evaluate(() => (window as unknown as H).__vp.field.renderer()))!
    expect(st.uavAnchors, k).toBeGreaterThanOrEqual(1)
    expect(st.points, k).toBeGreaterThan(50)
    expect(st.points, k).toBeLessThanOrEqual(3072)
    expect(st.circles, k).toBeLessThanOrEqual(32)
    expect(rr.draws, k).toBeGreaterThanOrEqual(1)
    expect(rr.draws, k).toBeLessThanOrEqual(2)
    const img = await shot(page)
    const c = changed(base, img)
    expect(c.n, k).toBeGreaterThan(200)
    expect(c.redLean, k).toBeLessThanOrEqual(8)
  }
  test.info().annotations.push({ type: 'switch-ms', description: JSON.stringify(times) })
  // volumes of two synthetic targets: all, physical reference, selected
  const ids = await page.evaluate(() => (window as unknown as H).__vp.field.targets(2))
  expect(ids.length).toBe(2)
  await view(page, 'acoustic', { volumes: 'all' })
  await page.waitForFunction(() => ((window as unknown as H).__vp.field.stats()?.bodyAnchors ?? 0) >= 2, null, { timeout: 20_000 })
  let st = (await page.evaluate(() => (window as unknown as H).__vp.field.stats()))!
  expect(st.bodyAnchors).toBeLessThanOrEqual(6)
  expect(st.pMax).toBeLessThanOrEqual(1)
  expect(st.pMin).toBeGreaterThanOrEqual(0)
  // the selected target's acoustic body sets the vehicles' solid shells
  expect(st.inShell).toBeGreaterThanOrEqual(0)
  await view(page, 'acoustic', { volumes: 'selected', physicalRef: true })
  await page.waitForFunction((t) => {
    const r = (window as unknown as H).__vp.field.renderer()
    return !!r && r.anchors.filter((a) => a.kind === 'body').length > 0 && r.anchors.filter((a) => a.kind === 'body').every((a) => a.id === t)
  }, ids[0], { timeout: 20_000 })
  await page.evaluate((i) => (window as unknown as H).__vp.field.highlight(i), id)
  await frames(page, 5)
  // governor levels: half the points, selected bodies only, no dashed shells, off (each a rebuild)
  const rebuilt = async (fn: () => Promise<void>): Promise<FieldStats> => {
    const r0 = (await page.evaluate(() => (window as unknown as H).__vp.field.renderer()))!.rebuilds
    await fn()
    await page.waitForFunction((n) => ((window as unknown as H).__vp.field.renderer()?.rebuilds ?? 0) > n, r0, { timeout: 20_000 })
    return (await page.evaluate(() => (window as unknown as H).__vp.field.stats()))!
  }
  const level = (l: number): Promise<FieldStats> => rebuilt(() => page.evaluate((x) => (window as unknown as H).__vp.field.setLevel(x), l))
  const full = await rebuilt(() => page.evaluate(() => (window as unknown as H).__vp.field.highlight(null)))
  const halfSt = await level(1)
  expect(halfSt.points).toBeLessThanOrEqual(Math.ceil(full.points / 2) + 32)
  expect(halfSt.points).toBeGreaterThan(0)
  const noDash = await level(3)
  expect(noDash.circles).toBeLessThanOrEqual(full.circles)
  await level(4)
  await frames(page, 3)
  expect((await page.evaluate(() => (window as unknown as H).__vp.field.renderer()))!.draws).toBe(0)
  await level(0)
  await page.evaluate(() => (window as unknown as H).__vp.field.setLevel(null))
  const off = await view(page, 'none', { volumes: 'off' })
  expect(off.dp).toBe(0)
  await frames(page, 5)
  expect((await page.evaluate(() => (window as unknown as H).__vp.field.renderer()))!.draws).toBe(0)
  expect(await perf<number>(page, 'gpu.programs')).toBe(programs0)
  expect(await perf<number>(page, 'gpu.compiledAfterReveal')).toBe(compiled0)
  expect(await perf<number>(page, 'gpu.planMismatches')).toBe(0)
  expect(gl, gl.join('\n')).toEqual([])
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

async function mobilePage(browser: Browser, o: { w: number; h: number; dpr: number; renderer: string | null }): Promise<Page> {
  const ctx = await browser.newContext({ viewport: { width: o.w, height: o.h }, deviceScaleFactor: o.dpr, isMobile: true, hasTouch: true })
  const page = await ctx.newPage()
  if (o.renderer) {
    await page.addInitScript((r) => {
      const P = WebGL2RenderingContext.prototype
      const gp = Object.getOwnPropertyDescriptor(P, 'getParameter')!.value as WebGL2RenderingContext['getParameter']
      P.getParameter = function (this: WebGL2RenderingContext, n: number) {
        return n === 0x9246 ? r : gp.call(this, n)
      } as typeof P.getParameter
    }, o.renderer)
  }
  return page
}

for (const dev of [
  { ff: 'phone', w: 390, h: 844, dpr: 3, mp: 1.0e6, fps: 30, low: 32, hero: 1, labels: 8, rung: 2, points: 2048 },
  { ff: 'tablet', w: 1180, h: 820, dpr: 2, mp: 2.0e6, fps: 0, low: 64, hero: 2, labels: 16, rung: 3, points: 4096 },
] as const) {
  test(`D2-AC-49 quality profile: ${dev.ff} ${dev.w} x ${dev.h} DPR ${dev.dpr}, mobile GPU`, async ({ browser }) => {
    test.setTimeout(420_000)
    const page = await mobilePage(browser, { ...dev, renderer: ADRENO })
    const w = watch(page)
    const gl = glWatch(page)
    await openWorld(page, srv!.url, Q, 'synthcity', Math.max(BOOT_MS, 240_000))
    const q = await page.evaluate(() => (window as unknown as H).__vp.quality())
    expect(q.formFactor).toBe(dev.ff)
    expect(q.mobile).toBe(true)
    expect(q.profile?.formFactor).toBe(dev.ff)
    expect(q.dpr!).toBeLessThanOrEqual(2.01)
    expect(q.drawingBuffer![0] * q.drawingBuffer![1]).toBeLessThanOrEqual(dev.mp * 1.02)
    expect(q.qualityFps).toBe(dev.fps)
    expect(q.startRung).toBeLessThanOrEqual(dev.rung)
    expect(q.lowCap!).toBeLessThanOrEqual(dev.low)
    expect(q.heroCap!).toBeLessThanOrEqual(dev.hero)
    if (q.labelsCap !== null) expect(q.labelsCap).toBeLessThanOrEqual(dev.labels)
    expect((await page.evaluate(() => (window as unknown as H).__vp.field.renderer()))!.budget.totalPoints).toBe(dev.points)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    expect(gl, gl.join('\n')).toEqual([])
    expect(w.errors).toEqual([])
    await page.context().close()
  })
}

/** CDP touch input in CSS px of the page */
async function touches(page: Page): Promise<(type: 'touchStart' | 'touchMove' | 'touchEnd', pts: [number, number][]) => Promise<void>> {
  const cdp = await page.context().newCDPSession(page)
  return async (type, pts) => {
    await cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], i) => ({ x, y, id: i, radiusX: 1, radiusY: 1, force: 1 })) })
  }
}

test('D2-AC-48 touch gestures on the viewport (phone, Tier S)', async ({ browser }) => {
  test.setTimeout(360_000)
  const page = await mobilePage(browser, { w: 390, h: 844, dpr: 2, renderer: null })
  const w = watch(page)
  await openWorld(page, srv!.url, `${Q.replace('fakeN=6', 'fakeN=4')}&fakeGround=1`, 'synthcity', Math.max(BOOT_MS, 120_000))
  const id = await firstVehicle(page)
  const t = await touches(page)
  const pose = async (): Promise<{ eye: number[]; tgt: number[] }> => {
    const c = (await page.evaluate(() => (window as unknown as H).__vp.camera()))!
    return { eye: c.pose.eye_enu_m, tgt: c.pose.target_enu_m }
  }
  const p = (await page.evaluate((i) => (window as unknown as H).__vp.dronePose(i), id))!
  await page.evaluate(([x, y, z]) => (window as unknown as H).__vp.vpSession.rig!.lookAtEnu([x, y - 120, z + 90], [x, y, z]), p)
  await frames(page, 30)
  const cx = 195
  const cy = 420
  const azOf = (q: { eye: number[]; tgt: number[] }): number => Math.atan2(q.eye[1] - q.tgt[1], q.eye[0] - q.tgt[0])
  const dist = (q: { eye: number[]; tgt: number[] }): number => Math.hypot(q.eye[0] - q.tgt[0], q.eye[1] - q.tgt[1], q.eye[2] - q.tgt[2])
  // one finger: rotation
  let a = await pose()
  await t('touchStart', [[cx, cy]])
  for (let k = 1; k <= 10; k++) await t('touchMove', [[cx + 12 * k, cy]])
  await t('touchEnd', [])
  await frames(page, 40)
  let b = await pose()
  expect(Math.abs(azOf(b) - azOf(a))).toBeGreaterThan(0.2)
  // pinch out: closer
  a = b
  await t('touchStart', [[cx - 40, cy], [cx + 40, cy]])
  for (let k = 1; k <= 10; k++) await t('touchMove', [[cx - 40 - 6 * k, cy], [cx + 40 + 6 * k, cy]])
  await t('touchEnd', [])
  await frames(page, 40)
  b = await pose()
  expect(dist(b)).toBeLessThan(dist(a) * 0.8)
  // two-finger pan: the target moves
  a = b
  await t('touchStart', [[cx - 40, cy], [cx + 40, cy]])
  for (let k = 1; k <= 10; k++) await t('touchMove', [[cx - 40, cy + 8 * k], [cx + 40, cy + 8 * k]])
  await t('touchEnd', [])
  await frames(page, 40)
  b = await pose()
  expect(Math.hypot(b.tgt[0] - a.tgt[0], b.tgt[1] - a.tgt[1], b.tgt[2] - a.tgt[2])).toBeGreaterThan(1)
  // twist: yaw
  a = b
  await t('touchStart', [[cx - 60, cy], [cx + 60, cy]])
  for (let k = 1; k <= 10; k++) {
    const an = (k * 4 * Math.PI) / 180
    await t('touchMove', [[cx - 60 * Math.cos(an), cy - 60 * Math.sin(an)], [cx + 60 * Math.cos(an), cy + 60 * Math.sin(an)]])
  }
  await t('touchEnd', [])
  await frames(page, 40)
  b = await pose()
  expect(Math.abs(azOf(b) - azOf(a))).toBeGreaterThan(0.2)
  // long press
  await t('touchStart', [[cx, cy + 100]])
  await page.waitForTimeout(700)
  await t('touchEnd', [])
  const lp = await page.evaluate(() => (window as unknown as H).__vp.touch.longPresses())
  expect(lp.length).toBe(1)
  // tap on the vehicle selects it
  await page.evaluate(([x, y, z]) => (window as unknown as H).__vp.vpSession.rig!.lookAtEnu([x, y - 40, z + 30], [x, y, z]), p)
  await frames(page, 40)
  const s = (await page.evaluate(([x, y, z]) => (window as unknown as H).__vp.project(x, y, z), p))!
  const r = (await page.evaluate(() => (window as unknown as H).__vp.viewportRect()))!
  await t('touchStart', [[r.x + s[0], r.y + s[1]]])
  await t('touchEnd', [])
  await page.waitForFunction((i) => (window as unknown as H).__vp.selected().includes(i), id, { timeout: 10_000 })
  // double tap on the ground: the camera flies (orbit target moves). The two taps go back to back (the recogniser wants the
  // second within 300 ms of the first by event time); a loaded machine can stretch CDP dispatch past that, so up to 3 tries
  let flew = false
  for (let k = 0; k < 3 && !flew; k++) {
    a = await pose()
    for (let n = 0; n < 2; n++) {
      await t('touchStart', [[r.x + 60, r.y + 700]])
      await t('touchEnd', [])
    }
    flew = await page.waitForFunction((t0) => {
      const c = (window as unknown as H).__vp.camera()
      return !!c && Math.hypot(c.pose.target_enu_m[0] - t0[0], c.pose.target_enu_m[1] - t0[1]) > 1
    }, a.tgt, { timeout: 8_000 }).then(() => true, () => false)
    await page.waitForTimeout(400)
  }
  expect(flew).toBe(true)
  // no page scroll or zoom
  expect(await page.evaluate(() => [window.scrollX, window.scrollY, window.visualViewport?.scale ?? 1])).toEqual([0, 0, 1])
  expect(w.errors).toEqual([])
  await page.context().close()
})

test('FPV drag (fpv.onDrag) and the touch edge band (phone, Tier S)', async ({ browser }) => {
  test.setTimeout(360_000)
  const page = await mobilePage(browser, { w: 390, h: 844, dpr: 2, renderer: null })
  const w = watch(page)
  await openWorld(page, srv!.url, `${Q.replace('fakeN=6', 'fakeN=4')}&fakeGround=1`, 'synthcity', Math.max(BOOT_MS, 120_000))
  const id = await firstVehicle(page)
  const t = await touches(page)
  const pose = async (): Promise<number[]> => {
    const c = (await page.evaluate(() => (window as unknown as H).__vp.camera()))!
    return [...c.pose.eye_enu_m, ...c.pose.target_enu_m]
  }
  const drags = (clear = false): Promise<{ dxRad: number; dyRad: number; phase: string }[]> =>
    page.evaluate((c) => (window as unknown as H).__vp.fpvDrags(c), clear)
  const r = (await page.evaluate(() => (window as unknown as H).__vp.viewportRect()))!
  const cx = Math.round(r.x + r.w / 2)
  const cy = Math.round(r.y + r.h / 2)
  // orbit: a drag rotates the camera and emits no FPV drag; a drag from the 16 px edge band moves nothing
  const p = (await page.evaluate((i) => (window as unknown as H).__vp.dronePose(i), id))!
  await page.evaluate(([x, y, z]) => (window as unknown as H).__vp.vpSession.rig!.lookAtEnu([x, y - 120, z + 90], [x, y, z]), p)
  await frames(page, 30)
  await drags(true)
  let a = await pose()
  await t('touchStart', [[r.x + 6, cy]])
  for (let k = 1; k <= 10; k++) await t('touchMove', [[r.x + 6 + 12 * k, cy]])
  await t('touchEnd', [])
  await frames(page, 30)
  expect(await pose()).toEqual(a)
  await t('touchStart', [[cx - 60, cy]])
  for (let k = 1; k <= 10; k++) await t('touchMove', [[cx - 60 + 12 * k, cy]])
  await t('touchEnd', [])
  await frames(page, 30)
  expect(await pose()).not.toEqual(a)
  expect(await drags()).toEqual([])
  // FPV on the vehicle
  await page.evaluate((i) => (window as unknown as H).__vp.select([i]), id)
  const m = await page.evaluate(() => (window as unknown as H).__vp.setMode('fpv'))
  expect(m.ok, `fpv: ${m.reason}`).toBe(true)
  await frames(page, 10)
  const k0 = await page.evaluate(() => (window as unknown as H).__vp.fpvRadPerPx())
  expect(k0[0]).toBeGreaterThan(0)
  expect(k0[1]).toBeGreaterThan(0)
  // one-finger touch drag: start, moves, end; 4 steps of 30 px after the 6 px threshold step
  await t('touchStart', [[cx - 60, cy]])
  for (let k = 1; k <= 5; k++) await t('touchMove', [[cx - 60 + 30 * k, cy]])
  await t('touchEnd', [])
  let d = await drags(true)
  expect(d.map((x) => x.phase)).toEqual(['start', 'move', 'move', 'move', 'end'])
  expect(d.reduce((s, x) => s + x.dxRad, 0)).toBeCloseTo(120 * k0[0], 6)
  expect(Math.abs(d.reduce((s, x) => s + x.dyRad, 0))).toBeLessThan(1e-9)
  // the canvas keeps touch-action none in FPV (camera-controls clears its inline value there): a pinch neither scrolls
  // nor zooms the page and the browser cancels no pointer
  await t('touchStart', [[cx - 40, cy], [cx + 40, cy]])
  for (let k = 1; k <= 8; k++) await t('touchMove', [[cx - 40 - 8 * k, cy + 4 * k], [cx + 40 + 8 * k, cy + 4 * k]])
  await t('touchEnd', [])
  expect(await page.evaluate(() => [window.scrollX, window.scrollY, window.visualViewport?.scale ?? 1])).toEqual([0, 0, 1])
  expect(await page.evaluate(() => getComputedStyle(document.querySelector('[data-viewport]')!).touchAction)).toBe('none')
  expect(await drags(true)).toEqual([])
  // mouse drag: the first event carries the motion since the press; no click (the selection stays)
  const sel = await page.evaluate(() => (window as unknown as H).__vp.selected())
  await page.mouse.move(cx, cy - 40)
  await page.mouse.down()
  await page.mouse.move(cx, cy - 20, { steps: 2 })
  await page.mouse.move(cx, cy + 10, { steps: 3 })
  await page.mouse.up()
  d = await drags(true)
  expect(d[0].phase).toBe('start')
  expect(d.at(-1)!.phase).toBe('end')
  expect(d.slice(1, -1).every((x) => x.phase === 'move')).toBe(true)
  expect(d.reduce((s, x) => s + x.dyRad, 0)).toBeCloseTo(50 * k0[1], 6)
  expect(await page.evaluate(() => (window as unknown as H).__vp.selected())).toEqual(sel)
  // a plain click in FPV is no drag
  await page.mouse.click(cx, cy)
  expect(await drags()).toEqual([])
  await page.evaluate(() => (window as unknown as H).__vp.setMode('orbit'))
  expect(w.errors).toEqual([])
  await page.context().close()
})

test('D2-AC-44 power save on the viewport: suspended, one frame, wake on input, resume', async ({ page }) => {
  test.setTimeout(240_000)
  await openWorld(page, srv!.url, Q, 'synthcity', Math.max(BOOT_MS, 120_000))
  const count = (): Promise<number> => perf<number>(page, 'frame.count')
  await page.evaluate(() => (window as unknown as H).__vp.loop.suspend(true))
  await page.waitForTimeout(300)
  const c0 = await count()
  await page.waitForTimeout(2000)
  expect(await count()).toBe(c0)
  const s0 = (await page.evaluate(() => (window as unknown as H).__vp.loop.stats())).suspendedFrames
  await page.evaluate(() => (window as unknown as H).__vp.loop.requestFrame())
  await page.waitForTimeout(300)
  expect((await page.evaluate(() => (window as unknown as H).__vp.loop.stats())).suspendedFrames).toBe(s0 + 1)
  // static browsing: a drag renders frames, which stop within 2.5 s of the last input
  await page.evaluate(() => (window as unknown as H).__vp.loop.wakeOnInput(true))
  const r = (await page.evaluate(() => (window as unknown as H).__vp.viewportRect()))!
  await page.mouse.move(r.x + r.w / 2, r.y + r.h / 2)
  await page.mouse.down()
  for (let k = 1; k <= 10; k++) await page.mouse.move(r.x + r.w / 2 + 10 * k, r.y + r.h / 2)
  await page.mouse.up()
  await page.waitForTimeout(200)
  const s1 = (await page.evaluate(() => (window as unknown as H).__vp.loop.stats())).suspendedFrames
  expect(s1).toBeGreaterThan(s0 + 2)
  await page.waitForTimeout(3000)
  const s2 = (await page.evaluate(() => (window as unknown as H).__vp.loop.stats())).suspendedFrames
  await page.waitForTimeout(1000)
  expect((await page.evaluate(() => (window as unknown as H).__vp.loop.stats())).suspendedFrames).toBe(s2)
  await page.evaluate(() => {
    const vp = (window as unknown as H).__vp
    vp.loop.wakeOnInput(false)
    vp.loop.suspend(false)
  })
  const c1 = await count()
  await frames(page, 10)
  expect(await count()).toBeGreaterThan(c1 + 5)
})
