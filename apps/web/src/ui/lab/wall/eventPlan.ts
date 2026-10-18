// Event plan view helpers (P0a-2 event replay on the wall; M23 §6.8; AWR-14 §21.7.3 evaluation view). Pure functions: UAV
// script positions from the run manifest (same rules as awr.sensorsvc.capture.path_state, horizontal only), scripted actor
// trails sampled from the tape, and the square plan frame (ENU, north up) that fits both.
import type { TapeReader } from '@/engine'

export interface OrbitPath { kind: 'orbit'; center?: [number, number]; radius_m: number; period_s: number; phase_deg?: number; ccw?: boolean }
export interface WaypointPath { kind: 'waypoints'; points: [number, number, number, number, number][] }
export type UavPath = OrbitPath | WaypointPath

/** UAV ground-track position (ENU m) at capture time tS. */
export function uavXY(path: UavPath, tS: number): [number, number] {
  if (path.kind === 'orbit') {
    const [cx, cy] = path.center ?? [0, 0]
    const w = ((2 * Math.PI) / path.period_s) * (path.ccw === false ? -1 : 1)
    const a = ((path.phase_deg ?? 0) * Math.PI) / 180 + w * tS
    return [cx + path.radius_m * Math.cos(a), cy + path.radius_m * Math.sin(a)]
  }
  const wp = path.points
  const t = Math.min(Math.max(tS, wp[0]![0]), wp[wp.length - 1]![0])
  let i = 0
  while (i < wp.length - 2 && wp[i + 1]![0] <= t) i++
  const a = wp[i]!
  const b = wp[i + 1] ?? a
  const u = (t - a[0]) / Math.max(b[0] - a[0], 1e-9)
  return [(1 - u) * a[1] + u * b[1], (1 - u) * a[2] + u * b[2]]
}

/** Scripted actor trails: one polyline per actor, visible samples (moving or waiting) every stepS seconds of event time in
 * [fromS, toS] (default: the whole tape), split at gaps. */
export function actorTrails(r: TapeReader, stepS = 1, fromS = 0, toS = Infinity): { idx: number; id: string; segs: [number, number][][] }[] {
  const ids = Array.from({ length: r.nScripted }, (_, i) => i)
  const out = ids.map((i) => ({ idx: i, id: r.header.actors[i]!.id, segs: [] as [number, number][][] }))
  const open: ([number, number][] | null)[] = ids.map(() => null)
  const endS = Math.min((r.nTicks - 1) / 10, toS)
  for (let t = Math.max(0, fromS); t <= endS + 1e-9; t += stepS) {
    const st = r.actorStates(Math.round(t * 1e9), ids)
    for (let j = 0; j < ids.length; j++) {
      const vis = st.state[j] === 1 || st.state[j] === 2
      if (!vis) {
        open[j] = null
        continue
      }
      if (!open[j]) {
        open[j] = []
        out[j]!.segs.push(open[j]!)
      }
      open[j]!.push([st.pos[3 * j]!, st.pos[3 * j + 1]!])
    }
  }
  return out
}

export interface PlanFrame { x0: number; y0: number; size: number }

/** Square frame (m) around all points with a relative margin; minimum side 50 m. */
export function planFrame(points: Iterable<[number, number]>, margin = 0.08): PlanFrame {
  let xa = Infinity
  let xb = -Infinity
  let ya = Infinity
  let yb = -Infinity
  for (const [x, y] of points) {
    xa = Math.min(xa, x)
    xb = Math.max(xb, x)
    ya = Math.min(ya, y)
    yb = Math.max(yb, y)
  }
  if (!Number.isFinite(xa)) return { x0: -25, y0: -25, size: 50 }
  const size = Math.max(xb - xa, yb - ya, 50) * (1 + 2 * margin)
  return { x0: (xa + xb) / 2 - size / 2, y0: (ya + yb) / 2 - size / 2, size }
}

/** Plan frame → SVG pixel (north up). */
export function toPx(f: PlanFrame, px: number, x: number, y: number): [number, number] {
  return [((x - f.x0) / f.size) * px, (1 - (y - f.y0) / f.size) * px]
}

/** Scale bar length (m) from the 1–2–5 series, about a fifth of the frame. */
export function scaleBar(size: number): number {
  const target = size / 5
  const p = 10 ** Math.floor(Math.log10(target))
  for (const k of [5, 2, 1]) if (k * p <= target) return k * p
  return p
}

/** Label placement without overlap: each label keeps its anchor x and moves down in line steps until its box is free. */
export function placeLabels(items: { x: number; y: number; w: number }[], lineH = 11): number[] {
  const placed: { x: number; y: number; w: number }[] = []
  return items.map((it) => {
    let y = it.y
    for (let k = 0; k < 8 && placed.some((p) => it.x < p.x + p.w && p.x < it.x + it.w && Math.abs(p.y - y) < lineH); k++) y += lineH
    placed.push({ x: it.x, y, w: it.w })
    return y
  })
}
