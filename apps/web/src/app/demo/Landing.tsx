// Public demo landing page, "/" of the VITE_AWR_DEMO=public build (ADR-083; M15-FR-121; AWR-19 §3.7 T4). Rendered by
// app/demo/entry.tsx before (and instead of) the application: React, shadcn (base-mira) parts, morphicons icons and the
// i18n dictionaries only, so the first screen loads neither three nor the engine nor the realtime client.
// D2 (ADR-111, ADR-112; M15-FR-121 as revised, FR-204, FR-205; AWR-14 §20.4.1): the second main action "open my sandbox"
// opens the world and template Sheet (loaded on demand, so the first screen chunks stay the same).
// D2.1 (ADR-119, ADR-122; AWR-04 §15.5.3, §15.6.5; M15-FR-225; UX-FR-150): the capacity line ("live viewing x / N · sandboxes
// a / b · y queued · about t" and the "needs queue" badge) under the buttons, refreshed every 10 s while the page is visible;
// the request is the store-free one of app/i18n/capacity (the landing chunk must not load zustand); "open my sandbox"
// says "needs queue · position y + 1 · about t" when the pool is full. Only windows narrower than 360 CSS px disable the
// sandbox button ("window too narrow").
// D2.2-L1 (redesign): one-line positioning, one subtitle and the two main actions in the hero, then four sections
// (capabilities with delivery status, captured media, stack and roadmap, open source and citation) in enterprise copy
// (docs/05 §7.10); a scroll-driven voxel backdrop (LandingVoxel, WebGL2 single instanced draw call or Canvas2D, lazy
// chunk); phone and tablet layouts with a Sheet menu below 1280 CSS px and touch targets of at least 44 px. Test hooks of
// D2 and D2.1 are kept (data-view, data-demo-launch, data-sandbox-open, data-launch-hint, data-landing-capacity,
// data-capacity-queue, data-landing-queue-hint, data-sandbox-small).
import * as React from 'react'
import { createRoot } from 'react-dom/client'
import { setLocale, useT, type Locale } from '@/app/i18n'
import { DEMO_ENTRY } from '@/lib/demo'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { bootCapacityFetch, capacityLine, sandboxQueueHint, type CapacityLine } from '@/app/i18n/capacity'
import type { CapacityView } from '@/net/sandbox/client'
import { buildVersion } from './LandingContent'
import { LandingHeader } from './LandingHeader'
import { Capabilities, LandingFooter, OpenSource, Showcase, StackRoadmap } from './LandingSections'
import { LandingVoxel } from './LandingVoxel'
import './landing.css'

const SandboxLaunchSheet = React.lazy(() => import('./SandboxLaunchSheet'))
const LANG_KEY = 'awr.demo.lang'

function initialLocale(): Locale {
  try {
    const v = globalThis.localStorage?.getItem(LANG_KEY)
    if (v === 'zh-CN' || v === 'en') return v
  } catch {
    // storage unavailable (private mode): follow the browser language
  }
  return /^zh\b/i.test(globalThis.navigator?.language ?? 'zh-CN') ? 'zh-CN' : 'en'
}

const MIN_W = 360
const MIN_H = 0
const CAPACITY_POLL_MS = 10_000
/** the capacity answer of the landing page: now, then every 10 s while visible (no store, no token) */
function useLandingCapacity(): { data: CapacityView | null; line: CapacityLine | null } {
  const [data, setData] = React.useState<CapacityView | null>(null)
  const [failed, setFailed] = React.useState(false)
  React.useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null
    const tick = (): void => {
      timer = null
      if (document.visibilityState === 'visible') {
        bootCapacityFetch().then((d) => {
          if (!alive) return
          setData(d)
          setFailed(false)
        }, () => alive && setFailed(true))
      }
      timer = setTimeout(tick, CAPACITY_POLL_MS)
    }
    tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [])
  return { data, line: data ? capacityLine(data) : failed ? capacityLine(null) : null }
}
function useBigWindow(): boolean {
  const [ok, setOk] = React.useState(() => (globalThis.innerWidth || 0) >= MIN_W && (globalThis.innerHeight || 0) >= MIN_H)
  React.useEffect(() => {
    const on = () => setOk(innerWidth >= MIN_W && innerHeight >= MIN_H)
    addEventListener('resize', on)
    return () => removeEventListener('resize', on)
  }, [])
  return ok
}

/** sections fade and rise once when they enter the viewport (not under reduced motion, not without IntersectionObserver) */
function useReveal(): void {
  React.useEffect(() => {
    const root = document.documentElement
    if (typeof IntersectionObserver !== 'function' || matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const io = new IntersectionObserver((es) => {
      for (const e of es) {
        if (!e.isIntersecting) continue
        e.target.setAttribute('data-in', '')
        io.unobserve(e.target)
      }
    }, { rootMargin: '0px 0px 12% 0px', threshold: 0 })
    const els = document.querySelectorAll('[data-view="landing"] .landing-reveal')
    // elements already on screen are shown without the transition
    const vh = innerHeight
    for (const el of els) {
      if (el.getBoundingClientRect().top < vh) el.setAttribute('data-in', '')
      else io.observe(el)
    }
    root.setAttribute('data-landing-reveal', '')
    return () => {
      io.disconnect()
      root.removeAttribute('data-landing-reveal')
    }
  }, [])
}

export function Landing() {
  const t = useT()
  const [locale, setLoc] = React.useState<Locale>(initialLocale)
  React.useLayoutEffect(() => {
    setLocale(locale)
    document.title = t('landing.docTitle')
  }, [locale, t])
  const choose = (l: Locale): void => {
    setLoc(l)
    try {
      globalThis.localStorage?.setItem(LANG_KEY, l)
    } catch {
      // not persisted in private mode
    }
  }
  const big = useBigWindow()
  const capacity = useLandingCapacity()
  const queueHint = sandboxQueueHint(capacity.data)
  const [sheet, setSheet] = React.useState(false)
  const [sheetLoaded, setSheetLoaded] = React.useState(false)
  const openSheet = (): void => {
    if (!big) return
    setSheetLoaded(true)
    setSheet(true)
  }
  const version = React.useMemo(() => buildVersion(), [])
  useReveal()
  return (
    <div data-view="landing" className="relative isolate min-h-full bg-background text-foreground">
      <LandingVoxel />
      <LandingHeader locale={locale} onLocale={choose} onSandbox={big ? openSheet : null} />

      <main id="top" className="relative z-(--z-labels)">
        <section className="relative" data-landing-hero="">
          <div className="mx-auto flex max-w-6xl flex-col px-4 pt-14 pb-12 sm:px-6 md:pt-24 md:pb-16 xl:pt-28">
            <div className="brand-enter flex max-w-5xl flex-col gap-5 md:gap-6">
              <p className="flex items-center gap-2.5 font-mono text-xs tracking-widest text-muted-foreground uppercase" data-landing-eyebrow="">
                <span aria-hidden="true" className="size-1.5 bg-brand" />
                {version ? <span className="tabular-nums">V{version}</span> : null}
                {version ? <span aria-hidden="true" className="h-px w-4 bg-border" /> : null}
                <span>{t('landing.eyebrow')}</span>
              </p>
              <h1 className="text-3xl leading-tight font-semibold tracking-tight break-keep text-balance sm:text-4xl xl:text-5xl xl:leading-tight">
                {t('landing.slogan')}
              </h1>
              <p className="max-w-2xl text-base leading-relaxed text-pretty text-muted-foreground md:text-lg">{t('landing.tagline')}</p>
            </div>
            <div className="brand-enter mt-8 flex flex-col gap-4" style={{ animationDelay: 'calc(var(--duration-stagger) * 2)' }}>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                <Button nativeButton={false} render={<a href={DEMO_ENTRY} data-demo-launch="" />} className="h-11 w-full px-5 text-sm sm:w-auto">
                  <Icon icon="tl.play" data-icon="inline-start" />
                  {t('landing.launch')}
                </Button>
                <Button variant="outline" className="h-11 w-full px-5 text-sm sm:w-auto dark:bg-card dark:hover:bg-muted" disabled={!big} onClick={openSheet} data-sandbox-open=""
                  aria-describedby={big ? undefined : 'landing-sandbox-small'}>
                  <Icon icon="sandbox.mine" data-icon="inline-start" />
                  {t('landing.sandbox.open')}
                </Button>
              </div>
              <div className="flex min-h-5 flex-col gap-1.5">
                {capacity.line ? (
                  <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground tabular-nums" data-landing-capacity={capacity.line.kind}>
                    <span>{capacity.line.text}</span>
                    {capacity.line.badge ? <Badge variant="outline" data-capacity-queue="">{capacity.line.badge}</Badge> : null}
                    {queueHint ? <span data-landing-queue-hint="">{queueHint}</span> : null}
                  </p>
                ) : null}
                <p className="text-xs leading-relaxed text-muted-foreground" data-launch-hint="">{t('landing.launchHint')}</p>
                {big ? null : <p id="landing-sandbox-small" className="text-xs text-muted-foreground" data-sandbox-small="">{t('sandbox.smallWindow')}</p>}
              </div>
            </div>
          </div>
          <div className="mx-auto max-w-6xl px-4 pb-4 sm:px-6">
            <img src="/demo/drone4d-banner.jpg" alt={t('landing.heroAlt')} width={2400} height={900} fetchPriority="high" decoding="async"
              sizes="(min-width: 1152px) 1104px, 100vw"
              className="brand-enter block h-auto w-full rounded-lg border border-border" style={{ animationDelay: 'calc(var(--duration-stagger) * 4)' }} />
          </div>
        </section>

        <Capabilities />
        <Showcase />
        <StackRoadmap />
        <OpenSource />
      </main>

      {sheetLoaded ? (
        <React.Suspense fallback={null}>
          <SandboxLaunchSheet open={sheet} onOpenChange={setSheet} />
        </React.Suspense>
      ) : null}
      <LandingFooter />
    </div>
  )
}

/** mount the landing page into #root (app/demo/entry.tsx) */
export function renderLanding(el: HTMLElement): void {
  setLocale(initialLocale())
  createRoot(el).render(<Landing />)
}
