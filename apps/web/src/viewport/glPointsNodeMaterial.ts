// GLPointsNodeMaterial (ADR-011; g01 §6.3; M06 §6.3): PointsNodeMaterial for the classic path whose generated vertex
// shader no longer ends with the hard-coded `gl_PointSize = 1.0;`, so a TSL `builtin('gl_PointSize')` assignment takes
// effect. The program cache key changes too: the handler keys programs by node graph only, and two materials with the
// same graph would otherwise share the unpatched program. Public API only (onBeforeCompile, customProgramCacheKey).
import { PointsNodeMaterial } from 'three/webgpu'

export class GLPointsNodeMaterial extends PointsNodeMaterial {
  static get type(): string {
    return 'GLPointsNodeMaterial'
  }
  onBeforeCompile(p: { vertexShader: string }): void {
    p.vertexShader = p.vertexShader.replace(/\n\s*gl_PointSize = 1\.0;/, '\n')
  }
  customProgramCacheKey(): string {
    return super.customProgramCacheKey() + '|glps'
  }
}
