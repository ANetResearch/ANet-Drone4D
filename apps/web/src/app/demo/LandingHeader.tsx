// Header of the landing page (D2.2-L1): brand, demo badge, section navigation, language switch, GitHub and the primary
// action from 1280 CSS px up; below that (tablets 768-1279 and phones) a 44 px menu button opens a shadcn Sheet with the
// same navigation, the language switch, both main actions and the repository links (touch targets >= 44 px). A section
// link in the Sheet closes it first and scrolls once the close transition has finished (the modal scroll lock is gone).
import * as React from 'react'
import { useT, type Locale } from './LandingI18n'
import { DEMO_DOCS_URL, DEMO_ENTRY, DEMO_REPO_URL } from '@/lib/demo'
import { BRAND } from '@/ui/brand/assets'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/ui/components/ui/sheet'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import { API_DOCS_PATH, NAV, NAV_KEY, scrollToSection, type NavId } from './LandingContent'

export interface LandingHeaderProps {
  locale: Locale
  onLocale: (l: Locale) => void
  /** opens the sandbox start Sheet (null while the window is too narrow) */
  onSandbox: (() => void) | null
}

function LangSwitch({ locale, onLocale, touch = false }: { locale: Locale; onLocale: (l: Locale) => void; touch?: boolean }) {
  const t = useT()
  const choose = (v: unknown[]): void => {
    const l = v[0]
    if (l === 'zh-CN' || l === 'en') onLocale(l)
  }
  const item = touch ? 'h-11 min-w-20 px-4 text-sm' : undefined
  return (
    <ToggleGroup spacing={0} size="sm" variant="outline" value={[locale]} onValueChange={choose} aria-label={t('landing.lang')} data-lang-switch="">
      <ToggleGroupItem value="zh-CN" lang="zh-CN" className={item}>{t('landing.lang.zh')}</ToggleGroupItem>
      <ToggleGroupItem value="en" lang="en" className={item}>{t('landing.lang.en')}</ToggleGroupItem>
    </ToggleGroup>
  )
}

export function LandingHeader({ locale, onLocale, onSandbox }: LandingHeaderProps) {
  const t = useT()
  const [open, setOpen] = React.useState(false)
  const pending = React.useRef<(() => void) | null>(null)
  const later = (fn: () => void) => (e: React.MouseEvent) => {
    e.preventDefault()
    pending.current = fn
    setOpen(false)
  }
  const done = (o: boolean): void => {
    if (o || !pending.current) return
    const fn = pending.current
    pending.current = null
    fn()
  }
  const navLink = (id: NavId) => (e: React.MouseEvent) => {
    e.preventDefault()
    scrollToSection(id)
  }
  return (
    <header className="sticky top-0 z-(--z-header) border-b border-border bg-background">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <a href="#top" className="flex min-h-11 items-center gap-2.5 rounded-md" aria-label={t('brand.product')}
          onClick={(e) => { e.preventDefault(); scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }) }}>
          <img src={BRAND.avatar96} width={24} height={24} alt="" aria-hidden="true" className="size-6 shrink-0" />
          <span className="text-sm font-semibold tracking-tight whitespace-nowrap">{t('brand.product')}</span>
        </a>
        <Badge variant="outline" className="max-sm:hidden" data-demo-badge="">{t('landing.badge')}</Badge>
        <nav className="ml-4 hidden items-center gap-0.5 xl:flex" aria-label={t('landing.nav.title')} data-landing-nav="">
          {NAV.map((id) => (
            <Button key={id} variant="ghost" size="sm" className="h-8 px-3 text-sm text-muted-foreground hover:text-foreground" nativeButton={false}
              render={<a href={`#${id}`} onClick={navLink(id)} />}>
              {t(NAV_KEY[id])}
            </Button>
          ))}
        </nav>
        <div className="ml-auto hidden items-center gap-2 xl:flex">
          <LangSwitch locale={locale} onLocale={onLocale} />
          <Button variant="ghost" size="sm" className="h-8 px-3 text-sm" nativeButton={false}
            render={<a href={DEMO_REPO_URL} target="_blank" rel="noreferrer noopener" />}>
            GitHub
            <Icon icon="external" data-icon="inline-end" />
          </Button>
          <Button size="sm" className="h-8 px-3 text-sm" nativeButton={false} render={<a href={DEMO_ENTRY} />}>{t('landing.launch')}</Button>
        </div>
        <Button variant="ghost" size="icon" className="ml-auto size-11 xl:hidden" onClick={() => setOpen(true)}
          aria-label={t('landing.nav.menu')} aria-expanded={open} data-landing-menu="">
          <Icon icon="nav.menu" className="size-5" />
        </Button>
      </div>
      <Sheet open={open} onOpenChange={setOpen} onOpenChangeComplete={done}>
        <SheetContent side="right" showCloseButton={false} className="w-full gap-0 overflow-y-auto sm:max-w-sm" data-landing-sheet="">
          <SheetHeader className="flex-row items-center justify-between gap-3 border-b border-border px-4 py-2">
            <div className="flex min-w-0 flex-col">
              <SheetTitle className="text-sm font-semibold">{t('brand.product')}</SheetTitle>
              <SheetDescription>{t('landing.nav.title')}</SheetDescription>
            </div>
            <SheetClose render={<Button variant="ghost" size="icon" className="size-11" aria-label={t('landing.nav.close')} />}>
              <Icon icon="close" className="size-5" />
            </SheetClose>
          </SheetHeader>
          <nav className="flex flex-col px-2 py-2" aria-label={t('landing.nav.title')}>
            {NAV.map((id, i) => (
              <a key={id} href={`#${id}`} onClick={later(() => scrollToSection(id))}
                className="flex min-h-12 items-center gap-3 rounded-md px-3 text-base text-foreground hover:bg-muted">
                <span className="font-mono text-xs text-muted-foreground tabular-nums">{String(i + 1).padStart(2, '0')}</span>
                {t(NAV_KEY[id])}
              </a>
            ))}
          </nav>
          <div className="flex flex-col gap-3 border-t border-border px-4 py-4">
            <span className="text-xs text-muted-foreground">{t('landing.lang')}</span>
            <LangSwitch locale={locale} onLocale={onLocale} touch />
          </div>
          <div className="flex flex-col gap-2 border-t border-border px-4 py-4">
            <Button className="h-11 w-full text-sm" nativeButton={false} render={<a href={DEMO_ENTRY} />}>
              <Icon icon="tl.play" data-icon="inline-start" />
              {t('landing.launch')}
            </Button>
            <Button variant="outline" className="h-11 w-full text-sm" disabled={onSandbox === null}
              onClick={onSandbox ? later(onSandbox) : undefined}>
              <Icon icon="sandbox.mine" data-icon="inline-start" />
              {t('landing.sandbox.open')}
            </Button>
          </div>
          <div className="flex flex-col border-t border-border px-2 py-2">
            {[
              { href: DEMO_REPO_URL, label: t('landing.repo') },
              { href: DEMO_DOCS_URL, label: t('landing.docs') },
              { href: API_DOCS_PATH, label: t('landing.api') },
            ].map((l) => (
              <a key={l.href} href={l.href} target={l.href.startsWith('/') ? undefined : '_blank'} rel="noreferrer noopener"
                className="flex min-h-11 items-center justify-between gap-3 rounded-md px-3 text-sm text-muted-foreground hover:bg-muted hover:text-foreground">
                {l.label}
                <Icon icon={l.href.startsWith('/') ? 'chev.right' : 'external'} className="size-4" />
              </a>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </header>
  )
}
