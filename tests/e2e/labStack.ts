// D3 数据检视 Playwright 用例（R07：lab/inspect、lab/public-samples；D3-AC-25、D3-AC-21）的共用部分：测试栈的构建与拉起
// （tests/e2e/lab_stack.py，`.venv-d3`）、面板状态时间线与首帧可见的测量、请求记录与凭据检查、外壳文案的 copy-lint。
//
// 设备档（AWR-05 §6.1.6；ADR-203）：`AWR_LAB_TIER` 缺省 `S`，即本机 Chrome for Testing + SwiftShader（Tier S）。此时内部页以
// 测试构建的 `?inspect=force` 启用面板，公开页（生产构建，无测试开关）以 WebGL 渲染器字符串仿真硬件 GPU，数字只作开发参考；
// `AWR_LAB_TIER=B` 表示在 Tier B 参考设备上运行（真实 GPU，不仿真），首帧 ≤ 5 s 按正式阈值断言。远端设备上的浏览器以
// `AWR_PW_CONNECT=<ws 端点>`（该设备上 `npx playwright run-server`）接入，测试栈以 `AWR_LAB_HOST=<本机对该设备可达的地址>` 监听。
//
// 输入（均可缺省，缺省时合成）：`AWR_LAB_RUN_EXPORT` 真实运行导出目录（export.json + obs.<uav>.rrd），`AWR_LAB_SLICES` R04 public
// 切片输出目录（slices.json + 切片 + basemap.rrd），`AWR_LAB_TEST_DIST` 内部测试构建，`AWR_LAB_DIST` /lab/ 公开构建
// （缺省 .cache/deploy/emax/lab-dist，即 `make lab-build` 的输出）。结果写 `AWR_R07_OUT`（缺省 runs/playwright/r07）。
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { chromium, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { checkText } from '../../tools/lint/copy-lint.mjs'

export const ROOT = resolve(import.meta.dirname, '../..')
export const TIER: 'S' | 'B' = process.env.AWR_LAB_TIER === 'B' ? 'B' : 'S'
/** D3-AC-25：切片与 basemap.rrd、catalog 按需取数的首帧可见 ≤ 5 s（暂定，P0b 实测后以 ADR 冻结） */
export const FIRST_FRAME_MAX_MS = 5_000
/** D3-AC-25：误差 ≤ 1 帧（≤ 100 ms 仿真时间） */
export const FRAME_NS = 100_000_000
export const OUT_DIR = resolve(process.env.AWR_R07_OUT ?? join(ROOT, 'runs/playwright/r07'))
const PY = process.env.AWR_PY_D3 ?? join(ROOT, '.venv-d3/bin/python')
const HOST = process.env.AWR_LAB_HOST ?? '127.0.0.1'

export async function freePort(): Promise<number> {
  return new Promise((ok, err) => {
    const s = createServer()
    s.once('error', err)
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      s.close(() => ok(typeof a === 'object' && a ? a.port : 0))
    })
  })
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function py(args: string[], opts: { cwd?: string } = {}): ChildProcess {
  return spawn(PY, [join(ROOT, 'tests/e2e/lab_stack.py'), ...args], {
    cwd: opts.cwd ?? ROOT, stdio: ['ignore', 'pipe', 'pipe'], detached: true,
    env: { ...process.env, PYTHONPATH: [join(ROOT, 'python'), join(ROOT, 'tests/e2e')].join(':'), PYTHONUNBUFFERED: '1' },
  })
}

function stop(p: ChildProcess | null): void {
  if (!p || p.exitCode !== null || p.pid === undefined) return
  try {
    process.kill(-p.pid, 'SIGTERM')
  } catch {
    // 已退出
  }
}

/** 内部测试构建（VITE_AWR_TEST_SWITCHES=1、lab 路由组）；给出 AWR_LAB_TEST_DIST 时直接使用 */
function internalDist(tmp: string): string {
  const given = process.env.AWR_LAB_TEST_DIST
  if (given) return resolve(given)
  const out = join(tmp, 'web-test')
  const r = spawnSync('npx', ['vite', 'build', '--logLevel', 'error', '--outDir', out, '--emptyOutDir'], {
    cwd: join(ROOT, 'apps/web'), stdio: 'inherit',
    env: { ...process.env, VITE_AWR_TEST_SWITCHES: '1', VITE_AWR_EXT_ROUTES: 'lab,lab-login,lab-board,lab-submissions,lab-run', AWR_BUILD_CHANNEL: 'local' },
  })
  if (r.status !== 0) throw new Error('internal test build failed')
  return out
}

export interface InternalStack {
  url: string
  runId: string
  /** 有运行目录、未注册到 catalog 的运行（BN-57 404 305） */
  orphanRun: string
  uavs: string[]
  actors: string[]
  durationS: number
  real: boolean
  tokens: { researcher: string; evaluator: string }
  /** 已部署的内部工作区（AWR_LAB_BASE，生产构建，无测试钩子） */
  remote: boolean
  close(): Promise<void>
}

/**
 * 已部署的内部工作区（如 dmax lab：AWR_LAB_BASE=https://10.253.20.5:18660）：AWR_LAB_RUN 为已注册的运行，AWR_LAB_TOKEN 为研究者
 * bench token（只经环境变量传入，不写入结果）。生产构建没有测试钩子，只运行鉴权与首帧用例。
 */
function remoteStack(): InternalStack | null {
  const base = process.env.AWR_LAB_BASE
  if (!base) return null
  const tok = process.env.AWR_LAB_TOKEN ?? ''
  if (!process.env.AWR_LAB_RUN || tok.length < 8) throw new Error('AWR_LAB_BASE needs AWR_LAB_RUN and AWR_LAB_TOKEN')
  return {
    url: base.replace(/\/$/, ''), runId: process.env.AWR_LAB_RUN, orphanRun: '', uavs: [], actors: [], durationS: 0, real: true,
    tokens: { researcher: tok, evaluator: process.env.AWR_LAB_TOKEN_EVAL ?? '' }, remote: true, close: async () => {},
  }
}

/** 内部工作区同源栈：测试构建 + bench-api + rerun-proxy + 两个真实 Rerun catalog 服务（给出 AWR_LAB_BASE 时改用已部署的工作区） */
export async function startInternalStack(): Promise<InternalStack> {
  const remote = remoteStack()
  if (remote) return remote
  const tmp = mkdtempSync(join(process.env.AWR_LAB_TMP ?? tmpdir(), 'awr-r07-int-'))
  const dist = internalDist(tmp)
  const port = await freePort()
  const args = ['serve-internal', '--dist', dist, '--root', join(tmp, 'stack'), '--port', String(port), '--host', HOST]
  if (process.env.AWR_LAB_RUN_EXPORT) args.push('--export', resolve(process.env.AWR_LAB_RUN_EXPORT))
  mkdirSync(join(tmp, 'stack'), { recursive: true })
  const p = py(args)
  const log: string[] = []
  p.stdout?.on('data', (b: Buffer) => log.push(b.toString()))
  p.stderr?.on('data', (b: Buffer) => log.push(b.toString()))
  const ready = join(tmp, 'stack', 'ready.json')
  const t0 = Date.now()
  while (!existsSync(ready)) {
    if (p.exitCode !== null || Date.now() - t0 > 180_000) {
      stop(p)
      rmSync(tmp, { recursive: true, force: true })
      throw new Error(`lab stack did not start:\n${log.join('').slice(-4000)}`)
    }
    await sleep(200)
  }
  const r = JSON.parse(readFileSync(ready, 'utf8')) as { url: string; run_id: string; orphan_run: string; uavs: string[]; actors: string[]; duration_s: number; real: boolean
    tokens: { researcher: string; evaluator: string } }
  return {
    url: r.url, runId: r.run_id, orphanRun: r.orphan_run, uavs: r.uavs, actors: r.actors, durationS: r.duration_s, real: r.real, tokens: r.tokens, remote: false,
    close: async () => {
      stop(p)
      await sleep(500)
      rmSync(tmp, { recursive: true, force: true })
    },
  }
}

export interface PublicSite {
  url: string
  runId: string
  real: boolean
  slices: [string, number][]
  basemapBytes: number
  requests(): { m: string; p: string; s: number }[]
  close(): Promise<void>
}

/** /lab/ 公开构建 + /lab-static/ 夹具（请求逐条记录） */
export async function startPublicSite(): Promise<PublicSite> {
  const dist = resolve(process.env.AWR_LAB_DIST ?? join(ROOT, '.cache/deploy/emax/lab-dist'))
  if (!existsSync(join(dist, 'index.html'))) throw new Error(`no /lab/ build at ${dist} (make lab-build)`)
  const tmp = mkdtempSync(join(process.env.AWR_LAB_TMP ?? tmpdir(), 'awr-r07-pub-'))
  const mk = spawnSync(PY, [join(ROOT, 'tests/e2e/lab_stack.py'), 'make-public-site', '--out', tmp,
    ...(process.env.AWR_LAB_SLICES ? ['--slices', resolve(process.env.AWR_LAB_SLICES)] : [])], {
    cwd: ROOT, encoding: 'utf8', env: { ...process.env, PYTHONPATH: join(ROOT, 'python') },
  })
  if (mk.status !== 0) throw new Error(`fixture build failed: ${mk.stderr.slice(-3000)}`)
  const made = JSON.parse(mk.stdout.trim().split('\n').at(-1)!) as { run_id: string; real: boolean; slices: [string, number][]; basemap: number }
  const port = await freePort()
  const logPath = join(tmp, 'requests.jsonl')
  writeFileSync(logPath, '')
  const p = py(['serve-public', '--dist', dist, '--static', join(tmp, 'lab-static'), '--log', logPath, '--port', String(port), '--host', HOST])
  const url = `http://${HOST}:${port}`
  const t0 = Date.now()
  for (;;) {
    try {
      const r = await fetch(`${url}/lab/`)
      if (r.ok) break
    } catch {
      // 尚未监听
    }
    if (p.exitCode !== null || Date.now() - t0 > 30_000) throw new Error('static server did not start')
    await sleep(200)
  }
  writeFileSync(logPath, '')
  return {
    url, runId: made.run_id, real: made.real, slices: made.slices, basemapBytes: made.basemap,
    requests: () => readFileSync(logPath, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l) as { m: string; p: string; s: number }),
    close: async () => {
      stop(p)
      await sleep(300)
      rmSync(tmp, { recursive: true, force: true })
    },
  }
}

/**
 * Tier B 仿真（只用于本机开发参考）：WebGL 渲染器字符串报为硬件 GPU，渲染仍为 SwiftShader，使生产构建的设备档判定为 Tier B。
 */
export async function emulateHardwareRenderer(ctx: BrowserContext): Promise<void> {
  await ctx.addInitScript(() => {
    for (const C of [WebGLRenderingContext, WebGL2RenderingContext]) {
      const g = C.prototype.getParameter
      C.prototype.getParameter = function (p: number) {
        return p === 0x9246 || p === 0x1F01 ? 'ANGLE (NVIDIA, NVIDIA RTX A4000 Direct3D11 vs_5_0 ps_5_0)' : g.call(this, p)
      }
    }
  })
}

/** 远端设备上的浏览器（AWR_PW_CONNECT）；未给出时返回 null，用例使用 Playwright 自带的浏览器 */
export async function remoteBrowser(): Promise<Browser | null> {
  const ws = process.env.AWR_PW_CONNECT
  return ws ? chromium.connect(ws) : null
}

// ── 面板状态与首帧 ───────────────────────────────────────────────────────────────────────────────────────────────────────

/**
 * 在页面中记录面板状态迁移（`[data-inspect-panel]` 的 `data-inspect-state`）的时刻（performance.now）。不依赖测试钩子，生产构建
 * 同样可用；addInitScript 注入，页面导航后仍生效。
 */
export async function traceInspectStates(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __r07States: [string, number][] }
    w.__r07States = []
    const seen = new WeakMap<Element, string>()
    const scan = () => {
      for (const el of document.querySelectorAll('[data-inspect-panel]')) {
        const s = el.getAttribute('data-inspect-state') ?? ''
        if (seen.get(el) !== s) {
          seen.set(el, s)
          w.__r07States.push([s, performance.now()])
        }
      }
    }
    new MutationObserver(scan).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-inspect-state'] })
  })
}

export const inspectStates = (page: Page): Promise<[string, number][]> =>
  page.evaluate(() => (window as unknown as { __r07States?: [string, number][] }).__r07States ?? [])

/**
 * Viewer 区域截图的缩略灰度（在页面内解码 PNG，不引入解码依赖）：64 × 40 格的平均亮度与该区域的颜色数。用于判断画面何时稳定在
 * 当前播放头时刻的完整画面。
 */
async function viewerThumb(page: Page, sel: string): Promise<{ g: number[]; colors: number } | null> {
  const box = await page.locator(sel).first().boundingBox()
  if (!box || box.width < 50 || box.height < 50) return null
  const png = await page.screenshot({ clip: box, animations: 'disabled' })
  return page.evaluate(async (b64) => {
    const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const bmp = await createImageBitmap(new Blob([bin], { type: 'image/png' }))
    const c = new OffscreenCanvas(bmp.width, bmp.height)
    const ctx = c.getContext('2d')!
    ctx.drawImage(bmp, 0, 0)
    const d = ctx.getImageData(0, 0, bmp.width, bmp.height).data
    const GW = 64, GH = 40
    const sum = new Float64Array(GW * GH), cnt = new Float64Array(GW * GH)
    const hist = new Set<number>()
    for (let y = 0; y < bmp.height; y += 2) {
      for (let x = 0; x < bmp.width; x += 2) {
        const i = 4 * (y * bmp.width + x)
        const k = Math.min(GH - 1, Math.floor((y * GH) / bmp.height)) * GW + Math.min(GW - 1, Math.floor((x * GW) / bmp.width))
        sum[k] += 0.299 * d[i]! + 0.587 * d[i + 1]! + 0.114 * d[i + 2]!
        cnt[k]++
        hist.add(((d[i]! >> 3) << 10) | ((d[i + 1]! >> 3) << 5) | (d[i + 2]! >> 3))
      }
    }
    return { g: Array.from(sum, (v, k) => v / Math.max(cnt[k]!, 1)), colors: hist.size }
  }, png.toString('base64'))
}

/**
 * Viewer 区域中纹理最丰富处（相机图像）的页面坐标：按 64 × 40 格计算亮度标准差，在横向 [xFrom, 1]、纵向 [0.06, 0.85]（避开 Viewer
 * 标签栏与时间面板）内取 5 × 5 窗口标准差之和最大的格子中心。用于在 Viewer 中点选图像实体，不依赖自动版面的具体位置。
 */
export async function texturedPoint(page: Page, sel: string, xFrom = 0.5): Promise<{ x: number; y: number } | null> {
  const box = await page.locator(sel).first().boundingBox()
  if (!box) return null
  const png = await page.screenshot({ clip: box, animations: 'disabled' })
  const cell = await page.evaluate(async ([b64, x0]) => {
    const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const bmp = await createImageBitmap(new Blob([bin], { type: 'image/png' }))
    const c = new OffscreenCanvas(bmp.width, bmp.height)
    const ctx = c.getContext('2d')!
    ctx.drawImage(bmp, 0, 0)
    const d = ctx.getImageData(0, 0, bmp.width, bmp.height).data
    const GW = 64, GH = 40
    const s1 = new Float64Array(GW * GH), s2 = new Float64Array(GW * GH), n = new Float64Array(GW * GH)
    for (let y = 0; y < bmp.height; y++) {
      for (let x = 0; x < bmp.width; x++) {
        const i = 4 * (y * bmp.width + x)
        const l = 0.299 * d[i]! + 0.587 * d[i + 1]! + 0.114 * d[i + 2]!
        const k = Math.min(GH - 1, Math.floor((y * GH) / bmp.height)) * GW + Math.min(GW - 1, Math.floor((x * GW) / bmp.width))
        s1[k] += l
        s2[k] += l * l
        n[k]++
      }
    }
    const sd = Array.from(s1, (v, k) => Math.sqrt(Math.max(0, s2[k]! / n[k]! - (v / n[k]!) ** 2)))
    let best = -1, at = [0, 0]
    for (let gy = Math.ceil(GH * 0.06) + 2; gy < Math.floor(GH * 0.85) - 2; gy++) {
      for (let gx = Math.ceil(GW * (x0 as number)) + 2; gx < GW - 2; gx++) {
        let sum = 0
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) sum += sd[(gy + dy) * GW + gx + dx]!
        if (sum > best) {
          best = sum
          at = [gx, gy]
        }
      }
    }
    return { gx: at[0]!, gy: at[1]!, score: best }
  }, [png.toString('base64'), xFrom] as const)
  return cell.score > 0 ? { x: box.x + ((cell.gx + 0.5) * box.width) / 64, y: box.y + ((cell.gy + 0.5) * box.height) / 40 } : null
}

/** 两幅缩略灰度中差异超过 12 级的格子占比 */
const thumbDiff = (a: number[], b: number[]): number => a.reduce((n, v, i) => n + (Math.abs(v - b[i]!) > 12 ? 1 : 0), 0) / a.length

export interface FirstFrame {
  /** 起点（performance.now，ms）：导航开始或打开数据检视的点击 */
  t0: number
  states: [string, number][]
  /** 各状态首次出现距起点（ms） */
  at: Record<string, number>
  /**
   * 首帧可见距起点（ms）：READY 之后 Viewer 区域截图首次与最终稳定画面一致（差异格 ≤ 2%）的时刻，即当前播放头时刻的完整画面（录制
   * 的视图与数据均已绘出）。截图轮询的完成时刻，为上界；画面未在 60 s 内稳定为 null
   */
  visibleMs: number | null
  /** READY（recording_open）到首帧可见（ms） */
  readyToVisibleMs: number | null
  /** OPENING（交给 Viewer 打开）到首帧可见（ms） */
  openToVisibleMs: number | null
  /** 稳定画面的颜色数（空白 Viewer 只有背景色） */
  colors: number | null
  samples: number
}

/**
 * 首帧可见：等面板 READY（recording_open），随后约每 150 ms 截取 Viewer 区域，直到画面连续 3 s 不变（最长 60 s）；首帧时刻取其后
 * 一直与最终画面一致的最早一次截图。
 */
export async function measureFirstFrame(page: Page, t0: number, opts: { timeoutMs?: number; shots?: string } = {}): Promise<FirstFrame> {
  await page.waitForSelector('[data-inspect-panel][data-inspect-state="ready"], [data-inspect-panel][data-inspect-state="error"]', { timeout: opts.timeoutMs ?? 180_000 })
  if (opts.shots) mkdirSync(dirname(opts.shots), { recursive: true })
  const seq: { t: number; g: number[]; colors: number }[] = []
  const until = Date.now() + 60_000
  while (Date.now() < until) {
    const th = await viewerThumb(page, '[data-inspect-viewer]')
    const t = (await page.evaluate(() => performance.now())) - t0
    if (th) seq.push({ t, ...th })
    const last = seq.at(-1)
    if (last && seq.length > 3) {
      const stableFrom = [...seq].reverse().find((x) => thumbDiff(x.g, last.g) > 0.02)
      if (last.t - (stableFrom?.t ?? seq[0]!.t) >= 3000) break
    }
    await sleep(150)
  }
  const states = await inspectStates(page)
  const at: Record<string, number> = {}
  for (const [s, t] of states) if (!(s in at) && t >= t0 - 1) at[s] = Math.round(t - t0)
  const fin = seq.at(-1)
  let visible: number | null = null
  if (fin && fin.colors > 8) {
    for (let i = seq.length - 1; i >= 0 && thumbDiff(seq[i]!.g, fin.g) <= 0.02; i--) visible = seq[i]!.t
  }
  if (opts.shots) await page.locator('[data-inspect-viewer]').screenshot({ path: `${opts.shots}-visible.png` })
  const r = (x: number | undefined) => (visible === null || x === undefined ? null : Math.round(visible - x))
  return {
    t0, states, at, visibleMs: visible === null ? null : Math.round(visible), readyToVisibleMs: r(at.ready), openToVisibleMs: r(at.opening),
    colors: fin?.colors ?? null, samples: seq.length,
  }
}

// ── 凭据与文案 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

/** URL 中的凭据形态（与 ui/lab/inspect/inspect.ts CREDENTIAL_RE 同一口径，另加给定的明文令牌） */
export function credentialIn(url: string, secrets: readonly string[]): boolean {
  if (/[?&#](?:token|access_token|auth|authorization|key|sig|signature|d3_rr|d3_session)=|d3(?:st|rr|bt)_[A-Za-z0-9]|\/\/[^/@]+@/i.test(url)) return true
  return secrets.some((s) => s.length >= 8 && url.includes(s))
}

/** 页面上可见外壳文案（含 aria-label、title）逐条过 copy-lint（D3-AC-23 规则集，按文字判定中英文） */
export interface CopyCheck { texts: string[]; findings: { rule: string; match: string; text: string }[] }
export async function shellCopyFindings(page: Page, sel: string): Promise<CopyCheck> {
  const texts = await page.evaluate((s) => {
    const out: string[] = []
    for (const root of document.querySelectorAll(s)) {
      const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const v = (n.textContent ?? '').trim()
        if (v && (n.parentElement?.closest('[data-inspect-viewer]') === null)) out.push(v)
      }
      for (const el of root.querySelectorAll('[aria-label],[title]')) {
        for (const a of ['aria-label', 'title']) {
          const v = el.getAttribute(a)
          if (v) out.push(v)
        }
      }
    }
    return [...new Set(out)]
  }, sel)
  const out: { rule: string; match: string; text: string }[] = []
  for (const t of texts) {
    const lang = /[㐀-鿿]/.test(t) ? 'zh' : 'en'
    for (const f of checkText('rendered', t, { lang, kind: 'text' }) as { rule: string; match: string }[]) out.push({ rule: f.rule, match: f.match, text: t })
  }
  return { texts, findings: out }
}

/** 结果写入 OUT_DIR/<name>.json（含设备档与时间，UTC） */
export function writeResult(name: string, data: unknown): string {
  mkdirSync(OUT_DIR, { recursive: true })
  const p = join(OUT_DIR, `${name}.json`)
  writeFileSync(p, JSON.stringify({ tier: TIER, at_utc: new Date().toISOString(), ...(data as object) }, null, 1))
  return p
}
