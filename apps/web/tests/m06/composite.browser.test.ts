// M06-AC-021 and AC-048 (M06 §6.5, FR-026, FR-070; g01 §6.5): on Tier B the P1 cloudRT + P2 composite + P3 main pass
// output equals the single Tier S pass of the same scene pixel for pixel at s = 1 (colour, far-plane sky, depth written
// back so main-channel objects are occluded by the cloud), with the M06 composite and with the M05 EDL composite at
// strength 0; render.calls equals the plan; s = 0.6 changes only the sub-viewport (round(db x s)) and the uvScale
// uniform: no render-target allocation and the default framebuffer viewport is untouched.
import { describe, expect, it } from 'vitest'
import { Mesh, PerspectiveCamera, PlaneGeometry, Scene, Vector4 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { vec3 } from 'three/tsl'
import { ctx as frameCtx, makeEdlCompositeMaterial, perfProbe } from '@/engine'
import { createRenderBackend, newPassPlan, type RenderBackend } from '@/viewport/renderer'
import { registerLayer } from '@/viewport/layers/registry'
import { makeSkyQuadMaterial, makeSkyUniforms } from '@/viewport/layers/groundSky.materials'

type N = any

// withSky: the Tier S reference carries the SkyQuad (renderOrder -1000, no depth); Tier B shades far-plane pixels of
// the composite with the same sky Fn (default uniforms on both sides)
function scene(withSky = false): { s: Scene; cloud: Mesh; main: Mesh } {
  const s = new Scene()
  if (withSky) {
    const sky = new Mesh(new PlaneGeometry(2, 2), makeSkyQuadMaterial(makeSkyUniforms()))
    sky.frustumCulled = false
    sky.renderOrder = -1000
    s.add(sky)
  }
  const mc = new MeshBasicNodeMaterial()
  mc.colorNode = vec3(0.3, 0.5, 0.7) as N
  const cloud = new Mesh(new PlaneGeometry(40, 40), mc)
  cloud.position.set(0, 0, -60)
  cloud.layers.set(1) // CH_CLOUD
  const mm = new MeshBasicNodeMaterial()
  mm.colorNode = vec3(0.9, 0.1, 0.1) as N
  const main = new Mesh(new PlaneGeometry(20, 20), mm)
  main.position.set(8, 0, -80) // behind the cloud plane: must be occluded where they overlap
  main.layers.set(0)
  s.add(cloud, main)
  return { s, cloud, main }
}
function cam(): PerspectiveCamera {
  const c = new PerspectiveCamera(60, 1, 0.5, 20000)
  c.updateMatrixWorld()
  c.updateProjectionMatrix()
  return c
}
async function frame(be: RenderBackend, s: Scene, c: PerspectiveCamera, W: number): Promise<Uint8Array> {
  be.attach(s)
  const ctx = { ...frameCtx, camera: c, cssW: W, cssH: W, dbW: W, dbH: W, dpr: 1, tier: be.tier }
  const plan = newPassPlan()
  be.plan(ctx, plan)
  be.renderFrame(ctx, plan)
  const gl = be.renderer.getContext()
  const out = new Uint8Array(W * W * 4)
  gl.readPixels(0, 0, W, W, gl.RGBA, gl.UNSIGNED_BYTE, out)
  expect(be.renderer.info.render.calls).toBe(plan.draws)
  return out
}

describe('Tier B pass plan (M06-AC-021, AC-048)', () => {
  it('P1 + P2 + P3 equals the single pass; s = 0.6 allocates nothing', async () => {
    const W = 96
    const mk = async (tier: 'B' | 'S'): Promise<RenderBackend> => {
      const canvas = document.createElement('canvas')
      const be = await createRenderBackend(canvas, { pref: 'auto', forced: { tier } })
      be.renderer.setPixelRatio(1)
      be.renderer.setSize(W, W, false)
      be.state = 'READY'
      return be
    }
    const offs = [
      registerLayer({ id: 'pointcloud', owner: 'M05', perfKey: 'pointcloud', root: null, channel: 1, drawCount: () => 1, setVisible: () => {}, dispose: () => {} }),
      registerLayer({ id: 'drones', owner: 'M06', perfKey: 'drones', root: null, channel: 0, drawCount: () => 1, setVisible: () => {}, dispose: () => {} }),
      // the SkyQuad draws only on Tier S (it is the composite background on Tier B)
      registerLayer({ id: 'groundSky', owner: 'M06', perfKey: 'groundSky', root: null, channel: 0, drawCount: (c) => (c.tier === 'S' ? 1 : 0), setVisible: () => {}, dispose: () => {} }),
    ]
    const S = await mk('S')
    const ref = await frame(S, scene(true).s, cam(), W)
    const B = await mk('B')
    const got = await frame(B, scene().s, cam(), W)
    let diff = 0
    let at = -1
    for (let i = 0; i < ref.length; i++) {
      const d = Math.abs(ref[i] - got[i])
      if (d > diff) {
        diff = d
        at = i
      }
    }
    expect([diff, (at >> 2) % W, Math.floor((at >> 2) / W)][0]).toBeLessThanOrEqual(1)
    // the occluded part of the main plane stays hidden (cloud colour at the centre)
    const c = ((W / 2) * W + W / 2) * 4
    expect(got[c + 2]).toBeGreaterThan(got[c])
    // M05 EDL composite at strength 0 gives the same image
    const edl = makeEdlCompositeMaterial({ reversedDepth: B.caps.reversedZ, strength: 0, radiusPx: 1.4, clear: [0, 0, 0] })
    B.setEdl(edl as never)
    const got2 = await frame(B, scene().s, cam(), W)
    let d2 = 0
    for (let i = 0; i < ref.length; i++) {
      const x = (i >> 2) % W
      const y = Math.floor((i >> 2) / W)
      if (x < 2 || y < 2 || x > W - 3 || y > W - 3) continue // EDL clamps its neighbourhood at the sub-viewport border
      d2 = Math.max(d2, Math.abs(ref[i] - got2[i]))
    }
    expect(d2).toBeLessThanOrEqual(2)
    // s = 0.6: only the sub-viewport and the uniform change
    const probe = perfProbe()
    const allocs = probe.gpu.rtAllocs
    const vp0 = be0(B)
    B.setCloudScale(0.6)
    await frame(B, scene().s, cam(), W)
    expect(probe.gpu.rtAllocs).toBe(allocs)
    expect(B.cloudScale).toBeCloseTo(0.6, 6)
    expect((edl.uniforms.uvScale as { value: number }).value).toBeCloseTo(0.6, 6)
    expect(be0(B).equals(vp0)).toBe(true)
    const rt = B.cloudTarget()!
    expect([rt.viewport.z, rt.viewport.w]).toEqual([Math.round(W * 0.6), Math.round(W * 0.6)])
    for (const off of offs) off()
    await S.dispose()
    await B.dispose()
  })
})

function be0(be: RenderBackend): Vector4 {
  return be.renderer.getViewport(new Vector4())
}
