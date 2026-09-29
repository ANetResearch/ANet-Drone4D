// Sandbox view, /world/:id (M15-FR-012, AWR-14 §5.2): the floating shell over the resident canvas: left rail, right rail
// (DroneRail), bottom Dock, Timeline bar and the viewport overlay. The canvas is mounted by app/App.tsx and never moves;
// only the unobscured rect changes. Below 1280 x 720 the full-viewport Empty replaces the shell and the viewport is
// suspended (M15-FR-007).
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

export function Sandbox({ shell }: { shell: ReturnType<typeof useShellLayout> }) {
  const t = useT()
  React.useEffect(() => {
    frameCap.suspend('small-window', shell.small)
    return () => frameCap.suspend('small-window', false)
  }, [shell.small])
  React.useEffect(() => {
    UX.layout.breakpoint = shell.breakpoint
  }, [shell.breakpoint])
  if (shell.small) return <FullEmptyState title={t('shell.tooSmall')} description={t('shell.tooSmallHint')} narrow />
  const compact = shell.breakpoint === 'C'
  return (
    <div data-view="sandbox">
      <ViewportOverlay compactBp={compact} />
      <Banners />
      <WorldSidebar />
      <DroneRail />
      <BottomDock viewportW={shell.w} viewportH={shell.h} />
      <TimelineBar compact={compact} />
    </div>
  )
}
