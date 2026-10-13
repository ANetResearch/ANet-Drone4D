// M06-AC-035 (AWR-15 §10.7; M13 §7.4): frustum corners equal M13 frustumCorners transformed by the vehicle pose
// (<= 1e-4 m), follow a fast yaw exactly (the same pose as the vehicle), dashed edges when the newest SensorPose48 is
// older than 3 / f, a collapsed far plane when FOV_VALID = 0, inactive sensors skipped, L = 60 m for a NaN range.
import { describe, expect, it } from 'vitest'
import { Quaternion, Vector3 } from 'three'
import { FrustumLayer, frustumLength, payloadIlluminators, SENSOR_ACTIVE, SENSOR_FOV_VALID, type SensorsApi, type SensorView } from '@/engine'

function sensor(flags: number, tMs: number, rangeM = 80): SensorView {
  const ring = new Float64Array(36)
  ring[0] = tMs
  ring[8] = flags
  return { agentNo: 1, sensorNo: 0, name: 'cam', kind: 0, rangeM, hfov: 1.2, vfov: 0.8, ring, ringHead: 1, valid: true }
}
/** a pinhole frustum along body +x with a 10 deg pitch-down mount */
const api: SensorsApi = {
  sensorsOf: () => [],
  hasCamera: () => true,
  frustumCorners(s, L, out) {
    const q = new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), (10 * Math.PI) / 180)
    const h = Math.tan(s.hfov / 2) * L
    const v = Math.tan(s.vfov / 2) * L
    const pts = [[0, 0, 0], [L, h, v], [L, -h, v], [L, -h, -v], [L, h, -v]]
    pts.forEach((p, k) => {
      const w = new Vector3(p[0], p[1], p[2]).applyQuaternion(q)
      out[3 * k] = w.x + 0.1
      out[3 * k + 1] = w.y
      out[3 * k + 2] = w.z - 0.05
    })
    return out
  },
  T_base_cam: (_s, out) => out,
  projectionFor: (_s, _a, _n, _f, out) => out,
}

describe('sensor frustums (M06-AC-035)', () => {
  it('corners = pose x frustumCorners (<= 1e-4 m); fast yaw follows the vehicle pose', () => {
    const f = new FrustumLayer(16)
    const s = sensor(SENSOR_ACTIVE | SENSOR_FOV_VALID, 1000)
    for (const yawDeg of [0, 45, 170, -120]) {
      const q = new Quaternion().setFromAxisAngle(new Vector3(0, 0, 1), (yawDeg * Math.PI) / 180)
      f.begin()
      expect(f.add(api, s, 100, -50, 30, q.x, q.y, q.z, q.w, 1.0)).toBe(true)
      f.commit()
      const got = f.cornersOf(0, new Float64Array(15))
      const ref = api.frustumCorners(s, frustumLength(s.rangeM), new Float64Array(15))
      for (let k = 0; k < 5; k++) {
        const w = new Vector3(ref[3 * k], ref[3 * k + 1], ref[3 * k + 2]).applyQuaternion(q).add(new Vector3(100, -50, 30))
        expect(Math.hypot(got[3 * k] - w.x, got[3 * k + 1] - w.y, got[3 * k + 2] - w.z)).toBeLessThan(1e-4)
      }
      // the optical axis (origin -> centre of the far plane) turns with the vehicle yaw
      const cx = (got[3] + got[6] + got[9] + got[12]) / 4 - got[0]
      const cy = (got[4] + got[7] + got[10] + got[13]) / 4 - got[1]
      const ang = (Math.atan2(cy, cx) * 180) / Math.PI
      expect(Math.abs(((ang - yawDeg + 540) % 360) - 180)).toBeLessThan(0.5)
    }
    expect(f.drawCount()).toBe(2)
  })

  it('stale pose -> dashed edges; FOV_VALID = 0 -> no far plane; inactive -> skipped; NaN range -> 60 m', () => {
    const f = new FrustumLayer(4)
    const q = new Quaternion()
    const dashed = (f as unknown as { eDash: { array: Float32Array } }).eDash.array
    const fill = (f as unknown as { fPos: { array: Float32Array } }).fPos.array
    f.begin()
    f.add(api, sensor(SENSOR_ACTIVE | SENSOR_FOV_VALID, 1000), 0, 0, 0, q.x, q.y, q.z, q.w, 1.2) // 200 ms old
    f.add(api, sensor(SENSOR_ACTIVE | SENSOR_FOV_VALID, 1000), 0, 0, 0, q.x, q.y, q.z, q.w, 1.4) // 400 ms old > 300
    f.add(api, sensor(SENSOR_ACTIVE, 1000), 0, 0, 0, q.x, q.y, q.z, q.w, 1.0)
    expect(f.add(api, sensor(0, 1000), 0, 0, 0, q.x, q.y, q.z, q.w, 1.0)).toBe(false)
    f.commit()
    expect(f.count).toBe(3)
    expect(dashed[0]).toBe(0)
    expect(dashed[16]).toBe(1)
    // third frustum: all 6 fill vertices at the same point (zero area)
    const o = 2 * 6 * 3
    for (let k = 1; k < 6; k++) for (let j = 0; j < 3; j++) expect(fill[o + 3 * k + j]).toBe(fill[o + j])
    expect(frustumLength(Number.NaN)).toBe(60)
    expect(frustumLength(25)).toBe(25)
    expect(frustumLength(500)).toBe(60)
  })

  it('illuminator cones (D2 WP-11): divergence about the camera axis, dashed, no fill, in the extra slots', () => {
    const f = new FrustumLayer(1)
    const s = sensor(SENSOR_ACTIVE | SENSOR_FOV_VALID, 1000, 80)
    const q = new Quaternion()
    f.begin()
    expect(f.add(api, s, 0, 0, 10, q.x, q.y, q.z, q.w, 1.0)).toBe(true)
    expect(f.add(api, s, 0, 0, 10, q.x, q.y, q.z, q.w, 1.0)).toBe(false) // sensor slots full
    const div = (5.5 * Math.PI) / 180
    expect(f.addIlluminator(api, s, div, 464.8, 0, 0, 10, q.x, q.y, q.z, q.w)).toBe(true)
    expect(f.addIlluminator(api, s, 0.3, 30, 0, 0, 10, q.x, q.y, q.z, q.w)).toBe(true)
    expect(f.addIlluminator(api, s, 0.3, 30, 0, 0, 10, q.x, q.y, q.z, q.w)).toBe(false)
    f.commit()
    const c = f.cornersOf(1, new Float64Array(15))
    const ax = [(c[3] + c[6] + c[9] + c[12]) / 4 - c[0], (c[4] + c[7] + c[10] + c[13]) / 4 - c[1], (c[5] + c[8] + c[11] + c[14]) / 4 - c[2]]
    const L = Math.hypot(ax[0], ax[1], ax[2])
    expect(L).toBeCloseTo(60, 6) // min(R_nir, 60 m)
    for (let k = 1; k <= 4; k++) {
      const d = Math.hypot(c[3 * k] - c[0] - ax[0], c[3 * k + 1] - c[1] - ax[1], c[3 * k + 2] - c[2] - ax[2])
      expect(d).toBeCloseTo(L * Math.tan(div / 2) * Math.SQRT2, 3)
    }
    const dash = (f as unknown as { eDash: { array: Float32Array } }).eDash.array
    expect(dash[16]).toBe(1)
    expect(dash[0]).toBe(0)
    expect(f.count).toBe(3)
    expect(f.drawCount()).toBe(2)
    const out = [{ on: false, divergenceRad: 0, rangeM: 0 }, { on: false, divergenceRad: 0, rangeM: 0 }]
    expect(payloadIlluminators({ sensors: [{ sensor: 'eo', hfov_rad: 0.2, illuminator: { on: true, divergence_rad: 0.2, range_m: 200 } },
      { sensor: 'nir_ill', kind: 'nir_illuminator', active: true }] }, out)).toBe(2)
    expect(out[1].rangeM).toBeCloseTo(464.8, 6)
  })
})
