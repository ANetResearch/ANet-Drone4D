// One observation-wall tile (AWR-14 §21.7.3, §21.7.4): the private view of one UAV's imaging sensor at the playhead.
// RGB tiles prefetch d3frames.v1 ranges ahead of the playhead (2 s) and decode JPEG to ImageBitmap; depth tiles fetch
// single 32FC1 frames and draw the log grey ramp. The bitmap cache is bounded; stale frames keep the last image at 0.6
// opacity with the "no new frame" badge. In the evaluation view (evaluator and admin) a second canvas overlays the truth
// of the camera at the same seq (BN-52 decode=raw): instance boundaries, invalid_mask hatching and object boxes.
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { benchBytes, frameIndex } from '@/net/bench'
import { usePlayhead } from '@/stores/lab/playhead'
import { Icon } from '@/ui/icons/Icon'
import { useLfTokens } from '@/ui/lf/useLfTokens'
import { depthToRgba } from './depth'
import { frameAt, isStale, parseD3Frames } from './frames'
import { parseD3GT, truthOverlay, type Rgb, type TruthFrame } from './truth'

const PREFETCH_S = 2
const CACHE_MAX = 48
const TRUTH_CACHE_MAX = 16

const rgbCache = new Map<string, Rgb>()
/** CSS colour (any syntax the tokens use) to sRGB bytes, through a 1 × 1 canvas */
function cssRgb(color: string): Rgb {
  const hit = rgbCache.get(color)
  if (hit) return hit
  const c = document.createElement('canvas')
  c.width = c.height = 1
  const g = c.getContext('2d', { willReadFrequently: true })
  let out: Rgb = [128, 128, 128]
  if (g && color) {
    g.fillStyle = color
    g.fillRect(0, 0, 1, 1)
    const d = g.getImageData(0, 0, 1, 1).data
    out = [d[0]!, d[1]!, d[2]!]
  }
  rgbCache.set(color, out)
  return out
}

export interface WallTileProps {
  rid: string
  uav: string
  cam: string
  mode: 'rgb' | 'depth'
  evalView: boolean
  w: number
  h: number
  selected: boolean
  onSelect: () => void
  onZoom: () => void
  onSpan: (firstS: number, lastS: number) => void
}

export function WallTile({ rid, uav, cam, mode, evalView, w, h, selected, onSelect, onZoom, onSpan }: WallTileProps) {
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

  const tokens = useLfTokens()
  const over = React.useRef<HTMLCanvasElement>(null)
  const [truths, setTruths] = React.useState<ReadonlyMap<number, TruthFrame>>(() => new Map())
  const seq = item?.seq
  const truth = evalView && seq !== undefined ? (truths.get(seq) ?? null) : null
  const want = evalView && seq !== undefined && !truths.has(seq) ? seq : undefined
  React.useEffect(() => {
    if (want === undefined) return
    const ctl = new AbortController()
    const url = `/runs/${encodeURIComponent(rid)}/truth/frames/${encodeURIComponent(uav)}/${encodeURIComponent(cam)}/${want}?decode=raw`
    benchBytes(url, ctl.signal).then(({ buf }) => {
      const fr = parseD3GT(buf)
      setTruths((m) => {
        const next = new Map(m)
        next.set(want, fr)
        while (next.size > TRUTH_CACHE_MAX) next.delete(next.keys().next().value as number)
        return next
      })
    }).catch(() => undefined)
    return () => ctl.abort()
  }, [want, rid, uav, cam])

  React.useEffect(() => {
    const c = over.current
    const g = c?.getContext('2d')
    if (!c || !g) return
    g.clearRect(0, 0, c.width, c.height)
    if (!truth) return
    const { width: tw, height: th } = truth.rec.agent_view
    const px = truthOverlay(truth, cssRgb(tokens.data), cssRgb(tokens.faint))
    let live = true
    const boxes = () => {
      const sx = c.width / tw
      const sy = c.height / th
      g.strokeStyle = tokens.data
      g.fillStyle = tokens.data
      g.lineWidth = 1
      g.font = `10px ${tokens.font}`
      for (const o of truth.rec.ground_truth.objects ?? []) {
        const [x, y, bw, bh] = o.bbox_xywh
        g.strokeRect(x * sx + 0.5, y * sy + 0.5, bw * sx, bh * sy)
        g.fillText(t('lab.wall.objLabel', { inst: o.inst, vis: o.visible_ratio.toFixed(2), h: o.h_px_unocc }), x * sx, Math.max(10, y * sy - 2))
      }
    }
    if (!px) {
      boxes()
      return
    }
    void createImageBitmap(new ImageData(px, tw, th)).then((bmp) => {
      if (live) {
        g.imageSmoothingEnabled = false
        g.drawImage(bmp, 0, 0, c.width, c.height)
        boxes()
      }
      bmp.close()
    })
    return () => {
      live = false
    }
  }, [truth, tokens, t, w, h])

  return (
    <figure data-wall-tile={`${uav}/${sensor}`} data-selected={selected || undefined} onClick={onSelect} onDoubleClick={onZoom}
      className="relative m-0 overflow-hidden rounded-md bg-muted outline-offset-2 data-[selected]:outline-2 data-[selected]:outline-foreground"
      style={{ width: w, height: h }}>
      <canvas ref={canvas} width={w} height={h} className={stale ? 'opacity-60' : undefined} aria-label={t('lab.wall.tileAria', { uav, sensor })} />
      {evalView ? <canvas ref={over} width={w} height={h} className="pointer-events-none absolute inset-0" aria-hidden /> : null}
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
