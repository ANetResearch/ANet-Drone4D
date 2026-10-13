// M15-FR-215 to FR-217 (AWR-04 §10.6; AWR-14 §20.10; UX-FR-132 to UX-FR-135; M15-AC-211 unit part): input shaping (dead
// zone, expo), the keyboard smoothing, the multirotor body-frame mapping and tiers, the fixed-wing airspeed / turn / climb
// mapping, the 3 s keep-out prediction, the manual key scope outranking every other scope while leaving Shift+R to the
// global safety binding, and the setpoint pump (30 Hz path, zero tail, no React).
import { describe, expect, it, vi } from 'vitest'
import {
  DEAD_ZONE, ZERO_TAIL, fwSetpoint, isZero, mcSetpoint, mcTier, merge, nextTier, predictEntry, shape, signedDistance, smooth,
  type KeepVolume,
} from '@/ui/manual/model'

vi.setConfig({ testTimeout: 30_000 })

describe('shaping and mapping', () => {
  it('dead zone, renormalisation and expo', () => {
    expect(shape(DEAD_ZONE * 0.9)).toBe(0)
    expect(shape(1)).toBeCloseTo(1, 12)
    expect(shape(-1)).toBeCloseTo(-1, 12)
    const half = shape(0.54) // (0.54 - 0.08) / 0.92 = 0.5
    expect(half).toBeCloseTo(0.7 * 0.5 + 0.3 * 0.125, 9)
    expect(shape(Number.NaN)).toBe(0)
  })

  it('smooths keys with tau 150 ms and snaps when close', () => {
    let v = 0
    for (let i = 0; i < 3; i++) v = smooth(v, 1, 0.05)
    expect(v).toBeCloseTo(1 - Math.exp(-1), 3)
    for (let i = 0; i < 100; i++) v = smooth(v, 1, 0.05)
    expect(v).toBe(1)
  })

  it('multirotor FLU signs and tiers (fast horizontal capped by vmax)', () => {
    const out = new Float32Array(4)
    mcSetpoint({ fwd: 1, right: 1, up: -1, yawRight: 1 }, 'normal', { vmaxMps: 12 }, out)
    expect([...out]).toEqual([5, -5, -2, Math.fround(-0.8)])
    expect(mcTier('fast', { vmaxMps: 8 }).h).toBe(8)
    expect(mcTier('fast', { vmaxMps: 15 }).h).toBe(10)
    expect(mcTier('slow').v).toBe(1)
    expect(nextTier('fast')).toBe('slow')
  })

  it('fixed wing integrates airspeed at 2 m/s² within [V_min, V_max] and turns within g tan(phi) / V', () => {
    const out = new Float32Array(4)
    const lim = { vMinMps: 14, vMaxMps: 30, vCruiseMps: 20, bankMaxRad: Math.PI / 6, climbMaxMps: 4 }
    const st = { vCmd: Number.NaN }
    fwSetpoint({ fwd: 1, right: 0, up: 1, yawRight: 1 }, 'fast', lim, st, 1, 20, out)
    expect(out[0]).toBeCloseTo(22, 5)
    expect(Number.isNaN(out[1])).toBe(true)
    expect(out[2]).toBeCloseTo(4, 5)
    expect(out[3]).toBeCloseTo(-(9.80665 * Math.tan(Math.PI / 6)) / 20, 5)
    for (let i = 0; i < 20; i++) fwSetpoint({ fwd: 1, right: 0, up: 0, yawRight: 0 }, 'fast', lim, st, 1, 20, out)
    expect(out[0]).toBe(30)
  })

  it('merges keys and sticks by the larger magnitude', () => {
    const o = { fwd: 0, right: 0, up: 0, yawRight: 0 }
    merge({ fwd: 0.2, right: -1, up: 0, yawRight: 0 }, { fwd: -0.5, right: 0.3, up: 0, yawRight: 0 }, o)
    expect(o).toEqual({ fwd: -0.5, right: -1, up: 0, yawRight: 0 })
    expect(isZero({ fwd: 0, right: 0, up: 0, yawRight: 0 })).toBe(true)
  })
})

describe('keep-out prediction', () => {
  const sphere: KeepVolume = { target: 'person-01', kind: 'acoustic', shape: 'hemisphere', c: [0, 0, 0], r: 100, zMin: 0, zMax: 100 }
  const cyl: KeepVolume = { target: 'dog-01', kind: 'visual', shape: 'cylinder', c: [500, 0, 0], r: 50, zMin: 0, zMax: 120 }

  it('signed distances (outside positive)', () => {
    expect(signedDistance([200, 0, 0], sphere).d).toBeCloseTo(100, 9)
    expect(signedDistance([50, 0, 0], sphere).d).toBeCloseTo(-50, 9)
    expect(signedDistance([500, 80, 60], cyl).d).toBeCloseTo(30, 9)
    expect(signedDistance([500, 0, 200], cyl).d).toBeGreaterThan(0)
  })

  it('warns 3 s ahead of the entry, not earlier, and at once inside', () => {
    expect(predictEntry([130, 0, 0], [-10, 0, 0], [sphere])?.tS).toBeCloseTo(3, 6)
    expect(predictEntry([131, 0, 0], [-10, 0, 0], [sphere])).toBeNull()
    expect(predictEntry([130, 0, 0], [10, 0, 0], [sphere])).toBeNull()
    expect(predictEntry([0, 10, 10], [0, 0, 0], [sphere])?.tS).toBe(0)
    const a = predictEntry([500, 70, 50], [0, -10, 0], [sphere, cyl])
    expect(a?.target).toBe('dog-01')
    expect(a?.tS).toBeCloseTo(2, 6)
  })
})

describe('manual key scope and pump', () => {
  function key(code: string, shift = false) {
    let prevented = false
    const ev = { code, ctrlKey: false, metaKey: false, shiftKey: shift, altKey: false, isComposing: false, defaultPrevented: false, target: null,
      preventDefault: () => { prevented = true } }
    return { ev: ev as unknown as KeyboardEvent, prevented: () => prevented }
  }

  it('outranks earlier and later global bindings and leaves Shift+R to the safety binding', async () => {
    const { dispatchKey, registerHotkey } = await import('@/ui/hotkeys/registry')
    const hits: string[] = []
    let active = true
    const offManual = registerHotkey({ id: 't.manual.f', combo: 'KeyF', scope: 'manual', labelKey: 'x', when: () => active, deniedKey: () => null, run: () => hits.push('manual') })
    const offLater = registerHotkey({ id: 't.global.f', combo: 'KeyF', labelKey: 'x', run: () => hits.push('global') })
    const offSafety = registerHotkey({ id: 't.global.sr', combo: 'shift+KeyR', labelKey: 'x', run: () => hits.push('rtl') })
    expect(dispatchKey(key('KeyF').ev)).toBe(true)
    expect(hits).toEqual(['manual'])
    expect(dispatchKey(key('KeyR', true).ev)).toBe(true)
    expect(hits).toEqual(['manual', 'rtl'])
    active = false
    expect(dispatchKey(key('KeyF').ev)).toBe(true)
    expect(hits).toEqual(['manual', 'rtl', 'global'])
    offManual()
    offLater()
    offSafety()
  })

  it('the pump sends setpoints while ACTIVE and a zero tail after release of the inputs', async () => {
    const c = await import('@/ui/manual/controller')
    const sent: number[][] = []
    const rt = {
      roster: { agentNoOf: (id: string) => (id === 'p600-01' ? 3 : -1) },
      publishSetpoint: (no: number, vx: number, vy: number, vz: number, yaw: number) => sent.push([no, vx, vy, vz, yaw]),
    }
    c.setManualDeps({ rt: () => rt as never, limits: () => ({ fw: false, mc: { vmaxMps: 10 }, fwl: null }), volumes: () => [] })
    c.manualStore.setState({ state: 'ACTIVE', vehicle: 'p600-01', tier: 'normal', fw: false })
    c.setStick('right', 0, 1)
    let t = 1000
    for (let i = 0; i < 5; i++) c.pumpTick((t += 33))
    expect(sent.length).toBe(5)
    expect(sent.at(-1)?.[0]).toBe(3)
    expect(sent.at(-1)?.[1]).toBeCloseTo(5, 5)
    c.setStick('right', 0, 0)
    for (let i = 0; i < ZERO_TAIL; i++) c.pumpTick((t += 33))
    expect(sent.length).toBe(5 + ZERO_TAIL)
    expect(sent.at(-1)?.slice(1).map(Math.abs)).toEqual([0, 0, 0, 0])
    // idle: one zero setpoint every IDLE_KEEPALIVE_TICKS ticks keeps the stream past the server's 250 ms watchdog
    for (let i = 0; i < 3 * c.IDLE_KEEPALIVE_TICKS; i++) c.pumpTick((t += 33))
    expect(sent.length).toBe(5 + ZERO_TAIL + 3)
    expect(sent.at(-1)?.slice(1).map(Math.abs)).toEqual([0, 0, 0, 0])
    c.manualStore.setState({ state: 'IDLE', vehicle: null })
    expect(c.pumpTick((t += 33))).toBe(false)
  })
})
