// M07 TS implementation vs the environment golden (M07-AC-002; D1-AC-13 environment part; AWR-03 §5.1 rule 8).
// Reads packages/contracts/env/golden/*.json (generated from tools/contracts/env_ref.py) and evaluates every case with
// engine/environment; mixed tolerance |a - b| <= atol + 1e-9 |b| (position 1e-6 m, velocity 1e-9 m/s, dimensionless
// 1e-12; angle_deg compared in radians at 1e-12; exact means equal). The Python side runs tests/environment/test_parity.py.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { PRESETS_SHA256 } from '@awr/contracts/presets'
import { enuToNed, enuToThree, eDir, fromToUv, nDir, shortestArc, threeToEnu, uvToFrom } from '@/engine/environment/state/conventions'
import { derive, DERIVED_KEYS, newDerived, type EnvDerived } from '@/engine/environment/state/derive'
import { evalEnv, makeTransition } from '@/engine/environment/state/evalEnv'
import { advance, anchorsInitial } from '@/engine/environment/state/anchors'
import { newAnchors, type Anchors } from '@/engine/environment/state/keyframe'
import { NF, PRESETS_MODEL } from '@/engine/environment/state/presets'
import { isa, sectorSlots } from '@/engine/environment/state/isa'
import { kimQ, lidarTwoWay, opticalDepth, sigmaAt, sigmaLambda } from '@/engine/environment/atmosphere/optics'
import { gust, gustCreate, gustExpired } from '@/engine/environment/wind/gust'
import { profile, profileCfg } from '@/engine/environment/wind/profile'
import { decodeAWRV } from '@/engine/environment/wind/awrv'
import { TurbBoxCPU, milSigma } from '@/engine/environment/wind/turbBox'

type Tol = { rtol: number; atol: Record<string, number> }
interface Case { fn: string; args: Record<string, any>; out: Record<string, any>; kinds: Record<string, string> } // eslint-disable-line @typescript-eslint/no-explicit-any
interface Gold { presets_sha256: string; tolerance: Tol; cases: Case[]; asset?: string }

const ROOT = new URL('../../../../', import.meta.url)
const gold = (g: string): Gold => JSON.parse(readFileSync(new URL(`packages/contracts/env/golden/${g}.json`, ROOT), 'utf8')) as Gold

function close(a: unknown, b: unknown, kind: string, tol: Tol): boolean {
  if (Array.isArray(b)) return Array.isArray(a) && a.length === b.length && b.every((y, i) => close(a[i], y, kind, tol))
  if (b !== null && typeof b === 'object') return Object.keys(b).every((k) => close((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k], kind, tol))
  if (kind === 'exact' || typeof b === 'boolean' || typeof b === 'string') return a === b
  let x = Number(a)
  let y = Number(b)
  if (kind === 'angle_deg') {
    x = (x * Math.PI) / 180
    y = (y * Math.PI) / 180
    kind = 'angle_rad'
  }
  return Math.abs(x - y) <= tol.atol[kind] + tol.rtol * Math.abs(y)
}

const v2 = new Float64Array(2)
const v3 = new Float64Array(3)
const derivedOf = (s: number[]): EnvDerived => derive(s, newDerived())
const dict = (d: EnvDerived): Record<string, number> => Object.fromEntries(DERIVED_KEYS.map((k) => [k, d[k]]))
const tr = (kf: Record<string, any>) => makeTransition(kf.mode, kf.t0_ns, kf.t1_ns, kf.from, kf.to, kf.via) // eslint-disable-line @typescript-eslint/no-explicit-any
const ev = (a: number[]) => ({ kind: a[0], id: a[1], tCreateNs: a[2], x0: a[3], s0: a[4], amp: a[5], lam: a[6], dirFromDeg: a[7], sSpan: a[8] })
const anchJson = (A: Anchors) => ({ t_ns: A.tNs, s_m: A.sM, d_enu_m: [A.d[0], A.d[1], A.d[2]], fall_rain_m: A.fallRain, fall_snow_m: A.fallSnow, wetness: A.wetness, puddle: A.puddle })
const kV2 = PRESETS_MODEL.c.k_v2

/* eslint-disable @typescript-eslint/no-explicit-any */
const DISPATCH: Record<string, (a: any) => Record<string, unknown>> = {
  from_to_uv: (a) => (fromToUv(a.speed_mps, a.dir_from_deg, v2), { u_mps: v2[0], v_mps: v2[1] }),
  uv_to_from: (a) => (uvToFrom(a.u_mps, a.v_mps, v3), { speed_mps: v3[0], dir_from_deg: v3[1], calm: v3[2] === 1 }),
  e_n: (a) => {
    const e = Array.from(eDir(a.dir_from_deg, v2))
    return { e, n: Array.from(nDir(a.dir_from_deg, v2)) }
  },
  shortest_arc: (a) => ({ d_deg: shortestArc(a.a_deg, a.b_deg) }),
  enu_to_three: (a) => ({ v: Array.from(enuToThree(a.v[0], a.v[1], a.v[2], v3)) }),
  three_to_enu: (a) => ({ v: Array.from(threeToEnu(a.v[0], a.v[1], a.v[2], v3)) }),
  enu_to_ned: (a) => ({ v: Array.from(enuToNed(a.v[0], a.v[1], a.v[2], v3)) }),
  profile: (a) => ({ f: profile(a.z_agl_m, a.kind, a.z_ref, a.z0, a.d, a.alpha) }),
  derive: (a) => dict(derivedOf(a.s)),
  mor_bg_from_total: () => ({}),
  eval_env: (a) => ({ s: Array.from(evalEnv(tr(a.kf), a.t_ns, new Float64Array(NF))) }),
  optical_depth: (a) => ({ od: opticalDepth(a.z0_agl_m, a.rd_z, a.len_m, derivedOf(a.s), a.fog_top_m, a.cloud_base_m) }),
  sigma_at: (a) => (sigmaAt(a.z_agl_m, derivedOf(a.s), a.fog_top_m, a.cloud_base_m, v2), { sigma_per_m: v2[0], flags: v2[1] }),
  sigma_lambda: (a) => ({ sigma_per_m: sigmaLambda(derivedOf(a.s), a.lam_nm, kV2), t2_100m: lidarTwoWay(derivedOf(a.s), a.lam_nm, 100.0, kV2) }),
  kim_q: (a) => ({ q: kimQ(a.v2_km) }),
  f_adv: (a) => ({ f_adv: profileCfg(a.profile.adv_height_m, a.profile) }),
  gust_create: (a) => {
    const g = gustCreate(a.id, a.t_ns, a.amp_mps, a.d_m, a.dir_from_deg, a.S_t_m, a.speed_ref_mps, a.bounds_min, a.bounds_max, a.f_adv)
    return { ev: [g.kind, g.id, g.tCreateNs, g.x0, g.s0, g.amp, g.lam, g.dirFromDeg, g.sSpan] }
  },
  gust: (a) => ({ g_mps: gust(a.p_xy_m[0], a.p_xy_m[1], a.S_t_m, ev(a.ev), a.f_adv), expired: gustExpired(a.S_t_m, ev(a.ev), a.f_adv) }),
  sector_slots: (a) => {
    const sl = sectorSlots(a.theta_from_deg, a.sectors_deg, a.antisymmetric)
    return { file_index: sl.map((x) => x[0]), weight: sl.map((x) => x[1]), sign: sl.map((x) => x[2]), a_deg: sl.map((x) => x[3]) }
  },
  isa: (a) => (isa(a.anchor_h_msl_m + a.z_m, a.isa_dt_c, v3), { temperature_c: v3[0], pressure_pa: v3[1], rho_kgm3: v3[2] }),
  advance: (a) => {
    const kf = tr(a.kf)
    const A = anchorsInitial(kf, 0, newAnchors())
    const outs = []
    let prev = 0
    for (const k of a.checkpoints_k as number[]) {
      advance(A, kf, prev, k)
      prev = k
      outs.push(anchJson(A))
    }
    return { anchors: outs }
  },
}
/* eslint-enable @typescript-eslint/no-explicit-any */

const GROUPS = ['conventions', 'profile', 'derive', 'eval_env', 'optical_depth', 'gust', 'sector_slots', 'isa', 'anchors']

describe('environment golden parity (M07-AC-002)', () => {
  for (const g of GROUPS) {
    it(g, () => {
      const d = gold(g)
      expect(d.presets_sha256).toBe(PRESETS_SHA256)
      const bad: string[] = []
      for (const c of d.cases) {
        if (c.fn === 'mor_bg_from_total') continue // preset authoring conversion, Python only
        const got = DISPATCH[c.fn](c.args)
        for (const [k, kind] of Object.entries(c.kinds)) if (!close(got[k], c.out[k], kind, d.tolerance)) bad.push(`${c.fn}.${k} ${JSON.stringify(c.args).slice(0, 80)}`)
      }
      expect(bad.slice(0, 5)).toEqual([])
    })
  }

  it('turbulence box sampling (turb_small.awrv, f16 asset shared with Python)', () => {
    const d = gold('turb')
    const buf = readFileSync(new URL(`packages/contracts/env/golden/${d.asset}`, ROOT))
    const vol = decodeAWRV(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength))
    const box = new TurbBoxCPU(vol)
    const out = new Float64Array(3)
    const su = new Float64Array(2)
    const bad: string[] = []
    for (const c of d.cases) {
      if (c.fn === 'turb_sample') {
        box.sample(c.args.q_m[0], c.args.q_m[1], c.args.q_m[2], out)
        if (!close(Array.from(out), c.out.b, 'dimensionless', d.tolerance)) bad.push(JSON.stringify(c.args))
      } else if (c.fn === 'mil_sigma') {
        milSigma(c.args.z_agl_m, c.args.sigma_ref_mps, su)
        if (!close(su[0], c.out.sigma_u_mps, 'velocity_mps', d.tolerance) || !close(su[1], c.out.sigma_w_mps, 'velocity_mps', d.tolerance)) bad.push(JSON.stringify(c.args))
      }
    }
    expect(bad.slice(0, 5)).toEqual([])
  })
})
