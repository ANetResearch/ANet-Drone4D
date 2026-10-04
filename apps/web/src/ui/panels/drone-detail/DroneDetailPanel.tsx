// Vehicle detail, the DroneRail page 2 (M15-FR-021, FR-027, FR-040; AWR-14 §4.4, §6.4, §6.7, §6.11, §6.12, §7.8): header
// with id, model, SIM and owner badges, the shape-coded flight state, camera buttons (Third 3, FPV 4, follow lock L)
// and the trail / frustum layer switches (T, V); line Tabs (telemetry with C-class altitude, speed and heading and the
// D-class battery; mission; sensors; twin); the command area: take-off (AlertDialog with the target altitude, default
// 2.5 m AGL), hover, land and return home (AlertDialog), GoTo (sends to the viewport's ground pick when there is one,
// otherwise enters the GoTo tool) and "more" (safety stop, resume, remove). Buttons follow the command tracker
// (Spinner while pending, CircleCheck / CircleX for INPUT.resultHoldMs, one shake per failure, reason toast) and the
// client-side admission pre-check greys them with the reason in the Tooltip ("take off before GoTo"). Read-only
// sessions see one explanation with "request control" instead of greyed buttons (AWR-14 §7.8).
// DOM hooks kept for the walking-skeleton gate: [data-cmd=<op>], [data-cmd-confirm=<op>], [data-goto-target], the
// "fly here" button name.
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { rtClient } from '@/net/rt'
import { camera, mission, pick, viewport, type CameraMode, type GroundPickView } from '@/viewport/facade'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { ButtonGroup } from '@/ui/components/ui/button-group'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Switch } from '@/ui/components/ui/switch'
import { Table, TableBody, TableCell, TableRow } from '@/ui/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { leaveReplay, replayDeniedKey } from '@/ui/views/replayFlow'
import { LfStat } from '@/ui/lf/LfStat'
import { PanelEmpty } from '@/ui/brand'
import { CallButton } from '@/ui/actions/CallButton'
import { precheck, vehicleState } from '@/ui/actions/admission'
import { runAction } from '@/ui/actions/registry'
import { cmdKeyOf, runGoto, runVehicleCmd } from '@/ui/actions/vehicleCommands'
import { removeVehicles } from '@/ui/actions/removeVehicles'
import { FlightStateBadge } from '@/ui/notify/StatusBadge'
import { useRedOwner } from '@/ui/notify/redFigures'
import { useCameraMode } from '@/ui/layout/ViewportOverlay'
import { useRt } from '@/ui/shell/RtContext'
import { apiGet } from '@/net/api'
import { connViewStore, useConnView } from '@/ui/shell/connView'
import { requestControl } from '@/ui/shell/control'
import { DEMO_PUBLIC } from '@/lib/demo'
import { writeDeniedKey } from '@/ui/shell/guards'
import { toolMode } from '@/ui/tools/toolMode'
import { OWNER_ICON } from '@/ui/panels/drones/DronesPanel'
import { openEditor, RouteStatus } from '@/ui/panels/mission-edit/MissionEditPanel'
import { useFleet } from '@/stores/fleet'
import { layers, useLayers } from '@/stores/layers'
import { prefs } from '@/stores/prefs'
import { useMission, selectMissionRows } from '@/stores/mission'
import { useSensors } from '@/stores/sensors'
import { useSelection } from '@/stores/selection'

const TAKEOFF_ALT = { defaultM: 2.5, minM: 0.5, maxM: 120 } as const // commands.json takeoff.alt_m

/** the viewport's ground pick (re-rendered on viewport changes) */
function useGroundPick(): GroundPickView | null {
  const [, bump] = React.useReducer((x: number) => x + 1, 0)
  React.useEffect(() => viewport.onChange(bump), [])
  return pick.ground
}

function swarmIdx(id: string): number {
  const rt = rtClient()
  if (!rt) return -1
  const no = rt.roster.agentNoOf(id)
  const sw = rt.swarm
  for (let i = 0; i < sw.n; i++) if (sw.agentNo[i] === no) return i
  return -1
}
/** telemetry readers of one vehicle from the swarm columns of the page client (pos, vel, quat) */
function readers(id: string) {
  return {
    alt: () => {
      const i = swarmIdx(id)
      return i < 0 ? Number.NaN : rtClient()!.swarm.pos[3 * i + 2]
    },
    speed: () => {
      const i = swarmIdx(id)
      if (i < 0) return Number.NaN
      const vel = rtClient()!.swarm.vel
      return Math.hypot(vel[3 * i], vel[3 * i + 1], vel[3 * i + 2])
    },
    yaw: () => {
      const i = swarmIdx(id)
      if (i < 0) return Number.NaN
      const q = rtClient()!.swarm.quat
      const x = q[4 * i]
      const y = q[4 * i + 1]
      const z = q[4 * i + 2]
      const w = q[4 * i + 3]
      // yaw of WORLD(ENU) <- BODY(FLU), [x, y, z, w] (AWR-03 §5.3): psi_enu, east = 0, counter-clockwise
      return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    },
  }
}
const fmt1 = (v: number) => fmt.num(v, 1)

function CameraRow({ id }: { id: string }) {
  const t = useT()
  const { mode, followLock } = useCameraMode()
  const visible = useLayers((s) => s.visible)
  return (
    <div className="flex flex-wrap items-center gap-2">
      <ToggleGroup spacing={0} size="sm" variant="outline" value={mode === 'third' || mode === 'fpv' ? [mode] : []} aria-label={t('camera.modes')}
        onValueChange={(v: unknown[]) => void camera.setMode((v[0] as CameraMode | undefined) ?? 'orbit')}>
        <ToggleGroupItem value="third" aria-label={t('camera.third')}><Icon icon="cam.third" />{t('camera.third')}</ToggleGroupItem>
        <ToggleGroupItem value="fpv" aria-label={t('camera.fpv')}><Icon icon="cam.fpv" />{t('camera.fpv')}</ToggleGroupItem>
      </ToggleGroup>
      <Button size="sm" variant={followLock ? 'secondary' : 'outline'} aria-pressed={followLock} disabled={mode !== 'orbit' && mode !== 'bird'} focusableWhenDisabled
        onClick={() => runAction('camera.follow')} aria-label={t('camera.follow')}>
        <StateIcon icon="cam.follow" alt={followLock} />
      </Button>
      <Button size="sm" variant="outline" onClick={() => camera.focus([id])} aria-label={t('camera.focus')}><Icon icon="cmd.track" /></Button>
      <div className="flex items-center gap-3 text-hud-sub">
        <Field orientation="horizontal" className="w-auto gap-1.5">
          <Switch id="detail-trails" size="sm" checked={visible.trails} onCheckedChange={(on: boolean) => layers.setVisible('trails', on)} />
          <FieldLabel htmlFor="detail-trails" className="font-normal">{t('layers.trails')}</FieldLabel>
        </Field>
        <Field orientation="horizontal" className="w-auto gap-1.5">
          <Switch id="detail-frustums" size="sm" checked={visible.frustums} onCheckedChange={(on: boolean) => layers.setVisible('frustums', on)} />
          <FieldLabel htmlFor="detail-frustums" className="font-normal">{t('layers.frustums')}</FieldLabel>
        </Field>
      </div>
    </div>
  )
}

function Commands({ id, ground }: { id: string; ground: GroundPickView | null }) {
  const t = useT()
  const rt = useRt()
  useFleet((s) => s.version) // pre-check follows the flight state (<= 4 Hz)
  useConnView((s) => s.version)
  const [alt, setAlt] = React.useState(String(TAKEOFF_ALT.defaultM))
  const denied = writeDeniedKey()
  const altM = Number(alt)
  const altOk = Number.isFinite(altM) && altM >= TAKEOFF_ALT.minM && altM <= TAKEOFF_ALT.maxM
  // read-only and replay replace the command group by one notice instead of greying every button (AWR-14 §7.1 DroneRail
  // row, §7.8 "隐藏为一处说明"); a read-only session that goes offline keeps its notice. An operator that loses the
  // connection keeps the greyed group with the offline reason, so a short reconnect does not reflow the panel.
  const cv = connViewStore.getState()
  const readOnly = cv.role === 'viewer' || (cv.role !== null && cv.seat !== 'held')
  const notice = denied === 'hint.replay' ? 'hint.replay' : denied === 'hint.readOnly' || (denied === 'hint.offline' && readOnly) ? 'hint.readOnly' : null
  if (notice) {
    return (
      <div data-readonly-commands="" data-reason={notice} className="flex items-center justify-between gap-2 rounded-md border border-dashed px-2 py-1.5 text-hud-sub text-muted-foreground">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <Icon icon={notice === 'hint.replay' ? 'tl.history' : 'layer.visible'} />
          <span className="truncate">{notice === 'hint.readOnly' ? t('detail.readOnly') : t(notice)}</span>
        </span>
        {notice === 'hint.readOnly' && denied !== 'hint.offline' && !DEMO_PUBLIC ? (
          <Button size="xs" variant="outline" onClick={() => void requestControl(rt)}>{t('control.request')}</Button>
        ) : notice === 'hint.replay' && replayDeniedKey() === null ? (
          <Button size="xs" variant="outline" onClick={() => void leaveReplay()}>{t('replay.backToLive')}</Button>
        ) : null}
      </div>
    )
  }
  const reason = (op: string): string | null => {
    if (denied) return t(denied)
    const c = precheck(op, id)
    return c.ok ? null : t(c.reasonKey ?? 'admit.state', { state: t(`fs.${vehicleState(id)?.fs ?? 0}`) })
  }
  const target = ground?.target ?? null
  const gotoReason = reason('goto')
  return (
    <div className="flex flex-col gap-1.5" data-commands="">
      {/* labelled command group (AWR-14 §4.4): take-off, hover, land, return home on one row, GoTo and "more" below */}
      <ButtonGroup className="w-full [&>*]:flex-1">
          <CallButton showLabel cmdKey={cmdKeyOf(id, 'takeoff')} op="takeoff" icon="cmd.takeoff" label={t('command.cmd.takeoff')} hotkey="shift+KeyT"
            disabledReason={reason('takeoff')} onRun={() => runVehicleCmd('takeoff', id, { alt_m: altM })}
            confirm={{
              title: t('command.confirm.takeoff.title'), body: t('command.confirm.takeoff.body', { alt: altOk ? fmt.num(altM, 1) : alt }), action: t('command.cmd.takeoff'),
              actionDisabled: !altOk,
              children: (
                <Field>
                  <FieldLabel htmlFor="takeoff-alt">{t('command.confirm.takeoff.alt')}</FieldLabel>
                  <Input id="takeoff-alt" type="number" inputMode="decimal" step="0.5" min={TAKEOFF_ALT.minM} max={TAKEOFF_ALT.maxM}
                    value={alt} aria-invalid={!altOk} onChange={(e) => setAlt(e.target.value)} />
                </Field>
              ),
            }} />
          <CallButton showLabel cmdKey={cmdKeyOf(id, 'hover')} op="hover" icon="cmd.hover" label={t('command.cmd.hover')} hotkey="KeyH"
            disabledReason={reason('hover')} onRun={() => runVehicleCmd('hover', id)} />
          <CallButton showLabel cmdKey={cmdKeyOf(id, 'land')} op="land" icon="cmd.land" label={t('command.cmd.land')} hotkey="shift+KeyL"
            disabledReason={reason('land')} onRun={() => runVehicleCmd('land', id)}
            confirm={{ title: t('command.confirm.land.title'), body: t('command.confirm.land.body'), action: t('command.cmd.land') }} />
          <CallButton showLabel cmdKey={cmdKeyOf(id, 'rtl')} op="rtl" icon="cmd.rth" label={t('command.cmd.rth')} hotkey="shift+KeyR"
            disabledReason={reason('rtl')} onRun={() => runVehicleCmd('rtl', id)}
            confirm={{ title: t('command.confirm.rth.title'), body: t('command.confirm.rth.body'), action: t('command.cmd.rth') }} />
      </ButtonGroup>
      <div className="flex items-center gap-1">
        <CallButton showLabel className="flex-1" cmdKey={cmdKeyOf(id, 'goto')} op="goto" icon="cmd.goto" label={t('command.cmd.goto')} hotkey="KeyG"
          disabledReason={gotoReason} onRun={() => (target ? void runGoto(id) : toolMode.enterGoto(id))} />
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button size="sm" variant="outline" aria-label={t('detail.more')} />}>
            <Icon icon="more" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{t('detail.more')}</DropdownMenuLabel>
              <DropdownMenuItem disabled={reason('safety_stop') !== null} onClick={() => void runVehicleCmd('safety_stop', id, {}, { toastSuccess: true })}>
                <Icon icon="mission.abort" />{t('command.cmd.safetyStop')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={reason('resume') !== null} onClick={() => void runVehicleCmd('resume', id, {}, { toastSuccess: true })}>
                <Icon icon="mission.start" />{t('command.cmd.resume')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={denied !== null} onClick={() => removeVehicles([id])}>
                <Icon icon="mission.delete" />{t('command.cmd.remove')}
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {/* route editor (D1-AC-17): page 3 of the rail for this vehicle; the result of the last submitted route below */}
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex" />}>
          <Button size="sm" variant="outline" className="w-full" disabled={denied !== null} onClick={() => openEditor(id)} data-edit-route="">
            <Icon icon="mission.edit" data-icon="inline-start" />
            {t('edit.open')}
          </Button>
        </TooltipTrigger>
        <TooltipContent>{denied ? t(denied) : t('edit.openHint')}</TooltipContent>
      </Tooltip>
      <RouteStatus vehicle={id} />
      {target ? (
        <p className="font-mono text-hud-sub text-muted-foreground" data-goto-target="">
          {t('detail.gotoTarget')} {fmt.enu(target[0], target[1], target[2])}
        </p>
      ) : null}
      {mission.gotoState ? <Badge variant="outline" data-goto-state={mission.gotoState}>{t(`goto.state.${mission.gotoState}`)}</Badge> : null}
    </div>
  )
}

function Telemetry({ id }: { id: string }) {
  const t = useT()
  useFleet((s) => s.version)
  const r = React.useMemo(() => readers(id), [id])
  const v = vehicleState(id)
  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-2 gap-x-3 gap-y-2" data-telemetry="">
        <LfStat label={t('detail.alt')} bind={r.alt} format={fmt1} unit="m" />
        <LfStat label={t('detail.speed')} bind={r.speed} format={fmt1} unit="m/s" />
        <LfStat label={t('detail.battery')} value={v && v.battery !== 255 ? v.battery : Number.NaN} format={(x) => fmt.num(x)} unit="%" cls="D" />
        <LfStat label={t('detail.heading')} bind={r.yaw} format={fmt.heading} />
      </div>
      {/* fidelity (M16-FR-006, PRD-AC-005): every value here comes from the simulator, not from a real vehicle */}
      <p className="text-hud-cap text-muted-foreground" data-honesty="simulated">{t('detail.simulatedNote')}</p>
    </div>
  )
}

function MissionTab({ id }: { id: string }) {
  const t = useT()
  const rows = useMission(selectMissionRows)
  const mine = rows.filter((m) => m.vehicles.includes(id))
  if (!mine.length) return <PanelEmpty brand={false} title={t('detail.noMission')} />
  return (
    <div data-lf-table="">
      <Table>
        <TableBody>
          {mine.map((m) => (
            <TableRow key={m.mid}>
              <TableCell className="font-mono">{m.mid}</TableCell>
              <TableCell>{t(`mission.state.${m.state}`)}</TableCell>
              <TableCell data-num="">{fmt.pct(m.progressPct)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function SensorsTab({ id }: { id: string }) {
  const t = useT()
  const vehicleId = useSensors((s) => s.vehicleId)
  const sensors = useSensors((s) => s.sensors)
  if (vehicleId !== id || !sensors.length) return <PanelEmpty brand={false} title={t('detail.noSensors')} />
  return (
    <div data-lf-table="">
      <Table>
        <TableBody>
          {sensors.map((s) => (
            <TableRow key={s.name}>
              <TableCell className="font-mono">{s.name}</TableCell>
              <TableCell>{t(`sensor.kind.${s.kind}`)}</TableCell>
              <TableCell data-num="">{s.hfovDeg !== null ? `${fmt.num(s.hfovDeg)}°` : '—'}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/** status of the vehicle profile of a model (R17 items: profile_id, model, status placeholder | identified), cached 5 min */
function useProfileStatus(model: string | undefined): string | null {
  const q = useQuery({
    queryKey: ['fleet', 'profiles'], staleTime: 300_000,
    queryFn: ({ signal }) => apiGet<{ items?: { model?: string; status?: string }[] }>('/api/fleet/profiles', { signal }),
  })
  if (!model) return null
  return q.data?.items?.find((p) => p.model === model)?.status ?? null
}

export function DroneDetailPanel() {
  const t = useT()
  const primary = useSelection((s) => s.primary)
  const ground = useGroundPick()
  const rt = useRt()
  useFleet((s) => s.version)
  const red = useRedOwner('drone-rail')
  const entry = primary ? rt.roster.get(rt.roster.agentNoOf(primary)) : undefined
  const v = primary ? vehicleState(primary) : null
  const ownerIcon = v ? OWNER_ICON[v.owner] : null
  const profileSt = useProfileStatus(entry?.model)
  // the selection went away (Esc, a new epoch pruned it, the vehicle was removed): back to the list instead of an empty page
  const hadPrimary = React.useRef(primary !== null)
  React.useEffect(() => {
    if (primary === null && hadPrimary.current) prefs.setLayout({ right: { page: 'list' } })
    hadPrimary.current = primary !== null
  }, [primary])
  return (
    <div className="flex flex-col gap-2" data-drone-detail={primary ?? ''}>
      <Button size="xs" variant="ghost" className="self-start" onClick={() => prefs.setLayout({ right: { page: 'list' } })}>
        <Icon icon="chev.left" data-icon="inline-start" />
        {t('drones.back')}
      </Button>
      {primary ? (
        <>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-sm">{primary}</span>
            {entry?.model ? <Badge variant="outline" className="font-mono">{entry.model}</Badge> : null}
            <Badge variant="outline">{entry?.simulated === false ? 'REAL' : 'SIM'}</Badge>
            {/* placeholder vehicle profile: parameters not identified (ADR-043; M16-FR-006 honesty; INT-1) */}
            {profileSt === 'placeholder' ? <Badge variant="outline" data-profile-placeholder="">{t('detail.twinHint')}</Badge> : null}
            {v && ownerIcon ? <Badge variant="outline" className="gap-1"><Icon icon={ownerIcon} />{t(`owner.${v.owner}`)}</Badge> : null}
            {v ? <FlightStateBadge fs={v.fs} sub={v.sub} flags={v.flags} redOwner={red?.id === primary} className="ml-auto" /> : null}
          </div>
          <CameraRow id={primary} />
          <Tabs defaultValue="telemetry">
            <TabsList variant="line">
              <TabsTrigger value="telemetry">{t('detail.telemetry')}</TabsTrigger>
              <TabsTrigger value="mission">{t('detail.mission')}</TabsTrigger>
              <TabsTrigger value="sensors">{t('detail.sensors')}</TabsTrigger>
              <TabsTrigger value="twin">{t('detail.twin')}</TabsTrigger>
            </TabsList>
            <TabsPanels>
              <TabsContent value="telemetry"><Telemetry id={primary} /></TabsContent>
              <TabsContent value="mission"><MissionTab id={primary} /></TabsContent>
              <TabsContent value="sensors"><SensorsTab id={primary} /></TabsContent>
              <TabsContent value="twin"><PanelEmpty brand={false} title={t('detail.noTwin')} description={t('detail.twinHint')} /></TabsContent>
            </TabsPanels>
          </Tabs>
          <Commands id={primary} ground={ground} />
        </>
      ) : (
        <PanelEmpty title={t('detail.noSelection')} />
      )}
    </div>
  )
}
