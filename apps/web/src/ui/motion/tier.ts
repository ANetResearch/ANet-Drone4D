// Motion tier resolver (M15-FR-052; AWR-15 §8.4; ADR-029, ADR-041): effective = min(OS preference, user setting,
// PerfGovernor motionCap) with full > lite > reduced; Tier S starts at lite and the governor input is lite until the
// render tier is known. `off` exists only in dev/test builds (?motion=off) and overrides everything; it is reported as
// reduced in __perf.ui.motionTier (the awr.perf.v1 enum has three values) and as off in __ux.motion.
// The result is written to <html data-motion>; React reads it with useMotionTier() (useSyncExternalStore).
import { useSyncExternalStore } from 'react'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { PERF_UI } from '@/ui/shell/perfUi'
import { UX } from '@/ui/testing/uxProbe'

export type MotionTier = 'full' | 'lite' | 'reduced' | 'off'
export type MotionSource = 'os' | 'user' | 'governor' | 'test'
export type UserMotion = 'system' | 'lite' | 'reduced'
type RunTier = Exclude<MotionTier, 'off'>
const RANK: Readonly<Record<MotionTier, number>> = { full: 3, lite: 2, reduced: 1, off: 0 }

const mq = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null
let os: RunTier = mq?.matches ? 'reduced' : 'full'
let user: RunTier = 'full'
let gov: RunTier = 'lite'
let test: MotionTier | null = null
let current: MotionTier = 'lite'
let source: MotionSource = 'governor'
const listeners = new Set<() => void>()

export function parseMotionParam(search: string): MotionTier | null {
  if (!TEST_SWITCHES) return null
  const v = new URLSearchParams(search).get('motion')
  return v === 'off' || v === 'reduced' || v === 'lite' || v === 'full' ? v : null
}

export function resolveTier(inputs: { os: RunTier; user: RunTier; gov: RunTier; test: MotionTier | null }): { tier: MotionTier; source: MotionSource } {
  if (inputs.test) return { tier: inputs.test, source: 'test' }
  let tier: MotionTier = 'full'
  let src: MotionSource = 'os'
  for (const [s, t] of [['os', inputs.os], ['user', inputs.user], ['governor', inputs.gov]] as const) {
    if (RANK[t] < RANK[tier]) {
      tier = t
      src = s
    }
  }
  return { tier, source: src }
}

function apply() {
  const r = resolveTier({ os, user, gov, test })
  const changed = r.tier !== current || r.source !== source
  current = r.tier
  source = r.source
  if (typeof document !== 'undefined') document.documentElement.dataset.motion = current
  PERF_UI.motionTier = current === 'off' ? 'reduced' : current
  UX.motion.tier = current
  UX.motion.source = source
  if (changed) for (const l of listeners) l()
}

export const userMotionToTier = (u: UserMotion): RunTier => (u === 'system' ? 'full' : u)
export function setUserMotion(u: UserMotion): void {
  user = userMotionToTier(u)
  apply()
}
/** PerfGovernor input: stores/perf.motionCap (null until the render tier is known: lite) */
export function setGovernorMotion(cap: RunTier | null): void {
  gov = cap ?? 'lite'
  apply()
}
export function setTestMotion(t: MotionTier | null): void {
  test = TEST_SWITCHES ? t : null
  apply()
}

let started = false
export function initMotionTier(search = typeof location !== 'undefined' ? location.search : ''): void {
  if (started) return
  started = true
  test = parseMotionParam(search)
  mq?.addEventListener('change', () => {
    os = mq.matches ? 'reduced' : 'full'
    apply()
  })
  apply()
}

export const getMotionTier = (): MotionTier => current
export const getMotionSource = (): MotionSource => source
const subscribe = (cb: () => void) => {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}
export function useMotionTier(): MotionTier {
  return useSyncExternalStore(subscribe, getMotionTier, getMotionTier)
}
export function useMotionSource(): MotionSource {
  return useSyncExternalStore(subscribe, getMotionSource, getMotionSource)
}
