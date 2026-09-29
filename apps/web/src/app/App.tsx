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
        {chrome ? <GlobalLayers /> : null}
      </div>
    </ErrorBoundaryShell>
  )
}
