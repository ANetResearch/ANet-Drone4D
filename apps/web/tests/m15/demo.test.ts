// Public demo build (ADR-083; M15-FR-120..123): VITE_AWR_DEMO=public turns on the demo branches (world filter, read-only
// texts, demo-only world route); a default build keeps every branch off. The switch is read at module evaluation, so each
// case re-imports the modules after stubbing the variable.
// D2 (ADR-109, ADR-111; M15 §15.3): the demo build serves seven worlds and registers the sandbox and open interface routes;
// the read-only texts say the shared show is read-only and point at the sandbox.
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

async function demoModules(on: boolean) {
  vi.resetModules()
  if (on) vi.stubEnv('VITE_AWR_DEMO', 'public')
  const demo = await import('@/lib/demo')
  const i18n = await import('@/app/i18n')
  const world = await import('@/app/routes/world')
  return { demo, i18n, world }
}

describe('public demo build switch', () => {
  it('is off in a default build: every world, the usual read-only texts, any world id', async () => {
    const { demo, i18n, world } = await demoModules(false)
    expect(demo.DEMO_PUBLIC).toBe(false)
    expect(demo.demoWorlds([{ id: 'shenzhen' }, { id: 'synthcity' }]).map((w) => w.id)).toEqual(['shenzhen', 'synthcity'])
    expect(i18n.t('hint.readOnly')).toBe(i18n.t('hint.readOnly'))
    expect(i18n.t('hint.readOnly')).not.toBe(i18n.t('demo.readOnly'))
    expect(world.route.validate?.({ id: 'shenzhen' })).toBe(true)
  })

  it('in the demo build lists the seven worlds, accepts their routes and words every read-only reason as the shared show', async () => {
    const { demo, i18n, world } = await demoModules(true)
    expect(demo.DEMO_PUBLIC).toBe(true)
    expect(demo.DEMO_ENTRY).toBe('/world/synthcity')
    expect(demo.DEMO_WORLDS).toHaveLength(7)
    expect(demo.demoWorlds([{ id: 'shenzhen' }, { id: 'synthcity' }, { id: 'newyork' }, { id: 'paris' }]).map((w) => w.id)).toEqual(['shenzhen', 'synthcity', 'newyork'])
    expect(world.route.validate?.({ id: 'synthcity' })).toBe(true)
    expect(world.route.validate?.({ id: 'shenzhen' })).toBe(true)
    expect(world.route.validate?.({ id: 'paris' })).toBe(false)
    expect(demo.DEMO_ROUTES.has('sandbox') && demo.DEMO_ROUTES.has('sandboxApi')).toBe(true)
    expect(i18n.t('demo.readOnly')).toBe('共享展示为只读，开启我的沙盒即可操作')
    const ro = i18n.t('demo.readOnly')
    for (const k of ['hint.readOnly', 'detail.readOnly', 'hint.needSeat', 'hint.replaySeat']) expect(i18n.t(k)).toBe(ro)
    expect(i18n.t('role.viewer')).toBe(i18n.t('demo.role'))
    i18n.setLocale('en')
    try {
      expect(i18n.t('hint.readOnly')).toBe('The shared show is read-only; open your own sandbox to issue commands')
      expect(i18n.t('landing.launch')).toBe('Enter shared show')
    } finally {
      i18n.setLocale('zh-CN')
    }
    expect(demo.DEMO_ROUTES.has('jobs') || demo.DEMO_ROUTES.has('runs') || demo.DEMO_ROUTES.has('bench')).toBe(false)
    expect(demo.DEMO_HIDDEN_ACTIONS.has('jobs.open') && demo.DEMO_HIDDEN_ACTIONS.has('replay.runs')).toBe(true)
  })
})
