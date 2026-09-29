// window.__ux writers that follow other state (M15-FR-010, §7.1.7), dev and test builds only: selection (ids, primary),
// camera (mode, follow lock) and the visible toast count (sampled every INPUT.bridgeFlushMs from the DOM, the same
// selector the production specs use: [data-slot=toast]:not([data-limited])).
import { INPUT } from '@/lib/tokens/input.gen'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { selectionStore } from '@/stores/selection'
import { camera } from '@/viewport/facade'
import { UX } from './uxProbe'

export function installUxBindings(): () => void {
  if (!TEST_SWITCHES || typeof window === 'undefined') return () => {}
  const sel = () => {
    const s = selectionStore.getState()
    UX.selection.ids = [...s.ids]
    UX.selection.primary = s.primary
  }
  sel()
  const offSel = selectionStore.subscribe(sel)
  const offCam = camera.onMode((m) => {
    UX.camera.mode = m.mode
    UX.camera.followLock = m.followLock
  })
  const t = setInterval(() => {
    UX.toasts.visible = document.querySelectorAll('[data-slot="toast"]:not([data-limited])').length
  }, INPUT.bridgeFlushMs)
  return () => {
    offSel()
    offCam()
    clearInterval(t)
  }
}
