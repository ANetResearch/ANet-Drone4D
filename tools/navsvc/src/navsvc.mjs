#!/usr/bin/env node
// navsvc：NavMesh 查询服务（ADR-196 决策 (3)；M23-FR-018 的服务端预检，M22 §6.6.14 的 nav_z 来源）。
//
// 用法：node tools/navsvc/src/navsvc.mjs [--world <世界目录>] [--profile <name>=<profile.json> ...]
//   --world 读取 world.json 中 type = navmesh 的全部图层（geometry/nav/<profile>.json 与同目录 bin）；
//   --profile 直接指定图层清单（测试夹具与未发布的 staging）。两者可同时给出，后者覆盖同名 profile。
// 协议：标准输入每行一个 JSON 请求，标准输出每行一个 JSON 应答（同序、同 id）；坐标一律为世界帧 [E, N, U] 整数毫米。
//   {"id":1,"op":"hello"}                                       → 版本、WASM sha256、已加载的 profile 与图层 sha256
//   {"id":2,"op":"snap","profile":"person","p":[E,N,U],"ext":[x,y,z]?}
//   {"id":3,"op":"path","profile":"person","a":[...],"b":[...]}
//   {"id":4,"op":"los","profile":"person","a":[...],"b":[...]}
//   {"id":5,"op":"random_point_around","profile":"person","p":[...],"r_mm":5000,"seed":42}
//   {"id":6,"op":"height","profile":"person","p":[...]}
//   {"id":7,"op":"random_point","profile":"person","seed":42}     → 可走面上按面积均匀取点（mulberry32，可达性统计抽样）
//   {"id":8,"op":"batch","items":[{op, ...}, ...]}              → {"results":[...]}（逐项与单条请求相同）
//   {"id":9,"op":"quit"}
// 错误应答：{"id", "ok": false, "error": {"code", "message"}}；code 取 BAD_REQUEST、UNKNOWN_OP、NAV_UNAVAILABLE（604，profile 未加载）。
// 确定性：同一 NavMesh 与同一请求序列的应答逐字节相同（整数毫米、固定键序、random_point_around 每次以请求种子重置）。
// 进程只在启动时读文件；无墙钟输出，`hello` 中的 node 版本只供出处记录（M23 §6.7.6 `provenance.node`）。

import fs from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'

import { NavError, NavQuery } from './core.mjs'
import { initRecast, sha256 } from './recast.mjs'

function parseArgs(argv) {
  const o = { world: null, profiles: {} }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    if (k === '--world') o.world = argv[++i]
    else if (k === '--profile') {
      const [name, file] = argv[++i].split('=')
      o.profiles[name] = file
    } else throw new Error(`unknown argument ${k}`)
  }
  return o
}

/** 图层清单路径：--world 下 world.json 的 navmesh 图层，再叠加 --profile。 */
function layerFiles(o) {
  const out = {}
  if (o.world) {
    const w = JSON.parse(fs.readFileSync(path.join(o.world, 'world.json'), 'utf8'))
    for (const L of w.layers || []) {
      if (L.type !== 'navmesh') continue
      const doc = JSON.parse(fs.readFileSync(path.join(o.world, L.href), 'utf8'))
      out[doc.profile] = path.join(o.world, L.href)
    }
  }
  return { ...out, ...o.profiles }
}

/** 读取图层清单与二进制：bin 字节数与 sha256 必须与清单一致。 */
export function loadProfile(file) {
  const doc = JSON.parse(fs.readFileSync(file, 'utf8'))
  const b = doc.bin || {}
  const bytes = fs.readFileSync(path.join(path.dirname(file), b.href))
  const got = sha256(bytes)
  if (bytes.length !== b.bytes || got !== b.sha256) throw new Error(`${file}: bin ${b.href} differs from the layer (bytes or sha256)`)
  return { doc, nav: new NavQuery(new Uint8Array(bytes), doc), sha256: got }
}

export function handle(state, req) {
  const op = req.op
  if (op === 'hello') {
    const profiles = {}
    for (const k of Object.keys(state.navs).sort()) profiles[k] = state.navs[k].sha256
    return { ...state.info, profiles }
  }
  if (op === 'batch') {
    if (!Array.isArray(req.items)) throw new NavError('BAD_REQUEST', 'items must be an array')
    return { results: req.items.map((it) => respond(state, it, false)) }
  }
  const p = state.navs[req.profile]
  const known = ['snap', 'path', 'los', 'random_point_around', 'random_point', 'height']
  if (!known.includes(op)) throw new NavError('UNKNOWN_OP', `unknown op ${JSON.stringify(op)}`)
  if (!p) throw new NavError('NAV_UNAVAILABLE', `profile ${JSON.stringify(req.profile)} not loaded`)
  const n = p.nav
  if (op === 'snap') return n.snap(req.p, req.ext)
  if (op === 'path') return n.path(req.a, req.b, req.ext)
  if (op === 'los') return n.los(req.a, req.b, req.ext)
  if (op === 'random_point_around') return n.randomPointAround(req.p, req.r_mm, req.seed, req.ext)
  if (op === 'random_point') return n.randomPoint(req.seed)
  return n.height(req.p, req.ext)
}

/** 单条请求 → 应答对象（withId 为假时用于 batch 子项，不带 id）。 */
export function respond(state, req, withId = true) {
  const head = withId ? { id: req && req.id !== undefined ? req.id : null } : {}
  try {
    if (!req || typeof req !== 'object') throw new NavError('BAD_REQUEST', 'request must be a JSON object')
    return { ...head, ok: true, ...handle(state, req) }
  } catch (e) {
    const code = e instanceof NavError ? e.code : 'BAD_REQUEST'
    return { ...head, ok: false, error: { code, message: String(e && e.message ? e.message : e) } }
  }
}

async function main() {
  const o = parseArgs(process.argv.slice(2))
  const info = await initRecast()
  const navs = {}
  for (const [name, file] of Object.entries(layerFiles(o))) navs[name] = loadProfile(file)
  const state = { info, navs }
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
  for await (const line of rl) {
    if (!line.trim()) continue
    let req
    try {
      req = JSON.parse(line)
    } catch {
      process.stdout.write(`${JSON.stringify({ id: null, ok: false, error: { code: 'BAD_REQUEST', message: 'invalid JSON' } })}\n`)
      continue
    }
    if (req && req.op === 'quit') {
      process.stdout.write(`${JSON.stringify({ id: req.id ?? null, ok: true })}\n`)
      break
    }
    process.stdout.write(`${JSON.stringify(respond(state, req))}\n`)
  }
  for (const k of Object.keys(navs)) navs[k].nav.destroy()
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname)
if (isMain) {
  main().catch((e) => {
    process.stderr.write(`navsvc: ${e && e.message ? e.message : e}\n`)
    process.exit(2)
  })
}
