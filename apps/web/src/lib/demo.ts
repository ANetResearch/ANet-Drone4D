// Public demo build switch (ADR-083; M15-FR-120; AWR-19 §3.7 T4): a production build made with VITE_AWR_DEMO=public.
// Constant folded at build time, so a default build carries none of the demo branches and a demo build none of the
// hidden pages' routes. vite.config.ts refuses VITE_AWR_DEMO together with VITE_AWR_TEST_SWITCHES (no test switch in the
// demo bundle).
// D2 (ADR-109, ADR-111; M15-FR-120 and FR-122 as revised by M15 §15.3): the site serves seven worlds (the generated city
// synthcity and the six UrbanScene3D cities, used with the authorisation of the dataset right holders); the shared show
// (scenario S7) runs on synthcity and is read-only for every visitor; each visitor may open one isolated sandbox. The
// write surfaces are no longer stripped at build time: the build switches between the viewer and the operator surface at
// run time by session role (app/shell/surface.ts); the server enforces the same policy independently (ADR-093).

export const DEMO_PUBLIC: boolean = import.meta.env.VITE_AWR_DEMO === 'public'

/** the world of the shared show run (public profile v2: run.world = synthcity, scenario S7) */
export const DEMO_WORLD = 'synthcity'
/** the seven worlds of every deployment (ADR-109); the server's GET /api/worlds whitelist is the run-time source */
export const DEMO_WORLDS: readonly string[] = ['synthcity', 'shenzhen', 'suzhou', 'newyork', 'chicago', 'shanghai', 'sanfrancisco']
/** worlds of the UrbanScene3D dataset (licence note and ECCV 2022 citation on their cards, ADR-109) */
export const URBANSCENE_WORLDS: ReadonlySet<string> = new Set(['shenzhen', 'suzhou', 'newyork', 'chicago', 'shanghai', 'sanfrancisco'])
/** ECCV 2022 BibTeX of UrbanScene3D (ADR-109; the same entry as NOTICE, README.md and THIRD_PARTY_NOTICES.md section 4),
 *  shown on the World Hub cards and in About */
export const URBANSCENE_BIBTEX = `@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}`
/** dataset name of the six cities in world.json (dataset.name; the deployment kit keys the authorization on it) */
export const URBANSCENE_DATASET = 'UrbanScene3D'
export type DatasetNoteKey = 'world.datasetFree' | 'world.datasetAuthorized' | 'world.dataset'
/** i18n key of the dataset line of the WORLD group (ADR-109 rule 3): generated and free to redistribute (synthcity,
 *  redistribution = true); used under the authorization of the dataset rights holders (world.json dataset.terms, or any
 *  UrbanScene3D world: packages built before ADR-109 have no terms and the old "research only" licence text, but the
 *  authorization covers every deployment of this product); otherwise the generic line */
export function datasetNoteKey(ds: { name?: string | null; redistribution?: boolean | null; terms?: string | null } | null | undefined): DatasetNoteKey {
  if (ds?.redistribution === true) return 'world.datasetFree'
  if ((typeof ds?.terms === 'string' && ds.terms.trim() !== '') || ds?.name === URBANSCENE_DATASET) return 'world.datasetAuthorized'
  return 'world.dataset'
}
/** where "Launch demo" goes */
export const DEMO_ENTRY = `/world/${DEMO_WORLD}`

/** routes registered in the demo build (Jobs, Runs, replay, Bench, reports and the design sample are left out) */
export const DEMO_ROUTES: ReadonlySet<string> = new Set(['root', 'world', 'worlds', 'settings', 'sandbox', 'sandboxApi', 'pair'])
/** palette and hotkey actions that would open a hidden page */
export const DEMO_HIDDEN_ACTIONS: ReadonlySet<string> = new Set(['replay.runs', 'jobs.open'])

export const DEMO_REPO_URL = 'https://github.com/ANetResearch/ANet-Drone4D'
export const DEMO_DOCS_URL = 'https://docs.agentnetwork.org.cn/drone4d/'

/** world ids of a list allowed in this build (the seven worlds in the demo build, all of them outside it) */
export function demoWorlds<T extends { id: string }>(list: readonly T[]): T[] {
  return DEMO_PUBLIC ? list.filter((w) => DEMO_WORLDS.includes(w.id)) : [...list]
}

// D2.2-W2: a recorded monitoring run for each of the six UrbanScene3D cities (shared drone fleet, nests, targets and
// tasking, generated and captured offline by tools/sim/gen_city_watch.py and tools/sim/capture_city_watch.py; see
// docs/impl/D2.2-W2-report.md §4). Deployed next to the world package at `/worlds/<id>/show/show.awrrt` (served as a
// static file, worlds/ is outside git); replayed client-side from a `fake:` source (net/rt/FakeSource.ts), the same
// in-process replay path the world-isolation test suite and perf/m15/tasks-d2.spec.ts already use for fixtures, now
// reached through a production URL instead of a test-only switch. synthcity keeps the live shared show (scenario S7);
// a city with no entry here falls back to static browsing (app/shell/surface.ts).
export const CITY_SHOW_FIXTURE: Readonly<Record<string, string>> = {
  shenzhen: '/worlds/shenzhen/show/show.awrrt',
  suzhou: '/worlds/suzhou/show/show.awrrt',
  newyork: '/worlds/newyork/show/show.awrrt',
  chicago: '/worlds/chicago/show/show.awrrt',
  shanghai: '/worlds/shanghai/show/show.awrrt',
  sanfrancisco: '/worlds/sanfrancisco/show/show.awrrt',
}
/** the `fake:` realtime URL of a city's recorded show, or null when it has none (app/shell/surface.ts resolveSurface) */
export function cityShowRtUrl(worldId: string | null): string | null {
  const fixture = worldId ? CITY_SHOW_FIXTURE[worldId] : undefined
  return fixture ? `fake:?world=${encodeURIComponent(worldId as string)}&fixture=${encodeURIComponent(fixture)}&loop=1` : null
}
