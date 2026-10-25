// navsvc 与烘焙工具的自测（node --test tools/navsvc/test/）：版本锁、烘焙确定性（线程数无关、重复烘焙字节相同）、种子裁剪、
// 查询协议与应答逐字节确定（ADR-196 决策 (1)、(3)、(6)；D3-AC-03）。夹具为程序生成的小场景，不依赖世界包。

import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')
const ROOT = path.resolve(HERE, '..', '..', '..')

/** 夹具：100 m × 100 m 缓坡平面（1 m 格）、中央 10 m × 10 m × 3 m 实心箱体、东侧 20 m 外抬高 10 m 的平台（独立岛）。 */
function fixture(dir, cfg = {}) {
  fs.mkdirSync(dir, { recursive: true })
  const V = []; const F = []; const A = []
  const n = 100
  for (let j = 0; j <= n; j++) for (let i = 0; i <= n; i++) V.push([i - 50, j - 50, 0.02 * (i - 50)])
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const a = j * (n + 1) + i; const b = a + 1; const c = a + n + 1; const d = c + 1
      const yc = j - 50 + 0.5
      const area = Math.abs(yc) < 3 ? 1 : 3
      F.push([a, b, d], [a, d, c]); A.push(area, area)
    }
  }
  const box = (x0, y0, x1, y1, z0, z1) => {
    const k = V.length
    for (const z of [z0, z1]) for (const [x, y] of [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) V.push([x, y, z])
    const q = (a, b, c, d) => { F.push([k + a, k + b, k + c], [k + a, k + c, k + d]); A.push(3, 3) }
    q(4, 5, 6, 7); q(0, 1, 5, 4); q(1, 2, 6, 5); q(2, 3, 7, 6); q(3, 0, 4, 7)
  }
  box(-5, 10, 5, 20, -0.1, 3)
  const k = V.length
  V.push([70, 0, 10], [90, 0, 10], [90, 20, 10], [70, 20, 10])
  F.push([k, k + 1, k + 2], [k, k + 2, k + 3]); A.push(3, 3)
  const verts = new Float32Array(V.length * 3)
  V.forEach(([e, nn, u], i) => { verts[3 * i] = e; verts[3 * i + 1] = u; verts[3 * i + 2] = -nn })
  fs.writeFileSync(path.join(dir, 'verts.f32'), Buffer.from(verts.buffer))
  fs.writeFileSync(path.join(dir, 'tris.u32'), Buffer.from(new Uint32Array(F.flat()).buffer))
  fs.writeFileSync(path.join(dir, 'areas.u8'), Buffer.from(new Uint8Array(A)))
  const config = {
    profile: 'person',
    recast: {
      cs: 0.3, ch: 0.2, tileSize: 64, walkableSlopeAngle: 35, walkableHeight: 9, walkableClimb: 2, walkableRadius: 1, maxEdgeLen: 40,
      maxSimplificationError: 1.3, minRegionArea: 8, mergeRegionArea: 20, maxVertsPerPoly: 6, detailSampleDist: 3, detailSampleMaxError: 0.4,
    },
    poly_flag: 1, seeds: [{ id: 'j-1', p_mm: [-30000, 0, -600] }], seed_ext_mm: [2000, 4000, 2000], offmesh: [], ...cfg,
  }
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(config))
  return dir
}

function bake(dir, out, threads) {
  const r = spawnSync(process.execPath, [path.join(SRC, 'bake.mjs'), '--in', dir, '--out', out, '--threads', String(threads)], { encoding: 'utf8' })
  assert.equal(r.status, 0, r.stdout + r.stderr)
  return JSON.parse(r.stdout.trim().split('\n').pop())
}

function layer(dir, bin, res) {
  const b = fs.readFileSync(bin)
  const doc = { profile: 'person', bin: { href: path.basename(bin), bytes: b.length, sha256: createHash('sha256').update(b).digest('hex') } }
  const f = path.join(dir, 'person.json')
  fs.writeFileSync(f, JSON.stringify(doc))
  assert.equal(doc.bin.sha256, res.bin.sha256)
  return f
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'navsvc-test-'))

test('version pins: packages = RECAST_VERSION, WASM sha256 pinned', () => {
  const want = fs.readFileSync(path.join(ROOT, 'tools', 'worldglb', 'RECAST_VERSION'), 'utf8').trim()
  const pkg = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'package.json'), 'utf8'))
  for (const n of ['core', 'generators', 'wasm']) assert.equal(pkg.dependencies[`@recast-navigation/${n}`], want)
  const lock = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'package-lock.json'), 'utf8'))
  for (const n of ['core', 'generators', 'wasm']) assert.equal(lock.packages[`node_modules/@recast-navigation/${n}`].version, want)
  const wasm = fs.readFileSync(path.join(HERE, '..', 'node_modules', '@recast-navigation', 'wasm', 'dist', 'recast-navigation.wasm.wasm'))
  assert.equal(createHash('sha256').update(wasm).digest('hex'), fs.readFileSync(path.join(HERE, '..', 'WASM_SHA256'), 'utf8').trim())
})

test('bake: deterministic across thread counts and runs; seed island kept, platform dropped', () => {
  const d = fixture(path.join(tmp, 'a'))
  const r1 = bake(d, path.join(tmp, 'a1.bin'), 1)
  const r4 = bake(d, path.join(tmp, 'a4.bin'), 4)
  const r1b = bake(d, path.join(tmp, 'a1b.bin'), 1)
  assert.equal(r1.bin.sha256, r4.bin.sha256)
  assert.equal(r1.bin.sha256, r1b.bin.sha256)
  assert.deepEqual(fs.readFileSync(path.join(tmp, 'a1.bin')), fs.readFileSync(path.join(tmp, 'a4.bin')))
  assert.equal(r1.islands.kept, 1)
  assert.ok(r1.islands.dropped >= 1)
  assert.ok(r1.islands.walkable_area_m2 > 9000 && r1.islands.walkable_area_m2 < 10000, String(r1.islands.walkable_area_m2))
  assert.ok(r1.islands.walkable_area_before_m2 > r1.islands.walkable_area_m2)
  assert.equal(r1.seeds[0].found, true)
  assert.equal(r1.seed_fallback, null)
})

test('bake: no seed snaps -> largest island kept and fallback reported', () => {
  const d = fixture(path.join(tmp, 'b'), { seeds: [{ id: 'far', p_mm: [500000, 500000, 0] }] })
  const r = bake(d, path.join(tmp, 'b.bin'), 1)
  assert.equal(r.seeds[0].found, false)
  assert.equal(r.seed_fallback, 'largest-island')
  assert.equal(r.islands.kept, 1)
})

test('navsvc: queries and byte-identical replies for the same request sequence', () => {
  const d = fixture(path.join(tmp, 'c'))
  const bin = path.join(tmp, 'c', 'person.bin')
  const res = bake(d, bin, 2)
  const prof = layer(path.join(tmp, 'c'), bin, res)
  const reqs = [
    { id: 1, op: 'snap', profile: 'person', p: [1234, -5678, 100] },
    { id: 2, op: 'path', profile: 'person', a: [-40000, -40000, 0], b: [0, 30000, 0] },
    { id: 3, op: 'los', profile: 'person', a: [0, 0, 0], b: [0, 30000, 0] },
    { id: 4, op: 'los', profile: 'person', a: [-40000, -30000, 0], b: [40000, -30000, 0] },
    { id: 5, op: 'random_point_around', profile: 'person', p: [0, 0, 0], r_mm: 5000, seed: 7 },
    { id: 6, op: 'random_point_around', profile: 'person', p: [0, 0, 0], r_mm: 5000, seed: 7 },
    { id: 7, op: 'random_point', profile: 'person', seed: 11 },
    { id: 8, op: 'height', profile: 'person', p: [10000, -10000, 500] },
    { id: 9, op: 'path', profile: 'person', a: [0, 0, 0], b: [80000, 10000, 10000] },
    { id: 10, op: 'snap', profile: 'vehicle', p: [0, 0, 0] },
    { id: 11, op: 'batch', items: [{ op: 'snap', profile: 'person', p: [0, 0, 0] }, { op: 'nope' }] },
    { id: 12, op: 'snap', profile: 'person', p: [0.5, 0, 0] },
    { id: 13, op: 'quit' },
  ]
  const input = reqs.map((r) => JSON.stringify(r)).join('\n') + '\n'
  const run = () => execFileSync(process.execPath, [path.join(SRC, 'navsvc.mjs'), '--profile', `person=${prof}`], { input })
  const o1 = run(); const o2 = run()
  assert.deepEqual(o1, o2)
  const rs = o1.toString('utf8').trim().split('\n').map((l) => JSON.parse(l))
  assert.equal(rs.length, reqs.length)
  rs.forEach((r, i) => assert.equal(r.id, reqs[i].id))
  assert.equal(rs[0].found, true)
  assert.equal(rs[0].dist_mm, 0)
  assert.equal(rs[1].complete, true)
  assert.ok(rs[1].points.length >= 3, 'path bends around the box')
  assert.ok(rs[1].length_mm > Math.hypot(40000, 70000))
  assert.equal(rs[2].clear, false, 'box blocks the line of sight')
  assert.ok(rs[2].t_ppm > 0 && rs[2].t_ppm < 1000000)
  assert.equal(rs[3].clear, true)
  assert.deepEqual(rs[4], { ...rs[5], id: 5 })
  assert.equal(rs[6].found, true)
  assert.equal(rs[7].found, true)
  assert.ok(Math.abs(rs[7].z_mm - 200) < 600, `nav height ${rs[7].z_mm} near the slope surface 200 mm`)
  assert.equal(rs[8].found, false, 'pruned platform is not a target')
  assert.equal(rs[9].ok, false)
  assert.equal(rs[9].error.code, 'NAV_UNAVAILABLE')
  assert.equal(rs[10].results[1].error.code, 'UNKNOWN_OP')
  assert.equal(rs[11].ok, false)
  assert.equal(rs[11].error.code, 'BAD_REQUEST')
})

test('core: isomorphic module (no node: imports) shared with the browser', () => {
  const src = fs.readFileSync(path.join(SRC, 'core.mjs'), 'utf8')
  const imports = [...src.matchAll(/^import[^'"]*['"]([^'"]+)['"]/gm)].map((m) => m[1])
  assert.deepEqual(imports.sort(), ['@recast-navigation/core', '@recast-navigation/wasm/wasm'])
})

test('core: axis conversion round-trips integer millimetres', async () => {
  const { toRecast, fromRecast, mulberry32 } = await import('../src/core.mjs')
  for (const p of [[0, 0, 0], [1234567, -987654, 4321], [-1, 1, -1]]) assert.deepEqual(fromRecast(toRecast(p)), p)
  const r = mulberry32(42)
  const a = [r(), r(), r()]
  const r2 = mulberry32(42)
  assert.deepEqual([r2(), r2(), r2()], a)
  assert.ok(a.every((v) => v >= 0 && v < 1))
})
