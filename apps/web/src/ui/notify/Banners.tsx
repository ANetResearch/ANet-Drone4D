// Banners (M15-FR-037; AWR-14 §4.6, §7.6, §7.7): under the header, as wide as the unobscured rect, 07 Y-axis variant.
// The connection banner appears INPUT.offlineBannerDelayMs after the link went down ("reconnecting, attempt 3, in 2 s"
// with "reconnect now"); FATAL shows the version or access problem with "reload". Server status items (status op,
// replaced by id) follow, at most 3. The total height feeds the unobscured rect (bannerVisible, bannerHeight).
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { useRt } from '@/ui/shell/RtContext'
import { useConnView } from '@/ui/shell/connView'

export const BANNER_PX = 36

/** number of visible banners (the shell reserves BANNER_PX each) */
export function useBannerCount(): number {
  const conn = useConnView((s) => s.bannerVisible)
  const status = useConnView((s) => s.status.length)
  return (conn ? 1 : 0) + status
}

export function Banners() {
  const t = useT()
  const rt = useRt()
  const visible = useConnView((s) => s.bannerVisible)
  const conn = useConnView((s) => s.conn)
  const attempt = useConnView((s) => s.attempt)
  const nextInMs = useConnView((s) => s.nextInMs)
  const code = useConnView((s) => s.code)
  const status = useConnView((s) => s.status)
  if (!visible && !status.length) return null
  const fatal = conn === 'FATAL'
  return (
    <div data-banners="" className="app-layer-banner fixed top-(--header-height) left-(--uo-x) flex w-(--uo-w) flex-col gap-1 px-2 pt-1">
      {visible ? (
        <Alert variant={fatal ? 'destructive' : 'default'} data-conn-banner={conn} className="flex h-8 items-center gap-2 py-1">
          <Icon icon="alert.linklost" />
          <AlertDescription className="truncate">
            {fatal ? t('banner.fatal', { code: code ?? 0 })
              : attempt > 0 ? t('banner.reconnecting', { n: attempt, s: fmt.num(Math.max(0, nextInMs) / 1000, 0) }) : t('banner.offline')}
          </AlertDescription>
          <AlertAction>
            {fatal ? (
              <Button size="xs" variant="outline" onClick={() => location.reload()}>{t('banner.reload')}</Button>
            ) : (
              <Button size="xs" variant="outline" onClick={() => rt.reconnectNow()}>{t('banner.reconnectNow')}</Button>
            )}
          </AlertAction>
        </Alert>
      ) : null}
      {status.map((s) => (
        <Alert key={s.id} variant={s.level === 'error' ? 'destructive' : 'default'} data-status-banner={s.id} className="flex h-8 items-center gap-2 py-1">
          <Icon icon={s.level === 'info' ? 'alert.info' : 'alert.warning'} />
          <AlertDescription className="truncate">{sanitizeText(s.message)}</AlertDescription>
        </Alert>
      ))}
    </div>
  )
}
