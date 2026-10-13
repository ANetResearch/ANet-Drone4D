// D2-INT integration cases on the live sandbox stack (D2-AC-24 basemap switch, D2-AC-16, D2-AC-17 and D2-AC-35 task types,
// D2-AC-19 relay chain in a live session). Backend: perf/d21/live21.ts (tests/e2e/sbstack.py, supervisor `sandbox` profile,
// real sim-core per session) behind a private preview routed like nginx; test build (D21_DIST or .cache/d21/test).
//   1. basemap: a T5 session held by the page switches through all seven worlds from the session bar (Dialog, Select, go);
//      every switch reloads the page into the new world (session world, viewport world, same template, LIVE link);
//   2. task types: in a T5 session the seven M20 task types (point_watch, polyline_patrol, area_patrol, perimeter_patrol,
//      area_scan_gsd, area_guard, relay_watch) are created through the session REST with synthcity anchor geometry and
//      started; each reaches RUNNING (relay_watch: a station is selected, never INFEASIBLE) and shows as a row of the Dock's
//      task tab; each is cancelled before the next (tasks_active_max);
//   2b. targets in the UI (D2-AC-11, D2-INT round 2): in a live T1 session the operator adds a person from the targets panel
//      (class menu, then a click on the viewport ground: TARGET_PLACE), the target exists in SB REST and its detail opens;
//      removing it from the detail page (confirmation) takes it off REST and off the panel;
//   3. S7 relay under fast-forward: a session from the full S7 configuration (ADR-094: nest A with six P600, VTOL pad B with
//      two AWR-V1, three targets, the V1 area guard that hands each person over to a relay) runs at the granted rate (asked
//      x10); the guard turns into relays and one relay completes >= 2 handovers (two full relay cycles: first vehicle ->
//      relief -> relief) with discovered 0 on every relay and no target.discovered event, read from SB-59 relay stats and
//      shown in the Dock's relay tab. D2-INT round 2: the full S7 (the V1 guard cycle works since D2-FX-R2-tasking); with
//      D2INT_S7_RELAY_PART=1 the reduced relay part of round 1 (nest A, three P600, the static person, relay_watch person-01).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import {
  baseUrl, createSession, endSession, ensureBuild, holdSession, isTestBuild, pageHeld, REPO, revealed, shot, startLive21, startLive21Preview,
  waitPoolEmpty, waitState, watch, type Held, type Live21,
} from '../d21/live21'

const PORT = 4205
const WORLDS = ['shenzhen', 'suzhou', 'newyork', 'chicago', 'shanghai', 'sanfrancisco', 'synthcity'] as const
let live: Live21 | null = null
let seq = 0
let server: PreviewServer | null = null

test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 }, locale: 'zh-CN' })
test.describe.configure({ mode: 'serial' })
test.beforeAll(async () => {
  test.setTimeout(600_000)
  const dist = ensureBuild('test')
  live = await startLive21({ maxSessions: 2, releaseFrom: dist, set: ['run.scenario=s0-synthcity-showcase'] })
  server = await startLive21Preview(PORT, live, dist)
})
test.afterAll(async () => {
  await server?.close()
  await live?.stop()
})

type Vp = { __vp: { session(): { worldId: string } | null; conn(): string } }
const vpWorld = (page: Page): Promise<string | null> => page.evaluate(() => (window as unknown as Vp).__vp.session()?.worldId ?? null)

/** the Dock tab (the Dock may start folded at this size: open it first; never waits for the test timeout) */
async function openDockTab(page: Page, name: RegExp): Promise<void> {
  const tab = page.locator('[data-slot="dock"] [role="tab"]').filter({ hasText: name }).first()
  if (!(await tab.isVisible().catch(() => false))) await page.getByRole('button', { name: '显示底部面板' }).click({ timeout: 5_000 }).catch(() => null)
  await tab.click({ timeout: 10_000 }).catch(() => null)
}

async function sbx(h: Held, method: string, path: string, body?: unknown): Promise<{ status: number; json: Record<string, unknown> }> {
  const r = await fetch(`${live!.base}/api/sandbox/v1/sessions/${h.sid}${path}`, {
    method, headers: { authorization: `Bearer ${h.token}`, 'content-type': 'application/json', 'idempotency-key': `d2int-${++seq}-${Date.now()}` },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const txt = await r.text()
  return { status: r.status, json: txt ? (JSON.parse(txt) as Record<string, unknown>) : {} }
}

test('basemap: the session bar switches a T5 session through the seven worlds', async ({ page }) => {
  test.setTimeout(1_500_000)
  const w = watch(page)
  let h: Held | null = null
  try {
    h = await createSession(live!, 'DTWOINTWORLDSWITCHHINTAA', 't5_area_guard')
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    await page.goto(`/sandbox/${h.sid}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    expect(await vpWorld(page)).toBe('synthcity')
    for (const world of WORLDS) {
      const held = (await pageHeld(page).catch(() => null)) ?? h
      await expect(page.locator('[data-session-world]')).toBeEnabled({ timeout: 90_000 })
      await page.locator('[data-session-world]').click()
      await expect(page.locator('[data-world-switch]').first()).toBeVisible()
      await page.locator('[data-world-switch-select]').click()
      await page.getByRole('option').filter({ hasText: `· ${world}` }).first().click()
      // the session respawns in the new world; the link follows at once (1012 world_switch) and the page reloads into the new
      // world once the session is live again (ui/sandbox/dialogs.tsx useWorldFollow)
      const reloaded = page.waitForEvent('load', { timeout: 300_000 })
      await page.locator('[data-world-switch-go]').click()
      await expect.poll(async () => (await sbx(held, 'GET', '')).json.world_id, { timeout: 180_000, intervals: [1000] }).toBe(world)
      await reloaded
      await revealed(page, 240_000)
      await expect.poll(() => vpWorld(page).catch(() => null), { timeout: 120_000, intervals: [1000] }).toBe(world)
      await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
      await page.waitForTimeout(3000)
      expect(await vpWorld(page).catch(() => null), 'no second reload after the switch').toBe(world)
      const s = (await sbx((await pageHeld(page).catch(() => null)) ?? held, 'GET', '')).json
      expect(s.world_id).toBe(world)
      expect(s.template_id).toBe('t5_area_guard')
      await shot(page, `d2int-world-${world}`)
    }
    expect(w.errors).toEqual([])
    expect(w.gl).toEqual([])
  } finally {
    const held = await pageHeld(page).catch(() => null)
    await endSession(live!, held ?? h)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('task types: the seven M20 types run in a live T5 session and list in the Dock', async ({ page }) => {
  test.setTimeout(1_200_000)
  const a = JSON.parse(readFileSync(join(REPO, 'scenarios', 'sandbox', 'anchors', 'synthcity.json'), 'utf8')) as Record<string, unknown>
  const guard = a.aoi_guard as number[][]
  const scan = a.aoi_scan as number[][]
  const wp = a.watch_point as number[]
  const specs: Record<string, unknown>[] = [
    { type: 'point_watch', label: '定点 D2-INT', params: { point: [wp[0], wp[1], null] } },
    { type: 'polyline_patrol', label: '巡线 D2-INT', params: { waypoints: (a.polyline as number[][]).map((p) => [p[0], p[1]]) } },
    { type: 'area_patrol', label: '区域巡查 D2-INT', params: { polygon: guard } },
    { type: 'perimeter_patrol', label: '巡边 D2-INT', params: { polygon: guard } },
    { type: 'area_scan_gsd', label: 'GSD 扫描 D2-INT', params: { polygon: scan, gsd_cm: 3 } },
    { type: 'area_guard', label: '区域值守 D2-INT', params: { polygon: guard, target_class: 'person' } },
    { type: 'relay_watch', label: '接力监控 D2-INT', params: { target: '' } },
  ]
  const ts = (a.target_static as { pos: number[] }).pos
  const w = watch(page)
  let h: Held | null = null
  const seen: Record<string, string> = {}
  try {
    h = await createSession(live!, 'DTWOINTTASKTYPESHINTAAAA', 't5_area_guard')
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    await page.goto(`/sandbox/${h.sid}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    // the template's own guard task: cancel it so the seven run one at a time on the session's vehicles
    await expect.poll(async () => ((await sbx(h!, 'GET', '/tasks')).json.items as unknown[] | undefined)?.length ?? 0, { timeout: 60_000 }).toBeGreaterThan(0)
    for (const t of (await sbx(h, 'GET', '/tasks')).json.items as { tid: string }[]) await sbx(h, 'POST', `/tasks/${t.tid}:cancel`)
    // the template's people sit inside the anchor geometry of the other templates (583 KEEPOUT_VIOLATION for the polyline):
    // remove them for the six geometric types, add one static person at the anchor's target position for relay_watch
    for (const t of ((await sbx(h, 'GET', '/targets')).json.items ?? []) as { id: string }[]) await sbx(h, 'DELETE', `/targets/${t.id}`)
    await openDockTab(page, /^任务$/)
    for (const spec of specs) {
      if (spec.type === 'relay_watch') {
        const tg = await sbx(h, 'POST', '/targets', { class: 'person', pos: [ts[0], ts[1], null] })
        expect(tg.status, JSON.stringify(tg.json).slice(0, 300)).toBe(201)
        spec.params = { target: String(tg.json.id) }
      }
      const c = await sbx(h, 'POST', '/tasks', { ...spec, common: { priority: 'normal' } })
      expect(c.status, `${String(spec.type)}: ${JSON.stringify(c.json).slice(0, 300)}`).toBe(201)
      const tid = String(c.json.tid)
      await sbx(h, 'POST', `/tasks/${tid}:start`)
      let last: Record<string, unknown> = {}
      await expect.poll(async () => {
        last = (await sbx(h!, 'GET', `/tasks/${tid}`)).json
        return String(last.state)
      }, { timeout: 120_000, intervals: [1000], message: `${String(spec.type)} state` }).toBe('RUNNING')
      if (spec.type === 'relay_watch') {
        // a station was found and a vehicle is on its way or on station (SB-59 `next` / `on_station`), the task stays RUNNING
        await expect.poll(async () => {
          const r = (await sbx(h!, 'GET', `/relay/${tid}/stats`)).json as { on_station?: unknown[]; next?: unknown }
          const st = String((await sbx(h!, 'GET', `/tasks/${tid}`)).json.state)
          return st === 'RUNNING' && ((r.on_station?.length ?? 0) > 0 || (r.next !== null && r.next !== undefined))
        }, { timeout: 120_000, intervals: [1000] }).toBe(true)
      }
      await openDockTab(page, /^任务$/)
      await expect(page.locator(`[data-task-row="${tid}"]`)).toBeVisible({ timeout: 30_000 })
      seen[String(spec.type)] = String(last.state)
      await shot(page, `d2int-task-${String(spec.type)}`)
      const x = await sbx(h, 'POST', `/tasks/${tid}:cancel`)
      expect(x.status).toBe(200)
    }
    expect(Object.keys(seen).sort()).toEqual(specs.map((s) => String(s.type)).sort())
    expect(w.errors).toEqual([])
  } finally {
    test.info().annotations.push({ type: 'task-states', description: JSON.stringify(seen) })
    const held = await pageHeld(page).catch(() => null)
    await endSession(live!, held ?? h)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('targets: add a person with a viewport click, then remove it from the detail page', async ({ page }) => {
  test.setTimeout(600_000)
  const w = watch(page)
  const h = await createSession(live!, 'DTWOINTTARGETSUIHINTAAAA', 't1_point_watch')
  try {
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    await page.goto(`/sandbox/${h.sid}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    await waitState(live!, h, ['ACTIVE'])
    const ids = async (): Promise<string[]> => (((await sbx(h, 'GET', '/targets')).json.items ?? []) as { id: string }[]).map((t) => t.id)
    const before = await ids()
    await page.locator('[data-entity="targets"]').click()
    await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 30_000 })
    await page.locator('[data-targets-add]').click()
    await page.locator('[data-targets-add-class="person"]').click()
    // the viewport ground below the centre of the canvas (TARGET_PLACE picks the ground; 540 keeps the tool)
    const box = (await page.locator('canvas').first().boundingBox())!
    let added: string[] = []
    for (const [fx, fy] of [[0.5, 0.62], [0.42, 0.7], [0.58, 0.55]] as const) {
      await page.mouse.click(box.x + box.width * fx, box.y + box.height * fy)
      await expect.poll(async () => (added = (await ids()).filter((x) => !before.includes(x))).length, { timeout: 15_000 }).toBeGreaterThan(0).catch(() => null)
      if (added.length) break
    }
    expect(added, 'a target was placed').toHaveLength(1)
    const id = added[0]
    await expect(page.locator(`[data-target-detail="${id}"]`)).toBeVisible({ timeout: 20_000 })
    await shot(page, 'd2int-target-placed')
    await page.locator('[data-target-remove]').click()
    await page.locator('[data-confirm-action="target.remove"]').click()
    await expect.poll(async () => (await ids()).includes(id), { timeout: 20_000 }).toBe(false)
    await expect(page.locator(`[data-target-row="${id}"]`)).toHaveCount(0, { timeout: 20_000 })
    expect(w.errors).toEqual([])
  } finally {
    const held = await pageHeld(page).catch(() => null)
    await endSession(live!, held ?? h)
    await waitPoolEmpty(live!).catch(() => null)
  }
})

test('S7 relay under fast-forward: two full relay cycles, discovered 0', async ({ page }) => {
  // wall budget of the relay loop (minutes, D2INT_S7_MIN; default 45): the governor grants by measured cost, so a loaded
  // machine may run the full S7 at x1 with RTF < 1 (D2-INT round 2: about 60 min for two handovers)
  const budgetMs = (Number(process.env.D2INT_S7_MIN) || 45) * 60_000
  test.setTimeout(budgetMs + 600_000)
  const s7 = JSON.parse(readFileSync(join(REPO, 'scenarios', 's7-synthcity-relay.json'), 'utf8')) as Record<string, unknown> & {
    nests: { id: string }[]; vehicles: { id: string; nest_id: string }[]; targets: { id: string }[]; tasks: unknown[] }
  const part = process.env.D2INT_S7_RELAY_PART === '1'
  const cfg = part ? {
    ...s7,
    nests: s7.nests.filter((n) => n.id === 'nest-a'),
    vehicles: s7.vehicles.filter((v) => v.nest_id === 'nest-a').slice(0, 3),
    targets: s7.targets.filter((t) => t.id === 'person-01'),
    tasks: [{ type: 'relay_watch', label: '接力监控 S7', params: { target: 'person-01', level_req: 'R' }, common: { priority: 'high', on_detect: 'notify' } }],
  } : s7
  test.info().annotations.push({ type: 's7-config', description: part ? 'relay part (nest A, 3 x P600, person-01)' : 'full S7 (ADR-094)' })
  const w = watch(page)
  let h: Held | null = null
  let last: Record<string, unknown> = {}
  try {
    const r = await fetch(`${live!.base}/api/sandbox/v1/sessions`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ world: 'synthcity', config_snapshot: cfg, principal_hint: 'DTWOINTSSEVENRELAYHINTAA' }),
    })
    const j = await r.json() as { session?: { sid: string }; token?: string }
    expect(r.status, JSON.stringify(j).slice(0, 300)).toBe(201)
    h = { sid: j.session!.sid, token: j.token! }
    await waitState(live!, h, ['READY', 'ACTIVE'])
    await holdSession(page, h)
    // the operator page without the 3D viewport (test switch `viewport=off`, UI-only): on SwiftShader the S7 scene takes about
    // 5 cores, the governor then grants x1 by measured cost and the case takes over an hour (D2-INT round 2); the relay itself
    // and the Dock's relay tab do not need the canvas. D2INT_S7_VIEWPORT=1 keeps the viewport.
    const vpOff = process.env.D2INT_S7_VIEWPORT === '1' ? '' : '?reveal=shell&viewport=off'
    await page.goto(`/sandbox/${h.sid}${vpOff}`)
    await revealed(page)
    test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
    await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 60_000 })
    // the session token is renewed by the page (keepalive swaps it in sessionStorage); the creation token expires after the
    // session lifetime, so REST reads use the page's current token (D2-INT round 2: with the stale token every read was 401
    // after 30 min and the relay count stayed 0 although the relays had 2 and 3 handovers)
    const fresh = async (): Promise<void> => {
      const p = await pageHeld(page).catch(() => null)
      if (p?.sid === h!.sid && p.token) h = { sid: p.sid, token: p.token }
    }
    const relays = async (): Promise<string[]> => {
      await fresh()
      const items = ((await sbx(h!, 'GET', '/tasks')).json.items ?? []) as { tid: string; type: string; state: string }[]
      return items.filter((t) => t.type === 'relay_watch').map((t) => t.tid)
    }
    if (part) await expect.poll(async () => (await relays()).length, { timeout: 60_000 }).toBeGreaterThan(0)
    const sp = await sbx(h, 'POST', '/clock', { op: 'speed', rate: 10 })
    test.info().annotations.push({ type: 'speed', description: JSON.stringify(sp.json).slice(0, 300) })
    // an operator watching the relay: a pointer move every 10 s keeps the page ACTIVE (otherwise power saving parks the
    // session after sandbox_idle_s, AWR-04 §15.7) while the relay runs at the granted rate
    const t0 = Date.now()
    let ho = 0
    let asked = Date.now()
    while (Date.now() - t0 < budgetMs) {
      await page.mouse.move(700 + ((Date.now() / 1000) % 40), 400)
      // the governor grants from a cost estimate that settles after the first minutes: ask again while the grant is low
      if (Date.now() - asked > 60_000) {
        asked = Date.now()
        const clock = (await sbx(h, 'GET', '')).json.clock as { rate_granted?: number; rate_cap?: number } | undefined
        if ((clock?.rate_granted ?? 1) < Math.min(5, Math.floor(clock?.rate_cap ?? 1))) await sbx(h, 'POST', '/clock', { op: 'speed', rate: 5 })
      }
      // the full S7: the V1 guard hands each person over to its own relay_watch child; every relay counts
      const all: Record<string, unknown> = {}
      for (const tid of await relays()) {
        const st = (await sbx(h, 'GET', `/relay/${tid}/stats`)).json
        all[tid] = st
        const hv = st.handovers as { count?: number } | number | undefined
        ho = Math.max(ho, typeof hv === 'number' ? hv : hv?.count ?? 0)
        expect(Number(st.discovered ?? 0), `discovered (${tid})`).toBe(0)
      }
      last = all
      if (ho >= 2) break
      await page.waitForTimeout(10_000)
    }
    expect(ho, `handovers after ${Math.round((Date.now() - t0) / 1000)} s`).toBeGreaterThanOrEqual(2)
    const disc = (await sbx(h, 'GET', '/events?since_seq=0&limit=1000&types=target.discovered')).json.items as unknown[] | undefined
    expect(disc ?? [], 'target.discovered events').toEqual([])
    expect(String((await sbx(h, 'GET', '')).json.state)).toMatch(/^(ACTIVE|READY|IDLE)$/)
    await openDockTab(page, /^接力监控$/)
    await page.waitForTimeout(1500)
    await shot(page, 'd2int-s7-relay')
    expect(w.errors).toEqual([])
  } finally {
    test.info().annotations.push({ type: 'relay-stats', description: JSON.stringify(last).slice(0, 2000) })
    const held = await pageHeld(page).catch(() => null)
    await endSession(live!, held ?? h)
    await waitPoolEmpty(live!).catch(() => null)
  }
})
