// Sandbox session port and its interim implementation (AWR-04 §4.6, §10.2; AWR-14 §20.4; AWR-17 §17.4; M15-FR-204,
// FR-206 to FR-208, UX-FR-106 to UX-FR-111). The domain store is M17's `stores/sandboxSession.ts` (M17 PRD §7.9, WP-04);
// until it ships the shell runs on this interim store with the same phase names and actions, built on the frozen
// contracts (`@awr/contracts/types` Session, SessionCreated, SessionQueued, Ticket, SessionKeepaliveReply, Snapshot,
// SandboxSessionState) and the sandbox REST of AWR-17 §17.4. Wiring point: bindSandboxSessionStore() (report list).
// The interim store covers: creation with the proof-of-work retry (409 code 509), the queue ticket long poll (wait_s 25,
// slot_ready with a 60 s claim window, claim, leave), the session object from REST and the `sandbox/session` topic (1 Hz),
// automatic keepalive (remaining <= 300 s, renewable, page visible, one tab), end, world switch, template, snapshot export
// and restore, and recovery after a reload from sessionStorage with a localStorage backup (AWR-14 §20.4.7). Every storage
// access is fault tolerant (UX-NFR-028). Creation refuses windows smaller than 1280 x 720 without a request.
// WP-12b: the proof of work is M17's solver (net/sandbox/pow.ts); the session state itself stays on this port until M17's
// store carries the fields the shell needs (another tab, the verification hint, Retry-After, the world switch, the queue
// ticket kept over a reload, the 14 §20.4.7 storage keys; request d2-WP-12b-to-M17).
// D2.1 (R-D2-33, R-D2-34; ADR-121, ADR-122; AWR-04 §15.6.4, §15.7.4; AWR-17 §17.20.4; M15-FR-226, FR-227): power saving parks
// the session (SB-69 park, SB-72 park beacon from pagehide or freeze, WS close 4430 from the server) and resume brings it
// back (SB-70: 201 a fresh token, 200 already active, 202 a resume ticket in the same FIFO as new visitors, claimed
// automatically while the page is visible; 410 the park retention passed). A parked session keeps its stored token up to the
// park retention (the server accepts an expired token on resume), so the sandbox surface stays while parked. The internal
// identity token rides on session creation as X-AWR-Identity (setSandboxIdentity, fed by stores/identity.ts). Windows
// narrower than 360 CSS px never create a session (ADR-122: phones and tablets may open a sandbox).
import type { Session, SessionCreated, SessionChallenge, SessionKeepaliveReply, SessionQueued, Snapshot, Ticket, SandboxSessionState } from '@awr/contracts/types'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { CLIENT_HEADER, CLIENT_NAME } from '@/net/clientName'
import { apiUrl, SANDBOX_API } from '@/net/endpoint'
import type { RtClient, RtEvent } from '@/net/rt'
import { UX } from '@/ui/testing/uxProbe'
import { solvePow } from '@/net/sandbox/pow'
import { freezableInterval } from '@/lib/powerFreeze'

export type SandboxPhase = 'none' | 'creating' | 'challenge' | 'queued' | 'slot_ready' | 'spawning' | 'ready' | 'active'
  | 'parking' | 'parked' | 'resuming' | 'draining' | 'ended' | 'failed'
/** session states of the wire including the D2.1 park states (contracts 1.2.0; the 1.1.0 types lack them) */
type WireState = Session['state'] | 'PARKING' | 'PARKED'
export type EndReason = NonNullable<Session['end_reason']> | 'gone' | 'parked_expired'

export const SID_RE = /^sb-[a-z2-7]{10}$/
/** ADR-122: only windows narrower than 360 CSS px refuse a sandbox (D2's 1280 x 720 guard is revised) */
export const MIN_WINDOW = { w: 360, h: 0 } as const
/** AWR-04 §15.6.2 sandbox.park.retention_s: how long a parked session (and its stored token) stays resumable */
export const PARK_RETENTION_MS = 3600_000
export const QUEUE_WAIT_S = 25
export const CLAIM_WINDOW_MS = 60_000
export const AUTO_RENEW_S = 300
export const SNAPSHOT_MAX_BYTES = 256 * 1024
export const SNAPSHOT_KEEP_MS = 7 * 24 * 3600 * 1000
export const KEYS = {
  session: 'awr.sandbox.session.v1',
  queue: 'awr.sandbox.queue.v1',
  snapshot: 'awr.sandbox.snapshot.v1',
  principal: 'awr.principal_hint',
  renewLock: 'awr.sandbox.renew.v1',
} as const

export interface SandboxError { code: number | null; status: number; message: string; detail: Record<string, unknown> | null }
export interface QueueView {
  /** a new visitor's ticket or the resume ticket of a parked session (AWR-04 §15.6.4 item 2) */
  kind: 'new' | 'resume'
  ticketId: string
  ticketToken: string
  position: number
  etaS: number | null
  /** wall ms of the claim deadline once the slot is ready */
  claimDeadlineMs: number | null
  world: string
  template: string | null
}
export interface SandboxSessionView {
  phase: SandboxPhase
  sid: string | null
  token: string | null
  tokenExpUnixMs: number
  world: string | null
  template: string | null
  /** latest session object (REST or the 1 Hz topic, merged) */
  session: Session | null
  /** server time offset of `expires_unix_ns` reads (wall ms of the last update) */
  updatedAtMs: number
  queue: QueueView | null
  expiring: { reason: string; atMs: number } | null
  endReason: EndReason | null
  error: SandboxError | null
  /** proof of work in progress; the hint shows after VERIFY_HINT_MS (a fast solve never flashes it) */
  verifying: boolean
  verifyHint: boolean
  /** wall ms when the current phase began (the "slow start" hint) */
  phaseSinceMs: number
  /** wall ms until which a quota refusal asks to wait (Retry-After) */
  retryUntilMs: number
  /** the create answered "your sandbox runs in another tab" without a token (AWR-14 §20.4.7) */
  anotherTab: boolean
  /** a world switch is in progress (loading layer and session bar) */
  switching: { world: string; template: string | null } | null
  /** D2.1: the parked session (reason, resume mode, frozen sim time, retention) */
  parked: ParkedView | null
  /** D2.1: tier of the session (internal identity sessions: reserved slot, 8 h, never preempted) */
  tier: 'public' | 'internal' | null
  /** wall ms (page clock) of the latest external API activity of the session (topic `api_activity_age_s`; SDK, scripts and
   *  other clients; 0: none seen); the power-save idle clock follows it (issue #59) */
  apiActivityMs: number
  version: number
}
export interface ParkedView {
  reason: string | null
  resumeMode: 'checkpoint' | 'snapshot' | null
  parkedAtMs: number
  retentionUntilMs: number
  tSimNs: string | null
  remainingS: number | null
}

const INITIAL: SandboxSessionView = {
  phase: 'none', sid: null, token: null, tokenExpUnixMs: 0, world: null, template: null, session: null, updatedAtMs: 0, queue: null,
  expiring: null, endReason: null, error: null, verifying: false, verifyHint: false, phaseSinceMs: 0, retryUntilMs: 0, anotherTab: false,
  switching: null, parked: null, tier: null, apiActivityMs: 0, version: 0,
}
export const VERIFY_HINT_MS = 300
/** external API activity newer than the stored one by at least this much updates the store (the 1 Hz topic repeats the age) */
export const API_ACTIVITY_STEP_MS = 1000

export const sandboxStore = createAwrStore<SandboxSessionView>('ui.sandbox', () => ({ ...INITIAL }))
export const useSandbox = <T,>(sel: (s: SandboxSessionView) => T): T => useStore(sandboxStore, sel)

function set(p: Partial<SandboxSessionView>): void {
  const s = sandboxStore.getState()
  const since = p.phase !== undefined && p.phase !== s.phase ? { phaseSinceMs: Date.now() } : {}
  sandboxStore.setState({ ...p, ...since, version: s.version + 1 })
  const n = sandboxStore.getState()
  UX.sandbox.phase = n.phase
  UX.sandbox.queue = n.queue ? n.phase : 'none'
}

// ------------------------------------------------------------ storage (fault tolerant)
function store(kind: 'session' | 'local'): Storage | null {
  try {
    return kind === 'session' ? globalThis.sessionStorage ?? null : globalThis.localStorage ?? null
  } catch {
    return null
  }
}
function readJson<T>(s: Storage | null, key: string): T | null {
  if (!s) return null
  try {
    const v = s.getItem(key)
    return v ? (JSON.parse(v) as T) : null
  } catch {
    return null
  }
}
function writeJson(s: Storage | null, key: string, v: unknown): boolean {
  if (!s) return false
  try {
    s.setItem(key, JSON.stringify(v))
    return true
  } catch {
    return false
  }
}
function remove(s: Storage | null, key: string): void {
  try {
    s?.removeItem(key)
  } catch {
    // storage unavailable
  }
}

interface StoredSession { sid: string; token: string; token_exp_unix_ms: number; world: string | null; template: string | null; parked_until_ms?: number }
interface StoredQueue { ticket_id: string; ticket_token: string; expires_unix_ms: number; world: string; template: string | null; kind?: 'new' | 'resume'; sid?: string }
interface StoredSnapshots { v: 1; items: Record<string, { savedUnixMs: number; snapshot: Snapshot }> }

export function storedSession(sid?: string): StoredSession | null {
  for (const s of [store('session'), store('local')]) {
    const x = readJson<StoredSession>(s, KEYS.session)
    if (x && typeof x.sid === 'string' && SID_RE.test(x.sid) && typeof x.token === 'string' && x.token && (!sid || x.sid === sid)) {
      const now = Date.now()
      if (Number.isFinite(x.token_exp_unix_ms) && x.token_exp_unix_ms > now) return x
      // a parked session stays resumable with its (expired) token until the park retention passes (SB-70)
      if (Number.isFinite(x.parked_until_ms) && (x.parked_until_ms ?? 0) > now) return x
    }
  }
  return null
}
function saveSession(x: StoredSession): void {
  writeJson(store('session'), KEYS.session, x)
  writeJson(store('local'), KEYS.session, x)
}
function clearSession(): void {
  remove(store('session'), KEYS.session)
  remove(store('local'), KEYS.session)
}

export function storedQueue(): StoredQueue | null {
  const x = readJson<StoredQueue>(store('session'), KEYS.queue)
  return x && typeof x.ticket_id === 'string' && x.expires_unix_ms > Date.now() ? x : null
}

/** the last snapshot saved in this browser for a world (7 days), null otherwise */
export function localSnapshot(world: string): { savedUnixMs: number; snapshot: Snapshot } | null {
  const x = readJson<StoredSnapshots>(store('local'), KEYS.snapshot)
  const it = x?.items?.[world]
  return it && Date.now() - it.savedUnixMs < SNAPSHOT_KEEP_MS && it.snapshot?.schema === 'awr.sandbox.snapshot.v1' ? it : null
}
/** keep a snapshot (<= 256 KB in total, expired entries dropped); false when it did not fit or storage is off */
export function saveLocalSnapshot(s: Snapshot): boolean {
  const cur = readJson<StoredSnapshots>(store('local'), KEYS.snapshot)
  const items: StoredSnapshots['items'] = {}
  const now = Date.now()
  for (const [w, it] of Object.entries(cur?.items ?? {})) if (now - it.savedUnixMs < SNAPSHOT_KEEP_MS && w !== s.world_id) items[w] = it
  items[s.world_id] = { savedUnixMs: now, snapshot: s }
  const text = JSON.stringify({ v: 1, items })
  if (text.length > SNAPSHOT_MAX_BYTES) {
    const only = JSON.stringify({ v: 1, items: { [s.world_id]: items[s.world_id] } })
    if (only.length > SNAPSHOT_MAX_BYTES) return false
    return writeJson(store('local'), KEYS.snapshot, JSON.parse(only))
  }
  return writeJson(store('local'), KEYS.snapshot, { v: 1, items })
}

/**
 * the principal hint shared with the D1 page token, the viewer queue (R81) and the identity (R79): one localStorage key
 * (net/api.ts `awr.principal_hint`), so the sandbox, the live seat and the ticket name the same principal. show-api takes
 * a 16 to 64 character base32 hint (AWR-17 §3.1) and the sandbox API takes base32 or 32 hex (§17.3), so an existing hint
 * of either form is reused as is (never overwritten: overwriting the D1 page's base32 hint moved the page token to another
 * principal and the viewer queue's seat_ready reservation never matched it, d2-WP-13b-to-M15 item 10); a new hint is
 * 26 base32 characters (128 bits), which both servers accept.
 */
export const PRINCIPAL_HINT_RE = /^(?:[0-9a-f]{32}|[A-Za-z2-7]{16,64})$/
const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
export function principalHint(): string {
  const ls = store('local')
  let h: string | null = null
  try {
    h = ls?.getItem(KEYS.principal) ?? null
  } catch {
    h = null
  }
  if (h && PRINCIPAL_HINT_RE.test(h)) return h
  const b = new Uint8Array(17)
  globalThis.crypto?.getRandomValues?.(b)
  // 26 base32 characters from 130 random bits (5 bits each)
  let out = ''
  let acc = 0
  let bits = 0
  for (const x of b) {
    acc = (acc << 8) | x
    bits += 8
    while (bits >= 5 && out.length < 26) {
      bits -= 5
      out += B32[(acc >> bits) & 31]
    }
    acc &= (1 << bits) - 1
  }
  h = out
  try {
    ls?.setItem(KEYS.principal, h)
  } catch {
    // private mode: a new principal next time
  }
  return h
}

// ------------------------------------------------------------ HTTP (status codes matter: 200 idempotent, 201, 202)
export interface HttpReply<T> { status: number; body: T | null; retryAfterS: number | null }
type Fetcher = (url: string, init: RequestInit) => Promise<Response>
let fetcher: Fetcher = (u, i) => fetch(u, i)
/** tests and the Playwright stubs replace the transport */
export function setSandboxFetcher(f: Fetcher | null): void {
  fetcher = f ?? ((u, i) => fetch(u, i))
}

export async function sbFetch<T>(method: string, path: string, body?: unknown, token?: string | null, signal?: AbortSignal, identity?: string | null): Promise<HttpReply<T>> {
  const headers: Record<string, string> = { accept: 'application/json', [CLIENT_HEADER]: CLIENT_NAME }
  if (body !== undefined) headers['content-type'] = 'application/json'
  if (token) headers.authorization = `Bearer ${token}`
  if (identity) headers['x-awr-identity'] = identity
  const r = await fetcher(apiUrl(`${SANDBOX_API}${path}`), { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal })
  let j: unknown = null
  try {
    j = r.status === 204 ? null : await r.json()
  } catch {
    j = null
  }
  const ra = Number(r.headers.get('retry-after'))
  return { status: r.status, body: j as T | null, retryAfterS: Number.isFinite(ra) && ra > 0 ? ra : null }
}

/** problem+json (AWR-17 §4.4) of a failed reply */
export function problemOf(r: HttpReply<unknown>): SandboxError {
  const p = (r.body ?? {}) as { code?: number; reason?: number; message?: string; detail?: Record<string, unknown> }
  const code = typeof p.code === 'number' ? p.code : typeof p.reason === 'number' ? p.reason : null
  return { code, status: r.status, message: typeof p.message === 'string' ? p.message : `${r.status}`, detail: p.detail ?? null }
}

const nsToMs = (ns: string | undefined | null): number => {
  if (!ns) return 0
  try {
    return Number(BigInt(ns) / 1_000_000n)
  } catch {
    return 0
  }
}

/** UI phase of a session state */
export function phaseOfState(state: WireState): SandboxPhase {
  switch (state) {
    case 'PARKING':
      return 'parking'
    case 'PARKED':
      return 'parked'
    case 'SPAWNING':
      return 'spawning'
    case 'READY':
      return 'ready'
    case 'ACTIVE':
    case 'IDLE':
      return 'active'
    case 'DRAINING':
      return 'draining'
    case 'ENDED':
      return 'ended'
    case 'FAILED':
      return 'failed'
  }
  return 'active'
}

/** remaining lifetime in seconds at wall time `now` */
export function remainingS(s: Pick<SandboxSessionView, 'session'>, now = Date.now()): number {
  const exp = nsToMs(s.session?.expires_unix_ns)
  return exp > 0 ? Math.max(0, (exp - now) / 1000) : Number.NaN
}

// ------------------------------------------------------------ proof of work (M17 net/sandbox/pow.ts: Web Worker, main-thread
// slices as the fallback; the M15 duplicate was removed in WP-12b)
type PowSolver = (c: SessionChallenge) => Promise<number | null>
let powSolver: PowSolver = async (c) => {
  try {
    return (await solvePow({ challenge_id: c.challenge_id, alg: 'sha256-lz', prefix_b64: c.prefix_b64, bits: c.bits, expires_unix_ns: c.expires_unix_ns })).nonce
  } catch {
    return null
  }
}
export function setPowSolver(f: PowSolver | null): void {
  powSolver = f ?? powSolver
}

// ------------------------------------------------------------ actions
export interface CreateOptions {
  calendar?: { start_utc: string; tz: string }
  configSnapshot?: Snapshot | null
  forceNew?: boolean
  queueOk?: boolean
  /** window size check (tests pass their own) */
  window?: { w: number; h: number }
}
export type CreateResult =
  | { kind: 'created'; sid: string }
  | { kind: 'queued' }
  | { kind: 'another-tab'; sid: string }
  | { kind: 'refused'; error: SandboxError }

export function windowLargeEnough(w = globalThis.innerWidth || 0, h = globalThis.innerHeight || 0): boolean {
  return w >= MIN_WINDOW.w && h >= MIN_WINDOW.h
}

function adopt(created: SessionCreated, template: string | null): string {
  const s = created.session
  const exp = nsToMs(created.token_exp_unix_ns) || nsToMs(s.expires_unix_ns)
  if (created.token) saveSession({ sid: s.sid, token: created.token, token_exp_unix_ms: exp, world: s.world_id, template: s.template_id ?? template })
  remove(store('session'), KEYS.queue)
  set({
    phase: phaseOfState(s.state), sid: s.sid, token: created.token ?? sandboxStore.getState().token, tokenExpUnixMs: exp, world: s.world_id,
    template: s.template_id ?? template, session: s, updatedAtMs: Date.now(), queue: null, error: null, verifying: false, anotherTab: false,
    endReason: null, expiring: null, parked: null, tier: (s as { tier?: 'public' | 'internal' }).tier ?? null,
  })
  return s.sid
}

let pollAbort: AbortController | null = null
const pageVisible = (): boolean => typeof document === 'undefined' || document.visibilityState !== 'hidden'

/** D2.1 internal identity: the token rides on POST /sessions as X-AWR-Identity (stores/identity.ts sets the provider) */
let identityOf: () => string | null = () => null
export function setSandboxIdentity(fn: (() => string | null) | null): void {
  identityOf = fn ?? (() => null)
}

/** the park fields of a session object (PARKED, AWR-17 §17.20.4) */
function parkedOf(s: Session): ParkedView | null {
  const x = s as Session & { parked_unix_ns?: string | null; park_reason?: string | null; resume_mode?: 'checkpoint' | 'snapshot' | null;
    retention_unix_ns?: string | null; t_sim_ns?: string | null; remaining_s?: number | null }
  if ((s.state as WireState) !== 'PARKED' && (s.state as WireState) !== 'PARKING') return null
  return {
    reason: x.park_reason ?? null, resumeMode: x.resume_mode ?? null, parkedAtMs: nsToMs(x.parked_unix_ns ?? null) || Date.now(),
    retentionUntilMs: nsToMs(x.retention_unix_ns ?? null) || Date.now() + PARK_RETENTION_MS, tSimNs: x.t_sim_ns ?? null, remainingS: x.remaining_s ?? null,
  }
}

/** keep the stored token resumable for the park retention */
function markStoredParked(untilMs: number): void {
  const st = sandboxStore.getState()
  if (!st.sid || !st.token) return
  saveSession({ sid: st.sid, token: st.token, token_exp_unix_ms: st.tokenExpUnixMs, world: st.world, template: st.template, parked_until_ms: untilMs })
}

async function pollQueue(): Promise<void> {
  pollAbort?.abort()
  const ac = new AbortController()
  pollAbort = ac
  while (!ac.signal.aborted) {
    const q = sandboxStore.getState().queue
    if (!q || (sandboxStore.getState().phase !== 'queued')) return
    let r: HttpReply<Ticket>
    try {
      r = await sbFetch<Ticket>('GET', `/queue/${encodeURIComponent(q.ticketId)}?wait_s=${QUEUE_WAIT_S}`, undefined, q.ticketToken, ac.signal)
    } catch {
      if (ac.signal.aborted) return
      await new Promise((res) => setTimeout(res, 2000))
      continue
    }
    if (ac.signal.aborted) return
    if (r.status === 410 || (r.body && r.body.state === 'expired')) {
      remove(store('session'), KEYS.queue)
      set({ phase: 'none', queue: null, error: { code: 511, status: 410, message: '511', detail: null } })
      return
    }
    if (r.status !== 200 || !r.body) {
      await new Promise((res) => setTimeout(res, 2000))
      continue
    }
    const t = r.body
    if (t.state === 'slot_ready') {
      const deadline = nsToMs(t.claim_deadline_unix_ns ?? null) || Date.now() + CLAIM_WINDOW_MS
      set({ phase: 'slot_ready', queue: { ...q, position: 0, etaS: 0, claimDeadlineMs: deadline } })
      // a resume ticket is claimed automatically while the page is visible (AWR-04 §15.7.3 QUEUED row)
      if (q.kind === 'resume' && pageVisible()) void sandbox.claim()
      return
    }
    set({ queue: { ...q, position: t.position, etaS: t.eta_s } })
  }
}

/**
 * GET /sessions/{sid} again while the page has no session object or the session is still SPAWNING (1 s, then every 2 s,
 * 429 and 5xx back off to 8 s; one chain at a time). D2-INT: after the world-switch reload the store stayed "spawning" with
 * the switch buttons disabled when the first GET failed or answered SPAWNING and no session topic followed.
 */
let refreshTimer: ReturnType<typeof setTimeout> | null = null
function refreshSessionLater(sid: string, delayMs = 1000): void {
  if (refreshTimer !== null) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    const s = sandboxStore.getState()
    if (s.sid !== sid || !s.token || (s.session && s.phase !== 'spawning')) return
    void sbFetch<Session>('GET', `/sessions/${sid}`, undefined, s.token).catch(() => null).then((r) => {
      const now = sandboxStore.getState()
      if (now.sid !== sid) return
      if (r && r.status === 200 && r.body) sandbox.applySession(r.body)
      const again = !now.session || sandboxStore.getState().phase === 'spawning'
      if (again && (!r || r.status === 200 || r.status === 429 || r.status >= 500)) {
        refreshSessionLater(sid, r && r.status === 200 ? 2000 : Math.min(Math.max(delayMs, 1000) * 2, 8000))
      }
    })
  }, delayMs)
}

export const sandbox = {
  /**
   * POST /sessions (AWR-17 §17.4.2): 201 or 200 with a token -> the session; 200 without a token -> another tab holds
   * it; 202 -> queue ticket and long poll; 409 509 -> solve the challenge in a worker and send again (at most 3 rounds);
   * other refusals (500, 504, 507, 508 with available_now) stay in the store for the Sheet.
   */
  async create(world: string, template: string | null, o: CreateOptions = {}): Promise<CreateResult> {
    const win = o.window ?? { w: globalThis.innerWidth || 0, h: globalThis.innerHeight || 0 }
    if (!windowLargeEnough(win.w, win.h)) {
      const error: SandboxError = { code: null, status: 0, message: 'small_window', detail: null }
      set({ error })
      return { kind: 'refused', error }
    }
    set({ phase: 'creating', error: null, anotherTab: false, verifying: false })
    const body: Record<string, unknown> = { world, template, principal_hint: principalHint(), client: { kind: 'browser', version: '0.2.0' } }
    if (o.calendar) body.calendar = o.calendar
    if (o.configSnapshot) {
      body.config_snapshot = o.configSnapshot
      body.template = null
    }
    if (o.forceNew) body.force_new = true
    if (o.queueOk) body.queue_ok = true
    for (let round = 0; round < 3; round++) {
      let r: HttpReply<SessionCreated & SessionQueued>
      try {
        r = await sbFetch<SessionCreated & SessionQueued>('POST', '/sessions', body, null, undefined, identityOf())
      } catch {
        const error: SandboxError = { code: null, status: 0, message: 'network', detail: null }
        set({ phase: 'none', error, verifying: false })
        return { kind: 'refused', error }
      }
      if ((r.status === 201 || r.status === 200) && r.body?.session) {
        if (!r.body.token) {
          const own = storedSession(r.body.session.sid)
          if (own) {
            adopt({ session: r.body.session, token: own.token, token_exp_unix_ns: String(own.token_exp_unix_ms * 1e6) }, template)
            return { kind: 'created', sid: r.body.session.sid }
          }
          set({ phase: 'none', anotherTab: true, verifying: false, error: null })
          return { kind: 'another-tab', sid: r.body.session.sid }
        }
        return { kind: 'created', sid: adopt(r.body, template) }
      }
      if (r.status === 202 && r.body?.queue) {
        const q = r.body.queue
        const exp = nsToMs(q.expires_unix_ns) || Date.now() + 3600_000
        writeJson(store('session'), KEYS.queue, { ticket_id: q.ticket_id, ticket_token: q.ticket_token, expires_unix_ms: exp, world, template } satisfies StoredQueue)
        set({ phase: 'queued', verifying: false, queue: { kind: 'new', ticketId: q.ticket_id, ticketToken: q.ticket_token, position: q.position, etaS: q.eta_s, claimDeadlineMs: null, world, template } })
        void pollQueue()
        return { kind: 'queued' }
      }
      const err = problemOf(r)
      if (err.code === 509) {
        const c = (err.detail?.challenge ?? null) as SessionChallenge | null
        if (!c) break
        set({ phase: 'challenge', verifying: true })
        const hint = setTimeout(() => set({ verifyHint: true }), VERIFY_HINT_MS)
        const nonce = await powSolver(c)
        clearTimeout(hint)
        set({ verifyHint: false })
        if (nonce === null) break
        body.pow = { challenge_id: c.challenge_id, nonce }
        continue
      }
      if (r.retryAfterS !== null) err.detail = { ...(err.detail ?? {}), retry_after_s: r.retryAfterS }
      set({ phase: 'none', error: err, verifying: false, retryUntilMs: r.retryAfterS !== null ? Date.now() + r.retryAfterS * 1000 : 0 })
      return { kind: 'refused', error: err }
    }
    const error: SandboxError = { code: 509, status: 409, message: 'challenge', detail: null }
    set({ phase: 'none', error, verifying: false })
    return { kind: 'refused', error }
  },

  /** POST /queue/{ticket}/claim within the claim window (201 -> the session) */
  async claim(): Promise<CreateResult> {
    const s = sandboxStore.getState()
    const q = s.queue
    if (!q || s.phase !== 'slot_ready') return { kind: 'refused', error: { code: 105, status: 409, message: 'NOT_READY', detail: null } }
    const r = await sbFetch<SessionCreated>('POST', `/queue/${encodeURIComponent(q.ticketId)}/claim`, {}, q.ticketToken).catch(() => null)
    if (r && r.status === 201 && r.body?.session) return { kind: 'created', sid: adopt(r.body, q.template) }
    const error = r ? problemOf(r) : { code: null, status: 0, message: 'network', detail: null }
    remove(store('session'), KEYS.queue)
    set({ phase: 'none', queue: null, error })
    return { kind: 'refused', error }
  },

  /** DELETE /queue/{ticket} (leave, or give up the reserved slot) */
  async leaveQueue(): Promise<void> {
    const q = sandboxStore.getState().queue
    pollAbort?.abort()
    remove(store('session'), KEYS.queue)
    set({ phase: q?.kind === 'resume' ? 'parked' : 'none', queue: null })
    if (q) await sbFetch('DELETE', `/queue/${encodeURIComponent(q.ticketId)}`, undefined, q.ticketToken).catch(() => null)
  },

  /** the claim window passed: forget the ticket locally (the server already voided it) */
  expireSlot(): void {
    remove(store('session'), KEYS.queue)
    set({ phase: 'none', queue: null, error: { code: 511, status: 410, message: 'claim_timeout', detail: null } })
  },

  /** a stored queue ticket of this tab: show the card again and continue the long poll */
  resumeQueue(): boolean {
    const q = storedQueue()
    if (!q || sandboxStore.getState().queue) return false
    set({ phase: 'queued', queue: { kind: q.kind ?? 'new', ticketId: q.ticket_id, ticketToken: q.ticket_token, position: 0, etaS: null, claimDeadlineMs: null, world: q.world, template: q.template } })
    void pollQueue()
    return true
  },

  /**
   * recover the session of a /sandbox/:sid route: the stored token (sessionStorage, then the localStorage backup of a
   * second tab) is adopted at once and checked with GET /sessions/{sid}; 401, 403 or 410 drop it (the "unavailable" page)
   */
  async resume(sid: string): Promise<boolean> {
    const cur = sandboxStore.getState()
    if (cur.sid === sid && cur.token && cur.phase !== 'ended' && cur.phase !== 'failed') return true
    const own = storedSession(sid)
    if (!own) {
      set({ ...INITIAL, version: cur.version, phase: 'none', endReason: 'gone' })
      return false
    }
    // a parked session whose token expired while parked: resumable through SB-70 only (GET would refuse the token)
    if (own.token_exp_unix_ms <= Date.now() && (own.parked_until_ms ?? 0) > Date.now()) {
      const now = Date.now()
      set({ phase: 'parked', sid, token: own.token, tokenExpUnixMs: own.token_exp_unix_ms, world: own.world, template: own.template, endReason: null, error: null,
        parked: { reason: null, resumeMode: null, parkedAtMs: now, retentionUntilMs: own.parked_until_ms ?? now + PARK_RETENTION_MS, tSimNs: null, remainingS: null } })
      return true
    }
    set({ phase: 'spawning', sid, token: own.token, tokenExpUnixMs: own.token_exp_unix_ms, world: own.world, template: own.template, endReason: null, error: null })
    const r = await sbFetch<Session>('GET', `/sessions/${sid}`, undefined, own.token).catch(() => null)
    if (r && r.status === 200 && r.body) {
      sandbox.applySession(r.body)
      if (sandboxStore.getState().phase === 'spawning') refreshSessionLater(sid, 2000)
      return sandboxStore.getState().phase !== 'ended'
    }
    if (r && (r.status === 401 || r.status === 403 || r.status === 404 || r.status === 410)) {
      clearSession()
      set({ phase: 'none', sid: null, token: null, session: null, endReason: 'gone' })
      return false
    }
    // offline, 429 or 5xx: keep the optimistic state and fetch the session again; without a session object the 1 Hz topic
    // cannot update the phase, which then stayed "spawning" after the world-switch reload (D2-INT)
    refreshSessionLater(sid)
    return true
  },

  /** merge a session object (REST) */
  applySession(s: Session): void {
    const cur = sandboxStore.getState()
    if (cur.sid && s.sid !== cur.sid) return
    const phase = phaseOfState(s.state)
    const ended = phase === 'ended' || phase === 'failed'
    const switching = cur.switching && (s.world_id !== cur.switching.world || phase === 'spawning') ? cur.switching : null
    // a parked session keeps its local "parked" or "resuming" phase until the resume answer or a live state arrives
    const keep = (cur.phase === 'resuming' || cur.phase === 'queued' || cur.phase === 'slot_ready') && (phase === 'parked' || phase === 'parking')
    const parked = phase === 'parked' || phase === 'parking' ? parkedOf(s) ?? cur.parked : null
    set({
      phase: keep ? cur.phase : phase, session: s, world: s.world_id, template: s.template_id ?? cur.template, updatedAtMs: Date.now(), switching,
      endReason: ended ? (s.end_reason ?? (phase === 'failed' ? 'failed' : 'user')) : null, parked,
      tier: (s as { tier?: 'public' | 'internal' }).tier ?? cur.tier,
    })
    if (ended) clearSession()
    if (parked) markStoredParked(parked.retentionUntilMs)
    // a world switch keeps sid and token: the stored copy follows the session's world, otherwise the reload after the switch
    // opens the old world first and reloads a second time (D2-INT)
    const own = !ended ? storedSession(s.sid) : null
    if (own && (own.world !== s.world_id || (s.template_id && own.template !== s.template_id))) {
      saveSession({ ...own, world: s.world_id, template: s.template_id ?? own.template })
    }
  },

  /** merge the 1 Hz `sandbox/session` topic (AWR-17 §17.7) */
  applyTopic(st: SandboxSessionState): void {
    const cur = sandboxStore.getState()
    if (st.sid !== cur.sid) return
    if (!cur.session) {
      refreshSessionLater(st.sid, 0)
      return
    }
    const session: Session = {
      ...cur.session, state: st.state, world_id: st.world_id ?? cur.session.world_id, template_id: st.template_id ?? cur.session.template_id,
      expires_unix_ns: st.expires_unix_ns, expires_in_s: st.expires_in_s, renewable: st.renewable, renew_block_reason: st.renew_block_reason ?? null,
      lifetime_max_unix_ns: st.max_expires_unix_ns ?? cur.session.lifetime_max_unix_ns, degrade_level: st.degrade_level,
      clock: { ...cur.session.clock, rate_granted: st.clock.rate_granted, rate_cap: st.clock.rate_cap, limited_by: st.clock.limited_by ?? null,
        rate_requested: st.clock.rate_requested ?? cur.session.clock.rate_requested },
      usage: { ...cur.session.usage, ...st.usage },
    }
    sandbox.applySession(session)
    if (st.expiring && !sandboxStore.getState().expiring) sandbox.noteExpiring(st.expiring.reason, st.expiring.in_s)
    // the age is relative to the server's publish time (no clock skew); a change below API_ACTIVITY_STEP_MS is the same activity
    const age = st.api_activity_age_s
    if (typeof age === 'number' && Number.isFinite(age) && age >= 0) {
      const at = Date.now() - age * 1000
      if (at - sandboxStore.getState().apiActivityMs >= API_ACTIVITY_STEP_MS) set({ apiActivityMs: at })
    }
  },

  /** `sandbox.expiring` (event or topic): 60 s countdown and one automatic snapshot export (AWR-14 §20.4.6 item 2) */
  noteExpiring(reason: string, inS: number): void {
    set({ expiring: { reason, atMs: Date.now() + Math.max(0, inS) * 1000 } })
    void sandbox.exportSnapshot().catch(() => null)
  },

  /** POST /sessions/{sid}/keepalive; the new token replaces the stored one (the realtime link uses it on reconnect) */
  async keepalive(): Promise<boolean> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return false
    const r = await sbFetch<SessionKeepaliveReply>('POST', `/sessions/${s.sid}/keepalive`, {}, s.token).catch(() => null)
    UX.sandbox.keepalives++
    if (!r || r.status !== 200 || !r.body) return false
    const b = r.body
    if (b.token) {
      const exp = nsToMs(b.token_exp_unix_ns) || nsToMs(b.session.expires_unix_ns)
      saveSession({ sid: s.sid, token: b.token, token_exp_unix_ms: exp, world: b.session.world_id, template: b.session.template_id ?? s.template })
      set({ token: b.token, tokenExpUnixMs: exp })
      tokenListeners.forEach((cb) => cb(b.token as string))
    }
    sandbox.applySession(b.session)
    if (b.renewed) set({ expiring: null })
    return b.renewed
  },

  /** DELETE /sessions/{sid} after saving the snapshot locally */
  async end(): Promise<void> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return
    await sandbox.exportSnapshot().catch(() => null)
    const r = await sbFetch('DELETE', `/sessions/${s.sid}`, undefined, s.token).catch(() => null)
    if (r && r.status < 300) set({ phase: 'draining', endReason: 'user' })
  },

  /** GET /sessions/{sid}/snapshot; kept in this browser for "restore last configuration" */
  async exportSnapshot(): Promise<Snapshot | null> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return null
    const r = await sbFetch<Snapshot>('GET', `/sessions/${s.sid}/snapshot`, undefined, s.token)
    if (r.status !== 200 || !r.body) return null
    saveLocalSnapshot(r.body)
    return r.body
  },

  /** POST /sessions/{sid}/restore (same world only, AWR-17 §17.4.5) */
  async restore(snapshot: Snapshot): Promise<SandboxError | null> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return { code: 506, status: 410, message: 'no_session', detail: null }
    if (s.world && snapshot.world_id !== s.world) return { code: 110, status: 422, message: 'WORLD_MISMATCH', detail: { world: snapshot.world_id } }
    const r = await sbFetch<Session>('POST', `/sessions/${s.sid}/restore`, { snapshot }, s.token).catch(() => null)
    return r && r.status < 300 ? null : r ? problemOf(r) : { code: null, status: 0, message: 'network', detail: null }
  },

  /** POST /sessions/{sid}/template (load or reset a scenario template) */
  async loadTemplate(template: string): Promise<SandboxError | null> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return { code: 506, status: 410, message: 'no_session', detail: null }
    const r = await sbFetch<Session>('POST', `/sessions/${s.sid}/template`, { template }, s.token).catch(() => null)
    if (r && r.status < 300) {
      set({ template })
      return null
    }
    return r ? problemOf(r) : { code: null, status: 0, message: 'network', detail: null }
  },

  /** POST /sessions/{sid}/world {world, template}: the sim restarts in the new world, sid and token stay (AWR-17 §17.4.6) */
  async switchWorld(world: string, template: string | null): Promise<SandboxError | null> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return { code: 506, status: 410, message: 'no_session', detail: null }
    const r = await sbFetch<{ session?: Session } & Session>('POST', `/sessions/${s.sid}/world`, { world, template: template ?? s.template }, s.token).catch(() => null)
    if (r && r.status < 300) {
      set({ switching: { world, template: template ?? s.template } })
      const sess = r.body?.session ?? (r.body?.sid ? (r.body as Session) : null)
      if (sess) sandbox.applySession(sess)
      refreshSessionLater(s.sid, 2000)
      return null
    }
    return r ? problemOf(r) : { code: null, status: 0, message: 'network', detail: null }
  },

  /** the realtime link reported the sandbox gone (close 4410): the session ended */
  gone(): void {
    const s = sandboxStore.getState()
    if (s.phase === 'ended' || s.phase === 'none') return
    clearSession()
    set({ phase: 'ended', endReason: s.endReason ?? (s.expiring?.reason as EndReason | undefined) ?? 'gone' })
  },

  // ---------------------------------------------------------- D2.1 power saving (SB-69, SB-70, SB-72; AWR-04 §15.7)
  /** POST /sessions/{sid}/park {reason}: 202 PARKING or 200 PARKED; the realtime link closes after it (PowerController) */
  async park(reason: 'idle' | 'hidden' | 'user' = 'idle'): Promise<boolean> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token || (s.phase !== 'active' && s.phase !== 'ready')) return false
    const now = Date.now()
    set({ phase: 'parking', parked: { reason, resumeMode: null, parkedAtMs: now, retentionUntilMs: now + PARK_RETENTION_MS, tSimNs: null, remainingS: null } })
    markStoredParked(now + PARK_RETENTION_MS)
    const r = await sbFetch<{ state?: string; session?: Session }>('POST', `/sessions/${s.sid}/park`, { reason }, s.token).catch(() => null)
    if (r && (r.status === 202 || r.status === 200)) {
      if (r.body?.session) sandbox.applySession(r.body.session)
      if (sandboxStore.getState().phase === 'parking') set({ phase: 'parked' })
      return true
    }
    if (r && r.status === 409 && problemOf(r).code === 513) {
      set({ phase: 'draining' })
      return false
    }
    // the server answered nothing usable: it parks the session itself when the link stays closed (no connection 120 s)
    set({ phase: 'parked' })
    return false
  },

  /** pagehide or Page Lifecycle freeze: SB-72 as a beacon (token in the body) */
  parkBeacon(reason: 'idle' | 'hidden' | 'user' = 'hidden'): boolean {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token || (s.phase !== 'active' && s.phase !== 'ready')) return false
    const nav = globalThis.navigator as Navigator | undefined
    if (!nav?.sendBeacon) return false
    const ok = nav.sendBeacon(apiUrl(`${SANDBOX_API}/sessions/${s.sid}/park-beacon`), new Blob([JSON.stringify({ token: s.token, reason })], { type: 'application/json' }))
    if (ok) {
      const now = Date.now()
      markStoredParked(now + PARK_RETENTION_MS)
      set({ phase: 'parked', parked: { reason, resumeMode: null, parkedAtMs: now, retentionUntilMs: now + PARK_RETENTION_MS, tSimNs: null, remainingS: null } })
    }
    return ok
  },

  /** the server parked the session first (WS close 4430 or REST 515): no request, only the local state */
  markParked(reason: string | null = 'idle'): void {
    const s = sandboxStore.getState()
    if (!s.sid || s.phase === 'parked' || s.phase === 'ended' || s.phase === 'none') return
    const now = Date.now()
    markStoredParked(now + PARK_RETENTION_MS)
    set({ phase: 'parked', parked: s.parked ?? { reason, resumeMode: null, parkedAtMs: now, retentionUntilMs: now + PARK_RETENTION_MS, tSimNs: null, remainingS: null } })
  },

  /**
   * POST /sessions/{sid}/resume (SB-70): 201 -> the sim-core restarts from the checkpoint with a fresh token; 200 -> already
   * active; 202 -> a resume ticket (same FIFO as new visitors, auto-claimed while visible); 410 -> the retention passed
   */
  async resumeParked(): Promise<'resumed' | 'active' | 'queued' | 'ended' | 'failed'> {
    const s = sandboxStore.getState()
    if (!s.sid || !s.token) return 'failed'
    set({ phase: 'resuming' })
    const r = await sbFetch<SessionCreated & { queue?: SessionQueued['queue'] }>('POST', `/sessions/${s.sid}/resume`, {}, s.token).catch(() => null)
    if (!r) {
      set({ phase: 'parked' })
      return 'failed'
    }
    if (r.status === 201 && r.body?.session) {
      adopt(r.body.token ? r.body : { ...r.body, token: s.token, token_exp_unix_ns: String(BigInt(Math.max(s.tokenExpUnixMs, Date.now() + 60_000)) * 1_000_000n) }, s.template)
      return 'resumed'
    }
    if (r.status === 200 && r.body?.session) {
      sandbox.applySession({ ...r.body.session, state: r.body.session.state === ('PARKED' as Session['state']) ? 'ACTIVE' : r.body.session.state })
      set({ parked: null })
      return 'active'
    }
    if (r.status === 202 && r.body?.queue) {
      const q = r.body.queue
      const exp = nsToMs(q.expires_unix_ns) || Date.now() + 3600_000
      writeJson(store('session'), KEYS.queue, { ticket_id: q.ticket_id, ticket_token: q.ticket_token, expires_unix_ms: exp, world: s.world ?? '', template: s.template, kind: 'resume', sid: s.sid } satisfies StoredQueue)
      set({ phase: 'queued', queue: { kind: 'resume', ticketId: q.ticket_id, ticketToken: q.ticket_token, position: q.position, etaS: q.eta_s, claimDeadlineMs: null, world: s.world ?? '', template: s.template } })
      void pollQueue()
      return 'queued'
    }
    if (r.status === 410 || r.status === 401 || r.status === 404) {
      clearSession()
      set({ phase: 'ended', endReason: 'parked_expired', parked: null })
      return 'ended'
    }
    set({ phase: 'parked', error: problemOf(r) })
    return 'failed'
  },

  /**
   * D2.1 P1 commander pairing (SB-77): adopt the sandbox token another device's pairing code gave this browser (same
   * principal, the seat is shared); the sandbox route then resumes it like a stored session
   */
  adopt(sid: string, token: string, tokenExpUnixMs: number, world: string | null): void {
    saveSession({ sid, token, token_exp_unix_ms: tokenExpUnixMs, world, template: null })
    set({ phase: 'spawning', sid, token, tokenExpUnixMs, world, template: null, endReason: null, error: null, parked: null })
  },

  /** back to the initial state (after the ended dialog) */
  reset(): void {
    pollAbort?.abort()
    const v = sandboxStore.getState().version
    sandboxStore.setState({ ...INITIAL, version: v + 1 })
    UX.sandbox.phase = 'none'
  },
  clearError(): void {
    set({ error: null, anotherTab: false })
  },
}

const tokenListeners = new Set<(token: string) => void>()
/** a renewed token (the realtime client takes it for its next reconnect) */
export function onSandboxToken(cb: (token: string) => void): () => void {
  tokenListeners.add(cb)
  return () => {
    tokenListeners.delete(cb)
  }
}

// ------------------------------------------------------------ automatic keepalive (one tab, page visible)
let renewTimer: (() => void) | null = null
export const RENEW_CHECK_MS = 5000
const RENEW_LOCK_MS = 30_000

/** whether the automatic keepalive is due now (pure; tests) */
export function renewDue(s: Pick<SandboxSessionView, 'phase' | 'session' | 'token'>, now = Date.now(), visible = true): boolean {
  if (!visible || !s.token || !s.session?.renewable) return false
  if (s.phase !== 'active' && s.phase !== 'ready') return false
  const left = remainingS(s, now)
  return Number.isFinite(left) && left <= AUTO_RENEW_S
}

function takeRenewLock(now: number): boolean {
  const ls = store('local')
  const held = readJson<{ at: number }>(ls, KEYS.renewLock)
  if (held && now - held.at < RENEW_LOCK_MS) return false
  writeJson(ls, KEYS.renewLock, { at: now })
  return true
}

export function installAutoRenew(enabled: () => boolean = () => true): () => void {
  if (renewTimer) return () => {}
  // frozen with the power-save mode: a parked or queued session renews nothing, the viewer has no session (D2-AC-44, D2-AC-46)
  renewTimer = freezableInterval(() => {
    const visible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
    const now = Date.now()
    if (!enabled() || !renewDue(sandboxStore.getState(), now, visible) || !takeRenewLock(now)) return
    void sandbox.keepalive()
  }, RENEW_CHECK_MS)
  return () => {
    renewTimer?.()
    renewTimer = null
  }
}

// ------------------------------------------------------------ realtime binding (topic, events, close 4410)
const SANDBOX_EVENTS = new Set(['sandbox.expiring', 'sandbox.rate', 'session.switched'])

export function onSandboxEvent(e: RtEvent): void {
  if (!SANDBOX_EVENTS.has(e.type)) return
  const d = e.data ?? {}
  if (e.type === 'sandbox.expiring') sandbox.noteExpiring(typeof d.reason === 'string' ? d.reason : 'lifetime', typeof d.in_s === 'number' ? d.in_s : 60)
  if (e.type === 'session.switched') {
    const s = sandboxStore.getState()
    if (s.switching && (d.reason === 'world' || typeof d.world === 'string')) set({ switching: null })
  }
}

/** attach the sandbox session to the page client while the sandbox surface is active; returns the detach function */
export function bindSandboxRt(rt: RtClient): () => void {
  const offTopic = rt.subscribe('sandbox/session', { rate: 1 })
  const offData = rt.onData((m) => {
    if (m.topic === 'sandbox/session' && m.data && typeof m.data === 'object') sandbox.applyTopic(m.data as SandboxSessionState)
  })
  const offEvents = rt.onEvents((batch) => {
    for (const e of batch) onSandboxEvent(e)
  })
  const offGone = rt.onSandboxGone(() => sandbox.gone())
  return () => {
    offTopic()
    offData()
    offEvents()
    offGone()
  }
}

/** the sandbox phases in which the session (and its sid route) is alive */
export const livePhase = (p: SandboxPhase): boolean => p === 'spawning' || p === 'ready' || p === 'active' || p === 'draining' || p === 'parking' || p === 'parked' || p === 'resuming'
