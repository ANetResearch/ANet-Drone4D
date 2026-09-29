#!/usr/bin/env node
// icons codemod (M15-FR-067, FR-083; ADR-030; g07 §4): lucide-react imports -> @/ui/icons/lucide-compat.
// lucide-react is forbidden in the whole repository; the compat layer renders the 16 base-mira names with morphicons.
// Idempotent. Usage: node tools/shadcn/codemods/icons.mjs [--dry] [uiDir]
import { UI_DIR, eachFile } from './_lib.mjs'

export const transform = (src) => src.replace(/from\s+["']lucide-react["']/g, 'from "@/ui/icons/lucide-compat"')
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`icons codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
