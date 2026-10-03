// No runtime shader compilation (M16-FR-059; D1-AC-25; PERF-AC-028; AWR-18 §6.2): after the reveal, first weather preset
// switch, first selection, first follow, first close-up (P600 model), first ground pick: renderer programs do not grow and
// the largest frame interval within 1 s after each operation is <= 150 ms. The operations start once the PerfGovernor
// walk after the reveal is over (FX2-R5, ADR-076; was a fixed 3 s, so its steps fell into the first windows).
import { hotkey, palette } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

test('first operations compile nothing', async ({ perfPage }) => {
  test.setTimeout(180_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  // first-use costs do not depend on when the operation comes; the governor's walk after the reveal (Tier S: about 5 s,
  // its frames judged by D1-AC-03b/04) must not fall into the 1 s windows (FX2-R5, ADR-076)
  const settledMs = await perfPage.waitGovernorSettled()
  const programs0 = await perfPage.eval((p) => p.gpu.programs)
  const gaps: Record<string, number> = {}
  const ops: [string, () => Promise<void>][] = [
    ['preset', () => palette(page, '切换天气')],
    ['select', () => hotkey(page, 'Period')],
    ['follow', () => hotkey(page, 'KeyL')],
    ['closeup', () => hotkey(page, 'KeyF')],
    ['pick', async () => { await page.mouse.click(640, 520) }],
  ]
  for (const [name, op] of ops) {
    const g = perfPage.maxGap(1000)
    await op().catch(() => {})
    gaps[name] = await g
    await page.waitForTimeout(1500)
    await page.keyboard.press('Escape')
  }
  const programs1 = await perfPage.eval((p) => p.gpu.programs)
  const worst = Math.max(...Object.values(gaps))
  // per operation as well (ACC-4 4.5: only the maximum was written, so a failing run did not say which operation it was)
  const perOp = Object.fromEntries(Object.entries(gaps).map(([k, v]) => [`after_op_gap_${k}_ms`, Math.round(v * 10) / 10]))
  perfPage.writeMetrics({ programs_delta: programs1 - programs0, after_op_max_gap_ms: worst, ...perOp, settled_after_reveal_ms: Math.round(settledMs) })
  await perfPage.saveSnapshot()
  expect(programs1 - programs0, JSON.stringify(gaps)).toBe(0)
  perfPage.assertNoPageErrors()
})
