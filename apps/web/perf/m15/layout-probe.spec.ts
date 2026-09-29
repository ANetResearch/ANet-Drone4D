// M15-AC-010 (test build part) and M15-AC-011; D1-AC-24 (UI part): the floating rails and the Dock never resize the
// canvas; the unobscured rect follows the formula of M15 §6.4.2 and is committed to the viewport only at commit points:
// never during a separator drag, once on release. Mod+B, \ and ` are pressed 10 times each; the three separators are
// dragged 5 times each. The drawing-buffer assertion needs the real viewport (M06): with `M15_UI_ONLY=1` the spec runs
// in UI-only mode (?viewport=off) and checks the commit counting and the formula only.
// The production-build gate with frame statistics (layout.spec.ts) belongs to M16 (AWR-18 §8.5).
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, canvasBuffer, isTestBuild, shot, startPreview, watch } from './helpers/shell'

const PORT = 4187
const UI_ONLY = process.env.M15_UI_ONLY === '1'
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT) })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})

type Ux = { __ux: { layout: { unobscured: { x: number; y: number; w: number; h: number }; unobscuredCommits: number } } }
const commits = (page: Page) => page.evaluate(() => (window as unknown as Ux).__ux.layout.unobscuredCommits)
const rect = (page: Page) => page.evaluate(() => ({ ...(window as unknown as Ux).__ux.layout.unobscured }))

/** M15 §6.4.2 from the DOM geometry of the rail hosts (expected value) */
async function expectedRect(page: Page): Promise<{ x: number; y: number; w: number; h: number }> {
  return page.evaluate(() => {
    const G = 8
    const W = innerWidth
    const H = innerHeight
    const host = (side: string) => document.querySelector<HTMLElement>(`[data-slot="rail-host"][data-side="${side}"]`)
    const open = (side: string) => host(side)?.dataset.state === 'open'
    const size = (side: string) => {
      const panel = host(side)?.querySelector<HTMLElement>(`[id="${side}-rail"]`)
      const r = panel?.getBoundingClientRect()
      return side === 'bottom' ? (r?.height ?? 0) : (r?.width ?? 0)
    }
    const x0 = open('left') ? G + size('left') + G : 0
    const x1 = open('right') ? W - (G + size('right') + G) : W
    const y0 = 44
    const y1 = H - 48 - (open('bottom') ? G + size('bottom') + G : 0)
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
  })
}

async function expectRect(page: Page): Promise<void> {
  const got = await rect(page)
  const want = await expectedRect(page)
  for (const k of ['x', 'y', 'w', 'h'] as const) expect(Math.abs(got[k] - want[k]), `unobscured.${k} ${got[k]} vs ${want[k]}`).toBeLessThanOrEqual(1.5)
}

async function drag(page: Page, handle: ReturnType<Page['locator']>, dx: number, dy: number): Promise<void> {
  const b = await handle.boundingBox()
  if (!b) throw new Error('separator not visible')
  const x = b.x + b.width / 2
  const y = b.y + b.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  for (let k = 1; k <= 8; k++) await page.mouse.move(x + (dx * k) / 8, y + (dy * k) / 8)
}

test('rails, Dock and separators never resize the canvas; the unobscured rect commits on release only', async ({ page }) => {
  const w = watch(page)
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto(`/world/shenzhen?source=fake&fakeN=1${UI_ONLY ? '&viewport=off&reveal=shell' : ''}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
  const buffer0 = UI_ONLY ? 'none' : await canvasBuffer(page)
  if (!UI_ONLY) expect(buffer0).not.toBe('none')
  await page.locator('[data-figure="header"]').click({ position: { x: 5, y: 5 } })
  for (const key of ['Control+KeyB', 'Backslash', 'Backquote']) {
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press(key)
      await page.waitForTimeout(120)
      if (!UI_ONLY) expect(await canvasBuffer(page)).toBe(buffer0)
    }
  }
  // 10 presses leave every host as it was; open the Dock for the separator drags
  if ((await page.locator('[data-slot="rail-host"][data-side="bottom"]').getAttribute('data-state')) !== 'open') await page.keyboard.press('Backquote')
  await page.waitForTimeout(600)
  await expectRect(page)
  for (const side of ['left', 'right', 'bottom'] as const) {
    const handle = page.locator(`[data-slot="rail-host"][data-side="${side}"] [data-slot="resizable-handle"]`)
    for (let i = 0; i < 5; i++) {
      const before = await commits(page)
      const d = i % 2 === 0 ? 40 : -40
      await drag(page, handle, side === 'bottom' ? 0 : side === 'left' ? d : -d, side === 'bottom' ? -d : 0)
      expect(await commits(page), `${side} drag ${i}: no commit while dragging`).toBe(before)
      await page.mouse.up()
      await page.waitForTimeout(250)
      expect(await commits(page), `${side} drag ${i}: one commit on release`).toBe(before + 1)
      if (!UI_ONLY) expect(await canvasBuffer(page)).toBe(buffer0)
    }
  }
  await expectRect(page)
  await shot(page, 'm15-layout-after-drags')
  expect(w.errors.filter((e) => !/loop task/.test(e))).toEqual([])
})
