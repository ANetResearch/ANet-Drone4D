// GPU uniform-block limits (FX-UBO, ADR-086; M06-FR-085, M06-AC-058; AWR-18 §8.5, §10). Owner: M06.
// SwiftShader (the C1 browser of every other spec) reports MAX_UNIFORM_BUFFER_BINDINGS 72, 14 blocks per stage and 60
// combined, far above the WebGL2 minimum that many real GPUs report (24 / 12 / 12 / 24, e.g. Chrome on macOS through
// ANGLE Metal). three r186 gave each node uniform group a global binding point for life; synthcity needs 60-70 of them,
// so the public demo failed on such GPUs with "Maximum number of simultaneously usable uniforms groups reached" and
// hundreds of GL_INVALID_OPERATION "uniform buffer that is too small" while every test passed. This spec emulates the
// limits strictly in an init script: getParameter reports them, bindBufferBase / bindBufferRange / uniformBlockBinding
// past the binding points are refused with a console error (as a real driver raises GL_INVALID_VALUE), and the first
// use of a program whose referenced blocks exceed a per-stage or combined limit logs an error (a real link failure).
// For the spec minimum (24/12/12/24) and a middle set (36/14/14/28), on Tier S and Tier B, with a test build and the
// public demo build, it opens /world/synthcity on FakeSource (6 vehicles on the ground ring; step keyframes every 3 s run
// twice through the 12 weather presets, then clear holds) and runs: reveal, all 12 presets (recorded from the environment
// store or the environment panel), select a vehicle, Third and FPV, the P600 close-up,
// every layer off and on, a pick, the colour modes. It asserts no WebGL error and no three warning or error in the
// console (the THREE.Clock and AttributeNode warnings included), the limits reached the page (__perf.gpu.ubo), the
// binding points and per-program block counts stay within them, nothing was over budget, the point cloud and a vehicle
// change the pixels when toggled (screenshot difference against the frame-to-frame noise), and no program was compiled
// after the reveal. Builds: GPU_LIMITS_TEST_DIST / GPU_LIMITS_DEMO_DIST, or both are built into .cache/gpu-limits/.
// The demo build has no test hooks: it is driven through the UI (fleet rail, hotkeys, layers panel and its colour modes);
// its Tier B run spoofs the WebGL renderer string of an Apple M2 (iGPU by name), as the reported user had.
// VERIFY-UBO (ADR-087): the emulation also reports MAX_UNIFORM_BLOCK_SIZE (16 384 in the minimum set, the WebGL2 minimum
// and the value of ANGLE Metal; SwiftShader reports 65 536) and refuses a program whose active block is larger (a real
// link failure). At 16 384 the 300 low-poly matrices of Tier B (19 200 B) leave the uniform buffer for interleaved
// instance attributes, which the classic handler never updated before fix 6 (every low-poly vehicle drawn at the origin):
// the low-poly cases read back what the GPU draws with and compare it with instanceMatrix. More limit sets and the plain
// production build: GPU_LIMITS_SETS=min,wide,mid,swiftshader (or all; default min,mid) and GPU_LIMITS_BUILDS=test,default,demo
// (default test,demo); the production build is driven through the UI like the demo build (GPU_LIMITS_DEFAULT_DIST).
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { inflateSync } from 'node:zlib'
import { expect, test, type Page } from '@playwright/test'
import { frames } from './common'
import { startM06Server, type M06Server } from './server'

const WEB = resolve(import.meta.dirname, '../..')
const ROOT = resolve(WEB, '../..')
interface Limits { bindings: number; vertex: number; fragment: number; combined: number; blockSize: number }
type LimitSet = 'min' | 'wide' | 'mid' | 'swiftshader'
/** null: no emulation (the device's own limits; SwiftShader 72 / 14 / 14 / 60, 65 536 B) */
const LIMITS: Record<LimitSet, Limits | null> = {
  min: { bindings: 24, vertex: 12, fragment: 12, combined: 24, blockSize: 16384 },
  wide: { bindings: 24, vertex: 16, fragment: 16, combined: 32, blockSize: 16384 },
  mid: { bindings: 36, vertex: 14, fragment: 14, combined: 28, blockSize: 65536 },
  swiftshader: null,
}
const envList = <T extends string>(v: string | undefined, all: readonly T[], dflt: readonly T[]): T[] =>
  !v ? [...dflt] : v === 'all' ? [...all] : v.split(',').map((x) => x.trim()).filter((x): x is T => (all as readonly string[]).includes(x))
const SETS = envList<LimitSet>(process.env.GPU_LIMITS_SETS, ['min', 'wide', 'mid', 'swiftshader'], ['min', 'mid'])
const BUILDS = envList<Build>(process.env.GPU_LIMITS_BUILDS, ['test', 'default', 'demo'], ['test', 'demo'])
const PRESETS = ['clear', 'partlyCloudy', 'overcast', 'lightRain', 'rain', 'heavyRain', 'thunderstorm', 'fog', 'haze', 'snow', 'blizzard', 'sandstorm']
const LAYERS = ['pointcloud', 'drones', 'trails', 'frustums', 'mission', 'zones', 'environment', 'labels']
const ENV_PERIOD_S = 3
// passes through the 12 presets from the connection on, then clear holds: the last pass runs entirely after the reveal
// (2 passes; 3 for the demo build on Tier B, whose iGPU class at DPR 1 renders at about 1 fps on SwiftShader and reveals
// later)
const query = (cycles: number): string =>
  `source=fake&fakeWorld=synthcity&fakeN=6&fakeGround=1&fakeEnvPeriodS=${ENV_PERIOD_S}&fakeEnvPresets=all&fakeEnvStep=1&fakeEnvCycles=${cycles}`
const APPLE_M2 = 'ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)'

type Build = 'test' | 'default' | 'demo'
const dists: Record<Build, string> = { test: '', default: '', demo: '' }
const servers: Partial<Record<Build, M06Server>> = {}

/** a private build (never the shared dist/ of other work packages) */
function ensureBuild(kind: Build): string {
  const given = { test: process.env.GPU_LIMITS_TEST_DIST, default: process.env.GPU_LIMITS_DEFAULT_DIST, demo: process.env.GPU_LIMITS_DEMO_DIST }[kind]
  if (given) return resolve(given)
  const out = join(ROOT, '.cache', 'gpu-limits', kind)
  const env: NodeJS.ProcessEnv = { ...process.env }
  delete env.VITE_AWR_TEST_SWITCHES
  delete env.VITE_AWR_DEMO
  if (kind === 'test') env.VITE_AWR_TEST_SWITCHES = '1'
  else if (kind === 'demo') env.VITE_AWR_DEMO = 'public'
  execFileSync('npx', ['vite', 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'warn'], { cwd: WEB, env, stdio: 'pipe' })
  if (!existsSync(join(out, 'index.html'))) throw new Error(`${kind} build missing in ${out}`)
  return out
}

test.beforeAll(async () => {
  test.setTimeout(240_000)
  for (const k of BUILDS) {
    dists[k] = ensureBuild(k)
    servers[k] = await startM06Server(undefined, dists[k])
  }
})
test.afterAll(async () => {
  for (const s of Object.values(servers)) await s?.close()
})

/**
 * strict emulation of the uniform-block limits (and optionally a hardware renderer string), before any page script;
 * L null: the device's own limits, still enforced the same way
 */
function emulate(o: { L: Limits | null; renderer: string | null }): void {
  const P = WebGL2RenderingContext.prototype
  // the original methods, taken from their property descriptors (called with the context as `this`)
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  const gp = own('getParameter')
  let L = o.L
  const limits = (gl: WebGL2RenderingContext): Limits => (L ??= {
    bindings: gp.call(gl, 0x8a2f) as number, vertex: gp.call(gl, 0x8a2b) as number, fragment: gp.call(gl, 0x8a2d) as number,
    combined: gp.call(gl, 0x8a2e) as number, blockSize: gp.call(gl, 0x8a30) as number,
  })
  ;(window as unknown as { __gpuLimitsOf: (gl: WebGL2RenderingContext) => Limits }).__gpuLimitsOf = limits
  P.getParameter = function (this: WebGL2RenderingContext, n: number) {
    if (o.renderer !== null && n === 0x9246) return o.renderer // UNMASKED_RENDERER_WEBGL
    if (o.L === null) return gp.call(this, n)
    if (n === 0x8a2f) return o.L.bindings // MAX_UNIFORM_BUFFER_BINDINGS
    if (n === 0x8a2b) return o.L.vertex // MAX_VERTEX_UNIFORM_BLOCKS
    if (n === 0x8a2d) return o.L.fragment // MAX_FRAGMENT_UNIFORM_BLOCKS
    if (n === 0x8a2e) return o.L.combined // MAX_COMBINED_UNIFORM_BLOCKS
    if (n === 0x8a30) return o.L.blockSize // MAX_UNIFORM_BLOCK_SIZE
    return gp.call(this, n)
  } as typeof P.getParameter
  const bbb = own('bindBufferBase')
  P.bindBufferBase = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null) {
    const L = limits(this)
    if (t === 0x8a11 && i >= L.bindings) {
      console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferBase(UNIFORM_BUFFER, ${i}) >= MAX_UNIFORM_BUFFER_BINDINGS ${L.bindings}`)
      return
    }
    bbb.call(this, t, i, b)
  }
  const bbr = own('bindBufferRange')
  P.bindBufferRange = function (this: WebGL2RenderingContext, t: number, i: number, b: WebGLBuffer | null, off: number, size: number) {
    const L = limits(this)
    if (t === 0x8a11 && i >= L.bindings) {
      console.error(`[gpu-limits] GL_INVALID_VALUE: bindBufferRange(UNIFORM_BUFFER, ${i}) >= MAX_UNIFORM_BUFFER_BINDINGS ${L.bindings}`)
      return
    }
    bbr.call(this, t, i, b, off, size)
  }
  const ubb = own('uniformBlockBinding')
  P.uniformBlockBinding = function (this: WebGL2RenderingContext, p: WebGLProgram, bi: number, point: number) {
    const L = limits(this)
    if (point >= L.bindings) {
      console.error(`[gpu-limits] GL_INVALID_VALUE: uniformBlockBinding(..., ${bi}, ${point}) >= MAX_UNIFORM_BUFFER_BINDINGS ${L.bindings}`)
      return
    }
    ubb.call(this, p, bi, point)
  }
  // a program over a per-stage or combined limit would not link on such a GPU: report it at its first use
  const checked = new WeakSet<WebGLProgram>()
  const stats = { programs: 0, maxV: 0, maxF: 0, maxBlockSize: 0 }
  ;(window as unknown as { __gpuLimits: unknown }).__gpuLimits = stats
  const up = own('useProgram')
  P.useProgram = function (this: WebGL2RenderingContext, p: WebGLProgram | null) {
    if (p && !checked.has(p)) {
      checked.add(p)
      if (this.getProgramParameter(p, this.LINK_STATUS)) {
        const L = limits(this)
        const n = this.getProgramParameter(p, this.ACTIVE_UNIFORM_BLOCKS) as number
        let v = 0
        let f = 0
        let size = 0
        for (let i = 0; i < n; i++) {
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_VERTEX_SHADER)) v++
          if (this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_REFERENCED_BY_FRAGMENT_SHADER)) f++
          size = Math.max(size, this.getActiveUniformBlockParameter(p, i, this.UNIFORM_BLOCK_DATA_SIZE) as number)
        }
        stats.programs++
        stats.maxV = Math.max(stats.maxV, v)
        stats.maxF = Math.max(stats.maxF, f)
        stats.maxBlockSize = Math.max(stats.maxBlockSize, size)
        if (v > L.vertex || f > L.fragment || v + f > L.combined) {
          console.error(`[gpu-limits] GL_INVALID_OPERATION: program links ${v} vertex / ${f} fragment uniform blocks (limits ${L.vertex} / ${L.fragment} / ${L.combined})`)
        }
        if (size > L.blockSize) {
          console.error(`[gpu-limits] GL_INVALID_OPERATION: program links a ${size} B uniform block (MAX_UNIFORM_BLOCK_SIZE ${L.blockSize})`)
        }
      }
    }
    up.call(this, p)
  }
}

interface Watch { gl: string[]; three: string[]; errors: string[]; pageErrors: string[]; bad404: string[] }
const GL_RE = /GL_INVALID|GL_OUT_OF|uniform buffer|too many errors|Maximum number of simultaneously usable uniforms groups|\[gpu-limits\]|WebGL: /
/** every console warning and error is a failure except resource 404s of the static test server (/api, missing shared env assets) */
function watchConsole(page: Page): Watch {
  const w: Watch = { gl: [], three: [], errors: [], pageErrors: [], bad404: [] }
  page.on('pageerror', (e) => w.pageErrors.push(`${e.name}: ${e.message}`.slice(0, 300)))
  page.on('response', (r) => {
    if (r.status() < 400) return
    const p = new URL(r.url()).pathname
    if (!p.startsWith('/api/') && !p.startsWith('/worlds/_shared/')) w.bad404.push(`${r.status()} ${p}`)
  })
  page.on('console', (m) => {
    const t = m.text()
    if (m.type() !== 'warning' && m.type() !== 'error') return
    if (GL_RE.test(t)) w.gl.push(t.slice(0, 240))
    else if (/THREE\./.test(t)) w.three.push(t.slice(0, 240))
    else if (m.type() === 'error' && !/^Failed to load resource/.test(t)) w.errors.push(t.slice(0, 240))
  })
  return w
}

// ---------------------------------------------------------------- pixels (screenshots, PNG decoded here)
interface Img { w: number; h: number; px: Uint8Array }
function decodePng(buf: Buffer): Img {
  let o = 8
  let w = 0
  let h = 0
  let bpp = 0
  const idat: Buffer[] = []
  while (o < buf.length) {
    const len = buf.readUInt32BE(o)
    const type = buf.toString('ascii', o + 4, o + 8)
    const data = buf.subarray(o + 8, o + 8 + len)
    if (type === 'IHDR') {
      w = data.readUInt32BE(0)
      h = data.readUInt32BE(4)
      if (data[8] !== 8 || data[12] !== 0 || (data[9] !== 2 && data[9] !== 6)) throw new Error('PNG: only 8-bit RGB/RGBA, not interlaced')
      bpp = data[9] === 6 ? 4 : 3
    } else if (type === 'IDAT') idat.push(data)
    else if (type === 'IEND') break
    o += 12 + len
  }
  const raw = inflateSync(Buffer.concat(idat))
  const stride = w * bpp
  const px = new Uint8Array(w * h * 4)
  let prev = new Uint8Array(stride)
  let cur = new Uint8Array(stride)
  for (let y = 0; y < h; y++) {
    const base = y * (stride + 1)
    const f = raw[base]
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? cur[i - bpp] : 0
      const b = prev[i]
      const c = i >= bpp ? prev[i - bpp] : 0
      let v = raw[base + 1 + i]
      if (f === 1) v += a
      else if (f === 2) v += b
      else if (f === 3) v += (a + b) >> 1
      else if (f === 4) {
        const p = a + b - c
        const pa = Math.abs(p - a)
        const pb = Math.abs(p - b)
        const pc = Math.abs(p - c)
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c
      }
      cur[i] = v & 255
    }
    for (let x = 0; x < w; x++) {
      const q = 4 * (y * w + x)
      px[q] = cur[x * bpp]
      px[q + 1] = cur[x * bpp + 1]
      px[q + 2] = cur[x * bpp + 2]
      px[q + 3] = bpp === 4 ? cur[x * bpp + 3] : 255
    }
    ;[prev, cur] = [cur, prev]
  }
  return { w, h, px }
}
/** pixels whose largest channel difference exceeds thr */
function diffPx(a: Img, b: Img, thr = 24): number {
  let n = 0
  for (let i = 0; i < a.px.length; i += 4) {
    if (Math.max(Math.abs(a.px[i] - b.px[i]), Math.abs(a.px[i + 1] - b.px[i + 1]), Math.abs(a.px[i + 2] - b.px[i + 2])) > thr) n++
  }
  return n
}
interface Rect { x: number; y: number; width: number; height: number }
async function viewportRect(page: Page): Promise<Rect> {
  return page.evaluate(() => {
    const r = document.querySelector('[data-viewport] canvas')!.getBoundingClientRect()
    return { x: Math.round(r.left), y: Math.round(r.top), width: Math.floor(r.width), height: Math.floor(r.height) }
  })
}
async function shot(page: Page, clip: Rect): Promise<Img> {
  await frames(page, 4)
  return decodePng(await page.screenshot({ clip }))
}
/**
 * pixels the toggled content adds over the frame-to-frame noise: two frames with it (noise), one without (signal); the
 * content is visible when the signal exceeds the noise by minPx
 */
/**
 * the frame stops changing on its own: no point-cloud download in flight or queued, and the drawn point count, the CAS
 * budget and rung and the PerfGovernor step unchanged for 2.5 s (Tier B on SwiftShader keeps adapting for a while)
 */
async function settle(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    type P = { pc: { inflight: number; queued: number; drawn: number }; cas: { B: number; index: number }; governor: { step: number } }
    const w = window as unknown as { __perf: P; __settle?: { key: string; since: number } }
    const p = w.__perf
    const key = `${p.pc.drawn}|${p.cas.B}|${p.cas.index}|${p.governor.step}`
    const now = performance.now()
    if (p.pc.inflight > 0 || p.pc.queued > 0 || w.__settle?.key !== key) {
      w.__settle = { key, since: now }
      return false
    }
    return now - w.__settle.since > 2500
  }, null, { timeout: 90_000, polling: 250 }).catch(() => {})
}

const BRIGHT = new Set(['clear', 'partlyCloudy', 'overcast', 'haze'])
/**
 * pixels the toggled content changes, against the frame-to-frame noise: frames a1, a2 with the content, b without, a3
 * with it again. The weather keeps cycling (a step keyframe every 3 s relights the whole scene), so a measurement only
 * counts when it starts under a bright preset (contrast) and both brackets are quiet (a1 = a2 = a3 within 1 % of the
 * pixels); otherwise it is repeated. noise is the larger bracket difference.
 */
async function visibleBy(page: Page, build: Build, clip: Rect, hide: () => Promise<void>, show: () => Promise<void>, thr = 24): Promise<{ signal: number; noise: number; px: number; tries: number }> {
  const px = clip.width * clip.height
  let last = { signal: 0, noise: px, px, tries: 0 }
  for (let k = 1; k <= 8; k++) {
    await settle(page)
    const t0 = Date.now()
    while (!BRIGHT.has(await currentPreset(page, build)) && Date.now() - t0 < 40_000) await page.waitForTimeout(200)
    const a1 = await shot(page, clip)
    const a2 = await shot(page, clip)
    await hide()
    const b = await shot(page, clip)
    await show()
    const a3 = await shot(page, clip)
    last = { signal: diffPx(a2, b, thr), noise: Math.max(diffPx(a1, a2, thr), diffPx(a2, a3, thr)), px, tries: k }
    if (last.noise < 0.01 * px) break
  }
  return last
}

// ---------------------------------------------------------------- driving the page
type Hooks = {
  __perf: { frame: { count: number }; gpu: { programs: number; passPlan: number; compiledAfterReveal: number; planMismatches: number
    ubo: null | { limits: Limits; points: number; maxVertex: number; maxFragment: number; maxCombined: number; maxDistinct: number; groups: number; violations: number; pruned: string[] } }
    meta: { tier: string } }
  __vp: { roster(): { id: string }[]; select(ids: string[]): void; setMode(m: string): { ok: boolean }; camera(): { mode: string } | null
    dronePose(id: string): [number, number, number] | null; project(e: number, n: number, u: number): [number, number] | null
    drones(): { heroN: number; n: number } | null; pointAt(x: number, y: number): Promise<unknown>; viewportRect(): { x: number; y: number; w: number; h: number } | null
    vpSession: { rig: { lookAtEnu(e: number[], t: number[], tr: boolean): void } } }
  __pc: { layers: { setVisible(id: string, on: boolean): void; setColorMode(m: string): void } }
  __env: { env: { store: { current: { toPreset?: string } | null } } }
  __gpuLimits: { programs: number; maxV: number; maxF: number; maxBlockSize: number }
  __gpuLimitsOf(gl: WebGL2RenderingContext): Limits
}

async function setLayer(page: Page, build: Build, id: string, on: boolean): Promise<void> {
  if (build === 'test') await page.evaluate(([i, v]) => (window as unknown as Hooks).__pc.layers.setVisible(i as string, v as boolean), [id, on] as const)
  else {
    // Base UI Switch: the id sits on its hidden checkbox; the field label toggles it
    if ((await page.locator(`#layer-${id}`).isChecked()) !== on) await page.locator(`label[for="layer-${id}"]`).click()
  }
  await frames(page, 3)
}

/** the weather preset in effect: the environment store (test build) or the pressed preset of the environment panel */
async function currentPreset(page: Page, build: Build): Promise<string> {
  if (build === 'test') return page.evaluate(() => (window as unknown as Hooks).__env.env.store.current?.toPreset ?? '')
  return page.evaluate(() => document.querySelector('[data-env-panel] [data-preset][aria-pressed="true"]')?.getAttribute('data-preset') ?? '')
}

interface RunResult { tier: string; L: Limits; ubo: NonNullable<Hooks['__perf']['gpu']['ubo']>; emulated: Hooks['__gpuLimits']; programsAtReveal: number; programsEnd: number
  pc: { signal: number; noise: number; px: number; tries: number }; drone: { signal: number; noise: number; px: number; tries: number }; presets: string[] }

/** AWR_SHOTS_DIR: frames of the run for the visual review (P600 close-up, layers, end) */
async function keep(page: Page, name: string): Promise<void> {
  const dir = process.env.AWR_SHOTS_DIR
  if (dir) await page.screenshot({ path: join(dir, `${name}.png`) })
}

/** the limits the page runs under: the emulated set, or the device's own (read through the unpatched getParameter) */
async function effectiveLimits(page: Page): Promise<Limits> {
  return page.evaluate(() => {
    const g = window as unknown as Hooks
    return g.__gpuLimitsOf(document.querySelector<HTMLCanvasElement>('[data-viewport] canvas')!.getContext('webgl2')!)
  })
}

async function exercise(page: Page, build: Build, tier: 'S' | 'B', set: LimitSet): Promise<RunResult> {
  const base = servers[build]!.url
  const tag = `fx-ubo-${build}-${tier}-${set}`
  // builds without test switches reach Tier B through the device class: an Apple M2 renderer string (iGPU by name)
  await page.addInitScript(emulate, { L: LIMITS[set], renderer: build !== 'test' && tier === 'B' ? APPLE_M2 : null })
  const cycles = build !== 'test' && tier === 'B' ? 3 : 2
  const tGoto = Date.now()
  await page.goto(`${base}/world/synthcity?${query(cycles)}${build === 'test' && tier === 'B' ? '&tier=B' : ''}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 90_000 })
  await page.waitForFunction(() => {
    const p = (window as unknown as Hooks).__perf
    return !!p && p.gpu.passPlan > 0 && p.frame.count > 20
  }, null, { timeout: 90_000 })
  expect(await page.evaluate(() => (window as unknown as Hooks).__perf.meta.tier)).toBe(tier)
  const L = await effectiveLimits(page)
  if (LIMITS[set]) expect(L).toEqual(LIMITS[set])
  expect(await page.evaluate(() => (window as unknown as Hooks).__perf.gpu.ubo?.limits)).toEqual({ bindings: L.bindings, vertex: L.vertex, fragment: L.fragment, combined: L.combined })
  const programsAtReveal = await page.evaluate(() => (window as unknown as Hooks).__perf.gpu.programs)
  const clip = await viewportRect(page)

  // the layers and environment panels live in the left dock of the demo and production builds (closed by default)
  if (build !== 'test') {
    const label = page.locator('label[for="layer-pointcloud"]')
    if (!(await label.isVisible())) await page.keyboard.press('Control+KeyB')
    await expect(label).toBeVisible({ timeout: 10_000 })
  }

  // every weather preset (FakeSource step keyframes every 3 s, the 12 presets in turn), then the end of the cycle: clear
  // holds, so the pixel checks below are not relit between frames
  const presets = new Set<string>()
  const cycleEnd = tGoto + (cycles * PRESETS.length + 1) * ENV_PERIOD_S * 1000 + 6000
  while (Date.now() < cycleEnd + 30_000) {
    const p = await currentPreset(page, build)
    if (p) presets.add(p)
    if (presets.size === PRESETS.length && Date.now() > cycleEnd && p === 'clear') break
    await page.waitForTimeout(300)
  }

  // the point cloud's pixels at the home view (weather layer hidden: precipitation moves between frames)
  await setLayer(page, build, 'environment', false)
  await keep(page, `${tag}-home`)
  // the centre of the viewport, clear of the docked panels and the HUD (dark buildings over the dark ground: 12 levels)
  const centre: Rect = { x: Math.round(clip.x + clip.width / 2 - 320), y: Math.round(clip.y + clip.height / 2 - 200), width: 640, height: 400 }
  const pc = await visibleBy(page, build, centre, () => setLayer(page, build, 'pointcloud', false), () => setLayer(page, build, 'pointcloud', true), 12)
  await setLayer(page, build, 'environment', true)

  // select a vehicle, Third and FPV, back to orbit
  if (build === 'test') {
    await page.waitForFunction(() => (window as unknown as Hooks).__vp.roster().length >= 1, null, { timeout: 30_000 })
    const id = await page.evaluate(() => (window as unknown as Hooks).__vp.roster()[0].id)
    await page.evaluate((i) => (window as unknown as Hooks).__vp.select([i]), id)
    for (const m of ['third', 'fpv', 'orbit']) {
      expect((await page.evaluate((mm) => (window as unknown as Hooks).__vp.setMode(mm), m)).ok).toBe(true)
      await frames(page, 20)
    }
  } else {
    await page.locator('[data-rail-row]').first().click()
    for (const k of ['Digit3', 'Digit4', 'Digit1']) {
      await page.keyboard.press(k)
      await frames(page, 20)
    }
  }

  // P600 close-up and the vehicle's pixels (weather hidden so precipitation does not move under the crop)
  await setLayer(page, build, 'environment', false)
  let droneClip: Rect
  if (build === 'test') {
    const id = await page.evaluate(() => (window as unknown as Hooks).__vp.roster()[0].id)
    const p = (await page.evaluate((i) => (window as unknown as Hooks).__vp.dronePose(i), id))!
    await page.evaluate(([e, t]) => (window as unknown as Hooks).__vp.vpSession.rig.lookAtEnu(e, t, false),
      [[p[0] - 3.5, p[1] - 2.0, p[2] + 1.6], [p[0], p[1], p[2] + 0.2]] as [number[], number[]])
    await page.waitForFunction(() => ((window as unknown as Hooks).__vp.drones()?.heroN ?? 0) >= 1, null, { timeout: 60_000, polling: 250 })
    await frames(page, 10)
    const s = (await page.evaluate(([e, n, u]) => (window as unknown as Hooks).__vp.project(e, n, u), [p[0], p[1], p[2] + 0.2] as const))!
    droneClip = { x: Math.max(clip.x, Math.round(clip.x + s[0] - 120)), y: Math.max(clip.y, Math.round(clip.y + s[1] - 120)), width: 240, height: 240 }
  } else {
    // Third on the selected vehicle (chase camera 16 m behind it, the vehicle at the viewport centre), then dolly in to
    // the 5 m minimum: the screen radius passes 48 px and the P600 model replaces the low-poly instance
    await page.keyboard.press('Digit3')
    await frames(page, 20)
    const cx = clip.x + clip.width / 2
    const cy = clip.y + clip.height / 2
    await page.mouse.move(cx, cy)
    for (let i = 0; i < 12; i++) {
      await page.mouse.wheel(0, -400)
      await page.waitForTimeout(120)
    }
    await page.waitForTimeout(2500)
    await frames(page, 10)
    droneClip = { x: Math.round(cx - 120), y: Math.round(cy - 120), width: 240, height: 240 }
  }
  await keep(page, `${tag}-p600`)
  const drone = await visibleBy(page, build, droneClip, () => setLayer(page, build, 'drones', false), () => setLayer(page, build, 'drones', true))
  await setLayer(page, build, 'environment', true)

  // every layer off, then on
  for (const id of LAYERS) await setLayer(page, build, id, false)
  await frames(page, 10)
  for (const id of LAYERS) await setLayer(page, build, id, true)
  await frames(page, 10)

  // a pick at the viewport centre (point-cloud ID pass and ground pick)
  if (build === 'test') {
    await page.evaluate(() => (window as unknown as Hooks).__vp.setMode('orbit'))
    await page.evaluate(({ x, y }) => (window as unknown as Hooks).__vp.pointAt(x, y), { x: clip.width / 2, y: clip.height / 2 })
  } else {
    await page.keyboard.press('Digit1')
    await frames(page, 10)
    await page.mouse.click(clip.x + clip.width / 2, clip.y + clip.height / 2)
  }
  await frames(page, 10)

  // colour modes
  if (build === 'test') {
    for (const m of ['hag', 'class', 'normal', 'intensity', 'height']) {
      await page.evaluate((mm) => (window as unknown as Hooks).__pc.layers.setColorMode(mm), m)
      await frames(page, 6)
    }
  } else {
    // the colour-mode toggle group of the layers panel (the command palette has no colour-mode action: VERIFY-UBO found
    // the earlier palette queries matched nothing); each click must leave its item pressed
    await page.keyboard.press('Escape')
    const group = page.locator('[aria-label="着色"]')
    for (const m of ['离地', '类别', '法线', '高度']) {
      const item = group.getByRole('button', { name: m, exact: true })
      await item.click()
      await expect(item).toHaveAttribute('aria-pressed', 'true')
      await frames(page, 6)
    }
  }
  await frames(page, 20)

  await keep(page, `${tag}-end`)
  const end = await page.evaluate(() => {
    const g = window as unknown as Hooks
    const gl = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas')!.getContext('webgl2')!
    return { ubo: g.__perf.gpu.ubo!, programs: g.__perf.gpu.programs, emulated: g.__gpuLimits, glError: gl.getError() }
  })
  expect(end.glError, 'gl.getError() after the run').toBe(0)
  return { tier, L, ubo: end.ubo, emulated: end.emulated, programsAtReveal, programsEnd: end.programs, pc, drone, presets: [...presets] }
}

for (const build of BUILDS) {
  for (const tier of ['S', 'B'] as const) {
    for (const lim of SETS) {
      test(`${build} build, Tier ${tier}, uniform-block limits ${lim} (${LIMITS[lim] ? Object.values(LIMITS[lim]).join('/') : 'device'})`, async ({ page }, info) => {
        test.setTimeout(480_000)
        const w = watchConsole(page)
        const r = await exercise(page, build, tier, lim)
        const L = r.L
        info.annotations.push({ type: 'gpu-limits', description: JSON.stringify(r) })
        const u = r.ubo
        console.log(`[gpu-limits] ${build} Tier ${tier} ${Object.values(L).join('/')}: points ${u.points}, blocks per program V ${u.maxVertex} F ${u.maxFragment} `
          + `combined ${u.maxCombined} distinct ${u.maxDistinct} (linked: V ${r.emulated.maxV} F ${r.emulated.maxF} over ${r.emulated.programs} programs), live groups ${u.groups}, `
          + `violations ${u.violations}, programs ${r.programsAtReveal} -> ${r.programsEnd}, point cloud ${r.pc.signal}/${r.pc.noise} px (${r.pc.tries}), vehicle ${r.drone.signal}/${r.drone.noise} px (${r.drone.tries}), `
          + `presets ${r.presets.length}, gl ${w.gl.length}, three ${w.three.length}, errors ${w.errors.length}`)
        // binding points and per-program blocks within the emulated limits, nothing over budget
        expect(r.ubo.points).toBeGreaterThan(0)
        expect(r.ubo.points).toBeLessThanOrEqual(L.bindings)
        expect(r.ubo.maxVertex).toBeLessThanOrEqual(L.vertex)
        expect(r.ubo.maxFragment).toBeLessThanOrEqual(L.fragment)
        expect(r.ubo.maxCombined).toBeLessThanOrEqual(L.combined)
        expect(r.ubo.maxDistinct).toBeLessThanOrEqual(L.bindings)
        expect(r.ubo.violations).toBe(0)
        expect(r.ubo.pruned).toEqual([])
        expect(r.emulated.maxV).toBeLessThanOrEqual(L.vertex)
        expect(r.emulated.maxF).toBeLessThanOrEqual(L.fragment)
        expect(r.emulated.maxBlockSize).toBeLessThanOrEqual(L.blockSize)
        expect(r.presets.sort()).toEqual([...PRESETS].sort())
        // the point cloud and the vehicle are on screen
        expect(r.pc.noise, `point cloud ${JSON.stringify(r.pc)}`).toBeLessThan(0.01 * r.pc.px)
        expect(r.pc.signal, `point cloud ${JSON.stringify(r.pc)}`).toBeGreaterThan(r.pc.noise + 0.1 * r.pc.px)
        expect(r.drone.noise, `vehicle ${JSON.stringify(r.drone)}`).toBeLessThan(0.01 * r.drone.px)
        expect(r.drone.signal, `vehicle ${JSON.stringify(r.drone)}`).toBeGreaterThan(r.drone.noise + 600)
        // no program compiled after the reveal (renderer.info.programs)
        expect(r.programsEnd - r.programsAtReveal).toBeLessThanOrEqual(0)
        expect(w.gl, w.gl.join('\n')).toEqual([])
        expect(w.three, w.three.join('\n')).toEqual([])
        expect(w.errors, w.errors.join('\n')).toEqual([])
        expect(w.pageErrors, w.pageErrors.join('\n')).toEqual([])
        expect(w.bad404).toEqual([])
      })
    }
  }
}

// ---------------------------------------------------------------- low-poly instance matrices (fix 6, VERIFY-UBO)
/** test hook: on the next instanced draw of window.__instTarget.prog, read what the GPU holds for instance 0's translation */
function captureInstanced(): void {
  const P = WebGL2RenderingContext.prototype
  const own = <K extends keyof WebGL2RenderingContext>(k: K): WebGL2RenderingContext[K] => Object.getOwnPropertyDescriptor(P, k)!.value as WebGL2RenderingContext[K]
  type T = { prog: WebGLProgram; cpu(): number[] }
  const w = window as unknown as { __instTarget: T | null; __instGrab: { kind: string; gpu: number[]; cpu: number[] } | null }
  const grab = (gl: WebGL2RenderingContext): void => {
    const t = w.__instTarget
    if (!t || gl.getParameter(gl.CURRENT_PROGRAM) !== t.prog) return
    w.__instTarget = null
    const read = (buf: WebGLBuffer, off: number, n: number): number[] => {
      const dst = new Float32Array(n)
      gl.bindBuffer(gl.COPY_READ_BUFFER, buf)
      gl.getBufferSubData(gl.COPY_READ_BUFFER, off, dst)
      gl.bindBuffer(gl.COPY_READ_BUFFER, null)
      return Array.from(dst)
    }
    let kind = 'none'
    let gpu: number[] = []
    const loc = gl.getAttribLocation(t.prog, 'nodeAttribute3') // 4th matrix column (translation) of the instancing fallback
    if (loc >= 0) {
      kind = 'attribute'
      gpu = read(gl.getVertexAttrib(loc, gl.VERTEX_ATTRIB_ARRAY_BUFFER_BINDING) as WebGLBuffer, gl.getVertexAttribOffset(loc, gl.VERTEX_ATTRIB_ARRAY_POINTER), 3)
    } else {
      const n = gl.getProgramParameter(t.prog, gl.ACTIVE_UNIFORM_BLOCKS) as number
      for (let i = 0; i < n; i++) {
        if (!/^NodeBuffer/.test(gl.getActiveUniformBlockName(t.prog, i) ?? '')) continue
        kind = 'ubo'
        const point = gl.getActiveUniformBlockParameter(t.prog, i, gl.UNIFORM_BLOCK_BINDING) as number
        gpu = read(gl.getIndexedParameter(gl.UNIFORM_BUFFER_BINDING, point) as WebGLBuffer, 0, 16).slice(12, 15)
      }
    }
    w.__instGrab = { kind, gpu, cpu: t.cpu() }
  }
  const de = own('drawElementsInstanced')
  const da = own('drawArraysInstanced')
  P.drawElementsInstanced = function (this: WebGL2RenderingContext, ...a: Parameters<WebGL2RenderingContext['drawElementsInstanced']>) {
    grab(this)
    de.apply(this, a)
  }
  P.drawArraysInstanced = function (this: WebGL2RenderingContext, ...a: Parameters<WebGL2RenderingContext['drawArraysInstanced']>) {
    grab(this)
    da.apply(this, a)
  }
}

type LowHooks = Hooks & { __vp: Hooks['__vp'] & { vpSession: { scene: { traverse(f: (o: LowObj) => void): void }; be: { renderer: { properties: { get(m: unknown): { currentProgram?: { program: WebGLProgram } } } } } } }
  __instTarget: { prog: WebGLProgram; cpu(): number[] } | null; __instGrab: { kind: string; gpu: number[]; cpu: number[] } | null }
interface LowObj { name: string; count: number; material: { _latestBuilder?: { updateAfterNodes: unknown[] } } | unknown[]; instanceMatrix?: { array: Float32Array } }

for (const tier of BUILDS.includes('test') ? (['S', 'B'] as const) : []) {
  test(`test build, Tier ${tier}, low-poly instance matrices reach the GPU (MAX_UNIFORM_BLOCK_SIZE ${LIMITS.min!.blockSize})`, async ({ page }) => {
    test.setTimeout(240_000)
    const w = watchConsole(page)
    await page.addInitScript(emulate, { L: LIMITS.min, renderer: null })
    await page.addInitScript(captureInstanced)
    // 40 vehicles orbiting: with the camera 85 m from the first, its neighbours fall into the low-poly bucket
    await page.goto(`${servers.test!.url}/world/synthcity?source=fake&fakeWorld=synthcity&fakeN=40${tier === 'B' ? '&tier=B' : ''}`)
    await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: 90_000 })
    await page.waitForFunction(() => ((window as unknown as Hooks).__perf?.frame.count ?? 0) > 20, null, { timeout: 90_000 })
    expect(await page.evaluate(() => (window as unknown as Hooks).__perf.meta.tier)).toBe(tier)
    // fix 6 runs updateBefore nodes only: no program of the scene may rely on updateAfter
    const after = await page.evaluate(() => {
      const out: string[] = []
      ;(window as unknown as LowHooks).__vp.vpSession.scene.traverse((o) => {
        const m = o.material as { _latestBuilder?: { updateAfterNodes: unknown[] } } | undefined
        if (m && !Array.isArray(m) && (m._latestBuilder?.updateAfterNodes.length ?? 0) > 0) out.push(o.name)
      })
      return out
    })
    expect(after).toEqual([])
    const id = await page.evaluate(() => (window as unknown as Hooks).__vp.roster()[0].id)
    const samples: { batch: string; lowN: number; kind: string; gpu: number[]; cpu: number[] }[] = []
    for (let k = 0; k < 4; k++) {
      const p = (await page.evaluate((i) => (window as unknown as Hooks).__vp.dronePose(i), id))!
      await page.evaluate(([e, t]) => (window as unknown as Hooks).__vp.vpSession.rig.lookAtEnu(e, t, false), [[p[0] - 60, p[1] - 60, p[2] + 40], [p[0], p[1], p[2]]] as [number[], number[]])
      await frames(page, 30)
      const batch = await page.evaluate(() => {
        const g = window as unknown as LowHooks
        let m: LowObj | null = null
        g.__vp.vpSession.scene.traverse((o) => {
          if (!m && /^DroneLowPoly\./.test(o.name) && o.count > 0) m = o
        })
        if (!m) return ''
        const o = m as LowObj
        g.__instGrab = null
        g.__instTarget = { prog: g.__vp.vpSession.be.renderer.properties.get(o.material).currentProgram!.program, cpu: () => Array.from(o.instanceMatrix!.array.slice(12, 15)) }
        return o.name
      })
      await frames(page, 3)
      const lowN = await page.evaluate(() => ((window as unknown as Hooks).__vp.drones() as unknown as { lowN: number }).lowN)
      const g = await page.evaluate(() => (window as unknown as LowHooks).__instGrab)
      samples.push({ batch, lowN, kind: g?.kind ?? 'no draw', gpu: g?.gpu ?? [], cpu: g?.cpu ?? [] })
    }
    console.log(`[gpu-limits] low-poly Tier ${tier}: ${JSON.stringify(samples)}`)
    for (const s of samples) {
      expect(s.batch, JSON.stringify(s)).toMatch(/^DroneLowPoly\./)
      expect(s.lowN).toBeGreaterThan(0)
      // Tier B: 300 x 64 B > 16 384 B, interleaved instance attributes; Tier S: 32 x 64 B, the uniform buffer
      expect(s.kind).toBe(tier === 'B' ? 'attribute' : 'ubo')
      for (let i = 0; i < 3; i++) expect(Math.abs(s.gpu[i] - s.cpu[i]), JSON.stringify(s)).toBeLessThan(1e-3)
    }
    expect(Math.hypot(samples[3].cpu[0] - samples[0].cpu[0], samples[3].cpu[1] - samples[0].cpu[1])).toBeGreaterThan(1) // the vehicles moved
    expect(w.gl, w.gl.join('\n')).toEqual([])
    expect(w.three, w.three.join('\n')).toEqual([])
    expect(w.errors, w.errors.join('\n')).toEqual([])
    expect(w.pageErrors, w.pageErrors.join('\n')).toEqual([])
  })
}
