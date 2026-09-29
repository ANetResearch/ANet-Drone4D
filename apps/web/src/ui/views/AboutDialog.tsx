// About dialog (M15-FR-110; AWR-15 §4.2): the full badge at 320 px on the popover surface, product name and version.
// AboutContent is the single About placement, shared by this dialog and the About tab of Settings (M15-FR-026).
import { useT } from '@/app/i18n'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { BrandBadge } from '@/ui/brand'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { useModalFrameCap } from './useModalFrameCap'

export const APP_VERSION = '0.1.0'

/** badge (320 px) and version line; the About placement of AWR-15 §4.2 */
export function AboutContent({ showVersion = true }: { showVersion?: boolean }) {
  const t = useT()
  return (
    <div className="flex flex-col items-center gap-3 py-2">
      <BrandBadge width={320} />
      {showVersion ? <span className="text-hud-sub text-muted-foreground">{t('about.version', { version: APP_VERSION })}</span> : null}
    </div>
  )
}

export function AboutDialog() {
  const t = useT()
  const open = useOverlays((s) => s.about)
  const cap = useModalFrameCap('modal:about')
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      overlays.set('about', o)
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-[25rem]">
        <DialogHeader>
          <DialogTitle>{t('about.title')}</DialogTitle>
          <DialogDescription>{t('about.version', { version: APP_VERSION })}</DialogDescription>
        </DialogHeader>
        <AboutContent showVersion={false} />
      </DialogContent>
    </Dialog>
  )
}
