// Full screen, landscape lock and "add to home screen" (R-D2-35; AWR-04 §15.8.7 items 3 and 4; AWR-14 §20.26.5; M15-FR-234;
// UX-FR-156). Owner: M15. The Fullscreen API on the document element (Android, desktop, iPadOS); browsers without element
// full screen (iPhone Safari) hide the button and offer a Popover "add to the home screen for full screen" instead (the PWA
// manifest asks for display fullscreen). The landscape lock (`screen.orientation.lock('landscape')`) only shows on the
// tablet commander while in full screen or standalone mode and where the API exists; a refusal hides it again.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { Button } from '@/ui/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/ui/components/ui/popover'
import { Icon } from '@/ui/icons/Icon'

export function fullscreenSupported(): boolean {
  return typeof document !== 'undefined' && !!document.fullscreenEnabled && typeof document.documentElement.requestFullscreen === 'function'
}

function standalone(): boolean {
  try {
    return matchMedia('(display-mode: fullscreen), (display-mode: standalone)').matches
  } catch {
    return false
  }
}

function useFullscreen(): boolean {
  const [on, setOn] = React.useState(() => typeof document !== 'undefined' && !!document.fullscreenElement)
  React.useEffect(() => {
    const h = (): void => setOn(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', h)
    return () => document.removeEventListener('fullscreenchange', h)
  }, [])
  return on
}

export function FullscreenButton({ className }: { className?: string }) {
  const t = useT()
  const on = useFullscreen()
  if (!fullscreenSupported()) {
    if (standalone()) return null
    return (
      <Popover>
        <PopoverTrigger render={<Button size="icon" variant="ghost" aria-label={t('pwa.addToHome')} className={className} data-fullscreen="unsupported" />}>
          <Icon icon="ui.fullscreen" />
        </PopoverTrigger>
        <PopoverContent className="w-64 text-hud-sub" data-pwa-hint="">{t('pwa.addToHomeHint')}</PopoverContent>
      </Popover>
    )
  }
  const toggle = (): void => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => null)
    else void document.documentElement.requestFullscreen({ navigationUI: 'hide' }).catch(() => null)
  }
  return (
    <Button size="icon" variant="ghost" aria-label={t(on ? 'fullscreen.exit' : 'fullscreen.enter')} onClick={toggle} className={className} data-fullscreen={on ? 'on' : 'off'}>
      <Icon icon={on ? 'ui.fullscreen-exit' : 'ui.fullscreen'} />
    </Button>
  )
}

type LockableOrientation = ScreenOrientation & { lock?: (o: 'landscape') => Promise<void> }

export function OrientationLockButton({ className }: { className?: string }) {
  const t = useT()
  const full = useFullscreen()
  const [locked, setLocked] = React.useState(false)
  const [refused, setRefused] = React.useState(false)
  const o = (typeof screen !== 'undefined' ? screen.orientation : undefined) as LockableOrientation | undefined
  if (!o || typeof o.lock !== 'function' || refused || !(full || standalone())) return null
  const toggle = (): void => {
    if (locked) {
      try {
        o.unlock()
      } catch {
        // nothing to undo
      }
      setLocked(false)
      return
    }
    void o.lock?.('landscape').then(() => setLocked(true), () => setRefused(true))
  }
  return (
    <Button size="icon" variant={locked ? 'secondary' : 'ghost'} aria-pressed={locked} aria-label={t('orientation.lock')} onClick={toggle} className={className} data-orientation-lock={locked ? 'on' : 'off'}>
      <Icon icon="ui.orientation-lock" />
    </Button>
  )
}
