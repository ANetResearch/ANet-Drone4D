// Route table: every app/routes/*.tsx exports `route` (extension point, AWR-03 §4.3). ext routes are registered only when
// their D1-ext feature ships (VITE_AWR_EXT_ROUTES lists delivered ids; /bench ships as a placeholder), test-only routes
// only in dev and test builds. The public demo build (ADR-083) registers only the sandbox, World Hub and settings routes:
// Jobs, Runs, replay, Bench and the report pages are not offered there.
// D3 lab routes are sharded (AWR-14 §21.2.2 rule 1): each lab feature adds its own app/routes/lab/<feature>.route.ts, collected
// by the second glob below, so parallel lab work never edits a shared table. The demo build rewrites that glob to an empty one
// (vite.config.ts labStripPlugin) and folds the DEMO_PUBLIC branch away, so its bundle is the D2 one byte for byte.
import { DEMO_PUBLIC, DEMO_ROUTES } from '@/lib/demo'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { setRoutes, type RouteDef } from './router'

const mods = import.meta.glob<{ route: RouteDef }>('../routes/*.tsx', { eager: true })
const labMods = import.meta.glob<{ route: RouteDef }>('../routes/lab/*.route.ts', { eager: true })
const EXT_DELIVERED = new Set(['bench', 'runs', 'replay', 'jobs', ...String(import.meta.env.VITE_AWR_EXT_ROUTES ?? '').split(',').filter(Boolean)])

/**
 * register the route table; `internal` (D2.1, ADR-123; M15-FR-236) also registers the pages the demo build hides (Jobs,
 * Runs, replay, Bench, reports) for the internal identity; locking it again re-installs the demo table
 */
export function installRoutes(internal = false): readonly RouteDef[] {
  const list = Object.values(DEMO_PUBLIC ? mods : { ...mods, ...labMods }).map((m) => m.route).filter((r) => (r.d1 === 'core' || EXT_DELIVERED.has(r.id)) && (!r.testOnly || TEST_SWITCHES)
    && (!DEMO_PUBLIC || internal || DEMO_ROUTES.has(r.id)))
  setRoutes(list)
  return list
}
