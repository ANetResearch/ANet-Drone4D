// EnvStore client synchronisation (M07-AC-021 and M07-AC-025 unit parts; M07 §6.6.2, §6.3.10, §6.3.15):
// C01 first frame, C02 version switch exactly when tRender crosses t_apply_ns, C03 heartbeat anchor correction, C04 STALE
// after 3 s wall, C05 recovery, C06/C07 epoch wait and snap, C08 presets hash mismatch; forward play equals a jump to the
// same t; step frames start the 400 ms visual damping while physics scalars switch at once; zero allocation in update.
import { describe, expect, it } from 'vitest'
import { EnvStore, F, H_NS, PRESETS_MODEL, advance, copyAnchors, decodeKeyframe, newAnchors, type EnvKeyframeWire } from '@/engine/environment'

const SEC = 1e9

function wire(o: Partial<EnvKeyframeWire> & { preset?: string; fromPreset?: string } = {}): EnvKeyframeWire {
  const to = Array.from(PRESETS_MODEL.presetVector(o.preset ?? 'clear'))
  const from = Array.from(PRESETS_MODEL.presetVector(o.fromPreset ?? o.preset ?? 'clear'))
  const t = o.t_apply_ns ?? 0
  return {
    schema: 'awr.env.keyframe.v1', world_id: 'w', version: 1, epoch: 1, seed: 1, t_ns: t, t_apply_ns: t,
    config: { wind: { level: 1, profile: { kind: 'log', z_ref_m: 10, z0_m: 0.5, d_m: 0, alpha: 0.25, adv_height_m: 40 }, library: null,
      turbulence: { model: 'box', n: 64, dx_m: 4, l_m: 30 }, gust: { model: 'cos1_front', max_active: 4 } },
    sun: { azimuth_deg: 150, elevation_deg: 50 }, weather_map: { n: 512, scale_m: 24000 }, presets_sha256: PRESETS_MODEL.sha256 },
    mode: 'step', t0_ns: t, t1_ns: t, from, to, via: [], to_preset: o.preset ?? 'clear',
    anchors: { t_ns: t, s_m: 0, d_enu_m: [0, 0, 0], fall_rain_m: 0, fall_snow_m: 0, wetness: 0, puddle: 0 }, events: [],
    vis: { streamlines: null, vmax_mps: 20 },
    ...o,
  } as EnvKeyframeWire
}

describe('EnvStore', () => {
  it('C01 first frame, C02 switch exactly at t_apply, forward play == jump', () => {
    const s = new EnvStore()
    expect(s.state).toBe('EMPTY')
    s.ingest(wire({ t_apply_ns: 0 }), 0)
    s.update(0.5, 10)
    expect(s.state).toBe('SYNCED')
    expect(s.version).toBe(1)
    // v2 applies at 2.02 s: a smooth transition to rain
    const v2 = wire({ version: 2, preset: 'rain', fromPreset: 'clear', mode: 'smooth', t_apply_ns: 2.02 * SEC, t0_ns: 2.02 * SEC, t1_ns: 32.02 * SEC })
    // the change frame carries anchors at t_apply: compute them as the server would
    const A = newAnchors()
    copyAnchors(decodeKeyframe(wire()).anchors, A)
    advance(A, decodeKeyframe(wire()), 0, 101)
    v2.anchors = { t_ns: 2.02 * SEC, s_m: A.sM, d_enu_m: [A.d[0], A.d[1], A.d[2]], fall_rain_m: A.fallRain, fall_snow_m: A.fallSnow, wetness: A.wetness, puddle: A.puddle }
    s.ingest(v2, 20)
    s.update(2.0, 30)
    expect(s.version).toBe(1) // tRender < t_apply
    s.update(2.019, 40)
    expect(s.version).toBe(1)
    s.update(2.021, 50)
    expect(s.version).toBe(2)
    for (let t = 2.03; t < 12; t += 0.033) s.update(t, 60)
    s.update(12.0, 70)
    const fwd = Array.from(s.scalars)
    const sFwd = s.anchors.sM
    // a fresh client joining late (anchors from the change frame) lands on the same values
    const s2 = new EnvStore()
    s2.ingest(v2, 0)
    s2.update(12.0, 10)
    expect(Array.from(s2.scalars)).toEqual(fwd)
    expect(Math.abs(s2.anchors.sM - sFwd)).toBeLessThan(1e-9)
  })

  it('C04 STALE after 3 s wall, C05 any frame recovers', () => {
    const s = new EnvStore()
    s.ingest(wire(), 0)
    s.update(0.1, 100)
    s.update(0.2, 2900)
    expect(s.state).toBe('SYNCED')
    s.update(0.3, 3101)
    expect(s.state).toBe('STALE')
    s.ingest(wire(), 3200)
    expect(s.state).toBe('SYNCED')
  })

  it('C06 epoch change waits and keeps the last values, C07 snaps on the next frame', () => {
    const s = new EnvStore()
    s.onEpoch(1)
    s.ingest(wire({ preset: 'fog' }), 0)
    s.update(1, 10)
    const before = s.scalars[F.MOR_BG]
    s.onEpoch(2)
    expect(s.state).toBe('EPOCH_WAIT')
    s.update(1.1, 20)
    expect(s.scalars[F.MOR_BG]).toBe(before)
    s.ingest(wire({ preset: 'rain', version: 9, t_apply_ns: 0.5 * SEC }), 30)
    s.update(1.2, 40)
    expect(s.state).toBe('SYNCED')
    expect(s.version).toBe(9)
    expect(s.scalars[F.RAIN]).toBe(6)
    expect(s.damper.active).toBe(false)
  })

  it('C08 presets hash mismatch asks for the server copy', () => {
    const s = new EnvStore()
    const w = wire()
    w.config.presets_sha256 = 'f'.repeat(64)
    s.ingest(w, 0)
    expect(s.presetsWanted).toBe('f'.repeat(64))
    expect(s.presetsMismatch).toBe(true)
  })

  it('step frames: physics switches at once, visuals fade over --duration-slow', () => {
    const s = new EnvStore()
    s.ingest(wire({ preset: 'clear' }), 0)
    s.update(0.1, 0)
    s.ingest(wire({ preset: 'fog', version: 2, t_apply_ns: 0.2 * SEC }), 0)
    s.update(0.21, 1000)
    expect(s.scalars[F.MOR_BG]).toBe(150)
    expect(s.damper.vis[F.MOR_BG]).toBeGreaterThan(150)
    s.update(0.3, 1200)
    expect(s.damper.vis[F.MOR_BG]).toBeGreaterThan(150)
    s.update(0.5, 1401)
    expect(s.damper.vis[F.MOR_BG]).toBe(150)
  })

  it('heartbeat below 1 m is spread over --duration-very-slow, above is snapped', () => {
    const s = new EnvStore()
    s.ingest(wire({ preset: 'rain' }), 0)
    s.update(1.0, 0)
    const hb = wire({ preset: 'rain' })
    hb.t_ns = 1.0 * SEC
    const A = newAnchors()
    advance(A, decodeKeyframe(hb), 0, 50)
    hb.anchors = { t_ns: 1.0 * SEC, s_m: A.sM + 0.5, d_enu_m: [A.d[0], A.d[1], A.d[2]], fall_rain_m: A.fallRain, fall_snow_m: A.fallSnow, wetness: A.wetness, puddle: A.puddle }
    s.ingest(hb, 10)
    expect(Math.abs(s.anchorDriftM - 0.5)).toBeLessThan(1e-9)
    s.update(1.0, 10)
    expect(Math.abs(s.anchors.sM - (A.sM + 0.5))).toBeLessThan(1e-9) // truth moved
    expect(Math.abs(s.anchorsVis.sM - A.sM)).toBeLessThan(1e-3) // the picture did not jump
    s.update(1.0, 600)
    expect(Math.abs(s.anchorsVis.sM - s.anchors.sM)).toBeLessThan(1e-12)
  })

  it('update does not allocate (steady state, 20x replay steps)', () => {
    const s = new EnvStore()
    s.ingest(wire({ preset: 'thunderstorm' }), 0)
    s.update(0.0, 0)
    for (let i = 1; i < 200; i++) s.update(i * 0.033 * 20, i * 33)
    const g = globalThis as { gc?: () => void }
    const before = process.memoryUsage().heapUsed
    for (let i = 200; i < 2200; i++) s.update(i * 0.033 * 20, i * 33)
    g.gc?.()
    const grew = process.memoryUsage().heapUsed - before
    expect(grew).toBeLessThan(4 * 1024 * 1024)
    expect(s.anchors.tNs).toBeGreaterThan(1000 * SEC)
    expect(H_NS).toBe(20_000_000)
  })
})
