// Timeline tab (M15-FR-076; M12 §6.5, §8.2-§8.4, §8.7; AWR-14 §5.4, §6.17): the detailed view of stores/timeline.
//   toolbar   LIVE or REPLAY with the run and segment, epoch; zoom out / in, fit, previous and next mark (replay), add a
//             bookmark (M); "recordings" (/runs) in live mode, "back to live" in replay;
//   track     TimelineTrackArea at 64 px with the overview (same interactions as the bar);
//   facts     seen time, requested and actual rate, render delay D, epoch, last seek latency (replay);
//   legend    the marker shapes of M12 §8.3 L4 (shape, not colour, carries the class);
//   bookmarks table.log of the run's bookmarks (time, label, shared or local) with seek (replay), edit and delete.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { DEMO_PUBLIC } from '@/lib/demo'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { Icon } from '@/ui/icons/Icon'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { IconButton } from '@/ui/layout/IconButton'
import { TimelineTrackArea, simLabel } from '@/ui/layout/TimelineTrackArea'
import { rateLabel } from '@/ui/layout/timelineGuards'
import { timeline, timelineGuards, timelineStore, useTimeline, type Bookmark } from '@/stores/timeline'
import { bookmarkUi } from './BookmarkEditor'

const ZOOM = 1.6

function zoomBy(f: number): void {
  const v = timelineViewNow()
  const c = (v.t0S + v.t1S) / 2
  const h = ((v.t1S - v.t0S) * f) / 2
  timeline.zoom(c - h, c + h)
}
const timelineViewNow = () => timelineStore.getState().view

/** shape legend of the markers (M12 §8.3 L4): CSS shapes, never glyph characters */
function Legend() {
  const t = useT()
  const dot = (cls: string) => <span aria-hidden="true" className={cn('inline-block size-1.5 rounded-full', cls)} />
  const items: [React.ReactNode, string][] = [
    [dot('bg-foreground'), t('timeline.cls.lifecycle')],
    [dot('border border-foreground'), t('timeline.cls.route')],
    [<Icon key="w" icon="alert.warning" className="size-3 text-brand-text" />, t('timeline.cls.warning')],
    [<Icon key="c" icon="alert.critical" className="size-3 text-brand-text" />, t('timeline.cls.critical')],
    [<span key="s" aria-hidden="true" className="inline-block h-2 w-px bg-muted-foreground" />, t('timeline.cls.system')],
    [<Icon key="b" icon="tl.bookmark" className="size-3" />, t('timeline.cls.bookmark')],
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-hud-sub text-muted-foreground" data-timeline-legend="">
      {items.map(([m, l]) => (
        <span key={l} className="inline-flex items-center gap-1.5">{m}{l}</span>
      ))}
    </div>
  )
}

function Bookmarks() {
  const t = useT()
  const bookmarks = useTimeline((s) => s.bookmarks)
  const segment = useTimeline((s) => s.segment)
  const mode = useTimeline((s) => s.mode)
  useTimeline((s) => s.canWrite)
  useTimeline((s) => s.playback?.status)
  const seekOk = mode === 'replay' && timelineGuards().seek === null
  const rows = bookmarks.filter((b) => b.segment === segment)
  const cols: LfColumn<Bookmark>[] = [
    { key: 'tS', label: t('bookmark.col.time'), format: (b) => <span className="font-mono tabular-nums">{simLabel(b.tS)}</span>, width: '11rem' },
    { key: 'label', label: t('bookmark.col.label'), format: (b) => b.label || <span className="text-muted-foreground">{t('bookmark.noLabel')}</span> },
    { key: 'source', label: t('bookmark.col.source'), format: (b) => t(b.source === 'user' ? 'bookmark.shared' : 'bookmark.local'), width: '6rem' },
    { key: 'act', label: '', align: 'right', width: '7.5rem', format: (b) => (
      <span className="inline-flex items-center gap-0.5">
        <IconButton icon="mission.target" size="icon-xs" label={seekOk ? t('bookmark.seek') : t('hint.liveNoRewind')} disabled={!seekOk} onClick={() => timeline.seek(b.tS)} />
        <IconButton icon="mission.edit" size="icon-xs" label={t('bookmark.edit')} onClick={() => bookmarkUi.edit(b.id)} />
        <IconButton icon="mission.delete" size="icon-xs" label={t('bookmark.delete')} onClick={() => void timeline.removeBookmark(b.id)} />
      </span>
    ) },
  ]
  if (!rows.length) {
    return (
      <p className="border-t border-dotted py-2 text-hud-sub text-muted-foreground" data-bookmarks-empty="">{t('bookmark.empty')}</p>
    )
  }
  return <LfTable columns={cols} rows={rows} rowKey={(b) => b.id} height={Math.min(160, 32 + rows.length * 28)} ariaLabel={t('bookmark.table')} figureId="timeline-bookmarks" />
}

export function TimelinePanel() {
  const t = useT()
  const tS = useTimeline((s) => s.tDisplayS)
  const rateAct = useTimeline((s) => s.rateActual)
  const rateReq = useTimeline((s) => s.rateRequested)
  const epoch = useTimeline((s) => s.epoch)
  const mode = useTimeline((s) => s.mode)
  const runId = useTimeline((s) => s.runId)
  const segment = useTimeline((s) => s.segment)
  const lastSeekMs = useTimeline((s) => s.lastSeekMs)
  const recording = useTimeline((s) => s.recording)
  useTimeline((s) => s.canWrite)
  useTimeline((s) => s.playback?.status)
  const seekOk = mode === 'replay' && timelineGuards().seek === null
  const d = timeline.clockFacts()
  return (
    <div className="flex flex-col gap-3" data-panel-timeline="">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="gap-1 font-mono">
          <Icon icon={mode === 'live' ? 'tl.live' : 'tl.history'} />
          {mode === 'live' ? 'LIVE' : 'REPLAY'}
        </Badge>
        <span className="truncate font-mono text-hud-sub text-muted-foreground" data-timeline-run="">{runId ? `${runId} · ${t('timeline.segment', { n: segment })}` : t('header.noRun')}</span>
        {recording && mode === 'live' ? (
          <span className="flex items-center gap-1 text-hud-sub" data-recording=""><Icon icon="tl.record" />{t('timeline.recording')}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          <ButtonGroup>
            <IconButton icon="cam.zoomout" variant="outline" label={t('timeline.zoomOut')} onClick={() => zoomBy(ZOOM)} />
            <IconButton icon="cam.zoomin" variant="outline" label={t('timeline.zoomIn')} onClick={() => zoomBy(1 / ZOOM)} />
            <IconButton icon="view.fullscreen" variant="outline" label={t('timeline.ctx.fit')} onClick={() => timeline.fit()} />
          </ButtonGroup>
          {mode === 'replay' ? (
            <ButtonGroup>
              <IconButton icon="tl.skipback" variant="outline" label={seekOk ? t('timeline.prevMark') : t('hint.replaySeat')} hotkey="PageUp" disabled={!seekOk} onClick={() => timeline.jumpMarker(-1)} />
              <IconButton icon="tl.skipfwd" variant="outline" label={seekOk ? t('timeline.nextMark') : t('hint.replaySeat')} hotkey="PageDown" disabled={!seekOk} onClick={() => timeline.jumpMarker(1)} />
            </ButtonGroup>
          ) : null}
          {DEMO_PUBLIC ? (
            // public demo build (ADR-083): bookmarks are a write, disabled with the read-only reason; no recordings list
            <Tooltip>
              <TooltipTrigger render={<Button size="sm" variant="outline" disabled focusableWhenDisabled className="aria-disabled:opacity-50" data-demo-readonly="" />}>
                <Icon icon="tl.bookmark" data-icon="inline-start" />
                {t('timeline.addBookmark')}
              </TooltipTrigger>
              <TooltipContent>{t('demo.readOnly')}</TooltipContent>
            </Tooltip>
          ) : (
            <Button size="sm" variant="outline" disabled={!runId} onClick={() => void bookmarkUi.addAndEdit()}>
              <Icon icon="tl.bookmark" data-icon="inline-start" />
              {t('timeline.addBookmark')}
            </Button>
          )}
          {DEMO_PUBLIC ? null : mode === 'replay' ? (
            <Button size="sm" variant="outline" onClick={() => void timeline.closeReplay()} data-replay-exit="">
              <Icon icon="tl.live" data-icon="inline-start" />
              {t('replay.backToLive')}
            </Button>
          ) : (
            <Button size="sm" variant="outline" onClick={() => navigate('/runs')}>
              <Icon icon="data.folder" data-icon="inline-start" />
              {t('runs.open')}
            </Button>
          )}
        </div>
      </div>
      <TimelineTrackArea height={56} overview overviewHeight={12} />
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <Legend />
        <dl className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-hud-sub" data-timeline-facts="">
          {([
            ['timeline.stat.seen', fmt.simTime(tS * 1e9)],
            ['timeline.stat.rate', `${rateLabel(rateAct)} / ${rateLabel(rateReq)}`],
            ['timeline.stat.delay', fmt.ms(d.dGlobalMs, 0)],
            ['timeline.stat.epoch', epoch >= 0 ? fmt.count(epoch) : '—'],
            ...(mode === 'replay' ? [['timeline.stat.seek', lastSeekMs !== null ? fmt.ms(lastSeekMs, 0) : '—'] as [string, string]] : []),
          ] as [string, string][]).map(([k, v]) => (
            <div key={k} className="flex items-baseline gap-1.5">
              <dt className="text-muted-foreground">{t(k)}</dt>
              <dd className="font-mono tabular-nums">{v}</dd>
            </div>
          ))}
        </dl>
      </div>
      <Bookmarks />
    </div>
  )
}
