// Agents store (M14 §8.3; M14-FR-061, FR-064, FR-072, FR-073; owner M14). Vanilla zustand store for the AGENTS panel
// (M15 JSX) and the 3D collaboration overlay (M06): agent rows, the task ring (<= 256), evidence loaded on demand from
// R76, runtime state and the attention count. Only the net binding writes it, in batches: ingestion fills module buffers
// and flushAgents() publishes at most every AGENTS_FLUSH_MS (250 ms, <= 4 writes/s, M15-AC-021).
// Wire sources: agent/{aid}/status (awr.agent.status.v1, 1 Hz), agent/tasks (awr.agent.tasks.v1, change driven + 1 Hz),
// the event channel agent.* (agent.runtime.state, agent.health, agent.task.*), R43 GET /api/agents (names, caps,
// network) and R76 GET /api/agent-tasks/{id}/evidence. Every text that comes from agents or the runtime (reason,
// message, evidence summary, names) goes through lib/sanitize before it is stored (AWR-03 §8.4 D1-AC-20 note 5).
// The two axes stay separate: TaskRow.state is the A2A task state, TaskRow.effect the effect status; verified is the
// single server-side verdict (OK and (verify_trust >= 2 or simulated)).
import { useStore } from 'zustand'
import { useShallow } from 'zustand/react/shallow'
import { createAwrStore } from '@/lib/createStore'
import { sanitizeText } from '@/lib/sanitize'
import { apiGet, apiPost } from '@/net/api'
import type { DataMsg, RtEvent } from '@/net/rt'

export type A2AState = 'submitted' | 'working' | 'input-required' | 'completed' | 'failed' | 'canceled' | 'rejected'
export type AwrPhase = 'queued' | 'lease' | 'enroute' | 'on_station' | 'executing' | 'returning'
export type EffectStatus = 'OK' | 'UNVERIFIED' | 'FAILED' | 'UNAVAILABLE'
export type RuntimeState = 'READY' | 'DEGRADED' | 'OFFLINE' | 'STARTING'

export interface AgentRow {
  aid: string; aidShort: string; name: string; vehicleId: string; network: 'mock' | 'anet'; caps: readonly string[]
  health: string | null; load: number; leaseOwner: string; socPct: number | null; trust: { verifyMax: number; auth: number }
  currentTask: string | null; phase: AwrPhase | null; tUpdateMs: number
}
export interface TaskEffect { status: EffectStatus; verifyTrust: number; simulated: boolean }
export interface TaskRow {
  taskId: string; capability: string; state: A2AState; phase: AwrPhase | null; alloc: string
  requesterAid: string; providerAid: string | null; providerVehicle: string | null
  effect: TaskEffect | null; verified: boolean; predicateOk: boolean | null; scopeOk: boolean | null
  reasonCode: number; reason: string | null; confClaim: number | null; confCurrent: number | null
  etaS: number | null; progress: number | null; targetEnuM: readonly [number, number, number] | null
  tSubmitMs: number; tUpdateMs: number
}
export interface EvidenceRow { chain: string; seq: number; id: string; type: string; tSimMs: number; summary: string }
export interface EvidenceState { rows: readonly EvidenceRow[]; verified: boolean; loading: boolean; error: string | null }
export interface CollabOverlayItem {
  taskId: string; targetEnuM: readonly [number, number, number]; confCurrent: number | null; providerVehicle: string | null
  phase: AwrPhase | null; footprintRadiusM: number | null; attention: boolean
}
export interface AgentsState {
  /** SYNCING shows as STARTING; STOPPING and unreachable show as OFFLINE */
  runtimeState: RuntimeState
  coordinatorAid: string | null
  network: 'mock' | 'anet' | null
  agents: ReadonlyMap<string, AgentRow>
  /** ring, newest first, <= TASK_RING */
  tasks: readonly TaskRow[]
  evidence: ReadonlyMap<string, EvidenceState>
  selectedTaskId: string | null
  attentionCount: number
  version: number
}
export interface TaskSubmit {
  capability: string; target_enu_m: readonly [number, number, number | null] | null; args?: Record<string, unknown>
  accept?: Record<string, unknown>; negative_scope?: Record<string, unknown> | null; strategy?: 'auction' | 'direct'
  provider_aid?: string | null; max_retries?: number; note?: string
}

export const AGENTS_FLUSH_MS = 250
export const TASK_RING = 256
export const EVIDENCE_MAX = 16
const ATTENTION: ReadonlySet<A2AState> = new Set(['failed', 'rejected', 'input-required'])
const TERMINAL: ReadonlySet<A2AState> = new Set(['completed', 'failed', 'canceled', 'rejected'])
/** observation footprint radius R_foot = alt_agl_m * tan(hfov / 2) (M14 §8.4); defaults of the S3 thermal task */
const FOOT_ALT_M = 60
const FOOT_HFOV_DEG = 50

export const agentsStore = createAwrStore<AgentsState>('agents', () => ({
  runtimeState: 'OFFLINE', coordinatorAid: null, network: null, agents: new Map(), tasks: [], evidence: new Map(),
  selectedTaskId: null, attentionCount: 0, version: 0,
}))

// ------------------------------------------------------------------ helpers
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null)
const clean = (v: unknown, max = 256): string | null => (typeof v === 'string' ? sanitizeText(v, max) : null)

export function aidShort(aid: string): string {
  return aid.length > 13 ? `${aid.slice(0, 7)}…${aid.slice(-6)}` : aid
}

function mapRuntimeState(s: unknown): RuntimeState {
  if (s === 'READY' || s === 'DEGRADED') return s
  if (s === 'STARTING' || s === 'SYNCING') return 'STARTING'
  return 'OFFLINE'
}

// ------------------------------------------------------------------ module buffers (no store write on ingestion)
const agentsBuf = new Map<string, AgentRow>()
const tasksBuf = new Map<string, TaskRow>()
const evidenceBuf = new Map<string, EvidenceState>()
let runtimeBuf: RuntimeState = 'OFFLINE'
let coordinatorBuf: string | null = null
let networkBuf: 'mock' | 'anet' | null = null
let selectedBuf: string | null = null
let lastFrameVersion = -1
let dirty = false
let lastFlush = -Infinity
let timer: ReturnType<typeof setTimeout> | null = null

function markDirty(nowMs: number): void {
  dirty = true
  if (timer !== null) return
  const wait = Math.max(0, lastFlush + AGENTS_FLUSH_MS - nowMs)
  timer = setTimeout(() => {
    timer = null
    flushAgents(performance.now())
  }, wait)
}

function emptyAgent(aid: string, nowMs: number): AgentRow {
  return { aid, aidShort: aidShort(aid), name: '', vehicleId: '', network: 'mock', caps: [], health: null, load: 0,
    leaseOwner: 'NONE', socPct: null, trust: { verifyMax: 2, auth: 1 }, currentTask: null, phase: null, tUpdateMs: nowMs }
}

// ------------------------------------------------------------------ wire -> rows
/** awr.agent.status.v1 (17 §6.6) -> AgentRow fields; soc_pct 255 means unknown. */
export function statusFromWire(w: Record<string, unknown>, prev: AgentRow | undefined, nowMs: number): AgentRow | null {
  const aid = str(w.aid)
  if (!aid) return null
  const base = prev ?? emptyAgent(aid, nowMs)
  const trust = (w.trust ?? {}) as Record<string, unknown>
  const soc = num(w.soc_pct)
  return {
    ...base, vehicleId: str(w.vehicle_id) ?? base.vehicleId, health: clean(w.health, 64), load: num(w.load) ?? 0,
    leaseOwner: str(w.lease_owner) ?? 'NONE', socPct: soc === null || soc === 255 ? null : soc,
    trust: { verifyMax: num(trust.verify_max) ?? base.trust.verifyMax, auth: num(trust.auth) ?? base.trust.auth },
    currentTask: str(w.current_task), phase: (str(w.phase) as AwrPhase | null) ?? null, tUpdateMs: nowMs,
  }
}

/** R43 item (AgentView) -> AgentRow */
export function agentFromView(v: Record<string, unknown>, prev: AgentRow | undefined, nowMs: number): AgentRow | null {
  const aid = str(v.aid)
  if (!aid) return null
  const base = prev ?? emptyAgent(aid, nowMs)
  const caps = Array.isArray(v.caps) ? v.caps.filter((c): c is string => typeof c === 'string').map((c) => sanitizeText(c, 64)) : base.caps
  const row = statusFromWire(v, base, nowMs) ?? base
  return { ...row, aidShort: clean(v.aid_short, 16) ?? aidShort(aid), name: clean(v.name, 64) ?? row.name,
    network: v.network === 'anet' ? 'anet' : 'mock', caps }
}

/** TaskStatusLite (awr.agent.tasks.v1 item, snake_case) -> TaskRow */
export function taskFromWire(w: Record<string, unknown>): TaskRow | null {
  const taskId = str(w.task_id)
  if (!taskId) return null
  const e = w.effect as Record<string, unknown> | null | undefined
  const tgt = Array.isArray(w.target_enu_m) && w.target_enu_m.length === 3
    ? [num(w.target_enu_m[0]) ?? 0, num(w.target_enu_m[1]) ?? 0, num(w.target_enu_m[2]) ?? 0] as const : null
  return {
    taskId: sanitizeText(taskId, 24), capability: sanitizeText(str(w.capability) ?? '', 64), state: (str(w.state) ?? 'submitted') as A2AState,
    phase: (str(w.phase) as AwrPhase | null) ?? null, alloc: str(w.alloc) ?? '', requesterAid: str(w.requester_aid) ?? '',
    providerAid: str(w.provider_aid), providerVehicle: clean(w.vehicle_id ?? w.provider_vehicle, 64),
    effect: e && typeof e.status === 'string'
      ? { status: e.status as EffectStatus, verifyTrust: num(e.verify_trust) ?? 0, simulated: e.simulated === true } : null,
    verified: w.verified === true, predicateOk: typeof w.predicate_ok === 'boolean' ? w.predicate_ok : null,
    scopeOk: typeof w.scope_ok === 'boolean' ? w.scope_ok : null, reasonCode: num(w.reason_code) ?? 0, reason: clean(w.reason, 120),
    confClaim: num(w.conf_claim), confCurrent: num(w.conf_current), etaS: num(w.eta_s), progress: num(w.progress),
    targetEnuM: tgt, tSubmitMs: Math.round((num(w.t_submit_ns) ?? 0) / 1e6), tUpdateMs: Math.round((num(w.t_update_ns) ?? 0) / 1e6),
  }
}

function summarize(type: string, p: Record<string, unknown>): string {
  const parts: string[] = []
  for (const k of ['to', 'aid', 'status', 'phase', 'reason_code', 'confidence', 'score', 'rank', 'pattern'] as const) {
    const v = p[k]
    if (v === undefined || v === null) continue
    const txt = typeof v === 'string' ? (v.length > 24 ? aidShort(v) : v) : typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(v)
    parts.push(`${k}=${txt}`)
  }
  if (Array.isArray(p.rows)) parts.push(`rows=${p.rows.length}`)
  if (Array.isArray(p.candidates)) parts.push(`candidates=${p.candidates.length}`)
  return sanitizeText(`${type} ${parts.join(' ')}`.trim(), 160)
}

export function evidenceFromWire(rows: readonly Record<string, unknown>[]): EvidenceRow[] {
  return rows.map((r) => ({
    chain: aidShort(str(r.chain) ?? ''), seq: num(r.seq) ?? 0, id: str(r.id) ?? '', type: sanitizeText(str(r.type) ?? '', 48),
    tSimMs: Math.round((num(r.t_sim_ns) ?? 0) / 1e6), summary: summarize(str(r.type) ?? '', (r.payload ?? {}) as Record<string, unknown>),
  }))
}

// ------------------------------------------------------------------ ingestion (net binding, tests)
/** agent/{aid}/status batch (already decoded) */
export function applyStatusBatch(rows: readonly AgentRow[], nowMs = performance.now()): void {
  for (const r of rows) agentsBuf.set(r.aid, { ...(agentsBuf.get(r.aid) ?? {}), ...r })
  if (rows.length) markDirty(nowMs)
}

export function ingestStatusWire(items: readonly Record<string, unknown>[], nowMs = performance.now()): number {
  let n = 0
  for (const w of items) {
    const row = statusFromWire(w, agentsBuf.get(String(w.aid)), nowMs)
    if (row) {
      agentsBuf.set(row.aid, row)
      n++
    }
  }
  if (n) markDirty(nowMs)
  return n
}

/** agent/tasks frame (self-contained latest): rows replace ring entries; older rows stay until the ring is full. */
export function applyTasksFrame(frame: { version: number; tasks: readonly TaskRow[] }, nowMs = performance.now()): void {
  if (frame.version === lastFrameVersion) return
  lastFrameVersion = frame.version
  for (const t of frame.tasks) tasksBuf.set(t.taskId, t)
  if (tasksBuf.size > TASK_RING) {
    const drop = [...tasksBuf.values()].sort((a, b) => Number(TERMINAL.has(b.state)) - Number(TERMINAL.has(a.state)) || a.tUpdateMs - b.tUpdateMs)
    for (const t of drop.slice(0, tasksBuf.size - TASK_RING)) tasksBuf.delete(t.taskId)
  }
  markDirty(nowMs)
}

export function ingestTasksWire(frame: Record<string, unknown>, nowMs = performance.now()): number {
  const items = Array.isArray(frame.tasks) ? frame.tasks : []
  const rows = items.map((w) => taskFromWire(w as Record<string, unknown>)).filter((t): t is TaskRow => t !== null)
  applyTasksFrame({ version: num(frame.version) ?? lastFrameVersion + 1, tasks: rows }, nowMs)
  return rows.length
}

/** event channel agent.* (and sim.reset) */
export function applyAgentEvents(events: readonly RtEvent[], nowMs = performance.now()): number {
  let n = 0
  for (const ev of events) {
    if (ev.type === 'sim.reset') {
      tasksBuf.clear()
      evidenceBuf.clear()
      n++
      continue
    }
    if (!ev.type.startsWith('agent.') && !ev.type.startsWith('anet.')) continue
    n++
    const d = ev.data ?? {}
    if (ev.type === 'agent.runtime.state') runtimeBuf = mapRuntimeState(d.state)
    else if (ev.type === 'agent.health') {
      const aid = str(d.aid)
      if (aid) agentsBuf.set(aid, { ...(agentsBuf.get(aid) ?? emptyAgent(aid, nowMs)), health: clean(d.health, 64), tUpdateMs: nowMs })
    } else if (ev.type === 'agent.registered') {
      const aid = str(d.aid)
      if (aid && !agentsBuf.has(aid)) agentsBuf.set(aid, { ...emptyAgent(aid, nowMs), vehicleId: str(d.vehicle_id) ?? '', name: clean(d.vehicle_id, 64) ?? '' })
      if (runtimeBuf === 'OFFLINE') runtimeBuf = 'READY'
    } else if (runtimeBuf === 'OFFLINE') runtimeBuf = 'READY'
  }
  if (n) markDirty(nowMs)
  return n
}

export function setRuntimeState(s: RuntimeState, nowMs = performance.now()): void {
  if (runtimeBuf !== s) {
    runtimeBuf = s
    markDirty(nowMs)
  }
}

export function selectTask(taskId: string | null, nowMs = performance.now()): void {
  selectedBuf = taskId
  markDirty(nowMs)
}

/** Publish buffered changes (one store write). */
export function flushAgents(nowMs: number): boolean {
  if (!dirty) return false
  dirty = false
  lastFlush = nowMs
  const tasks = [...tasksBuf.values()].sort((a, b) => b.tUpdateMs - a.tUpdateMs || (a.taskId < b.taskId ? 1 : -1))
  let attention = 0
  for (const t of tasks) if (ATTENTION.has(t.state)) attention++
  const s = agentsStore.getState()
  agentsStore.setState({
    runtimeState: runtimeBuf, coordinatorAid: coordinatorBuf, network: networkBuf, agents: new Map(agentsBuf), tasks,
    evidence: new Map(evidenceBuf), selectedTaskId: selectedBuf, attentionCount: attention, version: s.version + 1,
  })
  return true
}

/** Test helper and session reset. */
export function resetAgents(): void {
  agentsBuf.clear()
  tasksBuf.clear()
  evidenceBuf.clear()
  runtimeBuf = 'OFFLINE'
  coordinatorBuf = null
  networkBuf = null
  selectedBuf = null
  lastFrameVersion = -1
  dirty = false
  lastFlush = -Infinity
  if (timer !== null) clearTimeout(timer)
  timer = null
  agentsStore.setState({ runtimeState: 'OFFLINE', coordinatorAid: null, network: null, agents: new Map(), tasks: [], evidence: new Map(),
    selectedTaskId: null, attentionCount: 0, version: agentsStore.getState().version + 1 })
}

// ------------------------------------------------------------------ REST (R43, R45, R46, R76, R77)
/** R43: names, capabilities and network of every agent; 213 marks the runtime OFFLINE. */
export async function loadAgents(nowMs = performance.now()): Promise<void> {
  try {
    const r = await apiGet<{ coordinator_aid?: string; network?: string; items?: Record<string, unknown>[] }>('/api/agents')
    coordinatorBuf = str(r.coordinator_aid)
    networkBuf = r.network === 'anet' ? 'anet' : 'mock'
    for (const v of r.items ?? []) {
      const row = agentFromView(v, agentsBuf.get(String(v.aid)), nowMs)
      if (row) agentsBuf.set(row.aid, row)
    }
    if (runtimeBuf === 'OFFLINE') runtimeBuf = 'READY'
  } catch {
    runtimeBuf = 'OFFLINE'
  }
  markDirty(nowMs)
}

/** R76, on demand (Sheet open); results are sanitised; at most EVIDENCE_MAX tasks are cached. */
export async function loadEvidence(taskId: string, nowMs = performance.now()): Promise<void> {
  evidenceBuf.set(taskId, { rows: evidenceBuf.get(taskId)?.rows ?? [], verified: evidenceBuf.get(taskId)?.verified ?? false, loading: true, error: null })
  markDirty(nowMs)
  try {
    const r = await apiGet<{ chains?: { chain: string; verified: boolean }[]; rows?: Record<string, unknown>[] }>(
      `/api/agent-tasks/${encodeURIComponent(taskId)}/evidence`)
    const chains = r.chains ?? []
    evidenceBuf.set(taskId, { rows: evidenceFromWire(r.rows ?? []), verified: chains.length > 0 && chains.every((c) => c.verified === true),
      loading: false, error: null })
  } catch (e) {
    evidenceBuf.set(taskId, { rows: [], verified: false, loading: false, error: sanitizeText(e instanceof Error ? e.message : 'error', 120) })
  }
  while (evidenceBuf.size > EVIDENCE_MAX) evidenceBuf.delete(evidenceBuf.keys().next().value as string)
  markDirty(performance.now())
}

/** R45; returns the task id and the merge target (FR-018). */
export async function submitTask(spec: TaskSubmit): Promise<{ taskId: string; mergedInto: string | null }> {
  const r = await apiPost<{ task_id: string; state: string; merged_into: string | null }>('/api/agent-tasks', spec)
  return { taskId: r.task_id, mergedInto: r.merged_into }
}

/** R46 */
export async function cancelTask(taskId: string): Promise<void> {
  await apiPost(`/api/agent-tasks/${encodeURIComponent(taskId)}/cancel`, {})
}

/** R77; providerAid null means retry (only while the task is input-required). */
export async function assignTask(taskId: string, providerAid: string | null): Promise<void> {
  await apiPost(`/api/agent-tasks/${encodeURIComponent(taskId)}/assign`, { provider_aid: providerAid })
}

// ------------------------------------------------------------------ selectors and hooks
const agentsList = new WeakMap<ReadonlyMap<string, AgentRow>, readonly AgentRow[]>()
export function selectAgents(s: AgentsState): readonly AgentRow[] {
  let v = agentsList.get(s.agents)
  if (!v) {
    v = [...s.agents.values()].sort((a, b) => (a.vehicleId < b.vehicleId ? -1 : a.vehicleId > b.vehicleId ? 1 : 0))
    agentsList.set(s.agents, v)
  }
  return v
}

export function selectTasks(s: AgentsState, filter?: { state?: readonly A2AState[]; capability?: string }): readonly TaskRow[] {
  if (!filter || (!filter.state && !filter.capability)) return s.tasks
  return s.tasks.filter((t) => (!filter.state || filter.state.includes(t.state)) && (!filter.capability || t.capability === filter.capability))
}

const EMPTY_EVIDENCE: EvidenceState = { rows: [], verified: false, loading: false, error: null }

/** Read-only data for the M06 collaboration overlay (FR-073): target, confidence, delegation line, footprint circle. */
export function selectCollabOverlay(s: AgentsState): readonly CollabOverlayItem[] {
  const foot = FOOT_ALT_M * Math.tan((FOOT_HFOV_DEG * Math.PI) / 360)
  const out: CollabOverlayItem[] = []
  for (const t of s.tasks) {
    if (!t.targetEnuM || (TERMINAL.has(t.state) && t.state !== 'completed')) continue
    out.push({ taskId: t.taskId, targetEnuM: t.targetEnuM, confCurrent: t.confCurrent, providerVehicle: t.providerVehicle, phase: t.phase,
      footprintRadiusM: t.phase === 'executing' ? foot : null, attention: ATTENTION.has(t.state) })
  }
  return out
}

export function useAgentsState<T>(sel: (s: AgentsState) => T): T {
  return useStore(agentsStore, sel)
}
export const useAgents = (): readonly AgentRow[] => useStore(agentsStore, selectAgents)
export const useTasks = (filter?: { state?: A2AState[]; capability?: string }): readonly TaskRow[] =>
  useStore(agentsStore, useShallow((s) => selectTasks(s, filter)))
export const useTaskEvidence = (taskId: string): EvidenceState => useStore(agentsStore, (s) => s.evidence.get(taskId) ?? EMPTY_EVIDENCE)
export const useAgentSummary = (): { agents: number; active: number; attention: number; network: string; state: RuntimeState } =>
  useStore(agentsStore, useShallow((s) => ({
    agents: s.agents.size, active: s.tasks.reduce((n, t) => n + (TERMINAL.has(t.state) ? 0 : 1), 0), attention: s.attentionCount,
    network: s.network ?? 'mock', state: s.runtimeState,
  })))

// ------------------------------------------------------------------ net binding
/** What the binding needs from the realtime client (tests pass a stub). */
export interface AgentsSource {
  onEvents(cb: (b: readonly RtEvent[]) => void): () => void
  onData(cb: (m: DataMsg) => void): () => void
  subscribe(topic: string, o: { rate: number }): () => void
}

/**
 * Wire the page RtClient. The event channel (default subscription) is always consumed; while the AGENTS panel is open
 * the binding subscribes agent/tasks at 2 Hz and agent/+/status at 1 Hz and loads R43 once; closing unsubscribes.
 */
export function bindAgents(rt: AgentsSource, now: () => number = () => performance.now()): { setPanelOpen(open: boolean): void; dispose(): void } {
  let offTopics: (() => void)[] = []
  const offEv = rt.onEvents((b) => {
    applyAgentEvents(b, now())
  })
  const offData = rt.onData((m) => {
    if (m.topic === 'agent/tasks') {
      if (m.data && typeof m.data === 'object') ingestTasksWire(m.data as Record<string, unknown>, now())
    } else if (m.topic.startsWith('agent/') && m.topic.endsWith('/status')) {
      if (m.data && typeof m.data === 'object') ingestStatusWire([m.data as Record<string, unknown>], now())
    }
  })
  return {
    setPanelOpen(open: boolean): void {
      for (const off of offTopics) off()
      offTopics = []
      if (open) {
        offTopics = [rt.subscribe('agent/tasks', { rate: 2 }), rt.subscribe('agent/*/status', { rate: 1 })]
        void loadAgents(now())
      }
    },
    dispose(): void {
      for (const off of offTopics) off()
      offTopics = []
      offEv()
      offData()
    },
  }
}
