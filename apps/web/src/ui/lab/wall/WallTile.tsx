// One observation-wall tile (AWR-14 §21.7.3, §21.7.4): the private view of one UAV's imaging sensor at the playhead.
// RGB tiles prefetch d3frames.v1 ranges ahead of the playhead (2 s) and decode JPEG to ImageBitmap; depth tiles fetch
// single 32FC1 frames and draw the log grey ramp. The bitmap cache is bounded; stale frames keep the last image at 0.6
// opacity with the "no new frame" badge.
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { benchBytes, frameIndex } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { Icon } from '@/ui/icons/Icon'
import { depthToRgba } from './depth'
import { frameAt, isStale, parseD3Frames } from './frames'

const PREFETCH_S = 2
const CACHE_MAX = 48

export interface WallTileProps {
  rid: string
  uav: string
  cam: string
  mode: 'rgb' | 'depth'
  w: number
  h: number
  selected: boolean
  onSelect: () => void
  onZoom: () => void
  onSpan: (firstS: number, lastS: number) => void
}

export function WallTile({ rid, uav, cam, mode, w, h, selected, onSelect, onZoom, onSpan }: WallTileProps) {
  const t = useT()
  const sensor = mode === 'depth' ? `${cam}.depth` : cam
  const idx = useQuery({ queryKey: ['lab', 'fidx', rid, uav, sensor], queryFn: ({ signal }) => frameIndex(rid, uav, sensor, signal), staleTime: Infinity })
  const tS = usePlayhead((s) => s.tS)
  const canvas = React.useRef<HTMLCanvasElement>(null)
  const cache = React.useRef(new Map<number, ImageBitmap>())
  const inflight = React.useRef(new Set<number>())
  const failed = React.useRef(new Set<number>())
  const [ver, bump] = React.useReducer((x: number) => x + 1, 0)
  const [drawn, setDrawn] = React.useState<number | null>(null)
  const items = React.useMemo(() => idx.data?.items ?? [], [idx.data])
  const periodNs = idx.data?.periodNs ?? 100_000_000
  const i = frameAt(items, tS * 1e9)
  const item = i >= 0 ? items[i] : undefined
  const stale = isStale(item, tS * 1e9, periodNs)

  React.useEffect(() => {
    const first = items[0]
    const last = items.at(-1)
    if (first && last) onSpan(first.t_ns / 1e9, last.t_ns / 1e9)
  }, [items, onSpan])

  // requests outlive playhead moves (a move only starts the next range once the current one no longer covers it); they
  // are aborted when the tile unmounts, and every finished request bumps `ver` so the loader and the canvas re-run
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
    // RGB keeps a runway of PREFETCH_S ahead of the playhead: the next range starts once less than half of it is cached
    const ahead = mode === 'depth' ? 0 : Math.max(1, Math.ceil((PREFETCH_S * 1e9) / periodNs))
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
    const base = `/runs/${encodeURIComponent(rid)}/frames/${encodeURIComponent(uav)}/${encodeURIComponent(sensor)}`
    const seqs = win.slice(miss).map((x) => x.seq).filter(want)
    for (const q of seqs) inflight.current.add(q)
    const run = async () => {
      if (mode === 'depth') {
        const { buf, headers } = await benchBytes(`${base}/${seqs[0]}`, signal)
        const [fw, fh] = (headers.get('x-frame-size') ?? '0x0').split('x').map(Number)
        put(seqs[0]!, await createImageBitmap(new ImageData(depthToRgba(new Float32Array(buf)), fw!, fh!)))
      } else {
        const { buf } = await benchBytes(`${base}:range?seq_from=${seqs[0]}&seq_to=${seqs.at(-1)}`, signal)
        for (const f of parseD3Frames(buf).frames) {
          if (!cache.current.has(f.seq)) put(f.seq, await createImageBitmap(new Blob([f.data as Uint8Array<ArrayBuffer>], { type: 'image/jpeg' })))
        }
      }
    }
    // a failed frame is not requested again while the tile is mounted (no retry loop; reopening the run retries)
    run().catch(() => {
      if (!signal.aborted) for (const q of seqs) if (!cache.current.has(q)) failed.current.add(q)
    }).finally(() => {
      for (const q of seqs) inflight.current.delete(q)
      if (ctl.current && !ctl.current.signal.aborted) bump()
    })
  }, [item, i, items, mode, periodNs, rid, sensor, uav, ver])

  React.useEffect(() => {
    const c = canvas.current
    const g = c?.getContext('2d')
    const bmp = item ? cache.current.get(item.seq) : undefined
    if (!c || !g || !bmp || !item) return
    g.drawImage(bmp, 0, 0, c.width, c.height)
    setDrawn(item.seq)
  }, [item, ver, w, h])

  return (
    <figure data-wall-tile={`${uav}/${sensor}`} data-selected={selected || undefined} onClick={onSelect} onDoubleClick={onZoom}
      className="relative m-0 overflow-hidden rounded-md bg-muted outline-offset-2 data-[selected]:outline-2 data-[selected]:outline-foreground"
      style={{ width: w, height: h }}>
      <canvas ref={canvas} width={w} height={h} className={stale ? 'opacity-60' : undefined} aria-label={t('lab.wall.tileAria', { uav, sensor })} />
      {drawn === null ? <span className="absolute inset-0 flex items-center justify-center text-hud-sub text-muted-foreground">{idx.isError ? t('lab.wall.noFrames') : t('lab.wall.loading')}</span> : null}
      <figcaption className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-background/70 px-2 py-0.5 text-hud-sub">
        <span className="font-mono">{uav}</span>
        <span className="text-muted-foreground">{sensor}</span>
        <span className="ml-auto font-mono tabular-nums">{item ? `#${item.seq} · ${(item.t_ns / 1e9).toFixed(1)} s` : '—'}</span>
      </figcaption>
      {drawn !== null && item && drawn !== item.seq && !stale ? (
        <span className="absolute right-1 top-1 rounded-sm bg-background/80 px-1.5 text-hud-sub">{t('lab.wall.loading')}</span>
      ) : null}
      {stale && item ? (
        <span className="absolute right-1 top-1 inline-flex items-center gap-1 rounded-sm bg-background/80 px-1.5 text-hud-sub">
          <Icon icon="state.stale" />
          {t('lab.wall.stale', { s: (tS - item.t_ns / 1e9).toFixed(1) })}
        </span>
      ) : null}
    </figure>
  )
}
