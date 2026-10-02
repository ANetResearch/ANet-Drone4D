// Drone layer adapter (M06 §6.9; AWR-03 §8.5 default subscriptions). Owner: M06.
// Creates the drone runtime (telemetry, drones and frustum tasks on the engine loop, M12 interpolation), registers the
// 'drones' LayerSpec (markers, low-poly batches, hero and hull; warm-up variants with one instance each; PerfGovernor
// step 4 low-poly cap) and the default subscription set: fleet/roster 10, swarm/state 10 (Tier S) or 20 (B/A),
// env/state 10, event all, perf/server 1, sys/procs 1 (selected vehicles at 60 Hz come from bindings/selection).
// __perf.net.creditSkips follows perf/server clients[] of this connection (M11-FR-096).
import { useEffect } from 'react'
import { BUCKETS, createDroneRuntime, perf, perfProbe, setProceduralHero, type GovernorKnob } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { createRtClient, rtClient } from '@/net/rt'
import type { RenderBackend } from '../renderer'
import { registerLayer, type WarmupItem } from './registry'
import { vp } from '../session'
import { applyLayerVisibility } from '../bindings'
import { pcOnlyScene } from '../bench'

if (TEST_SWITCHES && typeof location !== 'undefined' && new URLSearchParams(location.search).get('hero') === 'procedural') setProceduralHero(true)

function motionTier(): 'full' | 'lite' | 'reduced' | 'off' {
  const m = typeof document !== 'undefined' ? document.documentElement.dataset.motion : 'full'
  return m === 'lite' || m === 'reduced' || m === 'off' ? m : 'full'
}

export function DronesLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const rt = createRtClient() // the page instance (M15 RtProvider initialises it)
    const drones = createDroneRuntime(be, rtClient, { motionTier })
    drones.setSensors(vp.sensors)
    vp.drones = drones
    const L = drones.layer
    const swarmRate = be.tier === 'S' ? 10 : 20
    // system channels always; simulation channels only while the route shows the session's world (FR-030)
    const subs = [
      rt.subscribe('event', { rate: 0, mode: 'all' }),
      rt.subscribe('perf/server', { rate: 1 }),
      rt.subscribe('sys/procs', { rate: 1 }),
    ]
    let simSubs: (() => void)[] | null = null
    const pcOnly = pcOnlyScene() // flight60 scene=pc: no simulation channels, no vehicles
    let userVisible = true
    const syncSim = (): void => {
      const session = rt.serverInfo?.worldId ?? null
      const st = pcOnly || (session !== null && vp.worldId !== null && session !== vp.worldId)
      if (st && simSubs) {
        for (const off of simSubs) off()
        simSubs = null
      } else if (!st && !simSubs) {
        simSubs = [rt.subscribe('fleet/roster', { rate: 10 }), rt.subscribe('swarm/state', { rate: swarmRate }), rt.subscribe('env/state', { rate: 10 })]
      }
      L.root.visible = userVisible && !st
      L.suppressed = st
      if (vp.staticBrowse !== st) {
        vp.staticBrowse = st
        vp.changed()
      }
    }
    syncSim()
    const offSim = [vp.subscribe(syncSim), rt.onServerInfo(syncSim)]
    const offData = rt.onData((m) => {
      if (m.topic !== 'perf/server') return
      const clients = (m.data as { clients?: { conn_id?: string; credit_skips?: number }[] }).clients ?? []
      const mine = clients.find((c) => c.conn_id === rt.serverInfo?.connId) ?? (clients.length === 1 ? clients[0] : undefined)
      if (mine && typeof mine.credit_skips === 'number') perfProbe().net.creditSkips = mine.credit_skips
    })
    const S = be.tier === 'S'
    const lowLevels = S ? BUCKETS.lowLevelsS : BUCKETS.lowLevelsBA
    const knob: GovernorKnob = { step: 4, id: 'lowpoly', levels: lowLevels.length, labelKey: 'perf.governor.lowpoly', apply: (l) => L.setLowCap(lowLevels[l]),
      visible: (l) => L.root.visible && L.buckets.lowN > lowLevels[l] }
    const warm = (): WarmupItem[] => {
      const items: WarmupItem[] = []
      const one = (mesh: { count: number; visible: boolean }): Pick<WarmupItem, 'before' | 'after'> => ({
        before: () => {
          mesh.count = 1
        },
        after: () => {
          mesh.count = 0
        },
      })
      for (const b of L.lowBatches()) items.push({ object: b.mesh, ...one(b.mesh) })
      items.push({ object: L.hero.mesh, ...one(L.hero.mesh) })
      items.push({ object: L.hero.hull })
      items.push({
        object: L.markers.mesh,
        before: () => L.markers.mesh.geometry.setDrawRange(0, 6),
        after: () => L.markers.mesh.geometry.setDrawRange(0, 6 * L.markers.n),
      })
      return items
    }
    const offs = [
      registerLayer({
        id: 'drones', owner: 'M06', perfKey: 'drones', root: L.root, channel: 0,
        caps: { S: { maxInstances: BUCKETS.lowCapS }, BA: { maxInstances: BUCKETS.lowCapBA } },
        drawCount: () => (L.root.visible ? L.drawCountDrones() : 0),
        warmupVariants: warm,
        setVisible: (v) => {
          userVisible = v
          L.root.visible = v && !vp.staticBrowse
        },
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
    ]
    applyLayerVisibility()
    vp.changed()
    return () => {
      for (const off of offs) off()
      for (const off of subs) off()
      for (const off of simSubs ?? []) off()
      for (const off of offSim) off()
      offData()
      drones.dispose()
      if (vp.drones === drones) vp.drones = null
      vp.changed()
    }
  }, [be])
  return null
}
