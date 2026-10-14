// Sections of the landing page around the story (D2.2): captured media, open source and citation, footer, and the
// shared status badge and section frame. Status labels are the same three in every place
// (Available, In development, Research) and follow the V0.2.0 release and the D3 design baseline (docs/05). Images are
// lazy with their intrinsic sizes (no layout shift); grids keep min-w-0 so code blocks scroll inside their card.
import * as React from 'react'
import { useT } from './LandingI18n'
import { cn } from '@/lib/utils'
import { DEMO_DOCS_URL, DEMO_REPO_URL } from '@/lib/demo'
import { BRAND } from '@/ui/brand/assets'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Separator } from '@/ui/components/ui/separator'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import {
  ANET_URL, API_DOCS_PATH, CITATIONS, MEDIA_FLIGHT, MEDIA_SHOTS, MEDIA_WIDE, type CapStatus, type Media,
} from './LandingContent'

const COPIED_MS = 1600

export function StatusBadge({ status, className }: { status: CapStatus; className?: string }) {
  const t = useT()
  return (
    <Badge variant="outline" className={cn('shrink-0 gap-1.5 font-normal text-muted-foreground', className)} data-status={status}>
      <span aria-hidden="true" className={cn('size-1.5 shrink-0 rounded-full',
        status === 'available' ? 'bg-brand' : status === 'dev' ? 'ring-1 ring-foreground ring-inset' : 'bg-muted-foreground')} />
      {t(`landing.status.${status}`)}
    </Badge>
  )
}

export function Section({ id, index, eyebrow, title, sub, children, ...rest }: {
  id: string; index: string; eyebrow: string; title: string; sub?: string; children: React.ReactNode
} & Record<`data-${string}`, string>) {
  return (
    <section id={id} className="scroll-mt-14 py-16 md:py-24" aria-labelledby={`${id}-title`} {...rest}>
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <header className="landing-reveal mb-8 flex max-w-3xl flex-col gap-3 md:mb-12">
          <p className="flex items-center gap-2 font-mono text-xs tracking-widest text-muted-foreground uppercase">
            <span className="text-brand-text tabular-nums">{index}</span>
            <span aria-hidden="true" className="h-px w-6 bg-border" />
            {eyebrow}
          </p>
          <h2 id={`${id}-title`} className="text-2xl font-semibold tracking-tight text-balance md:text-3xl">{title}</h2>
          {sub ? <p className="text-sm leading-relaxed text-pretty text-muted-foreground md:text-base">{sub}</p> : null}
        </header>
        {children}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------- showcase
// D2.2-FX-landing (D2.2-R-landing §2.5): a lazy-loaded capture occasionally stayed an unexplained blank muted block
// (intermittent, not every load; a direct re-fetch of the same image completed normally, so most likely a native
// loading="lazy" timing quirk under a fast or programmatic scroll rather than a real network failure). data-media-loaded
// makes that loading state explicit and inspectable (it was already visually a neutral bg-muted block, just with no
// signal that it was still pending rather than finished or broken); a resident animated skeleton (animate-pulse) was
// considered but the app is already at its two-loop budget (ShimmerText, MatrixLoader; motion-lint MOT-03, ADR-029),
// so this stays a plain static block rather than adding a third. onError retries the request once before falling
// back to the browser's own broken-image presentation (the alt text stays readable either way).
function Figure({ m, wide = false, sizes }: { m: Media; wide?: boolean; sizes: string }) {
  const t = useT()
  const text = t(`landing.media.${m.key}`)
  const [loaded, setLoaded] = React.useState(false)
  const retried = React.useRef(false)
  const onError = (e: React.SyntheticEvent<HTMLImageElement>): void => {
    if (retried.current) return
    retried.current = true
    e.currentTarget.src = `/demo/${m.file}?retry=1`
  }
  return (
    <figure className={cn('landing-reveal flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card', wide && 'md:col-span-2')}
      data-media={m.key}>
      <img src={`/demo/${m.file}`} alt={text} width={m.w} height={m.h} sizes={sizes} loading="lazy" decoding="async"
        data-media-loaded={loaded} onLoad={() => setLoaded(true)} onError={onError}
        className="block h-auto w-full bg-muted" />
      <figcaption className="border-t border-border px-4 py-3 text-xs leading-relaxed text-pretty text-muted-foreground">{text}</figcaption>
    </figure>
  )
}

export function Showcase() {
  const t = useT()
  return (
    <Section id="showcase" index="02" eyebrow={t('landing.nav.showcase')} title={t('landing.media.title')} sub={t('landing.media.sub')}
      data-landing-media="">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
        <Figure m={MEDIA_FLIGHT} wide sizes="(min-width: 1152px) 1104px, 100vw" />
        {MEDIA_SHOTS.map((m) => <Figure key={m.key} m={m} sizes="(min-width: 768px) 50vw, 100vw" />)}
        {MEDIA_WIDE.map((m) => <Figure key={m.key} m={m} wide sizes="(min-width: 1152px) 1104px, 100vw" />)}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------- open source and citation
function LinkRow({ href, icon, label, external = true }: { href: string; icon: IconKey; label: string; external?: boolean }) {
  return (
    <a href={href} target={external ? '_blank' : undefined} rel={external ? 'noreferrer noopener' : undefined}
      className="group flex min-h-12 items-center gap-3 border-b border-border px-1 text-sm text-foreground last:border-b-0 hover:text-brand-text">
      <Icon icon={icon} className="size-4 text-muted-foreground" />
      <span className="min-w-0 flex-1">{label}</span>
      <Icon icon={external ? 'external' : 'chev.right'} className="size-4 text-muted-foreground" />
    </a>
  )
}

function Citation() {
  const t = useT()
  const [which, setWhich] = React.useState<string>(CITATIONS[0].id)
  const [copied, setCopied] = React.useState(false)
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null)
  React.useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  const entry = CITATIONS.find((c) => c.id === which) ?? CITATIONS[0]
  const copy = (): void => {
    const done = (): void => {
      setCopied(true)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), COPIED_MS)
    }
    void navigator.clipboard?.writeText(entry.bibtex).then(done, () => setCopied(false))
  }
  return (
    <div className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card" data-landing-citation="">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[which]} aria-label={t('landing.open.citeSwitch')}
          onValueChange={(v: unknown[]) => { if (typeof v[0] === 'string') setWhich(v[0]) }}>
          {CITATIONS.map((c) => <ToggleGroupItem key={c.id} value={c.id} className="h-11 px-3 text-xs xl:h-8">{c.label}</ToggleGroupItem>)}
        </ToggleGroup>
        <Button variant="ghost" className="h-11 px-3 text-xs xl:h-8" onClick={copy} data-cite-copy="">
          <Icon icon={copied ? 'check' : 'copy'} data-icon="inline-start" />
          {copied ? t('landing.open.copied') : t('landing.open.copy')}
        </Button>
      </div>
      <pre className="overflow-x-auto px-4 py-4 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-card-foreground" tabIndex={0}
        aria-label={entry.label}><code>{entry.bibtex}</code></pre>
    </div>
  )
}

export function OpenSource() {
  const t = useT()
  return (
    <Section id="open-source" index="04" eyebrow={t('landing.nav.open')} title={t('landing.open.title')} sub={t('landing.open.sub')}
      data-landing-open="">
      <div className="grid grid-cols-1 gap-10 xl:grid-cols-2 xl:gap-12">
        <div className="landing-reveal flex min-w-0 flex-col gap-6">
          <p className="text-sm leading-relaxed text-pretty text-foreground md:text-base">{t('landing.open.license')}</p>
          <nav className="flex flex-col border-y border-border" aria-label={t('landing.open.links')}>
            <LinkRow href={DEMO_REPO_URL} icon="link" label={t('landing.repo')} />
            <LinkRow href={DEMO_DOCS_URL} icon="data.file" label={t('landing.docs')} />
            <LinkRow href={API_DOCS_PATH} icon="api.doc" label={t('landing.api')} external={false} />
            <LinkRow href={ANET_URL} icon="agent.network" label={t('landing.footer.anet')} />
          </nav>
        </div>
        <div className="landing-reveal flex min-w-0 flex-col gap-4">
          <Citation />
          <p className="text-xs leading-relaxed text-pretty text-muted-foreground">{t('landing.open.data')}</p>
        </div>
      </div>
    </Section>
  )
}

export function LandingFooter() {
  const t = useT()
  return (
    <footer className="relative border-t border-border bg-background">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-8 text-xs text-muted-foreground sm:px-6">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-2.5 text-foreground">
            <img src={BRAND.avatar96} width={20} height={20} alt="" aria-hidden="true" className="size-5" loading="lazy" />
            <span className="text-sm font-semibold tracking-tight">{t('brand.product')}</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-5">
            <a href={DEMO_REPO_URL} target="_blank" rel="noreferrer noopener" className="inline-flex min-h-11 min-w-11 items-center hover:text-foreground">GitHub</a>
            <a href={DEMO_DOCS_URL} target="_blank" rel="noreferrer noopener" className="inline-flex min-h-11 min-w-11 items-center hover:text-foreground">{t('landing.docs')}</a>
            <a href={API_DOCS_PATH} className="inline-flex min-h-11 min-w-11 items-center hover:text-foreground">{t('landing.api')}</a>
            <a href={ANET_URL} target="_blank" rel="noreferrer noopener" className="inline-flex min-h-11 min-w-11 items-center hover:text-foreground">{t('landing.footer.anet')}</a>
          </div>
        </div>
        <Separator />
        <div className="flex flex-col gap-1.5 md:flex-row md:justify-between md:gap-6">
          <p>{t('landing.footer.license')}</p>
          <p data-landing-copyright="">{t('landing.footer.copyright')}</p>
        </div>
      </div>
    </footer>
  )
}
