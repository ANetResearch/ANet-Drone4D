// Viewport pointer input (M06 §6.12; AWR-14 §6.1, §6.5-§6.7). Owner: M06.
// Click (primary button, movement below input.clickTolPx): drones first (CPU ray-sphere, hot zone >= 12 CSS px) ->
// select; otherwise M04 ray_hit -> the viewport's ground pick (GoTo preview marker; the M15 tool state machine reads it
// through the facade). Double click on the ground: orbit target to the hit, distance kept (camera flight). Pointer move:
// hover pick at <= 20 Hz (off while the camera moves) -> stores/selection hover (ring and expanded label).
// Everything asynchronous; no layout reads on the hot path (the host rect is read on pointer events only).
import { INPUT } from '@/lib/tokens/input.gen'
import { selection } from '@/stores/selection'
import { vp } from './session'
import { gotoTargetFor, primaryAgentNo } from './gotoRule'

export function installInteraction(host: HTMLElement): () => void {
  let down: { x: number; y: number; id: number } | null = null
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
  const onDown = (e: PointerEvent): void => {
    if (e.button !== 0) return
    down = { x: e.clientX, y: e.clientY, id: e.pointerId }
  }
  const onUp = (e: PointerEvent): void => {
    const d = down
    down = null
    if (!d || e.button !== 0 || d.id !== e.pointerId) return
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > INPUT.clickTolPx) return
    const [x, y] = local(e)
    void clickAt(x, y, vp.cssW, vp.cssH)
  }
  const onMove = (e: PointerEvent): void => {
    if (e.buttons !== 0 || !vp.picker) return
    const [x, y] = local(e)
    const r = vp.picker.hoverAt(x, y)
    if (r === null) return
    selection.setHover(r.kind === 'drone' ? r.id : null)
  }
  const onLeave = (): void => selection.setHover(null)
  const onDbl = (e: MouseEvent): void => {
    const [x, y] = local(e)
    void dblclickAt(x, y)
  }
  host.addEventListener('pointerdown', onDown)
  host.addEventListener('pointerup', onUp)
  host.addEventListener('pointermove', onMove)
  host.addEventListener('pointerleave', onLeave)
  host.addEventListener('dblclick', onDbl)
  return () => {
    ro?.disconnect()
    host.removeEventListener('pointerdown', onDown)
    host.removeEventListener('pointerup', onUp)
    host.removeEventListener('pointermove', onMove)
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
