// Full-viewport empty state with the badge (M15-FR-110; AWR-15 §4.2): shown for windows below 1280 x 720 (with the
// viewport suspended, M15-FR-007) and when no world can be opened. Solid g950 surface, badge 480 px (320 below 1280).
import type * as React from 'react'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyTitle } from '@/ui/components/ui/empty'
import { BrandBadge } from './BrandBadge'

export function FullEmptyState({ title, description, action, narrow }: { title: string; description?: string; action?: React.ReactNode; narrow?: boolean }) {
  return (
    <div data-empty="full" className="app-layer-mask flex items-center justify-center bg-background">
      <Empty className="gap-6">
        <BrandBadge width={narrow ? 320 : 480} enter />
        <EmptyHeader>
          <EmptyTitle className="text-ed-title">{title}</EmptyTitle>
          {description ? <EmptyDescription>{description}</EmptyDescription> : null}
        </EmptyHeader>
        {action ? <EmptyContent>{action}</EmptyContent> : null}
      </Empty>
    </div>
  )
}
