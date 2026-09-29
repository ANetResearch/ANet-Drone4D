// Debug probe (M15): serves a build with vite preview, opens a page in the local Chrome and prints the console and
// page errors grouped by message. Usage: M15_DIST=<build dir> node perf/m15/helpers/probe.mjs [path] [waitMs]
import { chromium } from '@playwright/test'
import { preview } from 'vite'
const OUT = process.env.M15_DIST
const server = await preview({ configFile: false, root: process.cwd(), logLevel: 'silent', build: { outDir: OUT },
  preview: { host: '127.0.0.1', port: 4196, strictPort: true, headers: { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp' } } })
const browser = await chromium.launch({ executablePath: `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } })
const logs = new Map()
page.on('console', (m) => { const k = `${m.type()}: ${m.text().slice(0, 200)}`; logs.set(k, (logs.get(k) ?? 0) + 1) })
page.on('pageerror', (e) => { const k = `pageerror: ${e.message.slice(0, 200)}`; logs.set(k, (logs.get(k) ?? 0) + 1) })
await page.goto(`http://127.0.0.1:4196${process.argv[2] ?? '/world/shenzhen?source=fake&fakeN=1&reveal=shell'}`)
await page.waitForTimeout(Number(process.argv[3] ?? 12000))
const info = await page.evaluate(() => ({ rows: document.querySelectorAll('[data-rail-row]').length, mask: !!document.getElementById('boot-mask'), conn: document.querySelector('[data-conn]')?.getAttribute('data-conn'), ux: window.__ux?.boot }))
console.log(JSON.stringify(info))
for (const [k, n] of logs) console.log(n, k)
await browser.close(); await server.close()
