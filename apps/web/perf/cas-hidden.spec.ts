// CAS freeze while the page is hidden (D1-AC-04 sub-item "页面隐藏期间不评估"; AWR-18 §4.4 freeze row, PERF-AC-004;
// M05-FR-040). Headless Chromium 151 cannot hide a page (window minimise, a second tab brought to front, focus emulation
// and Page.setWebLifecycleState all keep visibilityState 'visible', ACC-4 4.2), so an init script makes
// document.visibilityState / document.hidden overridable and the page is switched to 'hidden' for 6 s during a flight60
// scene=pc run: the product path PointCloudEngine.sampleFrame -> casFreezeMask(hidden) must keep __perf.cas.evals
// constant while frozenFrames grow, and evaluate again after the page is visible. A really hidden tab stops rAF; its
// first frame back has dt > casMaxDtMs and is discarded by CascadeController.sample (cas.unit.test.ts). FX2-R5, ADR-076.
import { flightQuery } from './fixtures/flight'
import { caseParams, expect, test } from './fixtures/perf'

interface CasCounters { evals: number; frozenFrames: number; frames: number; vis: string }

test('no CAS evaluation while the page is hidden', async ({ perfPage }) => {
  test.setTimeout(180_000)
  const page = perfPage.page
  const p = { city: 'shenzhen', scene: 'pc' as const, ...caseParams() }
  await page.addInitScript(() => {
    const w = window as unknown as { __vis: string; __setVis(v: string): void }
    w.__vis = 'visible'
    Object.defineProperty(Document.prototype, 'visibilityState', { configurable: true, get: () => w.__vis })
    Object.defineProperty(Document.prototype, 'hidden', { configurable: true, get: () => w.__vis === 'hidden' })
    w.__setVis = (v: string) => {
      w.__vis = v
      document.dispatchEvent(new Event('visibilitychange'))
    }
  })
  const city = String(p.city)
  perfPage.assertBinding(city)
  await perfPage.open(`/world/${city}?${flightQuery(p)}`)
  await perfPage.waitReveal()
  // past the 30 frozen frames after the reveal, the CAS evaluates every 250 ms
  await page.waitForTimeout(4000)
  const read = (): Promise<CasCounters> => page.evaluate(() => {
    const w = window as unknown as { __perf: { cas: { evals: number; frozenFrames: number }; frame: { count: number } } }
    return { evals: w.__perf.cas.evals, frozenFrames: w.__perf.cas.frozenFrames, frames: w.__perf.frame.count, vis: document.visibilityState }
  })
  const visible0 = await read()
  expect(visible0.evals, 'the CAS evaluates while visible').toBeGreaterThan(0)
  await page.evaluate(() => (window as unknown as { __setVis(v: string): void }).__setVis('hidden'))
  const atHide = await read()
  await page.waitForTimeout(6000)
  const endHidden = await read()
  await page.evaluate(() => (window as unknown as { __setVis(v: string): void }).__setVis('visible'))
  await page.waitForTimeout(3000)
  const after = await read()
  const hidden = { frames: endHidden.frames - atHide.frames, evals: endHidden.evals - atHide.evals, frozen: endHidden.frozenFrames - atHide.frozenFrames }
  perfPage.writeMetrics({ hidden_frames: hidden.frames, hidden_cas_evals: hidden.evals, hidden_frozen_frames: hidden.frozen,
    evals_after_show_3s: after.evals - endHidden.evals })
  expect(endHidden.vis).toBe('hidden')
  expect(hidden.frames, 'headless keeps presenting frames while emulated hidden').toBeGreaterThan(30)
  expect(hidden.evals, JSON.stringify(hidden)).toBe(0)
  expect(hidden.frozen).toBeGreaterThanOrEqual(hidden.frames - 2)
  expect(after.evals - endHidden.evals, 'evaluations resume once visible').toBeGreaterThan(0)
  perfPage.assertNoPageErrors()
})
