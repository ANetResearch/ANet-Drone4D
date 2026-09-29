// Viewport host runtime (M06 §6.2.4, §6.5, §6.6, §6.14, §6.17; FR-009, FR-010, FR-016..018, FR-022, FR-069..072). Owner: M06.
// Non-React side of WorldCanvas: attaches the backend to the live scene, installs the camera rig and the picker,
// registers the frame tasks (camera phase: FrameCtx canvas fields, cloud scale with motion degradation, camera update;
// render phase: pass plan + RenderBackend.renderFrame + per-layer draw counts; governor phase: PerfGovernor 1 Hz and the
// start-rung memory), runs self test and shader zoo under the mask (gate 'warmup'), and handles device loss (layers
// release GPU resources, 250 ms later the canvas is remounted and the backend re-created; 3 losses within 60 s are
// fatal, M06-E003).
import type { PerspectiveCamera, Scene } from 'three'
import {
  CameraRig, ctx as frameCtx, events, governor, loop, perf, perfProbe, Picker, refreshMs, register, setSoftware, onRefreshChange, type CameraPose, type FrameCtx,
} from '@/engine'
import { INJECT } from '@/engine'
import { boot } from '@/app/boot/BootController'
import { rtClient } from '@/net/rt'
import type { ClientStats } from '@/net/rt/types'
import { perfStore, type MotionCap } from '@/stores/perf'
import { listLayers, pointCloudServices, type WarmupItem } from './layers/registry'
import { newPassPlan, type RenderBackend } from './renderer'
import { vp } from './session'
import { installQualityMasks } from './qualityMask'
import { installBench } from './bench'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { parseInject } from './backend/testSwitches'
import { readCache, rememberFloorHeld, PREF_FLOOR_MS } from './backend/deviceClass'

export const HOST = { lostDelayMs: 250, lostWindowMs: 60_000, lostMax: 3, motionFactor: 0.75, motionRestoreMs: 200, motionRadS: 0.5, motionLinFrac: 0.5 } as const

const lostTimes: number[] = []
let rebuildCb: (() => void) | null = null
/** camera pose kept across a rebuild (device loss, viewport.rebuild()): the remounted rig resumes it (M05 request 7) */
let poseAcrossRebuild: CameraPose | null = null
/** WorldCanvas installs the remount callback (key bump) */
export function onRebuildRequest(cb: (() => void) | null): void {
  rebuildCb = cb
}

/** viewport.rebuild() and device loss: release GPU state of every layer, then remount the canvas after 250 ms */
export function requestRebuild(reason: string): Promise<void> {
  const now = performance.now()
  while (lostTimes.length && now - lostTimes[0] > HOST.lostWindowMs) lostTimes.shift()
  lostTimes.push(now)
  for (const s of listLayers()) s.onBackendLost?.()
  if (lostTimes.length >= HOST.lostMax && reason !== 'user') {
    console.error('M06-E003 the render backend was lost 3 times within 60 s')
    if (vp.be) vp.be.state = 'FAILED'
    perfStore.setState({ backendState: 'FAILED' })
    events.emit('backend.state', { state: 'FAILED', code: 'M06-E003' })
    return Promise.resolve()
  }
  perfStore.setState({ backendState: 'LOST' })
  events.emit('backend.state', { state: 'LOST', code: 'M06-E002' })
  if (vp.rig && vp.rig.mode !== 'fpv' && vp.rig.mode !== 'third') poseAcrossRebuild = vp.rig.getPose()
  return new Promise((ok) => setTimeout(() => {
    rebuildCb?.()
    ok()
  }, HOST.lostDelayMs))
}

/** shader-zoo items of every registered layer */
export function collectZoo(): WarmupItem[] {
  const out: WarmupItem[] = []
  const be = vp.be
  if (!be) return out
  for (const s of listLayers()) {
    try {
      out.push(...(s.warmupVariants?.(be) ?? []))
    } catch (e) {
      console.warn(`warm-up variants of ${s.id} failed`, e)
    }
  }
  return out
}

/** self test + shader zoo under the mask; resolves the 'warmup' boot gate */
export async function warmupBackend(be: RenderBackend, camera: PerspectiveCamera): Promise<void> {
  const p = perfProbe()
  try {
    const st = await be.selftest()
    if (!st.pointSizeOk) perfStore.setState({ pointSizeDegraded: true })
    await new Promise((ok) => requestAnimationFrame(() => ok(null)))
    const rep = await be.warmup(camera, collectZoo())
    p.meta.warmupMs = rep.warmupMs
  } catch (e) {
    console.error('shader zoo warm-up failed', e)
  }
  if (be.state === 'WARMING') be.state = 'READY'
  // after a rebuild (device loss, viewport.rebuild) the page is already revealed: the new renderer's warmed program
  // count is the new baseline of "no program after the reveal" (M06-FR-010, NFR-004)
  if (perf.revealed) p.gpu.programsAtReveal = be.programsCount()
  p.meta.backendState = be.state
  perfStore.setState({ backendState: be.state })
  events.emit('backend.state', { state: be.state, tier: be.tier, deviceClass: be.deviceClass })
  boot.resolveGate('warmup')
}

/**
 * AWR-17 §6.3 clientStats for the gateway (M11 request 5), governor phase at 1 Hz, one preallocated object: frame p50 and
 * p95 of the perf summary, heap (Chrome performance.memory), frames over 1.5 x the target since the last report, the
 * CAS point budget, the focus latency p95 and the global delay. Returns the new ring position.
 */
function reportClientStats(out: ClientStats, from: number): number {
  const rt = rtClient()
  const p = perfProbe()
  const r = p.frame.interval
  const cap = r.buf.length
  const lim = 1.5 * (p.meta.targetMs > 0 ? p.meta.targetMs : 16.7)
  let dropped = 0
  for (let i = Math.max(from, r.n - cap); i < r.n; i++) if (r.buf[i & (cap - 1)] > lim) dropped++
  if (!rt) return r.n
  const sum = perfStore.getState()
  const mem = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory
  out.frameMs = Number.isFinite(sum.p50Ms) ? sum.p50Ms : 0
  out.frameP95Ms = Number.isFinite(sum.p95Ms) ? sum.p95Ms : 0
  out.heapMB = mem ? mem.usedJSHeapSize / 1048576 : 0
  out.droppedFrames = dropped
  out.pointBudget = sum.B
  out.latencyP95Ms = Number.isFinite(sum.latencyP95Ms) ? sum.latencyP95Ms : 0
  out.dGlobalMs = Number.isFinite(p.latency.dGlobalMs) ? p.latency.dGlobalMs : 0
  rt.setClientStats(out)
  return r.n
}

/** install the frame tasks of a mounted canvas; returns the uninstall function */
export function installHost(be: RenderBackend, scene: Scene, camera: PerspectiveCamera, host: HTMLElement, canvas: HTMLCanvasElement): () => void {
  const p = perfProbe()
  vp.be = be
  vp.scene = scene
  vp.camera = camera
  vp.host = host
  be.attach(scene)
  loop.setTier(be.tier, be.deviceClass)
  setSoftware(be.deviceClass === 'software')
  frameCtx.be = be
  p.meta.tier = be.tier
  p.meta.deviceClass = be.deviceClass
  p.meta.renderer = be.info.rendererString
  p.meta.adapterArch = be.info.adapterArch
  p.meta.targetMs = be.deviceClass === 'software' ? 33.3 : refreshMs()
  // test switches only in dev/test builds (TEST_SWITCHES folds to false in production and drops the branch: M06-AC-010)
  if (TEST_SWITCHES) {
    const f = be.info.forced
    p.forced = f ? { ...(f.tier ? { tier: f.tier } : {}), ...(f.perfInject ? { perfInject: f.perfInject } : {}) } : null
    const inj = parseInject(f?.perfInject)
    INJECT.busyMs = inj.busyMs
    INJECT.renderBusyMs = inj.renderBusyMs
  } else p.forced = null
  perfStore.setState({ tier: be.tier, deviceClass: be.deviceClass, forced: be.info.forced !== null, targetMs: p.meta.targetMs, backendState: be.state })
  const motionTier = (): 'full' | 'lite' | 'reduced' | 'off' => {
    const m = typeof document !== 'undefined' ? document.documentElement.dataset.motion : 'full'
    return m === 'lite' || m === 'reduced' || m === 'off' ? m : 'full'
  }
  const dtm = (x: number, y: number): number => vp.groundAt(x, y, pointCloudServices()?.dtm ? (a, b) => pointCloudServices()!.dtm!.sample(a, b) : null)
  const rig = new CameraRig(camera, host, {
    motionTier, dtm,
    sensors: () => vp.sensors,
    focusPose: (a, pos, q, vel) => vp.drones?.fullPoseOf(a, pos, q, vel) ?? false,
    setFocus: (a) => {
      vp.drones?.layer.setFocus(a, a >= 0 ? (rig.mode === 'fpv' ? 'fpv' : 'third') : 'none')
      pointCloudServices()?.setFocus?.(null, a >= 0 ? (rig.mode === 'fpv' ? 'fpv' : 'follow') : 'none')
      ;(vp.drones?.time.interp as unknown as { setFocus?: (a: number) => void } | undefined)?.setFocus?.(a)
      perfStore.setState({ focusLowLatency: a >= 0 })
    },
    prefetch: (eye, target, fov) => pointCloudServices()?.prefetchView?.(eye, target, fov),
    onMode: (mode, followLock) => {
      vp.drones?.layer.setFocus(rig.focusAgent, mode === 'fpv' ? 'fpv' : mode === 'third' ? 'third' : 'none')
      events.emit('camera.mode', { mode, followLock })
    },
    onMoved: (active) => events.emit('camera.moved', { active }),
    onFlight: (phase, reason) => events.emit('camera.flight', { phase, reason }),
  })
  vp.rig = rig
  if (poseAcrossRebuild) {
    rig.setPose(poseAcrossRebuild, { fly: false })
    poseAcrossRebuild = null
  }
  vp.picker = new Picker({
    camera: () => vp.camera, size: () => ({ w: vp.cssW, h: vp.cssH }), poses: () => (vp.staticBrowse ? null : vp.drones?.poses ?? null),
    idOf: (a) => rtClient()?.roster.idOf(a) ?? String(a), worldId: () => vp.worldId, moving: () => rig.moving,
  })
  const plan = newPassPlan()
  const focusP = new Float64Array(3)
  let focusSent = false
  let statsFrom = 0
  const stats: ClientStats = { frameMs: 0, frameP95Ms: 0, heapMB: 0, droppedFrames: 0, pointBudget: 0, latencyP95Ms: 0, dGlobalMs: 0 }
  let lastMoveMs = Number.NEGATIVE_INFINITY
  let floorTotalMs = 0
  let floorSaved = false
  onRefreshChange((ms) => {
    if (be.deviceClass !== 'software') pointCloudServices()?.cas?.setTarget?.(ms, p.meta.tailK)
  })
  const offs = [
    register('camera', 'viewport.ctx', (ctx: FrameCtx) => {
      ctx.camera = camera
      ctx.cssW = vp.cssW
      ctx.cssH = vp.cssH
      ctx.dbW = canvas.width
      ctx.dbH = canvas.height
      ctx.dpr = vp.cssW > 0 ? canvas.width / vp.cssW : 1
      rig.update(ctx)
      ctx.moving = rig.moving
      // Follow/FPV focus position (ENU) for the point-cloud selection (M05 services.setFocus); cleared once on leaving
      const pcs = pointCloudServices()
      const fm = rig.mode === 'fpv' ? 'fpv' : rig.mode === 'third' ? 'follow' : 'none'
      if (fm !== 'none' && rig.focusAgent >= 0 && vp.drones?.poseOf(rig.focusAgent, focusP)) {
        pcs?.setFocus?.(focusP, fm)
        focusSent = true
      } else if (focusSent) {
        pcs?.setFocus?.(null, 'none')
        focusSent = false
      }
      // Tier B/A cloud scale = rung rs x motion degradation (x 0.75 while moving, back after 200 ms still)
      if (be.tier !== 'S') {
        const st = pointCloudServices()?.cas?.state() as { rs?: number } | undefined
        const rs = st?.rs ?? 1
        if (rig.moving) lastMoveMs = ctx.nowMs
        const motion = ctx.nowMs - lastMoveMs < HOST.motionRestoreMs ? HOST.motionFactor : 1
        be.setCloudScale(rs * motion)
      }
      ctx.cloudScale = be.cloudScale
      p.meta.renderScale = be.tier === 'S' ? 0.5 : be.cloudScale
    }, { order: -100 }),
    register('render', 'backend.render', (ctx: FrameCtx) => {
      // M05 EDL composite for P2 once the point-cloud layer exposes it (Tier B/A)
      if (be.tier !== 'S') be.setEdl(pointCloudServices()?.edlMaterial ?? null)
      be.plan(ctx, plan)
      be.renderFrame(ctx, plan)
      for (const s of listLayers()) {
        const L = p.layers[s.perfKey]
        if (L) L.draws = s.drawCount(ctx)
      }
    }),
    register('governor', 'perf.governor', (ctx: FrameCtx) => {
      const g = governor()
      const cas = pointCloudServices()?.cas ?? null
      g.setCas(cas)
      g.evaluate(ctx)
      if (g.labelKey !== perfStore.getState().governorLabelKey) perfStore.setState({ governorStep: g.step, governorLabelKey: g.labelKey })
      statsFrom = reportClientStats(stats, statsFrom)
      // FR-013 start-rung memory: CAS held at the floor > 30 s in this session
      const st = cas?.state() as { atFloorTotalMs?: number } | undefined
      if (!floorSaved && st?.atFloorTotalMs !== undefined) {
        floorTotalMs = st.atFloorTotalMs
        if (floorTotalMs > PREF_FLOOR_MS && be.deviceClass !== 'software') {
          floorSaved = true
          rememberFloorHeld(readCache(be.info.rendererKey), be.info.rendererKey, be.deviceClass, be.tier)
        }
      }
    }, { fps: 1 }),
  ]
  const offLost = be.onLost(() => {
    perfStore.setState({ backendState: 'LOST' })
    void requestRebuild('webglcontextlost')
  })
  // PerfGovernor step 6: motion tier cap (stores/perf.motionCap, M15 ui/motion/tier.ts takes the minimum). Tier S starts
  // at lite (one level: reduced), Tier B/A at full (lite, then reduced)
  const motionLevels: readonly (MotionCap | null)[] = be.tier === 'S' ? [null, 'reduced'] : [null, 'lite', 'reduced']
  const offMotion = perf.registerKnob({
    step: 6, id: 'motion', levels: motionLevels.length, labelKey: 'perf.governor.motion',
    apply: (l) => perfStore.setState({ motionCap: motionLevels[l] ?? (be.tier === 'S' ? null : 'full') }),
  })
  const offQuality = installQualityMasks(be)
  const offBench = installBench(be, rig, camera)
  const offReveal = perf.onReveal(() => vp.changed())
  vp.changed()
  return () => {
    for (const off of offs) off()
    offLost()
    offMotion()
    offQuality()
    offBench()
    offReveal()
    onRefreshChange(null)
    rig.dispose()
    if (vp.rig === rig) vp.rig = null
    vp.picker = null
  }
}
