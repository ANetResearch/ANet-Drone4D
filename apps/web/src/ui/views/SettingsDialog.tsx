// Settings dialog (M15-FR-026; AWR-14 §5.6): 640 px, opened by ?settings=<tab> (deep link) or the header button; closing
// removes the parameter. Caps the canvas at 15 fps while open.
import { useT } from '@/app/i18n'
import { updateSearch, useRoute } from '@/app/router/router'
import { parseSettings, type SettingsTab } from '@/app/router/search'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { SettingsPanel } from '@/ui/panels/settings/SettingsPanel'
import { useModalFrameCap } from './useModalFrameCap'

export function SettingsDialog() {
  const t = useT()
  const route = useRoute()
  const tab = route ? parseSettings(route.search) : undefined
  const cap = useModalFrameCap('modal:settings')
  return (
    <Dialog open={tab !== undefined} onOpenChange={(open) => {
      cap.onOpenChange(open)
      if (!open) updateSearch({ settings: null })
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-[40rem]">
        <DialogHeader>
          <DialogTitle>{t('settings.title')}</DialogTitle>
          <DialogDescription>{t('settings.description')}</DialogDescription>
        </DialogHeader>
        {tab ? <SettingsPanel tab={tab} onTab={(k: SettingsTab) => updateSearch({ settings: k })} /> : null}
      </DialogContent>
    </Dialog>
  )
}
