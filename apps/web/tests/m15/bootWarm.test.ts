// ADR-069 (M15-FR-006): the 'uiWarm' gate of the pre-raster phase. whenReadyExcept fires once when every other registered
// gate is resolved, the reveal still waits for the held gate, and resolving it reveals.
import { describe, expect, it, vi } from 'vitest'

vi.setConfig({ testTimeout: 30_000 })

describe('boot gates with the pre-raster phase', () => {
  it('runs the phase when the engine gates are resolved and reveals after it', async () => {
    const { boot } = await import('@/app/boot/BootController')
    boot.start()
    boot.addGate('warmup')
    boot.addGate('uiWarm')
    const cb = vi.fn()
    boot.whenReadyExcept('uiWarm', cb)
    boot.resolveGate('shell')
    boot.resolveGate('canvas')
    expect(cb).not.toHaveBeenCalled()
    boot.resolveGate('warmup')
    expect(cb).toHaveBeenCalledTimes(1)
    expect(boot.state).not.toBe('REVEALED')
    boot.resolveGate('warmup')
    expect(cb).toHaveBeenCalledTimes(1)
    boot.resolveGate('uiWarm')
    expect(boot.state).toBe('REVEALED')
  })
})
