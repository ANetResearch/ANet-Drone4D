// Viewport touch gestures (D2.1 WP-11b; AWR-04 §15.8.4; ADR-122; AWR-14 §20.26.5). Owner: M06.
// TouchGestures is a pure recogniser over touch pointers (CSS px, ms):
//   one finger    moved >= 6 px -> rotate (Orbit, Third, Free; Bird: pan); long press 500 ms within 10 px -> context menu;
//                 tap (< 6 px, < 300 ms) -> select; two taps within 300 ms and 24 px -> double tap (focus, as D1 double click)
//   two fingers   distance change >= 4 % -> pinch (zoom about the midpoint); both fingers moved >= 8 px the same way -> pan;
//                 angle change >= 5 deg -> twist (yaw in Orbit and Bird); the three may run together once started
//   three fingers not used (system gestures)
// Lifting a finger of a two-finger gesture ends it; the remaining finger does not start a rotation until all are up.
// installTouch binds it to the viewport host: touch-action none on the canvas host (no browser scroll or zoom there; inline
// here and by class in WorldCanvas, since camera-controls clears the inline value while disabled in FPV), the
// camera rig's touch methods (camera-controls' own touch actions are off, engine/camera/CameraRig.ts), the click path of
// viewport/interaction.ts for taps, and the 'viewport.longpress' event {x, y, clientX, clientY, pick} for M15's anchored
// DropdownMenu (Base UI ContextMenu does not open on a long press in iOS Safari). Without a touch camera (the FPV camera)
// the one-finger drag goes to deps.drag (start, move, end: the FPV gimbal drag of viewport/interaction.ts, M15-FR-218).
// A first finger that lands within TOUCH.edgePx of the host's edge starts nothing (AWR-04 §15.16: the system edge
// gestures, the back swipe and the notification shade, keep their edge).
export const TOUCH = {
  dragStartPx: 6,
  pinchFrac: 0.04,
  panPx: 8,
  twistRad: (5 * Math.PI) / 180,
  longPressMs: 500,
  longPressTolPx: 10,
  tapMaxMs: 300,
  tapTolPx: 6,
  dblTapMs: 300,
  dblTapPx: 24,
  /** a sequence whose first finger lands this close to the viewport edge is left to the system (CSS px) */
  edgePx: 16,
} as const

export type TouchAction =
  | { type: 'start' }
  | { type: 'rotate'; dx: number; dy: number }
  | { type: 'pan'; dx: number; dy: number }
  | { type: 'pinch'; scale: number; cx: number; cy: number }
  | { type: 'twist'; dRad: number; cx: number; cy: number }
  | { type: 'tap'; x: number; y: number }
  | { type: 'doubleTap'; x: number; y: number }
  | { type: 'longPress'; x: number; y: number }
  | { type: 'end' }

interface Ptr { x0: number; y0: number; x: number; y: number; t0: number }
type Phase = 'idle' | 'pending' | 'rotate' | 'two' | 'consumed'

const wrapPi = (a: number): number => Math.atan2(Math.sin(a), Math.cos(a))

export class TouchGestures {
  private readonly ptrs = new Map<number, Ptr>()
  private phase: Phase = 'idle'
  private lastX = 0
  private lastY = 0
  private lastTap: { x: number; y: number; t: number } | null = null
  private longAt = Number.POSITIVE_INFINITY
  // two-finger state
  private d0 = 0
  private dLast = 0
  private a0 = 0
  private aLast = 0
  private mx = 0
  private my = 0
  private pinch = false
  private pan = false
  private twist = false
  private started = false

  constructor(private readonly emit: (a: TouchAction) => void) {}

  get active(): boolean {
    return this.ptrs.size > 0
  }
  get state(): Phase {
    return this.phase
  }

  down(id: number, x: number, y: number, t: number): void {
    this.ptrs.set(id, { x0: x, y0: y, x, y, t0: t })
    const n = this.ptrs.size
    if (n === 1) {
      this.phase = 'pending'
      this.lastX = x
      this.lastY = y
      this.longAt = t + TOUCH.longPressMs
      this.started = false
    } else if (n === 2 && (this.phase === 'pending' || this.phase === 'rotate' || this.phase === 'idle')) {
      this.phase = 'two'
      this.longAt = Number.POSITIVE_INFINITY
      const [a, b] = [...this.ptrs.values()]
      this.d0 = this.dLast = Math.hypot(b.x - a.x, b.y - a.y)
      this.a0 = this.aLast = Math.atan2(b.y - a.y, b.x - a.x)
      this.mx = (a.x + b.x) / 2
      this.my = (a.y + b.y) / 2
      for (const p of this.ptrs.values()) {
        p.x0 = p.x
        p.y0 = p.y
      }
      this.pinch = this.pan = this.twist = false
      this.begin()
    } else {
      this.phase = 'consumed'
      this.longAt = Number.POSITIVE_INFINITY
    }
  }

  move(id: number, x: number, y: number, t: number): void {
    const p = this.ptrs.get(id)
    if (!p) return
    p.x = x
    p.y = y
    this.tick(t)
    if (this.phase === 'pending') {
      if (Math.hypot(x - p.x0, y - p.y0) >= TOUCH.dragStartPx) {
        this.phase = 'rotate'
        this.longAt = Number.POSITIVE_INFINITY
        this.lastX = x
        this.lastY = y
        this.begin()
      }
      return
    }
    if (this.phase === 'rotate') {
      const dx = x - this.lastX
      const dy = y - this.lastY
      this.lastX = x
      this.lastY = y
      if (dx !== 0 || dy !== 0) this.emit({ type: 'rotate', dx, dy })
      return
    }
    if (this.phase !== 'two' || this.ptrs.size !== 2) return
    const [a, b] = [...this.ptrs.values()]
    const d = Math.hypot(b.x - a.x, b.y - a.y)
    const ang = Math.atan2(b.y - a.y, b.x - a.x)
    const mx = (a.x + b.x) / 2
    const my = (a.y + b.y) / 2
    if (!this.pinch && this.d0 > 0 && Math.abs(d / this.d0 - 1) >= TOUCH.pinchFrac) {
      this.pinch = true
      this.dLast = d
    }
    if (!this.twist && Math.abs(wrapPi(ang - this.a0)) >= TOUCH.twistRad) {
      this.twist = true
      this.aLast = ang
    }
    if (!this.pan) {
      const ax = a.x - a.x0, ay = a.y - a.y0, bx = b.x - b.x0, by = b.y - b.y0
      if (Math.hypot(ax, ay) >= TOUCH.panPx && Math.hypot(bx, by) >= TOUCH.panPx && ax * bx + ay * by > 0) {
        this.pan = true
        this.mx = mx
        this.my = my
      }
    }
    if (this.pinch && this.dLast > 0 && d > 0 && d !== this.dLast) {
      this.emit({ type: 'pinch', scale: d / this.dLast, cx: mx, cy: my })
      this.dLast = d
    }
    if (this.twist && ang !== this.aLast) {
      this.emit({ type: 'twist', dRad: wrapPi(ang - this.aLast), cx: mx, cy: my })
      this.aLast = ang
    }
    if (this.pan && (mx !== this.mx || my !== this.my)) {
      this.emit({ type: 'pan', dx: mx - this.mx, dy: my - this.my })
      this.mx = mx
      this.my = my
    }
  }

  up(id: number, x: number, y: number, t: number): void {
    const p = this.ptrs.get(id)
    if (!p) return
    p.x = x
    p.y = y
    this.tick(t)
    this.ptrs.delete(id)
    if (this.phase === 'pending' && this.ptrs.size === 0) {
      if (t - p.t0 < TOUCH.tapMaxMs && Math.hypot(x - p.x0, y - p.y0) < TOUCH.tapTolPx) {
        const l = this.lastTap
        if (l && t - l.t < TOUCH.dblTapMs && Math.hypot(x - l.x, y - l.y) < TOUCH.dblTapPx) {
          this.lastTap = null
          this.emit({ type: 'doubleTap', x, y })
        } else {
          this.lastTap = { x, y, t }
          this.emit({ type: 'tap', x, y })
        }
      }
    } else if (this.phase === 'two' || this.phase === 'rotate') this.phase = this.ptrs.size > 0 ? 'consumed' : 'idle'
    if (this.ptrs.size === 0) this.finish()
  }

  cancel(id: number): void {
    if (!this.ptrs.delete(id)) return
    this.phase = this.ptrs.size > 0 ? 'consumed' : 'idle'
    if (this.ptrs.size === 0) this.finish()
  }

  /** timers: the long press fires here (pointer moves and a periodic check call it) */
  tick(t: number): void {
    if (this.phase !== 'pending' || t < this.longAt || this.ptrs.size !== 1) return
    const p = this.ptrs.values().next().value as Ptr
    this.longAt = Number.POSITIVE_INFINITY
    if (Math.hypot(p.x - p.x0, p.y - p.y0) >= TOUCH.longPressTolPx) return
    this.phase = 'consumed'
    this.emit({ type: 'longPress', x: p.x, y: p.y })
  }

  private begin(): void {
    if (this.started) return
    this.started = true
    this.emit({ type: 'start' })
  }

  private finish(): void {
    this.longAt = Number.POSITIVE_INFINITY
    if (this.started) this.emit({ type: 'end' })
    this.started = false
    this.phase = 'idle'
  }
}

/** the camera side of the gestures (engine/camera CameraRig) */
export interface TouchCamera {
  touchActive(on: boolean): void
  touchRotate(dx: number, dy: number, hPx: number): void
  touchPan(dx: number, dy: number, hPx: number): void
  touchDolly(scale: number, cx: number, cy: number, wPx: number, hPx: number): void
  touchTwist(dRad: number): void
}

export type TouchDragPhase = 'start' | 'move' | 'end'

export interface TouchDeps {
  camera(): TouchCamera | null
  size(): { w: number; h: number }
  tap(x: number, y: number): void
  doubleTap(x: number, y: number): void
  longPress(x: number, y: number, clientX: number, clientY: number): void
  /** any touch input (the power-save wake of static browsing) */
  input?(): void
  /** one-finger drag while there is no touch camera (FPV): CSS px since the last call; 'end' with zeros */
  drag?(phase: TouchDragPhase, dx: number, dy: number): void
}

/** the point (host CSS px) lies within TOUCH.edgePx of the host's edge (w, h: host size; 0: unknown, never) */
export function nearEdge(x: number, y: number, w: number, h: number): boolean {
  if (!(w > 0) || !(h > 0)) return false
  const e = TOUCH.edgePx
  return x < e || y < e || x > w - e || y > h - e
}

/**
 * the recogniser's actions to the deps: camera gestures to the touch camera, the one-finger drag to deps.drag when there
 * is no camera (a drag started on the drag path ends there even if the camera comes back), taps and the long press to the
 * click path; origin: the host's client offset for the long press
 */
export function touchDispatch(deps: TouchDeps, origin: () => { x: number; y: number }): (a: TouchAction) => void {
  let dragging = false
  return (a) => {
    const cam = deps.camera()
    const { w, h } = deps.size()
    switch (a.type) {
      case 'start': cam?.touchActive(true); break
      case 'end':
        cam?.touchActive(false)
        if (dragging) {
          dragging = false
          deps.drag?.('end', 0, 0)
        }
        break
      case 'rotate':
        if (dragging || !cam) {
          if (!deps.drag) break
          deps.drag(dragging ? 'move' : 'start', a.dx, a.dy)
          dragging = true
        } else cam.touchRotate(a.dx, a.dy, h)
        break
      case 'pan': cam?.touchPan(a.dx, a.dy, h); break
      case 'pinch': cam?.touchDolly(a.scale, a.cx, a.cy, w, h); break
      case 'twist': cam?.touchTwist(a.dRad); break
      case 'tap': deps.tap(a.x, a.y); break
      case 'doubleTap': deps.doubleTap(a.x, a.y); break
      case 'longPress': {
        const o = origin()
        deps.longPress(a.x, a.y, a.x + o.x, a.y + o.y)
        break
      }
    }
  }
}

/** bind the recogniser to the host's touch pointers; returns the uninstall function */
export function installTouch(host: HTMLElement, deps: TouchDeps): () => void {
  host.style.touchAction = 'none'
  let ox = 0
  let oy = 0
  let hw = 0
  let hh = 0
  const measure = (): void => {
    const r = host.getBoundingClientRect()
    ox = r.left
    oy = r.top
    hw = r.width
    hh = r.height
  }
  measure()
  const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
  ro?.observe(host)
  const origin = { x: 0, y: 0 }
  const g = new TouchGestures(touchDispatch(deps, () => {
    origin.x = ox
    origin.y = oy
    return origin
  }))
  // pointers of a sequence that began at the edge (left to the system): ignored until they lift
  const ignored = new Set<number>()
  let timer: ReturnType<typeof setInterval> | null = null
  const touch = (e: PointerEvent): boolean => e.pointerType === 'touch'
  const onDown = (e: PointerEvent): void => {
    if (!touch(e)) return
    deps.input?.()
    const x = e.clientX - ox
    const y = e.clientY - oy
    if (ignored.size > 0 || (!g.active && nearEdge(x, y, hw, hh))) {
      ignored.add(e.pointerId)
      return
    }
    g.down(e.pointerId, x, y, e.timeStamp)
    try {
      host.setPointerCapture(e.pointerId)
    } catch {
      /* synthetic pointers (tests) cannot be captured */
    }
    timer ??= setInterval(() => {
      g.tick(performance.now())
      if (!g.active && timer) {
        clearInterval(timer)
        timer = null
      }
    }, 50)
  }
  const onMove = (e: PointerEvent): void => {
    if (!touch(e) || !g.active || ignored.has(e.pointerId)) return
    deps.input?.()
    g.move(e.pointerId, e.clientX - ox, e.clientY - oy, e.timeStamp)
  }
  const onUp = (e: PointerEvent): void => {
    if (!touch(e) || ignored.delete(e.pointerId)) return
    g.up(e.pointerId, e.clientX - ox, e.clientY - oy, e.timeStamp)
  }
  const onCancel = (e: PointerEvent): void => {
    if (!touch(e) || ignored.delete(e.pointerId)) return
    g.cancel(e.pointerId)
  }
  host.addEventListener('pointerdown', onDown)
  host.addEventListener('pointermove', onMove)
  host.addEventListener('pointerup', onUp)
  host.addEventListener('pointercancel', onCancel)
  return () => {
    ro?.disconnect()
    if (timer) clearInterval(timer)
    host.removeEventListener('pointerdown', onDown)
    host.removeEventListener('pointermove', onMove)
    host.removeEventListener('pointerup', onUp)
    host.removeEventListener('pointercancel', onCancel)
  }
}
