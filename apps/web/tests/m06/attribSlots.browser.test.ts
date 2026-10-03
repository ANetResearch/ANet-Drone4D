// SwiftShader vertex-input slot normalisation (P4-WEB, ADR-071 item 1): every draw is preceded by one 16-attribute
// rasterizer-discard draw, three's program and vertex array stay bound as it set them, bare() issues draws without
// the reset (each after a flush), and dispose() restores the prototype methods.
import { describe, expect, it } from 'vitest'
import { installAttribSlotReset, SLOT_COUNT } from '@/viewport/backend/attribSlots'

function program(gl: WebGL2RenderingContext): WebGLProgram {
  const vs = '#version 300 es\nlayout(location = 0) in vec3 position;\nvoid main() { gl_Position = vec4(position, 1.0); gl_PointSize = 4.0; }\n'
  const fs = '#version 300 es\nprecision mediump float;\nout vec4 o;\nvoid main() { o = vec4(1.0, 0.5, 0.0, 1.0); }\n'
  const p = gl.createProgram()!
  for (const [t, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]] as const) {
    const s = gl.createShader(t)!
    gl.shaderSource(s, src)
    gl.compileShader(s)
    gl.attachShader(p, s)
  }
  gl.linkProgram(p)
  return p
}

describe('attribSlots (ADR-071 item 1)', () => {
  it('draws a reset before every draw, keeps the program and vertex array, and renders the real draw unchanged', () => {
    const canvas = document.createElement('canvas')
    canvas.width = 8
    canvas.height = 8
    const gl = canvas.getContext('webgl2', { antialias: false, preserveDrawingBuffer: true })!
    const slots = installAttribSlotReset(gl)
    expect(slots).not.toBeNull()
    const p = program(gl)
    const vao = gl.createVertexArray()
    gl.bindVertexArray(vao)
    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 0]), gl.STATIC_DRAW)
    gl.enableVertexAttribArray(0)
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0)
    gl.useProgram(p)
    const n0 = slots!.count
    gl.clearColor(0, 0, 0, 1)
    gl.clear(gl.COLOR_BUFFER_BIT)
    gl.drawArrays(gl.POINTS, 0, 1)
    expect(slots!.count).toBe(n0 + 1)
    expect(gl.getParameter(gl.CURRENT_PROGRAM)).toBe(p)
    expect(gl.getParameter(gl.VERTEX_ARRAY_BINDING)).toBe(vao)
    expect(gl.isEnabled(gl.RASTERIZER_DISCARD)).toBe(false)
    expect(gl.getError()).toBe(gl.NO_ERROR)
    // the real point is drawn (the reset draw produced no fragment)
    const px = new Uint8Array(4)
    gl.readPixels(4, 4, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px)
    expect(px[0]).toBeGreaterThan(200)
    expect(px[1]).toBeGreaterThan(100)
    // bare(): no reset, the draw still happens
    const n1 = slots!.count
    slots!.bare(() => gl.drawArrays(gl.POINTS, 0, 1))
    expect(slots!.count).toBe(n1)
    expect(gl.getError()).toBe(gl.NO_ERROR)
    // tail(): one reset, bindings kept
    slots!.tail()
    expect(slots!.count).toBe(n1 + 1)
    expect(gl.getParameter(gl.CURRENT_PROGRAM)).toBe(p)
    slots!.dispose()
    expect(Object.prototype.hasOwnProperty.call(gl, 'drawArrays')).toBe(false)
    expect(SLOT_COUNT).toBe(16)
  })
})
