// AnetNodesHandler (ADR-007; M06 §6.3; g01-gap §6.2, verified code). Owner: M06.
// WebGLNodesHandler subclass for the classic WebGLRenderer (Tier B/S):
//   fix 1: when a non-XR render target is bound, output linear colour without tone mapping (the stock handler encodes
//          sRGB into render targets too, so multi-pass chains double-encode);
//   fix 2: honour scene.fogNode like WebGPURenderer's NodeManager.getFogNode() (the stock handler only reads scene.fog);
//   fix 3: expose renderer.depth (the default framebuffer's depth attribute): NodeMaterial.setup() only emits
//          depthNode / gl_FragDepth on the default framebuffer when renderer.depth === true, a WebGPURenderer field that
//          WebGLRenderer lacks, so the P2 composite's depth write-back silently vanished on screen (it worked into RTs).
//   fix 4: a material flagged userData.awrOutputInVertex (the Tier S point material, ADR-064) already carries the output
//          colour transform in its vertex stage: its output passes through unchanged on every target.
// It relies on three internals (getOutputCallback instance property, renderStack[].sceneContext, the proxy forwarding
// getRenderTarget); the feat-matrix regression page guards them on three upgrades (g01 §9).
import { WebGLNodesHandler } from 'three/addons/tsl/WebGLNodesHandler.js'
import { workingToColorSpace } from 'three/tsl'

type AnyNode = any // TSL output node (loosely typed in @types/three)
interface HandlerInternals {
  renderer: { getRenderTarget(): { isXRRenderTarget?: boolean } | null; toneMapping: number; outputColorSpace: string }
  renderStack: { sceneContext: { fogNode: unknown } }[]
  getOutputCallback: (out: AnyNode, builder: { material?: { toneMapped?: boolean; userData?: { awrOutputInVertex?: boolean } } }) => AnyNode
}

export class AnetNodesHandler extends WebGLNodesHandler {
  constructor() {
    super()
    const self = this as unknown as HandlerInternals
    self.getOutputCallback = (out, builder) => {
      if (builder?.material?.userData?.awrOutputInVertex === true) return out
      const r = self.renderer
      const rt = r.getRenderTarget()
      if (rt !== null && rt.isXRRenderTarget !== true) return out
      let o = out
      if (builder?.material?.toneMapped !== false) o = o.toneMapping(r.toneMapping)
      return workingToColorSpace(o, r.outputColorSpace as never)
    }
  }

  setRenderer(renderer: Parameters<WebGLNodesHandler['setRenderer']>[0]): void {
    super.setRenderer(renderer)
    const r = renderer as unknown as { depth?: boolean; getContext(): WebGL2RenderingContext }
    if (r.depth === undefined) r.depth = r.getContext().getContextAttributes()?.depth !== false
  }

  renderStart(scene: Parameters<WebGLNodesHandler['renderStart']>[0], camera: Parameters<WebGLNodesHandler['renderStart']>[1],
    targetScene: Parameters<WebGLNodesHandler['renderStart']>[2] = scene): void {
    super.renderStart(scene, camera, targetScene)
    const self = this as unknown as HandlerInternals
    const ctx = self.renderStack[self.renderStack.length - 1]?.sceneContext
    const fogNode = (targetScene as unknown as { fogNode?: unknown }).fogNode
    if (ctx && fogNode) ctx.fogNode = fogNode
  }
}
