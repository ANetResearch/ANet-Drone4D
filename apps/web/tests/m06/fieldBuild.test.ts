// D2.1 WP-11b (AWR-04 §15.11.2-§15.11.5; ADR-125; D2-AC-53 Node part): the field view's instance data.
//   - radiation field: values are the interpolated profile at each pattern point (the server's samples within 0.5 dB or 2 %),
//     bands follow the frame's edges, grey, opacity and size rise with the band, acoustic values never rise with distance;
//     points inside the solid shell are drawn in the top band, 1 px larger;
//   - volumes: only points inside the configured body, at the server's positions; P equals the server's table model
//     (physical) or 1 (geometric), opacity 0.05 + 0.8 P is monotone in P; the physical reference switches geometric bodies to P;
//   - budgets per column (AWR-04 §15.11.5), proportional reduction over the total, circle caps; governor levels;
//   - encoding without hue (point codes over the five field greys; modality by shape).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  allocatePoints, buildField, FIELD, FIELD_BUDGETS, FIELD_LEVEL, FIELD_SHAPE, FieldBuffers, fieldBudget, fieldRamp, MODAL_RAMP, type FieldBuildInput,
  type TargetLookup,
} from '@/engine/field'
import { decodeFieldProfile, decodeFieldVolumes, profileBand, profileValue, type ProfileFrame } from '@/engine/perception'
import type { SensitivityBody } from '@/engine/targets'
import { SCENE } from '@/lib/tokens/scene.gen'

interface VolSample { target: string; body: number; model: number; pos: [number, number, number]; yaw: number; cfg: SensitivityBody; points: [number, number, number, number, boolean][] }
const FIX = JSON.parse(readFileSync(join(import.meta.dirname, '../m19/field_cases.json'), 'utf8')) as {
  profiles: { kind: string; model: string; frame: unknown; samples: [number, number, number | null][] }[]
  volumes: { frame: unknown; samples: VolSample[] }
}

const profile = (kind: string, model = 'p600_mid360'): ProfileFrame => decodeFieldProfile(FIX.profiles.find((p) => p.kind === kind && p.model === model)!.frame)!

/** n vehicles uav-01.. on a line (ids beyond the frame's two reuse the first entry's arrays) */
function withVehicles(f: ProfileFrame, n: number): ProfileFrame {
  const uavs = Array.from({ length: n }, (_, i) => ({ ...f.uavs[0], id: `uav-${String(i + 1).padStart(2, '0')}` }))
  return { ...f, uavs }
}

function input(o: Partial<FieldBuildInput> & { n?: number }): FieldBuildInput {
  const n = o.n ?? 2
  return {
    kind: 'acoustic', profile: null, volumes: null, volumesMode: 'off', physicalRef: false, selectedTarget: null, highlightUav: null, volumeModel: null,
    budget: FIELD_BUDGETS.desktop, level: 0, camera: null, targets: null,
    uavs: {
      agentNo: (id) => (/^uav-(\d+)$/.test(id) ? Number(id.slice(4)) : -1),
      pose: (a, out) => {
        if (a < 1 || a > n) return false
        out[0] = 200 * a
        out[1] = 0
        out[2] = 50
        return true
      },
    },
    ...o,
  }
}

function targetsOf(samples: VolSample[]): TargetLookup {
  const ids = [...new Set(samples.map((s) => s.target))]
  return {
    rowOf: (id) => ids.indexOf(id),
    keyOf: (row) => row,
    pos: (row, out) => {
      const s = samples.find((x) => x.target === ids[row])!
      out[0] = s.pos[0]
      out[1] = s.pos[1]
      out[2] = s.pos[2]
    },
    yaw: (row) => samples.find((x) => x.target === ids[row])!.yaw,
    body: (id, _row, idx) => samples.find((x) => x.target === id && x.body === idx)?.cfg ?? null,
  }
}

const decode = (code: number): { ramp: number; shape: number } => ({ shape: Math.floor(code / 5 + 0.01), ramp: code - 5 * Math.floor(code / 5 + 0.01) })

describe('radiation field points', () => {
  it('values, bands, encoding and the solid shell (acoustic)', () => {
    const f = profile('acoustic')
    const buf = new FieldBuffers(8192, 96)
    const st = buildField(input({ profile: f }), buf)
    expect(st.uavAnchors).toBe(2)
    expect(st.points).toBeGreaterThan(400)
    expect(st.points).toBeLessThanOrEqual(1024)
    const shell = f.uavs[0].shellM
    let inShell = 0
    for (let i = 0; i < buf.n; i++) {
      const x = buf.off[3 * i], y = buf.off[3 * i + 1], z = buf.off[3 * i + 2]
      const r = Math.hypot(x, y, z)
      const e = -Math.asin(z / r) * 180 / Math.PI
      const band = profileBand(f, profileValue(f, f.uavs[0], r, e))
      expect(band).toBeGreaterThan(0)
      const { ramp, shape } = decode(buf.info[4 * i + 1])
      expect(shape).toBe(FIELD_SHAPE.acoustic)
      if (r <= shell * (1 - 1e-6)) {
        expect(ramp).toBe(FIELD.ramp.top)
        expect(buf.info[4 * i + 3]).toBe(FIELD_BUDGETS.desktop.sizePx[1] + 1)
        inShell++
      } else if (r > shell * (1 + 1e-6)) {
        expect(ramp).toBe(band - 1)
        expect(buf.info[4 * i + 2]).toBeCloseTo(0.15 + ((band - 1) / 4) * 0.7, 5)
      }
    }
    expect(inShell).toBeGreaterThan(0)
    expect(st.inShell).toBe(inShell)
  })

  it('every kind: the built values equal the server samples within 0.5 dB or 2 % (pattern j = 1, no shell highlight)', () => {
    for (const c of FIX.profiles) {
      const f = decodeFieldProfile(c.frame)!
      // the fixture's vehicle 2 has pattern index 1: build only it, whole budget
      const one = { ...f, uavs: [f.uavs[0], f.uavs[1]] }
      const buf = new FieldBuffers(8192, 96)
      buildField(input({ kind: f.kind, profile: one, budget: { ...FIELD_BUDGETS.desktop, perUav: 256 }, highlightUav: null }), buf)
      let drawn = 0
      for (let i = 0; i < buf.n; i++) if (buf.info[4 * i] === 1) drawn++ // slot 1 = vehicle j 1
      let n = 0
      let want1 = 0
      for (const [r, e, want] of c.samples) {
        if (want === null) continue
        const band = profileBand(f, want)
        if (band === 0) continue
        want1++
        const got = profileValue(f, f.uavs[1], r, e)
        if (Number.isNaN(got)) continue
        n++
        if (f.combine === 'add') expect(Math.abs(got - want)).toBeLessThanOrEqual(0.5)
        else expect(Math.abs(got - want) / Math.abs(want)).toBeLessThanOrEqual(0.02)
      }
      expect(n).toBeGreaterThan(50)
      // the same pattern (j = 1, r_max at the draw floor) draws about as many points as the server's samples above the floor
      if (c.kind !== 'thermal') expect(Math.abs(drawn - want1), `${c.kind} ${c.model}`).toBeLessThanOrEqual(Math.max(6, 0.05 * want1))
    }
  })

  it('acoustic values never rise with distance along a direction', () => {
    const f = profile('acoustic')
    for (let e = -75; e <= 75; e += 30) {
      let last = Number.POSITIVE_INFINITY
      for (let k = 0; k < 200; k++) {
        const r = 2 * 1.035 ** k
        const v = profileValue(f, f.uavs[0], r, e)
        if (Number.isNaN(v)) break
        expect(v).toBeLessThanOrEqual(last + 1e-9)
        last = v
      }
    }
  })

  it('budgets per column, proportional reduction, governor half and off', () => {
    const f = withVehicles(profile('acoustic'), 24)
    const buf = new FieldBuffers(8192, 96)
    for (const [col, b] of Object.entries(FIELD_BUDGETS)) {
      const st = buildField(input({ profile: f, n: 24, budget: b }), buf)
      expect(st.uavAnchors, col).toBe(24)
      expect(st.points, col).toBeLessThanOrEqual(b.totalPoints)
      // every vehicle keeps a share
      for (const a of buf.anchors) expect(a.points, col).toBeGreaterThan(0)
      expect(st.circles, col).toBeLessThanOrEqual(b.circles)
    }
    const full = buildField(input({ profile: f, n: 24, budget: FIELD_BUDGETS.desktopS }), buf).points
    const half = buildField(input({ profile: f, n: 24, budget: FIELD_BUDGETS.desktopS, level: FIELD_LEVEL.halfPoints }), buf).points
    expect(half).toBeLessThanOrEqual(Math.ceil(full / 2) + 24)
    expect(buildField(input({ profile: f, n: 24, level: FIELD_LEVEL.off }), buf).points).toBe(0)
    expect(fieldBudget('phone', 'B')).toBe(FIELD_BUDGETS.phone)
    expect(fieldBudget('tablet', 'S')).toBe(FIELD_BUDGETS.tablet)
    expect(fieldBudget('desktop', 'S')).toBe(FIELD_BUDGETS.desktopS)
    expect(fieldBudget('desktop', 'A')).toBe(FIELD_BUDGETS.desktop)
  })

  it('allocatePoints scales in proportion and never exceeds the total', () => {
    const out = new Array<number>(4)
    expect(allocatePoints([512, 512, 384, 384], 8192, out)).toBe(1792)
    expect(out).toEqual([512, 512, 384, 384])
    const n = allocatePoints([512, 512, 384, 384], 1000, out)
    expect(n).toBeLessThanOrEqual(1000)
    expect(out[0]).toBe(out[1])
    expect(out[0] / out[2]).toBeCloseTo(512 / 384, 1)
  })

  it('shells: solid and dashed great circles within the circle cap; level 3 drops the dashed ones; highlight in the top grey', () => {
    const f = withVehicles(profile('acoustic'), 12)
    const buf = new FieldBuffers(8192, 96)
    const st = buildField(input({ profile: f, n: 12, highlightUav: 'uav-03' }), buf)
    expect(st.circles).toBeGreaterThan(12)
    expect(st.circles).toBeLessThanOrEqual(96)
    let dashed = 0
    let hl = 0
    for (let v = 0; v < buf.nVerts; v++) {
      if (buf.sInfo[4 * v + 2] >= 0) dashed++
      if (buf.sInfo[4 * v + 1] === FIELD.ramp.highlight) hl++
      const slot = buf.sInfo[4 * v]
      expect(slot).toBeGreaterThanOrEqual(0)
      expect(slot).toBeLessThan(12)
    }
    expect(dashed).toBeGreaterThan(0)
    expect(hl).toBeGreaterThan(0)
    const s3 = buildField(input({ profile: f, n: 12, level: FIELD_LEVEL.noDashed }), buf)
    for (let v = 0; v < buf.nVerts; v++) expect(buf.sInfo[4 * v + 2]).toBe(-1)
    expect(s3.circles).toBeGreaterThan(0)
    const tierS = buildField(input({ profile: f, n: 12, budget: FIELD_BUDGETS.desktopS }), buf)
    expect(tierS.circles).toBeLessThanOrEqual(32)
    // the solid shell's radius is the frame's default shell
    const r0 = Math.hypot(buf.sPos[0], buf.sPos[1], buf.sPos[2])
    expect(r0).toBeCloseTo(f.uavs[0].shellM, 3)
  })
})

describe('perception volumes', () => {
  const vf = decodeFieldVolumes(FIX.volumes.frame)!
  const samples = FIX.volumes.samples
  const targets = targetsOf(samples)

  it('points inside the configured body only, at the server positions, P of the table model (physical)', () => {
    const buf = new FieldBuffers(8192, 96)
    // the server's samples are 256 points of model index 2 (p600_mid360)
    const st = buildField(input({ kind: 'none', volumes: vf, volumesMode: 'all', volumeModel: 'p600_mid360', targets, budget: { ...FIELD_BUDGETS.desktop, perBody: 256 } }), buf)
    expect(st.modelIdx).toBe(2)
    expect(st.bodyAnchors).toBe(samples.filter((s) => s.cfg.enabled !== false).length)
    let checked = 0
    for (const s of samples) {
      const slot = buf.anchors.findIndex((a) => a.kind === 'body' && a.id === s.target && a.bodyIdx === s.body)
      if (slot < 0) continue
      const kept = s.points.filter((p) => p[4])
      const mine: number[] = []
      for (let i = 0; i < buf.n; i++) if (buf.info[4 * i] === slot) mine.push(i)
      expect(mine.length, `${s.target}/${s.body}`).toBe(kept.length)
      const physical = (vf.targets.get(s.target)!.bodies.find((b) => b.idx === s.body)!.mode) === 'physical'
      mine.forEach((i, k) => {
        for (let q = 0; q < 3; q++) expect(s.pos[q] + buf.off[3 * i + q]).toBeCloseTo(kept[k][q] as number, 3)
        const P = (buf.info[4 * i + 2] - 0.05) / 0.8
        expect(P).toBeCloseTo(physical ? kept[k][3] : 1, 3)
        const { ramp, shape } = decode(buf.info[4 * i + 1])
        const kind = vf.targets.get(s.target)!.bodies.find((b) => b.idx === s.body)!.kind as 'visual' | 'acoustic'
        expect(ramp).toBe(MODAL_RAMP[kind])
        expect(shape).toBe(FIELD_SHAPE[kind])
        checked++
      })
    }
    expect(checked).toBeGreaterThan(50)
  })

  it('geometric bodies: P = 1 in the body; the physical reference shows the table P; opacity monotone in P', () => {
    const geo = decodeFieldVolumes(JSON.parse(JSON.stringify(FIX.volumes.frame).replaceAll('"physical"', '"geometric"')))!
    const buf = new FieldBuffers(8192, 96)
    buildField(input({ kind: 'none', volumes: geo, volumesMode: 'all', volumeModel: 'p600_mid360', targets }), buf)
    for (let i = 0; i < buf.n; i++) expect(buf.info[4 * i + 2]).toBeCloseTo(0.85, 6)
    const st = buildField(input({ kind: 'none', volumes: geo, volumesMode: 'all', volumeModel: 'p600_mid360', targets, physicalRef: true }), buf)
    expect(st.pMin).toBeLessThan(st.pMax)
    const pairs: [number, number][] = []
    for (let i = 0; i < buf.n; i++) pairs.push([buf.info[4 * i + 2], (buf.info[4 * i + 2] - 0.05) / 0.8])
    pairs.sort((a, b) => a[1] - b[1])
    for (let k = 1; k < pairs.length; k++) expect(pairs[k][0]).toBeGreaterThanOrEqual(pairs[k - 1][0])
  })

  it('selected mode and governor level 2 keep the selected target only; the body cap applies', () => {
    const buf = new FieldBuffers(8192, 96)
    const sel = buildField(input({ kind: 'none', volumes: vf, volumesMode: 'selected', selectedTarget: 'dog-01', targets }), buf)
    expect(buf.anchors.every((a) => a.id === 'dog-01')).toBe(true)
    expect(sel.bodyAnchors).toBeGreaterThan(0)
    buildField(input({ kind: 'none', volumes: vf, volumesMode: 'all', selectedTarget: 'person-01', targets, level: FIELD_LEVEL.selectedOnly }), buf)
    expect(buf.anchors.every((a) => a.id === 'person-01')).toBe(true)
    const capped = buildField(input({ kind: 'none', volumes: vf, volumesMode: 'all', targets, budget: { ...FIELD_BUDGETS.phone, bodies: 1 } }), buf)
    expect(capped.bodyAnchors).toBe(1)
  })

  it('a selected target sets the vehicles\' solid shell to its effective radius for their model', () => {
    const f = profile('acoustic')
    const buf = new FieldBuffers(8192, 96)
    buildField(input({ profile: f, volumes: vf, volumesMode: 'off', selectedTarget: 'person-01', targets }), buf)
    const r0 = Math.hypot(buf.sPos[0], buf.sPos[1], buf.sPos[2])
    expect(Math.abs(r0 - f.uavs[0].shellM)).toBeGreaterThan(0.5)
    expect(r0).toBeGreaterThan(5)
  })
})

describe('no hue (R2)', () => {
  it('the five field greys are the neutral scene greys (never r500), modality by shape', () => {
    const ramp = fieldRamp()
    expect(ramp).toBe(SCENE.fieldRamp) // the --field-ramp-1..5 scene token (d2-WP-12c-to-M06 item 3)
    expect(ramp.length).toBe(5)
    const greys = SCENE.glyphPalette.filter((_, i) => i !== 1)
    for (const c of ramp) expect(greys.some((g) => g.every((x, k) => Math.abs(x - c[k]) < 1e-9)), JSON.stringify(c)).toBe(true)
    for (let i = 1; i < 5; i++) expect(ramp[i][1]).toBeGreaterThan(ramp[i - 1][1])
    expect(new Set(Object.values(FIELD_SHAPE)).size).toBe(4)
  })
})
