// stores/selection -> drone highlights and the 60 Hz state channel of the selected vehicles (M06 §7.1
// drones.setHighlights; AWR-03 §8.5; AWR-17 §6.6). Owner: M06. Selection to 3D highlight <= 2 frames: the highlight is
// applied synchronously on the store change and read by the next drones phase. Up to 8 selected vehicles (the primary
// first) get uav/<id>/state@60; the roster may arrive after the selection, so unresolved ids are retried at 4 Hz.
import { register } from '@/engine'
import { rtClient } from '@/net/rt'
import { selectionStore } from '@/stores/selection'
import { vp } from '../session'

export const SELECTION = { rateHz: 60, maxSubscribed: 8 } as const

/** agent number of a roster id (numeric ids before the roster) or -1 */
export function agentNoOf(id: string | null): number {
  if (id === null) return -1
  const no = rtClient()?.roster.agentNoOf(id) ?? -1
  if (no >= 0) return no
  return /^\d+$/.test(id) ? Number(id) : -1
}

export function installSelectionBinding(): () => void {
  const subs = new Map<string, () => void>()
  let unresolved = false
  const apply = (): void => {
    const s = selectionStore.getState()
    const agents: number[] = []
    unresolved = false
    for (const id of s.ids) {
      const a = agentNoOf(id)
      if (a >= 0) agents.push(a)
      else unresolved = true
    }
    const primary = agentNoOf(s.primary)
    const hover = agentNoOf(s.hover)
    vp.drones?.layer.setHighlights(agents, primary, hover)
    // 60 Hz channel of the selected vehicles (primary first)
    const rt = rtClient()
    const want = new Set<string>()
    const order = s.primary !== null ? [s.primary, ...s.ids.filter((x) => x !== s.primary)] : [...s.ids]
    for (const id of order) {
      if (want.size >= SELECTION.maxSubscribed) break
      const a = agentNoOf(id)
      const rid = a >= 0 ? rt?.roster.idOf(a) : undefined
      if (rid) want.add(rid)
    }
    for (const [id, off] of subs) {
      if (!want.has(id)) {
        off()
        subs.delete(id)
      }
    }
    if (rt) for (const id of want) if (!subs.has(id)) subs.set(id, rt.subscribe(`uav/${id}/state`, { rate: SELECTION.rateHz }))
    // the focus vehicle of third/fpv stays fixed while the mode runs; otherwise it follows the primary selection
    if (vp.rig && vp.rig.mode !== 'third' && vp.rig.mode !== 'fpv') vp.rig.focusAgent = primary
  }
  apply()
  const offStore = selectionStore.subscribe(apply)
  const offVp = vp.subscribe(() => {
    if (vp.drones && vp.drones.layer.selected.length === 0 && selectionStore.getState().ids.length > 0) apply()
  })
  const offRetry = register('overlay', 'selection.retry', () => {
    if (unresolved || (selectionStore.getState().ids.length > 0 && subs.size === 0)) apply()
  }, { fps: 4 })
  return () => {
    offStore()
    offVp()
    offRetry()
    for (const off of subs.values()) off()
    subs.clear()
  }
}
