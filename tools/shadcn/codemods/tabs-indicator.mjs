#!/usr/bin/env node
// tabs-indicator codemod (M15-FR-083; g07 §2.4 item 4; AWR-14 §4.7; transitions.dev 16 tabs-sliding and 08 page-side-by-side):
// - TabsList renders <TabsPrimitive.Indicator data-slot="tabs-indicator" data-variant={variant}/> for both variants
//   (default: sliding pill; line: 2 px foreground underline). Geometry comes from Base UI's --active-tab-* variables,
//   the transition from styles/motion/base-ui.css;
// - the line variant's after: pseudo-element underline in TabsTrigger is removed (replaced by the Indicator);
// - TabsPanels (single cell grid) is added so entering and leaving panels overlap during the 08 slide.
// Idempotent (marker: data-slot="tabs-indicator"). Usage: node tools/shadcn/codemods/tabs-indicator.mjs [--dry] [uiDir]
import { UI_DIR, patchFile, replaceOnce } from './_lib.mjs'

const W = 'tabs-indicator'
export function transform(src) {
  if (src.includes('data-slot="tabs-indicator"')) return src
  let s = src
  if (!/^import \* as React from "react"/m.test(s)) s = s.replace(/^("use client"\n\n)?/, (m) => `${m}import * as React from "react"\n`)
  s = replaceOnce(s, 'function TabsList({\n  className,\n  variant = "default",\n  ...props\n}',
    'function TabsList({\n  className,\n  variant = "default",\n  children,\n  ...props\n}', W)
  s = replaceOnce(s, '      className={cn(tabsListVariants({ variant }), className)}\n      {...props}\n    />',
    '      className={cn(tabsListVariants({ variant }), "relative", className)}\n      {...props}\n    >\n      {children}\n' +
    '      <TabsPrimitive.Indicator data-slot="tabs-indicator" data-variant={variant ?? "default"} />\n    </TabsPrimitive.List>', W)
  s = s.replace(/\n\s*"after:absolute after:bg-foreground[^"\n]*",/, '')
  if (s.includes('after:bg-foreground')) throw new Error(`${W}: line-variant after: underline not found (upstream changed?)`)
  s = replaceOnce(s, 'export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }',
    '/* Single cell grid: the entering and the leaving panel overlap during the page slide (transitions.dev 08). */\n' +
    'function TabsPanels({ className, ...props }: React.ComponentProps<"div">) {\n' +
    '  return <div data-slot="tabs-panels" className={cn("grid", className)} {...props} />\n}\n\n' +
    'export { Tabs, TabsList, TabsTrigger, TabsContent, TabsPanels, tabsListVariants }', W)
  return s
}
export const run = (dir = UI_DIR, opts = {}) => (patchFile(dir, 'tabs', transform, opts) ? ['tabs'] : [])

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`${W} codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
