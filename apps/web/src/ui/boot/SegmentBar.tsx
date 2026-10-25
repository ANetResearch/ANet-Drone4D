// Weighted progress bar for loading cards that are not driven by the boot model (R-D2-30; AWR-04 §15.5.2 item 3;
// M15-FR-224). v0.3.0: one bar (shadcn Progress, role="progressbar" with aria-valuenow), no longer a segmented track: the
// weighted segments only set the total, so a later segment that completes first can never show as a second bar. The
// power-save resume uses it with its own weights (viewer: reconnect 0.3, state 0.5, reveal 0.2; sandbox: spawn 0.5,
// state 0.3, reveal 0.2). The values only grow (the controller raises segments, never lowers them).
import { Progress } from '@/ui/components/ui/progress'

export interface Segment { id: string; weight: number }

/** weighted total of the segment values (0..1) */
export function segmentTotal(segments: readonly Segment[], values: Readonly<Record<string, number>>): number {
  const v = (id: string): number => Math.min(1, Math.max(0, values[id] ?? 0))
  const w = segments.reduce((s, g) => s + g.weight, 0)
  return w > 0 ? segments.reduce((s, g) => s + g.weight * v(g.id), 0) / w : 0
}

export function SegmentBar({ segments, values, label }: { segments: readonly Segment[]; values: Readonly<Record<string, number>>; label: string }) {
  const total = segmentTotal(segments, values)
  return (
    <Progress value={Math.round(total * 100)} aria-label={label} className="gap-0" data-loading-progress="" data-progress={total.toFixed(3)} />
  )
}
