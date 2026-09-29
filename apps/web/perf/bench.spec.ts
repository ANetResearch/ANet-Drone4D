// /bench upload (M16-FR-074; PERF-AC-066; AWR-18 §11.5): test build with ?tier=B simulates a real-GPU upload; the page
// POSTs awr.perf.report.v1 to /api/sys/perf-report (201) and the file lands in runs/perf-reports/.
import { expect, test } from './fixtures/perf'

test('bench page uploads a report', async ({ perfPage }) => {
  test.setTimeout(240_000)
  const page = perfPage.page
  const upload = page.waitForResponse((r) => r.url().includes('/api/sys/perf-report') && r.request().method() === 'POST', { timeout: 200_000 })
  await perfPage.open('/bench?tier=B&city=shenzhen')
  const start = page.getByRole('button', { name: /开始|Start/ })
  if (await start.count()) await start.first().click()
  const r = await upload
  expect(r.status()).toBe(201)
  const body = r.request().postDataJSON() as { schema: string; gate: string; kind: string }
  expect(body.schema).toBe('awr.perf.report.v1')
  expect(body.gate).toBe('bench')
})
