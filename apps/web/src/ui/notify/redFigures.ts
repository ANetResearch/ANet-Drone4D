// One-red arbitration of the DOM figures and the viewport (M15-FR-091, §6.10; AWR-14 §11.2; AWR-15 §3.7.2): every
// INPUT.redEvalIntervalMs (250 ms, wall clock) the candidates of each figure are rebuilt and lib/redArbiter decides the
// owner with the 1.5 s dwell. Figures: `viewport` (critical vehicles, then the focused vehicle; result to M06
// drones.setRedOwner), `drone-rail` (critical rows only: list selection never uses red), `event-table` (the latest
// unacknowledged critical row), `header` (the alarm count badge while unacknowledged criticals exist). The store holds
// the owners and is written only when one changes; window.__ux.red mirrors it in dev and test builds.
// D2 (AWR-04 §10.7; AWR-14 §20.11.2; M15-FR-219): unconfirmed target discoveries are critical candidates of the viewport
// with rank 5 (the ELAND rank, the latest wins among equals); a target owner is published in the store for the targets
// layer of M18 (viewport/layers/targets.tsx reads redStore.owners.viewport when kind is 'target'), the drones layer then
// has no red owner.
import { useStore } from 'zustand'
import { SEVERITY } from '@awr/contracts/enums'
import { createAwrStore } from '@/lib/createStore'
import { arbitrate, RED_NONE, sameEntity, type RedCandidate, type RedEntity, type RedState } from '@/lib/redArbiter'
import { INPUT } from '@/lib/tokens/input.gen'
import { fleetIdOf, fleetRows } from '@/stores/fleet'
import { selectionStore } from '@/stores/selection'
import { drones } from '@/viewport/facade'
import { UX } from '@/ui/testing/uxProbe'
import { alarms, alarmsStore } from './alarms'
import { eventLog } from './eventLog'
import { unconfirmedDiscoveries } from './discoveries'
import { freezableInterval } from '@/lib/powerFreeze'

export const FIGURES = ['viewport', 'drone-rail', 'event-table', 'header'] as const
export type FigureId = (typeof FIGURES)[number]
export interface RedStoreState { owners: Readonly<Record<FigureId, RedEntity | null>>; version: number }
export const redStore = createAwrStore<RedStoreState>('ui.red', () => ({
  owners: { viewport: null, 'drone-rail': null, 'event-table': null, header: null }, version: 0,
}))
export const useRedOwner = (fig: FigureId): RedEntity | null => useStore(redStore, (s) => s.owners[fig])

const states: Record<FigureId, RedState> = { viewport: RED_NONE, 'drone-rail': RED_NONE, 'event-table': RED_NONE, header: RED_NONE }
/** wall ms since a vehicle is critical (the "latest" tie-break of AWR-15 §3.7.2) */
const critSince = new Map<string, number>()
let selectedSince = 0
let selectedId: string | null = null

/** critical vehicles that are not acknowledged, from the fleet rows (FlightState severity >= 5) */
function criticalCandidates(now: number, out: RedCandidate[]): void {
  const seen = new Set<string>()
  for (let i = 0; i < fleetRows.n; i++) {
    const rank = SEVERITY[fleetRows.fs[i]] ?? 0
    if (rank < 5) continue
    const id = fleetIdOf(i)
    seen.add(id)
    let since = critSince.get(id)
    if (since === undefined) {
      since = now
      critSince.set(id, since)
    }
    const a = alarms.ofVehicle(id)
    if (a?.acked) continue
    out.push({ entity: { kind: 'drone', id }, level: 'critical', rank, tLastWallMs: since })
  }
  for (const id of critSince.keys()) if (!seen.has(id)) critSince.delete(id)
}

/** one evaluation of every figure (exported for tests) */
export function evaluateRed(now = Date.now()): void {
  const crit: RedCandidate[] = []
  criticalCandidates(now, crit)
  for (const [id, at] of unconfirmedDiscoveries()) crit.push({ entity: { kind: 'target', id }, level: 'critical', rank: 5, tLastWallMs: at })
  const primary = selectionStore.getState().primary
  if (primary !== selectedId) {
    selectedId = primary
    selectedSince = now
  }
  const vp: RedCandidate[] = primary ? [...crit, { entity: { kind: 'drone', id: primary }, level: 'selected', rank: 0, tLastWallMs: selectedSince }] : crit
  const droneCrit = crit.filter((c) => c.entity.kind === 'drone')
  const table: RedCandidate[] = []
  for (let k = 0; k < eventLog.len && table.length === 0; k++) {
    const slot = eventLog.slotOfNewest(k)
    if (eventLog.sev[slot] !== 2) continue
    const e = eventLog.get(slot)
    if (!e) continue
    const a = e.uav ? alarms.ofVehicle(e.uav) : undefined
    if (a?.acked) continue
    table.push({ entity: { kind: 'row', id: String(e.seq) }, level: 'critical', rank: eventLog.rank[slot], tLastWallMs: eventLog.tWallMs[slot] })
  }
  const header: RedCandidate[] = alarmsStore.getState().critUnacked > 0 ? [{ entity: { kind: 'cell', id: 'alarm-count' }, level: 'critical', rank: 1, tLastWallMs: 0 }] : []
  const next = {
    viewport: arbitrate(states.viewport, vp, now),
    'drone-rail': arbitrate(states['drone-rail'], droneCrit, now),
    'event-table': arbitrate(states['event-table'], table, now),
    header: arbitrate(states.header, header, now),
  }
  let changed = false
  for (const f of FIGURES) {
    if (!sameEntity(next[f].owner, states[f].owner) && (next[f].owner !== null || states[f].owner !== null)) changed = true
    states[f] = next[f]
  }
  if (!changed) return
  const owners = { viewport: next.viewport.owner, 'drone-rail': next['drone-rail'].owner, 'event-table': next['event-table'].owner, header: next.header.owner }
  redStore.setState({ owners, version: redStore.getState().version + 1 })
  const o = next.viewport.owner
  drones.setRedOwner(o && o.kind === 'drone' ? { kind: 'drone', id: o.id } : null)
  for (const f of FIGURES) UX.red[f] = owners[f] ? { kind: owners[f].kind, id: owners[f].id } : null
}

let stopTimer: (() => void) | null = null
export function installRedFigures(): () => void {
  // not while the power-save mode froze the page (D2-AC-44)
  if (!stopTimer) stopTimer = freezableInterval(() => evaluateRed(), INPUT.redEvalIntervalMs)
  return () => {
    stopTimer?.()
    stopTimer = null
  }
}
