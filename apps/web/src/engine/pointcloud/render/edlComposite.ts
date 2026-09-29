// EDL composite material for Tier B/A (M05 §6.7.6, M05-FR-035; AWR-15 §10.3; g01 feat.js depthPipelines; r11 §3.5, r12
// §3.6). Owner: M05. M06 draws it in pass P2 as the full-screen quad (PlaneGeometry(2, 2), positions in clip space) after
// P1 rendered the cloud into the cloudRT sub-viewport (dbW x s, dbH x s). One program covers every combination: uvScale,
// strength (0 = EDL off) and taps (4 or 8) are float uniforms. Formula (log2 of the linear depth; background is depth =
// far plane, never log depth = 0):
//   lz = log2(-viewZ(d)); sum = sum over taps of [neighbour not background] max(0, lz - nd_i); nd_i at uv + texel 1.4 dir_i
//   shade = exp(-300 strength sum / taps); colour = colour(uv) shade; depthNode = d (the cloud depth is written back)
// Background pixels output the sky Fn injected by M06 (setBackgroundNode, called with the clip-space xy) at the far
// plane. Texture coordinates are clamped half a texel inside the sub-viewport so neighbours never read cleared pixels.
// bindTargets() swaps the texture values (no recompile) and derives the texel size from the colour target.
import { AlwaysDepth, DataTexture, DepthTexture, FloatType, NeverDepth, type Texture } from 'three'
import { MeshBasicNodeMaterial } from 'three/webgpu'
import { Fn, If, exp, float, log2, max, min, perspectiveDepthToViewZ, positionGeometry, select, texture, uniform, vec2, vec3, vec4 } from 'three/tsl'
import type { N } from './fetchNode'

export interface EdlUniforms {
  uvScale: N
  strength: N
  taps: N
  near: N
  far: N
  /** 1 / cloudRT full size (px) */
  texel: N
  radiusPx: N
}

/** sky Fn of M06: clip-space xy (vec2 node) -> linear colour (vec3 node) */
export type ShaderNodeFn = (clipXY: N) => N

const DIRS: readonly [number, number][] = Array.from({ length: 8 }, (_, i) => [Math.cos((i * Math.PI) / 4), Math.sin((i * Math.PI) / 4)])

export class EdlCompositeMaterial extends MeshBasicNodeMaterial {
  readonly uniforms: EdlUniforms
  private readonly colorT: N
  private readonly depthT: N
  private background: ShaderNodeFn
  private readonly reversed: boolean
  private readonly placeholders: Texture[]

  constructor(o: { reversedDepth: boolean; strength: number; radiusPx: number; clear: readonly [number, number, number]; color?: Texture; depth?: DepthTexture }) {
    super()
    this.reversed = o.reversedDepth
    this.uniforms = {
      uvScale: uniform(1), strength: uniform(o.strength), taps: uniform(8), near: uniform(0.5), far: uniform(20000), texel: uniform(vec2(1, 1)),
      radiusPx: uniform(o.radiusPx),
    }
    const ph = new DataTexture(new Uint8Array(4), 1, 1)
    ph.needsUpdate = true
    const dph = new DepthTexture(1, 1)
    dph.type = FloatType
    this.placeholders = [ph, dph]
    this.colorT = texture(o.color ?? ph)
    this.depthT = texture(o.depth ?? dph)
    const clear = o.clear
    this.background = () => vec3(clear[0], clear[1], clear[2])
    this.depthTest = true
    this.depthWrite = true
    // three r186 flips every depth function under a reversed depth buffer (utils.js ReversedDepthFuncs), AlwaysDepth
    // included, which turns it into NEVER: request NeverDepth there to obtain GL_ALWAYS
    this.depthFunc = o.reversedDepth ? NeverDepth : AlwaysDepth
    this.transparent = false
    this.fog = false
    this.buildNodes()
    if (o.color) this.setTexel(o.color)
  }

  /** M06 calls this after allocating or rebuilding cloudRT (colour RGBA8 + DepthTexture) */
  bindTargets(color: Texture, depth: DepthTexture): void {
    this.colorT.value = color
    this.depthT.value = depth
    this.setTexel(color)
  }

  private setTexel(color: Texture): void {
    const img = color.image as { width?: number; height?: number } | undefined
    const w = img?.width ?? 1
    const h = img?.height ?? 1
    this.uniforms.texel.value.set(1 / Math.max(1, w), 1 / Math.max(1, h))
  }

  /** camera near and far of the cloud pass (the engine sets them every frame) */
  setCamera(near: number, far: number): void {
    this.uniforms.near.value = near
    this.uniforms.far.value = far
  }

  /** sky Fn for background pixels; call before the first render (it rebuilds the node graph once) */
  setBackgroundNode(fn: ShaderNodeFn): void {
    this.background = fn
    this.buildNodes()
    this.needsUpdate = true
  }

  private buildNodes(): void {
    const u = this.uniforms
    const colorT = this.colorT
    const depthT = this.depthT
    const rev = this.reversed
    const clip: N = (positionGeometry as N).xy
    this.vertexNode = vec4(clip, 0, 1)
    const isBg = (d: N): N => (rev ? d.lessThanEqual(1e-7) : d.greaterThanEqual(0.9999999))
    const hiUv: N = vec2(u.uvScale, u.uvScale).sub(u.texel.mul(0.5))
    const clampUv = (st: N): N => min(max(st, u.texel.mul(0.5)), hiUv)
    const logZ = (d: N): N => log2(max(perspectiveDepthToViewZ(d, u.near, u.far).negate(), float(1e-6)))
    const bg = this.background
    const stOf = (): N => clampUv(clip.mul(0.5).add(0.5).mul(u.uvScale))
    // colour and depth are separate expressions: one Fn result read by both colorNode and depthNode renders black on the
    // classic path (the depth flow does not see the colour flow's variables)
    const out: N = Fn(() => {
      const st: N = stOf().toVar()
      const d: N = depthT.sample(st).r.toVar()
      const col: N = vec3(0).toVar()
      If(isBg(d), () => {
        col.assign(bg(clip))
      }).Else(() => {
        const z0: N = logZ(d).toVar()
        const sum: N = float(0).toVar()
        for (let i = 0; i < 8; i++) {
          const use: N = i % 2 === 0 ? float(1) : select(u.taps.greaterThan(4.5), float(1), float(0))
          const nst: N = clampUv(st.add(u.texel.mul(u.radiusPx).mul(vec2(DIRS[i][0], DIRS[i][1]))))
          const nd: N = depthT.sample(nst).r.toVar()
          sum.addAssign(select(isBg(nd), float(0), max(float(0), z0.sub(logZ(nd)))).mul(use))
        }
        const shade: N = exp(float(-300).mul(u.strength).mul(sum).div(max(u.taps, float(1))))
        col.assign(colorT.sample(st).rgb.mul(shade))
      })
      return vec4(col, 1)
    })()
    this.colorNode = out
    this.depthNode = depthT.sample(stOf()).r
  }

  dispose(): void {
    for (const t of this.placeholders) t.dispose()
    super.dispose()
  }
}

export function makeEdlCompositeMaterial(o: ConstructorParameters<typeof EdlCompositeMaterial>[0]): EdlCompositeMaterial {
  return new EdlCompositeMaterial(o)
}
