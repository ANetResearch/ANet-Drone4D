// D2 shell start-up (ADR-111; M15 §15.0; AWR-14 §20.2 to §20.4): the run-time surface follows the route and the sandbox
// session, a queue ticket of this tab resumes its long poll, the automatic keepalive runs (remaining <= 300 s, renewable,
// visible page, one tab), the D2 actions are registered (key B, GSD calculator, new task), the manual controller gets the
// page client and the domain data (model limits from M21's catalogue store, the 3 s keep-out entry prediction of M18's
// manualKeepoutWarning over the current criterion bodies), and the `ent` / `tgt` / `panel` deep links apply once.
// D2.2-W1: the world scope follows the surface and the opened world (app/shell/worldScope.ts), and the shell's world-bound
// state (entity selection, drafts, tools, manual control, alarms, event log, discoveries) resets with it
// (ui/shell/worldReset.ts).
// Called once from app/App.tsx when the application module loads (before the router resolves the first path).
import { rtClient } from '@/net/rt'
import { catalogStore } from '@/stores/catalog'
import { manualKeepoutWarning } from '@/stores/targets'
import { setManualDeps } from '@/ui/manual/controller'
import { applyEntityDeepLink } from '@/ui/sandbox/entities'
import { installAutoRenew, sandbox } from '@/ui/sandbox/session'
import { registerD2Actions } from '@/ui/actions/d2Actions'
import { installSensorBindings } from '@/ui/fpv/bindings'
import { installSurface } from './surface'
import { installWorldScope } from './worldScope'
import { installWorldReset } from '@/ui/shell/worldReset'

let done = false

/** limits of a vehicle's model from the catalogue cache (multirotor vmax; fixed-wing envelope) */
function limitsOf(vehicle: string): { fw: boolean; mc: { vmaxMps: number }; fwl: { vMinMps: number; vMaxMps: number; vCruiseMps: number; bankMaxRad: number; climbMaxMps: number } | null } {
  const rt = rtClient()
  const model = rt ? rt.roster.get(rt.roster.agentNoOf(vehicle))?.model ?? null : null
  const cs = catalogStore.getState()
  const m = model ? [...cs.models, ...cs.sessionModels].find((x) => x.model_id === model) : undefined
  const fw = m?.category === 'fixed_wing' || m?.category === 'vtol'
  const vmax = m?.speed?.max_mps ?? 10
  return {
    fw, mc: { vmaxMps: vmax },
    fwl: fw ? { vMinMps: m?.speed?.min_mps ?? 14, vMaxMps: vmax, vCruiseMps: m?.speed?.cruise_mps ?? 20, bankMaxRad: (30 * Math.PI) / 180, climbMaxMps: 4 } : null,
  }
}

export function installD2Shell(): void {
  if (done) return
  done = true
  installSurface()
  installWorldReset()
  installWorldScope()
  registerD2Actions()
  installSensorBindings()
  sandbox.resumeQueue()
  installAutoRenew()
  setManualDeps({
    rt: () => rtClient(),
    limits: limitsOf,
    predict: (p, v, horizonS) => {
      const w = manualKeepoutWarning(p, v, horizonS)
      return w ? { target: w.id, kind: w.kind === 'acoustic' ? 'acoustic' : 'visual', tS: w.tS, d: Number.NaN } : null
    },
  })
  if (typeof location !== 'undefined') applyEntityDeepLink(new URLSearchParams(location.search))
}
