// Static content of the public demo landing page (D2.2-L1). Texts live in the i18n dictionaries under landing.*; this
// module holds the structure: the capability keywords with their delivery status (V0.1 and V0.2.0 delivered; D3 scope in
// development or research per docs/05 and docs/research/d3), the captured media with their intrinsic sizes, the stack
// rows, the roadmap and the citation entries. Keyword labels are the official spellings (Gazebo Sim, Rerun, FAST-LIVO2,
// PX4), the same in both locales.
import type { IconKey } from '@/ui/icons/registry'
import { URBANSCENE_BIBTEX } from '@/lib/demo'

export type CapStatus = 'available' | 'dev' | 'research'

export interface Capability {
  id: string
  /** official keyword label, shown in both locales */
  kw: string
  icon: IconKey
  status: CapStatus
}

/** delivered in V0.1 (D1) and V0.2.0 (D2) */
export const CAPS_DELIVERED: readonly Capability[] = [
  { id: 'twin', kw: 'Digital Twin', icon: 'layer.pointcloud', status: 'available' },
  { id: 'swarm', kw: 'Drone Swarm Simulation', icon: 'nav.fleet', status: 'available' },
  { id: 'px4lite', kw: 'PX4-lite', icon: 'drone.quad', status: 'available' },
  { id: 'control', kw: 'Swarm Control', icon: 'cmd.formation', status: 'available' },
  { id: 'perception', kw: 'Target Perception', icon: 'mission.target', status: 'available' },
  { id: 'relay', kw: 'Relay Watch', icon: 'task.relay', status: 'available' },
  { id: 'api', kw: 'Open API', icon: 'api.doc', status: 'available' },
  { id: 'agents', kw: 'Agent Swarm', icon: 'agent.network', status: 'available' },
]

/** D3 Event Lab scope (docs/05) and later research */
export const CAPS_NEXT: readonly Capability[] = [
  { id: 'gazebo', kw: 'Gazebo Sim', icon: 'sensor.compute', status: 'dev' },
  { id: 'bench', kw: 'Event Benchmark', icon: 'perf.bench', status: 'dev' },
  { id: 'sensors', kw: 'Mesh Camera · LiDAR', icon: 'sensor.lidar', status: 'dev' },
  { id: 'recon', kw: '4D Reconstruction', icon: 'layer.mesh', status: 'dev' },
  { id: 'rerun', kw: 'Rerun', icon: 'perf.chart', status: 'dev' },
  { id: 'gs', kw: '3DGS', icon: 'layer.splat', status: 'research' },
  { id: 'sitl', kw: 'PX4 SITL', icon: 'drone.hexa', status: 'research' },
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

/** rows of the stack table (label and value both in the dictionaries: landing.stack.<row> and landing.stack.<row>Value) */
export const STACK_ROWS = ['world', 'sim', 'safety', 'client', 'api', 'site', 'd3'] as const

export interface RoadItem { id: string; version: string; status: CapStatus }
export const ROADMAP: readonly RoadItem[] = [
  { id: 'v01', version: 'V0.1 · D1', status: 'available' },
  { id: 'v02', version: 'V0.2.0 · D2', status: 'available' },
  { id: 'v03', version: 'V0.3 · D3', status: 'dev' },
  { id: 'later', version: 'P3 · V1.0', status: 'research' },
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
export const NAV = ['capabilities', 'showcase', 'stack', 'open-source'] as const
export type NavId = (typeof NAV)[number]
/** i18n key of each section's navigation label */
export const NAV_KEY: Record<NavId, string> = {
  capabilities: 'landing.nav.capabilities',
  showcase: 'landing.nav.showcase',
  stack: 'landing.nav.stack',
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
