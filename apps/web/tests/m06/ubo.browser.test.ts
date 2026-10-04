// Uniform blocks of node materials under the WebGL2 minimum limits (FX-UBO, ADR-086; AnetNodesHandler fix 5,
// viewport/backend/uboBinder.ts). The context reports MAX_UNIFORM_BUFFER_BINDINGS 24 (and 12 / 12 / 24 blocks) instead of
// SwiftShader's 72 / 14 / 14 / 60, and refuses binding points past 24 like a driver (GL_INVALID_VALUE):
//   - the stock WebGLNodesHandler exhausts the binding points with 40 materials (the public demo failure);
//   - AnetNodesHandler draws 40 materials, each with its own uniform colour, with 2 binding points, also after the
//     values change, also for two materials sharing one program, without a GL error;
//   - disposing materials deletes their buffers; a program over the block budget draws nothing and is reported;
//   - a fresh camera on a reversed depth buffer is right on its first draw (blocks upload before setProgram);
//   - attribute-less GLPointsNodeMaterial points build without the AttributeNode "position" warning;
//   - instance matrices over MAX_UNIFORM_BLOCK_SIZE (16 384, the WebGL2 minimum) fall back to interleaved instance
//     attributes, and their updates reach the GPU (fix 6, VERIFY-UBO, ADR-087; the stock handler kept the first upload).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BufferGeometry, Color, DynamicDrawUsage, InstancedMesh, Matrix4, Mesh, OrthographicCamera, PerspectiveCamera, PlaneGeometry, Points, Scene, Sphere, Vector3, WebGLRenderer, WebGLRenderTarget } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, builtin, float, uniform, vec3, vec4 } from 'three/tsl'
import { WebGLNodesHandler } from 'three/addons/tsl/WebGLNodesHandler.js'
import { AnetNodesHandler } from '@/viewport/anetNodesHandler'
import { WEBGL2_MIN_LIMITS, type UboLimits } from '@/viewport/backend/uboBinder'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

type N = any // TSL nodes
const UB = 0x8a11

/** report and enforce `L` on every WebGL2 context created while installed (blockSize: MAX_UNIFORM_BLOCK_SIZE, if given) */
function emulate(L: UboLimits, blockSize?: number): { errors: string[]; restore(): void } {
  const P = WebGL2RenderingContext.prototype
  // the original methods, taken from their property descriptors (called with the context as `this`)
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  const gp = own('getParameter')
  const bbb = own('bindBufferBase')
  const ubb = own('uniformBlockBinding')
  const errors: string[] = []
  P.getParameter = function (this: WebGL2RenderingContext, n: number) {
    if (n === 0x8a2f) return L.bindings
    if (n === 0x8a2b) return L.vertex
    if (n === 0x8a2d) return L.fragment
    if (n === 0x8a2e) return L.combined
    if (n === 0x8a30 && blockSize !== undefined) return blockSize
    return gp.call(this, n)
  } as typeof P.getParameter
  P.bindBufferBase = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null) {
    if (t === UB && i >= L.bindings) errors.push(`bindBufferBase ${i}`)
    else bbb.call(this, t, i, b)
  }
  P.uniformBlockBinding = function (this: WebGL2RenderingContext, p: WebGLProgram, bi: number, point: number) {
    if (point >= L.bindings) errors.push(`uniformBlockBinding ${point}`)
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

const GRID_X = 8
const GRID_Y = 5
const CELL = 8
const W = GRID_X * CELL
const H = GRID_Y * CELL

function classic(handler: WebGLNodesHandler, reversed = false): WebGLRenderer {
  const r = new WebGLRenderer({ canvas: document.createElement('canvas'), antialias: false, reversedDepthBuffer: reversed })
  r.setNodesHandler(handler)
  if (reversed) (r as unknown as { reversedDepthBuffer: boolean }).reversedDepthBuffer = r.capabilities.reversedDepthBuffer
  r.setPixelRatio(1)
  r.setSize(W, H, false)
  return r
}

/** 40 quads in an 8 x 5 grid, each with its own material and colour uniform (object group) */
function grid(): { scene: Scene; colors: N[]; mats: MeshBasicNodeMaterial[] } {
  const scene = new Scene()
  const colors: N[] = []
  const mats: MeshBasicNodeMaterial[] = []
  for (let i = 0; i < GRID_X * GRID_Y; i++) {
    const c = uniform(new Color((i % 8) / 7, Math.floor(i / 8) / 4, ((i * 3) % 5) / 4))
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec4(c, 1) as N
    m.depthTest = false
    m.fog = false
    const q = new Mesh(new PlaneGeometry(CELL, CELL), m)
    q.position.set((i % GRID_X) * CELL + CELL / 2, Math.floor(i / GRID_X) * CELL + CELL / 2, 0)
    scene.add(q)
    colors.push(c)
    mats.push(m)
  }
  return { scene, colors, mats }
}

async function renderRead(r: WebGLRenderer, scene: Scene, cam: OrthographicCamera | PerspectiveCamera): Promise<Uint8Array> {
  const rt = new WebGLRenderTarget(W, H)
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.render(scene, cam)
  r.setRenderTarget(null)
  const out = new Uint8Array(W * H * 4)
  await r.readRenderTargetPixelsAsync(rt, 0, 0, W, H, out)
  rt.dispose()
  return out
}
const cellPx = (px: Uint8Array, i: number): number[] => {
  const x = (i % GRID_X) * CELL + CELL / 2
  const y = Math.floor(i / GRID_X) * CELL + CELL / 2
  const o = 4 * (y * W + x)
  return [px[o], px[o + 1], px[o + 2]]
}
const want = (c: Color): number[] => [c.r, c.g, c.b].map((v) => Math.round(v * 255)) // render targets hold linear colour (fix 1)
const ortho = (): OrthographicCamera => {
  const c = new OrthographicCamera(0, W, H, 0, -10, 10)
  c.updateMatrixWorld()
  return c
}

describe('uniform blocks under WebGL2 minimum limits (FX-UBO)', () => {
  let emu: ReturnType<typeof emulate>
  beforeEach(() => {
    emu = emulate(WEBGL2_MIN_LIMITS)
  })
  afterEach(() => emu.restore())

  it('the stock handler runs out of binding points with 40 materials (the root cause)', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const r = classic(new WebGLNodesHandler())
    const { scene } = grid()
    await renderRead(r, scene, ortho())
    const msgs = err.mock.calls.map((c) => String(c[0]))
    err.mockRestore()
    expect(msgs.some((m) => /Maximum number of simultaneously usable uniforms groups/.test(m))).toBe(true)
    r.dispose()
  })

  it('40 materials draw their own uniforms with 2 binding points, also after the values change', async () => {
    const h = new AnetNodesHandler()
    const r = classic(h)
    const gl = r.getContext()
    const { scene, colors, mats } = grid()
    const px = await renderRead(r, scene, ortho())
    for (let i = 0; i < colors.length; i++) {
      const got = cellPx(px, i)
      const exp = want(colors[i].value)
      for (let k = 0; k < 3; k++) expect(Math.abs(got[k] - exp[k]), `cell ${i}: ${JSON.stringify(got)} vs ${JSON.stringify(exp)}`).toBeLessThanOrEqual(2)
    }
    for (const c of colors) (c.value as Color).offsetHSL(0.37, 0, 0)
    const px2 = await renderRead(r, scene, ortho())
    for (let i = 0; i < colors.length; i++) {
      const got = cellPx(px2, i)
      const exp = want(colors[i].value)
      for (let k = 0; k < 3; k++) expect(Math.abs(got[k] - exp[k]), `cell ${i} after the change`).toBeLessThanOrEqual(2)
    }
    const st = h.uboStats!
    expect(st.limits).toEqual(WEBGL2_MIN_LIMITS)
    expect(st.points).toBeLessThanOrEqual(3)
    expect(st.groups).toBeGreaterThanOrEqual(40)
    expect(st.violations).toBe(0)
    expect(emu.errors).toEqual([])
    expect(gl.getError()).toBe(gl.NO_ERROR)
    // the renderer never sees the groups (no global binding point per group)
    for (const m of mats) expect((m as unknown as { uniformsGroups: unknown[] }).uniformsGroups).toEqual([])
    // disposing materials deletes their uniform buffers
    const before = st.groups
    for (const m of mats.slice(0, 10)) m.dispose()
    expect(st.groups).toBeLessThanOrEqual(before - 10)
    expect(st.deleted).toBeGreaterThanOrEqual(10)
    r.dispose()
  })

  it('one material on several objects: each draw uploads its own object block', async () => {
    const h = new AnetNodesHandler()
    const r = classic(h)
    const scene = new Scene()
    const m = new MeshBasicNodeMaterial()
    const c = new Color(0.9, 0.1, 0.1)
    m.colorNode = vec4(uniform(c), 1) as N
    m.depthTest = false
    const cells = [0, 9, 18, 27, 39]
    for (const i of cells) {
      const q = new Mesh(new PlaneGeometry(CELL, CELL), m) // one material, one program, one set of groups
      q.position.set((i % GRID_X) * CELL + CELL / 2, Math.floor(i / GRID_X) * CELL + CELL / 2, 0)
      scene.add(q)
    }
    const px = await renderRead(r, scene, ortho())
    expect((r.info.programs ?? []).length).toBe(1)
    for (const i of cells) expect(cellPx(px, i), `cell ${i}`).toEqual(want(c))
    expect(cellPx(px, 1)).toEqual([0, 0, 0])
    r.dispose()
  })

  it('a program over the block budget draws nothing and is reported', async () => {
    const h = new AnetNodesHandler()
    h.limitsOverride = { ...WEBGL2_MIN_LIMITS, vertex: 1 } // every node material uses 2 vertex blocks (render, object)
    const seen: string[] = []
    h.onViolation = (v) => seen.push(`${v.object.name}: ${v.reason}`)
    const r = classic(h)
    const gl = r.getContext()
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec4(uniform(new Color(1, 1, 1)), 1) as N
    m.depthTest = false
    const q = new Mesh(new PlaneGeometry(W, H), m)
    q.name = 'OverBudget'
    q.position.set(W / 2, H / 2, 0)
    const s = new Scene()
    s.add(q)
    const px = await renderRead(r, s, ortho())
    expect(seen).toEqual(['OverBudget: vertex blocks 2 > 1'])
    expect(h.uboStats!.violations).toBe(1)
    expect(h.violations[0].object).toBe(q)
    expect(cellPx(px, 0)).toEqual([0, 0, 0])
    expect(gl.getError()).toBe(gl.NO_ERROR)
    r.dispose()
  })

  it('a fresh camera on a reversed depth buffer is right on its first draw', async () => {
    const h = new AnetNodesHandler()
    const r = classic(h, true)
    expect((r as unknown as { reversedDepthBuffer: boolean }).reversedDepthBuffer).toBe(true)
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec4(uniform(new Color(0.2, 0.6, 1)), 1) as N
    const q = new Mesh(new PlaneGeometry(400, 400), m)
    q.position.z = -50
    const s = new Scene()
    s.add(q)
    const cam = new PerspectiveCamera(60, W / H, 1, 1000)
    const px = await renderRead(r, s, cam)
    expect(cellPx(px, 12)).toEqual(want(new Color(0.2, 0.6, 1)))
    r.dispose()
  })

  it('attribute-less GLPointsNodeMaterial points build without the position warning and draw at positionNode', async () => {
    const warn = vi.spyOn(console, 'warn')
    const h = new AnetNodesHandler()
    const r = classic(h)
    const m = new GLPointsNodeMaterial()
    const at = uniform(new Vector3(20.5, 12.5, 0))
    m.positionNode = Fn(() => {
      builtin('gl_PointSize').assign(float(4))
      return vec3(at)
    })() as N
    m.colorNode = vec4(1, 1, 1, 1) as N
    const g = new BufferGeometry()
    g.setDrawRange(0, 1)
    g.boundingSphere = new Sphere(new Vector3(), 1e6)
    const p = new Points(g, m)
    p.frustumCulled = false
    const s = new Scene()
    s.add(p)
    const px = await renderRead(r, s, ortho())
    const msgs = warn.mock.calls.map((c) => String(c[0]))
    warn.mockRestore()
    expect(msgs.filter((t) => /AttributeNode/.test(t))).toEqual([])
    const o = 4 * (12 * W + 20)
    expect(px[o]).toBeGreaterThan(200)
    r.dispose()
  })

  describe('instance matrices over MAX_UNIFORM_BLOCK_SIZE 16 384 (fix 6, VERIFY-UBO)', () => {
    beforeEach(() => {
      emu.restore()
      emu = emulate(WEBGL2_MIN_LIMITS, 16384)
    })

    /** 300 instances (19 200 B of matrices, the Tier B low-poly cap): three uses interleaved instance attributes */
    async function moveInstance(h: WebGLNodesHandler): Promise<{ first: number[][]; moved: number[][]; attributes: boolean; err: number }> {
      const r = classic(h)
      const gl = r.getContext()
      const m = new MeshBasicNodeMaterial()
      m.colorNode = vec4(1, 1, 1, 1) as N
      m.depthTest = false
      const im = new InstancedMesh(new PlaneGeometry(CELL, CELL), m, 300)
      im.instanceMatrix.setUsage(DynamicDrawUsage)
      im.frustumCulled = false
      im.count = 1
      const at = (i: number): Matrix4 => new Matrix4().makeTranslation((i % GRID_X) * CELL + CELL / 2, Math.floor(i / GRID_X) * CELL + CELL / 2, 0)
      const s = new Scene()
      s.add(im)
      const cam = ortho()
      im.setMatrixAt(0, at(0))
      im.instanceMatrix.needsUpdate = true
      await renderRead(r, s, cam) // builds the program (WebGLNodesHandler injects the instance attributes afterwards)
      await Promise.resolve()
      const px1 = await renderRead(r, s, cam)
      im.setMatrixAt(0, at(9))
      im.instanceMatrix.needsUpdate = true
      const px2 = await renderRead(r, s, cam)
      const prog = (r.properties.get(m) as { currentProgram: { program: WebGLProgram } }).currentProgram.program
      const out = { first: [cellPx(px1, 0), cellPx(px1, 9)], moved: [cellPx(px2, 0), cellPx(px2, 9)], attributes: gl.getAttribLocation(prog, 'nodeAttribute0') >= 0, err: gl.getError() }
      r.dispose()
      return out
    }

    it('the stock handler keeps the matrices of the first upload (the low-poly vehicles of Tier B stayed put)', async () => {
      const o = await moveInstance(new WebGLNodesHandler())
      expect(o.attributes).toBe(true)
      expect(o.first).toEqual([[255, 255, 255], [0, 0, 0]])
      expect(o.moved).toEqual([[255, 255, 255], [0, 0, 0]])
    })

    it('AnetNodesHandler syncs the interleaved instance buffer: the moved instance draws at its new place', async () => {
      const o = await moveInstance(new AnetNodesHandler())
      expect(o.attributes).toBe(true)
      expect(o.first).toEqual([[255, 255, 255], [0, 0, 0]])
      expect(o.moved).toEqual([[0, 0, 0], [255, 255, 255]])
      expect(o.err).toBe(0)
      expect(emu.errors).toEqual([])
    })
  })
})
