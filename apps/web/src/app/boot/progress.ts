// Boot progress bar writer (R-D2-01; AWR-04 §10.1, ADR-108; M15-FR-201; AWR-14 §20.3.3): drives the D1 node
// `#boot-mask .boot-progress > i` (styles/boot.css: transform scaleX(var(--boot-p)), transform-origin left, 2 px) and the
// phase line `.boot-phase` from the five-segment model of lib/bootProgress. Writes at most every BOOT_WRITE_MIN_MS
// (<= 10 Hz) and only when the value grew by at least half a pixel; the value never decreases; reduced motion (the
// prefers-reduced-motion query or the reduced/off motion tier) jumps between segment boundaries. The reveal sets 1
// synchronously before the mask fades; a boot error freezes the bar (the mask shows the error text, AWR-14 §20.3.3 rule 4).
// The bar only reflects the BootController gates and two non-gating segments: it never delays the reveal.
// Kept free of the engine, stores and React. The demo entry moves the code part of the bar itself before the application
// chunk loads (through app/boot/loadMeter.ts; it must not import this module, see app/demo/entry.tsx); start() continues
// from the value the page readout shows.
// v0.3.0 (single bar; app/boot/loadMeter.ts): the five-segment track of D2.1 is gone. Its segments filled independently,
// so the shader warm-up (warming) filled on the right while the first-screen points were still empty in the middle, and
// the mask showed two bars. The writer now writes ONE value, the monotone total `--boot-p`, on the bar's fill, and hands
// it to the bar's readout (LoadReadout: percent, aria-valuenow/-valuetext, loaded / total bytes of the current stage,
// throughput and remaining time). The phase line names the stage only (the bytes moved to the readout). The loading
// card of a world switch and static browsing uses the same writer and readout (ui/boot/LoadingLayer.tsx).
import { t } from '@/app/i18n'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import {
  BOOT_PHASE_KEY, BOOT_SEGS, BOOT_WEIGHTS, BOOT_WRITE_MIN_MS, BOOT_WRITE_MIN_STEP, BootProgressModel, applyReport, onBootProgress, type BootSeg,
} from '@/lib/bootProgress'
import { UX } from '@/ui/testing/uxProbe'
import { type LoadReadout, maskReadout, readoutTexts, stageOfSeg, stopMaskReadout } from './loadMeter'

/** capacity of the __ux.boot.progress ring (AWR-14 §20.3.3 rule 6) */
export const BOOT_PROGRESS_RING = 256
export interface ProgressSample { tMs: number; p: number; seg: BootSeg | null }

/** phase text of the first incomplete segment (the bytes are shown by the readout under the bar) */
export function phaseText(m: BootProgressModel): string {
  const seg = m.firstIncomplete()
  if (!seg) return t('boot.phase.reveal')
  const key = BOOT_PHASE_KEY[seg]
  return key ? t(key) : t('boot.phase.reveal')
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
 * One progress bar on the page (the boot mask or a loading card): a model, the fill element whose `--boot-p` it writes
 * (through the readout when one is attached), an optional phase element, the write throttle and the monotone shown value.
 */
export class ProgressWriter {
  readonly model: BootProgressModel
  private lastWriteMs = Number.NEGATIVE_INFINITY
  private pending = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private lastText = ''
  frozen = false
  /** the bar's readout (percent, aria, bytes, throughput, remaining time); null: only `--boot-p` is written */
  readout: LoadReadout | null = null
  /** written samples (tests and the __ux ring) */
  readonly samples: ProgressSample[] = []

  constructor(private bar: HTMLElement | null, private phase: HTMLElement | null, model: BootProgressModel,
    private readonly now: () => number = () => performance.now(), private readonly ux = false) {
    this.model = model
  }

  attach(bar: HTMLElement | null, phase: HTMLElement | null): void {
    this.bar = bar
    this.phase = phase
    this.lastText = ''
    this.schedule()
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
    const seg = this.model.firstIncomplete()
    const r = this.readout
    if (r) {
      if (this.model.bytes) r.meter.world = { received: this.model.bytes.received, total: this.model.bytes.total }
      r.progress(p, stageOfSeg(seg), text)
    } else this.bar?.style.setProperty('--boot-p', p.toFixed(4))
    if (this.phase && text !== this.lastText) this.phase.textContent = text
    this.lastText = text
    const sample = { tMs: now, p, seg }
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
    // one readout per page: the demo entry started it (and moved the code part of the bar) before the application chunk
    // arrived; continue from the value it shows, so the bar never goes back and no second bar appears
    const readout = maskReadout()
    w.readout = readout
    readout?.setTexts(readoutTexts(t))
    const prior = readout ? readout.shown : Number.parseFloat(bar?.style.getPropertyValue('--boot-p') ?? '')
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
    stopMaskReadout()
    offSink?.()
    offSink = null
  },
  /** BOOT_ERROR: the bar keeps its value, the mask shows the error text */
  freeze(): void {
    main?.freeze()
    main?.readout?.stop()
  },
  /** retry after a boot error: writes continue from the frozen value */
  resume(): void {
    if (!main) return
    main.frozen = false
    main.readout?.start()
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
