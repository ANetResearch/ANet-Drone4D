// Boot mask (M15-FR-006, FR-110; AWR-15 §4.2, §4.6): index.html paints #boot-mask (badge 480 px, phase text, progress)
// before any JavaScript runs; this component takes over the same node (no re-creation, no flash): it updates the phase
// text and fades the node out with --duration-fast on reveal, then removes it. Errors show two actions (back to the world
// list, retry). The badge never sits inside [data-viewport].
// Pre-raster phase (ADR-069; M15-FR-006): with `prewarm` (the UI shell is mounted, never on ?chrome=0) the mask registers
// the boot gate 'uiWarm'. When every other gate is resolved (first point frame, shader zoo), the mask layer is drawn at
// opacity 0.996 (data-prewarm, styles/boot.css) and the warm stage (WarmStage.tsx) is shown below it. An opaque mask lets
// the compositor cull everything below it, so the shell used to be rasterised and composited for the first time at the
// reveal, when Tier S (SwiftShader) compiles the raster and draw programs of every UI layer and JITs their draw routines in
// the first frames the user sees (FX2-R3 traces: 5 Skia programs and 300-650 ms frames in the first seconds). Under the
// 0.996 mask that work happens invisibly (at most one 8-bit level shows through), and the exit fade draws the mask with the
// translucent-layer program that is already compiled. The phase ends when the presented frames settle (at least
// PREWARM_MIN_FRAMES frames and PREWARM_MIN_MS, then two consecutive intervals <= PREWARM_SETTLED_MS: the GPU process no
// longer stalls on compiles) or after PREWARM_MAX_MS, whichever is first; then 'uiWarm' resolves and the reveal follows.
// The warm stage is hidden synchronously before the fade.
// Palette rehearsal (ADR-076; D1-AC-25): the static specimens do not cover the command palette. Its first opening over
// the revealed scene compiled 13-15 SwiftShader routines (136-168 KB) in one 220-280 ms GPU-process frame (FX2-R5 ops
// diagnostics; the second opening compiles 0-1), because SwiftShader keys the routines of compositor draws on what was
// drawn before them in the same submission (ADR-071 consequences): only the real dialog in the real stacking order gets
// the same variants. So after the stage settles it is hidden, the real palette (full-viewport overlay, popup, focused
// input, list) is opened below the mask until the frames settle again, closed, and the reveal waits for its overlay to
// unmount (an exit animation must never show after the reveal); an info and a success toast of the page's own toaster
// follow the same way. On Tier S both are repeated at motion tier reduced (the tier PerfGovernor step 6 sets shortly
// after the reveal; popups and toasts then appear and leave without transitions). Toast coverage of every mode
// (ADR-081): the toast pass runs at the starting tier as well (Tier S lite: the first toast of a session that PerfGovernor
// never takes to step 6, a command result before it, or a storm whose first visible step is step 1, ACC-5 4.3), and
// it runs when the palette was already open; ?chrome=0 mounts no toaster (its notices never reach the DOM), so there is
// nothing to rehearse there.
import * as React from 'react'
import { t } from '@/app/i18n'
import { MOTION } from '@/lib/tokens/motion.gen'
import { overlays, overlaysStore } from '@/ui/shell/overlays'
import { toast } from '@/ui/components/ui/toast'
import { getGovernorMotion, getMotionTier, setGovernorMotion } from '@/ui/motion/tier'
import { boot } from './BootController'
import { hideWarmStage, showWarmStage, warmStep } from './WarmStage'

export const UI_WARM_GATE = 'uiWarm'
export const PREWARM_MIN_FRAMES = 6
export const PREWARM_MIN_MS = 400
export const PREWARM_SETTLED_MS = 100
export const PREWARM_MAX_MS = 2000
/**
 * palette and toast rehearsal (ADR-076): each phase ends when settled (>= REHEARSE_MIN_FRAMES frames, 2 intervals
 * <= PREWARM_SETTLED_MS) or after REHEARSE_PHASE_MAX_MS; closing waits for the overlay or toasts to unmount, at most
 * REHEARSE_CLOSE_MAX_MS. The compile happens in the first frame or two of a phase; the bounds keep the added cold start
 * near 2 s (FX2-R5: 3.6 s with 1.5 s phases and a toast pass per motion tier)
 */
export const REHEARSE_MIN_FRAMES = 3
export const REHEARSE_PHASE_MAX_MS = 800
export const REHEARSE_CLOSE_MAX_MS = 800

/** the pre-raster phase: show the warm stage below a 0.996 mask until the presented frames settle; resolves when done */
function runPrewarm(el: HTMLElement): Promise<void> {
  return new Promise((done) => {
    el.dataset.prewarm = ''
    showWarmStage()
    const t0 = performance.now()
    let last = t0
    let frames = 0
    let calm = 0
    const step = (now: number): void => {
      const dt = now - last
      last = now
      frames++
      calm = dt <= PREWARM_SETTLED_MS ? calm + 1 : 0
      warmStep(frames)
      const elapsed = now - t0
      const settled = frames >= PREWARM_MIN_FRAMES && elapsed >= PREWARM_MIN_MS && calm >= 2
      if (settled || elapsed >= PREWARM_MAX_MS || boot.state === 'BOOT_ERROR') done()
      else requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
  })
}

/**
 * one pass of the palette rehearsal: open the real palette below the mask until the frames settle, type a query (the
 * filtered state of every search) until they settle again, clear it, close, and resolve one frame after the overlay
 * unmounted (or REHEARSE_CLOSE_MAX_MS)
 */
function palettePass(): Promise<void> {
  return new Promise((done) => {
    overlays.set('palette', true)
    const t0 = performance.now()
    let last = t0
    let frames = 0
    let calm = 0
    let closedAt = -1
    let typedAt = -1
    let phase0 = t0
    const step = (now: number): void => {
      const dt = now - last
      last = now
      frames++
      calm = dt <= PREWARM_SETTLED_MS ? calm + 1 : 0
      if (boot.state === 'BOOT_ERROR') {
        overlays.set('palette', false)
        done()
        return
      }
      const settled = (frames >= REHEARSE_MIN_FRAMES && calm >= 2) || now - phase0 >= REHEARSE_PHASE_MAX_MS
      if (typedAt < 0) {
        if (settled) {
          typePaletteQuery(t('palette.preset', { name: '' }))
          typedAt = now
          phase0 = now
          frames = 0
          calm = 0
        }
      } else if (closedAt < 0) {
        if (settled) {
          // programmatic close does not run the palette's onOpenChange: clear the query here, or the user's first
          // palette would open pre-filled
          typePaletteQuery('')
          overlays.set('palette', false)
          closedAt = now
        }
      } else if (!document.querySelector('[data-slot="dialog-overlay"]') || now - closedAt >= REHEARSE_CLOSE_MAX_MS) {
        requestAnimationFrame(() => done())
        return
      }
      requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
  })
}

/** the toasts of the page's toaster, not the warm stage's specimens */
function liveToasts(): number {
  let n = 0
  for (const el of document.querySelectorAll('[data-slot="toast"]')) if (!el.closest('[data-warm-stage]')) n++
  return n
}

/**
 * one pass of the toast rehearsal: an info toast (PerfGovernor notices) and a success toast (command results) shown by
 * the page's own toaster until the frames settle, then closed; resolves one frame after they unmounted (or
 * REHEARSE_CLOSE_MAX_MS). The specimens of the warm stage sit in a static container without the toaster's position,
 * stacking and enter transition, so the first real toast still compiled 4-7 routines (FX2-R5 ops diagnostics)
 */
function toastPass(pass = ''): Promise<void> {
  return new Promise((done) => {
    const ids = (['info', 'success'] as const).map((type) => toast.add({ id: `boot-rehearsal-${pass}${type}`, type, timeout: 0,
      title: t('boot.phase.warming'), description: t('boot.phase.firstScreen') }))
    const t0 = performance.now()
    let last = t0
    let frames = 0
    let calm = 0
    let closedAt = -1
    const step = (now: number): void => {
      const dt = now - last
      last = now
      frames++
      calm = dt <= PREWARM_SETTLED_MS ? calm + 1 : 0
      if (closedAt < 0) {
        if ((frames >= REHEARSE_MIN_FRAMES && calm >= 2) || now - t0 >= REHEARSE_PHASE_MAX_MS || boot.state === 'BOOT_ERROR') {
          for (const id of ids) toast.close(id)
          closedAt = now
        }
      } else if (liveToasts() === 0 || now - closedAt >= REHEARSE_CLOSE_MAX_MS) {
        requestAnimationFrame(() => done())
        return
      }
      requestAnimationFrame(step)
    }
    requestAnimationFrame(step)
  })
}

/**
 * the real command palette opened and closed below the mask (ADR-076), then the toasts (ADR-081); a palette that something
 * already opened is left alone, the toast passes still run. On Tier S a second pass runs at motion tier reduced:
 * PerfGovernor step 6 sets it about 5 s after the reveal in most sessions, and without the open and close transitions the
 * compositor draws the popup and the toasts with other program variants
 */
export async function rehearsePalette(): Promise<void> {
  if (typeof document === 'undefined' || boot.state === 'BOOT_ERROR') return
  const palette = (): boolean => !overlaysStore.getState().palette
  const failed = (): boolean => boot.state === 'BOOT_ERROR' // read again after every await
  performance.mark?.('awr.boot.rehearse.start')
  hideWarmStage()
  if (palette()) await palettePass()
  // the starting tier (Tier S lite, Tier B/A full or the user's setting)
  if (liveToasts() === 0 && !failed()) await toastPass()
  const tierS = document.documentElement?.dataset?.tier === 'S'
  if (!tierS || getMotionTier() === 'reduced' || getMotionTier() === 'off' || failed()) {
    performance.mark?.('awr.boot.rehearse.end')
    return
  }
  // Tier S: the reduced pass (the first toasts after the reveal come from PerfGovernor step 7, issued in the same
  // evaluation as step 6, i.e. at motion tier reduced, in most full-scene sessions)
  const prev = getGovernorMotion()
  setGovernorMotion('reduced')
  try {
    if (palette() && !failed()) await palettePass()
    if (liveToasts() === 0 && !failed()) await toastPass('reduced-')
  } finally {
    setGovernorMotion(prev)
    performance.mark?.('awr.boot.rehearse.end')
  }
}

/** type into the open palette's input the way a user does (native value setter and an input event, so cmdk filters) */
function typePaletteQuery(q: string): void {
  const el = document.querySelector<HTMLInputElement>('[data-slot="dialog-content"] [data-slot="command-input"]')
  if (!el) return
  // through the prototype setter with the input as receiver: React tracks the value on the instance, so a plain
  // assignment would not reach its onChange
  if (!Reflect.set(HTMLInputElement.prototype, 'value', q.trim(), el)) return
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

export function BootMask({ prewarm = false }: { prewarm?: boolean }) {
  React.useEffect(() => {
    const el = document.getElementById('boot-mask')
    if (!el) return
    const phase = el.querySelector<HTMLElement>('.boot-phase')
    let removeTimer: ReturnType<typeof setTimeout> | null = null
    let offWarm: (() => void) | null = null
    if (prewarm && boot.state !== 'REVEALED' && boot.state !== 'BOOT_ERROR') {
      boot.addGate(UI_WARM_GATE)
      offWarm = boot.whenReadyExcept(UI_WARM_GATE, () => {
        void runPrewarm(el).then(rehearsePalette).then(() => boot.resolveGate(UI_WARM_GATE))
      })
    }
    const un = boot.subscribe((s, info) => {
      el.dataset.boot = s
      if (phase) phase.textContent = info.error ? t(info.error) : info.slow ? t('boot.slow') : t(info.phaseKey)
      // an error page must be readable: back to the fully opaque mask
      if (s === 'BOOT_ERROR') {
        delete el.dataset.prewarm
        hideWarmStage()
      }
      if (s === 'REVEALED' && el.dataset.state !== 'revealed') {
        hideWarmStage()
        el.dataset.state = 'revealed'
        const done = () => el.remove()
        el.addEventListener('transitionend', done, { once: true })
        removeTimer = setTimeout(done, MOTION.durationFastMs + MOTION.durationQuickMs) // transitionend fallback
      }
    })
    return () => {
      un()
      offWarm?.()
      if (removeTimer) clearTimeout(removeTimer)
    }
  }, [prewarm])
  return null
}
