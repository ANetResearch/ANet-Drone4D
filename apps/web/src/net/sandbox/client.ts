// Sandbox REST v1 client for the session endpoints SB-00 to SB-17 (M17-FR-043, FR-059; AWR-17 §17.4, §17.5). Owner: M17.
// Plain fetchers (net never imports stores, AWR-03 §4.2): every function takes the sid and token explicitly; errors are
// problem+json mapped to ApiError (status, reason code, message, remedy, detail). Queue endpoints carry the ticket token
// `q1.…` (§17.3.2). The domain routes (vehicles, targets, ...) go through net/api.ts with the session endpoint.
// D2.1 (AWR-17 §17.20.2, §17.20.4, §17.20.5; M17-FR-068, FR-079): the public capacity endpoint SB-71 (no token), park SB-69,
// the park beacon SB-72 (token in the body, sendBeacon cannot set headers) and resume SB-70 (201 with a fresh token, 200 when
// already active, 202 with a resume ticket when full). The internal identity token rides on session creation as
// `X-AWR-Identity`; the identity store (M15) registers a provider with setSandboxIdentityProvider (net never imports stores).
import { ApiError } from '../api'
import { CLIENT_HEADER, CLIENT_NAME } from '../clientName'
import { apiUrl, SANDBOX_API } from '../endpoint'

export interface SandboxLimits {
  vehicles_max: number; targets_max: number; nests_max: number; tasks_active_max: number; cloned_models_max?: number
  rest_rps: number; call_rps: number; clock_rps: number; ws_max: number; setpoint_hz_max: number; [k: string]: unknown
}
export interface SandboxClock { state: string; rate_requested: number; rate_granted: number; rate_cap: number; limited_by: string | null; tick_hz: number }
export type SandboxStateName = 'SPAWNING' | 'READY' | 'ACTIVE' | 'IDLE' | 'PARKING' | 'PARKED' | 'DRAINING' | 'ENDED' | 'FAILED'
export interface SandboxSessionView {
  sid: string
  state: SandboxStateName
  world_id: string
  template_id: string | null
  created_unix_ns: string
  expires_unix_ns: string
  lifetime_max_unix_ns?: string
  expires_in_s: number
  renewable: boolean
  renew_block_reason?: 'queue_nonempty' | 'max_lifetime' | 'degraded' | null
  clock: SandboxClock
  limits: SandboxLimits
  usage?: Record<string, number>
  epoch?: number
  degrade_level?: string
  end_reason?: string | null
  endpoints: { rest: string; rt: string }
  // D2.1 (AWR-17 §17.20.4)
  tier?: 'public' | 'internal'
  last_input_unix_ns?: string | null
  parked_unix_ns?: string | null
  park_reason?: 'idle' | 'hidden' | 'user' | 'idle_disconnected' | 'preempted' | 'reclaimed_cpu' | 'reclaimed_mem' | null
  resume_mode?: 'checkpoint' | 'snapshot' | null
  retention_unix_ns?: string | null
  remaining_s?: number | null
  t_sim_ns?: string | null
}
export interface QueueInfo {
  ticket_id: string; ticket_token: string; position: number; eta_s: number | null; expires_unix_ns?: string
  kind?: 'new' | 'resume' | 'internal'; sid?: string
}
export interface TicketView {
  ticket_id: string; state: 'waiting' | 'slot_ready' | 'claimed' | 'expired'; position: number; eta_s: number | null
  claim_deadline_unix_ns?: string | null; world_id?: string; template_id?: string | null
}
export interface Challenge { challenge_id: string; alg: 'sha256-lz'; prefix_b64: string; bits: number; expires_unix_ns: string }
export interface CreateBody {
  world: string; template?: string | null; config_snapshot?: unknown; calendar?: { start_utc: string; tz: string }
  acoustic_bg?: 'rural' | 'suburban' | 'urban'; seed?: number; principal_hint: string
  pow?: { challenge_id: string; nonce: number }; force_new?: boolean; queue_ok?: boolean
  client?: { kind: 'browser' | 'sdk' | 'other'; version?: string }
}
export type CreateResult =
  | { kind: 'created'; session: SandboxSessionView; token: string | null; tokenExpUnixNs: string | null; idempotent: boolean }
  | { kind: 'queued'; queue: QueueInfo; availableNow: string[] }

/** `awr.capacity.v1` (SB-71, AWR-17 §17.20.2) */
export interface BuildInfo { version: string; channel: 'demo' | 'local' | 'ci'; commit: string; dirty: boolean; built_at: string; release: string }
export type CapacityHint = 'open' | 'queue_viewer' | 'queue_sandbox' | 'queue_both' | 'closed' | 'unavailable'
export interface CapacityView {
  schema: 'awr.capacity.v1'
  t_unix_ns: string
  build: BuildInfo
  accepting: boolean
  maintenance: boolean
  degrade_level: string
  viewers: { live: number | null; max: number | null; queue: number | null; eta_s: number | null; state: 'open' | 'full' | 'unknown' }
  sandbox: {
    active: number | null; max: number | null; reserved_internal: number | null; internal_active?: number | null; parked: number | null
    queue: number | null; eta_s: number | null; admit_now: boolean; worlds_admit_now: string[]; state: 'open' | 'full' | 'queue' | 'closed'
  }
  power: { viewer_idle_s: number; sandbox_idle_s: number; hidden_s: number; warn_s: number }
  hint: CapacityHint
}
export type ParkReason = 'idle' | 'hidden' | 'user'
export type ResumeResult =
  | { kind: 'resumed'; session: SandboxSessionView; token: string | null; tokenExpUnixNs: string | null }
  | { kind: 'active'; session: SandboxSessionView }
  | { kind: 'queued'; queue: QueueInfo }

type FetchFn = (input: string, init?: RequestInit) => Promise<Response>
let fetchFn: FetchFn = (input, init) => globalThis.fetch(input, init)
let identityProvider: (() => string | null) | null = null

/** the identity store (M15, stores/identity.ts) hands in the current internal identity token (or null) */
export function setSandboxIdentityProvider(p: (() => string | null) | null): void {
  identityProvider = p
}

function identityToken(): string | null {
  try {
    return identityProvider?.() ?? null
  } catch {
    return null
  }
}

/** test hook: replace fetch (vitest runs without a server) */
export function setSandboxFetch(f: FetchFn | null): void {
  fetchFn = f ?? ((input, init) => globalThis.fetch(input, init))
}

interface Problem { code?: number; name?: string; message?: string; remedy?: string; detail?: unknown }

async function toError(r: Response, path: string): Promise<ApiError> {
  let p: Problem = {}
  try {
    p = (await r.json()) as Problem
  } catch {
    p = {}
  }
  const err = new ApiError(r.status, typeof p.code === 'number' ? p.code : null, p.message ?? `${r.status} ${path}`, p.name ?? null,
    p.remedy ?? null, p.detail ?? null)
  const ra = r.headers.get('retry-after')
  ;(err as ApiError & { retryAfterS?: number }).retryAfterS = ra && /^\d+$/.test(ra) ? Number(ra) : undefined
  return err
}

async function call<T>(method: string, path: string, opts: { token?: string; body?: unknown; signal?: AbortSignal; identity?: boolean } = {}): Promise<{ status: number; body: T; headers: Headers }> {
  const headers = new Headers({ accept: 'application/json', [CLIENT_HEADER]: CLIENT_NAME })
  if (opts.token) headers.set('authorization', `Bearer ${opts.token}`)
  const ident = opts.identity ? identityToken() : null
  if (ident) headers.set('x-awr-identity', ident)
  if (opts.body !== undefined) headers.set('content-type', 'application/json')
  const r = await fetchFn(apiUrl(path), { method, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal, cache: 'no-store' })
  if (!r.ok) throw await toError(r, path)
  const text = r.status === 204 ? '' : await r.text()
  return { status: r.status, body: (text ? JSON.parse(text) : null) as T, headers: r.headers }
}

const S = (sid: string, rel = '') => `${SANDBOX_API}/sessions/${sid}${rel}`

export async function getMeta(): Promise<Record<string, unknown>> {
  return (await call<Record<string, unknown>>('GET', `${SANDBOX_API}/meta`)).body
}
export async function getWorlds(): Promise<{ items: Record<string, unknown>[] }> {
  return (await call<{ items: Record<string, unknown>[] }>('GET', `${SANDBOX_API}/worlds`)).body
}
export async function getTemplates(world?: string): Promise<{ items: Record<string, unknown>[] }> {
  const q = world ? `?world=${encodeURIComponent(world)}` : ''
  return (await call<{ items: Record<string, unknown>[] }>('GET', `${SANDBOX_API}/templates${q}`)).body
}

export async function createSession(body: CreateBody, signal?: AbortSignal): Promise<CreateResult> {
  const r = await call<Record<string, unknown>>('POST', `${SANDBOX_API}/sessions`, { body, signal, identity: true })
  if (r.status === 202) {
    const b = r.body as { queue: QueueInfo; available_now?: string[] }
    return { kind: 'queued', queue: b.queue, availableNow: b.available_now ?? [] }
  }
  const b = r.body as { session: SandboxSessionView; token: string | null; token_exp_unix_ns?: string; idempotent?: boolean }
  return { kind: 'created', session: b.session, token: b.token, tokenExpUnixNs: b.token_exp_unix_ns ?? null, idempotent: !!b.idempotent }
}

export async function getSession(sid: string, token: string, wait?: { state: 'READY' | 'ACTIVE'; waitS: number }, signal?: AbortSignal): Promise<SandboxSessionView> {
  const q = wait ? `?wait_state=${wait.state}&wait_s=${Math.min(10, wait.waitS)}` : ''
  return (await call<SandboxSessionView>('GET', S(sid, q), { token, signal })).body
}

export async function keepalive(sid: string, token: string): Promise<{ session: SandboxSessionView; renewed: boolean; token: string | null; token_exp_unix_ns?: string }> {
  return (await call<{ session: SandboxSessionView; renewed: boolean; token: string | null; token_exp_unix_ns?: string }>('POST', S(sid, '/keepalive'), { token, body: {} })).body
}

export async function endSession(sid: string, token: string): Promise<void> {
  await call('DELETE', S(sid), { token })
}

export async function exportSnapshot(sid: string, token: string): Promise<Record<string, unknown>> {
  return (await call<Record<string, unknown>>('GET', S(sid, '/snapshot'), { token })).body
}

export async function restoreSnapshot(sid: string, token: string, snapshot: unknown): Promise<SandboxSessionView> {
  return (await call<SandboxSessionView>('POST', S(sid, '/restore'), { token, body: { snapshot } })).body
}

export async function loadTemplate(sid: string, token: string, template: string): Promise<SandboxSessionView> {
  return (await call<SandboxSessionView>('POST', S(sid, '/template'), { token, body: { template } })).body
}

export async function switchWorld(sid: string, token: string, world: string, template?: string): Promise<SandboxSessionView> {
  return (await call<SandboxSessionView>('POST', S(sid, '/world'), { token, body: template ? { world, template } : { world } })).body
}

export async function pollTicket(ticketId: string, ticketToken: string, waitS: number, signal?: AbortSignal): Promise<TicketView> {
  return (await call<TicketView>('GET', `${SANDBOX_API}/queue/${ticketId}?wait_s=${Math.min(25, Math.max(0, waitS))}`, { token: ticketToken, signal })).body
}

export async function claimTicket(ticketId: string, ticketToken: string, principalHint: string): Promise<CreateResult> {
  const r = await call<Record<string, unknown>>('POST', `${SANDBOX_API}/queue/${ticketId}/claim`, { token: ticketToken, body: { principal_hint: principalHint } })
  const b = r.body as { session: SandboxSessionView; token: string | null; token_exp_unix_ns?: string; idempotent?: boolean }
  return { kind: 'created', session: b.session, token: b.token, tokenExpUnixNs: b.token_exp_unix_ns ?? null, idempotent: !!b.idempotent }
}

export async function leaveTicket(ticketId: string, ticketToken: string): Promise<void> {
  await call('DELETE', `${SANDBOX_API}/queue/${ticketId}`, { token: ticketToken })
}

/** SB-17: end the session from pagehide (sendBeacon cannot set headers, so the token goes in the body). */
export function beaconEnd(sid: string, token: string): boolean {
  const nav = globalThis.navigator as Navigator | undefined
  if (!nav?.sendBeacon) return false
  return nav.sendBeacon(apiUrl(S(sid, '/end-beacon')), new Blob([JSON.stringify({ token })], { type: 'application/json' }))
}

// ------------------------------------------------------------ D2.1: capacity, park and resume
/** SB-71: public capacity (no token, no visitor identifiers); the caller applies its own timeout through `signal`. */
export async function getCapacity(signal?: AbortSignal): Promise<CapacityView> {
  return (await call<CapacityView>('GET', `${SANDBOX_API}/capacity`, { signal })).body
}

/** SB-69: 202 PARKING, 200 already PARKED. */
export async function parkSession(sid: string, token: string, reason: ParkReason = 'user'): Promise<{ state: 'PARKING' | 'PARKED'; session?: SandboxSessionView }> {
  return (await call<{ state: 'PARKING' | 'PARKED'; session?: SandboxSessionView }>('POST', S(sid, '/park'), { token, body: { reason } })).body
}

/** SB-72: park from pagehide or freeze (sendBeacon cannot set headers, so the token goes in the body). */
export function beaconPark(sid: string, token: string, reason: ParkReason = 'hidden'): boolean {
  const nav = globalThis.navigator as Navigator | undefined
  if (!nav?.sendBeacon) return false
  return nav.sendBeacon(apiUrl(S(sid, '/park-beacon')), new Blob([JSON.stringify({ token, reason })], { type: 'application/json' }))
}

/** SB-70: 201 resumed (fresh token), 200 already active, 202 resume ticket (same FIFO as new visitors). The token may have
 *  expired while parked; the server accepts it up to the park retention. */
export async function resumeSession(sid: string, token: string): Promise<ResumeResult> {
  const r = await call<Record<string, unknown>>('POST', S(sid, '/resume'), { token, body: {} })
  if (r.status === 202) return { kind: 'queued', queue: (r.body as { queue: QueueInfo }).queue }
  const b = r.body as { session: SandboxSessionView; token: string | null; token_exp_unix_ns?: string }
  if (r.status === 201) return { kind: 'resumed', session: b.session, token: b.token, tokenExpUnixNs: b.token_exp_unix_ns ?? null }
  return { kind: 'active', session: b.session }
}

// ------------------------------------------------------------ D2.1 P1: commander pairing (SB-76, SB-77; M17-FR-081)
/** SB-76: a single-use 8-character pairing code (120 s) for another device to join this sandbox. */
export async function createPairCode(sid: string, token: string): Promise<{ code: string; expires_unix_ns: string }> {
  return (await call<{ code: string; expires_unix_ns: string }>('POST', S(sid, '/pair-code'), { token, body: {} })).body
}

/** SB-77: redeem a pairing code: a sandbox token for the same principal (the paired device shares the seat). */
export async function redeemPairCode(sid: string, code: string): Promise<{ session: SandboxSessionView; token: string; token_exp_unix_ns?: string }> {
  return (await call<{ session: SandboxSessionView; token: string; token_exp_unix_ns?: string }>('POST', S(sid, '/pair'),
    { body: { code: code.trim().toUpperCase() } })).body
}
