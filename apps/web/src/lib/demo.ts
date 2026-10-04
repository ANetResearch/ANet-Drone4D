// Public demo build switch (ADR-083; M15-FR-120; AWR-19 §3.7 T4): a production build made with VITE_AWR_DEMO=public.
// Constant folded at build time, so a default build carries none of the demo branches and a demo build none of the
// hidden pages' routes. The public demo site serves only the generated city synthcity (UrbanScene3D data must not be
// redistributed, ADR-034, ADR-077) to anonymous read-only visitors; server-side GPU features are not offered.
// vite.config.ts refuses VITE_AWR_DEMO together with VITE_AWR_TEST_SWITCHES (no test switch in the demo bundle).
export const DEMO_PUBLIC: boolean = import.meta.env.VITE_AWR_DEMO === 'public'

/** the only world of the public demo */
export const DEMO_WORLD = 'synthcity'
export const DEMO_WORLDS: readonly string[] = [DEMO_WORLD]
/** where "Launch demo" goes */
export const DEMO_ENTRY = `/world/${DEMO_WORLD}`

/** routes registered in the demo build (Jobs, Runs, replay, Bench, reports and the design sample are left out) */
export const DEMO_ROUTES: ReadonlySet<string> = new Set(['root', 'world', 'worlds', 'settings'])
/** palette and hotkey actions that would open a hidden page */
export const DEMO_HIDDEN_ACTIONS: ReadonlySet<string> = new Set(['replay.runs', 'jobs.open'])

export const DEMO_REPO_URL = 'https://github.com/ANetResearch/ANet-Drone4D'
export const DEMO_DOCS_URL = 'https://github.com/ANetResearch/ANet-Drone4D/tree/main/docs'

/** world ids of a list allowed in this build (all of them outside the demo build) */
export function demoWorlds<T extends { id: string }>(list: readonly T[]): T[] {
  return DEMO_PUBLIC ? list.filter((w) => DEMO_WORLDS.includes(w.id)) : [...list]
}
