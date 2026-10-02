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
import * as React from 'react'
import { t } from '@/app/i18n'
import { MOTION } from '@/lib/tokens/motion.gen'
import { boot } from './BootController'
import { hideWarmStage, showWarmStage, warmStep } from './WarmStage'

export const UI_WARM_GATE = 'uiWarm'
export const PREWARM_MIN_FRAMES = 6
export const PREWARM_MIN_MS = 400
export const PREWARM_SETTLED_MS = 100
export const PREWARM_MAX_MS = 2000

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
        void runPrewarm(el).then(() => boot.resolveGate(UI_WARM_GATE))
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
