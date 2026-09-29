// Canvas colours (M15-FR-074; d01 §3.6.5): the lieflat roles are read once from the computed style on mount and again
// whenever <html class> changes (the light report theme), then cached; canvas code reads only this object.
import { useSyncExternalStore } from 'react'

export interface LfTokens {
  data: string; data2: string; faint: string; faintdata: string; floor: string; grid: string; track: string
  hero: string; heroText: string; halo: string; txt: string; mut: string; lab: string; bg: string
  ramp: readonly [string, string, string, string, string]
  font: string
}

let cache: LfTokens | null = null
const listeners = new Set<() => void>()
let observer: MutationObserver | null = null

function read(): LfTokens {
  const el = typeof document !== 'undefined' ? document.documentElement : null
  const cs = el ? getComputedStyle(el) : null
  const g = (n: string) => (cs ? cs.getPropertyValue(n).trim() : '')
  return {
    data: g('--lf-data'), data2: g('--lf-data2'), faint: g('--lf-faint'), faintdata: g('--lf-faintdata'), floor: g('--lf-floor'),
    grid: g('--lf-grid'), track: g('--lf-track'), hero: g('--lf-hero'), heroText: g('--lf-hero-text'), halo: g('--lf-halo'),
    txt: g('--lf-txt'), mut: g('--lf-mut'), lab: g('--lf-lab'), bg: g('--lf-bg'),
    ramp: [g('--lf-ramp-1'), g('--lf-ramp-2'), g('--lf-ramp-3'), g('--lf-ramp-4'), g('--lf-ramp-5')],
    font: g('--font-sans'),
  }
}

export function getLfTokens(): LfTokens {
  if (!cache) cache = read()
  if (!observer && typeof MutationObserver === 'function' && typeof document !== 'undefined') {
    observer = new MutationObserver(() => {
      cache = read()
      for (const l of listeners) l()
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  }
  return cache
}

const subscribe = (cb: () => void) => {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
export function useLfTokens(): LfTokens {
  return useSyncExternalStore(subscribe, getLfTokens, getLfTokens)
}
