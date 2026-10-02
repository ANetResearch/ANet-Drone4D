#!/usr/bin/env node
// 网关容量用例的浏览器客户端编排（D1-AC-08、PERF-AC-035；AWR-18 §8.7(2)、§9.5 `?rt=1`、§12.4；M11）。
// 由 tools/bench/ipc/bench_state.py --clients K --with-flight60 启动（PR-12：客户端属于本用例，由同一工具启动）。
// 启动 K 个独立的 headless Chromium（Chrome for Testing 151，标志集 C1 SwiftShader，1280×720，DPR 1，临时 profile），
// 各自打开 `<base>/world/<city>?bench=flight60&source=live&<query>`，等遮罩揭开，1 s 预热后 `__perf.reset('all')` 与
// `mark('flight.start')`，再等 `__perf.bench.done`。K 个客户端全部开始飞行时打印一行 `FLIGHT_START <unix_ms>`，
// 全部结束时打印 `FLIGHT_DONE <unix_ms>`，最后一行输出 JSON 摘要（每个客户端的帧数、TTFP、飞行次数、错误）。客户端帧节奏不判定
// （18 §8.7(2)）。标志集与 harness 共用 apps/web/perf/harness/browser.mjs，不在此重复书写。
// `--hold S`：各客户端遮罩揭开的时刻不同（负载下 TTFP 相差可达 20 s），先飞完的客户端在 FLIGHT_START 之后 S 秒内重新载入页面
// 再飞一遍，使测量窗口内始终有 K 个客户端在流式加载与订阅（缺省 0：每个客户端只飞一次）。
// 用法：node tools/bench/ipc/flight60_clients.mjs --base http://127.0.0.1:8090 --clients 3 [--city shenzhen]
//       [--query "scene=full&n=1000"] [--timeout 240] [--hold 65]
import { existsSync } from 'node:fs'
import { chromium } from 'playwright'
import { FLAGS, VIEWPORT } from '../../../apps/web/perf/harness/browser.mjs'

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : 'true'])
  return acc
}, []))
const BASE = (args.base ?? 'http://127.0.0.1:8000').replace(/\/$/, '')
const CLIENTS = Number(args.clients ?? 3)
const CITY = args.city ?? 'shenzhen'
const QUERY = args.query ?? 'scene=pc&chrome=0&rt=1'
const TIMEOUT_MS = Number(args.timeout ?? 240) * 1000
const HOLD_MS = Number(args.hold ?? 0) * 1000
let holdUntil = Number.POSITIVE_INFINITY
const CHROME = process.env.PW_CHROME ?? `${process.env.HOME}/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`

function url() {
  const q = new URLSearchParams(QUERY)
  q.set('bench', 'flight60')
  if (!q.has('source')) q.set('source', 'live')
  return `${BASE}/world/${CITY}?${q.toString()}`
}

async function client(idx, started, release) {
  const browser = await chromium.launch({ executablePath: existsSync(CHROME) ? CHROME : undefined, headless: true, args: FLAGS.C1 })
  const errors = []
  try {
    const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1 })
    const page = await ctx.newPage()
    page.on('pageerror', (e) => { if (!/WebGPU is not available/i.test(e.message)) errors.push(`${e.name}: ${e.message}`) })
    const fly = async () => {
      await page.waitForFunction(() => (window.__perf?.load?.revealAt ?? 0) > 0, null, { polling: 1000, timeout: 120_000 })
      await page.waitForTimeout(1000)
      await page.evaluate(() => { window.__perf.reset('all'); window.__perf.mark('flight.start') })
    }
    await page.goto(url(), { waitUntil: 'domcontentloaded' })
    await fly()
    const first = await page.evaluate(() => ({ ttfp: window.__perf.load?.ttfp ?? null, tier: window.__perf.meta?.tier ?? null }))
    started()
    await release
    let flights = 0
    let frames = 0
    const isDone = () => window.__perf?.bench?.done === true
    if (HOLD_MS <= 0) {
      await page.waitForFunction(isDone, null, { polling: 1000, timeout: TIMEOUT_MS })
      flights = 1
      frames = await page.evaluate(() => window.__perf.frame?.count ?? 0)
    } else {
      // 飞完即重新载入再飞，直到保持期结束；保持期结束时正在飞的那一遍不等完
      for (;;) {
        const remain = holdUntil - Date.now()
        if (remain <= 0) break
        const done = await page.waitForFunction(isDone, null, { polling: 1000, timeout: Math.min(TIMEOUT_MS, remain) })
          .then(() => true, () => false)
        frames += await page.evaluate(() => window.__perf.frame?.count ?? 0)
        if (!done) break
        flights++
        if (Date.now() >= holdUntil) break
        await page.reload({ waitUntil: 'domcontentloaded' })
        await fly()
      }
    }
    return { idx, ok: true, frames, flights, ...first, errors }
  } catch (e) {
    return { idx, ok: false, error: String(e?.message ?? e).slice(0, 300), errors }
  } finally {
    await browser.close().catch(() => {})
  }
}

async function main() {
  let n = 0
  let releaseAll
  const release = new Promise((r) => { releaseAll = r })
  const started = () => {
    n += 1
    if (n === CLIENTS) {
      holdUntil = Date.now() + HOLD_MS
      process.stdout.write(`FLIGHT_START ${Date.now()}\n`)
      releaseAll()
    }
  }
  // 客户端启动错开 1.5 s：避免 K 个浏览器同时首编译与首屏 Range 叠加成启动尖峰（测量窗口从全部开始飞行起算）
  const jobs = []
  for (let i = 0; i < CLIENTS; i++) {
    jobs.push(client(i, started, release))
    await new Promise((r) => setTimeout(r, 1500))
  }
  const timer = setTimeout(() => {
    if (n < CLIENTS) {
      holdUntil = Date.now() + HOLD_MS
      process.stdout.write(`FLIGHT_START ${Date.now()}\n`)
      releaseAll()
    }
  }, 180_000)
  const res = await Promise.all(jobs)
  clearTimeout(timer)
  process.stdout.write(`FLIGHT_DONE ${Date.now()}\n`)
  process.stdout.write(`${JSON.stringify({ schema: 'awr.bench.flight60clients.v1', base: BASE, query: QUERY, clients: res })}\n`)
  process.exit(res.every((r) => r.ok) ? 0 : 1)
}

main().catch((e) => {
  process.stderr.write(`flight60_clients: ${e?.stack ?? e}\n`)
  process.exit(2)
})
