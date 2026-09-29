// M05-AC-029 (request part, P0 structural isolation): while colour modes, classes, the rung and the budget change, every
// request of the point cloud engine is GET /worlds/** (the import boundary engine/pointcloud -> net/** is enforced by
// oxlint TS-BND-01; this spec checks the wire).
import { expect, test } from '@playwright/test'
import { expectNoErrors, frames, openWorld, m05Server, watch } from './common'

const srv = m05Server(11)

test('point cloud traffic is GET /worlds/** only (M05-AC-029)', async ({ page }) => {
  const w = watch(page)
  const reqs: { method: string; url: string; type: string }[] = []
  page.on('request', (r) => reqs.push({ method: r.method(), url: r.url(), type: r.resourceType() }))
  await openWorld(page, srv.get().url, 'shenzhen')
  await page.evaluate(() => {
    const l = (window as unknown as { __pc: { layers: { setColorMode(m: string): void; setQuality(q: unknown): void; setClassVisible(i: number, on: boolean): void } } }).__pc.layers
    l.setColorMode('class')
    l.setClassVisible(6, false)
    l.setQuality(1)
  })
  await frames(page, 60)
  const fetches = reqs.filter((r) => r.type === 'fetch' || r.type === 'xhr')
  const pointCloud = fetches.filter((r) => r.url.includes('/worlds/'))
  expect(pointCloud.length).toBeGreaterThan(5)
  for (const r of pointCloud) expect(r.method).toBe('GET')
  expectNoErrors(w)
})
