# R13 研究笔记：Potree-Next（WebGPU）/ Spark / SuperSplat Viewer / antimatter15-splat——WebGPU 点云技巧与 3DGS 图层接入

> 研究单元：r13 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §8（双表达）、§9–§12（Web/WebGPU）、§14–§16（点云与 WorldLayer）、§21–§24（环境视觉）、§41（World Package）
>
> 仓库快照（本文路径均相对各仓库根目录）：
>
> | 仓库 | 本地路径 | commit | 最后提交 | ★ | License |
> |---|---|---|---|---|---|
> | Potree-Next | `refs/web3d/Potree-Next` | `c0f497d` | 2025-10-07 | 124 | BSD-2 |
> | Spark 2.2.0 | `refs/web3d/spark` | `9672638` | 2026-09-25 | 3660 | MIT |
> | SuperSplat Viewer 1.35.2 | `refs/web3d/supersplat-viewer` | `733ad57` | 2026-09-26 | 579 | MIT |
> | splat | `refs/web3d/splat` | `ba182b5` | 2025-11-16 | 3072 | MIT |
>
> 结论均来自源码精读。实测脚本放在 `/data/projs/anet-drone/.cache/research/r13/`：
> - `r13_lod_sim.py`：三种 LOD 选择策略在 UrbanScene3D 真实八叉树上的飞行回放对比（复用 r09 的 `build_fast` 建树）
> - `probe.cjs`：headless Chromium 的 WebGL2 / WebGPU 能力探测
> - `bench.html`、`run_bench.cjs`：CPU 计数排序、WebGPU compute 剔除 + indirect 绘制、WebGL2 点绘制
> - `bench2.html`、`run_bench2.cjs`：WebGL2 属性取数与纹理顶点拉取（vertex pulling）对比
>
> 实测时本机负载 14–22（8 核，其他研究单元在并行跑任务），**绝对耗时偏大，只看同批次内的相对关系**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **Spark 2.2**（World Labs） | Three.js 上最成熟的 3DGS 渲染器。有 LoD 树、RAD 分块格式、虚拟分页 `SplatPager`、Rust/WASM worker 排序、dyno 着色器图 | **port（核心）**：`traverse_lod_trees` 的"预算 + 像素阈值 + 注视点"贪心遍历，移植到点云八叉树；`SplatPager` 的页池、LRU 和按优先级抓取；stateless 粒子（`snowBox`）。**adopt（可选）**：`SparkGaussianLayer`，只在 WebGLRenderer 页面里用（独立的"高保真 3DGS 预览"路由）；`build-lod` 作为离线 LoD 工具。**reference**：RAD 格式、tiny-lod/bhatt-lod 合并 | V0.1（LOD 遍历）→ V0.3（粒子）→ V0.8（3DGS 预览） | ★★★★★ |
| **SuperSplat Viewer**（PlayCanvas） | superspl.at 官方查看器，基于 PlayCanvas 引擎，WebGPU 优先、WebGL 回退。有流式 LOD、预算、实验性随机透明（stochastic）渲染器、体素碰撞 | **port**：WebGPU compute 剔除管线（projector 压实、上一帧 HiZ 遮挡剔除、256 桶深度排序、无 CPU 回读的 indirect args）；体素碰撞 SVO 格式与射线/球体查询（进 Geometry World）；"先锁最粗 LOD 再放开"的渐进揭示。**reference**：随机透明 + TAA、深度拾取。**skip**：整体嵌入（引擎不同、UI 不是 shadcn） | V0.4（碰撞）→ V0.6（compute 剔除） | ★★★★ |
| **Potree-Next**（m-schuetz） | Potree 的 WebGPU 原型重写。存储缓冲顶点拉取、节点表、点 ID 拾取、反向 Z、EDL/HQS、Potree2/Potree3/COPC/3D Tiles 加载、3DGS 实验 | **port（技巧）**：一次 draw 画一个节点（`firstInstance` 作节点号）的顶点拉取、56 B 节点表、`r32uint` 点 ID 附件拾取、反向 Z 无限远投影 + `depth32float`、timestamp-query 分段计时、EDL 参数。**reference**：Potree3 单文件格式（内节点体素化、相对父体素的 childmask 编码、8 B/8 样本的类 BC 颜色块）。**skip**：直接依赖 | V0.1（反向 Z、拾取）→ V0.6（WebGPU 管线） | ★★★ |
| **splat**（antimatter15） | 最薄的 WebGL2 3DGS 实现，约 1500 行 | **reference**：worker 内 16 位单趟计数排序、视线方向变化阈值、节流排序、按重要性排序的渐进流式 `.splat` | V0.8（仅作对照） | ★★ |
| 补充：three.js r186 `GaussianSplat` | TSL 实现的 3DGS。只支持 WebGPURenderer（含 `forceWebGL` 回退），GPU 计数排序（4096 桶），WebGL 后端用 CPU 排序 | **adopt**（r03 结论，本文确认）：`GaussianLayer` 默认实现；LoD 由我们移植的 Spark 遍历在瓦片层完成 | V0.8 | ★★★★★ |

**实现者先读这几条：**

1. **点云 LOD 遍历用"Spark 式预算贪心"，不要照搬 Potree-Next。**
   - Potree-Next 的 `PointCloudOctree.updateVisibility_additive` 只用 `minNodeSize` 截断，**完全不看点预算**。`init.js:443` 把 `settings.pointBudget` 写进 `octree.pointBudget`，但遍历从不读它。
   - 实测（New York，G=64，1M 预算，120 帧无人机飞行）：Potree-Next 平均选 **149 万点，最多 295 万点**，73/120 帧超预算。
   - Potree 1.8 自带的 `minimumNodePixelSize=150`（`PointCloudOctree.js:113`）在无人机高度会严重欠填：平均只用了 47 万点，4 px 空洞率 2.7%；San Francisco 更差，只用 21 万点，空洞率 10.6%。改成 viewer 的默认值 30（`viewer.js:134`）后是 89 万点，空洞率 0.10%。
   - Spark 式遍历（按 `spacing/距离` 排优先级，达到像素阈值就停，预算不够时最后一层按比例取前缀）**恰好填满预算**，细节已经足够时会提前停。1M 预算下平均用 85 万点，空洞率 0.11%，与 P18(30) 相当，还能省约 4% 的点（详见 §3.2）。
2. **"疏密自动调节"要三件事一起做：** 点预算（节点级）、节点内前缀比例（点级，节点内点序预先打散）、自适应点大小（用遍历返回的"实际达到的像素阈值"反推点径）。Spark 的遍历会返回 `pixelLimit`（`lod_tree.rs` 的 `min_pixel_scale`），可以直接驱动点径。软件渲染档（300k 预算）下，4 px 瓦片空洞率在 12–16%，**必须**配合点径放大或屏幕空间膨胀（dilate）。
3. **WebGPU 点云的关键技巧（源码级）：**
   - 存储缓冲顶点拉取，不用顶点属性（Potree-Next `octree.wgsl` 的 `readU8/readF32`）
   - 节点表加 `instance_index`（`draw(n,1,0,nodeIndex)`）
   - `r32uint` 点 ID 附件用于拾取
   - 反向 Z 无限远投影（`Camera.updateProj` 的 `remap`）
   - 256 线程工作组压实 + 每组一次全局原子操作（supersplat `projector.ts`）
   - 上一帧深度的 8 px/32 px 最大深度网格作遮挡剔除（`reduce.ts`）
   - GPU 写 indirect 参数，不回读 CPU（`args.ts`）
   - 256 个对数深度桶做前向后排序，让 early-z 起作用（`order.ts`）
   - 上面这些 V0.6 以后才值得做。**V0.1 先用"统一页池 + 单次 draw"**（§3.3），把 three.js 每对象 22–29 µs 的开销（r11 实测）降到常数。
4. **本机 headless Chromium 可以跑 WebGPU。** 默认 flags 下 `requestAdapter()` 返回 null。加上 `--enable-unsafe-webgpu --enable-unsafe-swiftshader --use-webgpu-adapter=swiftshader --enable-features=Vulkan` 后能拿到 `swiftshader` 适配器，支持 `timestamp-query`、`indirect-first-instance`、`subgroups`、`float32-blendable`，`maxStorageBufferBindingSize` 为 1 GiB。
   - 实测：compute 剔除 + 压实 + 256 桶计数 + indirect 画点在 25 万和 100 万点上都**跑通**，回读的存活数和亮像素都正确。
   - 所以 **CI 可以同时覆盖 WebGPU 与 WebGL2 两条路径**，但速度是软件级的：100 万点 compute 约 0.7 s，加绘制约 3 s。
5. **3DGS 图层的接入决策（与 r03 一致，本文补全接口与预算仲裁）：**
   - 主栈 WebGPURenderer（含 `forceWebGL`）→ `GaussianLayer` 默认用 three.js `GaussianSplat`，按瓦片加 LoD。
   - Spark 只接受 `THREE.WebGLRenderer`（`SparkRenderer.ts:30`），两者**不能共用一个渲染上下文**。只在独立的"3DGS 高保真预览"路由里 adopt Spark，这个路由用 WebGLRenderer。
   - 点云和 3DGS 共用一个**全局渲染预算仲裁器**：splat 的代价按 ≈6–10 个点折算（§4.2）。
6. **UrbanScene3D 的 PLY 进不了 Spark 的"点云模式"。** Spark 用 `x/y/z/red/green/blue` 六个属性判断是否是点云（`rust/spark-lib/src/ply.rs:13`），默认点尺度只有 `DEFAULT_POINT_SCALE=0.001`。我们的 PLY 只有 xyz + 法线、没有颜色（r09 实测），要先经 `ingest` 伪彩色并设置尺度，才能当 splat 预览。
7. **SuperSplat 的体素碰撞格式可以直接移植成 Geometry World 的"占据查询"。** 格式是 BFS 稀疏体素八叉树：u32 节点 = childMask<<24 | baseOffset；实心叶子标记 `0xFF000000`；混合叶子是 4×4×4 = 64 位掩码。它有 DDA 射线、球体/胶囊推出（push-out）和表面法线查询，前端（相机防穿模、航点贴地）和后端（mock LiDAR、碰撞检测）都能用同一份数据。

---

## 1. 仓库概览

| 项 | Potree-Next | Spark | SuperSplat Viewer | splat |
|---|---|---|---|---|
| 语言 / 构建 | 原生 ES Module + importmap，**无构建**（`http-server` 直接跑） | TypeScript + Vite。Rust→WASM（`rust/spark-rs`），WASM 内嵌进 `dist/spark.module.js`（2.6 MB）。**仓库带预构建 dist**，npm 包 `@sparkjsdev/spark@2.2.0` 装上就能用，不需要 Rust | TypeScript + Rollup，peer 依赖 `playcanvas@^2.22` | 单文件 `main.js` + `index.html` |
| 图形后端 | **只支持 WebGPU**（`navigator.gpu`），不支持 WebGL | **只支持 WebGL2**（`THREE.WebGLRenderer`，three ≥ r180） | WebGPU 优先，自动回退 WebGL2；XR 强制 WebGL | WebGL2 |
| 活跃度 | 2025-10 后无提交；README 自称原型（"2.0 WebGPU prototype"） | 2026-09 仍在发版，2.0（2026-04）引入 LoD/RAD/分页 | 2026-09-26 刚合入实验性随机透明渲染器（#314） | 2025-11 仅 README 推荐 Spark，基本停更 |
| 与本项目契合 | 点云 WebGPU 技巧最直接；工程质量是原型级，含死代码（`renderer/writeBuffer.js` 是旧 WGSL 语法） | LoD/流式/排序思想最好，但渲染器与主栈冲突 | WebGPU compute 管线最先进；碰撞与拾取有用 | 教学价值 |
| 关键目录 | `src/potree/octree/`、`src/potree/*.js`（后处理）、`src/modules/gaussians/`、`src/modules/progressive_loader/`、`libs/webgpu-radix-sort/` | `src/SparkRenderer.ts`、`src/SplatPager.ts`、`src/SplatAccumulator.ts`、`src/shaders/`、`rust/spark-rs/src/{lod_tree,sort}.rs`、`rust/spark-lib/src/{rad,tiny_lod,bhatt_lod,chunk_tree,ply}.rs` | `src/viewer.ts`、`src/render/`（stochastic 渲染器与 WGSL）、`src/collision/`、`src/picker*.ts` | `main.js`、`convert.py` |

---

## 2. 源码结构与关键模块

### 2.1 Potree-Next：WebGPU 点云原型

**入口与主循环。**
- `src/Potree.js`：导出与全局 `settings`。默认值为 `pointBudget: 2_000_000`、`minNodeSize: 150`、`useCompute: false`、`splatType: POINTS`。
- `src/init.js::loop → renderNotSoBasic()`，每帧的流程：
  1. 遍历场景图，按 `renderLayer` 和类型分组
  2. 每个八叉树执行 `octree.updateVisibility(camera, renderer)`
  3. `PointCloudOctree.clearLRU(renderer)`
  4. 选择管线：HQS 三趟、EDL、或前向
  5. 画其余物体
  6. 3DGS 画进自己的 RT 后再 compose
  7. 3×3 窗口读 `r32uint` 做拾取
  8. timestamp 结果每 20 帧 `mapAsync` 一次

**关于"compute rasterization"的澄清。**
- Potree-Next 里**没有** compute 光栅化：`useCompute: false`，除 3DGS 深度键和 `libs/webgpu-radix-sort` 外，`src` 下没有任何 `@compute` 点渲染。
- Schütz 的 compute 光栅化（64 位 `atomicMin(depth<<32|color)`）在 WebGPU 上**无法直接移植**，因为 WGSL 没有 64 位原子（见 §6）。

**LOD 遍历**（`src/potree/octree/PointCloudOctree.js`）：
- `updateVisibility_additive`（L64）：`BinaryHeap` 优先队列，根节点权重 `MAX_VALUE`。
- 节点可见性：`frustum.intersectsSphere(sphere) || level <= 2`（L133 起），也就是 L0–L2 永远可见，作兜底。
- 子节点权重计算：`pixelSize = radius/(tan(fov/2)·d)·H`；如果 `< minNodeSize`，就丢弃该子节点（L189）。
  - `wCenter = clamp(1 − |ndc|, 0, 1) + 0.5`，屏幕中心的权重最高可到 1.5 倍。
  - **注意**：子节点的球心和半径用的是**父节点**的包围盒（L164–L168 用 `node.boundingBox` 和 `octreeRadius/2**node.level`）。所以同一父节点的 8 个子节点权重相同，属于简化或缺陷。
- 未加载的节点进入 `loadQueue`，最多 40 个，按 level 排序后加载。
- `REPLACING` 模式在 additive 结果上后处理：只有子节点全部加载完，才替换父节点。
- `clearLRU`（L346）：每帧最多淘汰 5 个"最旧与最新时间戳差 > 100 ms"的节点，连同它的整棵子树。

**加载器。**
- `loader/PotreeLoader.js`：Potree 2.0 格式。
  - `hierarchy.bin` 每条记录 22 B：type u8、childMask u8、numPoints u32、byteOffset i64、byteSize i64。PROXY 节点按需加载子层级（Range 请求）。
  - 全局并发上限 `nodesLoading >= 10`，失败重试（注释写着 "Chrome frequently fails with range requests"）。
- `loader/DecoderWorker_default.js`：int32 定点转 float32，坐标**相对八叉树 min**。输出 **SoA 布局**：`numPoints * byteOffset + j * size`，并补齐到 4 字节对齐（GPU 要求）。根节点顺带统计属性 min/max/mean。
- `loader_v3/Potree3Loader.js`：新单文件 `.potree` 格式。
  - 布局：u32 metaSize + JSON5 元数据 + 层级 + 点缓冲。层级记录 38 B，多了 `byteOffset_unfiltered`、`byteSize_position/filtered/unfiltered`。
  - 按"兄弟节点一批"合并成一次 Range 请求。
  - **内节点是 128³ 体素**（`DecoderWorker_voxels.js`）：根节点直接存 u8×3 体素坐标；子节点只存"父体素的 8 位 childmask"，由父体素推出子体素坐标，每个体素约 1 字节。
  - 颜色是**类 BC 块**：8 个样本一块，块首尾端点 RGB8 共 6 字节，加 16 位 2-bit 插值索引，合计 8 字节。
  - 叶子节点存原始点，BROTLI 压缩。
  - 这个格式能把内节点压到约 1–2 B/体素，但只适合"体素化代表点"的 LOD 语义（REPLACING），与 r09 的 additive 前缀方案不同。
- `CopcLoader.js`：COPC（LAZ 1.4）。

**渲染**（`renderPointsOctree.js` + `octree/octree.wgsl` + `octree/pipelineGenerator.js`）：
- **顶点拉取**：不声明顶点缓冲（`buffers: []`）。每个节点的数据是一个 `array<u32>` 存储缓冲（group 2），着色器里用 `readU8/readU16/readF32/readF64` 按字节偏移解码任意属性类型。F64 在着色器里就地转成 F32。
- **节点表**（group 3）：`struct Node`，56 B/节点（`WGSL_NODE_BYTESIZE=56`，L14）。字段为 numPoints、counter（全局点 ID 的前缀和）、世界包围盒、childmask、spacing、splatType、isVoxelNode、index。
- 每个节点一次 `draw(numElements, 1, 0, nodeIndex)`（L590），用 `firstInstance` 把节点号传进着色器（`@builtin(instance_index)`）。
  - 每个节点的 bind group 按 `geometry.id` 缓存（`getCachedBufferBindGroup`，L382）。
  - 节点表每帧 `writeBuffer` 一次（`updateNodesBuffer`，L323）。
- **属性着色模板**：`pipelineGenerator.js` 用字符串替换 `<<TEMPLATE_MAPPING_*>>`，把每种 mapping（scalar→渐变、vec3→RGB、分类表）的 WGSL 片段注入着色器。管线用 `createRenderPipelineAsync` 异步编译，编译期间这一帧直接跳过该八叉树。
- **两个颜色附件**：`bgra8unorm` 颜色 + `r32uint` 点 ID（`pipelineGenerator.js:192`）。深度用 `depth32float`、`greater-equal`，清屏值为 0，也就是**反向 Z**（L202；`init.js::startPass` 里 `depthClearValue: 0`）。
- **投影**（`scene/Camera.js::updateProj`）：先用 `perspectiveZO`（无限远、z∈[0,1)），再左乘 `remap`（`e[10]=-1, e[14]=1`）。结果 `ndc.z = near/d`，即反向 Z 无限远平面。`EDL.js` 里 `toLinear(d)=near/d` 与之对应。
- **分类过滤**：着色器里读 classification，大于 12 时把 `position.w` 置 0，直接丢弃。
- **点精灵**：WebGPU 的 point-list 恒为 1 px。
  - `SplatType.QUADS` 走 6 顶点实例四边形（`toQuadPos`，边长 `spacing×0.69`）。
  - VOXELS 走 18 顶点立方体的三个面。
  - 默认 POINTS，也就是 1 px。
  - 大点靠后处理：`dilate.js` 在窗口内选最近深度并加抛物面深度惩罚，但源码里 `window = 0` 被写死，属于未完成的实验。

**后处理。**
- `EDL.js`：
  - 8 邻域，响应 `Σ max(log2 d − log2 d_n, 0)/8`
  - 权重 `w = exp(−response·300·0.2)`
  - 深度由 `near/depth` 线性化
- `hqs_normalize.js`：HQS 三趟。
  1. 深度趟：把点推远 0.5%，`viewPos.z *= 1.005`
  2. 加性累积趟：`rgba16float`，one/one 混合，只写不测深度
  3. 归一化趟：5×5 窗口里 `exp(−nl²·5.5)` 加权，除以 w，同时回写深度和点 ID
- `Timer.js` + `init.js::startPass`：timestamp-query 对每个 pass 写 begin/end，`resolveQuerySet` 到 256 B 对齐的缓冲，**结果缓冲池化**，每 20 帧 map 一次，避免 map 冲突。

**3DGS 实验**（`modules/gaussians/`）：
- `GSLoader.js`：按 10 000 个 splat 一批做 Range 读取，Inria PLY 边读边解码（SH0→RGB、sigmoid 不透明度、exp 尺度、四元数归一化），逐批 `writeBuffer`。
- `gaussians_distance.wgsl`：compute 写 `key = bitcast<u32>(−viewPos.z)`，正浮点的位模式与数值同序，然后用 `libs/webgpu-radix-sort` 做 4-way 基数排序（每趟 2 bit）。
- `gaussians.wgsl`：6 顶点实例四边形，协方差投影与特征分解（数学源自 GaussianSplats3D），最大屏幕半径 300 px。
  - 混合用 **front-to-back 的 under 算子**：`src=one-minus-dst-alpha, dst=one`，写到 `rgba16float`。
  - `compose.js` 再按预乘 alpha 叠到屏幕上。
  - 3DGS 读的是**点云的深度缓冲**（`depthLoadOp: load`，`depthCompare: greater`），因此能与点云正确遮挡。

**渐进加载器**（`modules/progressive_loader/`）：拖入 LAS/LAZ 文件后：
- 阶段 0：只读每个文件头的 227 字节拿到包围盒，先画出包围盒
- `LasDecoder_worker.js`：每批 100 万点解析并推给主线程；LAZ 用 laz-perf WASM
- `ProgressivePointCloud.update` 按视线夹角分三档优先级

### 2.2 Spark 2.2：3DGS 的 LoD / 分页 / 排序

**与 Three.js 的集成方式**（`src/SparkRenderer.ts`）：
- `SparkRenderer extends THREE.Mesh`，必须显式 `scene.add(spark)`。几何是 `SplatGeometry`：一个四边形的 `InstancedBufferGeometry`，**整个场景的所有 splat 只用一次实例化 draw**。
- 所有工作都挂在 `onBeforeRender` 里：
  - `preUpdate` 为真时同步调用 `updateInternal`；XR 模式下改为 `setTimeout` 异步。
  - 渲染尺寸取自当前 RT；Vision Pro 的 1×1 特例单独处理。
  - 设置 `accumToCamera` 等 uniform。
- `SplatAccumulator` 做 **GPGPU**（WebGL2 下没有 compute）：
  - 每个 `SplatGenerator`（`SplatMesh` 等）由 dyno 着色器图生成 GLSL。
  - 用全屏四边形渲染到 `RGBA32UI` 的 2D 数组纹理（2048×2048×层），通过 MRT 同时输出 packed splat 与排序度量（`outputSplatDepth`，支持径向或 Z 深度）。
  - 按 scissor 分行分层写入。
- **排序链路**（`driveSort`，L1041）：
  1. `readbackDepth` 用 `readRenderTargetPixelsAsync`（PBO 异步回读）
  2. worker `sortSplats32`（`rust/spark-rs/src/sort.rs::sort32_internal`：两趟 16 位基数，65 536 桶，降序即远在前，`≥ Inf` 的键视为剔除）
  3. 结果上传为 `RGBA32UI` 的 4096×rows 排序纹理
  4. 顶点着色器用 `gl_InstanceID` 通过 `texelFetch` 取 splat 索引
- **双缓冲累加器**：`display` 与 `current` 按 `mappingVersion` 切换。splat 的映射（哪个 mesh 占哪段）不变时，**先显示新数据、沿用旧排序**，排序晚 1 帧以上也不会闪烁。
- **按需渲染**：`onDirty` 回调在排序完成、LoD 更新、分块到达时触发。R3F 用 `frameloop="demand"` 加 `invalidate()`（`docs/docs/on-demand-rendering.md`）。

**LoD**（`driveLod` L1180 → worker `traverseLodTrees` → `rust/spark-rs/src/lod_tree.rs`）：
- 预算 `maxSplats = lodSplatCount × lodSplatScale`。`defaultSplatTarget()`（L1168）：Quest 500k、Vision Pro 750k、Android 1M、iOS 1.5M、桌面 2.5M。
- 像素阈值 `pixelScaleLimit = 2·tan(fov_y/2)/H × lodRenderScale`（L1196），意思是"单位距离上一个像素对应的世界尺寸"。
- 重新遍历的触发条件：相机位移超过 1 个单位，或四元数点积变化超过 1%（similarity < 0.999），或预算/阈值变化，或 mesh 集合变化。
- `traverse_lod_trees`（`lod_tree.rs:412`）：
  1. 最大堆，键为 `pixel_scale`。
  2. 取堆顶：若 `≤ limit` 就结束；若是叶子就输出。
  3. 若展开后 `num − 1 + child_count > max_splats` 就**结束整个遍历**（L499）。
  4. 子节点所在分块未驻留时，输出父节点，即"缺页时用父节点顶上"。
  5. 结束时把堆里剩下的 frontier 全部输出。
  6. 同时返回 `touched` 分块列表（按访问顺序），用作抓取优先级。
- `compute_pixel_scale`（L601）：`size/distance × lodScale × foveate`。foveate 是三段插值：`coneFov0` 以内为 1，到 `coneFov` 降到 `coneFoveate`，视线后方为 `behindFoveate`。默认值 90°/120°/0.4/0.2。
- `dynamic_traverse_lod_trees`（L632，2.2 版实验性）：从 `100×limit` 开始，逐轮按 `0.99·scale·√(count/max)` 下调阈值（每轮最多减半），直到接近预算。它的好处是每个实例能分到更均匀的阈值。
- LodSplat 节点只有 16 B：center f16×3、size f16、child_count u16、child_start u32。整棵树放在 worker 的 Rust 状态里。

**虚拟分页**（`src/SplatPager.ts`）：
- 页大小 `PAGE_SPLATS = 256×256 = 65 536`（L42）。池大小 `maxPagedSplats`：桌面 256 页，即 1678 万 splat；iOS 96 页；其他移动端 128 页。
- `fetchPriority` 的顺序：先放各 mesh 的根块（按到相机距离排序），再放遍历"触碰"到的块。
- `driveFetchers`（L1070）：
  - 最多 `numLodFetchers=3` 个并发，共享 4 个 worker 的池
  - 已驻留的块刷新 LRU，按需求的逆序打时间戳
  - 不在需求集里的页进入 `freeablePages`
  - 失败时退避 250–750 ms
- `processFetched` 拿到空闲页后插入页表，生成 `lodTreeUpdates` 通知 worker 更新 `chunk_to_page`，并把纹理上传排队；上传在下一次遍历时的 `processUploads` 里执行。
- 页被淘汰时，worker 的页表写回 `0xFFFFFFFF`。遍历遇到缺页，就用父节点顶上。

**数据格式。**
- `PackedSplats`：16 B/splat。
  - word0：RGBA8
  - word1–2：中心 f16×3
  - 四元数：oct 编码 88 + 8 位角度
  - 尺度：log 编码 u8×3，其中 0 表示"尺度为零"，用于 2DGS
  - 见 `shaders/splatDefines.glsl::packSplatEncoding`
- `ExtSplats`：32 B/splat，中心为 f32。文档提醒 f16 中心在 1000 单位外步长只有 1。
- **RAD**（`rust/spark-lib/src/rad.rs`）：
  - 文件头：`'RAD0'` + u32 元数据长度 + JSON（`count`、`chunkSize=65536`、`chunks[{offset,bytes,filename?}]`、`splatEncoding`、`lodTree`），8 字节对齐。
  - 每个块：`'RADC'` + u32 + JSON 元数据 + u64 payload 长度 + **列式属性**。每个属性独立选编码（center F32/F16、rgb R8/R8Delta、scales Ln0R8/LnF16、orientation Oct88R8、SH S8/S8Delta），可选 deflate 压缩。
  - LoD 树的 child_count/child_start 也作为列存进块。
  - `--rad-chunked` 输出 `*-lod.rad` 头文件加 `*-lod-{i}.radc` 分块。
- **LoD 构建**：
  - `tiny_lod.rs`：自底向上网格合并，默认 `lod_base=1.5`。按 feature size 排序，第 L 级步长 `step = base^L`，同一格内的 splat 做矩匹配合并，然后逐级上升。
  - `bhatt_lod.rs`：离线高质量，基于 Bhattacharyya 距离。
  - `chunk_tree.rs::chunk_tree_size`：把树重排成"粗层优先、每 64K 一块、块内按 Hilbert 八分顺序"，保证**第 0 块就是最粗的全局概览**。
- **合并公式**（`gsplat.rs::new_merged`）：
  - 权重 `w_i ∝ area_i·opacity_i`
  - `center = Σw·c`
  - `Σ = Σw(δδᵀ + Σ_i + (step/2)²I)`
  - 特征分解得到尺度和旋转
  - `opacity = Σ(area·op)/area(merged)`，可大于 1，渲染时 1..5 映射到展宽的 std（`splatVertex.glsl` 的 `adjustedStdDev`）
  - `feature_size = 2·max_scale·lod_opacity`

**着色器要点**（`shaders/splatVertex.glsl`）：
- 视锥外判定：`clipXY=1.4` 倍宽限
- `maxStdDev=√8`
- 抗锯齿 blur：`a+=0.3`，按 `√(det_orig/det)` 衰减 alpha
- 支持 2DGS（某一轴尺度为 0）、景深（`focalDistance`、`apertureAngle`）、`minPixelRadius/maxPixelRadius`、对数深度

**可编程 splat。**
- dyno 着色器图（`src/dyno/*`）。
- `SplatEdit`：SDF 编辑，可做区域高亮或裁剪。
- `SplatSkinning`：双四元数蒙皮。
- `generators/snow.ts::snowBox`：**无状态粒子**，粒子位置由 index 哈希加时间直接算出，见 §3.9。

### 2.3 SuperSplat Viewer：PlayCanvas 上的流式 3DGS 与 WebGPU compute

**加载与 LOD**（`src/index.ts`、`src/viewer.ts`）：
- 支持的内容：`.ply` / `.compressed.ply` / `.sog` / `meta.json` / `lod-meta.json`。`lod-meta.json` 由引擎自己拉取（`index.ts:37` 注释），分块和 LOD 八叉树都由 PlayCanvas 引擎的 gsplat-unified 实现。
- 场景参数（`viewer.ts:483–491`）：

  | 参数 | 取值 |
  |---|---|
  | `lodUpdateAngle` | 90 |
  | `lodBehindPenalty` | 5（相机后方降 5 倍精度） |
  | `lodMode` | `DISTANCE` |
  | `minContribution` | 1（alpha 质量，单位像素） |
  | `alphaClip` | 1/255 |
  | `radialSorting` | true（旋转时侧边不出现未排序的 splat） |

- 预算：桌面 2M/4M（低/高），移动 1M/2M（`budgets`，L79）。
- 渲染器：WebGPU 用 `GSPLAT_RENDERER_RASTER_GPU_SORT`，WebGL 用 `RASTER_CPU_SORT`。
- **渐进揭示**（L498–520）：
  1. 资源一就绪就把 `lodRangeMin = lodRangeMax = 最粗级`，先整体显示
  2. 首帧就绪后放开到 0..1000，再应用预算
  3. 碰撞数据不在揭示的关键路径上（注释里的实测：碰撞 5.65 MB，比最粗 LOD 的 4.28 MB 还大）
  4. 进度条按 `frame:ready` 事件里的 loading 计数推进

**实验性随机透明渲染器**（`src/render/stochastic-splat-renderer.ts` 与 `render/shaders/*.ts`，全部 WGSL，只在 WebGPU 下启用）：
- `projector.ts`：
  - 一个 256 线程工作组处理一个 chunk（256 个 splat）。
  - 剔除项依次为：alpha、近平面、尺寸（`minPixelSize`）、贡献度（`opacity·2π·√det < minContribution`）、屏幕外（按包围盒）。
  - **上一帧遮挡剔除**：`occL1` 是 8 px 网格的最大深度，`occL2` 是 32 px 网格。按 splat 足迹选网格层级，先查中心块，再查 (2g+1)² 邻域，前表面 `prevDepth − 2.83σ_z` 比网格最远深度还远就剔除。足迹超过 64 px 的不测。
  - 存活的 splat 写入稠密缓存，每个 7 个 u32：ndc snorm16×2、depth f32、axis1 f16×2、len2 加 alpha 加桶号、RGB 10/10/10 共享指数、popless 深度梯度、id。
  - 工作组内先用 `atomicAdd(&wgCount)` 算组内名次，再由 0 号线程做**一次**全局 `atomicAdd` 拿基址。
- `order.ts`：256 个对数深度桶（`log(depth)` 在 near..far 上归一化）。单工作组做前缀和，scatter 时每组本地计数、每桶一次全局原子。结果是**前向后**顺序，让不透明深度测试的 early-z 起效。
- `args.ts`：单线程 compute 从计数器写 `DrawIndexedIndirectArgs` 和下一趟的 dispatch 参数，**整个过程不回读 CPU**。
- `raster.ts`：
  - 每个实例是 128 个四边形的网格，indirect 实例数 = ⌈存活数/128⌉，尾部多出的丢弃。
  - 片元以概率 alpha 保留：哈希取随机数，2×2 像素分层抽样。保留的片元写**不透明**颜色加硬件深度，因此**不需要排序**。
  - popless 深度：每个角点深度取高斯峰值所在平面。
- `taa.ts`：时间累积，静止时最多 256 样本，运动时 16。
- `reduce.ts`：先把深度纹理归约到 8×8 块的最大深度，再归约 4×4 块得到 L2 层。深度按 f32 位模式做 `atomicMax`（正浮点的位模式与数值同序）。
- `resident-set.ts`：通过窄化类型访问引擎内部（`@ignore`）的 world state。引擎升级时只会在这一个文件出错，是隔离引擎私有 API 的好做法。

**碰撞**（`src/collision/`）：
- `collision.ts`：统一接口 `queryRay / querySphere / queryCapsule / querySurfaceNormal / isFreeAt / voxelResolution`。`resolveIterative` 最多迭代 4 次，把推出向量投影到历史约束法线上累加。
- `voxel-collision.ts`：稀疏体素八叉树（格式来自 `splat-transform --voxel-carve` 的 `.voxel.json`）。
  - `nodes: Uint32Array` 按 BFS 排列，节点 = `childMask<<24 | baseOffset(24 位)`。
  - `childMask=0` 表示混合叶子，低 24 位是 `leafData` 下标，指向 4×4×4 = 64 位掩码（两个 u32）。
  - `0xFF000000` 表示实心叶子。
  - 子节点下标 = `baseOffset + popcount(childMask & ((1<<octant)−1))`。
  - `isVoxelSolid`（L839）从根逐级下降。`queryRay`（L378）先做 AABB slab 求交，再体素 DDA。
- `mesh-collision.ts`：GLB 网格版本，接口相同。
- `find-spawn.ts`：在空闲体素里找出生点。

**其他**：`picker.ts`（异步深度拾取，按不透明度估计表面）、`cameras/*`（orbit/fly/walk/anim 控制器和出生状态）、`input/*`（键鼠、触控、手柄、触控板）、`xr.ts`。UI 是原生 DOM 加 SCSS，与 shadcn 无关。

### 2.4 splat（antimatter15）

- `main.js::createWorker`：worker 源码用 `Blob` 内联。
  - `generateTexture`：每个 splat 占 2 个 `RGBA32UI` 纹素，内容为 xyz f32×3、协方差 half×6（预乘 4 倍）、RGBA8。纹理宽 2048。
  - `runSort`：`depth = (vp[2]x+vp[6]y+vp[10]z)·4096` 取整，量化到 16 位做**单趟计数排序**（65 536 桶）。视线方向与上次的点积变化小于 0.01 时跳过。`throttledSort` 只排最新视角。
- 主线程用 `ReadableStream` 流式读取，每读一段就推新的 `vertexCount` 给 worker。因为 `convert.py` 已按重要性 `exp(Σscale)·sigmoid(opacity)` 降序写出 `.splat`，**任意前缀都是最重要的那部分**，所以这就是渐进加载。
- 顶点着色器：2D 协方差特征分解，`majorAxis = min(√(2λ1),1024)·v1`，片元 `exp(−r²)`，范围在 r² > 4 处截断。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 预算约束的贪心 LOD 遍历（Spark `traverse_lod_trees` 移植到 additive 八叉树）

**定义**（与 r09 的八叉树一致：G=64，`spacing_L = cube/(G·2^L)`，节点内点序已打散）：

```
f_px          = (H/2) / tan(fov_y/2)                         // 焦距（像素）
limit         = τ_px / f_px                                  // Spark: pixelScaleLimit = 2tan(fov/2)/H · lodRenderScale
d(n)          = max(|c_n − eye| − 0.5·r_n, ε)                // r_n = 半对角线；减半径让相机附近节点优先
fov(n)        = foveate(angle(c_n − eye, fwd); cone0, cone1, coneFoveate, behindFoveate)
score(n)      = spacing_L(n) / d(n) · fov(n) · lodScale(layer)
refine(n) 当且仅当 score(n) > limit
```

**伪代码**（TypeScript，放在 LOD Worker 或主线程，10 Hz）：

```ts
function selectNodes(root, cam, budget, tauPx, opts): Selection {
  const limit = tauPx / cam.focalPx;
  const heap = new MaxHeap<[score:number, node:Node]>();
  const out: Array<[Node, number /*fraction*/]> = [[root, 1]];
  let pts = root.count, achieved = Infinity;
  heap.push([score(root), root]);
  while (heap.size) {
    const [s, n] = heap.peek();
    achieved = Math.min(achieved, s);
    if (s <= limit) break;                                        // 细节已够：提前停（Spark 语义）
    const kids = n.children.filter(c => c && cam.frustum.intersectsSphere(c.sphere)); // 点云加视锥剔除
    const add = kids.reduce((a, c) => a + c.count, 0);
    if (!kids.every(c => c.resident)) { heap.pop(); requestLoad(kids, s); continue; }  // 缺页：父节点顶上，按 s 排队加载
    if (pts + add > budget) {                                     // 预算不够：按比例取子节点前缀（Potree 打散点序）
      const phi = (budget - pts) / add;
      if (phi >= opts.minFraction /*0.25*/) { for (const c of kids) out.push([c, phi]); pts = budget; }
      break;                                                      // Spark：预算触顶即结束（不跳过去找更小的）
    }
    heap.pop();
    for (const c of kids) { out.push([c, 1]); pts += c.count; heap.push([score(c), c]); }
  }
  return { nodes: out, points: pts, achievedPx: achieved * cam.focalPx };  // achievedPx → 自适应点径
}
```

**参数建议：**

| 参数 | 桌面 GPU | 集显/移动 | 软件 GL（CI） | 说明 |
|---|---|---|---|---|
| `budget` | 2–4M | 0.8–1.5M | 0.15–0.3M | 由 FPS 控制器（r11 §3.4 的 AIMD）调节 |
| `τ_px` | 1.5 | 2 | 3 | 点间距投影超过 τ 才细化；和点径 `≈ τ` 一起取 |
| `minFraction` | 0.25 | 0.25 | 0.25 | 与 r11 的 `PARTIAL_MIN_RATIO` 一致 |
| foveation | 沙盘默认**关**（cone0=cone1=180°）；FPV/跟随模式开：cone0=110°、cone1=150°、coneFoveate=0.5 | 同左 | 同左 | 实测 Spark 默认 90/120 在点云上会增加边缘空洞 |
| `behindFoveate` | 0.2（只在关闭视锥剔除、需要"转头兜底"时用） | 0.2 | — | FPV 急转时不至于全黑 |
| 触发 | 相机位移 > 0.5·spacing_leaf，或 `dot(q, q_last) < 0.999`，或预算/阈值变化 | — | — | 来自 Spark `driveLod` 的 similarity 门控 |

**自适应点径**：
- 遍历停在 `achievedPx`，意思是 frontier 上最粗节点的点间距投影成了这么多像素。
- 点径取 `clamp(k·spacing_L·f_px/d, 1, maxPx)`，其中 `k≈1.2`，`maxPx` 取 6（桌面）或 8（软件档）。可以逐点在顶点着色器里算（Potree 的 adaptive 模式），也可以逐节点传 uniform。
- 与 r11 的"quad 比 1 px 点贵 16–23 倍（SwiftShader）"结合：软件档优先用 1 px 点加屏幕空间膨胀（§3.7），不要用 quad。

### 3.2 实测：三种策略在 UrbanScene3D 飞行回放上的对比

**设置：**
- 场景：New York，500 万点，Z-up，立方体边长 3166.7 m。用 r09 的 `build_fast` 建树，G=64、LEAF=20000，共 852 个节点。
- 相机：1280×720，FOV_y 60°。前 10 帧从高空下降做全景，之后 110 帧是距地 60–140 m 的 8 字形航线，前视下俯 25°。
- 指标：
  - "4 px 空洞率"：全量点云在 4×4 px 瓦片上覆盖、但所选点没有覆盖的比例
  - 帧间节点变化率：相邻帧节点集的 |AΔB|/|A∪B|
  - 新请求节点：该节点此前从未被选中过

**1M 预算：**

| 策略 | 平均点 | 最大点 | 超预算帧 | 平均节点 | 空洞率 均值/P90 | 屏内点 | 变化率 | 新请求/帧 |
|---|---|---|---|---|---|---|---|---|
| Potree-Next（minNode=150） | 1,485,660 | 2,950,858 | **73/120** | 209 | 0.01% / 0.01% | 1,074,917 | 0.100 | 6.21 |
| Potree-Next（minNode=200） | 1,337,344 | 2,447,739 | 72/120 | 174 | 0.03% / 0.02% | 937,538 | 0.106 | 6.01 |
| Potree 1.8（min=30） | 888,797 | 999,922 | 0 | 104 | 0.10% / 0.25% | 605,144 | 0.145 | 4.87 |
| Potree 1.8（min=150） | 469,495 | 753,213 | 0 | 49 | **2.68% / 4.10%** | 254,469 | 0.156 | 3.89 |
| Spark 式（τ=1.5，fov 70/120） | 877,425 | 1,000,000 | 0 | 102 | 0.40% / 1.14% | 634,644 | 0.133 | 4.99 |
| Spark 式（τ=2，fov 70/120） | 836,015 | 1,000,000 | 0 | 97 | 0.42% / 1.14% | 600,392 | 0.135 | 4.99 |
| **Spark 式（τ=2，无 fov）** | 853,932 | 1,000,000 | 0 | 98 | **0.11% / 0.30%** | 571,124 | 0.144 | 5.13 |
| Spark 式（τ=2，fov 110/150） | 853,932 | 1,000,000 | 0 | 98 | 0.18% / 0.34% | 581,263 | 0.144 | 5.18 |

**300k 预算（软件 GL 档）：**

| 策略 | 平均点 | 平均节点 | 空洞率 均值/P90 | 屏内点 | 变化率 | 新请求/帧 |
|---|---|---|---|---|---|---|
| Potree 1.8（min=30） | 292,198 | 29 | 12.34% / 22.0% | 134,262 | 0.154 | 1.89 |
| Potree 1.8（min=150） | 271,103 | 27 | 13.14% / 22.0% | 113,211 | 0.156 | 2.04 |
| Spark 式（τ=1.5，fov 70/120） | **300,000** | 29 | 16.09% / 34.7% | **184,669** | 0.179 | 2.55 |
| Spark 式（τ=2，无 fov） | **300,000** | 28 | 13.17% / 24.0% | 141,347 | 0.167 | 1.78 |
| Spark 式（τ=2，fov 110/150） | 300,000 | 28 | 13.50% / 30.9% | 173,039 | 0.169 | 2.24 |

**复核场景 San Francisco（500 万点，立方体 740.1 m，650 个节点，1M 预算，航线同上）：**

| 策略 | 平均点 | 最大点 | 超预算帧 | 空洞率 均值/P90 | 屏内点 | 变化率 | 新请求/帧 |
|---|---|---|---|---|---|---|---|
| Potree-Next（minNode=150） | 1,440,280 | 2,868,723 | **83/120** | 0.02% / 0.00% | 1,095,828 | 0.092 | 4.81 |
| Potree 1.8（min=30） | 893,880 | 999,884 | 0 | 0.05% / 0.15% | 645,090 | 0.123 | 3.67 |
| Potree 1.8（min=150） | 208,751 | 369,341 | 0 | **10.64% / 15.31%** | 96,208 | 0.112 | 1.53 |
| Spark 式（τ=1.5，fov 70/120） | 868,169 | 1,000,000 | 0 | 0.30% / 0.80% | 659,065 | 0.119 | 4.00 |
| **Spark 式（τ=2，无 fov）** | 853,118 | 1,000,000 | 0 | **0.06% / 0.17%** | 606,281 | 0.122 | 4.07 |

SF 场景更小更密，150 px 阈值的欠填更严重，只用了 21%，空洞 10.6%。其余结论与 New York 一致。

**解读：**
1. **Potree-Next 的遍历不受预算约束**，点数随视角在 0.9–2.95M 之间剧烈波动，帧时间不可控，不能照搬。
2. 同一个 150 px 阈值，1.8 版公式用半径，Next 版用直径乘以 2，两者口径不同。在无人机下视场景下，1.8 版取 150 会**提前截断**，预算只用到 47%（SF 为 21%）。viewer 的默认值 30 才合理。r11 草案里的 `MIN_NODE_PX=150` 应改为 30，或者直接换成 Spark 式的 spacing 阈值。
3. Spark 式的遍历有两个优点。一是**严格用满预算**：最后一层取前缀，300k 档正好 300,000 点，而 1.8 版在 27.1 万到 29.2 万之间浮动，有 2.5–10% 的浪费。二是细节足够时**提前停**：1M 档平均只用 85 万点。点数随预算线性变化，这对 FPS 反馈控制器（执行器必须是单调、近似线性的）很重要。
4. 注视点（fov）把点集中到画面中心，屏内点占比从 57% 升到 63%，但边缘空洞从 0.11% 升到 0.40%。沙盘俯视时**默认关**，FPV 时开"宽锥"（110/150）。
5. 300k 档所有策略的 4 px 空洞都在 12–16%，这是**点数本身不够**导致的。软件档必须配合点径放大（≥4 px），或者用 1 px 点加 dilate 后处理；不要指望靠 LOD 策略解决。

### 3.3 "统一页池 + 单次 draw"的点云 GPU 数据结构（V0.1 起可用）

动机：r11 实测 three.js 每个对象的 CPU 开销约 22–29 µs（WebGPURenderer），可见节点上限只能设 256。Potree-Next 每节点一次 draw，Spark 所有 splat 一次 draw。下面把 Spark 的页池、Potree-Next 的节点表和前缀和合起来：

```
PointPool（GPU 常驻）
  PAGE = 16384 点（Spark 页为 65536 splat；点节点更小，取 16K 降低尾页浪费）
  pages = ceil(1.5 × budgetMax / PAGE)          // 桌面 4M → 384 页
  record = 12 B/点（r09 的 ANET_Q16：unorm16×3 局部坐标 + oct16 法线 | RGBA8）
  WebGPU：storage buffer array<u32>，大小 = pages×PAGE×12 B（4M 档约 75 MB）
  WebGL2：RGBA32UI 2D 纹理，宽 4096，1 点 = 1 texel（第 4 个 word 放分类/强度/时间戳）
PageTable：每个驻留节点占若干页（不要求连续），nodePages[] 存页号
DrawTable（每次 LOD 更新写一次，≤ 1024 条，每条 32 B）
  { prefix: u32, drawCount: u32, firstPageRef: u32, level: u32, min: vec3f, size: f32 }
单次绘制：draw(totalDrawCount)（point-list），或实例化 quad（instanceCount = totalDrawCount）
```

顶点着色器（WGSL 示意，TSL 同构）：

```wgsl
@vertex fn vs(@builtin(vertex_index) vid: u32) -> VSOut {
  // 在 ≤1024 条 DrawTable 上二分，最多 10 步
  var lo = 0u; var hi = uniforms.numDraws;
  loop { if (hi - lo <= 1u) { break; } let mid = (lo + hi) / 2u;
         if (draws[mid].prefix <= vid) { lo = mid; } else { hi = mid; } }
  let d = draws[lo];
  let local = vid - d.prefix;                                 // local < drawCount，节点内点序已打散 → 前缀即均匀子采样
  let page  = nodePages[d.firstPageRef + (local >> 14u)];
  let slot  = page * 16384u + (local & 16383u);
  let w0 = pool[3u*slot]; let w1 = pool[3u*slot+1u]; let w2 = pool[3u*slot+2u];
  let q  = vec3f(f32(w0 & 0xffffu), f32(w0 >> 16u), f32(w1 & 0xffffu)) / 65535.0;
  let p  = d.min + q * d.size;                                 // 节点局部量化 → 世界（ENU，相对世界原点）
  ...  // 颜色 = unpack4x8unorm(w2)，法线 = oct 解码 (w1 >> 16u)
}
```

要点：
- 预算或密度变化只需重写 DrawTable（几 KB），零重编译、零大块上传。部分节点的 `drawCount = ⌊count·φ⌋`。
- 节点进出只是页的分配和回收，没有缓冲创建或销毁，WebGPU 不会产生 bind group 抖动。LRU 策略照搬 Spark `driveFetchers`：在"需要集"里的页按优先级逆序刷新时间戳，其余进入 `freeable`，分配新页时从这里取。
- 拾取：点 ID = `vid`（全局），画到 `r32uint` 附件（§3.6）。CPU 用 DrawTable 的前缀和二分即可反查节点和节点内下标，Potree-Next 的 `renderedObjects` 前缀扣减就是这个做法。
- WebGL2 回退：`gl_VertexID` 加 `texelFetch` 实测与属性取数同量级（SwiftShader、1 px：10 万点 244 ms 对 283 ms，25 万点 338 ms 对 295 ms，噪声内持平）。原生 WebGL2 下 256 次 draw 与 1 次 draw 耗时也相同，瓶颈是 three.js 的每对象开销，不是 GL 本身。所以两个后端**共用一个设计**是可行的。
- three.js 集成：WebGPURenderer 用 `StorageBufferAttribute`/`storage()` 加 TSL `vertexIndex`，建一个 `Points` 或 `Sprite` 实例物体。`forceWebGL` 时 TSL 的 storage 会降级成纹理或属性，需要实测，不行就在 WebGL2 路径直接用 `DataTexture(RGBA32UI)`。r11 负责给出最终 TSL 写法。

### 3.4 WebGPU compute 剔除与压实管线（V0.6，参考 supersplat projector）

适用于真 GPU，而且点预算超过约 2M 或城市遮挡严重（无人机低空穿楼）的情况：

```
Pass A  cull/compact  @workgroup_size(256)，一组处理一段连续 slot（DrawTable 二分同 §3.3）
  p = decode(pool[slot]); c = VP * p
  keep = inFrustum(c) && !(occlusion && frontDepth(p) > maxDepthGrid_prev[block(reproject(p))])
  rank = atomicAdd(&wgCount, 1u)（仅 keep）; barrier
  if (li == 0) wgBase = atomicAdd(&counter[0], wgCount); barrier
  if (keep) { visible[wgBase + rank] = slot; atomicAdd(&bucket[log2Depth(c.w)], 1u) }   // 可选：前向后桶
Pass B  args  @workgroup_size(1)：drawIndirectArgs = {vertexCount: counter[0], instanceCount: 1, ...}
Pass C  (可选) scan+scatter 256 桶 → 前向后顺序（early-z 更有效）
Pass D  drawIndirect(args)：顶点着色器读 visible[vid]
Pass E  reduce：本帧深度 → 8×8 最大深度（atomicMax(bitcast<u32>(depth))）→ 4×4 再归约得 32 px 层，供下一帧 A 使用
```

参数（取自 supersplat）：
- 工作组 256。
- 遮挡网格两层，8 px 和 32 px。点的足迹不超过 16 px 用 L1，不超过 64 px 用 L2，更大不测。
- `cull:auto`：连续若干帧剔除率低于 8% 时暂停遮挡测试，因为测试本身比光栅化还贵，比如从外部俯瞰整城时。
- 深度桶：`key = (log(d) − log(near))/(log(far) − log(near))·256`。

本机验证（`bench.html`，SwiftShader WebGPU）：
- 25 万点：存活 180,196，compute 214 ms，加绘制 766 ms，亮像素 130,703
- 100 万点：存活 720,608，compute 737 ms，加绘制 3169 ms
- 结果说明**管线在 headless 下能正确跑通**，可以作为 CI 的功能测试。

注意：
- 反向 Z 下"最远深度"是**最小值**，reduce 要改成 `atomicMin`，比较方向也要反过来。supersplat 用的是正向 Z。
- WGSL 没有 64 位原子，Schütz 的"`atomicMin(depth<<32|color)` 单趟 compute 光栅化"要拆成两趟：先 `atomicMin(depthBits)`，再对"深度相等"的点写颜色索引，或者把深度量化到 24 位、颜色压成 8 位索引打包进 u32。列为 V1.0 预研，不进 MVP。

### 3.5 反向 Z 无限远投影（Potree-Next，V0.1 直接用）

```
P_inf_ZO = [[f/a,0,0,0],[0,f,0,0],[0,0,-1,-1],[0,0,-n,0]]   (列主序)
remap    = z' = −z + w                    ⇒  ndc_z = n / d   (近 1，远 0)
depth32float, depthCompare = "greater-equal", depthClearValue = 0
线性化：d = n / ndc_z   （EDL、拾取、雾都用它）
```

城市尺度下，near=0.1 m、远处 5 km 的楼顶都要画，用 24 位或正向 Z 会有 z-fighting。three.js WebGPURenderer 支持 `reversedDepthBuffer`（以 r11 核实为准），WebGL2 路径需要 `EXT_clip_control`，没有就退回对数深度。

### 3.6 点 ID 附件拾取（Potree-Next）

- 管线第二个颜色目标是 `r32uint`，片元写 `node.counter + pointID`，即全局前缀加节点内下标。
- 拾取时读鼠标周围 3×3 窗口（`renderer.readPixels`，异步），取最大 ID，再用 `renderedObjects` 的前缀和反查到节点和点，用 `node.getPoint(i)` 解码出世界坐标。
- 本项目用途：点击点云设航点、测距、贴地放置无人机。WebGL2 路径同样可行（`R32UI` 颜色附件加 `readPixels(RED_INTEGER)`）。拾取每 100 ms 或 hover 停顿时做一次，不必每帧。

### 3.7 EDL / HQS / 屏幕空间膨胀参数（Potree-Next）

| 效果 | 公式 / 参数 | 软件档建议 |
|---|---|---|
| EDL | 8 邻域，`resp = Σ max(log2 d − log2 d_n, 0)/8`，`w = exp(−resp·300·strength)`，strength=0.2，`d = near/z` | r11 实测 EDL 在 SwiftShader 下贵 3.6 倍，软件档关闭 |
| HQS | ① 深度趟 `z·1.005` ② 加性累积（rgba16f，不写深度，`greater-equal` 测试）③ 归一化：5×5 窗口，`w=exp(−nl²·5.5)`，只收 `d ≤ closest·1.01` 的样本 | 仅桌面 GPU |
| 膨胀（dilate） | 窗口 (2w+1)²，候选深度 `d + (|Δ|²/w²)²·(near·w·d/W)`（抛物面惩罚），取最近；再对 `d ≤ 1.01·closest` 的邻域颜色按 `exp(−100·max(|dx|,|dy|))` 加权 | **软件档首选**：1 px 点 + w=1–2 的膨胀，代价与点数无关 |

### 3.8 splat 排序的三种实现与阈值

| 实现 | 位置 | 算法 | 触发阈值 | 本机实测 |
|---|---|---|---|---|
| CPU 16 位计数排序 | splat `runSort`；Spark `sort_internal`（f16 键） | 1 趟直方图 + 前缀 + 散射 | 视线方向点积变化 > 0.01（splat）；Spark 为 0.999 或位移 > 0.001 | JS：50 万点 46–96 ms，100 万点 119–125 ms，200 万点 369–466 ms（高负载下） |
| CPU 32 位两趟基数 | Spark `sort32_internal`（Rust/WASM） | 低/高 16 位各一趟，65 536 桶，降序 | 同上，另有 `minSortIntervalMs` | 未单测（需 Rust 构建）；2.2 版说明"排序快约 20%" |
| GPU 计数排序 | three.js `gpgpu/CountingSort.js`（4096 桶）；supersplat `order.ts`（256 桶） | 清零 / 直方图 / 前缀 / 散射 | three.js 为 0.9995 | WebGPU 下的 3DGS 首选；WebGL 后端自动转 CPU（`GaussianSplat.updateSort`） |

结论：
- 3DGS 图层在 WebGL2 回退和软件档上要**限 splat 数不超过约 15 万**，并且把排序放进 worker。
- 视角静止时不要排序。门控阈值：方向点积 0.9995，或位移超过 0.5 m。

### 3.9 无状态程序化粒子（Spark `snowBox`，EnvironmentLayer 回退路径）

```
h_i = hash4(i),  h'_i = hash4(i, 0x1ab5)
O(t) = O(t−Δt) + v_fall · dir · Δt            // CPU 累积的全局偏移（可以直接用风场均值 W(t) 代替 dir·v）
p_i(t) = min + (max − min) ⊙ fract(h_i.xyz + O(t))           // 盒内环绕
       + wanderScale · sin(t + wanderVar·h'_i.w + h'_i.xyz)  // 抖动
p_i.z = max(p_i.z, groundZ)                                   // 落地停留
size_i = minS + (maxS − minS)·sin(π·fract(100·h_i.w)),  aniso = (0.1,1,0.1) 拉长成雨丝
```

- 参数基线：雨 `density=10/m³, v=2(相对单位), aniso=(0.1,1,0.1)`；雪 `density=100, v=0.02, wander=0.04`。
- 用途：在**只有 WebGL2、没有 compute** 的档位，EnvironmentLayer 的雨、雪、沙尘可以完全放进顶点着色器，没有状态缓冲，也不需要 transform feedback。r11 实测 WebGL2 TF compute 1 万粒子约 270 ms/帧，这个方法可以绕开它。
- 盒子跟着相机走，粒子数量直接由 `count` 控制，可作为 FPS 控制器阶梯的第 5 步。
- WebGPU 档再用 compute 粒子，对风场 3D 纹理做平流。

### 3.10 稀疏体素八叉树碰撞（SuperSplat → Geometry World）

格式（BFS 排列，u32 节点）：

```
node = (childMask << 24) | baseOffset          // 内部节点
node = 0xFF000000                              // 实心叶子
node = (0 << 24) | leafIndex                   // 混合叶子：leafData[2*leafIndex..+1] 为 64 位掩码，bit = z*16 + y*4 + x（4×4×4）
child(octant) = baseOffset + popcount(childMask & ((1<<octant)-1)),  octant = (bz<<2)|(by<<1)|bx
元数据：gridBounds、voxelResolution、leafSize=4、treeDepth、nodeCount、leafDataCount
```

查询：
- `isVoxelSolid`：treeDepth 次下降加一次位测试。
- `queryRay`：先做 AABB slab 求交，再体素 DDA。
- `querySphere` / `queryCapsule`：找最深穿透，迭代投影推出，最多 4 次。
- `querySurfaceNormal`：9 个候选方向，在 5×5 块上打分。

本项目用法：
- **后端**（Python/numpy，V0.4）：从点云体素化生成这个格式，建议 `voxelResolution` 取 0.5 m（城市）或 0.25 m（园区）。用于 mock LiDAR 射线（DDA）、无人机球体碰撞检测、航点可行性检查。它比 Open3D 的 VoxelGrid 更省内存，数据还能直接发给前端。
- **前端**（TS，V0.4）：相机防穿楼（fly 模式推出）、航点贴地（向下射线）、"点击空中"时给出最近可飞点。
- 数据放在 World Package 的 `geometry/voxel/occupancy.voxel.{json,bin}`，前后端共用。

### 3.11 渐进揭示与流式优先级（三家做法合并）

1. 首帧只要**最粗层**：
   - supersplat：`lodRange` 先锁到最粗级
   - Spark：根块按距离优先抓取
   - r09：octree.bin 的 BFS 前缀，L0–L2 约 1.9–3.3 MB，一次 Range 请求取回
2. 最粗层到齐后放开 LOD。**碰撞、语义等非渲染数据不进关键路径**。
3. 抓取优先级：
   - 本帧遍历 frontier 上被"触碰"的节点，按 score 降序排
   - 相机后方的请求降权（supersplat `lodBehindPenalty=5`，Spark `behindFoveate=0.2`）
   - 并发 3–4 个，失败退避 250–750 ms
4. 进度条：已完成 /（已完成 + 在途），单调、上限 99%，揭示那一刻跳到 100%。

### 3.12 按需渲染（Spark `onDirty` / R3F `frameloop="demand"`）

- 相机、遥测、动画、LOD 结果到达、分块到达时才 `invalidate()`。
- 相机静止、没有无人机运动时 GPU 占用降到接近 0，对笔记本和多标签演示很重要。
- 仿真在跑时（WebSocket 10–50 Hz）照常逐帧渲染，只在"暂停 + 静止"时进入 demand 模式。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| # | 能力 | 来源 | 目标模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | 预算贪心 LOD 遍历（spacing/距离，提前停，前缀取整，可选注视点） | Spark `lod_tree.rs::traverse_lod_trees`、`compute_pixel_scale` | `apps/web/src/world/pointcloud/lod/select.ts` | V0.1 | port | 实测严格守预算、响应线性 |
| 2 | 遍历触发门控（位移/四元数 similarity） | Spark `driveLod` | 同上 | V0.1 | port | 静止不重算 |
| 3 | 统一页池 + DrawTable 单次 draw | Spark `SplatPager` + Potree-Next 节点表 | `.../pointcloud/gpu/PointPool.ts` | V0.1（WebGL2 纹理版）→ V0.2（WebGPU storage 版） | port | 消除每对象开销 |
| 4 | 页 LRU + 优先级抓取 + 退避 | Spark `driveFetchers`、`processFetched` | `.../pointcloud/stream/Fetcher.ts` | V0.1 | port | |
| 5 | 反向 Z 无限远 + depth32float | Potree-Next `Camera.updateProj` | `apps/web/src/render/camera.ts` | V0.1 | port | 城市尺度 z 精度 |
| 6 | r32uint 点 ID 拾取 | Potree-Next `init.js` 拾取段 | `.../interaction/pick.ts` | V0.2 | port | 航点、测距 |
| 7 | 自适应点径（achievedPx） | Spark 返回的 `pixelLimit` | `.../pointcloud/material.ts` | V0.1 | port | 疏密调节的第三级 |
| 8 | 屏幕空间膨胀（软件档）、EDL/HQS（桌面档） | Potree-Next `dilate.js`、`EDL.js`、`hqs_normalize.js` | `.../render/post/*.ts` | V0.2 | port | 分档画质 |
| 9 | timestamp-query 分段计时（池化结果缓冲） | Potree-Next `Timer.js`、`startPass` | `.../perf/gpuTimer.ts` | V0.2 | port | 给 FPS 控制器提供 GPU ms |
| 10 | compute 剔除 / 压实 / 深度桶 / indirect / HiZ | supersplat `render/shaders/*.ts` | `.../pointcloud/gpu/cullCompute.ts` | V0.6 | port | 大预算、强遮挡场景 |
| 11 | 无状态粒子（雨/雪/沙） | Spark `generators/snow.ts` | `apps/web/src/environment/particles/stateless.ts` | V0.3 | port | WebGL2 无 compute 回退 |
| 12 | 稀疏体素碰撞 | supersplat `collision/voxel-collision.ts` | `world/voxel/svo.py`（生成、查询）+ `apps/web/src/world/geometry/VoxelCollision.ts` | V0.4 | port | 前后端共用占据查询 |
| 13 | 渐进揭示（最粗层先行、进度估计） | supersplat `viewer.ts`、`index.ts` | `.../world/loader/reveal.ts` | V0.1 | port | 首屏快 |
| 14 | 按需渲染 | Spark `onDirty` 文档 | `apps/web/src/render/loop.ts` | V0.1 | reference | 静止时省电 |
| 15 | 3DGS 渲染（默认） | three.js r186 `GaussianSplat`、`SPZLoader` | `apps/web/src/world/visual/GaussianLayer.ts` | V0.8 | adopt | 与主栈一致（r03） |
| 16 | 3DGS 高保真预览（可选） | Spark `SparkRenderer`、`SplatMesh({paged:true})`、RAD | `apps/web/src/routes/splat-preview/`（独立 WebGLRenderer） | V0.8 | adopt | 成熟的分页 LoD |
| 17 | 3DGS LoD 合并（离线） | Spark `build-lod --quality --rad-chunked`、`tiny_lod.rs` | `world/pipeline/gaussian_lod.py`（port 合并公式）或直接调用 CLI | V0.8 | adopt（CLI）/ port（公式） | |
| 18 | Potree3 单文件、体素内节点、类 BC 颜色 | Potree-Next `loader_v3/*` | — | V1.0 评估 | reference | 压缩率高，但语义是 replacing，与 r09 的 additive 前缀方案不同 |
| 19 | 随机透明 + TAA | supersplat `stochastic-splat-renderer.ts` | — | V1.0 评估 | reference | 无排序 3DGS，只在 WebGPU |
| 20 | GPU 基数排序库 | Potree-Next `libs/webgpu-radix-sort`（npm `webgpu-radix-sort`） | — | — | skip | three.js 自带 CountingSort 已够用 |
| 21 | 整体嵌入 supersplat viewer 或 Potree-Next | — | — | — | skip | 引擎不同、UI 不是 shadcn、原型质量 |

### 4.2 Visual World 的 3DGS 图层接入 WorldLayer（未来方案）

**统一接口**（放在 `apps/web/src/world/layers/VisualLayer.ts`）：

```ts
export interface VisualLayer {
  readonly id: string;
  readonly kind: 'pointcloud' | 'mesh' | 'gaussian' | 'tiles3d';
  readonly frame: 'ENU';                       // 统一世界原点（World Package coordinate.json）
  readonly caps: { renderer: 'webgpu' | 'webgl2' | 'both'; needsSort: boolean; writesDepth: boolean };
  attach(ctx: RenderContext): Promise<void>;   // 加入 WorldLayer 的 Group
  detach(): void;
  /** 每次 LOD tick（≤10 Hz）调用；返回本层在给定预算下的选择结果与代价 */
  update(view: ViewState, budget: LayerBudget): LayerStats;
  estimateCost(): { primitives: number; gpuBytes: number; costUnits: number };
  raycast?(ray: Ray, opts?: { maxDist?: number }): Hit | null;
  setVisible(v: boolean): void; setOpacity(a: number): void;
  dispose(): void;
}
export interface LayerBudget { costUnits: number; tauPx: number; foveation?: FoveationParams }
```

**全局预算仲裁**（`RenderBudgetArbiter`，挂在 r11 的 FPS 控制器下面）：
- 代价单位：点 = 1。splat 按 `c_s ≈ 6–10` 折算，依据是一个 splat 要画 2 个三角形加排序，r11 实测 quad 与点的差距在真 GPU 上估计 2–4 倍、SwiftShader 16–23 倍。上线后在目标机器上标定。
- 分配方式：按可见层的"边际收益"贪心。每个层上报自己的 frontier 最高 score 队列，仲裁器把总预算发给 score 最高的层，也就是把 §3.1 的堆从层内扩展到层间，与 Spark 用一个堆遍历多个 `lodInstances` 同构。
- 默认策略：3DGS 开启时点云降为 geometry-only 显示（半透明或 EDL 轮廓），预算主要给 splat；关闭时预算全给点云。

**两种 GaussianLayer 实现：**

| 实现 | 渲染器 | 数据 | LoD | 适用 |
|---|---|---|---|---|
| `ThreeGaussianLayer`（默认） | WebGPURenderer（含 `forceWebGL`） | `visual/gaussian/manifest.json` + `spz/{L}/{tileKey}.spz`（r03 方案），瓦片局部 ENU，\|x\| ≤ 1024 m | 我们的 §3.1 遍历在**瓦片**上按 REPLACE 运行：父瓦片存合并 splat（Spark `tiny_lod` 公式离线合并），子瓦片全部驻留才替换；每瓦片一个 `GaussianSplat`（瓦片数封顶 64，最好合并成一个大 `GaussianSplat` 的分段） | 沙盘内与点云、无人机、环境同屏 |
| `SparkGaussianLayer`（可选） | 独立路由，`THREE.WebGLRenderer` | `visual/gaussian/rad/scene-lod.rad` + `scene-lod-{i}.radc`（`build-lod --quality --rad-chunked`） | Spark 内建（`lodSplatScale`、`coneFov*`、`pagedExtSplats:true`） | 高保真漫游、对外演示 |

**3DGS 瓦片 SSE 阈值**：
- Spark 的 `lodRenderScale` 默认 1 px，文档说 5 px 通常也看不出差别。瓦片的 geometric error 取"该级合并 splat 的 feature size 的 P90"。
- τ 取 2–4 px（桌面）或 5 px（软件、移动）。这比 r10 建议的 4–8 px 小一些，因为这里的误差用的是 splat 自身尺寸，不是节点包围盒。

**深度合成：**
- 点云、网格、无人机不透明，先画并写深度。
- `GaussianSplat` 只测深度不写深度，最后画。three.js 与 Spark 默认都是这个顺序，Potree-Next 的 3DGS 也读点云深度。
- 3DGS 开启时，EnvironmentLayer 的雾、雨必须在 3DGS 之后作为后处理叠加，否则半透明排序会乱。

---

## 5. 对比与推荐

| 维度 | Spark | SuperSplat Viewer | Potree-Next | splat |
|---|---|---|---|---|
| ★ / 2026 活跃度 | 3660 / 很活跃（2.2，2026-09） | 579 / 很活跃（2026-09-26） | 124 / 停滞（2025-10） | 3072 / 停滞 |
| 与主栈（Three.js WebGPURenderer）兼容 | ✗（只支持 WebGLRenderer） | ✗（PlayCanvas） | ✗（自研 WebGPU 渲染器） | ✗ |
| 可移植算法价值 | **最高**：LoD 遍历、分页、排序、合并、RAD | **高**：compute 剔除、碰撞、揭示 | **中高**：WebGPU 点云基础技巧 | 低 |
| 点云直接相关度 | 中（算法可迁移） | 中（管线可迁移） | **高**（本身就是点云） | 低 |
| 3DGS 成熟度 | **最高**（分页 LoD、2DGS、景深、编辑） | 高（流式 LOD、GPU 排序） | 实验 | 入门 |
| 工程质量 | 高（TS 类型、测试、CI、文档） | 高 | 原型（死代码、`window=0`、父节点包围盒权重） | 教学 |
| 构建难度 | npm 预构建，零 Rust；`build-lod` 需要 Rust | npm | 零构建 | 零构建 |

**推荐排序**：Spark（port 为主，预览页 adopt）> SuperSplat Viewer（port 管线与碰撞）> Potree-Next（port 技巧）> splat（reference）。

---

## 6. 风险与注意事项

1. **渲染器生态割裂**：
   - Spark 只支持 WebGLRenderer，three.js `GaussianSplat` 只支持 WebGPURenderer，Potree-Next 与 supersplat 各有自己的引擎。
   - 应对：主栈锁定 WebGPURenderer（含 `forceWebGL`），**所有移植都用 TSL 或原生 WGSL 重写**，不要试图在一个 canvas 里混用两个渲染器。多 canvas 叠加也不行，深度无法共享。
2. **WebGPU 点恒为 1 px**：
   - 大点只能用实例 quad（软件档慢 16–23 倍）或 1 px 点加膨胀。
   - Potree-Next 的 dilate 没有完成（`window=0`），要自己调参。
3. **没有 64 位原子**：Schütz 式 compute 光栅化要拆两趟或降精度。同时注意 WGSL 的 `atomicMax` 只对 u32/i32 有效，深度要 `bitcast`，而且只对非负浮点保序。
4. **反向 Z 与 HiZ 的方向**：supersplat 用正向 Z，最远 = max；我们用反向 Z，最远 = min。移植时比较方向和清屏值要一起改，否则遮挡剔除会把可见点全剔掉。
5. **headless WebGPU 依赖 flags**：
   - 默认拿不到适配器；加 swiftshader flags 后可用，但速度是软件级（100 万点 compute 加画点约 3 s）。
   - CI 只做功能断言（存活数、像素非空、预算守恒），不断言 FPS。
   - 真机 Linux Chrome 的 WebGPU 可用性要逐台实测。
6. **Spark 的 f16 中心**：`PackedSplats` 的中心是 float16，城市坐标下有条带。预览页要开 `pagedExtSplats:true` / `extSplats:true`，或者瓦片局部化，\|x\| ≤ 1024 m（r03）。
7. **Spark 点云模式**：要求 red/green/blue 属性，默认尺度 0.001。UrbanScene3D 没有颜色，要先 `ingest` 伪彩色；点的尺度按 r09 的 spacing 设为 `≈0.5·spacing_leaf`。
8. **Potree-Next 的正确性问题**：子节点权重用父包围盒、`pointBudget` 不生效、`clearLRU` 用"时间戳差 > 100 ms"这种与帧率耦合的启发式、Range 请求失败只靠重试。移植时要逐一修正，不能当参考实现直接比对结果。
9. **supersplat 依赖引擎私有 API**：`resident-set.ts` 访问 `@ignore` 内部类，PlayCanvas 升级会坏。我们只移植 WGSL 与算法，不依赖它。
10. **排序与 LOD 的延迟**：
    - Spark 的排序晚 ≥1 帧，LoD 遍历在 worker 里约数十毫秒。无人机 FPV 快速转向时会出现"未排序侧边"。
    - 应对：`sortRadial:true`（supersplat 也默认径向排序），FPV 时 `behindFoveate` 保底，排序门控用方向点积 0.9995。
11. **流式请求风暴**：
    - 实测飞行中每帧新请求节点 2–6 个（G=64，1M）。节点均约 20 KB–240 KB，10 Hz 调度大约 0.2–1.4 MB/s。
    - 必须有并发上限（3–4）、优先级和退避。PROXY 层级一次拉取不超过 1 MB（r09）。
12. **性能数字的可信度**：本文浏览器实测在机器负载 14–22 下完成，只能用于同批对比。上线前要在目标 GPU 上重测 `c_s`（splat 折算系数）、τ 和预算档位。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§12 / §34 "WebGPURenderer + WGSL"要补"渲染器兼容矩阵"与三档画质**：
   - 主栈 WebGPURenderer（`forceWebGL` 回退）；3DGS 默认 three.js `GaussianSplat`，Spark 只进独立预览路由。
   - 画质三档（gpu-high / webgl2 / software）的点预算分别为 2–4M、0.8–1.5M、0.15–0.3M，并写明每档开哪些后处理（EDL/HQS/膨胀）。
   - 注明"WebGPU 点恒为 1 px"与"GLSL ShaderMaterial 不能用于 WebGPURenderer"两条硬约束。
2. **§14 点云 Web 渲染架构**：把"远处 10K、近处 1M"改成可实现的规范：
   - **LOD 选择 = 全局点预算 + spacing 屏幕投影阈值 τ + 前缀取整**（§3.1 公式）。
   - 点云**不要**直接用 150 px 的节点尺寸阈值，实测会欠填到 47%。
   - 给出触发频率（运动时 10 Hz）、门控条件、并发抓取数、LRU 页池。
   - 加上"疏密三级联动"：预算、节点内前缀、自适应点径（外加膨胀）。
3. **§16 WorldLayer 结构**：
   - 在 `WorldLayer` 下定义统一的 `VisualLayer` 接口（§4.2）和 `RenderBudgetArbiter`，PointCloud、Mesh、Gaussian、Tiles3D 都实现它。
   - DebugLayer 增加"LOD 着色、节点包围盒、预算与实际点数、GPU ms"。
4. **§8 Visual World**：写明 3DGS 路线：
   - V0.3 交付 GaussianLayer 骨架（默认关闭）。
   - V0.8 用 three.js `GaussianSplat` + SPZ 瓦片 + 我们的 REPLACE 遍历（父瓦片为矩匹配合并 splat）。
   - V1.0 以 `KHR_gaussian_splatting` 3D Tiles 为标准出口（r10）。
   - Spark RAD 作高保真预览。
   - 深度合成顺序：不透明先画，3DGS 只测不写，最后是环境后处理。
5. **§8 Geometry World 与 §41 World Package**：
   - 增加 `geometry/voxel/occupancy.voxel.{json,bin}`，采用 SuperSplat 的稀疏体素八叉树格式（§3.10）。
   - 这份数据同时服务后端（碰撞、mock LiDAR 射线、航点可行性）和前端（相机防穿模、航点贴地）。
   - 原文 Geometry 列了 Voxel、SDF、Collision Mesh，但没有**统一的查询接口**。建议补上 `queryRay / querySphere / isFree / nearestFree`。
6. **§21–§24 环境视觉**：
   - 补一条**无 compute 的回退路径**：无状态程序化粒子（§3.9），全局偏移由风场均值积分得到。
   - WebGPU 档再用 compute 粒子加 3D 风场纹理平流。
   - 粒子数量纳入 FPS 控制器阶梯。
7. **§28 / §36 前端时序**：
   - 区分渲染帧与 LOD tick（10 Hz）、排序 tick（门控）、拾取 tick（100 ms 或 hover）。
   - 仿真暂停且相机静止时进入按需渲染（§3.12）。
8. **§43 MVP**：
   - 建议把"统一页池 + 单次 draw"和"反向 Z"列为 V0.1 的必做项。它们决定后面能不能在 256 个以上节点、城市尺度下保持流畅，后补的改造成本高。
   - compute 剔除、HiZ 放到 V0.6。
9. **测试与验收（新增）**：
   - CI 用 headless Chromium 同时跑 WebGL2 和 WebGPU（swiftshader flags）。
   - 断言三项：预算守恒（选中点数 ≤ budget，且 ≥ 0.95·budget，除非已达到 τ）；飞行回放的节点变化率 < 0.2；4 px 空洞率不超过基线 +20%。
   - 回放脚本复用本文 `r13_lod_sim.py` 的航线定义。
10. **原文小问题**：
    - §11 表格把 Potree 定为"点云能力参考/集成"，建议拆成 **Potree 1.8（算法参考）/ Potree-Next（WebGPU 技巧参考，原型停更）/ potree-core 与 three-loader（WebGLRenderer 生态，与主栈冲突）**。
    - §12 列的 "Point Rendering" 应注明 WebGPU 无点尺寸。
    - §15 "兼容 3D Tiles"应明确"运行时用 Potree2 或 3D Tiles 1.1 的 ADD 子集，导出用 3D Tiles"（r09/r10）。
