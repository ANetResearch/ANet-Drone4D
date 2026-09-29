// URL state (M15-FR-004, FR-009; AWR-14 §2.2): the selection (`sel`, at most LIMITS.selectionUrlMax ids) and the camera
// mode (`cam`) are written back with replaceState at INPUT.urlSyncHz (1 Hz), parameters of other modules untouched. On a
// deep link the camera mode is restored once the canvas is revealed; ids of `sel` that the roster does not know are
// dropped once the roster arrives, with one toast telling how many (the selection itself is restored by the router).
import { t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { updateSearch } from '@/app/router/router'
import { parseCam, parseSel } from '@/app/router/search'
import { INPUT, LIMITS } from '@/lib/tokens/input.gen'
import { rtClient } from '@/net/rt'
import { selection, selectionStore } from '@/stores/selection'
import { camera, type CameraMode } from '@/viewport/facade'

const PERIOD_MS = 1000 / INPUT.urlSyncHz

/** the sel parameter of a selection (null when empty or too long for the URL) */
export function selParam(ids: readonly string[]): string | null {
  return ids.length === 0 || ids.length > LIMITS.selectionUrlMax ? null : ids.join(',')
}

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
  const timer = setInterval(() => {
    if (!dirty || typeof location === 'undefined' || !location.pathname.startsWith('/world/')) return
    dirty = false
    const q = new URLSearchParams(location.search)
    const sel = selParam(selectionStore.getState().ids)
    const camQ = cam === 'orbit' ? null : cam
    if (q.get('sel') !== sel || q.get('cam') !== camQ) updateSearch({ sel, cam: camQ })
  }, PERIOD_MS)
  // deep link: camera mode after reveal; unknown vehicles dropped when the roster arrives (one toast)
  const q0 = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams()
  const cam0 = parseCam(q0)
  if (cam0 && cam0 !== 'orbit') onRevealed(() => void camera.setMode(cam0))
  const sel0 = parseSel(q0) ?? []
  let rosterCheck: ReturnType<typeof setInterval> | null = sel0.length
    ? setInterval(() => {
      const rt = rtClient()
      if (!rt || rt.roster.size === 0) return
      if (rosterCheck) clearInterval(rosterCheck)
      rosterCheck = null
      const missing = sel0.filter((id) => rt.roster.agentNoOf(id) < 0)
      if (missing.length) {
        selection.prune((id) => rt.roster.agentNoOf(id) >= 0)
        notify('url:sel', 'info', t('url.selMissing', { n: missing.length }))
      }
    }, INPUT.bridgeFlushMs)
    : null
  return () => {
    offSel()
    offCam()
    clearInterval(timer)
    if (rosterCheck) clearInterval(rosterCheck)
  }
}
