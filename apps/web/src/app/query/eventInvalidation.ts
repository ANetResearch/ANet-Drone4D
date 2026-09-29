// Event driven cache updates (M15-FR-039, §6.6.3): the event bridge hands every flushed batch here once; each query key is
// invalidated at most once per flush (<= 4 Hz). Environment events never invalidate (stores/env.ts is fed by the
// env/state channel); session.switched invalidates the session and the fleet profiles and navigates to the new world.
import type { QueryClient } from '@tanstack/react-query'
import type { RtEvent } from '@/net/rt'
import { qk } from './keys'

export interface InvalidationPlan { fleet: boolean; missions: boolean; worlds: boolean; session: boolean; procs: boolean; switchedTo: string | null }

/** which keys a batch touches (pure, exported for tests) */
export function planInvalidation(batch: Iterable<Pick<RtEvent, 'type' | 'data'>>): InvalidationPlan {
  const p: InvalidationPlan = { fleet: false, missions: false, worlds: false, session: false, procs: false, switchedTo: null }
  for (const e of batch) {
    const ty = e.type
    if (ty === 'sim.vehicle.state' || ty === 'vehicle.added' || ty === 'vehicle.removed' || ty === 'roster.changed') p.fleet = true
    else if (ty.startsWith('mission.')) p.missions = true
    else if (ty === 'job.state' && e.data?.to === 'SUCCEEDED') p.worlds = true
    else if (ty === 'world.added' || ty === 'world.updated') p.worlds = true
    else if (ty === 'proc.state') p.procs = true
    else if (ty === 'session.switched') {
      p.session = true
      p.fleet = true
      const w = e.data?.world_id
      p.switchedTo = typeof w === 'string' ? w : p.switchedTo
    }
  }
  return p
}

export function applyInvalidation(qc: QueryClient, p: InvalidationPlan, go?: (worldId: string) => void): void {
  if (p.fleet) void qc.invalidateQueries({ queryKey: qk.fleetVehicles() })
  if (p.missions) void qc.invalidateQueries({ queryKey: qk.missions() })
  if (p.worlds) void qc.invalidateQueries({ queryKey: qk.worlds() })
  if (p.procs) void qc.invalidateQueries({ queryKey: ['sys', 'procs'] })
  if (p.session) void qc.invalidateQueries({ queryKey: qk.sessionCurrent() })
  if (p.switchedTo && go) go(p.switchedTo)
}
