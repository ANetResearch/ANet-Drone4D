// Types of check-report.mjs.
export declare const MAX_BYTES: number
export declare const FORBIDDEN: RegExp
export declare function htmlText(html: string): string
export declare function staticErrors(report: unknown, html: string, bytes: number, validate?: unknown): string[]
export declare function checkReport(dir: string, o?: { browser?: boolean }): Promise<string[]>
