// Bottom task strip of the tablet commander (R-D2-36; AWR-04 §15.8.2 item 2; AWR-14 §20.26.5; M15-FR-230). Owner: M15.
// 64 px (+ safe area): the task pills (type icon, label, state, progress; horizontally scrolling; a tap opens the task tab),
// the four quick commands acting on the selected vehicles (goto, hover, return, watch; sandbox operator and internal
// identity only, ui/mobile/QuickCommand.tsx), the simulation rate and the alarm count (opens the alerts drawer).
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { useTasking, type TaskRow } from '@/stores/tasking'
import { useSelection } from '@/stores/selection'
import { useAlarms } from '@/ui/notify/alarms'
import { useConnView } from '@/ui/shell/connView'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { PausedNote } from '@/ui/power/PowerOverlay'
import { OP_ICON, quick } from './QuickCommand'
import { QUICK_OPS } from './quickModel'

const TASK_ICON: Readonly<Record<string, IconKey>> = {
  point_watch: 'task.point', polyline_patrol: 'task.polyline', area_patrol: 'task.area', area_scan_gsd: 'task.scan', perimeter_patrol: 'task.perimeter',
  relay_watch: 'task.relay', area_guard: 'task.guard',
}

function TaskPill({ r, onOpen }: { r: TaskRow; onOpen: () => void }) {
  const t = useT()
  return (
    <Button variant="outline" size="sm" className="h-11 shrink-0 gap-1.5" onClick={onOpen} data-task-pill={r.tid}>
      <Icon icon={TASK_ICON[r.type] ?? 'task.guard'} />
      <span className="max-w-32 truncate">{r.label || r.tid}</span>
      <span className="text-muted-foreground tabular-nums">{`${Math.round(r.progressPct)}%`}</span>
      <span className="text-hud-cap text-muted-foreground">{t(`task.state.${r.state}`)}</span>
    </Button>
  )
}

export function TaskStrip({ onTasks, onAlerts, className }: { onTasks: () => void; onAlerts: () => void; className?: string }) {
  const t = useT()
  const tasks = useTasking((s) => s.tasks)
  const write = useD2Write()
  const hasSel = useSelection((s) => s.ids.length > 0)
  const crit = useAlarms((s) => s.critUnacked)
  const warn = useAlarms((s) => s.warnActive)
  const rate = useConnView((s) => s.rate)
  const active = tasks.filter((r) => r.state !== 'DONE' && r.state !== 'CANCELED' && r.state !== 'FAILED')
  return (
    <div className={cn('flex h-16 items-center gap-2 border-t bg-card px-2', className)} data-task-strip="">
      <div className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto" data-task-pills={active.length}>
        {active.length === 0 ? <span className="px-1 text-hud-sub text-muted-foreground">{t('mobile.noTasks')}</span> : null}
        {active.map((r) => <TaskPill key={r.tid} r={r} onOpen={onTasks} />)}
      </div>
      {write ? (
        <div className="flex shrink-0 items-center gap-1" data-strip-quick="">
          {QUICK_OPS.map((op) => (
            <Button key={op} size="sm" variant="outline" className="h-11 gap-1" disabled={!hasSel} onClick={() => quick.open(op)} data-strip-op={op}>
              <Icon icon={OP_ICON[op]} />
              {t(`cmd.quick.${op}`)}
            </Button>
          ))}
        </div>
      ) : null}
      <PausedNote />
      <Badge variant="outline" className="h-6 shrink-0 font-mono" data-strip-rate={rate}>{`x${rate}`}</Badge>
      <Button size="icon" variant="ghost" className="relative shrink-0" aria-label={t('tab.alerts')} onClick={onAlerts} data-strip-alerts={crit + warn}>
        <Icon icon={crit > 0 ? 'notify.ring' : 'notify'} />
        {crit + warn > 0 ? (
          <span className={cn('absolute top-0.5 right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-hud-cap font-bold',
            crit > 0 ? 'bg-brand-solid text-brand-foreground' : 'border border-brand bg-card text-brand-text')}>{crit > 0 ? crit : warn}</span>
        ) : null}
      </Button>
    </div>
  )
}
