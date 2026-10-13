// Entry of the public demo build (ADR-083; vite.config.ts swaps it in for src/main.tsx when VITE_AWR_DEMO=public): "/"
// renders the landing page (index.html hides the boot mask there before any script runs); every other path loads the
// full application (src/main.tsx, a separate chunk with the engine, three and the realtime client), so visitors who only
// read the landing page never download the 3D runtime.
// D2 (R-D2-01; M15-FR-201; AWR-14 §20.3.2 bundle row): on application paths the bundle segment of the boot progress bar
// (weight 0.15) follows the application chunks as they complete (Resource Timing over the modulepreload links the preload
// helper inserts; one step per chunk until the build writes a byte manifest, request to M00); the full five-segment
// writer (app/boot/progress.ts) takes over when the application runs and never goes below what was shown here. The
// watcher is written inline on purpose: a module shared with the application would be bundled into the shared UI chunk,
// and the landing page would then load three (M15-FR-121 chunk rule). The open interface page /sandbox/api renders on
// its own (app/demo/apiDocsEntry.tsx), without the application.
// D2.1 (R-D2-29, R-D2-30, R-D2-32; ADR-118, ADR-119; AWR-04 §15.5.2, §15.5.3; M15-FR-223 to FR-225): the bundle watcher
// writes the bundle segment of the five-segment track (`--seg-p` on [data-seg="bundle"], 0..1). The boot capacity request
// and its line belong to the early boot module (app/boot/early.ts, the first module script of index.html, D2-FX-R2-web):
// it runs before this entry's chunks have arrived, and the application takes its request over (window.__awrBootCapacity
// -> app/boot/capacity.ts, M17's stores/capacity) so the 5 s spacing holds. The version line
// (static text of index.html) copies the full build information on click or long press (lib/build is loaded on that click,
// so it stays out of the static closure). Only modules of the landing group are imported statically: the landing chunk set
// stays {index, landing, react, rolldown-runtime} plus the early module and its boot chunk (no three, no store).
// D2.2-FX-landing: `t` of the shared app/i18n module (and its full bilingual dictionary) is only read on application
// routes (the `else` branch below), so it is imported dynamically there instead of at module scope: a static top-level
// import here would otherwise pull the whole dictionary into this always-loaded entry module, undoing the landing
// page's own trimmed dictionary (./landingI18n).
import '@/styles/index.css'
import { renderLanding } from './Landing'

const BUNDLE_WEIGHT = 0.15
const WRITE_MIN_MS = 100

/** bundle segment of the boot bar before the application runs; returns the stop function */
function watchBundleInline(t: (key: string) => string): () => void {
  const bar = document.querySelector<HTMLElement>('#boot-mask .boot-progress > i')
  const seg = bar?.querySelector<HTMLElement>('[data-seg="bundle"]') ?? null
  const phase = document.querySelector<HTMLElement>('#boot-mask .boot-phase')
  if (!bar || typeof PerformanceObserver === 'undefined') return () => {}
  if (phase) phase.textContent = t('boot.phase.bundle')
  const done = new Set<string>()
  let shown = 0
  let last = 0
  const path = (href: string): string => {
    try {
      return new URL(href, location.href).pathname
    } catch {
      return href
    }
  }
  const update = (): void => {
    const known = new Set<string>()
    for (const l of document.querySelectorAll<HTMLLinkElement>('link[rel="modulepreload"]')) known.add(path(l.href))
    if (known.size === 0) return
    let n = 0
    for (const k of known) if (done.has(k)) n++
    const p = BUNDLE_WEIGHT * Math.min(1, n / known.size)
    const now = performance.now()
    if (p <= shown || now - last < WRITE_MIN_MS) return
    shown = p
    last = now
    bar.style.setProperty('--boot-p', p.toFixed(4))
    seg?.style.setProperty('--seg-p', (p / BUNDLE_WEIGHT).toFixed(4))
  }
  const take = (list: readonly PerformanceEntry[]): void => {
    for (const e of list) if (/\.m?js(\?|$)/.test(e.name)) done.add(path(e.name))
    update()
  }
  take(performance.getEntriesByType('resource'))
  let obs: PerformanceObserver | null = null
  try {
    obs = new PerformanceObserver((l) => take(l.getEntries()))
    obs.observe({ type: 'resource', buffered: true })
  } catch {
    obs = null
  }
  return () => obs?.disconnect()
}

/** the version line copies the full build information (click, or the long press of touch screens) */
function bindVersionCopy(t: (key: string) => string): void {
  const el = document.querySelector<HTMLElement>('#boot-mask .boot-version')
  if (!el) return
  el.title = t('build.copy')
  const copy = (e: Event): void => {
    e.preventDefault()
    const text = el.textContent ?? ''
    void import('@/lib/build').then((b) => b.copyText(b.diagnostics())).then((ok) => {
      if (!ok) return
      el.textContent = t('build.copied')
      setTimeout(() => {
        el.textContent = text
      }, 1500)
    })
  }
  el.addEventListener('click', copy)
  el.addEventListener('contextmenu', copy)
}

if (location.pathname === '/') renderLanding(document.getElementById('root')!)
else if (location.pathname === '/sandbox/api') void import('./apiDocsEntry').then((m) => m.renderApiDocs(document.getElementById('root')!))
else {
  // import first: the preload helper inserts the modulepreload links of the application chunks synchronously, so the
  // watcher counts them from its first look (otherwise the three static chunks alone would read as a complete bundle)
  const app = import('../../main')
  void import('@/app/i18n').then(({ t }) => {
    bindVersionCopy(t)
    const stop = watchBundleInline(t)
    void app.finally(stop)
  })
}
