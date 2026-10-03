// ViewCube (M06-FR-057, FR-068, AC-041; AWR-15 §10.11 item 3; g01 §5: DOM instead of drei GizmoHelper). Owner: M06.
// 72 x 72 CSS px cube under an orthographic projection (P4-UI, ADR-072): the cube rotation (the inverse camera rotation
// expressed for the ENU cube: M = F . R_view . F with F = diag(1, -1, 1) and cube space (E, -U, -N)) is applied in JS to
// each face, and every face gets a flat 2D matrix() and is shown only while it faces the viewer. The former CSS-3D cube
// (perspective 400 px, preserve-3d, hidden back faces) made the compositor draw every visible face as its own render
// pass in every frame: on Tier S that was about 11 ms of GPU process CPU per frame and about two of the three
// percentage points of D1-AC-23 (interleaved A/B, P4-UI report). Faces are composited layers (will-change: transform):
// an orientation change is a compositor-only update, written in the overlay phase when the matrix changed visibly; on
// Tier S only in the frames of the shared UI tick (4 Hz). Faces
// East/West/North/South/Up/Down, each with 3 x 3 regions (face centre, edges, corners: 26 directions). A region click (or Enter/Space) flies the camera to look from that direction with the target and the
// distance kept (straight up keeps orbit, north up). Regions are div role=button tabIndex=0 with aria-labels (the
// ADR-028 whitelist of no-raw-controls covers this file). Faces use g800 (muted in the dark viewport), ring
// foreground/10, text-hud-cap, the north face g50 bold, never red; hover and focus change lightness only over
// --duration-quick. Face and direction texts come from M15 (viewCube.setLabels); defaults are E/W/N/S/U/D.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Matrix4 } from 'three'
import { events, register } from '@/engine'
import { vp } from '../session'
import { uiTickDue } from '@/stores/uiTick'

export interface ViewCubeLabels { E: string; W: string; N: string; S: string; U: string; D: string; view: string }
let labels: ViewCubeLabels = { E: 'E', W: 'W', N: 'N', S: 'S', U: 'U', D: 'D', view: 'View' }
const labelListeners = new Set<() => void>()
export const viewCube = {
  setLabels(l: Partial<ViewCubeLabels>): void {
    labels = { ...labels, ...l }
    for (const cb of labelListeners) cb()
  },
}

type Face = 'E' | 'W' | 'N' | 'S' | 'U' | 'D'
/** face normal and in-face axes (CSS x right, y down on the face) in ENU; the faces sit like CSS rotateY / rotateX then
 * translateZ(36px) in cube space (FACE_M below) */
const FACES: Record<Face, { n: [number, number, number]; x: [number, number, number]; y: [number, number, number] }> = {
  S: { n: [0, -1, 0], x: [1, 0, 0], y: [0, 0, -1] },
  N: { n: [0, 1, 0], x: [-1, 0, 0], y: [0, 0, -1] },
  E: { n: [1, 0, 0], x: [0, 1, 0], y: [0, 0, -1] },
  W: { n: [-1, 0, 0], x: [0, -1, 0], y: [0, 0, -1] },
  U: { n: [0, 0, 1], x: [1, 0, 0], y: [0, -1, 0] },
  D: { n: [0, 0, -1], x: [1, 0, 0], y: [0, 1, 0] },
}
const FACE_ORDER: readonly Face[] = ['S', 'N', 'E', 'W', 'U', 'D']

/** ENU direction of region (u, v) in {-1, 0, 1} on a face */
export function regionDir(face: Face, u: number, v: number): [number, number, number] {
  const f = FACES[face]
  return [f.n[0] + u * f.x[0] + v * f.y[0], f.n[1] + u * f.x[1] + v * f.y[1], f.n[2] + u * f.x[2] + v * f.y[2]]
}
/** readable name of a direction from its components: U/D, then N/S, then E/W */
export function dirName(d: readonly number[], l: ViewCubeLabels = labels): string {
  const parts: string[] = []
  if (d[2] > 0.5) parts.push(l.U)
  if (d[2] < -0.5) parts.push(l.D)
  if (d[1] > 0.5) parts.push(l.N)
  if (d[1] < -0.5) parts.push(l.S)
  if (d[0] > 0.5) parts.push(l.E)
  if (d[0] < -0.5) parts.push(l.W)
  return parts.join('-')
}
/** the 26 unique directions (tests) */
export function allDirections(): [number, number, number][] {
  const seen = new Map<string, [number, number, number]>()
  for (const f of FACE_ORDER) for (let v = -1; v <= 1; v++) for (let u = -1; u <= 1; u++) {
    const d = regionDir(f, u, v)
    seen.set(d.join(','), d)
  }
  return [...seen.values()]
}

const F = new Matrix4().makeScale(1, -1, 1)
/** cube transform for a camera world matrix: F . R_view . F (translation dropped) */
export function cubeMatrix(cameraWorld: Matrix4, out: Matrix4): Matrix4 {
  out.extractRotation(cameraWorld).invert()
  return out.premultiply(F).multiply(F)
}

/** CSS face placement (rotate, then push 36 px out) as matrices, in FACE_ORDER */
const FACE_M: readonly Matrix4[] = FACE_ORDER.map((f) => {
  const half = new Matrix4().makeTranslation(0, 0, 36)
  const r = new Matrix4()
  if (f === 'N') r.makeRotationY(Math.PI)
  else if (f === 'E') r.makeRotationY(Math.PI / 2)
  else if (f === 'W') r.makeRotationY(-Math.PI / 2)
  else if (f === 'U') r.makeRotationX(Math.PI / 2)
  else if (f === 'D') r.makeRotationX(-Math.PI / 2)
  return r.multiply(half)
})
/** stride of faceTransforms: CSS matrix(a, b, c, d, e, f) and the view-space z of the face normal */
export const FACE_STRIDE = 7
const tmpFace = new Matrix4()

/**
 * orthographic placement of the six faces for a cube matrix: for face k, out[7k..7k+5] is the CSS 2D matrix of the face
 * (about the cube centre, the faces' transform origin) and out[7k+6] the z of its normal (> 0: faces the viewer)
 */
export function faceTransforms(m: Matrix4, out: Float64Array): Float64Array {
  for (let k = 0; k < FACE_M.length; k++) {
    const e = tmpFace.multiplyMatrices(m, FACE_M[k]).elements
    const o = FACE_STRIDE * k
    out[o] = e[0]
    out[o + 1] = e[1]
    out[o + 2] = e[4]
    out[o + 3] = e[5]
    out[o + 4] = e[12]
    out[o + 5] = e[13]
    out[o + 6] = e[10]
  }
  return out
}
/** a face is drawn while its normal points towards the viewer (the CSS backface rule, with a little margin) */
export const FACE_VISIBLE_Z = 1e-3
/** smallest rotation-element change that is written (sub-pixel changes are skipped) */
const VISIBLE_STEP = 6e-3

export function ViewCube() {
  const cube = useRef<HTMLDivElement>(null!)
  const box = useRef<HTMLDivElement>(null!)
  const [l, setL] = useState(labels)
  const [hidden, setHidden] = useState(false)
  useEffect(() => {
    const cb = (): void => setL(labels)
    labelListeners.add(cb)
    const off = events.on<{ mode: string }>('camera.mode', (m) => setHidden(m.mode === 'fpv'))
    // native listeners: the host's camera controls and click picking must not see ViewCube input
    const el = box.current
    const stop = (e: Event): void => e.stopPropagation()
    for (const t of ['pointerdown', 'pointerup', 'wheel', 'dblclick']) el.addEventListener(t, stop)
    return () => {
      labelListeners.delete(cb)
      off()
      for (const t of ['pointerdown', 'pointerup', 'wheel', 'dblclick']) el.removeEventListener(t, stop)
    }
  }, [])
  useEffect(() => {
    const m = new Matrix4()
    const last = new Float64Array(16).fill(Number.NaN)
    const tf = new Float64Array(FACE_STRIDE * 6)
    const shown = new Uint8Array(6).fill(2)
    return register('overlay', 'viewcube', (ctx) => {
      const cam = vp.camera
      const box = cube.current
      if (!cam || !box) return
      // Tier S: in the frames of the shared UI tick only (ADR-066 rule for periodic UI writes; P4-UI): a turning camera
      // otherwise rewrote three face transforms in nearly every frame (about 40 style mutations per second in flight60)
      if (ctx.tier === 'S' && !uiTickDue(ctx)) return
      cubeMatrix(cam.matrixWorld, m)
      // write only a visible change: 0.006 in a rotation element moves a cube corner (51 px from the centre) by about
      // 0.3 px, so a slow camera turn writes every few frames instead of every frame (P4-UI)
      let same = true
      for (let i = 0; i < 16; i++) if (!(Math.abs(m.elements[i] - last[i]) <= VISIBLE_STEP)) same = false
      if (same) return
      for (let i = 0; i < 16; i++) last[i] = m.elements[i]
      faceTransforms(m, tf)
      const faces = box.children
      for (let k = 0; k < 6 && k < faces.length; k++) {
        const el = faces[k] as HTMLElement
        const o = FACE_STRIDE * k
        const vis = tf[o + 6] > FACE_VISIBLE_Z ? 1 : 0
        if (vis) el.style.transform = `matrix(${tf[o].toFixed(4)},${tf[o + 1].toFixed(4)},${tf[o + 2].toFixed(4)},${tf[o + 3].toFixed(4)},${tf[o + 4].toFixed(2)},${tf[o + 5].toFixed(2)})`
        if (vis !== shown[k]) {
          el.style.visibility = vis ? 'visible' : 'hidden'
          shown[k] = vis
        }
      }
    }, { layer: 'labels' })
  }, [])
  const faces = useMemo(() => FACE_ORDER.map((f) => ({ f, regions: [-1, 0, 1].flatMap((v) => [-1, 0, 1].map((u) => ({ u, v, d: regionDir(f, u, v) }))) })), [])
  const go = (d: [number, number, number]): void => vp.rig?.viewFrom(d)
  return (
    <div ref={box} data-anchor="top-right" data-viewcube="" hidden={hidden} className="pointer-events-auto pr-(--rail-gap)">
      <div className="mr-9 size-18">
      <div ref={cube} className="relative size-full">
        {faces.map(({ f, regions }) => (
          <div key={f} data-face={f} className="invisible absolute inset-0 grid grid-cols-3 grid-rows-3 overflow-hidden rounded-sm bg-muted ring-1 ring-foreground/10 will-change-transform">
            <span className={`pointer-events-none absolute inset-0 flex items-center justify-center text-hud-cap ${f === 'N' ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}>{l[f]}</span>
            {regions.map(({ u, v, d }) => (
              <div key={`${u},${v}`} role="button" tabIndex={0} aria-label={`${l.view}: ${dirName(d, l)}`} data-dir={d.join(',')}
                className="relative z-10 outline-none [transition:background-color_var(--duration-quick)_var(--ease-smooth-out)] hover:bg-foreground/10 focus-visible:bg-foreground/15 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                onClick={() => go(d)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    go(d)
                  }
                }} />
            ))}
          </div>
        ))}
      </div>
      </div>
    </div>
  )
}
