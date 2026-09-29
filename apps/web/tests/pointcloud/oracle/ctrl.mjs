// g02 controllers under test (shared by offline sim and browser live test). Research-only.
// LADDER: n01 7 rungs; each rung owns a budget band [lo, hi] so an inner continuous loop can live inside it.
export const LADDER = [
  { name: 'soft-min', rs: 0.5, lo: 10_000, hi: 40_000, tau: 4.0 },
  { name: 'soft', rs: 0.6, lo: 40_000, hi: 150_000, tau: 3.0 },
  { name: 'minimum', rs: 0.6, lo: 150_000, hi: 750_000, tau: 2.7 },
  { name: 'low', rs: 0.75, lo: 750_000, hi: 1_500_000, tau: 2.0 },
  { name: 'medium', rs: 1.0, lo: 1_500_000, hi: 3_000_000, tau: 1.35 },
  { name: 'high', rs: 1.0, lo: 3_000_000, hi: 6_000_000, tau: 1.0 },
  { name: 'ultra', rs: 1.0, lo: 6_000_000, hi: 12_000_000, tau: 0.7 },
];
export const AUTO_MAX = 5;

// voxelkloud quality.ts QualityController (verbatim logic; rung budget = hi)
export class QualityController {
  constructor(start, max = AUTO_MAX) { this.index = start; this.max = max; this.iv = []; this.settle = 20; this.lastChange = 0; this.upDelay = 5000; this.lastUpIdx = -1; this.refresh = 16.7; }
  sample(dt, now) {
    if (!(dt > 0) || dt > 400) return 0;
    if (dt < this.refresh) this.refresh += (dt - this.refresh) * 0.25; else this.refresh += (dt - this.refresh) * 0.001;
    this.refresh = Math.min(Math.max(this.refresh, 6), 34);
    if (this.settle > 0) { this.settle--; return 0; }
    this.iv.push(dt); if (this.iv.length > 90) this.iv.shift(); if (this.iv.length < 90) return 0;
    const s = [...this.iv].sort((a, b) => a - b); const p50 = s[45], p95 = s[85];
    if (p50 > this.refresh * 1.35 && this.index > 0) { if (this.lastUpIdx === this.index) this.upDelay = Math.min(this.upDelay * 2, 120000); return this.move(this.index - 1, now); }
    if (this.index < this.max && p95 < this.refresh * 1.1 && now - this.lastChange > this.upDelay) { this.lastUpIdx = this.index + 1; return this.move(this.index + 1, now); }
    return 0;
  }
  move(i, now) { const d = i - this.index; this.index = i; this.lastChange = now; this.iv.length = 0; this.settle = 20; return d; }
}

// n05 AdaptiveBudget (AIMD, EMA, time-based eval)
export class AdaptiveBudget {
  constructor(o) { this.o = { alpha: 0.2, evalEveryMs: 250, hi: 1.2, lo: 1.05, up: 1.1, downFloor: 0.6, cooldownMs: 1000, ...o }; this.budget = o.initial; this.ema = undefined; this.lastEval = -1e9; this.lastDec = -1e9; }
  sample(dt, now) {
    const o = this.o; if (!(dt > 0) || dt > 5000) return this.budget; dt = Math.min(dt, 1000);
    this.ema = this.ema === undefined ? dt : this.ema + o.alpha * (dt - this.ema);
    if (now - this.lastEval < o.evalEveryMs) return this.budget; this.lastEval = now;
    const r = this.ema / o.targetMs;
    if (r > o.hi) { const f = Math.min(0.9, Math.max(o.downFloor, 1 / r)); this.budget = Math.max(o.min, Math.floor(this.budget * f)); this.lastDec = now; }
    else if (r < o.lo && now - this.lastDec > o.cooldownMs) this.budget = Math.min(o.max, Math.ceil(this.budget * o.up));
    return this.budget;
  }
}

// Proposed cascade ("G2-CAS"): inner log-domain AIMD on B inside the rung band, outer rung moves ONLY on inner saturation.
// Target T* is the tier target frame interval (Tier S 33.3 ms; hardware = estimated refresh), never the vsync-pinned refresh on Tier S.
export class CascadeController {
  constructor({ start, targetMs, max = AUTO_MAX, minIndex = 0, tailK = 1.6 }) {
    this.index = start; this.max = max; this.minIndex = minIndex; this.T = targetMs; this.tailK = tailK;
    this.B = LADDER[start].hi; this.win = []; this.lastEval = -1e9; this.good = 0; this.satLoSince = -1; this.satHiSince = -1;
    this.lastChange = -1e9; this.upDelay = 5000; this.lastUp = -1e9; this.onT = 0;
  }
  get rung() { return LADDER[this.index]; }
  // dt: presentation interval; workMs: optional GPU/frame work time (timer query) — lets the loop see headroom under vsync
  // oxlint-disable-next-line no-useless-default-assignment -- verbatim prototype code (M05 oracle copy)
  sample(dt, now, pending = false, workMs = undefined) {
    if (!(dt > 0) || dt > 1000) return 0;
    this.win.push(dt); if (this.win.length > 24) this.win.shift();
    if (workMs !== undefined) { (this.ww || (this.ww = [])).push(workMs); if (this.ww.length > 24) this.ww.shift(); }
    if (now - this.lastEval < 250 || this.win.length < 6) return 0;
    this.lastEval = now;
    const s = [...this.win].sort((a, b) => a - b); const p50 = s[s.length >> 1], p90 = s[Math.floor(s.length * 0.9)], p95 = s[Math.floor(s.length * 0.95)];
    const wm = this.ww && this.ww.length >= 6 ? [...this.ww].sort((a, b) => a - b)[this.ww.length >> 1] : undefined;
    const r = p50 / this.T;                                   // inner loop always on presentation interval (what the user sees)
    const tail = p90 > this.tailK * this.T;                          // >10% of frames missed a whole interval
    const R = this.rung; let B = this.B; let moved = 0;
    if (r > 1.10) { B *= Math.min(Math.max(Math.pow(1 / r, 0.8), 0.5), 0.92); this.good = 0; this.onT = 0; }
    else if (tail) { B *= 0.9; this.good = 0; this.onT = 0; }
    else if (r < 0.85) { this.onT = 0; if (!pending && ++this.good >= 2) B *= 1.08; }
    else { this.good = 0; if (!pending && ++this.onT >= 8) { B *= 1.03; this.onT = 0; } }   // on target: slow probe (+3% / 2 s)
    B = Math.min(Math.max(B, R.lo), R.hi);
    // saturation bookkeeping
    const atLo = B <= R.lo * 1.001, atHi = B >= R.hi * 0.999;
    this.satLoSince = atLo && r > 1.2 ? (this.satLoSince < 0 ? now : this.satLoSince) : -1;
    // headroom evidence at the band ceiling: measured work < 0.7T, or (vsync-pinned, no timer) no dropped frame in the window
    const headroom = wm !== undefined ? wm < 0.7 * this.T : (r < 0.7 || p95 <= 1.1 * this.T);
    this.satHiSince = atHi && headroom ? (this.satHiSince < 0 ? now : this.satHiSince) : -1;
    if (this.satLoSince >= 0 && now - this.satLoSince >= 1000 && this.index > this.minIndex) {
      if (now - this.lastUp < 10000) this.upDelay = Math.min(this.upDelay * 2, 120000);   // up-then-down: back off
      this.index--; B = LADDER[this.index].hi; moved = -1; this.satLoSince = -1; this.lastChange = now; this.win.length = 0;
    } else if (this.satHiSince >= 0 && now - this.satHiSince >= (r < 0.7 || wm !== undefined ? 3000 : 5000) && this.index < this.max && now - this.lastChange > this.upDelay) {
      this.index++; B = LADDER[this.index].lo; moved = 1; this.satHiSince = -1; this.lastChange = now; this.lastUp = now; this.win.length = 0;
    }
    this.B = B; return moved;
  }
}
