// Command palette, Mod+K (M15-FR-100; AWR-14 §2.3, §6.10): groups jump, vehicles, drone commands, camera, simulation,
// layers, environment presets, panels and settings. Actions come from the shared registry (same guards, labels and
// hotkeys as menus and keys; refused entries stay listed, disabled, with their reason). Vehicles are searched by id
// prefix first, then substring, over the roster (the 20 best matches are listed, so N = 1000 filters well under a
// frame); choosing one selects it, opens the detail page and focuses the camera. Presets send env/preset.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { rtClient } from '@/net/rt'
import { camera } from '@/viewport/facade'
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from '@/ui/components/ui/command'
import { Icon } from '@/ui/icons/Icon'
import { listActions, runAction, type ActionGroup } from '@/ui/actions/registry'
import { runService } from '@/ui/actions/vehicleCommands'
import { comboLabel } from '@/ui/hotkeys/registry'
import { overlays, useOverlays } from '@/ui/shell/overlays'
import { writeDeniedKey } from '@/ui/shell/guards'
import { PRESETS } from '@/ui/panels/env/EnvPanel'
import { prefs } from '@/stores/prefs'
import { selection } from '@/stores/selection'
import { useModalFrameCap } from './useModalFrameCap'

const GROUPS: readonly ActionGroup[] = ['jump', 'drone', 'camera', 'sim', 'layer', 'panel', 'settings']
const MAX_VEHICLES = 20

/** roster ids matching a query: prefix matches first, then substring, at most `max` (pure; exported for tests) */
export function matchVehicles(ids: readonly string[], query: string, max = MAX_VEHICLES): string[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const pre: string[] = []
  const sub: string[] = []
  for (const id of ids) {
    const at = id.toLowerCase().indexOf(q)
    if (at === 0) pre.push(id)
    else if (at > 0 && sub.length < max) sub.push(id)
    if (pre.length >= max) break
  }
  return [...pre, ...sub].slice(0, max)
}

/**
 * the palette list (groups jump, vehicles, drone, camera, sim, layers, panels, settings, environment presets). The warm
 * stage under the boot mask renders it once as well (ADR-069), so the first Mod+K no longer pays the first mount of about
 * a hundred items (FX2-R3 trace: a 97 ms render task on the first open, D1-AC-25).
 */
export function PaletteList({ query, vehicles, denied, onClose }: { query: string; vehicles: readonly string[]; denied: string | null; onClose: () => void }) {
  const t = useT()
  return (
    <CommandList>
      <CommandEmpty>{t('palette.empty')}</CommandEmpty>
      {vehicles.length ? (
        <CommandGroup heading={t('palette.group.vehicles')}>
          {vehicles.map((id) => (
            <CommandItem key={id} value={`vehicle ${id} ${query}`} data-palette-vehicle={id} onSelect={() => {
              onClose()
              selection.select([id])
              prefs.setLayout({ right: { open: true, page: 'detail' } })
              camera.focus([id])
            }}>
              <Icon icon="drone.quad" />
              <span className="font-mono">{id}</span>
            </CommandItem>
          ))}
        </CommandGroup>
      ) : null}
      {GROUPS.map((g) => {
        const acts = listActions(g)
        if (!acts.length) return null
        return (
          <CommandGroup key={g} heading={t(`palette.group.${g}`)}>
            {acts.map((a) => {
              const ok = !a.when || a.when()
              const reason = ok ? null : a.disabledReasonKey?.() ?? null
              return (
                <CommandItem key={a.id} value={`${a.id} ${t(a.labelKey)} ${(a.keywords ?? []).join(' ')}`} disabled={!ok}
                  onSelect={() => {
                    onClose()
                    runAction(a.id)
                  }}>
                  {a.icon ? <Icon icon={a.icon} /> : null}
                  {t(a.labelKey)}
                  {reason ? <span className="text-muted-foreground">{` · ${t(reason)}`}</span> : null}
                  {a.hotkey ? <CommandShortcut>{comboLabel(a.hotkey).join(' ')}</CommandShortcut> : null}
                </CommandItem>
              )
            })}
          </CommandGroup>
        )
      })}
      <CommandGroup heading={t('palette.group.env')}>
        {PRESETS.map((p) => (
          <CommandItem key={p.id} value={`env preset ${p.id} ${t(`env.preset.${p.id}`)}`} disabled={denied !== null}
            onSelect={() => {
              onClose()
              runService('env:preset', 'env/preset', { name: p.id, duration_s: 30 }, t(`env.preset.${p.id}`), { toastSuccess: true })
            }}>
            <Icon icon={p.icon} />
            {t('palette.preset', { name: t(`env.preset.${p.id}`) })}
          </CommandItem>
        ))}
      </CommandGroup>
    </CommandList>
  )
}

export function CommandPalette() {
  const t = useT()
  const open = useOverlays((s) => s.palette)
  const cap = useModalFrameCap('modal:palette')
  const [query, setQuery] = React.useState('')
  const close = () => overlays.set('palette', false)
  const vehicles = React.useMemo(() => {
    if (!open) return []
    const rt = rtClient()
    return rt ? matchVehicles(rt.roster.entries().map((e) => e.id), query) : []
  }, [open, query])
  const denied = open ? writeDeniedKey() : null
  return (
    <CommandDialog open={open} title={t('palette.title')} description={t('palette.description')}
      onOpenChange={(o) => {
        cap.onOpenChange(o)
        overlays.set('palette', o)
        if (!o) setQuery('')
      }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      {/* cmdk needs its Command root inside the dialog (base-mira CommandDialog does not add one) */}
      <Command>
        <CommandInput autoFocus placeholder={t('palette.placeholder')} value={query} onValueChange={setQuery} />
        <PaletteList query={query} vehicles={vehicles} denied={denied} onClose={close} />
      </Command>
    </CommandDialog>
  )
}
