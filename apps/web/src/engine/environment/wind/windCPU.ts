// CPU mirror of windAtEnu (M07-FR-033; M07 §6.3.5; env-gpu reference). Owner: M07.
// Same terms and order as wind/windNode.ts and python/awr/environment/field.py query: mean profile wind, vertical wind,
// frozen-direction fronts (level >= 1), optional turbulence box (MIL scaling, 2 m ground fade). Evaluated at the store's
// tRender state (scalars, anchors, current keyframe); zero allocation.
import { eDir, mod } from '../state/conventions'
import type { EnvStore } from '../state/EnvStore'
import { F } from '../state/presets'
import type { EnvTerrain } from '../terrain/dtmSampler'
import { gust } from './gust'
import { fAdv, profileCfg } from './profile'
import { milSigma, type TurbBoxCPU } from './turbBox'

const e2 = new Float64Array(2)
const s2 = new Float64Array(2)
const b3 = new Float64Array(3)

export interface WindCpuOpts {
  turb: boolean
  box: TurbBoxCPU | null
  terrain: EnvTerrain
  /** gust long component (projection of the gust vector on the mean going-to direction) written to out[3] when true */
  gustLong?: boolean
}

/** ENU wind at (x, y, z) -> out[0..2] (out[3] = gust along the mean direction when out.length >= 4) */
export function windCPU(x: number, y: number, z: number, store: EnvStore, o: WindCpuOpts, out: Float64Array): Float64Array {
  const kf = store.current
  if (!kf) {
    out.fill(0)
    return out
  }
  const s = store.scalars
  const prof = kf.profile
  const zAgl = z - o.terrain.sample(x, y)
  const f = profileCfg(zAgl, prof)
  eDir(s[F.DIR], e2)
  const spd = s[F.SPEED_REF]
  let wx = spd * f * e2[0]
  let wy = spd * f * e2[1]
  let wz = s[F.W_MEAN]
  let gl = 0
  const fa = fAdv(prof)
  if (kf.level >= 1) {
    const S = store.anchors.sM
    for (const ev of kf.events) {
      if (ev.kind !== 1) continue
      const g = gust(x, y, S, ev, fa)
      if (g === 0) continue
      const ex = -Math.sin((ev.dirFromDeg * Math.PI) / 180)
      const ey = -Math.cos((ev.dirFromDeg * Math.PI) / 180)
      wx += g * ex
      wy += g * ey
      gl += g * (ex * e2[0] + ey * e2[1])
    }
    if (o.turb && o.box && kf.turbModel === 'box') {
      const D = store.anchors.d
      const P = o.box.period
      o.box.sample(x - mod(fa * D[0], P), y - mod(fa * D[1], P), z - mod(fa * D[2], P), b3)
      milSigma(zAgl, s[F.SIGMA_REF], s2)
      const g = Math.min(Math.max(zAgl / 2.0, 0), 1)
      wx += s2[0] * b3[0] * g
      wy += s2[0] * b3[1] * g
      wz += s2[1] * b3[2] * g
    }
  }
  out[0] = wx
  out[1] = wy
  out[2] = wz
  if (out.length >= 4) out[3] = gl
  return out
}
