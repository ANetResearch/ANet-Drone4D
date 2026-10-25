// "Event Lab · UAV camera views" section of the landing page (V0.3.1; extension point 15, ADR-129): rendered only in the
// demo build with VITE_AWR_LAB_PUBLIC=1 (lab-public/flags.ts LAB_PUBLIC), right after the story. It shows the camera
// frames each UAV of a public sample run actually received (server-side GLB mesh rendering in the Gazebo Sim lockstep
// run; LandingContent.ts LAB_MEDIA), states that every UAV sees only its own camera, and links to the /lab/ static build
// (event replay, multi-UAV private views, Rerun inspection) and to the sample run page, both as full-page navigations
// (the /lab/ build is a separate static site, not an application route). The world mesh is CC BY 4.0: the attribution
// line names the title, author and source, licence and the modification. Images are lazy with their intrinsic sizes.
import { useT } from './LandingI18n'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { LAB_PUBLIC_ENTRY } from '@/app/lab-public/flags'
import { LAB_MEDIA, LAB_POINTS, LAB_SAMPLE, LAB_WORLD_AUTHOR, LAB_WORLD_LICENSE, labSampleUrl, sectionIndex } from './LandingContent'
import { Figure, Section } from './LandingSections'

const EXT = { target: '_blank', rel: 'noreferrer noopener' } as const

/** the attribution sentence with the author, the source and the licence as links (placeholders {author}, {source}, {license}) */
function Attribution() {
  const t = useT()
  const link = (href: string, text: string, key: number) => (
    <a key={key} href={href} {...EXT} className="underline underline-offset-2 hover:text-foreground">{text}</a>
  )
  const parts = t('landing.lab.attribution').split(/(\{author\}|\{source\}|\{license\})/)
  return (
    <p className="landing-reveal mt-6 text-xs leading-relaxed text-pretty text-muted-foreground" data-lab-attribution="">
      {parts.map((p, i) =>
        p === '{author}' ? link(LAB_WORLD_AUTHOR.url, LAB_WORLD_AUTHOR.name, i)
          : p === '{source}' ? link(LAB_WORLD_AUTHOR.url, LAB_WORLD_AUTHOR.url.replace(/^https:\/\//, ''), i)
            : p === '{license}' ? link(LAB_WORLD_LICENSE.url, LAB_WORLD_LICENSE.name, i)
              : p)}
    </p>
  )
}

export function EventLab() {
  const t = useT()
  const [lead, ...rest] = LAB_MEDIA
  return (
    <Section id="event-lab" index={sectionIndex('event-lab')} eyebrow={t('landing.nav.lab')} title={t('landing.lab.title')}
      sub={t('landing.lab.sub')} data-landing-event-lab="">
      <div className="grid grid-cols-1 items-start gap-8 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] xl:gap-12">
        <div className="landing-reveal flex min-w-0 flex-col gap-6">
          <p className="font-mono text-xs text-muted-foreground" data-lab-meta="">{t('landing.lab.meta', LAB_SAMPLE)}</p>
          <ul className="flex flex-col border-y border-border" data-lab-points="">
            {LAB_POINTS.map((icon, i) => (
              <li key={icon} className="flex items-start gap-3 border-b border-border py-3 last:border-b-0">
                <Icon icon={icon} className="mt-0.5 size-4 shrink-0 text-brand-text" aria-hidden="true" />
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-sm font-medium text-foreground">{t(`landing.lab.p${i + 1}.title`)}</span>
                  <span className="text-sm leading-relaxed text-pretty text-muted-foreground">{t(`landing.lab.p${i + 1}.body`)}</span>
                </div>
              </li>
            ))}
          </ul>
          <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
            <Button nativeButton={false} render={<a href={LAB_PUBLIC_ENTRY} data-landing-lab-entry="section" />} className="h-11 w-full px-5 text-sm sm:w-auto">
              <Icon icon="tl.replay" data-icon="inline-start" />
              {t('landing.lab.open')}
            </Button>
            <Button variant="ghost" nativeButton={false} render={<a href={labSampleUrl()} data-landing-lab-sample="" />} className="h-11 w-full px-4 text-sm sm:w-auto">
              {t('landing.lab.sample', { id: LAB_SAMPLE.run })}
              <Icon icon="chev.right" data-icon="inline-end" />
            </Button>
          </div>
        </div>
        <Figure m={lead} sizes="(min-width: 1280px) 640px, (min-width: 1152px) 1104px, 100vw" />
      </div>
      <div className="mt-4 grid grid-cols-1 gap-4 md:mt-6 md:grid-cols-2 md:gap-6" data-lab-frames="">
        {rest.map((m) => <Figure key={m.key} m={m} frame={m.w / m.h < 1.7} sizes="(min-width: 768px) 50vw, 100vw" />)}
      </div>
      <Attribution />
    </Section>
  )
}
