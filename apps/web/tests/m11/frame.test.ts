// TelemetryFrame slot layout (M11 §6.3.8) and the persistent views on both sides (M11-FR-090, FR-095): header offsets
// exactly as the table, the reserved-area additions, FrameFront keeps its views across loads, SlotWriter copies the
// composed image into a transferable slot.
import { describe, expect, it } from 'vitest'
import { FrameFront, H, REGION, RESET_MAX, SLOT_BYTES, SLOT_CAP, SLOT_FULL, SLOT_ITEM, SLOT_MAGIC, SLOT_RAW, SlotWriter } from '@/net/rt/frame'

describe('slot header offsets (M11 §6.3.8 table)', () => {
  it('matches the frozen offsets', () => {
    expect(H).toMatchObject({
      magic: 0, slotNo: 4, frameSeqMax: 8, epoch: 12, flags: 14, frameTSimMs: 16, swarmN: 24, swarmSeq: 28, swarmTSimMs: 32,
      fullCount: 40, rawCount: 44, resetCount: 48, ctrlCount: 52, timeTSimMs: 56, timeTSrvMs: 64, timeRate: 72, timeState: 76,
      connState: 77, timeEpoch: 78, clockOffsetMainMs: 80, srttMs: 88, decodeMs: 96, ageMs: 104, bytesPerS: 112, swarmHz: 120, selHz: 124,
      reconnects: 128, droppedEpochFrames: 132, resetChannelIds: 136, swarmRecvMainMs: 200, focusHz: 208, eventGaps: 212,
    })
    expect(H.resetChannelIds + 2 * RESET_MAX).toBe(H.swarmRecvMainMs)
    // additions live in the reserved area 216-255
    expect(H.timeRecvMainMs).toBeGreaterThanOrEqual(216)
    expect(H.malformedFrames + 4).toBeLessThanOrEqual(256)
    expect(SLOT_MAGIC).toBe(0x31524654)
  })
  it('regions follow the table and fit in 64 KiB', () => {
    expect(REGION).toEqual({ swarmPos: 256, swarmQuat: 12544, swarmVel: 28928, swarmAgentNo: 41216, swarmFs: 43264, swarmFlags: 44288, swarmCtrl: 45312,
      swarmBattery: 46336, full: 47360, raw: 52480, end: 55040 })
    expect(REGION.full + SLOT_ITEM * SLOT_FULL).toBe(REGION.raw)
    expect(REGION.raw + SLOT_ITEM * SLOT_RAW).toBe(REGION.end)
    expect(REGION.swarmBattery + SLOT_CAP).toBe(REGION.full)
    expect(REGION.end).toBeLessThanOrEqual(SLOT_BYTES)
  })
})

describe('persistent views', () => {
  it('SlotWriter composes in its own image and copies it into a slot', () => {
    const w = new SlotWriter()
    w.swarm.pos[0] = 12.5
    w.swarm.agentNo[1023] = 77
    w.dv.setUint32(H.swarmN, 2, true)
    const src = new Uint8Array(64).map((_, i) => i)
    w.writeFull(0, 5, 16, 9, 123.5, src, 0)
    w.writeRaw(3, 7, 40, 1, 4.25, src, 10, 32)
    const slot = new ArrayBuffer(SLOT_BYTES)
    new Uint8Array(slot).fill(0xee)
    w.copyTo(slot)
    const f = new FrameFront()
    f.load(slot)
    expect(f.hdr.swarmN).toBe(2)
    expect(f.swarm.pos[0]).toBe(12.5)
    expect(f.swarm.agentNo[1023]).toBe(77)
    expect(f.fullAgentNo(0)).toBe(5)
    expect(f.fullChannelId(0)).toBe(16)
    expect(f.fullTSampleMs(0)).toBe(123.5)
    expect(f.dv.getUint8(f.fullPayloadOff(0) + 63)).toBe(63)
    expect(f.rawAgentNo(3)).toBe(7)
    expect(f.rawSchema(3)).toBe(1)
    expect(f.dv.getUint16(REGION.raw + SLOT_ITEM * 3 + 6, true)).toBe(32)
    expect(f.dv.getUint8(f.rawPayloadOff(3))).toBe(10)
    // bytes after REGION.end are not part of the image
    expect(new Uint8Array(slot)[REGION.end]).toBe(0xee)
  })

  it('FrameFront keeps the same view objects across loads', () => {
    const f = new FrameFront()
    const views = [f.swarm.pos, f.swarm.agentNo, f.swarm.quat, f.full.bytes, f.resetChannelIds, f.hdr]
    for (let k = 1; k <= 3; k++) {
      const w = new SlotWriter()
      w.dv.setUint32(H.swarmN, k, true)
      w.swarm.pos[0] = k
      const slot = new ArrayBuffer(SLOT_BYTES)
      w.copyTo(slot)
      f.load(slot)
      expect(f.hdr.swarmN).toBe(k)
      expect(f.swarm.pos[0]).toBe(k)
    }
    expect([f.swarm.pos, f.swarm.agentNo, f.swarm.quat, f.full.bytes, f.resetChannelIds, f.hdr]).toEqual(views)
    expect(f.swarm.pos).toBe(views[0])
    expect(f.hdr).toBe(views[5])
    expect(f.loads).toBe(3)
  })
})
