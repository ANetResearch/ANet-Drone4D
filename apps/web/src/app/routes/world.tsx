// /world/:id, the Sandbox (M15-FR-003, FR-005): id must match ^[a-z0-9-]{1,63}$ (AWR-03 §5.6).
// The public demo build accepts only the demo world (ADR-083); other ids fall back to the World Hub like unknown paths.
import type { RouteDef } from '@/app/router/router'
import { WORLD_ID } from '@/app/router/search'
import { DEMO_PUBLIC, DEMO_WORLDS } from '@/lib/demo'

export const route: RouteDef = {
  id: 'world', pattern: '/world/:id', layer: 'sandbox', d1: 'core',
  validate: (p) => WORLD_ID.test(p.id ?? '') && (!DEMO_PUBLIC || DEMO_WORLDS.includes(p.id ?? '')),
}
