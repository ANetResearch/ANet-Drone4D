// M20-AC-003 (client part), AC-014 (SDK-less part), AC-016 (store part), AC-028 (store part); M20 PRD §8 item 1: the tasking
// store subscribes the three 1 Hz topics, mirrors nests, tasks and relay statistics (600-sample history), fetches the
// plan geometry when the status revision grows (sandbox REST), runs the dry-run with 202 polling and keeps the 422
// problem for the form, sends the writes with the contract shapes (REST in the sandbox, WS calls on the shared show),
// refuses writes while the session is stale (D2.1 energy-saving mode) and resets on an endpoint change.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { NestStatus, RelayStats, TaskingTask } from '@awr/contracts/types'
import { ApiError } from '@/net/api'
import { setEndpoint } from '@/net/endpoint'
import type { CallResult, DataMsg, RtClient, RtEvent } from '@/net/rt'
import {
  RELAY_HISTORY, assignmentOf, attachTaskingFeed, configureTaskingPort, nestPacks, nestQueue, resetTasking, taskGeometry, taskingActions,
  taskingFeedTick, taskingStore, taskingSummary, whyKey, type TaskStatusMsg, type TaskingPort,
} from '@/stores/tasking'

const SID = 'sb-k3m7q2x7ab'

class FakeRt {
  calls: { service: string; args: object }[] = []
  subs: string[] = []
  data: ((m: DataMsg) => void)[] = []
  ev: ((b: readonly RtEvent[]) => void)[] = []
  status = 'LIVE'
  serverInfo: { role: string } | null = { role: 'operator' }
  subscribe(topic: string) {
    this.subs.push(topic)
    return () => {
      this.subs = this.subs.filter((t) => t !== topic)
    }
  }
  onData(cb: (m: DataMsg) => void) {
    this.data.push(cb)
    return () => {
      this.data = this.data.filter((c) => c !== cb)
    }
  }
  onEvents(cb: (b: readonly RtEvent[]) => void) {
    this.ev.push(cb)
    return () => {
      this.ev = this.ev.filter((c) => c !== cb)
    }
  }
  call(service: string, args: object) {
    this.calls.push({ service, args })
    const r: CallResult = { id: 'c1', status: 'succeeded', code: 0, final: true, data: { result: { tid: 't-9', state: 'PLANNING' } } }
    return { id: 'c1', result: Promise.resolve(r), onResult() {}, onProgress() {}, cancel() {} }
  }
  push(topic: string, data: unknown) {
    for (const cb of this.data) cb({ topic, channelId: 1, seq: 1, tSimMs: 0, data })
  }
}

class FakePort implements TaskingPort {
  gets: string[] = []
  posts: { path: string; body: unknown; headers?: Record<string, string> }[] = []
  patches: { path: string; body: unknown }[] = []
  dels: string[] = []
  routes = new Map<string, (path: string, body?: unknown) => unknown>()
  async get<T>(path: string): Promise<T> {
    this.gets.push(path)
    return this.answer(path) as T
  }
  async post<T>(path: string, body: unknown, headers?: Record<string, string>): Promise<T> {
    this.posts.push({ path, body, headers })
    return this.answer(path, body) as T
  }
  async patch<T>(path: string, body: unknown): Promise<T> {
    this.patches.push({ path, body })
    return this.answer(path, body) as T
  }
  async del(path: string): Promise<number> {
    this.dels.push(path)
    this.answer(path)
    return 204
  }
  private answer(path: string, body?: unknown): unknown {
    for (const [k, fn] of this.routes) if (path.startsWith(k)) return fn(path, body)
    return {}
  }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

function nestStatus(id: string, extra: Partial<NestStatus> = {}): NestStatus {
  return { id, kind: 'box', slots: { total: 4, occupied: 2 }, inventory: [{ model_id: 'p600_mid360', count: 2 }], wind_ok: true,
    aircraft: [{ uav: 'p600-01', state: 'airborne', soc: 0.8 }, { uav: 'p600-02', state: 'ready', soc: 0.95 }],
    packs: [{ pack_id: 'a', state: 'ready', soc: 0.95 }, { pack_id: 'b', state: 'charging', soc: 0.4 }],
    chargers: [{ ch: 0, state: 'charging', pack_id: 'b', soc_pct: 40, eta_s: 1800 }],
    launch_queue: [{ uav: 'p600-02', tid: 't-1', delay_reason: 'battery' }], recover_queue: [{ uav: 'p600-01', eta_s: 60 }], ...extra }
}

function status(tid: string, revision: number, extra: Partial<TaskStatusMsg> = {}): TaskStatusMsg {
  return { tid, type: 'area_scan_gsd', state: 'RUNNING', revision, progress: { pct: 25 }, n_vehicles: 2, makespan_s: 480,
    items: [{ wid: `${tid}.g0`, kind: 'strip_group', uav: 'p600-01', state: 'ACTIVE', eta_s: 30 }], ...extra }
}

function relay(tid: string, tMon: number, gaps = 0): RelayStats {
  return { tid, target: 'person-01', t_monitor_s: tMon, coverage: 0.99, gaps: { count: gaps, max_s: 0, by_cause: {} }, discovered: 0,
    config_entries: { count: 0, total_s: 0 }, handovers: { count: 1, mean_overlap_s: 16 }, on_station: [{ uav: 'p600-02', station: `${tid}.st` }] }
}

function taskObj(tid: string, revision: number): TaskingTask {
  return { tid, type: 'area_scan_gsd', label: '扫描', state: 'RUNNING', revision, params: {}, parent: null,
    plan: { revision, geometry: { strips: [{ id: 's0', p0: [0, 0, 110], p1: [100, 0, 110], uav: 'p600-01' }] },
      assignments: [{ uav: 'p600-01', items: [{ wid: `${tid}.g0`, kind: 'strip_group' }] }] } }
}

describe('tasking store (M20 PRD §8 item 1)', () => {
  let rt: FakeRt
  let port: FakePort
  let detach: () => void
  beforeEach(() => {
    setEndpoint({ kind: 'shared' })
    rt = new FakeRt()
    port = new FakePort()
    configureTaskingPort(port)
    resetTasking()
    detach = attachTaskingFeed(() => rt as unknown as RtClient)
  })
  afterEach(() => {
    detach()
    configureTaskingPort(null)
    setEndpoint({ kind: 'shared' })
  })

  it('subscribes the three 1 Hz topics and mirrors nests, tasks and relay statistics', () => {
    expect(rt.subs).toEqual(expect.arrayContaining(['nest/*/status', 'task/*/status', 'relay/*/stats']))
    rt.push('nest/n-1/status', nestStatus('n-1'))
    rt.push('task/t-1/status', status('t-1', 2))
    rt.push('task/t-2/status', { ...status('t-2', 1), type: 'relay_watch', items: [] })
    rt.push('relay/t-2/stats', relay('t-2', 10))
    rt.push('relay/t-2/stats', relay('t-2', 10))
    rt.push('relay/t-2/stats', relay('t-2', 11, 1))
    rt.push('bogus/topic', {})
    taskingFeedTick(rt as unknown as RtClient)
    const s = taskingStore.getState()
    expect(s.mode).toBe('shared')
    expect(s.canEdit).toBe(true)
    expect(Object.keys(s.nestStatus)).toEqual(['n-1'])
    expect(s.tasks.map((t) => t.tid)).toEqual(['t-1', 't-2'])
    expect(s.tasks[0].progressPct).toBe(25)
    expect(s.tasks[1].relay?.coverage).toBe(0.99)
    expect(s.tasks[1].relay?.onStation).toEqual(['p600-02'])
    expect(s.relayHistory['t-2'].map((x) => x.tMonitorS)).toEqual([10, 11])
    const v = s.version
    taskingFeedTick(rt as unknown as RtClient)
    expect(taskingStore.getState().version).toBe(v)
    // selectors
    expect(nestQueue(s, 'n-1').launch[0].uav).toBe('p600-02')
    expect(nestPacks(s, 'n-1')).toEqual({ ready: 1, charging: 1, queued: 0, batteryDelay: true })
    expect(assignmentOf(s).get('p600-01')).toEqual({ tid: 't-1', wid: 't-1.g0', kind: 'strip_group' })
    const sum = taskingSummary(s)
    expect(sum).toMatchObject({ active: 2, running: 2, airborne: 1, readyPacks: 1 })
    expect(sum.relays[0].tid).toBe('t-2')
    expect(whyKey(580, 'fleet_short')).toBe('tasking.why.fleet_short')
    expect(whyKey(580, null)).toBe('reason.580')
  })

  it('keeps at most 600 relay samples', () => {
    for (let k = 0; k < RELAY_HISTORY + 20; k++) rt.push('relay/t-3/stats', relay('t-3', k))
    taskingFeedTick(rt as unknown as RtClient)
    const h = taskingStore.getState().relayHistory['t-3']
    expect(h.length).toBe(RELAY_HISTORY)
    expect(h[0].tMonitorS).toBe(20)
  })

  it('sandbox: refreshes nests and tasks, refetches the plan when the revision grows', async () => {
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok' })
    let rev = 1
    port.routes.set('/nests', () => ({ items: [{ id: 'n-1', pos: [0, 0, 5], kind: 'box', slots: 4 }] }))
    port.routes.set('/tasks/t-1?include=plan', () => taskObj('t-1', rev))
    port.routes.set('/tasks', () => ({ items: [taskObj('t-1', rev)] }))
    rt.push('task/t-1/status', status('t-1', 1))
    rt.push('nest/n-1/status', nestStatus('n-1'))
    taskingFeedTick(rt as unknown as RtClient)
    await flush()
    taskingFeedTick(rt as unknown as RtClient)
    const s = taskingStore.getState()
    expect(s.mode).toBe('sandbox')
    expect(port.gets).toEqual(expect.arrayContaining(['/nests', '/tasks', '/tasks/t-1?include=plan']))
    expect(s.nests.map((n) => n.id)).toEqual(['n-1'])
    expect(s.tasks[0].label).toBe('扫描')
    expect(taskGeometry(s, 't-1')?.strips?.length).toBe(1)
    // same revision: no refetch; larger revision: refetch (after the minimum gap)
    const n0 = port.gets.filter((p) => p.includes('include=plan')).length
    rt.push('task/t-1/status', status('t-1', 1))
    taskingFeedTick(rt as unknown as RtClient)
    expect(port.gets.filter((p) => p.includes('include=plan')).length).toBe(n0)
    rev = 2
    rt.push('task/t-1/status', status('t-1', 2))
    await new Promise((r) => setTimeout(r, 2100))
    taskingFeedTick(rt as unknown as RtClient)
    await flush()
    taskingFeedTick(rt as unknown as RtClient)
    expect(port.gets.filter((p) => p.includes('include=plan')).length).toBe(n0 + 1)
    expect(taskingStore.getState().plans['t-1'].revision).toBe(2)
    // endpoint change resets
    setEndpoint({ kind: 'shared' })
    expect(taskingStore.getState().tasks).toEqual([])
  })

  it('sandbox actions: REST shapes, idempotency keys, preview polling and the 422 problem', async () => {
    setEndpoint({ kind: 'sandbox', sid: SID, token: () => 'tok' })
    port.routes.set('/nests/n-1', () => ({ id: 'n-1', pos: [0, 0, 0], name: 'x' }))
    port.routes.set('/nests', (_p, body) => ({ ...(body as object), id: 'n-1', vehicle_ids: ['p600-01'] }))
    port.routes.set('/tasking/snapshot', () => ({ revision: 3, t_sim_ns: 0, work_items: [], vehicles: [],
      keepouts: [{ id: 'person-01/0', shape: 'cylinder', center_enu_m: [50, 60, 0], r_m: 340 }] }))
    let polls = 0
    port.routes.set('/tasks/previews/', () => (++polls < 2 ? { preview_id: 'pv-abcdefgh', state: 'pending' } :
      { preview_id: 'pv-abcdefgh', n_vehicles: 2, assignments: [], geometry: { strips: [] } }))
    port.routes.set('/tasks?dry_run=true', (_p, body) => {
      const b = body as { type: string }
      if (b.type === 'area_patrol') throw new ApiError(422, 580, 'x', 'TASK_INFEASIBLE', null, { why: 'fleet_short', remedies: [{ kind: 'fleet_short' }] })
      return { preview_id: 'pv-abcdefgh', state: 'pending' }
    })
    port.routes.set('/tasks/t-5:', () => ({ tid: 't-5', state: 'PAUSED' }))
    port.routes.set('/tasks', () => ({ tid: 't-5', type: 'area_scan_gsd', state: 'PLANNING', revision: 0, params: {} }))
    rt.push('task/t-5/status', status('t-5', 0))
    taskingFeedTick(rt as unknown as RtClient)
    const add = await taskingActions.addNest({ pos: [0, 0, null], kind: 'box', inventory: [{ model_id: 'p600_mid360', count: 1 }] })
    expect(add.ok).toBe(true)
    expect(add.data?.vehicle_ids).toEqual(['p600-01'])
    expect(port.posts[0].path).toBe('/nests')
    expect(port.posts[0].headers?.['Idempotency-Key']).toMatch(/^nest-/)
    expect((await taskingActions.updateNest('n-1', { name: 'x' })).ok).toBe(true)
    expect(port.patches[0]).toEqual({ path: '/nests/n-1', body: { name: 'x' } })
    expect((await taskingActions.removeNest('n-1')).ok).toBe(true)
    expect(port.dels).toEqual(['/nests/n-1'])
    // dry-run: 202 then SB-68 polling; the keepout band of the snapshot
    const spec = { type: 'area_scan_gsd' as const, params: { polygon: [[0, 0], [100, 0], [100, 100]], gsd_cm: 3 } }
    const pv = await taskingActions.preview(spec)
    expect(pv.state).toBe('done')
    expect(pv.previewId).toBe('pv-abcdefgh')
    expect(port.gets.filter((p) => p.startsWith('/tasks/previews/')).length).toBe(2)
    expect(taskingStore.getState().preview?.state).toBe('done')
    await flush()
    expect(taskingStore.getState().previewKeepouts).toEqual([{ id: 'person-01/0', x: 50, y: 60, r: 340 }])
    expect(taskGeometry(taskingStore.getState(), 'pv-abcdefgh')).toEqual({ strips: [] })
    // create reuses the preview id when the spec is the same
    const c = await taskingActions.create(spec)
    expect(c.ok).toBe(true)
    const body = port.posts.find((p) => p.path === '/tasks')?.body as { preview_id?: string }
    expect(body.preview_id).toBe('pv-abcdefgh')
    expect(taskingStore.getState().selected).toBe('t-5')
    // the infeasible problem stays in the entry for the form
    const bad = await taskingActions.preview({ type: 'area_patrol', params: { polygon: [[0, 0], [1, 0], [1, 1]] } })
    expect(bad.state).toBe('infeasible')
    expect(bad.code).toBe(580)
    expect(bad.detail?.why).toBe('fleet_short')
    // control
    const ctl = await taskingActions.control('t-5', 'pause')
    expect(ctl.ok).toBe(true)
    expect(port.posts.at(-1)?.path).toBe('/tasks/t-5:pause')
    // external plan
    port.routes.set('/tasking/plans', () => ({ plan_id: 'pl-1', applied: true }))
    const pl = await taskingActions.submitPlan(3, [{ wid: 't-5.g0', uav: 'p600-01', order: 0 }])
    expect(pl.data).toEqual({ plan_id: 'pl-1', applied: true })
    expect(port.posts.at(-1)?.body).toEqual({ based_on_revision: 3, allocator: 'external', assignments: [{ wid: 't-5.g0', uav: 'p600-01', order: 0 }] })
    taskingActions.clearPreview()
    expect(taskingStore.getState().preview).toBeNull()
  })

  it('shared show: writes go through the WS calls, the dry-run is sandbox only; stale refuses writes', async () => {
    const r = await taskingActions.guardArea([[0, 0], [200, 0], [200, 200], [0, 200]])
    expect(r.ok).toBe(true)
    expect(rt.calls[0].service).toBe('task/create')
    expect(rt.calls[0].args).toMatchObject({ type: 'area_guard', params: { target_class: 'person', level_req: 'R', revisit_s: 300 } })
    await taskingActions.control('t-9', 'cancel')
    expect(rt.calls[1].service).toBe('task/t-9/cancel')
    await taskingActions.relayTarget('dog-01', 'I')
    expect(rt.calls[2].args).toEqual({ type: 'relay_watch', params: { target: 'dog-01', level_req: 'I' } })
    expect((await taskingActions.preview({ type: 'point_watch', params: { point: [0, 0] } })).code).toBe(305)
    rt.status = 'RECONNECTING'
    rt.serverInfo = { role: 'viewer' }
    taskingFeedTick(rt as unknown as RtClient)
    expect(taskingStore.getState().stale).toBe(true)
    expect(taskingStore.getState().canEdit).toBe(false)
    const n = rt.calls.length
    expect((await taskingActions.watchPoint([10, 20])).code).toBe(211)
    expect(rt.calls.length).toBe(n)
  })
})
