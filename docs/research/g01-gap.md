# G1 补充深挖：渲染器路线端到端验证——经典 WebGLRenderer + WebGLNodesHandler、WebGPURenderer(forceWebGL) + gl_PointSize 补丁、WebGPURenderer(WebGPU) 三方对比与 RenderBackend 定案

> 研究单元：G1（00-index §9 缺口 G1 的补充深挖）｜日期：2026-09-28｜关联单元：r11、r12、r14、n01、r16、n05，并与 G2（PointPool + DrawTable）、G6（环境 TSL）交叉核对
>
> 版本：`three@0.186.1`（源码对照 `refs/web3d/three.js` @ `110fbbe`，r187dev）、`@react-three/fiber@9.8.1`、`@react-three/drei@10.7.9`、`react@19.3.0`、Vite 8.3.1、playwright-core 1.63.0、Chromium 151（chromium-1234）。
>
> 环境：本机无 GPU。WebGL2 走 ANGLE + SwiftShader（Vulkan），WebGPU 走 SwiftShader fallback adapter（`--enable-unsafe-webgpu --use-webgpu-adapter=swiftshader`）。关闭 GPU 程序缓存（`--disable-gpu-program-cache --disable-gpu-shader-disk-cache`），编译类测试每次都新开浏览器。
>
> 实验工程：`/data/projs/anet-drone/.cache/research/g01/`。`src/common.js` 是后端工厂，`src/feat.js` 是 28 项功能矩阵，`src/bench.js` 是性能测试，`src/pool.js` 是 G2 的 PointPool 单 draw 对照，`src/r3f.jsx` 是 R3F + drei 矩阵，`run.mjs` 是 playwright 驱动，`results/*.jsonl` 是原始数据，`shots/` 是截图。
>
> **负载说明**：测量期间有其他研究单元在并行跑 Chromium，load average 从约 1 升到 12–20（8 核）。凡是绝对毫秒数都会被放大，**结论以同一批次内交错测得的比值为准**。每组数据都注明了批次：A、B 为低到中负载，A2、B2、C、D2、P 为高负载。

---

## 0. 结论速览

**RenderBackend 定案**：Tier A（硬件 WebGPU）用 `WebGPURenderer`；Tier B（硬件 WebGL2）和 Tier S（软件渲染，含 CI）用经典 `WebGLRenderer`，并挂上 `AnetNodesHandler`（`WebGLNodesHandler` 的子类，约 25 行）。`WebGPURenderer(forceWebGL)` 不进入生产，只保留为回归对照。**所有图层只写一套 TSL**，包括点云、EDL、云合成、粒子、雾、无人机和轨迹，不需要 GLSL 第二实现。

| # | 结论 | 依据（本机实测，数据文件见 §9） |
|---|---|---|
| 1 | **经典渲染器 + handler 能跑本项目需要的全部 TSL 功能，输出与 WebGPURenderer 逐像素一致**。验证过的功能：`vertexIndex` 无属性扁平四边形、`PointsNodeMaterial`（1 px 与实例化四边形）、自定义 `Fn` 雾（材质内、`scene.fog`、`scene.fogNode`）、`onObjectUpdate`/`onRenderUpdate`/`onFrameUpdate` uniform、`texture3D`、`Loop` 光线步进、深度纹理 + `perspectiveDepthToViewZ`（含 reversed-Z）、`MeshStandardNodeMaterial` 光照、`LineBasicNodeMaterial`、`Line2NodeMaterial` 粗线、`depthNode` 写深度、整数纹理 `textureLoad` 与二分查找（G2 的 PointPool） | `F_feat_*.json`：28 项中，应当一致的 16 项三后端数值完全相同（如 `onObjectUpdate` 为 64/128/191/255，EDL 暗像素 340，云着色像素 1478，粗线 1004 px；光照一项在去掉回读行序差异后相同）。其余差异全部在预期内：点径、ShaderMaterial、共享材质的 InstancedMesh、RenderPipeline 与 compute、回读行序、handler 副作用 |
| 2 | **EDL 与体积云合成不需要 GLSL 版**。写法是"显式 RenderTarget（颜色 + DepthTexture）+ 全屏四边形 NodeMaterial"，一份 TSL 在三个后端输出相同。共享代码中**禁止使用** `RenderPipeline`、`pass()`、MRT、storage texture。"EDL 只作用于点云"有两种不依赖 MRT 的做法，均已验证：①点云单独渲染到 RT，EDL 全屏四边形用 `depthNode` 把点云深度写回屏幕，之后其他图层正常做深度测试；②用 alpha 通道做图层掩码，非点云图层设 `transparent:true, blending:NoBlending, opacityNode=0.5` | `depthTex_EDL_cloud_fsq(_reversedZ)` 与 `mask_alpha_and_fsq_depthWrite` 三后端相同。注意：不透明材质的 `opacityNode` 会被强制写成 1，所以方法②必须设 `transparent + NoBlending` |
| 3 | **点径**：经典路径用 `GLPointsNodeMaterial`，即在 `onBeforeCompile` 里删掉 GLSL 模板末尾的 `gl_PointSize = 1.0;`，并给 `customProgramCacheKey` 加后缀。这只用公开 API。`WebGPURenderer` 的 WebGL2 后端只能改私有的 `GLSLNodeBuilder.prototype._getGLSLVertexCode`。WebGPU 没有点径，用 1 px 或四边形 | 不修复时 400 px（1 px/点），修复后 6400 px（4×4），逐对象点径 2/4 px 得到 960/3040 px，三组都符合预期。**必须改缓存键**：handler 只按节点图（`constructor.prototype.customProgramCacheKey`）区分程序，节点图相同的两个材质会共用一个未修复的程序 |
| 4 | **stock `WebGLNodesHandler` 有两个缺陷，由 `AnetNodesHandler` 修复**：①渲染到 RT 时也做 sRGB 编码和色调映射，多 pass 链会重复编码（0.6 灰色经过 RT 再全屏输出，得到 231，正确值是 203）；②忽略 `scene.fogNode`，只认 `scene.fog` | 修复后，RT 中为线性值 128，上屏为 188；`scene.fogNode` 的自定义 Fn 雾生效（stock 下输出白色，修复后输出红色） |
| 5 | **handler 路径的 7 条使用约束**：①每个 `InstancedMesh` 必须有自己的材质；②`material.onBeforeRender` 会被覆盖；③每画一个 NodeMaterial 对象 `info.render.frame` 加 1；④首次 build 后会在 microtask 里 `geometry.dispose()` 一次；⑤**int/uint uniform 失效**（UBO 一律按 Float32 写入），只能用 float uniform，在 shader 里转 `int()`；⑥逐对象变化的 uniform 会让每个对象都重传一次 UBO，每节点参数应改由模型矩阵推导；⑦`onRenderUpdate` 在首帧可能多触发一次，回调必须幂等 | ①共享材质时第二个 InstancedMesh 为 0 px；②③④见 `handler_sideEffects`（0 次调用，frame 加 9，1 次 dispose 事件）；⑤int uniform 时点云覆盖只有 9386 px，改 float 后为 185071 px；⑥B2：每对象墙钟 440 µs 降到 178 µs（SwiftShader，高负载）；⑦render 回调在 2 次渲染中被调用 3 次 |
| 6 | **固定帧开销**：`WebGPURenderer` 默认有一个 HalfFloat 离屏缓冲加 output pass（`needsFrameBufferTarget`），在软件渲染下代价很大。改成"direct 输出"可以消除：`outputColorSpace=LinearSRGB`、`NoToneMapping`，再用 `renderer.contextNode = context({ getOutput })` 在每个材质里做 sRGB 编码。经典渲染器没有这项开销 | A 批（1 个对象、1000 点、960×540 墙钟中位数）：经典 2.9–3.1 ms；wgpu-gl 34.1 ms，u8 输出 16.5 ms，direct 2.8 ms；wgpu 42.5 ms，u8 24.1 ms，direct 17.2 ms。direct 模式实测上屏 188、RT 128，编码正确 |
| 7 | **每对象开销**（JS 提交耗时，比值可迁移到真机）：经典 GLSL 约 6–7 µs，WebGPURenderer 约 9–11 µs，经典 + handler 约 15–21 µs。点云按 G2 走单 draw，这项开销只影响少量非点云对象，**不构成选型因素** | B（K=1024）与 B2（K=256）。SwiftShader 下每个 draw 的墙钟约 40–60 µs，四条路径相近 |
| 8 | **首帧（冷编译）**：经典 ≈ wgpu-gl direct < wgpu-gl 默认 < wgpu。WebGPU 的 `compileAsync` 真正预建了 pipeline，之后首帧 CPU 只需 20–40 ms；经典的 `compileAsync` 在 SwiftShader 上**没有省掉首帧成本**（之后首帧 CPU 仍需 230–470 ms，因为 ANGLE 延迟到首次 draw 才建管线）。所以预热一律用"在加载遮罩下真实渲染一帧" | C（960×540，64k 点，7 种材质，高负载）：冷首帧经典 1051 ms，GLSL 版 1142，wgpu-gl 1262，其 direct 941，wgpu 1343，其 direct 1245。D2（Tier S，480×270）：经典 940，wgpu-gl direct 928，wgpu-gl 1126，wgpu 1352 |
| 9 | **R3F 9 + drei 10**：经典 + handler 下，23 个 drei 组件与 TSL 材质同场景，全部无报错，其中 22 个画面正确（Outlines 看不到描边，与本项目无关，未深究），事件拾取正常。WebGPURenderer（两个后端）下 `GizmoHelper` 会崩掉整个 Canvas；`Line`、`Segments`、`Edges` 在 WebGPU 后端上让整帧失效；`Text` 渲染成色块；`Grid`、`Sky`、`Stars`、`Sparkles`、`Outlines`、`PointMaterial` 不显示或静默退化。**`gl` 必须用 async 工厂**：同步工厂下 drei `Html` 跑 300 帧仍未挂载，async 工厂下正常 | `R_r3f.jsonl`（69 条）加 5 条补测；截图在 `shots/` |
| 10 | **CI**：headless 截图拿不到 WebGPU canvas（结果是白屏），所以像素回归一律用 RT 回读。WebGPU 的 `readRenderTargetPixelsAsync` 行序是自上而下，与 WebGL 相反，由 `RenderBackend.readPixels` 统一。另外，**在 WebGPU 后端给 DataTexture 设 WebGL 专用的 `internalFormat='RGBA32UI'` 会让整数纹理静默读出 0** | `readback_row_order`：wgpu 的 row100 与 row20 对调；`pool.js`：wgpu 设了 `internalFormat` 时只有 1 px，去掉后 84277 px（1 px 点） |

**给 00-index 的修订**（详见 §8）：
- C1 按上表关闭。
- §3.6 中"Tier B/S 的 EDL、云合成提供 GLSL 版"改为"TSL 单源（全屏四边形），不用 RenderPipeline"。
- §3.5 的 RenderBackend 从"Glsl 与 Tsl 两个实现"改为"一个 TSL 实现 + 三种点径策略"。
- §10.1 补充本文数字。

---

## 1. 方法

### 1.1 三种后端组合（`src/common.js::createBackend`）

| 代号 | 构造 | 点径 | 输出 |
|---|---|---|---|
| `classic` | `new WebGLRenderer({antialias:false, powerPreference:'high-performance', reversedDepthBuffer})`，再 `setNodesHandler(new AnetNodesHandler())`，并设 `r.reversedDepthBuffer = capabilities.reversedDepthBuffer` | `GLPointsNodeMaterial`（公开 API） | 着色器内编码（WebGLRenderer 原生行为） |
| `wgpu-gl` | `new WebGPURenderer({forceWebGL:true, outputBufferType})`，`await init()`；启动时执行 `patchGLPointSize()`（私有 API） | 原型补丁 | 默认：HalfFloat 离屏缓冲 + output pass；`u8`：UnsignedByte 缓冲；`direct`：无离屏缓冲，由 contextNode 在材质内编码 |
| `wgpu` | `new WebGPURenderer()`，走 SwiftShader WebGPU fallback adapter | 无（1 px 或四边形） | 同上 |

### 1.2 测量项

- **功能矩阵**（`feat.js`）：每项渲染到 128×128 的 RT，回读像素后与预期比对。共 28 项。
- **固定开销 A/A2**：1 个 Points 对象、1000 点、960×540。
  - `wall` = `render()` + 同步。WebGL 用 1 px `readPixels` 同步，WebGPU 用 `onSubmittedWorkDone`。
  - `rAF` = 2 s 内 rAF 驱动的帧间隔中位数，包含 canvas 呈现。
- **每对象 B/B2**：K 个对象 × 32 点，共享材质。斜率 = (t_K − t_1)/(K−1)。
  - `perobj=1`：每对象 uniform（handler 用 `onObjectUpdate`，GLSL 用 `object.onBeforeRender` + `uniformsNeedUpdate`）。
  - `perobj=0`：常量。
  - `perobj=2`：由模型矩阵推导，`spacing = length(modelWorldMatrix[0].xyz) · k`。
- **应用场景 C/D2**，组成如下：
  - 点云节点：12 B/点量化，共享材质；高度色带 + 材质内高度雾 + `gl_PointSize` 1–4 px。
  - 程序化网格地面、天空穹顶渐变。
  - 200 架无人机：`InstancedMesh` + `MeshStandardNodeMaterial` + 半球光与平行光。
  - 20 条轨迹：`LineBasicNodeMaterial`。
  - 3000 个雨滴：`vertexIndex` 扁平四边形、加法混合。
  - 可选 EDL（全屏四边形 TSL）。

  测量冷首帧（新浏览器）、`compileAsync`、稳态墙钟与 rAF。
- **PointPool 单 draw P**（`pool.js`，G2 的 O3d 形态）：120k 点（60 个节点 × 2000）；RGBA32UI 池纹理加 DrawTable 二分查找；无属性 `Points`，一次 draw；点径 2 px。对比经典 GLSL3 `RawShaderMaterial` 与三条 TSL 路径。
- **R3F 矩阵**：`<Canvas gl={工厂}>`，内容为 TSL 平面 + TSL 点云，再加 1 个 drei 组件，共 23 个组件 × 3 个后端。在第 40 帧合成一次点击，第 60 帧用 RT 回读计算覆盖率，同时记录控制台错误和截图。

---

## 2. 源码核对：handler 的工作机制与限制根因

| 位置 | 事实 | 影响 |
|---|---|---|
| `src/renderers/WebGLRenderer.js` L1077 `setNodesHandler` | 只做两件事：`nodesHandler.setRenderer(this)`，以及保存 `_nodesHandler`。挂钩点：L1364/L2174 `setObject`（每个对象每帧调用）、L1403/L1655 `renderStart`、L2261 `build`（仅在新建程序时调用）、**L2265 build 之后调用 `material.onBeforeCompile(parameters)`**、L2568 `onUpdateProgram` | 因为 `onBeforeCompile` 在 build 之后执行，可以用它改 handler 生成的 GLSL，这就是 `GLPointsNodeMaterial` 的依据 |
| `examples/jsm/tsl/WebGLNodesHandler.js` L31–38 | 源码自述的限制：不支持 VSM、MRT、transmission、WebGPU 后处理栈、storage texture；fog/environment 不会自动更新；instanced mesh 的几何体不能共享 | 共享代码中禁止使用 RenderPipeline、MRT、compute |
| 同上 L304 `getOutputCallback` | 无条件调用 `toneMapping(renderer.toneMapping)` 和 `workingToColorSpace(out, renderer.outputColorSpace)`，不区分当前是否绑定了 RT | 多 pass 链会重复编码。修复见 §6.2 |
| 同上 L144 `SceneContext.getFogNode()` | 只把 `scene.fog`（Fog/FogExp2）转换成节点；WebGPURenderer 的 `NodeManager.getFogNode()`（`src/renderers/common/nodes/NodeManager.js` L620–624）会优先使用 `scene.fogNode` | 自定义 Fn 雾在 stock handler 下失效，修复见 §6.2 |
| 同上 L315–340 `onBeforeRenderCallback` | 通过 `material.onBeforeRender = …`（L455）注入，每个对象执行一次 `renderer.info.render.frame++`，目的是强制 UBO 按对象重传（`src/renderers/webgl/WebGLUniformsGroups.js` 按 frame 去重） | 用户的 `material.onBeforeRender` 被覆盖；`info.render.frame` 失去"帧"的语义；每个对象都会检查并重传所有 UBO 组 |
| 同上 L511–525 `updateGeometryAttributes` | 把 builder 生成的实例属性（如 `instanceMatrix` 节点缓冲）**写入当前对象的 geometry**，随后 `queueMicrotask(() => geometry.dispose())` | 程序命中缓存的第二个 InstancedMesh 拿不到注入的属性，因此 0 px；首个对象会多上传一次 |
| `src/renderers/webgl/WebGLUniformsGroups.js` L302 | `uniform.__data = new Float32Array(…)`：UBO 成员一律按 Float32 写入 | TSL 在 GLSL 下把所有非纹理 uniform 放进 `layout(std140)` 块（`GLSLNodeBuilder.getUniforms` L911 起），所以 int/uint uniform 读到的是 float 的位模式 |
| `src/renderers/webgl-fallback/nodes/GLSLNodeBuilder.js` L1702 | 顶点模板在 `${flow}` **之后**硬编码 `gl_PointSize = 1.0;` | 用户在 flow 里赋的点径会被覆盖；handler 路径同样继承这一行（`WebGLNodeBuilder extends GLSLNodeBuilder`） |
| `src/renderers/common/Renderer.js` L2662 `needsFrameBufferTarget` | `toneMapping !== None` 或 `outputColorSpace !== workingColorSpace` 时，先渲染到离屏缓冲，再加一个全屏 output pass | 这是 WebGPURenderer 固定开销的来源；direct 模式同时关掉这两个条件 |
| `src/nodes/core/NodeBuilder.js` L3126–3130 | `renderer.contextNode.getFlowContextData()` 会合并进 builder.context；`NodeMaterial.setupOutput` 在 L553 调用 `builder.context.getOutput` | 这是 direct 模式的公开钩子 |
| `src/nodes/display/ViewportDepthNode.js` L227–239 | `perspectiveDepthToViewZ` 通过 `builder.renderer.reversedDepthBuffer` 选择公式；经典 WebGLRenderer 没有这个属性，只有 `capabilities.reversedDepthBuffer` | 经典路径必须手动设 `r.reversedDepthBuffer`（proxy 的 set 会转发到目标对象），实测 reversed-Z 下线性深度结果正确 |
| `src/renderers/WebGLRenderer.js` L1227–1252 | 非索引几何体没有 `position` 属性时，`drawEnd` 只受 `drawRange.count` 限制 | 无属性 `Points` 与扁平四边形在经典路径可用。**不要为了消除警告去加一个假的 position**，否则会被 `position.count` 截断 |
| `react-three-fiber/packages/fiber/src/core/configuration.ts` L106–111 `createRenderer` | `gl` 是函数时直接 `return gl(defaults)`，可以是 Promise；`isRenderer` 的判定只是 `!!def.render` | 经典渲染器 + handler 可以用 async 工厂接入（推荐，见 §5） |

---

## 3. 功能兼容矩阵（功能 × 后端）

数值为 RT 回读结果（128×128；8 位线性 RT）。标记：✔ = 与参考一致；△ = 可用，但需要本文给出的写法；✘ = 不可用。

| 功能 | classic + AnetNodesHandler | WebGPURenderer / WebGL2 | WebGPURenderer / WebGPU | 备注 |
|---|---|---|---|---|
| `Points` + `PointsNodeMaterial`（1 px） | ✔ 400 | ✔ 400 | ✔ 400 | |
| TSL 中写 `builtin('gl_PointSize')`（未修复） | ✘ 400（被模板覆盖） | △ 6400（依赖全局原型补丁） | ✘（WGSL 没有点径） | |
| 点径修复 | △ 6400，`GLPointsNodeMaterial`（公开 API） | △ 6400，`patchGLPointSize`（私有 API） | — | |
| 逐对象点径（`onObjectUpdate` + `gl_PointSize`） | ✔ 960 / 3040 | ✔ 960 / 3040 | — | 预期 800 / 3200（加上跨行溢出，总和 4000 与预期一致） |
| GLSL `ShaderMaterial` 点 | ✔ 6400 | ✘ "not compatible" | ✘ | |
| 实例化四边形精灵（`Mesh(InstancedBufferGeometry)` + `PointsNodeMaterial.sizeNode`） | ✔ 6400 | ✔ 6400 | ✔ 6400 | |
| **`vertexIndex` 无属性扁平四边形**（`drawRange = 6N`） | ✔ 6400 | ✔ 6400 | ✔ 6400 | 三端都会有一次 "position not found" 警告，无害 |
| `scene.fog`（Fog） | ✔ 红 | ✔ | ✔ | |
| **`scene.fogNode = fog(color, Fn)`** | △ 红（需要 AnetNodesHandler；stock 为白） | ✔ 红 | ✔ 红 | |
| 材质内自定义 Fn 雾（`mix(c, fogColor, factor(positionWorld))`） | ✔ 红 | ✔ | ✔ | 点材质推荐用这种写法 |
| **`onObjectUpdate` uniform + 共享材质** | ✔ 64/128/191/255 | ✔ 相同 | ✔ 相同 | stock handler 输出为 137/188/225（在 RT 中被 sRGB 编码） |
| `onRenderUpdate` / `onFrameUpdate` | ✔（首帧 render 回调多 1 次） | ✔ | ✔ | 回调要写成幂等 |
| 两个 `InstancedMesh` 共享一个 NodeMaterial | ✘ 400 / **0** | ✔ 400 / 400 | ✔ | 经典路径：每个 InstancedMesh 一个材质 |
| 两个 `InstancedMesh` 各用各的材质 | ✔ 400 / 400 | ✔ | ✔ | |
| 深度纹理 + `perspectiveDepthToViewZ`（线性深度 40 m / 80 m） | ✔ 102 / 204 | ✔ 102 / 204 | ✔ 102 / 204 | |
| 同上，**reversed-Z** | ✔ 102 / 204（需设 `r.reversedDepthBuffer`） | ✔ | ✔ | |
| **EDL：全屏四边形 TSL**（8 邻域，r=1.5） | ✔ 暗像素 340 | ✔ 340 | ✔ 340 | 所有中心像素和角落像素都是 153，也就是无重复编码 |
| **半分辨率 `Loop(32)` 光线步进云 + 按深度合成** | ✔ 着色像素 1478，近处遮挡的中心不着色 | ✔ 1478 | ✔ 1478 | |
| RT（线性）与上屏（sRGB）区分 | ✔ 128 / 188 | ✔ RT 128 | ✔ RT 128 | |
| direct 输出（contextNode `getOutput`） | — | ✔ 上屏 188 / RT 128 / 再上屏 188 | ✔ RT 128；上屏无法回读，节点路径与 WebGL2 后端相同 | 渲染对象按 RenderContext 分开缓存，RT 与屏幕不会串用 |
| alpha 图层掩码（`transparent + NoBlending + opacityNode`） | ✔ α=255 / 128 | ✔ | ✔ | 不透明材质的 `opacityNode` 会被忽略（α 恒为 255） |
| **全屏四边形 `depthNode` 写深度**，之后的图层做深度测试 | ✔ | ✔ | ✔ | EDL 方法①的依据 |
| `MeshStandardNodeMaterial` + 平行光与环境光 | ✔ | ✔（数值与经典路径完全相同） | ✔ | WebGPU 的差异来自回读行序 |
| `texture3D`（Data3DTexture） | ✔ 200/100/50 | ✔ | ✔ | 可用于风场或噪声体 |
| `LineBasicNodeMaterial` | ✔ 108 | ✔ | ✔ | |
| **`Line2NodeMaterial` 粗线**（`three/addons/lines/webgpu/Line2.js`） | ✔ 1004 | ✔ 1004 | ✔ 1004 | 轨迹可以用 TSL 单源粗线，替代 drei `Line` |
| 整数纹理 `textureLoad` + `Loop` 二分查找 + `int` 运算（G2 PointPool） | ✔ 185071（**uniform 必须是 float**） | ✔ 185071 | ✔ 84277（1 px；**不要设 `internalFormat`**） | 见 §4.5 |
| `RenderPipeline` + `pass()` | ✘ | ✔ | ✔ | |
| compute、`instancedArray`、storage | ✘ | △ Transform Feedback 模拟（r11） | ✔ | |
| RT 回读行序 | 自下而上 | 自下而上 | **自上而下** | 在 RenderBackend 中统一 |
| `material.onBeforeRender` | ✘ 被覆盖 | ✘ 未调用 | ✘ 未调用 | 三端都不能依赖它，改用 `object.onBeforeRender` 或节点更新回调 |

---

## 4. 性能对比

### 4.1 固定帧开销（1 个对象、1000 点、960×540）

| 配置 | A：cpu / wall（ms） | A：rAF 间隔（fps） | A：冷首帧 | A2（高负载）：wall / rAF |
|---|---|---|---|---|
| classic + handler（TSL 点材质） | 0.20 / **3.1** | 16.7 ms（59.9） | 151 ms | 10.6 / 33.3 ms |
| classic（GLSL 点材质） | 0.10 / **2.9** | 16.7（59.9） | 87 | 11.7 / 33.3 |
| wgpu-gl，HalfFloat + output pass（默认） | 0.70 / **34.1** | 50.0（20.0） | 291 | 56.6 / 66.7 |
| wgpu-gl，UnsignedByte 缓冲 | 0.70 / 16.5 | 33.3（30.0） | 270 | 36.5 / 50.0 |
| **wgpu-gl，direct** | 0.30 / **2.8** | 16.7（59.9） | 138 | 8.5 / 33.3 |
| wgpu（SwiftShader WebGPU），默认 | 0.90 / 42.5 | 49.9（20.0） | 323 | 69.0 / 66.7 |
| wgpu，UnsignedByte | 0.90 / 24.1 | 16.8（59.5） | 265 | 44.8 / 49.9 |
| wgpu，direct | 0.60 / 17.2 | 16.7（59.9） | 176 | 30.2 / 33.3 |

解读：
- WebGPURenderer 在软件档多出的 14–40 ms 全部来自 output pass（HalfFloat 缓冲更贵）。direct 模式让 WebGL2 后端与经典渲染器持平。
- SwiftShader 的 WebGPU 后端即使用 direct 仍有约 17 ms 的呈现和队列成本，所以 **fallback adapter 必须判为 Tier S 并切到经典渲染器**。这与 00-index §3.6 的探测规则一致。
- 在真 GPU 上 output pass 估计小于 0.3 ms（r11 估算），Tier A 保持默认输出（HalfFloat，可以做色调映射）。

### 4.2 每对象开销

| 配置 | B：CPU 斜率 perobj 1 / 0（µs） | B：墙钟斜率 1 / 0（µs） | B2：CPU 斜率 1 / 2 | B2：墙钟斜率 1 / 2 | 冷首帧 K=1024（B） |
|---|---|---|---|---|---|
| classic + handler | 15.6 / 14.4 | **116.5** / 61.4 | 20.8 / 17.6 | **440** / 178 | 309–337 ms |
| classic GLSL | **6.2** / 5.7 | 49.2 / 46.6 | 7.5 / 7.1 | 186 / 148 | 225 ms |
| wgpu-gl | 11.0 / 10.6 | 56.2 / 49.8 | 11.4 / 11.4 | 164 / 158 | 588–617 ms |
| wgpu | 9.4 / 11.0 | 39.1 / 48.2 | 9.0 / 9.4 | 110 / 129 | 662–695 ms |

（perobj：1 = 逐对象 uniform，0 = 常量，2 = 由模型矩阵推导。B2 中 WebGPURenderer 用 direct 输出。）

解读：
- handler 的 CPU 开销约为经典 GLSL 的 2.5 倍，来自 `onBeforeRender` 注入、节点更新和逐对象 UBO 检查。
- 逐对象变化的 uniform 会让 SwiftShader 上每个 draw 的墙钟翻倍以上（ANGLE 的 buffer ghosting）。改由模型矩阵推导（perobj=2）后，与 GLSL 和 WebGPURenderer 持平。
- 按 G2 的定案，点云只有 1 个 draw。其余图层（3 档无人机、轨迹、环境、地面、天空）合计不超过 30–60 个对象，handler 多出的约 10 µs/对象即 0.3–0.6 ms，**不影响选型**。

### 4.3 应用场景：冷首帧与 compileAsync（C，960×540，64k 点，高负载）

| 配置 | 冷首帧（直接渲染） | compileAsync + 首帧 | 之后首帧 CPU | 冷首帧 + EDL |
|---|---|---|---|---|
| classic + handler | **1051 ms** | 208 + 1231 | 472 ms | 1419 |
| classic（点材质为 GLSL） | 1142 | 170 + 907 | 234 | 1353 |
| wgpu-gl | 1262 | 443 + 913 | 39 | 1548 |
| wgpu-gl direct | **941** | 435 + 718 | 20 | 1424 |
| wgpu | 1343 | 562 + 964 | 28 | 1671 |
| wgpu direct | 1245 | 576 + 796 | 20 | 1637 |

各配置输出一致：classic、classic-GLSL、wgpu-gl 的覆盖率都是 43.67%（红色像素 12647–12650）；wgpu 为 1 px 点，覆盖率 18.3%。

结论：
- 冷首帧在四条路径之间差 ±25%，在噪声量级，不是选型因素。
- 经典路径的 `compileAsync` 在 SwiftShader 上**不能消除**首帧成本。在 WebGPU 上可以消除，但总时间不变。
- 统一做法：数据就绪后，在 `Progress` 遮罩下先对 1×1 RT 真实渲染一帧（`renderer.setRenderTarget(warmRT); render(); setRenderTarget(null)`），再揭开遮罩。这与 00-index §3.5"TTFP ≤ 1 s"的口径一致：TTFP 从首批点落地开始计时，编译算在加载阶段。

### 4.4 Tier S 实况（D2：480×270，即 soft-min 的 0.5 渲染比例；40 节点 × 1000 点加全套图层，高负载）

| 配置 | 冷首帧 | 稳态 wall | rAF 间隔（fps） |
|---|---|---|---|
| classic + handler | 940 ms | 74.3 ms | 83.3（12） |
| classic GLSL | 929 | 74.7 | 66.7（15） |
| wgpu-gl | 1126 | 81.9 | 83.3（12） |
| wgpu-gl direct | 928 | 74.4 | 66.6（15） |
| wgpu | 1352 | 86.4 | 99.9（10） |
| wgpu direct | 1171 | 75.9 | 66.7（15） |

在 Tier S 真实规模下，帧成本由光栅化主导，各路径差异缩小到 0–15%。rAF 的 12 与 15 fps 是 vsync 量化后的相邻两档，差一个量化级。负载正常时（A 批的负载水平），同一场景可以达到 G2 给出的 30 fps 目标。

### 4.5 PointPool 单 draw：TSL 对 GLSL（P，120k 点，960×540，2 px，高负载）

| 配置 | µs/点 | 冷首帧 | 覆盖像素 |
|---|---|---|---|
| classic GLSL3 `RawShaderMaterial`（G2 原型） | 1.395 | 346 ms | 185071 |
| **classic + handler，TSL** | **1.465（+5%，在噪声内）** | 416 | **185071（逐像素一致）** |
| wgpu-gl，TSL | 1.398 | 389 | 185071 |
| wgpu，TSL（1 px） | 1.306 | 508 | 84277 |

**结论**：G2 的 O3d（无属性 `Points` + `vertexIndex` + DrawTable 二分 + 池纹理 `textureLoad`）可以写成 TSL，一份源码覆盖三个后端，SwiftShader 上与手写 GLSL 等速。G2 §4.4 中"RawShaderMaterial GLSL3"改为 TSL，GLSL 版只保留在 dev 对照页。需要注意的两个坑：
1. `nDraw` 等整数参数必须用 float uniform，在 shader 里 `int(u)`。int uniform 在 handler 下只有 9386 px。
2. WebGPU 下不要设 `texture.internalFormat`。three 会从 `RGBAIntegerFormat + UnsignedIntType` 推导出 RGBA32UI（WebGL）或 rgba32uint（WebGPU）。G2 的 RGB32UI（12 B/点）在 WebGPU 上没有对应的三通道格式，**Tier A 用 RGBA32UI（16 B/点）或 storage buffer**。

---

## 5. R3F 9.8.1 + drei 10.7.9 组合实测

`gl` 工厂写法见 §6.4。表中"✔"表示 RT 覆盖或截图可见，且无错误。readback 看不到的叠加层（Gizmo、Hud）以截图为准；WebGPU 后端无法截图，这类组件只能以"无报错"为判据。

| drei 组件 | classic + handler（与 TSL 材质同场景） | WebGPURenderer / WebGL2 | WebGPURenderer / WebGPU |
|---|---|---|---|
| CameraControls、OrbitControls | ✔ | ✔ | ✔ |
| **GizmoHelper** + GizmoViewport | ✔（截图可见） | ✘ `capabilities.getMaxAnisotropy` 未定义，**整个 Canvas 崩溃** | ✘ 同左 |
| Html | ✔（**需要 async 工厂**；同步工厂下 300 帧仍为 0 个 DOM 节点） | ✔ | ✔ |
| Line、Segments（Line2/LineMaterial） | ✔ | ✘ 不渲染 | ✘ `drawIndexed` 非有限值，**整帧失效** |
| Edges | ✔ | ✘ 只画出盒子本体，不画边线（LineMaterial 不兼容） | ✘ 整帧失效 |
| Text（troika） | ✔ | ✘ 渲染成红色块 | ✘ 同左 |
| Grid、Sky、Stars、Sparkles | ✔ | ✘ "ShaderMaterial is not compatible" | ✘ |
| Outlines | △ 无报错，但截图中看不到描边（未深究，本项目不用） | ✘ "ShaderMaterial is not compatible" | ✘ |
| PointMaterial（`onBeforeCompile`） | ✔ 6 px | ✘ 退化为 1 px | ✘ |
| Instances、Detailed、Bvh（点击命中）、AdaptiveDpr、AdaptiveEvents、PerformanceMonitor、Billboard、Bounds、Hud | ✔ | ✔ | ✔ |
| JSX 中的 `meshStandardNodeMaterial`（`extend`） | ✔ | ✔ | ✔ |
| R3F 事件（合成点击 → `onClick`） | ✔ 1 次 | ✔ | ✔ |

其他现象：
- React 19.3 加 drei `Html` 在三个后端都会打印 "Attempted to synchronously unmount a root while React was already rendering"，不影响功能。
- R3F 9.8.1 会打印 `THREE.Clock` 弃用警告，无害。
- R3F 默认 `toneMapping = ACESFilmic`。经典 + handler 下，它由 `getOutputCallback` 只在上屏时施加，RT 中保持线性。建议 Canvas 设 `flat`，保证品牌红 `#E93024` 不被色调映射改色。

**drei 使用规则（修订 00-index §3.6 白名单）**：
- 共享代码只用后端无关的组件：CameraControls、Html（仅用于 1–3 个选中卡片）、Detailed、Bvh、AdaptiveDpr/AdaptiveEvents、PerformanceMonitor、Instances、Billboard、Bounds、Hud、View。
- GizmoHelper、Line、Text、Grid **不进共享代码**，替代方案：
  - 视角 gizmo：DOM/SVG ViewCube，跟随相机四元数，不占 GPU pass。
  - 轨迹粗线：`Line2NodeMaterial`，三后端已验证。
  - 文字：LabelLayer DOM 覆盖层。
  - 地面网格：TSL 程序化网格（本文 bench 中的地面材质）。

---

## 6. 定案实现（可直接落地）

### 6.1 RenderBackend 接口（`web/src/viewport/renderer.ts`，对应 00-index §3.6 目录约定）

```ts
import * as THREE from 'three/webgpu'
import { WebGLRenderer } from 'three'
import { AnetNodesHandler } from './anetNodesHandler'

export type Tier = 'A' | 'B' | 'S'
export type PointSizeMode = 'glpoint' | 'pixel' | 'quad'
export interface RenderBackend {
  tier: Tier
  kind: 'webgpu' | 'webgl2'
  renderer: THREE.WebGPURenderer | WebGLRenderer
  caps: { compute: boolean; mrt: boolean; renderPipeline: boolean; pointSize: boolean; timestamp: boolean; readbackTopDown: boolean }
  pointSizeMode: PointSizeMode                // Tier B/S: 'glpoint'; Tier A: 'pixel'（加 EDL）或 'quad'
  createRT(w: number, h: number, o?: { depthTexture?: boolean; halfFloat?: boolean }): THREE.RenderTarget
  readPixels(rt: THREE.RenderTarget, x: number, y: number, w: number, h: number): Promise<Uint8Array>  // 统一为自下而上行序
  warmup(scene: THREE.Scene, camera: THREE.Camera): Promise<void>  // 对 1×1 RT 真实渲染一帧
  dispose(): Promise<void>
}

export async function createRenderBackend(canvas: HTMLCanvasElement, pref: 'auto' | 'webgpu' | 'webgl' = 'auto'): Promise<RenderBackend> {
  let tier: Tier = 'B'
  if (pref !== 'webgl' && navigator.gpu) {
    const ad = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' })
    if (ad && !(ad as any).info?.isFallbackAdapter) tier = 'A'          // fallback adapter（SwiftShader）按 Tier S 处理
  }
  if (tier === 'A' && pref !== 'webgl') {
    const r = new THREE.WebGPURenderer({ canvas, antialias: false, reversedDepthBuffer: true, trackTimestamp: true })   // 保持默认 HalfFloat 输出
    await r.init()
    if ((r.backend as any).isWebGPUBackend) return wrapGpu(r, 'A')
    await r.dispose()                                                     // init 时回退到了 WebGL2：弃用，改走经典路径
  }
  const r = new WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance', reversedDepthBuffer: true, stencil: false })
  r.setNodesHandler(new AnetNodesHandler())
  ;(r as any).reversedDepthBuffer = r.capabilities.reversedDepthBuffer  // 供 perspectiveDepthToViewZ 等 TSL 深度函数读取
  const gl = r.getContext(); const dbg = gl.getExtension('WEBGL_debug_renderer_info')
  const name = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) : ''
  return wrapGl(r, /swiftshader|llvmpipe|software|basic render/i.test(name) ? 'S' : 'B')
}
// wrapGl：readPixels 用同步 readRenderTargetPixels，包成 Promise
// wrapGpu：readPixels 用 readRenderTargetPixelsAsync，再做行翻转（实测 WebGPU 为自上而下）
```

- `?rb=webgl|webgpu` 与设置页可以强制指定后端；运行中不切换。
- `onDeviceLost` 与 `webglcontextlost` 统一触发整页重建渲染器。
- `wgpu-gl`（WebGPURenderer 加 forceWebGL）只用于 CI 回归变体，不作为生产档位。

### 6.2 `AnetNodesHandler`（`web/src/viewport/anetNodesHandler.ts`，已实测）

```ts
import { WebGLNodesHandler } from 'three/addons/tsl/WebGLNodesHandler.js'
import { workingToColorSpace } from 'three/tsl'
export class AnetNodesHandler extends WebGLNodesHandler {
  constructor() {
    super()
    const self = this
    // 修复 1：渲染到 RT 时输出线性、不做色调映射，与 WebGLRenderer.getParameters() 一致
    //（WebGLPrograms 已把 outputColorSpace/toneMapping 纳入程序键，所以缓存是安全的）
    this.getOutputCallback = function (out: any, builder: any) {
      const r = self.renderer, rt = r.getRenderTarget()
      if (rt !== null && rt.isXRRenderTarget !== true) return out
      if (builder?.material?.toneMapped !== false) out = out.toneMapping(r.toneMapping)
      return workingToColorSpace(out, r.outputColorSpace)
    }
  }
  // 修复 2：支持 scene.fogNode（与 WebGPURenderer 的 NodeManager.getFogNode 一致）
  renderStart(scene: any, camera: any, target = scene) {
    super.renderStart(scene, camera, target)
    const ctx = this.renderStack[this.renderStack.length - 1].sceneContext
    if (target.fogNode) ctx.fogNode = target.fogNode
  }
}
```

升级风险：这个子类依赖 handler 的三处内部结构（`getOutputCallback` 实例属性、`renderStack[].sceneContext.fogNode`、proxy 转发 `getRenderTarget`）。§7 的回归页会逐项断言。

### 6.3 点材质：一个 Fn，三种点径（`engine/pointcloud/render/pointMaterial.ts`）

```ts
export class GLPointsNodeMaterial extends THREE.PointsNodeMaterial {        // 仅用于经典路径，公开 API
  static get type() { return 'GLPointsNodeMaterial' }
  onBeforeCompile(p: any) { p.vertexShader = p.vertexShader.replace(/\n\s*gl_PointSize = 1\.0;/, '\n') }
  customProgramCacheKey() { return super.customProgramCacheKey() + '|glps' }   // 必须加：handler 只按节点图区分程序
}
export function makePointMaterial(be: RenderBackend, fetch: PointFetch /* G2 PointPool：vertexIndex → {pos, cls, nodeIdx} */) {
  const m = be.pointSizeMode === 'glpoint' ? new GLPointsNodeMaterial() : new THREE.PointsNodeMaterial()
  const vQz = varyingProperty('float', 'vQz')
  m.positionNode = Fn(() => {
    const p = fetch.position()                                  // TSL：textureLoad(pool) + DrawTable 二分，uniform 一律为 float
    vQz.assign(fetch.heightNorm())
    if (be.pointSizeMode === 'glpoint') builtin('gl_PointSize').assign(liteSizePx())   // G2 §5.3 Lite 点径，clamp 到 [1, maxPx]
    return p
  })()
  m.colorNode = applyFog(ramp(vQz))                            // 与环境模块共用同一个雾因子 Fn
  return m
}
```

- Tier A 的 `quad` 模式：同一个 `fetch`，改为 `drawRange = 6·Σcnt`，`pid = vertexIndex / 6`，四边形角点按 `vertexIndex % 6` 取（本文 T6 的公式）。
- 自检：启动时用这个材质把 1 个 4 px 的点画到 8×8 RT，回读得到的点亮像素少于 4 个，就说明 three 升级改了 GLSL 模板、删除失效。此时 `pointSizeMode` 降级为 `pixel`，并打一条告警（与 `feat.js` T3 的判据相同）。

### 6.4 R3F 宿主写法

```tsx
<Canvas flat dpr={tierDpr} frameloop="never" camera={{ fov: 55, near: 1, far: 20000 }}
  gl={async ({ canvas }) => { const be = await createRenderBackend(canvas as HTMLCanvasElement, pref); backendRef.current = be; return be.renderer as any }}>
```

- **三档都用 async 工厂**，同步工厂下 drei Html 不挂载。
- `flat`：NoToneMapping。
- 渲染调度由 `@pmndrs/scheduler` 调用 `advance()`，写法与 n05 相同。
- NodeMaterial 由引擎命令式创建，不走 JSX；如果要在 JSX 中使用，需要 `extend({ MeshStandardNodeMaterial })`。

### 6.5 后处理与合成：统一"显式 RT + 全屏四边形 TSL"

```
Tier S：点云和其余图层直接上屏；不做 EDL，不做体积云（00-index §3.7 Low 档）
Tier B / A：
  pass 1  setRenderTarget(cloudRT{color, depthTexture}) → render(cloudLayer)
  pass 2  setRenderTarget(null) → render(edlQuad)           // colorNode = EDL(colorT, depthT)；depthNode = depthT.sample(uv)（把点云深度写回屏幕）
  pass 3  autoClear=false → render(otherLayers)            // 无人机、轨迹、地面、环境，与点云深度正确遮挡
  pass 4  （Med/High 云）setRenderTarget(cloudHalfRT) → render(raymarchQuad)；上屏时 render(compositeQuad, 混合 + 深度门控)
```

- EDL 与云合成的 TSL 代码见 `src/feat.js::depthPipelines`（EDL、半分辨率 `Loop` 云、按深度合成），三后端逐像素一致。
- Tier A 如需 TRAA、bloom 等**可选**效果，可以另外接 `RenderPipeline`，但不能出现在 Tier B/S 的必经路径上。
- 拾取在 G2 中定为"ID 写入 R32UI 或 RGBA8 附件"。由于不能用 MRT，改为**按需单独渲染一次 ID pass**：1×1 scissor 的 RT，用 `RenderBackend.readPixels` 读回。

---

## 7. 各图层"写一套还是两套"（修订 00-index §3.5–§3.7）

| 图层 | 共享 TSL 源 | 后端差异 | 已验证项 |
|---|---|---|---|
| 点云（G2 PointPool + DrawTable） | ✔ fetch、Lite 点径、着色、雾 | 点径策略（glpoint / pixel / quad）；Tier A 在 V0.3+ 可把 fetch 换成 storage 实现 | 4.5、T3、T3b |
| EDL | ✔ 全屏四边形 | Tier S 关闭 | T10、T11d |
| 雨、雪、沙尘（Low/Med） | ✔ `vertexIndex` 无状态扁平四边形 | High 档的 compute 版只在 Tier A | T6，bench 中的雨 |
| 雾 | ✔ `scene.fogNode = fog(color, heightFogFactor)`，点材质中调用同一个 Fn | 无（经典路径依赖 AnetNodesHandler 的修复 2） | T7 |
| 体积云 | ✔ 光线步进四边形 + 合成四边形；噪声体用 CPU 生成的 `Data3DTexture` | compute 写 Storage3DTexture 只在 Tier A | T10 云、T13 |
| 天空 | ✔ 渐变 Fn（或 `SkyMesh`，TSL 实现，但 handler 下尚未实测） | 无 | bench 天空 |
| 无人机 | ✔ `InstancedMesh` + `MeshStandardNodeMaterial` | 经典路径每个 InstancedMesh 一个材质（3 个 LOD 档本来就各有几何体） | T9、T12 |
| 轨迹 | ✔ `Line2NodeMaterial`（粗线）或 `LineBasicNodeMaterial` | 无 | T14、T11e |
| 标签、视角 gizmo | DOM/SVG | 无 | — |
| 拾取 | ✔ ID pass | 回读 API 与行序由 RenderBackend 统一 | T11f |

每节点、每对象参数的写法：
- 点云节点的 spacing 与 level：从 DrawTable 或节点表纹理读取，或 `length(modelWorldMatrix[0].xyz)·k`。
- 无人机：用实例属性或实例纹理。
- **不在热路径上用逐对象 `onObjectUpdate`**。

---

## 8. 对 00-index 的具体修订

1. **§0 第 5 条、§8 C1**：关闭。定案为"Tier A 用 WebGPURenderer；Tier B/S 用经典 WebGLRenderer + AnetNodesHandler；全部图层一份 TSL；点径分三种策略"。原裁决中"需要的后处理（EDL、体积云合成）提供 GLSL 版"**作废**，改为"显式 RT + 全屏四边形 TSL，禁止在共享路径用 RenderPipeline/pass()/MRT"。"补丁方案保留为备选"改为"`wgpu-gl` 只作 CI 回归变体"。
2. **§3.5**：RenderBackend 从"Glsl 与 Tsl 两个实现"改为"一个 TSL 实现 + 点径策略 + 回读适配"。G2 §4.4 的点着色器改为 TSL（等速且逐像素一致），GLSL3 版只保留在 dev 对照页。
3. **§3.6**：
   - 探测规则不变：fallback adapter 判为 Tier S。补充：WebGPU 初始化失败时直接改用经典渲染器，不用 WebGPURenderer 的 WebGL2 回退。
   - R3F 的 `gl` 一律用 async 工厂，Canvas 设 `flat`。
   - drei 白名单按 §5 修订：GizmoHelper 改为 DOM/SVG ViewCube；轨迹粗线用 `Line2NodeMaterial`。
   - "首帧前调用 `compileAsync` 预热"改为"加载遮罩下对 1×1 RT 真实渲染一帧"。
4. **§3.7**：雾统一为 `scene.fogNode` + 同一个因子 Fn；Low 档的"在点材质里逐顶点算"仍然成立，写成同一个 Fn 的顶点版即可。
5. **§10.1** 追加：
   - 固定开销：经典 2.9 ms；wgpu-gl 34 ms（HalfFloat）、16.5 ms（u8）、2.8 ms（direct）；wgpu 42.5 ms、24.1 ms、17.2 ms。
   - 每对象 CPU：GLSL 6–7 µs，WebGPURenderer 9–11 µs，handler 15–21 µs。
   - handler 下逐对象 uniform 的墙钟代价约 2.5 倍。
   - PointPool 的 TSL 与 GLSL 之比为 1.05。
   - 冷首帧 0.93–1.35 s（Tier S 应用场景）。
6. **G2 追补**：Tier A 的池纹理用 RGBA32UI（16 B/点）或 storage；**任何后端都不设 `texture.internalFormat`**；DrawTable 的 `nDraw` 等参数用 float uniform。

---

## 9. 回归测试与复现

**回归页**（three、R3F、drei 升级时必跑，放进 CI）：
- `feat.js` 的 28 项断言，在经典路径与 WebGPU SwiftShader 两个后端上运行（WebGPU 用 RT 回读，不截图）。
- 重点断言：
  - `points_glPointSize_fixed == 6400`：GLSL 模板没有改动。
  - `onObjectUpdate == [64,128,191,255]`：AnetNodesHandler 的 RT 线性输出仍然有效。
  - `fog_sceneFogNode == 红`：修复 2 仍然有效。
  - `instancedMesh_sharedMaterial`：如果上游修复了共享问题，会变为 400/400，届时可放宽约束。
  - `pool` 的覆盖像素三端一致（GL 为 185071）。
- R3F 冒烟：`r3f.jsx` 中的 base、CameraControls、Html、Hud、Instances、Bvh，外加一个 TSL 材质，在两个生产后端上运行。

**复现命令**：
```bash
cd /data/projs/anet-drone/.cache/research/g01
npx vite --port 8791 --strictPort --host 127.0.0.1 &          # 开发服务器
node run.mjs feat '[{"backend":"classic"},{"backend":"wgpu-gl"},{"backend":"wgpu"}]'        # 功能矩阵
./suite.sh; ./suite2.sh; ./suite3.sh                           # A/B、C、A2/B2/D2（结果写入 results/*.jsonl）
node run.mjs pool '[{"backend":"classic","mat":"glsl","nofmt":"1"},{"backend":"classic","nofmt":"1"},{"backend":"wgpu","direct":"1","nofmt":"1"}]'
VW=640 VH=360 SHOT=shots node run.mjs r3f '[{"backend":"classic","comp":"GizmoHelper"}]'
python3 analyze.py; python3 analyze2.py
```

**数据文件**（`.cache/research/g01/results/`）：

| 文件 | 内容 |
|---|---|
| `F_feat_{classic,wgpu-gl,wgpu}.json` | 功能矩阵 |
| `A_fixed.jsonl`、`A2_fixed.jsonl` | 固定开销 |
| `B_perobj.jsonl`、`B2_perobj.jsonl` | 每对象开销 |
| `C_app.jsonl` | 应用场景的编译、首帧与稳态 |
| `D2_tierS.jsonl` | Tier S 实况 |
| `P_pool.jsonl` | PointPool TSL 对 GLSL |
| `R_r3f.jsonl` | R3F + drei 矩阵 |
| `interim/` | 过程数据 |
