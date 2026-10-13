// URL state (M15-FR-004, FR-009; AWR-14 §2.2): the selection (`sel`, at most LIMITS.selectionUrlMax ids) and the camera
// mode (`cam`) are written back with replaceState at INPUT.urlSyncHz (1 Hz), parameters of other modules untouched. On a
// deep link the camera mode is restored once the canvas is revealed.
// D2.2-W1: a `sel` deep link belongs to the world of its route (requestSelection, called by the router outlet on every world
// change): it is applied once the world scope is live for that world and the roster has arrived (ids the roster does not
// know are dropped with one toast telling how many), and it is dropped together with its URL parameter when that world has
// no realtime source (static browsing) or its source simulates another world; a later world change replaces it.
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { updateSearch } from '@/app/router/router'
import { parseCam } from '@/app/router/search'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { freezableInterval } from '@/lib/powerFreeze'
import { rtClient } from '@/net/rt'
import { selection, selectionStore } from '@/stores/selection'
import { worldScopeNow, worldScopeStore, type WorldScopeState } from '@/stores/worldScope'
import { camera, type CameraMode } from '@/viewport/facade'

const PERIOD_MS = 1000 / INPUT.urlSyncHz

/** the sel parameter of a selection (null when empty or too long for the URL) */
export function selParam(ids: readonly string[]): string | null {
  return ids.length === 0 || ids.length > LIMITS.selectionUrlMax ? null : ids.join(',')
}

// ------------------------------------------------------------ world-bound `sel` deep link
let pendingSel: { world: string; ids: string[] } | null = null
let pendingTimer: ReturnType<typeof setInterval> | null = null
let offScope: (() => void) | null = null

function stopPending(): void {
  pendingSel = null
  if (pendingTimer) clearInterval(pendingTimer)
  pendingTimer = null
  offScope?.()
  offScope = null
}

/** the `sel` parameter leaves the current URL (silent: no route subscriber re-renders) */
function dropSelParam(): void {
  if (typeof location === 'undefined') return
  if (new URLSearchParams(location.search).has('sel')) updateSearch({ sel: null }, { silent: true })
}

/** one check of the pending deep link against the world scope and the roster (exported for tests) */
export function checkPendingSelection(sc: WorldScopeState = worldScopeNow()): void {
  const p = pendingSel
  if (!p) return
  if (!sc.bound) {
    stopPending()
    selection.select(p.ids)
    return
  }
  if (sc.world !== p.world) return // the view has not opened the link's world yet
  if (sc.source === null || sc.foreign) {
    // static browsing, or the source simulates another world: no entity of this link exists here
    stopPending()
    dropSelParam()
    return
  }
  const rt = rtClient()
  if (!sc.live || !rt || rt.roster.size === 0) return
  stopPending()
  const known = p.ids.filter((id) => rt.roster.agentNoOf(id) >= 0)
  if (known.length) selection.select(known)
  if (known.length < p.ids.length) {
    notify('url:sel', 'info', t('url.selMissing', { n: p.ids.length - known.length }))
    if (!known.length) dropSelParam()
  }
}

/**
 * the `sel` deep link of a world (router outlet, on every world change): replaces any pending one; applied or dropped by
 * checkPendingSelection (scope changes and a 4 Hz check while the roster is awaited)
 */
export function requestSelection(world: string, ids: readonly string[] | undefined): void {
  stopPending()
  if (!ids || ids.length === 0) return
  pendingSel = { world, ids: [...ids] }
  offScope = worldScopeStore.subscribe((sc) => checkPendingSelection(sc))
  pendingTimer = setInterval(() => checkPendingSelection(), INPUT.bridgeFlushMs)
  checkPendingSelection()
}

/** tests: the pending deep link (null when none) */
export const pendingSelection = (): { world: string; ids: readonly string[] } | null => pendingSel

export function installUrlSync(onRevealed: (cb: () => void) => void): () => void {
  let dirty = false
  let cam: CameraMode = camera.mode
  const offSel = selectionStore.subscribe(() => {
    dirty = true
  })
  const offCam = camera.onMode((m) => {
    if (m.mode !== cam) {
      cam = m.mode
      dirty = true
    }
  })
  // frozen with the power-save mode (nothing selects or moves the camera mode then; D2-AC-44, D2-AC-46)
  const stopTimer = freezableInterval(() => {
    if (!dirty || typeof location === 'undefined' || !location.pathname.startsWith('/world/')) return
    dirty = false
    const q = new URLSearchParams(location.search)
    const sel = selParam(selectionStore.getState().ids)
    const camQ = cam === 'orbit' ? null : cam
    // silent: the URL mirrors the selection and camera mode, no route subscriber needs to re-render (ADR-069)
    if (q.get('sel') !== sel || q.get('cam') !== camQ) updateSearch({ sel, cam: camQ }, { silent: true })
  }, PERIOD_MS)
  // deep link: camera mode after reveal (the `sel` link is world-bound: requestSelection)
  const q0 = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams()
  const cam0 = parseCam(q0)
  if (cam0 && cam0 !== 'orbit') onRevealed(() => void camera.setMode(cam0))
  return () => {
    offSel()
    offCam()
    stopTimer()
    stopPending()
  }
}
