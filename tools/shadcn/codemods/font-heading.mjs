#!/usr/bin/env node
// font-heading codemod (M15-FR-083, AC-045): shadcn CLI 4.21 writes the `font-heading` utility into title slots only
// when the configured tailwind.css itself contains `--font-heading:`. That differs between the init path (the CLI's own
// index.css is present during the run) and the add path (our index.css imports theme.css), so the same install gives
// two outputs. AWR-15 §13.2 uses one sans family (--font-heading = --font-sans), so the class carries no information:
// strip it and both paths converge. Idempotent. Usage: node tools/shadcn/codemods/font-heading.mjs [--dry] [uiDir]
import { UI_DIR, eachFile } from './_lib.mjs'

const LITERAL = /(["'`])([^"'`\n]*\bfont-heading\b[^"'`\n]*)\1/g

export function transform(src) {
  return src.replace(LITERAL, (m, q, body) => {
    const kept = body.split(/\s+/).filter((c) => c && c !== 'font-heading')
    return kept.length === body.trim().split(/\s+/).length ? m : `${q}${kept.join(' ')}${q}`
  })
}
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`font-heading codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
