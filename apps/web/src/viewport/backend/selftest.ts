// Backend self tests (M06-FR-005, FR-006; g01 §6.3, §0 item 4; M06-AC-004). Owner: M06.
// Point size: one 4 px glpoint point into an 8 x 8 RGBA8 target, read back asynchronously; fewer than 4 lit pixels
// means the GLSL template changed (the gl_PointSize strip no longer applies) -> pointSizeMode 'pixel' and M06-E004.
// Linear RT: a uniform 0.5 linear grey quad into an RGBA8 target must read 128 +- 2 (AnetNodesHandler fix 1: no sRGB
// encoding into render targets); otherwise M06-E005 (warning only). The test build switch ?selftest=nofix renders the
// point with a stock PointsNodeMaterial to prove the degradation path.
import { BufferAttribute, BufferGeometry, Color, Mesh, OrthographicCamera, PlaneGeometry, Points, Scene, Sphere, Vector3, type RenderTarget, type WebGLRenderTarget, type WebGLRenderer } from 'three'
import { MeshBasicNodeMaterial, PointsNodeMaterial } from 'three/webgpu'
import { Fn, builtin, float, positionGeometry, positionLocal, vec3, vec4 } from 'three/tsl'
import { GLPointsNodeMaterial } from '../glPointsNodeMaterial'

type N = any // TSL nodes

export interface SelftestResult { litPx: number; pointSizeOk: boolean; linearValue: number; linearOk: boolean }
export type ReadPixels = (rt: RenderTarget, x: number, y: number, w: number, h: number, out: Uint8Array) => Promise<Uint8Array>
export type MakeRT = (w: number, h: number) => RenderTarget

export async function runSelftest(r: WebGLRenderer, makeRT: MakeRT, read: ReadPixels, o: { noFix?: boolean } = {}): Promise<SelftestResult> {
  const prev = r.getRenderTarget()
  const cam = new OrthographicCamera(0, 8, 8, 0, -10, 10)
  cam.position.z = 5
  cam.updateMatrixWorld()
  // --- point size
  const g = new BufferGeometry()
  g.setAttribute('position', new BufferAttribute(new Float32Array([4, 4, 0]), 3))
  g.boundingSphere = new Sphere(new Vector3(4, 4, 0), 8)
  const pm = o.noFix ? new PointsNodeMaterial() : new GLPointsNodeMaterial()
  pm.positionNode = Fn(() => {
    builtin('gl_PointSize').assign(float(4))
    return positionLocal
  })() as N
  pm.colorNode = vec4(1, 1, 1, 1)
  const pts = new Points(g, pm)
  pts.frustumCulled = false
  const s1 = new Scene()
  s1.add(pts)
  const rt1 = makeRT(8, 8)
  const buf1 = new Uint8Array(8 * 8 * 4)
  // --- linear RT
  const qm = new MeshBasicNodeMaterial()
  qm.colorNode = vec3(0.5, 0.5, 0.5)
  qm.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
  qm.depthTest = false
  qm.depthWrite = false
  const quad = new Mesh(new PlaneGeometry(2, 2), qm)
  quad.frustumCulled = false
  const s2 = new Scene()
  s2.add(quad)
  const rt2 = makeRT(4, 4)
  const buf2 = new Uint8Array(4 * 4 * 4)
  const clear = r.getClearColor(new Color())
  const clearA = r.getClearAlpha()
  try {
    r.setRenderTarget(rt1 as WebGLRenderTarget)
    r.setClearColor(0x000000, 1)
    r.clear()
    r.render(s1, cam)
    r.setRenderTarget(rt2 as WebGLRenderTarget)
    r.clear()
    r.render(s2, cam)
    r.setRenderTarget(prev)
    await read(rt1, 0, 0, 8, 8, buf1)
    await read(rt2, 0, 0, 4, 4, buf2)
  } finally {
    r.setRenderTarget(prev)
    r.setClearColor(clear, clearA)
    rt1.dispose()
    rt2.dispose()
    g.dispose()
    pm.dispose()
    quad.geometry.dispose()
    qm.dispose()
  }
  let lit = 0
  for (let i = 0; i < buf1.length; i += 4) if (buf1[i] > 8) lit++
  const lin = buf2[(2 * 4 + 2) * 4]
  return { litPx: lit, pointSizeOk: lit >= 4, linearValue: lin, linearOk: Math.abs(lin - 128) <= 2 }
}
