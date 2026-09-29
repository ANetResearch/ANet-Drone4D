// M06-AC-044 and AC-045 in Node (M06 §6.12, FR-062, FR-063): ground previews are throttled to input.rayPreviewHz (5 Hz)
// and a new request aborts the one in flight; clicks go out at once; 1 s timeout and failures give M06-E015 and a
// 'none' result; pickAt follows the order of `want` (drone before ground; ground only when asked) and resolves
// asynchronously; hover picking is limited to 20 Hz and off while the camera moves.
import { describe, expect, it, vi } from 'vitest'
import { PerspectiveCamera } from 'three'
import { GroundRay, Picker, enuToThree, type DronePoseSoA, type QueryFn } from '@/engine'
import { INPUT } from '@/lib/tokens/input.gen'

function poses(pts: number[][]): DronePoseSoA {
  const n = pts.length
  const p: DronePoseSoA = { n, agentNo: new Uint16Array(n), pos: new Float32Array(3 * n), quat: new Float32Array(4 * n), vel: new Float32Array(3 * n),
    state: new Uint8Array(n), flags: new Uint8Array(n), battery: new Uint8Array(n), hold: new Uint8Array(n), clamped: new Uint8Array(n),
    sampleT: new Float64Array(n), ageS: new Float32Array(n) }
  pts.forEach((q, i) => {
    p.agentNo[i] = i + 1
    p.pos.set(q, 3 * i)
    p.quat[4 * i + 3] = 1
  })
  return p
}

function camera(): PerspectiveCamera {
  const c = new PerspectiveCamera(60, 16 / 9, 0.5, 20000)
  c.position.copy(enuToThree(0, -100, 50, c.position))
  c.lookAt(enuToThree(0, 0, 20, c.position.clone()))
  c.updateMatrixWorld()
  c.updateProjectionMatrix()
  return c
}

describe('ground ray (M06-AC-044)', () => {
  it('previews <= 5 Hz; a new request aborts the previous one; clicks at once', async () => {
    let t = 0
    const seen: AbortSignal[] = []
    const query: QueryFn = (_w, _r, s) => {
      seen.push(s)
      return new Promise((ok, ko) => {
        s.addEventListener('abort', () => ko(new Error('aborted')))
        setTimeout(() => ok({ hit: true, point_enu_m: [1, 2, 3], surface: 'dsm', dist_m: 10 } as never), 5)
      })
    }
    const g = new GroundRay(query, () => t)
    const a = g.preview('w', [0, 0, 0], [0, 0, -1])
    t += 50 // inside 1 / 5 Hz
    expect(await g.preview('w', [0, 0, 0], [0, 0, -1])).toEqual({ kind: 'none', reason: 'throttled' })
    t += 1000 / INPUT.rayPreviewHz
    const b = g.preview('w', [0, 0, 0], [0, 0, -1]) // aborts a
    expect(await a).toMatchObject({ kind: 'none', reason: 'aborted' })
    expect(await b).toMatchObject({ kind: 'ground', surface: 'dsm' })
    expect(seen[0].aborted).toBe(true)
    const c = await g.hit('w', [0, 0, 0], [0, 0, -1]) // click: no throttle
    expect(c.kind).toBe('ground')
    expect([g.requests, g.aborted]).toEqual([3, 1])
  })

  it('1 s timeout and failures: none + M06-E015', async () => {
    vi.useFakeTimers()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const hang: QueryFn = (_w, _r, s) => new Promise((_ok, ko) => s.addEventListener('abort', () => ko(new Error('aborted'))))
      const g = new GroundRay(hang)
      const p = g.hit('w', [0, 0, 0], [0, 0, -1])
      await vi.advanceTimersByTimeAsync(1001)
      expect(await p).toMatchObject({ kind: 'none', reason: 'timeout' })
      const bad = new GroundRay(() => Promise.reject(new Error('503')))
      expect(await bad.hit('w', [0, 0, 0], [0, 0, -1])).toMatchObject({ kind: 'none', reason: 'error' })
      expect(warn.mock.calls.map((c) => String(c[0])).filter((m) => m.includes('M06-E015')).length).toBe(2)
    } finally {
      warn.mockRestore()
      vi.useRealTimers()
    }
  })
})

describe('pick orchestration (M06-AC-045)', () => {
  const cam = camera()
  const W = 1280
  const H = 720
  const centre = (): [number, number] => {
    const v = enuToThree(0, 0, 20, cam.position.clone()).project(cam)
    return [((v.x + 1) / 2) * W, ((1 - v.y) / 2) * H]
  }
  const ground: QueryFn = async () => ({ hit: true, point_enu_m: [0, 5, 0], surface: 'dsm', dist_m: 120 } as never)

  it('want order: drone first, ground only when asked; asynchronous', async () => {
    const pk = new Picker({ camera: () => cam, size: () => ({ w: W, h: H }), poses: () => poses([[0, 0, 20]]), idOf: (a) => `uav${a}`, worldId: () => 'w',
      moving: () => false, query: ground })
    const [x, y] = centre()
    const r1 = pk.pickAt(x, y, { want: ['drone', 'ground'] })
    expect(r1).toBeInstanceOf(Promise)
    expect(await r1).toMatchObject({ kind: 'drone', id: 'uav1' })
    expect(await pk.pickAt(x, y, { want: ['ground', 'drone'] })).toMatchObject({ kind: 'ground', surface: 'dsm' })
    expect(await pk.pickAt(10, 10, { want: ['drone'] })).toEqual({ kind: 'none', reason: 'miss' })
    expect(await pk.pickAt(10, 10, { want: ['drone', 'ground'] })).toMatchObject({ kind: 'ground' })
    const ac = new AbortController()
    ac.abort()
    expect(await pk.pickAt(x, y, { want: ['drone'], signal: ac.signal })).toEqual({ kind: 'none', reason: 'aborted' })
  })

  it('hover: <= 20 Hz and off while the camera moves', () => {
    let t = 0
    let moving = false
    const pk = new Picker({ camera: () => cam, size: () => ({ w: W, h: H }), poses: () => poses([[0, 0, 20]]), idOf: (a) => `uav${a}`, worldId: () => 'w',
      moving: () => moving, now: () => t })
    const [x, y] = centre()
    expect(pk.hoverAt(x, y)).toMatchObject({ kind: 'drone' })
    t += 10
    expect(pk.hoverAt(x, y)).toBeNull() // inside 1 / 20 Hz
    t += 1000 / INPUT.hoverPickHz
    moving = true
    expect(pk.hoverAt(x, y)).toBeNull()
    moving = false
    expect(pk.hoverAt(x, y)).toMatchObject({ kind: 'drone' })
  })
})
