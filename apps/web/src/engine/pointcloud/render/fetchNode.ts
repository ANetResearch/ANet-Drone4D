// Vertex pulling from the PointPool through the DrawTable (M05 §6.7.2; g01 pool.js; ADR-007, ADR-010). Owner: M05.
// TSL single source shared by the point material and the pick material: vertexIndex -> DrawTable entry -> pool texel ->
// node cube from the NodeTable -> layer-frame position. With the block index (point material, FX2-R2) the entry search
// runs inside [block[v >> 6], block[(v >> 6) + 1]] and stops as soon as the range holds one entry (usually after 0-1
// steps); without it (the pick sub-table) it is the 12-step binary search over the whole prefix column (<= 4096 entries).
// On SwiftShader the 12 dependent texel loads per point were about 4.3 ms of a 25k-point pass.
// Every integer parameter is a float uniform converted with int() in the shader (g01 §0 item 5: int uniforms are broken
// under the WebGL nodes handler); no texture sets internalFormat (g01 §0 item 10).
import { Break, If, Loop, float, int, ivec2, uint, vec3, vertexIndex } from 'three/tsl'
import { PC } from '../params'

// TSL node graphs are typed loosely: @types/three does not model the swizzles and bit operations of TSL values
export type N = any

export interface FetchTextures {
  /** base texture nodes (texture(tex)); swapping .value rebinds without a recompile */
  pool: N
  draw: N
  node: N
  /** DrawTable block index (R32UI, DrawTables.block); absent: binary search over the whole table */
  block?: N
}

export interface FetchedPoint {
  /** layer-frame position */
  p: N
  /** node-cube normalised position (octant of the Lite size) */
  q: N
  /** pool texel words (uvec4) */
  w: N
  /** NodeTable texel 1: spacing_L, level */
  n1: N
  /** childDrawnMask (uint) */
  mask: N
  /** fade 0..1 */
  fade: N
  /** node-local point index (fade hash, picking) */
  local: N
  /** DrawTable entry index */
  entry: N
  /** global node index */
  node: N
  /** the vertex id (int) this point was pulled for */
  vid: N
}

/**
 * Emits the pulling code into the current Fn body (call it inside Fn(() => ...)). vid: vertex id (int node; for quads
 * vertexIndex / 6). numDraws: float uniform with the number of DrawTable entries.
 */
export function fetchPoint(tex: FetchTextures, numDraws: N, vid: N = int(vertexIndex)): FetchedPoint {
  const W = PC.poolWidth
  const DW = PC.drawTableWidth
  const NW = PC.nodeTableWidth
  const v: N = int(vid).toVar()
  const lo: N = int(0).toVar()
  const hi: N = int(numDraws).toVar()
  const step = (): void => {
    const mid: N = lo.add(hi).shiftRight(1).toVar()
    const e: N = tex.draw.load(ivec2(mid.bitAnd(DW - 1), mid.shiftRight(10))).x
    If(int(e).lessThanEqual(v), () => {
      lo.assign(mid)
    }).Else(() => {
      hi.assign(mid)
    })
  }
  if (tex.block) {
    // the entry of v lies in [block[b], block[b + 1]] (DrawTable.buildBlockIndex); at most DRAW_BLOCK + 1 entries
    const b: N = v.shiftRight(PC.drawIndexBlockLog2).toVar()
    const b1: N = b.add(1)
    lo.assign(int(tex.block.load(ivec2(b.bitAnd(DW - 1), b.shiftRight(10))).x))
    hi.assign(int(tex.block.load(ivec2(b1.bitAnd(DW - 1), b1.shiftRight(10))).x).add(1))
    Loop(PC.drawIndexBlockLog2 + 1, () => {
      If(hi.sub(lo).lessThanEqual(1), () => {
        Break()
      })
      step()
    })
  } else {
    Loop(12, () => {
      If(hi.sub(lo).greaterThan(1), step)
    })
  }
  const entry: N = lo
  const d: N = tex.draw.load(ivec2(entry.bitAnd(DW - 1), entry.shiftRight(10))).toVar()
  const local: N = v.sub(int(d.x)).toVar()
  const gi: N = int(d.y).add(local).toVar()
  const w: N = tex.pool.load(ivec2(gi.bitAnd(W - 1), gi.shiftRight(12))).toVar()
  const node: N = int(d.w.bitAnd(uint(0xffffff))).toVar()
  const t0: N = node.mul(2).toVar()
  const t1: N = t0.add(1).toVar()
  const n0: N = tex.node.load(ivec2(t0.bitAnd(NW - 1), t0.shiftRight(10))).toVar()
  const n1: N = tex.node.load(ivec2(t1.bitAnd(NW - 1), t1.shiftRight(10))).toVar()
  const q: N = vec3(float(w.x.bitAnd(uint(65535))), float(w.x.shiftRight(uint(16))), float(w.y.bitAnd(uint(65535)))).div(65535).toVar()
  const p: N = n0.xyz.add(q.mul(n0.w)).toVar()
  return { p, q, w, n1, mask: d.z.shiftRight(uint(24)), fade: float(d.w.shiftRight(uint(24))).div(255), local, entry, node, vid: v }
}
