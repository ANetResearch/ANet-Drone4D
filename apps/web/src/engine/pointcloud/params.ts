// Point cloud parameters (M05 §6.8.1, §6.8.2, §6.10; ADR-009..ADR-013, ADR-041, ADR-044). Owner: M05.
// The single place for every M05 tuning value; other files must not repeat these literals (M05 §6.10). Test builds may
// override fixedB, pcInject and quality through PointCloudEngineOptions.params (M05-FR-046).
import type { DeviceClass, Tier } from '../loop'

export interface Rung {
  name: string
  /** render scale of the point pass (Tier S is locked at 0.5 on every rung) */
  rs: number
  lo: number
  hi: number
  /** screen-space error target, raster px */
  tau: number
  minPx: number
  /** maxPx when limited by tau (headroom, error, complete) */
  maxPx: number
  /** maxPx when limited by the budget (budget, nodes) or while streaming */
  maxPxSparse: number
  edlTaps: 0 | 4 | 8
}

/**
 * 7-rung ladder, frozen by ADR-012 (g02 §7.2). The tau-limited maxPx below is g02's upper bound; ADR-063 (FX-WEB1) caps
 * the non-leaf nodes of dense frames further at min(maxPx, max(4, ceil(2.5 sizeK tau))), see tauCapPx.
 */
export const LADDER: readonly Rung[] = [
  { name: 'soft-min', rs: 0.5, lo: 10_000, hi: 40_000, tau: 4.0, minPx: 2, maxPx: 8, maxPxSparse: 16, edlTaps: 0 },
  { name: 'soft', rs: 0.6, lo: 40_000, hi: 150_000, tau: 3.0, minPx: 2, maxPx: 8, maxPxSparse: 16, edlTaps: 0 },
  { name: 'minimum', rs: 0.6, lo: 150_000, hi: 750_000, tau: 2.7, minPx: 1.5, maxPx: 8, maxPxSparse: 12, edlTaps: 4 },
  { name: 'low', rs: 0.75, lo: 750_000, hi: 1_500_000, tau: 2.0, minPx: 1.5, maxPx: 8, maxPxSparse: 12, edlTaps: 4 },
  { name: 'medium', rs: 1.0, lo: 1_500_000, hi: 3_000_000, tau: 1.35, minPx: 1, maxPx: 8, maxPxSparse: 8, edlTaps: 8 },
  { name: 'high', rs: 1.0, lo: 3_000_000, hi: 6_000_000, tau: 1.0, minPx: 1, maxPx: 8, maxPxSparse: 8, edlTaps: 8 },
  { name: 'ultra', rs: 1.0, lo: 6_000_000, hi: 12_000_000, tau: 0.7, minPx: 1, maxPx: 8, maxPxSparse: 8, edlTaps: 8 },
]

export const PC = {
  // ---- selector (M05 §6.4, §6.10)
  tauMinFrac: 0.25,
  headroom: 0.15,
  maxNodes: 4096,
  maxSkips: 32,
  minPrefix: 512,
  hysteresis: 0.1,
  depthCapNone: 255,
  focusSigmaM: 150,
  focusGain: 1.5,
  // ---- CAS (M05 §6.8.3)
  casEvalMs: 250,
  casWindow: 24,
  casMinSamples: 6,
  casMaxDtMs: 1000,
  casOverR: 1.1,
  casDownExp: 0.8,
  casDownMin: 0.5,
  casDownMax: 0.92,
  casTailDown: 0.9,
  casUnderR: 0.85,
  casUp: 1.08,
  casUpGood: 2,
  casProbe: 1.03,
  casProbeEvals: 8,
  casRungDownR: 1.2,
  casRungDownMs: 1000,
  casHeadroomR: 0.7,
  casHeadroomP95: 1.1,
  casWorkHeadroom: 0.7,
  casUpFastMs: 3000,
  casUpSlowMs: 5000,
  casUpDelayMs: 5000,
  casUpDelayMaxMs: 120_000,
  casBounceMs: 10_000,
  casReversalRel: 0.02,
  casCeilFrac: 0.9,
  casInBandR: 1.1,
  freezeFrames: 30,
  targetMsSoftware: 33.3,
  tailKSoftware: 2.0,
  tailKHardware: 1.6,
  b0Software: 25_000,
  bFloorSoftware: 20_000,
  rsLockSoftware: 0.5,
  // ---- loading (M05 §6.5, §6.10)
  inflightS: 4,
  inflightB: 8,
  inflightA: 12,
  httpCap: 4,
  httpCapMax: 5,
  workersS: 1,
  workersBA: 2,
  abortOutsideFrames: 2,
  abortStaleFrames: 8,
  abortSuperseded: false,
  maxAttempts: 3,
  retryBaseMs: 500,
  retryFactor: 4,
  failedRequeueMs: 10_000,
  firstScreenAttempts: 3,
  firstScreenAbsCap: 450_000,
  firstScreenPoolFrac: 0.8,
  firstScreenHiFactor: 2.5,
  prefetchMax: 64,
  // ---- residency (M05 §6.6, §6.10)
  cpuCacheS: 64 * 1024 * 1024,
  cpuCacheBA: 256 * 1024 * 1024,
  cpuCacheEvictTo: 0.9,
  bytesPerTexel: 16,
  poolWidth: 4096,
  poolRowsS: 62,
  poolRowsIgpu: 1221,
  poolRowsDgpu: 2442,
  page: 256,
  uploadPtsS: 20_000,
  uploadBytesBA: 8 * 1024 * 1024,
  pendingQuotaFactor: 2,
  capacityFrac: 0.6,
  /** a rung whose clamped band [lo, min(hi, 0.6 cap)] is narrower than 1 % of lo counts as degenerate (M05 §6.8.4) */
  degenerateEps: 0.01,
  evictTrigger: 1.5,
  evictTo: 1.15,
  dwellMs: 1000,
  /** after a device loss, node dispatch resumes at 0.95 coverage or after this delay (M05 §6.6.5) */
  recoverDispatchMs: 2000,
  // ---- draw (M05 §6.7, §6.10)
  sizeK: 1.7,
  /** ADR-063 cap of non-leaf nodes in dense frames: max(tauCapMinPx, ceil(tauCapK sizeK tau)), raster px */
  tauCapK: 2.5,
  tauCapMinPx: 4,
  maxPxTauMs: 300,
  sparseProgress: 0.95,
  sparseWhileStreaming: true,
  weyl: 0.618033988749895,
  heightGamma: 0.6,
  lightAmbient: 0.45,
  lightSun: 0.55,
  lightSky: 0.15,
  drawTableWidth: 1024,
  drawTableRows: 4,
  /**
   * DrawTable block index (FX2-R2): log2 of the vertex block; for every block of 64 vertices the entry containing its
   * first vertex, so the vertex stage searches only the entries of its block (usually 1-2) instead of 12 binary-search
   * steps over the whole table. R32UI, drawTableWidth wide, rows for the pool capacity.
   */
  drawIndexBlockLog2: 6,
  nodeTableWidth: 1024,
  classMaskDefault: 0x1fff,
  classMaskValid: 0x7fff,
  heroClass: 11,
  edlTapsLow: 4,
  edlTapsHigh: 8,
  // ---- picking (M05 §6.9)
  pickWindow: 5,
  pickTimeoutMs: 2000,
  // ---- telemetry (M05 §6.10)
  statsHz: 4,
  fillTauMs: 1000,
  eventThrottleMs: 1000,
  // ---- quality sampling (M05-FR-054, AWR-18 §4.5)
  qualityT0S: 2.5,
  qualityStepS: 5,
  qualitySamples: 12,
} as const

/** CAS and residency parameters of one device class (M05 §6.8.2, §6.10) */
export interface DeviceParams {
  deviceClass: DeviceClass
  tier: Tier
  poolRows: number
  floorIndex: number
  ceilIndex: number
  tailK: number
  /** B_floor before PerfGovernor step 7 */
  bFloor: number
  /** B at start; software 25k, hardware the start rung's hi */
  b0: number
  cpuCacheBytes: number
  uploadPtsPerFrame: number
  inflight: number
  workers: number
  /** render-scale lock (Tier S 0.5), 0 = follow the rung */
  rsLock: number
}

export function poolRowsFor(deviceClass: DeviceClass): number {
  return deviceClass === 'software' ? PC.poolRowsS : deviceClass === 'iGPU' ? PC.poolRowsIgpu : PC.poolRowsDgpu
}

/**
 * Degenerate rung (M05 §6.8.4): the capacity clamp 0.6 x pool collapses its band to (almost) one point, so moving up
 * into it adds no budget. With the ADR-010 round-2 pools the clamp sits a few hundred points above the auto-ceiling
 * rung's hi (iGPU 3,000,729 vs 3M), hence the 1 % tolerance instead of a strict lo >= 0.6 x pool.
 */
export function isDegenerateRung(r: Rung, capPts: number): boolean {
  return Math.min(r.hi, PC.capacityFrac * capPts) <= r.lo * (1 + PC.degenerateEps)
}

/** highest non-degenerate rung at or below k for a pool of capPts points (M05 §6.8.4) */
export function nonDegenerateAtOrBelow(k: number, capPts: number, ladder: readonly Rung[] = LADDER): number {
  let i = Math.min(Math.max(0, k), ladder.length - 1)
  while (i > 0 && isDegenerateRung(ladder[i], capPts)) i--
  return i
}

/**
 * ADR-063 (FX-WEB1): the point-size cap of non-leaf nodes in dense (tau-limited) frames. With the one-level Lite shrink
 * the frontier level (tau <= key < 2 tau) and its parent (octant over a drawn child: pitch = the child's spacing) draw
 * at sizeK x key < 2 sizeK tau px, but older ancestors keep pitch >= 2 x the frontier spacing and cover the fine levels
 * as overlapping discs of up to maxPx on 5-6 level hierarchies (the "bubble" look). Cap = max(4, ceil(2.5 sizeK tau))
 * keeps the frontier and its parent at their Lite size (2.5 rather than 2: the +-10 % hysteresis and keys taken at the
 * nearest point of the node box) and clamps the older ancestors. Leaf nodes (no child in the data: where the data is
 * exhausted near the camera) keep the rung's maxPx per node (NodeTable leaf flag), so the finest points still close up;
 * sparse frames keep maxPxSparse for every node. Never above the rung's maxPx (g02 §7.2): Tier S rungs (tau >= 2.7) and
 * low (tau 2) come out at 8, i.e. unchanged.
 */
export function tauCapPx(rung: Rung): number {
  return Math.min(rung.maxPx, Math.max(PC.tauCapMinPx, Math.ceil(PC.tauCapK * PC.sizeK * rung.tau)))
}

export function deviceParams(tier: Tier, deviceClass: DeviceClass, startRung: number, lowestAllowedRung: number, maxTextureSize = 16384): DeviceParams {
  const software = deviceClass === 'software'
  const rows = Math.min(poolRowsFor(deviceClass), Math.max(1, maxTextureSize))
  const cap = rows * PC.poolWidth
  const floorIndex = software ? 0 : Math.max(0, lowestAllowedRung)
  const ceil = software ? 1 : deviceClass === 'iGPU' ? 4 : 5
  const start = nonDegenerateAtOrBelow(startRung, cap)
  return {
    deviceClass, tier, poolRows: rows, floorIndex,
    ceilIndex: nonDegenerateAtOrBelow(ceil, cap),
    tailK: software ? PC.tailKSoftware : PC.tailKHardware,
    bFloor: software ? PC.bFloorSoftware : LADDER[floorIndex].lo,
    b0: software ? PC.b0Software : Math.min(LADDER[start].hi, PC.capacityFrac * cap),
    cpuCacheBytes: software ? PC.cpuCacheS : PC.cpuCacheBA,
    uploadPtsPerFrame: software ? PC.uploadPtsS : PC.uploadBytesBA / PC.bytesPerTexel,
    inflight: tier === 'S' ? PC.inflightS : tier === 'B' ? PC.inflightB : PC.inflightA,
    workers: software ? PC.workersS : PC.workersBA,
    rsLock: software ? PC.rsLockSoftware : 0,
  }
}

/** in-flight limit: min(tier value, HTTP cap); http/1.1 or unknown 4 (configurable up to 5), h2/h3 unlimited (M05-FR-016) */
export function httpCapFor(nextHopProtocol: string | null | undefined, configured: number = PC.httpCap): number {
  const p = (nextHopProtocol ?? '').toLowerCase()
  if (p === 'h2' || p === 'h3' || p.startsWith('h3-')) return Number.POSITIVE_INFINITY
  return Math.min(Math.max(1, configured), PC.httpCapMax)
}
