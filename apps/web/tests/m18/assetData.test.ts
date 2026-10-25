import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadTargetAsset, TARGET_ASSETS, type TargetAsset } from '@/engine/targets/assetData'

const digest = async (buffer: ArrayBuffer) => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', buffer)), (n) => n.toString(16).padStart(2, '0')).join('')
async function fixture() {
  const geometry = new ArrayBuffer(48), png = new Uint8Array([1, 2, 3, 4]).buffer
  new Float32Array(geometry, 0, 3).set([1, 2, 3])
  const asset: TargetAsset = { ...TARGET_ASSETS[0], bytes: 48, texture_bytes: 4, wheels: undefined,
    geometry_sha256: await digest(geometry), texture_sha256: await digest(png),
    layout: { p: { offset: 0, count: 3 }, n: { offset: 12, count: 3 }, uv: { offset: 24, count: 2 },
      c: { offset: 32, count: 3 }, poses: { offset: 44, count: 0 }, indices: { offset: 44, count: 1 } } }
  const image = { close: vi.fn() }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(url.endsWith('.bin') ? geometry : png)))
  vi.stubGlobal('createImageBitmap', vi.fn(async () => image))
  return { asset, image }
}
afterEach(() => vi.unstubAllGlobals())
describe('shared preview and scene source loader', () => {
  it('keeps source coordinates and flipped texture orientation consistent', async () => {
    const { asset } = await fixture()
    const data = await loadTargetAsset(asset, new AbortController().signal)
    expect(Array.from(data.p)).toEqual([1, 2, 3])
    expect(createImageBitmap).toHaveBeenCalledWith(expect.any(Blob), { imageOrientation: 'flipY' })
    expect(data.tex.flipY).toBe(false)
    data.tex.dispose(); data.image.close()
  })
  it('refuses tampered content before decoding an image', async () => {
    const { asset } = await fixture()
    await expect(loadTargetAsset({ ...asset, geometry_sha256: '0'.repeat(64) }, new AbortController().signal)).rejects.toThrow('hash mismatch')
    expect(createImageBitmap).not.toHaveBeenCalled()
  })
  it('refuses incomplete downloads', async () => {
    const { asset } = await fixture()
    await expect(loadTargetAsset({ ...asset, texture_bytes: 5 }, new AbortController().signal)).rejects.toThrow('length mismatch')
  })
  it('does not turn an HTTP error into a ready preview', async () => {
    const { asset } = await fixture()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 404 })))
    await expect(loadTargetAsset(asset, new AbortController().signal)).rejects.toThrow('HTTP 404')
  })
  it('closes a bitmap when selection changes during decoding', async () => {
    const { asset, image } = await fixture()
    const abort = new AbortController()
    vi.stubGlobal('createImageBitmap', vi.fn(async () => { abort.abort(); return image }))
    await expect(loadTargetAsset(asset, abort.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(image.close).toHaveBeenCalledOnce()
  })
})
