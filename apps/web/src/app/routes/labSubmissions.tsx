// /lab/submissions, internal submissions (BN-95; AWR-14 §21.2.2); ext route `lab-submissions`, internal D3 build only.
import type { RouteDef } from '@/app/router/router'
import { LabSubmissionsPage } from '@/ui/lab/LabSubmissionsPage'

export const route: RouteDef = { id: 'lab-submissions', pattern: '/lab/submissions', layer: 'overlay', d1: 'ext', component: LabSubmissionsPage }
