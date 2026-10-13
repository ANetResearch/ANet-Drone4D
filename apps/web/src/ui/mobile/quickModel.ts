// Quick-command state machine (R-D2-37; AWR-04 §15.8.3 item 2; AWR-14 §20.26.5 table; M15-FR-232; UX-FR-154). Owner: M15.
// Pure: no React, stores or network. Three steps, each command in at most four taps:
//   CLOSED --open (a vehicle selected: skip SELECT)--> SELECT | ACTION
//   SELECT --vehicles chosen--> ACTION
//   ACTION --goto--> PICK_POINT --point--> CONFIRM
//   ACTION --hover | rtl--> CONFIRM
//   ACTION --watch--> PICK_TARGET --target--> CONFIRM
//   CONFIRM --confirm--> CLOSED (the command or the point_watch task is sent; a Toast says accepted or why not)
//   any --close (swipe down, back)--> CLOSED
export type QuickState = 'CLOSED' | 'SELECT' | 'ACTION' | 'PICK_POINT' | 'PICK_TARGET' | 'CONFIRM'
export type QuickOp = 'goto' | 'hover' | 'rtl' | 'watch'
export const QUICK_OPS: readonly QuickOp[] = ['goto', 'hover', 'rtl', 'watch']

export interface QuickModel {
  state: QuickState
  vids: string[]
  op: QuickOp | null
  point: [number, number, number] | null
  target: { id: string; label: string; pos: [number, number, number] } | null
  /** taps so far (D2-AC-48: <= 4 per command) */
  taps: number
}

export const QUICK_CLOSED: QuickModel = { state: 'CLOSED', vids: [], op: null, point: null, target: null, taps: 0 }

export type QuickEvent =
  | { type: 'open'; selected: readonly string[] }
  | { type: 'vehicles'; vids: readonly string[] }
  | { type: 'op'; op: QuickOp }
  | { type: 'point'; p: readonly [number, number, number] }
  | { type: 'target'; id: string; label: string; pos: readonly [number, number, number] }
  | { type: 'confirm' }
  | { type: 'back' }
  | { type: 'close' }

/** the next model (pure); invalid events leave the model unchanged */
export function quickStep(m: QuickModel, e: QuickEvent): QuickModel {
  const tap = m.taps + 1
  switch (e.type) {
    case 'open':
      if (m.state !== 'CLOSED') return m
      return e.selected.length ? { ...QUICK_CLOSED, state: 'ACTION', vids: [...e.selected], taps: 1 } : { ...QUICK_CLOSED, state: 'SELECT', taps: 1 }
    case 'vehicles':
      if (m.state !== 'SELECT' || e.vids.length === 0) return m
      return { ...m, state: 'ACTION', vids: [...e.vids], taps: tap }
    case 'op':
      if (m.state !== 'ACTION') return m
      if (e.op === 'goto') return { ...m, state: 'PICK_POINT', op: 'goto', taps: tap }
      if (e.op === 'watch') return { ...m, state: 'PICK_TARGET', op: 'watch', taps: tap }
      return { ...m, state: 'CONFIRM', op: e.op, taps: tap }
    case 'point':
      if (m.state !== 'PICK_POINT') return m
      return { ...m, state: 'CONFIRM', point: [e.p[0], e.p[1], e.p[2]], taps: tap }
    case 'target':
      if (m.state !== 'PICK_TARGET') return m
      return { ...m, state: 'CONFIRM', target: { id: e.id, label: e.label, pos: [e.pos[0], e.pos[1], e.pos[2]] }, taps: tap }
    case 'confirm':
      if (m.state !== 'CONFIRM') return m
      return { ...QUICK_CLOSED, taps: tap }
    case 'back':
      if (m.state === 'CONFIRM') return { ...m, state: m.op === 'goto' ? 'PICK_POINT' : m.op === 'watch' ? 'PICK_TARGET' : 'ACTION', point: null, target: null }
      if (m.state === 'PICK_POINT' || m.state === 'PICK_TARGET') return { ...m, state: 'ACTION', op: null }
      if (m.state === 'ACTION') return { ...m, state: 'SELECT', vids: [] }
      return QUICK_CLOSED
    case 'close':
      return QUICK_CLOSED
  }
}

/** goto altitude: the vehicle's current altitude when airborne (> 5 m above the point), else 60 m above the point */
export function gotoAltitude(groundZ: number, vehicleZ: number | null): number {
  if (vehicleZ !== null && Number.isFinite(vehicleZ) && vehicleZ - groundZ > 5) return vehicleZ
  return groundZ + 60
}
