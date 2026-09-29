// TelemetryFrame slot layout (M11 §6.3.8; AWR-10 AD-06): three 64 KiB transferable ArrayBuffers circulate between
// rt.worker (writer) and the main thread (reader). Owner: M11.
// Zero allocation (AWR-03 §3.6 rule 1; M11-FR-095): a transferred ArrayBuffer arrives as a new object on the other side,
// so typed views over it cannot be cached. Both sides therefore keep one persistent 64 KiB image with views built once:
// the worker composes the slot in its image (SlotWriter) and copies it into the free slot with a single set(); the main
// thread copies the received slot into its image (FrameFront) the same way. Each side creates exactly one Uint8Array per
// slot for that copy (about 55 KB, a few microseconds), the same class of exception as the DataView per WebSocket message
// (M11 §6.4.14 point 4). SharedArrayBuffer removes even that in V0.3 (M11 §2.2).
import type { SwarmSoA } from './layouts'
import type { FrameHeaderView, FullRecordsView, RawRecordsView, TelemetryFrame } from './types'

export const SLOT_BYTES = 65536
export const SLOT_CAP = 1024 // C: vehicles per slot
export const SLOT_FULL = 64 // F: Full64 records per slot
export const SLOT_RAW = 32 // R: small raw records per slot
export const SLOT_ITEM = 80 // bytes per Full64 or raw item
export const SLOT_MAGIC = 0x31524654 // "TFR1"

/**
 * Header offsets of M11 §6.3.8. `timeRecvMainMs` (216, f64) and `malformedFrames` (224, u32) use the reserved area
 * 216-255 (M12 onTime recvMs; fuzz counter); readers of the frozen layout keep treating them as reserved.
 */
export const H = {
  magic: 0, slotNo: 4, frameSeqMax: 8, epoch: 12, flags: 14, frameTSimMs: 16, swarmN: 24, swarmSeq: 28, swarmTSimMs: 32,
  fullCount: 40, rawCount: 44, resetCount: 48, ctrlCount: 52, timeTSimMs: 56, timeTSrvMs: 64, timeRate: 72, timeState: 76,
  connState: 77, timeEpoch: 78, clockOffsetMainMs: 80, srttMs: 88, decodeMs: 96, ageMs: 104, bytesPerS: 112, swarmHz: 120,
  selHz: 124, reconnects: 128, droppedEpochFrames: 132, resetChannelIds: 136, swarmRecvMainMs: 200, focusHz: 208, eventGaps: 212,
  timeRecvMainMs: 216, malformedFrames: 224,
} as const
export const REGION = {
  swarmPos: 256, swarmQuat: 12544, swarmVel: 28928, swarmAgentNo: 41216, swarmFs: 43264, swarmFlags: 44288, swarmCtrl: 45312,
  swarmBattery: 46336, full: 47360, raw: 52480, end: 55040,
} as const
export const RESET_MAX = 32

/** slot header flags (u16 at 14) */
export const SF = {
  EPOCH_CHANGED: 1, SNAPSHOT: 2, REPLAY: 4, GAP: 8, ROSTER_CHANGED: 16, TIME_CHANGED: 32, CONN_CHANGED: 64,
} as const

/** raw item schema codes (M11 §6.3.8) */
export const RAW_SCHEMA = { ENV_SAMPLE32: 1, SENSOR_POSE48: 2 } as const

export function swarmViews(buf: ArrayBuffer): SwarmSoA {
  return {
    agentNo: new Uint16Array(buf, REGION.swarmAgentNo, SLOT_CAP),
    fs: new Uint8Array(buf, REGION.swarmFs, SLOT_CAP),
    battery: new Uint8Array(buf, REGION.swarmBattery, SLOT_CAP),
    pos: new Float32Array(buf, REGION.swarmPos, 3 * SLOT_CAP),
    quat: new Float32Array(buf, REGION.swarmQuat, 4 * SLOT_CAP),
    vel: new Float32Array(buf, REGION.swarmVel, 3 * SLOT_CAP),
    flags: new Uint8Array(buf, REGION.swarmFlags, SLOT_CAP),
    ctrl: new Uint8Array(buf, REGION.swarmCtrl, SLOT_CAP),
  }
}

export const newHeaderView = (): FrameHeaderView => ({
  slotNo: 0, frameSeqMax: 0, epoch: 0, flags: 0, frameTSimMs: 0, swarmN: 0, swarmSeq: 0, swarmTSimMs: 0, fullCount: 0,
  rawCount: 0, resetCount: 0, timeTSimMs: 0, timeTSrvMs: 0, timeRate: 0, timeState: 0, connState: 0, timeEpoch: 0,
  clockOffsetMainMs: 0, srttMs: 0, decodeMs: 0, ageMs: Number.NaN, bytesPerS: 0, swarmHz: 0, selHz: 0, focusHz: 0,
  reconnects: 0, droppedEpochFrames: 0, eventGaps: 0, swarmRecvMainMs: 0, timeRecvMainMs: 0, malformedFrames: 0,
})

export function readHeader(dv: DataView, out: FrameHeaderView): FrameHeaderView {
  out.slotNo = dv.getUint32(H.slotNo, true)
  out.frameSeqMax = dv.getUint32(H.frameSeqMax, true)
  out.epoch = dv.getUint16(H.epoch, true)
  out.flags = dv.getUint16(H.flags, true)
  out.frameTSimMs = dv.getFloat64(H.frameTSimMs, true)
  out.swarmN = dv.getUint32(H.swarmN, true)
  out.swarmSeq = dv.getUint32(H.swarmSeq, true)
  out.swarmTSimMs = dv.getFloat64(H.swarmTSimMs, true)
  out.fullCount = dv.getUint32(H.fullCount, true)
  out.rawCount = dv.getUint32(H.rawCount, true)
  out.resetCount = dv.getUint32(H.resetCount, true)
  out.timeTSimMs = dv.getFloat64(H.timeTSimMs, true)
  out.timeTSrvMs = dv.getFloat64(H.timeTSrvMs, true)
  out.timeRate = dv.getFloat32(H.timeRate, true)
  out.timeState = dv.getUint8(H.timeState)
  out.connState = dv.getUint8(H.connState)
  out.timeEpoch = dv.getUint16(H.timeEpoch, true)
  out.clockOffsetMainMs = dv.getFloat64(H.clockOffsetMainMs, true)
  out.srttMs = dv.getFloat64(H.srttMs, true)
  out.decodeMs = dv.getFloat64(H.decodeMs, true)
  out.ageMs = dv.getFloat64(H.ageMs, true)
  out.bytesPerS = dv.getFloat64(H.bytesPerS, true)
  out.swarmHz = dv.getFloat32(H.swarmHz, true)
  out.selHz = dv.getFloat32(H.selHz, true)
  out.reconnects = dv.getUint32(H.reconnects, true)
  out.droppedEpochFrames = dv.getUint32(H.droppedEpochFrames, true)
  out.swarmRecvMainMs = dv.getFloat64(H.swarmRecvMainMs, true)
  out.focusHz = dv.getFloat32(H.focusHz, true)
  out.eventGaps = dv.getUint32(H.eventGaps, true)
  out.timeRecvMainMs = dv.getFloat64(H.timeRecvMainMs, true)
  out.malformedFrames = dv.getUint32(H.malformedFrames, true)
  return out
}

/** Read-only view of one slot image: the header object, the swarm SoA and the Full64 and raw item regions. */
export class SlotReader implements TelemetryFrame {
  readonly hdr: FrameHeaderView = newHeaderView()
  readonly swarm: SwarmSoA
  readonly full: FullRecordsView
  readonly raw: RawRecordsView
  readonly resetChannelIds: Uint16Array
  readonly dv: DataView
  constructor(readonly buf: ArrayBuffer) {
    const dv = new DataView(buf)
    this.dv = dv
    this.swarm = swarmViews(buf)
    this.full = { count: 0, bytes: dv, base: REGION.full }
    this.raw = { count: 0, bytes: dv, base: REGION.raw }
    this.resetChannelIds = new Uint16Array(buf, H.resetChannelIds, RESET_MAX)
    this.refresh()
  }
  /** re-read the header after the image changed */
  refresh(): void {
    readHeader(this.dv, this.hdr)
    this.full.count = Math.min(this.hdr.fullCount, SLOT_FULL)
    this.raw.count = Math.min(this.hdr.rawCount, SLOT_RAW)
  }
  /** Full64 item k: agent number, channel, sample time; payload at fullPayloadOff(k) */
  fullAgentNo(k: number): number {
    return this.dv.getUint16(REGION.full + SLOT_ITEM * k, true)
  }
  fullChannelId(k: number): number {
    return this.dv.getUint16(REGION.full + SLOT_ITEM * k + 2, true)
  }
  fullTSampleMs(k: number): number {
    return this.dv.getFloat64(REGION.full + SLOT_ITEM * k + 8, true)
  }
  fullPayloadOff(k: number): number {
    return REGION.full + SLOT_ITEM * k + 16
  }
  rawSchema(k: number): number {
    return this.dv.getUint8(REGION.raw + SLOT_ITEM * k + 4)
  }
  rawAgentNo(k: number): number {
    return this.dv.getUint16(REGION.raw + SLOT_ITEM * k, true)
  }
  rawPayloadOff(k: number): number {
    return REGION.raw + SLOT_ITEM * k + 16
  }
}

/**
 * Main-thread TelemetryFrame: a persistent 64 KiB image whose views never change. load() copies a received slot into it
 * (one Uint8Array over the slot) and re-reads the header; swapFrame() hands out this same object every frame.
 */
export class FrameFront extends SlotReader {
  private readonly image: Uint8Array
  loads = 0
  constructor() {
    super(new ArrayBuffer(SLOT_BYTES))
    this.image = new Uint8Array(this.buf, 0, REGION.end)
  }
  load(slot: ArrayBuffer): void {
    this.image.set(new Uint8Array(slot, 0, REGION.end))
    this.refresh()
    this.loads++
  }
}

/**
 * Worker-side slot composer over its own persistent image (views built once). The decoder writes the swarm SoA, the
 * Full64 and raw items and the header here; copyTo() moves the image into a free transferable slot.
 */
export class SlotWriter {
  readonly buf = new ArrayBuffer(SLOT_BYTES)
  readonly dv = new DataView(this.buf)
  readonly swarm: SwarmSoA = swarmViews(this.buf)
  readonly u8 = new Uint8Array(this.buf)
  private readonly image = new Uint8Array(this.buf, 0, REGION.end)
  writeFull(k: number, agentNo: number, channelId: number, seq: number, tSampleMs: number, src: Uint8Array, off: number): void {
    const o = REGION.full + SLOT_ITEM * k
    this.dv.setUint16(o, agentNo, true)
    this.dv.setUint16(o + 2, channelId, true)
    this.dv.setUint32(o + 4, seq >>> 0, true)
    this.dv.setFloat64(o + 8, tSampleMs, true)
    for (let i = 0; i < 64; i++) this.u8[o + 16 + i] = src[off + i]
  }
  writeRaw(k: number, agentNo: number, channelId: number, schema: number, tSampleMs: number, src: Uint8Array, off: number, len: number): void {
    const o = REGION.raw + SLOT_ITEM * k
    const n = Math.min(len, 64)
    this.dv.setUint16(o, agentNo, true)
    this.dv.setUint16(o + 2, channelId, true)
    this.dv.setUint8(o + 4, schema)
    this.dv.setUint8(o + 5, 0)
    this.dv.setUint16(o + 6, n, true)
    this.dv.setFloat64(o + 8, tSampleMs, true)
    for (let i = 0; i < n; i++) this.u8[o + 16 + i] = src[off + i]
  }
  /** copy the composed image into a transferable slot (one view per slot) */
  copyTo(slot: ArrayBuffer): void {
    new Uint8Array(slot, 0, REGION.end).set(this.image)
  }
}
