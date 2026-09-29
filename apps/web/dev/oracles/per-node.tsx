// Per-node oracle page O1 (D1-ext, M05-FR-057; g02 §4.2 bench.mjs "O3d"). Owner: M05. Dev build only.
// The product selector (APH) picks nodes for a pose and a budget; each selected node is drawn as its own THREE.Points
// with its own VBO (the design M05 rejected for the product, one draw per node) using the same Lite point size rule
// (childDrawnMask from the selection, sizeK 1.7, rung minPx / maxPx). Coverage masks are comparable with the product
// PointPool + DrawTable path and with dev/oracles/fullref.html:
//   await window.__pernode.ready; const { mask, draws, points } = await window.__pernode.render(pose, B, rung)
import { BufferAttribute, BufferGeometry, GLSL3, PerspectiveCamera, Points, RawShaderMaterial, Scene, WebGLRenderTarget, WebGLRenderer } from 'three'
import { NodeStore } from '@/engine/pointcloud/core/NodeStore'
import { newScratch, newSelection, selectVisible } from '@/engine/pointcloud/core/Selector'
import { lodCameraLookAt, newLodCamera } from '@/engine/pointcloud/core/frustum'
import { parseHierarchy, parseHierarchyExt } from '@/engine/pointcloud/io/hierarchy'
import { LADDER, PC } from '@/engine/pointcloud/params'
import type { PotreeMeta, WorldJson } from '@/engine/pointcloud/types'
import type { Pose } from './fullref'

const W = 640
const H = 360
const q = new URLSearchParams(location.search)
const base = `${location.origin}/worlds/${q.get('world') ?? 'shenzhen'}/`

interface Loaded { t: NodeStore; octree: ArrayBuffer[] }

async function load(): Promise<Loaded> {
  const w = (await (await fetch(`${base}world.json`, { cache: 'no-cache' })).json()) as WorldJson
  const v = `?v=${encodeURIComponent(w.contentVersion)}`
  const layer = w.layers.find((l) => l.type === 'pointcloud' && l.default)!
  const octree: ArrayBuffer[] = []
  const inputs = []
  for (const r of layer.roots!) {
    const md = (await (await fetch(`${base}${r.href}metadata.json${v}`)).json()) as PotreeMeta
    const records = parseHierarchy(await (await fetch(`${base}${r.href}hierarchy.bin${v}`)).arrayBuffer(), md.hierarchy.firstChunkSize)
    const xr = await fetch(`${base}${r.href}hierarchy_ext.bin${v}`)
    const ext = xr.ok ? parseHierarchyExt(await xr.arrayBuffer(), records.length) : null
    octree.push(await (await fetch(`${base}${r.href}octree.bin${v}`)).arrayBuffer())
    inputs.push({ cubeMin: md.boundingBox.min, cubeSize: md.boundingBox.max[0] - md.boundingBox.min[0], spacing: md.spacing, records, ext, octreeUrl: '' })
  }
  return { t: new NodeStore(inputs), octree }
}

const material = (pitch: number, half: number, minPx: number, maxPx: number, projK: number) => new RawShaderMaterial({
  glslVersion: GLSL3,
  uniforms: { pitch: { value: pitch }, childMask: { value: half }, minPx: { value: minPx }, maxPx: { value: maxPx }, projK: { value: projK } },
  vertexShader: `
    in vec3 position; in vec3 q;
    uniform mat4 modelViewMatrix; uniform mat4 projectionMatrix; uniform float pitch, childMask, minPx, maxPx, projK;
    void main() {
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      gl_Position = projectionMatrix * mv;
      float oct = (q.x >= 0.5 ? 4.0 : 0.0) + (q.y >= 0.5 ? 2.0 : 0.0) + (q.z >= 0.5 ? 1.0 : 0.0);
      float half_ = mod(floor(childMask / exp2(oct)), 2.0);
      gl_PointSize = clamp(1.7 * (half_ > 0.5 ? 0.5 * pitch : pitch) * projK / max(-mv.z, 1e-6), minPx, maxPx);
    }`,
  fragmentShader: 'precision highp float; out vec4 c; void main() { c = vec4(1.0); }',
})

const canvas = document.getElementById('c') as HTMLCanvasElement
const renderer = new WebGLRenderer({ canvas, antialias: false })
renderer.setPixelRatio(1)
renderer.setSize(W, H, false)
const rt = new WebGLRenderTarget(W, H)
const ready = load()

async function render(pose: Pose, B: number, rungIndex = 0): Promise<{ mask: Uint8Array; draws: number; points: number }> {
  const { t, octree } = await ready
  const rung = LADDER[rungIndex]
  const cam = newLodCamera()
  lodCameraLookAt(pose.eye, pose.target, pose.fovYDeg, W, H, pose.near, pose.far, H, cam)
  const sel = selectVisible(t, cam, { tau: rung.tau, B, headroom: PC.headroom, maxNodes: PC.maxNodes, maxSkips: PC.maxSkips, minPrefix: PC.minPrefix, hysteresis: 0,
    depthCap: 255, tauMinFrac: PC.tauMinFrac }, newScratch(t.N), newSelection(PC.maxNodes))
  const selected = new Set(Array.from(sel.idx.subarray(0, sel.n)))
  const scene = new Scene()
  const root = new Points()
  root.rotation.x = -Math.PI / 2
  scene.add(root)
  const projK = (0.5 * H) / Math.tan((pose.fovYDeg * Math.PI) / 360)
  for (let k = 0; k < sel.n; k++) {
    const i = sel.idx[k]
    const n = sel.cnt[k]
    if (!n) continue
    const u16 = new Uint16Array(octree[t.rootOf[i]], t.byteOffset[i], 4 * n)
    const P = new Float32Array(3 * n)
    const Q = new Float32Array(3 * n)
    const s = t.cubeSize[i]
    for (let j = 0; j < n; j++) {
      for (let a = 0; a < 3; a++) {
        Q[3 * j + a] = u16[4 * j + a] / 65535
        P[3 * j + a] = t.cubeMin[3 * i + a] + Q[3 * j + a] * s
      }
    }
    let mask = 0
    for (let c = 0; c < 8; c++) if (selected.has(t.children[8 * i + c])) mask |= 1 << c
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(P, 3))
    g.setAttribute('q', new BufferAttribute(Q, 3))
    const pts = new Points(g, material(t.spacing[i], mask, rung.minPx, rung.maxPx, projK))
    pts.frustumCulled = false
    root.add(pts)
  }
  const cam3 = new PerspectiveCamera(pose.fovYDeg, W / H, pose.near, pose.far)
  cam3.position.set(pose.eye[0], pose.eye[2], -pose.eye[1])
  cam3.lookAt(pose.target[0], pose.target[2], -pose.target[1])
  cam3.updateMatrixWorld(true)
  renderer.setRenderTarget(rt)
  renderer.setClearColor(0x000000, 1)
  renderer.clear()
  renderer.info.reset()
  renderer.render(scene, cam3)
  const draws = renderer.info.render.calls
  const px = new Uint8Array(W * H * 4)
  renderer.readRenderTargetPixels(rt, 0, 0, W, H, px)
  renderer.setRenderTarget(null)
  const out = new Uint8Array(Math.ceil((W * H) / 8))
  for (let i = 0; i < W * H; i++) if (px[4 * i] > 127) out[i >> 3] |= 1 << (i & 7)
  for (const c of root.children) {
    ;(c as Points).geometry.dispose()
    ;((c as Points).material as RawShaderMaterial).dispose()
  }
  return { mask: out, draws, points: sel.points }
}

;(window as unknown as { __pernode: unknown }).__pernode = { ready, render, width: W, height: H }
