// M15-FR-003..005 (§6.3.2): route matching, specificity order, validation and query parsers.
import { describe, expect, it } from 'vitest'
import { compile, rank, sortRoutes } from '@/app/router/match'
import { matchPath, setRoutes, type RouteDef } from '@/app/router/router'
import { parseCam, parseChrome, parseSel, parseSettings, parseT, parseTier, WORLD_ID } from '@/app/router/search'

const r = (id: string, pattern: string, extra: Partial<RouteDef> = {}): RouteDef => ({ id, pattern, layer: 'sandbox', d1: 'core', ...extra })

describe('match', () => {
  it('extracts parameters and rejects other shapes', () => {
    const c = compile('w', '/world/:id')
    expect(c.test('/world/shenzhen')).toEqual({ id: 'shenzhen' })
    expect(c.test('/world')).toBeNull()
    expect(c.test('/world/a/b')).toBeNull()
  })

  it('orders static segments before parameters', () => {
    expect(rank('/world/:id')).toEqual([1, 1])
    const sorted = sortRoutes([compile('p', '/:x'), compile('w', '/worlds'), compile('r', '/')])
    expect(sorted[0].pattern).toBe('/worlds')
  })
})

describe('matchPath', () => {
  it('uses validate and returns the parsed search', () => {
    setRoutes([
      r('index', '/'),
      r('worlds', '/worlds', { layer: 'page' }),
      r('world', '/world/:id', { validate: (p) => WORLD_ID.test(p.id) }),
    ])
    expect(matchPath('/world/shenzhen', '?cam=fpv')?.route.id).toBe('world')
    expect(matchPath('/world/shenzhen', '?cam=fpv')?.search.get('cam')).toBe('fpv')
    expect(matchPath('/world/Bad_Id')).toBeNull()
    expect(matchPath('/worlds')?.route.layer).toBe('page')
    expect(matchPath('/nowhere')).toBeNull()
  })
})

describe('search parsers', () => {
  const q = (s: string) => new URLSearchParams(s)
  it('accepts only known values', () => {
    expect(parseCam(q('cam=bird'))).toBe('bird')
    expect(parseCam(q('cam=drone'))).toBeUndefined()
    expect(parseSettings(q('settings=motion'))).toBe('motion')
    expect(parseSettings(q('settings=x'))).toBeUndefined()
    expect(parseTier(q('tier=S'))).toBe('S')
    expect(parseTier(q('tier=Z'))).toBeUndefined()
    expect(parseChrome(q('chrome=0'))).toBe(false)
    expect(parseChrome(q(''))).toBe(true)
  })

  it('parses selection lists and times', () => {
    expect(parseSel(q('sel=3,7'))).toEqual(['3', '7'])
    expect(parseSel(q(''))).toBeUndefined()
    expect(parseT(q('t=12.5'))).toBe(12.5)
    expect(parseT(q('t=abc'))).toBeUndefined()
  })
})

describe('hotkey labels', () => {
  it('shows physical key codes in their printed form', async () => {
    const { comboLabel, comboOf } = await import('@/ui/hotkeys/registry')
    expect(comboLabel('mod+KeyK')).toEqual(['Ctrl', 'K'])
    expect(comboLabel('Digit3')).toEqual(['3'])
    expect(comboLabel('shift+Slash')).toEqual(['Shift', '/'])
    expect(comboOf({ code: 'KeyB', ctrlKey: true, metaKey: false, shiftKey: false, altKey: false })).toBe('mod+KeyB')
  })
})
