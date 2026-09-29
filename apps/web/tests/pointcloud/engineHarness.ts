// Node harness for the PointCloudEngine: a mock RenderBackendView (texture copies recorded, no GL), a fetch that serves
// the generated World Packages (whole GETs and single Range requests with Content-Range, 409 for a stale ?v=, optional
// failures), a camera under a WorldRoot-like group (rotation.x = -pi/2) driven from flight60 rows, and a simulated clock
// (33.3 ms frames; fetch replies resolve between frames).
import { closeSync, existsSync, fstatSync, openSync, readFileSync, readSync } from 'node:fs'
import { Group, PerspectiveCamera } from 'three'
import { PointsNodeMaterial } from 'three/webgpu'
import type { FrameCtx, RenderBackendView, Tier, DeviceClass } from '@/engine/loop'
import { PointCloudEngine, type PointCloudEngineOptions } from '@/engine/pointcloud/PointCloudEngine'
import { newStats } from '@/engine/pointcloud/core/stats'
import type { PerfSink } from '@/engine/pointcloud/core/stats'
import { WORLDS } from './helpers'

export interface ReqLog { url: string; range: string | null; cache?: string; at: number; status: number }

export function mockBackend(o: { tier?: Tier; deviceClass?: DeviceClass; startRung?: number; lowest?: number } = {}): RenderBackendView & { copies: number; inits: number } {
  const tier = o.tier ?? 'S'
  const dc = o.deviceClass ?? 'software'
  const be = {
    tier, deviceClass: dc, kind: 'webgl2' as const, pointSizeMode: 'glpoint' as const,
    caps: { reversedZ: true, timerQuery: false, readbackTopDown: false, maxTextureSize: 16384, floatColorRT: true },
    startRung: o.startRung ?? (dc === 'software' ? 0 : dc === 'iGPU' ? 3 : 4), lowestAllowedRung: o.lowest ?? (tier === 'S' ? 0 : 2),
    copies: 0, inits: 0,
    textures: {
      initTexture: () => {
        be.inits++
      },
      copyTextureToTexture: () => {
        be.copies++
      },
    },
    createRT: () => {
      throw new Error('not in the Node harness')
    },
    readPixels: async (_rt: unknown, _x: number, _y: number, _w: number, _h: number, out: Uint8Array) => out,
    createPointsMaterial: () => new PointsNodeMaterial(),
    programsCount: () => 0,
  }
  return be as unknown as RenderBackendView & { copies: number; inits: number }
}

export class WorldServer {
  readonly log: ReqLog[] = []
  inflight = 0
  maxInflight = 0
  /** override of the served contentVersion (simulates a rebuilt world) */
  cvOverride: string | null = null
  failRate = 0
  private seed = 7
  private readonly fds = new Map<string, number>()
  now = (): number => 0

  constructor(readonly delayMs = 0) {}

  private rand(): number {
    this.seed = (Math.imul(this.seed, 1664525) + 1013904223) >>> 0
    return this.seed / 4294967296
  }

  private fd(rel: string): number {
    let fd = this.fds.get(rel)
    if (fd === undefined) {
      fd = openSync(new URL(rel, WORLDS), 'r')
      this.fds.set(rel, fd)
    }
    return fd
  }

  readonly fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const range = new Headers(init?.headers).get('Range')
    const u = new URL(url)
    const rel = decodeURIComponent(u.pathname.replace(/^\/worlds\//, ''))
    const entry: ReqLog = { url, range, cache: init?.cache, at: this.now(), status: 0 }
    this.log.push(entry)
    this.inflight++
    this.maxInflight = Math.max(this.maxInflight, this.inflight)
    try {
      if (this.delayMs > 0) await new Promise((r) => setTimeout(r, this.delayMs))
      if (init?.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' })
      const p = new URL(rel, WORLDS)
      if (!existsSync(p)) return (entry.status = 404, new Response('missing', { status: 404 }))
      const v = u.searchParams.get('v')
      if (rel.endsWith('world.json')) {
        const w = JSON.parse(readFileSync(p, 'utf8'))
        if (this.cvOverride) w.contentVersion = this.cvOverride
        entry.status = 200
        return new Response(JSON.stringify(w), { status: 200 })
      }
      if (this.cvOverride && v !== this.cvOverride) return (entry.status = 409, new Response('stale', { status: 409 }))
      if (range) {
        if (this.failRate > 0 && this.rand() < this.failRate) return (entry.status = 503, new Response('busy', { status: 503 }))
        const m = /^bytes=(\d+)-(\d+)$/.exec(range)!
        const a = Number(m[1])
        const b = Number(m[2])
        const fd = this.fd(rel)
        const total = fstatSync(fd).size
        const buf = Buffer.alloc(b - a + 1)
        readSync(fd, buf, 0, buf.length, a)
        entry.status = 206
        return new Response(buf, { status: 206, headers: { 'Content-Range': `bytes ${a}-${b}/${total}` } })
      }
      entry.status = 200
      return new Response(readFileSync(p), { status: 200 })
    } finally {
      this.inflight--
    }
  }) as typeof fetch

  close(): void {
    for (const fd of this.fds.values()) closeSync(fd)
    this.fds.clear()
  }
}

export function makePerf(): PerfSink {
  const ring = () => ({ buf: new Float64Array(4096), n: 0 })
  return {
    forced: null,
    load: { ttfp: Number.NaN, ttfpFirstPixel: Number.NaN, switchMs: Number.NaN, revealAt: 1, firstScreenBytes: 0, warmupWaitMs: 0 },
    cas: { index: 0, B: 0, lo: 0, hi: 0, B_floor: 0, rungChanges: 0, bounces: 0, reversals: 0, inBandAtMs: Number.NaN, frozenFrames: 0, evals: 0, B_ring: ring(),
      index_ring: ring(), atFloorSinceMs: 0, atCeilSinceMs: 0 },
    pc: { drawn: 0, limitedBy: 4, limitedByHist: new Uint32Array(5), achievedErr: ring(), selectMs: ring(), drawn_ring: ring(), B_ring: ring(), limitedBy_ring: ring(),
      budgetViolations: 0, inflight: 0, queued: 0, failed: 0, canceled: 0, residentPts: 0, residentPeak: 0, cpuCacheBytes: 0, cpuCachePeak: 0, downloadedBytes: 0,
      uniqueBytes: 0, uploadPtsMax: 0, progress: 0, poolRows: 0, clampedByCapacity: false },
  }
}

export interface Rig {
  engine: PointCloudEngine
  be: ReturnType<typeof mockBackend>
  server: WorldServer
  perf: PerfSink
  camera: PerspectiveCamera
  ctx: FrameCtx
  /** eye and target in ENU */
  look(eye: ArrayLike<number>, target: ArrayLike<number>): void
  /** one frame: world phase, (render), governor phase, then let fetch replies land */
  frame(dtMs?: number): Promise<void>
  frames(n: number, each?: (k: number) => void, dtMs?: number): Promise<void>
  dispose(): void
}

export function makeRig(o: { be?: ReturnType<typeof mockBackend>; opts?: Partial<PointCloudEngineOptions>; delayMs?: number; dbH?: number } = {}): Rig {
  const be = o.be ?? mockBackend()
  const server = new WorldServer(o.delayMs ?? 0)
  const perf = makePerf()
  const engine = new PointCloudEngine({ backend: be, perf, f: server.fetch, useWorker: false, ...o.opts })
  const worldRoot = new Group()
  worldRoot.rotation.x = -Math.PI / 2
  worldRoot.add(engine.root)
  worldRoot.updateMatrixWorld(true)
  const camera = new PerspectiveCamera(60, 16 / 9, 1, 20000)
  const ctx = {
    frameNo: 0, nowMs: 1000, dtMs: 33.3, tRenderS: 0, tFocusS: 0, simRate: 1, clockState: 0, camera, cssW: 1280, cssH: 720, dbW: 640, dbH: o.dbH ?? 360, dpr: 0.5,
    cloudScale: 1, tier: be.tier, deviceClass: be.deviceClass, capsIdx: 0, moving: false, frozen: false, compiledThisFrame: false, benchLayers: false, be,
  } as unknown as FrameCtx
  server.now = () => ctx.nowMs
  const rig: Rig = {
    engine, be, server, perf, camera, ctx,
    look(eye, target) {
      camera.position.set(eye[0], eye[2], -eye[1])
      camera.up.set(0, 1, 0)
      camera.lookAt(target[0], target[2], -target[1])
      camera.updateMatrixWorld(true)
    },
    async frame(dtMs = 33.3) {
      ctx.frameNo++
      ctx.dtMs = dtMs
      ctx.nowMs += dtMs
      engine.update(ctx)
      engine.sampleFrame(ctx)
      await new Promise((r) => setTimeout(r, 0))
      await new Promise((r) => setTimeout(r, 0))
    },
    async frames(n, each, dtMs) {
      for (let k = 0; k < n; k++) {
        await rig.frame(dtMs)
        each?.(k)
      }
    },
    dispose() {
      engine.dispose()
      server.close()
    },
  }
  return rig
}

export const worldBase = (city: string): string => `http://awr.test/worlds/${city}/`

export { newStats }
