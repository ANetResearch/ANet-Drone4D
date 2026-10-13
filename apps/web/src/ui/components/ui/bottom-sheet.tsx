// Bottom sheet and bottom drawer (AWR-04 §15.8.5 row 2; M15-FR-233; ADR-122). Owner: M15 (project component in the
// shadcn directory: the base-mira Drawer depends on a new library, so it is not added; this wraps the shadcn Sheet with
// side="bottom" and adds a drag handle with snap points; no new dependency).
//   * BottomSheet: modal (phone quick commands, the layers drawer, full-height detail pages). The handle drags the sheet
//     between its snap points (fractions of the viewport height); dragging below the lowest snap point by more than a
//     quarter of it closes the sheet; the handle is a Button, so a tap or Enter cycles the snap points for keyboard users.
//   * BottomDrawer: non-modal, always present (the tablet portrait panel area: 25 %, 50 %, 90 %); the viewport above stays
//     interactive. Same handle rules; it never closes, the lowest snap point is its smallest state.
// Height changes animate with the transitions.dev tokens (--duration-fast, --ease-smooth-out) and jump under reduced
// motion; the content uses the safe-area inset at the bottom. Hit areas of the handle are 44 px high.
import * as React from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { Icon } from '@/ui/icons/Icon'

export const DRAWER_SNAPS: readonly number[] = [0.25, 0.5, 0.9]

/** the snap point nearest to a height fraction (pure; tests) */
export function nearestSnap(frac: number, snaps: readonly number[]): number {
  let best = snaps[0] ?? 0.5
  for (const s of snaps) if (Math.abs(s - frac) < Math.abs(best - frac)) best = s
  return best
}

/** pointer drag of the handle: reports the live fraction and the release fraction */
function useHandleDrag(frac: number, onLive: (f: number | null) => void, onRelease: (f: number) => void) {
  const start = React.useRef<{ y: number; frac: number; id: number } | null>(null)
  const onPointerDown = (e: React.PointerEvent<HTMLElement>): void => {
    start.current = { y: e.clientY, frac, id: e.pointerId }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const fracAt = (e: React.PointerEvent<HTMLElement>): number => {
    const s = start.current
    if (!s) return frac
    const h = globalThis.innerHeight || 1
    return Math.min(0.95, Math.max(0.05, s.frac - (e.clientY - s.y) / h))
  }
  const onPointerMove = (e: React.PointerEvent<HTMLElement>): void => {
    if (!start.current || start.current.id !== e.pointerId) return
    onLive(fracAt(e))
  }
  const onPointerUp = (e: React.PointerEvent<HTMLElement>): void => {
    if (!start.current || start.current.id !== e.pointerId) return
    const moved = Math.abs(e.clientY - start.current.y)
    const f = fracAt(e)
    start.current = null
    onLive(null)
    if (moved >= 6) onRelease(f)
  }
  return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp }
}

function Handle({ label, drag, onCycle }: { label: string; drag: ReturnType<typeof useHandleDrag>; onCycle: () => void }) {
  return (
    <Button variant="ghost" size="sm" aria-label={label} data-sheet-handle="" onClick={onCycle}
      className="mx-auto flex h-11 w-24 touch-none items-center justify-center rounded-none" {...drag}>
      <Icon icon="ui.drag-handle" />
    </Button>
  )
}

export interface BottomSheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: React.ReactNode
  description?: React.ReactNode
  snaps?: readonly number[]
  /** initial snap point (fraction) */
  initial?: number
  handleLabel: string
  className?: string
  children: React.ReactNode
}

export function BottomSheet({ open, onOpenChange, title, description, snaps = [0.5, 0.9], initial, handleLabel, className, children }: BottomSheetProps) {
  const [snap, setSnap] = React.useState(initial ?? snaps[0] ?? 0.5)
  const [live, setLive] = React.useState<number | null>(null)
  React.useEffect(() => {
    if (open) setSnap(initial ?? snaps[0] ?? 0.5)
    // reset to the first snap point on every opening only
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  const drag = useHandleDrag(snap, setLive, (f) => {
    const low = snaps[0] ?? 0.25
    if (f < low * 0.75) onOpenChange(false)
    else setSnap(nearestSnap(f, snaps))
  })
  const cycle = (): void => setSnap(snaps[(snaps.indexOf(snap) + 1) % snaps.length] ?? snap)
  const h = live ?? snap
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="bottom" showCloseButton={false} data-bottom-sheet={Math.round(snap * 100)} data-dragging={live !== null ? '' : undefined}
        style={{ height: `${Math.round(h * 100)}dvh` }} className={cn('bottom-sheet rounded-t-xl pb-[env(safe-area-inset-bottom)]', className)}>
        <Handle label={handleLabel} drag={drag} onCycle={cycle} />
        <SheetHeader className="px-4 pt-0 pb-2">
          <SheetTitle>{title}</SheetTitle>
          {description ? <SheetDescription>{description}</SheetDescription> : <SheetDescription className="sr-only">{title}</SheetDescription>}
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">{children}</div>
      </SheetContent>
    </Sheet>
  )
}

export interface BottomDrawerProps {
  snap: number
  onSnap: (f: number) => void
  snaps?: readonly number[]
  handleLabel: string
  className?: string
  /** reports the live height fraction while dragging (the viewport rect follows on release only) */
  header?: React.ReactNode
  children: React.ReactNode
}

export function BottomDrawer({ snap, onSnap, snaps = DRAWER_SNAPS, handleLabel, className, header, children }: BottomDrawerProps) {
  const [live, setLive] = React.useState<number | null>(null)
  const drag = useHandleDrag(snap, setLive, (f) => onSnap(nearestSnap(f, snaps)))
  const cycle = (): void => onSnap(snaps[(snaps.indexOf(snap) + 1) % snaps.length] ?? snap)
  const h = live ?? snap
  return (
    <section data-bottom-drawer={Math.round(snap * 100)} data-dragging={live !== null ? '' : undefined} aria-label={handleLabel}
      style={{ height: `${Math.round(h * 100)}dvh` }}
      className={cn('bottom-drawer fixed inset-x-0 bottom-0 z-(--z-rails) flex flex-col rounded-t-xl border-t bg-card pb-[env(safe-area-inset-bottom)] text-xs/relaxed', className)}>
      <Handle label={handleLabel} drag={drag} onCycle={cycle} />
      {header}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">{children}</div>
    </section>
  )
}
