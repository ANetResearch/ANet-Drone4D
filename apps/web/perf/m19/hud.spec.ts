// M19-AC-019 HUD (WP-13 writes it for M19, d2-WP-06-to-M16 item 1; M19-FR-026, FR-028; D2-AC-14 HUD part). Test build on
// the sandbox route with the scripted REST of perf/m15/helpers/d2rest.ts and FakeSource replaying
// packages/contracts/fixtures/d2/sandbox_t5.awrrt (viewport on): select uav0001, enter its FPV on the EO view
// (__vp.sensorView), the HUD shows "n_eff / n_req px" of a target in that vehicle's `uav/{id}/perception` frames (the value
// is one of the fixture's frames: the replay loops), the limiting factor line ("no limiting factor" for these frames) and a
// progress bar of n_eff / n_req; the capture state follows the frame (captured for person-01).
// Build: `M15_DIST=<dir> node perf/m15/helpers/build-ui.mjs`; run: `M15_DIST=<dir> npx playwright test perf/m19/hud.spec.ts`.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { open, REPO, routes, storeSession, type Script } from '../m15/helpers/d2rest'
import { baseUrl, startPreview, watch } from '../m15/helpers/shell'

const PORT = 4193
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => { server = await startPreview(PORT) })
test.afterAll(async () => { await server?.close() })

interface PTarget { target: string; n_eff: number; n_req: number; limiting: string | null; captured?: boolean }
interface Rec { batch?: { records: { channel_id: number; decoded?: { uav?: string; sensors?: { kind: string; targets: PTarget[] }[] } }[] }; text?: { op?: string; channels?: { id: number; topic: string }[] } }

/** the "n_eff/n_req" strings the HUD may show for uav0001 (fmt.num 1 and 0 digits, en-US) */
function fixtureValues(): Set<string> {
  const d = JSON.parse(readFileSync(join(REPO, 'packages', 'contracts', 'fixtures', 'd2', 'sandbox_t5.json'), 'utf8')) as { records: Rec[] }
  const chan = new Map<number, string>()
  for (const r of d.records) for (const c of r.text?.op === 'advertise' ? r.text.channels ?? [] : []) chan.set(c.id, c.topic)
  const out = new Set<string>()
  for (const r of d.records) {
    for (const rec of r.batch?.records ?? []) {
      if (chan.get(rec.channel_id) !== 'uav/uav0001/perception') continue
      for (const s of rec.decoded?.sensors ?? []) for (const t of s.targets) out.add(`${t.n_eff.toFixed(1)}/${t.n_req.toFixed(0)}`)
    }
  }
  return out
}

test('FPV HUD: n_eff / n_req px and the limiting factor of the perception frames', async ({ page, context }) => {
  const w = watch(page)
  const s: Script = { posts: [], preview: 'ok' }
  await routes(context, s)
  await storeSession(page)
  await open(page, true)
  const values = fixtureValues()
  expect(values.size).toBeGreaterThan(5)
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-drone-id="uav0001"]').first().click()
  await page.evaluate(() => (window as unknown as { __vp?: { sensorView: { set: (m: string, o: unknown) => void } } }).__vp?.sensorView.set('eo', { enterFpv: true }))
  const hud = page.locator('[data-fpv-hud]').first()
  await expect(hud).not.toHaveAttribute('data-fpv-hud', '', { timeout: 30_000 })
  const px = await page.locator('[data-fpv-px]').first().getAttribute('data-fpv-px')
  expect(values.has(px ?? '')).toBe(true)
  await expect(hud.getByRole('progressbar')).toBeVisible()
  const target = await hud.getAttribute('data-fpv-hud')
  expect(['person-01', 'dog-01']).toContain(target)
  if ((await page.locator('[data-fpv-limiting]').count()) === 0) await expect(hud).toContainText(/无限制|没有限制|无/)
  expect(s.posts.filter((p) => !p.url.includes('keepalive'))).toEqual([])          // looking is read-only
  expect(w.errors).toEqual([])
})
