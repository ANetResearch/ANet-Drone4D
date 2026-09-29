// Types of analyze.mjs.
export declare const STEADY: [number, number]
export declare function steadyIntervals(snap: unknown, lo?: number, hi?: number): number[]
export declare const extractors: Record<string, { source: string; fn: (snap: unknown, server?: unknown) => number | null }>
export declare const benchExtractors: Record<string, (bench: unknown, params: Record<string, unknown>) => number | null>
export declare function extract(m: { key: string; source: string; extract?: string }, art: Record<string, unknown>,
  params?: Record<string, unknown>): number | null
export declare function cpuCores(a: { t: number; ticks: number } | undefined, b: { t: number; ticks: number } | undefined, hz?: number): number | null
