// /lab/worlds，世界列表（AWR-14 §21.2.2、§21.3；BN-10；D3-AC-51）。最低角色研究者，内部 D3 构建。
import { WorldsPage } from '@/ui/lab/worlds/WorldsPage'
import { labRoute } from './lab'

export const route = labRoute({ id: 'lab-worlds', pattern: '/lab/worlds', component: WorldsPage })
