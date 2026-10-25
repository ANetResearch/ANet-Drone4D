// Playwright `lab/mbl`（F09；D3-AC-56 的内部工作区部分：原型榜、内部提交、样例包；AWR-05 §10.2 P0a 出口、§11 D3-AC-56；
// AWR-14 §21.2.2、§21.15；AWR-17 §18.4 BN-83、BN-84、BN-85、BN-96、BN-97；M27 §6.7.9）。
// 被测数据是 dmax 上正式口径轮次的真实产物（B10 第 2 轮 `b10-formal-r2`）：内部工作区数据根的 `boards/`（榜单版本，去掉含秘密种子的
// `config.json` 与逐 Run 的 `rows.jsonl`）、`submissions/`（提交文档与扫描记录）与报告 `mbl_e2e.json`，放在 `AWR_MBL_DATA`
// （缺省 runs/f09/mbl-data）。取得方式（只读，不含密钥）：
//   rsync -a --exclude config.json --exclude rows.jsonl dmax:/data/anet-drone4d-d3/lab/data/{boards,submissions} runs/f09/mbl-data/
//   rsync -a dmax:/data/anet-drone4d-d3/d3dev/b10r2/out/b10-formal-r2/mbl_e2e.json runs/f09/mbl-data/
// 被测栈（同源，与 lab nginx 同一路由口径）：内部构建（与 labctl.sh build 相同，无测试开关）+ 真实 bench-api，数据根为上述目录的副本。
// 给出 `AWR_LAB_BASE`（已部署的内部工作区，如 https://10.253.20.5:18660）与 `AWR_LAB_TOKEN`（研究者 bench token，只经环境变量；
// 可另给 `AWR_LAB_TOKEN_MEMBER`：提交方团队 d3-internal 的受邀成员，`AWR_LAB_TOKEN_OUTSIDER`：其他团队的受邀成员）时改测已部署的
// 工作区，期望值取自报告、`AWR_MBL_DATA` 中的榜单副本与该工作区自身的接口；派生物下载（BN-54）只在该模式下逐个核对。
//   1 原型榜（交汇与交接赛道 rendezvous_handoff）：报告中两个正式榜单版本（L1、L2）各显示四项指标点估计与 95% 置信区间、3 个参考
//     基线行；页面数字 = BN-97 响应 = 数据根 board.json = mbl_e2e 报告；列为层级、条目、团队、四项指标与四个名次，无合成分（BN-83
//     weights = null）；条目详情按测试事件分解（参考基线 20 个 Run = 10 个秘密种子 × 2 次重复，无效 0）；
//   2 内部提交：成员提交的 Agent 镜像经扫描（verdict pass）、冒烟（VALIDATING）、正式评测与评分（RECEIVED → … → DONE）后作为非基线
//     条目出现在当前榜单（非支配层级与名次），/lab/submissions 中提交方团队成员看到"已完成""已上榜"与同一组指标；
//   3 样例包：每个基线 1 个代表运行（L1、L2 两组 × 3 个基线，与报告 representative_runs 一致），3 机私有视角视频、MCAP 与指标，
//     在 /lab/replay 的样例包表中可浏览（文件菜单、设为运行 A、打开观测墙）；文档不含种子。来源优先为 bench-api `GET /samples`；
//     接口未交付或数据根未安装代表运行（本机栈）时以 `AWR_MBL_SAMPLES`（缺省 <AWR_MBL_DATA>/samples）中 sample_pack.py 写出的
//     pack.json 经页面路由注入，结果标注 `source = interposed`（只测页面）；两者都没有时该用例跳过并在结果中写明原因。代表运行已安装
//     时（内部工作区数据根）另核对 BN-53/BN-54 派生物下载（状态、字节数），并以 L2 两个代表运行同步回放 10 s：逐 100 ms 两侧同机
//     观测帧时间戳差 ≤ 1 个帧周期、全程检查一致（D3-AC-19 的同一口径）。
// 结果写 `AWR_F09_OUT`（缺省 runs/f09/out；不放在 Playwright 的 outputDir runs/playwright 下，后者每次运行开始时清空）/lab-mbl.json。
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect as baseExpect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { freePort, ROOT, shellCopyFindings } from './labStack'

test.describe.configure({ mode: 'default' })
// 内部页叠在常驻 3D 画布之上，Tier S（SwiftShader）下应用启动要预热着色器；断言等待放宽到 30 s
const expect = baseExpect.configure({ timeout: 30_000 })

const PY = process.env.AWR_PY_D3 ?? join(ROOT, '.venv-d3/bin/python')
const OUT = resolve(process.env.AWR_F09_OUT ?? join(ROOT, 'runs/f09/out'))
const DATA = resolve(process.env.AWR_MBL_DATA ?? join(ROOT, 'runs/f09/mbl-data'))
const SAMPLES = resolve(process.env.AWR_MBL_SAMPLES ?? join(DATA, 'samples'))
const REMOTE = process.env.AWR_LAB_BASE?.replace(/\/$/, '') ?? null
const API = '/api/bench/v1'
const LANE = 'rendezvous_handoff'
const EVENT = 'crop-handoff-001'
const BASELINES = ['baseline/coverage_scan', 'baseline/static_guard', 'baseline/track_share']
const METRICS = ['ec', 'eu', 'ao', 'ce'] as const
/** 参考基线每个测试事件的 Run 数：10 个秘密种子 × 2 次重复（D3-AC-56） */
const BASELINE_RUNS = 20
/** 提交流水线（AWR-17 §18.23）：接收 → 扫描 → 冒烟 → 正式评测 → 评分 → 完成 */
const PIPELINE = ['RECEIVED', 'SCANNING', 'VALIDATING', 'EVALUATING', 'SCORING', 'DONE']
const MEMBER = { name: 'mbl-member', team: 'd3-internal' }

type Metric = { value: number | null; ci95: [number, number]; rank?: number | null; tie_group?: number | null; repeat_delta?: number | null }
interface Entry { entry_id: string; team: string; display_name: string; track: string; lane: string; baseline: boolean; status: string; dominance_tier: number | null; metrics: Record<(typeof METRICS)[number], Metric> }
interface Board { board_version: string; tracks: { track: string; fleet_ref: string; obs_level: string; physics: string }[]; lanes: { lane: string; test_events: string[] }[]; entries: Entry[] }
interface Report {
  formal: boolean; formal_passed: boolean; round: string; checks: { id: string; passed: boolean }[]
  boards: Record<string, { board_version: string; entries: (Partial<Entry> & { entry_id: string })[] }>
  submission: { submission_id: string; board_version: string; state: string; history: string[]; listed: boolean }
  representative_runs: { group: string; policy: string; run_id: string }[]
}

const readJson = <T>(p: string): T => JSON.parse(readFileSync(p, 'utf8')) as T
const haveData = existsSync(join(DATA, 'mbl_e2e.json')) && existsSync(join(DATA, 'boards'))
const report: Report | null = haveData ? readJson<Report>(join(DATA, 'mbl_e2e.json')) : null

// ── 测试栈 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

const STACK_PY = String.raw`
import json, os, shutil, sys, threading, time
from pathlib import Path

cfg = json.loads(sys.argv[1])
REPO = Path(cfg["repo"])
sys.path[:0] = [str(REPO / "python"), str(REPO / "tests" / "e2e")]

import uvicorn
from awr.api.bench_main import create_app
from awr.bench.api.rerun_proxy import create_proxy_app
from awr.bench.settings import BenchSettings
from lab_stack import _Front

root = Path(cfg["root"])
origin = "http://%s:%d" % (cfg["host"], cfg["port"])
s = BenchSettings(data_root=root / "d3", allowed_origins=(origin,), secure_cookie=False)
s.data_root.mkdir(parents=True)
src = Path(cfg["data"])
for name in ("boards", "submissions", "samples"):
    if (src / name).is_dir():
        # 只复制只读数据：含秘密种子的榜单配置与逐 Run 行不进入数据根
        shutil.copytree(src / name, s.data_root / name, ignore=shutil.ignore_patterns("config.json", "rows.jsonl"))
app = create_app(s)
auth = app.state.auth
tokens = {k: auth.issue_token(v[0], v[1], tuple(v[2]))[1] for k, v in cfg["principals"].items()}
proxy = create_proxy_app(s, upstreams={"eval": "http://127.0.0.1:9", "dist": "http://127.0.0.1:9"})
server = uvicorn.Server(uvicorn.Config(_Front(app, proxy, Path(cfg["dist"])), host=cfg["host"], port=cfg["port"], log_level="warning",
                                       lifespan="on", timeout_graceful_shutdown=2))

def ready():
    while not server.started:
        time.sleep(0.05)
    tmp = root / ".ready.json"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": origin, "tokens": tokens, "data_root": str(s.data_root)}, f)
    tmp.replace(root / "ready.json")

threading.Thread(target=ready, daemon=True).start()
server.run()
`

interface Stack { url: string; tokens: { researcher: string; member: string | null; outsider: string | null }; dataRoot: string | null; close(): Promise<void> }
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function internalDist(tmp: string): string {
  const given = process.env.AWR_LAB_INTERNAL_DIST
  if (given) return resolve(given)
  const out = join(tmp, 'web')
  const r = spawnSync('npx', ['vite', 'build', '--logLevel', 'error', '--outDir', out, '--emptyOutDir'], {
    cwd: join(ROOT, 'apps/web'), stdio: 'inherit',
    env: { ...process.env, VITE_AWR_EXT_ROUTES: 'lab,lab-login,lab-board,lab-submissions,lab-run', AWR_BUILD_CHANNEL: 'local' },
  })
  if (r.status !== 0) throw new Error('internal build failed')
  return out
}

async function startStack(): Promise<Stack> {
  if (REMOTE) {
    const tok = process.env.AWR_LAB_TOKEN ?? ''
    if (tok.length < 8) throw new Error('AWR_LAB_BASE needs AWR_LAB_TOKEN')
    const opt = (v: string | undefined) => (v && v.length >= 8 ? v : null)
    return { url: REMOTE, tokens: { researcher: tok, member: opt(process.env.AWR_LAB_TOKEN_MEMBER), outsider: opt(process.env.AWR_LAB_TOKEN_OUTSIDER) }, dataRoot: null,
      close: async () => {} }
  }
  const tmp = mkdtempSync(join(process.env.AWR_LAB_TMP ?? tmpdir(), 'awr-f09-mbl-'))
  const dist = internalDist(tmp)
  const port = await freePort()
  const root = join(tmp, 'stack')
  mkdirSync(root, { recursive: true })
  writeFileSync(join(tmp, 'mbl_stack.py'), STACK_PY)
  const principals = { researcher: ['alice', 'researcher', ['d3']], member: [MEMBER.name, 'invited', [MEMBER.team]], outsider: ['ivan', 'invited', ['team-x']] }
  const cfg = { repo: ROOT, root, host: '127.0.0.1', port, dist, data: DATA, principals }
  const p: ChildProcess = spawn(PY, [join(tmp, 'mbl_stack.py'), JSON.stringify(cfg)], {
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], detached: true, env: { ...process.env, PYTHONUNBUFFERED: '1' },
  })
  const log: string[] = []
  p.stdout?.on('data', (b: Buffer) => log.push(b.toString()))
  p.stderr?.on('data', (b: Buffer) => log.push(b.toString()))
  const kill = () => {
    try {
      if (p.pid !== undefined && p.exitCode === null) process.kill(-p.pid, 'SIGTERM')
    } catch {
      // 已退出
    }
  }
  const t0 = Date.now()
  while (!existsSync(join(root, 'ready.json'))) {
    if (p.exitCode !== null || Date.now() - t0 > 120_000) {
      kill()
      rmSync(tmp, { recursive: true, force: true })
      throw new Error(`mbl stack did not start:\n${log.join('').slice(-4000)}`)
    }
    await sleep(200)
  }
  const r = readJson<{ url: string; tokens: Stack['tokens']; data_root: string }>(join(root, 'ready.json'))
  return {
    url: r.url, tokens: r.tokens, dataRoot: r.data_root,
    close: async () => {
      kill()
      await sleep(500)
      rmSync(tmp, { recursive: true, force: true })
    },
  }
}

// ── 文案与公共步骤 ───────────────────────────────────────────────────────────────────────────────────────────────

const DICT: Record<string, string> = (() => {
  const dir = join(ROOT, 'apps/web/src/app/i18n')
  const d = readJson<Record<string, string>>(join(dir, 'zh-CN.json'))
  for (const f of readdirSync(join(dir, 'lab')).filter((x) => x.endsWith('.zh-CN.json')).sort()) Object.assign(d, readJson<Record<string, string>>(join(dir, 'lab', f)))
  return d
})()
function tz(key: string, params: Record<string, string | number> = {}): string {
  const raw = DICT[key]
  if (raw === undefined) throw new Error(`missing zh-CN key ${key}`)
  return raw.replace(/\{(\w+)\}/g, (_m, k: string) => String(params[k] ?? `{${k}}`))
}
/** 页面数字格式（ui/lab/board.ts fmtValue、fmtCi） */
const fmtValue = (m: Metric) => (m.value === null ? '—' : m.value.toFixed(3))
const fmtCi = (m: Metric) => (m.value === null ? '' : `[${m.ci95[0].toFixed(3)}, ${m.ci95[1].toFixed(3)}]`)

let st: Stack
let browser0: Browser
const contexts: BrowserContext[] = []
const result: Record<string, unknown> = { data: DATA, remote: REMOTE !== null }

test.beforeAll(async ({ browser }) => {
  test.skip(!haveData && !REMOTE, `no B10 formal round data at ${DATA} (see the header of this file for the rsync commands)`)
  test.setTimeout(300_000)
  mkdirSync(OUT, { recursive: true })
  st = await startStack()
  browser0 = browser
})
test.afterAll(async () => {
  for (const c of contexts) await c.close()
  if (st) {
    mkdirSync(OUT, { recursive: true })
    writeFileSync(join(OUT, 'lab-mbl.json'), JSON.stringify({ at_utc: new Date().toISOString(), ...result }, null, 1))
  }
  await st?.close()
})

async function loggedIn(token: string, next: string): Promise<Page> {
  const c = await browser0.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1, locale: 'zh-CN', ignoreHTTPSErrors: REMOTE !== null })
  contexts.push(c)
  const page = await c.newPage()
  await page.goto(`${st.url}/lab/login?next=${encodeURIComponent(next)}`)
  await page.fill('#lab-token', token)
  await page.click('[data-view="lab-login"] button[type="submit"]')
  await page.waitForURL((u) => u.pathname === next.split('?')[0], { timeout: 60_000 })
  return page
}

async function api<T>(page: Page, path: string): Promise<{ status: number; body: T }> {
  return page.evaluate(async (p) => {
    const r = await fetch(p, { credentials: 'same-origin' })
    const txt = await r.text()
    let body: unknown = null
    try {
      body = txt ? JSON.parse(txt) : null
    } catch {
      body = txt
    }
    return { status: r.status, body }
  }, `${API}${path}`) as Promise<{ status: number; body: T }>
}

const rowKey = (e: Pick<Entry, 'track' | 'lane' | 'entry_id'>) => `${e.track}/${e.lane}/${e.entry_id}`
const sameMetric = (a: Metric | undefined, b: Metric | undefined) => !!a && !!b && a.value === b.value && a.ci95[0] === b.ci95[0] && a.ci95[1] === b.ci95[1]

// ── 用例 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

test('原型榜：交汇与交接赛道两个正式版本各显示四项指标与 95% 置信区间及 3 个参考基线行，与 board.json 和 mbl_e2e 报告一致，无合成分', async () => {
  test.setTimeout(300_000)
  expect(report, 'mbl_e2e.json').toBeTruthy()
  const rep = report!
  expect([rep.formal, rep.formal_passed], 'formal round passed all ten checks').toEqual([true, true])
  expect(rep.checks.filter((c) => !c.passed).map((c) => c.id)).toEqual([])
  const page = await loggedIn(st.tokens.researcher, '/lab/board')
  await expect(page.locator('section[data-view="lab-board"] h1')).toHaveText(tz('lab.board.title'))
  // BN-83：全部版本 weights = null（不计算合成分）
  const idx = await api<{ current: string; versions: { board_version: string; weights: unknown; score_version: string; n_entries: number }[] }>(page, '/board')
  expect(idx.status).toBe(200)
  expect(idx.body.versions.every((v) => v.weights === null)).toBe(true)
  const header = [tz('lab.board.col.tier'), tz('lab.board.col.entry'), tz('lab.board.col.team'), ...METRICS.map((k) => tz(`lab.board.metric.${k}`)),
    ...METRICS.map((k) => tz('lab.board.col.rankOf', { m: tz(`lab.board.metric.${k}`) }))]
  const shown: Record<string, unknown> = {}
  for (const [group, rb] of Object.entries(rep.boards).sort()) {
    const v = rb.board_version
    expect(idx.body.versions.map((x) => x.board_version), `board version ${v} published`).toContain(v)
    await page.selectOption(`select[aria-label="${tz('lab.board.version')}"]`, v)
    const lb = await api<Board>(page, `/leaderboard?board_version=${encodeURIComponent(v)}`)
    expect(lb.status).toBe(200)
    const board = lb.body
    expect(board.lanes.map((l) => l.lane)).toEqual([LANE])
    expect(board.lanes[0]!.test_events).toEqual([EVENT])
    expect(board.tracks).toHaveLength(1)
    const tr = board.tracks[0]!
    expect([tr.fleet_ref, tr.obs_level, tr.physics]).toEqual(['3x-p600_bench', group, 'realistic'])
    // 数据根 board.json（本机栈）与报告：同一组条目与数字
    const diskPath = join(st.dataRoot ?? DATA, 'boards', v, 'board.json')
    if (existsSync(diskPath)) {
      const disk = readJson<Board>(diskPath)
      for (const e of board.entries) {
        const d = disk.entries.find((x) => x.entry_id === e.entry_id)
        for (const k of METRICS) expect(sameMetric(e.metrics[k], d?.metrics[k]), `${v} ${e.entry_id} ${k} vs board.json`).toBe(true)
      }
    }
    for (const e of board.entries) {
      const r = rb.entries.find((x) => x.entry_id === e.entry_id)
      expect(r, `${v} ${e.entry_id} in mbl_e2e report`).toBeTruthy()
      for (const k of METRICS) expect(sameMetric(e.metrics[k], r!.metrics![k] as Metric), `${v} ${e.entry_id} ${k} vs report`).toBe(true)
    }
    // 页面
    const table = page.locator('[data-figure="lab-board-table"]')
    await expect(table.locator(`[data-row-key="${rowKey({ track: tr.track, lane: LANE, entry_id: BASELINES[0]! })}"]`)).toBeVisible()
    await expect(page.locator('[data-figure="lab-board-meta"]')).toContainText(`${tr.track} · ${tr.fleet_ref} · ${tr.obs_level} · ${tr.physics}`)
    await expect(page.locator('[data-figure="lab-board-meta"]')).toContainText(`${LANE} · ${EVENT}`)
    const heads = (await table.locator('thead th').allInnerTexts()).map((x) => x.trim())
    expect(heads).toEqual(header)
    const keys = await table.locator('tbody [data-row-key]').evaluateAll((els) => els.map((e) => e.getAttribute('data-row-key')))
    expect(keys.sort()).toEqual(board.entries.map(rowKey).sort())
    const refRows = board.entries.filter((e) => e.baseline)
    expect(refRows.map((e) => e.entry_id).sort()).toEqual(BASELINES)
    const rows: Record<string, string[]> = {}
    for (const e of board.entries) {
      const row = table.locator(`[data-row-key="${rowKey(e)}"]`)
      const cells = (await row.locator('td').allInnerTexts()).map((x) => x.replace(/\s+/g, ' ').trim())
      rows[e.entry_id] = cells
      expect(cells[1], `${e.entry_id} display name`).toContain(e.display_name)
      if (e.baseline) {
        expect(cells[1]).toContain(tz('lab.board.reference'))
        expect(cells.slice(7), 'reference baselines take no rank').toEqual(['—', '—', '—', '—'])
      }
      METRICS.forEach((k, i) => {
        expect(cells[3 + i], `${v} ${e.entry_id} ${k}`).toBe(`${fmtValue(e.metrics[k])} ${fmtCi(e.metrics[k])}`.trim())
      })
    }
    await expect(table.getByText(tz('lab.board.reference'), { exact: true })).toHaveCount(3)
    // 条目详情：参考基线按测试事件分解，20 个 Run（10 个秘密种子 × 2 次重复），无效 0
    const details: Record<string, string[]> = {}
    for (const id of BASELINES) {
      await table.locator(`[data-row-key="${rowKey({ track: tr.track, lane: LANE, entry_id: id })}"]`).click()
      const det = page.locator('[data-figure="lab-board-entry-table"]')
      await expect(page.locator('[data-figure="lab-board-entry"] h2')).toContainText(board.entries.find((e) => e.entry_id === id)!.display_name)
      await expect(det.locator(`tbody [data-row-key="${EVENT}"]`)).toBeVisible()
      const cells = (await det.locator(`tbody [data-row-key="${EVENT}"] td`).allInnerTexts()).map((x) => x.trim())
      details[id] = cells
      expect(cells.slice(0, 3), `${v} ${id} per-event breakdown`).toEqual([EVENT, String(BASELINE_RUNS), '0'])
      await table.locator(`[data-row-key="${rowKey({ track: tr.track, lane: LANE, entry_id: id })}"]`).click()    // 收起
      await expect(page.locator('[data-figure="lab-board-entry"]')).toHaveCount(0)
    }
    const cc = await shellCopyFindings(page, 'section[data-view="lab-board"]')
    expect(cc.findings, `${v} copy-lint`).toEqual([])
    shown[v] = { group, rows, details, entries: board.entries.length }
    await page.screenshot({ path: join(OUT, `lab-mbl-board-${v}.png`) })
  }
  result.board = shown
})

test('内部提交：成员提交的 Agent 镜像经扫描、冒烟、正式评测与评分后出现在原型榜；提交方团队在 /lab/submissions 看到已完成与已上榜', async () => {
  test.setTimeout(240_000)
  const sub = report!.submission
  expect(sub.history).toEqual(PIPELINE)
  expect([sub.state, sub.listed]).toEqual(['DONE', true])
  const sid = sub.submission_id
  const subDir = join(st.dataRoot ?? DATA, 'submissions')
  if (existsSync(join(subDir, `${sid}.json`))) {
    // 提交文档与扫描记录（数据根或其副本）
    const doc = readJson<{ state: string; visibility: string; _meta: { history: { state: string }[]; team: string; kind: string; board_version: string } }>(join(subDir, `${sid}.json`))
    expect([doc.state, doc.visibility, doc._meta.kind, doc._meta.team, doc._meta.board_version]).toEqual(['DONE', 'board', 'formal', MEMBER.team, sub.board_version])
    expect(doc._meta.history.map((h) => h.state)).toEqual(PIPELINE)
    const scan = readJson<{ verdict: string; errors: unknown[] }>(join(subDir, sid, 'scan.json'))
    expect([scan.verdict, scan.errors.length]).toEqual(['pass', 0])
  }
  // 原型榜：当前版本中的非基线条目（非支配层级与四项名次）
  const page = await loggedIn(st.tokens.researcher, '/lab/board')
  const idx = await api<{ current: string }>(page, '/board')
  expect(idx.body.current).toBe(sub.board_version)
  const lb = (await api<Board>(page, `/leaderboard?board_version=${sub.board_version}`)).body
  const entry = lb.entries.find((e) => e.entry_id.endsWith(`/${sid}`))
  expect(entry, `${sid} on ${sub.board_version}`).toBeTruthy()
  expect([entry!.baseline, entry!.status, entry!.team]).toEqual([false, 'valid', MEMBER.team])
  expect(entry!.dominance_tier).not.toBeNull()
  const row = page.locator(`[data-figure="lab-board-table"] [data-row-key="${rowKey(entry!)}"]`)
  await expect(row).toBeVisible()
  const cells = (await row.locator('td').allInnerTexts()).map((x) => x.replace(/\s+/g, ' ').trim())
  expect(cells[0]).toBe(String(entry!.dominance_tier))
  expect(cells[1]).not.toContain(tz('lab.board.reference'))
  for (const [i, k] of METRICS.entries()) expect(cells[7 + i], `${k} rank`).toMatch(/^=?\d+$/)
  // 提交方团队成员（受邀成员，团队 d3-internal）：/lab/submissions 行与 BN-96 详情
  const member = st.tokens.member ? await loggedIn(st.tokens.member, '/lab/submissions') : page
  if (!st.tokens.member) await page.goto(`${st.url}/lab/submissions`)
  const srow = member.locator(`[data-figure="lab-submissions"] [data-row-key="${sid}"]`)
  await expect(srow).toBeVisible()
  const scells = (await srow.locator('td').allInnerTexts()).map((x) => x.trim())
  expect(scells).toContain(tz('lab.sub.state.DONE'))
  expect(scells).toContain(tz('lab.sub.visibility.board'))
  for (const k of METRICS) expect(scells, k).toContain(fmtValue(entry!.metrics[k]))
  const det = await api<{ state: string; visibility: string; result: { lanes: { lane: string; metrics: Record<string, Metric> }[] } }>(member, `/submissions/${sid}`)
  expect([det.status, det.body.state, det.body.visibility]).toEqual([200, 'DONE', 'board'])
  expect(det.body.result.lanes[0]!.lane).toBe(LANE)
  for (const k of METRICS) expect(sameMetric(det.body.result.lanes[0]!.metrics[k], entry!.metrics[k]), `submission result ${k} = board`).toBe(true)
  // 其他团队的受邀成员看不到该提交
  let outsider: number | null = null
  if (st.tokens.outsider) {
    const o = await loggedIn(st.tokens.outsider, '/lab/submissions')
    await expect(o.locator('section[data-view="lab-submissions"]')).toBeVisible()
    outsider = (await api(o, `/submissions/${sid}`)).status
    expect([403, 404]).toContain(outsider)
    await expect(o.locator(`[data-row-key="${sid}"]`)).toHaveCount(0)
  }
  await member.screenshot({ path: join(OUT, 'lab-mbl-submissions.png') })
  result.submission = { submission_id: sid, board_version: sub.board_version, history: sub.history, tier: entry!.dominance_tier, row: cells, submissions_row: scells, outsider_status: outsider }
})

interface PackFile { kind: string; path: string; bytes: number | null; uav?: string | null; cam?: string | null }
interface Pack { schema: string; pack_id: string; event_id: string; samples: { group: string; policy: string; run_id: string; metrics: Record<string, number | null>; files: PackFile[] }[] }

/** 样例包文档：`<dir>/**\/pack.json` 或 `<dir>/*.json`（drone4d.bench.sample_pack.v1） */
function localPacks(dir: string): Pack[] {
  if (!existsSync(dir)) return []
  const out: Pack[] = []
  const walk = (d: string) => {
    for (const n of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, n.name)
      if (n.isDirectory()) walk(p)
      else if (n.name.endsWith('.json')) {
        try {
          const j = readJson<Pack>(p)
          if (j.schema === 'drone4d.bench.sample_pack.v1') out.push(j)
        } catch {
          // 非样例包文档
        }
      }
    }
  }
  walk(dir)
  return out
}

/** 页面识别的样例文件种类（ui/lab/replay/samplePack.ts SAMPLE_FILE_KINDS）；其余种类被页面丢弃，结果中单列 */
const UI_KINDS = ['video', 'mcap', 'metrics', 'tape', 'manifest', 'slice', 'tracks']

test('样例包：每个基线 1 个代表运行（L1、L2），3 机私有视角视频、MCAP 与指标在 /lab/replay 可浏览并可同步回放，文档不含种子', async () => {
  test.setTimeout(420_000)
  const page = await loggedIn(st.tokens.researcher, '/lab/board')
  const probe = await api<{ items?: Pack[] }>(page, '/samples')
  let packs: Pack[] = []
  let source: 'api' | 'interposed' | 'none' = 'none'
  // bench-api 只列出代表运行已安装到本数据根的样例；本机栈的数据根不含运行目录时样例为空，改以 pack.json 注入（只测页面）
  if (probe.status === 200 && Array.isArray(probe.body.items) && probe.body.items.some((p) => (p.samples ?? []).length > 0)) {
    packs = probe.body.items
    source = 'api'
  } else {
    packs = localPacks(SAMPLES)
    if (packs.length) source = 'interposed'
  }
  result.samples = { source, api_status: probe.status }
  test.skip(source === 'none', `no sample pack: bench-api GET /samples returned ${probe.status} and no pack.json under ${SAMPLES}`)
  if (source === 'interposed') {
    // bench-api 尚无 GET /samples：只把该接口的响应换成 sample_pack.py 写出的文档，页面其余请求照常走真实栈
    await page.route(`**${API}/samples`, (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: packs }) }))
  }
  const pack = packs.find((p) => p.event_id === EVENT)
  expect(pack, `sample pack for ${EVENT}`).toBeTruthy()
  expect(JSON.stringify(pack).match(/"seed[^"]*"\s*:/g), 'no seed fields in the sample pack document').toBeNull()
  // 每组每基线 1 个代表运行，与报告一致
  const want = report!.representative_runs.map((r) => `${r.group.toLowerCase()}/${r.policy}/${r.run_id}`).sort()
  const got = pack!.samples.map((s) => `${s.group.toLowerCase()}/${s.policy}/${s.run_id}`).sort()
  expect(got).toEqual(want)
  expect(want).toHaveLength(6)
  // 每个基线：至少一个代表运行带 3 机私有视角视频（L1 观测档没有相机通道，视频只在 L2 组）；每个代表运行都有 MCAP 与指标
  const videoUavs = (s: Pack['samples'][number]) => new Set(s.files.filter((f) => f.kind === 'video').map((f) => f.uav)).size
  for (const pol of new Set(pack!.samples.map((s) => s.policy))) {
    expect(Math.max(...pack!.samples.filter((s) => s.policy === pol).map(videoUavs)), `${pol}: private-view videos of 3 UAVs`).toBeGreaterThanOrEqual(3)
  }
  for (const s of pack!.samples) {
    expect(s.files.some((f) => f.kind === 'mcap'), `${s.run_id} MCAP`).toBe(true)
    expect(s.files.some((f) => f.kind === 'metrics'), `${s.run_id} metrics`).toBe(true)
    for (const k of METRICS) expect(typeof s.metrics[k] === 'number' || s.metrics[k] === null, `${s.run_id} ${k}`).toBe(true)
  }
  // 页面：/lab/replay 的样例包表与文件菜单
  await page.goto(`${st.url}/lab/replay`)
  const panel = page.locator('[data-figure="lab-replay-pack"]')
  await expect(panel.locator(`[data-pack-meta="${pack!.pack_id}"]`)).toBeVisible()
  const files: Record<string, string[]> = {}
  const notShown: Record<string, string[]> = {}
  const downloads: Record<string, string> = {}
  for (const s of pack!.samples) {
    const row = panel.locator(`[data-row-key="${s.group}/${s.policy}/${s.run_id}"]`)
    await expect(row).toBeVisible()
    const shown = s.files.filter((f) => UI_KINDS.includes(f.kind))
    notShown[s.run_id] = s.files.filter((f) => !UI_KINDS.includes(f.kind)).map((f) => `${f.kind}:${f.path}`)
    await row.locator(`[data-pack-files="${s.run_id}"]`).click()
    const items = page.locator('[data-pack-file]')
    await expect(items).toHaveCount(shown.length)
    files[s.run_id] = (await items.evaluateAll((els) => els.map((e) => e.getAttribute('data-pack-file') ?? ''))).sort()
    expect(files[s.run_id]).toEqual(shown.map((f) => f.path).sort())
    const hrefs = await items.evaluateAll((els) => els.map((e) => e.getAttribute('href') ?? ''))
    for (const h of hrefs) expect(h.startsWith(`${API}/runs/${encodeURIComponent(s.run_id)}/artifacts/`), h).toBe(true)
    await page.keyboard.press('Escape')
    // 派生物下载（BN-53 列表、BN-54 下载）：代表运行已安装到数据根时逐个核对状态与字节数（视频取首 64 KiB 区间）
    const listing = await api<{ items?: { path: string }[] }>(page, `/runs/${encodeURIComponent(s.run_id)}/artifacts`)
    if (listing.status !== 200) {
      downloads[s.run_id] = `BN-53 ${listing.status}`
      continue
    }
    for (const f of s.files) {
      const href = `${st.url}${API}/runs/${encodeURIComponent(s.run_id)}/artifacts/${f.path}`
      const big = (f.bytes ?? 0) > 65_536
      const r = await page.request.get(href, big ? { headers: { Range: 'bytes=0-65535' } } : {})
      expect(r.status(), `${s.run_id} ${f.path}`).toBe(big ? 206 : 200)
      if (f.bytes !== null) {
        const total = big ? Number((r.headers()['content-range'] ?? '').split('/')[1]) : (await r.body()).length
        expect(total, `${s.run_id} ${f.path} bytes`).toBe(f.bytes)
      }
    }
    downloads[s.run_id] = `ok ${s.files.length}`
  }
  // 事件回放：L2 两个代表运行设为 A、B，同步回放 10 s，逐 100 ms 两侧同机观测帧时间戳差 ≤ 1 个帧周期，全程检查一致
  const l2 = pack!.samples.filter((s) => videoUavs(s) >= 3)
  let replay: Record<string, unknown> = { skipped: 'no L2 sample with frames' }
  const runA = await api<{ has_frames?: boolean }>(page, `/runs/${encodeURIComponent(l2[0]?.run_id ?? 'x')}`)
  if (l2.length >= 2 && runA.status === 200 && runA.body.has_frames) {
    await panel.locator(`[data-pack-assign="a/${l2[0]!.run_id}"]`).click()
    await page.waitForURL((u) => u.searchParams.get('a') === l2[0]!.run_id)
    // 留在同一页面内指定运行 B（重新载入会重走应用启动的着色器预热）
    await panel.locator(`[data-pack-assign="b/${l2[1]!.run_id}"]`).click()
    await page.waitForURL((u) => u.searchParams.get('a') === l2[0]!.run_id && u.searchParams.get('b') === l2[1]!.run_id)
    await expect(page.locator('[data-replay-sweep]')).toHaveAttribute('data-replay-sweep', 'true', { timeout: 120_000 })
    const tiles = page.locator('[data-replay-tile^="a/"]')
    await expect(tiles).toHaveCount(3, { timeout: 60_000 })
    await page.getByRole('button', { name: tz('lab.replay.play') }).click()
    const samples: { a: Record<string, number>; b: Record<string, number> }[] = []
    const t0 = Date.now()
    while (Date.now() - t0 < 10_000) {
      samples.push(await page.evaluate(() => {
        const out = { a: {} as Record<string, number>, b: {} as Record<string, number> }
        for (const el of document.querySelectorAll('[data-replay-tile]')) {
          const [side, uav] = (el.getAttribute('data-replay-tile') ?? '').split('/')
          const t = el.getAttribute('data-t-ns')
          if (t && (side === 'a' || side === 'b')) out[side][uav!] = Number(t)
        }
        return out
      }))
      await sleep(100)
    }
    await page.getByRole('button', { name: tz('lab.replay.pause') }).click()
    // 帧周期：同一侧同一机相邻不同时间戳的最小正差（两侧取较大者）
    const periodOf = (side: 'a' | 'b') => {
      let p = Number.POSITIVE_INFINITY
      for (const u of Object.keys(samples[0]?.[side] ?? {})) {
        const ts = [...new Set(samples.map((x) => x[side][u]).filter((v): v is number => v !== undefined))].sort((x, y) => x - y)
        for (let k = 1; k < ts.length; k++) p = Math.min(p, ts[k]! - ts[k - 1]!)
      }
      return p
    }
    const period = Math.max(periodOf('a'), periodOf('b'))
    expect(Number.isFinite(period), 'frame period observed').toBe(true)
    let maxDelta = 0
    let n = 0
    for (const x of samples) {
      for (const u of Object.keys(x.a)) {
        if (x.b[u] === undefined) continue
        maxDelta = Math.max(maxDelta, Math.abs(x.a[u]! - x.b[u]!))
        n++
      }
    }
    const advanced = new Set(samples.map((x) => x.a[Object.keys(x.a)[0] ?? ''])).size
    replay = { a: l2[0]!.run_id, b: l2[1]!.run_id, samples: samples.length, comparisons: n, distinct_frames: advanced, period_ns: period, max_delta_ns: maxDelta }
    expect(n, 'compared frame pairs').toBeGreaterThan(50)
    expect(advanced, 'playhead advanced over frames').toBeGreaterThan(5)
    expect(maxDelta, 'per-UAV frame timestamp difference ≤ 1 frame period').toBeLessThanOrEqual(period)
    await page.screenshot({ path: join(OUT, 'lab-mbl-replay.png') })
  } else {
    // 设为运行 A：进入同步回放（事件回放入口）
    const first = pack!.samples[0]!
    await panel.locator(`[data-pack-assign="a/${first.run_id}"]`).click()
    await page.waitForURL((u) => u.searchParams.get('a') === first.run_id)
  }
  const cc = await shellCopyFindings(page, 'section[data-view="lab-replay"]')
  expect(cc.findings).toEqual([])
  await page.screenshot({ path: join(OUT, 'lab-mbl-samples.png') })
  result.samples = { source, api_status: probe.status, pack_id: pack!.pack_id, samples: got, files, not_shown_by_page: notShown, downloads, replay }
})
