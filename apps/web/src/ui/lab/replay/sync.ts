// /lab/replay 双运行同步回放的时间模型（AWR-14 §21.13 lab 播放头、§21.7.3 取帧规则；M27-AC-009；D3-AC-19"两个 Run 同步对比时
// 时间轴一致"）。纯函数，不含 React 与网络：
// - 统一时间轴：lab 播放头只有一个时刻 u（仿真秒，运行 A 的仿真时间）；运行 r 的本地仿真时刻 t_r = u + shift_r。缺省对齐方式
//   `sim` 下两个运行的 shift 都为 0，即"同一 t_sim_ns"；`event` 方式按事件起点对齐（事件时刻 = 运行时刻 + offset_s，
//   shift_B = offset_A − offset_B），用于事件偏移不同的两个运行；
// - 取帧：每个通道取 t_ns ≤ t_r 的最大序号帧（不取未来帧），超过 1.5 个帧周期标记为无新帧（与观测墙相同，ui/lab/wall/frames.ts）；
// - 同步判定：同一机号两侧所取帧的时间戳换算到统一时间轴后的差 |Δ| ≤ 1 个帧周期（两侧周期不同时取较大者）；
// - 全程检查：Δ(u) 只在任一侧的帧时刻处变化（分段常数），因此在重叠区间内逐个帧时刻求值即得全程最大偏差，结果精确且与播放速率无关。
import { frameAt, isStale, type FrameRef } from '@/ui/lab/wall/frames'

export type RunSide = 'a' | 'b'
export const SIDES: readonly RunSide[] = ['a', 'b']
export type AlignMode = 'sim' | 'event'
export const ALIGN_MODES: readonly AlignMode[] = ['sim', 'event']
/** 播放速率（AWR-14 §21.13 LabPlayhead.rate） */
export const RATES = [0.25, 0.5, 1, 2, 4, 8] as const
/** 运行 id（与 /lab/runs/:rid 路由的校验相同：采集运行有命名 id，基准运行为 run-<10 位 base32>） */
export const REPLAY_RUN_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
/** 缺省帧周期（ns；帧索引未给出 period_ns 时，10 Hz） */
export const DEFAULT_PERIOD_NS = 100_000_000

/** 一个运行的一个成像通道（机号 × 传感器）的帧索引 */
export interface ReplayTrack { uav: string; periodNs: number; items: readonly FrameRef[] }
/** 一侧运行在统一时间轴上的位置：本地仿真时刻 = 统一时刻 + shiftNs */
export interface SideTracks { shiftNs: number; tracks: readonly ReplayTrack[] }

/** 某通道在统一时刻 u 所取的帧 */
export interface FramePick {
  /** 帧在索引中的下标；-1 为该时刻之前无帧 */
  i: number
  item: FrameRef | undefined
  /** 该侧的本地仿真时刻（ns） */
  tLocalNs: number
  /** 所取帧换算到统一时间轴的时刻（ns）；无帧时为 null */
  tUnifiedNs: number | null
  stale: boolean
}

/** 统一时刻 → 本地时刻（ns，取整） */
export const toLocalNs = (uNs: number, shiftNs: number): number => Math.round(uNs + shiftNs)

export function pickFrame(track: ReplayTrack, uNs: number, shiftNs: number): FramePick {
  const tLocalNs = toLocalNs(uNs, shiftNs)
  const i = frameAt(track.items, tLocalNs)
  const item = i >= 0 ? track.items[i] : undefined
  return { i, item, tLocalNs, tUnifiedNs: item ? item.t_ns - shiftNs : null, stale: isStale(item, tLocalNs, track.periodNs) }
}

/**
 * 两个运行的对齐量：`sim` 两侧为 0（同一 t_sim_ns）；`event` 以运行 A 为基准，B 的 shift = offset_A − offset_B（ns）。
 * 任一侧缺事件偏移时 `event` 不可用，回落为 `sim`（返回值的 mode 为实际生效的方式）。
 */
export function alignShifts(mode: AlignMode, offA: number | null | undefined, offB: number | null | undefined): { mode: AlignMode; a: number; b: number } {
  if (mode === 'event' && typeof offA === 'number' && typeof offB === 'number' && Number.isFinite(offA) && Number.isFinite(offB)) {
    return { mode, a: 0, b: Math.round((offA - offB) * 1e9) }
  }
  return { mode: 'sim', a: 0, b: 0 }
}

/** 统一时间轴的区间（s）：两侧全部通道首帧的最小值至末帧的最大值（换算到统一时间轴）；无帧时为 null */
export function unifiedSpan(sides: readonly SideTracks[]): { startS: number; endS: number } | null {
  let lo = Infinity
  let hi = -Infinity
  for (const s of sides) {
    for (const tr of s.tracks) {
      const first = tr.items[0]
      const last = tr.items.at(-1)
      if (!first || !last) continue
      lo = Math.min(lo, first.t_ns - s.shiftNs)
      hi = Math.max(hi, last.t_ns - s.shiftNs)
    }
  }
  return Number.isFinite(lo) ? { startS: lo / 1e9, endS: hi / 1e9 } : null
}

/** 同一机号两侧的取帧与偏差 */
export interface SyncPair {
  uav: string
  a: FramePick | null
  b: FramePick | null
  /** 统一时间轴上 A 帧时刻 − B 帧时刻（ns）；任一侧无该机或无帧时为 null */
  deltaNs: number | null
  /** 判定用的帧周期（两侧较大者，ns） */
  periodNs: number
  /** |Δ| ≤ 1 个帧周期；无法比较时为 null */
  ok: boolean | null
}
export interface SyncState { pairs: SyncPair[]; maxAbsDeltaNs: number | null; periodNs: number; ok: boolean | null; compared: number }

const byUav = (tracks: readonly ReplayTrack[]) => new Map(tracks.map((t) => [t.uav, t]))

/** 统一时刻 u 的两侧取帧与同步判定（机号取两侧并集，按机号升序） */
export function syncAt(a: SideTracks, b: SideTracks, uNs: number): SyncState {
  const ma = byUav(a.tracks)
  const mb = byUav(b.tracks)
  const uavs = [...new Set([...ma.keys(), ...mb.keys()])].sort()
  const pairs: SyncPair[] = []
  let maxAbs: number | null = null
  let period = 0
  let compared = 0
  let ok: boolean | null = null
  for (const uav of uavs) {
    const ta = ma.get(uav)
    const tb = mb.get(uav)
    const pa = ta ? pickFrame(ta, uNs, a.shiftNs) : null
    const pb = tb ? pickFrame(tb, uNs, b.shiftNs) : null
    const periodNs = Math.max(ta?.periodNs ?? 0, tb?.periodNs ?? 0) || DEFAULT_PERIOD_NS
    const deltaNs = pa?.tUnifiedNs != null && pb?.tUnifiedNs != null ? pa.tUnifiedNs - pb.tUnifiedNs : null
    const pairOk = deltaNs === null ? null : Math.abs(deltaNs) <= periodNs
    if (deltaNs !== null) {
      compared++
      maxAbs = Math.max(maxAbs ?? 0, Math.abs(deltaNs))
      period = Math.max(period, periodNs)
      ok = (ok ?? true) && Boolean(pairOk)
    }
    pairs.push({ uav, a: pa, b: pb, deltaNs, periodNs, ok: pairOk })
  }
  return { pairs, maxAbsDeltaNs: maxAbs, periodNs: period || DEFAULT_PERIOD_NS, ok, compared }
}

/** 全程同步检查的一个机号 */
export interface SweepPair { uav: string; points: number; maxAbsDeltaNs: number; worstUNs: number; periodNs: number; ok: boolean }
export interface SweepResult { pairs: SweepPair[]; points: number; maxAbsDeltaNs: number | null; ok: boolean | null }

/**
 * 全程同步检查：对两侧都有的每个机号，在两侧首帧都已出现、末帧都未结束的重叠区间 [max(first), min(last)] 内，逐个帧时刻
 * （两侧帧时刻的并集，换算到统一时间轴）求 Δ，返回最大 |Δ| 与其发生时刻。Δ(u) 为分段常数，只在帧时刻跳变，结果精确。
 */
export function sweepSync(a: SideTracks, b: SideTracks): SweepResult {
  const mb = byUav(b.tracks)
  const pairs: SweepPair[] = []
  for (const ta of [...a.tracks].sort((x, y) => (x.uav < y.uav ? -1 : x.uav > y.uav ? 1 : 0))) {
    const tb = mb.get(ta.uav)
    if (!tb || !ta.items.length || !tb.items.length) continue
    const lo = Math.max(ta.items[0]!.t_ns - a.shiftNs, tb.items[0]!.t_ns - b.shiftNs)
    const hi = Math.min(ta.items.at(-1)!.t_ns - a.shiftNs, tb.items.at(-1)!.t_ns - b.shiftNs)
    if (hi < lo) continue
    const periodNs = Math.max(ta.periodNs, tb.periodNs) || DEFAULT_PERIOD_NS
    let ia = Math.max(0, frameAt(ta.items, lo + a.shiftNs))
    let ib = Math.max(0, frameAt(tb.items, lo + b.shiftNs))
    let u = lo
    let worst = -1
    let worstU = lo
    let points = 0
    // 归并两侧的帧时刻：每一步推进到下一个最早的帧时刻，在该时刻求 Δ
    for (;;) {
      const da = ta.items[ia]!.t_ns - a.shiftNs - (tb.items[ib]!.t_ns - b.shiftNs)
      points++
      if (Math.abs(da) > worst) {
        worst = Math.abs(da)
        worstU = u
      }
      const na = ta.items[ia + 1]
      const nb = tb.items[ib + 1]
      const ua = na ? na.t_ns - a.shiftNs : Infinity
      const ub = nb ? nb.t_ns - b.shiftNs : Infinity
      const next = Math.min(ua, ub)
      if (next > hi) break
      if (ua === next) ia++
      if (ub === next) ib++
      u = next
    }
    pairs.push({ uav: ta.uav, points, maxAbsDeltaNs: worst, worstUNs: worstU, periodNs, ok: worst <= periodNs })
  }
  if (!pairs.length) return { pairs, points: 0, maxAbsDeltaNs: null, ok: null }
  return {
    pairs, points: pairs.reduce((s, p) => s + p.points, 0), maxAbsDeltaNs: Math.max(...pairs.map((p) => p.maxAbsDeltaNs)), ok: pairs.every((p) => p.ok),
  }
}

/** 两侧共有的成像传感器（不含 `cam0.depth` 一类派生通道），按名称排序；只有一侧时取该侧 */
export function commonCams(chA: readonly { sensor_id: string }[] | undefined, chB: readonly { sensor_id: string }[] | undefined): string[] {
  const cams = (ch: readonly { sensor_id: string }[] | undefined) => new Set((ch ?? []).map((c) => c.sensor_id).filter((s) => !s.includes('.')))
  const a = cams(chA)
  const b = cams(chB)
  const out = chA && chB ? [...a].filter((c) => b.has(c)) : [...(chA ? a : b)]
  return out.sort()
}

/** 一侧运行的一个待取索引的通道 */
export interface ReplayChannel { side: RunSide; rid: string; uav: string }

/** 两侧运行在传感器 cam 下的通道（各侧按机号升序；未选运行或运行详情未到时该侧为空） */
export function replayChannels(
  a: { rid: string | null; frames: readonly { uav_id: string; sensor_id: string }[] | undefined },
  b: { rid: string | null; frames: readonly { uav_id: string; sensor_id: string }[] | undefined },
  cam: string | null,
): ReplayChannel[] {
  const out: ReplayChannel[] = []
  for (const [side, r] of [['a', a], ['b', b]] as const) {
    if (!r.rid || !cam) continue
    const uavs = (r.frames ?? []).filter((c) => c.sensor_id === cam).map((c) => c.uav_id).sort()
    for (const uav of uavs) out.push({ side, rid: r.rid, uav })
  }
  return out
}

/** 通道与其帧索引（与通道同序；索引未到时为空轨道）→ 两侧轨道 */
export function tracksOf(chans: readonly ReplayChannel[], idx: readonly ({ periodNs: number; items: readonly FrameRef[] } | undefined)[]): Record<RunSide, ReplayTrack[]> {
  const out: Record<RunSide, ReplayTrack[]> = { a: [], b: [] }
  chans.forEach((c, i) => {
    const d = idx[i]
    out[c.side].push({ uav: c.uav, periodNs: d?.periodNs || DEFAULT_PERIOD_NS, items: d?.items ?? [] })
  })
  return out
}

// ── URL ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

/** 查询参数：`a`、`b`（运行 id）、`t`（统一时间轴仿真秒）、`cam`（传感器）、`align ∈ {sim, event}`、`rate` */
export interface ReplaySearch { a: string | null; b: string | null; t: number | null; cam: string | null; align: AlignMode; rate: number }

export function parseReplaySearch(s: URLSearchParams): ReplaySearch {
  const id = (k: string) => {
    const v = s.get(k)
    return v && REPLAY_RUN_RE.test(v) ? v : null
  }
  const t = Number(s.get('t'))
  const cam = s.get('cam')
  const rate = Number(s.get('rate'))
  return {
    a: id('a'), b: id('b'), t: s.get('t') !== null && Number.isFinite(t) && t >= 0 ? t : null,
    cam: cam && /^[A-Za-z0-9_-]{1,32}$/.test(cam) ? cam : null,
    align: s.get('align') === 'event' ? 'event' : 'sim',
    rate: (RATES as readonly number[]).includes(rate) ? rate : 1,
  }
}

/** 选定运行的 URL 补丁：交换、同侧替换；设为另一侧已选的同一运行时两侧交换（不出现 A = B 的误操作） */
export function assignRun(cur: Pick<ReplaySearch, 'a' | 'b'>, side: RunSide, rid: string | null): { a: string | null; b: string | null } {
  const other: RunSide = side === 'a' ? 'b' : 'a'
  if (rid !== null && cur[other] === rid) return { [side]: rid, [other]: cur[side] } as { a: string | null; b: string | null }
  return { ...cur, [side]: rid }
}

/** 仿真时刻读数 `HH:MM:SS.s`（§21.15.3 第 3 条，前缀 `T+` 由调用方加） */
export const clockText = (s: number): string => {
  const ds = Math.round(Math.max(0, s) * 10)
  const h = Math.floor(ds / 36_000)
  const m = Math.floor((ds % 36_000) / 600)
  const sec = (ds % 600) / 10
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${sec.toFixed(1).padStart(4, '0')}`
}

/** 带符号秒数（负号 U+2212，§13.3），三位小数 */
export const signedS = (ns: number): string => `${ns < 0 ? '\u2212' : ns > 0 ? '+' : ''}${(Math.abs(ns) / 1e9).toFixed(3)}`

/** 偏差读数（ms，一位小数） */
export const msText = (ns: number | null): string => (ns === null ? '—' : (ns / 1e6).toFixed(1))
