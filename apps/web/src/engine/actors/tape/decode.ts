// Canonical Event Tape decoder (M23 §6.7, §6.8; AWR-05 §5.2.3; docs/05 §5.2 front-end tape decoder). Owner: M06.
// Container: magic "D4DTAPE\x01" | u32 header_len | JCS header | u32 n_blocks | n_blocks × 40 B index (u64 off_S, u32 comp_S,
// u32 raw_S, u64 off_B, u32 comp_B, u32 raw_B, u32 tick0, u16 n_ticks, u16 0) | blocks. Block b covers ticks [100b, 100b + 100)
// and is split into the scripted sub-block S (idx < n_scripted) and the background sub-block B. A sub-block is laid out
// field → actor → tick; per actor the first value is absolute and the rest are deltas wrapped to the field width.
// Records are 22 B: x_mm i32 | y_mm i32 | z_mm i32 | yaw_cdeg i16 | vx_cmps i16 | vy_cmps i16 | vz_cmps i16 | state u8 | container u8.
// Decoding matches awr.sim.events.tape.TapeReader bit for bit (golden packages/contracts/golden/event/tape-small.*).
import { decompress } from 'fzstd'

export const TAPE_MAGIC = 'D4DTAPE\x01'
export const TICK_NS = 100_000_000
export const BLOCK_TICKS = 100
export const NO_CONTAINER = 255
export const RECORD_BYTES = 22
export const STATE_NAMES = ['absent', 'moving', 'waiting', 'in_vehicle', 'inside', 'gone'] as const
const VISIBLE = (s: number): boolean => s === 1 || s === 2
const DEG2RAD = Math.PI / 180 // numpy.radians: x · (π / 180)

export const FIELDS = [
  ['x_mm', 4], ['y_mm', 4], ['z_mm', 4], ['yaw_cdeg', 2], ['vx_cmps', 2], ['vy_cmps', 2], ['vz_cmps', 2], ['state', 1], ['container', 1],
] as const
export type FieldName = (typeof FIELDS)[number][0]

export interface TapeActor {
  idx: number
  id: string
  class: string
  scripted: boolean
  profile: string
  size_mm: [number, number, number]
  appearance: Record<string, unknown>
}

export interface TapeHeader {
  schema: 'drone4d.tape.v1'
  event_id: string
  event_version: number
  n_ticks: number
  duration_ms: number
  actors: TapeActor[]
  world: { id: string; content_version: string } & Record<string, unknown>
  [k: string]: unknown
}

export interface TapeBlock { offS: number; compS: number; rawS: number; offB: number; compB: number; rawB: number; tick0: number; nTicks: number }

/** Decoded sub-block: one Int32Array per field, index actor·nTicks + tick (values sign- or zero-extended from the field width). */
export interface SubFrames { nTicks: number; nActors: number; f: Record<FieldName, Int32Array> }

/** Actor states at one instant (SI units; velocity is zero for non-visible states). */
export interface ActorStates {
  ids: Int32Array
  pos: Float64Array // 3 per actor, ENU m
  yaw: Float64Array // rad
  vel: Float64Array // 3 per actor, ENU m/s
  state: Uint8Array
  container: Uint8Array
}

export class TapeError extends Error {
  constructor(readonly code: number, message: string) {
    super(message)
  }
}

const u64 = (dv: DataView, o: number): number => {
  const v = dv.getBigUint64(o, true)
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) throw new TapeError(620, 'tape offset out of range')
  return Number(v)
}

function decodeSub(raw: Uint8Array, nTicks: number, nActors: number): SubFrames {
  const n = nTicks * nActors
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
  const f = {} as Record<FieldName, Int32Array>
  let p = 0
  for (const [name, w] of FIELDS) {
    const out = new Int32Array(n)
    if (p + n * w > raw.byteLength) throw new TapeError(620, 'tape sub-block length mismatch')
    for (let a = 0; a < nActors; a++) {
      let acc = 0
      for (let t = 0; t < nTicks; t++) {
        const o = p + (a * nTicks + t) * w
        const d = w === 4 ? dv.getInt32(o, true) : w === 2 ? dv.getInt16(o, true) : dv.getUint8(o)
        acc = t === 0 ? d : acc + d
        // same-width wrap, identical to numpy cumsum in the field dtype
        acc = w === 4 ? acc | 0 : w === 2 ? (acc << 16) >> 16 : acc & 0xff
        out[a * nTicks + t] = acc
      }
    }
    f[name] = out
    p += n * w
  }
  if (p !== raw.byteLength) throw new TapeError(620, 'tape sub-block length mismatch')
  return { nTicks, nActors, f }
}

export class TapeReader {
  readonly header: TapeHeader
  readonly headerBytes: Uint8Array
  readonly blocks: TapeBlock[]
  readonly nTicks: number
  readonly nActors: number
  readonly nScripted: number
  private readonly buf: Uint8Array
  private readonly cache = new Map<string, SubFrames>()

  constructor(data: ArrayBuffer | Uint8Array) {
    const b = data instanceof Uint8Array ? data : new Uint8Array(data)
    this.buf = b
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength)
    if (b.byteLength < 16 || String.fromCharCode(...b.subarray(0, 8)) !== TAPE_MAGIC) throw new TapeError(620, 'not a drone4d tape (magic)')
    const hl = dv.getUint32(8, true)
    this.headerBytes = b.subarray(12, 12 + hl)
    this.header = JSON.parse(new TextDecoder().decode(this.headerBytes)) as TapeHeader
    if (this.header.schema !== 'drone4d.tape.v1') throw new TapeError(620, `unsupported tape schema ${String(this.header.schema)}`)
    let p = 12 + hl
    const nb = dv.getUint32(p, true)
    p += 4
    this.blocks = []
    for (let i = 0; i < nb; i++, p += 40) {
      this.blocks.push({
        offS: u64(dv, p), compS: dv.getUint32(p + 8, true), rawS: dv.getUint32(p + 12, true),
        offB: u64(dv, p + 16), compB: dv.getUint32(p + 24, true), rawB: dv.getUint32(p + 28, true),
        tick0: dv.getUint32(p + 32, true), nTicks: dv.getUint16(p + 36, true),
      })
    }
    this.nTicks = this.header.n_ticks
    this.nActors = this.header.actors.length
    this.nScripted = this.header.actors.filter((a) => a.scripted).length
    let t = 0
    for (const blk of this.blocks) {
      if (blk.tick0 !== t) throw new TapeError(614, `block index not contiguous at tick ${t}`)
      t += blk.nTicks
    }
    if (t !== this.nTicks) throw new TapeError(614, `blocks cover ${t} ticks, header says ${this.nTicks}`)
  }

  /** Sub-block `S` (scripted) or `B` (background) of block `bi`; LRU of 6 sub-blocks. */
  sub(bi: number, which: 'S' | 'B'): SubFrames {
    const key = `${bi}${which}`
    const hit = this.cache.get(key)
    if (hit) {
      this.cache.delete(key)
      this.cache.set(key, hit)
      return hit
    }
    const blk = this.blocks[bi]
    const nA = which === 'S' ? this.nScripted : this.nActors - this.nScripted
    const [off, comp, raw] = which === 'S' ? [blk.offS, blk.compS, blk.rawS] : [blk.offB, blk.compB, blk.rawB]
    let sf: SubFrames
    if (nA === 0) {
      sf = decodeSub(new Uint8Array(0), blk.nTicks, 0)
    } else {
      let data: Uint8Array
      try {
        data = decompress(this.buf.subarray(off, off + comp))
      } catch (e) {
        throw new TapeError(614, `block ${bi} ${which}: ${(e as Error).message}`)
      }
      if (data.byteLength !== raw) throw new TapeError(614, `block ${bi} ${which}: raw length ${data.byteLength} != ${raw}`)
      sf = decodeSub(data, blk.nTicks, nA)
    }
    this.cache.set(key, sf)
    while (this.cache.size > 6) this.cache.delete(this.cache.keys().next().value as string)
    return sf
  }

  /** Field value of actor `idx` at tick `k` (clamped to [0, n_ticks − 1]). */
  value(name: FieldName, idx: number, k: number): number {
    k = Math.min(Math.max(Math.trunc(k), 0), this.nTicks - 1)
    const bi = Math.floor(k / BLOCK_TICKS)
    const j = k - bi * BLOCK_TICKS
    const s = idx < this.nScripted
    const sf = this.sub(bi, s ? 'S' : 'B')
    const a = s ? idx : idx - this.nScripted
    return sf.f[name][a * sf.nTicks + j]
  }

  /** Canonical frame data F (tick-major 22 B records), the body of content_sha256 = SHA-256(header bytes ‖ F). */
  canonicalBody(): Uint8Array {
    const out = new Uint8Array(this.nTicks * this.nActors * RECORD_BYTES)
    const dv = new DataView(out.buffer)
    for (let k = 0; k < this.nTicks; k++) {
      for (let i = 0; i < this.nActors; i++) {
        let o = (k * this.nActors + i) * RECORD_BYTES
        for (const [name, w] of FIELDS) {
          const v = this.value(name, i, k)
          if (w === 4) dv.setInt32(o, v, true)
          else if (w === 2) dv.setInt16(o, v, true)
          else dv.setUint8(o, v)
          o += w
        }
      }
    }
    return out
  }

  /** M23 §6.8: k = ⌊t / 100 ms⌋; step (tick k) when the visibility class or the container changes, otherwise cubic Hermite
   * with velocities and shortest-arc yaw. t past the last tick holds the last tick; t < 0 returns tick 0 with state 0. */
  actorStates(tNs: number, ids?: ArrayLike<number>): ActorStates {
    const sel = Int32Array.from(ids ?? Array.from({ length: this.nActors }, (_, i) => i))
    const n = sel.length
    const st: ActorStates = { ids: sel, pos: new Float64Array(3 * n), yaw: new Float64Array(n), vel: new Float64Array(3 * n),
      state: new Uint8Array(n), container: new Uint8Array(n) }
    const neg = tNs < 0
    let k = neg ? 0 : Math.floor(tNs / TICK_NS)
    let rem = neg ? 0 : tNs - k * TICK_NS
    if (k >= this.nTicks - 1) {
      k = this.nTicks - 1
      rem = 0
    }
    for (let j = 0; j < n; j++) {
      const i = sel[j]
      const s0 = this.value('state', i, k)
      const c0 = this.value('container', i, k)
      const vis0 = VISIBLE(s0)
      const p0 = [this.value('x_mm', i, k) / 1000, this.value('y_mm', i, k) / 1000, this.value('z_mm', i, k) / 1000]
      const v0 = vis0 ? [this.value('vx_cmps', i, k) / 100, this.value('vy_cmps', i, k) / 100, this.value('vz_cmps', i, k) / 100] : [0, 0, 0]
      const y0 = this.value('yaw_cdeg', i, k)
      st.state[j] = neg ? 0 : s0
      st.container[j] = c0
      let pos = p0
      let vel = neg ? [0, 0, 0] : v0
      let yawC = y0
      if (rem > 0) {
        const k1 = k + 1
        const s1 = this.value('state', i, k1)
        if (vis0 && VISIBLE(s1) && c0 === this.value('container', i, k1)) {
          const p1 = [this.value('x_mm', i, k1) / 1000, this.value('y_mm', i, k1) / 1000, this.value('z_mm', i, k1) / 1000]
          const v1 = [this.value('vx_cmps', i, k1) / 100, this.value('vy_cmps', i, k1) / 100, this.value('vz_cmps', i, k1) / 100]
          const h = TICK_NS / 1e9
          const s = rem / 1e9 / h
          const s2 = s * s
          const s3 = s2 * s
          pos = [0, 1, 2].map((c) => (2 * s3 - 3 * s2 + 1) * p0[c] + (s3 - 2 * s2 + s) * h * v0[c] + (-2 * s3 + 3 * s2) * p1[c] + (s3 - s2) * h * v1[c])
          vel = [0, 1, 2].map((c) => ((6 * s2 - 6 * s) / h) * p0[c] + (3 * s2 - 4 * s + 1) * v0[c] + ((-6 * s2 + 6 * s) / h) * p1[c] + (3 * s2 - 2 * s) * v1[c])
          const dy = (((this.value('yaw_cdeg', i, k1) - y0 + 18000) % 36000) + 36000) % 36000 - 18000
          yawC = y0 + dy * (rem / TICK_NS)
        }
      }
      st.pos.set(pos, 3 * j)
      st.vel.set(vel, 3 * j)
      st.yaw[j] = (yawC / 100) * DEG2RAD
    }
    return st
  }
}
