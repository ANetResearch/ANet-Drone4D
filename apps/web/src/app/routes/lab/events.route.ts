// /lab/events，事件列表与编译状态（AWR-14 §21.4.1；BN-20、BN-22；D3-AC-51）。查询参数 `world`、`status`、`q`；`eid` 与 `v` 打开
// 事件版本的编译详情侧栏（时间线与编辑器 /lab/events/:eid 不在本页）。
import { EventsPage } from '@/ui/lab/worlds/EventsPage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-events', pattern: '/lab/events', component: EventsPage })
