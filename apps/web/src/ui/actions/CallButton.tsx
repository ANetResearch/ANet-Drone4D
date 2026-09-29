// Command button (M15-FR-040, §9.2; AWR-14 §6.11, §12): one shadcn Button bound to a command tracker key. READY shows the
// action icon; PENDING, ACCEPTED and RUNNING a Spinner (aria-busy); DONE_OK the CircleCheck and DONE_ERR the CircleX state
// icon (plus one 12 shake per failure) for INPUT.resultHoldMs. A pre-check reason disables the button but keeps it
// focusable and hoverable, so the Tooltip explains why (for example "take off before GoTo"); the Tooltip text is the
// aria-label. Confirmed commands open an AlertDialog whose initial focus is Cancel (UX-FR-050); the action button text
// is the verb (AWR-14 §13.5). DOM hooks for specs: [data-cmd=<op>] with data-cmd-state, [data-cmd-confirm=<op>].
import * as React from 'react'
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Button } from '@/ui/components/ui/button'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { Spinner } from '@/ui/components/ui/spinner'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import type { IconKey } from '@/ui/icons/registry'
import { comboLabel } from '@/ui/hotkeys/registry'
import { ShakeOnce } from '@/ui/motion/ShakeOnce'
import { useModalFrameCap } from '@/ui/views/useModalFrameCap'
import { isBusy, useCmdEntry } from './commands'

export interface ConfirmSpec {
  title: string
  body: string
  action: string
  /** extra fields between the text and the footer (take-off altitude) */
  children?: React.ReactNode
  actionDisabled?: boolean
}
export interface CallButtonProps {
  cmdKey: string
  op: string
  icon: IconKey
  label: string
  hotkey?: string
  /** pre-check or role reason; the button stays focusable and the Tooltip shows the reason */
  disabledReason?: string | null
  onRun: () => void
  confirm?: ConfirmSpec
  size?: 'sm' | 'default' | 'icon-sm' | 'icon'
  variant?: 'outline' | 'ghost' | 'secondary' | 'default'
  showLabel?: boolean
  className?: string
}

export function CallButton({ cmdKey, op, icon, label, hotkey, disabledReason, onRun, confirm, size = 'sm', variant = 'outline', showLabel, className }: CallButtonProps) {
  const t = useT()
  const e = useCmdEntry(cmdKey)
  const phase = e?.phase ?? 'READY'
  const busy = isBusy(phase)
  const disabled = !!disabledReason
  const [open, setOpen] = React.useState(false)
  const cancelRef = React.useRef<HTMLButtonElement>(null)
  const cap = useModalFrameCap(`modal:cmd:${cmdKey}`)
  const glyph = busy ? <Spinner />
    : phase === 'DONE_OK' ? <StateIcon icon="mission.done" spring="smooth" />
      : phase === 'DONE_ERR' ? <StateIcon icon="mission.failed" spring="smooth" />
        : <Icon icon={icon} />
  const click = () => {
    if (disabled || busy) return
    if (confirm) {
      cap.onOpenChange(true)
      setOpen(true)
    } else onRun()
  }
  return (
    <>
      <Tooltip>
        <ShakeOnce trigger={e?.shake ?? 0}>
          <TooltipTrigger
            render={
              <Button size={size} variant={variant} aria-label={label} aria-busy={busy || undefined} disabled={disabled} focusableWhenDisabled
                data-cmd={op} data-cmd-state={e?.status ?? ''} data-cmd-phase={phase} onClick={click}
                className={cn(disabled && 'aria-disabled:opacity-50', className)} />
            }
          >
            {glyph}
            {showLabel ? <span>{label}</span> : null}
          </TooltipTrigger>
        </ShakeOnce>
        <TooltipContent>
          {disabledReason ?? label}
          {hotkey && !disabledReason ? <KbdGroup>{comboLabel(hotkey).map((k) => <Kbd key={k}>{k}</Kbd>)}</KbdGroup> : null}
        </TooltipContent>
      </Tooltip>
      {confirm ? (
        <AlertDialog open={open} onOpenChange={(o) => {
          cap.onOpenChange(o)
          setOpen(o)
        }} onOpenChangeComplete={cap.onOpenChangeComplete}>
          <AlertDialogContent initialFocus={cancelRef}>
            <AlertDialogHeader>
              <AlertDialogTitle>{confirm.title}</AlertDialogTitle>
              <AlertDialogDescription>{confirm.body}</AlertDialogDescription>
            </AlertDialogHeader>
            {confirm.children}
            <AlertDialogFooter>
              <AlertDialogCancel ref={cancelRef}>{t('common.cancel')}</AlertDialogCancel>
              <AlertDialogAction data-cmd-confirm={op} disabled={confirm.actionDisabled} onClick={() => {
                setOpen(false)
                cap.onOpenChange(false)
                onRun()
              }}>
                {confirm.action}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      ) : null}
    </>
  )
}
