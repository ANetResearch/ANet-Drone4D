// M05 oracle (M05 §9.3, M05-FR-056; AWR-18 §4.3 item 1): read-only copy of .cache/research/g02/lod.mjs with the two M05
// patches, both off unless requested so the unpatched prototype stays callable:
//   patch 1 (o.m05First, M05-FR-012): the root is culled and keyed by its real error instead of +inf, and only the first
//            admitted node is exempt from the budget (a prefix of B when its own points exceed B);
//   patch 2 (o.m05DeferMark, g02 §3.3): selA does not write the hysteresis marks; the caller marks the selected AND
//            resident nodes with markDrawn() after the frame's uploads (the product writes them in the DrawTable).
// Nothing else differs from the prototype.
// g02: LOD selectors under test (shared by Node sim and browser bench). Research-only.
//   selA  : voxelkloud two-tier best-first (lod/select.ts port; skip-not-break; cull-at-push; bonus tier)
//   selAP : selA + "prefix of the first budget-rejected REQUIRED node" (linear actuator, r13/r12 fractional draw)
//   selB  : r12 §4.4 target-set + segmented prefix (Potree-style break, ±10% hysteresis, centre weighting)
// Geometry is identical for all: key = spacing_L * pf(d), d = max(dist(eye, tightAABB), near), device px.

export function loadTree(meta) {
  const N = meta.nodes.length;
  const T = {
    N, meta, G: meta.G, spacingRoot: meta.spacingRoot, depth: meta.depth,
    level: new Int8Array(N), count: new Int32Array(N), offset: new Int32Array(N), parent: new Int32Array(N),
    childMask: new Uint8Array(N), children: new Int32Array(N * 8),
    cmin: new Float64Array(N * 3), csize: new Float64Array(N), tmin: new Float64Array(N * 3), tmax: new Float64Array(N * 3),
    bfsSlot: null,
  };
  for (const n of meta.nodes) {
    const i = n.i;
    T.level[i] = n.level; T.count[i] = n.count; T.offset[i] = n.offset; T.parent[i] = n.parent; T.childMask[i] = n.childMask;
    for (let k = 0; k < 8; k++) T.children[i * 8 + k] = n.children[k];
    for (let k = 0; k < 3; k++) { T.cmin[i * 3 + k] = n.cmin[k]; T.tmin[i * 3 + k] = n.tmin[k]; T.tmax[i * 3 + k] = n.tmax[k]; }
    T.csize[i] = n.csize;
  }
  return T;
}

export const spacingAt = (T, L) => T.spacingRoot / (1 << L);

// ---------------- camera ----------------
export function makeCamera(eye, tgt, { fovY = 60, W = 1280, H = 720, near = 1, far = 20000 } = {}) {
  const fx = tgt[0] - eye[0], fy = tgt[1] - eye[1], fz = tgt[2] - eye[2];
  const fl = Math.hypot(fx, fy, fz); const f = [fx / fl, fy / fl, fz / fl];
  let r = [f[1] * 1 - f[2] * 0, f[2] * 0 - f[0] * 1, 0]; // f x up(0,0,1)
  let rl = Math.hypot(r[0], r[1], r[2]); if (rl < 1e-9) { r = [1, 0, 0]; rl = 1; }
  r = [r[0] / rl, r[1] / rl, r[2] / rl];
  const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
  // view (column-major)
  const V = new Float64Array(16);
  V[0] = r[0]; V[4] = r[1]; V[8] = r[2]; V[12] = -(r[0] * eye[0] + r[1] * eye[1] + r[2] * eye[2]);
  V[1] = u[0]; V[5] = u[1]; V[9] = u[2]; V[13] = -(u[0] * eye[0] + u[1] * eye[1] + u[2] * eye[2]);
  V[2] = -f[0]; V[6] = -f[1]; V[10] = -f[2]; V[14] = (f[0] * eye[0] + f[1] * eye[1] + f[2] * eye[2]);
  V[15] = 1;
  const slope = Math.tan((fovY * Math.PI) / 360); const a = W / H;
  const P = new Float64Array(16);
  P[0] = 1 / (slope * a); P[5] = 1 / slope; P[10] = -(far + near) / (far - near); P[11] = -1; P[14] = (-2 * far * near) / (far - near);
  const C = mul4(P, V);
  const planes = new Float64Array(24);
  const row = (k) => [C[k], C[4 + k], C[8 + k], C[12 + k]];
  const r0 = row(0), r1 = row(1), r2 = row(2), r3 = row(3);
  const pl = [add(r3, r0), sub(r3, r0), add(r3, r1), sub(r3, r1), add(r3, r2), sub(r3, r2)];
  for (let k = 0; k < 6; k++) { const p = pl[k]; const l = Math.hypot(p[0], p[1], p[2]); for (let j = 0; j < 4; j++) planes[k * 4 + j] = p[j] / l; }
  return { eye, fwd: f, V, P, C, planes, slope, W, H, near, far };
}
function add(a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3]]; }
function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2], a[3] - b[3]]; }
export function mul4(A, B) { // column-major A*B
  const o = new Float64Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
    let s = 0; for (let k = 0; k < 4; k++) s += A[k * 4 + r] * B[c * 4 + k]; o[c * 4 + r] = s;
  }
  return o;
}

// 0 outside, 1 intersecting, 2 inside
export function classify(pl, x0, y0, z0, x1, y1, z1) {
  let inside = true;
  for (let k = 0; k < 24; k += 4) {
    const a = pl[k], b = pl[k + 1], c = pl[k + 2], d = pl[k + 3];
    const dmax = a * (a > 0 ? x1 : x0) + b * (b > 0 ? y1 : y0) + c * (c > 0 ? z1 : z0) + d;
    if (dmax < 0) return 0;
    const dmin = a * (a > 0 ? x0 : x1) + b * (b > 0 ? y0 : y1) + c * (c > 0 ? z0 : z1) + d;
    if (dmin < 0) inside = false;
  }
  return inside ? 2 : 1;
}

function distAabb(T, i, e) {
  const o = i * 3;
  const dx = Math.max(T.tmin[o] - e[0], 0, e[0] - T.tmax[o]);
  const dy = Math.max(T.tmin[o + 1] - e[1], 0, e[1] - T.tmax[o + 1]);
  const dz = Math.max(T.tmin[o + 2] - e[2], 0, e[2] - T.tmax[o + 2]);
  return Math.hypot(dx, dy, dz);
}
export function keyPx(T, cam, i, L = T.level[i]) {
  const d = Math.max(distAabb(T, i, cam.eye), cam.near);
  return spacingAt(T, L) * (0.5 * cam.H) / (cam.slope * d);
}

// ---------------- heap (parallel typed arrays, max-heap) ----------------
export function makeScratch(N) {
  return { hn: new Int32Array(N * 2), hk: new Float64Array(N * 2), hc: new Uint8Array(N * 2), epoch: new Int32Array(N), frame: 0,
    prevDrawn: new Int32Array(N).fill(-10) };
}
function hpush(S, n, i, k, c) {
  let j = n++; const hn = S.hn, hk = S.hk, hc = S.hc;
  while (j > 0) { const p = (j - 1) >> 1; if (hk[p] >= k) break; hn[j] = hn[p]; hk[j] = hk[p]; hc[j] = hc[p]; j = p; }
  hn[j] = i; hk[j] = k; hc[j] = c; return n;
}
function hpop(S, n) {
  const hn = S.hn, hk = S.hk, hc = S.hc;
  S.pn = hn[0]; S.pk = hk[0]; S.pc = hc[0];
  n--; if (n === 0) return 0;
  const li = hn[n], lk = hk[n], lc = hc[n]; let j = 0;
  for (;;) {
    let c = 2 * j + 1; if (c >= n) break;
    if (c + 1 < n && hk[c + 1] > hk[c]) c++;
    if (hk[c] <= lk) break;
    hn[j] = hn[c]; hk[j] = hk[c]; hc[j] = hc[c]; j = c;
  }
  hn[j] = li; hk[j] = lk; hc[j] = lc; return n;
}

export function makeSelection(N) {
  return { idx: new Int32Array(N), cnt: new Int32Array(N), n: 0, points: 0, limitedBy: 'complete', achieved: 0, frame: 0 };
}

// ---------------- selA / selAP ----------------
export function selA(T, cam, o, S, out, prefix = false, hyst = 0) {
  const frame = ++S.frame;
  const tau = o.tau, floorPx = o.tauMin ?? tau / 4, B = o.B, bonus = Math.floor(B * (1 - (o.h ?? 0.15)));
  const maxNodes = o.maxNodes ?? 4096, maxSkips = o.maxSkips ?? 32;
  const pl = cam.planes;
  let heap;
  if (o.m05First) heap = classify(pl, T.tmin[0], T.tmin[1], T.tmin[2], T.tmax[0], T.tmax[1], T.tmax[2]) === 0 ? 0 : hpush(S, 0, 0, keyPx(T, cam, 0), 1); // M05 patch 1
  else heap = hpush(S, 0, 0, Infinity, 1);
  let n = 0, pts = 0, skips = 0, worst = 0, hitB = false, hitH = false, hitF = false, hitN = false, prefixed = false;
  while (heap > 0) {
    if (n >= maxNodes) { hitN = true; break; }
    heap = hpop(S, heap);
    const i = S.pn, key = S.pk, cont = S.pc;
    const required = key >= tau;
    const cap = required ? B : bonus;
    const own = T.count[i];
    if (o.m05First && n === 0 && own > B) {                           // M05 patch 1: first node larger than B -> prefix B
      hitB = true; if (key > worst) worst = key; S.epoch[i] = frame; out.idx[0] = i; out.cnt[0] = B; n = 1; pts = B;
      continue;
    }
    if ((o.m05First ? n > 0 : i !== 0) && pts + own > cap) {           // M05 patch 1: only the first admitted node is exempt
      if (required) hitB = true; else hitH = true;
      if (key > worst) worst = key;
      if (prefix && required && !prefixed && B - pts >= 512) {       // AP: draw a shuffled prefix of the best rejected node
        prefixed = true; S.epoch[i] = frame; out.idx[n] = i; out.cnt[n++] = B - pts; pts = B;
        continue;
      }
      if (++skips > maxSkips) break;
      continue;
    }
    pts += own; S.epoch[i] = frame; out.idx[n] = i; out.cnt[n++] = own;
    const m = T.childMask[i]; if (m === 0) continue;
    for (let c = 0; c < 8; c++) {
      if (!((m >> c) & 1)) continue;
      const ch = T.children[i * 8 + c]; const b = ch * 3;
      const cc = cont === 2 ? 2 : classify(pl, T.tmin[b], T.tmin[b + 1], T.tmin[b + 2], T.tmax[b], T.tmax[b + 1], T.tmax[b + 2]);
      if (cc === 0) continue;
      let ck = keyPx(T, cam, ch);
      if (hyst) ck *= S.prevDrawn[ch] === frame - 1 ? 1 + hyst : 1 - hyst;   // APH: sticky thresholds for last frame's set
      if (ck < floorPx) { hitF = true; if (ck > worst) worst = ck; continue; }
      if (ck < tau && pts >= bonus) { hitH = true; if (ck > worst) worst = ck; continue; }
      heap = hpush(S, heap, ch, ck, cc);
    }
  }
  if (heap > 0 && S.hk[0] > worst) worst = S.hk[0];
  if (hyst && !o.m05DeferMark) for (let k = 0; k < n; k++) S.prevDrawn[out.idx[k]] = frame;   // M05 patch 2: deferred
  out.n = n; out.points = pts; out.frame = frame; out.achieved = worst;
  out.limitedBy = hitN ? 'nodes' : hitB ? 'budget' : hitH ? 'headroom' : hitF ? 'error' : 'complete';
  return out;
}

// M05 patch 2: hysteresis marks for the selected nodes that are resident (resident[i] != 0) after the frame's uploads
export function markDrawn(S, out, resident) {
  for (let k = 0; k < out.n; k++) if (resident[out.idx[k]]) S.prevDrawn[out.idx[k]] = out.frame;
}

// ---------------- selB (r12 §4.4) ----------------
export function selB(T, cam, o, S, out, resident = null) {
  const frame = ++S.frame; const tau = o.tau, B = o.B; const pl = cam.planes;
  let heap = hpush(S, 0, 0, Infinity, 1);
  let n = 0, used = 0, worst = 0, limited = 'complete';
  const e = cam.eye;
  while (heap > 0) {
    heap = hpop(S, heap);
    const i = S.pn; const L = T.level[i]; const b = i * 3;
    if (L > 1 && classify(pl, T.tmin[b], T.tmin[b + 1], T.tmin[b + 2], T.tmax[b], T.tmax[b + 1], T.tmax[b + 2]) === 0) continue;
    const k = T.count[i];
    if (used + k > B) {
      const res = resident ? resident[i] : true;
      if (res && B - used >= 512) { S.epoch[i] = frame; out.idx[n] = i; out.cnt[n++] = B - used; used = B; }
      limited = 'budget'; worst = Math.max(worst, keyPx(T, cam, i));
      break;
    }
    used += k; S.epoch[i] = frame; out.idx[n] = i; out.cnt[n++] = k;
    const m = T.childMask[i]; if (m === 0) continue;
    for (let c = 0; c < 8; c++) {
      if (!((m >> c) & 1)) continue;
      const ch = T.children[i * 8 + c];
      const s = keyPx(T, cam, ch);
      const th = S.prevDrawn[ch] === frame - 1 ? 0.9 * tau : 1.1 * tau;
      if (s < th) { if (s > worst) worst = s; if (limited === 'complete') limited = 'error'; continue; }
      let w;
      const cb = ch * 3;
      if (e[0] >= T.tmin[cb] && e[0] <= T.tmax[cb] && e[1] >= T.tmin[cb + 1] && e[1] <= T.tmax[cb + 1] && e[2] >= T.tmin[cb + 2] && e[2] <= T.tmax[cb + 2]) w = Infinity;
      else {
        // centre weighting (Potree-Next): clamp(1-|ndc|,0,1)+0.5 on the child's tight centre
        const cx = 0.5 * (T.tmin[cb] + T.tmax[cb]), cy = 0.5 * (T.tmin[cb + 1] + T.tmax[cb + 1]), cz = 0.5 * (T.tmin[cb + 2] + T.tmax[cb + 2]);
        const C = cam.C; const X = C[0] * cx + C[4] * cy + C[8] * cz + C[12], Y = C[1] * cx + C[5] * cy + C[9] * cz + C[13], W = C[3] * cx + C[7] * cy + C[11] * cz + C[15];
        let wc = 0.5; if (W > 1e-6) wc = Math.min(Math.max(1 - Math.hypot(X / W, Y / W), 0), 1) + 0.5;
        w = s * wc;
      }
      heap = hpush(S, heap, ch, w, 1);
    }
  }
  for (let k = 0; k < n; k++) S.prevDrawn[out.idx[k]] = frame;
  out.n = n; out.points = used; out.frame = frame; out.achieved = worst; out.limitedBy = limited;
  return out;
}

// ---------------- octree cut (voxelkloud cut.ts) ----------------
// returns Uint8Array RGBA entries in BFS order of drawn nodes; slot map for node->bfs slot
export function buildCut(T, drawnFlag, out, slotOf) {
  const q = out.q || (out.q = new Int32Array(T.N));
  const data = out.data || (out.data = new Uint8Array(1024 * Math.ceil(T.N / 1024) * 4));
  if (!drawnFlag[0]) { data[0] = data[1] = data[2] = data[3] = 0; out.count = 1; return out; }
  let qn = 0; q[qn++] = 0; let write = 1;
  for (let qi = 0; qi < qn; qi++) {
    const i = q[qi]; let mask = 0; const first = write;
    if (slotOf) slotOf[i] = qi;
    for (let c = 0; c < 8; c++) {
      const ch = T.children[i * 8 + c]; if (ch < 0 || !drawnFlag[ch]) continue;
      mask |= 1 << c; q[qn++] = ch; write++;
    }
    const o = qi * 4; data[o] = mask; data[o + 1] = (first >>> 16) & 255; data[o + 2] = (first >>> 8) & 255; data[o + 3] = first & 255;
  }
  out.count = qn; return out;
}
