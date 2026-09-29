// End-to-end engine behaviour in Node (no GL; texture copies recorded): M05-AC-006 request sequence, AC-013 streaming and
// residency invariants, AC-014 upload quota, AC-015 page utilisation, AC-018 in-flight limit, AC-008 world switch,
// AC-026 device loss without re-download, AC-033 content-version change, AC-017 failure injection with requeue, the
// first-frame target set and TTFP bookkeeping, CAS telemetry. Runs the flight60 camera of the generated worlds.
import { afterEach, describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { NS } from '@/engine/pointcloud/core/NodeStore'
import { LADDER, PC } from '@/engine/pointcloud/params'
import { sampleFlight60 } from '@/engine/pointcloud/bench/flight60'
import { haveWorlds } from './helpers'
import { makeRig, worldBase, type Rig } from './engineHarness'

const FLIGHTS = new URL('../../public/bench/flight60/', import.meta.url)
const haveFlight = haveWorlds && existsSync(new URL('shenzhen.bin', FLIGHTS))
function flightOf(city: string): Float32Array {
  const b = readFileSync(new URL(`${city}.bin`, FLIGHTS))
  return new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength))
}
const EYE = new Float64Array(3)
const TGT = new Float64Array(3)

let rig: Rig | null = null
afterEach(() => {
  rig?.dispose()
  rig = null
})

/** open a world, look from the flight60 start pose and run until the first-frame target set is drawn */
async function openAndFirstFrame(r: Rig, city: string, fl: Float32Array): Promise<void> {
  sampleFlight60(fl, 0, EYE, TGT)
  r.look(EYE, TGT)
  await r.engine.open(worldBase(city))
  for (let k = 0; k < 60 && r.engine.enginePhase !== 'streaming'; k++) await r.frame()
}

/** invariants checked after every frame of a flight */
function checkFrame(r: Rig, acc: { maxUpload: number; violations: string[] }): void {
  const t = r.engine.store!
  const s = r.engine.stats()
  if (s.drawn > s.Beff) acc.violations.push(`drawn ${s.drawn} > Beff ${s.Beff}`)
  for (let i = 0; i < t.N; i++) {
    if (t.poolBase[i] >= 0 && t.cacheSlot[i] < 0) acc.violations.push(`node ${i} resident but not cached`)
    if (t.poolBase[i] >= 0 && t.state[i] !== NS.RESIDENT) acc.violations.push(`node ${i} poolBase without RESIDENT`)
  }
  const roots = Array.from(t.roots).reduce((a, i) => a + t.numPoints[i], 0)
  const Bref = Math.min(LADDER[s.rungIndex].hi, PC.capacityFrac * r.engine.poolCapacityPts)
  if (s.residentPts > PC.evictTrigger * Bref + roots + 32_000) acc.violations.push(`resident ${s.residentPts} > 1.5 Bref + roots`)
}

describe.skipIf(!haveFlight)('PointCloudEngine streaming in Node', { timeout: 300_000 }, () => {
  it('shenzhen: request sequence, first screen, TTFP bookkeeping, then flight60 with every invariant', async () => {
    const r = (rig = makeRig())
    const fl = flightOf('shenzhen')
    const opened: string[] = []
    r.engine.on('pc.world.opened', (e) => opened.push(e.reason))
    let firstFrame = 0
    r.engine.on('pc.first.frame', () => firstFrame++)
    await openAndFirstFrame(r, 'shenzhen', fl)
    expect(opened).toEqual(['open'])
    expect(r.engine.enginePhase).toBe('streaming')
    expect(firstFrame).toBe(1)
    expect(Number.isFinite(r.perf.load.ttfp)).toBe(true)
    expect(r.perf.load.firstScreenBytes).toBe(321_384)
    // M05-AC-006: world.json no-cache, ?v= everywhere else, whole hierarchy, one first-screen Range before node Ranges
    const log = r.server.log
    const cv = r.engine.info!.contentVersion
    expect(log[0].url).toBe(`${worldBase('shenzhen')}world.json`)
    expect(log[0].cache).toBe('no-cache')
    for (const e of log.slice(1)) expect(e.url).toContain(`?v=${cv}`)
    expect(log.find((e) => e.url.includes('hierarchy.bin'))!.range).toBeNull()
    const ranges = log.filter((e) => e.range !== null && e.url.includes('octree.bin'))
    expect(ranges[0].range).toBe('bytes=0-321383')
    // the first-frame target set came entirely from the first screen: no node request before the first frame
    // flight60 (30 Hz sampled): budgets, residency, uploads, in-flight
    r.engine.markRevealed()
    const acc = { maxUpload: 0, violations: [] as string[] }
    let t = 0
    await r.frames(1800, () => {
      t += 1 / 30
      sampleFlight60(fl, t, EYE, TGT)
      r.look(EYE, TGT)
      checkFrame(r, acc)
    })
    expect(acc.violations.slice(0, 5)).toEqual([])
    const pc = r.perf.pc
    expect(pc.budgetViolations).toBe(0)
    expect(pc.failed).toBe(0)
    expect(r.server.maxInflight).toBeLessThanOrEqual(4) // M05-AC-018 (HTTP/1.1 cap 4)
    expect(pc.uploadPtsMax).toBeLessThanOrEqual(PC.uploadPtsS) // M05-AC-014 (first-over-quota frames counted apart)
    expect(pc.downloadedBytes / pc.uniqueBytes).toBeLessThanOrEqual(1.3) // M05-AC-013
    expect(pc.cpuCachePeak).toBeLessThanOrEqual(PC.cpuCacheS)
    expect(r.engine.stats().poolStalls).toBe(0)
    expect(pc.pageUtil).toBeGreaterThan(0.9)
    expect(pc.limitedByHist.reduce((a, b) => a + b, 0)).toBeGreaterThan(1700)
    // DTM arrived after the first screen (M05-AC-035 order) and samples like the terrain
    expect(r.engine.dtm.loaded).toBe(true)
    const iDtm = log.findIndex((e) => e.url.includes('dtm_10m'))
    expect(iDtm).toBeGreaterThan(log.indexOf(ranges[0]))
  })

  it('world switch shenzhen -> newyork -> suzhou: old world released, material and pool kept, switchMs recorded (M05-AC-008)', async () => {
    const r = (rig = makeRig())
    await openAndFirstFrame(r, 'shenzhen', flightOf('shenzhen'))
    r.engine.markRevealed()
    await r.frames(30)
    const material = r.engine.root.material
    const inits = r.be.inits
    for (const city of ['newyork', 'suzhou']) {
      const reasons: string[] = []
      const off = r.engine.on('pc.world.opened', (e) => reasons.push(e.reason))
      await openAndFirstFrame(r, city, flightOf(city))
      off()
      expect(reasons).toEqual(['switch'])
      expect(r.engine.info!.worldId).toBe(city)
      expect(r.engine.enginePhase).toBe('streaming')
      expect(Number.isFinite(r.perf.load.switchMs)).toBe(true)
      // residency and cache only hold the new world
      const t = r.engine.store!
      let res = 0
      for (let i = 0; i < t.N; i++) if (t.poolBase[i] >= 0) res += t.numPoints[i]
      expect(r.engine.stats().residentPts).toBe(res)
      expect(r.engine.root.material).toBe(material) // no new program (FR-007)
      expect(r.be.inits).toBe(inits) // the pool texture is not reallocated
    }
    // suzhou: 6 roots, one first-screen Range per root
    const suzhouRanges = r.server.log.filter((e) => e.url.includes('/suzhou/') && e.range?.startsWith('bytes=0-'))
    expect(suzhouRanges).toHaveLength(6)
  })

  it('device loss: GPU residency rebuilt from the CPU cache without a single new request (M05-AC-026)', async () => {
    const r = (rig = makeRig())
    const fl = flightOf('shenzhen')
    await openAndFirstFrame(r, 'shenzhen', fl)
    r.engine.markRevealed()
    sampleFlight60(fl, 8, EYE, TGT)
    r.look(EYE, TGT)
    await r.frames(120)
    const before = r.server.log.length
    const downloaded = r.perf.pc.downloadedBytes
    const resets: number[] = []
    r.engine.on('pc.gpu.reset', (e) => resets.push(e.recoveredMs))
    r.engine.onBackendLost()
    expect(r.engine.enginePhase).toBe('suspended')
    await r.frames(5)
    r.engine.onBackendReady(r.be)
    for (let k = 0; k < 120 && resets.length === 0; k++) await r.frame()
    expect(resets).toHaveLength(1)
    expect(r.engine.stats().progress).toBeGreaterThanOrEqual(0.95)
    expect(r.server.log.length).toBe(before)
    expect(r.perf.pc.downloadedBytes).toBe(downloaded)
  })

  it('a rebuilt world (409 on the old contentVersion) is reopened with the camera kept (M05-AC-033)', async () => {
    const r = (rig = makeRig())
    const fl = flightOf('shenzhen')
    await openAndFirstFrame(r, 'shenzhen', fl)
    r.engine.markRevealed()
    const reasons: string[] = []
    r.engine.on('pc.world.opened', (e) => reasons.push(e.reason))
    r.server.cvOverride = 'rebuilt0001'
    let t = 1
    for (let k = 0; k < 240 && !(r.engine.info?.contentVersion === 'rebuilt0001' && r.engine.enginePhase === 'streaming'); k++) {
      t += 1 / 30
      sampleFlight60(fl, t, EYE, TGT)
      r.look(EYE, TGT)
      await r.frame()
    }
    expect(reasons).toEqual(['stale'])
    expect(r.engine.info!.contentVersion).toBe('rebuilt0001')
    expect(r.engine.enginePhase).toBe('streaming')
    expect(r.engine.stats().drawn).toBeGreaterThan(0)
  })

  it('5 % injected Range failures: retries with back-off, FAILED nodes requeued after 10 s, nothing missing at the end (M05-AC-017)', async () => {
    const r = (rig = makeRig())
    const fl = flightOf('shenzhen')
    await openAndFirstFrame(r, 'shenzhen', fl)
    r.engine.markRevealed()
    r.server.failRate = 0.05
    let t = 0
    await r.frames(900, () => {
      t += 1 / 30
      sampleFlight60(fl, Math.min(t, 30), EYE, TGT)
      r.look(EYE, TGT)
    })
    expect(r.server.log.some((e) => e.status === 503)).toBe(true)
    // hold still: every selected node ends up resident
    r.server.failRate = 0
    await r.frames(400)
    const s = r.engine.stats()
    expect(s.progress).toBe(1)
    const t2 = r.engine.store!
    for (let i = 0; i < t2.N; i++) if (t2.state[i] === NS.FAILED) expect(t2.lastSeen[i]).not.toBe(t2.lastSeen[0])
  })
})
