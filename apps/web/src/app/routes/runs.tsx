// /runs, the recordings overlay page (AWR-14 §5.4 "Runs 覆盖页"; M12 §8.8; D1-ext, delivered by FX-WEB2).
import type { RouteDef } from '@/app/router/router'
import { RunsPage } from '@/ui/views/RunsPage'

export const route: RouteDef = { id: 'runs', pattern: '/runs', layer: 'overlay', d1: 'ext', component: RunsPage }
