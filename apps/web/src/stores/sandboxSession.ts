// Sandbox session store (M17-FR-059, AC-022; M17 PRD §7.9; AWR-04 §4.6 "会话恢复与多标签页"; AWR-17 §17.4). Owner: M17.
// Phases: none -> creating -> (challenge) -> spawning -> ready -> active -> draining -> ended | failed, or
// creating -> queued -> slot_ready -> (claim) -> spawning. The store owns the session credentials:
//   * {sid, token, token_exp_unix_ms, world, template} in sessionStorage and a localStorage backup (key
//     awr.sandbox.session.v1, AWR-14 §20.4.7; the D2 key awr.sandbox.v1 is still read), dropped when expired, so a refresh
//     resumes the same sid and a second tab of the same browser joins the same sandbox (resume()); the queue ticket in
//     sessionStorage (awr.sandbox.queue.v1) so a refresh keeps the place in the queue (restoreQueue());
//   * BroadcastChannel awr.sandbox shares a renewed token and the end of the session between tabs; only one tab renews
//     (a 60 s lease in localStorage, awr.sandbox.ka);
//   * auto keepalive: remaining lifetime <= 300 s, page visible and a user input within the last 10 min;
//   * on sandbox.expiring (action end) the configuration snapshot is exported to localStorage (awr.sandbox.snapshot.v1);
//   * proof of work (409 509) is solved in a Web Worker (net/sandbox/pow.ts) and the create request retried; the
//     "verifying" hint only shows when solving takes longer than 300 ms (verifyHint);
//   * 429 and 504 keep the Retry-After as retryUntilMs; an idempotent 200 without a token and nothing in storage means
//     "your sandbox runs in another tab" (anotherTab; force_new opens a second one).
// D2.1 power saving (AWR-04 §15.7; ADR-121; M17-FR-079): park(reason) (SB-69) and parkBeacon() (SB-72, pagehide/freeze)
// move the session to PARKING -> PARKED (the server saves a checkpoint, stops the sim-core and releases the slot);
// resume() (SB-70) brings it back (201: fresh token; 202: a resume ticket in the same FIFO as new visitors, claimed
// automatically while the page is visible); onPowerState(state) is the interface for M15's PowerController; WS close 4430
// lands in markSandboxParked(). A parked session keeps its stored token up to the park retention (the server accepts an
// expired token on resume). Windows narrower than 360 CSS px never create a session (ADR-122; D2's 1280 x 720 guard is
// revised: phones and tablets may open a sandbox).
// While a session is held the net endpoint is the sandbox (setEndpoint): REST under /api/sandbox/v1/sessions/{sid} and the
// realtime URL .../rt use the token kept here (net never imports stores; the endpoint calls back into the store).
// Actions are plain functions next to the store (STORE-01); all writes go through store.setState.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { ApiError } from '@/net/api'
import { rtUrl, setEndpoint } from '@/net/endpoint'
import * as api from '@/net/sandbox/client'
import type { Challenge, CreateResult, ParkReason, QueueInfo, SandboxSessionView } from '@/net/sandbox/client'
import { solvePow, type PowSolution } from '@/net/sandbox/pow'

export type Phase = 'none' | 'creating' | 'challenge' | 'queued' | 'slot_ready' | 'spawning' | 'ready' | 'active' | 'parking'
  | 'parked' | 'resuming' | 'draining' | 'ended' | 'failed'
export type PowerState = 'active' | 'quiet' | 'warning' | 'saving' | 'resuming' | 'queued'

export interface SandboxError { status: number; code: number | null; message: string; remedy: string | null; detail: unknown }
export interface QueueState {
  ticketId: string; position: number; etaS: number | null; offerExpiresInS?: number; claimDeadlineUnixNs?: string | null
  /** new visitor, resume of a parked session, or internal identity (AWR-04 §15.6.4) */
  kind: 'new' | 'resume' | 'internal'
  ticketToken?: string
  world?: string
  template?: string | null
}
export interface ParkedInfo {
  reason: string | null
  resumeMode: 'checkpoint' | 'snapshot' | null
  parkedUnixNs: string | null
  retentionUnixNs: string | null
  remainingS: number | null
  tSimNs: string | null
}
export interface SandboxSessionState {
  phase: Phase
  /** wall-clock ms when the current phase began ("starting slowly" after 20 s of spawning, AWR-14 §20.4) */
  phaseSinceMs?: number
  sid?: string
  token?: string
  expiresUnixNs?: string
  maxExpiresUnixNs?: string
  world?: string
  template?: string | null
  limits?: SandboxSessionView['limits']
  grantedRate?: number
  rateCap?: number
  limitedBy?: string | null
  renewable?: boolean
  renewBlockReason?: string | null
  epoch?: number
  tier?: 'public' | 'internal'
  queue?: QueueState
  expiring?: { reason: string; inS: number; action?: 'end' | 'park' }
  parked?: ParkedInfo
  endReason?: string | null
  availableNow?: string[]
  error?: SandboxError
  /** 200 without a token and nothing in storage: the sandbox runs in another tab of this browser */
  anotherTab?: boolean
  /** proof of work has been solving for more than 300 ms */
  verifyHint?: boolean
  /** Retry-After of a 429 or 504 as a wall-clock deadline */
  retryUntilMs?: number
  /** a world switch is in progress (loading layer, session bar locked) */
  switching?: { world: string; template: string | null } | null
}

export const STORAGE_KEY = 'awr.sandbox.session.v1'
export const LEGACY_STORAGE_KEY = 'awr.sandbox.v1'
export const QUEUE_KEY = 'awr.sandbox.queue.v1'
export const SNAPSHOT_KEY = 'awr.sandbox.snapshot.v1'
export const HINT_KEY = 'awr.sandbox.hint'
export const KA_LOCK_KEY = 'awr.sandbox.ka'
export const CHANNEL = 'awr.sandbox'
export const MIN_VIEWPORT = { w: 360, h: 0 }
export const KEEPALIVE_MARGIN_S = 300
export const INPUT_RECENT_MS = 10 * 60_000
export const VERIFY_HINT_MS = 300
export const PARK_RETENTION_S = 3600
const KA_LOCK_MS = 60_000
const KA_TICK_MS = 15_000
const POW_ATTEMPTS = 3
const PARK_WAIT_POLLS = 24
const RESUME_BUSY_RETRIES = 10

export const sandboxSessionStore = createAwrStore<SandboxSessionState>('sandboxSession', () => ({ phase: 'none' }))

export function useSandboxSession<T>(sel: (s: SandboxSessionState) => T): T {
  return useStore(sandboxSessionStore, sel)
}

// ------------------------------------------------------------ environment (injectable for tests)
interface Deps {
  session: Storage | null
  local: Storage | null
  channel: BroadcastChannel | null
  now: () => number
  viewport: () => { w: number; h: number }
  visible: () => boolean
  solve: (ch: Challenge, signal?: AbortSignal) => Promise<PowSolution>
  sleep: (ms: number) => Promise<void>
  tabId: string
}

function store(kind: 'sessionStorage' | 'localStorage'): Storage | null {
  try {
    return (globalThis as unknown as Record<string, Storage | undefined>)[kind] ?? null
  } catch {
    return null
  }
}

function defaultDeps(): Deps {
  let channel: BroadcastChannel | null = null
  try {
    channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel(CHANNEL) : null
  } catch {
    channel = null
  }
  return {
    session: store('sessionStorage'), local: store('localStorage'), channel, now: () => Date.now(),
    viewport: () => ({ w: globalThis.innerWidth ?? 0, h: globalThis.innerHeight ?? 0 }),
    visible: () => (typeof document === 'undefined' ? true : document.visibilityState !== 'hidden'),
    solve: (ch, signal) => solvePow(ch, { signal }),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    tabId: Math.random().toString(36).slice(2),
  }
}

let deps: Deps | null = null
let lastInputMs = 0
let kaTimer: ReturnType<typeof setInterval> | null = null
let loopGen = 0

function D(): Deps {
  if (!deps) {
    deps = defaultDeps()
    attachChannel(deps)
  }
  return deps
}

/** test hook: replace the environment (storage, channel, clock, viewport, solver); resets the store */
export function configureSandboxSession(over: Partial<Deps>): void {
  const base = deps ?? defaultDeps()
  deps = { ...base, ...over }
  attachChannel(deps)
  loopGen++
  stopAutoKeepalive()
  queueToken = null
  sandboxSessionStore.setState({ phase: 'none' }, true)
}

function attachChannel(d: Deps): void {
  if (!d.channel) return
  d.channel.onmessage = (ev: MessageEvent) => onChannel(ev.data as ChannelMsg)
}

type ChannelMsg = { type: 'token'; sid: string; token: string; expiresUnixNs: string } | { type: 'ended'; sid: string }

function post(m: ChannelMsg): void {
  try {
    D().channel?.postMessage(m)
  } catch {
    // channel closed
  }
}

function onChannel(m: ChannelMsg): void {
  const s = sandboxSessionStore.getState()
  if (!m || m.sid !== s.sid) return
  if (m.type === 'token') {
    set({ token: m.token, expiresUnixNs: m.expiresUnixNs })
    persist()
  } else if (m.type === 'ended') {
    clearPersisted()
    set({ phase: 'ended', token: undefined })
    setEndpoint({ kind: 'shared' })
  }
}

function set(p: Partial<SandboxSessionState>): void {
  if (p.phase !== undefined && p.phase !== sandboxSessionStore.getState().phase) p = { ...p, phaseSinceMs: D().now() }
  sandboxSessionStore.setState(p)
}

// ------------------------------------------------------------ persistence (AWR-14 §20.4.7)
interface Persisted {
  sid: string; token: string; token_exp_unix_ms?: number; expires_unix_ns?: string; world?: string; template?: string | null
  /** parked: the token stays usable for resume until the end of the park retention */
  parked_until_ms?: number
}
interface PersistedQueue {
  ticket_id: string; ticket_token: string; expires_unix_ms: number; kind?: QueueState['kind']; sid?: string; world?: string
  template?: string | null
}

function nowNs(): bigint {
  return BigInt(Math.floor(D().now())) * 1_000_000n
}

function nsToMs(v: string | null | undefined): number | null {
  if (!v || !/^\d+$/.test(v)) return null
  return Number(BigInt(v) / 1_000_000n)
}

function persist(): void {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token || !s.expiresUnixNs) return
  const rec: Persisted = { sid: s.sid, token: s.token, token_exp_unix_ms: nsToMs(s.expiresUnixNs) ?? 0,
    expires_unix_ns: s.expiresUnixNs, world: s.world, template: s.template ?? null }
  if (s.phase === 'parked' || s.phase === 'parking') {
    rec.parked_until_ms = nsToMs(s.parked?.retentionUnixNs) ?? D().now() + PARK_RETENTION_S * 1000
  }
  const v = JSON.stringify(rec)
  for (const st of [D().session, D().local]) {
    try {
      st?.setItem(STORAGE_KEY, v)
    } catch {
      // quota or disabled storage: the session still works in this tab
    }
  }
}

function readPersisted(): Persisted | null {
  for (const st of [D().session, D().local]) {
    for (const key of [STORAGE_KEY, LEGACY_STORAGE_KEY]) {
      try {
        const raw = st?.getItem(key)
        if (!raw) continue
        const p = JSON.parse(raw) as Persisted
        if (!p?.sid || !p?.token) continue
        const expMs = p.token_exp_unix_ms ?? nsToMs(p.expires_unix_ns)
        if (expMs === null || expMs === undefined) continue
        const keep = expMs > D().now() || (p.parked_until_ms !== undefined && p.parked_until_ms > D().now())
        if (!keep) {
          st?.removeItem(key)
          continue
        }
        return { ...p, expires_unix_ns: p.expires_unix_ns ?? String(BigInt(expMs) * 1_000_000n) }
      } catch {
        continue
      }
    }
  }
  return null
}

function clearPersisted(): void {
  for (const st of [D().session, D().local]) {
    for (const key of [STORAGE_KEY, LEGACY_STORAGE_KEY]) {
      try {
        st?.removeItem(key)
      } catch {
        // ignore
      }
    }
  }
}

function persistQueue(q: QueueInfo, kind: QueueState['kind'], world?: string, template?: string | null): void {
  const rec: PersistedQueue = { ticket_id: q.ticket_id, ticket_token: q.ticket_token,
    expires_unix_ms: nsToMs(q.expires_unix_ns) ?? D().now() + 120_000, kind, sid: q.sid, world, template }
  try {
    D().session?.setItem(QUEUE_KEY, JSON.stringify(rec))
  } catch {
    // the queue still works in this tab
  }
}

function clearQueue(): void {
  queueToken = null
  try {
    D().session?.removeItem(QUEUE_KEY)
  } catch {
    // ignore
  }
}

export function principalHint(): string {
  const l = D().local
  try {
    const h = l?.getItem(HINT_KEY)
    if (h && /^[0-9a-f]{32}$/.test(h)) return h
  } catch {
    // fall through
  }
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  const h = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  try {
    l?.setItem(HINT_KEY, h)
  } catch {
    // private mode: a new hint next time
  }
  return h
}

// ------------------------------------------------------------ session view -> state
function phaseOf(st: SandboxSessionView['state']): Phase {
  switch (st) {
    case 'SPAWNING': return 'spawning'
    case 'READY': return 'ready'
    case 'ACTIVE':
    case 'IDLE': return 'active'
    case 'PARKING': return 'parking'
    case 'PARKED': return 'parked'
    case 'DRAINING': return 'draining'
    case 'ENDED': return 'ended'
    default: return 'failed'
  }
}

function applyView(v: SandboxSessionView): void {
  const parked = v.state === 'PARKING' || v.state === 'PARKED'
  set({
    phase: phaseOf(v.state), sid: v.sid, world: v.world_id, template: v.template_id, expiresUnixNs: v.expires_unix_ns,
    maxExpiresUnixNs: v.lifetime_max_unix_ns, limits: v.limits, grantedRate: v.clock?.rate_granted, rateCap: v.clock?.rate_cap,
    limitedBy: v.clock?.limited_by ?? null, renewable: v.renewable, renewBlockReason: v.renew_block_reason ?? null,
    epoch: v.epoch, endReason: v.end_reason ?? null, tier: v.tier ?? sandboxSessionStore.getState().tier ?? 'public',
    parked: parked ? { reason: v.park_reason ?? null, resumeMode: v.resume_mode ?? null, parkedUnixNs: v.parked_unix_ns ?? null,
      retentionUnixNs: v.retention_unix_ns ?? null, remainingS: v.remaining_s ?? null, tSimNs: v.t_sim_ns ?? null } : undefined,
  })
}

function errorOf(e: unknown): SandboxError {
  if (e instanceof ApiError) return { status: e.status, code: e.reason, message: e.message, remedy: e.remedy, detail: e.detail }
  return { status: 0, code: null, message: e instanceof Error ? e.message : String(e), remedy: null, detail: null }
}

function retryUntil(e: unknown): number | undefined {
  const ra = (e as { retryAfterS?: number } | null)?.retryAfterS
  if (!(e instanceof ApiError) || !(e.status === 429 || e.reason === 504) || ra === undefined) return undefined
  return D().now() + ra * 1000
}

function holdEndpoint(): void {
  const s = sandboxSessionStore.getState()
  if (!s.sid) return
  setEndpoint({ kind: 'sandbox', sid: s.sid, token: () => sandboxSessionStore.getState().token ?? '', onAuthExpired: refreshToken })
}

async function refreshToken(): Promise<string | null> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return null
  try {
    const r = await api.keepalive(s.sid, s.token)
    if (r.token) adoptToken(r.token, r.session.expires_unix_ns)
    return sandboxSessionStore.getState().token ?? null
  } catch (e) {
    if (e instanceof ApiError && e.reason === 515) {
      markSandboxParked()
      return null
    }
    if (e instanceof ApiError && (e.status === 410 || e.status === 401 || e.status === 403)) markSandboxGone(e.reason)
    return null
  }
}

function adoptToken(token: string, expiresUnixNs: string): void {
  const s = sandboxSessionStore.getState()
  set({ token, expiresUnixNs })
  persist()
  if (s.sid) post({ type: 'token', sid: s.sid, token, expiresUnixNs })
}

// ------------------------------------------------------------ actions
/** Viewport wide enough for any sandbox layout (ADR-122: < 360 CSS px shows "window too narrow"; phones and tablets may open). */
export function canCreateSandbox(): boolean {
  const v = D().viewport()
  return v.w >= MIN_VIEWPORT.w && v.h >= MIN_VIEWPORT.h
}

export async function createSandbox(world: string, template?: string | null, extra: Partial<api.CreateBody> = {}): Promise<void> {
  if (!canCreateSandbox()) {
    set({ error: { status: 0, code: null, message: 'viewport', remedy: null, detail: MIN_VIEWPORT } })
    return
  }
  const cur = sandboxSessionStore.getState().phase
  if (cur !== 'none' && cur !== 'ended' && cur !== 'failed') return
  const gen = ++loopGen
  set({ phase: 'creating', error: undefined, availableNow: undefined, queue: undefined, world, template: template ?? null,
    anotherTab: false, verifyHint: false, retryUntilMs: undefined })
  const body: api.CreateBody = { world, principal_hint: principalHint(), client: { kind: 'browser' }, ...extra }
  if (template) body.template = template
  let res: CreateResult | null = null
  for (let attempt = 0; attempt <= POW_ATTEMPTS && res === null; attempt++) {
    try {
      res = await api.createSession(body)
    } catch (e) {
      const ch = e instanceof ApiError && e.reason === 509 ? (e.detail as { challenge?: Challenge } | null)?.challenge : undefined
      if (ch && attempt < POW_ATTEMPTS) {
        set({ phase: 'challenge' })
        const hint = setTimeout(() => {
          if (gen === loopGen && sandboxSessionStore.getState().phase === 'challenge') set({ verifyHint: true })
        }, VERIFY_HINT_MS)
        try {
          body.pow = await D().solve(ch)
        } catch (pe) {
          set({ phase: 'none', error: errorOf(pe), verifyHint: false })
          return
        } finally {
          clearTimeout(hint)
        }
        if (gen !== loopGen) return
        set({ phase: 'creating', verifyHint: false })
        continue
      }
      const err = errorOf(e)
      const det = err.detail as { available_now?: string[] } | null
      set({ phase: 'none', error: err, availableNow: det?.available_now, retryUntilMs: retryUntil(e), verifyHint: false })
      return
    }
  }
  if (res === null || gen !== loopGen) return
  await afterCreate(res, gen)
}

async function afterCreate(res: CreateResult, gen: number, kind: QueueState['kind'] = 'new'): Promise<void> {
  if (res.kind === 'queued') {
    const s = sandboxSessionStore.getState()
    const qk = res.queue.kind ?? kind
    set({ phase: 'queued', queue: { ticketId: res.queue.ticket_id, position: res.queue.position, etaS: res.queue.eta_s, kind: qk,
      ticketToken: res.queue.ticket_token, world: s.world, template: s.template ?? null }, availableNow: res.availableNow })
    queueToken = res.queue.ticket_token
    persistQueue(res.queue, qk, s.world, s.template)
    void queueLoop(gen)
    return
  }
  if (!res.token) {
    // idempotent: the server does not re-issue the token (principal_hint is not a credential); resume from storage
    const ok = await resumeSandbox()
    if (!ok) {
      set({ phase: 'none', anotherTab: true, error: { status: 200, code: null, message: 'session_exists', remedy: null, detail: res.session } })
    }
    return
  }
  applyView(res.session)
  set({ token: res.token, expiresUnixNs: res.tokenExpUnixNs ?? res.session.expires_unix_ns, anotherTab: false })
  persist()
  holdEndpoint()
  await waitReady(gen)
}

let queueToken: string | null = null

async function queueLoop(gen: number): Promise<void> {
  while (gen === loopGen) {
    const s = sandboxSessionStore.getState()
    if (s.phase !== 'queued' || !s.queue || !queueToken) return
    try {
      const v = await api.pollTicket(s.queue.ticketId, queueToken, 25)
      if (gen !== loopGen) return
      if (v.state === 'slot_ready') {
        const dl = v.claim_deadline_unix_ns ? Number(BigInt(v.claim_deadline_unix_ns) / 1_000_000n) : D().now() + 60_000
        set({ phase: 'slot_ready', queue: { ...s.queue, position: 0, etaS: 0, claimDeadlineUnixNs: v.claim_deadline_unix_ns,
          offerExpiresInS: Math.max(0, Math.round((dl - D().now()) / 1000)) } })
        // resuming a parked sandbox claims by itself while the page is visible (AWR-04 §15.7.3, QUEUED row)
        if (s.queue.kind === 'resume' && D().visible()) void claimSlot()
        return
      }
      if (v.state === 'expired' || v.state === 'claimed') {
        clearQueue()
        const parked = s.queue.kind === 'resume'
        set({ phase: parked ? 'parked' : 'none', queue: undefined,
          error: { status: 410, code: 511, message: 'ticket_expired', remedy: null, detail: v } })
        return
      }
      set({ queue: { ...s.queue, position: v.position, etaS: v.eta_s } })
    } catch (e) {
      if (gen !== loopGen) return
      if (e instanceof ApiError && (e.status === 410 || e.status === 401)) {
        const parked = s.queue.kind === 'resume'
        clearQueue()
        set({ phase: parked ? 'parked' : 'none', queue: undefined, error: errorOf(e) })
        return
      }
      await D().sleep(2000)
    }
  }
}

/** After a reload: pick up the queue ticket kept in sessionStorage (AWR-14 §20.4.7). */
export function restoreQueue(): boolean {
  let q: PersistedQueue | null = null
  try {
    const raw = D().session?.getItem(QUEUE_KEY)
    q = raw ? (JSON.parse(raw) as PersistedQueue) : null
  } catch {
    q = null
  }
  if (!q?.ticket_id || !q.ticket_token || q.expires_unix_ms <= D().now()) {
    clearQueue()
    return false
  }
  const gen = ++loopGen
  queueToken = q.ticket_token
  set({ phase: 'queued', sid: q.sid ?? sandboxSessionStore.getState().sid, world: q.world, template: q.template ?? null,
    queue: { ticketId: q.ticket_id, position: 0, etaS: null, kind: q.kind ?? 'new', ticketToken: q.ticket_token, world: q.world,
      template: q.template ?? null } })
  void queueLoop(gen)
  return true
}

export async function claimSlot(): Promise<void> {
  const s = sandboxSessionStore.getState()
  if (s.phase !== 'slot_ready' || !s.queue || !queueToken) return
  const gen = ++loopGen
  const kind = s.queue.kind
  set({ phase: kind === 'resume' ? 'resuming' : 'creating' })
  try {
    const res = await api.claimTicket(s.queue.ticketId, queueToken, principalHint())
    clearQueue()
    set({ queue: undefined })
    await afterCreate(res, gen, kind)
  } catch (e) {
    clearQueue()
    set({ phase: kind === 'resume' ? 'parked' : 'none', queue: undefined, error: errorOf(e) })
  }
}

export async function leaveQueue(): Promise<void> {
  const s = sandboxSessionStore.getState()
  loopGen++
  if (s.queue && queueToken) {
    try {
      await api.leaveTicket(s.queue.ticketId, queueToken)
    } catch {
      // the ticket may already be gone
    }
  }
  const parked = s.queue?.kind === 'resume'
  clearQueue()
  set({ phase: parked ? 'parked' : 'none', queue: undefined })
}

async function waitReady(gen: number): Promise<void> {
  for (let i = 0; i < 6 && gen === loopGen; i++) {
    const s = sandboxSessionStore.getState()
    if (!s.sid || !s.token) return
    try {
      const v = await api.getSession(s.sid, s.token, { state: 'READY', waitS: 10 })
      if (gen !== loopGen) return
      applyView(v)
      if (v.state !== 'SPAWNING') {
        startAutoKeepalive()
        return
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        await D().sleep(1000)
        continue
      }
      set({ phase: e instanceof ApiError && e.status === 410 ? 'ended' : 'failed', error: errorOf(e) })
      clearPersisted()
      return
    }
  }
}

export async function keepaliveSandbox(): Promise<boolean> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return false
  try {
    const r = await api.keepalive(s.sid, s.token)
    applyView(r.session)
    if (r.renewed && r.token) adoptToken(r.token, r.token_exp_unix_ns ?? r.session.expires_unix_ns)
    return r.renewed
  } catch (e) {
    if (e instanceof ApiError && e.reason === 515) markSandboxParked()
    else if (e instanceof ApiError && (e.status === 410 || e.status === 401)) markSandboxGone(e.reason)
    else set({ error: errorOf(e) })
    return false
  }
}

export async function endSandbox(): Promise<void> {
  const s = sandboxSessionStore.getState()
  loopGen++
  stopAutoKeepalive()
  if (s.sid && s.token) {
    try {
      await api.endSession(s.sid, s.token)
    } catch {
      // already ended
    }
    post({ type: 'ended', sid: s.sid })
  }
  clearPersisted()
  clearQueue()
  set({ phase: 'ended', token: undefined })
  setEndpoint({ kind: 'shared' })
}

export async function switchSandboxWorld(world: string, template?: string): Promise<void> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return
  const gen = ++loopGen
  set({ switching: { world, template: template ?? null } })
  try {
    applyView(await api.switchWorld(s.sid, s.token, world, template))
  } catch (e) {
    set({ error: errorOf(e), availableNow: ((e as ApiError).detail as { available_now?: string[] } | null)?.available_now, switching: null })
    return
  }
  await waitReady(gen)
  set({ switching: null })
}

export async function loadSandboxTemplate(template: string): Promise<void> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return
  try {
    applyView(await api.loadTemplate(s.sid, s.token, template))
  } catch (e) {
    set({ error: errorOf(e) })
  }
}

export async function exportSandboxSnapshot(save = true): Promise<Record<string, unknown> | null> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return null
  try {
    const snap = await api.exportSnapshot(s.sid, s.token)
    if (save) {
      try {
        D().local?.setItem(SNAPSHOT_KEY, JSON.stringify({ sid: s.sid, world: s.world, saved_ms: D().now(), snapshot: snap }))
      } catch {
        // quota: the caller still gets the snapshot
      }
    }
    return snap
  } catch (e) {
    set({ error: errorOf(e) })
    return null
  }
}

export function savedSnapshot(): { sid: string; world: string; saved_ms: number; snapshot: Record<string, unknown> } | null {
  try {
    const raw = D().local?.getItem(SNAPSHOT_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export async function restoreSandbox(snapshot: Record<string, unknown>): Promise<void> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return
  try {
    applyView(await api.restoreSnapshot(s.sid, s.token, snapshot))
  } catch (e) {
    set({ error: errorOf(e) })
  }
}

/** Resume after a refresh or in a second tab: validate the stored {sid, token} with GET /sessions/{sid}. A parked session
 *  whose token expired while parked is resumed through SB-70 (the server accepts it within the park retention). */
export async function resumeSandbox(): Promise<boolean> {
  const p = readPersisted()
  if (!p || !p.expires_unix_ns) return false
  try {
    const v = await api.getSession(p.sid, p.token)
    applyView(v)
    set({ token: p.token, expiresUnixNs: v.expires_unix_ns })
    persist()
    holdEndpoint()
    if (v.state === 'SPAWNING') await waitReady(++loopGen)
    else if (v.state !== 'PARKED' && v.state !== 'PARKING') startAutoKeepalive()
    return true
  } catch (e) {
    if (e instanceof ApiError && e.status === 401 && p.parked_until_ms !== undefined && p.parked_until_ms > D().now()) {
      set({ phase: 'parked', sid: p.sid, token: p.token, expiresUnixNs: p.expires_unix_ns, world: p.world,
        template: p.template ?? null })
      holdEndpoint()
      return true
    }
    if (e instanceof ApiError && [401, 403, 404, 410].includes(e.status)) clearPersisted()
    set({ phase: 'none', error: errorOf(e) })
    return false
  }
}

export function sandboxRtEndpoint(): string | null {
  const s = sandboxSessionStore.getState()
  return s.sid && s.token ? rtUrl() : null
}

/** WS close 4410 or 410 from REST: the session is gone (AWR-17 §17.9.3); clear credentials, back to the shared show. */
export function markSandboxGone(code: number | null = null): void {
  const s = sandboxSessionStore.getState()
  loopGen++
  stopAutoKeepalive()
  clearPersisted()
  clearQueue()
  if (s.sid) post({ type: 'ended', sid: s.sid })
  set({ phase: 'ended', token: undefined, endReason: s.endReason ?? (code === 510 ? 'preempted' : null) })
  setEndpoint({ kind: 'shared' })
}

// ------------------------------------------------------------ D2.1 power saving: park and resume
/** SB-69: save and pause the sandbox, release the slot (PARKING -> PARKED). The caller closes the realtime connection. */
export async function parkSandbox(reason: ParkReason = 'user'): Promise<boolean> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token || !(s.phase === 'ready' || s.phase === 'active')) return false
  const gen = ++loopGen
  stopAutoKeepalive()
  set({ phase: 'parking', expiring: undefined })
  try {
    const r = await api.parkSession(s.sid, s.token, reason)
    if (r.session) applyView(r.session)
  } catch (e) {
    if (e instanceof ApiError && (e.status === 410 || e.status === 401)) markSandboxGone(e.reason)
    else set({ phase: 'active', error: errorOf(e) })
    return false
  }
  for (let i = 0; i < PARK_WAIT_POLLS && gen === loopGen; i++) {
    const cur = sandboxSessionStore.getState()
    if (cur.phase === 'parked' || !cur.sid || !cur.token) break
    try {
      const v = await api.getSession(cur.sid, cur.token)
      if (gen !== loopGen) break
      applyView(v)
      if (v.state === 'PARKED') break
    } catch {
      break
    }
    await D().sleep(250)
  }
  if (sandboxSessionStore.getState().phase === 'parking') set({ phase: 'parked' })
  persist()
  return true
}

/** SB-72 from pagehide or freeze: fire and forget (sendBeacon); the server parks, the next visit resumes. */
export function parkSandboxBeacon(reason: ParkReason = 'hidden'): boolean {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token || !(s.phase === 'ready' || s.phase === 'active')) return false
  stopAutoKeepalive()
  const sent = api.beaconPark(s.sid, s.token, reason)
  if (sent) {
    set({ phase: 'parking' })
    persist()
  }
  return sent
}

/** SB-70: bring a parked sandbox back. 201 adopts the fresh token and waits for READY; 202 queues with a resume ticket
 *  (claimed automatically while visible); 410 means the park retention ran out (the session ended, snapshot kept). */
export async function resumeParked(): Promise<'resumed' | 'active' | 'queued' | 'ended' | 'failed'> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return 'failed'
  if (s.phase === 'queued' || s.phase === 'slot_ready') return 'queued'
  const gen = ++loopGen
  set({ phase: 'resuming', error: undefined })
  for (let attempt = 0; attempt <= RESUME_BUSY_RETRIES && gen === loopGen; attempt++) {
    const cur = sandboxSessionStore.getState()
    try {
      const r = await api.resumeSession(cur.sid ?? s.sid, cur.token ?? s.token)
      if (gen !== loopGen) return 'failed'
      if (r.kind === 'queued') {
        await afterCreate({ kind: 'queued', queue: r.queue, availableNow: [] }, gen, 'resume')
        return 'queued'
      }
      if (r.kind === 'active') {
        applyView(r.session)
        holdEndpoint()
        startAutoKeepalive()
        return 'active'
      }
      applyView(r.session)
      if (r.token) adoptToken(r.token, r.tokenExpUnixNs ?? r.session.expires_unix_ns)
      holdEndpoint()
      await waitReady(gen)
      return 'resumed'
    } catch (e) {
      if (e instanceof ApiError && e.reason === 513) {          // still PARKING: try again shortly
        await D().sleep(1000)
        continue
      }
      if (e instanceof ApiError && e.status === 410) {
        markSandboxGone(e.reason)
        set({ endReason: (e.detail as { end_reason?: string } | null)?.end_reason ?? 'parked_expired' })
        return 'ended'
      }
      set({ phase: 'parked', error: errorOf(e), retryUntilMs: retryUntil(e) })
      return 'failed'
    }
  }
  return 'failed'
}

/** WS close 4430 (`error{518, detail.reason = parked}`) or 515 from REST: the server parked the session. */
export function markSandboxParked(): void {
  const s = sandboxSessionStore.getState()
  if (!s.sid || s.phase === 'parked' || s.phase === 'ended') return
  loopGen++
  stopAutoKeepalive()
  set({ phase: 'parked' })
  persist()
  const sid = s.sid
  const token = s.token
  if (token) {
    void api.getSession(sid, token).then((v) => {
      if (sandboxSessionStore.getState().sid === sid) {
        applyView(v)
        persist()
      }
    }).catch(() => undefined)
  }
}

/** M15 PowerController interface (M17-FR-079; AWR-04 §15.7.3): SAVING parks the sandbox (reason idle or hidden), RESUMING
 *  resumes it. Returns the action taken so the controller can show the right loading layer. */
export async function onPowerState(state: PowerState, reason: 'idle' | 'hidden' = 'idle'): Promise<'parked' | 'resumed' | 'queued' | 'none'> {
  const s = sandboxSessionStore.getState()
  if (state === 'saving') {
    if (s.phase === 'ready' || s.phase === 'active') return (await parkSandbox(reason)) ? 'parked' : 'none'
    return 'none'
  }
  if (state === 'resuming' && (s.phase === 'parked' || s.phase === 'parking')) {
    const r = await resumeParked()
    return r === 'queued' ? 'queued' : r === 'resumed' || r === 'active' ? 'resumed' : 'none'
  }
  return 'none'
}

/** Pool events forwarded by the realtime client (sandbox.state, sandbox.expiring, sandbox.rate, sandbox.parking/parked/resumed). */
export function handleSandboxEvent(kind: string, data: Record<string, unknown>): void {
  const s = sandboxSessionStore.getState()
  if (data.sid !== undefined && data.sid !== s.sid) return
  if (kind === 'sandbox.expiring') {
    const action = data.action === 'park' ? 'park' : 'end'
    set({ expiring: { reason: typeof data.reason === 'string' ? data.reason : '', inS: Number(data.in_s ?? 0), ...(data.action ? { action } : {}) } })
    if (action === 'end') void exportSandboxSnapshot(true)   // a park keeps the state on the server: no export needed
  } else if (kind === 'sandbox.state') {
    const to = (typeof data.to === 'string' ? data.to : '') as SandboxSessionView['state']
    if (to) set({ phase: phaseOf(to) })
    if (to === 'ENDED' || to === 'FAILED') markSandboxGone()
    if (to === 'PARKED') markSandboxParked()
  } else if (kind === 'sandbox.rate') {
    set({ grantedRate: Number(data.granted), rateCap: Number(data.cap), limitedBy: (data.limited_by as string | null) ?? null })
  } else if (kind === 'sandbox.parking') {
    if (s.phase === 'ready' || s.phase === 'active') set({ phase: 'parking', expiring: undefined })
  } else if (kind === 'sandbox.parked') {
    markSandboxParked()
  } else if (kind === 'sandbox.resumed') {
    if (typeof data.epoch === 'number') set({ epoch: data.epoch })
  }
}

/** `sandbox/session` topic payloads (1 Hz, awr.sandbox.session_state.v1). */
export function handleSessionTopic(d: { state?: string; expires_unix_ns?: string; renewable?: boolean; renew_block_reason?: string | null;
  clock?: { rate_granted?: number; rate_cap?: number; limited_by?: string | null };
  expiring?: { reason: string; in_s: number; action?: 'end' | 'park' } | null }): void {
  const p: Partial<SandboxSessionState> = {}
  if (d.state) p.phase = phaseOf(d.state as SandboxSessionView['state'])
  if (d.expires_unix_ns) p.expiresUnixNs = d.expires_unix_ns
  if (d.renewable !== undefined) p.renewable = d.renewable
  if (d.renew_block_reason !== undefined) p.renewBlockReason = d.renew_block_reason
  if (d.clock) {
    p.grantedRate = d.clock.rate_granted
    p.rateCap = d.clock.rate_cap
    p.limitedBy = d.clock.limited_by ?? null
  }
  if (d.expiring) p.expiring = { reason: d.expiring.reason, inS: d.expiring.in_s, ...(d.expiring.action ? { action: d.expiring.action } : {}) }
  else if (d.expiring === null) p.expiring = undefined
  set(p)
}

// ------------------------------------------------------------ auto keepalive
export function noteUserInput(): void {
  lastInputMs = D().now()
}

function holdKaLock(): boolean {
  const l = D().local
  const me = D().tabId
  const now = D().now()
  try {
    const raw = l?.getItem(KA_LOCK_KEY)
    const cur = raw ? (JSON.parse(raw) as { tab: string; t: number }) : null
    if (cur && cur.tab !== me && now - cur.t < KA_LOCK_MS) return false
    l?.setItem(KA_LOCK_KEY, JSON.stringify({ tab: me, t: now }))
    return true
  } catch {
    return true
  }
}

/** one auto-keepalive check (exported for tests; the interval calls it every 15 s) */
export async function autoKeepaliveTick(): Promise<boolean> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token || !s.expiresUnixNs || !(s.phase === 'ready' || s.phase === 'active')) return false
  const leftS = Number((BigInt(s.expiresUnixNs) - nowNs()) / 1_000_000_000n)
  if (leftS > KEEPALIVE_MARGIN_S || s.renewable === false) return false
  if (!D().visible() || D().now() - lastInputMs > INPUT_RECENT_MS) return false
  if (!holdKaLock()) return false
  return keepaliveSandbox()
}

export function startAutoKeepalive(): void {
  if (kaTimer !== null) return
  kaTimer = setInterval(() => void autoKeepaliveTick(), KA_TICK_MS)
}

export function stopAutoKeepalive(): void {
  if (kaTimer !== null) clearInterval(kaTimer)
  kaTimer = null
}

// ------------------------------------------------------------ D2.1 P1: commander pairing (AWR-04 §15.8.2 item 4)
/** Holder side: a pairing code for a tablet or phone to join this sandbox (single use, 120 s). */
export async function sandboxPairCode(): Promise<{ code: string; expiresUnixNs: string } | null> {
  const s = sandboxSessionStore.getState()
  if (!s.sid || !s.token) return null
  try {
    const r = await api.createPairCode(s.sid, s.token)
    return { code: r.code, expiresUnixNs: r.expires_unix_ns }
  } catch (e) {
    set({ error: errorOf(e), retryUntilMs: retryUntil(e) })
    return null
  }
}

/** Joining device: redeem the code, hold the same sandbox with the same principal (shared seat). */
export async function joinByPairCode(sid: string, code: string): Promise<boolean> {
  const gen = ++loopGen
  set({ phase: 'creating', error: undefined })
  try {
    const r = await api.redeemPairCode(sid, code)
    if (gen !== loopGen) return false
    applyView(r.session)
    set({ token: r.token, expiresUnixNs: r.token_exp_unix_ns ?? r.session.expires_unix_ns })
    persist()
    holdEndpoint()
    if (r.session.state === 'SPAWNING') await waitReady(gen)
    else if (r.session.state !== 'PARKED' && r.session.state !== 'PARKING') startAutoKeepalive()
    return true
  } catch (e) {
    set({ phase: 'none', error: errorOf(e) })
    return false
  }
}

/** M17 PRD §7.9 interface (one object for the UI shell); D2.1 adds park, parkBeacon, resumeParked, onPowerState. */
export const sandboxSession = {
  create: createSandbox, claim: claimSlot, leaveQueue, keepalive: keepaliveSandbox, end: endSandbox, switchWorld: switchSandboxWorld,
  loadTemplate: loadSandboxTemplate, exportSnapshot: exportSandboxSnapshot, restore: restoreSandbox, resume: resumeSandbox,
  rtEndpoint: sandboxRtEndpoint, canCreate: canCreateSandbox, park: parkSandbox, parkBeacon: parkSandboxBeacon,
  resumeParked, onPowerState, restoreQueue, markParked: markSandboxParked, pairCode: sandboxPairCode, joinByPairCode,
}
