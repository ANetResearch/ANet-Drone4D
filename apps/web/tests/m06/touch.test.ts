// D2.1 WP-11b (AWR-04 §15.8.4; D2-AC-48 Node part): the touch recogniser's thresholds and outputs, and the camera rig's
// touch methods (rotation like the mouse, pinch about the midpoint keeps that point under the fingers, pan, twist in Orbit
// and Bird, FPV ignores touches); the dispatch of the actions (the FPV one-finger drag path of M15-FR-218) and the edge
// band left to the system gestures (AWR-04 §15.16).
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera, Vector3 } from 'three'
import { CameraRig } from '@/engine/camera/CameraRig'
import { nearEdge, TOUCH, TouchGestures, touchDispatch, type TouchAction, type TouchCamera, type TouchDeps } from '@/viewport/input/touch'

function rec(): { g: TouchGestures; out: TouchAction[] } {
  const out: TouchAction[] = []
  return { g: new TouchGestures((a) => out.push(a)), out }
}
const types = (out: TouchAction[]): string[] => out.map((a) => a.type)

describe('touch recogniser', () => {
  it('tap, double tap, and a slow second tap is a tap again', () => {
    const { g, out } = rec()
    g.down(1, 100, 100, 0)
    g.up(1, 102, 101, 120)
    g.down(1, 110, 104, 300)
    g.up(1, 110, 104, 380)
    expect(types(out)).toEqual(['tap', 'doubleTap'])
    g.down(1, 100, 100, 1000)
    g.up(1, 100, 100, 1100)
    g.down(1, 100, 100, 1500)
    g.up(1, 100, 100, 1600)
    expect(types(out)).toEqual(['tap', 'doubleTap', 'tap', 'tap'])
    // too long or moved: no tap
    g.down(2, 0, 0, 5000)
    g.up(2, 0, 0, 5000 + TOUCH.tapMaxMs + 10)
    expect(out.length).toBe(4)
  })

  it('long press after 500 ms within 10 px; no tap afterwards', () => {
    const { g, out } = rec()
    g.down(1, 50, 60, 0)
    g.move(1, 54, 62, 200)
    g.tick(499)
    expect(out).toEqual([])
    g.tick(500)
    expect(out).toEqual([{ type: 'longPress', x: 54, y: 62 }])
    g.up(1, 54, 62, 700)
    expect(types(out)).toEqual(['longPress'])
    // moved 7 px: the drag (rotation) starts, no long press
    const r = rec()
    r.g.down(1, 0, 0, 0)
    r.g.move(1, 5, 0, 10)
    r.g.move(1, 5, 5, 20)
    r.g.tick(600)
    expect(types(r.out)).toEqual(['start'])
  })

  it('one finger rotates after 6 px, from the threshold point on', () => {
    const { g, out } = rec()
    g.down(1, 0, 0, 0)
    g.move(1, 4, 0, 10)
    expect(out).toEqual([])
    g.move(1, 6, 0, 20)
    expect(types(out)).toEqual(['start'])
    g.move(1, 16, 3, 30)
    expect(out[1]).toEqual({ type: 'rotate', dx: 10, dy: 3 })
    g.up(1, 16, 3, 40)
    expect(types(out)).toEqual(['start', 'rotate', 'end'])
  })

  it('two fingers: pinch after 4 %, pan after 8 px the same way, twist after 5 deg', () => {
    const { g, out } = rec()
    g.down(1, 100, 100, 0)
    g.down(2, 200, 100, 5)
    expect(types(out)).toEqual(['start'])
    g.move(2, 203, 100, 10) // 3 %: nothing
    expect(out.length).toBe(1)
    g.move(2, 205, 100, 20) // 5 %: pinch starts (no scale yet), then scale per move
    g.move(2, 215, 100, 30)
    const p = out.filter((a) => a.type === 'pinch') as { scale: number; cx: number }[]
    expect(p.length).toBe(1)
    expect(p[0].scale).toBeCloseTo(115 / 105, 6)
    expect(p[0].cx).toBeCloseTo(157.5, 6)
    // both fingers down 10 px: pan starts, then follows the midpoint
    g.move(1, 100, 110, 40)
    g.move(2, 215, 110, 50)
    expect(out.some((a) => a.type === 'pan')).toBe(false)
    g.move(1, 100, 120, 55)
    g.move(2, 215, 120, 56)
    const pans = out.filter((a) => a.type === 'pan') as { dx: number; dy: number }[]
    expect(pans.reduce((s, x) => s + x.dy, 0)).toBeCloseTo(10, 6)
    // twist: rotate finger 2 around finger 1 by 10 deg
    const a = (10 * Math.PI) / 180
    g.move(2, 100 + 115 * Math.cos(a), 120 + 115 * Math.sin(a), 60) // past 5 deg: the twist starts here
    g.move(2, 100 + 115 * Math.cos(2 * a), 120 + 115 * Math.sin(2 * a), 65)
    const tw = out.filter((x) => x.type === 'twist') as { dRad: number }[]
    expect(tw.length).toBe(1)
    expect(tw[0].dRad).toBeCloseTo(a, 6)
    g.up(2, 0, 0, 70)
    // the remaining finger does not rotate
    g.move(1, 160, 160, 80)
    expect(out.some((x) => x.type === 'rotate')).toBe(false)
    g.up(1, 160, 160, 90)
    expect(out[out.length - 1].type).toBe('end')
  })

  it('three fingers are not used', () => {
    const { g, out } = rec()
    g.down(1, 0, 0, 0)
    g.down(2, 100, 0, 1)
    g.down(3, 50, 80, 2)
    g.move(3, 50, 200, 10)
    g.move(1, 0, 100, 11)
    expect(out.filter((a) => a.type !== 'start').length).toBe(0)
  })
})

describe('camera rig touch methods', () => {
  const rig = (): CameraRig => {
    const cam = new PerspectiveCamera(60, 1, 0.5, 20000)
    const r = new CameraRig(cam, null)
    r.lookAtEnu([0, -100, 100], [0, 0, 0])
    r.controls.update(1)
    cam.updateMatrixWorld()
    return r
  }
  const settle = (r: CameraRig): void => {
    for (let i = 0; i < 120; i++) r.controls.update(1 / 30)
    r.camera.updateMatrixWorld()
  }

  it('one-finger rotation turns the azimuth like a mouse drag; FPV ignores touches', () => {
    const r = rig()
    const az0 = r.controls.azimuthAngle
    r.touchRotate(100, 0, 800)
    settle(r)
    expect(r.controls.azimuthAngle).toBeCloseTo(az0 - (2 * Math.PI * 100) / 800, 3)
    r.mode = 'fpv'
    const az1 = r.controls.azimuthAngle
    r.touchRotate(100, 0, 800)
    settle(r)
    expect(r.controls.azimuthAngle).toBe(az1)
  })

  it('pinch: distance / scale, the midpoint stays put; pan moves the target', () => {
    const r = rig()
    const d0 = r.controls.distance
    // the ground point under (600, 300) of an 800 x 600 viewport
    const ndc = new Vector3((600 / 800) * 2 - 1, 1 - (300 / 600) * 2, 0.5).unproject(r.camera)
    const eye = r.camera.position.clone()
    const dir = ndc.sub(eye).normalize()
    const p = eye.clone().add(dir.clone().multiplyScalar(d0))
    r.touchDolly(2, 600, 300, 800, 600)
    settle(r)
    expect(r.controls.distance).toBeCloseTo(d0 / 2, 3)
    const s = p.clone().project(r.camera)
    expect((s.x + 1) / 2 * 800).toBeCloseTo(600, 0)
    expect((1 - s.y) / 2 * 600).toBeCloseTo(300, 0)
    const t0 = new Vector3()
    r.controls.getTarget(t0)
    r.touchPan(100, 0, 600)
    settle(r)
    const t1 = new Vector3()
    r.controls.getTarget(t1)
    expect(t1.distanceTo(t0)).toBeGreaterThan(1)
  })

  it('twist yaws the orbit and moves the north-up lock in Bird', () => {
    const r = rig()
    const az0 = r.controls.azimuthAngle
    r.touchTwist(0.3)
    settle(r)
    expect(r.controls.azimuthAngle).toBeCloseTo(az0 + 0.3, 3)
    r.mode = 'bird'
    r.controls.minAzimuthAngle = 0
    r.controls.maxAzimuthAngle = 0
    r.touchTwist(0.2)
    settle(r)
    expect(r.controls.minAzimuthAngle).toBeCloseTo(r.controls.maxAzimuthAngle, 9)
    expect(r.controls.minAzimuthAngle).not.toBe(0)
  })
})

describe('touch dispatch and the edge band', () => {
  function deps(cam: TouchCamera | null): { d: TouchDeps; log: string[]; setCam(c: TouchCamera | null): void } {
    const log: string[] = []
    let c = cam
    const d: TouchDeps = {
      camera: () => c,
      size: () => ({ w: 800, h: 600 }),
      tap: (x, y) => log.push(`tap ${x},${y}`),
      doubleTap: (x, y) => log.push(`dbl ${x},${y}`),
      longPress: (x, y, cx, cy) => log.push(`long ${x},${y} ${cx},${cy}`),
      drag: (p, dx, dy) => log.push(`drag ${p} ${dx},${dy}`),
    }
    return { d, log, setCam: (n) => (c = n) }
  }
  function cam(log: string[]): TouchCamera {
    return {
      touchActive: (on) => log.push(`active ${on}`),
      touchRotate: (dx, dy, h) => log.push(`rotate ${dx},${dy} ${h}`),
      touchPan: (dx, dy) => log.push(`pan ${dx},${dy}`),
      touchDolly: (s) => log.push(`dolly ${s}`),
      touchTwist: (r) => log.push(`twist ${r}`),
    }
  }

  it('with a touch camera the one-finger drag rotates; without one (FPV) it is a drag start, move, end', () => {
    const camLog: string[] = []
    const a = deps(cam(camLog))
    const fa = touchDispatch(a.d, () => ({ x: 10, y: 20 }))
    fa({ type: 'start' })
    fa({ type: 'rotate', dx: 5, dy: -2 })
    fa({ type: 'end' })
    expect(camLog).toEqual(['active true', 'rotate 5,-2 600', 'active false'])
    expect(a.log).toEqual([])
    const b = deps(null)
    const fb = touchDispatch(b.d, () => ({ x: 10, y: 20 }))
    fb({ type: 'start' })
    fb({ type: 'rotate', dx: 7, dy: 1 })
    fb({ type: 'rotate', dx: 3, dy: -4 })
    // the camera comes back mid-drag: the drag still ends on the drag path
    b.setCam(cam(camLog))
    fb({ type: 'rotate', dx: 1, dy: 1 })
    fb({ type: 'end' })
    expect(b.log).toEqual(['drag start 7,1', 'drag move 3,-4', 'drag move 1,1', 'drag end 0,0'])
    fb({ type: 'longPress', x: 3, y: 4 })
    expect(b.log.at(-1)).toBe('long 3,4 13,24')
  })

  it('a two-finger gesture without a camera is no drag; taps still reach the click path', () => {
    const b = deps(null)
    const g = new TouchGestures(touchDispatch(b.d, () => ({ x: 0, y: 0 })))
    g.down(1, 100, 100, 0)
    g.down(2, 200, 100, 5)
    g.move(1, 80, 100, 20)
    g.move(2, 220, 100, 21)
    g.up(1, 80, 100, 40)
    g.up(2, 220, 100, 41)
    expect(b.log).toEqual([])
    g.down(3, 300, 300, 1000)
    g.up(3, 300, 300, 1050)
    expect(b.log).toEqual(['tap 300,300'])
    // one finger past the 6 px threshold: drag start, moves, end on lift
    g.down(4, 300, 300, 3000)
    g.move(4, 310, 300, 3010)
    g.move(4, 320, 305, 3020)
    g.up(4, 320, 305, 3030)
    expect(b.log.slice(1)).toEqual(['drag start 10,5', 'drag end 0,0'])
  })

  it('the 16 px edge band of the viewport', () => {
    expect(TOUCH.edgePx).toBe(16)
    expect(nearEdge(15.9, 300, 800, 600)).toBe(true)
    expect(nearEdge(16, 300, 800, 600)).toBe(false)
    expect(nearEdge(400, 10, 800, 600)).toBe(true)
    expect(nearEdge(790, 300, 800, 600)).toBe(true)
    expect(nearEdge(400, 590, 800, 600)).toBe(true)
    expect(nearEdge(400, 300, 800, 600)).toBe(false)
    // unknown size: never
    expect(nearEdge(1, 1, 0, 0)).toBe(false)
  })
})
