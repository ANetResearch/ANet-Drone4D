// Vehicle models (M06-FR-035; AWR-16 §11.5; AWR-17 §5.1; ADR-022). Owner: M06.
// Low-poly geometry is procedural (same attribute layout on every model and tier). The hero model is
// GET /vehicles/<model>/model/p600.glb (no ?v= in D1; no-cache + ETag), baked once into FLU with the inverse of
// model.yaml gltf_from_flu (rows [0,0,1],[1,0,0],[0,1,0]), normalised to {position, normal}, <= 5000 triangles. It is
// requested only when a model source is configured (setHeroSource; the api does not serve /vehicles yet, see the M06
// request to M11): without it every hero falls back to the low-poly bucket and M06-E011 is logged once per model. Test
// builds can switch to the procedural hero placeholder (?hero=procedural) to exercise the hero and hull paths.
import { BufferAttribute, BufferGeometry, Matrix4, type Mesh } from 'three'
import { makeHeroPlaceholderGeometry, visRadius } from './lowpoly'

export const HERO_MAX_TRIS = 5000
/** FLU <- glTF (inverse of gltf_from_flu): x_flu = z_gltf, y_flu = x_gltf, z_flu = y_gltf */
export const FLU_FROM_GLTF = new Matrix4().set(0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1)

export type HeroState = 'none' | 'loading' | 'ready' | 'failed'
export interface HeroModel { state: HeroState; geometry: BufferGeometry | null; rVis: number }

const warned = new Set<string>()
// default hero source: the api serves vehicle model products at /vehicles/<model>/model/<file>.glb (AWR-17 §5;
// INT-1 wired the route); setHeroSource(model, '') disables, FakeSource pages without a backend fall back to low-poly
const sources = new Map<string, string>([['p600', '/vehicles/p600/model/p600.glb']])
const heroes = new Map<string, HeroModel>()
let procedural = false
const listeners = new Set<(model: string) => void>()

/** configure where a model's hero glb is served (url); '' disables */
export function setHeroSource(model: string, url: string): void {
  if (url) sources.set(model, url)
  else sources.delete(model)
}
/** test builds: procedural hero placeholder for every model */
export function setProceduralHero(on: boolean): void {
  procedural = on
  heroes.clear()
}
export function onHeroReady(cb: (model: string) => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** bake a glTF scene's meshes into one FLU geometry {position, normal} */
export function bakeFlu(meshes: readonly Mesh[]): BufferGeometry {
  const pos: number[] = []
  const nor: number[] = []
  const m = new Matrix4()
  for (const mesh of meshes) {
    mesh.updateWorldMatrix(true, false)
    m.multiplyMatrices(FLU_FROM_GLTF, mesh.matrixWorld)
    const g = (mesh.geometry.index ? mesh.geometry.toNonIndexed() : mesh.geometry.clone()).applyMatrix4(m)
    if (!g.getAttribute('normal')) g.computeVertexNormals()
    pos.push(...(g.getAttribute('position').array as Float32Array))
    nor.push(...(g.getAttribute('normal').array as Float32Array))
    g.dispose()
  }
  const out = new BufferGeometry()
  out.setAttribute('position', new BufferAttribute(new Float32Array(pos), 3))
  out.setAttribute('normal', new BufferAttribute(new Float32Array(nor), 3))
  out.computeBoundingSphere()
  return out
}

async function loadGlb(url: string): Promise<BufferGeometry> {
  const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js')
  const gltf = await new GLTFLoader().loadAsync(url)
  const meshes: Mesh[] = []
  gltf.scene.traverse((o) => {
    if ((o as Mesh).isMesh) meshes.push(o as Mesh)
  })
  const g = bakeFlu(meshes)
  if (g.getAttribute('position').count / 3 > HERO_MAX_TRIS) console.warn(`hero model ${url} exceeds ${HERO_MAX_TRIS} triangles`)
  return g
}

/** hero model of a vehicle model id; starts the load on first use */
export function heroOf(model: string): HeroModel {
  let h = heroes.get(model)
  if (h) return h
  if (procedural) {
    const g = makeHeroPlaceholderGeometry()
    h = { state: 'ready', geometry: g, rVis: 0.6 }
  } else {
    const url = sources.get(model)
    h = { state: url ? 'loading' : 'failed', geometry: null, rVis: 0.6 }
    if (!url && !warned.has(model)) {
      warned.add(model)
      console.warn(`M06-E011 hero model of ${model} is not available; heroes fall back to the low-poly bucket`)
    }
    if (url) {
      const hm = h
      loadGlb(url).then((g) => {
        hm.geometry = g
        hm.rVis = visRadius(g)
        hm.state = 'ready'
        for (const l of listeners) l(model)
      }).catch((e: unknown) => {
        hm.state = 'failed'
        console.warn(`M06-E011 hero model ${url} failed to load (${String((e as Error)?.message ?? e)}); low-poly fallback`)
      })
    }
  }
  heroes.set(model, h)
  return h
}
