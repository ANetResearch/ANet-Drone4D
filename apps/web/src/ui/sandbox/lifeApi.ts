// Experimental scene target authoring of the sandbox (m23 scene-target foundations; AWR_TARGET_LIFE_ENABLE on the server,
// otherwise 404): population preview (seed or the metered model-service designer), saved-recipe rehearsal and scene
// facilities. The public demo build replaces this module with a stub unless VITE_AWR_TARGET_LIFE=1 (vite.config.ts
// TARGET_LIFE_STUBS), so neither the routes nor the provider wording ship in the public bundle.
import type { Target } from '@awr/contracts/types'
import { apiGet, apiPost } from '@/net/api'
import { apiPath } from '@/net/endpoint'

export interface LifeFacility { label: string; indoor?: boolean; frontage_center?: number[]; normal_xy?: number[]; portal_width_m?: number; portal_height_m?: number }

export const lifeApi = {
  rehearsal: (sha: string, actorId: string) => apiPost<LifeRehearsal>(apiPath(`/target-life/${encodeURIComponent(sha)}/rehearsal`), { actor_id: actorId }),
  scene: (sha: string) => apiGet<{ facilities: Record<string, LifeFacility> }>(apiPath(`/target-life/${encodeURIComponent(sha)}/scene`)),
  preview: (body: { seed: number; people: number; vehicles: number; extras?: Record<string, number>; vehicle_types?: string[]; mode: 'seed' | 'deepseek'; prompt?: string }) => apiPost<LifePreview>(apiPath('/target-life/preview'), body),
}

export interface LifeSample {
  t: number; pos: number[]; yaw: number; speed: number; distance: number
  hidden: boolean; phase: string; destination: string; reason: string
}
export interface LifeRehearsal {
  recipe_sha256: string; actor_id: string; seed: number; duration_s: number; sample_s: number
  samples: LifeSample[]; roads: number[][][]
  facilities: { id: string; label: string; pos: number[]; indoor: boolean }[]
  itinerary: { facility: string; purpose: string; dwell_s: number }[]
}

export interface LifePreview {
  seed: number
  sha256: string | null
  targets: Target[]
  audit: { admitted: boolean; ticks: number; min_clearance_m: number | null; failures: { actor?: string; why: string; seconds?: number }[]; actors: Record<string, { class: string; stationary: boolean; distance_m: number; visits: number; updates: number; max_no_progress_s: number }> }
  design?: { summary: string } | null
  provider_usage?: { requested_model?: string; usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }; wall_s?: number } | null
}
