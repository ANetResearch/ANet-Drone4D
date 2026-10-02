// FX-WEB2 (acceptance hardening of the UI panels): the reconstruction jobs store (R39 mapping, job.state / job.progress
// folding at <= 4 Hz, monotonic progress per attempt, REST unavailability), the Timeline track helpers (floor labels,
// lane geometry, overview range), the replay deep-link redirect, the Runs page helpers, the report fidelity block and
// footer (M16-FR-010, FR-011), the job log line formatter, the duration format and the brand name (ADR-056); the replay
// hover text from R68 (M12 §8.4: request window, cache by mseq per run and segment, the vehicle named once).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fmt } from '@/lib/format'
import { t } from '@/app/i18n'
import {
  JOBS_FLUSH_MS, applyJobEvent, flushJobs, ingestJobEvents, jobFromWire, jobs, jobsStore, resetJobs,
} from '@/stores/jobs'
import { pxCol, trackLane, trackTimeLabel } from '@/ui/lf/LfTimelineTrack'
import { describeColumn, overviewRange } from '@/ui/layout/TimelineTrackArea'
import { ingestReplayEvents, r68Path, replayEventText, REPLAY_HOVER_LIMIT } from '@/ui/layout/replayEventText'
import { timelineTrack } from '@/stores/timeline'
import { describeEvent, subStateText } from '@/ui/notify/severity'
import { rateLabel } from '@/ui/layout/timelineGuards'
import { replayRedirect } from '@/ui/views/replayFlow'
import { runDurationS, runState } from '@/ui/views/RunsPage'
import { fidelityRows, footerLines, metaSrcOf, type PerfReport } from '@/ui/views/Report'
import { jobIcon, jobStateKey, logLine } from '@/ui/views/JobsPage'

const wire = (over: Record<string, unknown> = {}) => ({
  job_id: 'j-01a0ed3d-5212-7fa7-982d-7bc5dd4430de', kind: 'recon', state: 'INFERRING', stage: 'INFERRING', progress_pct: 12.5,
  submitted_by: 'p-1', created_unix_ns: '1790686614081885383', updated_unix_ns: '1790686615081885383', target_world_id: 'shenzhen-recon-01',
  attempt: 1, resumable: false, scale_status: null, error: null,
  recon: { engine: 'mock', session_id: 's-1', source_world_id: 'shenzhen', target_world_id: 'shenzhen-recon-01', scale_status: null,
    alignment: null, frames: { done: null, total: 600 }, paused_reason: null, stage_durations_s: {} },
  ...over,
})

describe('jobs store (D1-AC-22 UI part)', () => {
  beforeEach(() => {
    resetJobs()
    vi.useFakeTimers()
  })
  afterEach(() => {
    resetJobs()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('maps an R39 item (snake_case, ns strings) to a row', () => {
    const r = jobFromWire(wire())
    expect(r).toMatchObject({ jobId: 'j-01a0ed3d-5212-7fa7-982d-7bc5dd4430de', kind: 'recon', state: 'INFERRING', progressPct: 12.5, engine: 'mock',
      sourceWorldId: 'shenzhen', targetWorldId: 'shenzhen-recon-01', framesTotal: 600, attempt: 1, resumable: false, outputWorldId: null })
    expect(r.createdMs).toBeCloseTo(1790686614081.885, 2)
    const f = jobFromWire(wire({ state: 'FAILED', error: { code: 339, name: 'GEOREF_REJECTED', stage: 'GEOREFERENCING', resumable: true, message: 'x' } }))
    expect(f.error).toMatchObject({ code: 339, name: 'GEOREF_REJECTED', stage: 'GEOREFERENCING', resumable: true })
    expect(f.resumable).toBe(true)
    const ok = jobFromWire(wire({ state: 'SUCCEEDED', progress_pct: 100, scale_status: 'gnss' }))
    expect(ok.outputWorldId).toBe('shenzhen-recon-01')
    expect(ok.scaleStatus).toBe('gnss')
  })

  it('keeps progress monotonic within an attempt and restarts it on a retry', () => {
    let r = jobFromWire(wire({ progress_pct: 40 }))
    r = applyJobEvent(r, 'job.progress', { job_id: r.jobId, progress_pct: 35, frames_done: 200, frames_total: 600, fps: 8.5, attempt: 1 }, 1)
    expect(r.progressPct).toBe(40)
    expect(r.framesDone).toBe(200)
    expect(r.fps).toBe(8.5)
    r = applyJobEvent(r, 'job.state', { job_id: r.jobId, state: 'QUEUED', progress_pct: 0, attempt: 2 }, 2)
    expect(r.progressPct).toBe(0)
    expect(r.attempt).toBe(2)
    r = applyJobEvent(r, 'job.state', { job_id: r.jobId, state: 'SUCCEEDED', attempt: 2, output: { world_id: 'w-1', scale_status: 'gnss', recon_session: 's-2' } }, 3)
    expect(r).toMatchObject({ state: 'SUCCEEDED', progressPct: 100, outputWorldId: 'w-1', scaleStatus: 'gnss', sessionId: 's-2', pausedReason: null })
  })

  it('shows perf_lock pauses and clears them on the next state', () => {
    let r = jobFromWire(wire())
    r = applyJobEvent(r, 'job.progress', { job_id: r.jobId, paused_reason: 'perf_lock' }, 1)
    expect(jobStateKey(r)).toBe('jobs.state.pausedPerf')
    expect(jobIcon(r)).toBe('mission.paused')
    r = applyJobEvent(r, 'job.progress', { job_id: r.jobId, paused_reason: null }, 2)
    expect(jobStateKey(r)).toBe('jobs.state.INFERRING')
  })

  it('writes the store at most every 250 ms however many events arrive (<= 4 Hz)', () => {
    const id = 'j-01a0ed3d-5212-7fa7-982d-7bc5dd4430de'
    ingestJobEvents([{ type: 'job.state', data: { job_id: id, kind: 'recon', state: 'QUEUED', target_world_id: 'x' } }])
    for (let i = 1; i <= 40; i++) {
      ingestJobEvents([{ type: 'job.progress', data: { job_id: id, progress_pct: i, state: 'INFERRING' } }, { type: 'sim.clock', data: {} }])
      vi.advanceTimersByTime(25)
    }
    vi.advanceTimersByTime(JOBS_FLUSH_MS)
    const s = jobsStore.getState()
    // 40 progress events over 1 s -> at most 4 flushes plus the trailing one
    expect(s.flushes).toBeLessThanOrEqual(5)
    expect(s.jobs).toHaveLength(1)
    expect(s.jobs[0].progressPct).toBe(40)
    expect(s.jobs[0].state).toBe('INFERRING')
  })

  it('reports an unwired or stopped job service instead of an empty queue', async () => {
    const { ApiError } = await import('@/net/api')
    const apiMod = await import('@/net/api')
    const spy = vi.spyOn(apiMod, 'apiGet')
    spy.mockRejectedValueOnce(new ApiError(503, 213, 'down'))
    await jobs.refresh()
    expect(jobsStore.getState()).toMatchObject({ status: 'unavailable', code: 213 })
    spy.mockRejectedValueOnce(new ApiError(404, 305, 'no route'))
    await jobs.refresh()
    expect(jobsStore.getState()).toMatchObject({ status: 'unavailable', code: 404 })
    spy.mockResolvedValueOnce({ items: [wire()] })
    await jobs.refresh()
    expect(jobsStore.getState()).toMatchObject({ status: 'ready', code: null })
    expect(jobsStore.getState().jobs).toHaveLength(1)
    spy.mockRestore()
    flushJobs()
  })
})

describe('timeline helpers', () => {
  it('labels floor ticks as m:ss, h:mm:ss and tenths below one second', () => {
    expect(trackTimeLabel(0, 5)).toBe('0:00')
    expect(trackTimeLabel(75, 5)).toBe('1:15')
    expect(trackTimeLabel(3725, 60)).toBe('1:02:05')
    expect(trackTimeLabel(12.5, 0.5)).toBe('0:12.5')
  })

  it('keeps markers above the floor and labels at the bottom of the lane', () => {
    const a = trackLane(28, true)
    expect(a.base).toBeLessThan(28 - 10)
    expect(a.cy).toBeLessThan(a.base)
    expect(a.labelY).toBeGreaterThan(a.base)
    const b = trackLane(56, true)
    expect(b.cy).toBeGreaterThan(a.cy)
  })

  it('keeps the live playhead, pinned to the right end, inside the canvas', () => {
    expect(pxCol(800, 800)).toBe(799.5)
    expect(pxCol(812.4, 800)).toBe(799.5)
    expect(pxCol(-3, 800)).toBe(0.5)
    expect(pxCol(400.4, 800)).toBe(400.5)
  })

  it('draws the whole recording in replay and at least one minute live', () => {
    expect(overviewRange('replay', 10, 150)).toEqual({ t0S: 10, t1S: 150 })
    expect(overviewRange('live', 0, 12)).toEqual({ t0S: 0, t1S: 60 })
    expect(overviewRange('live', 0, 612)).toEqual({ t0S: 0, t1S: 612 })
  })

  it('formats rate labels of both rate tables', () => {
    expect([0.1, 0.25, 0.5, 1, 2, 20].map(rateLabel)).toEqual(['×0.1', '×0.25', '×0.5', '×1', '×2', '×20'])
  })

  it('redirects the replay deep link to one-shot sandbox parameters and drops invalid values', () => {
    expect(replayRedirect({ id: 'shenzhen', run: 'r20260929-100000-f00d' }, new URLSearchParams('seg=2&t=420.5')))
      .toBe('/world/shenzhen?replay=r20260929-100000-f00d&seg=2&t=420.5')
    expect(replayRedirect({ id: 'shenzhen', run: 'r20260929-100000-f00d' }, new URLSearchParams('seg=x&t=-1')))
      .toBe('/world/shenzhen?replay=r20260929-100000-f00d')
  })
})

describe('replay hover text (M12 §8.4, R68)', () => {
  const RUN = 'r20260929-100000-f00d'
  afterEach(() => timelineTrack.clearMarkers())

  it('asks R68 for one column window of the open segment, at most 20 events', () => {
    const p = r68Path(RUN, 2, 41_999_999_999.4, 42_100_000_000.2)
    expect(p.startsWith(`/api/runs/${RUN}/events?`)).toBe(true)
    const q = new URLSearchParams(p.split('?')[1])
    expect(q.get('seg')).toBe('2')
    expect(q.get('from_ns')).toBe('41999999999')
    expect(q.get('to_ns')).toBe('42100000001')
    expect(q.get('limit')).toBe(String(REPLAY_HOVER_LIMIT))
    expect(REPLAY_HOVER_LIMIT).toBe(20)
  })

  it('caches the text by mseq for one run and segment and forgets it when either changes', () => {
    expect(ingestReplayEvents(RUN, 0, [
      { mseq: 7, type: 'cmd.succeeded', uav: 'p600-01', data: { op: 'goto' } },
      { mseq: 8, type: 'sim.rtf_limited', data: {} },
      { type: 'broken' },
    ])).toBe(2)
    expect(replayEventText(RUN, 0, 7)).toContain('p600-01')
    expect(replayEventText(RUN, 0, 8).length).toBeGreaterThan(0)
    expect(replayEventText(RUN, 1, 7)).toBe('')
    expect(replayEventText(null, 0, 7)).toBe('')
    ingestReplayEvents(RUN, 1, [{ mseq: 3, type: 'sim.rtf_limited', data: {} }])
    expect(replayEventText(RUN, 0, 7)).toBe('')
    expect(replayEventText(RUN, 1, 3).length).toBeGreaterThan(0)
  })

  it('shows the class at once, asks for the marker column, then shows the cached text with the vehicle named once', () => {
    const n = 2
    timelineTrack.loadEvx(n, Float64Array.from([10_000, 30_000]), Uint8Array.from([1, 2]), Uint8Array.from([3, 3]),
      Uint16Array.from([0xffff, 0xffff]), Uint32Array.from([11, 12]))
    const view = { t0S: 0, t1S: 60 }
    const h = describeColumn(500 * (30 / 60), 500, view, 'replay', { run: RUN, seg: 4 })
    expect(h.tS).toBeCloseTo(30, 6)
    expect(h.fetch?.mseq).toBe(12)
    expect(h.fetch!.t0S).toBeLessThanOrEqual(30)
    expect(h.fetch!.t1S).toBeGreaterThanOrEqual(30)
    expect(h.fetch!.t1S - h.fetch!.t0S).toBeLessThanOrEqual(60 / 500 + 1e-9)
    ingestReplayEvents(RUN, 4, [{ mseq: 12, type: 'cmd.failed', uav: 'p600-02', data: { op: 'goto', code: 0 } }])
    const h2 = describeColumn(250, 500, view, 'replay', { run: RUN, seg: 4 })
    expect(h2.fetch).toBeUndefined()
    const line = h2.lines[1]
    expect(line.split('p600-02').length - 1).toBe(1)
    // live mode never asks R68
    expect(describeColumn(250, 500, view, 'live').fetch).toBeUndefined()
  })
})

describe('flight sub-state text (FX-WEB2 screenshot review)', () => {
  it('names RTL, landing and hold sub-states in Chinese and keeps unknown names sanitised', () => {
    expect(subStateText('CRUISE')).toBe('巡航')
    expect(subStateText('TOUCHDOWN')).toBe('触地')
    expect(subStateText('SAFETY_STOP')).toBe('安全停止')
    expect(subStateText('X_NEW')).toBe('X_NEW')
    const text = describeEvent({ type: 'uav.state', uav: 'p600-02', data: { to: 'RTL/CRUISE' } })
    expect(text).toContain('巡航')
    expect(text).not.toContain('CRUISE')
  })
})

describe('runs page helpers', () => {
  const seg = (state: string, t0: number, t1: number) => ({ seg: 0, state, closed: state === 'CLOSED', t0_ns: t0 * 1e9, t1_ns: t1 * 1e9, bytes: 1, speed_max: 20, epochs: [] })
  it('sums segment durations and derives the run state', () => {
    expect(runDurationS({ segments: [seg('CLOSED', 0, 90), seg('CLOSED', 0, 60)] })).toBe(150)
    expect(runState({ segments: [seg('CLOSED', 0, 1), seg('OPEN', 0, 1)] })).toBe('OPEN')
    expect(runState({ segments: [seg('CORRUPT', 0, 1), seg('OPEN', 0, 1)] })).toBe('CORRUPT')
    expect(runState({ segments: [seg('CLOSED', 0, 1)] })).toBe('CLOSED')
  })

  it('formats durations for ETA and elapsed columns', () => {
    expect(fmt.dur(0)).toBe('0:00')
    expect(fmt.dur(245)).toBe('4:05')
    expect(fmt.dur(3727)).toBe('1:02:07')
    expect(fmt.dur(Number.NaN)).toBe('—')
  })
})

describe('report fidelity block and footer (M16-FR-010, FR-011)', () => {
  const report = {
    schema: 'awr.perf.report.v1', run_id: 'p20260929-120000-abcd', gate: 'G2', kind: 'suite', git: { sha: 'abc' },
    build: { mode: 'test', contracts: '1.0.0' }, env: { device_class: 'software', backend_tier: 'S' },
    cases: [{ id: 'flight60.pc.shenzhen', ac_ids: [], world_id: 'shenzhen', priority: 'P0', layer: 'core', status: 'PASS', metrics: [] }],
    summary: { p0_pass: 1, p0_total: 1 },
  } as unknown as PerfReport
  it('finds the sibling meta of a report source', () => {
    expect(metaSrcOf('/report.json')).toBe('/report.meta.json')
    expect(metaSrcOf('/runs/p1/report.json?x=1')).toBe('/runs/p1/report.meta.json')
  })

  it('states backend, profile status, simulated values, device class and the forced tier', () => {
    const rows = fidelityRows(report, { fidelity: { backend: 'Mock L1', vehicle: 'p600_mid360', vehicle_status: '参数未辨识', simulated: true } })
    const text = rows.map((r) => r.join(' ')).join('\n')
    expect(rows).toHaveLength(5)
    expect(text).toContain('Mock L1')
    expect(text).toContain('p600_mid360')
    expect(text).toContain('参数未辨识')
    expect(text).toContain('simulated')
    expect(text).toContain('software')
    expect(text).toContain(t('report.fid.forcedTest'))
  })

  it('writes data source, version, citation and research use from world.json, never a hand-written source', () => {
    const lines = footerLines(report, { datasets: { shenzhen: { name: 'UrbanScene3D', version: 'virtual_cities-sampled (GitHub Release v0.0.1)',
      citation: 'Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022', anchor: 'synthetic' } } })
    expect(lines[0]).toContain('UrbanScene3D')
    expect(lines[0]).toContain('Lin et al., ECCV 2022')
    expect(lines[0]).toContain('v0.0.1')
    expect(lines[0]).toContain('科研用途')
    expect(lines[1]).toContain('synthetic')
    expect(lines[2]).toBe('PERF · SHENZHEN · WEBGL2 SOFTWARE · run p20260929-120000-abcd')
    // defaults without meta still name the dataset and the fidelity
    expect(footerLines(report, null)[0]).toContain('UrbanScene3D')
  })
})

describe('job log lines and brand', () => {
  it('formats JSON log records and sanitises plain lines', () => {
    const l = logLine(JSON.stringify({ t_wall_ns: '1790686616293054579', job_id: 'j-x', level: 'info', msg: 'stage SEGMENTING start', seconds: 2.21 }))
    expect(l).toMatch(/^\d{2}:\d{2}:\d{2} INFO stage SEGMENTING start · seconds=2.21$/)
    expect(logLine('plain text')).toBe('plain text')
  })

  it('names the product ANet Drone4D (ADR-056) and keeps World Runtime as the runtime name', () => {
    expect(t('brand.product')).toBe('ANet Drone4D')
    expect(t('brand.runtime')).toBe('World Runtime')
    expect(t('about.version', { version: '0.1.0' })).toContain('ANet Drone4D')
    expect(t('about.copyright')).toContain('Agent Network Research')
  })
})
