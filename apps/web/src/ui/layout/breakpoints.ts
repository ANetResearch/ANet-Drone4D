// Breakpoints C/S/W/U and height rules (M15-FR-016, §6.4.3; AWR-14 §14.1): they only decide defaults; widths the user
// dragged are kept and clamped to the ranges of the current breakpoint.
export type BreakpointId = 'C' | 'S' | 'W' | 'U'
export interface Breakpoint {
  id: BreakpointId; minW: number; leftOpen: boolean; leftW: number; rightW: number
  dock: { open: boolean; h: number }; compactHeader: boolean; speed: 'select' | 'toggle'; hudCompact: boolean; perfCols: number; hubCols: number
}
export const BP: readonly Breakpoint[] = [
  { id: 'C', minW: 1280, leftOpen: false, leftW: 272, rightW: 288, dock: { open: false, h: 240 }, compactHeader: true, speed: 'select', hudCompact: true, perfCols: 2, hubCols: 3 },
  { id: 'S', minW: 1600, leftOpen: true, leftW: 288, rightW: 320, dock: { open: false, h: 240 }, compactHeader: false, speed: 'toggle', hudCompact: false, perfCols: 3, hubCols: 3 },
  { id: 'W', minW: 2200, leftOpen: true, leftW: 320, rightW: 360, dock: { open: true, h: 240 }, compactHeader: false, speed: 'toggle', hudCompact: false, perfCols: 4, hubCols: 4 },
  { id: 'U', minW: 3000, leftOpen: true, leftW: 360, rightW: 400, dock: { open: true, h: 280 }, compactHeader: false, speed: 'toggle', hudCompact: false, perfCols: 4, hubCols: 5 },
]

export function breakpointOf(w: number): Breakpoint {
  let b = BP[0]
  for (const x of BP) if (w >= x.minW) b = x
  return b
}

/** H < 900: dock closed by default; H >= 1200: dock default +40 px */
export function dockDefaultHeight(b: Breakpoint, h: number): { open: boolean; h: number } {
  if (h < 900) return { open: false, h: b.dock.h }
  return { open: b.dock.open, h: h >= 1200 ? b.dock.h + 40 : b.dock.h }
}

/** below 1280 x 720 the shell shows a full-screen Empty and suspends the viewport (AWR-14 §14.1) */
export const tooSmall = (w: number, h: number): boolean => w < 1280 || h < 720
