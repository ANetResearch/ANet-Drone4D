#!/usr/bin/env node
// accordion-single-chevron codemod (M15-FR-083; g07 §2.4 item 3; transitions.dev 21 accordion):
// - one ChevronDownIcon flipped with scaleY(-1) by styles/motion/base-ui.css instead of two icons swapped instantly;
// - the Panel keeps only layout classes (the keyframe classes are removed, base-ui.css animates the height variable);
// - the inner div gets data-slot="accordion-content-inner" (fade and blur target) and loses the height classes that
//   never apply there (the inner div is not a Base UI part).
// Idempotent (marker: data-slot="accordion-content-inner"). Usage: node .../accordion-single-chevron.mjs [--dry] [uiDir]
import { UI_DIR, patchFile, replaceOnce } from './_lib.mjs'

const W = 'accordion-single-chevron'
export function transform(src) {
  if (src.includes('data-slot="accordion-content-inner"')) return src
  let s = src.replace(/import \{ ChevronDownIcon, ChevronUpIcon \} from/, 'import { ChevronDownIcon } from')
  s = s.replace(/\n\s*<ChevronDownIcon data-slot="accordion-trigger-icon"[^\n]*\/>\n\s*<ChevronUpIcon data-slot="accordion-trigger-icon"[^\n]*\/>/,
    '\n        <ChevronDownIcon data-slot="accordion-trigger-icon" className="pointer-events-none shrink-0" />')
  if (s.includes('ChevronUpIcon')) throw new Error(`${W}: two-icon trigger not found (upstream changed?)`)
  s = s.replace(/(data-slot="accordion-content"\s*\n\s*className=")[^"]*(")/, '$1overflow-hidden px-2 text-xs/relaxed$2')
  s = replaceOnce(s, '<div\n        className={cn(\n          "h-(--accordion-panel-height) pt-0 pb-4 data-ending-style:h-0 data-starting-style:h-0 ',
    '<div\n        data-slot="accordion-content-inner"\n        className={cn(\n          "pt-0 pb-4 ', W)
  return s
}
export const run = (dir = UI_DIR, opts = {}) => (patchFile(dir, 'accordion', transform, opts) ? ['accordion'] : [])

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`${W} codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
