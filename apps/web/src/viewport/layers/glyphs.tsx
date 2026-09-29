// GlyphLayer adapter (M06 §6.10, FR-036, AC-027). Owner: M06.
// The drone runtime begins the glyph list in the drones phase (drone symbols), the mission overlay adds waypoints and
// the GoTo marker in the world phase, and this adapter commits the batch at the end of the world phase (order 1000):
// one draw, priority truncation at the tier cap (VIS_GLYPH_CAP counted in __perf.bench.pairs.glyphCap for tests).
import { useEffect } from 'react'
import { Group } from 'three'
import { register } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { registerLayer } from './registry'
import { useVp } from '../session'

export function GlyphsLayer() {
  const drones = useVp((s) => s.drones)
  useEffect(() => {
    if (!drones) return
    const g = drones.layer.glyphs
    const root = new Group()
    root.name = 'GlyphLayer'
    root.add(g.mesh)
    let warned = false
    const offs = [
      register('world', 'glyphs.commit', (ctx) => {
        g.commit(ctx.dpr > 0 ? ctx.dpr : 1, ctx.dbW, ctx.dbH)
        if (g.truncated > 0 && TEST_SWITCHES && !warned) {
          warned = true
          console.warn(`VIS_GLYPH_CAP M06-E014 ${g.truncated} glyphs truncated at cap ${g.cap}`)
        }
      }, { order: 1000, layer: 'trails' }),
      registerLayer({
        id: 'glyphs', owner: 'M06', perfKey: 'trails', root, channel: 0,
        caps: { S: { maxQuads: 256 }, BA: { maxQuads: 1024 } },
        drawCount: () => (root.visible ? g.drawCount() : 0),
        warmupVariants: () => [{ object: g.mesh, before: () => g.mesh.geometry.setDrawRange(0, 6), after: () => g.mesh.geometry.setDrawRange(0, 6 * g.n) }],
        setVisible: (v) => {
          root.visible = v
        },
        dispose: () => {},
      }),
    ]
    return () => {
      for (const off of offs) off()
    }
  }, [drones])
  return null
}
