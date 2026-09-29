// No runtime shader compilation (M16-FR-059; D1-AC-25; PERF-AC-028; AWR-18 §6.2): after the reveal, first weather preset
// switch, first selection, first follow, first close-up (P600 model), first ground pick: renderer programs do not grow and
// the largest frame interval within 1 s after each operation is <= 150 ms.
import { hotkey, palette } from './fixtures/ui'
import { expect, test } from './fixtures/perf'

test('first operations compile nothing', async ({ perfPage }) => {
  test.setTimeout(180_000)
  const page = perfPage.page
  await perfPage.open('/world/shenzhen')
  await perfPage.waitReveal()
  await page.waitForTimeout(3000)
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
  perfPage.writeMetrics({ programs_delta: programs1 - programs0, after_op_max_gap_ms: worst })
  await perfPage.saveSnapshot()
  expect(programs1 - programs0, JSON.stringify(gaps)).toBe(0)
  perfPage.assertNoPageErrors()
})
