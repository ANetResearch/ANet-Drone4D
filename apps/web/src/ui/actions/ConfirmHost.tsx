// Global confirmation for commands started without their button in view (hotkeys Shift+R, Shift+L, Shift+T, Delete,
// menus, the command palette; AWR-14 §6.10 rule 3, §6.11, §13.4): one AlertDialog driven by a UI store, initial focus on
// Cancel (UX-FR-050), the action button carries the verb and the object ("Return 37 vehicles"). The dialog caps the
// canvas at 15 fps while open (M15-FR-007).
import * as React from 'react'
import { useStore } from 'zustand'
import { useT } from '@/app/i18n'
import { createAwrStore } from '@/lib/createStore'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { useModalFrameCap } from '@/ui/views/useModalFrameCap'

export interface ConfirmRequest { id: string; title: string; body: string; action: string; run: () => void }
interface ConfirmState { req: ConfirmRequest | null }
const confirmStore = createAwrStore<ConfirmState>('ui.confirm', () => ({ req: null }))

/** ask for confirmation, then run */
export function confirmThen(req: ConfirmRequest): void {
  confirmStore.setState({ req })
}

export function ConfirmHost() {
  const t = useT()
  const req = useStore(confirmStore, (s) => s.req)
  const cancelRef = React.useRef<HTMLButtonElement>(null)
  const cap = useModalFrameCap('modal:confirm')
  const [shown, setShown] = React.useState<ConfirmRequest | null>(null)
  if (req && req !== shown) setShown(req)
  return (
    <AlertDialog open={req !== null} onOpenChange={(o) => {
      cap.onOpenChange(o)
      if (!o) confirmStore.setState({ req: null })
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <AlertDialogContent initialFocus={cancelRef} data-confirm={shown?.id}>
        <AlertDialogHeader>
          <AlertDialogTitle>{shown?.title}</AlertDialogTitle>
          <AlertDialogDescription>{shown?.body}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel ref={cancelRef}>{t('common.cancel')}</AlertDialogCancel>
          <AlertDialogAction data-confirm-action={shown?.id} onClick={() => {
            const r = confirmStore.getState().req
            confirmStore.setState({ req: null })
            r?.run()
          }}>
            {shown?.action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
