// M18-AC-020 (browser part): the target overlay (32 boxes, 64 sensitivity bodies: faces + outlines, 3 draws) renders under
// the WebGL2 minimum limits (ADR-086/087: MAX_UNIFORM_BUFFER_BINDINGS 24, 12 / 12 / 24 blocks, MAX_UNIFORM_BLOCK_SIZE
// 16 384, emulated like tests/m06/ubo.browser.test.ts and perf/m06/gpu-limits.spec.ts) with the viewport's AnetNodesHandler:
// no binding point past the limit, no GL error, no console error, and the faces and lines change the pixels.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { OrthographicCamera, Scene, WebGLRenderer, WebGLRenderTarget } from 'three'
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'
import { TargetsModel, TargetsOverlay } from '@/engine/targets'
import { AnetNodesHandler } from '@/viewport/anetNodesHandler'
import { WEBGL2_MIN_LIMITS, type UboLimits } from '@/viewport/backend/uboBinder'

const UB = 0x8a11
const W = 96
const H = 96

function emulate(Lm: UboLimits, blockSize: number): { errors: string[]; restore(): void } {
  const P = WebGL2RenderingContext.prototype
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  const gp = own('getParameter')
  const bbb = own('bindBufferBase')
  const ubb = own('uniformBlockBinding')
  const errors: string[] = []
  P.getParameter = function (this: WebGL2RenderingContext, n: number) {
    if (n === 0x8a2f) return Lm.bindings
    if (n === 0x8a2b) return Lm.vertex
    if (n === 0x8a2d) return Lm.fragment
    if (n === 0x8a2e) return Lm.combined
    if (n === 0x8a30) return blockSize
    return gp.call(this, n)
  } as typeof P.getParameter
  P.bindBufferBase = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null) {
    if (t === UB && i >= Lm.bindings) errors.push(`bindBufferBase ${i}`)
    else bbb.call(this, t, i, b)
  }
  P.uniformBlockBinding = function (this: WebGL2RenderingContext, p: WebGLProgram, bi: number, point: number) {
    if (point >= Lm.bindings) errors.push(`uniformBlockBinding ${point}`)
    else ubb.call(this, p, bi, point)
  }
  return {
    errors,
    restore() {
      P.getParameter = gp
      P.bindBufferBase = bbb
      P.uniformBlockBinding = ubb
    },
  }
}

function model(): TargetsModel {
  const m = new TargetsModel()
  m.resolver = { targetIdOf: (n) => `t-${n}` }
  const b = new Uint8Array(32 * 32)
  const dv = new DataView(b.buffer)
  for (let k = 0; k < 32; k++) {
    const o = k * 32
    dv.setUint16(o + L.AGENT_NO, k, true)
    dv.setUint8(o + L.CLASS, 5)
    dv.setUint8(o + L.STATE, 0x80 | (k % 3 === 0 ? 0x20 : 0))
    dv.setFloat32(o + L.POS, (k % 8) * 12 - 42, true)
    dv.setFloat32(o + L.POS + 4, Math.floor(k / 8) * 12 - 18, true)
    dv.setFloat32(o + L.HEADING_RAD, k * 0.3, true)
  }
  m.ingestRows(b, 0)
  for (let k = 0; k < 32; k++) {
    m.ingestDetail(`t-${k}`, { id: `t-${k}`, class: 'vehicle', awareness: 'UNAWARE', alertness: 0.3, criterion_mode: 'geometric', discovered_count: 0,
      cfg_rev: 1, sensitivity: [
        { idx: 0, kind: 'visual', shape: 'sector', radius_cfg_m: 5, enabled: true, z_min_m: 0, z_max_m: 3, half_angle_deg: 45 },
        { idx: 1, kind: 'acoustic', shape: 'hemisphere', radius_cfg_m: 4, enabled: true },
      ] })
  }
  return m
}

describe('target overlay under WebGL2 minimum limits (M18-AC-020)', () => {
  let emu: ReturnType<typeof emulate>
  beforeEach(() => {
    emu = emulate(WEBGL2_MIN_LIMITS, 16384)
  })
  afterEach(() => emu.restore())

  it('renders 32 targets and 64 bodies within the limits', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const r = new WebGLRenderer({ canvas: document.createElement('canvas'), antialias: false })
    r.setNodesHandler(new AnetNodesHandler())
    r.setPixelRatio(1)
    r.setSize(W, H, false)
    const gl = r.getContext() as WebGL2RenderingContext
    const scene = new Scene()
    const o = new TargetsOverlay()
    scene.add(o.root)
    const cam = new OrthographicCamera(-50, 50, 50, -50, -100, 100)
    cam.position.set(0, 0, 50)
    cam.updateMatrixWorld()
    const rt = new WebGLRenderTarget(W, H)
    const draw = async (): Promise<Uint8Array> => {
      r.setRenderTarget(rt)
      r.setClearColor(0x000000, 1)
      r.clear()
      r.render(scene, cam)
      r.setRenderTarget(null)
      const px = new Uint8Array(W * H * 4)
      await r.readRenderTargetPixelsAsync(rt, 0, 0, W, H, px)
      return px
    }
    const empty = await draw()
    const m = model()
    expect(o.update(m, { view: 'configured', modelId: null, selectedId: 't-1', redId: 't-2', showBodies: true })).toBe(true)
    expect(o.nBodies).toBe(64)
    expect(o.drawCount()).toBe(3)
    const full = await draw()
    let changed = 0
    for (let i = 0; i < full.length; i += 4) if (full[i] !== empty[i] || full[i + 1] !== empty[i + 1] || full[i + 2] !== empty[i + 2]) changed++
    expect(changed).toBeGreaterThan(200)
    o.level = 1
    o.update(m, { view: 'planning', modelId: null, selectedId: null, redId: null, showBodies: true })
    await draw()
    expect(gl.getError()).toBe(gl.NO_ERROR)
    expect(emu.errors).toEqual([])
    const msgs = [...err.mock.calls, ...warn.mock.calls].map((c) => String(c[0])).filter((s) => !/AttributeNode|THREE\.Clock/.test(s))
    err.mockRestore()
    warn.mockRestore()
    expect(msgs).toEqual([])
    rt.dispose()
    o.dispose()
    r.dispose()
  })
})
