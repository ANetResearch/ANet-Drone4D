// GLPointsNodeMaterial (ADR-011; g01 §6.3; M06 §6.3): PointsNodeMaterial for the classic path whose generated vertex
// shader no longer ends with the hard-coded `gl_PointSize = 1.0;`, so a TSL `builtin('gl_PointSize')` assignment takes
// effect. The program cache key changes too: the handler keys programs by node graph only, and two materials with the
// same graph would otherwise share the unpatched program. Public API only (onBeforeCompile, customProgramCacheKey,
// contextNode with overrideNode).
// Attribute-less draws (the PointPool, precipitation and dust, the pick pass: points addressed by vertexIndex, no
// 'position' attribute, and rule 10 of M06 §6.3 forbids a fake one) take their position from positionNode, yet
// NodeMaterial.setupPosition still assigns it to positionLocal, a varying initialised from attribute('position'): three
// warned 'AttributeNode: Vertex attribute "position" not found on geometry.' once per program and emitted vec3( 0.0 ).
// On such geometry positionGeometry now resolves to that constant directly (same GLSL, no warning; FX-UBO, ADR-086);
// geometry with a position attribute (self test, microbench) keeps it.
import { PointsNodeMaterial } from 'three/webgpu'
import { overrideNode, positionGeometry, vec3 } from 'three/tsl'

type N = any // TSL nodes (loosely typed in @types/three)

export class GLPointsNodeMaterial extends PointsNodeMaterial {
  static get type(): string {
    return 'GLPointsNodeMaterial'
  }
  constructor() {
    super()
    this.contextNode = overrideNode(positionGeometry as N, (builder: { geometry?: { hasAttribute?(n: string): boolean } | null }) =>
      builder.geometry?.hasAttribute?.('position') === false ? vec3(0, 0, 0) : positionGeometry) as N
  }
  onBeforeCompile(p: { vertexShader: string }): void {
    p.vertexShader = p.vertexShader.replace(/\n\s*gl_PointSize = 1\.0;/, '\n')
  }
  customProgramCacheKey(): string {
    return super.customProgramCacheKey() + '|glps'
  }
}
