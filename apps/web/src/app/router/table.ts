// Route table: every app/routes/*.tsx exports `route` (extension point, AWR-03 §4.3). ext routes are registered only when
// their D1-ext feature ships (VITE_AWR_EXT_ROUTES lists delivered ids; /bench ships as a placeholder), test-only routes
// only in dev and test builds. The public demo build (ADR-083) registers only the sandbox, World Hub and settings routes:
// Jobs, Runs, replay, Bench and the report pages are not offered there.
import { DEMO_PUBLIC, DEMO_ROUTES } from '@/lib/demo'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { setRoutes, type RouteDef } from './router'

const mods = import.meta.glob<{ route: RouteDef }>('../routes/*.tsx', { eager: true })
const EXT_DELIVERED = new Set(['bench', 'runs', 'replay', 'jobs', ...String(import.meta.env.VITE_AWR_EXT_ROUTES ?? '').split(',').filter(Boolean)])

export function installRoutes(): readonly RouteDef[] {
  const list = Object.values(mods).map((m) => m.route).filter((r) => (r.d1 === 'core' || EXT_DELIVERED.has(r.id)) && (!r.testOnly || TEST_SWITCHES)
    && (!DEMO_PUBLIC || DEMO_ROUTES.has(r.id)))
  setRoutes(list)
  return list
}
