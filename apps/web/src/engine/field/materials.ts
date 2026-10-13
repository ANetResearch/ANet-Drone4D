// Field-view programs (AWR-04 §15.11.6; ADR-086, ADR-087, ADR-125). Owner: M06.
// Two programs, both warmed by the shader zoo under the mask; switching the view or the volumes only rewrites instance data:
//   points  'glpoint': GL points, gl_PointSize from the per-point diameter (one drawArrays, no instancing: SwiftShader runs
//           instanced draws instance by instance); 'quad' (ALIASED_POINT_SIZE_RANGE below 8 px, the WebGL2 minimum is 1):
//           instanced screen-space quads (one drawElementsInstanced). Per point: offset vec3 (u * r) and info vec4
//           (anchor slot, ramp + 5 * shape, opacity, diameter CSS px); the anchor positions come from a 64 x 1 RGBA32F texture
//           (nearest, vertex fetch, written every frame; no uniform array, so no uniform block grows with the anchors).
//           Shapes are distance fields in the fragment stage (dot, diamond, ring, cross: no texture); colours are the five
//           field greys (no hue, R2) converted in the vertex stage; premultiplied alpha, depth test on, depth write off.
//   shells  1 px native line segments: offset vec3 and info vec4 (anchor slot, ramp, dash coordinate or -1, opacity);
//           dashes discarded in the fragment (one dash per unit of the coordinate, half on).
// Hidden points and points of a missing anchor collapse to a vertex outside the clip volume (no raster). Uniforms: the CSS to
// raster ratio and the drawing-buffer size (vertex), five ramp colours (vertex): with the camera and object matrices that is
// two vertex blocks and no fragment block per program (WebGL2 minimum 12 / 12 / 24, binding points <= 3, 16 384 B).
import { DataTexture, FloatType, InstancedBufferAttribute, InstancedBufferGeometry, NearestFilter, RGBAFormat, Vector2, Vector3, BufferAttribute, BufferGeometry, Sphere } from 'three'
import { LineBasicNodeMaterial, MeshBasicNodeMaterial, type PointsNodeMaterial } from 'three/webgpu'
import {
  Fn, If, abs, attribute, builtin, cameraProjectionMatrix, float, floor, fract, int, ivec2, length, max, min, modelViewMatrix, pointUV, positionGeometry,
  select, step, texture, uniform, varying, vec4,
} from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { outputTransform } from '../shading'
import { FIELD } from './build'

type N = any // TSL nodes

/**
 * the five field greys, darkest first: the scene token --field-ramp-1..5 (g500, g400, g300, g200, g50; styles/theme.css,
 * generated into SCENE.fieldRamp by gen-theme-tokens; request d2-WP-12c-to-M06 item 3)
 */
export function fieldRamp(): readonly (readonly [number, number, number])[] {
  return SCENE.fieldRamp
}

function rampSelect(idx: N): N {
  const cols = fieldRamp().map((c) => uniform(new Vector3(c[0], c[1], c[2])))
  let c: N = cols[cols.length - 1]
  for (let i = cols.length - 2; i >= 0; i--) c = select(idx.lessThan(i + 0.5), cols[i], c)
  return c
}

/** the anchor table: FIELD.anchors x 1 RGBA32F texels (xyz ENU m, w 1 when the anchor is placed) */
export function makeAnchorTexture(): DataTexture {
  const t = new DataTexture(new Float32Array(4 * FIELD.anchors), FIELD.anchors, 1, RGBAFormat, FloatType)
  t.minFilter = NearestFilter
  t.magFilter = NearestFilter
  t.generateMipmaps = false
  t.needsUpdate = true
  t.name = 'field.anchors'
  return t
}

export interface FieldUniforms {
  /** raster px per CSS px */
  dpr: N
  /** drawing-buffer size (quad mode) */
  viewport: N
}

/** inside test of the four shapes at p in [-1, 1]^2: 1 inside, 0 outside */
function shapeInside(p: N, shape: N): N {
  const ax: N = abs(p.x)
  const ay: N = abs(p.y)
  const d: N = length(p)
  const dot: N = step(d, 1)
  const diamond: N = step(ax.add(ay), 1)
  const ring: N = step(d, 1).mul(step(0.55, d))
  const cross: N = step(min(ax, ay), 0.3).mul(step(max(ax, ay), 1))
  return select(shape.lessThan(0.5), dot, select(shape.lessThan(1.5), diamond, select(shape.lessThan(2.5), ring, cross)))
}

export interface FieldPointsGeometry {
  geometry: BufferGeometry
  off: BufferAttribute
  info: BufferAttribute
}

/** geometry of capPoints points: GL points (position = offset) or instanced quads (position = corner, fOff = offset) */
export function makeFieldPointsGeometry(mode: 'glpoint' | 'quad', capPoints: number): FieldPointsGeometry {
  if (mode === 'quad') {
    const g = new InstancedBufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array([-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, 1, 0]), 3))
    g.setIndex([0, 1, 2, 0, 2, 3])
    const off = new InstancedBufferAttribute(new Float32Array(3 * capPoints), 3)
    const info = new InstancedBufferAttribute(new Float32Array(4 * capPoints), 4)
    off.setUsage(35048)
    info.setUsage(35048)
    g.setAttribute('fOff', off)
    g.setAttribute('fInfo', info)
    g.instanceCount = 0
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    return { geometry: g, off, info }
  }
  const g = new BufferGeometry()
  const off = new BufferAttribute(new Float32Array(3 * capPoints), 3)
  const info = new BufferAttribute(new Float32Array(4 * capPoints), 4)
  off.setUsage(35048)
  info.setUsage(35048)
  g.setAttribute('position', off)
  g.setAttribute('fInfo', info)
  g.setDrawRange(0, 0)
  g.boundingSphere = new Sphere(new Vector3(), 1e7)
  return { geometry: g, off, info }
}

/** the points material: glpoint mode on the backend's points material (gl_PointSize kept), quad mode on a mesh material */
export function makeFieldPointsMaterial(mode: 'glpoint' | 'quad', createPoints: () => PointsNodeMaterial, anchors: DataTexture): { material: PointsNodeMaterial | MeshBasicNodeMaterial; u: FieldUniforms } {
  const u: FieldUniforms = { dpr: uniform(1), viewport: uniform(new Vector2(1, 1)) }
  const m = mode === 'quad' ? new MeshBasicNodeMaterial() : createPoints()
  const info: N = attribute('fInfo', 'vec4')
  const off: N = mode === 'quad' ? attribute('fOff', 'vec3') : attribute('position', 'vec3')
  const anc: N = texture(anchors).load(ivec2(int(info.x), int(0)))
  const code: N = info.y
  const shape: N = floor(code.div(5).add(0.01))
  const ramp: N = code.sub(shape.mul(5))
  const vShape: N = varying(shape, 'vFieldShape')
  const vAlpha: N = varying(info.z, 'vFieldAlpha')
  const corner: N = varying(positionGeometry.xy, 'vFieldCorner')
  m.vertexNode = Fn(() => {
    const out: N = vec4(2, 2, 2, 1).toVar()
    const size: N = info.w.mul(u.dpr).toVar()
    If(anc.w.greaterThan(0.5).and(size.greaterThan(0.01)), () => {
      const c: N = cameraProjectionMatrix.mul(modelViewMatrix.mul(vec4(anc.xyz.add(off), 1))).toVar()
      if (mode === 'quad') c.assign(vec4(c.xy.add(positionGeometry.xy.mul(size).div(u.viewport).mul(c.w)), c.z, c.w))
      out.assign(c)
    })
    if (mode === 'glpoint') builtin('gl_PointSize').assign(max(size, float(1)))
    return out
  })() as N
  m.colorNode = vec4(varying(outputTransform(rampSelect(ramp)), 'vFieldColor') as N, 1) as N
  m.userData.awrOutputInVertex = true
  const p: N = mode === 'quad' ? corner : (pointUV as N).mul(2).sub(1)
  m.opacityNode = Fn(() => {
    shapeInside(p, vShape).lessThan(0.5).discard()
    return vAlpha
  })() as N
  m.transparent = true
  m.premultipliedAlpha = true
  m.depthWrite = false
  m.depthTest = true
  m.fog = false
  m.name = `field.points.${mode}`
  return { material: m as PointsNodeMaterial | MeshBasicNodeMaterial, u }
}

export interface FieldShellGeometry {
  geometry: BufferGeometry
  pos: BufferAttribute
  info: BufferAttribute
}

export function makeFieldShellGeometry(capVerts: number): FieldShellGeometry {
  const g = new BufferGeometry()
  const pos = new BufferAttribute(new Float32Array(3 * capVerts), 3)
  const info = new BufferAttribute(new Float32Array(4 * capVerts), 4)
  pos.setUsage(35048)
  info.setUsage(35048)
  g.setAttribute('position', pos)
  g.setAttribute('sInfo', info)
  g.setDrawRange(0, 0)
  g.boundingSphere = new Sphere(new Vector3(), 1e7)
  return { geometry: g, pos, info }
}

/** the shell material: 1 px lines, offset from the anchor, dashed where the dash coordinate is >= 0 */
export function makeFieldShellMaterial(anchors: DataTexture): LineBasicNodeMaterial {
  const m = new LineBasicNodeMaterial()
  const info: N = attribute('sInfo', 'vec4')
  const anc: N = texture(anchors).load(ivec2(int(info.x), int(0)))
  const vDash: N = varying(info.z, 'vShellDash')
  const vAlpha: N = varying(info.w, 'vShellAlpha')
  m.vertexNode = Fn(() => {
    const out: N = vec4(2, 2, 2, 1).toVar()
    If(anc.w.greaterThan(0.5), () => {
      out.assign(cameraProjectionMatrix.mul(modelViewMatrix.mul(vec4(anc.xyz.add(attribute('position', 'vec3')), 1))))
    })
    return out
  })() as N
  m.colorNode = vec4(varying(outputTransform(rampSelect(info.y)), 'vShellColor') as N, 1) as N
  m.userData.awrOutputInVertex = true
  m.opacityNode = Fn(() => {
    vDash.greaterThanEqual(0).and(fract(vDash).greaterThan(0.5)).discard()
    return vAlpha
  })() as N
  m.transparent = true
  m.premultipliedAlpha = true
  m.depthWrite = false
  m.depthTest = true
  m.fog = false
  m.name = 'field.shells'
  return m
}
