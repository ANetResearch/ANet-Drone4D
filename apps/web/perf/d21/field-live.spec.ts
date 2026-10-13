// Field views and the perception readout on the real stack (WP-13b, M16; D2-AC-52 card side, D2-AC-53 page side; R-D2-40 to
// R-D2-42; AWR-04 §15.10.4, §15.11.3). Backend: perf/d21/live21.ts (tests/e2e/sbstack.py), a real T1 session (two P600, the
// static person) held by the test build's page, viewport on.
//   1. the readout card shows the server's numbers: the expanded `awr.target.quant.v1` of SB-73 for person-01, fed to the card
//      through the perception feed of the test build (`__uxInject.quant`, the same ingest as the topic), gives one row per pair
//      with range, dominant band level / threshold, angular size / threshold and probabilities as the server computed them,
//      and the readout sentence of the first row; no write request;
//   2. the field toolbar on the live sandbox page: acoustic, visual, thermal, radar and none in turn (and Shift+V), no program
//      compiled after the reveal, no error; the legend shows the unit of each view once data is there;
//   3. live field frames: with the acoustic view and all volumes on, the gateway's `field/acoustic` and `field/volumes` should
//      turn into drawn points and body anchors; the session gateway does not forward these topics yet (d2-WP-19b-to-M11, M08):
//      the case then records the gap and skips instead of passing silently.
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, createSession, endSession, ensureBuild, frames, holdSession, isTestBuild, pageHeld, revealed, session, shot, startLive21,
  startLive21Preview, waitPoolEmpty, waitState, watch, type Held, type Live21,
} from './live21'

const PORT = 4199
let live: Live21 | null = null
let server: PreviewServer | null = null
let held: Held | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  const dist = ensureBuild('test')
  live = await startLive21({ maxSessions: 2, releaseFrom: dist })
  server = await startLive21Preview(PORT, live, dist)
  held = await createSession(live, 'MSIXFIELDLIVEHINTAAAAAAA', 't1_point_watch')
  await waitState(live, held, ['READY', 'ACTIVE'])
})
test.afterAll(async () => {
  await endSession(live!, held)
  await waitPoolEmpty(live!).catch(() => null)
  await server?.close()
  await live?.stop()
})

type H = { __perf: { gpu: { programs: number } }; __vp: { field: { stats(): { kind: string; points: number; bodyAnchors: number; uavAnchors: number } | null } };
  __uxInject: { quant(f: unknown, select?: boolean): void } }

/** the previous test's page closed: pagehide parked the shared session through the SB-72 beacon (AWR-04 §15.7.1); resume it
 *  (SB-70, a new token) before the next page opens (D2-INT) */
async function ensureLive(): Promise<void> {
  let st = String((await session(live!, held!)).state)
  if (st === 'PARKING') {
    await expect.poll(async () => String((await session(live!, held!)).state), { timeout: 30_000 }).not.toBe('PARKING')
    st = String((await session(live!, held!)).state)
  }
  if (st !== 'PARKED') return
  const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${held!.sid}/resume`, {
    method: 'POST', headers: { authorization: `Bearer ${held!.token}`, 'content-type': 'application/json' }, body: '{}',
  })
  const j = await r.json() as { token?: string }
  if (j.token) held = { ...held!, token: j.token }
  await waitState(live!, held!, ['READY', 'ACTIVE'])
}

async function openSandbox(page: Page): Promise<void> {
  await ensureLive()
  await holdSession(page, held!)
  await page.goto(`/sandbox/${held!.sid}`)
  await revealed(page)
  test.skip(!(await isTestBuild(page)), 'needs a test build')
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('[data-viewport] canvas')).toBeVisible({ timeout: 60_000 })
}

interface Pair { uav: string; range_m: number; acoustic: { lp_band_db: number; thr_dba: number; margin_db: number; p: number } | null;
  visual: { theta_arcmin: number; theta_th_arcmin: number; n_eq_px: number; p: number } | null; p_any: number }

test('the readout card shows the server readout of SB-73 row by row', async ({ page }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  const writes: string[] = []
  page.on('request', (r) => { if (!['GET', 'HEAD', 'OPTIONS'].includes(r.method()) && r.url().includes('quant')) writes.push(r.url()) })
  await openSandbox(page)
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 30_000 })
  await page.locator('[data-target-row]').first().click()
  await expect(page.locator('[data-target-detail]')).toBeVisible()
  const tid = await page.locator('[data-target-detail]').getAttribute('data-target-detail')
  expect(tid).toBe('person-01')
  await page.locator('[data-target-tab="quant"]').click()
  const h = (await pageHeld(page)) ?? held!
  const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${h.sid}/perception/quant?target=person-01`, { headers: { authorization: `Bearer ${h.token}` } })
  expect(r.status).toBe(200)
  const q = await r.json() as { target: string; pairs: Pair[] }
  expect(q.pairs.length).toBeGreaterThanOrEqual(2)
  await page.evaluate((f) => (window as unknown as H).__uxInject.quant(f), q)
  const card = page.locator(`[data-quant-card="${q.pairs.length}"]`)
  await expect(card).toBeVisible({ timeout: 10_000 })
  const rows = card.locator('tbody tr')
  await expect(rows).toHaveCount(q.pairs.length)
  // the live `target/person-01/quant` frames (2 Hz, forwarded since WP-03b) replace the injected REST frame within 0.5 s, and
  // the vehicles move meanwhile: the card's values are compared with SB-73 within what 1 s of flight changes (D2-INT)
  const near = (a: number, b: number, tol: number): boolean => Math.abs(a - b) <= tol
  for (let i = 0; i < q.pairs.length; i++) {
    const p = q.pairs[i]
    const row = rows.filter({ hasText: p.uav }).first()
    const text = (await row.innerText()).replace(/\s+/g, ' ')
    expect(text).toContain(p.uav)
    const frac = [...text.matchAll(/(-?[0-9]+\.[0-9]) \/ (-?[0-9]+\.[0-9])/g)].map((m) => [Number(m[1]), Number(m[2])])
    if (p.acoustic) expect(frac.some(([a, b]) => near(a, p.acoustic!.lp_band_db, 1.0) && near(b, p.acoustic!.thr_dba, 0.11)), text).toBe(true)
    if (p.visual) expect(frac.some(([a, b]) => near(a, p.visual!.theta_arcmin, Math.max(0.5, 0.1 * p.visual!.theta_arcmin)) && near(b, p.visual!.theta_th_arcmin, 0.11)), text).toBe(true)
  }
  const first = q.pairs[0]
  await rows.filter({ hasText: first.uav }).first().click()
  const sentence = page.locator(`[data-quant-sentence="${first.uav}"]`)
  await expect(sentence).toBeVisible()
  if (first.acoustic) await expect(sentence).toContainText(`dB(A) / 阈 ${first.acoustic.thr_dba.toFixed(1)}`)
  await shot(page, 'd21-field-live-quant')
  expect(writes).toEqual([])
  await page.evaluate(() => (window as unknown as { __vp: { field: { setLevel(l: number | null): void } } }).__vp.field.setLevel(null))
  expect(w.errors).toEqual([])
})

test('field toolbar on the live sandbox page: five views, Shift+V, no program compiled, no error', async ({ page }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  await openSandbox(page)
  await frames(page, 20)
  const programs0 = await page.evaluate(() => (window as unknown as H).__perf.gpu.programs)
  const bar = page.locator('[data-figure="field-bar"]')
  await expect(bar).toBeVisible()
  for (const k of ['acoustic', 'visual', 'thermal', 'radar', 'none']) {
    await bar.locator(`[data-field-option="${k}"]`).click()
    await expect(page.locator('[data-field-view]').first()).toHaveAttribute('data-field-view', k)
    await frames(page, 10)
  }
  await page.mouse.click(800, 300)
  for (const want of ['acoustic', 'visual']) {
    await page.keyboard.press('Shift+KeyV')
    await expect(page.locator('[data-field-view]').first()).toHaveAttribute('data-field-view', want)
  }
  await bar.locator('[data-volumes-option="all"]').click()
  await frames(page, 20)
  expect(await page.evaluate(() => (window as unknown as H).__perf.gpu.programs)).toBe(programs0)
  expect(w.gl).toEqual([])
  await page.evaluate(() => (window as unknown as { __vp: { field: { setLevel(l: number | null): void } } }).__vp.field.setLevel(null))
  expect(w.errors).toEqual([])
})

test('live field frames turn into drawn points and body anchors', async ({ page }) => {
  test.setTimeout(300_000)
  const w = watch(page)
  await openSandbox(page)
  // subscribe once the link is LIVE (the subscription is what makes the session gateway forward field/*)
  await page.waitForFunction(() => (window as unknown as { __vp: { conn(): string } }).__vp.conn() === 'LIVE', null, { timeout: 60_000 })
  const bar = page.locator('[data-figure="field-bar"]')
  await bar.locator('[data-field-option="acoustic"]').click()
  await bar.locator('[data-volumes-option="all"]').click()
  // pin the field level once the layer exists: under SwiftShader on a loaded host the PerfGovernor steps the field down to off
  // within seconds (473 -> 238 -> 192 -> 0 points), which says nothing about the live frames (D2-INT)
  await page.evaluate(() => (window as unknown as { __vp: { field: { setLevel(l: number | null): void } } }).__vp.field.setLevel(0))
  let st: { points: number; bodyAnchors: number; uavAnchors: number } | null = null
  const t0 = Date.now()
  while (Date.now() - t0 < 30_000) {
    st = await page.evaluate(() => (window as unknown as H).__vp.field.stats())
    // D2-ACC-1 (2026-10-11) tool fix: wait for the vehicle anchors as well (the first frames can carry the body anchors and points
    // before the UAV anchors; breaking on body anchors alone made the uavAnchors assertion below race the next frame)
    if (st && st.points > 0 && st.bodyAnchors > 0 && st.uavAnchors >= 2) break
    await page.waitForTimeout(500)
  }
  test.info().annotations.push({ type: 'field-stats', description: JSON.stringify(st) })
  expect(st?.points ?? 0, 'field/* frames from the session gateway drawn').toBeGreaterThan(0)
  expect(st!.uavAnchors).toBeGreaterThanOrEqual(2)
  expect(st!.bodyAnchors).toBeGreaterThanOrEqual(1)
  await expect(page.locator('[data-field-legend="acoustic"]')).toHaveAttribute('data-field-unit', 'dba', { timeout: 10_000 })
  await shot(page, 'd21-field-live-acoustic')
  await page.evaluate(() => (window as unknown as { __vp: { field: { setLevel(l: number | null): void } } }).__vp.field.setLevel(null))
  expect(w.errors).toEqual([])
})
