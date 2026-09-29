// Fog in TSL: T = exp(-optical_depth(camera -> p)) (M07-FR-006, FR-035, FR-036; M07 §6.3.4, §6.8.2 item 1; ADR-023).
// Owner: M07. Same formula as atmosphere/optics.ts (exponential haze with the Quilez closed form, flat fog layer, uniform
// precipitation below the cloud base, AGL measured from coordinate.ground.zM). No FogExp2, no densityFogFactor. The
// scene fog node is fog(fogColor, 1 - T) = mix(c, fogColor, 1 - T) = c T + fog (1 - T) (three 0.186 nodes/fog/Fog.js).
// positionWorld and cameraPosition are three-frame positions: ENU height = y.
import { abs, cameraPosition, exp, float, fog, length, max, min, positionWorld, select } from 'three/tsl'
import type { EnvNodes } from '../lighting/EnvUniforms'

type N = any // TSL nodes

/** length of the ray segment [0, L] below `top` (AGL) */
export function flatLenNode(z0: N, rdZ: N, L: N, top: N): N {
  const big: N = abs(rdZ).greaterThan(1e-5)
  const tc: N = top.sub(z0).div(select(big, rdZ, float(1)))
  const t1: N = select(big.and(rdZ.greaterThan(0)), min(L, tc), L)
  const t0: N = select(big.and(rdZ.lessThanEqual(0)), max(float(0), tc), float(0))
  const len: N = max(t1.sub(t0), float(0))
  return select(big.not().and(z0.greaterThan(top)), float(0), len)
}

/** optical depth of a ray from z0 (AGL) along a unit direction with vertical component rdZ over length L */
export function opticalDepthNode(n: EnvNodes, z0: N, rdZ: N, L: N): N {
  const a: N = n.sigmaHaze0.mul(exp(z0.negate().div(n.hazeH)))
  const k: N = rdZ.mul(L).div(n.hazeH)
  // (1 - e^-k)/k; below |k| = 1e-3 the float32 quotient cancels, use its series 1 - k/2 + k^2/6
  const q: N = select(abs(k).greaterThan(1e-3), float(1).sub(exp(k.negate())).div(k), float(1).sub(k.mul(0.5)).add(k.mul(k).div(6)))
  let od: N = a.mul(L).mul(q)
  od = od.add(n.sigmaFog.mul(flatLenNode(z0, rdZ, L, n.fogTop)))
  od = od.add(n.sigmaPrecip.mul(flatLenNode(z0, rdZ, L, n.precipTop)))
  return od
}

/** transmittance from the camera to a three-frame world position */
export function transmittanceNode(n: EnvNodes, posW: N): N {
  const d: N = posW.sub(cameraPosition)
  const L: N = max(length(d), float(1e-4))
  const rdZ: N = d.y.div(L)
  const z0: N = cameraPosition.y.sub(n.groundZ)
  return exp(opticalDepthNode(n, z0, rdZ, L).negate())
}

/** 1 - T (the fog factor of the scene fog node and of transparent environment objects) */
export function fogFactorNode(n: EnvNodes, posW: N = positionWorld): N {
  return float(1).sub(transmittanceNode(n, posW))
}

/** scene.fogNode = fog(fogColor, 1 - T(camera -> positionWorld)) */
export function sceneFogNode(n: EnvNodes): N {
  return fog(n.fogColor, fogFactorNode(n, positionWorld))
}
