// Public demo landing page, "/" of the VITE_AWR_DEMO=public build (ADR-083; M15-FR-121; AWR-19 §3.7 T4). Rendered by
// app/demo/entry.tsx before (and instead of) the application: React, shadcn (base-mira) parts, morphicons icons and the
// i18n dictionaries only, so the first screen loads neither three nor the engine nor the realtime client. "Launch demo"
// is a plain link to /world/synthcity (a full page load that boots the application with its mask). Content: brand
// banner, one-line positioning, real captures of synthcity (screenshots and the S0 recording, lazy below the fold), the
// four highlights, the demo notes (public read-only, synthcity generated, no server-side GPU features, frame rate on the
// visitor's GPU), a lieflat table of what runs here, repository and docs links, and a zh-CN / English switch kept in this
// browser. Colours are theme tokens (ANet Graphite, dark); the entrance uses the brand-enter recipe (transitions.dev).
import * as React from 'react'
import { createRoot } from 'react-dom/client'
import { setLocale, useT, type Locale } from '@/app/i18n'
import { DEMO_DOCS_URL, DEMO_ENTRY, DEMO_REPO_URL } from '@/lib/demo'
import { BRAND } from '@/ui/brand/assets'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Separator } from '@/ui/components/ui/separator'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'

const LANG_KEY = 'awr.demo.lang'
const ANET_URL = 'https://github.com/ANetResearch/ANet'

function initialLocale(): Locale {
  try {
    const v = globalThis.localStorage?.getItem(LANG_KEY)
    if (v === 'zh-CN' || v === 'en') return v
  } catch {
    // storage unavailable (private mode): follow the browser language
  }
  return /^zh\b/i.test(globalThis.navigator?.language ?? 'zh-CN') ? 'zh-CN' : 'en'
}

const FEATURES: readonly { id: string; icon: IconKey }[] = [
  { id: 'stream', icon: 'layer.pointcloud' },
  { id: 'env', icon: 'env.field' },
  { id: 'fleet', icon: 'cmd.formation' },
  { id: 'anet', icon: 'agent.network' },
]
const NOTES: readonly { id: string; icon: IconKey }[] = [
  { id: 'readonly', icon: 'layer.visible' },
  { id: 'synth', icon: 'layer.building' },
  { id: 'server', icon: 'data.server' },
  { id: 'gpu', icon: 'perf.gpu' },
]
const SHOTS: readonly { file: string; key: string; w: number; h: number }[] = [
  { file: 'screenshot-hero.jpg', key: 'hero', w: 1920, h: 1080 },
  { file: 'screenshot-swarm.jpg', key: 'swarm', w: 1920, h: 1080 },
  { file: 'screenshot-follow.jpg', key: 'follow', w: 1920, h: 1080 },
  { file: 'screenshot-perf.jpg', key: 'perf', w: 1920, h: 1080 },
]
const WIDE: readonly { file: string; key: string }[] = [
  { file: 'screenshot-streaming.jpg', key: 'streaming' },
  { file: 'screenshot-weather.jpg', key: 'weather' },
]
const SPEC = ['world', 'scenario', 'weather', 'server', 'client'] as const

function ExtButton({ href, children, variant = 'outline' }: { href: string; children: React.ReactNode; variant?: 'outline' | 'ghost' }) {
  return (
    <Button variant={variant} size="lg" nativeButton={false} render={<a href={href} target="_blank" rel="noreferrer noopener" />}>
      {children}
      <Icon icon="external" data-icon="inline-end" />
    </Button>
  )
}

function Figure({ src, alt, caption, w, h }: { src: string; alt: string; caption: string; w: number; h: number }) {
  return (
    <figure className="flex flex-col gap-2">
      <img src={src} alt={alt} width={w} height={h} loading="lazy" decoding="async" className="h-auto w-full rounded-md ring-1 ring-foreground/10" />
      <figcaption className="text-ed-sub text-muted-foreground">{caption}</figcaption>
    </figure>
  )
}

function SectionTitle({ children, sub }: { children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="mb-4 flex flex-col gap-1">
      <h2 className="text-xl font-semibold tracking-tight text-foreground">{children}</h2>
      {sub ? <p className="text-ed-sub text-muted-foreground">{sub}</p> : null}
    </div>
  )
}

export function Landing() {
  const t = useT()
  const [locale, setLoc] = React.useState<Locale>(initialLocale)
  React.useLayoutEffect(() => {
    setLocale(locale)
    document.title = t('landing.docTitle')
  }, [locale, t])
  const choose = (v: unknown[]) => {
    const l = v[0]
    if (l !== 'zh-CN' && l !== 'en') return
    setLoc(l)
    try {
      globalThis.localStorage?.setItem(LANG_KEY, l)
    } catch {
      // not persisted in private mode
    }
  }
  const launch = (
    <Button size="lg" nativeButton={false} render={<a href={DEMO_ENTRY} data-demo-launch="" />} className="h-10 px-4 text-sm">
      <Icon icon="tl.play" data-icon="inline-start" />
      {t('landing.launch')}
    </Button>
  )
  return (
    <div data-view="landing" className="min-h-full bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b bg-background">
        <div className="mx-auto flex h-12 max-w-6xl items-center gap-3 px-6">
          <img src={BRAND.avatar96} width={24} height={24} alt="" aria-hidden="true" className="size-6 shrink-0" />
          <span className="text-hud-title font-semibold whitespace-nowrap">{t('brand.product')}</span>
          <Badge variant="outline" data-demo-badge="">{t('landing.badge')}</Badge>
          <div className="ml-auto flex items-center gap-2">
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[locale]} onValueChange={choose} aria-label={t('landing.lang')} data-lang-switch="">
              <ToggleGroupItem value="zh-CN" lang="zh-CN">{t('landing.lang.zh')}</ToggleGroupItem>
              <ToggleGroupItem value="en" lang="en">{t('landing.lang.en')}</ToggleGroupItem>
            </ToggleGroup>
            <Button variant="ghost" size="sm" nativeButton={false} render={<a href={DEMO_REPO_URL} target="_blank" rel="noreferrer noopener" />}>
              <Icon icon="external" data-icon="inline-start" />
              GitHub
            </Button>
            <Button size="sm" nativeButton={false} render={<a href={DEMO_ENTRY} />}>{t('landing.launch')}</Button>
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-6xl flex-col gap-16 px-6 py-10">
        <section className="flex flex-col gap-8" data-landing-hero="">
          <img src="/demo/drone4d-banner.jpg" alt={t('landing.heroAlt')} width={2400} height={900} fetchPriority="high"
            className="brand-enter h-auto w-full rounded-lg ring-1 ring-foreground/10" />
          <div className="brand-enter flex flex-col gap-4" style={{ animationDelay: 'calc(var(--duration-stagger) * 2)' }}>
            <h1 className="text-4xl font-bold tracking-tight text-balance">{t('landing.slogan')}</h1>
            <p className="max-w-3xl text-base leading-relaxed text-pretty text-muted-foreground">{t('landing.tagline')}</p>
            <div className="flex flex-wrap items-center gap-3 pt-2">
              {launch}
              <ExtButton href={DEMO_REPO_URL}>{t('landing.repo')}</ExtButton>
              <ExtButton href={DEMO_DOCS_URL} variant="ghost">{t('landing.docs')}</ExtButton>
            </div>
            <p className="text-ed-sub text-muted-foreground" data-launch-hint="">{t('landing.launchHint')}</p>
          </div>
        </section>

        <section data-landing-media="">
          <SectionTitle sub={t('landing.media.sub')}>{t('landing.media.title')}</SectionTitle>
          <div className="flex flex-col gap-6">
            <Figure src="/demo/demo-flight.webp" alt={t('landing.media.flight')} caption={t('landing.media.flight')} w={960} h={540} />
            <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
              {SHOTS.map((s) => <Figure key={s.key} src={`/demo/${s.file}`} alt={t(`landing.media.${s.key}`)} caption={t(`landing.media.${s.key}`)} w={s.w} h={s.h} />)}
            </div>
            {WIDE.map((s) => <Figure key={s.key} src={`/demo/${s.file}`} alt={t(`landing.media.${s.key}`)} caption={t(`landing.media.${s.key}`)} w={1920} h={613} />)}
          </div>
        </section>

        <section data-landing-features="">
          <SectionTitle>{t('landing.features.title')}</SectionTitle>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map((f) => (
              <Card key={f.id} data-feature={f.id}>
                <CardHeader>
                  <Icon icon={f.icon} className="mb-2 size-5 text-brand-text" />
                  <CardTitle>{t(`landing.f.${f.id}.title`)}</CardTitle>
                  <CardDescription className="leading-relaxed">{t(`landing.f.${f.id}.body`)}</CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        </section>

        <section className="grid grid-cols-1 gap-8 lg:grid-cols-2" data-landing-notes="">
          <div>
            <SectionTitle>{t('landing.notes.title')}</SectionTitle>
            <Card>
              <CardContent className="flex flex-col gap-4">
                {NOTES.map((n) => (
                  <div key={n.id} className="flex gap-3" data-note={n.id}>
                    <Icon icon={n.icon} className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    <p className="text-sm leading-relaxed text-card-foreground">{t(`landing.notes.${n.id}`)}</p>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
          <div>
            <SectionTitle>{t('landing.spec.title')}</SectionTitle>
            <div data-lf-table="" data-figure="landing-spec">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-24">{t('landing.spec.item')}</TableHead>
                    <TableHead>{t('landing.spec.value')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {SPEC.map((k) => (
                    <TableRow key={k}>
                      <TableCell className="text-muted-foreground">{t(`landing.spec.${k}`)}</TableCell>
                      <TableCell className="whitespace-normal">{t(`landing.spec.${k}Value`)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            <div className="mt-6">{launch}</div>
          </div>
        </section>
      </main>

      <footer className="border-t">
        <div className="mx-auto flex max-w-6xl flex-col gap-3 px-6 py-6 text-ed-sub text-muted-foreground">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <a href={DEMO_REPO_URL} target="_blank" rel="noreferrer noopener" className="underline decoration-muted-foreground underline-offset-2 hover:text-foreground">
              github.com/ANetResearch/ANet-Drone4D
            </a>
            <a href={DEMO_DOCS_URL} target="_blank" rel="noreferrer noopener" className="underline decoration-muted-foreground underline-offset-2 hover:text-foreground">
              {t('landing.docs')}
            </a>
            <a href={ANET_URL} target="_blank" rel="noreferrer noopener" className="underline decoration-muted-foreground underline-offset-2 hover:text-foreground">
              {t('landing.footer.anet')}
            </a>
          </div>
          <Separator />
          <p>{t('landing.footer.license')}</p>
          <p data-landing-copyright="">{t('landing.footer.copyright')}</p>
        </div>
      </footer>
    </div>
  )
}

/** mount the landing page into #root (app/demo/entry.tsx) */
export function renderLanding(el: HTMLElement): void {
  setLocale(initialLocale())
  createRoot(el).render(<Landing />)
}
