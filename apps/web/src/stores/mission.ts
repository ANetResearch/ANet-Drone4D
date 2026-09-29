// Mission store (M10-FR-066; M10 §8.1; AWR-17 §6.5, §6.6). Owner: M10.
// Writers: `bindMissionStore()` subscribes `mission/*/status` (msgpack awr.mission.status.v1, 2 Hz) and `uav/*/path`
// (blob awr.blob.polyline4.v1) on the page RtClient and batches the updates into at most 4 store writes per second
// (M10-AC-033). Rows are deduplicated by (revision, t_ns): an older sample never replaces a newer one. The status
// payload carries no generator or vehicle list, so the first status of an unknown mission triggers one R23 fetch
// (`GET /api/missions`, debounced). Path points reference the received buffer without copying (Float32Array view on
// the blob after its 16-byte header). Detail (R24) is loaded lazily by `loadMissionDetail(mid)` and converted to the
// overlay shape M06 reads (waypoints, areas, slots). Pure geometry (formation slots, CAPT) lives in ./missionGeom.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { loop } from '@/engine/loop'
import { apiGet } from '@/net/api'
import { rtClient, type DataMsg, type RtClient } from '@/net/rt'

export { captAssign, captCost, formationSlots, hungarian, rotateSlots, FORMATION_SHAPES, type FormationShape } from './missionGeom'

export type MissionState = 'IDLE' | 'RUNNING' | 'PAUSED' | 'DONE' | 'ABORTED'
export type TrackState = 'PENDING' | 'TRANSIT' | 'WORKING' | 'WAITING' | 'RETURNING' | 'SUSPENDED' | 'DONE' | 'DROPPED'
  // skeleton values (M15-S placeholder) kept for existing fixtures; never produced by the wire decoder
  | 'ACTIVE' | 'FAILED' | 'SKIPPED'
export interface MissionTrackRow {
  vehicleId: string
  state: TrackState
  /** current item index (0-based); null before items exist */
  item: number
  total: number
}
export interface MissionRow {
  mid: string
  generator: string
  state: MissionState
  vehicles: string[]
  progressPct: number
  etaS: number | null
  revision: number
  /** simulation ns of the status sample (dedupe key with revision); absent in hand-written fixtures */
  tNs?: number
  tracks: MissionTrackRow[]
  metrics?: { facadeCoverage?: number; areaCoverage?: number; formationErrRmsM?: number }
  plan?: { pending: boolean; lastMs: number; planner?: 'profile' | 'astar25'; degraded?: boolean }
  formation?: { phase: string; shape: string; rmsM: number; headingRad: number }
}
export interface MissionPathEntry {
  trajId: number
  rev: number
  /** (x, y, z, t_rel_s) f32 quads; a view on the received blob (no copy) */
  pts: Float32Array
}
export interface GridGeom { x0M: number; y0M: number; resM: number; w: number; h: number }
export interface MissionDetail {
  mid: string
  revision: number
  generator: string
  region: [number, number][] | null
  /** overlay shapes (M06 missionDataOf) */
  waypoints: { x: number; y: number; z: number; state: 'planned' | 'reached' | 'current' }[]
  areas: { ring: Float64Array; kind: 'task' | 'coverage' }[]
  slots: { x: number; y: number; z: number }[]
  coverageGrid?: GridGeom
  raw: unknown
}
export interface MissionStore {
  rows: Map<string, MissionRow>
  paths: Map<string, MissionPathEntry>
  detail: Map<string, MissionDetail>
  /** mission/{mid}/coverage owner snapshots (ext; geom from R24 coverage_grid, null until the detail is loaded) */
  coverage: Map<string, { geom: GridGeom | null; owner: Uint8Array; ver: number }>
  /** R26 preview result (ext), null when none */
  preview: unknown
}

export const missionStore = createAwrStore<MissionStore>('mission', () => ({
  rows: new Map(), paths: new Map(), detail: new Map(), coverage: new Map(), preview: null,
}))

const ORDER: Record<MissionState, number> = { RUNNING: 0, PAUSED: 1, IDLE: 2, DONE: 3, ABORTED: 4 }
let rowsCache: MissionRow[] = []
let rowsSource: Map<string, MissionRow> | null = null
/** sorted by state (running first) then mid; the array reference is stable while rows is unchanged */
export const selectMissionRows = (s: MissionStore): MissionRow[] => {
  if (s.rows !== rowsSource) {
    rowsSource = s.rows
    rowsCache = [...s.rows.values()].sort((a, b) => ORDER[a.state] - ORDER[b.state] || a.mid.localeCompare(b.mid))
  }
  return rowsCache
}
export const selectPathOf = (vehicleId: string) => (s: MissionStore): Float32Array | null => s.paths.get(vehicleId)?.pts ?? null
export const selectMissionOf = (mid: string) => (s: MissionStore): MissionRow | undefined => s.rows.get(mid)

export function useMission<T>(sel: (s: MissionStore) => T): T {
  return useStore(missionStore, sel)
}

// ------------------------------------------------------------------------------------------------ decoding
const num = (v: unknown, d = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : d)
const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d)
const MSTATES = new Set<MissionState>(['IDLE', 'RUNNING', 'PAUSED', 'DONE', 'ABORTED'])

/** awr.mission.status.v1 -> MissionRow (generator and vehicles are merged from the previous row or R23). */
export function decodeStatus(o: unknown, prev?: MissionRow): MissionRow | null {
  if (!o || typeof o !== 'object') return null
  const m = o as Record<string, unknown>
  const mid = str(m.mid)
  const state = str(m.state) as MissionState
  if (!mid || !MSTATES.has(state)) return null
  const tracks: MissionTrackRow[] = Array.isArray(m.tracks)
    ? (m.tracks as Record<string, unknown>[]).map((t) => ({
        vehicleId: str(t.vehicle_id), state: str(t.state, 'PENDING') as TrackState, item: num(t.item), total: num(t.total),
      }))
    : []
  const row: MissionRow = {
    mid, state, generator: prev?.generator ?? '', vehicles: prev?.vehicles.length ? prev.vehicles : tracks.map((t) => t.vehicleId),
    progressPct: num(m.progress_pct), etaS: typeof m.eta_s === 'number' ? m.eta_s : null, revision: num(m.revision),
    tNs: num(m.t_ns), tracks,
  }
  const met = m.metrics as Record<string, unknown> | undefined
  if (met && typeof met === 'object') {
    row.metrics = {}
    if (typeof met.facade_coverage === 'number') row.metrics.facadeCoverage = met.facade_coverage
    if (typeof met.area_coverage === 'number') row.metrics.areaCoverage = met.area_coverage
    if (typeof met.formation_err_rms_m === 'number') row.metrics.formationErrRmsM = met.formation_err_rms_m
  }
  const pl = m.plan as Record<string, unknown> | undefined
  if (pl && typeof pl === 'object') row.plan = { pending: pl.pending === true, lastMs: num(pl.last_ms) }
  const f = m.formation as Record<string, unknown> | undefined
  if (f && typeof f === 'object') {
    row.formation = { phase: str(f.phase), shape: str(f.shape), rmsM: num(f.rms_m), headingRad: num(f.heading_rad) }
  }
  return row
}

/** Newer wins: higher revision, or the same revision with a later sample time. */
export function isNewer(a: MissionRow, b: MissionRow | undefined): boolean {
  if (!b) return true
  if (a.revision !== b.revision) return a.revision > b.revision
  return (a.tNs ?? 0) >= (b.tNs ?? 0)
}

/** awr.blob.polyline4.v1: 16-byte header "AWRB" | u16 version | u16 dtype (2 = f32) | u32 count | u32 comp (4). */
export function decodePolyline4(data: ArrayBuffer | ArrayBufferView): Float32Array | null {
  const buf = data instanceof ArrayBuffer ? data : data.buffer
  const off = data instanceof ArrayBuffer ? 0 : data.byteOffset
  const len = data instanceof ArrayBuffer ? data.byteLength : data.byteLength
  if (len < 16) return null
  const dv = new DataView(buf as ArrayBuffer, off, len)
  if (dv.getUint8(0) !== 0x41 || dv.getUint8(1) !== 0x57 || dv.getUint8(2) !== 0x52 || dv.getUint8(3) !== 0x42) return null
  const dtype = dv.getUint16(6, true)
  const count = dv.getUint32(8, true)
  const comp = dv.getUint32(12, true)
  if (dtype !== 2 || comp !== 4 || 16 + count * 16 > len) return null
  if ((off + 16) % 4 === 0) return new Float32Array(buf as ArrayBuffer, off + 16, count * 4)
  return new Float32Array((buf as ArrayBuffer).slice(off + 16, off + 16 + count * 16))
}

/** awr.blob.grid_u8.v1: 16-byte header "AWRB" | u16 version | u16 dtype (3 = u8) | u32 count | u32 comp (1); view, no copy. */
export function decodeGridU8(data: ArrayBuffer | ArrayBufferView): Uint8Array | null {
  const buf = data instanceof ArrayBuffer ? data : data.buffer
  const off = data instanceof ArrayBuffer ? 0 : data.byteOffset
  const len = data.byteLength
  if (len < 16) return null
  const dv = new DataView(buf as ArrayBuffer, off, len)
  if (dv.getUint8(0) !== 0x41 || dv.getUint8(1) !== 0x57 || dv.getUint8(2) !== 0x52 || dv.getUint8(3) !== 0x42) return null
  const count = dv.getUint32(8, true)
  if (dv.getUint16(6, true) !== 3 || dv.getUint32(12, true) !== 1 || 16 + count > len) return null
  return new Uint8Array(buf as ArrayBuffer, off + 16, count)
}

// ------------------------------------------------------------------------------------------------ batching (<= 4 Hz)
export const FLUSH_MIN_MS = 250
const pendingRows = new Map<string, MissionRow>()
const pendingPaths = new Map<string, MissionPathEntry>()
const pendingCoverage = new Map<string, Uint8Array>()
let coverageSeq = 0
let flushTimer: ReturnType<typeof setTimeout> | null = null
let lastFlush = -Infinity
let pathSeq = 0
let indexTimer: ReturnType<typeof setTimeout> | null = null
const known = new Map<string, { generator: string; vehicles: string[] }>()

function nowMs(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

/** Writes the pending updates in one setState; returns whether a write happened. */
export function flushMission(): boolean {
  flushTimer = null
  if (pendingRows.size === 0 && pendingPaths.size === 0 && pendingCoverage.size === 0) return false
  lastFlush = nowMs()
  const s = missionStore.getState()
  const patch: Partial<MissionStore> = {}
  if (pendingRows.size) {
    const rows = new Map(s.rows)
    for (const [mid, r] of pendingRows) {
      const k = known.get(mid)
      if (k) {
        if (!r.generator) r.generator = k.generator
        if (k.vehicles.length) r.vehicles = k.vehicles
      }
      if (isNewer(r, rows.get(mid))) rows.set(mid, r)
    }
    pendingRows.clear()
    patch.rows = rows
  }
  if (pendingPaths.size) {
    const paths = new Map(s.paths)
    for (const [vid, p] of pendingPaths) {
      if (p.pts.length === 0) paths.delete(vid)
      else paths.set(vid, p)
    }
    pendingPaths.clear()
    patch.paths = paths
  }
  if (pendingCoverage.size) {
    const coverage = new Map(s.coverage)
    for (const [mid, owner] of pendingCoverage) {
      const geom = s.detail.get(mid)?.coverageGrid ?? null
      coverage.set(mid, { geom, owner, ver: ++coverageSeq })
      if (!geom) void loadMissionDetail(mid)
    }
    pendingCoverage.clear()
    patch.coverage = coverage
  }
  missionStore.setState(patch)
  return true
}

function schedule(): void {
  if (flushTimer !== null) return
  const wait = Math.max(0, FLUSH_MIN_MS - (nowMs() - lastFlush))
  flushTimer = setTimeout(flushMission, wait)
}

/** Feed one channel message (exported for tests and replay tools). */
export function ingestMissionData(m: DataMsg): void {
  const topic = m.topic
  if (topic.startsWith('mission/') && topic.endsWith('/status')) {
    const prev = pendingRows.get(topic.slice(8, -7)) ?? missionStore.getState().rows.get(topic.slice(8, -7))
    const row = decodeStatus(m.data, prev)
    if (!row) return
    if (!isNewer(row, prev)) return
    pendingRows.set(row.mid, row)
    if (!known.has(row.mid) && !row.generator) scheduleIndex()
    schedule()
  } else if (topic.startsWith('uav/') && topic.endsWith('/path')) {
    const vid = topic.slice(4, -5)
    const d = m.data
    const pts = d instanceof ArrayBuffer || ArrayBuffer.isView(d) ? decodePolyline4(d as ArrayBuffer | ArrayBufferView) : null
    if (!pts) return
    pendingPaths.set(vid, { trajId: 0, rev: ++pathSeq, pts })
    schedule()
  } else if (topic.startsWith('mission/') && topic.endsWith('/coverage')) {
    const d = m.data
    const owner = d instanceof ArrayBuffer || ArrayBuffer.isView(d) ? decodeGridU8(d as ArrayBuffer | ArrayBufferView) : null
    if (!owner) return
    pendingCoverage.set(topic.slice(8, -9), owner)
    schedule()
  }
}

/** Subscribe the coverage snapshot of one mission while an overlay shows it (published only when subscribed). */
export function subscribeCoverage(mid: string, rt: RtClient | null = rtClient()): () => void {
  if (!rt) return () => {}
  return rt.subscribe(`mission/${mid}/coverage`, { rate: 1 })
}

function scheduleIndex(): void {
  if (indexTimer !== null) return
  indexTimer = setTimeout(() => {
    indexTimer = null
    void fetchMissionIndex()
  }, 300)
}

/** R23: generator and vehicle lists for the rows (the status topic does not carry them). */
export async function fetchMissionIndex(): Promise<void> {
  try {
    const r = await apiGet<{ items: { mid: string; generator: string; vehicles: string[] }[] }>('/api/missions')
    for (const it of r.items ?? []) known.set(it.mid, { generator: it.generator ?? '', vehicles: it.vehicles ?? [] })
    const s = missionStore.getState()
    let changed = false
    const rows = new Map(s.rows)
    for (const [mid, row] of rows) {
      const k = known.get(mid)
      if (k && (row.generator !== k.generator || row.vehicles.join() !== k.vehicles.join())) {
        rows.set(mid, { ...row, generator: k.generator, vehicles: k.vehicles.length ? k.vehicles : row.vehicles })
        changed = true
      }
    }
    if (changed) missionStore.setState({ rows })
  } catch {
    // viewer without backend (FakeSource) or the api is restarting: the next unknown status retries
  }
}

/** R24 -> overlay detail (waypoints of each item start, task area, formation slots); cached by revision. */
export async function loadMissionDetail(mid: string): Promise<MissionDetail | null> {
  try {
    const d = await apiGet<Record<string, unknown>>(`/api/missions/${encodeURIComponent(mid)}`)
    const det = toDetail(d)
    const s = missionStore.getState()
    const detail = new Map(s.detail)
    detail.set(mid, det)
    const cov = s.coverage.get(mid)
    if (cov && !cov.geom && det.coverageGrid) {
      const coverage = new Map(s.coverage)
      coverage.set(mid, { ...cov, geom: det.coverageGrid })
      missionStore.setState({ detail, coverage })
    } else {
      missionStore.setState({ detail })
    }
    return det
  } catch {
    return null
  }
}

export function toDetail(d: Record<string, unknown>): MissionDetail {
  const waypoints: MissionDetail['waypoints'] = []
  const tracks = Array.isArray(d.tracks) ? (d.tracks as Record<string, unknown>[]) : []
  for (const t of tracks) {
    const cursor = num(t.cursor)
    const items = Array.isArray(t.items) ? (t.items as Record<string, unknown>[]) : []
    items.forEach((it, i) => {
      const p = Array.isArray(it.pos) ? (it.pos as number[]) : [0, 0, 0]
      waypoints.push({ x: num(p[0]), y: num(p[1]), z: num(p[2]), state: i < cursor ? 'reached' : i === cursor ? 'current' : 'planned' })
    })
  }
  const region = Array.isArray(d.region) ? (d.region as [number, number][]) : null
  const areas: MissionDetail['areas'] = []
  if (region && region.length >= 3) {
    const ring = new Float64Array(region.length * 2)
    region.forEach((p, i) => {
      ring[2 * i] = num(p[0])
      ring[2 * i + 1] = num(p[1])
    })
    areas.push({ ring, kind: 'coverage' })
  }
  const slots: MissionDetail['slots'] = []
  const f = d.formation as Record<string, unknown> | null | undefined
  if (f && Array.isArray(f.slots_flu)) {
    const z = num(f.z_m)
    for (const s of f.slots_flu as number[][]) slots.push({ x: num(s[0]), y: num(s[1]), z })
  }
  const cg = d.coverage_grid as Record<string, unknown> | undefined
  return {
    mid: str(d.mid), revision: num(d.revision), generator: str(d.generator), region, waypoints, areas, slots,
    coverageGrid: cg ? { x0M: num(cg.x0_m), y0M: num(cg.y0_m), resM: num(cg.res_m), w: num(cg.w), h: num(cg.h) } : undefined,
    raw: d,
  }
}

// ------------------------------------------------------------------------------------------------ binding
let unbind: (() => void) | null = null

/** Subscribe the mission topics on the page RtClient (idempotent); returns the release function. */
export function bindMissionStore(rt: RtClient | null = rtClient()): () => void {
  if (unbind) return unbind
  if (!rt) return () => {}
  const offData = rt.onData(ingestMissionData)
  const offS = rt.subscribe('mission/*/status', { rate: 2 })
  const offP = rt.subscribe('uav/*/path', { rate: 2 })
  unbind = () => {
    offData()
    offS()
    offP()
    unbind = null
  }
  return unbind
}

// bind once the page RtClient exists (checked at 1 Hz in the overlay phase; no work after binding)
loop.register('overlay', 'mission-store.bind', () => {
  if (!unbind) {
    const rt = rtClient()
    if (rt) bindMissionStore(rt)
  }
}, { fps: 1 })

/** Test helper: clear the store and the batching state. */
export function resetMissionStore(): void {
  if (flushTimer !== null) clearTimeout(flushTimer)
  if (indexTimer !== null) clearTimeout(indexTimer)
  flushTimer = null
  indexTimer = null
  lastFlush = -Infinity
  pendingRows.clear()
  pendingPaths.clear()
  pendingCoverage.clear()
  known.clear()
  missionStore.setState({ rows: new Map(), paths: new Map(), detail: new Map(), coverage: new Map(), preview: null })
}
