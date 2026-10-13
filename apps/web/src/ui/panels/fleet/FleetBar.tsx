// Fleet list header of D2 (R-D2-13, R-D2-20; M15-FR-211; UX-FR-117; AWR-14 §20.6.1, §20.6.4, §20.6.6): "vehicles n / 12",
// "add" for the sandbox operator (Dialog: model from the catalogue and the session's clones with endurance and link
// range, placement "in a nest" (only compatible nests: box multirotor, launcher fixed wing, vtol_pad VTOL and
// multirotor) or "on the map" (VEHICLE_PLACE), label and initial charge; POST /sessions/{sid}/vehicles, the new vehicle
// shows within the next roster update; 505, 525, 102 and 520 explain themselves) and "model catalogue" for everyone
// (the Sheet; the shared show browses it read-only). Models from M21's stores/catalog.ts (presets and the session's
// clones), nests from M20's stores/tasking.ts; the vehicle itself is created through the sandbox REST (SB-31).
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { ApiError } from '@/net/api'
import { catalog, useCatalog, type ModelSummary } from '@/stores/catalog'
import { useFleet } from '@/stores/fleet'
import { refreshNests } from '@/stores/tasking'
import { Button } from '@/ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/ui/components/ui/dialog'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { RadioGroup, RadioGroupItem } from '@/ui/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Spinner } from '@/ui/components/ui/spinner'
import { Icon } from '@/ui/icons/Icon'
import { useSurface } from '@/app/shell/surface'
import { NumField, TextField } from '@/ui/sandbox/fields'
import { sbSession } from '@/ui/sandbox/domain'
import { useSandbox } from '@/ui/sandbox/session'
import { d2Tools } from '@/ui/tools/d2Tools'
import { useNests } from '@/ui/panels/nests/NestsPanel'
import { labelOk } from '@/ui/panels/targets/targetModel'
import { useModalFrameCap } from '@/ui/views/useModalFrameCap'
import { CatalogSheet } from './CatalogSheet'
import { fleetDialogs, useFleetDialogs } from './fleetDialogs'
import { nestAccepts } from './fleetModel'

const errText = (e: unknown): string => (e instanceof ApiError && e.reason ? reasonText(e.reason).short : String(e))

function AddVehicleDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const t = useT()
  const sid = useSandbox((s) => s.sid)
  const models = useCatalog((s) => s.models)
  const mine = useCatalog((s) => s.sessionModels)
  React.useEffect(() => {
    if (!open) return
    void catalog.load()
    if (sid) void catalog.loadSession(sid)
  }, [open, sid])
  const nests = useNests()
  const [model, setModel] = React.useState<string | null>(null)
  const [where, setWhere] = React.useState<'nest' | 'map'>('nest')
  const [nest, setNest] = React.useState<string | null>(null)
  const [label, setLabel] = React.useState('')
  const [soc, setSoc] = React.useState(100)
  const [busy, setBusy] = React.useState(false)
  const cap = useModalFrameCap('modal:add-vehicle')
  const all: ModelSummary[] = [...models, ...mine]
  const m = all.find((x) => x.model_id === model) ?? null
  const nestItems = nests.map((n) => ({ n, ok: m ? nestAccepts(n.kind ?? 'box', m.category) : true }))
  const add = async (body: { nest_id?: string | null; home_enu_m?: (number | null)[] | null }) => {
    if (!model) return
    setBusy(true)
    try {
      await sbSession.addVehicle({ model_id: model, label: label.trim() || null, initial_soc: soc / 100, ...body })
      void refreshNests()
      onOpenChange(false)
    } catch (e) {
      notify('fleet:add', 'warning', errText(e))
    } finally {
      setBusy(false)
    }
  }
  const go = () => {
    if (where === 'nest') {
      void add({ nest_id: nest })
      return
    }
    onOpenChange(false)
    d2Tools.start('VEHICLE_PLACE', model, ([p]) => void add({ home_enu_m: [p[0], p[1], null] }))
  }
  return (
    <Dialog open={open} onOpenChange={(o) => {
      cap.onOpenChange(o)
      onOpenChange(o)
    }} onOpenChangeComplete={cap.onOpenChangeComplete}>
      <DialogContent className="sm:max-w-md" data-add-vehicle="">
        <DialogHeader>
          <DialogTitle>{t('fleet.add.title')}</DialogTitle>
          <DialogDescription>{t('fleet.add.desc')}</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel>{t('fleet.add.model')}</FieldLabel>
          <Select items={all.map((x) => ({ value: x.model_id, label: x.name_zh ?? x.model_id }))} value={model ?? ''} onValueChange={(v) => typeof v === 'string' && setModel(v)}>
            <SelectTrigger className="w-full" data-add-model=""><SelectValue placeholder={t('fleet.add.model')} /></SelectTrigger>
            <SelectContent>
              {all.map((x) => (
                <SelectItem key={x.model_id} value={x.model_id}>
                  {`${x.name_zh ? sanitizeText(x.name_zh, 24) : x.model_id} · ${fmt.num(((x.category === 'multirotor' ? x.endurance?.hover_s : x.endurance?.cruise_s) ?? 0) / 60, 0)} min · ${fmt.num((x.link_range_m ?? 0) / 1000, 1)} km`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <RadioGroup value={where} onValueChange={(v) => (v === 'nest' || v === 'map') && setWhere(v)} aria-label={t('fleet.add.where')}>
          <Field orientation="horizontal"><RadioGroupItem value="nest" id="add-nest" /><FieldLabel htmlFor="add-nest" className="font-normal">{t('fleet.add.inNest')}</FieldLabel></Field>
          <Field orientation="horizontal"><RadioGroupItem value="map" id="add-map" /><FieldLabel htmlFor="add-map" className="font-normal">{t('fleet.add.onMap')}</FieldLabel></Field>
        </RadioGroup>
        {where === 'nest' ? (
          <Field>
            <FieldLabel>{t('fleet.add.nest')}</FieldLabel>
            <Select items={nestItems.map(({ n }) => ({ value: n.id ?? '', label: n.name ?? n.id ?? '' }))} value={nest ?? ''} onValueChange={(v) => typeof v === 'string' && setNest(v)}>
              <SelectTrigger className="w-full" data-add-nest=""><SelectValue placeholder={t('fleet.add.nest')} /></SelectTrigger>
              <SelectContent>
                {nestItems.map(({ n, ok }) => <SelectItem key={n.id} value={n.id ?? ''} disabled={!ok}>{`${sanitizeText(n.name ?? n.id ?? '', 24)} · ${t(`nest.kind.${n.kind ?? 'box'}`)}${ok ? '' : ` · ${t('fleet.add.incompatible')}`}`}</SelectItem>)}
              </SelectContent>
            </Select>
            {nestItems.length === 0 ? <FieldDescription>{t('fleet.add.noNest')}</FieldDescription> : null}
          </Field>
        ) : <FieldDescription>{t('fleet.add.mapHint')}</FieldDescription>}
        <div className="grid grid-cols-2 gap-2">
          <TextField id="add-label" label={t('fleet.add.label')} value={label} validate={(s) => s.trim() === '' || labelOk(s)} onCommit={async (v) => {
            setLabel(v)
            return true
          }} />
          <NumField id="add-soc" label={t('fleet.add.soc')} value={soc} unit="%" range={{ min: 10, max: 100 }} digits={0} onCommit={async (v) => {
            setSoc(v)
            return true
          }} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button disabled={!model || busy || (where === 'nest' && !nest)} onClick={go} data-add-go="">{busy ? <Spinner data-icon="inline-start" /> : null}{t('fleet.add.go')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function FleetBar() {
  const t = useT()
  const surface = useSurface((s) => s.surface)
  const operator = surface === 'sandbox'
  const n = useFleet((s) => s.n)
  const max = useSandbox((s) => s.session?.limits.vehicles_max ?? 12)
  const catalog = useFleetDialogs((s) => s.catalog)
  const add = useFleetDialogs((s) => s.add)
  const setCatalog = (o: boolean) => fleetDialogs.setCatalog(o)
  const setAdd = (o: boolean) => fleetDialogs.setAdd(o)
  if (surface === 'local') return null
  return (
    <div className="flex items-center gap-1.5" data-fleet-bar="">
      {operator ? <span className="text-hud-cap font-semibold uppercase text-muted-foreground" data-fleet-count={n}>{t('fleet.count', { n, max })}</span> : null}
      <Button size="xs" variant="ghost" className="ml-auto" onClick={() => setCatalog(true)} data-fleet-catalog="">
        <Icon icon="drone.quad" data-icon="inline-start" />{t('fleet.catalog')}
      </Button>
      {operator ? <Button size="xs" variant="outline" disabled={n >= max} onClick={() => setAdd(true)} data-fleet-add=""><Icon icon="plus" data-icon="inline-start" />{t('fleet.add')}</Button> : null}
      <CatalogSheet open={catalog} onOpenChange={setCatalog} />
      {operator ? <AddVehicleDialog open={add} onOpenChange={setAdd} /> : null}
    </div>
  )
}
