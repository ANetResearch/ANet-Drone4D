// Image quality sampling (M16-FR-059; D1-AC-05; AWR-18 §4.5): test build, scene=pc with ?quality=1; 12 coverage masks in
// __perf.quality.samples are compared with the full reference render of M05 (apps/web/dev/oracles/fullref.html,
// window.__fullref.render(poses) and holeRate(ref, test)). The hole rate (12-frame mean) goes to metrics.json.
// The oracle is a dev page that the api does not serve (its static routes are /assets, /brand, /bench, /models and the
// SPA), so the spec serves it itself from the test build (AWR_PERF_DIST, which must contain dev/oracles/fullref.html and its
// asset) together with /worlds (ACC-1 round 1). Sample poses are [eye xyz, target xyz, fovY deg, near, far] in world ENU
// (M05 PointCloudEngine.sampleQuality) and are converted to the oracle's Pose objects.
import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'
import { runFlight60 } from './fixtures/flight'
import { ROOT, WEB, caseParams, expect, test } from './fixtures/perf'

const MIME: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json',
  '.bin': 'application/octet-stream' }

/** static server for the oracle: /worlds/** from the worlds directory, everything else from the test build */
async function oracleServer(): Promise<{ url: string; close: () => Promise<void> }> {
  const dist = process.env.AWR_PERF_DIST ?? join(WEB, 'dist')
  const worlds = process.env.AWR_WORLDS_DIR ?? join(ROOT, 'worlds')
  const server = createServer((req, res) => {
    const path = decodeURIComponent(new URL(req.url ?? '/', 'http://x').pathname)
    const [root, rel] = path.startsWith('/worlds/') ? [worlds, path.slice(8)] : [dist, path.slice(1)]
    const f = normalize(join(root, rel))
    if (!f.startsWith(root) || !existsSync(f) || !statSync(f).isFile()) return void res.writeHead(404).end()
    res.writeHead(200, { 'Content-Type': MIME[extname(f)] ?? 'application/octet-stream', 'Content-Length': String(statSync(f).size) })
    createReadStream(f).pipe(res)
  })
  await new Promise<void>((ok) => server.listen(0, '127.0.0.1', ok))
  const a = server.address()
  const url = `http://127.0.0.1:${typeof a === 'object' && a ? a.port : 0}`
  return { url, close: () => new Promise<void>((done) => { server.closeAllConnections(); server.close(() => done()) }) }
}

test('quality samples and hole rate', async ({ perfPage, browser }) => {
  test.setTimeout(420_000)
  const p = { city: 'shenzhen', scene: 'pc' as const, ...caseParams() }
  const snap = await runFlight60(perfPage, p, { query: { quality: 1 } })
  const samples = ((snap.quality as { samples?: { t: number; pose: number[]; mask: number[] | null }[] })?.samples) ?? []
  expect(samples.length, 'twelve quality samples').toBe(12)
  const srv = await oracleServer()
  try {
    const ref = await browser.newPage()
    const r = await ref.goto(`${srv.url}/dev/oracles/fullref.html?world=${p.city}`)
    if (!r || r.status() !== 200) throw new Error('fullref oracle page is not in the test build (dev/oracles/fullref.html); PERF-AC-005 needs it')
    const poses = samples.map((x) => ({ eye: x.pose.slice(0, 3), target: x.pose.slice(3, 6), fovYDeg: x.pose[6], near: x.pose[7], far: x.pose[8] }))
    const holes = await ref.evaluate(async ({ s, poses: ps }) => {
      const w = window as unknown as { __fullref: { render(poses: unknown[]): Promise<Uint8Array[]>; holeRate(a: Uint8Array, b: ArrayLike<number>): number } }
      const refs = await w.__fullref.render(ps)
      return s.map((x, i) => (x.mask ? w.__fullref.holeRate(refs[i], x.mask) : null))
    }, { s: samples, poses })
    const valid = holes.filter((h): h is number => typeof h === 'number')
    expect(valid.length).toBe(12)
    perfPage.writeMetrics({ hole_rate_pct: (100 * valid.reduce((a, b) => a + b, 0)) / valid.length })
  } finally {
    await srv.close()
  }
})
