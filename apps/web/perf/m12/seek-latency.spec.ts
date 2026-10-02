// M12-AC-063 / D1-AC-18 seek part (performance case, ADR-033 protocol only): real backend (supervisor perf profile with PR-6 pinning,
// sim-core + api, on-demand replay-worker) with a synthetic recording (M12_SEEK_N vehicles, M12_SEEK_S seconds); the
// page opens the replay through the timeline store (window.__timeline, test build) and performs 20 random seeks; the
// first backfill frame after each seek (timeline store lastSeekMs, measured at the first samples of the new epoch) is
// <= 500 ms each (blocking) and p95 <= 200 ms (design target, reported). The functional equivalent without a browser
// is tests/recorder/test_replay.py.
import { expect, test } from '@playwright/test'
import { RUN, needTestBuild, perfWindow, startReplayBackend, viewerAuth, watch, type ReplayBackend } from './common'

let be: ReplayBackend | null = null
test.beforeAll(async () => {
  be = await startReplayBackend({ n: Number(process.env.M12_SEEK_N ?? 1000), simS: Number(process.env.M12_SEEK_S ?? 600), profile: 'perf' })
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
  const w0 = performance.now()
  const ms = await page.evaluate(async (run) => {
    const tl = (window as unknown as { __timeline: { store: { getState(): Record<string, unknown> }; actions: Record<string, (...a: unknown[]) => unknown> } }).__timeline
    await tl.actions.pause()
    await new Promise((r) => setTimeout(r, 1000))
    const ok = await tl.actions.openReplay(run, 0)
    if (!ok) return { error: 'open failed' }
    const out: number[] = []
    // per seek, for diagnosis: when the playbackState reply settled the pending seek (polled every 20 ms) and the target
    const reply: number[] = []
    const targets: number[] = []
    let x = 12345
    for (let i = 0; i < 20; i++) {
      x = (x * 1103515245 + 12345) % 2147483648
      const pb = tl.store.getState().playback as { dataStartS: number; dataEndS: number }
      const t = pb.dataStartS + (x / 2147483648) * (pb.dataEndS - pb.dataStartS)
      const before = tl.store.getState().lastSeekMs
      const t0 = performance.now()
      tl.actions.seek(t)
      let replied = Number.NaN
      const end = performance.now() + 5000
      while (performance.now() < end && tl.store.getState().lastSeekMs === before) {
        if (Number.isNaN(replied) && tl.store.getState().pending === null) replied = performance.now() - t0
        await new Promise((r) => setTimeout(r, 20))
      }
      out.push(Number(tl.store.getState().lastSeekMs))
      reply.push(Math.round(Number.isNaN(replied) ? -1 : replied))
      targets.push(Math.round(t))
      await new Promise((r) => setTimeout(r, 300))
    }
    await tl.actions.closeReplay()
    return { ms: out, reply, targets }
  }, RUN)
  expect((ms as { error?: string }).error).toBeUndefined()
  const m = ms as { ms: number[]; reply: number[]; targets: number[] }
  const v = m.ms.slice().sort((a, b) => a - b)
  test.info().annotations.push({ type: 'perf', description: JSON.stringify({ p50: v[10], p95: v[18], max: v[19] }) })
  test.info().annotations.push({ type: 'perf-detail', description: JSON.stringify({ ms: m.ms.map(Math.round), reply: m.reply, targets: m.targets }) })
  // api event-loop lag over the seeks (perf/server R60): tells a blocked api apart from a slow replay-worker or page
  const win = await perfWindow(be!.url, await viewerAuth(be!.url), (performance.now() - w0) / 1000)
  const lag = win.fields?.['api.loop_lag_p99_ms']
  // eslint-disable-next-line no-console
  console.log(`seek-latency detail ${JSON.stringify({ ms: m.ms.map(Math.round), reply: m.reply, targets: m.targets, apiLoopLagP99: lag?.p99, apiLoopLagMax: lag?.max })}`)
  expect(v[19]).toBeLessThanOrEqual(500)
  expect(errors).toEqual([])
})
