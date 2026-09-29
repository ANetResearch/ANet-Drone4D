// Anchor determinism on the TS side (M07-AC-011; M07-NFR-013): tests/environment/fixtures/anchors_1h.json holds the
// server change frames of a 1 h random env/set and preset sequence and the server anchors at checkpoints. From the
// frame valid at each checkpoint, the TS grid integration must land within 1e-6 m (S, D, fall) and 1e-9 (wetness,
// puddle) of the server. The whole hour is also replayed frame by frame (version switch at t_apply, anchors reset to
// the frame's anchors) as EnvStore does in forward play.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { advance, backstep, H_NS, partial } from '@/engine/environment/state/anchors'
import { copyAnchors, decodeKeyframe, newAnchors, type EnvKeyframeWire } from '@/engine/environment/state/keyframe'

const ROOT = new URL('../../../../', import.meta.url)
const fx = JSON.parse(readFileSync(new URL('tests/environment/fixtures/anchors_1h.json', ROOT), 'utf8')) as {
  frames: EnvKeyframeWire[]
  checkpoints: { t_ns: number; s_m: number; d_enu_m: number[]; fall_rain_m: number; fall_snow_m: number; wetness: number; puddle: number }[]
}
const frames = fx.frames.map((w) => decodeKeyframe(w))

function check(A: ReturnType<typeof newAnchors>, cp: (typeof fx.checkpoints)[number]): void {
  expect(Math.abs(A.sM - cp.s_m)).toBeLessThanOrEqual(1e-6)
  for (let j = 0; j < 3; j++) expect(Math.abs(A.d[j] - cp.d_enu_m[j])).toBeLessThanOrEqual(1e-6)
  expect(Math.abs(A.fallRain - cp.fall_rain_m)).toBeLessThanOrEqual(1e-6)
  expect(Math.abs(A.fallSnow - cp.fall_snow_m)).toBeLessThanOrEqual(1e-6)
  expect(Math.abs(A.wetness - cp.wetness)).toBeLessThanOrEqual(1e-9)
  expect(Math.abs(A.puddle - cp.puddle)).toBeLessThanOrEqual(1e-9)
}

describe('anchors (M07-AC-011)', () => {
  it('from the valid frame to every checkpoint', () => {
    for (const cp of fx.checkpoints) {
      const kf = frames.filter((f) => f.tApplyNs <= cp.t_ns).at(-1)!
      const A = copyAnchors(kf.anchors, newAnchors())
      advance(A, kf, Math.floor(A.tNs / H_NS), Math.floor(cp.t_ns / H_NS))
      check(A, cp)
    }
  })

  it('forward play over the whole hour with version switches', () => {
    const A = copyAnchors(frames[0].anchors, newAnchors())
    let fi = 0
    let k = 0
    for (const cp of fx.checkpoints) {
      const kEnd = cp.t_ns / H_NS
      while (k < kEnd) {
        const next = frames[fi + 1]
        const kSwitch = next ? next.tApplyNs / H_NS : Number.POSITIVE_INFINITY
        const kTo = Math.min(kEnd, kSwitch)
        advance(A, frames[fi], k, kTo)
        k = kTo
        if (k === kSwitch) {
          fi++
          copyAnchors(frames[fi].anchors, A)
        }
      }
      check(A, cp)
    }
  })

  it('partial inside a cell and backward trapezoid', () => {
    const kf = frames[3]
    const A = copyAnchors(kf.anchors, newAnchors())
    const k0 = A.tNs / H_NS
    advance(A, kf, k0, k0 + 10)
    const B = copyAnchors(A, newAnchors())
    backstep(B, kf, k0 + 10, k0)
    expect(Math.abs(B.sM - kf.anchors.sM)).toBeLessThan(1e-9)
    const mid = partial(A, kf, A.tNs + H_NS / 2, newAnchors())
    expect(mid.sM).toBeGreaterThanOrEqual(A.sM)
  })
})
