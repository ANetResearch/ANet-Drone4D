// /lab/worlds/:wid，世界详情（AWR-14 §21.3.2；BN-11；D3-AC-51）。查询参数 `tab ∈ {overview, calibration, quality, layers, sensors,
// license}`；不合规的 :wid 由页面以 replace 回到世界列表（§21.2.2 规则 1：回到对应列表页，而不是路由器的缺省 /worlds）。
import { WorldDetailPage } from '@/ui/lab/worlds/WorldDetailPage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-world', pattern: '/lab/worlds/:wid', component: WorldDetailPage })
