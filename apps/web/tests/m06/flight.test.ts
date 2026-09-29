// M06-AC-038 (ADR-029; AWR-14 §6.5): flight durations for d = 0, 100, 10000 m are 0.40, 0.67, 1.20 s (+- 1 frame),
// the curve is --ease-smooth-out, any user input cancels within the frame, reduced motion cuts (pseudo clock).
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import { CameraFlight, CameraRig, ctx as frameCtx, flightDurationS } from '@/engine'

describe('camera flight (M06-AC-038)', () => {
  it('durations clamp(0.4 + 0.15 ln(1 + d/20), 0.4, 1.2) s', () => {
    expect(flightDurationS(0)).toBeCloseTo(0.4, 6)
    expect(Math.abs(flightDurationS(100) - 0.67)).toBeLessThan(1 / 60)
    expect(flightDurationS(10_000)).toBeCloseTo(1.2, 6)
    const f = new CameraFlight()
    expect(f.start([0, 0, 0, 0, 0, 0], [0, 100, 0, 0, 0, 0], 0, 'focus')).toBeCloseTo(flightDurationS(100) * 1000, 6)
  })

  it('eases monotonically to the destination and reports completion', () => {
    const f = new CameraFlight()
    const ms = f.start([0, 0, 0, 0, 0, 0], [100, 0, 0, 0, 0, 0], 0, 'mode')
    let prev = -1
    for (let t = 0; t <= ms; t += 16) {
      const s = f.step(t)
      if (s === 0) break
      expect(f.out[0]).toBeGreaterThanOrEqual(prev)
      prev = f.out[0]
    }
    expect(f.step(ms + 1)).toBe(2)
    expect(f.out[0]).toBe(100)
    expect(f.step(ms + 2)).toBe(0)
    expect(prev).toBeGreaterThan(90)
  })

  it('user input cancels at once; reduced motion cuts', () => {
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    const r = new CameraRig(cam, null, { motionTier: () => 'full' })
    r.lookAtEnu([0, -100, 100], [0, 0, 0])
    const ms = r.flyTo([500, 0, 300], [500, 100, 0], 'focus', 0)
    expect(ms).toBeGreaterThan(400)
    expect(r.flight.active).toBe(true)
    r.update({ ...frameCtx, nowMs: 50, dtMs: 50, cssW: 1280, cssH: 720, camera: cam })
    r.controls.dispatchEvent({ type: 'controlstart' })
    expect(r.flight.active).toBe(false)
    const r2 = new CameraRig(new PerspectiveCamera(60, 1, 0.5, 20000), null, { motionTier: () => 'reduced' })
    expect(r2.flyTo([10, 20, 30], [0, 0, 0], 'home', 0)).toBe(0)
    const p = r2.getPose()
    expect(p.eye_enu_m.map((v) => Math.round(v))).toEqual([10, 20, 30])
  })
})
