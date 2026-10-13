// Voxel backdrop of the public demo landing page (D2.2-L1). Loaded on idle by LandingVoxel (its own lazy chunk: the first
// screen chunks stay {index, landing, react, rolldown-runtime}, ADR-083), never three.js.
// Scene: a procedural voxel city block on a cell grid that grows outward from the centre, a scan band (red) that sweeps
// the block and leaves brighter seams behind it, and a seven-drone V formation with two drones orbiting the central
// tower. Scroll position drives growth, scan, camera azimuth and zoom; time adds only slow drift.
// Renderers: WebGL2 with one instanced draw call per frame (36-vertex cube, per-instance cell data, plain uniforms only:
// no uniform blocks, textures or float targets, so the WebGL2 specification minimum limits suffice, ADR-086/087);
// without WebGL2 a Canvas2D isometric renderer on a smaller grid; without either the CSS grid of the backdrop stays.
// Budget: desktop 40 x 40 cells at up to 60 fps; tablet 30 x 30 and phone 22 x 22 cells at 30 fps; device pixel ratio
// capped (2, phone 1.5) and the backing store capped at 4 Mpx. The loop pauses when the page is hidden, when the canvas
// leaves the viewport and after 9 s without scrolling or pointer input; prefers-reduced-motion renders one still frame.
// Colours are read from the theme tokens at start (no colour literals). Test hooks: data-voxel-mode (webgl2, canvas2d,
// static), data-voxel-state (running, idle, paused, still) and the read-only element property `voxelFrames`.

export type VoxelMode = 'webgl2' | 'canvas2d' | 'static'
export interface VoxelHandle { stop(): void }

type Rgb = [number, number, number]
interface Profile { grid: number; fps: number; dprCap: number; aa: boolean; alphaTop: number; alphaBody: number }
interface Palette { ground: Rgb; face: Rgb; top: Rgb; line: Rgb; line2: Rgb; accent: Rgb }
interface City { n: number; inst: Float32Array; count: number; tower: [number, number, number]; heights: Float32Array }
interface Scene { p: number; t: number; level: number; alpha: number }
interface Renderer {
  mode: Exclude<VoxelMode, 'static'>
  /** a software rasteriser (SwiftShader, llvmpipe): the loop drops to 20 fps at device pixel ratio 1 */
  software: boolean
  resize(w: number, h: number, dpr: number): void
  draw(s: Scene): void
  dispose(): void
}

const IDLE_MS = 9000
const INTRO_S = 2.2
const MAX_PIXELS = 4_000_000
const FORMATION = 7
const ORBIT = 2

function profile(): Profile {
  const w = globalThis.innerWidth || 1280
  const coarse = globalThis.matchMedia?.('(pointer: coarse)').matches ?? false
  if (w < 768) return { grid: 22, fps: 30, dprCap: 1.5, aa: false, alphaTop: 0.6, alphaBody: 0.3 }
  if (coarse || w < 1280) return { grid: 30, fps: 30, dprCap: 2, aa: false, alphaTop: 0.62, alphaBody: 0.32 }
  return { grid: 40, fps: 60, dprCap: 2, aa: true, alphaTop: 0.9, alphaBody: 0.36 }
}

// ---------------------------------------------------------------- palette (theme tokens to linear 0..1 sRGB triples)
function palette(): Palette {
  const probe = document.createElement('canvas')
  probe.width = 1
  probe.height = 1
  const x = probe.getContext('2d', { willReadFrequently: true })
  const css = getComputedStyle(document.documentElement)
  const read = (name: string, fb: Rgb): Rgb => {
    const v = css.getPropertyValue(name).trim()
    if (!x || !v) return fb
    x.clearRect(0, 0, 1, 1)
    x.fillStyle = v
    x.fillRect(0, 0, 1, 1)
    const d = x.getImageData(0, 0, 1, 1).data
    return [d[0] / 255, d[1] / 255, d[2] / 255]
  }
  return {
    ground: read('--g900', [0.067, 0.071, 0.078]),
    face: read('--g850', [0.086, 0.094, 0.106]),
    top: read('--g800', [0.114, 0.122, 0.137]),
    line: read('--g700', [0.165, 0.176, 0.196]),
    line2: read('--g500', [0.361, 0.38, 0.416]),
    accent: read('--r500', [0.914, 0.188, 0.141]),
  }
}

// ---------------------------------------------------------------- city (deterministic)
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** blocks of 6 x 6 cells between one-cell streets; 2 x 2 lots share a height; taller towards the centre; one tower */
function makeCity(n: number): City {
  const r = rng(20261013)
  const c = n / 2
  // heights scale with the grid so that every profile shows the same skyline proportions
  const hs = Math.min(1, Math.max(0.55, n / 40))
  const towerH = Math.round(18 * hs)
  const heights = new Float32Array(n * n)
  const lot = new Map<number, number>()
  let tower: [number, number, number] = [c, c, 0]
  let best = Infinity
  for (let z = 0; z < n; z++) {
    for (let x = 0; x < n; x++) {
      if (x % 7 === 0 || z % 7 === 0) continue
      const key = (x >> 1) * 4096 + (z >> 1)
      let h = lot.get(key)
      if (h === undefined) {
        const d = Math.min(1, Math.hypot(x + 0.5 - c, z + 0.5 - c) / c)
        const base = (1 - d) ** 1.3
        h = r() < 0.07 + d * 0.12 ? 0 : 1 + Math.floor((r() ** 1.7 * 10 * base + base * 3) * hs)
        lot.set(key, h)
      }
      heights[z * n + x] = h
      const dc = Math.hypot(x + 0.5 - c, z + 0.5 - c)
      if (h > 0 && dc < best) {
        best = dc
        tower = [x, z, 0]
      }
    }
  }
  // the central tower: one lot raised to towerH voxels (18 on the desktop grid)
  const tx = tower[0] & ~1
  const tz = tower[1] & ~1
  for (const [dx, dz] of [[0, 0], [1, 0], [0, 1], [1, 1]]) {
    const x = tx + dx
    const z = tz + dz
    if (x % 7 !== 0 && z % 7 !== 0) heights[z * n + x] = towerH
  }
  tower = [tx + 1, tz + 1, towerH]
  const count = n * n + FORMATION + ORBIT
  const inst = new Float32Array(count * 4)
  let k = 0
  for (let z = 0; z < n; z++) {
    for (let x = 0; x < n; x++) {
      const h = heights[z * n + x]
      inst.set([x, z, h, h > 0 ? 1 : 0], k)
      k += 4
    }
  }
  for (let i = 0; i < FORMATION; i++, k += 4) inst.set([i, 0, 0, 2], k)
  for (let i = 0; i < ORBIT; i++, k += 4) inst.set([i, 0, 0, 3], k)
  return { n, inst, count, tower, heights }
}

// ---------------------------------------------------------------- shared scene maths
const smooth = (a: number, b: number, x: number): number => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)))
  return t * t * (3 - 2 * t)
}
/** camera of a scene: azimuth, elevation, zoom factor (world half width per cell count) and the clip-space shift */
function camera(s: Scene, aspect: number): { az: number; el: number; k: number; sx: number; sy: number } {
  const wide = aspect >= 1.2
  const mid = !wide && aspect >= 0.8
  const k = (wide ? 1.22 : mid ? 0.95 : 0.66) * (1 - 0.16 * smooth(0, 1, s.p))
  return {
    az: 0.78 + s.p * 0.85 + s.t * 0.008,
    el: 0.62 - s.p * 0.08,
    k,
    sx: wide ? 0.34 : mid ? 0.12 : 0,
    sy: wide ? -0.1 : mid ? -0.14 : -0.36,
  }
}
/** scan band: x position (cells) and strength */
function scan(s: Scene, n: number): [number, number] {
  const c = n / 2
  const x = c + (smooth(0.12, 0.9, s.p) * 2 - 1) * c * 1.2 + Math.sin(s.t * 0.35) * 1.2
  return [x, smooth(0.06, 0.2, s.p) * (1 - smooth(0.93, 1, s.p))]
}

// ---------------------------------------------------------------- WebGL2
const VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_nrm;
layout(location = 2) in vec4 a_inst;
uniform mat4 u_vp;
uniform vec2 u_shift;
uniform float u_level;
uniform float u_p;
uniform float u_t;
uniform float u_n;
uniform vec3 u_tower;
out vec3 v_nrm;
out vec3 v_loc;
out vec3 v_world;
flat out float v_kind;
float hash(vec2 q) { return fract(sin(dot(q, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
  float kind = a_inst.w;
  float c = u_n * 0.5;
  vec3 org;
  vec3 size;
  if (kind < 1.5) {
    vec2 cell = a_inst.xy;
    float d = length(cell + 0.5 - c) / c;
    float g = clamp((u_level - d * 0.6 - hash(cell) * 0.12) / 0.3, 0.0, 1.0);
    float h = kind < 0.5 ? 0.04 : max(1.0, ceil(a_inst.z * (1.0 - (1.0 - g) * (1.0 - g)) - 0.001));
    size = vec3(1.0, h, 1.0) * step(0.0001, g);
    org = vec3(cell.x, 0.0, cell.y);
  } else {
    float i = a_inst.x;
    vec3 ctr;
    if (kind < 2.5) {
      float th = 6.2831853 * (u_p * 0.75 + u_t * 0.012) + 0.6;
      vec2 pth = vec2(c) + vec2(sin(th) * c * 0.6, sin(2.0 * th) * c * 0.32);
      vec2 fw = normalize(vec2(cos(th) * 0.6, cos(2.0 * th) * 0.64));
      vec2 sd = vec2(-fw.y, fw.x);
      float row = ceil(i * 0.5);
      float sg = i < 0.5 ? 0.0 : (mod(i, 2.0) < 0.5 ? 1.0 : -1.0);
      vec2 xz = pth - fw * row * 1.5 + sd * sg * row * 1.3;
      ctr = vec3(xz.x, u_tower.z + 3.0 + sin(u_t * 1.7 + i) * 0.15, xz.y);
      size = vec3(0.5, 0.22, 0.5) * smoothstep(0.7, 0.95, u_level);
    } else {
      float a = u_t * 0.45 + u_p * 5.0 + i * 3.14159;
      ctr = vec3(u_tower.x + cos(a) * 3.2, u_tower.z * 0.72, u_tower.y + sin(a) * 3.2);
      size = vec3(0.5, 0.22, 0.5) * smoothstep(0.35, 0.6, u_level);
    }
    org = ctr - size * 0.5;
  }
  vec3 wp = org + a_pos * size;
  v_world = wp;
  v_loc = a_pos * size;
  v_nrm = a_nrm;
  v_kind = kind;
  gl_Position = u_vp * vec4(wp, 1.0);
  gl_Position.xy += u_shift;
}`

const FS = `#version 300 es
precision highp float;
in vec3 v_nrm;
in vec3 v_loc;
in vec3 v_world;
flat in float v_kind;
uniform vec3 u_ground;
uniform vec3 u_face;
uniform vec3 u_top;
uniform vec3 u_line;
uniform vec3 u_line2;
uniform vec3 u_accent;
uniform vec2 u_scan;
uniform vec3 u_fade;
uniform float u_alpha;
uniform float u_lw;
out vec4 o;
float edges(vec3 loc, vec3 n) {
  vec2 uv = abs(n.y) > 0.5 ? loc.xz : (abs(n.x) > 0.5 ? loc.zy : loc.xy);
  vec2 w = max(fwidth(uv) * u_lw, vec2(0.0001));
  vec2 g = abs(fract(uv + 0.5) - 0.5);
  vec2 l = 1.0 - smoothstep(vec2(0.0), w, g);
  return max(l.x, l.y);
}
void main() {
  vec3 n = normalize(v_nrm);
  float lit = 0.62 + 0.38 * max(dot(n, normalize(vec3(0.45, 1.0, 0.25))), 0.0);
  float d = length(v_world.xz - u_fade.xy);
  float al = (1.0 - smoothstep(u_fade.z * 0.5, u_fade.z, d)) * u_alpha;
  if (v_kind > 1.5) {
    al = u_alpha * 0.85;
    o = vec4(mix(u_face, u_accent, 0.88) * (0.8 + 0.2 * lit) * al, al);
    return;
  }
  vec3 base = v_kind < 0.5 ? u_ground : (n.y > 0.5 ? u_top : u_face * lit);
  float band = u_scan.y * (1.0 - smoothstep(0.0, 1.6, abs(v_world.x - u_scan.x)));
  vec3 line = mix(u_line, u_line2, step(v_world.x, u_scan.x) * u_scan.y * 0.55);
  line = mix(line, u_accent, band);
  vec3 col = mix(base, line, edges(v_loc, n));
  col = mix(col, u_accent, band * 0.16);
  o = vec4(col * al, al);
}`

/** 36 vertices of the unit cube [0,1]^3: position and normal, counter-clockwise outside */
function cube(): Float32Array {
  const out: number[] = []
  const faces: [number[], number[][]][] = [
    [[1, 0, 0], [[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 1]]],
    [[-1, 0, 0], [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0]]],
    [[0, 1, 0], [[0, 1, 0], [0, 1, 1], [1, 1, 1], [1, 1, 0]]],
    [[0, -1, 0], [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]]],
    [[0, 0, 1], [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]]],
    [[0, 0, -1], [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]]],
  ]
  for (const [nrm, q] of faces) {
    // order the quad so that (b - a) x (c - a) points along the normal
    const [a, b, c] = q
    const e1 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]]
    const e2 = [c[0] - a[0], c[1] - a[1], c[2] - a[2]]
    const cr = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]]
    const quad = cr[0] * nrm[0] + cr[1] * nrm[1] + cr[2] * nrm[2] > 0 ? q : [q[0], q[3], q[2], q[1]]
    for (const i of [0, 1, 2, 0, 2, 3]) out.push(...quad[i], ...nrm)
  }
  return new Float32Array(out)
}

/** column-major orthographic view-projection of an orbit camera around `tgt` */
function viewProj(m: Float32Array, az: number, el: number, tgt: Rgb, hw: number, hh: number): void {
  const ce = Math.cos(el)
  const fx = ce * Math.sin(az)
  const fy = Math.sin(el)
  const fz = ce * Math.cos(az)
  const D = 160
  const ex = tgt[0] + fx * D
  const ey = tgt[1] + fy * D
  const ez = tgt[2] + fz * D
  const rl = Math.hypot(fz, fx)
  const rx = fz / rl
  const rz = -fx / rl
  const ux = fy * rz
  const uy = fz * rx - fx * rz
  const uz = -fy * rx
  const near = 1
  const far = 400
  const A = 2 / (far - near)
  const B = (far + near) / (far - near)
  m.set([
    rx / hw, ux / hh, -A * fx, 0,
    0, uy / hh, -A * fy, 0,
    rz / hw, uz / hh, -A * fz, 0,
    -(rx * ex + rz * ez) / hw, -(ux * ex + uy * ey + uz * ez) / hh, A * (fx * ex + fy * ey + fz * ez) - B, 1,
  ])
}

function createGl(canvas: HTMLCanvasElement, city: City, prof: Profile, pal: Palette): Renderer | null {
  let gl: WebGL2RenderingContext | null = null
  try {
    gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: prof.aa, depth: true, stencil: false,
      powerPreference: 'low-power', preserveDrawingBuffer: false, failIfMajorPerformanceCaveat: false })
  } catch {
    gl = null
  }
  if (!gl) return null
  const g = gl
  const shader = (type: number, src: string): WebGLShader | null => {
    const s = g.createShader(type)
    if (!s) return null
    g.shaderSource(s, src)
    g.compileShader(s)
    return g.getShaderParameter(s, g.COMPILE_STATUS) ? s : null
  }
  const vs = shader(g.VERTEX_SHADER, VS)
  const fs = shader(g.FRAGMENT_SHADER, FS)
  const prog = g.createProgram()
  if (!vs || !fs || !prog) return null
  g.attachShader(prog, vs)
  g.attachShader(prog, fs)
  g.linkProgram(prog)
  if (!g.getProgramParameter(prog, g.LINK_STATUS)) return null
  const vao = g.createVertexArray()
  const geo = g.createBuffer()
  const ins = g.createBuffer()
  g.bindVertexArray(vao)
  g.bindBuffer(g.ARRAY_BUFFER, geo)
  g.bufferData(g.ARRAY_BUFFER, cube(), g.STATIC_DRAW)
  g.enableVertexAttribArray(0)
  g.vertexAttribPointer(0, 3, g.FLOAT, false, 24, 0)
  g.enableVertexAttribArray(1)
  g.vertexAttribPointer(1, 3, g.FLOAT, false, 24, 12)
  g.bindBuffer(g.ARRAY_BUFFER, ins)
  g.bufferData(g.ARRAY_BUFFER, city.inst, g.STATIC_DRAW)
  g.enableVertexAttribArray(2)
  g.vertexAttribPointer(2, 4, g.FLOAT, false, 16, 0)
  g.vertexAttribDivisor(2, 1)
  g.bindVertexArray(null)
  const U = (name: string): WebGLUniformLocation | null => g.getUniformLocation(prog, name)
  const u = {
    vp: U('u_vp'), shift: U('u_shift'), level: U('u_level'), p: U('u_p'), t: U('u_t'), n: U('u_n'), tower: U('u_tower'),
    ground: U('u_ground'), face: U('u_face'), top: U('u_top'), line: U('u_line'), line2: U('u_line2'), accent: U('u_accent'),
    scan: U('u_scan'), fade: U('u_fade'), alpha: U('u_alpha'), lw: U('u_lw'),
  }
  g.useProgram(prog)
  g.uniform3fv(u.ground, pal.ground)
  g.uniform3fv(u.face, pal.face)
  g.uniform3fv(u.top, pal.top)
  g.uniform3fv(u.line, pal.line)
  g.uniform3fv(u.line2, pal.line2)
  g.uniform3fv(u.accent, pal.accent)
  g.uniform1f(u.n, city.n)
  g.uniform3f(u.tower, city.tower[0], city.tower[1], city.tower[2])
  g.uniform3f(u.fade, city.n / 2, city.n / 2, city.n * 0.72)
  g.enable(g.DEPTH_TEST)
  g.enable(g.CULL_FACE)
  g.clearColor(0, 0, 0, 0)
  const vp = new Float32Array(16)
  let W = 1
  let H = 1
  // RENDERER is unmasked in Firefox (which warns on the debug extension); Chromium masks it as "WebKit WebGL"
  let renderer = String(g.getParameter(g.RENDERER) ?? '')
  if (/^webkit webgl$/i.test(renderer)) {
    const info = g.getExtension('WEBGL_debug_renderer_info')
    if (info) renderer = String(g.getParameter(info.UNMASKED_RENDERER_WEBGL) ?? '')
  }
  return {
    mode: 'webgl2',
    software: /swiftshader|llvmpipe|softpipe|software|basic render/i.test(renderer),
    resize(w, h, dpr) {
      W = w
      H = h
      g.viewport(0, 0, w, h)
      g.useProgram(prog)
      g.uniform1f(u.lw, Math.max(1, dpr * 0.75))
    },
    draw(s) {
      const cam = camera(s, W / H)
      const hw = city.n * cam.k
      viewProj(vp, cam.az, cam.el, [city.n / 2, 4, city.n / 2], hw, hw / (W / H))
      const [sx, ss] = scan(s, city.n)
      g.useProgram(prog)
      g.uniformMatrix4fv(u.vp, false, vp)
      g.uniform2f(u.shift, cam.sx, cam.sy)
      g.uniform1f(u.level, s.level)
      g.uniform1f(u.p, s.p)
      g.uniform1f(u.t, s.t)
      g.uniform2f(u.scan, sx, ss)
      g.uniform1f(u.alpha, s.alpha)
      g.clear(g.COLOR_BUFFER_BIT | g.DEPTH_BUFFER_BIT)
      g.bindVertexArray(vao)
      g.drawArraysInstanced(g.TRIANGLES, 0, 36, city.count)
      g.bindVertexArray(null)
    },
    dispose() {
      g.deleteBuffer(geo)
      g.deleteBuffer(ins)
      g.deleteVertexArray(vao)
      g.deleteProgram(prog)
      g.deleteShader(vs)
      g.deleteShader(fs)
    },
  }
}

// ---------------------------------------------------------------- Canvas2D fallback (isometric, painter's order)
function createCanvas2d(canvas: HTMLCanvasElement, prof: Profile, pal: Palette): Renderer | null {
  let x: CanvasRenderingContext2D | null = null
  try {
    x = canvas.getContext('2d')
  } catch {
    x = null
  }
  if (!x) return null
  const ctx = x
  const city = makeCity(Math.min(18, Math.round(prof.grid * 0.55)))
  const n = city.n
  const c = n / 2
  const rgba = (k: Rgb, a: number, f = 1): string =>
    `rgba(${Math.round(k[0] * f * 255)},${Math.round(k[1] * f * 255)},${Math.round(k[2] * f * 255)},${a.toFixed(3)})`
  const hash = (i: number, j: number): number => {
    const v = Math.sin(i * 12.9898 + j * 78.233) * 43758.5453
    return v - Math.floor(v)
  }
  let W = 1
  let H = 1
  let D = 1
  return {
    mode: 'canvas2d',
    software: false,
    resize(w, h, dpr) {
      W = w
      H = h
      D = dpr
    },
    draw(s) {
      const cam = camera(s, W / H)
      const hw = n * cam.k
      const sc = W / 2 / hw
      const ox = W / 2 + cam.sx * W / 2
      const oy = H / 2 - cam.sy * H / 2
      const az = Math.PI / 4
      const el = cam.el
      const ca = Math.cos(az)
      const sa = Math.sin(az)
      const se = Math.sin(el)
      const ce = Math.cos(el)
      const P = (px: number, py: number, pz: number): [number, number] => {
        const dx = px - c
        const dz = pz - c
        return [ox + (dx * ca - dz * sa) * sc, oy + ((dx * sa + dz * ca) * se - (py - 4) * ce) * sc]
      }
      const [scx, ss] = scan(s, n)
      const alpha = s.alpha
      ctx.clearRect(0, 0, W, H)
      ctx.lineWidth = Math.max(1, D * 0.75)
      const poly = (pts: [number, number][], fill: string, stroke: string): void => {
        ctx.beginPath()
        ctx.moveTo(pts[0][0], pts[0][1])
        for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1])
        ctx.closePath()
        ctx.fillStyle = fill
        ctx.fill()
        ctx.strokeStyle = stroke
        ctx.stroke()
      }
      // camera at +x +z: draw by increasing x + z
      for (let sum = 0; sum <= 2 * (n - 1); sum++) {
        for (let i = Math.max(0, sum - n + 1); i <= Math.min(n - 1, sum); i++) {
          const j = sum - i
          const d = Math.hypot(i + 0.5 - c, j + 0.5 - c) / c
          const gr = Math.min(1, Math.max(0, (s.level - d * 0.6 - hash(i, j) * 0.12) / 0.3))
          if (gr <= 0) continue
          const fade = (1 - smooth(0.5, 1, Math.hypot(i + 0.5 - c, j + 0.5 - c) / (n * 0.72))) * alpha
          if (fade <= 0.01) continue
          const h0 = city.heights[j * n + i]
          const h = h0 > 0 ? Math.max(1, Math.ceil(h0 * (1 - (1 - gr) * (1 - gr)) - 0.001)) : 0.04
          const band = ss * (1 - smooth(0, 1.6, Math.abs(i + 0.5 - scx)))
          const lineK = band > 0.05 ? pal.accent : i + 0.5 < scx && ss > 0 ? pal.line2 : pal.line
          const stroke = rgba(lineK, fade * (band > 0.05 ? 0.9 : 0.8))
          const top: [number, number][] = [P(i, h, j), P(i + 1, h, j), P(i + 1, h, j + 1), P(i, h, j + 1)]
          if (h0 > 0) {
            poly([P(i + 1, 0, j), P(i + 1, h, j), P(i + 1, h, j + 1), P(i + 1, 0, j + 1)], rgba(pal.face, fade, 1.1), stroke)
            poly([P(i, 0, j + 1), P(i + 1, 0, j + 1), P(i + 1, h, j + 1), P(i, h, j + 1)], rgba(pal.face, fade, 0.85), stroke)
            poly(top, rgba(pal.top, fade), stroke)
          } else poly(top, rgba(pal.ground, fade), stroke)
        }
      }
      // drones: the formation and the two orbiting drones (same paths as the WebGL2 shader)
      const showF = smooth(0.7, 0.95, s.level)
      const showO = smooth(0.35, 0.6, s.level)
      if (showO <= 0) return
      ctx.fillStyle = rgba(pal.accent, alpha * 0.85)
      const th = 2 * Math.PI * (s.p * 0.75 + s.t * 0.012) + 0.6
      const fwx0 = Math.cos(th) * 0.6
      const fwz0 = Math.cos(2 * th) * 0.64
      const fl = Math.hypot(fwx0, fwz0) || 1
      const fwx = fwx0 / fl
      const fwz = fwz0 / fl
      const px = c + Math.sin(th) * c * 0.6
      const pz = c + Math.sin(2 * th) * c * 0.32
      const dot = (wx: number, wy: number, wz: number, show: number): void => {
        const sz = 0.5 * show * sc
        if (sz <= 0) return
        const [qx, qy] = P(wx, wy, wz)
        ctx.fillRect(qx - sz / 2, qy - sz / 3, sz, sz * 0.66)
      }
      for (let i = 0; i < FORMATION; i++) {
        const row = Math.ceil(i * 0.5)
        const sg = i === 0 ? 0 : i % 2 === 0 ? 1 : -1
        dot(px - fwx * row * 1.5 - fwz * sg * row * 1.3, city.tower[2] + 3 + Math.sin(s.t * 1.7 + i) * 0.15, pz - fwz * row * 1.5 + fwx * sg * row * 1.3, showF)
      }
      for (let i = 0; i < ORBIT; i++) {
        const a = s.t * 0.45 + s.p * 5 + i * Math.PI
        dot(city.tower[0] + Math.cos(a) * 3.2, city.tower[2] * 0.72, city.tower[1] + Math.sin(a) * 3.2, showO)
      }
    },
    dispose() {
      ctx.clearRect(0, 0, W, H)
    },
  }
}

// ---------------------------------------------------------------- loop
/** start the backdrop on `canvas`; returns the stop function (removes every listener and frees the GL resources) */
export function startVoxelField(canvas: HTMLCanvasElement): VoxelHandle {
  const prof = { ...profile() }
  const pal = palette()
  let frames = 0
  Object.defineProperty(canvas, 'voxelFrames', { configurable: true, get: () => frames })
  const city = makeCity(prof.grid)
  let r: Renderer | null = createGl(canvas, city, prof, pal) ?? createCanvas2d(canvas, prof, pal)
  if (!r) {
    canvas.dataset.voxelMode = 'static'
    canvas.dataset.voxelState = 'still'
    return { stop() {} }
  }
  canvas.dataset.voxelMode = r.mode
  if (r.software) {
    prof.fps = 20
    prof.dprCap = 1
  }
  const reduceMq = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  const frameMs = 1000 / prof.fps
  let reduced = reduceMq?.matches ?? false
  let raf = 0
  let last = 0
  let clock = 0
  let target = 0
  let p = 0
  let lastInput = performance.now()
  let onScreen = true
  let lost = false
  const setState = (st: string): void => {
    if (canvas.dataset.voxelState !== st) canvas.dataset.voxelState = st
  }
  const progress = (): number => {
    const el = document.scrollingElement ?? document.documentElement
    const max = el.scrollHeight - globalThis.innerHeight
    return max > 0 ? Math.min(1, Math.max(0, el.scrollTop / max)) : 0
  }
  const scene = (): Scene => {
    if (reduced) return { p: 0.25, t: 0, level: 1.3, alpha: (prof.alphaTop + prof.alphaBody) / 2 }
    const intro = 1 - (1 - Math.min(1, clock / INTRO_S)) ** 3
    const alpha = prof.alphaTop + (prof.alphaBody - prof.alphaTop) * smooth(0.03, 0.22, p)
    return { p, t: clock, level: Math.min(1.3, intro * 0.62 + p * 2.4), alpha }
  }
  const paint = (): void => {
    if (!r || lost) return
    r.draw(scene())
    frames++
  }
  const size = (): void => {
    if (!r) return
    const w = canvas.clientWidth
    const h = canvas.clientHeight
    if (w === 0 || h === 0) return
    let dpr = Math.min(prof.dprCap, globalThis.devicePixelRatio || 1)
    if (w * h * dpr * dpr > MAX_PIXELS) dpr = Math.sqrt(MAX_PIXELS / (w * h))
    const bw = Math.max(1, Math.round(w * dpr))
    const bh = Math.max(1, Math.round(h * dpr))
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw
      canvas.height = bh
    }
    r.resize(bw, bh, dpr)
  }
  const active = (): boolean => !reduced && !lost && onScreen && document.visibilityState === 'visible'
  const tick = (now: number): void => {
    raf = 0
    if (!active()) {
      setState(reduced ? 'still' : 'paused')
      return
    }
    raf = requestAnimationFrame(tick)
    if (last !== 0 && now - last < frameMs - 2) return
    const dt = last === 0 ? 0 : Math.min(0.1, (now - last) / 1000)
    last = now
    clock += dt
    p += (target - p) * (1 - Math.exp(-dt * 5))
    paint()
    if (now - lastInput > IDLE_MS && clock > INTRO_S && Math.abs(target - p) < 0.0005) {
      cancelAnimationFrame(raf)
      raf = 0
      last = 0
      setState('idle')
    }
  }
  const wake = (): void => {
    lastInput = performance.now()
    if (raf === 0 && active()) {
      last = 0
      setState('running')
      raf = requestAnimationFrame(tick)
    }
  }
  const onScroll = (): void => {
    target = progress()
    wake()
  }
  const onResize = (): void => {
    size()
    if (reduced) paint()
    else wake()
  }
  const onVisibility = (): void => {
    if (document.visibilityState === 'visible') wake()
  }
  const onReduce = (): void => {
    reduced = reduceMq?.matches ?? false
    if (reduced) {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      setState('still')
      paint()
    } else wake()
  }
  const onLost = (e: Event): void => {
    e.preventDefault()
    lost = true
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    setState('paused')
  }
  const onRestored = (): void => {
    r?.dispose()
    r = createGl(canvas, city, prof, pal)
    lost = r === null
    if (!r) {
      canvas.dataset.voxelMode = 'static'
      return
    }
    size()
    if (reduced) paint()
    else wake()
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(onResize) : null
  ro?.observe(canvas)
  const io = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver((es) => {
      onScreen = es[es.length - 1]?.isIntersecting ?? true
      if (onScreen) wake()
    })
    : null
  io?.observe(canvas)
  addEventListener('scroll', onScroll, { passive: true })
  addEventListener('resize', onResize, { passive: true })
  addEventListener('pointermove', wake, { passive: true })
  document.addEventListener('visibilitychange', onVisibility)
  reduceMq?.addEventListener('change', onReduce)
  if (r.mode === 'webgl2') {
    canvas.addEventListener('webglcontextlost', onLost)
    canvas.addEventListener('webglcontextrestored', onRestored)
  }
  target = progress()
  p = target
  size()
  if (reduced) {
    setState('still')
    paint()
  } else wake()
  return {
    stop() {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      ro?.disconnect()
      io?.disconnect()
      removeEventListener('scroll', onScroll)
      removeEventListener('resize', onResize)
      removeEventListener('pointermove', wake)
      document.removeEventListener('visibilitychange', onVisibility)
      reduceMq?.removeEventListener('change', onReduce)
      canvas.removeEventListener('webglcontextlost', onLost)
      canvas.removeEventListener('webglcontextrestored', onRestored)
      r?.dispose()
      r = null
    },
  }
}
