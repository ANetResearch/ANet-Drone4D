// Point-pick pass, M06 side (M06-FR-064; M05-FR-047, FR-048; FX-WEB1): the pick window matrix narrows the frame
// projection to the 5 x 5 raster-px window around the cursor; the pick camera carries the frame camera's matrices and
// is flagged as reversed-depth when the renderer uses a reversed depth buffer, so three r186 does not rebuild its
// hand-written projection from fov/aspect on the first pick of a page (the first pick of the page then hit a point
// elsewhere on screen, perf/m05/pick.spec.ts); the raster is the drawing buffer on Tier S and the cloudRT sub-viewport
// on Tier B/A; the request resolves through M05's PointPicker (prepare -> decode).
import { describe, expect, it, vi } from 'vitest'
import { Matrix4, PerspectiveCamera, Vector4 } from 'three'

const picker = {
  prepare: vi.fn((): { id: number } | null => ({ id: 1 })),
  decode: vi.fn(async () => ({ posEnuM: Float64Array.from([1, 2, 3]), classIdx: 6, className: 'building', normal: null, hagM: 3, nodeId: 4, nodeName: 'r0', local: 9, spacingM: 0.5 })),
  end: vi.fn(),
}
vi.mock('@/engine', async (orig) => ({ ...(await orig<typeof import('@/engine')>()), activePointCloud: () => ({ picker }) }))

const { makePointPick, pickWindowMatrix, pointRaster } = await import('@/viewport/pickPass')
const { PICK_PX } = await import('@/viewport/backend/webgl2')

interface Req { camera: PerspectiveCamera; prepare(ctx: unknown): boolean; done(px: Uint8Array | null): void }
function fakeBackend(tier: 'S' | 'B', reversedZ: boolean) {
  const reqs: Req[] = []
  return { be: { tier, cloudScale: 0.6, caps: { reversedZ }, requestPick: (r: Req) => reqs.push(r) } as never, reqs }
}
function frameCamera(): PerspectiveCamera {
  const c = new PerspectiveCamera(50, 16 / 9, 0.5, 20000)
  c.position.set(10, 200, 300)
  c.lookAt(0, 0, 0)
  c.updateMatrixWorld()
  c.updateProjectionMatrix()
  return c
}

describe('pickWindowMatrix', () => {
  it('maps the centre of raster pixel (px, py) (top-left origin) to NDC 0 and the 5 px window to +-1', () => {
    const W = 640
    const H = 360
    const m = pickWindowMatrix(100, 50, W, H, new Matrix4())
    const ndc = (x: number, y: number): Vector4 => new Vector4((x / W) * 2 - 1, 1 - (y / H) * 2, 0, 1).applyMatrix4(m)
    const c = ndc(100.5, 50.5)
    expect(c.x).toBeCloseTo(0, 9)
    expect(c.y).toBeCloseTo(0, 9)
    const e = ndc(100.5 + PICK_PX / 2, 50.5 - PICK_PX / 2)
    expect(e.x).toBeCloseTo(1, 9)
    expect(e.y).toBeCloseTo(1, 9)
  })
  it('the raster is the drawing buffer on Tier S and the cloud sub-viewport on Tier B/A', () => {
    expect(pointRaster({ dbW: 640, dbH: 360 }, true, 0.6)).toEqual([640, 360])
    expect(pointRaster({ dbW: 1280, dbH: 720 }, false, 0.6)).toEqual([768, 432])
  })
})

describe('makePointPick', () => {
  for (const reversedZ of [true, false]) {
    it(`pick camera: frame matrices x window, reversedDepth ${reversedZ} like the renderer (no projection rebuild)`, async () => {
      const { be, reqs } = fakeBackend('B', reversedZ)
      const pick = makePointPick(be)
      const pending = pick(320, 180, Float64Array.from([0, 0, 100]), Float64Array.from([0, 0, -1]))
      expect(reqs).toHaveLength(1)
      const cam = reqs[0].camera
      expect(cam.reversedDepth).toBe(reversedZ)
      const src = frameCamera()
      expect(reqs[0].prepare({ camera: src, cssW: 1280, cssH: 720, dbW: 1280, dbH: 720 })).toBe(true)
      // cursor (320, 180) css -> raster (192, 108) of the 768 x 432 cloud sub-viewport
      expect(picker.prepare).toHaveBeenLastCalledWith(192, 108, expect.anything(), expect.anything())
      const want = pickWindowMatrix(192, 108, 768, 432, new Matrix4()).multiply(src.projectionMatrix)
      expect(cam.projectionMatrix.elements.map((x) => +x.toFixed(9))).toEqual(want.elements.map((x) => +x.toFixed(9)))
      expect(cam.matrixWorldInverse.equals(src.matrixWorldInverse)).toBe(true)
      expect(cam.near).toBe(src.near)
      expect(cam.far).toBe(src.far)
      // three's updateProjectionMatrix() would only run for a camera whose flag differs from the depth buffer's
      if (reversedZ) expect(cam.reversedDepth).toBe(true)
      reqs[0].done(new Uint8Array(4 * PICK_PX * PICK_PX))
      const r = await pending
      expect(r).toMatchObject({ kind: 'point', className: 'building', spacingM: 0.5 })
      expect(Array.from((r as { pointEnu: Float64Array }).pointEnu)).toEqual([1, 2, 3])
    })
  }
  it('no ticket (the ray meets no drawn node) or no read-back resolves null', async () => {
    const { be, reqs } = fakeBackend('S', true)
    const pick = makePointPick(be)
    picker.prepare.mockReturnValueOnce(null)
    const a = pick(10, 10, Float64Array.from([0, 0, 1]), Float64Array.from([0, 0, -1]))
    expect(reqs[0].prepare({ camera: frameCamera(), cssW: 1280, cssH: 720, dbW: 640, dbH: 360 })).toBe(false)
    reqs[0].done(null)
    expect(await a).toBeNull()
    expect(picker.end).toHaveBeenCalled()
  })
})
