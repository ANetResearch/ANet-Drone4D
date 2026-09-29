// M13-AC-012 (Node part): gimbal angles derived from a SensorPose48 sample and the body orientation at the sample time;
// frustum aligned with the nose while the vehicle yaws at 90 deg/s; critically damped follow; stale samples.
import { describe, expect, it } from 'vitest'
import { GIMBAL_TAU_S, dampGimbal, gimbalFromSample, nlerp, quatMul, type GimbalState } from '@/engine/sensors/gimbalTrack'
import { frustumCorners, gimbalR, mountMatrix } from '@/engine/sensors/intrinsics'
import { SensorCache } from '@/engine/sensors/sensorCache'

const D = Math.PI / 180

function yawQ(yaw: number): Float64Array {
  return Float64Array.of(0, 0, Math.sin(yaw / 2), Math.cos(yaw / 2))
}
/** quaternion of a row-major 3x3 (Shepperd, w >= 0) */
function rToQ(m: Float64Array): Float64Array {
  const tr = m[0] + m[4] + m[8]
  const q = new Float64Array(4)
  if (tr > 0) {
    const s = Math.sqrt(tr + 1) * 2
    q[3] = s / 4
    q[0] = (m[7] - m[5]) / s
    q[1] = (m[2] - m[6]) / s
    q[2] = (m[3] - m[1]) / s
  } else if (m[0] > m[4] && m[0] > m[8]) {
    const s = Math.sqrt(1 + m[0] - m[4] - m[8]) * 2
    q[3] = (m[7] - m[5]) / s
    q[0] = s / 4
    q[1] = (m[1] + m[3]) / s
    q[2] = (m[2] + m[6]) / s
  } else if (m[4] > m[8]) {
    const s = Math.sqrt(1 + m[4] - m[0] - m[8]) * 2
    q[3] = (m[2] - m[6]) / s
    q[0] = (m[1] + m[3]) / s
    q[1] = s / 4
    q[2] = (m[5] + m[7]) / s
  } else {
    const s = Math.sqrt(1 + m[8] - m[0] - m[4]) * 2
    q[3] = (m[3] - m[1]) / s
    q[0] = (m[2] + m[6]) / s
    q[1] = (m[5] + m[7]) / s
    q[2] = s / 4
  }
  return q
}
/** q_ws of a sensor: q_wb · q(mount·gimbal) */
function sensorQ(qwb: Float64Array, mount: Float64Array, az: number, el: number): Float64Array {
  const g = gimbalR(az, el, new Float64Array(9))
  const m = [mount[0], mount[1], mount[2], mount[4], mount[5], mount[6], mount[8], mount[9], mount[10]]
  const r = new Float64Array(9)
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) r[3 * i + j] = m[3 * i] * g[j] + m[3 * i + 1] * g[3 + j] + m[3 * i + 2] * g[6 + j]
  return quatMul(qwb, rToQ(r), new Float64Array(4))
}

describe('gimbalFromSample', () => {
  it('recovers az/el through a non-trivial mount', () => {
    const mount = mountMatrix([0.1, 0, -0.05], [3, 12, -7], new Float64Array(16))
    const out = new Float64Array(2)
    for (const [az, el] of [[0, -15], [40, -60], [-120, 20], [10, -89]]) {
      const qwb = Float64Array.of(0.1, -0.2, 0.3, 0.9)
      const n = Math.hypot(...qwb)
      for (let i = 0; i < 4; i++) qwb[i] /= n
      expect(gimbalFromSample(sensorQ(qwb, mount, az * D, el * D), qwb, mount, out)).toBe(true)
      expect(out[0] / D).toBeCloseTo(az, 9)
      expect(out[1] / D).toBeCloseTo(el, 9)
    }
  })
})

describe('frustum stays on the nose at 90 deg/s yaw (M13-AC-012)', () => {
  it('deviation <= 0.5 deg', () => {
    const c = new SensorCache()
    c.modelOf = () => 'p600'
    const cam = c.sensorsOf(1)[0]
    const rate = 90 * D
    let worst = 0
    // body orientation history at 60 Hz, SensorPose48 at 10 Hz, render at 60 fps with a 100 ms render delay
    for (let f = 0; f <= 180; f++) {
      const t = (f * 1000) / 60
      c.pushOrientation(1, t, ...(Array.from(yawQ(rate * t / 1000)) as [number, number, number, number]))
      if (f % 6 === 0) {
        const ts = t - 20 // pose sample time slightly behind the newest body sample
        const q = sensorQ(yawQ((rate * ts) / 1000), cam.mount, 0, -15 * D)
        c.onPose(1, 0, ts, 3, 0, 0, 0, q[0], q[1], q[2], q[3])
      }
      const tRender = t - 100
      c.update(1000 / 60, tRender, false)
      if (tRender < 1000) continue
      const fc = frustumCorners(cam, 60, new Float64Array(15))
      // optical axis in the body frame = mean of the far corners minus origin; rotate by the body yaw at tRender
      let ax = 0
      let ay = 0
      for (let k = 0; k < 4; k++) {
        ax += fc[3 + 3 * k] - fc[0]
        ay += fc[4 + 3 * k] - fc[1]
      }
      const yaw = (rate * tRender) / 1000
      const dirYaw = Math.atan2(ay, ax) + yaw
      worst = Math.max(worst, Math.abs(Math.atan2(Math.sin(dirYaw - yaw), Math.cos(dirYaw - yaw))))
    }
    expect(worst / D).toBeLessThanOrEqual(0.5)
    expect(c.resolved).toBeGreaterThan(20)
    expect(Math.abs(cam.gimbal.el / D + 15)).toBeLessThan(0.01)
  })
})

describe('critically damped follow', () => {
  it('reaches the target without overshoot, tau 0.15 s, frame-rate independent', () => {
    const run = (dtMs: number): number[] => {
      const g: GimbalState = { az: 0, el: 0, azT: 1, elT: -0.5, vaz: 0, vel: 0 }
      const out: number[] = []
      for (let t = 0; t < 1500; t += dtMs) {
        dampGimbal(g, dtMs / 1000, false)
        if (Math.abs(t + dtMs - 300) < 1e-6 || Math.abs(t + dtMs - 1500) < 1e-6) out.push(g.az)
        expect(g.az).toBeLessThanOrEqual(1 + 1e-12)
      }
      return out
    }
    const a = run(1000 / 60)
    const b = run(50)
    // exact solution: 1 - (1 + t/tau) e^(-t/tau) at 0.3 s
    const x = 1 - (1 + 0.3 / GIMBAL_TAU_S) * Math.exp(-0.3 / GIMBAL_TAU_S)
    expect(b[0]).toBeCloseTo(x, 9)
    expect(a[a.length - 1]).toBeCloseTo(1, 3)
  })
  it('reduced motion jumps to the target', () => {
    const g: GimbalState = { az: 0, el: 0, azT: 0.7, elT: -0.3, vaz: 1, vel: 1 }
    dampGimbal(g, 0.016, true)
    expect(g).toEqual({ az: 0.7, el: -0.3, azT: 0.7, elT: -0.3, vaz: 0, vel: 0 })
  })
})

describe('staleness (M13-FR-022)', () => {
  it('FOV_VALID drops after 3 / f and the M06 ring convention holds', () => {
    const c = new SensorCache()
    c.modelOf = () => 'p600'
    const v = c.sensorsOf(4)[0]
    expect(v.valid).toBe(false)
    expect(v.fovValid).toBe(true)
    const q = yawQ(0)
    c.pushOrientation(4, 1000, q[0], q[1], q[2], q[3])
    c.onPose(4, 0, 1000, 3, 1, 2, 3, q[0], q[1], q[2], q[3])
    c.update(16, 1200, false)
    expect(v.fovValid).toBe(true)
    expect(v.poseAgeS).toBeCloseTo(0.2, 9)
    c.update(16, 1301, false)
    expect(v.fovValid).toBe(false)
    const k = (v.ringHead - 1 + 4) % 4
    expect(v.ring[9 * k]).toBe(1000)
    expect(v.ring[9 * k + 8]).toBe(3)
    c.onPose(4, 0, 1350, 1, 1, 2, 3, q[0], q[1], q[2], q[3]) // ACTIVE without FOV_VALID
    c.update(16, 1360, false)
    expect(v.fovValid).toBe(false)
    expect(v.active).toBe(true)
  })
  it('nlerp aligns hemispheres', () => {
    const out = new Float64Array(4)
    nlerp([0, 0, 0, 1], 0, [0, 0, 0, -1], 0, 0.5, out)
    expect(Array.from(out)).toEqual([0, 0, 0, 1])
  })
})
