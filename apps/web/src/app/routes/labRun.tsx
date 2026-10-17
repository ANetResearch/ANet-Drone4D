// /lab/runs/:rid, multi-UAV observation wall (AWR-14 §21.7); ext route `lab-run`, internal D3 build only. Run ids follow
// the bench-api pattern (capture runs are named, benchmark runs are run-<10 base32>).
import type { RouteDef } from '@/app/router/router'
import { LabWallPage } from '@/ui/lab/LabWallPage'

export const route: RouteDef = {
  id: 'lab-run', pattern: '/lab/runs/:rid', layer: 'overlay', d1: 'ext', component: LabWallPage,
  validate: (p) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(p.rid ?? ''),
}
