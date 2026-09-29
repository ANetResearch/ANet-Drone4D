// Full-reference coverage renderer (D1-ext, M05-FR-054, FR-057; AWR-18 §4.5 item 3). Owner: M05. Dev build only
// (vite dev serves /dev/oracles/fullref.html; the production build never includes it).
// Loads every node of every root of a world (octree.bin whole, ANET_Q16 12 B/point) into one VBO and renders coverage
// masks at the given camera poses with the Lite point size of a full selection (every node drawn: pitch = spacing_L / 2
// where the point's octant has a child node, else spacing_L), sizeK 1.7, minPx 1, maxPx 8, into a 640 x 360 target.
// Usage (Playwright or a person): /dev/oracles/fullref.html?world=shenzhen, then
//   await window.__fullref.ready; const masks = await window.__fullref.render(poses)
// pose = { eye: [x, y, z], target: [x, y, z], fovYDeg, near, far } in world ENU; a mask is 1 bit per pixel packed in a
// Uint8Array (row-major, rows bottom-up, the __perf.quality.samples layout). Plain GLSL: a dev oracle, not product code.
import { BufferAttribute, BufferGeometry, PerspectiveCamera, Points, RawShaderMaterial, Scene, WebGLRenderTarget, WebGLRenderer, GLSL3 } from 'three'
import { parseHierarchy } from '@/engine/pointcloud/io/hierarchy'
import type { PotreeMeta, WorldJson } from '@/engine/pointcloud/types'

const W = 640
const H = 360
export interface Pose { eye: number[]; target: number[]; fovYDeg: number; near: number; far: number }

const q = new URLSearchParams(location.search)
const world = q.get('world') ?? 'shenzhen'
const base = `${location.origin}/worlds/${world}/`

async function load(): Promise<Points> {
  const w = (await (await fetch(`${base}world.json`, { cache: 'no-cache' })).json()) as WorldJson
  const v = `?v=${encodeURIComponent(w.contentVersion)}`
  const layer = w.layers.find((l) => l.type === 'pointcloud' && l.default)!
  const pos: Float32Array[] = []
  const pitch: Float32Array[] = []
  for (const r of layer.roots!) {
    const md = (await (await fetch(`${base}${r.href}metadata.json${v}`)).json()) as PotreeMeta
    const recs = parseHierarchy(await (await fetch(`${base}${r.href}hierarchy.bin${v}`)).arrayBuffer(), md.hierarchy.firstChunkSize)
    const oct = await (await fetch(`${base}${r.href}octree.bin${v}`)).arrayBuffer()
    const size0 = md.boundingBox.max[0] - md.boundingBox.min[0]
    const cmin: number[][] = []
    const csize: number[] = []
    const kids: number[] = []
    recs.forEach((rec, i) => {
      if (rec.parent < 0) {
        cmin[i] = [...md.boundingBox.min]
        csize[i] = size0
      } else {
        const s = csize[rec.parent] / 2
        const p = cmin[rec.parent]
        cmin[i] = [p[0] + s * ((rec.child >> 2) & 1), p[1] + s * ((rec.child >> 1) & 1), p[2] + s * (rec.child & 1)]
        csize[i] = s
        kids[rec.parent] = (kids[rec.parent] ?? 0) | (1 << rec.child)
      }
    })
    recs.forEach((rec, i) => {
      const n = rec.numPoints
      if (!n) return
      const u16 = new Uint16Array(oct, rec.byteOffset, 4 * n)
      const P = new Float32Array(3 * n)
      const S = new Float32Array(n)
      const sp = md.spacing / 2 ** rec.level
      for (let k = 0; k < n; k++) {
        const qx = u16[4 * k] / 65535
        const qy = u16[4 * k + 1] / 65535
        const qz = u16[4 * k + 2] / 65535
        P[3 * k] = cmin[i][0] + qx * csize[i]
        P[3 * k + 1] = cmin[i][1] + qy * csize[i]
        P[3 * k + 2] = cmin[i][2] + qz * csize[i]
        const o = (qx >= 0.5 ? 4 : 0) | (qy >= 0.5 ? 2 : 0) | (qz >= 0.5 ? 1 : 0)
        S[k] = ((kids[i] ?? 0) >> o) & 1 ? 0.5 * sp : sp
      }
      pos.push(P)
      pitch.push(S)
    })
  }
  const total = pitch.reduce((a, s) => a + s.length, 0)
  const P = new Float32Array(3 * total)
  const S = new Float32Array(total)
  let o = 0
  pos.forEach((p, k) => {
    P.set(p, 3 * o)
    S.set(pitch[k], o)
    o += pitch[k].length
  })
  const g = new BufferGeometry()
  g.setAttribute('position', new BufferAttribute(P, 3))
  g.setAttribute('pitch', new BufferAttribute(S, 1))
  const m = new RawShaderMaterial({
    glslVersion: GLSL3,
    uniforms: { projK: { value: 1 } },
    vertexShader: `
      in vec3 position; in float pitch;
      uniform mat4 modelViewMatrix; uniform mat4 projectionMatrix; uniform float projK;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = clamp(1.7 * pitch * projK / max(-mv.z, 1e-6), 1.0, 8.0);
      }`,
    fragmentShader: `
      precision highp float; out vec4 c;
      void main() { c = vec4(1.0); }`,
  })
  const pts = new Points(g, m)
  pts.frustumCulled = false
  pts.rotation.x = -Math.PI / 2 // ENU -> three (WorldRoot convention)
  return pts
}

const canvas = document.getElementById('c') as HTMLCanvasElement
const renderer = new WebGLRenderer({ canvas, antialias: false })
renderer.setPixelRatio(1)
renderer.setSize(W, H, false)
const rt = new WebGLRenderTarget(W, H)
const scene = new Scene()
const ready = load().then((p) => scene.add(p))

async function render(poses: Pose[]): Promise<Uint8Array[]> {
  await ready
  const pts = scene.children[0] as Points
  const out: Uint8Array[] = []
  const px = new Uint8Array(W * H * 4)
  for (const p of poses) {
    const cam = new PerspectiveCamera(p.fovYDeg, W / H, p.near, p.far)
    cam.position.set(p.eye[0], p.eye[2], -p.eye[1])
    cam.lookAt(p.target[0], p.target[2], -p.target[1])
    cam.updateMatrixWorld(true)
    ;(pts.material as RawShaderMaterial).uniforms.projK.value = (0.5 * H) / Math.tan((p.fovYDeg * Math.PI) / 360)
    renderer.setRenderTarget(rt)
    renderer.setClearColor(0x000000, 1)
    renderer.clear()
    renderer.render(scene, cam)
    renderer.readRenderTargetPixels(rt, 0, 0, W, H, px)
    const mask = new Uint8Array(Math.ceil((W * H) / 8))
    for (let i = 0; i < W * H; i++) if (px[4 * i] > 127) mask[i >> 3] |= 1 << (i & 7)
    out.push(mask)
  }
  renderer.setRenderTarget(null)
  return out
}

/** hole rate |ref and not test| / |ref| of two packed masks (AWR-18 §4.5 item 4) */
function holeRate(ref: Uint8Array, test: Uint8Array): number {
  let r = 0
  let miss = 0
  for (let i = 0; i < ref.length; i++) {
    const a = ref[i]
    for (let b = 0; b < 8; b++) {
      if (!((a >> b) & 1)) continue
      r++
      if (!((test[i] >> b) & 1)) miss++
    }
  }
  return r ? miss / r : 0
}

;(window as unknown as { __fullref: unknown }).__fullref = { ready, render, holeRate, width: W, height: H }
