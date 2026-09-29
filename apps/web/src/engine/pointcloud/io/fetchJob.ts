// One Range fetch job of the fetch worker (M05 §6.5.1, M05-FR-015, FR-018; AWR-17 §5). Owner: M05.
// Shared by io/worker/fetch.worker.ts and the in-process fallback (Node tests). Only the `Range` header is sent (no
// content-type, no multi-range, M05 §9.5 item 6). The reply must be 206 with a Content-Range whose start and end match the
// request and a body of exactly that length, otherwise the job fails with kind `length` (405 PC_RANGE_MISMATCH, no
// retry). The body is split into the requested spans (node, relOffset, n) and each span is packed into pool texels
// (packQ16) in its own transferable buffer; gzip containers are inflated per node (DecompressionStream, D1 stub).
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { packQ16 } from './q16'

/** main -> worker */
export type WorkerIn =
  | { op: 'init'; bpp: 12 | 16; compression: 'none' | 'gzip'; injectFail: number; seed: number }
  | { op: 'range'; id: number; url: string; start: number; endIncl: number; spans: Int32Array }
  | { op: 'abort'; id: number }

/** worker -> main */
export type WorkerOut =
  | { op: 'done'; id: number; spans: Int32Array; buffers: ArrayBuffer[]; bytes: number; total: number; fetchMs: number; packMs: number; arrivedAt: number }
  | { op: 'fail'; id: number; status: number; kind: 'http' | 'network' | 'length' | 'abort'; message: string }

export interface JobConfig { bpp: 12 | 16; compression: 'none' | 'gzip'; injectFail: number; rand: () => number }

/** deterministic LCG for the test-build failure injection (DET-01: no Math.random in engine code) */
export function lcg(seed: number): () => number {
  let s = seed >>> 0 || 1
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    return s / 4294967296
  }
}

const CR = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i

async function inflate(buf: ArrayBuffer, off: number, len: number): Promise<ArrayBuffer> {
  const ds = new DecompressionStream('gzip')
  const stream = new Blob([new Uint8Array(buf, off, len)]).stream().pipeThrough(ds)
  return new Response(stream).arrayBuffer()
}

/** time origin aligned clock: ms since the epoch of performance.timeOrigin (converted back on the main thread) */
const absNow = (): number => performance.timeOrigin + performance.now()

export async function runRange(job: Extract<WorkerIn, { op: 'range' }>, cfg: JobConfig, signal: AbortSignal, f: typeof fetch = fetch): Promise<WorkerOut> {
  const t0 = performance.now()
  // test builds only (M05-FR-046): the injection is folded away in production bundles
  if (TEST_SWITCHES && cfg.injectFail > 0 && cfg.rand() < cfg.injectFail) return { op: 'fail', id: job.id, status: 503, kind: 'http', message: 'injected failure (pcInject)' }
  let r: Response
  try {
    r = await f(job.url, { headers: { Range: `bytes=${job.start}-${job.endIncl}` }, signal })
  } catch (e) {
    const aborted = (e as { name?: string })?.name === 'AbortError'
    return { op: 'fail', id: job.id, status: 0, kind: aborted ? 'abort' : 'network', message: String((e as Error)?.message ?? e) }
  }
  if (r.status !== 206) {
    // 200 means the server ignored the Range: a mismatch, not a transient error
    const kind = r.status === 200 ? 'length' : 'http'
    return { op: 'fail', id: job.id, status: r.status, kind, message: `HTTP ${r.status}` }
  }
  const m = CR.exec(r.headers.get('content-range') ?? '')
  const need = job.endIncl - job.start + 1
  let body: ArrayBuffer
  try {
    body = await r.arrayBuffer()
  } catch (e) {
    const aborted = (e as { name?: string })?.name === 'AbortError'
    return { op: 'fail', id: job.id, status: 0, kind: aborted ? 'abort' : 'network', message: String((e as Error)?.message ?? e) }
  }
  const arrivedAt = absNow()
  if (!m || Number(m[1]) !== job.start || Number(m[2]) !== job.endIncl || body.byteLength !== need) {
    return { op: 'fail', id: job.id, status: 206, kind: 'length', message: `Content-Range ${r.headers.get('content-range')} / ${body.byteLength} B for ${job.start}-${job.endIncl}` }
  }
  const total = m[3] === '*' ? -1 : Number(m[3])
  const fetchMs = performance.now() - t0
  const t1 = performance.now()
  const spans = job.spans
  const buffers: ArrayBuffer[] = []
  for (let s = 0; s < spans.length; s += 3) {
    const rel = spans[s + 1]
    const n = spans[s + 2]
    const dst = new Uint32Array(4 * n)
    if (cfg.compression === 'gzip') {
      const len = (s + 4 < spans.length ? spans[s + 4] : need) - rel
      const raw = await inflate(body, rel, len)
      if (raw.byteLength !== n * cfg.bpp) return { op: 'fail', id: job.id, status: 206, kind: 'length', message: `gzip node ${spans[s]}: ${raw.byteLength} B` }
      packQ16(raw, 0, n, cfg.bpp, dst, 0)
    } else {
      if (rel + n * cfg.bpp > body.byteLength) return { op: 'fail', id: job.id, status: 206, kind: 'length', message: `span ${spans[s]} outside the body` }
      packQ16(body, rel, n, cfg.bpp, dst, 0)
    }
    buffers.push(dst.buffer)
  }
  return { op: 'done', id: job.id, spans, buffers, bytes: need, total, fetchMs, packMs: performance.now() - t1, arrivedAt }
}
