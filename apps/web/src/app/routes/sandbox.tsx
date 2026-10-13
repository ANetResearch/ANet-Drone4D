// /sandbox/:sid, the sandbox view (ADR-111; M15-FR-203; UX-FR-103; AWR-14 §20.2.2): the same floating shell as /world/:id
// over the resident canvas, with the world taken from the session. `sid` must match ^sb-[a-z2-7]{10}$ (otherwise the
// router falls back to the World Hub); /sandbox/api is registered as its own route with more static segments, so it is
// never taken for a sid. Whether this browser holds the sid's token (operator surface) or not (the "session unavailable"
// page) is decided at run time by app/shell/surface.ts.
import type { RouteDef } from '@/app/router/router'
import { SID_RE } from '@/ui/sandbox/session'

export const route: RouteDef = {
  id: 'sandbox', pattern: '/sandbox/:sid', layer: 'sandbox', d1: 'core',
  validate: (p) => SID_RE.test(p.sid ?? ''),
}
