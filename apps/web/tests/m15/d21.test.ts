// D2.1 unit part of M15-AC-220 to AC-230 (AWR-04 §15.4 to §15.11; AWR-14 §20.26; D2-AC-40, 41, 43, 44, 45, 47, 48, 51, 52, 53):
// the build line and diagnostics, the five-segment writer, the capacity texts, the power-save state machine and its
// thresholds, the jitter filter, the form factor rule, the dock fit, the bottom-sheet snaps, the shell rects, the quick
// command state machine (<= 4 taps), the identity store (the pass phrase never stays anywhere), the sandbox park and
// resume of the session port, the parked surface, the field legend bands and the readout sentence.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { buildDisplay, buildShort, commitShort, diagnostics, type BuildInfo } from '@/lib/build'
import { FF, formFactorOf } from '@/lib/formFactor'
import { capacityLine, etaShort, queueSides, sandboxQueueHint } from '@/app/i18n/capacity'
import { ProgressWriter } from '@/app/boot/progress'
import { BootProgressModel } from '@/lib/bootProgress'
import { JitterFilter, POWER_DEFAULTS, PowerMachine, idleThresholdS, thresholdsFrom } from '@/app/power/machine'
import { QUICK_CLOSED, gotoAltitude, quickStep, type QuickModel } from '@/ui/mobile/quickModel'
import { dockFit, DOCK_PX } from '@/ui/mobile/DockPanel'
import { nearestSnap } from '@/ui/components/ui/bottom-sheet'
import { tabletRect } from '@/ui/mobile/TabletShell'
import { phoneRect } from '@/ui/mobile/MobileShell'
import { bandLabels } from '@/ui/field/FieldToolbar'
import { pairSentence } from '@/ui/panels/targets/QuantCard'
import type { CapacityView } from '@/net/sandbox/client'
import type { QuantFrame } from '@/stores/perception'
import { t } from '@/app/i18n'

function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return {
    get length() { return m.size },
    clear: () => m.clear(),
    getItem: (k: string) => m.get(k) ?? null,
    key: (i: number) => [...m.keys()][i] ?? null,
    removeItem: (k: string) => void m.delete(k),
    setItem: (k: string, v: string) => void m.set(k, v),
  }
}

const B: BuildInfo = { version: '0.2.0', channel: 'demo', commit: '3f9a1c2d4e', dirty: false, built_at: '2026-10-12T08:30:00Z', release: '20261012-083000-3f9a1c2d4e' }

describe('build information (D2-AC-40, M15-FR-223)', () => {
  it('formats the boot line like the vite plugin', () => {
    expect(buildDisplay(B)).toBe('V0.2.0-demo · 3f9a1c2 · 2026-10-12')
    expect(buildDisplay({ ...B, dirty: true })).toBe('V0.2.0-demo · 3f9a1c2-dirty · 2026-10-12')
    expect(buildDisplay({ ...B, commit: 'nogit' })).toBe('V0.2.0-demo · nogit · 2026-10-12')
    expect(buildShort(B)).toBe('V0.2.0-demo · 3f9a1c2')
    expect(commitShort({ ...B, dirty: true })).toBe('3f9a1c2-dirty')
  })
  it('the diagnostic text carries every build field including the release', () => {
    const d = diagnostics({ world: 'synthcity' }, B)
    for (const s of ['V0.2.0-demo', 'channel: demo', 'commit: 3f9a1c2d4e', 'built_at: 2026-10-12T08:30:00Z', 'release: 20261012-083000-3f9a1c2d4e', 'world: synthcity']) {
      expect(d).toContain(s)
    }
  })
})

describe('five-segment writer (D2-AC-41, M15-FR-224)', () => {
  function bar() {
    const segs = new Map<string, Map<string, string>>()
    const make = (seg: string) => {
      const props = new Map<string, string>()
      segs.set(seg, props)
      return { dataset: { seg }, style: { setProperty: (k: string, v: string) => props.set(k, v) } }
    }
    const els = ['bundle', 'world', 'points', 'warming', 'reveal'].map(make)
    const props = new Map<string, string>()
    const el = { style: { setProperty: (k: string, v: string) => props.set(k, v) }, querySelectorAll: () => els } as unknown as HTMLElement
    return { el, segs, props }
  }
  it('writes --seg-p per segment, monotone, and fills every segment at the reveal', () => {
    let now = 0
    const b = bar()
    const w = new ProgressWriter(b.el, null, new BootProgressModel(), () => now)
    w.model.report('bundle', 1)
    w.model.report('points', 0.5)
    now = 1000
    w.flush()
    expect(Number(b.segs.get('bundle')?.get('--seg-p'))).toBe(1)
    expect(Number(b.segs.get('points')?.get('--seg-p'))).toBeCloseTo(0.5, 4)
    expect(b.segs.get('world')?.get('--seg-p')).toBeUndefined()
    expect(Number(b.props.get('--boot-p'))).toBeCloseTo(0.15 + 0.2, 4)
    w.model.report('points', 0.3) // a smaller estimate never lowers a segment
    now = 2000
    w.flush()
    expect(w.segValue('points')).toBeCloseTo(0.5, 4)
    w.complete()
    for (const s of ['bundle', 'world', 'points', 'warming', 'reveal']) expect(Number(b.segs.get(s)?.get('--seg-p'))).toBe(1)
  })
})

const cap = (over: Partial<CapacityView> = {}): CapacityView => ({
  schema: 'awr.capacity.v1', t_unix_ns: '1', build: B, accepting: true, maintenance: false, degrade_level: 'L0',
  viewers: { live: 18, max: 24, queue: 0, eta_s: null, state: 'open' },
  sandbox: { active: 1, max: 2, reserved_internal: 1, parked: 0, queue: 0, eta_s: null, admit_now: true, worlds_admit_now: ['synthcity'], state: 'open' },
  power: { viewer_idle_s: 300, sandbox_idle_s: 600, hidden_s: 60, warn_s: 30 }, hint: 'open', ...over,
})

describe('capacity texts (D2-AC-41, D2-AC-42 text part, M15-FR-225)', () => {
  it('open, viewer full, sandbox full, both, closed and unavailable', () => {
    expect(capacityLine(cap())).toMatchObject({ kind: 'open', badge: null, text: '实时观看 18 / 24 · 沙盒 1 / 2 可用' })
    const v = capacityLine(cap({ viewers: { live: 24, max: 24, queue: 2, eta_s: 150, state: 'full' }, hint: 'queue_viewer' }))
    expect(v.kind).toBe('queueViewer')
    expect(v.text).toBe('实时观看已满（24 / 24）· 进入后排队，第 3 位，约 3 min')
    expect(v.badge).toBe('需排队')
    const s = capacityLine(cap({ sandbox: { active: 2, max: 2, reserved_internal: 1, parked: 3, queue: 3, eta_s: 360, admit_now: false, worlds_admit_now: [], state: 'queue' }, hint: 'queue_sandbox' }))
    expect(s).toMatchObject({ kind: 'queueSandbox', text: '沙盒 2 / 2 在用 · 排队 3 人 · 预计约 6 min', badge: '开启沙盒需排队' })
    expect(sandboxQueueHint(cap({ sandbox: { ...cap().sandbox, queue: 3, eta_s: 360, state: 'full' } }))).toBe('需排队 · 第 4 位 · 约 6 min')
    const both = capacityLine(cap({ viewers: { live: 24, max: 24, queue: 0, eta_s: null, state: 'full' }, sandbox: { ...cap().sandbox, state: 'full', queue: 1 } }))
    expect(both.kind).toBe('queueBoth')
    expect(both.text).toContain('暂无法估计')
    expect(capacityLine(cap({ maintenance: true, sandbox: { ...cap().sandbox, state: 'closed' } })).text).toBe('沙盒维护中，共享展示照常')
    expect(capacityLine(null)).toMatchObject({ kind: 'unavailable', text: '容量信息暂不可用' })
    expect(capacityLine(cap({ viewers: { live: null, max: null, queue: null, eta_s: null, state: 'unknown' } })).text).toContain('— / —')
  })
  it('rounds waits to < 1 min, n min and n h; null cannot be estimated', () => {
    expect(etaShort(10)).toBe('< 1 min')
    expect(etaShort(89)).toBe('1 min')
    expect(etaShort(3599)).toBe('60 min')
    expect(etaShort(7200)).toBe('2 h')
    expect(etaShort(null)).toBeNull()
    expect(queueSides(cap({ sandbox: { ...cap().sandbox, state: 'queue' } }))).toEqual({ viewer: false, sandbox: true })
  })
  it('marks an internal session next to the sandbox count', () => {
    expect(capacityLine(cap({ sandbox: { ...cap().sandbox, internal_active: 1 } })).text).toContain('沙盒 1 +1 内部 / 2')
  })
})

describe('power-save state machine (D2-AC-44, 45, M15-FR-227)', () => {
  const viewer = (m: PowerMachine) => {
    m.ctx = { role: 'viewer', busy: false, internal: false, keepRunning: false }
    m.th = { ...POWER_DEFAULTS, viewerIdleS: 20, warnS: 5, quietS: 8, hiddenS: 3 }
    return m
  }
  it('quiet at quiet_s, warning at idle - warn, saving at idle; any input cancels', () => {
    const m = viewer(new PowerMachine(0))
    expect(m.tick(7_000)).toBeNull()
    expect(m.tick(8_000)?.to).toBe('QUIET')
    expect(m.tick(14_000)).toBeNull()
    expect(m.tick(15_000)?.to).toBe('WARNING')
    expect(m.warnLeftS(15_000)).toBe(5)
    expect(m.input(16_000)?.to).toBe('ACTIVE')
    expect(m.tick(24_000)?.to).toBe('QUIET')
    expect(m.tick(31_000)?.to).toBe('WARNING')
    expect(m.tick(36_000)?.to).toBe('SAVING')
    expect(m.reason).toBe('idle')
    expect(m.tick(40_000)).toBeNull()
    expect(m.input(41_000)?.to).toBe('RESUMING')
    expect(m.live()?.to).toBe('ACTIVE')
  })
  it('hidden for hidden_s saves; pagehide saves at once; the server reclaim needs no request', () => {
    const m = viewer(new PowerMachine(0))
    m.visibility(false, 1000)
    expect(m.tick(3_999)).toBeNull()
    expect(m.tick(4_000)?.to).toBe('SAVING')
    expect(m.reason).toBe('hidden')
    const p = viewer(new PowerMachine(0))
    expect(p.pageHide()?.to).toBe('SAVING')
    const s = viewer(new PowerMachine(0))
    expect(s.serverReclaimed()).toMatchObject({ to: 'SAVING', reason: 'server' })
  })
  it('queues when the seats are full and reconnects on seat_ready', () => {
    const m = viewer(new PowerMachine(0))
    m.tick(30_000)
    m.input(31_000)
    expect(m.state).toBe('RESUMING')
    expect(m.queued()?.to).toBe('QUEUED')
    expect(m.tick(99_000)).toBeNull()
    expect(m.ready()?.to).toBe('RESUMING')
    expect(m.live()?.to).toBe('ACTIVE')
  })
  it('static browsing (role none) and keep-running never warn or save; busy and internal thresholds', () => {
    const m = new PowerMachine(0)
    m.th = { ...POWER_DEFAULTS, viewerIdleS: 20, warnS: 5, quietS: 8 }
    expect(m.tick(1e6)).toBeNull()
    expect(m.tick(2e6)).toBeNull()
    expect(m.state).toBe('ACTIVE')
    const k = viewer(new PowerMachine(0))
    k.ctx = { ...k.ctx, internal: true, keepRunning: true }
    k.tick(1e7)
    expect(k.state === 'SAVING' || k.state === 'WARNING').toBe(false)
    expect(k.inputAgeS(1e7)).toBe(0)
    expect(idleThresholdS(POWER_DEFAULTS, { role: 'sandbox', busy: true, internal: false, keepRunning: false })).toBe(300)
    expect(idleThresholdS(POWER_DEFAULTS, { role: 'viewer', busy: true, internal: false, keepRunning: false })).toBe(120)
    expect(idleThresholdS(POWER_DEFAULTS, { role: 'sandbox', busy: false, internal: true, keepRunning: false })).toBe(3600)
  })
  it('takes the server thresholds and ignores bad values', () => {
    const th = thresholdsFrom(POWER_DEFAULTS, { viewer_idle_s: 20, warn_s: 5, hidden_s: -1, sandbox_idle_s: 'x' })
    expect(th).toMatchObject({ viewerIdleS: 20, warnS: 5, hiddenS: 60, sandboxIdleS: 600 })
  })
  it('external API activity restarts the idle clock and cancels the warning; quiet follows the local input (issue #59)', () => {
    const sb = (m: PowerMachine) => {
      m.ctx = { role: 'sandbox', busy: false, internal: false, keepRunning: false }
      m.th = { ...POWER_DEFAULTS, sandboxIdleS: 20, warnS: 5, quietS: 8, hiddenS: 3 }
      return m
    }
    const m = sb(new PowerMachine(0))
    expect(m.activity(6_000, 6_000)).toBeNull()
    expect(m.tick(8_000)?.to).toBe('QUIET') // no local input for quiet_s: the frame cap still applies
    expect(m.tick(15_000)).toBeNull() // the idle clock runs from the activity at 6 s
    expect(m.tick(21_000)?.to).toBe('WARNING')
    expect(m.warnLeftS(21_000)).toBe(5)
    expect(m.activity(22_000, 22_500)).toMatchObject({ from: 'WARNING', to: 'QUIET', reason: 'activity' })
    expect(m.activity(20_000, 23_000)).toBeNull() // older than the latest: ignored
    expect(m.tick(36_000)).toBeNull()
    expect(m.tick(37_000)?.to).toBe('WARNING')
    expect(m.tick(42_000)?.to).toBe('SAVING')
    expect(m.reason).toBe('idle')
    expect(m.activity(43_000, 43_000)).toBeNull() // saving: only an input resumes
    expect(m.state).toBe('SAVING')
    const a = sb(new PowerMachine(0))
    a.th = { ...a.th, quietS: 30 }
    a.input(10_000)
    a.tick(25_000)
    expect(a.state).toBe('WARNING')
    expect(a.activity(26_000, 26_000)?.to).toBe('ACTIVE') // local input within quiet_s: back to ACTIVE
    const f = sb(new PowerMachine(0))
    expect(f.activity(99_000, 50_000)).toBeNull() // a future time is clamped to now
    expect(f.activityMs).toBe(50_000)
  })
  it('a hidden page saves only once the session saw no external API activity for hidden_s (issue #59)', () => {
    const m = new PowerMachine(0)
    m.ctx = { role: 'sandbox', busy: false, internal: false, keepRunning: false }
    m.th = { ...POWER_DEFAULTS, sandboxIdleS: 600, hiddenS: 3 }
    m.visibility(false, 1_000)
    m.activity(2_500, 2_500)
    expect(m.tick(4_000)).toBeNull()
    m.activity(5_000, 5_000)
    expect(m.tick(7_999)).toBeNull()
    expect(m.tick(8_000)).toMatchObject({ to: 'SAVING', reason: 'hidden' })
  })
  it('pointer jitter below 8 px accumulated is not an interaction', () => {
    const j = new JitterFilter()
    expect(j.move(100, 100, false)).toBe(false)
    expect(j.move(103, 100, false)).toBe(false)
    expect(j.move(106, 100, false)).toBe(false)
    expect(j.move(109, 100, false)).toBe(true)
    expect(j.move(110, 100, true)).toBe(true)
  })
})

describe('form factor and shell geometry (D2-AC-47, M15-FR-229 to FR-231)', () => {
  it('classifies the device matrix', () => {
    expect(formFactorOf(393, 852)).toBe('phone') // iPhone 15
    expect(formFactorOf(852, 393)).toBe('phone') // iPhone 15 landscape (short side < 600)
    expect(formFactorOf(412, 915)).toBe('phone') // Pixel 8
    expect(formFactorOf(834, 1194)).toBe('tablet') // iPad Pro 11 portrait
    expect(formFactorOf(1194, 834)).toBe('tablet') // iPad Pro 11 landscape
    expect(formFactorOf(820, 1180)).toBe('tablet') // iPad 10.9 portrait
    expect(formFactorOf(1366, 1024)).toBe('desktop') // iPad Pro 12.9 landscape (coarse desktop)
    expect(formFactorOf(1280, 720)).toBe('desktop')
    expect(formFactorOf(320, 640)).toBe('narrow')
    expect(FF.narrowW).toBe(360)
  })
  it('docks both sides only while the viewport keeps half of the width; the last one floats', () => {
    expect(dockFit({ left: 'docked', right: 'docked', last: 'right' }, 1600)).toEqual({ left: 'docked', right: 'docked' })
    expect(dockFit({ left: 'docked', right: 'docked', last: 'right' }, 1194)).toEqual({ left: 'docked', right: 'floating' })
    expect(dockFit({ left: 'docked', right: 'docked', last: 'left' }, 1194)).toEqual({ left: 'floating', right: 'docked' })
    expect(dockFit({ left: 'collapsed', right: 'docked', last: 'right' }, 1024)).toEqual({ left: 'collapsed', right: 'docked' })
  })
  it('snaps the drawer to 25 / 50 / 90 %', () => {
    expect(nearestSnap(0.31, [0.25, 0.5, 0.9])).toBe(0.25)
    expect(nearestSnap(0.41, [0.25, 0.5, 0.9])).toBe(0.5)
    expect(nearestSnap(0.8, [0.25, 0.5, 0.9])).toBe(0.9)
  })
  it('computes the unobscured rects of the tablet and phone shells', () => {
    const z = { top: 0, bottom: 0, left: 0, right: 0 }
    expect(tabletRect(1194, 834, false, 'docked', 'floating', 0.25, z)).toEqual({ x: DOCK_PX.left, y: 48, w: 1194 - DOCK_PX.left - DOCK_PX.right - 8, h: 834 - 48 - 64 })
    expect(tabletRect(1194, 834, false, 'docked', 'none', 0.25, z, 36)).toEqual({ x: DOCK_PX.left, y: 84, w: 1194 - DOCK_PX.left, h: 834 - 84 - 64 })
    expect(tabletRect(1194, 834, false, 'collapsed', 'docked', 0.25, z)).toEqual({ x: 48, y: 48, w: 1194 - 48 - DOCK_PX.right, h: 834 - 48 - 64 })
    expect(tabletRect(834, 1194, true, 'docked', 'docked', 0.25, z)).toEqual({ x: 0, y: 48, w: 834, h: 1194 * 0.75 - 48 })
    expect(phoneRect(393, 852, false, z)).toEqual({ x: 0, y: 36, w: 393, h: 852 - 36 - 56 })
    expect(phoneRect(852, 393, true, z)).toEqual({ x: 64, y: 36, w: 852 - 64, h: 393 - 36 })
  })
})

describe('quick commands (D2-AC-48, M15-FR-232)', () => {
  const run = (events: Parameters<typeof quickStep>[1][]): QuickModel => events.reduce(quickStep, QUICK_CLOSED)
  it('goto, hover, return and watch each take at most four taps from the commands button', () => {
    const sel = ['p600-01']
    const goto = run([{ type: 'open', selected: sel }, { type: 'op', op: 'goto' }, { type: 'point', p: [10, 20, 3] }])
    expect(goto.state).toBe('CONFIRM')
    expect(goto.point).toEqual([10, 20, 3])
    expect(quickStep(goto, { type: 'confirm' })).toMatchObject({ state: 'CLOSED', taps: 4 })
    const hover = run([{ type: 'open', selected: sel }, { type: 'op', op: 'hover' }, { type: 'confirm' }])
    expect(hover.taps).toBe(3)
    const rtl = run([{ type: 'open', selected: sel }, { type: 'op', op: 'rtl' }, { type: 'confirm' }])
    expect(rtl.taps).toBe(3)
    const watch = run([{ type: 'open', selected: sel }, { type: 'op', op: 'watch' }, { type: 'target', id: 'person-01', label: '人-01', pos: [5, 5, 0] }])
    expect(watch).toMatchObject({ state: 'CONFIRM', op: 'watch', target: { id: 'person-01' } })
    expect(quickStep(watch, { type: 'confirm' }).taps).toBe(4)
  })
  it('without a selection the vehicles come first; back steps back; invalid events change nothing', () => {
    const m = run([{ type: 'open', selected: [] }])
    expect(m.state).toBe('SELECT')
    expect(quickStep(m, { type: 'vehicles', vids: [] })).toBe(m)
    const a = quickStep(m, { type: 'vehicles', vids: ['a', 'b'] })
    expect(a).toMatchObject({ state: 'ACTION', vids: ['a', 'b'] })
    expect(quickStep(a, { type: 'confirm' })).toBe(a)
    const p = quickStep(a, { type: 'op', op: 'goto' })
    expect(quickStep(p, { type: 'back' }).state).toBe('ACTION')
    expect(quickStep(p, { type: 'close' })).toBe(QUICK_CLOSED)
  })
  it('goto keeps the current altitude when airborne, else 60 m above the point', () => {
    expect(gotoAltitude(10, 80)).toBe(80)
    expect(gotoAltitude(10, 12)).toBe(70)
    expect(gotoAltitude(10, null)).toBe(70)
  })
})

describe('field legend and readout (D2-AC-52, 53, M15-FR-237, FR-238)', () => {
  it('band labels from the lower edges', () => {
    expect(bandLabels([20, 30, 40, 50, 60])).toEqual(['< 30', '30–40', '40–50', '50–60', '≥ 60'])
    expect(bandLabels([0, 1, 2, 5, 10])).toEqual(['< 1', '1–2', '2–5', '5–10', '≥ 10'])
    expect(bandLabels([1, 2])).toEqual([])
  })
  it('the readout sentence follows AWR-04 §15.10.4', () => {
    const q: QuantFrame = {
      t_sim_ns: 0, target: 'person-01', criterion_mode: 'geometric',
      pairs: [{
        uav: 'P600-02', model_id: 'p600', range_m: 182, elev_deg: 20, az_rel_deg: 0, los: true, los_age_s: 0,
        acoustic: { lp_dba: 31.2, lp_band_db: 30.9, band_hz: 150, thr_dba: 33.0, l_bg_dba: 30, margin_db: -2.1, p: 0.26, in_body: false },
        visual: { theta_arcmin: 8.2, d_vis_m: 0.436, n_eq_px: 8.2, theta_th_arcmin: 1.0, ratio: 8.2, c_app: 0.3, light_band: 'day', lights_on: false, in_fov: true, p: 1, in_body: false },
        reverse: null, p_any: 1, would_discover: false, dwell_s: 0, t_dwell_s: 2,
      }],
    }
    const s = pairSentence(t, q, q.pairs[0], '人-01')
    expect(s).toBe('P600-02 → 人-01 · 斜距 182 m · 声学 30.9 dB(A) / 阈 33.0 · 余量 -2.1 dB · 概率 0.26 · 视觉 8.2′（8.2 px）/ 阈 1.0′ · 概率 1.00 · 判定：未满足（geometric：不在配置体内）')
  })
})

describe('identity store (D2-AC-51 front part, M15-FR-235, NFR-204)', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.stubGlobal('sessionStorage', memoryStorage())
    vi.stubGlobal('localStorage', memoryStorage())
  })
  afterEach(() => vi.unstubAllGlobals())
  // a random pass phrase generated by the test (the deployment's one is never in the repository)
  const secret = `pw-${Date.now().toString(36)}-${(performance.now() * 1000).toFixed(0)}`
  const token = 'v1.eyJ2IjoxfQ.c2ln'
  async function withServer(reply: { status: number; body: unknown; headers?: Record<string, string> }) {
    const m = await import('@/stores/identity')
    const seen: { method: string; body: string | null; ident: string | null }[] = []
    m.setIdentityFetcher(async (_u, init) => {
      seen.push({ method: init.method ?? 'GET', body: typeof init.body === 'string' ? init.body : null, ident: new Headers(init.headers).get('x-awr-identity') })
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: reply.headers })
    })
    return { m, seen }
  }
  it('ok keeps the token (sessionStorage), never the pass phrase', async () => {
    const { m, seen } = await withServer({ status: 200, body: { identity_token: token, exp_unix_ns: String(BigInt(Date.now() + 3600_000) * 1_000_000n), tier: 'internal' } })
    expect(await m.identity.verify(secret)).toBe('ok')
    expect(m.identityStore.getState()).toMatchObject({ tier: 'internal', token, phase: 'ok' })
    expect(m.identityToken()).toBe(token)
    expect(seen[0].body).toContain(secret) // only in the one request body
    expect(JSON.stringify(m.identityStore.getState())).not.toContain(secret)
    for (const st of [sessionStorage, localStorage]) for (let i = 0; i < st.length; i++) expect(st.getItem(st.key(i) as string)).not.toContain(secret)
    expect(sessionStorage.getItem(m.IDENTITY_KEY)).toContain(token)
    expect(localStorage.getItem(m.IDENTITY_KEY)).toBeNull()
    m.identity.setRemember(true)
    expect(localStorage.getItem(m.IDENTITY_KEY)).toContain(token)
    await m.identity.logout()
    expect(m.identityStore.getState()).toMatchObject({ tier: 'public', token: null })
    expect(sessionStorage.getItem(m.IDENTITY_KEY)).toBeNull()
    expect(localStorage.getItem(m.IDENTITY_KEY)).toBeNull()
    expect(seen.at(-1)).toMatchObject({ method: 'DELETE', ident: token })
  })
  it('maps 401 304, 429 516 with Retry-After, 429 111 and 403 115', async () => {
    let r = await withServer({ status: 401, body: { code: 304 } })
    expect(await r.m.identity.verify(secret)).toBe('wrong')
    r = await withServer({ status: 429, body: { code: 516, detail: { scope: 'prefix', retry_after_s: 1800 } }, headers: { 'retry-after': '1800' } })
    expect(await r.m.identity.verify(secret)).toBe('locked')
    expect(r.m.identityStore.getState().retryUntilMs - Date.now()).toBeGreaterThan(1790_000)
    r = await withServer({ status: 429, body: { code: 111 } })
    expect(await r.m.identity.verify(secret)).toBe('rate')
    r = await withServer({ status: 403, body: { code: 115, detail: { why: 'PUBLIC_READONLY' } } })
    expect(await r.m.identity.verify(secret)).toBe('disabled')
    expect(JSON.stringify(r.m.identityStore.getState())).not.toContain(secret)
  })
})

describe('sandbox park and resume of the session port (D2-AC-45 front part, M15-FR-226, FR-227)', () => {
  const SID = 'sb-k3m7q2x7ab'
  const future = (ms: number) => String(BigInt(Date.now() + ms) * 1_000_000n)
  const session = (state: string, extra: Record<string, unknown> = {}) => ({
    sid: SID, state, world_id: 'synthcity', template_id: 't5_area_guard', created_unix_ns: future(0), expires_unix_ns: future(1800_000), expires_in_s: 1800,
    renewable: true, clock: { state: 'PLAYING', rate_requested: 1, rate_granted: 1, rate_cap: 4, limited_by: null, tick_hz: 50 }, limits: {}, endpoints: { rest: '', rt: '' }, ...extra,
  })
  beforeEach(() => {
    vi.resetModules()
    vi.stubGlobal('sessionStorage', memoryStorage())
    vi.stubGlobal('localStorage', memoryStorage())
    sessionStorage.setItem('awr.sandbox.session.v1', JSON.stringify({ sid: SID, token: 'v1.tok.sig', token_exp_unix_ms: Date.now() + 1800_000, world: 'synthcity', template: 't5_area_guard' }))
  })
  afterEach(() => vi.unstubAllGlobals())
  async function port(handler: (method: string, url: string) => { status: number; body?: unknown }) {
    const m = await import('@/ui/sandbox/session')
    const calls: string[] = []
    m.setSandboxFetcher(async (url, init) => {
      calls.push(`${init.method ?? 'GET'} ${url.replace(/^.*\/api\/sandbox\/v1/, '')}`)
      const r = handler(init.method ?? 'GET', url)
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status })
    })
    await m.sandbox.resume(SID)
    return { m, calls }
  }
  it('park -> parked (stored token resumable), resume 201 adopts the fresh token', async () => {
    const { m, calls } = await port((method, url) => {
      if (method === 'GET' && url.endsWith(SID)) return { status: 200, body: session('ACTIVE') }
      if (url.endsWith('/park')) return { status: 202, body: { state: 'PARKING' } }
      if (url.endsWith('/resume')) return { status: 201, body: { session: session('SPAWNING'), token: 'v1.new.sig', token_exp_unix_ns: future(1800_000) } }
      return { status: 404 }
    })
    expect(m.sandboxStore.getState().phase).toBe('active')
    expect(await m.sandbox.park('idle')).toBe(true)
    expect(m.sandboxStore.getState().phase).toBe('parked')
    expect(JSON.parse(sessionStorage.getItem('awr.sandbox.session.v1') as string).parked_until_ms).toBeGreaterThan(Date.now() + 3500_000)
    expect(await m.sandbox.resumeParked()).toBe('resumed')
    expect(m.sandboxStore.getState()).toMatchObject({ phase: 'spawning', token: 'v1.new.sig', parked: null })
    expect(calls).toContain(`POST /sessions/${SID}/park`)
    expect(calls).toContain(`POST /sessions/${SID}/resume`)
  })
  it('a full pool answers 202 with a resume ticket; 410 ends the session (parked_expired)', async () => {
    const r = await port((method, url) => {
      if (method === 'GET' && url.endsWith(SID)) return { status: 200, body: session('PARKED', { t_sim_ns: '120000000000', park_reason: 'idle', retention_unix_ns: future(3600_000) }) }
      if (url.endsWith('/resume')) return { status: 202, body: { queue: { ticket_id: 'q-1', ticket_token: 'q1.x', position: 2, eta_s: 240, kind: 'resume' } } }
      if (url.includes('/queue/')) return { status: 200, body: { ticket_id: 'q-1', state: 'waiting', position: 2, eta_s: 240 } }
      return { status: 404 }
    })
    expect(r.m.sandboxStore.getState().phase).toBe('parked')
    expect(r.m.sandboxStore.getState().parked?.tSimNs).toBe('120000000000')
    expect(await r.m.sandbox.resumeParked()).toBe('queued')
    expect(r.m.sandboxStore.getState().queue).toMatchObject({ kind: 'resume', position: 2 })
    await r.m.sandbox.leaveQueue()
    expect(r.m.sandboxStore.getState().phase).toBe('parked')
    const e = await port((method, url) => {
      if (method === 'GET' && url.endsWith(SID)) return { status: 200, body: session('PARKED') }
      if (url.endsWith('/resume')) return { status: 410, body: { code: 506 } }
      return { status: 404 }
    })
    expect(await e.m.sandbox.resumeParked()).toBe('ended')
    expect(e.m.sandboxStore.getState()).toMatchObject({ phase: 'ended', endReason: 'parked_expired' })
  })
  it('the parked surface has no realtime link but stays the sandbox surface', async () => {
    const { resolveSurface } = await import('@/app/shell/surface')
    const route = { route: { id: 'sandbox' }, params: { sid: SID } }
    const now = Date.now()
    const parked = { sid: SID, token: 'v1.t.s', tokenExpUnixMs: now - 1000, phase: 'parked' as const, world: 'synthcity',
      parked: { reason: 'idle', resumeMode: null, parkedAtMs: now, retentionUntilMs: now + 60_000, tSimNs: null, remainingS: null } }
    expect(resolveSurface(route, parked, null, true, null, now)).toMatchObject({ surface: 'sandbox', rtPath: null })
    expect(resolveSurface(route, { ...parked, parked: { ...parked.parked, retentionUntilMs: now - 1 } }, null, true, null, now).surface).toBe('sandbox-gone')
    expect(resolveSurface(route, { ...parked, phase: 'active', tokenExpUnixMs: now + 60_000 }, null, true, null, now).rtPath).toContain('/rt')
  })
})
