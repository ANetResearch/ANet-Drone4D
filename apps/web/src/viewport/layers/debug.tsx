// DebugLayer container (M06-FR-029, P2 stub; V0.2 FrameTree, velocity, force and wind vectors). Owner: M06.
// Hidden by default and outside the pass plan (drawCount 0 while hidden). In dev and test builds `?debug=ray` shows the
// last pick ray as a line (one draw, counted when visible).
import { useEffect } from 'react'
import { BufferAttribute, BufferGeometry, Group, Line } from 'three'
import { LineBasicNodeMaterial } from 'three/webgpu'
import { register } from '@/engine'
import { TEST_SWITCHES } from '@/lib/testSwitches'
import { registerLayer } from './registry'
import { vp } from '../session'

export function DebugLayer() {
  useEffect(() => {
    const root = new Group()
    root.name = 'DebugLayer'
    root.visible = TEST_SWITCHES && typeof location !== 'undefined' && new URLSearchParams(location.search).get('debug') === 'ray'
    const pos = new BufferAttribute(new Float32Array(6), 3)
    const g = new BufferGeometry()
    g.setAttribute('position', pos)
    const ray = new Line(g, new LineBasicNodeMaterial())
    ray.frustumCulled = false
    root.add(ray)
    const offs = [
      registerLayer({
        id: 'debug', owner: 'M06', perfKey: 'mainJs', root, channel: 0,
        drawCount: () => (root.visible ? 1 : 0),
        setVisible: (v) => {
          root.visible = v
        },
        dispose: () => {},
      }),
      register('world', 'debug.ray', () => {
        const p = vp.picker
        if (!root.visible || !p) return
        const a = pos.array as Float32Array
        for (let i = 0; i < 3; i++) {
          a[i] = p.origin[i]
          a[3 + i] = p.origin[i] + p.dir[i] * 500
        }
        pos.needsUpdate = true
      }),
    ]
    return () => {
      for (const off of offs) off()
      g.dispose()
      ;(ray.material as LineBasicNodeMaterial).dispose()
    }
  }, [])
  return null
}
