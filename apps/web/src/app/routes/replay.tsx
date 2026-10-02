// /world/:id/replay/:run?seg=&t= (AWR-14 §2.2; M12-FR-028 deep link): redirects to the sandbox of the world with one-shot
// ?replay=&seg=&t= parameters that the sandbox consumes once the session may open a replay (ui/views/replayFlow.ts).
import type { RouteDef } from '@/app/router/router'
import { WORLD_ID } from '@/app/router/search'
import { replayRedirect } from '@/ui/views/replayFlow'

export const route: RouteDef = {
  id: 'replay', pattern: '/world/:id/replay/:run', layer: 'sandbox', d1: 'ext',
  validate: (p) => WORLD_ID.test(p.id ?? '') && /^r\d{8}-\d{6}-[0-9a-f]{4}$/.test(p.run ?? ''), redirect: replayRedirect,
}
