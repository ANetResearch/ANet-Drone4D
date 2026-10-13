// Simulation calendar on the client (AWR-04 §6.5; ADR-102; AWR-17 §17.6.9). Owner: M07 (WP-09).
// The keyframe carries calendar{start_utc, tz, t_cal_unix_ns} with t_cal_unix_ns at the frame's t_ns (decimal string,
// heartbeats refresh it); the calendar instant at tRender is cal0 + tRender with cal0 = t_cal_unix_ns - t_ns. parseUtcMs
// mirrors python/awr/environment/sky/calendar.py (ISO 8601, Z or +-HH:MM, no offset = UTC, years 1901-2099) and is what a
// calendar input (M15, InputGroup + regex) can validate against before calling env/calendar.

const ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?$/
export const YEAR_MIN = 1901
export const YEAR_MAX = 2099

/** ISO 8601 -> UNIX ms (truncated to ms), null when malformed or out of range */
export function parseUtcMs(s: string): number | null {
  const m = ISO.exec(s.trim())
  if (!m) return null
  const [y, mo, d, hh, mm] = [1, 2, 3, 4, 5].map((i) => Number(m[i]))
  const ss = Number(m[6] ?? 0)
  const frac = Number((m[7] ?? '').padEnd(3, '0').slice(0, 3))
  if (y < YEAR_MIN || y > YEAR_MAX || mo < 1 || mo > 12 || d < 1 || hh > 23 || mm > 59 || ss > 59) return null
  let t = Date.UTC(y, mo - 1, d, hh, mm, ss, frac)
  const chk = new Date(t)
  if (chk.getUTCDate() !== d || chk.getUTCMonth() !== mo - 1) return null
  const off = m[8]
  if (off && off !== 'Z') {
    const sign = off[0] === '+' ? 1 : -1
    const dg = off.slice(1).replace(':', '')
    const oh = Number(dg.slice(0, 2))
    const om = Number(dg.slice(2))
    if (oh > 18 || om > 59) return null
    t -= sign * (oh * 60 + om) * 60000
  }
  return t
}

/** cal0 (calendar UNIX ms at t_sim = 0) from the wire calendar of a keyframe at t_ns */
export function cal0Ms(cal: { start_utc: string; t_cal_unix_ns?: string }, tNs: number): number | null {
  if (cal.t_cal_unix_ns !== undefined && /^[0-9]{1,20}$/.test(cal.t_cal_unix_ns)) {
    return Number(BigInt(cal.t_cal_unix_ns) - BigInt(Math.round(tNs))) / 1e6
  }
  return parseUtcMs(cal.start_utc)
}

const FMT = new Map<string, Intl.DateTimeFormat | null>()

function formatter(tz: string): Intl.DateTimeFormat | null {
  let f = FMT.get(tz)
  if (f === undefined) {
    try {
      f = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
        hourCycle: 'h23' })
    } catch {
      f = null
    }
    FMT.set(tz, f)
  }
  return f
}

/** 'YYYY-MM-DD HH:mm' in the IANA zone (UTC with a trailing 'Z' when the zone is unknown) */
export function formatLocal(unixMs: number, tz: string): string {
  const f = formatter(tz)
  if (!f) return new Date(unixMs).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
  const p: Record<string, string> = {}
  for (const part of f.formatToParts(new Date(unixMs))) p[part.type] = part.value
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`
}

/** whether the runtime knows the IANA zone */
export function validTz(tz: string): boolean {
  return formatter(tz) !== null
}
