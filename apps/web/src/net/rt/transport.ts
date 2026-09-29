// Transport seam of rt.worker (M11 §6.4.14, §6.6.3; AWR-17 §6.13 item 2). Owner: M11.
// SocketLike is the subset of the WebSocket API the worker uses, so FakeSource (and tests) can stand in for the network.
// Backoff follows partysocket's shape: minDelay 500 ms, grow 1.5, max 10 s, +-20 % jitter, connection timeout 4 s.
export const WS_CONNECTING = 0
export const WS_OPEN = 1
export const WS_CLOSING = 2
export const WS_CLOSED = 3

export interface SocketMessage { data: string | ArrayBuffer }
export interface SocketClose { code: number; reason?: string }
export interface SocketLike {
  binaryType: string
  readonly readyState: number
  onopen: ((ev?: unknown) => void) | null
  onmessage: ((ev: SocketMessage) => void) | null
  onclose: ((ev: SocketClose) => void) | null
  onerror: ((ev?: unknown) => void) | null
  send(data: string | ArrayBuffer | ArrayBufferView): void
  close(code?: number, reason?: string): void
}
export type SocketFactory = (url: string, protocols: string[]) => SocketLike

export const BACKOFF = { minDelayMs: 500, grow: 1.5, maxDelayMs: 10_000, jitter: 0.2, connectTimeoutMs: 4000, connLimitMs: 30_000 } as const

/** attempt >= 1; rnd in [0, 1) */
export function backoffDelay(attempt: number, rnd: number): number {
  const base = Math.min(BACKOFF.maxDelayMs, BACKOFF.minDelayMs * BACKOFF.grow ** Math.max(0, attempt - 1))
  return Math.round(base * (1 + BACKOFF.jitter * (2 * rnd - 1)))
}

/** subprotocol list of AWR-17 §6.2: awr.rt.v1 first, bearer token second (omitted when empty) */
export function protocolsFor(token: string): string[] {
  return token ? ['awr.rt.v1', `bearer.${token}`] : ['awr.rt.v1']
}

/** http(s) URL on the same host as a ws(s) URL (whoami triage after 1006, AWR-17 §3.3 item 4) */
export function httpUrlOf(wsUrl: string, path: string): string {
  try {
    const u = new URL(wsUrl)
    u.protocol = u.protocol === 'wss:' ? 'https:' : 'http:'
    u.pathname = path
    u.search = ''
    u.hash = ''
    return u.toString()
  } catch {
    return path
  }
}

/** GET /api/auth/whoami with the bearer token; the HTTP status, 0 on a network error */
export function whoamiStatus(wsUrl: string, token: string): Promise<number> {
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token) headers.authorization = `Bearer ${token}`
  return fetch(httpUrlOf(wsUrl, '/api/auth/whoami'), { headers, cache: 'no-store' }).then((r) => r.status, () => 0)
}
