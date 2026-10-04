// Per-draw uniform-buffer binding for node materials on the classic path (FX-UBO, ADR-086; M06 §6.3 fix 5). Owner: M06.
// three r186 WebGLUniformsGroups gives every UniformsGroup its own global binding point for the group's whole life
// (allocateBindingPointIndex, one bindBufferBase at creation), and WebGLNodesHandler creates one group per uniform group
// name ('render', 'object', ...) and one per buffer node for every (material, program) pair. The synthcity scene holds
// 28 (Tier S) to 31 (Tier B) node materials with 2-3 groups each: 57 / 65 live groups, 60 / 68 binding points at the
// peak, while WebGL2 guarantees only MAX_UNIFORM_BUFFER_BINDINGS = 24 (the value of many real GPUs; SwiftShader reports
// 72, so no test saw it). Past the limit three logs "Maximum number of simultaneously usable uniforms groups reached", hands out
// point 0, and every draw whose block reads a smaller buffer through point 0 fails with GL_INVALID_OPERATION ("uniform
// buffer that is too small").
// UboBinder owns the GL buffers of the node groups instead: block i of every program reads binding point i (set once per
// program with uniformBlockBinding), and before each draw the groups of the material's current program are uploaded
// (changed values only: the std140 layout, value types and change test of three's WebGLUniformsGroups) and bound with
// bindBufferBase to their block's point. The binding points in use are the largest block count of a single program
// (3 in D1), independent of the number of materials, programs and passes. Buffers are sized at least as the program's
// UNIFORM_BLOCK_DATA_SIZE (never "too small"), and deleted when three disposes the group (material dispose).
// The per-program budget (blocks per stage, combined, distinct blocks vs binding points) is checked on the generated
// GLSL before the program is created (blockNames / overBudget; the handler substitutes a program that draws nothing).

const UNIFORM_BUFFER = 0x8a11
const UNIFORM_BLOCK_DATA_SIZE = 0x8a40
const INVALID_INDEX = 0xffffffff
const MAX_UNIFORM_BUFFER_BINDINGS = 0x8a2f
const MAX_VERTEX_UNIFORM_BLOCKS = 0x8a2b
const MAX_FRAGMENT_UNIFORM_BLOCKS = 0x8a2d
const MAX_COMBINED_UNIFORM_BLOCKS = 0x8a2e

/** the device limits that bound uniform blocks (WebGL2 minimums: 24 / 12 / 12 / 24) */
export interface UboLimits { bindings: number; vertex: number; fragment: number; combined: number }
export const WEBGL2_MIN_LIMITS: Readonly<UboLimits> = { bindings: 24, vertex: 12, fragment: 12, combined: 24 }

export function readUboLimits(gl: WebGL2RenderingContext): UboLimits {
  const q = (p: number, d: number): number => {
    const v = Number(gl.getParameter(p))
    return Number.isFinite(v) && v > 0 ? v : d
  }
  return {
    bindings: q(MAX_UNIFORM_BUFFER_BINDINGS, WEBGL2_MIN_LIMITS.bindings), vertex: q(MAX_VERTEX_UNIFORM_BLOCKS, WEBGL2_MIN_LIMITS.vertex),
    fragment: q(MAX_FRAGMENT_UNIFORM_BLOCKS, WEBGL2_MIN_LIMITS.fragment), combined: q(MAX_COMBINED_UNIFORM_BLOCKS, WEBGL2_MIN_LIMITS.combined),
  }
}

const BLOCK_RE = /\buniform\s+([A-Za-z_]\w*)\s*\{/g
/** names of the uniform blocks a GLSL ES 3.0 stage declares (`layout( std140 ) uniform render {`, `uniform NodeBuffer_7 {`) */
export function blockNames(glsl: string): string[] {
  const out: string[] = []
  for (const m of glsl.matchAll(BLOCK_RE)) if (!out.includes(m[1])) out.push(m[1])
  return out
}

export interface ProgramBlocks { vertex: number; fragment: number; distinct: number }
export function programBlocks(vertexGlsl: string, fragmentGlsl: string): ProgramBlocks {
  const v = blockNames(vertexGlsl)
  const f = blockNames(fragmentGlsl)
  const all = new Set([...v, ...f])
  return { vertex: v.length, fragment: f.length, distinct: all.size }
}
/**
 * null when the program fits, otherwise the first exceeded limit. Binding points: block i reads point i, so a program
 * needs as many points as it has distinct blocks (a block referenced by both stages counts once there, twice in combined)
 */
export function overBudget(b: ProgramBlocks, L: UboLimits): string | null {
  if (b.vertex > L.vertex) return `vertex blocks ${b.vertex} > ${L.vertex}`
  if (b.fragment > L.fragment) return `fragment blocks ${b.fragment} > ${L.fragment}`
  if (b.vertex + b.fragment > L.combined) return `combined blocks ${b.vertex + b.fragment} > ${L.combined}`
  if (b.distinct > L.bindings) return `distinct blocks ${b.distinct} > binding points ${L.bindings}`
  return null
}

/** what three's UniformsGroup and NodeUniform expose (UniformsGroup.uniforms holds objects with a `value`) */
export interface UniformLike { value: unknown }
export interface GroupLike {
  readonly id: number
  name: string
  uniforms: (UniformLike | UniformLike[])[]
  usage?: number
  addEventListener(type: 'dispose', fn: (e: { target: GroupLike }) => void): void
  removeEventListener(type: 'dispose', fn: (e: { target: GroupLike }) => void): void
}

type Vecish = { isVector2?: boolean; isVector3?: boolean; isColor?: boolean; isVector4?: boolean; isMatrix3?: boolean; isMatrix4?: boolean; isTexture?: boolean
  elements?: ArrayLike<number>; toArray(a: Float32Array, o: number): unknown; clone(): Vecish; equals(o: unknown): boolean; copy(o: unknown): unknown }

/** std140 base alignment and size in bytes of one value (three WebGLUniformsGroups.getUniformSize; 0 = unsupported) */
export function std140(v: unknown): { align: number; size: number } {
  if (typeof v === 'number' || typeof v === 'boolean') return { align: 4, size: 4 }
  if (ArrayBuffer.isView(v)) return { align: 16, size: v.byteLength }
  const o = v as Vecish | null
  if (!o || typeof o !== 'object') return { align: 4, size: 0 }
  if (o.isVector2) return { align: 8, size: 8 }
  if (o.isVector3 || o.isColor) return { align: 16, size: 12 }
  if (o.isVector4) return { align: 16, size: 16 }
  if (o.isMatrix3) return { align: 16, size: 48 }
  if (o.isMatrix4) return { align: 16, size: 64 }
  return { align: 4, size: 0 }
}

/** one scalar, vector, matrix or typed array of a group at its std140 offset */
interface Slot { u: UniformLike; k: number; off: number; data: Float32Array; cache: unknown }
interface Ubo { buf: WebGLBuffer; slots: Slot[]; size: number }

/** std140 layout of a group: slots in declaration order and the padded block size (three prepareUniformsGroup) */
export function layoutGroup(g: Pick<GroupLike, 'uniforms'>): { slots: { u: UniformLike; k: number; off: number; size: number }[]; size: number; unsupported: number } {
  const slots: { u: UniformLike; k: number; off: number; size: number }[] = []
  let off = 0
  let unsupported = 0
  for (const item of g.uniforms) {
    for (const u of Array.isArray(item) ? item : [item]) {
      const v = u.value
      const vals = Array.isArray(v) ? v : [v]
      for (let k = 0; k < vals.length; k++) {
        const s = std140(vals[k])
        if (s.size === 0) {
          unsupported++
          continue
        }
        off = Math.ceil(off / s.align) * s.align
        slots.push({ u, k: Array.isArray(v) ? k : -1, off, size: s.size })
        off += s.size
      }
    }
  }
  return { slots, size: Math.ceil(off / 16) * 16, unsupported }
}

function write(v: unknown, d: Float32Array): void {
  if (typeof v === 'number') d[0] = v
  else if (typeof v === 'boolean') d[0] = v ? 1 : 0
  else if (ArrayBuffer.isView(v)) {
    const n = Math.min(d.length, Math.floor(v.byteLength / ((v as unknown as { BYTES_PER_ELEMENT?: number }).BYTES_PER_ELEMENT ?? 4)))
    const C = (v as unknown as { constructor: new (b: ArrayBufferLike, o: number, n: number) => ArrayLike<number> }).constructor
    d.set(new C(v.buffer, v.byteOffset, n))
  } else {
    const o = v as Vecish
    if (o.isMatrix3 && o.elements) {
      const e = o.elements // mat3 as three vec4 columns (std140)
      d[0] = e[0]; d[1] = e[1]; d[2] = e[2]; d[3] = 0
      d[4] = e[3]; d[5] = e[4]; d[6] = e[5]; d[7] = 0
      d[8] = e[6]; d[9] = e[7]; d[10] = e[8]; d[11] = 0
    } else o.toArray(d, 0)
  }
}

/** three hasUniformChanged: numbers by value, typed arrays always, objects against a cached clone */
function changed(s: Slot, v: unknown): boolean {
  if (typeof v === 'number' || typeof v === 'boolean') {
    if (s.cache === v) return false
    s.cache = v
    return true
  }
  if (ArrayBuffer.isView(v)) return true
  const o = v as Vecish
  if (s.cache === undefined) {
    s.cache = o.clone()
    return true
  }
  if ((s.cache as Vecish).equals(o)) return false
  ;(s.cache as Vecish).copy(o)
  return true
}

export interface UboStats {
  limits: UboLimits
  /** highest binding point bound + 1 (the binding points in use) */
  points: number
  /** largest block count of one program per stage, combined and distinct (from the generated GLSL) */
  maxVertex: number
  maxFragment: number
  maxCombined: number
  maxDistinct: number
  /** node uniform groups with a live GL buffer, buffers created and deleted */
  groups: number
  created: number
  deleted: number
  /** programs over the block budget (replaced by a program that draws nothing), and the layers hidden for it */
  violations: number
  pruned: string[]
}

export class UboBinder {
  private readonly ubos = new WeakMap<GroupLike, Ubo>()
  private readonly blocks = new WeakMap<WebGLProgram, Map<string, number>>()
  private readonly onDispose = (e: { target: GroupLike }): void => this.release(e.target)
  readonly stats: UboStats

  constructor(private readonly gl: WebGL2RenderingContext, limits: UboLimits = readUboLimits(gl)) {
    this.stats = { limits, points: 0, maxVertex: 0, maxFragment: 0, maxCombined: 0, maxDistinct: 0, groups: 0, created: 0, deleted: 0, violations: 0, pruned: [] }
  }

  /** a program about to be created: record its block counts; returns the exceeded limit or null */
  noteProgram(b: ProgramBlocks): string | null {
    const s = this.stats
    s.maxVertex = Math.max(s.maxVertex, b.vertex)
    s.maxFragment = Math.max(s.maxFragment, b.fragment)
    s.maxCombined = Math.max(s.maxCombined, b.vertex + b.fragment)
    s.maxDistinct = Math.max(s.maxDistinct, b.distinct)
    const over = overBudget(b, s.limits)
    if (over) s.violations++
    return over
  }

  /** upload the changed values of the groups and bind each to the binding point of its block in `program` */
  bind(groups: readonly GroupLike[], program: WebGLProgram): void {
    const gl = this.gl
    let idx = this.blocks.get(program)
    if (idx === undefined) {
      idx = new Map()
      this.blocks.set(program, idx)
    }
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i]
      let bi = idx.get(g.name)
      if (bi === undefined) {
        const b = gl.getUniformBlockIndex(program, g.name)
        bi = b === INVALID_INDEX || b >= this.stats.limits.bindings ? -1 : b
        if (bi >= 0) gl.uniformBlockBinding(program, bi, bi)
        idx.set(g.name, bi)
      }
      if (bi < 0) continue // block not active in this program (optimised out)
      const u = this.ubos.get(g) ?? this.create(g, program, bi)
      this.upload(u)
      gl.bindBufferBase(UNIFORM_BUFFER, bi, u.buf)
      if (bi >= this.stats.points) this.stats.points = bi + 1
    }
  }

  private create(g: GroupLike, program: WebGLProgram, bi: number): Ubo {
    const gl = this.gl
    const lay = layoutGroup(g)
    const glSize = Number(gl.getActiveUniformBlockParameter(program, bi, UNIFORM_BLOCK_DATA_SIZE)) || 0
    const size = Math.max(16, lay.size, glSize)
    const buf = gl.createBuffer()
    gl.bindBuffer(UNIFORM_BUFFER, buf)
    gl.bufferData(UNIFORM_BUFFER, size, g.usage ?? gl.DYNAMIC_DRAW)
    gl.bindBuffer(UNIFORM_BUFFER, null)
    const u: Ubo = { buf, size, slots: lay.slots.map((s) => ({ u: s.u, k: s.k, off: s.off, data: new Float32Array(s.size / 4), cache: undefined })) }
    this.ubos.set(g, u)
    g.addEventListener('dispose', this.onDispose)
    this.stats.groups++
    this.stats.created++
    return u
  }

  private upload(u: Ubo): void {
    const gl = this.gl
    let bound = false
    for (let i = 0; i < u.slots.length; i++) {
      const s = u.slots[i]
      const v0 = s.u.value
      const v = s.k >= 0 ? (v0 as unknown[])[s.k] : v0
      if (!changed(s, v)) continue
      write(v, s.data)
      if (!bound) {
        gl.bindBuffer(UNIFORM_BUFFER, u.buf)
        bound = true
      }
      gl.bufferSubData(UNIFORM_BUFFER, s.off, s.data)
    }
    if (bound) gl.bindBuffer(UNIFORM_BUFFER, null)
  }

  private release(g: GroupLike): void {
    g.removeEventListener('dispose', this.onDispose)
    const u = this.ubos.get(g)
    if (!u) return
    this.gl.deleteBuffer(u.buf)
    this.ubos.delete(g)
    this.stats.groups--
    this.stats.deleted++
  }
}
