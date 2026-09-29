// Environment runtime: the per-page engine object behind viewport/layers/environment.tsx (M07 §6.1, §6.8, §7.2). Owner: M07.
// Owns the EnvStore, the EnvParams written each frame, the shared assets (weather map; turbulence box on demand), the
// Low objects (rain streaks with a second box for octave cross-fades, snow and dust points, wind arrows, the interim 2D
// cloud quad) and the quality state. update(ctx) runs in the loop 'world' phase: store.update(tRender) then EnvParams
// (float64 reductions on the CPU), precipitation anchor, live counts (drawRange only), visibility and perf counters.
// Nothing here allocates per frame; nothing imports stores, React or viewport (AWR-03 §4.2).
import { Group, type Data3DTexture, type PerspectiveCamera } from 'three'
import { PALETTE_LINEAR } from '@/lib/tokens/palette.gen'
import type { FrameCtx, RenderBackendView } from '../loop'
import { eDir, mod } from './state/conventions'
import { EnvStore } from './state/EnvStore'
import { F } from './state/presets'
import { weatherUrl, turbUrl } from './state/keyframe'
import { EnvParams, horizonColor, MAX_GUSTS } from './lighting/EnvUniforms'
import { createEnvShading, type SceneShadingProvider } from './lighting/EnvShading'
import { sceneFogNode } from './atmosphere/fogNode'
import { makeEnvNodes } from './lighting/EnvUniforms'
import { EnvTerrain, type DtmSource } from './terrain/dtmSampler'
import { WeatherMap } from './clouds/WeatherMap'
import { Cloud2DLayer } from './clouds/Cloud2D'
import { RainStreaks } from './precip/RainStreaks'
import { PrecipPoints } from './precip/SnowPoints'
import { PrecipAnchor } from './precip/PrecipAnchor'
import { WindArrows } from './wind/WindArrows'
import { Streamlines } from './wind/Streamlines'
import { decodeAWRV, volumeTexture3D } from './wind/awrv'
import { TurbBoxCPU } from './wind/turbBox'
import { fAdv, profileCfg } from './wind/profile'
import { EnvQuality, type EnvUserLevel } from './quality/EnvQuality'
import { ENV_TIERS, arrowCount } from './quality/envTiers'

type N = any // TSL nodes

export interface EnvSubLayers { precip: boolean; clouds: boolean; arrows: boolean; arrowsSliceAglM: number; streamlines: boolean }

export interface EnvRuntimeOptions {
  be: RenderBackendView
  reversedZ: boolean
  /** GET bytes of a same-origin URL */
  fetchBytes: (url: string) => Promise<ArrayBuffer>
  /** GET bytes of an authenticated /api URL (streamlines); defaults to fetchBytes */
  fetchApiBytes?: (url: string) => Promise<ArrayBuffer>
}

export interface EnvPerf {
  quality: string
  state: string
  version: number
  stale: boolean
  anchorDriftM: number
  live: { rain: number; snow: number; dust: number; arrows: number }
  draws: number
  verts: number
  assets: { weather: boolean; turb: boolean }
}

const TWO_PI = 2 * Math.PI

export class EnvironmentRuntime {
  readonly store = new EnvStore()
  readonly params = new EnvParams()
  readonly terrain = new EnvTerrain()
  readonly weather = new WeatherMap()
  readonly anchor = new PrecipAnchor()
  readonly quality: EnvQuality
  readonly root = new Group()
  readonly rain: RainStreaks
  readonly rainPrev: RainStreaks
  readonly snow: PrecipPoints
  readonly dust: PrecipPoints
  readonly arrows: WindArrows
  readonly cloud2d: Cloud2DLayer
  /** AWSL streamlines (D1-ext): Tier B/A only */
  readonly streamlines: Streamlines | null
  readonly shading: SceneShadingProvider
  /** scene fog node, set once on the scene by the adapter (M06 §6.4 rule 2) */
  readonly fogNode: N
  readonly sub: EnvSubLayers = { precip: true, clouds: true, arrows: false, arrowsSliceAglM: 50, streamlines: false }
  masterVisible = true
  turb: { cpu: TurbBoxCPU; tex: Data3DTexture } | null = null
  readonly perf: EnvPerf = { quality: 'low', state: 'EMPTY', version: 0, stale: false, anchorDriftM: 0, live: { rain: 0, snow: 0, dust: 0, arrows: 0 },
    draws: 0, verts: 0, assets: { weather: false, turb: false } }
  /** lighting outputs for the point cloud (ENU unit sun vector, cloud shadow at the focus) and the sky hook */
  readonly sunEnu = new Float64Array([0, 0, 1])
  cloudShadowFocus = 1
  private weatherSeed = -1
  private turbWanted = false
  private turbLoading = false
  private readonly caps: { rain: number; snow: number; dust: number }
  private readonly e2 = new Float64Array(2)
  private readonly cam = new Float64Array(3)
  private readonly focus = new Float64Array(3)
  private spacingIdx = 2
  private readonly tier: 'A' | 'B' | 'S'

  constructor(private readonly o: EnvRuntimeOptions) {
    const be = o.be
    this.tier = be.tier
    this.quality = new EnvQuality(be.tier, be.deviceClass, false)
    const dc = be.deviceClass
    const c = ENV_TIERS.low[dc]
    this.caps = { rain: be.tier === 'S' ? ENV_TIERS.tierSQuads : c.rain, snow: be.tier === 'S' ? ENV_TIERS.tierSQuads : c.snow,
      dust: be.tier === 'S' ? ENV_TIERS.tierSQuads : c.dust }
    const p = this.params
    this.rain = new RainStreaks(this.caps.rain, p, this.terrain)
    this.rainPrev = new RainStreaks(this.caps.rain, p, this.terrain)
    this.rainPrev.mesh.name = 'EnvRainFade'
    this.snow = new PrecipPoints('snow', this.caps.snow, p, this.terrain, () => be.createPointsMaterial(), be.pointSizeMode)
    this.dust = new PrecipPoints('dust', this.caps.dust, p, this.terrain, () => be.createPointsMaterial(), be.pointSizeMode)
    this.arrows = new WindArrows(p, this.terrain)
    this.cloud2d = new Cloud2DLayer(p, this.weather, o.reversedZ)
    this.root.name = 'EnvironmentLayer'
    this.root.add(this.cloud2d.mesh, this.dust.mesh, this.rain.mesh, this.rainPrev.mesh, this.snow.mesh, this.arrows.mesh)
    this.streamlines = be.tier === 'S' ? null : new Streamlines(p, o.fetchApiBytes ?? o.fetchBytes)
    if (this.streamlines) this.root.add(...this.streamlines.meshes)
    this.shading = createEnvShading(p, this.weather)
    this.fogNode = sceneFogNode(makeEnvNodes(p))
    const P = this.params
    const z = PALETTE_LINEAR.g950
    P.zenithColor.set(z[0], z[1], z[2])
  }

  setDtmSource(src: DtmSource | null, groundZ: number): void {
    this.terrain.src = src
    this.terrain.groundZ = groundZ
    this.params.groundZ = groundZ
  }

  setUserQuality(l: EnvUserLevel): void {
    this.quality.setUserLevel(l)
  }

  /** load the turbulence box (Med, env-gpu tests, local turbulence for the selected vehicle) */
  wantTurbulence(): void {
    this.turbWanted = true
  }

  private loadAssets(): void {
    const kf = this.store.current
    if (!kf) return
    const cfg = kf.wire.config
    if (kf.seed !== this.weatherSeed) {
      this.weatherSeed = kf.seed
      void this.weather.load(weatherUrl(kf.seed, cfg.weather_map.n), this.o.fetchBytes).then(() => {
        this.perf.assets.weather = this.weather.loaded
      })
    }
    if (this.turbWanted && !this.turb && !this.turbLoading) {
      this.turbLoading = true
      const t = cfg.wind.turbulence
      void this.o.fetchBytes(turbUrl(kf.seed, t.n, t.dx_m, t.l_m)).then((b) => {
        const v = decodeAWRV(b)
        this.turb = { cpu: new TurbBoxCPU(v), tex: volumeTexture3D(v) }
        this.perf.assets.turb = true
      }).catch(() => {
        this.perf.assets.turb = false
      }).finally(() => {
        this.turbLoading = false
      })
    }
  }

  /** world phase, zero allocation */
  update(ctx: FrameCtx, focusEnu: ArrayLike<number> | null, forceNear: boolean): void {
    const st = this.store
    st.update(ctx.tRenderS, ctx.nowMs)
    this.terrain.sync()
    const kf = st.current
    const perf = this.perf
    perf.state = st.state
    perf.stale = st.state === 'STALE'
    perf.version = st.version
    perf.anchorDriftM = st.anchorDriftM
    perf.quality = this.quality.level
    if (!kf) {
      this.hideAll()
      return
    }
    this.loadAssets()
    const P = this.params
    const s = st.scalars
    const vis = st.damper.vis
    const d = st.derivedVis
    const A = st.anchors
    const Av = st.anchorsVis
    const prof = kf.profile
    const fa = fAdv(prof)
    const cfg = kf.wire.config
    // camera
    const cam = ctx.camera as PerspectiveCamera | null
    if (cam) {
      const pos = cam.position
      this.cam[0] = pos.x
      this.cam[1] = -pos.z
      this.cam[2] = pos.y
      P.camEnu.set(this.cam[0], this.cam[1], this.cam[2])
      P.pixelWorldScale = (2 * Math.tan((cam.fov * Math.PI) / 360)) / Math.max(1, ctx.dbH)
    }
    P.viewportPx.set(Math.max(1, ctx.dbW), Math.max(1, ctx.dbH))
    P.dpr = ctx.dpr
    if (focusEnu) {
      this.focus[0] = focusEnu[0]
      this.focus[1] = focusEnu[1]
      this.focus[2] = focusEnu[2]
    } else this.focus.set(this.cam)
    // sun (azimuth clockwise from north, elevation)
    const az = (cfg.sun.azimuth_deg * Math.PI) / 180
    const el = (cfg.sun.elevation_deg * Math.PI) / 180
    const se = this.sunEnu
    se[0] = Math.sin(az) * Math.cos(el)
    se[1] = Math.cos(az) * Math.cos(el)
    se[2] = Math.sin(el)
    P.sunDirEnu.set(se[0], se[1], se[2])
    P.sunDirThree.set(se[0], se[2], -se[1])
    // fog and sky (visual values: damped on step changes)
    P.groundZ = this.terrain.groundZ
    P.sigmaHaze0 = d.sigma_haze0
    P.sigmaFog = d.sigma_fog
    P.sigmaPrecip = d.sigma_precip
    P.fogTop = vis[F.FOG_TOP]
    P.precipTop = vis[F.BASE]
    horizonColor(vis[F.HORIZON], P.fogColor)
    // clouds
    const on = this.masterVisible && this.quality.level !== 'off'
    P.visualOn = on ? 1 : 0
    P.cloudsOn = this.sub.clouds ? 1 : 0
    P.sunVis = d.sun_vis
    P.cloudCover = vis[F.COVER]
    P.cloudOD = d.cloud_od
    P.cloudHmid = vis[F.BASE] + ENV_TIERS.cloudHmidFrac * (vis[F.TOP] - vis[F.BASE])
    P.cloud2DAlphaMax = vis[F.CLOUD2D]
    P.shadowStrength = (ENV_TIERS.shadowMaxStrength * vis[F.CLOUD2D]) / 0.5
    const scale = cfg.weather_map.scale_m
    P.weatherScale = scale
    const fh = profileCfg(P.cloudHmid, prof)
    P.cloudOffset.set(mod(-fh * Av.d[0], scale), mod(-fh * Av.d[1], scale))
    // wind (physics truth, not damped: arrows show the flying field)
    eDir(s[F.DIR], this.e2)
    P.windS = s[F.SPEED_REF]
    P.windE.set(this.e2[0], this.e2[1])
    P.wMean = s[F.W_MEAN]
    P.profKind = prof.kind === 'log' ? 0 : prof.kind === 'power' ? 1 : 2
    P.profZ0 = prof.z0_m
    P.profD = prof.d_m
    P.profZref = prof.z_ref_m
    P.profAlpha = prof.alpha
    P.fAdv = fa
    P.level = kf.level
    for (let k = 0; k < MAX_GUSTS; k++) {
      const ev = kf.events[k]
      const ga = P.gustA[k]
      const gb = P.gustB[k]
      if (!ev || ev.kind !== 1) {
        gb.set(0, 0, 1, 0)
        continue
      }
      const ex = -Math.sin((ev.dirFromDeg * Math.PI) / 180)
      const ey = -Math.cos((ev.dirFromDeg * Math.PI) / 180)
      ga.set(fa * A.sM - ev.x0 + ev.s0, ex, ey, 0)
      gb.set(ev.amp, TWO_PI / ev.lam, ev.lam, 1)
    }
    P.turbD.set(mod(fa * A.d[0], 256), mod(fa * A.d[1], 256), mod(fa * A.d[2], 256))
    P.turbSigmaRef = s[F.SIGMA_REF]
    P.turbOn = kf.turbModel === 'box' && this.quality.level === 'med' ? 1 : 0
    P.vmax = kf.wire.vis.vmax_mps
    // precipitation
    P.rainK = d.rain_k
    P.snowK = d.snow_k
    P.dustK = vis[F.DUST]
    P.rainLambda = d.mp_lambda
    P.windOffset.set(mod(Av.d[0], ENV_TIERS.precipPMax), mod(Av.d[1], ENV_TIERS.precipPMax), 0)
    const r = ENV_TIERS.fallRatios
    const hm = ENV_TIERS.precipHgtMax
    P.fallPhase4.set(mod(Av.fallRain * r[0], hm), mod(Av.fallRain * r[1], hm), mod(Av.fallRain * r[2], hm), mod(Av.fallRain * r[3], hm))
    P.fallSpeed4.set(d.v_rain_mps * r[0], d.v_rain_mps * r[1], d.v_rain_mps * r[2], d.v_rain_mps * r[3])
    P.snowPhase = mod(Av.fallSnow, hm)
    P.wetness = Av.wetness
    P.puddle = Av.puddle
    // precipitation anchor octave (camera AGL from the DTM)
    const camAgl = this.cam[2] - this.terrain.sample(this.cam[0], this.cam[1])
    const gFocus = this.terrain.sample(this.focus[0], this.focus[1])
    this.anchor.update(this.cam, this.focus, camAgl, gFocus, forceNear, ctx.nowMs)
    const an = this.anchor.anchor
    const fz = profileCfg(Math.max(an[2] - this.terrain.sample(an[0], an[1]), 0), prof)
    P.precipWind.set(s[F.SPEED_REF] * fz * this.e2[0], s[F.SPEED_REF] * fz * this.e2[1], s[F.W_MEAN])
    P.precipAnchor.set(an[0], an[1], an[2])
    P.precipR = this.anchor.R
    P.precipH = this.anchor.H
    // arrows: origin snapped to the spacing octave around the focus
    const camDist = Math.hypot(this.cam[0] - this.focus[0], this.cam[1] - this.focus[1], this.cam[2] - this.focus[2])
    this.spacingIdx = arrowSpacingIdx(camDist, this.spacingIdx)
    const sp = ENV_TIERS.arrowSpacingM[this.spacingIdx]
    P.arrowSpacing = sp
    P.arrowOrigin.set(Math.round(this.focus[0] / sp) * sp, Math.round(this.focus[1] / sp) * sp, 0)
    P.arrowSliceAgl = this.sub.arrowsSliceAglM
    // cloud shadow at the focus for the point cloud (CPU, same mask as the GPU)
    const zAgl = this.focus[2] - P.groundZ
    const kk = (P.cloudHmid - zAgl) / Math.max(se[2], 0.15)
    const mask = this.weather.maskAt(this.focus[0] + se[0] * kk, this.focus[1] + se[1] * kk, P.cloudCover, P.cloudOffset.x, P.cloudOffset.y, scale)
    const ks = on && this.sub.clouds ? P.shadowStrength : 0
    this.cloudShadowFocus = 1 + (Math.exp(-mask * P.cloudOD) - 1) * ks
    if (this.streamlines) {
      this.streamlines.enabled = on && this.sub.streamlines
      this.streamlines.update(s[F.DIR], kf.wire.vis.streamlines, Av.sM, ctx.nowMs, this.store.damper.reduced)
    }
    this.applyCounts(on)
  }

  private applyCounts(on: boolean): void {
    const st = this.store
    const d = st.derivedVis
    const P = this.params
    const precipOn = on && this.sub.precip
    const arrowsOn = on && this.sub.arrows
    let budget = this.tier === 'S' ? ENV_TIERS.tierSQuads - (arrowsOn ? arrowCount() : 0) : Number.POSITIVE_INFINITY
    let nRain = precipOn ? Math.floor(Math.min(this.caps.rain, budget) * d.rain_k) : 0
    let nSnow = precipOn ? Math.floor(Math.min(this.caps.snow, budget) * d.snow_k) : 0
    let nDust = precipOn ? Math.floor(Math.min(this.caps.dust, budget) * P.dustK) : 0
    if (this.tier === 'S') {
      // at most two precipitation kinds (the strongest), sharing the quad budget
      const ks = [d.rain_k, d.snow_k, P.dustK]
      const weakest = ks.indexOf(Math.min(...ks))
      if (nRain > 0 && nSnow > 0 && nDust > 0) {
        if (weakest === 0) nRain = 0
        else if (weakest === 1) nSnow = 0
        else nDust = 0
      }
      const sum = nRain + nSnow + nDust
      if (sum > budget) {
        const f = budget / sum
        nRain = Math.floor(nRain * f)
        nSnow = Math.floor(nSnow * f)
        nDust = Math.floor(nDust * f)
      }
      budget -= nRain + nSnow + nDust
    }
    const a = this.anchor
    const fading = a.prevLevel >= 0
    // cross-fade: 60 / 40 split of the same total
    const nCur = fading ? Math.floor(nRain * 0.6) : nRain
    this.rain.setLive(nCur)
    this.rain.setBox(a.anchor, a.R, a.H, fading ? a.fade : 1)
    this.rainPrev.setLive(fading ? nRain - nCur : 0)
    if (fading) this.rainPrev.setBox(a.prevAnchor, ENV_TIERS.precipR[a.prevLevel], ENV_TIERS.precipH[a.prevLevel], 1 - a.fade)
    this.snow.setLive(nSnow)
    this.snow.setBox(a.anchor, a.R, a.H, 1)
    this.dust.setLive(nDust)
    this.dust.setBox(a.anchor, a.R, a.H, 1)
    this.arrows.setVisible(arrowsOn)
    this.cloud2d.mesh.visible = on && this.sub.clouds && P.cloud2DAlphaMax > 0
    const perf = this.perf
    perf.live.rain = nRain
    perf.live.snow = nSnow
    perf.live.dust = nDust
    perf.live.arrows = arrowsOn ? arrowCount() : 0
    perf.draws = this.drawCount()
    perf.verts = 6 * (this.rain.live + this.rainPrev.live) + nSnow + nDust + (arrowsOn ? 6 * arrowCount() : 0) + (this.cloud2d.mesh.visible ? 6 : 0)
  }

  private hideAll(): void {
    this.rain.setLive(0)
    this.rainPrev.setLive(0)
    this.snow.setLive(0)
    this.dust.setLive(0)
    this.arrows.setVisible(false)
    this.cloud2d.mesh.visible = false
    if (this.streamlines) this.streamlines.enabled = false
    this.perf.draws = 0
    this.perf.verts = 0
  }

  drawCount(): number {
    if (!this.root.visible) return 0
    return this.rain.drawCount() + this.rainPrev.drawCount() + this.snow.drawCount() + this.dust.drawCount() + this.arrows.drawCount() + this.cloud2d.drawCount() +
      (this.streamlines ? this.streamlines.drawCount() : 0)
  }

  /** shader zoo: every environment material drawn once under the mask (ADR-007; D1-AC-25) */
  warmupObjects(): { object: Group | THREEObj; before(): void; after(): void }[] {
    const items: { object: THREEObj; before(): void; after(): void }[] = []
    const add = (o: THREEObj, count: number): void => {
      items.push({
        object: o,
        before: () => {
          ;(o as unknown as { geometry: { setDrawRange(a: number, b: number): void } }).geometry.setDrawRange(0, count)
        },
        after: () => {
          /* live counts are rewritten by the next update() */
        },
      })
    }
    add(this.rain.mesh, 6)
    add(this.rainPrev.mesh, 6)
    add(this.snow.mesh, 1)
    add(this.dust.mesh, 1)
    add(this.arrows.mesh, 6)
    add(this.cloud2d.mesh, 6)
    if (this.streamlines) for (const m of this.streamlines.meshes) add(m, 6)
    return items
  }

  dispose(): void {
    this.rain.dispose()
    this.rainPrev.dispose()
    this.snow.dispose()
    this.dust.dispose()
    this.arrows.dispose()
    this.cloud2d.dispose()
    this.streamlines?.dispose()
  }
}

type THREEObj = import('three').Object3D

/** arrow spacing octave (10-160 m) from the camera distance with 0.8 / 1.25 hysteresis */
export function arrowSpacingIdx(dist: number, cur: number): number {
  const S = ENV_TIERS.arrowSpacingM
  const want = (i: number): number => S[i] * 6 // about 6 spacings between the camera and the focus
  let i = cur
  while (i < S.length - 1 && dist > want(i + 1) * ENV_TIERS.hystUp * 0.75) i++
  while (i > 0 && dist < want(i) * ENV_TIERS.hystDown * 0.75) i--
  return i
}
