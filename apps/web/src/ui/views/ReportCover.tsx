// Test report cover (M15-FR-080, FR-110; AWR-18 §11.4; ADR-032): the full brand badge at 480 px (unchanged, on the light
// paper surface), the report title, run id, gate level, report kind, date and commit.
import { useT } from '@/app/i18n'
import { BrandBadge } from '@/ui/brand'

export interface CoverInfo { runId: string; gate: string; kind: string; date: string; commit: string }

export function ReportCover({ info }: { info: CoverInfo }) {
  const t = useT()
  return (
    <header data-report-cover="" className="flex flex-col items-start gap-4 border-b pb-6">
      <BrandBadge width={480} />
      <h1 className="text-ed-title font-bold">{t('report.title')}</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="text-muted-foreground">{t('report.runId')}</dt><dd className="font-mono">{info.runId}</dd>
        <dt className="text-muted-foreground">{t('report.gate')}</dt><dd className="font-mono">{info.gate}</dd>
        <dt className="text-muted-foreground">{t('report.kind')}</dt><dd className="font-mono">{info.kind}</dd>
        <dt className="text-muted-foreground">{t('report.date')}</dt><dd className="font-mono">{info.date}</dd>
        <dt className="text-muted-foreground">{t('report.commit')}</dt><dd className="font-mono">{info.commit}</dd>
      </dl>
    </header>
  )
}
