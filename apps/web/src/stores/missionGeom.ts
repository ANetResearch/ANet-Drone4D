// Formation slots and CAPT assignment (M10-FR-041, FR-042; M10 §6.5.11, §8.1; AWR-18 §8.3). Owner: M10.
// Pure functions, golden-tested against the Python reference (python/awr/swarm/formation/{slots,capt}.py) with
// apps/web/tests/mission/golden/formation.json: slots equal element-wise within 1e-9 m, assignments equal, costs within
// a mixed tolerance. Formation frame is FLU (x forward, y left, z up); offsets are returned as a flat Float64Array
// [x0, y0, z0, x1, ...]. The virtual anchor (offsets minus their centroid) is the default (r26 §3.6).

export type FormationShape = 'line' | 'column' | 'v' | 'echelon' | 'grid' | 'circle'
export const FORMATION_SHAPES: readonly FormationShape[] = ['line', 'column', 'v', 'echelon', 'grid', 'circle']

export function formationSlots(shape: FormationShape, n: number, spacingM: number, halfAngleDeg = 35, cols?: number, virtual = true): Float64Array {
  const s = spacingM
  const al = (halfAngleDeg * Math.PI) / 180
  const off = new Float64Array(n * 3)
  if (shape === 'grid') {
    const c = Math.max(1, cols && cols > 0 ? cols : Math.ceil(Math.sqrt(n)))
    const rc: [number, number][] = []
    for (let k = 0; k < n; k++) rc.push([Math.floor(k / c), k % c])
    rc.sort((a, b) => a[0] - b[0] || Math.abs(a[1] - (c - 1) / 2) - Math.abs(b[1] - (c - 1) / 2) || a[1] - b[1])
    for (let k = 0; k < n; k++) {
      off[3 * k] = -rc[k][0] * s
      off[3 * k + 1] = ((c - 1) / 2 - rc[k][1]) * s
    }
    const x0 = off[0]
    const y0 = off[1]
    for (let k = 0; k < n; k++) {
      off[3 * k] -= x0
      off[3 * k + 1] -= y0
    }
  } else if (shape === 'circle') {
    const m = n - 1
    if (m >= 1) {
      const R = m > 1 ? Math.max(s, s / (2 * Math.sin(Math.PI / m))) : s
      for (let k = 1; k < n; k++) {
        const a = (2 * Math.PI * (k - 1)) / m
        off[3 * k] = R * Math.cos(a)
        off[3 * k + 1] = R * Math.sin(a)
      }
    }
  } else {
    for (let k = 1; k < n; k++) {
      const r = Math.floor((k + 1) / 2)
      const side = k % 2 === 1 ? 1 : -1
      if (shape === 'line') off[3 * k + 1] = side * r * s
      else if (shape === 'column') off[3 * k] = -k * s
      else if (shape === 'v') {
        off[3 * k] = -r * s * Math.cos(al)
        off[3 * k + 1] = side * r * s * Math.sin(al)
      } else {
        off[3 * k] = -k * s * Math.cos(al)
        off[3 * k + 1] = -k * s * Math.sin(al)
      }
    }
  }
  if (virtual && n > 0) {
    for (let d = 0; d < 3; d++) {
      let m = 0
      for (let k = 0; k < n; k++) m += off[3 * k + d]
      m /= n
      for (let k = 0; k < n; k++) off[3 * k + d] -= m
    }
  }
  return off
}

/** Hungarian (Kuhn-Munkres with potentials, O(n^3)); cost is row-major n x m with n <= m. Returns column per row. */
export function hungarian(cost: Float64Array, n: number, m: number): Int32Array {
  const INF = Number.POSITIVE_INFINITY
  const u = new Float64Array(n + 1)
  const v = new Float64Array(m + 1)
  const p = new Int32Array(m + 1)
  const way = new Int32Array(m + 1)
  for (let i = 1; i <= n; i++) {
    p[0] = i
    let j0 = 0
    const minv = new Float64Array(m + 1).fill(INF)
    const used = new Uint8Array(m + 1)
    do {
      used[j0] = 1
      const i0 = p[j0]
      let delta = INF
      let j1 = 0
      for (let j = 1; j <= m; j++) {
        if (used[j]) continue
        const cur = cost[(i0 - 1) * m + (j - 1)] - u[i0] - v[j]
        if (cur < minv[j]) {
          minv[j] = cur
          way[j] = j0
        }
        if (minv[j] < delta) {
          delta = minv[j]
          j1 = j
        }
      }
      for (let j = 0; j <= m; j++) {
        if (used[j]) {
          u[p[j]] += delta
          v[j] -= delta
        } else minv[j] -= delta
      }
      j0 = j1
    } while (p[j0] !== 0)
    do {
      const j1 = way[j0]
      p[j0] = p[j1]
      j0 = j1
    } while (j0)
  }
  const out = new Int32Array(n).fill(-1)
  for (let j = 1; j <= m; j++) if (p[j] > 0) out[p[j] - 1] = j - 1
  return out
}

/** CAPT: member i (P row i) -> slot a[i] (G row), minimising the sum of squared distances. P, G flat [x, y, z, ...]. */
export function captAssign(P: Float64Array, G: Float64Array): Int32Array {
  const n = P.length / 3
  const m = G.length / 3
  const C = new Float64Array(n * m)
  for (let i = 0; i < n; i++)
    for (let j = 0; j < m; j++) {
      const dx = P[3 * i] - G[3 * j]
      const dy = P[3 * i + 1] - G[3 * j + 1]
      const dz = P[3 * i + 2] - G[3 * j + 2]
      C[i * m + j] = dx * dx + dy * dy + dz * dz
    }
  return hungarian(C, n, m)
}

export function captCost(P: Float64Array, G: Float64Array, a: Int32Array): number {
  let c = 0
  for (let i = 0; i < a.length; i++) {
    const j = a[i]
    const dx = P[3 * i] - G[3 * j]
    const dy = P[3 * i + 1] - G[3 * j + 1]
    const dz = P[3 * i + 2] - G[3 * j + 2]
    c += dx * dx + dy * dy + dz * dz
  }
  return c
}

/** Rotate FLU offsets by the formation heading psi (ENU, rad) into world ENU offsets. */
export function rotateSlots(off: Float64Array, psi: number): Float64Array {
  const c = Math.cos(psi)
  const s = Math.sin(psi)
  const out = new Float64Array(off.length)
  for (let k = 0; k < off.length; k += 3) {
    out[k] = c * off[k] - s * off[k + 1]
    out[k + 1] = s * off[k] + c * off[k + 1]
    out[k + 2] = off[k + 2]
  }
  return out
}
