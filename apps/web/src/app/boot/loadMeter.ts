// Load meter and the single loading bar readout (v0.3.0 boot bar rework; M15-FR-006, FR-201; replaces the five-segment
// track of D2.1, whose segments filled out of order: the shader warm-up of the warming segment ran while the points
// segment was still empty, so the mask showed one filled bar on the left and another on the right).
// One bar per loading surface, one fill: the total of the weighted boot model (lib/bootProgress, unchanged) drives the
// fill, the percent text and aria-valuenow, and never decreases. Next to it the readout shows what is being transferred:
//   * bytes (resource sizes, never decreasing on screen): while the code loads, loaded / total of the application code of
//     the build's boot manifest (window.__AWR_BOOT_MANIFEST, vite.config.ts bootManifest; decoded chunk sizes); while the
//     first screen of the world loads, code plus the first-screen point cloud bytes of the opened world (stores/world
//     firstScreenBytes x progress) against code plus the first-screen total; otherwise the bytes loaded so far;
//   * throughput (network bytes, cache hits count as zero): Resource Timing entries (PerformanceObserver 'resource');
//     the transfers that ended in the last RATE_LOOKBACK_MS give bytes over their busy time (the union of their response
//     intervals, so parallel downloads are not counted twice and idle gaps do not dilute the figure), smoothed
//     exponentially (time constant RATE_TAU_MS). Resource Timing only reports finished transfers, so while the stage still
//     has pending transfers the last figure is held; when the network is idle it decays to zero;
//   * the remaining time: remaining network bytes (code bytes scaled by the observed compression ratio) over the smoothed
//     rate, only while a total is known and the estimate is meaningful (ETA_MIN_S..ETA_MAX_S).
// No imports: the public demo entry (app/demo/entry.tsx) starts the meter and the mask readout before the application
// chunk has arrived (the landing chunk rule of entry.tsx), and the application continues the same page-wide instances
// (globalThis), so the bar hands over without a second bar and without going backwards.

export const MB = 1024 * 1024
export const KB = 1024
/** throughput look-back and smoothing time constant (about one second) */
export const RATE_LOOKBACK_MS = 2000
export const RATE_TAU_MS = 1000
/** shortest busy time a throughput figure is computed over (tiny responses would read as bursts) */
export const RATE_MIN_BUSY_MS = 50
/** readout refresh while a surface is shown (4 Hz) */
export const READOUT_TICK_MS = 250
/** the remaining time is shown between these bounds, and only above the smallest meaningful rate */
export const ETA_MIN_S = 1
export const ETA_MAX_S = 3600
export const RATE_MIN_BPS = 1024
/** weight of the application code in the boot model (lib/bootProgress BOOT_WEIGHTS.bundle) */
export const CODE_WEIGHT = 0.15

/** what the bytes of the readout refer to */
export type MeterStage = 'code' | 'world' | 'other'

export interface BootManifestLike { chunks: readonly { href: string; bytes: number }[]; total: number }

/** the fields of a PerformanceResourceTiming entry the meter reads */
export interface ResourceLike {
  name: string
  startTime: number
  responseStart?: number
  responseEnd: number
  transferSize?: number
  encodedBodySize?: number
  decodedBodySize?: number
}

export interface MeterSnapshot {
  stage: MeterStage
  /** loaded bytes (resource sizes) of the current stage */
  loaded: number
  /** the stage total, null when unknown */
  total: number | null
  /** smoothed network throughput, bytes per second */
  rate: number
  /** remaining time in seconds, null when not meaningful */
  etaS: number | null
}

const clamp01 = (x: number): number => (Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0)

export function pathOfHref(href: string): string {
  try {
    return new URL(href, typeof location !== 'undefined' ? location.href : 'http://localhost/').pathname
  } catch {
    return href
  }
}

/** bytes that crossed the network for one entry (0 for a cache hit; encodedBodySize where transferSize is not exposed) */
export function wireBytes(e: ResourceLike): number {
  if (typeof e.transferSize === 'number') return e.transferSize > 0 ? e.transferSize : 0
  return typeof e.encodedBodySize === 'number' && e.encodedBodySize > 0 ? e.encodedBodySize : 0
}

/**
 * Smoothed throughput: the transfers that ended in the last `lookbackMs` divided by their busy time (the union of their
 * response intervals); followed exponentially with time constant `tauMs`. Without a recent transfer the figure is held
 * while `busy` (transfers are pending, Resource Timing reports them only when they finish) and decays to zero otherwise.
 */
export class RateMeter {
  private spans: { s: number; e: number; b: number }[] = []
  private ema = 0
  private lastMs: number | null = null

  constructor(readonly lookbackMs = RATE_LOOKBACK_MS, readonly tauMs = RATE_TAU_MS) {}

  add(bytes: number, startMs: number, endMs: number): void {
    if (!(bytes > 0) || !Number.isFinite(endMs)) return
    const s = Number.isFinite(startMs) && startMs < endMs ? startMs : endMs - 1
    this.spans.push({ s, e: endMs, b: bytes })
  }

  /** bytes per second over the busy time of the transfers that ended in (now - lookback, now]; null when none did */
  raw(nowMs: number): number | null {
    const from = nowMs - this.lookbackMs
    const xs = this.spans.filter((x) => x.e > from && x.e <= nowMs).sort((a, b) => a.s - b.s)
    if (xs.length === 0) return null
    let bytes = 0
    let busy = 0
    let cs = xs[0].s
    let ce = xs[0].e
    for (const x of xs) {
      bytes += x.b
      if (x.s > ce) {
        busy += ce - cs
        cs = x.s
        ce = x.e
      } else ce = Math.max(ce, x.e)
    }
    busy += ce - cs
    return (bytes * 1000) / Math.max(busy, RATE_MIN_BUSY_MS)
  }

  /** advance the smoothing to `nowMs` and return the shown rate (bytes per second) */
  sample(nowMs: number, busy = false): number {
    const r = this.raw(nowMs)
    const target = r ?? (busy ? this.ema : 0)
    if (this.lastMs === null) this.ema = target
    else {
      const dt = Math.max(0, nowMs - this.lastMs)
      this.ema += (target - this.ema) * (1 - Math.exp(-dt / this.tauMs))
    }
    this.lastMs = nowMs
    // idle and below the smallest shown rate: zero (the exponential tail would never reach it)
    if (target === 0 && this.ema < RATE_MIN_BPS) this.ema = 0
    const keepFrom = nowMs - this.lookbackMs * 2
    if (this.spans.length > 0 && this.spans[0].e < keepFrom) this.spans = this.spans.filter((x) => x.e >= keepFrom)
    return this.ema < 1 ? 0 : this.ema
  }

  get rate(): number {
    return this.ema < 1 ? 0 : this.ema
  }
}

/** Byte accounting of one page: application code against the boot manifest, all transfers, and the world bytes. */
export class LoadMeter {
  readonly rate: RateMeter
  private readonly codeBytes = new Map<string, number>()
  private readonly codeDone = new Set<string>()
  private readonly seen = new Set<string>()
  private codeWire = 0
  private codeDecoded = 0
  /** all bytes that crossed the network since the meter started */
  transferred = 0
  /** loaded resource sizes (decoded body sizes; the network bytes where a size is not exposed) */
  loadedBytes = 0
  /** first-screen point cloud bytes of the opened world */
  world: { received: number; total: number } | null = null

  constructor(readonly manifest: BootManifestLike | null, rate = new RateMeter()) {
    this.rate = rate
    if (manifest) for (const c of manifest.chunks) this.codeBytes.set(pathOfHref(c.href), c.bytes)
  }

  /** take completed resource entries (duplicates are ignored); true when something new arrived */
  take(list: readonly ResourceLike[]): boolean {
    let grew = false
    for (const e of list) {
      const key = `${e.name}@${e.startTime}`
      if (this.seen.has(key)) continue
      this.seen.add(key)
      const wire = wireBytes(e)
      this.transferred += wire
      this.loadedBytes += typeof e.decodedBodySize === 'number' && e.decodedBodySize > 0 ? e.decodedBodySize : wire
      this.rate.add(wire, typeof e.responseStart === 'number' && e.responseStart > 0 ? e.responseStart : e.startTime, e.responseEnd)
      const path = pathOfHref(e.name)
      if (this.codeBytes.has(path) && !this.codeDone.has(path)) {
        this.codeDone.add(path)
        const decoded = typeof e.decodedBodySize === 'number' && e.decodedBodySize > 0 ? e.decodedBodySize : 0
        if (wire > 0 && decoded > 0) {
          this.codeWire += wire
          this.codeDecoded += decoded
        }
      }
      grew = true
    }
    if (this.seen.size > 4096) this.seen.clear() // long sessions: the keys only guard against replays of recent entries
    return grew
  }

  /** decoded code bytes of the completed manifest chunks */
  codeLoaded(): number {
    let b = 0
    for (const p of this.codeDone) b += this.codeBytes.get(p) ?? 0
    return b
  }

  /** fraction of the application code (0..1), null without a manifest */
  codeFraction(): number | null {
    if (!this.manifest || !(this.manifest.total > 0)) return null
    return clamp01(this.codeLoaded() / this.manifest.total)
  }

  /** wire bytes per decoded byte of the code seen so far (1 until known) */
  compression(): number {
    return this.codeDecoded > 0 ? Math.min(1, this.codeWire / this.codeDecoded) : 1
  }

  /** `withCode`: the world stage counts on from the application code (the boot mask; a world switch card does not) */
  snapshot(stage: MeterStage, nowMs: number, withCode = true): MeterSnapshot {
    const code = this.manifest && this.manifest.total > 0 ? this.manifest.total : null
    const base = withCode ? (code ?? 0) : 0
    let loaded = this.loadedBytes
    let total: number | null = null
    let remainingWire: number | null = null
    let busy = stage === 'world'
    if (stage === 'code' && code !== null) {
      loaded = Math.min(this.codeLoaded(), code)
      total = code
      remainingWire = (total - loaded) * this.compression()
      busy = loaded < total
    } else if (stage === 'world' && this.world && this.world.total > 0) {
      const received = Math.min(Math.max(0, this.world.received), this.world.total)
      loaded = base + received
      total = base + this.world.total
      remainingWire = this.world.total - received
      busy = remainingWire > 0
    }
    const rate = this.rate.sample(nowMs, busy)
    let etaS: number | null = null
    if (remainingWire !== null && remainingWire > 0 && rate >= RATE_MIN_BPS) {
      const e = remainingWire / rate
      if (e >= ETA_MIN_S && e <= ETA_MAX_S) etaS = e
    }
    return { stage, loaded, total, rate, etaS }
  }
}

// ------------------------------------------------------------ formatting (language-neutral parts)
/** "3.2 / 9.8 MB" or "3.2 MB" (tabular figures come from the CSS) */
export function formatBytes(loaded: number, total: number | null): string {
  const f = (b: number): string => (Math.max(0, b) / MB).toFixed(1)
  return total !== null ? `${f(loaded)} / ${f(total)} MB` : `${f(loaded)} MB`
}

/** "640 KB/s", "1.2 MB/s"; empty below the smallest meaningful rate */
export function formatRate(bps: number): string {
  if (!(bps >= RATE_MIN_BPS)) return ''
  if (bps < MB) return `${Math.round(bps / KB)} KB/s`
  return `${(bps / MB).toFixed(bps < 10 * MB ? 1 : 0)} MB/s`
}

/** "18 s", "2 min 05 s", "12 min" */
export function formatDuration(s: number): string {
  const r = Math.max(1, Math.round(s))
  if (r < 60) return `${r} s`
  const m = Math.floor(r / 60)
  const sec = r % 60
  return m >= 10 ? `${m} min` : `${m} min ${String(sec).padStart(2, '0')} s`
}

// ------------------------------------------------------------ the readout of one bar
/** texts of the readout (the dictionary is loaded after the mask is painted; until then the readout has no words) */
export interface ReadoutTexts {
  /** aria-label of the progress bar */
  label: string
  /** "about 18 s remaining" */
  eta: (duration: string) => string
  /** aria-valuetext: "42 % complete, loading the application" */
  valueText: (percent: number, stage: string) => string
}

/** the elements of one bar surface (any may be missing) */
export interface ReadoutEls {
  /** the progressbar element (role, aria-*, data-state) */
  bar: HTMLElement | null
  /** the fill (`--boot-p`) */
  fill: HTMLElement | null
  percent: HTMLElement | null
  bytes: HTMLElement | null
  rate: HTMLElement | null
}

type Timer = ReturnType<typeof setInterval>

/**
 * The readout of one loading bar: the fill, the percent, aria-valuenow/-valuetext, the bytes, the throughput and the
 * remaining time. `progress(p)` is monotone (a smaller value is ignored), `tick()` refreshes the network figures and runs
 * every READOUT_TICK_MS while started. Writes only changed text.
 */
export class LoadReadout {
  shown = 0
  determinate = false
  stage: MeterStage = 'code'
  stageText = ''
  texts: ReadoutTexts | null = null
  /** the world stage counts on from the application code (false for a world switch card) */
  withCode = true
  private timer: Timer | null = null
  private last = { percent: '', bytes: '', rate: '', valueText: '' }
  /** the loaded bytes shown so far (the figure never goes back when the stage changes) */
  private bytesShown = 0

  constructor(public els: ReadoutEls, readonly meter: LoadMeter, private readonly now: () => number = () => performance.now()) {}

  setTexts(texts: ReadoutTexts): void {
    this.texts = texts
    this.els.bar?.setAttribute('aria-label', texts.label)
    this.last.valueText = ''
    this.render()
  }

  /** the shown fraction (monotone) and, optionally, the stage it belongs to */
  progress(p: number, stage?: MeterStage, stageText?: string): void {
    const v = clamp01(p)
    if (stage) this.stage = stage
    if (stageText !== undefined) this.stageText = stageText
    if (v > this.shown || !this.determinate) {
      this.shown = Math.max(this.shown, v)
      this.determinate = true
      this.els.fill?.style.setProperty('--boot-p', this.shown.toFixed(4))
      const bar = this.els.bar
      if (bar && bar.dataset.state !== 'determinate') bar.dataset.state = 'determinate'
    }
    this.render()
  }

  /** refresh the bytes, the throughput and the remaining time */
  tick(): void {
    this.render()
  }

  start(): void {
    if (this.timer !== null || typeof setInterval !== 'function') return
    this.timer = setInterval(() => this.tick(), READOUT_TICK_MS)
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
  }

  /** the texts of the current state (tests) */
  view(): { percent: string; bytes: string; rate: string; valueText: string } {
    return { ...this.last }
  }

  private render(): void {
    const pct = Math.floor(this.shown * 100 + 1e-9)
    const percent = this.determinate ? `${pct}%` : ''
    const s = this.meter.snapshot(this.stage, this.now(), this.withCode)
    this.bytesShown = Math.max(this.bytesShown, s.loaded)
    const shownTotal = s.total !== null ? Math.max(s.total, this.bytesShown) : null
    const bytes = this.bytesShown > 0 || shownTotal !== null ? formatBytes(this.bytesShown, shownTotal) : ''
    const parts: string[] = []
    const r = formatRate(s.rate)
    if (r) parts.push(r)
    if (r && s.etaS !== null && this.texts) parts.push(this.texts.eta(formatDuration(s.etaS)))
    const rate = parts.join(' · ')
    const valueText = this.determinate && this.texts ? this.texts.valueText(pct, this.stageText) : ''
    this.write('percent', this.els.percent, percent)
    this.write('bytes', this.els.bytes, bytes)
    this.write('rate', this.els.rate, rate)
    const bar = this.els.bar
    if (bar && this.determinate) {
      const now = String(pct)
      if (bar.getAttribute('aria-valuenow') !== now) bar.setAttribute('aria-valuenow', now)
      if (valueText && valueText !== this.last.valueText) bar.setAttribute('aria-valuetext', valueText)
    }
    this.last.valueText = valueText || this.last.valueText
  }

  private write(k: 'percent' | 'bytes' | 'rate', el: HTMLElement | null, text: string): void {
    if (this.last[k] === text) return
    this.last[k] = text
    if (el) el.textContent = text
  }
}

// ------------------------------------------------------------ page-wide instances (shared by the entry and the application)
interface PageSlots { __awrLoadMeter?: LoadMeter; __awrLoadObserver?: PerformanceObserver | null; __awrMaskReadout?: LoadReadout | null }
const slots = globalThis as typeof globalThis & PageSlots & { __AWR_BOOT_MANIFEST?: BootManifestLike }

/** the page meter; the first call starts the Resource Timing observer (buffered: the chunks before the call count too) */
export function pageMeter(): LoadMeter {
  if (slots.__awrLoadMeter) return slots.__awrLoadMeter
  const m = new LoadMeter(slots.__AWR_BOOT_MANIFEST ?? null)
  slots.__awrLoadMeter = m
  slots.__awrLoadObserver = null
  if (typeof performance !== 'undefined' && typeof performance.getEntriesByType === 'function') {
    m.take(performance.getEntriesByType('resource') as unknown as ResourceLike[])
  }
  if (typeof PerformanceObserver !== 'undefined') {
    try {
      const obs = new PerformanceObserver((l) => {
        if (m.take(l.getEntries() as unknown as ResourceLike[])) for (const f of listeners) f()
      })
      obs.observe({ type: 'resource', buffered: true })
      slots.__awrLoadObserver = obs
    } catch {
      slots.__awrLoadObserver = null
    }
  }
  return m
}

const listeners = new Set<() => void>()
/** called when new resource entries arrived; returns the detach function */
export function onMeterEntries(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** the elements of the boot mask bar (index.html) */
export function maskEls(doc: Document = document): ReadoutEls | null {
  const mask = doc.getElementById('boot-mask')
  if (!mask) return null
  const bar = mask.querySelector<HTMLElement>('.boot-progress')
  return {
    bar, fill: bar?.querySelector<HTMLElement>('.boot-fill') ?? null, percent: mask.querySelector<HTMLElement>('.boot-percent'),
    bytes: mask.querySelector<HTMLElement>('.boot-bytes'), rate: mask.querySelector<HTMLElement>('.boot-rate'),
  }
}

/** the boot mask's readout (one per page; null without a mask) */
export function maskReadout(doc: Document = document): LoadReadout | null {
  if (slots.__awrMaskReadout) return slots.__awrMaskReadout
  const els = maskEls(doc)
  if (!els) return null
  const r = new LoadReadout(els, pageMeter())
  slots.__awrMaskReadout = r
  r.start()
  return r
}

/** the mask is gone: stop its refresh */
export function stopMaskReadout(): void {
  slots.__awrMaskReadout?.stop()
}

/**
 * Before the application runs (public demo entry): the bar follows the application code by manifest bytes (by completed
 * modulepreload count without a manifest), stage 'code'. Returns the stop function; the application's writer continues
 * from `readout.shown`.
 */
export function startCodeProgress(doc: Document = document): () => void {
  const r = maskReadout(doc)
  if (!r) return () => {}
  const m = r.meter
  const done = new Set<string>()
  const update = (): void => {
    let f = m.codeFraction()
    if (f === null) {
      const known = new Set<string>()
      for (const l of doc.querySelectorAll<HTMLLinkElement>('link[rel="modulepreload"]')) known.add(pathOfHref(l.href))
      if (typeof performance !== 'undefined') {
        for (const e of performance.getEntriesByType('resource')) if (/\.m?js(\?|$)/.test(e.name)) done.add(pathOfHref(e.name))
      }
      let n = 0
      for (const k of known) if (done.has(k)) n++
      f = known.size > 0 ? n / known.size : 0
    }
    r.progress(CODE_WEIGHT * f, 'code')
  }
  update()
  return onMeterEntries(update)
}

/** the readout texts from the application dictionary (keys boot.progress.label, boot.eta, boot.valueText) */
export function readoutTexts(t: (key: string, params?: Record<string, string | number>) => string): ReadoutTexts {
  return {
    label: t('boot.progress.label'),
    eta: (duration) => t('boot.eta', { time: duration }),
    valueText: (percent, stage) => (stage ? t('boot.valueText', { percent, stage }) : t('boot.valueTextBare', { percent })),
  }
}

/** the readout stage of a boot model segment */
export function stageOfSeg(seg: string | null): MeterStage {
  if (seg === 'bundle') return 'code'
  if (seg === 'world' || seg === 'points') return 'world'
  return 'other'
}
