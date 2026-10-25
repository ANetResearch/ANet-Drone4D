// 原因码块（AWR-14 §21.15.6；事件编译失败与运行失败共用）：红描边 Alert + TriangleAlert，`code NAME` 等宽，短文案，中文界面附
// 处理建议（remedy 取 bench-api reason_view 的中文文案；英文界面只显示短文案）。
import { getLocale, hasKey, useT } from '@/app/i18n'
import { sanitizeText } from '@/lib/sanitize'
import { Alert, AlertDescription, AlertTitle } from '@/ui/components/ui/alert'
import { Icon } from '@/ui/icons/Icon'
import { reasonLine } from '@/ui/lab/runs/runs'
import type { ReasonView } from './worlds'

/** 原因码块：`code NAME` 等宽，短文案，中文界面附处理建议（§21.15.6） */
export function ReasonBlock({ r, title }: { r: ReasonView; title?: string }) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  const line = reasonLine(r, zh, { has: hasKey, t })
  return (
    <Alert className="border-destructive" data-reason={r.code}>
      <Icon icon="alert.warning" />
      <AlertTitle>{title ?? line.short}</AlertTitle>
      <AlertDescription>
        <span className="font-mono">{`${line.code} ${sanitizeText(line.name, 48)}`}</span>
        {title ? <span>{` · ${sanitizeText(line.short, 120)}`}</span> : null}
        {line.remedy ? <span className="block">{t('lab.runs.detail.remedy', { text: sanitizeText(line.remedy, 200) })}</span> : null}
      </AlertDescription>
    </Alert>
  )
}
