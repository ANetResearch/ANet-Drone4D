// stores/catalog.ts (M21; AWR-20 §9; AWR-17 §17.6.2 SB-21 to SB-27; 14 §20.6.2 to §20.6.4) against a stubbed fetch that
// serves the generated catalog REST view (python/awr/sim/catalog/rest_view.json, the bytes the sandbox-api returns):
// preset cards in catalog order with reference flags and the disclaimer, ETag revalidation (304 keeps the cached lists),
// category toggle and search, headline endurance, the GSD closed form, the mount mass margin of the clone form, clone
// create (201: session clone listed after the presets) and the 522 violation mapping to form fields, delete refused 409.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CATALOG_API, DISCLAIMER_ZH, catalog, catalogStore, gsdCm, headlineEndurance, isReference, mapViolations, mountMarginKg,
  visibleModels, type ModelDetail,
} from '@/stores/catalog'

const ROOT = new URL('../../../../', import.meta.url)
interface View {
  models: { items: { model_id: string; category: string; reference: boolean; endurance?: Record<string, number> }[] }
  details: Record<string, ModelDetail>
  sensors: { items: unknown[] }
  etag: { models: string; sensors: string; details: Record<string, string> }
}
const VIEW = JSON.parse(readFileSync(new URL('python/awr/sim/catalog/rest_view.json', ROOT), 'utf8')) as View
const SID = 'sb-k3m7q2x7ab'

interface Req { url: string; method: string; headers: Record<string, string>; body: unknown }
let reqs: Req[] = []
let sessionReply: (r: Req) => { status: number; body: unknown } = () => ({ status: 404, body: { code: 305 } })

function headersOf(h: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  if (h instanceof Headers) h.forEach((v, k) => { out[k.toLowerCase()] = v })
  else if (h && typeof h === 'object') for (const [k, v] of Object.entries(h as Record<string, string>)) out[k.toLowerCase()] = v
  return out
}

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(status === 304 || status === 204 ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } })
}

beforeEach(() => {
  reqs = []
  catalog.reset()
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    const u = String(url)
    const r: Req = { url: u, method: init?.method ?? 'GET', headers: headersOf(init?.headers), body: typeof init?.body === 'string' ? JSON.parse(init.body) : null }
    reqs.push(r)
    const pub = (body: unknown, tag: string): Response =>
      r.headers['if-none-match'] === `"${tag}"` ? reply(304, null, { etag: `"${tag}"` }) : reply(200, body, { etag: `"${tag}"`, 'cache-control': 'public, max-age=300' })
    if (u.endsWith(`${CATALOG_API}/models`)) return pub(VIEW.models, VIEW.etag.models)
    if (u.endsWith(`${CATALOG_API}/sensors`)) return pub(VIEW.sensors, VIEW.etag.sensors)
    const m = u.match(/\/catalog\/models\/([a-z0-9_]+)$/)
    if (m) {
      const d = VIEW.details[m[1]]
      return d ? pub(d, VIEW.etag.details[m[1]]) : reply(404, { code: 520 })
    }
    const s = sessionReply(r)
    return reply(s.status, s.body)
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  sessionReply = () => ({ status: 404, body: { code: 305 } })
})

describe('catalog store', () => {
  it('loads preset cards in catalog order, revalidates with the ETag', async () => {
    await catalog.load({ sensors: true })
    const s = catalogStore.getState()
    expect(s.state).toBe('ready')
    expect(s.models.map((m) => m.model_id)).toEqual(['p600_mid360', 'awr_q1', 'awr_q9', 'awr_f1', 'awr_v1'])
    expect(s.models.filter(isReference).map((m) => m.model_id)).toEqual(['awr_q1', 'awr_q9', 'awr_f1', 'awr_v1'])
    expect(s.sensors).toHaveLength(7)
    expect(reqs.every((r) => !('authorization' in r.headers))).toBe(true) // public routes: no token
    const v0 = catalogStore.getState().version
    await catalog.load()
    expect(reqs.at(-1)?.headers['if-none-match']).toBe(`"${VIEW.etag.models}"`)
    expect(catalogStore.getState().models).toBe(s.models) // 304 keeps the cached list
    expect(catalogStore.getState().version).toBe(v0 + 1)
  })

  it('filters by category and text, picks the headline endurance', async () => {
    await catalog.load()
    catalog.setFilter('fixed_wing')
    expect(visibleModels(catalogStore.getState()).map((m) => m.model_id)).toEqual(['awr_f1'])
    catalog.setFilter('all')
    catalog.setQuery('q9')
    expect(visibleModels(catalogStore.getState()).map((m) => m.model_id)).toEqual(['awr_q9'])
    const by = Object.fromEntries(catalogStore.getState().models.map((m) => [m.model_id, m]))
    expect(headlineEndurance(by.p600_mid360)?.kind).toBe('hover')
    expect(headlineEndurance(by.p600_mid360)?.s).toBeCloseTo(1740, -1)
    expect(headlineEndurance(by.awr_f1)).toEqual({ kind: 'cruise', s: by.awr_f1.endurance?.cruise_s })
    expect(headlineEndurance(by.awr_v1)?.kind).toBe('mission')
    expect((headlineEndurance(by.awr_v1)?.s ?? 0) / 60).toBeCloseTo(102.1, 1)
  })

  it('details carry the disclaimer for reference models and the slot table', async () => {
    const f1 = await catalog.detail('awr_f1')
    expect(f1?.catalog?.disclaimer_zh).toBe(DISCLAIMER_ZH)
    expect(f1?.fw?.loiter_radius_table?.find((r) => r.w_max_mps === 10)?.r_loiter_min_m).toBeCloseTo(96.5, 1)
    const p600 = await catalog.detail('p600_mid360')
    expect(p600?.catalog?.disclaimer_zh).toBeNull()
    expect(await catalog.detail('x500')).toBeNull()
    const q1 = await catalog.detail('awr_q1')
    expect(q1 && mountMarginKg(q1, {})).toBeCloseTo(0.10, 6)
    expect(q1 && mountMarginKg(q1, { lwir: true })).toBeCloseTo(0.01, 6) // 1.05 − (0.95 + 0.09)
    expect(catalogStore.getState().details.size).toBe(2 + 1)
  })

  it('GSD closed form R·p\'/f', () => {
    expect(gsdCm(100, 48, 2.9)).toBeCloseTo(0.604, 3)
    expect(gsdCm(1000, 4.8, 2.9)).toBeCloseTo(60.4, 1)
    expect(Number.isNaN(gsdCm(0, 48, 2.9))).toBe(true)
  })

  it('creates a session clone and maps 522 violations to the form', async () => {
    await catalog.load()
    const clone = { ...VIEW.details.awr_q1, model_id: 'awr_q1_c1', name_zh: 'Q1 热像', base: 'awr_q1@1.0.0' }
    sessionReply = (r) => (r.method === 'POST' && (r.body as { patch?: { mass_kg?: number } }).patch?.mass_kg === undefined
      ? { status: 201, body: clone }
      : { status: 422, body: { code: 522, message: '克隆机型或挂载未通过自洽检查', detail: { violations: [
        { field: 'mass_kg', rule: 'CL-2', value: 5, limit: [0.665, 1.05], message_zh: '质量须在 [0.7·m_base, mtow_kg] 内' },
        { field: 'VH-7', rule: 'CL-3', value: 5, limit: [0, 1.05], message_zh: 'VH-7 检查不通过' }] } } })
    const d = await catalog.createClone(SID, { from_model_id: 'awr_q1', label: 'Q1 热像', patch: { sensors: [{ name: 'lwir' }] } })
    expect(d?.model_id).toBe('awr_q1_c1')
    expect(reqs.at(-1)?.url).toContain(`/api/sandbox/v1/sessions/${SID}/models`)
    const s = catalogStore.getState()
    expect(s.sessionModels.map((m) => [m.model_id, m.session_clone])).toEqual([['awr_q1_c1', true]])
    expect(visibleModels(s).at(-1)?.model_id).toBe('awr_q1_c1')
    const bad = await catalog.createClone(SID, { from_model_id: 'awr_q1', patch: { mass_kg: 5 } }, ['mass_kg', 'battery.capacity_ah'])
    expect(bad).toBeNull()
    const e = catalogStore.getState()
    expect(e.fieldErrors).toEqual({ mass_kg: '质量须在 [0.7·m_base, mtow_kg] 内' })
    expect(e.formErrors).toEqual(['VH-7 检查不通过'])
    expect(e.saving).toBe(false)
  })

  it('delete refused while an instance uses the clone', async () => {
    sessionReply = (r) => (r.method === 'DELETE' ? { status: 409, body: { code: 105, message: '状态不允许' } } : { status: 404, body: {} })
    expect(await catalog.deleteClone(SID, 'awr_q1_c1')).toBe(false)
    expect(catalogStore.getState().formErrors).toHaveLength(1)
  })

  it('maps violations without a field list to field errors', () => {
    const r = mapViolations([{ field: 'link.range_m', rule: 'CL-2', message_zh: '链路距离须在 [0.5, 2]·base 内' }])
    expect(r.fieldErrors).toEqual({ 'link.range_m': '链路距离须在 [0.5, 2]·base 内' })
    expect(r.formErrors).toEqual([])
  })
})
