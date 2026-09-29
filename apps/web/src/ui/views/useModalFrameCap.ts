// Modal frame cap (M15-FR-007; d02 §4.6; g07 §1.1 item 6): 15 fps from onOpenChange(true) until
// onOpenChangeComplete(false), i.e. after the closing animation finished; CAS is frozen meanwhile (ADR-012).
import * as React from 'react'
import { frameCap } from '@/ui/shell/frameCap'

const MODAL_FPS = 15
export function useModalFrameCap(source: string) {
  React.useEffect(() => () => frameCap.set(source, 0), [source])
  return {
    onOpenChange: (open: boolean) => {
      if (open) frameCap.set(source, MODAL_FPS)
    },
    onOpenChangeComplete: (open: boolean) => {
      if (!open) frameCap.set(source, 0)
    },
  }
}
