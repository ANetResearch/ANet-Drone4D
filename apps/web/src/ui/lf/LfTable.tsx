// lieflat table.log on shadcn Table (M15-FR-079; d01 §3.9; AWR-15 §9.6): numbers right aligned, units in the header, a
// 1 px solid rule under the header, dotted row rules, no zebra, an optional total row, at most one hot cell per table
// (red text, the table's hero), selected row = bg-muted + 2 px foreground bar (lists never use red for selection).
// More than 200 rows are virtualised with @tanstack/react-virtual (ADR-028). Sortable headers use the chev.up/chev.down
// morph pair; unsorted columns show `sort`.
import * as React from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { cn } from '@/lib/utils'
import { LIMITS } from '@/lib/tokens/input.gen'
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from '@/ui/components/ui/table'
import { Button } from '@/ui/components/ui/button'
import { StateIcon } from '@/ui/icons/StateIcon'

export interface LfColumn<R> {
  key: string
  label: string
  unit?: string
  align?: 'left' | 'right'
  format?: (row: R) => React.ReactNode
  sortable?: boolean
  width?: string
}
export interface LfTableProps<R> {
  columns: readonly LfColumn<R>[]
  rows: readonly R[]
  rowKey: (row: R, i: number) => string
  total?: Partial<Record<string, React.ReactNode>>
  hot?: { row: string; col: string } | null
  selected?: string | null
  onRowClick?: (row: R) => void
  sort?: { key: string; dir: 'asc' | 'desc' } | null
  onSort?: (key: string) => void
  height?: number
  rowHeight?: number
  figureId?: string
  ariaLabel: string
  className?: string
}

/** default cell text: primitives as is, missing values empty, anything else as JSON */
function cellText(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint') return String(v)
  return JSON.stringify(v) ?? ''
}

export function LfTable<R>({ columns, rows, rowKey, total, hot, selected, onRowClick, sort, onSort, height = 240, rowHeight = 28, figureId, ariaLabel, className }: LfTableProps<R>) {
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const virtual = rows.length > LIMITS.virtualizeTableRows
  // oxlint-disable-next-line react/incompatible-library -- ADR-028: TanStack Virtual; the compiler skips this component
  const v = useVirtualizer({ count: virtual ? rows.length : 0, getScrollElement: () => scrollRef.current, estimateSize: () => rowHeight, overscan: 10 })
  const items = virtual ? v.getVirtualItems() : null
  const renderRow = (r: R, i: number) => {
    const k = rowKey(r, i)
    return (
      <TableRow key={k} data-selected={selected === k ? '' : undefined} data-row-key={k} onClick={onRowClick ? () => onRowClick(r) : undefined}
        className={cn(onRowClick && 'cursor-pointer')} style={{ height: rowHeight }}>
        {columns.map((c) => (
          <TableCell key={c.key} data-num={c.align === 'right' ? '' : undefined} data-hot={hot && hot.row === k && hot.col === c.key ? '' : undefined}>
            {c.format ? c.format(r) : cellText((r as Record<string, unknown>)[c.key])}
          </TableCell>
        ))}
      </TableRow>
    )
  }
  return (
    <div data-lf-table="" data-figure={figureId} aria-label={ariaLabel} className={cn('relative', className)}>
      <div ref={scrollRef} className="overflow-auto" style={{ maxHeight: height }}>
        <Table>
          <TableHeader className="sticky top-0 z-(--z-canvas) bg-card">
            <TableRow>
              {columns.map((c) => (
                <TableHead key={c.key} data-num={c.align === 'right' ? '' : undefined} style={c.width ? { width: c.width } : undefined}
                  className={cn('text-hud-cap font-semibold uppercase text-muted-foreground', c.align === 'right' && 'text-right')}>
                  {c.sortable && onSort ? (
                    <Button variant="ghost" size="xs" onClick={() => onSort(c.key)} aria-label={c.label}>
                      {c.label}
                      {c.unit ? <span className="normal-case">{` (${c.unit})`}</span> : null}
                      <StateIcon icon={sort?.key === c.key ? 'chev.down' : 'sort'} alt={sort?.key === c.key && sort.dir === 'asc'} />
                    </Button>
                  ) : (
                    <>
                      {c.label}
                      {c.unit ? <span className="normal-case">{` (${c.unit})`}</span> : null}
                    </>
                  )}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {items ? (
              <>
                {items.length > 0 && items[0].start > 0 ? <tr aria-hidden="true" style={{ height: items[0].start }} /> : null}
                {items.map((it) => renderRow(rows[it.index], it.index))}
                {items.length > 0 ? <tr aria-hidden="true" style={{ height: v.getTotalSize() - items[items.length - 1].end }} /> : null}
              </>
            ) : (
              rows.map(renderRow)
            )}
          </TableBody>
          {total ? (
            <TableFooter>
              <TableRow>
                {columns.map((c) => <TableCell key={c.key} data-num={c.align === 'right' ? '' : undefined}>{total[c.key] ?? null}</TableCell>)}
              </TableRow>
            </TableFooter>
          ) : null}
        </Table>
      </div>
    </div>
  )
}
