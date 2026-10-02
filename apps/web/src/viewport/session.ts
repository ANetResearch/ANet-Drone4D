// Viewport session state (M06 §6.6, §7.1). Owner: M06.
// One per page: the RenderBackend, the WorldRoot (rotation.x = -pi/2 maps ENU to three (E, U, -N), ADR-002), the camera
// rig, the drone runtime, the mission overlay, zones, labels, the picker and the current ground pick, plus the world
// context the viewport needs (coordinate ground height, bounds, zones). React adapters (WorldCanvas, layers/*.tsx,
// overlay/*.tsx) fill it; the facade (facade.ts) reads it for ui/**. useVp() subscribes React to its changes.
import { useSyncExternalStore } from 'react'
import { Group, type PerspectiveCamera, type Scene } from 'three'
import type { CameraRig, DroneRuntime, LabelLayer, MissionOverlay, OpenedWorldInfo, Picker, SensorsApi, ZonesLayer } from '@/engine'
import type { RenderBackend } from './renderer'

export interface GroundPick {
  worldId: string
  /** surface point returned by ray_hit (ENU m) */
  surface: Float64Array
  surfaceKind: string
  atMs: number
}

/** point-cloud pick of a click (M05 PointPick subset, PRD-FR-017) */
export interface PointPickInfo {
  worldId: string
  pointEnu: Float64Array
  classIdx: number
  className: string
  hagM: number | null
  spacingM: number
  atMs: number
}

/** world context read from world.json / coordinate.json (M06 §7.3) */
export interface WorldContext {
  worldId: string
  contentVersion: string
  groundZ: number
  reliefLow: number
  boundsMin: [number, number, number] | null
  boundsMax: [number, number, number] | null
  coordinateBytes: ArrayBuffer | null
}

type Listener = () => void

class ViewportSession {
  be: RenderBackend | null = null
  readonly worldRoot = new Group()
  scene: Scene | null = null
  camera: PerspectiveCamera | null = null
  host: HTMLElement | null = null
  rig: CameraRig | null = null
  drones: DroneRuntime | null = null
  mission: MissionOverlay | null = null
  zones: ZonesLayer | null = null
  labels: LabelLayer | null = null
  picker: Picker | null = null
  sensors: SensorsApi | null = null
  world: OpenedWorldInfo | null = null
  worldCtx: WorldContext | null = null
  /** world id currently shown by the viewport (the route's id) */
  worldId: string | null = null
  /**
   * static browsing (M06-FR-030; AWR-10 AD-01): the route's world differs from the realtime session's world, so the
   * simulation channels are not subscribed and the drone layer and drone picking are off
   */
  staticBrowse = false
  pick: GroundPick | null = null
  /** last point-cloud point under a click (M06-FR-064, PRD-FR-017; D1-ext): ENU, class, HAG for the info card */
  pointPick: PointPickInfo | null = null
  cssW = 0
  cssH = 0
  /** rebuild generation of the canvas (device loss, viewport.rebuild) */
  generation = 0
  version = 0
  private readonly listeners = new Set<Listener>()

  constructor() {
    this.worldRoot.name = 'WorldRoot'
    this.worldRoot.rotation.x = -Math.PI / 2
    this.worldRoot.updateMatrixWorld(true)
  }

  /** notify React adapters and the facade subscribers (pick, backend, world changes) */
  changed(): void {
    this.version++
    for (const l of this.listeners) l()
  }
  subscribe(l: Listener): () => void {
    this.listeners.add(l)
    return () => {
      this.listeners.delete(l)
    }
  }
  /** ground height for clamps and patches: M05 dtm.sample when registered, else coordinate ground.zM */
  groundAt(x: number, y: number, dtm: ((x: number, y: number) => number) | null): number {
    if (dtm) {
      const z = dtm(x, y)
      if (Number.isFinite(z)) return z
    }
    return this.worldCtx?.groundZ ?? 0
  }
}

export const vp = new ViewportSession()

/** React: re-render when the session changes */
export function useVp<T>(sel: (s: typeof vp) => T): T {
  return useSyncExternalStore((cb) => vp.subscribe(cb), () => sel(vp), () => sel(vp))
}
