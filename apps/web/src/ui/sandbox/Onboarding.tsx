// Onboarding (ADR-112; AWR-04 §10.4 item 3; M15-FR-209; UX-FR-142; AWR-14 §20.13.2): four non-modal Popover steps the
// first time a sandbox is ready in this browser (`awr.ui.onboarding.v1`, localStorage, fault tolerant): (1) the session
// bar and its lifetime, (2) the targets entity and the sensitivity ranges, (3) the tasks (the area guard already runs:
// "view the allocation"; new tasks can preview theirs), (4) the capture notice and the FPV views. A step anchors to its
// element and falls back to the top centre of the unobscured rect when the element is hidden (folded rail, compact
// breakpoint). It never takes the focus or makes the page inert (the title is announced once), the arrow keys move
// between steps while the focus is inside, Esc or "skip" ends it for good; Help > onboarding starts it again.
import * as React from 'react'
import { useStore } from 'zustand'
import { useT } from '@/app/i18n'
import { createAwrStore } from '@/lib/createStore'
import { Button } from '@/ui/components/ui/button'
import { Popover, PopoverContent, PopoverDescription, PopoverHeader, PopoverTitle } from '@/ui/components/ui/popover'
import { useSurface } from '@/app/shell/surface'
import { useSandbox } from './session'

export const ONBOARDING_KEY = 'awr.ui.onboarding.v1'
export const STEPS = ['session', 'targets', 'tasks', 'capture'] as const
const ANCHOR: Readonly<Record<(typeof STEPS)[number], string>> = {
  session: '[data-session-bar]',
  targets: '[data-entity="targets"]',
  tasks: '[data-dock-tab="tasks"], [data-panel="events"]',
  capture: '[data-slot="toast-viewport"], [data-badge="focus-low-latency"], [data-anchor="top-center"]',
}

interface Stored { done: boolean; step: number; v: 1 }
export function readOnboarding(): Stored {
  try {
    const raw = globalThis.localStorage?.getItem(ONBOARDING_KEY)
    const x = raw ? (JSON.parse(raw) as Partial<Stored>) : null
    if (x && typeof x.done === 'boolean') return { done: x.done, step: Math.min(STEPS.length - 1, Math.max(0, Number(x.step) || 0)), v: 1 }
  } catch {
    // unreadable: as a first visit
  }
  return { done: false, step: 0, v: 1 }
}
function write(s: Stored): void {
  try {
    globalThis.localStorage?.setItem(ONBOARDING_KEY, JSON.stringify(s))
  } catch {
    // private mode: the guide may show again next time
  }
}

interface OnboardingState { open: boolean; step: number }
const store = createAwrStore<OnboardingState>('ui.onboarding', () => ({ open: false, step: 0 }))

export const onboarding = {
  /** start (first ready sandbox, or Help > onboarding) */
  start(force = false): void {
    const s = readOnboarding()
    if (s.done && !force) return
    store.setState({ open: true, step: force ? 0 : s.step })
  },
  go(step: number): void {
    const k = Math.min(STEPS.length - 1, Math.max(0, step))
    store.setState({ step: k })
    write({ done: false, step: k, v: 1 })
  },
  finish(): void {
    store.setState({ open: false })
    write({ done: true, step: STEPS.length - 1, v: 1 })
  },
}

function anchorOf(step: number): Element | null {
  if (typeof document === 'undefined') return null
  for (const sel of ANCHOR[STEPS[step]].split(',')) {
    const el = document.querySelector(sel.trim())
    if (el && (el as HTMLElement).offsetParent !== null) return el
  }
  return document.querySelector('[data-anchor="top-center"]')
}

export function Onboarding() {
  const t = useT()
  const open = useStore(store, (s) => s.open)
  const step = useStore(store, (s) => s.step)
  const sandbox = useSurface((s) => s.surface === 'sandbox')
  const ready = useSandbox((s) => s.phase === 'ready' || s.phase === 'active')
  React.useEffect(() => {
    if (sandbox && ready) onboarding.start()
  }, [sandbox, ready])
  // the step's element (read when the step changes; the layout is up by then)
  const anchor = React.useMemo(() => (open ? anchorOf(step) : null), [open, step])
  if (!open || !sandbox || !anchor) return null
  const key = STEPS[step]
  const last = step === STEPS.length - 1
  return (
    <Popover open={open} modal={false} onOpenChange={(o, e) => {
      // outside clicks never close it (non-modal guide); Esc ends it
      if (!o && e.reason === 'escape-key') onboarding.finish()
    }}>
      <PopoverContent anchor={anchor} side="bottom" initialFocus={false} data-onboarding={key}
        onKeyDown={(e) => {
          if (e.key === 'ArrowRight' && !last) onboarding.go(step + 1)
          if (e.key === 'ArrowLeft' && step > 0) onboarding.go(step - 1)
        }}>
        <PopoverHeader>
          <PopoverTitle role="status">{t('onboarding.title', { n: step + 1, of: STEPS.length, title: t(`onboarding.${key}.title`) })}</PopoverTitle>
          <PopoverDescription>{t(`onboarding.${key}.body`)}</PopoverDescription>
        </PopoverHeader>
        <div className="flex items-center gap-2">
          <Button size="xs" variant="ghost" onClick={() => onboarding.finish()} data-onboarding-skip="">{t('onboarding.skip')}</Button>
          <Button size="xs" variant="outline" className="ml-auto" disabled={step === 0} onClick={() => onboarding.go(step - 1)}>{t('onboarding.prev')}</Button>
          <Button size="xs" onClick={() => (last ? onboarding.finish() : onboarding.go(step + 1))} data-onboarding-next="">{last ? t('onboarding.done') : t('onboarding.next')}</Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
