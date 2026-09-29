// M05-AC-001 / M05-AC-002 (AWR-18 §4.3 layer 1): the product APH selector against the g02 prototype, frame by frame, on
// the flight60 camera sequence (30 Hz) of shenzhen, newyork and shanghai for B in {25k, 40k, 150k, 750k, 3M} (tau of the
// matching rung), in the ideal model (everything resident) and in the g02 streaming model (first node with one request
// in flight then 4, 20 ms + 30 MB/s, abort outside the frustum after 2 frames, 20k uploads per frame, eviction
// 1.5 B -> 1.15 B; port of g02 sim.mjs). idx, cnt, n, points and limitedBy must be equal, achievedScreenError within
// 1e-9 relative. The oracle carries the two M05 patches (first-node rule, hysteresis marks for selected AND resident
// nodes); in the ideal model the unpatched prototype must agree as well.
import { describe, expect, it } from 'vitest'
import { LIMITED, newScratch, newSelection, selectVisible, type SelectOptions } from '@/engine/pointcloud/core/Selector'
import { classify, newLodCamera, type LodCamera } from '@/engine/pointcloud/core/frustum'
import { buildDrawTable, newDrawBuild } from '@/engine/pointcloud/gpu/DrawTable'
import { PC } from '@/engine/pointcloud/params'
import { fixture, flightRow, oracle, type Fixture } from './helpers'

const W = 1280
const H = 720
const FPS = 30
const CASES: [number, number][] = [[25_000, 4], [40_000, 4], [150_000, 3], [750_000, 2.7], [3_000_000, 1.35]]
const CITIES = ['shenzhen', 'newyork', 'shanghai']

interface OracleCam { eye: number[]; planes: Float64Array; slope: number; H: number; near: number }
function camAt(f: Fixture, tSec: number): OracleCam {
  const { eye, tgt } = flightRow(f.flight, Math.round(tSec * 60))
  return oracle.makeCamera(eye, tgt, { W, H, near: 1, far: 20000 }) as OracleCam
}
function productCam(c: OracleCam, out: LodCamera): LodCamera {
  out.eye[0] = c.eye[0]
  out.eye[1] = c.eye[1]
  out.eye[2] = c.eye[2]
  out.planes.set(c.planes)
  out.slope = c.slope
  out.hPx = c.H
  out.near = c.near
  out.far = 20000
  out.orthoH = 0
  return out
}

interface RunResult { frames: number; mismatch: string | null; fill: number[]; limited: number[]; drawnOverB: number }

function run(f: Fixture, B: number, tau: number, mode: 'ideal' | 'stream', patched: boolean): RunResult {
  const t = f.store
  const T = f.T as { N: number; count: Int32Array; level: Int8Array; tmin: Float64Array; tmax: Float64Array }
  t.resetStreaming()
  const N = t.N
  const S = newScratch(N)
  const sel = newSelection(PC.maxNodes)
  const OS = oracle.makeScratch(N)
  const out = oracle.makeSelection(N)
  const lc = newLodCamera()
  const o: SelectOptions = { tau, B, headroom: PC.headroom, maxNodes: PC.maxNodes, maxSkips: PC.maxSkips, minPrefix: PC.minPrefix, hysteresis: PC.hysteresis,
    depthCap: 255, tauMinFrac: PC.tauMinFrac }
  const oo = { B, tau, h: PC.headroom, m05First: patched, m05DeferMark: patched }
  const resident = new Uint8Array(N)
  const lastSel = new Int32Array(N).fill(-1e9)
  const inflight = new Map<number, number>()
  const pending: number[] = []
  const table = new Uint32Array(PC.drawTableWidth * PC.drawTableRows * 4)
  const db = newDrawBuild()
  let residentPts = 0
  const up = B <= 300_000 ? 20_000 : 500_000
  const frames = 60 * FPS
  const res: RunResult = { frames: 0, mismatch: null, fill: [], limited: [0, 0, 0, 0, 0], drawnOverB: 0 }
  for (let fr = 0; fr <= frames; fr++) {
    const now = fr / FPS
    const cam = camAt(f, now)
    productCam(cam, lc)
    selectVisible(t, lc, o, S, sel)
    oracle.selA(T, cam, oo, OS, out, true, PC.hysteresis)
    // compare
    if (res.mismatch === null) {
      let bad = sel.n !== out.n ? `n ${sel.n} != ${out.n}` : sel.points !== out.points ? `points ${sel.points} != ${out.points}`
        : LIMITED[sel.limitedBy] !== out.limitedBy ? `limitedBy ${LIMITED[sel.limitedBy]} != ${out.limitedBy}` : ''
      for (let k = 0; !bad && k < sel.n; k++) if (sel.idx[k] !== out.idx[k] || sel.cnt[k] !== out.cnt[k]) bad = `entry ${k}: ${sel.idx[k]}/${sel.cnt[k]} != ${out.idx[k]}/${out.cnt[k]}`
      const ae = Math.abs(sel.achieved - out.achieved) / Math.max(1e-12, Math.abs(out.achieved))
      if (!bad && !(ae <= 1e-9 || (sel.achieved === 0 && out.achieved === 0))) bad = `achieved ${sel.achieved} != ${out.achieved}`
      if (bad) res.mismatch = `frame ${fr}: ${bad}`
    }
    res.limited[sel.limitedBy]++
    if (sel.limitedBy <= 1) res.fill.push(sel.points / B)
    if (sel.points > B) res.drawnOverB++
    for (let k = 0; k < sel.n; k++) lastSel[sel.idx[k]] = fr
    if (mode === 'ideal') {
      for (let k = 0; k < sel.n; k++) resident[sel.idx[k]] = 1
    } else {
      for (const [i, ready] of inflight) {
        if (ready <= now) {
          inflight.delete(i)
          pending.push(i)
        }
      }
      pending.sort((a, b) => lastSel[b] - lastSel[a])
      let budgetUp = up
      while (pending.length && budgetUp > 0) {
        const i = pending.shift()!
        if (!resident[i]) {
          resident[i] = 1
          residentPts += T.count[i]
          budgetUp -= T.count[i]
        }
      }
      for (const [i] of inflight) {
        if (fr - lastSel[i] >= 2) {
          const b = 3 * i
          if (classify(lc.planes, T.tmin[b], T.tmin[b + 1], T.tmin[b + 2], T.tmax[b], T.tmax[b + 1], T.tmax[b + 2]) === 0) inflight.delete(i)
        }
      }
      let anyRes = false
      for (let k = 0; k < sel.n; k++) if (resident[sel.idx[k]]) anyRes = true
      const width = residentPts === 0 && !anyRes ? 1 : 4
      for (let k = 0; k < sel.n && inflight.size < width; k++) {
        const i = sel.idx[k]
        if (resident[i] || inflight.has(i) || pending.includes(i)) continue
        inflight.set(i, now + 0.02 + (T.count[i] * 12) / 30e6)
      }
      if (residentPts > 1.5 * B) {
        const c: number[] = []
        for (let i = 1; i < N; i++) if (resident[i] && lastSel[i] < fr) c.push(i)
        c.sort((a, b) => lastSel[a] - lastSel[b] || T.level[b] - T.level[a])
        for (const i of c) {
          if (residentPts <= 1.15 * B) break
          resident[i] = 0
          residentPts -= T.count[i]
        }
      }
    }
    // hysteresis marks: product through the DrawTable builder (selected and resident), oracle through markDrawn
    for (let i = 0; i < N; i++) t.poolBase[i] = resident[i] ? 0 : -1
    buildDrawTable(sel, t, now * 1000, 1, (x) => x, true, table, db)
    if (patched) oracle.markDrawn(OS, out, resident)
    if (db.drawn > B) res.drawnOverB++
    res.frames++
  }
  return res
}

const q = (a: number[], p: number): number => {
  if (!a.length) return Number.NaN
  const s = Float64Array.from(a).sort()
  return s[Math.min(s.length - 1, Math.floor(p * s.length))]
}

describe('APH selector vs g02 oracle (M05-AC-001)', { timeout: 120_000 }, () => {
  for (const city of CITIES) {
    describe(city, () => {
      for (const [B, tau] of CASES) {
        it(`B=${B} tau=${tau}: ideal (patched and unpatched oracle) and streaming, frame by frame`, () => {
          const f = fixture(city)
          const ideal = run(f, B, tau, 'ideal', true)
          expect(ideal.mismatch).toBeNull()
          expect(ideal.frames).toBe(1801)
          const raw = run(f, B, tau, 'ideal', false)
          expect(raw.mismatch).toBeNull()
          const stream = run(f, B, tau, 'stream', true)
          expect(stream.mismatch).toBeNull()
          // M05-AC-002 properties on the same runs
          expect(ideal.drawnOverB + stream.drawnOverB).toBe(0)
          if (B <= 250_000 && ideal.fill.length) expect(q(ideal.fill, 0.1)).toBeGreaterThanOrEqual(0.98)
          // tau-limited budgets: budget-limited frames are rare (g02 §3.2: shenzhen 750k 0 %; newyork 750k has 70 of 1801
          // frames in the oracle as well, so the bound is 5 % rather than 0)
          if (B >= 750_000) expect(ideal.limited[0] / ideal.frames).toBeLessThanOrEqual(B >= 3_000_000 ? 0 : 0.05)
        })
      }
    })
  }
})
