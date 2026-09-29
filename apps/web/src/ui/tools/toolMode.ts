// Tool state machine (M15-FR-028, §6.9; AWR-14 §6.7, §6.13): IDLE, GOTO_PICK, GOTO_CONFIRM, ADD_PICK, ADD_CONFIRM
// (EDIT_PATH and DRAW_AREA are D1-ext). The pick itself is M06's: a viewport click runs ray_hit and publishes
// pick.ground; while a *_PICK tool is active a new ground pick moves the tool to its CONFIRM state with the surface
// point as the Popover anchor. A shift-click (pointerdown with Shift within INPUT.contextMaxMs before the pick) sends
// GoTo at once with the default parameters. Esc steps back one level (confirm -> pick -> idle). A short hint flash
// ("read-only mode", "no ground under the cursor") is shown in the tool hint bar. Written on user actions only.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { INPUT } from '@/lib/tokens/input.gen'
import { pick, viewport } from '@/viewport/facade'

export type ToolModeId = 'IDLE' | 'GOTO_PICK' | 'GOTO_CONFIRM' | 'ADD_PICK' | 'ADD_CONFIRM'
export interface ToolState {
  mode: ToolModeId
  /** surface point of the pick being confirmed (ENU m) */
  anchor: readonly [number, number, number] | null
  /** vehicle the GoTo is for */
  vehicle: string | null
  /** transient hint key (i18n) with its time, shown for a moment in the hint bar */
  flash: { key: string; atMs: number } | null
  version: number
}
export const toolStore = createAwrStore<ToolState>('ui.tool', () => ({ mode: 'IDLE', anchor: null, vehicle: null, flash: null, version: 0 }))
export const useTool = <T,>(sel: (s: ToolState) => T): T => useStore(toolStore, sel)

function set(p: Partial<ToolState>): void {
  toolStore.setState({ ...p, version: toolStore.getState().version + 1 })
}

/** pure transition table (exported for tests) */
export function toolNext(mode: ToolModeId, ev: 'goto' | 'add' | 'pick' | 'confirm' | 'escape' | 'cameraMoved' | 'cancel'): ToolModeId {
  switch (ev) {
    case 'goto':
      return 'GOTO_PICK'
    case 'add':
      return 'ADD_PICK'
    case 'pick':
      return mode === 'GOTO_PICK' || mode === 'GOTO_CONFIRM' ? 'GOTO_CONFIRM' : mode === 'ADD_PICK' || mode === 'ADD_CONFIRM' ? 'ADD_CONFIRM' : mode
    case 'cameraMoved':
      return mode === 'GOTO_CONFIRM' ? 'GOTO_PICK' : mode
    case 'escape':
      return mode === 'GOTO_CONFIRM' ? 'GOTO_PICK' : mode === 'ADD_CONFIRM' ? 'ADD_PICK' : 'IDLE'
    case 'confirm':
    case 'cancel':
      return 'IDLE'
  }
}

let lastPick: unknown = null
let shiftDownAt = Number.NEGATIVE_INFINITY
type DirectSend = (vehicle: string) => void
let directGoto: DirectSend | null = null

export const toolMode = {
  enterGoto(vehicle: string): void {
    lastPick = pick.ground
    set({ mode: toolNext(toolStore.getState().mode, 'goto'), vehicle, anchor: null })
  },
  enterAdd(): void {
    lastPick = pick.ground
    set({ mode: toolNext(toolStore.getState().mode, 'add'), vehicle: null, anchor: null })
  },
  /** Esc inside the tool chain; returns false when the tool is idle (the next level of the Esc chain runs) */
  escape(): boolean {
    const s = toolStore.getState()
    if (s.mode === 'IDLE') return false
    const next = toolNext(s.mode, 'escape')
    set({ mode: next, anchor: next === 'IDLE' ? null : s.anchor, vehicle: next === 'IDLE' ? null : s.vehicle })
    return true
  },
  done(): void {
    set({ mode: 'IDLE', anchor: null, vehicle: null })
  },
  cameraMoved(): void {
    const s = toolStore.getState()
    const next = toolNext(s.mode, 'cameraMoved')
    if (next !== s.mode) set({ mode: next, anchor: null })
  },
  flash(key: string): void {
    set({ flash: { key, atMs: performance.now() } })
  },
  /** installed by the GoTo tool: sends with the default parameters (shift-click) */
  setDirectGoto(fn: DirectSend | null): void {
    directGoto = fn
  },
}

/** follow the viewport's ground picks while a pick tool is active (App start-up); returns the detach function */
export function installToolWatcher(): () => void {
  const onPointer = (e: PointerEvent) => {
    if (e.shiftKey && e.button === 0) shiftDownAt = performance.now()
  }
  if (typeof window !== 'undefined') window.addEventListener('pointerdown', onPointer, { capture: true })
  const off = viewport.onChange(() => {
    const s = toolStore.getState()
    if (s.mode === 'IDLE') return
    const g = pick.ground
    if (!g || g === lastPick) return
    // the facade builds a fresh view object per read; compare by the surface point
    const prev = lastPick as { surface?: number[] } | null
    lastPick = g
    if (prev?.surface && prev.surface[0] === g.surface[0] && prev.surface[1] === g.surface[1] && prev.surface[2] === g.surface[2]) return
    const shift = performance.now() - shiftDownAt < INPUT.contextMaxMs * 2
    if (s.mode === 'GOTO_PICK' && shift && s.vehicle && directGoto) {
      directGoto(s.vehicle)
      set({ mode: 'IDLE', anchor: null, vehicle: null })
      return
    }
    set({ mode: toolNext(s.mode, 'pick'), anchor: [g.surface[0], g.surface[1], g.surface[2]] })
  })
  return () => {
    off()
    if (typeof window !== 'undefined') window.removeEventListener('pointerdown', onPointer, { capture: true })
  }
}
