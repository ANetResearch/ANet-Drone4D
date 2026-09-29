// M06-AC-043 (AWR-14 §6.6; M06 §6.12): 1000 random vehicles x 1000 random clicks agree with the brute-force reference;
// the hot zone is >= 12 CSS px (6 px radius); hidden vehicles are skipped; hover picking is rate-limited to 20 Hz and off
// while the camera moves. Timing is reported (the 0.2 ms bound is a performance criterion run in the acceptance phase).
import { describe, expect, it } from 'vitest'
import { PerspectiveCamera } from 'three'
import { newDronePoseSoA, pickDroneBrute, pickDroneRay, Picker } from '@/engine'

let s = 99
const rnd = (): number => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296

describe('drone picking (M06-AC-043)', () => {
  it('matches the brute-force reference on 1000 vehicles x 1000 rays', () => {
    const p = newDronePoseSoA(1024)
    p.n = 1000
    for (let i = 0; i < 1000; i++) {
      p.agentNo[i] = i
      p.pos[3 * i] = (rnd() - 0.5) * 2000
      p.pos[3 * i + 1] = (rnd() - 0.5) * 2000
      p.pos[3 * i + 2] = rnd() * 300
    }
    const k = (2 * Math.tan(Math.PI / 6)) / 720
    const out = { index: -1, t: 0 }
    let hits = 0
    const t0 = performance.now()
    for (let j = 0; j < 1000; j++) {
      const o = [(rnd() - 0.5) * 2000, (rnd() - 0.5) * 2000, 400]
      // aim at a random vehicle with a random miss of up to 8 m so both hits and misses occur
      const tgt = Math.floor(rnd() * 1000)
      const d = [p.pos[3 * tgt] + (rnd() - 0.5) * 16 - o[0], p.pos[3 * tgt + 1] + (rnd() - 0.5) * 16 - o[1], p.pos[3 * tgt + 2] - o[2]]
      const l = Math.hypot(d[0], d[1], d[2])
      const dir = [d[0] / l, d[1] / l, d[2] / l]
      pickDroneRay(o, dir, p, k, 0.5, out)
      expect(out.index).toBe(pickDroneBrute(o, dir, p.pos, p.n, k, 0.5))
      if (out.index >= 0) hits++
    }
    const perPick = (performance.now() - t0) / 1000
    expect(hits).toBeGreaterThan(100)
    expect(hits).toBeLessThan(1000)
    expect(perPick).toBeLessThan(5) // functional sanity; the 0.2 ms bound is checked with the performance lock
  })

  it('hot zone >= 12 CSS px at distance; hidden vehicles skipped', () => {
    const p = newDronePoseSoA(4)
    p.n = 1
    p.agentNo[0] = 5
    p.pos.set([0, 1000, 0])
    const k = (2 * Math.tan(Math.PI / 6)) / 720 // 1000 m away: 1 CSS px = 1.6 m
    const out = { index: -1, t: 0 }
    const miss = 5.9 * k * 1000 // 5.9 px off-axis
    const dir = [miss / 1000, 1, 0]
    const l = Math.hypot(dir[0], dir[1])
    pickDroneRay([0, 0, 0], [dir[0] / l, dir[1] / l, 0], p, k, 0.5, out)
    expect(out.index).toBe(0)
    pickDroneRay([0, 0, 0], [dir[0] / l, dir[1] / l, 0], p, k, 0.5, out, 0.6, new Uint8Array([1]))
    expect(out.index).toBe(-1)
  })

  it('hover picking: <= 20 Hz and none while the camera moves', () => {
    let now = 0
    let moving = false
    const cam = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
    cam.position.set(0, 0, 0)
    cam.lookAt(0, 0, -1)
    cam.updateMatrixWorld()
    const p = newDronePoseSoA(4)
    p.n = 1
    p.agentNo[0] = 2
    p.pos.set([0, 100, 0])
    const picker = new Picker({
      camera: () => cam, size: () => ({ w: 1280, h: 720 }), poses: () => p, idOf: (a) => `uav${a}`, worldId: () => 'shenzhen', moving: () => moving,
      now: () => now,
    })
    expect(picker.hoverAt(640, 360)?.kind).toBe('drone')
    now += 10
    expect(picker.hoverAt(640, 360)).toBeNull() // < 50 ms
    now += 60
    moving = true
    expect(picker.hoverAt(640, 360)).toBeNull()
    moving = false
    now += 60
    expect(picker.hoverAt(10, 10)?.kind).toBe('none')
  })
})
