// FPV perception HUD (R-D2-12, R-D2-14; M15-FR-218, FR-219; UX-FR-137; AWR-14 §20.10.6, §20.11; d2-WP-06-to-M15 item 2,
// d2-WP-11-to-M15 item 6): the frame of `uav/{id}/perception` (5 Hz, stores/perception.ts, the followed vehicle = the FPV
// focus) for the sensor the view looks through. Capture boxes: corner brackets around the projected box of each target of
// that sensor (nearest first, at most 8; viewport/facade sensorView.projectBox, centre = TargetLite32 position + h/2,
// size and heading from the target model), written by one overlay-phase task as transforms (no React render per frame);
// not captured: thin brackets, arming (P >= 0.9 and timing): dashed, captured: thick brackets with the verified mark and
// the one HERO red of the figure when no unconfirmed critical owns the viewport red (AWR-14 §20.11.2). The readout card
// shows the nearest target's level, pixel progress "9.6 / 14 px" with a bar, the localised limiting factor, the capture
// timing and the optics line (f, HFOV, slant range, GSD; LWIR adds the apparent temperature difference). Numbers are C
// class text at the store rate (<= 4 Hz). Viewer safe: reads only.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { loop } from '@/engine'
import { TARGET_CLASSES, targetsModel, type PerceptionSensorFrame, type PerceptionTarget, type TargetClassName } from '@/app/shell/engineD2'
import { usePerception } from '@/stores/perception'
import { useSelection } from '@/stores/selection'
import { attachTargetsFeed } from '@/stores/targets'
import { sensorView } from '@/viewport/facade'
import { Card } from '@/ui/components/ui/card'
import { Progress } from '@/ui/components/ui/progress'
import { Icon } from '@/ui/icons/Icon'
import { useRedOwner } from '@/ui/notify/redFigures'
import { useManual } from '@/ui/manual/controller'
import { useSensorViewInfo } from './SensorViewBar'
import { boxState, HUD_BOXES, nearestTargets, sensorFrameOf } from './hudModel'

const CORNER = 10
const CLASSES = new Set<string>(TARGET_CLASSES)

function CaptureBoxes({ targets, heroId }: { targets: readonly PerceptionTarget[]; heroId: string | null }) {
  const t = useT()
  const refs = React.useRef<(HTMLDivElement | null)[]>([])
  const layer = React.useRef<HTMLDivElement>(null)
  // id, measured position (the frame's meas_enu_m: the fallback while the page has no TargetLite32 rows) and class
  const ids = React.useRef<{ id: string; meas: readonly number[] | null; cls: string | null }[]>([])
  React.useEffect(() => {
    ids.current = targets.map((x) => ({ id: x.target, meas: x.meas_enu_m ?? null, cls: x.cls_est ?? null }))
  }, [targets])
  React.useEffect(() => {
    const rect = new Float32Array(4)
    const center = new Float64Array(3)
    const size = new Float64Array(3)
    return loop.register('overlay', 'm15.fpv-boxes', () => {
      // one layout read before the writes: the labels stay inside the viewport
      const W = layer.current?.clientWidth ?? 0
      for (let i = 0; i < HUD_BOXES; i++) {
        const el = refs.current[i]
        if (!el) continue
        const tg = ids.current[i]
        const row = tg ? targetsModel.rowOf(tg.id) : -1
        let ok = false
        if (tg && (row >= 0 || tg.meas)) {
          const cls = row >= 0 ? targetsModel.classOfRow(row) : (CLASSES.has(tg.cls ?? '') ? (tg.cls as TargetClassName) : null)
          const s = targetsModel.sizeOf(tg.id, cls)
          const R = targetsModel.rows
          const m = tg.meas
          center[0] = row >= 0 ? R.pos[3 * row] : (m?.[0] ?? 0)
          center[1] = row >= 0 ? R.pos[3 * row + 1] : (m?.[1] ?? 0)
          center[2] = (row >= 0 ? R.pos[3 * row + 2] : (m?.[2] ?? 0)) + s[2] / 2
          size[0] = s[0]
          size[1] = s[1]
          size[2] = s[2]
          ok = sensorView.projectBox(center, size, row >= 0 ? R.heading[row] : 0, rect)
        }
        if (!ok) {
          if (!el.dataset.hidden) el.dataset.hidden = ''
          continue
        }
        if (el.dataset.hidden !== undefined) delete el.dataset.hidden
        const x0 = rect[0]
        const y0 = rect[1]
        const x1 = Math.max(rect[2], x0 + 2 * CORNER)
        const y1 = Math.max(rect[3], y0 + 2 * CORNER)
        const c = el.children
        ;(c[0] as HTMLElement).style.transform = `translate(${x0}px,${y0}px)`
        ;(c[1] as HTMLElement).style.transform = `translate(${x1 - CORNER}px,${y0}px)`
        ;(c[2] as HTMLElement).style.transform = `translate(${x0}px,${y1 - CORNER}px)`
        ;(c[3] as HTMLElement).style.transform = `translate(${x1 - CORNER}px,${y1 - CORNER}px)`
        const lx = W > 0 ? Math.min(Math.max(4, x0), Math.max(4, W - 160)) : x0
        ;(c[4] as HTMLElement).style.transform = `translate(${lx}px,${y1 + 3}px)`
      }
    }, { order: 30, layer: 'labels' })
  }, [])
  return (
    <div ref={layer} className="fpv-boxes" aria-hidden="true" data-fpv-boxes={targets.length}>
      {Array.from({ length: HUD_BOXES }, (_, i) => {
        const tg = targets[i]
        const st = tg ? boxState(tg) : 'idle'
        return (
          <div key={i} ref={(el) => {
            refs.current[i] = el
          }} className="fpv-box" data-hidden="" data-state={st} data-hero={tg && tg.target === heroId ? '' : undefined} data-fpv-box={tg?.target ?? ''}
            style={tg ? undefined : { display: 'none' }}>
            <span className="fpv-corner" data-c="tl" />
            <span className="fpv-corner" data-c="tr" />
            <span className="fpv-corner" data-c="bl" />
            <span className="fpv-corner" data-c="br" />
            <span className="fpv-box-label flex items-center gap-1 rounded-sm bg-hud px-1 font-mono text-hud-cap text-foreground">
              {st === 'captured' ? <Icon icon="effect.verified" /> : null}
              {tg ? `${sanitizeText(tg.target, 24)} · ${t(`perception.best.${tg.level}`)}` : ''}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function Readout({ sf, tg }: { sf: PerceptionSensorFrame; tg: PerceptionTarget | null }) {
  const t = useT()
  const manual = useManual((s) => s.state === 'ACTIVE' || s.state === 'STOPPING')
  const optics = [
    sf.f_mm ? `f ${fmt.num(sf.f_mm, 1)} mm` : null,
    sf.hfov_rad ? `HFOV ${fmt.num((sf.hfov_rad * 180) / Math.PI, 1)}°` : null,
    tg ? `${t('fpv.range')} ${fmt.num(tg.range_m, 0)} m` : null,
    tg ? `GSD ${fmt.num(tg.gsd_m * 100, 1)} cm` : null,
    tg && sf.kind === 'lwir' && tg.delta_t_app_k !== undefined ? `ΔT ${fmt.num(tg.delta_t_app_k, 1)} K` : null,
  ].filter((x): x is string => x !== null)
  const progress = tg && tg.n_req > 0 ? Math.min(1, tg.n_eff / tg.n_req) : 0
  const level = tg?.level_req ?? null
  return (
    <div data-anchor="fpv-hud" data-below-manual={manual ? '' : undefined}>
      <Card size="sm" className="w-88 max-w-[calc(100vw-2rem)] gap-1.5 rounded-xl bg-hud px-3 py-2 text-hud-sub" data-fpv-hud={tg?.target ?? ''}>
        {tg ? (
          <>
            <span className="flex items-center gap-2">
              <Icon icon={boxState(tg) === 'captured' ? 'effect.verified' : 'cmd.track'} />
              <span className="font-mono">{sanitizeText(tg.target, 24)}</span>
              <span className="text-muted-foreground">{level ? t('fpv.required', { level: t(`perception.best.${level}`) }) : t(`perception.best.${tg.level}`)}</span>
              <span className="ml-auto font-mono font-semibold tabular-nums" data-fpv-px={`${fmt.num(tg.n_eff, 1)}/${fmt.num(tg.n_req, 0)}`}>
                {`${fmt.num(tg.n_eff, 1)} / ${fmt.num(tg.n_req, 0)} px`}
              </span>
            </span>
            <Progress value={Math.round(progress * 100)} aria-label={t('fpv.pixels')} />
            <span className="flex items-center gap-2 text-muted-foreground">
              {tg.limiting ? <span data-fpv-limiting={tg.limiting}>{t('perception.limitedBy', { factor: t(`perception.limiting.${tg.limiting}`) })}</span> : <span>{t('fpv.noLimit')}</span>}
              {boxState(tg) === 'arming' && tg.hold_s !== undefined ? <span className="ml-auto font-mono" data-fpv-hold="">{t('fpv.hold', { t: fmt.num(Math.min(1, tg.hold_s), 1) })}</span> : null}
              {boxState(tg) === 'captured' ? <span className="ml-auto" data-fpv-captured="">{t('target.captured')}</span> : null}
            </span>
          </>
        ) : <span className="text-muted-foreground" data-fpv-none="">{t('fpv.noTarget')}</span>}
        {optics.length ? <span className="font-mono text-muted-foreground" data-fpv-optics="">{optics.join(' · ')}</span> : null}
      </Card>
    </div>
  )
}

/** the HUD of the FPV camera (mounted by the viewport overlay while the camera mode is FPV) */
export function PerceptionHud() {
  // the boxes read the target model (positions, sizes): keep its feed attached while the HUD is shown
  React.useEffect(() => attachTargetsFeed(), [])
  const info = useSensorViewInfo()
  const frame = usePerception((s) => s.frame)
  const hud = usePerception((s) => s.hud)
  const primary = useSelection((s) => s.primary)
  const red = useRedOwner('viewport')
  const sf = frame ? sensorFrameOf(frame.sensors, info.sensorName, hud?.sensor ?? null) : null
  const targets = nearestTargets(sf)
  const nearest = targets[0] ?? null
  // the HERO red of the figure: the nearest captured target, unless a critical (discovery, critical vehicle) owns it
  const free = !red || (red.kind === 'drone' && red.id === primary)
  const heroId = free ? (targets.find((x) => boxState(x) === 'captured')?.target ?? null) : null
  if (!frame || !sf) return null
  return (
    <>
      <CaptureBoxes targets={targets} heroId={heroId} />
      <Readout sf={sf} tg={nearest} />
    </>
  )
}
