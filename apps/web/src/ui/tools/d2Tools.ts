// D2 viewport tools (AWR-14 §20.7.2, §20.8, §20.6.4; UX-FR-117, UX-FR-120, UX-FR-125): TARGET_PLACE (click places a
// target of the chosen class), TARGET_MOVE (click moves the selected target), TARGET_PATH (clicks add path points, Enter
// commits, Backspace removes the last point), TARGET_REGION (clicks add region vertices, Enter or a double click closes
// the ring; a self-intersecting ring is refused), NEST_PLACE and VEHICLE_PLACE. Clicks come through the viewport's click
// consumer (camera controls still see the whole press) and land on the ground with M06's ray_hit pick (pick.pickAt);
// the drawn path or region is shown with the mission overlay's draft (mission.setDraft), the click point with the GoTo
// preview marker. Esc ends the tool (tool scope, below manual control). The D1 GoTo and add-vehicle tools are left first.
// Task drawing (AWR-14 §20.9.1, §20.9.2; UX-FR-126): TASK_POINT (one click), TASK_LINE (clicks add waypoints, Enter or a
// double click ends, >= 2), TASK_AREA (clicks add vertices, Enter or a double click closes, self-intersecting rings are
// refused) and TASK_PICK_TARGET (one click, the page takes the nearest target); lines and rings stop at 64 points (the
// server quota, code 585), and the point count of a drawing is shown by the page.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { mission, pick } from '@/viewport/facade'
import { registerHotkey } from '@/ui/hotkeys/registry'
import { selfIntersects } from '@/ui/panels/mission-edit/editModel'
import { toolMode } from './toolMode'

export type D2ToolMode = 'TARGET_PLACE' | 'TARGET_MOVE' | 'TARGET_PATH' | 'TARGET_REGION' | 'NEST_PLACE' | 'VEHICLE_PLACE'
  | 'TASK_POINT' | 'TASK_LINE' | 'TASK_AREA' | 'TASK_PICK_TARGET'
/** most points of a drawn line or ring (AWR-17 §17.6.7: 64 waypoints or vertices) */
export const DRAW_MAX_POINTS = 64
export type Pt = readonly [number, number, number]
export interface D2ToolState {
  mode: D2ToolMode | null
  /** points of a path or region being drawn (ENU m) */
  points: readonly Pt[]
  /** what the tool is for (target class, target id, ...) */
  subject: string | null
  version: number
}
export const d2ToolStore = createAwrStore<D2ToolState>('ui.d2tool', () => ({ mode: null, points: [], subject: null, version: 0 }))
export const useD2Tool = <T,>(sel: (s: D2ToolState) => T): T => useStore(d2ToolStore, sel)
const set = (p: Partial<D2ToolState>): void => d2ToolStore.setState({ ...p, version: d2ToolStore.getState().version + 1 })

type Done = (pts: readonly Pt[]) => void
let onDone: Done | null = null
let offKeys: (() => void)[] = []

const isLine = (m: D2ToolMode | null): boolean => m === 'TARGET_PATH' || m === 'TASK_LINE'
const isRing = (m: D2ToolMode | null): boolean => m === 'TARGET_REGION' || m === 'TASK_AREA'
const isDraw = (m: D2ToolMode | null): boolean => isLine(m) || isRing(m)

function draft(): void {
  const s = d2ToolStore.getState()
  if (!isDraw(s.mode) || s.points.length === 0) {
    mission.setDraft(null)
    return
  }
  const route = isLine(s.mode) ? s.points.flatMap((p) => [p[0], p[1], p[2]]) : []
  const area = isRing(s.mode) ? s.points.flatMap((p) => [p[0], p[1]]) : null
  mission.setDraft({ route, bad: null, sel: -1, area, areaClosed: false, preview: [] })
}

/** the region ring is closable: at least 3 vertices and no self intersection (pure; tests) */
export function regionClosable(points: readonly Pt[]): boolean {
  if (points.length < 3) return false
  const xy = points.map((p) => [p[0], p[1]] as [number, number])
  return !selfIntersects(xy)
}

async function onClick(kind: 'click' | 'dblclick', x: number, y: number): Promise<void> {
  const s = d2ToolStore.getState()
  if (!s.mode) return
  if (kind === 'dblclick' && (s.mode === 'TARGET_REGION' || s.mode === 'TASK_AREA' || s.mode === 'TASK_LINE')) {
    d2Tools.commit()
    return
  }
  if (isDraw(s.mode) && s.mode?.startsWith('TASK_') && s.points.length >= DRAW_MAX_POINTS) {
    toolMode.flash('tool.d2.maxPoints')
    return
  }
  const r = await pick.pickAt(x, y, { want: ['ground'] })
  if (r.kind !== 'ground') {
    toolMode.flash('tool.noGround')
    return
  }
  const p: Pt = [r.pointEnu[0], r.pointEnu[1], r.pointEnu[2]]
  const cur = d2ToolStore.getState()
  if (cur.mode !== s.mode) return
  if (isDraw(cur.mode)) {
    // the two clicks of a double click land on the same spot: no zero-length edge
    const last = cur.points[cur.points.length - 1]
    if (last && Math.hypot(last[0] - p[0], last[1] - p[1]) < 0.5) return
    set({ points: [...cur.points, p] })
    draft()
    return
  }
  mission.setGotoPreview(Float64Array.from(p))
  const cb = onDone
  d2Tools.end()
  cb?.([p])
}

export const d2Tools = {
  /** start a tool; `done` receives the clicked point (place, move) or the drawn points (path, region) */
  start(mode: D2ToolMode, subject: string | null, done: Done): void {
    d2Tools.end()
    toolMode.done()
    onDone = done
    set({ mode, subject, points: [] })
    pick.setClickConsumer((kind, x, y) => {
      if (!d2ToolStore.getState().mode) return false
      void onClick(kind, x, y)
      return true
    })
    const active = () => d2ToolStore.getState().mode !== null
    offKeys = [
      registerHotkey({ id: 'd2tool.escape', combo: 'Escape', scope: 'tool', group: 'tool', labelKey: 'hotkey.escape', when: active, deniedKey: () => null, run: () => d2Tools.end() }),
      registerHotkey({ id: 'd2tool.enter', combo: 'Enter', scope: 'tool', group: 'tool', labelKey: 'tool.d2.commit', when: () => isDraw(d2ToolStore.getState().mode), deniedKey: () => null,
        run: () => d2Tools.commit() }),
      registerHotkey({ id: 'd2tool.back', combo: 'Backspace', scope: 'tool', group: 'tool', labelKey: 'tool.d2.undo', when: () => isDraw(d2ToolStore.getState().mode), deniedKey: () => null,
        run: () => d2Tools.undo() }),
    ]
    toolMode.flash(`tool.d2.${mode}`)
  },
  /** remove the last drawn point */
  undo(): void {
    const s = d2ToolStore.getState()
    if (!s.points.length) return
    set({ points: s.points.slice(0, -1) })
    draft()
  },
  /** finish a path (>= 2 points) or close a region (closable ring) */
  commit(): boolean {
    const s = d2ToolStore.getState()
    if (isLine(s.mode) && s.points.length < 2) return false
    if (isRing(s.mode) && !regionClosable(s.points)) {
      toolMode.flash('tool.d2.selfIntersect')
      return false
    }
    const cb = onDone
    const pts = s.points
    d2Tools.end()
    cb?.(pts)
    return true
  },
  end(): void {
    for (const off of offKeys) off()
    offKeys = []
    onDone = null
    pick.setClickConsumer(null)
    if (d2ToolStore.getState().mode !== null) {
      mission.setDraft(null)
      set({ mode: null, points: [], subject: null })
    }
  },
}
