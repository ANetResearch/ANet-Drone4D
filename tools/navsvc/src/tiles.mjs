// Recast 分瓦片烘焙（M22-FR-032、FR-033，§6.6.11；ADR-196 决策 (2)、(6)）。
//
// 与 @recast-navigation/generators 的 generateTiledNavMesh 同一流程（rcConfig 与瓦片网格由其 buildTiledNavMeshRcConfig 给出），
// 差别只有两处：(1) 不用 ChunkyTriMesh，而按三角形 xz 包围盒（外扩 borderSize 格）把三角形号分到瓦片，瓦片内按原三角形号升序
// 光栅化，从而能逐三角形写入 Python 给出的 Detour 区域号（低层 API 传入 area id）；(2) 多边形标志位按 profile 写入（行人 walk、
// 车辆 drive）。每个瓦片只依赖输入与瓦片号，与线程数和调度次序无关；装配按 (ty, tx) 行主序 addTile，故导出字节确定。

import {
  NavMeshCreateParams, Raw, Recast, RecastBuildContext, TriangleAreasArray, TrianglesArray, VerticesArray,
  allocCompactHeightfield, allocContourSet, allocHeightfield, allocPolyMesh, allocPolyMeshDetail,
  buildCompactHeightfield, buildContours, buildDistanceField, buildPolyMesh, buildPolyMeshDetail, buildRegions,
  cloneRcConfig, createHeightfield, createNavMeshData, erodeWalkableArea, filterLedgeSpans,
  filterLowHangingWalkableObstacles, filterWalkableLowHeightSpans, freeCompactHeightfield, freeContourSet,
  freeHeightfield, freePolyMesh, freePolyMeshDetail, markWalkableTriangles, rasterizeTriangles,
} from '@recast-navigation/core'
import { buildTiledNavMeshRcConfig } from '@recast-navigation/generators'

import { toRecast } from './core.mjs'

/** 输入网格（Recast 轴向）的包围盒：只统计被三角形引用的顶点。 */
export function bounds(verts, tris) {
  const lo = [Infinity, Infinity, Infinity]; const hi = [-Infinity, -Infinity, -Infinity]
  for (let i = 0; i < tris.length; i++) {
    const v = tris[i] * 3
    for (let c = 0; c < 3; c++) {
      const x = verts[v + c]
      if (x < lo[c]) lo[c] = x
      if (x > hi[c]) hi[c] = x
    }
  }
  return [lo, hi]
}

/** 瓦片网格：rcConfig、瓦片列数 tw、行数 th、瓦片边长 tcs（m）、外扩宽度 border（m）。 */
export function tileGrid(verts, tris, recastConfig) {
  const [bmin, bmax] = bounds(verts, tris)
  const r = buildTiledNavMeshRcConfig({ recastConfig, navMeshBounds: [bmin, bmax] })
  return { ...r, bmin, bmax, tw: r.tileWidth, th: r.tileHeight, border: r.config.borderSize * r.config.cs }
}

/**
 * 三角形 → 瓦片（CSR）：`offsets[t]..offsets[t + 1]` 为瓦片 t = ty * tw + tx 的三角形号（升序）。
 * 判据为三角形 xz 包围盒与外扩瓦片框相交（保守；框外部分由 Recast 光栅化裁掉）。
 */
export function partition(verts, tris, grid) {
  const { tw, th, tcs, border, bmin } = grid
  const nt = tris.length / 3
  const nTiles = tw * th
  const span = new Int32Array(nt * 4)
  const counts = new Int32Array(nTiles + 1)
  for (let t = 0; t < nt; t++) {
    let x0 = Infinity; let x1 = -Infinity; let z0 = Infinity; let z1 = -Infinity
    for (let k = 0; k < 3; k++) {
      const v = tris[3 * t + k] * 3
      const x = verts[v]; const z = verts[v + 2]
      if (x < x0) x0 = x
      if (x > x1) x1 = x
      if (z < z0) z0 = z
      if (z > z1) z1 = z
    }
    const a = Math.max(0, Math.floor((x0 - border - bmin[0]) / tcs))
    const b = Math.min(tw - 1, Math.floor((x1 + border - bmin[0]) / tcs))
    const c = Math.max(0, Math.floor((z0 - border - bmin[2]) / tcs))
    const d = Math.min(th - 1, Math.floor((z1 + border - bmin[2]) / tcs))
    span.set([a, b, c, d], 4 * t)
    for (let ty = c; ty <= d; ty++) for (let tx = a; tx <= b; tx++) counts[ty * tw + tx + 1]++
  }
  for (let i = 0; i < nTiles; i++) counts[i + 1] += counts[i]
  const offsets = counts
  const fill = offsets.slice(0, nTiles)
  const ids = new Int32Array(offsets[nTiles])
  for (let t = 0; t < nt; t++) {
    const a = span[4 * t]; const b = span[4 * t + 1]; const c = span[4 * t + 2]; const d = span[4 * t + 3]
    for (let ty = c; ty <= d; ty++) for (let tx = a; tx <= b; tx++) ids[fill[ty * tw + tx]++] = t
  }
  return { offsets, ids }
}

/** 人工连接（`offmesh.json`，世界帧毫米）→ Detour 参数（区域 OFFMESH = 6）。 */
export function offMeshParams(links, polyFlag) {
  return (links || []).map((l, i) => ({
    startPosition: toRecast(l.a_mm), endPosition: toRecast(l.b_mm), radius: l.radius_mm / 1000,
    bidirectional: !!l.bidir, area: 6, flags: polyFlag, userId: 1000 + i,
  }))
}

/**
 * 瓦片烘焙器：持有 WASM 内的整份顶点数组，按瓦片号逐个生成 Detour 瓦片数据（Uint8Array 副本；空瓦片返回 null）。
 * `job`：{verts, tris, areas, offsets, ids, recast, polyFlag, offmesh}（TypedArray 可为 SharedArrayBuffer 视图）。
 */
export class TileBaker {
  constructor(job) {
    this.job = job
    this.grid = tileGrid(job.verts, job.tris, job.recast)
    this.vertsArr = new VerticesArray()
    this.vertsArr.copy(job.verts)
    this.nv = job.verts.length / 3
    this.ctx = new RecastBuildContext()
    this.offmesh = offMeshParams(job.offmesh, job.polyFlag)
  }

  destroy() {
    this.vertsArr.destroy()
  }

  build(tileIndex) {
    const { tw, tcs, bmin, bmax, config: rc } = this.grid
    const tx = tileIndex % tw; const ty = Math.floor(tileIndex / tw)
    const { tris, areas, offsets, ids, polyFlag } = this.job
    const n = offsets[tileIndex + 1] - offsets[tileIndex]
    if (n === 0) return null
    const cfg = cloneRcConfig(rc)
    const b = cfg.borderSize * cfg.cs
    const lo = [bmin[0] + tx * tcs - b, bmin[1], bmin[2] + ty * tcs - b]
    const hi = [bmin[0] + (tx + 1) * tcs + b, bmax[1], bmin[2] + (ty + 1) * tcs + b]
    for (let c = 0; c < 3; c++) {
      cfg.set_bmin(c, lo[c])
      cfg.set_bmax(c, hi[c])
    }
    const ctx = this.ctx
    const hf = allocHeightfield()
    let chf = null; let cset = null; let pmesh = null; let dmesh = null
    const cleanup = () => {
      if (hf) freeHeightfield(hf)
      if (chf) freeCompactHeightfield(chf)
      if (cset) freeContourSet(cset)
      if (pmesh) freePolyMesh(pmesh)
      if (dmesh) freePolyMeshDetail(dmesh)
    }
    const fail = (msg) => {
      cleanup()
      throw new Error(`tile (${tx}, ${ty}): ${msg}`)
    }
    if (!createHeightfield(ctx, hf, cfg.width, cfg.height, lo, hi, cfg.cs, cfg.ch)) fail('createHeightfield')
    // 瓦片三角形（原三角形号升序）与逐三角形区域号：坡度合格（markWalkableTriangles）且 Python 区域号非 0 时取该区域号
    const triArr = new TrianglesArray()
    const sub = new Int32Array(3 * n)
    for (let i = 0; i < n; i++) {
      const t = ids[offsets[tileIndex] + i]
      sub[3 * i] = tris[3 * t]; sub[3 * i + 1] = tris[3 * t + 1]; sub[3 * i + 2] = tris[3 * t + 2]
    }
    triArr.copy(sub)
    const areaArr = new TriangleAreasArray()
    areaArr.resize(n)
    markWalkableTriangles(ctx, cfg.walkableSlopeAngle, this.vertsArr, this.nv, triArr, n, areaArr)
    const view = areaArr.getHeapView()
    for (let i = 0; i < n; i++) view[i] = view[i] ? areas[ids[offsets[tileIndex] + i]] : 0
    const ok = rasterizeTriangles(ctx, this.vertsArr, this.nv, triArr, areaArr, n, hf, cfg.walkableClimb)
    triArr.destroy()
    areaArr.destroy()
    if (!ok) fail('rasterizeTriangles')
    filterLowHangingWalkableObstacles(ctx, cfg.walkableClimb, hf)
    filterLedgeSpans(ctx, cfg.walkableHeight, cfg.walkableClimb, hf)
    filterWalkableLowHeightSpans(ctx, cfg.walkableHeight, hf)
    chf = allocCompactHeightfield()
    if (!buildCompactHeightfield(ctx, cfg.walkableHeight, cfg.walkableClimb, hf, chf)) fail('buildCompactHeightfield')
    if (!erodeWalkableArea(ctx, cfg.walkableRadius, chf)) fail('erodeWalkableArea')
    if (!buildDistanceField(ctx, chf)) fail('buildDistanceField')
    if (!buildRegions(ctx, chf, cfg.borderSize, cfg.minRegionArea, cfg.mergeRegionArea)) fail('buildRegions')
    cset = allocContourSet()
    if (!buildContours(ctx, chf, cfg.maxSimplificationError, cfg.maxEdgeLen, cset, Recast.RC_CONTOUR_TESS_WALL_EDGES)) fail('buildContours')
    pmesh = allocPolyMesh()
    if (!buildPolyMesh(ctx, cset, cfg.maxVertsPerPoly, pmesh)) fail('buildPolyMesh')
    dmesh = allocPolyMeshDetail()
    if (!buildPolyMeshDetail(ctx, pmesh, chf, cfg.detailSampleDist, cfg.detailSampleMaxError, dmesh)) fail('buildPolyMeshDetail')
    if (pmesh.npolys() === 0) {
      cleanup()
      return null
    }
    if (pmesh.npolys() > this.grid.maxPolysPerTile) fail(`${pmesh.npolys()} polygons > maxPolysPerTile ${this.grid.maxPolysPerTile}`)
    for (let i = 0; i < pmesh.npolys(); i++) pmesh.setFlags(i, pmesh.areas(i) ? polyFlag : 0)
    const p = new NavMeshCreateParams()
    p.setPolyMeshCreateParams(pmesh)
    p.setPolyMeshDetailCreateParams(dmesh)
    p.setWalkableHeight(cfg.walkableHeight * cfg.ch)
    p.setWalkableRadius(cfg.walkableRadius * cfg.cs)
    p.setWalkableClimb(cfg.walkableClimb * cfg.ch)
    p.setCellSize(cfg.cs)
    p.setCellHeight(cfg.ch)
    p.setBuildBvTree(true)
    if (this.offmesh.length) p.setOffMeshConnections(this.offmesh)
    p.setTileX(tx)
    p.setTileY(ty)
    const r = createNavMeshData(p)
    Raw.destroy(p.raw)
    if (!r.success) fail('createNavMeshData')
    const out = r.navMeshData.toTypedArray().slice()
    r.navMeshData.destroy()
    cleanup()
    return out
  }
}
