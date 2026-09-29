// M05-AC-017 (e2e part): ?pcInject=fail:0.05 (test builds) fails 5 % of the node Ranges; __perf.forced records it; the
// camera tour keeps streaming and, once still, every selected node ends up resident (FAILED nodes are requeued after
// 10 s); no page error.
import { expect, test } from '@playwright/test'
import { expectNoErrors, look, openWorld, perf, stats, m05Server, watch } from './common'

const srv = m05Server(6)

test('5 % injected fetch failures heal (M05-AC-017)', async ({ page }) => {
  test.setTimeout(240_000)
  const w = watch(page)
  await openWorld(page, srv.get().url, 'shenzhen', '?pcInject=fail:0.05')
  expect(await perf<{ perfInject?: string }>(page, 'forced')).toMatchObject({ perfInject: 'pcFail:0.05' })
  for (const [e, t] of [[[-300, -500, 250], [0, 0, 20]], [[200, -300, 150], [0, 50, 20]], [[-150, 200, 180], [0, 0, 0]]]) {
    await look(page, e, t)
    await page.waitForTimeout(4000)
  }
  await page.waitForTimeout(12_000)
  const s = await stats(page)
  expect(s.progress).toBeGreaterThanOrEqual(0.95)
  expectNoErrors(w)
})
