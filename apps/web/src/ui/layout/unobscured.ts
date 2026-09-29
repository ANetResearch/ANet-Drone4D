// Unobscured rect (M15-FR-015, §6.4.2; AWR-14 §3.4): computed from the layout state, never by DOM measurement; the
// camera view offset centres on it (M06 setUnobscuredRect). CSS px; below 320 x 180 it falls back to the whole canvas.
import { LAYOUT } from '@/lib/tokens/input.gen'
import type { BreakpointId } from './breakpoints'

export interface Rect { x: number; y: number; w: number; h: number }
export interface LayoutState {
  left: { open: boolean; width: number }
  right: { open: boolean; width: number }
  dock: { open: boolean; height: number }
  bannerVisible: boolean
  bannerHeight: number
  breakpoint: BreakpointId
}

export function computeUnobscured(W: number, H: number, s: LayoutState, out: Rect): Rect {
  const G = LAYOUT.gapPx
  const banner = s.bannerVisible ? s.bannerHeight : 0
  const x0 = s.left.open ? G + s.left.width + G : 0
  const x1 = s.right.open ? W - (G + s.right.width + G) : W
  const y0 = LAYOUT.headerPx + banner
  const y1 = H - LAYOUT.timelinePx - (s.dock.open ? G + s.dock.height + G : 0)
  out.x = x0
  out.y = y0
  out.w = x1 - x0
  out.h = y1 - y0
  if (out.w < LAYOUT.unobscuredMinWPx || out.h < LAYOUT.unobscuredMinHPx) {
    out.x = 0
    out.y = 0
    out.w = W
    out.h = H
  }
  return out
}

export const rectEq = (a: Rect, b: Rect): boolean => a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h
