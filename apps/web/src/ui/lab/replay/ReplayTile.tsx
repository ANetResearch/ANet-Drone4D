// 双运行回放的一格（AWR-14 §21.7.3、§21.7.4 的取帧与预取规则；BN-49、BN-51）：一个运行中一架机一个成像传感器的私有视角。
// 与观测墙格（ui/lab/wall/WallTile.tsx）的区别：本格不读 lab 播放头，所取帧由页面按统一时间轴算出后传入（sync.ts pickFrame），
// 两侧格因而由同一时刻决定；只显示 RGB（存档 JPEG，即 Agent 收到的字节，ADR-163），不叠加真值。
// 预取：播放头前方 2 s 的 d3frames.v1 区间（缓存不足一半时再取下一段），JPEG 在浏览器解码为 ImageBitmap，缓存有上限；失败的帧在
// 本格挂载期间不重试。无新帧时保留上一帧并降低不透明度到 0.6，角标"无新帧 {Δ} s"。
import * as React from 'react'
import { useT } from '@/app/i18n'
import { benchBytes } from '@/net/bench'
import { Icon } from '@/ui/icons/Icon'
import { parseD3Frames } from '@/ui/lab/wall/frames'
import type { FramePick, ReplayTrack, RunSide } from './sync'

const PREFETCH_S = 2
const CACHE_MAX = 48

export interface ReplayTileProps {
  side: RunSide
  rid: string
  cam: string
  track: ReplayTrack
  pick: FramePick
  w: number
  h: number
  selected: boolean
  onSelect: () => void
}

export function ReplayTile({ side, rid, cam, track, pick, w, h, selected, onSelect }: ReplayTileProps) {
  const t = useT()
  const { uav, items, periodNs } = track
  const { i, item, stale, tLocalNs } = pick
  const canvas = React.useRef<HTMLCanvasElement>(null)
  const cache = React.useRef(new Map<number, ImageBitmap>())
  const inflight = React.useRef(new Set<number>())
  const failed = React.useRef(new Set<number>())
  const [ver, bump] = React.useReducer((x: number) => x + 1, 0)
  const [drawn, setDrawn] = React.useState<number | null>(null)

  const ctl = React.useRef<AbortController | null>(null)
  React.useEffect(() => {
    const c = new AbortController()
    ctl.current = c
    const bitmaps = cache.current
    return () => {
      c.abort()
      for (const b of bitmaps.values()) b.close()
      bitmaps.clear()
    }
  }, [])

  React.useEffect(() => {
    const signal = ctl.current?.signal
    if (!item || !signal) return
    const ahead = Math.max(1, Math.ceil((PREFETCH_S * 1e9) / periodNs))
    const win = items.slice(i, i + ahead + 1)
    const want = (q: number) => !cache.current.has(q) && !inflight.current.has(q) && !failed.current.has(q)
    const miss = win.findIndex((x) => want(x.seq))
    if (miss < 0 || miss > ahead / 2) return
    const put = (seq: number, b: ImageBitmap) => {
      cache.current.set(seq, b)
      while (cache.current.size > CACHE_MAX) {
        const k = cache.current.keys().next().value as number
        cache.current.get(k)?.close()
        cache.current.delete(k)
      }
    }
    const seqs = win.slice(miss).map((x) => x.seq).filter(want)
    for (const q of seqs) inflight.current.add(q)
    const base = `/runs/${encodeURIComponent(rid)}/frames/${encodeURIComponent(uav)}/${encodeURIComponent(cam)}`
    const run = async () => {
      const { buf } = await benchBytes(`${base}:range?seq_from=${seqs[0]}&seq_to=${seqs.at(-1)}`, signal)
      for (const f of parseD3Frames(buf).frames) {
        if (!cache.current.has(f.seq)) put(f.seq, await createImageBitmap(new Blob([f.data as Uint8Array<ArrayBuffer>], { type: 'image/jpeg' })))
      }
    }
    run().catch(() => {
      if (!signal.aborted) for (const q of seqs) if (!cache.current.has(q)) failed.current.add(q)
    }).finally(() => {
      for (const q of seqs) inflight.current.delete(q)
      if (ctl.current && !ctl.current.signal.aborted) bump()
    })
  }, [item, i, items, periodNs, rid, cam, uav, ver])

  React.useEffect(() => {
    const c = canvas.current
    const g = c?.getContext('2d')
    const bmp = item ? cache.current.get(item.seq) : undefined
    if (!c || !g || !bmp || !item) return
    g.drawImage(bmp, 0, 0, c.width, c.height)
    setDrawn(item.seq)
  }, [item, ver, w, h])

  return (
    <figure data-replay-tile={`${side}/${uav}/${cam}`} data-seq={item?.seq ?? ''} data-t-ns={item?.t_ns ?? ''} data-stale={stale || undefined}
      data-selected={selected || undefined} onClick={onSelect}
      className="relative m-0 overflow-hidden rounded-md bg-muted outline-offset-2 data-[selected]:outline-2 data-[selected]:outline-foreground"
      style={{ width: w, height: h }}>
      <canvas ref={canvas} width={w} height={h} className={stale ? 'opacity-60' : undefined} aria-label={t('lab.replay.tileAria', { side: side.toUpperCase(), uav, cam })} />
      {drawn === null ? (
        <span className="absolute inset-0 flex items-center justify-center text-hud-sub text-muted-foreground">
          {!items.length ? t('lab.replay.noFrames') : !item ? t('lab.replay.beforeFirst') : t('lab.replay.loading')}
        </span>
      ) : null}
      <figcaption className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-background/70 px-2 py-0.5 text-hud-sub">
        <span className="font-mono">{uav}</span>
        <span className="text-muted-foreground">{cam}</span>
        <span className="ml-auto font-mono tabular-nums">{item ? `#${item.seq} · ${(item.t_ns / 1e9).toFixed(2)} s` : '—'}</span>
      </figcaption>
      {stale && item ? (
        <span className="absolute right-1 top-1 inline-flex items-center gap-1 rounded-sm bg-background/80 px-1.5 text-hud-sub">
          <Icon icon="state.stale" />
          {t('lab.replay.stale', { s: ((tLocalNs - item.t_ns) / 1e9).toFixed(1) })}
        </span>
      ) : null}
    </figure>
  )
}
