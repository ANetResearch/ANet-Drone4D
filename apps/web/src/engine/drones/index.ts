// engine/drones facade and runtime (M06 §6.9, §7.1, FR-030; M12 §7.1; M11 §7). Owner: M06.
// telemetry phase: RtClient.swapFrame() -> M12 time.ingest(frame) -> __perf.net (AD-06: 3-slot ring, zero allocation);
// drones phase: DroneLayer.update (interpolation at tRender, buckets, instances, glyphs, trails, focus set), latency
// metrics (tSimToPixel of the focus or selected vehicle, pending cmdToVisible checks, HOLD and extrapolation frames);
// world phase (order 10, after m13.gimbal): sensor frustums. Epoch changes clear the trails; RESET clears a producer.
import type { RateClass, RtClient, TelemetryFrame } from '@/net/rt/types'
import { events, register, type RenderBackendView } from '../loop'
import { latency, perfProbe, pushRing } from '../perf/index'
import { initTime, type DronePoseSoA, type TimeRuntime } from '../time/index'
import { DroneLayer, DRONES, MARK, type InterpView } from './DroneLayer'
import type { SensorsApi } from './frustums'
import { hypot3 } from '../hypot'

export { DroneLayer, DRONES, MARK, alertLevelOf, type InterpView, type RosterLike, type DroneLayerOptions } from './DroneLayer'
export { Buckets, BUCKET, BUCKETS, type BucketInputs } from './buckets'
export { FocusSet, FOCUS } from './focusSet'
export { FrustumLayer, FRUSTUM, frustumLength, newestSample, SENSOR_ACTIVE, SENSOR_FOV_VALID, type SensorView, type SensorsApi } from './frustums'
export { GlyphLayer, GLYPH, GlyphClass, Palette, Shape } from './glyph/GlyphLayer'
export { MarkerBatch, MARKER, MarkerStyle } from './markers'
export { LowPolyBatch, HeroBatch, DEFAULT_SUN } from './models'
export { makeLowPolyGeometry, makeHeroPlaceholderGeometry, triangleCount, visRadius, LOWPOLY } from './lowpoly'
export { TrailRing, TRAIL } from './trails/TrailRing'
export { TrailBatch, TRAIL_STYLES } from './trails/TrailBatch'
export { heroOf, setHeroSource, setProceduralHero, bakeFlu, FLU_FROM_GLTF } from './vehicleModels'
export { makeInstanceTexture, quadVertex, clipQuad } from './screenQuad'

export interface DroneRuntime {
  readonly layer: DroneLayer
  /**
   * the TelemetryFrame of the latest swap (valid until the next swap; null before the first) and its sequence number:
   * other engine modules read raw records from it in the same frame, e.g. M07's EnvSample32 (M07-to-M06 item 2)
   */
  readonly frame: TelemetryFrame | null
  readonly frameSeq: number
  readonly time: TimeRuntime
  readonly poses: DronePoseSoA
  /** latest rendered pose of an agent (ENU m); false when unknown */
  poseOf(agentNo: number, out: Float64Array): boolean
  /** position (3), quaternion [x, y, z, w] (4) and velocity (3) of the rendered pose */
  fullPoseOf(agentNo: number, pos: Float64Array, quat: Float64Array, vel: Float64Array): boolean
  setSelected(agentNo: number): void
  setSensors(api: SensorsApi | null): void
  dispose(): void
}

export interface DroneRuntimeOptions {
  motionTier?: () => 'full' | 'lite' | 'reduced' | 'off'
  capacity?: number
}

export function createDroneRuntime(be: RenderBackendView, rt: () => RtClient | null, o: DroneRuntimeOptions = {}): DroneRuntime {
  const time = initTime({ register })
  const interp = time.interp as unknown as InterpView
  let sensors: SensorsApi | null = null
  const layer = new DroneLayer({
    be, capacity: o.capacity ?? DRONES.capacity,
    interp: { sampleSwarm: (tS, out) => time.sampleSwarm(tS, out), sampleOne: interp.sampleOne?.bind(interp), setFocus: interp.setFocus?.bind(interp) },
    roster: () => rt()?.roster ?? null,
    subscribe: (topic, rate) => rt()?.subscribe(topic, { rate: rate as RateClass }) ?? noop,
    sensors: () => sensors,
    motionTier: o.motionTier,
    frozen: () => !time.clock.advancing && !time.clock.stale,
  })
  const poses = layer.poses
  const p = perfProbe()
  const lat = latency()
  const holdState = new Uint8Array(65536)
  const prevPos = new Float64Array(3 * 65536)
  const prevVel = new Float64Array(3 * 65536)
  let lastEpoch = -1
  let frame: TelemetryFrame | null = null
  let frameSeq = 0
  const offs = [
    () => time.dispose(),
    register('telemetry', 'rt.swap', (ctx) => {
      const c = rt()
      lat.frameStart(time.clock.simNowS(), time.clock.rate, time.clock.advancing)
      if (!c) return
      const f = c.swapFrame()
      if (!f) return
      time.ingest(f, ctx.nowMs)
      frame = f
      frameSeq++
      // M13 sensor poses (SensorPose48 raw items) and body orientations of the same frame (M13-to-M06 item 1; FX-WEB1)
      sensors?.ingestFrame?.(f)
      const h = f.hdr
      if (lastEpoch >= 0 && h.epoch !== lastEpoch) layer.clearTrails()
      lastEpoch = h.epoch
      const net = p.net
      net.swarmHz = h.swarmHz
      net.focusHz = h.focusHz
      net.selectedHz = h.selHz
      net.reconnects = h.reconnects
      net.epoch = h.epoch
      net.eventGaps = h.eventGaps
      net.rttMs = h.srttMs
      net.clockOffsetMs = h.clockOffsetMainMs
      net.bytesPerS = h.bytesPerS
      pushRing(net.decodeUs, h.decodeMs * 1000)
      if (Number.isFinite(h.ageMs)) pushRing(net.ageMs, h.ageMs)
    }, { layer: 'drones' }),
    register('drones', 'drones.update', (ctx) => {
      layer.update(ctx)
      p.latency.dGlobalMs = time.clock.dGlobalMs
      // latency metrics (focus vehicle, else the primary selection)
      const who = layer.focusAgent >= 0 ? layer.focusAgent : layer.primary
      let anyHold = false
      let presented = false
      for (let i = 0; i < poses.n; i++) {
        const a = poses.agentNo[i]
        const hold = poses.hold[i] === 1
        if (hold && (layer.mark[a] & MARK.HIDDEN) === 0) anyHold = true
        if (hold !== (holdState[a] === 1)) {
          holdState[a] = hold ? 1 : 0
          if (a === layer.primary || a === layer.focusAgent) events.emit('focus.hold', { id: rt()?.roster.idOf(a) ?? String(a), hold })
        }
        if (a === who || lat.isPending(a)) {
          const tPose = layer.focusAgent === a ? ctx.tFocusS : ctx.tRenderS
          if (a === who) {
            const extrap = poses.ageS[i] > 0 && poses.sampleT[i] / 1000 < tPose
            lat.presented(a, tPose, hold, extrap)
            presented = true
          }
          // commands are checked for every vehicle that has one pending (a command to a vehicle other than the
          // Third/FPV focus one, D1-AC-26 command to visible)
          lat.check(a, ctx.nowMs, poses.pos[3 * i], poses.pos[3 * i + 1], poses.pos[3 * i + 2], poses.vel[3 * i], poses.vel[3 * i + 1], poses.vel[3 * i + 2],
            poses.state[i], time.interp.stateSinceMs(a) / 1000, tPose, time.interp.ctrlOf(a))
        }
      }
      if (!presented) lat.presented(-1, 0, false, false)
      if (anyHold) lat.holdFrame()
      // focus set entries and exits: position discontinuity against the previous extrapolation
      const fs = layer.focus
      for (let j = 0; j < fs.changedN; j++) {
        const a = fs.changed[j]
        const i = layer.poseIndexOf(a)
        if (i < 0 || ctx.dtMs <= 0) continue
        const dt = ctx.dtMs / 1000
        const ex = prevPos[3 * a] + prevVel[3 * a] * dt
        const ey = prevPos[3 * a + 1] + prevVel[3 * a + 1] * dt
        const ez = prevPos[3 * a + 2] + prevVel[3 * a + 2] * dt
        lat.focusJump(hypot3(poses.pos[3 * i] - ex, poses.pos[3 * i + 1] - ey, poses.pos[3 * i + 2] - ez))
      }
      fs.changedN = 0
      for (let i = 0; i < poses.n; i++) {
        const a = poses.agentNo[i]
        for (let k = 0; k < 3; k++) {
          prevPos[3 * a + k] = poses.pos[3 * i + k]
          prevVel[3 * a + k] = poses.vel[3 * i + k]
        }
      }
    }, { layer: 'drones' }),
    register('world', 'drones.frustums', (ctx) => layer.updateFrustums(ctx), { order: 10, layer: 'frustums' }),
  ]
  return {
    layer, time, poses,
    get frame() {
      return frame
    },
    get frameSeq() {
      return frameSeq
    },
    poseOf(agentNo, out) {
      const i = layer.poseIndexOf(agentNo)
      if (i < 0) return false
      out[0] = poses.pos[3 * i]
      out[1] = poses.pos[3 * i + 1]
      out[2] = poses.pos[3 * i + 2]
      return true
    },
    fullPoseOf(agentNo, pos, quat, vel) {
      const i = layer.poseIndexOf(agentNo)
      if (i < 0) return false
      for (let k = 0; k < 3; k++) {
        pos[k] = poses.pos[3 * i + k]
        vel[k] = poses.vel[3 * i + k]
      }
      for (let k = 0; k < 4; k++) quat[k] = poses.quat[4 * i + k]
      return true
    },
    setSelected(agentNo) {
      layer.setHighlights(agentNo >= 0 ? [agentNo] : [], agentNo, layer.hover)
    },
    setSensors(api) {
      sensors = api
      // the page motion tier drives M13's gimbal damping (reduced and off snap to the target)
      const mt = o.motionTier
      api?.setMotionTier?.(mt ? () => {
        const m = mt()
        return m === 'off' ? 'reduced' : m
      } : null)
    },
    dispose() {
      for (const off of offs) off()
      layer.dispose()
    },
  }
}

const noop = (): void => {}
