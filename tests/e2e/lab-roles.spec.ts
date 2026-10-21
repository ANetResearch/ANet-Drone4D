// Playwright `lab/roles`（F09；D3-AC-51 的页面部分；AWR-05 ADR-166、§7.11；AWR-14 §21.2.2、§21.2.5、§21.3、§21.4、§21.6、§21.15；
// AWR-17 §18.3、§18.4）。被测栈（同源，与 lab nginx 同一路由口径）：内部构建（`VITE_AWR_EXT_ROUTES=lab,lab-login,lab-board,
// lab-submissions,lab-run`，与 tools/deploy/dmax/lab/labctl.sh build 相同，无测试开关）+ 真实 bench-api（会话 Cookie、x-d3-role
// 最低角色、数据范围、访问审计、RunStore 状态机）。数据根在临时目录内按用例口径构造：crop 世界、dev/test 与不可行事件、
// 四个项目的 14 个受管运行（八种状态）；run-controller 的取消确认以栈内线程代替（取消请求 1 s 后迁至 CANCELLED，时限 10 s）。
//   1 路由清单：apps/web/src/app/routes 中全部 /lab 路由模式均在本用例的路由 × 角色矩阵中（新增路由未覆盖时失败）；
//     未登录访问每条路由跳转 /lab/login?next=<原路径>，bench-api 无凭据 401；
//   2 会话：bench token 经 /lab/login 换取 d3_session（HttpOnly、SameSite=Strict、12 h），令牌不留在 URL、存储与 document.cookie；
//     无效令牌显示 302；注销后会话失效；
//   3 四类角色 × 全部 /lab 路由：页面可达或显示 115 提示；受邀成员不发起越权请求；可达页面的 bench 请求无 401/403；
//     评测视图（观测墙 view=eval、数据检视 catalog=eval）只对评测员及以上；页面外壳文案过 copy-lint；
//   4 /lab/worlds、/lab/worlds/:wid（六个标签）与 /lab/events（受邀成员只见 dev 事件；三道门与原因码）；
//   5 /lab/runs 状态机：八态徽标、排队位置、重启副文、原因码（code NAME · 短文案）、唯一 hot 行、状态侧栏的尝试与迁移审计；
//   6 取消（排队立即取消、运行中经"取消中"倒计时到已取消、批量取消）与重试（同一 run_id 新 attempt）；
//   7 数据范围：研究者对他人项目只读（无操作、直接调用 403 115 project_scope），评测员不限项目；受邀成员只见本团队 dev 运行、
//     无写操作（403 115 role）、他人运行 403 115 invited_scope；
//   8 越权审计：上述每次 403 在 audit/access-*.jsonl 恰有一条 deny 记录（主体、方法、路径、115），审计文件不含令牌明文。
// 结果写 `AWR_F09_OUT`（缺省 runs/f09/out；不放在 Playwright 的 outputDir runs/playwright 下，后者每次运行开始时清空）/lab-roles.json。内部构建可由 `AWR_LAB_INTERNAL_DIST` 给出（缺省现场构建）。
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect as baseExpect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'
import { freePort, ROOT, shellCopyFindings } from './labStack'

test.describe.configure({ mode: 'default' })
// 内部页叠在常驻 3D 画布之上，Tier S（SwiftShader）下应用启动要预热着色器，首屏可达数秒；断言等待放宽到 30 s
const expect = baseExpect.configure({ timeout: 30_000 })

const PY = process.env.AWR_PY_D3 ?? join(ROOT, '.venv-d3/bin/python')
const OUT = resolve(process.env.AWR_F09_OUT ?? join(ROOT, 'runs/f09/out'))
const API = '/api/bench/v1'
const ROLES = ['invited', 'researcher', 'evaluator', 'admin'] as const
type Role = (typeof ROLES)[number]
const RANK: Record<Role, number> = { invited: 0, researcher: 1, evaluator: 2, admin: 3 }
/** 主体（名称、角色、项目）：受邀成员 ivan（团队 team-x）、研究者 alice（项目 d3）、评测员 eve、管理员 root；zed 为他人项目研究者 */
const PRINCIPALS: Record<Role | 'other', [string, Role, string[]]> = {
  invited: ['ivan', 'invited', ['team-x']], researcher: ['alice', 'researcher', ['d3']], evaluator: ['eve', 'evaluator', []],
  admin: ['root', 'admin', []], other: ['zed', 'researcher', ['other']],
}

// 受管运行：id、项目、提交者、迁移路径（自 QUEUED 起）、划分、FAILED 原因码
interface SeedRun { id: string; tenant: string; by: string; path?: string[]; split?: string; reason?: [number, Record<string, unknown>] }
const RUNS: SeedRun[] = [
  { id: 'run-a000000001', tenant: 'd3', by: 'alice' },                                                       // 排队：立即取消
  { id: 'run-a000000002', tenant: 'd3', by: 'alice', path: ['PREPARING', 'RUNNING'] },                       // 运行中：取消中 → 已取消
  { id: 'run-a000000003', tenant: 'd3', by: 'alice', path: ['PREPARING', 'FAILED'], reason: [631, { why: 'gz_ready_timeout' }] },  // 重试
  { id: 'run-a000000004', tenant: 'd3', by: 'alice', path: ['PREPARING', 'PREPARING'] },                     // 第 1 次重启
  { id: 'run-a000000005', tenant: 'd3', by: 'alice', path: ['PREPARING', 'RUNNING', 'FINALIZING'] },
  { id: 'run-a000000006', tenant: 'd3', by: 'alice', path: ['PREPARING', 'RUNNING', 'FINALIZING', 'EVALUATING'] },
  { id: 'run-a000000007', tenant: 'd3', by: 'alice', path: ['PREPARING', 'RUNNING', 'FINALIZING', 'EVALUATING', 'COMPLETED'] },
  { id: 'run-a000000008', tenant: 'd3', by: 'alice', path: ['CANCELLED'] },
  { id: 'run-a000000009', tenant: 'd3', by: 'alice' },                                                       // 批量取消
  { id: 'run-a000000010', tenant: 'd3', by: 'alice' },                                                       // 批量取消
  { id: 'run-b000000001', tenant: 'team-x', by: 'ivan', path: ['PREPARING', 'RUNNING'] },                    // 受邀成员可见
  { id: 'run-b000000002', tenant: 'team-x', by: 'ivan', path: ['PREPARING', 'FAILED'], reason: [683, { why: 'gpu_share' }] },
  { id: 'run-c000000001', tenant: 'other', by: 'zed', path: ['PREPARING', 'FAILED'], reason: [649, { why: 'rig' }] },  // 他人项目
  { id: 'run-d000000001', tenant: 'team-x', by: 'ivan', split: 'test' },                                     // test 划分：受邀成员不可见
]
const STATES = ['QUEUED', 'PREPARING', 'RUNNING', 'FINALIZING', 'EVALUATING', 'COMPLETED', 'FAILED', 'CANCELLED'] as const
const TEAM_RUN = 'run-b000000001'

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
from awr.bench.runs_store import RunStore
from awr.bench.settings import BenchSettings
from lab_stack import _Front

root = Path(cfg["root"])
origin = "http://%s:%d" % (cfg["host"], cfg["port"])
s = BenchSettings(data_root=root / "d3", allowed_origins=(origin,), secure_cookie=False)
wd = s.worlds_dir / "oryahovo-crop1"
(wd / "qa").mkdir(parents=True)
for rel in ("world.json", "coordinate.json", "qa/report.json"):
    shutil.copyfile(REPO / "worlds" / "oryahovo-crop1" / rel, wd / rel)
WORLD = {"id": "oryahovo-crop1", "content_version": json.loads((wd / "world.json").read_text(encoding="utf-8")).get("contentVersion", "cv-1")}

def event(eid, split=None, status="compiled", gates=(), reason=None):
    d = s.data_root / "events" / eid / "v1"
    d.mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps({"event_id": eid, "version": 1, "world": WORLD, "duration_ms": 360000, "actors": []}))
    (d / "event.json").write_text(json.dumps(dict({"event_id": eid, "world": WORLD}, **({"split": split} if split else {}))))
    rep = {"event_id": eid, "version": 1, "status": status, "strict": True, "gates": list(gates)}
    if reason:
        rep["reason"] = reason
    (d / "compile_report.json").write_text(json.dumps(rep, ensure_ascii=False))

for e in cfg["events"]:
    event(e["id"], e.get("split"), e.get("status", "compiled"), e.get("gates", ()), e.get("reason"))

app = create_app(s)
app.state.runs = st = RunStore(s.db_path)
SPEC = {"schema": "drone4d.bench.run_spec.v1", "policy": {"kind": "reference", "name": "track_share"}, "physics": "realistic",
        "obs_level": "L1", "fleet": [{"model": "p600_bench", "count": 3}], "duration_s": 360.0}
for r in cfg["runs"]:
    st.enqueue(r["id"], experiment_id="exp-f09roles01", event_id="abc-crossing", world_id="oryahovo-crop1", spec=SPEC, tenant=r["tenant"],
               submitted_by=r["by"], seed=7, seed_commit="e" * 64, split=r.get("split", "dev"))
    for to in r.get("path", ()):
        kw = {"reason_code": r["reason"][0], "reason_detail": r["reason"][1]} if to == "FAILED" else {}
        st.transition(r["id"], to, **kw)

auth = app.state.auth
tokens = {k: auth.issue_token(v[0], v[1], tuple(v[2]))[1] for k, v in cfg["principals"].items()}
proxy = create_proxy_app(s, upstreams={"eval": "http://127.0.0.1:9", "dist": "http://127.0.0.1:9"})
server = uvicorn.Server(uvicorn.Config(_Front(app, proxy, Path(cfg["dist"])), host=cfg["host"], port=cfg["port"], log_level="warning",
                                       lifespan="on", timeout_graceful_shutdown=2))
stop = threading.Event()

def controller():
    # run-controller 的取消确认：登记了取消请求的 PREPARING、RUNNING 运行在 1 s 后终止并迁至 CANCELLED（时限 10 s 之内）
    while not stop.wait(0.25):
        for c in st.pending_cancels():
            if time.time_ns() - c["requested_unix_ns"] >= 1_000_000_000:
                try:
                    st.transition(c["run_id"], "CANCELLED", expect=c["state"], actor="run-controller")
                except Exception:
                    pass

def ready():
    while not server.started:
        time.sleep(0.05)
    tmp = root / ".ready.json"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": origin, "tokens": tokens, "data_root": str(s.data_root)}, f)
    tmp.replace(root / "ready.json")

threading.Thread(target=controller, daemon=True).start()
threading.Thread(target=ready, daemon=True).start()
try:
    server.run()
finally:
    stop.set()
`

const GATES_OK = [
  { gate: 1, status: 'pass', errors: [], warnings: [] }, { gate: 2, status: 'pass', errors: [], warnings: [] },
  { gate: 3, status: 'warn', errors: [], warnings: [{ code: 'W_SEMANTIC_MARGIN', path: 'relations[0]', message_zh: '交接时刻距门限 0.4 s', message_en: 'Handoff time is 0.4 s from the limit' }] },
]
const EVENTS = [
  { id: 'abc-crossing', gates: GATES_OK },
  { id: 'tst-hidden', split: 'test', gates: GATES_OK },
  { id: 'bad-infeasible', status: 'infeasible', reason: { code: 612, name: 'EVENT_INFEASIBLE' }, gates: [
    { gate: 1, status: 'pass', errors: [], warnings: [] },
    { gate: 2, status: 'fail', errors: [{ code: 'E_NO_PATH', reason: 612, path: 'actors[1].route', message_zh: '演员 ped-2 的路段 3 无可达路径', message_en: 'Actor ped-2 has no reachable path on segment 3' }], warnings: [] },
  ] },
]

interface Stack { url: string; tokens: Record<Role | 'other', string>; dataRoot: string; close(): Promise<void> }

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** 内部构建（与 labctl.sh build 同一路由组，无测试开关）；给出 AWR_LAB_INTERNAL_DIST 时直接使用 */
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
  const tmp = mkdtempSync(join(process.env.AWR_LAB_TMP ?? tmpdir(), 'awr-f09-roles-'))
  const dist = internalDist(tmp)
  const port = await freePort()
  const root = join(tmp, 'stack')
  mkdirSync(root, { recursive: true })
  writeFileSync(join(tmp, 'roles_stack.py'), STACK_PY)
  const cfg = { repo: ROOT, root, host: '127.0.0.1', port, dist, principals: PRINCIPALS, runs: RUNS, events: EVENTS }
  const p: ChildProcess = spawn(PY, [join(tmp, 'roles_stack.py'), JSON.stringify(cfg)], {
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
      throw new Error(`roles stack did not start:\n${log.join('').slice(-4000)}`)
    }
    await sleep(200)
  }
  const r = JSON.parse(readFileSync(join(root, 'ready.json'), 'utf8')) as { url: string; tokens: Stack['tokens']; data_root: string }
  return {
    url: r.url, tokens: r.tokens, dataRoot: r.data_root,
    close: async () => {
      kill()
      await sleep(500)
      rmSync(tmp, { recursive: true, force: true })
    },
  }
}

// ── 文案与路由 ──────────────────────────────────────────────────────────────────────────────────────────────────────

/** zh-CN 词典（主词典 + lab 分片，与 i18n/index.ts 的合并次序相同） */
const DICT: Record<string, string> = (() => {
  const dir = join(ROOT, 'apps/web/src/app/i18n')
  const d = JSON.parse(readFileSync(join(dir, 'zh-CN.json'), 'utf8')) as Record<string, string>
  for (const f of readdirSync(join(dir, 'lab')).filter((x) => x.endsWith('.zh-CN.json')).sort()) Object.assign(d, JSON.parse(readFileSync(join(dir, 'lab', f), 'utf8')))
  return d
})()
function tz(key: string, params: Record<string, string | number> = {}): string {
  const raw = DICT[key]
  if (raw === undefined) throw new Error(`missing zh-CN key ${key}`)
  return raw.replace(/\{(\w+)\}/g, (_m, k: string) => String(params[k] ?? `{${k}}`))
}

/** 源码中的全部 /lab 路由模式（routes/lab/*.route.ts 分片与 routes/lab*.tsx） */
function sourceLabPatterns(): string[] {
  const base = join(ROOT, 'apps/web/src/app/routes')
  const files = [...readdirSync(join(base, 'lab')).filter((f) => f.endsWith('.route.ts')).map((f) => join(base, 'lab', f)),
    ...readdirSync(base).filter((f) => /^lab.*\.tsx$/.test(f)).map((f) => join(base, f))]
  const out = new Set<string>()
  for (const f of files) for (const m of readFileSync(f, 'utf8').matchAll(/pattern:\s*'(\/lab[^']*)'/g)) out.add(m[1]!)
  return [...out].sort()
}

/**
 * 路由 × 角色矩阵（测试侧口径，依据 AWR-14 §21.2.5 与 AWR-17 §18.3.5，不从实现读取）：`min` 为可查看页面的最低角色，低于它显示
 * `denied` 文案（115）；`evalMin` 为评测视图的最低角色。`path` 为该模式的具体实例（受邀成员可见的团队运行）。
 */
interface RouteCase { pattern: string; path: string; view: string; min: Role | 'none'; denied?: string; figure?: string; eval?: 'wall' | 'inspect' }
const ROUTES: RouteCase[] = [
  { pattern: '/lab/login', path: '/lab/login', view: 'lab-login', min: 'none' },
  { pattern: '/lab/worlds', path: '/lab/worlds', view: 'lab-worlds', min: 'invited', figure: 'lab-worlds' },
  { pattern: '/lab/worlds/:wid', path: '/lab/worlds/oryahovo-crop1', view: 'lab-world', min: 'invited', figure: 'lab-world' },
  { pattern: '/lab/events', path: '/lab/events', view: 'lab-events', min: 'invited', figure: 'lab-events' },
  { pattern: '/lab/runs', path: '/lab/runs', view: 'lab-runs', min: 'invited', figure: 'lab-runs' },
  { pattern: '/lab/runs/:rid', path: `/lab/runs/${TEAM_RUN}?view=eval`, view: 'lab-wall', min: 'invited', figure: 'lab-wall', eval: 'wall' },
  { pattern: '/lab/runs/:rid/inspect', path: `/lab/runs/${TEAM_RUN}/inspect?catalog=eval`, view: 'lab-inspect', min: 'invited', eval: 'inspect' },
  { pattern: '/lab/runs/:rid/lidar', path: `/lab/runs/${TEAM_RUN}/lidar`, view: 'lab-lidar-compare', min: 'researcher', denied: 'lab.lidarCompare.roleDenied' },
  { pattern: '/lab/replay', path: '/lab/replay', view: 'lab-replay', min: 'researcher', denied: 'lab.replay.roleDenied', figure: 'lab-replay' },
  { pattern: '/lab/compare', path: '/lab/compare', view: 'lab-compare', min: 'researcher', denied: 'lab.compare.roleDenied' },
  { pattern: '/lab/board', path: '/lab/board', view: 'lab-board', min: 'invited', figure: 'lab-board' },
  { pattern: '/lab/submissions', path: '/lab/submissions', view: 'lab-submissions', min: 'invited', figure: 'lab-submissions' },
]
const EVAL_MIN: Role = 'evaluator'

// ── 公共步骤 ──────────────────────────────────────────────────────────────────────────────────────────────────────

let st: Stack
let browser0: Browser
const contexts = new Map<Role, BrowserContext>()
const result: Record<string, unknown> = {}

test.beforeAll(async ({ browser }) => {
  test.setTimeout(300_000)
  st = await startStack()
  browser0 = browser
})
test.afterAll(async () => {
  for (const c of contexts.values()) await c.close()
  mkdirSync(OUT, { recursive: true })
  writeFileSync(join(OUT, 'lab-roles.json'), JSON.stringify({ at_utc: new Date().toISOString(), ...result }, null, 1))
  await st?.close()
})

/** 新上下文：页面内记录出现过的 Toast 文本（Toast 4 s 后消失，断言不依赖其仍在屏幕上） */
async function newContext(): Promise<BrowserContext> {
  const c = await browser0.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1, locale: 'zh-CN' })
  await c.addInitScript(() => {
    const w = window as unknown as { __f09Toasts: string[] }
    w.__f09Toasts = []
    new MutationObserver(() => {
      for (const el of document.querySelectorAll('[data-slot="toast"]')) {
        const v = (el.textContent ?? '').trim()
        if (v && !w.__f09Toasts.includes(v)) w.__f09Toasts.push(v)
      }
    }).observe(document, { subtree: true, childList: true, characterData: true })
  })
  return c
}

/** 等页面出现含 `text` 的 Toast（自本页加载起） */
async function sawToast(page: Page, text: string): Promise<void> {
  await page.waitForFunction((x) => (window as unknown as { __f09Toasts?: string[] }).__f09Toasts?.some((v) => v.includes(x)) ?? false, text, { timeout: 15_000 })
}

/** 经 /lab/login 以该角色的 bench token 登录（表单），回到 `next` */
async function login(page: Page, who: Role | 'other', next = '/lab/runs'): Promise<void> {
  await page.goto(`${st.url}/lab/login?next=${encodeURIComponent(next)}`)
  await page.fill('#lab-token', st.tokens[who])
  await page.click('[data-view="lab-login"] button[type="submit"]')
  await page.waitForURL((u) => u.pathname === next.split('?')[0], { timeout: 30_000 })
}

/** 每个角色一个已登录的浏览器上下文（会话 Cookie 在上下文内复用） */
/** 已登录角色的页面收到的 403 响应计数（`主体 方法 路径` → 次数），越权审计用例逐项对照 deny 记录 */
const observed403 = new Map<string, number>()

/** 每个角色一个已登录的浏览器上下文（会话 Cookie 在上下文内复用）；上下文内全部 bench 403 响应计入 observed403 */
async function as(role: Role): Promise<Page> {
  let c = contexts.get(role)
  if (!c) {
    c = await newContext()
    contexts.set(role, c)
    const who = PRINCIPALS[role][0]
    c.on('response', (r) => {
      const u = new URL(r.url())
      if (r.status() === 403 && u.pathname.startsWith('/api/bench/')) {
        const k = `${who} ${r.request().method()} ${u.pathname}`
        observed403.set(k, (observed403.get(k) ?? 0) + 1)
      }
    })
    const p = await c.newPage()
    await login(p, role)
    await p.close()
  }
  return c.newPage()
}

/** 在页面会话内直接调用 bench-api（与页面相同的 Cookie、Origin 与 X-D3-CSRF），模拟绕过界面的请求 */
async function call(page: Page, method: string, path: string, body?: unknown): Promise<{ status: number; code: number | null; why: string | null; body: unknown }> {
  return page.evaluate(async ([m, p, b]) => {
    const r = await fetch(p as string, { method: m as string, credentials: 'same-origin',
      headers: { 'X-D3-CSRF': '1', ...(b === undefined ? {} : { 'Content-Type': 'application/json' }) }, body: b === undefined ? undefined : JSON.stringify(b) })
    const txt = await r.text()
    let j: Record<string, unknown> | null = null
    try {
      j = txt ? (JSON.parse(txt) as Record<string, unknown>) : null
    } catch {
      j = null
    }
    const det = (j?.detail ?? null) as Record<string, unknown> | null
    return { status: r.status, code: typeof j?.code === 'number' ? j.code : null, why: typeof det?.why === 'string' ? det.why : null, body: j }
  }, [method, `${API}${path}`, body] as const)
}

/** 记录页面的 bench 请求与响应状态 */
function benchLog(page: Page): { m: string; p: string; s: number }[] {
  const out: { m: string; p: string; s: number }[] = []
  page.on('response', (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/api/bench/')) out.push({ m: r.request().method(), p: u.pathname, s: r.status() })
  })
  return out
}

/** 令牌形态与明文（凭据不得出现在 URL、Web 存储与 document.cookie） */
const leaks = (s: string) => Object.values(st.tokens).some((t) => t.length >= 8 && s.includes(t)) || /d3bt_[A-Za-z0-9]/.test(s)

const rowOf = (page: Page, rid: string) => page.locator(`[data-figure="lab-runs-table"] [data-row-key="${rid}"]`)
async function runState(page: Page, rid: string): Promise<string | null> {
  return rowOf(page, rid).locator('[data-run-state]').getAttribute('data-run-state')
}
async function openMenu(page: Page, rid: string): Promise<void> {
  await page.click(`[data-run-menu="${rid}"]`)
  await expect(page.getByRole('menu')).toBeVisible()
}
async function closeMenu(page: Page): Promise<void> {
  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu')).toHaveCount(0)
}

// ── 用例 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

test('路由清单完整；未登录访问全部 /lab 路由跳转登录页并带 next，bench-api 无凭据 401', async () => {
  const src = sourceLabPatterns()
  const covered = ROUTES.map((r) => r.pattern).sort()
  expect(src, 'every /lab route pattern in apps/web/src/app/routes must be covered by the role matrix').toEqual(covered)
  const ctx = await newContext()
  const page = await ctx.newPage()
  const redirects: Record<string, string | null> = {}
  for (const r of ROUTES.filter((x) => x.min !== 'none')) {
    await page.goto(st.url + r.path)
    await page.waitForURL(/\/lab\/login\?next=/, { timeout: 30_000 })
    const next = new URL(page.url()).searchParams.get('next')
    redirects[r.path] = next
    // 回放类页面在跳转前可能已把播放头写回查询参数 t（与 lab-inspect 同一口径）
    expect(next?.replace(/[?&]t=[\d.]+$/, ''), r.path).toBe(r.path)
    await expect(page.locator('[data-view="lab-login"]')).toBeVisible()
  }
  for (const p of ['/session', '/runs', '/worlds', '/events', '/submissions', `/runs/${TEAM_RUN}`]) {
    const res = await page.request.get(`${st.url}${API}${p}`)
    expect(res.status(), p).toBe(401)
  }
  result.unauthenticated = { patterns: src, redirects }
  await ctx.close()
})

test('会话：bench token 换取 HttpOnly、SameSite=Strict、12 h 的 d3_session，令牌不留在页面；无效令牌 302；注销后失效', async () => {
  const ctx = await newContext()
  const page = await ctx.newPage()
  const urls: string[] = []
  page.on('request', (r) => urls.push(r.url()))
  // 无效令牌
  await page.goto(`${st.url}/lab/login?next=%2Flab%2Fworlds`)
  await page.fill('#lab-token', 'd3bt_invalid_token_000000')
  await page.click('[data-view="lab-login"] button[type="submit"]')
  await expect(page.getByText(tz('lab.login.failed', { code: 302, name: 'TOKEN_INVALID' }).split('：')[0])).toBeVisible()
  await expect(page.locator('[data-view="lab-login"]')).toContainText('302')
  expect(await ctx.cookies()).toEqual([])
  // 有效令牌
  const t0 = Date.now() / 1000
  await page.fill('#lab-token', st.tokens.researcher)
  await page.click('[data-view="lab-login"] button[type="submit"]')
  await page.waitForURL((u) => u.pathname === '/lab/worlds', { timeout: 30_000 })
  await expect(page.locator('[data-view="lab-worlds"]')).toBeVisible()
  const cookies = await ctx.cookies()
  const sess = cookies.find((c) => c.name === 'd3_session')
  expect(sess, 'd3_session cookie').toBeTruthy()
  expect(sess!.httpOnly).toBe(true)
  expect(sess!.sameSite).toBe('Strict')
  expect(sess!.path).toBe('/')
  const life = sess!.expires - t0
  expect(life).toBeGreaterThan(43_200 - 120)
  expect(life).toBeLessThanOrEqual(43_200 + 5)
  const session = await call(page, 'GET', '/session')
  expect(session.status).toBe(200)
  const sb = session.body as { principal: string; role: string; iat_unix_ns?: string; exp_unix_ns?: string }
  expect([sb.principal, sb.role]).toEqual(['alice', 'researcher'])
  if (sb.iat_unix_ns && sb.exp_unix_ns) expect((BigInt(sb.exp_unix_ns) - BigInt(sb.iat_unix_ns)) / 1_000_000_000n).toBe(43_200n)
  const store = await page.evaluate(() => JSON.stringify({ ls: { ...localStorage }, ss: { ...sessionStorage }, c: document.cookie, href: location.href }))
  expect(leaks(store), 'token in URL, Web Storage or document.cookie').toBe(false)
  expect(store.includes('d3_session'), 'HttpOnly cookie must not be readable from script').toBe(false)
  expect(urls.filter(leaks), 'token in a request URL').toEqual([])
  // 注销：DELETE /session 后会话失效，页面回到登录
  expect((await call(page, 'DELETE', '/session')).status).toBe(204)
  expect((await call(page, 'GET', '/session')).status).toBe(401)
  await page.goto(`${st.url}/lab/events`)
  await page.waitForURL(/\/lab\/login\?next=%2Flab%2Fevents/, { timeout: 30_000 })
  result.session = { httpOnly: sess!.httpOnly, sameSite: sess!.sameSite, secure: sess!.secure, lifetime_s: Math.round(life), session_window_s: 43_200 }
  await ctx.close()
})

test('四类角色 × 全部 /lab 路由：可达或 115 提示，评测视图只对评测员及以上，不发起越权请求，外壳文案过 copy-lint', async () => {
  test.setTimeout(600_000)
  const matrix: Record<string, Record<string, unknown>> = {}
  const copy: { route: string; role: Role; rule: string; match: string; text: string }[] = []
  for (const role of ROLES) {
    const page = await as(role)
    const log = benchLog(page)
    for (const r of ROUTES) {
      log.length = 0
      await page.goto(st.url + r.path)
      const sec = page.locator(`section[data-view="${r.view}"]`)
      await expect(sec, `${role} ${r.path}`).toBeVisible({ timeout: 30_000 })
      await page.waitForLoadState('networkidle')
      await sleep(300)
      const cell: Record<string, unknown> = {}
      const allowed = r.min === 'none' || RANK[role] >= RANK[r.min]
      if (r.min !== 'none') {
        // 已登录：不跳登录页，标题行显示主体与角色（有标题行的页面）
        expect(new URL(page.url()).pathname).toBe(r.path.split('?')[0])
      }
      if (r.denied) {
        const deniedText = page.getByText(tz(r.denied), { exact: true })
        if (allowed) await expect(deniedText).toHaveCount(0)
        else await expect(deniedText).toBeVisible()
        cell.denied = !allowed
      }
      if (r.figure && allowed) await expect(page.locator(`[data-figure="${r.figure}"]`).first()).toBeVisible()
      if (r.eval === 'wall') {
        const ev = RANK[role] >= RANK[EVAL_MIN]
        if (ev) await expect(page.locator('[data-eval-tag]')).toBeVisible()
        else {
          await expect(page.locator('[data-eval-tag]')).toHaveCount(0)
          await sawToast(page, tz('lab.wall.evalDenied'))
        }
        cell.eval = ev
      }
      if (r.eval === 'inspect') {
        const ev = RANK[role] >= RANK[EVAL_MIN]
        await expect(sec).toHaveAttribute('data-inspect-catalog', ev ? 'eval' : 'dist')
        if (!ev) await sawToast(page, tz('lab.inspect.evalDenied'))
        cell.eval = ev
      }
      // 可达页面的 bench 请求没有 401/403；115 提示页面除会话读取外不发起请求（界面不推断越权操作）
      const bad = log.filter((x) => x.s === 401 || x.s === 403)
      expect(bad, `${role} ${r.path} bench 401/403`).toEqual([])
      if (!allowed) expect(log.filter((x) => x.p !== `${API}/session`), `${role} ${r.path} requests behind a 115 notice`).toEqual([])
      if (r.view === 'lab-runs') {
        const [name] = PRINCIPALS[role]
        await expect(page.locator('[data-lab-header="runs"]')).toContainText(tz('lab.session', { name, role: tz(`lab.role.${role}`) }))
      }
      const cc = await shellCopyFindings(page, `section[data-view="${r.view}"]`)
      for (const f of cc.findings) copy.push({ route: r.path, role, ...f })
      cell.requests = log.map((x) => `${x.m} ${x.p} ${x.s}`)
      ;(matrix[r.path] ??= {})[role] = { allowed, ...cell }
    }
    await page.close()
  }
  result.matrix = matrix
  result.copy_findings = copy
  expect(copy).toEqual([])
})

test('/lab/worlds 与 /lab/worlds/:wid：世界列表、六个标签；/lab/events：受邀成员只见 dev 事件，三道门与原因码', async () => {
  const page = await as('researcher')
  await page.goto(`${st.url}/lab/worlds`)
  const row = page.locator('[data-figure="lab-worlds"] [data-row-key="oryahovo-crop1"]')
  await expect(row).toBeVisible()
  await row.click()
  await page.waitForURL((u) => u.pathname === '/lab/worlds/oryahovo-crop1')
  await expect(page.locator('[data-view="lab-world"][data-world-id="oryahovo-crop1"]')).toBeVisible()
  const tabs = await page.locator('[data-world-tab]').evaluateAll((els) => els.map((e) => e.getAttribute('data-world-tab')))
  expect(tabs).toEqual(['overview', 'calibration', 'quality', 'layers', 'sensors', 'license'])
  for (const k of tabs) {
    await page.click(`[data-world-tab="${k}"]`)
    await expect(page.locator(`[data-world-panel="${k}"]`)).toBeVisible()
  }
  // 事件：研究者见全部（含 test 划分标记），受邀成员只见 dev
  await page.goto(`${st.url}/lab/events`)
  const evRows = () => page.locator('[data-figure="lab-events"] [data-row-key]').evaluateAll((els) => els.map((e) => e.getAttribute('data-row-key')).sort())
  await expect(page.locator('[data-figure="lab-events"] [data-row-key="tst-hidden"]')).toBeVisible()
  expect(await evRows()).toEqual(['abc-crossing', 'bad-infeasible', 'tst-hidden'])
  await expect(page.locator('[data-row-key="tst-hidden"]')).toContainText(tz('lab.events.split.test'))
  // 不可行事件：原因码块、失败门在前且红描边、门错误按界面语言
  await expect(page.locator('[data-row-key="bad-infeasible"] [data-event-status="infeasible"]')).toBeVisible()
  await page.locator('[data-row-key="bad-infeasible"]').click()
  const sheet = page.locator('[data-event-sheet="bad-infeasible"]')
  await expect(sheet.locator('[data-figure="lab-event-detail"]')).toBeVisible()
  await expect(sheet.locator('[data-reason="612"]')).toContainText('612 EVENT_INFEASIBLE')
  const gates = await sheet.locator('[data-gate]').evaluateAll((els) => els.map((e) => [e.getAttribute('data-gate'), e.getAttribute('data-gate-status')]))
  expect(gates[0]).toEqual(['2', 'fail'])
  await expect(sheet.locator('[data-gate="2"]')).toContainText('演员 ped-2 的路段 3 无可达路径')
  const inv = await as('invited')
  await inv.goto(`${st.url}/lab/events`)
  await expect(inv.locator('[data-figure="lab-events"] [data-row-key="abc-crossing"]')).toBeVisible()
  const invRows = await inv.locator('[data-figure="lab-events"] [data-row-key]').evaluateAll((els) => els.map((e) => e.getAttribute('data-row-key')).sort())
  expect(invRows).toEqual(['abc-crossing', 'bad-infeasible'])
  const hidden = await call(inv, 'GET', '/events/tst-hidden')
  expect([hidden.status, hidden.code, hidden.why]).toEqual([403, 115, 'invited_scope'])
  result.worlds = { tabs, events_researcher: await evRows(), events_invited: invRows }
  await inv.close()
  await page.close()
})

test('/lab/runs 状态机：八态徽标、排队位置、重启副文、原因码、唯一 hot 行与状态侧栏', async () => {
  const page = await as('researcher')
  await page.goto(`${st.url}/lab/runs`)
  await expect(rowOf(page, 'run-a000000001')).toBeVisible()
  const states = await page.locator('[data-figure="lab-runs-table"] [data-run-state]').evaluateAll((els) => els.map((e) => e.getAttribute('data-run-state')))
  for (const s of STATES) expect(states, s).toContain(s)
  for (const s of STATES) await expect(page.locator(`[data-run-state="${s}"]`).first()).toContainText(tz(`lab.runs.state.${s}`))
  // 排队位置（QUEUED 按入队先后）、第 1 次重启、取消中以外的活动态
  await expect(rowOf(page, 'run-a000000001').locator('[data-run-phase="queued"]')).toContainText(tz('lab.runs.queued', { n: 1 }))
  await expect(rowOf(page, 'run-a000000004').locator('[data-run-restart="1"]')).toContainText(tz('lab.runs.restart', { k: 1 }))
  // 原因码：code NAME · 短文案
  await expect(rowOf(page, 'run-a000000003').locator('[data-run-reason="631"]')).toContainText('631 GZ_NOT_READY')
  await expect(rowOf(page, 'run-c000000001').locator('[data-run-reason="649"]')).toContainText('649 SENSOR_RIG_INVALID')
  // 最新的 FAILED 为唯一 hot 单元
  const hot = page.locator('[data-figure="lab-runs-table"] [data-hot]')
  await expect(hot).toHaveCount(1)
  await expect(hot.locator('[data-run-state]')).toHaveAttribute('data-run-state', 'FAILED')
  // 状态侧栏：尝试与迁移审计
  await page.goto(`${st.url}/lab/runs?run=run-a000000003`)
  const sheet = page.locator('[data-run-sheet="run-a000000003"]')
  await expect(sheet.locator('[data-figure="lab-run-detail"]')).toBeVisible()
  await expect(sheet.locator('[data-reason="631"]')).toBeVisible()
  // 迁移审计：排队中 → 准备中 → 失败（631，scheduler）
  for (const k of ['QUEUED', 'PREPARING', 'FAILED']) await expect(sheet).toContainText(tz(`lab.runs.state.${k}`))
  await expect(sheet).toContainText('scheduler')
  result.run_states = [...new Set(states)].sort()
  await page.close()
})

test('取消：排队运行立即取消，运行中经取消中倒计时到已取消，批量取消；重试：同一 run_id 新 attempt', async () => {
  test.setTimeout(180_000)
  const page = await as('researcher')
  await page.goto(`${st.url}/lab/runs`)
  // 排队 → 立即 CANCELLED
  await openMenu(page, 'run-a000000001')
  await page.click('[data-run-cancel="run-a000000001"]')
  const dlg = page.locator('[data-run-confirm="cancel"]')
  await expect(dlg).toContainText(tz('lab.runs.cancelBody'))
  await page.click('[data-run-confirm-action="cancel"]')
  await expect(rowOf(page, 'run-a000000001').locator('[data-run-state]')).toHaveAttribute('data-run-state', 'CANCELLED', { timeout: 15_000 })
  // 运行中 → 取消中（10 s 时限倒计时）→ run-controller 确认后 CANCELLED
  await openMenu(page, 'run-a000000002')
  await page.click('[data-run-cancel="run-a000000002"]')
  await page.click('[data-run-confirm-action="cancel"]')
  const t0 = Date.now()
  const phase = rowOf(page, 'run-a000000002').locator('[data-run-phase="cancelling"]')
  let sawCancelling = false
  for (;;) {
    if (await phase.count()) sawCancelling = true
    if ((await runState(page, 'run-a000000002')) === 'CANCELLED') break
    if (Date.now() - t0 > 12_000) throw new Error('run-a000000002 did not reach CANCELLED within the 10 s cancel window')
    await sleep(100)
  }
  const cancelMs = Date.now() - t0
  const det = await call(page, 'GET', '/runs/run-a000000002')
  const tr = (det.body as { transitions: { to: string; actor: string }[] }).transitions.map((x) => `${x.to}:${x.actor}`)
  expect(tr).toEqual(expect.arrayContaining(['CANCEL_REQUESTED:alice', 'CANCELLED:run-controller']))
  // 批量取消
  await page.click('[data-run-select="run-a000000009"]')
  await page.click('[data-run-select="run-a000000010"]')
  await page.click('[data-runs-batch-cancel="2"]')
  await expect(page.locator('[data-run-confirm="batch"]')).toContainText(tz('lab.runs.batchTitle', { n: 2 }))
  await page.click('[data-run-confirm-action="batch"]')
  for (const rid of ['run-a000000009', 'run-a000000010']) {
    await expect(rowOf(page, rid).locator('[data-run-state]')).toHaveAttribute('data-run-state', 'CANCELLED', { timeout: 15_000 })
  }
  // 重试：FAILED → 第 2 次尝试（QUEUED），第 1 次尝试的原因码保留
  await openMenu(page, 'run-a000000003')
  await page.click('[data-run-retry="run-a000000003"]')
  await expect(page.locator('[data-run-confirm="retry"]')).toContainText(tz('lab.runs.retryBody', { n: 2 }))
  await page.click('[data-run-confirm-action="retry"]')
  await expect(rowOf(page, 'run-a000000003').locator('[data-run-state]')).toHaveAttribute('data-run-state', 'QUEUED', { timeout: 15_000 })
  await expect(rowOf(page, 'run-a000000003')).toContainText(tz('lab.runs.attempt', { n: 2 }))
  const r3 = (await call(page, 'GET', '/runs/run-a000000003')).body as { attempt: number; attempts: { attempt: number; state: string; reason: { code: number } | null }[] }
  expect(r3.attempt).toBe(2)
  expect(r3.attempts.map((a) => [a.attempt, a.state, a.reason?.code ?? null])).toEqual([[1, 'FAILED', 631], [2, 'QUEUED', null]])
  // 终态的运行没有取消操作，已完成的运行没有重试操作
  await openMenu(page, 'run-a000000007')
  await expect(page.locator('[data-run-cancel="run-a000000007"], [data-run-retry="run-a000000007"]')).toHaveCount(0)
  await closeMenu(page)
  result.cancel = { running_to_cancelled_ms: cancelMs, saw_cancelling_phase: sawCancelling, transitions: tr }
  expect(sawCancelling, 'the cancelling phase with its 10 s countdown is shown before CANCELLED').toBe(true)
  expect(cancelMs).toBeLessThan(10_000)
  await page.close()
})

const denials: { who: string; method: string; path: string; why: string | null }[] = []

test('数据范围：研究者对他人项目只读（403 115 project_scope），评测员不限项目；受邀成员只见本团队 dev 运行且无写操作', async () => {
  // 研究者 alice：他人项目运行无操作，直接调用 403
  const res = await as('researcher')
  await res.goto(`${st.url}/lab/runs`)
  await openMenu(res, 'run-c000000001')
  await expect(res.locator('[data-run-retry="run-c000000001"]')).toHaveCount(0)
  await closeMenu(res)
  await expect(res.locator('[data-run-select="run-b000000001"]')).toHaveCount(0)
  const r1 = await call(res, 'POST', '/runs/run-c000000001/retry')
  expect([r1.status, r1.code, r1.why]).toEqual([403, 115, 'project_scope'])
  denials.push({ who: 'alice', method: 'POST', path: `${API}/runs/run-c000000001/retry`, why: 'project_scope' })
  const r2 = await call(res, 'DELETE', `/runs/${TEAM_RUN}`)
  expect([r2.status, r2.code, r2.why]).toEqual([403, 115, 'project_scope'])
  denials.push({ who: 'alice', method: 'DELETE', path: `${API}/runs/${TEAM_RUN}`, why: 'project_scope' })
  // 评测员 eve：不限项目，可重试他人项目的失败运行
  const ev = await as('evaluator')
  await ev.goto(`${st.url}/lab/runs`)
  await openMenu(ev, 'run-c000000001')
  await ev.click('[data-run-retry="run-c000000001"]')
  await ev.click('[data-run-confirm-action="retry"]')
  await expect(rowOf(ev, 'run-c000000001').locator('[data-run-state]')).toHaveAttribute('data-run-state', 'QUEUED', { timeout: 15_000 })
  // 受邀成员 ivan：只见本团队 dev 运行，无选择框与写操作
  const inv = await as('invited')
  await inv.goto(`${st.url}/lab/runs`)
  await expect(rowOf(inv, TEAM_RUN)).toBeVisible()
  const ids = await inv.locator('[data-figure="lab-runs-table"] [data-row-key]').evaluateAll((els) => els.map((e) => e.getAttribute('data-row-key')).sort())
  expect(ids).toEqual(['run-b000000001', 'run-b000000002'])
  await expect(inv.locator('[data-run-select]')).toHaveCount(0)
  for (const rid of ids) {
    await openMenu(inv, rid!)
    await expect(inv.locator(`[data-run-cancel="${rid}"], [data-run-retry="${rid}"]`)).toHaveCount(0)
    await closeMenu(inv)
  }
  const i1 = await call(inv, 'DELETE', `/runs/${TEAM_RUN}`)
  expect([i1.status, i1.code]).toEqual([403, 115])
  expect((i1.body as { detail?: { required?: string } }).detail?.required).toBe('researcher')
  denials.push({ who: 'ivan', method: 'DELETE', path: `${API}/runs/${TEAM_RUN}`, why: null })
  const i2 = await call(inv, 'POST', '/runs/run-b000000002/retry')
  expect([i2.status, i2.code]).toEqual([403, 115])
  denials.push({ who: 'ivan', method: 'POST', path: `${API}/runs/run-b000000002/retry`, why: null })
  for (const rid of ['run-a000000001', 'run-d000000001']) {
    const g = await call(inv, 'GET', `/runs/${rid}`)
    expect([g.status, g.code, g.why], rid).toEqual([403, 115, 'invited_scope'])
    denials.push({ who: 'ivan', method: 'GET', path: `${API}/runs/${rid}`, why: 'invited_scope' })
  }
  const f = await call(inv, 'GET', `/runs/${TEAM_RUN}/evaluation`)
  expect([f.status, f.code]).toEqual([403, 115])
  denials.push({ who: 'ivan', method: 'GET', path: `${API}/runs/${TEAM_RUN}/evaluation`, why: null })
  // 他人运行的观测墙：不可用外壳
  await inv.goto(`${st.url}/lab/runs/run-a000000001`)
  await expect(inv.getByText(tz('lab.wall.unavailable'))).toBeVisible()
  result.scope = { invited_runs: ids, denials }
  for (const p of [res, ev, inv]) await p.close()
})

test('越权审计：每次 403 恰有一条 deny 记录（主体、方法、路径、115），审计文件不含令牌明文', async () => {
  expect(denials.length, 'runs after the data scope case').toBeGreaterThan(0)
  const dir = join(st.dataRoot, 'audit')
  const files = readdirSync(dir).filter((f) => /^access-\d{8}\.jsonl$/.test(f))
  const text = files.map((f) => readFileSync(join(dir, f), 'utf8')).join('')
  expect(leaks(text), 'token plaintext in the audit log').toBe(false)
  const recs = text.split('\n').filter(Boolean).map((l) => JSON.parse(l) as Record<string, unknown>)
  const names = new Set(Object.values(PRINCIPALS).map((p) => p[0]))
  const deny = recs.filter((r) => r.action === 'deny' && names.has(String(r.principal)))
  // 审计侧：（主体、方法、路径）→ deny 记录数（均为 115）；浏览器侧：同一键的 403 响应数。两者逐键相等（含页面重试产生的 403）
  const logged = new Map<string, number>()
  for (const r of deny) {
    expect(Number(r.code), JSON.stringify(r)).toBe(115)
    const k = `${String(r.principal)} ${String(r.method)} ${String(r.path)}`
    logged.set(k, (logged.get(k) ?? 0) + 1)
  }
  const keys = [...new Set([...logged.keys(), ...observed403.keys()])].sort()
  const table = keys.map((k) => ({ key: k, responses_403: observed403.get(k) ?? 0, deny_records: logged.get(k) ?? 0 }))
  const why = denials.map((d) => ({ ...d, why_logged: [...new Set(deny.filter((r) => r.principal === d.who && r.method === d.method && r.path === d.path).map((r) => r.why ?? null))] }))
  result.audit = { files, deny_records: deny.length, table, why }
  expect(table.filter((x) => x.responses_403 !== x.deny_records), 'every 403 has exactly one deny record').toEqual([])
  for (const d of denials) expect(observed403.get(`${d.who} ${d.method} ${d.path}`) ?? 0, `${d.who} ${d.method} ${d.path}`).toBeGreaterThan(0)
  for (const w of why) if (w.why) expect(w.why_logged, `${w.who} ${w.method} ${w.path}`).toEqual([w.why])
})
