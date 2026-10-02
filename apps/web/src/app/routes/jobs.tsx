// /jobs, the Reconstruction Jobs overlay page (AWR-14 §5.7; M01 §8.2; D1-AC-22 UI part; D1-ext, delivered by FX-WEB2).
import type { RouteDef } from '@/app/router/router'
import { JobsPage } from '@/ui/views/JobsPage'

export const route: RouteDef = { id: 'jobs', pattern: '/jobs', layer: 'overlay', d1: 'ext', component: JobsPage }
