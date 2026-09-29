# r12 研究笔记：Potree / potree-core / three-loader —— Web 点云 LOD 流式核心与 PointCloudEngine 设计

> 研究单元：r12 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §9–16（Web 3D 选型、WebGPU 定位、点云渲染架构、数据格式、场景结构）、§34（前端栈）、§37（刷新率）、§38–40（UI 与交互）、§43–44（MVP / V0.1）
>
> 仓库快照（shallow clone，只读，路径均相对各仓库根）：
> - `refs/web3d/potree` @ `5636cd4`（2026-01-08，★5622，BSD-2，v1.8.0，自带 three r124）
> - `refs/web3d/potree-core` @ `b901ca9`（2026-09-14，★255，MIT，npm `potree-core@2.0.15`）
> - `refs/web3d/three-loader` @ `64e23ce`（2026-05-28，★285，MIT，`@pnext/three-loader@1.0.0`，peer `three ~0.160`）
>
> 对照阅读：`refs/web3d/three.js`（r186，2026-09-28）、`refs/web3d/Potree-Next`（2025-10-07，WebGPU 版 Potree）、`refs/world/PotreeConverter`。
>
> 上游笔记：
> - **r09**：Potree 2.0 二进制格式、`ANET_Q16` 节点编码、numpy 切片器 `worldpkg tile`、前端接口约定（§3.4 把“自适应点大小的细节”交给本单元）。
> - **r06**：UrbanScene3D 坐标系不统一。
>
> 本机实测环境：8 核 Xeon E5-2603 v4 1.7 GHz，无 GPU。浏览器为 playwright 的 `chromium_headless_shell-1234`，渲染走 SwiftShader。脚本和原始结果在 `/data/projs/anet-drone/.cache/research/r12-bench/`（`bench*.html`、`inst.html`、`wgpu*.html`、`edl.html`、`RESULTS.txt`）。测量时机器负载 load avg 12–18（有其他任务并行），**绝对值偏悲观，但各方案之间的相对关系可信**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **tentone/potree-core**（★255，2026-09-14，TS，MIT） | 从 Potree 抽出的库化内核，可以直接 `scene.add()` 到普通 Three.js 场景。2025–26 年新增了 `EDLPass` / `PotreeRenderer`、include/exclude 裁剪体、`RequestManager` | **port**：LOD 遍历、LRU、hierarchy/proxy 解析、DEFAULT/BROTLI worker、EDLPass、GPU picker，改写进我们的 `PointCloudEngine`<br>**adopt（仅 dev）**：做交叉验证页，渲染同一份 Potree 2.0 DEFAULT 数据对比正确性 | V0.1 | ★★★★★ |
| **pnext/three-loader**（★285，2026-05-28，TS，MIT，Pix4D） | TypeScript 版 Potree loader，类型定义最干净：`IPointCloudGeometryNode.failed`、`load(): Promise`、`memoryScale`、`finally` 归还 worker；独有 GLTF 编码和 **LOD 化 3DGS splats**（`SplatsMesh` + wasm 排序） | **reference / port**：公共 API 的 TS 类型形状、错误与重试语义、带空闲自动终止的有界 worker 池（`utils/worker-pool.ts`）<br>**reference（V1.0）**：splats 的 LOD 渲染 | V0.1（类型）/ V1.0（3DGS） | ★★★★☆ |
| **potree/potree**（★5622，2026-01-08，JS，BSD-2） | 完整的 Potree 1.8 viewer，所有算法的原始出处：密度修正版 adaptive size、EDL、HQ weighted splats、绕开 three 的自研 GL 渲染器、EPT/COPC | **reference**：密度 lodOffset、`getLOD`/`getPointSize` 着色器、`PotreeRenderer.renderNodes` 的“少走 three 抽象”思路、HQSplat 三遍渲染<br>**skip**：整站 viewer（jQuery / three r124 / 全局变量） | V0.1（着色器）/ V0.3（HQ） | ★★★★☆ |
| （对照）m-schuetz/Potree-Next | WebGPU 版 Potree，屏幕中心加权优先级、按层级排序的加载队列 | **reference**：优先级公式（其余归 Potree-Next 研究单元） | V0.3+ | — |

**关键结论（实现者先读这几条）**

1. **三个库的 LOD 核心是同一套 Potree 1.x 算法**，差别只在默认参数和若干缺陷上（§2.4、§2.5）。算法要点：
   - 以节点包围球的**屏幕投影半径**为权重，用最大堆做 best-first 遍历。
   - 在物体空间做视锥裁剪。
   - 投影半径小于 `minNodePixelSize` 的子节点不入队。
   - 累计点数超过 `pointBudget` 时直接 `break`。
   - 每帧上 GPU 的节点数有上限（`MAX_LOADS_TO_GPU`），并发加载数有上限（`maxNumNodesLoading`）。
   - LRU 常驻上限为 `2 × budget`，超出后淘汰最久未用的整棵子树。
2. **三个库都不能直接用在 `WebGPURenderer` 上**：
   - 它们用的是 `RawShaderMaterial`（GLSL3）、`onBeforeRender` 里的 `uniformsNeedUpdate` hack、`WebGLRenderTarget` 和 `renderer.getContext()`。
   - peer three 版本分别是 r124、`>0.125`（开发时用 0.154）、`~0.160`，而仓库里的 three.js 已到 r186。
   - 因此，**我们的 `PointCloudEngine` 采用 port 方式**：与渲染无关的 LOD Core 直接移植并修复缺陷，渲染后端做 GLSL 和 TSL 两套。
3. **WebGPU 点云有一个硬限制（源码确认 + 本机实测）**：
   - WebGPU 的 point 图元只能是 1 px（`src/materials/nodes/PointsNodeMaterial.js` 的注释写明 “WebGPU only supports point primitives with a pixel size of 1”）。
   - `WebGPURenderer` 的 WebGL2 回退后端把 `gl_PointSize = 1.0` 写死了（`src/renderers/webgl-fallback/nodes/GLSLNodeBuilder.js:1702`）。
   - three 官方做 >1 px 点的方式是 `Sprite` + instancing。本机 SwiftShader 上，instanced quad 比 GL_POINTS **慢 30–80 倍**（WebGL2 和 WebGPU 都如此）；**非实例化的 vertex-pulling quad**（每点 6 个顶点，用 `vertexIndex/6` 取点）只慢 2–4 倍。
   - 结论：
     - 回退/无头档必须用**经典 `WebGLRenderer` + GLSL `gl_PointSize`**，不要用 `WebGPURenderer` 的 WebGL2 后端来画点云。
     - 硬件 WebGPU 档用非实例化 vertex pulling，或者“1 px 点 + 屏幕空间膨胀”。
4. **headless Chromium 可以拿到 WebGPU adapter**，结果与 `docs/02-refs` 里“WebGPU 大概率不可用”的判断不同：
   - 需要安全上下文（`file://` / `https` / `localhost`），并加启动参数 `--enable-unsafe-webgpu --enable-features=Vulkan --use-webgpu-adapter=swiftshader`。
   - 得到的 adapter 是 `{vendor: google, architecture: swiftshader}`。
   - 所以 CI 可以测 WebGPU 路径的**正确性**，但**性能**要按软件渲染来看。
5. **SwiftShader 的吞吐只有约 0.3–1 M 点/秒**（每点 1–3 µs，点大小 1–4 px 影响很小，瓶颈在顶点和图元装配）。其他开销：
   - 每个 draw call 约 40–60 µs。
   - 720p 的 8-tap EDL 全屏 pass 约 **107 ms**（360p 约 36 ms）。
   - 因此 r09 §3.4 给 SwiftShader 的默认预算 250k 偏高，那样每帧要 0.25–0.8 s。**无头档应从 40k 起步，下限 10k，目标 20–30 FPS，关闭 EDL**，用法线做光照（UrbanScene3D 自带法线）。
6. **“自动调节疏密”应当做成一个闭环**，由四部分组成（§4.4–4.7）：
   1. **选择**：用投影点间距 `s_px = spacing_L · projFactor` 对阈值 τ 做细分判据，τ 与 `minNodePixelSize` 可以互换，§3.2 给了公式。
   2. **预算**：点预算由 FPS 控制器决定，控制律为“前馈（在线回归 t≈a+bN）+ 对数域 AIMD 反馈 + 交互降档 + 画质阶梯”。
   3. **填充**：节点内点序随机（r09 的 `ANET_Q16`），可以只画节点的一个前缀（fractional draw），实现连续的“密度淡入”。
   4. **点大小**：自适应点大小分两档。Lite 档每节点 1 个 8 位子节点可见掩码，着色器开销 O(1)；Full 档移植 Potree 的 `visibleNodes` 纹理逐点查询最深可见层级。
7. **移植时必须修掉的缺陷**（§2.5）：
   - worker 池无上限。
   - 解码失败没有 `failed` 标记，会导致无限重试。
   - potree-core 的 `nodeLoadPromises` 实际上是空承诺。
   - `onBeforeRender` 里每次 `visibleNodes.indexOf()`，复杂度 O(n²)。
   - `visibleNodes` 纹理固定 2048 宽，超过 2048 个可见节点就越界。
   - potree-core 对 Potree 2.0 数据强制只用 rgba 着色（`new_format`）。
   - three-loader 构造函数的运算符优先级 bug 会忽略传入的自定义材质。
   - potree-core 用 device px、Potree 原版用 CSS px，`minNodePixelSize` 的含义在高 DPR 屏上差了 DPR 倍。
8. **React 集成**：两个库都没有 React 绑定。推荐做法：
   - `PointCloudEngine` 写成纯 TS 类，热路径里不碰 React state。
   - R3F 组件 `<PointCloudLayer>` 在 `useFrame(…, -1)` 里调用 `engine.update()`；开启 EDL 时由高优先级的 `useFrame` 接管渲染。
   - 统计数据以 4 Hz 节流写入 Zustand，供 lieflat 风格的性能 HUD 使用。

---

## 1. 仓库概览

| 项 | potree/potree | tentone/potree-core | pnext/three-loader |
|---|---|---|---|
| 版本 / 最后提交 | 1.8.0 / 2026-01-08（README 更新；核心代码多年未动） | 2.0.15 / 2026-09-14（PR #98：每个裁剪体独立 include/exclude） | 1.0.0 / 2026-05-28（依赖升级；2025-08 到 2025-11 有大量 3DGS 相关提交） |
| 语言 / 规模 | JS，`src/` 约 34k 行，着色器约 1.6k 行 | TS，`source/` 约 12.4k 行（其中 brotli `decode.js` 2.4k 行） | TS，`src/` 约 10k 行 |
| 构建 | gulp + rollup；`postinstall` 自动 build；three r124 放在 `libs/three.js` | webpack 5 + ts-loader + `worker-loader {inline:'no-fallback'}`（worker 内联成 blob）；着色器用 raw-loader | webpack 5 + babel；husky/commitlint；Node ≥ 22.21.1 |
| three 依赖 | 自带 r124 | peer `>0.125.0`，dev 0.154，`@types/three` 0.152 | peer `~0.160.0` |
| 其他依赖 | jQuery、proj4、TWEEN、d3、OpenLayers、i18next、laz-perf 等 | `brotli@1.3.3`（实际使用仓库内的 `loading2/libs/brotli/decode.js`） | 无运行时依赖（wasm 排序器在 example 中） |
| 支持的格式 | Potree 1.x（`cloud.js` + `.hrc` + `.bin`/LAS/LAZ）、2.0（DEFAULT/BROTLI）、EPT、COPC | 1.x、2.0（DEFAULT/BROTLI） | 1.x、2.0 DEFAULT、2.0 **GLTF**（`positions.glbin`/`colors.glbin`）、GLTF + **Gaussian Splats**（`sh_band_0`） |
| 入口 | `src/Potree.js`、`src/viewer/viewer.js` | `source/index.ts` → `Potree`、`PointCloudOctree`、`PotreeRenderer`、`EDLPass` | `src/index.ts` → `Potree('v1'\|'v2')`、`PointCloudOctree`、`SplatsMesh` |
| License | BSD-2 | MIT | MIT |

**谱系**：Potree 1.x（Schütz 2016）→ pnext/three-loader（2018 年把 Potree 移植成 TS 的 loader，Pix4D 维护）→ shiukaheng/potree-loader（三方 fork，加入 Potree 2.0 支持）→ potree-core 2.0（tentone 基于该 fork 重构）。因此 potree-core 和 three-loader 的 `potree.ts` 几乎逐行相同（§2.4）。

**活跃度**：
- potree-core 在 2026 年仍有功能 PR（EDL、裁剪体、raycast 修复），最契合“嵌入普通 Three.js 场景”这一需求。
- three-loader 最近的主要投入在 3DGS LOD，2026 年只有依赖更新。
- Potree 原版核心处于维护停滞，作者的精力已转到 Potree-Next（WebGPU）。

---

## 2. 源码结构与关键模块

### 2.1 potree/potree（原始实现，参考价值最高）

```text
src/
├── Potree.js                     # 全局状态：pointBudget=1M(:101)、maxNodesLoading=4(:104)、lru、workerPool(:89)
├── Potree_update_visibility.js   # ★ updatePointClouds(:6) / updateVisibilityStructures(:37) / updateVisibility(:103)
├── LRU.js                        # 双向链表 LRU；freeMemory(:138) 以 Potree.pointLoadLimit 为阈值
├── PointCloudOctree.js           # toTreeNode、computeVisibilityTextureData(:321，含密度 lodOffset)、pick
├── PotreeRenderer.js             # ★ 自研 GL 渲染器：直接管理 VAO 和 uniform location，renderNodes(:698)
├── WorkerPool.js                 # 按 URL 分桶、无上限
├── modules/loader/2.0/           # OctreeLoader.js(NodeLoader.load)、DecoderWorker.js、DecoderWorker_brotli.js
├── materials/
│   ├── PointCloudMaterial.js
│   ├── EyeDomeLightingMaterial.js    # neighbours 在单位圆上均匀取 N 点
│   └── shaders/pointcloud.vs         # getLOD(:216)、getPointSizeAttenuation(:301)、getPointSize(:666)
│       shaders/edl.fs                # CloudCompare qEDL 移植
└── viewer/
    ├── viewer.js                 # minNodeSize=30、edlStrength=1.0、edlRadius=1.4(:134-136)；pointLoadLimit=2×budget(:1628)
    ├── EDLRenderer.js            # 三遍：背景 → 点云写入 rtEDL（alpha 存 log2 深度）→ EDL 合成
    └── HQSplatRenderer.js        # 高质量加权 splat：depth pass → 加性 attribute pass → normalize pass
```

值得借鉴的细节：
- **强制显示前三层**：`updateVisibility` 在 `Potree_update_visibility.js:182` 有一行 `visible = visible || node.getLevel() <= 2`。L0–L2 即使在视锥外也保留，保证转身时不会出现空白。
- **变换版本号**：同一文件 :127–156 用 `_pointcloudTransformVersion` 做变换版本号，只在点云矩阵变化时才重算各节点的 `matrixWorld`，省掉每帧的矩阵乘法。
- **近平面代码无效**：:61–65 构造了一个 `near=0.1` 的 `frustumCam`，但随后用的仍是 `camera.projectionMatrix`，这段代码实际不起作用。
- **密度修正**：`PointCloudOctree.js:321` 的 `computeVisibilityTextureData` 按名字排序可见节点，编码“子节点位掩码 + 到第一个子节点的偏移”。**alpha 通道存的是密度修正**：`lodOffset = log2(density)/2 − 1.5`，写入值为 `(lodOffset+10)·10`。其中 `density` 由 decoder 用 32³ 网格统计，含义是“每个被占格子的平均点数”。着色器 `getLOD()` 在叶端返回 `depth + lodOffset`，高密度节点的点因此画得更小。**potree-core 和 three-loader 都没有保留这项修正**（alpha 只写 `name.length`，着色器也不读）。
- **自研渲染器**：`PotreeRenderer.renderNodes` 绕开 three 的 `WebGLRenderer.render`，每个节点只设置 `modelMatrix`/`modelViewMatrix`/`uVNStart`/`uLevel`/`uPCIndex` 等少数 uniform 然后 `drawArrays`。节点多时，这比 three 的“每对象完整状态检查”快得多。我们的 GLSL 后端在 V0.3 可以参考这一做法（§4.8）。

### 2.2 tentone/potree-core（我们移植的主体）

```text
source/
├── potree.ts                      # ★ Potree 类：updatePointClouds(:112) → updateVisibility(:165)
│                                  #   updateChildVisibility(:297)、shouldClip(:376)、updateVisibilityStructures(:432)
├── constants.ts                   # DEFAULT_POINT_BUDGET=1M, MIN_NODE_PIXEL_SIZE=50, MAX_LOADS_TO_GPU=2, MAX_NUM_NODES_LOADING=4
├── point-cloud-octree.ts          # PointCloudOctree extends Object3D：toTreeNode(:195)、hideDescendants(:298)、raycast(:369，兼容 EDL 图层)
├── point-cloud-octree-node.ts     # 已上 GPU 的“树节点”（包着 THREE.Points）
├── point-cloud-octree-picker.ts   # GPU picking（15×15 窗口，alpha=节点序号，RGB=点序号）
├── utils/lru.ts                   # LRU：touch / remove / freeMemory(numPoints > 2×budget) / disposeSubtree
├── utils/binary-heap.js           # 经典最小堆，score = 1/weight
├── loading2/                      # Potree 2.0
│   ├── OctreeLoader.ts            # NodeLoader.load(:37)、parseHierarchy(:183，22 B 记录)、createChildAABB(:304)、OctreeLoader.load(:478)
│   ├── OctreeGeometryNode.ts      # load()：numNodesLoading ≥ maxNumNodesLoading(=3) 时直接 return
│   ├── WorkerPool.ts              # DECODER_WORKER / DECODER_WORKER_BROTLI，空了就新建，没有上限
│   ├── decoder.worker.js          # DEFAULT：AoS int32 → Float32（减去节点 min）+ 32³ 密度统计 + INDICES
│   ├── brotli-decoder.worker.js   # BROTLI：BrotliDecode + Morton 解交织（dealign24b）
│   └── RequestManager.ts          # { fetch, getUrl }，可替换（签名 URL / 自定义缓存）
├── loading/                       # Potree 1.x（cloud.js / .hrc / .bin）
├── materials/
│   ├── point-cloud-material.ts    # updateMaterial(:884)、updateVisibilityTextureData(:947)、makeOnBeforeRender(:985)
│   └── shaders/pointcloud.vs|fs, edl.fs, blur.*, normalize.*
└── rendering/
    ├── edl-pass.ts                # ★ EDLPass：layer 0 常规渲染 → 点云 layer 1 渲染到 float RT → EDL 全屏合成（写 gl_FragDepth）
    └── potree-renderer.ts         # PotreeRenderer：统一处理 EDL 开关、图层、材质标志的保存与恢复
```

- **嵌入普通 Three.js 场景的方式**（`example/main.ts`）：
  1. `const potree = new Potree(); const pco = await potree.loadPointCloud('metadata.json', baseUrl); scene.add(pco);`
  2. 每帧先调用 `potree.updatePointClouds(pcos, camera, renderer)`，再调用 `potreeRenderer.render({renderer, scene, camera, pointClouds})`。开 EDL 时由后者跑三遍渲染，不开时等价于 `renderer.render`。
  3. 放进 R3F 时用 `<primitive object={pco}/>`，并在 `useFrame` 里调用 update。
  4. **前提是 `gl` 必须是 `WebGLRenderer`**。
- **README 已过时**：README 仍写着 “EDL shading is not supported by potree core”，但代码里已经有 `EDLPass`。

### 2.3 pnext/three-loader（TS 类型与 3DGS LOD）

```text
src/
├── potree.ts                     # Potree(version: 'v1'|'v2')；updatePointClouds 末尾额外调用 pointCloud.updateSplats()
├── constants.ts                  # MIN_NODE_PIXEL_SIZE=200, MAX_LOADS_TO_GPU=10, MAX_NUM_NODES_LOADING=10, MEMORY_SCALE=2, MAX_AMOUNT_OF_SPLATS=5.29M
├── types.ts                      # ★ IPointCloudTreeNode / IPointCloudGeometryNode(failed, load(): Promise) / IVisibilityUpdateResult / IPotree
├── utils/worker-pool.ts          # ★ 有界池（maxWorkers）+ AutoTerminatingWorker（空闲 7 s 自动 terminate）+ AsyncBlockingQueue（仅 v1 使用）
├── loading2/
│   ├── octree-loader.ts          # NodeLoader：按 metadata.encoding 选 Decoder / GltfDecoder / GltfSplatDecoder；try/finally 归还 worker
│   ├── decoder.ts                # DEFAULT 解码，额外返回 tightBoundingBox
│   ├── gltf-decoder.ts           # positions.glbin(12 B/pt) + colors.glbin(4 B/pt) 两次 Range
│   └── worker-pool.ts            # 同样无上限
├── splats-mesh.ts                # ★ LOD 化 3DGS：DataTexture 存协方差 / 颜色 / SH，visibilityNodes 纹理，排序 worker
└── workers/SortWorker.ts         # wasm 排序器（example/sorter_test.wasm）
```

**3DGS 的价值**：三个库里只有 three-loader 能“按八叉树 LOD 流式加载 3DGS”。这正对应 01-design §8 中 Visual World 从“点云”升级到“3DGS”的路线（V1.0），届时应与 spark 单元的结论一并评估。

### 2.4 三个库的参数与行为差异

| 项 | potree 1.8 | potree-core 2.0.15 | three-loader 1.0.0 | **我们的取值（§4.5）** |
|---|---|---|---|---|
| `pointBudget` 默认 | 1,000,000 | 1,000,000 | 1,000,000（example 用 1.2M） | 按档位设初值，由 FPS 闭环调节 |
| `minNodePixelSize` | viewer 30；PCO 默认 150 | **50** | **200** | 改用投影点间距阈值 τ（默认 1.5 px），与 G 换算见 §3.2 |
| 像素单位 | CSS px（`clientHeight`） | device px（`height × DPR`） | device px | 明确用 CSS px 定义 τ，再乘 DPR |
| 子节点权重 | `screenPixelRadius`；相机在球内时 `MAX_VALUE` | `screenPixelRadius + 1/distance` | 同 core，但 fov 用 `getEffectiveFOV()`（计入 zoom） | 投影点间距 × 屏幕中心权重 × 焦点权重 |
| 强制显示低层 | L ≤ 2 忽略视锥 | 无 | 无 | L ≤ 1 忽略视锥（概览不丢失） |
| 预算溢出 | `break` | `break` | `break` | 溢出节点只画一部分（fractional），然后 `break` |
| 未加载节点 | 计入预算并继续下探（“目标集”语义） | 同 | 同 | 保留“目标集”语义（§4.4） |
| 并发加载上限 | 全局 `numNodesLoading < 4` | `Potree.maxNumNodesLoading=4`，且每个几何体 `maxNumNodesLoading=3`（两层门限） | 10 | 请求在途 4–8 个，解码 worker 另设有界池 |
| 每帧上 GPU 上限 | 硬编码 2 个节点 | 2 个节点 | 10 个节点 | 按点数 / 字节预算 |
| LRU 上限 | `pointLoadLimit = 2 × budget` | `2 × budget` | `memoryScale × budget` | 点数和 GPU 字节双约束 |
| Worker 池 | 按 URL 分桶，无上限 | 按类型，无上限 | v1 有界并自动终止；v2 无上限 | 固定 N = min(4, hc−1) |
| EDL | `EDLRenderer` | `EDLPass` / `PotreeRenderer` | 材质支持 `useEDL`（alpha 写 log 深度），但没有提供 pass | 移植 potree-core 的 EDLPass |
| 自适应点大小 | `r=1.7·spacing`，`size·r/2^(LOD+lodOffset)` | `2·size·spacing/(0.5·2^LOD)` | 同 core | Lite / Full 两档（§3.5） |
| Picking | GPU 索引拾取 | GPU 索引拾取（≤255 节点）+ 兼容 EDL 图层的 raycast | GPU 索引拾取 | 深度回读为主，索引拾取为辅（§3.7） |

### 2.5 源码中发现的缺陷与坑（移植时逐条修正）

| # | 位置 | 问题 | 后果 | 修正 |
|---|---|---|---|---|
| 1 | potree-core `loading2/OctreeGeometryNode.ts` `load()` | 返回 `void`，但 `updateVisibility` 把它放进了 `nodeLoadPromises` | 调用方拿到的是 `undefined[]`，无法感知加载完成或失败 | `load()` 返回 Promise，失败时 reject |
| 2 | potree-core `OctreeLoader.ts:90–148` | worker 的 `onmessage` 里没有错误路径，也没有 `onerror`。`catch` 只重置 `loading`，从不设置 `failed` | 坏节点会每帧被重新请求，形成无限重试和请求风暴 | 增加 `failed`、`retryCount`、指数退避（1 s·2^k，最多 3 次）以及 `worker.onerror` |
| 3 | 两个库的 `loading2/WorkerPool` | 池子空了就 `new Worker`，没有上限 | 快速转动视角时可能同时存在几十个 worker，内存和 CPU 被打满 | 固定大小的池 + 任务队列；可选空闲自动终止（参考 three-loader `utils/worker-pool.ts`） |
| 4 | 两个库的 `makeOnBeforeRender` | 每次 draw 都执行 `octree.visibleNodes.indexOf(node)` | 复杂度 O(n²)，1000 个可见节点时约 50 万次比较 | 在 `updateVisibilityTextureData` 里给每个节点写好 `node.pcIndex` |
| 5 | 两个库的 `visibleNodes` 纹理 | 固定为 `2048×1`，着色器写死 `/2048.0` | 可见节点超过 2048 个时越界，产生错误的点大小 | 改成 2D 纹理（`width=2048`，`row=i>>11`），或者 `maxVisibleNodes ≤ 2048` |
| 6 | potree-core `pointcloud.vs:425` | 只要定义了 `new_format`（Potree 2.0 数据），`vColor = rgba` 就无条件生效 | 高度、强度、LOD 等着色模式对 2.0 数据全部失效。UrbanScene3D 没有 RGB，会整片发黑 | 按 three-loader 的方式，把 rgba 只作为 `getRGB()` 的来源 |
| 7 | three-loader `point-cloud-octree.ts:79` | `material \|\| pcoGeometry instanceof OctreeGeometry ? A : B` 存在运算符优先级错误 | 传入的自定义材质被忽略 | 补上括号 |
| 8 | three-loader `loading2` | 不支持 `encoding: "BROTLI"` | PotreeConverter 用 `--encoding BROTLI` 生成的数据无法加载 | 我们主用 ANET_Q16 / gzip；BROTLI 按需实现 |
| 9 | potree-core `potree.ts:248` 与 potree `:370` | 前者用 `height × DPR`，后者用 `clientHeight` | 同一个 `minNodePixelSize` 在 DPR=2 的屏上细分程度差 2 倍（节点数约差 4 倍） | τ 按 CSS px 定义，内部换算时显式乘 DPR |
| 10 | 三个库的 `updateChildVisibility` | 用**立方体**包围球算投影半径，也用立方体做视锥裁剪 | 城市数据很扁（NY 的 z 范围 292 m，立方体边长 3167 m），投影尺寸被高估，导致过度细分和误判可见 | 使用 tight bbox（r09 的 `hierarchy_ext.bin`；three-loader 的 decoder 已经回传 `tightBoundingBox`） |
| 11 | potree-core `hideDescendants` | BFS 队列用 `Array.shift()` | O(n²)，而且每帧都要遍历全部场景节点 | 维护一个 `renderSet`，只把上一帧可见、这一帧不可见的节点置为 `visible=false` |
| 12 | 两个库的 EDL | alpha 存 `log2(depth)`，并用 `alpha==1.0` 标记背景 | 线性深度恰好等于 2 m 的点会被当成背景丢掉（potree-core 注释里承认了这一点） | 用单独的深度纹理，或者把背景标记改为 `-1` |
| 13 | 两个库的 picker | 最多 255 个节点（alpha 存节点序号），而且要求 CPU 端仍保留 position 数组 | GPU 上传后释放 CPU 副本的优化做不了 | 默认改用深度回读拾取（§3.7） |
| 14 | potree `Potree_update_visibility.js:61–65` | `frustumCam` 构造了却没有用上 | 无（死代码） | — |
| 15 | potree-core `getPointSizeAttenuation` | `worldSize = 2·size·spacing/(0.5·2^LOD)`，相当于 **4×** 局部点距；Potree 原版是 1.7× | 同一个 `size` 在两个库里视觉差 2.35 倍 | 统一为 `k · size · spacing_LOD`，k 作为参数，默认 1.4 |

---

## 3. 可复用算法与实现

### 3.1 LOD 节点选择（Potree `updateVisibility`，三库共同核心）

potree-core `potree.ts:165–275` 的等价伪代码：

```text
function updateVisibility(pointClouds, camera, renderer):
  heap ← MinHeap(score = 1/weight)                       // 实际效果：weight 大的先出队
  for pc in pointClouds:
    frustum_obj[pc] ← Frustum(P · V · M_pc)              // 物体空间视锥，只算一次，省掉逐节点变换
    cam_obj[pc]     ← (M_pc⁻¹ · camera.matrixWorld).position
    heap.push({pc, node: pc.root, weight: +∞})
    hide all previously visible sceneNodes
  numVisible ← 0; loadedToGPU ← 0; unloaded ← []
  while item ← heap.pop():
    n ← item.node
    if numVisible + n.numPoints > pointBudget: break      // ① 预算截断：直接停止，不再尝试后面的小节点
    if n.level > maxLevel or !frustum_obj.intersectsBox(n.box) or clippedByBoxes(n): continue
    numVisible += n.numPoints                             // ② 未加载的节点也计入预算，即“预留”
    if n is GeometryNode and (parent is null or parent is TreeNode):
       if n.loaded and loadedToGPU < MAX_LOADS_TO_GPU: n ← toTreeNode(n); loadedToGPU++   // ③ 上传节流
       elif !n.failed: unloaded.push(n)                   // 只有“父节点已上 GPU”的节点才能进加载队列
    if n is TreeNode: lru.touch(n); n.sceneNode.visible ← true; visibleNodes.push(n)
    for c in n.children (non-null):                       // ④ 即使 n 还没加载，也会展开它的子节点（目标集语义）
       d ← |c.sphere.center − cam_obj|; r ← c.sphere.radius
       pf ← halfHeight / (tan(fov/2) · d)                 // 透视；正交相机为 2·halfH·zoom/(top−bottom)
       rpx ← r · pf
       if rpx < pc.minNodePixelSize: continue             // ⑤ 屏幕上太小就不细分
       weight ← (d < r) ? +∞ : rpx + 1/d
       heap.push({pc, node: c, parent: n, weight})
  for i < min(maxNumNodesLoading, unloaded.length): unloaded[i].load()   // ⑥ 按优先级顺序发起加载
  return {visibleNodes, numVisiblePoints, exceededMaxLoadsToGPU, nodeLoadFailed, nodeLoadPromises}

then per pc: material.updateMaterial(pc, visibleNodes, camera, renderer)   // 构建 visibleNodes 纹理、屏幕尺寸、fov
             lru.freeMemory()                                          // 常驻点数 > 2×budget 时淘汰最久未用的子树
```

**设计要点**：
- **预算是全局的**，所有点云共用一个堆。更近、更大的节点自然先分到点。
- **加法式 LOD**：父节点的点不会在子节点中重复。所以子节点必须在父节点已渲染的前提下才有意义，加载队列只收“父节点已上 GPU”的前沿节点。
- **“目标集”语义**：未加载的节点也计入预算，并继续展开子节点。这样预算锁定的是“理想可见集”，加载完成后可见集不会大幅变动，从而避免抖动。
- **限流**：每帧上传 2 个节点、同时最多加载 4 个。宁可慢慢细化，也不在单帧造成卡顿。

### 3.2 投影尺寸与点间距（SSE）的换算：`minNodePixelSize` 的物理含义

PotreeConverter 2 以及 r09 的 `worldpkg tile` 都满足 `spacing_L = cube_L / G`，其中 G 是每个节点的采样网格：Potree 用 128，r09 推荐 64。示例数据 `potree-core/example/data/pump/metadata.json` 验证了这一点：立方体边长 5.033，spacing 0.03932，5.033 / 0.03932 = 128。节点立方体包围球半径 `r_L = cube_L·√3/2`。因此：

```text
r_px(L)  = r_L · pf(d)
s_px(L)  = spacing_L · pf(d) = r_px(L) · 2/(√3·G)          // 投影点间距
⇒ minNodePixelSize = τ · (√3/2) · G                         // τ：期望的屏幕点间距（px）
   G=128：τ=1.5 px ⇔ minNodePixelSize ≈ 166 px；τ=0.45 px ⇔ 50 px（potree-core 默认值，严重过细分）
   G=64 ：τ=1.5 px ⇔ ≈ 83 px
pf(d) = (H_css·DPR/2) / (tan(fov_y/2) · max(d − r_tight, near))    // 用 tight 球的最近点距离，更保守
```

**细分判据**：子节点的投影点间距 `s_px(child) ≥ τ` 时才细分，意思是子节点的点在屏幕上仍然能分辨。

**迟滞**：为防止在阈值附近闪烁，上一帧已可见的节点用 `τ·0.9`，新细分的节点用 `τ·1.1`。

**推荐的优先级（weight）**，在 Potree 基础上加两个乘子：

```text
w = s_px · w_center · w_focus ;  相机在 tight 球内时 w = +∞
w_center = clamp(1 − |ndc_xy(c)|, 0, 1) + 0.5          // 来自 Potree-Next PointCloudOctree.js:206-215：屏幕中心优先
w_focus  = 1 + α · exp(−|c − p_focus|² / (2σ²))        // 本项目新增：选中的无人机或 FPV 目标附近优先，α=1.5，σ=150 m
```

**可选：街景视角的远处降级**。无人机低空 FPV 看向地平线时，可以借鉴 `Cesium3DTileset.js:90-93` 的 `dynamicScreenSpaceError`（默认 density 2e−4、factor 24、heightFalloff 0.25），对远处节点放宽阈值：

```text
τ_eff = τ · (1 + factor · (1 − exp(−(density·d)²)))
```

只在相机高度低于场景高度的 25% 时启用。

### 3.3 节点加载管线：hierarchy/proxy → Range → Worker 解码 → GPU 上传

Potree 2.0 的格式细节见 r09 §3.1，这里只列 loader 端的要点：

1. **hierarchy**：
   - `OctreeLoader.load` 读 `metadata.json` 后，把根节点设为 `nodeType=2`（proxy），并令 `hierarchyByteSize = firstChunkSize`。
   - 根节点第一次 `load()` 时，先用 `loadHierarchy` 发 Range 读取 `hierarchy.bin`，再由 `parseHierarchy` 按 BFS 顺序展开 22 B 记录：`<BBIqq>`，即 type、childMask、numPoints、byteOffset、byteSize。
   - `type==2` 的记录是下一层 chunk 的代理，等访问到时再加载。
   - 子节点包围盒由 `createChildAABB` 计算：childIndex 的 bit2 对应 x，bit1 对应 y，bit0 对应 z。
2. **节点负载**：
   - `fetch(octree.bin, {Range: bytes=off-(off+size-1)})`。potree-core 还多带了一个无意义的 `content-type: multipart/byteranges` 请求头，跨域时会触发 CORS 预检，**移植时去掉**。
3. **DEFAULT 解码**（`decoder.worker.js`）：
   - 每点 `x = int32·scale + offset − nodeMin`，结果是相对节点 min 的 float32，节点 `Points.position = box.min`。这样既保留了精度，每节点的 modelView 又在 JS 里用双精度计算。
   - rgb 为 u16，大于 255 时除以 256。
   - 其他属性打包成 f32，并附带 `preciseBuffer`。
   - 生成 `INDICES`，供 picking 使用。
   - 用 32³ 网格统计 `density = numPoints / 被占格子数`。
4. **BROTLI 解码**：先 `BrotliDecode`（纯 JS，慢），再对 SoA 布局按 16 B/点做 Morton 解交织。**本项目不作为主路径**：r09 的实测结论是 ANET_Q16 + 原生 `DecompressionStream('gzip')` 更合适。
5. **GPU 上传**：`toTreeNode` 新建 `THREE.Points(geometry, pc.material)`，挂到父节点的 `sceneNode` 下，设 `frustumCulled=false`（剔除由 LOD 负责），`onBeforeRender` 负责逐节点写 uniform（level、vnStart、pcIndex）。three 在该对象第一次 render 时才上传 buffer，所以“每帧最多 2 个 toTreeNode”实际上就是在限制每帧的上传量。

### 3.4 LRU 缓存与 GPU 内存回收

- `utils/lru.ts` 是双向链表加 `Map<id, item>`：
  - `touch(node)` 把节点移到尾部，表示最近使用。
  - `freeMemory()` 在 `numPoints > budget × 2` 时反复取表头，即最久未用的节点，调用 `disposeSubtree`。
- **连带整棵子树释放**，因为加法式 LOD 下子节点离开父节点就没有意义。
- `dispose()` 触发 `oneTimeDisposeHandlers`：释放 `BufferGeometry`，并把父节点的 `children[i]` 从 TreeNode 换回 GeometryNode（`point-cloud-octree.ts:212-218`）。
- 根节点不释放，`OctreeGeometryNode.dispose` 要求 `parent != null`。
- **缺口**：
  - 只按点数计，不按字节计。不同属性组合（例如 DEFAULT 解码后约 20+ B/点，ANET_Q16 为 12 B/点）占用的 GPU 内存差别很大。
  - three 在上传后仍保留 CPU 数组，内存实际翻倍。
- **我们的改进**：
  - 双约束：`residentPoints ≤ memFactor·B` 且 `gpuBytes ≤ gpuMemLimit`。
  - `BufferAttribute.onUpload(() => attr.array = null)` 释放 CPU 副本，前提是默认使用深度回读拾取（§3.7）。
  - 淘汰时跳过本帧目标集中的节点。

### 3.5 自适应点大小：visibleNodes 纹理与 getLOD

**问题**：加法式 LOD 下，同一块区域可能叠着 L、L+1、L+2 三层的点。如果按各自节点层级设置点大小，父层的点会过大，遮住细节。正确做法是：**每个点按它所在位置的“最深可见层级”来定大小**。

**编码**（potree-core `point-cloud-material.ts:947`，RGBA8，每个可见节点 1 个 texel）：

```text
nodes.sort(byLevelAndIndex)                 // 先按名字长度，再按字典序：r, r0, r3, r01, r07, r30 ...
for i, node in nodes:
   offsets[node.name] = i
   if i > 0:
     p = offsets[parentName(node)]
     offsetToChild[p] = min(offsetToChild[p], i − p)         // 父节点到“第一个可见子节点”的距离
     tex[p].r |= 1 << node.index                              // 子节点可见位掩码
     tex[p].g  = offsetToChild[p] >> 8 ; tex[p].b = offsetToChild[p] & 255
   tex[i].a = potree: (log2(density)/2 − 1.5 + 10)·10  |  core/loader: name.length（着色器不使用）
```

**顶点着色器**（`pointcloud.vs` 中的 `getLOD`，以 Potree 原版为准）：

```glsl
float getLOD() {                          // position：相对节点 min 的局部坐标
  vec3 offset = vec3(0); int iOffset = int(vnStart); float depth = level;
  for (float i = 0.0; i <= 30.0; i++) {
    float nodeSize = octreeSize / pow(2.0, i + level);          // 当前所在节点的边长
    vec3 idx3 = floor((position - offset) / nodeSize + 0.5);    // 落在哪个子八分体（0 或 1）
    int  idx  = int(round(4.0*idx3.x + 2.0*idx3.y + idx3.z));
    vec4 v    = texture(visibleNodes, vec2(float(iOffset)/2048.0, 0.0));
    int  mask = int(round(v.r*255.0));
    if (isBitSet(mask, idx)) {                                  // 该子八分体可见 → 下探一层
      iOffset += int(round(v.g*255.0))*256 + int(round(v.b*255.0)) + numberOfOnes(mask, idx-1);
      depth++;
    } else {
      return depth + ((255.0*v.a)/10.0 - 10.0);                // Potree 原版：叶端加上密度修正
    }
    offset += nodeSize * 0.5 * idx3;
  }
  return depth;
}
// 点大小（透视）：
float projFactor = -0.5*screenHeight / (tan(fov/2.0) * mvPosition.z);
float worldSize  = k * size * spacingRoot / pow(2.0, getLOD());   // k：Potree 1.7，core/loader 4.0，我们用 1.4
gl_PointSize     = clamp(worldSize * projFactor, minSize, maxSize);
```

**代价**：每个顶点最多循环 depth 次纹理采样加位运算。在 SwiftShader 上顶点是瓶颈，所以**无头档和低端档不用 Full 版**。

**Lite 版**（本项目新增，开销 O(1)）：每个节点额外传一个 `uChildDrawnMask`（8 位，表示“本帧已渲染的子节点”）。着色器只判断一层：

```glsl
uniform vec3 uNodeMin; uniform float uNodeSize; uniform float uNodeSpacing; uniform float uChildDrawnMask;
vec3 rel = (position) / uNodeSize;                               // position 已是节点局部坐标，取值 0..1
int oct  = (rel.x >= 0.5 ? 4 : 0) | (rel.y >= 0.5 ? 2 : 0) | (rel.z >= 0.5 ? 1 : 0);
bool deeper = ((int(uChildDrawnMask) >> oct) & 1) == 1;
float spacingLocal = deeper ? 0.5 * uNodeSpacing : uNodeSpacing;
gl_PointSize = clamp(uSizeK * spacingLocal * projFactor, uMinPx, uMaxPx);
```

- **误差**：孙节点也可见的区域，点会偏大约 2 倍。深度测试会遮住大部分，视觉上是轻微发糊。
- **收益**：零纹理采样、无循环，并且 WebGL2 和 WGSL 都容易实现。
- **分档**：Full 版用于高端 GPU 档；Lite 版用于中低端和无头档；Node 版（只用节点自身的层级）作为最低兜底。

### 3.6 EDL（Eye-Dome Lighting）

来源：CloudCompare qEDL，经 potree `edl.fs` 移植到 potree-core `rendering/edl-pass.ts` 与 `materials/shaders/edl.fs`。

**三遍渲染**：
1. 用相机的 layer mask 去掉点云图层（默认 layer 1），渲染其余场景到默认帧缓冲，得到颜色和深度。
2. 只渲染点云图层，输出到 `rtEDL`（RGBA Float，`NearestFilter`，带深度）。点云片元着色器在 `use_edl` 下把 `log2(linearDepth)` 写进 alpha。
3. 全屏 EDL pass，与第 1 遍的深度做深度测试，并写 `gl_FragDepth`，保证点云与网格、无人机模型正确互相遮挡。

**着色公式**：

```text
uvR = radius / (W, H)
res(d) = (1/N) Σ_i  [d_i ≠ 0] · ( d == 0 ? 100 : max(0, d − d_i) ),   d_i = alpha(uv + uvR·nb_i), nb_i = (cos 2πi/N, sin 2πi/N)
shade  = exp(−res · 300 · edlStrength)
out    = (rgb · shade, opacity);  gl_FragDepth 从 dl = 2^d 反算：logdepth / reversed-Z / 常规三种写法
```

**参数**：

| 参数 | Potree viewer | potree-core `EDLPass` | 我们的默认值 |
|---|---|---|---|
| `edlStrength` | 1.0 | 0.4 | 0.6 |
| `radius` | 1.4 | 1.4 | 1.4 |
| N（邻域采样数） | 8 | 8 | 8；低端档用 4 |

- **启用前提**：WebGL2 需要 `EXT_color_buffer_float`（SwiftShader 支持）。
- **何时开**：UrbanScene3D 没有颜色，EDL 能显著增强轮廓，是高端档的默认项。
- **无头档必须关闭**：本机实测 720p 的 8-tap pass 约 107 ms。改用法线 Lambert 着色，开销在顶点阶段，几乎为零。

### 3.7 Picking（拾取）

**Potree 的 GPU 索引拾取**（`point-cloud-octree-picker.ts`）：
1. 在可见节点中筛出与拾取射线相交的节点（`ray.intersectsSphere`）。
2. 复制成临时 `Points`，用 pick 材质把点序号写进 RGB（`indices` 属性：u8×4 normalized，共 24 位），把节点序号 +1 写进 alpha，最多 255 个节点。
3. 只在 scissor 限定的 15×15 px 窗口内渲染，然后 `readRenderTargetPixels`。
4. 取离窗口中心最近的命中像素，回查 CPU 端的 position/normal 数组，得到世界坐标。

**本项目的推荐方案**：
- **默认：深度回读拾取**。复用 EDL 的 alpha（log2 深度）或点云 pass 的深度纹理，读 1×1（或 5×5 取最小）像素，反投影得到世界坐标。
  - 不需要 `indices` 属性，也不需要 CPU 端数组，可以放心释放内存。
  - 同一套方法适用于网格和无人机。
- **按需：索引拾取**。只有要查询点属性（分类、强度）时才用，并且只在被拾取的节点上临时保留 CPU 数组（或者重新请求该节点）。
- **API 统一为 `async pick(x, y): Promise<PickResult>`**：
  - WebGPU 的回读天然是异步的（`mapAsync`）。
  - WebGL2 用 PBO 加 `fenceSync` 做非阻塞回读，避免 `readPixels` 同步卡顿。
- **用途**：任务规划里的“点云上放航点”、“点选 GoTo 目标”，以及测距。

### 3.8 裁剪体（Clip Volumes，potree-core 2026 新增的统一公式）

```text
B = (!hasInclude || insideAnyInclude) && !insideAnyExclude ;  P = insideAllPlanes ; S = hasBoxOrSphere
CLIP_OUTSIDE: visible = B && P ;  CLIP_INSIDE: visible = (S && B) || !P ;  HIGHLIGHT_INSIDE：只高亮，不裁剪
```

- **CPU 侧剔除**（`potree.ts:376 shouldClip`）：只在“全部为 include 盒、没有球也没有平面”时，才按节点做整体剔除。
- **用途**：
  - 禁飞区 / Restricted Area 高亮（01-design §7 Semantic）。
  - “剖切”查看建筑内部。
  - 任务区域外的点云变暗。
- **UI 配色**：高亮用产品红 `#E93024`。
- **复用方式**：port 着色器片段，最多 30 个盒 / 球 / 平面。

### 3.9 其他可参考的实现

- **Potree 的 HQ weighted splats**（`viewer/HQSplatRenderer.js`，三遍渲染）：
  1. 深度预 pass，深度加上 `blendDepthSupplement`。
  2. 加性混合 pass，按 `weight = 1 − r²`（抛物面）累加 `rgb·w` 和 `w`。
  3. normalize pass 输出 `rgb / w`。
  - 效果：点的边缘柔和、没有锯齿，但代价约 3 倍。定位为“静态截图 / 演示模式”（V0.3）。
- **three-loader 的 splats LOD**（`splats-mesh.ts`）：把 3DGS 的协方差、颜色、SH 放进 DataTexture，用可见节点纹理决定哪些 splat 生效，由 wasm worker 排序。这是 V1.0 “Visual World = 3DGS” 的直接参考，要和 spark / supersplat 单元一起评估。
- **Potree 自研渲染器**（`PotreeRenderer.js`）：逐节点只设置最少的 uniform，直接调用 `gl.drawArrays`。节点数达到上千时，比 three 的通用渲染路径每节点省约 10–30 µs 的 JS 开销。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用矩阵

| 算法 / 模块 | 来源 | 落点（本项目模块） | 版本 | 方式 |
|---|---|---|---|---|
| best-first LOD 遍历、目标集语义、预算截断 | core `potree.ts` / potree `Potree_update_visibility.js` | `apps/web/src/engine/pointcloud/core/selector.ts` | V0.1 | **port**（加入 tight bbox、τ、迟滞、中心和焦点加权、fractional） |
| hierarchy / proxy 解析、childAABB | core `loading2/OctreeLoader.ts` | `…/pointcloud/io/hierarchy.ts` | V0.1 | **port** |
| DEFAULT 解码 worker（兼容路径） | core `decoder.worker.js` | `…/pointcloud/io/decode.worker.ts` | V0.1 | **port**（Vite 的 `new Worker(new URL(…), {type:'module'})`） |
| ANET_Q16 零解码路径 + gzip | r09 §3.3 | `…/pointcloud/io/q16.ts` | V0.1 | 自研 |
| LRU 与子树释放 | core `utils/lru.ts` | `…/pointcloud/core/cache.ts` | V0.1 | **port**（双约束、onUpload 释放 CPU 副本） |
| 有界 worker 池、空闲终止 | three-loader `utils/worker-pool.ts` | `…/pointcloud/io/workerPool.ts` | V0.1 | **port** |
| TS 公共类型（节点、结果、Potree 接口） | three-loader `types.ts` | `…/pointcloud/types.ts` | V0.1 | **reference**（保留形状，改成 async 语义） |
| GLSL 点材质（Lite 自适应、高度 / 法线 / 强度 / LOD 着色、裁剪） | core `pointcloud.vs/fs` | `…/pointcloud/render/glsl/` | V0.1 | **port**（删掉 30% 用不到的分支） |
| Full 自适应点大小（visibleNodes 纹理 + 密度修正） | potree `PointCloudOctree.js:321` + `pointcloud.vs:216` | 同上 | V0.3 | **port** |
| EDLPass | core `rendering/edl-pass.ts` + `edl.fs` | `…/pointcloud/render/glsl/edl.ts` | V0.1（高端档） | **port** |
| GPU 索引拾取 | core `point-cloud-octree-picker.ts` | `…/pointcloud/pick/` | V0.2 | **reference**（默认用深度回读） |
| 裁剪体统一公式 | core README 与 `pointcloud.vs` | `…/render/glsl/clip.glsl` | V0.3 | **port** |
| TSL / WGSL 后端（vertex pulling） | three r186 `PointsNodeMaterial`、Potree-Next | `…/pointcloud/render/tsl/` | V0.3 | 自研 |
| HQ weighted splats | potree `HQSplatRenderer.js` | 演示模式 | V0.3 | **reference** |
| 3DGS LOD | three-loader `splats-mesh.ts` | Visual World 3DGS | V1.0 | **reference** |
| potree-core 整库 | npm `potree-core@2.0.15` | `apps/web/dev/potree-crosscheck`（开发页） | V0.1 | **adopt（dev only）** |

### 4.2 PointCloudEngine 总体架构

```text
                    ┌────────────────────────── PointCloudEngine（纯 TS，无 React 依赖）─────────────────────────┐
 camera, viewport ─►│ Controller（FPS 闭环）──► budget B, τ, 画质阶梯（EDL / DPR / sizeMode）                     │
 frame stats  ─────►│        │                                                                                   │
                    │        ▼                                                                                   │
                    │ Selector（每帧，<1 ms）── 目标集 T、渲染集 R（含 fractional 数量）、加载队列 Q ──┐        │
                    │        ▲                                                                           │        │
                    │  NodeStore（层级树、状态机、tight bbox）◄── HierarchyLoader（proxy 分页）          │        │
                    │        ▲                                                                           ▼        │
                    │  Cache（LRU：点数 + 字节）◄── Uploader（每帧点数 / 字节预算）◄── Decoded 队列 ◄── Fetcher │
                    │        │                                          （Range 合并、AbortController、重试） │
                    │        │                                          WorkerPool（gzip / DEFAULT 解码）     │
                    │        ▼                                                                                   │
                    │ RenderBackend 接口 ── GlslBackend（WebGLRenderer，gl_PointSize，EDLPass）                  │
                    │                    └─ TslBackend（WebGPURenderer，vertex-pulling quad，TSL EDL）           │
                    │ Picker（深度回读，async）   Stats / Events（4 Hz）                                         │
                    └────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 数据结构（TypeScript）

```ts
export type NodeState = 'unloaded' | 'queued' | 'loading' | 'decoded' | 'resident' | 'failed';

export interface PCNode {
  id: number; name: string; level: number; index: number;          // index = 子节点序号 0..7
  parent: PCNode | null; children: (PCNode | null)[];               // 长度 8
  cubeMin: Float64Array; cubeSize: number;                          // 八叉树局部坐标（ENU，米）
  tightMin: Float32Array; tightMax: Float32Array;                   // 来自 hierarchy_ext.bin，缺省时等于立方体
  center: Float32Array; radius: number;                             // tight 包围球
  spacing: number;                                                  // spacingRoot / 2^level
  numPoints: number;
  byteOffset: bigint; byteSize: bigint;                             // octree.bin 中的负载位置
  proxy?: { offset: bigint; size: bigint };                         // 下层 hierarchy chunk（未展开时存在）
  state: NodeState; retry: number; retryAt: number;
  lastTargetFrame: number; lastRenderFrame: number;
  gpu?: GpuNode;                                                    // 后端相关句柄（Points / Mesh + buffers）
  drawCount: number;                                                // 本帧绘制的前缀点数（fractional / 淡入）
  fadeStart: number;                                                // 驻留时刻，用于密度淡入
  childDrawnMask: number;                                           // Lite 自适应点大小用
  prio: number; sPx: number;                                        // 调试与统计
}

export interface PointCloudSource {
  id: string; baseUrl: string;                                      // 例如 /worlds/newyork/pointcloud/
  metadata: PotreeMetadata & { anet?: AnetExt };                    // r09 §3.1 / §3.3
  root: PCNode; nodesById: PCNode[];
  transform: Matrix4;                                               // 八叉树局部 → 世界（ENU→three Y-up）
  encoding: 'DEFAULT' | 'ANET_Q16';
}

export interface EngineStats {
  fps: number; frameMsP50: number; frameMsP95: number;
  budget: number; tauPx: number; visiblePoints: number; visibleNodes: number;
  residentPoints: number; gpuBytes: number; inflight: number; queued: number; failed: number;
  qualityStep: number; tier: TierName; backend: 'webgl2' | 'webgpu';
}
```

### 4.4 每帧算法（完整伪代码）

```text
update(camera, viewportCss, dpr, dtMs, focus?):
  frame++ ; now ← performance.now()
  ctl.observe(lastFrameStats)                                   // §4.6，每 200 ms 调整一次
  B ← ctl.effectiveBudget(interacting) ; τ ← ctl.tau ; H ← viewportCss.h · dpr·ctl.dprScale
  for src in sources: fr[src] ← objectSpaceFrustum(P·V·M_src) ; cam[src] ← M_src⁻¹·camPos ; vel[src] ← 相机速度（物体空间）

  // ---- 1. 选择：目标集 T / 渲染集 R / 加载队列 Q ----
  heap.clear() ; for src: heap.push(src.root, +∞)
  used ← 0 ; R ← [] ; Q ← [] ; hierQ ← []
  while n ← heap.pop():
     if n.level > 1 and !fr.intersectsBox(n.tight): continue                   // L≤1 不做视锥裁剪
     k ← n.numPoints
     if used + k > B:
        if n.state == resident and parentRendered(n) and B − used ≥ 512 and src.pointOrder=='shuffled':
            n.drawCount ← B − used ; R.push(n) ; used ← B                       // fractional：只画一个前缀
        break
     used += k ; n.lastTargetFrame ← frame
     if n.state == resident and parentRendered(n):
         n.drawCount ← fadeCount(n, now) ; R.push(n) ; n.lastRenderFrame ← frame ; cache.touch(n)
     else if n.state ∈ {unloaded, failed(可重试)} and (n.parent == null or n.parent.state == resident):
         Q.push(n)                                                             // 只加载前沿节点
     if n.proxy: hierQ.push(n) ; continue                                      // 子节点未知，先展开层级
     for c in n.children if c:
         s ← c.spacing · (H/2) / (tan(fov/2) · max(|c.center − cam| − c.radius, near))
         th ← (c.lastRenderFrame == frame−1) ? 0.9τ : 1.1τ                     // 迟滞
         if s < th: continue
         w ← (|c.center − cam| < c.radius) ? +∞ : s · wCenter(c) · wFocus(c, focus)
         heap.push(c, w)
  for n in R: if n.parent in R: n.parent.childDrawnMask |= 1 << n.index        // 先对 R 中节点清零再累加
  hide(prevR \ R)                                                               // 只翻转差集的 visible 标志

  // ---- 2. 加载调度 ----
  fetcher.cancelStale(frame − 30)                                               // 30 帧未进入目标集 → abort
  for n in hierQ ∪ Q（按 prio 降序） while fetcher.inflight < tier.maxInflight:
     if n.state == failed and now < n.retryAt: continue
     batch ← mergeContiguousSiblings(n, Q, ≤ 4 MB)                              // BFS 布局下兄弟节点相邻，一次 Range 取回
     fetcher.request(batch) → worker.decode → decodedQ.push
  if fetcher.inflight < tier.maxInflight/2 and !interacting:
     prefetch(predictCamera(cam, vel, 0.4 s), guardBand = 1.2)                  // 预测相机位置 + 扩大视锥，低优先级

  // ---- 3. 上传（节流） ----
  up ← tier.uploadPtsPerFrame
  while up > 0 and d ← decodedQ.popMaxPrio():
     backend.createNode(d) ; d.node.state ← resident ; d.node.fadeStart ← now ; up −= d.node.numPoints

  // ---- 4. 回收 ----
  cache.evictWhile(residentPts > tier.memFactor·B or gpuBytes > tier.gpuMemLimit, skip = lastTargetFrame == frame)

  // ---- 5. 材质 / uniform ----
  backend.prepareFrame(R, camera, sizeMode)       // Full 档：构建 visibleNodes 纹理，并写好每个节点的 pcIndex / vnStart
  stats.push(...)                                 // 4 Hz 发送给 UI
```

**`fadeCount(n, now)`**：`drawCount = n.numPoints · smoothstep(0, 1, (now − n.fadeStart)/250ms)`，前提是节点内点序随机（ANET_Q16）。
- 新节点以“密度渐增”的方式出现，不用 alpha 混合，没有排序问题，可以和 transitions.dev 的时间曲线统一。
- 预算下降时，`drawCount` 同样线性减小，节点不会突然消失。

### 4.5 参数默认值（分档）

| 参数 | desktop dGPU | iGPU / 笔记本 | 移动端 | headless SwiftShader（CI） |
|---|---|---|---|---|
| 后端 | WebGPU（TSL）或 WebGL2 | WebGL2（首选）/ WebGPU | WebGL2 | **WebGL2（经典 `WebGLRenderer`）** |
| `targetFps` | 60 | 60（退到 45） | 30 | 20–30 |
| 初始预算 `B0` | 2,000,000 | 1,000,000 | 400,000 | **40,000** |
| `Bmin` / `Bmax` | 200k / 8M | 100k / 3M | 50k / 1M | 10k / 200k |
| τ（投影点间距，CSS px） | 1.2 | 1.5 | 2.0 | 3.0 |
| 等效 `minNodePixelSize`（G=64 / 128） | 67 / 133 | 83 / 166 | 111 / 222 | 166 / 333 |
| 在途请求数 `maxInflight` | 8（HTTP/2） | 6 | 4 | 4 |
| 解码 worker 数 | min(4, hc−1) | 2–3 | 2 | 2 |
| 每帧上传点数 | 500k | 200k | 100k | 20k |
| 常驻因子 `memFactor` | 3 | 2 | 1.5 | 4（CPU 内存充足） |
| GPU 内存上限 | 1.5 GB | 512 MB | 256 MB | 256 MB |
| 点大小模式 | Full | Lite | Lite | Node / Lite |
| `sizeK` / `minPx` / `maxPx` | 1.4 / 1 / 32 | 1.4 / 1.5 / 24 | 1.6 / 2 / 16 | 1.6 / 2 / 8 |
| 点形状 | 圆（discard） | 圆 | 方 | 方 |
| EDL | 开（N=8，全分辨率） | 开（N=4） | 关 | **关** |
| 着色 | EDL + 法线 Lambert + 高度渐变 | 法线 + 高度 | 法线 + 高度 | 法线 + 高度 |
| DPR 上限 | 2 | 1.5 | 1.5 | 1 |
| 可见节点上限 | 2048 | 1024 | 512 | 128 |

**档位检测伪代码**：

```ts
async function detectTier(): Promise<Tier> {
  const a = await navigator.gpu?.requestAdapter?.();
  const soft = (s: string) => /swiftshader|llvmpipe|software|basic render/i.test(s);
  if (a && !(a.info?.isFallbackAdapter ?? (a as any).isFallbackAdapter) && !soft(a.info?.architecture ?? '')) {
    return pickByLimits(a.limits, 'webgpu');                            // 判断 maxStorageBufferBindingSize、UA 是否为移动端等
  }
  const gl = document.createElement('canvas').getContext('webgl2');
  const r = gl?.getParameter(gl.getExtension('WEBGL_debug_renderer_info')?.UNMASKED_RENDERER_WEBGL ?? gl.RENDERER) ?? '';
  if (soft(r)) return TIERS.software;                                   // headless / 无 GPU 的机器
  return /Intel|Apple M|Mali|Adreno/i.test(r) ? TIERS.integrated : TIERS.desktop;
}
```

### 4.6 FPS 反馈控制律（点预算自动调节）

**建模**：在一定范围内，帧时间近似满足 `t ≈ a + b·N`。本机 SwiftShader 实测 `a≈3 ms`，`b≈1–3 µs/点`；独显约 0.001–0.003 µs/点。因此：
- 在**对数域**调 `B` 最自然：B 横跨 3 个数量级，乘性调整在各个量级上的步长是一致的。
- 前馈项可以根据模型直接跳到平衡点附近，避免 AIMD 从 2M 一步步降到 40k 所需的几十秒。

```ts
class BudgetController {
  B: number; tau: number; step = 0;                       // step：画质阶梯位置
  private buf = new Ring<number>(30); private rls = new RLS2(0.97);   // θ=[a,b]，φ=[1, N/1e6]
  private last = 0; private good = 0; private motionUntil = 0;
  constructor(private cfg: TierCfg) { this.B = cfg.B0; this.tau = cfg.tau; }

  observe(s: { frameMs: number; cpuMs: number; gpuMs?: number; N: number; pendingLoads: number; interacting: boolean }) {
    const work = s.gpuMs != null ? Math.max(s.cpuMs, s.gpuMs) : s.frameMs;   // 有 timer query 时用真实 GPU 时间
    this.buf.push(work);
    if (work < 3 * this.T) this.rls.update(s.N, work);                       // 剔除上传或 GC 导致的尖峰
    if (s.interacting) this.motionUntil = now() + 300;
    if (now() - this.last < 200) return; this.last = now();

    const t = this.buf.p75(); const r = t / this.T; let B = this.B;
    // (1) 前馈：模型可信时（样本数 > 60、b > 0、R² > 0.5），朝 B* 靠拢 30%（几何平均）
    if (this.rls.ok()) {
      const Bstar = clamp((this.cfg.headroom * this.T - this.rls.a) / this.rls.b, this.cfg.Bmin, this.cfg.Bmax);
      B = Math.exp(0.7 * Math.log(B) + 0.3 * Math.log(Bstar));
    }
    // (2) 反馈：对数域 AIMD，死区为 [0.8, 1.1]
    if (r > 1.10)      { B *= clamp(Math.pow(1 / r, 0.8), 0.5, 0.92); this.good = 0; }        // 按超载比例乘性下降
    else if (r < 0.80) { if (++this.good >= 5 && s.pendingLoads === 0) B *= 1.06; }         // 连续约 1 s 有余量：+6%
    else this.good = 0;
    // vsync 封顶（frameMs≈16.7 且拿不到 gpuMs）时无法观测余量：每秒 +2% 缓慢试探，超时即回退（类似 TCP 的探测）
    this.B = clamp(B, this.cfg.Bmin, this.cfg.Bmax);
    // (3) 画质阶梯：预算已触底仍超时 → 降一级；预算触顶且很空闲 → 升一级（迟滞 2 s）
    if (this.B === this.cfg.Bmin && r > 1.1) this.stepDown();   // EDL 关 → DPR×0.75 → sizeMode Full→Lite → 圆点→方点 → DPR×0.5
    else if (this.B === this.cfg.Bmax && r < 0.6) this.stepUp();
  }
  effectiveBudget(interacting: boolean) {                       // 交互降档：拖拽或飞行中用 0.6·B，τ×1.5
    return (interacting || now() < this.motionUntil) ? 0.6 * this.B : this.B;
  }
  get T() { return 1000 / this.cfg.targetFps; }
}
```

**RLS2 更新公式**（两参数递推最小二乘，遗忘因子 λ=0.97）：

```text
φ = [1, N/1e6]ᵀ ; k = Pφ/(λ + φᵀPφ) ; θ ← θ + k(t − φᵀθ) ; P ← (P − kφᵀP)/λ
```

`ok()` 的判据：样本数 > 60，且 `b > 1e−4 ms/百万点`。

**设计理由**：
- 用 p75 而不是均值，节点上传或 GC 造成的单帧尖峰不会触发降预算。
- 调整周期 200 ms，大约是上传和渲染生效所需的 10 帧。
- 死区 [0.8, 1.1] 加上“按比例乘性下降、固定 6% 上升”，结构和 TCP 的 AIMD 相同，收敛且不振荡。
- 交互期间降预算：拖拽时对细节不敏感，而对卡顿敏感。Cesium 也是同一思路：`Cesium3DTileset.js:85` 的 `cullRequestsWhileMoving` 在相机移动时不请求很可能用不上的瓦片。

**备选（PI，对数域）**：`u = ln B`，`e = ln(T*/t̄)`，`u ← u + Kp(e − e_prev) + Ki·e`，Kp=0.3，Ki=0.15（10 Hz 更新），加抗积分饱和。它的稳态更平滑，但阶跃响应慢于“前馈 + AIMD”，**不作为默认**。

### 4.7 渐进加载与过渡

1. **首屏**：
   - 读取 `metadata.json` 和 `hierarchy.bin`（≤ 1 MB）。
   - 发**一次** Range 请求取 `[0, levelsByteEnd[2])`（r09 实测 NY 为 L0–L2 共 181,872 点，DEFAULT 编码 3.27 MB）。
   - 在 Worker 中切片，立即显示。
   - 无头档预算 40k 时只显示 L0–L1 的一部分，但整个城市轮廓是完整的。
   - 目标：metadata 返回后 ≤ 1 s 出现首帧（TTFP）。
2. **细化**：相机静止后，目标集逐步填满（TTFD）。控制器在余量充足时逐步提高 B，画面表现为渐进细化。
3. **过渡**：
   - 节点层面：密度淡入和淡出（§4.4 的 `fadeCount`）。
   - 视角切换（Follow → FPV → Bird Eye）时，相机插值期间处于交互态，预算 ×0.6，结束后恢复。
   - 动效曲线统一使用 transitions.dev 的 easing 和时长，例如 standard 250 ms、emphasized 400 ms。
4. **加载进度**：`progress = |R ∩ resident| / |T|`，显示在 HUD 上，用 shadcn `Progress` 组件，不使用 emoji 或 spinner 图标。

### 4.8 渲染后端

**GlslBackend**（V0.1 主力，适用于 WebGL2 / 经典 `WebGLRenderer`，headless 可用）：
- 所有节点共享一个 `RawShaderMaterial`（GLSL3）。每个节点只有少量 uniform：`uNodeMin`、`uNodeSize`、`uNodeSpacing`、`uLevel`、`uChildDrawnMask`、`uVnStart`、`uPcIndex`，在 `onBeforeRender` 里写入并设置 `uniformsNeedUpdate`。
- 属性：
  - ANET_Q16：`a_pos: unorm16x4` + `a_col: unorm8x4`，两者都 `normalized=true`。
  - DEFAULT：`position f32x3` + `rgba u8x4`。
- 节点的 `Points` 平铺在同一个 `Group` 下（不像 potree-core 那样层层嵌套），`matrixAutoUpdate=false`，在节点创建时一次性写好 `matrixWorld`。
- 世界坐标用 ENU（米），场景 < 10 km。节点局部坐标加上 JS 双精度计算的 modelView，精度约 1 mm。
- 着色模式：
  - 高度渐变（科技灰阶：`#0B0D10 → #5B6570 → #C9CFD6 → #F4F6F8`）
  - 法线 Lambert，采用半球光
  - 强度
  - RGB（数据有颜色时）
  - LOD 调试
  - 选中 / 裁剪高亮用产品红 `#E93024`
- 雾：01-design §23 的 Fog Engine 通过 uniform 注入指数雾 `f = exp(−(d/visibility)²)`。`RawShaderMaterial` 不读 `scene.fog`，必须自己实现。
- **V0.3 优化**：节点超过 500 时，参考 `PotreeRenderer.renderNodes`，在一个自定义 `Object3D.onBeforeRender` 里直接对 GL 发出所有节点的 draw 调用，绕开 three 的逐对象开销。

**TslBackend**（V0.3 起，硬件 WebGPU）：
- 方案 A：**非实例化 vertex pulling**。几何体不带属性，`drawRange = 6·n`。`positionNode` 通过 `storage(buf).element(vertexIndex.div(6))` 取出点，解码 unorm16x4（位运算），再按 `vertexIndex % 6` 取 quad 的角，偏移量为 `pointSizePx · 2/viewport · clip.w`。实测软件渲染下只比 point-list 慢 2–4 倍，而 instanced 慢 30–80 倍。
- 方案 B：**1 px `point-list` + 屏幕空间膨胀**。点只写 1 px 的颜色和深度，随后用一个全屏 pass，在半径 r(px) 内取深度最近的邻居来填洞。这是 Potree-Next `dilateEnabled` 的思路。
- 默认用 A，B 作为高密度数据的实验项。
- 每节点参数放在 `uniform(...).onObjectUpdate(({object}) => …)`，或者放进节点记录 storage buffer，按 `nodeIndex` 索引。
- `visibleNodes` 用 `DataTexture` + `textureLoad`（TSL），可同时编译为 WGSL 和 GLSL。WebGPU-only 的场景下可以换成 storage buffer。

### 4.9 WebGPU 适配注意事项

1. **不能复用 GLSL 材质**：`WebGPURenderer` 不支持 `RawShaderMaterial` / `ShaderMaterial`。点材质、EDL、拾取都要用 TSL 重写，或者用 `wgslFn` 内嵌 WGSL。
2. **点只有 1 px**：必须采用 §4.8 的方案 A 或 B。**不要用 `Sprite` + `count` 的实例化方案**：SwiftShader 上慢 30–80 倍；真实 GPU 上 4 顶点的小实例 wave 利用率也差。
3. **WebGL2 回退不能用 `WebGPURenderer` 的 WebGL 后端**：`GLSLNodeBuilder.js:1702` 把 `gl_PointSize` 写死为 1.0，在这个后端上方案 A 也需要 storage buffer 的模拟。点云的回退路径应当是**经典 `WebGLRenderer` + GlslBackend**。其他图层（无人机、天气）需要同时兼容两种渲染器；或者在回退档里整个应用改用经典 `WebGLRenderer`，天气降级为 GLSL 版本。**这一点是全局架构决策，需要在系统架构说明书里明确。**
4. **顶点格式**：WebGPU 没有 `unorm16x3` 或 `unorm8x3`，只能用 x2 / x4，并且要 4 字节对齐。r09 的 ANET_Q16 已经按 x4 设计。
5. **没有 `onBeforeRender` + `uniformsNeedUpdate` 这套写法**：改用 `onObjectUpdate` 回调，或用 storage buffer 按节点索引。节点数 ≤ 2048 时，逐节点 draw 的开销可以接受。Chrome 的 `multi-draw-indirect` 仍是实验特性，不要依赖。
6. **回读是异步的**：`mapAsync` 意味着拾取、深度查询、截图都必须是 `async` API。WebGL2 端也按同样的方式封装（PBO + fence）。
7. **深度**：
   - 大场景（Shanghai 7.7 km）优先用 **reversed-Z**（three r186 的 `reversedDepthBuffer`），不用对数深度，因为对数深度会禁用 early-z。
   - EDL 从深度纹理读取，不要用 alpha 编码。
8. **计时**：
   - WebGPU 在 adapter 支持时启用 `timestamp-query`（SwiftShader adapter 暴露了 `chromium-experimental-timestamp-query-inside-passes`）。
   - WebGL2 用 `EXT_disjoint_timer_query_webgl2`（常被禁用）。
   - 拿不到时，控制器退回到帧时间模式（§4.6）。
9. **异步初始化和编译卡顿**：
   - 必须 `await renderer.init()`。
   - 首帧前调用 `renderer.compileAsync(scene, camera)` 预热管线，避免首次显示点云时卡顿 100–500 ms。
   - R3F v9 的 `<Canvas gl={async (props) => { const r = new WebGPURenderer(props); await r.init(); return r; }}>`。
10. **安全上下文与测试**：
    - `navigator.gpu` 只在安全上下文中存在。本机实测 `about:blank` 下拿不到，`file://` 或 `http://localhost` 可以。
    - CI 启动参数：`--enable-unsafe-webgpu --enable-features=Vulkan --use-webgpu-adapter=swiftshader`。

### 4.10 React / R3F / Zustand 集成与 UI 契约

```tsx
// apps/web/src/world/layers/PointCloudLayer.tsx
export function PointCloudLayer({ source, quality = 'auto' }: { source: string; quality?: QualityPreset }) {
  const { gl, scene } = useThree();
  const engine = useMemo(() => new PointCloudEngine({ renderer: gl, tier: useTier() }), [gl]);
  useEffect(() => { engine.addSource(source); scene.add(engine.group);
                    return () => { scene.remove(engine.group); engine.removeSource(source); }; }, [source]);
  useEffect(() => engine.setPreset(quality), [quality]);
  useEffect(() => engine.on('stats', throttle((s: EngineStats) => usePerfStore.setState(s), 250)), [engine]);
  useFrame(({ camera, size, viewport }, dt) => engine.update(camera, size, viewport.dpr, dt * 1000,
                                                              useSelection.getState().focusPosition), -1);
  useFrame(({ gl, scene, camera }) => engine.render(gl, scene, camera), 1);   // 接管渲染：可能跑 EDL 三遍
  return null;
}
```

- **热路径不经过 React**：`engine.update` 和 `engine.render` 都只操作 TS 对象；store 只接收 4 Hz 的统计数据。
- **与 UI 的约定**（左侧 WORLD 面板和底部 HUD，全部使用 shadcn 组件）：
  - `Select` 选择画质：Auto / 性能 / 均衡 / 画质。
  - `Slider` 调点预算（Auto 模式下只读，显示实时值）和点大小系数。
  - `ToggleGroup` 选着色模式：高度 / 法线 / 强度 / RGB / LOD 调试。
  - `Switch` 控制 EDL 和节点包围盒显示。
  - HUD 的 `Card` 里放 FPS、可见点数、预算、在途请求数、GPU 内存，配 lieflat 风格的 sparkline。
  - 所有图标使用 morphicons，不用 emoji。
- **R3F 的前提**：R3F 默认 `gl` 是 `WebGLRenderer`，所以 GlslBackend 开箱即用。换成 WebGPU 档时，通过上面的异步 `gl` 工厂切换，`PointCloudEngine` 根据 `renderer.isWebGPURenderer` 选择后端。

### 4.11 流畅性测试方案（headless）

- **驱动**：playwright-core + 本机 chromium（与本研究的脚本相同，见 `.cache/research/r12-bench/run*.js`）。启动参数：
  - WebGL2：默认即可。
  - WebGPU：见 §4.9 第 10 条。
- **场景**：`/world/newyork?bench=flythrough&tier=auto`。页面暴露 `window.__perf` 环形缓冲。录制一段 60 s 的相机路径，包括俯瞰、贴近街道、快速转身、跟随无人机，并同时开启 mock 的 10 架无人机和天气粒子。
- **指标**：
  - TTFP、TTFD。
  - 帧时间 p50、p95、p99，以及超过 250 ms 的帧数。
  - 预算轨迹与收敛时间。
  - `visiblePoints`、`residentPoints`、`gpuBytes`（`renderer.info.memory`）。
  - 请求数、失败数、取消数。
- **通过标准**：
  - 无头档：p95 ≤ 1.5·T*（T*=33–50 ms）。
  - 控制器在 3 s 内收敛到死区，且不振荡（B 在 10 s 内的变化系数 < 15%）。
  - 前 2 s 之后没有超过 250 ms 的帧。
  - 节点失败数为 0。
  - `residentPoints ≤ memFactor·B`。
- **回归基线**：保存每次 CI 的 p95 和收敛后的 B。机器负载会带来噪声（本机 load avg 12–18 时，吞吐波动达 3 倍），所以同时记录 `os.loadavg()`，只在负载 < 4 时做严格比对。

### 4.12 本机实测数据（SwiftShader，1280×720，load avg 12–18）

| 测试 | 结果 |
|---|---|
| WebGL2 空帧（clear + readPixels） | 2.5–2.8 ms |
| WebGL2 GL_POINTS，单次 draw | 10k：13 ms；25k：27–37 ms；50k：54–78 ms（高负载下 252 ms）；100k：111–202 ms；200k：211–653 ms；1M：2.2–3.2 s；4M：7.3–9.2 s |
| 点大小 1 / 2 / 4 px | 差异 ≤ 1.5 倍（瓶颈在顶点和装配，不在填充） |
| 500 次 draw 拆分 | 每次 draw 额外约 40–60 µs |
| WebGL2 2 px：points / instanced quad / 非实例化 6 顶点 quad | 50k：97 ms / **7.3 s** / 267 ms；100k：230 ms / **18.3 s** / 827 ms |
| WebGPU（SwiftShader adapter）point-list / instanced quad / vertex-pulling | 25k：20–28 ms / **0.8 s** / 90–122 ms；100k：126–323 ms / **4.5–5.3 s** / 463–550 ms |
| EDL 类全屏 pass（RGBA32F，8 tap） | 1280×720：107 ms；640×360：36 ms（4 tap：74 / 45 ms） |
| 能力 | `ALIASED_POINT_SIZE_RANGE=[1,1023]`，`MAX_TEXTURE_SIZE=8192`，`EXT_color_buffer_float` 支持；WebGPU adapter `{google, swiftshader}`，`maxStorageBufferBindingSize=1 GiB` |

---

## 5. 对比与推荐

| 维度 | potree-core | three-loader | potree |
|---|---|---|---|
| 嵌入普通 Three / React 场景 | ★★★★★（Object3D、`PotreeRenderer`、`RequestManager`） | ★★★★☆（Object3D，EDL 需要自己组装） | ★☆☆☆☆（全局变量加整站 viewer） |
| 2026 年活跃度 | ★★★★★（2026-09 仍有功能 PR） | ★★★☆☆（2025 年以 3DGS 为主，2026 年只有依赖更新） | ★★☆☆☆ |
| TS 类型质量 | ★★★☆☆（有 `@ts-ignore`，`load()` 返回 void） | ★★★★★ | —（JS） |
| 功能完整度（EDL、裁剪、拾取） | ★★★★☆ | ★★★☆☆ | ★★★★★（外加 HQ splats、剖面、DEM、密度修正） |
| 格式 | 1.x、2.0 DEFAULT / BROTLI | 1.x、2.0 DEFAULT、GLTF、GS | 最全（外加 EPT、COPC） |
| three 版本兼容 | 较好（peer 宽松） | 较差（`~0.160`） | 较差（r124） |
| 与本项目的契合度 | 最高（port 的主体） | 类型与 3DGS 参考 | 算法原典 |
| star | 255 | 285 | 5622 |

**推荐排序**：
1. **potree-core**：port 主体，dev 环境下 adopt 做交叉验证。
2. **three-loader**：取类型、错误语义、有界池，V1.0 参考 3DGS。
3. **potree**：取着色器原典、密度修正和 HQ 渲染。

整体结论：**不 adopt 任何一个作为生产依赖**。原因有四：
- WebGPU 不兼容。
- three 版本冲突。
- 存在 §2.5 列出的缺陷。
- 我们使用的是自定义的 ANET_Q16 编码。

需要移植的代码量约 2.5k 行 TS：
- LOD core 约 600 行
- IO 与 worker 约 700 行
- GLSL 材质约 800 行
- EDL 与拾取约 400 行

工作量约 1.5–2 人周，可控。

---

## 6. 风险与注意事项

1. **渲染器双轨的成本**（最高风险）：
   - 点云在 WebGL2 回退档必须走经典 `WebGLRenderer`，但天气粒子等模块原计划用 TSL compute。
   - 需要在架构层面决定：回退档整个应用用经典 `WebGLRenderer`，还是维护双后端。
   - **建议**：V0.1–V0.2 全部用经典 `WebGLRenderer`（点云、无人机、简单天气）。V0.3 起引入 WebGPU 档，并为 EnvironmentLayer 提供 GLSL 降级实现。
2. **软件渲染性能**：
   - SwiftShader 每秒只能处理约 0.3–1 M 点，CI 的画面只能是粗 LOD。
   - 性能测试必须以控制器平衡点为准，并在同一台机器上做相对比较。
   - EDL、圆点 `discard`、高 DPR 在无头档一律关闭。
3. **instancing 的性能陷阱**：three 官方的 WebGPU 点方案（`Sprite` 实例化）在软件渲染下慢约 50 倍，在部分 GPU 上也不理想。实现者看 three 的示例很容易照抄，需要在代码评审中把关。
4. **HTTP Range 与压缩的冲突**：
   - `Content-Encoding: gzip` 与 Range 请求一般不能同时使用（Range 作用在编码后的字节上）。
   - ANET_Q16 应当按节点做 gzip，写进 `octree.bin` 的是压缩后的字节，由 `byteSize` 指示长度；或者不压缩，依靠 HTTP/2 和 CDN。
   - Python `http.server` 不支持 Range。Starlette `FileResponse` 在新版本中支持 Range，但需要实测。
   - HTTP/1.1 下每个 host 最多 6 个连接，`maxInflight` 不要超过这个数。
5. **内存**：
   - three 上传后仍保留 CPU 数组，要用 `onUpload` 释放。
   - DEFAULT 解码后每点约 20+ B，是 ANET_Q16（12 B）的 1.7 倍。
   - 30 帧未进入目标集的请求要 abort，否则快速飞行时会堆积大量无用的在途请求。
6. **节点数和 draw call**：
   - SwiftShader 上每次 draw 约 50 µs，three 的逐对象 JS 开销约 10–30 µs。
   - 切片时叶节点不宜过小，r09 建议 leaf 为 20k、G=64。
   - 可见节点上限：无头档 128，桌面 2048。
7. **坐标与朝向**：
   - Potree 和 ENU 数据是 Z-up，three 是 Y-up。统一在 `source.transform` 里旋转，着色器里的高度一律用 ENU 的 z。potree-core 的 `getElevation()` 读的是 `world.z`，移植时要保持一致。
   - UrbanScene3D 中 Suzhou 为 Y-up、Chicago 单位疑似 km（r06、r09），必须在 ingest 阶段规范化，否则 τ 和点大小都会错一个数量级。
8. **精度**：场景 > 10 km 或使用 UTM 大坐标时，必须保证“节点局部坐标 + 双精度 modelView”，不能把世界坐标直接放进 float32 attribute。
9. **构建**：
   - potree-core 的 dist 用 `worker-loader` 把 worker 内联成 blob，CSP 需要允许 `worker-src blob:`。
   - three-loader 使用 webpack 的 `require('./x.worker.js').default`，不兼容 Vite。
   - 这也是选择 port 而非 adopt 的原因之一。
10. **许可**：BSD-2 / MIT，科研用途下没有顾虑。按用户要求忽略 license 限制，但源码头部仍保留原作者署名。
11. **测量噪声**：本研究的所有 SwiftShader 数据都是在 load avg 12–18 下测得的。r09 的 Potree 1.8 在 300k 预算下“可以加载”，不代表“流畅”。以本文 §4.12 为准，并在空闲机器上复测。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§14 点云 Web 渲染架构**：
   - “远处 10K 点 / 近处 1M 点”的说法不准确。Potree 系的 LOD 是**全屏共享的全局点预算**，加上**投影点间距**判据，不按距离分配固定点数。
   - 建议改写为：“浏览器维护一个全局点预算 B（桌面 1–3 M，无头 CI 约 40k），由 FPS 闭环自动调节；节点按投影点间距 `s_px ≥ τ` 细分，按屏幕中心和焦点加权排序，加法式 LOD，节点内随机点序支持部分绘制与密度淡入。”
   - 同时补充 §4.4 的每帧流程图。
2. **§15 数据格式**：
   - 把 “Binary Tile + Octree Index” 具体化为 **Potree 2.0 三文件容器 + `ANET_Q16` 编码**（r09），外加可选的 `hierarchy_ext.bin` tight bbox，通过 HTTP Range 访问。
   - 3D Tiles 只作为 V1.0 的导出格式，不作为内部格式。
3. **§9、§12、§34 WebGPU 定位**：需要补充三条限制：
   - WebGPU 点图元只有 1 px。
   - `WebGPURenderer` 的 WebGL2 后端把点大小写死为 1。
   - instancing 在软件和部分 GPU 上有性能陷阱。
   - “Fallback: WebGL2” 应明确为**经典 `WebGLRenderer` + GLSL 点材质**，并增加“渲染档位检测”（§4.5）这一小节。
4. **§34 前端栈里的 “Point Cloud: Custom Octree / Potree concepts”**：
   - 具体化为 `PointCloudEngine`：port 自 potree-core（MIT），修正 §2.5 的 15 项问题，提供 GLSL 和 TSL 双后端。
   - potree-core 只在开发交叉验证页中使用。
5. **§16 场景结构**：
   - 点云由引擎管理，放在独立 layer（EDL 需要）。
   - EnvironmentLayer 的雾和雨必须以 uniform 形式注入点云材质，因为点云材质不读 `scene.fog`。
   - 粒子在 EDL 合成之后绘制，保证点云与粒子的深度关系正确。
   - DebugLayer 增加 “LOD 着色 / 节点包围盒 / 预算 HUD”。
6. **§37 刷新率**：
   - “Web Rendering 60 FPS” 应改为**分档 SLO**：
     - 桌面：p95 ≤ 16.7 ms。
     - 集显：p95 ≤ 22 ms。
     - 无头 CI：p95 ≤ 50 ms（控制器平衡点）。
   - 同时加入 TTFP ≤ 1 s、TTFD ≤ 3 s，以及常驻内存上限。
7. **§38–40 UI / 交互**：
   - 增加画质预设、性能 HUD、着色模式（UrbanScene3D 没有 RGB，需要高度、法线、EDL）。
   - 相机模式切换期间自动进入交互降档。
   - Follow / FPV 模式启用**焦点加权**和**运动预测预取**（§3.2、§4.4），无人机高速飞行时前方点云能提前就绪。
8. **§8 Geometry World 与 Visual World**：
   - 明确“浏览器 LOD 点云只用于渲染”。
   - 碰撞和距离查询以服务端的 Geometry World 为准（r06 的 `RaycastingScene`）。
   - 前端的“地面高度 / 点选”只通过深度回读拾取得到，不在 JS 里遍历点。
9. **§43–44 MVP / V0.1**：链路里补上两项：
   - “UrbanScene3D ingest（轴向、单位、法线、伪彩）→ `worldpkg tile`”。
   - “headless 流畅性测试（§4.11）”。
   - 目的是让“极其流畅”成为可度量的验收项。
10. **路线（新增）**：
    - V0.1：GLSL 后端、Lite 自适应、EDL（仅高端档）、深度拾取、FPS 闭环。
    - V0.3：TSL 后端、Full 自适应、裁剪体、HQ 演示模式。
    - V1.0：参照 three-loader 与 spark 单元评估 3DGS LOD（Visual World 升级），研究 compute 光栅化（Potree-Next / Schütz 2021）。
