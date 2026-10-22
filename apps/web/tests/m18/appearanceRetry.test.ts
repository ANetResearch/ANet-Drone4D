import { afterEach, expect, it, vi } from 'vitest'
import { assetStatus, retryAsset, TARGET_ASSETS, TargetAppearances } from '../../src/engine/targets/appearance'
import type { TargetsModel } from '../../src/engine/targets/model'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('requires an explicit retry after failure, without retrying every render frame', async () => {
  const fetchMock = vi.fn().mockRejectedValue(new TypeError('Network unavailable'))
  vi.stubGlobal('fetch', fetchMock)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  const asset = TARGET_ASSETS[0]
  const model = {
    rows: { n: 1 }, idOfRow: () => 'resident-stable', classOfRow: () => asset.class,
    cfgs: new Map([['resident-stable', { appearance: { asset_id: asset.id, geometry_sha256: asset.geometry_sha256 } }]]),
  } as unknown as TargetsModel
  const layer = new TargetAppearances()
  try {
    layer.update(model)
    await vi.waitFor(() => expect(assetStatus(asset.id)).toBe('error'))
    const firstAttempt = fetchMock.mock.calls.length
    expect(firstAttempt).toBeGreaterThan(0)
    for (let i = 0; i < 60; i++) layer.update(model)
    expect(fetchMock).toHaveBeenCalledTimes(firstAttempt)
    retryAsset(asset.id)
    expect(assetStatus(asset.id)).toBe('idle')
    layer.update(model)
    for (let i = 0; i < 60; i++) layer.update(model)
    await vi.waitFor(() => expect(assetStatus(asset.id)).toBe('error'))
    expect(fetchMock).toHaveBeenCalledTimes(firstAttempt * 2)
    expect(model.idOfRow(0)).toBe('resident-stable')
  } finally { layer.dispose() }
  expect(assetStatus(asset.id)).toBe('idle')
  retryAsset(asset.id)
  expect(fetchMock).toHaveBeenCalledTimes(4)
})

it('bounds asset download concurrency and admits queued assets when a slot becomes free', async () => {
  const rejectors: Array<(reason: Error) => void> = []
  vi.stubGlobal('fetch', vi.fn(() => new Promise((_resolve, reject) => rejectors.push(reject))))
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  const assets = TARGET_ASSETS.slice(0, 3)
  const model = {
    rows: { n: 3 }, idOfRow: (i: number) => `resident-${i}`, classOfRow: (i: number) => assets[i].class,
    cfgs: new Map(assets.map((asset, i) => [`resident-${i}`, { appearance: { asset_id: asset.id, geometry_sha256: asset.geometry_sha256 } }])),
  } as unknown as TargetsModel
  const layer = new TargetAppearances()
  try {
    for (let i = 0; i < 60; i++) layer.update(model)
    expect(assets.map(a => assetStatus(a.id))).toEqual(['loading', 'loading', 'idle'])
    rejectors[0](new Error('Connection interrupted'))
    await vi.waitFor(() => expect(assetStatus(assets[0].id)).toBe('error'))
    layer.update(model)
    expect(assets.map(a => assetStatus(a.id))).toEqual(['error', 'loading', 'loading'])
  } finally {
    layer.dispose()
    rejectors.forEach(reject => reject(new Error('Disposed')))
    await Promise.resolve()
  }
})
