// /bench placeholder page (M15-FR-022, D1-ext; M06 §8.1): the GPU self-test content and the bench.start/state/upload
// facade belong to M06; until then the page states that the self-test is not available in this build.
import { useT } from '@/app/i18n'
import { navigate } from '@/app/router/router'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Icon } from '@/ui/icons/Icon'
import { BrandLockup } from '@/ui/brand'

export function BenchPage() {
  const t = useT()
  return (
    <main data-view="bench" className="app-layer-overlay-page flex flex-col gap-4 bg-background p-6" style={{ top: 0 }}>
      <BrandLockup />
      <Card className="max-w-xl">
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Icon icon="perf.bench" />{t('bench.title')}</CardTitle>
          <CardDescription>{t('bench.pending')}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button size="sm" variant="outline" onClick={() => navigate('/')}>{t('bench.back')}</Button>
        </CardContent>
      </Card>
    </main>
  )
}
