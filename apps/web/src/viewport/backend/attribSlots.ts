// SwiftShader vertex-input slot normalisation (P4-WEB, ADR-071 item 1; D1-AC-03b, 04, 09a, 19, 25, 29). Owner: M06.
// ANGLE on SwiftShader (Chrome 151, Tier S) sets each draw's vertex input with vkCmdSetVertexInputEXT for the program's
// active attribute locations only. SwiftShader keeps the formats of every other location from earlier draws of the same
// submission (CmdSetVertexInput only overwrites the locations it lists) and keys its JIT vertex routine on all 32 of them
// (VertexProcessor::State::input[].format). A program's routine therefore depended on the draws recorded before it: when
// a layer appeared or hid, or ANGLE submitted mid-frame, the same program was compiled again on SwiftShader's queue
// thread while the frame waited (Subzero; 236 KB and 0.3-0.6 s for the point-cloud program). That is the "WebGL-side
// spike" of ACC-3 4.2: no new GL state, buffer or texture after the reveal, one GPU-process thread busy for the whole
// spike, a new swiftshader_jit mapping each time (P4-WEB report §2; a Vulkan layer logged the slot sets per draw).
// Remedy, two parts:
//   1. before every draw call one point of a 16-attribute program is drawn with rasterizer discard (no setup or pixel
//      routine, no fragment), alternating between two vertex arrays so ANGLE always re-emits its vertex input: the
//      locations a real draw does not use then always hold the same formats (variant A);
//   2. ANGLE still submits now and then between a reset and the following draw (about once a second in the S1 full
//      scene, for varying programs), so that draw opens a fresh submission with all other slots undefined (variant B).
//      bare() issues every draw of a pass that way; the shader zoo renders each target once more through it, so both
//      variants of every program are compiled under the boot mask.
// The program and vertex array that three.js caches are rebound right after a reset, so its state cache stays valid
// (three's classic renderer never enables RASTERIZER_DISCARD). SwiftShader only: on a GPU the extra draw is pure cost.
type DrawName = 'drawArrays' | 'drawElements' | 'drawArraysInstanced' | 'drawElementsInstanced' | 'drawRangeElements'
const DRAWS: readonly DrawName[] = ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced', 'drawRangeElements']
/** WebGL 2 on ANGLE exposes 16 vertex attributes; SwiftShader's slots 16-31 are never set and stay undefined */
export const SLOT_COUNT = 16

function source(n: number): { vs: string; fs: string } {
  const ins: string[] = []
  const sum: string[] = []
  for (let i = 0; i < n; i++) {
    ins.push(`layout(location = ${i}) in vec4 a${i};`)
    sum.push(`a${i}`)
  }
  // every attribute is read (an unused one would be optimised out of the program and its slot left stale)
  const vs = `#version 300 es\n${ins.join('\n')}\nvoid main() {\n  vec4 s = ${sum.join(' + ')};\n  gl_Position = vec4(2.0 + 0.0 * s.x, 2.0, 2.0, 1.0);\n  gl_PointSize = 1.0;\n}\n`
  const fs = '#version 300 es\nprecision mediump float;\nout vec4 o;\nvoid main() { o = vec4(0.0); }\n'
  return { vs, fs }
}

export interface AttribSlotReset {
  /** reset draws issued (tests, diagnostics) */
  readonly count: number
  /**
   * one more reset after the frame's last draw: when ANGLE does not submit at the frame end, the next frame's draws of
   * the same submission start from the reset's slot formats too, not from those of whichever layer drew last
   */
  tail(): void
  /**
   * run fn with every draw issued as the first draw of a fresh submission (a 1-pixel blit and a flush before it, no
   * reset): the second vertex-routine variant of each program (own attributes, all other slots undefined), which ANGLE
   * produces whenever it submits between a reset and the following draw (about once a second, P4-WEB report §2.5). The
   * shader zoo runs this under the boot mask so that variant is compiled there too.
   */
  bare(fn: () => void): void
  dispose(): void
}

/** patch the draw calls of `gl` (own properties shadowing the prototype); returns null when the program fails to link */
export function installAttribSlotReset(gl: WebGL2RenderingContext, slots = SLOT_COUNT): AttribSlotReset | null {
  const n = Math.max(1, Math.min(slots, gl.getParameter(gl.MAX_VERTEX_ATTRIBS) as number))
  const { vs, fs } = source(n)
  const sh = (type: number, src: string): WebGLShader | null => {
    const s = gl.createShader(type)
    if (!s) return null
    gl.shaderSource(s, src)
    gl.compileShader(s)
    return s
  }
  const v = sh(gl.VERTEX_SHADER, vs)
  const f = sh(gl.FRAGMENT_SHADER, fs)
  const prog = gl.createProgram()
  if (!v || !f || !prog) return null
  gl.attachShader(prog, v)
  gl.attachShader(prog, f)
  gl.linkProgram(prog)
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.warn('vertex-input reset program failed to link', gl.getProgramInfoLog(prog))
    gl.deleteProgram(prog)
    return null
  }
  const proto = Object.getPrototypeOf(gl) as WebGL2RenderingContext
  // current program and vertex array as three.js set them (tracked through instance patches: no getParameter per draw)
  const cur = {
    prog: gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram | null,
    vao: gl.getParameter(gl.VERTEX_ARRAY_BINDING) as WebGLVertexArrayObject | null,
  }
  const prevBuf = gl.getParameter(gl.ARRAY_BUFFER_BINDING) as WebGLBuffer | null
  const buf = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.bufferData(gl.ARRAY_BUFFER, 16 * n, gl.STATIC_DRAW)
  // two vertex arrays with the same 16 vec4 attributes, used alternately: ANGLE re-emits a draw's vertex input only when
  // the program or the vertex array binding changed, so a reset that follows a reset (the tail, then the next frame's
  // first draw) must still bind a different array, or it runs on the slots left in a fresh submission (P4-WEB report §2.4)
  const vaos = [gl.createVertexArray(), gl.createVertexArray()]
  for (const vao of vaos) {
    proto.bindVertexArray.call(gl, vao)
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    for (let i = 0; i < n; i++) {
      gl.enableVertexAttribArray(i)
      gl.vertexAttribPointer(i, 4, gl.FLOAT, false, 0, 0)
    }
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, prevBuf)
  proto.bindVertexArray.call(gl, cur.vao)
  let flip = 0
  const st = { count: 0 }
  const own = gl as unknown as Record<string, unknown>
  own.useProgram = (p: WebGLProgram | null): void => {
    cur.prog = p
    proto.useProgram.call(gl, p)
  }
  own.bindVertexArray = (a: WebGLVertexArrayObject | null): void => {
    cur.vao = a
    proto.bindVertexArray.call(gl, a)
  }
  const reset = (): void => {
    proto.useProgram.call(gl, prog)
    proto.bindVertexArray.call(gl, vaos[flip])
    flip ^= 1
    gl.enable(gl.RASTERIZER_DISCARD)
    proto.drawArrays.call(gl, gl.POINTS, 0, 1)
    gl.disable(gl.RASTERIZER_DISCARD)
    proto.useProgram.call(gl, cur.prog)
    proto.bindVertexArray.call(gl, cur.vao)
    st.count++
  }
  let bareMode = false
  // a fresh submission without a CPU wait: a 1-pixel blit from the bound framebuffer into a private one is recorded
  // outside the render pass (ANGLE ends the pass), after which gl.flush() is not deferred and submits; the next draw
  // opens a new render pass in a new submission (Chrome's WebGL flush()/finish() alone are deferred while a pass is open)
  const fbo = gl.createFramebuffer()
  const rbo = gl.createRenderbuffer()
  const prevRb = gl.getParameter(gl.RENDERBUFFER_BINDING) as WebGLRenderbuffer | null
  gl.bindRenderbuffer(gl.RENDERBUFFER, rbo)
  gl.renderbufferStorage(gl.RENDERBUFFER, gl.RGBA8, 1, 1)
  gl.bindRenderbuffer(gl.RENDERBUFFER, prevRb)
  const prevDraw = gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null
  gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, fbo)
  gl.framebufferRenderbuffer(gl.DRAW_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rbo)
  gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, prevDraw)
  const freshSubmission = (): void => {
    const draw = gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null
    const read = gl.getParameter(gl.READ_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null
    const sc = gl.isEnabled(gl.SCISSOR_TEST)
    if (sc) gl.disable(gl.SCISSOR_TEST)
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, draw)
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, fbo)
    gl.blitFramebuffer(0, 0, 1, 1, 0, 0, 1, 1, gl.COLOR_BUFFER_BIT, gl.NEAREST)
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, draw)
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, read)
    if (sc) gl.enable(gl.SCISSOR_TEST)
    gl.flush()
  }
  for (const name of DRAWS) {
    const orig = proto[name] as (...a: unknown[]) => void
    own[name] = (...a: unknown[]): void => {
      if (bareMode) freshSubmission()
      else reset()
      orig.apply(gl, a)
    }
  }
  // the first reset draw compiles its own vertex routine now (under the boot mask)
  reset()
  return {
    get count() {
      return st.count
    },
    tail: reset,
    bare(fn: () => void): void {
      bareMode = true
      try {
        fn()
      } finally {
        bareMode = false
      }
      gl.flush()
    },
    dispose(): void {
      for (const name of [...DRAWS, 'useProgram', 'bindVertexArray']) delete own[name]
      for (const vao of vaos) gl.deleteVertexArray(vao)
      gl.deleteBuffer(buf)
      gl.deleteFramebuffer(fbo)
      gl.deleteRenderbuffer(rbo)
      gl.deleteProgram(prog)
      gl.deleteShader(v)
      gl.deleteShader(f)
    },
  }
}
