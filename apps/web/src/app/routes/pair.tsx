// /pair, the commander pairing input page (D2.1 P1; AWR-04 §15.8.2 item 4; M15-FR-239): a page-layer route in its own lazy
// chunk; the code comes from the sandbox holder's "pair a commander" dialog.
import { lazy } from 'react'
import type { RouteDef } from '@/app/router/router'

const PairPage = lazy(() => import('@/ui/views/PairPage').then((m) => ({ default: m.PairPage })))

export const route: RouteDef = { id: 'pair', pattern: '/pair', layer: 'page', d1: 'core', component: PairPage }
