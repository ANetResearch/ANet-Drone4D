// M12-AC-043 / AC-065 / D1-AC-18 20x part (performance case, ADR-033 protocol only): the synthetic recording played at
// the highest allowed speed (20x when speed_max allows) for the whole segment; the page's interpolation HOLD ratio
// (__perf.time.holdRatio, 1 s windows) stays below 1 % and the replay-worker reports BUFFERING below 1 % of the played
// time (playbackState status samples). api CPU (<= 0.35 core) and tick data age come from perf/server and the
// harness /proc sampling, not from this spec.
import { expect, test } from '@playwright/test'
import { RUN, needTestBuild, startReplayBackend, watch, type ReplayBackend } from './common'

let be: ReplayBackend | null = null
test.beforeAll(async () => {
  be = await startReplayBackend({ n: Number(process.env.M12_R20_N ?? 1000), simS: Number(process.env.M12_R20_S ?? 600) })
})
test.afterAll(async () => {
  await be?.close()
})

test('@perf 20x replay: HOLD < 1 %, BUFFERING < 1 %', async ({ page }) => {
  test.setTimeout(900_000)
  const errors = watch(page)
  await page.goto(`${be!.url}/world/shenzhen`)
  await needTestBuild(page)
  await page.waitForFunction(() => ((window as unknown as { __perf?: { time?: { epoch: number } } }).__perf?.time?.epoch ?? -1) >= 0, null, { timeout: 90_000 })
  const r = await page.evaluate(async (run) => {
    const w = window as unknown as {
      __timeline: { store: { getState(): Record<string, unknown> }; actions: Record<string, (...a: unknown[]) => unknown> }
      __perf: { time: { holdRatio: number; state4: number } }
    }
    const tl = w.__timeline
    await tl.actions.pause()
    await new Promise((res) => setTimeout(res, 1000))
    if (!(await tl.actions.openReplay(run, 0))) return { error: 'open failed' }
    const pb = tl.store.getState().playback as { speedMax: number; dataEndS: number; dataStartS: number }
    tl.actions.setRate(Math.min(20, pb.speedMax))
    await new Promise((res) => setTimeout(res, 500))
    tl.actions.play()
    const hold: number[] = []
    let buffering = 0
    let samples = 0
    const t0 = performance.now()
    const budget = ((pb.dataEndS - pb.dataStartS) / Math.min(20, pb.speedMax) + 10) * 1000
    while (performance.now() - t0 < budget) {
      await new Promise((res) => setTimeout(res, 1000))
      hold.push(w.__perf.time.holdRatio)
      samples++
      if (w.__perf.time.state4 === 4) buffering++
      if ((tl.store.getState().playback as { status: string }).status === 'ended') break
    }
    await tl.actions.closeReplay()
    return { hold, bufferingRatio: buffering / Math.max(1, samples) }
  }, RUN)
  expect((r as { error?: string }).error).toBeUndefined()
  const res = r as { hold: number[]; bufferingRatio: number }
  const meanHold = res.hold.reduce((a, b) => a + b, 0) / Math.max(1, res.hold.length)
  test.info().annotations.push({ type: 'perf', description: JSON.stringify({ meanHold, bufferingRatio: res.bufferingRatio }) })
  expect(meanHold).toBeLessThan(0.01)
  expect(res.bufferingRatio).toBeLessThan(0.01)
  expect(errors).toEqual([])
})
