// Targets store (M18-FR-019, FR-022, FR-024; M18-M19 PRD §8.1, §8.4; AWR-03 §4.3: owner M18, M15 writes the panel JSX).
// Fields (read by the target panel, the viewport layer and the manual-control HUD):
//   items[]       one row per target in target_no order: id, label, cls, aware (UNAWARE ... CALMING), alertness,
//                 discovered (count), level (best perception level none | D | R | I), captured, physical (criterion mode),
//                 enabled, moving, occupied, keepoutRM, pos (ENU m)
//   count, max    "识别物 n / 30"
//   selected      the focused target (its detail is subscribed at 2 Hz) and `detail` (awr.target.detail.v1)
//   bodyView      viewport sensitivity display: configured | effective (per modelId) | planning (keepout, full circles)
//   modelId       model for the effective view (the selected vehicle's model, else the form's model drop-down)
//   placing       class being placed (cross cursor; Esc leaves), null otherwise
//   alarms[]      unacknowledged target.discovered (critical red candidates until acknowledged, §8.4)
// Writers: a reference-counted feed (useTargets attaches it while mounted) subscribes `swarm/target/state` at 10 Hz and
// `target/*/detail` at 1 Hz into engine/targets targetsModel (no store write per message) and mirrors the summary at the
// shared UI tick (<= 4 writes/s on Tier S, ADR-066) only when it changed. Actions send WS calls (target/add, update,
// remove; seat required, 5 per second per sandbox) and never write the summary directly. Text from the wire passes
// lib/sanitize.ts.
// D2.2-W1: the targets belong to the world of the realtime source (stores/worldScope): rows, details and discoveries are
// ingested only while that world is the one the view shows, and a world or source switch clears the model and the summary
// (selection, detail, placement preview and alarms included).
import { useEffect } from 'react'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { loop, type FrameCtx } from '@/engine/loop'
import {
  AWARENESS, CLASS_OF_CODE, LEVELS, targetsModel, type AwarenessName, type BodyView, type LevelName, type TargetCfg, type TargetClassName,
  type TargetDetail,
} from '@/engine/targets'
import { rtClient, type CallResult, type DataMsg, type RtClient, type RtEvent } from '@/net/rt'
import { uiTickDue } from './uiTick'
import { onWorldReset, worldLive } from './worldScope'

export const TARGETS_MAX = 30
export const TARGETS_STORE_HZ = 4

export interface TargetItem {
  id: string
  label: string
  cls: TargetClassName | null
  aware: AwarenessName
  alertness: number
  discovered: number
  level: LevelName
  captured: boolean
  physical: boolean
  enabled: boolean
  moving: boolean
  occupied: boolean
  keepoutRM: number
  pos: [number, number, number]
}

export interface DiscoveredAlarm {
  targetId: string
  uav: string
  kind: string
  tWallMs: number
  seq: number
}

export interface TargetsState {
  items: TargetItem[]
  count: number
  max: number
  selected: string | null
  detail: TargetDetail | null
  bodyView: BodyView
  modelId: string | null
  placing: TargetClassName | null
  alarms: DiscoveredAlarm[]
  version: number
}

export const targetsStore = createAwrStore<TargetsState>('targets', () => ({
  items: [], count: 0, max: TARGETS_MAX, selected: null, detail: null, bodyView: 'configured', modelId: null, placing: null, alarms: [],
  version: 0,
}))

// ------------------------------------------------------------------------------------------------ feed
let refs = 0
let offs: (() => void)[] = []
let detailSub: { id: string; off: () => void } | null = null
let lastKey = ''
/** the rt client getter of the attached feed (actions use the same client; tests inject a double) */
let rtGetter: () => RtClient | null = rtClient

function onData(m: DataMsg): void {
  if (worldLive()) targetsModel.ingest(m)
}

function onEvents(batch: readonly RtEvent[]): void {
  if (!worldLive()) return
  let add: DiscoveredAlarm[] | null = null
  for (const e of batch) {
    if (e.type !== 'target.discovered') continue
    const d = e.data as { target?: string; uav?: string; kind?: string }
    if (!d.target) continue
    ;(add ??= []).push({ targetId: sanitizeText(String(d.target)), uav: sanitizeText(String(d.uav ?? '')), kind: String(d.kind ?? ''),
      tWallMs: Date.now(), seq: e.seq })
  }
  if (add) {
    const s = targetsStore.getState()
    targetsStore.setState({ alarms: [...s.alarms, ...add].slice(-64), version: s.version + 1 })
  }
}

/** UI summary of the model (pure: also used by tests) */
export function summarize(): Pick<TargetsState, 'items' | 'count' | 'detail'> {
  const R = targetsModel.rows
  const items: TargetItem[] = []
  for (let i = 0; i < R.n; i++) {
    const id = targetsModel.idOfRow(i)
    const det = targetsModel.details.get(id)
    const cfg = targetsModel.cfgs.get(id)
    const label = sanitizeText(String(cfg?.label ?? det?.label ?? id)).slice(0, 32)
    items.push({
      id, label, cls: CLASS_OF_CODE[R.cls[i]] ?? null, aware: AWARENESS[R.aware[i]] ?? 'UNAWARE', alertness: Math.round(R.alertness[i] * 100) / 100,
      discovered: R.discovered[i], level: LEVELS[R.level[i]] ?? 'none', captured: R.captured[i] === 1, physical: R.physical[i] === 1,
      enabled: R.enabled[i] === 1, moving: R.moving[i] === 1, occupied: R.occupied[i] === 1, keepoutRM: Math.round(R.keepoutR[i] * 10) / 10,
      pos: [Math.round(R.pos[3 * i] * 10) / 10, Math.round(R.pos[3 * i + 1] * 10) / 10, Math.round(R.pos[3 * i + 2] * 10) / 10],
    })
  }
  const sel = targetsStore.getState().selected
  return { items, count: items.length, detail: sel ? (targetsModel.details.get(sel) ?? null) : null }
}

function tick(_ctx: FrameCtx | null, rt: RtClient | null): void {
  if (rt) {
    targetsModel.resolver = rt.roster
    const sel = targetsStore.getState().selected
    if (detailSub?.id !== sel) {
      detailSub?.off()
      detailSub = sel ? { id: sel, off: rt.subscribe(`target/${sel}/detail`, { rate: 2 }) } : null
    }
  }
  const patch = summarize()
  const key = `${targetsModel.version}|${targetsModel.detailVersion}|${JSON.stringify(patch.items)}|${patch.detail?.t_sim_ns ?? ''}`
  if (key === lastKey) return
  lastKey = key
  targetsStore.setState({ ...patch, version: targetsStore.getState().version + 1 })
}

/** reference-counted feed (useTargets and the viewport layer attach it); returns the detach function */
export function attachTargetsFeed(getRt: () => RtClient | null = rtClient): () => void {
  refs++
  if (refs === 1) {
    rtGetter = getRt
    const rt = getRt()
    offs = [
      loop.register('overlay', 'm18.targets-store', (ctx) => tick(ctx, getRt()), { fps: TARGETS_STORE_HZ, order: 22, tiers: ['A', 'B'] }),
      loop.register('overlay', 'm18.targets-store.s', (ctx) => void (uiTickDue(ctx) && tick(ctx, getRt())), { order: 22, tiers: ['S'] }),
    ]
    if (rt) {
      targetsModel.resolver = rt.roster
      offs.push(rt.onData(onData), rt.onEvents(onEvents), rt.subscribe('swarm/target/state', { rate: 10 }),
        rt.subscribe('target/*/detail', { rate: 1 }))
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
      detailSub?.off()
      detailSub = null
      lastKey = ''
      rtGetter = rtClient
    }
  }
}

/** test helper: one feed tick without the frame loop */
export function targetsFeedTick(rt: RtClient | null): void {
  tick(null, rt)
}

/** world scope reset: the model and the summary of the previous world go (the view options bodyView and modelId stay) */
export function resetTargetsWorld(): void {
  targetsModel.reset()
  detailSub?.off()
  detailSub = null
  lastKey = ''
  const s = targetsStore.getState()
  targetsStore.setState({ items: [], count: 0, selected: null, detail: null, placing: null, alarms: [], version: s.version + 1 })
}
onWorldReset('targets', resetTargetsWorld)

// ------------------------------------------------------------------------------------------------ actions
export interface TargetCallOutcome {
  ok: boolean
  id?: string
  code: number
  /** field-level detail of 110, 540, 541, 542, 544 (form errors) */
  detail?: Record<string, unknown>
  warnings?: string[]
}

function outcome(r: CallResult, id?: string): TargetCallOutcome {
  const ok = r.status === 'succeeded' || r.status === 'accepted' || r.status === 'running'
  const data = (r.data ?? {}) as { result?: { id?: string; cfg?: TargetCfg }; id?: string; cfg?: TargetCfg }
  const res = data.result ?? data
  if (ok && res.cfg && typeof res.cfg === 'object') targetsModel.setConfig(res.cfg)
  return { ok, id: res.id ?? id, code: r.code, detail: (r.detail && typeof r.detail === 'object' ? r.detail : undefined) as Record<string, unknown> | undefined,
    warnings: r.warnings }
}

function call(service: string, args: object, id?: string): Promise<TargetCallOutcome> {
  const rt = rtGetter()
  if (!rt) return Promise.resolve({ ok: false, code: 211 })
  return rt.call(service, args).result.then((r) => outcome(r, id))
}

export const targetsActions = {
  /** place a target of a class at a ground point (z null: the server snaps to the DSM); extra fields from the form */
  add(cls: TargetClassName, pos: readonly [number, number, number | null], extra: Partial<TargetCfg> = {}): Promise<TargetCallOutcome> {
    return call('target/add', { target: { ...extra, class: cls, pos: [...pos] } })
  },
  /** JSON Merge Patch (arrays replace); one call per drag release (5 per second limit) */
  update(id: string, patch: Partial<TargetCfg>): Promise<TargetCallOutcome> {
    return call('target/update', { id, patch }, id)
  },
  remove(id: string): Promise<TargetCallOutcome> {
    return call('target/remove', { id }, id).then((o) => {
      if (o.ok) targetsModel.remove(id)
      return o
    })
  },
  select(id: string | null): void {
    const s = targetsStore.getState()
    if (s.selected === id) return
    targetsStore.setState({ selected: id, detail: id ? (targetsModel.details.get(id) ?? null) : null, version: s.version + 1 })
  },
  setBodyView(bodyView: BodyView, modelId?: string | null): void {
    const s = targetsStore.getState()
    targetsStore.setState({ bodyView, modelId: modelId === undefined ? s.modelId : modelId, version: s.version + 1 })
  },
  setPlacing(placing: TargetClassName | null): void {
    const s = targetsStore.getState()
    if (s.placing !== placing) targetsStore.setState({ placing, version: s.version + 1 })
  },
  /** "确认" on the discovered toast: the target leaves the critical red candidates */
  acknowledge(targetId: string): void {
    const s = targetsStore.getState()
    targetsStore.setState({ alarms: s.alarms.filter((a) => a.targetId !== targetId), version: s.version + 1 })
  },
}

/** critical red candidate of the viewport RedArbiter (latest unacknowledged discovery), or null (M06 redOwner reads it) */
export function targetRedCandidate(): { id: string; tLastWallMs: number } | null {
  const a = targetsStore.getState().alarms
  const last = a[a.length - 1]
  return last ? { id: last.targetId, tLastWallMs: last.tWallMs } : null
}

/** manual-control warning (M18-FR-022): the target body the commanded velocity enters within 3 s (no request sent); the
 *  manual controller looks a little further (ui/manual/controller.ts PREDICT_LEAD_S, D2-AC-21) */
export function manualKeepoutWarning(p: readonly [number, number, number], v: readonly [number, number, number], horizonS = 3) {
  return targetsModel.manualWarning(p, v, targetsStore.getState().modelId, horizonS)
}

/** React hook: selector over the targets store; mounting it attaches the feed */
export function useTargets<T>(sel: (s: TargetsState) => T): T {
  useEffect(() => attachTargetsFeed(), [])
  return useStore(targetsStore, sel)
}
