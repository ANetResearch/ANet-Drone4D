// M15-FR-046 (CN-01): the project cn knows every @theme key added by styles/**, so custom sizes, durations, easings,
// blurs and shadows take part in conflict resolution instead of swallowing colour classes.
import { describe, expect, it } from 'vitest'
import { cn, CN_DURATIONS, CN_THEME } from '@/lib/utils'

describe('cn (createCn with the AWR theme)', () => {
  it('keeps a text colour next to a custom text size', () => {
    expect(cn('text-hud-sub', 'text-muted-foreground')).toBe('text-hud-sub text-muted-foreground')
    expect(cn('text-ed-kpi text-brand-text')).toBe('text-ed-kpi text-brand-text')
  })

  it('lets the later custom text size win over a stock size', () => {
    expect(cn('text-sm', 'text-hud-kpi')).toBe('text-hud-kpi')
    expect(cn('text-hud-kpi', 'text-sm')).toBe('text-sm')
  })

  it('resolves duration, ease, blur and shadow tokens as one group each', () => {
    expect(cn('duration-quick', 'duration-slow')).toBe('duration-slow')
    expect(cn('duration-lod-fade', 'duration-fast')).toBe('duration-fast')
    expect(cn('ease-smooth-out', 'ease-in-out')).toBe('ease-in-out')
    expect(cn('blur-small', 'blur-large')).toBe('blur-large')
    expect(cn('shadow-pop', 'shadow-md')).toBe('shadow-md')
  })

  it('drops falsy values and merges conditional classes', () => {
    const off = false as boolean
    expect(cn('px-2', off && 'px-4', null, undefined, 'px-3')).toBe('px-3')
  })

  it('registers every namespace listed in CN_THEME', () => {
    expect(CN_THEME.text.length).toBeGreaterThanOrEqual(12)
    expect(CN_DURATIONS).toContain('very-slow')
    for (const d of CN_DURATIONS) expect(cn('duration-micro', `duration-${d}`)).toBe(`duration-${d}`)
  })
})
