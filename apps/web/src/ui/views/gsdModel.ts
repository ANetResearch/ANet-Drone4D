// GSD and recognition-distance calculator model (M15-FR-220; UX-FR-144; AWR-14 §20.13.5; AWR-04 §10.3): the inputs of the
// Sheet turned into the M19 calculator calls (engine/perception, the TypeScript port of python awr.sim.perception: the same
// formulas, unit-tested against the reference at <= 1 %): GSD, critical dimension and pixels on target, the TTPF
// probability per level, the largest slant range per level at P >= 0.9 (horizontal line of sight, static target) and the
// feasible station window of the class (geometric keepouts, AWR-04 §9.2) with the limiting reason. Pure and local: the
// viewer uses it freely (no request, D2-AC-37). The criterion of record stays the server's perception.
import {
  defaultEnv, feasibilityDetail, gsdM, gx40, lwir640, maxRangeForLevel, N50_PX, perceptionLevel, pPrimeUm, sensorFromCatalog, targetFromClass, ttpf,
  type Light, type ReqLevel, type SensorP,
  type TargetClassName,
} from '@/app/shell/engineD2'

export type Aspect = 'level' | 'down' | 'worst'
export type Resolution = '1080p' | '4k'
export interface CalcSensorOption { id: string; label: string; make: (res: Resolution) => SensorP; zoom: boolean; kind: SensorP['kind'] }

/** the reference sensors (always offered) and the imaging sensors of the catalogue (deduplicated by type) */
export function sensorOptions(catalog: readonly { sensor_type: string; name_zh: string; kind: string; spec: Record<string, unknown> }[] = []): CalcSensorOption[] {
  const out: CalcSensorOption[] = [
    { id: 'ref:gx40', label: 'GX40 EO', make: (r) => gx40(r), zoom: true, kind: 'eo' },
    { id: 'ref:gx40-nir', label: 'GX40 NIR', make: (r) => gx40(r, { night: true }), zoom: true, kind: 'nir' },
    { id: 'ref:gx40-nir-ext', label: 'GX40 NIR + NIR-ILL-L', make: (r) => gx40(r, { night: true, ext: true }), zoom: true, kind: 'nir' },
    { id: 'ref:lwir640-25', label: 'LWIR 640 25 mm', make: () => lwir640(25), zoom: false, kind: 'lwir' },
    { id: 'ref:lwir640-100', label: 'LWIR 640 100 mm', make: () => lwir640(100), zoom: false, kind: 'lwir' },
  ]
  for (const it of catalog) {
    if (it.kind !== 'eo' && it.kind !== 'nir' && it.kind !== 'lwir') continue
    const probe = sensorFromCatalog(it)
    if (!probe) continue
    out.push({ id: `cat:${it.sensor_type}`, label: it.name_zh || it.sensor_type, zoom: probe.fMaxMm > probe.fMinMm, kind: probe.kind,
      make: (r) => sensorFromCatalog(it, { resolution: r }) ?? probe })
  }
  return out
}

export interface CalcInput {
  sensor: SensorP
  fMm: number
  resolution: Resolution
  cls: TargetClassName
  aspect: Aspect
  level: ReqLevel
  rangeM: number
  light: Light
  morM: number
}

export interface CalcOutput {
  gsdCm: number
  dcM: number
  n: number
  nEff: number
  p: Record<ReqLevel, number>
  limiting: string | null
  rMax: Record<ReqLevel, number>
}

const LEVELS: readonly ReqLevel[] = ['D', 'R', 'I']

export function envFor(light: Light, morM: number) {
  return defaultEnv(light, morM)
}

/** the instant part (<= 10 Hz while a slider moves): GSD, pixels, probabilities and the per-level ranges */
export function calc(i: CalcInput): CalcOutput {
  const T = { ...targetFromClass(i.cls), moving: false, speedMaxMps: 0 }
  const env = envFor(i.light, i.morM)
  const eps = i.aspect === 'down' ? Math.PI / 2 : 0
  const out = perceptionLevel({ sensor: i.sensor, fMm: i.fMm, resolution: i.resolution, target: T, env, nReq: undefined,
    geom: { rangeM: i.rangeM, epsRad: eps, betaRad: 0, worst: i.aspect === 'worst', vRelPerpMps: 0 } })
  const pp = pPrimeUm(i.sensor, i.fMm, i.resolution)
  const dc = (out.n * i.rangeM * pp * 1e-3) / i.fMm
  const p = { D: 0, R: 0, I: 0 } as Record<ReqLevel, number>
  const rMax = { D: 0, R: 0, I: 0 } as Record<ReqLevel, number>
  for (const L of LEVELS) {
    p[L] = ttpf(out.nEff, N50_PX[L])
    rMax[L] = maxRangeForLevel(i.sensor, T, env, L, { fMm: i.fMm, resolution: i.resolution })
  }
  return { gsdCm: gsdM(i.rangeM, pp, i.fMm) * 100, dcM: dc, n: out.n, nEff: out.nEff, p, limiting: out.limiting, rMax }
}

/** the slow part (computed once the inputs settle): the feasible horizontal station window of the class */
export function windowFor(i: Pick<CalcInput, 'sensor' | 'cls' | 'level' | 'light' | 'morM'>): { dLoM: number | null; dHiM: number | null; aglM: number | null; why: string | null } {
  return feasibilityDetail({ target: targetFromClass(i.cls), sensor: i.sensor, light: i.light, morM: i.morM, levelReq: i.level })
}

/** log-scale slider position (0-1000) of a focal length and back (4.8-48 mm GX40) */
export function zoomPos(fMm: number, fMin: number, fMax: number): number {
  if (fMax <= fMin) return 0
  return Math.round((Math.log(fMm / fMin) / Math.log(fMax / fMin)) * 1000)
}
export function posZoom(pos: number, fMin: number, fMax: number): number {
  if (fMax <= fMin) return fMin
  return fMin * Math.exp((pos / 1000) * Math.log(fMax / fMin))
}

export const MOR_OPTIONS: readonly number[] = [20000, 10000, 5000, 3000, 1000]
