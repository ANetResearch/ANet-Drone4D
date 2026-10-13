// WebGL2 limit emulation shared by the WP-13b specs (M16; ADR-086, ADR-087; the rules of perf/m06/gpu-limits.spec.ts, M06):
// getParameter reports the limits, bindings past MAX_UNIFORM_BUFFER_BINDINGS are refused with a console error (a real driver
// raises GL_INVALID_VALUE), a program over a per-stage or combined block limit or the block size logs an error at its first use
// (a real link failure), and the mobile set adds a point-size ceiling of 1, MAX_TEXTURE_SIZE 4096 and no float colour-buffer
// extensions. Install with page.addInitScript(emulate, {L, renderer, mobile}) before any page script.
export interface Limits { bindings: number; vertex: number; fragment: number; combined: number; blockSize: number }
export interface MobileExtras { pointSize: number; maxTexture: number; noFloat: boolean }
export const MIN: Limits = { bindings: 24, vertex: 12, fragment: 12, combined: 24, blockSize: 16384 }
export const MOBILE: MobileExtras = { pointSize: 1, maxTexture: 4096, noFloat: true }
export const ADRENO = 'ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)'
export type Shell = 'desktop' | 'phone' | 'tablet'
export const VIEW: Record<Shell, { width: number; height: number; dpr: number; touch: boolean }> = {
  desktop: { width: 1600, height: 900, dpr: 1, touch: false },
  phone: { width: 393, height: 852, dpr: 3, touch: true },
  tablet: { width: 1194, height: 834, dpr: 2, touch: true },
}
/** strict emulation (same rules as perf/m06/gpu-limits.spec.ts): reported limits, refused bindings, link-time block checks */
export function emulate(o: { L: Limits; renderer: string | null; mobile: MobileExtras | null }): void {
  const P = WebGL2RenderingContext.prototype
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  const gp = own('getParameter')
  const L = o.L
  const mob = o.mobile
  const FLOAT = ['EXT_color_buffer_float', 'EXT_float_blend', 'OES_texture_float_linear']
  if (mob?.noFloat) {
    const ge = own('getExtension')
    const gse = own('getSupportedExtensions')
    P.getExtension = function (this: WebGL2RenderingContext, name: string) {
      return FLOAT.includes(name) ? null : (ge as (this: WebGL2RenderingContext, n: string) => unknown).call(this, name)
    } as typeof P.getExtension
    P.getSupportedExtensions = function (this: WebGL2RenderingContext) {
      return (gse.call(this) ?? []).filter((x) => !FLOAT.includes(x))
    } as typeof P.getSupportedExtensions
  }
  P.getParameter = function (this: WebGL2RenderingContext, n: number) {
    if (o.renderer !== null && n === 0x9246) return o.renderer
    if (mob && n === 0x846d) return new Float32Array([1, mob.pointSize])
    if (mob && n === 0x0d33) return mob.maxTexture
    if (n === 0x8a2f) return L.bindings
    if (n === 0x8a2b) return L.vertex
    if (n === 0x8a2d) return L.fragment
    if (n === 0x8a2e) return L.combined
    if (n === 0x8a30) return L.blockSize
    return gp.call(this, n)
  } as typeof P.getParameter
  const bbb = own('bindBufferBase')
  P.bindBufferBase = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null) {
    if (t === 0x8a11 && i >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferBase(UNIFORM_BUFFER, ${i})`); return }
    bbb.call(this, t, i, b)
  }
  const bbr = own('bindBufferRange')
  P.bindBufferRange = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null, off: number, size: number) {
    if (t === 0x8a11 && i >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferRange(UNIFORM_BUFFER, ${i})`); return }
    bbr.call(this, t, i, b, off, size)
  }
  const ubb = own('uniformBlockBinding')
  P.uniformBlockBinding = function (this: WebGL2RenderingContext, p: WebGLProgram, bi: number, point: number) {
    if (point >= L.bindings) { console.error(`[gpu-limits] GL_INVALID_VALUE: uniformBlockBinding(${bi}, ${point})`); return }
    ubb.call(this, p, bi, point)
  }
  const tex = own('texImage2D')
  if (mob) {
    P.texImage2D = function (this: WebGL2RenderingContext, ...a: unknown[]) {
      const w = typeof a[3] === 'number' ? a[3] : 0
      const h = typeof a[4] === 'number' ? a[4] : 0
      if ((w as number) > mob.maxTexture || (h as number) > mob.maxTexture) console.error(`[gpu-limits] GL_INVALID_VALUE: texImage2D ${w} x ${h} > MAX_TEXTURE_SIZE`)
      return (tex as (...x: unknown[]) => void).apply(this, a)
    } as typeof P.texImage2D
  }
  const stats = { programs: 0, maxV: 0, maxF: 0, maxBlockSize: 0 }
  ;(window as unknown as { __gpuLimits: unknown }).__gpuLimits = stats
  const checked = new WeakSet<WebGLProgram>()
  const up = own('useProgram')
  P.useProgram = function (this: WebGL2RenderingContext, p: WebGLProgram | null) {
    if (p && !checked.has(p)) {
      checked.add(p)
      if (this.getProgramParameter(p, this.LINK_STATUS)) {
        const n = this.getProgramParameter(p, this.ACTIVE_UNIFORM_BLOCKS) as number
        let v = 0
        let f = 0
        let size = 0
        for (let i = 0; i < n; i++) {
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_VERTEX_SHADER)) v++
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_FRAGMENT_SHADER)) f++
          size = Math.max(size, this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_DATA_SIZE) as number)
        }
        stats.programs++
        stats.maxV = Math.max(stats.maxV, v)
        stats.maxF = Math.max(stats.maxF, f)
        stats.maxBlockSize = Math.max(stats.maxBlockSize, size)
        if (v > L.vertex || f > L.fragment || v + f > L.combined) console.error(`[gpu-limits] GL_INVALID_OPERATION: program links ${v} / ${f} uniform blocks`)
        if (size > L.blockSize) console.error(`[gpu-limits] GL_INVALID_OPERATION: ${size} B uniform block`)
      }
    }
    up.call(this, p)
  }
}

