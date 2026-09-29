// Integer hashes for stateless particles (M07 §6.8.2 item 4; r16 §3.2.1). Owner: M07.
// PCG hash on uint (Jarzynski and Olano 2020): independent channels per particle id, no sin() hash (banding at large
// arguments). u01 keeps the top 24 bits (exact in float32).
import { float, uint } from 'three/tsl'

type N = any // TSL nodes

export function pcg(v: N): N {
  const state: N = uint(v).mul(uint(747796405)).add(uint(2891336453))
  const word: N = state.shiftRight(state.shiftRight(uint(28)).add(uint(4))).bitXor(state).mul(uint(277803737))
  return word.shiftRight(uint(22)).bitXor(word)
}

/** uniform [0, 1) from a uint hash */
export function u01(h: N): N {
  return float(h.shiftRight(uint(8))).mul(1.0 / 16777216.0)
}

/** k-th independent [0, 1) channel of particle id (float) */
export function rand(id: N, k: number): N {
  return u01(pcg(uint(id).mul(uint(8)).add(uint(k))))
}

/** CPU mirror (tests) */
export function pcgCPU(v: number): number {
  const state = (Math.imul(v >>> 0, 747796405) + 2891336453) >>> 0
  const word = Math.imul(((state >>> ((state >>> 28) + 4)) ^ state) >>> 0, 277803737) >>> 0
  return ((word >>> 22) ^ word) >>> 0
}
export function randCPU(id: number, k: number): number {
  return (pcgCPU((id * 8 + k) >>> 0) >>> 8) / 16777216
}
