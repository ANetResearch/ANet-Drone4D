import { describe, expect, it } from 'vitest'
import type { Target } from '@awr/contracts/types'
import { customizationAssets, customizationErrors, customizationOf, customizationPatch } from '@/ui/panels/targets/customization'

describe('manual target configuration', () => {
  it('offers only compatible assets and gives each new class a real default mesh', () => {
    for (const cls of ['person', 'vehicle', 'dog', 'uav', 'machine', 'object'] as const) {
      const entries = customizationAssets(cls)
      const value = customizationOf({ label: cls }, cls, true)
      expect(entries.length).toBeGreaterThan(0)
      expect(entries.every((a) => a.class === cls)).toBe(true)
      expect(entries.some((a) => a.id === value.assetId)).toBe(true)
    }
  })
  it('sends selected vehicle mesh, dimensions and normalized plate in one update', () => {
    const value = { ...customizationOf({ label: 'Delivery' }, 'vehicle', true), assetId: 'van', plate: ' 皖a 12345 ', region: '安徽' }
    const patch = customizationPatch('vehicle', value)
    const asset = customizationAssets('vehicle').find((a) => a.id === 'van')!
    expect(patch.appearance).toEqual({ asset_id: 'van', geometry_sha256: asset.geometry_sha256 })
    expect(patch.size_m).toEqual({ l: asset.dimensions_m[0], w: asset.dimensions_m[1], h: asset.dimensions_m[2] })
    expect(patch.metadata).toMatchObject({ vehicle_type: 'van', plate_text: '皖A 12345', plate_region: '安徽' })
    expect(patch).not.toHaveProperty('motion')
    expect(patch).not.toHaveProperty('id')
    expect(patch).not.toHaveProperty('pos')
  })
  it('metadata-only edits preserve an admitted body envelope, recipe and identity', () => {
    const original: Partial<Target> = { id: 'driver-01', label: 'Driver', class: 'vehicle', appearance: { asset_id: 'sedan', geometry_sha256: 'a'.repeat(64) },
      size_m: { l: 6, w: 3, h: 3 }, life: { actor_id: 'driver-01', recipe_id: 'life', sha256: 'b'.repeat(64) }, metadata: { vehicle_type: 'sedan', plate_text: 'OLD' } }
    const patch = customizationPatch('vehicle', { ...customizationOf(original, 'vehicle'), plate: '' }, original)
    expect(patch.metadata?.plate_text).toBe('')
    for (const key of ['id', 'life', 'size_m', 'appearance', 'motion', 'pos']) expect(patch).not.toHaveProperty(key)
    expect(original.metadata?.plate_text).toBe('OLD')
  })
  it('rejects cross-class meshes and invalid plate characters before placing', () => {
    const person = { ...customizationOf({ label: 'Person' }, 'person', true), assetId: 'sedan' }
    expect(customizationErrors('person', person)).toContain('asset')
    expect(() => customizationPatch('person', person)).toThrow()
    const car = { ...customizationOf({ label: 'Car' }, 'vehicle', true), plate: 'ABC\n123' }
    expect(customizationErrors('vehicle', car)).toContain('plate')
  })
  it('does not leak vehicle fields into manually created people or dogs', () => {
    for (const cls of ['person', 'dog'] as const) {
      const patch = customizationPatch(cls, { ...customizationOf({ label: cls }, cls, true), plate: 'HIDDEN', region: 'HIDDEN' })
      expect(patch.metadata).not.toHaveProperty('plate_text')
      expect(patch.metadata).not.toHaveProperty('plate_region')
      expect(patch.metadata).not.toHaveProperty('vehicle_type')
    }
  })
  it('explicit wireframe selection removes only the appearance, retaining existing geometry', () => {
    const original: Partial<Target> = { label: 'Dog', appearance: { asset_id: 'dog-small', geometry_sha256: 'c'.repeat(64) } }
    const patch = customizationPatch('dog', { ...customizationOf(original, 'dog'), assetId: '' }, original)
    expect(patch.appearance).toBeNull()
    expect(patch).not.toHaveProperty('size_m')
  })
})
