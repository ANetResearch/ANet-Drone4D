// Cheap production checks of world.json and metadata.json (AWR-16 §15.5; M05-FR-002, §6.13) and the M05 reason codes
// (AWR-17 §8, 400-411). Owner: M05. A failure rejects the whole world with 401 PC_FORMAT_UNSUPPORTED; the full schema
// validation runs in worldpkg and the contract tests.
import type { PotreeMeta, WorldJson } from '../types'

export class PcError extends Error {
  constructor(readonly code: number, message: string) {
    super(message)
  }
}

export const PC_WORLD_NOT_FOUND = 400
export const PC_FORMAT_UNSUPPORTED = 401
export const PC_CONTENT_STALE = 402
export const PC_FIRST_SCREEN_FAILED = 403
export const PC_NODE_FAILED = 404
export const PC_RANGE_MISMATCH = 405
export const PC_POOL_STALL = 406
export const PC_CAPACITY_CLAMPED = 407
export const PC_WORKER_CRASHED = 408
export const PC_GPU_RESET = 409
export const PC_PICK_MISS = 410
export const PC_PICK_TIMEOUT = 411

export function checkWorldCheap(w: WorldJson): void {
  if (!w || typeof w.contentVersion !== 'string' || String(w.schemaVersion ?? '').split('.')[0] !== '1') {
    throw new PcError(PC_FORMAT_UNSUPPORTED, 'world.json: schemaVersion 1.x with contentVersion required')
  }
}

export function checkMetaCheap(md: PotreeMeta, root?: { points: number; depth: number }): void {
  const a = md?.anet
  const fail = (m: string): never => {
    throw new PcError(PC_FORMAT_UNSUPPORTED, `metadata.json: ${m}`)
  }
  if (!md || md.version !== '2.0') fail('Potree version 2.0 required')
  if (md.encoding !== 'ANET_Q16') fail(`encoding ${String(md.encoding)} is not ANET_Q16`)
  if (!a || a.formatVersion !== 1) fail('anet.formatVersion 1 required')
  if (a.bytesPerPoint !== 12 && a.bytesPerPoint !== 16) fail('bytesPerPoint must be 12 or 16')
  if (!Array.isArray(a.streams) || a.bytesPerPoint !== 4 * (a.streams.length + 1)) fail('bytesPerPoint does not match streams')
  if (a.compression !== 'none' && a.compression !== 'gzip') fail(`compression ${String(a.compression)} is not none or gzip`)
  if (md.attributes?.[0]?.name !== 'anet:pos' || md.attributes?.[1]?.name !== 'anet:col') fail('attributes must start with anet:pos, anet:col')
  const s = md.boundingBox
  const ex = s.max[0] - s.min[0]
  if (!(ex > 0) || Math.abs(s.max[1] - s.min[1] - ex) > 1e-6 * Math.max(1, ex) || Math.abs(s.max[2] - s.min[2] - ex) > 1e-6 * Math.max(1, ex)) fail('boundingBox must be a cube')
  if (!Array.isArray(a.levelsByteEnd) || a.levelsByteEnd.length !== md.hierarchy.depth + 1) fail('levelsByteEnd length must be depth + 1')
  for (let i = 1; i < a.levelsByteEnd.length; i++) if (a.levelsByteEnd[i] < a.levelsByteEnd[i - 1]) fail('levelsByteEnd must be monotonic')
  if (!Array.isArray(a.levelsPoints) || a.levelsPoints.length !== md.hierarchy.depth + 1) fail('levelsPoints length must be depth + 1')
  if (md.hierarchy.firstChunkSize % 22 !== 0) fail('hierarchy.firstChunkSize must be a multiple of 22')
  if (root && (root.points !== md.points || root.depth !== md.hierarchy.depth)) fail('world.json roots[] and metadata disagree on points or depth')
}

/** hierarchy.bin length must be a multiple of 22 (FR-002) */
export function checkHierarchyLength(byteLength: number): void {
  if (byteLength === 0 || byteLength % 22 !== 0) throw new PcError(PC_FORMAT_UNSUPPORTED, `hierarchy.bin is ${byteLength} B, not a multiple of 22`)
}

/** the first-screen levelsByteEnd must lie inside octree.bin (total from Content-Range) (FR-002) */
export function checkOctreeTotal(md: PotreeMeta, total: number): void {
  if (total < 0) return
  const last = md.anet.levelsByteEnd[md.anet.levelsByteEnd.length - 1]
  if (last > total) throw new PcError(PC_FORMAT_UNSUPPORTED, `levelsByteEnd ${last} exceeds octree.bin (${total} B)`)
}
