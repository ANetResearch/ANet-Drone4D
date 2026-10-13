// Dockable side panels of the tablet commander (R-D2-36; AWR-04 §15.8.2 item 1; AWR-14 §20.26.5; M15-FR-230). Owner: M15.
// Each side is `docked` (takes width from the viewport: the unobscured rect and the camera's view centre follow), `floating`
// (laid over the viewport, the viewport keeps its full width) or `collapsed` (a 48 px icon bar). The left side holds the
// entity lists (drones, targets, nests), the right side the selected entity's detail page. The state persists in
// localStorage `awr.ui.dock.v1`. When both sides are docked the viewport keeps at least half of the window width,
// otherwise the side opened last floats (dockFit). The header buttons switch pin, float and collapse; every control has a
// 44 px hit area on coarse pointers.
import * as React from 'react'
import { useStore } from 'zustand'
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { createAwrStore } from '@/lib/createStore'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'

export type DockMode = 'docked' | 'floating' | 'collapsed'
export interface DockState { left: DockMode; right: DockMode; last: 'left' | 'right'; version: number }
export const DOCK_KEY = 'awr.ui.dock.v1'
export const DOCK_PX = { left: 320, right: 360, collapsed: 48 } as const
const MODES: readonly DockMode[] = ['docked', 'floating', 'collapsed']

function load(): DockState {
  try {
    const v = JSON.parse(globalThis.localStorage?.getItem(DOCK_KEY) ?? 'null') as Partial<DockState> | null
    if (v && MODES.includes(v.left as DockMode) && MODES.includes(v.right as DockMode)) {
      return { left: v.left as DockMode, right: v.right as DockMode, last: v.last === 'left' ? 'left' : 'right', version: 0 }
    }
  } catch {
    // storage unavailable or malformed
  }
  return { left: 'docked', right: 'docked', last: 'right', version: 0 }
}

export const dockStore = createAwrStore<DockState>('ui.dock', load)
export const useDock = <T,>(sel: (s: DockState) => T): T => useStore(dockStore, sel)

export const dock = {
  set(side: 'left' | 'right', mode: DockMode): void {
    const s = dockStore.getState()
    const next = { ...s, [side]: mode, last: mode === 'collapsed' ? s.last : side, version: s.version + 1 }
    dockStore.setState(next)
    try {
      globalThis.localStorage?.setItem(DOCK_KEY, JSON.stringify({ left: next.left, right: next.right, last: next.last }))
    } catch {
      // storage unavailable: kept for this page
    }
  },
}

/**
 * the effective modes for a window width: both docked must leave >= 50 % of the width to the viewport, else the side
 * changed last floats (pure; tests)
 */
export function dockFit(s: Pick<DockState, 'left' | 'right' | 'last'>, w: number): { left: DockMode; right: DockMode } {
  const width = (side: 'left' | 'right', m: DockMode): number => (m === 'docked' ? DOCK_PX[side] : m === 'collapsed' ? DOCK_PX.collapsed : 0)
  let { left, right } = s
  if (w - width('left', left) - width('right', right) < w / 2) {
    if (s.last === 'left' && left === 'docked') left = 'floating'
    else if (right === 'docked') right = 'floating'
    else if (left === 'docked') left = 'floating'
  }
  return { left, right }
}

export interface DockPanelProps {
  side: 'left' | 'right'
  mode: DockMode
  title: React.ReactNode
  icons: readonly { icon: IconKey; label: string; onClick: () => void }[]
  /** top offset (header) and bottom offset (task strip) in px; `banner`: the banners' height (a floating panel starts below) */
  top: number
  bottom: number
  banner?: number
  children: React.ReactNode
}

export function DockPanel({ side, mode, title, icons, top, bottom, banner = 0, children }: DockPanelProps) {
  const t = useT()
  const width = mode === 'collapsed' ? DOCK_PX.collapsed : DOCK_PX[side]
  const style = { top, bottom, width, [side]: 0 } as React.CSSProperties
  if (mode === 'collapsed') {
    return (
      <aside style={style} className={cn('fixed z-(--z-rails) flex flex-col items-center gap-1 bg-card py-2', side === 'left' ? 'border-r' : 'border-l')}
        data-dock={side} data-dock-mode="collapsed" aria-label={typeof title === 'string' ? title : undefined}>
        <Button size="icon" variant="ghost" aria-label={t('dock.expand')} onClick={() => dock.set(side, 'docked')} data-dock-expand={side}>
          <Icon icon={side === 'left' ? 'panel.left' : 'panel.right'} />
        </Button>
        {icons.map((x) => (
          <Button key={x.label} size="icon" variant="ghost" aria-label={x.label} onClick={() => {
            x.onClick()
            dock.set(side, 'docked')
          }}>
            <Icon icon={x.icon} />
          </Button>
        ))}
      </aside>
    )
  }
  const floating = mode === 'floating'
  return (
    <aside style={floating ? { ...style, [side]: 8, top: top + banner + 8, bottom: bottom + 8 } : style}
      className={cn('fixed z-(--z-rails) flex min-h-0 flex-col bg-card', floating ? 'rounded-xl shadow-lg ring-1 ring-foreground/10' : side === 'left' ? 'border-r' : 'border-l')}
      data-dock={side} data-dock-mode={mode}>
      <div className="flex h-12 shrink-0 items-center gap-1 border-b px-2">
        <div className="min-w-0 flex-1 truncate text-hud-title font-semibold">{title}</div>
        <Button size="icon" variant="ghost" aria-label={t(floating ? 'dock.pin' : 'dock.float')} onClick={() => dock.set(side, floating ? 'docked' : 'floating')}
          data-dock-toggle={side} aria-pressed={!floating}>
          <Icon icon={floating ? 'ui.dock' : 'ui.undock'} />
        </Button>
        <Button size="icon" variant="ghost" aria-label={t('dock.collapse')} onClick={() => dock.set(side, 'collapsed')} data-dock-collapse={side}>
          <Icon icon={side === 'left' ? 'panel.left' : 'panel.right'} />
        </Button>
      </div>
      {/* 12 px padding: the 44 px hit areas of small controls at the edge stay inside the scroll box */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-3">{children}</div>
    </aside>
  )
}
