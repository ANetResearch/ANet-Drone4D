// M12-AC-043 / AC-065 / D1-AC-18 20x part (performance case, ADR-033 protocol only): the synthetic recording played at
// the highest allowed speed (20x when speed_max allows) for the whole segment; the page's interpolation HOLD ratio
// (__perf.time.holdRatio, 1 s windows) stays below 1 % and the replay-worker reports BUFFERING below 1 % of the played
// time (playbackState status samples). The spec starts its own backend (harness backend kind 'tool'), so the harness has
// no pids to sample: the spec itself reads /proc/<api pid>/stat around the playback (18 §9.4 item 3) and the R60 window
// of perf/server for the tick data age, and reports both as annotations (api <= 0.35 core, tick age p99 <= 15 ms; the CPU
// figure is judged by the reader under the PR-5 load rule, so it is reported, not asserted; FX2-R2-gateway).
import { expect, test } from '@playwright/test'
import { RUN, needTestBuild, perfWindow, procTicks, startReplayBackend, viewerAuth, watch, type ReplayBackend } from './common'

let be: ReplayBackend | null = null
test.beforeAll(async () => {
  be = await startReplayBackend({ n: Number(process.env.M12_R20_N ?? 1000), simS: Number(process.env.M12_R20_S ?? 600), profile: 'perf' })
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
  const auth = await viewerAuth(be!.url)
  const procs = (await (await fetch(`${be!.url}/api/sys/procs`, { headers: auth })).json()) as { items?: { name: string; pid?: number }[] }
  const apiPid = procs.items?.find((x) => x.name === 'api')?.pid ?? 0
  const c0 = procTicks(apiPid)
  const w0 = performance.now()
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
    // per 1 s window, for diagnosis: page frames, interpolation delay and arrival rate seen by the time engine
    const diag: number[][] = []
    let buffering = 0
    let samples = 0
    const t0 = performance.now()
    const budget = ((pb.dataEndS - pb.dataStartS) / Math.min(20, pb.speedMax) + 10) * 1000
    let f0 = (w.__perf.time as unknown as { frames: number }).frames
    while (performance.now() - t0 < budget) {
      await new Promise((res) => setTimeout(res, 1000))
      const pt = w.__perf.time as unknown as { holdRatio: number; state4: number; frames: number; hzEff: number; dWallMs: number;
        dGlobalMs: number; maxAgeMs: number; rate: number; simNowS: number; tRenderS: number }
      hold.push(pt.holdRatio)
      diag.push([pt.frames - f0, Math.round(pt.hzEff * 10) / 10, Math.round(pt.dWallMs), Math.round(pt.dGlobalMs), Math.round(pt.maxAgeMs),
        pt.state4, Math.round((pt.simNowS - pt.tRenderS) * 1000)])
      f0 = pt.frames
      samples++
      if (pt.state4 === 4) buffering++
      if ((tl.store.getState().playback as { status: string }).status === 'ended') break
    }
    await tl.actions.closeReplay()
    return { hold, diag, bufferingRatio: buffering / Math.max(1, samples) }
  }, RUN)
  const wallS = (performance.now() - w0) / 1000
  const c1 = procTicks(apiPid)
  const apiCpuCore = c0 !== null && c1 !== null && wallS > 0 ? (c1 - c0) / 100 / wallS : null
  const win = await perfWindow(be!.url, auth, wallS)
  const tickAgeP99 = win.fields?.['api.tick_age_p99_ms']?.p99 ?? null
  expect((r as { error?: string }).error).toBeUndefined()
  const res = r as { hold: number[]; diag: number[][]; bufferingRatio: number }
  const meanHold = res.hold.reduce((a, b) => a + b, 0) / Math.max(1, res.hold.length)
  test.info().annotations.push({ type: 'perf', description: JSON.stringify({ meanHold, bufferingRatio: res.bufferingRatio, apiCpuCore, tickAgeP99, wallS }) })
  // per 1 s window, for diagnosis (where in the playback the HOLD happens)
  test.info().annotations.push({ type: 'perf-detail', description: JSON.stringify({ hold: res.hold.map((h) => Math.round(h * 1000) / 1000) }) })
  // eslint-disable-next-line no-console
  console.log(`replay20x detail ${JSON.stringify({ meanHold, apiCpuCore, tickAgeP99, wallS, hold: res.hold.map((h) => Math.round(h * 1000) / 1000) })}`)
  // eslint-disable-next-line no-console
  console.log(`replay20x windows [frames, hzEff, dWallMs, dGlobalMs, maxAgeMs, state4, simNow-tRender ms] ${JSON.stringify(res.diag)}`)
  expect(meanHold).toBeLessThan(0.01)
  expect(res.bufferingRatio).toBeLessThan(0.01)
  expect(errors).toEqual([])
})
