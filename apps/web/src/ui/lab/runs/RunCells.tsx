// /lab/runs 的状态与原因码单元（AWR-14 §21.6、§21.15.4、§21.15.6）：列表、尝试记录与状态侧栏共用。
import { getLocale, hasKey, tOr, useT } from '@/app/i18n'
import { sanitizeText } from '@/lib/sanitize'
import { Badge } from '@/ui/components/ui/badge'
import { reasonLine, restartOf, type RunItem } from './runs'

/** 状态徽标（§21.15.4）；FAILED 红描边（warning） */
export function RunStateBadge({ r }: { r: Pick<RunItem, 'state' | 'managed' | 'restart_count'> }) {
  const t = useT()
  const k = restartOf(r)
  return (
    <span className="inline-flex items-center gap-1" data-run-state={r.state}>
      <Badge variant={r.state === 'COMPLETED' ? 'secondary' : 'outline'} className={r.state === 'FAILED' ? 'border-destructive text-brand-text' : undefined}>
        {tOr(`lab.runs.state.${r.state}`, sanitizeText(r.state, 24))}
      </Badge>
      {k ? <span className="text-hud-cap text-muted-foreground" data-run-restart={k}>{t('lab.runs.restart', { k })}</span> : null}
      {!r.managed ? <span className="text-hud-cap text-muted-foreground" title={t('lab.runs.unmanagedHint')} data-run-unmanaged="">{t('lab.runs.unmanaged')}</span> : null}
    </span>
  )
}

/** 原因码单元：`code NAME` 等宽 + 短文案，悬停为处理建议 */
export function RunReason({ r }: { r: RunItem['reason'] }) {
  const t = useT()
  if (!r) return <span className="text-muted-foreground">—</span>
  const line = reasonLine(r, getLocale() === 'zh-CN', { has: hasKey, t })
  return (
    <span data-run-reason={r.code} title={line.remedy ? t('lab.runs.detail.remedy', { text: sanitizeText(line.remedy, 200) }) : undefined}>
      <span className="font-mono">{`${line.code} ${sanitizeText(line.name, 40)}`}</span>
      <span className="text-muted-foreground">{` · ${sanitizeText(line.short, 80)}`}</span>
    </span>
  )
}
