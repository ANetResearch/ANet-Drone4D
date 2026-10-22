import { getLocale, useT } from '@/app/i18n'
import type { TargetDetail } from '@/app/shell/engineD2'
import { camera } from '@/viewport/facade'
import { prefs } from '@/stores/prefs'
import { Button } from '@/ui/components/ui/button'

export function ActivityCard({ activity }: { activity: TargetDetail['activity'] }) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  if (!activity) return <p className="text-xs text-muted-foreground">{t('activity.loading')}</p>
  const life = activity.updates !== undefined
  return <div className="flex flex-col gap-1.5 rounded-md border p-2 text-xs" data-target-activity={activity.phase}>
    <strong>{t(life ? 'activity.life.title' : 'activity.title')}</strong>
    <span>{t(`activity.phase.${activity.phase}`)} · {activity.destination}</span>
    {activity.reason ? <span>{zh ? '当前原因：' : 'Reason: '}{activity.reason}</span> : null}
    {activity.hidden ? <span>{zh ? '已从登记入口进入设施；身份仍保留' : 'Inside the registered facility; identity retained'}</span> : null}
    {activity.updates !== undefined ? <span className="text-muted-foreground">{zh ? `已参与 ${activity.updates} 个仿真步 · 最长非计划等待 ${activity.max_unplanned_wait_s ?? 0} 秒` : `${activity.updates} simulation steps · longest unplanned wait ${activity.max_unplanned_wait_s ?? 0} s`}</span> : null}
    <span>{t('activity.progress', { distance: Math.round(activity.walked_m), visits: activity.visits })}</span>
    <span className="text-muted-foreground">{t(life && activity.phase !== 'visit' ? 'activity.seed' : 'activity.remaining', { seconds: Math.ceil(activity.remaining_s), seed: activity.seed })}</span>
    <Button size="sm" variant="outline" onClick={() => { prefs.setUi('mobileTab', 'viewport'); camera.inspectRoute(activity.route_enu_m) }}>{t('activity.route')}</Button>
    <span className="text-muted-foreground">{t(life ? 'activity.life.bound' : 'activity.bound')}</span>
  </div>
}
