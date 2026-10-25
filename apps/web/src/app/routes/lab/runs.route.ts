// /lab/runs，运行列表与状态（AWR-14 §21.6；BN-42 至 BN-45；D3-AC-51）：状态机徽标、排队位置、取消、重试与原因码。查询参数
// `event`、`xp`、`policy`、`state`、`ranked`、`cursor`，`run` 打开运行状态侧栏。取代原 app/routes/labRuns.tsx（同一 id `lab-runs`）。
import { RunsPage } from '@/ui/lab/runs/RunsPage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-runs', pattern: '/lab/runs', component: RunsPage })
