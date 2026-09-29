// Browser (Chrome 151, SwiftShader C1) checks of the render backend:
//   M06-AC-001 default Tier S / software on this machine; ?tier=B forced through the test switch
//   M06-AC-004 self test: 16 lit pixels for the 4 px point, linear RT 128 +- 2; without the gl_PointSize strip the
//              point is 1 px -> pointSizeMode 'pixel' and M06-E004
//   M06-AC-006 readPixels is asynchronous and bottom-up
//   M06-AC-002 the 28-item feature matrix + PointPool equal the classic expectations of g01 §3 on Tier S and Tier B
import { describe, expect, it } from 'vitest'
import { Mesh, OrthographicCamera, PlaneGeometry, Scene } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { vec3 } from 'three/tsl'
import { createRenderBackend } from '@/viewport/renderer'
import { runFeatMatrix } from '@/viewport/dev/featMatrix'
import { diffMatrix } from '../../perf/m06/featMatrix.expected'

type N = any

describe('render backend on SwiftShader', () => {
  it('Tier S software by default; forced Tier B', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    expect([be.tier, be.deviceClass, be.kind, be.startRung, be.lowestAllowedRung]).toEqual(['S', 'software', 'webgl2', 0, 0])
    expect(be.renderer.info.autoReset).toBe(false)
    expect(be.info.rendererString).toMatch(/swiftshader/i)
    await be.dispose()
    const b = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'B' } })
    expect([b.tier, b.deviceClass, b.lowestAllowedRung]).toEqual(['B', 'software', 2])
    expect(b.info.forced).toEqual({ tier: 'B' })
    expect(b.composite).not.toBeNull()
    await b.dispose()
  })

  it('self test: point size 4 x 4 and linear render targets; the nofix path degrades to pixel', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    const r = await be.selftest()
    expect(r.litPx).toBe(16)
    expect(Math.abs(r.linearValue - 128)).toBeLessThanOrEqual(2)
    expect(be.pointSizeMode).toBe('glpoint')
    await be.dispose()
    const warn: string[] = []
    const w0 = console.warn
    console.warn = (...a: unknown[]) => warn.push(a.map(String).join(' '))
    try {
      const b2 = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { selftestNoFix: true } })
      const r2 = await b2.selftest()
      expect(r2.litPx).toBeLessThan(4)
      expect(b2.pointSizeMode).toBe('pixel')
      await b2.dispose()
    } finally {
      console.warn = w0
    }
    expect(warn.some((x) => x.includes('M06-E004'))).toBe(true)
  })

  it('readPixels is asynchronous and bottom-up', async () => {
    const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: null })
    const rt = be.createRT('selftest', { width: 16, height: 16 })
    const s = new Scene()
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec3(1, 1, 1) as N
    const o = new Mesh(new PlaneGeometry(16, 8), m)
    o.position.set(8, 12, 0) // top half
    s.add(o)
    const cam = new OrthographicCamera(0, 16, 16, 0, -10, 10)
    cam.position.z = 5
    cam.updateMatrixWorld()
    be.renderer.setRenderTarget(rt as never)
    be.renderer.clear()
    be.renderer.render(s, cam)
    be.renderer.setRenderTarget(null)
    const buf = new Uint8Array(16 * 16 * 4)
    const p = be.readPixels(rt, 0, 0, 16, 16, buf)
    expect(p).toBeInstanceOf(Promise)
    await p
    expect(buf[(14 * 16 + 8) * 4]).toBe(255) // row 14 (upper) lit
    expect(buf[(2 * 16 + 8) * 4]).toBe(0) // row 2 (lower) dark
    await be.dispose()
  })

  it('feature matrix: 28 items + PointPool equal the classic expectations on Tier S and Tier B', async () => {
    for (const tier of ['S', 'B'] as const) {
      const m = await runFeatMatrix({ tier })
      expect(m.tier).toBe(tier)
      expect(Object.keys(m.tests).length).toBe(29)
      expect(diffMatrix(m.tests)).toEqual([])
    }
  }, 240_000)
})
