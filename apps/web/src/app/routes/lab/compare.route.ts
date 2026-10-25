// /lab/compare，快速仿真与高保真仿真对照（D3-AC-35；AWR-05 §5.3.6 档间差异报告；AWR-14 §21.10；UX-AC-097）。查询参数 `run`（承载
// `tier_compare/` 制品的运行；缺省时取 `b`，即高保真仿真一侧的运行）、`case`（用例 id）、`uav`、`t`（仿真秒）；最低角色研究者，只读，内部 D3 构建。
import { TierComparePage } from '@/ui/lab/compare/TierComparePage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-compare', pattern: '/lab/compare', component: TierComparePage })
