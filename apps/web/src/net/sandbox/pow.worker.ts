// Proof-of-work worker (M17-FR-060): solves in chunks and reports progress; the page terminates it when done or aborted.
// Owner: M17.
import { solveChunk } from './pow'

const CHUNK = 65536

self.onmessage = (ev: MessageEvent<{ prefix: Uint8Array; bits: number }>) => {
  const { prefix, bits } = ev.data
  let start = 0
  for (;;) {
    const n = solveChunk(prefix, bits, start, CHUNK)
    if (n !== null) {
      ;(self as unknown as Worker).postMessage({ nonce: n })
      return
    }
    start += CHUNK
    ;(self as unknown as Worker).postMessage({ tried: start })
  }
}
