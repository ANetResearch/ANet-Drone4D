// 测试辅助（K06；D3-AC-03 锚点条款的分母，AWR-05 §28.5 选项 (A)）：判定点状锚点是否位于被洪泛裁剪掉的岛上。
//
// 用法：node tests/d3/world/navmesh_pruned.mjs <profile.json> <points.json>
//   points.json：[[E, N, U], ...]（世界帧整数毫米，U 为竖直初值）；标准输出一行 JSON：
//   {"kept_mm": [..], "pruned_mm": [..]}，逐点为到最近保留多边形与最近被裁剪多边形的水平距离（整数毫米，搜索半宽外为 null）。
//
// 被裁剪的多边形 flags = 0，任何 Detour 过滤器都取不到（core.mjs FLAGS）；本辅助只在自己的进程内把它们的标志位改为未使用的位
// PRUNED 后以只含该位的过滤器查询，不写回 NavMesh 二进制，不影响 navsvc 的应答。搜索半宽取 M22 §6.6.11 的锚点吸附 (15, 10, 15) m。

import fs from 'node:fs'
import path from 'node:path'

import { NavQuery, makeFilter, toRecast } from '../../../tools/navsvc/src/core.mjs'
import { initRecast, sha256 } from '../../../tools/navsvc/src/recast.mjs'

const PRUNED = 16
const EXT = { x: 15, y: 10, z: 15 }

const [layerFile, pointsFile] = process.argv.slice(2)
await initRecast()
const doc = JSON.parse(fs.readFileSync(layerFile, 'utf8'))
const bytes = fs.readFileSync(path.join(path.dirname(layerFile), doc.bin.href))
if (sha256(bytes) !== doc.bin.sha256) throw new Error(`${layerFile}: bin sha256 differs from the layer`)
const q = new NavQuery(new Uint8Array(bytes), doc)
let pruned = 0
for (let ti = 0; ti < q.navMesh.getMaxTiles(); ti++) {
  const tile = q.navMesh.getTile(ti)
  const h = tile.header()
  if (!h) continue
  const base = q.navMesh.getPolyRefBase(tile)
  for (let i = 0; i < h.polyCount(); i++) {
    const poly = tile.polys(i)
    if (poly.getType() === 0 && poly.flags() === 0) {
      q.navMesh.setPolyFlags(base + i, PRUNED)
      pruned++
    }
  }
}
const fp = makeFilter(doc.profile, doc.areas)
fp.includeFlags = PRUNED
fp.excludeFlags = 0

function dist(p, filter) {
  const r = q.query.findNearestPoly(toRecast(p), { filter, halfExtents: EXT })
  if (!r.success || !r.nearestRef) return null
  const v = r.nearestPoint
  return Math.round(Math.hypot(v.x * 1000 - p[0], -v.z * 1000 - p[1]))
}

const pts = JSON.parse(fs.readFileSync(pointsFile, 'utf8'))
const out = { pruned_polys: pruned, kept_mm: pts.map((p) => dist(p, q.filter)), pruned_mm: pts.map((p) => dist(p, fp)) }
process.stdout.write(`${JSON.stringify(out)}\n`)
q.destroy()
