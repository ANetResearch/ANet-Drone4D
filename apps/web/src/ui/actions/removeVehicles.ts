// Remove virtual P600 (M15-FR-027; AWR-14 §6.13 item 5; D1-AC-32): confirmation first (grounded vehicle: "Remove
// P600-03?"; airborne: it lands first, then leaves the simulation), then DELETE /api/fleet/vehicles/{id} per vehicle.
// 202 shows "removing" (lifecycle DRAINING); the row leaves when the roster drops it. 105 STATE on an airborne vehicle
// (17 and 12 still differ, AWR-14 §19 item 16) asks to land first.
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { ApiError } from '@/net/api'
import { FlightState } from '@awr/contracts/enums'
import { vehicleState } from './admission'
import { confirmThen } from './ConfirmHost'
import { deleteVehicle } from './fleetRest'

const GROUNDED = new Set<number>([FlightState.UNKNOWN, FlightState.DISARMED, FlightState.PREFLIGHT, FlightState.READY, FlightState.LANDED, FlightState.CRASHED])

export function removeVehicles(ids: readonly string[]): void {
  if (!ids.length) return
  const airborne = ids.filter((id) => {
    const v = vehicleState(id)
    return v !== null && !GROUNDED.has(v.fs)
  })
  const one = ids.length === 1
  confirmThen({
    id: 'vehicle.remove',
    title: one ? t('remove.title', { id: ids[0] }) : t('remove.titleN', { n: ids.length }),
    body: airborne.length ? (one ? t('remove.airborne', { id: ids[0] }) : t('remove.airborneN', { n: airborne.length })) : t('remove.grounded'),
    action: one ? t('remove.action') : t('remove.actionN', { n: ids.length }),
    run: () => {
      for (const id of ids) {
        deleteVehicle(id).then(
          () => notify(`fleet:remove:${id}`, 'info', t('remove.sent', { id })),
          (e: unknown) => {
            const code = e instanceof ApiError ? (e.reason ?? 0) : 0
            const desc = code === 105 ? t('remove.landFirst') : code ? `${code} ${reasonText(code).short}` : e instanceof Error ? e.message : ''
            notify(`fleet:remove:${code}`, 'warning', t('remove.failed', { id }), desc)
          },
        )
      }
    },
  })
}
