// Point-cloud point picking, M06 side (M06-FR-063, FR-064; M05-FR-047, FR-048; PRD-FR-017). Owner: M06 (FX-WEB1).
// Picker.pickAt(..., {want: [..., 'point', ...]}) calls pickPoint(cssX, cssY, rayOrigin, rayDir): one request is queued on
// the render backend; in the render phase of the next frame (after the world phase built that frame's DrawTable) M05's
// PointPicker.prepare() builds the pick sub-table for the DrawTable entries whose tight box meets the ray, the pick
// camera is the frame camera with its projection narrowed to the 5 x 5 raster-px window around the cursor (the raster
// of the point pass: the drawing buffer on Tier S, the cloudRT sub-viewport db x cloudScale on Tier B/A, so gl_PointSize
// is the same as on screen), the backend renders CH_PICK into pickRT (one draw, counted in the pass plan), reads it back
// asynchronously and PointPicker.decode() resolves the point from the CPU cache. One request at a time (a newer one
// resolves the older with null); 2 s without a rendered frame resolves null (M05-FR-048). No allocation per frame.
import { Matrix4, PerspectiveCamera } from 'three'
import { activePointCloud, type FrameCtx, type PickResult, type PointPick } from '@/engine'
import { CH_PICK } from './layers/registry'
import { PICK_PX, type RenderBackend } from './backend/webgl2'

export const POINT_PICK = { timeoutMs: 2000 } as const

/** clip-space window matrix: maps the PICK_PX-wide window centred on raster pixel (px, py) (top-left origin) onto NDC */
export function pickWindowMatrix(px: number, py: number, W: number, H: number, out: Matrix4): Matrix4 {
  const cx = ((px + 0.5) / W) * 2 - 1
  const cy = 1 - ((py + 0.5) / H) * 2
  const sx = W / PICK_PX
  const sy = H / PICK_PX
  return out.set(sx, 0, 0, -sx * cx, 0, sy, 0, -sy * cy, 0, 0, 1, 0, 0, 0, 0, 1)
}

/** raster size of the point pass for a frame (Tier S: drawing buffer; Tier B/A: cloudRT sub-viewport) */
export function pointRaster(ctx: Pick<FrameCtx, 'dbW' | 'dbH'>, tierS: boolean, cloudScale: number): [number, number] {
  if (tierS) return [Math.max(1, ctx.dbW), Math.max(1, ctx.dbH)]
  return [Math.max(1, Math.round(ctx.dbW * cloudScale)), Math.max(1, Math.round(ctx.dbH * cloudScale))]
}

export type PointPickResult = Extract<PickResult, { kind: 'point' }>

/** PointPick of M05 -> the Picker's 'point' result */
export function toPickResult(p: PointPick): PointPickResult {
  return { kind: 'point', nodeId: p.nodeId, pointEnu: p.posEnuM, classIdx: p.classIdx, className: p.className, hagM: p.hagM, normal: p.normal, spacingM: p.spacingM }
}

/** the pickPoint dependency of the Picker for a render backend */
export function makePointPick(be: RenderBackend): (cssX: number, cssY: number, o: Float64Array, d: Float64Array) => Promise<PickResult | null> {
  const cam = new PerspectiveCamera()
  cam.matrixAutoUpdate = false
  cam.matrixWorldAutoUpdate = false
  cam.layers.set(CH_PICK)
  // the projection is written by hand (frame camera x pick window); with a reversed depth buffer three r186 rebuilds the
  // projection of a camera whose reversedDepth flag is still false (WebGLRenderer setProgram: updateProjectionMatrix()
  // from fov/aspect), which threw the first pick of a page onto a default 50 deg frustum. The frame camera already
  // carries the reversed projection, so the pick camera is marked as reversed from the start.
  ;(cam as unknown as { _reversedDepth: boolean })._reversedDepth = be.caps.reversedZ
  const win = new Matrix4()
  return (cssX, cssY, o, d) => new Promise<PickResult | null>((resolve) => {
    const engine = activePointCloud()
    if (!engine) {
      resolve(null)
      return
    }
    let settled = false
    const finish = (r: PickResult | null): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(r)
    }
    const timer = setTimeout(() => finish(null), POINT_PICK.timeoutMs)
    let ticket: ReturnType<typeof engine.picker.prepare> = null
    be.requestPick({
      camera: cam,
      prepare(ctx: FrameCtx): boolean {
        const src = ctx.camera
        if (!src || ctx.cssW <= 0 || ctx.cssH <= 0) return false
        const [W, H] = pointRaster(ctx, be.tier === 'S', be.cloudScale)
        const rx = Math.min(W - 1, Math.max(0, Math.floor((cssX / ctx.cssW) * W)))
        const ry = Math.min(H - 1, Math.max(0, Math.floor((cssY / ctx.cssH) * H)))
        ticket = engine.picker.prepare(rx, ry, o, d)
        if (!ticket) return false
        cam.near = src.near
        cam.far = src.far
        cam.matrixWorld.copy(src.matrixWorld)
        cam.matrixWorldInverse.copy(src.matrixWorldInverse)
        cam.projectionMatrix.multiplyMatrices(pickWindowMatrix(rx, ry, W, H, win), src.projectionMatrix)
        cam.projectionMatrixInverse.copy(cam.projectionMatrix).invert()
        return true
      },
      done(px: Uint8Array | null): void {
        if (!px || !ticket) {
          engine.picker.end()
          finish(null)
          return
        }
        engine.picker.decode(ticket, px, PICK_PX, PICK_PX).then((p) => finish(p ? toPickResult(p) : null), () => finish(null))
      },
    })
  })
}
