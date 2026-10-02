// M05-AC-006 (FR-001), AC-018, AC-029 (request part), AC-032 (static browsing): the request sequence of opening shenzhen
// without a backend (no WS, /api answers 404): world.json with no-cache, every other /worlds request with ?v=, whole
// hierarchy.bin, one first-screen Range per root before any node Range, never a multi-range or a content-type header,
// only GET /worlds/** from the point cloud engine, and at most 4 point cloud requests in flight (HTTP/1.1).
import { expect, test } from '@playwright/test'
import { expectNoErrors, openWorld, perf, m05Server, watch } from './common'

const srv = m05Server(1)

test('shenzhen request sequence and static browsing (M05-AC-006, AC-018, AC-032)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  await page.waitForTimeout(3000)
  // M05's files only: other modules read semantic/zones.geojson, environment/env.json and world.json themselves
  const mine = /\/worlds\/shenzhen\/(world\.json|coordinate\.json|visual\/pointcloud\/|geometry\/terrain\/dtm_10m)/
  const all = srv.get().log.filter((e) => e.path.startsWith('/worlds/shenzhen/'))
  const log = all.filter((e) => mine.test(e.path))
  test.info().annotations.push({ type: 'other-world-requests', description: all.filter((e) => !mine.test(e.path)).map((e) => e.path + e.query).join(' ') })
  expect(log[0].path).toBe('/worlds/shenzhen/world.json')
  // every world.json read revalidates (the engine's and the UI's dataset query, which may come first; AWR-17 §5.1)
  for (const e of log.filter((x) => x.path.endsWith('/world.json'))) expect(e.cacheControl ?? '', e.path).toMatch(/no-cache|max-age=0/)
  const cv = await page.evaluate(() => (window as unknown as { __pc: { engine: { info: { contentVersion: string } } } }).__pc.engine.info.contentVersion)
  // the first world.json is the engine's (no-cache, no ?v=); later world.json reads (other modules, reopen) are allowed
  for (const e of log.slice(1)) if (!e.path.endsWith('world.json')) expect(e.query, e.path).toContain(`v=${cv}`)
  for (const e of log) {
    expect(e.method).toBe('GET')
    expect(e.contentType).toBeNull()
    if (e.range) expect(e.range).toMatch(/^bytes=\d+-\d+$/)
  }
  expect(log.find((e) => e.path.endsWith('/hierarchy.bin'))!.range).toBeNull()
  const octree = log.filter((e) => e.path.endsWith('/octree.bin'))
  expect(octree[0].range).toBe('bytes=0-321383')
  expect(octree.filter((e) => e.range?.startsWith('bytes=0-'))).toHaveLength(1)
  const dtm = log.findIndex((e) => e.path.includes('dtm_10m'))
  expect(dtm).toBeGreaterThan(log.indexOf(octree[0])) // DTM after the first screen (M05-AC-035 order)
  expect(await perf<number>(page, 'load.firstScreenBytes')).toBe(321_384)
  expect(Number.isFinite(await perf<number>(page, 'load.ttfp'))).toBe(true)
  expect(await perf<number>(page, 'pc.drawn')).toBeGreaterThan(10_000)
  // in-flight bound: requests overlapping in time never exceed 4 (arrival log is a lower bound of concurrency)
  expect(await perf<number>(page, 'pc.inflight')).toBeLessThanOrEqual(4)
  expectNoErrors(w)
})
