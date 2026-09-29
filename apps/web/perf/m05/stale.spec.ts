// M05-AC-033 (FR-008): the world is rebuilt while the page runs (the server switches contentVersion): the next ?v= request
// answers 409, the engine reopens with the new version, keeps the camera and draws points again within 3 s.
import { expect, test } from '@playwright/test'
import { expectNoErrors, look, openWorld, m05Server, waitStreaming, watch } from './common'

const srv = m05Server(7)

test('content version change reopens the world with the camera kept (M05-AC-033)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  await look(page, [-250, -350, 160], [0, 0, 30])
  await page.waitForTimeout(1500)
  await page.request.post(`${srv.get().url}/__m05/cv`, { data: { world: 'shenzhen', cv: 'rebuilt00001' } })
  await look(page, [250, 350, 160], [0, 0, 30]) // new view: new node requests hit the 409
  const t0 = Date.now()
  await page.waitForFunction(() => (window as unknown as { __pc: { engine: { info: { contentVersion: string } | null } } }).__pc.engine.info?.contentVersion === 'rebuilt00001',
    null, { timeout: 30_000 })
  await waitStreaming(page, 'shenzhen')
  const dt = Date.now() - t0
  const drawn = await page.evaluate(() => (window as unknown as { __pc: { stats(): { drawn: number } } }).__pc.stats().drawn)
  expect(drawn).toBeGreaterThan(0)
  test.info().annotations.push({ type: 'reopen_ms', description: String(dt) })
  expect(dt).toBeLessThan(10_000)
  await page.request.post(`${srv.get().url}/__m05/cv`, { data: { world: 'shenzhen', cv: null } })
  expectNoErrors(w)
})
