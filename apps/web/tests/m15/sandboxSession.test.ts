// M15-FR-204, FR-206 to FR-208 (AWR-17 §17.4; AWR-14 §20.4; UX-FR-106 to UX-FR-111; M15-AC-203 to AC-206 unit part): the
// interim sandbox session store against a scripted sandbox REST: windows below 1280 x 720 never send a request; 201
// enters and stores the token (sessionStorage and the localStorage backup); 409 code 509 solves the challenge and sends
// again with the nonce; 202 queues and the long poll turns slot_ready into the claim window; 504 keeps the Retry-After;
// a reload recovers the session from storage and 410 drops it; keepalive swaps the token; the 1 Hz topic merges; the
// automatic renewal rule; snapshots are kept per world within 256 KB; storage failures never throw.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import createdFixture from '../../../../packages/contracts/fixtures/d2/rest/session_created.json'
import sessionJson from '../../../../packages/contracts/fixtures/d2/rest/session.json'
import snapshotFixture from '../../../../packages/contracts/fixtures/d2/rest/snapshot.json'

// the fixtures carry fixed instants: move their expiries 30 min into the future of this run
const future = String(BigInt(Date.now() + 1800_000) * 1_000_000n)
const sessionFixture = { ...sessionJson, expires_unix_ns: future }
const sessionCreated = { ...createdFixture, session: { ...createdFixture.session, expires_unix_ns: future }, token_exp_unix_ns: future }

function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return {
    get length() { return m.size },
    clear: () => m.clear(),
    getItem: (k: string) => m.get(k) ?? null,
    key: (i: number) => [...m.keys()][i] ?? null,
    removeItem: (k: string) => void m.delete(k),
    setItem: (k: string, v: string) => void m.set(k, v),
  }
}

type Reply = { status: number; body?: unknown; headers?: Record<string, string> }
const calls: { method: string; url: string; body: unknown; auth: string | null; client: string | null }[] = []
function script(handler: (method: string, url: string, body: unknown) => Reply) {
  return async (url: string, init: RequestInit): Promise<Response> => {
    const method = init.method ?? 'GET'
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    const h = new Headers(init.headers)
    calls.push({ method, url, body, auth: h.get('authorization'), client: h.get('x-awr-client') })
    const r = handler(method, url, body)
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status, headers: r.headers })
  }
}

const SID = 'sb-k3m7q2x7ab'
const BIG = { w: 1600, h: 900 }

beforeEach(() => {
  vi.resetModules()
  calls.length = 0
  vi.stubGlobal('sessionStorage', memoryStorage())
  vi.stubGlobal('localStorage', memoryStorage())
})
afterEach(() => {
  vi.unstubAllGlobals()
})

async function load() {
  return import('@/ui/sandbox/session')
}

describe('create', () => {
  it('refuses a window narrower than 360 CSS px without any request (D2.1 ADR-122: phones and tablets may open one)', async () => {
    const m = await load()
    m.setSandboxFetcher(script(() => ({ status: 500 })))
    expect(m.windowLargeEnough(1200, 700)).toBe(true)
    expect(m.windowLargeEnough(390, 844)).toBe(true)
    const r = await m.sandbox.create('synthcity', 't5_area_guard', { window: { w: 320, h: 640 } })
    expect(r.kind).toBe('refused')
    expect(calls).toHaveLength(0)
  })

  it('201 stores the session and its token, 509 solves the challenge first', async () => {
    const m = await load()
    let round = 0
    m.setSandboxFetcher(script((method, url, body) => {
      if (method === 'POST' && url.endsWith('/api/sandbox/v1/sessions')) {
        round++
        if (round === 1) return { status: 409, body: { code: 509, message: 'challenge', detail: { why: 'required', challenge: { challenge_id: 'c-1', alg: 'sha256-lz', prefix_b64: 'AAAA', bits: 4, expires_unix_ns: '0' } } } }
        expect((body as { pow: unknown }).pow).toEqual({ challenge_id: 'c-1', nonce: 42 })
        return { status: 201, body: sessionCreated }
      }
      return { status: 404 }
    }))
    m.setPowSolver(async () => 42)
    const r = await m.sandbox.create('synthcity', 't5_area_guard', { window: BIG, calendar: { start_utc: '2026-10-05T02:00:00Z', tz: 'Asia/Shanghai' } })
    expect(r).toEqual({ kind: 'created', sid: SID })
    const s = m.sandboxStore.getState()
    expect(s.phase).toBe('spawning')
    expect(s.token).toBe(sessionCreated.token)
    expect(m.storedSession(SID)?.token).toBe(sessionCreated.token)
    expect(JSON.parse(localStorage.getItem(m.KEYS.session) ?? '{}').sid).toBe(SID)
    expect(calls[0].body).toMatchObject({ world: 'synthcity', template: 't5_area_guard', calendar: { tz: 'Asia/Shanghai' } })
    expect(calls[0].auth).toBeNull()
  })

  it('202 queues, the long poll reaches slot_ready and the claim enters', async () => {
    const m = await load()
    let polls = 0
    m.setSandboxFetcher(script((method, url) => {
      if (method === 'POST' && url.endsWith('/sessions')) return { status: 202, body: { queue: { ticket_id: 'q-1', ticket_token: 'q1.tok', position: 2, eta_s: 240, expires_unix_ns: '9999999999999999999' } } }
      if (method === 'GET' && url.includes('/queue/q-1')) {
        polls++
        return polls === 1 ? { status: 200, body: { ticket_id: 'q-1', state: 'waiting', position: 1, eta_s: 60 } }
          : { status: 200, body: { ticket_id: 'q-1', state: 'slot_ready', position: 0, eta_s: 0, claim_deadline_unix_ns: String(BigInt(Date.now() + 60_000) * 1_000_000n) } }
      }
      if (method === 'POST' && url.endsWith('/queue/q-1/claim')) return { status: 201, body: sessionCreated }
      return { status: 404 }
    }))
    const r = await m.sandbox.create('synthcity', 't5_area_guard', { window: BIG })
    expect(r.kind).toBe('queued')
    await vi.waitFor(() => expect(m.sandboxStore.getState().phase).toBe('slot_ready'))
    const q = m.sandboxStore.getState().queue
    expect(q?.ticketToken).toBe('q1.tok')
    expect(q?.claimDeadlineMs).toBeGreaterThan(Date.now())
    expect(calls.find((c) => c.url.includes('/queue/q-1?wait_s=25'))?.auth).toBe('Bearer q1.tok')
    const c = await m.sandbox.claim()
    expect(c).toEqual({ kind: 'created', sid: SID })
    expect(m.sandboxStore.getState().queue).toBeNull()
  })

  it('504 keeps the reason and the Retry-After; 200 without a token means another tab', async () => {
    const m = await load()
    m.setSandboxFetcher(script(() => ({ status: 429, body: { code: 504, message: 'quota' }, headers: { 'retry-after': '30' } })))
    const r = await m.sandbox.create('synthcity', null, { window: BIG })
    expect(r.kind).toBe('refused')
    const s = m.sandboxStore.getState()
    expect(s.error?.code).toBe(504)
    expect(s.retryUntilMs).toBeGreaterThan(Date.now() + 25_000)
    m.setSandboxFetcher(script(() => ({ status: 200, body: { session: sessionFixture, token: null, idempotent: true } })))
    const r2 = await m.sandbox.create('synthcity', null, { window: BIG })
    expect(r2.kind).toBe('another-tab')
    expect(m.sandboxStore.getState().anotherTab).toBe(true)
  })
})

describe('session life', () => {
  async function created() {
    const m = await load()
    m.setSandboxFetcher(script((method, url) => {
      if (method === 'POST' && url.endsWith('/sessions')) return { status: 201, body: sessionCreated }
      if (method === 'POST' && url.endsWith('/keepalive')) return { status: 200, body: { session: { ...sessionFixture, expires_unix_ns: future }, renewed: true, token: 'v1.new', token_exp_unix_ns: future } }
      if (method === 'GET' && url.endsWith('/snapshot')) return { status: 200, body: snapshotFixture }
      if (method === 'GET' && url.endsWith(`/sessions/${SID}`)) return { status: 200, body: sessionFixture }
      if (method === 'DELETE') return { status: 202, body: { state: 'DRAINING' } }
      return { status: 404 }
    }))
    await m.sandbox.create('synthcity', 't5_area_guard', { window: BIG })
    return m
  }

  it('keepalive swaps the token and tells the realtime client', async () => {
    const m = await created()
    const seen: string[] = []
    const off = m.onSandboxToken((tk) => seen.push(tk))
    expect(await m.sandbox.keepalive()).toBe(true)
    expect(m.sandboxStore.getState().token).toBe('v1.new')
    expect(m.storedSession(SID)?.token).toBe('v1.new')
    expect(seen).toEqual(['v1.new'])
    expect(calls.find((c) => c.url.endsWith('/keepalive'))?.auth).toBe(`Bearer ${sessionCreated.token}`)
    off()
  })

  it('merges the topic, notes the expiry and saves the snapshot locally', async () => {
    const m = await created()
    m.sandbox.applySession(sessionFixture as never)
    expect(m.sandboxStore.getState().phase).toBe('active')
    m.sandbox.applyTopic({ sid: SID, state: 'ACTIVE', expires_unix_ns: future, expires_in_s: 100, renewable: false, renew_block_reason: 'queue_nonempty',
      clock: { rate_granted: 2, rate_cap: 5, limited_by: 'cpu_budget' }, usage: { vehicles: 3, targets: 1, nests: 1, tasks_active: 1, ws: 1 }, queue_len: 2, degrade_level: 'L0',
      expiring: { reason: 'preempted', in_s: 60 } })
    const s = m.sandboxStore.getState()
    expect(s.session?.renewable).toBe(false)
    expect(s.session?.clock.rate_granted).toBe(2)
    expect(s.session?.usage?.vehicles).toBe(3)
    expect(s.expiring?.reason).toBe('preempted')
    await vi.waitFor(() => expect(m.localSnapshot('synthcity')?.snapshot.world_id).toBe('synthcity'))
  })

  it('external API activity of the topic becomes the page-clock time of the store; every request names the web client (issue #59)', async () => {
    const m = await created()
    m.sandbox.applySession(sessionFixture as never)
    const topic = { sid: SID, state: 'ACTIVE' as const, expires_unix_ns: future, expires_in_s: 100, renewable: true, renew_block_reason: null,
      clock: { rate_granted: 1, rate_cap: 5, limited_by: null }, usage: { vehicles: 2, targets: 1, nests: 1, tasks_active: 0, ws: 1 }, queue_len: 0,
      degrade_level: 'L0' as const, expiring: null }
    m.sandbox.applyTopic(topic)
    expect(m.sandboxStore.getState().apiActivityMs).toBe(0)
    const t0 = Date.now()
    m.sandbox.applyTopic({ ...topic, api_activity_age_s: 4 })
    const at = m.sandboxStore.getState().apiActivityMs
    expect(Math.abs(at - (t0 - 4000))).toBeLessThan(500)
    m.sandbox.applyTopic({ ...topic, api_activity_age_s: 4.2 }) // the same activity one topic later
    expect(m.sandboxStore.getState().apiActivityMs).toBe(at)
    m.sandbox.applyTopic({ ...topic, api_activity_age_s: 0 })
    expect(m.sandboxStore.getState().apiActivityMs).toBeGreaterThanOrEqual(at + 3000)
    expect(calls.length).toBeGreaterThan(0)
    expect(calls.every((c) => c.client?.startsWith('awr-web/'))).toBe(true)
  })

  it('the end beacon path: DELETE then draining; ENDED clears the stored token', async () => {
    const m = await created()
    await m.sandbox.end()
    expect(m.sandboxStore.getState().phase).toBe('draining')
    m.sandbox.applySession({ ...sessionFixture, state: 'ENDED', end_reason: 'user' } as never)
    expect(m.sandboxStore.getState().phase).toBe('ended')
    expect(m.storedSession(SID)).toBeNull()
  })

  it('a reload recovers the stored session; 410 drops it', async () => {
    const m0 = await created()
    expect(m0.storedSession(SID)).not.toBeNull()
    vi.resetModules()
    const m = await load()
    m.setSandboxFetcher(script(() => ({ status: 200, body: sessionFixture })))
    expect(await m.sandbox.resume(SID)).toBe(true)
    expect(m.sandboxStore.getState().token).toBe(sessionCreated.token)
    m.sandbox.reset()
    m.setSandboxFetcher(script(() => ({ status: 410, body: { code: 506 } })))
    expect(await m.sandbox.resume(SID)).toBe(false)
    expect(m.storedSession(SID)).toBeNull()
    expect(m.sandboxStore.getState().endReason).toBe('gone')
  })
})

describe('rules', () => {
  it('renews automatically only when due, renewable, visible and live', async () => {
    const m = await load()
    const exp = (s: number) => String(BigInt(Date.now() + s * 1000) * 1_000_000n)
    const sess = (left: number, renewable = true) => ({ ...sessionFixture, expires_unix_ns: exp(left), renewable }) as never
    expect(m.renewDue({ phase: 'active', token: 't', session: sess(200) })).toBe(true)
    expect(m.renewDue({ phase: 'active', token: 't', session: sess(400) })).toBe(false)
    expect(m.renewDue({ phase: 'active', token: 't', session: sess(200, false) })).toBe(false)
    expect(m.renewDue({ phase: 'active', token: 't', session: sess(200) }, Date.now(), false)).toBe(false)
    expect(m.renewDue({ phase: 'spawning', token: 't', session: sess(200) })).toBe(false)
  })

  it('maps session states to phases', async () => {
    const m = await load()
    expect(['SPAWNING', 'READY', 'ACTIVE', 'IDLE', 'DRAINING', 'ENDED', 'FAILED'].map((s) => m.phaseOfState(s as never)))
      .toEqual(['spawning', 'ready', 'active', 'active', 'draining', 'ended', 'failed'])
  })

  it('keeps snapshots per world within 256 KB and survives a throwing storage', async () => {
    const m = await load()
    expect(m.saveLocalSnapshot(snapshotFixture as never)).toBe(true)
    expect(m.saveLocalSnapshot({ ...snapshotFixture, world_id: 'shanghai' } as never)).toBe(true)
    expect(m.localSnapshot('synthcity')).not.toBeNull()
    expect(m.localSnapshot('shanghai')).not.toBeNull()
    const huge = { ...snapshotFixture, world_id: 'chicago', env: { preset: 'x'.repeat(300 * 1024) } }
    expect(m.saveLocalSnapshot(huge as never)).toBe(false)
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') }, removeItem: () => { throw new Error('denied') } })
    expect(m.localSnapshot('synthcity')).toBeNull()
    expect(m.storedSession(SID)).toBeNull()
    expect(() => m.principalHint()).not.toThrow()
  })

  // d2-WP-13b-to-M15 item 10 (D2-INT): one principal hint for the page token (R01, base32), the viewer queue (R81) and the
  // sandbox; an existing hint of either accepted form is reused and never overwritten, a new one is base32
  it('shares the D1 base32 principal hint and creates a base32 one when there is none', async () => {
    const m = await load()
    localStorage.setItem('awr.principal_hint', 'QCRYQQRE5XB2LIGAB2WGRC7QCQ')
    expect(m.principalHint()).toBe('QCRYQQRE5XB2LIGAB2WGRC7QCQ')
    expect(localStorage.getItem('awr.principal_hint')).toBe('QCRYQQRE5XB2LIGAB2WGRC7QCQ')
    localStorage.setItem('awr.principal_hint', '0123456789abcdef0123456789abcdef')
    expect(m.principalHint()).toBe('0123456789abcdef0123456789abcdef')
    localStorage.removeItem('awr.principal_hint')
    const h = m.principalHint()
    expect(h).toMatch(/^[A-Z2-7]{26}$/)
    expect(localStorage.getItem('awr.principal_hint')).toBe(h)
    expect(m.principalHint()).toBe(h)
    localStorage.setItem('awr.principal_hint', 'not a hint!')
    expect(m.principalHint()).toMatch(/^[A-Z2-7]{26}$/)
  })
})
