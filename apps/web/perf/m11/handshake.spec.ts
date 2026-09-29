// M11-AC-008 / AC-011 / AC-033 (browser and wire parts) against the real gateway: the WebSocket upgrade selects awr.rt.v1
// without echoing the bearer and without permessage-deflate even when the client offers it; a missing subprotocol is
// HTTP 400 with AWR-Supported-Protocols; a foreign Origin is 403; the page is crossOriginIsolated and the rt.worker link
// reaches LIVE through the real gateway (serverInfo seen by the client).
// Opt-in (the backend is a supervisor with sim-core and api, heavy for the parallel phase): M11_BACKEND=1 starts one on
// free ports (perf/skeleton.server.ts startBackend), SKELETON_API=host:port uses a running `make run`. The api serves
// apps/web/dist; the LIVE check needs a test build (window.__vp) and is skipped otherwise.
import { randomBytes } from 'node:crypto'
import { request } from 'node:http'
import { expect, test } from '@playwright/test'
import { startBackend, type Backend } from '../skeleton.server'

const ENABLED = process.env.M11_BACKEND === '1' || !!process.env.SKELETON_API
let backend: Backend | null = null

test.describe.configure({ mode: 'serial' })
test.skip(!ENABLED, 'set M11_BACKEND=1 (or SKELETON_API=host:port) to run against the real gateway')
test.beforeAll(async () => {
  backend = await startBackend()
})
test.afterAll(async () => {
  await backend?.close()
})

interface Upgrade { status: number; headers: Record<string, string | string[] | undefined> }
/** raw upgrade request (the browser hides handshake statuses and headers) */
function upgrade(url: string, headers: Record<string, string>): Promise<Upgrade> {
  const u = new URL('/api/rt', url)
  return new Promise((ok, fail) => {
    const req = request({ host: u.hostname, port: u.port, path: u.pathname, method: 'GET', headers: {
      Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': randomBytes(16).toString('base64'), ...headers } })
    req.on('upgrade', (res, socket) => {
      socket.destroy()
      ok({ status: res.statusCode ?? 0, headers: res.headers })
    })
    req.on('response', (res) => {
      res.resume()
      ok({ status: res.statusCode ?? 0, headers: res.headers })
    })
    req.on('error', fail)
    req.end()
  })
}

async function viewerToken(url: string): Promise<string> {
  const r = await fetch(`${url}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role: 'viewer', client: 'm11-handshake', principal_hint: 'MTONEHANDSHAKESPECVIEWER' }) })
  expect(r.status).toBe(200)
  return ((await r.json()) as { token: string }).token
}

test('upgrade: awr.rt.v1 selected, bearer not echoed, no permessage-deflate', async () => {
  const url = backend!.url
  const token = await viewerToken(url)
  const r = await upgrade(url, { 'Sec-WebSocket-Protocol': `awr.rt.v1, bearer.${token}`, 'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
    Origin: url })
  expect(r.status).toBe(101)
  expect(r.headers['sec-websocket-protocol']).toBe('awr.rt.v1')
  expect(r.headers['sec-websocket-extensions']).toBeUndefined()
})

test('upgrade without awr.rt.v1 is 400 with AWR-Supported-Protocols; a foreign Origin is 403', async () => {
  const url = backend!.url
  const noProto = await upgrade(url, { 'Sec-WebSocket-Protocol': 'chat', Origin: url })
  expect(noProto.status).toBe(400)
  expect(String(noProto.headers['awr-supported-protocols'])).toContain('awr.rt.v1')
  const foreign = await upgrade(url, { 'Sec-WebSocket-Protocol': 'awr.rt.v1', Origin: 'http://evil.example' })
  expect(foreign.status).toBe(403)
})

test('the page is crossOriginIsolated and the realtime link reaches LIVE through the gateway', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`${backend!.url}/world/shenzhen`)
  await page.waitForFunction(() => document.readyState === 'complete')
  expect(await page.evaluate(() => crossOriginIsolated)).toBe(true)
  const hooks = await page.waitForFunction(() => '__vp' in window, null, { timeout: 30_000 }).then(() => true, () => false)
  test.skip(!hooks, 'LIVE check needs a test build (VITE_AWR_TEST_SWITCHES=1) served by the api')
  await page.waitForFunction(() => (window as unknown as { __vp: { conn(): string } }).__vp.conn() === 'LIVE', null, { timeout: 60_000 })
  const s = await page.evaluate(() => (window as unknown as { __vp: { session(): { worldId: string } | null } }).__vp.session())
  expect(s?.worldId).toBe('shenzhen')
})
