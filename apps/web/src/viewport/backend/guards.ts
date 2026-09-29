// Coding-rule assertions of M06 §6.3 (AnetNodesHandler constraints; M06-FR-007, AC-005). Owner: M06.
// Run at layer registration in dev and test builds (and directly in unit tests):
//   rule 1  every InstancedMesh owns its material instance            -> M06-E008
//   rule 11 instanced objects (InstancedMesh, InstancedBufferGeometry) do not share geometry -> M06-E008
//   rule 5  no int/uint uniforms (UBO members are written as Float32) -> M06-E009
//   rule 8  no texture.internalFormat on any backend                  -> M06-E010 family (texture wrapper assertion)
// Rules 2, 3, 6 and 9 (material.onBeforeRender, info.render.frame, onObjectUpdate on hot paths, RenderPipeline/pass/
// MRT/storage/compute on the shared path) are enforced by the M06 lint (apps/web/tests/m06/lint, make lint-m06).
import type { Material, Object3D, Texture } from 'three'
import { TEST_SWITCHES } from '@/lib/testSwitches'

export class M06Error extends Error {
  constructor(readonly code: string, message: string) {
    super(`${code} ${message}`)
    this.name = 'M06Error'
  }
}

export const GUARDS_ON: boolean = TEST_SWITCHES

type AnyNode = { isNode?: boolean; isUniformNode?: boolean; isTextureNode?: boolean; nodeType?: string | null; value?: unknown; getChildren?: () => Iterable<AnyNode> }
const NODE_SLOTS = ['colorNode', 'positionNode', 'opacityNode', 'normalNode', 'vertexNode', 'fragmentNode', 'outputNode', 'depthNode', 'sizeNode',
  'alphaTestNode', 'emissiveNode', 'maskNode', 'castShadowNode', 'receivedShadowNode', 'backdropNode', 'geometryNode', 'mrtNode', 'lightsNode', 'envNode']

function walkNodes(root: AnyNode | null | undefined, visit: (n: AnyNode) => void, seen: Set<AnyNode>): void {
  if (!root || typeof root !== 'object' || seen.has(root)) return
  seen.add(root)
  visit(root)
  if (typeof root.getChildren === 'function') {
    try {
      for (const c of root.getChildren()) walkNodes(c, visit, seen)
    } catch {
      /* some nodes compute children lazily from a builder; skip them */
    }
  }
}

/** rule 5: int and uint uniforms break under WebGLNodesHandler (g01 §0 item 5) */
export function assertFloatUniforms(m: Material, where = ''): void {
  const seen = new Set<AnyNode>()
  const bad: string[] = []
  for (const slot of NODE_SLOTS) {
    walkNodes((m as unknown as Record<string, AnyNode>)[slot], (n) => {
      if (n.isUniformNode && typeof n.nodeType === 'string' && /^(?:int|uint|[iu]vec[234])$/.test(n.nodeType)) bad.push(n.nodeType)
      if (n.isTextureNode) assertNoInternalFormat(n.value as Texture | null, where)
    }, seen)
  }
  if (bad.length) throw new M06Error('M06-E009', `int/uint uniform in ${where || m.type}: ${bad.join(', ')}; use a float uniform and int() in the shader`)
}

/** rule 8: never set texture.internalFormat (WebGPU reads integer textures as 0, g01 §0 item 10) */
export function assertNoInternalFormat(t: Texture | null | undefined, where = ''): void {
  if (t && (t as unknown as { internalFormat: unknown }).internalFormat != null) {
    throw new M06Error('M06-E010', `texture.internalFormat set on ${t.name || where || 'a texture'}; let three derive the format`)
  }
}

/** rules 1 and 11 over a subtree (and against already registered objects through `owners`) */
export function assertInstancing(root: Object3D, owners: Map<unknown, Object3D> = new Map()): void {
  root.traverse((o) => {
    const obj = o as Object3D & { isInstancedMesh?: boolean; isMesh?: boolean; material?: Material | Material[]; geometry?: { isInstancedBufferGeometry?: boolean } }
    const instanced = obj.isInstancedMesh === true || obj.geometry?.isInstancedBufferGeometry === true
    if (!instanced) return
    const mats = Array.isArray(obj.material) ? obj.material : obj.material ? [obj.material] : []
    for (const m of mats) {
      const prev = owners.get(m)
      if (prev && prev !== obj) throw new M06Error('M06-E008', `instanced object ${obj.name || obj.type} shares its material with ${prev.name || prev.type}`)
      owners.set(m, obj)
    }
    const g = obj.geometry as unknown
    const pg = owners.get(g)
    if (pg && pg !== obj) throw new M06Error('M06-E008', `instanced object ${obj.name || obj.type} shares its geometry with ${pg.name || pg.type}`)
    if (g) owners.set(g, obj)
  })
}

/** all guards over a layer root; materials are checked once */
export function checkLayerRoot(root: Object3D | null, owners: Map<unknown, Object3D>, where: string): void {
  if (!root) return
  assertInstancing(root, owners)
  root.traverse((o) => {
    const m = (o as { material?: Material | Material[] }).material
    const mats = Array.isArray(m) ? m : m ? [m] : []
    for (const x of mats) assertFloatUniforms(x, `${where}/${o.name || o.type}`)
  })
}

/**
 * M06-FR-021: no R3F pointer events on the point cloud, the terrain or any WorldRoot ancestor (picking is the engine
 * Picker; R3F would raycast the whole world on every pointer move). `interaction` is R3F internal.interaction.
 */
export function assertNoPointerEventsOnWorld(interaction: readonly Object3D[], worldRoot: Object3D, guarded: readonly (Object3D | null)[]): void {
  const forbidden = new Set<Object3D>()
  for (let a: Object3D | null = worldRoot; a; a = a.parent) forbidden.add(a)
  for (const g of guarded) for (let a: Object3D | null = g; a; a = a.parent) forbidden.add(a)
  for (const o of interaction) {
    if (forbidden.has(o)) throw new M06Error('M06-E008', `M06-FR-021 R3F pointer events on ${o.name || o.type} (WorldRoot ancestor or point cloud)`)
  }
}
