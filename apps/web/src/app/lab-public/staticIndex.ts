// 公开只读构建的唯一数据入口（AWR-14 §21.14.2；AWR-17 §18.8.5；M27 §7.8 `drone4d.lab_static_index.v1`；D3-AC-21）。Owner: M15.
// - 只发起 GET：labGet() 拒绝 /lab/ 与 /lab-static/ 以外的地址与任何非 GET 方法（AWR-14 §21.14.2 规则 1，UX-NFR-045）；
// - 索引 /lab-static/index.json 以 `cache: 'no-cache'` 读取（浏览器带 ETag 复核），制品只经版本化前缀 /lab-static/v/<version>/；
// - 索引中的制品路径一律为版本目录内的相对路径（不得以 / 开头、不得含 ..、反斜杠或协议），否则整份索引视为无效；
// - 公开构建不提供排行榜与提交（v1.3，R-D3-46）：索引声明 features.leaderboard 或 features.submit 时拒绝；
// - 页面不显示得分（D3-AC-21）：运行条目的 metrics 路径只保留在索引对象中，界面不读取。
import { LAB_ALLOWED_PREFIXES, LAB_STATIC_INDEX, LAB_STATIC_MAX_FILE_BYTES, LAB_STATIC_ROOT } from './flags'

export const LAB_STATIC_SCHEMA = 'drone4d.lab_static_index.v1'
/** 版本目录名（AWR-17 §18.8.5：YYYY.MM.DD-n） */
export const LAB_STATIC_VERSION_RE = /^\d{4}\.\d{2}\.\d{2}-\d+$/
/** 样例运行 id（AWR-14 §21.2.2 规则 1） */
export const LAB_RUN_ID_RE = /^run-[a-z2-7]{10}$/
/** 事件 id（AWR-14 §21.2.2 规则 1） */
export const LAB_EVENT_ID_RE = /^[a-z][a-z0-9-]{2,63}$/

export interface LabStaticFile {
  path: string
  bytes: number | null
  sha256: string | null
  /** 单文件 > 64 MB 时的分发渠道副本（ADR-201：ModelScope）；其他地址一律忽略 */
  external: string | null
}
export interface LabStaticVideo extends LabStaticFile {
  uav: string
  cam: string
  /** 视频种类（rgb、thermal）；索引未给出时为 null，由传感器名推断（app/lab-public/samples/videoSync.ts） */
  kind: 'rgb' | 'thermal' | null
}
export interface LabStaticSlice extends LabStaticFile {
  eventId: string
  tFromS: number
  tToS: number
}
export interface LabStaticRun {
  runId: string
  eventId: string
  eventVersion: number
  worldId: string | null
  policy: string
  nUav: number
  obsLevel: string
  videos: LabStaticVideo[]
  slices: LabStaticSlice[]
  basemap: string | null
  basemapBytes: number | null
  basemapSha256: string | null
  /** 观测层机队轨迹 tracks_obs.bin（AWR-17 §18.8.3） */
  tracksObs: string | null
  lidarCompare: string | null
}
export interface LabStaticEvent {
  id: string
  version: number
  worldId: string | null
  tape: string
  tapeJson: string | null
  runs: LabStaticRun[]
}
export interface LabStaticWorld {
  id: string
  pointcloud: string | null
  bytes: number | null
}
export interface LabStaticIndex {
  schema: typeof LAB_STATIC_SCHEMA
  version: string
  builtUnixNs: string | null
  license: { files: string[]; attribution: string[] }
  worlds: LabStaticWorld[]
  events: LabStaticEvent[]
  filesSha256: string | null
  filesBytesTotal: number | null
  features: { inspect: boolean; compare: boolean }
  /** 版本目录内相对路径 → 可请求的地址（/lab-static/v/<version>/<path>） */
  url: (path: string) => string
}

export type LabStaticErrorCode = 'static.http' | 'static.json' | 'static.schema' | 'static.version' | 'static.path' | 'static.feature' | 'static.request'

export class LabStaticError extends Error {
  readonly code: LabStaticErrorCode
  readonly detail: string
  constructor(code: LabStaticErrorCode, detail: string) {
    super(`${code}: ${detail}`)
    this.code = code
    this.detail = detail
  }
}

/** 地址是否在公开只读页允许的前缀内（同源相对路径，不含协议与主机） */
export function isAllowedLabUrl(url: string): boolean {
  if (!url.startsWith('/') || url.startsWith('//') || url.includes('\\')) return false
  const path = url.split(/[?#]/, 1)[0]
  if (path.split('/').some((s) => s === '..' || s === '.')) return false
  return LAB_ALLOWED_PREFIXES.some((p) => path.startsWith(p))
}

export type LabFetch = (url: string, init?: RequestInit) => Promise<Response>

/** 公开只读页的唯一请求函数：只允许 GET，只允许 /lab/ 与 /lab-static/ */
export function labGet(url: string, init: Omit<RequestInit, 'method' | 'body'> = {}, fetchFn: LabFetch = globalThis.fetch.bind(globalThis)): Promise<Response> {
  if (!isAllowedLabUrl(url)) return Promise.reject(new LabStaticError('static.request', url))
  const method = (init as RequestInit).method
  if (method !== undefined && method.toUpperCase() !== 'GET') return Promise.reject(new LabStaticError('static.request', `${method} ${url}`))
  return fetchFn(url, { ...init, method: 'GET', credentials: 'same-origin' })
}

/** 版本目录内相对路径的校验（不得逃出版本目录） */
export function isSafeRelPath(p: string): boolean {
  if (p === '' || p.length > 512 || p.startsWith('/') || p.includes('\\') || /^[a-z][a-z0-9+.-]*:/i.test(p)) return false
  return p.split('/').every((s, i, a) => s !== '..' && s !== '.' && (s !== '' || i === a.length - 1))
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function relPath(v: unknown, where: string, optional: true): string | null
function relPath(v: unknown, where: string, optional?: false): string
function relPath(v: unknown, where: string, optional = false): string | null {
  if ((v === undefined || v === null) && optional) return null
  const s = str(v)
  if (s === null || !isSafeRelPath(s)) throw new LabStaticError('static.path', `${where}: ${String(v)}`)
  return s
}

/** 分发渠道地址（lab_static_index.v1 `external`：channel = modelscope，https://modelscope.cn/ 或 www.modelscope.cn/） */
export const LAB_EXTERNAL_URL_RE = /^https:\/\/(www\.)?modelscope\.cn\/[^\s"'<>\\]*$/
const sha256Of = (v: unknown): string | null => {
  const sha = str(v)
  return sha && /^[0-9a-f]{64}$/.test(sha) ? sha : null
}
const bytesOf = (v: unknown): number | null => {
  const b = num(v)
  return b !== null && b >= 0 ? b : null
}

function file(v: Obj, key: string, where: string): LabStaticFile {
  const ext = isObj(v.external) ? str(v.external.url) : null
  return {
    path: relPath(v[key], `${where}.${key}`), bytes: bytesOf(v.bytes), sha256: sha256Of(v.sha256),
    external: ext !== null && LAB_EXTERNAL_URL_RE.test(ext) ? ext : null,
  }
}

function parseRun(v: unknown, ev: { id: string; version: number; worldId: string | null }, where: string): LabStaticRun {
  if (!isObj(v)) throw new LabStaticError('static.schema', where)
  const runId = str(v.run_id)
  if (runId === null || !LAB_RUN_ID_RE.test(runId)) throw new LabStaticError('static.schema', `${where}.run_id`)
  return {
    runId,
    eventId: ev.id,
    eventVersion: ev.version,
    worldId: ev.worldId,
    policy: str(v.policy) ?? '',
    nUav: Math.max(0, Math.trunc(num(v.n_uav) ?? 0)),
    obsLevel: str(v.obs_level) ?? '',
    videos: arr(v.videos).filter(isObj).map((x, i) => ({
      ...file(x, 'path', `${where}.videos[${i}]`), uav: str(x.uav) ?? '', cam: str(x.cam) ?? '', kind: x.kind === 'rgb' || x.kind === 'thermal' ? x.kind : null,
    })),
    slices: arr(v.slices).filter(isObj).map((x, i) => ({
      ...file(x, 'rrd', `${where}.slices[${i}]`), eventId: str(x.event_id) ?? '', tFromS: num(x.t_from_s) ?? 0, tToS: num(x.t_to_s) ?? 0,
    })),
    basemap: relPath(v.basemap, `${where}.basemap`, true),
    basemapBytes: bytesOf(v.basemap_bytes),
    basemapSha256: sha256Of(v.basemap_sha256),
    tracksObs: relPath(v.tracks_obs, `${where}.tracks_obs`, true),
    lidarCompare: relPath(v.lidar_compare, `${where}.lidar_compare`, true),
  }
}

/** 解析并校验索引；url() 把版本目录内相对路径映射到 /lab-static/v/<version>/ 下 */
export function parseStaticIndex(raw: unknown): LabStaticIndex {
  if (!isObj(raw) || raw.schema !== LAB_STATIC_SCHEMA) throw new LabStaticError('static.schema', String(isObj(raw) ? raw.schema : typeof raw))
  const version = str(raw.version)
  if (version === null || !LAB_STATIC_VERSION_RE.test(version)) throw new LabStaticError('static.version', String(raw.version))
  const feats = isObj(raw.features) ? raw.features : {}
  if (feats.leaderboard === true || feats.submit === true) throw new LabStaticError('static.feature', 'leaderboard/submit are not offered on the public site (R-D3-46)')
  const worlds = arr(raw.worlds).filter(isObj).map((w, i): LabStaticWorld => {
    const id = str(w.id)
    if (id === null) throw new LabStaticError('static.schema', `worlds[${i}].id`)
    const bytes = num(w.bytes)
    return { id, pointcloud: relPath(w.pointcloud, `worlds[${i}].pointcloud`, true), bytes: bytes !== null && bytes >= 0 ? bytes : null }
  })
  const events = arr(raw.events).map((e, i): LabStaticEvent => {
    if (!isObj(e)) throw new LabStaticError('static.schema', `events[${i}]`)
    const id = str(e.id)
    if (id === null || !LAB_EVENT_ID_RE.test(id)) throw new LabStaticError('static.schema', `events[${i}].id`)
    const ev = { id, version: Math.trunc(num(e.version) ?? 0), worldId: str(e.world_id) ?? str(e.world) }
    return {
      ...ev,
      tape: relPath(e.tape, `events[${i}].tape`),
      tapeJson: relPath(e.tape_json, `events[${i}].tape_json`, true),
      runs: arr(e.runs).map((r, j) => parseRun(r, ev, `events[${i}].runs[${j}]`)),
    }
  })
  const lic = isObj(raw.license) ? raw.license : {}
  const total = num(raw.files_bytes_total)
  const base = `${LAB_STATIC_ROOT}v/${version}/`
  return {
    schema: LAB_STATIC_SCHEMA,
    version,
    builtUnixNs: str(raw.built_unix_ns),
    license: {
      files: arr(lic.files).map((f, i) => relPath(f, `license.files[${i}]`)),
      attribution: arr(lic.attribution).map(str).filter((s): s is string => s !== null),
    },
    worlds,
    events,
    filesSha256: relPath(raw.files_sha256, 'files_sha256', true),
    filesBytesTotal: total !== null && total >= 0 ? total : null,
    features: { inspect: feats.inspect === true, compare: feats.compare === true },
    url: (p: string) => {
      if (!isSafeRelPath(p)) throw new LabStaticError('static.path', p)
      return base + p
    },
  }
}

/** 全部样例运行（按事件、运行 id 排序） */
export function sampleRuns(idx: LabStaticIndex): LabStaticRun[] {
  return idx.events.flatMap((e) => e.runs).sort((a, b) => a.eventId.localeCompare(b.eventId) || a.runId.localeCompare(b.runId))
}

export function findSample(idx: LabStaticIndex, runId: string): LabStaticRun | null {
  return idx.events.flatMap((e) => e.runs).find((r) => r.runId === runId) ?? null
}

/** 可下载的制品（视频、切片与底图），单文件 > 64 MB 的标记为不在本站提供（AWR-14 §21.14.2 规则 2） */
export function sampleFiles(run: LabStaticRun): (LabStaticFile & { kind: 'video' | 'slice' | 'basemap'; label: string; hosted: boolean })[] {
  const hosted = (b: number | null): boolean => b === null || b <= LAB_STATIC_MAX_FILE_BYTES
  return [
    ...run.videos.map((v) => ({ ...v, kind: 'video' as const, label: `${v.uav} · ${v.cam}`, hosted: hosted(v.bytes) })),
    ...run.slices.map((s) => ({ ...s, kind: 'slice' as const, label: `${s.eventId} · ${s.tFromS.toFixed(1)}–${s.tToS.toFixed(1)} s`, hosted: hosted(s.bytes) })),
    ...(run.basemap ? [{ path: run.basemap, bytes: run.basemapBytes, sha256: run.basemapSha256, external: null, kind: 'basemap' as const, label: 'basemap.rrd', hosted: true }] : []),
  ]
}

/** 读取索引（no-cache，ETag 复核） */
export async function loadStaticIndex(fetchFn?: LabFetch): Promise<LabStaticIndex> {
  let r: Response
  try {
    r = await labGet(LAB_STATIC_INDEX, { cache: 'no-cache' }, fetchFn)
  } catch (e) {
    if (e instanceof LabStaticError) throw e
    throw new LabStaticError('static.http', String(e))
  }
  if (!r.ok) throw new LabStaticError('static.http', String(r.status))
  let raw: unknown
  try {
    raw = await r.json()
  } catch (e) {
    throw new LabStaticError('static.json', String(e))
  }
  return parseStaticIndex(raw)
}
