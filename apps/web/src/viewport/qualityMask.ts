// Quality coverage masks (AWR-18 §4.5 item 2; M05-FR-054; M05 request 5). Owner: M06. Test builds with ?quality=1 only.
// M05 appends __perf.quality.samples[k] = {t, pose, mask: null} in the world phase of the sampled frame and emits
// 'pc.quality.sample'. In the overlay phase of that frame (after the render phase, outside the pass-plan assertion)
// the point-cloud channel is drawn again with the same DrawTable and point parameters into an offscreen RGBA8 target of
// the point pass size (Tier S: the drawing buffer, 640 x 360; Tier B/A: the cloudRT sub-viewport), cleared to 0; the
// alpha channel (opaque point materials write 1) is read back asynchronously (ADR-007) and packed to 1 bit per pixel,
// rows bottom-up, LSB first, into samples[k].mask.
import type { WebGLRenderTarget } from 'three'
import { activePointCloud, perfProbe, register, type FrameCtx } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { CH_CLOUD } from './layers/registry'
import type { RenderBackend } from './renderer'
import { vp } from './session'

/** pack the alpha channel of an RGBA8 read-back into 1 bit per pixel (bit i of byte i >> 3, LSB first) */
export function packCoverage(rgba: Uint8Array, w: number, h: number, out?: Uint8Array): Uint8Array {
  const n = w * h
  const bits = out ?? new Uint8Array((n + 7) >> 3)
  bits.fill(0)
  for (let i = 0; i < n; i++) if (rgba[4 * i + 3] > 0) bits[i >> 3] |= 1 << (i & 7)
  return bits
}

/** install the overlay task (no-op outside test builds or without ?quality=1) */
export function installQualityMasks(be: RenderBackend): () => void {
  if (!TEST_SWITCHES || typeof location === 'undefined' || new URLSearchParams(location.search).get('quality') !== '1') return () => {}
  const pending: number[] = []
  let off: (() => void) | null = null
  let rt: WebGLRenderTarget | null = null
  const offTask = register('overlay', 'perf.quality.mask', (ctx: FrameCtx) => {
    if (!off) {
      const pc = activePointCloud()
      if (pc) off = pc.on('pc.quality.sample', (e: { k: number }) => pending.push(e.k))
    }
    if (pending.length === 0 || !vp.scene || !ctx.camera) return
    const k = pending.shift()!
    const s = be.tier === 'S' ? 1 : be.cloudScale
    const w = Math.max(1, Math.round(ctx.dbW * s))
    const h = Math.max(1, Math.round(ctx.dbH * s))
    if (!rt || rt.width !== w || rt.height !== h) {
      rt?.dispose()
      rt = be.createRT('quality', { width: w, height: h }) as WebGLRenderTarget
    }
    const r = be.renderer
    const cam = ctx.camera
    const mask0 = cam.layers.mask
    const cc = r.getClearAlpha()
    r.setRenderTarget(rt)
    r.setClearAlpha(0)
    r.clear()
    cam.layers.mask = 1 << CH_CLOUD
    r.render(vp.scene, cam)
    cam.layers.mask = mask0
    r.setRenderTarget(null)
    r.setClearAlpha(cc)
    const buf = new Uint8Array(w * h * 4)
    void be.readPixels(rt, 0, 0, w, h, buf).then(() => {
      const sample = perfProbe().quality.samples[k]
      if (sample) sample.mask = packCoverage(buf, w, h)
    })
  })
  return () => {
    offTask()
    off?.()
    rt?.dispose()
  }
}
