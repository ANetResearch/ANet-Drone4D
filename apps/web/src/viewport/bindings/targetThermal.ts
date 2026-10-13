// Targets as thermal emitters of the LWIR view (D2 WP-11; AWR-04 §7.2 thermal attributes, §6.2 LWIR row; M18-M19 PRD
// §7.2). Owner: M06. The page's target model of M18 (engine/targets targetsModel: TargetLite32 rows, configurations,
// class templates) is discovered the way the M07 and M13 modules are (import.meta.glob: the build does not depend on its
// delivery) and registered as the 'targets' emitter source of viewport/sensorView: one box per enabled target (base
// centre + h / 2, l x w x h along the heading), its thermal attributes from the target configuration, else the class
// template: mode relative (delta_t_k above the background surface) or absolute (t_surface_c), emissivity, hot fraction.
// Read per frame only while the LWIR view is shown; sizes and thermal attributes are cached per target and configuration
// version.
import { setEmitterSource } from '../sensorView/state'
import { EMITTER as E, EMITTER_STRIDE } from '../sensorView/state'

interface Thermal { mode?: string; delta_t_k?: number; t_surface_c?: number; emissivity?: number; hot_fraction?: number }
interface ModelLike {
  rows: { n: number; pos: Float64Array; heading: Float32Array; enabled: Uint8Array }
  detailVersion: number
  cfgs: Map<string, { thermal?: Thermal; [k: string]: unknown }>
  idOfRow(i: number): string
  classOfRow(i: number): string | null
  sizeOf(id: string, cls: string | null): [number, number, number]
}
interface TargetsModule { targetsModel?: ModelLike; CLASS_DEFAULTS?: Record<string, { thermal?: Thermal }> }

const mods = import.meta.glob<TargetsModule>('../../engine/targets/index.ts', { eager: true })

/** [l, w, h, tC (NaN: relative), dT, eps, hot] per target id, rebuilt when the model's configuration version changes */
const cache = new Map<string, Float64Array>()
let cacheVersion = -1

function attrsOf(m: ModelLike, defaults: Record<string, { thermal?: Thermal }>, id: string, cls: string | null): Float64Array {
  let a = cache.get(id)
  if (a) return a
  const [l, w, h] = m.sizeOf(id, cls)
  const th = (m.cfgs.get(id)?.thermal ?? (cls ? defaults[cls]?.thermal : undefined) ?? {}) as Thermal
  const abs = th.mode === 'absolute' && Number.isFinite(th.t_surface_c)
  a = Float64Array.of(l, w, h, abs ? (th.t_surface_c as number) : Number.NaN, abs ? 0 : (th.delta_t_k ?? 5), th.emissivity ?? 0.95, th.hot_fraction ?? 1)
  cache.set(id, a)
  return a
}

/** register the source when M18's engine module is present; returns the unregister function */
export function installTargetThermal(): () => void {
  const mod = Object.values(mods)[0]
  const m = mod?.targetsModel
  const defaults = mod?.CLASS_DEFAULTS ?? {}
  if (!m) return () => {}
  return setEmitterSource('targets', (out, max) => {
    if (m.detailVersion !== cacheVersion) {
      cache.clear()
      cacheVersion = m.detailVersion
    }
    const R = m.rows
    let k = 0
    for (let i = 0; i < R.n && k < max; i++) {
      if (R.enabled[i] === 0) continue
      const id = m.idOfRow(i)
      const a = attrsOf(m, defaults, id, m.classOfRow(i))
      const o = k * EMITTER_STRIDE
      out[o + E.X] = R.pos[3 * i]
      out[o + E.Y] = R.pos[3 * i + 1]
      out[o + E.Z] = R.pos[3 * i + 2] + a[2] / 2
      out[o + E.L] = a[0]
      out[o + E.W] = a[1]
      out[o + E.H] = a[2]
      out[o + E.YAW] = R.heading[i]
      out[o + E.TC] = a[3]
      out[o + E.DT] = a[4]
      out[o + E.EPS] = a[5]
      out[o + E.HOT] = a[6]
      k++
    }
    return k
  })
}
