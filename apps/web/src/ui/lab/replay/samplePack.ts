// 内部样例包（D3-AC-56"样例包在内部工作区可浏览"；AWR-05 §10.2 P0a 交付物：crop-handoff-001 上每个基线 1 个代表运行的事件回放、
// 3 机私有视角视频、MCAP 与指标）的浏览数据形状与校验。纯函数。
// 来源：`GET /api/bench/v1/samples`（研究者及以上）返回 `{items: SamplePack[]}`，每项为 tools/bench/d3/sample_pack.py（B20）写出的
// `drone4d.bench.sample_pack.v1` 文档；代表运行取自 mbl_e2e 报告的 `representative_runs`（每组每基线取第 1 次重复中 EC 居中的种子）。
// 样例文件是代表运行的派生物，路径与 M27 §7.6 `derived/` 一致（例如 `video/uav-01_cam0.mp4`），经 BN-54
// `GET /runs/{rid}/artifacts/{path}` 下载（会话 Cookie；Range、ETag = sha256）。
// 校验规则：路径只接受派生物目录内的相对路径（不以 / 开头、不含 ..、反斜杠、协议或查询串）；运行 id、指标与字节数不合规的样例整条
// 丢弃并计入 `dropped`；文档不含种子（报告生成侧已检查，界面不显示任何种子字段）。
import { BENCH, type BoardMetricKey, type Components } from '@/net/bench'
import { REPLAY_RUN_RE } from './sync'

export const SAMPLE_PACK_SCHEMA = 'drone4d.bench.sample_pack.v1'
/** 与 `sample_pack.v1.schema.json` 的 `files[].kind` 枚举一致；tracks 为事件回放的机队轨迹 `tracks_obs.bin`（`bench/tracks.v1` 观测层） */
export const SAMPLE_FILE_KINDS = ['video', 'mcap', 'metrics', 'tape', 'manifest', 'slice', 'tracks'] as const
export type SampleFileKind = (typeof SAMPLE_FILE_KINDS)[number]
const METRIC_KEYS: readonly BoardMetricKey[] = ['ec', 'eu', 'ao', 'ce']

export interface SampleFile { kind: SampleFileKind; path: string; bytes: number | null; sha256: string | null; uav: string | null; cam: string | null }
export interface SampleEntry {
  /** 观测档组（l1、l2） */
  group: string
  policy: string
  runId: string
  metrics: Components
  simS: number | null
  files: SampleFile[]
}
export interface SamplePack {
  packId: string
  generatedUtc: string | null
  eventId: string
  eventVersion: number | null
  worldId: string | null
  boardVersion: string | null
  samples: SampleEntry[]
  /** 校验时丢弃的样例条数 */
  dropped: number
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown, re = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/): string | null => (typeof v === 'string' && re.test(v) ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** 派生物相对路径（BN-54）：不以 / 开头，不含 ..、反斜杠、协议、查询串或片段，段名只含安全字符 */
export function safeArtifactPath(p: unknown): p is string {
  if (typeof p !== 'string' || !p || p.length > 256 || p.startsWith('/')) return false
  if (/[\\?#]|:\/\/|^[A-Za-z]+:/.test(p)) return false
  return p.split('/').every((seg) => seg !== '' && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]+$/.test(seg))
}

function parseFile(v: unknown): SampleFile | null {
  if (!isObj(v) || !(SAMPLE_FILE_KINDS as readonly unknown[]).includes(v.kind) || !safeArtifactPath(v.path)) return null
  const bytes = num(v.bytes)
  const sha = typeof v.sha256 === 'string' && /^[0-9a-f]{64}$/.test(v.sha256) ? v.sha256 : null
  return { kind: v.kind as SampleFileKind, path: v.path, bytes: bytes !== null && bytes >= 0 ? bytes : null, sha256: sha, uav: str(v.uav), cam: str(v.cam) }
}

function parseMetrics(v: unknown): Components | null {
  if (!isObj(v)) return null
  const out = {} as Components
  for (const k of METRIC_KEYS) {
    const x = v[k]
    if (x === null || x === undefined) out[k] = null
    else if (typeof x === 'number' && Number.isFinite(x)) out[k] = x
    else return null
  }
  return out
}

function parseSample(v: unknown): SampleEntry | null {
  if (!isObj(v)) return null
  const runId = typeof v.run_id === 'string' && REPLAY_RUN_RE.test(v.run_id) ? v.run_id : null
  const group = str(v.group)
  const policy = str(v.policy)
  const metrics = parseMetrics(v.metrics ?? {})
  if (!runId || !group || !policy || !metrics) return null
  const files = Array.isArray(v.files) ? v.files.map(parseFile).filter((f): f is SampleFile => f !== null) : []
  const simS = num(v.sim_s)
  return { group, policy, runId, metrics, simS: simS !== null && simS >= 0 ? simS : null, files }
}

/** 一份样例包文档；schema 不符或缺少包 id、事件 id 时为 null */
export function parseSamplePack(v: unknown): SamplePack | null {
  if (!isObj(v) || v.schema !== SAMPLE_PACK_SCHEMA) return null
  const packId = str(v.pack_id)
  const eventId = str(v.event_id, /^[a-z][a-z0-9-]{2,63}$/)
  if (!packId || !eventId) return null
  const raw = Array.isArray(v.samples) ? v.samples : []
  const samples = raw.map(parseSample).filter((s): s is SampleEntry => s !== null)
  // 组内按策略名排序，组按名称排序（l1 在 l2 之前）
  samples.sort((x, y) => (x.group < y.group ? -1 : x.group > y.group ? 1 : x.policy < y.policy ? -1 : x.policy > y.policy ? 1 : 0))
  const ev = num(v.event_version)
  return {
    packId, generatedUtc: typeof v.generated_utc === 'string' ? v.generated_utc.slice(0, 32) : null, eventId,
    eventVersion: ev !== null && Number.isInteger(ev) && ev >= 1 ? ev : null, worldId: str(v.world_id), boardVersion: str(v.board_version),
    samples, dropped: raw.length - samples.length,
  }
}

/** `GET /samples` 的列表：无效文档丢弃，按生成时刻由新到旧（同刻按包 id） */
export function parseSamplePacks(v: unknown): SamplePack[] {
  const items = isObj(v) && Array.isArray(v.items) ? v.items : []
  const packs = items.map(parseSamplePack).filter((p): p is SamplePack => p !== null)
  return packs.sort((x, y) => (y.generatedUtc ?? '').localeCompare(x.generatedUtc ?? '') || (x.packId < y.packId ? -1 : x.packId > y.packId ? 1 : 0))
}

/** 样例文件的下载地址（BN-54） */
export const artifactHref = (runId: string, path: string): string =>
  `${BENCH}/runs/${encodeURIComponent(runId)}/artifacts/${path.split('/').map(encodeURIComponent).join('/')}`

/** 字节数读数（KiB、MiB、GiB，一位小数） */
export function bytesText(n: number | null): string {
  if (n === null) return '—'
  if (n < 1024) return `${n} B`
  const u = ['KiB', 'MiB', 'GiB', 'TiB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(1)} ${u[i]}`
}
