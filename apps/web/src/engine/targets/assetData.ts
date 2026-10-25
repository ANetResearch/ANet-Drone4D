// Shared, hash-verified source data for the scene renderer and isolated appearance preview.
import { SRGBColorSpace, Texture } from 'three'
import catalog from '../../../../../packages/contracts/target/assets.catalog.json'
import type { WheelGroups } from './wheels'

export const TARGET_ASSETS = catalog.assets
export type TargetAsset = typeof TARGET_ASSETS[number] & { wheels?: { sha256: string; bytes: number; count: number; vertices: number } }
export interface LoadedTargetAsset { asset: TargetAsset; p: Float32Array; n: Float32Array; uv: Float32Array; c: Float32Array; poses: Float32Array; indices: Uint32Array; tex: Texture; image: ImageBitmap; wheels?: WheelGroups }

async function verified(url: string, hash: string, bytes: number, signal: AbortSignal): Promise<ArrayBuffer> {
  const res = await fetch(url, { signal })
  if (!res.ok) throw new Error(`Asset HTTP ${res.status}`)
  const buffer = await res.arrayBuffer()
  if (buffer.byteLength !== bytes) throw new Error('Asset length mismatch')
  const sum = await crypto.subtle.digest('SHA-256', buffer)
  const hex = Array.from(new Uint8Array(sum), (n) => n.toString(16).padStart(2, '0')).join('')
  if (hex !== hash) throw new Error('Asset hash mismatch')
  return buffer
}

export async function loadTargetAsset(asset: TargetAsset, signal: AbortSignal): Promise<LoadedTargetAsset> {
  const base = '/assets/target-assets/'
  const [buffer, png, wheelBuffer] = await Promise.all([
    verified(`${base}${asset.geometry_sha256}.bin`, asset.geometry_sha256, asset.bytes, signal),
    verified(`${base}${asset.texture_sha256}.png`, asset.texture_sha256, asset.texture_bytes, signal),
    asset.wheels ? verified(`${base}${asset.wheels.sha256}.wheels.bin`, asset.wheels.sha256, asset.wheels.bytes, signal) : Promise.resolve(null),
  ])
  const image = await createImageBitmap(new Blob([png]), { imageOrientation: 'flipY' })
  if (signal.aborted) { image.close(); throw new DOMException('Aborted', 'AbortError') }
  const tex = new Texture(image)
  tex.colorSpace = SRGBColorSpace
  tex.flipY = false
  tex.needsUpdate = true
  const floats = (key: 'p' | 'n' | 'uv' | 'c' | 'poses') => new Float32Array(buffer, asset.layout[key].offset, asset.layout[key].count)
  const wheels = asset.wheels && wheelBuffer ? {
    ids: new Int32Array(wheelBuffer, 0, asset.wheels.vertices),
    centers: new Float32Array(wheelBuffer, asset.wheels.vertices * 4, asset.wheels.count * 3),
    radii: new Float32Array(wheelBuffer, (asset.wheels.vertices + asset.wheels.count * 3) * 4, asset.wheels.count),
  } : undefined
  return { asset, wheels, p: floats('p'), n: floats('n'), uv: floats('uv'), c: floats('c'), poses: floats('poses'),
    indices: new Uint32Array(buffer, asset.layout.indices.offset, asset.layout.indices.count), tex, image }
}
