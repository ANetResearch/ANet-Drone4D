// PointCloudEngine (M05 §6, §7.1): lifecycle state machine, world-phase update, governor-phase CAS sample, events.
// Owner: M05. The only point cloud runtime in the browser: progressive loading of ANET_Q16 octrees driven by the camera
// and the frame cadence, with a closed loop on the point budget, screen error, point size and render scale.
//
// World phase (update): arrivals of the fetch worker enter the CPU cache -> camera into the layer frame -> APH selection
// (B from CAS, tau from the rung; the first-frame target set is capped at the first-screen level) -> cancel stale fetches
// -> dispatch downloads in pop order (focus window) -> uploads within the frame quota -> GPU eviction past 1.5 B_ref ->
// DrawTable with fade and childDrawnMask -> uniforms (Lite size, adaptive maxPx) -> coverage, statistics, __perf.pc.
// Governor phase (sampleFrame): TTFP commit, CAS sample with the freeze mask, __perf.cas.
// One attribute-less Points object and one draw call (Tier S CH_MAIN, Tier B/A CH_CLOUD); the pool texture, tables and
// material are created once and survive world switches (FR-007); a device loss rebuilds them from the CPU cache (FR-027).
import { BufferGeometry, Matrix4, Points, Sphere, Vector3, type DataTexture } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'
import { EASE, MOTION } from '@/lib/tokens/motion.gen'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { PALETTE_LINEAR } from '@/lib/tokens/palette.gen'
import { SCENE } from '@/lib/tokens/scene.gen'
import { bezierAt } from '../anim/bezier'
import type { FrameCtx, RenderBackendView } from '../loop'
import { onSceneShading, sceneShading } from '../shading'
import { CascadeController, FREEZE_WARMUP, type RungReason } from './core/CascadeController'
import { CpuCache } from './core/CpuCache'
import { PrefetchQueue, collectCandidates, newCandidates, reorderWindow, type FocusState } from './core/DownloadQueue'
import { newEvictionCandidates, planEviction, type EvictionCandidates } from './core/EvictionPolicy'
import { NS, type NodeStore } from './core/NodeStore'
import { LB_BUDGET, LB_NODES, LIMITED, newScratch, newSelection, selectVisible, type LodScratch, type SelectOptions, type Selection } from './core/Selector'
import { failIsFinal, isFailedAfter, nextRetryAt, shouldAbort } from './core/StreamPolicy'
import { OUTSIDE, classifyNode, distToBox, lodCameraLookAt, makeLodCamera, newLodCamera } from './core/frustum'
import { casFreezeMask, fillEma, newStats, pushRing, resetStats, type PerfSink } from './core/stats'
import { DrawTables, buildBlockIndex, buildDrawTable, newDrawBuild } from './gpu/DrawTable'
import { PageAllocator } from './gpu/PageAllocator'
import { PointPool, dummyIntTexture } from './gpu/PointPool'
import { drainUploads, newUploadResult, type UploadSink } from './gpu/Uploader'
import { DtmSampler } from './io/dtm'
import { Fetcher } from './io/fetcher'
import type { WorkerOut } from './io/fetchJob'
import { PC_CONTENT_STALE, PC_NODE_FAILED, PcError } from './io/meta'
import { openWorld, type OpenedWorld } from './io/openWorld'
import { EdlCompositeMaterial } from './render/edlComposite'
import { makeIdMaterial } from './render/idMaterial'
import { PointPicker } from './pick/PointPicker'
import { COLOR_MODE_INDEX, dummyDtmTexture, makeClassTexture, makePointMaterial, makePointUniforms, makeTextureNodes, type PointColorTokens, type PointTextures, type PointUniforms } from './render/pointMaterial'
import { LADDER, PC, deviceParams, httpCapFor, startBudget, tauCapPx, type DeviceParams } from './params'
import type { ColorMode, EnginePhase, FocusMode, OpenedWorldInfo, PointCloudEvents, PointCloudStats } from './types'

export interface PointCloudTokens extends PointColorTokens {
  /** --duration-lod-fade, ms */
  lodFadeMs: number
  /** --ease-smooth-out control points */
  easeSmoothOut: readonly [number, number, number, number]
  edlStrength: number
  edlRadiusPx: number
  clear: readonly [number, number, number]
}

/** tokens from the generated modules (lib/tokens/*.gen.ts); the layer adapter may inject others (M05 §7.1) */
export function defaultTokens(): PointCloudTokens {
  return {
    lodFadeMs: MOTION.lodFadeMs, easeSmoothOut: EASE.smoothOut, ramp: SCENE.pcRamp, classColors: SCENE.classColors, normalBase: PALETTE_LINEAR.g300,
    heroDemoted: PALETTE_LINEAR.g50, gamma: SCENE.pcGamma, edlStrength: SCENE.edlStrength, edlRadiusPx: SCENE.edlRadiusPx, clear: SCENE.clear,
  }
}

export interface PointCloudParams {
  /** test builds: lock B and bypass CAS */
  fixedB?: number
  /** test builds: fetch failure probability (pcInject=fail:<p>) */
  pcInject?: number
  /** test builds: quality sampling hooks (quality=1) */
  quality?: boolean
  seed?: number
}

export interface PointCloudEngineOptions {
  backend: RenderBackendView
  /** window.__perf (M06); pc, cas and load are written in place */
  perf?: PerfSink | null
  tokens?: Partial<PointCloudTokens>
  motionTier?: () => 'full' | 'lite' | 'reduced'
  /** M06 FrameSampler refresh estimate (hardware T*) */
  refreshMs?: () => number
  /**
   * whether the fixed layers (drones, trails, environment, ground and sky) share the frame with the cloud (the viewport
   * passes false only for the point-cloud-only bench scene flight60 scene=pc; omitted = cloud alone). Software devices
   * then start the CAS at startBudget(): the budget that fits the frame left after the fixed layers, at least the
   * quality floor (ADR-076)
   */
  fixedLayers?: boolean
  params?: PointCloudParams
  /** tests: fetch implementation and in-process jobs */
  f?: typeof fetch
  useWorker?: boolean
}

type Listener = (payload: never) => void

let active: PointCloudEngine | null = null
/** the engine of the page (M06 PerfGovernor reads cas.state() through it) */
export function activePointCloud(): PointCloudEngine | null {
  return active
}
function setActive(e: PointCloudEngine | null): void {
  active = e
}

export class PointCloudEngine {
  readonly root: Points
  readonly geometry: BufferGeometry
  readonly cas: CascadeController
  readonly dtm = new DtmSampler()
  readonly dev: DeviceParams
  /** TTFP bookkeeping (ms, performance.now base) */
  readonly load = { ttfpStart: Number.NaN, ttfp: Number.NaN, ttfpFirstPixel: Number.NaN, switchMs: Number.NaN, openedAt: Number.NaN, firstScreenBytes: 0,
    pendingCommit: false, firstPixelPending: false, firstPixelCommit: false, isSwitch: false }
  info: OpenedWorldInfo | null = null
  edlMaterial: EdlCompositeMaterial | null = null
  /** D1-ext picking (M06 Picker renders picker.object in its CH_PICK pass) */
  readonly picker: PointPicker
  private classNames: string[] = []
  private readonly layerM = { m: new Matrix4(), inv: new Matrix4() }

  private be: RenderBackendView
  private readonly tok: PointCloudTokens
  /**
   * test-build switches, read only behind the build-time constant TEST_SWITCHES so that a production bundle keeps neither
   * their names nor their branches (M06-AC-010; FX-WEB1): lockB is the locked point budget of ?fixedB= (0: CAS drives B),
   * qualityHooks the ?quality=1 sampling hooks
   */
  private readonly lockB: number
  private readonly qualityHooks: boolean
  private readonly perf: PerfSink | null
  private readonly motionTier: () => 'full' | 'lite' | 'reduced'
  private readonly refreshMs: (() => number) | null
  private readonly fetcher: Fetcher
  private readonly f: typeof fetch | undefined
  private readonly listeners = new Map<string, Set<Listener>>()
  private readonly st: PointCloudStats = newStats()
  private readonly ease: (x: number) => number
  // GPU side (rebuilt on device loss)
  private pool: PointPool
  private tables: DrawTables
  private classTex: DataTexture
  private dummyDtm: DataTexture
  private texNodes: PointTextures
  private u: PointUniforms
  private material: PointsNodeMaterial
  private alloc: PageAllocator
  // world
  private world: OpenedWorld | null = null
  private t: NodeStore | null = null
  private cache: CpuCache | null = null
  private base = ''
  private openSeq = 0
  private abort: AbortController | null = null
  private phase: EnginePhase = 'idle'
  // per frame
  private sel: Selection = newSelection(PC.maxNodes)
  private scratch: LodScratch = newScratch(1)
  private readonly cam = newLodCamera()
  private readonly Linv = new Matrix4()
  private readonly v3 = new Vector3()
  private readonly db = newDrawBuild()
  private readonly up = newUploadResult()
  private cand = newCandidates(1)
  private evc: EvictionCandidates = newEvictionCandidates(1)
  private evOut = new Int32Array(1)
  private reqOfNode = new Int32Array(0)
  private cancelAsked = new Uint8Array(0)
  private readonly reqNode = new Map<number, number>()
  private readonly inflightNodes: number[] = []
  private readonly arrivals: WorkerOut[] = []
  private readonly focus: FocusState = { mode: 'none', p: new Float64Array(3) }
  private readonly prefetch = new PrefetchQueue()
  private prefetchScratch: LodScratch | null = null
  private prefetchSel: Selection | null = null
  private targetSet = new Int32Array(0)
  private targetN = -1
  private targetPts = 0
  private revealed = false
  private visible = true
  private residentPts = 0
  private warmupLeft = 0
  private lastPrograms = -1
  private lastNodeFailedAt = Number.NEGATIVE_INFINITY
  private uniqueBytes = 0
  private lastClamped = false
  private suspendedAt = Number.NaN
  private recovering = false
  private resetPending = false
  private readyAt = 0
  /** performance.now() of the first world-phase update with the shader zoo done (NaN before) */
  private warmReadyAt = Number.NaN
  private edlOn = true
  private readonly freezeIn = { frozen: false, hidden: false, compiled: false, warmupLeft: 0 }
  private readonly abortPol = { abortOutside: true, abortSuperseded: PC.abortSuperseded as boolean, saturated: false }
  private readonly selectOpts: SelectOptions = { tau: 4, B: 25_000, headroom: PC.headroom, maxNodes: PC.maxNodes, maxSkips: PC.maxSkips, minPrefix: PC.minPrefix,
    hysteresis: PC.hysteresis, depthCap: 255, tauMinFrac: PC.tauMinFrac }

  constructor(o: PointCloudEngineOptions) {
    this.be = o.backend
    this.tok = { ...defaultTokens(), ...o.tokens }
    this.lockB = TEST_SWITCHES ? (o.params?.fixedB ?? 0) : 0
    this.qualityHooks = TEST_SWITCHES ? o.params?.quality === true : false
    this.perf = o.perf ?? null
    this.motionTier = o.motionTier ?? (() => 'full')
    this.refreshMs = o.refreshMs ?? null
    this.f = o.f
    const e = this.tok.easeSmoothOut
    this.ease = (x: number) => bezierAt(e, x)
    const be = this.be
    const devBase = deviceParams(be.tier, be.deviceClass, be.startRung, be.lowestAllowedRung, be.caps?.maxTextureSize ?? 16384)
    // test builds: a locked budget above the device's pool (the high-budget quality check, ?fixedB=2000000 on a software
    // device forced to Tier B) sizes the pool for it and takes the Tier B/A cache and upload quota without the software
    // render-ratio lock, so the cloud converges and is drawn at the full sub-viewport; never in production (lockB is 0
    // there), never on the perf path (18 §2.5, FX-WEB1)
    const wantRows = Math.ceil(this.lockB / PC.capacityFrac / PC.poolWidth)
    this.dev = TEST_SWITCHES && wantRows > devBase.poolRows
      ? { ...devBase, poolRows: Math.min(wantRows, Math.max(1, be.caps?.maxTextureSize ?? 16384)), rsLock: 0,
        cpuCacheBytes: Math.max(devBase.cpuCacheBytes, PC.cpuCacheBA), uploadPtsPerFrame: Math.max(devBase.uploadPtsPerFrame, PC.uploadBytesBA / PC.bytesPerTexel) }
      : devBase
    const capPts = this.dev.poolRows * PC.poolWidth
    const software = be.deviceClass === 'software'
    this.cas = new CascadeController({
      startIndex: be.startRung, floorIndex: this.dev.floorIndex, ceilIndex: this.dev.ceilIndex, targetMs: software ? PC.targetMsSoftware : (this.refreshMs?.() ?? 16.7),
      tailK: this.dev.tailK, initialB: startBudget(this.dev, software, o.fixedLayers === true), Bfloor: this.dev.bFloor, poolCapacityPts: capPts, rsLock: this.dev.rsLock,
      onRung: (from, to, reason) => this.onRung(from, to, reason),
    })
    if (this.lockB) this.cas.setB(Math.min(this.lockB, PC.capacityFrac * capPts))
    this.fetcher = new Fetcher({ workers: this.dev.workers, limit: this.dev.inflight, useWorker: o.useWorker, f: o.f,
      injectFail: TEST_SWITCHES ? (o.params?.pcInject ?? 0) : 0, seed: o.params?.seed ?? 1 })
    this.fetcher.onReply = (r) => this.arrivals.push(r)
    this.geometry = new BufferGeometry()
    this.geometry.setDrawRange(0, 0)
    this.geometry.boundingSphere = new Sphere(new Vector3(), 1e7)
    this.root = new Points(this.geometry)
    this.root.frustumCulled = false
    this.root.renderOrder = -1
    this.root.name = 'PointCloudLayer'
    this.root.raycast = () => {}
    this.root.layers.set(1) // CH_CLOUD on every tier (Tier S renders MAIN | CLOUD in one pass, M06 registry)
    // GPU objects, created once (FR-007: world switches keep them)
    this.alloc = new PageAllocator(capPts, PC.page)
    this.pool = new PointPool(this.dev.poolRows, 2, be.textures)
    this.tables = new DrawTables(4096, capPts)
    this.classTex = makeClassTexture(this.tok.classColors)
    this.dummyDtm = dummyDtmTexture()
    this.texNodes = makeTextureNodes({ pool: this.pool.tex, draw: this.tables.draw, block: this.tables.block, node: this.tables.node, dtm: this.dummyDtm, classes: this.classTex })
    this.u = makePointUniforms(this.tok)
    this.material = this.buildMaterial()
    this.root.material = this.material
    this.edlMaterial = this.buildEdl()
    this.picker = new PointPicker({
      store: () => this.t, drawTable: () => ({ data: this.tables.drawData, k: this.db.k }), packed: (i) => this.cache?.get(i) ?? null, dtm: this.dtm,
      layerMatrix: () => {
        this.layerM.m.copy(this.root.matrix)
        this.layerM.inv.copy(this.root.matrix).invert()
        return this.layerM
      },
      className: (c) => this.classNames[c] ?? `class-${c}`, now: () => performance.now(),
    })
    this.picker.object.material = this.buildIdMaterial()
    this.root.add(this.picker.object)
    this.st.B = this.cas.B
    this.syncCasStats()
    if (this.perf) {
      this.perf.pc.poolRows = this.dev.poolRows
      if (TEST_SWITCHES && (o.params?.fixedB || o.params?.pcInject)) {
        this.perf.forced = { ...(this.perf.forced ?? {}), ...(o.params.fixedB ? { fixedB: o.params.fixedB } : {}),
          ...(o.params.pcInject ? { perfInject: `pcFail:${o.params.pcInject}` } : {}) }
      }
    }
    setActive(this)
  }

  private buildEdl(): EdlCompositeMaterial | null {
    if (this.be.tier === 'S') return null
    const m = new EdlCompositeMaterial({ reversedDepth: this.be.caps.reversedZ, strength: this.tok.edlStrength, radiusPx: this.tok.edlRadiusPx, clear: this.tok.clear })
    this.edlMaterial = m
    this.applyEdl()
    return m
  }

  private buildIdMaterial(): PointsNodeMaterial {
    return makeIdMaterial(() => this.be.createPointsMaterial(), { ...this.texNodes, pick: this.picker.texNode }, this.u, this.picker.numDraws, { pointSizeMode: this.be.pointSizeMode })
  }

  private buildMaterial(): PointsNodeMaterial {
    return makePointMaterial(() => this.be.createPointsMaterial(), this.texNodes, this.u, { pointSizeMode: this.be.pointSizeMode, round: this.be.tier !== 'S',
      shading: sceneShading() })
  }

  /** the scene shading provider changed (the environment adapter mounts before the shader zoo): new point material */
  private readonly offShading = onSceneShading(() => {
    const old = this.material
    this.material = this.buildMaterial()
    this.root.material = this.material
    old.dispose()
  })

  // ------------------------------------------------------------ events
  on<K extends keyof PointCloudEvents>(name: K, fn: (e: PointCloudEvents[K]) => void): () => void {
    let s = this.listeners.get(name)
    if (!s) this.listeners.set(name, (s = new Set()))
    s.add(fn as Listener)
    return () => s!.delete(fn as Listener)
  }
  private emit<K extends keyof PointCloudEvents>(name: K, payload: PointCloudEvents[K]): void {
    const s = this.listeners.get(name)
    if (!s) return
    for (const fn of s) (fn as (e: PointCloudEvents[K]) => void)(payload)
  }

  get poolCapacityPts(): number {
    return this.dev.poolRows * PC.poolWidth
  }
  get enginePhase(): EnginePhase {
    return this.phase
  }
  get store(): NodeStore | null {
    return this.t
  }
  get inflight(): number {
    return this.fetcher.inflight
  }

  // ------------------------------------------------------------ open / switch / close
  /**
   * Open a world (base = absolute URL of the world directory). A second open switches worlds (FR-007): requests are
   * aborted, the allocator reset (the pool texture stays), the DrawTable and CPU cache cleared, CAS keeps rung and B and
   * freezes 30 frames; the first-frame target set of the new world is selected with the current B.
   */
  async open(base: string, o: { reason?: 'open' | 'switch' | 'stale' } = {}): Promise<OpenedWorldInfo> {
    if (this.perf?.marks && this.perf.marks['pc.open'] === undefined) this.perf.marks['pc.open'] = performance.now()
    const wasOpen = this.t !== null || this.phase === 'manifest' || this.phase === 'first_screen'
    const reason = o.reason ?? (wasOpen ? 'switch' : 'open')
    this.release()
    const seq = ++this.openSeq
    const ac = new AbortController()
    this.abort = ac
    this.base = base
    this.phase = 'manifest'
    this.st.phase = 'manifest'
    this.st.error = null
    this.load.openedAt = performance.now()
    this.load.isSwitch = reason !== 'open'
    try {
      const w = await openWorld(base, {
        fetcher: this.fetcher, poolCapacityPts: this.poolCapacityPts, startHi: LADDER[this.cas.index].hi, signal: ac.signal, now: () => performance.now(), f: this.f,
        onPhase: (p) => {
          if (seq === this.openSeq) {
            this.phase = p
            this.st.phase = p
          }
        },
      })
      if (seq !== this.openSeq) throw new PcError(0, 'superseded')
      this.install(w, reason)
      return this.info!
    } catch (e) {
      if (seq === this.openSeq && !(e instanceof PcError && e.code === 0)) {
        const err = e instanceof PcError ? { code: e.code, message: e.message } : { code: 400, message: String((e as Error)?.message ?? e) }
        if (err.code === PC_CONTENT_STALE) {
          // the world was rebuilt while we opened it: reopen once with the new contentVersion (FR-008)
          return this.open(base, { reason: 'stale' })
        }
        this.phase = 'error'
        this.st.phase = 'error'
        this.st.error = err
        this.emit('pc.world.error', err)
      }
      throw e
    }
  }

  private install(w: OpenedWorld, reason: 'open' | 'switch' | 'stale'): void {
    const t = w.store
    this.world = w
    this.t = t
    this.cache = new CpuCache(t, this.dev.cpuCacheBytes)
    this.fetcher.setHttpCap(httpCapFor(w.nextHopProtocol))
    this.reqOfNode = new Int32Array(t.N).fill(-1)
    this.cancelAsked = new Uint8Array(t.N)
    this.cand = newCandidates(t.N)
    this.evc = newEvictionCandidates(t.N)
    this.evOut = new Int32Array(t.N)
    this.scratch = newScratch(t.N)
    this.sel = newSelection(PC.maxNodes)
    this.prefetchScratch = null
    this.prefetchSel = null
    for (const b of w.firstScreen) this.cache.put(b.node, b.buf, 0, true)
    for (let i = 0; i < t.N; i++) if (t.numPoints[i] === 0) t.state[i] = NS.CACHED
    this.pool.ensureStaging(t.maxNodePoints)
    if (t.N > this.tables.nodeCapacity) this.growNodeTable(t.N)
    this.tables.setNodes(t)
    this.alloc.reset()
    this.residentPts = 0
    this.uniqueBytes = w.firstScreenBytes
    for (const b of w.firstScreen) t.fetchedOnce[b.node] = 1
    const md = w.metas[0]
    const zr = w.world.render?.zRangeM ?? [md.anet.stats.zP1, md.anet.stats.zP99]
    const hr = w.world.render?.hagRangeM ?? [md.anet.stats.hagP1, md.anet.stats.hagP99]
    this.u.zLo.value = zr[0]
    this.u.zHi.value = zr[1]
    this.u.hagLo.value = hr[0]
    this.u.hagHi.value = hr[1]
    this.u.dtmReady.value = 0
    this.u.groundZ.value = w.coord.ground?.zM ?? 0
    this.dtm.reset(w.coord.ground?.zM ?? 0)
    this.texNodes.dtm.value = this.dummyDtm
    if (w.layerMatrix) {
      this.root.matrixAutoUpdate = false
      this.root.matrix.fromArray(w.layerMatrix).transpose()
    } else {
      this.root.matrixAutoUpdate = true
      this.root.matrix.identity()
    }
    this.targetN = -1
    this.targetPts = 0
    this.load.ttfpStart = w.ttfpStart
    if (this.perf?.marks && !this.load.isSwitch && this.perf.marks['pc.firstScreen'] === undefined) this.perf.marks['pc.firstScreen'] = w.ttfpStart
    this.load.firstScreenBytes = w.firstScreenBytes
    this.load.ttfp = Number.NaN
    this.load.pendingCommit = false
    this.load.firstPixelPending = true
    this.phase = 'first_screen'
    this.st.phase = 'first_screen'
    this.root.visible = this.visible
    let tmin = [Infinity, Infinity, Infinity]
    let tmax = [-Infinity, -Infinity, -Infinity]
    for (let r = 0; r < t.roots.length; r++) {
      const i = t.roots[r]
      tmin = tmin.map((v, a) => Math.min(v, t.tightMin[3 * i + a]))
      tmax = tmax.map((v, a) => Math.max(v, t.tightMax[3 * i + a]))
    }
    const home = w.world.camera?.home
    this.info = {
      worldId: w.world.id, contentVersion: w.world.contentVersion, roots: t.roots.length, nodes: t.N, points: t.totalPoints, firstScreenLevel: w.firstScreenLevel,
      firstScreenBytes: w.firstScreenBytes, anchorKind: w.coord.anchor?.kind ?? 'synthetic',
      northConfidence: w.coord.trueNorth?.confidence ?? null, syntheticGroundZ: w.world.render?.syntheticGroundZ ?? null, bboxEnuM: Float64Array.from([...tmin, ...tmax]),
      defaultColorMode: (w.world.render?.defaultColorMode ?? 'height') as ColorMode,
      home: home ? { position: home.position, target: home.target, fovDeg: home.fovDeg ?? 60 } : null,
    }
    if (this.perf) this.perf.load.firstScreenBytes = w.firstScreenBytes
    if (reason !== 'open') this.warmupLeft = PC.freezeFrames
    this.emit('pc.world.opened', { info: this.info, ttfpMs: Number.NaN, reason })
  }

  private growNodeTable(N: number): void {
    const old = this.tables
    this.tables = new DrawTables(Math.max(N, 2 * old.nodeCapacity), old.maxPoints)
    this.texNodes.draw.value = this.tables.draw
    this.texNodes.block.value = this.tables.block
    this.texNodes.node.value = this.tables.node
    old.dispose()
  }

  /** drop the current world (requests, residency, cache); GPU objects stay */
  private release(): void {
    this.abort?.abort()
    this.abort = null
    this.openSeq++
    this.fetcher.cancelAll()
    this.arrivals.length = 0
    this.reqNode.clear()
    this.inflightNodes.length = 0
    this.prefetch.clear()
    this.cache?.clear()
    this.cache = null
    this.world = null
    this.t = null
    this.info = null
    this.alloc.reset()
    this.residentPts = 0
    this.u.numDraws.value = 0
    this.geometry.setDrawRange(0, 0)
    this.st.drawn = 0
    this.dtm.reset()
  }

  close(): void {
    this.release()
    this.phase = 'idle'
    resetStats(this.st)
    this.root.visible = false
  }

  dispose(): void {
    this.offShading()
    this.close()
    this.fetcher.dispose()
    this.geometry.dispose()
    this.material.dispose()
    this.pool.dispose()
    this.tables.dispose()
    this.classTex.dispose()
    this.dummyDtm.dispose()
    this.dtm.dispose()
    this.edlMaterial?.dispose()
    this.picker.dispose()
    if (active === this) setActive(null)
  }

  // ------------------------------------------------------------ settings
  setVisible(v: boolean): void {
    this.visible = v
    this.root.visible = v && this.t !== null
  }
  setClassMask(mask: number): void {
    this.u.classMask.value = mask & PC.classMaskValid // bit 15 (reserved class) is always cleared
  }
  setColorMode(m: ColorMode): void {
    this.u.colorMode.value = COLOR_MODE_INDEX[m] ?? 0
  }
  setHeroClassActive(on: boolean): void {
    this.u.heroActive.value = on ? 1 : 0
  }
  /** true while class 11 is demoted to g50 because another entity owns the red (RedArbiter) */
  get heroClassActive(): boolean {
    return this.u.heroActive.value === 1
  }
  /**
   * legacy lighting uniforms (sun direction ENU and a focus cloud-shadow scalar); the material now takes its light from
   * the scene shading provider (engine/shading.ts, per-vertex cloud shadow), so these values are kept for tests only
   */
  setLighting(sunEnu: ArrayLike<number>, cloudShadow = 1): void {
    this.u.sun.value.set(sunEnu[0], sunEnu[1], sunEnu[2]).normalize()
    this.u.cloudShadow.value = cloudShadow
  }
  setEdl(on: boolean): void {
    this.edlOn = on
    this.applyEdl()
  }
  setQuality(mode: 'auto' | number): void {
    this.cas.setManual(mode === 'auto' ? null : mode)
    this.syncCasStats()
  }
  setFocus(p: Float64Array | null, mode: FocusMode): void {
    this.focus.mode = p ? mode : 'none'
    if (p) this.focus.p.set(p)
  }
  /** called by the host when the boot mask is revealed: the upload quota drops to the per-frame value */
  markRevealed(): void {
    this.revealed = true
  }

  /**
   * D1-ext (FR-020): camera-flight destination prefetch. Runs one selection for the destination with its own scratch
   * (no hysteresis marks) and queues the missing nodes (<= 64) at low priority; null clears (flight interrupted).
   */
  prefetchView(eye: ArrayLike<number> | null, target?: ArrayLike<number>, fovYRad?: number): void {
    const t = this.t
    this.prefetch.clear()
    if (!t || !eye || !target) return
    this.prefetchScratch ??= newScratch(t.N)
    this.prefetchSel ??= newSelection(PC.maxNodes)
    const cam = newLodCamera()
    const H = Math.max(1, this.cam.hPx)
    lodCameraLookAt(eye, target, ((fovYRad ?? Math.PI / 3) * 180) / Math.PI, H * (16 / 9), H, this.cam.near || 1, this.cam.far || 20000, H, cam)
    const o = { ...this.selectOpts, hysteresis: 0, depthCap: 255 }
    selectVisible(t, cam, o, this.prefetchScratch, this.prefetchSel)
    this.prefetch.fill(this.prefetchSel, t, performance.now(), this.reqOfNode)
  }

  stats(): Readonly<PointCloudStats> {
    return this.st
  }

  /** LayerSpec.drawCount: follows exactly what three draws. three issues (and counts) the draw call of a visible Points
   * object even when its draw range is empty (WebGLRenderer.renderBufferDirect only returns early for a negative or
   * infinite count), so a visible layer with st.drawn = 0 (world switch, first screen) is still one call; the previous
   * `st.drawn > 0` made render.calls exceed the pass plan (M06-E007, M06-to-M05 item 1; INT-1) */
  drawCount(): number {
    return this.root.visible ? 1 : 0
  }

  // ------------------------------------------------------------ world phase
  update(ctx: FrameCtx): void {
    const t = this.t
    const cache = this.cache
    const cam = ctx.camera
    if (!t || !cache) return
    const now = ctx.nowMs
    if (!this.drainArrivals(now)) return
    if (!this.visible || this.phase === 'suspended' || !cam) {
      this.writePc(0)
      return
    }
    const t0 = performance.now()
    // camera into the layer frame (the Points object sits under WorldRoot)
    cam.updateMatrixWorld()
    this.root.updateWorldMatrix(true, false)
    this.Linv.copy(this.root.matrixWorld).invert()
    const hPx = Math.max(1, ctx.dbH * (this.be.tier === 'S' ? 1 : ctx.cloudScale))
    makeLodCamera(cam, this.root.matrixWorld.elements, this.Linv.elements, hPx, this.cam)
    const Beff = this.budget()
    const rung = this.cas.rung
    const o = this.selectOpts
    o.tau = rung.tau
    o.B = Beff
    // until the first frame is committed the selection stays capped at the first-screen level L and is the first-frame
    // target set (re-taken every frame, so a camera that moves while opening cannot strand the target set)
    const opening = this.phase === 'first_screen'
    o.depthCap = opening && this.world ? this.world.firstScreenLevel : PC.depthCapNone
    const sel = selectVisible(t, this.cam, o, this.scratch, this.sel)
    const selectMs = performance.now() - t0
    const frame = sel.frame
    for (let k = 0; k < sel.n; k++) {
      const i = sel.idx[k]
      t.lastSeen[i] = frame
      cache.touch(i, frame)
    }
    if (opening) this.captureTarget(sel)
    this.cancelStale(frame)
    if (this.recovering) {
      // device-loss recovery: cached selected nodes keep their hysteresis mark while they are re-uploaded, so the
      // selection does not drift towards nodes that would need a download; dispatch resumes at 0.95 coverage or 2 s
      for (let k = 0; k < sel.n; k++) if (t.cacheSlot[sel.idx[k]] >= 0) t.drawnFrame[sel.idx[k]] = frame
      if (performance.now() - this.readyAt >= PC.recoverDispatchMs) this.recovering = false
    }
    if (!this.recovering) this.dispatch(sel, now)
    // uploads: the whole first-frame target set before the mask is revealed (cold start), the frame quota otherwise
    const perFrame = this.dev.uploadPtsPerFrame
    const burst = !this.revealed && !this.load.isSwitch && this.phase === 'first_screen'
    const quota = burst ? Math.max(perFrame, this.targetPts) : perFrame
    drainUploads(sel, t, quota, now, this.alloc, this.sink, this.up)
    this.residentPts += this.up.used
    if (this.up.stalled) this.st.poolStalls++
    if (this.revealed && this.perf) {
      if (this.up.firstOverQuota) this.perf.pc.uploadOverQuotaFrames = ((this.perf.pc.uploadOverQuotaFrames as number | undefined) ?? 0) + 1
      else if (this.up.used > this.perf.pc.uploadPtsMax) this.perf.pc.uploadPtsMax = this.up.used
    }
    this.evictIfNeeded(frame, now)
    // DrawTable, fade and childDrawnMask, uniforms. While the shader zoo still warms (M06 backend state WARMING) the
    // points are not drawn: the point program is compiled by the zoo, never by the first point frame on the TTFP path;
    // the wait from t_start to the end of the warm-up is load.warmupWaitMs (D1-AC-02; 18 §4.2; FX-WEB1)
    const hold = this.be.state === 'WARMING'
    if (!hold && Number.isNaN(this.warmReadyAt)) this.warmReadyAt = performance.now()
    const reduced = this.motionTier() === 'reduced'
    const db = buildDrawTable(sel, t, now, this.tok.lodFadeMs, this.ease, reduced, this.tables.drawData, this.db)
    this.tables.commit(db.k)
    this.blockWords = buildBlockIndex(this.tables.drawData, db.k, db.drawn, this.tables.blockData)
    this.tables.commitBlocks(this.blockWords)
    this.u.numDraws.value = db.k
    this.geometry.setDrawRange(0, hold ? 0 : db.drawn)
    const e = this.cam.eye
    this.u.eye.value.set(e[0], e[1], e[2])
    this.v3.set(0, 0, -1).applyQuaternion(cam.quaternion).transformDirection(this.Linv)
    this.u.behind.value.set(e[0] - 10 * this.v3.x, e[1] - 10 * this.v3.y, e[2] - 10 * this.v3.z)
    this.u.projK.value = (0.5 * hPx) / this.cam.slope
    this.u.minPx.value = rung.minPx
    this.edlMaterial?.setCamera(cam.near, cam.far)
    // coverage: first-frame target set while opening, the selection afterwards
    let progress: number
    if (this.phase === 'first_screen' && this.targetN > 0) {
      let res = 0
      for (let q = 0; q < this.targetN; q++) {
        const i = this.targetSet[q]
        if (t.numPoints[i] === 0 || (t.poolBase[i] >= 0 && t.drawnFrame[i] === frame)) res++
      }
      progress = res / this.targetN
      if (res === this.targetN && db.drawn > 0 && !hold && !this.load.pendingCommit) this.load.pendingCommit = true
    } else progress = sel.n > 0 ? db.residentSel / sel.n : 1
    if (db.drawn > 0 && !hold && this.load.firstPixelPending) {
      this.load.firstPixelPending = false
      this.load.firstPixelCommit = true
    }
    // adaptive maxPx (M05 §6.7.3): sparse while budget/node limited or streaming, 300 ms smoothing
    const sparse = sel.limitedBy <= LB_NODES || (PC.sparseWhileStreaming && progress < PC.sparseProgress)
    // ADR-063: in dense frames the non-leaf nodes are capped at the tau cap, leaf nodes keep the rung maxPx (maxPxEff,
    // the upper bound reported in __perf.pc.maxPxEff); sparse frames use maxPxSparse for both
    const target = sparse ? rung.maxPxSparse : rung.maxPx
    const targetCap = sparse ? rung.maxPxSparse : tauCapPx(rung)
    const k = 1 - Math.exp(-Math.max(0, ctx.dtMs) / PC.maxPxTauMs)
    if (!(this.st.maxPxEff > 0)) this.st.maxPxEff = target
    if (!(this.st.maxPxCapEff > 0)) this.st.maxPxCapEff = targetCap
    this.st.maxPxEff += (target - this.st.maxPxEff) * k
    this.st.maxPxCapEff += (targetCap - this.st.maxPxCapEff) * k
    this.u.maxPxLeaf.value = this.st.maxPxEff
    this.u.maxPx.value = this.st.maxPxCapEff
    if (this.resetPending && progress >= PC.sparseProgress) {
      this.recovering = false
      this.resetPending = false
      this.emit('pc.gpu.reset', { recoveredMs: performance.now() - this.suspendedAt })
    }
    // statistics
    const s = this.st
    s.progress = progress
    s.drawn = db.drawn
    s.limitedBy = LIMITED[sel.limitedBy]
    s.achievedErrPx = sel.achieved
    s.minSpacingM = sel.minSpacingM
    s.levelCounts.set(sel.levelCounts)
    s.selectMs = selectMs
    s.pendingUploadPts = this.up.pending
    if (sel.limitedBy === LB_BUDGET || sel.limitedBy === LB_NODES) s.fillRate = fillEma(s.fillRate || 1, Beff > 0 ? db.drawn / Beff : 1, ctx.dtMs, PC.fillTauMs)
    s.rsEff = this.be.tier === 'S' ? PC.rsLockSoftware : ctx.cloudScale
    this.writePc(Beff, sel)
  }

  private readonly sink: UploadSink = {
    copy: (base, packed, n) => this.pool.upload(base, packed, n),
    evictFor: (n, now) => this.forceEvict(n, now),
    packed: (i) => this.cache?.get(i) ?? null,
    touched: (i) => this.cache?.touch(i, this.sel.frame),
  }

  /** B_eff = min(B, 0.6 x pool) (CAS already clamps; a locked test budget may not) */
  private budget(): number {
    const B = this.lockB || this.cas.B
    return Math.max(1, Math.floor(Math.min(B, PC.capacityFrac * this.poolCapacityPts)))
  }

  private captureTarget(sel: Selection): void {
    const t = this.t!
    if (this.targetSet.length < sel.n) this.targetSet = new Int32Array(Math.max(sel.n, 64))
    let n = 0
    let pts = 0
    for (let k = 0; k < sel.n; k++) {
      const i = sel.idx[k]
      if (t.numPoints[i] === 0) continue
      this.targetSet[n++] = i
      pts += sel.cnt[k]
    }
    this.targetN = n
    this.targetPts = pts
  }

  // ------------------------------------------------------------ fetching
  /** queued fetch replies into the CPU cache; false when a 409 started a reopen (the world changed) */
  private drainArrivals(now: number): boolean {
    const t = this.t!
    const cache = this.cache!
    if (this.arrivals.length === 0) return true
    for (let a = 0; a < this.arrivals.length; a++) {
      const r = this.arrivals[a]
      const i = this.reqNode.get(r.id)
      if (i === undefined) continue
      this.reqNode.delete(r.id)
      this.reqOfNode[i] = -1
      this.cancelAsked[i] = 0
      const at = this.inflightNodes.indexOf(i)
      if (at >= 0) this.inflightNodes.splice(at, 1)
      if (r.op === 'done') {
        cache.put(i, new Uint32Array(r.buffers[0]), this.sel.frame)
        if (!t.fetchedOnce[i]) {
          t.fetchedOnce[i] = 1
          this.uniqueBytes += r.bytes
        }
        t.attempts[i] = 0
        continue
      }
      if (r.kind === 'abort') {
        t.state[i] = NS.UNLOADED
        continue
      }
      if (r.status === 409) {
        void this.staleReopen()
        return false
      }
      t.attempts[i] = failIsFinal(r.kind) ? PC.maxAttempts : t.attempts[i] + 1
      t.retryAt[i] = nextRetryAt(t.attempts[i], now)
      if (isFailedAfter(t.attempts[i])) {
        t.state[i] = NS.FAILED
        this.st.failed++
        if (now - this.lastNodeFailedAt >= PC.eventThrottleMs) {
          this.lastNodeFailedAt = now
          this.emit('pc.node.failed', { node: i, code: PC_NODE_FAILED })
        }
      } else t.state[i] = NS.RETRY_WAIT
    }
    this.arrivals.length = 0
    cache.trim(this.sel.frame)
    return true
  }

  private staleReopen(): Promise<unknown> {
    const base = this.base
    return this.open(base, { reason: 'stale' }).catch(() => {})
  }

  private fetchNode(i: number): void {
    const t = this.t!
    const spans = new Int32Array(3)
    spans[0] = i
    spans[2] = t.numPoints[i]
    const id = this.fetcher.request(t.rootUrl[t.rootOf[i]], t.byteOffset[i], t.byteOffset[i] + t.byteSize[i] - 1, spans)
    this.reqNode.set(id, i)
    this.reqOfNode[i] = id
    this.inflightNodes.push(i)
    t.state[i] = NS.FETCHING
  }

  private dispatch(sel: Selection, now: number): void {
    const t = this.t!
    const c = collectCandidates(sel, t, now, this.reqOfNode, this.cand)
    this.st.queued = c.n
    if (this.focus.mode !== 'none') reorderWindow(c, 2 * this.fetcher.limit, t, this.cam, this.focus)
    for (let j = 0; j < c.n && !this.fetcher.full; j++) this.fetchNode(c.node[j])
    if (this.fetcher.inflight < this.fetcher.limit / 2) {
      for (;;) {
        if (this.fetcher.inflight >= this.fetcher.limit / 2) break
        const i = this.prefetch.next(t, now, this.reqOfNode)
        if (i < 0) break
        this.fetchNode(i)
      }
    }
  }

  /** abort in-flight fetches of nodes out of the selection and outside the frustum for 2 frames (FR-017) */
  private cancelStale(frame: number): void {
    const t = this.t!
    const pol = this.abortPol
    pol.saturated = this.fetcher.full
    for (let k = 0; k < this.inflightNodes.length; k++) {
      const i = this.inflightNodes[k]
      const id = this.reqOfNode[i]
      if (id < 0 || this.cancelAsked[i]) continue
      const stale = frame - t.lastSeen[i]
      if (stale <= 0) continue
      if (shouldAbort(stale, classifyNode(this.cam.planes, t.tightMin, t.tightMax, i) === OUTSIDE, pol)) {
        this.fetcher.cancel(id) // the abort reply returns the node to UNLOADED
        this.cancelAsked[i] = 1
        this.st.canceled++
      }
    }
  }

  // ------------------------------------------------------------ GPU residency
  private evictNode(i: number): void {
    const t = this.t!
    this.alloc.free(t.poolBase[i], t.numPoints[i])
    t.poolBase[i] = -1
    t.state[i] = t.cacheSlot[i] >= 0 ? NS.CACHED : NS.UNLOADED
    this.residentPts -= t.numPoints[i]
  }

  private gatherEvictionCandidates(frame: number, force: boolean, need: number): EvictionCandidates {
    const t = this.t!
    const c = this.evc
    let n = 0
    for (let i = 0; i < t.N; i++) {
      if (t.poolBase[i] < 0 || t.lastSeen[i] === frame || t.parent[i] < 0) continue
      c.node[n] = i
      c.pts[n] = t.numPoints[i]
      c.level[n] = t.level[i]
      c.dist[n] = distToBox(t.tightMin, t.tightMax, i, this.cam.eye)
      c.outside[n] = classifyNode(this.cam.planes, t.tightMin, t.tightMax, i) === OUTSIDE ? 1 : 0
      c.residentSince[n] = t.residentSince[i]
      n++
    }
    c.n = n
    c.force = force
    c.needPts = need
    return c
  }

  /** B_ref = the current rung's clamped hi (ADR-010) */
  private bRef(): number {
    return Math.min(this.cas.rung.hi, PC.capacityFrac * this.poolCapacityPts)
  }

  private evictIfNeeded(frame: number, now: number): void {
    const Bref = this.bRef()
    if (this.residentPts <= PC.evictTrigger * Bref) return
    const c = this.gatherEvictionCandidates(frame, false, 0)
    const m = planEviction(c, this.residentPts, Bref, now, this.evOut)
    for (let k = 0; k < m; k++) this.evictNode(this.evOut[k])
  }

  private forceEvict(need: number, now: number): number {
    const c = this.gatherEvictionCandidates(this.sel.frame, true, need)
    const m = planEviction(c, this.residentPts, this.bRef(), now, this.evOut)
    for (let k = 0; k < m; k++) this.evictNode(this.evOut[k])
    return m
  }

  // ------------------------------------------------------------ governor phase
  /** after the render phase: TTFP commit, CAS sample (freeze mask), __perf.cas */
  sampleFrame(ctx: FrameCtx): void {
    const now = performance.now()
    if (this.load.firstPixelCommit) {
      this.load.firstPixelCommit = false
      this.load.ttfpFirstPixel = now - this.load.ttfpStart
      if (this.perf && !this.load.isSwitch) this.perf.load.ttfpFirstPixel = this.load.ttfpFirstPixel
    }
    if (this.load.pendingCommit) this.commitFirstFrame(now)
    if (!this.t) return
    this.sampleQuality(ctx)
    const programs = this.be.programsCount()
    const compiled = ctx.compiledThisFrame || (this.lastPrograms >= 0 && programs > this.lastPrograms)
    this.lastPrograms = programs
    const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'
    const fz = this.freezeIn
    fz.frozen = ctx.frozen
    fz.hidden = hidden
    fz.compiled = compiled
    fz.warmupLeft = this.warmupLeft
    let mask = casFreezeMask(fz)
    if (this.phase !== 'streaming') mask |= FREEZE_WARMUP
    if (this.warmupLeft > 0) this.warmupLeft--
    this.st.frozenMask = mask
    if (!this.lockB) {
      if (this.refreshMs && this.be.deviceClass !== 'software') {
        const T = this.refreshMs()
        if (T > 0 && Math.abs(T - this.cas.T) / this.cas.T > 0.05) this.cas.setTarget(T, this.dev.tailK)
      }
      const quota = this.dev.uploadPtsPerFrame
      const pending = this.st.pendingUploadPts > PC.pendingQuotaFactor * quota || this.fetcher.full
      const evals = this.cas.evals
      this.cas.sample(ctx.dtMs, ctx.nowMs, mask, pending)
      if (this.cas.evals !== evals && this.perf) {
        pushRing(this.perf.cas.B_ring, this.cas.B)
        pushRing(this.perf.cas.index_ring, this.cas.index)
      }
      this.trackInBand(ctx.dtMs, ctx.nowMs)
    }
    this.syncCasStats()
  }

  /** intervals presented since the reveal (time, interval), for the in-band time; preallocated, never grows */
  private readonly bandT = new Float64Array(128)
  private readonly bandDt = new Float64Array(128)
  private readonly bandTmp = new Float64Array(128)
  private bandN = 0
  /**
   * cas.inBandAtMs (AWR-18 §1.5): from the reveal to the first time that the p50 of the presented intervals of the last
   * 1 s is <= casInBandR x T*, or B equals the rung's lo. It is a property of the presented frames, so it is evaluated on
   * every frame from the reveal on, also while the CAS itself is frozen (the first 30 frames after the reveal) - the
   * CAS evaluations are not the clock of this measure (FX2-R2: evaluated only at CAS evaluations, a slow first second
   * after the reveal pushed it past 3 s even when the frames were already on target).
   */
  private trackInBand(dtMs: number, nowMs: number): void {
    const perf = this.perf
    if (!perf || !Number.isNaN(perf.cas.inBandAtMs) || !(perf.load.revealAt > 0) || nowMs < perf.load.revealAt) return
    const k = this.bandN++ & 127
    this.bandT[k] = nowMs
    this.bandDt[k] = dtMs
    const sinceReveal = nowMs - perf.load.revealAt
    const atLo = this.cas.B <= this.cas.lo * 1.001
    if (sinceReveal < 1000 && !atLo) return
    // p50 of the intervals presented in the last 1000 ms (nearest rank, as AWR-18 §2.5)
    let m = 0
    const n = Math.min(this.bandN, 128)
    for (let i = 0; i < n; i++) {
      const j = (this.bandN - 1 - i) & 127
      if (nowMs - this.bandT[j] > 1000) break
      if (this.bandDt[j] > 0) this.bandTmp[m++] = this.bandDt[j]
    }
    let p50 = Number.POSITIVE_INFINITY
    if (m > 0) {
      const a = this.bandTmp // insertion sort in place (m <= 128, only until the band is entered; no allocation)
      for (let i = 1; i < m; i++) {
        const v = a[i]
        let j = i - 1
        while (j >= 0 && a[j] > v) {
          a[j + 1] = a[j]
          j--
        }
        a[j + 1] = v
      }
      p50 = a[Math.min(m - 1, Math.floor(0.5 * m))]
    }
    if (atLo || (sinceReveal >= 1000 && p50 <= PC.casInBandR * this.cas.T)) perf.cas.inBandAtMs = sinceReveal
  }

  private commitFirstFrame(now: number): void {
    this.load.pendingCommit = false
    if (this.perf?.marks && !this.load.isSwitch && this.perf.marks['pc.firstFrame'] === undefined) this.perf.marks['pc.firstFrame'] = performance.now()
    // t_start before the end of the shader-zoo warm-up: the wait is excluded from TTFP and reported (D1-AC-02)
    const ready = this.warmReadyAt
    const wait = !this.load.isSwitch && ready > this.load.ttfpStart ? ready - this.load.ttfpStart : 0
    if (this.perf && !this.load.isSwitch) this.perf.load.warmupWaitMs = wait
    this.load.ttfp = now - this.load.ttfpStart - wait
    this.load.switchMs = this.load.isSwitch ? now - this.load.openedAt : Number.NaN
    if (this.perf) {
      if (this.load.isSwitch) this.perf.load.switchMs = this.load.switchMs
      else this.perf.load.ttfp = this.load.ttfp
    }
    this.phase = 'streaming'
    this.st.phase = 'streaming'
    this.warmupLeft = PC.freezeFrames
    this.emit('pc.first.frame', { ttfpMs: this.load.ttfp, switchMs: this.load.switchMs })
    this.startDtm()
  }

  private startDtm(): void {
    const w = this.world
    const href = w?.coord.ground?.dtm?.href
    if (!w || !href || this.dtm.loaded) return
    const seq = this.openSeq
    const f = this.f ?? ((...a: Parameters<typeof fetch>) => fetch(...a))
    const get = async (url: string): Promise<Response> => {
      await this.fetcher.acquireSlot(this.abort?.signal)
      try {
        return await f(url, { signal: this.abort?.signal })
      } finally {
        this.fetcher.release()
      }
    }
    const cls = w.world.layers.find((l) => l.type === 'class-table')
    if (cls) {
      void get(new URL(`${cls.href}?v=${encodeURIComponent(w.world.contentVersion)}`, w.base).href).then(async (r) => {
        if (!r.ok || seq !== this.openSeq) return
        const j = (await r.json()) as { classes?: { index: number; key: string }[] }
        this.classNames = []
        for (const c of j.classes ?? []) this.classNames[c.index] = c.key
      }).catch(() => {})
    }
    void this.dtm.load(w.base, href, w.world.contentVersion, w.coord.ground?.zM ?? 0, get).then(() => {
      if (seq !== this.openSeq || !this.dtm.texture) return
      const g = this.dtm.grid
      this.texNodes.dtm.value = this.dtm.texture
      this.u.dtmOrigin.value.set(g.originX, g.originY)
      this.u.dtmCell.value = g.cellM
      this.u.dtmSize.value.set(g.width, g.height)
      this.u.groundZ.value = this.dtm.groundZ
      this.u.dtmReady.value = 1
    }).catch(() => {})
  }

  /**
   * Quality sampling hook (test builds, ?quality=1; M05-FR-054, AWR-18 §4.5): at the first frame whose flight time
   * reaches 2.5 + 5k s (k = 0..11) the camera pose actually used (eye, target, fovY, near, far; world ENU) is appended to
   * __perf.quality.samples and 'pc.quality.sample' is emitted so the render backend can add the coverage mask of the
   * same DrawTable (mask stays null until M06 renders it); dev/oracles/fullref.html renders the reference at the pose.
   */
  private qualityK = 0
  private sampleQuality(ctx: FrameCtx): void {
    const p = this.perf
    const cam = ctx.camera
    if (!this.qualityHooks || !p?.quality || !p.bench || !cam || this.qualityK >= PC.qualitySamples) return
    const t = p.bench.flightT
    if (!(t >= PC.qualityT0S + PC.qualityStepS * this.qualityK)) return
    const e = cam.position
    this.v3.set(0, 0, -1).applyQuaternion(cam.quaternion)
    const pose = Float64Array.from([e.x, -e.z, e.y, e.x + 100 * this.v3.x, -(e.z + 100 * this.v3.z), e.y + 100 * this.v3.y, cam.fov, cam.near, cam.far])
    p.quality.samples.push({ t, pose, mask: null })
    this.emit('pc.quality.sample', { k: this.qualityK, t })
    this.qualityK++
  }

  private onRung(from: number, to: number, reason: RungReason): void {
    this.applyEdl()
    this.emit('pc.rung.changed', { from, to, reason })
  }

  private syncCasStats(): void {
    const c = this.cas
    const s = this.st
    const st = c.state()
    s.B = c.B
    s.Beff = this.budget()
    s.lo = st.lo
    s.hi = st.hi
    s.Bfloor = st.Bfloor
    s.rungIndex = c.index
    s.rungName = c.rung.name
    s.manual = c.manual
    s.floorHeld = c.floorHeld
    const clamped = this.lockB ? this.lockB > PC.capacityFrac * this.poolCapacityPts : c.clampedByCapacity
    if (clamped !== this.lastClamped) {
      this.lastClamped = clamped
      this.emit('pc.capacity.clamped', { clamped, Beff: s.Beff, capacity: this.poolCapacityPts })
    }
    s.clampedByCapacity = clamped
    const p = this.perf
    if (!p) return
    const pc = p.cas
    pc.index = c.index
    pc.B = s.Beff
    pc.lo = st.lo
    pc.hi = st.hi
    pc.B_floor = st.Bfloor
    pc.rungChanges = c.rungChanges
    pc.bounces = c.bounces
    pc.reversals = c.reversals
    pc.frozenFrames = c.frozenFrames
    pc.evals = c.evals
    pc.atFloorSinceMs = st.atFloorSinceMs
    pc.atCeilSinceMs = st.atCeilSinceMs
  }

  private writePc(Beff: number, sel?: Selection): void {
    const s = this.st
    s.inflight = this.fetcher.inflight
    s.residentPts = this.residentPts
    s.cpuCacheBytes = this.cache?.bytes ?? 0
    s.pageUtil = this.alloc.usedTexels > 0 ? this.residentPts / this.alloc.usedTexels : 0
    s.downloadedBytes = this.fetcher.downloadedBytes
    s.uniqueBytes = this.uniqueBytes
    const p = this.perf
    if (!p) return
    const pc = p.pc
    pc.drawn = s.drawn
    pc.inflight = s.inflight
    pc.queued = s.queued
    pc.failed = s.failed
    pc.canceled = s.canceled
    pc.residentPts = s.residentPts
    if (s.residentPts > pc.residentPeak) pc.residentPeak = s.residentPts
    pc.cpuCacheBytes = s.cpuCacheBytes
    if (s.cpuCacheBytes > pc.cpuCachePeak) pc.cpuCachePeak = s.cpuCacheBytes
    pc.downloadedBytes = s.downloadedBytes
    pc.uniqueBytes = s.uniqueBytes
    pc.progress = s.progress
    pc.clampedByCapacity = s.clampedByCapacity
    pc.fillRate = s.fillRate
    pc.maxPxEff = s.maxPxEff
    pc.rsEff = s.rsEff
    pc.poolStalls = s.poolStalls
    pc.pageUtil = s.pageUtil
    if (!sel) return
    const lb = sel.limitedBy
    pc.limitedBy = lb
    pc.limitedByHist[lb]++
    if (s.drawn > Beff) pc.budgetViolations++
    pushRing(pc.achievedErr, sel.achieved)
    pushRing(pc.selectMs, s.selectMs)
    pushRing(pc.drawn_ring, s.drawn)
    pushRing(pc.B_ring, Beff)
    pushRing(pc.limitedBy_ring, lb)
  }

  // ------------------------------------------------------------ EDL (Tier B/A)
  private applyEdl(): void {
    const m = this.edlMaterial
    if (!m) return
    const taps = this.cas.rung.edlTaps
    m.uniforms.strength.value = this.edlOn && taps > 0 ? this.tok.edlStrength : 0
    m.uniforms.taps.value = taps > 0 ? taps : PC.edlTapsLow
  }

  // ------------------------------------------------------------ device loss (FR-027)
  onBackendLost(): void {
    const t = this.t
    this.phase = this.t ? 'suspended' : this.phase
    this.st.phase = this.phase
    this.suspendedAt = performance.now()
    if (t) {
      for (let i = 0; i < t.N; i++) {
        if (t.poolBase[i] < 0) continue
        t.poolBase[i] = -1
        t.state[i] = t.cacheSlot[i] >= 0 ? NS.CACHED : NS.UNLOADED
      }
    }
    this.residentPts = 0
    this.alloc.reset()
    this.u.numDraws.value = 0
    this.geometry.setDrawRange(0, 0)
    this.st.drawn = 0
  }

  get backend(): RenderBackendView {
    return this.be
  }
  /** base URL of the current (or opening) world */
  get worldBase(): string {
    return this.base
  }

  onBackendReady(be: RenderBackendView): void {
    if (be === this.be && this.phase !== 'suspended') return // already rebuilt for this backend (hook and remount both call it)
    if (this.phase !== 'suspended') this.onBackendLost()
    this.be = be
    this.pool.dispose()
    this.tables.dispose()
    this.classTex.dispose()
    this.dummyDtm.dispose()
    this.material.dispose()
    this.pool = new PointPool(this.dev.poolRows, 2, be.textures)
    if (this.t) this.pool.ensureStaging(this.t.maxNodePoints)
    this.tables = new DrawTables(Math.max(4096, this.t?.N ?? 0), this.poolCapacityPts)
    if (this.t) this.tables.setNodes(this.t)
    this.classTex = makeClassTexture(this.tok.classColors)
    this.dummyDtm = dummyDtmTexture()
    if (this.dtm.loaded && this.dtm.texture) this.dtm.texture.needsUpdate = true
    this.texNodes = makeTextureNodes({ pool: this.pool.tex, draw: this.tables.draw, block: this.tables.block, node: this.tables.node, dtm: this.dtm.texture ?? this.dummyDtm,
      classes: this.classTex })
    this.material = this.buildMaterial()
    this.root.material = this.material
    ;(this.picker.object.material as PointsNodeMaterial).dispose()
    this.picker.object.material = this.buildIdMaterial()
    this.edlMaterial?.dispose()
    this.edlMaterial = this.buildEdl()
    this.lastPrograms = -1
    if (this.t) {
      this.phase = 'streaming'
      this.st.phase = 'streaming'
      this.recovering = true
      this.resetPending = true
      this.readyAt = performance.now()
      this.warmupLeft = PC.freezeFrames
    }
  }

  // ------------------------------------------------------------ warm-up (M05 §6.7.7, FR-037)
  /** words of the block index written by the last frame (re-uploaded after a warm-up variant) */
  private blockWords = 0
  private readonly warmSave = { words: new Uint32Array(4), blocks: new Uint32Array(2), numDraws: 0, count: 0, visible: false, active: false, pick: false }
  /**
   * Shader-zoo variant (M06): make the layer drawable with one DrawTable entry and one point so the point program of
   * this tier compiles under the boot mask; colour modes, fade, class mask and EDL are uniforms (no other variant).
   */
  warmupBegin(pick = false): void {
    const w = this.warmSave
    if (w.active) return
    w.active = true
    w.pick = pick
    if (pick) {
      // the ID program: one pick-table entry, one point
      this.picker.data.set([0, 0, 1, (255 << 24) >>> 0], 0)
      this.picker.tex.needsUpdate = true
      this.picker.numDraws.value = 1
      this.picker.object.geometry.setDrawRange(0, 1)
      this.picker.object.visible = true
      return
    }
    const d = this.tables.drawData
    w.words.set(d.subarray(0, 4))
    w.blocks.set(this.tables.blockData.subarray(0, 2))
    this.tables.blockData[0] = 0
    this.tables.blockData[1] = 0
    this.tables.commitBlocks(2)
    w.numDraws = this.u.numDraws.value as number
    w.count = this.geometry.drawRange.count
    w.visible = this.root.visible
    d[0] = 0
    d[1] = 0
    d[2] = 1
    d[3] = (255 << 24) >>> 0
    this.tables.commit(1)
    this.u.numDraws.value = 1
    this.geometry.setDrawRange(0, 1)
    this.root.visible = true
  }
  warmupEnd(): void {
    const w = this.warmSave
    if (!w.active) return
    w.active = false
    if (w.pick) {
      this.picker.numDraws.value = 0
      this.picker.object.geometry.setDrawRange(0, 0)
      this.picker.end()
      return
    }
    this.tables.drawData.set(w.words, 0)
    this.tables.commit(Math.max(1, w.numDraws))
    this.tables.blockData.set(w.blocks, 0)
    this.tables.commitBlocks(Math.max(2, this.blockWords))
    this.u.numDraws.value = w.numDraws
    this.geometry.setDrawRange(0, w.count)
    // the live visibility, not the one saved by warmupBegin: the zoo awaits compileAsync for seconds while the loop runs,
    // and a world that opened in that window set root.visible = true (open()); restoring the value saved before it left
    // the cloud undrawn for the whole session while selection, streaming and the CAS went on (ACC-4 4.2b: the pass plan
    // without the point pass in 2 of 47 runs, 4 of 9 ladder n200 runs). The table and draw range are rebuilt by the next
    // frame anyway (FX2-R5, ADR-076)
    this.root.visible = this.visible && this.t !== null
  }

  /** stand-in pool texel used by warm-up variants of other hosts (1 texel) */
  static dummyPoolTexture(): DataTexture {
    return dummyIntTexture()
  }
}
