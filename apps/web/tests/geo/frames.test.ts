// M02 frames.ts against the Python golden (M02-AC-001, 007, 008; D1-AC-13); mixed tolerance |a - b| <= atol + rtol |b|
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Object3D, Vector3 } from 'three'
import {
  CGCS2000, R_THREE_ENU, WGS84, ecefToLlaInto, enuToNedInto, enuToThreeInto, fluToFrdInto, headingDeg, llaToEcefInto,
  llaToWorldInto, makeAnchor, quatEnuFluFromNedFrdInto, quatEnuToThreeInto, sim3InterpolateInto, threeToEnuInto,
  ueCmToWorldInto, ueRotToQuatInto, worldToLlaInto, yawEnuFromHeadingDeg, yawNedFromEnu,
} from '@/engine/geo/frames'
import type { AnchorJson, Sim3 } from '@/engine/geo/frames'

type Kind = 'position_m' | 'angle_deg' | 'angle_rad' | 'dimensionless' | 'velocity_mps' | 'exact'
interface Case { fn: string; args: Record<string, unknown>; out: Record<string, unknown>; kinds: Record<string, Kind>; ts: boolean }
interface Group { tolerance: { rtol: number; atol: Record<string, number> }; cases: Case[] }

const ROOT = new URL('../../../../', import.meta.url)
const load = (g: string): Group =>
  JSON.parse(readFileSync(new URL(`packages/contracts/golden/frames/${g}.json`, ROOT), 'utf8')) as Group

const flat = (x: unknown): number[] => {
  if (Array.isArray(x)) return x.flatMap(flat)
  if (x !== null && typeof x === 'object') return Object.keys(x).sort().flatMap((k) => flat((x as Record<string, unknown>)[k]))
  return [x as number]
}

function close(a: number, b: number, kind: Kind, tol: Group['tolerance']): boolean {
  if (kind === 'exact') return a === b
  let k: string = kind
  if (kind === 'angle_deg') {
    a = (a * Math.PI) / 180
    b = (b * Math.PI) / 180
    k = 'angle_rad'
  }
  return Math.abs(a - b) <= tol.atol[k] + tol.rtol * Math.abs(b)
}

const v = (x: unknown) => x as number[]
const ell = (d: unknown) => (d === 'CGCS2000' ? CGCS2000 : WGS84)

function run(c: Case): Record<string, unknown> {
  const a = c.args
  const o3 = [0, 0, 0]
  const o4 = [0, 0, 0, 0]
  switch (c.fn) {
    case 'lla_to_ecef':
      return { p: [...llaToEcefInto(o3, a.lat_deg as number, a.lon_deg as number, a.h_m as number, ell(a.datum))] }
    case 'ecef_to_lla': {
      const p = v(a.p)
      const r = ecefToLlaInto(o3, p[0], p[1], p[2], ell(a.datum))
      return { lat_deg: r[0], lon_deg: r[1], h_m: r[2] }
    }
    case 'world_to_lla': {
      const p = v(a.p)
      const r = worldToLlaInto(o3, p[0], p[1], p[2], makeAnchor(a.anchor as AnchorJson)!)
      return { lat_deg: r[0], lon_deg: r[1], h_m: r[2] }
    }
    case 'lla_to_world':
      return { p: [...llaToWorldInto(o3, a.lat_deg as number, a.lon_deg as number, a.h_m as number, makeAnchor(a.anchor as AnchorJson)!)] }
    case 'enu_to_ned': {
      const p = v(a.v)
      return { v: [...enuToNedInto(o3, p[0], p[1], p[2])] }
    }
    case 'flu_to_frd': {
      const p = v(a.v)
      return { v: [...fluToFrdInto(o3, p[0], p[1], p[2])] }
    }
    case 'q_enuflu_from_nedfrd': {
      const q = v(a.q_wxyz)
      return { q_xyzw: [...quatEnuFluFromNedFrdInto(o4, q[0], q[1], q[2], q[3])] }
    }
    case 'yaw_ned_from_enu':
      return { yaw: yawNedFromEnu(a.psi as number) }
    case 'heading_deg':
      return { deg: headingDeg(a.psi as number) }
    case 'yaw_enu_from_heading_deg':
      return { yaw: yawEnuFromHeadingDeg(a.h_deg as number) }
    case 'enu_to_three': {
      const p = v(a.v)
      return { v: [...enuToThreeInto(o3, p[0], p[1], p[2])] }
    }
    case 'three_to_enu': {
      const p = v(a.v)
      return { v: [...threeToEnuInto(o3, p[0], p[1], p[2])] }
    }
    case 'quat_enu_to_three': {
      const q = v(a.q_xyzw)
      return { q_xyzw: [...quatEnuToThreeInto(o4, q[0], q[1], q[2], q[3])] }
    }
    case 'ue_cm_to_world': {
      const p = v(a.p_ue_cm), t = v(a.t_world_m)
      return { p: [...ueCmToWorldInto(o3, p[0], p[1], p[2], t[0], t[1], t[2])] }
    }
    case 'ue_rot_to_q': {
      const q = ueRotToQuatInto(o4, a.pitch_deg as number, a.roll_deg as number, a.yaw_deg as number)
      const [x, y, z, w] = q
      // first column of R(q): body forward in world
      const fwd = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)]
      return { q_xyzw: [...q], forward: fwd }
    }
    case 'sim3_interpolate': {
      const r = sim3InterpolateInto(new Array(8).fill(0), a.a as Sim3, a.b as Sim3, a.u as number)
      return { s: r[0], q: [r[1], r[2], r[3], r[4]], t: [r[5], r[6], r[7]] }
    }
  }
  throw new Error(`no TS implementation for ${c.fn}`)
}

describe.each(['geodesy', 'enu_ned', 'three', 'ue', 'sim3'])('frames golden: %s', (group) => {
  const g = load(group)
  const cases = g.cases.filter((c) => c.ts)
  it(`${cases.length} TS cases match frames.py`, () => {
    expect(cases.length).toBeGreaterThan(0)
    const bad: string[] = []
    for (const c of cases) {
      const got = run(c)
      for (const [key, kind] of Object.entries(c.kinds)) {
        const exp = flat(c.out[key]), act = flat(got[key])
        if (exp.length !== act.length || exp.some((e, i) => !close(act[i], e, kind, g.tolerance))) {
          bad.push(`${c.fn}.${key} exp ${exp.slice(0, 3).join(',')} got ${act.slice(0, 3).join(',')}`)
        }
      }
    }
    expect(bad.slice(0, 5)).toEqual([])
  })
})

describe('frames: structural checks', () => {
  it('at least 3000 golden cases across TS groups (M02-AC-001)', () => {
    const n = ['geodesy', 'enu_ned', 'three', 'ue', 'sim3'].reduce((s, g) => s + load(g).cases.filter((c) => c.ts).length, 0)
    expect(n).toBeGreaterThanOrEqual(3000)
  })

  it('enuToThree equals a three.js Object3D with rotation.x = -pi/2 (M02-AC-007)', () => {
    const o = new Object3D()
    o.rotation.x = -Math.PI / 2
    o.updateMatrixWorld(true)
    const out = [0, 0, 0]
    for (const [e, n, u] of [[3, 4, 5], [-120.5, 33.25, 7], [1e4, -2e4, 380]]) {
      const v = new Vector3(e, n, u).applyMatrix4(o.matrixWorld)
      enuToThreeInto(out, e, n, u)
      expect(Math.abs(v.x - out[0])).toBeLessThanOrEqual(1e-12 * Math.max(1, Math.abs(e)))
      expect(Math.abs(v.y - out[1])).toBeLessThanOrEqual(1e-12 * Math.max(1, Math.abs(u)))
      expect(Math.abs(v.z - out[2])).toBeLessThanOrEqual(1e-12 * Math.max(1, Math.abs(n)))
    }
    const m = o.matrixWorld.elements // column major
    const rows = [m[0], m[4], m[8], m[1], m[5], m[9], m[2], m[6], m[10]]
    rows.forEach((x, i) => expect(Math.abs(x - R_THREE_ENU[i])).toBeLessThanOrEqual(1e-15))
    enuToThreeInto(out, 3, 4, 5)
    expect(out).toEqual([3, 5, -4])
    threeToEnuInto(out, out[0], out[1], out[2])
    expect(out).toEqual([3, 4, 5])
  })

  it('UE forward axes (M02-AC-008)', () => {
    const cases: [number, number, number, number[]][] = [[0, 0, 0, [0, 1, 0]], [0, 0, 90, [1, 0, 0]], [90, 0, 0, [0, 0, -1]]]
    for (const [p, r, y, f] of cases) {
      const [qx, qy, qz, qw] = ueRotToQuatInto([0, 0, 0, 0], p, r, y)
      const fwd = [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy + qz * qw), 2 * (qx * qz - qy * qw)]
      fwd.forEach((x, i) => expect(Math.abs(x - f[i])).toBeLessThanOrEqual(1e-12))
    }
  })

  it('invalid anchor yields null (unknown value, AWR-03 §5.7)', () => {
    expect(makeAnchor(null)).toBeNull()
    expect(makeAnchor({ latDeg: Number.NaN, lonDeg: 0, hEllipsoidM: 0 })).toBeNull()
    expect(makeAnchor({ latDeg: 0, lonDeg: 0, hEllipsoidM: 0, datum: 'ED50' as 'WGS84' })).toBeNull()
  })

  it('ellipsoid constants live only in frames.ts (M02-FR-012)', async () => {
    const { readdirSync, statSync } = await import('node:fs')
    const src = new URL('apps/web/src/', ROOT)
    const hits: string[] = []
    const walk = (u: URL) => {
      for (const n of readdirSync(u)) {
        const f = new URL(n, u)
        if (statSync(f).isDirectory()) walk(new URL(`${n}/`, u))
        else if (/\.tsx?$/.test(n) && !f.pathname.endsWith('engine/geo/frames.ts')) {
          if (/6378137|298\.257223563|298\.257222101/.test(readFileSync(f, 'utf8'))) hits.push(f.pathname)
        }
      }
    }
    walk(src)
    expect(hits).toEqual([])
  })
})
