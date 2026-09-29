// M06-AC-028 in Node (ADR-032; AWR-15 §3.7; M06-FR-037): five scenarios (no alarm, selected, one critical, several
// critical, critical + selected) through the M15 arbiter (lib/redArbiter.ts) into the drone layer. At most one vehicle
// is a solid red entity (red marker style, r500 selection ring, red trail colour), it is the arbiter's owner, the
// other criticals keep r500 outlines only (octagon rings, allowed by AWR-15 §3.7.1 rule 3), and the focus vehicle that
// yields the red gets the g50 double ring. The owner switch is a uniform/data change: the marker, glyph and trail
// materials stay the same objects (no recompiles; the pixel variant with gpu.programs runs in the browser smoke).
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import { ctx as frameCtx, DroneLayer, InterpRing, MARK, MarkerStyle, Palette, Shape, type RenderBackendView } from '@/engine'
import { arbitrate, RED_NONE, type RedCandidate } from '@/lib/redArbiter'
import { GLPointsNodeMaterial } from '@/viewport/glPointsNodeMaterial'

const be = { tier: 'S', deviceClass: 'software', kind: 'webgl2', pointSizeMode: 'glpoint', startRung: 0, lowestAllowedRung: 0, caps: {},
  createPointsMaterial: () => new GLPointsNodeMaterial(), programsCount: () => 0 } as unknown as RenderBackendView

function setup() {
  const interp = new InterpRing()
  const ids = new Map<number, { id: string; model: string; kind: string }>()
  for (let a = 0; a < 6; a++) {
    ids.set(a, { id: `uav${a}`, model: 'p600', kind: 'uav' })
    const s = interp.slotFor(a)
    interp.push(s, 0, a * 20, 0, 30, 0, 0, 0, 0, 0, 0, 1, 0)
    interp.push(s, 100, a * 20, 0, 30, 0, 0, 0, 0, 0, 0, 1, 0)
  }
  interp.eMaxMs = 1e9
  const roster = { get size() { return ids.size }, get: (a: number) => ids.get(a), idOf: (a: number) => ids.get(a)?.id }
  const layer = new DroneLayer({ be, interp, roster: () => roster, subscribe: () => () => {} })
  const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  cam.position.set(50, -400, 300)
  cam.lookAt(50, 0, 30)
  cam.updateMatrixWorld()
  return { layer, cam }
}

const glyphRows = (layer: DroneLayer): { shape: number; palette: number; x: number }[] => {
  const d = (layer.glyphs as unknown as { data: Float32Array }).data
  return Array.from({ length: layer.glyphs.n }, (_, i) => ({ x: d[8 * i], shape: d[8 * i + 4], palette: d[8 * i + 6] }))
}

describe('one red entity in the viewport (M06-AC-028)', () => {
  const scenarios: { name: string; critical: number[]; selected: number | null; owner: number }[] = [
    { name: 'no alarm', critical: [], selected: null, owner: -1 },
    { name: 'selected', critical: [], selected: 2, owner: 2 },
    { name: 'one critical', critical: [4], selected: null, owner: 4 },
    { name: 'several critical', critical: [1, 3, 5], selected: null, owner: 5 },
    { name: 'critical + selected', critical: [3], selected: 2, owner: 3 },
  ]
  for (const sc of scenarios) {
    it(sc.name, () => {
      const { layer, cam } = setup()
      const mats = [layer.markers.mesh.material, layer.glyphs.mesh.material]
      // candidates as the viewport builds them (evaluateRed): criticals ranked equal, newest last; the focus vehicle
      const cands: RedCandidate[] = sc.critical.map((a, k) => ({ entity: { kind: 'drone', id: `uav${a}` }, level: 'critical', rank: 1, tLastWallMs: 1000 + k }))
      if (sc.selected !== null) cands.push({ entity: { kind: 'drone', id: `uav${sc.selected}` }, level: 'selected', rank: 0, tLastWallMs: 1000 })
      const st = arbitrate(RED_NONE, cands, 5000)
      const owner = st.owner ? Number(st.owner.id.slice(3)) : -1
      expect(owner).toBe(sc.owner)
      for (const a of sc.critical) layer.setAlertLevel(a, 2)
      layer.setHighlights(sc.selected !== null ? [sc.selected] : [], sc.selected ?? -1, -1)
      layer.setRedOwner(owner)
      layer.update({ ...frameCtx, nowMs: 50, tRenderS: 0.05, tFocusS: 0.05, camera: cam, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5 })
      layer.glyphs.commit(0.5, 640, 360)
      // solid red: RED mark bit and red marker style on at most one vehicle, the owner
      const reds: number[] = []
      for (let i = 0; i < layer.poses.n; i++) {
        const a = layer.poses.agentNo[i]
        if ((layer.mark[a] & MARK.RED) !== 0) reds.push(a)
      }
      expect(reds).toEqual(owner >= 0 ? [owner] : [])
      const redStyles = []
      for (let i = 0; i < layer.markers.n; i++) if (layer.markers.data[4 * i + 3] === MarkerStyle.Red) redStyles.push(i)
      expect(redStyles.length).toBeLessThanOrEqual(1)
      // glyphs: red class only for the owner; no filled red shape anywhere; other criticals are r500 octagon outlines
      const rows = glyphRows(layer)
      const redClass = rows.filter((r) => r.palette === Palette.R500 && r.shape !== Shape.Octagon && r.shape !== Shape.Triangle)
      for (const r of redClass) expect(r.x).toBe(owner * 20)
      expect(rows.some((r) => r.palette === Palette.R500 && r.shape === Shape.Disc)).toBe(false)
      for (const a of sc.critical) {
        if (a === owner) continue
        expect(rows.some((r) => r.x === a * 20 && r.shape === Shape.Octagon && r.palette === Palette.R500)).toBe(true)
      }
      // the focus vehicle that yields the red: g50 double ring
      if (sc.selected !== null && sc.selected !== owner) {
        const own = rows.filter((r) => r.x === sc.selected! * 20 && r.shape === Shape.Ring && r.palette === Palette.G50)
        expect(own.length).toBe(2)
      }
      // owner switch: data only, same materials
      layer.setRedOwner(owner >= 0 ? (owner + 1) % 6 : 0)
      layer.update({ ...frameCtx, nowMs: 100, tRenderS: 0.1, tFocusS: 0.1, camera: cam, cssW: 1280, cssH: 720, dbW: 640, dbH: 360, dpr: 0.5 })
      expect([layer.markers.mesh.material, layer.glyphs.mesh.material]).toEqual(mats)
      layer.dispose()
    })
  }
})
