// RenderBackend selection (ADR-007, ADR-044; AWR-03 §3.5; M06 §6.2, FR-001..004, FR-011, FR-012). Owner: M06.
// One decision at start-up, never switched at run time:
//   1. Tier A only on an explicit preference (?rb=webgpu, settings) or the test switch ?tier=A: WebGPURenderer when the
//      adapter is not a fallback adapter (?tier=A / allowFallback=1 accept it); an init that lands on the WebGL2 backend
//      is disposed (never the WebGPURenderer WebGL2 fallback) and the classic path follows with notice M06-E013.
//   2. Classic WebGLRenderer + AnetNodesHandler (reversed-Z when EXT_clip_control exists, info.autoReset off).
//      Software rasterisers by UNMASKED_RENDERER (adapter info only when the string is masked) are Tier S / software;
//      other hardware is Tier B, iGPU by family name, otherwise the 300 ms microbench (or its 30-day cache).
// Start rung: software 0, iGPU 3, dGPU 4 (+1 with microbench headroom), minus the FR-013 start-rung memory.
import { WebGLRenderer } from 'three'
import { events, perfProbe, type DeviceClass, type Tier } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { AnetNodesHandler } from './anetNodesHandler'
import { M06Error } from './backend/guards'
import {
  classifyByMicrobench, classifyByName, isSoftware, readCache, rendererKey, startRungFor, writeCache, type AdapterText, type DeviceCache,
} from './backend/deviceClass'
import { runMicrobench } from './backend/microbench'
import { wrapGl, type BackendInfo, type RenderBackend } from './backend/webgl2'
import type { Forced } from './backend/testSwitches'

export { wrapGl, newPassPlan, type BackendInfo, type BackendState, type PassPlan, type RenderBackend } from './backend/webgl2'
export { classifyRenderer, dprFor, lowestRungFor, startRungFor, classifyByMicrobench, classifyByName } from './backend/deviceClass'
export { CH_MAIN, CH_CLOUD, CH_PICK, CH_BENCH } from './layers/registry'
export type { Forced as ForcedFlags } from './backend/testSwitches'

export interface BackendOptions {
  pref: 'auto' | 'webgpu'
  forced: Forced | null
  cached?: DeviceCache | null
  /** tests: replaces the microbench (returns t_1M in ms) */
  microbench?: (r: WebGLRenderer) => Promise<number>
}

function unmaskedRenderer(gl: WebGL2RenderingContext): string {
  const ext = gl.getExtension('WEBGL_debug_renderer_info')
  return ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : ''
}

async function adapterInfo(timeoutMs = 100): Promise<AdapterText | null> {
  const gpu = (typeof navigator !== 'undefined' ? (navigator as unknown as { gpu?: { requestAdapter(o: object): Promise<{ info?: AdapterText } | null> } }).gpu : undefined)
  if (!gpu) return null
  try {
    const ad = await Promise.race([gpu.requestAdapter({ powerPreference: 'high-performance' }), new Promise<null>((ok) => setTimeout(() => ok(null), timeoutMs))])
    return ad?.info ?? null
  } catch {
    return null
  }
}

export async function createRenderBackend(canvas: HTMLCanvasElement, opts: BackendOptions): Promise<RenderBackend> {
  const forced = TEST_SWITCHES ? opts.forced : null
  const wantA = forced?.tier === 'A' || (forced == null && opts.pref === 'webgpu')
  if (wantA) {
    // Tier A is D1-ext (P1): the WebGPU backend is not part of this delivery; the preference falls back (M06-E013)
    console.info('M06-E013 webgpu preference: Tier A unavailable in this build, using the classic WebGL2 backend')
    events.emit('backend.notice', 'webgpu.unavailable')
  }
  let r: WebGLRenderer
  try {
    r = new WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance', reversedDepthBuffer: true, stencil: false, alpha: false })
  } catch (e) {
    throw new M06Error('M06-E001', `WebGL2 context creation failed: ${String((e as Error)?.message ?? e)}`)
  }
  r.setNodesHandler(new AnetNodesHandler())
  ;(r as unknown as { reversedDepthBuffer: boolean }).reversedDepthBuffer = r.capabilities.reversedDepthBuffer
  r.info.autoReset = false
  const name = unmaskedRenderer(r.getContext() as WebGL2RenderingContext)
  const adapter = name === '' ? await adapterInfo() : null
  const software = isSoftware(name, adapter)
  let tier: Tier = software ? 'S' : 'B'
  if (forced?.tier === 'B' || forced?.tier === 'S') tier = forced.tier
  const key = rendererKey(name, adapter)
  const cache = opts.cached !== undefined ? opts.cached : readCache(key)
  let dc: DeviceClass = 'software'
  let bonus = 0
  let t1M: number | null = null
  let mbMs: number | null = null
  if (!software) {
    const byName = classifyByName(name, adapter)
    if (byName) dc = byName
    else if (cache && cache.t1M !== null) {
      t1M = cache.t1M
      const c = classifyByMicrobench(t1M)
      dc = c.deviceClass
      bonus = c.bonus
    } else {
      const t0 = performance.now()
      t1M = opts.microbench ? await opts.microbench(r) : (await runMicrobench(r)).t1M
      mbMs = performance.now() - t0
      const c = classifyByMicrobench(t1M)
      dc = c.deviceClass
      bonus = c.bonus
    }
    writeCache({ rendererKey: key, t1M, deviceClass: dc, ts: Date.now(), startMinus: cache?.startMinus ?? 0, demoteA: cache?.demoteA ?? false })
  }
  const info: BackendInfo = {
    rendererString: name, adapterArch: adapter?.architecture ?? '', microbenchMs: mbMs, t1M, forced, selftest: null, rendererKey: key,
  }
  const p = perfProbe()
  if (mbMs !== null) p.meta.microbenchMs = mbMs
  return wrapGl(r, tier, dc, startRungFor(dc, bonus, cache?.startMinus ?? 0), info)
}
