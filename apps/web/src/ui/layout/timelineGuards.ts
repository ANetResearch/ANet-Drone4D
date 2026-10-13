// Transport guards and labels of the Timeline (AWR-14 §6.17, §7.8; M12 §6.5 guard table), shared by the Timeline bar,
// the Timeline tab, the Runs page and the hotkeys (no component code, so ui/actions can import it).
import { fmt } from '@/lib/format'
import { timelineGuards } from '@/stores/timeline'
import { connViewStore, isOnline } from '@/ui/shell/connView'
import { otherWorld, writeDeniedKey } from '@/ui/shell/guards'
import { manualActive } from '@/ui/manual/controller'
import { sandboxStore } from '@/ui/sandbox/session'

/** TIME.state values (AWR-03 §5.2 item 6) */
export const TS = { STOPPED: 0, PLAYING: 1, PAUSED: 2, STEPPING: 3, BUFFERING: 4, ENDED: 5, STALLED: 6, RESTARTING: 7, FAILED: 8, LIVE: 9 } as const

/** rate label: x0.25, x1, x20 */
export const rateLabel = (r: number): string => `×${fmt.num(r, r < 1 ? (r * 10 === Math.round(r * 10) ? 1 : 2) : 0)}`

/**
 * Reasons (i18n keys) of the transport controls: the session guards first (offline, other world; live also read-only and
 * replay), then M12's guard table. null = allowed.
 */
export function transportReasons(mode: 'live' | 'replay'): { play: string | null; step: string | null; seek: string | null; speed: (r: number) => string | null } {
  const g = timelineGuards()
  let session: string | null
  if (mode === 'live') session = writeDeniedKey()
  else session = !isOnline(connViewStore.getState().conn) ? 'hint.offline' : otherWorld() !== null ? 'hint.otherWorld' : null
  return {
    play: session ?? g.play,
    step: session ?? g.step,
    seek: session ?? g.seek,
    speed: (r: number) => session ?? (mode === 'live' ? sandboxSpeedKey(r) : null) ?? g.speed(r),
  }
}

/**
 * D2 (AWR-04 §4.4.5, §10.6 item 1; M15-FR-206): a rate above 1 is greyed while a vehicle is under manual control (the
 * sim locks x1, 117 MANUAL_CONTROL), and a rate above the sandbox session's reachable cap is greyed with that reason
 */
export function sandboxSpeedKey(r: number): string | null {
  if (r > 1 && manualActive()) return 'manual.rateLocked'
  const s = sandboxStore.getState()
  const cap = s.session?.clock.rate_cap
  if (s.sid && typeof cap === 'number' && r > cap + 1e-9) return 'sandbox.rateAboveCap'
  return null
}

