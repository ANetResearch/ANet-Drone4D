// D1-MS1/MS2 集成冒烟（集成验证工作包；跨模块集成用例按 AWR-03 §4.3 放在 tests/e2e/，所有者 M16，可接管或删除）。
// 与 perf/m15/smoke.spec.ts（M15 自带 preview）不同，本用例走 playwright.config.ts 配置的 webServer（vite preview，
// 端口 4173 + 10·AWR_PORT_OFFSET，使用 apps/web/vite.config.ts 的代理表），验证：
//   1. 生产构建的首页无 pageerror，全部 /assets/* 构建产物 200（/assets 不再被代理遮挡，AWR-03 §3.3）；
//   2. / 重定向到默认世界 /world/shenzhen，启动遮罩揭开，头栏与常驻画布存在（M15-AC-003 的冒烟口径）；
//   3. /worlds 是前端路由（World Hub），而 /worlds/<id>/** 数据路径转发到后端；
//   4. /api 经 preview 代理到早期数据源 tools/fake/fake_gw.py（M11-R），健康检查 200。
// 运行：npm run build -w apps/web && (cd apps/web && npx playwright test --project e2e ms-smoke)
// 截图写 .cache/impl/shots/（AWR_SHOTS_DIR 可覆盖）。本机 8000 + 10·k 端口被占用时跳过 fake_gw 相关断言。
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'

const ROOT = join(import.meta.dirname, '..', '..')
const SHOTS = process.env.AWR_SHOTS_DIR ?? join(ROOT, '.cache', 'impl', 'shots')
mkdirSync(SHOTS, { recursive: true })

const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
const API_PORT = 8000 + 10 * OFFSET
let gw: ChildProcess | null = null
let gwUp = false

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = createServer()
    s.once('error', () => resolve(false))
    s.listen(port, '127.0.0.1', () => s.close(() => resolve(true)))
  })
}

async function waitHttp(url: string, ms: number): Promise<boolean> {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) {
    try {
      if ((await fetch(url)).ok) return true
    } catch {
      // 尚未监听
    }
    await new Promise((r) => setTimeout(r, 200))
  }
  return false
}

test.beforeAll(async () => {
  const py = join(ROOT, '.venv', 'bin', 'python')
  if (!existsSync(py) || !(await portFree(API_PORT))) return
  gw = spawn(py, [join(ROOT, 'tools', 'fake', 'fake_gw.py'), '--n', '200', '--port', String(API_PORT)], {
    cwd: ROOT,
    stdio: 'ignore',
  })
  gwUp = await waitHttp(`http://127.0.0.1:${API_PORT}/api/health/live`, 20_000)
})

test.afterAll(async () => {
  gw?.kill('SIGTERM')
})

interface Watch { errors: string[]; badAssets: string[] }
function watch(page: Page): Watch {
  const w: Watch = { errors: [], badAssets: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('response', (r) => {
    const u = new URL(r.url())
    if (u.pathname.startsWith('/assets/') && r.status() !== 200) w.badAssets.push(`${r.status()} ${u.pathname}`)
  })
  return w
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(SHOTS, `${name}.png`), animations: 'disabled' })
}

test.describe('D1-MS1/MS2 集成冒烟', () => {
  test('首页：生产构建无 pageerror，构建产物可加载，重定向到默认世界', async ({ page }) => {
    const w = watch(page)
    await page.goto('/')
    await page.waitForURL(/\/world\/shenzhen/, { timeout: 4_000 })
    await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 30_000 })
    await expect(page.locator('[data-figure="header"]')).toBeVisible()
    await expect(page.locator('[data-viewport] canvas')).toHaveCount(1)
    await page.waitForTimeout(800)
    await shot(page, 'ms-home-1280x720')
    expect(w.badAssets).toEqual([])
    expect(w.errors).toEqual([])
  })

  test('/worlds 为前端路由，/worlds/<id>/** 转发到后端', async ({ page }) => {
    const w = watch(page)
    const nav = await page.goto('/worlds')
    expect(nav?.status()).toBe(200)
    expect(nav?.headers()['content-type'] ?? '').toContain('text/html')
    await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 30_000 })
    await page.waitForTimeout(500)
    await shot(page, 'ms-world-hub')
    expect(w.errors).toEqual([])
    // 数据路径不得落到 SPA 回退（无后端时 502，fake_gw 未提供静态世界时 404）
    const data = await page.request.get('/worlds/shenzhen/world.json')
    expect(data.status() === 200 && (data.headers()['content-type'] ?? '').includes('text/html')).toBe(false)
  })

  test('/api 经 preview 代理到 fake_gw', async ({ page }) => {
    test.skip(!gwUp, `fake_gw 未能在 ${API_PORT} 端口启动（端口占用或缺 .venv）`)
    const r = await page.request.get('/api/health/live')
    expect(r.status()).toBe(200)
  })
})
