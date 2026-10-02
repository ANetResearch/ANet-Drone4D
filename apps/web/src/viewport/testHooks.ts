// window.__vp test hooks (dev and VITE_AWR_TEST_SWITCHES=1 builds only; AWR-18 §9.5 test switches). Owner: M06.
// Playwright specs (perf/skeleton.spec.ts, perf/m06/*.spec.ts) use them to aim clicks at world ENU points, read
// vehicle poses and viewport internals (buckets, glyph and label counts, camera state), run the 28-item feature
// matrix of the regression page (D1-AC-14) and force a context loss. Plain production builds fold TEST_SWITCHES to
// false and drop this code (M06-AC-010).
import { FLIGHT_STATE_NAMES } from '@awr/contracts/enums'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { activePointCloud, governor } from '@/engine'
import { rtClient } from '@/net/rt'
import { selection } from '@/stores/selection'
import { vp } from './session'
import { camera, mission, pick, viewport } from './facade'
import { listLayers } from './layers/registry'

export function installTestHooks(): void {
  if (!TEST_SWITCHES || typeof window === 'undefined') return
  const out = new Float32Array(2)
  const pose = new Float64Array(3)
  ;(window as unknown as { __vp: unknown }).__vp = {
    /** CSS px of a world ENU point in the viewport, or null outside the frustum */
    project(e: number, n: number, u: number): [number, number] | null {
      return viewport.projectToScreen([e, n, u], out) ? [out[0], out[1]] : null
    },
    viewportRect(): { x: number; y: number; w: number; h: number } | null {
      const r = vp.host?.getBoundingClientRect()
      return r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null
    },
    pickAt: (x: number, y: number) => pick.at(x, y),
    pickFull: (x: number, y: number, want: ('drone' | 'point' | 'ground')[]) => pick.pickAt(x, y, { want }),
    /** point-cloud pick (ID pass) at CSS px: ENU, class, HAG, node spacing; and the last click's point */
    pointAt: async (x: number, y: number) => {
      const p = await pick.pointAt(x, y)
      return p ? { ...p, pointEnu: Array.from(p.pointEnu) } : null
    },
    pointPick: () => (vp.pointPick ? { ...vp.pointPick, pointEnu: Array.from(vp.pointPick.pointEnu) } : null),
    ground: () => pick.ground,
    sendGoto: () => mission.sendGoto()?.target ?? null,
    gotoState: () => mission.gotoState,
    /** status of the last takeoff, hover, land or rtl for the primary ('sent', accepted, running, succeeded, ...) */
    cmdState: (op: 'takeoff' | 'hover' | 'land' | 'rtl') => mission.commandState(op)?.status ?? null,
    /** every result status of the last takeoff, hover, land or rtl, and of the last goto, in arrival order */
    cmdResults: (op: 'takeoff' | 'hover' | 'land' | 'rtl') => [...(mission.commandState(op)?.results ?? [])],
    gotoResults: () => [...mission.gotoResults],
    clearPick: () => pick.clear(),
    /** role and seat of the realtime session (serverInfo) */
    session: () => {
      const s = rtClient()?.serverInfo
      return s ? { role: s.role, seat: s.seat, runId: s.runId, worldId: s.worldId } : null
    },
    /** rendered pose (ENU m) of a vehicle id */
    dronePose(id: string): [number, number, number] | null {
      const no = rtClient()?.roster.agentNoOf(id) ?? -1
      return no >= 0 && vp.drones?.poseOf(no, pose) ? [pose[0], pose[1], pose[2]] : null
    },
    /** FlightState name of a vehicle from the latest swarm columns (Lite32 fs), null before it is seen */
    flightState(id: string): string | null {
      const rt = rtClient()
      const no = rt?.roster.agentNoOf(id) ?? -1
      if (!rt || no < 0) return null
      const sw = rt.swarm
      for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === no) return FLIGHT_STATE_NAMES[sw.fs[i]] ?? null
      return null
    },
    select: (ids: string[]) => selection.select(ids),
    world: () => vp.world,
    worldCtx: () => (vp.worldCtx ? { ...vp.worldCtx, coordinateBytes: vp.worldCtx.coordinateBytes?.byteLength ?? 0 } : null),
    backend: () => viewport.backend,
    backendInfo: () => (vp.be ? { tier: vp.be.tier, deviceClass: vp.be.deviceClass, state: vp.be.state, pointSizeMode: vp.be.pointSizeMode, caps: vp.be.caps,
      startRung: vp.be.startRung, lowestAllowedRung: vp.be.lowestAllowedRung, selftest: vp.be.info.selftest, programs: vp.be.programsCount(),
      renderer: vp.be.info.rendererString, cloudScale: vp.be.cloudScale } : null),
    conn: () => rtClient()?.status ?? 'IDLE',
    roster: () => rtClient()?.roster.entries() ?? [],
    /** camera state (mode, follow lock, pose ENU, flight, view offset) and mode switching through the facade */
    camera: () => {
      const r = vp.rig
      if (!r) return null
      const c = vp.camera
      return { mode: r.mode, followLock: r.followLock, pose: r.getPose(), flying: r.flight.active, moving: r.moving, focusAgent: r.focusAgent,
        near: c?.near, far: c?.far, fov: c?.fov, view: c?.view ? { offsetX: c.view.offsetX, offsetY: c.view.offsetY, enabled: c.view.enabled } : null }
    },
    setMode: (m: 'orbit' | 'free' | 'third' | 'fpv' | 'bird') => camera.setMode(m),
    /** drone layer internals: counts per bucket, glyph and marker instances, trails, focus set */
    drones: () => {
      const d = vp.drones
      if (!d) return null
      const L = d.layer
      return {
        n: d.poses.n, heroN: L.buckets.heroN, lowN: L.buckets.lowN, lowCap: L.lowCap, glyphs: L.glyphs.n, glyphTruncated: L.glyphs.truncatedTotal,
        markers: L.markers.n, focusSet: L.focus.size, trails: { focus: L.trailFocus.drawCount(), sel: L.trailSel.drawCount(), halo: L.trailHalo.drawCount() },
        frustums: L.frustums.count, draws: L.drawCountDrones(),
      }
    },
    labels: () => vp.labels?.shown() ?? [],
    zones: () => vp.zones?.zones.map((z) => ({ id: z.id, kind: z.kind, z0: z.z0, z1: z.z1, n: z.ring.length / 2 })) ?? [],
    /** PerfGovernor wiring: CAS attached (M05 services.cas) and the knob levels in step order */
    governor: () => ({ hasCas: governor().hasCas, knobs: governor().knobLevels() }),
    /** M05 point-cloud engine state the viewport relies on: EDL material bound (Tier B/A), class-11 hero demotion */
    pointCloud: () => {
      const e = activePointCloud()
      return e ? { edl: e.edlMaterial !== null, heroDemoted: e.heroClassActive } : null
    },
    /** the viewport session (debugging only) */
    vpSession: vp,
    /** layer roots under WorldRoot: name, visibility, children (debugging) */
    layers: () => vp.worldRoot.children.map((c) => ({ name: c.name, visible: c.visible, children: c.children.map((k) => `${k.name || k.type}:${k.visible}`) })),
    /** registered LayerSpecs with this frame's draw counts */
    specs: () => listLayers().map((s) => ({ id: s.id, owner: s.owner, perfKey: s.perfKey, channel: s.channel, hasRoot: s.root !== null })),
    /** WEBGL_lose_context: lose (and, after restoreMs, restore) the current context (M06-AC-009) */
    loseContext(): boolean {
      const gl = vp.be?.renderer.getContext() as WebGL2RenderingContext | undefined
      const ext = gl?.getExtension('WEBGL_lose_context')
      if (!ext) return false
      ext.loseContext()
      return true
    },
    rebuild: () => viewport.rebuild(),
    /** the 28-item feature matrix of g01 §3 on this build's backend path (D1-AC-14; lazy chunk) */
    featMatrix: async (o: { tier?: 'A' | 'B' | 'S' } = {}) => (await import('./dev/featMatrix')).runFeatMatrix(o),
  }
}
