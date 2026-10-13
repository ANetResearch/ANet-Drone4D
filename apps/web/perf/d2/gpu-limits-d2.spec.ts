// D2 sandbox surface under the WebGL2 specification minimum (WP-13, M16; ADR-086, ADR-087; the new D2 layers of M18 targets
// and sensitivity volumes, M20 tasking geometry, the M06 sensor views). Test build on the M06 test server (dist and /worlds),
// sandbox REST scripted by perf/m15/helpers/d2rest.ts (tasks with plan geometry, targets, nests) and FakeSource replaying
// fixtures/d2/sandbox_t5.awrrt (TargetLite32 rows, perception, payload, nest, task and relay topics), viewport on.
// The emulation (an init script, the same rules as perf/m06/gpu-limits.spec.ts) reports MAX_UNIFORM_BUFFER_BINDINGS 24,
// 12 vertex / 12 fragment / 24 combined uniform blocks and MAX_UNIFORM_BLOCK_SIZE 16 384, refuses binding points past the
// limit with a console error and reports a program over a per-stage limit at its first use (a link failure on such a GPU).
// Steps: reveal, the targets entity list, the Dock tasks tab (plan geometry of the area guard and the relay stations), FPV
// of uav0001 and the three sensor views EO, night (illuminator on) and LWIR (white-hot and black-hot), back to orbit.
// Asserts: no [gpu-limits] or GL error and no page error, the limits reached the page, every program within them.
import { resolve } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { startM06Server, type M06Server } from '../m06/server'
import { dockTab, routes, storeSession, SID, type Script } from '../m15/helpers/d2rest'
import { isTestBuild } from '../m15/helpers/shell'
import { DIST, WEB } from './live'

interface Limits { bindings: number; vertex: number; fragment: number; combined: number; blockSize: number }
const MIN: Limits = { bindings: 24, vertex: 12, fragment: 12, combined: 24, blockSize: 16384 }
let srv: M06Server | null = null
test.use({ viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => { srv = await startM06Server(undefined, resolve(WEB, DIST)) })
test.afterAll(async () => { await srv?.close() })

function emulate(L: Limits): void {
  const P = WebGL2RenderingContext.prototype
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  const gp = own('getParameter')
  P.getParameter = function (this: WebGL2RenderingContext, n: number) {
    if (n === 0x8a2f) return L.bindings
    if (n === 0x8a2b) return L.vertex
    if (n === 0x8a2d) return L.fragment
    if (n === 0x8a2e) return L.combined
    if (n === 0x8a30) return L.blockSize
    return gp.call(this, n)
  } as typeof P.getParameter
  const bbb = own('bindBufferBase')
  P.bindBufferBase = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null) {
    if (t === 0x8a11 && i >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferBase(UNIFORM_BUFFER, ${i})`); return }
    bbb.call(this, t, i, b)
  }
  const bbr = own('bindBufferRange')
  P.bindBufferRange = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null, off: number, size: number) {
    if (t === 0x8a11 && i >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferRange(UNIFORM_BUFFER, ${i})`); return }
    bbr.call(this, t, i, b, off, size)
  }
  const ubb = own('uniformBlockBinding')
  P.uniformBlockBinding = function (this: WebGL2RenderingContext, p: WebGLProgram, bi: number, point: number) {
    if (point >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: uniformBlockBinding(${bi}, ${point})`); return }
    ubb.call(this, p, bi, point)
  }
  const stats = { programs: 0, maxV: 0, maxF: 0, maxBlockSize: 0 }
  ;(window as unknown as { __gpuLimits: unknown }).__gpuLimits = stats
  const checked = new WeakSet<WebGLProgram>()
  const up = own('useProgram')
  P.useProgram = function (this: WebGL2RenderingContext, p: WebGLProgram | null) {
    if (p && !checked.has(p)) {
      checked.add(p)
      if (this.getProgramParameter(p, this.LINK_STATUS)) {
        const n = this.getProgramParameter(p, this.ACTIVE_UNIFORM_BLOCKS) as number
        let v = 0
        let f = 0
        let size = 0
        for (let i = 0; i < n; i++) {
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_VERTEX_SHADER)) v++
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_FRAGMENT_SHADER)) f++
          size = Math.max(size, this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_DATA_SIZE) as number)
        }
        stats.programs++
        stats.maxV = Math.max(stats.maxV, v)
        stats.maxF = Math.max(stats.maxF, f)
        stats.maxBlockSize = Math.max(stats.maxBlockSize, size)
        if (v > L.vertex || f > L.fragment || v + f > L.combined) console.error(`[gpu-limits] GL_INVALID_OPERATION: program links ${v} / ${f} uniform blocks`)
        if (size > L.blockSize) console.error(`[gpu-limits] GL_INVALID_OPERATION: ${size} B uniform block`)
      }
    }
    up.call(this, p)
  }
}

async function frames(page: Page, n: number): Promise<void> {
  await page.evaluate((k) => new Promise<void>((ok) => {
    let i = 0
    const step = (): void => { if (++i >= k) ok(); else requestAnimationFrame(step) }
    requestAnimationFrame(step)
  }), n)
}

type Sv = { __vp: { sensorView: { set: (m: string, o?: unknown) => { ok: boolean }; setPalette: (p: string) => void; setIlluminators?: (x: unknown[]) => void } } }

test('sandbox surface with targets, tasking and sensor views under the WebGL2 minimum limits', async ({ page, context }) => {
  test.setTimeout(300_000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error' && /gpu-limits|GL_|WebGL|THREE/.test(m.text())) errors.push(m.text()) })
  await page.addInitScript(emulate, MIN)
  const s: Script = { posts: [], preview: 'ok' }
  await routes(context, s)
  await storeSession(page)
  await page.goto(`${srv!.url}/sandbox/${SID}?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 120_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build')
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 30_000 })
  await frames(page, 30)
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 30_000 })
  await dockTab(page, /任务/)
  await frames(page, 30)
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-drone-id="uav0001"]').first().click()
  for (const [v, pal] of [['eo', null], ['nir', null], ['lwir', 'white_hot'], ['lwir', 'black_hot']] as const) {
    const r = await page.evaluate(([vv, pp]) => {
      const sv = (window as unknown as Sv).__vp.sensorView
      sv.setIlluminators?.([{ on: true, divergenceRad: 0.4, rangeM: 150 }])
      const out = sv.set(vv, { enterFpv: true })
      if (pp) sv.setPalette(pp)
      return out
    }, [v, pal] as const)
    expect(r.ok, `${v}`).toBe(true)
    await frames(page, 15)
  }
  await page.evaluate(() => (window as unknown as Sv).__vp.sensorView.set('off'))
  await frames(page, 20)
  const stats = await page.evaluate(() => (window as unknown as { __gpuLimits: { programs: number; maxV: number; maxF: number; maxBlockSize: number } }).__gpuLimits)
  expect(stats.programs).toBeGreaterThan(3)
  expect(stats.maxV).toBeLessThanOrEqual(MIN.vertex)
  expect(stats.maxF).toBeLessThanOrEqual(MIN.fragment)
  expect(stats.maxBlockSize).toBeLessThanOrEqual(MIN.blockSize)
  expect(errors).toEqual([])
})
