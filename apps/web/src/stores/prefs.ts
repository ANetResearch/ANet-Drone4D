// View preferences (M15-FR-019, FR-032, §7.1.2; AWR-14 §3.6). Owner: M15. Two persisted slices: awr.ui.layout.v1 and
// awr.ui.prefs.v1. Every read and write is fault tolerant (lib/persist.ts); numbers are clamped on read; writes are
// debounced 500 ms and flushed when the page is hidden. No Sidebar cookie and no useDefaultLayout storage.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { clampNum, loadSlice, saveSliceDebounced } from '@/lib/persist'
import { LAYOUT } from '@/lib/tokens/input.gen'

export type CameraModeId = 'orbit' | 'free' | 'third' | 'fpv' | 'bird'
export interface CameraPoseV1 { mode: CameraModeId; eye_enu_m: [number, number, number]; target_enu_m: [number, number, number]; fov_deg: number }
export type DockSlot = 'left' | 'right' | 'bottom'

export interface LayoutPrefsV1 {
  v: 1
  preset: 'demo' | 'debug' | 'replay' | 'edit'
  left: { open: boolean; width: number }
  right: { open: boolean; width: number; page: 'list' | 'detail' | 'edit' }
  dock: { open: boolean; height: number; tab: string }
  panels: Record<string, { slot: DockSlot; order: number }>
  hud: { open: boolean }
}
export interface UiPrefsV1 {
  v: 1
  motion: 'system' | 'lite' | 'reduced'
  altRef: 'AGL' | 'MSL'
  coord: 'enu' | 'lla'
  labels: boolean
  lastWorld: string | null
  camByWorld: Record<string, { mode: 'orbit' | 'free' | 'bird'; pose: CameraPoseV1 }>
  locale: 'zh-CN' | 'en'
}
export interface PrefsState { layout: LayoutPrefsV1; ui: UiPrefsV1 }

export const LAYOUT_KEY = 'awr.ui.layout.v1'
export const PREFS_KEY = 'awr.ui.prefs.v1'

export const DEFAULT_LAYOUT: LayoutPrefsV1 = {
  v: 1, preset: 'demo',
  left: { open: true, width: LAYOUT.leftDefaultPx },
  right: { open: true, width: LAYOUT.rightDefaultPx, page: 'list' },
  dock: { open: false, height: LAYOUT.dockDefaultPx, tab: 'events' },
  panels: {}, hud: { open: true },
}
export const DEFAULT_UI: UiPrefsV1 = {
  v: 1, motion: 'system', altRef: 'AGL', coord: 'enu', labels: true, lastWorld: null, camByWorld: {}, locale: 'zh-CN',
}

const WORLD_ID = /^[a-z0-9-]{1,63}$/
export function clampLayout(x: LayoutPrefsV1): LayoutPrefsV1 {
  const maxDock = Math.max(LAYOUT.dockMinPx, Math.round((globalThis.innerHeight || 1080) * LAYOUT.dockMaxFrac))
  const presets = ['demo', 'debug', 'replay', 'edit'] as const
  const pages = ['list', 'detail', 'edit'] as const
  return {
    v: 1,
    preset: presets.includes(x.preset) ? x.preset : 'demo',
    left: { open: x.left?.open !== false, width: clampNum(x.left?.width, LAYOUT.leftMinPx, LAYOUT.leftMaxPx, LAYOUT.leftDefaultPx) },
    right: {
      open: x.right?.open !== false, width: clampNum(x.right?.width, LAYOUT.rightMinPx, LAYOUT.rightMaxPx, LAYOUT.rightDefaultPx),
      page: pages.includes(x.right?.page) ? x.right.page : 'list',
    },
    dock: {
      open: x.dock?.open === true, height: clampNum(x.dock?.height, LAYOUT.dockMinPx, maxDock, LAYOUT.dockDefaultPx),
      tab: typeof x.dock?.tab === 'string' ? x.dock.tab : 'events',
    },
    panels: x.panels && typeof x.panels === 'object' ? x.panels : {},
    hud: { open: x.hud?.open !== false },
  }
}
export function clampUi(x: UiPrefsV1): UiPrefsV1 {
  return {
    v: 1,
    motion: x.motion === 'lite' || x.motion === 'reduced' ? x.motion : 'system',
    altRef: x.altRef === 'MSL' ? 'MSL' : 'AGL',
    coord: x.coord === 'lla' ? 'lla' : 'enu',
    labels: x.labels !== false,
    lastWorld: typeof x.lastWorld === 'string' && WORLD_ID.test(x.lastWorld) ? x.lastWorld : null,
    camByWorld: x.camByWorld && typeof x.camByWorld === 'object' ? x.camByWorld : {},
    locale: 'zh-CN', // D1: zh-CN is the only locale (M15-FR-102)
  }
}

/** true when a layout was restored from localStorage (breakpoint defaults then apply only on later crossings) */
export const layoutRestored: boolean = (() => {
  try {
    return globalThis.localStorage?.getItem(LAYOUT_KEY) != null
  } catch {
    return false
  }
})()

export const prefsStore = createAwrStore<PrefsState>('prefs', () => ({
  layout: loadSlice(LAYOUT_KEY, 1, DEFAULT_LAYOUT, clampLayout),
  ui: loadSlice(PREFS_KEY, 1, DEFAULT_UI, clampUi),
}))

const layoutWriter = saveSliceDebounced(LAYOUT_KEY, () => prefsStore.getState().layout)
const uiWriter = saveSliceDebounced(PREFS_KEY, () => prefsStore.getState().ui)

type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] }

export const prefs = {
  setLayout(patch: DeepPartial<LayoutPrefsV1>, _reason: 'user' | 'breakpoint' | 'preset' = 'user'): void {
    const cur = prefsStore.getState().layout
    const next = clampLayout({
      ...cur, ...(patch as Partial<LayoutPrefsV1>),
      left: { ...cur.left, ...patch.left }, right: { ...cur.right, ...patch.right } as LayoutPrefsV1['right'],
      dock: { ...cur.dock, ...patch.dock }, hud: { ...cur.hud, ...patch.hud },
      panels: { ...cur.panels, ...(patch.panels as LayoutPrefsV1['panels'] | undefined) },
    })
    // slices whose fields did not change keep their identity: a rail page change no longer re-renders the left rail and
    // the Dock that subscribe to their own slice (P4-UI, D1-AC-25)
    const stable: LayoutPrefsV1 = {
      ...next,
      left: shallowEq(next.left, cur.left) ? cur.left : next.left,
      right: shallowEq(next.right, cur.right) ? cur.right : next.right,
      dock: shallowEq(next.dock, cur.dock) ? cur.dock : next.dock,
      hud: shallowEq(next.hud, cur.hud) ? cur.hud : next.hud,
      panels: shallowEq(next.panels, cur.panels) ? cur.panels : next.panels,
    }
    prefsStore.setState({ layout: stable })
    layoutWriter.schedule()
  },
  setUi<K extends keyof UiPrefsV1>(k: K, v: UiPrefsV1[K]): void {
    const next = clampUi({ ...prefsStore.getState().ui, [k]: v })
    prefsStore.setState({ ui: next })
    uiWriter.schedule()
  },
  resetLayout(): void {
    prefsStore.setState({ layout: clampLayout(DEFAULT_LAYOUT) })
    layoutWriter.schedule()
  },
  flush(): void {
    layoutWriter.flush()
    uiWriter.flush()
  },
}

function shallowEq(a: object, b: object): boolean {
  const ka = Object.keys(a)
  if (ka.length !== Object.keys(b).length) return false
  for (const k of ka) if ((a as Record<string, unknown>)[k] !== (b as Record<string, unknown>)[k]) return false
  return true
}

export function usePrefs<T>(sel: (s: PrefsState) => T): T {
  return useStore(prefsStore, sel)
}
