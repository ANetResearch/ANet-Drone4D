// Viewport layer registry (M06 §6.7, FR-025; AWR-03 §4.3). Owner: M06. Layer adapters viewport/layers/<layer>.tsx belong
// to the layer's module (pointcloud M05, environment M07, the rest M06) and register a LayerSpec here; WorldCanvas mounts
// each root under WorldRoot (its objects on the layer's channel), the render backend sums drawCount() into the pass plan,
// the shader zoo collects warmupVariants(), the PerfGovernor collects knobs, and device loss calls onBackendLost/Ready.
// Channels (three Layers): CH_MAIN 0, CH_CLOUD 1, CH_PICK 2, CH_BENCH 3. The point cloud always lives on CH_CLOUD
// (Tier S renders MAIN | CLOUD in one pass; Tier B/A renders CLOUD into cloudRT first).
import type { Object3D, RenderTarget } from 'three'
import type { FrameCtx, PerfLayerId, RenderBackendView, RTName } from '@/engine'
import type { GovernorKnob } from '@/engine'
import { checkLayerRoot, GUARDS_ON } from '../backend/guards'

export type LayerId = 'pointcloud' | 'drones' | 'trails' | 'frustums' | 'mission' | 'zones' | 'environment' | 'groundSky' | 'labels' | 'glyphs' | 'debug'
export const CH_MAIN = 0
export const CH_CLOUD = 1
export const CH_PICK = 2
export const CH_BENCH = 3

export interface LayerCaps { maxInstances?: number; maxQuads?: number; maxSegments?: number; maxLabels?: number }
/** a shader-zoo entry: the object is rendered (made drawable by before()) to every target it uses at run time */
export type WarmTarget = 'screen' | RTName
export interface WarmupItem {
  object: Object3D
  targets?: readonly WarmTarget[]
  /** make the object drawable (instance count >= 1, visible); after() restores the live state */
  before?(): void
  after?(): void
}
/** point-cloud services of M05 exposed to M06 through its LayerSpec (M05 §7.1-§7.2); all optional until delivered */
export interface PointCloudServices {
  cas?: import('@/engine').CasHandle | null
  edlMaterial?: EdlCompositeLike | null
  dtm?: { sample(x: number, y: number): number } | null
  prefetchView?(eye: Float64Array, target: Float64Array, fovYRad: number): void
  setFocus?(p: Float64Array | null, mode: 'none' | 'follow' | 'fpv'): void
}
/** M05 EdlCompositeMaterial shape (M05 §7.2) */
export interface EdlCompositeLike {
  readonly uniforms: { uvScale: { value: number }; strength?: { value: number }; taps?: { value: number } }
  bindTargets(color: RenderTarget['texture'], depth: NonNullable<RenderTarget['depthTexture']>): void
  setBackgroundNode?(fn: unknown): void
}

export interface LayerSpec {
  id: LayerId
  owner: 'M05' | 'M06' | 'M07' | 'M10' | 'M13'
  perfKey: PerfLayerId
  /** mounted under WorldRoot (layer frame = world ENU); null for DOM layers */
  root: Object3D | null
  /** 0 CH_MAIN, 1 CH_CLOUD, 2 CH_PICK */
  channel: 0 | 1 | 2
  caps?: { S: LayerCaps; BA: LayerCaps }
  /** draw calls this layer produces in the current frame (0 when invisible); summed into the pass plan */
  drawCount(ctx: FrameCtx): number
  warmupVariants?(be: RenderBackendView): WarmupItem[]
  setVisible(v: boolean): void
  onBackendLost?(): void
  onBackendReady?(be: RenderBackendView): void
  knobs?: GovernorKnob[]
  /** M05 only: CAS, EDL composite, DTM sampler and prefetch (M05 §7.1) */
  services?: PointCloudServices
  dispose(): void
}

const layers = new Map<LayerId, LayerSpec>()
const listeners = new Set<() => void>()
const owners = new Map<unknown, Object3D>()
/** cached list (rebuilt on registration changes only: the render phase iterates it every frame) */
let list: LayerSpec[] = []

export function registerLayer(spec: LayerSpec): () => void {
  if (GUARDS_ON) checkLayerRoot(spec.root, owners, spec.id)
  layers.set(spec.id, spec)
  list = [...layers.values()]
  for (const l of listeners) l()
  return () => {
    if (layers.get(spec.id) === spec) layers.delete(spec.id)
    list = [...layers.values()]
    if (spec.root) spec.root.traverse((o) => {
      const m = (o as { material?: unknown }).material
      owners.delete(m)
      owners.delete((o as { geometry?: unknown }).geometry)
    })
    for (const l of listeners) l()
  }
}
export const listLayers = (): readonly LayerSpec[] => list
export const getLayer = (id: LayerId): LayerSpec | undefined => layers.get(id)
export function onLayersChanged(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}
/** effective channel: the point cloud is CH_CLOUD on every tier (M06 §6.7) */
export function channelOf(s: LayerSpec): number {
  return s.id === 'pointcloud' ? CH_CLOUD : s.channel
}
/** planned draw calls of the frame (M06 §6.5): sum of the visible layers */
export function plannedDraws(ctx: FrameCtx): number {
  let n = 0
  for (let i = 0; i < list.length; i++) n += list[i].drawCount(ctx)
  return n
}
/** point-cloud services (M05), null until the point-cloud layer registers them */
export function pointCloudServices(): PointCloudServices | null {
  return layers.get('pointcloud')?.services ?? null
}
