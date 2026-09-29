// Alarm centre (M15-FR-090; AWR-14 §4.1, §11.5): the header alarm button with its count badge and a 400 px Popover:
// segmented filter (all, critical, warning), list sorted by severity and time (virtualised beyond 100 items), each item
// with its shape icon, text, subjects (click selects and focuses), wall time and "acknowledge"; "acknowledge all" at the
// bottom. The count badge is the only element of the header that may be solid red (unacknowledged critical present,
// one-red figure `header`); warnings only show an outlined badge; no alarms hides it. A new critical plays the 03
// notification-badge pop (NotificationDot); the single shake is D1-ext.
import * as React from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { camera } from '@/viewport/facade'
import { selection } from '@/stores/selection'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Item, ItemActions, ItemContent, ItemDescription, ItemMedia, ItemTitle } from '@/ui/components/ui/item'
import { Popover, PopoverContent, PopoverTrigger } from '@/ui/components/ui/popover'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { MotionNumber } from '@/ui/motion/MotionNumber'
import { PanelEmpty } from '@/ui/brand'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { alarms, isActive, useAlarms, type AlarmItem } from './alarms'
import { useRedOwner } from './redFigures'
import { subjectsText } from './toastMerger'

type Filter = 'all' | 'critical' | 'warning'
const ROW_PX = 64

function AlarmRow({ a }: { a: AlarmItem }) {
  const t = useT()
  const crit = a.severity === 'critical'
  return (
    <Item size="sm" variant="outline" data-alarm={a.key} data-alarm-sev={a.severity} className="h-full">
      <ItemMedia className="text-brand-text">
        <Icon icon={crit ? 'alert.critical' : 'alert.warning'} />
      </ItemMedia>
      <ItemContent className="min-w-0">
        <ItemTitle className="truncate">{a.text}</ItemTitle>
        <ItemDescription className="truncate">
          {a.subjects.length ? (
            <Button variant="link" size="xs" className="h-auto p-0" onClick={() => {
              selection.select(a.subjects)
              camera.focus(a.subjects)
            }}>{subjectsText(a.subjects)}</Button>
          ) : null}
          {` ${t('alarm.at', { time: fmt.wallTime(a.lastWallMs), count: a.count })}${isActive(a) ? '' : ` · ${t('alarm.cleared')}`}`}
        </ItemDescription>
      </ItemContent>
      <ItemActions>
        {a.acked ? <Badge variant="outline">{t('alarm.acked')}</Badge> : (
          <Button size="xs" variant="outline" onClick={() => alarms.ack(a.key)}>{t('alarm.ack')}</Button>
        )}
      </ItemActions>
    </Item>
  )
}

function AlarmList({ filter }: { filter: Filter }) {
  const t = useT()
  const version = useAlarms((s) => s.version)
  // alarms is a mutable module; `version` is the cache key of the list
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- see above
  const list = React.useMemo(() => alarms.list(filter), [filter, version])
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const virtual = list.length > 100
  // oxlint-disable-next-line react/incompatible-library -- ADR-028: TanStack Virtual
  const v = useVirtualizer({ count: virtual ? list.length : 0, getScrollElement: () => scrollRef.current, estimateSize: () => ROW_PX, overscan: 6 })
  if (!list.length) return <PanelEmpty brand={false} title={t('alarm.empty')} />
  return (
    <div ref={scrollRef} className="max-h-96 overflow-auto">
      {virtual ? (
        <div style={{ height: v.getTotalSize(), position: 'relative' }}>
          {v.getVirtualItems().map((it) => (
            <div key={it.key} className="absolute inset-x-0 pb-1" style={{ transform: `translateY(${it.start}px)`, height: ROW_PX }}>
              <AlarmRow a={list[it.index]} />
            </div>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-1">{list.map((a) => <AlarmRow key={a.key} a={a} />)}</div>
      )}
    </div>
  )
}

export function AlarmCenter() {
  const t = useT()
  const open = useOverlays((s) => s.alarms)
  const crit = useAlarms((s) => s.critUnacked)
  const warn = useAlarms((s) => s.warnActive)
  const red = useRedOwner('header')
  const [filter, setFilter] = React.useState<Filter>('all')
  const count = crit > 0 ? crit : warn
  return (
    <Popover open={open} onOpenChange={(o) => overlays.set('alarms', o)}>
      <Tooltip>
        <TooltipTrigger render={
          <PopoverTrigger render={<Button size="icon-sm" variant="ghost" aria-label={t('header.alarms')} data-alarm-button="" className="relative" />} />
        }>
          <Icon icon={crit > 0 ? 'notify.ring' : 'notify'} />
          {count > 0 ? (
            <span data-figure="header-alarm" data-alarm-count={count} className={cn('absolute -top-1.5 -right-1.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-hud-cap font-bold',
              crit > 0 && red ? 'bg-brand-solid text-brand-foreground' : 'border border-brand bg-card text-brand-text')}>
              <MotionNumber value={count} format={(n) => fmt.count(n)} />
            </span>
          ) : null}
        </TooltipTrigger>
        <TooltipContent>{t('header.alarms')}</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-100 gap-2" data-alarm-center="">
        <div className="flex items-center justify-between gap-2">
          <ToggleGroup spacing={0} size="sm" variant="outline" value={[filter]} aria-label={t('alarm.filter')}
            onValueChange={(v: unknown[]) => v[0] !== undefined && setFilter(v[0] as Filter)}>
            <ToggleGroupItem value="all">{t('alarm.filter.all')}</ToggleGroupItem>
            <ToggleGroupItem value="critical">{t('alarm.filter.critical')}</ToggleGroupItem>
            <ToggleGroupItem value="warning">{t('alarm.filter.warning')}</ToggleGroupItem>
          </ToggleGroup>
          <Button size="sm" variant="ghost" onClick={() => alarms.ackAll()}>{t('alarm.ackAll')}</Button>
        </div>
        <AlarmList filter={filter} />
      </PopoverContent>
    </Popover>
  )
}
