// Header lock-up (M15-FR-110; AWR-15 §4.5; ADR-056): avatar 24 px + "ANet Drone4D" (text-hud-title 600) + 1 x 20 px separator +
// "World Runtime" (13 px 400, muted); the compact header drops the secondary name. No crop, border, shadow or filter.
import { Separator } from '@/ui/components/ui/separator'
import { useT } from '@/app/i18n'
import { BRAND } from './assets'

export function BrandLockup({ compact = false }: { compact?: boolean }) {
  const t = useT()
  return (
    <div data-brand="lockup" className="flex h-full shrink-0 items-center gap-2.5">
      <img src={BRAND.avatar96} width={24} height={24} alt="" aria-hidden="true" data-brand-avatar="" className="size-6 shrink-0" />
      <span className="text-hud-title font-semibold whitespace-nowrap text-foreground">{t('brand.product')}</span>
      {compact ? null : (
        <>
          <Separator orientation="vertical" className="h-5!" />
          <span className="text-hud-title font-normal whitespace-nowrap text-muted-foreground">{t('brand.runtime')}</span>
        </>
      )}
    </div>
  )
}
