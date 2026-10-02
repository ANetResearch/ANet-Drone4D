// Backend orchestration of one run (M16-FR-043; M16 §6.7.2, §9.2; AWR-18 §3.1 PR-6, §9.4).
//   live  supervisor with the perf profile of configs/runtime.yaml (CPU pinning on: api core0, sim-core core1,
//         plan-pool core7), AWR_WORLD / AWR_SCENARIO / AWR_SCENARIO_PROFILE from the CaseDef, ports offset
//         AWR_PORT_OFFSET (default 9); ready = READY banner + /api/health/ready + every /api/sys/procs item RUNNING;
//         affinity check (PERF-E014); the api serves apps/web/dist, /worlds and /api (COOP/COEP).
//   fake  tools/fake/fake_gw.py pinned to core0 + a static server (dist, worlds, public/bench) that proxies /api and
//         the WebSocket to it (18 §19 F-08: diagnostics only, never gating).
//   none  static server only (M06 and M05 specs bring their own servers; kept for static feature pages).
// During the sampling window /proc/<pid>/stat of api and sim-core is read every 1 s (authoritative CPU, 18 §9.4 item 3).
import { spawn } from 'node:child_process'
import { cpSync, createReadStream, existsSync, mkdirSync, mkdtempSync, rmSync, statSync } from 'node:fs'
import { createServer, request } from 'node:http'
import { connect, createServer as createTcpServer } from 'node:net'
import { tmpdir } from 'node:os'
import { extname, join, normalize } from 'node:path'
import { cpuCores } from './analyze.mjs'
import { pinned } from './browser.mjs'
import { affinityOf, procRssMb, procTicks } from './protocol.mjs'
import { ROOT, WEB_DIR } from './registry.mjs'

const PY = join(ROOT, '.venv', 'bin', 'python')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export function portOffset() {
  const k = Number.parseInt(process.env.AWR_PERF_PORT_OFFSET ?? process.env.AWR_PORT_OFFSET ?? '9', 10)
  return Number.isFinite(k) ? k : 9
}

export function freePort() {
  return new Promise((ok, fail) => {
    const s = createTcpServer()
    s.once('error', fail)
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      s.close(() => ok(typeof a === 'object' && a ? a.port : 0))
    })
  })
}

async function fetchJson(url, init = {}, timeoutMs = 5000) {
  const ac = new AbortController()
  const t = setTimeout(() => ac.abort(), timeoutMs)
  try {
    const r = await fetch(url, { ...init, signal: ac.signal })
    const body = await r.text()
    return { status: r.status, json: body ? JSON.parse(body) : null }
  } finally {
    clearTimeout(t)
  }
}

// ------------------------------------------------------------ live backend (supervisor)
/**
 * @param {import('./types').BackendSpec} spec
 * @param {{ portOffset?: number, runDir: string, profile?: string, pin?: boolean, log?: (s: string) => void }} o
 */
export async function startBackend(spec, o) {
  if (spec.kind === 'fake') return startFake(spec, o)
  if (spec.kind === 'none' || spec.kind === 'tool') return startStatic({ runDir: o.runDir })
  const k = o.portOffset ?? portOffset()
  const runs = mkdtempSync(join(tmpdir(), 'awr-perf-runs-'))
  const profile = o.profile ?? 'perf'
  const env = { ...process.env, AWR_RUNS_DIR: runs, AWR_WORLD: spec.world, PYTHONUNBUFFERED: '1', ...(spec.env ?? {}) }
  delete env.AWR_SUPERVISOR_PID
  if (spec.scenario) env.AWR_SCENARIO = spec.scenario
  if (spec.scenarioProfile) env.AWR_SCENARIO_PROFILE = spec.scenarioProfile
  const args = ['-m', 'awr.runtime.supervisor', '--profile', profile, '--set', `net.port_offset=${k}`, '--set', 'run.keep_run_dir=false']
  // diagnostics only (never a gate run): AWR_PERF_RUNTIME_CONFIG points the supervisor at another runtime.yaml (ACC-1 used it
  // to look at the front end of ladder n1000 while the production sim-core liveness threshold kills the scenario setup)
  if (process.env.AWR_PERF_RUNTIME_CONFIG) args.push('-c', process.env.AWR_PERF_RUNTIME_CONFIG)
  // PR-12 single object under test: only sim-core and api unless the case asks for the ext processes
  const only = spec.only ?? 'sim-core,api'
  if (only !== 'all') args.push('--only', only)
  const p = spawn(PY, args, { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
  const log = []
  let exited = null
  p.on('exit', (code) => { exited = code ?? -1 })
  const banner = {}
  const ready = new Promise((ok, fail) => {
    const t = setTimeout(() => fail(new Error(`supervisor: no READY within 90 s\n${log.slice(-30).join('\n')}`)), 90_000)
    const onData = (d) => {
      for (const line of d.toString().split('\n')) {
        if (!line) continue
        log.push(line)
        const m = /^\s*(api|admin)\s+(\S+)/.exec(line)
        if (m) banner[m[1]] = m[2]
        if (line.startsWith('READY')) {
          banner.run = /run=(\S+)/.exec(line)?.[1]
          setTimeout(() => { clearTimeout(t); ok() }, 300)   // the banner lines after READY
        }
      }
    }
    p.stdout.on('data', onData)
    p.stderr.on('data', onData)
    p.on('exit', (code) => { clearTimeout(t); fail(new Error(`supervisor exited ${code}\n${log.slice(-30).join('\n')}`)) })
  })
  const handle = { kind: 'live', proc: p, log, runs, runDir: o.runDir, base: null, banner, exited: () => exited, profile, spec,
    samplers: [], adminToken: null, viewerToken: null }
  try {
    await ready
    const api = new URL(banner.api ?? `http://127.0.0.1:${8000 + 10 * k}/world/${spec.world}`)
    handle.base = `${api.protocol}//${api.host}`
    await waitHttp(`${handle.base}/api/health/ready`, 60_000)
    handle.viewerToken = await token(handle.base, 'viewer')
    // No admin token (ACC-1): issuing one claims the free operator seat (17 §3.2, auth.py), so the page under test came up as
    // a viewer with "seat: other" and every spec that needs the seat (skeleton, interaction, storm RTL) failed or hung.
    // /api/sys/procs is a viewer route and returns the pids; the admin-only log tail is not used by the harness.
    void banner.admin
    await waitProcsRunning(handle, 30_000)
  } catch (e) {
    await stopBackend(handle)
    throw e
  }
  return handle
}

async function waitHttp(url, timeoutMs) {
  const end = Date.now() + timeoutMs
  let last = ''
  while (Date.now() < end) {
    try {
      const r = await fetch(url)
      if (r.ok) return
      last = `${r.status}`
    } catch (e) {
      last = String(e)
    }
    await sleep(250)
  }
  throw new Error(`${url} not ready: ${last}`)
}

async function token(base, role, adminSecret) {
  const body = { role, principal_hint: role === 'admin' ? 'MSIXTEENHARNESSADMINAAAA' : 'MSIXTEENHARNESSVIEWERAAA' }
  if (adminSecret) body.admin_secret = adminSecret
  const r = await fetchJson(`${base}/api/auth/token`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  if (r.status !== 200) throw new Error(`token ${role}: ${r.status}`)
  return r.json.token
}

export async function procs(h) {
  const r = await fetchJson(`${h.base}/api/sys/procs`, { headers: { authorization: `Bearer ${h.adminToken ?? h.viewerToken}` } })
  if (r.status !== 200) throw new Error(`/api/sys/procs ${r.status}`)
  return r.json.items ?? []
}

async function waitProcsRunning(h, timeoutMs) {
  const end = Date.now() + timeoutMs
  let items = []
  while (Date.now() < end) {
    try {
      items = await procs(h)
      const core = items.filter((x) => x.layer === 'core' || ['api', 'sim-core'].includes(x.name))
      if (core.length && core.every((x) => x.state === 'RUNNING')) {
        h.pids = Object.fromEntries(items.filter((x) => x.pid).map((x) => [x.name, x.pid]))
        return
      }
    } catch {
      // api restarting
    }
    await sleep(500)
  }
  throw new Error(`processes not RUNNING: ${JSON.stringify(items.map((x) => [x.name, x.state]))}`)
}

/** PR-6: api on core0 and sim-core on core1 under the perf profile; returns error strings (PERF-E014) */
export function checkAffinity(h, want = { api: '0', 'sim-core': '1' }) {
  const errs = []
  for (const [name, cpus] of Object.entries(want)) {
    const pid = h.pids?.[name]
    if (!pid) continue
    const got = affinityOf(pid)
    if (got !== cpus) errs.push(`PERF-E014 ${name} pid ${pid} affinity ${got} != ${cpus}`)
  }
  return errs
}

/** wait for a scenario mark label (e.g. ladder.steady) on /api/events; resolves with the event */
export async function waitMark(h, label, timeoutS = 150) {
  let since = 0
  const end = Date.now() + timeoutS * 1000
  while (Date.now() < end) {
    if (h.exited?.() !== null && h.exited?.() !== undefined) throw new Error('backend exited while waiting for mark')
    const r = await fetchJson(`${h.base}/api/events?since=${since}&limit=1000`, { headers: { authorization: `Bearer ${h.viewerToken}` } })
    if (r.status === 200) {
      for (const e of r.json.items ?? []) {
        const d = e.data ?? {}
        if (d.label === label || e.type === label) return e
        if (e.type === 'scenario.invalid') throw new Error(`scenario.invalid ${JSON.stringify(d)}`)
      }
      since = r.json.next_since ?? since
    }
    await sleep(1000)
  }
  throw new Error(`mark ${label} not seen within ${timeoutS} s`)
}

/** 1 Hz /proc sampling of api and sim-core during the measured window */
export function startProcSampler(h) {
  const pids = h.pids ?? {}
  const s = { t0: {}, t1: {}, rss0: {}, rss1: {}, timer: null }
  const take = (dst, rss) => {
    for (const [name, pid] of Object.entries(pids)) {
      const ticks = procTicks(pid)
      if (ticks !== null) dst[name] = { t: performance.now() / 1000, ticks }
      if (rss) rss[name] = procRssMb(pid)
    }
  }
  take(s.t0, s.rss0)
  s.timer = setInterval(() => take(s.t1, s.rss1), 1000)
  s.timer.unref?.()
  return {
    stop() {
      clearInterval(s.timer)
      take(s.t1, s.rss1)
      const proc = {}
      for (const name of Object.keys(pids)) proc[name] = { cpu_core: cpuCores(s.t0[name], s.t1[name]), rss_mb: [s.rss0[name], s.rss1[name]] }
      return proc
    },
  }
}

export async function fetchServerWindow(h, windowS = 60) {
  if (!h?.base || h.kind !== 'live') return null
  try {
    const r = await fetchJson(`${h.base}/api/sys/perf?window_s=${windowS}`, { headers: { authorization: `Bearer ${h.viewerToken}` } })
    return r.status === 200 ? r.json : null
  } catch {
    return null
  }
}

export async function stopBackend(h) {
  if (!h) return { exitCode: null, logsDir: null }
  for (const x of h.extra ?? []) await x.close?.()
  if (h.static) await h.static.close()
  const p = h.proc
  if (p && h.exited?.() === null) {
    try {
      process.kill(-p.pid, 'SIGTERM')
    } catch {
      // gone
    }
    const end = Date.now() + 30_000
    while (h.exited() === null && Date.now() < end) await sleep(100)
    if (h.exited() === null) {
      try {
        process.kill(-p.pid, 'SIGKILL')
      } catch {
        // gone
      }
    }
  }
  let logsDir = null
  if (h.runs && h.runDir) {
    const run = h.banner?.run
    const src = run ? join(h.runs, run, 'logs') : null
    if (src && existsSync(src)) {
      logsDir = join(h.runDir, 'logs')
      mkdirSync(logsDir, { recursive: true })
      cpSync(src, logsDir, { recursive: true })
    }
    rmSync(h.runs, { recursive: true, force: true })
  }
  return { exitCode: h.exited?.() ?? null, logsDir }
}

// ------------------------------------------------------------ static server (+ /api proxy)
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.geojson': 'application/geo+json', '.bin': 'application/octet-stream', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
  '.png': 'image/png', '.wasm': 'application/wasm', '.f32': 'application/octet-stream', '.u8': 'application/octet-stream' }
const COI = { 'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Embedder-Policy': 'require-corp', 'Cross-Origin-Resource-Policy': 'same-origin' }

function sendFile(req, res, file) {
  const st = statSync(file)
  const base = { ...COI, 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache' }
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '')
  if (m) {
    const a = m[1] === '' ? Math.max(0, st.size - Number(m[2])) : Number(m[1])
    const b = m[1] === '' || m[2] === '' ? st.size - 1 : Math.min(st.size - 1, Number(m[2]))
    if (a > b) return res.writeHead(416, { ...base, 'Content-Range': `bytes */${st.size}` }).end()
    res.writeHead(206, { ...base, 'Content-Range': `bytes ${a}-${b}/${st.size}`, 'Content-Length': String(b - a + 1) })
    return createReadStream(file, { start: a, end: b }).pipe(res)
  }
  res.writeHead(200, { ...base, 'Content-Length': String(st.size) })
  createReadStream(file).pipe(res)
}

/**
 * Static server: SPA from `dist` (fallback index.html), /worlds/** from the repository worlds, /bench/** from dist or
 * public; /api (and the /api/rt upgrade) proxied to `api` when given. Used by fake backends and report rendering.
 */
export async function startStatic({ dist = process.env.AWR_PERF_DIST ?? join(WEB_DIR, 'dist'), api = null, port = 0, routes = {} } = {}) {
  const worlds = process.env.AWR_WORLDS_DIR ?? join(ROOT, 'worlds')
  const server = createServer((req, res) => {
    const u = new URL(req.url ?? '/', 'http://x')
    const path = decodeURIComponent(u.pathname)
    if (routes[path]) return routes[path](req, res)
    if (path.startsWith('/api/')) {
      if (!api) return res.writeHead(404, COI).end()
      const pr = request({ host: api.host, port: api.port, path: req.url, method: req.method, headers: req.headers }, (r) => {
        res.writeHead(r.statusCode ?? 502, r.headers)
        r.pipe(res)
      })
      pr.on('error', () => res.writeHead(502, COI).end())
      return req.pipe(pr)
    }
    const roots = path.startsWith('/worlds/') ? [[worlds, path.slice(8)]] : [[dist, path.slice(1)], [join(WEB_DIR, 'public'), path.slice(1)]]
    for (const [r, rel] of roots) {
      const f = normalize(join(r, rel))
      if (f.startsWith(r) && existsSync(f) && statSync(f).isFile()) return sendFile(req, res, f)
    }
    if (path.startsWith('/worlds/') || path.startsWith('/bench/')) return res.writeHead(404, COI).end()
    sendFile(req, res, join(dist, 'index.html'))
  })
  server.on('upgrade', (req, socket, head) => {
    if (!api || !(req.url ?? '').startsWith('/api/rt')) return socket.destroy()
    const up = connect(api.port, api.host, () => {
      up.write([`${req.method} ${req.url} HTTP/1.1`, ...Object.entries(req.headers).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`), '', ''].join('\r\n'))
      if (head.length) up.write(head)
      up.pipe(socket)
      socket.pipe(up)
    })
    up.on('error', () => socket.destroy())
    socket.on('error', () => up.destroy())
  })
  await new Promise((ok) => server.listen(port, '127.0.0.1', ok))
  const addr = server.address()
  const url = `http://127.0.0.1:${typeof addr === 'object' && addr ? addr.port : port}`
  const close = () => new Promise((done) => { server.closeAllConnections(); server.close(() => done()) })
  return { kind: 'none', base: url, static: { url, close }, exited: () => null }
}

async function startFake(spec, o) {
  const port = await freePort()
  const [cmd, args] = pinned(PY, [join(ROOT, 'tools', 'fake', 'fake_gw.py'), '--n', String(spec.fakeN ?? 2), '--port', String(port),
    '--world', spec.world], '0')
  const p = spawn(cmd, args, { cwd: ROOT, stdio: 'ignore', detached: true })
  let exited = null
  p.on('exit', (c) => { exited = c ?? -1 })
  await waitHttp(`http://127.0.0.1:${port}/api/health/live`, 20_000)
  const st = await startStatic({ api: { host: '127.0.0.1', port } })
  return { ...st, kind: 'fake', proc: p, exited: () => exited, runDir: o.runDir }
}

// ------------------------------------------------------------ weak-network proxy (M11 tools/bench/ipc/netem_proxy.py)
/**
 * start the proxy in front of the api with its control port (M16 §6.11, §7.4): `proxy.control` is
 * http://127.0.0.1:<port> (POST /cut?ms=, /profile?name=, /stall?ms=; GET /stats). By default W3 still cuts the connections
 * 30 s after the proxy starts (built into the profile); `autoCut: false` leaves the cut to cutProxy (flight-time aligned).
 */
export async function startProxy(h, profile, seed = 7, o = {}) {
  const api = new URL(h.base)
  const port = await freePort()
  const cport = await freePort()
  const args = [join(ROOT, 'tools', 'bench', 'ipc', 'netem_proxy.py'), '--listen', `127.0.0.1:${port}`, '--target',
    `${api.hostname}:${api.port}`, '--profile', profile, '--seed', String(seed), '--control', `127.0.0.1:${cport}`]
  if (o.autoCut === false) args.push('--no-auto-cut')
  const p = spawn(PY, args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'ignore'] })
  await new Promise((ok, fail) => {
    const t = setTimeout(() => fail(new Error('netem_proxy: no READY')), 15_000)
    p.stdout.on('data', (d) => { if (d.toString().includes('READY')) { clearTimeout(t); ok() } })
    p.on('exit', (c) => { clearTimeout(t); fail(new Error(`netem_proxy exited ${c}`)) })
  })
  const proxy = { base: `http://localhost:${port}`, control: `http://127.0.0.1:${cport}`, proc: p, close: async () => { p.kill('SIGTERM') } }
  ;(h.extra ??= []).push(proxy)
  return proxy
}

/** cut every proxied connection for `ms` (POST /cut?ms=; W3 uses 3000); falls back to one SIGUSR1 stall without a control port */
export async function cutProxy(proxy, ms = 3000) {
  if (!proxy.control) {
    proxy.proc.kill('SIGUSR1')
    return { ok: true, stall: true }
  }
  const r = await fetch(`${proxy.control}/cut?ms=${Math.round(ms)}`, { method: 'POST' })
  if (!r.ok) throw new Error(`netem_proxy /cut: HTTP ${r.status}`)
  return r.json()
}

/** switch the proxy profile at run time (POST /profile?name=) */
export async function setProxyProfile(proxy, name) {
  const r = await fetch(`${proxy.control}/profile?name=${encodeURIComponent(name)}`, { method: 'POST' })
  if (!r.ok) throw new Error(`netem_proxy /profile: HTTP ${r.status}`)
  return r.json()
}
