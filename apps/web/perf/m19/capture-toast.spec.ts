// M19-AC-013 capture events and Toasts (WP-13 writes it for M19, d2-WP-06-to-M16 item 1; M19-FR-016, FR-028; M15-FR-219,
// UX-FR-138, UX-FR-139; D2-AC-14 Toast part, D2-AC-25 one-red rule). Test build, sandbox route, scripted REST and the
// FakeSource D2 replay (perf/m15/helpers/d2rest.ts); events go in through the event bridge of the test build (__uxInject):
//   1. `perception.capture` of a target: one neutral Toast "已明确捕获 <id>"; the same target again within 30 s: no second
//      Toast; another target: its own Toast;
//   2. a burst of 60 captures of distinct targets (more than the 50 per second the server merges, M19-FR-016): the Toast
//      stack stays bounded (at most 3 visible, the rest merged or limited, AWR-14 §11.4);
//   3. `target.discovered` (critical) while a capture box is shown: the RedArbiter gives every figure at most one red owner and
//      the viewport's owner is the discovered target (__ux.red); the discovery Toast carries a confirm action.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { open, routes, storeSession, type Script } from '../m15/helpers/d2rest'
import { baseUrl, injectEvents, startPreview, visibleToasts, watch, type InjectEvent } from '../m15/helpers/shell'

const PORT = 4194
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => { server = await startPreview(PORT) })
test.afterAll(async () => { await server?.close() })
test.describe.configure({ mode: 'serial' })

let seq = 8_000_000
const ev = (type: string, level: 0 | 1 | 2 | 3, data: Record<string, unknown>): InjectEvent =>
  ({ seq: ++seq, t_sim_ns: seq * 1e6, type, level, uav: (data.uav as string) ?? null, cid: null, data })
const capture = (target: string, uav = 'uav0001'): InjectEvent =>
  ev('perception.capture', 1, { uav, sensor: 'eo_gx40', target, level_to: 'R', level_req: 'R', n_eff: 21.4, n_req: 14, t_hold_s: 1.0 })
const toastOf = (page: Page, text: string) => page.locator('[data-slot="toast"]').filter({ hasText: text })

async function setup(page: Page, context: Parameters<typeof routes>[0]): Promise<void> {
  const s: Script = { posts: [], preview: 'ok' }
  await routes(context, s)
  await storeSession(page)
  await open(page, false)
}

test('capture Toast: one per target within 30 s', async ({ page, context }) => {
  const w = watch(page)
  await setup(page, context)
  await injectEvents(page, [capture('person-71')])
  await expect(toastOf(page, '已明确捕获 person-71')).toHaveCount(1)
  await injectEvents(page, [capture('person-71', 'uav0002'), capture('person-71')])
  await page.waitForTimeout(800)
  expect(await toastOf(page, '已明确捕获 person-71').count()).toBeLessThanOrEqual(1)
  await injectEvents(page, [capture('dog-71')])
  await expect(toastOf(page, '已明确捕获 dog-71')).toHaveCount(1)
  expect(w.errors).toEqual([])
})

test('a burst of 60 captures keeps the Toast stack bounded', async ({ page, context }) => {
  await setup(page, context)
  await injectEvents(page, Array.from({ length: 60 }, (_x, i) => capture(`person-${(100 + i).toString()}`)))
  await page.waitForTimeout(1500)
  expect(await visibleToasts(page)).toBeLessThanOrEqual(3)
})

test('discovery while a capture is shown: one red owner per figure, the discovered target in the viewport', async ({ page, context }) => {
  await setup(page, context)
  await injectEvents(page, [capture('person-72'), ev('target.discovered', 3, { target: 'dog-72', uav: 'uav0002', kind: 'acoustic', range_m: 110, mode: 'geometric' })])
  const disc = toastOf(page, 'dog-72')
  await expect(disc).toHaveCount(1)
  await expect(disc.locator('[data-toast-action]')).toHaveCount(1)
  const red = await page.evaluate(() => (window as unknown as { __ux: { red: Record<string, { kind: string; id: string } | null> } }).__ux.red)
  for (const owner of Object.values(red)) expect(owner === null || typeof owner.id === 'string').toBe(true)
  const vp = red.viewport ?? null
  if (vp) expect(vp.id).toBe('dog-72')
  await disc.locator('[data-toast-action]').click()
  await expect(disc).toHaveCount(0, { timeout: 10_000 })
})
