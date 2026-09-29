// Static server of the M07 Playwright specs (perf/m07/*.spec.ts): apps/web/dist with the SPA fallback and the
// cross-origin isolation headers (AWR-17 §5.4), /worlds/<id>/** and /worlds/_shared/** from worlds/ (single-range 206,
// no-cache world.json), /api answering 404. The pages run on FakeSource (?source=fake): keyframes come from the fake
// gateway or from window.__env.injectPreset (test builds).
import { createReadStream, existsSync, readdirSync, statSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import { extname, join, normalize, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../../../..')
const DIST = process.env.M07_DIST ?? resolve(import.meta.dirname, '../../dist')
const WORLDS = join(ROOT, 'worlds')
const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.geojson': 'application/geo+json', '.wasm': 'application/wasm', '.png': 'image/png',
}
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

export const distReady = (): boolean => existsSync(join(DIST, 'index.html'))
export const worldsReady = (): boolean => existsSync(join(WORLDS, 'shenzhen', 'world.json'))

export function sharedSeeds(): number[] {
  const dir = join(WORLDS, '_shared', 'env', 'turb')
  if (!existsSync(dir)) return []
  // vk_s{seed}_n64_dx4_L30.awrv
  return readdirSync(dir).map((f) => /^vk_s(\d+)_n64_dx4_L30\.awrv$/.exec(f)?.[1]).filter(Boolean).map(Number)
}

export interface M07Server { url: string; close(): Promise<void> }

export function startM07Server(port: number): Promise<M07Server> {
  const srv: Server = createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x')
    const path = decodeURIComponent(u.pathname)
    let file: string | null = null
    let cache = 'no-cache'
    if (path.startsWith('/api/')) {
      res.writeHead(404, { ...COI, 'Content-Type': 'application/json' })
      res.end('{"code":305}')
      return
    }
    if (path.startsWith('/worlds/') && path.split('/').length > 3) {
      file = normalize(join(WORLDS, path.slice('/worlds/'.length)))
      if (!file.startsWith(WORLDS)) file = null
      cache = path.endsWith('world.json') ? 'no-cache' : 'public, max-age=31536000, immutable'
    } else {
      file = normalize(join(DIST, path))
      if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
    }
    if (!file || !existsSync(file) || statSync(file).isDirectory()) {
      res.writeHead(404, COI)
      res.end()
      return
    }
    const size = statSync(file).size
    const type = MIME[extname(file)] ?? 'application/octet-stream'
    const m = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range ?? '')
    if (m) {
      const a = Number(m[1])
      const b = Math.min(Number(m[2]), size - 1)
      res.writeHead(206, { ...COI, 'Cache-Control': cache, 'Content-Type': type, 'Content-Range': `bytes ${a}-${b}/${size}`, 'Content-Length': String(b - a + 1) })
      createReadStream(file, { start: a, end: b }).pipe(res)
      return
    }
    res.writeHead(200, { ...COI, 'Cache-Control': cache, 'Content-Type': type, 'Content-Length': String(size) })
    createReadStream(file).pipe(res)
  })
  return new Promise((ok) => srv.listen(port, '127.0.0.1', () => ok({
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((r) => srv.close(() => r())),
  })))
}
