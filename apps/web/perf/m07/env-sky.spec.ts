// env-sky (WP-09; D2-AC-15 client part; ADR-102; AWR-17 §17.6.9; 14 §20.5.4): a keyframe with the simulation calendar
// and a clock sun drives the scene sun: the EnvStore sky follows the keyframe (band, calendar instant), the direct light
// goes to zero at night (EnvParams.sunVis) and comes back by day with the sun vector of the keyframe's az/el; the D2
// fields only change uniform values, so the renderer programs do not grow (no shader change) and the page has no errors.
import { expect, test } from '@playwright/test'
import { m07Server, openEnv, pageErrors, programs } from './common'

const srv = m07Server(5)

interface Sky { mode: string; d2: boolean; band: string; azDeg: number; elDeg: number; sunVis: number; sunDirEnu: number[]; version: number; tCalUnixMs: number }
type Env = { __env: { injectSky(o: object): number; sky(): Sky } }

test('clock sun from the D2 keyframe: band, direct light at night, no new programs', async ({ page }) => {
  await openEnv(page, srv.get().url, '&tier=S')
  await page.waitForTimeout(1500)
  const p0 = await programs(page)
  const d1 = await page.evaluate(() => (window as unknown as Env).__env.sky())
  expect(d1.mode).toBe('fixed')
  expect(d1.d2).toBe(false)
  expect(d1.sunVis).toBeGreaterThan(0)
  const night = { azDeg: 300, elDeg: -40, band: 'night', startUtc: '2026-10-05T14:00:00Z', tz: 'Asia/Shanghai', tCalUnixNs: '1791216000000000000' }
  const v1 = await page.evaluate((o) => (window as unknown as Env).__env.injectSky(o), night)
  expect(v1).toBeGreaterThan(0)
  await page.waitForFunction((v) => {
    const s = (window as unknown as Env).__env.sky()
    return s.version >= v && s.sunVis === 0
  }, v1, { timeout: 60_000 })
  const n = await page.evaluate(() => (window as unknown as Env).__env.sky())
  expect(n.mode).toBe('clock')
  expect(n.d2).toBe(true)
  expect(n.band).toBe('night')
  expect(n.elDeg).toBe(-40)
  expect(Number.isFinite(n.tCalUnixMs)).toBe(true)
  expect(n.sunDirEnu[2]).toBeLessThan(0)
  const day = { ...night, azDeg: 150, elDeg: 45, band: 'day', tCalUnixNs: '1791165600000000000', acoustic: 'urban' }
  const v2 = await page.evaluate((o) => (window as unknown as Env).__env.injectSky(o), day)
  await page.waitForFunction((v) => {
    const s = (window as unknown as Env).__env.sky()
    return s.version >= v && s.sunVis > 0
  }, v2, { timeout: 60_000 })
  const d = await page.evaluate(() => (window as unknown as Env).__env.sky())
  expect(d.band).toBe('day')
  const r = Math.PI / 180
  const want = [Math.sin(150 * r) * Math.cos(45 * r), Math.cos(150 * r) * Math.cos(45 * r), Math.sin(45 * r)]
  d.sunDirEnu.forEach((x, i) => expect(Math.abs(x - want[i])).toBeLessThan(1e-6))
  await page.waitForTimeout(500)
  expect(await programs(page)).toBe(p0)
  expect(pageErrors(page)).toEqual([])
})
