// M14 stores/agents.ts（M14 §8.3；M14-AC-035 vitest 部分）：≤ 4 Hz 合批写入、任务环 256 上限、证据按需加载、文本净化后
// emoji 为 0、两轴分列、协作叠加 selector、面板开关驱动订阅。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataMsg, RtEvent } from '@/net/rt'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/net/api', () => ({ apiGet: (...a: unknown[]) => apiGet(...a), apiPost: (...a: unknown[]) => apiPost(...a) }))

const S = await import('@/stores/agents')
const { hasForbidden } = await import('@/lib/sanitize')

// 禁用字形在运行时由码点构造（源码中不出现，D1-AC-20）
const FIRE = String.fromCodePoint(0x1f525)
const SUN = String.fromCodePoint(0x2600)
const SIREN = String.fromCodePoint(0x1f6a8)
const SMILE = String.fromCodePoint(0x1f600)
const AID = 'bafyreifuuyi4rfxeoazbrznnzbvyhemsdinnifyz7tbvb6azzz3ccy7bou'

function task(i: number, state = 'working', extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { task_id: `T-${String(i).padStart(4, '0')}`, capability: 'thermal.imaging', state, phase: 'enroute', alloc: 'awarding',
    provider_aid: AID, vehicle_id: 'p600-b1', ix: 'ix_1', effect: { status: 'UNVERIFIED', verify_trust: 0, simulated: false },
    verified: false, predicate_ok: null, scope_ok: null, reason: null, reason_code: 0, retries_left: 2, conf_claim: 0.42,
    conf_current: 0.42, target_enu_m: [-24, -1253, null], origin: 'agent', t_submit_ns: i * 1e9, t_update_ns: i * 1e9 + 5e8,
    requester_aid: 'bafyreia1', eta_s: 46.2, progress: 0.1, merged_count: 0, ...extra }
}

beforeEach(() => {
  vi.useFakeTimers()
  S.resetAgents()
  apiGet.mockReset()
  apiPost.mockReset()
})
afterEach(() => vi.useRealTimers())

describe('agents store', () => {
  it('coalesces writes to <= 4 per second', () => {
    const v0 = S.agentsStore.getState().version
    for (let i = 0; i < 99; i++) { // 半开窗口 [0, 1000) ms
      const t = performance.now()
      S.ingestStatusWire([{ aid: AID, vehicle_id: 'p600-b1', health: null, load: 0, current_task: null, phase: null,
        lease_owner: 'MISSION', soc_pct: 72, trust: { verify_max: 2, auth: 1 }, t_sim_ns: t * 1e6 }], t)
      S.ingestTasksWire({ version: i, t_sim_ns: 0, tasks: [task(1)] }, t)
      vi.advanceTimersByTime(10)
    }
    const writes = S.agentsStore.getState().version - v0 // 1 s【墙钟】内 99 × 2 次输入
    expect(writes).toBeGreaterThan(0)
    expect(writes).toBeLessThanOrEqual(4)
    vi.advanceTimersByTime(300)
    const a = S.agentsStore.getState().agents.get(AID)!
    expect(a.socPct).toBe(72)
    expect(a.leaseOwner).toBe('MISSION')
  })

  it('keeps a ring of at most 256 tasks, terminal dropped first', () => {
    const rows = []
    for (let i = 0; i < 300; i++) rows.push(task(i, i < 100 ? 'completed' : 'working'))
    S.ingestTasksWire({ version: 1, t_sim_ns: 0, tasks: rows }, 0)
    S.flushAgents(1000)
    const s = S.agentsStore.getState()
    expect(s.tasks.length).toBe(256)
    expect(s.tasks.filter((t) => t.state === 'working').length).toBe(200)
    expect(s.tasks[0].tUpdateMs).toBeGreaterThanOrEqual(s.tasks[1].tUpdateMs)
  })

  it('keeps task state and effect status as separate columns; counts attention', () => {
    S.ingestTasksWire({ version: 1, t_sim_ns: 0, tasks: [
      task(1, 'completed', { effect: { status: 'OK', verify_trust: 4, simulated: true }, verified: true, predicate_ok: true, scope_ok: true }),
      task(2, 'input-required', { reason: 'escalated', reason_code: 0 }), task(3, 'rejected', { reason_code: 474 })] }, 0)
    S.flushAgents(1000)
    const s = S.agentsStore.getState()
    const done = s.tasks.find((t) => t.taskId === 'T-0001')!
    expect(done.state).toBe('completed')
    expect(done.effect).toEqual({ status: 'OK', verifyTrust: 4, simulated: true })
    expect(done.verified).toBe(true)
    expect(s.attentionCount).toBe(2)
    expect(s.tasks.find((t) => t.taskId === 'T-0003')!.reasonCode).toBe(474)
  })

  it('sanitizes agent text (no emoji or forbidden glyphs)', () => {
    S.ingestTasksWire({ version: 1, t_sim_ns: 0, tasks: [task(1, 'failed', { reason: `boom ${FIRE}${SUN} done`, reason_code: 477 })] }, 0)
    S.applyAgentEvents([{ seq: 1, t_sim_ns: 0, type: 'agent.health', level: 1, data: { aid: AID, health: `FS_ELAND ${SIREN}` } }], 0)
    S.flushAgents(1000)
    const s = S.agentsStore.getState()
    expect(hasForbidden(s.tasks[0].reason ?? '')).toBe(false)
    expect(s.tasks[0].reason).toBe('boom done')
    expect(s.agents.get(AID)!.health).toBe('FS_ELAND')
  })

  it('loads evidence on demand through R76 and marks chain verification', async () => {
    apiGet.mockResolvedValueOnce({ chains: [{ chain: AID, verified: true }], rows: [
      { chain: AID, seq: 0, id: 'sha256:aa', type: 'agent.task.submitted', t_sim_ns: 83_600_000_000, payload: { task_id: 'T-0001' } },
      { chain: AID, seq: 5, id: 'sha256:bb', type: 'agent.task.accepted', t_sim_ns: 154_600_000_000,
        payload: { task_id: 'T-0001', aid: AID, confidence: 0.9, note: SMILE } }] })
    const p = S.loadEvidence('T-0001', 0)
    S.flushAgents(0)
    expect(S.agentsStore.getState().evidence.get('T-0001')!.loading).toBe(true)
    await p
    S.flushAgents(10_000)
    const e = S.agentsStore.getState().evidence.get('T-0001')!
    expect(apiGet).toHaveBeenCalledWith('/api/agent-tasks/T-0001/evidence')
    expect(e.loading).toBe(false)
    expect(e.verified).toBe(true)
    expect(e.rows.map((r) => r.type)).toEqual(['agent.task.submitted', 'agent.task.accepted'])
    expect(e.rows[1].summary).toContain('confidence=0.9')
    expect(e.rows.every((r) => !hasForbidden(r.summary))).toBe(true)
    apiGet.mockRejectedValueOnce(new Error('503 agent-runtime'))
    await S.loadEvidence('T-0002', 20_000)
    S.flushAgents(30_000)
    expect(S.agentsStore.getState().evidence.get('T-0002')!.error).toContain('503')
  })

  it('maps runtime state events and R43 agents', async () => {
    S.applyAgentEvents([{ seq: 1, t_sim_ns: 0, type: 'agent.runtime.state', level: 2, data: { state: 'DEGRADED' } }], 0)
    S.flushAgents(0)
    expect(S.agentsStore.getState().runtimeState).toBe('DEGRADED')
    S.applyAgentEvents([{ seq: 2, t_sim_ns: 0, type: 'agent.runtime.state', level: 1, data: { state: 'SYNCING' } }], 300)
    S.flushAgents(300)
    expect(S.agentsStore.getState().runtimeState).toBe('STARTING')
    apiGet.mockResolvedValueOnce({ coordinator_aid: 'bafyreigcs', network: 'mock', items: [
      { aid: AID, aid_short: 'bafyrei…cy7bou', name: 'p600-b1', vehicle_id: 'p600-b1', network: 'mock', caps: ['thermal.imaging'],
        health: null, load: 0, trust: { verify_max: 2, auth: 1 }, current_task: null, manifest_sha256: 'x' }] })
    await S.loadAgents(600)
    S.flushAgents(10_000)
    const s = S.agentsStore.getState()
    expect(s.coordinatorAid).toBe('bafyreigcs')
    expect(s.agents.get(AID)!.aidShort).toBe('bafyrei…cy7bou')
    expect(S.selectAgents(s)[0].caps).toEqual(['thermal.imaging'])
    apiGet.mockRejectedValueOnce(new Error('503'))
    await S.loadAgents(20_000)
    S.flushAgents(40_000)
    expect(S.agentsStore.getState().runtimeState).toBe('OFFLINE')
  })

  it('provides the collaboration overlay selector for M06', () => {
    S.ingestTasksWire({ version: 1, t_sim_ns: 0, tasks: [task(1, 'working', { phase: 'executing' }), task(2, 'canceled')] }, 0)
    S.flushAgents(1000)
    const items = S.selectCollabOverlay(S.agentsStore.getState())
    expect(items).toHaveLength(1)
    expect(items[0].footprintRadiusM).toBeCloseTo(60 * Math.tan((25 * Math.PI) / 180), 3)
    expect(items[0].providerVehicle).toBe('p600-b1')
  })

  it('binds topics only while the panel is open', () => {
    const subs: string[] = []
    const offs: string[] = []
    let dataCb: ((m: DataMsg) => void) | null = null
    let evCb: ((b: readonly RtEvent[]) => void) | null = null
    apiGet.mockResolvedValue({ items: [] })
    const rt = {
      onEvents: (cb: (b: readonly RtEvent[]) => void) => { evCb = cb; return () => undefined },
      onData: (cb: (m: DataMsg) => void) => { dataCb = cb; return () => undefined },
      subscribe: (topic: string) => { subs.push(topic); return () => offs.push(topic) },
    }
    const b = S.bindAgents(rt, () => 0)
    expect(subs).toEqual([])
    b.setPanelOpen(true)
    expect(subs).toEqual(['agent/tasks', 'agent/*/status'])
    dataCb!({ topic: 'agent/tasks', channelId: 100, seq: 1, tSimMs: 0, data: { version: 7, t_sim_ns: 0, tasks: [task(9)] } })
    dataCb!({ topic: `agent/${AID}/status`, channelId: 101, seq: 1, tSimMs: 0, data: { aid: AID, vehicle_id: 'p600-b1', health: null,
      load: 1, current_task: 'T-0009', phase: 'enroute', lease_owner: 'AGENT', soc_pct: 255, trust: { verify_max: 2, auth: 1 }, t_sim_ns: 0 } })
    evCb!([{ seq: 1, t_sim_ns: 0, type: 'agent.task.state', level: 0, data: { task_id: 'T-0009', to: 'working' } }])
    S.flushAgents(1000)
    const s = S.agentsStore.getState()
    expect(s.tasks[0].taskId).toBe('T-0009')
    expect(s.agents.get(AID)!.socPct).toBeNull()
    expect(s.runtimeState).toBe('READY')
    b.setPanelOpen(false)
    expect(offs).toEqual(['agent/tasks', 'agent/*/status'])
    b.dispose()
  })
})
