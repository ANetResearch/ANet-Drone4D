// openWorld (AWR-16 §4.12 frozen order; AWR-17 §5.5; ADR-013; M05 §6.3, M05-FR-001..006, FR-019). Owner: M05.
// world.json (cache: no-cache) -> coordinate.json?v -> per root metadata.json?v -> hierarchy.bin?v (whole GET, no Range)
// + hierarchy_ext.bin?v -> cheap checks -> NodeStore (all roots, one index space) -> rule R level L -> one octree.bin?v
// Range [0, levelsByteEnd[L]) per root (forests in batches under the in-flight limit), split and packed in the fetch
// worker. The arrival of the last first-screen body starts TTFP (AWR-03 §11). Every request except world.json carries
// ?v=<contentVersion>; only the Range header is ever set; a 409 (310 CONTENT_VERSION_STALE) aborts with 402.
import { NodeStore, type HierRecord, type RootInput } from '../core/NodeStore'
import type { CoordinateJson, PotreeMeta, WorldJson } from '../types'
import { PC, httpCapFor } from '../params'
import { firstScreenLevel } from './firstScreen'
import { HIER_RECORD, T_PROXY, parseChunk, parseHierarchyExt, parseHierarchyPaged } from './hierarchy'
import {
  PC_CONTENT_STALE, PC_FIRST_SCREEN_FAILED, PC_FORMAT_UNSUPPORTED, PC_RANGE_MISMATCH, PC_WORLD_NOT_FOUND, PcError, checkHierarchyLength, checkMetaCheap,
  checkOctreeTotal, checkWorldCheap,
} from './meta'
import type { Fetcher } from './fetcher'
import { retryDelayMs } from '../core/StreamPolicy'

export interface OpenEnv {
  fetcher: Fetcher
  poolCapacityPts: number
  startHi: number
  signal?: AbortSignal
  /** main-thread clock (performance.now base) */
  now: () => number
  f?: typeof fetch
  onPhase?: (phase: 'manifest' | 'first_screen') => void
  /** resource timing protocol of a URL ('http/1.1', 'h2', ...); default reads performance entries */
  protocolOf?: (url: string) => string
  sleep?: (ms: number) => Promise<void>
}

export interface OpenedWorld {
  base: string
  world: WorldJson
  coord: CoordinateJson
  metas: PotreeMeta[]
  store: NodeStore
  firstScreenLevel: number
  firstScreenBytes: number
  /** packed node buffers of the first screen */
  firstScreen: { node: number; buf: Uint32Array }[]
  /** arrival of the last first-screen body (performance.now base) */
  ttfpStart: number
  bpp: 12 | 16
  compression: 'none' | 'gzip'
  nextHopProtocol: string
  /** T_world_layer (row-major 4x4) or null for identity */
  layerMatrix: number[] | null
  requests: number
}

const join = (base: string, rel: string): string => (base.endsWith('/') ? base : `${base}/`) + rel.replace(/^\.?\//, '')

function statusError(status: number, url: string): PcError {
  if (status === 409) return new PcError(PC_CONTENT_STALE, `409 ${url}`)
  if (status === 404) return new PcError(PC_WORLD_NOT_FOUND, `404 ${url}`)
  return new PcError(PC_FORMAT_UNSUPPORTED, `${status} ${url}`)
}

function defaultProtocol(url: string): string {
  try {
    const e = globalThis.performance?.getEntriesByName?.(url) as PerformanceResourceTiming[] | undefined
    return e && e.length ? (e[e.length - 1].nextHopProtocol ?? '') : ''
  } catch {
    return ''
  }
}

/** base = absolute URL of the world directory, e.g. https://host/worlds/shenzhen/ */
export async function openWorld(base: string, env: OpenEnv): Promise<OpenedWorld> {
  const f = env.f ?? ((...a: Parameters<typeof fetch>) => fetch(...a))
  const fetcher = env.fetcher
  const sleep = env.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
  let requests = 0
  const get = async (url: string, init: RequestInit = {}): Promise<Response> => {
    await fetcher.acquireSlot(env.signal)
    requests++
    try {
      return await f(url, { ...init, signal: env.signal })
    } finally {
      fetcher.release()
    }
  }
  const getJson = async <T>(url: string, init?: RequestInit): Promise<T> => {
    const r = await get(url, init)
    if (!r.ok) throw statusError(r.status, url)
    return (await r.json()) as T
  }
  const getBin = async (url: string, range?: [number, number]): Promise<ArrayBuffer> => {
    const r = await get(url, range ? { headers: { Range: `bytes=${range[0]}-${range[1]}` } } : {})
    if (!r.ok) throw statusError(r.status, url)
    return r.arrayBuffer()
  }
  env.onPhase?.('manifest')
  const worldUrl = join(base, 'world.json')
  const world = await getJson<WorldJson>(worldUrl, { cache: 'no-cache' })
  checkWorldCheap(world)
  const nextHopProtocol = (env.protocolOf ?? defaultProtocol)(worldUrl)
  fetcher.setHttpCap(httpCapFor(nextHopProtocol))
  const cv = world.contentVersion
  const v = `?v=${encodeURIComponent(cv)}`
  const coordP = getJson<CoordinateJson>(join(base, `coordinate.json${v}`))
  coordP.catch(() => {}) // awaited below; a failure of the roots first must not leave it unhandled
  const layer = world.layers.find((l) => l.type === 'pointcloud' && l.default && l.roots && l.roots.length)
  if (!layer || !layer.roots) throw new PcError(PC_FORMAT_UNSUPPORTED, 'world.json has no default point cloud layer')
  const roots = await Promise.all(layer.roots.map(async (r) => {
    const rb = join(base, r.href)
    const md = await getJson<PotreeMeta>(join(rb, `metadata.json${v}`))
    checkMetaCheap(md, { points: r.points, depth: r.depth })
    const hierUrl = join(rb, `hierarchy.bin${v}`)
    const extUrl = md.anet.hierarchyExt ? join(rb, `${md.anet.hierarchyExt.href}${v}`) : null
    const [hb, xb] = await Promise.all([getBin(hierUrl), extUrl ? getBin(extUrl).catch(() => null) : Promise.resolve(null)])
    checkHierarchyLength(hb.byteLength)
    const first = md.hierarchy.firstChunkSize
    let records: HierRecord[]
    let ext: Uint16Array | null
    const firstRecs = parseChunk(hb, 0, first)
    if (firstRecs.some((x) => x.type === T_PROXY)) {
      // FR-019: paged hierarchy; the sub-chunks already arrived with the whole-file GET, slice them locally
      const paged = await parseHierarchyPaged(hb.slice(0, first), xb ? xb.slice(0, (first / HIER_RECORD) * 12) : null,
        async (o, s) => hb.slice(o, o + s), xb ? async (o, s) => xb.slice(o, o + s) : null)
      records = paged.records
      ext = paged.ext
    } else {
      records = firstRecs
      ext = xb ? parseHierarchyExt(xb, records.length) : null
    }
    const input: RootInput = {
      cubeMin: md.boundingBox.min, cubeSize: md.boundingBox.max[0] - md.boundingBox.min[0], spacing: md.spacing, records, ext, octreeUrl: join(rb, `octree.bin${v}`),
    }
    return { md, input }
  }))
  const coord = await coordP
  const metas = roots.map((r) => r.md)
  const bpp = metas[0].anet.bytesPerPoint
  const compression = metas[0].anet.compression
  if (metas.some((m) => m.anet.bytesPerPoint !== bpp || m.anet.compression !== compression)) throw new PcError(PC_FORMAT_UNSUPPORTED, 'roots differ in bytesPerPoint or compression')
  fetcher.configure(bpp, compression)
  const store = new NodeStore(roots.map((r) => r.input))
  const L = firstScreenLevel(metas.map((m) => ({ levelsPoints: m.anet.levelsPoints, levelsByteEnd: m.anet.levelsByteEnd, depth: m.hierarchy.depth })), env.poolCapacityPts, env.startHi)
  env.onPhase?.('first_screen')
  // one Range per root for the BFS prefix of levels 0..L; forests in batches under the in-flight limit
  const firstScreen: { node: number; buf: Uint32Array }[] = []
  let bytes = 0
  let ttfpStart = 0
  const origin = globalThis.performance?.timeOrigin ?? 0
  const one = async (ri: number): Promise<void> => {
    const md = metas[ri]
    const end = md.anet.levelsByteEnd[Math.min(L, md.anet.levelsByteEnd.length - 1)]
    const [a, b] = store.rootRange(ri)
    const spans: number[] = []
    for (let i = a; i < b; i++) {
      if (store.level[i] <= L && store.numPoints[i] > 0 && store.byteOffset[i] + store.byteSize[i] <= end) spans.push(i, store.byteOffset[i], store.numPoints[i])
    }
    if (end <= 0 || !spans.length) return
    for (let attempt = 1; ; attempt++) {
      if (env.signal?.aborted) throw new PcError(0, 'aborted')
      const job = fetcher.requestAsync(store.rootUrl[ri], 0, end - 1, Int32Array.from(spans))
      const onAbort = (): void => fetcher.cancel(job.id)
      env.signal?.addEventListener('abort', onAbort, { once: true })
      requests++
      const r = await job.done
      env.signal?.removeEventListener('abort', onAbort)
      if (r.op === 'done') {
        checkOctreeTotal(md, r.total)
        ttfpStart = Math.max(ttfpStart, r.arrivedAt - origin)
        bytes += r.bytes
        for (let s = 0, k = 0; s < r.spans.length; s += 3, k++) firstScreen.push({ node: r.spans[s], buf: new Uint32Array(r.buffers[k]) })
        return
      }
      if (r.kind === 'abort') throw new PcError(0, 'aborted')
      if (r.status === 409) throw new PcError(PC_CONTENT_STALE, `409 first screen of root ${ri}`)
      if (r.kind === 'length') throw new PcError(PC_RANGE_MISMATCH, `first screen of root ${ri}: ${r.message}`)
      if (attempt >= PC.firstScreenAttempts) throw new PcError(PC_FIRST_SCREEN_FAILED, `first screen Range of root ${ri} failed ${attempt} times: ${r.message}`)
      await sleep(retryDelayMs(attempt))
    }
  }
  const order = Array.from({ length: metas.length }, (_, i) => i)
  const width = Math.max(1, Math.min(fetcher.limit, order.length))
  let next = 0
  await Promise.all(Array.from({ length: width }, async () => {
    while (next < order.length) await one(order[next++])
  }))
  const T = layer.T_world_layer
  const identity = !T || T.every((row, i) => row.every((x, j) => Math.abs(x - (i === j ? 1 : 0)) < 1e-12))
  return {
    base, world, coord, metas, store, firstScreenLevel: L, firstScreenBytes: bytes, firstScreen, ttfpStart: ttfpStart || env.now(), bpp, compression, nextHopProtocol,
    layerMatrix: identity ? null : T!.flat(), requests,
  }
}
