// Observation wall grid (AWR-14 §21.7.2): 1, 2x2, 3x3 or 4x4 tiles of 16:9 with a 4 px gap; automatic = the smallest
// grid that holds n tiles; more than 16 tiles page.
export const GRIDS = [1, 4, 9, 16] as const
export type GridSize = (typeof GRIDS)[number]
export interface WallLayout { cols: number; rows: number; tileW: number; tileH: number; pages: number; cap: GridSize }

export function layoutWall(n: number, w: number, h: number, user?: GridSize): WallLayout {
  const cap: GridSize = user ?? GRIDS.find((g) => g >= n) ?? 16
  const k = Math.sqrt(cap)
  const tileW = Math.max(0, Math.floor(Math.min((w - (k - 1) * 4) / k, (((h - (k - 1) * 4) / k) * 16) / 9)))
  return { cols: k, rows: k, tileW, tileH: Math.round((tileW * 9) / 16), pages: Math.max(1, Math.ceil(n / cap)), cap }
}
