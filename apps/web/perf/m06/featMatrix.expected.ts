// Expected values of the 28-item feature matrix + PointPool per backend (g01 §3 and results/F_feat_*.json, P_pool.jsonl;
// D1-AC-14, M06-AC-002, AC-011). Classic = WebGLRenderer + AnetNodesHandler (Tier B and S), wgpu = WebGPURenderer on
// WebGPU (Tier A, P1). Guards of g01 §9: points_glPointSize_fixed 6400, onObjectUpdate [64, 128, 191, 255],
// fog_sceneFogNode_customFn red, instancedMesh_sharedMaterial (0 on the classic path until upstream fixes it),
// pointPool 185071 (84277 with 1 px points on WebGPU).
export type Expected = Record<string, Record<string, unknown>>

export const CLASSIC: Expected = {
  points_1px: { litPx: 400 },
  points_glPointSize_nofix: { litPx: 400 },
  points_glPointSize_fixed: { litPx: 6400 },
  points_perObjectSize: { rows0_9_px: 960, rows10_19_px: 3040 },
  points_glsl_ShaderMaterial: { litPx: 6400 },
  sprite_quad_instanced: { litPx: 6400 },
  vertexIndex_flatQuads: { litPx: 6400 },
  fog_sceneFog_classic: { center: [255, 0, 0, 255] },
  fog_sceneFogNode_customFn: { center: [255, 0, 0, 255] },
  fog_sceneFogNode_stockHandler: { center: [255, 255, 255, 255] },
  fog_inMaterial_Fn: { center: [255, 0, 0, 255] },
  onObjectUpdate_sharedMaterial: { reds: [64, 128, 191, 255] },
  onRender_onFrame_update: { first: [51, 26, 0, 255], second: [76, 51, 0, 255], calls: { render: 3, frame: 2 } },
  instancedMesh_sharedMaterial: { leftPx: 400, rightPx: 0 },
  instancedMesh_ownMaterials: { leftPx: 400, rightPx: 400 },
  depthTex_EDL_cloud_fsq: { reversedActive: false, linDepthCenter: 102, linDepthCorner: 204, edlDarkPx: 340, edlCenter: [153, 153, 153, 255], edlCorner: [153, 153, 153, 255], cloudTintedPx: 1478, compCenter: [153, 153, 153, 255] },
  depthTex_EDL_cloud_fsq_reversedZ: { linDepthCenter: 102, linDepthCorner: 204, edlDarkPx: 340, edlCenter: [153, 153, 153, 255], edlCorner: [153, 153, 153, 255], cloudTintedPx: 1478, compCenter: [153, 153, 153, 255] },
  rt_vs_screen_encoding: { inRT: [128, 128, 128, 255], onScreen: [188, 188, 188, 255] },
  direct_contextNode_encoding: { skipped: 'WebGPURenderer only' },
  mask_alpha_and_fsq_depthWrite: { alphaCorner: 255, alphaCenter: 128, outCorner: [255, 0, 0, 255], outCenter: [153, 153, 153, 255] },
  line2_node_fatline: { litPx: 1004 },
  readback_row_order: { row100: 255, row20: 0 },
  lit_standard: { center: [25, 25, 110, 255], upperRight: [39, 39, 147, 255], lowerLeft: [16, 16, 72, 255] },
  texture3D: { center: [200, 100, 50, 255] },
  line_basic_node: { litPx: 108 },
  renderPipeline_pass: { unsupported: 'RenderPipeline requires common/Renderer (WebGPURenderer); handler has no PassNode/MRT support' },
  compute: { available: false },
  handler_sideEffects: { userOnBeforeRenderCalls: 0, infoRenderFrameDelta: 9, firstGeometryDisposeEvents: 1 },
  pointPool: { litPx: 185071 },
}

/** per-channel tolerance for colour triples (SwiftShader is deterministic; 1 LSB for rounding differences) */
export const COLOR_TOL = 1

/** compare one result with its expectation: numbers exactly (colours +- COLOR_TOL), strings and booleans exactly */
export function matches(got: unknown, want: unknown): boolean {
  if (typeof want === 'number') return typeof got === 'number' && Math.abs(got - want) <= (Number.isInteger(want) && want <= 255 ? COLOR_TOL : 0)
  if (Array.isArray(want)) return Array.isArray(got) && got.length === want.length && want.every((w, i) => matches(got[i], w))
  if (want && typeof want === 'object') return !!got && typeof got === 'object' && Object.entries(want).every(([k, w]) => matches((got as Record<string, unknown>)[k], w))
  return got === want
}

/** list of mismatching items: [id, got, want] */
export function diffMatrix(tests: Record<string, unknown>, expected: Expected = CLASSIC): [string, unknown, unknown][] {
  const out: [string, unknown, unknown][] = []
  for (const [id, want] of Object.entries(expected)) if (!matches(tests[id], want)) out.push([id, tests[id], want])
  return out
}
