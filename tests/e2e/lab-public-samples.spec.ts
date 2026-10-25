// Playwright `lab/public-samples`（R07；D3-AC-21 只读样例视图、D3-AC-25 公开样例页条款；AWR-14 §21.13、§21.14；AWR-17 §18.8.5）。
// 被测对象为部署到 emax 的 /lab/ 公开构建本身（`make lab-build` 的输出，生产构建，无测试开关），静态目录为 lab_static_index.v1 夹具
// （tests/e2e/lab_stack.py make-public-site：golden 磁带、3 机 × [RGB、热成像] H.264（帧顶条码编码帧号）、tracks_obs.bin、public 档
// 切片与 basemap.rrd；AWR_LAB_SLICES 给出时用 R04 的真实切片）。静态服务记录全部请求。
//   1 Tier S（本机软件渲染即 Tier S）：样例目录 → 样例运行页；事件回放只显示脚本演员；多机私有视角按仿真时间同步：1× 播放期间合成帧
//     偏差 ≤ 1 帧（页面 requestVideoFrameCallback 度量，data-sync-*；性能类度量，只在主机空闲即 1 min 负载 ≤ 0.5 × 核数时判定，
//     否则记为未判定），暂停拖动后画面条码与播放头一致（始终判定）；数据检视为切片与底图下载；
//     只发起同源 /lab/、/lab-static/ 的 GET，无 WebSocket，无主得分与跨策略比较；面板外壳文案过 copy-lint；
//   2 Tier B：数据检视面板以静态来源打开切片与 basemap.rrd（同一 recording_id），首帧可见时刻；不发起 catalog、/api/、/rerun/ 请求，
//     Viewer 的站外请求（GitHub releases）由请求守卫拦截；面板外壳文案过 copy-lint。本机（AWR_LAB_TIER 缺省 S）以 WebGL 渲染器字符串
//     仿真硬件 GPU，渲染仍为 SwiftShader，数字只作开发参考；AWR_LAB_TIER=B 时在 Tier B 参考设备上运行并按 ≤ 5 s、≤ 1 帧断言。
import { cpus, loadavg } from 'node:os'
import { join } from 'node:path'
import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import {
  emulateHardwareRenderer, FIRST_FRAME_MAX_MS, measureFirstFrame, OUT_DIR, remoteBrowser, shellCopyFindings, startPublicSite, TIER, traceInspectStates, writeResult,
  type PublicSite,
} from './labStack'

test.describe.configure({ mode: 'default' })

let site: PublicSite
let ctxS: BrowserContext | null = null
let ctxB: BrowserContext
const FPS = 10
const loadPerCore = () => Math.round((loadavg()[0]! / cpus().length) * 100) / 100

test.beforeAll(async ({ browser }) => {
  test.setTimeout(300_000)
  site = await startPublicSite()
  const b = (await remoteBrowser()) ?? browser
  if (TIER === 'S') ctxS = await b.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })
  ctxB = await b.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })
  if (TIER === 'S') await emulateHardwareRenderer(ctxB)
})
test.afterAll(async () => {
  await ctxS?.close()
  await ctxB?.close()
  await site?.close()
})

interface BrowserReq { m: string; u: string; failed?: string }
function recordRequests(page: Page): { reqs: BrowserReq[]; ws: string[] } {
  const r = { reqs: [] as BrowserReq[], ws: [] as string[] }
  page.on('request', (q) => r.reqs.push({ m: q.method(), u: q.url() }))
  page.on('requestfailed', (q) => r.reqs.push({ m: q.method(), u: q.url(), failed: q.failure()?.errorText }))
  page.on('websocket', (w) => r.ws.push(w.url()))
  return r
}

/** 只读静态：浏览器发出的请求全为同源 /lab/、/lab-static/ 的 GET（data:、blob: 除外），服务器日志同样只有 GET/HEAD */
function checkStaticOnly(rec: { reqs: BrowserReq[]; ws: string[] }): { total: number; offsite: string[]; nonGet: string[]; catalog: string[] } {
  const origin = new URL(site.url).origin
  const net = rec.reqs.filter((r) => !r.u.startsWith('data:') && !r.u.startsWith('blob:'))
  const offsite = net.filter((r) => !r.u.startsWith(`${origin}/lab/`) && !r.u.startsWith(`${origin}/lab-static/`)).map((r) => r.u)
  const nonGet = net.filter((r) => r.m !== 'GET').map((r) => `${r.m} ${r.u}`)
  const catalog = net.filter((r) => /rerun\+|\/rerun[/.]|\/api\/|catalog/i.test(r.u)).map((r) => r.u)
  const srv = site.requests()
  expect(srv.filter((x) => x.m !== 'GET' && x.m !== 'HEAD'), 'server log: GET/HEAD only').toEqual([])
  expect(srv.filter((x) => !x.p.startsWith('/lab/') && !x.p.startsWith('/lab-static/') && x.p !== '/lab'), 'server log: /lab/ and /lab-static/ only').toEqual([])
  expect(offsite, 'off-site requests').toEqual([])
  expect(nonGet, 'non-GET requests').toEqual([])
  expect(catalog, 'catalog / api / rerun requests').toEqual([])
  expect(rec.ws, 'WebSocket').toEqual([])
  return { total: net.length, offsite, nonGet, catalog }
}

async function openRun(page: Page): Promise<void> {
  await page.goto(`${site.url}/lab/`)
  await page.waitForSelector('[data-view="lab-public-catalog"]', { timeout: 60_000 })
  await page.click(`[data-sample-link="${site.runId}"]`)
  await page.waitForSelector(`[data-view="lab-public-run"][data-sample="${site.runId}"]`, { timeout: 60_000 })
  await page.waitForSelector('[data-figure="lab-public-scene"][data-scene-state="ready"]', { timeout: 60_000 })
  await page.waitForFunction(() => {
    const t = [...document.querySelectorAll('[data-view-tile]')]
    return t.length > 0 && t.every((x) => x.getAttribute('data-view-state') === 'ready')
  }, null, { timeout: 60_000 })
}

const syncStats = (page: Page) => page.evaluate(() => {
  const d = (document.querySelector('[data-sample-right]') as HTMLElement | null)?.dataset ?? {}
  const hist: Record<string, number> = {}
  for (const kv of (d.syncHist ?? '').split(',').filter(Boolean)) {
    const [k, n] = kv.split(':')
    hist[k!] = Number(n)
  }
  return { presented: Number(d.syncPresented ?? 0), maxDev: d.syncMaxDev ? Number(d.syncMaxDev) : null, videos: Number(d.syncVideos ?? 0), blocked: Number(d.guardBlocked ?? 0), hist }
})

/** 阶段内的偏差分布（累计直方图之差）与最大绝对偏差 */
function phaseHist(a: Record<string, number>, b: Record<string, number>): { hist: Record<string, number>; frames: number; maxAbs: number | null } {
  const hist: Record<string, number> = {}
  for (const k of Object.keys(b)) {
    const n = b[k]! - (a[k] ?? 0)
    if (n > 0) hist[k] = n
  }
  const ks = Object.keys(hist).map(Number)
  return { hist, frames: Object.values(hist).reduce((x, y) => x + y, 0), maxAbs: ks.length ? Math.max(...ks.map(Math.abs)) : null }
}

/** 点击时间线滑条到 tS（暂停时） */
async function seekTo(page: Page, tS: number): Promise<void> {
  const box = (await page.locator('[data-sample-timeline] [data-slot="slider"]').boundingBox())!
  const dur = await page.evaluate(() => {
    const m = /\/ (\d+):(\d+\.\d)/.exec(document.querySelector('[data-sample-clock]')?.textContent ?? '')
    return m ? Number(m[1]) * 60 + Number(m[2]) : 1
  })
  await page.mouse.click(box.x + 8 + (box.width - 16) * (tS / dur), box.y + box.height / 2)
}

/** 一个动画帧内读取播放头与每路视频画面顶部条码（16 位帧号）；暂停时偏差 = 条码帧号 − ⌊播放头 × 10⌋ */
const barcodes = (page: Page) => page.evaluate(() => new Promise<{ tS: number; frames: { id: string; frame: number; outside: boolean }[] }>((res) => {
  requestAnimationFrame(() => {
    const tS = Number(document.querySelector('[data-sample-clock]')?.getAttribute('data-sample-clock'))
    const c = document.createElement('canvas')
    c.width = 640
    c.height = 48
    const g = c.getContext('2d', { willReadFrequently: true })!
    const frames = [...document.querySelectorAll<HTMLVideoElement>('[data-view-tile] video')].map((v) => {
      g.drawImage(v, 0, 0, 640, 48, 0, 0, 640, 48)
      const row = g.getImageData(0, 24, 640, 1).data
      let k = 0
      for (let b = 0; b < 16; b++) k = (k << 1) | (row[(b * 40 + 20) * 4]! > 128 ? 1 : 0)
      const tile = v.closest('[data-view-tile]')!
      return { id: tile.getAttribute('data-view-tile') ?? '', frame: k, outside: tile.hasAttribute('data-view-outside') }
    })
    res({ tS, frames })
  })
}))

test('Tier S：只读样例页、事件回放、多机视频同步 ≤ 1 帧、数据检视为切片下载、只发起静态 GET', async () => {
  test.skip(TIER !== 'S', '本用例验证 Tier S（软件渲染）路径')
  test.setTimeout(400_000)
  const page = await ctxS!.newPage()
  const rec = recordRequests(page)
  await openRun(page)
  const layout = await page.evaluate(() => ({
    limit: Number(document.querySelector('[data-figure="lab-public-views"]')?.getAttribute('data-view-limit')),
    mode: document.querySelector('[data-figure="lab-public-views"]')?.getAttribute('data-view-mode'),
    tiles: [...document.querySelectorAll('[data-view-tile]')].map((x) => `${x.getAttribute('data-view-tile')}:${x.getAttribute('data-view-kind')}`),
    actors: Number(document.querySelector('[data-figure="lab-public-scene"]')?.getAttribute('data-scene-actors')),
    uavs: Number(document.querySelector('[data-figure="lab-public-scene"]')?.getAttribute('data-scene-uavs')),
    // 主得分与跨策略比较：除"样例运行，不构成排名"声明外，页面文字中没有得分、排行与四项指标名
    text: (() => {
      const c = document.body.cloneNode(true) as HTMLElement
      c.querySelectorAll('[data-sample-notice]').forEach((n) => n.remove())
      return c.innerText
    })(),
  }))
  expect(layout.limit).toBe(4)
  expect(layout.tiles.length).toBeGreaterThanOrEqual(3)
  expect(layout.uavs).toBe(3)
  expect(layout.actors).toBe(5)                                    // golden 磁带的 5 名脚本演员（背景演员不显示）
  expect(await page.locator('[data-scene-actor]').count()).toBeLessThanOrEqual(5)
  expect(layout.text).not.toMatch(/得分|排行|排名|leaderboard|ranking|\bscores?\b|\b(?:EC|EU|AO|CE)\b/i)
  // 1× 播放 12 s：页面合成帧偏差（rVFC）
  await seekTo(page, 3)
  await page.waitForTimeout(800)
  const load = [loadPerCore()]
  const a = await syncStats(page)
  await page.click('[data-sample-play]')
  const marks = [a]
  for (let i = 0; i < 12; i++) {
    await page.waitForTimeout(1000)
    marks.push(await syncStats(page))
  }
  const b = marks.at(-1)!
  await page.click('[data-sample-play]')
  load.push(loadPerCore())
  const judged = TIER === 'B' || Math.max(...load) <= 0.5
  const play = phaseHist(a.hist, b.hist)
  const perSecond = marks.slice(1).map((m, i) => phaseHist(marks[i]!.hist, m.hist).hist)
  // 暂停拖动 6 次：画面条码与播放头对应帧一致
  const scrub: { tS: number; devs: number[] }[] = []
  for (const f of [5.3, 22.1, 9.8, 41.6, 1.2, 30.4]) {
    await seekTo(page, f)
    await page.waitForTimeout(800)
    const s = await barcodes(page)
    scrub.push({ tS: s.tS, devs: s.frames.filter((x) => !x.outside).map((x) => x.frame - Math.floor(s.tS * FPS + 1e-6)) })
  }
  const scrubMax = Math.max(...scrub.flatMap((s) => s.devs.map(Math.abs)))
  // 数据检视：Tier S 为切片与底图下载
  await page.click('[data-sample-inspect-toggle]')
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'unsupported', { timeout: 30_000 })
  await expect(page.locator('[data-inspect-unsupported]')).toHaveAttribute('data-inspect-unsupported', 'software')
  const links = await page.evaluate(() => [...document.querySelectorAll('[data-sample-slices] a')].map((x) => x.getAttribute('href') ?? ''))
  expect(links.length).toBe(site.slices.length + 1)
  for (const h of links) expect(h).toMatch(/^\/lab-static\/v\/[^/]+\/runs\/.+\.rrd$/)
  const copy = await shellCopyFindings(page, '[data-sample-inspect]')
  const req = checkStaticOnly(rec)
  writeResult('lab-public-samples-tier-s', {
    run_id: site.runId, real_slices: site.real, layout: { ...layout, text: undefined }, play_1x: play, play_judged: judged, host_load_per_core: load, per_second: perSecond, play_presented: b.presented - a.presented, scrub, scrub_max: scrubMax,
    links, copy, requests: req, guard_blocked: b.blocked,
  })
  expect(scrubMax, '暂停拖动后画面帧号与播放头').toBeLessThanOrEqual(1)
  // 播放中的偏差是设备性能类度量：只在主机空闲（1 min 负载 ≤ 0.5 × 核数，播放前后各取一次）时判定，否则记为未判定并注明
  if (judged) {
    expect(play.frames, '1× 播放期间已呈现的视频帧').toBeGreaterThan(50)
    expect(play.maxAbs!, '多机视频与播放头偏差（帧）').toBeLessThanOrEqual(1)
  } else {
    test.info().annotations.push({ type: 'not-judged', description: `host load ${JSON.stringify(load)}; play_1x max ${play.maxAbs} frames` })
  }
  expect(copy.findings).toEqual([])
  expect(copy.texts.length).toBeGreaterThan(3)
  await page.close()
})

test('Tier B：数据检视面板打开静态切片与 basemap.rrd，首帧可见；不发起 catalog 请求', async () => {
  test.setTimeout(600_000)
  const page = await ctxB.newPage()
  const rec = recordRequests(page)
  await traceInspectStates(page)
  await openRun(page)
  const limit = Number(await page.locator('[data-figure="lab-public-views"]').getAttribute('data-view-limit'))
  expect(limit).toBe(6)
  // 1× 播放 8 s（6 路并排）：Tier B 正式断言 ≤ 1 帧；本机仿真（软件解码 6 路）只记录
  await seekTo(page, 3)
  await page.waitForTimeout(800)
  const a = await syncStats(page)
  await page.click('[data-sample-play]')
  await page.waitForTimeout(8_000)
  await page.click('[data-sample-play]')
  const play = phaseHist(a.hist, (await syncStats(page)).hist)
  // 数据检视：起点为点击（面板挂载并开始加载 Viewer）
  await seekTo(page, 20)
  await page.waitForTimeout(500)
  const t0 = await page.evaluate(() => performance.now())
  await page.click('[data-sample-inspect-toggle]')
  const ff = await measureFirstFrame(page, t0, { timeoutMs: 300_000, shots: join(OUT_DIR, 'lab-public-samples-first-frame') })
  await expect(page.locator('[data-inspect-panel]')).toHaveAttribute('data-inspect-state', 'ready')
  const copy = await shellCopyFindings(page, '[data-sample-inspect]')
  await page.waitForTimeout(2000)
  const srv = site.requests()
  const rrd = srv.filter((x) => x.p.endsWith('.rrd')).map((x) => x.p)
  const blocked = (await syncStats(page)).blocked
  const req = checkStaticOnly(rec)
  const out = writeResult('lab-public-samples-tier-b', {
    run_id: site.runId, real_slices: site.real, slices: site.slices, basemap_bytes: site.basemapBytes, first_frame: ff, play_1x_6way: play, rrd_requests: rrd,
    host_load_per_core: loadPerCore(),
    guard_blocked: blocked, copy, requests: req,
    note: TIER === 'S' ? 'Tier B 仿真（渲染器字符串仿真，实际为 SwiftShader 渲染与软件解码）：开发参考，不作正式值' : 'Tier B 参考设备',
  })
  test.info().annotations.push({ type: 'first-frame', description: `${JSON.stringify(ff.at)} visible=${ff.visibleMs} ms → ${out}` })
  expect(rrd.some((p) => p.endsWith('/basemap.rrd')), 'basemap.rrd 已由 Viewer 读取').toBe(true)
  expect(rrd.filter((p) => !p.endsWith('/basemap.rrd')).length, '切片已由 Viewer 读取').toBeGreaterThan(0)
  expect(ff.visibleMs, 'Viewer 画面出现录制内容').not.toBeNull()
  expect(copy.findings).toEqual([])
  if (TIER === 'B') {
    expect(ff.visibleMs!).toBeLessThanOrEqual(FIRST_FRAME_MAX_MS)
    expect(play.maxAbs!).toBeLessThanOrEqual(1)
  }
  await page.close()
})
