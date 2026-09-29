// M15-FR-059..065 (ADR-030; AWR-15 §7.6; d03 §3.6, §4.2): semantic registry, morph whitelist, telemetry buckets.
import { describe, expect, it } from 'vitest'
import { ICON_GEOMETRY_COUNT, ICON_KEY_COUNT, ICONS, PARTNERS, ROTATE_KEYS } from '@/ui/icons/registry'
import { MORPH_PAIRS, morphSpring } from '@/ui/icons/whitelist'
import { iconD } from '@/ui/icons/Icon'
import { bucketStep, nextLevel } from '@/ui/icons/bucket'
import { ICON_BUCKETS, INPUT } from '@/lib/tokens/input.gen'

describe('icon registry', () => {
  it('exposes dotted semantic keys with lucide or custom geometry', () => {
    const keys = Object.keys(ICONS)
    expect(keys).toHaveLength(ICON_KEY_COUNT)
    expect(new Set(Object.values(ICONS)).size).toBe(ICON_GEOMETRY_COUNT)
    for (const k of ['conn.online', 'nav.settings', 'cmd.search', 'tl.clock', 'bat.full', 'link.high', 'notify']) expect(keys).toContain(k)
    for (const node of Object.values(ICONS)) {
      expect(Array.isArray(node)).toBe(true)
      expect(node.length).toBeGreaterThan(0)
    }
  })

  it('builds a path string for every geometry', () => {
    for (const node of Object.values(ICONS)) expect(typeof iconD(node)).toBe('string')
  })

  it('keeps angle keys out of the morph whitelist (rotation only)', () => {
    expect(ROTATE_KEYS.has('heading')).toBe(true)
    for (const [a, b, , key] of MORPH_PAIRS) {
      expect(ROTATE_KEYS.has(key as never)).toBe(false)
      expect(a).not.toBe(b)
    }
  })

  it('allows whitelisted morphs in both directions and refuses others', () => {
    for (const [a, b, spring] of MORPH_PAIRS) {
      expect(morphSpring(a, b)).toBe(spring)
      expect(morphSpring(b, a)).toBe(spring)
    }
    expect(morphSpring(ICONS['cmd.search'], ICONS['bat.full'])).toBeNull()
  })

  it('gives the connection key a partner geometry for the alt state', () => {
    expect(PARTNERS['conn.online']).toBeDefined()
  })
})

describe('telemetry buckets (hysteresis 3 %, dwell 1.5 s)', () => {
  const edges = ICON_BUCKETS.batteryEdgesPct
  it('needs edge + hysteresis to go up and drops at once', () => {
    expect(nextLevel(50, 1, edges)).toBe(2)
    expect(nextLevel(41, 1, edges)).toBe(1)
    expect(nextLevel(43, 1, edges)).toBe(2)
    expect(nextLevel(39, 2, edges)).toBe(1)
    expect(nextLevel(5, 3, edges)).toBe(0)
    expect(nextLevel(100, 0, edges)).toBe(3)
  })

  it('keeps a level for the dwell time unless an alarm escalates', () => {
    const s0 = { level: 3, since: 0 }
    expect(bucketStep(s0, 50, 100, false, edges)).toBe(s0)
    const s1 = bucketStep(s0, 50, INPUT.iconDwellMs, false, edges)
    expect(s1.level).toBe(2)
    expect(bucketStep(s1, 10, INPUT.iconDwellMs + 1, true, edges).level).toBe(0)
    expect(bucketStep(s1, 10, INPUT.iconDwellMs + 1, false, edges)).toBe(s1)
  })
})
