#!/usr/bin/env node
// Static server for the paper's streaming measurements: <dist> with the SPA fallback and cross-origin isolation
// headers, and /worlds/<id>/** from <worlds> with single-range 206 responses (the point-cloud client streams octree
// pages with HTTP Range). Usage: node serve.mjs <dist> <worlds> [port]
import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize } from 'node:path'

const [dist, worlds, portArg] = process.argv.slice(2)
const port = Number(portArg ?? 4390)
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.wasm': 'application/wasm', '.geojson': 'application/geo+json' }
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp',
  'Cross-Origin-Resource-Policy': 'same-origin' }

function serve(req, res, file, cache) {
  const size = statSync(file).size
  const type = MIME[extname(file)] ?? 'application/octet-stream'
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '')
  if (m) {
    const a = m[1] === '' ? size - Number(m[2]) : Number(m[1])
    const b = m[1] === '' || m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1)
    if (a > b || a >= size) {
      res.writeHead(416, { ...COI, 'Content-Range': `bytes */${size}` })
      return res.end()
    }
    res.writeHead(206, { ...COI, 'Content-Type': type, 'Content-Range': `bytes ${a}-${b}/${size}`, 'Content-Length': b - a + 1,
      'Accept-Ranges': 'bytes', 'Cache-Control': cache })
    return createReadStream(file, { start: a, end: b }).pipe(res)
  }
  res.writeHead(200, { ...COI, 'Content-Type': type, 'Content-Length': size, 'Accept-Ranges': 'bytes', 'Cache-Control': cache })
  createReadStream(file).pipe(res)
}

createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  if (path.startsWith('/api')) {
    res.writeHead(404, COI)
    return res.end()
  }
  if (path.startsWith('/worlds/')) {
    const f = normalize(join(worlds, path.slice('/worlds/'.length)))
    if (!f.startsWith(normalize(worlds)) || !existsSync(f) || statSync(f).isDirectory()) {
      res.writeHead(404, COI)
      return res.end()
    }
    return serve(req, res, f, f.endsWith('world.json') ? 'no-cache' : 'public, max-age=31536000, immutable')
  }
  let f = normalize(join(dist, path))
  if (!f.startsWith(normalize(dist)) || !existsSync(f) || statSync(f).isDirectory()) f = join(dist, 'index.html')
  serve(req, res, f, f.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable')
}).listen(port, '127.0.0.1', () => console.log(`serving ${dist} and ${worlds} on http://127.0.0.1:${port}`))
