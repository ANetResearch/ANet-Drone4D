// rt.worker (M11-FR-089..096; AWR-17 §6.13; AWR-10 AD-06). Owner: M11. The only place that holds the WebSocket; all
// protocol work lives in session.ts (RtHost), this file binds it to the dedicated worker global scope. `fake:` URLs
// (the `?source=fake` switch, D1-AC-35) open FakeSource connections instead of a WebSocket; connections of one URL share
// a FakeWorld so a reconnect meets the same fake session. A replay fixture, when given, is fetched once before the first
// connection. After close code 1006 the host asks GET /api/auth/whoami to tell 401 from 403 (AWR-17 §3.3 item 4).
import { isFakeUrl, openFake, parseFakeUrl, type FakeWorld } from './FakeSource'
import { RtHost, type WorkerIn, type WorkerOut } from './session'
import { whoamiStatus, type SocketLike } from './transport'

interface WorkerScope {
  postMessage(m: WorkerOut, transfer: ArrayBuffer[]): void
  onmessage: ((e: MessageEvent<WorkerIn>) => void) | null
}
const scope = self as unknown as WorkerScope

let fixture: ArrayBuffer | null = null
const fakeWorlds = new Map<string, FakeWorld>()
const host = new RtHost({
  post: (m, t) => scope.postMessage(m, t),
  socket: (url, protocols): SocketLike =>
    isFakeUrl(url) ? openFake(url, fakeWorlds, fixture) : (new WebSocket(url, protocols) as unknown as SocketLike),
  now: () => performance.now(),
  timeOrigin: performance.timeOrigin,
  random: () => Math.random(), // reconnect jitter only (net/** is outside DET-01)
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  checkAuth: (url, token) => whoamiStatus(url, token),
})

const queue: WorkerIn[] = []
let ready = true

scope.onmessage = (e) => {
  const m = e.data
  if (m.cmd === 'init' && isFakeUrl(m.url)) {
    const src = parseFakeUrl(m.url).fixtureUrl
    if (src && !fixture) {
      ready = false
      void fetch(src)
        .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(`${r.status} ${src}`))))
        .then((b) => {
          fixture = b
        })
        .catch(() => {
          fixture = null
        })
        .finally(() => {
          ready = true
          host.onMessage(m)
          for (const q of queue.splice(0)) host.onMessage(q)
        })
      return
    }
  }
  if (!ready) {
    queue.push(m)
    return
  }
  host.onMessage(m)
}
