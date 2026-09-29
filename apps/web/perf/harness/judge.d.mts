// Types of judge.mjs (Vitest unit tests in apps/web/tests/m16 import the module from TypeScript).
export declare const LOAD_FRAME_MAX: number
export declare const LOAD_QUALITY_MEAN: number
export declare const DISPERSION_MAX: number
export declare function sorted(a: ArrayLike<number>): Float64Array
export declare function quantile(sorted: ArrayLike<number>, p: number): number | null
export declare function mean(a: number[]): number | null
export declare function median(values: (number | null)[]): number | null
export declare function dispersion(values: (number | null)[]): number
export declare function compare(v: number, op: string, th: number): boolean
export interface Judged { key: string; runs: (number | null)[]; median: number | null; dispersion: number; threshold: { op: string; value: number } | null;
  gating: boolean; status: string; code?: string }
export declare function judgeMetric(m: { key: string; gating: boolean; dispersionWatch?: boolean }, runs: (number | null)[],
  load: { max: number[]; mean: number[] }, th: { op: string; value: number; kind?: string } | null): Judged
export declare function worst(statuses: string[]): string | null
export declare function judgeCase(c: { priority: string; acIds: string[] }, metrics: { status: string; gating: boolean }[],
  ex: { execStatus: string }, waivers?: { ac_id: string }[]): string
export declare const WAIVER_FIELDS: string[]
export interface Waiver { ac_id: string; priority: string; [field: string]: string }
export declare function validateWaivers(list: unknown): { valid: Waiver[]; rejected: { waiver: unknown; reason: string }[] }
export declare function regressed(key: string, cur: number | null, base: number | null): boolean | null
export declare function fingerprintMatches(a: unknown, b: unknown): boolean
