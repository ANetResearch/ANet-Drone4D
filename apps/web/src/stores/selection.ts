// Selection (M15-FR-031, §7.1.2; AWR-14 §6.2; AWR-10 §6.4). Owner: M15. ids are ordered and unique (<= 1000); primary is
// the focused vehicle; hover is the pointer target. M06 viewport/bindings/selection.ts subscribes and calls
// drones.setHighlights (input to 3D highlight <= 2 frames). Writes are driven by user actions only.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { LIMITS } from '@/lib/tokens/input.gen'

export interface SelectionState {
  ids: readonly string[]
  primary: string | null
  hover: string | null
  version: number
}

export const selectionStore = createAwrStore<SelectionState>('selection', () => ({ ids: [], primary: null, hover: null, version: 0 }))

function commit(ids: string[], primary: string | null) {
  const s = selectionStore.getState()
  const capped = ids.length > LIMITS.selectionMax ? ids.slice(0, LIMITS.selectionMax) : ids
  const p = primary !== null && capped.includes(primary) ? primary : (capped[0] ?? null)
  selectionStore.setState({ ids: capped, primary: p, version: s.version + 1 })
}

export const selection = {
  select(ids: readonly string[], mode: 'replace' | 'add' | 'toggle' = 'replace'): void {
    const cur = selectionStore.getState()
    const first = ids[0] ?? null
    if (mode === 'replace') {
      commit([...new Set(ids)], first)
    } else if (mode === 'add') {
      const set = new Set(cur.ids)
      for (const id of ids) set.add(id)
      commit([...set], first ?? cur.primary)
    } else {
      const set = new Set(cur.ids)
      for (const id of ids) {
        if (set.has(id)) set.delete(id)
        else set.add(id)
      }
      const next = [...set]
      const primary = first !== null && set.has(first) ? first : cur.primary !== null && set.has(cur.primary) ? cur.primary : (next[0] ?? null)
      commit(next, primary)
    }
  },
  selectRange(fromId: string, toId: string, order: readonly string[]): void {
    const a = order.indexOf(fromId)
    const b = order.indexOf(toId)
    if (a < 0 || b < 0) return
    const [lo, hi] = a <= b ? [a, b] : [b, a]
    commit(order.slice(lo, hi + 1), toId)
  },
  clear(): void {
    const s = selectionStore.getState()
    if (s.ids.length === 0 && s.primary === null) return
    selectionStore.setState({ ids: [], primary: null, version: s.version + 1 })
  },
  setHover(id: string | null): void {
    if (selectionStore.getState().hover !== id) selectionStore.setState({ hover: id })
  },
  /** drop ids that are no longer valid (epoch change, vehicle removed) */
  prune(valid: (id: string) => boolean): void {
    const s = selectionStore.getState()
    const kept = s.ids.filter(valid)
    if (kept.length !== s.ids.length || (s.hover !== null && !valid(s.hover))) {
      selectionStore.setState({
        ids: kept, primary: s.primary !== null && valid(s.primary) ? s.primary : (kept[0] ?? null),
        hover: s.hover !== null && valid(s.hover) ? s.hover : null, version: s.version + 1,
      })
    }
  },
}

export function useSelection<T>(sel: (s: SelectionState) => T): T {
  return useStore(selectionStore, sel)
}
