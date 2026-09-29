// Static server for the M11 browser specs without a backend: a (test) build with COOP/COEP, /worlds from the repository
// with single-range Range, SPA fallback, and 404 for /api so the token request fails fast and pages stay on FakeSource.
// M11_DIST selects the build directory (parallel agents share apps/web/dist; a private output directory is recommended).
import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { extname, join, normalize, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../../../..')
const DIST = resolve(process.env.M11_DIST ?? join(import.meta.dirname, '../../dist'))
const WORLDS = join(ROOT, 'worlds')
const OFFSET = Number.parseInt(process.env.AWR_PORT_OFFSET ?? '0', 10) || 0
export const PORT = 4187 + 10 * OFFSET
const MIME: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.bin': 'application/octet-stream', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.wasm': 'application/wasm', '.f32': 'application/octet-stream' }
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

function sendFile(req: IncomingMessage, res: ServerResponse, file: string): void {
  const size = statSync(file).size
  const type = MIME[extname(file)] ?? 'application/octet-stream'
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '')
  if (m) {
    const start = m[1] === '' ? Math.max(0, size - Number(m[2])) : Number(m[1])
    const end = m[1] !== '' && m[2] !== '' ? Math.min(size - 1, Number(m[2])) : size - 1
    if (start > end || start >= size) {
      res.writeHead(416, { ...COI, 'Content-Range': `bytes */${size}` }).end()
      return
    }
    res.writeHead(206, { ...COI, 'Content-Type': type, 'Content-Length': end - start + 1, 'Content-Range': `bytes ${start}-${end}/${size}`, 'Accept-Ranges': 'bytes' })
    createReadStream(file, { start, end }).pipe(res)
    return
  }
  res.writeHead(200, { ...COI, 'Content-Type': type, 'Content-Length': size, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache' })
  createReadStream(file).pipe(res)
}

export function startStaticServer(port = PORT): Promise<Server> {
  const server = createServer((req, res) => {
    const path = decodeURIComponent((req.url ?? '/').split('?')[0])
    if (path.startsWith('/api/')) {
      res.writeHead(404, { ...COI, 'Content-Type': 'application/problem+json' }).end(JSON.stringify({ code: 305, reason: 305, message: 'no backend (M11 smoke)' }))
      return
    }
    const base = path.startsWith('/worlds/') ? WORLDS : DIST
    const rel = path.startsWith('/worlds/') ? path.slice('/worlds/'.length) : path.slice(1)
    const file = normalize(join(base, rel))
    if (!file.startsWith(base) || rel.split('/').some((s) => s.startsWith('.'))) {
      res.writeHead(404, COI).end()
      return
    }
    if (existsSync(file) && statSync(file).isFile()) sendFile(req, res, file)
    else if (path.startsWith('/worlds/') || extname(path)) res.writeHead(404, COI).end()
    else sendFile(req, res, join(DIST, 'index.html')) // SPA routes
  })
  return new Promise((ok) => server.listen(port, '127.0.0.1', () => ok(server)))
}

export function stopServer(server: Server | null): Promise<void> {
  return new Promise<void>((ok) => (server ? server.close(() => ok()) : ok()))
}
