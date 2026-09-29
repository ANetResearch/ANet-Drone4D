// M05-AC-022 (FR-032, FR-033, FR-037, NFR-010): after the reveal, cycling the five colour modes, hiding three classes,
// toggling EDL and locking a rung neither compiles a program nor issues a request; San Francisco opens with HAG.
import { expect, test } from '@playwright/test'
import { expectNoErrors, frames, openWorld, perf, m05Server, waitStreaming, watch } from './common'

const srv = m05Server(4)

test('colour modes, class mask, EDL and manual rung without new programs (M05-AC-022)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  await page.waitForTimeout(2000)
  const programs = await perf<number>(page, 'gpu.programs')
  const requests = srv.get().log.length
  for (const m of ['hag', 'normal', 'class', 'source', 'height']) {
    await page.evaluate((mode) => (window as unknown as { __pc: { layers: { setColorMode(m: string): void } } }).__pc.layers.setColorMode(mode), m)
    await frames(page, 5)
  }
  await page.evaluate(() => {
    const l = (window as unknown as { __pc: { layers: { setClassVisible(i: number, on: boolean): void; setEdl(on: boolean): void; setQuality(q: unknown): void } } }).__pc.layers
    for (const c of [2, 3, 4]) l.setClassVisible(c, false)
    l.setEdl(false)
    l.setEdl(true)
    l.setQuality(1)
  })
  await frames(page, 10)
  expect(await perf<number>(page, 'gpu.programs')).toBe(programs)
  expect(await perf<number>(page, 'cas.index')).toBe(1)
  expect(srv.get().log.slice(requests).some((e) => e.path.endsWith('world.json'))).toBe(false) // nothing reopened
  await page.evaluate(() => (window as unknown as { __pc: { layers: { setQuality(q: unknown): void } } }).__pc.layers.setQuality('auto'))
  expectNoErrors(w)
})

test('San Francisco opens with its default colour mode HAG (world.json render.defaultColorMode)', async ({ browser }) => {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  const w = watch(page)
  await openWorld(page, srv.get().url, 'sanfrancisco')
  const mode = await page.evaluate(() => (window as unknown as { __pc: { prefs(): { colorMode: string } } }).__pc.prefs().colorMode)
  expect(mode).toBe('hag')
  await waitStreaming(page, 'sanfrancisco')
  expectNoErrors(w)
  await ctx.close()
})
