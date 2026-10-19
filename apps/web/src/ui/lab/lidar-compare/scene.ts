// LiDAR 与重建对比的专用画布场景（AWR-05 §7.5；AWR-14 §21.8.2）：three.js 对象的构建，不经 M06 图层注册表（P0 不依赖扩展点 14）。
// - 真值：Tier B/A 为裁剪碰撞网格（GLB，≤ 300,000 面）半透明（不透明度 0.25，g400）；Tier S 为网格采样彩色点云（实心小方点，原色）；
// - L0 已知位姿地图为实心圆点，FAST-LIVO2 地图为空心方点（两路以点形区分）；误差着色为灰阶顺序色阶：误差 u8 0（0 m）为 g400、
//   250（≥ 0.5 m）为 g50，无值点为 g600；不使用色相与红色（§21.27 第 4 条）；可切换为原色（FAST-LIVO2 彩色地图）；
// - 轨迹：真值 g300 实线，估计 g50 虚线；裁剪框 g500 线框。
// 颜色一律取自主题令牌（--g*），着色器直接输出 sRGB 分量；本文件不含颜色字面量。场景对象可在 Node 中构建（单元测试），渲染器只在画布组件中创建。
import {
  Box3, Box3Helper, BufferAttribute, BufferGeometry, Color, DoubleSide, Float32BufferAttribute, Line, LineBasicMaterial, LineDashedMaterial, Mesh,
  MeshBasicMaterial, type Object3D, Points, ShaderMaterial, SRGBColorSpace, Vector3,
} from 'three'
import { ERR_NONE, ERR_U8_MAX, type CompareSource, type ColorBy, type DecodedPoints, type Vec3 } from './compare'

export type PointShape = 'circle' | 'hollow' | 'square'
/** 点形：真值采样点为实心方点，L0 为实心圆，FAST-LIVO2 为空心方 */
export const SHAPE_OF: Readonly<Record<CompareSource, PointShape>> = { gt: 'square', l0: 'circle', livo: 'hollow' }
const SHAPE_CODE: Readonly<Record<PointShape, number>> = { circle: 0, hollow: 1, square: 2 }
/** 真值网格的不透明度（§21.8.2） */
export const MESH_OPACITY = 0.25
/** 空心方点的最小绘制尺寸（绘制缓冲像素）：更小时无法区分空心与实心；Tier S 像素比 0.5 时对应 6 CSS px */
export const HOLLOW_MIN_PX = 3
/** 点的 CSS 尺寸：空心方点不小于 HOLLOW_MIN_PX 个绘制缓冲像素 */
export const pointCssPx = (shape: PointShape, sizePx: number, pixelRatio: number): number =>
  shape === 'hollow' ? Math.max(sizePx, HOLLOW_MIN_PX / Math.max(pixelRatio, 1e-3)) : sizePx

export interface SceneTokens { lo: Vec3; hi: Vec3; none: Vec3; mesh: Vec3; gtTraj: Vec3; estTraj: Vec3; crop: Vec3 }
/** 令牌名 → 角色（主题切换时重读） */
export const TOKEN_VARS: Readonly<Record<keyof SceneTokens, string>> = {
  lo: '--g400', hi: '--g50', none: '--g600', mesh: '--g400', gtTraj: '--g300', estTraj: '--g50', crop: '--g500',
}

/** CSS 颜色 → sRGB 分量（0–1）；空值或无法解析时为中灰 */
export function cssRgb(css: string): Vec3 {
  const c = new Color(0.5, 0.5, 0.5)
  if (css) c.setStyle(css, SRGBColorSpace)
  const o = { r: 0, g: 0, b: 0 }
  c.getRGB(o, SRGBColorSpace)
  return [o.r, o.g, o.b]
}

export function readSceneTokens(el: Element | null = typeof document !== 'undefined' ? document.documentElement : null): SceneTokens {
  const cs = el ? getComputedStyle(el) : null
  const g = (k: keyof SceneTokens) => cssRgb(cs ? cs.getPropertyValue(TOKEN_VARS[k]).trim() : '')
  return { lo: g('lo'), hi: g('hi'), none: g('none'), mesh: g('mesh'), gtTraj: g('gtTraj'), estTraj: g('estTraj'), crop: g('crop') }
}

const color3 = (v: Vec3) => new Color().setRGB(v[0], v[1], v[2], SRGBColorSpace)

const VERT = /* glsl */ `
attribute vec3 rgb;
attribute float erru8;
uniform float uSize;
uniform float uPx;
uniform int uColorBy;
uniform vec3 uLo;
uniform vec3 uHi;
uniform vec3 uNone;
varying vec3 vCol;
void main() {
  if (uColorBy == 0) {
    vCol = erru8 > ${ERR_U8_MAX}.5 ? uNone : mix(uLo, uHi, clamp(erru8 / ${ERR_U8_MAX}.0, 0.0, 1.0));
  } else {
    vCol = rgb;
  }
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = uSize * uPx;
}`
const FRAG = /* glsl */ `
uniform int uShape;
uniform float uSize;
uniform float uPx;
varying vec3 vCol;
void main() {
  vec2 c = gl_PointCoord * 2.0 - 1.0;
  if (uShape == 0) {
    if (dot(c, c) > 1.0) discard;
  } else if (uShape == 1) {
    float inner = 1.0 - 2.0 / max(uSize * uPx, 2.0);
    if (max(abs(c.x), abs(c.y)) < inner) discard;
  }
  gl_FragColor = vec4(vCol, 1.0);
}`

/** 显示点（按抽稀步长取下标 0, stride, …）的几何：position（局部 ENU）、rgb（u8 归一化）、erru8（u8 原值，无误差层全为 ERR_NONE） */
export function pointsGeometry(L: DecodedPoints): BufferGeometry {
  const m = Math.ceil(L.n / L.stride)
  const pos = new Float32Array(3 * m)
  const rgb = new Uint8Array(3 * m)
  const err = new Uint8Array(m)
  for (let k = 0, i = 0; i < L.n; i += L.stride, k++) {
    pos.set(L.pos.subarray(3 * i, 3 * i + 3), 3 * k)
    rgb.set(L.rgb.subarray(3 * i, 3 * i + 3), 3 * k)
    err[k] = L.err ? L.err[i]! : ERR_NONE
  }
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(pos, 3))
  g.setAttribute('rgb', new BufferAttribute(rgb, 3, true))
  g.setAttribute('erru8', new BufferAttribute(err, 1, false))
  g.computeBoundingBox()
  g.computeBoundingSphere()
  return g
}

export interface PointsOpts { colorBy: ColorBy; sizePx: number; pixelRatio: number }
/** 一路点云；真值采样点始终为原色，误差着色只作用于带误差的地图层 */
export function pointsObject(L: DecodedPoints, tk: SceneTokens, o: PointsOpts): Points {
  const shape = SHAPE_OF[L.desc.src]
  const size = pointCssPx(shape, o.sizePx, o.pixelRatio)
  const mat = new ShaderMaterial({
    vertexShader: VERT, fragmentShader: FRAG,
    uniforms: {
      uSize: { value: size }, uPx: { value: o.pixelRatio }, uShape: { value: SHAPE_CODE[shape] },
      uColorBy: { value: L.desc.src !== 'gt' && L.err && o.colorBy === 'err' ? 0 : 1 },
      uLo: { value: new Vector3(...tk.lo) }, uHi: { value: new Vector3(...tk.hi) }, uNone: { value: new Vector3(...tk.none) },
    },
  })
  const p = new Points(pointsGeometry(L), mat)
  p.name = `points:${L.desc.src}:${L.desc.path}`
  p.userData = { src: L.desc.src, shape }
  return p
}

/** 更新已有点云的着色方式、尺寸与像素比（不重建几何） */
export function updatePoints(p: Points, tk: SceneTokens, o: PointsOpts): void {
  const m = p.material as ShaderMaterial
  const src = p.userData.src as CompareSource
  const shape = p.userData.shape as PointShape
  const hasErr = (p.geometry.getAttribute('erru8').array as Uint8Array).some((u) => u !== ERR_NONE)
  m.uniforms.uSize!.value = pointCssPx(shape, o.sizePx, o.pixelRatio)
  m.uniforms.uPx!.value = o.pixelRatio
  m.uniforms.uColorBy!.value = src !== 'gt' && hasErr && o.colorBy === 'err' ? 0 : 1
  ;(m.uniforms.uLo!.value as Vector3).set(...tk.lo)
  ;(m.uniforms.uHi!.value as Vector3).set(...tk.hi)
  ;(m.uniforms.uNone!.value as Vector3).set(...tk.none)
}

/** 轨迹折线：真值实线（g300），估计虚线（g50） */
export function trajectoryObject(xyz: Float64Array, kind: 'gt' | 'est', tk: SceneTokens): Line {
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(Float32Array.from(xyz), 3))
  const mat = kind === 'gt'
    ? new LineBasicMaterial({ color: color3(tk.gtTraj) })
    : new LineDashedMaterial({ color: color3(tk.estTraj), dashSize: 2, gapSize: 1.5 })
  const line = new Line(g, mat)
  if (kind === 'est') line.computeLineDistances()
  line.name = `traj:${kind}`
  return line
}

/** 网格的三角形数（有索引按索引，否则按顶点） */
export function faceCount(root: Object3D): number {
  let faces = 0
  root.traverse((o) => {
    const m = o as Mesh
    if (!m.isMesh) return
    const g = m.geometry
    faces += g.index ? g.index.count / 3 : g.getAttribute('position').count / 3
  })
  return Math.round(faces)
}

/** 真值网格：所有子网格换为半透明 g400 基础材质（不写深度，双面） */
export function styleTruthMesh(root: Object3D, tk: SceneTokens): Object3D {
  const mat = new MeshBasicMaterial({ color: color3(tk.mesh), transparent: true, opacity: MESH_OPACITY, depthWrite: false, side: DoubleSide })
  const old = new Set<{ dispose: () => void }>()
  root.traverse((o) => {
    const m = o as Mesh
    if (!m.isMesh) return
    for (const x of Array.isArray(m.material) ? m.material : [m.material]) old.add(x)
    m.material = mat
  })
  for (const x of old) x.dispose()
  root.name = 'truth:mesh'
  root.renderOrder = 1
  return root
}

/** 裁剪 GLB（局部 ENU，不换轴）→ 场景根节点与面数 */
export async function parseMeshGlb(buf: ArrayBuffer): Promise<{ root: Object3D; faces: number }> {
  const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js')
  const gltf = await new GLTFLoader().parseAsync(buf, '')
  return { root: gltf.scene, faces: faceCount(gltf.scene) }
}

export function cropObject(crop: { min: Vec3; max: Vec3 }, tk: SceneTokens): Box3Helper {
  const h = new Box3Helper(new Box3(new Vector3(...crop.min), new Vector3(...crop.max)), color3(tk.crop))
  h.name = 'crop'
  return h
}

/** 释放对象树的几何与材质（卸载与重建时调用） */
export function disposeTree(root: Object3D): void {
  root.traverse((o) => {
    const m = o as Mesh
    m.geometry?.dispose()
    const mat = m.material as { dispose?: () => void } | { dispose?: () => void }[] | undefined
    if (Array.isArray(mat)) mat.forEach((x) => x.dispose?.())
    else mat?.dispose?.()
  })
}
