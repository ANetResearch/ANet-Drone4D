// Test helpers for the M05 selector, streaming and controller tests: g02 fixtures (nodes.json + flight60.bin, M05-FR-056)
// as both the oracle tree (oracle/lod.mjs loadTree) and the product NodeStore, and World Package trees (worlds/<id>).
import { existsSync, readFileSync } from 'node:fs'
import { NodeStore, type HierRecord, type RootInput } from '@/engine/pointcloud/core/NodeStore'
import { parseHierarchy, parseHierarchyExt } from '@/engine/pointcloud/io/hierarchy'
import type { PotreeMeta, WorldJson } from '@/engine/pointcloud/types'
// @ts-expect-error untyped research oracle (read-only copy with the two M05 patches)
import * as oracle from './oracle/lod.mjs'

export const FIXTURES = new URL('./fixtures/', import.meta.url)
export const WORLDS = new URL('../../../../worlds/', import.meta.url)
export const haveWorlds = existsSync(new URL('shenzhen/world.json', WORLDS))

export interface G02Node { i: number; level: number; count: number; offset: number; parent: number; childMask: number; children: number[]; cmin: number[]; csize: number; tmin: number[]; tmax: number[] }
export interface G02Meta { city: string; points: number; G: number; cubeMin: number[]; cubeSize: number; spacingRoot: number; depth: number; nodes: G02Node[] }

export interface Fixture {
  city: string
  meta: G02Meta
  /** oracle tree (lod.mjs loadTree) */
  T: unknown
  /** product node table built from the same nodes */
  store: NodeStore
  flight: Float32Array
}

const cache = new Map<string, Fixture>()

export function storeFromG02(meta: G02Meta): NodeStore {
  const N = meta.nodes.length
  const names = new Array<string>(N)
  const records: HierRecord[] = meta.nodes.map((n) => {
    let child = 0
    if (n.parent >= 0) child = meta.nodes[n.parent].children.indexOf(n.i)
    return { type: n.childMask ? 0 : 1, childMask: n.childMask, numPoints: n.count, byteOffset: n.offset * 12, byteSize: n.count * 12, parent: n.parent, child, level: n.level, name: '' }
  })
  for (let k = 0; k < N; k++) {
    const r = records[k]
    names[k] = r.parent < 0 ? 'r' : names[r.parent] + r.child
    r.name = names[k]
  }
  const tight = new Float64Array(6 * N)
  meta.nodes.forEach((n, k) => {
    tight.set(n.tmin, 6 * k)
    tight.set(n.tmax, 6 * k + 3)
  })
  const input: RootInput = { cubeMin: meta.cubeMin, cubeSize: meta.cubeSize, spacing: meta.spacingRoot, records, ext: null, tight, octreeUrl: '' }
  return new NodeStore([input])
}

export function fixture(city: string): Fixture {
  const hit = cache.get(city)
  if (hit) return hit
  const meta = JSON.parse(readFileSync(new URL(`${city}/nodes.json`, FIXTURES), 'utf8')) as G02Meta
  const buf = readFileSync(new URL(`${city}/flight60.bin`, FIXTURES))
  const flight = new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength))
  const f: Fixture = { city, meta, T: oracle.loadTree(meta), store: storeFromG02(meta), flight }
  cache.set(city, f)
  return f
}

const ab = (b: Buffer): ArrayBuffer => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer

/** product NodeStore of a generated World Package (all roots) */
export function storeFromWorld(city: string): { store: NodeStore; world: WorldJson; metas: PotreeMeta[] } {
  const world = JSON.parse(readFileSync(new URL(`${city}/world.json`, WORLDS), 'utf8')) as WorldJson
  const layer = world.layers.find((l) => l.type === 'pointcloud' && l.default)!
  const metas: PotreeMeta[] = []
  const inputs: RootInput[] = layer.roots!.map((r) => {
    const md = JSON.parse(readFileSync(new URL(`${city}/${r.href}metadata.json`, WORLDS), 'utf8')) as PotreeMeta
    metas.push(md)
    const hb = ab(readFileSync(new URL(`${city}/${r.href}hierarchy.bin`, WORLDS)))
    const records = parseHierarchy(hb, md.hierarchy.firstChunkSize)
    const xp = new URL(`${city}/${r.href}hierarchy_ext.bin`, WORLDS)
    const ext = existsSync(xp) ? parseHierarchyExt(ab(readFileSync(xp)), records.length) : null
    return { cubeMin: md.boundingBox.min, cubeSize: md.boundingBox.max[0] - md.boundingBox.min[0], spacing: md.spacing, records, ext, octreeUrl: '' }
  })
  return { store: new NodeStore(inputs), world, metas }
}

/** row k of a flight60 buffer (f32 [3601 x 6]): eye xyz, target xyz */
export function flightRow(fl: Float32Array, k: number): { eye: [number, number, number]; tgt: [number, number, number] } {
  const o = 6 * Math.min(3600, Math.max(0, k))
  return { eye: [fl[o], fl[o + 1], fl[o + 2]], tgt: [fl[o + 3], fl[o + 4], fl[o + 5]] }
}

export { oracle }

/** deterministic LCG (ctrlsim.mjs rng) */
export function rng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0
    return s / 4294967296
  }
}
