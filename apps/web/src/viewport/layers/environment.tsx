// Environment layer adapter (M07 §9.1, §7.2, §8.3; M06 §6.7; <= 150 lines). Owner: M07.
// Creates the EnvironmentRuntime on the backend, sets scene.fogNode once before the shader zoo (M06 §6.4 rule 2),
// registers the world-phase update, the LayerSpec (draws, zoo variants, PerfGovernor step 5 knob), the realtime binding,
// the sky and point-cloud lighting hooks of M06/M05, and writes stores/env at <= 4 Hz (Tier S) / 10 Hz from the
// engine summary; the sub-layer switches and the quality setting travel the other way (engine never imports stores).
import { useEffect } from 'react'
import type { Scene } from 'three'
import { activePointCloud, ctx as frameCtx, perf, perfProbe, register } from '@/engine'
import { buildSummary, EnvBinding, EnvironmentRuntime, EnvSeries, localReadings, profileCurve, type DtmSource, type EnvSelectedData } from '@/engine/environment'
import { installEnvTestHooks } from '@/engine/environment/dev/testHooks'
import { getToken } from '@/net/api'
import { rtClient } from '@/net/rt'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { envStore, type EnvSummary, type PresetId } from '@/stores/env'
import { layersStore } from '@/stores/layers'
import { selectionStore } from '@/stores/selection'
import { getMotionTier } from '@/ui/motion/tier'
import type { RenderBackend } from '../renderer'
import { pointCloudServices, registerLayer } from './registry'
import { skyControl } from './groundSky'
import { vp } from '../session'

const bytes = (u: string, h?: HeadersInit): Promise<ArrayBuffer> => fetch(u, { headers: h }).then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(`${u} ${r.status}`))))
const apiBytes = async (u: string): Promise<ArrayBuffer> => {
  const t = await getToken()
  return bytes(u, t ? { authorization: `Bearer ${t}` } : undefined)
}
const pose = new Float64Array(6)
const dp = new Float64Array(3)
const dq = new Float64Array(4)
const dv = new Float64Array(3)
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)

export function EnvironmentLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const env = new EnvironmentRuntime({ be, reversedZ: be.caps.reversedZ, fetchBytes: (u) => bytes(u), fetchApiBytes: apiBytes })
    const scene = vp.scene as (Scene & { fogNode?: unknown }) | null
    if (scene) scene.fogNode = env.fogNode // once, before the shader zoo (WarmupTrigger mounts after this adapter)
    const bind = new EnvBinding(() => rtClient(), env)
    const series = new EnvSeries()
    const p = perfProbe() as ReturnType<typeof perfProbe> & { env?: unknown }
    p.env = env.perf
    let userVisible = true
    let lastHorizon = ''
    let lastSun = ''
    const applySub = (): void => {
      const l = envStore.getState().layers
      Object.assign(env.sub, { precip: l.precip, clouds: l.clouds, arrows: l.arrows, arrowsSliceAglM: l.arrowsSliceAglM, streamlines: l.streamlines })
      env.setUserQuality(envStore.getState().quality.user)
      env.masterVisible = userVisible && layersStore.getState().visible.environment
    }
    const selected = (): EnvSelectedData | null => {
      const id = selectionStore.getState().primary
      const c = rtClient()
      const a = id && c ? c.roster.agentNoOf(id) : -1
      if (!(a >= 0) || !vp.drones || !vp.drones.fullPoseOf(a, dp, dq, dv)) return null
      return localReadings(env.store, { turb: !!env.turb, box: env.turb?.cpu ?? null, terrain: env.terrain }, dp, dv, env.store.state !== 'SYNCED', env.terrain.groundZ)
    }
    const offs = [
      bind.install(),
      register('world', 'env.update', (ctx) => {
        const dtm = pointCloudServices()?.dtm as unknown as DtmSource | null | undefined
        if (dtm !== env.terrain.src || (vp.worldCtx && env.terrain.groundZ !== vp.worldCtx.groundZ && !dtm?.loaded)) env.setDtmSource(dtm ?? null, vp.worldCtx?.groundZ ?? 0)
        const rig = vp.rig
        const focus = rig ? rig.currentPose(pose).subarray(3, 6) : null
        env.update(ctx, focus, rig?.mode === 'third' || rig?.mode === 'fpv')
        const L = p.layers.environment
        L.draws = env.perf.draws
        L.verts = env.perf.verts
        const P = env.params
        const h = `${P.fogColor.x.toFixed(5)},${P.fogColor.y.toFixed(5)},${P.fogColor.z.toFixed(5)}`
        if (h !== lastHorizon) {
          lastHorizon = h
          skyControl.setHorizon([P.fogColor.x, P.fogColor.y, P.fogColor.z], [P.zenithColor.x, P.zenithColor.y, P.zenithColor.z])
        }
        const s = `${env.sunEnu.join(',')},${env.cloudShadowFocus.toFixed(3)}`
        if (s !== lastSun) {
          lastSun = s
          activePointCloud()?.setLighting(env.sunEnu, env.cloudShadowFocus)
        }
      }, { layer: 'environment' }),
      register('overlay', 'env.summary', (ctx) => {
        const sel = selected()
        if (sel) series.push(ctx.tRenderS, sel.windMps)
        const s = buildSummary(env.store, ctx.nowMs, sel)
        const q = env.quality
        const next: Partial<EnvSummary> = {
          ...s, activePreset: s.activePreset as PresetId | null, toPreset: s.toPreset as PresetId | null,
          quality: { level: q.level, reason: q.reason, user: envStore.getState().quality.user },
          advanced: { ...s.advanced, turbAssetOk: !bind.assetUnavailable },
          profileCurve: profileCurve(env.store), envSeries: series.buckets(ctx.tRenderS),
        }
        const cur = envStore.getState() as unknown as Record<string, unknown>
        const diff: Record<string, unknown> = {}
        for (const [k, v] of Object.entries(next)) if (!same(cur[k], v)) diff[k] = v
        if (Object.keys(diff).length) envStore.setState(diff as Partial<EnvSummary>)
      }, { fps: be.tier === 'S' ? 4 : 10 }),
      registerLayer({
        id: 'environment', owner: 'M07', perfKey: 'environment', root: env.root, channel: 0,
        caps: { S: { maxQuads: 2000 }, BA: { maxQuads: 8000 } },
        drawCount: () => env.drawCount(),
        warmupVariants: () => env.warmupObjects(),
        setVisible: (v) => {
          userVisible = v
          applySub()
        },
        knobs: [env.quality.knob],
        dispose: () => {},
      }),
      perf.registerKnob(env.quality.knob),
      envStore.subscribe((s, prev) => {
        if (s.layers !== prev.layers || s.quality.user !== prev.quality.user) applySub()
      }),
      layersStore.subscribe(applySub),
      selectionStore.subscribe((s) => bind.select(s.primary ?? null)),
    ]
    const motion = (): void => {
      const reduced = getMotionTier() === 'reduced'
      env.store.damper.reduced = reduced
      env.anchor.reduced = reduced
    }
    motion()
    applySub()
    bind.select(selectionStore.getState().primary ?? null)
    if (TEST_SWITCHES) installEnvTestHooks(env, be.renderer, frameCtx, () => (vp.rig ? vp.rig.currentPose(new Float64Array(6)).subarray(3, 6) : [0, 0, 0]))
    return () => {
      for (const off of offs) off()
      if (scene && scene.fogNode === env.fogNode) scene.fogNode = undefined
      env.dispose()
    }
  }, [be])
  return null
}
