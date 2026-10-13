// World files for the M15 specs that need the real viewport (WP-12b screenshots and FPV cases): `vite preview` serves the
// web build only (the api process serves /worlds/** in production, vite.config.ts proxy table), so the page's requests
// for /worlds/<id>/** are answered from the repository's worlds/ directory, with HTTP Range support for the point cloud
// hierarchy and node byte ranges (206 + Content-Range).
import { existsSync, readFileSync, statSync } from 'node:fs'
import { join, normalize } from 'node:path'
import type { BrowserContext } from '@playwright/test'

const ROOT = join(import.meta.dirname, '..', '..', '..', '..', '..', 'worlds')
const TYPES: Record<string, string> = { json: 'application/json', geojson: 'application/geo+json', bin: 'application/octet-stream', awrv: 'application/octet-stream' }

/** answer /worlds/** from the repository (false when the directory is missing: build the worlds first) */
export async function serveWorlds(ctx: BrowserContext): Promise<boolean> {
  if (!existsSync(ROOT)) return false
  await ctx.route('**/worlds/**', (route) => {
    const url = new URL(route.request().url())
    const rel = normalize(decodeURIComponent(url.pathname.replace(/^\/worlds\//, ''))).replace(/^(\.\.(\/|\\|$))+/, '')
    const file = join(ROOT, rel)
    if (!file.startsWith(ROOT) || !existsSync(file) || !statSync(file).isFile()) return route.fulfill({ status: 404, body: '' })
    const body = readFileSync(file)
    const type = TYPES[file.split('.').pop() ?? ''] ?? 'application/octet-stream'
    const headers: Record<string, string> = { 'content-type': type, 'accept-ranges': 'bytes', 'cross-origin-resource-policy': 'same-origin', 'cache-control': 'no-cache' }
    const range = route.request().headers().range
    const m = range ? /^bytes=(\d+)-(\d*)$/.exec(range) : null
    if (m) {
      const a = Number(m[1])
      const b = m[2] ? Math.min(Number(m[2]), body.length - 1) : body.length - 1
      if (a >= body.length) return route.fulfill({ status: 416, headers: { 'content-range': `bytes */${body.length}` }, body: '' })
      return route.fulfill({ status: 206, headers: { ...headers, 'content-range': `bytes ${a}-${b}/${body.length}` }, body: body.subarray(a, b + 1) })
    }
    return route.fulfill({ status: 200, headers, body })
  })
  return true
}
