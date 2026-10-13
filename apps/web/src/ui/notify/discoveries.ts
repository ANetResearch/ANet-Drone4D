// Unconfirmed target discoveries (AWR-04 §10.7; AWR-14 §20.11.2; M15-FR-219): target id -> wall ms of the latest
// discovery while not confirmed. The "confirm" of the discovery toast and the alarm centre's acknowledgement are the
// same state (the alarm item target:target.discovered:<target>, local to this client, never sent; M18's stores/targets.ts
// alarm list is acknowledged with it). Read by the one-red
// arbitration (ui/notify/redFigures.ts: critical candidates of the viewport, rank 5) and the targets list.
import { dismiss } from '@/app/providers/ToastProvider'
import { targetsActions } from '@/stores/targets'
import { alarms } from './alarms'

const discovered = new Map<string, number>()

export const discoveryKey = (target: string): string => `target:target.discovered:${target}`
export const discoveryToastKey = (target: string): string => `target:discovered:${target}`

export function noteDiscovery(target: string, wallMs: number): void {
  discovered.set(target, wallMs)
}

/** unconfirmed discovered targets with the time of their latest discovery */
export function unconfirmedDiscoveries(): ReadonlyMap<string, number> {
  for (const id of [...discovered.keys()]) {
    if (!alarms.get(discoveryKey(id))?.acked) continue
    // acknowledged in the alarm centre: the sticky toast goes as well
    discovered.delete(id)
    dismiss(discoveryToastKey(id))
  }
  return discovered
}

/** confirm a discovery (toast action and alarm centre share the acknowledgement) */
export function confirmDiscovery(target: string): void {
  alarms.ack(discoveryKey(target))
  discovered.delete(target)
  dismiss(discoveryToastKey(target))
  // M18's alarm list (its red candidate for the viewport layers) follows the same confirmation
  targetsActions.acknowledge(target)
}

export function resetDiscoveries(): void {
  discovered.clear()
}
