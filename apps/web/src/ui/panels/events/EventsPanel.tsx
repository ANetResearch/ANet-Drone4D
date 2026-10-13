// Events tab (M15-FR-025; AWR-14 §4.5, §11.3, §11.6): the event log (fixed ring of 5000, ui/notify/eventLog) in the
// table.log skin, virtualised beyond 200 rows (@tanstack/react-virtual inside LfTable); newest first. Columns: SIM T+
// time, source, level (shape-coded: warning outline with TriangleAlert, critical with OctagonAlert, the table's one
// red is the latest unacknowledged critical row = the single hot cell), message (sanitised), vehicle. A row click
// selects its vehicle (the global selection). The level filter keeps warnings and criticals only when asked; a
// dropped-events note appears when the ring overflowed (M15-E010).
// D2 (AWR-14 §20.11.1; UX-FR-138): four more filters (multiple choice) for the D2 sources: perception, targets, tasking and
// sandbox; level changes (perception.level, perception.detect) stay folded until "show level changes" is on.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { StatusBadge } from '@/ui/notify/StatusBadge'
import { eventLog, eventLogStore } from '@/ui/notify/eventLog'
import { useVisibleState } from '@/ui/panels/PanelHost'
import { useRedOwner } from '@/ui/notify/redFigures'
import { d2FilterOf, sourceOf, type D2Filter } from '@/ui/notify/severity'
import { Toggle } from '@/ui/components/ui/toggle'
import { selection, selectionStore } from '@/stores/selection'

type Level = 'all' | 'warning' | 'critical'
const D2_FILTERS: readonly D2Filter[] = ['perception', 'targets', 'tasking', 'sandbox']
const LEVEL_CHANGES = new Set(['perception.level', 'perception.detect'])

/** whether an event row passes the D2 filters (pure; tests) */
export function passesD2(type: string, filters: ReadonlySet<D2Filter>, showLevels: boolean): boolean {
  if (!showLevels && LEVEL_CHANGES.has(type)) return false
  if (filters.size === 0) return true
  const f = d2FilterOf(type)
  return f !== null && filters.has(f)
}

export function EventsPanel() {
  const t = useT()
  // held while the Dock is folded or another tab is active: the 4 Hz bridge flush no longer re-renders a hidden table
  // (ADR-069; D1-AC-27)
  const { version, len, dropped } = useVisibleState(eventLogStore)
  const primary = useVisibleState(selectionStore).primary
  const red = useRedOwner('event-table')
  const [level, setLevel] = React.useState<Level>('all')
  const [d2, setD2] = React.useState<ReadonlySet<D2Filter>>(() => new Set())
  const [showLevels, setShowLevels] = React.useState(false)
  // the ring is mutable: `version` is the cache key of the slot list (newest first)
  const rows = React.useMemo(() => {
    const min = level === 'critical' ? 2 : level === 'warning' ? 1 : 0
    const out: number[] = []
    for (let k = 0; k < eventLog.len; k++) {
      const slot = eventLog.slotOfNewest(k)
      if (eventLog.sev[slot] >= min && passesD2(eventLog.get(slot)?.type ?? '', d2, showLevels)) out.push(slot)
    }
    return out
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- `version` is the cache key of the mutable ring
  }, [version, level, d2, showLevels])
  const keyOf = React.useCallback((slot: number) => String(eventLog.get(slot)?.seq ?? slot), [])
  const columns: LfColumn<number>[] = [
    { key: 'time', label: t('events.col.time'), width: '7.5rem', format: (s) => <span className="font-mono">{fmt.simTime(eventLog.get(s)?.t_sim_ns)}</span> },
    { key: 'source', label: t('events.col.source'), width: '5rem', format: (s) => sourceOf(eventLog.get(s)?.type ?? '') },
    {
      key: 'level', label: t('events.col.level'), width: '6rem',
      format: (s) => {
        const sev = eventLog.sevOf(s)
        const isRed = red !== null && red.id === keyOf(s)
        return sev === 'info' ? <span className="text-muted-foreground">{t('level.info')}</span>
          : <StatusBadge kind={sev === 'critical' ? (isRed ? 'critical-primary' : 'critical-secondary') : 'warning'} text={t(`level.${sev}`)} />
      },
    },
    { key: 'message', label: t('events.col.message'), format: (s) => <span className="line-clamp-1">{eventLog.textOf(s)}</span> },
    { key: 'vehicle', label: t('events.col.vehicle'), width: '6.5rem', format: (s) => <span className="font-mono">{eventLog.get(s)?.uav ?? ''}</span> },
  ]
  if (len === 0) return <PanelEmpty brand={false} title={t('events.empty')} description={t('events.emptyHint')} />
  const hot = red ? { row: red.id, col: 'message' } : null
  const selectedRow = primary ? rows.find((s) => eventLog.get(s)?.uav === primary) : undefined
  return (
    <div className="flex min-h-0 flex-col gap-2" data-events-panel="">
      <div className="flex items-center justify-between gap-2">
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[level]} aria-label={t('events.level')}
          onValueChange={(v: unknown[]) => v[0] !== undefined && setLevel(v[0] as Level)}>
          <ToggleGroupItem value="all">{t('events.level.all')}</ToggleGroupItem>
          <ToggleGroupItem value="warning">{t('events.level.warning')}</ToggleGroupItem>
          <ToggleGroupItem value="critical">{t('events.level.critical')}</ToggleGroupItem>
        </ToggleGroup>
        <ToggleGroup multiple spacing={0} size="sm" variant="outline" value={[...d2]} aria-label={t('events.d2')} data-events-d2=""
          onValueChange={(v: unknown[]) => setD2(new Set(v.filter((x): x is D2Filter => D2_FILTERS.includes(x as D2Filter))))}>
          {D2_FILTERS.map((f) => <ToggleGroupItem key={f} value={f}>{t(`events.d2.${f}`)}</ToggleGroupItem>)}
        </ToggleGroup>
        <Toggle size="sm" variant="outline" pressed={showLevels} onPressedChange={(p: boolean) => setShowLevels(p)} data-events-levels="">
          {t('events.d2.levels')}
        </Toggle>
        <span className="text-hud-cap uppercase text-muted-foreground" data-event-count={len}>{t('events.count', { n: fmt.count(rows.length), total: fmt.count(len) })}</span>
      </div>
      {dropped > 0 ? <p className="text-hud-sub text-muted-foreground">{t('events.dropped', { n: fmt.count(dropped) })}</p> : null}
      <LfTable columns={columns} rows={rows} rowKey={keyOf} figureId="event-table" ariaLabel={t('panel.events.title')} height={180}
        hot={hot} selected={selectedRow !== undefined ? keyOf(selectedRow) : null}
        onRowClick={(s) => {
          const uav = eventLog.get(s)?.uav
          if (!uav) return
          selection.select([uav])
        }} />
    </div>
  )
}
