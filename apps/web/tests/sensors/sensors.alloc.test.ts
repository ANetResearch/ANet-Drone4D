// M13-AC-028 (Node part): the per-frame paths of engine/sensors write into preallocated arrays and allocate nothing:
// 1000 frames of update + frustumCorners + T_base_cam + projectionFor + frameRect + sample ingestion leave the V8 heap
// unchanged (used_heap_size delta below a small tolerance for the measuring code itself), and the outputs are the
// caller's arrays.
import { getHeapStatistics } from 'node:v8'
import { describe, expect, it } from 'vitest'
import { SensorCache } from '@/engine/sensors/sensorCache'
import { T_base_cam, frameRect, frustumCorners, projectionFor } from '@/engine/sensors/intrinsics'

describe('zero allocation per frame', () => {
  it('1000 frames keep the heap flat', () => {
    const c = new SensorCache()
    c.modelOf = () => 'p600'
    const views = c.sensorsOf(1)
    const cam = views[0]
    const f15 = new Float64Array(15)
    const m16 = new Float64Array(16)
    const p16 = new Float64Array(16)
    const r4 = new Float64Array(4)
    const frame = (i: number): void => {
      const t = i * 16.6
      c.pushOrientation(1, t, 0, 0, Math.sin(i * 1e-3), Math.cos(i * 1e-3))
      if (i % 6 === 0) c.onPose(1, 0, t - 5, 3, 1, 2, 3, 0, 0, Math.sin(i * 1e-3), Math.cos(i * 1e-3))
      c.update(16.6, t - 100, false)
      for (let k = 0; k < views.length; k++) {
        if (frustumCorners(views[k], 60, f15) !== f15) throw new Error('out')
      }
      if (T_base_cam(cam, m16) !== m16 || projectionFor(cam, 16 / 9, 0.2, 5000, p16) !== p16 || frameRect(cam, 16 / 9, r4) !== r4) throw new Error('out')
    }
    for (let i = 0; i < 3000; i++) frame(i) // warm up the JIT
    // background compilation may still land code objects in one window: take the best of 6 windows of 1000 frames
    // (a per-frame allocation shows in every window: 1000 x >= 64 B)
    let best = Number.POSITIVE_INFINITY
    for (let w = 0; w < 6; w++) {
      const before = getHeapStatistics().used_heap_size
      for (let i = 0; i < 1000; i++) frame(3000 + 1000 * w + i)
      best = Math.min(best, getHeapStatistics().used_heap_size - before)
    }
    expect(best).toBeLessThan(16 * 1024)
    expect(c.resolved).toBeGreaterThan(0)
  })
})
