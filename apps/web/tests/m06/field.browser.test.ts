// D2.1 WP-11b (AWR-04 §15.11.6; ADR-086, ADR-087, ADR-125; D2-AC-53, D2-AC-54 browser part): the field view's two programs
// (points in 'glpoint' and 'quad' mode, shells) on the classic path with AnetNodesHandler under the WebGL2 minimum limits
// (24 binding points, 12 / 12 / 24 blocks, 16 384 B blocks; emulated like tests/m06/ubo.browser.test.ts): no GL error, no
// binding past the limit, per program <= 3 vertex and <= 2 fragment blocks, <= 8 attributes and <= 4 vec4 varyings, one draw
// each; the radiation field and the volumes light pixels in grey only (no channel leans red); a hidden anchor collapses its
// points; switching the view kind rewrites data only (no new program).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Group, PerspectiveCamera, Scene, WebGLRenderer, WebGLRenderTarget } from 'three'
import { FieldRenderer, FIELD_BUDGETS, type FieldSource, type TargetLookup } from '@/engine/field'
import { decodeFieldProfile, decodeFieldVolumes, fieldModel } from '@/engine/perception'
import type { FrameCtx } from '@/engine/loop'
import type { SensitivityBody } from '@/engine/targets'
import { AnetNodesHandler } from '@/viewport/anetNodesHandler'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'
import { WEBGL2_MIN_LIMITS, type UboLimits } from '@/viewport/backend/uboBinder'
import fix from '../m19/field_cases.json'

const UB = 0x8a11
const W = 160
const H = 120

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

interface VolSample { target: string; body: number; pos: [number, number, number]; yaw: number; cfg: SensitivityBody }
const F = fix as unknown as { profiles: { kind: string; model: string; frame: unknown }[]; volumes: { frame: unknown; samples: VolSample[] } }

function source(uavPos: [number, number, number][], hide = -1): FieldSource {
  const samples = F.volumes.samples
  const ids = [...new Set(samples.map((s) => s.target))]
  const targets: TargetLookup = {
    rowOf: (id) => ids.indexOf(id),
    keyOf: (row) => row,
    pos: (row, out) => {
      out[0] = 0
      out[1] = 30 + 10 * row
      out[2] = 0
    },
    yaw: (row) => samples.find((x) => x.target === ids[row])!.yaw,
    body: (id, _row, idx) => {
      const c = samples.find((x) => x.target === id && x.body === idx)?.cfg
      return c ? { ...c, radius_max_m: 25 } : null
    },
  }
  return {
    model: fieldModel,
    uavs: {
      agentNo: (id) => (/^uav-0(\d)$/.test(id) ? Number(id.slice(5)) - 1 : -1),
      pose: (a, out) => {
        if (a < 0 || a >= uavPos.length || a === hide) return false
        out[0] = uavPos[a][0]
        out[1] = uavPos[a][1]
        out[2] = uavPos[a][2]
        return true
      },
    },
    targets,
    volumeModel: () => 'p600_mid360',
    structureVersion: () => 0,
  }
}

function setup(): { r: WebGLRenderer; gl: WebGL2RenderingContext; scene: Scene; world: Group; cam: PerspectiveCamera; rt: WebGLRenderTarget } {
  const r = new WebGLRenderer({ canvas: document.createElement('canvas'), antialias: false })
  r.setNodesHandler(new AnetNodesHandler())
  r.setPixelRatio(1)
  r.setSize(W, H, false)
  const scene = new Scene()
  const world = new Group()
  world.rotation.x = -Math.PI / 2 // ENU -> three, as WorldRoot
  scene.add(world)
  const cam = new PerspectiveCamera(60, W / H, 0.5, 5000)
  cam.position.set(0, 140, 160) // three frame: above and south of the origin
  cam.lookAt(0, 0, 0)
  cam.updateMatrixWorld()
  world.updateMatrixWorld(true)
  return { r, gl: r.getContext() as WebGL2RenderingContext, scene, world, cam, rt: new WebGLRenderTarget(W, H) }
}

async function draw(s: ReturnType<typeof setup>): Promise<Uint8Array> {
  s.r.setRenderTarget(s.rt)
  s.r.setClearColor(0x000000, 1)
  s.r.clear()
  s.r.render(s.scene, s.cam)
  s.r.setRenderTarget(null)
  const px = new Uint8Array(W * H * 4)
  await s.r.readRenderTargetPixelsAsync(s.rt, 0, 0, W, H, px)
  return px
}

const lit = (px: Uint8Array): number => {
  let n = 0
  for (let i = 0; i < px.length; i += 4) if (px[i] + px[i + 1] + px[i + 2] > 12) n++
  return n
}
const redLean = (px: Uint8Array): number => {
  let worst = 0
  for (let i = 0; i < px.length; i += 4) worst = Math.max(worst, px[i] - Math.max(px[i + 1], px[i + 2]))
  return worst
}

function programBudget(gl: WebGL2RenderingContext, r: WebGLRenderer): { maxV: number; maxF: number; maxAttr: number; maxVaryingFloats: number } {
  const progs = (r.info.programs ?? []) as unknown as { program: WebGLProgram }[]
  let maxV = 0, maxF = 0, maxAttr = 0, maxVar = 0
  for (const p of progs) {
    const n = gl.getProgramParameter(p.program, gl.ACTIVE_UNIFORM_BLOCKS) as number
    let v = 0, f = 0
    for (let i = 0; i < n; i++) {
      if (gl.getActiveUniformBlockParameter(p.program, i, gl.UNIFORM_BLOCK_REFERENCED_BY_VERTEX_SHADER)) v++
      if (gl.getActiveUniformBlockParameter(p.program, i, gl.UNIFORM_BLOCK_REFERENCED_BY_FRAGMENT_SHADER)) f++
    }
    maxV = Math.max(maxV, v)
    maxF = Math.max(maxF, f)
    maxAttr = Math.max(maxAttr, gl.getProgramParameter(p.program, gl.ACTIVE_ATTRIBUTES) as number)
    const vs = (gl.getAttachedShaders(p.program) ?? []).find((sh) => gl.getShaderParameter(sh, gl.SHADER_TYPE) === gl.VERTEX_SHADER)
    const src = vs ? gl.getShaderSource(vs) ?? '' : ''
    let floats = 0
    for (const m of src.matchAll(/^\s*(?:flat\s+)?out\s+(?:highp\s+|mediump\s+|lowp\s+)?(float|vec2|vec3|vec4)\s+\w+/gm)) floats += { float: 1, vec2: 2, vec3: 3, vec4: 4 }[m[1] as 'float']
    maxVar = Math.max(maxVar, floats)
  }
  return { maxV, maxF, maxAttr, maxVaryingFloats: maxVar }
}

function ctxOf(cam: PerspectiveCamera): FrameCtx {
  return { camera: cam, dpr: 1, dbW: W, dbH: H } as unknown as FrameCtx
}

describe('field view programs under the WebGL2 minimum limits (D2-AC-54)', () => {
  let emu: ReturnType<typeof emulate>
  beforeEach(() => {
    emu = emulate(WEBGL2_MIN_LIMITS, 16384)
    fieldModel.reset()
  })
  afterEach(() => {
    emu.restore()
    fieldModel.reset()
    fieldModel.kind = 'none'
    fieldModel.volumesMode = 'off'
  })

  for (const mode of ['glpoint', 'quad'] as const) {
    it(`${mode}: radiation field, shells and volumes in grey, one draw each, no GL error, no new program on a kind switch`, async () => {
      const err = vi.spyOn(console, 'error').mockImplementation(() => {})
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const s = setup()
      const fr = new FieldRenderer({ pointSizeMode: mode, createPointsMaterial: () => new GLPointsNodeMaterial(), budget: FIELD_BUDGETS.desktopS })
      s.world.add(fr.root)
      s.world.updateMatrixWorld(true)
      const empty = await draw(s)
      expect(lit(empty)).toBe(0)
      // acoustic view of two vehicles
      for (const k of ['acoustic', 'visual', 'thermal', 'radar']) {
        const p = F.profiles.find((x) => x.kind === k && x.model === 'p600_mid360')!
        fieldModel.profiles.set(k as 'acoustic', decodeFieldProfile(p.frame)!)
      }
      fieldModel.kind = 'acoustic'
      fieldModel.version++
      const src = source([[-30, 0, 20], [30, 0, 20]])
      fr.update(ctxOf(s.cam), src, 10_000)
      expect(fr.stats.uavAnchors).toBe(2)
      expect(fr.stats.points).toBeGreaterThan(100)
      expect(fr.drawCount()).toBe(2)
      s.world.updateMatrixWorld(true)
      const a = await draw(s)
      const programs0 = (s.r.info.programs ?? []).length
      expect(lit(a)).toBeGreaterThan(150)
      expect(redLean(a)).toBeLessThanOrEqual(3)
      // the other kinds: data only
      for (const k of ['visual', 'thermal', 'radar'] as const) {
        fieldModel.kind = k
        fieldModel.version++
        fr.update(ctxOf(s.cam), src, 20_000)
        expect(fr.stats.kind).toBe(k)
        const px = await draw(s)
        expect(lit(px), k).toBeGreaterThan(50)
        expect(redLean(px), k).toBeLessThanOrEqual(3)
      }
      expect((s.r.info.programs ?? []).length).toBe(programs0)
      // volumes of the fixture targets (all), no field view
      fieldModel.kind = 'none'
      fieldModel.volumes = decodeFieldVolumes(F.volumes.frame)
      fieldModel.volumesMode = 'all'
      fieldModel.version++
      fr.update(ctxOf(s.cam), src, 30_000)
      expect(fr.stats.bodyAnchors).toBeGreaterThan(0)
      expect(fr.drawCount()).toBe(1)
      const v = await draw(s)
      expect(lit(v)).toBeGreaterThan(20)
      expect(redLean(v)).toBeLessThanOrEqual(3)
      expect((s.r.info.programs ?? []).length).toBe(programs0)
      // a vehicle that disappears: its anchor texel is cleared and its points collapse
      fieldModel.kind = 'acoustic'
      fieldModel.volumesMode = 'off'
      fieldModel.version++
      fr.update(ctxOf(s.cam), source([[-30, 0, 20], [30, 0, 20]], 1), 40_000)
      const d = fr.anchors.image.data as Float32Array
      expect(d[3]).toBe(1)
      expect(d[7]).toBe(0)
      const one = await draw(s)
      expect(lit(one)).toBeLessThan(lit(a))
      expect(lit(one)).toBeGreaterThan(30)
      const b = programBudget(s.gl, s.r)
      expect(b.maxV).toBeLessThanOrEqual(3)
      expect(b.maxF).toBeLessThanOrEqual(2)
      expect(b.maxAttr).toBeLessThanOrEqual(8)
      expect(b.maxVaryingFloats).toBeLessThanOrEqual(16)
      expect(s.gl.getError()).toBe(s.gl.NO_ERROR)
      expect(emu.errors).toEqual([])
      const msgs = [...err.mock.calls, ...warn.mock.calls].map((c) => String(c[0])).filter((x) => !/THREE\.Clock/.test(x))
      err.mockRestore()
      warn.mockRestore()
      expect(msgs).toEqual([])
      fr.dispose()
      s.rt.dispose()
      s.r.dispose()
    })
  }
})
