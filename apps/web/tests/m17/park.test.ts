// Sandbox session store, D2.1 power saving (M17-FR-079, AC-032; AWR-04 §15.7.3, §15.7.4; D2-AC-45 unit part): park (SB-69)
// polls to PARKED and keeps the token past its expiry for resume; resume (SB-70) 201 adopts the fresh token and waits for
// READY, 202 queues with a resume ticket that is claimed automatically while visible, 410 ends, 513 (still PARKING) retries;
// the PowerController interface; WS 4430 / 515 -> parked; the park beacon; queue restore after a reload; the D2.1
// storage format (AWR-14 §20.4.7) and the WP-12 fields (anotherTab, verifyHint, retryUntilMs, phaseSinceMs, switching).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getEndpoint, setEndpoint } from '@/net/endpoint'
import { setSandboxFetch, setSandboxIdentityProvider } from '@/net/sandbox/client'
import {
  configureSandboxSession, createSandbox, handleSandboxEvent, joinByPairCode, keepaliveSandbox, markSandboxParked, onPowerState,
  parkSandbox, parkSandboxBeacon, QUEUE_KEY, restoreQueue, resumeParked, resumeSandbox, sandboxPairCode, sandboxSessionStore,
  STORAGE_KEY, stopAutoKeepalive, switchSandboxWorld,
} from '@/stores/sandboxSession'

const SID = 'sb-k3m7q2x7ab'
const T0 = 1_791_000_000_000

class MemStorage implements Storage {
  m = new Map<string, string>()
  get length() { return this.m.size }
  clear() { this.m.clear() }
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null }
  key(i: number) { return [...this.m.keys()][i] ?? null }
  removeItem(k: string) { this.m.delete(k) }
  setItem(k: string, v: string) { this.m.set(k, v) }
}

function ns(ms: number): string { return String(BigInt(ms) * 1_000_000n) }

function view(state: string, extra: Record<string, unknown> = {}) {
  return { sid: SID, state, world_id: 'synthcity', template_id: 't5_area_guard', created_unix_ns: ns(T0), expires_unix_ns: ns(T0 + 1_800_000),
    expires_in_s: 1800, renewable: true, renew_block_reason: null,
    clock: { state: 'STOPPED', rate_requested: 1, rate_granted: 1, rate_cap: 5, limited_by: null, tick_hz: 100 },
    limits: { vehicles_max: 12, targets_max: 30, nests_max: 4, tasks_active_max: 6, rest_rps: 10, call_rps: 20, clock_rps: 2, ws_max: 4, setpoint_hz_max: 50 },
    endpoints: { rest: `/api/sandbox/v1/sessions/${SID}`, rt: `/api/sandbox/v1/sessions/${SID}/rt` }, ...extra }
}
const PARKED_FIELDS = { park_reason: 'idle', resume_mode: 'checkpoint', parked_unix_ns: ns(T0), retention_unix_ns: ns(T0 + 3_600_000),
  remaining_s: 1700, t_sim_ns: '42000000000', tier: 'public' }

type Route = (url: string, init: RequestInit) => { status: number; body?: unknown; headers?: Record<string, string> } | undefined
let routes: Route[] = []
let calls: { url: string; method: string; body: unknown; auth: string | null; ident: string | null }[] = []

function installFetch() {
  setSandboxFetch(async (url: string, init?: RequestInit) => {
    const i = init ?? {}
    const h = new Headers(i.headers)
    calls.push({ url, method: i.method ?? 'GET', body: typeof i.body === 'string' ? JSON.parse(i.body) : undefined,
      auth: h.get('authorization'), ident: h.get('x-awr-identity') })
    for (const r of routes) {
      const x = r(url, i)
      if (x) return new Response(x.status === 204 || x.body === undefined ? null : JSON.stringify(x.body),
        { status: x.status, headers: { 'content-type': 'application/json', ...(x.headers ?? {}) } })
    }
    return new Response(JSON.stringify({ code: 305 }), { status: 404 })
  })
}

let session: MemStorage
let local: MemStorage
let now = T0
let visible = true

function setup() {
  session = new MemStorage()
  local = new MemStorage()
  configureSandboxSession({ session, local, channel: null, now: () => now, viewport: () => ({ w: 1180, h: 820 }),
    visible: () => visible, sleep: async () => {}, tabId: 't1', solve: async (ch) => ({ challenge_id: ch.challenge_id, nonce: 7 }) })
}

beforeEach(() => {
  routes = []
  calls = []
  now = T0
  visible = true
  installFetch()
  setup()
})

afterEach(() => {
  stopAutoKeepalive()
  setEndpoint({ kind: 'shared' })
  setSandboxFetch(null)
  setSandboxIdentityProvider(null)
})

async function active(): Promise<void> {
  local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', token_exp_unix_ms: T0 + 1_800_000 }))
  routes.push((url, init) => (url === `/api/sandbox/v1/sessions/${SID}` && (init.method ?? 'GET') === 'GET'
    ? { status: 200, body: view('ACTIVE') } : undefined))
  expect(await resumeSandbox()).toBe(true)
  routes = []
}

async function flush(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

describe('park', () => {
  it('202 -> polls to PARKED; the token outlives its expiry for resume', async () => {
    await active()
    let gets = 0
    routes.push((url, init) => (url.endsWith('/park') && init.method === 'POST' ? { status: 202, body: { state: 'PARKING' } } : undefined))
    routes.push((url) => (url === `/api/sandbox/v1/sessions/${SID}` ? { status: 200, body: view(++gets < 2 ? 'PARKING' : 'PARKED', PARKED_FIELDS) } : undefined))
    const phases: string[] = []
    const off = sandboxSessionStore.subscribe((s) => { if (phases.at(-1) !== s.phase) phases.push(s.phase) })
    expect(await parkSandbox('idle')).toBe(true)
    off()
    expect(phases).toEqual(['parking', 'parked'])
    const s = sandboxSessionStore.getState()
    expect(s.parked).toMatchObject({ reason: 'idle', resumeMode: 'checkpoint', remainingS: 1700, tSimNs: '42000000000' })
    expect(calls.find((c) => c.url.endsWith('/park'))?.body).toEqual({ reason: 'idle' })
    const stored = JSON.parse(session.getItem(STORAGE_KEY)!)
    expect(stored.parked_until_ms).toBe(T0 + 3_600_000)
    expect(stored.token_exp_unix_ms).toBe(T0 + 1_800_000)
    // a reload after the token expired: GET is 401 but the parked session is kept for resume
    now = T0 + 2_000_000
    setup()
    session.setItem(STORAGE_KEY, JSON.stringify(stored))
    routes = [(url) => (url === `/api/sandbox/v1/sessions/${SID}` ? { status: 401, body: { code: 302 } } : undefined)]
    expect(await resumeSandbox()).toBe(true)
    expect(sandboxSessionStore.getState().phase).toBe('parked')
    expect(getEndpoint().kind).toBe('sandbox')
  })

  it('park beacon (pagehide) sends the token in the body', async () => {
    await active()
    const beacons: { url: string; body: Blob }[] = []
    vi.stubGlobal('navigator', { sendBeacon: (url: string, body: Blob) => { beacons.push({ url, body }); return true } })
    expect(parkSandboxBeacon('hidden')).toBe(true)
    vi.unstubAllGlobals()
    expect(beacons[0].url).toBe(`/api/sandbox/v1/sessions/${SID}/park-beacon`)
    expect(JSON.parse(await beacons[0].body.text())).toEqual({ token: 'v1.tok.1', reason: 'hidden' })
    expect(sandboxSessionStore.getState().phase).toBe('parking')
  })

  it('WS 4430 / 515 / sandbox.parked -> parked (refreshes the view)', async () => {
    await active()
    routes.push((url) => (url.endsWith('/keepalive') ? { status: 409, body: { code: 515, detail: { state: 'PARKED' } } } : undefined))
    routes.push((url) => (url === `/api/sandbox/v1/sessions/${SID}` ? { status: 200, body: view('PARKED', PARKED_FIELDS) } : undefined))
    expect(await keepaliveSandbox()).toBe(false)
    await flush()
    expect(sandboxSessionStore.getState().phase).toBe('parked')
    expect(sandboxSessionStore.getState().parked?.resumeMode).toBe('checkpoint')
    handleSandboxEvent('sandbox.parked', { sid: SID, reason: 'idle', resume_mode: 'checkpoint' })
    markSandboxParked()
    expect(sandboxSessionStore.getState().phase).toBe('parked')
  })
})

describe('resume', () => {
  async function parked(): Promise<void> {
    await active()
    handleSandboxEvent('sandbox.state', { sid: SID, from: 'ACTIVE', to: 'PARKED' })
    await flush()
    routes = []
  }

  it('201 adopts the fresh token and waits for READY', async () => {
    await parked()
    routes.push((url, init) => (url.endsWith('/resume') && init.method === 'POST'
      ? { status: 201, body: { session: view('SPAWNING'), token: 'v1.tok.2', token_exp_unix_ns: ns(T0 + 1_800_000) } } : undefined))
    routes.push((url) => (url.startsWith(`/api/sandbox/v1/sessions/${SID}?`) ? { status: 200, body: view('READY') } : undefined))
    expect(await resumeParked()).toBe('resumed')
    const s = sandboxSessionStore.getState()
    expect(s.phase).toBe('ready')
    expect(s.token).toBe('v1.tok.2')
    expect(calls.find((c) => c.url.endsWith('/resume'))?.auth).toBe('Bearer v1.tok.1')
    expect(JSON.parse(session.getItem(STORAGE_KEY)!).token).toBe('v1.tok.2')
  })

  it('202 resume ticket -> slot_ready -> claimed automatically while visible', async () => {
    await parked()
    routes.push((url, init) => (url.endsWith('/resume') && init.method === 'POST' ? { status: 202, body: { queue: {
      ticket_id: 'q-abcdefghij', ticket_token: 'q1.res', position: 2, eta_s: 300, kind: 'resume', sid: SID } } } : undefined))
    let polls = 0
    routes.push((url) => (url.startsWith('/api/sandbox/v1/queue/q-abcdefghij?') ? { status: 200, body: ++polls < 2
      ? { ticket_id: 'q-abcdefghij', state: 'waiting', position: 1, eta_s: 60 }
      : { ticket_id: 'q-abcdefghij', state: 'slot_ready', position: 0, eta_s: 0, claim_deadline_unix_ns: ns(T0 + 60_000) } } : undefined))
    routes.push((url) => (url.endsWith('/queue/q-abcdefghij/claim')
      ? { status: 201, body: { session: view('SPAWNING'), token: 'v1.tok.3', token_exp_unix_ns: ns(T0 + 1_800_000) } } : undefined))
    routes.push((url) => (url.startsWith(`/api/sandbox/v1/sessions/${SID}?`) ? { status: 200, body: view('READY') } : undefined))
    expect(await resumeParked()).toBe('queued')
    expect(sandboxSessionStore.getState().queue).toMatchObject({ kind: 'resume', ticketToken: 'q1.res' })
    expect(JSON.parse(session.getItem(QUEUE_KEY)!)).toMatchObject({ ticket_id: 'q-abcdefghij', kind: 'resume' })
    for (let i = 0; i < 50 && sandboxSessionStore.getState().phase !== 'ready'; i++) await new Promise((r) => setTimeout(r, 1))
    expect(sandboxSessionStore.getState().phase).toBe('ready')
    expect(sandboxSessionStore.getState().token).toBe('v1.tok.3')
    expect(session.getItem(QUEUE_KEY)).toBeNull()
  })

  it('513 while still PARKING retries; 410 ends with parked_expired', async () => {
    await parked()
    let n = 0
    routes.push((url) => (url.endsWith('/resume') ? (++n < 3 ? { status: 409, body: { code: 513, detail: { state: 'PARKING' } } }
      : { status: 410, body: { code: 506, detail: { end_reason: 'parked_expired', snapshot_available: true } } }) : undefined))
    expect(await resumeParked()).toBe('ended')
    expect(n).toBe(3)
    const s = sandboxSessionStore.getState()
    expect(s.phase).toBe('ended')
    expect(s.endReason).toBe('parked_expired')
    expect(getEndpoint().kind).toBe('shared')
  })

  it('PowerController interface: SAVING parks, RESUMING resumes', async () => {
    await active()
    routes.push((url) => (url.endsWith('/park') ? { status: 200, body: { state: 'PARKED', session: view('PARKED', PARKED_FIELDS) } } : undefined))
    expect(await onPowerState('saving', 'hidden')).toBe('parked')
    expect(calls.find((c) => c.url.endsWith('/park'))?.body).toEqual({ reason: 'hidden' })
    expect(await onPowerState('saving')).toBe('none')
    routes.unshift((url) => (url.endsWith('/resume') ? { status: 200, body: { session: view('ACTIVE'), token: null } } : undefined))
    expect(await onPowerState('resuming')).toBe('resumed')
    expect(sandboxSessionStore.getState().phase).toBe('active')
  })
})

describe('storage and UI fields', () => {
  it('queue ticket survives a reload', async () => {
    session.setItem(QUEUE_KEY, JSON.stringify({ ticket_id: 'q-abcdefghij', ticket_token: 'q1.xyz', expires_unix_ms: T0 + 60_000, kind: 'new',
      world: 'shenzhen', template: null }))
    let polls = 0
    routes.push((url) => (url.startsWith('/api/sandbox/v1/queue/q-abcdefghij?') ? { status: 200, body: ++polls < 2
      ? { ticket_id: 'q-abcdefghij', state: 'waiting', position: 3, eta_s: 400 }
      : { ticket_id: 'q-abcdefghij', state: 'slot_ready', position: 0, eta_s: 0, claim_deadline_unix_ns: ns(T0 + 60_000) } } : undefined))
    const seen: (number | undefined)[] = []
    const off = sandboxSessionStore.subscribe((s) => seen.push(s.queue?.position))
    expect(restoreQueue()).toBe(true)
    expect(sandboxSessionStore.getState().queue).toMatchObject({ world: 'shenzhen', kind: 'new', ticketToken: 'q1.xyz' })
    for (let i = 0; i < 20 && sandboxSessionStore.getState().phase !== 'slot_ready'; i++) await new Promise((r) => setTimeout(r, 1))
    off()
    expect(seen).toContain(3)
    expect(sandboxSessionStore.getState().phase).toBe('slot_ready')    // a new visitor's ticket waits for the click
    expect(calls[0].auth).toBe('Bearer q1.xyz')
  })

  it('anotherTab, retryUntilMs, verifyHint, phaseSinceMs, switching, identity header', async () => {
    setSandboxIdentityProvider(() => 'v1.ident.x')
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST'
      ? { status: 200, body: { session: view('ACTIVE'), token: null, idempotent: true } } : undefined))
    await createSandbox('synthcity')
    expect(sandboxSessionStore.getState().anotherTab).toBe(true)
    expect(calls[0].ident).toBe('v1.ident.x')
    routes = [(url) => (url === '/api/sandbox/v1/sessions' ? { status: 429, body: { code: 504, detail: { why: 'create_rate' } },
      headers: { 'retry-after': '120' } } : undefined)]
    await createSandbox('synthcity')
    expect(sandboxSessionStore.getState().retryUntilMs).toBe(T0 + 120_000)
    let solveResolve: (v: { challenge_id: string; nonce: number }) => void = () => {}
    configureSandboxSession({ session, local, channel: null, now: () => now, viewport: () => ({ w: 1180, h: 820 }), visible: () => true,
      sleep: async () => {}, tabId: 't1', solve: () => new Promise((r) => { solveResolve = r }) })
    vi.useFakeTimers()
    let posts = 0
    routes = [(url) => (url === '/api/sandbox/v1/sessions' ? (++posts < 2 ? { status: 409, body: { code: 509, detail: { challenge: {
      challenge_id: 'c-abcdefghij', alg: 'sha256-lz', prefix_b64: 'AAAA', bits: 4, expires_unix_ns: '0' } } } }
      : { status: 201, body: { session: view('SPAWNING'), token: 'v1.tok.9', token_exp_unix_ns: ns(T0 + 1_800_000) } }) : undefined),
    (url) => (url.startsWith(`/api/sandbox/v1/sessions/${SID}?`) ? { status: 200, body: view('READY') } : undefined)]
    const p = createSandbox('synthcity')
    await vi.advanceTimersByTimeAsync(100)
    expect(sandboxSessionStore.getState().phase).toBe('challenge')
    expect(sandboxSessionStore.getState().verifyHint).toBeFalsy()
    await vi.advanceTimersByTimeAsync(300)
    expect(sandboxSessionStore.getState().verifyHint).toBe(true)
    solveResolve({ challenge_id: 'c-abcdefghij', nonce: 1 })
    await vi.advanceTimersByTimeAsync(10)
    vi.useRealTimers()
    await p
    expect(sandboxSessionStore.getState().phase).toBe('ready')
    expect(sandboxSessionStore.getState().phaseSinceMs).toBe(T0)
    stopAutoKeepalive()
    setup()
    await active()
    routes.push((url) => (url.endsWith('/world') ? { status: 202, body: view('SPAWNING') } : undefined))
    routes.push((url) => (url.startsWith(`/api/sandbox/v1/sessions/${SID}?`) ? { status: 200, body: view('READY') } : undefined))
    const seen: (string | null)[] = []
    const off = sandboxSessionStore.subscribe((s) => seen.push(s.switching ? s.switching.world : null))
    await switchSandboxWorld('shenzhen')
    off()
    expect(seen).toContain('shenzhen')
    expect(sandboxSessionStore.getState().switching).toBeNull()
  })
})

describe('commander pairing (P1)', () => {
  it('holder issues a code; another device joins the same sid with its own token', async () => {
    await active()
    routes.push((url, init) => (url.endsWith('/pair-code') && init.method === 'POST'
      ? { status: 201, body: { code: 'ABCD2345', expires_unix_ns: ns(T0 + 120_000) } } : undefined))
    expect(await sandboxPairCode()).toEqual({ code: 'ABCD2345', expiresUnixNs: ns(T0 + 120_000) })
    expect(calls.at(-1)?.auth).toBe('Bearer v1.tok.1')
    setup()
    routes = [(url, init) => (url.endsWith('/pair') && init.method === 'POST'
      ? { status: 201, body: { session: view('ACTIVE'), token: 'v1.paired', token_exp_unix_ns: ns(T0 + 1_800_000) } } : undefined)]
    expect(await joinByPairCode(SID, 'abcd2345')).toBe(true)
    expect(calls.at(-1)?.body).toEqual({ code: 'ABCD2345' })
    expect(calls.at(-1)?.auth).toBeNull()
    const s = sandboxSessionStore.getState()
    expect(s.phase).toBe('active')
    expect(s.token).toBe('v1.paired')
    expect(getEndpoint().kind).toBe('sandbox')
  })
})
