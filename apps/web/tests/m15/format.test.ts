// M15-FR-105 (AWR-15 §5.3-§5.4; AWR-03 §5.3): the only formatter; U+2212 minus, em dash for unknown, SI suffixes.
import { describe, expect, it } from 'vitest'
import { fmt, MINUS, UNKNOWN } from '@/lib/format'

describe('fmt', () => {
  it('uses the em dash for unknown values and sentinels', () => {
    for (const v of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(fmt.alt(v)).toBe(UNKNOWN)
      expect(fmt.count(v)).toBe(UNKNOWN)
    }
    expect(fmt.pct(255)).toBe(UNKNOWN)
    expect(UNKNOWN).toBe(String.fromCodePoint(0x2014))
  })

  it('formats negative numbers with U+2212', () => {
    expect(MINUS).toBe(String.fromCodePoint(0x2212))
    expect(fmt.num(-3.5, 1)).toBe(`${MINUS}3.5`)
    expect(fmt.num(-0.01, 1)).toBe('0.0')
  })

  it('formats telemetry with units', () => {
    expect(fmt.alt(82.34)).toBe('82.3 m AGL')
    expect(fmt.alt(12, 'MSL')).toBe('12.0 m MSL')
    expect(fmt.speed(7.25)).toBe('7.3 m/s')
    expect(fmt.pct(78.4)).toBe('78%')
    expect(fmt.mmh(22)).toBe('22.0 mm/h')
    expect(fmt.ms(16.667)).toBe('16.7 ms')
    expect(fmt.fps(29.6)).toBe('30 fps')
  })

  it('converts ENU yaw to a 3-digit compass heading', () => {
    expect(fmt.heading(Math.PI / 2)).toBe(`000${String.fromCodePoint(0xb0)}`)
    expect(fmt.heading(0)).toBe(`090${String.fromCodePoint(0xb0)}`)
    expect(fmt.heading(Math.PI / 4)).toBe(`045${String.fromCodePoint(0xb0)}`)
    expect(fmt.dirDeg(-90)).toBe(`270${String.fromCodePoint(0xb0)}`)
  })

  it('uses 3 significant digits for point counts and bytes', () => {
    expect(fmt.pts(999)).toBe('999')
    expect(fmt.pts(25_000)).toBe('25.0K')
    expect(fmt.pts(350_400)).toBe('350K')
    expect(fmt.pts(4_823_000)).toBe('4.82M')
    expect(fmt.bytes(512 * 1024 * 1024)).toBe('512 MiB')
    expect(fmt.bytes(1.25 * 1024 ** 3)).toBe('1.25 GiB')
    expect(fmt.bytes(10)).toBe('10 B')
  })

  it('groups counts only from 10000', () => {
    expect(fmt.count(9999)).toBe('9999')
    expect(fmt.count(12345)).toBe('12,345')
  })

  it('formats visibility as MOR', () => {
    expect(fmt.mor(851)).toBe('851 m')
    expect(fmt.mor(12_345)).toBe('12.3 km')
  })

  it('formats simulation time from nanoseconds', () => {
    expect(fmt.simTime(0)).toBe('T+00:00:00.0')
    expect(fmt.simTime((12 * 60 + 5.24) * 1e9)).toBe('T+00:12:05.2')
    expect(fmt.simTime(3 * 3600 * 1e9)).toBe('T+03:00:00.0')
  })

  it('formats ENU and staleness', () => {
    expect(fmt.enu(120.45, -33.1, 82.3)).toBe(`E 120.45 ${String.fromCodePoint(0xb7)} N ${MINUS}33.10 ${String.fromCodePoint(0xb7)} U 82.30`)
    expect(fmt.stale(3.21)).toBe('STALE 3.2 S')
  })
})
