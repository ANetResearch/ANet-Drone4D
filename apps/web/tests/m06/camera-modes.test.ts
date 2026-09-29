// M06-AC-037 (AWR-14 §6.4; M06 §6.11): controller parameters per mode (orbit, free, third, bird), bird north-up (screen
// top = +N), follow lock only in orbit/bird and released by a pan, eye >= DTM + 2 m outside FPV (the target shifted
// with it), no clamp in FPV. The rig runs without DOM in Node (camera-controls without a DOM element).
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera, Vector3 } from 'three'
import { CAMERA, CameraRig, ctx as frameCtx, type FrameCtx } from '@/engine'

function rig(deps: ConstructorParameters<typeof CameraRig>[2] = {}): CameraRig {
  const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  return new CameraRig(cam, null, { motionTier: () => 'reduced', ...deps })
}
function frame(now: number, r: CameraRig): FrameCtx {
  const c = { ...frameCtx, nowMs: now, dtMs: 33, cssW: 1280, cssH: 720, camera: r.camera }
  r.update(c)
  return c
}

describe('camera modes (M06-AC-037)', () => {
  it('orbit parameters (r14 §3.10)', () => {
    const r = rig()
    const c = r.controls
    expect([c.smoothTime, c.draggingSmoothTime, c.dollyToCursor, c.infinityDolly, c.minDistance, c.maxDistance]).toEqual([0.25, 0.08, true, false, 2, 5000])
    expect(c.maxPolarAngle).toBeCloseTo(0.49 * Math.PI, 12)
    expect(r.camera.near).toBe(0.5)
    expect(r.camera.far).toBe(20000)
    expect(r.camera.fov).toBe(60)
  })

  it('free: 0.01 m radius, inverted rotate speeds; bird: polar 0, azimuth 0, 50-5000 m, north up', () => {
    const r = rig()
    r.lookAtEnu([0, -100, 100], [0, 0, 0])
    expect(r.setMode('free').ok).toBe(true)
    expect([r.controls.minDistance, r.controls.maxDistance, r.controls.azimuthRotateSpeed, r.controls.polarRotateSpeed]).toEqual([0.01, 0.01, -0.3, -0.3])
    expect(r.setMode('bird').ok).toBe(true)
    frame(1000, r)
    const c = r.controls
    expect([c.minPolarAngle, c.maxPolarAngle, c.minAzimuthAngle, c.maxAzimuthAngle, c.minDistance, c.maxDistance]).toEqual([0, 0, 0, 0, 50, 5000])
    // screen up is +N: the camera's up vector projected on the ground points north (three -Z)
    const up = new Vector3(0, 1, 0).applyQuaternion(r.camera.quaternion)
    expect(up.z).toBeLessThan(-0.99)
    // looking straight down
    const fwd = new Vector3(0, 0, -1).applyQuaternion(r.camera.quaternion)
    expect(fwd.y).toBeLessThan(-0.99)
  })

  it('third needs a focus vehicle; follow lock only in orbit and bird', () => {
    const r = rig({ focusPose: () => false })
    expect(r.setMode('third', -1)).toEqual({ ok: false, reason: 'no_focus' })
    expect(r.setMode('fpv', -1)).toEqual({ ok: false, reason: 'no_focus' })
    expect(r.setFollowLock(true, 3)).toBe(true)
    expect(r.followLock).toBe(true)
    r.setMode('free')
    expect(r.followLock).toBe(false)
    expect(r.setFollowLock(true, 3)).toBe(false)
  })

  it('eye >= DTM + 2 m outside FPV; the target moves with the eye', () => {
    const r = rig({ dtm: () => 50 })
    r.lookAtEnu([0, -100, 10], [0, 0, 5])
    frame(0, r)
    const p = r.getPose()
    expect(p.eye_enu_m[2]).toBeGreaterThanOrEqual(52 - 1e-6)
    expect(p.target_enu_m[2] - p.eye_enu_m[2]).toBeCloseTo(5 - 10, 3)
    expect(CAMERA.groundClearM).toBe(2)
  })
})
