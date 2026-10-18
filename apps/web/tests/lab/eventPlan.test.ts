// Event plan helpers (P0a-2 event replay): UAV script positions equal awr.sensorsvc.capture.path_state (horizontal part),
// trails from the golden tape, plan frame and scale bar.
import { describe, expect, it } from 'vitest'
import { TapeReader } from '@/engine'
import { actorTrails, placeLabels, planFrame, scaleBar, toPx, uavXY, type UavPath } from '@/ui/lab/wall/eventPlan'
import { readBytes } from '../contracts/util'

const orbit: UavPath = { kind: 'orbit', center: [13, -41], radius_m: 70, period_s: 180, phase_deg: 200 }
const orbitCw: UavPath = { kind: 'orbit', center: [40, -50], radius_m: 90, period_s: 240, phase_deg: 30, ccw: false }
const wps: UavPath = { kind: 'waypoints', points: [[0, -10, -60, 35, 0], [45, 30, -60, 35, 0], [90, 70, -70, 35, 0]] }

describe('event plan', () => {
  it.each([
    [orbit, 0, [-52.778483455013586, -64.94141003279681]],
    [orbit, 37.3, [18.613924703020135, -110.77452149193729]],
    [orbitCw, 12.5, [128.27067523629074, -32.441871018548454]],
    [wps, -1, [-10, -60]],
    [wps, 45, [30, -60]],
    [wps, 60.5, [43.77777777777778, -63.44444444444444]],
    [wps, 200, [70, -70]],
  ] as const)('uavXY matches the capture path rules (%#)', (p, t, xy) => {
    const [x, y] = uavXY(p, t)
    expect(x).toBeCloseTo(xy[0], 9)
    expect(y).toBeCloseTo(xy[1], 9)
  })

  it('samples visible trails from the tape and splits them at gaps', () => {
    const r = new TapeReader(readBytes('golden/event/tape-small.bin'))
    const tr = actorTrails(r, 1)
    expect(tr.map((a) => a.id)).toEqual(['person_A', 'vehicle_C'])
    expect(tr[0]!.segs).toHaveLength(1)                     // visible 0.5–15 s, then in a vehicle and gone
    expect(tr[0]!.segs[0]!).toHaveLength(14)                 // samples at 1 … 14 s
    expect(tr[0]!.segs[0]![0]![0]).toBeCloseTo(-12.425, 9)
    expect(tr[1]!.segs).toHaveLength(1)
    expect(tr[1]!.segs[0]!).toHaveLength(25)
    const win = actorTrails(r, 1, 10, 12)
    expect(win[0]!.segs[0]!).toHaveLength(3)                 // 10, 11, 12 s
  })

  it('moves overlapping labels down a line at a time', () => {
    expect(placeLabels([{ x: 0, y: 10, w: 40 }, { x: 10, y: 12, w: 40 }, { x: 100, y: 10, w: 20 }, { x: 5, y: 15, w: 30 }])).toEqual([10, 23, 10, 37])
  })

  it('fits a square north-up frame and picks a 1-2-5 scale bar', () => {
    const f = planFrame([[0, 0], [100, 20]], 0)
    expect(f).toEqual({ x0: 0, y0: -40, size: 100 })
    expect(toPx(f, 200, 0, 60)).toEqual([0, 0])
    expect(toPx(f, 200, 100, -40)).toEqual([200, 200])
    expect(planFrame([])).toEqual({ x0: -25, y0: -25, size: 50 })
    expect([scaleBar(100), scaleBar(230), scaleBar(600), scaleBar(55)]).toEqual([20, 20, 100, 10])
  })
})
