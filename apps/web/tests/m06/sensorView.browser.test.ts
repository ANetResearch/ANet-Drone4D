// D2 WP-11 sensor view composite on the real classic path (WebGL2 over SwiftShader, Tier S backend): a plane rendered into
// the sensor target (XR-flagged sRGB colour + float depth) is drawn to the canvas by the sensor view material.
// EO by day returns the canvas colour (within the vignette and the noise floor); LWIR puts an emitter (projected box on
// the optical axis, in front of the plane) at the top of the grey ramp and the background mid-range, black-hot inverts
// it, an emitter behind the plane is occluded, the sky (cleared depth above the horizon) is cold; the night view without
// light is dark and an illuminator lights the plane inside its cone only; no program is compiled after the first frame of
// each view; the canvas has no red pixel in LWIR although the plane is red.
import { describe, expect, it } from 'vitest'
import { Mesh, PerspectiveCamera, PlaneGeometry, Scene } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { vec3 } from 'three/tsl'
import { createRenderBackend } from '@/viewport/renderer'
import { EMIT_COLS, MODE_INDEX } from '@/viewport/sensorView/materials'

type N = any
const W = 96

async function setup() {
  const canvas = document.createElement('canvas')
  const be = await createRenderBackend(canvas, { pref: 'auto', forced: { tier: 'S' } })
  be.renderer.setPixelRatio(1)
  be.renderer.setSize(W, W, false)
  const cam = new PerspectiveCamera(60, 1, 0.5, 20000)
  cam.position.set(0, 0, 0)
  cam.lookAt(0, -0.0001, -1)
  cam.updateMatrixWorld()
  cam.updateProjectionMatrix()
  const s = new Scene()
  const m = new MeshBasicNodeMaterial()
  m.colorNode = vec3(0.6, 0.05, 0.05) as N // red in the canvas colour space path
  // a wall 50 m ahead covering the lower half of the view (the upper half stays at the cleared depth: sky)
  const wall = new Mesh(new PlaneGeometry(400, 100), m)
  wall.position.set(0, -50, -50)
  s.add(wall)
  return { be, cam, s }
}

function frame(be: Awaited<ReturnType<typeof setup>>['be'], s: Scene, cam: PerspectiveCamera, set: (u: Record<string, { value: any }>) => void): Uint8Array {
  const r = be.renderer
  const p = be.sensor
  const rt = p.ensure(W, W)
  r.setRenderTarget(rt)
  r.render(s, cam)
  r.setRenderTarget(null)
  const u = p.view.u as unknown as Record<string, { value: any }>
  u.projInv.value.copy(cam.projectionMatrixInverse)
  u.camWorld.value.copy(cam.matrixWorld)
  u.camPos.value.setFromMatrixPosition(cam.matrixWorld)
  u.res.value.set(W, W)
  u.groundY.value = -1000
  set(u)
  r.render(p.scene, cam)
  const gl = r.getContext()
  const out = new Uint8Array(W * W * 4)
  gl.readPixels(0, 0, W, W, gl.RGBA, gl.UNSIGNED_BYTE, out)
  return out
}
const at = (px: Uint8Array, x: number, y: number): number[] => {
  const q = 4 * (y * W + x)
  return [px[q], px[q + 1], px[q + 2]]
}
const lum = (c: number[]): number => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

describe('sensor view composite (D2 WP-11)', () => {
  it('EO passes the frame, LWIR shows emitters and the sky cold without red, NIR lights its cone only', async () => {
    const { be, cam, s } = await setup()
    // EO by day: the red wall stays red, the sky stays the clear colour
    const eo = frame(be, s, cam, (u) => {
      u.mode.value = MODE_INDEX.eo
      u.gain.value = 1
      u.sat.value = 1
      u.noise.value = 0
    })
    const wallEo = at(eo, W / 2, W / 4)
    expect(wallEo[0]).toBeGreaterThan(wallEo[1] + 60)
    const programs = be.programsCount()
    // LWIR: emitter (person on the optical axis at 20 m, in front of the wall) and one behind the wall (occluded)
    const data = be.sensor.emitTex.image.data as Float32Array
    data.fill(0)
    const row = (j: number, cx: number, cy: number, hx: number, hy: number, near: number, core: number, shell: number): void => {
      const q = j * EMIT_COLS * 4
      data.set([cx, cy, hx, hy, near, core, shell, 1, near + 1, 0, 0, 0], q)
    }
    row(0, 0.5, -0.5, 0.1, 0.1, 80, 40, 40) // behind the wall (near range 80 m > 50 m): occluded
    row(1, 0, -0.4, 0.12, 0.25, 19, 40, 40)
    be.sensor.emitTex.needsUpdate = true
    const lw = (black: number): Uint8Array => frame(be, s, cam, (u) => {
      u.mode.value = MODE_INDEX.lwir
      u.tAir.value = 15
      u.tSurf.value = 15
      u.tSky.value = -30
      u.epsBg.value = 1
      u.sigma.value = 0
      u.day.value = 0
      u.albK.value = 0
      u.sunK.value = 0
      u.structK.value = 0
      u.lo.value = 0
      u.hi.value = 30
      u.blackHot.value = black
      u.count.value = 2
    })
    const white = lw(0)
    // pixel rows: y = 0 is the bottom; ndc y -0.4 -> pixel (1 - 0.4) / 2 * W
    const ex = W / 2
    const ey = Math.round(((1 - 0.4) / 2) * W)
    const hot = lum(at(white, ex, ey))
    const bg = lum(at(white, 8, W / 4))
    const sky = lum(at(white, 8, W - 8))
    const occl = lum(at(white, Math.round(((1 + 0.5) / 2) * W), Math.round(((1 - 0.5) / 2) * W)))
    expect(hot).toBeGreaterThan(200)
    expect(bg).toBeGreaterThan(40)
    expect(bg).toBeLessThan(200)
    expect(sky).toBeLessThan(bg - 20)
    expect(Math.abs(occl - bg)).toBeLessThan(8)
    let red = 0
    for (let q = 0; q < white.length; q += 4) if (white[q] >= 96 && white[q] - Math.max(white[q + 1], white[q + 2]) >= 48) red++
    expect(red).toBe(0)
    const black = lw(1)
    expect(lum(at(black, ex, ey))).toBeLessThan(lum(at(black, 8, W / 4)) - 40)
    // NIR at night: dark without light; an illuminator of 20 deg divergence lights the wall around the axis only
    const nir = (on: number): Uint8Array => frame(be, s, cam, (u) => {
      u.mode.value = MODE_INDEX.nir
      u.kPas.value = 0
      const h = (10 * Math.PI) / 180
      u.ill0.value.set(on, Math.cos(h * 0.85), Math.cos(h * 1.15))
      u.r0.value = 150
      u.ill1.value.set(0, 1, 1)
    })
    const off = nir(0)
    const lit = nir(1)
    const axis = [W / 2, Math.round(W / 2 - 3)]
    expect(lum(at(lit, axis[0], axis[1]))).toBeGreaterThan(lum(at(off, axis[0], axis[1])) + 30)
    expect(Math.abs(lum(at(lit, 4, W / 4)) - lum(at(off, 4, W / 4)))).toBeLessThan(12)
    expect(be.programsCount()).toBe(programs)
    await be.dispose()
  })
})
