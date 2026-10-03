// Viewport side of the route and area editor (AWR-14 §6.8; UX-FR-023, UX-FR-024; D1-AC-17). Mounted by the viewport
// overlay only while the editor is open, so it costs nothing in the steady state (D1-AC-23). Pointer input is taken in the
// capture phase on window before the camera controls see it:
//   select / add  a press within 10 px of a waypoint selects it and drags it in the horizontal plane of its height (Shift:
//                 only the height, along its vertical line); a press on the "+" at the middle of the hovered segment inserts
//                 a waypoint there; with "add waypoint" a click on the ground appends one (ray_hit through the facade);
//   area          a click adds a vertex, a double click closes the ring, Shift + drag draws a rectangle.
// Anything else (orbiting, a drag that misses the handles) goes on to the camera; plain clicks and double clicks come back
// through the viewport's click consumer (facade pick.setClickConsumer), so the camera controls always see a whole press. The selected waypoint and the insert
// handle are two small marks moved by transform in the overlay phase (Tier S: only transform per frame, ADR-029); the
// route itself is drawn by the mission overlay (facade mission.setDraft). Keyboard (editor scope, registered while open):
// Delete or Backspace, arrows (Shift x10), Alt + up or down, Mod+Z, Mod+Shift+Z, Enter, Esc.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { register } from '@/engine'
import { EASE_CSS, MOTION } from '@/lib/tokens/motion.gen'
import { pick, viewport } from '@/viewport/facade'
import { Alert, AlertDescription } from '@/ui/components/ui/alert'
import { Kbd } from '@/ui/components/ui/kbd'
import { Icon } from '@/ui/icons/Icon'
import { registerHotkey } from '@/ui/hotkeys/registry'
import { toolMode } from '@/ui/tools/toolMode'
import { getMotionTier } from '@/ui/motion/tier'
import { nearestMidpoint, nearestScreen, rayPlaneZ, rayVerticalZ, rectFrom, ROUTE_LIMITS, worldZ, type DraftWp } from './editModel'
import { areaEditor, editStore, routeEditor, useEdit } from './editStore'
import { leaveEditor } from './MissionEditPanel'

const HIT_PX = 10
const out = new Float32Array(2)
const ro = new Float64Array(3)
const rd = new Float64Array(3)
const ground = (x: number, y: number): number => viewport.groundZ(x, y)

/** screen positions of the draft waypoints (NaN when outside the view) */
function projectAll(wps: readonly DraftWp[], dst: Float32Array<ArrayBuffer>): Float32Array<ArrayBuffer> {
  const scr = dst.length >= wps.length * 2 ? dst : new Float32Array(Math.max(64, wps.length * 2))
  const p = [0, 0, 0]
  for (let i = 0; i < wps.length; i++) {
    p[0] = wps[i].x
    p[1] = wps[i].y
    p[2] = worldZ(wps[i], ground)
    if (viewport.projectToScreen(p, out)) {
      scr[2 * i] = out[0]
      scr[2 * i + 1] = out[1]
    } else {
      scr[2 * i] = Number.NaN
      scr[2 * i + 1] = Number.NaN
    }
  }
  return scr
}

const hostEl = (): HTMLElement | null => (typeof document === 'undefined' ? null : document.querySelector('[data-viewport]'))
/** a pointer event over the 3D view itself: the viewport host or its canvas (not a panel, the overlay or the ViewCube) */
function onCanvas(e: Event): boolean {
  const t = e.target
  const host = hostEl()
  return t instanceof Element && host !== null && host.contains(t) && t.closest('[data-viewcube]') === null
}

export function EditViewportLayer() {
  const t = useT()
  const tool = useEdit((s) => s.tool)
  const areaClosed = useEdit((s) => s.area.closed)
  const selRef = React.useRef<HTMLSpanElement>(null)
  const ringRef = React.useRef<HTMLSpanElement>(null)
  const plusRef = React.useRef<HTMLSpanElement>(null)
  const hoverSeg = React.useRef(-1)

  // pointer input in the capture phase (before camera-controls and the viewport click path)
  React.useEffect(() => {
    let scr: Float32Array<ArrayBuffer> = new Float32Array(128)
    let drag: { i: number; id: number; before: readonly DraftWp[]; z: number; moved: boolean } | null = null
    let rect: { a: [number, number]; z: number; id: number } | null = null
    const local = (e: PointerEvent | MouseEvent): [number, number] => {
      const r = hostEl()?.getBoundingClientRect()
      return r ? [e.clientX - r.left, e.clientY - r.top] : [e.clientX, e.clientY]
    }
    const swallow = (e: Event): void => {
      e.stopPropagation()
      e.preventDefault()
    }
    // ground picks are asynchronous (ray_hit): a double click closes the area once the vertex picks before it landed
    let inflight = 0
    let closeWanted = false
    const settle = (): void => {
      if (closeWanted && inflight === 0) {
        closeWanted = false
        areaEditor.close()
      }
    }
    const groundPoint = async (x: number, y: number): Promise<[number, number] | null> => {
      inflight++
      try {
        const r = await pick.pickAt(x, y, { want: ['ground'] })
        if (r.kind !== 'ground') {
          toolMode.flash('tool.noGround')
          return null
        }
        return [r.pointEnu[0], r.pointEnu[1]]
      } finally {
        inflight--
      }
    }
    const onDown = (e: PointerEvent): void => {
      if (e.button !== 0 || !onCanvas(e)) return
      const s = editStore.getState()
      if (s.phase === 'CLOSED' || s.phase === 'SUBMITTING') return
      const [x, y] = local(e)
      if (s.tool === 'area') {
        if (e.shiftKey && viewport.screenRay(x, y, ro, rd)) {
          // Shift + drag: an axis-aligned rectangle in the horizontal plane of the ground under the press
          const g0 = rayPlaneZ(ro, rd, ground(ro[0], ro[1]))
          const z = g0 ? ground(g0[0], g0[1]) : 0
          const a = rayPlaneZ(ro, rd, z)
          if (a) {
            rect = { a, z, id: e.pointerId }
            areaEditor.setRect(rectFrom(a, a), false)
            swallow(e)
          }
          return
        }
        return
      }
      scr = projectAll(s.wps, scr)
      const k = nearestScreen(scr, s.wps.length, x, y, HIT_PX)
      if (k >= 0) {
        routeEditor.select(k)
        drag = { i: k, id: e.pointerId, before: s.wps, z: worldZ(s.wps[k], ground), moved: false }
        swallow(e)
        return
      }
      const m = nearestMidpoint(scr, s.wps.length, x, y, HIT_PX)
      if (m >= 0 && s.wps.length < ROUTE_LIMITS.maxWaypoints) {
        routeEditor.insertAfter(m)
        swallow(e)
      }
      // anything else goes on to the camera; a click without movement comes back through the click consumer below
    }
    const onMove = (e: PointerEvent): void => {
      if (rect && e.pointerId === rect.id) {
        const [x, y] = local(e)
        if (viewport.screenRay(x, y, ro, rd)) {
          const b = rayPlaneZ(ro, rd, rect.z)
          if (b) areaEditor.setRect(rectFrom(rect.a, b), false)
        }
        swallow(e)
        return
      }
      if (drag && e.pointerId === drag.id) {
        const [x, y] = local(e)
        if (!viewport.screenRay(x, y, ro, rd)) return
        const s = editStore.getState()
        const w = s.dragging?.i === drag.i ? s.dragging.wp : s.wps[drag.i]
        if (!w) return
        drag.moved = true
        if (e.shiftKey) {
          // only the height, along the waypoint's vertical line
          const z = rayVerticalZ(ro, rd, w.x, w.y)
          if (z !== null) {
            drag.z = z
            routeEditor.drag(drag.i, { h: w.ref === 'WORLD' ? z : z - ground(w.x, w.y) }, true)
          }
        } else {
          // in the horizontal plane of the waypoint's height; an AGL waypoint keeps that world height while it moves
          const p = rayPlaneZ(ro, rd, drag.z)
          if (p) routeEditor.drag(drag.i, w.ref === 'WORLD' ? { x: p[0], y: p[1] } : { x: p[0], y: p[1], h: drag.z - ground(p[0], p[1]) }, true)
        }
        swallow(e)
        return
      }
      if (e.buttons === 0 && onCanvas(e)) {
        const s = editStore.getState()
        if (s.tool === 'area' || s.wps.length < 2) {
          hoverSeg.current = -1
          return
        }
        const [x, y] = local(e)
        scr = projectAll(s.wps, scr)
        hoverSeg.current = nearestScreen(scr, s.wps.length, x, y, HIT_PX) >= 0 ? -1 : nearestMidpoint(scr, s.wps.length, x, y, 3 * HIT_PX)
      }
    }
    const onUp = (e: PointerEvent): void => {
      if (rect && e.pointerId === rect.id) {
        const s = editStore.getState()
        const pts = s.area.pts
        rect = null
        if (pts.length === 4 && Math.abs(pts[0][0] - pts[2][0]) > 1 && Math.abs(pts[0][1] - pts[2][1]) > 1) areaEditor.setRect(pts.map((p) => [p[0], p[1]] as [number, number]), true)
        else areaEditor.clear()
        swallow(e)
        return
      }
      if (drag && e.pointerId === drag.id) {
        const d = drag
        drag = null
        if (d.moved) routeEditor.drag(d.i, {}, false, d.before)
        swallow(e)
        return
      }
    }
    // clicks and double clicks of the viewport (after its own click tolerance): add tools take them instead of the
    // selection, the ground pick marker and the double-click retarget; the camera controls keep the pointer stream
    const consume = (kind: 'click' | 'dblclick', x: number, y: number): boolean => {
      const s = editStore.getState()
      if (s.phase === 'CLOSED' || s.phase === 'SUBMITTING' || s.tool === 'select') return false
      if (kind === 'dblclick') {
        if (s.tool === 'area' && !s.area.closed) {
          closeWanted = true
          settle()
        }
        return true
      }
      void groundPoint(x, y).then((g) => {
        const cur = editStore.getState()
        if (g && cur.phase !== 'CLOSED') {
          if (cur.tool === 'add') routeEditor.addAt(g[0], g[1])
          else if (cur.tool === 'area') areaEditor.addVertex(g[0], g[1])
        }
        settle()
      })
      return true
    }
    pick.setClickConsumer(consume)
    window.addEventListener('pointerdown', onDown, { capture: true })
    window.addEventListener('pointermove', onMove, { capture: true })
    window.addEventListener('pointerup', onUp, { capture: true })
    return () => {
      pick.setClickConsumer(null)
      window.removeEventListener('pointerdown', onDown, { capture: true })
      window.removeEventListener('pointermove', onMove, { capture: true })
      window.removeEventListener('pointerup', onUp, { capture: true })
    }
  }, [])

  // the selected waypoint mark and the insert handle follow the camera: transform only, in the overlay phase
  React.useEffect(() => {
    const p = [0, 0, 0]
    let selShown = false
    let plusShown = false
    const place = (el: HTMLElement | null, show: boolean, shown: boolean): boolean => {
      if (!el) return false
      if (show) el.style.transform = `translate3d(${Math.round(out[0])}px, ${Math.round(out[1])}px, 0)`
      if (show !== shown) el.style.visibility = show ? 'visible' : 'hidden'
      return show
    }
    return register('overlay', 'mission-edit.handles', () => {
      const s = editStore.getState()
      const w = s.dragging?.i === s.sel ? s.dragging.wp : s.wps[s.sel]
      let show = false
      if (w && s.tool !== 'area') {
        p[0] = w.x
        p[1] = w.y
        p[2] = worldZ(w, ground)
        show = viewport.projectToScreen(p, out)
      }
      selShown = place(selRef.current, show, selShown)
      const k = hoverSeg.current
      const a = s.wps[k]
      const b = s.wps[k + 1]
      let showPlus = false
      if (a && b && s.tool !== 'area') {
        p[0] = (a.x + b.x) / 2
        p[1] = (a.y + b.y) / 2
        p[2] = (worldZ(a, ground) + worldZ(b, ground)) / 2
        showPlus = viewport.projectToScreen(p, out)
      }
      plusShown = place(plusRef.current, showPlus, plusShown)
    }, { layer: 'labels' })
  }, [])

  // a newly selected or added waypoint pops in (scale 0 -> 1, --duration-very-slow with --ease-bounce, AWR-14 §6.8 item 4),
  // on the ring inside the positioned mark; reduced and off tiers skip it
  React.useEffect(() => {
    let sel = editStore.getState().sel
    let n = editStore.getState().wps.length
    return editStore.subscribe((s) => {
      if (s.sel === sel && s.wps.length === n) return
      const changed = s.sel !== sel || s.wps.length > n
      sel = s.sel
      n = s.wps.length
      const tier = getMotionTier()
      if (!changed || sel < 0 || tier === 'reduced' || tier === 'off') return
      ringRef.current?.animate([{ scale: '0' }, { scale: '1' }], { duration: MOTION.durationVerySlowMs, easing: EASE_CSS.bounce })
    })
  }, [])

  // editor keys (registered after the global bindings, so they win while their guard holds)
  React.useEffect(() => {
    const open = () => editStore.getState().phase !== 'CLOSED'
    const route = () => open() && editStore.getState().tool !== 'area'
    const hasSel = () => route() && editStore.getState().sel >= 0
    const noLayer = () => typeof document === 'undefined' || document.querySelector(OPEN_LAYER) === null
    const offs = [
      registerHotkey({ id: 'edit.delete', combo: 'Delete', scope: 'editor', labelKey: 'edit.row.delete', when: () => hasSel() || areaOpen(), deniedKey: () => null, run: deleteKey }),
      registerHotkey({ id: 'edit.backspace', combo: 'Backspace', scope: 'editor', labelKey: 'edit.row.delete', when: () => hasSel() || areaOpen(), deniedKey: () => null, run: deleteKey }),
      ...([['ArrowLeft', -1, 0], ['ArrowRight', 1, 0], ['ArrowUp', 0, 1], ['ArrowDown', 0, -1]] as const).flatMap(([code, dx, dy]) => [
        registerHotkey({ id: `edit.nudge.${code}`, combo: code, scope: 'editor', labelKey: 'edit.nudge', when: hasSel, deniedKey: () => null,
          run: () => routeEditor.nudge(dx * ROUTE_LIMITS.nudgeM, dy * ROUTE_LIMITS.nudgeM) }),
        registerHotkey({ id: `edit.nudge10.${code}`, combo: `shift+${code}`, scope: 'editor', labelKey: 'edit.nudge', when: hasSel, deniedKey: () => null,
          run: () => routeEditor.nudge(dx * ROUTE_LIMITS.nudgeFastM, dy * ROUTE_LIMITS.nudgeFastM) }),
      ]),
      registerHotkey({ id: 'edit.up', combo: 'alt+ArrowUp', scope: 'editor', labelKey: 'edit.row.up', when: hasSel, deniedKey: () => null, run: () => routeEditor.move(editStore.getState().sel, -1) }),
      registerHotkey({ id: 'edit.down', combo: 'alt+ArrowDown', scope: 'editor', labelKey: 'edit.row.down', when: hasSel, deniedKey: () => null, run: () => routeEditor.move(editStore.getState().sel, 1) }),
      registerHotkey({ id: 'edit.undo', combo: 'mod+KeyZ', scope: 'editor', labelKey: 'edit.undo', when: open, deniedKey: () => null, run: () => routeEditor.undo() }),
      registerHotkey({ id: 'edit.redo', combo: 'mod+shift+KeyZ', scope: 'editor', labelKey: 'edit.redo', when: open, deniedKey: () => null, run: () => routeEditor.redo() }),
      registerHotkey({ id: 'edit.enter', combo: 'Enter', scope: 'editor', labelKey: 'edit.submit', when: () => open() && noLayer(), deniedKey: () => null,
        run: () => {
          const s = editStore.getState()
          if (s.tool === 'area') areaEditor.close()
          else void routeEditor.submit()
        } }),
      registerHotkey({ id: 'edit.escape', combo: 'Escape', scope: 'editor', labelKey: 'hotkey.escape', blockedByModal: false, when: () => open() && noLayer(), deniedKey: () => null,
        run: () => {
          const s = editStore.getState()
          if (toolMode.escape()) return
          if (s.tool === 'area' && s.area.pts.length && !s.area.closed) areaEditor.clear()
          else if (s.tool !== 'select') routeEditor.setTool('select')
          else leaveEditor()
        } }),
    ]
    return () => {
      for (const off of offs) off()
    }
  }, [])

  const hint = tool === 'add' ? 'edit.hint.add' : tool === 'area' ? (areaClosed ? 'edit.hint.areaClosed' : 'edit.hint.area') : 'edit.hint.select'
  return (
    <>
      <div data-anchor="top-center" data-edit-hint={tool} className="mt-11">
        <Alert className="flex items-center gap-2 bg-hud px-3 py-1.5">
          <Icon icon={tool === 'area' ? 'tool.box' : tool === 'add' ? 'wp.add' : 'tool.select'} />
          <AlertDescription className="flex items-center gap-1.5 text-hud-sub">
            {t(hint)}
            <Kbd>Esc</Kbd>
          </AlertDescription>
        </Alert>
      </div>
      {/* the marks are positioned from the viewport origin and take no pointer events (input is handled above) */}
      <span ref={selRef} aria-hidden="true" data-edit-selected="" className="pointer-events-none invisible absolute top-0 left-0 -mt-2.5 -ml-2.5 size-5 will-change-transform">
        {/* the ring pops inside the positioned mark: a scale on the mark itself would also scale its translation */}
        <span ref={ringRef} className="block size-full rounded-full ring-2 ring-foreground" />
      </span>
      <span ref={plusRef} aria-hidden="true" data-edit-insert="" className="pointer-events-none invisible absolute top-0 left-0 -mt-2.5 -ml-2.5 flex size-5 items-center justify-center rounded-full bg-hud text-foreground ring-1 ring-foreground/40 will-change-transform">
        <Icon icon="plus" className="size-3" />
      </span>
    </>
  )
}

// an open popup takes Enter and Esc itself (the vehicle Combobox, the generator Select, row menus, dialogs)
const OPEN_LAYER = ['[data-slot="dialog-content"]', '[data-slot="alert-dialog-content"]', '[role="menu"]', '[data-slot="popover-content"]',
  '[data-slot="combobox-content"]', '[data-slot="select-content"]', '[role="listbox"]'].map((x) => `${x}:not([data-closed]):not([data-ending-style])`).join(',')
const areaOpen = (): boolean => {
  const s = editStore.getState()
  return s.phase !== 'CLOSED' && s.tool === 'area' && !s.area.closed && s.area.pts.length > 0
}
function deleteKey(): void {
  const s = editStore.getState()
  if (s.tool === 'area') areaEditor.removeLast()
  else if (s.sel >= 0) routeEditor.remove(s.sel)
}

/** mounts the viewport side while the editor is open */
export function EditViewportMount() {
  const open = useEdit((s) => s.phase !== 'CLOSED')
  return open ? <EditViewportLayer /> : null
}
