// Fetcher (M05 §6.5, M05-FR-015..018; M05 §9.1 fetcher.ts + workerPool.ts): bounded in-flight Range requests through a
// small worker pool (Tier S 1 worker, Tier B/A 2), abort by id, crash recovery (a worker error rebuilds the worker and
// re-sends its requests, 408 PC_WORKER_CRASHED), test-build failure injection, HTTP cap. Owner: M05.
// Every /worlds/** request of M05 shares the in-flight count: node Ranges, first-screen Ranges and auxiliary GETs (DTM)
// through acquire()/release(). Replies are handed to one callback; the engine only queues them (callbacks never touch
// GPU state). Without Worker (Node tests) jobs run in process with the same code.
import { PC } from '../params'
import { lcg, runRange, type JobConfig, type WorkerIn, type WorkerOut } from './fetchJob'

export interface WorkerPort {
  postMessage(m: WorkerIn, transfer?: Transferable[]): void
  onmessage: ((e: { data: WorkerOut }) => void) | null
  onerror: ((e: unknown) => void) | null
  terminate(): void
}

export interface FetcherOptions {
  workers: number
  /** in-flight limit (tier value; the HTTP cap is applied with setHttpCap) */
  limit: number
  useWorker?: boolean
  f?: typeof fetch
  /** test builds: failure probability per request (pcInject=fail:<p>) */
  injectFail?: number
  seed?: number
  /** worker factory (tests) */
  spawn?: () => WorkerPort
}

interface Pending {
  msg: Extract<WorkerIn, { op: 'range' }>
  worker: number
  resolve: ((r: WorkerOut) => void) | null
}

export class Fetcher {
  private seq = 0
  private readonly pending = new Map<number, Pending>()
  private readonly local = new Map<number, AbortController>()
  private readonly workers: (WorkerPort | null)[]
  private readonly spawn: (() => WorkerPort) | null
  private readonly f: typeof fetch
  private rr = 0
  private tierLimit: number
  private httpCap = Number.POSITIVE_INFINITY
  private aux = 0
  private cfg: JobConfig
  private init: Extract<WorkerIn, { op: 'init' }>
  inflight = 0
  downloadedBytes = 0
  crashes = 0
  /** single reply sink (engine queue); first-screen requests resolve their own promise instead */
  onReply: ((r: WorkerOut) => void) | null = null

  constructor(o: FetcherOptions) {
    this.tierLimit = o.limit
    this.f = o.f ?? ((...a: Parameters<typeof fetch>) => fetch(...a))
    const useWorker = o.useWorker ?? typeof Worker !== 'undefined'
    this.spawn = o.spawn ?? (useWorker
      ? () => new Worker(new URL('./worker/fetch.worker.ts', import.meta.url), { type: 'module', name: 'pc-fetch' }) as unknown as WorkerPort
      : null)
    this.workers = new Array<WorkerPort | null>(Math.max(1, o.workers)).fill(null)
    this.init = { op: 'init', bpp: 12, compression: 'none', injectFail: o.injectFail ?? 0, seed: o.seed ?? 1 }
    this.cfg = { bpp: 12, compression: 'none', injectFail: o.injectFail ?? 0, rand: lcg(o.seed ?? 1) }
  }

  /** in-flight limit: min(tier value, HTTP cap) (M05-FR-016) */
  get limit(): number {
    return Math.min(this.tierLimit, this.httpCap)
  }
  get full(): boolean {
    return this.inflight >= this.limit
  }
  setHttpCap(cap: number): void {
    this.httpCap = cap
  }
  setTierLimit(n: number): void {
    this.tierLimit = n
  }

  /** per world: bytes per point and compression (sent to the workers) */
  configure(bpp: 12 | 16, compression: 'none' | 'gzip'): void {
    this.init = { ...this.init, bpp, compression }
    this.cfg.bpp = bpp
    this.cfg.compression = compression
    for (const w of this.workers) w?.postMessage(this.init)
  }

  private worker(k: number): WorkerPort | null {
    if (!this.spawn) return null
    let w = this.workers[k]
    if (!w) {
      w = this.spawn()
      w.onmessage = (e) => this.done(e.data)
      w.onerror = () => this.crashed(k)
      w.postMessage(this.init)
      this.workers[k] = w
    }
    return w
  }

  /** a worker died: rebuild it and re-send its requests (each counts as one attempt for the caller via a fail) */
  private crashed(k: number): void {
    this.crashes++
    this.workers[k]?.terminate()
    this.workers[k] = null
    const w = this.worker(k)
    for (const p of this.pending.values()) if (p.worker === k && w) w.postMessage(p.msg)
  }

  /** Range [start, endIncl] of url split into spans (node, relOffset, n); returns the request id */
  request(url: string, start: number, endIncl: number, spans: Int32Array, resolve: ((r: WorkerOut) => void) | null = null): number {
    const id = ++this.seq
    const msg: Extract<WorkerIn, { op: 'range' }> = { op: 'range', id, url, start, endIncl, spans }
    const k = this.rr++ % this.workers.length
    this.inflight++
    this.pending.set(id, { msg, worker: k, resolve })
    const w = this.worker(k)
    if (w) w.postMessage(msg)
    else {
      const ac = new AbortController()
      this.local.set(id, ac)
      void runRange(msg, this.cfg, ac.signal, this.f).then((r) => {
        this.local.delete(id)
        this.done(r)
      })
    }
    return id
  }

  /** promise form (first screen) */
  requestAsync(url: string, start: number, endIncl: number, spans: Int32Array): { id: number; done: Promise<WorkerOut> } {
    let res: (r: WorkerOut) => void = () => {}
    const done = new Promise<WorkerOut>((r) => (res = r))
    const id = this.request(url, start, endIncl, spans, res)
    return { id, done }
  }

  isPending(id: number): boolean {
    return this.pending.has(id)
  }

  cancel(id: number): void {
    const p = this.pending.get(id)
    if (!p) return
    const w = this.workers[p.worker]
    if (w) w.postMessage({ op: 'abort', id })
    else this.local.get(id)?.abort()
  }

  cancelAll(): void {
    for (const id of [...this.pending.keys()]) this.cancel(id)
  }

  /** auxiliary GET (hierarchy, DTM) counted against the shared in-flight limit */
  acquire(): void {
    this.aux++
    this.inflight++
  }
  /** wait for a free slot, then take it (manifest and hierarchy GETs while opening, DTM) */
  async acquireSlot(signal?: AbortSignal): Promise<void> {
    while (this.inflight >= this.limit) {
      if (signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' })
      await new Promise<void>((r) => this.waiters.push(r))
    }
    this.acquire()
  }
  release(): void {
    if (this.aux > 0) {
      this.aux--
      this.inflight--
    }
    this.wake()
  }
  private readonly waiters: (() => void)[] = []
  private wake(): void {
    const w = this.waiters.splice(0)
    for (const f of w) f()
  }

  private done(r: WorkerOut): void {
    const p = this.pending.get(r.id)
    if (!p) return
    this.pending.delete(r.id)
    this.inflight--
    this.wake()
    if (r.op === 'done') this.downloadedBytes += r.bytes
    if (p.resolve) p.resolve(r)
    else this.onReply?.(r)
  }

  dispose(): void {
    this.cancelAll()
    this.pending.clear()
    for (const w of this.workers) w?.terminate()
    this.workers.fill(null)
  }
}

/** worker counts and in-flight limits per tier (M05 §6.10) */
export function tierInflight(tier: 'A' | 'B' | 'S'): number {
  return tier === 'S' ? PC.inflightS : tier === 'B' ? PC.inflightB : PC.inflightA
}
