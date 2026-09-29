// DroneRail ordering and filtering (M15-FR-024; AWR-14 §4.4, §6.2): search by id prefix then substring, filter by state
// group, owner and alerts, sort by agent number, severity or battery, all computed on the typed rows of stores/fleet
// (M11) into a Uint16Array permutation (N <= 1000, well under 0.5 ms per pass). The filter state is a small UI store so
// Mod+A (select the filtered fleet) and . / , (next / previous vehicle) use the same order as the list.
import { useStore } from 'zustand'
import { SEVERITY } from '@awr/contracts/enums'
import { createAwrStore } from '@/lib/createStore'
import { fleetIdOf, fleetRows } from '@/stores/fleet'

export type StateGroup = 'all' | 'air' | 'ground' | 'alert'
export type RailSort = 'id' | 'severity' | 'battery'
export interface RailFilter { query: string; group: StateGroup; owner: number; sort: RailSort }
export const railStore = createAwrStore<RailFilter>('ui.rail', () => ({ query: '', group: 'all', owner: -1, sort: 'id' }))
export const useRail = <T,>(sel: (s: RailFilter) => T): T => useStore(railStore, sel)
export const railFilterActive = (f: RailFilter): boolean => f.query !== '' || f.group !== 'all' || f.owner >= 0

/** FlightState groups: air = TAKING_OFF .. LANDING, ELAND, FAILSAFE; ground = the rest */
const AIR = new Uint8Array(16)
for (const s of [4, 5, 6, 7, 8, 9, 10, 11]) AIR[s] = 1

export interface RowSource {
  n: number
  agentNo: ArrayLike<number>
  fs: ArrayLike<number>
  battery: ArrayLike<number>
  owner: ArrayLike<number>
  alert: ArrayLike<number>
}

let scratch = new Uint16Array(1024)
const key = new Float64Array(1024)

/** permutation of row indices after filtering and sorting (pure over its inputs; exported for tests and benches) */
export function buildOrder(rows: RowSource, f: RailFilter, idOf: (i: number) => string): Uint16Array {
  const n = rows.n
  if (scratch.length < n) scratch = new Uint16Array(n)
  const q = f.query.trim().toLowerCase()
  let k = 0
  for (let i = 0; i < n; i++) {
    const fs = rows.fs[i]
    if (f.group === 'air' && !AIR[fs]) continue
    if (f.group === 'ground' && AIR[fs]) continue
    if (f.group === 'alert' && !rows.alert[i]) continue
    if (f.owner >= 0 && rows.owner[i] !== f.owner) continue
    if (q) {
      const id = idOf(i).toLowerCase()
      const at = id.indexOf(q)
      if (at < 0) continue
      // prefix matches sort before substring matches
      key[i] = at === 0 ? 0 : 1
    } else key[i] = 0
    scratch[k++] = i
  }
  const out = scratch.slice(0, k)
  const sev = (i: number) => SEVERITY[rows.fs[i]] ?? 0
  const bat = (i: number) => (rows.battery[i] === 255 ? 256 : rows.battery[i])
  out.sort((a, b) => key[a] - key[b]
    || (f.sort === 'severity' ? sev(b) - sev(a) || rows.alert[b] - rows.alert[a] : f.sort === 'battery' ? bat(a) - bat(b) : 0)
    || rows.agentNo[a] - rows.agentNo[b])
  return out
}

/** ids of the filtered list in display order (Mod+A, . and ,) */
export function railIds(): string[] {
  const order = buildOrder(fleetRows, railStore.getState(), fleetIdOf)
  const ids = new Array<string>(order.length)
  for (let i = 0; i < order.length; i++) ids[i] = fleetIdOf(order[i])
  return ids
}

/** next or previous vehicle of the list after `current` (wraps; the first when nothing is focused) */
export function stepId(ids: readonly string[], current: string | null, dir: 1 | -1): string | null {
  if (!ids.length) return null
  const i = current === null ? -1 : ids.indexOf(current)
  if (i < 0) return dir > 0 ? ids[0] : ids[ids.length - 1]
  return ids[(i + dir + ids.length) % ids.length]
}
