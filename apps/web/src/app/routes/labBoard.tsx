// /lab/board, internal prototype leaderboard (BN-97; AWR-14 §21.2.2); ext route `lab-board`, internal D3 build only.
import type { RouteDef } from '@/app/router/router'
import { LabBoardPage } from '@/ui/lab/LabBoardPage'

export const route: RouteDef = { id: 'lab-board', pattern: '/lab/board', layer: 'overlay', d1: 'ext', component: LabBoardPage }
