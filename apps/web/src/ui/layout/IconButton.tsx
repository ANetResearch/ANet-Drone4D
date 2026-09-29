// Icon-only button with a Tooltip whose text is the same string as its aria-label (M15-FR-112; AWR-14 §4.6, §12). A
// disabled button stays focusable and hoverable (focusableWhenDisabled, aria-disabled) so the Tooltip can say why it is
// disabled; callers pass the reason as the label in that case.
import type * as React from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/components/ui/button'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { comboLabel } from '@/ui/hotkeys/registry'

export interface IconButtonProps {
  icon: IconKey
  label: string
  hotkey?: string
  onClick?: () => void
  pressed?: boolean
  disabled?: boolean
  size?: 'icon-sm' | 'icon' | 'icon-xs'
  variant?: 'ghost' | 'outline' | 'secondary'
  children?: React.ReactNode
  className?: string
}

export function IconButton({ icon, label, hotkey, onClick, pressed, disabled, size = 'icon-sm', variant = 'ghost', children, className }: IconButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Button size={size} variant={variant} aria-label={label} aria-pressed={pressed} disabled={disabled} focusableWhenDisabled
          onClick={onClick} className={cn(disabled && 'aria-disabled:opacity-50', className)} />}
      >
        {children ?? <Icon icon={icon} />}
      </TooltipTrigger>
      <TooltipContent>
        {label}
        {hotkey ? (
          <KbdGroup>
            {comboLabel(hotkey).map((k) => <Kbd key={k}>{k}</Kbd>)}
          </KbdGroup>
        ) : null}
      </TooltipContent>
    </Tooltip>
  )
}
