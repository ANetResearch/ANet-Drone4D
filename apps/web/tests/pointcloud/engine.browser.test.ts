// M05-AC-019 (Tier S part), AC-020, AC-021, AC-022 (programs), AC-023 (material part): the whole engine in the browser on
// SwiftShader WebGL2 with the classic renderer + AnetNodesHandler + GLPointsNodeMaterial, on an in-memory world (three
// nodes on the plane z = 0). The pool is allocated without a CPU mirror and filled through staging + copyTextureToTexture;
// one attribute-less draw; the render target read back shows the points. Class mask hides them without new requests;
// cycling the colour modes adds no program; nodes fade in monotonically over --duration-lod-fade; the EDL composite
// darkens depth edges and outputs the background colour at the far plane.
import { afterAll, describe, expect, it } from 'vitest'
import { DepthTexture, FloatType, Group, Mesh, OrthographicCamera, PerspectiveCamera, PlaneGeometry, Scene, Vector3, WebGLRenderTarget, WebGLRenderer } from 'three'
import type { FrameCtx, RenderBackendView } from '@/engine/loop'
import { PointCloudEngine } from '@/engine/pointcloud/PointCloudEngine'
import { AnetNodesHandler } from '@/viewport/anetNodesHandler'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'
import { MOTION } from '@/lib/tokens/motion.gen'
import { synthWorld } from './synthWorld'

const W = 128
const H = 128
const canvas = document.createElement('canvas')
canvas.width = W
canvas.height = H
const renderer = new WebGLRenderer({ canvas, antialias: false, reversedDepthBuffer: true, alpha: false })
renderer.setNodesHandler(new AnetNodesHandler())
renderer.setPixelRatio(1)
renderer.setSize(W, H, false)
const rt = new WebGLRenderTarget(W, H, { depthBuffer: true })
rt.depthTexture = new DepthTexture(W, H, FloatType)

function backend(tier: 'S' | 'B'): RenderBackendView {
  return {
    tier, deviceClass: tier === 'S' ? 'software' : 'iGPU', kind: 'webgl2', pointSizeMode: 'glpoint',
    caps: { reversedZ: renderer.capabilities.reversedDepthBuffer, timerQuery: false, readbackTopDown: false, maxTextureSize: renderer.capabilities.maxTextureSize, floatColorRT: true },
    startRung: tier === 'S' ? 0 : 3, lowestAllowedRung: tier === 'S' ? 0 : 2,
    textures: { initTexture: (t) => renderer.initTexture(t), copyTextureToTexture: (s, d, r, p) => renderer.copyTextureToTexture(s, d, r, p) },
    createRT: () => rt, readPixels: async (_r, _x, _y, _w, _h, out) => out,
    createPointsMaterial: () => new GLPointsNodeMaterial(), programsCount: () => renderer.info.programs?.length ?? 0,
  } as RenderBackendView
}

interface Stage { engine: PointCloudEngine; scene: Scene; camera: PerspectiveCamera; ctx: FrameCtx; log: string[] }
async function stage(tier: 'S' | 'B' = 'S', motion: 'full' | 'reduced' = 'reduced', height = 150): Promise<Stage> {
  const w = synthWorld()
  const be = backend(tier)
  const engine = new PointCloudEngine({ backend: be, f: w.fetch, useWorker: false, motionTier: () => motion })
  const scene = new Scene()
  const worldRoot = new Group()
  worldRoot.rotation.x = -Math.PI / 2
  worldRoot.add(engine.root)
  scene.add(worldRoot)
  const camera = new PerspectiveCamera(60, W / H, 1, 1000)
  camera.layers.enable(1)
  camera.position.set(0, height, 0.01) // ENU (0, -0.01, height) looking down
  camera.lookAt(0, 0, 0)
  camera.updateMatrixWorld(true)
  const ctx = { frameNo: 0, nowMs: performance.now(), dtMs: 33.3, camera, cssW: W, cssH: H, dbW: W, dbH: H, dpr: 1, cloudScale: 1, tier, deviceClass: be.deviceClass,
    frozen: false, compiledThisFrame: false, be } as unknown as FrameCtx
  await engine.open('http://awr.test/worlds/synth/')
  return { engine, scene, camera, ctx, log: w.log }
}

function frame(s: Stage, dtMs = 33.3): Uint8Array {
  s.ctx.frameNo++
  s.ctx.nowMs += dtMs
  s.engine.update(s.ctx)
  renderer.setRenderTarget(rt)
  renderer.setClearColor(0x000000, 1)
  renderer.clear()
  renderer.render(s.scene, s.camera)
  const px = new Uint8Array(W * H * 4)
  renderer.readRenderTargetPixels(rt, 0, 0, W, H, px)
  renderer.setRenderTarget(null)
  s.engine.sampleFrame(s.ctx)
  return px
}
const lit = (px: Uint8Array): number => {
  let n = 0
  for (let i = 0; i < px.length; i += 4) if (px[i] + px[i + 1] + px[i + 2] > 6) n++
  return n
}

afterAll(() => {
  rt.dispose()
  renderer.dispose()
})

describe('PointCloudEngine on WebGL2 (SwiftShader)', { timeout: 120_000 }, () => {
  it('pool without CPU mirror, one draw, points on screen; deterministic pixels (M05-AC-019)', async () => {
    const s = await stage()
    let px: Uint8Array = new Uint8Array(0)
    for (let k = 0; k < 6; k++) px = frame(s)
    expect(s.engine.enginePhase).toBe('streaming')
    expect(s.engine.stats().drawn).toBe(7000)
    expect(s.engine.drawCount()).toBe(1)
    expect(renderer.info.render.calls).toBe(1)
    const n = lit(px)
    expect(n).toBeGreaterThan(0.3 * W * H) // the plane covers most of the view
    const again = frame(s)
    expect(Array.from(again)).toEqual(Array.from(px))
    expect(s.log.filter((l) => l.includes('octree.bin'))).toEqual(['visual/pointcloud/octree.bin bytes=0-83999'])
    s.engine.dispose()
  })

  it('class mask hides points without requests; colour modes switch without new programs (M05-AC-022)', async () => {
    const s = await stage()
    for (let k = 0; k < 6; k++) frame(s)
    const requests = s.log.length
    s.engine.setClassMask(0x1fff & ~(1 << 5))
    expect(lit(frame(s))).toBe(0)
    s.engine.setClassMask(0x1fff)
    const base = frame(s)
    expect(lit(base)).toBeGreaterThan(0)
    const programs = renderer.info.programs!.length
    const colours: number[] = []
    for (const m of ['hag', 'normal', 'class', 'source', 'intensity', 'height'] as const) {
      s.engine.setColorMode(m)
      const px = frame(s)
      colours.push(px[4 * (64 * W + 64)])
    }
    expect(renderer.info.programs!.length).toBe(programs)
    expect(new Set(colours).size).toBeGreaterThan(1) // the modes shade differently
    expect(s.log.length).toBe(requests)
    s.engine.dispose()
  })

  it('nodes fade in monotonically over --duration-lod-fade and then stay (M05-AC-021)', async () => {
    // 400 m: only the root is selected and every point is clamped to minPx, so coverage depends on the fade alone
    const s = await stage('S', 'full', 400)
    const counts: number[] = []
    for (let t = 0; t <= MOTION.lodFadeMs + 100; t += 25) counts.push(lit(frame(s, 25)))
    for (let k = 1; k < counts.length; k++) expect(counts[k]).toBeGreaterThanOrEqual(counts[k - 1])
    expect(counts[0]).toBeLessThan(counts[counts.length - 1])
    const reduced = await stage('S', 'reduced')
    const first = lit(frame(reduced))
    frame(reduced)
    expect(first).toBe(lit(frame(reduced)))
    s.engine.dispose()
    reduced.engine.dispose()
  })

  it('pick: the ID pass returns the point under the cursor, decoded from the CPU cache (M05-AC-027 material and decode)', async () => {
    const s = await stage()
    for (let k = 0; k < 6; k++) frame(s)
    const cx = 40
    const cy = 50
    // ray through pixel (cx, cy) (RT rows bottom-up) in three coordinates, then ENU (x, -z, y)
    const ndc = new Vector3(((cx + 0.5) / W) * 2 - 1, ((cy + 0.5) / H) * 2 - 1, 0.5)
    const p = ndc.clone().unproject(s.camera)
    const o = s.camera.position.clone()
    const d = p.sub(o).normalize()
    const ticket = s.engine.picker.prepare(cx, cy, [o.x, -o.z, o.y], [d.x, -d.z, d.y])
    expect(ticket, 'ticket').not.toBeNull()
    expect(ticket!.points).toBeGreaterThan(0)
    expect(ticket!.points).toBeLessThanOrEqual(7000)
    const scene = new Scene()
    const root = new Group()
    root.rotation.x = -Math.PI / 2
    scene.add(root)
    root.add(s.engine.root) // the pick object is a child of the layer root, on channel 2
    const cam = s.camera.clone()
    cam.layers.set(2)
    renderer.setRenderTarget(rt)
    renderer.setClearColor(0x000000, 0)
    renderer.clear()
    renderer.render(scene, cam)
    const px = new Uint8Array(5 * 5 * 4)
    renderer.readRenderTargetPixels(rt, cx - 2, cy - 2, 5, 5, px)
    renderer.setRenderTarget(null)
    const pick = await s.engine.picker.decode(ticket!, px)
    expect(pick, `pick from ${Array.from(px).join(',')}`).not.toBeNull()
    // the picked point is on the synthetic planes and projects within a few pixels of the cursor
    expect([0, 20].some((z) => Math.abs(pick!.posEnuM[2] - z) < 0.01)).toBe(true)
    const three = new Vector3(pick!.posEnuM[0], pick!.posEnuM[2], -pick!.posEnuM[1]).project(s.camera)
    const sx = ((three.x + 1) / 2) * W
    const sy = ((three.y + 1) / 2) * H
    expect(Math.hypot(sx - (cx + 0.5), sy - (cy + 0.5))).toBeLessThan(8)
    expect(pick!.classIdx).toBe(5)
    expect(pick!.normal![2]).toBeGreaterThan(0.99)
    // a new prepare voids the ticket
    s.engine.picker.prepare(cx, cy, [o.x, -o.z, o.y], [d.x, -d.z, d.y])
    expect(await s.engine.picker.decode(ticket!, px)).toBeNull()
    s.engine.dispose()
  })

  it('Tier B: round points, EDL composite darkens edges and writes the background at the far plane (M05-AC-023 material)', async () => {
    const s = await stage('B')
    let cloud: Uint8Array = new Uint8Array(0)
    for (let k = 0; k < 6; k++) cloud = frame(s)
    expect(lit(cloud)).toBeGreaterThan(0.2 * W * H) // round points still cover the plane
    const edl = s.engine.edlMaterial!
    expect(edl).not.toBeNull()
    edl.bindTargets(rt.texture, rt.depthTexture!)
    edl.setCamera(s.camera.near, s.camera.far)
    const quad = new Mesh(new PlaneGeometry(2, 2), edl)
    quad.frustumCulled = false
    const qs = new Scene()
    qs.add(quad)
    const qc = new OrthographicCamera(-1, 1, 1, -1, 0, 1)
    const out = new WebGLRenderTarget(W, H)
    const shade = (strength: number): Uint8Array => {
      frame(s)
      edl.uniforms.strength.value = strength
      renderer.setRenderTarget(out)
      renderer.clear()
      renderer.render(qs, qc)
      const px = new Uint8Array(W * H * 4)
      renderer.readRenderTargetPixels(out, 0, 0, W, H, px)
      renderer.setRenderTarget(null)
      return px
    }
    const off = shade(0)
    const on = shade(0.45)
    let sumOff = 0
    let sumOn = 0
    for (let i = 0; i < off.length; i += 4) {
      sumOff += off[i]
      sumOn += on[i]
    }
    expect(sumOn).toBeLessThan(sumOff) // EDL only darkens
    expect(lit(off)).toBeGreaterThan(0)
    out.dispose()
    s.engine.dispose()
  })
})
