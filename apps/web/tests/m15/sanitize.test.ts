// M15-FR-108 (D1-AC-20 notes 4, 5): runtime text loses emoji and forbidden glyph blocks; arrows, units and CJK stay.
// Forbidden characters are built from code points so this source file itself stays clean.
import { describe, expect, it } from 'vitest'
import { hasForbidden, sanitizeStats, sanitizeText } from '@/lib/sanitize'

const cp = (...c: number[]) => String.fromCodePoint(...c)

describe('sanitizeText', () => {
  it('removes emoji, presentation selectors, joiners, keycaps, skin tones and flags', () => {
    expect(sanitizeText(`ok ${cp(0x1f680)} go`)).toBe('ok go')
    expect(sanitizeText(`heart${cp(0x2764, 0xfe0f)}`)).toBe('heart')
    expect(sanitizeText(`fam${cp(0x1f468, 0x200d, 0x1f469, 0x200d, 0x1f467)}ily`)).toBe('family')
    expect(sanitizeText(`key ${cp(0x31, 0xfe0f, 0x20e3)}`)).toBe('key 1')
    expect(sanitizeText(`wave${cp(0x1f44b, 0x1f3fd)}`)).toBe('wave')
    expect(sanitizeText(`flag${cp(0x1f1e8, 0x1f1f3)}`)).toBe('flag')
    expect(sanitizeText(`tag${cp(0x1f3f4, 0xe0067, 0xe0062, 0xe007f)}`)).toBe('tag')
  })

  it('removes the D1-AC-20 glyph blocks (geometric shapes, misc symbols, dingbats, arrows from U+2194)', () => {
    for (const c of [0x25a0, 0x25cf, 0x25ff, 0x2600, 0x2605, 0x26ff, 0x2700, 0x2714, 0x27bf, 0x2194, 0x21d2, 0x21ff]) {
      expect(sanitizeText(`a${cp(c)}b`)).toBe('ab')
      expect(hasForbidden(`a${cp(c)}b`)).toBe(true)
    }
  })

  it('keeps basic arrows, units, maths and CJK', () => {
    const keep = `${cp(0x2190)}${cp(0x2191)}${cp(0x2192)}${cp(0x2193)} 12 m/s ${cp(0xb0)} ${cp(0x2212)}3 ${cp(0x00b7)} 无人机 #1 *`
    expect(sanitizeText(keep)).toBe(keep)
    expect(hasForbidden(keep)).toBe(false)
  })

  it('collapses whitespace and trims', () => {
    expect(sanitizeText('  a \n\t b  ')).toBe('a b')
    expect(sanitizeText(null)).toBe('')
    expect(sanitizeText(undefined)).toBe('')
  })

  it('caps at max code points and ends with an em dash (M15-E009)', () => {
    const before = sanitizeStats.truncations
    const long = 'x'.repeat(300)
    const out = sanitizeText(long)
    expect(Array.from(out)).toHaveLength(256)
    expect(out.endsWith(cp(0x2014))).toBe(true)
    expect(sanitizeStats.truncations).toBe(before + 1)
    // code points, not UTF-16 units
    const cjk = '机'.repeat(10)
    expect(sanitizeText(cjk, 5)).toBe(`机机机机${cp(0x2014)}`)
    expect(sanitizeText('abc', 5)).toBe('abc')
  })

  it('returns the cached result for repeated input', () => {
    const s = `repeat ${cp(0x1f600)}`
    expect(sanitizeText(s)).toBe('repeat')
    expect(sanitizeText(s)).toBe('repeat')
  })
})
