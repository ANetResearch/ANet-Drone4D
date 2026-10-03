# r11 研究笔记：three.js（WebGPURenderer / TSL / compute / Points / 体积云）

> 研究单元：r11 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §9–16（Web 3D、WebGPU 定位、点云渲染、场景结构）、§17–25（环境可视化）、§34（前端栈）、§37（刷新频率）、§38–40（UI/交互）
> 仓库快照：`refs/web3d/three.js` @ `110fbbe`（2026-09-28，116010 stars，MIT）。`package.json` 的 version 是 `0.186.0`，`src/constants.js` 里 `REVISION = '187dev'`，也就是 r186 发布后的 dev 分支。npm 上最新是 `three@0.186.1`（2026-09-24），`@types/three@0.186.0`。
> 本文路径都相对仓库根目录。结论来自源码精读，并在本机 headless Chromium 151 里做了 6 组实测（脚本在 `/data/projs/anet-drone/.cache/research/r11/`）。实测跑在 **SwiftShader 软件渲染**上，绝对数值不代表真实 GPU，只能用来比较相对量级和验证功能。凡是估算都会标注"估算"。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **mrdoob/three.js r186**（`three/webgpu` + `three/tsl`） | Web 3D 引擎本体。`WebGPURenderer` 同时有 WebGPU 和 WebGL2 两个后端；TSL 节点着色语言会同时编译成 WGSL 和 GLSL；自带 compute、后处理 `RenderPipeline`、fog 节点、体积 raymarch、GPU 排序，以及官方 3DGS（`GaussianSplat`） | **adopt**：直接依赖 npm `three@~0.186.1`，作为 World/Environment/Drone/Mission 各 Layer 的渲染内核。**port**：点云节点渲染、EDL、粒子、体积云、自适应质量控制器按本文 §3 的配方移植。**reference**：compute rasterizer、indirect draw、CountingSort 留给 V1.0 的 GPU-driven 点云 | V0.1 起全程 | 5/5（唯一核心渲染引擎，无替代） |

**实现者先读这 10 条（每条都有源码或实测依据）：**

1. **自动回退已内建，不用自己写。** `new WebGPURenderer()` 默认用 `WebGPUBackend`，同时注入 `getFallback`（`src/renderers/webgpu/WebGPURenderer.js`）。`Renderer.init()` 里 `backend.init()` 抛异常就切到 `WebGLBackend`（`src/renderers/common/Renderer.js` L784–849）。本机默认 headless 模式下 `navigator.gpu` 存在，但 `requestAdapter()` 返回 `null`，于是自动走 WebGL2（ANGLE + SwiftShader）。要强制 WebGL2 就传 `forceWebGL: true`。**必须 `await renderer.init()` 之后才能调用 `hasFeature`、`compute`、`render`。**
2. **headless 下 WebGPU 其实能用，可以进 CI**：加上 `--enable-unsafe-webgpu --enable-unsafe-swiftshader --use-angle=swiftshader --enable-features=Vulkan --use-webgpu-adapter=swiftshader`，Chromium 151 会给出一个 SwiftShader WebGPU fallback adapter（`isFallbackAdapter=true`，带 core-features、timestamp-query、subgroups）。两个后端都能在本机跑功能测试（§3.11）。
3. **点云最大的坑：`WebGPURenderer` 下 `THREE.Points` 永远只有 1 像素。** WGSL 没有 point size，WebGL2 后端的 GLSL 模板也写死了 `gl_PointSize = 1.0;`（`src/renderers/webgl-fallback/nodes/GLSLNodeBuilder.js` L1702）。`PointsNodeMaterial.sizeNode` 只在配合 `Sprite` 或实例化四边形时生效（源码注释原话："WebGPU only supports point primitives with a pixel size of 1"）。
4. **"实例化四边形"方式的大点代价很高**。实测 20 万点下，quad 比 1px `Points` 慢约 **20 倍**（SwiftShader；真实 GPU 估算 2–4 倍）。所以点云要做**三种模式**：`pixel`（1px，最快）、`glpoint`（WebGL2 后端做一个 10 行的 monkey-patch 恢复 `gl_PointSize`，**已实测可用**）、`quad`（WebGPU 高端档）。详见 §3.2。
5. **每个 draw 对象的 CPU 开销：WebGPURenderer 约为 WebGLRenderer 的 1.6–2.1 倍**。实测每对象约 22 µs（WebGPU 后端）、29 µs（WebGL2 后端）、14 µs（经典 WebGLRenderer）。所以八叉树的**可见节点数必须封顶**（建议 ≤256），单节点点数要大一些（2–6 万）。WebGPU 后端用 `BundleGroup` 可以把 1024 个对象的提交耗时从 15.5 ms 降到 6.5 ms，但可见集变化后必须 `needsUpdate`，否则会引用已销毁的 buffer（实测报错，见 §6）。
6. **点云密度调节优先用 `geometry.setDrawRange(0, k)`**：节点内的点预先随机打散，前缀就是均匀子采样，调节时零上传、零重编译。它和 point budget、FPS 反馈控制器组成三级调节（§3.3–3.4）。
7. **WebGPU 后端的 attribute 会被隐式改写。** 非 normalized 的 `Uint8/Uint16` 会被扩成 `Uint32`（内存 ×2 到 ×4）；vec3 的 storage buffer 会 pad 成 vec4（`WebGPUAttributeUtils.createAttribute`）。点云二进制布局应该用 **`uint16x4 normalized` 存位置 + `unorm8x4` 存颜色，每点 12 B**（§3.2.1）。
8. **WebGL2 回退下的 compute 用 Transform Feedback 模拟**（`WebGLBackend.compute`）。`instanceIndex` 编译成 `gl_InstanceID`，只有当第一个 storage attribute 是 `instancedArray`（`StorageInstancedBufferAttribute`）时才会走 `drawArraysInstanced`。**用 `attributeArray` 写 compute，在 WebGL2 下所有线程的 `instanceIndex` 都是 0**（实测读回全部相同）。不支持 atomics、workgroup 和散射写。
9. **dispose 语义**：`geometry.dispose()` 会释放它的所有 GPU buffer（多个几何体共享的 attribute 会被一并销毁）。`StorageBufferAttribute.dispose()` 释放 compute buffer。`material.dispose()` 会让所有使用它的 RenderObject 失效，所以共享材质不能在卸载节点时 dispose。WebGPURenderer **不会调用** `onUploadCallback`，CPU 端的数组一直保留，要自己做 LRU 控制 JS 堆。
10. **性能钩子**：自定义 `InspectorBase` 子类（`begin/finish/beginRender/beginCompute`）可以拿到帧边界。`trackTimestamp: true` 加 `renderer.resolveTimestampsAsync(THREE.TimestampQuery.RENDER)` 会把真实 GPU 时间写入 `info.render.timestamp`（毫秒），两个后端都已实测。`info` 会被内部 rAF 循环在每帧自动 reset。

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 版本 | npm `0.186.1`（2026-09-24）；源码 dev `187dev`。发布节奏约每 1–3 个月一个 minor：r183 2026-02、r184 04、r185 06、r186 09 |
| 活跃度 | 2026-09-28 当天仍有提交（`BatchedMesh: Clarify geometry compatibility requirements`），116k stars，Web 3D 领域 star 最多且维护最活跃 |
| 规模 | `src/` 754 个 JS 文件、约 18.4 万行；`src/nodes/` 是 TSL 与节点系统（235 个模块）；`examples/jsm/` 是 addons（loaders、controls、TSL display 节点、gpgpu、inspector） |
| 构建 | 使用者直接 `npm i three`，用包导出 `three`、`three/webgpu`、`three/tsl`、`three/addons/*`。`build/three.webgpu.js` 和 `build/three.module.js` 都从 `three.core.js` 引入，**可以共存、共享核心类**。仓库自身用 `rollup -c utils/build/rollup.config.js` 构建，e2e 用 puppeteer（`test/e2e/puppeteer.js --webgpu`，flags 含 `--enable-unsafe-webgpu --enable-features=Vulkan --disable-vulkan-surface`） |
| 近期 API 变更 | `PostProcessing` 在 r183 改名为 `RenderPipeline`（旧名仍能用，但会告警）；`hasFeatureAsync`、`renderAsync`、`PassNode.setResolution` 在 r181 废弃；新增 `renderer.highPrecision`、`CanvasTarget`、`WebGLRenderer.setNodesHandler`（让经典 WebGLRenderer 也能跑 TSL 材质）、官方 `GaussianSplat` 与 SPLAT/SPZ/KSPLAT/PLY splat loader |
| 许可 | MIT（科研用途，按要求忽略） |

**本机能力探针实测**（`probe.mjs`，Chromium 151 headless）：

| 项 | 默认 headless | 加 WebGPU/SwiftShader flags |
|---|---|---|
| `navigator.gpu` | 存在 | 存在 |
| WebGPU adapter | `null`，three 自动回退 WebGL2 | SwiftShader，`isFallbackAdapter=true`，`core-features-and-limits`，`timestamp-query`，`subgroups`，`maxStorageBuffersInVertexStage=10`，`maxBufferSize=1 GiB` |
| WebGL2 renderer | ANGLE（Vulkan 1.3 SwiftShader） | 同左 |
| `ALIASED_POINT_SIZE_RANGE` | [1, 1023] | [1, 1023] |
| `MAX_TEXTURE_SIZE` | 8192 | 8192 |
| 扩展 | `EXT_clip_control`、`EXT_color_buffer_float`、`WEBGL_multi_draw`、`OES_texture_float_linear`；**没有** timer query | 多了 `EXT_disjoint_timer_query_webgl2` |

---

## 2. 源码结构与关键模块

### 2.1 渲染器与后端

| 文件 / 类 | 关键点（本项目相关） |
|---|---|
| `src/renderers/webgpu/WebGPURenderer.js` · `WebGPURenderer` | 构造参数：`forceWebGL`、`antialias`、`samples`、`alpha`、`logarithmicDepthBuffer`、`reversedDepthBuffer`、`outputBufferType`（默认 `HalfFloatType`）、`multiview`，以及透传给后端的 `trackTimestamp`、`powerPreference`、`requiredLimits`、`device`。非 `forceWebGL` 时注入 `getFallback()` 并 warn 一条 "WebGPU is not available, running under WebGL2 backend." |
| `src/renderers/common/Renderer.js` · `Renderer` | `init()`（L784）：`backend.init` 失败就调 `_getFallback`，然后构建 `NodeManager/Attributes/Geometries/Textures/Pipelines/Bindings/RenderObjects/RenderBundles`，并启动内部 `Animation`。`compute(nodes, dispatchSize)`（L2928）：dispatchSize 可以是数字、`[x,y,z]` 或 `IndirectStorageBufferAttribute`。`getArrayBufferAsync(attr)`（L2150）用于 GPU 到 CPU 读回。`resolveTimestampsAsync(type)`（L3072）。`needsFrameBufferTarget`（L2662）：只要 toneMapping 不是 None，或输出色彩空间不等于工作色彩空间，就会多走一个全屏 output pass。`highPrecision` setter（L1317）：全局改用 CPU fp64 的 modelView（与 InstancedMesh/SkinnedMesh 不兼容）。`_renderBundle`（L1464）与 BundleGroup 录制（L3392）。`onDeviceLost`、`onError` 回调 |
| `src/renderers/webgpu/WebGPUBackend.js` | `init()`（L201）：`requestAdapter({ powerPreference, featureLevel: 'compatibility' })`，**请求 adapter 支持的全部 feature**；`compatibilityMode = !device.features.has('core-features-and-limits')`，compat 模式下强制 `samples=0`；`device.lost` 回调 `renderer.onDeviceLost`。draw 路径（约 L2124）：`BatchedMesh` 是**逐项 `drawIndexed`**，没有 multiDrawIndirect；`geometry.setIndirect()` 走 `drawIndirect`/`drawIndexedIndirect`；`beginBundle/finishBundle/addBundle`（L2690 起，**只有 WebGPU 有**） |
| `src/renderers/webgpu/utils/WebGPUAttributeUtils.js` | `createAttribute`：非 normalized 的 Int8/Int16/Uint8/Uint16 会被转成 32 位；storage 的 vec3 会 pad 成 vec4 并**改写 `attribute.itemSize` 与 `array`**；stride 不是 4 字节倍数时也会 pad。`updateAttribute`：有 `updateRanges` 就按区间 `writeBuffer`，否则整段上传 |
| `src/renderers/webgl-fallback/WebGLBackend.js` | `init()`：`getContext('webgl2')`，加载 `EXT_color_buffer_float`、`WEBGL_multi_draw`、`EXT_clip_control`、`EXT_disjoint_timer_query_webgl2`、`KHR_parallel_shader_compile` 等扩展。`compute()`（L928）：`RASTERIZER_DISCARD` + Transform Feedback + `drawArrays(Instanced)(POINTS)`，完成后 `switchBuffers` 做双缓冲，storage 用 PBO 纹理映射做随机读 |
| `src/renderers/webgl-fallback/nodes/GLSLNodeBuilder.js` | `supports.storageBuffer=false`；`setupPBO`（L426）把 storage 数组映射成 2 的幂宽度纹理；`getInstanceIndex()` 是 `uint(gl_InstanceID)`（L1314）；顶点模板结尾硬编码 `gl_PointSize = 1.0;`（L1702） |
| `src/renderers/common/{Attributes,Geometries,RenderObject,Bindings}.js` | 生命周期：`geometry` 的 dispose 事件 → `Geometries.onDispose` 删除 index 与全部 attributes 的 GPU buffer；`RenderObject.onGeometryDispose` 删除"节点 attribute"（材质里用到、但不在 geometry 上的 storage buffer）；`material` 或 `object` 的 dispose 事件 → `RenderObject.dispose()` 删除 bindings。Attribute 版本比较：`data.version < attr.version \|\| usage === DynamicDrawUsage` 时重传（**DynamicDrawUsage 会每帧整段重传**） |
| `src/renderers/common/Info.js` | `info.render.{drawCalls, frameCalls, points, triangles, timestamp}`、`info.compute.{frameCalls, timestamp}`、`info.memory.{attributesSize, storageAttributesSize, texturesSize, total, ...}` |
| `src/renderers/common/Animation.js` | 内部 rAF 循环：每帧 `inspector.finish()` → `info.reset()`（`autoReset`）→ `nodeFrame.update()` → `inspector.begin()` → 调用你的 `animationLoop` |
| `src/renderers/common/InspectorBase.js` | 帧钩子基类：`begin/finish/beginRender/finishRender/beginCompute/finishCompute/computeAsync/inspect`。`renderer.inspector = x` 可以替换。官方 UI 是 `examples/jsm/inspector/Inspector.js`（Performance、Memory、Timeline tabs） |
| `src/renderers/common/RenderPipeline.js` | 后处理总线：`outputNode` → 全屏 `QuadMesh`，自动做 `renderOutput`（色调映射和色彩空间） |
| `src/renderers/common/BundleGroup.js` | `static=true`，`needsUpdate` 就 `version++`；子树在录制时做一次视锥剔除，之后回放 |
| `src/renderers/common/CanvasTarget.js` | 一个 device 画多个 canvas（`renderer.setCanvasTarget`），示例只支持 WebGPU |

### 2.2 TSL 与节点系统（`src/nodes/`）

| 模块 | 用途 |
|---|---|
| `nodes/TSL.js` | 统一导出（147 个 export 语句）；上层写 `import { Fn, If, Loop, uniform, attribute, ... } from 'three/tsl'` |
| `accessors/Arrays.js` | `attributeArray(count, type)`（`StorageBufferAttribute`，逐顶点）与 `instancedArray(count, type)`（`StorageInstancedBufferAttribute`，逐实例） |
| `accessors/StorageBufferNode.js` | `storage(attr, type, count)`、`.element(i)`、`.toAttribute()`（把 storage 当顶点属性读，compat 模式也安全）、`.toReadOnly()`、`.toAtomic()`、`.setPBO()` |
| `gpgpu/ComputeNode.js` | `Fn(...)().compute(count, workgroupSize=[64])`、`computeKernel`、`onInit`、`setName`；`gpgpu/AtomicFunctionNode.js`（`atomicAdd/Max/Min/Store/Load`）；`WorkgroupInfoNode.workgroupArray`；`BarrierNode.workgroupBarrier` |
| `accessors/StorageTextureNode.js` / `StorageTexture3DNode.js` | `textureStore(tex, coord, value)`：compute 写 2D/3D 纹理（体积云噪声） |
| `accessors/ModelNode.js` | `modelViewMatrix` 默认是 `mediumpModelViewMatrix = cameraViewMatrix * modelWorldMatrix`（GPU fp32）；`highpModelViewMatrix` 在 CPU 端用 fp64 算好再上传 |
| `fog/Fog.js` | `rangeFogFactor(near, far)`、`densityFogFactor(d)`（FogExp2 形式：1-exp(-(d·z)²)）、`exponentialHeightFogFactor(d, h)`、`fog(color, factor)`；用 `scene.fogNode = fog(...)` 挂上 |
| `display/PassNode.js` | `pass(scene, camera)`、`getTextureNode('output'\|'depth')`、`getViewZNode()`、`getLinearDepthNode()`、`setMRT(mrt({...}))`、`setResolutionScale(s)`（每帧 `setSize` 时生效，可动态调） |
| `core/Node.js` | uniform 更新钩子 `onFrameUpdate / onRenderUpdate / onObjectUpdate`；`onObjectUpdate` 可以按对象注入每个节点自己的 uniform（spacing、level），不必新建材质 |
| `accessors/BuiltinNode.js` | `builtin('gl_PointSize')`：生成原样的内建名，配合 §3.2.3 的补丁使用 |

### 2.3 材质与对象

| 文件 | 要点 |
|---|---|
| `materials/nodes/PointsNodeMaterial.js` | `object.isPoints` 时走 `NodeMaterial.setupVertex`（1px）；否则走 `setupVertexSprite`：`pointSize = sizeNode \|\| materialPointSize`，乘以 `screenDPR`；`sizeAttenuation` 时再乘 `0.5*H/-z`；按 `positionGeometry.xy * pointSize / (viewport/2) * clip.w` 偏移四边形顶点。`alphaToCoverage` 可配 `shapeCircle()` 画圆点 |
| `materials/nodes/SpriteNodeMaterial.js` | `positionNode`、`rotationNode`、`scaleNode`；默认 `transparent=true` |
| `materials/nodes/VolumeNodeMaterial.js` | `steps=25`、`BackSide`、`depthTest=false`、`VolumetricLightingModel`（受光体积） |
| `objects/{InstancedMesh,BatchedMesh,Points,Sprite,LOD}.js` | WebGPURenderer 里任意 `Mesh/Points/Sprite` 都可以设 `object.count = N` 做实例化绘制（`RenderObject.getDrawParameters` L630–640） |

### 2.4 与本项目强相关的 examples / addons

| 路径 | 价值 |
|---|---|
| `examples/webgpu_compute_points.html` | 30 万点：`instancedArray` + compute + `Points`（`drawRange.count=1`, `mesh.count=N`）；需要 `requiredLimits: { maxStorageBuffersInVertexStage: 1 }` |
| `examples/webgpu_compute_particles.html` | 20 万 `Sprite` 粒子：重力、弹跳、鼠标冲击，`SpriteNodeMaterial + shapeCircle + alphaToCoverage` |
| `examples/webgpu_compute_particles_rain.html` / `_snow.html` | **俯视正交相机把 `positionWorld.y` 渲到 1024² HalfFloat RT 当碰撞高度图**，粒子落地重生并溅起涟漪；雨滴用 `billboarding({horizontalRotation:true})` 的长条面片 |
| `examples/webgpu_volume_cloud.html` + `jsm/tsl/utils/Raymarching.js` | `RaymarchingBox(steps, cb)`：在单位盒内做 slab 求交，从 back-to-front 累积，alpha ≥ 0.95 提前退出；128³ `Data3DTexture`（CPU `ImprovedNoise`） |
| `examples/webgpu_compute_texture_3d.html` | compute 每帧用 `mx_noise_vec3` 把噪声写进 200³ `Storage3DTexture`（**仅 WebGPU**） |
| `examples/webgpu_fog_height.html`、`webgpu_custom_fog*.html`、`webgpu_postprocessing_fog.html` | 高度雾、`triNoise3D` 动态雾、后处理体积雾（ray-slab、1/4 分辨率 + 双边上采样） |
| `examples/webgpu_struct_drawindirect.html`、`webgpu_compute_rasterizer.html` | compute 用 `atomicStore` 写 indirect 参数后 `drawIndirect`；compute 软光栅（depth 与 payload 打包进 u32，用 `atomicMax` 解决深度测试）+ 视锥剔除 + indirect dispatch |
| `examples/webgpu_performance_renderbundle.html` | `BundleGroup` 对比（4000 个 mesh） |
| `examples/jsm/gpgpu/CountingSort.js`、`BitonicSort.js` | GPU 计数排序：reset、直方图、前缀和、散射，4 个 pass；另有 CPU 版 `computeCPU` 供 WebGL 后端 |
| `examples/jsm/objects/GaussianSplat.js` + `loaders/{SPLAT,SPZ,KSPLAT,GaussianSplatPLY}Loader.js` | 官方 3DGS：WebGPU 下用 GPU CountingSort（4096 bins），WebGL 后端用 CPU 排序；视线方向变化超过阈值（dot < 0.9995）才重排 |
| `examples/jsm/tsl/display/*` | `SSAONode/GTAONode`、`TRAANode`、`FXAANode`、`SMAANode`、`OutlineNode`、`BloomNode`、`DenoiseNode`、`FSR1Node`：后处理积木 |
| `examples/jsm/tsl/utils/SoftParticles.js`、`tsl/math/curlNoise.js` | 软粒子（按深度差淡出）、curl 噪声（沙尘湍流） |
| `examples/jsm/tsl/WebGLNodesHandler.js` | 让经典 `WebGLRenderer` 跑 TSL 材质。限制：不支持 MRT、storage texture 和 WebGPU 后处理栈 |

---

## 3. 可复用算法与实现（含伪代码、参数）

### 3.1 渲染器启动与能力分档（Tier）

```ts
import * as THREE from 'three/webgpu';
export type Tier = 'gpu-high' | 'webgl2' | 'software';

export async function createRenderer(canvas: HTMLCanvasElement, pref: 'auto'|'webgpu'|'webgl' = 'auto') {
  // 1) 先自己探测 adapter，因为 outputBufferType 等参数必须在构造时确定
  let fallbackAdapter = false;
  if (pref !== 'webgl' && navigator.gpu) {
    const ad = await navigator.gpu.requestAdapter({ featureLevel: 'compatibility' } as any);
    fallbackAdapter = !!ad?.info?.isFallbackAdapter;          // SwiftShader
  }
  const softwareHint = fallbackAdapter || /[?&]soft=1/.test(location.search);
  const renderer = new THREE.WebGPURenderer({
    canvas, antialias: false,                       // 点云不用 MSAA；需要时后处理用 FXAA/TRAA
    forceWebGL: pref === 'webgl',
    trackTimestamp: true,                           // 没有该 feature 时后端会自动置 false
    powerPreference: 'high-performance',
    outputBufferType: softwareHint ? THREE.UnsignedByteType : THREE.HalfFloatType,
    reversedDepthBuffer: true,                      // 大场景的深度精度；WebGL2 需要 EXT_clip_control（本机 SwiftShader 支持），没有会自动降级；不要用 logarithmicDepthBuffer（EDL 等深度换算不支持）
  });
  await renderer.init();
  const b: any = renderer.backend;
  let tier: Tier;
  if (b.isWebGPUBackend) tier = fallbackAdapter ? 'software' : 'gpu-high';
  else {
    const gl = b.gl as WebGL2RenderingContext;
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    const name = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '';
    tier = /SwiftShader|llvmpipe|Software|Basic Render/i.test(name) ? 'software' : 'webgl2';
  }
  renderer.onDeviceLost = (info) => bus.emit('render:device-lost', info);  // UI 提示并整页重建渲染器
  return { renderer, tier, compat: b.compatibilityMode === true };
}
```

各档默认参数（初值，之后交给 §3.4 的控制器在线调整）：

| 参数 | gpu-high（WebGPU） | webgl2 | software（SwiftShader，含本机 CI） |
|---|---|---|---|
| 点渲染模式 | `quad`（圆点、size attenuation）或 `pixel` + EDL | `glpoint`（补丁）或 `pixel` | `pixel` |
| 初始点预算 | 3,000,000 | 1,500,000 | 60,000 |
| 可见节点上限 | 256（BundleGroup 下 512） | 256 | 64 |
| EDL | 开（全分辨率） | 开 | 关（实测 20k 点下 EDL 让帧时间从 60 ms 涨到 217 ms） |
| 粒子（雨雪沙） | 200k–500k compute | 20k–50k（TF compute） | ≤5k 或关 |
| 体积云 raymarch steps | 64 | 32 | 关（用 billboard 云） |
| pixelRatio | `min(dpr, 2)` | `min(dpr, 1.5)` | 1 |

说明：headless CI 用 software 档，**只验证功能和相对指标**，不要拿绝对 FPS 做门槛（见 §3.11）。

### 3.2 点云节点渲染（WebGPU 与 WebGL2 的最佳实践）

#### 3.2.1 每节点的 GPU 数据布局（与服务端 tiler 的契约）

| 字段 | 格式 | 字节 | 说明 |
|---|---|---|---|
| `qpos` | `Uint16Array`，itemSize **4**，`normalized=true` | 8 | xyz = (p − node.min) / node.size × 65535；w = intensity 或 classification（可选）。**不能用 itemSize 3**：WebGPU 会逐点重新 pad（CPU 循环），6 字节 stride 也不对齐 |
| `rgba` | `Uint8Array`，itemSize 4，`normalized=true` | 4 | sRGB 颜色；a 可以存 LOD 随机秩（见下） |
| 合计 | | **12 B/点** | 比 f32x3 + rgba8（16 B）省 25%；精度 = node.size/65535，比如 200 m 的根节点是 3 mm |

- 节点 `Points` 对象的变换直接编码量化：`object.position = node.min - worldOrigin`，`object.scale = node.size`（Vector3）；着色器里 `positionLocal = qpos.xyz`（0..1）。
- geometry 上**没有 `position` 属性**，所以必须：`geometry.setDrawRange(0, n)`（否则 `getDrawParameters` 的 itemCount 是 Infinity，直接不画）；`geometry.boundingSphere/boundingBox` 手动设为节点包围体；`material.positionNode = attribute('qpos','vec4').xyz`。
- 世界坐标用**局部 ENU**（原点取 World Package 中心）。UrbanScene3D 上海场景的坐标范围约 7 km，fp32 在 7000 m 处的 ULP 约 0.5 mm，默认的 `mediumpModelViewMatrix` 够用。**不要开** `renderer.highPrecision`：它和 InstancedMesh（无人机编队）不兼容。
- **节点内点序随机打散**（服务端 tiler 做 Fisher–Yates，或按 Morton 交错生成"渐进顺序"），前 k 个点就是均匀子采样，这是 §3.3 用 drawRange 调密度的前提。

#### 3.2.2 共享材质与每节点 uniform（TSL）

```ts
import { Fn, attribute, uniform, vec4, float, clamp, builtin, modelViewMatrix, positionLocal, pointUV } from 'three/tsl';
const _v2 = new THREE.Vector2();
export const pc = {
  sizeScale: uniform(1.0),           // UI 滑杆："点大小"
  minPx: uniform(1.0), maxPx: uniform(6.0),
  screenH: uniform(1080).onRenderUpdate(({ renderer }) => renderer.getDrawingBufferSize(_v2).y),
  slope:   uniform(0.577).onRenderUpdate(({ camera }) => Math.tan((camera as any).fov * Math.PI / 360)),
  // 每节点 uniform：不需要新材质，按 object 注入。spacing 是该节点层级的点间距（世界单位，米）= rootSpacing / 2^level
  spacing: uniform(1.0).onObjectUpdate(({ object }) => object.userData.spacing),
};
// Potree 的 "attenuated" 尺寸：px = sizeScale * spacing_world * projFactor；projFactor = 0.5*H / (tan(fov/2) * -z_view)
const pixelSize = Fn(() => {
  // positionLocal 是 qpos.xyz（0..1）；modelViewMatrix 已含 object.scale=node.size 与平移，所以 mv 是米制视空间坐标
  const mv = modelViewMatrix.mul(vec4(positionLocal, 1));
  const proj = pc.screenH.mul(0.5).div(pc.slope.mul(mv.z.negate()));
  return clamp(pc.sizeScale.mul(pc.spacing).mul(proj), pc.minPx, pc.maxPx);
});
export function makePointMaterial(mode: 'pixel'|'glpoint'|'quad') {
  const m = new THREE.PointsNodeMaterial({ transparent: false, depthWrite: true });
  m.colorNode = attribute('rgba', 'vec4').rgb;                      // 颜色模式可以切换：RGB、高度、强度、分类
  if (mode === 'quad') {
    m.positionNode = attribute('qpos', 'vec4').xyz;                 // 实例属性
    m.sizeAttenuation = false; m.sizeNode = pixelSize();            // 自己算像素尺寸（setupVertexSprite 还会乘 screenDPR）
    // 圆点：m.opacityNode = shapeCircle(); m.alphaToCoverage = true;（MSAA 关时退化为 discard 的效果，按需开）
  } else if (mode === 'glpoint') {
    m.positionNode = Fn(() => {
      builtin('gl_PointSize').assign(pixelSize());                  // 依赖 §3.2.3 的补丁
      return attribute('qpos', 'vec4').xyz;
    })();
  } else {
    m.positionNode = attribute('qpos', 'vec4').xyz;                 // 1px
  }
  return m;
}
```

- **所有节点共享同一个材质实例**，pipeline 与 shader 只编译一次。每节点差异（spacing、level、node id 高亮）一律用 `onObjectUpdate` uniform 注入，**不要按节点 clone 材质**，否则每个 RenderObject 都要重建节点图。
- `quad` 模式的几何：`InstancedBufferGeometry`，四个角点的 `position` 加 `index [0,1,2,0,2,3]`，`qpos/rgba` 改成 `InstancedBufferAttribute`，数量用 `geometry.instanceCount` 控制（相当于 drawRange）。对象用 `THREE.Mesh`，不是 `Sprite`（Sprite 自带共享几何，没法挂实例属性）。实测 `Mesh(InstancedBufferGeometry) + PointsNodeMaterial` 在两个后端都能正常渲染。

#### 3.2.3 WebGL2 后端恢复 `gl_PointSize` 的补丁（已实测可用）

```ts
// 应用启动时、创建任何材质之前执行一次；只影响 WebGL2 后端（WGSL 不走这个 builder）
const proto = (THREE as any).GLSLNodeBuilder.prototype;
const orig = proto._getGLSLVertexCode;
proto._getGLSLVertexCode = function (shaderData: any) {
  const code = orig.call(this, shaderData);
  if (shaderData.flow?.includes('gl_PointSize =')) return code.replace(/\n\s*gl_PointSize = 1\.0;/, '\n');
  return code;
};
// 自检：升级 three 时若模板变了（找不到该行），回退为 pixel 模式，并在 console 记一条 warn
```

实测（`edl.html?forceWebGL=1&sized=1`）：`patchedPointSize=true`，截图里的点呈 2–12 px 自适应方块，帧耗时与 1px 相当（108.5 ms vs 110.6 ms，SwiftShader，2 万点加 EDL）。风险：依赖私有方法 `_getGLSLVertexCode`，**升级时必须跑回归测试**（§6）。

#### 3.2.4 "大量小 Points 对象" vs "合并 buffer"：实测与结论

实测 1：总点数 20 万，拆成 K 个对象；960×540；10 帧取均值；`cpu` 是 `render()` 的 JS 耗时，`wall` 包含 GPU 同步（WebGL 用 `readPixels`，WebGPU 用 `onSubmittedWorkDone`）。

| 路径 | K=1 cpu / wall | K=256 cpu / wall | K=2048 cpu / wall | 每对象 CPU | 首帧（编译） K=1 / 2048 |
|---|---|---|---|---|---|
| 经典 `WebGLRenderer` + ShaderMaterial（gl_PointSize） | 2.0 / 498 ms | 3.7 / 480 ms | 29.8 / 519 ms | **≈14 µs** | 0.70 / 1.02 s |
| `WebGPURenderer`（WebGPU 后端）1px | 1.1 / 580 ms | 8.3 / 573 ms | 46.4 / 706 ms | **≈22 µs** | 0.97 / 2.12 s |
| `WebGPURenderer`（WebGL2 后端）1px | 1.0 / 514 ms | 14.4 / 669 ms | 60.7 / 761 ms | **≈29 µs** | 0.79 / 2.92 s |
| WebGPU 后端 + `BundleGroup`（K=1024，2 万点） | 不带 bundle 15.5 ms → 带 bundle **6.5 ms** | | | ≈6 µs | |

实测 2：quad 与 1px 的代价，以及固定开销：

| 场景 | 结果 |
|---|---|
| 20 万点 quad（实例化四边形）vs 1px Points | WebGPU 9134 ms vs 580 ms；WebGL2 11640 ms vs 514 ms，**约 16–23 倍**（SwiftShader 的三角形 setup 极慢；真实 GPU 估算 2–4 倍：顶点着色 4 次/点，光栅 2 个三角形/点） |
| 1000 点时的固定帧开销 | WebGLRenderer 8.9 ms；WebGPURenderer（HalfFloat 输出）84–100 ms；改成 `outputBufferType: UnsignedByteType` 后降到 **36–64 ms**。原因是 `needsFrameBufferTarget` 带来的离屏 RT 加全屏 output pass，软件渲染下很贵，真实 GPU 上小于 0.3 ms（估算） |
| 1px 点的边际成本 | SwiftShader 约 2–2.5 µs/点，三种路径接近 |
| 显存统计 | 100 万点（f32x3 + rgba8 = 16 MB）时 `info.memory.total` = 21.2 MB（另有约 6 MB 是 960×540 的 HalfFloat 颜色和深度 RT）；全部 `geometry.dispose()` 后回到 5.94 MB 基线（释放完整） |

**最佳实践（按优先级）：**

1. **节点粒度要"粗"**：tiler 的目标是每节点 2–6 万点（Potree 2 的量级），叶子小于 5k 点就合并进父节点。这样 300 万点预算只对应 50–150 个可见节点。
2. **可见节点硬上限 256**（WebGPURenderer 约 22–29 µs/对象，256 个就是 6–7.5 ms；桌面 CPU 估算 3–4 ms），超出按优先级截断。
3. **WebGPU 档**把点云节点挂在一个 `BundleGroup` 下。可见集变化（LOD 更新，5–10 Hz）或节点卸载时设 `bundle.needsUpdate = true`；节点 `frustumCulled=false`，视锥剔除交给 LOD 管理器（bundle 只在录制时剔除一次）。
4. **所有节点共享材质**，用 `renderer.compileAsync(scene, camera)` 或加载时先渲染一帧来预热 pipeline，避免首帧 1–3 s 的卡顿（上表"首帧"列）。
5. **"合并成一个大 buffer"**在 WebGL2 下不推荐：每次 LOD 变化都要 `bufferSubData` 做压实拷贝，而且拿不到每节点的变换和 spacing。WebGPU 下的正确合并方式是 **GPU-driven**：固定页大小（64k 点/页）的 storage 页池，加 compute 视锥与 LOD 剔除，`atomicAdd` 写 indirect args，最后一次 `drawIndirect`（参照 `webgpu_struct_drawindirect.html` 与 `webgpu_compute_rasterizer.html`）。放到 V1.0 做（§4）。

### 3.3 LOD 选择、point budget 分配与渐进加载（three.js 侧实现）

八叉树命名和二进制分块格式由服务端 tiler 决定（参见 r09/r10）。这里给出 three.js 侧的选择算法，公式与 Potree `src/Potree_update_visibility.js` 一致，另加 budget 截断、部分节点 drawRange 和滞回：

```ts
// 常量（初值）
const MIN_NODE_PX = 150;        // Potree PointCloudOctree.minimumNodePixelSize 默认值；software 档设为 250
const MAX_LOADS_INFLIGHT = 4;   // Potree maxNodesLoading = 4
const MAX_UPLOADS_PER_FRAME = 2, MAX_UPLOAD_BYTES_PER_FRAME = 8 << 20;
const UNLOAD_GRACE_MS = 5000;   // 不可见超过 5 s 才进入可卸载 LRU
const PARTIAL_MIN_RATIO = 0.25; // 最后一个节点至少能画 25% 才部分显示

function updateLOD(camera, budget, nodeCap) {
  frustum.setFromProjectionMatrix(camera.projectionMatrix × camera.matrixWorldInverse);
  const H = drawingBufferHeight, slope = tan(fov/2);
  pq.clear(); pq.push(root, Infinity);
  let pts = 0; const visible = []; const want = [];
  while (!pq.empty() && visible.length < nodeCap) {
    const node = pq.pop();
    if (!frustum.intersectsBox(node.box)) continue;
    if (!node.loaded) { want.push(node); continue; }            // 父节点仍显示，天然渐进
    const room = budget - pts;
    if (room <= 0) break;
    const ratio = Math.min(1, room / node.numPoints);
    if (ratio < 1 && ratio < PARTIAL_MIN_RATIO) break;
    node.drawCount = Math.ceil(node.numPoints * ratio * densityScale);   // 预先打散的点序 → 前缀子采样
    visible.push(node); pts += node.drawCount;
    for (const c of node.children) {
      const d = dist(camPos, c.sphere.center), r = c.sphere.radius;
      const px = r * (0.5 * H) / (slope * d);                   // 屏幕像素半径
      if (px < MIN_NODE_PX) continue;
      let w = px * focusWeight(c);                               // 选中无人机或视锥中心附近 ×1.5
      if (d < r) w = Infinity;                                   // 相机在节点内
      pq.push(c, w);
    }
  }
  applyVisibility(visible);              // node.object.visible 与 geometry.setDrawRange(0, drawCount)（quad 模式改 instanceCount）
  scheduleLoads(want.sortBy(w).slice(0, MAX_LOADS_INFLIGHT - inflight));  // fetch 与 Worker 解码
  lru.touch(visible); lru.evictIfOver(gpuBytesCap, UNLOAD_GRACE_MS);      // 调用 geometry.dispose()
}
```

- **调用频率**：相机运动时每 100 ms（10 Hz），静止时每 250 ms，直到队列为空。不要每帧跑。
- **上传节流**：Worker 解码好的 `ArrayBuffer` 放进 `readyQueue`，每帧最多构建并上传 2 个节点或 8 MB（`BufferAttribute` 创建本身很快，首次 render 时才真正上传）。节点**首次**显示可以用 150 ms 的 `densityScale` 淡入（0.2 → 1，通过 drawRange 实现，无 alpha 混合开销）。
- **运动 LOD（Cesium/Potree 惯例）**：交互中（OrbitControls `start` 到 `end` 之间）`budget × 0.6`；停止 300 ms 后恢复，渐进 refine。
- **GPU 内存上限** `gpuBytesCap`：gpu-high 档 1.5 GB，webgl2 档 768 MB，software 档 128 MB。按 `info.memory.total` 与 LRU 自有计数双重控制（注意 WebGPU 后端 vec3 会 pad，WebGPU 的 `info` 统计已包含 pad）。
- **CPU 堆**：WebGPURenderer 不会释放 `attribute.array`（`onUploadCallback` 从不被调用）。LRU 淘汰时 dispose 并丢弃引用；需要重新显示时由浏览器 HTTP 缓存或 IndexedDB 秒级重载。

### 3.4 帧率自适应控制器（FPS 反馈，自动调节点云疏密）

**信号采集（钩子）：**

```ts
class PerfProbe extends THREE.InspectorBase {         // renderer.inspector = new PerfProbe()
  frameStart = 0; interval = 16.7; renders = 0;
  begin()  { super.begin(); const t = performance.now(); if (this.frameStart) this.interval = t - this.frameStart; this.frameStart = t; this.renders = 0; }
  finish() { super.finish(); }                        // 必须调 super，否则 isRunning 不会变，finish 不再被调用（已踩坑）
  beginRender() { this.renders++; }
}
// 在 animationLoop 里：
const t0 = performance.now(); pipeline.render(); const cpuMs = performance.now() - t0;
if (renderer.backend.trackTimestamp) renderer.resolveTimestampsAsync(THREE.TimestampQuery.RENDER); // 延迟 1–3 帧写入 info.render.timestamp
ctrl.sample({ interval: probe.interval, cpuMs, gpuMs: renderer.info.render.timestamp || null,
              draws: renderer.info.render.drawCalls, points: renderer.info.render.points });
```

实测（`hooks.html`）：两个后端都能拿到每帧 `renders=2`（场景一次，output pass 一次）、`draws=4`（3 个 Points 加 1 个 output quad）、`gpuRenderMs`（WebGPU 14.1 ms、WebGL2 15.4 ms，SwiftShader）。**`info` 会在内部 rAF 的每一帧开头 reset**，所以要在同一帧的 render 之后读；自定义循环可以设 `info.autoReset=false` 再手动 `reset()`。

**控制律（AIMD 加分级降质阶梯）：**

```ts
const T = 1000 / targetFps;                  // 默认 60 → 16.7 ms；software 档 20 fps
let ema = T, over = 0, under = 0, lastChange = 0;
function sample(s) {
  // rAF 被显示器刷新率封顶，interval 只能说明"是否掉帧"；余量用 max(cpu, gpu) 估计
  const cost = Math.max(s.cpuMs, s.gpuMs ?? 0, s.interval > 1.2 * T ? s.interval : 0);
  ema = 0.9 * ema + 0.1 * cost;                          // 约 10 帧平滑
  if (ema > 1.10 * T) { over++; under = 0; } else if (ema < 0.70 * T) { under++; over = 0; } else { over = under = 0; }
  const now = performance.now(); if (now - lastChange < 500) return;          // 每 500 ms 最多动一次，防振荡
  if (over >= 3)  { degrade(); lastChange = now; over = 0; }                   // 快降
  if (under >= 60){ upgrade(); lastChange = now; under = 0; }                  // 慢升
}
// 阶梯：先调便宜的、连续的旋钮，再动离散的
const ladder = [
  () => budget = clamp(budget * 0.8, Bmin, Bmax),          // 1) 点预算（连续）
  () => densityScale = Math.max(0.5, densityScale - 0.1),  // 2) 节点内密度（drawRange）
  () => edl.setResolutionScale(0.5) /* 或关 EDL */,         // 3) EDL
  () => scenePass.setResolutionScale(step(0.85, 0.7, 0.5)),// 4) 动态分辨率（量化档位，避免 RT 反复重建）
  () => particles.count *= 0.5,                            // 5) 粒子（改 object.count，零成本）
  () => cloud.steps.value = Math.max(16, cloud.steps.value / 2), // 6) 云步数
];
// degrade()：若 budget > Bmin 只走第 1 步；否则按顺序往下一档。upgrade() 按逆序回升，最后才加 budget（×1.05）
```

- 预算上下限：`Bmin = 50k`（software 档 10k），`Bmax` 取各档初值的 2 倍。
- 旋钮都是**无重编译**操作（uniform、drawRange、count、`setResolutionScale`）。**不要**在降质时切换材质模式（`quad` <-> `pixel`），那会触发 pipeline 重建和卡顿；模式只在设置页或启动时决定。
- UI：状态栏显示 FPS、GPU ms、可见点数和预算；提供质量预设 `Auto / Performance / Quality`，Auto 就是上面的控制器，另外两档锁定参数。

### 3.5 EDL（Eye-Dome Lighting）后处理：TSL 版（两后端已实测可编译运行）

公式照搬 Potree `src/materials/shaders/edl.fs`（Boucheny 2009）：8 个邻域、`radius=1.4`、`edlStrength=1.0`、`shade = exp(-300 · strength · mean(max(0, log2 z − log2 z_nb)))`。

```ts
import { Fn, float, vec4, uniform, uniformArray, uv, pass, If, Loop, max, exp, log2, select, perspectiveDepthToViewZ, cameraNear, cameraFar } from 'three/tsl';
const pipeline = new THREE.RenderPipeline(renderer);
const scenePass = pass(scene, camera);
const colorTex = scenePass.getTextureNode('output'), depthTex = scenePass.getTextureNode('depth');
const edlStrength = uniform(1.0), edlRadius = uniform(1.4);
const invSize = uniform(new THREE.Vector2()).onRenderUpdate(({ renderer }) => { renderer.getDrawingBufferSize(_v2); invSize.value.set(1/_v2.x, 1/_v2.y); });
const NB = 8, nb = uniformArray([...Array(NB)].map((_, c) => new THREE.Vector2(Math.cos(2*c*Math.PI/NB), Math.sin(2*c*Math.PI/NB))), 'vec2');
const isBg = (d) => renderer.reversedDepthBuffer ? d.lessThanEqual(1e-7) : d.greaterThanEqual(0.9999999);
const logDepth = Fn(([st]) => {
  const d = depthTex.sample(st).r;
  const vz = perspectiveDepthToViewZ(d, cameraNear, cameraFar);   // 已内建 reversedDepthBuffer 分支（ViewportDepthNode.js L233）
  return select(isBg(d), float(0), log2(vz.negate()));            // 0 表示背景
});
const edl = Fn(() => {
  const st = uv(); const depth = logDepth(st).toVar(); const sum = float(0).toVar();
  Loop(NB, ({ i }) => {
    const nd = logDepth(st.add(invSize.mul(edlRadius).mul(nb.element(i)))).toVar();
    If(nd.notEqual(0), () => { If(depth.equal(0), () => { sum.addAssign(100); }).Else(() => { sum.addAssign(max(0, depth.sub(nd))); }); });
  });
  return vec4(colorTex.sample(st).rgb.mul(exp(sum.div(NB).mul(-300).mul(edlStrength))), 1);
});
pipeline.outputNode = edl();          // animationLoop 里调 pipeline.render()
```

- `perspectiveDepthToViewZ` 已经按 `renderer.reversedDepthBuffer` 自动切换公式（与 `scenePass.getViewZNode()` 等价），但**不处理 `logarithmicDepthBuffer`**。大场景深度精度应当用 reversed depth，不要用 log depth，这样 EDL、SoftParticles、雾的深度换算都保持正确。
- 让无人机、航线、HUD 不受 EDL 影响有两种做法：用 `scenePass.setLayers()` 把点云单独放一个 pass，再和普通场景 pass 按深度合成；或者用 MRT 输出一个 mask（`setMRT(mrt({ output, mask }))`），合成时只对点云像素乘 shade。MVP 推荐第二种，只需一个 scene pass。
- 代价：9 次深度采样/像素。gpu-high 档可以忽略；software 档实测把帧时间从 60 ms 抬到 217 ms（WebGPU/SwiftShader），所以 software 档默认关。

### 3.6 环境粒子（雨、雪、沙尘、风可视化）：compute 结构

```ts
// 后端相关的 buffer 策略（实测结论）
const isGPU = renderer.backend.isWebGPUBackend;
const N = tierParticleCount;                       // 200k（gpu-high）/ 30k（webgl2）/ 5k（software）
// WebGL2：compute 写入的 buffer 必须是 instancedArray（instanceIndex = gl_InstanceID）
// WebGPU：instancedArray 也能用，但 “1 顶点 × N 实例” 绘制极慢（实测 594 ms vs 19 ms/帧，1 万点，SwiftShader）
//         → 位置 buffer 用 attributeArray，并直接挂为 geometry.position 做非实例化绘制
const pos = isGPU ? attributeArray(N, 'vec3') : instancedArray(N, 'vec3');
const vel = instancedArray(N, 'vec3');
const seed = instancedArray(N, 'float');
const U = {
  camPos: uniform(new THREE.Vector3()), box: uniform(new THREE.Vector3(200, 120, 200)),   // 以相机为中心的循环盒
  windTex: texture3D(windField /* Data3DTexture RGBA16F, (u,v,w) 归一化 */), windMin: uniform(new THREE.Vector3()), windSize: uniform(new THREE.Vector3()),
  windScale: uniform(20),                    // 纹理 [−1,1] → m/s
  fall: uniform(-9.0),                       // 雨：约 2 mm 雨滴终速 9 m/s；雪 −1.0；沙尘 −0.3
  turb: uniform(0.0),                        // 沙尘或湍流强度（curl noise）
  dsm: texture(dsmTex), dsmMin: uniform(new THREE.Vector2()), dsmSize: uniform(new THREE.Vector2()),   // 服务端下发的 DSM 高度图
  dt: uniform(1/60),
};
const update = Fn(() => {
  const p = pos.element(instanceIndex), v = vel.element(instanceIndex);
  const w = U.windTex.sample(p.sub(U.windMin).div(U.windSize)).xyz.mul(2).sub(1).mul(U.windScale);
  const target = w.add(vec3(0, U.fall, 0)).add(curlNoise(p.mul(0.05).add(time.mul(0.1))).mul(U.turb));
  v.assign(mix(v, target, clamp(U.dt.mul(3), 0, 1)));      // 一阶弛豫：粒子速度趋向风速加沉降速度（τ ≈ 0.33 s）
  p.addAssign(v.mul(U.dt));
  const rel = p.sub(U.camPos).add(U.box.mul(0.5)).toVar();
  p.xz = U.camPos.xz.sub(U.box.xz.mul(0.5)).add(mod(rel.xz, U.box.xz));           // 水平循环：有限粒子，无限雨幕（swizzle 赋值写法同官方 rain 示例）
  const ground = U.dsm.sample(p.xz.sub(U.dsmMin).div(U.dsmSize)).r;
  If(p.y.lessThan(ground).or(p.y.lessThan(U.camPos.y.sub(U.box.y.mul(0.5)))), () => {
    p.y = U.camPos.y.add(U.box.y.mul(0.5)).sub(hash(instanceIndex.add(time)).mul(10));  // 顶部重生
  });
})().compute(N, [64]).setName('envParticles');
// 渲染：雨用 billboarding({ horizontalRotation: true }) 的 0.02×0.8 m 长条（沿速度方向拉伸更真实），雪和沙尘用 1–3 px 点
```

- **碰撞地面**：官方 rain 示例是运行时用俯视正交相机渲一张 `positionWorld.y` 的 RT（`webgpu_compute_particles_rain.html`）。对 1px 点云，这张图会有大量空洞，所以本项目改用 **服务端从 Geometry World 体素生成 DSM**（World Package 自带，R16F，1–2 m/px），每个 World 只加载一次。
- **风场纹理**：服务端 Environment Service 按 1–2 Hz 推送 E(x,y,z,t) 的风分量切片（比如 64×64×16，RGBA16F，约 0.5 MB），前端 `Data3DTexture.needsUpdate = true`。两帧之间用两张纹理加 `mix(t)` 做时间插值，避免跳变。
- **风矢量和流线可视化**：同一套 compute，粒子不重生到地面，而是按寿命（2–4 s）重生到随机位置，颜色映射 |w|（lieflat 色板），轨迹用 `Line2NodeMaterial` 或尾迹点。
- **写入读回**：`renderer.getArrayBufferAsync(pos.value)` 读回时要按上传后的 `pos.value.itemSize` 解析。实测 WebGPU 的 stride 是 4（vec3 被 pad），WebGL2 是 3。

### 3.7 体积云、雾、天空

**体积云（设计文档 §25 Level 2）：**
- 几何：一个 `BoxGeometry(1,1,1)` 缩放到云层范围（比如 6 km × 0.8 km × 6 km，底高 800–1500 m；无人机作业高度 ≤ 150 m，所以通常是仰视）。
- 材质：`NodeMaterial`，`side=BackSide`，`transparent=true`，`depthWrite=false`。`colorNode = RaymarchingBox(steps, cb)`（`examples/jsm/tsl/utils/Raymarching.js`），回调里 `texture3D(noise).sample(positionRay + 0.5 + windOffset)`，用 `smoothstep(threshold±range)·opacity` 做 front-to-back 合成，alpha ≥ 0.95 时 `Break()`。
- 噪声：WebGL2 与 software 档用 CPU `ImprovedNoise` 生成 64³–128³ `Data3DTexture(RedFormat)`，在 Worker 里生成（128³ 约 2M 体素，单线程约 100–200 ms，估算）。gpu-high 档可以用 compute `textureStore` 写 `Storage3DTexture`，并随时间演化（`webgpu_compute_texture_3d.html`）。
- 覆盖率映射：`threshold = mix(0.6, 0.15, cloudCover)`，`opacity = 0.2–0.35`；风推进为 `windOffset += windAtCloudAlt * dt / boxSize`。
- `steps`：64 / 32 / 16 三档，由 §3.4 阶梯调节。

**雾（与 E 场的物理对齐）：** 用 Koschmieder 关系把环境场的能见度 V（米）换成消光系数 σ = 3.912 / V。three 的 `densityFogFactor` 是 Exp²（1-exp(-(ρz)²)），和物理不一致，建议自写：

```ts
const sigma = uniform(3.912 / visibility);                // V = 2000 m → σ ≈ 0.00196 /m
const H0 = uniform(80), hs = uniform(60);                  // 雾顶高度和标高：σ(h) = σ·exp(-(h-H0)/hs)（h > H0 时）
const dist = positionView.length();                         // 用视线距离，不用 view.z
const hFactor = exp(positionWorld.y.sub(H0).max(0).div(hs).negate());
scene.fogNode = fog(fogColor, float(1).sub(exp(sigma.mul(hFactor).mul(dist).negate())));
scene.backgroundNode = /* 天空渐变 */;
```

同一个 σ 发给服务端的 Sensor 模块，用于 LiDAR 射程衰减和 RGB 对比度，保证"看到的雾"和"传感器受到的雾"是同一个量（落实设计文档 §23）。

**天空**：`examples/jsm/objects/SkyMesh.js`（TSL 版 Preetham）加 `DirectionalLight` 联动太阳高度角；阴天把 turbidity 调高。

### 3.8 无人机、航线、传感器视锥

- **编队渲染**：每个无人机型号一个 `InstancedMesh`（GLTF 子网格先 `mergeGeometries`，材质尽量合并为一个带顶点色或贴图图集的）。20 架 × 10 个子网格 = 200 个独立对象，按实测每对象约 22 µs 算要 4–5 ms CPU；合并后 1–3 个 draw。`setColorAt` 表示状态（Ready 灰、Flying 白、Alarm 红 #E93024）。旋翼转动用单独的 InstancedMesh（4×N 实例），每帧只写这部分 `instanceMatrix`，配合 `addUpdateRange` 局部上传。
- **遥测插值**：WebSocket 10–50 Hz，渲染延迟 D = 100 ms（大于 2 个包间隔），做快照插值（位置线性插值，姿态四元数 slerp）；超过 250 ms 没收到新包就做 dead-reckoning 外推并在 UI 显示"信号延迟"。
- **`DynamicDrawUsage` 慎用**：WebGPU 与 WebGL2 后端对 `usage === DynamicDrawUsage` 的 attribute **每帧整段重传**（`Attributes.update`）。无人机矩阵只在收到包时设 `needsUpdate` 即可，保持 `StaticDrawUsage`。
- **航线和轨迹**：`Line2NodeMaterial`（`examples/webgpu_lines_fat.html`）画宽线；轨迹用环形缓冲的 `Float32Array` 加 `updateRanges`，每架最多保留 2000 点。航点标记（多种几何）放一个 `BatchedMesh`：1 个 RenderObject，后端逐项 drawIndexed，但省去 RenderObject 开销。
- **FOV 视锥**：半透明 `Mesh` 加 `depthWrite=false`；LiDAR 扫描可视化用 compute 生成的 1px 点环。
- **FPV 画中画**：同一个 canvas 做 scissor/viewport 分区渲染（`webgpu_multiple_elements.html` 的模式，两后端通用）。`CanvasTarget` 多 canvas 只支持 WebGPU，不作为默认方案。

### 3.9 GPU 内存释放规则表

| 资源 | 释放方式 | 注意 |
|---|---|---|
| 点云节点几何 | `geometry.dispose()` | 会销毁该几何上**所有** attribute 的 GPU buffer：不同节点**禁止共享** BufferAttribute；在 BundleGroup 内的对象，dispose 后必须 `bundle.needsUpdate = true`（否则报 "Buffer used in submit while destroyed"，实测复现） |
| 共享点云材质 | 只在卸载整个 World 时 `material.dispose()` | material 的 dispose 事件会让所有 RenderObject 失效并重建 |
| compute buffer | `node.value.dispose()`（`StorageBufferAttribute.dispose`） | `instancedArray` 返回的是 node，dispose 它的 `.value` |
| compute 节点 | `computeNode.dispose()` | 删除 pipeline 与 bindings |
| 纹理 / RT | `texture.dispose()`、`renderTarget.dispose()` | 风场 3D 纹理尺寸不变时复用对象，只改 `image.data` 并设 `needsUpdate` |
| 后处理 | `pipeline.dispose()`；`passNode.dispose()` | 切换场景时一起释放 |
| 渲染器 | `renderer.dispose()` | device lost 之后整体重建 |
| CPU 堆 | 丢弃 `attribute.array` 引用（仅在确定不再 raycast、不再更新时） | WebGPURenderer 不会调 `onUploadCallback` |

验证方法：`info.memory.total` 与各 `*Size` 字段。实测 100 万点 dispose 后回到基线。

### 3.10 拾取（点击设航点、测距）

- 首选**CPU 节点级拾取**：`Raycaster` 先测可见节点的 `boundingBox`，最多取前 8 个节点，再在节点内按量化坐标逐点求点到射线距离（阈值 = 像素阈值 × 距离 × 2·tan(fov/2)/H）。每节点 5 万点，JS 一次约 1–3 ms（估算）。CPU 端数组在 WebGPURenderer 里本来就保留着。
- 备选**深度读回**：把 `scenePass` 的深度拷到 1×1 RT，`readRenderTargetPixelsAsync` 取出后反投影。异步有 1–2 帧延迟，适合"拖拽航点"这类连续操作。

### 3.11 headless 流畅性与回归测试（本机实测可行）

```js
// playwright-core（装在项目本地 devDependency），可执行文件用 ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome
const WEBGPU_FLAGS = ['--headless=new','--enable-unsafe-webgpu','--enable-unsafe-swiftshader',
  '--use-angle=swiftshader','--enable-features=Vulkan','--use-webgpu-adapter=swiftshader'];
// 页面暴露 window.__perf = { frames:[{interval,cpuMs,gpuMs,points,draws}], budget, tier }
// 同步点：WebGL → gl.readPixels(0,0,1,1)；WebGPU → await device.queue.onSubmittedWorkDone()
```

- **断言相对指标，不断言绝对 FPS**：(1) 控制器能在 10 s 内把 `ema` 压到目标的 ±15%；(2) 预算上限内可见点数 ≤ budget；(3) 相机飞行脚本（回放一段固定路径）期间 p95 帧耗时相对基线不退化超过 20%；(4) dispose 后 `info.memory.total` 回到基线 ±1 MB；(5) 加载 UrbanScene3D 某城市时首个有效画面 ≤ N 秒（N 取基线 ×1.2）。
- **截图回归只用 `forceWebGL`**：headless 下 WebGPU canvas 截图是空白或黑屏，`canvas.toDataURL()` 还会触发 `CopyTextureToTexture` 校验错误（实测）。WebGPU 路径只做数值断言；需要像素时渲到 `RenderTarget` 再 `readRenderTargetPixelsAsync`。
- SwiftShader 下的量级：1px 点约 2 µs/点，quad 约 45 µs/点。1 万粒子的 compute 加绘制：WebGPU（非实例化顶点绘制）约 22 ms/帧；WebGL2 的 TF compute 约 270 ms/帧；WebGPU 用"1 顶点 × N 实例"绘制约 594 ms/帧。所以 software 档的预算和粒子数都要压低，这也正好检验控制器的下限逻辑。

---

## 4. 在本项目中的落点与复用方式

| # | 可用模块 / 算法 | 来源（仓库内） | 本项目落点（模块） | 版本 | 复用方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | `WebGPURenderer` 自动回退、`init`、`onDeviceLost` | `src/renderers/webgpu/WebGPURenderer.js`、`common/Renderer.js` | `apps/web/src/render/RendererHost.ts` | V0.1 | adopt | 官方双后端，零成本回退 |
| 2 | 能力分档（adapter info、SwiftShader 识别、compat 模式） | `WebGPUBackend.init`、`WebGLBackend.init` | `render/capabilities.ts` | V0.1 | port（§3.1） | 决定点模式、预算、粒子数 |
| 3 | TSL 点云材质（pixel/glpoint/quad）、每节点 `onObjectUpdate` uniform | `PointsNodeMaterial.js`、`core/Node.js` | `world/pointcloud/PointMaterial.ts` | V0.1 | port（§3.2） | 1px 限制必须绕开 |
| 4 | GLSL `gl_PointSize` 补丁 | `GLSLNodeBuilder._getGLSLVertexCode` | `render/patches/glPointSize.ts` | V0.1 | port（带自检） | WebGL2 下性价比最高的大点方案 |
| 5 | 12 B/点量化布局与 drawRange 密度调节 | `WebGPUAttributeUtils`、`RenderObject.getDrawParameters` | tiler 输出格式 + `PointCloudNode.ts` | V0.1 | port | 省带宽和显存，调密度零成本 |
| 6 | LOD 优先队列、budget、滞回、LRU | Potree 公式 + three `Frustum/Box3/Sphere` | `world/pointcloud/LodScheduler.ts` | V0.1 | port（§3.3） | 渐进加载核心 |
| 7 | 帧钩子 `InspectorBase`、`info`、timestamp query | `InspectorBase.js`、`Info.js`、`Backend.resolveTimestampsAsync` | `render/perf/PerfProbe.ts` + `QualityController.ts` | V0.1 | port（§3.4） | 帧率自适应 |
| 8 | `RenderPipeline` + `pass()` + EDL | `RenderPipeline.js`、`PassNode.js`、Potree `edl.fs` | `render/post/Edl.ts` | V0.1 | port（§3.5） | 点云立体感的关键 |
| 9 | `BundleGroup` | `BundleGroup.js`、`Renderer._renderBundle` | `PointCloudLayer`（仅 WebGPU） | V0.2 | adopt（带失效规则） | CPU 降约 2.4 倍 |
| 10 | InstancedMesh 编队、快照插值、`updateRanges` | `InstancedMesh`、`BufferAttribute.addUpdateRange` | `drone/DroneFleet.ts` | V0.1–V0.2 | adopt | 多机 |
| 11 | `Line2NodeMaterial` 航线和轨迹、`BatchedMesh` 航点 | `materials/nodes/Line2NodeMaterial.js`、`objects/BatchedMesh.js` | `mission/*` | V0.2 | adopt | Mission Layer |
| 12 | compute 粒子（雨、雪、沙尘、风线） | `webgpu_compute_particles_{rain,snow}.html`、`Arrays.js`、`curlNoise.js` | `environment/particles/*` | V0.3 | port（§3.6） | 环境视觉 |
| 13 | 体积云 `RaymarchingBox` | `jsm/tsl/utils/Raymarching.js`、`webgpu_volume_cloud.html` | `environment/cloud/VolumeCloud.ts` | V0.3（WebGL2 用 Data3D）/ V0.4（WebGPU 用 compute 3D） | port | §25 Level 2 |
| 14 | 雾节点、Koschmieder 物理雾 | `nodes/fog/Fog.js` | `environment/fog/FogNode.ts` | V0.3（视觉）→ V0.4（与 Sensor 共用 σ） | port | 视觉与物理一致 |
| 15 | `SkyMesh`（TSL 天空） | `examples/jsm/objects/SkyMesh.js` | `environment/sky` | V0.3 | adopt | |
| 16 | `SoftParticles` | `jsm/tsl/utils/SoftParticles.js` | 雨雾粒子与地物交界 | V0.3 | adopt | |
| 17 | 后处理积木 FXAA/TRAA/Outline/GTAO | `jsm/tsl/display/*` | 选中高亮（Outline）、抗锯齿 | V0.2 | adopt | Outline 用于选中无人机 |
| 18 | `getArrayBufferAsync` 读回 | `Renderer.getArrayBufferAsync` | 调试和测试 | V0.1 | adopt | 注意 stride |
| 19 | GPU-driven 点云（页池、compute 剔除、indirect） | `webgpu_struct_drawindirect.html`、`webgpu_compute_rasterizer.html`、`IndirectStorageBufferAttribute` | `world/pointcloud/gpu/*` | V1.0 | reference | 超大场景一次 draw，但只有 WebGPU |
| 20 | 官方 3DGS（`GaussianSplat` + CountingSort） | `jsm/objects/GaussianSplat.js`、`jsm/gpgpu/CountingSort.js`、SPLAT/SPZ loader | Visual World 的 3DGS 层 | V1.0 | reference（和 Spark 对比后选型） | 与 WebGPURenderer 原生集成、双后端 |
| 21 | `WebGLNodesHandler` | `jsm/tsl/WebGLNodesHandler.js` | — | — | skip | 无 compute、无 MRT，不如直接用 WebGPURenderer 的 WebGL2 后端 |
| 22 | 官方 `Inspector` UI | `jsm/inspector/*` | 仅开发调试（`?debug=1`） | V0.1 | adopt（开发期） | 产品 UI 用 shadcn 自绘性能面板 |

---

## 5. 对比与推荐

本单元只有 three.js 一个仓库，这里对比的是 **three.js 内部的几条渲染路径**，以及它和同组仓库的关系。

### 5.1 渲染路径对比

| 路径 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| **A. `WebGPURenderer`（WebGPU 后端）** | compute、storage、indirect、BundleGroup、timestamp、3D storage texture；TSL 一次编写 | 点只有 1px（大点要 quad，代价 2–4 倍，估算）；每对象 CPU 约 22 µs；首帧编译重 | 主路径（有 GPU 的 Chrome/Edge/Safari 26+/Firefox 141+） |
| **B. `WebGPURenderer`（WebGL2 后端）** | 同一套 TSL 代码；自动回退；TF compute 可跑简单粒子 | 每对象 CPU 约 29 µs；compute 限制多（无 atomics、`instanceIndex` 陷阱、PBO 拷贝）；gl_PointSize 需要补丁 | 回退路径与 CI 截图 |
| C. 经典 `WebGLRenderer` + ShaderMaterial | 每对象 CPU 最低（约 14 µs）；gl_PointSize 原生；Potree/potree-core/three-loader 生态都在这里 | 无 compute，无 TSL 后处理栈；要维护两套材质 | **不作为主路径**；只在 V0.1 需要直接复用 potree-core 时临时考虑 |
| D. `WebGLRenderer` + `WebGLNodesHandler` | 可复用 TSL 材质 | 无 MRT、无 storage texture、无 WebGPU 后处理栈 | skip |

**推荐：A 为主、B 为自动回退，只维护一套 TSL 代码。** 点云模式按后端选 `quad`、`glpoint` 或 `pixel`。potree-core 与 three-loader（同组其他单元）的价值在于**八叉树加载器和 LOD 调度逻辑**，其中的 `ShaderMaterial` 渲染部分要按 §3.2 改写成 TSL，不要为此引入第二个 renderer。

### 5.2 与同组仓库的关系（推荐排序，综合 star、2026 活跃度、契合度）

1. **three.js**（116k stars，2026-09 活跃）：引擎，必选。
2. **pmndrs/react-three-fiber**（另一单元）：可以通过 `gl={async p => { const r = new WebGPURenderer(p); await r.init(); return r }}` 承载 WebGPURenderer。本文的渲染循环、质量控制器和 LOD 调度都应该作为**纯 TS 类**实现，R3F 只负责挂载与 React 状态桥接，避免 React reconcile 进入每帧热路径。
3. **potree-core / pnext three-loader**：移植八叉树 `hierarchy.bin`、`octree.bin` 解码与调度；渲染层用本文的 TSL 材质替换。
4. **Potree-Next**（WebGPU 点云软光栅，另一单元）：V1.0 GPU-driven 路径的算法参考，与 §4 第 19 项互补。
5. **sparkjsdev/spark** 与 three 官方 `GaussianSplat`：V1.0 3DGS 选型时对比。官方实现和 WebGPURenderer 原生集成、双后端可用，是更"省心"的默认候选。

---

## 6. 风险与注意事项

| 风险 | 影响 | 对策 |
|---|---|---|
| **1px 点限制**（WebGPU 与 WebGL2 后端都是） | 远景稀疏、近景出洞 | 三模式（§3.2）；EDL；密度优先的 LOD；gpu-high 用 quad |
| **`gl_PointSize` 补丁依赖私有 API** | three 升级时静默失效（退回 1px） | 启动自检加 CI 截图断言"点直径 > 1px"；锁定 `three@~0.186.1`，升级时专门做回归 |
| **WebGPURenderer 每对象 CPU 开销**（约 1.6–2.1 倍于 WebGLRenderer） | 可见节点多时 CPU 瓶颈 | 节点粗粒度；可见节点上限 256；BundleGroup；共享材质 |
| **BundleGroup 失效规则** | dispose 后没失效 bundle 会报 "Buffer used in submit while destroyed" 并泄漏 | LOD 管理器统一在"可见集变化或卸载"后置 `needsUpdate`；卸载与 dispose 延迟一帧执行 |
| **首帧编译卡顿**（实测 0.4–2.9 s） | 进场和切换图层时卡顿 | 加载页里 `compileAsync(scene, camera)` 预热；材质模式启动后不切换；环境效果的材质在后台预编译 |
| **WebGPU compat 模式**（three 以 `featureLevel:'compatibility'` 请求 adapter） | 在 OpenGL ES 或老 GPU 上 `maxStorageBuffersInVertexStage` 可能为 0，`storage.element()` 在顶点阶段编译失败 | 材质里统一用 `.toAttribute()`；需要时 `requiredLimits: { maxStorageBuffersInVertexStage: 1 }`（同官方 compute_points 示例），失败就 `forceWebGL` 重建 |
| **WebGL2 compute 陷阱** | `attributeArray` 写入时 `instanceIndex` 全为 0；没有 atomics；PBO 拷贝开销 | compute 写入一律用 `instancedArray`；复杂 GPGPU（排序、剔除、直方图）只在 WebGPU 档启用，WebGL 用 CPU 版（例如 CountingSort.computeCPU） |
| **“1 顶点 × N 实例”绘制极慢**（WebGPU/SwiftShader 实测 31 倍） | 粒子卡顿 | WebGPU 下粒子用非实例化顶点绘制（§3.6） |
| **attribute 隐式扩宽和 pad** | 非 normalized 的 Uint16 → Uint32、vec3 → vec4，显存意外翻倍，读回 stride 变化 | 布局按 §3.2.1；读回按 `attr.itemSize` 解析 |
| **`DynamicDrawUsage` 每帧整段重传** | 带宽浪费 | 默认 Static，更新时显式 `needsUpdate` 加 `addUpdateRange` |
| **CPU 堆不释放**（没有 onUpload 回调） | 长时间漫游后 JS 堆增长 | LRU 淘汰时丢弃引用；堆上限监控（`performance.memory` 仅 Chrome） |
| **输出 pass 固定开销** | 软件渲染下 30–85 ms | software 档用 `UnsignedByteType` 输出；有 EDL 时 output 与 EDL 合并在 RenderPipeline 的一个 quad 里 |
| **headless 测试的局限** | WebGPU canvas 截图空白；SwiftShader 数值不可比 | 截图走 forceWebGL；只做相对断言（§3.11）；真机性能基线另外在带 GPU 的机器上采 |
| **精度** | 超过 10 km 的场景或直接使用 UTM 坐标会抖动 | 局部 ENU 原点；节点量化；必要时对点云材质局部使用 `highpModelViewMatrix`（不开全局 highPrecision） |
| **API 变动快**（约每 1–3 个月一个 minor，r181/r183 有改名） | 网上教程过时（`PostProcessing`、`renderAsync`、`hasFeatureAsync`） | 锁版本；统一以本仓库的 examples 为准；升级 PR 必跑 e2e |
| **浏览器 WebGPU 覆盖率** | 部分 Linux、Android 或企业环境仍在 WebGL2 | 自动回退本来就是一等路径，两后端都进 CI |

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§9、§34 "WebGPURenderer + WGSL"，建议改为"WebGPURenderer + TSL（必要时手写 WGSL）"。** TSL 同时编译成 WGSL 和 GLSL，"WebGL2 Fallback"才能真正零成本。手写 WGSL 只能跑 WebGPU，回退时要再写一份 GLSL。纯 WebGPU 的内核（GPU 剔除、排序、软光栅）可以直接写 WGSL（`wgslFn`），但只允许出现在 gpu-high 档的可选功能里。
2. **§12 把 "Point Rendering" 列为 WebGPU 的强项，需要修正。** WebGPU 的点图元只有 1px，点云大点要么用实例化四边形（更贵），要么用 compute 软光栅（V1.0）。WebGPU 真正的红利在 compute（粒子、风场、GPU 剔除与 indirect draw）。建议在 §12 或 §14 明确"点渲染三模式 + 分档"（本文 §3.1–3.2）。
3. **§12 "300,000 particles"要加分档说明。** WebGPU 档 20–50 万没问题；WebGL2 回退走 Transform Feedback，建议 ≤5 万；软件渲染 ≤5k 或关闭。同时给出 compute 写法约束（`instancedArray`）。
4. **§14 "远处 10K、近处 1M" 容易误读，应改成"全局 point budget + 屏幕空间误差排序"**，并补全：`minNodePixelSize=150`、每节点 2–6 万点、可见节点上限 256、最多 4 个并发加载、每帧上传 ≤2 节点或 8 MB、5 s 滞回的 LRU、运动时预算 ×0.6。再加上 **"疏密自动调节三级联动"**：point budget（节点级）、drawRange 密度（节点内）、FPS 反馈控制器（全局）。这正是用户提出的"点云疏密自动调节"的落地定义。
5. **§15 补充 Web 二进制布局**（本文 §3.2.1）：节点局部 `uint16x4 normalized` 加 `unorm8x4`，每点 12 B，节点内点序随机打散。明确**禁止 float16 世界坐标**（与 r01 的结论一致）。3D Tiles 兼容放在 V1.0。
6. **§16 场景结构补四项。** (a) `PostFX` 层：`RenderPipeline`（EDL、Outline、FXAA/TRAA），按 MRT mask 区分点云与其他物体。(b) `layers` 规划：碰撞深度渲染、EDL 只作用于点云、FPV 相机可见性。(c) 坐标约定：局部 ENU 原点和浮动原点策略。(d) 性能元件：`PerfProbe`、`QualityController`、`LodScheduler`，作为 World Runtime 前端的一等公民，而不是事后优化。
7. **§21–25 视觉与物理打通。** 雾用 Koschmieder 的 σ = 3.912/V 作为单一真值（前端雾、服务端 Sensor 共用）。风场粒子直接采样服务端下发的 E 场 3D 纹理（1–2 Hz，时间插值）。雨雪落地用 World Package 自带的 DSM，而不是运行时渲高度图。这样 V0.3（视觉）到 V0.4（物理）不需要推翻前端实现。
8. **§25 云 "Level 2 = 3D Noise Volume" 的实现要写清楚**：`RaymarchingBox` 加 64³–128³ 噪声体、steps 分档 64/32/16、alpha 0.95 提前退出。WebGPU 档可以用 compute 做时间演化，WebGL2 用 Worker 在 CPU 端生成。无人机作业高度下云层多为仰视背景，V0.3 可以先用 Level 1（天空盒加 2D 云），V0.4 再上 Level 2。
9. **§37 刷新频率补充前端内部节拍**：渲染 60 fps（software 档 20）；LOD 调度 10 Hz（运动时）或 4 Hz（静止）；遥测插值渲染延迟 100 ms；风场纹理 1–2 Hz；质量控制器每 500 ms 最多调整一次。
10. **§38–40 UI 增加"性能与质量"入口**：状态栏的 FPS、GPU ms、可见点数和预算小组件（lieflat 风格的迷你图）；质量预设 Auto、Performance、Quality；点大小、EDL 强度、颜色模式（RGB、高度、强度、分类）。FPV 画中画用单 canvas scissor 方案。
11. **§43 MVP 补两项交付物**：(a) **Mock 与性能测试 harness**：headless Chromium（两种后端 flags）、相机脚本回放、相对指标断言、截图回归（forceWebGL）。(b) **device-lost 与 context-lost 恢复**：提示用户并自动重建渲染器与 GPU 资源。
12. **§34 技术栈表建议增加两行**："three 版本锁定 `~0.186.1`（每季度评估升级）"，以及 "Visual World 3DGS 候选：three 官方 `GaussianSplat`（双后端）与 Spark"。
13. **§4.1 "浏览器不是仿真核心"的原则正确，但要补一句"浏览器可以做可视化用的 GPU 计算"**：粒子、风线、GPU 剔除、3DGS 排序都在浏览器侧 compute 完成，且不回传服务器。这样可以和 §13 "WebGPU 不做什么"呼应，边界更清晰。

---

### 附：本单元实测脚本与原始数据

- 目录：`/data/projs/anet-drone/.cache/research/r11/`：`probe.mjs`（能力探针）、`bench.mjs` 加 `www/bench.html`（draw 扩展性、quad vs 1px、BundleGroup、dispose）、`edl.mjs` 加 `www/edl.html`（TSL EDL、gl_PointSize 补丁、截图）、`compute.mjs` 加 `www/compute.html`（compute 双后端、实例化 vs 顶点绘制、读回 stride）、`hooks.mjs` 加 `www/hooks.html`（InspectorBase 钩子、timestamp）。
- 运行方式：`cd .cache/research/r11/www && python3 -m http.server 8765` 起静态服务，再 `node <script>.mjs '<cases-json>'`。`www/three` 是指向 `refs/web3d/three.js/build` 的符号链接（只读）。
- 截图：`shot_edl_webgl.png`（1px 加 EDL）、`shot_edl_webgl_sized.png`（补丁后自适应点大小）。
