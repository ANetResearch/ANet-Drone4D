#!/usr/bin/env node
// Inline node_modules/shadcn/dist/tailwind.css as apps/web/src/styles/shadcn-tailwind.css (M15-FR-044; g07 §3.5), so the
// build has no runtime dependency on the shadcn CLI package. Deterministic rewrite for the project lint rules:
//   * `#000` in the scroll-fade mask gradients -> `oklch(0 0 0)` (mask alpha only; VIS-L-01 forbids hex outside theme.css);
//   * the `1ms` duration of the scroll-driven scroll-fade animations -> var(--duration-epsilon) (MOT-01; the duration of a
//     scroll-driven animation is irrelevant, it only has to be non-zero);
//   * the `shimmer` utility block is dropped: it is an infinite loop outside the DOM motion budget; shimmer text is
//     ui/motion/ShimmerText (.t-shimmer, loop budget <= 2, ADR-029; g07 §2.3 row 15).
// Usage: node tools/shadcn/inline-shadcn-tailwind.mjs [--check]
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(ROOT, 'node_modules', 'shadcn', 'dist', 'tailwind.css')
const OUT = join(ROOT, 'apps', 'web', 'src', 'styles', 'shadcn-tailwind.css')

export function build(css, version) {
  const cut = css.indexOf('/* shimmer */')
  const body = (cut >= 0 ? css.slice(0, cut) : css).replace(/#000\b/g, 'oklch(0 0 0)').replace(/\b1ms\b/g, 'var(--duration-epsilon)').trimEnd()
  return `/* shadcn/tailwind.css ${version} inlined by tools/shadcn/inline-shadcn-tailwind.mjs (DO NOT EDIT; see the script header) */\n${body}\n`
}

const version = JSON.parse(readFileSync(join(ROOT, 'node_modules', 'shadcn', 'package.json'), 'utf8')).version
const out = build(readFileSync(SRC, 'utf8'), version)
if (process.argv.includes('--check')) {
  const cur = existsSync(OUT) ? readFileSync(OUT, 'utf8') : ''
  if (cur !== out) {
    console.error('inline-shadcn-tailwind: styles/shadcn-tailwind.css is out of date; run node tools/shadcn/inline-shadcn-tailwind.mjs')
    process.exit(1)
  }
  console.log('inline-shadcn-tailwind: up to date')
} else {
  writeFileSync(OUT, out)
  console.log(`inline-shadcn-tailwind: wrote ${OUT}`)
}
