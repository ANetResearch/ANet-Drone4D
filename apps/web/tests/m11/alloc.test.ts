// Zero allocation on the steady path (M11-AC-036, NFR-016; AWR-03 §3.6 rule 1): 10,000 BATCH frames with N = 1000 Lite32
// rows plus a Full64 record go through the rt.worker engine and the main-thread FrameFront (receive, index, compose,
// copy into the slot, pull, load); after a forced GC the retained heap grows by at most 64 KiB. Node's --expose-gc is
// switched on at run time for this file.
import { setFlagsFromString } from 'node:v8'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'
import { FrameFront, SLOT_BYTES } from '@/net/rt/frame'
import { RtHost, type HostEnv } from '@/net/rt/session'
import { advertise, batchFrame, CH, full64, lite32, ScriptSocket, serverInfo, timeFrame } from './wire'

setFlagsFromString('--expose-gc')
const gc = runInNewContext('gc') as () => void

describe('allocation', () => {
  it('10,000 frames of N = 1000: retained heap growth <= 64 KiB', () => {
    let sock: ScriptSocket | null = null
    let held: ArrayBuffer | null = null
    let ready: ArrayBuffer | null = null
    let t = 1000
    const noop = (): void => {}
    const env: HostEnv = {
      post: (m) => {
        if (m.slot) ready = m.slot
      },
      socket: (url, protocols) => (sock = new ScriptSocket(url, protocols)),
      now: () => t,
      timeOrigin: 0,
      random: () => 0.5,
      setTimeout: () => noop, // timers are irrelevant here (no ping, stale check or ctrl timer runs)
      clearTimeout: noop,
    }
    const host = new RtHost(env)
    host.onMessage({ cmd: 'init', url: 'ws://h/api/rt', token: '', tier: 'S', deviceClass: 'software', timeOriginMain: 0,
      slots: [new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES), new ArrayBuffer(SLOT_BYTES)] })
    const s = sock!
    s.record = false // the acks the engine sends are not kept by the test socket
    s.open()
    s.serverSend(serverInfo())
    s.serverSend(advertise())
    s.serverSend(timeFrame({ epoch: 1, tSimNs: 0, tSrvNs: 0 }))
    const front = new FrameFront()
    // 64 distinct receive buffers reused cyclically (a real socket creates a new one per message; those are garbage)
    const bufs = Array.from({ length: 64 }, (_, k) => batchFrame({ epoch: 1, frameSeq: k + 1, tSimNs: (k + 1) * 1e7, records: [
      { ch: CH.swarm, seq: k + 1, payload: lite32(1000, (a) => ({ pos: [a, k, 1] })) }, { ch: CH.full0, seq: k + 1, payload: full64(0, [k, 0, 0]) }] }))
    const pullMsg = { cmd: 'pull' as const, returned: null as ArrayBuffer | null }
    const run = (n: number, from: number): void => {
      for (let k = 0; k < n; k++) {
        t += 16
        const b = bufs[(from + k) % bufs.length]
        new DataView(b).setUint32(4, from + k + 1, true) // frame_seq keeps increasing
        s.onmessage!({ data: b })
        pullMsg.returned = held
        held = null
        host.onMessage(pullMsg)
        if (ready) {
          front.load(ready)
          held = ready
          ready = null
        }
      }
    }
    run(2000, 0) // warm-up: JIT, caches, maps at steady size
    gc()
    gc()
    const before = process.memoryUsage().heapUsed
    run(10_000, 2000)
    gc()
    gc()
    const after = process.memoryUsage().heapUsed
    expect(front.hdr.swarmN).toBe(1000)
    expect(front.hdr.fullCount).toBe(1)
    expect(host.counters.malformed).toBe(0)
    expect(s.sentCount).toBeGreaterThan(3000) // acks flowed (every 3 frames)
    expect(after - before).toBeLessThanOrEqual(64 * 1024)
  })
})
