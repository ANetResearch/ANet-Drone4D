// REST fetchers and token (AWR-03 §4.1: net/api.ts holds only fetchers and the token; QueryClient, keys and
// queryOptions live in app/query, M15). Owner: M11. Errors are problem+json (AWR-17 §4.4): ApiError carries the HTTP
// status, the reason code and the Chinese message and remedy from the server.
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: number | null,
    message: string,
    readonly name_: string | null = null,
    readonly remedy: string | null = null,
    readonly detail: unknown = null,
  ) {
    super(message)
  }
}

interface Problem { code?: number; reason?: number; name?: string; message?: string; remedy?: string; detail?: unknown }

async function toError(r: Response, path: string): Promise<ApiError> {
  let p: Problem = {}
  try {
    p = (await r.json()) as Problem
  } catch {
    p = {}
  }
  const code = typeof p.code === 'number' ? p.code : typeof p.reason === 'number' ? p.reason : null
  return new ApiError(r.status, code, p.message ?? `${r.status} ${path}`, p.name ?? null, p.remedy ?? null, p.detail ?? null)
}

/**
 * Headers with the bearer token. Before the first token exists the request waits for getToken() (the page's operator
 * token, shared with the realtime client), so early REST calls such as GET /api/worlds are not sent unauthenticated
 * (401 before SK-E2E); without a gateway getToken() resolves '' and the request goes out without authorization.
 */
async function withAuth(init?: RequestInit): Promise<Headers> {
  const headers = new Headers(init?.headers)
  if (!headers.has('accept')) headers.set('accept', 'application/json')
  if (!headers.has('authorization')) {
    const token = await getToken()
    if (token) headers.set('authorization', `Bearer ${token}`)
  }
  return headers
}

/** one retry with a fresh token after 401 (token expired, or a new run with a new secret) */
async function send(path: string, init: RequestInit, headers: Headers): Promise<Response> {
  const r = await fetch(path, { ...init, headers })
  if (r.status !== 401 || !cached || new Headers(init.headers).has('authorization')) return r
  cached = null
  const token = await getToken()
  if (!token) return r
  headers.set('authorization', `Bearer ${token}`)
  return fetch(path, { ...init, headers })
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await send(path, init ?? {}, await withAuth(init))
  if (!r.ok) throw await toError(r, path)
  return (await r.json()) as T
}

export async function apiPost<T>(path: string, body: unknown, init?: RequestInit): Promise<T> {
  const headers = await withAuth(init)
  headers.set('content-type', 'application/json')
  const r = await send(path, { ...init, method: 'POST', body: JSON.stringify(body) }, headers)
  if (!r.ok) throw await toError(r, path)
  return (await r.json()) as T
}

/** DELETE with the page token (AWR-17 §4.3.5 R15, R62; M15-to-M11 item 1, INT-1): resolves to the HTTP status on 2xx,
 * rejects with ApiError (problem+json) otherwise */
export async function apiDelete(path: string, init?: RequestInit): Promise<number> {
  const headers = await withAuth(init)
  const r = await send(path, { ...init, method: 'DELETE' }, headers)
  if (!r.ok) throw await toError(r, path)
  return r.status
}

/** POST /api/world/{id}/query (AWR-17 §4.3.2; M04). ray_hit request and response shapes. */
export interface RayHitRequest { op: 'ray_hit'; origin_enu_m: [number, number, number]; dir: [number, number, number]; max_range_m?: number }
export interface RayHitResponse {
  hit: boolean
  point_enu_m: [number, number, number] | null
  dist_m: number | null
  surface: 'dsm' | 'dtm' | 'none'
  content_version?: string
  source?: string
  hit_kind?: 'top' | 'side'
  ground_z_m?: number
  agl_m?: number
}
export function worldQuery<T>(worldId: string, body: object, signal?: AbortSignal): Promise<T> {
  return apiPost<T>(`/api/world/${encodeURIComponent(worldId)}/query`, body, { signal })
}

// ------------------------------------------------------------ token (AWR-17 §3.2, §4.3.1)
export interface TokenInfo { token: string; role: string; principalId: string; expUnixMs: number }
let cached: TokenInfo | null = null
let inflight: Promise<string> | null = null
const HINT_KEY = 'awr.principal_hint'
const REFRESH_MARGIN_MS = 60_000
/** after a failed token request (no gateway: FakeSource pages, api down) REST calls skip the token for this long */
const NO_GATEWAY_RETRY_MS = 5_000
let noGatewayUntil = 0

function readHint(): string | undefined {
  try {
    return globalThis.localStorage?.getItem(HINT_KEY) ?? undefined
  } catch {
    return undefined
  }
}
function writeHint(h: string): void {
  try {
    globalThis.localStorage?.setItem(HINT_KEY, h)
  } catch {
    // storage unavailable (private mode): the server issues a new principal next time
  }
}

/**
 * POST /api/auth/token {role, principal_hint, client}. Returns '' when no gateway answers (offline, FakeSource), so the
 * realtime client still starts; the token is cached until one minute before expiry.
 */
export async function getToken(role: 'viewer' | 'operator' | 'admin' = 'operator', o: { force?: boolean } = {}): Promise<string> {
  if (o.force) {
    // the realtime link was refused (4401, 4403, whoami 401): issue a fresh token for the same principal_hint
    cached = null
    noGatewayUntil = 0
  } else if (cached && cached.role === role && cached.expUnixMs - Date.now() > REFRESH_MARGIN_MS) return cached.token
  else if (cached && role === 'operator' && cached.role === 'viewer' && cached.expUnixMs - Date.now() > REFRESH_MARGIN_MS) return cached.token
  if (inflight) return inflight
  if (Date.now() < noGatewayUntil) return ''
  inflight = (async () => {
    try {
      const body: Record<string, unknown> = { role, client: 'web/0.1.0' }
      const hint = readHint()
      if (hint) body.principal_hint = hint
      const r = await fetch('/api/auth/token', { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(body) })
      if (!r.ok) {
        if (role === 'operator' && r.status === 409) return getTokenAs('viewer')
        if (r.status === 404 || r.status >= 500) noGatewayUntil = Date.now() + NO_GATEWAY_RETRY_MS
        return ''
      }
      const j = (await r.json()) as { token?: string; role?: string; principal_id?: string; principal_hint?: string; exp_unix_ns?: string }
      if (!j.token) return ''
      if (j.principal_hint) writeHint(j.principal_hint)
      cached = { token: j.token, role: j.role ?? role, principalId: j.principal_id ?? '', expUnixMs: j.exp_unix_ns ? Number(BigInt(j.exp_unix_ns) / 1_000_000n) : Date.now() + 3_600_000 }
      return j.token
    } catch {
      noGatewayUntil = Date.now() + NO_GATEWAY_RETRY_MS
      return ''
    } finally {
      inflight = null
    }
  })()
  return inflight
}
function getTokenAs(role: 'viewer'): Promise<string> {
  inflight = null
  return getToken(role)
}

/** role of the cached token (null before a token was issued) */
export function tokenRole(): string | null {
  return cached?.role ?? null
}

/** forget the cached token (the next getToken() issues a new one) */
export function invalidateToken(): void {
  cached = null
}

/** GET /api/auth/whoami (AWR-17 §4.3.1) with the cached token; the HTTP status, 0 on a network error */
export async function whoami(): Promise<{ status: number; body: Record<string, unknown> | null }> {
  const headers = new Headers({ accept: 'application/json' })
  if (cached) headers.set('authorization', `Bearer ${cached.token}`)
  try {
    const r = await fetch('/api/auth/whoami', { headers, cache: 'no-store' })
    let body: Record<string, unknown> | null = null
    try {
      body = r.ok ? ((await r.json()) as Record<string, unknown>) : null
    } catch {
      body = null
    }
    return { status: r.status, body }
  } catch {
    return { status: 0, body: null }
  }
}
