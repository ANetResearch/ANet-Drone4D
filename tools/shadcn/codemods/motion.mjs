#!/usr/bin/env node
// motion codemod (M15-FR-054, FR-051; g07 §2.4 item 1; ADR-029).
// 1. On the 18 data-slots taken over by styles/motion/base-ui.css: drop tw-animate keyframe classes, fixed durations and
//    easings, opacity-0 and starting/ending translate arbitrary values, transition shorthands (slot limited, g07 regex).
// 2. Everywhere in ui/components/ui: drop backdrop-blur-* and supports-backdrop-filter:* (ADR-029: backdrop-filter is
//    forbidden), map numeric duration-N to the transitions.dev tokens by purpose and ease-[cubic-bezier(..)] to
//    ease-smooth-out, drop arbitrary [transition:...] properties (MOT-01: no literals outside the token files).
// 3. sheet-content: drop shadow-lg (AWR-15 §6.4: large surfaces carry no shadow).
// Idempotent. Usage: node tools/shadcn/codemods/motion.mjs [--dry] [uiDir]
import { UI_DIR, dropClasses, eachFile, mapAllClassStrings, mapClasses, mapSlotClasses } from './_lib.mjs'

export const SLOTS = [
  'dialog-overlay', 'dialog-content', 'alert-dialog-overlay', 'alert-dialog-content', 'sheet-overlay', 'sheet-content',
  'dropdown-menu-content', 'dropdown-menu-sub-content', 'context-menu-content', 'context-menu-sub-content', 'menubar-content',
  'menubar-sub-content', 'popover-content', 'hover-card-content', 'select-content', 'combobox-content', 'tooltip-content',
  'accordion-content',
]
const PREFIX = String.raw`^(?:data-\[[^\]]+\]:|data-(?:open|closed|starting-style|ending-style|instant):|[a-z-]+:)*`
const SLOT_STRIP = new RegExp(PREFIX +
  String.raw`(?:animate-(?:in|out|none|accordion-(?:down|up))|fade-(?:in|out)-0|zoom-(?:in|out)-95|slide-(?:in|out)-(?:from|to)-[a-z-]+-\d+|` +
  String.raw`duration-\d+|ease-(?:in-out|linear|out|in)|transition(?:-opacity|-all|-transform)?|opacity-0|translate-[xy]-\[[^\]]+\]|` +
  String.raw`(?:supports-backdrop-filter:)?backdrop-blur-[a-z0-9]+)$`)
const BACKDROP = /^(?:[a-z0-9-]+:)*(?:backdrop-blur-[a-z0-9]+|backdrop-filter|supports-backdrop-filter:\S+)$|^supports-backdrop-filter:/
const ARB_TRANSITION = /^(?:[a-z0-9-]+:)*\[transition:[^\]]*\]$/
const DURATION = /^((?:[a-z0-9-]+:|data-\[[^\]]+\]:)*)duration-(\d+)$/
const EASE_ARB = /^((?:[a-z0-9-]+:|data-\[[^\]]+\]:)*)ease-\[cubic-bezier\([^\]]*\)\]$/

/** transitions.dev token by purpose (AWR-15 §8.2 rule 1): <=150 quick, <=250 fast, <=350 medium, <=400 slow, else very-slow. */
export function durationToken(ms) {
  if (ms <= 150) return 'quick'
  if (ms <= 250) return 'fast'
  if (ms <= 350) return 'medium'
  if (ms <= 400) return 'slow'
  return 'very-slow'
}

export function transform(src) {
  let out = mapSlotClasses(src, SLOTS, (cls, slot) => {
    let c = dropClasses(cls, SLOT_STRIP)
    if (slot === 'sheet-content') c = dropClasses(c, /^shadow-lg$/)
    return c
  })
  out = mapAllClassStrings(out, (cls) => mapClasses(cls, (t) => {
    if (BACKDROP.test(t) || ARB_TRANSITION.test(t)) return null
    let m = DURATION.exec(t)
    if (m) return `${m[1]}duration-${durationToken(Number(m[2]))}`
    m = EASE_ARB.exec(t)
    if (m) return `${m[1]}ease-smooth-out`
    return t
  }))
  return out
}

export function run(dir = UI_DIR, opts = {}) {
  return eachFile(dir, (src) => transform(src), opts)
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const dir = process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR
  const changed = run(dir, { dry })
  console.log(`motion codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}${changed.length ? ': ' + changed.join(', ') : ''}`)
}
