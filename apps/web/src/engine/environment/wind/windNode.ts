// Wind in TSL (M07-FR-009, FR-011, FR-012, FR-033; M07 §6.3.5-§6.3.8; D1-AC-13 env-gpu). Owner: M07.
// windAtEnu(p) returns the ENU wind (m/s) at an ENU position with the same formula as wind/windCPU.ts:
//   W = s f(z_agl) e(theta) + w_mean z + sum_k G_k (e_k, 0) [level >= 1] + g(z_agl) T [opts.turb, level >= 1]
// z_agl = z - dtm(x, y) (terrain following, four textureLoad taps), fronts packed as uGustA/B (xi0_k computed in
// float64 on the CPU), turbulence from the shared f16 box with texture3D(uvw = (p - Dq) / 256, repeat, linear) scaled by
// the MIL-F-8785C sigma_u(z), sigma_w and faded in over the first 2 m. Low draws mean + vertical + fronts; Med adds
// turbulence (EP-3: omitted terms only, never a different formula).
import { clamp, cos, float, log, max, pow, select, texture3D, vec3 } from 'three/tsl'
import type { EnvNodes } from '../lighting/EnvUniforms'
import { dtmHeight, type DtmNodes } from '../terrain/dtmSampler'

type N = any // TSL nodes

export const FT = 0.3048
const SIGMA_W_OVER_REF = (0.177 + 0.000823 * (10.0 / FT)) ** 0.4

/** profile f(z_agl): kind 0 log, 1 power, 2 uniform */
export function profileNode(n: EnvNodes, zAgl: N): N {
  const logF: N = select(zAgl.lessThanEqual(n.profD.add(n.profZ0)), float(0),
    log(max(zAgl.sub(n.profD).div(n.profZ0), float(1e-9))).div(log(n.profZref.sub(n.profD).div(n.profZ0))))
  const powF: N = pow(max(zAgl, float(0)).div(n.profZref), n.profAlpha)
  const uniF: N = select(zAgl.greaterThan(0), float(1), float(0))
  return select(n.profKind.lessThan(0.5), logF, select(n.profKind.lessThan(1.5), powF, uniF))
}

export interface WindNodeOpts {
  turb: boolean
  /** turbulence box texture node (texture3D base) when turb */
  turbTex?: N
  dtm: DtmNodes
  dtmTex: N
}

/** total ENU wind at pEnu (vec3 ENU m) */
export function windAtEnu(n: EnvNodes, pEnu: N, o: WindNodeOpts): N {
  const zG: N = dtmHeight(o.dtm, pEnu.xy, o.dtmTex)
  const zAgl: N = pEnu.z.sub(zG)
  const f: N = profileNode(n, zAgl)
  let w: N = vec3(n.windS.mul(f).mul(n.windE.x), n.windS.mul(f).mul(n.windE.y), n.wMean)
  const l1: N = select(n.level.greaterThan(0.5), float(1), float(0))
  for (let k = 0; k < 4; k++) {
    const A: N = n.gustA[k]
    const B: N = n.gustB[k]
    const xi: N = A.x.sub(A.y.mul(pEnu.x).add(A.z.mul(pEnu.y)))
    const inside: N = xi.greaterThanEqual(0).and(xi.lessThanEqual(B.z)).and(B.w.greaterThan(0.5))
    const g: N = select(inside, B.x.mul(0.5).mul(float(1).sub(cos(xi.mul(B.y)))), float(0)).mul(l1)
    w = w.add(vec3(g.mul(A.y), g.mul(A.z), float(0)))
  }
  if (o.turb && o.turbTex) {
    const q: N = pEnu.sub(n.turbD).div(256.0)
    const b: N = texture3D(o.turbTex, q).xyz
    const hFt: N = max(zAgl.div(FT), float(10))
    const sw: N = n.turbSigmaRef.mul(SIGMA_W_OVER_REF)
    const su: N = sw.div(pow(float(0.177).add(hFt.mul(0.000823)), float(0.4)))
    const g: N = clamp(zAgl.div(2.0), float(0), float(1)).mul(n.turbOn).mul(l1)
    w = w.add(vec3(su.mul(b.x), su.mul(b.y), sw.mul(b.z)).mul(g))
  }
  return w
}
