// Application root (M15-FR-001, FR-002, §6.3.1): providers from the outside in (root error boundary, i18n, theme, Query,
// Tooltip, Toast, RtProvider), the resident WorldCanvas on the --z-canvas layer (never unmounted by routing), the router
// outlet (sandbox shell, overlay pages, pages) and the global layers. ?chrome=0 renders the canvas only (the canvas-only
// baseline of AWR-18 §9.5).
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
import { selection } from '@/stores/selection'
import { ViewportErrorBoundary } from './providers/ErrorBoundaries'
import { UI_ONLY } from '@/ui/testing/uiOnly'

function RouterOutlet() {
  const t = useT()
  const route = useRoute()
  const banners = useBannerCount()
  const shell = useShellLayout(banners * BANNER_PX)
  React.useEffect(() => {
    document.documentElement.style.setProperty('--banner-height', `${banners * BANNER_PX}px`)
  }, [banners])
  const worldId = route?.route.id === 'world' ? route.params.id : null
  React.useEffect(() => {
    onNotFound(() => notify('route:notFound', 'info', t('route.notFound')))
  }, [t])
  // world change: remember it, drop the old selection and restore ?sel= from the URL (read at that moment only)
  const onWorld = React.useEffectEvent((id: string) => {
    prefs.setUi('lastWorld', id)
    selection.clear()
    const sel = route ? parseSel(route.search) : undefined
    if (sel) selection.select(sel)
  })
  React.useEffect(() => {
    if (worldId) onWorld(worldId)
  }, [worldId])
  React.useEffect(() => {
    boot.resolveGate('shell')
  }, [])
  // page title: the product name "ANet Drone4D" (the ANetResearch/ANet-Drone4D repository), prefixed with the view
  const routeId = route?.route.id ?? null
  const viewWorld = route?.params.id ?? null
  React.useEffect(() => {
    const product = t('brand.product')
    const view = routeId === 'world' && viewWorld ? viewWorld : routeId && ['worlds', 'jobs', 'runs', 'report', 'reports', 'bench'].includes(routeId) ? t(`title.${routeId}`) : null
    document.title = view ? t('title.view', { view, product }) : product
  }, [routeId, viewWorld, t])
  if (!route) return null
  const layer = route.route.layer
  const Page = route.route.component
  return (
    <>
      {layer !== 'page' ? <AppHeader compact={shell.breakpoint === 'C'} /> : null}
      {layer !== 'page' ? <Sandbox shell={shell} /> : null}
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
    <ErrorBoundaryShell>
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
