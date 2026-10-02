// TrailBatch without instancing (FX2-R3, ADR-067; M06-FR-042): every segment an explicit quad, screen-space width in
// raster px, square caps, empty segments and segments behind the camera collapsed in the vertex stage, the draw range
// ending at the last occupied slot. Rendered on the Tier S classic backend into a render target and read back.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera, Scene, type WebGLRenderTarget } from 'three'
import { TrailBatch, TrailRing } from '@/engine'
import { createRenderBackend, type RenderBackend } from '@/viewport/renderer'

const W = 160
const H = 120

async function backend(): Promise<RenderBackend> {
  const be = await createRenderBackend(document.createElement('canvas'), { pref: 'auto', forced: { tier: 'S' } })
  be.renderer.setPixelRatio(1)
  be.renderer.setSize(W, H, false)
  be.state = 'READY'
  return be
}

async function render(be: RenderBackend, s: Scene, cam: PerspectiveCamera): Promise<Uint8Array> {
  const rt = be.createRT('selftest', { width: W, height: H }) as WebGLRenderTarget
  const r = be.renderer
  r.setRenderTarget(rt)
  r.setClearColor(0x000000, 1)
  r.clear()
  r.render(s, cam)
  r.setRenderTarget(null)
  const out = new Uint8Array(W * H * 4)
  await be.readPixels(rt, 0, 0, W, H, out)
  rt.dispose()
  return out
}

/** lit pixels per row (green channel above the threshold) */
function litRows(b: Uint8Array, thr = 20): number[] {
  const rows = new Array<number>(H).fill(0)
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (b[(y * W + x) * 4 + 1] > thr) rows[y]++
  return rows
}

describe('TrailBatch (non-instanced quads)', () => {
  it('draws the history of a slotted vehicle as a line of the requested raster width, nothing for empty slots', async () => {
    const be = await backend()
    const ring = new TrailRing(4)
    // a vertical trail in front of the camera: x = 0, y from -10 to 10 m, z = -40 m (three world, the batch draws in
    // its parent's space); samples 0.5 s apart
    for (let k = 0; k <= 20; k++) ring.append(7, 0, -10 + k, -40, k * 0.5)
    const style = { widthCss: 4, colors: [[0, 1, 0]] as const, alpha: 1, renderOrder: 0, name: 'TestTrail' }
    const b = new TrailBatch(4, 64, style)
    const s = new Scene()
    s.add(b.mesh)
    const cam = new PerspectiveCamera(60, W / H, 1, 1000)
    cam.updateMatrixWorld()
    b.setView(W, H, cam.near)
    b.setWidth(4, 1)
    // empty batch: invisible, nothing drawn
    b.sync(ring, 10, () => 0)
    expect(b.mesh.visible).toBe(false)
    expect(b.drawCount()).toBe(0)
    b.assign(0, 7, 0, ring)
    b.sync(ring, 10, () => 0)
    expect(b.mesh.visible).toBe(true)
    const px = await render(be, s, cam)
    const rows = litRows(px)
    const covered = rows.filter((n) => n > 0)
    // 20 m at 40 m distance with fov 60 deg spans about 20 / (2 tan 30 deg 40) of the 120 px height = 52 rows
    expect(covered.length).toBeGreaterThan(40)
    expect(covered.length).toBeLessThan(70)
    // 4 px wide lines: every covered row in the middle has 3 to 6 lit pixels
    const mid = rows.slice(H / 2 - 10, H / 2 + 10)
    for (const n of mid) {
      expect(n).toBeGreaterThanOrEqual(3)
      expect(n).toBeLessThanOrEqual(6)
    }
    // releasing the slot draws nothing
    b.release(0)
    b.sync(ring, 10, () => 0)
    expect(b.mesh.visible).toBe(false)
    b.dispose()
  })

  it('trims a segment crossing the near plane and skips one entirely behind the camera', async () => {
    const be = await backend()
    const ring = new TrailRing(4)
    // from behind the camera (z = +5) to 30 m in front, offset to the right; and one segment fully behind
    ring.append(1, 3, 0, 5, 0)
    ring.append(1, 3, 0, -30, 0.5)
    ring.append(2, -3, 0, 5, 0)
    ring.append(2, -3, 0, 10, 0.5)
    const b = new TrailBatch(4, 8, { widthCss: 2, colors: [[0, 1, 0]] as const, alpha: 1, renderOrder: 0, name: 'TestTrail' })
    const s = new Scene()
    s.add(b.mesh)
    const cam = new PerspectiveCamera(60, W / H, 1, 1000)
    cam.updateMatrixWorld()
    b.setView(W, H, cam.near)
    b.setWidth(2, 1)
    b.assign(0, 1, 0, ring)
    b.assign(1, 2, 0, ring)
    b.sync(ring, 1, () => 0)
    const px = await render(be, s, cam)
    let left = 0
    let right = 0
    let total = 0
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        if (px[(y * W + x) * 4 + 1] <= 20) continue
        total++
        if (x < W / 2) left++
        else right++
      }
    }
    // the trimmed segment shows on the right half without covering the screen; the one behind the camera is not drawn
    expect(right).toBeGreaterThan(20)
    expect(total).toBeLessThan(0.2 * W * H)
    expect(left).toBe(0)
    b.dispose()
  })
})
