// 公开站样例运行页的路由扩展（app/lab-public/router.ts 扩展点 `./samples/*.route.tsx`；AWR-14 §21.29.3；F07）。Owner: M15.
// 与内置路由 labPublicSample 同一 pattern，按登记顺序优先匹配：样例运行页以事件回放、多机私有视角与数据检视取代内置的下载列表。
import type { LabPublicRoute } from '../router'
import { SampleRunPage } from './SampleRunPage'

export const route: LabPublicRoute = {
  id: 'labPublicSampleRun',
  pattern: '/lab/samples/:sid',
  title: (p, t) => t('lab.public.sample.title', { id: p.params.sid ?? '' }),
  render: (p) => <SampleRunPage {...p} />,
}
