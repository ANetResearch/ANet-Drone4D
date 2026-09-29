// Write guards shared by buttons, menus, hotkeys and the Timeline (AWR-14 §7.8, §6.17): the reason a server-state change
// is refused right now (offline, replay, read-only), as an i18n key, or null when the session may write.
import { connViewStore, isOnline } from './connView'

/** simulation rates of the live clock (AWR-03 §8.5; commands.json sim/speed) */
export const SIM_RATES = [0.25, 0.5, 1, 2, 5, 10] as const

/** the view world of the route when it differs from the session world (static browsing, AWR-14 §6.16), else null */
export function otherWorld(): string | null {
  const s = connViewStore.getState()
  if (typeof location === 'undefined' || !s.sessionWorldId) return null
  const m = /^\/world\/([a-z0-9-]{1,63})/.exec(location.pathname)
  return m && m[1] !== s.sessionWorldId ? m[1] : null
}

export function writeDeniedKey(): string | null {
  const s = connViewStore.getState()
  if (!isOnline(s.conn)) return 'hint.offline'
  if (s.mode === 'replay') return 'hint.replay'
  if (s.role === 'viewer' || s.seat !== 'held') return 'hint.readOnly'
  if (otherWorld() !== null) return 'hint.otherWorld'
  return null
}
