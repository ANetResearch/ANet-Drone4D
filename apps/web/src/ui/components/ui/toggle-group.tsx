"use client"

import * as React from "react"
import { Toggle as TogglePrimitive } from "@base-ui/react/toggle"
import { ToggleGroup as ToggleGroupPrimitive } from "@base-ui/react/toggle-group"
import { type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

import { toggleVariants } from "@/ui/components/ui/toggle"

const ToggleGroupContext = React.createContext<
  VariantProps<typeof toggleVariants> & {
    spacing?: number
    orientation?: "horizontal" | "vertical"
  }
>({
  size: "default",
  variant: "default",
  spacing: 2,
  orientation: "horizontal",
})

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
      ind.style.translate = `${item.offsetLeft}px ${item.offsetTop}px`
      ind.style.width = `${item.offsetWidth}px`
      ind.style.height = `${item.offsetHeight}px`
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

function ToggleGroup({
  className,
  variant,
  size,
  spacing = 2,
  orientation = "horizontal",
  indicator = true,
  children,
  ...props
}: ToggleGroupPrimitive.Props &
  VariantProps<typeof toggleVariants> & {
    spacing?: number
    orientation?: "horizontal" | "vertical"
    /** sliding indicator for single-select groups (AWR-14 §4.7); false for grids */
    indicator?: boolean
  }) {
  const withIndicator = indicator && !props.multiple
  const { rootRef, indicatorRef } = useToggleGroupIndicator(withIndicator)
  return (
    <ToggleGroupPrimitive
      ref={rootRef}
      data-slot="toggle-group"
      data-indicator={withIndicator ? "" : undefined}
      data-variant={variant}
      data-size={size}
      data-spacing={spacing}
      data-orientation={orientation}
      style={{ "--gap": spacing } as React.CSSProperties}
      className={cn(
        "group/toggle-group relative flex w-fit flex-row items-center gap-[--spacing(var(--gap))] rounded-md data-[size=sm]:rounded-[min(var(--radius-md),8px)] data-vertical:flex-col data-vertical:items-stretch",
        className
      )}
      {...props}
    >
      {withIndicator && <span ref={indicatorRef} data-slot="toggle-group-indicator" data-hidden="" aria-hidden="true" />}
      <ToggleGroupContext.Provider
        value={{ variant, size, spacing, orientation }}
      >
        {children}
      </ToggleGroupContext.Provider>
    </ToggleGroupPrimitive>
  )
}

function ToggleGroupItem({
  className,
  children,
  variant = "default",
  size = "default",
  ...props
}: TogglePrimitive.Props & VariantProps<typeof toggleVariants>) {
  const context = React.useContext(ToggleGroupContext)

  return (
    <TogglePrimitive
      data-slot="toggle-group-item"
      data-variant={context.variant || variant}
      data-size={context.size || size}
      data-spacing={context.spacing}
      className={cn(
        "shrink-0 group-data-[spacing=0]/toggle-group:rounded-none group-data-[spacing=0]/toggle-group:px-2 focus:z-10 focus-visible:z-10 group-data-[spacing=0]/toggle-group:has-data-[icon=inline-end]:pr-1.5 group-data-[spacing=0]/toggle-group:has-data-[icon=inline-start]:pl-1.5 group-data-horizontal/toggle-group:data-[spacing=0]:first:rounded-l-md group-data-vertical/toggle-group:data-[spacing=0]:first:rounded-t-md group-data-horizontal/toggle-group:data-[spacing=0]:last:rounded-r-md group-data-vertical/toggle-group:data-[spacing=0]:last:rounded-b-md group-data-horizontal/toggle-group:data-[spacing=0]:data-[variant=outline]:border-l-0 group-data-vertical/toggle-group:data-[spacing=0]:data-[variant=outline]:border-t-0 group-data-horizontal/toggle-group:data-[spacing=0]:data-[variant=outline]:first:border-l group-data-vertical/toggle-group:data-[spacing=0]:data-[variant=outline]:first:border-t",
        toggleVariants({
          variant: context.variant || variant,
          size: context.size || size,
        }),
        className
      )}
      {...props}
    >
      {children}
    </TogglePrimitive>
  )
}

export { ToggleGroup, ToggleGroupItem }
