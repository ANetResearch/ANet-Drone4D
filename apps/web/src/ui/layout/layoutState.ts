// Shell layout state (M15 §6.2 LayoutState; AWR-14 §3.3-§3.6): the persisted layout prefs plus the window breakpoint and
// the banner. Window resizes are debounced 120 ms (M06 uses the same debounce for setSize) before the breakpoint and the
// unobscured rect are re-evaluated. Breakpoints only decide defaults (M15-FR-016; AWR-14 §14.1): on a first visit and when
// the window crosses into another breakpoint the rail open states, the Dock default and the default widths of that
// breakpoint are applied; widths the user dragged (different from the previous breakpoint's default) are kept.
import * as React from 'react'
import { layoutRestored, prefs, prefsStore, usePrefs } from '@/stores/prefs'
import { INPUT } from '@/lib/tokens/input.gen'
import { BP, breakpointOf, dockDefaultHeight, tooSmall, type Breakpoint, type BreakpointId } from './breakpoints'
import { commitUnobscured, type CommitReason } from './useUnobscuredSync'
import type { LayoutState } from './unobscured'

export function useWindowSize(): { w: number; h: number } {
  const [size, setSize] = React.useState(() => ({ w: globalThis.innerWidth || 1280, h: globalThis.innerHeight || 720 }))
  React.useEffect(() => {
    let t: ReturnType<typeof setTimeout> | null = null
    const on = () => {
      if (t) clearTimeout(t)
      t = setTimeout(() => setSize({ w: innerWidth, h: innerHeight }), INPUT.resizeDebounceMs)
    }
    addEventListener('resize', on)
    return () => {
      removeEventListener('resize', on)
      if (t) clearTimeout(t)
    }
  }, [])
  return size
}

export function useShellLayout(bannerHeight = 0): LayoutState & { breakpoint: BreakpointId; small: boolean; w: number; h: number } {
  // the geometry fields only: a page change of the right rail, the Dock tab or the HUD switch re-rendered the whole shell
  // (header, rails, Dock, timeline) through RouterOutlet (P4-UI, D1-AC-25)
  const leftOpen = usePrefs((s) => s.layout.left.open)
  const leftW = usePrefs((s) => s.layout.left.width)
  const rightOpen = usePrefs((s) => s.layout.right.open)
  const rightW = usePrefs((s) => s.layout.right.width)
  const dockOpen = usePrefs((s) => s.layout.dock.open)
  const dockH = usePrefs((s) => s.layout.dock.height)
  const { w, h } = useWindowSize()
  const bp = breakpointOf(w)
  const state = React.useMemo<LayoutState>(() => ({
    left: { open: leftOpen, width: leftW },
    right: { open: rightOpen, width: rightW },
    dock: { open: dockOpen, height: dockH },
    bannerVisible: bannerHeight > 0, bannerHeight, breakpoint: bp.id,
  }), [leftOpen, leftW, rightOpen, rightW, dockOpen, dockH, bannerHeight, bp.id])
  const lastBp = React.useRef<BreakpointId | null>(null)
  React.useEffect(() => {
    const before = lastBp.current
    lastBp.current = bp.id
    if (before === bp.id || (before === null && layoutRestored)) return
    applyBreakpointDefaults(bp, h, before)
    // h is read at the crossing only; later height changes do not re-apply defaults
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [bp.id])
  const prev = React.useRef<LayoutState | null>(null)
  React.useEffect(() => {
    const p = prev.current
    let reason: CommitReason = 'resize'
    if (p) {
      const opened = (!p.left.open && state.left.open) || (!p.right.open && state.right.open) || (!p.dock.open && state.dock.open)
      const closed = (p.left.open && !state.left.open) || (p.right.open && !state.right.open) || (p.dock.open && !state.dock.open)
      reason = opened ? 'open' : closed ? 'close' : p.breakpoint !== state.breakpoint ? 'breakpoint' : 'dragEnd'
    }
    prev.current = state
    commitUnobscured(state, reason)
  }, [state, w, h])
  return { ...state, small: tooSmall(w, h), w, h }
}

/** apply the defaults of breakpoint `b`; widths that differ from the previous breakpoint's defaults were dragged and stay */
export function applyBreakpointDefaults(b: Breakpoint, h: number, before: BreakpointId | null): void {
  const cur = prefsStore.getState().layout
  const was = before ? BP.find((x) => x.id === before) ?? null : null
  const dock = dockDefaultHeight(b, h)
  prefs.setLayout({
    left: { open: b.leftOpen, width: !was || cur.left.width === was.leftW ? b.leftW : cur.left.width },
    right: { width: !was || cur.right.width === was.rightW ? b.rightW : cur.right.width },
    dock: { open: dock.open, height: !was || cur.dock.height === was.dock.h ? dock.h : cur.dock.height },
  }, 'breakpoint')
}

export const layoutActions = {
  toggleLeft: () => prefsStoreToggle('left'),
  toggleRight: () => prefsStoreToggle('right'),
  toggleDock: () => prefsStoreToggle('dock'),
  openDockTab(tab: string) {
    const cur = prefsStore.getState().layout
    prefsSet({ dock: { ...cur.dock, open: true, tab } })
  },
}
function prefsStoreToggle(side: 'left' | 'right' | 'dock') {
  const cur = prefsStore.getState().layout
  prefsSet({ [side]: { ...cur[side], open: !cur[side].open } })
}
function prefsSet(patch: Record<string, unknown>) {
  prefs.setLayout(patch as never, 'user')
}
