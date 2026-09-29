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
export interface ExtraPass { scene: Scene; camera: Camera; target: RenderTarget | null }

interface Saved { o: Object3D; parent: Object3D | null; visible: boolean; frustumCulled: boolean }

export async function warmupZoo(r: WebGLRenderer, scene: Scene, camera: Camera, items: readonly WarmupItem[],
  targetOf: (t: WarmTarget) => RenderTarget | null | undefined, o: { parallelCompile: boolean; extra?: readonly ExtraPass[]; programs: () => number }): Promise<WarmupReport> {
  const t0 = performance.now()
  const saved: Saved[] = []
  for (const it of items) {
    const obj = it.object
    saved.push({ o: obj, parent: obj.parent, visible: obj.visible, frustumCulled: obj.frustumCulled })
    it.before?.()
    if (!obj.parent) scene.add(obj)
    obj.visible = true
    obj.frustumCulled = false
    // hidden ancestors (an invisible layer root) would hide the object: force them visible for the warm-up
    for (let a = obj.parent; a && a !== scene; a = a.parent) {
      if (a.visible) continue
      saved.push({ o: a, parent: a.parent, visible: false, frustumCulled: a.frustumCulled })
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
      // only the objects that use this target
      for (let i = 0; i < items.length; i++) items[i].object.visible = (items[i].targets ?? ['screen']).includes(t)
      r.setRenderTarget(target as WebGLRenderTarget | null)
      r.autoClear = true
      r.render(scene, camera)
    }
    for (const x of o.extra ?? []) {
      r.setRenderTarget(x.target as WebGLRenderTarget | null)
      r.render(x.scene, x.camera)
    }
  } finally {
    r.setRenderTarget(prevTarget)
    r.autoClear = autoClear
    camera.layers.mask = mask
    for (const s of saved) {
      if (!s.parent && s.o.parent === scene) scene.remove(s.o)
      s.o.visible = s.visible
      s.o.frustumCulled = s.frustumCulled
    }
    for (const it of items) it.after?.()
  }
  return { warmupMs: performance.now() - t0, programs: o.programs(), items: items.length, targets: wanted.size }
}
