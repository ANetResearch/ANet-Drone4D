// FPV HUD model (M15-FR-218; UX-FR-137; AWR-14 §20.10.6): pure helpers of the perception HUD: the sensor frame the view
// looks through, the box state of a target (captured, arming toward a capture, idle) and the nearest targets of a frame
// (at most 8 boxes; the pixel progress is shown for the nearest one only).
import type { PerceptionSensorFrame, PerceptionTarget } from '@/app/shell/engineD2'
import type { ModelDetail } from '@/stores/catalog'

const FRAMES = new Set(['quad_x', 'hex', 'fixed_wing', 'vtol_quadplane'])

export const HUD_BOXES = 8

/** the frame of the sensor the view looks through (by name), else the frame's HUD sensor, else the first with targets */
export function sensorFrameOf(sensors: readonly PerceptionSensorFrame[], name: string | null, hudSensor: string | null): PerceptionSensorFrame | null {
  return sensors.find((s) => s.sensor === name) ?? sensors.find((s) => s.sensor === hudSensor) ?? sensors.find((s) => s.targets.length > 0) ?? null
}

/** box state of a target: captured (CAPTURED or HOLDING), arming (timing toward a capture), idle */
export function boxState(tg: Pick<PerceptionTarget, 'captured' | 'capture_state'>): 'captured' | 'arming' | 'idle' {
  if (tg.captured || tg.capture_state === 'CAPTURED' || tg.capture_state === 'HOLDING') return 'captured'
  return tg.capture_state === 'ARMING' ? 'arming' : 'idle'
}

/** the nearest targets of a sensor frame (at most HUD_BOXES) */
export function nearestTargets(f: PerceptionSensorFrame | null): PerceptionTarget[] {
  return f ? [...f.targets].sort((a, b) => a.range_m - b.range_m).slice(0, HUD_BOXES) : []
}

/** unit sun direction (ENU) of an azimuth (from north, clockwise) and an elevation, degrees (pure; tests) */
export function sunEnuOf(azDeg: number, elDeg: number): [number, number, number] {
  const a = (azDeg * Math.PI) / 180
  const e = (elDeg * Math.PI) / 180
  return [Math.sin(a) * Math.cos(e), Math.cos(a) * Math.cos(e), Math.sin(e)]
}

/** the shape registration of a model detail, or null when the detail lacks the frame or the size */
export function shapeOf(d: Pick<ModelDetail, 'model_id' | 'frame_type' | 'profile'>): { frame: 'quad_x' | 'hex' | 'fixed_wing' | 'vtol_quadplane'; sizeM: [number, number, number]; motorDeltaTK?: number } | null {
  const p = (d.profile ?? {}) as { geometry?: { size_m?: unknown }; ir?: { motor_delta_t_k?: unknown } }
  const s = p.geometry?.size_m
  const o = (s && typeof s === 'object' && !Array.isArray(s) ? s : null) as { l?: unknown; w?: unknown; h?: unknown } | null
  const size: [number, number, number] | null = Array.isArray(s) && s.length === 3 && s.every((x) => typeof x === 'number') ? (s as [number, number, number])
    : o ? [Number(o.l), Number(o.w), Number(o.h)] : null
  if (!d.frame_type || !FRAMES.has(d.frame_type) || !size || size.some((x) => !(x > 0))) return null
  const m = p.ir?.motor_delta_t_k
  return { frame: d.frame_type as 'quad_x', sizeM: size, ...(typeof m === 'number' ? { motorDeltaTK: m } : {}) }
}
