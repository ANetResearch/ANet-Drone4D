// Walking-skeleton test server (D1-AC-34 frontend part; used by perf/skeleton.spec.ts). Serves:
//   * apps/web/dist with the SPA fallback and the cross-origin isolation headers of AWR-17 §5.4;
//   * /worlds/<id>/** from the repository worlds/ directory with single-range Range support (AWR-17 §5.3; ?v= ignored);
//   * POST /api/world/<id>/query {op: "ray_hit"}: a DSM ray march over geometry/terrain/dsm_2m (stand-in for M04 until
//     the api process serves R07; request and response shapes of AWR-17 §4.3.2);
//   * everything else under /api (including the /api/rt WebSocket upgrade) is proxied to `apiProxy` when given
//     (tools/fake/fake_gw.py, or a real api with localRayHit: false), otherwise answered 404 so the client runs on
//     FakeSource.
// startBackend (SK-E2E, D1-AC-34 full chain) returns the real backend instead: SKELETON_API=host:port of a running
// `make run`, or a supervisor started here (`--profile ci --only sim-core,api` on free ports, temporary runs dir), whose
// api serves apps/web/dist, /worlds and /api itself (no proxy in between).
import { spawn, type ChildProcess } from 'node:child_process'
import { createReadStream, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import { createServer, request, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { connect, createServer as createTcpServer } from 'node:net'
import { tmpdir } from 'node:os'
import { extname, join, normalize, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../../..')
// AWR_PERF_DIST (or M11_DIST) points at a private test build during parallel development (M11-net request item 2)
const DIST = resolve(process.env.AWR_PERF_DIST ?? process.env.M11_DIST ?? resolve(import.meta.dirname, '../dist'))
const WORLDS = join(ROOT, 'worlds')
const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/geo+json',
  '.bin': 'application/octet-stream', '.f32': 'application/octet-stream', '.u8': 'application/octet-stream', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.wasm': 'application/wasm',
}
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

interface Dsm { w: number; h: number; cell: number; ox: number; oy: number; z: Float32Array }
const dsmCache = new Map<string, Dsm>()
function dsmOf(world: string): Dsm {
  let d = dsmCache.get(world)
  if (d) return d
  const dir = join(WORLDS, world, 'geometry/terrain')
  const j = JSON.parse(readFileSync(join(dir, 'dsm_2m.json'), 'utf8')) as { width: number; height: number; cellM: number; originXY: [number, number]; href: string }
  const b = readFileSync(join(dir, j.href))
  d = { w: j.width, h: j.height, cell: j.cellM, ox: j.originXY[0], oy: j.originXY[1], z: new Float32Array(b.buffer, b.byteOffset, b.byteLength / 4) }
  dsmCache.set(world, d)
  return d
}
function dsmAt(d: Dsm, x: number, y: number): number {
  const c = Math.floor((x - d.ox) / d.cell)
  const r = Math.floor((y - d.oy) / d.cell) // rowOrder south-to-north
  if (c < 0 || r < 0 || c >= d.w || r >= d.h) return Number.NaN
  return d.z[r * d.w + c]
}
/** first crossing of the ray below the DSM surface: 0.5 m march then bisection */
export function rayHit(world: string, o: number[], dir: number[], maxRange = 5000): { hit: boolean; point_enu_m: number[] | null; dist_m: number | null; surface: string } {
  const d = dsmOf(world)
  const at = (t: number): number[] => [o[0] + dir[0] * t, o[1] + dir[1] * t, o[2] + dir[2] * t]
  const below = (t: number): boolean => {
    const p = at(t)
    const z = dsmAt(d, p[0], p[1])
    return Number.isFinite(z) && p[2] <= z
  }
  const step = 0.5
  let prev = 0
  for (let t = step; t <= maxRange; t += step) {
    if (below(t)) {
      let lo = prev
      let hi = t
      for (let i = 0; i < 24; i++) {
        const m = (lo + hi) / 2
        if (below(m)) hi = m
        else lo = m
      }
      const p = at(hi)
      return { hit: true, point_enu_m: p.map((v) => Math.round(v * 1000) / 1000), dist_m: Math.round(hi * 1000) / 1000, surface: 'dsm' }
    }
    prev = t
  }
  return { hit: false, point_enu_m: null, dist_m: null, surface: 'none' }
}

function sendFile(req: IncomingMessage, res: ServerResponse, file: string, cache: string): void {
  const st = statSync(file)
  const type = MIME[extname(file)] ?? 'application/octet-stream'
  const range = req.headers.range
  const base = { ...COI, 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Cache-Control': cache }
  if (range) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(range)
    if (!m) {
      res.writeHead(416, { ...base, 'Content-Range': `bytes */${st.size}` }).end()
      return
    }
    let a = m[1] === '' ? st.size - Number(m[2]) : Number(m[1])
    let b = m[1] === '' ? st.size - 1 : m[2] === '' ? st.size - 1 : Number(m[2])
    a = Math.max(0, a)
    b = Math.min(st.size - 1, b)
    if (a > b) {
      res.writeHead(416, { ...base, 'Content-Range': `bytes */${st.size}` }).end()
      return
    }
    res.writeHead(206, { ...base, 'Content-Range': `bytes ${a}-${b}/${st.size}`, 'Content-Length': String(b - a + 1) })
    createReadStream(file, { start: a, end: b }).pipe(res)
    return
  }
  res.writeHead(200, { ...base, 'Content-Length': String(st.size) })
  createReadStream(file).pipe(res)
}

export interface SkeletonServer { server: Server; url: string; stats: { rayHits: number; ranges: number; worldBytes: number }; close(): Promise<void> }

export function startSkeletonServer(port: number, o: { apiProxy?: { host: string; port: number }; localRayHit?: boolean } = {}): Promise<SkeletonServer> {
  const stats = { rayHits: 0, ranges: 0, worldBytes: 0 }
  const server = createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x')
    const path = decodeURIComponent(u.pathname)
    if (path.startsWith('/worlds/')) {
      const file = normalize(join(WORLDS, path.slice('/worlds/'.length)))
      if (!file.startsWith(WORLDS) || !existsSync(file) || !statSync(file).isFile()) {
        res.writeHead(404, COI).end()
        return
      }
      if (req.headers.range) stats.ranges++
      stats.worldBytes += statSync(file).size
      sendFile(req, res, file, path.endsWith('world.json') ? 'no-cache' : 'public, max-age=31536000, immutable')
      return
    }
    const q = /^\/api\/world\/([a-z0-9-]{1,63})\/query$/.exec(path)
    if (q && req.method === 'POST' && o.localRayHit !== false) {
      let body = ''
      req.on('data', (c: Buffer) => (body += c.toString()))
      req.on('end', () => {
        try {
          const j = JSON.parse(body) as { op: string; origin_enu_m: number[]; dir: number[]; max_range_m?: number }
          if (j.op !== 'ray_hit') throw new Error('only ray_hit')
          stats.rayHits++
          const r = rayHit(q[1], j.origin_enu_m, j.dir, j.max_range_m ?? 5000)
          res.writeHead(200, { ...COI, 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'AWR-API-Version': '1' })
          res.end(JSON.stringify({ ...r, content_version: 'skeleton', source: 'dsm_2m' }))
        } catch (e) {
          res.writeHead(422, { ...COI, 'Content-Type': 'application/problem+json' })
          res.end(JSON.stringify({ code: 110, name: 'PARAM_OUT_OF_RANGE', message: String(e) }))
        }
      })
      return
    }
    if (path.startsWith('/api/')) {
      if (!o.apiProxy) {
        res.writeHead(404, { ...COI, 'Content-Type': 'application/problem+json' }).end(JSON.stringify({ code: 305, name: 'NOT_FOUND', message: 'no api in the skeleton server' }))
        return
      }
      const p = request({ host: o.apiProxy.host, port: o.apiProxy.port, path: req.url, method: req.method, headers: req.headers }, (pr) => {
        res.writeHead(pr.statusCode ?? 502, pr.headers)
        pr.pipe(res)
      })
      p.on('error', () => res.writeHead(502, COI).end())
      req.pipe(p)
      return
    }
    let file = normalize(join(DIST, path))
    if (!file.startsWith(DIST) || !existsSync(file) || !statSync(file).isFile()) file = join(DIST, 'index.html')
    sendFile(req, res, file, file.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable')
  })
  // WebSocket upgrade: raw TCP pass-through to the proxied api (fake_gw)
  server.on('upgrade', (req, socket, head) => {
    if (!o.apiProxy || !(req.url ?? '').startsWith('/api/rt')) {
      socket.destroy()
      return
    }
    const up = connect(o.apiProxy.port, o.apiProxy.host, () => {
      const lines = [`${req.method} ${req.url} HTTP/1.1`, ...Object.entries(req.headers).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`), '', '']
      up.write(lines.join('\r\n'))
      if (head.length) up.write(head)
      up.pipe(socket)
      socket.pipe(up)
    })
    up.on('error', () => socket.destroy())
    socket.on('error', () => up.destroy())
  })
  // port 0 picks a free port (FX-GW: the fixed 4193 + 10k collides with the vite preview of offset k + 2, ADR-055)
  return new Promise((ok, fail) => {
    server.once('error', fail)
    server.listen(port, '127.0.0.1', () => ok({
      server, url: `http://127.0.0.1:${(server.address() as { port: number } | null)?.port ?? port}`, stats,
      close: () => new Promise((done) => {
        server.closeAllConnections()
        server.close(() => done())
      }),
    }))
  })
}

// ------------------------------------------------------------ real backend (SK-E2E)
export interface Backend {
  /** http://host:port of the api (serves the SPA, /worlds and /api) */
  url: string
  /** false when SKELETON_API pointed at an already running `make run` */
  owned: boolean
  /** supervisor stdout and stderr lines (owned backends) */
  log: string[]
  close(): Promise<void>
}

export function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createTcpServer()
    s.once('error', fail)
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      const port = typeof a === 'object' && a ? a.port : 0
      s.close(() => ok(port))
    })
  })
}

async function waitReady(url: string, timeoutMs: number, alive: () => boolean): Promise<void> {
  const end = Date.now() + timeoutMs
  let last = ''
  while (Date.now() < end && alive()) {
    try {
      const r = await fetch(`${url}/api/health/ready`)
      if (r.ok) return
      last = `${r.status} ${await r.text()}`
    } catch (e) {
      last = String(e)
    }
    await new Promise((d) => setTimeout(d, 300))
  }
  throw new Error(`api not ready at ${url}: ${last}`)
}

/**
 * The real backend for the full chain: SKELETON_API=host:port (for example `make run` on 127.0.0.1:8000), or a
 * supervisor started here with the ci profile, only sim-core and api, on free api and bus ports and a temporary runs
 * directory (removed on close). Needs worlds/shenzhen and a built apps/web/dist.
 */
export async function startBackend(api = process.env.SKELETON_API, extraEnv: Record<string, string> = {}): Promise<Backend> {
  if (api) {
    const url = api.startsWith('http') ? api.replace(/\/$/, '') : `http://${api}`
    await waitReady(url, 60_000, () => true)
    return { url, owned: false, log: [], close: async () => {} }
  }
  const py = join(ROOT, '.venv/bin/python')
  if (!existsSync(py)) throw new Error(`${py} not found (make setup)`)
  const port = await freePort()
  const busPort = await freePort()
  const runs = mkdtempSync(join(tmpdir(), 'awr-skeleton-runs-'))
  const env: Record<string, string | undefined> = { ...process.env, AWR_RUNS_DIR: runs, PYTHONUNBUFFERED: '1', ...extraEnv }
  delete env.AWR_SUPERVISOR_PID
  const args = ['-m', 'awr.runtime.supervisor', '--profile', 'ci', '--only', 'sim-core,api',
    '--set', 'net.port_offset=0', '--set', `net.port=${port}`, '--set', `bus.rendezvous=tcp/127.0.0.1:${busPort}`,
    '--set', 'run.keep_run_dir=false']
  const p: ChildProcess = spawn(py, args, { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
  const log: string[] = []
  let exited = false
  p.on('exit', () => {
    exited = true
  })
  const url = `http://127.0.0.1:${port}`
  const stop = async (): Promise<void> => {
    if (!exited && p.pid) {
      try {
        process.kill(-p.pid, 'SIGTERM')
      } catch {
        // already gone
      }
      const end = Date.now() + 30_000
      while (!exited && Date.now() < end) await new Promise((d) => setTimeout(d, 100))
      if (!exited) {
        try {
          process.kill(-p.pid, 'SIGKILL')
        } catch {
          // already gone
        }
      }
    }
    rmSync(runs, { recursive: true, force: true })
  }
  try {
    await new Promise<void>((ok, fail) => {
      const t = setTimeout(() => fail(new Error(`supervisor did not print READY within 90 s:\n${log.slice(-40).join('\n')}`)), 90_000)
      const onLine = (d: Buffer): void => {
        for (const line of d.toString().split('\n')) {
          if (!line) continue
          log.push(line)
          if (line.startsWith('READY')) {
            clearTimeout(t)
            ok()
          }
        }
      }
      p.stdout!.on('data', onLine)
      p.stderr!.on('data', onLine)
      p.on('exit', (code) => {
        clearTimeout(t)
        fail(new Error(`supervisor exited with ${code}:\n${log.slice(-40).join('\n')}`))
      })
    })
    await waitReady(url, 60_000, () => !exited)
  } catch (e) {
    await stop()
    throw e
  }
  return { url, owned: true, log, close: stop }
}
