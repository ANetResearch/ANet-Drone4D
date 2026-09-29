// ack and credit (M11-FR-091, M11-AC-014; AWR-17 §6.9 L1): the worker acks the largest frame_seq of returned slots every
// 3 frames or 50 ms with fps, decodeMs and lagMs; ping carries srttMs; without pulls the credit window stops the data
// plane and pulls resume it; clientStats at 1 Hz with the worker fields plus setClientStats() values.
import { describe, expect, it } from 'vitest'
import { frames, rig } from '../net/harness'

const acks = (r: Awaited<ReturnType<typeof rig>>): Record<string, unknown>[] => r.fake.received.filter((m) => m.op === 'ack')

describe('ack cadence', () => {
  it('60 Hz frames pulled every frame: an ack at most every 3 frames, with fps, decodeMs and lagMs', async () => {
    const r = await rig({ url: 'fake:?n=5', n: 5, window: 8 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 60, mode: 'latest' }] })
    await frames(r, 180)
    const a = acks(r)
    expect(a.length).toBeGreaterThan(30)
    let prev = 0
    for (const m of a) {
      const f = Number(m.frame)
      expect(f).toBeGreaterThan(prev)
      expect(f - prev).toBeLessThanOrEqual(4)
      prev = f
      expect(typeof m.fps).toBe('number')
      expect(typeof m.decodeMs).toBe('number')
    }
    expect(a.some((m) => typeof m.lagMs === 'number')).toBe(true)
    const last = a.at(-1)!
    expect(Number(last.fps)).toBeGreaterThan(50)
    expect(Number(last.fps)).toBeLessThan(70)
    expect(r.fake.stats.creditSkips).toBe(0)
  })

  it('10 Hz frames: every consumed frame is acked within the 50 ms rule', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    await frames(r, 120)
    const a = acks(r)
    const frameCount = r.fake.stats.frames
    expect(a.length).toBeGreaterThanOrEqual(frameCount - 3)
    expect(Number(a.at(-1)!.frame)).toBeGreaterThanOrEqual(frameCount - 2)
  })

  it('ping carries srttMs once a pong arrived', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    await frames(r, 90)
    const pings = r.fake.received.filter((m) => m.op === 'ping')
    expect(pings.length).toBeGreaterThanOrEqual(6)
    expect(pings[0].srttMs).toBeUndefined()
    expect(typeof pings.at(-1)!.srttMs).toBe('number')
  })
})

describe('credit window', () => {
  it('no pulls: the gateway stops after W frames; pulls resume without a burst', async () => {
    const r = await rig({ url: 'fake:?n=1', window: 3 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 60, mode: 'latest' }] })
    for (let k = 0; k < 60; k++) await r.step(1000 / 60) // main thread not pulling (hidden, busy)
    expect(r.fake.stats.frames).toBe(3)
    expect(r.fake.stats.creditSkips).toBeGreaterThan(40)
    // control plane keeps working: pings and pongs flow
    expect(r.fake.stats.pings).toBeGreaterThan(3)
    const before = r.fake.stats.frames
    await frames(r, 30)
    expect(r.fake.stats.frames - before).toBeGreaterThan(20)
    expect(r.fake.stats.frames - before).toBeLessThanOrEqual(31)
  })
})

describe('clientStats', () => {
  it('1 Hz with decodeMs, tier, deviceClass and the main-thread fields (fps from the client)', async () => {
    const r = await rig({ url: 'fake:?n=1' })
    r.host.onMessage({ cmd: 'stats', stats: { frameMs: 16.7, frameP95Ms: 25.123456, heapMB: 80, droppedFrames: 2.4, pointBudget: 25000 } })
    r.host.onMessage({ cmd: 'stats', stats: { fps: 59.5 } }) // merged with the earlier fields
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 10, mode: 'latest' }] })
    await frames(r, 200)
    const cs = r.fake.received.filter((m) => m.op === 'clientStats')
    expect(cs.length).toBeGreaterThanOrEqual(2)
    expect(cs.length).toBeLessThanOrEqual(4)
    const last = cs.at(-1)!
    expect(last).toMatchObject({ tier: 'S', deviceClass: 'software', frameMs: 16.7, frameP95Ms: 25.123, heapMB: 80, droppedFrames: 2, pointBudget: 25000 })
    expect(last.fps).toBe(59.5)
    expect(Object.keys(last).every((k) => ['op', 'fps', 'frameMs', 'frameP95Ms', 'decodeMs', 'heapMB', 'droppedFrames', 'pointBudget', 'tier', 'deviceClass',
      'latencyP95Ms', 'dGlobalMs'].includes(k))).toBe(true)
  })
})
