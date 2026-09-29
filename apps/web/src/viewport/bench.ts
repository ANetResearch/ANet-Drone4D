// Bench drivers of the viewport (M06-FR-059, FR-080; AWR-18 §5.2, §8.6(4)). Owner: M06.
//   ?bench=flight60&scene=<pc|full>  (both builds): once the world context (coordinate.json bytes) of the routed world is
//     loaded, /bench/flight60/<world>.{bin,json} are fetched and verified (coordinate_sha256, bin_sha256; M06-E012 /
//     PERF-E009 refuse to run). After the reveal the camera phase (before the rig) samples the flight by wall clock with
//     M05's sampleFlight60, the controller input is off, camera fov/near/far come from the json; __perf.bench.flightT
//     every frame, __perf.bench.done at t >= 60 s. scene=pc hides every M06 layer (point cloud only, as g02).
//   ?bench=layers&fixedB= (test builds): the camera follows the same flight (looping) and, after the render phase and
//     outside the pass-plan assertion (ctx.benchLayers), every frame renders the base (point cloud only) and base + one
//     layer group 5 times each into an offscreen RT of the drawing-buffer size, gl.finish() (finishForBench) after each,
//     keeps the minima, swaps the order on odd frames and cycles the groups; __perf.bench.pairs[<group>] = { n,
//     medianMs, delta, base, exp } (rings of per-frame values; the increment is the median of the deltas). Done when every
//     group has >= 150 pairs. Groups follow AWR-18 §5.1: drones; trails (trails, frustums, mission, zones, glyphs);
//     environment; groundSky.
import type { PerspectiveCamera, Scene, WebGLRenderTarget } from 'three'
import { Flight60Driver, loadFlight60, perf, perfProbe, register, ring, pushRing, type CameraRig, type FrameCtx, type Ring } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { benchSwitch } from './backend/testSwitches'
import { CH_CLOUD, CH_MAIN, getLayer, type LayerId } from './layers/registry'
import type { RenderBackend } from './renderer'
import { vp } from './session'

export const BENCH = { reps: 5, minPairs: 150, ringCap: 4096, medianEvery: 30 } as const
export const PAIR_GROUPS: readonly (readonly [string, readonly LayerId[]])[] = [
  ['drones', ['drones']],
  ['trails', ['trails', 'frustums', 'mission', 'zones', 'glyphs']],
  ['environment', ['environment']],
  ['groundSky', ['groundSky']],
]

/** scene=pc of flight60: only the point cloud (AWR-18 §8.6(5)) */
export function pcOnlyScene(): boolean {
  const b = benchSwitch()
  return b.mode === 'flight60' && b.scene === 'pc'
}

export interface PairStat { n: number; medianMs: number; delta: Ring; base: Ring; exp: Ring }

/** median of the last n values of a ring (bench path only; allocation-free with the scratch array) */
export function ringMedian(r: Ring, scratch: Float64Array): number {
  const n = Math.min(r.n, r.buf.length, scratch.length)
  if (n === 0) return Number.NaN
  for (let i = 0; i < n; i++) scratch[i] = r.buf[(r.n - 1 - i) & (r.buf.length - 1)]
  const a = scratch.subarray(0, n).sort()
  return n % 2 ? a[(n - 1) >> 1] : 0.5 * (a[n / 2 - 1] + a[n / 2])
}

export function installBench(be: RenderBackend, rig: CameraRig, camera: PerspectiveCamera): () => void {
  const bs = benchSwitch()
  if (bs.mode === '') return () => {}
  const p = perfProbe()
  p.bench.mode = bs.mode
  let driver: Flight60Driver | null = null
  let loadingFor: string | null = null
  let failed = false
  const saved = { fov: camera.fov, near: camera.near, far: camera.far }
  const offs: (() => void)[] = []

  offs.push(register('camera', 'bench.flight60', (ctx: FrameCtx) => {
    const wc = vp.worldCtx
    if (!driver && !failed && wc?.coordinateBytes && wc.worldId === vp.worldId && loadingFor !== wc.worldId) {
      loadingFor = wc.worldId
      const origin = typeof location !== 'undefined' ? location.origin : ''
      loadFlight60(origin, wc.worldId, wc.coordinateBytes).then((f) => {
        driver = new Flight60Driver(f)
        camera.fov = f.json.fov_y_deg
        camera.near = f.json.near_m
        camera.far = f.json.far_m
        camera.updateProjectionMatrix()
        rig.setDriven(true)
      }).catch((e: unknown) => {
        failed = true
        console.error(e instanceof Error ? e.message : `M06-E012 PERF-E009 ${String(e)}`)
      })
    }
    if (!driver || !perf.revealed) return
    if (driver.done) {
      if (bs.mode !== 'layers' || p.bench.done) return
      driver.t0 = Number.NaN // layers: fly the path again until every group has its pairs
      driver.done = false
    }
    const t = driver.step(ctx.nowMs)
    rig.drive(driver.eye, driver.target)
    p.bench.flightT = t
    if (driver.done && bs.mode === 'flight60') p.bench.done = true
  }, { order: -150 }))

  if (bs.mode === 'layers' && TEST_SWITCHES) offs.push(installLayersPairs(be, p))

  return () => {
    for (const off of offs) off()
    if (driver) {
      rig.setDriven(false)
      camera.fov = saved.fov
      camera.near = saved.near
      camera.far = saved.far
      camera.updateProjectionMatrix()
    }
  }
}

function installLayersPairs(be: RenderBackend, p: ReturnType<typeof perfProbe>): () => void {
  const stats: PairStat[] = PAIR_GROUPS.map(() => ({ n: 0, medianMs: Number.NaN, delta: ring(BENCH.ringCap), base: ring(BENCH.ringCap), exp: ring(BENCH.ringCap) }))
  PAIR_GROUPS.forEach(([name], i) => {
    p.bench.pairs[name] = stats[i]
  })
  const scratch = new Float64Array(BENCH.ringCap)
  const hidden: { o: { visible: boolean }; v: boolean }[] = []
  let rt: WebGLRenderTarget | null = null
  let frame = 0
  let group = 0
  const timeOnce = (scene: Scene, cam: PerspectiveCamera): number => {
    const r = be.renderer
    let best = Number.POSITIVE_INFINITY
    for (let k = 0; k < BENCH.reps; k++) {
      const t0 = performance.now()
      r.setRenderTarget(rt)
      r.clear()
      r.render(scene, cam)
      be.finishForBench?.()
      best = Math.min(best, performance.now() - t0)
    }
    return best
  }
  const offTask = register('render', 'bench.layers', (ctx: FrameCtx) => {
    ctx.benchLayers = true
    const scene = vp.scene
    const cam = ctx.camera
    if (!scene || !cam || !perf.revealed || p.bench.done || be.state !== 'READY') return
    // next group that has registered layers with roots
    let g = -1
    for (let k = 0; k < PAIR_GROUPS.length; k++) {
      const c = (group + k) % PAIR_GROUPS.length
      if (PAIR_GROUPS[c][1].some((id) => getLayer(id)?.root)) {
        g = c
        break
      }
    }
    if (g < 0) return
    group = g + 1
    if (!rt || rt.width !== ctx.dbW || rt.height !== ctx.dbH) {
      rt?.dispose()
      rt = be.createRT('bench', { width: ctx.dbW, height: ctx.dbH }) as WebGLRenderTarget
    }
    // experiment group: only the layer roots of group g on the main channel
    const ids = PAIR_GROUPS[g][1]
    hidden.length = 0
    const hideOthers = (children: readonly { visible: boolean }[]): void => {
      for (const child of children) {
        if (child === vp.worldRoot) continue
        const own = ids.some((id) => getLayer(id)?.root === child)
        const isCloud = getLayer('pointcloud')?.root === child
        if (!own && !isCloud && child.visible) {
          hidden.push({ o: child, v: true })
          child.visible = false
        }
      }
    }
    hideOthers(vp.worldRoot.children)
    hideOthers(scene.children)
    const mask0 = cam.layers.mask
    const r = be.renderer
    const target0 = r.getRenderTarget()
    let base: number
    let exp: number
    const odd = (frame++ & 1) === 1
    cam.layers.mask = 1 << CH_CLOUD
    if (odd) {
      cam.layers.mask = (1 << CH_CLOUD) | (1 << CH_MAIN)
      exp = timeOnce(scene, cam)
      cam.layers.mask = 1 << CH_CLOUD
      base = timeOnce(scene, cam)
    } else {
      base = timeOnce(scene, cam)
      cam.layers.mask = (1 << CH_CLOUD) | (1 << CH_MAIN)
      exp = timeOnce(scene, cam)
    }
    cam.layers.mask = mask0
    r.setRenderTarget(target0)
    for (const h of hidden) h.o.visible = h.v
    const s = stats[g]
    pushRing(s.base, base)
    pushRing(s.exp, exp)
    pushRing(s.delta, exp - base)
    s.n++
    if (s.n % BENCH.medianEvery === 0 || s.n === BENCH.minPairs) s.medianMs = ringMedian(s.delta, scratch)
    let done = true
    for (let k = 0; k < PAIR_GROUPS.length; k++) {
      if (!PAIR_GROUPS[k][1].some((id) => getLayer(id)?.root)) continue
      if (stats[k].n < BENCH.minPairs) done = false
    }
    if (done) {
      for (const st of stats) st.medianMs = ringMedian(st.delta, scratch)
      p.bench.done = true
    }
  }, { order: 1000 })
  return () => {
    offTask()
    rt?.dispose()
  }
}
