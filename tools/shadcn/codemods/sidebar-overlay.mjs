#!/usr/bin/env node
// sidebar-overlay codemod (M15-FR-013, FR-083; ADR-028; d04 §6 items 6 and 7; AWR-14 §4.7):
// - the built-in Mod+B keydown listener and the sidebar_state cookie are removed: the hotkey registry
//   (ui/hotkeys) is the only keyboard dispatcher and the layout is persisted in awr.ui.layout.v1 (AWR-14 §3.6);
// - sidebar-gap has width 0 (the canvas never moves), without transition;
// - sidebar-container becomes `absolute inset-0` inside the RailHost (ui/layout/RailHost.tsx) instead of
//   `fixed inset-y-0 h-svh`, loses its left/right/width transition and the offcanvas left/right offsets, and the
//   floating variant drops its p-2 (the RailHost already keeps the 8 px gap to the canvas edges and the header);
//   folding is a transform + opacity transition of the whole RailHost (transitions.dev 07 panel-reveal).
// Idempotent (marker: "M15 sidebar-overlay"). Usage: node tools/shadcn/codemods/sidebar-overlay.mjs [--dry] [uiDir]
import { UI_DIR, patchFile, replaceOnce } from './_lib.mjs'

const W = 'sidebar-overlay'
export function transform(src) {
  if (src.includes('M15 sidebar-overlay')) return src
  let s = src
  s = replaceOnce(s, 'const SIDEBAR_COOKIE_NAME = "sidebar_state"\nconst SIDEBAR_COOKIE_MAX_AGE = 60 * 60 * 24 * 7\n',
    '/* M15 sidebar-overlay: no cookie and no built-in Mod+B (hotkey registry and awr.ui.layout.v1 own them) */\n', W)
  s = replaceOnce(s, 'const SIDEBAR_KEYBOARD_SHORTCUT = "b"\n', '', W)
  s = s.replace(/\n\s*\/\/ This sets the cookie to keep the sidebar state\.\n\s*document\.cookie = `[^`]*`\n/, '\n')
  if (s.includes('document.cookie')) throw new Error(`${W}: cookie write not found (upstream changed?)`)
  s = s.replace(/\n  \/\/ Adds a keyboard shortcut to toggle the sidebar\.\n  React\.useEffect\(\(\) => \{[\s\S]*?\n  \}, \[toggleSidebar\]\)\n/, '\n')
  if (s.includes('SIDEBAR_KEYBOARD_SHORTCUT') || s.includes('addEventListener("keydown"')) throw new Error(`${W}: keyboard listener not found`)
  s = s.replace(/(data-slot="sidebar-gap"\s*\n\s*className=\{cn\(\s*\n\s*)"relative w-\(--sidebar-width\) bg-transparent transition-\[width\] duration-[a-z0-9-]+ ease-linear",/,
    '$1"relative w-0 bg-transparent",')
  s = s.replace(/\n\s*"group-data-\[collapsible=offcanvas\]:w-0",\n\s*"group-data-\[side=right\]:rotate-180",\n\s*variant === "floating" \|\| variant === "inset"\n\s*\? "group-data-\[collapsible=icon\]:w-\[calc\(var\(--sidebar-width-icon\)\+\(--spacing\(4\)\)\)\]"\n\s*: "group-data-\[collapsible=icon\]:w-\(--sidebar-width-icon\)"\n/, '\n')
  if (!s.includes('"relative w-0 bg-transparent"')) throw new Error(`${W}: sidebar-gap classes not found`)
  s = s.replace(/"fixed inset-y-0 z-10 hidden h-svh w-\(--sidebar-width\) transition-\[left,right,width\][^"]*md:flex"/,
    '"absolute inset-0 z-10 hidden md:flex"')
  if (!s.includes('"absolute inset-0 z-10 hidden md:flex"')) throw new Error(`${W}: sidebar-container classes not found`)
  s = s.replace('? "p-2 group-data-[collapsible=icon]:w-[calc(var(--sidebar-width-icon)+(--spacing(4))+2px)]"',
    '? "group-data-[collapsible=icon]:w-[calc(var(--sidebar-width-icon)+(--spacing(4))+2px)]"')
  return s
}
export const run = (dir = UI_DIR, opts = {}) => (patchFile(dir, 'sidebar', transform, opts) ? ['sidebar'] : [])

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`${W} codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
