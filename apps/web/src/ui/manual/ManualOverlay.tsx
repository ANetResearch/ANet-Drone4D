// Manual control overlay (R-D2-21; M15-FR-215 to FR-217; UX-FR-132 to UX-FR-135; AWR-14 §20.10.1 to §20.10.4): the
// takeover confirmation (AlertDialog: the work items of the vehicle are reassigned, the sandbox clock locks x1), and while
// active the control bar under the camera toolbar (vehicle, speed tier, "locked x1", release with Esc, return to the
// nest), the keep-out line (predicted entry in t s, "limited by the soft fence" while the server clamps), the key hint
// card (collapsible; the soft fence switch with a confirmation before turning it off) and two virtual sticks (DOM discs
// of 120 px that only write transform; dead zone and expo in ui/manual/model.ts; release and a screen rotation recentre).
// The sticks show on coarse pointers or after a first touch, keyboard users can hide them.
import * as React from 'react'
import { attachTargetsFeed } from '@/stores/targets'
import { useTasking } from '@/stores/tasking'
import { tOr, useT } from '@/app/i18n'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from '@/ui/components/ui/alert-dialog'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Card } from '@/ui/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/ui/components/ui/collapsible'
import { Kbd, KbdGroup } from '@/ui/components/ui/kbd'
import { Switch } from '@/ui/components/ui/switch'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { manual, setStick, useManual } from './controller'
import { formFactorNow, onFormFactor } from '@/lib/formFactor'

const STICK_PX = 120
const KNOB_PX = 44

/** release the stick: drop the pointer capture, knob back to the centre, zero axes */
function recentreStick(side: 'left' | 'right', el: HTMLDivElement | null, knob: HTMLDivElement | null, active: { current: number | null }): void {
  const id = active.current
  active.current = null
  if (el) {
    delete el.dataset.dragging
    if (id !== null && el.hasPointerCapture?.(id)) el.releasePointerCapture(id)
  }
  knob?.style.setProperty('transform', 'translate(0px, 0px)')
  setStick(side, 0, 0)
}

/** one virtual stick: pointer capture, normalised position (-1..1, y up), recentre on release and on a rotation */
function Stick({ side, label }: { side: 'left' | 'right'; label: string }) {
  const base = React.useRef<HTMLDivElement>(null)
  const knob = React.useRef<HTMLDivElement>(null)
  const active = React.useRef<number | null>(null)
  const move = (e: React.PointerEvent) => {
    const el = base.current
    if (!el || active.current !== e.pointerId) return
    const r = el.getBoundingClientRect()
    const half = STICK_PX / 2
    let x = (e.clientX - (r.left + half)) / half
    let y = (e.clientY - (r.top + half)) / half
    const len = Math.hypot(x, y)
    if (len > 1) {
      x /= len
      y /= len
    }
    knob.current?.style.setProperty('transform', `translate(${(x * (half - KNOB_PX / 2)).toFixed(1)}px, ${(y * (half - KNOB_PX / 2)).toFixed(1)}px)`)
    setStick(side, x, -y)
  }
  const end = (e: React.PointerEvent) => {
    if (active.current !== e.pointerId) return
    recentreStick(side, base.current, knob.current, active)
  }
  // a rotation of the screen recentres the stick (D2-AC-48 "旋转屏幕时归零"): the layout moves under the finger, so the
  // held offset no longer means what the operator aimed at; the finger has to start a new drag
  React.useEffect(() => {
    let orient = formFactorNow().orient
    return onFormFactor((st) => {
      if (st.orient === orient) return
      orient = st.orient
      if (active.current !== null) recentreStick(side, base.current, knob.current, active)
    })
  }, [side])
  return (
    <div data-anchor={side === 'left' ? 'stick-left' : 'stick-right'} className="pointer-events-auto flex flex-col items-center gap-1" data-stick={side}>
      <div ref={base} aria-label={label} className="relative flex touch-none items-center justify-center rounded-full bg-hud ring-1 ring-foreground/20"
        style={{ width: STICK_PX, height: STICK_PX }}
        onPointerDown={(e) => {
          active.current = e.pointerId
          e.currentTarget.setPointerCapture(e.pointerId)
          e.currentTarget.dataset.dragging = ''
          move(e)
        }}
        onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
        <div ref={knob} className="stick-knob rounded-full bg-foreground/30 ring-1 ring-foreground/40" style={{ width: KNOB_PX, height: KNOB_PX }} />
      </div>
      <span className="text-hud-cap text-muted-foreground">{label}</span>
    </div>
  )
}

function KeyHints({ sticks, onSticks }: { sticks: boolean; onSticks: (on: boolean) => void }) {
  const t = useT()
  const guard = useManual((s) => s.guard)
  const [open, setOpen] = React.useState(true)
  const [ask, setAsk] = React.useState(false)
  const rows: { keys: string[]; text: string }[] = [
    { keys: ['W', 'S'], text: t('manual.hint.fwd') },
    { keys: ['A', 'D'], text: t('manual.hint.side') },
    { keys: ['Q', 'E'], text: t('manual.hint.updown') },
    { keys: ['←', '→'], text: t('manual.hint.yaw') },
    { keys: ['C'], text: t('manual.hint.tier') },
    { keys: ['Esc'], text: t('manual.hint.exit') },
    { keys: ['Shift', 'R'], text: t('manual.hint.rtl') },
  ]
  return (
    <div data-anchor="manual-hints" className="pointer-events-auto" data-manual-hints="">
      <Card size="sm" className="w-60 gap-1 rounded-xl bg-hud px-3 py-2">
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger render={<Button size="xs" variant="ghost" className="w-full justify-between" />}>
            <span className="flex items-center gap-1"><Icon icon="manual.stick" />{t('manual.hints')}</span>
            <StateIcon icon="chev.down" alt={!open} />
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-1 pt-1">
            {rows.map((r) => (
              <span key={r.text} className="flex items-center justify-between gap-2 text-hud-sub">
                <KbdGroup>{r.keys.map((k) => <Kbd key={k}>{k}</Kbd>)}</KbdGroup>
                <span className="text-muted-foreground">{r.text}</span>
              </span>
            ))}
            <span className="mt-1 flex items-center justify-between gap-2 text-hud-sub">
              <span>{t('manual.fence')}</span>
              <Switch checked={guard === 'clamp'} aria-label={t('manual.fence')} data-manual-fence={guard}
                onCheckedChange={(on: boolean) => (on ? manual.setGuard('clamp') : setAsk(true))} />
            </span>
            <span className="text-hud-cap text-muted-foreground">{t('manual.fenceNext')}</span>
            <span className="flex items-center justify-between gap-2 text-hud-sub">
              <span>{t('manual.sticks')}</span>
              <Switch checked={sticks} aria-label={t('manual.sticks')} onCheckedChange={(on: boolean) => onSticks(on)} data-manual-sticks={sticks ? 'on' : 'off'} />
            </span>
          </CollapsibleContent>
        </Collapsible>
      </Card>
      <AlertDialog open={ask} onOpenChange={setAsk}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('manual.fenceOff.title')}</AlertDialogTitle>
            <AlertDialogDescription>{t('manual.fenceOff.body')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={() => {
              manual.setGuard('warn')
              setAsk(false)
            }}>{t('manual.fenceOff.action')}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function ControlBar() {
  // the 3 s keep-out prediction reads M18's target model: keep its feed attached while a vehicle is under control
  React.useEffect(() => attachTargetsFeed(), [])
  const t = useT()
  const vehicle = useManual((s) => s.vehicle)
  const tier = useManual((s) => s.tier)
  const approach = useManual((s) => s.approach)
  const clamped = useManual((s) => s.clamped)
  return (
    <div data-anchor="manual-bar" className="pointer-events-auto" data-manual-bar="">
      <Card size="sm" className="gap-1 rounded-xl bg-hud px-3 py-1.5">
        <span className="flex items-center gap-2 text-hud-sub">
          <Icon icon="manual.stick" />
          <span className="font-medium">{t('manual.title', { id: vehicle ?? '' })}</span>
          <Badge variant="outline" data-manual-tier={tier}>{t(`manual.tier.${tier}`)}</Badge>
          <Badge variant="outline">{t('manual.locked')}</Badge>
          <Button size="xs" variant="outline" onClick={() => void manual.release()} data-manual-release="">{t('manual.release')}<Kbd>Esc</Kbd></Button>
          <Button size="xs" variant="ghost" onClick={() => void manual.release({ returnHome: true })} data-manual-home="">{t('manual.home')}</Button>
        </span>
        {approach ? (
          <span className="flex items-center gap-1.5 text-hud-sub" data-manual-approach={approach.target}>
            <Icon icon="alert.warning" />
            {approach.tS <= 0 ? t('manual.inside', { target: approach.target, kind: t(`sens.kind.${approach.kind}`) })
              : t('manual.approach', { t: approach.tS.toFixed(1), target: approach.target, kind: t(`sens.kind.${approach.kind}`) })}
            {clamped ? <Badge variant="outline" data-manual-clamped="">{t('manual.clampedShort')}</Badge> : null}
          </span>
        ) : clamped ? <span className="text-hud-sub text-muted-foreground" data-manual-clamped="">{t('manual.clampedShort')}</span> : null}
      </Card>
    </div>
  )
}

function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = React.useState(() => typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches)
  React.useEffect(() => {
    const on = (e: PointerEvent) => {
      if (e.pointerType === 'touch') setCoarse(true)
    }
    addEventListener('pointerdown', on, { passive: true })
    return () => removeEventListener('pointerdown', on)
  }, [])
  return coarse
}

/** the takeover confirmation */
/** the task work item a vehicle executes (M20 assignment), for the takeover confirmation (AWR-14 §20.10.1) */
function useCurrentWork(vehicle: string | null): string | null {
  const t = useT()
  const tasks = useTasking((s) => s.tasks)
  return React.useMemo(() => {
    if (!vehicle) return null
    for (const r of tasks) {
      if (r.state !== 'RUNNING' && r.state !== 'PAUSED' && r.state !== 'READY') continue
      const it = r.items.find((x) => x.uav === vehicle && (x.state === 'ASSIGNED' || x.state === 'ACTIVE' || x.state === 'WORKING'))
      if (it) return t('manual.confirm.work', { task: r.label || t(`task.type.${r.type}`), item: tOr(`task.item.${it.kind}`, it.kind), wid: it.wid })
    }
    return null
  }, [tasks, vehicle, t])
}

function Confirm() {
  const t = useT()
  const state = useManual((s) => s.state)
  const vehicle = useManual((s) => s.vehicle)
  const work = useCurrentWork(state === 'CONFIRMING' ? vehicle : null)
  return (
    <AlertDialog open={state === 'CONFIRMING'} onOpenChange={(o) => {
      if (!o) manual.cancel()
    }}>
      <AlertDialogContent data-manual-confirm={vehicle ?? ''}>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('manual.confirm.title', { id: vehicle ?? '' })}</AlertDialogTitle>
          <AlertDialogDescription className="flex flex-col gap-1">
            {work ? <span data-manual-work="">{work}</span> : null}
            <span>{t('manual.confirm.body')}</span>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
          <AlertDialogAction onClick={() => void manual.confirm()} data-manual-confirm-go="">{t('manual.confirm.action')}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function ManualOverlay() {
  const t = useT()
  const state = useManual((s) => s.state)
  const coarse = useCoarsePointer()
  const [sticks, setSticks] = React.useState<boolean | null>(null)
  const show = sticks ?? coarse
  return (
    <>
      <Confirm />
      {state === 'ACTIVE' || state === 'STOPPING' ? (
        <>
          <ControlBar />
          <KeyHints sticks={show} onSticks={setSticks} />
          {show ? (
            <>
              <Stick side="left" label={t('manual.stick.left')} />
              <Stick side="right" label={t('manual.stick.right')} />
            </>
          ) : null}
        </>
      ) : null}
    </>
  )
}
