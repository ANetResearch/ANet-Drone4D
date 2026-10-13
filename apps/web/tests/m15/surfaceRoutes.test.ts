// M15-FR-202, FR-203 (ADR-111; UX-FR-103, UX-FR-104; M15-AC-202, UX-AC-044 unit part): the run-time surface of a route
// and a sandbox session (operator on /sandbox/:sid with this sid's live token, the "unavailable" page otherwise; shared
// show or static browsing on /world/:id in the demo build; the D1 behaviour in a local build; overlay routes keep the view
// below), the session roles, and the D2 routes (/sandbox/api ranks before /sandbox/:sid, sid alphabet, seven worlds).
import { describe, expect, it, vi } from 'vitest'
import { matchPath, setRoutes, type RouteDef } from '@/app/router/router'
import { isCityReplay, resolveSurface, sessionRoleOf } from '@/app/shell/surface'

vi.setConfig({ testTimeout: 30_000 })

const SID = 'sb-k3m7q2x7ab'
const route = (id: string, params: Record<string, string> = {}) => ({ route: { id }, params })
const sess = (o: Partial<{ sid: string | null; token: string | null; tokenExpUnixMs: number; phase: string; world: string | null }> = {}) =>
  ({ sid: SID, token: 'v1.tok', tokenExpUnixMs: Date.now() + 60_000, phase: 'active', world: 'shanghai', ...o }) as never

describe('resolveSurface', () => {
  it('sandbox route: operator with this sid and a live token, else the unavailable page without a link', () => {
    const ok = resolveSurface(route('sandbox', { sid: SID }), sess(), 'synthcity', true, null)
    expect(ok).toEqual({ surface: 'sandbox', role: 'operator', rtPath: `/api/sandbox/v1/sessions/${SID}/rt`, worldId: 'shanghai', sid: SID })
    for (const bad of [sess({ sid: 'sb-aaaaaaaaaa' }), sess({ token: null }), sess({ tokenExpUnixMs: Date.now() - 1 }), sess({ phase: 'ended' }), sess({ phase: 'none' })]) {
      const g = resolveSurface(route('sandbox', { sid: SID }), bad, 'synthcity', true, null)
      expect(g.surface).toBe('sandbox-gone')
      expect(g.rtPath).toBeNull()
    }
  })

  it('demo build: the live show world and every city with a recorded show join the shared show, a world with neither browses statically', () => {
    expect(resolveSurface(route('world', { id: 'synthcity' }), sess(), 'synthcity', true, null)).toMatchObject({ surface: 'shared', rtPath: '/api/rt', role: 'viewer' })
    // D2.2-W2: shanghai (and the other five UrbanScene3D cities) has a recorded show too (lib/demo CITY_SHOW_FIXTURE),
    // played back from a `fake:` source instead of the D2.2-W1 static point-cloud-only browsing
    const shanghai = resolveSurface(route('world', { id: 'shanghai' }), sess(), 'synthcity', true, null)
    expect(shanghai).toMatchObject({ surface: 'shared', role: 'viewer', worldId: 'shanghai' })
    expect(shanghai.rtPath).toMatch(/^fake:\?world=shanghai&fixture=/)
    // a world with neither the live show nor a recorded fixture still browses statically (the fallback stays reachable)
    expect(resolveSurface(route('world', { id: 'no-such-world' }), sess(), 'synthcity', true, null)).toMatchObject({ surface: 'static', rtPath: null, worldId: 'no-such-world' })
    // before the show world is known the demo default synthcity is the show
    expect(resolveSurface(route('world', { id: 'synthcity' }), sess(), null, true, null).surface).toBe('shared')
  })

  it('local build keeps the D1 behaviour; overlay routes keep the surface below', () => {
    const local = resolveSurface(route('world', { id: 'shenzhen' }), sess(), null, false, null)
    expect(local).toMatchObject({ surface: 'local', rtPath: '/api/rt' })
    const sb = resolveSurface(route('sandbox', { sid: SID }), sess(), null, false, null)
    expect(sb.surface).toBe('sandbox')
    expect(resolveSurface(route('worlds'), sess(), null, false, sb)).toBe(sb)
    expect(resolveSurface(route('sandboxApi'), sess(), null, true, null).surface).toBe('shared')
  })

  it('maps surfaces to session roles', () => {
    expect(sessionRoleOf('local')).toBe('local')
    expect(sessionRoleOf('sandbox')).toBe('sandbox-operator')
    expect(sessionRoleOf('shared')).toBe('shared-viewer')
    expect(sessionRoleOf('static')).toBe('shared-viewer')
  })

  // D2.2-FX-world: AppHeader.tsx, ui/mobile/parts.tsx (RoleChip) and ui/mobile/TabletShell.tsx (the breadcrumb) each
  // used to recompute this three-line test separately; the breadcrumb's copy was missed when the other two were
  // added, so the same screen showed "shared demo" in the breadcrumb and "simulated replay" in the role chip next
  // to it. One shared function now, tested once, so the three callers cannot drift apart again.
  it('isCityReplay: only a shared surface whose rtPath is a fake: replay source', () => {
    expect(isCityReplay('shared', 'fake:?world=shanghai&fixture=show')).toBe(true)
    expect(isCityReplay('shared', '/api/rt')).toBe(false)
    expect(isCityReplay('shared', null)).toBe(false)
    expect(isCityReplay('static', 'fake:?world=shanghai&fixture=show')).toBe(false)
    expect(isCityReplay('sandbox', 'fake:?world=shanghai&fixture=show')).toBe(false)
    expect(isCityReplay('local', '/api/rt')).toBe(false)
    // end-to-end with the real resolver: shanghai's recorded show resolves to a replay rtPath and isCityReplay agrees
    const shanghai = resolveSurface(route('world', { id: 'shanghai' }), sess(), 'synthcity', true, null)
    expect(isCityReplay(shanghai.surface, shanghai.rtPath)).toBe(true)
    const synthcity = resolveSurface(route('world', { id: 'synthcity' }), sess(), 'synthcity', true, null)
    expect(isCityReplay(synthcity.surface, synthcity.rtPath)).toBe(false)
  })
})

describe('D2 routes', () => {
  it('registers /sandbox/api before /sandbox/:sid and validates the sid alphabet', async () => {
    const sandbox = (await import('@/app/routes/sandbox')).route
    const api = (await import('@/app/routes/sandboxApi')).route
    const world = (await import('@/app/routes/world')).route
    const worlds: RouteDef = { id: 'worlds', pattern: '/worlds', layer: 'overlay', d1: 'core' }
    setRoutes([sandbox, api, world, worlds])
    expect(matchPath('/sandbox/api')?.route.id).toBe('sandboxApi')
    expect(matchPath(`/sandbox/${SID}`)?.route.id).toBe('sandbox')
    expect(matchPath(`/sandbox/${SID}`)?.params.sid).toBe(SID)
    expect(matchPath('/sandbox/SB-1')).toBeNull()
    expect(matchPath('/sandbox/sb-k3m9q2x7ab')).toBeNull() // 9 is outside the base32 alphabet (17 §17.4.9 erratum)
    expect(matchPath('/world/shanghai')?.route.id).toBe('world')
  })

  it('the demo build registers the sandbox routes', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_AWR_DEMO', 'public')
    try {
      const { installRoutes } = await import('@/app/router/table')
      const ids = installRoutes().map((r) => r.id)
      expect(ids).toEqual(expect.arrayContaining(['root', 'world', 'worlds', 'settings', 'sandbox', 'sandboxApi']))
      expect(ids).not.toContain('jobs')
    } finally {
      vi.unstubAllEnvs()
      vi.resetModules()
    }
  })
})
