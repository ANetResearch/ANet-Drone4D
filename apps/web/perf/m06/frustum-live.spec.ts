// Live sensor frustum (M06-FR-046, AC-035; M13-FR-020..022, M13-to-M06 item 1; FX-WEB1): real backend (supervisor with
// sim-core and api, the default S1 scenario flies two P600), the telemetry phase hands every TelemetryFrame to M13's
// ingestFrame right after the swap, so the SensorPose48 records of the selected vehicle reach engine/sensors:
//   * the camera view of p600-01 turns valid with at least 3 distinct sample times (10 Hz topic, interest set);
//   * the gimbal derived from the samples stays inside the P600 limits ([-90, +30] deg pitch, +-150 deg yaw);
//   * the drawn frustum (vehicle pose at tRender x mount x smoothed gimbal) points along the optical axis of the newest
//     sample (q WORLD<-SENSOR, x = optical axis) within 6 deg, and its apex sits within 8 m of the sample position;
//   * the frame still matches the pass plan and nothing compiles after the reveal.
// Needs a test build (M06_DIST or apps/web/dist) and the Python environment; functional only (no timing thresholds).
import { expect, test } from '@playwright/test'
import { startBackend, type Backend } from '../skeleton.server'
import { DIST, distHasTestHooks } from './server'
import { watch } from './common'

let be: Backend

test.describe.configure({ mode: 'serial' })
test.describe('live sensor frustum', () => {
  test.skip(!distHasTestHooks(), 'needs a VITE_AWR_TEST_SWITCHES=1 build (M06_DIST)')
  test.beforeAll(async () => {
    test.setTimeout(180_000)
    be = await startBackend(undefined, { AWR_WEB_DIST: DIST })
  })
  test.afterAll(async () => {
    await be?.close()
  })

  test('SensorPose48 samples drive the gimbal and the frustum of the selected P600', async ({ page }) => {
    test.setTimeout(240_000)
    const w = watch(page)
    await page.goto(`${be.url}/world/shenzhen`)
    await page.waitForFunction(() => ((window as unknown as { __vp?: { roster(): { id: string }[] } }).__vp?.roster() ?? []).some((e) => e.id === 'p600-01'), null, { timeout: 120_000 })
    await page.evaluate(() => (window as unknown as { __vp: { select(ids: string[]): void } }).__vp.select(['p600-01']))
    type View = { valid: boolean; ring: Float64Array; gimbal?: { az: number; el: number }; name: string }
    type Vp = { roster(): { id: string; agentNo: number }[]; drones(): { frustums: number } | null
      vpSession: { sensors: { sensorsOf(a: number): readonly View[] } | null; drones: { layer: { frustums: { edges: { geometry: { attributes: { position: { array: Float32Array } } } } } } } | null } }
    const handle = await page.waitForFunction(() => {
      const vp = (window as unknown as { __vp: Vp }).__vp
      const no = vp.roster().find((e) => e.id === 'p600-01')?.agentNo ?? -1
      const cam = vp.vpSession.sensors?.sensorsOf(no).find((v) => v.name === 'camera')
      if (!cam?.valid || (vp.drones()?.frustums ?? 0) < 1) return null
      const times = new Set<number>()
      for (let k = 0; k < 4; k++) if (Number.isFinite(cam.ring[9 * k])) times.add(cam.ring[9 * k])
      if (times.size < 3) return null
      // newest sample: [t, qx, qy, qz, qw, px, py, pz, flags]
      let best = -1
      for (let k = 0; k < 4; k++) if (Number.isFinite(cam.ring[9 * k]) && (best < 0 || cam.ring[9 * k] > cam.ring[9 * best])) best = k
      const s = Array.from(cam.ring.slice(9 * best, 9 * best + 9))
      const P = vp.vpSession.drones!.layer.frustums.edges.geometry.attributes.position.array
      // edges 0..3 are apex -> far corner k (FrustumLayer.SEGS), vertex pairs (2e, 2e + 1)
      const apex = [P[0], P[1], P[2]]
      const axis = [0, 0, 0]
      for (let e = 0; e < 4; e++) for (let j = 0; j < 3; j++) axis[j] += P[3 * (2 * e + 1) + j] / 4 - apex[j] / 4
      return { sample: s, apex, axis, gimbal: cam.gimbal ?? null, samples: times.size }
    }, null, { timeout: 90_000, polling: 250 })
    const r = (await handle.jsonValue())!
    const [, qx, qy, qz, qw, px, py, pz] = r.sample
    // optical axis = R(q) (1, 0, 0)
    const ox = 1 - 2 * (qy * qy + qz * qz)
    const oy = 2 * (qx * qy + qz * qw)
    const oz = 2 * (qx * qz - qy * qw)
    const n = Math.hypot(...r.axis)
    const cos = (r.axis[0] * ox + r.axis[1] * oy + r.axis[2] * oz) / Math.max(1e-9, n * Math.hypot(ox, oy, oz))
    const angDeg = (Math.acos(Math.min(1, Math.max(-1, cos))) * 180) / Math.PI
    const apexErr = Math.hypot(r.apex[0] - px, r.apex[1] - py, r.apex[2] - pz)
    test.info().annotations.push({ type: 'frustum', description: `axis vs sample ${angDeg.toFixed(2)} deg, apex ${apexErr.toFixed(2)} m, gimbal ${JSON.stringify(r.gimbal)}` })
    expect(r.samples).toBeGreaterThanOrEqual(3)
    expect(angDeg, 'frustum axis along the sample optical axis').toBeLessThanOrEqual(6)
    expect(apexErr, 'frustum apex at the sensor position').toBeLessThanOrEqual(8)
    expect(r.gimbal).not.toBeNull()
    const el = (r.gimbal!.el * 180) / Math.PI
    const az = (r.gimbal!.az * 180) / Math.PI
    expect(el).toBeGreaterThanOrEqual(-90.5)
    expect(el).toBeLessThanOrEqual(30.5)
    expect(Math.abs(az)).toBeLessThanOrEqual(150.5)
    const gpu = await page.evaluate(() => (window as unknown as { __perf: { gpu: { planMismatches: number; compiledAfterReveal: number } } }).__perf.gpu)
    expect(gpu.planMismatches).toBe(0)
    expect(gpu.compiledAfterReveal).toBe(0)
    expect(w.errors).toEqual([])
    expect(w.m06).toEqual([])
  })
})
