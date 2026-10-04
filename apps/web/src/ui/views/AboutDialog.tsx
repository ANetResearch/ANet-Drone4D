// About (M15-FR-110, FR-026; AWR-15 §4.2; AWR-14 §5.6 "关于"; M16-FR-006 honesty; LICENSE condition 1b): the full badge
// at 320 px on the popover surface (never removed or altered), the product name "ANet Drone4D" (the ANetResearch/
// ANet-Drone4D repository) with its version, the licence summary, the repository link, the data source and citation of
// the built-in worlds (UrbanScene3D, non-commercial research use, not redistributed), the fidelity statement
// (simulated results, schematic coordinates), the copyright line and the component versions (contracts, world
// content version, session). AboutContent is the single About placement, shared by the Help > About dialog and the
// About tab of Settings. The public demo build adds the demo-site row first (read-only, synthcity only, no server-side
// GPU features; ADR-083).
import type * as React from 'react'
import { useT } from '@/app/i18n'
import { CONTRACTS_VERSION } from '@awr/contracts/layouts'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { ScrollArea } from '@/ui/components/ui/scroll-area'
import { Separator } from '@/ui/components/ui/separator'
import { Icon } from '@/ui/icons/Icon'
import { BrandBadge } from '@/ui/brand'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { useConnView } from '@/ui/shell/connView'
import { useWorld } from '@/stores/world'
import { DEMO_PUBLIC } from '@/lib/demo'
import { useModalFrameCap } from './useModalFrameCap'

export const APP_VERSION = '0.1.0'
export const REPO_URL = 'https://github.com/ANetResearch/ANet-Drone4D'
export const DATASET_URL = 'https://vcc.tech/UrbanScene3D'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </>
  )
}

function ExtLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 font-mono underline decoration-muted-foreground underline-offset-2 hover:decoration-foreground">
      {children}
      <Icon icon="external" className="size-3" />
    </a>
  )
}

/** badge (320 px), name and version, licence, repository, data source, fidelity, copyright and versions */
export function AboutContent({ showVersion = true }: { showVersion?: boolean }) {
  const t = useT()
  const world = useWorld((s) => s.worldId)
  const cv = useWorld((s) => s.contentVersion)
  const run = useConnView((s) => s.runId)
  const sessionWorld = useConnView((s) => s.sessionWorldId)
  return (
    <div className="flex flex-col gap-4 py-1" data-about="">
      <div className="flex flex-col items-center gap-2">
        <BrandBadge width={320} />
        <div className="flex items-baseline gap-2">
          <span className="text-ed-title font-bold" data-about-product="">{t('brand.product')}</span>
          <span className="text-hud-sub text-muted-foreground">{t('brand.runtime')}</span>
        </div>
        {showVersion ? <span className="font-mono text-hud-sub text-muted-foreground">{t('about.version', { version: APP_VERSION })}</span> : null}
      </div>
      <Separator />
      <dl className="grid grid-cols-[5.5rem_1fr] gap-x-4 gap-y-2.5 text-hud-sub leading-relaxed">
        {DEMO_PUBLIC ? (
          <Row label={t('demo.about')}>
            <span data-about-demo="">{t('demo.aboutSummary')}</span>
          </Row>
        ) : null}
        <Row label={t('about.license')}>
          <span data-about-license="">{t('about.licenseSummary')}</span>
        </Row>
        <Row label={t('about.repo')}>
          <ExtLink href={REPO_URL}>github.com/ANetResearch/ANet-Drone4D</ExtLink>
        </Row>
        <Row label={t('about.data')}>
          <span className="flex flex-col gap-1" data-about-data="">
            <span>{t('about.dataSummary')}</span>
            <span className="text-muted-foreground">{t('about.citation')}</span>
            <ExtLink href={DATASET_URL}>vcc.tech/UrbanScene3D</ExtLink>
            <span data-about-synthetic="">{t('about.dataSynthetic')}</span>
          </span>
        </Row>
        <Row label={t('about.fidelity')}>
          <span data-about-fidelity="">{t('about.fidelitySummary')}</span>
        </Row>
        <Row label={t('about.versions')}>
          <span className="font-mono">{t('about.versionsValue', { app: APP_VERSION, contracts: CONTRACTS_VERSION })}</span>
        </Row>
        <Row label={t('about.session')}>
          <span className="font-mono">{`${sessionWorld ?? world ?? '—'} · ${run ?? '—'}${cv ? ` · v ${cv.slice(0, 12)}` : ''}`}</span>
        </Row>
      </dl>
      <Separator />
      <p className="text-center text-hud-sub text-muted-foreground" data-about-copyright="">{t('about.copyright')}</p>
    </div>
  )
}

export function AboutDialog() {
  const t = useT()
  const open = useOverlays((s) => s.about)
  const cap = useModalFrameCap('modal:about')
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      overlays.set('about', o)
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('about.title')}</DialogTitle>
          <DialogDescription>{t('about.version', { version: APP_VERSION })}</DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[70vh]">
          <AboutContent showVersion={false} />
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}
