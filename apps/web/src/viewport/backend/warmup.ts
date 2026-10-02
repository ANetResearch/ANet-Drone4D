// Shader zoo warm-up (ADR-007; M06 §6.4, FR-009, AC-007/008; D1-AC-25). Owner: M06.
// Collects warmupVariants(be) of every registered layer, makes each object drawable (before()), then renders the live
// scene (so scene.fogNode, lights and output settings match run time) once per target the objects use: the default
// framebuffer (hidden under the DOM mask) and each render target (cloud, pick, bench ...). With KHR_parallel_shader_compile
// the scene is first handed to compileAsync (drivers compile in parallel; on SwiftShader ANGLE still links at first
// draw, so the real renders are what removes the first-frame cost, g01 §4.3). Extra scenes (the P2 composite quad)
// are rendered to their own target. Afterwards every object gets its live state back (after()).
// Report: warmupMs (wall) and programs (renderer.info.programs.length) as the baseline of D1-AC-25.
import type { Camera, Object3D, RenderTarget, Scene, WebGLRenderTarget, WebGLRenderer } from 'three'
import type { WarmTarget, WarmupItem } from '../layers/registry'

export interface WarmupReport { warmupMs: number; programs: number; items: number; targets: number }
export interface ExtraPass { scene: Scene; camera: Camera; target: RenderTarget | null; before?(): void; after?(): void }

interface Saved { o: Object3D; parent: Object3D | null; visible: boolean; frustumCulled: boolean; mask: number }

/** true when a is a proper ancestor of b */
function isAncestor(a: Object3D, b: Object3D): boolean {
  for (let p = b.parent; p; p = p.parent) if (p === a) return true
  return false
}

export async function warmupZoo(r: WebGLRenderer, scene: Scene, camera: Camera, items: readonly WarmupItem[],
  targetOf: (t: WarmTarget) => RenderTarget | null | undefined, o: { parallelCompile: boolean; extra?: readonly ExtraPass[]; programs: () => number }): Promise<WarmupReport> {
  const t0 = performance.now()
  const saved: Saved[] = []
  const itemMask = items.map((it) => it.object.layers.mask)
  for (const it of items) {
    const obj = it.object
    saved.push({ o: obj, parent: obj.parent, visible: obj.visible, frustumCulled: obj.frustumCulled, mask: obj.layers.mask })
    it.before?.()
    if (!obj.parent) scene.add(obj)
    obj.visible = true
    obj.frustumCulled = false
    // hidden ancestors (an invisible layer root) would hide the object: force them visible for the warm-up
    for (let a = obj.parent; a && a !== scene; a = a.parent) {
      if (a.visible) continue
      saved.push({ o: a, parent: a.parent, visible: false, frustumCulled: a.frustumCulled, mask: a.layers.mask })
      a.visible = true
    }
  }
  const mask = camera.layers.mask
  camera.layers.enableAll()
  const prevTarget = r.getRenderTarget()
  const autoClear = r.autoClear
  const wanted = new Set<WarmTarget>()
  for (const it of items) for (const t of it.targets ?? ['screen']) wanted.add(t)
  try {
    if (o.parallelCompile && typeof (r as unknown as { compileAsync?: unknown }).compileAsync === 'function') {
      try {
        await r.compileAsync(scene, camera)
      } catch {
        /* compileAsync is an optimisation only */
      }
    }
    for (const t of wanted) {
      const target = t === 'screen' ? null : targetOf(t)
      if (target === undefined) continue
      // only the objects that use this target draw; an item that is an ancestor of one that does (the point-pick object
      // is a child of the point-cloud root) stays visible with an empty layer mask, so three still descends into it
      // without drawing it (otherwise the child's program would first compile at run time, FX-WEB1)
      for (let i = 0; i < items.length; i++) {
        const uses = (items[i].targets ?? ['screen']).includes(t)
        const holder = !uses && items.some((it, k) => k !== i && (it.targets ?? ['screen']).includes(t) && isAncestor(items[i].object, it.object))
        items[i].object.visible = uses || holder
        items[i].object.layers.mask = holder ? 0 : itemMask[i]
      }
      r.setRenderTarget(target as WebGLRenderTarget | null)
      r.autoClear = true
      r.render(scene, camera)
    }
    for (const x of o.extra ?? []) {
      x.before?.()
      r.setRenderTarget(x.target as WebGLRenderTarget | null)
      r.render(x.scene, x.camera)
      x.after?.()
    }
  } finally {
    r.setRenderTarget(prevTarget)
    r.autoClear = autoClear
    camera.layers.mask = mask
    for (const s of saved) {
      if (!s.parent && s.o.parent === scene) scene.remove(s.o)
      s.o.visible = s.visible
      s.o.frustumCulled = s.frustumCulled
      s.o.layers.mask = s.mask
    }
    for (const it of items) it.after?.()
  }
  return { warmupMs: performance.now() - t0, programs: o.programs(), items: items.length, targets: wanted.size }
}
