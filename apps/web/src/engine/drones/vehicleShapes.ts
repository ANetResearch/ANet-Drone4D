// Vehicle shapes per model (D2 WP-11; AWR-04 §5.3-§5.4; AWR-20 §4.1, §4.2, §7.1; M06-FR-034). Owner: M06.
// The roster carries the model of each vehicle (`p600`, `x500`, `awr-q1`, `awr-q9`, `awr-f1`, `awr-v1`; clones keep the
// model of their source, AWR-04 §5.4). Each model maps to one procedural low-poly shape (engine/drones/lowpoly.ts) and
// an instance scale, so every quad of the catalog shares one InstancedMesh and one program (the shape geometry is built
// at the reference size, the instance matrix scales it): hex (the D1 P600 look, kept unchanged), quad (X500, AWR-Q1,
// AWR-Q9), fixed wing (AWR-F1, pusher with twin booms) and VTOL quadplane (AWR-V1, the fixed wing plus two lift booms and
// four lift rotors). R_vis (half diagonal of the body box) drives the screen-radius buckets, so a 2.6 m wing switches to
// its low-poly shape farther away than a 0.4 m quad. Unknown models fall back by name (fixed wing, VTOL, else the hex
// multirotor); M21's catalog (frame.type, geometry.size_m) can register any further model through registerVehicleModel.
// Also the per-model thermal hint of the LWIR view (ir.motor_delta_t_k, AWR-04 §2.3; a simulation reference value).
import { hypot3 } from '../hypot'

export type FrameShape = 'hex' | 'quad' | 'fw' | 'vtol'
/** AWR-20 §7.3 frame.type values (catalog) and the D1 hex look */
export type FrameType = 'quad_x' | 'hex' | 'fixed_wing' | 'vtol_quadplane'
export const SHAPES: readonly FrameShape[] = ['hex', 'quad', 'fw', 'vtol']

/** reference body sizes (l x w x h, m) the shape geometries are built at (lowpoly.ts) */
export const SHAPE_REF: Readonly<Record<FrameShape, readonly [number, number, number]>> = {
  hex: [1.22, 1.22, 0.14],
  quad: [0.6, 0.6, 0.16],
  fw: [1.4, 2.6, 0.35],
  vtol: [1.6, 2.8, 0.45],
}

export interface VehicleShape {
  shape: FrameShape
  /** body box l x w x h (m) */
  sizeM: readonly [number, number, number]
  /** uniform instance scale of the shape geometry */
  scale: number
  /** R_vis: half diagonal of the body box (m) */
  rVis: number
  /** motor and battery hot-spot temperature rise (K) for the LWIR view */
  motorDeltaTK: number
}

interface ModelInfo { frame: FrameType; sizeM: readonly [number, number, number]; motorDeltaTK?: number; rVis?: number }

// AWR-20 §4.1, §4.2 and §7.8 (geometry.size_m; the quads by wheelbase, scaled from the 0.6 m reference)
const KNOWN: Record<string, ModelInfo> = {
  // D1 bodies keep the D1 R_vis of 0.6 m (bucket thresholds unchanged, D2-AC-26)
  p600: { frame: 'hex', sizeM: SHAPE_REF.hex, rVis: 0.6 },
  x500: { frame: 'quad_x', sizeM: [0.5, 0.5, 0.2], rVis: 0.6 },
  'awr-q1': { frame: 'quad_x', sizeM: [0.38, 0.38, 0.11] },
  'awr-q9': { frame: 'quad_x', sizeM: [0.9, 0.9, 0.43] },
  'awr-f1': { frame: 'fixed_wing', sizeM: [1.4, 2.6, 0.35], motorDeltaTK: 20 },
  'awr-v1': { frame: 'vtol_quadplane', sizeM: [1.6, 2.8, 0.45], motorDeltaTK: 20 },
}
const registered = new Map<string, ModelInfo>()
const cache = new Map<string, VehicleShape>()
let version = 0

/** roster model or profile id to the catalog model key: `awr_f1_c2` and `awr-f1` -> `awr-f1`, `p600_mid360` -> `p600` */
export function modelKey(model: string): string {
  const m = model.trim().toLowerCase().replace(/_/g, '-')
  if (m.startsWith('awr-')) return m.split('-').slice(0, 2).join('-')
  return m.split('-')[0]
}

const SHAPE_OF: Record<FrameType, FrameShape> = { quad_x: 'quad', hex: 'hex', fixed_wing: 'fw', vtol_quadplane: 'vtol' }

function infoOf(key: string): ModelInfo {
  const r = registered.get(key) ?? KNOWN[key]
  if (r) return r
  if (/(^|-)(f\d+|fw|fixed|wing)/.test(key)) return { frame: 'fixed_wing', sizeM: SHAPE_REF.fw }
  if (/(^|-)(v\d+|vtol|quadplane)/.test(key)) return { frame: 'vtol_quadplane', sizeM: SHAPE_REF.vtol }
  return { frame: 'hex', sizeM: SHAPE_REF.hex }
}

/** shape, instance scale, R_vis and thermal hint of a roster model (cached per model string) */
export function shapeOf(model: string): VehicleShape {
  let s = cache.get(model)
  if (s) return s
  const info = infoOf(modelKey(model))
  const shape = SHAPE_OF[info.frame]
  const ref = SHAPE_REF[shape]
  const [l, w, h] = info.sizeM
  // multirotors scale by span (wheelbase), wings by the larger of length and span: the shape keeps its proportions
  const scale = shape === 'hex' ? 1 : Math.max(l, w) / Math.max(ref[0], ref[1])
  s = { shape, sizeM: [l, w, h], scale, rVis: info.rVis ?? Math.max(0.1, 0.5 * hypot3(l, w, h)), motorDeltaTK: info.motorDeltaTK ?? 15 }
  cache.set(model, s)
  return s
}

/** M21 catalog (frame.type, geometry.size_m, ir.motor_delta_t_k): adds or replaces a model; returns the new version */
export function registerVehicleModel(model: string, info: { frame: FrameType; sizeM: readonly [number, number, number]; motorDeltaTK?: number }): number {
  registered.set(modelKey(model), { frame: info.frame, sizeM: [info.sizeM[0], info.sizeM[1], info.sizeM[2]], motorDeltaTK: info.motorDeltaTK })
  cache.clear()
  return ++version
}

/** increments when a model registration changes the shapes (the drone layer re-reads its per-agent shapes) */
export function vehicleShapesVersion(): number {
  return version
}
