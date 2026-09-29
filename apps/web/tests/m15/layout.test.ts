// M15-FR-015 (AWR-14 §3.4): the unobscured rect comes from the layout state; M15-FR-032: prefs are clamped on read.
import { describe, expect, it } from 'vitest'
import { computeUnobscured, rectEq, type LayoutState, type Rect } from '@/ui/layout/unobscured'
import { LAYOUT } from '@/lib/tokens/input.gen'

const base: LayoutState = {
  left: { open: true, width: 288 },
  right: { open: true, width: 320 },
  dock: { open: false, height: 240 },
  bannerVisible: false,
  bannerHeight: 32,
  breakpoint: 'S',
}

describe('computeUnobscured', () => {
  it('subtracts the rails, header and timeline bar with the 8 px gaps', () => {
    const r = computeUnobscured(1920, 1080, base, { x: 0, y: 0, w: 0, h: 0 })
    const G = LAYOUT.gapPx
    expect(r).toEqual({ x: G + 288 + G, y: LAYOUT.headerPx, w: 1920 - (G + 288 + G) - (G + 320 + G), h: 1080 - LAYOUT.headerPx - LAYOUT.timelinePx })
  })

  it('accounts for the dock and the banner', () => {
    const r = computeUnobscured(1920, 1080, { ...base, dock: { open: true, height: 240 }, bannerVisible: true }, { x: 0, y: 0, w: 0, h: 0 })
    expect(r.y).toBe(LAYOUT.headerPx + 32)
    expect(r.h).toBe(1080 - LAYOUT.timelinePx - (8 + 240 + 8) - (LAYOUT.headerPx + 32))
  })

  it('uses the whole canvas when the rails are closed', () => {
    const r = computeUnobscured(1280, 720, { ...base, left: { open: false, width: 288 }, right: { open: false, width: 320 } }, { x: 0, y: 0, w: 0, h: 0 })
    expect(r.x).toBe(0)
    expect(r.w).toBe(1280)
  })

  it('falls back to the full canvas below 320 x 180', () => {
    const r = computeUnobscured(700, 400, base, { x: 0, y: 0, w: 0, h: 0 })
    expect(r).toEqual({ x: 0, y: 0, w: 700, h: 400 })
  })

  it('writes into the given rect without allocating', () => {
    const out: Rect = { x: 0, y: 0, w: 0, h: 0 }
    expect(computeUnobscured(1920, 1080, base, out)).toBe(out)
    expect(rectEq(out, { ...out })).toBe(true)
    expect(rectEq(out, { ...out, w: out.w + 1 })).toBe(false)
  })
})

describe('breakpoints', () => {
  it('maps widths to C/S/W/U', async () => {
    const { breakpointOf } = await import('@/ui/layout/breakpoints')
    expect(breakpointOf(1280).id).toBe('C')
    expect(breakpointOf(1599).id).toBe('C')
    expect(breakpointOf(1920).id).toBe('S')
    expect(breakpointOf(2560).id).toBe('W')
    expect(breakpointOf(3840).id).toBe('U')
  })
})
