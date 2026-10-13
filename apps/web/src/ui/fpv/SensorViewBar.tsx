// FPV sensor view toolbar (R-D2-14; M15-FR-218; UX-FR-136; AWR-14 §20.10.5; d2-WP-11-to-M15 items 1 to 5, 7): shown in the
// FPV camera under the camera toolbar. The view switch FPV / day (EO) / night (NIR) / thermal (LWIR) drives M06's sensor
// views (viewport/facade sensorView, local display state: the viewer may use it freely, no write request, D2-AC-37);
// views the focus vehicle cannot show are disabled. The sensor Select lists the vehicle's imaging sensors (cameras and
// thermal cameras of stores/sensors); the thermal view adds the white-hot / black-hot palette (local; the operator also
// sends `sensor/mode {palette}`) and the display-only digital zoom; the night view shows the illuminator switch (operator,
// `sensor/mode {illum}`) with its range. The operator's focal length slider (log scale over the sensor's zoom range,
// `uav/{id}/cmd/zoom`, sent at most 5 per second) sits on the right edge. "Visual approximation" is marked in the top left
// badge column while a sensor view is drawn (FpvApproxBadge, AWR-14 §20.24 K13). Key B cycles the views
// (ui/actions/d2Actions.ts).
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { sensorsStore, useSensors, type PayloadSensorInfo } from '@/stores/sensors'
import { selectionStore, useSelection } from '@/stores/selection'
import { rtClient } from '@/net/rt'
import { useTargetList } from '@/ui/panels/targets/TargetsPanel'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Button } from '@/ui/components/ui/button'
import { sensorView, type SensorViewInfo, type SensorViewMode } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { Kbd } from '@/ui/components/ui/kbd'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Slider } from '@/ui/components/ui/slider'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'
import { useRt } from '@/ui/shell/RtContext'
import { useD2Write } from '@/ui/sandbox/writeGate'
import { useManual } from '@/ui/manual/controller'

export const VIEW_MODES: readonly SensorViewMode[] = ['fpv', 'eo', 'nir', 'lwir']
const ZOOM_KEY_MS = 250

/** the payload sensor the FPV view looks through, with the vehicle (keys - / = / I and the gimbal menu) */
function viewedPayload(): { uav: string; sensor: PayloadSensorInfo } | null {
  const uav = selectionStore.getState().primary
  const st = sensorsStore.getState()
  if (!uav || st.vehicleId !== uav || !st.payload) return null
  const name = sensorView.info().sensorName
  const s = st.payload.sensors.find((x) => x.sensor === name) ?? st.payload.sensors.find((x) => x.kind === 'eo' || x.kind === 'nir') ?? null
  return s ? { uav, sensor: s } : null
}

let lastZoomKey = 0
/** keys - and = (AWR-14 §20.10.2): one step = x0.8 / x1.25 of the focal length, merged to <= 4 commands per second */
export function fpvZoomStep(factor: number): void {
  const v = viewedPayload()
  const rt = rtClient()
  if (!v || !rt || v.sensor.kind === 'lwir' || v.sensor.fMm === null) return
  const now = performance.now()
  if (now - lastZoomKey < ZOOM_KEY_MS) return
  lastZoomKey = now
  const f = Math.min(48, Math.max(4.8, v.sensor.fMm * factor))
  void rt.call(`uav/${v.uav}/cmd/zoom`, { sensor: v.sensor.sensor, f_mm: Number(f.toFixed(1)) })
}

/** key I (night view): the built-in illuminator on or off */
export function fpvToggleIllum(): void {
  const v = viewedPayload()
  const rt = rtClient()
  if (!v || !rt || sensorView.mode !== 'nir') return
  void rt.call(`uav/${v.uav}/cmd/sensor/mode`, { sensor: v.sensor.sensor, illum: { builtin: v.sensor.illumOn ? 'off' : 'on' } })
}
const VIEW_ICON: Readonly<Record<SensorViewMode, IconKey>> = { fpv: 'cam.fpv', eo: 'sensor.camera', nir: 'sensor.nir', lwir: 'sensor.thermal' }
export const ZOOM_SEND_MS = 200

/** the facade's sensor view state, re-read on every change (local display state, a few changes per second at most) */
export function useSensorViewInfo(): SensorViewInfo & { active: boolean } {
  const read = React.useCallback(() => ({ ...sensorView.info(), active: sensorView.active }), [])
  const [s, setS] = React.useState(read)
  React.useEffect(() => sensorView.onChange(() => setS(read())), [read])
  return s
}

/** focal length <-> log slider position (0..1000) over [fMin, fMax] */
export const fToPos = (f: number, lo: number, hi: number): number => (hi > lo ? Math.round((1000 * Math.log(Math.min(hi, Math.max(lo, f)) / lo)) / Math.log(hi / lo)) : 0)
export const posToF = (p: number, lo: number, hi: number): number => (hi > lo ? lo * (hi / lo) ** (Math.min(1000, Math.max(0, p)) / 1000) : lo)

/** the operator's zoom slider for the payload sensor of the view (right edge of the FPV view) */
function ZoomRail({ uav, sensor, fMm, lo, hi }: { uav: string; sensor: string; fMm: number; lo: number; hi: number }) {
  const t = useT()
  const rt = useRt()
  const [pos, setPos] = React.useState<number | null>(null)
  const lastSend = React.useRef(0)
  const send = (p: number, force: boolean) => {
    const now = performance.now()
    if (!force && now - lastSend.current < ZOOM_SEND_MS) return
    lastSend.current = now
    void rt.call(`uav/${uav}/cmd/zoom`, { sensor, f_mm: Number(posToF(p, lo, hi).toFixed(1)) }).result.then((r) => {
      if (r.status === 'rejected' || r.status === 'failed') notify(`fpv:zoom:${uav}`, 'warning', `${r.code} ${reasonText(r.code).short}`)
    })
  }
  const shown = pos ?? fToPos(fMm, lo, hi)
  return (
    <div data-anchor="fpv-zoom" className="flex flex-col items-center gap-1.5 rounded-lg bg-hud px-1.5 py-2 ring-1 ring-foreground/10" data-fpv-zoom={sensor}>
      <Icon icon="cam.zoomin" label={t('sensor.zoom')} />
      <Slider orientation="vertical" className="h-40" min={0} max={1000} step={1} value={[shown]} aria-label={t('sensor.zoom')}
        onValueChange={(v) => {
          const p = Array.isArray(v) ? (v as number[])[0] : (v as number)
          setPos(p)
          send(p, false)
        }}
        onValueCommitted={(v) => {
          const p = Array.isArray(v) ? (v as number[])[0] : (v as number)
          setPos(null)
          send(p, true)
        }} />
      <Icon icon="cam.zoomout" />
      <span className="font-mono text-hud-cap" data-fpv-f={fmt.num(posToF(shown, lo, hi), 1)}>{`${fmt.num(posToF(shown, lo, hi), 1)}`}</span>
    </div>
  )
}

/** the operator's gimbal menu (AWR-14 §20.10.5 "跟踪"): track a target, or back to centre */
function GimbalMenu({ uav }: { uav: string }) {
  const t = useT()
  const rt = useRt()
  const targets = useTargetList()
  const send = (args: Record<string, unknown>) => {
    void rt.call(`uav/${uav}/cmd/gimbal`, args).result.then((r) => {
      if (r.status === 'rejected' || r.status === 'failed') notify(`fpv:gimbal:${uav}`, 'warning', `${r.code} ${reasonText(r.code).short}`)
    })
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="xs" variant="ghost" className="h-7" data-fpv-gimbal="" />}>
        <Icon icon="cmd.track" data-icon="inline-start" />{t('fpv.gimbal')}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t('fpv.track')}</DropdownMenuLabel>
          {targets.length === 0 ? <DropdownMenuItem disabled>{t('fpv.noTargets')}</DropdownMenuItem> : null}
          {targets.slice(0, 12).map((x) => (
            <DropdownMenuItem key={x.id} onClick={() => send({ mode: 'track', target: x.id })} data-fpv-track={x.id}>{x.label}</DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => send({ mode: 'angles', pitch_deg: -45, yaw_deg: 0 })}>{t('sensor.center')}</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function SensorViewBar() {
  const t = useT()
  const rt = useRt()
  const info = useSensorViewInfo()
  const primary = useSelection((s) => s.primary)
  const sensors = useSensors((s) => s.sensors)
  const payload = useSensors((s) => s.payload)
  const vehicle = useSensors((s) => s.vehicleId)
  const canEdit = useD2Write()
  const manual = useManual((s) => s.state === 'ACTIVE' || s.state === 'STOPPING')
  const imaging = sensors.filter((s) => s.kind === 'camera' || s.kind === 'thermal')
  const current = imaging.find((s) => s.sensorNo === info.sensorNo) ?? null
  const pay = payload && vehicle === primary ? payload.sensors.find((s) => s.sensor === (info.sensorName ?? current?.name)) ?? null : null
  const set = (m: SensorViewMode) => {
    const r = sensorView.set(m)
    if (!r.ok) notify('fpv:view', 'info', t(`fpv.reason.${r.reason ?? 'not_fpv'}`))
  }
  const sendMode = (args: Record<string, unknown>) => {
    if (!primary) return
    void rt.call(`uav/${primary}/cmd/sensor/mode`, args).result.then((r) => {
      if (r.status === 'rejected' || r.status === 'failed') notify(`fpv:mode:${primary}`, 'warning', `${r.code} ${reasonText(r.code).short}`)
    })
  }
  const zoomable = canEdit && pay && pay.kind !== 'lwir' && primary
  return (
    <>
      <div data-anchor="fpv-bar" data-below-manual={manual ? '' : undefined} className="flex flex-wrap items-center justify-center gap-1 rounded-lg bg-hud p-1 ring-1 ring-foreground/10"
        data-sensor-view-bar={info.mode}>
        <ToggleGroup spacing={0} size="sm" value={[info.mode]} aria-label={t('fpv.views')}
          onValueChange={(v: unknown[]) => VIEW_MODES.includes(v[0] as SensorViewMode) && set(v[0] as SensorViewMode)}>
          {VIEW_MODES.map((m) => (
            <Tooltip key={m}>
              <TooltipTrigger render={<ToggleGroupItem value={m} aria-label={t(`fpv.view.${m}`)} disabled={!info.available[m]} data-sensor-view={m} />}>
                <Icon icon={VIEW_ICON[m]} data-icon="inline-start" />
                {t(`fpv.view.${m}`)}
              </TooltipTrigger>
              <TooltipContent>{info.available[m] ? t(`fpv.viewHint.${m}`) : t(`fpv.unavailable.${m}`)}<Kbd>B</Kbd></TooltipContent>
            </Tooltip>
          ))}
        </ToggleGroup>
        {imaging.length > 1 ? (
          <Select items={imaging.map((s) => ({ value: String(s.sensorNo), label: s.name }))} value={info.sensorNo !== null ? String(info.sensorNo) : ''}
            onValueChange={(v) => typeof v === 'string' && v !== '' && sensorView.selectSensor(Number(v))}>
            <SelectTrigger size="sm" className="h-7 w-40" aria-label={t('fpv.sensor')} data-fpv-sensor=""><SelectValue placeholder={t('fpv.sensor')} /></SelectTrigger>
            <SelectContent>
              {imaging.map((s) => <SelectItem key={s.sensorNo} value={String(s.sensorNo)}>{`${s.name} · ${t(s.kind === 'thermal' ? 'fpv.view.lwir' : 'fpv.camera')}`}</SelectItem>)}
            </SelectContent>
          </Select>
        ) : null}
        {info.mode === 'lwir' ? (
          <>
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[info.palette]} aria-label={t('sensor.palette')} data-fpv-palette={info.palette}
              onValueChange={(v: unknown[]) => {
                if (v[0] !== 'white_hot' && v[0] !== 'black_hot') return
                sensorView.setPalette(v[0])
                if (canEdit && pay) sendMode({ sensor: pay.sensor, palette: v[0] })
              }}>
              <ToggleGroupItem value="white_hot">{t('sensor.whiteHot')}</ToggleGroupItem>
              <ToggleGroupItem value="black_hot">{t('sensor.blackHot')}</ToggleGroupItem>
            </ToggleGroup>
            <ToggleGroup spacing={0} size="sm" variant="outline" value={[String(info.digitalZoom)]} aria-label={t('fpv.digitalZoom')}
              onValueChange={(v: unknown[]) => v[0] && sensorView.setDigitalZoom(Number(v[0]))}>
              {[1, 2, 4].map((z) => <ToggleGroupItem key={z} value={String(z)}>{`×${z}`}</ToggleGroupItem>)}
            </ToggleGroup>
          </>
        ) : null}
        {canEdit && pay && primary ? <GimbalMenu uav={primary} /> : null}
        {info.mode === 'nir' && pay ? (
          <span className="flex items-center gap-1.5 px-1 text-hud-sub" data-fpv-illum={pay.illumOn ? 'on' : 'off'}>
            <Switch size="sm" checked={pay.illumOn === true} disabled={!canEdit} aria-label={t('sensor.illum')}
              onCheckedChange={(on: boolean) => sendMode({ sensor: pay.sensor, illum: { builtin: on ? 'on' : 'off' } })} />
            {t('sensor.illum')}
            {pay.illumRangeM !== null ? <span className="text-muted-foreground">{t('sensor.illumRange', { m: fmt.num(pay.illumRangeM, 0) })}</span> : null}
          </span>
        ) : null}
      </div>
      {zoomable && pay && pay.fMm !== null ? <ZoomRail uav={primary} sensor={pay.sensor} fMm={pay.fMm} lo={4.8} hi={48} /> : null}
    </>
  )
}

/** "visual approximation" mark of a drawn sensor view (top-left badge column of the viewport overlay) */
export function FpvApproxBadge() {
  const t = useT()
  const info = useSensorViewInfo()
  if (!info.active) return null
  return <Badge variant="outline" className="bg-hud" data-fpv-approx={info.mode}>{t('fpv.approx')}</Badge>
}
