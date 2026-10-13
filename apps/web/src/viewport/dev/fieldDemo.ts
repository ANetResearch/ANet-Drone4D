// Synthetic field-view data for the test build (D2.1 WP-11b; AWR-04 §15.11.4 wire forms). Owner: M06. Loaded lazily by the
// test hooks (window.__vp.field) in dev and VITE_AWR_TEST_SWITCHES builds only: FakeSource has no perception server, so the
// Playwright specs of the field view feed the perception store the payloads the server would send (awr.field.profile.v1
// keyframes per kind for the roster's vehicles, awr.field.volumes.v1 for the injected targets) and the target rows
// (TargetLite32). The physics is the textbook form of AWR-04 §15.10-§15.11 for the P600 (L_WA 88 dB(A), side area 0.19 m2,
// hot spot 0.169 m, RCS 0.05 m2), close enough to the server's numbers for rendering tests; numbers are not asserted against it.
import { TARGET_LITE32 as L } from '@awr/contracts/layouts'
import { EL_DEG, N_R, type FieldKind } from '@/engine/perception'
import { targetsModel } from '@/engine/targets'
import { perceptionFeedIngest } from '@/stores/perception'

const ARCMIN = (180 * 60) / Math.PI
const NONE = 0xffff
/** per kind: unit, combine, grid end (ladder value), band edges, iso shells, shell reference, value encoding and offset */
const KINDS: Record<FieldKind, { unit: string; combine: 'add' | 'mul'; R: number; bands: number[]; iso: { id: string; v: number }[]; shellRef: string;
  enc: 'lin' | 'log'; off: number; value(r: number): number; shell: number }> = {
  acoustic: { unit: 'dba', combine: 'add', R: 707.1, bands: [20, 30, 40, 50, 60], iso: [{ id: 'bg', v: 40 }], shellRef: 'person', enc: 'lin', off: 0,
    value: (r) => 88 - 20 * Math.log10(r) - 11 - (5 * r) / 1000, shell: 158 },
  visual: { unit: 'arcmin', combine: 'mul', R: 2000, bands: [0.5, 1, 2, 5, 10], iso: [{ id: 'ref', v: 1 }], shellRef: 'person', enc: 'log', off: 0,
    value: (r) => (0.436 / r) * ARCMIN, shell: 1499 },
  thermal: { unit: 'px', combine: 'mul', R: 707.1, bands: [0, 1, 2, 3.5, 14], iso: [{ id: 'r', v: 14 }], shellRef: 'D', enc: 'log', off: 0,
    value: (r) => 0.169 / (r * 0.00048), shell: 100.6 },
  radar: { unit: 'db', combine: 'add', R: 353.6, bands: [-10, 0, 6, 13.2, 20], iso: [], shellRef: 'D', enc: 'lin', off: -10,
    value: (r) => 13.2 + 10 * Math.log10(0.05) - 40 * Math.log10(r / 180), shell: 85.4 },
}

/** an awr.field.profile.v1 keyframe for the vehicles (ids and models) */
export function syntheticProfile(kind: FieldKind, uavs: readonly { id: string; model: string }[], tSimNs = 0): Record<string, unknown> {
  const k = KINDS[kind]
  const rM = Array.from({ length: N_R }, (_, i) => 2 * (k.R / 2) ** (i / (N_R - 1)))
  const vR = rM.map((r) => {
    const v = k.value(r)
    if (!(v >= k.bands[0])) return NONE
    return k.enc === 'log' ? Math.round((Math.log10(v) + 8) * 4000) : Math.round((v - k.off) * 10)
  })
  return {
    t_sim_ns: tSimNs, kind, unit: k.unit, combine: k.combine, keyframe: true, r_m: rM, el_deg: [...EL_DEG], bands: k.bands, v_enc: k.enc, v_off: k.off,
    iso: k.iso, shell_ref: k.shellRef, ref: kind === 'thermal' ? { sensor: 'lwir640', lens_mm: 25 } : kind === 'radar' ? { sensor: 'mmw77' } : {},
    uav: uavs.map((u, i) => (i === 0
      ? { id: u.id, model_id: u.model, rev: 0, v_r: vR, f_el: [k.combine === 'add' ? 0 : 100], cut_el_m: [0], shell_m: k.shell }
      : { id: u.id, model_id: u.model, rev: 0, same: 0 })),
  }
}

/** an awr.field.volumes.v1 keyframe: per target a physical visual sector (300 m by elevation) and a geometric acoustic hemisphere */
export function syntheticVolumes(models: readonly string[], targets: readonly string[]): Record<string, unknown> {
  const per = <T>(v: T): T[] => models.map(() => v)
  return {
    t_sim_ns: 0, keyframe: true, models: [...models], az_deg: [0, 45, 90, 135, 180, 225, 270, 315], el_deg: [...EL_DEG],
    tgt: targets.map((id) => ({
      id, rev: 0,
      bodies: [
        { idx: 0, kind: 'visual', shape: 'sector', mode: 'physical', r_eff_dm: per([2400, 2400, 2400, 2400, 1800, 1800]), w_m: per(60) },
        { idx: 1, kind: 'acoustic', shape: 'hemisphere', mode: 'geometric', r_eff_dm: per([1580, 1400, 1100, 900, 800, 900, 1100, 1400]), w_m: per(20) },
      ],
    })),
  }
}

/** TargetLite32 rows (person class, enabled; physical when asked) */
export function targetRows(list: readonly { no: number; pos: readonly [number, number, number]; heading: number; physical?: boolean }[]): Uint8Array {
  const b = new Uint8Array(L.SIZE * list.length)
  const dv = new DataView(b.buffer)
  list.forEach((t, i) => {
    const o = i * L.SIZE
    dv.setUint16(o + L.AGENT_NO, t.no, true)
    dv.setUint8(o + L.CLASS, 1)
    dv.setUint8(o + L.STATE, 0x80 | (t.physical ? 8 : 0))
    dv.setFloat32(o + L.POS, t.pos[0], true)
    dv.setFloat32(o + L.POS + 4, t.pos[1], true)
    dv.setFloat32(o + L.POS + 8, t.pos[2], true)
    dv.setFloat32(o + L.HEADING_RAD, t.heading, true)
    dv.setUint8(o + L.ALERTNESS_U8, 128)
    dv.setUint8(o + L.N_VOL, 2)
  })
  return b
}

/** feed the perception store one profile per kind (the store keeps the subscribed kinds) */
export function injectProfiles(kinds: readonly FieldKind[], uavs: readonly { id: string; model: string }[]): void {
  for (const kind of kinds) perceptionFeedIngest.data({ topic: `field/${kind}`, data: syntheticProfile(kind, uavs) } as never)
}

/** targets near a point and their volumes; returns the target ids (the model's fallback ids target-<no>) */
export function injectTargets(near: readonly [number, number, number], n: number, models: readonly string[]): string[] {
  const list = Array.from({ length: n }, (_, i) => ({ no: 900 + i, pos: [near[0] + 60 + 25 * i, near[1] + 40 - 15 * i, near[2]] as [number, number, number], heading: 0.4 * i, physical: i % 2 === 0 }))
  targetsModel.ingestRows(targetRows(list), targetsModel.tSimMs)
  const ids = list.map((_, i) => targetsModel.idOfRow(i))
  perceptionFeedIngest.data({ topic: 'field/volumes', data: syntheticVolumes(models, ids) } as never)
  return ids
}
