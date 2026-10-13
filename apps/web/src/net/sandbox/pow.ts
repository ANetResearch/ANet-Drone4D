// Sandbox proof of work, `alg = sha256-lz` (M17-FR-017, FR-060; AWR-17 §17.3.5). Owner: M17.
// Find nonce (0 <= nonce < 2^53, decimal ASCII) so that SHA-256(prefix || nonce_ascii) has at least `bits` leading zero
// bits. solveChunk() is the pure kernel (Node tests, the worker); solvePow() runs it in a Web Worker (pow.worker.ts) so the
// main thread never blocks (no long task > 50 ms, D1-AC-03b), and falls back to small main-thread slices when Workers
// are unavailable (tests, old browsers).
import { sha256 } from './sha256'

export interface Challenge { challenge_id: string; alg: 'sha256-lz'; prefix_b64: string; bits: number; expires_unix_ns: string }
export interface PowSolution { challenge_id: string; nonce: number }

export function b64decode(s: string): Uint8Array {
  const bin = atob(s)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

export function leadingZeroBits(h: Uint8Array): number {
  let n = 0
  for (const b of h) {
    if (b === 0) {
      n += 8
      continue
    }
    return n + Math.clz32(b) - 24
  }
  return n
}

/** Try nonces [start, start + count); returns the first solution or null. */
export function solveChunk(prefix: Uint8Array, bits: number, start: number, count: number): number | null {
  const buf = new Uint8Array(prefix.length + 20)
  buf.set(prefix)
  const out = new Uint8Array(32)
  const p = prefix.length
  for (let n = start; n < start + count; n++) {
    const s = String(n)
    for (let i = 0; i < s.length; i++) buf[p + i] = s.charCodeAt(i)
    sha256(buf, p + s.length, out)
    if (leadingZeroBits(out) >= bits) return n
  }
  return null
}

export interface SolveOptions { signal?: AbortSignal; worker?: boolean; onProgress?: (tried: number) => void }

const CHUNK_MAIN = 4096

/** Solve a challenge; resolves with {challenge_id, nonce}. Rejects with AbortError when the signal aborts. */
export async function solvePow(ch: Challenge, opts: SolveOptions = {}): Promise<PowSolution> {
  const prefix = b64decode(ch.prefix_b64)
  if (opts.worker !== false && typeof Worker !== 'undefined') {
    try {
      return await solveInWorker(ch, prefix, opts)
    } catch (e) {
      if ((e as Error).name === 'AbortError') throw e
      // worker construction failed (CSP, bundler without worker support): main-thread slices below
    }
  }
  let start = 0
  for (;;) {
    if (opts.signal?.aborted) throw new DOMException('aborted', 'AbortError')
    const n = solveChunk(prefix, ch.bits, start, CHUNK_MAIN)
    if (n !== null) return { challenge_id: ch.challenge_id, nonce: n }
    start += CHUNK_MAIN
    opts.onProgress?.(start)
    await new Promise((r) => setTimeout(r, 0))
  }
}

function solveInWorker(ch: Challenge, prefix: Uint8Array, opts: SolveOptions): Promise<PowSolution> {
  return new Promise((resolve, reject) => {
    const w = new Worker(new URL('./pow.worker.ts', import.meta.url), { type: 'module' })
    const done = () => {
      w.terminate()
      opts.signal?.removeEventListener('abort', onAbort)
    }
    const onAbort = () => {
      done()
      reject(new DOMException('aborted', 'AbortError'))
    }
    opts.signal?.addEventListener('abort', onAbort)
    w.onmessage = (ev: MessageEvent<{ nonce?: number; tried?: number }>) => {
      if (typeof ev.data.nonce === 'number') {
        done()
        resolve({ challenge_id: ch.challenge_id, nonce: ev.data.nonce })
      } else if (typeof ev.data.tried === 'number') opts.onProgress?.(ev.data.tried)
    }
    w.onerror = (ev) => {
      done()
      reject(new Error(ev.message || 'pow worker failed'))
    }
    w.postMessage({ prefix, bits: ch.bits })
  })
}
