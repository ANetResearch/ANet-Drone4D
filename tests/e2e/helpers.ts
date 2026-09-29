// Shared helpers of the M16 end-to-end specs (tests/e2e, Playwright project `e2e`). Under the harness the backend comes
// from AWR_PERF_BASE; run directly, the live specs start their own supervisor through perf/skeleton.server.ts startBackend
// (ci profile, free ports, temporary runs directory) with AWR_SCENARIO set by the caller.
import { expect, type Page } from '@playwright/test'
import { startBackend } from '../../apps/web/perf/skeleton.server'

export interface Live { url: string; close(): Promise<void> }

export async function liveBackend(scenario: string): Promise<Live> {
  const base = process.env.AWR_PERF_BASE
  if (base) return { url: base.replace(/\/$/, ''), close: async () => {} }
  const b = await startBackend(undefined, { AWR_SCENARIO: scenario })
  return { url: b.url, close: () => b.close() }
}

export function watch(page: Page): { errors: string[] } {
  const w = { errors: [] as string[] }
  page.on('pageerror', (e) => {
    const m = `${e.name}: ${e.message}`
    if (!/WebGPU is not available/i.test(m)) w.errors.push(m)
  })
  return w
}

export async function revealed(page: Page, timeout = 60_000): Promise<void> {
  await page.waitForFunction(() => {
    const p = (window as unknown as { __perf?: { load?: { revealAt: number } } }).__perf
    return !!p && !!p.load && p.load.revealAt > 0
  }, null, { polling: 500, timeout })
}

/** D1-AC-20 in the rendered DOM (text nodes and aria-label, title, placeholder) */
export async function glyphScan(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const re = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u
    const bad: string[] = []
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = w.nextNode(); n; n = w.nextNode()) if (re.test(n.textContent ?? '')) bad.push((n.textContent ?? '').slice(0, 40))
    for (const el of document.querySelectorAll('[aria-label],[title],[placeholder]')) {
      for (const a of ['aria-label', 'title', 'placeholder']) {
        const v = el.getAttribute(a)
        if (v && re.test(v)) bad.push(`${a}=${v.slice(0, 40)}`)
      }
    }
    return bad
  })
}

export const isTestBuild = (page: Page): Promise<boolean> => page.evaluate(() => '__vp' in window || '__uxInject' in window)

export async function noErrors(w: { errors: string[] }): Promise<void> {
  expect(w.errors, w.errors.join('\n')).toEqual([])
}
