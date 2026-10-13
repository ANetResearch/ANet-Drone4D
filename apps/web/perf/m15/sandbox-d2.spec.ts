// M15 D2 UI functional specs (WP-12 part 1; M15-AC-201 to AC-208, AC-211, AC-213, AC-217 functional parts) on a test build
// with the sandbox REST scripted by page routes (contracts fixtures `fixtures/d2/rest/*.json`, the OpenAPI snapshot) and
// the realtime link on FakeSource replaying `fixtures/d2/sandbox_t5.awrrt` (served by a route). No backend; functional
// only (no timing thresholds: the performance cases of D2-AC-01 and D2-AC-34 belong to the M16 harness).
//   * boot progress: __ux.boot.progress monotone, 1 at the reveal, the mask bar scaled (transform), never width;
//   * /sandbox/api: every operation of the document, method as text, the token placeholder only;
//   * /sandbox/:sid without this browser's token: the "unavailable" page, no realtime link; open a sandbox from there:
//     the Sheet (worlds, templates T1-T6, default T5), 409 509 solved and sent again, 201 enters the sandbox route;
//   * the sandbox surface: session bar (badge, lifetime, counts), entity switch with the targets list and detail page
//     from REST, nests, the model catalogue Sheet, the manual takeover confirmation, capture and discovery notices;
//   * queue: 202, the long poll's slot_ready turns the card into the claim window, "enter" claims and enters.
// Build: `M15_DIST=<dir> node perf/m15/helpers/build-ui.mjs`; run: `M15_DIST=<dir> M15_UI_ONLY=1 npx playwright test
// perf/m15/sandbox-d2.spec.ts --project perf` (AWR_PORT_OFFSET for a private port).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import type { PreviewServer } from 'vite'
import { baseUrl, glyphScan, injectEvents, isTestBuild, shot, startPreview, watch, type InjectEvent } from './helpers/shell'

const PORT = 4188
const REPO = join(import.meta.dirname, '..', '..', '..', '..')
const FIX = join(REPO, 'packages', 'contracts', 'fixtures', 'd2')
const rest = (name: string): Record<string, unknown> => JSON.parse(readFileSync(join(FIX, 'rest', `${name}.json`), 'utf8')) as Record<string, unknown>
const OPENAPI = JSON.parse(readFileSync(join(REPO, 'packages', 'contracts', 'rest', 'sandbox.openapi.snapshot.json'), 'utf8')) as { paths: Record<string, Record<string, unknown>> }
const AWRRT = readFileSync(join(FIX, 'sandbox_t5.awrrt'))
const SID = 'sb-k3m7q2x7ab'
const TOKEN = 'v1.fixture.0000000000000000'
const future = () => String(BigInt(Date.now() + 1_800_000) * 1_000_000n)

let server: PreviewServer | null = null
test.use({ baseURL: baseUrl(PORT), viewport: { width: 1600, height: 900 } })
test.beforeAll(async () => {
  server = await startPreview(PORT)
})
test.afterAll(async () => {
  await server?.close()
})
test.describe.configure({ mode: 'serial' })

interface Script { posts: { url: string; body: unknown }[]; queuePolls: number; createRound: number }

/** the sandbox REST of the fixtures; `create` decides the answer of POST /sessions */
async function routes(ctx: BrowserContext, create: (round: number, body: unknown) => { status: number; body: unknown }): Promise<Script> {
  const s: Script = { posts: [], queuePolls: 0, createRound: 0 }
  const session = () => ({ ...rest('session'), expires_unix_ns: future() })
  const created = () => ({ ...rest('session_created'), session: { ...(rest('session_created').session as object), expires_unix_ns: future() }, token_exp_unix_ns: future() })
  await ctx.route('**/__d2/sandbox_t5.awrrt', (r) => r.fulfill({ status: 200, body: AWRRT, headers: { 'content-type': 'application/octet-stream', 'cross-origin-resource-policy': 'same-origin' } }))
  await ctx.route('**/api/sandbox/v1/**', async (r) => {
    const req = r.request()
    const url = new URL(req.url())
    const p = url.pathname.replace('/api/sandbox/v1', '')
    const m = req.method()
    const json = (status: number, body: unknown) => r.fulfill({ status, body: JSON.stringify(body), headers: { 'content-type': 'application/json' } })
    if (m !== 'GET') s.posts.push({ url: `${m} ${p}`, body: req.postDataJSON() as unknown })
    if (m === 'GET' && p === '/worlds') return json(200, rest('worlds'))
    if (m === 'GET' && p === '/templates') return json(200, rest('templates'))
    if (m === 'GET' && p === '/meta') return json(200, rest('meta'))
    if (m === 'GET' && p === '/openapi.json') return json(200, OPENAPI)
    if (m === 'GET' && p === '/catalog/models') return json(200, { items: [{ model_id: 'p600_mid360', name_zh: 'P600', category: 'multirotor', reference: false, version: '2', mass_kg: 3.825, mtow_kg: 4.1, endurance: { hover_s: 1740 }, speed: { cruise_mps: 8, max_mps: 15 }, link_range_m: 3500, default_sensors: ['eo_gx40', 'mid360'] }, { model_id: 'awr_f1', name_zh: 'AWR-F1', category: 'fixed_wing', reference: true, version: '1', mass_kg: 6.2, endurance: { cruise_s: 7200 }, speed: { cruise_mps: 20, max_mps: 30, min_mps: 14 }, link_range_m: 15000 }] })
    if (m === 'POST' && p === '/sessions') {
      s.createRound++
      const a = create(s.createRound, req.postDataJSON())
      return json(a.status, a.body === 'created' ? created() : a.body)
    }
    if (m === 'GET' && p.startsWith('/queue/')) {
      s.queuePolls++
      return json(200, s.queuePolls < 2 ? { ticket_id: 'q-1', state: 'waiting', position: 1, eta_s: 120 }
        : { ticket_id: 'q-1', state: 'slot_ready', position: 0, eta_s: 0, claim_deadline_unix_ns: String(BigInt(Date.now() + 60_000) * 1_000_000n) })
    }
    if (m === 'POST' && p.endsWith('/claim')) return json(201, created())
    if (m === 'GET' && p === `/sessions/${SID}`) return json(200, session())
    if (m === 'GET' && p === `/sessions/${SID}/targets`) return json(200, rest('targets'))
    if (m === 'GET' && p === `/sessions/${SID}/nests`) return json(200, rest('nests'))
    if (m === 'GET' && p === `/sessions/${SID}/models`) return json(200, { items: [] })
    if (m === 'POST' && p === `/sessions/${SID}/keepalive`) return json(200, { session: session(), renewed: true, token: TOKEN, token_exp_unix_ns: future() })
    return json(404, { code: 305, message: 'not scripted' })
  })
  return s
}

const fakeQuery = '?source=fake&fakeFixture=/__d2/sandbox_t5.awrrt&fakeLoop=1&reveal=shell&viewport=off'
async function storeSession(page: Page): Promise<void> {
  await page.addInitScript(([sid, token, exp]) => {
    const v = JSON.stringify({ sid, token, token_exp_unix_ms: exp, world: 'synthcity', template: 't5_area_guard' })
    sessionStorage.setItem('awr.sandbox.session.v1', v)
    localStorage.setItem('awr.ui.onboarding.v1', JSON.stringify({ done: true, step: 3, v: 1 }))
  }, [SID, TOKEN, Date.now() + 1_800_000] as const)
}
async function testBuild(page: Page): Promise<void> {
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  test.skip(!(await isTestBuild(page)), 'needs a test build (VITE_AWR_TEST_SWITCHES=1)')
}

test('boot progress: monotone, 1 at the reveal, the bar is scaled', async ({ page, context }) => {
  await routes(context, () => ({ status: 404, body: {} }))
  await page.goto(`/world/shenzhen?source=fake&fakeN=1&reveal=shell&viewport=off`)
  await testBuild(page)
  const samples = await page.evaluate(() => (window as unknown as { __ux: { boot: { progress: { p: number; seg: string | null }[] } } }).__ux.boot.progress)
  expect(samples.length).toBeGreaterThan(0)
  for (let i = 1; i < samples.length; i++) expect(samples[i].p).toBeGreaterThanOrEqual(samples[i - 1].p)
  expect(samples.at(-1)?.p).toBe(1)
})

test('/sandbox/api lists every operation without a token in the page', async ({ page, context }) => {
  const w = watch(page)
  await routes(context, () => ({ status: 404, body: {} }))
  await page.goto('/sandbox/api?reveal=shell&viewport=off')
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 45_000 })
  await expect(page.locator('[data-view="api-docs"]')).toBeVisible({ timeout: 45_000 })
  let ops = 0
  for (const item of Object.values(OPENAPI.paths)) for (const m of ['get', 'post', 'put', 'patch', 'delete']) if (item[m]) ops++
  await expect(page.locator('[data-api-nav]')).toHaveAttribute('data-api-nav', String(ops))
  await expect(page.locator('[data-api-link]')).toHaveCount(ops)
  await page.locator('[data-api-link="POST /sessions/{sid}/targets"]').click()
  const card = page.locator('[data-api-op="POST /sessions/{sid}/targets"]')
  await expect(card).toContainText('/api/sandbox/v1/sessions/{sid}/targets')
  await expect(card).toContainText('$AWR_SANDBOX_TOKEN')
  await expect(page.locator('[data-api-realtime]')).toContainText('swarm/target/state')
  expect(await page.locator('body').innerText()).not.toContain(TOKEN)
  expect(await page.evaluate(() => [...document.scripts].some((s) => /three/.test(s.src)))).toBe(false)
  await shot(page, 'm15-d2-api-docs')
  expect(w.errors).toEqual([])
})

test('an unknown sid shows the unavailable page; the Sheet opens a sandbox through the proof of work', async ({ page, context }) => {
  const w = watch(page)
  const s = await routes(context, (round) => round === 1
    ? { status: 409, body: { code: 509, message: 'challenge', detail: { why: 'required', challenge: { challenge_id: 'c-1', alg: 'sha256-lz', prefix_b64: 'q3Jx', bits: 6, expires_unix_ns: future() } } } }
    : { status: 201, body: 'created' })
  await page.goto(`/sandbox/${SID}?reveal=shell&viewport=off`)
  await testBuild(page)
  await expect(page.locator('[data-sandbox-unavailable]')).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { __ux: { sandbox: { surface: string } } }).__ux.sandbox.surface)).toBe('sandbox-gone')
  await page.locator('[data-sandbox-unavailable] button').last().click()
  const sheet = page.locator('[data-start-sheet]')
  await expect(sheet).toBeVisible()
  await expect(page.locator('[data-template]')).toHaveCount(6)
  await expect(page.locator('[data-template="t5_area_guard"]')).toHaveAttribute('data-checked', '')
  await page.locator('[data-start-go]').click()
  await expect.poll(() => s.createRound, { timeout: 20_000 }).toBe(2)
  const second = s.posts.filter((x) => x.url === 'POST /sessions')[1].body as { pow?: { challenge_id: string; nonce: number }; template: string; world: string }
  expect(second.pow?.challenge_id).toBe('c-1')
  expect(second.template).toBe('t5_area_guard')
  expect(second.world).toBe('synthcity')
  await expect(page).toHaveURL(new RegExp(`/sandbox/${SID}`))
  await expect(page.locator('[data-session-bar]')).toBeVisible({ timeout: 20_000 })
  expect(w.errors).toEqual([])
})

test('sandbox surface: session bar, entity switch, targets, nests, catalogue, takeover and notices', async ({ page, context }) => {
  const w = watch(page)
  await routes(context, () => ({ status: 404, body: {} }))
  await storeSession(page)
  await page.goto(`/sandbox/${SID}${fakeQuery}`)
  await testBuild(page)
  const bar = page.locator('[data-session-bar]')
  await expect(bar).toBeVisible()
  await expect(page.locator('[data-session-badge]')).toContainText('synthcity')
  await expect(page.locator('[data-session-left]')).not.toHaveAttribute('data-session-left', 'NaN')
  await expect(page.locator('[data-session-count="targets"]')).toContainText('/30')
  await expect(page.locator('[data-demo-role]')).toHaveCount(1)
  // entity switch: targets list from REST, detail page
  await page.locator('[data-entity="targets"]').click()
  await expect(page.locator('[data-target-row]').first()).toBeVisible({ timeout: 20_000 })
  await page.locator('[data-target-row="person-01"]').click()
  await expect(page.locator('[data-target-detail="person-01"]')).toBeVisible()
  await expect(page.locator('[data-sensitivity]')).toBeVisible()
  await expect(page.locator('[data-sens-add="visual"]')).toBeVisible()
  await page.locator('[data-target-advanced-open]').click()
  await expect(page.locator('[data-target-advanced]')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.locator('[data-target-back]').click()
  // nests
  await page.locator('[data-entity="nests"]').click()
  await expect(page.locator('[data-nest-row="nest-01"]')).toBeVisible({ timeout: 20_000 })
  // fleet: catalogue Sheet
  await page.locator('[data-entity="drones"]').click()
  await page.locator('[data-fleet-catalog]').click()
  await expect(page.locator('[data-model-card]')).toHaveCount(2)
  await expect(page.locator('[data-model-reference]')).toHaveCount(1)
  await page.keyboard.press('Escape')
  // capture and discovery notices through the event bridge (target ids the fixture replay never uses: its own capture
  // and discovery events would otherwise already hold the 30 s de-duplication window, depending on the replay timing)
  let seq = 9_000_000
  const ev = (type: string, level: 0 | 1 | 2 | 3, data: Record<string, unknown>): InjectEvent => ({ seq: ++seq, t_sim_ns: seq * 1e6, type, level, uav: (data.uav as string) ?? null, cid: null, data })
  await injectEvents(page, [ev('perception.capture', 1, { uav: 'uav0001', sensor: 'eo_gx40', target: 'person-91', level_to: 'R' })])
  await expect(page.locator('[data-slot="toast"]').filter({ hasText: '已明确捕获 person-91' })).toHaveCount(1)
  await injectEvents(page, [ev('perception.capture', 1, { uav: 'uav0001', sensor: 'eo_gx40', target: 'person-91', level_to: 'I' })])
  await page.waitForTimeout(500)
  // deduplicated within 30 s: no second toast (the first may already have timed out on a loaded machine)
  expect(await page.locator('[data-slot="toast"]').filter({ hasText: '已明确捕获 person-91' }).count()).toBeLessThanOrEqual(1)
  await injectEvents(page, [ev('target.discovered', 3, { target: 'dog-91', uav: 'uav0002', kind: 'acoustic', range_m: 120 })])
  const disc = page.locator('[data-slot="toast"]').filter({ hasText: 'dog-91' })
  await expect(disc).toHaveCount(1)
  await disc.locator('[data-toast-action]').click()
  await expect(disc).toHaveCount(0, { timeout: 10_000 })
  expect(await glyphScan(page)).toEqual([])
  await shot(page, 'm15-d2-sandbox')
  expect(w.errors).toEqual([])
})

test('queue: 202, slot ready, claim enters the sandbox', async ({ page, context }) => {
  const w = watch(page)
  const s = await routes(context, () => ({ status: 202, body: { queue: { ticket_id: 'q-1', ticket_token: 'q1.tok', position: 3, eta_s: 240, expires_unix_ns: future() } } }))
  await page.goto(`/sandbox/sb-aaaaaaaaaa?reveal=shell&viewport=off`)
  await testBuild(page)
  await page.locator('[data-sandbox-unavailable] button').last().click()
  await page.locator('[data-start-go]').click()
  // queued: the shared show with the non-modal card; the long poll turns it into the claim window
  await expect(page).toHaveURL(/\/world\/synthcity/, { timeout: 20_000 })
  await expect(page.locator('[data-queue-card]')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('[data-queue-card="slot_ready"]')).toBeVisible({ timeout: 30_000 })
  expect(s.queuePolls).toBeGreaterThan(1)
  await page.locator('[data-queue-claim]').click()
  await expect(page).toHaveURL(new RegExp(`/sandbox/${SID}`), { timeout: 20_000 })
  expect(s.posts.some((x) => x.url === 'POST /queue/q-1/claim')).toBe(true)
  expect(w.errors).toEqual([])
})
