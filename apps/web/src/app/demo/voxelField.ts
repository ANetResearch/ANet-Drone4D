// Story stage of the landing page (D2.2 landing narrative, designed with gpt-6-sol): one voxel airplane flies through
// the chapters while the scene behind it changes from a refining voxel city to wind and rain, a formation of quadcopter
// wingmen, a scanning fan, nests with a relay handover, a bounded sandbox and two framed fidelity tiers. The scroll
// position maps directly to the flight (no inertia): each [data-story-chapter] element gives its chapter progress
// t in [0, 1] from where the anchor line of the viewport crosses it, and the keyframes of ./storyScene interpolate the
// plane's pose. Everything is one instanced draw call of unit cubes in raw WebGL2 (five vertex attributes, four plain
// uniforms: inside the WebGL2 specification minimum limits), with a Canvas2D fallback that projects the same instances.
// prefers-reduced-motion renders one still frame per chapter (mid-chapter pose, no clock); the loop pauses when the page
// is hidden, the stage is off screen, or after 9 s without input.
import { CHAPTER_IDS, KEYFRAMES, PLANE, type Action, type ChapterId, type Keyframe } from './storyScene'

export type VoxelMode = 'webgl2' | 'canvas2d' | 'static'
export interface VoxelHandle { stop(): void }

type Rgb = [number, number, number]
interface Profile { fps: number; dprCap: number; aa: boolean; detail: number }
interface Palette { ground: Rgb; face: Rgb; top: Rgb; line: Rgb; line2: Rgb; light: Rgb; white: Rgb; accent: Rgb }
interface Pose { x: number; y: number; z: number; scale: number; yaw: number; pitch: number; roll: number }
interface Frame { ci: number; t: number }
interface Renderer {
  mode: Exclude<VoxelMode, 'static'>
  software: boolean
  resize(w: number, h: number, dpr: number): void
  draw(inst: Float32Array, count: number, vp: Float32Array, plane: Float32Array): void
  dispose(): void
}

const IDLE_MS = 9000
const MAX_PIXELS = 4_000_000
const MAX_INST = 2600
const F = 12 // floats per instance: off.xyz, kind | size.xyz, alpha | rgb, unused
const K_SOLID = 0
const K_PLANE = 1
const K_GLOW = 2
const K_LINE = 3
const SPAN_X = 12
const BASE_Y = 8.5
const SPAN_Y = 4.5
const PLANE_SCALE = 0.72
const DEG = Math.PI / 180

function profile(): Profile {
  const w = globalThis.innerWidth || 1280
  const coarse = globalThis.matchMedia?.('(pointer: coarse)').matches ?? false
  if (w < 768) return { fps: 30, dprCap: 1.5, aa: false, detail: 0.55 }
  if (coarse || w < 1280) return { fps: 30, dprCap: 2, aa: false, detail: 0.75 }
  return { fps: 60, dprCap: 2, aa: true, detail: 1 }
}

// ---------------------------------------------------------------- palette (theme tokens to 0..1 sRGB triples)
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
    face: read('--g800', [0.114, 0.122, 0.137]),
    top: read('--g700', [0.165, 0.176, 0.196]),
    line: read('--g600', [0.25, 0.265, 0.29]),
    line2: read('--g400', [0.48, 0.5, 0.54]),
    light: read('--g300', [0.62, 0.64, 0.67]),
    white: read('--g100', [0.9, 0.91, 0.92]),
    accent: read('--r500', [0.914, 0.188, 0.141]),
  }
}

// ---------------------------------------------------------------- math
const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v)
const smooth = (a: number, b: number, v: number): number => {
  const u = clamp01((v - a) / (b - a))
  return u * u * (3 - 2 * u)
}
const lerp = (a: number, b: number, u: number): number => a + (b - a) * u
const hash = (i: number): number => {
  const s = Math.sin(i * 127.1 + 311.7) * 43758.5453
  return s - Math.floor(s)
}
const bump = (v: number, c: number, w: number): number => clamp01(1 - Math.abs(v - c) / w)

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

/** column-major model matrix of the plane: translate * rotY(yaw) * rotZ(pitch) * rotX(roll) * scale */
function planeMatrix(m: Float32Array, p: Pose): void {
  const cy = Math.cos(p.yaw * DEG), sy = Math.sin(p.yaw * DEG)
  const cp = Math.cos(p.pitch * DEG), sp = Math.sin(p.pitch * DEG)
  const cr = Math.cos(p.roll * DEG), sr = Math.sin(p.roll * DEG)
  // Rz(pitch) * Rx(roll)
  const a = [cp, sp, 0, -sp * cr, cp * cr, sr, sp * sr, -cp * sr, cr] // columns
  // Ry(yaw) * a
  const r = new Array<number>(9)
  for (let c = 0; c < 3; c++) {
    const x = a[c * 3], y = a[c * 3 + 1], z = a[c * 3 + 2]
    r[c * 3] = cy * x + sy * z
    r[c * 3 + 1] = y
    r[c * 3 + 2] = -sy * x + cy * z
  }
  const s = p.scale
  m.set([r[0] * s, r[1] * s, r[2] * s, 0, r[3] * s, r[4] * s, r[5] * s, 0, r[6] * s, r[7] * s, r[8] * s, 0, p.x, p.y, p.z, 1])
}

// ---------------------------------------------------------------- flight
function pose(kfs: readonly Keyframe[], t: number): Pose {
  let k = 0
  while (k < kfs.length - 2 && t > kfs[k + 1][0]) k++
  const a = kfs[k]
  const b = kfs[Math.min(k + 1, kfs.length - 1)]
  const u = b[0] > a[0] ? smooth(0, 1, (t - a[0]) / (b[0] - a[0])) : 0
  const x = lerp(a[1], b[1], u), y = lerp(a[2], b[2], u)
  return { x: x * SPAN_X, y: BASE_Y + y * SPAN_Y, z: 0, scale: lerp(a[3], b[3], u) * PLANE_SCALE,
    yaw: lerp(a[4], b[4], u), pitch: lerp(a[5], b[5], u), roll: lerp(a[6], b[6], u) }
}
/** how strongly `action` is happening at chapter progress t (a bump around each keyframe that names it) */
function actionWeight(kfs: readonly Keyframe[], t: number, action: Action): number {
  let w = 0
  for (const k of kfs) if (k[7] === action) w = Math.max(w, bump(t, k[0], 0.32))
  return w
}

// ---------------------------------------------------------------- scene assembly
class Builder {
  buf = new Float32Array(MAX_INST * F)
  n = 0
  put(x: number, y: number, z: number, kind: number, sx: number, sy: number, sz: number, a: number, c: Rgb): void {
    if (a <= 0.004 || this.n >= MAX_INST) return
    const o = this.n++ * F
    const b = this.buf
    b[o] = x; b[o + 1] = y; b[o + 2] = z; b[o + 3] = kind
    b[o + 4] = sx; b[o + 5] = sy; b[o + 6] = sz; b[o + 7] = a
    b[o + 8] = c[0]; b[o + 9] = c[1]; b[o + 10] = c[2]; b[o + 11] = 0
  }
}

interface Ctx { b: Builder; pal: Palette; detail: number; time: number; drift: number; plane: Pose }

function ground(c: Ctx, a: number): void {
  const { b, pal } = c
  for (let z = -12; z <= 12; z += 4) b.put(0, 0, z, K_LINE, 64, 0.02, 0.05, 0.32 * a, pal.line)
  const off = ((c.drift % 4) + 4) % 4
  for (let k = -9; k <= 9; k++) b.put(k * 4 - off, 0, 0, K_LINE, 0.05, 0.02, 24, 0.32 * a, pal.line)
}

function quad(c: Ctx, x: number, y: number, z: number, a: number, s = 1): void {
  const { b, pal } = c
  b.put(x, y, z, K_SOLID, 0.6 * s, 0.24 * s, 0.6 * s, a, pal.line2)
  for (const [dx, dz] of [[0.55, 0.55], [-0.55, 0.55], [0.55, -0.55], [-0.55, -0.55]] as const)
    b.put(x + dx * s, y + 0.05 * s, z + dz * s, K_SOLID, 0.34 * s, 0.08 * s, 0.34 * s, a, pal.light)
  b.put(x, y + 0.18 * s, z, K_GLOW, 0.14 * s, 0.1 * s, 0.14 * s, a, pal.accent)
}

/** the voxel city: coarse blocks, refined into 2 x 2 columns inside `radius` of x = cx (radius < 0: all coarse) */
function city(c: Ctx, a: number, shift: number, cx: number, radius: number, zFrom = -7, zTo = 5): void {
  const { b, pal } = c
  const cols = Math.round(12 * c.detail) + 2
  const step = 2.7
  const x0 = -((cols - 1) * step) / 2
  for (let i = 0; i < cols; i++) {
    for (let z = zFrom, j = 0; z <= zTo; z += step, j++) {
      const id = i * 31 + j * 7
      if (hash(id) < 0.2) continue
      const x = x0 + i * step + shift
      const h = 1 + Math.floor(hash(id + 3) * 4.5) + (hash(id + 5) > 0.92 ? 1 : 0)
      if (radius > 0 && Math.abs(x - cx) < radius) {
        for (const [dx, dz] of [[-0.55, -0.55], [0.55, -0.55], [-0.55, 0.55], [0.55, 0.55]] as const) {
          const hh = Math.max(1, h - Math.floor(hash(id + dx * 3 + dz * 5) * 2))
          b.put(x + dx, hh / 2, z + dz, K_SOLID, 0.98, hh, 0.98, a, pal.face)
        }
        b.put(x, h + 0.2, z, K_GLOW, 0.12, 0.12, 0.12, a * 0.55 * bump(Math.abs(x - cx), radius - 1, 1.4), pal.accent)
      } else {
        b.put(x, h / 2, z, K_SOLID, 2.1, h, 2.1, a, pal.face)
      }
    }
  }
}

function wind(c: Ctx, a: number): void {
  const { b, pal, time } = c
  const rows = c.detail < 0.7 ? 3 : 5
  for (let r = 0; r < rows; r++) {
    for (let i = 0; i < 24; i++) {
      const x = ((i * 1.45 + time * 4 + r * 3.1) % 34) - 17
      const y = 4.5 + r * 1.7 + Math.sin(x * 0.32 + r) * 0.55
      const fade = smooth(-17, -12, x) * (1 - smooth(12, 17, x))
      b.put(x, y, -5 + r * 2.4, K_GLOW, 0.26, 0.1, 0.1, a * 0.75 * fade, pal.light)
    }
  }
  const drops = Math.round(60 * c.detail)
  for (let i = 0; i < drops; i++) {
    const x = hash(i) * 32 - 16
    const z = hash(i + 50) * 14 - 8
    const y = 13 - ((time * 9 + hash(i + 99) * 13) % 13)
    b.put(x, y, z, K_GLOW, 0.05, 0.55, 0.05, a * 0.45, pal.line2)
  }
}

function lanes(c: Ctx, a: number, shift: number): void {
  const { b, pal } = c
  for (let j = 0; j < 4; j++) {
    const z = -1 + j * 1.8
    for (let x = -11; x <= 11; x += 0.9) b.put(x + shift, 0.03, z, K_LINE, 0.32, 0.03, 0.32, a * 0.6, pal.line2)
  }
}

function nests(c: Ctx, a: number, shift: number): [number, number, number][] {
  const { b, pal } = c
  const at: [number, number, number][] = [[-9 + shift, 0, 3], [0 + shift, 0, -3], [9 + shift, 0, 3]]
  for (const [x, , z] of at) {
    b.put(x, 0.12, z, K_SOLID, 2.4, 0.24, 2.4, a, pal.top)
    b.put(x, 0.7, z, K_SOLID, 1.3, 0.9, 1.3, a, pal.face)
    b.put(x + 0.8, 1.3, z - 0.8, K_SOLID, 0.14, 1.6, 0.14, a, pal.line2)
    b.put(x + 0.8, 2.15, z - 0.8, K_GLOW, 0.22, 0.22, 0.22, a, pal.accent)
  }
  for (let s = 0; s < 2; s++) {
    const [x1, , z1] = at[s]
    const [x2, , z2] = at[s + 1]
    for (let k = 1; k < 12; k += 2) b.put(lerp(x1, x2, k / 12), 0.03, lerp(z1, z2, k / 12), K_LINE, 0.34, 0.03, 0.12, a * 0.7, pal.line2)
  }
  return at
}

function frame(c: Ctx, cx: number, cy: number, cz: number, s: number, a: number, col: Rgb, dashed: boolean): void {
  const { b } = c
  const h = s / 2
  const th = 0.09
  const edges: [number, number, number, number, number, number][] = []
  for (const y of [-h, h]) for (const z of [-h, h]) edges.push([0, y, z, s, th, th])
  for (const x of [-h, h]) for (const z of [-h, h]) edges.push([x, 0, z, th, s, th])
  for (const x of [-h, h]) for (const y of [-h, h]) edges.push([x, y, 0, th, th, s])
  for (const [x, y, z, sx, sy, sz] of edges) {
    if (!dashed) b.put(cx + x, cy + y, cz + z, K_GLOW, sx, sy, sz, a, col)
    else {
      const n = 5
      const big = Math.max(sx, sy, sz)
      for (let k = 0; k < n; k++) {
        const o = -big / 2 + (k + 0.25) * (big / n)
        const len = (big / n) * 0.5
        b.put(cx + x + (sx === big ? o + len / 2 : 0), cy + y + (sy === big ? o + len / 2 : 0), cz + z + (sz === big ? o + len / 2 : 0),
          K_GLOW, sx === big ? len : sx, sy === big ? len : sy, sz === big ? len : sz, a, col)
      }
    }
  }
  for (const x of [-h, h]) for (const y of [-h, h]) for (const z of [-h, h]) b.put(cx + x, cy + y, cz + z, K_GLOW, 0.22, 0.22, 0.22, a, col)
}

function scene(c: Ctx, id: ChapterId, t: number, a: number): void {
  const { b, pal } = c
  const shift = 4 - t * 8 // the scene drifts left as the chapter advances: the plane reads as flying forward
  const p = c.plane
  const kfs = KEYFRAMES[id]
  switch (id) {
    case 'hero':
      city(c, a * 0.35, shift * 0.5, 0, -1, -12, -8)
      break
    case 'digital-twin':
      city(c, a, shift, p.x, 2.5 + t * 22)
      break
    case 'environment':
      city(c, a * 0.4, shift, 0, -1)
      wind(c, a)
      break
    case 'fleet-control': {
      lanes(c, a, shift)
      const wf = a * smooth(0.08, 0.36, t)
      for (let k = 1; k <= 4; k++) for (const sd of [-1, 1]) quad(c, p.x - k * 2.8, p.y - 0.45 * k, sd * k * 2.5, wf, 1.3)
      break
    }
    case 'perception': {
      for (let i = 0; i < 9; i++) b.put(-12 + i * 3.3 + shift, 0.4 + hash(i) * 0.6, -5 + hash(i + 9) * 6, K_SOLID, 2.4, 0.8 + hash(i + 4), 2.2, a * 0.8, pal.face)
      const tx = 7 + shift
      const tz = 3
      b.put(tx, 0.45, tz, K_SOLID, 1.6, 0.9, 0.9, a, pal.line2)
      b.put(tx - 0.3, 1.1, tz, K_SOLID, 0.7, 0.45, 0.8, a, pal.light)
      const ws = a * actionWeight(kfs, t, 'scan')
      if (ws > 0) {
        for (let r = 0; r < 7; r++) {
          const gx = tx - 3 + r
          const gz = tz + (r % 2 ? 1.6 : -1.6)
          for (let k = 1; k <= 9; k++) {
            const u = k / 10
            b.put(lerp(p.x, gx, u), lerp(p.y - 0.4, 0.05, u), lerp(p.z, gz, u), K_GLOW, 0.16, 0.16, 0.16, ws * 0.9, pal.accent)
          }
        }
      }
      break
    }
    case 'relay-agents': {
      const at = nests(c, a, shift)
      const wr = actionWeight(kfs, t, 'relay')
      const u = smooth(0.2, 0.62, t)
      const [nx, , nz] = at[1]
      quad(c, lerp(nx, p.x - 3.4, u), lerp(2.4, p.y - 0.5, u), lerp(nz, p.z + 1.8, u), a, 1.3)
      if (wr > 0) b.put(p.x - 1.3, p.y - 0.2, p.z + 0.7, K_GLOW, 0.3, 0.3, 0.3, a * wr, pal.accent)
      break
    }
    case 'sandbox': {
      const ws = a * smooth(0.05, 0.4, t)
      for (let x = -9; x <= 9.01; x += 0.75) for (const z of [-5, 5]) b.put(x + shift * 0.3, 0.1, z, K_SOLID, 0.6, 0.2, 0.6, ws, pal.top)
      for (let z = -4.25; z <= 4.26; z += 0.75) for (const x of [-9, 9]) b.put(x + shift * 0.3, 0.1, z, K_SOLID, 0.6, 0.2, 0.6, ws, pal.top)
      for (const [x, z] of [[-9, -5], [9, -5], [-9, 5], [9, 5]] as const) b.put(x + shift * 0.3, 0.3, z, K_GLOW, 0.3, 0.3, 0.3, ws, pal.accent)
      for (let i = 0; i < 6; i++) b.put(-6 + i * 2.4 + shift * 0.3, 0.25, -2 + hash(i) * 4, K_SOLID, 0.5, 0.5, 0.5, ws * 0.8, pal.line2)
      break
    }
    case 'roadmap':
      frame(c, 6 + shift * 0.4, 8, -1, 6, a * smooth(0.05, 0.4, t), pal.light, false)
      frame(c, 15 + shift * 0.4, 10, -7, 5, a * 0.6 * smooth(0.3, 0.7, t), pal.line2, true)
      break
  }
}

function planeVoxels(c: Ctx, scan: number): void {
  const { b, pal } = c
  const cols: Rgb[] = [pal.light, pal.line2, pal.accent, pal.white, pal.top]
  for (const [x, y, z, part] of PLANE) b.put(x, y, z, K_PLANE, 1, 1, 1, 1, part === 2 && scan > 0.3 ? pal.accent : cols[part])
}

function trail(c: Ctx, id: ChapterId, t: number, scan: number): void {
  const { b, pal } = c
  for (let k = 1; k <= 10; k++) {
    const q = pose(KEYFRAMES[id], Math.max(0, t - k * 0.022))
    const s = 0.3 - k * 0.022
    b.put(q.x - k * 0.55, q.y, q.z, K_GLOW, s, s, s, 0.5 * (1 - k / 11), scan > 0.3 ? pal.accent : pal.line2)
  }
}

// ---------------------------------------------------------------- WebGL2
const VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_nrm;
layout(location = 2) in vec4 a_off;
layout(location = 3) in vec4 a_size;
layout(location = 4) in vec4 a_col;
uniform mat4 u_vp;
uniform mat4 u_plane;
out vec3 v_nrm;
out vec3 v_loc;
out vec4 v_col;
flat out float v_kind;
void main() {
  float kind = a_off.w;
  vec3 lp = (a_pos - 0.5) * a_size.xyz;
  vec3 wp = a_off.xyz + lp;
  vec3 n = a_nrm;
  if (abs(kind - 1.0) < 0.5) {
    wp = (u_plane * vec4(wp, 1.0)).xyz;
    n = mat3(u_plane) * a_nrm;
  }
  v_nrm = n;
  v_loc = a_pos * a_size.xyz;
  v_col = vec4(a_col.rgb, a_size.w);
  v_kind = kind;
  gl_Position = u_vp * vec4(wp, 1.0);
}`

const FS = `#version 300 es
precision highp float;
in vec3 v_nrm;
in vec3 v_loc;
in vec4 v_col;
flat in float v_kind;
uniform vec3 u_line;
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
  float a = v_col.a;
  if (v_kind > 1.5) {
    o = vec4(v_col.rgb * a, a);
    return;
  }
  vec3 n = normalize(v_nrm);
  float lit = 0.58 + 0.42 * max(dot(n, normalize(vec3(0.35, 1.0, 0.45))), 0.0);
  vec3 c = mix(v_col.rgb * lit, u_line, edges(v_loc, n) * 0.5);
  o = vec4(c * a, a);
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
    const [a, b, c] = q
    const e1 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]]
    const e2 = [c[0] - a[0], c[1] - a[1], c[2] - a[2]]
    const cr = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]]
    const quad4 = cr[0] * nrm[0] + cr[1] * nrm[1] + cr[2] * nrm[2] > 0 ? q : [q[0], q[3], q[2], q[1]]
    for (const i of [0, 1, 2, 0, 2, 3]) out.push(...quad4[i], ...nrm)
  }
  return new Float32Array(out)
}

function createGl(canvas: HTMLCanvasElement, prof: Profile, pal: Palette): Renderer | null {
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
  g.bufferData(g.ARRAY_BUFFER, MAX_INST * F * 4, g.DYNAMIC_DRAW)
  for (let i = 0; i < 3; i++) {
    g.enableVertexAttribArray(2 + i)
    g.vertexAttribPointer(2 + i, 4, g.FLOAT, false, F * 4, i * 16)
    g.vertexAttribDivisor(2 + i, 1)
  }
  g.bindVertexArray(null)
  const uVp = g.getUniformLocation(prog, 'u_vp')
  const uPlane = g.getUniformLocation(prog, 'u_plane')
  const uLine = g.getUniformLocation(prog, 'u_line')
  const uLw = g.getUniformLocation(prog, 'u_lw')
  g.useProgram(prog)
  g.uniform3fv(uLine, pal.line)
  g.enable(g.DEPTH_TEST)
  g.enable(g.CULL_FACE)
  g.enable(g.BLEND)
  g.blendFunc(g.ONE, g.ONE_MINUS_SRC_ALPHA)
  g.clearColor(0, 0, 0, 0)
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
      g.viewport(0, 0, w, h)
      g.useProgram(prog)
      g.uniform1f(uLw, Math.max(1, dpr * 0.75))
    },
    draw(inst, count, vp, plane) {
      g.useProgram(prog)
      g.uniformMatrix4fv(uVp, false, vp)
      g.uniformMatrix4fv(uPlane, false, plane)
      g.bindBuffer(g.ARRAY_BUFFER, ins)
      g.bufferSubData(g.ARRAY_BUFFER, 0, inst, 0, count * F)
      g.clear(g.COLOR_BUFFER_BIT | g.DEPTH_BUFFER_BIT)
      g.bindVertexArray(vao)
      g.drawArraysInstanced(g.TRIANGLES, 0, 36, count)
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

// ---------------------------------------------------------------- Canvas2D fallback (projected squares, painter's order)
function createCanvas2d(canvas: HTMLCanvasElement): Renderer | null {
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  let W = 1
  let H = 1
  const order: number[] = []
  const pts = new Float32Array(MAX_INST * 4)
  const css = (c: ArrayLike<number>, o: number, a: number, k: number): string =>
    `rgba(${Math.round(c[o] * 255 * k)},${Math.round(c[o + 1] * 255 * k)},${Math.round(c[o + 2] * 255 * k)},${a.toFixed(3)})`
  return {
    mode: 'canvas2d',
    software: false,
    resize(w, h) {
      W = w
      H = h
    },
    draw(inst, count, vp, plane) {
      ctx.clearRect(0, 0, W, H)
      order.length = 0
      for (let i = 0; i < count; i++) {
        const o = i * F
        let x = inst[o], y = inst[o + 1], z = inst[o + 2]
        let s = Math.max(inst[o + 4], inst[o + 6], inst[o + 5] * 0.5)
        if (inst[o + 3] === K_PLANE) {
          const px = plane[0] * x + plane[4] * y + plane[8] * z + plane[12]
          const py = plane[1] * x + plane[5] * y + plane[9] * z + plane[13]
          const pz = plane[2] * x + plane[6] * y + plane[10] * z + plane[14]
          x = px; y = py; z = pz
          s *= Math.hypot(plane[0], plane[1], plane[2])
        }
        const cx = vp[0] * x + vp[4] * y + vp[8] * z + vp[12]
        const cy = vp[1] * x + vp[5] * y + vp[9] * z + vp[13]
        const cz = vp[2] * x + vp[6] * y + vp[10] * z + vp[14]
        pts[i * 4] = (cx * 0.5 + 0.5) * W
        pts[i * 4 + 1] = (0.5 - cy * 0.5) * H
        pts[i * 4 + 2] = cz
        pts[i * 4 + 3] = Math.max(1, s * vp[0] * W * 0.5)
        order.push(i)
      }
      order.sort((a, b) => pts[b * 4 + 2] - pts[a * 4 + 2])
      for (const i of order) {
        const o = i * F
        const r = pts[i * 4 + 3]
        const kind = inst[o + 3]
        const a = inst[o + 7]
        const sx = pts[i * 4]
        const sy = pts[i * 4 + 1]
        if (kind === K_LINE) {
          const lw = Math.max(1, inst[o + 4] * vp[0] * W * 0.5)
          const lh = Math.max(1, inst[o + 6] * vp[0] * W * 0.25)
          ctx.fillStyle = css(inst, o + 8, a, 1)
          ctx.fillRect(sx - lw / 2, sy - lh / 2, lw, lh)
          continue
        }
        const hgt = kind === K_SOLID ? Math.max(1, inst[o + 5] * vp[5] * H * 0.5) : r
        ctx.fillStyle = css(inst, o + 8, a, kind === K_GLOW ? 1 : 0.8)
        ctx.fillRect(sx - r / 2, sy - hgt / 2, r, hgt)
        if (kind !== K_GLOW) {
          ctx.fillStyle = css(inst, o + 8, a, 1.15)
          ctx.fillRect(sx - r / 2, sy - hgt / 2, r, Math.max(1, r * 0.3))
        }
      }
    },
    dispose() {},
  }
}

// ---------------------------------------------------------------- the loop
export function startVoxelField(canvas: HTMLCanvasElement): VoxelHandle {
  const prof = { ...profile() }
  const pal = palette()
  let frames = 0
  Object.defineProperty(canvas, 'voxelFrames', { configurable: true, get: () => frames })
  let r: Renderer | null = createGl(canvas, prof, pal) ?? createCanvas2d(canvas)
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
  const builder = new Builder()
  const vp = new Float32Array(16)
  const pm = new Float32Array(16)
  let reduced = reduceMq?.matches ?? false
  let raf = 0
  let last = 0
  let clock = 0
  let lastInput = performance.now()
  let onScreen = true
  let lost = false
  let aspect = 1
  let stillKey = ''
  const setState = (st: string): void => {
    if (canvas.dataset.voxelState !== st) canvas.dataset.voxelState = st
  }
  /** chapter index and progress where the anchor line of the viewport crosses the story's chapter elements */
  const where = (): Frame => {
    const els = document.querySelectorAll<HTMLElement>('[data-story-chapter]')
    if (els.length === 0) return { ci: 0, t: 0 }
    const anchor = innerHeight * (innerWidth >= 1024 ? 0.5 : 0.7)
    let ci = -1
    let t = 0
    els.forEach((el, i) => {
      const rc = el.getBoundingClientRect()
      if (rc.top <= anchor) {
        ci = i
        t = rc.height > 0 ? clamp01((anchor - rc.top) / rc.height) : 1
      }
    })
    if (ci < 0) return { ci: 0, t: 0 }
    const id = els[ci].dataset.storyChapter as ChapterId
    const k = CHAPTER_IDS.indexOf(id)
    return { ci: k < 0 ? 0 : k, t }
  }
  const paint = (): void => {
    if (!r || lost) return
    const fr = where()
    const id = CHAPTER_IDS[fr.ci]
    const t = reduced ? 0.5 : fr.t
    const time = reduced ? 0 : clock
    const p = pose(KEYFRAMES[id], t)
    if (!reduced) p.y += Math.sin(time * 1.3) * 0.12
    builder.n = 0
    const scan = actionWeight(KEYFRAMES[id], t, 'scan') * (id === 'perception' ? 1 : 0)
    const c: Ctx = { b: builder, pal, detail: prof.detail, time, drift: fr.ci * 8 + t * 8 + time * 0.6, plane: p }
    ground(c, 1)
    const wCur = fr.ci > 0 && !reduced ? smooth(0, 0.16, t) : 1
    if (wCur < 1) {
      const prevId = CHAPTER_IDS[fr.ci - 1]
      scene({ ...c, plane: pose(KEYFRAMES[prevId], 1) }, prevId, 1, 1 - wCur)
    }
    scene(c, id, t, wCur)
    if (!reduced) trail(c, id, t, scan)
    planeVoxels(c, scan)
    planeMatrix(pm, p)
    const hw = 18
    viewProj(vp, 0.5, 0.42, [0, 5.5, 0], hw, hw / aspect)
    r.draw(builder.buf, builder.n, vp, pm)
    frames++
    canvas.dataset.voxelChapter = id
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
    aspect = w / h
    r.resize(bw, bh, dpr)
  }
  /** reduced motion: one still frame per chapter, drawn only when the chapter changes */
  const paintStill = (): void => {
    const fr = where()
    const key = `${fr.ci}:${canvas.width}x${canvas.height}`
    if (key === stillKey) return
    stillKey = key
    paint()
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
    paint()
    if (now - lastInput > IDLE_MS) {
      cancelAnimationFrame(raf)
      raf = 0
      last = 0
      setState('idle')
    }
  }
  const wake = (): void => {
    lastInput = performance.now()
    if (reduced) {
      paintStill()
      return
    }
    if (raf === 0 && active()) {
      last = 0
      setState('running')
      raf = requestAnimationFrame(tick)
    }
  }
  const onResize = (): void => {
    size()
    if (reduced) {
      stillKey = ''
      paintStill()
    } else wake()
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
      stillKey = ''
      paintStill()
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
    r = createGl(canvas, prof, pal)
    lost = r === null
    if (!r) {
      canvas.dataset.voxelMode = 'static'
      return
    }
    size()
    stillKey = ''
    if (reduced) paintStill()
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
  addEventListener('scroll', wake, { passive: true })
  addEventListener('resize', onResize, { passive: true })
  addEventListener('pointermove', wake, { passive: true })
  document.addEventListener('visibilitychange', onVisibility)
  reduceMq?.addEventListener('change', onReduce)
  if (r.mode === 'webgl2') {
    canvas.addEventListener('webglcontextlost', onLost)
    canvas.addEventListener('webglcontextrestored', onRestored)
  }
  size()
  if (reduced) {
    setState('still')
    paintStill()
  } else wake()
  return {
    stop() {
      if (raf) cancelAnimationFrame(raf)
      raf = 0
      ro?.disconnect()
      io?.disconnect()
      removeEventListener('scroll', wake)
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
