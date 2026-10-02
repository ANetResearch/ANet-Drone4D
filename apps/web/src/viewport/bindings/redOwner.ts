// Viewport RedArbiter (ADR-032; AWR-15 §3.7; M06-FR-037, AC-028). Owner: M06.
// Every input.redEvalIntervalMs (250 ms, wall clock) the viewport's candidates are arbitrated with the M15 pure function
// lib/redArbiter.ts: unacknowledged critical alarms (stores/safety, ranked) and critical vehicles from telemetry, the
// focused vehicle (primary selection), and data heroes: the point-cloud hero class (class 11 power lines, drawn red by
// M05 in the Class colour mode while it owns the red; FX-WEB1) and S3 targets (ext). The owner goes to the drone layer (marker,
// rings, trail, label ring, P600 hull), to the zones (fence violation) and to the GoTo marker (red failure only when the
// red is free). An explicit facade override (drones.setRedOwner) wins until it is cleared with null.
import { activePointCloud, MARK } from '@/engine'
import { arbitrate, RED_NONE, type RedCandidate, type RedEntity, type RedState } from '@/lib/redArbiter'
import { INPUT } from '@/lib/tokens/input.gen'
import { rtClient } from '@/net/rt'
import { layersStore } from '@/stores/layers'
import { safetyStore } from '@/stores/safety'
import { selectionStore } from '@/stores/selection'
import { vp } from '../session'
import { agentNoOf } from './selection'

/** point-cloud hero class (anet-classes@1 class 11, power lines; M05 PC.heroClass) */
const HERO_CLASS = 11
let state: RedState = RED_NONE
let override: RedEntity | null | undefined
const since = new Map<string, number>()

export function redOwner(): RedEntity | null {
  return override !== undefined ? override : state.owner
}
/** facade: explicit owner (null clears the override and returns to the arbiter) */
export function setRedOverride(o: RedEntity | null): void {
  override = o === null ? undefined : o
  applyOwner()
}

function applyOwner(): void {
  const o = redOwner()
  const drones = vp.drones
  if (drones) drones.layer.setRedOwner(o && o.kind === 'drone' ? agentNoOf(o.id) : -1)
  vp.zones?.setHero(o && o.kind === 'zone' ? o.id : null)
  // the point-cloud class hero (power lines, class 11) yields to any other red owner of the viewport (M05 request 9)
  activePointCloud()?.setHeroClassActive(o !== null && o.kind !== 'class')
}

export function evaluateRed(nowMs: number): RedState {
  const cands: RedCandidate[] = []
  const rt = rtClient()
  for (const a of safetyStore.getState().alarms) {
    if (!a.active || a.severity !== 'critical' || !a.vehicleId) continue
    cands.push({ entity: { kind: 'drone', id: a.vehicleId }, level: 'critical', rank: a.rank, tLastWallMs: a.tWallMs })
  }
  const d = vp.drones
  if (d) {
    const p = d.poses
    for (let i = 0; i < p.n; i++) {
      const no = p.agentNo[i]
      if (d.layer.alert[no] !== 2 || (d.layer.mark[no] & MARK.HIDDEN) !== 0) continue
      const id = rt?.roster.idOf(no) ?? String(no)
      if (cands.some((c) => c.entity.id === id)) continue
      const key = `c:${id}`
      if (!since.has(key)) since.set(key, nowMs)
      cands.push({ entity: { kind: 'drone', id }, level: 'critical', rank: 0, tLastWallMs: since.get(key)! })
    }
  }
  const primary = selectionStore.getState().primary
  if (primary !== null) {
    const key = `s:${primary}`
    if (!since.has(key)) since.set(key, nowMs)
    cands.push({ entity: { kind: 'drone', id: primary }, level: 'selected', rank: 0, tLastWallMs: since.get(key)! })
  }
  // data hero: the point-cloud hero class, while it is on screen in the Class colour mode (AWR-15 §3.7; M05-FR-032)
  const L = layersStore.getState()
  if (L.visible.pointcloud && L.colorMode === 'class' && (L.classMask & (1 << HERO_CLASS)) !== 0 && activePointCloud()) {
    const key = `h:class-${HERO_CLASS}`
    if (!since.has(key)) since.set(key, nowMs)
    cands.push({ entity: { kind: 'class', id: String(HERO_CLASS) }, level: 'hero', rank: 0, tLastWallMs: since.get(key)! })
  }
  const keyOf = (c: RedCandidate): string => `${c.level === 'selected' ? 's' : c.level === 'hero' ? 'h' : 'c'}:${c.level === 'hero' ? `class-${c.entity.id}` : c.entity.id}`
  for (const k of [...since.keys()]) if (!cands.some((c) => keyOf(c) === k)) since.delete(k)
  state = arbitrate(state, cands, nowMs)
  applyOwner()
  return state
}

export function installRedBinding(): () => void {
  const t = setInterval(() => evaluateRed(performance.now()), INPUT.redEvalIntervalMs)
  const off = selectionStore.subscribe(() => evaluateRed(performance.now()))
  const offLayers = layersStore.subscribe((s, prev) => {
    if (s.colorMode !== prev.colorMode || s.classMask !== prev.classMask || s.visible.pointcloud !== prev.visible.pointcloud) evaluateRed(performance.now())
  })
  return () => {
    clearInterval(t)
    off()
    offLayers()
  }
}
