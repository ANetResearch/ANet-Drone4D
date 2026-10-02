// Restricted-zone overlay, ZonesLayer (M06-FR-047, AC-036; AWR-16 §7 awr.zones.v1; AWR-15 §10.12). Owner: M06.
// Built once per world from semantic/zones.geojson (ENU metres): side walls (one quad per outer-ring edge) g300 8 %,
// top outline 1.5 px (nofly solid, restricted dashed 2w 4w: two Line2 batches), vertical edges 1 px g300 50 % (thin
// batch). min_z_m = null -> the lowest DTM value (ground.zM - relief), max_z_m = null -> world top + 50 m; `border` is
// not drawn by default. A zone that wins the red (fence violation, RedArbiter owner {kind: 'zone'}) turns r500 outline
// and r500 10 % walls through the float uniform uHeroZone compared with the per-vertex zone index: no rebuild, no
// recompile. Tier B/A walls add a 45 degree world-space hatch (4 m spacing) through a uniform (same program). The wall
// colour (constant per zone) gets the output transform in the vertex stage (ADR-064): the large translucent walls carry
// no transfer function per fragment.
import { BufferAttribute, BufferGeometry, DoubleSide, Group, Mesh, Sphere, Vector3 } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, abs, attribute, float, fract, mix, positionLocal, select, uniform, varying, vec4 } from 'three/tsl'
import { SCENE } from '@/lib/tokens/scene.gen'
import { outputTransform } from '../shading'
import { Palette } from '../drones/glyph/GlyphLayer'
import { ThinLineBatch, WideLineBatch } from './lineBatch'

type N = any // TSL nodes

export interface ZoneFeature {
  id: string
  /** nofly, restricted, border or another kind of awr.zones.v1 */
  kind: string
  ring: number[][]
  minZ: number | null
  maxZ: number | null
  label: string
  labelZh?: string
}
export interface ZoneBuilt { id: string; kind: string; idx: number; ring: Float64Array; z0: number; z1: number; label: string; labelZh?: string; seg0: number; segN: number }

export const ZONES = { wallA: 0.08, heroWallA: 0.1, edgeA: 0.5, topPx: 1.5, hatchM: 4, maxEdges: 4096 } as const

/** parse awr.zones.v1 features (Polygon outer rings; holes ignored for the overlay) */
export function parseZones(geo: unknown): ZoneFeature[] {
  const out: ZoneFeature[] = []
  const fc = geo as { features?: { id?: string; geometry?: { type: string; coordinates: number[][][] }; properties?: Record<string, unknown> }[] }
  for (const f of fc.features ?? []) {
    if (!f.geometry || f.geometry.type !== 'Polygon') continue
    const p = f.properties ?? {}
    const str = (v: unknown): string | null => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : null)
    const id = str(p.zone_id) ?? str(f.id) ?? String(out.length)
    out.push({
      id, kind: str(p.kind) ?? 'restricted', ring: f.geometry.coordinates[0] ?? [],
      minZ: typeof p.min_z_m === 'number' ? p.min_z_m : null, maxZ: typeof p.max_z_m === 'number' ? p.max_z_m : null,
      label: str(p.label) ?? id, labelZh: str(p.label_zh) ?? undefined,
    })
  }
  return out
}

export class ZonesLayer {
  readonly root = new Group()
  readonly walls: Mesh
  readonly topSolid: WideLineBatch
  readonly topDashed: WideLineBatch
  readonly edges: ThinLineBatch
  readonly zones: ZoneBuilt[] = []
  private readonly uHero: N
  private readonly uHatch: N
  private readonly uWallA: N
  showBorder = false

  constructor(readonly tier: 'A' | 'B' | 'S') {
    const g = new BufferGeometry()
    g.boundingSphere = new Sphere(new Vector3(), 1e7)
    const m = new MeshBasicNodeMaterial()
    this.uHero = uniform(-1)
    this.uHatch = uniform(tier === 'S' ? 0 : 1)
    this.uWallA = uniform(ZONES.wallA)
    const zi: N = varying(attribute('zoneIdx', 'float'))
    const g300 = SCENE.glyphPalette[4]
    const r500 = SCENE.glyphPalette[1]
    const cGrey: N = uniform(new Vector3(g300[0], g300[1], g300[2]))
    const cRed: N = uniform(new Vector3(r500[0], r500[1], r500[2]))
    const hero: N = abs(zi.sub(this.uHero)).lessThan(0.5)
    m.colorNode = vec4(varying(outputTransform(select(hero, cRed, cGrey)), 'vZoneColor') as N, 1) as N
    m.userData.awrOutputInVertex = true
    m.opacityNode = Fn(() => {
      const a: N = select(hero, float(ZONES.heroWallA), this.uWallA)
      // Tier S has no hatch (uHatch 0): its walls skip the per-fragment stripe term altogether (FX2-R2)
      if (tier === 'S') return a
      const p: N = positionLocal
      const stripe: N = abs(fract(p.x.add(p.y).add(p.z).div(ZONES.hatchM * Math.SQRT2)).sub(0.5)).lessThan(0.08)
      const hatch: N = mix(float(1), select(stripe, float(2.5), float(0.6)), this.uHatch)
      return a.mul(hatch)
    })() as N
    m.transparent = true
    m.depthWrite = false
    m.side = DoubleSide
    // one draw: three renders transparent DoubleSide materials in two passes (back, then front) unless forced to a single
    // pass, which made render.calls exceed the pass plan by one whenever zones are shown (M06-E007; INT-1)
    m.forceSinglePass = true
    m.fog = false
    this.walls = new Mesh(g, m)
    this.walls.frustumCulled = false
    this.walls.renderOrder = 20
    this.walls.name = 'ZoneWalls'
    this.walls.visible = false
    this.topSolid = new WideLineBatch(ZONES.maxEdges, ZONES.topPx, false, 'ZoneTopSolid', 21)
    this.topDashed = new WideLineBatch(ZONES.maxEdges, ZONES.topPx, true, 'ZoneTopDashed', 21)
    this.edges = new ThinLineBatch(ZONES.maxEdges, 'ZoneEdges', 21)
    this.root.add(this.walls, this.topSolid.obj, this.topDashed.obj, this.edges.obj)
    this.root.name = 'ZonesLayer'
  }

  /** build all geometry once per world; groundMin = lowest DTM z, worldTop = bounds max z */
  build(features: readonly ZoneFeature[], groundMin: number, worldTop: number): void {
    this.zones.length = 0
    const pos: number[] = []
    const idx: number[] = []
    this.topSolid.begin()
    this.topDashed.begin()
    this.edges.begin()
    for (const f of features) {
      if (f.kind === 'border' && !this.showBorder) continue
      const ring = f.ring.length > 1 && f.ring[0][0] === f.ring[f.ring.length - 1][0] && f.ring[0][1] === f.ring[f.ring.length - 1][1] ? f.ring.slice(0, -1) : f.ring
      if (ring.length < 3) continue
      const z0 = f.minZ ?? groundMin
      const z1 = f.maxZ ?? worldTop + 50
      const i = this.zones.length
      const flat = new Float64Array(ring.length * 2)
      ring.forEach((p, k) => {
        flat[2 * k] = p[0]
        flat[2 * k + 1] = p[1]
      })
      const top0 = f.kind === 'nofly' ? this.topSolid : this.topDashed
      this.zones.push({ id: f.id, kind: f.kind, idx: i, ring: flat, z0, z1, label: f.label, labelZh: f.labelZh, seg0: top0.n, segN: ring.length })
      let dist = 0
      for (let k = 0; k < ring.length; k++) {
        const a = ring[k]
        const b = ring[(k + 1) % ring.length]
        // wall quad a0 b0 b1 a1
        pos.push(a[0], a[1], z0, b[0], b[1], z0, b[0], b[1], z1, a[0], a[1], z0, b[0], b[1], z1, a[0], a[1], z1)
        for (let j = 0; j < 6; j++) idx.push(i)
        const top = f.kind === 'nofly' ? this.topSolid : this.topDashed
        top.seg(a[0], a[1], z1, b[0], b[1], z1, Palette.G300, dist)
        dist += Math.hypot(b[0] - a[0], b[1] - a[1])
        this.edges.seg(a[0], a[1], z0, a[0], a[1], z1, Palette.G300, false, ZONES.edgeA)
      }
    }
    const g = this.walls.geometry
    g.setAttribute('position', new BufferAttribute(new Float32Array(pos), 3))
    g.setAttribute('zoneIdx', new BufferAttribute(new Float32Array(idx), 1))
    g.setDrawRange(0, pos.length / 3)
    this.walls.visible = pos.length > 0
    this.topSolid.commit()
    this.topDashed.commit()
    this.edges.commit(0)
  }

  /** the violated zone wins the red (-1 none): walls and outline r500 */
  setHero(zoneId: string | null): void {
    const z = zoneId === null ? undefined : this.zones.find((x) => x.id === zoneId)
    const prev = this.zones.find((x) => x.idx === this.uHero.value)
    if (prev === z) return
    if (prev) (prev.kind === 'nofly' ? this.topSolid : this.topDashed).recolor(prev.seg0, prev.segN, Palette.G300)
    if (z) (z.kind === 'nofly' ? this.topSolid : this.topDashed).recolor(z.seg0, z.segN, Palette.R500)
    this.uHero.value = z ? z.idx : -1
  }

  /** zone under a horizontal point (ENU), preferring the smallest; null when none (hover label, M06 §6.13) */
  zoneAt(x: number, y: number, z: number): ZoneBuilt | null {
    let best: ZoneBuilt | null = null
    for (const zn of this.zones) {
      if (z < zn.z0 || z > zn.z1 || !pointInRing(zn.ring, x, y)) continue
      best = zn
    }
    return best
  }

  setWidth(dpr: number): void {
    this.topSolid.setWidth(dpr)
    this.topDashed.setWidth(dpr)
  }

  /** once per frame: raster/CSS ratio, drawing buffer and camera near plane of the screen-space top outlines */
  setView(dpr: number, dbW: number, dbH: number, near: number): void {
    this.setWidth(dpr)
    this.topSolid.setView(dbW, dbH, near)
    this.topDashed.setView(dbW, dbH, near)
  }

  drawCount(): number {
    return (this.walls.visible ? 1 : 0) + this.topSolid.drawCount() + this.topDashed.drawCount() + this.edges.drawCount()
  }

  dispose(): void {
    this.walls.geometry.dispose()
    ;(this.walls.material as MeshBasicNodeMaterial).dispose()
    this.topSolid.dispose()
    this.topDashed.dispose()
    this.edges.dispose()
  }
}

export function pointInRing(ring: Float64Array, x: number, y: number): boolean {
  let inside = false
  const n = ring.length / 2
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = ring[2 * i], yi = ring[2 * i + 1], xj = ring[2 * j], yj = ring[2 * j + 1]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}
