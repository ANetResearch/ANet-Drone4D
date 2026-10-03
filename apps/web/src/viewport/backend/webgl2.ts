// Classic WebGLRenderer backend for Tier B and S (ADR-007, ADR-044; M06 §6.2, §6.5, FR-002, FR-008, FR-018, FR-026,
// FR-070). Owner: M06.
// Pass plan: Tier S renders MAIN | CLOUD in one pass to the default framebuffer (canvas DPR 0.5, SkyQuad last opaque);
// Tier B/A render P1 the point cloud (CH_CLOUD) into cloudRT restricted to the sub-viewport round(db x s) (rt.viewport,
// never renderer.setViewport), P2 the composite quad (colour, far-plane sky, depth write-back) to the default
// framebuffer (rendered with the scene camera: its vertex stage ignores the matrices, and the environment sky Fn reads
// the camera position for the 2D clouds; FX-WEB1), P3 the rest (CH_MAIN) with autoClear off. info.autoReset is off, so render.calls counts the whole frame
// and must equal the plan (M06-E007; dev builds log an error, production counts gpu.glErrors); the assertion only runs
// in READY. Every render target allocation counts gpu.rtAllocs; cloudRT is allocated at full canvas size on creation and
// on resize only, so rung changes and motion degradation are zero-allocation uniform and viewport changes (D1-AC-24).
import {
  Color, DepthTexture, FloatType, UnsignedByteType, HalfFloatType, WebGLRenderTarget, type Box2, type Camera, type DataTexture, type Material, type Scene, type Texture,
  type Vector2, type WebGLRenderer, type RenderTarget, type Object3D,
} from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import { events, onSceneShading, perfProbe, type BackendCaps, type DeviceClass, type FrameCtx, type PointSizeMode, type RTName, type RTOptions, type TextureOps, type Tier } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { GLPointsNodeMaterial } from '../glPointsNodeMaterial'
import { channelOf, CH_CLOUD, CH_MAIN, listLayers, type EdlCompositeLike, type WarmupItem } from '../layers/registry'
import { makeComposite, type Composite } from './composite'
import { skyColor } from '../layers/groundSky.materials'
import { lowestRungFor } from './deviceClass'
import { runSelftest, type SelftestResult } from './selftest'
import { warmupZoo, type ExtraPass, type WarmupReport } from './warmup'
import { makeBenchFinish } from './benchFinish'
import { installAttribSlotReset } from './attribSlots'
import type { Forced } from './testSwitches'

export type BackendState = 'WARMING' | 'READY' | 'LOST' | 'FAILED'
export interface BackendInfo {
  rendererString: string
  adapterArch: string
  microbenchMs: number | null
  t1M: number | null
  forced: Forced | null
  selftest: SelftestResult | null
  rendererKey: string
}
export interface PassPlan { draws: number; cloudDraws: number; compositeDraws: number; mainDraws: number; pickDraws: number }
export const newPassPlan = (): PassPlan => ({ draws: 0, cloudDraws: 0, compositeDraws: 0, mainDraws: 0, pickDraws: 0 })

/** side length of the point pick target in raster px (M05-FR-047, M06-FR-064) */
export const PICK_PX = 5
/**
 * one point-pick pass (M06-FR-064; FX-WEB1): prepare() runs in the render phase of the frame that draws it (from plan(),
 * after the world phase built this frame's DrawTable), fills the pick sub-table and the pick camera and returns false when
 * nothing can be hit; the pass renders CH_PICK into the 5 x 5 pickRT (counted in the frame's pass plan as pickDraws = 1)
 * and done() receives the asynchronous read-back (RGBA8 rows bottom-up) or null
 */
export interface PickPassRequest {
  prepare(ctx: FrameCtx): boolean
  readonly camera: Camera
  done(px: Uint8Array | null): void
}

export interface RenderBackend {
  readonly tier: Tier
  readonly deviceClass: DeviceClass
  readonly kind: 'webgl2' | 'webgpu'
  pointSizeMode: PointSizeMode
  readonly caps: BackendCaps
  readonly startRung: number
  readonly lowestAllowedRung: number
  readonly textures: TextureOps
  readonly renderer: WebGLRenderer
  readonly info: BackendInfo
  state: BackendState
  cloudScale: number
  readonly composite: Composite | null
  createRT(name: RTName, o?: RTOptions): RenderTarget
  readPixels(rt: RenderTarget, x: number, y: number, w: number, h: number, out: Uint8Array): Promise<Uint8Array>
  createPointsMaterial(): PointsNodeMaterial
  programsCount(): number
  /** the live scene (R3F); called once by the host */
  attach(scene: Scene): void
  selftest(): Promise<SelftestResult>
  /** sync: no compileAsync first, so the renders happen inside the call (late layers warmed inside a frame) */
  warmup(camera: Parameters<WebGLRenderer['render']>[1], items: readonly WarmupItem[], o?: { sync?: boolean }): Promise<WarmupReport>
  plan(ctx: FrameCtx, out: PassPlan): void
  /** the only place that calls renderer.render at run time (AWR-03 §3.6 rule 2) */
  renderFrame(ctx: FrameCtx, plan: PassPlan): void
  setCloudScale(s: number): void
  resizeRTs(dbW: number, dbH: number): void
  /** Tier B/A cloud render target (null on Tier S or before the first frame) */
  cloudTarget(): RenderTarget | null
  /** M05 EDL composite (null: the built-in composite with EDL strength 0) */
  setEdl(m: EdlCompositeLike | null): void
  /** queue a point-pick pass for the next rendered frame; a newer request replaces (and resolves null) an older one */
  requestPick(req: PickPassRequest): void
  onLost(cb: (reason: string) => void): () => void
  finishForBench?(): void
  dispose(): Promise<void>
}

const MASK_S = (1 << CH_MAIN) | (1 << CH_CLOUD)
const MASK_CLOUD = 1 << CH_CLOUD
const MASK_MAIN = 1 << CH_MAIN

let mismatchLogged = 0

/**
 * test builds, error path of M06-E007 only: the planned draws per layer and the objects of the last render list of
 * the scene (the main pass), to name the layer whose drawCount() disagrees with what three drew
 */
function planDiagnosis(r: WebGLRenderer, scene: Scene, ctx: FrameCtx): string {
  const planned = listLayers().map((s) => `${s.id}=${s.drawCount(ctx)}`).join(' ')
  const list = (r as unknown as { renderLists: { get(s: Scene, d: number): { opaque: { object: Object3D }[]; transparent: { object: Object3D }[]; transmissive: { object: Object3D }[] } } }).renderLists.get(scene, 0)
  const drawn: string[] = []
  for (const it of [...list.opaque, ...list.transmissive, ...list.transparent]) {
    let o: Object3D | null = it.object
    const path: string[] = []
    while (o && o !== scene && path.length < 3) {
      path.push(o.name || o.type)
      o = o.parent
    }
    drawn.push(path.reverse().join('/'))
  }
  return `planned ${planned}; drawn ${drawn.join(', ')}`
}

export function wrapGl(r: WebGLRenderer, tier: Tier, deviceClass: DeviceClass, startRung: number, info: BackendInfo): RenderBackend {
  const gl = r.getContext() as WebGL2RenderingContext
  // SwiftShader keys its vertex routines on stale vertex-input slots of earlier draws: normalise them before every draw so
  // no program is compiled again after the reveal (P4-WEB, ADR-071 item 1; attribSlots.ts)
  const slotReset = deviceClass === 'software' && /swiftshader/i.test(info.rendererString) ? installAttribSlotReset(gl) : null
  const caps: BackendCaps = {
    reversedZ: r.capabilities.reversedDepthBuffer, timerQuery: gl.getExtension('EXT_disjoint_timer_query_webgl2') !== null,
    parallelCompile: gl.getExtension('KHR_parallel_shader_compile') !== null, compute: false, mrt: false, readbackTopDown: false,
    maxTextureSize: r.capabilities.maxTextureSize, floatColorRT: gl.getExtension('EXT_color_buffer_float') !== null,
  }
  const probe = perfProbe()
  const lost = new Set<(reason: string) => void>()
  const onLostEv = (e: Event): void => {
    e.preventDefault()
    if (be.state === 'FAILED') return
    be.state = 'LOST'
    probe.gpu.contextLost++
    for (const cb of lost) cb('webglcontextlost')
  }
  r.domElement.addEventListener('webglcontextlost', onLostEv)
  const textures: TextureOps = {
    initTexture: (t: Texture) => r.initTexture(t),
    copyTextureToTexture: (src: DataTexture, dst: DataTexture, srcRegion: Box2, dstPosition: Vector2) => r.copyTextureToTexture(src, dst, srcRegion, dstPosition),
  }
  let scene: Scene | null = null
  let cloudRT: WebGLRenderTarget | null = null
  let rtW = 0
  let rtH = 0
  let edl: EdlCompositeLike | null = null
  const composite: Composite | null = tier === 'S' ? null : makeComposite(caps.reversedZ)
  const bindComposite = (): void => {
    if (!cloudRT || !cloudRT.depthTexture) return
    composite?.bindTargets(cloudRT.texture, cloudRT.depthTexture)
    edl?.bindTargets(cloudRT.texture, cloudRT.depthTexture)
  }
  const rtsByName = new Map<RTName, RenderTarget>()
  // point pick (M06-FR-064): the queued request, whether plan() prepared it this frame, and the saved clear colour
  let pickReq: PickPassRequest | null = null
  let pickPlanned = false
  const clear0 = new Color()

  const be: RenderBackend = {
    tier, deviceClass, kind: 'webgl2', pointSizeMode: 'glpoint', caps, startRung, lowestAllowedRung: lowestRungFor(tier), textures,
    renderer: r, info, state: 'WARMING', cloudScale: 1, composite,
    createRT(name: RTName, o: RTOptions = {}): RenderTarget {
      const w = Math.max(1, Math.round(o.width ?? r.domElement.width * (o.scaleOfCanvas ?? 1)))
      const h = Math.max(1, Math.round(o.height ?? r.domElement.height * (o.scaleOfCanvas ?? 1)))
      const rt = new WebGLRenderTarget(w, h, { depthBuffer: true, type: o.halfFloat ? HalfFloatType : UnsignedByteType })
      if (o.depthTexture) {
        rt.depthTexture = new DepthTexture(w, h)
        rt.depthTexture.type = FloatType
      }
      rt.texture.name = `rt.${name}`
      probe.gpu.rtAllocs++
      rtsByName.set(name, rt)
      return rt
    },
    async readPixels(rt: RenderTarget, x: number, y: number, w: number, h: number, out: Uint8Array): Promise<Uint8Array> {
      // PBO + fence; RGBA8 only; rows bottom-up (GL convention, g01 T11f)
      await r.readRenderTargetPixelsAsync(rt as WebGLRenderTarget, x, y, w, h, out)
      return out
    },
    createPointsMaterial: (): PointsNodeMaterial => new GLPointsNodeMaterial(),
    programsCount: () => (r.info.programs as unknown[] | null)?.length ?? 0,
    attach(s: Scene): void {
      scene = s
    },
    async selftest(): Promise<SelftestResult> {
      const res = await runSelftest(r, (w, h) => be.createRT('selftest', { width: w, height: h }), (rt, x, y, w, h, o) => be.readPixels(rt, x, y, w, h, o), { noFix: TEST_SWITCHES && info.forced?.selftestNoFix === true })
      info.selftest = res
      if (!res.pointSizeOk) {
        be.pointSizeMode = 'pixel'
        console.warn(`M06-E004 point size self test lit ${res.litPx} px (< 4): pointSizeMode = pixel`)
        events.emit('backend.notice', 'pointsize.degraded')
      }
      if (!res.linearOk) console.warn(`M06-E005 render-target linear check read ${res.linearValue} (expected 128 +- 2)`)
      return res
    },
    async warmup(camera, items, wo = {}): Promise<WarmupReport> {
      if (!scene) throw new Error('RenderBackend.warmup before attach(scene)')
      if (tier !== 'S' && !cloudRT) be.resizeRTs(r.domElement.width, r.domElement.height)
      // pickRT (5 x 5 raster px, RGBA8, M05-FR-047) exists before the reveal so the ID material compiles under the mask
      if (!rtsByName.has('pick') && items.some((it) => it.targets?.includes('pick'))) be.createRT('pick', { width: 5, height: 5 })
      // test builds with ?quality=1: the coverage-mask target of viewport/qualityMask.ts, so its point program (render
      // target output) compiles under the mask and not at the first quality sample (FX-WEB1)
      if (TEST_SWITCHES && !rtsByName.has('quality') && items.some((it) => it.targets?.includes('quality'))) be.createRT('quality', { width: 8, height: 8 })
      const extra: ExtraPass[] = []
      if (composite && cloudRT) {
        // both P2 materials (the built-in composite and M05's EDL composite) with the scene camera, as at run time
        const c = composite
        extra.push({ scene: c.scene, camera, target: null, before: () => (c.quad.material = c.material) })
        if (edl) {
          const e = edl as unknown as Material
          extra.push({ scene: c.scene, camera, target: null, before: () => (c.quad.material = e), after: () => (c.quad.material = e) })
        }
      }
      const bench = TEST_SWITCHES ? rtsByName.get('bench') : undefined
      const rep = await warmupZoo(r, scene, camera, items, (t) => (t === 'cloud' ? cloudRT : t === 'bench' ? bench : t === 'screen' ? null : rtsByName.get(t)), {
        parallelCompile: caps.parallelCompile === true && wo.sync !== true, sync: wo.sync === true, extra, programs: () => be.programsCount(),
        bare: slotReset ? (fn) => slotReset.bare(fn) : undefined,
      })
      probe.gpu.programs = rep.programs
      probe.meta.warmupMs = rep.warmupMs
      return rep
    },
    plan(ctx: FrameCtx, out: PassPlan): void {
      let cloud = 0
      let main = 0
      const ls = listLayers()
      for (let i = 0; i < ls.length; i++) {
        const s = ls[i]
        const n = s.drawCount(ctx)
        if (channelOf(s) === CH_CLOUD) cloud += n
        else main += n
      }
      out.cloudDraws = tier === 'S' ? 0 : cloud
      out.compositeDraws = tier === 'S' ? 0 : 1
      out.mainDraws = tier === 'S' ? main + cloud : main
      out.pickDraws = 0
      if (pickReq && be.state === 'READY' && rtsByName.has('pick')) {
        let ok = false
        try {
          ok = pickReq.prepare(ctx)
        } catch (e) {
          console.warn('point pick prepare failed', e)
        }
        pickPlanned = ok
        out.pickDraws = ok ? 1 : 0
      }
      out.draws = out.cloudDraws + out.compositeDraws + out.mainDraws + out.pickDraws
    },
    renderFrame(ctx: FrameCtx, plan: PassPlan): void {
      const cam = ctx.camera
      if (!scene || !cam || be.state === 'LOST' || be.state === 'FAILED') return
      const before = be.programsCount()
      r.info.reset()
      if (tier === 'S') {
        cam.layers.mask = MASK_S
        r.setRenderTarget(null)
        r.render(scene, cam)
      } else {
        if (!cloudRT || rtW !== ctx.dbW || rtH !== ctx.dbH) be.resizeRTs(ctx.dbW, ctx.dbH)
        const rt = cloudRT!
        const s = be.cloudScale
        rt.viewport.set(0, 0, Math.max(1, Math.round(rtW * s)), Math.max(1, Math.round(rtH * s)))
        r.setRenderTarget(rt)
        r.clear()
        cam.layers.mask = MASK_CLOUD
        r.render(scene, cam)
        r.setRenderTarget(null)
        const c = composite!
        if (edl) {
          edl.uniforms.uvScale.value = s
          if (c.quad.material !== (edl as unknown)) c.quad.material = edl as unknown as Material
        } else c.uvScale.value = s
        cam.layers.mask = MASK_MAIN
        r.render(c.scene, cam)
        r.autoClear = false
        cam.layers.mask = MASK_MAIN
        r.render(scene, cam)
        r.autoClear = true
      }
      cam.layers.mask = MASK_S
      if (pickReq) {
        const req = pickReq
        const planned = pickPlanned
        pickReq = null
        pickPlanned = false
        const prt = rtsByName.get('pick') as WebGLRenderTarget | undefined
        if (planned && prt) {
          // ID pass: only CH_PICK (the pick object) into the 5 x 5 target, cleared to id 0 (no hit)
          r.getClearColor(clear0)
          const a0 = r.getClearAlpha()
          r.setClearColor(0x000000, 0)
          r.setRenderTarget(prt)
          r.clear()
          r.render(scene, req.camera)
          r.setRenderTarget(null)
          r.setClearColor(clear0, a0)
          be.readPixels(prt, 0, 0, PICK_PX, PICK_PX, new Uint8Array(4 * PICK_PX * PICK_PX)).then((px) => req.done(px), () => req.done(null))
        } else req.done(null)
      }
      slotReset?.tail()
      const calls = r.info.render.calls
      probe.gpu.calls = calls
      probe.gpu.passPlan = plan.draws
      const after = be.programsCount()
      probe.gpu.programs = after
      ctx.compiledThisFrame = after > before
      // only a READY backend counts (a rebuilt backend warms up with the page already revealed; M06-FR-010)
      if (after > before && probe.gpu.programsAtReveal >= 0 && be.state === 'READY') {
        probe.gpu.compiledAfterReveal += after - before
        if (TEST_SWITCHES && mismatchLogged++ < 20) {
          // the newest entries of info.programs are the ones compiled this frame: name them (diagnostics only)
          const ps = (r.info.programs as { name?: string; cacheKey?: string }[] | null) ?? []
          const names = ps.slice(before).map((q) => `${q.name ?? '?'}[${(q.cacheKey ?? '').slice(0, 96)}]`).join('; ')
          console.error(`M06-E006 ${after - before} program(s) compiled after the reveal (programs ${after}): ${names}`)
        }
      }
      if (be.state === 'READY' && calls !== plan.draws) {
        probe.gpu.planMismatches++
        probe.gpu.glErrors++
        if (TEST_SWITCHES && mismatchLogged++ < 20) {
          console.error(`M06-E007 render.calls ${calls} != pass plan ${plan.draws} (cloud ${plan.cloudDraws}, composite ${plan.compositeDraws}, main ${plan.mainDraws}); ${planDiagnosis(r, scene, ctx)}`)
        }
      }
    },
    cloudTarget: () => cloudRT,
    setCloudScale(s: number): void {
      be.cloudScale = tier === 'S' ? 1 : Math.max(0.1, Math.min(1, s))
    },
    resizeRTs(dbW: number, dbH: number): void {
      if (tier === 'S' || dbW <= 0 || dbH <= 0) return
      if (cloudRT && rtW === dbW && rtH === dbH) return
      cloudRT?.dispose()
      cloudRT = be.createRT('cloud', { depthTexture: true, width: dbW, height: dbH }) as WebGLRenderTarget
      rtW = dbW
      rtH = dbH
      bindComposite()
    },
    setEdl(m: EdlCompositeLike | null): void {
      if (m === edl) return
      edl = m
      if (composite && !m) composite.quad.material = composite.material
      // background pixels of the M05 composite: the M06 sky Fn (clip-space xy -> linear colour)
      if (m && composite) m.setBackgroundNode?.((clip: unknown) => skyColor(composite.sky, clip))
      bindComposite()
    },
    requestPick(req: PickPassRequest): void {
      const old = pickReq
      pickReq = req
      pickPlanned = false
      old?.done(null)
    },
    onLost(cb) {
      lost.add(cb)
      return () => {
        lost.delete(cb)
      }
    },
    async dispose() {
      r.domElement.removeEventListener('webglcontextlost', onLostEv)
      slotReset?.dispose()
      cloudRT?.dispose()
      for (const rt of rtsByName.values()) rt.dispose()
      composite?.dispose()
      r.dispose()
    },
  }
  // the environment's scene shading provider arrives after the backend exists (before the shader zoo): rebuild the P2
  // sky Fn of the built-in composite and of the EDL composite (FX-WEB1)
  const offShading = onSceneShading(() => {
    composite?.rebuild()
    const c = composite
    if (edl && c) edl.setBackgroundNode?.((clip: unknown) => skyColor(c.sky, clip))
  })
  const dispose0 = be.dispose.bind(be)
  be.dispose = async () => {
    offShading()
    await dispose0()
  }
  if (TEST_SWITCHES) be.finishForBench = makeBenchFinish(gl) // 1-pixel read-back, not gl.finish() (FX2-R2)
  return be
}
