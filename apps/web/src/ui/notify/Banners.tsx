// Banners (M15-FR-037; AWR-14 §4.6, §7.6, §7.7): under the header, as wide as the unobscured rect, 07 Y-axis variant.
// The connection banner appears INPUT.offlineBannerDelayMs after the link went down ("reconnecting, attempt 3, in 2 s"
// with "reconnect now"); FATAL shows the version or access problem with "reload". Server status items (status op,
// replaced by id) follow, at most 3. The total height feeds the unobscured rect (bannerVisible, bannerHeight).
import { reasonText, useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { useRt } from '@/ui/shell/RtContext'
import { useConnView } from '@/ui/shell/connView'
import { useTimeline } from '@/stores/timeline'
import { leaveReplay } from '@/ui/views/replayFlow'

export const BANNER_PX = 36

/** number of visible banners (the shell reserves BANNER_PX each) */
export function useBannerCount(): number {
  const conn = useConnView((s) => s.bannerVisible)
  const status = useConnView((s) => s.status.length)
  const replay = useTimeline((s) => s.mode === 'replay')
  return (conn ? 1 : 0) + status + (replay ? 1 : 0)
}

/**
 * Replay banner (AWR-14 §3.2 (c), §5.4; M12 §8.1, §8.7): run, segment and world, the REPLAY badge and "read-only, commands
 * disabled", with "back to live"; when the replay worker stopped (playbackState error) it shows the reason instead.
 */
function ReplayBanner() {
  const t = useT()
  const run = useTimeline((s) => s.runId)
  const seg = useTimeline((s) => s.segment)
  const status = useTimeline((s) => s.playback?.status ?? null)
  const code = useTimeline((s) => s.playback?.code ?? null)
  const world = useConnView((s) => s.sessionWorldId)
  const error = status === 'error'
  return (
    <Alert data-replay-banner={status ?? ''} className="flex h-8 items-center gap-2 py-1">
      <Icon icon={error ? 'alert.warning' : 'tl.history'} />
      <AlertDescription className="flex min-w-0 items-center gap-2 truncate">
        {error ? (
          <span className="truncate">{t('replay.banner.error', { reason: reasonText(code ?? 213).short })}</span>
        ) : (
          <>
            <span className="truncate font-mono">{t('replay.banner.title', { run: run ?? '—', seg, world: world ?? '—' })}</span>
            <span className="rounded-sm border px-1.5 font-mono text-hud-cap">REPLAY</span>
            <span className="truncate text-muted-foreground">{t('replay.banner.readOnly')}</span>
          </>
        )}
      </AlertDescription>
      <AlertAction>
        <Button size="xs" variant="outline" data-replay-exit="" onClick={() => void leaveReplay()}>
          <Icon icon="tl.live" data-icon="inline-start" />
          {t('replay.backToLive')}
        </Button>
      </AlertAction>
    </Alert>
  )
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
  const replay = useTimeline((s) => s.mode === 'replay')
  if (!visible && !status.length && !replay) return null
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
      {replay ? <ReplayBanner /> : null}
      {status.map((s) => (
        <Alert key={s.id} variant={s.level === 'error' ? 'destructive' : 'default'} data-status-banner={s.id} className="flex h-8 items-center gap-2 py-1">
          <Icon icon={s.level === 'info' ? 'alert.info' : 'alert.warning'} />
          <AlertDescription className="truncate">{sanitizeText(s.message)}</AlertDescription>
        </Alert>
      ))}
    </div>
  )
}
