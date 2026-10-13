// Sensor mounts and parameters of one vehicle (R-D2-14; M15-FR-211 item 2, UX-FR-118; AWR-14 §20.6.5; d2-WP-10 item 1):
// the payload block of M13 (`stores/sensors.ts` payload, <= 4 Hz: gimbal source and mode, angles, per sensor kind, active,
// focal length and FOV, imaging mode, analysis resolution, palette, illuminator) as a lieflat table, and for the sandbox
// operator the controls of each sensor through the vehicle commands: `zoom` (focal length, log scale 4.8-48 mm, sent on
// release, <= 5 Hz), `sensor/mode` (active, imaging mode, illuminator, analysis resolution 1080p or 4K, LWIR palette white
// or black hot) and `gimbal` (back to centre). Mount changes need the vehicle landed and locked (PATCH vehicles, 522 when
// the mounted mass exceeds the MTOW). The instant GSD hint (GSD at 100 m at the current focal length) is the M19
// calculator's (TS port, gsdAtRange over the catalogue sensor, the reference GX40 when the catalogue lacks it).
import * as React from 'react'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { useRt } from '@/ui/shell/RtContext'
import { useSurface } from '@/app/shell/surface'
import { useSensors, type PayloadSensorInfo } from '@/stores/sensors'
import { useCatalog } from '@/stores/catalog'
import { gsdAtRange, gx40, sensorFromCatalog } from '@/app/shell/engineD2'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Slider } from '@/ui/components/ui/slider'
import { Switch } from '@/ui/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Icon } from '@/ui/icons/Icon'
import type { IconKey } from '@/ui/icons/registry'

export const ZOOM_MM = { min: 4.8, max: 48 } as const
/** focal length <-> slider position on a log scale (0..1000) */
export const zoomToPos = (f: number): number => Math.round((1000 * Math.log(Math.min(ZOOM_MM.max, Math.max(ZOOM_MM.min, f)) / ZOOM_MM.min)) / Math.log(ZOOM_MM.max / ZOOM_MM.min))
export const posToZoom = (p: number): number => ZOOM_MM.min * (ZOOM_MM.max / ZOOM_MM.min) ** (Math.min(1000, Math.max(0, p)) / 1000)

const KIND_ICON: Readonly<Record<string, IconKey>> = {
  eo: 'sensor.camera', nir: 'sensor.nir', lwir: 'sensor.thermal', radar: 'sensor.radar', acoustic: 'sensor.acoustic', lidar: 'sensor.lidar',
}

function SensorRow({ id, s, operator }: { id: string; s: PayloadSensorInfo; operator: boolean }) {
  const t = useT()
  const rt = useRt()
  const [zoomPos, setZoomPos] = React.useState<number | null>(null)
  const send = (service: string, args: Record<string, unknown>) => {
    const h = rt.call(`uav/${id}/cmd/${service}`, args)
    void h.result.then((r) => {
      if (r.status === 'rejected' || r.status === 'failed') notify(`sensor:${id}:${s.sensor}`, 'warning', `${r.code} ${reasonText(r.code).short}`)
    })
  }
  const imaging = s.kind === 'eo' || s.kind === 'nir' || s.kind === 'lwir'
  const item = useCatalog((c) => c.sensors.find((x) => x.sensor_type === s.sensor) ?? null)
  const fNow = zoomPos !== null ? posToZoom(zoomPos) : s.fMm
  const gsd = React.useMemo(() => {
    if (!imaging || fNow === null) return null
    const sn = (item ? sensorFromCatalog(item, { resolution: s.resolution ?? undefined }) : null) ?? (s.kind === 'lwir' ? null : gx40(s.resolution ?? '1080p'))
    return sn ? gsdAtRange(sn, fNow, 100, s.resolution) : null
  }, [imaging, fNow, item, s.kind, s.resolution])
  return (
    <div className="flex flex-col gap-1.5 rounded-md p-1.5 ring-1 ring-foreground/10" data-sensor-row={s.sensor}>
      <span className="flex items-center gap-1.5">
        <Icon icon={KIND_ICON[s.kind] ?? 'sensor.camera'} label={t(`sensor.d2kind.${s.kind}`)} />
        <span className="font-mono text-xs">{s.sensor}</span>
        <Badge variant="outline">{t(`sensor.d2kind.${s.kind}`)}</Badge>
        <Switch className="ml-auto" checked={s.active} disabled={!operator} aria-label={t('sensor.active')} data-sensor-active={s.sensor}
          onCheckedChange={(on: boolean) => send('sensor/mode', { sensor: s.sensor, active: on })} />
      </span>
      {imaging ? (
        <span className="text-hud-sub text-muted-foreground" data-sensor-optics="">
          {`f ${fmt.num(s.fMm ?? Number.NaN, 1)} mm · HFOV ${fmt.num(s.hfovDeg ?? Number.NaN, 1)}°${s.mode ? ` · ${t(`sensor.mode.${s.mode}`)}` : ''}`}
          {gsd !== null ? ` · ${t('sensor.gsd100', { cm: fmt.num(gsd * 100, 2) })}` : ''}
        </span>
      ) : null}
      {operator && imaging && s.kind !== 'lwir' ? (
        <span className="flex items-center gap-2">
          <span className="w-12 text-hud-sub text-muted-foreground">{t('sensor.zoom')}</span>
          <Slider className="flex-1" min={0} max={1000} step={1} value={[zoomPos ?? zoomToPos(s.fMm ?? ZOOM_MM.min)]} aria-label={t('sensor.zoom')} data-sensor-zoom={s.sensor}
            onValueChange={(v) => setZoomPos(Array.isArray(v) ? v[0] : v)}
            onValueCommitted={(v) => {
              const p = Array.isArray(v) ? v[0] : v
              setZoomPos(null)
              send('zoom', { sensor: s.sensor, f_mm: Number(posToZoom(p).toFixed(1)) })
            }} />
          <span className="w-14 text-right font-mono text-hud-sub">{`${fmt.num(posToZoom(zoomPos ?? zoomToPos(s.fMm ?? ZOOM_MM.min)), 1)} mm`}</span>
        </span>
      ) : null}
      {operator && s.kind === 'eo' ? (
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[s.resolution ?? '1080p']} aria-label={t('sensor.resolution')}
          onValueChange={(v: unknown[]) => (v[0] === '1080p' || v[0] === '4k') && send('sensor/mode', { sensor: s.sensor, resolution: v[0] })}>
          <ToggleGroupItem value="1080p">1080P</ToggleGroupItem>
          <ToggleGroupItem value="4k">4K</ToggleGroupItem>
        </ToggleGroup>
      ) : null}
      {operator && s.kind === 'nir' ? (
        <span className="flex items-center gap-2 text-hud-sub">
          <span>{t('sensor.illum')}</span>
          <Switch checked={s.illumOn === true} aria-label={t('sensor.illum')} onCheckedChange={(on: boolean) => send('sensor/mode', { sensor: s.sensor, illum: { builtin: on ? 'on' : 'off' } })} />
          {s.illumRangeM !== null ? <span className="text-muted-foreground">{t('sensor.illumRange', { m: fmt.num(s.illumRangeM, 0) })}</span> : null}
        </span>
      ) : null}
      {s.kind === 'lwir' ? (
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[s.palette ?? 'white_hot']} aria-label={t('sensor.palette')} disabled={!operator}
          onValueChange={(v: unknown[]) => (v[0] === 'white_hot' || v[0] === 'black_hot') && send('sensor/mode', { sensor: s.sensor, palette: v[0] })}>
          <ToggleGroupItem value="white_hot">{t('sensor.whiteHot')}</ToggleGroupItem>
          <ToggleGroupItem value="black_hot">{t('sensor.blackHot')}</ToggleGroupItem>
        </ToggleGroup>
      ) : null}
    </div>
  )
}

export function PayloadControls({ id }: { id: string }) {
  const t = useT()
  const rt = useRt()
  const operator = useSurface((s) => s.surface === 'sandbox')
  const vehicleId = useSensors((s) => s.vehicleId)
  const payload = useSensors((s) => s.payload)
  if (vehicleId !== id || !payload) return null
  return (
    <div className="flex flex-col gap-2" data-payload={id}>
      <span className="flex items-center gap-2 text-hud-sub">
        <span className="text-hud-cap font-semibold uppercase text-muted-foreground">{t('sensor.payload')}</span>
        <span className="font-mono text-muted-foreground">{`${t('sensor.gimbal')} ${fmt.num(payload.pitchDeg ?? Number.NaN, 0)}° / ${fmt.num(payload.yawDeg ?? Number.NaN, 0)}°`}</span>
        {operator ? <Button size="xs" variant="ghost" className="ml-auto" onClick={() => void rt.call(`uav/${id}/cmd/gimbal`, { mode: 'angles', pitch_deg: -45, yaw_deg: 0 })}>{t('sensor.center')}</Button> : null}
      </span>
      {payload.sensors.map((s) => <SensorRow key={s.sensor} id={id} s={s} operator={operator} />)}
      <p className="text-hud-cap text-muted-foreground">{t('sensor.mountHint')}</p>
    </div>
  )
}
