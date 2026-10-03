// D1-AC-17 task editing (AWR-03 §8.4 D1-AC-17; AWR-14 §5.3, §6.8; UX-AC-040; M10-AC-025; M15-AC-016). Production build,
// free-shenzhen on a live backend (harness AWR_PERF_BASE, otherwise a ci-profile supervisor started here that serves
// AWR_WEB_DIST or apps/web/dist). Two parts, both through the UI only (no test hooks):
//   route  select p600-01, take off, open the route editor from the detail page; add three waypoints by clicking the
//          ground in the viewport ("add waypoint" tool), change their E, N and height in the table, insert one from the
//          row menu and delete it again with the Delete key, drag one in the viewport (select tool) and check the table
//          follows, undo and redo the drag (Mod+Z, Mod+Shift+Z); after the coarse check passes, submit: the follow_path
//          call must end succeeded (the editor closes on running, the detail page shows the route result).
//   area   reopen the editor, "draw area", Shift + drag a rectangle in the viewport, coverage generator (lawnmower) with a
//          10 m spacing, assign p600-02 (still on the ground: the mission takes it off), preview (server paths and energy
//          pre-check), create: the mission starts and ends DONE.
// No pageerror; every request the page makes answers below 500.
import { expect, test, type Page } from '@playwright/test'
import { startBackend } from './skeleton.server'

const HINT = 'MSIXTEENMISSIONEDITGATEA' // fixed principal: the first operator of a fresh backend holds the seat
const ID = 'p600-01'
const OTHER = 'p600-02'
const HOME = [-38, -94] as const // scenarios/free-shenzhen.json home of p600-01 (a flat pad)
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control'

let be: { url: string; close(): Promise<void> }
test.beforeAll(async () => {
  test.setTimeout(180_000)
  const base = process.env.AWR_PERF_BASE
  if (base) be = { url: base.replace(/\/$/, ''), close: async () => {} }
  else {
    const b = await startBackend(undefined, { AWR_SCENARIO: 'free-shenzhen' })
    be = { url: b.url, close: () => b.close() }
  }
})
test.afterAll(async () => {
  await be?.close()
})

/** GET with a viewer token of its own (R24 needs a principal; the page's token stays the page's) */
async function viewerGet(path: string): Promise<unknown> {
  const t = await fetch(`${be.url}/api/auth/token`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ role: 'viewer', principal_hint: 'MSIXTEENMISSIONEDITVIEWA' }),
  })
  const { token } = (await t.json()) as { token: string }
  return (await fetch(`${be.url}${path}`, { headers: { authorization: `Bearer ${token}` } })).json()
}

/** screen centre of the vehicle's label (the label sits a few px above the vehicle) */
async function labelCentre(page: Page, id: string): Promise<{ x: number; y: number }> {
  const box = await page.locator('[data-label]', { has: page.locator('span.id', { hasText: id }) }).first().boundingBox()
  expect(box, `label of ${id} on screen`).not.toBeNull()
  return { x: box!.x + box!.width / 2, y: box!.y + box!.height + 12 }
}

const cell = (page: Page, what: 'e' | 'n' | 'h', n: number) =>
  page.getByLabel(what === 'e' ? `航点 ${n} 东向坐标` : what === 'n' ? `航点 ${n} 北向坐标` : `航点 ${n} 高度`, { exact: true })

async function setCell(page: Page, what: 'e' | 'n' | 'h', n: number, v: number): Promise<void> {
  const c = cell(page, what, n)
  await c.fill(String(v))
  await c.press('Enter')
  await expect(c).toHaveValue(v.toFixed(1))
}

test('route editing ends with follow_path succeeded; an area becomes a finished coverage mission', async ({ page }) => {
  test.setTimeout(720_000)
  const errors: string[] = []
  const httpErrors: string[] = []
  page.on('pageerror', (e) => {
    if (!/WebGPU is not available/i.test(e.message)) errors.push(e.message)
  })
  page.on('response', (r) => {
    if (r.status() >= 500) httpErrors.push(`${r.status()} ${r.url()}`)
  })
  await page.addInitScript((h) => localStorage.setItem('awr.principal_hint', h), HINT)
  await page.goto(`${be.url}/world/shenzhen`)
  await page.waitForFunction(() => ((window as unknown as { __perf?: { load?: { revealAt: number } } }).__perf?.load?.revealAt ?? 0) > 0, null, { polling: 500, timeout: 180_000 })

  // ---------------------------------------------------------------- route
  const row = page.locator(`[data-drone-id="${ID}"]`)
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.click()
  const takeoff = page.locator('[data-cmd="takeoff"]')
  await expect(takeoff).toBeVisible()
  if (await takeoff.isEnabled()) {
    await takeoff.click()
    await page.locator('[data-cmd-confirm="takeoff"]').click()
    await expect(takeoff).toHaveAttribute('data-cmd-state', 'succeeded', { timeout: 90_000 })
  }
  // camera close to the vehicle (60 m sphere), so a few dozen pixels are a few metres
  await page.getByRole('button', { name: '聚焦选中' }).first().click()
  await page.waitForTimeout(2500)

  await page.locator('[data-edit-route]').click()
  const editor = page.locator('[data-mission-edit]')
  await expect(editor).toHaveAttribute('data-edit-phase', 'CLEAN')
  await page.locator('[data-edit-tool-item="add"]').click()
  await expect(editor).toHaveAttribute('data-edit-tool', 'add')
  // add: three clicks on the ground around the vehicle (ray_hit through the viewport)
  const c0 = await labelCentre(page, ID)
  const count = page.locator('[data-route-count]')
  for (const [k, [dx, dy]] of ([[-70, 40], [70, 40], [70, 110]] as const).entries()) {
    await page.mouse.click(c0.x + dx, c0.y + dy)
    await expect(count).toHaveAttribute('data-route-count', String(k + 1), { timeout: 15_000 })
  }
  await expect(editor).toHaveAttribute('data-edit-phase', 'DIRTY')
  // change: a square route over the pad at 25 m above the terrain
  const square: [number, number][] = [[HOME[0] + 12, HOME[1]], [HOME[0] + 12, HOME[1] + 12], [HOME[0], HOME[1] + 12]]
  for (const [i, [e, n]] of square.entries()) {
    await setCell(page, 'e', i + 1, e)
    await setCell(page, 'n', i + 1, n)
    await setCell(page, 'h', i + 1, 25)
  }
  // insert after waypoint 1 (row menu), then delete it with the Delete key (the new waypoint is selected)
  await page.locator('[data-wp-menu="0"]').click()
  await page.locator('[data-wp-insert="0"]').click()
  await expect(count).toHaveAttribute('data-route-count', '4')
  await expect(cell(page, 'e', 2)).toHaveValue((HOME[0] + 12).toFixed(1))
  await page.locator('[data-edit-tool-item="select"]').click()
  await page.keyboard.press('Delete')
  await expect(count).toHaveAttribute('data-route-count', '3')
  await expect(cell(page, 'n', 2)).toHaveValue((HOME[1] + 12).toFixed(1))
  // drag: select waypoint 2 in the table, then drag its viewport handle 40 px to the right
  await page.locator('[data-figure="waypoint-table"] [data-row-key]').nth(1).click({ position: { x: 8, y: 8 } })
  const mark = page.locator('[data-edit-selected]')
  await expect(mark).toBeVisible()
  const mb = (await mark.boundingBox())!
  const e0 = Number(await cell(page, 'e', 2).inputValue())
  const n0 = Number(await cell(page, 'n', 2).inputValue())
  await page.mouse.move(mb.x + mb.width / 2, mb.y + mb.height / 2)
  await page.mouse.down()
  await page.mouse.move(mb.x + mb.width / 2 + 40, mb.y + mb.height / 2, { steps: 8 })
  await page.mouse.up()
  await expect.poll(async () => Math.hypot(Number(await cell(page, 'e', 2).inputValue()) - e0, Number(await cell(page, 'n', 2).inputValue()) - n0), { timeout: 5000 })
    .toBeGreaterThan(0.5)
  const dragged = [await cell(page, 'e', 2).inputValue(), await cell(page, 'n', 2).inputValue()]
  // undo and redo the drag
  await page.keyboard.press(`${MOD}+KeyZ`)
  await expect(cell(page, 'e', 2)).toHaveValue(e0.toFixed(1))
  await page.keyboard.press(`${MOD}+Shift+KeyZ`)
  await expect(cell(page, 'e', 2)).toHaveValue(dragged[0])
  // the dragged point stays near the pad; bring it back to a known open spot if the drag went far
  if (Math.hypot(Number(dragged[0]) - square[1][0], Number(dragged[1]) - square[1][1]) > 8) {
    await setCell(page, 'e', 2, square[1][0] + 3)
    await setCell(page, 'n', 2, square[1][1])
  }
  // coarse check passes, then submit: follow_path accepted -> running (the editor closes) -> succeeded
  await expect(page.locator('[data-route-check]')).toHaveAttribute('data-route-check', 'ok', { timeout: 15_000 })
  const submit = page.locator('[data-route-submit-button]')
  await expect(submit).toBeEnabled()
  await submit.click()
  const status = page.locator(`[data-route-status][data-route-vehicle="${ID}"]`)
  await expect(status).toBeVisible({ timeout: 30_000 })
  await expect(status).toHaveAttribute('data-route-status', 'succeeded', { timeout: 180_000 })

  // ---------------------------------------------------------------- area
  await page.locator('[data-edit-route]').click()
  await expect(editor).toHaveAttribute('data-edit-phase', 'CLEAN')
  await page.locator('[data-edit-tool-item="area"]').click()
  await expect(editor).toHaveAttribute('data-edit-tool', 'area')
  const c1 = await labelCentre(page, ID)
  await page.keyboard.down('Shift')
  await page.mouse.move(c1.x - 80, c1.y + 30)
  await page.mouse.down()
  await page.mouse.move(c1.x + 80, c1.y + 130, { steps: 10 })
  await page.mouse.up()
  await page.keyboard.up('Shift')
  const area = page.locator('[data-area-section]')
  await expect(area).toHaveAttribute('data-area-closed', '')
  await expect(area).toHaveAttribute('data-area-vertices', '4')
  await expect(page.locator('[data-area-generator]')).toContainText('覆盖')
  await page.locator('#area-spacingM').fill('10')
  // assign the other vehicle (p600-01 still holds the operator lease of its route; the mission engine leases p600-02 and
  // takes it off by itself): remove the default chip, pick p600-02 from the vehicle Combobox
  const chips = page.locator('[data-area-vehicles]')
  await chips.locator('[data-slot="combobox-chip-remove"]').first().click()
  await chips.locator('input').fill(OTHER)
  await expect(page.getByRole('option', { name: OTHER })).toBeVisible()
  await page.getByRole('option', { name: OTHER }).click()
  await expect(chips.locator('[data-slot="combobox-chip"]')).toHaveText([OTHER])
  await expect(page.getByRole('listbox')).toHaveCount(0) // the Combobox closes its list after the pick (no Esc: it would leave the area tool)
  await page.locator('[data-area-preview-button]').click()
  await expect(page.locator('[data-area-preview]')).toHaveAttribute('data-area-preview', 'ok', { timeout: 30_000 })
  await expect(page.locator('[data-energy-infeasible]')).toHaveCount(0)
  await page.locator('[data-area-create]').click()
  const created = page.locator('[data-area-created]')
  await expect(created).toHaveAttribute('data-area-created', 'started', { timeout: 30_000 })
  const mid = await created.getAttribute('data-area-mid')
  expect(mid).toBeTruthy()
  await expect(created).toHaveAttribute('data-mission-state', 'DONE', { timeout: 420_000 })
  const r = (await viewerGet(`/api/missions/${mid}`)) as { generator: string; state: string }
  expect(r.generator).toBe('lawnmower')
  expect(r.state).toBe('DONE')

  expect(errors, errors.join('\n')).toEqual([])
  expect(httpErrors, httpErrors.join('\n')).toEqual([])
})
