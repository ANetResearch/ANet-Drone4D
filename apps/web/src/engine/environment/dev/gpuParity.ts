// env-gpu check (M07-AC-016; D1-AC-13): GPU windAtEnu vs windCPU at 32 x 32 points. Owner: M07. Test builds only
// (reached through window.__env.gpuParity in viewport/layers/environment.tsx and tests/environment/visual.browser.test.ts).
// The state is thunderstorm with 4 active fronts, the live DTM of the runtime (or the flat ground), and the turbulence
// box texture3D when it is loaded; each fragment writes W + 64 m/s (the colour output clamps negatives) into an
// RGBA32F target; tolerance 0.01 vmax + 0.02 m/s with vmax = derive().vmax_vis_mps.
import { DataTexture, FloatType, Mesh, NearestFilter, PerspectiveCamera, PlaneGeometry, RGBAFormat, Scene, type RenderTarget } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, float, positionGeometry, texture, uv, vec4 } from 'three/tsl'
import type { EnvironmentRuntime } from '../EnvRuntime'
import { derive, newDerived } from '../state/derive'
import type { EnvKeyframeWire } from '../state/keyframe'
import { PRESETS_MODEL } from '../state/presets'
import { makeEnvNodes } from '../lighting/EnvUniforms'
import { makeDtmNodes } from '../terrain/dtmSampler'
import { gustCreate } from '../wind/gust'
import { windAtEnu } from '../wind/windNode'
import { windCPU } from '../wind/windCPU'
import { fAdv } from '../wind/profile'
import type { FrameCtx } from '../../loop'

export interface GpuParityIO {
  /** render `scene` with `camera` into a float render target of n x n (created and owned by the caller) */
  renderFloat(scene: Scene, camera: PerspectiveCamera, n: number, out: Float32Array): Promise<void>
}

export interface GpuParityResult { worst: number; bound: number; vmax: number; gusty: number; turb: boolean; points: number }

export function parityFrame(tNs: number, center: ArrayLike<number>, S: number, seed = 7): EnvKeyframeWire {
  const to = Array.from(PRESETS_MODEL.presetVector('thunderstorm'))
  const prof = { kind: 'log' as const, z_ref_m: 10, z0_m: 0.5, d_m: 0, alpha: 0.25, adv_height_m: 40 }
  const fa = fAdv(prof)
  const evs = [270, 300, 200, 90].map((dir, k) => gustCreate(k + 1, 0, 8 - k, 60, dir, S - 60 * k, 14,
    [center[0] - 300, center[1] - 300], [center[0] + 300, center[1] + 300], fa))
  return {
    schema: 'awr.env.keyframe.v1', world_id: 'parity', version: 1_000_000, epoch: 1, seed, t_ns: tNs, t_apply_ns: tNs,
    config: { wind: { level: 1, profile: prof, library: null, turbulence: { model: 'box', n: 64, dx_m: 4, l_m: 30 }, gust: { model: 'cos1_front', max_active: 4 } },
      sun: { azimuth_deg: 150, elevation_deg: 50 }, weather_map: { n: 512, scale_m: 24000 }, presets_sha256: PRESETS_MODEL.sha256 },
    mode: 'step', t0_ns: tNs, t1_ns: tNs, from: to, to, via: [], to_preset: 'thunderstorm',
    anchors: { t_ns: tNs, s_m: S + 380, d_enu_m: [900.25, -300.5, 0], fall_rain_m: 0, fall_snow_m: 0, wetness: 0, puddle: 0 },
    events: evs.map((e) => [1, e.id, 0, e.x0, e.s0, e.amp, e.lam, e.dirFromDeg, e.sSpan]), vis: { streamlines: null, vmax_mps: 20 },
  }
}

export async function runGpuParity(env: EnvironmentRuntime, ctx: FrameCtx, center: ArrayLike<number>, io: GpuParityIO,
  opt: { seed?: number; waitTurbMs?: number } = {}): Promise<GpuParityResult> {
  const N = 32
  const w = parityFrame(ctx.tRenderS * 1e9, center, 5000, opt.seed ?? 7)
  env.store.clear()
  env.store.ingest(w, ctx.nowMs)
  env.update(ctx, center, false)
  if (opt.waitTurbMs) {
    env.wantTurbulence()
    const t0 = performance.now()
    while (!env.turb && performance.now() - t0 < opt.waitTurbMs) {
      env.update(ctx, center, false)
      await new Promise((r) => setTimeout(r, 50))
    }
  }
  const turb = !!env.turb
  env.params.turbOn = turb ? 1 : 0
  env.terrain.sync()
  const pts = new Float32Array(N * N * 4)
  const cpu = new Float64Array(N * N * 3)
  const o = new Float64Array(4)
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
    const k = j * N + i
    const x = center[0] - 150 + i * 9.7
    const y = center[1] - 120 + j * 7.9
    const z = env.terrain.sample(x, y) + 3 + ((i * 7 + j * 3) % 60)
    pts.set([x, y, z, 1], 4 * k)
    windCPU(x, y, z, env.store, { turb, box: env.turb?.cpu ?? null, terrain: env.terrain }, o)
    cpu.set([o[0], o[1], o[2]], 3 * k)
  }
  const ptex = new DataTexture(pts, N, N, RGBAFormat, FloatType)
  ptex.minFilter = NearestFilter
  ptex.magFilter = NearestFilter
  ptex.needsUpdate = true
  const n = makeEnvNodes(env.params)
  const dn = makeDtmNodes(env.terrain)
  const m = new MeshBasicNodeMaterial()
  m.vertexNode = vec4((positionGeometry as never as { xy: never }).xy, 0, 1) as never
  m.colorNode = Fn(() => {
    const p = texture(ptex, uv() as never) as never as { xyz: never }
    const wv = windAtEnu(n, p.xyz, { turb, turbTex: env.turb?.tex, dtm: dn, dtmTex: dn.tex }) as { add(x: number): never }
    return vec4(wv.add(64), float(1))
  })() as never
  const q = new Mesh(new PlaneGeometry(2, 2), m)
  q.frustumCulled = false
  const s = new Scene()
  s.add(q)
  const cam = new PerspectiveCamera()
  const out = new Float32Array(N * N * 4)
  await io.renderFloat(s, cam, N, out)
  const vmax = derive(env.store.scalars, newDerived()).vmax_vis_mps
  let worst = 0
  let gusty = 0
  for (let k = 0; k < N * N; k++) {
    for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(out[4 * k + c] - 64 - cpu[3 * k + c]))
    if (Math.hypot(cpu[3 * k], cpu[3 * k + 1]) > 25) gusty++
  }
  ptex.dispose()
  m.dispose()
  q.geometry.dispose()
  env.store.clear()
  return { worst, bound: 0.01 * vmax + 0.02, vmax, gusty, turb, points: N * N }
}

export type { RenderTarget }
