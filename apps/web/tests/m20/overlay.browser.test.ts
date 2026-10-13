// M20 PRD §8 item 2 (browser part): the tasking overlay (nests, sub-areas, strips, legs, fills, stations, the preview
// keepout band and the 2 px selected-task line; two draws) renders under the WebGL2 minimum limits (ADR-086/087:
// MAX_UNIFORM_BUFFER_BINDINGS 24, 12 / 12 / 24 blocks, MAX_UNIFORM_BLOCK_SIZE 16 384, emulated like
// tests/m18/overlay.browser.test.ts and perf/m06/gpu-limits.spec.ts) with the viewport's AnetNodesHandler: no binding
// point past the limit, no GL error, no console error, and the lines change the pixels.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PerspectiveCamera, Scene, WebGLRenderer, WebGLRenderTarget } from 'three'
import type { TaskingState } from '@/stores/tasking'
import { TaskingOverlay } from '@/viewport/layers/tasking'
import { AnetNodesHandler } from '@/viewport/anetNodesHandler'
import { WEBGL2_MIN_LIMITS, type UboLimits } from '@/viewport/backend/uboBinder'
import type { FrameCtx } from '@/engine'

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

function state(): TaskingState {
  const leg = (uav: string, y: number) => ({ uav, kind: 'strip' as const, polyline: [[0, y, 60], [300, y, 60], [300, y + 25, 60], [0, y + 25, 60]] as [number, number, number][] })
  return {
    mode: 'sandbox', canEdit: true, stale: false, lastError: null, version: 3, selected: 't-1', nestStatus: {}, relay: {}, relayHistory: {},
    nests: [{ id: 'n-1', pos: [20, 20, 0], kind: 'box', slots: 4 }],
    tasks: [{ tid: 't-1', type: 'area_patrol', label: '', state: 'RUNNING', revision: 1, parent: null, children: [], progressPct: 0, coveragePct: null,
      nVehicles: 2, makespanS: null, items: [], warnings: [], params: {}, relay: null }],
    plans: { 't-1': { revision: 1, assignments: [{ uav: 'p600-01', items: [] }, { uav: 'p600-02', items: [] }], geometry: {
      subregions: [[[0, 0], [300, 0], [300, 300], [0, 300]]],
      strips: [{ id: 's0', p0: [0, 150, 60], p1: [300, 150, 60], uav: 'p600-02' }],
      fill_waypoints: [{ pos: [150, 200, 60], dwell_s: 2 }],
      legs: [leg('p600-01', 40), leg('p600-02', 220)],
      stations: [{ id: 't-1.look0', pos: [150, 100, 60], role: 'primary', uav: 'p600-01' }, { id: 't-1.ho', pos: [170, 100, 60], role: 'handover' }] } } },
    preview: null,
    previewKeepouts: [],
  }
}

describe('tasking overlay under WebGL2 minimum limits (M20 PRD §8 item 2)', () => {
  let emu: ReturnType<typeof emulate>
  beforeEach(() => {
    emu = emulate(WEBGL2_MIN_LIMITS, 16384)
  })
  afterEach(() => emu.restore())

  it('renders the plan, the stations and the selected line within the limits', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const r = new WebGLRenderer({ canvas: document.createElement('canvas'), antialias: false })
    r.setNodesHandler(new AnetNodesHandler())
    r.setPixelRatio(1)
    r.setSize(W, H, false)
    const gl = r.getContext() as WebGL2RenderingContext
    const scene = new Scene()
    const o = new TaskingOverlay()
    scene.add(o.root)
    const cam = new PerspectiveCamera(60, 1, 1, 5000)
    cam.position.set(150, 150, 520)
    cam.lookAt(150, 150, 0)
    cam.updateMatrixWorld()
    const rt = new WebGLRenderTarget(W, H)
    const ctx = { dpr: 1, dbW: W, dbH: H, camera: cam } as unknown as FrameCtx
    const draw = async (): Promise<Uint8Array> => {
      o.frame(ctx)
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
    expect(o.update({ state: state(), ground: () => 0 })).toBe(true)
    expect(o.drawCount()).toBe(2)
    const full = await draw()
    let changed = 0
    for (let i = 0; i < full.length; i += 4) if (full[i] !== empty[i] || full[i + 1] !== empty[i + 1] || full[i + 2] !== empty[i + 2]) changed++
    expect(changed).toBeGreaterThan(100)
    o.level = 3
    o.update({ state: state(), ground: () => 0 })
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
