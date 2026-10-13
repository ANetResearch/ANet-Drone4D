// Internal identity store (R-D2-38, R-D2-39; ADR-123; AWR-04 §15.9; AWR-17 §17.20.5; M15-FR-235, NFR-204; UX-FR-157).
// Owner: M15. Holds the identity token (`scope = identity`, `role = internal`, 12 h) issued by show-api's R79 and the badge
// state; never the pass phrase: verify(secret) receives the user's input only as an argument, puts it into one request body
// and keeps no reference (no field, no storage, no log, no error text; the settings section clears its input before the
// request leaves). The server is the only comparison point (constant time, rate limit, prefix / aggregate / global lockout).
//   * storage: sessionStorage `awr.identity.v1` by default; "remember this device for 12 h" moves it to localStorage;
//     expired tokens are dropped on read; logout (R80) revokes the jti and clears both;
//   * results: ok, wrong (401 304), locked (429 516 with Retry-After), rate (429 111), disabled (403 115: this deployment has
//     no secret file), error (network, other);
//   * consumers: the sandbox clients attach X-AWR-Identity on session creation (ui/sandbox/session.ts setSandboxIdentity and
//     M17's net/sandbox/client setSandboxIdentityProvider), app/shell/internal.ts turns the internal surface on and off.
// Actions are plain functions next to the store (STORE-01).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'

export type IdentityPhase = 'idle' | 'checking' | 'ok' | 'wrong' | 'locked' | 'rate' | 'disabled' | 'error'
export interface IdentityState {
  tier: 'public' | 'internal'
  token: string | null
  expUnixMs: number
  remember: boolean
  phase: IdentityPhase
  /** wall ms until which the verify button waits (Retry-After of 429) */
  retryUntilMs: number
  version: number
}

export const IDENTITY_KEY = 'awr.identity.v1'
const PRINCIPAL_KEY = 'awr.principal_hint'
export const IDENTITY_PATH = '/api/auth/identity'

interface Stored { token: string; exp_unix_ms: number }

function storage(kind: 'session' | 'local'): Storage | null {
  try {
    return kind === 'session' ? globalThis.sessionStorage ?? null : globalThis.localStorage ?? null
  } catch {
    return null
  }
}
function readStored(kind: 'session' | 'local'): Stored | null {
  try {
    const v = storage(kind)?.getItem(IDENTITY_KEY)
    if (!v) return null
    const x = JSON.parse(v) as Stored
    if (typeof x.token !== 'string' || !/^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(x.token) || !(x.exp_unix_ms > Date.now())) return null
    return x
  } catch {
    return null
  }
}
function writeStored(kind: 'session' | 'local', x: Stored | null): void {
  try {
    const s = storage(kind)
    if (!s) return
    if (x) s.setItem(IDENTITY_KEY, JSON.stringify(x))
    else s.removeItem(IDENTITY_KEY)
  } catch {
    // storage unavailable: the token lives for this page only
  }
}

function initial(): IdentityState {
  const local = readStored('local')
  const sess = readStored('session')
  const x = sess ?? local
  return {
    tier: x ? 'internal' : 'public', token: x?.token ?? null, expUnixMs: x?.exp_unix_ms ?? 0, remember: !!local, phase: x ? 'ok' : 'idle', retryUntilMs: 0, version: 0,
  }
}

export const identityStore = createAwrStore<IdentityState>('identity', initial)
export const useIdentity = <T,>(sel: (s: IdentityState) => T): T => useStore(identityStore, sel)

function set(p: Partial<IdentityState>): void {
  identityStore.setState({ ...p, version: identityStore.getState().version + 1 })
}

type Fetcher = (url: string, init: RequestInit) => Promise<Response>
let fetcher: Fetcher = (u, i) => fetch(u, i)
/** tests replace the transport */
export function setIdentityFetcher(f: Fetcher | null): void {
  fetcher = f ?? ((u, i) => fetch(u, i))
}

/** the current identity token (null when public or expired) */
export function identityToken(): string | null {
  const s = identityStore.getState()
  if (!s.token) return null
  if (s.expUnixMs <= Date.now()) {
    clearLocal()
    return null
  }
  return s.token
}

function clearLocal(): void {
  writeStored('session', null)
  writeStored('local', null)
  set({ tier: 'public', token: null, expUnixMs: 0, phase: 'idle' })
}

function principal(): string | undefined {
  try {
    const h = globalThis.localStorage?.getItem(PRINCIPAL_KEY) ?? undefined
    return h && /^[0-9a-f]{32}$/i.test(h) ? h : undefined
  } catch {
    return undefined
  }
}

const nsToMs = (ns: unknown): number => {
  try {
    return typeof ns === 'string' || typeof ns === 'number' ? Number(BigInt(ns) / 1_000_000n) : 0
  } catch {
    return 0
  }
}

export const identity = {
  /**
   * R79 POST /api/auth/identity {secret, principal_hint}. The argument is used for the request body only. Resolves with the
   * resulting phase; the store keeps the token on success.
   */
  async verify(secret: string): Promise<IdentityPhase> {
    if (!secret) return identityStore.getState().phase
    set({ phase: 'checking' })
    let r: Response
    try {
      r = await fetcher(IDENTITY_PATH, {
        method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, credentials: 'omit', cache: 'no-store',
        body: JSON.stringify({ secret, principal_hint: principal() }),
      })
    } catch {
      set({ phase: 'error' })
      return 'error'
    }
    let body: { identity_token?: string; exp_unix_ns?: string; code?: number; detail?: { retry_after_s?: number } } = {}
    try {
      body = (await r.json()) as typeof body
    } catch {
      body = {}
    }
    if (r.ok && typeof body.identity_token === 'string') {
      const exp = nsToMs(body.exp_unix_ns) || Date.now() + 12 * 3600_000
      const x: Stored = { token: body.identity_token, exp_unix_ms: exp }
      writeStored(identityStore.getState().remember ? 'local' : 'session', x)
      set({ tier: 'internal', token: x.token, expUnixMs: exp, phase: 'ok', retryUntilMs: 0 })
      return 'ok'
    }
    const ra = Number(r.headers.get('retry-after'))
    const retryS = Number.isFinite(ra) && ra > 0 ? ra : typeof body.detail?.retry_after_s === 'number' ? body.detail.retry_after_s : 0
    const phase: IdentityPhase = r.status === 401 || body.code === 304 ? 'wrong'
      : body.code === 516 ? 'locked'
        : r.status === 429 ? 'rate'
          : r.status === 403 || body.code === 115 ? 'disabled'
            : 'error'
    set({ phase, retryUntilMs: retryS > 0 ? Date.now() + retryS * 1000 : 0 })
    return phase
  },

  /** R80 DELETE /api/auth/identity: revoke the jti; local state is cleared whatever the answer */
  async logout(): Promise<void> {
    const tok = identityStore.getState().token
    clearLocal()
    if (tok) await fetcher(IDENTITY_PATH, { method: 'DELETE', headers: { 'x-awr-identity': tok }, credentials: 'omit' }).catch(() => null)
  },

  /** "remember this device for 12 h": the token moves between sessionStorage and localStorage */
  setRemember(on: boolean): void {
    const s = identityStore.getState()
    set({ remember: on })
    if (!s.token) return
    const x: Stored = { token: s.token, exp_unix_ms: s.expUnixMs }
    writeStored(on ? 'local' : 'session', x)
    writeStored(on ? 'session' : 'local', null)
    if (!on) writeStored('session', x)
  },

  /** the server says the token is no longer valid (401 on an identity request): back to public */
  expire(): void {
    clearLocal()
  },
}
