// Shortcut help, ? (M15-FR-101; AWR-14 §2.3, §6.10; AWR-03 §8.5): Dialog with one Table per action group of the
// registered bindings (the same registry the dispatcher uses) and their key caps for the current platform (Mod = Ctrl or
// Cmd). The settings dialog's shortcut tab renders the same table.
// D2 (M15-FR-216; AWR-14 §20.10.2): the manual-control scope is listed as its own group (its bindings exist only while a
// vehicle is under manual control, so the rows come from the controller's static key table).
import { useT } from '@/app/i18n'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { ScrollArea } from '@/ui/components/ui/scroll-area'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/components/ui/table'
import { comboLabel, listHotkeys, type HotkeyBinding } from '@/ui/hotkeys/registry'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { MANUAL_KEYS } from '@/ui/manual/controller'
import { useModalFrameCap } from './useModalFrameCap'

const ORDER = ['panel', 'camera', 'drone', 'layer', 'sim', 'tool', 'settings', 'manual']

export function ShortcutTable() {
  const t = useT()
  const groups = new Map<string, HotkeyBinding[]>()
  for (const h of listHotkeys()) {
    const g = h.group ?? 'settings'
    if (g === 'manual') continue
    groups.set(g, [...(groups.get(g) ?? []), h])
  }
  groups.set('manual', MANUAL_KEYS.map((k) => ({ id: `manual.help.${k.combo}`, combo: k.combo, scope: 'manual', labelKey: k.labelKey, run: () => {} })))
  const keys = [...groups.keys()].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b))
  return (
    <div data-lf-table="">
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
    </div>
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
