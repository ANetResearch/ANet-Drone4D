// "Site updated · reload" (R-D2-29; ADR-118; AWR-04 §15.4.2 item 3; AWR-14 §20.26.2 rule 4; M15-FR-225; UX-FR-148).
// Owner: M15. The capacity response carries the server's build; when its `release` differs from this bundle's (stores/
// capacity `updated`, local builds never compare) a non-modal Alert under the header offers a reload. It never reloads by
// itself (a sandbox operation must not be interrupted), can be dismissed, and shows once per release (sessionStorage).
import * as React from 'react'
import { useT } from '@/app/i18n'
import { useCapacity } from '@/stores/capacity'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Icon } from '@/ui/icons/Icon'
import { power } from '@/app/power/PowerController'

const KEY = 'awr.build.dismissed'

function dismissed(): string | null {
  try {
    return globalThis.sessionStorage?.getItem(KEY) ?? null
  } catch {
    return null
  }
}

export function UpdateBanner() {
  const t = useT()
  const updated = useCapacity((s) => s.updated)
  const release = useCapacity((s) => s.data?.build.release ?? null)
  const [gone, setGone] = React.useState<string | null>(() => dismissed())
  if (!updated || !release || gone === release) return null
  const close = (): void => {
    try {
      globalThis.sessionStorage?.setItem(KEY, release)
    } catch {
      // storage unavailable: dismissed for this page only
    }
    setGone(release)
  }
  return (
    <div className="pointer-events-none fixed inset-x-0 top-(--header-height) z-(--z-banner) flex justify-center px-4 pt-1" data-update-banner={release}>
      <Alert role="status" className="pointer-events-auto flex w-auto max-w-[32rem] items-center gap-2 bg-card py-1.5">
        <Icon icon="alert.info" />
        <AlertDescription className="text-foreground">{t('build.updated')}</AlertDescription>
        <AlertAction className="static flex gap-1">
          <Button size="xs" onClick={() => { power.expectNavigation(); location.reload() }} data-update-reload="">{t('build.reload')}</Button>
          <Button size="xs" variant="ghost" onClick={close} aria-label={t('common.close')}><Icon icon="close" /></Button>
        </AlertAction>
      </Alert>
    </div>
  )
}
