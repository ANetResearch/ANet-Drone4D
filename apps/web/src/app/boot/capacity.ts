// Boot capacity line, application side (R-D2-32; ADR-119; AWR-04 §15.5.3; M15-FR-225; UX-FR-150). Owner: M15.
// The early boot module already sent the first public capacity request before the application chunks arrived and keeps
// the line up to date until the application runs (app/boot/early.ts, window.__awrBootCapacity; every build since
// D2-FX-R2-web, D2-AC-41). installBootCapacity() hands that request over
// to M17's stores/capacity: the store's first fetch returns the request already in flight (no second request, so the 5 s
// spacing of the mask period holds), then the store refreshes every 5 s until the reveal and afterwards only while a
// visible consumer holds a polling lease (landing, queue cards, power overlay, start Sheet). The mask line follows the
// store; the reveal stops the mask cadence. Never gates the reveal. Without a handoff (the early module did not run) the
// store requests itself; the local build only shows a successful answer (a local build normally has no sandbox-api; a 404
// shows nothing).
import { adoptBootCapacity, capacityStore, startBootCapacity, stopBootCapacity } from '@/stores/capacity'
import { BUILD } from '@/lib/build'
import { UX } from '@/ui/testing/uxProbe'
import { boot } from './BootController'
import { capacityLine, writeCapacityLine, type BootCapacityHandoff } from '@/app/i18n/capacity'

let installed = false

function handoff(): BootCapacityHandoff | null {
  return (globalThis as { __awrBootCapacity?: BootCapacityHandoff }).__awrBootCapacity ?? null
}

/** the capacity line of the mask from the store (demo: also "unavailable"; local: successful answers only) */
function writeFromStore(el: HTMLElement | null): void {
  const s = capacityStore.getState()
  if (s.phase === 'ok' && s.data) writeCapacityLine(el, capacityLine(s.data))
  else if (s.phase === 'unavailable' && BUILD.channel !== 'local') writeCapacityLine(el, capacityLine(null))
  UX.boot.capacity = s.phase === 'ok' ? (s.data?.hint ?? 'ok') : s.phase
}

/** start-up (main.tsx, before the router): idempotent; returns the stop function (tests) */
export function installBootCapacity(): () => void {
  if (installed || typeof document === 'undefined') return () => {}
  installed = true
  const h = handoff()
  if (h) {
    h.stop()
    // the first fetch of the store reuses the entry's request in flight (or its last answer); later ones go out normally
    adoptBootCapacity(h.current ?? (h.last ? Promise.resolve(h.last) : null))
  }
  const el = document.querySelector<HTMLElement>('#boot-mask .boot-capacity')
  const off = capacityStore.subscribe(() => writeFromStore(el))
  startBootCapacity()
  const offBoot = boot.subscribe((s) => {
    if (s !== 'REVEALED' && s !== 'BOOT_ERROR') return
    stopBootCapacity()
    off()
  })
  return () => {
    installed = false
    off()
    offBoot()
    stopBootCapacity()
  }
}
