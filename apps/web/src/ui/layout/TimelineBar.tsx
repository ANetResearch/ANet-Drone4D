// Timeline bar (M15-FR-012, FR-027, FR-076; AWR-14 §4.5, §6.17): always visible 48 px bar across the full width. Play and
// pause (StateIcon Play/Pause morph; LIVE state shows the lock), single step (only while PAUSED and steppable), rate
// (ToggleGroup with the sliding indicator; Select in the compact breakpoint), the L3 track (LfTimelineTrack) with a
// Slider for seeking (replay only, D1-ext), the SIM T+ readout (C-class), the LIVE/REPLAY/state badge and the Dock
// toggle. The transport actions are M12's (stores/timeline: sim/play, sim/pause, sim/step, sim/speed); while an action
// awaits its TIME confirmation the button shows a ring. Read-only sessions keep the controls greyed with the reason.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/components/ui/badge'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Slider } from '@/ui/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { StateIcon } from '@/ui/icons/StateIcon'
import { LfTimelineTrack } from '@/ui/lf/LfTimelineTrack'
import { BoundText } from '@/ui/motion/BoundText'
import { SIM_RATES, writeDeniedKey } from '@/ui/shell/guards'
import { timeAgeS, timeView, useConnView } from '@/ui/shell/connView'
import { timeline, timelineTrack, useTimeline } from '@/stores/timeline'
import { usePrefs } from '@/stores/prefs'
import { layoutActions } from './layoutState'
import { IconButton } from './IconButton'
import { useElementWidth } from './useElementWidth'

// M12 track model (markers, series, ranges, ticks, heroIdx); INT-1 wiring per M12-to-M15 item 1
const model = timelineTrack
const simTimeNs = () => timeView.tSimMs * 1e6
/** TIME.state values (AWR-03 §5.2 item 6) */
const TS = { STOPPED: 0, PLAYING: 1, PAUSED: 2, STEPPING: 3, BUFFERING: 4, ENDED: 5, STALLED: 6, RESTARTING: 7, FAILED: 8, LIVE: 9 } as const
const BLOCKED = new Set<number>([TS.STALLED, TS.RESTARTING, TS.FAILED, TS.LIVE])

export function TimelineBar({ compact }: { compact: boolean }) {
  const t = useT()
  const connState = useConnView((s) => s.timeState)
  const clock = useConnView((s) => s.clock)
  const rateActual = useConnView((s) => s.rate)
  const epoch = useConnView((s) => s.epoch)
  useConnView((s) => s.version) // re-evaluate the write guard on session changes
  const storeState = useTimeline((s) => s.state4)
  const pending = useTimeline((s) => s.pending)
  const rateReq = useTimeline((s) => s.rateRequested)
  const rtfLimited = useTimeline((s) => s.rtfLimited)
  const mode = useTimeline((s) => s.mode)
  const tS = useTimeline((s) => s.tDisplayS)
  const view = useTimeline((s) => s.view)
  const dockOpen = usePrefs((s) => s.layout.dock.open)
  const [trackRef, trackW] = useElementWidth<HTMLDivElement>(400)
  // M12 writes state4 from TIME; before it does, the connection view carries the TIME state of the page client
  const state = epoch >= 0 ? connState : storeState
  const playing = state === TS.PLAYING
  const denied = writeDeniedKey()
  const blocked = BLOCKED.has(state)
  const playReason = denied ? t(denied) : !clock.pausable ? t('hint.clockLocked') : blocked ? t(`time.state.${state}`) : null
  const stepReason = playReason ?? (!clock.steppable ? t('hint.clockLocked') : state !== TS.PAUSED ? t('hint.pauseFirst') : null)
  const rateReason = denied ? t(denied) : blocked ? t(`time.state.${state}`) : null
  const rate = rateReq
  const rateItems = React.useMemo(() => SIM_RATES.filter((r) => r <= clock.maxSpeed).map((r) => ({ value: String(r), label: `×${r}` })), [clock.maxSpeed])
  const pendingPlay = pending !== null && (pending.control === 'play' || pending.control === 'pause')
  return (
    <div data-slot="timeline-bar" data-figure="timeline" data-time-state={state} className="timeline-bar flex items-center gap-2 border-t bg-card px-2">
      <ButtonGroup>
        <IconButton icon="tl.play" label={playReason ?? (playing ? t('timeline.pause') : t('timeline.play'))} hotkey={playReason ? undefined : 'Space'} variant="outline"
          disabled={playReason !== null} onClick={() => timeline.togglePlay()} className={cn(pendingPlay && 'ring-2 ring-ring')}>
          {state === TS.LIVE ? <StateIcon icon="layer.lock" /> : <StateIcon icon={playing ? 'tl.pause' : 'tl.play'} />}
        </IconButton>
        <IconButton icon="tl.stepfwd" label={stepReason ?? t('timeline.step')} hotkey={stepReason ? undefined : 'ArrowRight'} variant="outline"
          disabled={stepReason !== null} onClick={() => timeline.step('100ms')} />
      </ButtonGroup>
      {compact ? (
        <Select items={rateItems} value={String(rate)} disabled={rateReason !== null} onValueChange={(v) => timeline.setRate(Number(v))}>
          <SelectTrigger size="sm" aria-label={t('timeline.rate')} className="w-20"><SelectValue /></SelectTrigger>
          <SelectContent>
            {rateItems.map((r) => <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>)}
          </SelectContent>
        </Select>
      ) : (
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[String(rate)]} aria-label={t('timeline.rate')} disabled={rateReason !== null}
          onValueChange={(v: unknown[]) => v[0] !== undefined && timeline.setRate(Number(v[0]))}>
          {rateItems.map((r) => <ToggleGroupItem key={r.value} value={r.value} aria-label={r.label}>{r.label}</ToggleGroupItem>)}
        </ToggleGroup>
      )}
      {rtfLimited || (Number.isFinite(rateActual) && Math.abs(rateActual - rate) > 1e-3 && state === TS.PLAYING) ? (
        <Badge variant="outline" data-rtf-limited="">{t('timeline.rtfLimited', { rate: fmt.num(rateActual, 1) })}</Badge>
      ) : null}
      <div ref={trackRef} className="relative h-9 min-w-0 flex-1">
        {trackW > 0 ? <LfTimelineTrack model={model} view={view} playheadS={tS} width={trackW} height={36} ariaLabel={t('timeline.track')} /> : null}
        <Slider className="absolute inset-x-0 bottom-0" value={[tS]} min={view.t0S} max={Math.max(view.t1S, view.t0S + 1)} disabled={mode === 'live'} aria-label={t('timeline.seek')} />
      </div>
      <span className="flex w-32 shrink-0 items-center justify-end gap-1 font-mono text-hud-sub">
        <span className="text-muted-foreground">SIM</span>
        <BoundText read={simTimeNs} format={fmt.simTime} stale={timeAgeS} />
      </span>
      <Badge variant="outline" data-mode={mode}>{mode === 'live' ? 'LIVE' : 'REPLAY'}</Badge>
      {state !== TS.PLAYING && state !== TS.LIVE ? <Badge variant="secondary">{t(`time.state.${state}`)}</Badge> : null}
      <IconButton icon="panel.bottom" label={t('menu.view.dock')} hotkey="Backquote" pressed={dockOpen} onClick={() => layoutActions.toggleDock()}>
        <StateIcon icon="panel.bottom" alt={!dockOpen} />
      </IconButton>
    </div>
  )
}
