// M15-FR-040, FR-041, FR-027 (AWR-14 §6.11, §6.7): command button phases, the tracker of a call (accepted, running,
// succeeded; rejections shake once and raise one reason toast; results of a superseded call are ignored), batch
// aggregation of summary results and progress, and the client-side admission pre-check from commands.json.
import { describe, expect, it, vi } from 'vitest'
import { FlightFlags, FlightState } from '@awr/contracts/enums'
import type { CallHandle, CallResult, Progress } from '@/net/rt'

// the first import of the viewport facade and three.js is slow on a loaded machine
vi.setConfig({ testTimeout: 30_000 })

/** a call handle driven by the test */
function fakeHandle(id: string) {
  const results: ((r: CallResult) => void)[] = []
  const progress: ((p: Progress) => void)[] = []
  let resolve: (r: CallResult) => void = () => {}
  const h: CallHandle = {
    id,
    result: new Promise<CallResult>((r) => {
      resolve = r
    }),
    onResult: (cb) => void results.push(cb),
    onProgress: (cb) => void progress.push(cb),
    cancel: () => {},
  }
  return {
    h,
    emit(status: CallResult['status'], extra: Partial<CallResult> = {}) {
      const r: CallResult = { id, status, code: 0, ...extra }
      for (const cb of results) cb(r)
      if (['succeeded', 'failed', 'rejected', 'timeout', 'canceled'].includes(status)) resolve(r)
    },
    progress(data: Record<string, unknown>) {
      for (const cb of progress) cb({ id, data })
    },
  }
}

describe('command phases', () => {
  it('follows AWR-14 §6.11', async () => {
    const { nextPhase, isBusy } = await import('@/ui/actions/commands')
    expect(nextPhase('PENDING', 'accepted')).toBe('ACCEPTED')
    expect(nextPhase('ACCEPTED', 'running')).toBe('RUNNING')
    expect(nextPhase('PENDING', 'running')).toBe('RUNNING')
    expect(nextPhase('RUNNING', 'accepted')).toBe('RUNNING')
    expect(nextPhase('RUNNING', 'succeeded')).toBe('DONE_OK')
    for (const s of ['rejected', 'failed', 'timeout'] as const) expect(nextPhase('PENDING', s)).toBe('DONE_ERR')
    expect(nextPhase('RUNNING', 'canceled')).toBe('READY')
    expect(isBusy('PENDING') && isBusy('ACCEPTED') && isBusy('RUNNING')).toBe(true)
    expect(isBusy('DONE_OK') || isBusy('READY')).toBe(false)
  })

  it('tracks one call and returns to READY after the hold time', async () => {
    vi.useFakeTimers()
    try {
      const { trackCall, cmdEntry } = await import('@/ui/actions/commands')
      const { INPUT } = await import('@/lib/tokens/input.gen')
      const f = fakeHandle('c1')
      trackCall('p600-01:takeoff', 'takeoff', f.h, { label: 'p600-01 takeoff' })
      expect(cmdEntry('p600-01:takeoff')?.phase).toBe('PENDING')
      f.emit('accepted')
      expect(cmdEntry('p600-01:takeoff')?.phase).toBe('ACCEPTED')
      f.emit('running')
      f.emit('succeeded')
      const e = cmdEntry('p600-01:takeoff')!
      expect(e.phase).toBe('DONE_OK')
      expect(e.results).toEqual(['accepted', 'running', 'succeeded'])
      expect(e.shake).toBe(0)
      vi.advanceTimersByTime(INPUT.resultHoldMs + 1)
      expect(cmdEntry('p600-01:takeoff')?.phase).toBe('READY')
    } finally {
      vi.useRealTimers()
    }
  })

  it('shakes once and toasts the reason on a rejection; a newer call on the key wins', async () => {
    const { trackCall, cmdEntry } = await import('@/ui/actions/commands')
    const { UX } = await import('@/ui/testing/uxProbe')
    const before = UX.toasts.merged['cmd:goto:102'] ?? 0
    const a = fakeHandle('c2')
    trackCall('p600-02:goto', 'goto', a.h, { label: 'p600-02 goto' })
    a.emit('rejected', { code: 102, reason: 'GEOFENCE_REJECT' })
    const e = cmdEntry('p600-02:goto')!
    expect(e.phase).toBe('DONE_ERR')
    expect(e.shake).toBe(1)
    expect(e.code).toBe(102)
    expect(UX.toasts.merged['cmd:goto:102']).toBe(before + 1)
    // a second call replaces the first; late results of the first are ignored
    const b = fakeHandle('c3')
    trackCall('p600-02:goto', 'goto', b.h, { label: 'p600-02 goto' })
    a.emit('running')
    expect(cmdEntry('p600-02:goto')?.phase).toBe('PENDING')
    b.emit('accepted')
    expect(cmdEntry('p600-02:goto')?.phase).toBe('ACCEPTED')
  })

  it('reports an unsendable call as a failure', async () => {
    const { trackCall, cmdEntry } = await import('@/ui/actions/commands')
    trackCall('p600-03:hover', 'hover', null, { label: 'x' })
    expect(cmdEntry('p600-03:hover')).toMatchObject({ phase: 'DONE_ERR', code: 213, shake: 1 })
  })
})

describe('batch aggregation', () => {
  it('folds summary results, progress payloads and final groups', async () => {
    const { foldBatch, trackBatch, batchState } = await import('@/ui/actions/commands')
    let b = { op: 'rtl', n: 1000, accepted: 0, rejected: 0, running: 0, ok: 0, failed: 0, byCode: {}, final: false }
    b = foldBatch(b, { accepted_n: 997, rejected_n: 3, rejected_by_code: { 105: 3 } })
    expect(b).toMatchObject({ accepted: 997, rejected: 3, byCode: { 105: 3 } })
    b = foldBatch(b, { counts: { running: 990, succeeded: 7, failed: 0 } })
    expect(b).toMatchObject({ running: 990, ok: 7, failed: 0, final: false })
    b = foldBatch(b, { counts: { running: 0, succeeded: 997, failed: 0 } }, true)
    expect(b).toMatchObject({ ok: 997, final: true })
    const f = fakeHandle('batch-1')
    trackBatch('fleet:rtl', 'rtl', 1000, f.h, 'rtl 1000')
    f.emit('accepted', { data: { accepted_n: 1000, rejected_n: 0 } })
    f.progress({ counts: { running: 1000, succeeded: 0, failed: 0 } })
    expect(batchState('batch-1')).toMatchObject({ accepted: 1000, running: 1000 })
  })
})

describe('admission pre-check', () => {
  it('greys GoTo on the ground and take-off in the air, with reasons', async () => {
    const { precheckState } = await import('@/ui/actions/admission')
    expect(precheckState('goto', FlightState.READY, 0)).toEqual({ ok: false, reasonKey: 'admit.airborneFirst' })
    expect(precheckState('goto', FlightState.FLYING, FlightFlags.IN_AIR).ok).toBe(true)
    expect(precheckState('takeoff', FlightState.READY, 0).ok).toBe(true)
    expect(precheckState('takeoff', FlightState.FLYING, FlightFlags.IN_AIR)).toMatchObject({ ok: false })
    expect(precheckState('land', FlightState.FLYING, FlightFlags.IN_AIR).ok).toBe(true)
    expect(precheckState('hover', FlightState.FLYING, FlightFlags.IN_AIR).ok).toBe(true)
    // unknown ops and out-of-range states pass: the server decides
    expect(precheckState('no-such-op', FlightState.READY, 0).ok).toBe(true)
    expect(precheckState('goto', 99, 0).ok).toBe(true)
  })
})
