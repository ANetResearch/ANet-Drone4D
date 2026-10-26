// Static content of the public demo landing page (D2.2). Texts live in the i18n dictionaries under landing.*; this module
// holds the structure: the chapters of the scroll narrative with the delivery status of each point (V0.1, V0.2 and V0.3
// delivered; the 3DGS appearance layer and PX4 SITL with Gazebo in research, docs/impl/D3-交付总结.md; the cross-entity
// collaborative experimentation platform of ANet4D in concept design, docs/06), the captured media
// with their intrinsic sizes and the citation entries. Keyword labels are the official spellings, the same in both locales.
// V0.3.1: with VITE_AWR_LAB_PUBLIC=1 (lab-public/flags.ts LAB_PUBLIC; extension point 15, ADR-129) the page also shows the
// Event Lab entries: the "event-lab" section (per-UAV camera frames of a public sample run, LAB_MEDIA), its navigation slot
// and the links to the /lab/ static build. Without the switch nothing of it is reachable and the bundler drops it.
import type { IconKey } from '@/ui/icons/registry'
import { URBANSCENE_BIBTEX } from '@/lib/demo'
import { REPO_URL } from '@/lib/site'
import { LAB_PUBLIC, LAB_PUBLIC_ENTRY } from '@/app/lab-public/flags'
import LAB_SAMPLES_DATA from './labSamples.json'

export type CapStatus = 'available' | 'dev' | 'research'

/** one chapter of the scroll narrative (D2.2 landing story, designed with gpt-6-sol): texts under landing.story.<id>.*
 * (eyebrow, title, body, p1..pN), the delivery status of each point, the concept illustration (public/demo/story/<id>.webp,
 * generated with gpt-image-2.5-sunburst) and the promoted keywords. The voxel airplane's keyframes per chapter live in
 * ./storyScene (read only by the lazily loaded stage renderer), keyed by the same ids. */
export interface StoryChapter {
  id: string
  icon: IconKey
  points: readonly CapStatus[]
  image: string | null
  /** official keyword labels, the same in both locales */
  keywords: readonly string[]
}

export const STORY: readonly StoryChapter[] = [
  { id: 'digital-twin', icon: 'layer.pointcloud', points: ['available', 'available', 'available'], image: 'digital-twin', keywords: ['Digital Twin', 'Simulation'] },
  { id: 'environment', icon: 'env.wind', points: ['available', 'available', 'available'], image: 'environment', keywords: ['Simulation'] },
  { id: 'fleet-control', icon: 'cmd.formation', points: ['available', 'available', 'available'], image: 'fleet-control', keywords: ['Drone Swarm', 'Swarm Control', 'PX4', 'Simulation'] },
  { id: 'perception', icon: 'mission.target', points: ['available', 'available', 'available'], image: 'perception', keywords: ['LiDAR', 'Simulation'] },
  { id: 'relay-agents', icon: 'agent.network', points: ['available', 'available', 'available'], image: 'relay-agents', keywords: ['Agent Swarm', 'Swarm Control'] },
  { id: 'sandbox', icon: 'sandbox.mine', points: ['available', 'available', 'available'], image: null, keywords: ['Simulation', 'Drone Swarm'] },
  { id: 'roadmap', icon: 'sensor.lidar', points: ['available', 'available', 'research', 'research', 'research'], image: 'roadmap', keywords: ['Gazebo', 'LiDAR', '4D Reconstruction', '3DGS', 'PX4'] },
]

export interface Media { file: string; key: string; w: number; h: number }
/** the S0 recording (animated WebP, 200 frames) */
export const MEDIA_FLIGHT: Media = { file: 'demo-flight.webp', key: 'flight', w: 960, h: 540 }
/** 16:9 captures of the V0.1 build in synthcity */
export const MEDIA_SHOTS: readonly Media[] = [
  { file: 'screenshot-hero.jpg', key: 'hero', w: 1920, h: 1080 },
  { file: 'screenshot-swarm.jpg', key: 'swarm', w: 1920, h: 1080 },
  { file: 'screenshot-follow.jpg', key: 'follow', w: 1920, h: 1080 },
  { file: 'screenshot-perf.jpg', key: 'perf', w: 1920, h: 1080 },
]
/** wide sequences (three frames side by side) */
export const MEDIA_WIDE: readonly Media[] = [
  { file: 'screenshot-streaming.jpg', key: 'streaming', w: 1920, h: 613 },
  { file: 'screenshot-weather.jpg', key: 'weather', w: 1920, h: 613 },
]

/** per-UAV camera frames of the public sample run (public/demo/lab/; captions landing.media.<key>): what the agents actually
 * received, rendered server-side from the GLB mesh of oryahovo-crop1 during the Gazebo Sim lockstep run of event
 * crop-handoff-001. The first is the lead image beside the section text; the junction and thermal frames are the same moment */
export const LAB_MEDIA: readonly Media[] = [
  { file: 'lab/lab-oblique.webp', key: 'lab-oblique', w: 1600, h: 900 },
  { file: 'lab/lab-junction.webp', key: 'lab-junction', w: 1600, h: 900 },
  { file: 'lab/lab-thermal.webp', key: 'lab-thermal', w: 640, h: 512 },
  { file: 'lab/lab-residential.webp', key: 'lab-residential', w: 1600, h: 900 },
  { file: 'lab/lab-blocks.webp', key: 'lab-blocks', w: 1600, h: 900 },
]
/** the sample run the frames come from, with its event and world (landing.lab.meta) */
export const LAB_SAMPLE = { run: 'run-koyqeft6xe', event: 'crop-handoff-001', world: 'oryahovo-crop1' } as const
/** one public sample run of the gallery (thumbnail under /demo/lab/samples/<run>.webp in the main site's own assets; the cards link to the
 * sample run page of the /lab/ static build, a full-page navigation) */
export interface LabSample {
  /** public run id, run-[a-z2-7]{10} */
  run: string
  event: string
  eventType: string
  world: string
  policy: string
  nUav: number
  obsLevel: 'L1' | 'L2'
  thumb: string
  thumbW: number
  thumbH: number
  /** one factual sentence on what the run shows */
  caption?: { en: string; zh: string }
}
/** the public sample runs in display order (data file ./labSamples.json, which keeps the Chinese captions out of the sources, I18N-01);
 * the first is the featured run of LAB_SAMPLE */
export const LAB_SAMPLES: readonly LabSample[] = LAB_SAMPLES_DATA as readonly LabSample[]
/** the sample run page of one run in the /lab/ static build; a function for the same reason as labSampleUrl */
export function labRunUrl(run: string): string {
  return `${LAB_PUBLIC_ENTRY}samples/${run}`
}
/** the sample run page of the /lab/ static build (full-page navigation, not an application route); a function, so that
 * the build without VITE_AWR_LAB_PUBLIC drops it with every other Event Lab entry (the K9 D3 scan finds no "/lab/") */
export function labSampleUrl(): string {
  return `${LAB_PUBLIC_ENTRY}samples/${LAB_SAMPLE.run}`
}
/** the three points of the section (landing.lab.p<n>.title / .body) */
export const LAB_POINTS: readonly IconKey[] = ['sensor.camera', 'layer.mesh', 'tl.replay']
/** CC BY 4.0 attribution of the world mesh (TASL: title in the text, author and source, licence, "modified") */
export const LAB_WORLD_AUTHOR = { name: 'Mitko', url: 'https://sketchfab.com/bg3d' } as const
export const LAB_WORLD_LICENSE = { name: 'CC BY 4.0', url: 'https://creativecommons.org/licenses/by/4.0/' } as const

/** BibTeX of the software (README.md "Data and citation", CITATION.cff); the product was named ANet Drone4D up to V0.3.1;
 * the repository is ANetResearch/ANet4D (lib/site.ts) */
export const ANET4D_BIBTEX = `@software{anet4d_2026,
  title   = {ANet4D: Simulation Infrastructure for Collaborative Embodied Agents},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.3.4},
  url     = {${REPO_URL}}
}`

export const CITATIONS: readonly { id: string; label: string; bibtex: string }[] = [
  { id: 'anet4d', label: 'ANet4D', bibtex: ANET4D_BIBTEX },
  { id: 'urbanscene3d', label: 'UrbanScene3D', bibtex: URBANSCENE_BIBTEX },
]

/** the sections of the page in reading order (header navigation and the mobile menu); "event-lab" only with LAB_PUBLIC */
export const NAV: readonly NavId[] = LAB_PUBLIC ? ['event-lab', 'story', 'showcase', 'open-source'] : ['story', 'showcase', 'open-source']
export type NavId = 'story' | 'event-lab' | 'showcase' | 'open-source'
/** i18n key of each section's navigation label */
export const NAV_KEY: Record<NavId, string> = {
  story: 'landing.nav.capabilities',
  'event-lab': 'landing.nav.labViews',
  showcase: 'landing.nav.showcase',
  'open-source': 'landing.nav.open',
}
/** the two-digit index of a numbered section header: the story is 01 and the closing call to action keeps its slot
 * (unnumbered) before the open source section; the Event Lab section, when present, comes first (right after the hero) */
export function sectionIndex(id: 'event-lab' | 'showcase' | 'open-source', lab: boolean = LAB_PUBLIC): string {
  const order = lab ? ['event-lab', 'story', 'showcase', 'closing', 'open-source'] : ['story', 'showcase', 'closing', 'open-source']
  return String(order.indexOf(id) + 1).padStart(2, '0')
}

export const ANET_URL = 'https://github.com/ANetResearch/ANet'
export const API_DOCS_PATH = '/sandbox/api'

/** product version from the build meta tag (`0.2.0-demo+abcdef0123`), or null in builds without it */
export function buildVersion(): string | null {
  const c = typeof document === 'undefined' ? null : document.querySelector('meta[name="awr-build"]')?.getAttribute('content')
  const m = c ? /^(\d+\.\d+\.\d+)/.exec(c) : null
  return m ? m[1] : null
}

/** scroll a section into view below the sticky header (smooth unless the visitor prefers reduced motion) */
export function scrollToSection(id: string): void {
  const el = document.getElementById(id)
  if (!el) return
  const reduce = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  history.replaceState(null, '', `#${id}`)
}
