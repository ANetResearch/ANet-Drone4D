// Pure models of the D2 panels and pages (M15-FR-204, FR-211 to FR-213, FR-221; UX-FR-106, UX-FR-115 to UX-FR-125,
// UX-FR-145): the calendar presets in the world's zone, the open interface page over the contracts' OpenAPI snapshot
// (every route covered, grouped, curl without a real token), target list filters and range checks, keep-out volumes, the
// sound waveform, the clone form checks and the 522 mapping, nest compatibility and the charger cells check, the queue
// claim countdown and the lifetime text.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { isoUtc, periodCalendar, tzOffsetMs, zonedToUtcMs } from '@/ui/sandbox/calendar'
import { curlOf, groupOperations, listOperations, sdkOf } from '@/ui/views/apiDocsModel'
import { buildRows, filterRows, keepVolumes, labelOk, NO_FILTER, sensitivityErrors, waveform } from '@/ui/panels/targets/targetModel'
import { cloneErrors, derived, filterModels, mapViolations, nestAccepts, nestBlock } from '@/ui/panels/fleet/fleetModel'

vi.setConfig({ testTimeout: 30_000 })

describe('calendar presets', () => {
  it('converts wall-clock times in a zone to UTC (fixed offset and DST)', () => {
    expect(isoUtc(zonedToUtcMs(2026, 10, 5, 10, 0, 'Asia/Shanghai'))).toBe('2026-10-05T02:00:00Z')
    expect(isoUtc(zonedToUtcMs(2026, 7, 1, 10, 0, 'America/New_York'))).toBe('2026-07-01T14:00:00Z')
    expect(isoUtc(zonedToUtcMs(2026, 12, 1, 10, 0, 'America/New_York'))).toBe('2026-12-01T15:00:00Z')
    expect(tzOffsetMs(Date.UTC(2026, 0, 1), 'Asia/Shanghai')).toBe(8 * 3600_000)
    expect(tzOffsetMs(0, 'Not/AZone')).toBe(0)
  })

  it('gives today at the period time in the zone', () => {
    const now = Date.UTC(2026, 9, 5, 20, 0)
    expect(periodCalendar('night', 'Asia/Shanghai', now)).toEqual({ start_utc: '2026-10-06T15:00:00Z', tz: 'Asia/Shanghai' })
    expect(periodCalendar('day', 'Asia/Shanghai', now).start_utc).toBe('2026-10-06T02:00:00Z')
  })
})

describe('open interface page', () => {
  const snap = JSON.parse(readFileSync(join(import.meta.dirname, '../../../../packages/contracts/rest/sandbox.openapi.snapshot.json'), 'utf8')) as {
    paths: Record<string, Record<string, unknown>>
  }
  it('lists every operation of the snapshot and groups them by owner file', () => {
    const ops = listOperations(snap)
    let expected = 0
    for (const item of Object.values(snap.paths)) for (const m of ['get', 'post', 'put', 'patch', 'delete']) if (item[m]) expected++
    expect(ops).toHaveLength(expected)
    expect(new Set(ops.map((o) => o.key)).size).toBe(expected)
    const groups = groupOperations(ops)
    expect(groups.reduce((n, g) => n + g.ops.length, 0)).toBe(expected)
    expect(groups.map((g) => g.id)).toEqual(expect.arrayContaining(['sessions', 'catalog', 'vehicles', 'targets', 'tasking', 'env']))
    expect(groups.find((g) => g.id === 'other')).toBeUndefined()
  })

  it('curl uses the token placeholder and fills the sid; the SDK call uses the session', () => {
    const ops = listOperations(snap)
    const add = ops.find((o) => o.key === 'POST /sessions/{sid}/targets')
    expect(add).toBeDefined()
    const c = curlOf(add!, 'https://example.org', 'sb-k3m7q2x7ab')
    expect(c).toContain("'https://example.org/api/sandbox/v1/sessions/sb-k3m7q2x7ab/targets'")
    expect(c).toContain('Bearer $AWR_SANDBOX_TOKEN')
    expect(c).toContain('"class":"person"')
    const pub = ops.find((o) => o.key === 'GET /catalog/models')!
    expect(curlOf(pub, '', null)).not.toContain('Authorization')
    expect(sdkOf(add!, 'https://example.org')).toContain('s.targets.create(')
    expect(sdkOf(ops.find((o) => o.key === 'GET /sessions/{sid}/tracks')!, '')).toContain('s.path("tracks")')
  })

  it('reads the reason codes and the server Python sample of the live document', () => {
    const doc = { paths: { '/meta': { get: { summary: 'meta', security: [], 'x-awr-reasons': [211, 'x', 1.5],
      'x-codeSamples': [{ lang: 'Shell', label: 'curl', source: 'curl -s $BASE/api/sandbox/v1/meta' }, { lang: 'Python', label: 'awr_sandbox', source: 'print(sb.status())' }] } } } }
    const [op] = listOperations(doc)
    expect(op.reasons).toEqual([211])
    expect(op.pySample).toBe('print(sb.status())')
    expect(listOperations(snap).every((o) => Array.isArray(o.reasons) && o.pySample === null)).toBe(true)
  })
})

describe('targets', () => {
  it('builds rows with live state and filters them', () => {
    const item = (id: string, cls: string | null, o: Partial<{ label: string; aware: 'ALERTED'; level: 'R'; captured: boolean; discovered: number; moving: boolean }> = {}) => ({
      id, label: o.label ?? '', cls: cls as 'person', aware: o.aware ?? 'UNAWARE' as const, level: o.level ?? 'none' as const, captured: o.captured ?? false,
      discovered: o.discovered ?? 0, moving: o.moving ?? false })
    const rows = buildRows([item('a', 'person', { aware: 'ALERTED', level: 'R', captured: true, discovered: 2, moving: true }), item('b', 'dog', { label: 'B' }), item('c', 'alien')],
      (id) => (id === 'a' ? 'random_walk' : undefined))
    expect(rows.map((r) => r.cls)).toEqual(['person', 'dog', 'object'])
    expect(rows[0]).toMatchObject({ motion: 'random_walk', best: 'R', captured: true, discovered: 2 })
    expect(filterRows(rows, { ...NO_FILTER, discoveredOnly: true }).map((r) => r.id)).toEqual(['a'])
    expect(filterRows(rows, { ...NO_FILTER, cls: 'dog' }).map((r) => r.id)).toEqual(['b'])
    expect(filterRows(rows, { ...NO_FILTER, best: 'none' }).map((r) => r.id)).toEqual(['b', 'c'])
  })

  it('checks labels and sensitivity volumes', () => {
    expect(labelOk('人-01')).toBe(true)
    expect(labelOk('')).toBe(false)
    expect(labelOk('x'.repeat(33))).toBe(false)
    expect(sensitivityErrors([{ kind: 'visual', shape: 'sector', radius_max_m: 0, half_angle_deg: 400 }])).toEqual({ 0: ['radius_max_m', 'z', 'half_angle_deg'] })
    expect(sensitivityErrors([{ kind: 'acoustic', shape: 'hemisphere', radius_max_m: 300, z_min_m: 0, z_max_m: 300 }])).toEqual({})
  })

  it('keep-out volumes add the 20 m margin; a moving target sector is a full circle', () => {
    const v = keepVolumes([
      { id: 'p', class: 'person', pos: [10, 20, null], motion: { mode: 'random_walk' }, sensitivity: [{ kind: 'visual', shape: 'sector', radius_max_m: 300 }, { kind: 'radar', shape: 'sphere', radius_max_m: 50 }] },
      { id: 's', class: 'object', pos: [0, 0], sensitivity: [{ kind: 'acoustic', shape: 'hemisphere', radius_max_m: 100, enabled: false }] },
    ])
    expect(v).toEqual([{ target: 'p', kind: 'visual', shape: 'cylinder', c: [10, 20, 0], r: 320, zMin: 0, zMax: 300 }])
  })

  it('draws one period of the source: silent outside the duty cycle, deterministic', () => {
    const s = { waveform: 'pulse_train' as const, f0_hz: 200, lw_dba: 70, period_s: 8, duty: 0.25 }
    const w = waveform(s, 81)
    expect(w).toHaveLength(81)
    expect(w[0].t).toBe(0)
    expect(w.at(-1)?.t).toBeCloseTo((8 * 80) / 81, 9)
    expect(w.filter((p) => p.t >= 2.1).every((p) => p.v === 0)).toBe(true)
    expect(w.filter((p) => p.t < 2).some((p) => Math.abs(p.v) > 0.1)).toBe(true)
    expect(waveform({ ...s, waveform: 'noise_band' }, 50)).toEqual(waveform({ ...s, waveform: 'noise_band' }, 50))
    expect(Math.max(...waveform({ ...s, waveform: 'harmonic', duty: 1 }).map((p) => Math.abs(p.v)))).toBeLessThanOrEqual(1)
  })
})

describe('fleet', () => {
  it('filters the catalogue, checks the clone form and maps 522 violations', () => {
    const models = [{ model_id: 'p600_mid360', name_zh: 'P600', category: 'multirotor', reference: false, version: '2' }, { model_id: 'awr_f1', category: 'fixed_wing', reference: true, version: '1' }] as const
    expect(filterModels(models, 'fixed_wing', '').map((m) => m.model_id)).toEqual(['awr_f1'])
    expect(filterModels(models, 'all', 'p6').map((m) => m.model_id)).toEqual(['p600_mid360'])
    expect(cloneErrors({ mass_kg: 5, mtow_kg: 4 }, 'multirotor')).toEqual({ mass_kg: 'mass_le_mtow' })
    expect(cloneErrors({ 'speed.cruise_mps': 20, 'speed.max_mps': 15 }, 'fixed_wing')).toEqual({ 'speed.cruise_mps': 'cruise_le_max' })
    expect(cloneErrors({ 'speed.min_mps': 20, 'speed.cruise_mps': 20, 'speed.max_mps': 30 }, 'fixed_wing')).toEqual({ 'speed.min_mps': 'min_lt_cruise' })
    expect(cloneErrors({ 'link.range_m': 100 }, 'multirotor')).toEqual({ 'link.range_m': 'range' })
    expect(derived({ 'battery.cells': 6, 'battery.capacity_ah': 10 }).useWh).toBeCloseTo(0.85 * 222, 6)
    const m = mapViolations([{ field: 'set.mass_kg', rule: 'VH-2', limit: 4.1 }, { field: 'mounts.lwir', rule: 'MTOW', message_zh: '超出' }])
    expect(m.fields).toEqual({ mass_kg: 'VH-2 4.1' })
    expect(m.other).toEqual(['mounts.lwir: 超出'])
  })

  it('nest compatibility and defaults', async () => {
    const { chargerMismatch, nestDefaults } = await import('@/ui/panels/nests/NestsPanel')
    expect(nestAccepts('box', 'multirotor')).toBe(true)
    expect(nestAccepts('box', 'fixed_wing')).toBe(false)
    expect(nestAccepts('launcher', 'fixed_wing')).toBe(true)
    expect(nestAccepts('vtol_pad', 'vtol')).toBe(true)
    expect(nestDefaults('launcher', 1, 2)).toMatchObject({ pos: [1, 2, null], kind: 'launcher', crewed: true })
    expect(chargerMismatch({ energy: { mode: 'swap', packs: [{ cells: 12, count: 2 }] }, chargers: [{ model: 'c', count: 1, channels: 1, i_max_a: 10, cells_max: 6 }] })).toBe(true)
    expect(chargerMismatch(nestDefaults('box', 0, 0))).toBe(false)
  })

  it('nestBlock mirrors the server 525: kind first, then a full nest counts airborne vehicles too (issue #8)', () => {
    const box = { kind: 'box' as const, slots: 6, inventory: [{ count: 6 }] }
    expect(nestBlock(box, undefined, 'fixed_wing')).toBe('kind')
    expect(nestBlock(box, undefined, 'multirotor')).toBe('full')
    expect(nestBlock({ ...box, inventory: [{ count: 5 }] }, undefined, 'multirotor')).toBeNull()
    const st = { slots: { total: 6 }, inventory: [{ count: 4 }, { count: 2 }] }
    expect(nestBlock({ kind: 'box', slots: 6 }, st, 'multirotor')).toBe('full')
    expect(nestBlock({ kind: 'box', slots: 6 }, { ...st, inventory: [{ count: 3 }] }, null)).toBeNull()
    expect(nestBlock({ kind: 'launcher' }, undefined, null)).toBeNull()
  })
})

describe('session texts', () => {
  it('claim countdown and lifetime text', async () => {
    const { claimLeftS } = await import('@/ui/sandbox/QueueCard')
    const { lifetimeText } = await import('@/ui/sandbox/format')
    expect(claimLeftS(10_500, 1000)).toBe(10)
    expect(claimLeftS(500, 1000)).toBe(0)
    expect(claimLeftS(null)).toBe(0)
    expect(lifetimeText(27 * 60 + 41.9)).toBe('27:41')
    expect(lifetimeText(Number.NaN)).toBe('—')
  })
})
