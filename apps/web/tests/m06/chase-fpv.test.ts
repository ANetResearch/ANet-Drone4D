// M06-AC-040 (r15 §3.15; ADR-046; M13 §7.4): Third chase distance 16.2 +- 0.1 m behind and above in the velocity frame,
// heading from the body yaw below 0.5 m/s without jumps (azimuth rate <= 180 deg/s), interp.setFocus on entry and exit;
// FPV projection equals projectionFor element-wise and the world matrix equals WorldRoot x pose x T_base_cam;
// hasCamera = false -> no_camera_sensor without switching.
import { describe, expect, it } from 'vitest'
import { Matrix4, PerspectiveCamera, Quaternion, Vector3 } from 'three'
import { CameraRig, ctx as frameCtx, WORLD_ROOT, type SensorsApi, type SensorView } from '@/engine'

const S: SensorView = { agentNo: 7, sensorNo: 0, name: 'cam', kind: 0, rangeM: 80, hfov: 1.2, vfov: 0.8, ring: new Float64Array(36), ringHead: 0, valid: true }
const PROJ = new Matrix4().makePerspective(-0.3, 0.32, 0.2, -0.18, 0.2, 20000)
const TBC = new Matrix4().compose(new Vector3(0.13, 0, 0.28), new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), 0.3), new Vector3(1, 1, 1))
function sensors(has: boolean): SensorsApi {
  return {
    sensorsOf: () => (has ? [S] : []), hasCamera: () => has, frustumCorners: (_s, _l, o) => o,
    T_base_cam: (_s, out) => {
      TBC.toArray(out)
      return out
    },
    projectionFor: (_s, _a, _n, _f, out) => {
      PROJ.toArray(out)
      return out
    },
  }
}

describe('Third and FPV (M06-AC-040)', () => {
  it('chase offset (-15, 0, 6) m in the velocity frame: 16.2 m; setFocus in and out', () => {
    const pos = [100, 200, 50]
    const vel = [0, 10, 0] // heading north
    const q = new Quaternion()
    const focus: number[] = []
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    const r = new CameraRig(cam, null, {
      motionTier: () => 'reduced',
      focusPose: (_a, p, qq, v) => {
        p.set(pos)
        qq.set([q.x, q.y, q.z, q.w])
        v.set(vel)
        return true
      },
      setFocus: (a) => focus.push(a),
    })
    r.lookAtEnu([0, 0, 300], [0, 1, 0])
    expect(r.setMode('third', 7).ok).toBe(true)
    let t = 0
    for (let k = 0; k < 90; k++) r.update({ ...frameCtx, nowMs: (t += 33), dtMs: 33, cssW: 1280, cssH: 720, camera: cam })
    const p = r.getPose()
    const d = Math.hypot(p.eye_enu_m[0] - pos[0], p.eye_enu_m[1] - pos[1], p.eye_enu_m[2] - pos[2])
    expect(Math.abs(d - Math.hypot(15, 6))).toBeLessThan(0.1)
    expect(p.eye_enu_m[1]).toBeLessThan(pos[1] - 14) // behind (south of) a north-bound vehicle
    expect(p.eye_enu_m[2]).toBeGreaterThan(pos[2] + 5)
    // slow down: heading switches to the body yaw (east) without a jump
    vel[1] = 0.2
    q.setFromAxisAngle(new Vector3(0, 0, 1), 0) // body x = east
    let prevAz = r.controls.azimuthAngle
    let maxRate = 0
    for (let k = 0; k < 90; k++) {
      r.update({ ...frameCtx, nowMs: (t += 33), dtMs: 33, cssW: 1280, cssH: 720, camera: cam })
      const az = r.controls.azimuthAngle
      maxRate = Math.max(maxRate, Math.abs(Math.atan2(Math.sin(az - prevAz), Math.cos(az - prevAz))) / 0.033)
      prevAz = az
    }
    expect((maxRate * 180) / Math.PI).toBeLessThanOrEqual(180)
    r.setMode('orbit', -1)
    expect(focus).toEqual([7, -1])
  })

  it('FPV matrices from M13; no camera -> no_camera_sensor', () => {
    const pos = [10, 20, 30]
    const q = new Quaternion().setFromAxisAngle(new Vector3(0, 0, 1), 0.7)
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    let has = false
    const r = new CameraRig(cam, null, {
      motionTier: () => 'reduced', sensors: () => sensors(has),
      focusPose: (_a, p, qq, v) => {
        p.set(pos)
        qq.set([q.x, q.y, q.z, q.w])
        v.set([0, 0, 0])
        return true
      },
    })
    expect(r.setMode('fpv', 7)).toEqual({ ok: false, reason: 'no_camera_sensor' })
    expect(r.mode).toBe('orbit')
    has = true
    expect(r.setMode('fpv', 7).ok).toBe(true)
    r.update({ ...frameCtx, nowMs: 10, dtMs: 16, cssW: 1280, cssH: 720, camera: cam })
    for (let i = 0; i < 16; i++) expect(cam.projectionMatrix.elements[i]).toBeCloseTo(PROJ.elements[i], 12)
    const ref = new Matrix4().compose(new Vector3(pos[0], pos[1], pos[2]), q, new Vector3(1, 1, 1)).premultiply(WORLD_ROOT).multiply(TBC)
    for (let i = 0; i < 16; i++) expect(cam.matrixWorld.elements[i]).toBeCloseTo(ref.elements[i], 9)
    expect(cam.matrixAutoUpdate).toBe(false)
    expect(cam.near).toBe(0.2)
    expect(r.controls.enabled).toBe(false)
    r.setMode('orbit', 7)
    expect(cam.matrixAutoUpdate).toBe(true)
    expect(r.controls.enabled).toBe(true)
  })
})
