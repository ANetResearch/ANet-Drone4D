// AnetNodesHandler (ADR-007; M06 §6.3; g01-gap §6.2, verified code). Owner: M06.
// WebGLNodesHandler subclass for the classic WebGLRenderer (Tier B/S):
//   fix 1: when a non-XR render target is bound, output linear colour without tone mapping (the stock handler encodes
//          sRGB into render targets too, so multi-pass chains double-encode);
//   fix 2: honour scene.fogNode like WebGPURenderer's NodeManager.getFogNode() (the stock handler only reads scene.fog);
//   fix 3: expose renderer.depth (the default framebuffer's depth attribute): NodeMaterial.setup() only emits
//          depthNode / gl_FragDepth on the default framebuffer when renderer.depth === true, a WebGPURenderer field that
//          WebGLRenderer lacks, so the P2 composite's depth write-back silently vanished on screen (it worked into RTs).
//   fix 4: a material flagged userData.awrOutputInVertex (the Tier S point material, ADR-064) already carries the output
//          colour transform in its vertex stage: its output passes through unchanged on every target.
//   fix 5: uniform blocks are bound per draw (FX-UBO, ADR-086; backend/uboBinder.ts). The stock handler hands its node
//          UniformsGroups to WebGLRenderer, which keeps one global binding point per group for life: 2-3 groups per
//          material and program exhaust the 24 binding points WebGL2 guarantees (synthcity peaks at 60 on Tier S and 68
//          on Tier B; SwiftShader reports 72, real GPUs often 24), after which every draw through the overflow point
//          fails with GL_INVALID_OPERATION. Here the renderer never sees the groups (material.uniformsGroups stays
//          empty); the binder uploads them and binds block i of the current program to point i before each draw, in the
//          material's onBeforeRender (the program is known) and again in onUpdateProgram (the program changed inside
//          setProgram); renderStart switches a fresh camera to the reversed-depth projection before the first upload.
//          Every generated program is checked against the device's block limits before it is created; one over budget
//          is replaced by a program that draws nothing and reported (stats.violations, onViolation; M06-E017).
//   fix 6: run the programs' updateBefore nodes (VERIFY-UBO, ADR-087). The stock handler only runs builder.updateNodes;
//          three's instancing falls back from a uniform buffer to interleaved instance attributes when the matrices
//          exceed MAX_UNIFORM_BLOCK_SIZE (the low-poly batches of Tier B: 300 x 64 B = 19 200 B > 16 384, the WebGL2
//          minimum and the value of ANGLE Metal; SwiftShader reports 65 536) and syncs that buffer's version to
//          instanceMatrix in an OnBeforeFrameUpdate node, so the GPU kept the matrices of the first upload (identity at
//          the origin) and every low-poly vehicle was drawn there. FRAME nodes of every live program run in renderStart,
//          before WebGLRenderer uploads the attributes of the frame (projectObject); every updateBefore node of the drawn
//          program runs in its onBeforeRender (OBJECT / RENDER semantics; FRAME ones once per frame). updateAfter nodes
//          are not run (none in D1; the gpu-limits spec asserts it).
// It relies on three internals (getOutputCallback / onBeforeRenderCallback instance properties, programCache entries'
// uniformsGroups, material._latestBuilder.updateBeforeNodes, nodeFrame.updateBeforeNode, renderStack[].sceneContext, the
// proxy forwarding getRenderTarget); the feat-matrix regression page and the gpu-limits spec guard them on three
// upgrades (g01 §9, FX-UBO, VERIFY-UBO).
import type { Material, Object3D, WebGLProgramParametersWithUniforms, WebGLRenderer } from 'three'
import { WebGLNodesHandler } from 'three/addons/tsl/WebGLNodesHandler.js'
import { workingToColorSpace } from 'three/tsl'
import { programBlocks, readUboLimits, UboBinder, type GroupLike, type UboLimits, type UboStats } from './backend/uboBinder'

type AnyNode = any // TSL output node (loosely typed in @types/three)
interface ProgramLike { program: WebGLProgram }
interface UpdateNode { updateBeforeType: string }
interface ProgramEntry { uniformsGroups: GroupLike[]; awrBefore?: readonly UpdateNode[]; awrBeforeFrame?: readonly UpdateNode[] }
interface HandlerInternals {
  nodeFrame: { material: unknown; object: unknown; updateBeforeNode(node: UpdateNode): void }
  renderer: { getRenderTarget(): { isXRRenderTarget?: boolean } | null; toneMapping: number; outputColorSpace: string
    state: { buffers: { depth: { getReversed(): boolean } } } }
  renderStack: { sceneContext: { fogNode: unknown } }[]
  programCache: Map<Material, Map<ProgramLike, ProgramEntry>>
  getOutputCallback: (out: AnyNode, builder: { material?: { toneMapped?: boolean; userData?: { awrOutputInVertex?: boolean } } }) => AnyNode
  onBeforeRenderCallback: (this: Material, renderer: WebGLRenderer, scene: unknown, camera: unknown, geometry: unknown, object: Object3D, group: unknown) => void
}
/** a program over the device's uniform block budget (replaced by NULL_VS / NULL_FS) */
export interface UboViolation { object: Object3D; material: Material; reason: string; vertex: number; fragment: number }

const NO_GROUPS: readonly GroupLike[] = Object.freeze([])
// draws nothing on any target: every vertex lies beyond the far plane (z > w for standard and reversed depth) and the
// fragment stage discards; no attributes, no uniforms, no blocks. The output is written before the discard: an output
// without a static write counts as missing, and ANGLE rejects the draw ("Active draw buffers with missing fragment
// shader outputs", GL_INVALID_OPERATION)
const NULL_VS = 'precision highp float;\nvoid main() {\n\tgl_Position = vec4( 0.0, 0.0, 2.0, 1.0 );\n\tgl_PointSize = 1.0;\n}\n'
const NULL_FS = 'precision highp float;\nlayout( location = 0 ) out vec4 awrNullColor;\nvoid main() {\n\tawrNullColor = vec4( 0.0 );\n\tdiscard;\n}\n'

const handlers = new WeakMap<object, AnetNodesHandler>()

export class AnetNodesHandler extends WebGLNodesHandler {
  /** the handler installed on a renderer (set in setRenderer) */
  static of(renderer: object): AnetNodesHandler | null {
    return handlers.get(renderer) ?? null
  }

  /** test hook: device limits to use instead of the context's (set before renderer.setNodesHandler) */
  limitsOverride: UboLimits | null = null
  ubo: UboBinder | null = null
  readonly violations: UboViolation[] = []
  /** called once per program over the block budget (the backend hides the layer and warns, M06-E017) */
  onViolation: ((v: UboViolation) => void) | null = null

  constructor() {
    super()
    const self = this as unknown as HandlerInternals
    self.getOutputCallback = (out, builder) => {
      if (builder?.material?.userData?.awrOutputInVertex === true) return out
      const r = self.renderer
      const rt = r.getRenderTarget()
      if (rt !== null && rt.isXRRenderTarget !== true) return out
      let o = out
      if (builder?.material?.toneMapped !== false) o = o.toneMapping(r.toneMapping)
      return workingToColorSpace(o, r.outputColorSpace as never)
    }
    // fix 5: bind the uniform blocks of the material's current program before its draw (setObject installs this
    // callback as material.onBeforeRender on every render)
    const stock = self.onBeforeRenderCallback
    const binder = (): UboBinder | null => this.ubo
    self.onBeforeRenderCallback = function (this: Material, renderer, scene, camera, geometry, object, group) {
      const program = renderer.properties.has(this) ? (renderer.properties.get(this) as { currentProgram?: ProgramLike }).currentProgram : undefined
      const entry = program ? self.programCache.get(this)?.get(program) : undefined
      // fix 6: the program's updateBefore nodes before its update nodes (WebGPURenderer order)
      const before = entry?.awrBefore
      if (before) {
        self.nodeFrame.material = this
        self.nodeFrame.object = object
        for (let i = 0; i < before.length; i++) self.nodeFrame.updateBeforeNode(before[i])
      }
      stock.call(this, renderer, scene, camera, geometry, object, group)
      const ubo = binder()
      if (ubo && entry && entry.uniformsGroups.length > 0) ubo.bind(entry.uniformsGroups, program!.program)
    }
  }

  setRenderer(renderer: Parameters<WebGLNodesHandler['setRenderer']>[0]): void {
    super.setRenderer(renderer)
    const r = renderer as unknown as { depth?: boolean; getContext(): WebGL2RenderingContext }
    const gl = r.getContext()
    if (r.depth === undefined) r.depth = gl.getContextAttributes()?.depth !== false
    this.ubo = new UboBinder(gl, this.limitsOverride ?? readUboLimits(gl))
    handlers.set(renderer as object, this)
  }

  /** binding points, block counts, live groups and budget violations (published as window.__perf.gpu.ubo) */
  get uboStats(): UboStats | null {
    return this.ubo?.stats ?? null
  }

  build(material: Material, object: Object3D, parameters: WebGLProgramParametersWithUniforms): void {
    super.build(material, object, parameters)
    const ubo = this.ubo
    if (!ubo) return
    const b = programBlocks(parameters.vertexShader, parameters.fragmentShader)
    const reason = ubo.noteProgram(b)
    if (reason === null) return
    // over the device's block budget: linking would fail (or bind blocks past the binding points); draw nothing instead
    parameters.vertexShader = NULL_VS
    parameters.fragmentShader = NULL_FS
    const v: UboViolation = { object, material, reason, vertex: b.vertex, fragment: b.fragment }
    this.violations.push(v)
    this.onViolation?.(v)
  }

  /** WebGLRenderer.setProgram calls this after a program change (not in @types/three) */
  onUpdateProgram(material: Material, program: ProgramLike, materialProperties: object): void {
    const self = this as unknown as HandlerInternals
    const fresh = self.programCache.get(material)?.has(program) !== true
    ;(WebGLNodesHandler.prototype as unknown as { onUpdateProgram(m: Material, p: ProgramLike, mp: object): void }).onUpdateProgram.call(this, material, program, materialProperties)
    // fix 6: keep the program's updateBefore nodes with its cache entry (the stock entry takes the same builder)
    const entry = self.programCache.get(material)?.get(program)
    if (entry && fresh) {
      const before = (material as unknown as { _latestBuilder?: { updateBeforeNodes?: UpdateNode[] } })._latestBuilder?.updateBeforeNodes ?? []
      if (before.length > 0) {
        entry.awrBefore = before.slice()
        const frame = before.filter((n) => n.updateBeforeType === 'frame')
        if (frame.length > 0) entry.awrBeforeFrame = frame
      }
    }
    const before = entry?.awrBefore
    if (before) for (let i = 0; i < before.length; i++) self.nodeFrame.updateBeforeNode(before[i])
    // fix 5: keep the groups away from WebGLRenderer's global binding points; bind them for the new program now (the
    // material's onBeforeRender ran before setProgram switched programs)
    const m = material as unknown as { uniformsGroups?: readonly GroupLike[] }
    const groups = m.uniformsGroups ?? NO_GROUPS
    m.uniformsGroups = NO_GROUPS
    if (groups.length > 0) this.ubo?.bind(groups, program.program)
  }

  renderStart(scene: Parameters<WebGLNodesHandler['renderStart']>[0], camera: Parameters<WebGLNodesHandler['renderStart']>[1],
    targetScene: Parameters<WebGLNodesHandler['renderStart']>[2] = scene): void {
    super.renderStart(scene, camera, targetScene)
    const self = this as unknown as HandlerInternals
    // fix 5: WebGLRenderer switches a camera to the reversed-depth projection inside setProgram of its first draw, after
    // the material's onBeforeRender has uploaded the blocks (the stock handler uploads later, in setProgram): switch it
    // here, before any draw of this render, as setProgram would (same condition, same calls)
    const cam = camera as unknown as { reversedDepth?: boolean; _reversedDepth?: boolean; updateProjectionMatrix?(): void }
    if (cam.reversedDepth !== true && typeof cam.updateProjectionMatrix === 'function' && self.renderer.state.buffers.depth.getReversed()) {
      cam._reversedDepth = true
      cam.updateProjectionMatrix()
    }
    const ctx = self.renderStack[self.renderStack.length - 1]?.sceneContext
    const fogNode = (targetScene as unknown as { fogNode?: unknown }).fogNode
    if (ctx && fogNode) ctx.fogNode = fogNode
    // fix 6: FRAME updateBefore nodes of every live program before projectObject uploads the frame's attributes (the
    // instancing fallback syncs its interleaved buffer's version here, so the matrices of this frame reach the GPU);
    // forEach with callbacks made once: nothing is allocated per render
    self.programCache.forEach(this.frameBeforeOfMaterial)
  }

  private readonly frameBeforeOfEntry = (e: ProgramEntry): void => {
    const ns = e.awrBeforeFrame
    if (!ns) return
    const frame = (this as unknown as HandlerInternals).nodeFrame
    for (let i = 0; i < ns.length; i++) frame.updateBeforeNode(ns[i])
  }
  private readonly frameBeforeOfMaterial = (programs: Map<ProgramLike, ProgramEntry>): void => programs.forEach(this.frameBeforeOfEntry)
}
