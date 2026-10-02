// Feature matrix regression page on WebGPURenderer (Tier A, D1-AC-14 P1; g01 §3, results/F_feat_wgpu.json). Owner: M06.
// Test builds only (lazy chunk of featMatrix.ts, reached through window.__vp.featMatrix({ tier: 'A' })). The product has no
// Tier A backend in D1 (ADR-044: Tier A is D1-ext and falls back to the classic path, M06-E013); D1 accepts the WebGPU
// column of the matrix on this regression page: the same 28 items + PointPool as the classic page, rendered by a
// WebGPURenderer on the WebGPU backend (SwiftShader WebGPU with the C2 flag set; a fallback adapter is accepted only with
// ?allowFallback=1, which the case passes) into 128 x 128 RGBA8 render targets and read back asynchronously. Items that
// WebGPU cannot do are recorded as such and are part of the expectation (no point-size builtin in WGSL, GLSL
// ShaderMaterial unsupported, 1 px PointPool). Expected values: perf/m06/featMatrix.expected.ts WGPU. Colours are test
// fixtures, not UI colours.
import {
  AlwaysDepth, AmbientLight, BufferAttribute, BufferGeometry, Color, Data3DTexture, DataTexture, DepthTexture, DirectionalLight, DoubleSide, Fog,
  FloatType, HalfFloatType, InstancedBufferAttribute, InstancedBufferGeometry, InstancedMesh, Line, Line2NodeMaterial, LineBasicNodeMaterial,
  LinearFilter, LinearSRGBColorSpace, Matrix4, Mesh, MeshBasicNodeMaterial, MeshStandardNodeMaterial, NoBlending, NoToneMapping, OrthographicCamera,
  PerspectiveCamera, PlaneGeometry, Points, PointsNodeMaterial, RenderPipeline, RenderTarget, RGBAIntegerFormat, Scene, ShaderMaterial, Sphere,
  SphereGeometry, UnsignedByteType, UnsignedIntType, Vector3, WebGPURenderer,
} from 'three/webgpu'
import {
  Fn, If, Loop, attribute, cameraPosition, clamp, color, context, exp, float, floor, fog, int, ivec2, length, log2, max, mix, mod, normalize, pass,
  perspectiveDepthToViewZ, positionGeometry, positionWorld, sRGBTransferOETF, select, step, texture, texture3D, textureLoad, uint, uniform, uv,
  varyingProperty, vec2, vec3, vec4, vertexIndex,
} from 'three/tsl'
import type { FeatMatrix, FeatResult } from './featMatrix'

type N = any // TSL nodes
const W = 128
const H = 128
const WGSL_NO_POINT_SIZE = 'WGSL has no point size builtin'

const ortho = (): OrthographicCamera => {
  const c = new OrthographicCamera(0, W, H, 0, -100, 100)
  c.position.z = 10
  c.updateMatrixWorld()
  return c
}
const persp = (): PerspectiveCamera => {
  const c = new PerspectiveCamera(60, W / H, 1, 200)
  c.position.set(0, 0, 0)
  c.lookAt(0, 0, -1)
  c.updateMatrixWorld()
  return c
}
const lit = (b: Uint8Array, ch = 0, thr = 8): number => {
  let n = 0
  for (let i = 0; i < b.length; i += 4) if (b[i + ch] > thr) n++
  return n
}
const px = (b: Uint8Array, x: number, y: number): number[] => {
  const i = (y * W + x) * 4
  return [b[i], b[i + 1], b[i + 2], b[i + 3]]
}
function gridPoints(n = 400, off = 0.5): BufferGeometry {
  const pos = new Float32Array(n * 3)
  const col = new Uint8Array(n * 4)
  for (let i = 0; i < n; i++) {
    pos[i * 3] = 10 + (i % 20) * 6 + off
    pos[i * 3 + 1] = 10 + Math.floor(i / 20) * 6 + off
    col.set([255, 255, 255, 255], i * 4)
  }
  const g = new BufferGeometry()
  g.setAttribute('position', new BufferAttribute(pos, 3))
  g.setAttribute('color4', new BufferAttribute(col, 4, true))
  g.boundingSphere = new Sphere(new Vector3(64, 64, 0), 128)
  return g
}
function fsq(mat: MeshBasicNodeMaterial): Mesh {
  const m = new Mesh(new PlaneGeometry(2, 2), mat)
  mat.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
  mat.depthTest = false
  mat.depthWrite = false
  m.frustumCulled = false
  return m
}
function fogPlane(): Mesh {
  const m = new MeshBasicNodeMaterial()
  m.colorNode = vec3(1, 1, 1) as N
  const o = new Mesh(new PlaneGeometry(W, H), m)
  o.position.set(W / 2, H / 2, 0)
  return o
}
function makeRT(w: number, h: number, depthTex = false): RenderTarget {
  const rt = new RenderTarget(w, h, { depthBuffer: true, type: UnsignedByteType })
  if (depthTex) {
    rt.depthTexture = new DepthTexture(w, h)
    rt.depthTexture.type = FloatType
  }
  return rt
}
async function read(r: WebGPURenderer, rt: RenderTarget): Promise<Uint8Array> {
  const a = (await r.readRenderTargetPixelsAsync(rt, 0, 0, rt.width, rt.height)) as Uint8Array
  return new Uint8Array(a.buffer, a.byteOffset, rt.width * rt.height * 4)
}
async function renderToRT(r: WebGPURenderer, rt: RenderTarget, scene: Scene | Mesh, camera: OrthographicCamera | PerspectiveCamera): Promise<Uint8Array> {
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.render(scene as Scene, camera)
  r.setRenderTarget(null)
  return read(r, rt)
}

/** a WebGPURenderer on the WebGPU backend (never its WebGL2 fallback); null when WebGPU is not available */
async function wgpu(o: { reversed?: boolean; allowFallback?: boolean; direct?: boolean } = {}): Promise<WebGPURenderer | null> {
  const gpu = (navigator as unknown as { gpu?: { requestAdapter(o?: object): Promise<{ info?: { isFallbackAdapter?: boolean } } | null> } }).gpu
  if (!gpu) return null
  const ad = await gpu.requestAdapter().catch(() => null)
  if (!ad || (ad.info?.isFallbackAdapter === true && !o.allowFallback)) return null
  const r = new WebGPURenderer({ antialias: false, forceWebGL: false, reversedDepthBuffer: o.reversed === true, outputBufferType: HalfFloatType })
  await r.init()
  const be = r.backend as unknown as { isWebGPUBackend?: boolean }
  if (be.isWebGPUBackend !== true) {
    void r.dispose()
    return null
  }
  r.setPixelRatio(1)
  r.setSize(W, H, false)
  if (o.direct) {
    // 'direct' output (g01 §0): no frame-buffer target and output pass; the sRGB transfer goes into every material
    r.outputColorSpace = LinearSRGBColorSpace
    r.toneMapping = NoToneMapping
    const getOutput = (out: N, b: { renderer: WebGPURenderer }): N => (b.renderer.getRenderTarget() === null ? (vec4 as N)(sRGBTransferOETF(out.rgb), out.a) : out)
    ;(r as unknown as { contextNode: unknown }).contextNode = (context as N)({ getOutput })
  }
  return r
}

function adapterText(r: WebGPURenderer): string {
  const be = r.backend as unknown as { adapter?: { info?: { vendor?: string; architecture?: string; isFallbackAdapter?: boolean } } }
  const i = be.adapter?.info
  return i ? `${i.vendor ?? ''}/${i.architecture ?? ''}/fallback=${i.isFallbackAdapter === true}` : 'webgpu'
}

export async function runFeatMatrixWgpu(o: { allowFallback?: boolean } = {}): Promise<FeatMatrix> {
  const r = await wgpu({ allowFallback: o.allowFallback })
  if (!r) return { backend: 'unavailable', tier: 'A', renderer: '', tests: {} }
  const out: FeatMatrix = { backend: 'wgpu', tier: 'A', renderer: adapterText(r), tests: {} }
  const rt = makeRT(W, H)
  // console errors and warnings raised by an item are part of its record (GLSL ShaderMaterial: "not compatible")
  const errs: string[] = []
  const origErr = console.error
  const test = async (id: string, fn: () => Promise<FeatResult> | FeatResult): Promise<void> => {
    errs.length = 0
    console.error = (...a: unknown[]) => {
      errs.push(a.map(String).join(' ').slice(0, 300))
    }
    try {
      out.tests[id] = await fn()
    } catch (e) {
      out.tests[id] = { exception: String((e as Error)?.message ?? e).slice(0, 300) }
    } finally {
      console.error = origErr
    }
    if (errs.length) out.tests[id] = { ...out.tests[id], errors: [...errs] }
  }
  await test('points_1px', async () => {
    const s = new Scene()
    const m = new PointsNodeMaterial()
    m.colorNode = (attribute('color4') as N).rgb
    s.add(new Points(gridPoints(), m))
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('points_glPointSize_nofix', () => ({ skipped: WGSL_NO_POINT_SIZE }))
  await test('points_glPointSize_fixed', () => ({ skipped: WGSL_NO_POINT_SIZE }))
  await test('points_perObjectSize', () => ({ skipped: WGSL_NO_POINT_SIZE }))
  await test('points_glsl_ShaderMaterial', async () => {
    const s = new Scene()
    const m = new ShaderMaterial({
      vertexShader: 'void main(){ gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); gl_PointSize = 4.0; }',
      fragmentShader: 'void main(){ gl_FragColor = vec4(1.0); }',
    })
    s.add(new Points(gridPoints(), m))
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('sprite_quad_instanced', async () => {
    const s = new Scene()
    const src = gridPoints(400, 0)
    const g = new InstancedBufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array([-0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0]), 3))
    g.setIndex([0, 1, 2, 0, 2, 3])
    g.setAttribute('ipos', new InstancedBufferAttribute(src.getAttribute('position').array as Float32Array, 3))
    g.instanceCount = 400
    const m = new PointsNodeMaterial({ sizeAttenuation: false })
    m.positionNode = attribute('ipos') as N
    m.sizeNode = float(4) as N
    m.colorNode = vec3(1, 1, 1) as N
    const ob = new Mesh(g, m)
    ob.frustumCulled = false
    s.add(ob)
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('vertexIndex_flatQuads', async () => {
    const s = new Scene()
    const n = 400
    const g = new BufferGeometry()
    g.setDrawRange(0, n * 6)
    g.boundingSphere = new Sphere(new Vector3(64, 64, 0), 200)
    const m = new MeshBasicNodeMaterial({ side: DoubleSide })
    m.positionNode = Fn(() => {
      const vf: N = float(vertexIndex)
      const pid: N = floor(vf.div(6))
      const c: N = vf.sub(pid.mul(6))
      const cx: N = step(0.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c).mul(float(1).sub(step(4.5, c))))
      const cy: N = step(1.5, c).mul(float(1).sub(step(2.5, c))).add(step(3.5, c))
      const ix: N = mod(pid, 20)
      const iy: N = floor(pid.div(20))
      return vec3(ix.mul(6).add(10).add(cx.sub(0.5).mul(4)), iy.mul(6).add(10).add(cy.sub(0.5).mul(4)), 0)
    })() as N
    m.colorNode = vec3(1, 1, 1) as N
    const ob = new Mesh(g, m)
    ob.frustumCulled = false
    s.add(ob)
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('fog_sceneFog_classic', async () => {
    const s = new Scene()
    s.fog = new Fog(new Color(1, 0, 0), 0.001, 0.002)
    s.add(fogPlane())
    return { center: px(await renderToRT(r, rt, s, ortho()), 64, 64) }
  })
  await test('fog_sceneFogNode_customFn', async () => {
    const s = new Scene() as Scene & { fogNode: unknown }
    s.fogNode = fog(color(1, 0, 0), Fn(() => float(1).sub(exp((positionWorld as N).sub(cameraPosition).length().mul(-10))))())
    s.add(fogPlane())
    return { center: px(await renderToRT(r, rt, s, ortho()), 64, 64) }
  })
  await test('fog_sceneFogNode_stockHandler', () => ({ skipped: 'classic only' }))
  await test('fog_inMaterial_Fn', async () => {
    const s = new Scene()
    const ob = fogPlane()
    const hfog = Fn(([c]: [N]) => {
      const d: N = (positionWorld as N).sub(cameraPosition).length()
      const f: N = float(1).sub(exp(d.mul(-10).mul(exp((positionWorld as N).y.mul(-0.0001)))))
      return mix(c, vec3(1, 0, 0), clamp(f, 0, 1))
    })
    ;(ob.material as MeshBasicNodeMaterial).colorNode = hfog(vec3(1, 1, 1)) as N
    s.add(ob)
    return { center: px(await renderToRT(r, rt, s, ortho()), 64, 64) }
  })
  await test('onObjectUpdate_sharedMaterial', async () => {
    const s = new Scene()
    const m = new MeshBasicNodeMaterial()
    const v: N = (uniform(0) as N).onObjectUpdate(({ object }: { object: { userData: { v: number } } }) => object.userData.v)
    m.colorNode = vec3(v, 0, 0) as N
    const vals = [0.25, 0.5, 0.75, 1.0]
    vals.forEach((val, i) => {
      const ob = new Mesh(new PlaneGeometry(28, 28), m)
      ob.position.set(16 + i * 32, 64, 0)
      ob.userData.v = val
      s.add(ob)
    })
    const b = await renderToRT(r, rt, s, ortho())
    return { reds: vals.map((_, i) => px(b, 16 + i * 32, 64)[0]) }
  })
  await test('onRender_onFrame_update', async () => {
    const s = new Scene()
    let k = 0
    let f = 0
    const a: N = (uniform(0) as N).onRenderUpdate(() => ++k / 10)
    const b0: N = (uniform(0) as N).onFrameUpdate(() => ++f / 10)
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec3(a, b0, 0) as N
    const ob = fogPlane()
    ob.material = m
    s.add(ob)
    const x1 = px(await renderToRT(r, rt, s, ortho()), 64, 64)
    const x2 = px(await renderToRT(r, rt, s, ortho()), 64, 64)
    return { first: x1, second: x2, calls: { render: k, frame: f } }
  })
  const instanced = (shared: boolean): Scene => {
    const s = new Scene()
    const sm = new MeshBasicNodeMaterial()
    sm.colorNode = vec3(1, 1, 1) as N
    const mk = (x0: number): InstancedMesh => {
      const m = shared ? sm : new MeshBasicNodeMaterial()
      if (!shared) m.colorNode = vec3(1, 1, 1) as N
      const im = new InstancedMesh(new PlaneGeometry(10, 10), m, 4)
      const M = new Matrix4()
      for (let i = 0; i < 4; i++) im.setMatrixAt(i, M.makeTranslation(x0, 16 + i * 28, 0))
      im.frustumCulled = false
      return im
    }
    s.add(mk(32), mk(96))
    return s
  }
  const lr = (b: Uint8Array): { leftPx: number; rightPx: number } => {
    let L = 0
    let R = 0
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        if (b[(y * W + x) * 4] <= 8) continue
        if (x < 64) L++
        else R++
      }
    }
    return { leftPx: L, rightPx: R }
  }
  await test('instancedMesh_sharedMaterial', async () => lr(await renderToRT(r, rt, instanced(true), ortho())))
  await test('instancedMesh_ownMaterials', async () => lr(await renderToRT(r, rt, instanced(false), ortho())))
  const depthPipelines = async (reversed: boolean): Promise<FeatResult> => {
    const rr = await wgpu({ reversed, allowFallback: o.allowFallback })
    if (!rr) return { unavailable: 'WebGPU' }
    const cam = persp()
    const sceneRT = makeRT(W, H, true)
    const outRT = makeRT(W, H)
    const s = new Scene()
    const mA = new MeshBasicNodeMaterial()
    mA.colorNode = vec3(0.6, 0.6, 0.6) as N
    const A = new Mesh(new PlaneGeometry(400, 400), mA)
    A.position.z = -80
    const mB = new MeshBasicNodeMaterial()
    mB.colorNode = vec3(0.6, 0.6, 0.6) as N
    const B = new Mesh(new PlaneGeometry(20, 20), mB)
    B.position.z = -40
    s.add(A, B)
    rr.setRenderTarget(sceneRT)
    rr.setClearColor(0x000000, 1)
    rr.clear()
    rr.render(s, cam)
    rr.setRenderTarget(null)
    const near: N = uniform(cam.near)
    const far: N = uniform(cam.far)
    const rev = (rr as unknown as { reversedDepthBuffer: boolean }).reversedDepthBuffer === true
    const depthT: N = texture(sceneRT.depthTexture!)
    const colorT: N = texture(sceneRT.texture)
    const m1 = new MeshBasicNodeMaterial()
    m1.colorNode = Fn(() => {
      const d: N = depthT.sample(uv()).r
      return vec3(perspectiveDepthToViewZ(d, near, far).negate().div(100))
    })() as N
    const lin = await renderToRT(rr, outRT, fsq(m1), cam)
    const texel: N = vec2(1 / W, 1 / H)
    const logZ = Fn(([st]: [N]) => {
      const d: N = depthT.sample(st).r
      const bg: N = rev ? d.lessThanEqual(1e-7) : d.greaterThanEqual(0.9999999)
      return select(bg, float(0), log2(perspectiveDepthToViewZ(d, near, far).negate()))
    })
    const m2 = new MeshBasicNodeMaterial()
    m2.colorNode = Fn(() => {
      const st: N = uv()
      const z0: N = logZ(st).toVar()
      const sum: N = float(0).toVar()
      for (let i = 0; i < 8; i++) {
        const ang = (i * Math.PI) / 4
        const nz: N = logZ(st.add(texel.mul(vec2(Math.cos(ang), Math.sin(ang)).mul(1.5)))).toVar()
        If(nz.notEqual(0), () => {
          sum.addAssign(max(0, z0.sub(nz)))
        })
      }
      return colorT.sample(st).rgb.mul(exp(sum.div(8).mul(-300)))
    })() as N
    const edl = await renderToRT(rr, outRT, fsq(m2), cam)
    let dark = 0
    for (let i = 0; i < edl.length; i += 4) if (edl[i] < 40) dark++
    const cloudRT = makeRT(W / 2, H / 2)
    const m3 = new MeshBasicNodeMaterial({ transparent: false })
    const tanH = Math.tan(Math.PI / 6)
    m3.colorNode = Fn(() => {
      const p: N = uv().mul(2).sub(1)
      const dir: N = normalize(vec3(p.x.mul(tanH), p.y.mul(tanH), -1))
      const T: N = float(1).toVar()
      Loop(32, ({ i }: { i: N }) => {
        const t: N = float(i).mul(2).add(40)
        const pos: N = dir.mul(t)
        const d: N = length(pos.sub(vec3(20, 0, -64)))
        T.mulAssign(select(d.lessThan(14), float(0.8), float(1)))
      })
      return vec3(float(1).sub(T))
    })() as N
    await renderToRT(rr, cloudRT, fsq(m3), cam)
    const cloudT: N = texture(cloudRT.texture)
    cloudT.value.minFilter = cloudT.value.magFilter = LinearFilter
    const m4 = new MeshBasicNodeMaterial()
    m4.colorNode = Fn(() => {
      const st: N = uv()
      const vz: N = perspectiveDepthToViewZ(depthT.sample(st).r, near, far).negate()
      const a: N = cloudT.sample(st).r.mul(select(vz.greaterThan(50), float(1), float(0)))
      return mix(colorT.sample(st).rgb, vec3(1, 0, 0), a)
    })() as N
    const comp = await renderToRT(rr, outRT, fsq(m4), cam)
    let cloudPx = 0
    for (let i = 0; i < comp.length; i += 4) if (comp[i] > comp[i + 1] + 30) cloudPx++
    void rr.dispose()
    return {
      reversedActive: rev, linDepthCenter: px(lin, 64, 64)[0], linDepthCorner: px(lin, 4, 4)[0], edlDarkPx: dark, edlCenter: px(edl, 64, 64),
      edlCorner: px(edl, 4, 4), cloudTintedPx: cloudPx, compCenter: px(comp, 64, 64),
    }
  }
  await test('depthTex_EDL_cloud_fsq', () => depthPipelines(false))
  await test('depthTex_EDL_cloud_fsq_reversedZ', () => depthPipelines(true))
  await test('rt_vs_screen_encoding', async () => {
    const s = new Scene()
    const ob = fogPlane()
    ;(ob.material as MeshBasicNodeMaterial).colorNode = vec3(0.5, 0.5, 0.5) as N
    s.add(ob)
    return { inRT: px(await renderToRT(r, rt, s, ortho()), 64, 64) }
  })
  await test('direct_contextNode_encoding', async () => {
    const r2 = await wgpu({ direct: true, allowFallback: o.allowFallback })
    if (!r2) return { unavailable: 'WebGPU' }
    const s = new Scene()
    const ob = fogPlane()
    ;(ob.material as MeshBasicNodeMaterial).colorNode = vec3(0.5, 0.5, 0.5) as N
    s.add(ob)
    const cam = ortho()
    const c2 = document.createElement('canvas')
    c2.width = W
    c2.height = H
    const ctx2 = c2.getContext('2d', { willReadFrequently: true })!
    // the WebGPU canvas is read through a 2D canvas (headless SwiftShader WebGPU yields transparent black here, g01)
    const readScreen = (): number[] => {
      ctx2.drawImage(r2.domElement, 0, 0)
      return [...ctx2.getImageData(64, 64, 1, 1).data]
    }
    r2.setRenderTarget(null)
    r2.render(s, cam)
    const screen = readScreen()
    const rt2 = makeRT(W, H)
    const inRT = px(await renderToRT(r2, rt2, s, cam), 64, 64)
    r2.setRenderTarget(null)
    r2.render(s, cam)
    const screenAgain = readScreen()
    const needsFrameBufferTarget = (r2 as unknown as { needsFrameBufferTarget: boolean }).needsFrameBufferTarget
    void r2.dispose()
    return { screen, inRT, screenAgain, needsFrameBufferTarget }
  })
  await test('mask_alpha_and_fsq_depthWrite', async () => {
    const cam = persp()
    const sceneRT = makeRT(W, H, true)
    const s = new Scene()
    const mA = new MeshBasicNodeMaterial()
    mA.colorNode = vec3(0.6, 0.6, 0.6) as N
    mA.opacityNode = float(1.0) as N
    const A = new Mesh(new PlaneGeometry(400, 400), mA)
    A.position.z = -80
    // method 2 of g01 §0 item 2 (as on the classic page): the non-cloud layer marks alpha 0.5 with transparent + NoBlending
    const mB = new MeshBasicNodeMaterial({ transparent: true, blending: NoBlending })
    mB.colorNode = vec3(0.6, 0.6, 0.6) as N
    mB.opacityNode = float(0.5) as N
    const B = new Mesh(new PlaneGeometry(20, 20), mB)
    B.position.z = -40
    s.add(A, B)
    r.setRenderTarget(sceneRT)
    r.setClearColor(0x000000, 0)
    r.clear()
    r.render(s, cam)
    r.setRenderTarget(null)
    const raw = await read(r, sceneRT)
    const outRT = makeRT(W, H)
    const fm = new MeshBasicNodeMaterial()
    fm.colorNode = (texture(sceneRT.texture) as N).rgb
    fm.depthNode = (texture(sceneRT.depthTexture!) as N).r
    fm.vertexNode = vec4((positionGeometry as N).xy, 0, 1) as N
    fm.depthTest = true
    fm.depthWrite = true
    fm.depthFunc = AlwaysDepth
    const q = new Mesh(new PlaneGeometry(2, 2), fm)
    q.frustumCulled = false
    const mC = new MeshBasicNodeMaterial()
    mC.colorNode = vec3(1, 0, 0) as N
    const C = new Mesh(new PlaneGeometry(400, 400), mC)
    C.position.z = -60
    const s2 = new Scene()
    s2.add(q)
    const s3 = new Scene()
    s3.add(C)
    const ac = r.autoClear
    r.setRenderTarget(outRT)
    r.setClearColor(0x000000, 1)
    r.clear()
    r.autoClear = false
    r.render(s2, cam)
    r.render(s3, cam)
    r.autoClear = ac
    r.setRenderTarget(null)
    const ob = await read(r, outRT)
    return { alphaCorner: px(raw, 4, 4)[3], alphaCenter: px(raw, 64, 64)[3], outCorner: px(ob, 4, 4), outCenter: px(ob, 64, 64) }
  })
  await test('line2_node_fatline', async () => {
    const { Line2 } = await import('three/addons/lines/webgpu/Line2.js')
    const { LineGeometry } = await import('three/addons/lines/LineGeometry.js')
    const g = new LineGeometry()
    g.setPositions([10, 10, 0, 118, 118, 0])
    const m = new Line2NodeMaterial({ color: new Color(1, 1, 1), linewidth: 6, worldUnits: false })
    const l = new Line2(g, m)
    l.computeLineDistances()
    const s = new Scene()
    s.add(l)
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('readback_row_order', async () => {
    const s = new Scene()
    const m = new MeshBasicNodeMaterial()
    m.colorNode = vec3(1, 1, 1) as N
    const ob = new Mesh(new PlaneGeometry(W, H / 2), m)
    ob.position.set(W / 2, H * 0.75, 0)
    s.add(ob)
    // WebGPU reads rows top-down (row 0 = top), the opposite of the GL convention of the classic page
    const b = await renderToRT(r, rt, s, ortho())
    return { row100: px(b, 64, 100)[0], row20: px(b, 64, 20)[0] }
  })
  await test('lit_standard', async () => {
    const s = new Scene()
    s.add(new AmbientLight(new Color(1, 1, 1), 0.2))
    const dl = new DirectionalLight(new Color(1, 1, 1), 2)
    dl.position.set(1, 1, 1)
    s.add(dl, dl.target)
    const ob = new Mesh(new SphereGeometry(20, 32, 16), new MeshStandardNodeMaterial({ color: new Color().setRGB(0.2158605, 0.2158605, 1), roughness: 0.6 }))
    ob.position.z = -60
    s.add(ob)
    const b = await renderToRT(r, rt, s, persp())
    return { center: px(b, 64, 64), upperRight: px(b, 74, 74), lowerLeft: px(b, 54, 54) }
  })
  await test('texture3D', async () => {
    const d = new Uint8Array(4 * 4 * 4 * 4)
    for (let i = 0; i < 64; i++) d.set([200, 100, 50, 255], i * 4)
    const t3 = new Data3DTexture(d, 4, 4, 4)
    t3.needsUpdate = true
    const s = new Scene()
    const ob = fogPlane()
    ;(ob.material as MeshBasicNodeMaterial).colorNode = (texture3D(t3, vec3(0.5, 0.5, 0.5)) as N).rgb
    s.add(ob)
    return { center: px(await renderToRT(r, rt, s, ortho()), 64, 64) }
  })
  await test('line_basic_node', async () => {
    const s = new Scene()
    const g = new BufferGeometry().setFromPoints([new Vector3(10, 10, 0), new Vector3(118, 118, 0)])
    s.add(new Line(g, new LineBasicNodeMaterial({ color: new Color(1, 1, 1) })))
    return { litPx: lit(await renderToRT(r, rt, s, ortho())) }
  })
  await test('renderPipeline_pass', () => {
    const s = new Scene()
    const mA = new MeshBasicNodeMaterial()
    mA.colorNode = vec3(0.6, 0.6, 0.6) as N
    const A = new Mesh(new PlaneGeometry(400, 400), mA)
    A.position.z = -80
    s.add(A)
    const pp = new RenderPipeline(r)
    const sp: N = pass(s, persp())
    pp.outputNode = sp.getTextureNode('output').mul(vec4(1, 0, 0, 1))
    r.setRenderTarget(null)
    pp.render()
    pp.dispose()
    return { ok: true }
  })
  await test('compute', () => ({ available: typeof (r as unknown as { compute?: unknown }).compute === 'function' }))
  await test('handler_sideEffects', async () => {
    const s = new Scene()
    const m = new MeshBasicNodeMaterial()
    let obr = 0
    ;(m as unknown as { onBeforeRender: () => void }).onBeforeRender = () => {
      obr++
    }
    const g = new PlaneGeometry(10, 10)
    let disposed = 0
    g.addEventListener('dispose', () => disposed++)
    for (let i = 0; i < 8; i++) {
      const ob = new Mesh(i ? new PlaneGeometry(10, 10) : g, m)
      ob.position.set(10 + i * 12, 64, 0)
      s.add(ob)
    }
    // WebGPURenderer's info.render has no frame counter: the delta is null (g01 F_feat_wgpu.json)
    const info = r.info.render as unknown as Record<string, number | undefined>
    const f0 = info['frame']
    await renderToRT(r, rt, s, ortho())
    await new Promise((res) => setTimeout(res, 0))
    const f1 = info['frame']
    const delta = typeof f0 === 'number' && typeof f1 === 'number' ? f1 - f0 : null
    return { userOnBeforeRenderCalls: obr, infoRenderFrameDelta: delta, firstGeometryDisposeEvents: disposed }
  })
  await test('pointPool', () => pointPoolWgpu(r))
  rt.dispose()
  void r.dispose()
  return out
}

/** PointPool single draw (g01 pool.js) on WebGPU: 60 nodes x 2000 points, RGBA32UI pool + DrawTable, 960 x 540; WGSL has
 * no point size, so every point is 1 px (g01 P_pool.jsonl: 84277 lit pixels) */
async function pointPoolWgpu(r: WebGPURenderer): Promise<FeatResult> {
  const Wp = 960
  const Hp = 540
  r.setSize(Wp, Hp, false)
  const NODES = 60
  const PER = 2000
  const N = NODES * PER
  const PW = 4096
  const PH = Math.ceil(N / PW)
  const pool = new Uint32Array(PW * PH * 4)
  let s = 7
  const rnd = (): number => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296
  for (let i = 0; i < N; i++) {
    const qx = (rnd() * 65535) | 0
    const qy = (rnd() * 65535) | 0
    const qz = (Math.pow(rnd(), 3) * 65535) | 0
    pool[i * 4] = (qx | (qy << 16)) >>> 0
    pool[i * 4 + 1] = qz
  }
  const poolTex = new DataTexture(pool, PW, PH, RGBAIntegerFormat, UnsignedIntType)
  poolTex.needsUpdate = true
  const DW = 1024
  const draw = new Uint32Array(DW * 4)
  const side = Math.ceil(Math.sqrt(NODES))
  const cell = 60
  const node = new Float32Array(NODES * 4)
  for (let n = 0; n < NODES; n++) {
    draw.set([n * PER, n * PER, PER, n], n * 4)
    node.set([((n % side) - side / 2) * cell, 0, (Math.floor(n / side) - side / 2) * cell, cell * 0.95], n * 4)
  }
  const drawTex = new DataTexture(draw, DW, 1, RGBAIntegerFormat, UnsignedIntType)
  drawTex.needsUpdate = true
  const nodeTex = new DataTexture(node, NODES, 1, undefined, FloatType)
  nodeTex.needsUpdate = true
  const nDraw: N = uniform(NODES)
  const mat = new PointsNodeMaterial()
  const qzv = varyingProperty('float', 'vQz')
  mat.positionNode = Fn(() => {
    const vid: N = int(vertexIndex).toVar()
    const lo: N = int(0).toVar()
    const hi: N = int(nDraw).toVar()
    Loop(12, () => {
      If(hi.sub(lo).greaterThan(1), () => {
        const mid: N = lo.add(hi).shiftRight(1).toVar()
        const e: N = textureLoad(drawTex, ivec2(mid.bitAnd(1023), mid.shiftRight(10))).x
        If(int(e).lessThanEqual(vid), () => {
          lo.assign(mid)
        }).Else(() => {
          hi.assign(mid)
        })
      })
    })
    const d: N = textureLoad(drawTex, ivec2(lo.bitAnd(1023), lo.shiftRight(10))).toVar()
    const gi: N = int(d.y).add(vid.sub(int(d.x))).toVar()
    const w: N = textureLoad(poolTex, ivec2(gi.bitAnd(4095), gi.shiftRight(12))).toVar()
    const n0: N = textureLoad(nodeTex, ivec2(int(d.w), 0)).toVar()
    const qp: N = vec3(float(w.x.bitAnd(uint(65535))), float(w.x.shiftRight(uint(16))), float(w.y.bitAnd(uint(65535)))).div(65535).toVar()
    qzv.assign(qp.z)
    return n0.xyz.add(vec3(qp.x, qp.z.mul(3), qp.y).mul(n0.w))
  })() as N
  mat.colorNode = mix(vec3(0.15, 0.16, 0.18), vec3(0.92, 0.93, 0.95), (qzv as N).smoothstep(0, 1)) as N
  const g = new BufferGeometry()
  g.setDrawRange(0, N)
  g.boundingSphere = new Sphere(new Vector3(), 1e4)
  const pts = new Points(g, mat)
  pts.frustumCulled = false
  const scene = new Scene()
  scene.background = new Color(0.003, 0.003, 0.0035)
  scene.add(pts)
  const camera = new PerspectiveCamera(55, Wp / Hp, 1, 5000)
  const R = side * cell * 0.9
  camera.position.set(R, R * 0.45, 0)
  camera.lookAt(0, 0, 0)
  camera.updateMatrixWorld()
  const rt = new RenderTarget(Wp, Hp, { depthBuffer: true, type: UnsignedByteType })
  r.setRenderTarget(rt)
  r.render(scene, camera)
  r.setRenderTarget(null)
  const a = (await r.readRenderTargetPixelsAsync(rt, 0, 0, Wp, Hp)) as Uint8Array
  const b = new Uint8Array(a.buffer, a.byteOffset, Wp * Hp * 4)
  let litN = 0
  for (let i = 0; i < b.length; i += 4) if (b[i] > 20) litN++
  rt.dispose()
  r.setSize(W, H, false)
  return { litPx: litN }
}
