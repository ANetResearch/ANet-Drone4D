// ClockSync in rt.worker (M11-FR-092, M11-AC-024, M11-AC-036, NFR-022; AWR-17 §6.10): 5 pings 100 ms apart then 2 Hz,
// minimum-RTT filtering, jump/smooth rule, srttMs reported in ping, reset on a new sessionId, and the main-thread time
// base: the slot's clockOffsetMainMs against an independently simulated server clock stays within 1 ms although worker
// and window have different performance.timeOrigin; with jittered links the p95 error stays within 2 ms.
import { describe, expect, it } from 'vitest'
import { CLOCK, ClockSync } from '@/net/rt/clockSync'
import { rng } from '../net/harness'
import { advertise, ctrlOf, lastFrame, scriptRig, serverInfo, timeFrame, type ScriptRig } from './wire'

const ORIGIN_WORKER = 1_000_000.25 // performance.timeOrigin of the worker (absolute ms)
const ORIGIN_MAIN = 999_400.5 // of the window
const SERVER_BASE = 993_210.125 // absolute ms at which the gateway monotonic clock reads 0

/** answer every new ping of the current socket with a pong whose server time is taken d1 after the send */
function pongLoop(r: ScriptRig, steps: number, delay: () => [number, number]): number[] {
  const errors: number[] = []
  let answered = 0
  const end = r.clock.t + steps * 10
  while (r.clock.t < end) {
    if (!r.nextTimer(end - r.clock.t)) r.clock.t = end
    const pings = r.sock.ops('ping')
    while (answered < pings.length) {
      const p = pings[answered++]
      const t0 = Number(p.t)
      const [d1, d2] = delay()
      const serverMs = ORIGIN_WORKER + t0 + d1 - SERVER_BASE
      r.clock.t = Math.max(r.clock.t, t0 + d1 + d2)
      r.sock.serverSend({ op: 'pong', t: t0, server_ns: Math.round(serverMs * 1e6), sim_ns: 0, epoch: 1 })
      r.sock.serverSend(timeFrame({ epoch: 1, tSimNs: 1e9, tSrvNs: 1e9 })) // makes a slot with the header
      r.pull()
      const off = lastFrame(r).hdr.clockOffsetMainMs
      // independent truth: server = mainNow + offMain, mainNow = abs - ORIGIN_MAIN, server = abs - SERVER_BASE
      if (Number.isFinite(off)) errors.push(Math.abs(off - (ORIGIN_MAIN - SERVER_BASE)))
    }
  }
  return errors
}

const handshake = (r: ScriptRig, sessionId = 'gw-1'): void => {
  r.sock.open()
  r.sock.serverSend(serverInfo({ sessionId }))
  r.sock.serverSend(advertise())
  r.sock.serverSend(timeFrame({ epoch: 1, tSimNs: 0, tSrvNs: 0 }))
}

describe('ClockSync filter', () => {
  it('keeps the minimum-RTT sample, jumps beyond 50 ms, smooths 0.1 below; srtt EWMA 0.875', () => {
    const c = new ClockSync()
    c.onPong(0, 10, 105)
    expect(c.offsetMs).toBe(100)
    expect(c.srttMs).toBe(10)
    c.onPong(20, 60, 200) // rtt 40: the min-RTT sample (offset 100) still wins
    expect(c.offsetMs).toBe(100)
    expect(c.srttMs).toBeCloseTo(0.875 * 10 + 0.125 * 40, 9)
    c.onPong(100, 102, 221) // rtt 2, offset 120: within 50 ms -> 0.1 smoothing
    expect(c.offsetMs).toBeCloseTo(102, 6)
    c.onPong(200, 201, 500.5) // rtt 1, offset 300: jump
    expect(c.offsetMs).toBe(300)
    expect(c.offsetMain(50, 10)).toBe(340)
    // only the last 16 samples count
    for (let i = 0; i < CLOCK.samples; i++) c.onPong(1000 + 100 * i, 1000 + 100 * i + 30, 1000 + 100 * i + 15 + 10)
    expect(c.offsetMs).toBeCloseTo(10, 6)
    c.reset()
    expect(Number.isNaN(c.offsetMs)).toBe(true)
  })
})

describe('ClockSync in the worker', () => {
  it('main-thread offset within 1 ms of the independent server clock (different timeOrigins)', () => {
    const r = scriptRig({ timeOrigin: ORIGIN_WORKER, timeOriginMain: ORIGIN_MAIN })
    handshake(r)
    const errors = pongLoop(r, 150, () => [1.5, 1.0])
    expect(errors.length).toBeGreaterThan(5)
    expect(Math.max(...errors)).toBeLessThanOrEqual(1)
    // the slot also reports srtt
    expect(lastFrame(r).hdr.srttMs).toBeCloseTo(2.5, 6)
  })

  it('p95 error <= 2 ms with jittered, asymmetric links (loopback profile)', () => {
    const rand = rng(42)
    const r = scriptRig({ timeOrigin: ORIGIN_WORKER, timeOriginMain: ORIGIN_MAIN })
    handshake(r)
    const errors = pongLoop(r, 3000, () => [0.2 + 8 * rand() * rand(), 0.2 + 8 * rand() * rand()])
    errors.sort((a, b) => a - b)
    const p95 = errors[Math.floor(0.95 * (errors.length - 1))]
    expect(errors.length).toBeGreaterThan(40)
    expect(p95).toBeLessThanOrEqual(2)
  })

  it('5 pings 100 ms apart, then 2 Hz; srttMs in pings after the first pong', () => {
    const r = scriptRig({ timeOrigin: ORIGIN_WORKER, timeOriginMain: ORIGIN_MAIN })
    handshake(r)
    const t0 = r.clock.t
    pongLoop(r, 250, () => [1, 1]) // 2.5 s
    const pings = r.sock.ops('ping')
    const times = pings.map((p) => Number(p.t) - t0)
    expect(times.slice(0, 5)).toEqual([0, 100, 200, 300, 400].map((x) => expect.closeTo(x, 0) as unknown as number))
    expect(times[5] - times[4]).toBeGreaterThanOrEqual(499)
    expect(pings.length).toBeGreaterThanOrEqual(8)
    expect(pings.length).toBeLessThanOrEqual(10)
    expect(pings[0].srttMs).toBeUndefined()
    expect(pings.at(-1)!.srttMs).toBeCloseTo(2, 3)
  })

  it('a new sessionId clears the samples; the same session keeps them and restarts the burst', () => {
    const r = scriptRig({ timeOrigin: ORIGIN_WORKER, timeOriginMain: ORIGIN_MAIN })
    handshake(r, 'gw-1')
    pongLoop(r, 60, () => [1, 1])
    expect(Number.isFinite(r.host.clock.offsetMs)).toBe(true)
    r.sock.serverClose(1011)
    r.advance(600)
    handshake(r, 'gw-1')
    expect(Number.isFinite(r.host.clock.offsetMs)).toBe(true)
    expect(r.host.clock.sent).toBeLessThanOrEqual(1)
    r.sock.serverClose(1011)
    r.advance(600)
    handshake(r, 'gw-2')
    expect(Number.isNaN(r.host.clock.offsetMs)).toBe(true)
    expect(ctrlOf(r, 'connected').map((m) => m.sameSession)).toEqual([false, true, false])
  })
})
