// Motion rules at run time (D1-AC-20; PERF-AC-024; ADR-029; AWR-18 §6.2): no backdrop-filter anywhere; at most 2 resident
// infinite animations; with prefers-reduced-motion the Base UI parts animate nothing (the motion tier is 'reduced').
import { expect, test } from '@playwright/test'
import { revealed } from './helpers'

async function animations(page: import('@playwright/test').Page): Promise<{ infinite: number; running: number; blur: number }> {
  return page.evaluate(() => {
    const all = document.getAnimations()
    let infinite = 0
    let blur = 0
    for (const a of all) {
      const t = a.effect?.getComputedTiming()
      if (t && t.iterations === Infinity) infinite++
      const kf = (a.effect as KeyframeEffect | null)?.getKeyframes?.() ?? []
      if (kf.some((k) => String(k.filter ?? '').includes('blur'))) blur++
    }
    // scroll-driven animations (shadcn scroll-fade masks, animation-timeline: scroll()) follow the scroll position, not
    // time, and are not motion in the prefers-reduced-motion sense (INT-1)
    const timed = all.filter((a) => !(typeof ScrollTimeline !== 'undefined' && a.timeline instanceof ScrollTimeline))
    return { infinite, running: timed.filter((a) => a.playState === 'running').length, blur }
  })
}

test('no backdrop-filter and resident loops <= 2', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/world/shenzhen?source=fake&fakeN=20')
  await revealed(page)
  await page.waitForTimeout(2000)
  const bf = await page.evaluate(() => [...document.querySelectorAll('*')].filter((el) => {
    const v = getComputedStyle(el).backdropFilter
    return v && v !== 'none'
  }).length)
  expect(bf).toBe(0)
  const a = await animations(page)
  expect(a.infinite).toBeLessThanOrEqual(2)
  expect(a.blur).toBeLessThanOrEqual(12)
})

test('reduced motion: overlays open without animations', async ({ browser }) => {
  test.setTimeout(120_000)
  const ctx = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1280, height: 720 } })
  const page = await ctx.newPage()
  await page.goto('/world/shenzhen?source=fake&fakeN=5')
  await revealed(page)
  await page.keyboard.press('Control+KeyK')
  await page.getByPlaceholder('输入命令或搜索').waitFor({ timeout: 5000 })
  const a = await animations(page)
  expect(a.running).toBe(0)
  const tier = await page.evaluate(() => (window as unknown as { __perf: { ui: { motionTier: string } } }).__perf.ui.motionTier)
  expect(tier).toBe('reduced')
  await ctx.close()
})
