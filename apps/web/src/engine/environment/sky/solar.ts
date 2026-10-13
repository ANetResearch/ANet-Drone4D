// NOAA solar position (AWR-04 §6.5; ADR-102; NOAA Solar Calculator, Meeus low-precision series). Owner: M07 (WP-09).
// Mirror of python/awr/environment/sky/solar.py (golden tests/environment/golden/d2_sky.json): UNIX ms (UTC, no Delta T)
// and WGS84 latitude/longitude (east positive) -> geometric elevation (no refraction), azimuth clockwise from true
// north, declination, equation of time and true solar time. The azimuth uses the atan2 form (equal to NOAA's acos form,
// stable at the zenith). The sky of the frontend and the server's EnvKeyframe use this same function.

export const MS_PER_DAY = 86_400_000
const JD_UNIX0 = 2440587.5
const JD_J2000 = 2451545.0
const D2R = Math.PI / 180
const R2D = 180 / Math.PI

export interface SunPos {
  azDeg: number
  elDeg: number
  declDeg: number
  eotMin: number
  tstMin: number
  haDeg: number
}

/** floored modulo (Python semantics) */
export function fmod(x: number, m: number): number {
  return x - Math.floor(x / m) * m
}

export function newSunPos(): SunPos {
  return { azDeg: 0, elDeg: 0, declDeg: 0, eotMin: 0, tstMin: 0, haDeg: 0 }
}

export function solarPosition(unixMs: number, latDeg: number, lonDeg: number, out: SunPos = newSunPos()): SunPos {
  const jd = unixMs / MS_PER_DAY + JD_UNIX0
  const jc = (jd - JD_J2000) / 36525.0
  const l0 = fmod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)
  const m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
  const e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
  const mr = m * D2R
  const c = Math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + Math.sin(2.0 * mr) * (0.019993 - 0.000101 * jc) +
    Math.sin(3.0 * mr) * 0.000289
  const omega = (125.04 - 1934.136 * jc) * D2R
  const appLong = (l0 + c - 0.00569 - 0.00478 * Math.sin(omega)) * D2R
  const obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
  const obliq = (obliq0 + 0.00256 * Math.cos(omega)) * D2R
  const decl = Math.asin(Math.sin(obliq) * Math.sin(appLong))
  const y = Math.tan(obliq / 2.0) ** 2
  const l0r = l0 * D2R
  const eot = 4.0 * R2D * (y * Math.sin(2.0 * l0r) - 2.0 * e * Math.sin(mr) + 4.0 * e * y * Math.sin(mr) * Math.cos(2.0 * l0r) -
    0.5 * y * y * Math.sin(4.0 * l0r) - 1.25 * e * e * Math.sin(2.0 * mr))
  const minUtc = fmod(unixMs / 60000.0, 1440.0)
  const tst = fmod(minUtc + eot + 4.0 * lonDeg, 1440.0)
  const ha = tst / 4.0 - 180.0
  const lat = latDeg * D2R
  const har = ha * D2R
  let cz = Math.sin(lat) * Math.sin(decl) + Math.cos(lat) * Math.cos(decl) * Math.cos(har)
  cz = Math.min(1.0, Math.max(-1.0, cz))
  out.elDeg = 90.0 - Math.acos(cz) * R2D
  out.azDeg = fmod(Math.atan2(Math.sin(har), Math.cos(har) * Math.sin(lat) - Math.tan(decl) * Math.cos(lat)) * R2D + 180.0, 360.0)
  out.declDeg = decl * R2D
  out.eotMin = eot
  out.tstMin = tst
  out.haDeg = ha
  return out
}

/** NOAA standard-atmosphere refraction (deg); apparent elevation = geometric + this */
export function refractionDeg(elDeg: number): number {
  if (elDeg > 85.0) return 0.0
  const te = Math.tan(elDeg * D2R)
  let r: number
  if (elDeg > 5.0) r = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
  else if (elDeg > -0.575) r = 1735.0 + elDeg * (-518.2 + elDeg * (103.4 + elDeg * (-12.79 + elDeg * 0.711)))
  else r = -20.772 / te
  return r / 3600.0
}

/** unit vector towards the sun (ENU); azimuth clockwise from grid north, as config.sun */
export function sunDirEnu(azDeg: number, elDeg: number, out: Float64Array | number[] = new Float64Array(3)): Float64Array | number[] {
  const a = azDeg * D2R
  const h = elDeg * D2R
  out[0] = Math.sin(a) * Math.cos(h)
  out[1] = Math.cos(a) * Math.cos(h)
  out[2] = Math.sin(h)
  return out
}
