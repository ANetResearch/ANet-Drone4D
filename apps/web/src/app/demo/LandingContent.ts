// Static content of the public demo landing page (D2.2). Texts live in the i18n dictionaries under landing.*; this module
// holds the structure: the chapters of the scroll narrative with the delivery status of each point (V0.1 and V0.2.0
// delivered; D3 scope in development or research per docs/05 and docs/research/d3), the captured media with their
// intrinsic sizes and the citation entries. Keyword labels are the official spellings, the same in both locales.
import type { IconKey } from '@/ui/icons/registry'
import { URBANSCENE_BIBTEX } from '@/lib/demo'

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
  { id: 'roadmap', icon: 'sensor.lidar', points: ['dev', 'dev', 'research', 'research'], image: 'roadmap', keywords: ['Gazebo', 'LiDAR', '4D Reconstruction', '3DGS', 'PX4'] },
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

/** BibTeX of the software (README.md "Data and citation", CITATION.cff) */
export const DRONE4D_BIBTEX = `@software{anet_drone4d_2026,
  title   = {ANet-Drone4D: A Real-World Grounded 4D World Runtime for Autonomous Multi-Agent Drones},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/ANetResearch/ANet-Drone4D}
}`

export const CITATIONS: readonly { id: string; label: string; bibtex: string }[] = [
  { id: 'drone4d', label: 'ANet Drone4D', bibtex: DRONE4D_BIBTEX },
  { id: 'urbanscene3d', label: 'UrbanScene3D', bibtex: URBANSCENE_BIBTEX },
]

/** the sections of the page in reading order (header navigation and the mobile menu) */
export const NAV = ['story', 'showcase', 'open-source'] as const
export type NavId = (typeof NAV)[number]
/** i18n key of each section's navigation label */
export const NAV_KEY: Record<NavId, string> = {
  story: 'landing.nav.capabilities',
  showcase: 'landing.nav.showcase',
  'open-source': 'landing.nav.open',
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
