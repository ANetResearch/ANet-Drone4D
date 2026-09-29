// Types of render.mjs.
export declare function footerLines(report: unknown, meta: unknown): string[]
export declare function fidelityRows(report: unknown, meta: unknown): string[][]
export declare function plainHtml(report: unknown, meta: unknown, themeCss: string): string
export declare function render(dir: string, o?: { plain?: boolean; pdf?: boolean }): Promise<string>
