// Application root (M15-FR-001, FR-002, §6.3.1): providers from the outside in (root error boundary, i18n, theme, Query,
// Tooltip, Toast, RtProvider), the resident WorldCanvas on the --z-canvas layer (never unmounted by routing), the router
// outlet (sandbox shell, overlay pages, pages) and the global layers. ?chrome=0 renders the canvas only (the canvas-only
// baseline of AWR-18 §9.5): no shell, no global layers and no toaster (notices take the non-DOM channel, ADR-081).
// D2.1 (ADR-122; AWR-04 §15.8.1; M15-FR-229): the shell follows the device form factor (lib/formFactor): desktop keeps the
// floating shell, tablets get the commander (ui/mobile/TabletShell), phones the single-window shell with the tab bar
// (ui/mobile/MobileShell) and windows narrower than 360 CSS px the "too narrow" Empty. Only the shell changes on a switch:
// the canvas, the realtime link, the selection and the camera stay.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { ErrorBoundaryShell } from './providers/Providers'
import { onNotFound, useRoute } from './router/router'
import { parseChrome, parseSel } from './router/search'
import { GlobalLayers } from './GlobalLayers'
import { boot } from './boot/BootController'
import { BootMask } from './boot/BootMask'
import { WorldCanvas } from '@/viewport/WorldCanvas'
import { AppHeader } from '@/ui/layout/AppHeader'
import { useShellLayout } from '@/ui/layout/layoutState'
import { BANNER_PX, useBannerCount } from '@/ui/notify/Banners'
import { Sandbox } from '@/ui/views/Sandbox'
import { prefs } from '@/stores/prefs'
import { DEMO_PUBLIC } from '@/lib/demo'
import { selection } from '@/stores/selection'
import { requestSelection } from '@/ui/shell/urlSync'
import { ViewportErrorBoundary } from './providers/ErrorBoundaries'
import { UI_ONLY } from '@/ui/testing/uiOnly'
import { installD2Shell } from './shell/install'
import { useSurface } from './shell/surface'
import { useSandboxBannerCount } from '@/ui/sandbox/banners'
import { useFormFactor } from '@/lib/formFactor'
import { TabletShell } from '@/ui/mobile/TabletShell'
import { NarrowShell, PhoneShell } from '@/ui/mobile/MobileShell'

// D2 (ADR-111): the run-time surface, the queue ticket and the automatic keepalive start with the application module
installD2Shell()

function RouterOutlet() {
  const t = useT()
  const route = useRoute()
  const banners = useBannerCount() + useSandboxBannerCount()
  const ff = useFormFactor().ff
  const shell = useShellLayout(banners * BANNER_PX, ff === 'desktop')
  React.useEffect(() => {
    document.documentElement.style.setProperty('--banner-height', `${banners * BANNER_PX}px`)
  }, [banners])
  // the sandbox route takes its world from the session (D2, ADR-111)
  const surfaceWorld = useSurface((s) => (s.surface === 'sandbox' ? s.worldId : null))
  const worldId = route?.route.id === 'world' ? route.params.id : route?.route.id === 'sandbox' ? surfaceWorld : null
  React.useEffect(() => {
    onNotFound(() => notify('route:notFound', 'info', t('route.notFound')))
  }, [t])
  // world change: remember it, drop the old selection and hand ?sel= (read at that moment only) to the world-bound deep link:
  // it is applied once this world's realtime data is live and dropped with its parameter otherwise (D2.2-W1, ui/shell/urlSync)
  const onWorld = React.useEffectEvent((id: string) => {
    prefs.setUi('lastWorld', id)
    selection.clear()
    requestSelection(id, route ? parseSel(route.search) : undefined)
  })
  React.useEffect(() => {
    if (worldId) onWorld(worldId)
  }, [worldId])
  React.useEffect(() => {
    boot.resolveGate('shell')
  }, [])
  // page title: the product name "ANet Drone4D" (the ANetResearch/ANet-Drone4D repository), prefixed with the view; the
  // public demo build names the demo site ("ANet Drone4D Public Demo", ADR-083)
  // D2 (M15-FR-205; AWR-14 §20.2.3 rule 5): "<world> · my sandbox · product" on the sandbox, "open interface · product"
  const routeId = route?.route.id ?? null
  const viewWorld = route?.params.id ?? null
  React.useEffect(() => {
    const product = t(DEMO_PUBLIC ? 'demo.product' : 'brand.product')
    if (routeId === 'sandbox') {
      document.title = t('title.sandbox', { world: surfaceWorld ?? '—', product })
      return
    }
    if (routeId === 'sandboxApi') {
      document.title = t('title.view', { view: t('title.sandboxApi'), product })
      return
    }
    const view = routeId === 'world' && viewWorld ? viewWorld : routeId && ['worlds', 'jobs', 'runs', 'report', 'reports', 'bench'].includes(routeId) ? t(`title.${routeId}`) : null
    document.title = view ? t('title.view', { view, product }) : product
  }, [routeId, viewWorld, surfaceWorld, t])
  if (!route) return null
  const layer = route.route.layer
  const Page = route.route.component
  return (
    <>
      {layer !== 'page' && ff === 'desktop' ? <AppHeader compact={shell.breakpoint === 'C'} /> : null}
      {layer !== 'page' && ff === 'desktop' ? <Sandbox shell={shell} /> : null}
      {layer !== 'page' && ff === 'tablet' ? <TabletShell /> : null}
      {layer !== 'page' && ff === 'phone' ? <PhoneShell /> : null}
      {layer !== 'page' && ff === 'narrow' ? <NarrowShell /> : null}
      {Page && layer !== 'sandbox' ? (
        <React.Suspense fallback={null}>
          <Page params={route.params} search={route.search} />
        </React.Suspense>
      ) : null}
    </>
  )
}

/**
 * ?chrome=0 (canvas-only baseline, AWR-18 §9.5; M15-FR-004): no shell and no global layers, but the boot still has to
 * reveal (the 'shell' gate resolves on the first commit, the mask node of index.html fades out), otherwise flight60
 * scene=pc never starts (the driver waits for the reveal) and the mask covers the canvas. FX-WEB1.
 */
function CanvasOnly() {
  React.useEffect(() => {
    boot.resolveGate('shell')
  }, [])
  return <BootMask />
}

export function App() {
  const route = useRoute()
  const chrome = route ? parseChrome(route.search) : true
  return (
    <ErrorBoundaryShell toasts={chrome}>
      <div data-app="" className="app-root">
        {UI_ONLY ? <div data-viewport="" className="app-layer-canvas bg-background" /> : (
          <ViewportErrorBoundary>
            <WorldCanvas />
          </ViewportErrorBoundary>
        )}
        {chrome ? <RouterOutlet /> : null}
        {chrome ? <GlobalLayers /> : <CanvasOnly />}
      </div>
    </ErrorBoundaryShell>
  )
}
