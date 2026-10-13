// D2 banners of the shell (AWR-14 §20.4.4, §20.4.6, §20.12.3; M15-FR-206, FR-203): the sandbox session bar (one banner,
// 32 px like the replay banner), the expiry countdown while `sandbox.expiring` runs, and on static browsing the info line
// "static browsing: no simulation runs in this city · open a sandbox here · back to the shared show". They join the
// banner stack of ui/notify/Banners.tsx, so the unobscured rect moves down by one banner height each.
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { DEMO_ENTRY } from '@/lib/demo'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { useSurface } from '@/app/shell/surface'
import { sandboxDialogs } from './dialogs'
import { useSandbox } from './session'

/** number of D2 banners shown now */
export function useSandboxBannerCount(session = true): number {
  const surface = useSurface((s) => s.surface)
  const expiring = useSandbox((s) => s.expiring !== null)
  // D2.1: the phone shell shows the session in its status bar and the session actions in its "more" menu (no session strip)
  return (surface === 'sandbox' && session ? 1 : 0) + (surface === 'sandbox' && expiring ? 1 : 0) + (surface === 'static' ? 1 : 0)
}

/** the static browsing line (no realtime link in this city) */
export function StaticBanner() {
  const t = useT()
  const world = useSurface((s) => s.worldId)
  return (
    <Alert data-static-banner={world ?? ''} className="flex h-8 items-center gap-2 py-1">
      <Icon icon="alert.info" />
      <AlertDescription className="truncate">{t('hint.staticBrowse')}</AlertDescription>
      <AlertAction className="flex gap-1">
        <Button size="xs" variant="outline" onClick={() => sandboxDialogs.open('start', { world })} data-static-open-sandbox="">{t('landing.sandbox.open')}</Button>
        <Button size="xs" variant="ghost" onClick={() => navigate(DEMO_ENTRY)}>{t('static.backToShow')}</Button>
      </AlertAction>
    </Alert>
  )
}
