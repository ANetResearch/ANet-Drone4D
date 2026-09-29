// Test helpers for engine/time (M12 §10.1): a deterministic RNG (DET-01), a hand-built TelemetryFrame with the slot
// header fields M12 reads, and a register() stub that collects the clock-phase task so tests drive frames by hand.
import { DS64 } from '@awr/contracts/layouts'
import { SF } from '@/net/rt/frame'
import type { FrameHeaderView, TelemetryFrame } from '@/net/rt/types'
import { ctx as frameCtx, type FrameCtx, type TaskFn } from '@/engine/loop'

export function lcg(seed: number): () => number {
  let x = seed >>> 0 || 1
  return () => {
    x = (Math.imul(x, 1664525) + 1013904223) >>> 0
    return x / 4294967296
  }
}

export interface TestFrame extends TelemetryFrame {
  hdr: FrameHeaderView
  clear(): void
  time(state: number, epoch: number, rate: number, tSimMs: number, tSrvMs: number, recvMs?: number): TestFrame
  swarm1(tMs: number, rows: { a: number; p: number[]; v?: number[]; q?: number[]; fs?: number }[], recvMs?: number): TestFrame
  full1(agent: number, channel: number, tMs: number, p: number[], v?: number[], q?: number[], w?: number[]): TestFrame
  reset(...channels: number[]): TestFrame
}

export function makeFrame(cap = 1024): TestFrame {
  const hdr = {
    slotNo: 0, frameSeqMax: 0, epoch: 0, flags: 0, frameTSimMs: 0, swarmN: 0, swarmSeq: 0, swarmTSimMs: 0, fullCount: 0, rawCount: 0,
    resetCount: 0, timeTSimMs: 0, timeTSrvMs: 0, timeRate: 1, timeState: 0, connState: 3, timeEpoch: 0, clockOffsetMainMs: 0, srttMs: 1,
    decodeMs: 0, ageMs: 0, bytesPerS: 0, swarmHz: 10, selHz: 0, focusHz: 0, reconnects: 0, droppedEpochFrames: 0, eventGaps: 0,
    swarmRecvMainMs: 0, timeRecvMainMs: 0, malformedFrames: 0,
  } as FrameHeaderView
  const swarm = {
    agentNo: new Uint16Array(cap), fs: new Uint8Array(cap), battery: new Uint8Array(cap), flags: new Uint8Array(cap), ctrl: new Uint8Array(cap),
    pos: new Float32Array(3 * cap), vel: new Float32Array(3 * cap), quat: new Float32Array(4 * cap),
  }
  const buf = new ArrayBuffer(64 * 80)
  const dv = new DataView(buf)
  const full = { count: 0, bytes: dv, base: 0 }
  const raw = { count: 0, bytes: dv, base: 0 }
  const resetChannelIds = new Uint16Array(32)
  const f: TestFrame = {
    hdr, swarm: swarm as unknown as TelemetryFrame['swarm'], full, raw, resetChannelIds,
    clear() {
      hdr.flags = 0
      hdr.swarmN = 0
      hdr.resetCount = 0
      full.count = 0
      return undefined as unknown as void
    },
    time(state, epoch, rate, tSimMs, tSrvMs, recvMs = 0) {
      hdr.flags |= SF.TIME_CHANGED
      hdr.timeState = state
      hdr.timeEpoch = epoch
      hdr.timeRate = rate
      hdr.timeTSimMs = tSimMs
      hdr.timeTSrvMs = tSrvMs
      hdr.timeRecvMainMs = recvMs
      if ((epoch & 0xffff) !== hdr.epoch) {
        hdr.epoch = epoch & 0xffff
        hdr.flags |= SF.EPOCH_CHANGED
      }
      return f
    },
    swarm1(tMs, rows, recvMs = 0) {
      hdr.swarmN = rows.length
      hdr.swarmTSimMs = tMs
      hdr.swarmSeq++
      hdr.swarmRecvMainMs = recvMs
      rows.forEach((r, i) => {
        swarm.agentNo[i] = r.a
        swarm.fs[i] = r.fs ?? 5
        swarm.battery[i] = 80
        for (let j = 0; j < 3; j++) {
          swarm.pos[3 * i + j] = r.p[j]
          swarm.vel[3 * i + j] = r.v?.[j] ?? 0
        }
        const q = r.q ?? [0, 0, 0, 1]
        for (let j = 0; j < 4; j++) swarm.quat[4 * i + j] = q[j]
      })
      return f
    },
    full1(agent, channel, tMs, p, v = [0, 0, 0], q = [0, 0, 0, 1], w = [0, 0, 0]) {
      const o = full.count * 80
      dv.setUint16(o, agent, true)
      dv.setUint16(o + 2, channel, true)
      dv.setFloat64(o + 8, tMs, true)
      const r = o + 16
      dv.setUint16(r + DS64.AGENT_NO, agent, true)
      dv.setUint8(r + DS64.FLIGHT_STATE, 5)
      dv.setUint8(r + DS64.BATTERY_PCT, 90)
      for (let j = 0; j < 3; j++) {
        dv.setFloat32(r + DS64.POS + 4 * j, p[j], true)
        dv.setFloat32(r + DS64.VEL + 4 * j, v[j], true)
        dv.setFloat32(r + DS64.OMEGA + 4 * j, w[j], true)
      }
      for (let j = 0; j < 4; j++) dv.setFloat32(r + DS64.Q + 4 * j, q[j], true)
      full.count++
      hdr.fullCount = full.count
      return f
    },
    reset(...channels) {
      hdr.resetCount = channels.length
      channels.forEach((c, i) => {
        resetChannelIds[i] = c
      })
      return f
    },
  }
  return f
}

/** register() stub: keeps the clock task so tests can run it with a FrameCtx */
export function stubRegister(): { register: (phase: string, id: string, fn: TaskFn) => () => void; run(nowMs: number): FrameCtx; tasks: Map<string, TaskFn> } {
  const tasks = new Map<string, TaskFn>()
  const c: FrameCtx = { ...frameCtx }
  return {
    tasks,
    register: (_phase, id, fn) => {
      tasks.set(id, fn)
      return () => tasks.delete(id)
    },
    run(nowMs) {
      c.dtMs = nowMs - c.nowMs
      c.nowMs = nowMs
      c.frameNo++
      for (const fn of tasks.values()) fn(c)
      return c
    },
  }
}
