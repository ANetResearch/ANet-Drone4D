// State icon (M15-FR-061; AWR-15 §7.5; d03 §3.4; g07 §7): one <svg> holding a main path (morphicons driver) and a ghost
// path for swaps. full tier: whitelisted pairs morph with a spring (snappy for user actions, smooth for state processes,
// hud for lists and the HUD) inside the K = 8 budget; other pairs swap (transitions.dev 09 icon-swap, WAAPI, blur only
// when a blur slot is granted). lite: always swap, keyframes without `filter`. reduced and off: set. The morph driver
// runs with reducedMotion "user"; while a morph runs the root carries data-morphing.
import * as React from 'react'
import { createMorph, type Morph } from 'morphicons/dom'
import { cn } from '@/lib/utils'
import { EASE_CSS, MOTION, SPRINGS } from '@/lib/tokens/motion.gen'
import { getMotionTier, useMotionTier } from '@/ui/motion/tier'
import { motionBudget } from '@/ui/motion/budget'
import { PERF_UI } from '@/ui/shell/perfUi'
import { iconD, resolveIcon } from './Icon'
import { morphBudget } from './morphBudget'
import { PARTNERS, type IconKey } from './registry'
import type { IconNode } from './types'
import { morphSpring, type SpringName } from './whitelist'

export type StateIconProps = Omit<React.SVGProps<SVGSVGElement>, 'ref'> & {
  icon: IconKey
  /** show the partner geometry of the key (the other end of its morph or swap pair), for example layer.visible off */
  alt?: boolean
  spring?: SpringName
  label?: string
}

const SPRING_OPTS = {
  snappy: { stiffness: SPRINGS.snappy.k, damping: SPRINGS.snappy.c },
  smooth: { stiffness: SPRINGS.smooth.k, damping: SPRINGS.smooth.c },
  hud: { stiffness: SPRINGS.hud.k, damping: SPRINGS.hud.c },
} as const

/** transitions.dev 09 keyframes; filter only appears in the blur variant (a blur(0px) keyframe would still animate) */
function playSwap(ghost: SVGPathElement, main: SVGPathElement, fromD: string, withBlur: boolean): void {
  const s = MOTION.iconSwapStartScale
  const out: Keyframe[] = withBlur
    ? [{ opacity: 1, transform: 'scale(1)', filter: 'blur(0px)' }, { opacity: 0, transform: `scale(${s})`, filter: `blur(${MOTION.blurSmallPx}px)` }]
    : [{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: `scale(${s})` }]
  const timing: KeyframeAnimationOptions = { duration: MOTION.iconSwapMs, easing: EASE_CSS.inOut }
  ghost.setAttribute('d', fromD)
  ghost.animate(out, { ...timing, fill: 'forwards' }).finished.then(
    () => ghost.setAttribute('d', ''),
    () => ghost.setAttribute('d', ''),
  )
  main.animate([...out].reverse(), timing)
}

export const StateIcon = React.memo(function StateIcon({ icon, alt = false, spring, label, className, ...rest }: StateIconProps) {
  const node: IconNode = (alt ? PARTNERS[icon] : undefined) ?? resolveIcon(icon)
  const [initialD] = React.useState(() => iconD(node))
  const root = React.useRef<SVGSVGElement>(null)
  const main = React.useRef<SVGPathElement>(null)
  const ghost = React.useRef<SVGPathElement>(null)
  const drv = React.useRef<Morph | null>(null)
  const last = React.useRef<IconNode>(node)
  const lastSwitch = React.useRef(0)
  useMotionTier()

  React.useLayoutEffect(() => {
    drv.current = createMorph(main.current!, last.current as never, { reducedMotion: 'user' })
    return () => {
      drv.current?.destroy()
      drv.current = null
    }
  }, [])

  React.useLayoutEffect(() => {
    const from = last.current
    const m = drv.current
    if (!m || from === node) return
    last.current = node
    const now = performance.now()
    if (lastSwitch.current > 0) {
      const dt = now - lastSwitch.current
      if (dt < PERF_UI.icons.minSwitchIntervalMs) PERF_UI.icons.minSwitchIntervalMs = dt
    }
    lastSwitch.current = now
    const tier = getMotionTier()
    if (tier === 'reduced' || tier === 'off') {
      m.set(node as never)
      return
    }
    const pairSpring = morphSpring(from, node)
    const useSpring: SpringName = spring ?? pairSpring ?? 'snappy'
    if (tier === 'full' && pairSpring && morphBudget.acquire(MOTION.springSettleMs[useSpring])) {
      const el = root.current
      if (el) {
        el.dataset.morphing = ''
        setTimeout(() => {
          if (last.current === node) delete el.dataset.morphing
        }, MOTION.springSettleMs[useSpring])
      }
      m.morphTo(node as never, SPRING_OPTS[useSpring])
      return
    }
    m.set(node as never)
    if (ghost.current && main.current) {
      playSwap(ghost.current, main.current, iconD(from), tier === 'full' && motionBudget.acquireBlur(MOTION.iconSwapMs))
    }
  }, [node, spring])

  return (
    <svg
      ref={root}
      data-icon=""
      data-state-icon=""
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? 'img' : undefined}
      aria-hidden={label ? undefined : true}
      className={cn('icon', className)}
      {...rest}
    >
      {label ? <title>{label}</title> : null}
      <path ref={ghost} d="" />
      <path ref={main} d={initialD} />
    </svg>
  )
})
