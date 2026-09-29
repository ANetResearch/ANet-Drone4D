// /worlds, the World Hub overlay page (M15-FR-023).
import type { RouteDef } from '@/app/router/router'
import { WorldHub } from '@/ui/views/WorldHub'

export const route: RouteDef = { id: 'worlds', pattern: '/worlds', layer: 'overlay', d1: 'core', component: WorldHub }
