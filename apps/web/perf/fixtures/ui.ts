// UI helpers of the M16 performance and e2e specs: hotkeys and dialogs through the product UI only (production builds
// have no test hooks). Hotkeys come from ui/actions/builtin.ts (mod+KeyA select all, shift+KeyR return home, mod+KeyB
// left panel, KeyL follow, KeyF focus, Period select next, mod+KeyK command palette).
import type { Page } from '@playwright/test'

export async function hotkey(page: Page, combo: string): Promise<void> {
  // move the pointer over the viewport without actionability waits: the DOM label overlay covers the canvas, so a
  // locator hover waits for pointer events until the test times out (INT-1)
  const vs = page.viewportSize() ?? { width: 1280, height: 720 }
  await page.mouse.move(Math.round(vs.width / 2), Math.round(vs.height / 2))
  await page.keyboard.press(combo)
}

/** confirm the open AlertDialog with its non-cancel action (focus starts on cancel, 14 §6.7) */
export async function confirmDialog(page: Page, timeout = 10_000): Promise<void> {
  const dlg = page.getByRole('alertdialog')
  await dlg.waitFor({ timeout })
  const buttons = dlg.getByRole('button')
  const n = await buttons.count()
  for (let i = n - 1; i >= 0; i--) {
    const b = buttons.nth(i)
    const t = (await b.innerText()).trim()
    if (t && !/取消|Cancel/.test(t)) {
      await b.hover()
      await b.click()
      return
    }
  }
  throw new Error('no confirm action in the dialog')
}

/** run a command palette entry by its visible label */
export async function palette(page: Page, text: string): Promise<void> {
  await page.keyboard.press('Control+KeyK')
  const input = page.getByPlaceholder('输入命令或搜索')
  await input.waitFor({ timeout: 5000 })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

/** visible toasts and the rendered versus visible rows of the drone rail (D1-AC-27, PERF-AC-026) */
export async function uiCounts(page: Page): Promise<{ toasts: number; railRendered: number; railVisible: number }> {
  return page.evaluate(() => {
    const vis = (el: Element): boolean => {
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
    }
    const toasts = [...document.querySelectorAll('[data-slot="toast"]:not([data-limited])')].filter(vis).length
    const rows = [...document.querySelectorAll('[data-rail-row]')]
    return { toasts, railRendered: rows.length, railVisible: rows.filter(vis).length }
  })
}
