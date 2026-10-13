// Sensor view state and controller (D2 WP-11; AWR-04 §10.3, §10.5; AWR-14 §20.10.5, §20.2.2 `sv`). Owner: M06.
// One per page. The view (FPV, EO day, NIR night, LWIR) applies to the FPV camera of the focus vehicle: FPV is the D1
// camera (first camera sensor, no composite); EO and NIR look through the vehicle's camera sensor (the optical zoom one
// when mounted, so the zoom of uav/{id}/payload drives the projection through M13's sensor cache); LWIR through its
// thermal sensor. A request outside the FPV camera, without a focus vehicle or without the sensor kind is refused with
// a reason (the B key cycle skips unavailable views); leaving the FPV camera returns the view to FPV. Everything here is
// local display state (ADR-108): it never writes to the simulation, so a viewer switches views without a write request
// (D2-AC-37). Inputs from other modules: the environment (M07 adapter or M15: illuminance, temperatures, sun,
// transmittance; defaults to a clear day), thermal emitter sources (M18 targets; the viewport adds the other vehicles'
// motors itself), the payload of M13 (illuminators) through the SensorsApi.
import { events, type SensorView } from '@/engine'
import { DEFAULT_SUN_THREE } from '@/engine'
import { vp } from '../session'
import { availableModes, nextMode, SV, type Illuminator, type LwirPalette, type SensorViewMode } from './math'

/** environment inputs of the views (AWR-04 §6.2, §6.5; M07 sky, thermal and optics functions) */
export interface SensorEnv {
  /** illuminance (lx) */
  illumLx: number
  /** air, background surface and effective sky temperatures (deg C), background emissivity */
  tAirC: number
  tSurfaceC: number
  tSkyC: number
  emissivityBg: number
  /** LWIR extinction (1/m: Kim extrapolation plus the water vapour term) */
  sigmaLwPerM: number
  /** unit sun direction (ENU) */
  sunEnu: [number, number, number]
}

const sunEnu = (): [number, number, number] => [DEFAULT_SUN_THREE.x, -DEFAULT_SUN_THREE.z, DEFAULT_SUN_THREE.y]
/** a clear day (the fixed D1 sun, 15 deg C air at sea level) until an environment source sets the values */
export const DEFAULT_ENV: Readonly<SensorEnv> = Object.freeze({
  illumLx: 50_000, tAirC: 18, tSurfaceC: 24, tSkyC: -15, emissivityBg: 0.95, sigmaLwPerM: 2e-4, sunEnu: sunEnu(),
})

/**
 * thermal emitter record (Float64Array, stride EMITTER_STRIDE): ENU box centre (m), size l w h (m, l along the yaw),
 * yaw (rad, ENU, x axis = east, counter-clockwise), kinetic temperature: absolute tC (deg C, NaN for relative) or dT
 * relative to the background surface (K), emissivity, hot fraction (< 1: a hot core of sqrt(max(f, 0.25)) of the size)
 */
export const EMITTER = { X: 0, Y: 1, Z: 2, L: 3, W: 4, H: 5, YAW: 6, TC: 7, DT: 8, EPS: 9, HOT: 10 } as const
export const EMITTER_STRIDE = 11
/** fills out (stride EMITTER_STRIDE) with up to max emitters and returns the count (no allocation expected) */
export type EmitterSource = (out: Float64Array, max: number) => number

export interface SensorViewResult { ok: boolean; reason?: 'no_focus' | 'no_camera_sensor' | 'no_thermal_sensor' | 'not_fpv' }
export interface SensorViewInfo {
  mode: SensorViewMode
  palette: LwirPalette
  digitalZoom: number
  /** the sensor the FPV camera looks through (null outside the FPV camera) */
  sensorNo: number | null
  sensorName: string | null
  /** SensorPose48 kind: 0 camera, 2 thermal */
  kind: number | null
  fMm: number | null
  hfovRad: number | null
  vfovRad: number | null
  available: Record<SensorViewMode, boolean>
}

type Listener = () => void

class SensorViewState {
  mode: SensorViewMode = 'fpv'
  palette: LwirPalette = 'white_hot'
  digitalZoom = 1
  /** requested sensor number of the current kind (-1: automatic) */
  sensorNo = -1
  readonly env: SensorEnv = { ...DEFAULT_ENV, sunEnu: [...DEFAULT_ENV.sunEnu] }
  readonly sources = new Map<string, EmitterSource>()
  /** test builds: illuminators used instead of uav/{id}/payload (FakeSource publishes no payload) */
  illumOverride: Illuminator[] | null = null
  /** the sources as an array (iterated per frame without an iterator) */
  sourceList: EmitterSource[] = []
  version = 0
  private readonly listeners = new Set<Listener>()
  changed(): void {
    this.version++
    for (const l of [...this.listeners]) l()
  }
  subscribe(l: Listener): () => void {
    this.listeners.add(l)
    return () => {
      this.listeners.delete(l)
    }
  }
}

export const sv = new SensorViewState()

/** the composite runs: a sensor view is selected and the camera is the FPV camera on its sensor */
export function sensorViewActive(): boolean {
  return sv.mode !== 'fpv' && vp.rig?.mode === 'fpv' && vp.rig.fpvSensor !== null
}

function focusAgent(): number {
  return vp.rig && vp.rig.mode === 'fpv' ? vp.rig.focusAgent : -1
}

/** imaging sensors of an agent: cameras (kind 0) and thermal cameras (kind 2) */
function imaging(agentNo: number): { cams: readonly SensorView[]; thermal: readonly SensorView[] } {
  const all = agentNo >= 0 ? vp.sensors?.sensorsOf(agentNo) ?? [] : []
  return { cams: all.filter((s) => s.kind === 0), thermal: all.filter((s) => s.kind === 2) }
}

export function availableNow(agentNo = focusAgent()): Record<SensorViewMode, boolean> {
  const { cams, thermal } = imaging(agentNo)
  return availableModes(cams.length > 0, thermal.length > 0)
}

/** the sensor a view looks through: the requested one when it has the right kind, else the zoom camera, else the first */
function sensorFor(mode: SensorViewMode, agentNo: number, want: number): SensorView | null {
  const { cams, thermal } = imaging(agentNo)
  const list = mode === 'lwir' ? thermal : cams
  if (list.length === 0) return null
  const req = want >= 0 ? list.find((s) => s.sensorNo === want) : undefined
  if (req) return req
  if (mode === 'fpv') return cams[0]
  return (mode !== 'lwir' ? list.find((s) => s.zoom) : undefined) ?? list[0]
}

/** switch the view; enterFpv switches the camera to FPV first (needs a focus vehicle) */
export function setSensorView(mode: SensorViewMode, o: { enterFpv?: boolean; sensorNo?: number } = {}): SensorViewResult {
  const rig = vp.rig
  if (!rig) return { ok: false, reason: 'not_fpv' }
  if (rig.mode !== 'fpv') {
    if (!o.enterFpv) return { ok: false, reason: 'not_fpv' }
    const r = rig.setMode('fpv', rig.focusAgent >= 0 ? rig.focusAgent : -1)
    if (!r.ok) return { ok: false, reason: r.reason }
  }
  const a = rig.focusAgent
  if (a < 0) return { ok: false, reason: 'no_focus' }
  const want = o.sensorNo ?? (mode === sv.mode ? sv.sensorNo : -1)
  const s = sensorFor(mode, a, want)
  if (!s) return { ok: false, reason: mode === 'lwir' ? 'no_thermal_sensor' : 'no_camera_sensor' }
  rig.setFpvSensor(s)
  // remember an explicit choice only when it was honoured (-1: automatic)
  const keep = want >= 0 && s.sensorNo === want ? want : -1
  const changed = sv.mode !== mode || sv.sensorNo !== keep
  sv.mode = mode
  sv.sensorNo = keep
  if (mode !== 'lwir') sv.digitalZoom = 1
  if (changed) {
    sv.changed()
    events.emit('sensorView.mode', { mode })
  }
  return { ok: true }
}

/** B key: FPV -> EO -> night -> LWIR -> FPV over the available views (only in the FPV camera) */
export function cycleSensorView(): SensorViewMode {
  if (vp.rig?.mode !== 'fpv') return sv.mode
  const next = nextMode(sv.mode, availableNow())
  if (next !== sv.mode) setSensorView(next)
  return sv.mode
}

/** choose an imaging sensor of the focus vehicle: a thermal sensor selects the LWIR view, a camera keeps EO / night / FPV */
export function selectSensor(sensorNo: number): SensorViewResult {
  const a = focusAgent()
  if (a < 0) return { ok: false, reason: vp.rig?.mode === 'fpv' ? 'no_focus' : 'not_fpv' }
  const { thermal } = imaging(a)
  const isThermal = thermal.some((s) => s.sensorNo === sensorNo)
  const mode: SensorViewMode = isThermal ? 'lwir' : sv.mode === 'lwir' ? 'eo' : sv.mode
  return setSensorView(mode, { sensorNo })
}

export function setPalette(p: LwirPalette): void {
  if (sv.palette === p) return
  sv.palette = p
  sv.changed()
}

/** LWIR digital zoom 1-4x (display only, AWR-04 §6.1); ignored outside the LWIR view */
export function setDigitalZoom(z: number): void {
  const v = sv.mode === 'lwir' && Number.isFinite(z) ? Math.min(SV.digitalZoomMax, Math.max(1, z)) : 1
  if (v === sv.digitalZoom) return
  sv.digitalZoom = v
  sv.changed()
}

/** environment inputs (partial; finite values only) */
export function setSensorEnv(e: Partial<SensorEnv>): void {
  const t = sv.env as unknown as Record<string, unknown>
  for (const [k, v] of Object.entries(e)) {
    if (k === 'sunEnu') {
      const s = v as readonly number[] | undefined
      if (s && s.length === 3 && s.every(Number.isFinite)) {
        const l = Math.hypot(s[0], s[1], s[2]) || 1
        sv.env.sunEnu = [s[0] / l, s[1] / l, s[2] / l]
      }
    } else if (typeof v === 'number' && Number.isFinite(v) && k in DEFAULT_ENV) t[k] = v
  }
}

/** register a thermal emitter source (M18 targets); returns the unregister function */
export function setEmitterSource(id: string, fn: EmitterSource | null): () => void {
  if (fn) sv.sources.set(id, fn)
  else sv.sources.delete(id)
  sv.sourceList = [...sv.sources.values()]
  return () => {
    if (sv.sources.get(id) !== fn) return
    sv.sources.delete(id)
    sv.sourceList = [...sv.sources.values()]
  }
}

export function sensorViewInfo(): SensorViewInfo {
  const s = vp.rig?.mode === 'fpv' ? vp.rig.fpvSensor : null
  return {
    mode: sv.mode, palette: sv.palette, digitalZoom: sv.digitalZoom, sensorNo: s?.sensorNo ?? null, sensorName: s?.name ?? null, kind: s?.kind ?? null,
    fMm: s && Number.isFinite(s.fMm) ? (s.fMm as number) : null, hfovRad: s && Number.isFinite(s.hfov) ? s.hfov : null,
    vfovRad: s && Number.isFinite(s.vfov) ? s.vfov : null, available: availableNow(),
  }
}

/**
 * camera mode changes: leaving FPV returns the view to FPV; entering FPV (or FPV on another focus vehicle) re-applies the
 * selected view on the new vehicle, falling back to FPV when it lacks the sensor
 */
export function installSensorViewCamera(): () => void {
  return events.on<{ mode: string }>('camera.mode', (m) => {
    if (m.mode !== 'fpv') {
      if (sv.mode !== 'fpv') {
        sv.mode = 'fpv'
        sv.sensorNo = -1
        sv.digitalZoom = 1
        sv.changed()
      }
      return
    }
    if (sv.mode === 'fpv') return
    const r = setSensorView(sv.mode, { sensorNo: sv.sensorNo })
    if (!r.ok) {
      sv.mode = 'fpv'
      sv.sensorNo = -1
      sv.changed()
    }
  })
}
