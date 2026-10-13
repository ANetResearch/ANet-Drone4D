// Draft of the "new task" page (AWR-14 §20.9.1 to §20.9.3; M15-FR-214; UX-FR-126 to UX-FR-129): the chosen type, the drawn
// geometry, the form values and the page phase (TYPE -> DRAWING -> PARAMS, the preview phases come from the tasking store's
// `preview`). Choosing a type starts its drawing tool (ui/tools/d2Tools.ts: TASK_POINT, TASK_LINE, TASK_AREA,
// TASK_PICK_TARGET); while the page shows the parameters the drawn geometry stays in the viewport as the mission overlay's
// draft (closed ring, polyline or a single point). Every geometry or form change after a preview drops the preview (the
// planning layer clears, AWR-14 §20.9.3 "预览作废"). Written on user actions only.
import type { TaskType } from '@/stores/tasking'
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { prefs, prefsStore } from '@/stores/prefs'
import { targetsStore } from '@/stores/targets'
import { targetsModel } from '@/app/shell/engineD2'
import { taskingActions, taskingStore } from '@/stores/tasking'
import { mission } from '@/viewport/facade'
import { d2Tools, type D2ToolMode } from '@/ui/tools/d2Tools'
import { defaultForm, drawKindOf, EMPTY_GEOM, type TaskForm, type TaskGeom, type XYZ } from './taskModel'

export type DraftPhase = 'type' | 'drawing' | 'params'
export interface TaskDraftState {
  type: TaskType | null
  geom: TaskGeom
  form: TaskForm
  phase: DraftPhase
  version: number
}

export const taskDraftStore = createAwrStore<TaskDraftState>('ui.taskDraft', () => ({
  type: null, geom: EMPTY_GEOM, form: defaultForm('area_guard'), phase: 'type', version: 0,
}))
export const useTaskDraft = <T,>(sel: (s: TaskDraftState) => T): T => useStore(taskDraftStore, sel)

const set = (p: Partial<TaskDraftState>): void => taskDraftStore.setState({ ...p, version: taskDraftStore.getState().version + 1 })

const TOOL: Readonly<Record<ReturnType<typeof drawKindOf>, D2ToolMode>> = { point: 'TASK_POINT', line: 'TASK_LINE', area: 'TASK_AREA', target: 'TASK_PICK_TARGET' }

/** the nearest target to a ground point within 40 m (TASK_PICK_TARGET), from the targets store */
export function nearestTarget(p: readonly [number, number, number], maxM = 40): string | null {
  let best: string | null = null
  let bd = maxM
  const consider = (id: string, x: number, y: number) => {
    const d = Math.hypot(x - p[0], y - p[1])
    if (d <= bd) {
      bd = d
      best = id
    }
  }
  const items = targetsStore.getState().items
  for (const it of items) consider(it.id, it.pos[0], it.pos[1])
  // no TargetLite32 rows on the page yet: the configured positions
  if (!items.length) for (const [id, c] of targetsModel.cfgs) if (c.pos && typeof c.pos[0] === 'number' && typeof c.pos[1] === 'number') consider(id, c.pos[0], c.pos[1])
  return best
}

/** show the drawn geometry as the mission overlay's draft while the parameters are edited */
export function showDraft(g: TaskGeom): void {
  if (!g.points.length) {
    mission.setDraft(null)
    return
  }
  if (g.kind === 'area') {
    mission.setDraft({ route: [], bad: null, sel: -1, area: g.points.flatMap((p) => [p[0], p[1]]), areaClosed: true, preview: [] })
    return
  }
  mission.setDraft({ route: g.points.flatMap((p) => [p[0], p[1], p[2]]), bad: null, sel: g.kind === 'point' ? 0 : -1, area: null, areaClosed: false, preview: [] })
}

function dropPreview(): void {
  if (taskingStore.getState().preview) taskingActions.clearPreview()
}

export const taskDraft = {
  /** open the page (right rail page 3), optionally with a type chosen (the drawing starts) */
  open(type?: TaskType): void {
    prefs.setLayout({ right: { open: true, page: 'task' } })
    if (type) taskDraft.chooseType(type)
  },
  /** choose a type: the form gets its defaults (the label is kept) and the drawing tool starts */
  chooseType(type: TaskType): void {
    const s = taskDraftStore.getState()
    const form = { ...defaultForm(type), label: s.form.label }
    dropPreview()
    set({ type, form, geom: { ...EMPTY_GEOM, kind: drawKindOf(type) }, phase: 'drawing' })
    taskDraft.draw()
  },
  /** (re)start the drawing tool of the current type */
  draw(): void {
    const s = taskDraftStore.getState()
    if (!s.type) return
    const kind = drawKindOf(s.type)
    dropPreview()
    set({ phase: 'drawing' })
    d2Tools.start(TOOL[kind], s.type, (pts) => {
      const cur = taskDraftStore.getState()
      if (cur.type !== s.type) return
      if (kind === 'target') {
        const p = pts[0]
        const id = p ? nearestTarget(p) : null
        taskDraft.setGeom({ kind, points: [], target: id ?? cur.geom.target })
        return
      }
      taskDraft.setGeom({ kind, points: pts.map((p) => [p[0], p[1], p[2]] as XYZ), target: null })
    })
  },
  setGeom(geom: TaskGeom): void {
    dropPreview()
    set({ geom, phase: 'params' })
    showDraft(geom)
  },
  /** relay target from the list (no drawing) */
  setTarget(id: string | null): void {
    d2Tools.end()
    taskDraft.setGeom({ kind: 'target', points: [], target: id })
  },
  setForm(patch: Partial<TaskForm>): void {
    dropPreview()
    set({ form: { ...taskDraftStore.getState().form, ...patch } })
  },
  /** back to the type choice (drawing and preview dropped) */
  reset(): void {
    d2Tools.end()
    dropPreview()
    mission.setDraft(null)
    mission.setGotoPreview(null)
    set({ type: null, geom: EMPTY_GEOM, phase: 'type' })
  },
  /** leave the page: the tool, the draft and the preview go away; the rail returns to its list */
  close(): void {
    taskDraft.reset()
    if (prefsStore.getState().layout.right.page === 'task') prefs.setLayout({ right: { page: 'list' } })
  },
}

// test builds: the UI-only specs (no viewport, no ray hit) hand the drawn geometry over directly
if (TEST_SWITCHES && typeof window !== 'undefined') {
  const w = window as unknown as { __uxInject?: Record<string, unknown> }
  w.__uxInject = {
    ...w.__uxInject,
    taskGeom: (points: [number, number, number][], target: string | null = null) => {
      const s = taskDraftStore.getState()
      if (!s.type) return
      d2Tools.end()
      taskDraft.setGeom({ kind: drawKindOf(s.type), points, target })
    },
  }
}
