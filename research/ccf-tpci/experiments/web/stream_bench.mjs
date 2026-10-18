#!/usr/bin/env node
// Streaming measurement for the paper: one scripted 60 s camera flight (bench=flight60, point-cloud scene, fake fleet)
// per (device, budget controller, rep), reading the client's own perf probe (frame intervals, first-frame time,
// points drawn, achieved screen-space error).
//   node stream_bench.mjs --base http://127.0.0.1:4390 --world shenzhen --device <label> --out <dir>
//        [--variants cas,fixed100k,fixed1m,fixed3m] [--reps 3] [--channel chrome] [--headed] [--gpu angle-vulkan|swiftshader]
//        [--throttle 4]
// PW_MODULE selects the Playwright module ('@playwright/test' by default, or a path to playwright-core).
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'

const { values: a } = parseArgs({ options: {
  base: { type: 'string', default: 'http://127.0.0.1:4390' }, world: { type: 'string', default: 'shenzhen' },
  device: { type: 'string' }, out: { type: 'string' }, variants: { type: 'string', default: 'cas,fixed100k,fixed1m,fixed3m' },
  reps: { type: 'string', default: '3' }, channel: { type: 'string' }, headed: { type: 'boolean' }, gpu: { type: 'string' },
  throttle: { type: 'string', default: '1' }, width: { type: 'string', default: '1600' }, height: { type: 'string', default: '900' },
} })
const pw = await import(process.env.PW_MODULE ?? '@playwright/test')
const chromium = pw.chromium ?? pw.default.chromium
const VARIANT = { cas: {}, fixed100k: { fixedB: 100000 }, fixed1m: { fixedB: 1000000 }, fixed3m: { fixedB: 3000000 } }
const GPU = {
  swiftshader: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  'angle-vulkan': ['--use-angle=vulkan', '--enable-features=Vulkan', '--ignore-gpu-blocklist', '--enable-gpu'],
  'angle-egl': ['--use-gl=angle', '--use-angle=gl-egl', '--ignore-gpu-blocklist', '--enable-gpu'],
}
mkdirSync(a.out, { recursive: true })

async function one(variant, rep) {
  const args = [...(GPU[a.gpu] ?? []), '--disable-background-timer-throttling', '--disable-renderer-backgrounding']
  const browser = await chromium.launch({ headless: !a.headed, channel: a.channel, args })
  const page = await browser.newPage({ viewport: { width: Number(a.width), height: Number(a.height) } })
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  if (Number(a.throttle) > 1) {
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: Number(a.throttle) })
  }
  const q = new URLSearchParams({ bench: 'flight60', scene: 'pc', source: 'fake', chrome: '0' })
  for (const [k, v] of Object.entries(VARIANT[variant])) q.set(k, String(v))
  const t0 = Date.now()
  await page.goto(`${a.base}/world/${a.world}?${q}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  console.log(`${variant} rep${rep}: page loaded`)
  await page.waitForFunction(() => (window.__perf?.load?.revealAt ?? 0) > 0, null, { timeout: 120000, polling: 500 })
  console.log(`${variant} rep${rep}: revealed after ${(Date.now() - t0) / 1000} s`)
  await page.waitForTimeout(1000)
  await page.evaluate(() => {
    window.__perf.reset('all')
    window.__perf.mark('flight.start')
  })
  await page.waitForFunction(() => window.__perf?.bench?.done === true, null, { timeout: 240000, polling: 1000 })
  const snap = await page.evaluate(() => window.__perf.snapshot({ rings: true }))
  const gl = await page.evaluate(() => {
    const c = document.createElement('canvas').getContext('webgl2')
    const x = c?.getExtension('WEBGL_debug_renderer_info')
    return { renderer: x ? c.getParameter(x.UNMASKED_RENDERER_WEBGL) : c?.getParameter(c.RENDERER), ua: navigator.userAgent }
  })
  await browser.close()
  const rec = { device: a.device, world: a.world, variant, rep, throttle: Number(a.throttle), wall_s: (Date.now() - t0) / 1000, gl, errors, snap }
  writeFileSync(join(a.out, `${a.device}_${a.world}_${variant}_${rep}.json`), JSON.stringify(rec))
  const iv = snap.frame?.interval ?? []
  const s = [...iv].sort((x, y) => x - y)
  console.log(`${a.device} ${variant} rep${rep}: frames ${iv.length} p50 ${s[s.length >> 1]?.toFixed(1)} ms ` +
    `p95 ${s[Math.floor(0.95 * s.length)]?.toFixed(1)} ms ttfp ${snap.load?.ttfp} renderer ${gl.renderer}`)
}

for (let r = 0; r < Number(a.reps); r++) for (const v of a.variants.split(',')) {
  try {
    await one(v, r)
  } catch (e) {
    console.error(`${a.device} ${v} rep${r} failed: ${e}`)
  }
}
