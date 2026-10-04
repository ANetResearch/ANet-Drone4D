# viewport/ and the M06 engine modules: coding rules

Owner: M06 (AWR-03 §4.3). Design: `docs/modules/M06-Web视口与渲染后端PRD.md` §6. This file carries the coding rules of
M06 §6.3 (g01 §0 item 5, §2); they are enforced by dev/test assertions (`viewport/backend/guards.ts`, run at layer
registration) and by the M06 lint (`apps/web/tests/m06/lint/m06-lint.mjs`, `make lint-m06`, part of `make lint`).

| # | Rule | Enforcement |
|---|---|---|
| 1 | Every InstancedMesh owns its material instance | registration assertion, M06-E008 |
| 2 | Never rely on `material.onBeforeRender` (WebGLNodesHandler overwrites it); use `object.onBeforeRender` | lint M06-L-01 |
| 3 | `info.render.frame` is not a frame number; use `FrameCtx.frameNo` | lint M06-L-02 |
| 4 | After the first build the handler disposes the geometry once in a microtask: never depend on uploaded CPU arrays | review |
| 5 | int/uint uniforms are broken (UBO members are Float32): float uniforms, `int()` in the shader | registration assertion, M06-E009 |
| 6 | No per-object `onObjectUpdate` on hot paths; per-object data comes from instance attributes, instance textures or matrices | lint M06-L-07 (engine/**) |
| 7 | `onRenderUpdate` callbacks are idempotent (the first frame may call twice) | review |
| 8 | Never set `texture.internalFormat` (WebGPU reads integer textures as 0) | assertion (texture nodes of registered materials), lint M06-L-06 |
| 9 | Shared paths never use RenderPipeline, `pass()`, MRT, storage textures or compute | lint M06-L-05 |
| 10 | Attribute-less geometry never gets a fake `position` (three would clamp the draw range to it) | review |
| 11 | Instanced objects (InstancedMesh, LineSegments2, InstancedBufferGeometry) never share geometry | registration assertion, M06-E008 |
| 12 | No hand-written `UniformsGroup` / `ShaderMaterial.uniformsGroups` on the classic path: uniform blocks come only from node materials, bound per draw by `AnetNodesHandler` fix 5 (WebGLRenderer's own UBO path keeps a global binding point per group for life and would collide); a program stays within the device's block limits (WebGL2 minimum: 12 per stage, 24 combined, 24 binding points), otherwise it draws nothing and M06-E017 names it (ADR-086) | `perf/m06/gpu-limits.spec.ts` (emulated limits), GLSL budget check |

Further rules of this module:

- Only `RenderBackend.renderFrame` calls `renderer.render` at run time (AWR-03 §3.6 rule 2); `info.autoReset = false`;
  every frame `render.calls` must equal the pass plan (M06-E007). Objects with nothing to draw are `visible = false` so
  their layer's `drawCount()` stays exact.
- Synchronous read-backs (`readRenderTargetPixels`, `gl.readPixels`) are forbidden in `engine/**` and `viewport/**`
  (lint M06-L-03); `RenderBackend.readPixels` is asynchronous. Exceptions: the 1-pixel microbench sync before the reveal
  (`backend/microbench.ts`) and the test-build regression page (`dev/featMatrix.ts`).
- drei: only the ADR-008 white list, and D1 uses none of it (lint M06-L-04 on every `@react-three/drei` import).
- Hot paths (phase tasks) allocate nothing: preallocated typed arrays, pools, reused objects (AWR-03 §3.6 rule 1).
- Uniform nodes are never shared between materials (under WebGLNodesHandler the second material keeps its default).
- Uniform blocks (FX-UBO, ADR-086): the handler never hands node UniformsGroups to WebGLRenderer (one global binding
  point per group for life: synthcity peaks at 60 (Tier S) and 68 (Tier B), WebGL2 guarantees 24, SwiftShader 72).
  `backend/uboBinder.ts` binds block i of the current program to point i and uploads the changed values before each
  draw; `window.__perf.gpu.ubo` shows the device limits, the binding points in use and the largest block counts of one
  program. SwiftShader's limits are wide: anything that adds materials, uniform groups or buffer nodes runs
  `m06.gpu-limits` (real-GPU limits emulated).
- Attribute-less points (`GLPointsNodeMaterial` without a `position` attribute) resolve `positionGeometry` to a constant
  through `contextNode` (no AttributeNode warning, same GLSL); rule 10 still forbids a fake attribute.
- updateBefore nodes (VERIFY-UBO, ADR-087): the stock handler runs only `builder.updateNodes`; `AnetNodesHandler` fix 6
  also runs `updateBeforeNodes` (FRAME ones of every live program in `renderStart`, before WebGLRenderer uploads the
  frame's attributes; all of the drawn program's in its `onBeforeRender`). three's instancing needs it on real GPUs:
  instance matrices larger than `MAX_UNIFORM_BLOCK_SIZE` (16 384 B, the WebGL2 minimum and ANGLE Metal; SwiftShader
  65 536) leave the uniform buffer for interleaved instance attributes whose version an OnBeforeFrameUpdate node syncs,
  which the Tier B low-poly batches (300 x 64 B) hit. `updateAfterNodes` are not run: a material must not depend on them
  (`m06.gpu-limits` asserts none exist).
- Depth writes (`depthNode`) to the default framebuffer need `renderer.depth === true`, a WebGPURenderer field that
  WebGLRenderer lacks: `AnetNodesHandler.setRenderer` sets it (fix 3), otherwise NodeMaterial drops gl_FragDepth on
  screen while render targets keep it. Under the reversed depth buffer three flips every depth function (AlwaysDepth
  becomes NEVER): full-screen depth write-back quads use `NeverDepth` there to get GL_ALWAYS.
- A depth texture read by both colorNode and depthNode goes through two texture nodes (one node read by both flows
  renders black on the classic path).
- Colours come from `lib/tokens/scene.gen.ts` as uniforms (linear sRGB), durations and curves from
  `lib/tokens/motion.gen.ts`; no hex, no motion literals (D1-AC-20).
- New materials after the reveal must reuse an existing program (same node graph): the shader zoo warms every layer's
  variants under the mask (`warmupVariants`), D1-AC-25.
