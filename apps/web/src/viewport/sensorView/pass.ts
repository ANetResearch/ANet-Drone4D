// Sensor view pass (D2 WP-11; AWR-04 §10.5 "三个着色变体进入 shader zoo"; AWR-14 §20.10.5; D2-AC-21, D2-AC-25). Owner: M06.
// While an EO, night or LWIR view is shown in the FPV camera the backend splits the frame (webgl2.ts renderFrame):
//   S1  the world layers (point cloud, ground and sky, vehicles, environment, targets and their volumes, zones) into the
//       sensor target: colour + float depth texture at the drawing-buffer size (Tier B/A: the point cloud's P1/P2 as
//       usual, P2 composited into the sensor target instead of the canvas);
//   S2  the composite quad (materials.ts: one program, the view chosen by a uniform) to the canvas, depth written back;
//   S3  EO only: the overlay layers (mission, tasking) on top, depth-tested; in the night and LWIR views no overlay is
//       drawn (their colours would not be monochrome, AWR-04 §10.7), and the HUD-like 3D layers (trails, frustums,
//       glyphs, labels, debug) are never drawn in a sensor view (the HUD is M15's DOM).
// No program is compiled by switching (D1-AC-25, D2-AC-21): the sensor target is flagged as an XR render target with an
// sRGB colour texture, so three gives the world layers exactly their canvas programs (WebGLPrograms parameters
// outputColorSpace = the texture's colour space, tone mapping as on the canvas; AnetNodesHandler fix 1 encodes like the
// canvas). The shader zoo renders the world items into the sensor target as well (SwiftShader JITs its routines per
// framebuffer format, ADR-071; rendered once, see warmup.ts) and the composite material to the canvas.
// D2-FX-R2-web (D2-AC-01): the attachments have the canvas formats, RGBA8 colour (the texture keeps its sRGB colour space
// for three's program parameters, the storage is forced to RGBA8) and DEPTH24_STENCIL8 depth (the WebGL drawing buffer's
// packed depth-stencil), so SwiftShader reuses the canvas routines of every world program instead of compiling a second
// set under the boot mask (about 0.5 s on Tier S with the SRGB8_ALPHA8 colour and float depth of D2). The attachment
// stores the canvas value s as the canvas does, sampling returns s, blending works on s like the canvas does; the
// reversed-Z depth has the canvas's 24-bit precision (the composite reconstructs positions for range and haze only, and
// writes the same quantised depth back).
// One extra draw per frame in a sensor view (the composite quad); the pass plan counts exactly what is drawn.
import { DepthStencilFormat, DepthTexture, Mesh, PlaneGeometry, Scene, SRGBColorSpace, UnsignedByteType, UnsignedInt248Type, WebGLRenderTarget, type Object3D, type PerspectiveCamera } from 'three'
import { payloadIlluminators, type FrameCtx, type PayloadView } from '@/engine'
import type { LayerId, LayerSpec } from '../layers/registry'
import { vp } from '../session'
import { writeEmitters } from './emitters'
import { MODE_INDEX, makeEmitterTexture, makeSensorViewMaterial, type SensorViewMaterial } from './materials'
import { eoLook, followRange, lwirRange, nirPassive, pathApparentC, surfaceApparentC, SV, type Illuminator, type SensorViewMode } from './math'
import { sensorViewActive, sv } from './state'

/** how a layer appears in a sensor view: drawn into the sensor image, drawn over it (EO), or not drawn */
export type SensorClass = 'world' | 'overlay' | 'hidden'
const DEFAULT_CLASS: Partial<Record<LayerId, SensorClass>> = {
  pointcloud: 'world', groundSky: 'world', drones: 'world', environment: 'world', targets: 'world', keepout: 'world', zones: 'world',
  mission: 'overlay', tasking: 'overlay',
  trails: 'hidden', frustums: 'hidden', glyphs: 'hidden', labels: 'hidden', debug: 'hidden', field: 'hidden',
}
export function sensorClassOf(s: LayerSpec): SensorClass {
  return s.sensorClass ?? DEFAULT_CLASS[s.id] ?? 'overlay'
}
/** does a layer draw in the given pass of a view (S1 world, S3 overlay) */
export function drawsIn(cls: SensorClass, pass: 'world' | 'overlay', mode: SensorViewMode): boolean {
  if (pass === 'world') return cls === 'world'
  return cls === 'overlay' && mode === 'eo'
}

const GROUND_BELOW_M = 0.5
/** colour storage of the sensor target: the canvas format (see the header) */
export const SENSOR_COLOR_FORMAT = 'RGBA8'

/** illuminators of a payload message (built-in eo illuminator, external nir_illuminator entries), at most out.length, on only */
export function illuminatorsOf(pl: PayloadView | undefined, out: Illuminator[]): number {
  return payloadIlluminators(pl, out)
}

export class SensorViewPass {
  readonly scene = new Scene()
  readonly quad: Mesh
  readonly view: SensorViewMaterial
  readonly emitTex = makeEmitterTexture()
  rt: WebGLRenderTarget | null = null
  private readonly hidden: Object3D[] = []
  private readonly range = new Float64Array([Number.NaN, Number.NaN])
  private readonly rangeT = new Float64Array(2)
  private readonly look = { gain: 1, sat: 1, noise: 0 }
  private lastNowMs = Number.NaN
  /** emitters written in the last LWIR frame, and the candidates and in-view counts (tests, HUD diagnostics) */
  emitters = 0
  emitterCandidates = 0
  emittersInView = 0

  constructor(readonly reversedZ: boolean, private readonly countAlloc: () => void) {
    this.view = makeSensorViewMaterial(reversedZ, this.emitTex)
    this.quad = new Mesh(new PlaneGeometry(2, 2), this.view.material)
    this.quad.frustumCulled = false
    this.quad.name = 'SensorViewQuad'
    this.scene.add(this.quad)
  }

  /** the sensor target at the drawing-buffer size; (re)allocated only when the size changes */
  ensure(w: number, h: number): WebGLRenderTarget {
    const W = Math.max(1, Math.round(w))
    const H = Math.max(1, Math.round(h))
    if (this.rt && this.rt.width === W && this.rt.height === H) return this.rt
    this.rt?.dispose()
    const rt = new WebGLRenderTarget(W, H, { depthBuffer: true, type: UnsignedByteType, colorSpace: SRGBColorSpace })
    rt.depthTexture = new DepthTexture(W, H, UnsignedInt248Type)
    rt.depthTexture.format = DepthStencilFormat
    rt.texture.name = 'rt.sensor'
    ;(rt.texture as unknown as { internalFormat: string }).internalFormat = SENSOR_COLOR_FORMAT
    // canvas programs for the world layers (see the header): three reads the output colour space of an XR target from
    // its texture and keeps the canvas tone mapping; nothing else in r186 treats the flag differently on this path
    ;(rt as unknown as { isXRRenderTarget: boolean }).isXRRenderTarget = true
    this.countAlloc()
    this.rt = rt
    this.view.bindTargets(rt.texture, rt.depthTexture)
    return rt
  }

  /** the view is drawn this frame */
  active(): boolean {
    return sensorViewActive() && this.rt !== null
  }

  /** hide the layer roots that do not draw in this pass (restore() puts the live state back) */
  hideFor(pass: 'world' | 'overlay', layers: readonly LayerSpec[]): void {
    const mode = sv.mode
    for (let i = 0; i < layers.length; i++) {
      const s = layers[i]
      const r = s.root
      if (!r || !r.visible || drawsIn(sensorClassOf(s), pass, mode)) continue
      r.visible = false
      this.hidden.push(r)
    }
  }
  restore(): void {
    for (let i = 0; i < this.hidden.length; i++) this.hidden[i].visible = true
    this.hidden.length = 0
  }

  /** draws of the frame in a sensor view: world + overlay (EO) layers, plus the composite quad */
  plannedDraws(ctx: FrameCtx, layers: readonly LayerSpec[], channelCloud: (s: LayerSpec) => boolean): { cloud: number; world: number; overlay: number } {
    let cloud = 0
    let world = 0
    let overlay = 0
    const mode = sv.mode
    for (let i = 0; i < layers.length; i++) {
      const s = layers[i]
      const cls = sensorClassOf(s)
      const n = s.drawCount(ctx)
      if (n === 0) continue
      if (channelCloud(s)) cloud += n
      else if (drawsIn(cls, 'world', mode)) world += n
      else if (drawsIn(cls, 'overlay', mode)) overlay += n
    }
    return { cloud, world, overlay }
  }

  /** world phase: uniforms of the shown view (camera matrices, light, illuminators, temperatures, emitters, gain) */
  update(ctx: FrameCtx): void {
    if (!sensorViewActive()) {
      this.lastNowMs = Number.NaN
      return
    }
    const cam = ctx.camera as PerspectiveCamera | null
    if (!cam || sv.mode === 'fpv') return
    const u = this.view.u
    u.mode.value = MODE_INDEX[sv.mode]
    u.projInv.value.copy(cam.projectionMatrixInverse)
    u.camWorld.value.copy(cam.matrixWorld)
    u.camPos.value.setFromMatrixPosition(cam.matrixWorld)
    u.res.value.set(Math.max(1, ctx.dbW), Math.max(1, ctx.dbH))
    u.seed.value = ctx.frameNo % 97
    u.zoom.value = sv.mode === 'lwir' ? sv.digitalZoom : 1
    // the ground grid's plane (groundSky.materials GROUND.belowGroundM below the world ground level)
    u.groundY.value = (vp.worldCtx?.groundZ ?? 0) - GROUND_BELOW_M
    const env = sv.env
    if (sv.mode === 'eo') {
      const l = eoLook(env.illumLx, this.look)
      u.gain.value = l.gain
      u.sat.value = l.sat
      u.noise.value = l.noise
    } else if (sv.mode === 'nir') this.updateNir(env.illumLx)
    else this.updateLwir(ctx, cam)
  }

  private readonly ills: Illuminator[] = [{ on: false, divergenceRad: 0, rangeM: 0 }, { on: false, divergenceRad: 0, rangeM: 0 }]
  /** illuminators drawn in the last night frame (tests, HUD) */
  illuminators = 0

  private updateNir(eLx: number): void {
    const u = this.view.u
    u.kPas.value = nirPassive(eLx)
    const rig = vp.rig
    let n = 0
    if (sv.illumOverride) {
      for (; n < Math.min(2, sv.illumOverride.length); n++) Object.assign(this.ills[n], sv.illumOverride[n])
    } else n = illuminatorsOf(rig ? vp.sensors?.payloadOf?.(rig.focusAgent) : undefined, this.ills)
    let k = 0
    for (let i = 0; i < n; i++) {
      const il = this.ills[i]
      if (!il.on || !(il.divergenceRad > 0) || !(il.rangeM > 0)) continue
      const h = il.divergenceRad / 2
      const ill = k === 0 ? u.ill0 : u.ill1
      ill.value.set(1, Math.cos(h * (1 - SV.coneSoft)), Math.cos(Math.min(Math.PI / 2, h * (1 + SV.coneSoft))))
      if (k === 0) u.r0.value = il.rangeM
      else u.r1.value = il.rangeM
      k++
    }
    this.illuminators = k
    for (; k < 2; k++) (k === 0 ? u.ill0 : u.ill1).value.set(0, 1, 1)
  }

  private updateLwir(ctx: FrameCtx, cam: PerspectiveCamera): void {
    const env = sv.env
    const u = this.view.u
    u.tAir.value = env.tAirC
    u.tSurf.value = env.tSurfaceC
    u.tSky.value = env.tSkyC
    u.epsBg.value = env.emissivityBg
    u.sigma.value = env.sigmaLwPerM
    const s = env.sunEnu
    const elDeg = (Math.asin(Math.max(-1, Math.min(1, s[2]))) * 180) / Math.PI
    const day = Math.min(1, Math.max(0, (elDeg + 3) / 12))
    u.day.value = day
    // structures (roofs, walls) above the ground keep more heat than the open ground, most visibly at night
    u.structK.value = 1.5 + (1 - day)
    u.sun.value.set(s[0], s[2], -s[1])
    u.blackHot.value = sv.palette === 'black_hot' ? 1 : 0
    const f = writeEmitters(cam, env, this.emitTex.image.data as Float32Array)
    this.emitTex.needsUpdate = true
    u.count.value = f.count
    this.emitters = f.count
    this.emitterCandidates = f.candidates
    this.emittersInView = f.inView
    const bg = pathApparentC(surfaceApparentC(env.tSurfaceC, env.emissivityBg, env.tSkyC), env.tAirC, env.sigmaLwPerM, 150)
    lwirRange(bg, f.hotC, this.rangeT)
    const dt = Number.isFinite(this.lastNowMs) ? (ctx.nowMs - this.lastNowMs) / 1000 : Infinity
    this.lastNowMs = ctx.nowMs
    if (dt === Infinity) {
      this.range[0] = Number.NaN
      this.range[1] = Number.NaN
    }
    followRange(this.range, this.rangeT, dt)
    u.lo.value = this.range[0]
    u.hi.value = this.range[1]
  }

  /** the gain range of the LWIR view (deg C) */
  get lwirRange(): readonly [number, number] {
    return [this.range[0], this.range[1]]
  }

  dispose(): void {
    this.rt?.dispose()
    this.rt = null
    this.view.dispose()
    this.quad.geometry.dispose()
    this.emitTex.dispose()
  }
}

export { SV }
