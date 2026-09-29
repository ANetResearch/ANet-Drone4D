// Command senders shared by buttons, hotkeys, menus and the command palette (M15-FR-027, FR-040, FR-041; AWR-14 §6.11,
// §6.19): single-vehicle commands of the primary selection go through the M06 facade (mission.command, mission.sendGoto)
// so the viewport marker and the test hooks see them; other vehicles and ops use RtClient.call directly
// (`uav/{id}/cmd/{op}`); a selection of two or more sends one batch call `fleet/cmd/{op}` ("*" when the selection is the
// whole unfiltered fleet). Environment writes are `env/preset` and `env/set` calls (AWR-14 §6.14); seat release is
// `seat/release`. Every call is tracked by ui/actions/commands.ts (button state, toasts).
import { t } from '@/app/i18n'
import { rtClient, type BatchOp, type CallHandle } from '@/net/rt'
import { selectionStore } from '@/stores/selection'
import { mission, type VehicleCmd } from '@/viewport/facade'
import { opText } from '@/ui/notify/severity'
import { trackBatch, trackCall } from './commands'

const FACADE_OPS = new Set<string>(['takeoff', 'hover', 'land', 'rtl'])
export const BATCH_OPS = new Set(['rtl', 'land', 'hover', 'safety_stop', 'pause', 'resume', 'takeoff'])

export const cmdKeyOf = (id: string, op: string): string => `${id}:${op}`
const label = (id: string, op: string): string => `${id} ${opText(op)}`

/** single-vehicle command; returns the call handle or null when it could not be sent */
export function runVehicleCmd(op: string, id: string, args: Record<string, unknown> = {}, o: { toastSuccess?: boolean } = {}): CallHandle | null {
  const rt = rtClient()
  let h: CallHandle | null = null
  if (FACADE_OPS.has(op) && selectionStore.getState().primary === id) h = mission.command(op as VehicleCmd, args)
  else if (rt) h = rt.call(`uav/${id}/cmd/${op}`, args)
  trackCall(cmdKeyOf(id, op), op, h, { label: label(id, op), toastSuccess: o.toastSuccess })
  return h
}

/** GoTo of the primary selection to the viewport's ground pick (the marker follows the call) */
export function runGoto(id: string, o: { speedMps?: number; toastSuccess?: boolean } = {}): CallHandle | null {
  const req = mission.sendGoto(o.speedMps !== undefined ? { speedMps: o.speedMps } : undefined)
  const h = req?.handle ?? null
  trackCall(cmdKeyOf(id, 'goto'), 'goto', h, { label: label(id, 'goto'), toastSuccess: o.toastSuccess })
  return h
}

/** batch command for a selection of two or more; `all` when the selection is the whole unfiltered fleet */
export function runBatch(op: string, ids: readonly string[], all: boolean, args: Record<string, unknown> = {}): CallHandle | null {
  const rt = rtClient()
  const h = rt ? rt.callBatch(op as BatchOp, all ? '*' : ids, args) : null
  trackBatch(`fleet:${op}`, op, ids.length, h, t('batch.label', { what: opText(op), n: ids.length }))
  return h
}

/** the selection command: batch for >= 2 vehicles, single otherwise */
export function runForSelection(op: string, args: Record<string, unknown> = {}, o: { all?: boolean; toastSuccess?: boolean } = {}): CallHandle | null {
  const { ids, primary } = selectionStore.getState()
  if (ids.length >= 2 && BATCH_OPS.has(op)) return runBatch(op, ids, o.all ?? false, args)
  const id = primary ?? ids[0]
  return id ? runVehicleCmd(op, id, args, { toastSuccess: o.toastSuccess }) : null
}

/** generic service call with tracking (env, seat, mission) */
export function runService(key: string, service: string, args: Record<string, unknown>, what: string, o: { toastSuccess?: boolean } = {}): CallHandle | null {
  const rt = rtClient()
  const h = rt ? rt.call(service, args) : null
  trackCall(key, service, h, { label: what, toastSuccess: o.toastSuccess })
  return h
}
