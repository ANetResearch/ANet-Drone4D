// In-memory World Package for browser tests: one root (cube (-50, -50, -50) + 100 m), three nodes (r, r0, r7), points on
// the plane z = 0 (root over the whole square, r0 in its octant) and r7 raised to z = 20, ANET_Q16 12 B/point, served by a fetch
// function with Range support (206 + Content-Range).
export interface SynthOptions { rootPts?: number; childPts?: number; cls?: number }

function grid(n: number, x0: number, y0: number, w: number, seed: number): [number, number][] {
  const out: [number, number][] = []
  let s = seed >>> 0
  const r = () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296)
  for (let i = 0; i < n; i++) out.push([x0 + r() * w, y0 + r() * w])
  return out
}

function nodePayload(pts: [number, number][], cubeMin: number[], size: number, cls: number, z = 0): Uint8Array {
  const n = pts.length
  const b = new Uint8Array(12 * n)
  const dv = new DataView(b.buffer)
  pts.forEach(([x, y], k) => {
    const q = (v: number, a: number) => Math.max(0, Math.min(65535, Math.round(((v - cubeMin[a]) / size) * 65535)))
    dv.setUint16(8 * k, q(x, 0), true)
    dv.setUint16(8 * k + 2, q(y, 1), true)
    dv.setUint16(8 * k + 4, q(z, 2), true)
    dv.setUint16(8 * k + 6, 0x8080, true) // normal +Z
    b[8 * n + 4 * k] = 200
    b[8 * n + 4 * k + 1] = 200
    b[8 * n + 4 * k + 2] = 200
    b[8 * n + 4 * k + 3] = cls
  })
  return b
}

export function synthWorld(o: SynthOptions = {}): { fetch: typeof fetch; points: number; log: string[] } {
  const rootPts = o.rootPts ?? 3000
  const childPts = o.childPts ?? 2000
  const cls = o.cls ?? 5
  const min = [-50, -50, -50]
  const size = 100
  const root = nodePayload(grid(rootPts, -50, -50, 100, 1), min, size, cls)
  // child 0 = (x0, y0, z0) octant: x, y in [-50, 0), z in [-50, 0): the plane z = 0 sits on its upper face
  const c0 = nodePayload(grid(childPts, -50, -50, 50, 2), [-50, -50, -50], 50, cls)
  // child 7 = (x1, y1, z1): x, y in [0, 50), raised to z = 20 (a depth step for EDL)
  const c7 = nodePayload(grid(childPts, 0, 0, 50, 3), [0, 0, 0], 50, cls, 20)
  const octree = new Uint8Array(root.length + c0.length + c7.length)
  octree.set(root, 0)
  octree.set(c0, root.length)
  octree.set(c7, root.length + c0.length)
  const hier = new ArrayBuffer(3 * 22)
  const dv = new DataView(hier)
  const rec = (i: number, type: number, mask: number, n: number, off: number, sz: number) => {
    dv.setUint8(22 * i, type)
    dv.setUint8(22 * i + 1, mask)
    dv.setUint32(22 * i + 2, n, true)
    dv.setBigInt64(22 * i + 6, BigInt(off), true)
    dv.setBigInt64(22 * i + 14, BigInt(sz), true)
  }
  rec(0, 0, 0b10000001, rootPts, 0, root.length)
  rec(1, 1, 0, childPts, root.length, c0.length)
  rec(2, 1, 0, childPts, root.length + c0.length, c7.length)
  const total = rootPts + 2 * childPts
  const md = {
    version: '2.0', name: 'synth', description: '', points: total, projection: '', hierarchy: { firstChunkSize: 66, stepSize: 1, depth: 1 },
    offset: min, scale: [0.001, 0.001, 0.001], spacing: 4, boundingBox: { min, max: [50, 50, 50] }, encoding: 'ANET_Q16',
    attributes: [{ name: 'anet:pos', size: 8, numElements: 4, elementSize: 2, type: 'uint16' }, { name: 'anet:col', size: 4, numElements: 4, elementSize: 1, type: 'uint8' }],
    anet: {
      formatVersion: 1, frame: 'world', streams: ['pos', 'col'], bytesPerPoint: 12, compression: 'none', pointOrder: 'shuffled', nodeCount: 3,
      levelsByteEnd: [root.length, octree.length], levelsPoints: [rootPts, total], levelsNodes: [1, 2], firstScreenLevel: 0,
      tightBounds: { min: [-50, -50, 0], max: [50, 50, 0] }, hierarchyExt: null, stats: { zP1: -10, zP99: 10, hagP1: 0, hagP99: 10, nnMedianM: 1 },
    },
  }
  const world = {
    schemaVersion: '1.0.0', id: 'synth', contentVersion: 'c1', layers: [{ id: 'pointcloud.visual', type: 'pointcloud', role: 'visual', format: 'potree2/anet-q16@1',
      href: 'visual/pointcloud/', status: 'ready', default: true, roots: [{ name: 'r', href: 'visual/pointcloud/', cubeMin: min, cubeSize: size, points: total, depth: 1,
        firstScreenBytes: root.length }] }],
    render: { defaultColorMode: 'height', zRangeM: [-10, 10], hagRangeM: [0, 10] },
  }
  const coord = { anchor: { kind: 'synthetic', georeferenced: false }, ground: { zM: 0 } }
  const files: Record<string, BodyInit | Uint8Array> = {
    'world.json': JSON.stringify(world), 'coordinate.json': JSON.stringify(coord), 'visual/pointcloud/metadata.json': JSON.stringify(md),
    'visual/pointcloud/hierarchy.bin': new Uint8Array(hier), 'visual/pointcloud/octree.bin': octree,
  }
  const log: string[] = []
  const f = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const u = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
    const rel = u.pathname.replace(/^\/worlds\/synth\//, '')
    const range = new Headers(init?.headers).get('Range')
    log.push(`${rel}${range ? ` ${range}` : ''}`)
    const body = files[rel]
    if (body === undefined) return new Response('missing', { status: 404 })
    if (range) {
      const m = /^bytes=(\d+)-(\d+)$/.exec(range)!
      const a = Number(m[1])
      const b = Number(m[2])
      const bytes = (body as Uint8Array).slice(a, b + 1)
      return new Response(bytes, { status: 206, headers: { 'Content-Range': `bytes ${a}-${b}/${(body as Uint8Array).length}` } })
    }
    return new Response(body as BodyInit, { status: 200 })
  }) as typeof fetch
  return { fetch: f, points: total, log }
}
