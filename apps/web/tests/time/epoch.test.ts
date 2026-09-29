// M12-AC-006 epoch and RESET clearing: the time_epoch.awrrt fixture (M00) replayed through rt.worker slots into the
// time runtime: every epoch (seek to 2, scenario reset 3, checkpoint restore via RESTARTING 4, replay bit 7 at 5,
// u16 wrap 65535 -> 0) fires onEpoch once and empties the ring before the new epoch's samples; the stale epoch-1 BATCH
// after the seek never reaches the ring. A RESET channel (checkpoint restore of one producer within an epoch) clears
// only that producer's agent range.
import { describe, expect, it } from 'vitest'
import { initTime, newDronePoseSoA, onEpoch, onReset, TIME_STATE as TS } from '@/engine/time/index'
import type { RosterEntry, RosterView } from '@/net/rt/types'
import { readBytes } from '../contracts/util'
import { rig } from '../net/harness'
import { makeFrame, stubRegister } from './helpers'

describe('epochs through the time_epoch fixture (M12-AC-006)', () => {
  it('clears the ring once per epoch, before the new epoch samples, and follows bit 7', async () => {
    const r = await rig({ fixture: readBytes('fixtures/rt/time_epoch.awrrt') })
    r.pull()
    for (let guard = 0; !r.fake.replayDone && guard < 1000; guard++) {
      await r.step(5)
      r.pull()
    }
    await r.step(5)
    r.pull()
    const reg = stubRegister()
    const rt = initTime({ register: reg.register, roster: () => null, reducedMotion: () => false })
    const epochs: number[] = []
    const off = onEpoch((e) => epochs.push(e))
    const out = newDronePoseSoA(8)
    let now = 0
    let sawReplay = false
    const stateSeq: number[] = []
    for (const s of r.slots) {
      now += 16
      rt.ingest(s, now)
      reg.run(now)
      stateSeq.push(rt.clock.state4)
      if (rt.clock.replay) sawReplay = true
      // after every slot: no sample older than the current epoch's first TIME t_sim may remain after a clear
      rt.interp.sampleSwarm(rt.clock.tSimMs / 1000, out)
      for (let i = 0; i < out.n; i++) expect(out.sampleT[i]).toBeGreaterThanOrEqual(0)
      if (rt.clock.epoch === 2) {
        for (let i = 0; i < out.n; i++) expect(out.sampleT[i]).toBeGreaterThanOrEqual(60_000)
      }
    }
    off()
    rt.dispose()
    expect(epochs).toEqual([1, 2, 3, 4, 5, 65535, 0])
    expect(sawReplay).toBe(true)
    expect(new Set(stateSeq).has(TS.LIVE)).toBe(true)
  })
})

describe('RESET of one producer (M12-AC-006, checkpoint restore)', () => {
  it('clears only the agent range of the RESET channel producer and notifies onReset', () => {
    const entries: RosterEntry[] = [
      { agentNo: 0, id: 'a', model: 'p600', kind: 'uav', producer: 'sim-core', simulated: true, lifecycle: 'READY' },
      { agentNo: 1, id: 'b', model: 'p600', kind: 'uav', producer: 'sim-core', simulated: true, lifecycle: 'READY' },
      { agentNo: 512, id: 'px', model: 'x500', kind: 'uav', producer: 'px4-bridge', simulated: false, lifecycle: 'READY' },
    ]
    const roster: RosterView = {
      size: entries.length, version: 1, get: (a) => entries.find((e) => e.agentNo === a), idOf: (a) => entries.find((e) => e.agentNo === a)?.id,
      agentNoOf: (id) => entries.find((e) => e.id === id)?.agentNo ?? -1, entries: () => entries,
    }
    const reg = stubRegister()
    const rt = initTime({ register: reg.register, roster: () => roster, reducedMotion: () => false })
    const resets: { idBase: number; idCount: number }[] = []
    const off = onReset((x) => resets.push({ ...x }))
    const f = makeFrame()
    f.time(TS.PLAYING, 1, 1, 0, 0).swarm1(0, [{ a: 0, p: [0, 0, 0] }, { a: 1, p: [1, 0, 0] }]).full1(512, 40, 0, [5, 0, 0])
    rt.ingest(f, 0)
    f.clear()
    f.swarm1(100, [{ a: 0, p: [0, 0, 0] }, { a: 1, p: [1, 0, 0] }]).full1(512, 40, 100, [5, 0, 0])
    rt.ingest(f, 100)
    f.clear()
    // px4-bridge restarts: its Full64 channel 40 carries RESET
    f.full1(512, 40, 90, [7, 0, 0]).reset(40)
    rt.ingest(f, 116)
    off()
    expect(resets).toEqual([{ idBase: 512, idCount: 1 }])
    expect(rt.interp.timesOf(512)).toEqual([90])
    expect(rt.interp.timesOf(0)).toEqual([0, 100])
    rt.dispose()
  })

  it('reports the first samples of each epoch (seek latency hook)', () => {
    const reg = stubRegister()
    const rt = initTime({ register: reg.register, roster: () => null, reducedMotion: () => false })
    const got: [number, number][] = []
    rt.onEpochData = (e, t) => got.push([e, t])
    const f = makeFrame()
    f.time(TS.PAUSED | 0x80, 7, 1, 1000, 0).swarm1(1000, [{ a: 1, p: [0, 0, 0] }])
    rt.ingest(f, 5)
    f.clear()
    f.swarm1(1040, [{ a: 1, p: [0, 0, 0] }])
    rt.ingest(f, 21)
    f.clear()
    f.time(TS.PAUSED | 0x80, 8, 1, 2000, 0).swarm1(2000, [{ a: 1, p: [1, 0, 0] }])
    rt.ingest(f, 40)
    expect(got).toEqual([[7, 5], [8, 40]])
    rt.dispose()
  })
})
