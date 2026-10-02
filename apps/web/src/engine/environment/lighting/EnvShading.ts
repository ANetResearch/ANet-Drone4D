// Environment shading provider (M07-FR-035-FR-038; M07 §6.8.2 items 1-3, §7.2; 15 §10.3; r16 §3.11). Owner: M07.
// Implements the SceneShadingProvider that M06 defines in engine/shading.ts (M07 §7.2; the environment adapter sets it
// before the shader zoo, FX-WEB1): the point material (M05), the drone meshes and the sky (SkyQuad on Tier S, the P2
// background on Tier B/A) take their light, fog and sky from it:
//   lambert(n, p) = 0.45 + 0.15 (0.5 + 0.5 n'_up) + 0.55 max(n' . L_sun, 0) sun_vis S_cloud(p)   (direct term only)
//   fogFactor(p)  = 1 - exp(-optical_depth(camera -> p));  fogColor = horizon colour of visual.horizon_step
//   sky(d)        = mix(zenith, horizon, (1 - max(d_up, 0))^3) with the 2D clouds composited over it
// Cloud shadow: xy' = p.xy + L.xy / max(L.z, 0.15) (h_mid - z_agl); S = mix(1, exp(-m(xy') cloud_od), k_sh) with the same
// mask, plane and offset as the visible 2D clouds, k_sh = 0.8 cloud2d_alpha_max / 0.5. Every call builds its own uniform
// nodes (one material per call, M06 §6.3).
import { Fn, If, cameraPosition, dot, exp, faceForward, float, max, mix, normalize, pow, vec2 } from 'three/tsl'
import { cloud2DNode } from '../clouds/Cloud2D'
import { cloudMaskNode, type WeatherMap } from '../clouds/WeatherMap'
import { fogFactorNode } from '../atmosphere/fogNode'
import { makeEnvNodes, type EnvNodes, type EnvParams } from './EnvUniforms'
import type { SceneShadingProvider } from '../../shading'

type N = any // TSL nodes

export type { SceneShadingProvider }

/**
 * cloud shadow factor at a three-frame world position. The weather-map sample and the exp only run when the shadow
 * weight (strength x clouds on x visual on, a uniform) is positive: in a uniform branch the whole block is skipped,
 * which saves the per-vertex texture sample of the point material in clear weather (FX2-R2)
 */
export function cloudShadowNode(n: EnvNodes, wm: WeatherMap, posW: N): N {
  // own Fn: the If needs a node stack also where callers build colour graphs outside a Fn (drone mesh materials)
  return Fn(() => {
    const w: N = n.shadowStrength.mul(n.cloudsOn).mul(n.visualOn)
    const sh: N = float(1).toVar()
    If(w.greaterThan(0), () => {
      const L: N = n.sunDirEnu
      const zAgl: N = posW.y.sub(n.groundZ)
      const k: N = n.cloudHmid.sub(zAgl).div(max(L.z, float(0.15)))
      const xy: N = vec2(posW.x.add(L.x.mul(k)), posW.z.negate().add(L.y.mul(k)))
      const m: N = cloudMaskNode(n, wm, xy)
      sh.assign(mix(float(1), exp(m.mul(n.cloudOD).negate()), w))
    })
    return sh
  })()
}

export function lambertNode(n: EnvNodes, wm: WeatherMap, nW: N, posW: N): N {
  const V: N = normalize(cameraPosition.sub(posW))
  const nf: N = faceForward(nW, V.negate(), nW)
  const direct: N = max(dot(nf, n.sunDirThree), float(0)).mul(n.sunVis).mul(cloudShadowNode(n, wm, posW))
  const lam: N = float(0.45).add(nf.y.mul(0.5).add(0.5).mul(0.15)).add(direct.mul(0.55))
  // wet ground darkening (D1-ext, M07-FR-049): exposed (upward) surfaces c (1 - 0.325 wet)
  return lam.mul(float(1).sub(n.wetness.mul(0.325).mul(max(nf.y, float(0))).mul(n.visualOn)))
}

export function skyNode(n: EnvNodes, wm: WeatherMap, dirW: N): N {
  const up: N = max(dirW.y, float(0))
  const grad: N = mix(n.zenithColor, n.fogColor, pow(float(1).sub(up), float(3)))
  const c: N = cloud2DNode(n, wm, dirW)
  return grad.mul(float(1).sub(c.w)).add(c.xyz)
}

export function createEnvShading(p: EnvParams, wm: WeatherMap): SceneShadingProvider {
  return {
    lambert: (nW, posW) => lambertNode(makeEnvNodes(p), wm, nW, posW),
    fogFactor: (posW) => fogFactorNode(makeEnvNodes(p), posW),
    // a new uniform node per read, like the other terms (the point material mixes its fog per vertex, ADR-064)
    get fogColor(): N {
      return makeEnvNodes(p).fogColor
    },
    sky: (dirW) => skyNode(makeEnvNodes(p), wm, dirW),
  }
}

