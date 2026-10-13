// Simulation calendar presets of the sandbox entry (AWR-14 §20.4.1 "仿真时段": day 10:00, dusk 18:30, night 23:00 in the
// world's time zone; ADR-102; AWR-17 §17.4.2 `calendar{start_utc, tz}`). Pure functions on Intl (no time zone library):
// the UTC instant of a wall-clock time in an IANA zone, found from the zone offset at that instant (two passes cover the
// offset change of a DST day).

export type DayPeriod = 'day' | 'dusk' | 'night'
export const PERIOD_TIME: Readonly<Record<DayPeriod, { h: number; m: number }>> = { day: { h: 10, m: 0 }, dusk: { h: 18, m: 30 }, night: { h: 23, m: 0 } }
export const DEFAULT_TZ = 'Asia/Shanghai'

/** offset of `tz` at the UTC instant `utcMs` (ms; positive east of UTC); 0 for an unknown zone */
export function tzOffsetMs(utcMs: number, tz: string): number {
  try {
    const f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
    const p: Record<string, number> = {}
    for (const x of f.formatToParts(new Date(utcMs))) if (x.type !== 'literal') p[x.type] = Number(x.value)
    const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour === 24 ? 0 : p.hour, p.minute, p.second)
    return asUtc - Math.floor(utcMs / 1000) * 1000
  } catch {
    return 0
  }
}

/** the calendar date (y, m, d) of the instant `utcMs` in `tz` */
export function localDate(utcMs: number, tz: string): { y: number; m: number; d: number } {
  const t = new Date(utcMs + tzOffsetMs(utcMs, tz))
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() }
}

/** UTC instant of the wall-clock time y-m-d hh:mm in `tz` */
export function zonedToUtcMs(y: number, m: number, d: number, hh: number, mm: number, tz: string): number {
  const guess = Date.UTC(y, m - 1, d, hh, mm)
  const first = guess - tzOffsetMs(guess, tz)
  return guess - tzOffsetMs(first, tz)
}

/** ISO 8601 UTC without milliseconds (the form the server validates, d2-WP-09 item 3) */
export function isoUtc(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** `{start_utc, tz}` of a period today (in the world's zone) */
export function periodCalendar(period: DayPeriod, tz: string = DEFAULT_TZ, nowMs = Date.now()): { start_utc: string; tz: string } {
  const { y, m, d } = localDate(nowMs, tz)
  const { h, m: mm } = PERIOD_TIME[period]
  return { start_utc: isoUtc(zonedToUtcMs(y, m, d, h, mm, tz)), tz }
}

const RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/

/** `YYYY-MM-DD HH:mm` in `tz` as the UTC instant the server takes (null when malformed; pure, tests) */
export function localToUtc(text: string, tz: string): string | null {
  const m = RE.exec(text.trim())
  if (!m) return null
  const [y, mo, d, h, mi] = m.slice(1).map(Number)
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59) return null
  const ms = zonedToUtcMs(y, mo, d, h, mi, tz)
  return Number.isFinite(ms) ? isoUtc(ms) : null
}

/** the period's wall time on the date of `localTime` (`YYYY-MM-DD HH:mm`) */
export function periodText(localTime: string, p: DayPeriod): string {
  const day = RE.test(localTime) ? localTime.slice(0, 10) : new Date().toISOString().slice(0, 10)
  const { h, m } = PERIOD_TIME[p]
  return `${day} ${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}
