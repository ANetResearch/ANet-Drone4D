// Pixel checks on SwiftShader (render-target read-back, linear colour; CI pixel regressions never use screenshots,
// ADR-046 consequences):
//   M06-AC-026 marker: 6 +- 1 CSS px fill with a 1 px halo ring (dpr 1), stale and red styles switch colour by style code
//   M06-AC-027 GlyphLayer: 8 SDF shapes in exactly 1 draw call, all with lit pixels, filled disc > rings
//   M06-AC-022 ground grid: minor lines every 10 m and major lines every 100 m (1 px = 1 m top view, error <= 1 px);
//              sky: zenith colour straight up and horizon colour at the horizon (uniform colours, no recompile)
import { describe, expect, it } from 'vitest'
import { Mesh, OrthographicCamera, PerspectiveCamera, PlaneGeometry, Scene, Vector3, type WebGLRenderTarget } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { positionGeometry, vec3, vec4 } from 'three/tsl'
import { GlyphLayer, GLYPH, MarkerBatch, MarkerStyle, Palette, Shape } from '@/engine'
import { SCENE } from '@/lib/tokens/scene.gen'
import { createRenderBackend, type RenderBackend } from '@/viewport/renderer'
import { makeGridMaterial, makeGridUniforms, makeSkyQuadGeometry, makeSkyQuadMaterial, makeSkyUniforms, setSkyColors, updateSkyUniforms } from '@/viewport/layers/groundSky.materials'

const to8 = (v: number): number => Math.round(Math.min(1, Math.max(0, v)) * 255)

async function draw(be: RenderBackend, s: Scene, cam: PerspectiveCamera | OrthographicCamera, W: number, H: number): Promise<{ buf: Uint8Array; calls: number }> {
  const rt = be.createRT('selftest', { width: W, height: H }) as WebGLRenderTarget
  const r = be.renderer
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.info.reset()
  r.render(s, cam)
  const calls = r.info.render.calls
  r.setRenderTarget(null)
  const buf = new Uint8Array(W * H * 4)
  await be.readPixels(rt, 0, 0, W, H, buf)
  rt.dispose()
  return { buf, calls }
}
const px = (b: Uint8Array, W: number, x: number, y: number): number[] => Array.from(b.slice((y * W + x) * 4, (y * W + x) * 4 + 3))
const lit = (p: number[]): boolean => p[0] + p[1] + p[2] > 0

describe('scene visuals (render-target read-back)', () => {
  it('marker: 6 px fill + 1 px halo; style colours (M06-AC-026)', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    const W = 64
    const m = new MarkerBatch(4)
    const s = new Scene()
    s.add(m.mesh)
    const cam = new PerspectiveCamera(60, 1, 0.5, 1000)
    cam.position.set(0, 0, 50)
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    m.set(0, 0, 0, 0, MarkerStyle.Normal)
    m.commit(1, 1, W, W)
    const { buf, calls } = await draw(be, s, cam, W, W)
    expect(calls).toBe(1)
    // horizontal run through the centre row: fill pixels (marker colour), 6 +- 1 px
    const fill = SCENE.droneMarker.map(to8)
    const row = W / 2
    let run = 0
    for (let x = 0; x < W; x++) {
      const p = px(buf, W, x, row)
      if (Math.abs(p[0] - fill[0]) <= 2 && Math.abs(p[1] - fill[1]) <= 2) run++
    }
    expect(run).toBeGreaterThanOrEqual(5)
    expect(run).toBeLessThanOrEqual(7)
    // the halo colour is g950 (near black): over a mid-grey background the 1 px ring shows on both sides of the fill
    const bgM = new MeshBasicNodeMaterial()
    bgM.colorNode = vec3(0.5, 0.5, 0.5) as never
    bgM.vertexNode = vec4((positionGeometry as never as { xy: unknown }).xy as never, 0, 1) as never
    bgM.depthTest = false
    const bg = new Mesh(new PlaneGeometry(2, 2), bgM)
    bg.frustumCulled = false
    bg.renderOrder = -1000
    s.add(bg)
    const withBg = await draw(be, s, cam, W, W)
    const cls = (p: number[]): 'fill' | 'halo' | 'bg' => (p[0] > 200 ? 'fill' : p[0] < 40 ? 'halo' : 'bg')
    const line = Array.from({ length: W }, (_, x) => cls(px(withBg.buf, W, x, row)))
    const first = line.indexOf('fill')
    const last = line.lastIndexOf('fill')
    const haloL = line.slice(0, first).reverse().findIndex((c) => c !== 'halo')
    const haloR = line.slice(last + 1).findIndex((c) => c !== 'halo')
    expect(haloL).toBeGreaterThanOrEqual(1)
    expect(haloL).toBeLessThanOrEqual(2)
    expect(haloR).toBeGreaterThanOrEqual(1)
    expect(haloR).toBeLessThanOrEqual(2)
    bg.removeFromParent()
    // stale and red styles
    m.set(0, 0, 0, 0, MarkerStyle.Stale)
    m.commit(1, 1, W, W)
    const st = await draw(be, s, cam, W, W)
    const staleC = px(st.buf, W, W / 2, W / 2)
    for (let i = 0; i < 3; i++) expect(Math.abs(staleC[i] - to8(SCENE.droneStale[i]))).toBeLessThanOrEqual(1)
    m.set(0, 0, 0, 0, MarkerStyle.Red)
    m.commit(1, 1, W, W)
    const red = px((await draw(be, s, cam, W, W)).buf, W, W / 2, W / 2)
    for (let i = 0; i < 3; i++) expect(Math.abs(red[i] - to8(SCENE.glyphPalette[Palette.R500][i]))).toBeLessThanOrEqual(1)
    // hidden: nothing drawn
    m.set(0, 0, 0, 0, MarkerStyle.Hidden)
    m.commit(1, 1, W, W)
    expect(lit(px((await draw(be, s, cam, W, W)).buf, W, W / 2, W / 2))).toBe(false)
    m.dispose()
    await be.dispose()
  })

  it('glyphs: 8 shapes, one draw (M06-AC-027)', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    const W = 256
    const H = 64
    const g = new GlyphLayer(GLYPH.capS)
    const s = new Scene()
    s.add(g.mesh)
    const cam = new OrthographicCamera(0, W, H, 0, -10, 10)
    cam.updateMatrixWorld()
    g.begin()
    const shapes = [Shape.Ring, Shape.Disc, Shape.Octagon, Shape.Triangle, Shape.Dashed, Shape.Diamond, Shape.Crosshair, Shape.Goto]
    shapes.forEach((sh, i) => g.push(0, 16 + 32 * i, 32, 0, 24, sh, 2, Palette.G50))
    expect(g.commit(1, W, H)).toBe(8)
    expect(g.drawCount()).toBe(1)
    const { buf, calls } = await draw(be, s, cam, W, H)
    expect(calls).toBe(1)
    const litIn = (cx: number): number => {
      let n = 0
      for (let y = 16; y < 48; y++) for (let x = cx - 16; x < cx + 16; x++) if (lit(px(buf, W, x, y))) n++
      return n
    }
    const counts = shapes.map((_, i) => litIn(16 + 32 * i))
    for (const c of counts) expect(c).toBeGreaterThan(20)
    expect(counts[1]).toBeGreaterThan(counts[0] * 2) // disc filled, ring outline
    expect(counts[4]).toBeLessThan(counts[0]) // dashed ring has gaps
    expect(new Set(counts).size).toBeGreaterThanOrEqual(6)
    g.dispose()
    await be.dispose()
  })

  it('ground grid spacing and sky colours (M06-AC-022)', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    const W = 256
    const u = makeGridUniforms()
    ;(u.minor.value as Vector3).set(0, 1, 0)
    ;(u.major.value as Vector3).set(1, 0, 0)
    u.minorA.value = 1
    u.majorA.value = 1
    const grid = new Mesh(new PlaneGeometry(1000, 1000), makeGridMaterial(u))
    const s = new Scene()
    s.add(grid)
    // top view, 1 px = 1 m, x from -128 to 128
    const cam = new OrthographicCamera(-W / 2, W / 2, W / 2, -W / 2, 0.1, 1000)
    cam.position.set(0.25, 0.25, 100)
    cam.updateMatrixWorld()
    const { buf } = await draw(be, s, cam, W, W)
    const row = W / 2 + 5
    const centres = (pred: (p: number[]) => boolean): number[] => {
      const out: number[] = []
      let a = -1
      for (let x = 0; x <= W; x++) {
        const on = x < W && pred(px(buf, W, x, row))
        if (on && a < 0) a = x
        if (!on && a >= 0) {
          out.push((a + x - 1) / 2)
          a = -1
        }
      }
      return out
    }
    const minor = centres((p) => p[1] > 64 && p[0] < 64)
    const major = centres((p) => p[0] > 64)
    expect(minor.length).toBeGreaterThanOrEqual(20)
    for (let i = 1; i < minor.length; i++) {
      const d = minor[i] - minor[i - 1]
      if (d > 15) continue // a major line sits between
      expect(Math.abs(d - 10)).toBeLessThanOrEqual(1)
    }
    expect(major.length).toBeGreaterThanOrEqual(2)
    for (let i = 1; i < major.length; i++) expect(Math.abs(major[i] - major[i - 1] - 100)).toBeLessThanOrEqual(1)
    // sky: straight up = zenith, horizontal = horizon (test colours through the uniforms)
    const su = makeSkyUniforms()
    setSkyColors(su, [0.8, 0.4, 0.2], [0.1, 0.3, 0.6])
    const sky = new Mesh(makeSkyQuadGeometry(), makeSkyQuadMaterial(su, be.caps.reversedZ))
    sky.frustumCulled = false
    const ss = new Scene()
    ss.add(sky)
    const pc = new PerspectiveCamera(60, 1, 0.5, 1000)
    pc.lookAt(0, 1, 0) // three frame: +y is up
    pc.updateMatrixWorld()
    updateSkyUniforms(su, pc)
    const up = px((await draw(be, ss, pc, 32, 32)).buf, 32, 16, 16)
    expect(up.map((v, i) => Math.abs(v - to8([0.1, 0.3, 0.6][i])) <= 2)).toEqual([true, true, true])
    pc.lookAt(0, 0, -1)
    pc.updateMatrixWorld()
    updateSkyUniforms(su, pc)
    // row 15 of 32 (bottom-up) is just below the horizon: the sky Fn clamps elevation < 0 to the horizon colour
    const hz = px((await draw(be, ss, pc, 32, 32)).buf, 32, 16, 15)
    expect(hz.map((v, i) => Math.abs(v - to8([0.8, 0.4, 0.2][i])) <= 3)).toEqual([true, true, true])
    // default uniforms carry the scene tokens
    const d = makeSkyUniforms()
    expect((d.zenith.value as Vector3).toArray()).toEqual([...SCENE.skyZenith])
    expect((d.horizon.value as Vector3).toArray()).toEqual([...SCENE.skyHorizon])
    // a colour change is a uniform write: no program compiled
    const programs0 = be.programsCount()
    setSkyColors(su, [0.2, 0.2, 0.2], [0.3, 0.3, 0.3])
    const again = px((await draw(be, ss, pc, 32, 32)).buf, 32, 16, 15)
    expect(Math.abs(again[0] - to8(0.2))).toBeLessThanOrEqual(3)
    expect(be.programsCount()).toBe(programs0)
    await be.dispose()
  })
})
