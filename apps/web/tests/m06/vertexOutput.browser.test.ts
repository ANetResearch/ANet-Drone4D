// Per-vertex output transform (ADR-064, FX2-R2; AnetNodesHandler fix 4):
//   - a material that applies outputTransform() in its vertex stage and sets userData.awrOutputInVertex draws the same
//     pixels as the default path (output transform added per fragment by the nodes handler), on the canvas (sRGB) and
//     into a render target (linear), without a second encoding;
//   - the Tier S SkyQuad (64 x 36 grid, sky colour per vertex, far plane, depth-tested, after the opaque objects) matches
//     the per-pixel sky Fn at 640 x 360 (max 8/255, > 97 % of the pixels within 1/255) and leaves pixels covered by an
//     opaque object untouched.
import { describe, expect, it } from 'vitest'
import { Mesh, OrthographicCamera, PerspectiveCamera, PlaneGeometry, Scene, type WebGLRenderTarget } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, positionGeometry, uniform, varying, vec3, vec4 } from 'three/tsl'
import { outputTransform } from '@/engine'
import { createRenderBackend, type RenderBackend } from '@/viewport/renderer'
import { SKY, makeSkyQuadGeometry, makeSkyQuadMaterial, makeSkyUniforms, setSkyColors, skyColor, updateSkyUniforms } from '@/viewport/layers/groundSky.materials'

type N = any

async function backend(W: number, H: number): Promise<RenderBackend> {
  const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
  be.renderer.setPixelRatio(1)
  be.renderer.setSize(W, H, false)
  be.state = 'READY'
  return be
}

/** render to the canvas and read it back in the same task (preserveDrawingBuffer is off) */
function canvasPixels(be: RenderBackend, s: Scene, cam: OrthographicCamera | PerspectiveCamera, W: number, H: number): Uint8Array {
  const r = be.renderer
  r.setRenderTarget(null)
  r.setClearColor(0x000000, 1)
  r.render(s, cam)
  const gl = r.getContext()
  const out = new Uint8Array(W * H * 4)
  gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, out)
  return out
}

async function targetPixels(be: RenderBackend, s: Scene, cam: OrthographicCamera, W: number, H: number): Promise<Uint8Array> {
  const rt = be.createRT('selftest', { width: W, height: H }) as WebGLRenderTarget
  const r = be.renderer
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.render(s, cam)
  r.setRenderTarget(null)
  const out = new Uint8Array(W * H * 4)
  await be.readPixels(rt, 0, 0, W, H, out)
  rt.dispose()
  return out
}

const maxDiff = (a: Uint8Array, b: Uint8Array): number => {
  let d = 0
  for (let i = 0; i < a.length; i++) if ((i & 3) !== 3) d = Math.max(d, Math.abs(a[i] - b[i]))
  return d
}

describe('per-vertex output transform (ADR-064)', () => {
  it('vertex-stage outputTransform equals the per-fragment output, on the canvas and into a render target', async () => {
    const W = 32
    const be = await backend(W, W)
    const cam = new OrthographicCamera(-1, 1, 1, -1, 0, 1)
    // 8 vertical bands of constant linear colours (a point sprite is constant too), dark ones included where the sRGB
    // transfer curves most; band i covers clip x in [-1 + i / 4, -1 + (i + 1) / 4]
    const LEVELS = [0.001, 0.003, 0.01, 0.04, 0.1, 0.25, 0.5, 0.9]
    const mk = (perVertex: boolean): Scene => {
      const s = new Scene()
      LEVELS.forEach((v, i) => {
        const col: N = vec3(uniform(v), uniform(v * 0.5), uniform(0.02))
        const m = new MeshBasicNodeMaterial()
        m.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
        if (perVertex) {
          m.colorNode = vec4(varying(outputTransform(col), 'vTestOut') as N, 1) as N
          m.userData.awrOutputInVertex = true
        } else m.colorNode = vec4(col, 1) as N
        m.depthTest = false
        m.fog = false
        const g = new PlaneGeometry(0.25, 2)
        g.translate(-1 + 0.125 + i * 0.25, 0, 0)
        const q = new Mesh(g, m)
        q.frustumCulled = false
        s.add(q)
      })
      return s
    }
    const ref = canvasPixels(be, mk(false), cam, W, W)
    const got = canvasPixels(be, mk(true), cam, W, W)
    expect(maxDiff(ref, got)).toBeLessThanOrEqual(2)
    // the canvas output is sRGB-encoded: band 5 (linear 0.25 red) reads about 137, not 64
    const mid = ((W >> 1) * W + 5 * (W / 8) + 2) * 4
    expect(got[mid]).toBeGreaterThan(110)
    const rtRef = await targetPixels(be, mk(false), cam, W, W)
    const rtGot = await targetPixels(be, mk(true), cam, W, W)
    expect(maxDiff(rtRef, rtGot)).toBeLessThanOrEqual(2)
    expect(rtGot[mid]).toBeLessThan(90) // linear into the target: no second encoding
    await be.dispose()
  })

  it('Tier S SkyQuad grid matches the per-pixel sky and leaves covered pixels alone', async () => {
    // the Tier S drawing buffer (1280 x 720 CSS at 0.5): grid cells of 10 x 10 raster px
    const W = 640
    const H = 360
    const be = await backend(W, H)
    const cam = new PerspectiveCamera(60, W / H, 0.5, 20000)
    cam.position.set(0, 100, 0)
    cam.lookAt(0, 140, -400) // looking slightly up: the horizon gradient fills the frame
    cam.updateMatrixWorld()
    cam.updateProjectionMatrix()
    const su = makeSkyUniforms()
    setSkyColors(su, [0.55, 0.6, 0.7], [0.05, 0.1, 0.3])
    updateSkyUniforms(su, cam)
    // reference: the per-pixel sky Fn on a full-screen quad (the Tier B/A composite background)
    const pm = new MeshBasicNodeMaterial()
    pm.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
    pm.colorNode = Fn(() => vec4(skyColor(su, (positionGeometry as N).xy), 1))() as N
    pm.depthTest = false
    pm.fog = false
    const refScene = new Scene()
    const refQuad = new Mesh(new PlaneGeometry(2, 2), pm)
    refQuad.frustumCulled = false
    refScene.add(refQuad)
    const ref = canvasPixels(be, refScene, cam, W, H)
    // Tier S: the grid sky after an opaque square that covers the centre
    const s = new Scene()
    const sky = new Mesh(makeSkyQuadGeometry(), makeSkyQuadMaterial(su, be.caps.reversedZ))
    sky.frustumCulled = false
    sky.renderOrder = SKY.renderOrder
    const om = new MeshBasicNodeMaterial()
    om.colorNode = vec3(1, 0, 0) as N
    const box = new Mesh(new PlaneGeometry(40, 40), om)
    box.position.set(0, 140, -300)
    box.lookAt(cam.position)
    s.add(sky, box)
    const got = canvasPixels(be, s, cam, W, H)
    const c = ((H >> 1) * W + (W >> 1)) * 4
    expect([got[c], got[c + 1], got[c + 2]]).toEqual([255, 0, 0]) // the sky did not overwrite the opaque object
    let d = 0
    const hist = new Uint32Array(256)
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4
        if (got[i] === 255 && got[i + 1] === 0) continue // the red square
        let e = 0
        for (let k = 0; k < 3; k++) e = Math.max(e, Math.abs(got[i + k] - ref[i + k]))
        hist[e]++
        d = Math.max(d, e)
      }
    }
    // linear interpolation over 10 px cells: exact or 1/255 almost everywhere, a few levels where the gradient curves
    // most (the identity sky's pow(elevation, 0.45) at the horizon); measured max 6, 98.4 % within 1
    let n = 0
    let within1 = 0
    for (let e = 0; e < 256; e++) {
      n += hist[e]
      if (e <= 1) within1 += hist[e]
    }
    expect(d, `hist ${Array.from(hist.slice(0, 10)).join(',')}`).toBeLessThanOrEqual(8)
    expect(within1 / n).toBeGreaterThan(0.97)
    await be.dispose()
  })
})
