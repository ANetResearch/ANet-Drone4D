// Timeline tab (M15-FR-076; M12 §6.5): the detailed track (LfTimelineTrack over M12's trackModel) with the clock facts of
// stores/timeline; replay controls are D1-ext.
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { Badge } from '@/ui/components/ui/badge'
import { LfTimelineTrack } from '@/ui/lf/LfTimelineTrack'
import { LfStat } from '@/ui/lf/LfStat'
import { useElementWidth } from '@/ui/layout/useElementWidth'
import { timelineTrack, useTimeline } from '@/stores/timeline'

// M12 track model (markers, series, ranges, ticks, heroIdx); INT-1 wiring per M12-to-M15 item 1
const model = timelineTrack
export function TimelinePanel() {
  const t = useT()
  const [ref, w] = useElementWidth<HTMLDivElement>(600)
  const view = useTimeline((s) => s.view)
  const tS = useTimeline((s) => s.tDisplayS)
  const rate = useTimeline((s) => s.rateActual)
  const epoch = useTimeline((s) => s.epoch)
  const mode = useTimeline((s) => s.mode)
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Badge variant="outline">{mode === 'live' ? 'LIVE' : 'REPLAY'}</Badge>
        <span className="text-hud-sub text-muted-foreground">{t('timeline.epoch', { epoch })}</span>
      </div>
      <div ref={ref}>{w > 0 ? <LfTimelineTrack model={model} view={view} playheadS={tS} width={w} height={48} ariaLabel={t('timeline.track')} /> : null}</div>
      <div className="grid grid-cols-3 gap-2">
        <LfStat label="SIM T" format={(v) => fmt.simTime(v * 1e9)} value={tS} />
        <LfStat label="RATE" format={(v) => fmt.num(v, 2)} value={rate} unit="x" />
        <LfStat label="EPOCH" format={(v) => fmt.num(v)} value={epoch} cls="D" />
      </div>
    </div>
  )
}
