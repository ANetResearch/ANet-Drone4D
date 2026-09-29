// /reports?src=<same-origin path>, the test report page fed by M16 perf/report/render.mjs (M15-FR-080; M16-FR-071).
import type { RouteDef } from '@/app/router/router'
import { ReportPage } from '@/ui/views/Report'

export const route: RouteDef = { id: 'reports', pattern: '/reports', layer: 'page', d1: 'core', component: ReportPage }
