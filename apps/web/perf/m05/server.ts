// Static server of the M05 Playwright specs (perf/m05/*.spec.ts): apps/web/dist with the SPA fallback and the
// cross-origin isolation headers of AWR-17 §5.4, and /worlds/<id>/** from worlds/ with the AWR-17 §5 semantics the point
// cloud engine relies on: world.json no-cache, `?v=<contentVersion>` requests immutable and 409 (310
// CONTENT_VERSION_STALE) for another version, single-range 206 with Content-Range, 416 outside the file. Every request
// is logged (method, path, query, Range, Cache-Control header, time) for the request-sequence assertions (M05-AC-006).
// POST /__m05/cv {world, cv} simulates a rebuilt world (M05-AC-033); /api answers 404 (static browsing, M05-AC-032).
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { extname, join, normalize, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../../../..')
// M05_DIST: a private test build (VITE_AWR_TEST_SWITCHES=1 vite build --outDir <dir>), so the shared apps/web/dist of other
// work packages is never overwritten; defaults to apps/web/dist
const DIST = resolve(process.env.M05_DIST ?? resolve(import.meta.dirname, '../../dist'))
const WORLDS = join(ROOT, 'worlds')
const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.bin': 'application/octet-stream',
  '.f32': 'application/octet-stream', '.u8': 'application/octet-stream', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
  '.geojson': 'application/geo+json', '.wasm': 'application/wasm', '.glb': 'model/gltf-binary',
}
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

export interface LoggedRequest { method: string; path: string; query: string; range: string | null; cacheControl: string | null; contentType: string | null; t: number; status: number }

export interface M05Server {
  url: string
  log: LoggedRequest[]
  cv: Map<string, string>
  close(): Promise<void>
}

function send(res: ServerResponse, status: number, headers: Record<string, string>, body?: Buffer | string): void {
  res.writeHead(status, { ...COI, ...headers })
  res.end(body)
}

function serveFile(req: IncomingMessage, res: ServerResponse, file: string, headers: Record<string, string>, entry: LoggedRequest): void {
  const size = statSync(file).size
  const type = MIME[extname(file)] ?? 'application/octet-stream'
  const range = req.headers.range
  if (range) {
    const m = /^bytes=(\d+)-(\d+)$/.exec(range)
    if (!m || Number(m[1]) >= size) {
      entry.status = 416
      return send(res, 416, { 'Content-Range': `bytes */${size}` })
    }
    const a = Number(m[1])
    const b = Math.min(Number(m[2]), size - 1)
    entry.status = 206
    res.writeHead(206, { ...COI, ...headers, 'Content-Type': type, 'Content-Range': `bytes ${a}-${b}/${size}`, 'Content-Length': String(b - a + 1), 'Accept-Ranges': 'bytes' })
    createReadStream(file, { start: a, end: b }).pipe(res)
    return
  }
  entry.status = 200
  res.writeHead(200, { ...COI, ...headers, 'Content-Type': type, 'Content-Length': String(size), 'Accept-Ranges': 'bytes' })
  createReadStream(file).pipe(res)
}

export function startM05Server(port: number): Promise<M05Server> {
  const log: LoggedRequest[] = []
  const cv = new Map<string, string>()
  const t0 = Date.now()
  const server: Server = createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x')
    const entry: LoggedRequest = { method: req.method ?? 'GET', path: u.pathname, query: u.search, range: req.headers.range ?? null,
      cacheControl: (req.headers['cache-control'] as string | undefined) ?? null, contentType: (req.headers['content-type'] as string | undefined) ?? null, t: Date.now() - t0, status: 0 }
    log.push(entry)
    if (u.pathname === '/__m05/cv' && req.method === 'POST') {
      let body = ''
      req.on('data', (c: Buffer) => (body += c.toString()))
      req.on('end', () => {
        const o = JSON.parse(body) as { world: string; cv: string | null }
        if (o.cv) cv.set(o.world, o.cv)
        else cv.delete(o.world)
        entry.status = 204
        send(res, 204, {})
      })
      return
    }
    if (u.pathname.startsWith('/api/')) {
      entry.status = 404
      return send(res, 404, { 'Content-Type': 'application/problem+json' }, JSON.stringify({ code: 305, reason: 'NOT_FOUND' }))
    }
    if (u.pathname.startsWith('/worlds/')) {
      const rel = normalize(decodeURIComponent(u.pathname.slice('/worlds/'.length)))
      if (rel.startsWith('..')) return send(res, 400, {})
      const file = join(WORLDS, rel)
      const world = rel.split('/')[0]
      if (!existsSync(file) || statSync(file).isDirectory()) {
        entry.status = 404
        return send(res, 404, {})
      }
      const real = JSON.parse(readFileSync(join(WORLDS, world, 'world.json'), 'utf8')) as { contentVersion: string }
      const current = cv.get(world) ?? real.contentVersion
      if (rel === `${world}/world.json`) {
        const w = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>
        w.contentVersion = current
        entry.status = 200
        return send(res, 200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache' }, JSON.stringify(w))
      }
      const v = u.searchParams.get('v')
      if (v !== null && v !== current) {
        entry.status = 409
        return send(res, 409, { 'Content-Type': 'application/problem+json', 'Cache-Control': 'no-store' }, JSON.stringify({ code: 310, reason: 'CONTENT_VERSION_STALE' }))
      }
      return serveFile(req, res, file, { 'Cache-Control': v ? 'public, max-age=31536000, immutable' : 'no-cache' }, entry)
    }
    let file = join(DIST, normalize(decodeURIComponent(u.pathname)))
    if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
    serveFile(req, res, file, { 'Cache-Control': file.endsWith('index.html') ? 'no-cache' : 'public, max-age=3600' }, entry)
  })
  return new Promise((ok, fail) => {
    server.once('error', fail)
    server.listen(port, '127.0.0.1', () => ok({ url: `http://127.0.0.1:${port}`, log, cv, close: () => new Promise<void>((r) => server.close(() => r())) }))
  })
}

export const distReady = (): boolean => existsSync(join(DIST, 'index.html'))
export const worldsReady = (): boolean => existsSync(join(WORLDS, 'shenzhen', 'world.json'))
