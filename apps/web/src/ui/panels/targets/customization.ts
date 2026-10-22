import type { Target } from '@awr/contracts/types'
import catalog from '../../../../../../packages/contracts/target/assets.catalog.json'

export const customizationAssets = (cls: Target['class']) => catalog.assets.filter((a) => a.class === cls)
export interface Customization {
  label: string; assetId: string; role: string; description: string; plate: string; region: string
}
export function customizationOf(target: Partial<Target>, cls: Target['class'], creating = false): Customization {
  return {
    label: target.label ?? '', assetId: target.appearance?.asset_id ?? (creating ? customizationAssets(cls)[0]?.id ?? '' : ''),
    role: target.metadata?.role ?? '', description: target.metadata?.description ?? '',
    plate: target.metadata?.plate_text ?? '', region: target.metadata?.plate_region ?? '',
  }
}
const cleanText = (s: string, max: number) => Array.from(s).length <= max && Array.from(s).every((c) => c.charCodeAt(0) >= 32 && c.charCodeAt(0) !== 127)
export function customizationErrors(cls: Target['class'], value: Customization): string[] {
  const errors: string[] = []
  if (!value.label.trim() || !cleanText(value.label.trim(), 32)) errors.push('label')
  if (value.assetId && !customizationAssets(cls).some((a) => a.id === value.assetId)) errors.push('asset')
  if (!cleanText(value.role, 48)) errors.push('role')
  if (!cleanText(value.description, 256)) errors.push('description')
  if (cls === 'vehicle') {
    if (!/^[A-Z0-9一-鿿 -]{0,16}$/.test(value.plate.trim().toUpperCase())) errors.push('plate')
    if (!cleanText(value.region, 32)) errors.push('region')
  }
  return errors
}
/** Build one atomic configuration update; never include pose, motion, life or identity keys. */
export function customizationPatch(cls: Target['class'], value: Customization, original: Partial<Target> = {}): Partial<Target> {
  if (customizationErrors(cls, value).length) throw new Error('Invalid target customization')
  const asset = customizationAssets(cls).find((a) => a.id === value.assetId)
  const metadata = { ...original.metadata, role: value.role.trim(), description: value.description.trim() }
  if (cls === 'vehicle') {
    metadata.plate_text = value.plate.trim().toUpperCase()
    metadata.plate_region = value.region.trim()
    if (asset) metadata.vehicle_type = asset.id as NonNullable<Target['metadata']>['vehicle_type']
  }
  const patch: Partial<Target> = { label: value.label.trim(), metadata }
  // Metadata-only changes must not resize an admitted actor or reset its appearance.
  if ((original.appearance?.asset_id ?? '') !== value.assetId) {
    patch.appearance = asset ? { asset_id: asset.id, geometry_sha256: asset.geometry_sha256 } : null
    if (asset) patch.size_m = { l: asset.dimensions_m[0], w: asset.dimensions_m[1], h: asset.dimensions_m[2] }
  }
  return patch
}
