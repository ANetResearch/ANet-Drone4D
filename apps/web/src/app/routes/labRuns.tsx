// /lab/runs, D3 run list (AWR-14 §21.2.2); ext route `lab-runs`, internal D3 build only.
import type { RouteDef } from '@/app/router/router'
import { LabRunsPage } from '@/ui/lab/LabRunsPage'

export const route: RouteDef = { id: 'lab-runs', pattern: '/lab/runs', layer: 'overlay', d1: 'ext', component: LabRunsPage }
