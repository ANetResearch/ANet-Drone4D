// Boot mask (M15-FR-006, FR-110; AWR-15 §4.2, §4.6): index.html paints #boot-mask (badge 480 px, phase text, progress)
// before any JavaScript runs; this component takes over the same node (no re-creation, no flash): it updates the phase
// text and fades the node out with --duration-fast on reveal, then removes it. Errors show two actions (back to the world
// list, retry). The badge never sits inside [data-viewport].
import * as React from 'react'
import { t } from '@/app/i18n'
import { MOTION } from '@/lib/tokens/motion.gen'
import { boot } from './BootController'

export function BootMask() {
  React.useEffect(() => {
    const el = document.getElementById('boot-mask')
    if (!el) return
    const phase = el.querySelector<HTMLElement>('.boot-phase')
    let removeTimer: ReturnType<typeof setTimeout> | null = null
    const un = boot.subscribe((s, info) => {
      el.dataset.boot = s
      if (phase) phase.textContent = info.error ? t(info.error) : info.slow ? t('boot.slow') : t(info.phaseKey)
      if (s === 'REVEALED' && el.dataset.state !== 'revealed') {
        el.dataset.state = 'revealed'
        const done = () => el.remove()
        el.addEventListener('transitionend', done, { once: true })
        removeTimer = setTimeout(done, MOTION.durationFastMs + MOTION.durationQuickMs) // transitionend fallback
      }
    })
    return () => {
      un()
      if (removeTimer) clearTimeout(removeTimer)
    }
  }, [])
  return null
}
