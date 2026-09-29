// Vertex pulling from the PointPool through the DrawTable (M05 §6.7.2; g01 pool.js; ADR-007, ADR-010). Owner: M05.
// TSL single source shared by the point material and the pick material: vertexIndex -> 12-step binary search over the
// DrawTable prefix column (<= 4096 entries) -> pool texel -> node cube from the NodeTable -> layer-frame position.
// Every integer parameter is a float uniform converted with int() in the shader (g01 §0 item 5: int uniforms are broken
// under the WebGL nodes handler); no texture sets internalFormat (g01 §0 item 10).
import { If, Loop, float, int, ivec2, uint, vec3, vertexIndex } from 'three/tsl'
import { PC } from '../params'

// TSL node graphs are typed loosely: @types/three does not model the swizzles and bit operations of TSL values
export type N = any

export interface FetchTextures {
  /** base texture nodes (texture(tex)); swapping .value rebinds without a recompile */
  pool: N
  draw: N
  node: N
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
  Loop(12, () => {
    If(hi.sub(lo).greaterThan(1), () => {
      const mid: N = lo.add(hi).shiftRight(1).toVar()
      const e: N = tex.draw.load(ivec2(mid.bitAnd(DW - 1), mid.shiftRight(10))).x
      If(int(e).lessThanEqual(v), () => {
        lo.assign(mid)
      }).Else(() => {
        hi.assign(mid)
      })
    })
  })
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
