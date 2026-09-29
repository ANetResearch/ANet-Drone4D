// Pure interpolation kernels of the interpolation ring (M12 §6.4, §9.2; ADR-046; r15 §3.9, r18 §3.3). Owner: M12.
// Cubic Hermite position with the sample velocities as end tangents (m = v * h), shortest-arc slerp with an nlerp
// fallback above dot 0.9995, and body-rate attitude integration q(t + dt) = q ⊗ exp(ω dt / 2) (ω in the body FLU frame,
// quaternions [x, y, z, w] WORLD <- BODY). No allocation; every function writes into an output array at an offset.
// The Python oracle is tools/bench/rec/interp_error.py (same basis functions).
export type F32 = Float32Array

/** Hermite basis weights at s in [0, 1] (h00, h10, h01, h11) */
export function hermiteBasis(s: number, out: Float64Array): void {
  const s2 = s * s
  const s3 = s2 * s
  out[0] = 2 * s3 - 3 * s2 + 1
  out[1] = s3 - 2 * s2 + s
  out[2] = -2 * s3 + 3 * s2
  out[3] = s3 - s2
}

/** p(s) of the cubic Hermite through (p0, v0) and (p1, v1) with interval h seconds; 3 components written at out[o] */
export function hermite3(p0: ArrayLike<number>, v0: ArrayLike<number>, p1: ArrayLike<number>, v1: ArrayLike<number>, h: number, s: number, out: F32 | Float64Array, o: number): void {
  const s2 = s * s
  const s3 = s2 * s
  const h00 = 2 * s3 - 3 * s2 + 1
  const h10 = (s3 - 2 * s2 + s) * h
  const h01 = -2 * s3 + 3 * s2
  const h11 = (s3 - s2) * h
  for (let j = 0; j < 3; j++) out[o + j] = h00 * p0[j] + h10 * v0[j] + h01 * p1[j] + h11 * v1[j]
}

/** the same on strided rings: samples a and b at index ia, ib (x 3) of p and v */
export function hermiteRing(p: F32, v: F32, ia: number, ib: number, h: number, s: number, out: F32, o: number): void {
  const s2 = s * s
  const s3 = s2 * s
  const h00 = 2 * s3 - 3 * s2 + 1
  const h10 = (s3 - 2 * s2 + s) * h
  const h01 = -2 * s3 + 3 * s2
  const h11 = (s3 - s2) * h
  const a = 3 * ia
  const b = 3 * ib
  out[o] = h00 * p[a] + h10 * v[a] + h01 * p[b] + h11 * v[b]
  out[o + 1] = h00 * p[a + 1] + h10 * v[a + 1] + h01 * p[b + 1] + h11 * v[b + 1]
  out[o + 2] = h00 * p[a + 2] + h10 * v[a + 2] + h01 * p[b + 2] + h11 * v[b + 2]
}

/** shortest-arc slerp of a[ao..ao+4) and b[bo..bo+4) at u, normalised; nlerp when dot > 0.9995 */
export function slerpInto(a: F32, ao: number, b: F32, bo: number, u: number, out: F32, oo: number): void {
  let bx = b[bo]
  let by = b[bo + 1]
  let bz = b[bo + 2]
  let bw = b[bo + 3]
  let dot = a[ao] * bx + a[ao + 1] * by + a[ao + 2] * bz + a[ao + 3] * bw
  if (dot < 0) {
    dot = -dot
    bx = -bx
    by = -by
    bz = -bz
    bw = -bw
  }
  let wa: number
  let wb: number
  if (dot > 0.9995) {
    wa = 1 - u
    wb = u
  } else {
    const th = Math.acos(Math.min(1, dot))
    const s = Math.sin(th)
    wa = Math.sin((1 - u) * th) / s
    wb = Math.sin(u * th) / s
  }
  const x = wa * a[ao] + wb * bx
  const y = wa * a[ao + 1] + wb * by
  const z = wa * a[ao + 2] + wb * bz
  const w = wa * a[ao + 3] + wb * bw
  const l = Math.hypot(x, y, z, w) || 1
  out[oo] = x / l
  out[oo + 1] = y / l
  out[oo + 2] = z / l
  out[oo + 3] = w / l
}

/** q(dt) = q ⊗ exp(ω dt / 2) with ω (rad/s) in the body frame; dt seconds; result normalised into out[o..o+4) */
export function integrateOmegaInto(q: F32, iq: number, w: F32, iw: number, dt: number, out: F32, o: number): void {
  const wx = w[iw]
  const wy = w[iw + 1]
  const wz = w[iw + 2]
  const x1 = q[iq]
  const y1 = q[iq + 1]
  const z1 = q[iq + 2]
  const w1 = q[iq + 3]
  const rate = Math.hypot(wx, wy, wz)
  const half = 0.5 * rate * dt
  if (!(rate > 1e-12) || !Number.isFinite(half)) {
    out[o] = x1
    out[o + 1] = y1
    out[o + 2] = z1
    out[o + 3] = w1
    return
  }
  const k = Math.sin(half) / rate
  const x2 = wx * k
  const y2 = wy * k
  const z2 = wz * k
  const w2 = Math.cos(half)
  const x = w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2
  const y = w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2
  const z = w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2
  const ww = w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2
  const l = Math.hypot(x, y, z, ww) || 1
  out[o] = x / l
  out[o + 1] = y / l
  out[o + 2] = z / l
  out[o + 3] = ww / l
}
