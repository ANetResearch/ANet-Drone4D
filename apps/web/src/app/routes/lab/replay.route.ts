// /lab/replay，双运行同步回放与样例包浏览（D3-AC-19、D3-AC-56；AWR-14 §21.7、§21.13）。查询参数 `a`、`b`（运行 id）、`t`（统一时间轴
// 仿真秒）、`cam`、`align ∈ {sim, event}`、`rate`；最低角色研究者，内部 D3 构建。
import { ReplayPage } from '@/ui/lab/replay/ReplayPage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-replay', pattern: '/lab/replay', component: ReplayPage })
