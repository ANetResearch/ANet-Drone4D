// D1-AC-14 / M06-AC-001, AC-002, AC-016 (PERF-AC-014; g01 §3, §9): the 28-item feature matrix + PointPool of the
// regression page equal the classic-path expectations of g01 §3 item by item on Tier B and Tier S (?tier=B, ?tier=S
// test switch), __perf.forced.tier records the switch, and every rendered frame issues exactly the pass plan
// (render.calls == passPlan). Tier A (P1; AWR-18 §2.4 item 1): with the C2 flag set (SwiftShader WebGPU, fallback
// adapter) the regression page runs the 28 items + PointPool on a WebGPURenderer and equals the WebGPU column of g01
// (featMatrix.expected.ts WGPU, including the expected unavailable items); the product page under ?tier=A&allowFallback=1
// is the R3F smoke: no pageerror, __perf.forced.tier = 'A', the classic fallback (M06-E013, ADR-044: no Tier A product
// backend in D1) keeps the pass plan. Without WebGPU (C1 flags) the Tier A test skips.
// Functional case: no timing assertion. Registered for the M16 harness in perf/harness/cases/ui.mjs (feat-matrix.{B,S,A}).
import { expect, test } from '@playwright/test'
import { diffMatrix, WGPU } from './featMatrix.expected'
import { frames, openWorld, perf, watch, type VpHooks } from './common'
import { startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

for (const tier of ['S', 'B'] as const) {
  test(`feature matrix and pass plan on Tier ${tier}`, async ({ page }) => {
    const w = watch(page)
    await openWorld(page, srv!.url, `source=fake&fakeN=5&tier=${tier}`)
    const info = await page.evaluate(() => (window as unknown as VpHooks).__vp.backendInfo())
    expect(info).toMatchObject({ tier, deviceClass: 'software', state: 'READY' })
    expect(await perf<string>(page, 'meta.tier')).toBe(tier)
    expect(await perf<{ tier?: string } | null>(page, 'forced')).toMatchObject({ tier })
    // pass plan on 30 consecutive frames (FakeSource vehicles moving, labels, trails)
    const plan = await page.evaluate(async () => {
      const p = (window as unknown as { __perf: { gpu: { calls: number; passPlan: number; planMismatches: number } } }).__perf
      const out: [number, number][] = []
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => requestAnimationFrame(() => r(null)))
        out.push([p.gpu.calls, p.gpu.passPlan])
      }
      return { out, mismatches: p.gpu.planMismatches }
    })
    for (const [calls, want] of plan.out) expect(calls).toBe(want)
    expect(plan.mismatches).toBe(0)
    // the regression page: 28 items + PointPool on this tier's backend path (own renderer, own canvas)
    const m = await page.evaluate((t) => (window as unknown as VpHooks).__vp.featMatrix({ tier: t }), tier)
    expect(m.tier).toBe(tier)
    expect(Object.keys(m.tests).length).toBe(29)
    expect(diffMatrix(m.tests)).toEqual([])
    await frames(page, 10)
    expect(w.errors).toEqual([])
    expect(w.m06).toEqual([])
  })
}

const CHROME = process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`
const C2 = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-unsafe-webgpu',
  '--enable-features=Vulkan', '--use-webgpu-adapter=swiftshader']

// Tier A always runs on its own browser with the C2 flag set (launchOptions cannot change per describe group)
test('feature matrix and pass plan on Tier A', async ({ playwright }) => {
  const browser = await playwright.chromium.launch({ executablePath: CHROME, args: C2, headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 })
    const w = watch(page)
    await openWorld(page, srv!.url, 'source=fake&fakeN=5&tier=A&allowFallback=1')
    expect(await perf<{ tier?: string } | null>(page, 'forced')).toMatchObject({ tier: 'A' })
    // D1 has no Tier A product backend (ADR-044, M06-E013): the page renders on the classic path with its pass plan
    const info = await page.evaluate(() => (window as unknown as VpHooks).__vp.backendInfo())
    expect(info).toMatchObject({ state: 'READY' })
    const plan = await page.evaluate(async () => {
      const p = (window as unknown as { __perf: { gpu: { calls: number; passPlan: number; planMismatches: number } } }).__perf
      const out: [number, number][] = []
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => requestAnimationFrame(() => r(null)))
        out.push([p.gpu.calls, p.gpu.passPlan])
      }
      return { out, mismatches: p.gpu.planMismatches }
    })
    for (const [calls, want] of plan.out) expect(calls).toBe(want)
    expect(plan.mismatches).toBe(0)
    const m = await page.evaluate(() => (window as unknown as VpHooks).__vp.featMatrix({ tier: 'A' }))
    test.skip(m.backend === 'unavailable', 'WebGPU unavailable: Tier A needs SwiftShader WebGPU (C2 flag set)')
    expect(m.backend).toBe('wgpu')
    expect(m.tier).toBe('A')
    expect(Object.keys(m.tests).length).toBe(29)
    expect(diffMatrix(m.tests, WGPU)).toEqual([])
    // GLSL ShaderMaterial is reported as incompatible by the node builder, not rendered
    expect(String((m.tests.points_glsl_ShaderMaterial as { errors?: string[] }).errors ?? '')).toMatch(/not compatible/)
    await frames(page, 10)
    expect(w.errors.filter((e) => !/WebGPU is not available/i.test(e))).toEqual([])
    expect(w.m06).toEqual([])
  } finally {
    await browser.close()
  }
})
