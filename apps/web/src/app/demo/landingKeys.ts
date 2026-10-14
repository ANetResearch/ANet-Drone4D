// Explicit allowlist of the i18n keys the landing page's eager surface needs (D2.2-FX-landing): every literal t()
// call of Landing.tsx, LandingHeader.tsx, LandingSections.tsx and LandingStory.tsx, the story chapters (with their point
// counts), the media ids and the delivery statuses of LandingContent.ts (read by hand, since LandingContent.ts cannot run outside Vite: it reads
// import.meta.env transitively through lib/demo.ts), and the capacity.* keys app/boot/capacityText.ts's
// capacityTexts() reads (the same keys the boot mask and the lazily loaded start Sheet use). vite.config.ts's
// virtual:awr-landing-i18n plugin filters zh-CN.json and en.json down to this list at build time, so the committed
// dictionaries stay the single source of truth and are never duplicated by hand (LandingI18n.ts, the landing page's
// own trimmed i18n runtime). apps/web/tests/m15/landingDict.test.ts re-derives the dynamic part of this list from
// LandingContent.ts's real exports (inside Vitest, where import.meta.env exists) and fails if it has drifted from
// the ids below, so a capability, media item, stack row or roadmap entry added without updating this file is caught
// instead of silently falling back to the key name in English and to zh-CN's text in Chinese.
/** STORY chapters of LandingContent.ts with their number of points (landing.story.<id>.p1 .. pN) */
const STORY_POINTS: readonly (readonly [string, number])[] = [['digital-twin', 3], ['environment', 3], ['fleet-control', 3],
  ['perception', 3], ['relay-agents', 3], ['sandbox', 3], ['roadmap', 4]]
const MEDIA_KEYS = ['flight', 'hero', 'swarm', 'follow', 'perf', 'streaming', 'weather']
const STATUSES = ['available', 'dev', 'research']

const STATIC_KEYS = [
  'landing.docTitle', 'landing.eyebrow', 'landing.slogan', 'landing.tagline', 'landing.launch', 'landing.sandbox.open',
  'landing.launchHint', 'sandbox.smallWindow', 'landing.heroAlt', 'landing.lang', 'landing.lang.zh', 'landing.lang.en',
  'landing.repo', 'landing.docs', 'landing.api', 'brand.product', 'landing.badge', 'landing.nav.title', 'landing.nav.menu',
  'landing.nav.close', 'landing.media.sub', 'landing.media.title', 'landing.nav.showcase', 'landing.open.citeSwitch',
  'landing.open.copied', 'landing.open.copy', 'landing.nav.open', 'landing.open.sub', 'landing.open.title',
  'landing.open.license', 'landing.open.links', 'landing.footer.anet', 'landing.open.data', 'landing.footer.license',
  'landing.footer.copyright', 'landing.nav.capabilities', 'landing.story.eyebrow', 'landing.story.title', 'landing.story.sub',
  'landing.story.concept', 'landing.closing.title', 'landing.closing.body',
  // app/boot/capacityText.ts's capacityTexts(); the same keys as the boot mask and the (lazy) start Sheet
  'capacity.open', 'capacity.viewers', 'capacity.queueViewer', 'capacity.queueSandbox', 'capacity.needQueue',
  'capacity.sandboxNeedsQueue', 'capacity.sandboxHint', 'capacity.closed', 'capacity.unavailable', 'capacity.internal',
  'capacity.eta.lt1', 'capacity.eta.min', 'capacity.eta.h', 'capacity.eta.unknown', 'capacity.etaAbout',
  'capacity.etaExpected', 'capacity.etaExpectedUnknown',
]

function landingI18nKeys(): string[] {
  const keys = new Set(STATIC_KEYS)
  for (const s of STATUSES) keys.add(`landing.status.${s}`)
  for (const [id, n] of STORY_POINTS) {
    for (const f of ['eyebrow', 'title', 'body', 'alt']) keys.add(`landing.story.${id}.${f}`)
    for (let i = 1; i <= n; i++) keys.add(`landing.story.${id}.p${i}`)
  }
  for (const m of MEDIA_KEYS) keys.add(`landing.media.${m}`)
  return [...keys].sort()
}

/** every i18n key the landing page's eager surface (and its capacity line) may read */
export const LANDING_I18N_KEYS: readonly string[] = landingI18nKeys()
