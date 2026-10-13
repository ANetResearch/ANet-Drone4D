// Field-view sample and refresh budget (AWR-04 §15.11.5; ADR-125). Owner: M06.
// Points per vehicle and per sensitivity body, bodies shown at once, the totals of the single point draw and the single
// shell draw, per column (desktop Tier B/A, desktop Tier S, tablet, phone). Over the total, every anchor's count shrinks
// in proportion. Renderer levels (FIELD_LEVEL): 1 halves the points, 2 keeps only the selected target's bodies, 3 drops the
// dashed reference shells, 4 switches the field view off; the PerfGovernor knob 'field' (step 1) goes to level 1 only
// (D2-FX-R2-web, D2-AC-30), the deeper levels are reached by pinning (tests).
import type { FormFactor, Tier } from '../loop'

export interface FieldBudget {
  perUav: number
  perBody: number
  bodies: number
  totalPoints: number
  circles: number
  /** point diameter range, CSS px (band 1 .. band 5) */
  sizePx: readonly [number, number]
}

export const FIELD_BUDGETS: Readonly<Record<'desktop' | 'desktopS' | 'tablet' | 'phone', FieldBudget>> = {
  desktop: { perUav: 512, perBody: 384, bodies: 16, totalPoints: 8192, circles: 96, sizePx: [2, 6] },
  desktopS: { perUav: 192, perBody: 128, bodies: 6, totalPoints: 3072, circles: 32, sizePx: [2, 4] },
  tablet: { perUav: 320, perBody: 256, bodies: 12, totalPoints: 4096, circles: 64, sizePx: [2, 6] },
  phone: { perUav: 160, perBody: 128, bodies: 6, totalPoints: 2048, circles: 32, sizePx: [2, 4] },
}

export function fieldBudget(ff: FormFactor, tier: Tier): FieldBudget {
  if (ff === 'phone') return FIELD_BUDGETS.phone
  if (ff === 'tablet') return FIELD_BUDGETS.tablet
  return tier === 'S' ? FIELD_BUDGETS.desktopS : FIELD_BUDGETS.desktop
}

/** governor levels of the field knob */
export const FIELD_LEVEL = { full: 0, halfPoints: 1, selectedOnly: 2, noDashed: 3, off: 4 } as const

/**
 * per-anchor point counts: `want` scaled down in proportion when their sum exceeds `total` (floor, at least 1 for a
 * non-zero want while the total allows); writes into out and returns the sum
 */
export function allocatePoints(want: ArrayLike<number>, total: number, out: { [i: number]: number; length: number }): number {
  let sum = 0
  for (let i = 0; i < want.length; i++) sum += want[i]
  const k = sum > total && sum > 0 ? total / sum : 1
  let n = 0
  for (let i = 0; i < want.length; i++) {
    const c = want[i] > 0 ? Math.max(1, Math.floor(want[i] * k)) : 0
    out[i] = n + c > total ? Math.max(0, total - n) : c
    n += out[i]
  }
  return n
}
