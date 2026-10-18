#!/usr/bin/env node
// NavMesh 烘焙工具（M22 navmesh 阶段，§6.6.11；ADR-196 决策 (2)、(6)）：由 Python 构建流水线以子进程调用。
//
// 用法：node tools/navsvc/src/bake.mjs --in <dir> --out <profile.bin> [--threads N]
// 输入目录（Python awr.world.glb.derive.navmesh 写出）：
//   config.json   {profile, recast{cs, ch, tileSize, walkableSlopeAngle, walkableHeight, walkableClimb, walkableRadius, maxEdgeLen,
//                  maxSimplificationError, minRegionArea, mergeRegionArea, maxVertsPerPoly, detailSampleDist, detailSampleMaxError},
//                  poly_flag, seeds[{id, p_mm}], seed_ext_mm, keep（seeded | largest-seeded）, offmesh[{id, a_mm, b_mm, radius_mm, bidir}]}
//   verts.f32     Recast 轴向 (E, U, −N) 的 float32 顶点（小端）
//   tris.u32      三角形顶点下标（小端）
//   areas.u8      逐三角形 Detour 区域号（0 为不可走）
// 输出：`--out` 写 exportNavMesh 字节（种子洪泛裁剪后：不可达多边形 flags = 0）；标准输出一行 JSON 统计。
// 确定性：瓦片按 (ty, tx) 行主序装配，与线程数无关；统计中的耗时、RSS、线程数与 Node 版本不进入图层清单。

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Worker } from 'node:worker_threads'

import {
  Detour, NavMesh, NavMeshParams, NavMeshQuery, QueryFilter, UnsignedCharArray, exportNavMesh, statusFailed,
} from '@recast-navigation/core'

import { MAX_NODES, polyArea, toRecast } from './core.mjs'
import { initRecast, sha256 } from './recast.mjs'
import { TileBaker, partition, tileGrid } from './tiles.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))

function args(argv) {
  const o = { threads: 1 }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    if (k === '--in') o.in = argv[++i]
    else if (k === '--out') o.out = argv[++i]
    else if (k === '--threads') o.threads = Math.max(1, Number.parseInt(argv[++i], 10) || 1)
    else throw new Error(`unknown argument ${k}`)
  }
  if (!o.in || !o.out) throw new Error('usage: bake.mjs --in <dir> --out <bin> [--threads N]')
  return o
}

function readTyped(file, Ctor, shared) {
  const b = fs.readFileSync(file)
  const n = b.byteLength / Ctor.BYTES_PER_ELEMENT
  const buf = shared ? new SharedArrayBuffer(b.byteLength) : new ArrayBuffer(b.byteLength)
  const out = new Ctor(buf, 0, n)
  out.set(new Ctor(b.buffer, b.byteOffset, n))
  return out
}

function toShared(a) {
  const buf = new SharedArrayBuffer(a.byteLength)
  const out = new a.constructor(buf, 0, a.length)
  out.set(a)
  return out
}

/** 逐瓦片烘焙：threads = 1 在本线程执行；否则 worker_threads 动态取瓦片号，结果按瓦片号归位。 */
async function bakeTiles(job, nTiles, threads) {
  const tiles = Array.from({ length: nTiles }, () => null)
  const todo = []
  for (let i = 0; i < nTiles; i++) if (job.offsets[i + 1] > job.offsets[i]) todo.push(i)
  if (threads <= 1 || todo.length < 2) {
    const b = new TileBaker(job)
    for (const i of todo) tiles[i] = b.build(i)
    b.destroy()
    return tiles
  }
  let next = 0
  const n = Math.min(threads, todo.length)
  await Promise.all(Array.from({ length: n }, () => new Promise((resolve, reject) => {
    const w = new Worker(path.join(HERE, 'bake_worker.mjs'), { workerData: job })
    const feed = () => {
      if (next < todo.length) w.postMessage(todo[next++])
      else w.postMessage(-1)
    }
    w.on('message', (m) => {
      if (m.ready) return feed()
      if (m.error) {
        w.terminate()
        return reject(new Error(m.error))
      }
      tiles[m.i] = m.data
      feed()
    })
    w.on('error', reject)
    w.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`bake worker exit ${code}`))))
  })))
  return tiles
}

/** 多边形连通分量（经 Detour 链接，含 off-mesh 连接）：返回每个多边形的分量号与分量面积。 */
function islands(navMesh, polyBits) {
  const mod = 2 ** polyBits
  const base = new Map()
  const refs = []
  const area = []
  const tilesOf = []
  for (let ti = 0; ti < navMesh.getMaxTiles(); ti++) {
    const tile = navMesh.getTile(ti)
    const h = tile.header()
    if (!h) continue
    const b = navMesh.getPolyRefBase(tile)
    base.set(b, refs.length)
    for (let i = 0; i < h.polyCount(); i++) {
      const poly = tile.polys(i)
      refs.push(b + i)
      area.push(poly.getType() === 0 ? polyArea(tile, poly) : 0)
      tilesOf.push(ti)
    }
  }
  const idx = (ref) => {
    const pi = ref % mod
    const s = base.get(ref - pi)
    return s === undefined ? -1 : s + pi
  }
  const comp = new Int32Array(refs.length).fill(-1)
  const compArea = []
  const stack = []
  for (let s = 0; s < refs.length; s++) {
    if (comp[s] >= 0) continue
    const c = compArea.length
    let a = 0
    comp[s] = c
    stack.push(s)
    while (stack.length) {
      const k = stack.pop()
      a += area[k]
      const { tile, poly } = navMesh.getTileAndPolyByRefUnsafe(refs[k])
      for (let l = poly.firstLink(); l !== Detour.DT_NULL_LINK; l = tile.links(l).next()) {
        const j = idx(tile.links(l).ref())
        if (j >= 0 && comp[j] < 0) {
          comp[j] = c
          stack.push(j)
        }
      }
    }
    compArea.push(a)
  }
  return { refs, comp, compArea, area, idx }
}

async function main() {
  const o = args(process.argv.slice(2))
  const t0 = performance.now()
  const cfg = JSON.parse(fs.readFileSync(path.join(o.in, 'config.json'), 'utf8'))
  const info = await initRecast()
  const shared = o.threads > 1
  const verts = readTyped(path.join(o.in, 'verts.f32'), Float32Array, shared)
  const tris = readTyped(path.join(o.in, 'tris.u32'), Uint32Array, shared)
  const areas = readTyped(path.join(o.in, 'areas.u8'), Uint8Array, shared)
  if (tris.length % 3 !== 0 || areas.length !== tris.length / 3) throw new Error('tris.u32 / areas.u8 size mismatch')
  const tInit = performance.now()
  const grid = tileGrid(verts, tris, cfg.recast)
  const part = partition(verts, tris, grid)
  const nTiles = grid.tw * grid.th
  const job = {
    verts, tris, areas, offsets: shared ? toShared(part.offsets) : part.offsets, ids: shared ? toShared(part.ids) : part.ids,
    recast: cfg.recast, polyFlag: cfg.poly_flag, offmesh: cfg.offmesh || [],
  }
  const tPart = performance.now()
  const tileData = await bakeTiles(job, nTiles, o.threads)
  const tTiles = performance.now()

  // 装配：(ty, tx) 行主序；数组对象保留到导出结束（addTile 不转移所有权）
  const navMesh = new NavMesh()
  const params = NavMeshParams.create({ orig: grid.orig, tileWidth: grid.tcs, tileHeight: grid.tcs, maxTiles: grid.maxTiles, maxPolys: grid.maxPolysPerTile })
  if (!navMesh.initTiled(params)) throw new Error('dtNavMesh initTiled failed')
  const keep = []
  let built = 0
  for (let i = 0; i < nTiles; i++) {
    if (!tileData[i]) continue
    const arr = new UnsignedCharArray()
    arr.copy(tileData[i])
    const r = navMesh.addTile(arr, 0, 0)
    if (statusFailed(r.status)) throw new Error(`addTile ${i % grid.tw}, ${Math.floor(i / grid.tw)} failed: status ${r.status}`)
    keep.push(arr)
    built++
  }
  const tAsm = performance.now()

  // 岛统计与种子洪泛裁剪：含种子的分量保留，其余多边形 flags = 0（M22 §6.6.11）
  const polyBits = Math.log2(grid.maxPolysPerTile)
  const isl = islands(navMesh, polyBits)
  const all = new QueryFilter()
  all.includeFlags = 0xffff
  all.excludeFlags = 0
  const q = new NavMeshQuery(navMesh, { maxNodes: MAX_NODES, defaultQueryFilter: all })
  const ext = cfg.seed_ext_mm || [2000, 4000, 2000]
  const seeds = []
  const keptComp = new Set()
  for (const s of cfg.seeds || []) {
    const r = q.findNearestPoly(toRecast(s.p_mm), { filter: all, halfExtents: { x: ext[0] / 1000, y: ext[1] / 1000, z: ext[2] / 1000 } })
    const k = r.success && r.nearestRef ? isl.idx(r.nearestRef) : -1
    if (k < 0) {
      seeds.push({ id: s.id, found: false })
      continue
    }
    const p = r.nearestPoint
    const d = Math.round(Math.hypot(p.x * 1000 - s.p_mm[0], -p.z * 1000 - s.p_mm[1]))
    seeds.push({ id: s.id, found: true, dist_mm: d, island: isl.comp[k], kept: true })
    keptComp.add(isl.comp[k])
  }
  q.destroy()
  let fallback = null
  const groundComps = new Set()
  for (let k = 0; k < isl.refs.length; k++) if (isl.area[k] > 0) groundComps.add(isl.comp[k])
  if (cfg.keep === 'largest-seeded' && keptComp.size > 1) {
    // 只保留面积最大的含种子岛（配方 build.nav.<profile>.keep）；并列取分量号小者
    let best = -1
    for (const c of keptComp) if (best < 0 || isl.compArea[c] > isl.compArea[best] || (isl.compArea[c] === isl.compArea[best] && c < best)) best = c
    keptComp.clear()
    keptComp.add(best)
    for (const s of seeds) if (s.found && s.island !== best) s.kept = false
  }
  if (keptComp.size === 0 && groundComps.size) {
    let best = -1
    for (const c of groundComps) if (best < 0 || isl.compArea[c] > isl.compArea[best]) best = c
    keptComp.add(best)
    fallback = 'largest-island'
  }
  let polysKept = 0; let areaKept = 0; let areaTotal = 0; let largest = 0
  for (const c of groundComps) {
    areaTotal += isl.compArea[c]
    if (isl.compArea[c] > largest) largest = isl.compArea[c]
  }
  for (const c of keptComp) areaKept += isl.compArea[c]
  const keptAreas = [...keptComp].map((c) => isl.compArea[c]).sort((a, b) => b - a)
  for (let k = 0; k < isl.refs.length; k++) {
    if (keptComp.has(isl.comp[k])) polysKept++
    else navMesh.setPolyFlags(isl.refs[k], 0)
  }
  const tPrune = performance.now()
  const bin = exportNavMesh(navMesh)
  fs.writeFileSync(o.out, bin)
  const tExport = performance.now()
  const r2 = (v) => Math.round(v * 100) / 100
  const kept = [...keptComp].filter((c) => groundComps.has(c)).length
  const out = {
    ok: true, profile: cfg.profile, bin: { bytes: bin.length, sha256: sha256(bin) },
    grid: { tiles_x: grid.tw, tiles_y: grid.th, tile_m: grid.tcs, bmin: grid.bmin, bmax: grid.bmax, max_polys_per_tile: grid.maxPolysPerTile },
    tiles: { total: nTiles, nonempty_input: job.offsets.reduce((n, _v, i) => (i < nTiles && job.offsets[i + 1] > job.offsets[i] ? n + 1 : n), 0), built },
    polys: isl.refs.length, polys_kept: polysKept,
    islands: { total: groundComps.size, kept, dropped: groundComps.size - kept, walkable_area_before_m2: r2(areaTotal), walkable_area_m2: r2(areaKept), largest_m2: r2(largest), kept_areas_m2: keptAreas.slice(0, 32).map(r2) },
    seeds, seed_fallback: fallback, keep: cfg.keep || 'seeded',
    runtime: {
      ...info, threads: o.threads, cpus: os.cpus().length, rss_mb: Math.round(process.memoryUsage().rss / 1048576),
      ms: { init: Math.round(tInit - t0), partition: Math.round(tPart - tInit), tiles: Math.round(tTiles - tPart), assemble: Math.round(tAsm - tTiles), prune: Math.round(tPrune - tAsm), export: Math.round(tExport - tPrune) },
    },
  }
  process.stdout.write(`${JSON.stringify(out)}\n`)
}

main().catch((e) => {
  process.stdout.write(`${JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e) })}\n`)
  process.exit(2)
})
