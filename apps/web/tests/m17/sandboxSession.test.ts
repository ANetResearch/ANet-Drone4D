// Sandbox session store (M17-FR-059, AC-022; M17 PRD §7.9): phase transitions for create (201), proof of work (409 509),
// queue (202 -> slot_ready -> claim), spawning -> ready, idempotent create resumed from storage, refresh resume and the
// second tab, the sandbox endpoint (REST prefix and token), keepalive token sharing over BroadcastChannel with only one
// renewing tab, sandbox.expiring -> snapshot in localStorage, 4410/410 -> ended, and the viewport guard (D2.1: < 360 CSS px).
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { apiBase, getEndpoint, sandboxTokenFor, setEndpoint } from '@/net/endpoint'
import { setSandboxFetch } from '@/net/sandbox/client'
import {
  autoKeepaliveTick, claimSlot, configureSandboxSession, createSandbox, endSandbox, handleSandboxEvent, KA_LOCK_KEY,
  keepaliveSandbox, leaveQueue, markSandboxGone, noteUserInput, resumeSandbox, sandboxRtEndpoint, sandboxSessionStore,
  SNAPSHOT_KEY, STORAGE_KEY, stopAutoKeepalive,
} from '@/stores/sandboxSession'

const SID = 'sb-k3m7q2x7ab'
const T0 = 1_791_000_000_000 // ms

class MemStorage implements Storage {
  m = new Map<string, string>()
  get length() { return this.m.size }
  clear() { this.m.clear() }
  getItem(k: string) { return this.m.has(k) ? this.m.get(k)! : null }
  key(i: number) { return [...this.m.keys()][i] ?? null }
  removeItem(k: string) { this.m.delete(k) }
  setItem(k: string, v: string) { this.m.set(k, v) }
}

class Chan {
  static all: Chan[] = []
  onmessage: ((ev: MessageEvent) => void) | null = null
  sent: unknown[] = []
  constructor() { Chan.all.push(this) }
  postMessage(m: unknown) {
    this.sent.push(m)
    for (const c of Chan.all) if (c !== this) c.onmessage?.({ data: m } as MessageEvent)
  }
  close() {}
}

function ns(ms: number): string { return String(BigInt(ms) * 1_000_000n) }

function view(state: string, expMs = T0 + 1_800_000, extra: Record<string, unknown> = {}) {
  return { sid: SID, state, world_id: 'synthcity', template_id: 't5_area_guard', created_unix_ns: ns(T0), expires_unix_ns: ns(expMs),
    expires_in_s: 1800, renewable: true, renew_block_reason: null,
    clock: { state: 'STOPPED', rate_requested: 1, rate_granted: 1, rate_cap: 5, limited_by: null, tick_hz: 100 },
    limits: { vehicles_max: 12, targets_max: 30, nests_max: 4, tasks_active_max: 6, rest_rps: 10, call_rps: 20, clock_rps: 2, ws_max: 4, setpoint_hz_max: 50 },
    endpoints: { rest: `/api/sandbox/v1/sessions/${SID}`, rt: `/api/sandbox/v1/sessions/${SID}/rt` }, ...extra }
}

type Route = (url: string, init: RequestInit) => { status: number; body?: unknown; headers?: Record<string, string> } | undefined
let routes: Route[] = []
let calls: { url: string; method: string; body: unknown; auth: string | null }[] = []

function json(status: number, body?: unknown, headers: Record<string, string> = {}) {
  return new Response(status === 204 || body === undefined ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } })
}

function installFetch() {
  setSandboxFetch(async (url: string, init?: RequestInit) => {
    const i = init ?? {}
    const h = new Headers(i.headers)
    calls.push({ url, method: i.method ?? 'GET', body: typeof i.body === 'string' ? JSON.parse(i.body) : undefined, auth: h.get('authorization') })
    for (const r of routes) {
      const x = r(url, i)
      if (x) return json(x.status, x.body, x.headers)
    }
    return json(404, { code: 305, message: 'no route' })
  })
}

let session: MemStorage
let local: MemStorage
let now = T0
let vp = { w: 1920, h: 1080 }

function setup(tab = 't1', shared?: { local: MemStorage }) {
  session = new MemStorage()
  local = shared?.local ?? new MemStorage()
  configureSandboxSession({ session, local, channel: new Chan() as unknown as BroadcastChannel, now: () => now, viewport: () => vp,
    visible: () => true, sleep: async () => {}, tabId: tab,
    solve: async (ch) => ({ challenge_id: ch.challenge_id, nonce: 42 }) })
}

beforeEach(() => {
  routes = []
  calls = []
  now = T0
  vp = { w: 1920, h: 1080 }
  Chan.all = []
  installFetch()
  setup()
})

afterEach(() => {
  stopAutoKeepalive()
  setEndpoint({ kind: 'shared' })
  setSandboxFetch(null)
})

const created = { session: view('SPAWNING'), token: 'v1.tok.1', token_exp_unix_ns: ns(T0 + 1_800_000) }

function sessionRoutes(states: string[]) {
  let i = 0
  routes.push((url, init) => (url.startsWith(`/api/sandbox/v1/sessions/${SID}`) && (init.method ?? 'GET') === 'GET' && !url.includes('/snapshot')
    ? { status: 200, body: view(states[Math.min(i++, states.length - 1)]) } : undefined))
}

describe('create', () => {
  it('201 -> spawning -> ready; endpoint and storage', async () => {
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST' ? { status: 201, body: created } : undefined))
    sessionRoutes(['SPAWNING', 'READY'])
    const seen: string[] = []
    const off = sandboxSessionStore.subscribe((s) => { if (seen.at(-1) !== s.phase) seen.push(s.phase) })
    await createSandbox('synthcity', 't5_area_guard')
    off()
    expect(seen).toEqual(['creating', 'spawning', 'ready'])
    const st = sandboxSessionStore.getState()
    expect(st.sid).toBe(SID)
    expect(st.token).toBe('v1.tok.1')
    const body = calls[0].body as Record<string, unknown>
    expect(body.world).toBe('synthcity')
    expect(body.template).toBe('t5_area_guard')
    expect(String(body.principal_hint)).toMatch(/^[0-9a-f]{32}$/)
    expect(getEndpoint().kind).toBe('sandbox')
    expect(apiBase()).toBe(`/api/sandbox/v1/sessions/${SID}`)
    expect(sandboxTokenFor(`/api/sandbox/v1/sessions/${SID}/vehicles`)).toBe('v1.tok.1')
    expect(sandboxRtEndpoint()).toContain(`/sessions/${SID}/rt`)
    expect(JSON.parse(session.getItem(STORAGE_KEY)!).sid).toBe(SID)
    expect(JSON.parse(local.getItem(STORAGE_KEY)!).token).toBe('v1.tok.1')
  })

  it('viewport narrower than 360 sends nothing; phones and tablets may open a sandbox (ADR-122)', async () => {
    vp = { w: 320, h: 640 }
    await createSandbox('synthcity')
    expect(calls).toHaveLength(0)
    expect(sandboxSessionStore.getState().phase).toBe('none')
    expect(sandboxSessionStore.getState().error?.message).toBe('viewport')
    vp = { w: 390, h: 844 }
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST' ? { status: 201, body: created } : undefined))
    sessionRoutes(['READY'])
    await createSandbox('synthcity')
    expect(sandboxSessionStore.getState().phase).toBe('ready')
  })

  it('409 509 -> challenge -> solved and retried', async () => {
    let n = 0
    routes.push((url, init) => {
      if (url !== '/api/sandbox/v1/sessions' || init.method !== 'POST') return undefined
      n++
      if (n === 1) return { status: 409, body: { code: 509, detail: { why: 'required', challenge: { challenge_id: 'c-abcdefghij', alg: 'sha256-lz', prefix_b64: 'AAAA', bits: 4, expires_unix_ns: '0' } } } }
      return { status: 201, body: created }
    })
    sessionRoutes(['READY'])
    const seen: string[] = []
    const off = sandboxSessionStore.subscribe((s) => { if (seen.at(-1) !== s.phase) seen.push(s.phase) })
    await createSandbox('synthcity')
    off()
    expect(seen).toContain('challenge')
    expect((calls[1].body as { pow: unknown }).pow).toEqual({ challenge_id: 'c-abcdefghij', nonce: 42 })
    expect(sandboxSessionStore.getState().phase).toBe('ready')
  })

  it('409 508 keeps the startable worlds', async () => {
    routes.push(() => ({ status: 409, body: { code: 508, detail: { available_now: ['synthcity'], queue_ok: true } } }))
    await createSandbox('shanghai')
    const s = sandboxSessionStore.getState()
    expect(s.phase).toBe('none')
    expect(s.error?.code).toBe(508)
    expect(s.availableNow).toEqual(['synthcity'])
  })
})

describe('queue', () => {
  it('202 -> queued -> slot_ready -> claim -> ready; leave', async () => {
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST'
      ? { status: 202, body: { queue: { ticket_id: 'q-abcdefghij', ticket_token: 'q1.xyz', position: 2, eta_s: 300 }, available_now: [] } } : undefined))
    let polls = 0
    routes.push((url) => {
      if (!url.startsWith('/api/sandbox/v1/queue/q-abcdefghij?')) return undefined
      polls++
      return { status: 200, body: polls < 2 ? { ticket_id: 'q-abcdefghij', state: 'waiting', position: 1, eta_s: 120 }
        : { ticket_id: 'q-abcdefghij', state: 'slot_ready', position: 0, eta_s: 0, claim_deadline_unix_ns: ns(T0 + 60_000) } }
    })
    routes.push((url) => (url === '/api/sandbox/v1/queue/q-abcdefghij/claim' ? { status: 201, body: created } : undefined))
    sessionRoutes(['READY'])
    await createSandbox('synthcity')
    for (let i = 0; i < 20 && sandboxSessionStore.getState().phase !== 'slot_ready'; i++) await new Promise((r) => setTimeout(r, 1))
    const s = sandboxSessionStore.getState()
    expect(s.phase).toBe('slot_ready')
    expect(s.queue?.offerExpiresInS).toBe(60)
    expect(calls.find((c) => c.url.includes('/queue/'))?.auth).toBe('Bearer q1.xyz')
    await claimSlot()
    expect(sandboxSessionStore.getState().phase).toBe('ready')
    expect(sandboxSessionStore.getState().token).toBe('v1.tok.1')
  })

  it('leaveQueue sends DELETE with the ticket token', async () => {
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST'
      ? { status: 202, body: { queue: { ticket_id: 'q-abcdefghij', ticket_token: 'q1.xyz', position: 1, eta_s: 10 } } } : undefined))
    routes.push((url, init) => (url.startsWith('/api/sandbox/v1/queue/q-abcdefghij') && init.method === 'DELETE' ? { status: 204 } : undefined))
    routes.push((url) => (url.startsWith('/api/sandbox/v1/queue/') ? { status: 200, body: { ticket_id: 'q-abcdefghij', state: 'waiting', position: 1, eta_s: 10 } } : undefined))
    await createSandbox('synthcity')
    await leaveQueue()
    expect(calls.some((c) => c.method === 'DELETE' && c.auth === 'Bearer q1.xyz')).toBe(true)
    expect(sandboxSessionStore.getState().phase).toBe('none')
  })
})

describe('resume, tabs and keepalive', () => {
  it('refresh resumes from sessionStorage; a second tab from the localStorage backup', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 1_000_000) }))
    sessionRoutes(['ACTIVE'])
    expect(await resumeSandbox()).toBe(true)
    expect(sandboxSessionStore.getState().phase).toBe('active')
    expect(getEndpoint().kind).toBe('sandbox')
    expect(calls[0].auth).toBe('Bearer v1.tok.1')
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.old', expires_unix_ns: ns(T0 - 1) }))
    setup('t2', { local })
    expect(await resumeSandbox()).toBe(false)
    expect(local.getItem(STORAGE_KEY)).toBeNull()
  })

  it('idempotent create (200, no token) resumes from storage', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 1_000_000) }))
    routes.push((url, init) => (url === '/api/sandbox/v1/sessions' && init.method === 'POST'
      ? { status: 200, body: { session: view('ACTIVE'), token: null, idempotent: true } } : undefined))
    sessionRoutes(['ACTIVE'])
    await createSandbox('synthcity')
    expect(sandboxSessionStore.getState().phase).toBe('active')
    expect(sandboxSessionStore.getState().token).toBe('v1.tok.1')
  })

  it('auto keepalive: only near expiry, with recent input, from one tab; token shared over the channel', async () => {
    const shared = new MemStorage()
    setup('t1', { local: shared })
    shared.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 1_000_000) }))
    sessionRoutes(['ACTIVE'])
    expect(await resumeSandbox()).toBe(true)
    routes.unshift((url) => (url.endsWith('/keepalive') ? { status: 200, body: { session: view('ACTIVE', T0 + 3_000_000), renewed: true,
      token: 'v1.tok.2', token_exp_unix_ns: ns(T0 + 3_000_000) } } : undefined))
    noteUserInput()
    expect(await autoKeepaliveTick()).toBe(false)                 // 1800 s left (server view)
    now = T0 + 1_600_000                                           // 200 s left
    noteUserInput()
    expect(await autoKeepaliveTick()).toBe(true)
    expect(sandboxSessionStore.getState().token).toBe('v1.tok.2')
    expect(JSON.parse(shared.getItem(KA_LOCK_KEY)!).tab).toBe('t1')
    const ch = Chan.all.at(-1)!
    expect(ch.sent.some((m) => (m as { type: string }).type === 'token')).toBe(true)
    now = T0 + 1_600_000 + 2_700_000                               // 200 s left of the renewed lifetime, no input for 45 min
    expect(await autoKeepaliveTick()).toBe(false)
  })

  it('another tab holding the renewal lease blocks this tab', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 100_000) }))
    local.setItem(KA_LOCK_KEY, JSON.stringify({ tab: 'other', t: T0 }))
    sessionRoutes(['ACTIVE'])
    await resumeSandbox()
    noteUserInput()
    expect(await autoKeepaliveTick()).toBe(false)
    expect(calls.some((c) => c.url.endsWith('/keepalive'))).toBe(false)
  })

  it('renewal refused while the queue is non-empty keeps the token', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 100_000) }))
    sessionRoutes(['ACTIVE'])
    await resumeSandbox()
    routes.unshift((url) => (url.endsWith('/keepalive') ? { status: 200, body: { session: view('ACTIVE', T0 + 100_000, { renewable: false,
      renew_block_reason: 'queue_nonempty' }), renewed: false, token: null } } : undefined))
    expect(await keepaliveSandbox()).toBe(false)
    const s = sandboxSessionStore.getState()
    expect(s.token).toBe('v1.tok.1')
    expect(s.renewBlockReason).toBe('queue_nonempty')
  })
})

describe('events and end', () => {
  it('sandbox.expiring exports the snapshot to localStorage; 4410 ends', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 1_000_000) }))
    sessionRoutes(['ACTIVE'])
    routes.unshift((url) => (url.endsWith('/snapshot') ? { status: 200, body: { schema: 'awr.sandbox.snapshot.v1', world_id: 'synthcity' } } : undefined))
    await resumeSandbox()
    handleSandboxEvent('sandbox.expiring', { sid: SID, reason: 'preempted', in_s: 60 })
    for (let i = 0; i < 10 && !local.getItem(SNAPSHOT_KEY); i++) await new Promise((r) => setTimeout(r, 1))
    expect(JSON.parse(local.getItem(SNAPSHOT_KEY)!).snapshot.world_id).toBe('synthcity')
    expect(sandboxSessionStore.getState().expiring).toEqual({ reason: 'preempted', inS: 60 })
    handleSandboxEvent('sandbox.rate', { sid: SID, granted: 2, cap: 5.6, limited_by: 'cost_cap' })
    expect(sandboxSessionStore.getState().grantedRate).toBe(2)
    markSandboxGone(510)
    expect(sandboxSessionStore.getState().phase).toBe('ended')
    expect(local.getItem(STORAGE_KEY)).toBeNull()
    expect(getEndpoint().kind).toBe('shared')
  })

  it('endSandbox deletes, broadcasts and clears', async () => {
    local.setItem(STORAGE_KEY, JSON.stringify({ sid: SID, token: 'v1.tok.1', expires_unix_ns: ns(T0 + 1_000_000) }))
    sessionRoutes(['ACTIVE'])
    routes.unshift((url, init) => (init.method === 'DELETE' ? { status: 202, body: { state: 'DRAINING' } } : undefined))
    await resumeSandbox()
    await endSandbox()
    expect(calls.some((c) => c.method === 'DELETE' && c.auth === 'Bearer v1.tok.1')).toBe(true)
    expect(sandboxSessionStore.getState().phase).toBe('ended')
    expect(Chan.all.at(-1)!.sent.some((m) => (m as { type: string }).type === 'ended')).toBe(true)
    expect(session.getItem(STORAGE_KEY)).toBeNull()
  })
})
