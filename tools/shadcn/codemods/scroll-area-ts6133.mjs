#!/usr/bin/env node
// scroll-area-ts6133 codemod (M15-FR-083; d04 §6 item 2): base-mira scroll-area.tsx imports React without using it,
// which fails `noUnusedLocals` (TS6133). Drop the unused namespace import in any component that does not use `React.`.
// Idempotent. Usage: node tools/shadcn/codemods/scroll-area-ts6133.mjs [--dry] [uiDir]
import { UI_DIR, eachFile } from './_lib.mjs'

export function transform(src) {
  const imp = /^import \* as React from ["']react["']\r?\n/m
  if (!imp.test(src)) return src
  const rest = src.replace(imp, '')
  return /\bReact\./.test(rest) ? src : rest
}
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`scroll-area-ts6133 codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
