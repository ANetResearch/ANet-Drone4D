// Device form factor (R-D2-35 to R-D2-37; ADR-122; AWR-04 §15.8.1; M15-FR-229; UX-FR-153). Owner: M15.
// Classes (CSS px, re-evaluated on resize, orientation change and the pointer media query):
//   narrow   width < 360                                   "window too narrow" Empty
//   phone    width < 768, or short side < 600 (phone landscape)   single window + bottom tab bar
//   tablet   768 <= width < 1280 and short side >= 600       landscape: commander layout; portrait: viewport + drawer
//   desktop  width >= 1280                                  the D1 / D2 floating shell (ADR-028) unchanged
// Input is decided apart from the size: `coarse` when `(pointer: coarse)` matches (a touch-first screen); a desktop with a
// coarse pointer (touch all-in-one, 12.9 inch tablet in landscape) keeps the desktop layout with the touch enhancements
// (44 px hit areas, gestures). The result is written to `<html data-ff="phone|tablet|desktop|narrow" data-coarse
// data-orient="portrait|landscape">`; M06 (viewport/quality.ts) reads the same attributes (it treats narrow as phone). A
// change only re-lays out: the canvas, the realtime link, the selection, the camera and the panel state stay.
// Kept free of React and stores (STORE-01 style plain functions plus a tiny subscription list); ui/mobile uses useFormFactor.
import * as React from 'react'

export type FormFactor = 'narrow' | 'phone' | 'tablet' | 'desktop'
export interface FormFactorState {
  ff: FormFactor
  coarse: boolean
  orient: 'portrait' | 'landscape'
  w: number
  h: number
  /** the viewport form factor M06 and the perception store use (narrow counts as phone) */
  device: 'phone' | 'tablet' | 'desktop'
}

export const FF = { narrowW: 360, phoneMaxW: 768, phoneShortMax: 600, desktopMinW: 1280 } as const

/** AWR-04 §15.8.1 for a CSS size (pure; the same rule as viewport/quality.ts formFactorOf plus the narrow class) */
export function formFactorOf(w: number, h: number): FormFactor {
  if (w < FF.narrowW) return 'narrow'
  if (w >= FF.desktopMinW) return 'desktop'
  if (w < FF.phoneMaxW || Math.min(w, h) < FF.phoneShortMax) return 'phone'
  return 'tablet'
}

export function deviceOf(ff: FormFactor): 'phone' | 'tablet' | 'desktop' {
  return ff === 'narrow' ? 'phone' : ff
}

function measure(): FormFactorState {
  const w = typeof window !== 'undefined' ? window.innerWidth || 0 : 1280
  const h = typeof window !== 'undefined' ? window.innerHeight || 0 : 720
  let coarse = false
  try {
    coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches
  } catch {
    coarse = false
  }
  const ff = formFactorOf(w, h)
  return { ff, coarse, orient: h > w ? 'portrait' : 'landscape', w, h, device: deviceOf(ff) }
}

let state: FormFactorState = measure()
const listeners = new Set<(s: FormFactorState) => void>()
let installed = false
/** wall ms of the last class change (the 300 ms re-layout budget of NFR-205 is measured from it in tests) */
let changedAtMs = 0

function write(s: FormFactorState): void {
  if (typeof document === 'undefined') return
  const el = document.documentElement
  if (el.dataset.ff !== s.ff) el.dataset.ff = s.ff
  if (el.dataset.orient !== s.orient) el.dataset.orient = s.orient
  if (s.coarse !== el.hasAttribute('data-coarse')) el.toggleAttribute('data-coarse', s.coarse)
}

/**
 * re-evaluate now (resize, orientation change, pointer query); returns the state. Listeners (and React) only hear about
 * class changes (form factor, coarse pointer, orientation): a resize storm inside one class re-renders nothing; the size
 * fields of the snapshot are those of the last class change.
 */
export function refreshFormFactor(): FormFactorState {
  const next = measure()
  const classChanged = next.ff !== state.ff || next.orient !== state.orient || next.coarse !== state.coarse
  if (!classChanged) return state
  state = next
  write(state)
  changedAtMs = Date.now()
  for (const cb of listeners) cb(state)
  return state
}

export function formFactorNow(): FormFactorState {
  return state
}
export function formFactorChangedAtMs(): number {
  return changedAtMs
}

export function onFormFactor(cb: (s: FormFactorState) => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** start-up: attributes now, then follow resize, orientation and the pointer query (idempotent) */
export function installFormFactor(): () => void {
  state = measure()
  write(state)
  if (installed || typeof window === 'undefined') return () => {}
  installed = true
  const on = (): void => {
    refreshFormFactor()
  }
  window.addEventListener('resize', on, { passive: true })
  window.addEventListener('orientationchange', on, { passive: true })
  let mq: MediaQueryList | null = null
  try {
    mq = typeof matchMedia === 'function' ? matchMedia('(pointer: coarse)') : null
    mq?.addEventListener?.('change', on)
  } catch {
    mq = null
  }
  return () => {
    installed = false
    window.removeEventListener('resize', on)
    window.removeEventListener('orientationchange', on)
    mq?.removeEventListener?.('change', on)
  }
}

/** test hook: forget the cached state (vitest) */
export function resetFormFactorForTests(s?: Partial<FormFactorState>): void {
  state = { ...measure(), ...s }
  listeners.clear()
}

const subscribe = (cb: () => void): (() => void) => onFormFactor(cb)
const snapshot = (): FormFactorState => state

/** React: the live form factor (re-renders only when it changes) */
export function useFormFactor(): FormFactorState {
  return React.useSyncExternalStore(subscribe, snapshot, snapshot)
}
