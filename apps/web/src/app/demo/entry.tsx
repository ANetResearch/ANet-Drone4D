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
// v0.3.0 (single loading bar): the bar is one fill with a readout (app/boot/loadMeter.ts, a module without imports, so
// it adds no chunk): this entry starts the page meter at once on application paths (Resource Timing, buffered, against the
// build's boot manifest), the bar turns determinate and follows the application code by bytes, and the dictionary texts
// of the readout (remaining time, accessible value) are added when app/i18n has loaded. The application continues the
// same readout (app/boot/progress.ts), so there is never a second bar and the value never goes back.
// D2.2-FX-landing: `t` of the shared app/i18n module (and its full bilingual dictionary) is only read on application
// routes (the `else` branch below), so it is imported dynamically there instead of at module scope: a static top-level
// import here would otherwise pull the whole dictionary into this always-loaded entry module, undoing the landing
// page's own trimmed dictionary (./landingI18n).
import '@/styles/index.css'
import { renderLanding } from './Landing'
import { clearReloadMark, fallbackTexts, importWithRetry, onAppLoadFailed } from './loadApp'
import { CODE_WEIGHT, maskReadout, readoutTexts, startCodeProgress } from '@/app/boot/loadMeter'

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
  // (issue #9: retried with backoff; after the last failure one automatic reload, then an error line and a reload button)
  const app = importWithRetry(() => import('../../main'))
  const stop = startCodeProgress()
  let tr: ((key: string) => string) | null = null
  const session = ((): Storage | null => {
    try {
      return sessionStorage
    } catch {
      return null
    }
  })()
  app.then(() => clearReloadMark(session), () => {
    const texts = tr ? { message: tr('boot.loadFailed'), reload: tr('boot.reload') } : fallbackTexts(navigator.language || 'zh', document.getElementById('boot-mask'))
    onAppLoadFailed(document, session, () => location.reload(), texts)
  })
  void app.finally(stop).catch(() => undefined)
  void import('@/app/i18n').then(({ t }) => {
    tr = t
    bindVersionCopy(t)
    const phase = document.querySelector<HTMLElement>('#boot-mask .boot-phase')
    const readout = maskReadout()
    // the application's writer owns the phase line once it runs; before that it names the code stage
    if (readout && readout.shown < CODE_WEIGHT) {
      if (phase) phase.textContent = t('boot.phase.bundle')
      readout.stageText = t('boot.phase.bundle')
    }
    readout?.setTexts(readoutTexts(t))
  })
}
