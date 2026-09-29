// GlyphLayer: screen-facing status symbols in one draw (M06-FR-036, AC-027; AWR-15 §10.4, §10.6). Owner: M06.
// Attribute-less flat quads (drawRange = 6 n); per instance two RGBA32F texels in GlyphTex: (pos.xyz, diameter CSS px)
// and (shape, line width CSS px, palette index, alpha). Shapes are SDFs in raster pixels: 0 ring, 1 filled disc,
// 2 octagon ring, 3 triangle ring (apex up), 4 dashed ring (2w on, 4w off), 5 diamond ring, 6 crosshair + ring,
// 7 GoTo marker (ring + centre dot). Palette indices follow lib/tokens/scene.gen.ts glyphPalette (0 g50, 1 r500, 2 g500,
// 3 g400, 4 g300, 5 g200), each a uniform of this material (no recompiles on colour changes). Depth test off: symbols
// are always visible. Writers push into six priority classes; commit() writes red entity > other critical > warning >
// selection > waypoints/targets > stale until the cap (Tier S 256, B/A 1024) and counts the truncated rest
// (VIS_GLYPH_CAP, M06-E014). CPU-side animations (appear scale, breathing, fades) only change sizes and alphas.
import { BufferGeometry, Mesh, Sphere, Vector2, Vector3, type DataTexture } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, abs, atan, clamp, dot, float, length, max, min, mod, select, sign, uniform, varyingProperty, vec2, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { clipQuad, hiddenClip, instanceTexel, makeInstanceTexture, quadVertex } from '../screenQuad'

type N = any // TSL nodes

export const Shape = { Ring: 0, Disc: 1, Octagon: 2, Triangle: 3, Dashed: 4, Diamond: 5, Crosshair: 6, Goto: 7 } as const
export const Palette = { G50: 0, R500: 1, G500: 2, G400: 3, G300: 4, G200: 5 } as const
/** priority classes, in truncation order */
export const GlyphClass = { Red: 0, Critical: 1, Warning: 2, Selection: 3, Mission: 4, Stale: 5 } as const
export const GLYPH = { capS: 256, capBA: 1024, texLog2W: 11, classes: 6 } as const

const STRIDE = 8

export class GlyphLayer {
  readonly mesh: Mesh
  readonly tex: DataTexture
  private readonly data: Float32Array
  private readonly staging: Float32Array[]
  private readonly counts: Int32Array
  private readonly uDpr: N
  private readonly uViewport: N
  readonly palette: N[]
  n = 0
  /** glyphs dropped in the last commit (class overflow + cap) and since start (VIS_GLYPH_CAP) */
  truncated = 0
  truncatedTotal = 0
  private overflow = 0

  constructor(readonly cap: number) {
    this.tex = makeInstanceTexture(1 << GLYPH.texLog2W, Math.max(1, Math.ceil((2 * cap) / (1 << GLYPH.texLog2W))), 'GlyphTex')
    this.data = this.tex.image.data as Float32Array
    this.staging = Array.from({ length: GLYPH.classes }, () => new Float32Array(cap * STRIDE))
    this.counts = new Int32Array(GLYPH.classes)
    this.uDpr = uniform(1)
    this.uViewport = uniform(new Vector2(1, 1))
    this.palette = SCENE.glyphPalette.map((c) => uniform(new Vector3(c[0], c[1], c[2])))
    const vQ = varyingProperty('vec2', 'vGlyphQ')
    const vA = varyingProperty('vec4', 'vGlyphA') // shape, line width px, palette, alpha
    const vR = varyingProperty('vec2', 'vGlyphR') // ring radius px, half quad px
    const tex = this.tex
    const dpr = this.uDpr
    const vp = this.uViewport
    const m = new MeshBasicNodeMaterial()
    m.vertexNode = Fn(() => {
      const { iid, corner } = quadVertex()
      const t0: N = instanceTexel(tex, iid, 0, 2, GLYPH.texLog2W)
      const t1: N = instanceTexel(tex, iid, 1, 2, GLYPH.texLog2W)
      const R: N = t0.w.mul(dpr).mul(0.5)
      const w: N = max(t1.y.mul(dpr), float(1))
      const half: N = R.add(w).add(2)
      vQ.assign(corner.mul(half))
      vA.assign(vec4(t1.x, w, t1.z, t1.w))
      vR.assign(vec2(R, half))
      return select(t1.w.lessThanEqual(0), hiddenClip(), clipQuad(t0.xyz, corner, half, vp))
    })() as N
    const pal = this.palette
    m.colorNode = Fn(() => {
      const p: N = vQ
      const shape: N = vA.x
      const w: N = vA.y
      const R: N = vR.x
      const ring: N = abs(length(p).sub(R)).sub(w.mul(0.5))
      // octagon (regular, circumradius ~ R), iq's sdOctogon
      const ko: N = vec2(-0.9238795325, 0.3826834323)
      let q: N = abs(p)
      q = q.sub(ko.mul(min(dot(ko, q), float(0)).mul(2)))
      const ko2: N = vec2(0.9238795325, 0.3826834323)
      q = q.sub(ko2.mul(min(dot(ko2, q), float(0)).mul(2)))
      const ro: N = R.mul(0.92388)
      const qo: N = q.sub(vec2(clamp(q.x, ro.mul(-0.4142135623), ro.mul(0.4142135623)), ro))
      const oct: N = abs(length(qo).mul(sign(qo.y))).sub(w.mul(0.5))
      // equilateral triangle, apex up (iq's sdEquilateralTriangle), circumradius R
      const k3 = Math.sqrt(3)
      const rt: N = R.mul(0.8660254)
      const tx: N = abs(p.x).sub(rt)
      const ty: N = p.y.add(rt.div(k3))
      const flip: N = tx.add(ty.mul(k3)).greaterThan(0)
      const px2: N = select(flip, tx.sub(ty.mul(k3)).mul(0.5), tx)
      const py2: N = select(flip, tx.mul(-k3).sub(ty).mul(0.5), ty)
      const px3: N = px2.sub(clamp(px2, rt.mul(-2), float(0)))
      const tri: N = abs(length(vec2(px3, py2)).mul(sign(py2)).negate()).sub(w.mul(0.5))
      // dashed ring: 2w dash, 4w gap along the circumference
      const arc: N = atan(p.y, p.x).add(Math.PI).mul(R)
      const dashOn: N = mod(arc, w.mul(6)).lessThan(w.mul(2))
      const dashed: N = select(dashOn, ring, float(1e3))
      const disc: N = length(p).sub(R)
      const diamond: N = abs(abs(p.x).add(abs(p.y)).sub(R).mul(0.7071068)).sub(w.mul(0.5))
      const crossArm: N = min(max(abs(p.x).sub(w.mul(0.5)), abs(p.y).sub(R.mul(1.3))), max(abs(p.y).sub(w.mul(0.5)), abs(p.x).sub(R.mul(1.3))))
      const cross: N = min(ring, max(crossArm, R.mul(0.45).sub(length(p))))
      const gotoD: N = min(ring, length(p).sub(max(R.mul(0.3), float(1.5))))
      const d: N = select(shape.lessThan(0.5), ring, select(shape.lessThan(1.5), disc, select(shape.lessThan(2.5), oct, select(shape.lessThan(3.5), tri,
        select(shape.lessThan(4.5), dashed, select(shape.lessThan(5.5), diamond, select(shape.lessThan(6.5), cross, gotoD)))))))
      const cov: N = clamp(float(0.5).sub(d), float(0), float(1))
      cov.lessThanEqual(0.004).discard()
      const idx: N = vA.z
      const c: N = select(idx.lessThan(0.5), pal[0], select(idx.lessThan(1.5), pal[1], select(idx.lessThan(2.5), pal[2],
        select(idx.lessThan(3.5), pal[3], select(idx.lessThan(4.5), pal[4], pal[5])))))
      return vec4(c, cov.mul(vA.w))
    })() as N
    m.depthTest = false
    m.depthWrite = false
    m.transparent = true
    m.fog = false
    const g = new BufferGeometry()
    g.setDrawRange(0, 0)
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    this.mesh = new Mesh(g, m)
    this.mesh.frustumCulled = false
    this.mesh.renderOrder = 90
    this.mesh.name = 'GlyphLayer'
    this.mesh.visible = false
  }

  begin(): void {
    this.counts.fill(0)
    this.overflow = 0
  }

  /** stage one glyph; returns false when its class is full (the caller may stop) */
  push(cls: number, x: number, y: number, z: number, sizeCss: number, shape: number, lineCss: number, palette: number, alpha = 1): boolean {
    const k = this.counts[cls]
    if (k >= this.cap) {
      this.overflow++
      return false
    }
    const s = this.staging[cls]
    const o = k * STRIDE
    s[o] = x
    s[o + 1] = y
    s[o + 2] = z
    s[o + 3] = sizeCss
    s[o + 4] = shape
    s[o + 5] = lineCss
    s[o + 6] = palette
    s[o + 7] = alpha
    this.counts[cls] = k + 1
    return true
  }

  /** write the classes in priority order up to the cap; upload; returns the instance count */
  commit(dpr: number, dbW: number, dbH: number): number {
    let n = 0
    let dropped = 0
    const d = this.data
    for (let c = 0; c < GLYPH.classes; c++) {
      const k = this.counts[c]
      const s = this.staging[c]
      for (let i = 0; i < k; i++) {
        if (n >= this.cap) {
          dropped += k - i
          break
        }
        const so = i * STRIDE
        const o = n * STRIDE
        for (let j = 0; j < STRIDE; j++) d[o + j] = s[so + j]
        n++
      }
    }
    this.truncated = dropped + this.overflow
    this.truncatedTotal += this.truncated
    this.n = n
    this.mesh.geometry.setDrawRange(0, 6 * n)
    this.mesh.visible = n > 0
    this.uDpr.value = dpr
    ;(this.uViewport.value as Vector2).set(Math.max(1, dbW), Math.max(1, dbH))
    if (n > 0) this.tex.needsUpdate = true
    return n
  }

  drawCount(): number {
    return this.mesh.visible ? 1 : 0
  }

  dispose(): void {
    this.mesh.geometry.dispose()
    ;(this.mesh.material as MeshBasicNodeMaterial).dispose()
    this.tex.dispose()
  }
}
