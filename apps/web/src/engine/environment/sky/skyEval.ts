// Sky state at tRender for the EnvStore (AWR-04 §6.5; ADR-102; 14 §20.5.4). Owner: M07 (WP-09).
// From the current keyframe: calendar instant at tRender, sun direction (mode clock: the NOAA function at the calendar
// instant when the world anchor is known, else the keyframe's az/el published by the server every >= 0.5 deg; mode fixed:
// config.sun), illuminance and band from the same functions as the server, plus the server's humidity, surface and
// acoustic background. Zero allocation per frame (the summary copies it at <= 10 Hz).
import { BAND_NAMES, illumBand, illuminanceLx, type BandName } from './illum'
import { cal0Ms } from './calendar'
import { fmod, newSunPos, solarPosition } from './solar'

export interface GeoAnchor { latDeg: number; lonDeg: number; yawOffsetDeg: number }

export interface SkyWire {
  config: { sun: { azimuth_deg: number; elevation_deg: number; mode?: 'fixed' | 'clock'; illum_lx?: number; band?: BandName } }
  t_ns: number
  calendar?: { start_utc: string; tz: string; t_cal_unix_ns?: string }
  humidity_pct?: number
  surface?: { t_c: number; emissivity: number; k_cross?: number }
  acoustic_bg?: { profile: 'rural' | 'suburban' | 'urban'; l_bg_dba: number }
}

export class SkyEval {
  /** keyframe carries D2 fields (calendar or acoustic background configured) */
  d2 = false
  mode: 'fixed' | 'clock' = 'fixed'
  /** calendar UNIX ms at tRender (NaN without a calendar) */
  tCalUnixMs = Number.NaN
  tz = ''
  startUtc = ''
  azDeg = 0
  elDeg = 0
  illumLx = 0
  band: 0 | 1 | 2 = 0
  /** server values of the current keyframe (NaN when absent) */
  humidityPct = Number.NaN
  surfaceTC = Number.NaN
  surfaceEmissivity = Number.NaN
  kCross = Number.NaN
  acousticProfile: 'rural' | 'suburban' | 'urban' | null = null
  lBgDba = Number.NaN
  /** sun direction source: 'noaa' (local NOAA at tRender), 'keyframe' (server az/el) */
  source: 'noaa' | 'keyframe' = 'keyframe'
  private readonly sp = newSunPos()
  private wireRef: unknown = null
  private cal0 = Number.NaN

  get bandName(): BandName {
    return BAND_NAMES[this.band]
  }

  update(w: SkyWire, tRenderNs: number, cover: number, geo: GeoAnchor | null): void {
    // D1 frames (no D2 fields): the fixed sun of the keyframe, nothing changes with tRender (no work per frame)
    if (w === this.wireRef && !this.d2) return
    if (w !== this.wireRef) {
      this.wireRef = w
      const c = w.calendar
      this.cal0 = c ? (cal0Ms(c, Number(w.t_ns)) ?? Number.NaN) : Number.NaN
      this.tz = c?.tz ?? ''
      this.startUtc = c?.start_utc ?? ''
      this.d2 = c !== undefined || w.acoustic_bg !== undefined || w.surface !== undefined || w.humidity_pct !== undefined
      this.humidityPct = w.humidity_pct ?? Number.NaN
      this.surfaceTC = w.surface?.t_c ?? Number.NaN
      this.surfaceEmissivity = w.surface?.emissivity ?? Number.NaN
      this.kCross = w.surface?.k_cross ?? Number.NaN
      this.acousticProfile = w.acoustic_bg?.profile ?? null
      this.lBgDba = w.acoustic_bg?.l_bg_dba ?? Number.NaN
    }
    const sun = w.config.sun
    this.mode = sun.mode === 'clock' && Number.isFinite(this.cal0) ? 'clock' : 'fixed'
    this.tCalUnixMs = Number.isFinite(this.cal0) ? this.cal0 + tRenderNs / 1e6 : Number.NaN
    if (this.mode === 'clock' && geo) {
      solarPosition(this.tCalUnixMs, geo.latDeg, geo.lonDeg, this.sp)
      this.azDeg = fmod(this.sp.azDeg - geo.yawOffsetDeg, 360)
      this.elDeg = this.sp.elDeg
      this.source = 'noaa'
    } else {
      this.azDeg = sun.azimuth_deg
      this.elDeg = sun.elevation_deg
      this.source = 'keyframe'
    }
    this.illumLx = illuminanceLx(this.elDeg, cover)
    this.band = illumBand(this.elDeg)
  }
}

/** direct-sun weight for the scene lambert term: 1 above +4 deg, 0 below -2 deg (fixed sun 50 deg -> exactly 1) */
export function daylightK(elDeg: number): number {
  return Math.min(1, Math.max(0, (elDeg + 2) / 6))
}
