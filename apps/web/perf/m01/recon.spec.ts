// Mock reconstruction product world in the Web (D1-AC-22 web part; M01-AC-008, AC-019, AC-020 display data). Owner: M01.
// beforeAll runs a small Mock job (`python -m awr.reconstruction run`, Shenzhen helix, 60 frames, source_keep 0.05) into
// a temporary worlds directory (the built Shenzhen world is symlinked, never modified) and starts the real backend on it
// (startBackend with AWR_WORLDS_DIR; no job-worker in the ci profile). Checks:
//   * /api/worlds lists the product world with scale_status gnss; /worlds/<id>/world.json carries tags [recon, synthetic];
//   * /world/<id> reveals, first screen arrives (ttfp > 0) and points are drawn, with no pageerror;
//   * GET /api/recon/engines answers (without a job-worker every engine reports WORKER_UNAVAILABLE).
// TTFP <= 1.0 s (D1-AC-02) is asserted only under the performance harness (AWR_PERF_BASE set, perf/m01/cases.mjs);
// the job overlay UI (progress <= 4 Hz, scale_status badge) belongs to M15 / M03 and is requested there.
import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { startBackend, type Backend } from '../skeleton.server'

const ROOT = resolve(import.meta.dirname, '../../../..')
const TARGET = 'shenzhen-recon-01'
const HAVE_WORLD = existsSync(join(ROOT, 'worlds/shenzhen/geometry/pointcloud/source/source.json'))

test.describe('M01 Mock reconstruction world', () => {
  test.skip(!HAVE_WORLD, 'worlds/shenzhen not built (make worlds)')
  test.describe.configure({ mode: 'serial', timeout: 240_000 })
  let tmp = ''
  let be: Backend | null = null

  test.beforeAll(async () => {
    test.setTimeout(240_000)
    tmp = mkdtempSync(join(tmpdir(), 'awr-m01-recon-'))
    const worlds = join(tmp, 'worlds')
    mkdirSync(join(worlds, '.status'), { recursive: true })
    symlinkSync(join(ROOT, 'worlds/shenzhen'), join(worlds, 'shenzhen'))
    copyFileSync(join(ROOT, 'worlds/.status/shenzhen.json'), join(worlds, '.status/shenzhen.json'))
    execFileSync(join(ROOT, '.venv/bin/python'), ['-m', 'awr.reconstruction', 'run', '--world', 'shenzhen', '--path', 'helix',
      '--frames', '60', '--keep', '0.05', '--target', TARGET, '--worlds', worlds, '--runs', join(tmp, 'runs')],
    { cwd: ROOT, stdio: 'ignore', timeout: 180_000, env: { ...process.env, OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1' } })
    be = await startBackend(undefined, { AWR_WORLDS_DIR: worlds })
  })

  test.afterAll(async () => {
    await be?.close()
    if (tmp) rmSync(tmp, { recursive: true, force: true })
  })

  test('catalog and manifest expose the product world', async () => {
    const tok = await (await fetch(`${be!.url}/api/auth/token`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: 'viewer' }) })).json() as { token: string }
    const auth = { Authorization: `Bearer ${tok.token}` }
    const worlds = await (await fetch(`${be!.url}/api/worlds`, { headers: auth })).json() as { items: Array<{ id: string; scale_status: string; status: string }> }
    const w = worlds.items.find((x) => x.id === TARGET)
    expect(w, JSON.stringify(worlds.items.map((x) => x.id))).toBeTruthy()
    expect(w!.scale_status).toBe('gnss')
    const wj = await (await fetch(`${be!.url}/worlds/${TARGET}/world.json`)).json() as { tags: string[]; scaleStatus: string; layers: Array<{ format: string }> }
    expect(wj.tags).toEqual(['recon', 'synthetic'])
    expect(wj.layers.some((l) => l.format === 'recon-ir@1')).toBe(true)
    const eng = await (await fetch(`${be!.url}/api/recon/engines`, { headers: auth })).json() as { items: Array<{ engine: string; reason: string }> }
    expect(eng.items.map((i) => i.engine).sort()).toEqual(['colmap', 'da3', 'lingbot_map', 'mapanything', 'mock', 'vggt'])
  })

  test('the product world opens in the web client', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${be!.url}/world/${TARGET}`)
    await page.waitForFunction(() => {
      const w = window as unknown as { __ux?: { boot?: { state?: string } }; __perf?: { load?: { ttfp?: number }; pc?: { drawn?: number } } }
      return w.__ux?.boot?.state === 'REVEALED' && (w.__perf?.load?.ttfp ?? 0) > 0 && (w.__perf?.pc?.drawn ?? 0) > 0
    }, undefined, { timeout: 60_000 })
    const ttfp = await page.evaluate(() => (window as unknown as { __perf: { load: { ttfp: number } } }).__perf.load.ttfp)
    test.info().annotations.push({ type: 'ttfp_ms', description: String(ttfp) })
    if (process.env.AWR_PERF_BASE) expect(ttfp).toBeLessThanOrEqual(1000)
    expect(errors).toEqual([])
  })
})
