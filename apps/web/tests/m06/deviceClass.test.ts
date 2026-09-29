// M06-AC-003 (ADR-044; M06 §6.2.2): 30 renderer strings classify correctly; injected microbench t_1M 0.8 / 3.0 / 6.0 ms
// give dGPU + 1, dGPU, iGPU; start rungs, DPR per class, the 30-day cache and the FR-013 start-rung memory.
import { describe, expect, it } from 'vitest'
import {
  CACHE_KEY, classifyByMicrobench, classifyByName, dprFor, isSoftware, lowestRungFor, readCache, rememberFloorHeld, rendererKey, startRungFor,
  writeCache, type DeviceCache,
} from '@/viewport/backend/deviceClass'

const SOFTWARE = [
  'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)',
  'Google SwiftShader',
  'llvmpipe (LLVM 15.0.7, 256 bits)',
  'Mesa softpipe',
  'Microsoft Basic Render Driver',
  'ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'Software Rasterizer',
  'ANGLE (Mesa, llvmpipe (LLVM 17.0.6 256 bits), OpenGL 4.5)',
]
const IGPU = [
  'ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'ANGLE (Intel, Intel(R) HD Graphics 530, OpenGL 4.6)',
  'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'Mesa Intel(R) Xe Graphics (TGL GT2)',
  'ANGLE (Intel, Intel(R) Arc(TM) Graphics (0x00007D55) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001638) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'AMD Radeon Vega 8 Graphics',
  'ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)',
  'Apple M2 Pro',
  'Mali-G78 MP20',
  'Adreno (TM) 740',
  'PowerVR Rogue GE8320',
]
const UNKNOWN_HW = [
  'ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 (0x00002206) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'NVIDIA GeForce GTX 1660/PCIe/SSE2',
  'ANGLE (AMD, AMD Radeon RX 6800 XT (0x000073BF) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics (0x000056A0) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  'NVIDIA RTX A4000',
  'ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Laptop GPU, OpenGL 4.5)',
  'AMD Radeon Pro W6800',
  'Quadro P2000/PCIe/SSE2',
  'ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 2070 SUPER/PCIe/SSE2, OpenGL 4.5.0)',
  'Intel(R) Arc(TM) A380 Graphics',
]

describe('device class heuristics (M06-AC-003)', () => {
  it('classifies the 30 renderer strings', () => {
    expect(SOFTWARE.length + IGPU.length + UNKNOWN_HW.length).toBe(30)
    for (const s of SOFTWARE) expect(classifyByName(s, null), s).toBe('software')
    for (const s of IGPU) expect(classifyByName(s, null), s).toBe('iGPU')
    for (const s of UNKNOWN_HW) expect(classifyByName(s, null), s).toBeNull()
  })
  it('uses isFallbackAdapter only when the WebGL string is masked', () => {
    expect(isSoftware('', { isFallbackAdapter: true })).toBe(true)
    expect(isSoftware('ANGLE (NVIDIA, NVIDIA GeForce RTX 3080)', { isFallbackAdapter: true })).toBe(false)
    expect(isSoftware('', { vendor: 'google', architecture: 'swiftshader' })).toBe(true)
    expect(classifyByName('', { vendor: 'intel', architecture: 'gen-12lp', description: 'Intel(R) UHD Graphics 770' })).toBe('iGPU')
  })
  it('microbench thresholds: 0.8 -> dGPU + 1, 3.0 -> dGPU, 6.0 -> iGPU', () => {
    expect(classifyByMicrobench(0.8)).toEqual({ deviceClass: 'dGPU', bonus: 1 })
    expect(classifyByMicrobench(3.0)).toEqual({ deviceClass: 'dGPU', bonus: 0 })
    expect(classifyByMicrobench(6.0)).toEqual({ deviceClass: 'iGPU', bonus: 0 })
    expect(classifyByMicrobench(Number.POSITIVE_INFINITY).deviceClass).toBe('iGPU')
  })
  it('start rungs, lowest allowed rungs and canvas DPR', () => {
    expect([startRungFor('software'), startRungFor('iGPU'), startRungFor('dGPU'), startRungFor('dGPU', 1), startRungFor('dGPU', 1, 1)]).toEqual([0, 3, 4, 5, 4])
    expect(startRungFor('dGPU', 5)).toBe(5)
    expect([lowestRungFor('S'), lowestRungFor('B'), lowestRungFor('A')]).toEqual([0, 2, 2])
    expect([dprFor('software', 2), dprFor('iGPU', 2), dprFor('dGPU', 3), dprFor('dGPU', 1)]).toEqual([0.5, 1.5, 2, 1])
  })
  it('cache: 30 days per renderer string and browser major; FR-013 memory', () => {
    const m = new Map<string, string>()
    const s = { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) }
    const key = rendererKey('NVIDIA GeForce RTX 3080', null, 'Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36')
    expect(key).toBe('NVIDIA GeForce RTX 3080|151')
    const c: DeviceCache = { rendererKey: key, t1M: 1.2, deviceClass: 'dGPU', ts: 1000, startMinus: 0, demoteA: false }
    writeCache(c, s)
    expect(m.has(CACHE_KEY)).toBe(true)
    expect(readCache(key, 2000, s)?.t1M).toBe(1.2)
    expect(readCache('other|151', 2000, s)).toBeNull()
    expect(readCache(key, 1000 + 31 * 24 * 3600 * 1000, s)).toBeNull()
    const n = rememberFloorHeld(readCache(key, 2000, s), key, 'dGPU', 'B', 3000, s)
    expect(n.startMinus).toBe(1)
    expect(startRungFor('dGPU', 1, n.startMinus)).toBe(4)
    expect(rememberFloorHeld(n, key, 'dGPU', 'A', 4000, s).demoteA).toBe(true)
  })
})
