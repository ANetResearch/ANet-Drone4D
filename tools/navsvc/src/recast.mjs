// recast-navigation WASM 的 Node 端装载与版本核对（ADR-196 决策 (1)）。
//
// 版本锁：@recast-navigation/core、generators、wasm 三个包的已安装版本必须等于 tools/worldglb/RECAST_VERSION（M22 与 M23 共用），
// WASM 文件（dist/recast-navigation.wasm.wasm，单线程、不含 SIMD 与 relaxed SIMD 指令）的 sha256 必须等于 tools/navsvc/WASM_SHA256。
// 任一不符即拒绝启动（烘焙与查询都不会以未登记的二进制运行）。

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { initRecastFromBinary } from './core.mjs'

export const NAVSVC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
export const ROOT = path.resolve(NAVSVC_DIR, '..', '..')
export const RECAST_VERSION_FILE = path.join(ROOT, 'tools', 'worldglb', 'RECAST_VERSION')
export const WASM_SHA256_FILE = path.join(NAVSVC_DIR, 'WASM_SHA256')
export const PKG_DIR = path.join(NAVSVC_DIR, 'node_modules', '@recast-navigation')
export const WASM_PATH = path.join(PKG_DIR, 'wasm', 'dist', 'recast-navigation.wasm.wasm')
export const NAVSVC_VERSION = JSON.parse(fs.readFileSync(path.join(NAVSVC_DIR, 'package.json'), 'utf8')).version

export function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex')
}

/** 版本与 WASM 核对；返回 `{recast_version, wasm_sha256, node, navsvc}`，不符时抛错。 */
export function checkVersions() {
  const want = fs.readFileSync(RECAST_VERSION_FILE, 'utf8').trim()
  for (const name of ['core', 'generators', 'wasm']) {
    const got = JSON.parse(fs.readFileSync(path.join(PKG_DIR, name, 'package.json'), 'utf8')).version
    if (got !== want) throw new Error(`@recast-navigation/${name} ${got} != tools/worldglb/RECAST_VERSION ${want}`)
  }
  const bin = fs.readFileSync(WASM_PATH)
  const got = sha256(bin)
  const pinned = fs.readFileSync(WASM_SHA256_FILE, 'utf8').trim()
  if (got !== pinned) throw new Error(`recast-navigation.wasm.wasm sha256 ${got} != tools/navsvc/WASM_SHA256 ${pinned}`)
  return { info: { recast_version: want, wasm_sha256: got, node: process.version, navsvc: NAVSVC_VERSION }, bin }
}

/** 核对后装载 WASM（同一进程只装载一次）。 */
export async function initRecast() {
  const { info, bin } = checkVersions()
  await initRecastFromBinary(bin)
  return info
}
