// Sandbox REST helpers of the D2 panels that no domain store covers (AWR-17 §17.4, §17.5, shapes from
// `@awr/contracts/types`): the public sandbox resources of the World Hub (worlds, templates, meta; no token, cacheable,
// TanStack Query keys), the vehicle instances of a session (SB-30 to SB-33: the fleet header's "add vehicle"; the roster
// shows the result), the target configuration list (SB-40: seeds M18's target model once per session) and the suggested
// sensitivity radius of a target for a model (SB-46). Everything else moved to the domain stores in WP-12b: targets
// (M18 stores/targets.ts), nests and tasks (M20 stores/tasking.ts), the model catalogue and clones (M21
// stores/catalog.ts), perception (M19 stores/perception.ts).
import type { SessionMeta, SessionTemplateItem, SessionWorldItem, Target, TargetSuggestedRadius } from '@awr/contracts/types'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/net/api'
import { apiPath, SANDBOX_API } from '@/net/endpoint'

// ------------------------------------------------------------ public sandbox resources (no token, cacheable)
export const sbPublic = {
  worlds: () => apiGet<{ items: SessionWorldItem[] }>(`${SANDBOX_API}/worlds`),
  templates: (world?: string) => apiGet<{ items: SessionTemplateItem[] }>(`${SANDBOX_API}/templates${world ? `?world=${encodeURIComponent(world)}` : ''}`),
  meta: () => apiGet<SessionMeta>(`${SANDBOX_API}/meta`),
}

export const sbKeys = {
  worlds: ['sandbox', 'worlds'] as const,
  templates: (w: string | null) => ['sandbox', 'templates', w ?? ''] as const,
  meta: ['sandbox', 'meta'] as const,
}

// ------------------------------------------------------------ session resources (sandbox token through net/api)
export interface VehicleInstance {
  id: string
  model_id: string
  nest_id?: string | null
  label?: string | null
  home_enu_m?: (number | null)[] | null
  initial_soc?: number
  limits_profile?: string | null
}

export const sbSession = {
  targets: () => apiGet<{ items: Target[] }>(apiPath('/targets')),
  suggestedRadius: (id: string, model: string) =>
    apiGet<TargetSuggestedRadius>(apiPath(`/targets/${encodeURIComponent(id)}/suggested_radius?model_id=${encodeURIComponent(model)}`)),
  vehicles: () => apiGet<{ items: VehicleInstance[] }>(apiPath('/vehicles')),
  addVehicle: (body: Omit<VehicleInstance, 'id'>) => apiPost<VehicleInstance>(apiPath('/vehicles'), body),
  patchVehicle: (id: string, patch: Record<string, unknown>) => apiPatch<VehicleInstance>(apiPath(`/vehicles/${encodeURIComponent(id)}`), patch),
  removeVehicle: (id: string) => apiDelete(apiPath(`/vehicles/${encodeURIComponent(id)}`)),
}
