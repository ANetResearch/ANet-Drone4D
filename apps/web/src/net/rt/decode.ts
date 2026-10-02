// Record indexing and slot decoding of rt.worker (M11-FR-089, FR-090, FR-096; M11 §6.4.14; AWR-17 §6.4-§6.7). Owner: M11.
// ChannelTable maps channel ids from `advertise` to decode kinds, entity ids and the effective subscribed rate (from
// `subscribed`, recomputed on unsubscribe). Decoder keeps, per channel, only a reference to the latest record (buffer,
// offset, length, seq, sample time); compose() decodes the dirty channels into the worker's persistent slot image: swarm
// Lite32 into the SoA, Full64 and EnvSample32/SensorPose48 copied as-is, msgpack/json decoded onto the control path.
// Receive statistics per 1 s window: swarmHz, selHz (the 60 Hz channels, highest per-channel rate), focusHz (median of
// the per-channel rates of the 30 Hz channels), bytesPerS; selJitterMs is the p95 of |Δw − 1000/selHz| over the last 64
// worker arrival intervals of the 60 Hz channels (FX2-R3: the focus delay D_focus of ADR-046 is computed from these
// arrivals; the main thread only sees the latest sample per channel and frame, so its own arrival times are frame times). The steady path allocates nothing except the one Uint8Array
// per received buffer that the byte copies read from (cached for consecutive records of the same message).
import { decode as mpDecode } from '@msgpack/msgpack'
import { BATCH_GAP, BATCH_REPLAY, BATCH_SNAPSHOT, ENC_JSON, ENC_MSGPACK, ENC_RAW, RF_RESET, SL32, decodeSwarmLite32Into, nextRecord } from './layouts'
import type { RecordView, RtBatchHeaderRec } from './layouts'
import { RAW_SCHEMA, RESET_MAX, SF, SLOT_CAP, SLOT_FULL, SLOT_RAW, type SlotWriter } from './frame'
import { str } from './types'

export const NCH = 65536
export const K_NONE = 0
export const K_SWARM = 1
export const K_FULL = 2
export const K_ENV32 = 3
export const K_POSE48 = 4
export const K_DATA = 5
export const K_ROSTER = 6
/** selected-vehicle and focus-set rates (AWR-17 §6.6 default subscription set) */
export const SEL_RATE = 60
export const FOCUS_RATE = 30
/** 60 Hz channels tracked for the arrival jitter, and the interval ring length */
export const SEL_SLOTS = 16
export const SEL_DW = 64

export type CtrlMsg = { op: string } & Record<string, unknown>

/** Channel id table built from advertise / unadvertise / subscribed (low-frequency path: allocation allowed). */
export class ChannelTable {
  readonly kind = new Uint8Array(NCH)
  readonly enc = new Uint8Array(NCH)
  readonly agent = new Int32Array(NCH).fill(-1)
  readonly rate = new Uint8Array(NCH)
  readonly topic: (string | undefined)[] = []
  readonly entity: (string | undefined)[] = []
  readonly agentOf = new Map<string, number>()
  private readonly subChans = new Map<number, { rate: number; ch: number[] }>()
  size = 0

  /** a new gateway instance (sessionId changed): every id is re-advertised */
  clear(): void {
    this.kind.fill(0)
    this.enc.fill(0)
    this.agent.fill(-1)
    this.rate.fill(0)
    this.topic.length = 0
    this.entity.length = 0
    this.subChans.clear()
    this.size = 0
  }

  advertise(c: Record<string, unknown>): void {
    const id = Number(c.id)
    if (!(id > 0 && id < NCH)) return
    const topic = str(c.topic)
    const schema = str(c.schemaName)
    const enc = str(c.encoding)
    let kind = K_NONE
    if (topic === 'fleet/roster') kind = K_ROSTER
    else if (enc === 'raw' && schema === 'awr.SwarmLite32.v1') kind = K_SWARM
    else if (enc === 'raw' && schema === 'awr.DroneState64.v1') kind = K_FULL
    else if (enc === 'raw' && schema === 'awr.EnvSample32.v1') kind = K_ENV32
    else if (enc === 'raw' && schema === 'awr.SensorPose48.v1') kind = K_POSE48
    else if (enc === 'msgpack' || enc === 'json') kind = K_DATA
    if (this.kind[id] === K_NONE && kind !== K_NONE) this.size++
    this.kind[id] = kind
    this.enc[id] = enc === 'json' ? ENC_JSON : enc === 'msgpack' ? ENC_MSGPACK : ENC_RAW
    this.topic[id] = topic
    const ent = c.entity as { id?: string } | null | undefined
    const eid = ent?.id ?? (topic.startsWith('uav/') ? topic.split('/')[1] : undefined)
    this.entity[id] = eid
    this.agent[id] = eid !== undefined ? (this.agentOf.get(eid) ?? -1) : -1
  }

  unadvertise(id: number): void {
    if (!(id > 0 && id < NCH)) return
    if (this.kind[id] !== K_NONE) this.size--
    this.kind[id] = K_NONE
    this.enc[id] = ENC_RAW
    this.topic[id] = undefined
    this.entity[id] = undefined
    this.agent[id] = -1
    this.rate[id] = 0
  }

  /** `subscribed{id, channels, rate, added?}`: the effective rate of each channel is the highest of its subscriptions */
  onSubscribed(subId: number, rate: number, channels: readonly number[], added: boolean): void {
    const r = Math.max(0, Math.min(255, Math.round(rate)))
    const prev = this.subChans.get(subId)
    const ch = added && prev ? [...prev.ch, ...channels] : [...channels]
    const touched = prev ? [...prev.ch, ...channels] : [...channels]
    this.subChans.set(subId, { rate: r, ch })
    this.recompute(touched)
  }

  onUnsubscribed(subIds: readonly number[]): void {
    const touched: number[] = []
    for (const id of subIds) {
      const s = this.subChans.get(id)
      if (!s) continue
      this.subChans.delete(id)
      touched.push(...s.ch)
    }
    this.recompute(touched)
  }

  /** a reconnect re-subscribes everything: forget the per-subscription channel lists */
  resetSubscriptions(): void {
    this.subChans.clear()
    this.rate.fill(0)
  }

  private recompute(chs: readonly number[]): void {
    for (const c of chs) {
      if (!(c > 0 && c < NCH)) continue
      let r = 0
      for (const s of this.subChans.values()) if (s.rate > r && s.ch.includes(c)) r = s.rate
      this.rate[c] = r
    }
  }

  setRoster(entries: readonly { agent_no: number; id: string }[]): void {
    this.agentOf.clear()
    for (const e of entries) this.agentOf.set(e.id, e.agent_no)
    for (let i = 1; i < this.entity.length; i++) {
      const eid = this.entity[i]
      if (eid !== undefined) this.agent[i] = this.agentOf.get(eid) ?? -1
    }
  }
}

export interface ComposeOut {
  swarmN: number
  swarmSeq: number
  swarmTSimMs: number
  fullN: number
  rawN: number
  /** at least one dirty channel was decoded */
  fresh: boolean
}

const td = new TextDecoder()

/** Latest-record index per channel and the slot composer (see file header). */
export class Decoder {
  readonly ch = new ChannelTable()
  // latest record per channel (references into received buffers)
  private readonly refBuf: (ArrayBuffer | null)[] = new Array<ArrayBuffer | null>(NCH).fill(null)
  private readonly refOff = new Int32Array(NCH)
  private readonly refLen = new Int32Array(NCH)
  private readonly refSeq = new Uint32Array(NCH)
  private readonly refT = new Float64Array(NCH)
  private readonly mark = new Uint8Array(NCH)
  private readonly dirty = new Uint16Array(NCH)
  dirtyN = 0
  // frame-level state carried into the next slot
  seqMax = 0
  frameTSimMs = 0
  latestTSimMs = Number.NaN
  flags = 0
  readonly resetIds = new Uint16Array(RESET_MAX)
  resetN = 0
  droppedEpochFrames = 0
  malformedFrames = 0
  eventGaps = 0
  swarmRecvAt = 0
  // 1 s receive window
  private winStart = 0
  private winSwarm = 0
  private winBytes = 0
  private readonly winCount = new Uint16Array(NCH)
  private readonly winList = new Uint16Array(512)
  private winListN = 0
  private readonly tmp = new Float32Array(512)
  swarmHz = 0
  selHz = 0
  focusHz = 0
  bytesPerS = 0
  /** arrival jitter p95 of the 60 Hz channels (ms, worker time); NaN until 8 intervals are known */
  selJitterMs = Number.NaN
  // last worker arrival per 60 Hz channel (at most SEL_SLOTS channels; the selection subscribes at most 8) and a ring of
  // the last SEL_DW arrival intervals over all of them
  private readonly selCh = new Int32Array(SEL_SLOTS).fill(-1)
  private readonly selAt = new Float64Array(SEL_SLOTS)
  private readonly selDw = new Float64Array(SEL_DW)
  private selDwN = 0
  private selDwHead = 0
  private readonly selTmp = new Float64Array(SEL_DW)
  // one Uint8Array per received buffer, reused for consecutive records of the same message
  private u8Buf: ArrayBuffer | null = null
  private u8View: Uint8Array = new Uint8Array(0)

  private u8Of(buf: ArrayBuffer): Uint8Array {
    if (buf !== this.u8Buf) {
      this.u8Buf = buf
      this.u8View = new Uint8Array(buf)
    }
    return this.u8View
  }

  /** forget all references (new epoch, new session); counters are kept */
  clearRefs(): void {
    for (let i = 0; i < this.dirtyN; i++) {
      const c = this.dirty[i]
      this.mark[c] = 0
      this.refBuf[c] = null
    }
    this.dirtyN = 0
  }

  countBytes(n: number): void {
    this.winBytes += n
  }

  /**
   * Index the records of one BATCH (header already read into fh). Unknown channels and encodings are skipped by length
   * (AWR-17 §6.4 rule 3); a truncated or inconsistent frame stops at the bad record, counts as malformed and keeps the
   * records before it (their bounds were checked). Returns false for a malformed frame.
   */
  indexBatch(buf: ArrayBuffer, dv: DataView, fh: RtBatchHeaderRec, rv: RecordView, now: number): boolean {
    if (fh.frame_seq > this.seqMax) this.seqMax = fh.frame_seq
    const tFrame = fh.t_sim_ns / 1e6
    this.frameTSimMs = tFrame
    if (!(tFrame < this.latestTSimMs)) this.latestTSimMs = tFrame
    if (fh.flags & BATCH_SNAPSHOT) this.flags |= SF.SNAPSHOT
    if (fh.flags & BATCH_REPLAY) this.flags |= SF.REPLAY
    if (fh.flags & BATCH_GAP) {
      this.flags |= SF.GAP
      this.eventGaps++
    }
    const ct = this.ch
    let off = 16
    try {
      while (off >= 0 && off < buf.byteLength) {
        off = nextRecord(dv, off, rv)
        const c = rv.channel_id
        const kind = ct.kind[c]
        if (kind === K_NONE) continue
        if (rv.rflags & RF_RESET && this.resetN < RESET_MAX) {
          let seen = false
          for (let i = 0; i < this.resetN; i++) if (this.resetIds[i] === c) seen = true
          if (!seen) this.resetIds[this.resetN++] = c
        }
        this.refBuf[c] = buf
        this.refOff[c] = rv.payload_off
        this.refLen[c] = rv.length
        this.refSeq[c] = rv.seq
        this.refT[c] = tFrame + rv.dt_us / 1000
        if (!this.mark[c]) {
          this.mark[c] = 1
          this.dirty[this.dirtyN++] = c
        }
        if (kind === K_SWARM) {
          this.swarmRecvAt = now
          this.winSwarm++
        } else if (kind === K_FULL) {
          if (this.winCount[c]++ === 0 && this.winListN < this.winList.length) this.winList[this.winListN++] = c
          if (ct.rate[c] >= SEL_RATE) this.selArrival(c, now)
        }
      }
    } catch {
      this.malformedFrames++
      return false
    }
    return true
  }

  /** one arrival of a 60 Hz channel record at worker time now (ms) */
  private selArrival(c: number, now: number): void {
    let k = -1
    let free = -1
    for (let i = 0; i < SEL_SLOTS; i++) {
      if (this.selCh[i] === c) {
        k = i
        break
      }
      if (free < 0 && this.selCh[i] < 0) free = i
    }
    if (k < 0) {
      k = free >= 0 ? free : (c % SEL_SLOTS)
      this.selCh[k] = c
      this.selAt[k] = now
      return
    }
    const d = now - this.selAt[k]
    this.selAt[k] = now
    // several records of one channel in one message (catch-up) share the arrival: no interval
    if (!(d > 0) || d > 1000) return
    this.selDw[this.selDwHead] = d
    this.selDwHead = (this.selDwHead + 1) % SEL_DW
    if (this.selDwN < SEL_DW) this.selDwN++
  }

  /** forget the 60 Hz arrival history (unsubscribe of the selection, new session) */
  resetSel(): void {
    this.selCh.fill(-1)
    this.selDwN = 0
    this.selDwHead = 0
    this.selJitterMs = Number.NaN
  }

  /** roll the 1 s receive window (called on every received message) */
  roll(now: number): void {
    if (this.winStart === 0) this.winStart = now
    const dt = now - this.winStart
    if (dt < 1000) return
    const s = dt / 1000
    this.swarmHz = this.winSwarm / s
    this.bytesPerS = this.winBytes / s
    let sel = 0
    let nf = 0
    for (let i = 0; i < this.winListN; i++) {
      const c = this.winList[i]
      const hz = this.winCount[c] / s
      this.winCount[c] = 0
      const r = this.ch.rate[c]
      if (r >= SEL_RATE) {
        if (hz > sel) sel = hz
      } else if (r >= FOCUS_RATE) this.tmp[nf++] = hz
    }
    this.selHz = sel
    this.focusHz = nf > 0 ? median(this.tmp, nf) : 0
    this.selJitterMs = sel > 0 && this.selDwN >= 8 ? jitterP95(this.selDw, this.selDwN, 1000 / sel, this.selTmp) : Number.NaN
    this.winSwarm = 0
    this.winBytes = 0
    this.winListN = 0
    this.winStart = now
  }

  /**
   * Decode the dirty channels into the slot image. msgpack/json channels are decoded onto the control path (roster as
   * `roster`, the rest as `data`); the roster also refreshes the channel -> agent map.
   */
  compose(w: SlotWriter, sink: (m: CtrlMsg) => void): ComposeOut {
    const ct = this.ch
    const out: ComposeOut = composeOut
    out.swarmN = 0
    out.swarmSeq = 0
    out.swarmTSimMs = 0
    out.fullN = 0
    out.rawN = 0
    out.fresh = this.dirtyN > 0
    for (let i = 0; i < this.dirtyN; i++) {
      const c = this.dirty[i]
      this.mark[c] = 0
      const src = this.refBuf[c]
      this.refBuf[c] = null
      if (!src) continue
      const kind = ct.kind[c]
      const off = this.refOff[c]
      const len = this.refLen[c]
      if (kind === K_SWARM) {
        const n = Math.min(Math.floor(len / SL32.SIZE), SLOT_CAP)
        decodeSwarmLite32Into(src, off, n, w.swarm)
        out.swarmN = n
        out.swarmSeq = this.refSeq[c]
        out.swarmTSimMs = this.refT[c]
      } else if (kind === K_FULL) {
        if (out.fullN < SLOT_FULL && len >= 64) {
          const u8 = this.u8Of(src)
          w.writeFull(out.fullN++, u8[off] | (u8[off + 1] << 8), c, this.refSeq[c], this.refT[c], u8, off)
        }
      } else if (kind === K_ENV32 || kind === K_POSE48) {
        if (out.rawN < SLOT_RAW) {
          const u8 = this.u8Of(src)
          const agent = kind === K_POSE48 && len >= 2 ? u8[off] | (u8[off + 1] << 8) : ct.agent[c]
          w.writeRaw(out.rawN++, agent < 0 ? 0xffff : agent, c, kind === K_ENV32 ? RAW_SCHEMA.ENV_SAMPLE32 : RAW_SCHEMA.SENSOR_POSE48, this.refT[c], u8, off, len)
        }
      } else if (kind === K_DATA || kind === K_ROSTER) {
        this.decodeData(c, kind, src, off, len, sink)
      }
    }
    this.dirtyN = 0
    return out
  }

  /** dirty msgpack/json channels waiting for decode (roster, env/state, perf/server, sys/procs, state_ext ...) */
  get dataPending(): number {
    let n = 0
    for (let i = 0; i < this.dirtyN; i++) {
      const k = this.ch.kind[this.dirty[i]]
      if (k === K_DATA || k === K_ROSTER) n++
    }
    return n
  }

  /**
   * Decode only the msgpack/json channels onto the control path and keep the raw references for the next slot: used
   * when no frame loop pulls (hidden page, a page without the viewport), so roster and low-rate state still flow.
   */
  takeData(sink: (m: CtrlMsg) => void): number {
    let w = 0
    let n = 0
    for (let i = 0; i < this.dirtyN; i++) {
      const c = this.dirty[i]
      const kind = this.ch.kind[c]
      const src = this.refBuf[c]
      if ((kind === K_DATA || kind === K_ROSTER) && src) {
        this.mark[c] = 0
        this.refBuf[c] = null
        this.decodeData(c, kind, src, this.refOff[c], this.refLen[c], sink)
        n++
      } else this.dirty[w++] = c
    }
    this.dirtyN = w
    return n
  }

  private decodeData(c: number, kind: number, src: ArrayBuffer, off: number, len: number, sink: (m: CtrlMsg) => void): void {
    let data: unknown
    try {
      const body = new Uint8Array(src, off, len)
      data = this.ch.enc[c] === ENC_JSON ? JSON.parse(td.decode(body)) : mpDecode(body)
    } catch {
      this.malformedFrames++
      return
    }
    if (kind === K_ROSTER) {
      const r = (data ?? {}) as { roster_version?: number; entries?: { agent_no: number; id: string }[] }
      const entries = Array.isArray(r.entries) ? r.entries : []
      this.ch.setRoster(entries)
      this.flags |= SF.ROSTER_CHANGED
      sink({ op: 'roster', version: r.roster_version ?? 0, entries })
      return
    }
    sink({ op: 'data', topic: this.ch.topic[c] ?? '', channelId: c, seq: this.refSeq[c], tSimMs: this.refT[c], data })
  }
}

const composeOut: ComposeOut = { swarmN: 0, swarmSeq: 0, swarmTSimMs: 0, fullN: 0, rawN: 0, fresh: false }

/** p95 of |d − ideal| over the first n values of d (insertion sort into tmp; n <= tmp.length) */
export function jitterP95(d: Float64Array, n: number, ideal: number, tmp: Float64Array): number {
  for (let i = 0; i < n; i++) {
    const v = Math.abs(d[i] - ideal)
    let j = i - 1
    while (j >= 0 && tmp[j] > v) {
      tmp[j + 1] = tmp[j]
      j--
    }
    tmp[j + 1] = v
  }
  return tmp[Math.min(n - 1, Math.floor(0.95 * n))]
}

/** median of the first n values (insertion sort in place; n <= 512) */
export function median(a: Float32Array, n: number): number {
  for (let i = 1; i < n; i++) {
    const x = a[i]
    let j = i - 1
    while (j >= 0 && a[j] > x) {
      a[j + 1] = a[j]
      j--
    }
    a[j + 1] = x
  }
  return n % 2 ? a[(n - 1) >> 1] : (a[n / 2 - 1] + a[n / 2]) / 2
}
