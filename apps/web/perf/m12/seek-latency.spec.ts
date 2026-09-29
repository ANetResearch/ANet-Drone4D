// M12-AC-063 / D1-AC-18 seek part (performance case, ADR-033 protocol only): real backend (supervisor ci profile,
// sim-core + api, on-demand replay-worker) with a synthetic recording (M12_SEEK_N vehicles, M12_SEEK_S seconds); the
// page opens the replay through the timeline store (window.__timeline, test build) and performs 20 random seeks; the
// first backfill frame after each seek (timeline store lastSeekMs, measured at the first samples of the new epoch) is
// <= 500 ms each (blocking) and p95 <= 200 ms (design target, reported). The functional equivalent without a browser
// is tests/recorder/test_replay.py.
import { expect, test } from '@playwright/test'
import { RUN, needTestBuild, startReplayBackend, watch, type ReplayBackend } from './common'

let be: ReplayBackend | null = null
test.beforeAll(async () => {
  be = await startReplayBackend({ n: Number(process.env.M12_SEEK_N ?? 1000), simS: Number(process.env.M12_SEEK_S ?? 600) })
})
test.afterAll(async () => {
  await be?.close()
})

test('@perf 20 seeks: first backfill frame <= 500 ms', async ({ page }) => {
  test.setTimeout(600_000)
  const errors = watch(page)
  await page.goto(`${be!.url}/world/shenzhen`)
  await needTestBuild(page)
  await page.waitForFunction(() => ((window as unknown as { __perf?: { time?: { epoch: number } } }).__perf?.time?.epoch ?? -1) >= 0, null, { timeout: 90_000 })
  const ms = await page.evaluate(async (run) => {
    const tl = (window as unknown as { __timeline: { store: { getState(): Record<string, unknown> }; actions: Record<string, (...a: unknown[]) => unknown> } }).__timeline
    await tl.actions.pause()
    await new Promise((r) => setTimeout(r, 1000))
    const ok = await tl.actions.openReplay(run, 0)
    if (!ok) return { error: 'open failed' }
    const out: number[] = []
    let x = 12345
    for (let i = 0; i < 20; i++) {
      x = (x * 1103515245 + 12345) % 2147483648
      const pb = tl.store.getState().playback as { dataStartS: number; dataEndS: number }
      const t = pb.dataStartS + (x / 2147483648) * (pb.dataEndS - pb.dataStartS)
      const before = tl.store.getState().lastSeekMs
      tl.actions.seek(t)
      const end = performance.now() + 5000
      while (performance.now() < end && tl.store.getState().lastSeekMs === before) await new Promise((r) => setTimeout(r, 20))
      out.push(Number(tl.store.getState().lastSeekMs))
      await new Promise((r) => setTimeout(r, 300))
    }
    await tl.actions.closeReplay()
    return { ms: out }
  }, RUN)
  expect((ms as { error?: string }).error).toBeUndefined()
  const v = (ms as { ms: number[] }).ms.slice().sort((a, b) => a - b)
  test.info().annotations.push({ type: 'perf', description: JSON.stringify({ p50: v[10], p95: v[18], max: v[19] }) })
  expect(v[19]).toBeLessThanOrEqual(500)
  expect(errors).toEqual([])
})
