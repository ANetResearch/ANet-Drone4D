// Phone tab bar (R-D2-37; AWR-04 §15.8.3; AWR-14 §20.26.5; M15-FR-231; UX-FR-153). Owner: M15. Five windows: viewport,
// fleet, tasks, targets, alerts; one window at a time. Portrait: a 56 px bar at the bottom (+ safe area); landscape: a
// vertical bar on the left. Each tab is at least 56 x 44 px with an icon and a label; the alerts tab carries the count of
// unacknowledged criticals (or active warnings). shadcn Tabs (TabsList only; the windows are rendered by the shell so that
// every window keeps its state while hidden).
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { Tabs, TabsList, TabsTrigger } from '@/ui/components/ui/tabs'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { useAlarms } from '@/ui/notify/alarms'

export type PhoneTab = 'viewport' | 'fleet' | 'tasks' | 'targets' | 'alerts'
export const PHONE_TABS: readonly { id: PhoneTab; icon: IconKey }[] = [
  { id: 'viewport', icon: 'tab.viewport' }, { id: 'fleet', icon: 'nav.fleet' }, { id: 'tasks', icon: 'tab.tasks' }, { id: 'targets', icon: 'target.person' },
  { id: 'alerts', icon: 'notify' },
]
export const TAB_BAR_PX = 56
export const TAB_RAIL_PX = 64

export function TabBar({ value, onChange, vertical }: { value: PhoneTab; onChange: (t: PhoneTab) => void; vertical: boolean }) {
  const t = useT()
  const crit = useAlarms((s) => s.critUnacked)
  const warn = useAlarms((s) => s.warnActive)
  const count = crit > 0 ? crit : warn
  return (
    <Tabs value={value} onValueChange={(v) => onChange(v as PhoneTab)} orientation={vertical ? 'vertical' : 'horizontal'}
      className={cn('tab-bar fixed z-(--z-header) bg-card', vertical ? 'safe-left inset-y-0 left-0 w-16 border-r' : 'safe-bottom inset-x-0 bottom-0 border-t')}
      data-tab-bar={vertical ? 'vertical' : 'bottom'}>
      <TabsList variant="line" aria-label={t('mobile.windows')} className={cn('h-auto w-full gap-0 p-0', vertical ? 'h-full flex-col justify-center' : 'h-14 justify-around')}>
        {PHONE_TABS.map((x) => (
          <TabsTrigger key={x.id} value={x.id} data-tab={x.id}
            className={cn('relative h-14 min-w-14 flex-1 flex-col gap-0.5 text-hud-cap', vertical && 'w-16 flex-none')}>
            <Icon icon={x.id === 'alerts' && crit > 0 ? 'notify.ring' : x.icon} />
            <span>{t(`tab.${x.id}`)}</span>
            {x.id === 'alerts' && count > 0 ? (
              <span data-tab-badge={count} className={cn('absolute top-1 right-2 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-hud-cap font-bold',
                crit > 0 ? 'bg-brand-solid text-brand-foreground' : 'border border-brand bg-card text-brand-text')}>{count}</span>
            ) : null}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  )
}
