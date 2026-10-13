// D2.1 WP-11b (AWR-04 §15.8.1, §15.8.6; ADR-122; D2-AC-47, D2-AC-49 Node part): form factor of the D2-AC-47 device matrix,
// deviceClass = mobile by renderer string and pointer, the quality rows (frame rate, pixel limit, DPR, start rung, caps,
// rates, EDL), and the field budget per column.
import { describe, expect, it } from 'vitest'
import { isMobileDevice, isMobileGpu } from '@/viewport/backend/deviceClass'
import {
  capOf, fieldBudget, FIELD_BUDGETS, formFactorOf, qualityDpr, qualityEdlNow, qualityFpsNow, qualityProfile, qualityStartRung, QUALITY_ROWS,
} from '@/viewport/quality'
import { targetMsOf } from '@/viewport/hostRuntime'

describe('form factor (AWR-04 §15.8.1)', () => {
  it('the device matrix of D2-AC-47', () => {
    expect(formFactorOf(390, 844)).toBe('phone') // iPhone 14 portrait
    expect(formFactorOf(844, 390)).toBe('phone') // landscape: short side < 600
    expect(formFactorOf(412, 915)).toBe('phone') // Pixel 7
    expect(formFactorOf(1180, 820)).toBe('tablet') // iPad 10.9 landscape
    expect(formFactorOf(820, 1180)).toBe('tablet') // portrait
    expect(formFactorOf(1366, 1024)).toBe('desktop') // iPad Pro 12.9 landscape
    expect(formFactorOf(1024, 1366)).toBe('tablet')
    expect(formFactorOf(320, 640)).toBe('phone') // narrow (the shell shows its Empty)
    expect(formFactorOf(1920, 1080)).toBe('desktop')
    expect(formFactorOf(1279, 900)).toBe('tablet')
  })
})

describe('deviceClass = mobile (AWR-04 §15.8.6)', () => {
  it('mobile GPU families by renderer string', () => {
    for (const s of ['Adreno (TM) 740', 'ANGLE (Qualcomm, Adreno (TM) 650, OpenGL ES 3.2)', 'Mali-G710 MC10', 'ANGLE (ARM, Mali-G78, OpenGL ES 3.2)', 'Apple GPU',
      'PowerVR Rogue GE8320']) expect(isMobileGpu(s), s).toBe(true)
    for (const s of ['ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)', 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)',
      'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)', 'NVIDIA GeForce RTX 4090']) expect(isMobileGpu(s), s).toBe(false)
  })
  it('needs a coarse pointer', () => {
    expect(isMobileDevice('Adreno (TM) 740', true)).toBe(true)
    expect(isMobileDevice('Adreno (TM) 740', false)).toBe(false)
    expect(isMobileDevice('Apple M2', true)).toBe(false)
  })
})

describe('quality rows (AWR-04 §15.8.6)', () => {
  it('phone, tablet and desktop values', () => {
    const p = qualityProfile('phone', true)
    expect([p.fps, p.maxMegapixels, p.pointBudgetMax, p.edl, p.envLevel, p.labelsMax, p.heroMax, p.lowPolyMax, p.swarmHz, p.selectedHz])
      .toEqual([30, 1.0, 500_000, 'off', 'low', 8, 1, 32, 5, 30])
    const t = qualityProfile('tablet', true)
    expect([t.fps, t.degradedFps, t.maxMegapixels, t.pointBudgetMax, t.edl, t.labelsMax, t.heroMax, t.lowPolyMax, t.swarmHz])
      .toEqual([0, 30, 2.0, 1_000_000, 'undegraded', 16, 2, 64, 10])
    const d = qualityProfile('desktop', false)
    expect(d).toMatchObject({ fps: 0, maxMegapixels: 0, pointBudgetMax: 0, edl: 'on', envLevel: null, labelsMax: 0, swarmHz: 0, formFactor: 'desktop', mobile: false })
    // a mobile GPU on a desktop-sized screen takes the tablet row
    expect(qualityProfile('desktop', true)).toMatchObject({ ...QUALITY_ROWS.tablet, formFactor: 'desktop', mobile: true })
  })
  it('DPR: ceiling 2 and the drawing-buffer pixel limit; never above the D1 value', () => {
    const phone = qualityProfile('phone', true)
    // iPhone 14 at DPR 3: 390 x 844 CSS -> sqrt(1e6 / 329160) = 1.74
    expect(qualityDpr(3, phone, 390, 844)).toBeCloseTo(1.74, 2)
    expect(390 * 844 * qualityDpr(3, phone, 390, 844) ** 2).toBeLessThanOrEqual(1.0e6 * 1.01)
    const tab = qualityProfile('tablet', true)
    expect(1180 * 820 * qualityDpr(2, tab, 1180, 820) ** 2).toBeLessThanOrEqual(2.0e6 * 1.01)
    expect(qualityDpr(1.5, qualityProfile('desktop', false), 1920, 1080)).toBe(1.5)
    expect(qualityDpr(0.5, phone, 390, 844)).toBe(0.5) // software stays at 0.5
  })
  it('start rung, caps, frame rate and EDL rules', () => {
    expect(qualityStartRung(4, qualityProfile('phone', true))).toBe(2)
    expect(qualityStartRung(4, qualityProfile('tablet', true))).toBe(3)
    expect(qualityStartRung(4, qualityProfile('desktop', false))).toBe(4)
    expect(capOf(32, 8)).toBe(8)
    expect(capOf(32, 0)).toBe(32)
    const t = qualityProfile('tablet', true)
    expect(qualityFpsNow(t, 0)).toBe(0)
    expect(qualityFpsNow(t, 2)).toBe(30)
    expect(qualityFpsNow(qualityProfile('phone', true), 0)).toBe(30)
    expect(qualityEdlNow(t, 0)).toBe(true)
    expect(qualityEdlNow(t, 1)).toBe(false)
    expect(qualityEdlNow(qualityProfile('phone', true), 0)).toBe(false)
    expect(qualityEdlNow(qualityProfile('desktop', false), 3)).toBe(true)
    expect(targetMsOf({ deviceClass: 'iGPU', quality: qualityProfile('phone', true) }, 16.7)).toBeCloseTo(33.33, 2)
    expect(targetMsOf({ deviceClass: 'dGPU', quality: qualityProfile('desktop', false) }, 8.3)).toBe(8.3)
    expect(targetMsOf({ deviceClass: 'software', quality: qualityProfile('desktop', false) }, 16.7)).toBe(33.3)
  })
  it('field budget per column (AWR-04 §15.11.5)', () => {
    expect(fieldBudget('phone', 'B')).toEqual({ perUav: 160, perBody: 128, bodies: 6, totalPoints: 2048, circles: 32, sizePx: [2, 4] })
    expect(fieldBudget('tablet', 'B').totalPoints).toBe(4096)
    expect(fieldBudget('desktop', 'S')).toBe(FIELD_BUDGETS.desktopS)
    expect(FIELD_BUDGETS.desktopS.totalPoints).toBe(3072)
    expect(FIELD_BUDGETS.desktop.totalPoints).toBe(8192)
  })
})
