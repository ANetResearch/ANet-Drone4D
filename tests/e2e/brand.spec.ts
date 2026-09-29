// Brand at run time (D1-AC-20 BRAND-04; ADR-032; AWR-15 §4.8): the badge and avatar come from public/brand as they are
// (no recolouring filter), the badge keeps its aspect ratio, and no brand asset sits inside the 3D viewport.
import { expect, test } from '@playwright/test'
import { revealed } from './helpers'

test('brand assets are unaltered and outside the viewport', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/world/shenzhen?source=fake&fakeN=2')
  await revealed(page)
  const r = await page.evaluate(() => {
    const els = [...document.querySelectorAll<HTMLElement>('[data-brand], [data-brand-avatar]')]
    return els.map((el) => {
      const img = el instanceof HTMLImageElement ? el : el.querySelector('img')
      const cs = getComputedStyle(img ?? el)
      return { inViewport: !!el.closest('[data-viewport]'), filter: cs.filter, mix: cs.mixBlendMode,
        src: img?.getAttribute('src') ?? '', w: img?.naturalWidth ?? 0, h: img?.naturalHeight ?? 0,
        rw: img?.getBoundingClientRect().width ?? 0, rh: img?.getBoundingClientRect().height ?? 0 }
    })
  })
  expect(r.length).toBeGreaterThan(0)
  for (const b of r) {
    expect(b.inViewport).toBe(false)
    expect(b.filter).toBe('none')
    expect(b.mix).toBe('normal')
    expect(b.src).toMatch(/brand/)
    if (b.w && b.h && b.rw && b.rh) expect(Math.abs(b.rw / b.rh - b.w / b.h)).toBeLessThan(0.05)
  }
})
