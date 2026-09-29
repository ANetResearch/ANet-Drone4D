#!/usr/bin/env node
// z-layers codemod (M15-FR-011; AWR-14 §3.1): the popup layers of the shadcn sources use the layer tokens defined in
// styles/layout.css instead of z-50, so dialogs sit on --z-dialog, anchored popups (and their positioners) on
// --z-popover (above dialogs, so Select and Tooltip inside a Dialog stay visible) and the toast viewport on --z-toast.
// Local stacking inside a component (z-10, z-20, toast z-[calc(...)]) is left untouched.
// Idempotent. Usage: node tools/shadcn/codemods/z-layers.mjs [--dry] [uiDir]
import { UI_DIR, eachFile, mapAllClassStrings, mapClasses } from './_lib.mjs'

const LAYER = { dialog: 'dialog', 'alert-dialog': 'dialog', sheet: 'dialog', command: 'dialog', toast: 'toast' }
export function transform(src, name) {
  const layer = LAYER[name] ?? 'popover'
  return mapAllClassStrings(src, (cls) => mapClasses(cls, (t) => {
    const m = /^((?:[a-z0-9-]+:)*)z-50$/.exec(t)
    if (!m) return t
    if (m[1].includes('data-[slot=kbd]') || m[1].startsWith('**:')) return t   // kbd inside tooltip: local stacking
    return `${m[1]}z-(--z-${layer})`
  }))
}
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`z-layers codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
