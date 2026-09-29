// Debug probe (M15): opens the UI-only build at a given size, selects the first rail row and prints the right rail DOM
// summary (used while fixing the compact breakpoint). Usage: M15_DIST=<dir> node perf/m15/helpers/probe-detail.mjs 1280 720
import { chromium } from '@playwright/test'
import { preview } from 'vite'
const [w, h] = [Number(process.argv[2] ?? 1280), Number(process.argv[3] ?? 720)]
const server = await preview({ configFile: false, root: process.cwd(), logLevel: 'silent', build: { outDir: process.env.M15_DIST },
  preview: { host: '127.0.0.1', port: 4197, strictPort: true, headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' } } })
const browser = await chromium.launch({ executablePath: `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome` })
const page = await browser.newPage({ viewport: { width: w, height: h } })
page.on('console', (m) => { if (m.type() === 'error') console.log('console', m.text().slice(0, 300)) })
await page.goto('http://127.0.0.1:4197/world/shenzhen?source=fake&fakeN=5&reveal=shell&viewport=off')
await page.waitForSelector('[data-rail-row]', { timeout: 30000 })
await page.locator('[data-rail-row]').first().click()
await page.waitForTimeout(1500)
console.log(await page.evaluate(() => {
  const host = document.querySelector('[data-slot="rail-host"][data-side="right"]')
  const panels = [...host.querySelectorAll('[data-slot="tabs-content"]')].map((p) => ({ hidden: p.hidden, cls: p.className.slice(0, 80), h: p.getBoundingClientRect().height, text: p.textContent.slice(0, 60), state: p.getAttribute('data-hidden') }))
  const detail = host.querySelector('[data-drone-detail]')
  return JSON.stringify({ panels, detail: detail ? detail.getBoundingClientRect() : null, hostRect: host.getBoundingClientRect() }, null, 1)
}))
await browser.close(); await server.close()
