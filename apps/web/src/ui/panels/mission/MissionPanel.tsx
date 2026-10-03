// Mission tab (M15-FR-021; AWR-14 §5.3, §6.19 rows 10-11; M10 §8.2): the mission table in the lieflat table.log skin over
// stores/mission (M10): mission, generator, state, vehicles, progress (20-tick gauge, 1 tick = 5%), ETA; the row menu
// starts, pauses, resumes or aborts the mission (mission/{mid}/*; abort confirms first) and focuses its vehicles. The
// header opens the route editor or the area drawing for the focused vehicle (right rail page 3, D1-AC-17), starts or
// pauses every mission and resets the scenario (sim/reset, confirmed: the recording segment ends and the epoch
// increments). "Pause mission" and "pause simulation" stay distinct texts. Viewers see the table only.
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { camera } from '@/viewport/facade'
import { Button } from '@/ui/components/ui/button'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { PanelEmpty } from '@/ui/brand'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { runService } from '@/ui/actions/vehicleCommands'
import { useConnView } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { selection, useSelection } from '@/stores/selection'
import { openEditor } from '@/ui/panels/mission-edit/MissionEditPanel'
import { selectMissionRows, useMission, type MissionRow } from '@/stores/mission'

type MissionOp = 'start' | 'pause' | 'resume' | 'abort'
function missionCall(m: MissionRow, op: MissionOp, what: string): void {
  runService(`mission:${m.mid}:${op}`, `mission/${m.mid}/${op}`, {}, what, { toastSuccess: true })
}

function RowMenu({ m, canWrite }: { m: MissionRow; canWrite: boolean }) {
  const t = useT()
  const what = (op: MissionOp) => t('mission.action', { op: t(`mission.op.${op}`), mid: m.mid })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon-xs" variant="ghost" aria-label={t('mission.rowMenu', { mid: m.mid })} />}>
        <Icon icon="more" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="font-mono">{m.mid}</DropdownMenuLabel>
          <DropdownMenuItem disabled={!canWrite || m.state !== 'IDLE'} onClick={() => missionCall(m, 'start', what('start'))}><Icon icon="mission.start" />{t('mission.op.start')}</DropdownMenuItem>
          <DropdownMenuItem disabled={!canWrite || m.state !== 'RUNNING'} onClick={() => missionCall(m, 'pause', what('pause'))}><Icon icon="mission.paused" />{t('mission.op.pause')}</DropdownMenuItem>
          <DropdownMenuItem disabled={!canWrite || m.state !== 'PAUSED'} onClick={() => missionCall(m, 'resume', what('resume'))}><Icon icon="mission.start" />{t('mission.op.resume')}</DropdownMenuItem>
          <DropdownMenuItem disabled={!canWrite || m.state === 'DONE' || m.state === 'ABORTED'} onClick={() => confirmThen({
            id: 'mission.abort', title: t('mission.abortTitle', { mid: m.mid }), body: t('mission.abortBody'), action: t('mission.op.abort'),
            run: () => missionCall(m, 'abort', what('abort')),
          })}><Icon icon="mission.abort" />{t('mission.op.abort')}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => {
            selection.select(m.vehicles)
            camera.focus(m.vehicles)
          }}><Icon icon="cmd.track" />{t('mission.focus')}</DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function MissionPanel() {
  const t = useT()
  const rows = useMission(selectMissionRows)
  useConnView((s) => s.version)
  const denied = writeDeniedKey()
  const canWrite = denied === null
  const primary = useSelection((s) => s.primary)
  const columns: LfColumn<MissionRow>[] = [
    { key: 'mid', label: t('mission.col.id'), format: (r) => <span className="font-mono">{r.mid}</span> },
    { key: 'generator', label: t('mission.col.generator') },
    { key: 'state', label: t('mission.col.state'), format: (r) => t(`mission.state.${r.state}`) },
    { key: 'vehicles', label: t('mission.col.vehicles'), format: (r) => (
      <span className="font-mono">{r.vehicles.length <= 2 ? r.vehicles.join(', ') || '—' : t('mission.vehiclesN', { first: r.vehicles.slice(0, 2).join(', '), n: r.vehicles.length })}</span>
    ) },
    { key: 'progressPct', label: t('mission.col.progress'), unit: '%', align: 'right', format: (r) => (
      <span className="inline-flex items-center gap-1.5"><LfTickGauge mini value={r.progressPct} ariaLabel={t('mission.col.progress')} />{fmt.num(r.progressPct)}</span>
    ) },
    { key: 'etaS', label: t('mission.col.eta'), align: 'right', format: (r) => <span className="tabular-nums">{fmt.dur(r.etaS)}</span> },
    { key: 'menu', label: '', width: '2rem', format: (r) => <RowMenu m={r} canWrite={canWrite} /> },
  ]
  const bulk = (op: 'start' | 'pause') => {
    for (const m of rows) {
      if (op === 'start' && (m.state === 'IDLE' || m.state === 'PAUSED')) missionCall(m, m.state === 'IDLE' ? 'start' : 'resume', t('mission.all', { op: t(`mission.op.${op}`) }))
      if (op === 'pause' && m.state === 'RUNNING') missionCall(m, 'pause', t('mission.all', { op: t('mission.op.pause') }))
    }
  }
  const reset = () => confirmThen({
    id: 'sim.reset', title: t('mission.resetTitle'), body: t('mission.resetBody'), action: t('mission.reset'),
    run: () => void runService('sim:reset', 'sim/reset', { scenario_id: null }, t('mission.reset'), { toastSuccess: true }),
  })
  return (
    <div className="flex min-h-0 flex-col gap-2" data-mission-panel="">
      <div className="flex items-center justify-between gap-2">
        <span className="text-hud-cap uppercase text-muted-foreground">{t('mission.count', { n: rows.length })}</span>
        {canWrite ? (
          <ButtonGroup>
            {/* D1-AC-17 entries: the route editor and area drawing for the focused vehicle (right rail page 3) */}
            {/* disabled reasons in a shadcn Tooltip (the buttons stay hoverable: focusableWhenDisabled, aria-disabled) */}
            <Tooltip>
              <TooltipTrigger render={
                <Button size="sm" variant="outline" disabled={!primary} focusableWhenDisabled className="aria-disabled:opacity-50"
                  onClick={() => primary && openEditor(primary, 'add')} data-mission-edit-route="" />
              }>
                <Icon icon="cmd.followpath" data-icon="inline-start" />{t('edit.open')}
              </TooltipTrigger>
              <TooltipContent>{primary ? t('edit.openHint') : t('hint.noFocus')}</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger render={
                <Button size="sm" variant="outline" disabled={!primary} focusableWhenDisabled className="aria-disabled:opacity-50"
                  onClick={() => primary && openEditor(primary, 'area')} data-mission-edit-area="" />
              }>
                <Icon icon="tool.box" data-icon="inline-start" />{t('edit.tool.area')}
              </TooltipTrigger>
              <TooltipContent>{primary ? t('edit.area.hint') : t('hint.noFocus')}</TooltipContent>
            </Tooltip>
            <Button size="sm" variant="outline" disabled={!rows.length} onClick={() => bulk('start')}><Icon icon="mission.start" data-icon="inline-start" />{t('mission.startAll')}</Button>
            <Button size="sm" variant="outline" disabled={!rows.length} onClick={() => bulk('pause')}><Icon icon="mission.paused" data-icon="inline-start" />{t('mission.pauseAll')}</Button>
            <Button size="sm" variant="outline" onClick={reset}><Icon icon="refresh" data-icon="inline-start" />{t('mission.reset')}</Button>
          </ButtonGroup>
        ) : (
          // read-only sessions see one explanation instead of greyed buttons (AWR-14 §7.8)
          <span className="flex items-center gap-1.5 text-hud-sub text-muted-foreground" data-mission-readonly={denied ?? ''}>
            <Icon icon={denied === 'hint.offline' ? 'alert.linklost' : 'layer.visible'} />
            {t(denied ?? 'hint.readOnly')}
          </span>
        )}
      </div>
      {rows.length === 0 ? <PanelEmpty title={t('mission.empty')} description={t('mission.emptyHint')} />
        : <LfTable columns={columns} rows={rows} rowKey={(r) => r.mid} figureId="mission-table" ariaLabel={t('panel.mission.title')} height={180} />}
    </div>
  )
}
