// Viewport pointer input (M06 §6.12; AWR-14 §6.1, §6.5-§6.7). Owner: M06.
// Click (primary button, movement below input.clickTolPx): drones first (CPU ray-sphere, hot zone >= 12 CSS px) ->
// select; otherwise M04 ray_hit -> the viewport's ground pick (GoTo preview marker; the M15 tool state machine reads it
// through the facade). Double click on the ground: orbit target to the hit, distance kept (camera flight). Pointer move:
// hover pick at <= 20 Hz (off while the camera moves) -> stores/selection hover (ring and expanded label).
// Everything asynchronous; no layout reads on the hot path (the host rect is read on pointer events only).
// D2.1 (WP-11b; AWR-04 §15.7.7, §15.8.4): touch pointers go to viewport/input/touch.ts (tap = click, double tap = double
// click, long press = 'viewport.longpress' {x, y, clientX, clientY, pick} for M15's anchored menu), the mouse path ignores
// them and the dblclick the browser synthesises after a double tap; with wake-on-input (viewer queue static browsing, power
// save) any input on the canvas renders frames for 2 s while the loop is suspended.
// FPV drag (M15-FR-218; request d2-WP-12b-to-M06 item 5): in the FPV camera (no camera controls there) a primary-button
// drag past the click tolerance, or a one-finger touch drag, emits 'fpv.drag' {dxRad, dyRad, phase} for the gimbal
// commands of M15 (facade fpv.onDrag); the angles come from the FPV projection at the image centre and the LWIR digital
// zoom. A drag is never a click.
import { INPUT } from '@/lib/tokens/input.gen'
import { selection } from '@/stores/selection'
import { events, loop, type PickResult } from '@/engine'
import { vp, type PointPickInfo } from './session'
import { gotoTargetFor, primaryAgentNo } from './gotoRule'
import { installTouch, type TouchDragPhase } from './input/touch'
import { sv } from './sensorView/state'

/** D2.1: frames after an input while suspended in static browsing (AWR-04 §15.7.7 item 3) */
export const WAKE_MS = 2000
let wakeOnInput = false
/** static browsing of the viewer queue: input on the canvas wakes the suspended loop (M15 PowerController) */
export function setWakeOnInput(on: boolean): void {
  wakeOnInput = on
}
function wake(): void {
  if (wakeOnInput && loop.suspended) loop.wake(WAKE_MS)
}

/** payload of 'viewport.longpress' (M15 opens the context menu anchored at clientX, clientY) */
export interface LongPress { x: number; y: number; clientX: number; clientY: number; pick: Promise<PickResult> }

/**
 * payload of 'fpv.drag': the angles (rad) the pointer moved through in the FPV image since the previous event, x to the
 * right and y downwards of the image ('start' carries the motion since the press, 'end' zeros). M15 turns them into
 * uav/{id}/cmd/gimbal angles: dragging the scene (the content follows the finger) turns the camera dxRad towards the left
 * of the image and dyRad upwards (pitch += dyRad; the sign of yaw follows M13's gimbal yaw_rad convention).
 */
export interface FpvDrag { dxRad: number; dyRad: number; phase: TouchDragPhase }

/**
 * CSS px to radians at the centre of the FPV image: [x, y] from the FPV projection (elements 0 and 5 are the focal
 * lengths in NDC units, so f_px = P * size / 2) and the LWIR digital zoom (display magnification); [0, 0] outside FPV
 */
export function fpvRadPerPx(out: [number, number] = [0, 0]): [number, number] {
  out[0] = 0
  out[1] = 0
  const rig = vp.rig
  if (!rig || rig.mode !== 'fpv' || !(vp.cssW > 0) || !(vp.cssH > 0)) return out
  const P = rig.camera.projectionMatrix.elements
  const z = sv.mode === 'lwir' && sv.digitalZoom > 1 ? sv.digitalZoom : 1
  const fx = (Math.abs(P[0]) * vp.cssW) / 2
  const fy = (Math.abs(P[5]) * vp.cssH) / 2
  out[0] = fx > 0 ? 1 / (fx * z) : 0
  out[1] = fy > 0 ? 1 / (fy * z) : 0
  return out
}

const radPx: [number, number] = [0, 0]
let fpvDragging = false
/** a drag on the FPV path (mouse or touch), CSS px; 'end' with zeros; emitted only while the camera is FPV or a drag runs */
function fpvDrag(phase: TouchDragPhase, dxPx: number, dyPx: number): void {
  if (phase === 'start') {
    if (vp.rig?.mode !== 'fpv') return
    fpvDragging = true
  } else if (!fpvDragging) return
  if (phase === 'end') fpvDragging = false
  fpvRadPerPx(radPx)
  events.emit('fpv.drag', { dxRad: dxPx * radPx[0], dyRad: dyPx * radPx[1], phase } satisfies FpvDrag)
}

/**
 * a consumer of viewport clicks and double clicks (M15 route and area editor): while installed it sees every click first
 * and returns true when it handled it, so the selection, the ground pick, the GoTo preview marker and the double-click
 * retarget do not happen (the camera controls still get the pointer stream)
 */
export type ClickConsumer = (kind: 'click' | 'dblclick', cssX: number, cssY: number) => boolean
let consumer: ClickConsumer | null = null
export function setClickConsumer(fn: ClickConsumer | null): void {
  consumer = fn
}

export function installInteraction(host: HTMLElement): () => void {
  let down: { x: number; y: number; id: number } | null = null
  // FPV mouse drag: the last position once the drag passed the click tolerance (null: no drag yet)
  let fd: { x: number; y: number } | null = null
  const endFpvDrag = (): boolean => {
    if (!fd) return false
    fd = null
    fpvDrag('end', 0, 0)
    return true
  }
  // host origin cached on resize (no layout read per pointer move)
  let ox = 0
  let oy = 0
  const measure = (): void => {
    const r = host.getBoundingClientRect()
    ox = r.left
    oy = r.top
  }
  measure()
  const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
  ro?.observe(host)
  const local = (e: MouseEvent): [number, number] => [e.clientX - ox, e.clientY - oy]
  let lastTouchMs = Number.NEGATIVE_INFINITY
  const isTouch = (e: PointerEvent): boolean => {
    if (e.pointerType !== 'touch') return false
    lastTouchMs = performance.now()
    return true
  }
  const onDown = (e: PointerEvent): void => {
    wake()
    if (isTouch(e) || e.button !== 0) return
    endFpvDrag() // a drag whose release never arrived ends before the next press
    down = { x: e.clientX, y: e.clientY, id: e.pointerId }
  }
  const onUp = (e: PointerEvent): void => {
    if (isTouch(e)) return
    const d = down
    down = null
    if (d && d.id === e.pointerId && endFpvDrag()) return
    if (!d || e.button !== 0 || d.id !== e.pointerId) return
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > INPUT.clickTolPx) return
    const [x, y] = local(e)
    if (consumer?.('click', x, y)) return
    void clickAt(x, y, vp.cssW, vp.cssH)
  }
  const onMove = (e: PointerEvent): void => {
    if (e.buttons !== 0) wake()
    const d = down
    if (d && !isTouch(e) && (e.buttons & 1) !== 0 && d.id === e.pointerId) {
      if (fd) {
        fpvDrag('move', e.clientX - fd.x, e.clientY - fd.y)
        fd.x = e.clientX
        fd.y = e.clientY
      } else if (vp.rig?.mode === 'fpv' && Math.hypot(e.clientX - d.x, e.clientY - d.y) > INPUT.clickTolPx) {
        fd = { x: e.clientX, y: e.clientY }
        try {
          host.setPointerCapture(e.pointerId)
        } catch {
          /* synthetic pointers cannot be captured */
        }
        fpvDrag('start', e.clientX - d.x, e.clientY - d.y)
      }
      return
    }
    if (isTouch(e) || e.buttons !== 0 || !vp.picker) return
    const [x, y] = local(e)
    const r = vp.picker.hoverAt(x, y)
    if (r === null) return
    selection.setHover(r.kind === 'drone' ? r.id : null)
  }
  const onLeave = (): void => selection.setHover(null)
  const onCancel = (e: PointerEvent): void => {
    if (isTouch(e) || !down || down.id !== e.pointerId) return
    down = null
    endFpvDrag()
  }
  const onDbl = (e: MouseEvent): void => {
    // the browser's dblclick after a double tap: the touch recogniser already handled it
    if (performance.now() - lastTouchMs < 800) return
    const [x, y] = local(e)
    if (consumer?.('dblclick', x, y)) return
    void dblclickAt(x, y)
  }
  const onWheel = (): void => wake()
  const offTouch = installTouch(host, {
    camera: () => (vp.rig && vp.rig.mode !== 'fpv' ? vp.rig : null),
    size: () => ({ w: vp.cssW, h: vp.cssH }),
    tap: (x, y) => {
      if (consumer?.('click', x, y)) return
      void clickAt(x, y, vp.cssW, vp.cssH)
    },
    doubleTap: (x, y) => {
      if (consumer?.('dblclick', x, y)) return
      void dblclickAt(x, y)
    },
    longPress: (x, y, clientX, clientY) => {
      const pick: Promise<PickResult> = vp.picker ? vp.picker.pickAt(x, y, { want: ['drone', 'ground'] }) : Promise.resolve({ kind: 'none' })
      events.emit('viewport.longpress', { x, y, clientX, clientY, pick } satisfies LongPress)
    },
    input: wake,
    drag: fpvDrag,
  })
  host.addEventListener('pointerdown', onDown)
  host.addEventListener('pointerup', onUp)
  host.addEventListener('pointermove', onMove)
  host.addEventListener('pointercancel', onCancel)
  host.addEventListener('pointerleave', onLeave)
  host.addEventListener('dblclick', onDbl)
  host.addEventListener('wheel', onWheel, { passive: true })
  return () => {
    offTouch()
    host.removeEventListener('wheel', onWheel)
    ro?.disconnect()
    host.removeEventListener('pointerdown', onDown)
    host.removeEventListener('pointerup', onUp)
    host.removeEventListener('pointermove', onMove)
    host.removeEventListener('pointercancel', onCancel)
    host.removeEventListener('pointerleave', onLeave)
    host.removeEventListener('dblclick', onDbl)
  }
}

/** exported for tests and the facade: pick at CSS coordinates of the viewport */
export async function clickAt(x: number, y: number, _w: number, _h: number): Promise<'drone' | 'ground' | 'none'> {
  const picker = vp.picker
  if (!picker) return 'none'
  const r = await picker.pickAt(x, y, { want: ['drone', 'ground'] })
  if (r.kind === 'drone') {
    selection.select([r.id])
    return 'drone'
  }
  void pointInfoAt(x, y) // PRD-FR-017: the point under the click for the info card (ID pass, asynchronous)
  const worldId = vp.worldId
  if (r.kind !== 'ground' || !worldId) {
    vp.pick = null
    vp.mission?.goto.clear()
    vp.changed()
    return 'none'
  }
  vp.pick = { worldId, surface: r.pointEnu, surfaceKind: r.surface, atMs: performance.now() }
  const target = gotoTargetFor(r.pointEnu, primaryAgentNo())
  vp.mission?.goto.set(r.pointEnu, target, 'preview', performance.now())
  vp.changed()
  return 'ground'
}

/** point-cloud pick of a click (M06-FR-064; D1-ext): vp.pointPick and the 'pick.point' event, null on a miss */
export async function pointInfoAt(x: number, y: number): Promise<PointPickInfo | null> {
  const picker = vp.picker
  const worldId = vp.worldId
  if (!picker || !worldId) return null
  const r = await picker.pickAt(x, y, { want: ['point'] })
  const info: PointPickInfo | null = r.kind === 'point'
    ? { worldId, pointEnu: r.pointEnu, classIdx: r.classIdx, className: r.className ?? `class-${r.classIdx}`, hagM: r.hagM ?? null, spacingM: r.spacingM ?? 0, atMs: performance.now() }
    : null
  vp.pointPick = info
  events.emit('pick.point', info)
  vp.changed()
  return info
}

/** double click: a vehicle -> focus it (60 m sphere); the ground -> orbit target to the hit (orbit and bird modes) */
export async function dblclickAt(x: number, y: number): Promise<boolean> {
  const picker = vp.picker
  const rig = vp.rig
  if (!picker || !rig || (rig.mode !== 'orbit' && rig.mode !== 'bird')) return false
  const r = await picker.pickAt(x, y, { want: ['drone', 'ground'] })
  if (r.kind === 'drone') {
    rig.focusSphere(r.pointEnu, 60)
    return true
  }
  if (r.kind !== 'ground') return false
  rig.retarget(r.pointEnu)
  return true
}
