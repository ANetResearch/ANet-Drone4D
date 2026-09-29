// Command button state machine (M15-FR-040, FR-041, §6.6.4; AWR-14 §6.11, §11.4): every command started from the UI
// (button, hotkey, menu, command palette) is tracked under a key (usually `<vehicle>:<op>` or `fleet:<op>`), so any
// button showing that key renders the same phase: READY -> PENDING (same event handler, the click to "pending" path
// stays within one frame) -> ACCEPTED -> RUNNING -> DONE_OK or DONE_ERR, held INPUT.resultHoldMs before READY.
// Failures (rejected, failed, timeout) bump the shake counter once and raise one toast with the reason text (merge key
// cmd:<op>:<code>); canceled by a newer command returns to READY quietly. The call handle of M11 already re-sends an
// in-flight call with the same id after a reconnect (duplicate results), so the tracker only follows its results.
// Batch commands (`fleet/cmd/{op}`, selection >= 2) keep one aggregate toast updated from the first summary result,
// progress and fleet.batch.progress events (<= 4 Hz through the event bridge).
import { useSyncExternalStore } from 'react'
import { reasonText, t } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { INPUT } from '@/lib/tokens/input.gen'
import type { CallHandle, CallResult, CallStatus, RtEvent } from '@/net/rt'
import { onBridgeEvent } from '@/ui/notify/eventBridge'
import { opText } from '@/ui/notify/severity'

export type CmdPhase = 'READY' | 'PENDING' | 'ACCEPTED' | 'RUNNING' | 'DONE_OK' | 'DONE_ERR'
export interface CmdEntry {
  readonly key: string
  readonly op: string
  readonly phase: CmdPhase
  readonly status: CallStatus | 'sent' | null
  readonly code: number
  readonly reason: string | null
  readonly results: readonly CallStatus[]
  /** incremented once per failure (drives ShakeOnce) */
  readonly shake: number
  readonly callId: string | null
}
const READY_ENTRY = (key: string, op: string, shake = 0): CmdEntry => ({ key, op, phase: 'READY', status: null, code: 0, reason: null, results: [], shake, callId: null })

/** phase after a result (pure; exported for tests) */
export function nextPhase(phase: CmdPhase, status: CallStatus): CmdPhase {
  switch (status) {
    case 'accepted':
      return phase === 'RUNNING' ? 'RUNNING' : 'ACCEPTED'
    case 'running':
      return 'RUNNING'
    case 'succeeded':
      return 'DONE_OK'
    case 'canceled':
      return 'READY'
    default:
      return 'DONE_ERR'
  }
}
export const isBusy = (p: CmdPhase): boolean => p === 'PENDING' || p === 'ACCEPTED' || p === 'RUNNING'

const entries = new Map<string, CmdEntry>()
const listeners = new Set<() => void>()
const holdTimers = new Map<string, ReturnType<typeof setTimeout>>()
let version = 0
const emit = () => {
  version++
  for (const l of listeners) l()
}
function set(e: CmdEntry): void {
  entries.set(e.key, e)
  emit()
}

export function cmdEntry(key: string): CmdEntry | null {
  return entries.get(key) ?? null
}
/** React view of one key (re-renders only on tracker changes) */
export function useCmdEntry(key: string): CmdEntry | null {
  return useSyncExternalStore(subscribeCmds, () => entries.get(key) ?? null, () => null)
}
export function subscribeCmds(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
export const cmdsVersion = (): number => version

export interface TrackOptions {
  /** text of the command for toasts ("P600-01 takeoff") */
  label: string
  /** toast on success too (hotkeys and menus: the button is not in view) */
  toastSuccess?: boolean
}

/** follow a call started by the UI; `handle` null means the call could not be sent (no client, no vehicle) */
export function trackCall(key: string, op: string, handle: CallHandle | null, o: TrackOptions): CmdEntry {
  const prev = entries.get(key)
  const shake = prev?.shake ?? 0
  const h0 = holdTimers.get(key)
  if (h0) {
    clearTimeout(h0)
    holdTimers.delete(key)
  }
  if (!handle) {
    const e: CmdEntry = { ...READY_ENTRY(key, op, shake + 1), phase: 'DONE_ERR', status: 'rejected', code: 213, reason: 'SERVICE_UNAVAILABLE' }
    set(e)
    toastFailure(e, o.label)
    hold(key)
    return e
  }
  const results: CallStatus[] = []
  const e0: CmdEntry = { key, op, phase: 'PENDING', status: 'sent', code: 0, reason: null, results, shake, callId: handle.id }
  set(e0)
  handle.onResult((r: CallResult) => {
    const cur = entries.get(key)
    if (!cur || cur.callId !== handle.id) return // superseded by a newer call on the same key
    results.push(r.status)
    const phase = nextPhase(cur.phase, r.status)
    const failed = phase === 'DONE_ERR'
    const next: CmdEntry = { ...cur, phase, status: r.status, code: r.code, reason: r.reason ?? null, results: [...results], shake: failed ? cur.shake + 1 : cur.shake }
    set(next)
    if (failed) toastFailure(next, o.label, r.message)
    else if (phase === 'DONE_OK' && o.toastSuccess) notify(`cmd:${op}:ok`, 'success', t('command.toast.ok', { what: o.label }))
    if (phase === 'DONE_OK' || phase === 'DONE_ERR' || phase === 'READY') hold(key)
  })
  return e0
}

function hold(key: string): void {
  const h = setTimeout(() => {
    holdTimers.delete(key)
    const cur = entries.get(key)
    if (cur && !isBusy(cur.phase)) set(READY_ENTRY(key, cur.op, cur.shake))
  }, INPUT.resultHoldMs)
  holdTimers.set(key, h)
}

function toastFailure(e: CmdEntry, label: string, message?: string): void {
  const r = reasonText(e.code)
  const status = t(`command.state.${e.status ?? 'failed'}`)
  const title = t('command.toast.fail', { what: label, status })
  const desc = e.code > 0 ? t('command.toast.reason', { short: r.short, code: e.code, name: e.reason ?? '', remedy: r.remedy }) : (message ?? '')
  notify(`cmd:${e.op}:${e.code}`, 'warning', title, desc || undefined)
}

// ------------------------------------------------------------ batch commands (AWR-14 §6.11 batch rules; M15-FR-041)
export interface BatchState { op: string; n: number; accepted: number; rejected: number; running: number; ok: number; failed: number; byCode: Record<string, number>; final: boolean }
const batches = new Map<string, BatchState>() // call id -> state

function batchText(b: BatchState): { title: string; desc: string } {
  const what = opText(b.op)
  if (!b.final) return { title: t('batch.progress', { what, accepted: b.accepted, n: b.n, rejected: b.rejected }), desc: t('batch.counts', { running: b.running, ok: b.ok, failed: b.failed }) }
  const groups = Object.entries(b.byCode).map(([code, k]) => `${code} ${reasonText(Number(code)).short} × ${k}`).join(t('common.listSep'))
  return { title: t('batch.final', { what, ok: b.ok, n: b.n }), desc: groups || t('batch.counts', { running: b.running, ok: b.ok, failed: b.failed }) }
}
function batchToast(id: string, b: BatchState): void {
  const { title, desc } = batchText(b)
  notify(`batch:${id}`, b.failed + b.rejected > 0 && b.final ? 'warning' : 'info', title, desc)
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}
/** fold a summary result, a progress payload or a fleet.batch.progress event (exported for tests) */
export function foldBatch(b: BatchState, d: Record<string, unknown> | undefined, final = false): BatchState {
  if (!d) return b
  const counts = (d.counts ?? d) as Record<string, unknown>
  const byCode = (d.rejected_by_code ?? d.failed_by_code ?? null) as Record<string, number> | null
  return {
    ...b,
    accepted: num(d.accepted_n) || b.accepted,
    rejected: num(d.rejected_n) || b.rejected,
    running: 'running' in counts ? num(counts.running) : b.running,
    ok: 'succeeded' in counts ? num(counts.succeeded) : b.ok,
    failed: 'failed' in counts ? num(counts.failed) : b.failed,
    byCode: byCode ? { ...b.byCode, ...byCode } : b.byCode,
    final: final || b.final,
  }
}

/** follow a batch call (one aggregate toast); returns the tracker entry of the batch button */
export function trackBatch(key: string, op: string, n: number, handle: CallHandle | null, label: string): CmdEntry {
  const e = trackCall(key, op, handle, { label })
  if (!handle) return e
  let b: BatchState = { op, n, accepted: 0, rejected: 0, running: 0, ok: 0, failed: 0, byCode: {}, final: false }
  batches.set(handle.id, b)
  handle.onResult((r) => {
    b = foldBatch(b, r.data, r.final === true || r.status === 'succeeded' || r.status === 'failed')
    if (r.status === 'rejected' && !r.data) b = { ...b, rejected: n, final: true }
    batches.set(handle.id, b)
    batchToast(handle.id, b)
  })
  handle.onProgress((p) => {
    b = foldBatch(b, p.data as Record<string, unknown>)
    batches.set(handle.id, b)
    batchToast(handle.id, b)
  })
  return e
}

onBridgeEvent((ev: RtEvent) => {
  if (ev.type !== 'fleet.batch.progress') return
  const id = typeof ev.data?.batch_id === 'string' ? ev.data.batch_id : ev.cid
  if (!id) return
  const b = batches.get(id)
  if (!b) return
  const nb = foldBatch(b, ev.data, ev.data?.final === true)
  batches.set(id, nb)
  batchToast(id, nb)
})

export const batchState = (callId: string): BatchState | null => batches.get(callId) ?? null
