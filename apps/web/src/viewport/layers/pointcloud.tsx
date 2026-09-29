// Point cloud layer adapter (M05 §7.3, §7.5; <= 150 lines). Owner: M05.
// Creates the PointCloudEngine on the backend, opens the world of the route (/world/:id; other routes keep the last
// world), registers the world-phase update, the governor-phase CAS sample and the 4 Hz copy of the statistics into
// stores/world, the LayerSpec (channel, drawCount, device loss hooks), the layers-store bindings and the boot gates.
// engine/pointcloud never imports stores or React and stores never import the engine (AWR-03 §4.2): this file connects.
import { useEffect, useRef } from 'react'
import { PointCloudEngine, perfProbe, register, type ColorMode } from '@/engine'
import { useRoute } from '@/app/router/router'
import { boot } from '@/app/boot/BootController'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { worldStore, writeWorldStats, type AnchorKind } from '@/stores/world'
import { layers, layersStore, type LayersState } from '@/stores/layers'
import { prefsStore } from '@/stores/prefs'
import { getMotionTier } from '@/ui/motion/tier'
import type { RenderBackend } from '../renderer'
import { registerLayer } from './registry'
import { vp } from '../session'

boot.addGate('firstFrame')

function motionTier(): 'full' | 'lite' | 'reduced' {
  const t = getMotionTier()
  return t === 'full' || t === 'lite' ? t : 'reduced'
}

/** test-build switches (AWR-18 §9.5, M05-FR-046): ?fixedB=, ?pcInject=fail:<p>, ?quality=1 */
function testParams(): { fixedB?: number; pcInject?: number; quality?: boolean } {
  if (!TEST_SWITCHES || typeof location === 'undefined') return {}
  const q = new URLSearchParams(location.search)
  const b = Number(q.get('fixedB'))
  const inj = /^fail:([0-9.]+)$/.exec(q.get('pcInject') ?? '')
  return { fixedB: Number.isFinite(b) && b > 0 ? b : undefined, pcInject: inj ? Math.min(1, Number(inj[1])) : undefined, quality: q.get('quality') === '1' }
}

function bindPrefs(engine: PointCloudEngine, s: LayersState, prev: LayersState | null): void {
  if (!prev || prev.visible.pointcloud !== s.visible.pointcloud) engine.setVisible(s.visible.pointcloud)
  if (!prev || prev.colorMode !== s.colorMode) engine.setColorMode(s.colorMode as ColorMode)
  if (!prev || prev.classMask !== s.classMask) engine.setClassMask(s.classMask)
  if (!prev || prev.edl !== s.edl) engine.setEdl(s.edl)
  if (!prev || prev.quality !== s.quality) engine.setQuality(s.quality)
}

/**
 * One engine per page: a device loss remounts the canvas with a new backend, and the engine must survive it so the GPU
 * residency is rebuilt from its CPU cache without a single new request (FR-027).
 */
let shared: PointCloudEngine | null = null

export function PointCloudLayer({ be }: { be: RenderBackend }) {
  const route = useRoute()
  const routeWorld = route?.route.id === 'world' ? (route.params.id ?? null) : null
  const worldId = routeWorld ?? vp.worldId ?? prefsStore.getState().ui.lastWorld ?? 'shenzhen'
  // the engine lives in a ref: the world effect below runs after the creating effect in the same commit
  const holder = useRef<PointCloudEngine | null>(null)

  useEffect(() => {
    let engine = shared
    if (!engine) engine = shared = new PointCloudEngine({ backend: be, perf: perfProbe(), params: testParams(), motionTier })
    else if (engine.backend !== be) engine.onBackendReady(be) // canvas rebuilt after a device loss
    holder.current = engine
    const offs = [
      register('world', 'pointcloud.update', (ctx) => engine.update(ctx), { layer: 'pointcloud' }),
      register('governor', 'pointcloud.cas', (ctx) => engine.sampleFrame(ctx), { layer: 'pointcloud' }),
      register('governor', 'pointcloud.stats', () => void writeWorldStats(engine.stats()), { fps: 4 }),
      registerLayer({
        id: 'pointcloud', owner: 'M05', perfKey: 'pointcloud', root: engine.root, channel: 1, drawCount: () => engine.drawCount(),
        setVisible: (v) => engine.setVisible(v), onBackendLost: () => engine.onBackendLost(), onBackendReady: (b) => engine.onBackendReady(b),
        warmupVariants: () => [{ object: engine.root, targets: be.tier === 'S' ? ['screen'] : ['cloud'], before: () => engine.warmupBegin(), after: () => engine.warmupEnd() },
          { object: engine.picker.object, targets: ['pick'], before: () => engine.warmupBegin(true), after: () => engine.warmupEnd() }],
        services: { cas: engine.cas, edlMaterial: engine.edlMaterial, dtm: engine.dtm, prefetchView: (e, t, f) => engine.prefetchView(e, t, f),
          setFocus: (p, m) => engine.setFocus(p, m) },
        dispose: () => {},
      }),
      layersStore.subscribe((s, prev) => bindPrefs(engine, s, prev)),
      engine.on('pc.world.opened', ({ info, reason }) => {
        vp.world = info
        worldStore.setState({ contentVersion: info.contentVersion, anchorKind: info.anchorKind as AnchorKind, firstScreenBytes: info.firstScreenBytes,
          northConfidence: info.northConfidence, syntheticGroundZ: info.syntheticGroundZ })
        layers.applyWorldDefault(info.defaultColorMode)
        // a reopen after a contentVersion change keeps the camera (FR-008)
        if (reason !== 'stale' && vp.rig && info.home) {
          vp.rig.home = info.home
          vp.rig.goHome(false)
        }
        vp.changed()
      }),
      engine.on('pc.first.frame', ({ ttfpMs, switchMs }) => {
        worldStore.setState({ ttfpMs: Number.isFinite(ttfpMs) ? ttfpMs : null, switchMs: Number.isFinite(switchMs) ? switchMs : null })
        boot.resolveGate('firstFrame')
      }),
      engine.on('pc.world.error', (err) => {
        worldStore.setState({ phase: 'error', error: err })
        boot.resolveGate('firstFrame') // a failed world must not keep the mask; the World panel shows the error
      }),
      boot.subscribe((st) => {
        if (st === 'REVEALED') engine.markRevealed()
      }),
    ]
    bindPrefs(engine, layersStore.getState(), null)
    // test builds: window.__pc for perf/m05 specs (engine, layer actions, world snapshot)
    if (TEST_SWITCHES) (window as unknown as { __pc: unknown }).__pc = { engine, layers, prefs: () => layersStore.getState(), world: () => worldStore.getState(), stats: () => ({ ...engine.stats(), levelCounts: [] }) }
    return () => {
      for (const off of offs) off() // the engine itself stays (see `shared`)
      holder.current = null
    }
  }, [be])

  useEffect(() => {
    const engine = holder.current
    if (!worldId || !engine) return
    vp.worldId = worldId
    const base = `${location.origin}/worlds/${encodeURIComponent(worldId)}/`
    if (engine.worldBase === base && engine.enginePhase !== 'error' && engine.enginePhase !== 'idle') return // remount: keep the world
    worldStore.setState({ worldId, phase: 'manifest', progress: 0, error: null })
    engine.open(base).catch(() => {
      // reported through pc.world.error (superseded opens are silent)
    })
  }, [be, worldId])
  return null
}
