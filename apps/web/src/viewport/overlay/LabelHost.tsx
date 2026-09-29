// LabelHost: the single DOM label overlay of the viewport (M06 §6.13, FR-066, FR-067, AC-047; AWR-15 §10.10). Owner: M06.
// Owns the LabelLayer pool inside `.app-layer-labels` (z-labels, pointer-events none, contain strict). World phase:
// candidates = selected, red entity, critical, warning, hover, focus set, GoTo readout, hovered zone (<= 64), then the
// grid layout; overlay phase: DOM writes. Texts: sanitised roster names (lib/sanitize.ts, cached per roster version),
// FlightState short text from the formatter injected by M15 (labels.setFormatter; the engine never imports ui/**),
// numbers through lib/format. Registers the DOM layer 'labels' (no draws) and PerfGovernor step 3.
import { useEffect, useRef } from 'react'
import { DECLUTTER, LABEL_PRIO, LabelIcon, LabelKind, LabelLayer, MARK, perf, projectEnu, register, type GovernorKnob, type LabelText } from '@/engine'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { rtClient } from '@/net/rt'
import { registerLayer } from '../layers/registry'
import { useVp, vp } from '../session'
import { applyLayerVisibility } from '../bindings'
import { labelFormatter } from './labelFormatter'
import { Vector3 } from 'three'

export function LabelHost() {
  const ref = useRef<HTMLDivElement>(null!)
  const tier = useVp((s) => s.be?.tier ?? null)
  useEffect(() => {
    if (!tier) return
    const tmp = new Vector3()
    const L = new LabelLayer(tier, (enu, out) => (vp.camera ? projectEnu(vp.camera, vp.cssW, vp.cssH, enu, out, tmp) : false))
    L.mount(ref.current)
    vp.labels = L
    let visible = true
    const names = new Map<number, string>()
    let rosterVersion = -1
    const nameOf = (a: number): string => {
      const r = rtClient()?.roster
      if (r && r.version !== rosterVersion) {
        rosterVersion = r.version
        names.clear()
      }
      let s = names.get(a)
      if (s === undefined) {
        s = sanitizeText(r?.idOf(a) ?? String(a))
        names.set(a, s)
      }
      return s
    }
    const pos = new Float64Array(3)
    const q = new Float64Array(4)
    const vel = new Float64Array(3)
    L.setTextFn((kind, key, expanded, out: LabelText) => {
      const f = labelFormatter()
      if (kind === LabelKind.Goto) {
        const g = vp.mission?.goto.target
        out.id = g ? fmt.enu(g[0], g[1], g[2]) : ''
        out.sub = ''
        return
      }
      if (kind === LabelKind.Zone) {
        const z = vp.zones?.zones[key]
        out.id = z ? `${f(0, z.kind === 'nofly' ? 'zone.nofly' : 'zone.restricted', 'zh-CN')} · ${sanitizeText(z.labelZh ?? z.label)}` : ''
        out.sub = z ? fmt.alt(z.z1, 'world') : ''
        return
      }
      const d = vp.drones
      out.id = nameOf(key)
      const i = d ? d.layer.poseIndexOf(key) : -1
      if (!d || i < 0) {
        out.sub = ''
        return
      }
      const p = d.poses
      const fs = p.state[i]
      const stale = (d.layer.mark[key] & MARK.STALE) !== 0
      if (stale) out.sub = `${f(fs, 'hold', 'zh-CN')} ${fmt.stale(p.ageS[i])}`
      else if (expanded && d.fullPoseOf(key, pos, q, vel)) {
        out.sub = `${f(fs, 'state', 'zh-CN')} · ${fmt.alt(pos[2], 'world')} · ${fmt.speed(Math.hypot(vel[0], vel[1], vel[2]))} · ${p.battery[i] === 255 ? fmt.pct(null) : fmt.pct(p.battery[i])}`
      } else out.sub = f(fs, 'state', 'zh-CN')
    })
    const levels = tier === 'S' ? DECLUTTER.levelsS : DECLUTTER.levelsBA
    const knob: GovernorKnob = { step: 3, id: 'labels', levels: levels.length, labelKey: 'perf.governor.labels', apply: (l) => L.setCap(levels[l]) }
    const offs = [
      register('world', 'labels.layout', (ctx) => {
        const c = L.cand
        c.clear()
        if (!visible) {
          L.acceptedN = 0
          return
        }
        const d = vp.drones
        if (d) {
          const p = d.poses
          const cam = ctx.camera
          for (let i = 0; i < p.n; i++) {
            const a = p.agentNo[i]
            const m = d.layer.mark[a]
            if ((m & MARK.HIDDEN) !== 0) continue
            const lvl = d.layer.alert[a]
            const sel = (m & MARK.SELECTED) !== 0
            const red = (m & MARK.RED) !== 0
            const hover = (m & MARK.HOVER) !== 0
            if (!sel && !red && lvl === 0 && !hover && (m & MARK.FOCUSSET) === 0 && (m & MARK.STALE) === 0) continue
            const prio = sel ? LABEL_PRIO.selected : red ? LABEL_PRIO.red : lvl === 2 ? LABEL_PRIO.critical : lvl === 1 ? LABEL_PRIO.warning : hover ? LABEL_PRIO.hover : LABEL_PRIO.other
            const icon = (m & MARK.STALE) !== 0 ? LabelIcon.Hold : lvl === 2 ? LabelIcon.Critical : lvl === 1 ? LabelIcon.Warning : LabelIcon.None
            if (!c.add(LabelKind.Drone, a, p.pos[3 * i], p.pos[3 * i + 1], p.pos[3 * i + 2], prio, icon, red, hover)) break
            if (cam) c.dist[c.n - 1] = Math.hypot(p.pos[3 * i] - cam.position.x, p.pos[3 * i + 2] - cam.position.y, -p.pos[3 * i + 1] - cam.position.z)
          }
        }
        const g = vp.mission?.goto
        if (g?.visible) c.add(LabelKind.Goto, -1, g.target[0], g.target[1], g.target[2], LABEL_PRIO.goto, LabelIcon.Goto, false, false)
        L.layout(ctx)
      }, { order: 200, layer: 'labels' }),
      register('overlay', 'labels.write', (ctx) => L.write(ctx), { layer: 'labels' }),
      registerLayer({
        id: 'labels', owner: 'M06', perfKey: 'labels', root: null, channel: 0,
        caps: { S: { maxLabels: DECLUTTER.capS }, BA: { maxLabels: DECLUTTER.capBA } },
        drawCount: () => 0,
        setVisible: (v) => {
          visible = v
        },
        knobs: [knob],
        dispose: () => {},
      }),
      perf.registerKnob(knob),
    ]
    applyLayerVisibility()
    return () => {
      for (const off of offs) off()
      L.unmount()
      if (vp.labels === L) vp.labels = null
    }
  }, [tier])
  return <div ref={ref} className="app-layer-labels" data-slot="labels" aria-hidden="true" />
}

