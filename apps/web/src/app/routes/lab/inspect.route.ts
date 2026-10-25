// /lab/runs/:rid/inspect，运行回放 · 数据检视（AWR-14 §21.2.2、§21.9；AWR-05 §6.1.6；D3-AC-25）：右侧按需加载的 Rerun Web Viewer
// 与 lab 播放头双向同步，左侧场景总览，选择联动。查询参数 `t`、`slice`（`<from>,<to>` 仿真秒）、`catalog ∈ {dist, eval}`；最低角色研究者
// （评测 catalog：评测员），内部 D3 构建。`:rid` 的校验与观测墙 `/lab/runs/:rid` 相同，不合规时回到运行记录。
import { INSPECT_RUN_RE } from '@/ui/lab/inspect/inspect'
import { InspectPage } from '@/ui/lab/inspect/InspectPage'
import { labRoute } from './lab'

export const route = labRoute({
  id: 'lab-inspect', pattern: '/lab/runs/:rid/inspect', component: InspectPage,
  validate: (p) => INSPECT_RUN_RE.test(p.rid ?? ''),
})
