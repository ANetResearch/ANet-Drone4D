// Global layers above the shell (M15 §6.3.1): dialogs (settings, about, shortcut help), the command palette, the label
// icon sprite and the boot mask controller with its pre-raster warm stage (ADR-069). Toasts render through the
// ToastProvider viewport.
import { IconSprite } from '@/ui/icons/IconSprite'
import { ConfirmHost } from '@/ui/actions/ConfirmHost'
import { LiveRegion } from '@/ui/shell/LiveRegion'
import { AboutDialog } from '@/ui/views/AboutDialog'
import { CommandPalette } from '@/ui/views/CommandPalette'
import { SettingsDialog } from '@/ui/views/SettingsDialog'
import { ShortcutHelp } from '@/ui/views/ShortcutHelp'
import { BootMask } from './boot/BootMask'
import { WarmStage } from './boot/WarmStage'

export function GlobalLayers() {
  return (
    <>
      <SettingsDialog />
      <AboutDialog />
      <ShortcutHelp />
      <CommandPalette />
      <ConfirmHost />
      <LiveRegion />
      <IconSprite />
      <WarmStage />
      <BootMask prewarm />
    </>
  )
}
