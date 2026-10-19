// /lab/runs/:rid/lidar，LiDAR 与重建对比（D3-AC-15；AWR-05 §7.5；AWR-14 §21.8）。查询参数 `uav`、`src`（gt,l0,livo 子集）、`rep`、
// `mode ∈ {livo, lio}`，以及 `tier`（S、B，覆盖设备类别判定）、`color`、`size`、`crop`；最低角色研究者，只读（预计算制品），内部 D3 构建。
// 运行 id 规则与 /lab/runs/:rid 相同（bench-api：采集运行为命名，基准运行为 run-<10 位 base32>）。
import { LidarComparePage } from '@/ui/lab/lidar-compare/ComparePage'
import { labRoute } from './lab'

export const route = labRoute({
  id: 'lab-lidar-compare', pattern: '/lab/runs/:rid/lidar', component: LidarComparePage,
  validate: (p) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(p.rid ?? ''),
})
