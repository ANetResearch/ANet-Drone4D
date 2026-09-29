#!/usr/bin/env node
// popover-anchor codemod (M15-FR-083; AWR-14 §4.3 "3D 锚定确认"; d04 §6 item 14): PopoverContent forwards `anchor` to the
// Positioner so the GoTo confirmation Popover can anchor to a VirtualElement built from viewport.projectToScreen().
// Idempotent (marker: anchor={anchor}). Usage: node tools/shadcn/codemods/popover-anchor.mjs [--dry] [uiDir]
import { UI_DIR, patchFile, replaceOnce } from './_lib.mjs'

const W = 'popover-anchor'
export function transform(src) {
  if (src.includes('anchor={anchor}')) return src
  let s = replaceOnce(src, '  sideOffset = 4,\n  ...props\n}: PopoverPrimitive.Popup.Props', '  sideOffset = 4,\n  anchor,\n  ...props\n}: PopoverPrimitive.Popup.Props', W)
  s = replaceOnce(s, '"align" | "alignOffset" | "side" | "sideOffset"\n  >) {\n  return (\n    <PopoverPrimitive.Portal>',
    '"align" | "alignOffset" | "side" | "sideOffset" | "anchor"\n  >) {\n  return (\n    <PopoverPrimitive.Portal>', W)
  s = replaceOnce(s, '        sideOffset={sideOffset}\n', '        sideOffset={sideOffset}\n        anchor={anchor}\n', W)
  return s
}
export const run = (dir = UI_DIR, opts = {}) => (patchFile(dir, 'popover', transform, opts) ? ['popover'] : [])

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`${W} codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
