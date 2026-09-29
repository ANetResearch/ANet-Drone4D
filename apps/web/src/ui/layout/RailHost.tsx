// Floating rail host (M15-FR-012 to FR-015, §6.4.1; ADR-028): a fixed container per side holding a shadcn Resizable group
// [rail | separator | slack] (the right rail mirrored, the Dock vertical). Only the rail itself resizes; the canvas never
// does. Folding moves the whole host by transform and opacity (transitions.dev 07, styles/motion/base-ui.css); after the
// closing transition the host becomes inert (out of tab order, accessibility tree and hit testing). Separator drags write
// the pixel size to a CSS variable during the drag and persist it on release (commit 'dragEnd').
import * as React from 'react'
import type { PanelImperativeHandle } from 'react-resizable-panels'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/ui/components/ui/resizable'
import { cn } from '@/lib/utils'
import { MOTION } from '@/lib/tokens/motion.gen'
import { useMotionTier } from '@/ui/motion/tier'

export interface RailHostProps {
  side: 'left' | 'right' | 'bottom'
  open: boolean
  size: number
  minSize: number
  maxSize: number
  onResized: (px: number) => void
  label: string
  className?: string
  children: React.ReactNode
}

export function RailHost({ side, open, size, minSize, maxSize, onResized, label, className, children }: RailHostProps) {
  const ref = React.useRef<HTMLDivElement>(null)
  const tier = useMotionTier()
  const instant = tier === 'reduced' || tier === 'off'
  // `settled`: the close transition of the current closed state has finished (inert only after it, AWR-14 §2.3)
  const [settled, setSettled] = React.useState(!open)
  const [prevOpen, setPrevOpen] = React.useState(open)
  if (prevOpen !== open) {
    setPrevOpen(open)
    setSettled(false)
  }
  const inert = !open && (settled || instant)
  React.useEffect(() => {
    const el = ref.current
    if (open || instant || !el) return
    const done = (e: TransitionEvent) => {
      if (e.target === el) setSettled(true)
    }
    el.addEventListener('transitionend', done)
    const fallback = setTimeout(() => setSettled(true), MOTION.panelCloseMs + MOTION.durationQuickMs)
    return () => {
      el.removeEventListener('transitionend', done)
      clearTimeout(fallback)
    }
  }, [open, instant])

  // `size` is the persisted width (or Dock height); defaultSize only applies at mount, so later changes (breakpoint
  // defaults, preset switches, reset) are pushed with the imperative API. User drags come back through onResized.
  const panelRef = React.useRef<PanelImperativeHandle | null>(null)
  React.useEffect(() => {
    const p = panelRef.current
    if (p && Math.abs(p.getSize().inPixels - size) > 0.5) p.resize(size)
  }, [size])

  const vertical = side === 'bottom'
  const extent = maxSize + 16
  const hostVars: Record<string, string> = { [vertical ? '--dock-h' : '--rail-w']: `${extent}px` }
  const hostStyle = hostVars as React.CSSProperties
  const rail = (
    <ResizablePanel
      id={`${side}-rail`}
      panelRef={panelRef}
      defaultSize={size}
      minSize={minSize}
      maxSize={maxSize}
      groupResizeBehavior="preserve-pixel-size"
      className="pointer-events-auto relative"
      onResize={(s) => ref.current?.style.setProperty('--rail-size', `${Math.round(s.inPixels)}px`)}
    >
      {children}
    </ResizablePanel>
  )
  const slack = <ResizablePanel id={`${side}-slack`} className="pointer-events-none" />
  const handle = <ResizableHandle className="pointer-events-auto opacity-0 hover:opacity-100 focus-visible:opacity-100" aria-label={label} />
  return (
    <div
      ref={ref}
      data-slot="rail-host"
      data-side={side}
      data-state={open ? 'open' : 'closed'}
      data-inert={inert ? '' : undefined}
      inert={inert}
      aria-label={label}
      style={hostStyle}
      className={cn('app-layer-rails', className)}
    >
      <ResizablePanelGroup
        orientation={vertical ? 'vertical' : 'horizontal'}
        className="pointer-events-none"
        onLayoutChanged={(_layout, meta) => {
          if (!meta.isUserInteraction) return
          const px = Number.parseFloat(ref.current?.style.getPropertyValue('--rail-size') ?? '')
          if (Number.isFinite(px)) onResized(px)
        }}
      >
        {side === 'left' ? <>{rail}{handle}{slack}</> : <>{slack}{handle}{rail}</>}
      </ResizablePanelGroup>
    </div>
  )
}
