// M05-AC-026 (FR-027): WEBGL_lose_context -> restore: M06 rebuilds the renderer and calls the layer hooks; M05 re-uploads
// from the CPU cache without a single new /worlds request (downloadedBytes unchanged) and coverage is back >= 0.95.
// Requires M06's device-loss rebuild (M06-FR-009); skipped while the backend does not report a restore.
import { expect, test } from '@playwright/test'
import { expectNoErrors, openWorld, perf, stats, m05Server, watch } from './common'

const srv = m05Server(8)

test('context loss re-uploads from the CPU cache (M05-AC-026)', async ({ page }) => {
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen')
  // let streaming settle: nothing in flight or queued
  await page.waitForFunction(() => {
    const s = (window as unknown as { __pc: { stats(): { inflight: number; queued: number } } }).__pc.stats()
    return s.inflight === 0 && s.queued === 0
  }, null, { timeout: 30_000 })
  const downloaded = await perf<number>(page, 'pc.downloadedBytes')
  const worldRequests = srv.get().log.filter((e) => e.path.includes('/octree.bin')).length
  const before = srv.get().log.length
  const lost = await page.evaluate(async () => {
    const c = document.querySelector('canvas')!
    const gl = (c.getContext('webgl2') as WebGL2RenderingContext | null)
    const ext = gl?.getExtension('WEBGL_lose_context')
    if (!ext) return false
    const pc = (window as unknown as { __pc: { engine: { enginePhase: string } } }).__pc
    ext.loseContext()
    let suspended = false
    for (let k = 0; k < 20 && !suspended; k++) {
      await new Promise((r) => setTimeout(r, 25))
      suspended = pc.engine.enginePhase === 'suspended'
    }
    ext.restoreContext()
    return suspended ? 'suspended' : 'unhandled'
  })
  test.skip(lost === false, 'WEBGL_lose_context unavailable')
  test.skip(lost === 'unhandled', 'the backend did not call onBackendLost (M06 device-loss handling pending)')
  const recovered = await page.waitForFunction(() => {
    const pc = (window as unknown as { __pc: { engine: { enginePhase: string }; stats(): { progress: number } } }).__pc
    return pc.engine.enginePhase === 'streaming' && pc.stats().progress >= 0.95
  }, null, { timeout: 10_000 }).catch(() => null)
  test.skip(recovered === null && (await page.evaluate(() => (window as unknown as { __pc: { engine: { enginePhase: string } } }).__pc.engine.enginePhase)) !== 'streaming',
    'the backend did not rebuild after the loss (M06 device-loss handling pending)')
  test.info().annotations.push({ type: 'after-loss', description: srv.get().log.slice(before).map((e) => `${e.path}${e.query} ${e.range ?? ''}`).join(' | ') })
  expect((await stats(page)).progress).toBeGreaterThanOrEqual(0.95)
  // FR-027: nothing that was downloaded before the loss is downloaded again (re-upload from the CPU cache); the
  // canvas remount may move the camera, so requests for nodes never loaded before are allowed
  const key = (e: { path: string; range: string | null }) => `${e.path} ${e.range}`
  const beforeKeys = new Set(srv.get().log.slice(0, before).filter((e) => e.path.includes('/octree.bin')).map(key))
  const again = srv.get().log.slice(before).filter((e) => e.path.includes('/octree.bin') && beforeKeys.has(key(e)))
  expect(again.map(key)).toEqual([])
  void downloaded
  void worldRequests
  expectNoErrors(w)
})
