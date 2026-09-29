// Keyboard and accessibility (D1-AC-21; M15-FR-112): every icon-only button has an accessible name; the focus ring is
// visible; the shortcut help opens with shift+Slash; shortcuts do not fire while typing in an input.
import { expect, test } from '@playwright/test'
import { revealed } from './helpers'

test('accessible names, focus and shortcuts', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/world/shenzhen?source=fake&fakeN=5')
  await revealed(page)
  const unnamed = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>('button,[role="button"]')]
    .filter((b) => !b.closest('[inert],[aria-hidden="true"]'))
    .filter((b) => !((b.getAttribute('aria-label') ?? '').trim() || (b.textContent ?? '').trim() || (b.getAttribute('title') ?? '').trim()
      || b.getAttribute('aria-labelledby'))).map((b) => b.outerHTML.slice(0, 100)))
  expect(unnamed).toEqual([])
  await page.keyboard.press('Tab')
  const outline = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null
    if (!el || el === document.body) return 'none'
    const cs = getComputedStyle(el)
    return cs.outlineStyle !== 'none' || cs.boxShadow !== 'none' ? 'visible' : 'none'
  })
  expect(outline).toBe('visible')
  await page.keyboard.press('Shift+Slash')
  await expect(page.getByRole('dialog')).toBeVisible({ timeout: 5000 })
  await page.keyboard.press('Escape')
  await page.keyboard.press('Control+KeyK')
  const input = page.getByPlaceholder('输入命令或搜索')
  await input.waitFor({ timeout: 5000 })
  await input.type('P')
  expect(await input.inputValue()).toBe('P')     // KeyP (HUD toggle) did not fire inside the input
})
