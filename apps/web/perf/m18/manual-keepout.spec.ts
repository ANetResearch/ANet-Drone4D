// M18-AC-019 soft fence and approach warning on the real stack (WP-13 writes it for M18, d2-WP-05-to-M16 item 2; M18-FR-022,
// M15-FR-217, UX-FR-135; D2-AC-21 "预测进入敏感体前 3 s 告警，软围栏开启时不进入"). Backend: tests/e2e/sbstack.py behind the
// nginx-like preview (perf/d2/live.ts), test build in UI-only mode (realtime link and the manual pump, no 3D on SwiftShader).
// T6: the P600 hovers 180 m from a static person (demo-scale sensitivity: visual sector 150 m, acoustic hemisphere 120 m).
//   1. take over, hold the key that flies toward the person (body FLU mapping from the vehicle quaternion and the target
//      position, REST): the approach warning appears before the clamp ([data-manual-approach] then [data-manual-clamped]),
//      the vehicle never enters the configured volume (3D distance > the acoustic radius 120 m) and no
//      `target.discovered` event is produced;
//   2. fence off (confirmation), the same key: entering is possible and is counted as a discovery (judged by the criterion).
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, startLivePreview, startLiveStack, type LiveStack } from '../d2/live'

const PORT = 4196
let live: LiveStack | null = null
let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(300_000)
  live = await startLiveStack(2)
  server = await startLivePreview(PORT, live)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

interface VState { state: { pos_enu_m: number[]; q_xyzw: number[]; flight_state: string } }
const yawOf = (q: number[]): number => Math.atan2(2 * (q[3] * q[2] + q[0] * q[1]), 1 - 2 * (q[1] * q[1] + q[2] * q[2]))

async function vstate(request: APIRequestContext, sid: string, token: string): Promise<VState['state']> {
  const r = await request.get(`/api/sandbox/v1/sessions/${sid}/vehicles/p600-01/state`, { headers: { authorization: `Bearer ${token}` } })
  const j = (r.ok() ? await r.json() : {}) as Partial<VState>
  return j.state ?? { pos_enu_m: [Number.NaN, Number.NaN, 0], q_xyzw: [0, 0, 0, 1], flight_state: '' }
}

/** the WASD key whose body direction is closest to the bearing of the target */
function keyToward(pos: number[], yaw: number, tgt: number[]): string {
  let rel = Math.atan2(tgt[1] - pos[1], tgt[0] - pos[0]) - yaw
  rel = Math.atan2(Math.sin(rel), Math.cos(rel))
  const d = (rel * 180) / Math.PI
  if (Math.abs(d) <= 45) return 'KeyW'
  if (d > 45 && d < 135) return 'KeyA'
  if (d < -45 && d > -135) return 'KeyD'
  return 'KeyS'
}

async function discovered(request: APIRequestContext, sid: string, token: string): Promise<number> {
  const r = await request.get(`/api/sandbox/v1/sessions/${sid}/events`, { params: { since_seq: 0, limit: 1000, types: 'target.discovered' },
    headers: { authorization: `Bearer ${token}` } })
  return ((await r.json()) as { items: unknown[] }).items.length
}

async function startT6(page: Page, request: APIRequestContext): Promise<{ sid: string; token: string }> {
  await page.addInitScript(() => localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 })))
  await page.goto('/sandbox/sb-aaaaaaaaaa?reveal=shell&viewport=off')
  await expect(page.locator('[data-sandbox-unavailable]')).toBeVisible({ timeout: 60_000 })
  await page.locator('[data-sandbox-unavailable] button').last().click()
  await page.locator('[data-template="t6_manual"]').click()
  await page.locator('[data-start-go]').click()
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 90_000 })
  const h = JSON.parse((await page.evaluate(() => sessionStorage.getItem('awr.sandbox.session.v1'))) ?? '{}') as { sid: string; token: string }
  await expect.poll(async () => (await vstate(request, h.sid, h.token)).flight_state, { timeout: 150_000, intervals: [2000] }).toBe('FLYING')
  await page.waitForTimeout(8000)                                                  // reach the watch point
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-drone-id="p600-01"]').first().click()
  await page.locator('[data-manual-takeover="p600-01"]').click()
  await page.locator('[data-manual-confirm-go]').click()
  await expect(page.locator('[data-manual-bar]')).toBeVisible({ timeout: 30_000 })
  // the closed confirmation returns the focus to the takeover button; key presses must reach the document (manual scope)
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
  return h
}

// History: on 2026-10-05 the manual pump stopped after 2-3 setpoints after the takeover (d2-WP-13-to-M15-M11 item 1); the
// root cause was the server's 250 ms stream watchdog ending the takeover (209) while the input was idle, fixed in D2-FX-R2-web
// (10 Hz zero-velocity keepalive and the rt.worker bridge, d2-FX-R2-web-to-M16 item 4). D2-INT removed the fixme.

test('UI manual control moves the vehicle', async ({ page, request }) => {
  test.setTimeout(480_000)
  const h = await startT6(page, request)
  const sp = (): Promise<number> => page.evaluate(() => (window as unknown as { __ux: { sandbox: { setpoints: number } } }).__ux.sandbox.setpoints)
  const a = await vstate(request, h.sid, h.token)
  const n0 = await sp()
  await page.keyboard.down('KeyW')
  await page.waitForTimeout(4000)
  await page.keyboard.up('KeyW')
  const b = await vstate(request, h.sid, h.token)
  const n1 = await sp()
  test.info().annotations.push({ type: 'manual', description: JSON.stringify({ setpoints: n1 - n0, a: a.pos_enu_m, b: b.pos_enu_m }) })
  try {
    expect(Math.hypot(b.pos_enu_m[0] - a.pos_enu_m[0], b.pos_enu_m[1] - a.pos_enu_m[1])).toBeGreaterThan(3)
  } finally {
    await page.locator('[data-manual-release]').click({ timeout: 10_000 }).catch(() => undefined)
    await request.delete(`/api/sandbox/v1/sessions/${h.sid}`, { headers: { authorization: `Bearer ${h.token}` } })
  }
})

test('fence on: warned before the clamp, never inside, no discovery', async ({ page, request }) => {
  test.setTimeout(480_000)
  const h = await startT6(page, request)
  const tgt = ((await (await request.get(`/api/sandbox/v1/sessions/${h.sid}/targets/person-01`, { headers: { authorization: `Bearer ${h.token}` } })).json()) as { pos: number[] }).pos
  await expect(page.locator('[data-manual-fence="clamp"]')).toBeVisible()
  let warnedAt: number | null = null
  let clampedAt: number | null = null
  let minDist = Number.POSITIVE_INFINITY
  const trace: string[] = []
  const t0 = Date.now()
  let held: string | null = null
  while (Date.now() - t0 < 60_000 && clampedAt === null) {
    const s = await vstate(request, h.sid, h.token)
    // three-dimensional distance: the acoustic volume is a 120 m hemisphere and the server's soft fence clamps on the signed
    // 3D distance, so the vehicle slides up along the dome (horizontal distance below 120 m is allowed; d2-FX-R2-web-to-M16 item 4)
    minDist = Math.min(minDist, Math.hypot(s.pos_enu_m[0] - tgt[0], s.pos_enu_m[1] - tgt[1], s.pos_enu_m[2] - (Number.isFinite(tgt[2]) ? tgt[2] : 0)))
    const key = keyToward(s.pos_enu_m, yawOf(s.q_xyzw), tgt)
    trace.push(`${key}@${s.pos_enu_m.map((v) => v.toFixed(0)).join(',')}`)
    if (key !== held) {                                           // keep the input continuous (the pump stops on zero)
      if (held) await page.keyboard.up(held)
      await page.keyboard.down(key)
      held = key
    }
    await page.waitForTimeout(500)
    if (warnedAt === null && (await page.locator('[data-manual-approach]').count()) > 0) warnedAt = Date.now()
    if ((await page.locator('[data-manual-clamped]').count()) > 0) clampedAt = Date.now()
  }
  if (held) await page.keyboard.up(held)
  test.info().annotations.push({ type: 'keepout', description: JSON.stringify({ warnedAt: warnedAt && warnedAt - t0, clampedAt: clampedAt && clampedAt - t0, minDist, trace }) })
  expect(warnedAt).not.toBeNull()
  expect(clampedAt).not.toBeNull()
  expect(warnedAt!).toBeLessThanOrEqual(clampedAt!)
  expect(minDist).toBeGreaterThan(120)
  expect(await discovered(request, h.sid, h.token)).toBe(0)
  await page.locator('[data-manual-release]').click()
  await request.delete(`/api/sandbox/v1/sessions/${h.sid}`, { headers: { authorization: `Bearer ${h.token}` } })
})
