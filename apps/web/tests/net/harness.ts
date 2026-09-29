// Test harness for net/rt (M11): an RtHost wired to FakeSource connections with a manual clock, collecting the slots and
// control messages it posts, and a pull() helper that plays the main thread's side of the 3-slot exchange (AD-06).
// Connections of one rig share a FakeWorld (like the worker's openFake cache), so reconnects meet the same session.
import { FakeSource, FakeWorld, type FakeSourceOptions } from '@/net/rt/FakeSource'
import { SLOT_BYTES, SlotReader } from '@/net/rt/frame'
import { RtHost, type CtrlMsg, type HostEnv, type WorkerOut } from '@/net/rt/session'
import type { SocketLike } from '@/net/rt/transport'

export const microtasks = async (n = 6): Promise<void> => {
  for (let i = 0; i < n; i++) await Promise.resolve()
}

export interface Rig {
  host: RtHost
  /** the current connection */
  fake: FakeSource
  /** the shared fake gateway (null for replay rigs) */
  world: FakeWorld | null
  clock: { t: number }
  ctrl: CtrlMsg[]
  slots: SlotReader[]
  /** sockets created so far */
  sockets: number
  /** main-thread pull: returns the previously received slot and asks for the next one */
  pull(): void
  /** advance the manual clock by ms and run one fake tick, then deliver messages */
  step(ms: number): Promise<void>
  timers: { fn: () => void; at: number }[]
  runTimers(): void
}

export interface RigOptions extends FakeSourceOptions {
  url?: string
  /** whoami stub for the 1006 triage (HTTP status) */
  checkAuth?: (url: string, token: string) => Promise<number>
  /** custom socket factory (overrides FakeSource) */
  socket?: (url: string, protocols: string[]) => SocketLike
  /** do not pull automatically after init */
  initialToken?: string
}

export async function rig(o: RigOptions = {}): Promise<Rig> {
  const clock = { t: 1000 }
  const ctrl: CtrlMsg[] = []
  const slots: SlotReader[] = []
  const timers: { fn: () => void; at: number }[] = []
  let fake: FakeSource | null = null
  let world: FakeWorld | null = null
  let held: ArrayBuffer | null = null
  let sockets = 0
  const { url, checkAuth, socket, initialToken, ...fo } = o
  const env: HostEnv = {
    post: (m: WorkerOut) => {
      if (m.slot) {
        // the buffer goes back to the host on the next pull and is overwritten: keep a copy for assertions
        slots.push(new SlotReader(m.slot.slice(0)))
        held = m.slot
      }
      if (m.ctrl) ctrl.push(...m.ctrl)
    },
    socket: (u, p) => {
      sockets++
      if (socket) return socket(u, p)
      if (!fo.fixture && !world) world = new FakeWorld({ autoTick: false, now: () => clock.t, ...fo })
      fake = fo.fixture ? new FakeSource({ autoTick: false, now: () => clock.t, ...fo }) : new FakeSource({ ...fo, shared: world! })
      return fake
    },
    now: () => clock.t,
    timeOrigin: 0,
    random: () => 0.5,
    setTimeout: (fn, ms) => {
      const h = { fn, at: clock.t + ms }
      timers.push(h)
      return h
    },
    clearTimeout: (h) => {
      const i = timers.indexOf(h as { fn: () => void; at: number })
      if (i >= 0) timers.splice(i, 1)
    },
    checkAuth,
  }
  const host = new RtHost(env)
  host.onMessage({ cmd: 'init', url: url ?? 'fake:?n=1', token: initialToken ?? '', tier: 'S', deviceClass: 'software', timeOriginMain: 0,
    slots: [new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES)] })
  await microtasks()
  const runTimers = (): void => {
    for (;;) {
      const due = timers.filter((x) => x.at <= clock.t)
      if (!due.length) return
      for (const d of due) {
        const i = timers.indexOf(d)
        if (i < 0) continue
        timers.splice(i, 1)
        d.fn()
      }
    }
  }
  runTimers()
  await microtasks()
  const r: Rig = {
    host, clock, ctrl, slots, timers, runTimers,
    get fake() {
      return fake!
    },
    get world() {
      return world
    },
    get sockets() {
      return sockets
    },
    pull() {
      const ret = held
      held = null
      host.onMessage({ cmd: 'pull', returned: ret })
    },
    async step(ms: number) {
      clock.t += ms
      runTimers()
      fake?.tick()
      await microtasks()
      runTimers()
    },
  }
  return r
}

export const ctrlOps = (c: CtrlMsg[], op: string): CtrlMsg[] => c.filter((m) => m.op === op)

/** run n frames of 1/60 s with a pull after each */
export async function frames(r: Rig, n: number, ms = 1000 / 60): Promise<void> {
  for (let k = 0; k < n; k++) {
    await r.step(ms)
    r.pull()
  }
}

/** deterministic xorshift32 in [0, 1) (DET-01: no Math.random in tests) */
export function rng(seed: number): () => number {
  let x = seed >>> 0 || 1
  return () => {
    x ^= x << 13
    x >>>= 0
    x ^= x >>> 17
    x ^= x << 5
    x >>>= 0
    return x / 4294967296
  }
}
