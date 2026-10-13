// D2.1 device form factor and quality profile (AWR-04 §15.8.1, §15.8.6; ADR-122; WP-11b). Owner: M06.
// Form factor: M15's lib/formFactor writes <html data-ff="phone|tablet|desktop" data-coarse>; until it does (or in pages
// without the shell) the same rule is evaluated here from the window size and the pointer media query:
//   desktop  width >= 1280;  phone  width < 768 or short side < 600 (phone landscape);  tablet  otherwise
// (the < 360 px "too narrow" state is the shell's Empty; the viewport treats it as a phone).
// Quality profile per form factor (the desktop row is D1, unchanged): frame-rate limit, drawing-buffer pixel limit (DPR <= 2),
// point budget ceiling and start rung of the point-cloud ladder, EDL, environment visual level, label, hero and low-poly
// caps, subscription rates. A mobile GPU (deviceClass = mobile: Adreno, Mali, PowerVR, Apple GPU on a coarse pointer) never
// gets the desktop row: on a desktop-sized screen (12.9 inch tablets in landscape) it takes the tablet row.
// The profile is fixed when the backend is created (be.quality); the field-view budget follows the live form factor.
import type { FormFactor, QualityView } from '@/engine'

export type { FormFactor, QualityView } from '@/engine'

export const FORM_FACTOR = { desktopMinW: 1280, phoneMaxW: 768, phoneShortMax: 600, narrowW: 360 } as const

/** AWR-04 §15.8.1 for a CSS size (the narrow state reports phone) */
export function formFactorOf(w: number, h: number): FormFactor {
  if (w >= FORM_FACTOR.desktopMinW) return 'desktop'
  if (w < FORM_FACTOR.phoneMaxW || Math.min(w, h) < FORM_FACTOR.phoneShortMax) return 'phone'
  return 'tablet'
}

/** the pointer of the page is coarse (touch first): <html data-coarse> or the media query */
export function coarsePointer(): boolean {
  if (typeof document !== 'undefined' && document.documentElement.hasAttribute('data-coarse')) return true
  return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches
}

/** live form factor: M15's <html data-ff> when present, else the rule above on the window size */
export function currentFormFactor(): FormFactor {
  const ff = typeof document !== 'undefined' ? document.documentElement.dataset.ff : undefined
  if (ff === 'phone' || ff === 'tablet' || ff === 'desktop') return ff
  if (typeof window === 'undefined') return 'desktop'
  return formFactorOf(window.innerWidth || 0, window.innerHeight || 0)
}

interface Row extends Omit<QualityView, 'formFactor' | 'mobile'> {
  /** highest start rung index of the point-cloud ladder (M05 LADDER; 5 = no limit) */
  startRungMax: number
  dprMax: number
  /** frame-rate limit once the PerfGovernor has stepped down (tablet 30; 0: none) */
  degradedFps: number
}

/** AWR-04 §15.8.6 */
export const QUALITY_ROWS: Readonly<Record<FormFactor, Row>> = {
  phone: { fps: 30, maxMegapixels: 1.0, dprMax: 2, pointBudgetMax: 500_000, startRungMax: 2, edl: 'off', envLevel: 'low', labelsMax: 8, heroMax: 1,
    lowPolyMax: 32, swarmHz: 5, selectedHz: 30, degradedFps: 30 },
  tablet: { fps: 0, maxMegapixels: 2.0, dprMax: 2, pointBudgetMax: 1_000_000, startRungMax: 3, edl: 'undegraded', envLevel: 'low', labelsMax: 16,
    heroMax: 2, lowPolyMax: 64, swarmHz: 10, selectedHz: 30, degradedFps: 30 },
  desktop: { fps: 0, maxMegapixels: 0, dprMax: 0, pointBudgetMax: 0, startRungMax: 5, edl: 'on', envLevel: null, labelsMax: 0, heroMax: 0, lowPolyMax: 0,
    swarmHz: 0, selectedHz: 0, degradedFps: 0 },
}
export interface QualityProfile extends QualityView {
  readonly startRungMax: number
  readonly dprMax: number
  readonly degradedFps: number
}

/** frame-rate limit of the profile now (the tablet's 60 fps becomes 30 once the PerfGovernor stepped down) */
export const qualityFpsNow = (q: Pick<QualityProfile, 'fps' | 'degradedFps'>, governorStep: number): number =>
  q.fps > 0 ? q.fps : governorStep > 0 ? q.degradedFps : 0

/** EDL of the point cloud now (AWR-04 §15.8.6: phone off, tablet while the PerfGovernor has not stepped down) */
export const qualityEdlNow = (q: Pick<QualityProfile, 'edl'>, governorStep: number): boolean => q.edl === 'on' || (q.edl === 'undegraded' && governorStep === 0)

/** the profile of a form factor; a mobile GPU on a desktop-sized screen takes the tablet row */
export function qualityProfile(ff: FormFactor, mobile: boolean): QualityProfile {
  const row = QUALITY_ROWS[ff === 'desktop' && mobile ? 'tablet' : ff]
  return { ...row, formFactor: ff, mobile }
}

/** the profile at backend creation (window size, pointer, renderer string) */
export function profileForPage(mobile: boolean): QualityProfile {
  return qualityProfile(currentFormFactor(), mobile)
}

/** a limit that is 0 when absent: min(a, b) over the positive values */
export const capOf = (base: number, limit: number): number => (limit > 0 ? Math.min(base, limit) : base)

/**
 * canvas DPR (M06 §6.5 dprFor, then the profile): the DPR ceiling and the drawing-buffer pixel limit for a CSS size; never
 * above the D1 value, never below 0.5
 */
export function qualityDpr(d1Dpr: number, q: Pick<QualityProfile, 'dprMax' | 'maxMegapixels'> | null | undefined, cssW: number, cssH: number): number {
  if (!q) return d1Dpr
  let d = q.dprMax > 0 ? Math.min(d1Dpr, q.dprMax) : d1Dpr
  if (q.maxMegapixels > 0 && cssW > 0 && cssH > 0) d = Math.min(d, Math.sqrt((q.maxMegapixels * 1e6) / (cssW * cssH)))
  return Math.max(0.5, Math.round(d * 100) / 100)
}

/** start rung of the point-cloud ladder under the profile */
export const qualityStartRung = (startRung: number, q: Pick<QualityProfile, 'startRungMax'> | null | undefined): number =>
  q ? Math.min(startRung, q.startRungMax) : startRung

// ------------------------------------------------------------------------------------------------ field view budget
export { FIELD_BUDGETS, fieldBudget, type FieldBudget } from '@/engine/field/budget'

/** watch the form factor: <html data-ff> changes (M15) and window resizes; returns the unsubscribe function */
export function onFormFactorChange(cb: (ff: FormFactor) => void): () => void {
  if (typeof window === 'undefined') return () => {}
  let last = currentFormFactor()
  const check = (): void => {
    const ff = currentFormFactor()
    if (ff === last) return
    last = ff
    cb(ff)
  }
  window.addEventListener('resize', check)
  const mo = typeof MutationObserver !== 'undefined' ? new MutationObserver(check) : null
  mo?.observe(document.documentElement, { attributes: true, attributeFilter: ['data-ff'] })
  return () => {
    window.removeEventListener('resize', check)
    mo?.disconnect()
  }
}
