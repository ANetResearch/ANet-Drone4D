// Run-time surface and session role (ADR-111; M15-FR-202, FR-203; UX-FR-103, UX-FR-104; AWR-14 §20.2.3). One build, two
// write surfaces: the operator surface on /sandbox/:sid when this browser holds that sid's sandbox token, the viewer
// surface everywhere else in the demo build (the shared show of the run world, or static browsing of another world:
// point cloud and terrain only, no realtime link). Outside the demo build `/world/:id` keeps the D1 behaviour ('local':
// the page's own role and seat decide). The server enforces the same policy independently (ADR-093): hiding a control
// is never the security boundary. Overlay and page routes (World Hub, settings, the OpenAPI page) keep the surface of
// the view below them, so the canvas and the realtime link stay as they are (AWR-14 §20.2.3 rule 4).
// D2.1 (ADR-121, ADR-123; AWR-04 §15.7, §15.9.4): a parked sandbox keeps its surface (its stored token stays resumable for
// the park retention) but has no realtime link (rtPath null) until resume brings the sim-core back; the internal identity
// adds the `internal` modifier (hidden entries of the demo build come back, lazily loaded; app/shell/internal.ts).
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { cityShowRtUrl, DEMO_PUBLIC, DEMO_WORLD } from '@/lib/demo'
import { SANDBOX_API, SHARED_API } from '@/net/endpoint'
import { currentRoute, onRoute, type RouteMatch } from '@/app/router/router'
import { sandbox, sandboxStore, type SandboxSessionView } from '@/ui/sandbox/session'
import { UX } from '@/ui/testing/uxProbe'
import { prefs, prefsStore } from '@/stores/prefs'

export type Surface = 'local' | 'shared' | 'static' | 'sandbox' | 'sandbox-gone'
export type SessionRole = 'local' | 'shared-viewer' | 'sandbox-operator' | 'sandbox-viewer'
export interface UiSurface {
  surface: Surface
  role: 'viewer' | 'operator'
  /** realtime path of this surface; null: no realtime link (static browsing, an unavailable sandbox) */
  rtPath: string | null
  worldId: string | null
  sid: string | null
}
export interface SurfaceState extends UiSurface {
  /** D2.1: the internal identity is unlocked (stores/identity.ts) */
  internal: boolean
  version: number
}

type Sess = Pick<SandboxSessionView, 'sid' | 'token' | 'tokenExpUnixMs' | 'phase' | 'world'> & { parked?: SandboxSessionView['parked'] }
/** phases of a parked session (no sim-core: no realtime link) */
const PARKED: ReadonlySet<SandboxSessionView['phase']> = new Set(['parking', 'parked', 'resuming', 'queued', 'slot_ready'])
type RouteLike = Pick<RouteMatch, 'params'> & { route: { id: string } }

const DEAD: ReadonlySet<SandboxSessionView['phase']> = new Set(['ended', 'failed', 'none'])

/** pure resolution of the surface (tests); `prev` is kept by overlay and page routes */
export function resolveSurface(route: RouteLike | null, sess: Sess, showWorld: string | null, demo: boolean, prev: UiSurface | null, now = Date.now()): UiSurface {
  const id = route?.route.id ?? null
  if (id === 'sandbox') {
    const sid = route?.params.sid ?? null
    const parked = PARKED.has(sess.phase) && (sess.parked?.retentionUntilMs ?? 0) > now
    const ok = sid !== null && sess.sid === sid && !!sess.token && (sess.tokenExpUnixMs > now || parked) && !DEAD.has(sess.phase)
    return ok
      ? { surface: 'sandbox', role: 'operator', rtPath: parked ? null : `${SANDBOX_API}/sessions/${sid}/rt`, worldId: sess.world, sid }
      : { surface: 'sandbox-gone', role: 'viewer', rtPath: null, worldId: showWorld ?? (demo ? DEMO_WORLD : null), sid }
  }
  if (id === 'world' || id === 'replay') {
    const w = route?.params.id ?? null
    if (!demo) return { surface: 'local', role: 'operator', rtPath: `${SHARED_API}/rt`, worldId: w, sid: null }
    if (w === (showWorld ?? DEMO_WORLD)) return { surface: 'shared', role: 'viewer', rtPath: `${SHARED_API}/rt`, worldId: w, sid: null }
    // D2.2-W2: a world with a recorded show (lib/demo CITY_SHOW_FIXTURE) is shared too, played back client-side
    // (fake: source) instead of static point-cloud-only browsing; a world with none keeps the D2.2-W1 static surface
    const replay = cityShowRtUrl(w)
    return replay
      ? { surface: 'shared', role: 'viewer', rtPath: replay, worldId: w, sid: null }
      : { surface: 'static', role: 'viewer', rtPath: null, worldId: w, sid: null }
  }
  if (prev) return prev
  return demo
    ? { surface: 'shared', role: 'viewer', rtPath: `${SHARED_API}/rt`, worldId: showWorld ?? DEMO_WORLD, sid: null }
    : { surface: 'local', role: 'operator', rtPath: `${SHARED_API}/rt`, worldId: null, sid: null }
}

// D2.2-W2: a city's recorded show plays back through the same 'shared' surface as the live demo (lib/demo
// cityShowRtUrl sets rtPath to a `fake:` replay source), told apart only by that prefix. D2.2-FX-world: this used to
// be the same three-line expression copied into AppHeader.tsx, ui/mobile/parts.tsx (RoleChip) and
// ui/mobile/TabletShell.tsx (the breadcrumb) separately, and the third copy was missed when the other two were added
// (TabletTopBar still read "shared demo" while the role chip next to it already said "simulated replay") — one
// shared, directly testable function instead, so the three callers cannot drift again.
export function isCityReplay(surface: Surface, rtPath: string | null): boolean {
  return surface === 'shared' && typeof rtPath === 'string' && rtPath.startsWith('fake:')
}

/** session role of FR-202 (`sandbox-viewer` is the P1 watch link) */
export function sessionRoleOf(s: Surface): SessionRole {
  if (s === 'local') return 'local'
  if (s === 'sandbox') return 'sandbox-operator'
  return 'shared-viewer'
}

export const surfaceStore = createAwrStore<SurfaceState>('ui.surface', () => ({
  ...resolveSurface(null, sandboxStore.getState(), null, DEMO_PUBLIC, null), internal: false, version: 0,
}))
/** the internal identity is unlocked (stores/identity.ts writes it through setSurfaceInternal) */
export const useInternal = (): boolean => useSurface((s) => s.internal)
export function setSurfaceInternal(on: boolean): void {
  const cur = surfaceStore.getState()
  if (cur.internal === on) return
  surfaceStore.setState({ internal: on, version: cur.version + 1 })
  UX.identity.tier = on ? 'internal' : 'public'
}
export const useSurface = <T,>(sel: (s: SurfaceState) => T): T => useStore(surfaceStore, sel)
export const useSessionRole = (): SessionRole => useSurface((s) => sessionRoleOf(s.surface))
export const surfaceNow = (): SurfaceState => surfaceStore.getState()
/** the write surfaces of the D2 management panels are shown (sandbox operator) */
export const isSandboxOperator = (): boolean => surfaceStore.getState().surface === 'sandbox'
export const useSandboxOperator = (): boolean => useSurface((s) => s.surface === 'sandbox')

const same = (a: UiSurface, b: UiSurface): boolean =>
  a.surface === b.surface && a.rtPath === b.rtPath && a.worldId === b.worldId && a.sid === b.sid && a.role === b.role

let showWorld: string | null = null
/** the shared show's run world once known (serverInfo of /api/rt); the demo default is synthcity */
export function setShowWorld(w: string | null): void {
  if (!w || w === showWorld) return
  showWorld = w
  evaluate()
}

/** sids whose stored token was already tried (a sandbox route recovers its session once, AWR-14 §20.4.7) */
const resumed = new Set<string>()

function evaluate(): void {
  const route = currentRoute()
  if (route?.route.id === 'sandbox') {
    const sid = route.params.sid
    // the synchronous part of resume() adopts a stored token at once (no "unavailable" flash), the GET checks it after
    if (sid && sandboxStore.getState().sid !== sid && !resumed.has(sid)) {
      resumed.add(sid)
      void sandbox.resume(sid)
    }
  }
  const cur = surfaceStore.getState()
  const next = resolveSurface(route, sandboxStore.getState(), showWorld, DEMO_PUBLIC, cur)
  UX.sandbox.surface = next.surface
  // the point cloud layer opens the last world on routes without a world parameter (the sandbox route): the session's
  // world becomes the last world before the canvas asks (interim until M05 reads the surface world, WP-12 report)
  if (next.surface === 'sandbox' && next.worldId && prefsStore.getState().ui.lastWorld !== next.worldId) prefs.setUi('lastWorld', next.worldId)
  if (same(cur, next)) return
  surfaceStore.setState({ ...next, internal: cur.internal, version: cur.version + 1 })
}

let installed = false
/** follow the route and the sandbox session (start-up); returns the detach function */
export function installSurface(): () => void {
  if (installed) return () => {}
  installed = true
  const offRoute = onRoute(evaluate)
  const offSess = sandboxStore.subscribe((s, p) => {
    if (s.sid !== p.sid || s.token !== p.token || s.phase !== p.phase || s.world !== p.world || s.tokenExpUnixMs !== p.tokenExpUnixMs || s.parked !== p.parked) evaluate()
  })
  evaluate()
  return () => {
    installed = false
    offRoute()
    offSess()
  }
}
