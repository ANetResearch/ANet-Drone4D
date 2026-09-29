// Entry (M15-FR-001): installs the probes (__perf.ui, dev/test __ux), the motion tier, routes, panels, actions and hotkeys,
// starts the boot controller and the router, then mounts <App>. StrictMode only in dev builds.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/index.css'
import { App } from '@/app/App'
import { boot } from '@/app/boot/BootController'
import { startRouter } from '@/app/router/router'
import { parseTier } from '@/app/router/search'
import { installRoutes } from '@/app/router/table'
import { registerExtPanels, registerCorePanels } from '@/ui/panels/builtin'
import { registerBuiltinActions } from '@/ui/actions/builtin'
import { installHotkeys } from '@/ui/hotkeys/registry'
import { installToolWatcher } from '@/ui/tools/toolMode'
import { installGovernorToasts } from '@/ui/hud/governorToasts'
import { recordTtfp } from '@/app/query/options'
import { initMotionTier, setGovernorMotion, setUserMotion } from '@/ui/motion/tier'
import { prewarmIcons } from '@/ui/icons/prewarm'
import { installPerfUi, PERF_UI } from '@/ui/shell/perfUi'
import { installUx } from '@/ui/testing/uxProbe'
import { installUxBindings } from '@/ui/testing/uxBindings'
import { installUiOnly } from '@/ui/testing/uiOnly'
import { installUrlSync } from '@/ui/shell/urlSync'
import { loop, perfProbe } from '@/engine'
import { rollStoreWrites } from '@/lib/createStore'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { INPUT } from '@/lib/tokens/input.gen'
import { perfStore } from '@/stores/perf'
import { prefsStore } from '@/stores/prefs'

installPerfUi()
installUx()
installUxBindings()
initMotionTier()
setUserMotion(prefsStore.getState().ui.motion)
installRoutes()
registerCorePanels()
if (TEST_SWITCHES && new URLSearchParams(location.search).get('ext') === '1') registerExtPanels()
registerBuiltinActions()
installHotkeys()
installToolWatcher()
installGovernorToasts()

// render tier and motion cap from stores/perf (M06); ?tier= forces the tier in dev and test builds (ADR-044)
const forced = parseTier(new URLSearchParams(location.search))
function applyPerf() {
  const p = perfStore.getState()
  const tier = forced ?? p.tier
  document.documentElement.dataset.tier = tier ?? ''
  if (tier) loop.setTier(tier, p.deviceClass ?? 'software')
  setGovernorMotion(p.motionCap ?? (tier === 'B' || tier === 'A' ? 'full' : null))
}
perfStore.subscribe(applyPerf)
applyPerf()
prefsStore.subscribe((s, prev) => {
  if (s.ui.motion !== prev.ui.motion) setUserMotion(s.ui.motion)
})
setInterval(() => rollStoreWrites(PERF_UI), INPUT.storeWriteWindowMs)

boot.start()
loop.onRunningChange((running) => {
  if (running) boot.resolveGate('canvas')
})
boot.subscribe((s) => {
  if (s !== 'REVEALED') return
  prewarmIcons()
  // World Hub "last TTFP" is a local record of this browser (not part of the REST contract)
  const m = /^\/world\/([a-z0-9-]{1,63})/.exec(location.pathname)
  if (m) recordTtfp(m[1], perfProbe().load.ttfp)
})
startRouter()
installUiOnly()
installUrlSync((cb) => {
  const un = boot.subscribe((s) => {
    if (s !== 'REVEALED') return
    queueMicrotask(() => un())
    cb()
  })
})

const root = createRoot(document.getElementById('root')!)
root.render(import.meta.env.DEV ? <StrictMode><App /></StrictMode> : <App />)
