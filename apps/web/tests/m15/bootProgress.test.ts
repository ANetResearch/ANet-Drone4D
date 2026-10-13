// M15-FR-201 (R-D2-01; AWR-04 §10.1; AWR-14 §20.3; UX-FR-101, UX-FR-102; M15-AC-201 unit part): five weighted segments,
// monotone per segment and in total, renormalised without the bundle segment (loading layer: 0.118 / 0.471 / 0.353 /
// 0.059), reduced motion jumps between segment boundaries, the reveal is 1, the writer throttles to <= 10 Hz and skips
// sub-pixel steps, the phase text follows the first incomplete segment (bytes on the points phase), the bundle fraction by
// bytes or by count, the floors of the gates and of the opened-world snapshot, and early reports reach the first sink.
import { describe, expect, it } from 'vitest'
import {
  BOOT_SEGS, BOOT_WEIGHTS, BootProgressModel, SWITCH_SEGS, applyReport, bundleFraction, onBootProgress, reportBootProgress, reportWarmPart,
  type BootReport,
} from '@/lib/bootProgress'
import { ProgressWriter, phaseText } from '@/app/boot/progress'
import { worldFloors } from '@/app/boot/progressSources'

describe('BootProgressModel', () => {
  it('weights sum to 1 and the segments are ordered as AWR-04 §10.1', () => {
    expect(BOOT_SEGS).toEqual(['bundle', 'world', 'points', 'warming', 'reveal'])
    expect(BOOT_SEGS.reduce((s, k) => s + BOOT_WEIGHTS[k], 0)).toBeCloseTo(1, 12)
    expect(BOOT_WEIGHTS).toEqual({ bundle: 0.15, world: 0.1, points: 0.4, warming: 0.3, reveal: 0.05 })
  })

  it('keeps every segment and the shown total monotone', () => {
    const m = new BootProgressModel()
    expect(m.report('points', 0.5)).toBe(true)
    expect(m.report('points', 0.3)).toBe(false)
    expect(m.f.points).toBe(0.5)
    m.shown = m.next()
    const before = m.shown
    m.report('points', 0.4)
    expect(m.next()).toBeGreaterThanOrEqual(before)
    expect(m.total()).toBeCloseTo(0.4 * 0.5, 12)
  })

  it('splits warming 0.6 shaders and 0.4 interface sub-phases', () => {
    const m = new BootProgressModel()
    m.reportWarm('shaders', 1)
    expect(m.f.warming).toBeCloseTo(0.6, 12)
    m.reportWarm('ui', 0.5)
    expect(m.f.warming).toBeCloseTo(0.8, 12)
  })

  it('renormalises without the bundle segment (loading layer)', () => {
    const m = new BootProgressModel(SWITCH_SEGS)
    m.report('world', 1)
    expect(m.total()).toBeCloseTo(0.1 / 0.85, 9)
    m.report('points', 1)
    m.report('warming', 1)
    expect(m.total()).toBeCloseTo(0.8 / 0.85, 9)
    expect(0.1 / 0.85).toBeCloseTo(0.118, 3)
    expect(0.4 / 0.85).toBeCloseTo(0.471, 3)
  })

  it('jumps between segment boundaries under reduced motion and reaches 1 on reveal', () => {
    const m = new BootProgressModel()
    m.report('bundle', 1)
    m.report('world', 0.5)
    expect(m.next(true)).toBeCloseTo(0.15, 12)
    m.report('reveal', 1)
    expect(m.next(true)).toBe(1)
    expect(m.next(false)).toBe(1)
  })

  it('names the first incomplete segment', () => {
    const m = new BootProgressModel()
    expect(m.firstIncomplete()).toBe('bundle')
    m.report('bundle', 1)
    m.report('world', 1)
    expect(m.firstIncomplete()).toBe('points')
    expect(phaseText(m)).toBe('加载首屏点云')
    m.bytes = { received: 1.4 * 1024 * 1024, total: 3.2 * 1024 * 1024 }
    expect(phaseText(m)).toBe('加载首屏点云 · 1.4 / 3.2 MB')
  })
})

describe('ProgressWriter', () => {
  function writer() {
    let now = 0
    const props = new Map<string, string>()
    const bar = { style: { setProperty: (k: string, v: string) => props.set(k, v) } } as unknown as HTMLElement
    const phase = { textContent: '' } as unknown as HTMLElement
    const w = new ProgressWriter(bar, phase, new BootProgressModel(), () => now)
    return { w, props, phase, setNow: (t: number) => { now = t } }
  }

  it('writes at most every 100 ms, never decreases and skips sub-pixel steps', () => {
    const { w, props, setNow } = writer()
    w.model.report('bundle', 1)
    setNow(1000)
    w.flush()
    expect(Number(props.get('--boot-p'))).toBeCloseTo(0.15, 4)
    w.model.report('world', 1)
    setNow(1050)
    w.flush() // inside the 100 ms window: deferred
    expect(Number(props.get('--boot-p'))).toBeCloseTo(0.15, 4)
    setNow(1200)
    w.flush()
    expect(Number(props.get('--boot-p'))).toBeCloseTo(0.25, 4)
    const n = w.samples.length
    w.model.report('points', 0.005) // 0.002 of the bar: below half a pixel
    setNow(1400)
    w.flush()
    expect(w.samples.length).toBe(n)
    for (let i = 1; i < w.samples.length; i++) expect(w.samples[i].p).toBeGreaterThanOrEqual(w.samples[i - 1].p)
    for (let i = 1; i < w.samples.length; i++) expect(w.samples[i].tMs - w.samples[i - 1].tMs).toBeGreaterThanOrEqual(100)
  })

  it('completes synchronously and freezes on error', () => {
    const { w, props, setNow } = writer()
    setNow(10)
    w.complete()
    expect(props.get('--boot-p')).toBe('1.0000')
    const { w: w2, props: p2 } = writer()
    w2.freeze()
    w2.model.report('bundle', 1)
    w2.flush()
    expect(p2.get('--boot-p')).toBeUndefined()
  })
})

describe('sources', () => {
  it('bundle fraction by manifest bytes, else by completed chunk count', () => {
    const done = new Set(['/assets/a.js'])
    expect(bundleFraction(done, ['/assets/a.js', '/assets/b.js'], null, 0)).toBe(0.5)
    const manifest = { chunks: [{ href: '/assets/a.js', bytes: 100 }, { href: '/assets/b.js', bytes: 300 }], total: 400 }
    expect(bundleFraction(done, [], manifest, 0)).toBe(0.25)
    expect(bundleFraction(new Set(), [], null, 0)).toBe(0)
  })

  it('maps the opened-world phases to world and points', () => {
    expect(worldFloors({ phase: 'manifest', progress: 0, firstScreenBytes: 0 })).toEqual({ world: 0.5, points: 0, bytes: null })
    expect(worldFloors({ phase: 'first_screen', progress: 0.25, firstScreenBytes: 4000 })).toEqual({ world: 1, points: 0.25, bytes: { received: 1000, total: 4000 } })
    expect(worldFloors({ phase: 'streaming', progress: 0.1, firstScreenBytes: 0 })).toEqual({ world: 1, points: 1, bytes: null })
    expect(worldFloors({ phase: 'idle', progress: 1, firstScreenBytes: 0 }).points).toBe(0)
  })

  it('replays reports made before the first sink, then delivers live', () => {
    reportBootProgress('world', 0.5)
    reportWarmPart('shaders', 1)
    const got: BootReport[] = []
    const off = onBootProgress((r) => got.push(r))
    expect(got.map((r) => r.seg ?? r.part)).toEqual(['world', 'shaders'])
    reportBootProgress('points', 0.2, { received: 1, total: 5 })
    expect(got[2].bytes).toEqual({ received: 1, total: 5 })
    const m = new BootProgressModel()
    for (const r of got) applyReport(m, r)
    expect(m.f.world).toBe(0.5)
    expect(m.f.points).toBe(0.2)
    expect(m.bytes).toEqual({ received: 1, total: 5 })
    off()
  })
})
