// Timeline track area (M15-FR-076; M12 §8.1, §8.4, §8.9; AWR-14 §6.17, §6.18): the overview track (whole range, density
// barcode and view frame; hidden in the compact bar) above the detail track (the view window of stores/timeline) with
// the interactions of M12 §8.4:
//   hover         hover line and a tooltip with the column's event count and its most severe event (live: the text of
//                 the event from the event log; replay: class at once, the text from R68 after a 150 ms dwell, see
//                 replayEventText), a bookmark label, or the time;
//   click         a marker (+-6 px) selects the related vehicle (replay: and seeks to it); blank area in replay seeks;
//   drag          blank area pans the view (live); the replay Slider drags the playhead with a 1:1 preview label and
//                 seeks once on release (AWR-14 §6.18);
//   wheel         pans; Ctrl+wheel zooms around the pointer (minimum span 2 s); double click fits the whole range;
//   right click   ContextMenu: add a bookmark here, fit, and in replay "play from here" and "copy link to this moment".
// Over the canvases a shadcn Slider carries the keyboard and screen reader part (transparent track and thumb): enabled in
// replay only, aria-disabled with the "live cannot rewind" description otherwise. BUFFERING shows a Spinner at the
// playhead. All writes go through M12's actions (timeline.seek, zoom, pan, fit, addBookmark).
import * as React from 'react'
import { t as tr, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { rtClient } from '@/net/rt'
import { ContextMenu, ContextMenuContent, ContextMenuGroup, ContextMenuItem, ContextMenuSeparator, ContextMenuTrigger } from '@/ui/components/ui/context-menu'
import { Slider } from '@/ui/components/ui/slider'
import { Spinner } from '@/ui/components/ui/spinner'
import { Icon } from '@/ui/icons/Icon'
import { CLS_CRITICAL, CLS_LIFECYCLE, CLS_ROUTE, CLS_SYSTEM, CLS_WARNING, LfTimelineTrack, trackLane, type TrackInputs } from '@/ui/lf/LfTimelineTrack'
import { eventLog } from '@/ui/notify/eventLog'
import { selection } from '@/stores/selection'
import { timeline, timelineGuards, timelineStore, timelineTrack, useTimeline } from '@/stores/timeline'
import { fetchReplayEvents, REPLAY_HOVER_DWELL_MS, replayEventText } from './replayEventText'
import { useElementWidth } from './useElementWidth'

const CLASS_KEY: Readonly<Record<number, string>> = {
  [CLS_LIFECYCLE]: 'timeline.cls.lifecycle', [CLS_ROUTE]: 'timeline.cls.route', [CLS_WARNING]: 'timeline.cls.warning',
  [CLS_CRITICAL]: 'timeline.cls.critical', [CLS_SYSTEM]: 'timeline.cls.system',
}
const HIT_PX = 6
const DRAG_PX = 4

/** SIM T+ label of a time in seconds */
export const simLabel = (tS: number): string => `SIM ${fmt.simTime(Math.max(0, tS) * 1e9)}`

/** text of the live event with this low-32-bit seq, from the event log ring (newest first); '' when gone */
export function eventTextOfSeq(mseq: number): string {
  for (let k = 0; k < eventLog.len; k++) {
    const slot = eventLog.slotOfNewest(k)
    const e = eventLog.get(slot)
    if (e && (e.seq >>> 0) === mseq) return eventLog.textOf(slot)
  }
  return ''
}

/** the whole range drawn by the overview: the recording in replay, run start to now (at least one minute) live */
export function overviewRange(mode: 'live' | 'replay', r0: number, r1: number): { t0S: number; t1S: number } {
  return mode === 'replay' ? { t0S: r0, t1S: Math.max(r1, r0 + 1) } : { t0S: 0, t1S: Math.max(r1, 60) }
}

interface Hover {
  x: number
  tS: number
  lines: string[]
  /** replay marker whose body is not cached yet: its mseq and the pixel column window to ask R68 for */
  fetch?: { mseq: number; t0S: number; t1S: number }
}

/** hover description of the detail track at pixel column x (exported for tests) */
export function describeColumn(x: number, width: number, view: { t0S: number; t1S: number }, mode: 'live' | 'replay',
  replay: { run: string | null; seg: number } = { run: null, seg: 0 }): Hover {
  const span = Math.max(1e-6, view.t1S - view.t0S)
  const tS = view.t0S + (x / Math.max(1, width)) * span
  const lines: string[] = []
  const tolS = (HIT_PX / Math.max(1, width)) * span
  const bm = timelineTrack.bookmarksS.find((b) => Math.abs(b - tS) <= tolS)
  const idx = timelineTrack.nearest(tS, tolS)
  if (idx >= 0) {
    const m = timelineTrack.markers
    const wCols = Math.max(1, Math.floor(width))
    const cols = timelineTrack.columns(view.t0S, view.t1S, wCols)
    const c = Math.min(cols.n - 1, Math.max(0, Math.floor(((m.t[idx] / 1000 - view.t0S) / span) * wCols)))
    const count = c >= 0 ? cols.count[c] : 1
    const cls = m.marker[idx] & 0x0f
    const who = m.agentNo[idx] !== 0xffff ? rtClient()?.roster.idOf(m.agentNo[idx]) ?? '' : ''
    const mseq = m.mseq[idx] >>> 0
    const text = mode === 'live' ? eventTextOfSeq(mseq) : replayEventText(replay.run, replay.seg, mseq)
    lines.push(tr('timeline.hover.count', { n: Math.max(1, count) }))
    // the event text already names its vehicle
    lines.push([tr(CLASS_KEY[cls] ?? 'timeline.cls.other'), text || who].filter(Boolean).join(' · '))
    lines.push(simLabel(m.t[idx] / 1000))
    const colS = span / wCols
    const fetch = mode === 'replay' && !text && replay.run
      ? { mseq, t0S: Math.min(view.t0S + Math.max(0, c) * colS, m.t[idx] / 1000), t1S: Math.max(view.t0S + (Math.max(0, c) + 1) * colS, m.t[idx] / 1000) }
      : undefined
    return { x, tS: m.t[idx] / 1000, lines, fetch }
  }
  if (bm !== undefined) {
    lines.push(tr('timeline.hover.bookmark'))
    lines.push(simLabel(bm))
    return { x, tS: bm, lines }
  }
  lines.push(simLabel(tS))
  if (mode === 'live') lines.push(tr('hint.liveNoRewind'))
  return { x, tS, lines }
}

export interface TimelineTrackAreaProps {
  /** detail track height (CSS px) */
  height: number
  /** draw the overview track above the detail (standard bar, Dock panel) */
  overview?: boolean
  /** height of the overview track */
  overviewHeight?: number
  className?: string
}

const LIVE_STEP_S = 10

/** the view at render or event time (the live clock moves it in every UI tick; components read it, not subscribe) */
const storeView = (): { t0S: number; t1S: number } => timelineStore.getState().view

/** overview inputs: the whole range, the view frame and the playhead, read by the LfScheduler before a redraw */
const readOverview = (): TrackInputs => {
  const s = timelineStore.getState()
  return { view: s.view, playheadS: s.tDisplayS, range: overviewRange(s.mode, s.rangeStartS, s.rangeEndS) }
}

/**
 * the keyboard and screen reader part over the detail track. Live: disabled, and its value and range move in ten-second
 * steps, so it re-renders (and re-measures its thumb) every ten seconds instead of in every UI tick (ADR-066, P4-UI);
 * replay keeps the exact values for 0.1 s seeking.
 */
function TrackSlider({ seekOk, preview, label, onPreview, onCommit }: {
  seekOk: boolean
  preview: number | null
  label: string
  onPreview: (v: number) => void
  onCommit: (v: number) => void
}) {
  // live: ten-second steps (the disabled Slider only carries the "live cannot rewind" description; each re-render of the
  // Base UI Slider rewrites its hidden input, P4-UI)
  const sv = (x: number): number => (seekOk ? x : Math.round(x / LIVE_STEP_S) * LIVE_STEP_S)
  const tS = useTimeline((s) => sv(s.tDisplayS))
  const v0 = useTimeline((s) => sv(s.view.t0S))
  const v1 = useTimeline((s) => sv(Math.max(s.view.t1S, s.view.t0S + 1e-3)))
  const raw = preview ?? tS
  // a live view zoomed below one step rounds both ends together: keep the range non-empty
  const hi = v1 > v0 ? v1 : v0 + (seekOk ? 1e-3 : LIVE_STEP_S)
  return (
    <Slider
      className={cn('absolute inset-0 h-full data-horizontal:w-full [&_[data-slot=slider-thumb]]:opacity-0 [&_[data-slot=slider-track]]:bg-transparent [&_[data-slot=slider-range]]:bg-transparent',
        '[&_[data-slot=slider-thumb]]:focus-visible:opacity-100 [&>div]:h-full', seekOk ? '' : 'pointer-events-none')}
      value={[Math.min(hi, Math.max(v0, sv(raw)))]} min={v0} max={hi} step={0.1}
      disabled={!seekOk} aria-label={label}
      onValueChange={(v: number | readonly number[]) => onPreview(Array.isArray(v) ? (v as number[])[0] : (v as number))}
      onValueCommitted={(v: number | readonly number[]) => onCommit(Array.isArray(v) ? (v as number[])[0] : (v as number))} />
  )
}

/** preview label and BUFFERING spinner at the playhead: mounted only while one of them shows, so the playhead
 * subscription exists only then */
function PlayheadMarks({ preview, buffering, width, cy }: { preview: number | null; buffering: boolean; width: number; cy: number }) {
  const tS = useTimeline((s) => s.tDisplayS)
  const view = useTimeline((s) => s.view)
  const span = Math.max(1e-6, view.t1S - view.t0S)
  const phX = (((preview ?? tS) - view.t0S) / span) * width
  if (!(phX >= 0 && phX <= width)) return null
  return (
    <>
      {preview !== null ? (
        <span aria-hidden="true" data-timeline-preview="" className="pointer-events-none absolute bottom-full mb-1.5 rounded-sm bg-foreground px-1.5 py-0.5 font-mono text-hud-sub text-background"
          style={{ left: Math.min(Math.max(0, phX - 40), Math.max(0, width - 96)) }}>{fmt.simTime(preview * 1e9)}</span>
      ) : null}
      {buffering ? (
        <span aria-hidden="true" data-timeline-buffering="" className="pointer-events-none absolute" style={{ top: cy - 7, left: phX - 7 }}>
          <Spinner className="size-3.5 text-foreground" />
        </span>
      ) : null}
    </>
  )
}

export function TimelineTrackArea({ height, overview = false, overviewHeight = 10, className }: TimelineTrackAreaProps) {
  const t = useT()
  const [ref, w] = useElementWidth<HTMLDivElement>(0)
  // no subscription to the view, the playhead or the range: they move in every UI tick of the live clock; the canvases
  // read them in the scheduler (LfTimelineTrack read) and the handlers read them at event time (P4-UI, D1-AC-23)
  const mode = useTimeline((s) => s.mode)
  const buffering = useTimeline((s) => s.buffering)
  const runId = useTimeline((s) => s.runId)
  const segment = useTimeline((s) => s.segment)
  useTimeline((s) => s.canWrite)
  useTimeline((s) => s.playback?.status)
  useTimeline((s) => s.markersVersion)
  const guards = timelineGuards()
  const seekOk = mode === 'replay' && guards.seek === null
  const [hover, setHover] = React.useState<Hover | null>(null)
  const [preview, setPreview] = React.useState<number | null>(null)
  const drag = React.useRef<{ x0: number; last: number; moved: boolean; id: number } | null>(null)
  const ctxT = React.useRef(Number.NaN)
  const detailRef = React.useRef<HTMLDivElement>(null)
  const ovRef = React.useRef<HTMLDivElement>(null)
  const width = Math.max(0, w)
  const lane = trackLane(height, true)
  const previewRef = React.useRef<number | null>(null)
  React.useLayoutEffect(() => {
    previewRef.current = preview
  }, [preview])
  const readDetail = React.useCallback((): TrackInputs => {
    const s = timelineStore.getState()
    return { view: s.view, playheadS: previewRef.current ?? s.tDisplayS }
  }, [])

  // wheel: pan; Ctrl+wheel zooms around the pointer (a native listener, because Ctrl+wheel must not zoom the page)
  React.useEffect(() => {
    const el = detailRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      const r = el.getBoundingClientRect()
      const v = storeView()
      const sp = Math.max(1e-6, v.t1S - v.t0S)
      e.preventDefault()
      if (e.ctrlKey || e.metaKey) {
        const f = Math.exp(Math.max(-0.5, Math.min(0.5, e.deltaY * 0.002)))
        const at = v.t0S + ((e.clientX - r.left) / Math.max(1, r.width)) * sp
        timeline.zoom(at - (at - v.t0S) * f, at + (v.t1S - at) * f)
      } else {
        const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY
        timeline.pan((d / Math.max(1, r.width)) * sp)
      }
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const localX = (e: React.PointerEvent | React.MouseEvent): number => {
    const r = detailRef.current?.getBoundingClientRect()
    return r ? Math.min(width, Math.max(0, e.clientX - r.left)) : 0
  }
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    drag.current = { x0: e.clientX, last: e.clientX, moved: false, id: e.pointerId }
    if (!seekOk) (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current
    const view = storeView()
    if (d && !seekOk) {
      if (Math.abs(e.clientX - d.x0) > DRAG_PX) d.moved = true
      if (d.moved) {
        timeline.pan((-(e.clientX - d.last) / Math.max(1, width)) * Math.max(1e-6, view.t1S - view.t0S))
        d.last = e.clientX
        setHover(null)
        return
      }
    }
    if (width > 0) setHover(describeColumn(localX(e), width, view, mode, { run: runId, seg: segment }))
  }
  // replay: after the pointer has rested on a marker for 150 ms, fetch the bodies of its column (R68) and redraw the tip
  const fxMseq = hover?.fetch?.mseq ?? -1
  const fxT0 = hover?.fetch?.t0S ?? 0
  const fxT1 = hover?.fetch?.t1S ?? 0
  React.useEffect(() => {
    if (fxMseq < 0 || !runId) return
    let live = true
    const timer = setTimeout(() => {
      void fetchReplayEvents(runId, segment, fxT0, fxT1).then((got) => {
        if (!live || !got) return
        setHover((h) => (h?.fetch?.mseq === fxMseq ? describeColumn(h.x, width, storeView(), 'replay', { run: runId, seg: segment }) : h))
      })
    }, REPLAY_HOVER_DWELL_MS)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [fxMseq, fxT0, fxT1, runId, segment, width])
  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    if (!d || d.moved || e.button !== 0 || width <= 0) return
    const x = localX(e)
    const view = storeView()
    const span = Math.max(1e-6, view.t1S - view.t0S)
    const tolS = (HIT_PX / width) * span
    const tt = view.t0S + (x / width) * span
    const idx = timelineTrack.nearest(tt, tolS)
    if (idx < 0) return
    const m = timelineTrack.markers
    const id = m.agentNo[idx] !== 0xffff ? rtClient()?.roster.idOf(m.agentNo[idx]) : undefined
    if (id) selection.select([id])
    if (seekOk) timeline.seek(m.t[idx] / 1000)
  }
  const onContextMenu = (e: React.MouseEvent) => {
    const view = storeView()
    ctxT.current = view.t0S + (localX(e) / Math.max(1, width)) * Math.max(1e-6, view.t1S - view.t0S)
  }
  const copyLink = () => {
    const world = /^\/world\/([a-z0-9-]{1,63})/.exec(location.pathname)?.[1]
    if (!world || !runId) return
    const url = `${location.origin}/world/${world}/replay/${encodeURIComponent(runId)}?seg=${segment}&t=${fmt.num(ctxT.current, 1)}`
    void navigator.clipboard?.writeText(url).then(() => notify('timeline:copy', 'info', t('timeline.linkCopied')), () => {})
  }

  // overview: click or drag centres the view there, double click fits
  const ovCenter = (clientX: number) => {
    const r = ovRef.current?.getBoundingClientRect()
    if (!r) return
    const { range, view } = readOverview() as Required<TrackInputs>
    const span = Math.max(1e-6, view.t1S - view.t0S)
    const at = range.t0S + ((clientX - r.left) / Math.max(1, r.width)) * (range.t1S - range.t0S)
    timeline.zoom(at - span / 2, at + span / 2)
  }

  return (
    // a raster island (ADR-066): the playhead, the Slider thumb and the track canvases change at the store rate; only
    // this area re-rasters, never the full-width timeline strip around it
    <div ref={ref} className={cn('relative flex min-w-0 flex-1 flex-col gap-0.5', className)} data-timeline-area="" data-mode={mode} data-island="">
      {overview && width > 0 ? (
        <div ref={ovRef} className="relative cursor-pointer" style={{ height: overviewHeight }} data-timeline-overview=""
          onPointerDown={(e) => {
            ovCenter(e.clientX)
            ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
          }}
          onPointerMove={(e) => {
            if (e.buttons & 1) ovCenter(e.clientX)
          }}
          onDoubleClick={() => timeline.fit()}>
          <LfTimelineTrack model={timelineTrack} variant="overview" read={readOverview} width={width} height={overviewHeight} ariaLabel={t('timeline.overview')} />
        </div>
      ) : null}
      <ContextMenu>
        <ContextMenuTrigger render={
          <div ref={detailRef} className={cn('relative', seekOk ? 'cursor-pointer' : 'cursor-grab')} style={{ height }} data-timeline-detail=""
            tabIndex={-1} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
            onPointerLeave={() => setHover(null)} onDoubleClick={() => timeline.fit()} onContextMenu={onContextMenu} />
        }>
          {width > 0 ? <LfTimelineTrack model={timelineTrack} read={readDetail} width={width} height={height} ariaLabel={t('timeline.track')} /> : null}
          {hover ? (
            <>
              <div aria-hidden="true" className="pointer-events-none absolute top-0 w-px border-l border-dashed border-muted-foreground"
                style={{ height: lane.base, transform: `translateX(${Math.round(hover.x)}px)` }} />
              <div aria-hidden="true" data-timeline-hover=""
                className="pointer-events-none absolute bottom-full mb-1.5 flex max-w-80 flex-col gap-0.5 rounded-md bg-popover px-2 py-1 text-hud-sub text-popover-foreground shadow-md ring-1 ring-foreground/10"
                style={{ left: Math.min(Math.max(0, hover.x - 8), Math.max(0, width - 240)) }}>
                {hover.lines.map((l, i) => <span key={i} className={cn('truncate', i === hover.lines.length - 1 && hover.lines.length > 1 ? 'text-muted-foreground' : '', i === 0 ? 'font-medium' : '')}>{l}</span>)}
              </div>
            </>
          ) : null}
          {preview !== null || buffering ? <PlayheadMarks preview={preview} buffering={buffering} width={width} cy={lane.cy} /> : null}
          <TrackSlider seekOk={seekOk} preview={preview} label={seekOk ? t('timeline.seek') : t(guards.seek ?? 'hint.liveNoRewind')}
            onPreview={setPreview} onCommit={(x) => {
              setPreview(null)
              timeline.seek(x)
            }} />
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuGroup>
            <ContextMenuItem onClick={() => void timeline.addBookmark('', ctxT.current)} disabled={!runId}>
              <Icon icon="tl.bookmark" />
              {t('timeline.ctx.bookmark')}
            </ContextMenuItem>
            <ContextMenuItem onClick={() => timeline.fit()}>
              <Icon icon="view.fullscreen" />
              {t('timeline.ctx.fit')}
            </ContextMenuItem>
          </ContextMenuGroup>
          {mode === 'replay' ? (
            <>
              <ContextMenuSeparator />
              <ContextMenuGroup>
                <ContextMenuItem disabled={!seekOk} onClick={() => {
                  timeline.seek(ctxT.current)
                  timeline.play()
                }}>
                  <Icon icon="tl.play" />
                  {t('timeline.ctx.playFromHere')}
                </ContextMenuItem>
                <ContextMenuItem onClick={copyLink}>
                  <Icon icon="link" />
                  {t('timeline.ctx.copyLink')}
                </ContextMenuItem>
              </ContextMenuGroup>
            </>
          ) : null}
        </ContextMenuContent>
      </ContextMenu>
    </div>
  )
}
