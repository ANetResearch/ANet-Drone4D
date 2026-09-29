// Shortcut help, ? (M15-FR-101; AWR-14 §2.3, §6.10; AWR-03 §8.5): Dialog with one Table per action group of the
// registered bindings (the same registry the dispatcher uses) and their key caps for the current platform (Mod = Ctrl or
// Cmd). The settings dialog's shortcut tab renders the same table.
import { useT } from '@/app/i18n'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { ScrollArea } from '@/ui/components/ui/scroll-area'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/components/ui/table'
import { comboLabel, listHotkeys, type HotkeyBinding } from '@/ui/hotkeys/registry'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { useModalFrameCap } from './useModalFrameCap'

const ORDER = ['panel', 'camera', 'drone', 'layer', 'sim', 'settings']

export function ShortcutTable() {
  const t = useT()
  const groups = new Map<string, HotkeyBinding[]>()
  for (const h of listHotkeys()) {
    const g = h.group ?? 'settings'
    groups.set(g, [...(groups.get(g) ?? []), h])
  }
  const keys = [...groups.keys()].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b))
  return (
    <Table data-shortcut-table="">
      <TableHeader><TableRow><TableHead>{t('settings.shortcut.action')}</TableHead><TableHead>{t('settings.shortcut.keys')}</TableHead></TableRow></TableHeader>
      <TableBody>
        {keys.map((g) => [
          <TableRow key={`g-${g}`}><TableCell colSpan={2} className="pt-3 text-hud-cap font-semibold uppercase text-muted-foreground">{t(`palette.group.${g}`)}</TableCell></TableRow>,
          ...(groups.get(g) ?? []).map((h) => (
            <TableRow key={h.id} data-hotkey={h.id}>
              <TableCell>{t(h.labelKey)}</TableCell>
              <TableCell><KbdGroup>{comboLabel(h.combo).map((k) => <Kbd key={k}>{k}</Kbd>)}</KbdGroup></TableCell>
            </TableRow>
          )),
        ])}
      </TableBody>
    </Table>
  )
}

export function ShortcutHelp() {
  const t = useT()
  const open = useOverlays((s) => s.help)
  const cap = useModalFrameCap('modal:help')
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      overlays.set('help', o)
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>{t('help.title')}</DialogTitle></DialogHeader>
        <ScrollArea className="max-h-[70vh]"><ShortcutTable /></ScrollArea>
      </DialogContent>
    </Dialog>
  )
}
