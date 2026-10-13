// Internal surface (R-D2-38, R-D2-39; ADR-123; AWR-04 §15.9.4; AWR-14 §20.26.6; M15-FR-236; UX-FR-158). Owner: M15.
// The internal identity (stores/identity.ts) adds the `internal` modifier to the run-time surface: the entries the public
// demo build hides come back (Jobs, Runs and replay, Bench, the report pages and their palette actions; the Perf panel's
// report upload; "request control" of the shared show, which exchanges the identity token for a run admin token, R01),
// and the sandbox clients attach X-AWR-Identity on session creation (reserved slot, 8 h, never preempted). Without the
// identity everything stays as D2 defines it (R-D2-38). The page components of the restored routes are the regular ones
// (route modules are part of the route table); the restored routes are registered only while unlocked.
// The server enforces the same policy independently (ADR-093): hiding or showing an entry is never the security boundary.
import { DEMO_PUBLIC } from '@/lib/demo'
import { navigate } from '@/app/router/router'
import { installRoutes } from '@/app/router/table'
import { setIdentityProvider } from '@/net/api'
import { rtClient } from '@/net/rt'
import { setSandboxIdentityProvider } from '@/net/sandbox/client'
import { identityStore, identityToken } from '@/stores/identity'
import { registerAction } from '@/ui/actions/registry'
import { setSandboxIdentity } from '@/ui/sandbox/session'
import { setSurfaceInternal } from './surface'

let offActions: (() => void)[] = []
let applied: boolean | null = null

function apply(on: boolean): void {
  if (applied === on) return
  applied = on
  setSurfaceInternal(on)
  if (DEMO_PUBLIC) installRoutes(on)
  for (const off of offActions) off()
  offActions = []
  if (on && DEMO_PUBLIC) {
    offActions.push(
      registerAction({ id: 'replay.runs', labelKey: 'runs.title', icon: 'data.folder', group: 'jump', keywords: ['runs', 'replay', 'recording'], run: () => navigate('/runs') }),
      registerAction({ id: 'jobs.open', labelKey: 'jobs.title', icon: 'nav.recon', group: 'jump', keywords: ['recon', 'jobs', 'reconstruction'], run: () => navigate('/jobs') }),
    )
  }
}

let installed = false
/** start-up (main.tsx, after the routes): follow the identity store; idempotent */
export function installInternalSurface(): () => void {
  if (installed) return () => {}
  installed = true
  setSandboxIdentity(identityToken)
  setSandboxIdentityProvider(identityToken)
  // D2-INT (d2-WP-12c-to-M11 item 5): shared-show GETs carry X-AWR-Identity (hidden families readable) and the page's
  // realtime link carries `ident.<token>` (not counted against the viewer seats, internal idle threshold)
  setIdentityProvider(identityToken)
  apply(identityStore.getState().tier === 'internal')
  const off = identityStore.subscribe((s, p) => {
    if (s.tier !== p.tier) apply(s.tier === 'internal')
    if (s.token !== p.token) rtClient()?.setIdentity(identityToken())
  })
  return () => {
    installed = false
    off()
  }
}
