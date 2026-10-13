// Perception store (M19-FR-017, FR-026, FR-028; M18-M19 PRD §8.3, §8.4; AWR-04 §6.4, §11.7; AWR-03 §4.3: owner M19, M15 writes
// the HUD, FPV capture box and event panel JSX).
// Fields:
//   uav           the followed vehicle: explicit follow (FPV, payload panel) or the selection primary
//   frame         latest `uav/{uav}/perception` (5 Hz, interest-driven on the server: only encoded while subscribed)
//   hud           HUD line of the frame: "9.6 / 14 px" and the limiting factor (i18n perception.limiting.<name>); null hides it
//   captured      targets in CAPTURED or HOLDING in the frame (FPV capture boxes, HERO candidates)
//   captures      recent perception.capture events, at most one per target per 30 s (the toast itself is M15 d2Notices with
//                 the same window, CAPTURE_DEDUPE_MS), newest last, <= 32
//   detections    recent perception.* events (level, capture, capture_lost, detect), newest last, <= 200
//   D2.1 (M19-FR-036; AWR-04 §15.11; ADR-124, ADR-125):
//   fieldView     field view of the viewport toolbar: none | acoustic | visual | thermal | radar (M15 binds its prefs key)
//   volumesMode   perception volumes: off | selected | all; physicalRef shows physical probabilities of geometric targets
//   deviceClass   phone | tablet | desktop (M15 lib/formFactor): the phone halves the profile and readout rates
//   legend        unit, band edges, dashed reference shells and reference sensor of the current field view
//   quant         pair readings of the selected target (target/{id}/quant, awr.target.quant.v1 decoded), the readout card
// Writers: a reference-counted feed (usePerception attaches it) subscribes the followed vehicle's frame and listens to the
// event batches; the summary is mirrored at the shared UI tick (Tier S <= 4 writes/s, ADR-066) only when it changed. Strings
// from the wire pass engine/perception decodePerception and lib/sanitize. No write requests (viewer safe, D2-AC-37).
// The same feed subscribes field/{kind} @1 while a field view is on, field/volumes @1 while volumes or the acoustic / visual
// view (target shells) are on, and target/{selected}/quant @2 (phone @1) while a target is selected; the decoded profiles and
// volumes go to engine/perception fieldModel (the renderer reads `fieldModel.version`, values change at most 1 Hz, phone
// 0.5 Hz); the readings and the legend are mirrored here. Interest-driven on the server: nothing is generated while nobody
// subscribes, and the power-save disconnect keeps the last data (static render).
// D2.2-W1: frames, events, profiles, volumes and readings belong to the world of the realtime source (stores/worldScope):
// they are ingested and subscribed only while that world is the one the view shows, and a world or source switch drops
// them with the followed vehicle (the toolbar's view, volumes and reference choice are view preferences and stay).
import { useEffect } from 'react'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { loop, type FrameCtx } from '@/engine/loop'
import {
  capturedTargets, decodeFieldProfile, decodeFieldVolumes, decodePerception, decodeTargetQuant, fieldModel, FIELD_KINDS, hudLine, type FieldKind,
  type HudInfo, type PerceptionFrame, type ProfileFrame, type QuantFrame,
} from '@/engine/perception'
import { rtClient, type ConnState, type DataMsg, type RtClient, type RtEvent } from '@/net/rt'
import { selectionStore } from './selection'
import { targetsStore } from './targets'
import { uiTickDue } from './uiTick'
import { onWorldReset, worldLive } from './worldScope'

export type { FieldKind, PairReading, QuantFrame } from '@/engine/perception'
export type FieldView = FieldKind | 'none'
export type VolumesMode = 'off' | 'selected' | 'all'
export type DeviceClass = 'phone' | 'tablet' | 'desktop'
export const FIELD_VIEWS: readonly FieldView[] = ['none', ...FIELD_KINDS]
/** subscription rates (Hz): profiles and volumes 1 (rate class floor; the phone applies every other profile, 0.5 Hz), readings 2 (phone 1) */
export const FIELD_RATE = 1
export const QUANT_RATE = { phone: 1, tablet: 2, desktop: 2 } as const
export const PROFILE_APPLY_MS = { phone: 2000, tablet: 0, desktop: 0 } as const

export interface FieldLegend {
  kind: FieldKind
  unit: ProfileFrame['unit']
  bands: readonly number[]
  iso: readonly { id: string; v: number }[]
  shellRef: 'person' | 'D'
  ref: ProfileFrame['ref']
}

export const PERCEPTION_STORE_HZ = 4
export const CAPTURE_DEDUP_MS = 30_000
export const DETECTIONS_MAX = 200
export const CAPTURES_MAX = 32

export interface CaptureItem {
  seq: number
  tSimNs: number
  tWallMs: number
  uav: string
  sensor: string
  target: string
  level: string
}

export interface DetectionItem {
  seq: number
  tSimNs: number
  type: string
  uav: string
  sensor: string
  target: string | null
  levelFrom: string | null
  levelTo: string | null
  reason: string | null
  nEff: number | null
  nReq: number | null
}

export interface PerceptionState {
  uav: string | null
  frame: PerceptionFrame | null
  hud: HudInfo | null
  captured: { sensor: string; target: string }[]
  captures: CaptureItem[]
  detections: DetectionItem[]
  // D2.1
  fieldView: FieldView
  volumesMode: VolumesMode
  physicalRef: boolean
  deviceClass: DeviceClass
  legend: FieldLegend | null
  quant: QuantFrame | null
  fieldVersion: number
  version: number
}

export const perceptionStore = createAwrStore<PerceptionState>('perception', () => ({
  uav: null, frame: null, hud: null, captured: [], captures: [], detections: [], fieldView: 'none', volumesMode: 'off', physicalRef: false,
  deviceClass: 'desktop', legend: null, quant: null, fieldVersion: 0, version: 0,
}))

// ------------------------------------------------------------------------------------------------ pure helpers
const lastCapture = new Map<string, number>()

/** 30 s de-duplication of capture notices per target (wall clock); records the time when due */
export function captureDue(target: string, nowMs = Date.now(), windowMs = CAPTURE_DEDUP_MS): boolean {
  const at = lastCapture.get(target)
  if (at !== undefined && nowMs - at < windowMs) return false
  lastCapture.set(target, nowMs)
  return true
}

const s = (v: unknown, n = 48): string => (typeof v === 'string' ? sanitizeText(v, n) : '')
const nu = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** perception.* events -> detection items (pure, for tests) */
export function detectionOf(e: RtEvent): DetectionItem | null {
  if (!e.type.startsWith('perception.')) return null
  const d = e.data ?? {}
  return {
    seq: e.seq, tSimNs: e.t_sim_ns, type: e.type, uav: s(d.uav ?? e.uav), sensor: s(d.sensor), target: typeof d.target === 'string' ? s(d.target) : null,
    levelFrom: typeof d.level_from === 'string' ? s(d.level_from, 8) : null,
    levelTo: typeof d.level_to === 'string' ? s(d.level_to, 8) : typeof d.level === 'string' ? s(d.level, 8) : null,
    reason: typeof d.reason === 'string' ? s(d.reason, 16) : null, nEff: nu(d.n_eff), nReq: nu(d.n_req),
  }
}

// ------------------------------------------------------------------------------------------------ feed
let refs = 0
let offs: (() => void)[] = []
let follow: string | null = null
let sub: { uav: string; off: () => void } | null = null
let latest: PerceptionFrame | null = null
let pendingCaptures: CaptureItem[] = []
let pendingDetections: DetectionItem[] = []
let lastKey = ''
// D2.1 field feed
let fieldSubs = new Map<string, () => void>()
let profileAppliedMs = new Map<FieldKind, number>()
let quantLatest: QuantFrame | null = null
let fieldDirty = false
let nowMs: () => number = () => Date.now()

function followed(): string | null {
  return follow ?? selectionStore.getState().primary
}

function selectedTarget(): string | null {
  return targetsStore.getState().selected
}

function onFieldData(m: DataMsg): boolean {
  if (m.topic.startsWith('field/')) {
    const k = m.topic.slice(6)
    if (k === 'volumes') {
      const v = decodeFieldVolumes(m.data, fieldModel.volumes)
      if (v) {
        fieldModel.volumes = v
        fieldModel.version++
        fieldDirty = true
      }
      return true
    }
    if (!(FIELD_KINDS as readonly string[]).includes(k)) return true
    const kind = k as FieldKind
    const prev = fieldModel.profiles.get(kind) ?? null
    const f = decodeFieldProfile(m.data, prev)
    if (!f) return true
    const dc = perceptionStore.getState().deviceClass
    const last = profileAppliedMs.get(kind) ?? Number.NEGATIVE_INFINITY
    const now = nowMs()
    // phone: 0.5 Hz (keep every other frame unless the grid or the anchor set changed)
    const structural = !prev || prev.rM[N_LAST] !== f.rM[N_LAST] || prev.uavs.length !== f.uavs.length
    if (!structural && now - last < PROFILE_APPLY_MS[dc]) return true
    profileAppliedMs.set(kind, now)
    fieldModel.profiles.set(kind, f)
    fieldModel.version++
    fieldDirty = true
    return true
  }
  const q = /^target\/([^/]+)\/quant$/.exec(m.topic)
  if (q) {
    if (q[1] !== selectedTarget()) return true
    const fr = decodeTargetQuant(m.data)
    if (fr) {
      quantLatest = fr
      fieldModel.quant.set(fr.target, fr)
      fieldDirty = true
    }
    return true
  }
  return false
}
const N_LAST = 31

function onData(m: DataMsg): void {
  if (!worldLive()) return
  if (onFieldData(m)) return
  const mm = /^uav\/([^/]+)\/perception$/.exec(m.topic)
  if (!mm || mm[1] !== followed()) return
  const f = decodePerception(m.data)
  if (f) latest = f
}

/** wanted field subscriptions: topic -> rate */
export function fieldTopics(s: Pick<PerceptionState, 'fieldView' | 'volumesMode' | 'deviceClass'>, target: string | null): Map<string, number> {
  const out = new Map<string, number>()
  if (s.fieldView !== 'none') out.set(`field/${s.fieldView}`, FIELD_RATE)
  if (s.volumesMode !== 'off' || s.fieldView === 'acoustic' || s.fieldView === 'visual') out.set('field/volumes', FIELD_RATE)
  if (target) out.set(`target/${target}/quant`, QUANT_RATE[s.deviceClass])
  return out
}

function syncFieldSubs(rt: RtClient): void {
  const st = perceptionStore.getState()
  // another world's stream (or none): nothing is wanted, the server generates nothing for this page
  const want = worldLive() ? fieldTopics(st, selectedTarget()) : new Map<string, number>()
  for (const [topic, off] of [...fieldSubs]) {
    if (!want.has(topic)) {
      off()
      fieldSubs.delete(topic)
      if (topic.startsWith('field/') && topic !== 'field/volumes') fieldModel.profiles.delete(topic.slice(6) as FieldKind)
      if (topic === 'field/volumes') fieldModel.volumes = null
      if (topic.endsWith('/quant')) {
        quantLatest = null
        fieldDirty = true
      }
      fieldModel.version++
    }
  }
  for (const [topic, rate] of want) {
    if (!fieldSubs.has(topic)) fieldSubs.set(topic, rt.subscribe(topic, { rate: rate as 1 | 2 }))
  }
  fieldModel.kind = st.fieldView
  fieldModel.volumesMode = st.volumesMode
  fieldModel.physicalRef = st.physicalRef
  fieldModel.selectedTarget = selectedTarget()
}

function legendOf(view: FieldView): FieldLegend | null {
  if (view === 'none') return null
  const f = fieldModel.profiles.get(view)
  return f ? { kind: f.kind, unit: f.unit, bands: f.bands, iso: f.iso, shellRef: f.shellRef, ref: f.ref } : null
}

function onConn(s: ConnState): void {
  // a new connection (reconnect, power-save resume, sandbox resume with a new sim-core) restarts the revisions: drop the old
  // arrays so delta frames never reuse stale data; the first frames after subscribing are keyframes
  if (s === 'SYNCING' || s === 'CONNECTING') {
    fieldModel.reset()
    profileAppliedMs = new Map()
  }
}

function onEvents(batch: readonly RtEvent[]): void {
  if (!worldLive()) return
  for (const e of batch) {
    const d = detectionOf(e)
    if (!d) continue
    pendingDetections.push(d)
    if (e.type === 'perception.capture' && d.target && captureDue(d.target)) {
      pendingCaptures.push({ seq: e.seq, tSimNs: e.t_sim_ns, tWallMs: Date.now(), uav: d.uav, sensor: d.sensor, target: d.target, level: d.levelTo ?? '' })
    }
  }
}

function tick(_ctx: FrameCtx | null, rt: RtClient | null): void {
  const uav = worldLive() ? followed() : null
  if (rt && sub?.uav !== uav) {
    sub?.off()
    sub = uav ? { uav, off: rt.subscribe(`uav/${uav}/perception`, { rate: 5 }) } : null
    if (latest && latest.uav !== uav) latest = null
  }
  if (rt) syncFieldSubs(rt)
  const st = perceptionStore.getState()
  const target = selectedTarget()
  if (quantLatest && quantLatest.target !== target) quantLatest = null
  const frame = latest && latest.uav === uav ? latest : null
  const key = `${uav}|${frame?.t_sim_ns ?? ''}|${pendingCaptures.length}|${pendingDetections.length}`
  if (key === lastKey && st.uav === uav && !fieldDirty && st.quant === quantLatest) return
  const patch: Partial<PerceptionState> = {}
  if (key !== lastKey || st.uav !== uav) {
    lastKey = key
    patch.captures = pendingCaptures.length ? [...st.captures, ...pendingCaptures].slice(-CAPTURES_MAX) : st.captures
    patch.detections = pendingDetections.length ? [...st.detections, ...pendingDetections].slice(-DETECTIONS_MAX) : st.detections
    pendingCaptures = []
    pendingDetections = []
    Object.assign(patch, { uav, frame, hud: hudLine(frame), captured: capturedTargets(frame) })
  }
  if (fieldDirty || st.quant !== quantLatest) {
    fieldDirty = false
    Object.assign(patch, { quant: quantLatest, legend: legendOf(st.fieldView), fieldVersion: fieldModel.version })
  }
  perceptionStore.setState({ ...patch, version: st.version + 1 })
}

/** reference-counted feed (usePerception attaches it); returns the detach function */
export function attachPerceptionFeed(getRt: () => RtClient | null = rtClient): () => void {
  refs++
  if (refs === 1) {
    const rt = getRt()
    offs = [
      loop.register('overlay', 'm19.perception-store', (ctx) => tick(ctx, getRt()), { fps: PERCEPTION_STORE_HZ, order: 23, tiers: ['A', 'B'] }),
      loop.register('overlay', 'm19.perception-store.s', (ctx) => void (uiTickDue(ctx) && tick(ctx, getRt())), { order: 23, tiers: ['S'] }),
    ]
    if (rt) {
      offs.push(rt.onData(onData), rt.onEvents(onEvents))
      // test doubles of other stores may omit onConnState; the real client always has it
      if (typeof (rt as Partial<RtClient>).onConnState === 'function') offs.push(rt.onConnState(onConn))
    }
  }
  let done = false
  return () => {
    if (done) return
    done = true
    refs--
    if (refs === 0) {
      for (const off of offs) off()
      offs = []
      sub?.off()
      sub = null
      latest = null
      lastKey = ''
      pendingCaptures = []
      pendingDetections = []
      for (const off of fieldSubs.values()) off()
      fieldSubs = new Map()
      quantLatest = null
      fieldModel.reset()
    }
  }
}

/** test helpers: one feed tick without the frame loop; feed a data message or an event batch */
export function perceptionFeedTick(rt: RtClient | null): void {
  tick(null, rt)
}
export const perceptionFeedIngest = { data: onData, events: onEvents, conn: onConn }

/** world scope reset: frames, events, field samples, readings and the followed vehicle of the previous world go */
export function resetPerceptionWorld(): void {
  follow = null
  sub?.off()
  sub = null
  latest = null
  pendingCaptures = []
  pendingDetections = []
  for (const off of fieldSubs.values()) off()
  fieldSubs = new Map()
  profileAppliedMs = new Map()
  quantLatest = null
  lastCapture.clear()
  fieldModel.reset()
  fieldModel.selectedTarget = null
  lastKey = ''
  fieldDirty = false
  const st = perceptionStore.getState()
  perceptionStore.setState({ uav: null, frame: null, hud: null, captured: [], captures: [], detections: [], legend: null, quant: null,
    fieldVersion: fieldModel.version, version: st.version + 1 })
}
onWorldReset('perception', resetPerceptionWorld)
/** test helper: replace the wall clock of the phone decimation */
export function setPerceptionClock(fn: () => number): void {
  nowMs = fn
}

export const perceptionActions = {
  /** follow a vehicle explicitly (FPV, payload panel); null returns to the selection primary */
  follow(uav: string | null): void {
    follow = uav
  },
  clearDetections(): void {
    const st = perceptionStore.getState()
    perceptionStore.setState({ detections: [], captures: [], version: st.version + 1 })
  },
  /** test helper: forget the 30 s de-duplication */
  resetDedup(): void {
    lastCapture.clear()
  },
  /** field view of the viewport toolbar (Shift+V cycles; switching changes only instance data and uniforms, no program) */
  setFieldView(v: FieldView): void {
    const st = perceptionStore.getState()
    if (st.fieldView === v || !FIELD_VIEWS.includes(v)) return
    perceptionStore.setState({ fieldView: v, legend: legendOf(v), version: st.version + 1 })
    fieldModel.kind = v
    fieldModel.version++
  },
  cycleFieldView(): FieldView {
    const st = perceptionStore.getState()
    const next = FIELD_VIEWS[(FIELD_VIEWS.indexOf(st.fieldView) + 1) % FIELD_VIEWS.length]
    perceptionActions.setFieldView(next)
    return next
  },
  setVolumes(mode: VolumesMode): void {
    const st = perceptionStore.getState()
    if (st.volumesMode === mode) return
    perceptionStore.setState({ volumesMode: mode, version: st.version + 1 })
    fieldModel.volumesMode = mode
    fieldModel.version++
  },
  setPhysicalRef(on: boolean): void {
    const st = perceptionStore.getState()
    if (st.physicalRef === on) return
    perceptionStore.setState({ physicalRef: on, version: st.version + 1 })
    fieldModel.physicalRef = on
    fieldModel.version++
  },
  setDeviceClass(c: DeviceClass): void {
    const st = perceptionStore.getState()
    if (st.deviceClass === c) return
    perceptionStore.setState({ deviceClass: c, version: st.version + 1 })
    // the readout rate changes: resubscribe at the next tick
    for (const [topic, off] of [...fieldSubs]) {
      if (topic.endsWith('/quant')) {
        off()
        fieldSubs.delete(topic)
      }
    }
  },
}

/** readout row of one pair for the card (M15 formats the columns; numbers already rounded by the server) */
export function readoutRows(q: QuantFrame | null): { uav: string; rangeM: number; acoustic: string | null; visual: string | null; pAny: number; would: boolean }[] {
  if (!q) return []
  return q.pairs.map((p) => ({
    uav: p.uav, rangeM: p.range_m, pAny: p.p_any, would: p.would_discover,
    acoustic: p.acoustic ? `${p.acoustic.lp_band_db.toFixed(1)} / ${p.acoustic.thr_dba.toFixed(1)}` : null,
    visual: p.visual ? `${p.visual.theta_arcmin.toFixed(1)} / ${p.visual.theta_th_arcmin.toFixed(1)}` : null,
  }))
}

/** level history of a target from the recent perception.level events (P1 chart: t in s, level 0-3), oldest first */
export function levelHistory(s: Pick<PerceptionState, 'detections'>, target: string, uav?: string): { tS: number; level: number }[] {
  const idx: Record<string, number> = { none: 0, D: 1, R: 2, I: 3 }
  return s.detections
    .filter((d) => d.type === 'perception.level' && d.target === target && (uav === undefined || d.uav === uav))
    .map((d) => ({ tS: d.tSimNs / 1e9, level: idx[d.levelTo ?? 'none'] ?? 0 }))
}

/** React hook: selector over the perception store; mounting it attaches the feed */
export function usePerception<T>(sel: (s: PerceptionState) => T): T {
  useEffect(() => attachPerceptionFeed(), [])
  return useStore(perceptionStore, sel)
}
