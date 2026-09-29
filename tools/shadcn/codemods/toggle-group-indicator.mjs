#!/usr/bin/env node
// toggle-group-indicator codemod (M15-FR-083; AWR-14 §4.7; d03 §4.2: camera modes use a sliding indicator, not a morph):
// a single-select ToggleGroup renders an absolutely positioned indicator block. When the pressed item or the group size
// changes (MutationObserver on data-pressed, ResizeObserver), the indicator reads the item's offsetLeft/offsetTop/
// offsetWidth/offsetHeight and writes only `translate`, `width` and `height`; the transition (--tabs-dur, --tabs-ease) is
// in styles/motion/base-ui.css. The first placement carries data-instant (no transition), reduced motion is 0 s.
// Grids (the 3 x 4 weather presets) opt out with indicator={false} (AWR-14 §4.2).
// Idempotent (marker: data-slot="toggle-group-indicator"). Usage: node .../toggle-group-indicator.mjs [--dry] [uiDir]
import { UI_DIR, patchFile, replaceOnce } from './_lib.mjs'

const W = 'toggle-group-indicator'
const HOOK = `
/* M15 toggle-group-indicator (tools/shadcn/codemods/toggle-group-indicator.mjs) */
function useToggleGroupIndicator(enabled: boolean) {
  const rootRef = React.useRef<HTMLDivElement>(null)
  const indicatorRef = React.useRef<HTMLSpanElement>(null)
  React.useLayoutEffect(() => {
    const root = rootRef.current
    const ind = indicatorRef.current
    if (!enabled || !root || !ind) return
    let first = true
    let raf = 0
    const update = () => {
      const item = root.querySelector<HTMLElement>(':scope > [data-slot="toggle-group-item"][data-pressed]')
      if (!item) {
        ind.dataset.hidden = ""
        return
      }
      delete ind.dataset.hidden
      if (first) ind.dataset.instant = ""
      ind.style.translate = \`\${item.offsetLeft}px \${item.offsetTop}px\`
      ind.style.width = \`\${item.offsetWidth}px\`
      ind.style.height = \`\${item.offsetHeight}px\`
      if (first) {
        first = false
        raf = requestAnimationFrame(() => delete ind.dataset.instant)
      }
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(root)
    const mo = new MutationObserver(update)
    mo.observe(root, { subtree: true, attributes: true, attributeFilter: ["data-pressed"] })
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      mo.disconnect()
    }
  }, [enabled])
  return { rootRef, indicatorRef }
}
`

export function transform(src) {
  if (src.includes('data-slot="toggle-group-indicator"')) return src
  let s = src
  s = replaceOnce(s, '\nfunction ToggleGroup({', `${HOOK}\nfunction ToggleGroup({`, W)
  s = replaceOnce(s, '  orientation = "horizontal",\n  children,\n  ...props\n}: ToggleGroupPrimitive.Props &\n  VariantProps<typeof toggleVariants> & {\n    spacing?: number\n    orientation?: "horizontal" | "vertical"\n  }) {\n  return (',
    '  orientation = "horizontal",\n  indicator = true,\n  children,\n  ...props\n}: ToggleGroupPrimitive.Props &\n  VariantProps<typeof toggleVariants> & {\n    spacing?: number\n    orientation?: "horizontal" | "vertical"\n    /** sliding indicator for single-select groups (AWR-14 §4.7); false for grids */\n    indicator?: boolean\n  }) {\n' +
    '  const withIndicator = indicator && !props.multiple\n  const { rootRef, indicatorRef } = useToggleGroupIndicator(withIndicator)\n  return (', W)
  s = replaceOnce(s, '    <ToggleGroupPrimitive\n      data-slot="toggle-group"\n', '    <ToggleGroupPrimitive\n      ref={rootRef}\n      data-slot="toggle-group"\n      data-indicator={withIndicator ? "" : undefined}\n', W)
  s = replaceOnce(s, '"group/toggle-group flex w-fit', '"group/toggle-group relative flex w-fit', W)
  s = replaceOnce(s, '      {...props}\n    >\n      <ToggleGroupContext.Provider',
    '      {...props}\n    >\n      {withIndicator && <span ref={indicatorRef} data-slot="toggle-group-indicator" data-hidden="" aria-hidden="true" />}\n      <ToggleGroupContext.Provider', W)
  return s
}
export const run = (dir = UI_DIR, opts = {}) => (patchFile(dir, 'toggle-group', transform, opts) ? ['toggle-group'] : [])

if (import.meta.url === `file://${process.argv[1]}`) {
  const dry = process.argv.includes('--dry')
  const changed = run(process.argv.slice(2).find((a) => !a.startsWith('--')) ?? UI_DIR, { dry })
  console.log(`${W} codemod: ${changed.length} file(s) ${dry ? 'would change' : 'changed'}`)
}
