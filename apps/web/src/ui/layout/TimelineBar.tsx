// Timeline bar (M15-FR-012, FR-027, FR-076; AWR-14 §4.5, §6.17, §5.4; M12 §8.1, §8.2, §8.7): always visible 48 px bar
// across the full width, wired to M12's stores/timeline:
//   transport     play / pause (StateIcon Play and Pause morph; LIVE shows the lock, ENDED "play from the start"),
//                 step back (replay only) and step forward (a Spinner while STEPPING); a pending action carries the ring
//                 until TIME confirms it (1 s timeout, toast through timeline.setNotifier);
//   rate          live ToggleGroup x0.25 .. x10 (capped by caps.clock.max_speed); Select in the compact bar and in replay
//                 (x0.1 .. x20, options above the recording's speed_max greyed with the reason);
//   readout       SIM T+ of the seen time (tDisplayS, <= 4 Hz), dashed while STALE, HoverCard with clockFacts();
//   track         TimelineTrackArea (overview + detail in the standard bar, detail only in the compact bar);
//   status        LIVE / REPLAY, the TIME state when not playing, BUFFERING, "recording" (tl.record, foreground, never
//                 red), "index loading", "RTF limited x3.4" and the Dock toggle.
// Every disabled control keeps its reason in the Tooltip (guards from timelineGuards(); read-only, offline and
// other-world reasons from ui/shell/guards). The bar never writes the clock itself: all actions are M12's.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/components/ui/badge'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/ui/components/ui/hover-card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { BoundText } from '@/ui/motion/BoundText'
import { SwapText } from '@/ui/motion/SwapText'
import { useConnView } from '@/ui/shell/connView'
import { LIVE_RATES, REPLAY_RATES, timeline, timelineStore, useTimeline } from '@/stores/timeline'
import { usePrefs } from '@/stores/prefs'
import { layoutActions } from './layoutState'
import { IconButton } from './IconButton'
import { TimelineTrackArea } from './TimelineTrackArea'
import { TS, rateLabel, transportReasons } from './timelineGuards'

export { TS, rateLabel, transportReasons } from './timelineGuards'

function ClockFacts() {
  const t = useT()
  useTimeline((s) => s.tDisplayS) // re-read the facts at the store rate while open
  const f = timeline.clockFacts()
  const rows: [string, string][] = [
    [t('clock.sim'), fmt.simTime(f.simNowS * 1e9)],
    [t('clock.seen'), fmt.simTime(f.tRenderS * 1e9)],
    [t('clock.delay'), `${fmt.ms(f.dGlobalMs, 0)} · ${t('clock.delayFocus')} ${fmt.ms(f.dFocusMs, 0)}`],
    [t('clock.rateReq'), rateLabel(f.rateRequested)],
    [t('clock.rateAct'), rateLabel(f.rateActual)],
    [t('clock.epoch'), f.epoch >= 0 ? fmt.count(f.epoch) : '—'],
    [t('clock.wall'), f.wallMs !== null ? fmt.wallTime(f.wallMs) : '—'],
  ]
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      {rows.map(([k, v]) => [<dt key={`k${k}`} className="text-muted-foreground">{k}</dt>, <dd key={`v${k}`} className="font-mono">{v}</dd>])}
    </dl>
  )
}

/** the seen time in ns for the bound readout (read in the UI tick frame, after the store summary of that tick) */
const readDisplayNs = (): number => timelineStore.getState().tDisplayS * 1e9

function Readout({ compact }: { compact: boolean }) {
  const t = useT()
  // the time itself is C-class bound text (written by bindText in the UI tick, P4-UI): the readout and its HoverCard
  // re-render only when the stale state changes, not four times a second
  const stale = useTimeline((s) => s.stale)
  const epoch = useTimeline((s) => s.epoch)
  return (
    <HoverCard>
      <HoverCardTrigger render={<span data-timeline-readout="" className={cn('flex shrink-0 items-center gap-1 font-mono text-hud-sub tabular-nums', compact ? 'w-28' : 'w-32')} />}>
        <span className="text-muted-foreground">SIM</span>
        <BoundText read={readDisplayNs} format={fmt.simTime} data-stale={stale ? '' : undefined}
          className={cn(stale && epoch >= 0 && 'underline decoration-dashed underline-offset-2')} />
      </HoverCardTrigger>
      <HoverCardContent className="w-72 text-hud-sub">
        <div className="mb-1.5 flex items-center gap-1.5 font-medium">
          <Icon icon="tl.clock" />
          {t('clock.title')}
        </div>
        <ClockFacts />
      </HoverCardContent>
    </HoverCard>
  )
}

function RateControl({ compact }: { compact: boolean }) {
  const t = useT()
  const mode = useTimeline((s) => s.mode)
  const rate = useTimeline((s) => s.rateRequested)
  const pending = useTimeline((s) => s.pending)
  const caps = useTimeline((s) => s.caps)
  const speedMax = useTimeline((s) => s.playback?.speedMax ?? 20)
  useConnView((s) => s.version)
  useTimeline((s) => s.canWrite)
  useTimeline((s) => s.state4)
  const r = transportReasons(mode)
  const base = mode === 'replay' ? REPLAY_RATES : LIVE_RATES.filter((x) => x <= caps.maxSpeed + 1e-9)
  const items = base.map((x) => ({ value: String(x), label: rateLabel(x), reason: r.speed(x) }))
  const anyOk = items.some((i) => i.reason === null)
  const reason = anyOk ? null : t(items[0]?.reason ?? 'hint.readOnly')
  const ring = pending?.control === 'speed' ? 'ring-2 ring-ring' : ''
  const cur = items.some((i) => i.value === String(rate)) ? String(rate) : String(items.find((i) => Number(i.value) >= rate)?.value ?? items.at(-1)?.value ?? '1')
  if (compact || mode === 'replay') {
    return (
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex" />}>
          <Select items={items} value={cur} disabled={!anyOk} onValueChange={(v) => timeline.setRate(Number(v))}>
            <SelectTrigger size="sm" aria-label={t('timeline.rate')} className={cn('w-22', ring)} data-rate-select="">
              <Icon icon="tl.speed" data-icon="inline-start" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {items.map((i) => (
                // greyed options carry no native title: the trigger's shadcn Tooltip states the reason (replay speed_max, read-only)
                <SelectItem key={i.value} value={i.value} disabled={i.reason !== null}>{i.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </TooltipTrigger>
        <TooltipContent>{reason ?? (mode === 'replay' ? t('timeline.replayRate', { max: rateLabel(speedMax) }) : t('timeline.rate'))}</TooltipContent>
      </Tooltip>
    )
  }
  return (
    <Tooltip>
      <TooltipTrigger render={<span className="inline-flex" />}>
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[cur]} aria-label={t('timeline.rate')} disabled={!anyOk} className={ring} data-rate-toggle=""
          onValueChange={(v: unknown[]) => v[0] !== undefined && timeline.setRate(Number(v[0]))}>
          {items.map((i) => <ToggleGroupItem key={i.value} value={i.value} aria-label={i.label} disabled={i.reason !== null} className="px-2 font-mono tabular-nums">{i.label}</ToggleGroupItem>)}
        </ToggleGroup>
      </TooltipTrigger>
      <TooltipContent>
        {reason ?? t('timeline.rate')}
      </TooltipContent>
    </Tooltip>
  )
}

function StatusBadges({ state, compact }: { state: number; compact: boolean }) {
  const t = useT()
  const mode = useTimeline((s) => s.mode)
  const recording = useTimeline((s) => s.recording)
  const indexLoading = useTimeline((s) => s.indexLoading)
  const rtfLimited = useTimeline((s) => s.rtfLimited)
  const rateActual = useTimeline((s) => s.rateActual)
  const buffering = useTimeline((s) => s.buffering)
  const showState = state !== TS.PLAYING && state !== TS.LIVE
  return (
    <div className="flex shrink-0 items-center gap-1.5" data-timeline-status="">
      {rtfLimited ? <Badge variant="outline" data-rtf-limited="">{t('timeline.rtfLimited', { rate: fmt.num(rateActual, 1) })}</Badge> : null}
      {indexLoading ? <span className="text-hud-sub text-muted-foreground" data-index-loading="">{t('timeline.indexLoading')}</span> : null}
      {recording && mode === 'live' && !compact ? (
        <span className="flex items-center gap-1 text-hud-sub text-foreground" data-recording="">
          <Icon icon="tl.record" />
          {t('timeline.recording')}
        </span>
      ) : null}
      {showState || buffering ? (
        <Badge variant="secondary" data-time-state-badge={state}>
          <SwapText value={t(buffering ? 'time.state.4' : `time.state.${state}`)} />
        </Badge>
      ) : null}
      <Badge variant="outline" data-mode={mode} className="gap-1 font-mono">
        <Icon icon={mode === 'live' ? 'tl.live' : 'tl.history'} />
        {mode === 'live' ? 'LIVE' : 'REPLAY'}
      </Badge>
    </div>
  )
}

export function TimelineBar({ compact }: { compact: boolean }) {
  const t = useT()
  const connState = useConnView((s) => s.timeState)
  const epoch = useConnView((s) => s.epoch)
  useConnView((s) => s.version) // re-evaluate the session guards
  const storeState = useTimeline((s) => s.state4)
  const pending = useTimeline((s) => s.pending)
  const mode = useTimeline((s) => s.mode)
  useTimeline((s) => s.canWrite)
  useTimeline((s) => s.playback?.status)
  const dockOpen = usePrefs((s) => s.layout.dock.open)
  // TIME from the connection view is immediate; before the first TIME the store's state is all there is
  const state = mode === 'replay' ? storeState : epoch >= 0 ? connState : storeState
  const playing = state === TS.PLAYING || state === TS.BUFFERING
  const ended = state === TS.ENDED
  const r = transportReasons(mode)
  const playReason = r.play ? t(r.play) : null
  const stepReason = r.step ? t(r.step) : null
  const pendingPlay = pending !== null && (pending.control === 'play' || pending.control === 'pause')
  const stepping = state === TS.STEPPING || (pending !== null && pending.control === 'step')
  const playLabel = playReason ?? (ended ? t('timeline.replayFromStart') : playing ? t('timeline.pause') : t('timeline.play'))
  return (
    <div data-slot="timeline-bar" data-figure="timeline" data-time-state={state} data-mode={mode}
      className="timeline-bar flex items-center gap-2 border-t bg-card px-2">
      <ButtonGroup>
        <IconButton icon="tl.play" label={playLabel} hotkey={playReason ? undefined : 'Space'} variant="outline"
          disabled={playReason !== null} onClick={() => timeline.togglePlay()} className={cn(pendingPlay && 'ring-2 ring-ring')}>
          {state === TS.LIVE ? <StateIcon icon="layer.lock" /> : ended ? <Icon icon="tl.replay" /> : <StateIcon icon="tl.play" alt={playing} />}
        </IconButton>
        {mode === 'replay' ? (
          <IconButton icon="tl.stepback" label={stepReason ?? t('timeline.stepBack')} hotkey={stepReason ? undefined : 'ArrowLeft'} variant="outline"
            disabled={stepReason !== null} onClick={() => timeline.step('-1s')} />
        ) : null}
        <IconButton icon="tl.stepfwd" label={stepReason ?? (mode === 'replay' ? t('timeline.stepFwdReplay') : t('timeline.step'))}
          hotkey={stepReason ? undefined : 'ArrowRight'} variant="outline" disabled={stepReason !== null} onClick={() => timeline.step('100ms')}>
          {stepping ? <Spinner className="size-4" /> : <Icon icon="tl.stepfwd" />}
        </IconButton>
      </ButtonGroup>
      <RateControl compact={compact} />
      <Readout compact={compact} />
      <TimelineTrackArea height={compact ? 36 : 28} overview={!compact} overviewHeight={10} />
      <StatusBadges state={state} compact={compact} />
      <IconButton icon="panel.bottom" label={t('menu.view.dock')} hotkey="Backquote" pressed={dockOpen} onClick={() => layoutActions.toggleDock()}>
        <StateIcon icon="panel.bottom" alt={!dockOpen} />
      </IconButton>
    </div>
  )
}

export const TimelineBarMemo = React.memo(TimelineBar)
