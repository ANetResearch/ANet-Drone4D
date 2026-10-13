// Field-view controls and legend (R-D2-41, R-D2-42; ADR-125; AWR-04 §15.11.3; AWR-14 §20.26.7; M15-FR-237; UX-FR-159).
// Owner: M15 (the data and the drawing are M19's stores/perception and M06's field layer; this file holds controls only).
//   * FieldControls: "field view: off | acoustic | visual | thermal | radar" (ToggleGroup, one choice), "volumes: off |
//     selected | all" and the "physical reference" Toggle; Shift+V cycles the view (swallowed while a vehicle is under
//     manual control); switching changes only instance data and uniforms (no program, M06). Viewers may use all of it (read-
//     only subscriptions). The choice persists in awr.ui.prefs.v1 (fieldView, fieldVolumes, fieldPhysicalRef).
//   * FieldLegend: the bottom-right card (240 px): view, unit, "free-space field"; the five grey swatches (--field-ramp-1..5)
//     with the band edges; the solid shell of the selected target ("heard by person-01") and the dashed reference shell
//     (background noise, 1 arcmin, 14 px); the point shapes of the modalities (no hue: circle acoustic g300, diamond visual
//     g50, ring radar g400, cross thermal g500); the reference sensor of the thermal and radar views; the footnote
//     "simulated reference values · free-space field".
// Placement: desktop at the bottom centre of the unobscured rect, tablet in the top bar (compact) and phone in the viewport
// window's "layers" drawer (ui/mobile).
// D2.2-W1: the legend reads the field of the realtime source's world; with no source for the view's world (static browsing)
// or a source that simulates another world it is not shown (stores/worldScope).
import * as React from 'react'
import { t, useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { notify } from '@/app/providers/ToastProvider'
import { FIELD_VIEWS, perceptionActions, perceptionStore, usePerception, type FieldLegend as Legend, type FieldView, type VolumesMode } from '@/stores/perception'
import { prefs, prefsStore } from '@/stores/prefs'
import { useTargets } from '@/stores/targets'
import { useWorldScope } from '@/stores/worldScope'
import { field as fieldApi } from '@/viewport/facade'
import { Card } from '@/ui/components/ui/card'
import { Toggle } from '@/ui/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { registerHotkey } from '@/ui/hotkeys/registry'
import { manualActive } from '@/ui/manual/controller'

export const FIELD_ICON: Readonly<Record<FieldView, IconKey>> = {
  none: 'field.off', acoustic: 'field.acoustic', visual: 'field.visual', thermal: 'field.thermal', radar: 'field.radar',
}
const VOLUMES: readonly VolumesMode[] = ['off', 'selected', 'all']

// ------------------------------------------------------------ prefs binding and the Shift+V hotkey (start-up)
let bound = false
/** apply the stored choice to the perception store and keep the prefs in step; register Shift+V (idempotent) */
export function installFieldPrefs(): () => void {
  if (bound) return () => {}
  bound = true
  const ui = prefsStore.getState().ui
  perceptionActions.setFieldView(ui.fieldView)
  perceptionActions.setVolumes(ui.fieldVolumes)
  perceptionActions.setPhysicalRef(ui.fieldPhysicalRef)
  const off = perceptionStore.subscribe((s, p) => {
    if (s.fieldView !== p.fieldView) prefs.setUi('fieldView', s.fieldView)
    if (s.volumesMode !== p.volumesMode) prefs.setUi('fieldVolumes', s.volumesMode)
    if (s.physicalRef !== p.physicalRef) prefs.setUi('fieldPhysicalRef', s.physicalRef)
  })
  const offKey = registerHotkey({
    id: 'field.cycle', combo: 'shift+KeyV', scope: 'global', labelKey: 'field.cycle', group: 'camera',
    // the manual scope swallows it (AWR-14 §20.26.7: Shift+V has no effect while flying by hand)
    when: () => !manualActive(), deniedKey: () => null,
    run: () => void perceptionActions.cycleFieldView(),
  })
  const offGov = fieldApi.onGovernorOff(() => notify('field:governor', 'info', t('field.governorOff')))
  return () => {
    bound = false
    off()
    offKey()
    offGov()
  }
}

/** the three controls; `compact` drops the text labels (tablet top bar), `stacked` lays them out in rows (phone drawer) */
export function FieldControls({ compact = false, stacked = false }: { compact?: boolean; stacked?: boolean }) {
  const tr = useT()
  const view = usePerception((s) => s.fieldView)
  const volumes = usePerception((s) => s.volumesMode)
  const physical = usePerception((s) => s.physicalRef)
  const group = (label: string, child: React.ReactNode) => (
    <div className={cn('flex items-center gap-1.5', stacked && 'flex-col items-start')}>
      <span className="text-hud-cap text-muted-foreground">{label}</span>
      {child}
    </div>
  )
  return (
    <div className={cn('flex items-center gap-3', stacked && 'flex-col items-start gap-3')} data-field-controls={view}>
      {group(tr('field.title'), (
        <ToggleGroup spacing={0} size="sm" value={[view]} aria-label={tr('field.title')} data-field-view={view}
          onValueChange={(v: unknown[]) => typeof v[0] === 'string' && perceptionActions.setFieldView(v[0] as FieldView)}>
          {FIELD_VIEWS.map((k) => (
            <ToggleGroupItem key={k} value={k} aria-label={tr(`field.${k === 'none' ? 'off' : k}`)} data-field-option={k}>
              <Icon icon={FIELD_ICON[k]} data-icon={compact ? undefined : 'inline-start'} />
              {compact ? null : tr(`field.${k === 'none' ? 'off' : k}`)}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      ))}
      {group(tr('field.volumes'), (
        <ToggleGroup spacing={0} size="sm" value={[volumes]} aria-label={tr('field.volumes')} data-field-volumes={volumes}
          onValueChange={(v: unknown[]) => typeof v[0] === 'string' && perceptionActions.setVolumes(v[0] as VolumesMode)}>
          {VOLUMES.map((k) => <ToggleGroupItem key={k} value={k} data-volumes-option={k}>{tr(`field.volumes.${k}`)}</ToggleGroupItem>)}
        </ToggleGroup>
      ))}
      <Toggle size="sm" variant="outline" pressed={physical} onPressedChange={(on: boolean) => perceptionActions.setPhysicalRef(on)} data-field-physical={physical ? 'on' : 'off'}
        aria-label={tr('field.physicalRef')}>
        {tr('field.physicalRef')}
      </Toggle>
    </div>
  )
}

/** the desktop placement: bottom centre of the unobscured rect */
export function FieldToolbar() {
  return (
    <div data-anchor="field-bar" className="rounded-lg bg-hud px-2 py-1 ring-1 ring-foreground/10" data-figure="field-bar">
      <FieldControls />
    </div>
  )
}

// ------------------------------------------------------------ legend
/** labels of the five bands from their lower edges: "< b1", "b1-b2", ..., ">= b4" */
export function bandLabels(bands: readonly number[]): string[] {
  const f = (v: number): string => (Number.isFinite(v) ? (Math.abs(v) >= 10 ? v.toFixed(0) : String(Math.round(v * 10) / 10)) : '—')
  const b = bands.slice(0, 5)
  if (b.length < 5) return []
  return [`< ${f(b[1])}`, `${f(b[1])}–${f(b[2])}`, `${f(b[2])}–${f(b[3])}`, `${f(b[3])}–${f(b[4])}`, `≥ ${f(b[4])}`]
}

/** point shape of a modality (CSS only, grey tokens; AWR-04 §15.11.2 item 2) */
export function ModalityMark({ kind }: { kind: 'acoustic' | 'visual' | 'radar' | 'thermal' }) {
  if (kind === 'acoustic') return <span aria-hidden="true" className="inline-block size-2 rounded-full bg-(--g300)" />
  if (kind === 'visual') return <span aria-hidden="true" className="inline-block size-2 rotate-45 bg-(--g50)" />
  if (kind === 'radar') return <span aria-hidden="true" className="inline-block size-2 rounded-full border border-(--g400)" />
  return (
    <span aria-hidden="true" className="relative inline-block size-2">
      <span className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-(--g500)" />
      <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-(--g500)" />
    </span>
  )
}

const RAMP = ['bg-(--field-ramp-1)', 'bg-(--field-ramp-2)', 'bg-(--field-ramp-3)', 'bg-(--field-ramp-4)', 'bg-(--field-ramp-5)'] as const

export function FieldLegend({ inline = false }: { inline?: boolean }) {
  const tr = useT()
  const view = usePerception((s) => s.fieldView)
  const legend = usePerception((s) => s.legend)
  const selectedTarget = useTargets((s) => s.selected)
  const away = useWorldScope((s) => s.bound && s.foreign)
  if (view === 'none' || away) return null
  const lg: Legend | null = legend && legend.kind === view ? legend : null
  const unit = lg ? tr(`field.unit.${lg.unit}`) : ''
  const labels = lg ? bandLabels(lg.bands) : []
  const target = selectedTarget
  const solidKey = view === 'acoustic' ? 'field.shell.heard' : view === 'visual' ? 'field.shell.seen' : view === 'thermal' ? 'field.shell.thermalD' : 'field.shell.radarD'
  return (
    <div data-anchor={inline ? undefined : 'field-legend'}>
      <Card size="sm" className="w-60 gap-1.5 rounded-xl bg-hud px-3 py-2 text-hud-sub" data-field-legend={view} data-field-unit={lg?.unit ?? ''}>
        <span className="flex items-center gap-1.5 font-medium">
          <Icon icon={FIELD_ICON[view]} />
          {tr(`field.kind.${view}`)}
          {unit ? <span className="text-muted-foreground">{` · ${unit}`}</span> : null}
        </span>
        <div className="flex gap-0.5" data-field-swatches="">
          {RAMP.map((c, i) => <span key={c} className={cn('h-2 flex-1', c)} data-band={i + 1} />)}
        </div>
        {labels.length ? (
          <div className="grid grid-cols-5 gap-0.5 text-center text-hud-cap tabular-nums text-muted-foreground" data-field-bands="">
            {labels.map((l) => <span key={l} className="truncate">{l}</span>)}
          </div>
        ) : <span className="text-hud-cap text-muted-foreground">{tr('field.waiting')}</span>}
        <span className="flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-0 w-5 border-t border-foreground" />
          {tr(solidKey, { target: target ?? tr(lg?.shellRef === 'D' ? 'field.shellRef.D' : 'field.shellRef.person') })}
        </span>
        {lg?.iso.map((s) => (
          <span key={s.id} className="flex items-center gap-1.5 text-muted-foreground" data-field-iso={s.id}>
            <span aria-hidden="true" className="inline-block h-0 w-5 border-t border-dashed border-muted-foreground" />
            {tr(`field.iso.${s.id}`, { v: Number.isFinite(s.v) ? (Math.round(s.v * 10) / 10).toString() : '—', unit })}
          </span>
        ))}
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-hud-cap text-muted-foreground" data-field-marks="">
          {(['acoustic', 'visual', 'radar', 'thermal'] as const).map((k) => (
            <span key={k} className="flex items-center gap-1"><ModalityMark kind={k} />{tr(`field.mark.${k}`)}</span>
          ))}
        </span>
        {lg?.ref.sensor ? <span className="text-hud-cap text-muted-foreground" data-field-ref="">{tr('field.refSensor', { sensor: lg.ref.sensor, lens: lg.ref.lens_mm ?? '' })}</span> : null}
        <span className="text-hud-cap text-muted-foreground">{tr('field.freeSpace')}</span>
      </Card>
    </div>
  )
}
