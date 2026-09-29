// M06 Playwright test server: serves a test build (M06_DIST, default apps/web/dist) with the cross-origin isolation
// headers of AWR-17 §5.4, /worlds/<id>/** from the repository worlds/ (single-range Range), /bench/** from the build,
// and POST /api/world/<id>/query {op: "ray_hit"} with the DSM stand-in of perf/skeleton.server.ts; other /api paths
// answer 404 so the page runs on FakeSource (?source=fake). Parallel development builds into a private directory so
// the shared dist/ of other work packages is never overwritten (M06 report, test section).
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { createServer as createTcpServer } from 'node:net'
import { extname, join, normalize, resolve } from 'node:path'
import { rayHit } from '../skeleton.server'

const ROOT = resolve(import.meta.dirname, '../../../..')
export const DIST = resolve(process.env.M06_DIST ?? join(import.meta.dirname, '../../dist'))
const WORLDS = join(ROOT, 'worlds')
const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.geojson': 'application/geo+json',
  '.bin': 'application/octet-stream', '.f32': 'application/octet-stream', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
}
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

function sendFile(req: IncomingMessage, res: ServerResponse, file: string, cache: string): void {
  const st = statSync(file)
  const base = { ...COI, 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream', 'Accept-Ranges': 'bytes', 'Cache-Control': cache }
  const range = req.headers.range
  const m = range ? /^bytes=(\d*)-(\d*)$/.exec(range) : null
  if (m) {
    const a = m[1] === '' ? st.size - Number(m[2]) : Number(m[1])
    const b = m[1] === '' || m[2] === '' ? st.size - 1 : Math.min(st.size - 1, Number(m[2]))
    res.writeHead(206, { ...base, 'Content-Range': `bytes ${a}-${b}/${st.size}`, 'Content-Length': String(b - a + 1) })
    createReadStream(file, { start: a, end: b }).pipe(res)
    return
  }
  res.writeHead(200, { ...base, 'Content-Length': String(st.size) })
  createReadStream(file).pipe(res)
}

export interface M06Server { server: Server; url: string; stats: { rayHits: number; http4xx: string[] }; close(): Promise<void> }

export async function freePort(): Promise<number> {
  return new Promise((ok) => {
    const s = createTcpServer()
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      const port = typeof a === 'object' && a ? a.port : 0
      s.close(() => ok(port))
    })
  })
}

export async function startM06Server(port?: number, dist: string = DIST): Promise<M06Server> {
  const p = port ?? (await freePort())
  const stats = { rayHits: 0, http4xx: [] as string[] }
  const server = createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x')
    const path = decodeURIComponent(u.pathname)
    if (path.startsWith('/worlds/')) {
      const file = normalize(join(WORLDS, path.slice('/worlds/'.length)))
      if (!file.startsWith(WORLDS) || !existsSync(file) || !statSync(file).isFile()) {
        stats.http4xx.push(`404 ${path}`)
        res.writeHead(404, COI).end()
        return
      }
      sendFile(req, res, file, path.endsWith('world.json') ? 'no-cache' : 'public, max-age=31536000, immutable')
      return
    }
    const q = /^\/api\/world\/([a-z0-9-]{1,63})\/query$/.exec(path)
    if (q && req.method === 'POST') {
      let body = ''
      req.on('data', (c: Buffer) => (body += c.toString()))
      req.on('end', () => {
        const j = JSON.parse(body) as { op: string; origin_enu_m: number[]; dir: number[]; max_range_m?: number }
        stats.rayHits++
        const r = rayHit(q[1], j.origin_enu_m, j.dir, j.max_range_m ?? 5000)
        res.writeHead(200, { ...COI, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
        res.end(JSON.stringify({ ...r, content_version: 'm06', source: 'dsm_2m' }))
      })
      return
    }
    if (path.startsWith('/api/')) {
      res.writeHead(404, { ...COI, 'Content-Type': 'application/problem+json' }).end(JSON.stringify({ code: 305, name: 'NOT_FOUND' }))
      return
    }
    let file = normalize(join(dist, path))
    if (!file.startsWith(dist) || !existsSync(file) || !statSync(file).isFile()) file = join(dist, 'index.html')
    sendFile(req, res, file, file.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable')
  })
  await new Promise<void>((ok) => server.listen(p, '127.0.0.1', () => ok()))
  return {
    server, url: `http://127.0.0.1:${p}`, stats,
    close: () => new Promise((done) => {
      server.closeAllConnections()
      server.close(() => done())
    }),
  }
}

export function distHasTestHooks(): boolean {
  try {
    const html = readFileSync(join(DIST, 'index.html'), 'utf8')
    return html.length > 0
  } catch {
    return false
  }
}
