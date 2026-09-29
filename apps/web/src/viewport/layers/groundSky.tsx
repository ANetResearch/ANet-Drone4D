// Ground grid and sky adapter (M06 §6.5, §6.7, FR-027, AC-022; AWR-15 §10.11). Owner: M06.
// Tier S: SkyQuad (full screen, renderOrder -1000, no depth, first) + ground grid = 2 draws. Tier B/A: the sky is the
// P2 composite's background (SkyQuad hidden) + ground grid = 1 draw. The grid plane sits at ground.zM - 0.5 m of the
// current world; camera matrices of the sky are refreshed in the world phase. M07 sets the horizon colour (= fog colour)
// through skyControl.setHorizon (a uniform, presets never recompile; M06 §14 item 9).
import { useEffect } from 'react'
import { Group, Mesh, PlaneGeometry } from 'three'
import { register } from '@/engine'
import type { RenderBackend } from '../renderer'
import { registerLayer } from './registry'
import { vp } from '../session'
import { GROUND, makeGridMaterial, makeGridUniforms, makeSkyQuadMaterial, makeSkyUniforms, setSkyColors, updateSkyUniforms, type SkyUniforms } from './groundSky.materials'

const skies = new Set<SkyUniforms>()
/** M07 hook: horizon (and optionally zenith) colour in linear sRGB */
export const skyControl = {
  setHorizon(rgb: readonly number[], zenith?: readonly number[]): void {
    for (const u of skies) setSkyColors(u, rgb, zenith)
  },
}

export function GroundSkyLayer({ be }: { be: RenderBackend }) {
  useEffect(() => {
    const root = new Group()
    root.name = 'GroundSky'
    const skyU = makeSkyUniforms()
    skies.add(skyU)
    if (be.composite) skies.add(be.composite.sky)
    const sky = new Mesh(new PlaneGeometry(2, 2), makeSkyQuadMaterial(skyU))
    sky.name = 'SkyQuad'
    sky.frustumCulled = false
    sky.renderOrder = -1000
    sky.visible = be.tier === 'S'
    const gridU = makeGridUniforms()
    const grid = new Mesh(new PlaneGeometry(2 * GROUND.planeHalfM, 2 * GROUND.planeHalfM), makeGridMaterial(gridU))
    grid.name = 'GroundGrid'
    grid.frustumCulled = false
    grid.renderOrder = 10
    root.add(sky, grid)
    const offs = [
      register('world', 'groundSky.update', (ctx) => {
        const cam = ctx.camera
        if (!cam) return
        updateSkyUniforms(skyU, cam)
        if (be.composite) updateSkyUniforms(be.composite.sky, cam)
        gridU.far.value = cam.far
        grid.position.z = (vp.worldCtx?.groundZ ?? 0) - GROUND.belowGroundM
      }, { layer: 'groundSky' }),
      registerLayer({
        id: 'groundSky', owner: 'M06', perfKey: 'groundSky', root, channel: 0,
        drawCount: () => (root.visible ? (sky.visible ? 1 : 0) + (grid.visible ? 1 : 0) : 0),
        warmupVariants: () => [{ object: sky, before: () => void 0, after: () => (sky.visible = be.tier === 'S') }, { object: grid }],
        setVisible: (v) => {
          root.visible = v
        },
        dispose: () => {},
      }),
    ]
    return () => {
      for (const off of offs) off()
      skies.delete(skyU)
      if (be.composite) skies.delete(be.composite.sky)
      sky.geometry.dispose()
      ;(sky.material as Mesh['material'] & { dispose(): void }).dispose()
      grid.geometry.dispose()
      ;(grid.material as Mesh['material'] & { dispose(): void }).dispose()
    }
  }, [be])
  return null
}
