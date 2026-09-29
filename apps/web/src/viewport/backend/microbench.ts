// 300 ms startup microbench (ADR-044; M06 §6.2.3, FR-004, NFR-005). Owner: M06.
// Hardware browsers only, under the boot mask: one million attribute-less glpoint points of 2 px (position = hash of
// vertexIndex inside a unit cube in front of the camera, no CPU data) into a 960 x 540 RGBA8 target. One untimed render
// compiles; then up to 5 timed renders while the total stays below 300 ms: GPU time through
// EXT_disjoint_timer_query_webgl2 when available (results polled for <= 3 frames), otherwise wall time closed by a
// 1-pixel synchronous readPixels (the only synchronous readback allowed, before the reveal; exempt from the M06 lint).
// t_1 > 100 ms ends early (iGPU). Returns the median t_1M in ms.
import { BufferGeometry, PerspectiveCamera, Points, Scene, Sphere, Vector3, WebGLRenderTarget, type WebGLRenderer } from 'three'
import { Fn, builtin, float, fract, vec3, vec4, vertexIndex } from 'three/tsl'
import { GLPointsNodeMaterial } from '../glPointsNodeMaterial'
import { MICROBENCH } from './deviceClass'

type N = any // TSL nodes

export interface MicrobenchResult { t1M: number; runs: number; totalMs: number; gpuTimer: boolean; aborted: boolean }

const nextFrame = (): Promise<void> => new Promise((ok) => (typeof requestAnimationFrame === 'function' ? requestAnimationFrame(() => ok()) : setTimeout(ok, 16)))

export function makeBenchPoints(n: number, sizePx: number): Points {
  const g = new BufferGeometry()
  g.setDrawRange(0, n)
  g.boundingSphere = new Sphere(new Vector3(0, 0, -2), 4)
  const m = new GLPointsNodeMaterial()
  m.positionNode = Fn(() => {
    const v: N = float(vertexIndex)
    const x: N = fract(v.mul(0.6180339887)).sub(0.5)
    const y: N = fract(v.mul(0.7548776662)).sub(0.5)
    const z: N = fract(v.mul(0.5698402910)).sub(2.5)
    builtin('gl_PointSize').assign(float(sizePx))
    return vec3(x, y, z)
  })() as N
  m.colorNode = vec4(1, 1, 1, 1)
  const p = new Points(g, m)
  p.frustumCulled = false
  return p
}

export async function runMicrobench(r: WebGLRenderer, o: { budgetMs?: number; points?: number } = {}): Promise<MicrobenchResult> {
  const budget = o.budgetMs ?? MICROBENCH.budgetMs
  const gl = r.getContext() as WebGL2RenderingContext
  const ext = gl.getExtension('EXT_disjoint_timer_query_webgl2') as { TIME_ELAPSED_EXT: number; GPU_DISJOINT_EXT: number } | null
  const rt = new WebGLRenderTarget(MICROBENCH.w, MICROBENCH.h, { depthBuffer: false })
  const scene = new Scene()
  const pts = makeBenchPoints(o.points ?? MICROBENCH.points, MICROBENCH.sizePx)
  scene.add(pts)
  const cam = new PerspectiveCamera(60, MICROBENCH.w / MICROBENCH.h, 0.5, 20)
  cam.updateMatrixWorld()
  const prev = r.getRenderTarget()
  const px = new Uint8Array(4)
  const times: number[] = []
  let total = 0
  let aborted = false
  try {
    r.setRenderTarget(rt)
    r.render(scene, cam) // compile, untimed
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
    for (let k = 0; k < MICROBENCH.runs && total < budget; k++) {
      let t: number
      if (ext) {
        const q = gl.createQuery()!
        gl.beginQuery(ext.TIME_ELAPSED_EXT, q)
        r.render(scene, cam)
        gl.endQuery(ext.TIME_ELAPSED_EXT)
        t = Number.NaN
        for (let f = 0; f < 3 && Number.isNaN(t); f++) {
          await nextFrame()
          if (gl.getQueryParameter(q, gl.QUERY_RESULT_AVAILABLE) && !gl.getParameter(ext.GPU_DISJOINT_EXT)) t = Number(gl.getQueryParameter(q, gl.QUERY_RESULT)) / 1e6
        }
        gl.deleteQuery(q)
        if (Number.isNaN(t)) continue
      } else {
        const a = performance.now()
        r.render(scene, cam)
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
        t = performance.now() - a
      }
      times.push(t)
      total += t
      if (k === 0 && t > MICROBENCH.abortMs) {
        aborted = true
        break
      }
    }
  } finally {
    r.setRenderTarget(prev)
    rt.dispose()
    pts.geometry.dispose()
    ;(pts.material as GLPointsNodeMaterial).dispose()
  }
  const s = [...times].sort((a, b) => a - b)
  const t1M = s.length ? (s[Math.floor(s.length / 2)] * 1_000_000) / (o.points ?? MICROBENCH.points) : Number.POSITIVE_INFINITY
  return { t1M, runs: times.length, totalMs: total, gpuTimer: ext !== null, aborted }
}
