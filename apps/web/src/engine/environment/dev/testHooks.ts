// window.__env for test builds (perf/m07 specs; M07-AC-016, AC-018-AC-021). Owner: M07. Installed by the environment
// layer adapter only when TEST_SWITCHES is folded in (production builds do not contain this module's side effects).
//   __env.state()            summary numbers (state, version, perf, live counts, params digest)
//   __env.injectPreset(id, s) a client-side smooth keyframe from the current scalars to a preset (route when steady on
//                            a preset), version + 1: exercises the Low visuals without a server (env-switch)
//   __env.injectStepAt(id, s) a step keyframe applied at absolute time s with zero anchors (env-fixed-time)
//   __env.wantTurbulence()   load the shared turbulence box (env-gpu turbulence term)
//   __env.gpuParity()        GPU windAtEnu vs windCPU at 32 x 32 points (env-gpu)
import { FloatType, WebGLRenderTarget, type PerspectiveCamera, type Scene, type WebGLRenderer } from 'three'
import type { FrameCtx } from '../../loop'
import type { EnvironmentRuntime } from '../EnvRuntime'
import { MODE_STEP } from '../state/keyframe'
import { runGpuParity } from './gpuParity'

export function installEnvTestHooks(env: EnvironmentRuntime, renderer: WebGLRenderer, frameCtx: FrameCtx, focus: () => ArrayLike<number>): void {
  const api = {
    env,
    state: () => ({
      state: env.store.state, version: env.store.version, perf: { ...env.perf, live: { ...env.perf.live } }, quality: env.quality.level,
      mor: env.store.derived.mor_m, rainK: env.store.derived.rain_k, scalars: Array.from(env.store.scalars), drawCount: env.drawCount(),
      tRenderNs: env.store.tRenderNs,
    }),
    injectPreset: (id: string, durationS = 30): number => {
      const st = env.store
      const cur = st.current
      if (!cur) return -1
      const t = Math.ceil((st.tRenderNs + 50e6) / 20e6) * 20e6
      const from = Array.from(st.scalars)
      const to = Array.from(st.P.overlay(st.scalars, id, new Float64Array(st.P.nf)))
      const steady = cur.mode === MODE_STEP || st.tRenderNs >= cur.t1Ns
      const via = steady ? [...st.P.route(cur.toPreset, id)] : []
      const w = { ...cur.wire, version: cur.version + 1 + st.queue.length, t_ns: t, t_apply_ns: t, mode: durationS > 0 ? 'smooth' : 'step', t0_ns: t,
        t1_ns: t + Math.round(durationS * 1e9), from, to, via, to_preset: id,
        anchors: { t_ns: t, s_m: st.anchors.sM, d_enu_m: Array.from(st.anchors.d), fall_rain_m: st.anchors.fallRain, fall_snow_m: st.anchors.fallSnow,
          wetness: st.anchors.wetness, puddle: st.anchors.puddle } } as typeof cur.wire
      st.ingest(w, performance.now())
      return w.version
    },
    /**
     * a step keyframe to preset `id` applied at the absolute simulation time tS with zero anchors: every page that
     * injects it holds the same environment state at any t >= tS (M07-AC-021 fixed-instant comparisons, FX-WEB1)
     */
    injectStepAt: (id: string, tS: number): number => {
      const st = env.store
      const cur = st.current
      if (!cur) return -1
      const t = Math.round(tS * 1e9 / 20e6) * 20e6
      const to = Array.from(st.P.overlay(st.scalars, id, new Float64Array(st.P.nf)))
      const w = { ...cur.wire, version: cur.version + 1000, t_ns: t, t_apply_ns: t, mode: 'step', t0_ns: t, t1_ns: t, from: to, to, via: [], to_preset: id,
        anchors: { t_ns: t, s_m: 0, d_enu_m: [0, 0, 0], fall_rain_m: 0, fall_snow_m: 0, wetness: 0, puddle: 0 } } as typeof cur.wire
      st.ingest(w, performance.now())
      return w.version
    },
    wantTurbulence: () => env.wantTurbulence(),
    gpuParity: (seed?: number, waitTurbMs = 0) => runGpuParity(env, frameCtx, focus(), {
      renderFloat: async (scene: Scene, camera: PerspectiveCamera, n: number, out: Float32Array) => {
        const rt = new WebGLRenderTarget(n, n, { type: FloatType, depthBuffer: false })
        const prev = renderer.getRenderTarget()
        renderer.setRenderTarget(rt)
        renderer.render(scene, camera)
        renderer.setRenderTarget(prev)
        await renderer.readRenderTargetPixelsAsync(rt, 0, 0, n, n, out)
        rt.dispose()
      },
    }, { seed, waitTurbMs }),
  }
  ;(window as unknown as { __env: unknown }).__env = api
}
