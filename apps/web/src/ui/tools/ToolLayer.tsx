// Tool presentation (M15-FR-027, FR-028; AWR-14 §4.3, §6.7, §6.13): the hint bar (Alert + Kbd, top centre of the
// unobscured rect, 07 Y-axis variant), the GoTo confirmation Popover and the Add-P600 Popover, both anchored at the picked
// surface point through a VirtualElement (viewport.projectToScreen). GoTo sends with the M06 altitude rule (directly
// above the hit, keep the current height, never below hit + 10 m) and an optional speed; Add P600 posts
// POST /api/fleet/vehicles with an Idempotency-Key (AWR-17 §4.3.5). Failures toast the reason text and keep the tool so
// the operator can pick again.
import * as React from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { reasonText, useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { INPUT } from '@/lib/tokens/input.gen'
import { apiGet, apiPost, ApiError } from '@/net/api'
import { pick, viewport } from '@/viewport/facade'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from '@/ui/components/ui/input-group'
import { Kbd } from '@/ui/components/ui/kbd'
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle } from '@/ui/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/components/ui/select'
import { Icon } from '@/ui/icons/Icon'
import { runGoto } from '@/ui/actions/vehicleCommands'
import { toolMode, useTool } from './toolMode'

const FLASH_MS = INPUT.resultHoldMs
const out = new Float32Array(2)

/** VirtualElement at the projected ENU point (recomputed on every positioner update) */
function useAnchor(p: readonly [number, number, number] | null): { getBoundingClientRect: () => DOMRect } | null {
  return React.useMemo(() => {
    if (!p) return null
    return {
      getBoundingClientRect: () => {
        const ok = viewport.projectToScreen(p, out)
        const x = ok ? out[0] : -1000
        const y = ok ? out[1] : -1000
        return DOMRect.fromRect({ x, y, width: 0, height: 0 })
      },
    }
  }, [p])
}

export function ToolHint() {
  const t = useT()
  const mode = useTool((s) => s.mode)
  const flash = useTool((s) => s.flash)
  const [fresh, setFresh] = React.useState(false)
  const [seen, setSeen] = React.useState(flash)
  if (flash !== seen) {
    setSeen(flash)
    setFresh(flash !== null)
  }
  React.useEffect(() => {
    if (!flash) return
    const h = setTimeout(() => setFresh(false), FLASH_MS)
    return () => clearTimeout(h)
  }, [flash])
  const text = fresh && flash ? t(flash.key) : mode === 'GOTO_PICK' ? t('tool.goto.hint') : mode === 'ADD_PICK' ? t('tool.add.hint')
    : mode === 'GOTO_CONFIRM' ? t('tool.goto.confirmHint') : mode === 'ADD_CONFIRM' ? t('tool.add.confirmHint') : null
  if (!text) return null
  return (
    <div data-anchor="top-center" data-tool-hint={mode} className="mt-11">
      <Alert className="flex items-center gap-2 bg-hud px-3 py-1.5">
        <Icon icon={mode.startsWith('ADD') ? 'plus' : 'cmd.goto'} />
        <AlertDescription className="flex items-center gap-1.5 text-hud-sub">
          {text}
          {!fresh && mode !== 'IDLE' ? <Kbd>Esc</Kbd> : null}
        </AlertDescription>
      </Alert>
    </div>
  )
}

function GotoPopover() {
  const t = useT()
  const mode = useTool((s) => s.mode)
  const anchorPt = useTool((s) => s.anchor)
  const vehicle = useTool((s) => s.vehicle)
  const anchor = useAnchor(mode === 'GOTO_CONFIRM' ? anchorPt : null)
  const [speed, setSpeed] = React.useState('')
  const ground = pick.ground
  const target = ground?.target ?? null
  const raised = target !== null && ground !== null && Math.abs(target[2] - (ground.surface[2] + 10)) < 1e-3
  const sp = Number(speed)
  const speedOk = speed === '' || (Number.isFinite(sp) && sp > 0 && sp <= 12)
  React.useEffect(() => {
    toolMode.setDirectGoto((v) => {
      runGoto(v)
    })
    return () => toolMode.setDirectGoto(null)
  }, [])
  const send = () => {
    if (!vehicle || !speedOk) return
    runGoto(vehicle, speed === '' ? {} : { speedMps: sp })
    toolMode.done()
  }
  return (
    <Popover open={mode === 'GOTO_CONFIRM' && anchor !== null} onOpenChange={(o) => {
      if (!o) toolMode.escape()
    }}>
      <PopoverContent anchor={anchor ?? undefined} side="top" sideOffset={12} className="w-72" data-tool-popover="goto"
        onKeyDown={(e) => {
          if (e.key === 'Enter') send()
        }}>
        <PopoverHeader>
          <PopoverTitle>{t('tool.goto.title', { id: vehicle ?? '' })}</PopoverTitle>
          <PopoverDescription className="font-mono">{target ? fmt.enu(target[0], target[1], target[2]) : '—'}</PopoverDescription>
        </PopoverHeader>
        {raised ? <p className="text-hud-sub text-muted-foreground">{t('tool.goto.raised')}</p> : null}
        <Field>
          <FieldLabel htmlFor="goto-speed">{t('tool.goto.speed')}</FieldLabel>
          <InputGroup>
            <InputGroupInput id="goto-speed" type="number" inputMode="decimal" min={0.5} max={12} step={0.5} value={speed}
              placeholder={t('tool.goto.speedDefault')} aria-invalid={!speedOk} onChange={(e) => setSpeed(e.target.value)} />
            <InputGroupAddon align="inline-end"><InputGroupText>m/s</InputGroupText></InputGroupAddon>
          </InputGroup>
        </Field>
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="outline" onClick={() => toolMode.escape()}>{t('common.cancel')}</Button>
          <Button size="sm" disabled={!speedOk || !target} onClick={send} data-tool-send="goto">
            <Icon icon="cmd.goto" data-icon="inline-start" />
            {t('command.cmd.goto')}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

// R17 items carry profile_id and display_name (AWR-17 §4.3.5); id / name kept as fallbacks
interface Profile { profile_id?: string; id?: string; display_name?: string; name?: string; name_zh?: string }
const DEFAULT_PROFILE = 'p600_mid360'
const VEHICLE_ID = /^[a-z0-9]+(-[a-z0-9]+)*$/

function AddPopover() {
  const t = useT()
  const mode = useTool((s) => s.mode)
  const anchorPt = useTool((s) => s.anchor)
  const anchor = useAnchor(mode === 'ADD_CONFIRM' ? anchorPt : null)
  const [id, setId] = React.useState('')
  const [yaw, setYaw] = React.useState('0')
  const [profile, setProfile] = React.useState(DEFAULT_PROFILE)
  const profiles = useQuery({
    queryKey: ['fleet', 'profiles'], staleTime: 300_000, enabled: mode === 'ADD_CONFIRM',
    queryFn: ({ signal }) => apiGet<{ items?: Profile[] } | Profile[]>('/api/fleet/profiles', { signal }),
    select: (d) => (Array.isArray(d) ? d : (d.items ?? [])),
  })
  const items = (profiles.data?.length ? profiles.data : [{ profile_id: DEFAULT_PROFILE }]).map((p) => {
    const pid = p.profile_id ?? p.id ?? DEFAULT_PROFILE
    return { value: pid, label: p.name_zh ?? p.display_name ?? p.name ?? pid }
  })
  const idOk = id === '' || VEHICLE_ID.test(id)
  const yawDeg = Number(yaw)
  const yawOk = Number.isFinite(yawDeg) && yawDeg >= -180 && yawDeg <= 360
  const add = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiPost<{ vehicle_id?: string; id?: string }>('/api/fleet/vehicles', body, { headers: { 'Idempotency-Key': crypto.randomUUID() } }),
    onSuccess: (r) => {
      notify('fleet:add', 'info', t('tool.add.sent', { id: r.vehicle_id ?? r.id ?? id }))
      toolMode.done()
    },
    onError: (e) => {
      const code = e instanceof ApiError ? (e.reason ?? 0) : 0
      notify(`fleet:add:${code}`, 'warning', t('tool.add.failed'), code ? `${code} ${reasonText(code).short}` : e.message)
      toolMode.escape()
    },
  })
  const submit = () => {
    if (!anchorPt || !idOk || !yawOk || add.isPending) return
    // yaw in the UI is a compass heading (0 = north, clockwise); the wire takes psi_enu (0 = east, counter-clockwise)
    const yawRad = ((90 - yawDeg) * Math.PI) / 180
    const body: Record<string, unknown> = { profile_id: profile, home_enu_m: [anchorPt[0], anchorPt[1], null], yaw_rad: Math.atan2(Math.sin(yawRad), Math.cos(yawRad)) }
    if (id) body.vehicle_id = id
    add.mutate(body)
  }
  return (
    <Popover open={mode === 'ADD_CONFIRM' && anchor !== null} onOpenChange={(o) => {
      if (!o) toolMode.escape()
    }}>
      <PopoverContent anchor={anchor ?? undefined} side="top" sideOffset={12} className="w-72" data-tool-popover="add"
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit()
        }}>
        <PopoverHeader>
          <PopoverTitle>{t('tool.add.title')}</PopoverTitle>
          <PopoverDescription className="font-mono">{anchorPt ? fmt.enu(anchorPt[0], anchorPt[1], anchorPt[2]) : '—'}</PopoverDescription>
        </PopoverHeader>
        <Field>
          <FieldLabel htmlFor="add-id">{t('tool.add.id')}</FieldLabel>
          <InputGroup>
            <InputGroupInput id="add-id" value={id} placeholder={t('tool.add.idAuto')} aria-invalid={!idOk} onChange={(e) => setId(e.target.value.trim())} />
          </InputGroup>
        </Field>
        <Field>
          <FieldLabel>{t('tool.add.profile')}</FieldLabel>
          <Select items={items} value={profile} onValueChange={(v) => v && setProfile(String(v))}>
            <SelectTrigger size="sm" className="w-full" aria-label={t('tool.add.profile')}><SelectValue /></SelectTrigger>
            <SelectContent>{items.map((p) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="add-yaw">{t('tool.add.heading')}</FieldLabel>
          <InputGroup>
            <InputGroupInput id="add-yaw" type="number" inputMode="decimal" step={15} value={yaw} aria-invalid={!yawOk} onChange={(e) => setYaw(e.target.value)} />
            <InputGroupAddon align="inline-end"><InputGroupText>°</InputGroupText></InputGroupAddon>
          </InputGroup>
        </Field>
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="outline" onClick={() => toolMode.escape()}>{t('common.cancel')}</Button>
          <Button size="sm" disabled={!idOk || !yawOk || add.isPending} onClick={submit} data-tool-send="add">
            <Icon icon="plus" data-icon="inline-start" />
            {t('world.addP600')}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

export function ToolPopovers() {
  return (
    <>
      <GotoPopover />
      <AddPopover />
    </>
  )
}
