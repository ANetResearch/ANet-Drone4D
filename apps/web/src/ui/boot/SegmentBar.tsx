// Segmented progress track for loading cards that are not driven by the boot model (R-D2-30; ADR-119; AWR-04 §15.5.2 item
// 3; M15-FR-224): the same look as the boot mask and the world-switch card (styles/boot.css `.loading-progress`, segment
// widths by weight, 2 px gaps, g50 fill, `--seg-p` per segment). The power-save resume uses it with its own segments
// (viewer: reconnect 0.3, state 0.5, reveal 0.2; sandbox: spawn 0.5, state 0.3, reveal 0.2). The values only grow (the
// controller raises segments, never lowers them).
import * as React from 'react'

export interface Segment { id: string; weight: number }

export function SegmentBar({ segments, values, label }: { segments: readonly Segment[]; values: Readonly<Record<string, number>>; label: string }) {
  // the values only grow (the power-save controller raises a segment, never lowers it), so the bar is a pure view
  const v = (id: string): number => Math.min(1, Math.max(0, values[id] ?? 0))
  const total = segments.reduce((s, g) => s + g.weight * v(g.id), 0) / Math.max(1e-9, segments.reduce((s, g) => s + g.weight, 0))
  return (
    <div className="loading-progress" data-loading-progress="" data-progress={total.toFixed(3)} aria-label={label}>
      <i>
        {segments.map((g) => (
          <i key={g.id} data-seg={g.id} style={{ flexGrow: Math.round(g.weight * 100), '--seg-p': v(g.id).toFixed(4) } as React.CSSProperties} />
        ))}
      </i>
    </div>
  )
}
