// Weather map and cloud mask (M07-FR-037, FR-038; M07 §6.8.2 items 2-3; r16 §3.4.4, §3.4.5). Owner: M07.
// The shared weather map (AWRV kind 6, 512^2 RGBA8, tileable over config.weather_map.scale_m = 24 km; R coverage fbm,
// G cell structure) drives the 2D clouds in the sky and the cloud shadows with one mask:
//   cov = clamp((r - 0.5) 1.6 + cover, 0, 1) smoothstep(0, 0.05, cover);  m = smoothstep(0.4, 0.8, cov (0.6 + 0.4 g))
// sampled at (xy + cloudOffset) / scale on the plane z = ground + h_mid (h_mid = base + 0.35 (top - base)); the offset is
// the cloud drift -(f(h_mid) D) mod scale computed on the CPU in float64. Until the asset loads (or when it is missing,
// M07-FR-026) a neutral 1 x 1 texture (r = 0.5, g = 1) gives a constant coverage `cover` without spatial structure.
import { DataTexture, RepeatWrapping, RGBAFormat, UnsignedByteType, type Texture } from 'three'
import { clamp, float, smoothstep, texture, vec2 } from 'three/tsl'
import type { EnvNodes } from '../lighting/EnvUniforms'
import { smoothstep as ss } from '../state/derive'
import { decodeAWRV, volumeTexture2D, type AwrvVolume } from '../wind/awrv'
import { ENV_TIERS } from '../quality/envTiers'

type N = any // TSL nodes

export class WeatherMap {
  readonly neutral: DataTexture
  /** base texture node: materials sample it through texture(base, uv) so a later load swaps the value everywhere */
  readonly node: N
  vol: AwrvVolume | null = null
  loaded = false
  error: string | null = null

  constructor() {
    this.neutral = new DataTexture(new Uint8Array([128, 255, 128, 255]), 1, 1, RGBAFormat, UnsignedByteType)
    this.neutral.wrapS = RepeatWrapping
    this.neutral.wrapT = RepeatWrapping
    this.neutral.needsUpdate = true
    // no uv transform: the texture matrix is identity (three would multiply every sample by it, FX2-R2)
    this.node = texture(this.neutral as Texture)
    this.node.updateMatrix = false
  }

  async load(url: string, fetcher: (u: string) => Promise<ArrayBuffer>): Promise<void> {
    try {
      const v = decodeAWRV(await fetcher(url))
      this.vol = v
      this.node.value = volumeTexture2D(v)
      this.loaded = true
      this.error = null
    } catch (e) {
      this.error = String(e)
    }
  }

  /** CPU mask at world xy (same formula as the TSL node; bilinear on the u8 texels) */
  maskAt(x: number, y: number, cover: number, offX: number, offY: number, scale: number): number {
    let r = 0.5
    let g = 1
    const v = this.vol
    if (v) {
      const u = (((x + offX) / scale) % 1 + 1) % 1
      const w = (((y + offY) / scale) % 1 + 1) % 1
      const fx = u * v.nx - 0.5
      const fy = w * v.ny - 0.5
      const x0 = Math.floor(fx)
      const y0 = Math.floor(fy)
      const tx = fx - x0
      const ty = fy - y0
      const d = v.raw as Uint8Array
      const at = (ix: number, iy: number, c: number): number => d[4 * ((((iy % v.ny) + v.ny) % v.ny) * v.nx + (((ix % v.nx) + v.nx) % v.nx)) + c] / 255
      const lerp2 = (c: number): number =>
        (at(x0, y0, c) * (1 - tx) + at(x0 + 1, y0, c) * tx) * (1 - ty) + (at(x0, y0 + 1, c) * (1 - tx) + at(x0 + 1, y0 + 1, c) * tx) * ty
      r = lerp2(0)
      g = lerp2(1)
    }
    const cov = Math.min(Math.max((r - 0.5) * ENV_TIERS.cloudContrast + cover, 0), 1) * ss(0, 0.05, cover)
    return ss(0.4, 0.8, cov * (0.6 + 0.4 * g))
  }
}

/** TSL cloud mask at ENU xy on the cloud plane */
export function cloudMaskNode(n: EnvNodes, wm: WeatherMap, xy: N): N {
  const uv: N = xy.add(n.cloudOffset).div(n.weatherScale)
  const t: N = texture(wm.node, vec2(uv.x, uv.y))
  const cov: N = clamp(t.r.sub(0.5).mul(ENV_TIERS.cloudContrast).add(n.cloudCover), float(0), float(1)).mul(smoothstep(float(0), float(0.05), n.cloudCover))
  return smoothstep(float(0.4), float(0.8), cov.mul(float(0.6).add(t.g.mul(0.4))))
}
