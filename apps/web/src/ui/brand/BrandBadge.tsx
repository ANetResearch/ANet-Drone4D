// Full badge (M15-FR-110; AWR-15 §4.2-§4.4): used unchanged (no recolour, no filter), width >= 320 px (480 px on the boot
// mask and report cover), only on solid Graphite surfaces, never inside [data-viewport]. Entrance: opacity +
// translateY(--distance-micro), --duration-fast, --ease-smooth-out (the .brand-enter class; reduced shows it directly).
import { cn } from '@/lib/utils'
import { useT } from '@/app/i18n'
import { BADGE_ASPECT, BRAND } from './assets'

export function BrandBadge({ width = 480, className, enter = false }: { /** CSS px; 320 or 480 in D1 layouts */ width?: number; className?: string; enter?: boolean }) {
  const t = useT()
  const w = Math.max(320, width)
  return (
    <img
      src={BRAND.badge}
      width={w}
      height={Math.round(w / BADGE_ASPECT)}
      alt={t('brand.badgeAlt')}
      data-brand="badge"
      className={cn('block h-auto max-w-full', enter && 'brand-enter', className)}
      style={{ width: w }}
    />
  )
}
