// Runtime text sanitising (M15-FR-108; AWR-03 §8.4 D1-AC-20 notes 4 and 5; ADR-030): text that comes from agents, LLMs,
// ANet, scenario and vehicle names, reason messages and user input loses every emoji (Extended_Pictographic,
// Emoji_Presentation, variation selector U+FE0F, ZWJ U+200D, keycap U+20E3, skin tones, regional indicators, tag
// characters; skin tones and regional indicators are Emoji_Presentation, the joiners stay outside the class so
// no combining sequence sits in it) and every forbidden glyph block (U+25A0-U+25FF, U+2600-U+26FF, U+2700-U+27BF, U+2194-U+21FF) before it is
// shown. Arrows U+2190-U+2193 and mathematical and unit symbols are kept. Whitespace runs collapse to one space; the
// result is capped at `max` code points (default 256) and then ends with an em dash (M15-E009). LRU cache of 512.
import { LIMITS } from './tokens/input.gen'

const STRIP =
  /[\p{Extended_Pictographic}\p{Emoji_Presentation}\u{E0020}-\u{E007F}\u{25A0}-\u{25FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{2194}-\u{21FF}]|\u{FE0F}|\u{200D}|\u{20E3}/gu
const DASH = '—'
const lru = new Map<string, string>()
let truncations = 0

export function sanitizeText(s: string | null | undefined, max: number = LIMITS.sanitizeMaxCodepoints): string {
  if (s === null || s === undefined) return ''
  const key = max === LIMITS.sanitizeMaxCodepoints ? s : `${max}\u0000${s}`
  const hit = lru.get(key)
  if (hit !== undefined) {
    lru.delete(key)
    lru.set(key, hit)
    return hit
  }
  let out = s.replace(STRIP, '').replace(/\s+/g, ' ').trim()
  if (out.length > max) {
    const cp = Array.from(out)
    if (cp.length > max) {
      out = cp.slice(0, Math.max(0, max - 1)).join('') + DASH
      truncations++
    }
  }
  lru.set(key, out)
  if (lru.size > LIMITS.sanitizeLruCap) lru.delete(lru.keys().next().value as string)
  return out
}

/** true when the text still contains a forbidden code point (tests, dev assertions) */
export function hasForbidden(s: string): boolean {
  STRIP.lastIndex = 0
  const r = STRIP.test(s)
  STRIP.lastIndex = 0
  return r
}

/** M15-E009 counter */
export const sanitizeStats = { get truncations() { return truncations } }
