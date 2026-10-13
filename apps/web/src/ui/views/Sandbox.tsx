// Sandbox view, /world/:id (M15-FR-012, AWR-14 §5.2): the floating shell over the resident canvas: left rail, right rail
// (DroneRail), bottom Dock, Timeline bar and the viewport overlay. The canvas is mounted by app/App.tsx and never moves;
// only the unobscured rect changes. Below 360 CSS px wide (D2.1, ADR-122; D1: 1280 x 720) the full-viewport Empty replaces
// the shell and the viewport is suspended (M15-FR-007); tablets and phones get ui/mobile instead of this view.
// D2 (ADR-111; M15-FR-202, FR-208; AWR-14 §20.4.7): the same shell serves /sandbox/:sid; without this browser's token for
// the sid the "session unavailable" page replaces it (no realtime link: back to the shared show or open a sandbox). The
// sandbox dialogs (start, template, world switch, ended) and the onboarding steps mount with the shell.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { frameCap } from '@/ui/shell/frameCap'
import { FullEmptyState } from '@/ui/brand'
import { BottomDock } from '@/ui/layout/BottomDock'
import { DroneRail } from '@/ui/layout/DroneRail'
import { TimelineBar } from '@/ui/layout/TimelineBar'
import { ViewportOverlay } from '@/ui/layout/ViewportOverlay'
import { WorldSidebar } from '@/ui/layout/WorldSidebar'
import { Banners } from '@/ui/notify/Banners'
import type { useShellLayout } from '@/ui/layout/layoutState'
import { UX } from '@/ui/testing/uxProbe'
import { useRoute } from '@/app/router/router'
import { BookmarkEditor } from '@/ui/panels/timeline/BookmarkEditor'
import { consumeReplayDeepLink } from './replayFlow'
import { useSurface } from '@/app/shell/surface'
import { navigate } from '@/app/router/router'
import { DEMO_ENTRY } from '@/lib/demo'
import { Button } from '@/ui/components/ui/button'
import { SandboxDialogsHost, sandboxDialogs } from '@/ui/sandbox/dialogs'
import { Onboarding } from '@/ui/sandbox/Onboarding'

export function Sandbox({ shell }: { shell: ReturnType<typeof useShellLayout> }) {
  const t = useT()
  React.useEffect(() => {
    frameCap.suspend('small-window', shell.small)
    return () => frameCap.suspend('small-window', false)
  }, [shell.small])
  React.useEffect(() => {
    UX.layout.breakpoint = shell.breakpoint
  }, [shell.breakpoint])
  // replay deep link (/world/:id/replay/:run redirects here with one-shot ?replay=&seg=&t=)
  const route = useRoute()
  const replayParam = route?.search.get('replay') ?? null
  React.useEffect(() => {
    if (replayParam && route) void consumeReplayDeepLink(route.search)
  }, [replayParam, route])
  const surface = useSurface((s) => s.surface)
  if (shell.small) return <FullEmptyState title={t('ff.narrow')} description={t('shell.tooSmallHint')} narrow />
  if (surface === 'sandbox-gone') return <SandboxUnavailable />
  const compact = shell.breakpoint === 'C'
  return (
    <div data-view="sandbox">
      <ViewportOverlay compactBp={compact} />
      <Banners compact={compact} />
      <SandboxDialogsHost />
      <Onboarding />
      <WorldSidebar />
      <DroneRail />
      <BottomDock viewportW={shell.w} viewportH={shell.h} />
      <TimelineBar compact={compact} />
      <BookmarkEditor />
    </div>
  )
}

/** /sandbox/:sid without this browser's token (also the phone and tablet shells) */
export function SandboxUnavailable() {
  const t = useT()
  return (
    <>
      <FullEmptyState title={t('sandbox.unavailable.title')} description={t('sandbox.unavailable.desc')} narrow
        action={
          <span className="flex gap-2" data-sandbox-unavailable="">
            <Button size="sm" variant="outline" onClick={() => navigate(DEMO_ENTRY)}>{t('static.backToShow')}</Button>
            <Button size="sm" onClick={() => sandboxDialogs.open('start')}>{t('landing.sandbox.open')}</Button>
          </span>
        } />
      <SandboxDialogsHost />
    </>
  )
}
