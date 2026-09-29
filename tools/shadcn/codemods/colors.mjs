#!/usr/bin/env node
// colors codemod (ADR-032; AWR-15 §3.3, §16 item 9; lint VIS-L-02): named palette colours in shadcn sources are replaced by
// ANet Graphite tokens. bg-black/NN (dialog, alert-dialog and sheet backdrops) -> bg-overlay (--overlay = g950 / 60%);
// bg-white (slider thumb) -> bg-(--white). Idempotent. Usage: node tools/shadcn/codemods/colors.mjs [--dry] [uiDir]
import { UI_DIR, eachFile, mapAllClassStrings, mapClasses } from './_lib.mjs'

const MAP = [
  [/^((?:[a-z0-9-]+:)*)bg-black(?:\/\d+)?$/, (m) => `${m[1]}bg-overlay`],
  [/^((?:[a-z0-9-]+:)*)bg-white(?:\/\d+)?$/, (m) => `${m[1]}bg-(--white)`],
  [/^((?:[a-z0-9-]+:)*)text-white$/, (m) => `${m[1]}text-brand-foreground`],
]
export const transform = (src) => mapAllClassStrings(src, (cls) => mapClasses(cls, (t) => {
  for (const [re, f] of MAP) {
    const m = re.exec(t)
    if (m) return f(m)
  }
  return t
}))
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`colors codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
