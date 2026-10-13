// Boot progress bar writer (R-D2-01; AWR-04 §10.1, ADR-108; M15-FR-201; AWR-14 §20.3.3): drives the D1 node
// `#boot-mask .boot-progress > i` (styles/boot.css: transform scaleX(var(--boot-p)), transform-origin left, 2 px) and the
// phase line `.boot-phase` from the five-segment model of lib/bootProgress. Writes at most every BOOT_WRITE_MIN_MS
// (<= 10 Hz) and only when the value grew by at least half a pixel; the value never decreases; reduced motion (the
// prefers-reduced-motion query or the reduced/off motion tier) jumps between segment boundaries. The reveal sets 1
// synchronously before the mask fades; a boot error freezes the bar (the mask shows the error text, AWR-14 §20.3.3 rule 4).
// The bar only reflects the BootController gates and two non-gating segments: it never delays the reveal.
// Kept free of the engine, stores and React. The demo entry writes the bundle segment itself before the application
// chunk loads (it must not import this module, see app/demo/entry.tsx); start() continues from the value it left.
// D2.1 (R-D2-30; ADR-119; AWR-04 §15.5.2; M15-FR-224; UX-FR-149): the track is five segments whose widths follow the
// weights (index.html `.boot-track > i[data-seg]`, flex-grow 15/10/40/30/5, 2 px gaps); the writer sets `--seg-p` (0..1)
// on every segment element of the bar (monotone per segment; reduced motion shows a segment only once it is complete) and
// keeps writing the total as `--boot-p` on the track (tests, the __ux ring). A bar without segment children (an older
// loading card) still scales by `--boot-p`. The loading layers of a world switch, static browsing and the power-save
// resume use the same segmented track (ui/boot/LoadingLayer.tsx, ui/power/PowerOverlay.tsx).
import { t } from '@/app/i18n'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import {
  BOOT_PHASE_KEY, BOOT_SEGS, BOOT_WEIGHTS, BOOT_WRITE_MIN_MS, BOOT_WRITE_MIN_STEP, BootProgressModel, applyReport, onBootProgress, type BootSeg,
} from '@/lib/bootProgress'
import { UX } from '@/ui/testing/uxProbe'

/** capacity of the __ux.boot.progress ring (AWR-14 §20.3.3 rule 6) */
export const BOOT_PROGRESS_RING = 256
const MB = 1024 * 1024

export interface ProgressSample { tMs: number; p: number; seg: BootSeg | null }

/** phase text of the first incomplete segment (the points phase shows the first-screen bytes when known) */
export function phaseText(m: BootProgressModel): string {
  const seg = m.firstIncomplete()
  if (!seg) return t('boot.phase.reveal')
  const key = BOOT_PHASE_KEY[seg]
  if (!key) return t('boot.phase.reveal')
  if (seg === 'points' && m.bytes && m.bytes.total > 0) {
    return t('boot.phase.pointsBytes', { received: (m.bytes.received / MB).toFixed(1), total: (m.bytes.total / MB).toFixed(1) })
  }
  return t(key)
}

export function prefersReducedMotion(): boolean {
  if (typeof document === 'undefined') return false
  const tier = document.documentElement?.dataset?.motion
  if (tier === 'reduced' || tier === 'off') return true
  try {
    return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/**
 * One progress bar on the page (the boot mask or a loading layer): a model, a bar element whose `--boot-p` it writes,
 * an optional phase element, the write throttle and the monotone shown value.
 */
export class ProgressWriter {
  readonly model: BootProgressModel
  /** shown fraction per segment element (monotone) */
  private readonly segShown = new Map<string, number>()
  private segEls: { seg: BootSeg; el: HTMLElement }[] | null = null
  private lastWriteMs = Number.NEGATIVE_INFINITY
  private pending = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private lastText = ''
  frozen = false
  /** written samples (tests and the __ux ring) */
  readonly samples: ProgressSample[] = []

  constructor(private bar: HTMLElement | null, private phase: HTMLElement | null, model: BootProgressModel,
    private readonly now: () => number = () => performance.now(), private readonly ux = false) {
    this.model = model
  }

  attach(bar: HTMLElement | null, phase: HTMLElement | null): void {
    this.bar = bar
    this.phase = phase
    this.segEls = null
    this.lastText = ''
    this.schedule()
  }

  /** the segment children of the bar (`[data-seg]`), looked up once */
  private segments(): { seg: BootSeg; el: HTMLElement }[] {
    if (this.segEls) return this.segEls
    const out: { seg: BootSeg; el: HTMLElement }[] = []
    const bar = this.bar as (HTMLElement & { querySelectorAll?: HTMLElement['querySelectorAll'] }) | null
    if (bar && typeof bar.querySelectorAll === 'function') {
      for (const el of bar.querySelectorAll<HTMLElement>('[data-seg]')) {
        const seg = el.dataset.seg as BootSeg
        if (BOOT_WEIGHTS[seg] !== undefined) out.push({ seg, el })
      }
    }
    this.segEls = out
    return out
  }

  /** the shown fraction of one segment (monotone; reduced motion: 0 until complete); tests */
  segValue(seg: BootSeg): number {
    return this.segShown.get(seg) ?? 0
  }

  schedule(): void {
    if (this.frozen || this.pending) return
    this.pending = true
    const run = (): void => this.flush()
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run)
    else this.timer = setTimeout(run, 0)
  }

  /** one write attempt (exported behaviour for tests: the throttle, the minimum step, monotonicity) */
  flush(): void {
    this.pending = false
    if (this.frozen) return
    const now = this.now()
    const since = now - this.lastWriteMs
    if (since < BOOT_WRITE_MIN_MS && this.model.f.reveal < 1) {
      if (this.timer === null) {
        this.timer = setTimeout(() => {
          this.timer = null
          this.schedule()
        }, BOOT_WRITE_MIN_MS - since)
      }
      return
    }
    const p = this.model.next(prefersReducedMotion())
    const text = phaseText(this.model)
    if (p - this.model.shown < BOOT_WRITE_MIN_STEP && p < 1 && text === this.lastText) return
    this.write(p, text, now)
  }

  /** set the value to 1 at once (reveal); not throttled */
  complete(): void {
    if (this.frozen) return
    this.model.report('reveal', 1)
    this.write(1, phaseText(this.model), this.now())
  }

  freeze(): void {
    this.frozen = true
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
  }

  private write(p: number, text: string, now: number): void {
    this.model.shown = p
    this.lastWriteMs = now
    this.bar?.style.setProperty('--boot-p', p.toFixed(4))
    const reduced = prefersReducedMotion()
    for (const { seg, el } of this.segments()) {
      const f = p >= 1 ? 1 : this.model.active.includes(seg) ? this.model.f[seg] : 0
      const prev = this.segShown.get(seg) ?? 0
      const v = Math.max(prev, reduced ? (f >= 1 ? 1 : 0) : f)
      if (v === prev && (v === 0 || this.segShown.has(seg))) continue
      this.segShown.set(seg, v)
      el.style.setProperty('--seg-p', v.toFixed(4))
    }
    if (this.phase && text !== this.lastText) this.phase.textContent = text
    this.lastText = text
    const sample = { tMs: now, p, seg: this.model.firstIncomplete() }
    this.samples.push(sample)
    if (this.samples.length > BOOT_PROGRESS_RING) this.samples.shift()
    if (this.ux && TEST_SWITCHES) {
      const ring = UX.boot.progress
      ring.push(sample)
      if (ring.length > BOOT_PROGRESS_RING) ring.shift()
    }
  }
}

let main: ProgressWriter | null = null
let offSink: (() => void) | null = null

/** the boot mask's bar (one per page) */
export const bootProgress = {
  /** start the mask bar (idempotent; the demo entry starts it before the application, BootController.start() again) */
  start(): ProgressWriter | null {
    if (main) return main
    if (typeof document === 'undefined') return null
    const mask = document.getElementById('boot-mask')
    if (!mask || document.documentElement.hasAttribute('data-landing')) return null
    const bar = mask.querySelector<HTMLElement>('.boot-progress > i')
    const phase = mask.querySelector<HTMLElement>('.boot-phase')
    main = new ProgressWriter(bar, phase, new BootProgressModel(BOOT_SEGS), undefined, true)
    const w = main
    // the demo entry may already have moved the bundle segment (app/demo/entry.tsx writes `--seg-p` on
    // [data-seg="bundle"], older entries `--boot-p` on the track): continue from there
    const segPrior = Number.parseFloat(bar?.querySelector<HTMLElement>('[data-seg="bundle"]')?.style.getPropertyValue('--seg-p') ?? '')
    const totalPrior = Number.parseFloat(bar?.style.getPropertyValue('--boot-p') ?? '')
    const prior = Number.isFinite(segPrior) && segPrior > 0 ? segPrior * BOOT_WEIGHTS.bundle : totalPrior
    if (Number.isFinite(prior) && prior > 0) {
      w.model.report('bundle', Math.min(1, prior / BOOT_WEIGHTS.bundle))
      w.model.shown = Math.min(prior, BOOT_WEIGHTS.bundle)
    }
    offSink = onBootProgress((r) => {
      if (applyReport(w.model, r)) w.schedule()
    })
    w.schedule()
    return w
  },
  /** the reveal: 100 % before the mask fades out */
  reveal(): void {
    main?.complete()
    offSink?.()
    offSink = null
  },
  /** BOOT_ERROR: the bar keeps its value, the mask shows the error text */
  freeze(): void {
    main?.freeze()
  },
  /** retry after a boot error: writes continue from the frozen value */
  resume(): void {
    if (!main) return
    main.frozen = false
    main.schedule()
  },
  get writer(): ProgressWriter | null {
    return main
  },
  /** tests: forget the page bar */
  resetForTests(): void {
    offSink?.()
    offSink = null
    main = null
  },
}
