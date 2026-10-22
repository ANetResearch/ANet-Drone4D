import type { Target } from '@awr/contracts/types'
import { useT, getLocale } from '@/app/i18n'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/ui/components/ui/dialog'
import { CustomizationForm } from './CustomizationForm'

export function CreateTargetDialog({ cls, close, place }: {
  cls: Target['class'] | null; close: () => void; place: (cls: Target['class'], patch: Partial<Target>) => void
}) {
  const t = useT()
  const zh = getLocale() === 'zh-CN'
  return <Dialog open={cls !== null} onOpenChange={(open) => { if (!open) close() }}>
    <DialogContent className="max-h-dvh overflow-y-auto sm:max-w-lg" data-create-target="">
      <DialogHeader>
        <DialogTitle>{zh ? '手动配置识别物' : 'Configure a target'}</DialogTitle>
        <DialogDescription>{zh ? '先选择外观和身份，再在场景中选择放置位置。新建目标初始静止，可在详情中配置运动。' : 'Choose appearance and identity, then click a position in the scene. New targets start stationary; motion is configured in their details.'}</DialogDescription>
      </DialogHeader>
      {cls ? <CustomizationForm key={cls} cls={cls} target={{ label: t(`target.class.${cls}`) }} operator creating save={async (patch) => { close(); place(cls, patch); return true }} /> : null}
    </DialogContent>
  </Dialog>
}
