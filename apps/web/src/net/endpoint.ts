// API and realtime endpoints by session role (M11-FR-219; AWR-04 §3.2 M11 row, §10.2; ADR-111). Owner: M11.
//   * shared show (D1 api): REST under `/api`, realtime `/api/rt`, page token from POST /api/auth/token (net/api.ts);
//   * sandbox (sandbox-api): REST under `/api/sandbox/v1/sessions/{sid}`, realtime `.../{sid}/rt`, the sandbox token
//     from the session store (stores/sandboxSession.ts, M17), which registers itself with setEndpoint(); net never
//     imports stores (AWR-03 §4.1 rule), the store hands in a token getter and an optional refresher;
//   * the API origin is configurable (VITE_AWR_API_ORIGIN at build time or setApiOrigin() at run time; default: the page
//     origin), so a static build can talk to a gateway on another host.
// apiBase() and rtUrl() follow the current endpoint; listeners hear endpoint changes (the realtime client reconnects to
// the new URL, M15 re-initialises it).

export const SHARED_API = '/api'
export const SANDBOX_API = '/api/sandbox/v1'
const SID_RE = /^sb-[a-z2-7]{10}$/
const SESSION_PATH = /^\/api\/sandbox\/v1\/sessions\/(sb-[a-z2-7]{10})(?:\/|$)/

export interface SharedEndpoint {
  kind: 'shared'
}
export interface SandboxEndpoint {
  kind: 'sandbox'
  sid: string
  /** current sandbox token (the store keeps it fresh after keepalive) */
  token: () => string
  /** a fresh token after 401 or WS 4401 (keepalive or GET /sessions/{sid} triage); null when the session is gone */
  onAuthExpired?: () => Promise<string | null>
}
export type Endpoint = SharedEndpoint | SandboxEndpoint

let current: Endpoint = { kind: 'shared' }
let origin = initialOrigin()
const listeners = new Set<(e: Endpoint) => void>()

function initialOrigin(): string {
  try {
    const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env
    const o = env?.VITE_AWR_API_ORIGIN
    return typeof o === 'string' ? o.replace(/\/+$/, '') : ''
  } catch {
    return ''
  }
}

/** API origin, '' for the page origin (scheme://host[:port], no trailing slash) */
export function apiOrigin(): string {
  return origin
}
export function setApiOrigin(o: string): void {
  origin = o.replace(/\/+$/, '')
}

export function getEndpoint(): Endpoint {
  return current
}

/** switch the session role (shared show <-> sandbox sid); listeners run when the URL changes */
export function setEndpoint(e: Endpoint): void {
  if (e.kind === 'sandbox' && !SID_RE.test(e.sid)) throw new Error(`invalid sandbox sid: ${e.sid}`)
  const changed = e.kind !== current.kind || (e.kind === 'sandbox' && current.kind === 'sandbox' && e.sid !== current.sid)
  current = e
  if (changed) for (const cb of listeners) cb(e)
}

export function onEndpointChange(cb: (e: Endpoint) => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** REST prefix of the current endpoint: `/api` or `/api/sandbox/v1/sessions/{sid}` */
export function apiBase(): string {
  return current.kind === 'sandbox' ? `${SANDBOX_API}/sessions/${current.sid}` : SHARED_API
}

/** `rel` under the current REST prefix (`/vehicles` -> `/api/sandbox/v1/sessions/{sid}/vehicles`) */
export function apiPath(rel: string): string {
  return `${apiBase()}${rel.startsWith('/') ? rel : `/${rel}`}`
}

/** absolute URL of an API path (prefixed with the configured origin) */
export function apiUrl(path: string): string {
  return /^https?:\/\//.test(path) ? path : `${origin}${path}`
}

interface LocLike { protocol: string; host: string }

/** realtime URL of the current endpoint (ws or wss by the API origin, else by the page) */
export function rtUrl(loc: LocLike | undefined = typeof location !== 'undefined' ? location : undefined): string {
  const path = current.kind === 'sandbox' ? `${SANDBOX_API}/sessions/${current.sid}/rt` : `${SHARED_API}/rt`
  if (origin) return `${origin.replace(/^http/, 'ws')}${path}`
  const proto = loc?.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc?.host ?? 'localhost'}${path}`
}

/** sid of a sandbox session path (`/api/sandbox/v1/sessions/{sid}/...`), else null */
export function sidOfPath(path: string): string | null {
  const p = path.replace(/^https?:\/\/[^/]+/, '')
  const m = SESSION_PATH.exec(p)
  return m ? m[1] : null
}

export function isSandboxPath(path: string): boolean {
  return path.replace(/^https?:\/\/[^/]+/, '').startsWith(`${SANDBOX_API}/`) || path.endsWith(SANDBOX_API)
}

/** sandbox token for a session path of the current sandbox endpoint, '' otherwise (public sandbox routes need none) */
export function sandboxTokenFor(path: string): string {
  const sid = sidOfPath(path)
  if (!sid || current.kind !== 'sandbox' || current.sid !== sid) return ''
  return current.token()
}

/** URL used to triage a refused realtime upgrade (1006 / 4401): whoami for the shared show, the session object
 * (SB-08) for a sandbox (AWR-17 §17.9.3: 200 reconnect, 401 or 410 the session is gone) */
export function authCheckPath(wsUrl: string): string {
  const sid = sidOfPath(wsUrl.replace(/^wss?:\/\/[^/]+/, ''))
  return sid ? `${SANDBOX_API}/sessions/${sid}` : `${SHARED_API}/auth/whoami`
}
