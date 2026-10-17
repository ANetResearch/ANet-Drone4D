// /lab/login, D3 internal workspace sign-in (AWR-14 §21.2.2). Registered only in builds that list `lab-login` in
// VITE_AWR_EXT_ROUTES (internal D3 build); D2 builds are unchanged.
import type { RouteDef } from '@/app/router/router'
import { LabLogin } from '@/ui/lab/LabLogin'

export const route: RouteDef = { id: 'lab-login', pattern: '/lab/login', layer: 'overlay', d1: 'ext', component: LabLogin }
