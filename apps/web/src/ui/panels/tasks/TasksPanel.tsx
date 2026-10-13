// Dock "tasks" tab (R-D2-03 to R-D2-06, R-D2-19; M15-FR-214; UX-FR-130; AWR-14 §20.9.4, §20.5.5): replaces the D1 mission
// tab in sandboxes and the D2 shared show (runs with the `tasking` capability). Toolbar: "new task" (operator, opens right
// rail page 3), the planning layer switch when the layers store has it, a state filter. The table (LfTable table.log,
// 1 Hz from stores/tasking.ts): task (type icon, label, id), state, vehicles, progress (20-tick bar: scan visible-cell
// coverage, patrols the latest visit coverage, relays the monitoring coverage), completion, detection action and the relay
// KPI; the latest FAILED or INFEASIBLE row is the table's hot cell. A row click selects the task (planning layer
// highlight) and opens its detail under the table: work items with their vehicles and states, children (area guard's
// patrol, perimeter and spawned relays), warnings. The row menu starts, pauses, resumes, cancels (confirmed), focuses the
// task's vehicles and opens the relay tab. Reassignments are not toasted (AWR-14 §20.9.4: events only). The shared show
// sees the S7 tasks read-only.
import * as React from 'react'
import { reasonText, tOr, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { camera } from '@/viewport/facade'
import { layers as layersActions, useLayers, type LayerId } from '@/stores/layers'
import { prefs } from '@/stores/prefs'
import { selection } from '@/stores/selection'
import { taskingActions, useTasking, type TaskRow } from '@/stores/tasking'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/ui/components/ui/dropdown-menu'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Switch } from '@/ui/components/ui/switch'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { taskDraft } from './taskDraft'
import { filterTasks, hotRow, mmss, opsFor, progressOf, TASK_ICON, vehiclesOf, warningKey, type TaskFilter } from './taskModel'

type Op = 'start' | 'pause' | 'resume' | 'cancel'

async function control(tid: string, op: Op, label: string): Promise<void> {
  const r = await taskingActions.control(tid, op)
  if (!r.ok) notify(`task:${tid}:${op}`, 'warning', `${label} · ${r.code ? reasonText(r.code).short : ''}`)
}

/** open the relay tab on a task */
export function openRelay(tid: string): void {
  taskingActions.select(tid)
  prefs.setLayout({ dock: { open: true, tab: 'relay' } })
}

function RowMenu({ r, canEdit }: { r: TaskRow; canEdit: boolean }) {
  const t = useT()
  const ops = opsFor(r.state)
  const name = r.label || t(`task.type.${r.type}`)
  const run = (op: Op) => void control(r.tid, op, t('task.op.done', { op: t(`task.op.${op}`), task: name }))
  const uavs = vehiclesOf(r)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon-xs" variant="ghost" aria-label={t('task.rowMenu', { task: name })} data-task-menu={r.tid} />}>
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-mono">{r.tid}</DropdownMenuLabel>
          {canEdit ? (
            <>
              <DropdownMenuItem disabled={!ops.start} onClick={() => run('start')}><Icon icon="mission.start" />{t('task.op.start')}</DropdownMenuItem>
              <DropdownMenuItem disabled={!ops.pause} onClick={() => run('pause')}><Icon icon="mission.paused" />{t('task.op.pause')}</DropdownMenuItem>
              <DropdownMenuItem disabled={!ops.resume} onClick={() => run('resume')}><Icon icon="mission.start" />{t('task.op.resume')}</DropdownMenuItem>
              <DropdownMenuItem disabled={!ops.cancel} data-task-cancel={r.tid} onClick={() => confirmThen({
                id: 'task.cancel', title: t('task.cancelTitle', { task: name }), body: t('task.cancelBody'), action: t('task.op.cancel'),
                run: () => run('cancel'),
              })}><Icon icon="mission.abort" />{t('task.op.cancel')}</DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          ) : null}
          <DropdownMenuItem disabled={uavs.length === 0} onClick={() => {
            selection.select(uavs)
            camera.focus(uavs)
          }}><Icon icon="cmd.track" />{t('task.focus')}</DropdownMenuItem>
          {r.type === 'relay_watch' ? <DropdownMenuItem onClick={() => openRelay(r.tid)}><Icon icon="task.relay" />{t('task.openRelay')}</DropdownMenuItem> : null}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** the selected task's work items, children and warnings (the row's "view allocation") */
function TaskDetail({ r, rows }: { r: TaskRow; rows: readonly TaskRow[] }) {
  const t = useT()
  const children = rows.filter((x) => r.children.includes(x.tid) || x.parent === r.tid)
  return (
    <div className="flex flex-col gap-1.5 rounded-md bg-muted/40 p-2 text-hud-sub" data-task-detail={r.tid}>
      <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('task.detail.items', { n: r.items.length })}</span>
      {r.items.length === 0 ? <span className="text-muted-foreground">{t('task.detail.noItems')}</span> : (
        <div className="grid grid-cols-[auto_1fr_auto_auto] gap-x-3 gap-y-0.5 font-mono">
          {r.items.slice(0, 24).map((it) => (
            <React.Fragment key={it.wid}>
              <span>{sanitizeText(it.wid, 24)}</span>
              <span className="truncate text-muted-foreground">{tOr(`task.item.${it.kind}`, it.kind)}</span>
              <span>{it.uav ? sanitizeText(it.uav, 24) : t('task.detail.unassigned')}</span>
              <span className="text-right text-muted-foreground">{tOr(`task.itemState.${it.state}`, it.state)}{it.eta_s !== null && it.eta_s !== undefined ? ` · ${mmss(it.eta_s)}` : ''}</span>
            </React.Fragment>
          ))}
        </div>
      )}
      {children.length ? (
        <span className="flex flex-wrap items-center gap-1" data-task-children={children.length}>
          <span className="text-muted-foreground">{t('task.detail.children')}</span>
          {children.map((c) => (
            <Button key={c.tid} size="xs" variant="ghost" onClick={() => taskingActions.select(c.tid)}>
              <Icon icon={TASK_ICON[c.type]} data-icon="inline-start" />{c.label || t(`task.type.${c.type}`)}
            </Button>
          ))}
        </span>
      ) : null}
      {r.warnings.length ? (
        <span className="flex flex-col gap-0.5" data-task-warnings={r.warnings.length}>
          {r.warnings.map((w) => {
            const k = warningKey(w)
            return <span key={w} className="flex items-center gap-1"><Icon icon="alert.info" />{tOr(k.key, w, { arg: k.arg ?? '' })}</span>
          })}
        </span>
      ) : null}
    </div>
  )
}

export function TasksPanel() {
  const t = useT()
  const rows = useTasking((s) => s.tasks)
  const canEdit = useD2Write()
  const selected = useTasking((s) => s.selected)
  const stale = useTasking((s) => s.stale)
  const lastError = useTasking((s) => s.lastError)
  const planningLayer = useLayers((s) => (s.visible as Record<string, boolean>).tasking)
  const [filter, setFilter] = React.useState<TaskFilter>('all')
  const shown = React.useMemo(() => filterTasks(rows, filter), [rows, filter])
  const hot = hotRow(rows)
  const sel = rows.find((r) => r.tid === selected) ?? null
  const active = rows.filter((r) => r.state === 'RUNNING').length
  const columns: LfColumn<TaskRow>[] = [
    { key: 'task', label: t('task.col.task'), format: (r) => (
      <span className="inline-flex min-w-0 items-center gap-1.5" data-task-row={r.tid}>
        <Icon icon={TASK_ICON[r.type]} label={t(`task.type.${r.type}`)} />
        <span className="truncate">{r.label || t(`task.type.${r.type}`)}</span>
        <span className="font-mono text-muted-foreground">{r.tid}</span>
        {r.parent ? <Badge variant="outline" className="h-4 px-1 text-hud-cap">{t('task.child')}</Badge> : null}
      </span>
    ) },
    { key: 'state', label: t('task.col.state'), format: (r) => <span data-task-state={r.state}>{t(`task.state.${r.state}`)}</span> },
    { key: 'n', label: t('task.col.n'), align: 'right', format: (r) => <span className="tabular-nums">{r.nVehicles}</span> },
    { key: 'progress', label: t('task.col.progress'), unit: '%', align: 'right', format: (r) => {
      const p = progressOf(r)
      return <span className="inline-flex items-center gap-1.5"><LfTickGauge mini value={p} ariaLabel={t('task.col.progress')} />{fmt.num(p, 0)}</span>
    } },
    { key: 'makespan', label: t('task.col.makespan'), align: 'right', format: (r) => <span className="tabular-nums">{r.makespanS !== null ? mmss(r.makespanS) : '-'}</span> },
    { key: 'onDetect', label: t('task.col.onDetect'), format: (r) => {
      const od = (r.params.on_detect as string | undefined) ?? (r.type === 'area_guard' ? 'relay' : r.type === 'relay_watch' ? null : 'notify')
      return od ? t(`task.onDetect.${od}`) : '-'
    } },
    { key: 'relay', label: t('task.col.relay'), format: (r) => r.relay ? (
      <span className="font-mono" data-task-relay-kpi={r.tid}>
        {t('task.relayKpi', { cov: fmt.num(r.relay.coverage * 100, 1), gaps: r.relay.gaps, disc: r.relay.discovered })}
      </span>
    ) : '' },
    { key: 'menu', label: '', width: '2rem', format: (r) => <RowMenu r={r} canEdit={canEdit} /> },
  ]
  const toggleLayer = planningLayer !== undefined
  return (
    <div className="flex min-h-0 flex-col gap-2" data-tasks-panel={rows.length}>
      <div className="flex items-center gap-2">
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('task.count', { n: rows.length, running: active })}</span>
        {stale ? <Badge variant="outline" className="border-dashed" data-task-stale="">{t('task.stale')}</Badge> : null}
        <div className="ml-auto flex items-center gap-1.5">
          {toggleLayer ? (
            <Field orientation="horizontal" className="w-auto gap-1.5">
              <Switch id="task-layer" size="sm" checked={planningLayer} onCheckedChange={(on: boolean) => layersActions.setVisible('tasking' as LayerId, on)} />
              <FieldLabel htmlFor="task-layer" className="text-hud-sub font-normal text-muted-foreground">{t('task.layer')}</FieldLabel>
            </Field>
          ) : null}
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button size="xs" variant="ghost" aria-label={t('task.filter')} data-task-filter={filter} />}>
              <Icon icon="filter" data-icon="inline-start" />{t(`task.filter.${filter}`)}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuRadioGroup value={filter} onValueChange={(v) => setFilter(v as TaskFilter)}>
                {(['all', 'active', 'ended', 'infeasible'] as const).map((f) => <DropdownMenuRadioItem key={f} value={f}>{t(`task.filter.${f}`)}</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          {canEdit ? (
            <Button size="xs" variant="outline" onClick={() => taskDraft.open()} data-task-new-open="">
              <Icon icon="plus" data-icon="inline-start" />{t('task.new')}
            </Button>
          ) : null}
        </div>
      </div>
      {lastError && canEdit ? (
        <p className="flex items-center gap-1.5 text-hud-sub text-muted-foreground" data-task-error={lastError.code}>
          <Icon icon="alert.warning" />{lastError.code ? reasonText(lastError.code).short : sanitizeText(lastError.message, 80)}
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => taskingActions.clearError()}>{t('common.close')}</Button>
        </p>
      ) : null}
      {rows.length === 0 ? (
        <PanelEmpty brand={false} title={t('task.empty')} description={canEdit ? t('task.emptyHint') : t('task.emptyViewer')} />
      ) : (
        <>
          <LfTable columns={columns} rows={shown} rowKey={(r) => r.tid} selected={selected} hot={hot ? { row: hot, col: 'state' } : null}
            onRowClick={(r) => taskingActions.select(r.tid === selected ? null : r.tid)} figureId="task-table" ariaLabel={t('panel.tasks.title')} height={180} />
          {sel ? <TaskDetail r={sel} rows={rows} /> : null}
        </>
      )}
    </div>
  )
}
