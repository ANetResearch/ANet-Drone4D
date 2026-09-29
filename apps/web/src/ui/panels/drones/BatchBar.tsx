// Batch operation bar (M15-FR-041; AWR-14 §4.4, §6.11 batch rules; D1-AC-27): with two or more vehicles selected, the
// bottom of the list shows "37 selected" and hover, return home, land and safety stop. Each sends ONE fleet/cmd/{op}
// call (vehicles "*" when the selection equals the whole unfiltered fleet, otherwise the id list); return home and land
// confirm first ("Return 37 vehicles?"). The buttons follow the batch tracker state; the aggregate toast shows the
// accepted, rejected, running and finished counts.
import { useT } from '@/app/i18n'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { CallButton } from '@/ui/actions/CallButton'
import { runBatch } from '@/ui/actions/vehicleCommands'
import { opText } from '@/ui/notify/severity'
import { useConnView } from '@/ui/shell/connView'
import { writeDeniedKey } from '@/ui/shell/guards'
import { useFleet } from '@/stores/fleet'
import { selectionStore, useSelection } from '@/stores/selection'
import type { IconKey } from '@/ui/icons/registry'
import { railFilterActive, railStore } from './railModel'

const OPS: readonly { op: 'hover' | 'rtl' | 'land' | 'safety_stop'; icon: IconKey; confirm: boolean }[] = [
  { op: 'hover', icon: 'cmd.hover', confirm: false },
  { op: 'rtl', icon: 'cmd.rth', confirm: true },
  { op: 'land', icon: 'cmd.land', confirm: true },
  { op: 'safety_stop', icon: 'mission.abort', confirm: false },
]

export function BatchBar() {
  const t = useT()
  const n = useSelection((s) => s.ids.length)
  const fleetN = useFleet((s) => s.n)
  useConnView((s) => s.version)
  const denied = writeDeniedKey()
  const send = (op: string) => {
    const ids = selectionStore.getState().ids
    const all = ids.length === fleetN && !railFilterActive(railStore.getState())
    runBatch(op, ids, all)
  }
  return (
    <div data-batch-bar="" className="flex items-center justify-between gap-2 rounded-md border bg-card px-2 py-1.5">
      <span className="text-hud-sub">{t('batch.selected', { n })}</span>
      <ButtonGroup>
        {OPS.map((o) => {
          const what = opText(o.op)
          return (
            <CallButton key={o.op} cmdKey={`fleet:${o.op}`} op={o.op} icon={o.icon} label={t('batch.label', { what, n })} size="icon-sm"
              disabledReason={denied ? t(denied) : null} onRun={() => send(o.op)}
              confirm={o.confirm ? { title: t('confirm.selection.title', { what, target: t('confirm.vehicles', { n }) }), body: t(`confirm.selection.${o.op}`, { alt: 2.5 }), action: t('confirm.selection.actionN', { what, n }) } : undefined} />
          )
        })}
      </ButtonGroup>
    </div>
  )
}
