// Sensor frustum adapter, SensorLayer (M06 §6.10, FR-046, AC-035; M13 §7.4). Owner: M06.
// The sensor math is M13's engine/sensors (intrinsics: frustumCorners, T_base_cam, projectionFor, hasCamera,
// sensorsOf); it is picked up here when present (import.meta.glob keeps the build independent of its delivery) and
// shared with the camera rig (FPV) through the viewport session. Registers the 'frustums' LayerSpec (edges + far plane,
// 2 draws) and PerfGovernor step 2 (Tier S: selected only -> off; Tier B/A: 16 -> selected only -> off), the edges
// fading out over --duration-quick through a uniform.
import { useEffect } from 'react'
import { FRUSTUM, perf, register, type GovernorKnob, type SensorsApi } from '@/engine'
import { MOTION } from '@/lib/tokens/motion.gen'
import { registerLayer } from './registry'
import { useVp, vp } from '../session'
import { applyLayerVisibility } from '../bindings'

const sensorModules = import.meta.glob<Partial<SensorsApi>>('../../engine/sensors/index.ts', { eager: true })

/** M13 engine/sensors when delivered and complete, else null (frustums off, FPV guarded by no_camera_sensor) */
export function m13Sensors(): SensorsApi | null {
  const m = Object.values(sensorModules)[0]
  if (!m || !m.sensorsOf || !m.hasCamera || !m.frustumCorners || !m.T_base_cam || !m.projectionFor) return null
  return m as SensorsApi
}

export function SensorsLayer() {
  const drones = useVp((s) => s.drones)
  useEffect(() => {
    if (!drones) return
    if (!vp.sensors) vp.sensors = m13Sensors()
    drones.setSensors(vp.sensors)
    const L = drones.layer
    const S = vp.be?.tier === 'S'
    const caps = S ? [FRUSTUM.capS, 0] : [FRUSTUM.capBA, FRUSTUM.capS, 0]
    let fadeTo = 1
    let fadeFrom = 1
    let fadeT0 = 0
    const knob: GovernorKnob = {
      step: 2, id: 'frustums', levels: caps.length, labelKey: 'perf.governor.frustums',
      apply: (l) => {
        const cap = caps[l]
        if (cap > 0) {
          L.frustumCap = cap
          fadeTo = 1
        } else fadeTo = 0
        fadeFrom = L.frustumFade
        fadeT0 = performance.now()
      },
    }
    const offs = [
      register('world', 'frustums.fade', (ctx) => {
        const u = Math.min(1, (ctx.nowMs - fadeT0) / MOTION.durationQuickMs)
        L.frustumFade = fadeFrom + (fadeTo - fadeFrom) * u
        if (fadeTo === 0 && u >= 1) L.frustumCap = 0
      }, { order: 5, layer: 'frustums' }),
      registerLayer({
        id: 'frustums', owner: 'M06', perfKey: 'frustums', root: L.frustumRoot, channel: 0,
        caps: { S: { maxInstances: FRUSTUM.capS }, BA: { maxInstances: FRUSTUM.capBA } },
        drawCount: () => (L.frustumRoot.visible ? L.drawCountFrustums() : 0),
        warmupVariants: () => [
          { object: L.frustums.edges, before: () => L.frustums.edges.geometry.setDrawRange(0, 2), after: () => L.frustums.commit() },
          { object: L.frustums.fill, before: () => L.frustums.fill.geometry.setDrawRange(0, 3), after: () => L.frustums.commit() },
        ],
        setVisible: (v) => {
          L.frustumRoot.visible = v
        },
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
    ]
    applyLayerVisibility()
    return () => {
      for (const off of offs) off()
    }
  }, [drones])
  return null
}
