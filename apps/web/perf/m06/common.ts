// Shared helpers of the M06 Playwright specs (functional runs during development; the performance assertions are
// switched on by M06_PERF=1 under the exclusive lock of the acceptance phase, AWR-18 §3). Owner: M06.
import { inflateSync } from 'node:zlib'
import { expect, type Page } from '@playwright/test'

/** performance thresholds are asserted only when the harness sets M06_PERF=1 (M16 run.mjs under the perf lock) */
export const PERF = process.env.M06_PERF === '1'

export interface Watch { errors: string[]; m06: string[] }

/** page errors and M06 diagnostic errors (M06-E001..E009, shader errors, uncaught type errors) */
export function watch(page: Page): Watch {
  const w: Watch = { errors: [], m06: [] }
  page.on('pageerror', (e) => w.errors.push(`${e.name}: ${e.message}`))
  page.on('console', (m) => {
    const t = m.text()
    if (m.type() === 'error' && /M06-E00[1-9]|TypeError|ReferenceError|RangeError|THREE\.WebGLProgram|Shader Error/.test(t)) w.m06.push(t.slice(0, 400))
  })
  return w
}

/** reveal and first-frames timeout (ms): 60 s, M06_BOOT_MS on a loaded machine (parallel development) */
export const BOOT_MS = Number(process.env.M06_BOOT_MS) > 0 ? Number(process.env.M06_BOOT_MS) : 60_000

/** open a world page and wait for the reveal and 20 rendered frames */
export async function openWorld(page: Page, base: string, query = 'source=fake&fakeN=5', world = 'shenzhen', timeoutMs = BOOT_MS): Promise<void> {
  await page.goto(`${base}/world/${world}?${query}`)
  await expect(page.locator('#boot-mask')).toHaveCount(0, { timeout: timeoutMs })
  await page.waitForFunction(() => {
    const p = (window as unknown as { __perf?: { gpu: { passPlan: number }; frame: { count: number } } }).__perf
    return !!p && p.gpu.passPlan > 0 && p.frame.count > 20
  }, null, { timeout: timeoutMs })
}

/** read a dotted path of window.__perf (live object, no snapshot) */
export async function perf<T>(page: Page, path: string): Promise<T> {
  return page.evaluate((p) => p.split('.').reduce<unknown>((o, k) => (o as Record<string, unknown>)?.[k], (window as unknown as { __perf: unknown }).__perf) as never, path)
}

/** wait n animation frames in the page */
export async function frames(page: Page, n: number): Promise<void> {
  await page.evaluate(async (k) => {
    for (let i = 0; i < k; i++) await new Promise((r) => requestAnimationFrame(() => r(null)))
  }, n)
}

/** drawing buffer of the viewport canvas, "WxH" */
export async function canvasBuffer(page: Page): Promise<string> {
  return page.evaluate(() => {
    const c = document.querySelector<HTMLCanvasElement>('[data-viewport] canvas') ?? document.querySelector<HTMLCanvasElement>('canvas')
    return c ? `${c.width}x${c.height}` : 'none'
  })
}

/**
 * largest frame interval (ms) over the next windowMs, measured with rAF timestamps in the page (the 1 s window after
 * each warm-up step of M06-AC-008)
 */
export async function maxGapMs(page: Page, windowMs = 1000): Promise<number> {
  return page.evaluate(async (ms) => {
    let last = -1
    let worst = 0
    const t0 = performance.now()
    await new Promise<void>((done) => {
      const step = (t: number): void => {
        if (last >= 0) worst = Math.max(worst, t - last)
        last = t
        if (t - t0 < ms) requestAnimationFrame(step)
        else done()
      }
      requestAnimationFrame(step)
    })
    return worst
  }, windowMs)
}

export type VpHooks = {
  __vp: {
    setMode(m: string): { ok: boolean; reason?: string }
    camera(): { mode: string; pose: { eye_enu_m: number[]; target_enu_m: number[] } } | null
    select(ids: string[]): void
    roster(): { id: string }[]
    dronePose(id: string): [number, number, number] | null
    project(e: number, n: number, u: number): [number, number] | null
    viewportRect(): { x: number; y: number; w: number; h: number } | null
    drones(): { n: number; heroN: number; lowN: number; glyphs: number; markers: number } | null
    backendInfo(): { tier: string; deviceClass: string; state: string; programs: number } | null
    featMatrix(o: { tier?: 'A' | 'B' | 'S' }): Promise<{ backend: string; tier: string; tests: Record<string, unknown> }>
    vpSession: { rig: { focusSphere(c: number[], r: number): void; lookAtEnu(eye: number[], target: number[]): void } | null }
  }
}

/** D2 sensor view hooks (viewport/testHooks.ts __vp.sensorView, WP-11) */
export type SvHooks = {
  __vp: {
    sensorView: {
      set(m: 'fpv' | 'eo' | 'nir' | 'lwir', o?: { enterFpv?: boolean; sensorNo?: number }): { ok: boolean; reason?: string }
      cycle(): string
      info(): { mode: string; palette: string; digitalZoom: number; sensorNo: number | null; sensorName: string | null; kind: number | null
        hfovRad: number | null; available: Record<string, boolean> }
      available(): Record<string, boolean>
      setPalette(p: 'white_hot' | 'black_hot'): void
      setDigitalZoom(z: number): void
      setEnv(e: Record<string, unknown>): void
      setEmitters(rows: number[][]): void
      setEmittersAhead(rows: number[][]): void
      setIlluminators(list: { on: boolean; divergenceRad: number; rangeM: number }[] | null): void
      projectBox(c: number[], sz: number[], yaw: number): number[] | null
      pass(): { active: boolean; rt: [number, number] | null; emitters: number; candidates: number; inView: number; illuminators: number
        range: [number, number]; material: string; mode: number } | null
      projection(): { m: number[]; reversed: boolean } | null
    }
    registerModel(model: string, frame: string, size: [number, number, number]): void
  }
}

/** first roster id once FakeSource vehicles are present */
export async function firstVehicle(page: Page): Promise<string> {
  await page.waitForFunction(() => (window as unknown as VpHooks).__vp.roster().length >= 1, null, { timeout: 30_000 })
  await page.waitForFunction(() => {
    const vp = (window as unknown as VpHooks).__vp
    const id = vp.roster()[0]?.id
    return !!id && vp.dronePose(id) !== null
  }, null, { timeout: 30_000 })
  return page.evaluate(() => (window as unknown as VpHooks).__vp.roster()[0].id)
}

/** RGBA pixels of an 8-bit RGB or RGBA PNG (Playwright screenshots) */
export interface Img { w: number; h: number; px: Uint8Array }
export function decodePng(buf: Buffer): Img {
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
