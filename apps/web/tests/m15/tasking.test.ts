// WP-12b (M15-FR-214, FR-218, FR-220; UX-FR-126 to UX-FR-131, UX-FR-136, UX-FR-137, UX-FR-144; AWR-14 §20.9, §20.10.6,
// §20.13.5): the pure models of the task creation page, the task and relay tabs, the FPV HUD, the Dock tabs and the GSD
// calculator: the pre-check of §20.9.2, the SB-53 body (counter-clockwise rings, common keys only when changed), the
// required GSD of a class and level (python decompose.g_req_m: person R 2.7 cm), the preview summary ("why n"), the task
// table filters, progress, hot row and row operations, the relay station rows and countdown, the HUD frame and box
// helpers, the Dock tab swap and the calculator against the M19 engine it wraps.
import type { TaskingTaskPreview } from '@awr/contracts/types'
import { describe, expect, it } from 'vitest'
import {
  areaM2, blocking, buildSpec, ccw, defaultForm, drawKindOf, filterTasks, hotRow, itemsText, lineLengthM, minWidthM, mmss, opsFor, precheck,
  previewSummary, progressOf, reliefLeftS, requiredGsdM, stationRows, vehiclesOf, warningKey, type TaskGeom,
} from '@/ui/panels/tasks/taskModel'
import { boxState, nearestTargets, sensorFrameOf } from '@/ui/fpv/hudModel'
import { dockPanelsFor } from '@/ui/layout/dockTabs'
import { calc, posZoom, sensorOptions, windowFor, zoomPos } from '@/ui/views/gsdModel'
import { gsdM, gx40, lwir640, maxRangeForLevel, perceptionLevel, pPrimeUm, targetFromClass, defaultEnv } from '@/engine/perception'
import type { TaskRow } from '@/stores/tasking'
import type { PanelDescriptor } from '@/ui/panels/registry'

const area = (pts: [number, number][]): TaskGeom => ({ kind: 'area', points: pts.map(([x, y]) => [x, y, 0] as const), target: null })
const row = (o: Partial<TaskRow>): TaskRow => ({
  tid: 't-1', type: 'area_patrol', label: '', state: 'RUNNING', revision: 1, parent: null, children: [], progressPct: 0, coveragePct: null, nVehicles: 1,
  makespanS: null, items: [], warnings: [], params: {}, relay: null, ...o,
})

describe('task drawing and pre-check (AWR-14 §20.9.2)', () => {
  it('maps the seven types to their drawing tools', () => {
    expect(drawKindOf('point_watch')).toBe('point')
    expect(drawKindOf('polyline_patrol')).toBe('line')
    expect(drawKindOf('relay_watch')).toBe('target')
    for (const t of ['area_patrol', 'area_scan_gsd', 'perimeter_patrol', 'area_guard'] as const) expect(drawKindOf(t)).toBe('area')
  })

  it('measures rings and polylines', () => {
    const sq: [number, number][] = [[0, 0], [100, 0], [100, 50], [0, 50]]
    expect(areaM2(sq)).toBeCloseTo(5000)
    expect(minWidthM(sq)).toBeCloseTo(50)
    expect(lineLengthM([[0, 0], [3, 4], [3, 10]])).toBeCloseTo(11)
    // clockwise input comes back counter-clockwise (the contract's orientation)
    expect(ccw([[0, 0], [0, 50], [100, 50], [100, 0]])[1]).toEqual([100, 50])
  })

  it('blocks small, self-intersecting, oversized and too fine scans; passes a sane area', () => {
    const f = defaultForm('area_scan_gsd')
    expect(precheck('area_patrol', area([[0, 0], [10, 0]]), f).map((i) => i.key)).toEqual(['task.issue.minVertices'])
    expect(precheck('area_patrol', area([[0, 0], [10, 10], [10, 0], [0, 10]]), f).map((i) => i.key)).toContain('task.issue.selfIntersect')
    const big = precheck('area_patrol', area([[0, 0], [2000, 0], [2000, 1000], [0, 1000]]), f)
    expect(big.find((i) => i.key === 'task.issue.area')?.code).toBe(585)
    const fine = precheck('area_scan_gsd', area([[0, 0], [900, 0], [900, 900], [0, 900]]), { ...f, gsdCm: 0.1 })
    expect(fine.find((i) => i.key === 'task.issue.strips')?.value).toBeGreaterThan(200)
    expect(blocking(precheck('area_scan_gsd', area([[0, 0], [400, 0], [400, 300], [0, 300]]), f))).toBe(false)
    expect(precheck('relay_watch', { kind: 'target', points: [], target: null }, f).map((i) => i.key)).toEqual(['task.issue.noTarget'])
    expect(precheck('polyline_patrol', { kind: 'line', points: [[0, 0, 0]], target: null }, f).map((i) => i.key)).toEqual(['task.issue.minWaypoints'])
  })
})

describe('SB-53 body', () => {
  it('builds a counter-clockwise polygon, rounds to 0.1 m and leaves default common keys out', () => {
    const g = area([[0, 0], [0, 50.04], [100.06, 50], [100, 0]])
    const spec = buildSpec('area_guard', g, { ...defaultForm('area_guard'), label: '  东区  ' })
    expect(spec.type).toBe('area_guard')
    expect(spec.label).toBe('东区')
    expect(spec.params.polygon).toEqual([[100, 0], [100.1, 50], [0, 50], [0, 0]])
    expect(spec.params).toMatchObject({ target_class: 'person', level_req: 'R', revisit_s: 300 })
    expect(spec.common).toBeUndefined()
  })

  it('carries changed common keys and the per-type parameters', () => {
    const f = { ...defaultForm('area_patrol'), priority: 'high' as const, onDetect: 'relay' as const, allowedNests: ['nest-01'] }
    const spec = buildSpec('area_patrol', area([[0, 0], [10, 0], [10, 10]]), f)
    expect(spec.common).toEqual({ priority: 'high', on_detect: 'relay', allowed_nests: ['nest-01'] })
    expect(spec.label).toBeNull()
    const pw = buildSpec('point_watch', { kind: 'point', points: [[1.04, 2, 3]], target: null }, { ...defaultForm('point_watch'), dwellS: null, look: 'target', lookTarget: 'p-1' })
    expect(pw.params).toMatchObject({ point: [1, 2, 3], alt_m: 60, dwell_s: null, look: { mode: 'target', target: 'p-1' } })
    const rw = buildSpec('relay_watch', { kind: 'target', points: [], target: 'person-01' }, { ...defaultForm('relay_watch'), relayLevel: 'I' })
    expect(rw.params).toEqual({ target: 'person-01', duration_s: null, level_req: 'I' })
    const scan = buildSpec('area_scan_gsd', area([[0, 0], [10, 0], [10, 10]]), { ...defaultForm('area_scan_gsd'), tMaxS: 480 })
    expect(scan.params).toMatchObject({ gsd_cm: 3, sidelap: 0.1, theta_max_deg: 15, h_cap_m: 120, clearance_m: 20, allow_multi_sortie: false, t_max_s: 480 })
  })

  it('derives the required GSD like python awr.tasking.decompose.g_req_m (person R 2.7 cm)', () => {
    expect(requiredGsdM('person', 'R')).toBeCloseTo(0.027, 6)
    expect(requiredGsdM('person', 'D')).toBeGreaterThan(requiredGsdM('person', 'R'))
    expect(requiredGsdM('person', 'I')).toBeLessThan(requiredGsdM('person', 'R'))
  })
})

describe('preview card', () => {
  const p = {
    preview_id: 'pv-1', n_vehicles: 2, makespan_s: 480, t_max_s: 600, why_n: { rule: 'makespan', n_minus_1_makespan_s: 768 },
    makespan_curve: [{ n: 1, makespan_s: 768 }, { n: 2, makespan_s: 480 }, { n: 3, makespan_s: null }],
    assignments: [{ uav: 'uav0001', nest: 'nest-01', items: [{ wid: 'w1', kind: 'strip_group', ref: 's1' }, { wid: 'w2', kind: 'strip_group', ref: 's2' }] },
      { uav: 'uav0002', nest: 'nest-01', items: [{ wid: 'w3', kind: 'fill' }] }],
  } as unknown as TaskingTaskPreview

  it('summarises vehicles, completion against T_max, why n and the curve', () => {
    const s = previewSummary(p, (u) => (u === 'uav0001' ? 'p600_mid360' : 'p600_mid360'))
    expect(s).toMatchObject({ n: 2, makespanMin: 8, tMaxMin: 10, why: { rule: 'makespan', nMinus1Min: 12.8 }, models: ['p600_mid360'], nests: ['nest-01'] })
    expect(s.curve).toEqual([{ n: 1, min: 12.8 }, { n: 2, min: 8 }])
  })

  it('writes the work items of an assignment in one line', () => {
    const kind = (k: string) => (k === 'strip_group' ? 'strips' : k)
    expect(itemsText(p.assignments[0].items, kind)).toBe('strips s1, s2')
    expect(itemsText([{ wid: 'a', kind: 'fill' }, { wid: 'b', kind: 'fill' }, { wid: 'c', kind: 'fill' }, { wid: 'd', kind: 'fill' }], kind)).toBe('fill a ... d (4)')
  })
})

describe('task table and relay tab', () => {
  const rows = [row({ tid: 't-1', state: 'RUNNING' }), row({ tid: 't-2', state: 'DONE' }), row({ tid: 't-3', state: 'FAILED' }), row({ tid: 't-4', state: 'INFEASIBLE' }),
    row({ tid: 't-5', state: 'PAUSED' })]

  it('filters, finds the hot row and the operations of a state', () => {
    expect(filterTasks(rows, 'active').map((r) => r.tid)).toEqual(['t-1', 't-5'])
    expect(filterTasks(rows, 'ended').map((r) => r.tid)).toEqual(['t-2'])
    expect(filterTasks(rows, 'infeasible').map((r) => r.tid)).toEqual(['t-3', 't-4'])
    expect(hotRow(rows)).toBe('t-4')
    expect(hotRow([rows[0]])).toBeNull()
    expect(opsFor('READY')).toEqual({ start: true, pause: false, resume: false, cancel: true })
    expect(opsFor('RUNNING')).toEqual({ start: false, pause: true, resume: false, cancel: true })
    expect(opsFor('DONE').cancel).toBe(false)
  })

  it('shows the coverage of relays and scans as progress and lists the assigned vehicles', () => {
    expect(progressOf(row({ type: 'relay_watch', relay: { target: 'p', coverage: 0.9234, gaps: 1, maxGapS: 3, discovered: 0, handovers: 2, onStation: [] } }))).toBeCloseTo(92.3)
    expect(progressOf(row({ type: 'area_scan_gsd', coveragePct: 120 }))).toBe(100)
    expect(progressOf(row({ progressPct: 35 }))).toBe(35)
    expect(vehiclesOf(row({ items: [{ wid: 'a', kind: 'x', state: 'ACTIVE', uav: 'u1' }, { wid: 'b', kind: 'x', state: 'PENDING', uav: null }, { wid: 'c', kind: 'x', state: 'ACTIVE', uav: 'u1' }] })))
      .toEqual(['u1'])
  })

  it('maps warning codes to keys', () => {
    expect(warningKey('handover_slow')).toEqual({ key: 'tasking.warning.handover_slow', arg: 'handover_slow' })
    expect(warningKey('unassigned:582')).toEqual({ key: 'tasking.warning.unassigned', arg: '582' })
    expect(warningKey('bad text!').key).toBe('tasking.warning.other')
  })

  it('joins the plan stations with the vehicles on station and counts the relief down', () => {
    const st = stationRows({ on_station: [{ uav: 'uav0001', station: 'st-1', since_t_sim_ns: 3_000_000_000 }, { uav: 'uav0003', station: 'st-9' }] },
      [{ id: 'st-1', role: 'primary', pos: [0, 0, 60] }, { id: 'st-2', role: 'handover', pos: [10, 0, 60] }])
    expect(st.map((r) => [r.station, r.role, r.uav, r.sinceSimS])).toEqual([['st-1', 'primary', 'uav0001', 3], ['st-2', 'handover', null, null], ['st-9', 'primary', 'uav0003', null]])
    expect(reliefLeftS(1150, 1000, 4000)).toBe(1147)
    expect(reliefLeftS(2, 1000, 9000)).toBe(0)
    expect(reliefLeftS(null, 0, 0)).toBeNull()
    expect(mmss(1147)).toBe('19:07')
    expect(mmss(3725)).toBe('1:02:05')
    expect(mmss(Number.NaN)).toBe('-')
  })
})

describe('FPV HUD model', () => {
  const tg = (target: string, range_m: number, o: object = {}) => ({ target, level: 'D' as const, n_eff: 5, n_req: 14, range_m, gsd_m: 0.04, limiting: null, captured: false, ...o })
  const frame = [{ sensor: 'eo_gx40', kind: 'eo' as const, targets: [tg('b', 300), tg('a', 120)] }, { sensor: 'lwir', kind: 'lwir' as const, targets: [] }]

  it('takes the frame of the viewed sensor, else the HUD sensor, else the first with targets', () => {
    expect(sensorFrameOf(frame, 'lwir', null)?.sensor).toBe('lwir')
    expect(sensorFrameOf(frame, null, 'eo_gx40')?.sensor).toBe('eo_gx40')
    expect(sensorFrameOf(frame, 'nope', null)?.sensor).toBe('eo_gx40')
  })

  it('orders boxes by range and derives the box state', () => {
    expect(nearestTargets(frame[0]).map((x) => x.target)).toEqual(['a', 'b'])
    expect(nearestTargets({ ...frame[0], targets: Array.from({ length: 12 }, (_, i) => tg(`t${i}`, 100 - i)) })).toHaveLength(8)
    expect(boxState({ captured: true })).toBe('captured')
    expect(boxState({ captured: false, capture_state: 'HOLDING' })).toBe('captured')
    expect(boxState({ captured: false, capture_state: 'ARMING' })).toBe('arming')
    expect(boxState({ captured: false })).toBe('idle')
  })
})

describe('Dock tabs', () => {
  const p = (id: string) => ({ id } as PanelDescriptor)
  const all = [p('events'), p('mission'), p('tasks'), p('relay'), p('timeline')]
  it('swaps the D1 mission tab for the task and relay tabs on D2 surfaces and tasking runs', () => {
    expect(dockPanelsFor(all, 'sandbox', []).map((x) => x.id)).toEqual(['events', 'tasks', 'relay', 'timeline'])
    expect(dockPanelsFor(all, 'shared', []).map((x) => x.id)).toEqual(['events', 'tasks', 'relay', 'timeline'])
    expect(dockPanelsFor(all, 'local', ['tasking']).map((x) => x.id)).toEqual(['events', 'tasks', 'relay', 'timeline'])
    expect(dockPanelsFor(all, 'local', []).map((x) => x.id)).toEqual(['events', 'mission', 'timeline'])
  })
})

describe('GSD calculator (wraps the M19 engine)', () => {
  it('offers the reference sensors and the catalogue imaging sensors', () => {
    const opts = sensorOptions([{ sensor_type: 'radar_x', name_zh: 'R', kind: 'radar', spec: {} }, { sensor_type: 'lwir_x', name_zh: 'L', kind: 'lwir', spec: { lenses: [{ f_mm: 25, f_number: 1 }] } }])
    expect(opts.map((o) => o.id)).toEqual(['ref:gx40', 'ref:gx40-nir', 'ref:gx40-nir-ext', 'ref:lwir640-25', 'ref:lwir640-100', 'cat:lwir_x'])
    expect(opts.find((o) => o.id === 'cat:lwir_x')?.zoom).toBe(false)
  })

  it('maps focal lengths to the log slider and back', () => {
    expect(zoomPos(4.8, 4.8, 48)).toBe(0)
    expect(zoomPos(48, 4.8, 48)).toBe(1000)
    expect(posZoom(zoomPos(15, 4.8, 48), 4.8, 48)).toBeCloseTo(15, 0)
    expect(posZoom(500, 25, 25)).toBe(25)
  })

  it('returns the engine values for GSD, pixels and the per-level ranges', () => {
    const sensor = gx40('1080p')
    const out = calc({ sensor, fMm: 48, resolution: '1080p', cls: 'person', aspect: 'level', level: 'R', rangeM: 600, light: 'day', morM: 10000 })
    const pp = pPrimeUm(sensor, 48, '1080p')
    expect(out.gsdCm).toBeCloseTo(gsdM(600, pp, 48) * 100, 9)
    const T = { ...targetFromClass('person'), moving: false, speedMaxMps: 0 }
    const ref = perceptionLevel({ sensor, fMm: 48, resolution: '1080p', target: T, env: defaultEnv('day', 10000), geom: { rangeM: 600, epsRad: 0, betaRad: 0, worst: false, vRelPerpMps: 0 } })
    expect(out.n).toBeCloseTo(ref.n, 9)
    expect(out.nEff).toBeCloseTo(ref.nEff, 9)
    expect(out.dcM).toBeGreaterThan(0.3)
    expect(out.rMax.R).toBeCloseTo(maxRangeForLevel(sensor, T, defaultEnv('day', 10000), 'R', { fMm: 48, resolution: '1080p' }), 6)
    expect(out.rMax.D).toBeGreaterThan(out.rMax.R)
    expect(out.rMax.R).toBeGreaterThan(out.rMax.I)
    expect(out.p.D).toBeGreaterThanOrEqual(out.p.R)
    expect(out.p.R).toBeGreaterThanOrEqual(out.p.I)
  })

  it('gives a station window or the reason it has none', () => {
    const w = windowFor({ sensor: gx40('1080p'), cls: 'person', level: 'R', light: 'day', morM: 10000 })
    expect(w.why === null ? (w.dHiM ?? 0) > (w.dLoM ?? 0) : typeof w.why === 'string').toBe(true)
    const thermal = windowFor({ sensor: lwir640(25), cls: 'person', level: 'I', light: 'night', morM: 10000 })
    expect(thermal.why === null || typeof thermal.why === 'string').toBe(true)
  })
})

describe('WP-12b panel helpers', () => {
  it('converts the calendar input of the world zone to the UTC instant the server takes', async () => {
    const { localToUtc, periodText } = await import('@/ui/sandbox/calendar')
    expect(localToUtc('2026-10-05 10:00', 'Asia/Shanghai')).toBe('2026-10-05T02:00:00Z')
    expect(localToUtc('2026-10-05 23:30', 'UTC')).toBe('2026-10-05T23:30:00Z')
    expect(localToUtc('2026-13-05 10:00', 'UTC')).toBeNull()
    expect(localToUtc('10:00', 'UTC')).toBeNull()
    expect(periodText('2026-10-05 14:12', 'dusk')).toBe('2026-10-05 18:30')
  })

  it('lists the charging channels 1-based and groups the idle ones', async () => {
    const { channelRows } = await import('@/ui/panels/nests/nestModel')
    const r = channelRows([{ ch: 0, state: 'charging', soc_pct: 40 }, { ch: 1, state: 'idle' }, { ch: 2, state: 'idle' }, { ch: 3, state: 'blocked' }])
    expect(r.busy.map((c) => c.ch)).toEqual([0])
    expect(r.idle).toEqual([2, 3])
    expect(r.blocked).toEqual([4])
  })

  it('prefills the clone form from the summary and the parameter rows and sends only changes', async () => {
    const { baseValues, changedFields } = await import('@/ui/panels/fleet/fleetModel')
    const base = baseValues({ model_id: 'p600', name_zh: 'P600', category: 'multirotor', reference: false, version: '1', mass_kg: 3.8, speed: { cruise_mps: 8 } },
      { params_table: [{ key: 'battery.cells', value: 6, confidence: 'A' }, { key: 'acoustic.bpf_hz', value: 160, confidence: 'B' }, { key: 'other', value: 1, confidence: 'C' }] })
    expect(base).toMatchObject({ mass_kg: 3.8, 'speed.cruise_mps': 8, 'battery.cells': 6, 'acoustic.bpf_hz': 160 })
    expect(base.other).toBeUndefined()
    expect(changedFields(base, { ...base, mass_kg: 4, 'battery.cells': 8, 'speed.max_mps': Number.NaN })).toEqual({ mass_kg: 4 })
  })

  it('builds the target list from the details while the page has no TargetLite32 rows', async () => {
    const { itemsFromDetails } = await import('@/ui/panels/targets/targetModel')
    const rows = itemsFromDetails([{ id: 'person-01', label: '人-01', cls: 'person' }],
      new Map([['person-01', { awareness: 'ALERTED', discovered_count: 2, capture: { best_level: 'R', captured: true }, motion: { mode: 'random_walk' } }]]),
      new Map([['car-01', { id: 'car-01', label: '车-01', class: 'vehicle', motion: { mode: 'static' } }]]))
    expect(rows).toEqual([
      { id: 'person-01', label: '人-01', cls: 'person', aware: 'ALERTED', level: 'R', captured: true, discovered: 2, moving: true },
      { id: 'car-01', label: '车-01', cls: 'vehicle', aware: 'UNAWARE', level: 'none', captured: false, discovered: 0, moving: false },
    ])
  })

  it('derives the sun direction and the catalogue shapes of the sensor view inputs', async () => {
    const { shapeOf, sunEnuOf } = await import('@/ui/fpv/hudModel')
    const s = sunEnuOf(90, 0)
    expect(s[0]).toBeCloseTo(1)
    expect(s[1]).toBeCloseTo(0)
    expect(sunEnuOf(0, 90)[2]).toBeCloseTo(1)
    expect(shapeOf({ model_id: 'x', frame_type: 'fixed_wing', profile: { geometry: { size_m: { l: 1.2, w: 2.4, h: 0.3 } }, ir: { motor_delta_t_k: 18 } } }))
      .toEqual({ frame: 'fixed_wing', sizeM: [1.2, 2.4, 0.3], motorDeltaTK: 18 })
    expect(shapeOf({ model_id: 'x', frame_type: 'blimp', profile: { geometry: { size_m: [1, 1, 1] } } })).toBeNull()
  })

  it('opens the D2 writes on the sandbox surface and for operator or admin roles, never while stale', async () => {
    const { d2WriteAllowed } = await import('@/ui/sandbox/writeGate')
    expect(d2WriteAllowed('sandbox', false, false)).toBe(true)
    expect(d2WriteAllowed('shared', true, false)).toBe(true)
    expect(d2WriteAllowed('shared', false, false)).toBe(false)
    expect(d2WriteAllowed('sandbox', true, true)).toBe(false)
  })

  it('applies the task deep links of AWR-14 §20.2.2 (panel = tasks, relay, task-new)', async () => {
    const { applyEntityDeepLink } = await import('@/ui/sandbox/entities')
    const { prefsStore } = await import('@/stores/prefs')
    applyEntityDeepLink(new URLSearchParams('panel=relay'))
    expect(prefsStore.getState().layout.dock).toMatchObject({ open: true, tab: 'relay' })
    applyEntityDeepLink(new URLSearchParams('panel=tasks'))
    expect(prefsStore.getState().layout.dock.tab).toBe('tasks')
    applyEntityDeepLink(new URLSearchParams('panel=task-new'))
    expect(prefsStore.getState().layout.right).toMatchObject({ page: 'task', open: true })
    applyEntityDeepLink(new URLSearchParams('panel=bogus'))
    expect(prefsStore.getState().layout.dock.tab).toBe('tasks')
  })
})
