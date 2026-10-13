// Target geometry (M18-FR-004, FR-016, FR-022, FR-024; M18-M19 PRD §6.2.2, §6.3, §7.8). Owner: M18.
// The same formulas as python/awr/sim/targets/{geometry,effective}.py (keepout volumes are compared on the shared fixture
// apps/web/tests/m18/keepout_cases.json): critical dimension d_c, sensitivity volumes in the world (ENU m, psi_enu rad,
// east 0, counter-clockwise), inside test, signed distance and the 3 s entry prediction of manual flight (HUD warning,
// AWR-04 §10.6; the server clamp is keepout_guard). Moving targets plan sectors as full circles.

export type BodyShape = 'sphere' | 'hemisphere' | 'cylinder' | 'sector'
export type BodyKind = 'visual' | 'acoustic' | 'radar' | 'rf'
export type CriterionMode = 'geometric' | 'physical'

/** one configured sensitivity volume (contract target/sensitivity.schema.json) */
export interface SensitivityBody {
  kind: BodyKind
  shape: BodyShape
  radius_max_m: number
  z_min_m?: number
  z_max_m?: number
  half_angle_deg?: number
  azimuth_offset_deg?: number
  gain_back_db?: number
  enabled?: boolean
}

/** a volume in world coordinates (contract target/keepout.schema.json; z values absolute) */
export interface Volume {
  shape: BodyShape
  center: [number, number, number]
  rM: number
  zMinM: number
  zMaxM: number
  yawRad: number
  halfAngleRad: number
  volIdx: number
  kind: BodyKind
}

export type Vec3 = readonly [number, number, number] | Float64Array

const TWO_PI = 2 * Math.PI
export const wrapPi = (a: number): number => ((((a + Math.PI) % TWO_PI) + TWO_PI) % TWO_PI) - Math.PI
export const DEFAULT_MARGIN_M = 20 + 3 * 0.02

/** d_c = sqrt(|sin e| l w + |cos e| (|cos b| w h + |sin b| l h)); worst: the side term is min(w h, l h) (AWR-04 §2.2) */
export function criticalDim(l: number, w: number, h: number, epsRad: number, betaRad: number, worst = false): number {
  const side = worst ? Math.min(w * h, l * h) : Math.abs(Math.cos(betaRad)) * w * h + Math.abs(Math.sin(betaRad)) * l * h
  return Math.sqrt(Math.abs(Math.sin(epsRad)) * l * w + Math.abs(Math.cos(epsRad)) * side)
}

export function isMoving(motionMode: string | undefined, speedMps: number): boolean {
  return (motionMode ?? 'static') !== 'static' || speedMps > 0.1
}

/** configured bodies -> world volumes (enabled visual and acoustic only unless kinds says otherwise); radiusOf replaces
 * the radius of body i (physical effective volume); sectors become cylinders for moving targets when movingAsCircle */
export function bodyVolumes(bodies: readonly SensitivityBody[], pos: Vec3, yawRad: number, marginM = 0, movingAsCircle = false,
  kinds: readonly BodyKind[] = ['visual', 'acoustic'], radiusOf?: readonly (number | null)[]): Volume[] {
  const out: Volume[] = []
  const x = pos[0], y = pos[1], z = pos[2]
  for (let i = 0; i < bodies.length; i++) {
    const b = bodies[i]
    if (b.enabled === false || !kinds.includes(b.kind)) continue
    let shape: BodyShape = b.shape
    const r = radiusOf && radiusOf[i] != null ? (radiusOf[i] as number) : b.radius_max_m
    let half = ((b.half_angle_deg ?? 60) * Math.PI) / 180
    if (shape === 'sector' && (movingAsCircle || half >= Math.PI - 1e-9)) shape = 'cylinder'
    const rr = r + marginM
    if (shape === 'sector' && marginM > 0) half = Math.min(Math.PI, half + Math.asin(Math.min(1, marginM / Math.max(rr, 1e-6))))
    const ball = shape === 'sphere' || shape === 'hemisphere'
    out.push({
      shape, center: [x, y, z], rM: rr,
      zMinM: ball ? z : z + (b.z_min_m ?? 0) - marginM, zMaxM: ball ? z + rr : z + (b.z_max_m ?? 300) + marginM,
      yawRad: wrapPi(yawRad + ((b.azimuth_offset_deg ?? 0) * Math.PI) / 180), halfAngleRad: half, volIdx: i, kind: b.kind,
    })
  }
  return out
}

/** keepout volumes of the geometric mode (configured bodies + margin; moving targets: full circles); the physical mode
 * needs the server's effective radii (detail.sensitivity[].radius_eff_m), passed through radiusOf */
export function keepoutVolumes(bodies: readonly SensitivityBody[], pos: Vec3, yawRad: number, moving: boolean, marginM = DEFAULT_MARGIN_M,
  radiusOf?: readonly (number | null)[]): Volume[] {
  return bodyVolumes(bodies, pos, yawRad, marginM, moving, ['visual', 'acoustic'], radiusOf)
}

export function insideVolume(v: Volume, px: number, py: number, pz: number): boolean {
  const dx = px - v.center[0], dy = py - v.center[1], dz = pz - v.center[2]
  if (v.shape === 'sphere' || v.shape === 'hemisphere') {
    const ok = dx * dx + dy * dy + dz * dz <= v.rM * v.rM
    return v.shape === 'hemisphere' ? ok && dz >= 0 : ok
  }
  const rh2 = dx * dx + dy * dy
  if (rh2 > v.rM * v.rM || pz < v.zMinM || pz > v.zMaxM) return false
  if (v.shape === 'cylinder' || v.halfAngleRad >= Math.PI || rh2 < 1e-12) return true
  return Math.abs(wrapPi(Math.atan2(dy, dx) - v.yawRad)) <= v.halfAngleRad
}

/** signed distance (outside > 0) and the outward unit normal (17 §17.7.6 keepout_guard; same as geometry.signed_distance) */
export function signedDistance(v: Volume, px: number, py: number, pz: number): { d: number; n: [number, number, number] } {
  const dx = px - v.center[0], dy = py - v.center[1], dz = pz - v.center[2]
  if (v.shape === 'sphere' || v.shape === 'hemisphere') {
    if (v.shape === 'hemisphere' && dz < 0) {
      const rh = Math.hypot(dx, dy)
      if (rh <= v.rM) return { d: -dz, n: [0, 0, -1] }
      const qx = (dx / rh) * v.rM, qy = (dy / rh) * v.rM
      const wx = dx - qx, wy = dy - qy, wz = dz
      const nn = Math.hypot(wx, wy, wz)
      return { d: nn, n: [wx / nn, wy / nn, wz / nn] }
    }
    const r3 = Math.hypot(dx, dy, dz)
    return { d: r3 - v.rM, n: r3 > 1e-9 ? [dx / r3, dy / r3, dz / r3] : [0, 0, 1] }
  }
  const half = v.shape === 'sector' ? v.halfAngleRad : Math.PI
  const rh = Math.hypot(dx, dy)
  let dh: number
  let nh: [number, number]
  const inAng = half >= Math.PI - 1e-9 || Math.abs(wrapPi(Math.atan2(dy, dx) - v.yawRad)) <= half
  if (inAng && rh <= v.rM) {
    dh = rh - v.rM
    nh = rh > 1e-9 ? [dx / rh, dy / rh] : [1, 0]
  } else if (inAng) {
    dh = rh - v.rM
    nh = [dx / rh, dy / rh]
  } else {
    let best = Infinity
    let c: [number, number] = [0, 0]
    for (const s of [-1, 1]) {
      const ux = Math.cos(v.yawRad + s * half), uy = Math.sin(v.yawRad + s * half)
      const t = Math.min(Math.max(dx * ux + dy * uy, 0), v.rM)
      const d = Math.hypot(dx - ux * t, dy - uy * t)
      if (d < best) {
        best = d
        c = [ux * t, uy * t]
      }
    }
    dh = best
    nh = [(dx - c[0]) / Math.max(best, 1e-9), (dy - c[1]) / Math.max(best, 1e-9)]
  }
  let dv: number
  let nv: number
  if (pz > v.zMaxM) [dv, nv] = [pz - v.zMaxM, 1]
  else if (pz < v.zMinM) [dv, nv] = [v.zMinM - pz, -1]
  else [dv, nv] = [-Math.min(pz - v.zMinM, v.zMaxM - pz), pz - v.zMinM > v.zMaxM - pz ? 1 : -1]
  if (dh > 0 && dv > 0) {
    const n = Math.hypot(dh, dv)
    return { d: n, n: [(nh[0] * dh) / n, (nh[1] * dh) / n, (nv * dv) / n] }
  }
  return dh >= dv ? { d: dh, n: [nh[0], nh[1], 0] } : { d: dv, n: [0, 0, nv] }
}

/** seconds until the straight path p + v t enters any volume within horizonS (0.1 s steps; 0 when already inside), or
 * null (M18-FR-022: the HUD warns 3 s before the predicted entry) */
export function predictEntry(p: Vec3, v: Vec3, vols: readonly Volume[], horizonS = 3, stepS = 0.1): number | null {
  const n = Math.round(horizonS / stepS)
  for (let k = 0; k <= n; k++) {
    const t = k * stepS
    const x = p[0] + v[0] * t, y = p[1] + v[1] * t, z = p[2] + v[2] * t
    for (const vol of vols) if (insideVolume(vol, x, y, z)) return t
  }
  return null
}

/** keepout volume in the wire form (target/keepout.schema.json) */
export function volumeToWire(v: Volume, id: string): Record<string, unknown> {
  const d: Record<string, unknown> = {
    id, shape: v.shape, center_enu_m: [...v.center], r_m: v.rM, z_min_m: v.zMinM, z_max_m: v.zMaxM,
    source: { kind: 'target', id: id.split('/')[0], vol_idx: v.volIdx },
  }
  if (v.shape === 'sector') {
    d.yaw_rad = wrapPi(v.yawRad)
    d.half_angle_rad = Math.min(Math.PI, v.halfAngleRad)
  }
  return d
}
