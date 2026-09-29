// MID-360 parametric scan pattern (M13-FR-050; M13 §6.5.10; r04 §3.1.3), D1 stub: same source as
// python/awr/sim/sensors/lidar/pattern.py. Point i: beam k = i & 3, beam sample n = i >> 2;
// el = EC(n) + S_EL[k]·cos(θ + 1.7°), az = 270.08 + AZSTEP·n + S_AZ[k]·sin θ / cos EC(n) (mod 360), θ = 2π n / 274.1.
// The carrier period is exactly one frame (5000 beam samples = 0.1 s), so EC and 1/cos EC are frame invariant and are
// tabulated once; per frame only the fan phase θ0 and the azimuth start are reduced in float64, the rest expands with the
// angle-addition formulas (a TSL port must keep "float64 reduction per frame, frame-local offsets in the shader").
// Tables are built on first use; the call itself allocates nothing. Pass Float64Array outputs for golden parity (<= 1e-6°).

export const MID360 = {
  C: [20.4535, 23.5232, 1.1247, 2.3002, 0.2232, 0.1565, 0.033],
  S_EL: [2.765, 0.921, -0.921, -2.765],
  S_AZ: [1.248, 0.617, -0.617, -1.248],
  ROT: 274.1,
  AZSTEP: -1.31341,
  CARRIER: 5000,
  NPF: 20000,
  AZ0: 270.08,
  FAN_PHASE_DEG: 1.7,
  FOV_EL_MIN_DEG: -7.2,
  FOV_EL_MAX_DEG: 52.2,
} as const

interface Tables {
  ec: Float64Array
  cd: Float64Array
  sd: Float64Array
  cd0: Float64Array
  sd0: Float64Array
  sel: Float64Array
  sazSec: Float64Array
  azl: Float64Array
}
let T: Tables | null = null

function tables(): Tables {
  if (T) return T
  const { C, S_EL, S_AZ, ROT, AZSTEP, CARRIER, NPF, FAN_PHASE_DEG } = MID360
  const t: Tables = {
    ec: new Float64Array(NPF), cd: new Float64Array(NPF), sd: new Float64Array(NPF), cd0: new Float64Array(NPF),
    sd0: new Float64Array(NPF), sel: new Float64Array(NPF), sazSec: new Float64Array(NPF), azl: new Float64Array(NPF),
  }
  const fan = (FAN_PHASE_DEG * Math.PI) / 180
  for (let i = 0; i < NPF; i++) {
    const k = i & 3
    const n = i >> 2
    const ph = (2 * Math.PI * n) / CARRIER
    let ec = C[0]
    for (let j = 1; j <= 6; j++) ec += C[j] * Math.cos(j * ph)
    const dth = (2 * Math.PI * n) / ROT
    t.ec[i] = ec
    t.cd[i] = Math.cos(dth + fan)
    t.sd[i] = Math.sin(dth + fan)
    t.cd0[i] = Math.cos(dth)
    t.sd0[i] = Math.sin(dth)
    t.sel[i] = S_EL[k]
    t.sazSec[i] = S_AZ[k] / Math.cos((ec * Math.PI) / 180)
    t.azl[i] = AZSTEP * n
  }
  T = t
  return t
}

/** Python-compatible float modulo (result has the sign of the divisor) */
function pmod(x: number, m: number): number {
  const r = x % m
  return r !== 0 && r < 0 !== m < 0 ? r + m : r
}

/** the 20 000 ray directions of frame frameIdx (degrees; lidar frame FLU: azimuth from +X counter-clockwise, elevation up) */
export function mid360Pattern(frameIdx: number, outAzDeg: Float32Array | Float64Array, outElDeg: Float32Array | Float64Array): void {
  const t = tables()
  const { ROT, AZSTEP, CARRIER, NPF, AZ0 } = MID360
  const n0 = frameIdx * CARRIER
  const th0 = 2 * Math.PI * (pmod(n0, ROT) / ROT)
  const c0 = Math.cos(th0)
  const s0 = Math.sin(th0)
  const az0 = pmod(AZ0 + pmod(AZSTEP * n0, 360), 360)
  for (let i = 0; i < NPF; i++) {
    outElDeg[i] = t.sel[i] * (t.cd[i] * c0 - t.sd[i] * s0) + t.ec[i]
    outAzDeg[i] = pmod(t.sazSec[i] * (t.sd0[i] * c0 + t.cd0[i] * s0) + t.azl[i] + az0, 360)
  }
}
