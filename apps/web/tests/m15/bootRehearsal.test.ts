// ADR-076 (M15-FR-006; D1-AC-25): after the pre-raster phase the boot mask opens the real command palette below the mask
// until the frames settle, closes it and resolves only after the dialog overlay unmounted, so no exit animation shows
// after the reveal (then an info and a success toast of the page's toaster are shown and closed the same way); a palette
// that is already open is left alone.
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.setConfig({ testTimeout: 30_000 })

const g = globalThis as unknown as Record<string, unknown>
afterEach(() => {
  delete g.document
  delete g.requestAnimationFrame
})

/** 40 ms frames; the overlay element exists while the palette is open and for `exitFrames` frames after closing */
function installDom(exitFrames: number, state: { open: () => boolean }): { frames: () => number; overlayFrames: () => number } {
  let now = 0
  let frames = 0
  let overlay = 0
  let closingLeft = -1
  g.document = { querySelector: (sel: string) => (sel.includes('dialog-overlay') && (state.open() || closingLeft > 0) ? {} : null),
    querySelectorAll: () => [] }
  g.requestAnimationFrame = (cb: (t: number) => void) => setTimeout(() => {
    now += 40
    frames++
    if (state.open()) {
      overlay++
      closingLeft = exitFrames
    } else if (closingLeft > 0) closingLeft--
    cb(now)
  }, 0)
  return { frames: () => frames, overlayFrames: () => overlay }
}

describe('palette rehearsal below the boot mask', () => {
  it('opens the palette, closes it once the frames settle and waits for the overlay to unmount', async () => {
    const { overlaysStore } = await import('@/ui/shell/overlays')
    const { rehearsePalette } = await import('@/app/boot/BootMask')
    const { toast } = await import('@/ui/components/ui/toast')
    const added: { id?: string }[] = []
    const closed: string[] = []
    const addSpy = vi.spyOn(toast, 'add').mockImplementation((o: { id?: string }) => {
      added.push(o)
      return o.id ?? ''
    })
    const closeSpy = vi.spyOn(toast, 'close').mockImplementation((id?: string) => {
      closed.push(id ?? '')
    })
    const seen: boolean[] = []
    const un = overlaysStore.subscribe((s) => seen.push(s.palette))
    const dom = installDom(3, { open: () => overlaysStore.getState().palette })
    await rehearsePalette()
    un()
    expect(seen).toEqual([true, false])
    expect(overlaysStore.getState().palette).toBe(false)
    expect(added.map((x) => x.id)).toEqual(['boot-rehearsal-info', 'boot-rehearsal-success'])
    expect(closed).toEqual(['boot-rehearsal-info', 'boot-rehearsal-success'])
    expect(dom.overlayFrames()).toBeGreaterThanOrEqual(2)
    // closed after settling, then at least the 3 exit frames and one more frame before resolving
    expect(dom.frames()).toBeGreaterThanOrEqual(dom.overlayFrames() + 4)
    addSpy.mockRestore()
    closeSpy.mockRestore()
  })

  it('Tier S: a second pass at motion tier reduced, then the governor input is restored', async () => {
    const { overlaysStore } = await import('@/ui/shell/overlays')
    const { getGovernorMotion } = await import('@/ui/motion/tier')
    const { rehearsePalette } = await import('@/app/boot/BootMask')
    const seen: boolean[] = []
    const un = overlaysStore.subscribe((s) => seen.push(s.palette))
    installDom(2, { open: () => overlaysStore.getState().palette })
    ;(g.document as Record<string, unknown>).documentElement = { dataset: { tier: 'S' } }
    const before = getGovernorMotion()
    await rehearsePalette()
    un()
    expect(seen).toEqual([true, false, true, false])
    expect(getGovernorMotion()).toBe(before)
  })

  it('leaves a palette that is already open alone', async () => {
    const { overlays, overlaysStore } = await import('@/ui/shell/overlays')
    const { rehearsePalette } = await import('@/app/boot/BootMask')
    overlays.set('palette', true)
    installDom(3, { open: () => overlaysStore.getState().palette })
    await rehearsePalette()
    expect(overlaysStore.getState().palette).toBe(true)
    overlays.set('palette', false)
  })
})
