// /world/:id, the Sandbox (M15-FR-003, FR-005): id must match ^[a-z0-9-]{1,63}$ (AWR-03 §5.6).
import type { RouteDef } from '@/app/router/router'
import { WORLD_ID } from '@/app/router/search'

export const route: RouteDef = { id: 'world', pattern: '/world/:id', layer: 'sandbox', d1: 'core', validate: (p) => WORLD_ID.test(p.id ?? '') }
