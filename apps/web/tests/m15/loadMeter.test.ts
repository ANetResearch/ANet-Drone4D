// v0.3.0 single loading bar (app/boot/loadMeter.ts): byte accounting against the boot manifest, the smoothed throughput
// from Resource Timing, the remaining-time estimate, the language-neutral number formats, and the readout (one monotone
// value, aria-valuenow/-valuetext, determinate after the first value).
import { describe, expect, it } from 'vitest'
import {
  ETA_MAX_S, KB, LoadMeter, LoadReadout, MB, RateMeter, formatBytes, formatDuration, formatRate, readoutTexts, stageOfSeg, wireBytes,
  type ResourceLike,
} from '@/app/boot/loadMeter'
import { t } from '@/app/i18n'

const res = (name: string, start: number, end: number, transfer: number, decoded = transfer): ResourceLike => ({
  name: `http://localhost${name}`, startTime: start, responseStart: start, responseEnd: end, transferSize: transfer, encodedBodySize: transfer, decodedBodySize: decoded,
})

describe('RateMeter', () => {
  it('bytes over the busy time of the recent transfers: parallel transfers are not counted twice, gaps do not dilute', () => {
    const r = new RateMeter(2000, 1000)
    r.add(MB, 0, 1000) // two parallel 1 MB transfers in the same second: 2 MB/s
    r.add(MB, 0, 1000)
    expect(r.raw(1000)).toBeCloseTo(2 * MB, 0)
    r.add(MB / 2, 1500, 2000) // after a 0.5 s gap, 0.5 MB in 0.5 s: busy 1.5 s for 2.5 MB
    expect(r.raw(2000)).toBeCloseTo((2.5 * MB) / 1.5, 0)
    expect(r.raw(4500)).toBeNull()
  })

  it('smooths exponentially, holds while transfers are pending and decays to zero when idle', () => {
    const r = new RateMeter(2000, 1000)
    r.add(MB, 0, 1000)
    expect(r.sample(1000)).toBeCloseTo(MB, 0) // the first sample starts at the raw value
    expect(r.sample(5000, true)).toBeCloseTo(MB, 0) // nothing finished, but the stage is still loading: held
    const a = r.sample(5500)
    expect(a).toBeLessThan(MB)
    expect(a).toBeGreaterThan(MB / 2)
    let v = a
    for (let t = 5750; t <= 16000; t += 250) v = r.sample(t)
    expect(v).toBe(0)
  })
})

describe('LoadMeter', () => {
  const manifest = { chunks: [{ href: '/assets/index.js', bytes: 400 * KB }, { href: '/assets/main.js', bytes: 1600 * KB }], total: 2000 * KB }

  it('counts manifest chunks by decoded size, ignores duplicates and treats cache hits as zero wire bytes', () => {
    const m = new LoadMeter(manifest)
    expect(m.take([res('/assets/index.js', 0, 100, 120 * KB, 400 * KB)])).toBe(true)
    expect(m.take([res('/assets/index.js', 0, 100, 120 * KB, 400 * KB)])).toBe(false)
    expect(m.codeLoaded()).toBe(400 * KB)
    expect(m.codeFraction()).toBeCloseTo(0.2, 9)
    expect(m.compression()).toBeCloseTo(0.3, 9)
    m.take([{ ...res('/world/a.bin', 0, 50, 0), transferSize: 0 }])
    expect(m.transferred).toBe(120 * KB)
    expect(wireBytes({ name: 'x', startTime: 0, responseEnd: 1, encodedBodySize: 10 })).toBe(10)
  })

  it('code stage: loaded / total of the manifest and a remaining time from the compressed remainder', () => {
    const m = new LoadMeter(manifest)
    m.take([res('/assets/index.js', 0, 1000, 120 * KB, 400 * KB)])
    const s = m.snapshot('code', 1000)
    expect(s.loaded).toBe(400 * KB)
    expect(s.total).toBe(2000 * KB)
    expect(s.rate).toBeCloseTo(120 * KB, -1)
    // the world stage counts on from the code: code plus first-screen bytes against code plus the first-screen total
    m.world = { received: MB, total: 4 * MB }
    expect(m.snapshot('world', 1000)).toMatchObject({ loaded: 2000 * KB + MB, total: 2000 * KB + 4 * MB })
    // remaining 1600 KB decoded x 0.3 = 480 KB on the wire at 120 KB/s
    expect(s.etaS).toBeCloseTo(4, 1)
  })

  it('world stage: the first-screen bytes; no total and no remaining time otherwise', () => {
    const m = new LoadMeter(null)
    m.take([res('/w/1.bin', 0, 1000, 2 * MB)])
    m.world = { received: 2 * MB, total: 10 * MB }
    const s = m.snapshot('world', 1000)
    expect(s).toMatchObject({ loaded: 2 * MB, total: 10 * MB })
    expect(s.etaS).toBeCloseTo(4, 1)
    const o = m.snapshot('other', 1000)
    expect(o.total).toBeNull()
    expect(o.etaS).toBeNull()
    expect(o.loaded).toBe(2 * MB) // resource sizes loaded so far
    expect(new LoadMeter(null).snapshot('code', 0)).toMatchObject({ total: null, etaS: null, rate: 0 })
  })

  it('suppresses an estimate beyond the meaningful range', () => {
    const m = new LoadMeter(null)
    m.take([res('/w/1.bin', 0, 1000, 2 * KB)])
    m.world = { received: 0, total: 100 * MB }
    expect(100 * MB / (2 * KB)).toBeGreaterThan(ETA_MAX_S)
    expect(m.snapshot('world', 1000).etaS).toBeNull()
  })
})

describe('formats', () => {
  it('bytes, rate and duration', () => {
    expect(formatBytes(3.24 * MB, 9.8 * MB)).toBe('3.2 / 9.8 MB')
    expect(formatBytes(0.5 * MB, null)).toBe('0.5 MB')
    expect(formatRate(0)).toBe('')
    expect(formatRate(640 * KB)).toBe('640 KB/s')
    expect(formatRate(1.25 * MB)).toBe('1.3 MB/s')
    expect(formatRate(24 * MB)).toBe('24 MB/s')
    expect(formatDuration(0.4)).toBe('1 s')
    expect(formatDuration(18.2)).toBe('18 s')
    expect(formatDuration(125)).toBe('2 min 05 s')
    expect(formatDuration(725)).toBe('12 min')
    expect(stageOfSeg('bundle')).toBe('code')
    expect(stageOfSeg('points')).toBe('world')
    expect(stageOfSeg('warming')).toBe('other')
    expect(stageOfSeg(null)).toBe('other')
  })
})

describe('LoadReadout', () => {
  function els() {
    const props = new Map<string, string>()
    const attrs = new Map<string, string>()
    const bar = { dataset: {} as Record<string, string>, getAttribute: (k: string) => attrs.get(k) ?? null, setAttribute: (k: string, v: string) => attrs.set(k, v) }
    const fill = { style: { setProperty: (k: string, v: string) => props.set(k, v) } }
    const text = () => ({ textContent: '' })
    return { props, attrs, bar, fill, percent: text(), bytes: text(), rate: text() }
  }

  it('is indeterminate until the first value, then monotone with aria and bilingual texts', () => {
    const e = els()
    let now = 0
    const meter = new LoadMeter({ chunks: [{ href: '/a.js', bytes: MB }], total: 2 * MB })
    const r = new LoadReadout(e as never, meter, () => now)
    expect(e.bar.dataset.state).toBeUndefined()
    expect(e.attrs.get('aria-valuenow')).toBeUndefined()
    meter.take([res('/a.js', 0, 1000, 512 * KB, MB)])
    now = 1000
    r.setTexts(readoutTexts(t))
    expect(e.attrs.get('aria-label')).toBe('载入进度')
    expect(e.attrs.get('aria-valuenow')).toBeUndefined()
    r.progress(0.075, 'code', '载入应用')
    expect(e.bar.dataset.state).toBe('determinate')
    expect(e.props.get('--boot-p')).toBe('0.0750')
    expect(e.percent.textContent).toBe('7%')
    expect(e.attrs.get('aria-valuenow')).toBe('7')
    expect(e.attrs.get('aria-valuetext')).toBe('已完成 7%，载入应用')
    expect(e.bytes.textContent).toBe('1.0 / 2.0 MB')
    // 512 KB/s; remaining 1 MB x 0.5 = 512 KB on the wire
    expect(e.rate.textContent).toBe('512 KB/s · 剩余约 1 s')
    r.progress(0.05) // never back
    expect(e.props.get('--boot-p')).toBe('0.0750')
    r.progress(1, 'other', '即将就绪')
    expect(e.percent.textContent).toBe('100%')
    expect(e.attrs.get('aria-valuenow')).toBe('100')
  })
})

describe('SegmentBar (power-save resume)', () => {
  it('one progressbar with the weighted total, no segment track', async () => {
    const React = await import('react')
    const { renderToStaticMarkup } = await import('react-dom/server')
    const { SegmentBar, segmentTotal } = await import('@/ui/boot/SegmentBar')
    const segs = [{ id: 'reconnect', weight: 0.3 }, { id: 'state', weight: 0.5 }, { id: 'reveal', weight: 0.2 }]
    // a later segment done before an earlier one only raises the total (no second bar)
    expect(segmentTotal(segs, { reconnect: 0, state: 1 })).toBeCloseTo(0.5, 9)
    const html = renderToStaticMarkup(React.createElement(SegmentBar, { segments: segs, values: { reconnect: 1, state: 0.5 }, label: 'resume' }))
    expect(html.match(/role="progressbar"/g)).toHaveLength(1)
    expect(html).toContain('aria-valuenow="55"')
    expect(html).toContain('aria-label="resume"')
    expect(html).not.toContain('data-seg=')
  })
})

describe('English readout texts', () => {
  it('formats the remaining time and the accessible value', async () => {
    const i18n = await import('@/app/i18n')
    i18n.setLocale('en')
    try {
      const x = readoutTexts(i18n.t)
      expect(x.label).toBe('Loading progress')
      expect(x.eta('18 s')).toBe('About 18 s remaining')
      expect(x.valueText(42, 'Loading the application')).toBe('42% complete, Loading the application')
    } finally {
      i18n.setLocale('zh-CN')
    }
  })
})
