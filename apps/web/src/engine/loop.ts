// Frame phase scheduler (AWR-03 §3.6; AWR-10 §6.3; M06 §6.6, FR-017, FR-019, FR-022). Owner: M06.
// Pure TypeScript: no React, no store imports (AWR-03 §4.2, TS-BND-01). About 150 lines of logic; no @pmndrs/scheduler.
//
// Order per frame: telemetry, clock, drones, camera, world, then advance(nowMs / 1000) (declarative useFrame, then the
// single priority-1 subscriber that runs the `render` phase), then overlay and governor. Without an advance hook (no
// canvas mounted, report pages, unit tests) the `render` phase runs directly between world and overlay.
// Every task is timed with performance.now() and its wall time is added to its perf layer (AWR-18 §9.2 LayerId,
// mainJs when omitted); the phase totals give "our logic" per frame (all phases minus render, AWR-18 §6.3 item 3).
// Hot path: no allocation per frame (preallocated FrameCtx and timing arrays, index loops over task arrays).
// Freeze (ADR-012): a frame cap, an explicit freeze counter (reveal + 30 frames, world switch) or a program compile in
// the previous frame set ctx.frozen; CAS and PerfGovernor do not evaluate frozen frames.
import type { Box2, DataTexture, PerspectiveCamera, RenderTarget, Texture, Vector2 } from 'three'
import type { PointsNodeMaterial } from 'three/webgpu'

export type Phase = 'telemetry' | 'clock' | 'drones' | 'camera' | 'world' | 'render' | 'overlay' | 'governor'
export type Tier = 'A' | 'B' | 'S'
export type DeviceClass = 'dGPU' | 'iGPU' | 'software'
/** AWR-18 §9.2 layer keys for __perf.layers timing; mainJs when omitted */
export type PerfLayerId = 'pointcloud' | 'drones' | 'trails' | 'frustums' | 'environment' | 'groundSky' | 'labels' | 'hudCharts' | 'mainJs'
export const PERF_LAYERS: readonly PerfLayerId[] = ['pointcloud', 'drones', 'trails', 'frustums', 'environment', 'groundSky', 'labels', 'hudCharts', 'mainJs']
/** CPU ms per perf layer of the current frame (task wall time summed; reset at frame start, read by engine/perf) */
export const layerMs = new Float64Array(PERF_LAYERS.length)

export type PointSizeMode = 'glpoint' | 'quad' | 'pixel'
export type RTName = 'cloud' | 'cloudHalf' | 'pick' | 'selftest' | 'bench' | 'quality'
export interface BackendCaps {
  readonly reversedZ: boolean
  readonly timerQuery: boolean
  /** KHR_parallel_shader_compile (optional in hand-made test backends) */
  readonly parallelCompile?: boolean
  readonly compute?: boolean
  readonly mrt?: false
  readonly readbackTopDown: boolean
  readonly maxTextureSize: number
  readonly floatColorRT: boolean
}
/** M05 PointPool upload without a CPU mirror (M05 §7.2; three r186 initTexture, copyTextureToTexture) */
export interface TextureOps {
  initTexture(t: Texture): void
  copyTextureToTexture(src: DataTexture, dst: DataTexture, srcRegion: Box2, dstPosition: Vector2): void
}
export interface RTOptions { depthTexture?: boolean; halfFloat?: boolean; scaleOfCanvas?: number; width?: number; height?: number }
/** read-only view of the RenderBackend for engine modules (M06 §6.2.1); the implementation lives in viewport/backend */
export interface RenderBackendView {
  readonly tier: Tier
  readonly deviceClass: DeviceClass
  readonly kind: 'webgl2' | 'webgpu'
  readonly pointSizeMode: PointSizeMode
  readonly caps: BackendCaps
  /** 0-5, start rung of the point cloud ladder (ADR-044): software 0, iGPU 3, dGPU 4 (+1 with microbench headroom) */
  readonly startRung: number
  /** S: 0; B/A: 2 (M06 §6.18) */
  readonly lowestAllowedRung: number
  readonly textures: TextureOps
  /** every allocation counts gpu.rtAllocs + 1 */
  createRT(name: RTName, o?: RTOptions): RenderTarget
  /** RGBA8, rows bottom-up on every backend (Tier A flips); always asynchronous (M06-FR-008) */
  readPixels(rt: RenderTarget, x: number, y: number, w: number, h: number, out: Uint8Array): Promise<Uint8Array>
  createPointsMaterial(): PointsNodeMaterial
  programsCount(): number
  /** WARMING until the shader zoo finished (M06 §6.4); layers may hold their first draw on it (optional for test doubles) */
  readonly state?: 'WARMING' | 'READY' | 'LOST' | 'FAILED'
}

export const PHASES: readonly Phase[] = ['telemetry', 'clock', 'drones', 'camera', 'world', 'render', 'overlay', 'governor']
const PRE: readonly Phase[] = ['telemetry', 'clock', 'drones', 'camera', 'world']
const POST: readonly Phase[] = ['overlay', 'governor']
const RENDER_IDX = 5

export interface FrameCtx {
  frameNo: number
  nowMs: number
  dtMs: number
  /** render time and focus time in seconds from the session start (M12 SimClockView; 0 until the clock is wired) */
  tRenderS: number
  tFocusS: number
  simRate: number
  clockState: number
  camera: PerspectiveCamera | null
  cssW: number
  cssH: number
  dbW: number
  dbH: number
  /** dbW / cssW (Tier S 0.5) */
  dpr: number
  cloudScale: number
  tier: Tier
  deviceClass: DeviceClass
  /** 0 = Tier S caps, 1 = Tier B/A caps */
  capsIdx: 0 | 1
  moving: boolean
  /** true while a frame cap is active or a freeze is requested (CAS and PerfGovernor do not evaluate, ADR-012) */
  frozen: boolean
  /** the previous frame compiled a program (gpu.programs grew) */
  compiledThisFrame: boolean
  benchLayers: boolean
  /** the render backend once the canvas exists (M06 §6.6) */
  be: RenderBackendView | null
}

export type TaskFn = (ctx: FrameCtx) => void
export interface TaskOptions {
  order?: number
  fps?: number
  tiers?: readonly Tier[]
  layer?: PerfLayerId
}
interface Task {
  id: string
  fn: TaskFn
  order: number
  periodMs: number
  last: number
  tiers: readonly Tier[] | null
  layerIdx: number
}

const tasks: Record<Phase, Task[]> = { telemetry: [], clock: [], drones: [], camera: [], world: [], render: [], overlay: [], governor: [] }

export const ctx: FrameCtx = {
  frameNo: 0, nowMs: 0, dtMs: 0, tRenderS: 0, tFocusS: 0, simRate: 1, clockState: 0, camera: null,
  cssW: 0, cssH: 0, dbW: 0, dbH: 0, dpr: 1, cloudScale: 1, tier: 'S', deviceClass: 'software', capsIdx: 0,
  moving: false, frozen: false, compiledThisFrame: false, benchLayers: false, be: null,
}

/** Register a task in a phase. Returns the unregister function. fn must not allocate. Same id replaces. */
export function register(phase: Phase, id: string, fn: TaskFn, opts: TaskOptions = {}): () => void {
  const list = tasks[phase]
  const layer = opts.layer ?? 'mainJs'
  const task: Task = {
    id, fn, order: opts.order ?? 0, periodMs: opts.fps && opts.fps > 0 ? 1000 / opts.fps : 0, last: Number.NEGATIVE_INFINITY,
    tiers: opts.tiers ?? null, layerIdx: Math.max(0, PERF_LAYERS.indexOf(layer)),
  }
  const dup = list.findIndex((t) => t.id === id)
  if (dup >= 0) list.splice(dup, 1)
  list.push(task)
  list.sort((a, b) => a.order - b.order)
  return () => {
    const i = list.indexOf(task)
    if (i >= 0) list.splice(i, 1)
  }
}

export function registered(phase?: Phase): readonly string[] {
  return phase ? tasks[phase].map((t) => t.id) : PHASES.flatMap((p) => tasks[p].map((t) => `${p}:${t.id}`))
}

/** wall ms per phase of the current frame (index = PHASES order) */
export const phaseMs = new Float64Array(PHASES.length)
let errorsLogged = 0
export function runPhase(phase: Phase, c: FrameCtx = ctx): void {
  const list = tasks[phase]
  const p0 = performance.now()
  for (let i = 0; i < list.length; i++) {
    const t = list[i]
    if (t.tiers !== null && t.tiers.indexOf(c.tier) < 0) continue
    if (t.periodMs > 0) {
      if (c.nowMs - t.last < t.periodMs - 1) continue // fps tasks quantised to frame boundaries (n05 §0 item 12)
      t.last = c.nowMs
    }
    const t0 = performance.now()
    try {
      t.fn(c)
    } catch (e) {
      if (errorsLogged++ < 20) console.error(`loop task ${phase}:${t.id} failed`, e)
    }
    layerMs[t.layerIdx] += performance.now() - t0
  }
  phaseMs[PHASES.indexOf(phase)] += performance.now() - p0
}

// ------------------------------------------------------------------ events (synchronous, M06 §7.2)
type EventCb = (payload: unknown) => void
const eventMap = new Map<string, Set<EventCb>>()
export const events = {
  on<T = unknown>(name: string, cb: (payload: T) => void): () => void {
    let s = eventMap.get(name)
    if (!s) eventMap.set(name, (s = new Set()))
    s.add(cb as EventCb)
    return () => {
      s!.delete(cb as EventCb)
    }
  },
  emit(name: string, payload: unknown): void {
    const s = eventMap.get(name)
    if (s) for (const cb of s) cb(payload)
  },
}

// ------------------------------------------------------------------ driver
let handle = 0
let running = false
let suspended = false
let capFps = 0
let freezeFrames = 0
let lastRunMs = Number.NEGATIVE_INFINITY
let advance: ((timestampS: number) => void) | null = null
const listeners: Array<(running: boolean) => void> = []
/** frame hook of engine/perf: called at the start of every executed frame with the rAF timestamp (FrameSampler) */
let onFrameStart: ((nowMs: number) => void) | null = null
let onFrameEnd: ((nowMs: number) => void) | null = null
/** timing of the last executed frame (ms): render phase, whole callback, our logic (all phases minus render) */
export const frameTiming = { renderMs: 0, frameMs: 0, oursMs: 0, cbStart: 0 }
/** callback start times of the last 256 frames (performance.now), for LoAF loop-callback identification (engine/perf/loaf.ts) */
export const cbStarts = new Float64Array(256)
let cbN = 0

export function frame(nowMs: number): void {
  if (running) handle = requestAnimationFrame(frame) // schedule first: a throwing frame never stops the loop
  if (suspended) return
  if (capFps > 0 && nowMs - lastRunMs < 1000 / capFps - 2) return
  const t0 = performance.now()
  frameTiming.cbStart = t0
  cbStarts[cbN++ & 255] = t0
  ctx.frameNo++
  ctx.dtMs = lastRunMs === Number.NEGATIVE_INFINITY ? 0 : nowMs - lastRunMs
  ctx.nowMs = nowMs
  ctx.frozen = capFps > 0 || freezeFrames > 0 || ctx.compiledThisFrame
  if (freezeFrames > 0) freezeFrames--
  onFrameStart?.(nowMs)
  layerMs.fill(0)
  phaseMs.fill(0)
  for (let i = 0; i < PRE.length; i++) runPhase(PRE[i])
  if (advance) advance(nowMs / 1000)
  else runPhase('render')
  // declarative useFrame work inside advance() counts as ours, the render phase itself does not (AWR-18 §6.3 item 3)
  frameTiming.renderMs = phaseMs[RENDER_IDX]
  for (let i = 0; i < POST.length; i++) runPhase(POST[i])
  lastRunMs = nowMs
  frameTiming.frameMs = performance.now() - t0
  frameTiming.oursMs = Math.max(0, frameTiming.frameMs - phaseMs[RENDER_IDX])
  onFrameEnd?.(nowMs)
}

export const loop = {
  register,
  runPhase,
  start(): void {
    if (running || typeof requestAnimationFrame !== 'function') return
    running = true
    handle = requestAnimationFrame(frame)
    for (const l of listeners) l(true)
  },
  stop(): void {
    if (!running) return
    running = false
    cancelAnimationFrame(handle)
    for (const l of listeners) l(false)
  },
  get running(): boolean {
    return running && !suspended
  },
  get frameNo(): number {
    return ctx.frameNo
  },
  /** R3F advance hook installed by viewport/LoopDriver (seconds, AWR-10 §6.3); null restores direct render */
  setAdvance(fn: ((timestampS: number) => void) | null): void {
    advance = fn
  },
  /** 0 = uncapped; modal 15, overlay pages 5 (AWR-14 §2.1) */
  setFrameCap(fps: number): void {
    capFps = fps > 0 ? fps : 0
  },
  get frameCap(): number {
    return capFps
  },
  setSuspended(on: boolean): void {
    if (suspended === on) return
    suspended = on
    for (const l of listeners) l(!on && running)
  },
  get suspended(): boolean {
    return suspended
  },
  /** freeze CAS and PerfGovernor for the next n frames (reveal + 30, world switch; ADR-012) */
  freeze(frames: number): void {
    freezeFrames = Math.max(freezeFrames, frames | 0)
  },
  get freezeFrames(): number {
    return freezeFrames
  },
  setTier(tier: Tier, deviceClass: DeviceClass): void {
    ctx.tier = tier
    ctx.deviceClass = deviceClass
    ctx.capsIdx = tier === 'S' ? 0 : 1
  },
  /** engine/perf installs its FrameSampler here (one hook each) */
  setFrameHook(fn: ((nowMs: number) => void) | null): void {
    onFrameStart = fn
  },
  setFrameEndHook(fn: ((nowMs: number) => void) | null): void {
    onFrameEnd = fn
  },
  onRunningChange(cb: (running: boolean) => void): () => void {
    listeners.push(cb)
    return () => {
      const i = listeners.indexOf(cb)
      if (i >= 0) listeners.splice(i, 1)
    }
  },
}
