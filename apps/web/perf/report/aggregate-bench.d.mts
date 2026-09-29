// Types of aggregate-bench.mjs.
export declare const MIN_REPORTS: number
export declare const MIN_DEVICES: number
export declare function loadReports(dir: string): unknown[]
export declare function aggregate(reports: unknown[]): { groups: { group: string; reports: number; devices: number }[];
  classes: { device_class: string; reports: number; devices: number; status: string; metrics: Record<string, { n: number; median: number; iqr: number }> }[] }
export declare function selftest(): boolean
