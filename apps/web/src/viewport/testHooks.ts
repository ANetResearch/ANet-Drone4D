// window.__vp test hooks (dev and VITE_AWR_TEST_SWITCHES=1 builds only; AWR-18 §9.5 test switches). Owner: M06.
// Playwright specs (perf/skeleton.spec.ts, perf/m06/*.spec.ts) use them to aim clicks at world ENU points, read
// vehicle poses and viewport internals (buckets, glyph and label counts, camera state), run the 28-item feature
// matrix of the regression page (D1-AC-14) and force a context loss. Plain production builds fold TEST_SWITCHES to
// false and drop this code (M06-AC-010).
// D2.1 (WP-11b): field (the field-view renderer, synthetic profiles, volumes and targets: dev/fieldDemo.ts, lazy), quality
// (form factor, profile, point-size mode, DPR), loop (suspension, requestFrame, wake) and touch (long presses seen).
// D2.2-W1: isolation() reports the world scope and what every world-bound layer and model holds and draws now, and
// governorHold(on) keeps the PerfGovernor at level 0 so the layers under test stay on under SwiftShader (perf/d22).
import { FLIGHT_STATE_NAMES } from '@awr/contracts/enums'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { activePointCloud, ctx as frameCtx, events, governor, loop } from '@/engine'
import { targetsModel } from '@/engine/targets'
import { fieldModel } from '@/engine/perception'
import { worldScopeNow } from '@/stores/worldScope'
import { taskingStore } from '@/stores/tasking'
import { targetsLayer } from './layers/targets'
import { taskingLayer } from './layers/tasking'
import type { FieldKind } from '@/engine/perception'
import { perceptionActions, perceptionStore, type FieldView, type VolumesMode } from '@/stores/perception'
import { targetsActions } from '@/stores/targets'
import { fieldLayer } from './layers/field'
import { fpvRadPerPx, type FpvDrag, type LongPress } from './interaction'
import { rtClient } from '@/net/rt'
import { selection, selectionStore } from '@/stores/selection'
import { vp } from './session'
import { camera, drones, field, mission, pick, quality, sensorView, vehicleModels, viewport, type SensorEnv, type SensorViewMode } from './facade'
import { listLayers } from './layers/registry'
import { sv } from './sensorView/state'

export function installTestHooks(): void {
  if (!TEST_SWITCHES || typeof window === 'undefined') return
  const out = new Float32Array(2)
  const pose = new Float64Array(3)
  const longPresses: { x: number; y: number; clientX: number; clientY: number }[] = []
  events.on<LongPress>('viewport.longpress', (p) => longPresses.push({ x: p.x, y: p.y, clientX: p.clientX, clientY: p.clientY }))
  const fpvDrags: FpvDrag[] = []
  events.on<FpvDrag>('fpv.drag', (d) => {
    if (fpvDrags.length < 512) fpvDrags.push({ ...d })
  })
  // lazy: the synthetic field data stays out of every bundle without test switches (same inline test as featMatrix)
  const demo = import.meta.env.DEV || import.meta.env.VITE_AWR_TEST_SWITCHES === '1' ? () => import('./dev/fieldDemo') : null
  const rosterUavs = (): { id: string; model: string }[] => (rtClient()?.roster.entries() ?? []).map((e) => ({ id: e.id, model: e.model || 'p600' }))
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
    /** the selected vehicle ids (D2.1 touch specs: a tap selects) */
    selected: () => [...selectionStore.getState().ids],
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
    /** D2.2-W1: the world scope and what the world-bound layers and models hold and draw now */
    isolation: () => {
      const L = vp.drones?.layer
      const draws: Record<string, number> = {}
      for (const s of listLayers()) draws[s.id] = s.drawCount(frameCtx)
      const fs = fieldLayer.renderer?.stats
      return {
        scope: { ...worldScopeNow() }, draws, staticBrowse: vp.staticBrowse,
        poses: vp.drones?.poses.n ?? 0, frustums: L?.frustums.count ?? 0, roster: rtClient()?.roster.size ?? 0,
        targets: targetsModel.rows.n, targetBodies: targetsLayer.overlay?.nBodies ?? 0,
        field: { profiles: fieldModel.profiles.size, volumes: fieldModel.volumes !== null, quant: fieldModel.quant.size, points: fs?.points ?? 0,
          bodyAnchors: fs?.bodyAnchors ?? 0, uavAnchors: fs?.uavAnchors ?? 0 },
        tasking: taskingLayer.overlay?.stats ?? null, labels: vp.labels?.shown().length ?? 0,
        taskingStore: { nests: taskingStore.getState().nests.length, nestStatus: Object.keys(taskingStore.getState().nestStatus).length,
          tasks: taskingStore.getState().tasks.length },
        mission: { goto: vp.mission?.goto.visible ?? false }, camera: { mode: vp.rig?.mode ?? null, focus: vp.rig?.focusAgent ?? -1 },
      }
    },
    zones: () => vp.zones?.zones.map((z) => ({ id: z.id, kind: z.kind, z0: z.z0, z1: z.z1, n: z.ring.length / 2 })) ?? [],
    /** PerfGovernor wiring: CAS attached (M05 services.cas) and the knob levels in step order */
    governor: () => ({ hasCas: governor().hasCas, knobs: governor().knobLevels() }),
    /** D2.2-W1: hold the PerfGovernor (every knob back to level 0 and kept there) or release it */
    governorHold: (on: boolean) => {
      const g = governor()
      g.hold = on
      if (on) g.resetAll()
    },
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
    /** explicit red owner of the viewport (a vehicle id; null returns to the RedArbiter) */
    setRedOwner: (id: string | null) => drones.setRedOwner(id ? { kind: 'drone', id } : null),
    /** D2 vehicle shapes: register a model's frame and size (the drone layer re-reads its shapes) */
    registerModel: (model: string, frame: 'quad_x' | 'hex' | 'fixed_wing' | 'vtol_quadplane', size: [number, number, number]) => vehicleModels.register(model, { frame, sizeM: size }),
    /** D2 sensor views (WP-11): the facade plus the pass internals the specs assert on */
    sensorView: {
      set: (m: SensorViewMode, o?: { enterFpv?: boolean; sensorNo?: number }) => sensorView.set(m, o),
      cycle: () => sensorView.cycle(),
      info: () => sensorView.info(),
      available: () => sensorView.available(),
      setPalette: (p: 'white_hot' | 'black_hot') => sensorView.setPalette(p),
      setDigitalZoom: (z: number) => sensorView.setDigitalZoom(z),
      setEnv: (e: Partial<SensorEnv>) => sensorView.setEnv(e),
      /** test emitters: [x, y, z, l, w, h, yaw, tC, dT, eps, hotFrac] rows (ENU); [] removes them */
      setEmitters: (rows: number[][]) => {
        const data = rows.map((r) => r.slice(0, 11))
        sensorView.setEmitterSource('test', data.length === 0 ? null : (out, max) => {
          const n = Math.min(max, data.length)
          for (let i = 0; i < n; i++) for (let k = 0; k < 11; k++) out[11 * i + k] = Number.isFinite(data[i][k]) ? data[i][k] : Number.NaN
          return n
        })
      },
      /**
       * test emitters placed relative to the current camera every frame: [forward, right, up (m, camera axes), l, w, h,
       * yaw (rad, ENU), tC, dT, eps, hotFrac] rows; they stay in view while the FPV vehicle flies
       */
      setEmittersAhead: (rows: number[][]) => {
        const data = rows.map((r) => r.slice(0, 11))
        const m = new Float64Array(16)
        sensorView.setEmitterSource('test', data.length === 0 ? null : (out, max) => {
          const cam = vp.camera
          if (!cam) return 0
          cam.matrixWorld.toArray(m)
          // three frame axes of the camera: right = col 0, up = col 1, back = col 2; ENU = (x, -z, y)
          const n = Math.min(max, data.length)
          for (let i = 0; i < n; i++) {
            const [f, r, u] = data[i]
            const x = m[12] + r * m[0] + u * m[4] - f * m[8]
            const y = m[13] + r * m[1] + u * m[5] - f * m[9]
            const z = m[14] + r * m[2] + u * m[6] - f * m[10]
            out[11 * i] = x
            out[11 * i + 1] = -z
            out[11 * i + 2] = y
            for (let k = 3; k < 11; k++) out[11 * i + k] = Number.isFinite(data[i][k]) ? data[i][k] : Number.NaN
          }
          return n
        })
      },
      /** illuminators instead of the payload's (null: back to uav/{id}/payload) */
      setIlluminators: (list: { on: boolean; divergenceRad: number; rangeM: number }[] | null) => {
        sv.illumOverride = list ? list.map((x) => ({ ...x })) : null
      },
      projectBox: (c: number[], sz: number[], yaw: number) => {
        const o = new Float32Array(4)
        return sensorView.projectBox(c, sz, yaw, o) ? Array.from(o) : null
      },
      pass: () => {
        const p = vp.be?.sensor
        return p ? { active: sensorView.active, rt: p.rt ? [p.rt.width, p.rt.height] : null, emitters: p.emitters, candidates: p.emitterCandidates, inView: p.emittersInView, illuminators: p.illuminators, range: p.lwirRange,
          material: (p.quad.material as { name?: string }).name ?? '', mode: p.view.u.mode.value as number,
          table: Array.from((p.emitTex.image.data as Float32Array).subarray(0, p.emitters * 12)).map((x) => Math.round(x * 1000) / 1000),
          u: Object.fromEntries((['lo', 'hi', 'blackHot', 'count', 'tSurf', 'tAir', 'tSky', 'epsBg', 'day', 'structK', 'groundY'] as const)
            .map((k) => [k, p.view.u[k].value as number])) } : null
      },
      /** FPV camera projection (column-major) and whether the renderer flagged it reversed */
      projection: () => (vp.camera ? { m: Array.from(vp.camera.projectionMatrix.elements), reversed: vp.camera.reversedDepth } : null),
    },
    /** D2.1 field view (WP-11b): renderer state, synthetic data, view switching */
    field: {
      stats: () => field.stats(),
      renderer: () => {
        const r = fieldLayer.renderer
        return r ? { mode: r.mode, rebuilds: r.rebuilds, lastBuildCpuMs: r.lastBuildCpuMs, lastSwitchMs: r.lastSwitchMs, level: r.level, budget: r.budget, budgetId: r.budgetId,
          visible: r.root.visible, points: r.points.visible, shells: r.shells.visible, draws: r.drawCount(),
          anchors: r.anchorList.map((a) => ({ kind: a.kind, id: a.id, j: a.j, points: a.points, bodyIdx: a.bodyIdx })) } : null
      },
      /** the first n built points: [x, y, z offset (m), slot, code, opacity, diameter] */
      sample: (n: number) => {
        const r = fieldLayer.renderer
        if (!r) return []
        const b = r.buf
        return Array.from({ length: Math.min(n, b.n) }, (_, i) => [b.off[3 * i], b.off[3 * i + 1], b.off[3 * i + 2], b.info[4 * i], b.info[4 * i + 1], b.info[4 * i + 2], b.info[4 * i + 3]])
      },
      /** anchor texels in use: [x, y, z, placed] */
      anchors: () => {
        const r = fieldLayer.renderer
        if (!r) return []
        const d = r.anchors.image.data as Float32Array
        return r.anchorList.map((_, s) => [d[4 * s], d[4 * s + 1], d[4 * s + 2], d[4 * s + 3]])
      },
      /** switch the view (and volumes, physical reference) through the perception store, then feed its synthetic profile */
      view: async (kind: FieldView, o: { volumes?: VolumesMode; physicalRef?: boolean } = {}) => {
        // the data arrives in the same task as the switch (the server's keyframe follows the subscription)
        const d = await demo?.()
        perceptionActions.setFieldView(kind)
        if (o.volumes) perceptionActions.setVolumes(o.volumes)
        if (o.physicalRef !== undefined) perceptionActions.setPhysicalRef(o.physicalRef)
        if (d && kind !== 'none') d.injectProfiles([kind as FieldKind], rosterUavs())
        return perceptionStore.getState().fieldView
      },
      /** synthetic targets near the first vehicle (rows and volumes); select: the first becomes the selected target */
      targets: async (n: number, select = true) => {
        const d = await demo?.()
        const u = rosterUavs()
        const no = u.length ? rtClient()?.roster.agentNoOf(u[0].id) ?? -1 : -1
        const near: [number, number, number] = no >= 0 && vp.drones?.poseOf(no, pose) ? [pose[0], pose[1], pose[2] - 20] : [0, 0, 0]
        const ids = d ? d.injectTargets(near, n, [...new Set(u.map((x) => x.model))]) : []
        if (select && ids.length) targetsActions.select(ids[0])
        return ids
      },
      highlight: (id: string | null) => field.highlight(id),
      /** pin the field level (FIELD_LEVEL; the governor's knob is ignored while pinned); null returns it to the governor */
      setLevel: (l: number | null) => fieldLayer.renderer?.pinLevel(l),
    },
    /** D2.1 quality profile (WP-11b) */
    quality: () => ({
      formFactor: quality.formFactor, profile: quality.profile, mobile: quality.mobile, pointSizeMode: vp.be?.pointSizeMode ?? null,
      maxPointSize: vp.be?.caps.maxPointSize ?? null, dpr: vp.be && vp.cssW > 0 ? vp.be.renderer.domElement.width / vp.cssW : null,
      drawingBuffer: vp.be ? [vp.be.renderer.domElement.width, vp.be.renderer.domElement.height] : null, qualityFps: loop.qualityFps, startRung: vp.be?.startRung ?? null,
      lowCap: vp.drones?.layer.lowCap ?? null, heroCap: vp.drones?.layer.buckets.heroCap ?? null, labelsCap: vp.labels?.cap ?? null,
    }),
    /** D2.1 power save (WP-11b): suspension without rAF, single frames and the static-browse wake */
    loop: {
      suspend: (on: boolean) => viewport.setSuspended(on),
      requestFrame: () => viewport.requestFrame(),
      wakeOnInput: (on: boolean) => viewport.setWakeOnInput(on),
      stats: () => ({ frameNo: loop.frameNo, suspended: loop.suspended, suspendedFrames: loop.suspendedFrames, running: loop.running }),
    },
    /** D2.1 touch (WP-11b): the long presses seen on the canvas; the FPV drags (fpv.onDrag), clear: true empties the log */
    touch: { longPresses: () => longPresses.slice() },
    fpvDrags: (clear = false) => (clear ? fpvDrags.splice(0) : fpvDrags.slice()),
    fpvRadPerPx: () => fpvRadPerPx([0, 0]),
    /** the 28-item feature matrix of g01 §3 on this build's backend path (D1-AC-14; lazy chunk). The env test is written
     * inline (not TEST_SWITCHES) so the bundler drops the dynamic import before it creates chunks: otherwise a plain
     * production build still emits the unreferenced featMatrix chunks with the allowFallback switch (M06-AC-010) */
    featMatrix: import.meta.env.DEV || import.meta.env.VITE_AWR_TEST_SWITCHES === '1'
      ? async (o: { tier?: 'A' | 'B' | 'S' } = {}) => (await import('./dev/featMatrix')).runFeatMatrix(o)
      : undefined,
  }
}
