// Types of build-report.mjs.
export declare function runPath(run: string): string
export declare function readResults(dir: string): Record<string, unknown>[]
export declare function reportCase(r: unknown): { id: string; status: string; metrics: { key: string; median: number | null; status: string }[];
  ac_ids: string[]; priority: string; world_id: string | null; scene: string | null }
export declare function summary(cases: { priority: string; status: string; id: string; ac_ids: string[] }[], results?: unknown[]): {
  p0_pass: number; p0_total: number; p1_pass: number; p1_total: number; p1_pass_rate_pct: number; regressions: string[]; waivers: string[] }
export declare function footerMeta(results: unknown[], worldsDir?: string): Record<string, unknown>
export declare function buildReport(dir: string, o?: { gate?: string; kind?: string }): Promise<Record<string, unknown>>
