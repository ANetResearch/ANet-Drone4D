// Shared helpers of the M15 Playwright specs (M15 §10 "M15 takes part through assertion helpers in perf/m15/helpers"):
// a private `vite preview` of a test build (COOP/COEP on, only /api proxied; M15_DIST selects the build output so
// parallel agents never share dist/), page watchers (page errors and error-boundary logs), screenshots under
// .cache/impl/shots (AWR_SHOTS_DIR overrides), the D1-AC-20 DOM scans (forbidden glyphs, backdrop filters), the canvas
// drawing buffer and the injection hooks of test builds (window.__uxInject).
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { expect, type Page } from '@playwright/test'
import { preview, type PreviewServer } from 'vite'

export const WEB = resolve(import.meta.dirname, '..', '..', '..')
export const SHOTS = process.env.AWR_SHOTS_DIR ?? join(WEB, '..', '..', '.cache', 'impl', 'shots')
mkdirSync(SHOTS, { recursive: true })
export const DIST = process.env.M15_DIST ?? 'dist'
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0

export async function startPreview(port: number): Promise<PreviewServer> {
  return preview({
    configFile: false,
    root: WEB,
    logLevel: 'silent', // proxy errors of the absent backend are expected
    build: { outDir: DIST },
    preview: {
      host: '127.0.0.1',
      port: port + 10 * OFFSET,
      strictPort: true,
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' },
      proxy: { '/api': { target: `http://127.0.0.1:${8000 + 10 * OFFSET}`, ws: true } },
    },
  })
}
export const baseUrl = (port: number): string => `http://127.0.0.1:${port + 10 * OFFSET}`

export interface Watch { errors: string[]; console: string[] }
export function watch(page: Page): Watch {
  const w: Watch = { errors: [], console: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    w.console.push(m.text())
    // error boundaries (M15-E004 root, viewport and panel) catch render errors before they become page errors
    if (/M15-E0\d\d|TypeError|ReferenceError|RangeError/.test(m.text())) w.errors.push(m.text().split('\n')[0])
  })
  return w
}

export async function shot(page: Page, name: string, fullPage = false): Promise<void> {
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage, animations: 'disabled' })
}

export async function revealed(page: Page): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  await expect(page.locator('[data-figure="header"]')).toBeVisible()
}

/** D1-AC-20 in the rendered DOM: no emoji, no geometric shapes, misc symbols, dingbats or arrows from U+2194 */
export async function glyphScan(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const re = /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}/u
    const bad: string[] = []
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const s = n.textContent ?? ''
      if (re.test(s)) bad.push(s.slice(0, 40))
    }
    for (const el of document.querySelectorAll('[aria-label],[title],[placeholder]')) {
      for (const a of ['aria-label', 'title', 'placeholder']) {
        const v = el.getAttribute(a)
        if (v && re.test(v)) bad.push(`${a}=${v.slice(0, 40)}`)
      }
    }
    return bad
  })
}

export async function backdropFilters(page: Page): Promise<number> {
  return page.evaluate(() => {
    let n = 0
    for (const el of document.querySelectorAll('*')) {
      const v = getComputedStyle(el).backdropFilter
      if (v && v !== 'none') n++
    }
    return n
  })
}

export async function canvasBuffer(page: Page): Promise<string> {
  return page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas')
    return c ? `${c.width}x${c.height}` : 'none'
  })
}

/** icon-only buttons without an accessible name (M15-FR-112; D1-AC-21) */
export async function unnamedButtons(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = []
    for (const b of document.querySelectorAll<HTMLElement>('button,[role="button"]')) {
      if (b.closest('[inert],[aria-hidden="true"]')) continue
      const name = (b.getAttribute('aria-label') ?? '').trim() || (b.textContent ?? '').trim() || (b.getAttribute('title') ?? '').trim()
      const labelledby = b.getAttribute('aria-labelledby')
      if (!name && !labelledby) out.push(b.outerHTML.slice(0, 120))
    }
    return out
  })
}

/** visible toasts (the 4th and later carry data-limited, AWR-14 §11.4) */
export async function visibleToasts(page: Page): Promise<number> {
  return page.locator('[data-slot="toast"]:not([data-limited])').count()
}

export interface InjectEvent { seq: number; t_sim_ns: number; type: string; level: 0 | 1 | 2 | 3; uav: string | null; cid: null; data: Record<string, unknown> }
/** push reliable events through the event bridge of a test build and flush it */
export async function injectEvents(page: Page, events: InjectEvent[]): Promise<void> {
  await page.evaluate((b) => {
    const w = window as unknown as { __uxInject?: { events: (x: unknown[]) => void; flush: () => void } }
    w.__uxInject?.events(b)
    w.__uxInject?.flush()
  }, events)
}
/** patch the connection and session view (viewer, seat taken, offline banner) */
export async function injectConn(page: Page, patch: Record<string, unknown>): Promise<void> {
  await page.evaluate((p) => {
    const w = window as unknown as { __uxInject?: { conn: (x: unknown) => void } }
    w.__uxInject?.conn(p)
  }, patch)
}
export const isTestBuild = (page: Page): Promise<boolean> => page.evaluate(() => '__ux' in window && '__uxInject' in window)
