// M06-AC-041 in Node (AWR-14 §6.5; AWR-15 §10.11): the ViewCube has 26 unique directions (6 faces, 12 edges, 8 corners)
// with readable names; its matrix turns the cube with the camera (looking north: the South face faces the viewer, the
// Up face is on top); a region flies the camera to look from that direction keeping target and distance; north-up and
// home view tools.
import { describe, expect, it } from 'vitest'
import { Matrix4, PerspectiveCamera, Vector3 } from 'three'
import { CameraRig, enuToThree } from '@/engine'
import { allDirections, cubeMatrix, dirName, regionDir } from '@/viewport/overlay/ViewCube'

describe('ViewCube and view tools (M06-AC-041)', () => {
  it('26 unique directions with names', () => {
    const d = allDirections()
    expect(d.length).toBe(26)
    expect(dirName(regionDir('N', 0, 0))).toBe('N')
    expect(dirName(regionDir('U', 0, 0))).toBe('U')
    expect(dirName(regionDir('S', -1, -1))).toBe('U-S-W')
    expect(dirName(regionDir('E', 1, 1))).toBe('D-N-E')
  })

  it('cube matrix: camera looking north shows the South face towards the viewer', () => {
    const cam = new PerspectiveCamera()
    cam.position.copy(enuToThree(0, -100, 0, new Vector3()))
    cam.lookAt(0, 0, 0)
    cam.updateMatrixWorld()
    const m = cubeMatrix(cam.matrixWorld, new Matrix4())
    // cube space (E, -U, -N): the South face normal (0, 0, +1 in cube space) must point to the viewer (+Z in CSS)
    const s = new Vector3(0, 0, 1).applyMatrix4(m)
    expect(s.z).toBeGreaterThan(0.99)
    // the Up face normal (cube (0, -1, 0)) points up on screen (CSS -y)
    const u = new Vector3(0, -1, 0).applyMatrix4(m)
    expect(u.y).toBeLessThan(-0.99)
  })

  it('region click keeps target and distance; north up; home', () => {
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    const r = new CameraRig(cam, null, { motionTier: () => 'reduced' })
    r.lookAtEnu([100, 0, 50], [0, 0, 0])
    r.viewFrom(regionDir('N', 0, 0))
    let p = r.getPose()
    expect(p.target_enu_m.map((v) => Math.round(v))).toEqual([0, 0, 0])
    expect(Math.hypot(...p.eye_enu_m)).toBeCloseTo(Math.hypot(100, 50), 3)
    expect(p.eye_enu_m[1]).toBeGreaterThan(100) // looking from the north
    r.northUp()
    p = r.getPose()
    expect(p.eye_enu_m[1]).toBeLessThan(0) // eye south of the target: screen top = +N
    r.home = { position: [1, 2, 300], target: [1, 3, 0], fovDeg: 60 }
    r.goHome()
    expect(r.getPose().eye_enu_m.map((v) => Math.round(v))).toEqual([1, 2, 300])
  })
})
