// Point cloud fetch worker (M05 §6.5.1, M05-FR-015): Range fetch, validation, split and pack off the main thread; the
// packed node buffers are transferred back. Protocol: init, range, abort in; done, fail out. Owner: M05.
import { lcg, runRange, type JobConfig, type WorkerIn, type WorkerOut } from '../fetchJob'

interface Scope {
  postMessage(m: WorkerOut, transfer: Transferable[]): void
  onmessage: ((e: MessageEvent<WorkerIn>) => void) | null
}
const scope = self as unknown as Scope
const aborts = new Map<number, AbortController>()
const cfg: JobConfig = { bpp: 12, compression: 'none', injectFail: 0, rand: lcg(1) }

scope.onmessage = (e) => {
  const m = e.data
  if (m.op === 'init') {
    cfg.bpp = m.bpp
    cfg.compression = m.compression
    cfg.injectFail = m.injectFail
    cfg.rand = lcg(m.seed)
    return
  }
  if (m.op === 'abort') {
    aborts.get(m.id)?.abort()
    return
  }
  const ac = new AbortController()
  aborts.set(m.id, ac)
  void runRange(m, cfg, ac.signal).then((r) => {
    aborts.delete(m.id)
    scope.postMessage(r, r.op === 'done' ? [...r.buffers, r.spans.buffer] : [])
  })
}
