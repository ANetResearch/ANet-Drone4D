// Point cloud engine types (M05 §7.1; AWR-16 §3.2, §4.9). Owner: M05.
export type ColorMode = 'height' | 'hag' | 'normal' | 'class' | 'source' | 'intensity'
export const COLOR_MODES: readonly ColorMode[] = ['height', 'hag', 'normal', 'class', 'source', 'intensity']
export type LimitedBy = 'budget' | 'nodes' | 'headroom' | 'error' | 'complete'
export type EnginePhase = 'idle' | 'manifest' | 'first_screen' | 'streaming' | 'suspended' | 'error'
export type RungIndex = 0 | 1 | 2 | 3 | 4 | 5 | 6
export type FocusMode = 'none' | 'follow' | 'fpv'

/** world.json subset read by the engine (AWR-16 §3.2) */
export interface WorldJson {
  schemaVersion: string
  id: string
  name?: string
  contentVersion: string
  layers: {
    id: string
    type: string
    role: string
    format: string
    href: string
    status: string
    default: boolean
    roots?: { name: string; href: string; cubeMin: [number, number, number]; cubeSize: number; points: number; depth: number; firstScreenBytes: number }[]
    T_world_layer?: number[][]
  }[]
  render?: { defaultColorMode?: ColorMode; zRangeM?: [number, number]; hagRangeM?: [number, number]; syntheticGroundZ?: number | null }
  camera?: { home?: { position: [number, number, number]; target: [number, number, number]; fovDeg?: number } }
  bounds?: { min: [number, number, number]; max: [number, number, number] }
}

/** metadata.json subset (Potree 2.0 + anet v1, AWR-16 §4.9) */
export interface PotreeMeta {
  version: string
  name: string
  points: number
  hierarchy: { firstChunkSize: number; stepSize: number; depth: number }
  offset: [number, number, number]
  scale: [number, number, number]
  spacing: number
  boundingBox: { min: [number, number, number]; max: [number, number, number] }
  encoding: string
  attributes: { name: string; size: number; numElements: number; elementSize: number; type: string }[]
  anet: {
    formatVersion: number
    frame: string
    streams: string[]
    bytesPerPoint: 12 | 16
    compression: 'none' | 'gzip'
    pointOrder: string
    nodeCount: number
    levelsByteEnd: number[]
    levelsPoints: number[]
    levelsNodes: number[]
    firstScreenLevel: number
    tightBounds: { min: [number, number, number]; max: [number, number, number] }
    hierarchyExt: { href: string; recordSize: number; content: string } | null
    stats: { zP1: number; zP99: number; hagP1: number; hagP99: number; nnMedianM: number }
    root?: { forestIndex: number; forestSize: number }
  }
}

export interface CoordinateJson {
  anchor: { kind: string; georeferenced: boolean }
  trueNorth?: { yawOffsetDeg?: number; confidence?: string }
  ground?: { type?: string; zM?: number; dtm?: { href: string; cellM: number } }
}

/** grid sidecar (AWR-16 §6.2) */
export interface GridSidecar {
  schemaVersion: string
  kind: string
  dtype: 'float32' | 'float16' | 'uint8' | 'uint16'
  href: string
  width: number
  height: number
  cellM: number
  originXY: [number, number]
  rowOrder: 'south-to-north'
  valueFrame: string
  scale?: number
  offset?: number
}

export interface OpenedWorldInfo {
  worldId: string
  contentVersion: string
  roots: number
  nodes: number
  points: number
  firstScreenLevel: number
  firstScreenBytes: number
  anchorKind: string
  /** honesty facts (AWR-03 §5.1 rule 3; M16-FR-006): true-north confidence and the synthetic ground height (null = none) */
  northConfidence: string | null
  syntheticGroundZ: number | null
  /** tight box of all roots in the layer frame: min xyz, max xyz */
  bboxEnuM: Float64Array
  defaultColorMode: ColorMode
  home: { position: [number, number, number]; target: [number, number, number]; fovDeg: number } | null
}

/** preallocated statistics (M05 §7.1); the layer adapter copies the scalars into stores/world at 4 Hz */
export interface PointCloudStats {
  phase: EnginePhase
  progress: number
  drawn: number
  B: number
  Beff: number
  lo: number
  hi: number
  Bfloor: number
  rungIndex: number
  rungName: string
  manual: boolean
  limitedBy: LimitedBy
  achievedErrPx: number
  fillRate: number
  inflight: number
  queued: number
  failed: number
  canceled: number
  residentPts: number
  cpuCacheBytes: number
  pendingUploadPts: number
  clampedByCapacity: boolean
  floorHeld: boolean
  poolStalls: number
  pageUtil: number
  maxPxEff: number
  rsEff: number
  frozenMask: number
  levelCounts: Int32Array
  selectMs: number
  minSpacingM: number
  downloadedBytes: number
  uniqueBytes: number
  uploadPtsMax: number
  error: { code: number; message: string } | null
}

/** point pick result (D1-ext, M05 §7.1) */
export interface PointPick {
  posEnuM: Float64Array
  classIdx: number
  className: string
  normal: Float64Array | null
  hagM: number | null
  nodeId: number
  nodeName: string
  local: number
  spacingM: number
}

export interface PointCloudEvents {
  /** reason: first open, switch to another world, or reopen after a contentVersion change (keep the camera) */
  'pc.world.opened': { info: OpenedWorldInfo; ttfpMs: number; reason: 'open' | 'switch' | 'stale' }
  'pc.world.error': { code: number; message: string }
  'pc.rung.changed': { from: number; to: number; reason: 'overload' | 'headroom' | 'manual' }
  'pc.capacity.clamped': { clamped: boolean; Beff: number; capacity: number }
  'pc.node.failed': { node: number; code: number }
  'pc.gpu.reset': { recoveredMs: number }
  'pc.first.frame': { ttfpMs: number; switchMs: number }
  /** test builds with ?quality=1: a quality sample pose was recorded (AWR-18 §4.5) */
  'pc.quality.sample': { k: number; t: number }
}
