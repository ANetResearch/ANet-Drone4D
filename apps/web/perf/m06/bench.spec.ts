// M06-AC-042 and FR-059 / FR-080 (AWR-18 §5.2, §8.6(4)): functional checks of the bench drivers, no timing thresholds.
//   flight60: ?bench=flight60&scene=pc drives the camera from /bench/flight60/shenzhen.bin (the camera eye equals the
//             linear interpolation of the .bin at __perf.bench.flightT within 1e-3 m, fov/near/far from the json),
//             only the point cloud is drawn, __perf.bench.done at t >= 60 s; a coordinate_sha256 mismatch refuses to
//             run (M06-E012 / PERF-E009).
//   layers:   ?bench=layers&fixedB=25000 (test build) collects paired base / base + group timings for the registered
//             groups outside the pass plan (planMismatches stays 0).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { openWorld, perf, watch } from './common'
import { DIST, startM06Server, type M06Server } from './server'

let srv: M06Server | null = null
test.beforeAll(async () => {
  srv = await startM06Server()
})
test.afterAll(async () => {
  await srv?.close()
})

function sample(buf: Float32Array, t: number): number[] {
  const x = Math.max(0, Math.min(t, 60)) * 60
  const k = Math.min(Math.floor(x), 3599)
  const f = x - k
  return [0, 1, 2, 3, 4, 5].map((i) => buf[6 * k + i] + (buf[6 * k + 6 + i] - buf[6 * k + i]) * f)
}

type Cam = { position: { x: number; y: number; z: number }; fov: number; near: number; far: number }
const camState = () => {
  const w = window as unknown as { __vp: { vpSession: { camera: Cam | null } }; __perf: { bench: { flightT: number; done: boolean; mode: string } } }
  const c = w.__vp.vpSession.camera!
  return { t: w.__perf.bench.flightT, done: w.__perf.bench.done, mode: w.__perf.bench.mode, eye: [c.position.x, -c.position.z, c.position.y], fov: c.fov, near: c.near, far: c.far }
}

test('flight60 drives the camera from the .bin and ends at 60 s (scene=pc)', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  const bin = readFileSync(join(DIST, 'bench/flight60/shenzhen.bin'))
  const buf = new Float32Array(bin.buffer.slice(bin.byteOffset, bin.byteOffset + bin.byteLength))
  const json = JSON.parse(readFileSync(join(DIST, 'bench/flight60/shenzhen.json'), 'utf8')) as { fov_y_deg: number; near_m: number; far_m: number }
  await openWorld(page, srv!.url, 'bench=flight60&scene=pc&source=fake')
  await page.waitForFunction(() => (window as unknown as { __perf: { bench: { flightT: number } } }).__perf.bench.flightT > 1, null, { timeout: 60_000 })
  for (let i = 0; i < 5; i++) {
    const s = await page.evaluate(camState)
    expect(s.mode).toBe('flight60')
    const want = sample(buf, s.t)
    for (let a = 0; a < 3; a++) expect(Math.abs(s.eye[a] - want[a])).toBeLessThan(1e-3)
    expect([s.fov, s.near, s.far]).toEqual([json.fov_y_deg, json.near_m, json.far_m])
    await page.waitForTimeout(700)
  }
  // scene=pc: only the point cloud draws (M06 layers report no draws)
  const draws = await page.evaluate(() => {
    const L = (window as unknown as { __perf: { layers: Record<string, { draws: number }> } }).__perf.layers
    return { drones: L.drones.draws, groundSky: L.groundSky.draws, trails: L.trails.draws }
  })
  expect(draws).toEqual({ drones: 0, groundSky: 0, trails: 0 })
  await page.waitForFunction(() => (window as unknown as { __perf: { bench: { done: boolean } } }).__perf.bench.done, null, { timeout: 150_000 })
  expect(await perf<number>(page, 'bench.flightT')).toBeGreaterThanOrEqual(60)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})

test('flight60 refuses a coordinate_sha256 mismatch (M06-E012 / PERF-E009)', async ({ page }) => {
  const errors: string[] = []
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text())
  })
  await page.route('**/bench/flight60/shenzhen.json', async (route) => {
    const r = await route.fetch()
    const j = (await r.json()) as Record<string, unknown>
    await route.fulfill({ response: r, json: { ...j, coordinate_sha256: '0'.repeat(64) } })
  })
  await openWorld(page, srv!.url, 'bench=flight60&scene=pc&source=fake')
  await page.waitForTimeout(3000)
  expect(errors.some((e) => /M06-E012/.test(e) && /PERF-E009/.test(e))).toBe(true)
  expect(await perf<number>(page, 'bench.flightT')).toBe(-1)
})

test('layers pairs: base and base + group timings outside the pass plan', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv!.url, 'bench=layers&fixedB=25000&source=fake&fakeN=5')
  await page.waitForFunction(() => {
    const pairs = (window as unknown as { __perf: { bench: { pairs: Record<string, { n?: number }> } } }).__perf.bench.pairs
    return (pairs.drones?.n ?? 0) >= 6 && (pairs.groundSky?.n ?? 0) >= 6 && (pairs.trails?.n ?? 0) >= 6
  }, null, { timeout: 90_000 })
  const pairs = await page.evaluate(() => {
    const p = (window as unknown as { __perf: { bench: { pairs: Record<string, { n: number; base: { buf: Float64Array; n: number }; exp: { buf: Float64Array; n: number } }> } } }).__perf.bench.pairs
    const out: Record<string, { n: number; base: number; exp: number }> = {}
    for (const k of ['drones', 'trails', 'groundSky']) out[k] = { n: p[k].n, base: p[k].base.buf[0], exp: p[k].exp.buf[0] }
    return out
  })
  for (const v of Object.values(pairs)) {
    expect(v.base).toBeGreaterThan(0)
    expect(v.exp).toBeGreaterThan(0)
  }
  expect(await perf<string>(page, 'bench.mode')).toBe('layers')
  expect(await perf<number>(page, 'gpu.planMismatches')).toBe(0)
  expect(w.errors).toEqual([])
  expect(w.m06).toEqual([])
})
