// Panel empty state (M15-FR-110; AWR-15 §4.2): containers narrower than 348 px use the 48 px avatar mark instead of the
// badge. shadcn Empty with the avatar as media, a title and an optional description and action.
import type * as React from 'react'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/ui/components/ui/empty'
import { cn } from '@/lib/utils'
import { BRAND } from './assets'

export function PanelEmpty({ title, description, action, className, brand = true }: { title: string; description?: string; action?: React.ReactNode; className?: string; brand?: boolean }) {
  return (
    <Empty data-empty="" className={cn('gap-3 p-4', className)}>
      <EmptyHeader>
        {brand ? (
          <EmptyMedia>
            <img src={BRAND.avatar96} width={48} height={48} alt="" aria-hidden="true" data-brand="avatar" className="size-12" />
          </EmptyMedia>
        ) : null}
        <EmptyTitle className="text-hud-title">{title}</EmptyTitle>
        {description ? <EmptyDescription className="text-hud-sub">{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}
