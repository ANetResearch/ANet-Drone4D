// Sidecar index parsers (M12 §7.5.1 .ovw, §7.5.2 .evx; FR-023, FR-018 replay part). Owner: M12. Little-endian,
// fixed layouts written by python/awr/recorder/sidecar.py; a file being written has no record count in its header, so
// readers use (size - header) / record size. int64 nanoseconds are read as lo + hi * 2^32 (exact below 2^53).
export const EVX = { MAGIC: 0x58525741, HEADER: 32, RECORD: 16 } as const // "AWRX"
export const OVW = { MAGIC: 0x4f525741, HEADER: 128, BIN_BASE: 32, TRACK: 16, MAX_TRACKS: 16 } as const // "AWRO"

const i64 = (dv: DataView, o: number): number => dv.getUint32(o, true) + dv.getInt32(o + 4, true) * 4294967296

export interface EvxIndex {
  version: number
  closed: boolean
  markersVersion: number
  segment: number
  tStartNs: number
  n: number
  /** ms from the run start */
  t: Float64Array
  mseq: Uint32Array
  level: Uint8Array
  marker: Uint8Array
  agentNo: Uint16Array
}

export function parseEvx(buf: ArrayBuffer): EvxIndex {
  const dv = new DataView(buf)
  if (buf.byteLength < EVX.HEADER || dv.getUint32(0, true) !== EVX.MAGIC) throw new Error('not an .evx index')
  const version = dv.getUint16(4, true)
  const flags = dv.getUint16(6, true)
  const rb = dv.getUint16(8, true) || EVX.RECORD
  const closed = (flags & 1) !== 0
  const byHeader = dv.getUint32(12, true)
  const bySize = Math.floor((buf.byteLength - EVX.HEADER) / rb)
  const n = closed ? Math.min(byHeader, bySize) : bySize
  const out: EvxIndex = {
    version, closed, markersVersion: dv.getUint16(10, true), tStartNs: i64(dv, 16), segment: dv.getUint32(24, true), n,
    t: new Float64Array(n), mseq: new Uint32Array(n), level: new Uint8Array(n), marker: new Uint8Array(n), agentNo: new Uint16Array(n),
  }
  for (let i = 0, o = EVX.HEADER; i < n; i++, o += rb) {
    out.t[i] = i64(dv, o) / 1e6
    out.mseq[i] = dv.getUint32(o + 8, true)
    out.level[i] = dv.getUint8(o + 12)
    out.marker[i] = dv.getUint8(o + 13)
    out.agentNo[i] = dv.getUint16(o + 14, true)
  }
  return out
}

export interface OvwIndex {
  version: number
  closed: boolean
  tStartNs: number
  binNs: number
  nBins: number
  nTracks: number
  trackAgentNo: Uint16Array
  rosterVersion: number
  segment: number
  /** per bin */
  nPresent: Uint16Array
  nAirborne: Uint16Array
  nAlert: Uint16Array
  nFailsafe: Uint16Array
  /** 4 per bin (levels 0..3) */
  evByLevel: Uint16Array
  minBatteryPct: Uint8Array
  binFlags: Uint8Array
  maxSpeedMps: Float32Array
  meanZM: Float32Array
  maxZM: Float32Array
  /** per bin and track: position (3), battery, flight state, flags (bit0 PRESENT) */
  trackPos: Float32Array
  trackBattery: Uint8Array
  trackFlightState: Uint8Array
  trackFlags: Uint8Array
}

export const OVW_BIN = { GAP: 1, DECIMATED: 2, RERUN: 4 } as const

export function parseOvw(buf: ArrayBuffer): OvwIndex {
  const dv = new DataView(buf)
  if (buf.byteLength < OVW.HEADER || dv.getUint32(0, true) !== OVW.MAGIC) throw new Error('not an .ovw index')
  const flags = dv.getUint16(6, true)
  const closed = (flags & 1) !== 0
  const binBytes = dv.getUint16(28, true)
  const nTracks = Math.min(OVW.MAX_TRACKS, dv.getUint16(30, true))
  const bySize = binBytes > 0 ? Math.floor((buf.byteLength - OVW.HEADER) / binBytes) : 0
  const nBins = closed ? Math.min(dv.getUint32(24, true), bySize) : bySize
  const trackAgentNo = new Uint16Array(nTracks)
  for (let k = 0; k < nTracks; k++) trackAgentNo[k] = dv.getUint16(32 + 2 * k, true)
  const o: OvwIndex = {
    version: dv.getUint16(4, true), closed, tStartNs: i64(dv, 8), binNs: i64(dv, 16), nBins, nTracks, trackAgentNo,
    rosterVersion: dv.getUint32(64, true), segment: dv.getUint32(68, true),
    nPresent: new Uint16Array(nBins), nAirborne: new Uint16Array(nBins), nAlert: new Uint16Array(nBins), nFailsafe: new Uint16Array(nBins),
    evByLevel: new Uint16Array(4 * nBins), minBatteryPct: new Uint8Array(nBins), binFlags: new Uint8Array(nBins),
    maxSpeedMps: new Float32Array(nBins), meanZM: new Float32Array(nBins), maxZM: new Float32Array(nBins),
    trackPos: new Float32Array(3 * nBins * nTracks), trackBattery: new Uint8Array(nBins * nTracks),
    trackFlightState: new Uint8Array(nBins * nTracks), trackFlags: new Uint8Array(nBins * nTracks),
  }
  for (let b = 0; b < nBins; b++) {
    const p = OVW.HEADER + b * binBytes
    o.nPresent[b] = dv.getUint16(p, true)
    o.nAirborne[b] = dv.getUint16(p + 2, true)
    o.nAlert[b] = dv.getUint16(p + 4, true)
    o.nFailsafe[b] = dv.getUint16(p + 6, true)
    for (let l = 0; l < 4; l++) o.evByLevel[4 * b + l] = dv.getUint16(p + 8 + 2 * l, true)
    o.minBatteryPct[b] = dv.getUint8(p + 16)
    o.binFlags[b] = dv.getUint8(p + 17)
    o.maxSpeedMps[b] = dv.getFloat32(p + 20, true)
    o.meanZM[b] = dv.getFloat32(p + 24, true)
    o.maxZM[b] = dv.getFloat32(p + 28, true)
    for (let k = 0; k < nTracks; k++) {
      const q = p + OVW.BIN_BASE + OVW.TRACK * k
      const j = b * nTracks + k
      o.trackPos[3 * j] = dv.getFloat32(q, true)
      o.trackPos[3 * j + 1] = dv.getFloat32(q + 4, true)
      o.trackPos[3 * j + 2] = dv.getFloat32(q + 8, true)
      o.trackBattery[j] = dv.getUint8(q + 12)
      o.trackFlightState[j] = dv.getUint8(q + 13)
      o.trackFlags[j] = dv.getUint8(q + 14)
    }
  }
  return o
}

/** start time (ms) of bin b */
export const ovwBinMs = (o: OvwIndex, b: number): number => (o.tStartNs + b * o.binNs) / 1e6
