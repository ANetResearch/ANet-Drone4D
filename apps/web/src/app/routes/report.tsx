// /reports/:rid, a stored test report (R49 GET /api/sys/perf-reports/{rid}; M15-FR-080).
import type { RouteDef } from '@/app/router/router'
import { ReportPage } from '@/ui/views/Report'

export const route: RouteDef = {
  id: 'report', pattern: '/reports/:rid', layer: 'page', d1: 'core', component: ReportPage, validate: (p) => /^[A-Za-z0-9_-]{1,64}$/.test(p.rid ?? ''),
}
