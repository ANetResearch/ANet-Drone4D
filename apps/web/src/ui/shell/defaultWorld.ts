// Provisional default world (ADR-077; M15-FR-003): "/" without a remembered world redirects to /world/shenzhen, but a
// backend without UrbanScene3D data runs the generated demo city (synthcity) instead. The first serverInfo then replaces
// that provisional route with the session world. Only the provisional redirect is corrected: an explicit /world/<id> or
// a route the user has already left stays as it is (static browsing, AWR-14 §6.16).
import { navigate } from '@/app/router/router'
import { connViewStore } from './connView'

let pending: string | null = null
let unsub: (() => void) | null = null

function settle(session: string): void {
  const w = pending
  pending = null
  unsub?.()
  unsub = null
  if (!w || session === w || typeof location === 'undefined' || location.pathname !== `/world/${w}`) return
  navigate(`/world/${session}${location.search}`, { replace: true })
}

/** called by the "/" redirect when it falls back to the built-in default world */
export function markDefaultRedirect(world: string): void {
  pending = world
  if (unsub) return
  unsub = connViewStore.subscribe((s) => {
    if (s.sessionWorldId) settle(s.sessionWorldId)
  })
  // the redirect runs inside the router's resolve(): never navigate synchronously from here
  const now = connViewStore.getState().sessionWorldId
  if (now) queueMicrotask(() => settle(now))
}
