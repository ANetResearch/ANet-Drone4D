// Case definition types of the M16 harness (M16 §6.7.3). Module registries annotate their default export with
// `/** @type {import('../harness/types').CaseDef[]} */` (perf/<module>/cases.mjs).

export type CaseKind = 'pw' | 'py' | 'pytest' | 'shell'
/** G2d and G2w map to 'G2' in awr.perf.report.v1 (18 §11.2) */
export type Gate = 'G2d' | 'G2w' | 'G3' | 'G4'

export interface BackendSpec {
  /** live = supervisor + sim-core; fake = tools/fake/fake_gw.py; none = static server only; tool = the tool starts its own */
  kind: 'live' | 'fake' | 'none' | 'tool'
  world: string
  scenario?: string
  scenarioProfile?: string
  /** scenario mark label to wait for, e.g. 'ladder.steady'; default: api ready */
  waitMark?: string
  env?: Record<string, string>
  /** fake_gw vehicle count */
  fakeN?: number
  /** processes to start (supervisor --only); default 'sim-core,api', 'all' = the whole profile */
  only?: string
  /** weak-network proxy profile W0-W3 in front of the api (perf/net.spec.ts, perf/m11/weaknet.spec.ts) */
  netProfile?: 'W0' | 'W1' | 'W2' | 'W3'
}

export interface MetricDef {
  /** dotted form of an 18 §11.2 name: 'frame.p95_ms' -> frame_p95_ms */
  key: string
  unit: 'ms' | 'us' | 'pct' | 'count' | 'core' | 'hz' | 'bytes' | 'ratio' | 'per_min' | 'm'
  source: 'snapshot' | 'server' | 'bench' | 'trace' | 'pytest' | 'script'
  /** extractor name in analyze.mjs (default: key with '.' replaced by '_') */
  extract?: string
  /** thresholds.json key; absent = recorded only */
  threshold?: string
  /** false = characterisation metric */
  gating: boolean
  /** PR-10 dispersion warning (key metrics only) */
  dispersionWatch?: boolean
  /** not registered in 18 §11.2 yet: judged, written to result.json extra, never reported (M16 §14 item 21) */
  extra?: boolean
}

export interface CaseDef {
  id: string
  kind: CaseKind
  /** 'perf/flight60.spec.ts' (relative to apps/web) or 'tests/e2e/x.spec.ts' (relative to the repository) */
  spec?: string
  /** py / pytest / shell command line (relative paths resolve against the repository root) */
  cmd?: string[]
  build: 'production' | 'test' | 'profiling' | 'none'
  browser?: 'C1' | 'C2'
  backend: BackendSpec
  params: Record<string, string | number | boolean>
  runs: 1 | 3
  timeoutS: number
  acIds: string[]
  priority: 'P0' | 'P1' | 'P2'
  layer: 'core' | 'ext'
  gates: Gate[]
  metrics: MetricDef[]
  owner: string
}

/** awr.perf.result.v1 (M16 §7.3.5; internal to M16) */
export interface CaseResult {
  schema: 'awr.perf.result.v1'
  case: Pick<CaseDef, 'id' | 'kind' | 'params' | 'acIds' | 'priority' | 'layer' | 'owner'> & { world?: string; scene?: string | null }
  runs: { index: number; dir: string; status: string; errors: { code: string; message: string }[]; load: { pre: number; max: number; mean: number };
    started_at: string; duration_s: number; retried: boolean }[]
  metrics: { key: string; report_key: string | null; unit: string; runs: (number | null)[]; median: number | null; dispersion: number;
    threshold: { op: string; value: number } | null; gating: boolean; status: string; baseline?: number | null; delta?: number | null;
    regression?: boolean | null }[]
  status: 'PASS' | 'FAIL' | 'ENV_UNMET' | 'WARN' | 'NA' | 'WAIVED'
  errors: { code: string; message: string }[]
  fingerprint: Record<string, unknown>
  extra: Record<string, unknown>
  gate: string
  source: 'live' | 'fake'
}
