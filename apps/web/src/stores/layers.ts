// Layer preferences (M05 §7.5; AWR-14 §3.6, §6.15). Owner: M05. Persisted with the view state (awr.layers.v1, fault
// tolerant, lib/persist.ts). The point cloud layer adapter subscribes (vanilla subscribe, not through React) and calls
// the engine setters; engine/** never imports stores/** (AWR-03 §4.2) and this module does not import the engine.
import { useStore } from 'zustand'
import { createAwrStore } from '@/lib/createStore'
import { clampNum, loadSlice, saveSliceDebounced } from '@/lib/persist'

export type LayerId = 'pointcloud' | 'drones' | 'trails' | 'frustums' | 'mission' | 'zones' | 'environment' | 'labels'
export type ColorMode = 'height' | 'hag' | 'normal' | 'class' | 'intensity' | 'source'
export type QualityMode = 'auto' | 0 | 1 | 2 | 3 | 4 | 5 | 6

/** class bit i = anet-classes@1 index i; 13 and 14 hidden by default; bit 15 (reserved) always 0 (AWR-15 §10.3) */
export const CLASS_MASK_DEFAULT = 0x1fff
export const CLASS_MASK_VALID = 0x7fff

export interface LayersState {
  visible: Record<LayerId, boolean>
  colorMode: ColorMode
  /** true once the user picked a colour mode; otherwise each opened world applies its default (San Francisco: hag) */
  colorModeByUser: boolean
  classMask: number
  edl: boolean
  quality: QualityMode
}

const KEY = 'awr.layers.v1'
interface LayersV1 extends LayersState { v: 1 }
const COLOR_MODES: readonly ColorMode[] = ['height', 'hag', 'normal', 'class', 'intensity', 'source']

const DEFAULTS: LayersState = {
  visible: { pointcloud: true, drones: true, trails: true, frustums: true, mission: true, zones: true, environment: true, labels: true },
  colorMode: 'height', colorModeByUser: false, classMask: CLASS_MASK_DEFAULT, edl: true, quality: 'auto',
}

function clamp(x: LayersV1): LayersV1 {
  const visible = { ...DEFAULTS.visible }
  for (const k of Object.keys(visible) as LayerId[]) if (typeof x.visible?.[k] === 'boolean') visible[k] = x.visible[k]
  const q = x.quality === 'auto' ? 'auto' : (clampNum(x.quality, 0, 6, -1) as number)
  return {
    v: 1, visible, colorMode: COLOR_MODES.includes(x.colorMode) ? x.colorMode : 'height', colorModeByUser: x.colorModeByUser === true,
    classMask: clampNum(x.classMask, 0, CLASS_MASK_VALID, CLASS_MASK_DEFAULT) & CLASS_MASK_VALID, edl: x.edl !== false,
    quality: q === 'auto' || q < 0 ? 'auto' : (Math.round(q) as QualityMode),
  }
}

function initial(): LayersState {
  const { v: _v, ...rest } = loadSlice<LayersV1>(KEY, 1, { v: 1, ...DEFAULTS }, clamp)
  void _v
  return rest
}

export const layersStore = createAwrStore<LayersState>('layers', initial)

const saver = saveSliceDebounced(KEY, () => ({ v: 1, ...layersStore.getState() }))
layersStore.subscribe(() => saver.schedule())

export const layers = {
  setVisible(id: LayerId, on: boolean): void {
    layersStore.setState({ visible: { ...layersStore.getState().visible, [id]: on } })
  },
  setColorMode(m: ColorMode): void {
    layersStore.setState({ colorMode: m, colorModeByUser: true })
  },
  /** world default colour mode (opened world), applied only while the user has not chosen one */
  applyWorldDefault(m: ColorMode): void {
    if (!layersStore.getState().colorModeByUser && layersStore.getState().colorMode !== m) layersStore.setState({ colorMode: m })
  },
  setClassMask(mask: number): void {
    layersStore.setState({ classMask: mask & CLASS_MASK_VALID })
  },
  setClassVisible(index: number, on: boolean): void {
    if (index < 0 || index > 14) return
    const m = layersStore.getState().classMask
    layersStore.setState({ classMask: (on ? m | (1 << index) : m & ~(1 << index)) & CLASS_MASK_VALID })
  },
  setEdl(on: boolean): void {
    layersStore.setState({ edl: on })
  },
  setQuality(q: QualityMode): void {
    layersStore.setState({ quality: q })
  },
}

export function useLayers<T>(selector: (s: LayersState) => T): T {
  return useStore(layersStore, selector)
}
