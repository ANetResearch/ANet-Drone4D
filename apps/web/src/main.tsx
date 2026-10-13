// Entry (M15-FR-001): installs the probes (__perf.ui, dev/test __ux), the motion tier, routes, panels, actions and hotkeys,
// starts the boot controller and the router, then mounts <App>. StrictMode only in dev builds.
// D2.1 (M15-FR-223 to FR-240): the boot capacity line takes over the early module's request at once (app/boot/capacity.ts), the
// version line copies the build information, the form factor attributes are written before the first render, the power-
// save controller, the internal surface and the field-view preferences are installed.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/index.css'
import { App } from '@/app/App'
import { boot } from '@/app/boot/BootController'
import { startRouter } from '@/app/router/router'
import { parseChrome, parseTier } from '@/app/router/search'
import { installRoutes } from '@/app/router/table'
import { registerExtPanels, registerCorePanels } from '@/ui/panels/builtin'
import { registerBuiltinActions } from '@/ui/actions/builtin'
import { installHotkeys } from '@/ui/hotkeys/registry'
import { installToolWatcher } from '@/ui/tools/toolMode'
import { installGovernorToasts } from '@/ui/hud/governorToasts'
import { recordTtfp } from '@/app/query/options'
import { initMotionTier, setGovernorMotion, setUserMotion } from '@/ui/motion/tier'
import { prewarmIcons } from '@/ui/icons/prewarm'
import { installLayoutClock } from '@/ui/notify/afterLayout'
import { installPerfUi, PERF_UI } from '@/ui/shell/perfUi'
import { installUx } from '@/ui/testing/uxProbe'
import { installUxBindings } from '@/ui/testing/uxBindings'
import { installUiOnly } from '@/ui/testing/uiOnly'
import { installUrlSync } from '@/ui/shell/urlSync'
import { loop, perfProbe } from '@/engine'
import { rollStoreWrites } from '@/lib/createStore'
import { freezableInterval } from '@/lib/powerFreeze'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { INPUT } from '@/lib/tokens/input.gen'
import { perfStore } from '@/stores/perf'
import { prefsStore } from '@/stores/prefs'
import { installBootCapacity } from '@/app/boot/capacity'
import { installFormFactor, onFormFactor, formFactorNow } from '@/lib/formFactor'
import { installPowerController } from '@/app/power/PowerController'
import { installInternalSurface } from '@/app/shell/internal'
import { installFieldPrefs } from '@/ui/field/FieldToolbar'
import { perceptionActions } from '@/stores/perception'
import { copyText, diagnostics } from '@/lib/build'
import { t } from '@/app/i18n'
import { UX } from '@/ui/testing/uxProbe'
import { installD21Hooks } from '@/ui/testing/d21Hooks'

// D2.1: the capacity line and the form factor first (the mask is still up; neither gates the reveal)
installBootCapacity()
installFormFactor()
{
  const ff = formFactorNow()
  UX.ff.ff = ff.ff
  UX.ff.coarse = ff.coarse
  UX.ff.orient = ff.orient
  perceptionActions.setDeviceClass(ff.device)
  onFormFactor((s) => {
    UX.ff.ff = s.ff
    UX.ff.coarse = s.coarse
    UX.ff.orient = s.orient
    UX.ff.changes++
    perceptionActions.setDeviceClass(s.device)
  })
}
// the local build has no demo entry: the version line copies the build information here (app/demo/entry.tsx in the demo)
if (!import.meta.env.VITE_AWR_DEMO) {
  const el = document.querySelector<HTMLElement>('#boot-mask .boot-version')
  if (el && !el.title) {
    el.title = t('build.copy')
    const copy = (e: Event): void => {
      e.preventDefault()
      void copyText(diagnostics())
    }
    el.addEventListener('click', copy)
    el.addEventListener('contextmenu', copy)
  }
}

// the post-layout clock of toast delivery first: its ResizeObserver must be the first of the page (ADR-081)
installLayoutClock()
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
installInternalSurface()
installFieldPrefs()
installPowerController()
installD21Hooks()
installToolWatcher()
installGovernorToasts()

// render tier and motion cap from stores/perf (M06); ?tier= forces the tier in dev and test builds (ADR-044)
const forced = parseTier(new URLSearchParams(location.search))
// the perf summary is written in the UI tick (about once a second with new values): the root attributes are written only
// when they change, because every write on <html> queues a mutation and invalidates the attribute selectors of the whole
// document (P4-UI, D1-AC-23)
let appliedTier: string | null = null
let appliedCap: string | null | undefined
function applyPerf() {
  const p = perfStore.getState()
  const tier = forced ?? p.tier
  const key = `${tier ?? ''}|${p.deviceClass ?? ''}`
  const cap = p.motionCap ?? (tier === 'B' || tier === 'A' ? 'full' : null)
  if (key === appliedTier && cap === appliedCap) return
  if (key !== appliedTier) {
    appliedTier = key
    if (document.documentElement.dataset.tier !== (tier ?? '')) document.documentElement.dataset.tier = tier ?? ''
    if (tier) loop.setTier(tier, p.deviceClass ?? 'software')
  }
  appliedCap = cap
  setGovernorMotion(cap)
}
perfStore.subscribe(applyPerf)
applyPerf()
prefsStore.subscribe((s, prev) => {
  if (s.ui.motion !== prev.ui.motion) setUserMotion(s.ui.motion)
})
// frozen with the power-save mode: no store writes happen then (D2-AC-44, D2-AC-46; d2-FX-R2-sandbox-to-M15 item 1)
freezableInterval(() => rollStoreWrites(PERF_UI), INPUT.storeWriteWindowMs)

boot.start()
loop.onRunningChange((running) => {
  if (running) boot.resolveGate('canvas')
})
// ?chrome=0 (canvas only) has no icons to morph: no idle warm-up slices there (ADR-081)
const chromeOn = parseChrome(new URLSearchParams(location.search))
boot.subscribe((s) => {
  if (s !== 'REVEALED') return
  if (chromeOn) prewarmIcons()
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
