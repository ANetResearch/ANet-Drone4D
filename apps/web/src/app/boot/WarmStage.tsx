// Warm stage under the boot mask (ADR-069; M15-FR-006; D1-AC-03b, 04, 25): specimens of the UI that is not on screen at
// the reveal but appears later (toasts of every level, dialog, command palette list, popover and menu, tooltip, the
// overlay, buttons and badges of every variant, form controls) and a 3D cube built like the viewport ViewCube
// (preserve-3d, rounded faces that clip, hidden back faces). On Tier S every new kind of DOM content makes the GPU process
// compile Skia raster and compositor programs and SwiftShader JIT its draw routines the first time it is drawn: 100-400
// ms frames at the first toast, the first palette or the first dialog (FX2-R3 traces: Compile/Link on the first palette,
// 3 programs on the first toast), and the ViewCube faces compile their render-pass programs when the camera first turns.
// The stage stays display:none until BootMask starts the pre-raster phase under the 0.996 mask; the cube turns a little
// every frame of the phase (warmStep), so every face orientation and perspective state is drawn once. The palette list
// itself (PaletteList, about a hundred items) is rendered once, so the first Mod+K does not pay its first mount either.
// BootMask hides the stage synchronously before the fade starts, then it unmounts. Specimens use the same components and
// classes as the real popups but no Base UI roots (no focus trap, no scroll lock, no portal), inert and hidden from
// assistive technology. Only the shell mounts it (never ?chrome=0).
import * as React from 'react'
import { t } from '@/app/i18n'
import { boot } from './BootController'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/ui/components/ui/card'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Command, CommandInput } from '@/ui/components/ui/command'
import { Input } from '@/ui/components/ui/input'
import { Kbd } from '@/ui/components/ui/kbd'
import { Progress } from '@/ui/components/ui/progress'
import { Separator } from '@/ui/components/ui/separator'
import { Skeleton } from '@/ui/components/ui/skeleton'
import { Spinner } from '@/ui/components/ui/spinner'
import { Switch } from '@/ui/components/ui/switch'
import {
  Toast, ToastClose, ToastContent, ToastDescription, ToastProvider, ToastTitle, ToastViewport, createToastManager, useToastManager,
} from '@/ui/components/ui/toast'
import { CircleCheckIcon, InfoIcon, OctagonXIcon, TriangleAlertIcon } from '@/ui/icons/lucide-compat'
import { Icon } from '@/ui/icons/Icon'
import { StatusBadge } from '@/ui/notify/StatusBadge'
import { PaletteList } from '@/ui/views/CommandPalette'

const noop = (): void => {}

let stageEl: HTMLElement | null = null
let cubeEl: HTMLElement | null = null
let shown = false

/** show the stage (start of the pre-raster phase, below the 0.996 mask) */
export function showWarmStage(): void {
  shown = true
  if (stageEl) stageEl.hidden = false
}

/** hide the stage at once (BootMask calls it in the same task that starts the fade, so no specimen is ever visible) */
export function hideWarmStage(): void {
  shown = false
  if (stageEl) stageEl.hidden = true
}

/** one frame of the phase: turn the cube so that every face and perspective state gets drawn */
export function warmStep(frame: number): void {
  if (!cubeEl) return
  const yaw = (frame * 47) % 360
  const pitch = ((frame * 29) % 140) - 70
  cubeEl.style.transform = `translateZ(-36px) rotateX(${pitch}deg) rotateY(${yaw}deg)`
}

const CUBE_FACES = ['translateZ(36px)', 'rotateY(180deg) translateZ(36px)', 'rotateY(90deg) translateZ(36px)', 'rotateY(-90deg) translateZ(36px)',
  'rotateX(90deg) translateZ(36px)', 'rotateX(-90deg) translateZ(36px)'] as const

/** a cube built like viewport/overlay/ViewCube.tsx (same classes and 3D structure), without its interaction */
function WarmCube() {
  return (
    <div className="size-18 [perspective:400px]">
      <div ref={(el) => { cubeEl = el }} className="relative size-full [transform-style:preserve-3d]">
        {CUBE_FACES.map((tf, k) => (
          <div key={tf} className="absolute inset-0 grid grid-cols-3 grid-rows-3 overflow-hidden rounded-sm bg-muted ring-1 ring-foreground/10 [backface-visibility:hidden]" style={{ transform: tf }}>
            <span className={`pointer-events-none absolute inset-0 flex items-center justify-center text-hud-cap ${k === 1 ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}>{k}</span>
            <div className="relative z-10 hover:bg-foreground/10" />
          </div>
        ))}
      </div>
    </div>
  )
}

const BUTTONS = ['default', 'outline', 'secondary', 'ghost', 'destructive', 'link'] as const
const BADGES = ['default', 'secondary', 'outline', 'destructive'] as const
const STATUS = ['nominal', 'warning', 'critical-primary', 'critical-secondary', 'stale', 'muted', 'outline', 'solid'] as const
const TOASTS = [['info', InfoIcon], ['warning', TriangleAlertIcon], ['error', OctagonXIcon], ['success', CircleCheckIcon]] as const

/** toasts of every level through a manager of their own (same parts as ui/components/ui/toast.tsx ToastList) */
function WarmToasts() {
  const manager = React.useMemo(() => createToastManager(), [])
  React.useEffect(() => {
    for (const [type] of TOASTS) manager.add({ title: t('governor.toast', { what: t('camera.follow') }), description: t('governor.toastHint'), type, timeout: 0 })
  }, [manager])
  return (
    <ToastProvider toastManager={manager} limit={TOASTS.length} timeout={0}>
      <ToastViewport className="static! inset-auto! mx-0! w-90! max-w-90!">
        <WarmToastList />
      </ToastViewport>
    </ToastProvider>
  )
}

function WarmToastList() {
  const { toasts } = useToastManager()
  return toasts.map((item) => {
    const Glyph = TOASTS.find(([type]) => type === item.type)?.[1] ?? InfoIcon
    return (
      <Toast key={item.id} toast={item}>
        <ToastContent>
          <span className="shrink-0 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4"><Glyph aria-hidden="true" /></span>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <ToastTitle />
            <ToastDescription />
          </div>
          <ToastClose />
        </ToastContent>
      </Toast>
    )
  })
}

/** popup surfaces (classes of dialog-content, alert-dialog-content, command, popover, menu and tooltip popups) */
function WarmPopups() {
  return (
    <div className="flex flex-wrap items-start gap-3">
      <div className="relative isolate size-40 bg-overlay" />
      <div className="grid w-72 gap-4 rounded-xl bg-popover p-4 text-xs/relaxed text-popover-foreground ring-1 ring-foreground/10">
        <div className="text-sm font-medium">{t('palette.title')}</div>
        <div className="text-xs/relaxed text-muted-foreground">{t('palette.description')}</div>
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="outline">{t('common.cancel')}</Button>
          <Button>{t('common.retry')}</Button>
        </div>
      </div>
      <div className="group/alert-dialog-content grid w-64 gap-3 rounded-xl bg-popover p-4 text-popover-foreground ring-1 ring-foreground/10">
        <div className="mb-2 inline-flex size-8 items-center justify-center rounded-md bg-muted *:[svg:not([class*='size-'])]:size-4"><TriangleAlertIcon /></div>
        <div className="text-xs/relaxed text-muted-foreground">{t('governor.toastHint')}</div>
      </div>
      <div className="flex h-72 w-80 flex-col overflow-hidden rounded-xl bg-popover p-1 text-popover-foreground">
        <Command>
          <CommandInput placeholder={t('palette.placeholder')} readOnly tabIndex={-1} />
          <PaletteList query="" vehicles={[]} denied={null} onClose={noop} />
        </Command>
      </div>
      <div className="flex w-72 flex-col gap-4 rounded-lg bg-popover p-2.5 text-xs text-popover-foreground shadow-md ring-1 ring-foreground/10">
        {t('hint.noFocus')}
      </div>
      <div className="w-auto min-w-32 rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-foreground/10">
        <div className="relative flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs/relaxed bg-accent text-accent-foreground">{t('events.level.all')}</div>
        <div className="relative flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs/relaxed">{t('events.level.critical')}</div>
      </div>
      <div className="relative inline-flex w-fit max-w-xs items-center gap-1.5 rounded-md bg-foreground px-3 py-1.5 pr-1.5 text-xs text-background">
        {t('camera.follow')}<Kbd>L</Kbd>
        <span className="absolute -bottom-1 left-1/2 size-2.5 rotate-45 rounded-xs bg-foreground" />
      </div>
    </div>
  )
}

function WarmControls() {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {BUTTONS.map((v) => <Button key={v} variant={v} size="sm"><Icon icon="cmd.track" data-icon="inline-start" />{t('camera.focus')}</Button>)}
      <Button size="icon-sm" variant="ghost" aria-label={t('camera.focus')}><Icon icon="cam.reset" /></Button>
      <Button size="sm" variant="outline" className="border-ring ring-2 ring-ring/30">{t('common.retry')}</Button>
      {BADGES.map((v) => <Badge key={v} variant={v}><Icon icon="alert.info" data-icon="inline-start" />{t('level.warning')}</Badge>)}
      {STATUS.map((k) => <StatusBadge key={k} kind={k} text={t(k.startsWith('critical') ? 'level.critical' : 'level.warning')} ageS={k === 'stale' ? 3.2 : undefined} />)}
      <Switch defaultChecked aria-label={t('camera.follow')} />
      <Switch aria-label={t('camera.follow')} />
      <Checkbox defaultChecked aria-label={t('camera.follow')} />
      <Checkbox aria-label={t('camera.follow')} />
      <Progress value={60} className="w-32" aria-label={t('camera.follow')} />
      <Spinner />
      <Skeleton className="h-4 w-20" />
      <Separator orientation="vertical" className="h-4" />
      <Kbd>Ctrl K</Kbd>
      <Input className="h-7 w-40" defaultValue={t('palette.placeholder')} readOnly tabIndex={-1} />
      <span className="underline decoration-dashed underline-offset-2">{t('panel.events.title')}</span>
    </div>
  )
}

/** the stage; GlobalLayers mounts it once (shell only) and it unmounts at the reveal or a boot error */
export function WarmStage() {
  const [on, setOn] = React.useState(() => boot.state !== 'REVEALED' && boot.state !== 'BOOT_ERROR')
  React.useEffect(() => boot.subscribe((s) => {
    if (s === 'REVEALED' || s === 'BOOT_ERROR') {
      hideWarmStage()
      setOn(false)
    }
  }), [])
  if (!on) return null
  return (
    <div ref={(el) => { stageEl = el }} data-warm-stage="" aria-hidden="true" inert hidden={!shown} className="warm-stage">
      <WarmCube />
      <WarmToasts />
      <WarmPopups />
      <WarmControls />
      <Card size="sm" className="w-72">
        <CardHeader><CardTitle>{t('rail.right')}</CardTitle></CardHeader>
        <CardContent className="flex items-center gap-2 text-hud-sub text-muted-foreground"><Icon icon="state.hold" />{t('env.stale')}</CardContent>
      </Card>
    </div>
  )
}
