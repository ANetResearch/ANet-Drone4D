// 样例运行页的数据：事件磁带与机队轨迹（只经 labGet 读取 /lab-static/v/<version>/，AWR-14 §21.14.2 规则 1）、视频与切片的地址、
// 播放头时长。Owner: M15.
// - 磁带由前端解码器解码（engine/actors/tape，按 G02 约定直接引用子模块，不经 '@/engine' 门面：门面带入实时客户端，触发 /lab/ 构建
//   守卫）；回放只显示脚本演员（D3-AC-21）；
// - 事件时刻 = 运行仿真时刻 + 事件偏移。公开样例为 Env 运行（偏移 0，tools/bench/d3/public_slice.py 同一口径），索引不携带偏移；
// - 播放头时长取磁带、轨迹、切片窗口与已加载视频时长中的最大值。
import * as React from 'react'
import { TapeReader } from '@/engine/actors/tape/decode'
import { labGet, type LabStaticEvent, type LabStaticIndex, type LabStaticRun } from '../staticIndex'
import { parseTracks, TracksError, tracksSpan, type FleetTracks } from './tracks'

/** 公开样例的事件偏移（s）：Env 运行为 0 */
export const SAMPLE_EVENT_OFFSET_S = 0

export type Loadable<T> = { kind: 'none' } | { kind: 'loading' } | { kind: 'ready'; value: T } | { kind: 'error'; code: string }

/** 样例运行所属的事件 */
export function eventOf(index: LabStaticIndex, run: LabStaticRun): LabStaticEvent | null {
  return index.events.find((e) => e.id === run.eventId && e.runs.some((r) => r.runId === run.runId)) ?? null
}

/** 播放头时长（s）：磁带、轨迹、切片窗口与视频（t0 + 时长）的最大值 */
export function sampleDurationS(parts: { tape?: TapeReader | null; tracks?: FleetTracks | null; run?: LabStaticRun | null; videoEndS?: number }): number {
  let d = 0
  if (parts.tape) d = Math.max(d, (parts.tape.nTicks - 1) / 10 - SAMPLE_EVENT_OFFSET_S)
  const sp = parts.tracks ? tracksSpan(parts.tracks) : null
  if (sp) d = Math.max(d, sp.endS)
  for (const s of parts.run?.slices ?? []) d = Math.max(d, s.tToS)
  if (parts.videoEndS !== undefined && Number.isFinite(parts.videoEndS)) d = Math.max(d, parts.videoEndS)
  return d
}

function errCode(e: unknown): string {
  if (e instanceof TracksError) return 'tracks'
  if (typeof e === 'object' && e !== null && 'code' in e) return String((e as { code: unknown }).code)
  return 'static.http'
}

async function fetchBytes(url: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const r = await labGet(url, { signal })
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { code: `http.${r.status}` })
  return r.arrayBuffer()
}

/** 读取并解码一个静态二进制制品；地址变化时重新读取（在途请求作废）。decode 须为模块级常量 */
function useBinary<T>(url: string | null, decode: (b: ArrayBuffer) => T): Loadable<T> {
  const [st, setSt] = React.useState<{ url: string | null; v: Loadable<T> }>({ url: null, v: { kind: 'none' } })
  React.useEffect(() => {
    if (!url) return
    const c = new AbortController()
    fetchBytes(url, c.signal).then((b) => {
      if (!c.signal.aborted) setSt({ url, v: { kind: 'ready', value: decode(b) } })
    }).catch((e: unknown) => {
      if (!c.signal.aborted) setSt({ url, v: { kind: 'error', code: errCode(e) } })
    })
    return () => c.abort()
  }, [url, decode])
  if (!url) return { kind: 'none' }
  return st.url === url ? st.v : { kind: 'loading' }
}

const decodeTape = (b: ArrayBuffer): TapeReader => new TapeReader(b)

/** 事件磁带（TapeReader）与观测层机队轨迹 */
export function useSampleData(index: LabStaticIndex, run: LabStaticRun): { tape: Loadable<TapeReader>; tracks: Loadable<FleetTracks> } {
  const ev = eventOf(index, run)
  const tape = useBinary(ev ? index.url(ev.tape) : null, decodeTape)
  const tracks = useBinary(run.tracksObs ? index.url(run.tracksObs) : null, parseTracks)
  return { tape, tracks }
}
