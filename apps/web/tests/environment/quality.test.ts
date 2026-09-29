// Quality state machine and precipitation anchor (M07-AC-026 unit part, M07-AC-027; M07 §6.6.3, §6.8.2 item 6),
// AWRV decoding (M07-FR-051) and the horizon colour of visual.horizon_step (M07 §8.5).
import { readFileSync } from 'node:fs'
import { Vector3 } from 'three'
import { describe, expect, it } from 'vitest'
import { PALETTE_LINEAR } from '@/lib/tokens/palette.gen'
import { SCENE } from '@/lib/tokens/scene.gen'
import { EnvQuality, ENV_TIERS, PrecipAnchor, arrowSpacingIdx, crc32, decodeAWRV, horizonColor } from '@/engine/environment'

describe('EnvQuality', () => {
  it('Tier S: 2 levels Low -> Off, fog kept by design; manual cap', () => {
    const q = new EnvQuality('S', 'software')
    expect(q.level).toBe('low')
    expect(q.knob.levels).toBe(2)
    expect(q.knob.step).toBe(5)
    const seen: string[] = []
    q.onChange = (l) => seen.push(l)
    q.knob.apply(1)
    expect(q.level).toBe('off')
    expect(q.reason).toBe('governor')
    expect(q.reasonKey).toBe('env.degraded.off')
    q.knob.apply(0)
    expect(q.level).toBe('low')
    q.setUserLevel('off')
    expect(q.level).toBe('off')
    expect(q.reason).toBe('user')
    q.setUserLevel('med') // capped to the start level
    expect(q.level).toBe('low')
    expect(seen).toEqual(['off', 'low', 'off', 'low'])
  })

  it('dGPU with Med delivered: 3 levels Med -> Low -> Off', () => {
    const q = new EnvQuality('B', 'dGPU', true)
    expect(q.level).toBe('med')
    expect(q.knob.levels).toBe(3)
    q.knob.apply(1)
    expect(q.level).toBe('low')
    q.knob.apply(2)
    expect(q.level).toBe('off')
  })
})

describe('PrecipAnchor', () => {
  it('camera AGL 20 -> 500 -> 20 m: 3 switches up and 3 down, no jitter, anchor continuous', () => {
    const a = new PrecipAnchor()
    const focus = [30, 0, 0]
    let prev: Float64Array | null = null
    let maxJump = 0
    const run = (z: number, t: number): void => {
      const sw = a.switches
      a.update([0, 0, z], focus, z, 0, false, t)
      // between switches the anchor follows the camera continuously; at a switch the old box keeps its own anchor
      // (prevAnchor) during the cross-fade, so no drawn drop jumps
      if (prev && sw === a.switches) maxJump = Math.max(maxJump, Math.hypot(a.anchor[0] - prev[0], a.anchor[1] - prev[1], a.anchor[2] - prev[2]))
      if (prev && sw !== a.switches) expect(Array.from(a.prevAnchor)).toEqual(Array.from(prev))
      prev = Float64Array.from(a.anchor)
    }
    let t = 0
    for (let z = 20; z <= 500; z += 0.5) run(z, (t += 16))
    const up = a.switches
    for (let z = 500; z >= 20; z -= 0.5) run(z, (t += 16))
    expect(a.level).toBe(0)
    expect(up).toBeGreaterThanOrEqual(2)
    expect(a.switches).toBe(2 * up)
    // hysteresis: oscillating around a threshold switches once, then holds
    expect(maxJump).toBeLessThan(5)
    const before = a.switches
    prev = null
    for (let i = 0; i < 50; i++) {
      a.update([0, 0, 36 + (i % 2) * 4], focus, 36 + (i % 2) * 4, 0, false, (t += 16))
    }
    expect(a.switches).toBe(before + 1)
  })

  it('Follow and FPV force octave 0 and cross-fade over --duration-very-slow', () => {
    const a = new PrecipAnchor()
    a.update([0, 0, 400], [0, 800, 0], 400, 0, false, 0)
    expect(a.level).toBeGreaterThan(0)
    a.update([0, 0, 400], [0, 800, 0], 400, 0, true, 1000)
    expect(a.level).toBe(0)
    expect(a.prevLevel).toBeGreaterThan(0)
    a.update([0, 0, 400], [0, 800, 0], 400, 0, true, 1250)
    expect(a.fade).toBeCloseTo(0.5, 2)
    a.update([0, 0, 400], [0, 800, 0], 400, 0, true, 1600)
    expect(a.prevLevel).toBe(-1)
  })

  it('arrow spacing octaves with hysteresis', () => {
    let i = 2
    i = arrowSpacingIdx(2000, i)
    expect(ENV_TIERS.arrowSpacingM[i]).toBe(160)
    i = arrowSpacingIdx(30, i)
    expect(ENV_TIERS.arrowSpacingM[i]).toBe(10)
  })
})

describe('AWRV and colours', () => {
  it('decodes the golden turbulence box and rejects corruption', () => {
    const buf = readFileSync(new URL('../../../../packages/contracts/env/golden/awrv/turb_small.awrv', import.meta.url))
    const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
    const v = decodeAWRV(ab)
    expect([v.kind, v.nx, v.ny, v.nz, v.comp, v.dtype]).toEqual([2, 16, 16, 16, 4, 1])
    expect(v.cpu!.length).toBe(16 * 16 * 16 * 4)
    const bad = new Uint8Array(ab.slice(0))
    bad[100] ^= 0xff
    expect(() => decodeAWRV(bad.buffer)).toThrow(/CRC/)
    const badMagic = new Uint8Array(ab.slice(0))
    badMagic[0] = 0
    expect(() => decodeAWRV(badMagic.buffer)).toThrow(/magic/)
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })

  it('horizon colour interpolates Graphite steps in linear sRGB and matches the preset tokens', () => {
    const v = new Vector3()
    expect(horizonColor(850, v).toArray()).toEqual([...PALETTE_LINEAR.g850])
    expect(horizonColor(600, v).toArray()).toEqual([...SCENE.skyHorizonByPreset.fog])
    expect(horizonColor(500, v).toArray()).toEqual([...SCENE.skyHorizonByPreset.blizzard])
    const mid = horizonColor(650, v).toArray()
    for (let i = 0; i < 3; i++) expect(mid[i]).toBeCloseTo((PALETTE_LINEAR.g600[i] + PALETTE_LINEAR.g700[i]) / 2, 12)
  })
})

describe('AWSL (D1-ext)', () => {
  it('decodes the Python-generated sample: offsets, 8-64 vertices per line, tau_hat increasing', async () => {
    const { decodeAWSL } = await import('@/engine/environment')
    const buf = readFileSync(new URL('../../../../tests/environment/fixtures/sample.awsl', import.meta.url))
    const s = decodeAWSL(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength))
    expect(s.nLines).toBeGreaterThan(5)
    expect(s.offsets[s.nLines]).toBe(s.nVerts)
    expect(s.dirFromDeg).toBe(250)
    for (let l = 0; l < s.nLines; l++) {
      const n = s.offsets[l + 1] - s.offsets[l]
      expect(n).toBeGreaterThanOrEqual(8)
      expect(n).toBeLessThanOrEqual(64)
      for (let v = s.offsets[l] + 1; v < s.offsets[l + 1]; v++) expect(s.verts[5 * v + 3]).toBeGreaterThan(s.verts[5 * (v - 1) + 3])
      expect(s.segStart[l + 1] - s.segStart[l]).toBe(n - 1)
    }
  })
})
