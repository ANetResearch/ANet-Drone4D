// M05-AC-017 (unit part), M05-AC-018: cancellation and retry policy, the worker job validation (206, Content-Range,
// length), failure injection, the Fetcher's shared in-flight limit and HTTP cap, and the node state machine of the
// download candidates (RETRY_WAIT, FAILED requeue after 10 s).
import { describe, expect, it } from 'vitest'
import { ABORT_OUTSIDE_FRAMES, ABORT_STALE_FRAMES, MAX_LOAD_ATTEMPTS, failCounts, failIsFinal, isFailedAfter, nextRetryAt, retryDelayMs, shouldAbort }
  from '@/engine/pointcloud/core/StreamPolicy'
import { lcg, runRange, type JobConfig } from '@/engine/pointcloud/io/fetchJob'
import { Fetcher } from '@/engine/pointcloud/io/fetcher'
import { httpCapFor, PC } from '@/engine/pointcloud/params'
import { NodeStore, NS, type HierRecord } from '@/engine/pointcloud/core/NodeStore'
import { requestable } from '@/engine/pointcloud/core/DownloadQueue'
import { packQ16 } from '@/engine/pointcloud/io/q16'

describe('StreamPolicy (voxelkloud stream-policy constants, wall-clock back-off)', () => {
  it('keeps the voxelkloud constants', () => {
    expect([ABORT_OUTSIDE_FRAMES, ABORT_STALE_FRAMES, MAX_LOAD_ATTEMPTS]).toEqual([2, 8, 3])
  })
  it('aborts a fetch out of the selection and outside the frustum for 2 frames, never a selected one', () => {
    const p = { abortOutside: true, abortSuperseded: false, saturated: true }
    expect(shouldAbort(0, true, p)).toBe(false)
    expect(shouldAbort(1, true, p)).toBe(false)
    expect(shouldAbort(2, true, p)).toBe(true)
    expect(shouldAbort(50, false, p)).toBe(false) // superseded tier off by default
    expect(shouldAbort(8, false, { ...p, abortSuperseded: true })).toBe(true)
    expect(shouldAbort(8, false, { ...p, abortSuperseded: true, saturated: false })).toBe(false)
  })
  it('backs off 0.5 / 2 / 8 s and marks FAILED at the third failure, requeued after 10 s', () => {
    expect([1, 2, 3].map(retryDelayMs)).toEqual([500, 2000, 8000])
    expect(nextRetryAt(1, 1000)).toBe(1500)
    expect(nextRetryAt(2, 1000)).toBe(3000)
    expect(isFailedAfter(3)).toBe(true)
    expect(nextRetryAt(3, 1000)).toBe(11_000)
    expect(failCounts('abort')).toBe(false)
    expect(failIsFinal('length')).toBe(true)
    expect(failIsFinal('http')).toBe(false)
  })
  it('candidate states: RETRY_WAIT until retryAt, FAILED back to UNLOADED with attempts cleared', () => {
    const recs: HierRecord[] = [{ type: 1, childMask: 0, numPoints: 10, byteOffset: 0, byteSize: 120, parent: -1, child: 0, level: 0, name: 'r' }]
    const t = new NodeStore([{ cubeMin: [0, 0, 0], cubeSize: 1, spacing: 1, records: recs, ext: null, octreeUrl: '' }])
    t.state[0] = NS.RETRY_WAIT
    t.retryAt[0] = 2000
    expect(requestable(t, 0, 1999, false)).toBe(false)
    expect(requestable(t, 0, 2000, false)).toBe(true)
    t.state[0] = NS.FAILED
    t.attempts[0] = 3
    t.retryAt[0] = 12_000
    expect(requestable(t, 0, 5000, false)).toBe(false)
    expect(requestable(t, 0, 12_000, false)).toBe(true)
    expect(t.attempts[0]).toBe(0)
    expect(t.state[0]).toBe(NS.UNLOADED)
    expect(requestable(t, 0, 12_000, true)).toBe(false) // in flight
  })
})

function node(n: number): Uint8Array<ArrayBuffer> {
  const b = new Uint8Array(12 * n)
  for (let i = 0; i < b.length; i++) b[i] = (i * 31 + 7) & 255
  return b
}

describe('worker job validation (M05 §6.5.1, FR-018)', () => {
  const cfg: JobConfig = { bpp: 12, compression: 'none', injectFail: 0, rand: lcg(1) }
  const body = node(100)
  const serve = (status: number, cr: string | null, bytes: Uint8Array = body) => (async () =>
    new Response(bytes.slice(), { status, headers: cr ? { 'Content-Range': cr } : {} })) as unknown as typeof fetch
  const job = { op: 'range' as const, id: 1, url: 'http://x/octree.bin?v=1', start: 1200, endIncl: 1200 + 1199, spans: Int32Array.from([7, 0, 100]) }
  it('206 with a matching Content-Range: packed texels of the span', async () => {
    const r = await runRange(job, cfg, new AbortController().signal, serve(206, 'bytes 1200-2399/99999'))
    expect(r.op).toBe('done')
    if (r.op !== 'done') return
    const ref = new Uint32Array(400)
    packQ16(body.buffer as ArrayBuffer, 0, 100, 12, ref, 0)
    expect(Array.from(new Uint32Array(r.buffers[0]))).toEqual(Array.from(ref))
    expect(r.total).toBe(99_999)
    expect(r.bytes).toBe(1200)
  })
  it('200 (Range ignored), a wrong Content-Range or a short body fail with kind length (no retry)', async () => {
    for (const [st, cr, b] of [[200, null, body], [206, 'bytes 0-1199/99999', body], [206, 'bytes 1200-2399/99999', body.subarray(0, 600)]] as const) {
      const r = await runRange(job, cfg, new AbortController().signal, serve(st, cr, b as Uint8Array))
      expect(r.op).toBe('fail')
      if (r.op === 'fail') expect(r.kind).toBe('length')
    }
  })
  it('HTTP errors retry (http), 409 is reported, aborts do not count', async () => {
    const r503 = await runRange(job, cfg, new AbortController().signal, serve(503, null))
    expect(r503).toMatchObject({ op: 'fail', kind: 'http', status: 503 })
    const r409 = await runRange(job, cfg, new AbortController().signal, serve(409, null))
    expect(r409).toMatchObject({ op: 'fail', status: 409 })
    const ac = new AbortController()
    ac.abort()
    const f = (async (_u: string, init: RequestInit) => {
      if (init.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' })
      return new Response('')
    }) as unknown as typeof fetch
    expect(await runRange(job, cfg, ac.signal, f)).toMatchObject({ op: 'fail', kind: 'abort' })
  })
  it('failure injection is deterministic for a seed (pcInject=fail:<p>)', async () => {
    const run = async (seed: number) => {
      const c: JobConfig = { ...cfg, injectFail: 0.3, rand: lcg(seed) }
      const out: string[] = []
      for (let i = 0; i < 40; i++) out.push((await runRange(job, c, new AbortController().signal, serve(206, 'bytes 1200-2399/99999'))).op)
      return out
    }
    const a = await run(5)
    expect(a).toEqual(await run(5))
    const fails = a.filter((x) => x === 'fail').length
    expect(fails).toBeGreaterThan(3)
    expect(fails).toBeLessThan(25)
  })
})

describe('Fetcher in-flight limit and HTTP cap (M05-FR-016, AC-018)', () => {
  it('HTTP/1.1 (or unknown) caps at 4, h2 and h3 are unlimited', () => {
    expect(httpCapFor('http/1.1')).toBe(4)
    expect(httpCapFor('')).toBe(4)
    expect(httpCapFor(null)).toBe(4)
    expect(httpCapFor('http/1.1', 5)).toBe(5)
    expect(httpCapFor('http/1.1', 9)).toBe(PC.httpCapMax)
    expect(httpCapFor('h2')).toBe(Infinity)
    const f = new Fetcher({ workers: 2, limit: PC.inflightB, useWorker: false })
    f.setHttpCap(httpCapFor('http/1.1'))
    expect(f.limit).toBe(4)
    f.setHttpCap(httpCapFor('h2'))
    expect(f.limit).toBe(8)
    f.dispose()
  })
  it('counts node Ranges and auxiliary GETs against one limit; cancel yields an abort reply', async () => {
    let open = 0
    let max = 0
    const slow = (async (_u: string, init: RequestInit) => {
      open++
      max = Math.max(max, open)
      try {
        await new Promise<void>((res, rej) => {
          const t = setTimeout(res, 20)
          init.signal?.addEventListener('abort', () => {
            clearTimeout(t)
            rej(Object.assign(new Error('aborted'), { name: 'AbortError' }))
          })
        })
        return new Response(node(1).slice().buffer, { status: 206, headers: { 'Content-Range': 'bytes 0-11/12' } })
      } finally {
        open--
      }
    }) as unknown as typeof fetch
    const f = new Fetcher({ workers: 1, limit: 4, useWorker: false, f: slow })
    const replies: string[] = []
    f.onReply = (r) => replies.push(r.op === 'done' ? 'done' : r.kind)
    f.acquire()
    expect(f.inflight).toBe(1)
    const ids = [0, 1, 2].map(() => f.request('http://x/o.bin?v=1', 0, 11, Int32Array.from([0, 0, 1])))
    expect(f.full).toBe(true)
    f.release()
    expect(f.full).toBe(false)
    f.cancel(ids[1])
    await new Promise((r) => setTimeout(r, 60))
    expect(replies.sort()).toEqual(['abort', 'done', 'done'])
    expect(f.inflight).toBe(0)
    expect(max).toBeLessThanOrEqual(3)
    expect(f.downloadedBytes).toBe(24)
    f.dispose()
  })
})
