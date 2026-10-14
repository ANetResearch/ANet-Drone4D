// The scroll narrative of the landing page (D2.2 landing story, designed with gpt-6-sol, copy polished with
// gemini-4-argon and gpt-6-sol, illustrations by gpt-image-2.5-sunburst): an index chapter and seven capability chapters
// beside a sticky voxel stage on which one airplane flies through the whole story (./LandingVoxel, ./voxelField).
// Desktop (>= 1024 px): text column left, stage column right, sticky below the header. Phones and tablets: the stage is
// a sticky band at the top and the text runs below it. Every [data-story-chapter] element is a chapter the stage reads.
import { useT } from './LandingI18n'
import { Badge } from '@/ui/components/ui/badge'
import { Icon } from '@/ui/icons/Icon'
import { STORY, type StoryChapter } from './LandingContent'
import { StatusBadge } from './LandingSections'
import { LandingVoxel } from './LandingVoxel'

function Chapter({ c }: { c: StoryChapter }) {
  const t = useT()
  const k = `landing.story.${c.id}`
  return (
    <article id={`story-${c.id}`} data-story-chapter={c.id} className="landing-chapter scroll-mt-14" aria-labelledby={`story-${c.id}-title`}>
      <div className="landing-reveal flex min-w-0 flex-col gap-4 md:gap-5">
        <p className="flex items-center gap-2 font-mono text-xs tracking-widest text-muted-foreground uppercase">
          <Icon icon={c.icon} className="size-3.5 text-brand-text" aria-hidden="true" />
          {t(`${k}.eyebrow`)}
        </p>
        <h3 id={`story-${c.id}-title`} className="text-2xl leading-tight font-semibold tracking-tight text-balance md:text-3xl">
          {t(`${k}.title`)}
        </h3>
        <p className="text-sm leading-relaxed text-pretty text-muted-foreground md:text-base">{t(`${k}.body`)}</p>
        <ul className="flex flex-col border-y border-border" data-story-points="">
          {c.points.map((status, i) => (
            <li key={i} className="flex items-start justify-between gap-3 border-b border-border py-2.5 text-sm last:border-b-0">
              <span className="min-w-0 text-pretty">{t(`${k}.p${i + 1}`)}</span>
              <StatusBadge status={status} />
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-1.5" data-story-keywords="">
          {c.keywords.map((kw) => (
            <Badge key={kw} variant="secondary" className="font-mono text-xs font-normal tracking-wide">{kw}</Badge>
          ))}
        </div>
        {c.image ? (
          <figure className="flex flex-col gap-2" data-story-figure={c.id}>
            <img src={`/demo/story/${c.image}.webp`} srcSet={`/demo/story/${c.image}-720.webp 720w, /demo/story/${c.image}.webp 1280w`}
              sizes="(min-width: 1024px) 40vw, 100vw" width={1280} height={853} loading="lazy" decoding="async" alt={t(`${k}.alt`)}
              className="block h-auto w-full rounded-lg border border-border bg-card" />
            <figcaption className="text-xs text-muted-foreground">{t('landing.story.concept')}</figcaption>
          </figure>
        ) : null}
      </div>
    </article>
  )
}

export function LandingStory() {
  const t = useT()
  return (
    <section id="story" className="landing-story scroll-mt-14" aria-labelledby="story-title" data-landing-story="">
      <div className="landing-story-grid mx-auto max-w-7xl px-4 sm:px-6">
        <div className="landing-stage" aria-hidden="true">
          <LandingVoxel />
        </div>
        <div className="landing-story-text min-w-0">
          <header data-story-chapter="hero" className="landing-chapter landing-chapter-intro">
            <div className="landing-reveal flex flex-col gap-4">
              <p className="flex items-center gap-2 font-mono text-xs tracking-widest text-muted-foreground uppercase">
                <span aria-hidden="true" className="size-1.5 bg-brand" />
                {t('landing.story.eyebrow')}
              </p>
              <h2 id="story-title" className="text-2xl leading-tight font-semibold tracking-tight text-balance md:text-3xl">{t('landing.story.title')}</h2>
              <p className="text-sm leading-relaxed text-pretty text-muted-foreground md:text-base">{t('landing.story.sub')}</p>
              <ol className="flex flex-col border-y border-border" data-story-index="">
                {STORY.map((c, i) => (
                  <li key={c.id} className="border-b border-border last:border-b-0">
                    <a href={`#story-${c.id}`} className="flex min-h-11 items-center gap-3 py-2 text-sm hover:text-foreground">
                      <span className="w-6 shrink-0 font-mono text-xs text-brand-text tabular-nums">{String(i + 1).padStart(2, '0')}</span>
                      <span className="min-w-0 flex-1 text-pretty">{t(`landing.story.${c.id}.title`)}</span>
                      <Icon icon="chev.right" className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    </a>
                  </li>
                ))}
              </ol>
            </div>
          </header>
          {STORY.map((c) => <Chapter key={c.id} c={c} />)}
        </div>
      </div>
    </section>
  )
}
