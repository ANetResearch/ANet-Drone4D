// ViewCube (M06-FR-057, FR-068, AC-041; AWR-15 §10.11 item 3; g01 §5: DOM instead of drei GizmoHelper). Owner: M06.
// 72 x 72 CSS px CSS-3D cube (perspective 400 px) whose rotation is written once per overlay phase as matrix3d (the
// inverse camera rotation expressed for the ENU cube: M = F . R_view . F with F = diag(1, -1, 1) and cube space
// (E, -U, -N)); faces East/West/North/South/Up/Down, each with 3 x 3 regions (face centre, edges, corners: 26
// directions). A region click (or Enter/Space) flies the camera to look from that direction with the target and the
// distance kept (straight up keeps orbit, north up). Regions are div role=button tabIndex=0 with aria-labels (the
// ADR-028 whitelist of no-raw-controls covers this file). Faces use g800 (muted in the dark viewport), ring
// foreground/10, text-hud-cap, the north face g50 bold, never red; hover and focus change lightness only over
// --duration-quick. Face and direction texts come from M15 (viewCube.setLabels); defaults are E/W/N/S/U/D.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Matrix4 } from 'three'
import { events, register } from '@/engine'
import { vp } from '../session'

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
/** face normal and in-face axes (CSS x right, y down on the face) in ENU */
const FACES: Record<Face, { n: [number, number, number]; x: [number, number, number]; y: [number, number, number]; css: string }> = {
  S: { n: [0, -1, 0], x: [1, 0, 0], y: [0, 0, -1], css: 'translateZ(36px)' },
  N: { n: [0, 1, 0], x: [-1, 0, 0], y: [0, 0, -1], css: 'rotateY(180deg) translateZ(36px)' },
  E: { n: [1, 0, 0], x: [0, 1, 0], y: [0, 0, -1], css: 'rotateY(90deg) translateZ(36px)' },
  W: { n: [-1, 0, 0], x: [0, -1, 0], y: [0, 0, -1], css: 'rotateY(-90deg) translateZ(36px)' },
  U: { n: [0, 0, 1], x: [1, 0, 0], y: [0, -1, 0], css: 'rotateX(90deg) translateZ(36px)' },
  D: { n: [0, 0, -1], x: [1, 0, 0], y: [0, 1, 0], css: 'rotateX(-90deg) translateZ(36px)' },
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
    return register('overlay', 'viewcube', () => {
      const cam = vp.camera
      if (!cam || !cube.current) return
      cubeMatrix(cam.matrixWorld, m)
      let same = true
      for (let i = 0; i < 16; i++) if (!(Math.abs(m.elements[i] - last[i]) <= 1e-4)) same = false
      if (same) return
      for (let i = 0; i < 16; i++) last[i] = m.elements[i]
      const e = m.elements
      cube.current.style.transform = `translateZ(-36px) matrix3d(${e[0]},${e[1]},${e[2]},${e[3]},${e[4]},${e[5]},${e[6]},${e[7]},${e[8]},${e[9]},${e[10]},${e[11]},0,0,0,1)`
    }, { layer: 'labels' })
  }, [])
  const faces = useMemo(() => FACE_ORDER.map((f) => ({ f, regions: [-1, 0, 1].flatMap((v) => [-1, 0, 1].map((u) => ({ u, v, d: regionDir(f, u, v) }))) })), [])
  const go = (d: [number, number, number]): void => vp.rig?.viewFrom(d)
  return (
    <div ref={box} data-anchor="top-right" data-viewcube="" hidden={hidden} className="pointer-events-auto pr-(--rail-gap)">
      <div className="mr-9 size-18 [perspective:400px]">
      <div ref={cube} className="relative size-full [transform-style:preserve-3d]">
        {faces.map(({ f, regions }) => (
          <div key={f} data-face={f} className="absolute inset-0 grid grid-cols-3 grid-rows-3 overflow-hidden rounded-sm bg-muted ring-1 ring-foreground/10 [backface-visibility:hidden]" style={{ transform: FACES[f].css }}>
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
