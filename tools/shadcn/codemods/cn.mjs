#!/usr/bin/env node
// cn codemod (M15-FR-046, FR-083; g07 §3.4): `import { cn } from "cn"` -> `from "@/lib/utils"`.
// The default cn table does not know the @theme names (text-hud-*, text-ed-*, ease-*, duration-*, blur-*);
// lib/utils.ts registers them with createCn so class conflicts resolve correctly.
// Idempotent. Usage: node tools/shadcn/codemods/cn.mjs [--dry] [uiDir]
import { UI_DIR, eachFile } from './_lib.mjs'

export const transform = (src) => src.replace(/import\s*\{\s*cn\s*\}\s*from\s*["']cn["']/g, 'import { cn } from "@/lib/utils"')
export const run = (dir = UI_DIR, opts = {}) => eachFile(dir, transform, opts)

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`cn codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
