import type { Target } from '@awr/contracts/types'
import { useT } from '@/app/i18n'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/ui/components/ui/dialog'
import { CustomizationForm } from './CustomizationForm'

export function CreateTargetDialog({ cls, close, place }: {
  cls: Target['class'] | null; close: () => void; place: (cls: Target['class'], patch: Partial<Target>) => void
}) {
  const t = useT()
  return <Dialog open={cls !== null} onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="max-h-dvh overflow-y-auto sm:max-w-lg" data-create-target="">
      <DialogHeader>
        <DialogTitle>{t('targets.createTargetDialog.configure_a_target')}</DialogTitle>
        <DialogDescription>{t('targets.createTargetDialog.choose_appearance_and_identity_then_click_a')}</DialogDescription>
      </DialogHeader>
      {cls ? <CustomizationForm key={cls} cls={cls} target={{ label: t(`target.class.${cls}`) }} operator creating save={async (patch) => { close(); place(cls, patch); return true }} /> : null}
    </DialogContent>
  </Dialog>
}
