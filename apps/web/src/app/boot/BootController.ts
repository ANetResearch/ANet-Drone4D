// Boot controller (M15-FR-006, §6.3.3): SHELL -> WARMING -> FIRST_SCREEN -> REVEALED, BOOT_ERROR on a failed backend or
// world. The mask is revealed when every registered gate resolved; D1-MS1 registers the gates 'shell' (first commit of
// the React shell) and 'canvas' (the R3F host mounted and the loop started); M05 adds 'firstFrame' (first frame with
// points committed) and M06 'warmup' (shader zoo done) through addGate/resolveGate without editing this file. On reveal
// the controller calls perf.markReveal() (M06 facade) and writes __ux.boot. After INPUT.bootSlowHintMs without a reveal
// the mask shows the "still loading" hint. The reveal never waits for the WebSocket.
// Test builds accept ?reveal=shell: only the shell and canvas gates count, so UI specs driven by FakeSource can run
// while a renderer or point cloud change is still in progress (never in production builds, TEST_SWITCHES folds).
import { perf } from '@/engine'
import { INPUT } from '@/lib/tokens/input.gen'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { UX } from '@/ui/testing/uxProbe'

const SHELL_ONLY = TEST_SWITCHES && typeof location !== 'undefined' && new URLSearchParams(location.search).get('reveal') === 'shell'

export type BootState = 'SHELL' | 'WARMING' | 'FIRST_SCREEN' | 'REVEALED' | 'BOOT_ERROR'
type Listener = (s: BootState, info: { slow: boolean; error: string | null; phaseKey: string }) => void

const gates = new Map<string, boolean>()
const listeners = new Set<Listener>()
let state: BootState = 'SHELL'
let slow = false
let error: string | null = null
let phaseKey = 'boot.phase.shell'
let slowTimer: ReturnType<typeof setTimeout> | null = null

function emit(): void {
  UX.boot.state = state
  for (const l of listeners) l(state, { slow, error, phaseKey })
}

function evaluate(): void {
  if (state === 'REVEALED' || state === 'BOOT_ERROR') return
  const pending = [...gates.values()].filter((v) => !v).length
  if (gates.size > 0 && pending === 0) {
    state = 'REVEALED'
    if (slowTimer) clearTimeout(slowTimer)
    perf.markReveal()
    UX.boot.revealAt = performance.now()
  } else if (gates.get('shell')) {
    state = gates.has('firstFrame') && gates.get('warmup') ? 'FIRST_SCREEN' : 'WARMING'
    phaseKey = state === 'FIRST_SCREEN' ? 'boot.phase.firstScreen' : 'boot.phase.warming'
  }
  emit()
}

export const boot = {
  start(): void {
    if (slowTimer) return
    for (const g of ['shell', 'canvas']) if (!gates.has(g)) gates.set(g, false)
    slowTimer = setTimeout(() => {
      slow = true
      emit()
    }, INPUT.bootSlowHintMs)
    emit()
  },
  addGate(name: string): void {
    if (SHELL_ONLY) return
    if (state !== 'REVEALED' && !gates.has(name)) gates.set(name, false)
  },
  resolveGate(name: string): void {
    if (!gates.has(name)) return
    gates.set(name, true)
    evaluate()
  },
  fail(reasonKey: string): void {
    state = 'BOOT_ERROR'
    error = reasonKey
    emit()
  },
  retry(): void {
    error = null
    state = 'SHELL'
    evaluate()
  },
  get state(): BootState {
    return state
  },
  subscribe(cb: Listener): () => void {
    listeners.add(cb)
    cb(state, { slow, error, phaseKey })
    return () => {
      listeners.delete(cb)
    }
  },
}
