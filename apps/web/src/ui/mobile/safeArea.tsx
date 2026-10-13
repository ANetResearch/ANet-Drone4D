// Safe-area insets and small shared bits of the mobile shells (AWR-04 §15.8.7 item 5; M15-FR-230, FR-231). Owner: M15.
// index.html sets `viewport-fit=cover`; the bars pad themselves with env(safe-area-inset-*) (styles/layout.css `.mobile-top`,
// `.safe-bottom`, `.safe-x`), and the unobscured rect needs the same insets in px: safeInsets() reads them once per call
// from a hidden probe element (0 on desktops and in emulation without a notch).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { isOnline, useConnView } from '@/ui/shell/connView'
import { useSurface } from '@/app/shell/surface'
import { StateIcon } from '@/ui/icons/StateIcon'
import { usePower } from '@/app/power/store'

let probe: HTMLDivElement | null = null
export function safeInsets(): { top: number; bottom: number; left: number; right: number } {
  if (typeof document === 'undefined') return { top: 0, bottom: 0, left: 0, right: 0 }
  if (!probe) {
    probe = document.createElement('div')
    probe.className = 'safe-probe'
    probe.setAttribute('aria-hidden', 'true')
    document.body.append(probe)
  }
  const cs = getComputedStyle(probe)
  const px = (v: string): number => {
    const n = Number.parseFloat(v)
    return Number.isFinite(n) ? n : 0
  }
  return { top: px(cs.paddingTop), bottom: px(cs.paddingBottom), left: px(cs.paddingLeft), right: px(cs.paddingRight) }
}

/** the link state as one icon (online, or WifiOff while down); nothing without a realtime link (static browsing) */
export function ConnDot({ className }: { className?: string }) {
  const t = useT()
  const conn = useConnView((s) => s.conn)
  const linked = useSurface((s) => s.rtPath !== null)
  const saving = usePower((s) => s.frozen)
  if (!linked) return null
  const on = isOnline(conn)
  return (
    <span className={cn('inline-flex shrink-0 items-center', !on && !saving && 'text-brand-text', className)} data-conn={conn} aria-label={t(on ? 'conn.online' : 'conn.offline')} role="img">
      <StateIcon icon="conn.online" alt={!on} />
    </span>
  )
}

/** the shell's header height for the overlay pages and dialogs (`--header-height`); the desktop value returns on unmount */
export function useHeaderHeight(v: string): void {
  React.useEffect(() => {
    const el = document.documentElement
    el.style.setProperty('--header-height', v)
    return () => {
      el.style.removeProperty('--header-height')
    }
  }, [v])
}
