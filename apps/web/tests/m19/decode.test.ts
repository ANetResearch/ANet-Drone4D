// M19-AC-014, AC-019 (Node parts): the perception parameters equal the contract (drift check), a `uav/{id}/perception`
// payload decodes (object and raw msgpack), strings are sanitised, the HUD line is "n_eff / n_req px" with the limiting
// factor of the nearest target in the frustum, and captured targets give the FPV boxes.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { decode, encode } from '@msgpack/msgpack'
import { describe, expect, it } from 'vitest'
import { capturedTargets, decodePerception, hudLine, LIMITING, LIMITING_ZH, PERCEPTION_LEVELS } from '@/engine/perception'

const ROOT = join(import.meta.dirname, '../../../..')

const FRAME = {
  uav: 'uav-01', t_sim_ns: 4_200_000_000,
  sensors: [{ sensor: 'eo', kind: 'eo', mode: 'day', f_mm: 48, hfov_rad: 0.1159, vfov_rad: 0.0652, n_in_fov: 2, targets: [
    { target: 'person-01', cls_est: 'unknown', level: 'D', p: 0.31, n: 16.2, n_eff: 9.6, n_req: 14, level_req: 'R', range_m: 612.4, gsd_m: 0.037,
      limiting: 'contrast', k: { los: 1, c: 0.59, light: 1, blur: 0.99, occ: 0 }, hold_s: 0, captured: false, capture_state: 'IDLE' },
    { target: 'dog-01', cls_est: 'dog', level: 'R', n_eff: 15, n_req: 14, range_m: 700, gsd_m: 0.04, limiting: null, captured: true, capture_state: 'CAPTURED',
      meas_enu_m: [10, 20, 0], sigma_m: 0.7 },
  ] }],
  hud: { sensor: 'eo', target: 'person-01', n_eff: 9.6, n_req: 14, limiting: 'contrast' },
  budget: { rays_tick_max: 3, rays_s: 21, dropped: 0 },
}

describe('perception parameters (contract drift)', () => {
  it('equal packages/contracts/sensor/perception_levels.json', () => {
    const c = JSON.parse(readFileSync(join(ROOT, 'packages/contracts/sensor/perception_levels.json'), 'utf8'))
    expect(JSON.parse(JSON.stringify(PERCEPTION_LEVELS))).toEqual(c)
  })
  it('limiting factors follow rt/enums PerceptionLimiting', () => {
    const e = JSON.parse(readFileSync(join(ROOT, 'packages/contracts/rt/enums.json'), 'utf8'))
    expect(e.PerceptionLimiting).toEqual([...LIMITING])
    for (const l of LIMITING) expect(LIMITING_ZH[l].length).toBeGreaterThan(0)
  })
})

describe('decodePerception and the HUD (M19-AC-019)', () => {
  it('decodes the object and the raw msgpack the same way', () => {
    const a = decodePerception(FRAME)
    const b = decodePerception(encode(FRAME), (x) => decode(x))
    expect(decodePerception(encode(FRAME))).toBeNull()
    expect(a).toEqual(b)
    expect(a?.sensors[0].targets.length).toBe(2)
    expect(a?.sensors[0].targets[1].meas_enu_m).toEqual([10, 20, 0])
  })
  it('rejects wrong shapes and drops unsafe strings', () => {
    expect(decodePerception({ foo: 1 })).toBeNull()
    expect(decodePerception(new Uint8Array([0xc1]), (x) => decode(x))).toBeNull()
    const f = decodePerception({ ...FRAME, sensors: [{ ...FRAME.sensors[0], targets: [{ ...FRAME.sensors[0].targets[0], target: 'x<script>' }] }] })
    expect(f?.sensors[0].targets.length).toBe(0)
  })
  it('HUD line: 9.6 / 14 px, limited by contrast; hidden without targets', () => {
    const h = hudLine(decodePerception(FRAME))
    expect(h?.text).toBe('9.6 / 14 px')
    expect(h?.limiting).toBe('contrast')
    expect(h?.progress).toBeCloseTo(9.6 / 14, 6)
    expect(hudLine(decodePerception({ ...FRAME, hud: null, sensors: [{ ...FRAME.sensors[0], targets: [] }] }))).toBeNull()
  })
  it('captured targets give the FPV boxes', () => {
    expect(capturedTargets(decodePerception(FRAME))).toEqual([{ sensor: 'eo', target: 'dog-01' }])
  })
})
