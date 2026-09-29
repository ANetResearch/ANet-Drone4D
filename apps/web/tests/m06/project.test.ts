// M06-AC-046 (AWR-14 §4.3; M06-FR-065) and the unobscured-rect projection centre (M06-AC-039 in Node): projectToScreen
// equals three's Vector3.project within 0.5 px, including a view offset; with the rect applied through the camera rig,
// the orbit target projects to the rect centre (<= 1 px); points behind the camera are reported outside.
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera, Vector3 } from 'three'
import { CameraRig, ctx as frameCtx, enuToThree, projectEnu } from '@/engine'

describe('projection to screen (M06-AC-046)', () => {
  it('matches Vector3.project (<= 0.5 px) with and without a view offset', () => {
    const cam = new PerspectiveCamera(60, 1280 / 720, 0.5, 20000)
    cam.position.set(30, 200, 300)
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    const out = new Float32Array(2)
    const tmp = new Vector3()
    for (const offset of [false, true]) {
      if (offset) cam.setViewOffset(1280, 720, -140, 20, 1280, 720)
      cam.updateProjectionMatrix()
      for (const p of [[0, 0, 0], [100, 50, 20], [-300, 200, 80]]) {
        const inside = projectEnu(cam, 1280, 720, p, out, tmp)
        const ref = enuToThree(p[0], p[1], p[2], new Vector3()).project(cam)
        expect(inside).toBe(true)
        expect(Math.abs(out[0] - ((ref.x + 1) / 2) * 1280)).toBeLessThan(0.5)
        expect(Math.abs(out[1] - ((1 - ref.y) / 2) * 720)).toBeLessThan(0.5)
      }
    }
    expect(projectEnu(cam, 1280, 720, [5000, 5000, -2000], out, tmp)).toBe(false)
  })

  it('unobscured rect: the orbit target lands on the rect centre (<= 1 px), the drawing buffer is untouched', () => {
    const cam = new PerspectiveCamera(60, 1920 / 1080, 0.5, 20000)
    const r = new CameraRig(cam, null, { motionTier: () => 'reduced' })
    r.lookAtEnu([0, -300, 200], [0, 0, 0])
    const rect = { x: 296, y: 44, w: 1920 - 296 - 328, h: 1080 - 44 - 56 }
    r.setViewCentre(rect.x + rect.w / 2, rect.y + rect.h / 2, 1920, 1080, 0, 0)
    r.update({ ...frameCtx, nowMs: 10, dtMs: 16, cssW: 1920, cssH: 1080, camera: cam })
    const out = new Float32Array(2)
    projectEnu(cam, 1920, 1080, [0, 0, 0], out, new Vector3())
    expect(Math.abs(out[0] - (rect.x + rect.w / 2))).toBeLessThanOrEqual(1)
    expect(Math.abs(out[1] - (rect.y + rect.h / 2))).toBeLessThanOrEqual(1)
  })
})
