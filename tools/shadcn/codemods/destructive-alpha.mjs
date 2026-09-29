#!/usr/bin/env node
// destructive-alpha codemod (AWR-15 §3.3 supplementary rule 2; VIS-FR-045, VIS-AC-002, VIS-AC-031): the destructive
// variants of Button, Badge and the DropdownMenu, ContextMenu and Menubar items use a /10 background and /15 for hover
// and focus in both themes, so red text keeps >= 4.5:1 on the tinted background (dark r400 on g850: 4.97 / 4.64;
// light r700 on g50: 5.02 / 4.62). Focus rings (ring-destructive/NN) and borders are not backgrounds and stay as is.
// Idempotent. Usage: node tools/shadcn/codemods/destructive-alpha.mjs [--dry] [uiDir]
import { UI_DIR, eachFile, mapAllClassStrings, mapClasses } from './_lib.mjs'

const FILES = ['button', 'badge', 'dropdown-menu', 'context-menu', 'menubar']
export function transform(src, name) {
  if (!FILES.includes(name)) return src
  return mapAllClassStrings(src, (cls) => mapClasses(cls, (t) => {
    const m = /^((?:[a-z0-9-]+:|data-\[[^\]]+\]:|\[[^\]]+\]:)*)bg-destructive\/(\d+)$/.exec(t)
    if (!m) return t
    const interactive = /(?:^|:)(?:hover|focus|focus-visible|active):/.test(m[1])
    return `${m[1]}bg-destructive/${interactive ? 15 : 10}`
  }))
}
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`destructive-alpha codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
