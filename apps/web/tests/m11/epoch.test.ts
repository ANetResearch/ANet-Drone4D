// Epoch rules of rt.worker (M11-FR-089, M11-AC-021; AWR-17 §6.10): TIME updates the epoch on arrival; a BATCH of an
// unknown epoch waits for the next TIME and passes only when that TIME carries its epoch; old-epoch BATCH are dropped;
// the slot after an epoch switch carries EPOCH_CHANGED and no record of the old epoch; with the fake gateway an epoch
// bump sends TIME before the SNAPSHOT frame.
import { describe, expect, it } from 'vitest'
import { BATCH_SNAPSHOT, OP_BATCH, OP_TIME } from '@awr/contracts/frame'
import { SF } from '@/net/rt/frame'
import { frames, rig } from '../net/harness'
import { CH, batchFrame, lastFrame, handshake, lite32, scriptRig, timeFrame } from './wire'

const swarm = (epoch: number, frameSeq: number, x: number): ArrayBuffer =>
  batchFrame({ epoch, frameSeq, tSimNs: frameSeq * 1e8, records: [{ ch: CH.swarm, seq: frameSeq, payload: lite32(1, () => ({ pos: [x, 0, 0] })) }] })

describe('epoch stash rule', () => {
  it('new-epoch BATCH before its TIME passes after the TIME', () => {
    const r = scriptRig()
    handshake(r, { epoch: 1 })
    r.pull()
    r.sock.serverSend(swarm(2, 1, 22)) // arrives before TIME(epoch 2)
    r.pull()
    expect(lastFrame(r).hdr.swarmN).toBe(0)
    r.sock.serverSend(timeFrame({ epoch: 2, tSimNs: 0, tSrvNs: 5e9 }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.epoch).toBe(2)
    expect(f.hdr.flags & SF.EPOCH_CHANGED).toBeTruthy()
    expect(f.hdr.swarmN).toBe(1)
    expect(f.swarm.pos[0]).toBe(22)
    expect(f.hdr.droppedEpochFrames).toBe(0)
  })

  it('an old-epoch BATCH after the switch is dropped at the next TIME; only one frame is stashed', () => {
    const r = scriptRig()
    handshake(r, { epoch: 1 })
    r.pull()
    r.sock.serverSend(timeFrame({ epoch: 2, tSimNs: 0, tSrvNs: 5e9 }))
    r.sock.serverSend(swarm(1, 7, 11)) // stale epoch 1 (stashed, unknown until TIME)
    r.sock.serverSend(swarm(1, 8, 12)) // replaces the stash: the first counts as dropped
    r.sock.serverSend(timeFrame({ epoch: 2, tSimNs: 1e8, tSrvNs: 5.1e9 }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.droppedEpochFrames).toBe(2)
    expect(f.hdr.swarmN).toBe(0)
    expect(r.host.counters.droppedEpochFrames).toBe(2)
  })

  it('an epoch switch forgets records of the old epoch that were not yet pulled', () => {
    const r = scriptRig()
    handshake(r, { epoch: 1 })
    r.pull()
    r.sock.serverSend(swarm(1, 1, 5))
    r.sock.serverSend(timeFrame({ epoch: 3, tSimNs: 0, tSrvNs: 5e9 }))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.flags & SF.EPOCH_CHANGED).toBeTruthy()
    expect(f.hdr.swarmN).toBe(0)
    expect(f.hdr.timeEpoch).toBe(3)
  })

  it('epoch is compared for equality only (wrap-around 65535 -> 0)', () => {
    const r = scriptRig()
    handshake(r, { epoch: 65535 })
    r.pull()
    r.sock.serverSend(timeFrame({ epoch: 0, tSimNs: 0, tSrvNs: 5e9 }))
    r.sock.serverSend(swarm(0, 1, 9))
    r.pull()
    const f = lastFrame(r)
    expect(f.hdr.epoch).toBe(0)
    expect(f.hdr.swarmN).toBe(1)
  })
})

describe('fake gateway epoch bump', () => {
  it('TIME with the new epoch precedes the SNAPSHOT frame; the slot shows EPOCH_CHANGED then SNAPSHOT', async () => {
    const r = await rig({ url: 'fake:?n=2', n: 2 })
    r.host.onMessage({ cmd: 'sub', subs: [{ id: 1, topic: 'swarm/state', rate: 20, mode: 'latest' }] })
    await frames(r, 20)
    const order: number[] = []
    const fake = r.fake
    const prev = fake.onmessage!
    fake.onmessage = (e) => {
      if (typeof e.data !== 'string') {
        const u8 = new Uint8Array(e.data)
        if (u8[0] === OP_TIME) order.push(-new DataView(e.data).getUint16(2, true))
        else if (u8[0] === OP_BATCH && (u8[1] & BATCH_SNAPSHOT)) order.push(new DataView(e.data).getUint16(2, true))
      }
      prev(e)
    }
    const before = r.slots.length
    r.world!.bumpEpoch()
    await frames(r, 10)
    expect(order[0]).toBe(-2) // TIME epoch 2 first
    expect(order).toContain(2) // then a SNAPSHOT BATCH of epoch 2
    expect(order.indexOf(2)).toBeGreaterThan(0)
    const after = r.slots.slice(before)
    const iChanged = after.findIndex((s) => (s.hdr.flags & SF.EPOCH_CHANGED) !== 0)
    const iSnap = after.findIndex((s) => (s.hdr.flags & SF.SNAPSHOT) !== 0 && s.hdr.epoch === 2)
    expect(iChanged).toBeGreaterThanOrEqual(0)
    expect(iSnap).toBeGreaterThanOrEqual(iChanged)
    expect(after[iSnap].hdr.swarmN).toBe(2)
  })
})
