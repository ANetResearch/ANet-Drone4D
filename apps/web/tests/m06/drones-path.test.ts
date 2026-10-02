// M06-AC-024, AC-030, AC-031 in Node (M06 §6.9; ADR-046; M12 §7.1): 200 vehicles from the M12 interpolation ring reach
// the drone layer's marker texture at tRender exactly (<= 1e-4 m, frame by frame); HOLD vehicles turn stale (marker
// style, dashed glyph); vehicles missing from the roster disappear on the next frame and their trail rows and buckets
// are recycled; unknown entity kinds render as generic markers; the glyph cap truncates by priority class.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import {
  ctx as frameCtx, DroneLayer, GLYPH, GlyphClass, GlyphLayer, InterpRing, MARK, MarkerStyle, Palette, Shape, alertLevelOf, type RenderBackendView,
} from '@/engine'
import { FlightFlags, FlightState } from '@awr/contracts/enums'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

const be = { tier: 'S', deviceClass: 'software', kind: 'webgl2', pointSizeMode: 'glpoint', startRung: 0, lowestAllowedRung: 0, caps: {},
  createPointsMaterial: () => new GLPointsNodeMaterial(), programsCount: () => 0 } as unknown as RenderBackendView

function roster(ids: Map<number, { id: string; model: string; kind: string }>) {
  return { get size() { return ids.size }, get: (a: number) => ids.get(a), idOf: (a: number) => ids.get(a)?.id }
}

describe('drone data path (M06-AC-024, AC-030, AC-031)', () => {
  it('200 vehicles: rendered positions equal the M12 interpolation output at tRender', () => {
    const interp = new InterpRing()
    const ids = new Map<number, { id: string; model: string; kind: string }>()
    for (let a = 0; a < 200; a++) {
      ids.set(a, { id: `uav${a}`, model: 'p600', kind: 'uav' })
      const s = interp.slotFor(a)
      for (let k = 0; k < 10; k++) {
        const t = k * 100
        interp.push(s, t, a + 0.1 * t, -a, 30 + Math.sin(t / 300), 1, 0, 0, 0, 0, 0, 1, 0)
      }
    }
    interp.eMaxMs = 1e9
    const layer = new DroneLayer({ be, interp, roster: () => roster(ids), subscribe: () => () => {} })
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    cam.position.set(0, 500, 500)
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    const ref = { ...layer.poses }
    for (const tS of [0.25, 0.4, 0.61, 0.8]) {
      layer.update({ ...frameCtx, nowMs: tS * 1000, tRenderS: tS, tFocusS: tS, camera: cam, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5 })
      const probe = { n: 0, agentNo: new Uint16Array(256), pos: new Float32Array(768), quat: new Float32Array(1024), vel: new Float32Array(768),
        state: new Uint8Array(256), flags: new Uint8Array(256), battery: new Uint8Array(256), hold: new Uint8Array(256), clamped: new Uint8Array(256),
        sampleT: new Float64Array(256), ageS: new Float32Array(256) }
      interp.sampleSwarm(tS, probe)
      expect(layer.poses.n).toBe(200)
      const tex = layer.markers.data
      for (let i = 0; i < 200; i++) {
        for (let k = 0; k < 3; k++) expect(Math.abs(tex[4 * i + k] - probe.pos[3 * i + k])).toBeLessThan(1e-4)
      }
    }
    void ref
    expect(layer.markers.n).toBe(200)
    layer.dispose()
  })

  it('HOLD -> stale marker and dashed glyph; removal on the next frame recycles rows', () => {
    const interp = new InterpRing()
    const ids = new Map([[1, { id: 'a', model: 'p600', kind: 'uav' }], [2, { id: 'b', model: 'x500', kind: 'uav' }], [3, { id: 'c', model: 'car', kind: 'vehicle' }]])
    for (const a of [1, 2, 3]) {
      const s = interp.slotFor(a)
      interp.push(s, 0, a * 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
      interp.push(s, 100, a * 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
    }
    interp.eMaxMs = 300
    const layer = new DroneLayer({ be, interp, roster: () => roster(ids), subscribe: () => () => {} })
    const ctx = { ...frameCtx, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5, camera: null }
    layer.update({ ...ctx, nowMs: 0, tRenderS: 0.05 })
    expect(layer.markers.data[3]).toBe(MarkerStyle.Normal)
    layer.update({ ...ctx, nowMs: 2000, tRenderS: 2.0 }) // far beyond the extrapolation limit -> HOLD
    expect(layer.mark[1] & MARK.STALE).not.toBe(0)
    expect(layer.markers.data[3]).toBe(MarkerStyle.Stale)
    layer.glyphs.commit(0.5, 640, 360)
    const shapes = new Set<number>()
    const gd = (layer.glyphs as unknown as { data: Float32Array }).data
    for (let i = 0; i < layer.glyphs.n; i++) shapes.add(gd[8 * i + 4])
    expect(shapes.has(Shape.Dashed)).toBe(true)
    expect(layer.trails.rowFor(2)).toBeGreaterThanOrEqual(0)
    ids.delete(2)
    layer.update({ ...ctx, nowMs: 2033, tRenderS: 2.03 })
    expect(Array.from(layer.poses.agentNo.slice(0, layer.poses.n))).not.toContain(2)
    expect(layer.trails.rowFor(2)).toBe(-1)
    layer.dispose()
  })

  it('a frozen clock (PAUSED or STEPPING with a fresh TIME) never marks STALE; a running or stale clock does (FX-WEB2 item 1)', () => {
    const interp = new InterpRing()
    const ids = new Map([[1, { id: 'a', model: 'p600', kind: 'uav' }]])
    const s = interp.slotFor(1)
    interp.push(s, 0, 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
    interp.push(s, 100, 10, 0, 20, 0, 0, 0, 0, 0, 0, 1, 0)
    interp.eMaxMs = 300
    let frozen = true
    const layer = new DroneLayer({ be, interp, roster: () => roster(ids), subscribe: () => () => {}, frozen: () => frozen })
    const ctx = { ...frameCtx, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5, camera: null }
    // paused 276 s after the newest sample (live sim held paused after a replay): held pose, but not a signal delay
    layer.update({ ...ctx, nowMs: 0, tRenderS: 276.9 })
    expect(layer.mark[1] & MARK.STALE).toBe(0)
    expect(layer.markers.data[3]).toBe(MarkerStyle.Normal)
    frozen = false
    layer.update({ ...ctx, nowMs: 33, tRenderS: 276.9 })
    expect(layer.mark[1] & MARK.STALE).not.toBe(0)
    layer.dispose()
  })

  it('alert levels from flight state and flags', () => {
    expect(alertLevelOf(FlightState.FLYING, 0)).toBe(0)
    expect(alertLevelOf(FlightState.FLYING, FlightFlags.ALERT)).toBe(2)
    expect(alertLevelOf(FlightState.CRASHED, 0)).toBe(2)
    expect(alertLevelOf(FlightState.FAILSAFE, 0)).toBe(2)
    expect(alertLevelOf(FlightState.HOLD, 0)).toBe(1)
    expect(alertLevelOf(FlightState.LANDED, 0)).toBe(0)
  })

  it('glyph cap truncates by class: red > critical > warning > selection > mission > stale', () => {
    const g = new GlyphLayer(GLYPH.capS)
    g.begin()
    for (let i = 0; i < 100; i++) g.push(GlyphClass.Stale, i, 0, 0, 20, Shape.Dashed, 1.5, Palette.G500)
    for (let i = 0; i < 100; i++) g.push(GlyphClass.Mission, i, 0, 0, 10, Shape.Ring, 1.5, Palette.G50)
    for (let i = 0; i < 100; i++) g.push(GlyphClass.Warning, i, 0, 0, 20, Shape.Triangle, 1.5, Palette.R500)
    g.push(GlyphClass.Red, 0, 0, 0, 24, Shape.Octagon, 2, Palette.R500)
    expect(g.commit(0.5, 640, 360)).toBe(256)
    const d = (g as unknown as { data: Float32Array }).data
    expect(d[4]).toBe(Shape.Octagon) // the red entity first
    expect(d[8 + 4]).toBe(Shape.Triangle)
    expect(g.truncated).toBe(301 - 256)
    expect(g.drawCount()).toBe(1)
  })
})
