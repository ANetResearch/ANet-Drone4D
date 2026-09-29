# n01 研究笔记：2025–2026 Web 大规模点云 / 3DGS 渲染新项目发现（Discovery）

> 研究单元：n01（Discovery）｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §8（双表达）、§9–§16（Web 3D、WebGPU、点云渲染、数据格式、场景结构）、§34（前端栈）、§37（刷新率）、§38–§40（UI）、§41（World Package）、§43–§44（MVP / V0.1）
>
> 上游笔记：r09（PotreeConverter / `ANET_Q16` 切片）、r10（3D Tiles）、r11（three.js r186 WebGPU/TSL，点尺寸限制）、r12（potree-core / three-loader，`PointCloudEngine` 架构）、r13（Potree-Next / Spark / SuperSplat）。本文不重复这些笔记已有的结论，重点回答一个问题：**2025–2026 年新出现或仍然活跃的项目里，有哪些东西比 refs/ 里已有的仓库更好，应该替换或补进 `PointCloudEngine`？**
>
> **方法**
> - 中英文多轮 WebSearch，覆盖 WebGPU 点云、COPC、3D Tiles 点云、3DGS LoD 流式、Potree 替代品、loaders.gl、Rerun 等方向。
> - 每个候选都用 `curl` 抓 GitHub 页面取 star，用 `commits.atom` 取最近提交，脚本和原始结果在 `/data/projs/anet-drone/.cache/research/n01/`（`ghinfo.sh`、`stars*.tsv`）。GitHub API 只用来取仓库大小、创建日期和 license。
> - 挑出 6 组最有价值的候选，shallow clone 到 `refs/discovery/` 后精读源码。
> - 对本项目的 UrbanScene3D 数据做了一次**盒计数维度**实测（脚本 `boxcount.py`，结果 `boxcount.txt`）。
> - 另外读了 TU Wien 2025 年一篇 WebGPU 计算光栅化论文（`bauer2025.txt`）。
>
> **本地克隆**（只读，路径都相对各仓库根目录）：
>
> | 本地路径 `refs/discovery/…` | commit | 最近提交 | ★（实测） | License |
> |---|---|---|---|---|
> | `voxelkloud-view` / `-loader` / `-react` / `-core` / `-format-potree` / `-format-copc` / `-format-3dtiles` / `-wasm-build` / `-wasm-core` | `23fbd7f` / `07161b5` / `d296740` / `96e3e7e` / `20aa419` / `7e6b2f2` / `f4c4dff` / `dc4e04e` / `b0f4b4e` | 2026-08-24 至 2026-09-24 | 0（组织创建于 2026-08-22） | MIT |
> | `openlidarviewer` | `75599b4` | 2026-09-28 | 23 | AGPL-3.0（科研用途，按要求忽略） |
> | `3DTilesRendererJS` | `b70e594` | 2026-09-28 | 2,476 | Apache-2.0 |
> | `copc.js` | `9515f41` | 2026-08-10 | 63 | MIT |
> | `splat-transform`（PlayCanvas） | `0f60b17` | 2026-09-28 | 1,339 | MIT |
> | `playcanvas-engine`（sparse：`src/scene/gsplat*`、`src/framework/…`） | `99476ef` | 2026-09-28 | 16,944 | MIT |
> | `aholo-viewer` + `aholo-egs`（渲染内核子模块 manycoretech/egs） | `964dc18` + `6fa8506` | 2026-09-24 | 1,076（egs：19） | MIT |

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **voxelkloud**（view / loader / core / format-* / react；2026-08 新出现） | 基于 three ≥0.180 的 TS 点云渲染栈。提供三种光栅器：WebGPU compute 三遍软光栅、WebGL2 真 `gl_PointSize` 单缓冲单 draw、three 实例化四边形。另有两级预算 LOD 调度器、视频播放器式自动画质阶梯、流式取消与重试策略、octree-cut 局部点径，以及 Potree v2 / COPC / EPT / 3D Tiles 驱动 | **port（核心）**：`lod/select.ts`、`quality.ts`、`stream-policy.ts`、`cut.ts`、`sink-points.ts` 与 `points-glsl.ts`、`compute-wgsl.ts`、`BlockAllocator`、`SlotPool`，改写进 `PointCloudEngine`。**adopt（仅 dev）**：`@voxelkloud/react` 做对照页和性能基线 | V0.1（LOD、调度、GL 单 draw）→ V0.3（compute 光栅）→ V0.5（COPC） | ★★★★★（与本项目契合度最高，但仓库很新，只 port 不 adopt） |
| **NASA-AMMOS/3DTilesRendererJS** 0.5.3 | three.js、Babylon、r3f 通用的 3D Tiles 渲染器。2026-09-18 新增 `PotreePlugin`（把 Potree v1/v2 映射成合成的 ADD tileset）和 `PointCloudEffectsPlugin`；核心层 `core/renderer` 与渲染引擎无关 | **adopt**：V0.8 起用于 3D Tiles 地形、倾斜、Google 3D Tiles 等网格瓦片。**reference**：SSE 公式、LRU（字节上下限）、下载与解析优先队列、`TilesFadePlugin`。**skip**：它的点云材质（基于经典 WebGLRenderer 的 `PointsMaterial` 补丁） | V0.8（GIS 底座）/ V0.1（公式对齐） | ★★★★☆ |
| **Aurtechmx/openlidarviewer**（OLV，2026-06 新出现） | 纯浏览器 LiDAR 查看器，WebGPU 优先、WebGL2 回退。COPC、EPT、3D Tiles 流式，OPFS 外存建索引，工程上约束很严 | **port（策略）**：`frameBudgetGovernor`（负载归一化 + 滞回）、`adaptiveDpr`（按角速度降 DPR）、`fadeDither`（Weyl 抖动淡入）、`evictionPolicy`（1.5/1.15 滞回 + 1 s 驻留保护）、`renderBackendChoice`（真实探测 adapter）、`depthCapForVelocity`。**reference**：OPFS 外存索引管线。**skip**：它的实例化 sprite 渲染器 | V0.1–V0.3 | ★★★★ |
| **PlayCanvas splat-transform 3.7 + engine `gsplat-unified`** | 3DGS LOD 离线构建（Streamed SOG，`lod-meta.json`），引擎侧跨实例“性价比”预算分配器 | **port**：`GSplatBudgetBalancer`（按覆盖率 × 误差下降 / 代价的贪心背包，512 个对数桶），用于点云瓦片、多城市、splat 之间的**全局预算仲裁**。**adopt**：`@playcanvas/splat-transform` CLI 用于 V0.8 离线构建 splat LOD | V0.6（仲裁）/ V0.8（3DGS） | ★★★★ |
| **connormanning/copc.js**（+ hobuinc/laz-perf） | COPC 读取的事实标准：`Copc.create` 读 header 与 VLR，`Hierarchy.load` 分页读层级，`loadPointDataView` 用 laz-perf 解压 | **adopt**：在 worker 里读 COPC（World Package 的归档与交换格式、用户拖入 COPC URL） | V0.5 | ★★★☆ |
| **manycoretech/aholo-viewer**（2026-05 开源） | 群核的 3DGS 与 Mesh 渲染器，自研 egs 引擎（WebGL2）。chunk 级 LOD、官方称支持 10 亿 splat，带体素碰撞 | **reference**：chunk LOD 的“距离分档 + 滞回 tick + 调度节流 + 相邻 chunk 合并 draw” | V0.8 | ★★★ |
| loaders.gl `modules/copc`（v5.0.0-alpha.6，npm 未发布） | COPC TileSource：Arrow 输出，并发信号量，Range 缓存 | **reference**：作为 copc.js 的替代实现对照 | V0.5+ | ★★★ |
| Rerun web viewer（rerun-io/rerun ★11,503，`@rerun-io/web-viewer` 0.38.1） | 机器人多模态日志的 Web 查看器（wasm），提供 React 封装 | **adopt（可选，仅调试）**：后端仿真日志和轨迹的离线复盘；不能用作主沙盘 | V0.2+（dev） | ★★★ |
| Visionary（★526，2025-12 创建，最近推送 2026-06） | WebGPU 3DGS/4DGS 平台，内置 ONNX Runtime，提供“Gaussian Generator 契约”和网格–高斯混合深度合成 | **reference**：V1.0 动态高斯的接口形态 | V1.0 | ★★☆ |
| LidarScout（cg-tuwien，HPG 2025，★27） | 不做预处理、直接对海量 LAZ 做外存浏览：先读稀疏子采样，再做高度图重建（CUDA 桌面程序） | **reference**：真机 LAZ 的“秒开预览”思路 | V0.5 | ★★ |
| 其余（Babylon.js GS 流式、Babylon-Lite、Reall3dViewer、GaussianSplats3D、gsplat.js、web-splat、three-loader-3dtiles、iTowns/Giro3D、CloudAnalyzer、vtk-js 等） | 见 §1 | **skip / reference** | — | ★–★★ |

**实现者先读这 12 条（每条都有源码或实测依据）**

1. **`PointCloudEngine` 的 Selector 换成 voxelkloud 的“两级预算 best-first”**（`voxelkloud-view/src/lod/select.ts::selectVisible`）。它修正了 Potree 系参考实现的 6 个问题：
   - 用 `Number.MAX_VALUE` 做哨兵，导致包含相机的节点排序并列；
   - 预算不够时 `break`（参考实现在 autzen 上实测丢掉 191 个已入堆节点）；
   - pop 时才做视锥裁剪；
   - near/far 在帧尾写回，形成振荡反馈；
   - 阈值按 CSS 像素计，在 2x 屏上阈值被悄悄减半；
   - `numPoints` 被当成子树总数。

   它还额外输出 `limitedBy` 和 `achievedScreenError` 两个遥测量，可以直接接到性能 HUD（§3.1）。
2. **预算分两级**：
   - 投影误差 ≥ τ 的节点是“要求的质量”，可以花满 B；
   - 投影误差 < τ 的节点是“奖励细化”，最多花到 B·(1−h)（h=0.15），并且最多比 τ 再深 2 级（下限 τ/4）；
   - 留出的 15% 用来吸收下一次相机移动，避免“移动 → 驱逐 → 重载”（§3.1）。
3. **点云 LOD 与 3D Tiles 的 SSE 是同一个量**：`sse_px = g·H_dev / (2·tan(fovY/2)·d)`。3DTilesRendererJS 的 `calculateTileViewError` 和 voxelkloud 的 `screenErrorPx` 代数上完全等价（§3.2）。点八叉树取 g = spacing_L，τ=1.35 px（voxelkloud 标定值；3DTilesRendererJS 的 `PotreePlugin` 取 `errorTarget=1`）。网格瓦片取 τ=16 px。**全系统统一用设备像素。**
4. **点径应由“选中 cut 在该位置的最深层级”决定，而不是由点所属节点的层级决定。**
   - 否则父层的点会比已驻留的细层宽 2^(D−L) 倍，把细节糊掉。
   - 做法：在 vertex 或 compute 阶段沿 BFS 编码的 cut 纹理（每项 4 B：`childMask` + 24 位首子偏移）走到最深的**已驻留**节点。
   - 缩小幅度**最多 1 级**：实测放开到全级时覆盖率会随加载**下降** 5.4%，封顶 1 级后只下降 0.7%，Speed Index 从 5083 降到 1918（§3.3）。
   - 3DTilesRendererJS 2026-09 的 `PotreePlugin` 采用了同一思路（`_updateActiveNodesTexture`）。
5. **WebGL2 档用一个大缓冲、一次 draw。** 用 `gl.POINTS` 加真实的 `gl_PointSize`，用 liveness 纹理把未选中节点的点推出裁剪空间。Potree 每个节点一个 `THREE.Points`，3M 点时约 2000 个 draw。voxelkloud 自报 3M 预算下 INP 为 56–72 ms，Potree 1.8 为 88–104 ms，three 实例化四边形为 656 ms（仅 7.5 fps）。r11/r12 实测过的“WebGPURenderer 点只能 1 px”的问题，在这一档用“经典 `WebGLRenderer` + RawShaderMaterial”解决（§3.4）。
6. **WebGPU 档的默认方案改为 compute 三遍软光栅**：`atomicMin(depth)` → `atomicAdd(rgb·w, w)` → 全屏 resolve，EDL 并进 resolve。
   - 这取代了 r12 的“vertex-pulling quad”方案。
   - voxelkloud 的 compute 路径直接写 swapchain，因此不能和 three 场景组合。
   - **我们的改法**：用 three r186 的 TSL `atomicMin/atomicAdd` 和 `renderer.compute()` 移植前两遍；resolve 用一个全屏 `NodeMaterial` 网格，**通过 `material.depthNode` 写深度**。这样无人机、天气、任务图层可以正常做深度测试（§3.8）。
7. **自动画质按“视频播放器”方式调**（`quality.ts::QualityController`）：
   - 只用 5 个离散档位（renderScale、pointBudget、τ 联动），**一次只移动一档**；
   - 以“相邻两次呈现的帧间隔”的 p50/p95 相对于**估计刷新周期**做判断，不看 frame time（被 vsync 钉死）；
   - 下降快：p50 > 1.35T；上升慢：p95 < 1.1T 且已稳定 5 s，每次“升档后又掉档”会让下次升档的等待时间翻倍，最长 120 s；
   - 解码积压期间不采样。

   本项目在它下方再加两个软件档，供 SwiftShader 和 CI 使用（§3.6）。
8. **“运动时缩小选择集”不能降低交互延迟（INP）**：voxelkloud 实测 INP 没有变化。原因是它的 arena 只把点**遮蔽**而不**压缩**，顶点工作量不变。真正起作用的是“驻留且被绘制”的点数。所以运动降载必须作用在 **draw range 或 compute dispatch 数**上（compact 分派，§3.8.3），或者作用在 DPR 上（OLV 的 `adaptiveDpr`）。
9. **流式调度的五条硬规则**：
   - 首个节点落地前并发宽度为 1，之后为 12（首像素时间 1224 → 866 ms）；
   - 离开视锥满 2 帧就取消请求；“被取代”的请求只在队列饱和且超过 8 帧后才取消（默认关闭）；
   - 失败重试间隔 30·4^(n−1) 帧，最多 3 次；
   - 每帧上传不超过 8 MiB，按 `lastSeen` 排序；
   - 驱逐**永远不碰本帧选择集和根节点**，驱逐到上限的 0.85 为止。

   OLV 另加两条：驱逐从 1.5×budget 开始、释放到 1.15×budget；可见节点有 1 s 驻留保护，但对“被更细层取代”的节点无效，否则 LOD 会冻结（§3.5）。
10. **多个点云源（多城市、多瓦片、splat）共享一个预算时，用 PlayCanvas 的性价比分配器**：每个节点从最粗层起步，按 `coverage·Δerror/Δcount` 从高到低“购买”单级升级，遇到第一个放不下的就停止（不跳过，避免闪烁）。用 512 个 float-bit 对数桶实现 O(N)，不需要堆（§3.9）。
11. **UrbanScene3D 实测**：盒计数维度 D 在 2.0–2.4 之间（立面让 D 超过 2）。
    - 每下降一级，点数约 ×4–5，锐度只 ×2。
    - 每城 5 M 点约 5 层就饱和（Potree spacing = cube/128）。
    - 在独显上，**单城 5 M 点整体放得进 6 M 预算**。
    - 因此“渐进加载 + 疏密调节”真正要应对的是 SwiftShader/集显、多城市拼接和 FPV 近景，预算表见 §3.12。
12. **World Package 的点云格式**：
    - 运行时仍用 **Potree 2.0 兼容格式**（r09 的 `ANET_Q16` 作为扩展编码），这样 voxelkloud、3DTilesRendererJS `PotreePlugin`、potree-core 三个现成查看器都能拿来当**正确性和性能的对照**；
    - 归档和交换格式定为 **COPC**（LAS 1.4，PDAL/untwine 写，copc.js 读）；
    - `hierarchy.bin` 用一次**不带 Range** 的 GET 整体拉取：206 响应不会被 CDN 压缩，200 可以（autzen 100,518 B → brotli 38,279 B）。

---

## 1. 仓库概览

### 1.1 候选全表（star 与最近提交都是 2026-09-28 实测）

| 分组 | 仓库 | ★ | 最近提交 | 创建 | 语言 / 许可 | 一句话 | 结论 |
|---|---|---|---|---|---|---|---|
| A. Web 点云渲染器 | **voxelkloud/view**（+ loader/core/react/vue/format-*/wasm-*，共 16 个仓库） | 0 | 2026-09-25 | 2026-08-22 | TS+Rust / MIT | three ≥0.180 的 WebGPU/WebGL2 点云栈，npm `@voxelkloud/view@0.8.0` | **port** |
| A | **Aurtechmx/openlidarviewer** | 23 | 2026-09-28 | 2026-06-03 | TS / AGPL-3.0 | 浏览器 LiDAR 工作台，WebGPU + WebGL2，COPC/EPT/3D Tiles 流式，OPFS 外存索引 | **port（策略）** |
| A | m-schuetz/Potree-Next（已在 refs） | 124 | 2025-10-07 | — | JS / BSD-2 | WebGPU 版 Potree 原型 | 保持 r13 结论 |
| A | davbau/Potree-Next（fork，TU Wien 2025 学士论文） | 0 | 2026-09-27 | — | JS | WebGPU compute 光栅 + 10/10/10 位分级精度坐标 | reference（§3.8.4） |
| A | m-schuetz/compute_rasterizer | 749 | 2023-02-02 | — | C++/CUDA/GL | 计算着色器光栅（桌面） | reference（不活跃） |
| A | m-schuetz/SimLOD | 520 | 2024-04-07 | — | CUDA | 边加载边建 LOD（桌面） | reference（不活跃） |
| A | cg-tuwien/lidarscout | 27 | 2026-07-27 | — | C++/CUDA | HPG 2025，免预处理外存 LAZ 浏览 | reference |
| A | rsasaki0109/CloudAnalyzer | 14 | 2026-09-28 | — | Rust+wasm+three | 浏览器点云分析（C2C、M3C2、ICP），COPC 按层读取 | reference（V0.5 QA） |
| A | Kitware/vtk-js | 1,534 | 2026-09-26 | — | JS | 科学可视化，有 WebGPU mapper | skip |
| A | iTowns/itowns | 1,272 | 2026-09-14 | — | JS / three WebGL | GIS 框架，支持 Potree/COPC/3D Tiles | skip（与 Cesium 路线重叠） |
| B. 格式、解码、流式 | **connormanning/copc.js** | 63 | 2026-08-10 | 2021 | TS / MIT | COPC 读取，npm `copc@0.0.9` | **adopt** |
| B | hobuinc/laz-perf | 103 | 2026-09-05 | — | C++→wasm | LAZ 解压（copc.js、loaders.gl、OLV 都依赖它，npm `laz-perf@0.0.7`） | adopt（间接） |
| B | hobuinc/untwine | 78 | 2026-08-14 | — | C++ | PDAL 系 COPC 生成器 | adopt（离线，V0.5） |
| B | visgl/loaders.gl（`modules/copc` v5 alpha） | 855 | 2026-09-26 | 2018 | TS | COPC TileSource 与 Arrow 输出；`@loaders.gl/copc` 尚未发布到 npm | reference |
| B | 360-geo/copc | 3 | 2026-07-17 | — | Rust | 带时间索引的 COPC 流式读取 | skip |
| C. 3D Tiles | **NASA-AMMOS/3DTilesRendererJS** | 2,476 | 2026-09-28 | 2020 | JS / Apache-2.0 | three/Babylon/r3f 的 3D Tiles 渲染器，npm `3d-tiles-renderer@0.5.3` | **adopt（V0.8）** |
| C | nytimes/three-loader-3dtiles | 538 | 2024-10-15 | — | TS | 基于 loaders.gl 的 3D Tiles 加载器 | skip（停更） |
| C | WilliamLiu-1997/3D-Tiles-RendererJS-3DGS-Plugin | 128 | 2026-09-09 | — | TS | 3D Tiles 中的 splat 内容交给 Spark 渲染 | reference（V0.8） |
| C | WilliamLiu-1997/3DTiles-Inspector | 24 | 2026-08-31 | — | TS | 3D Tiles 对齐、几何误差、splat 裁剪编辑器 | reference |
| C | bhouston/3d-tiles-splat-test、vinneyto/3dgs_tile_webgpu | 0 / 0 | 2026-09 | — | — | 3DGS tileset 与 three `GaussianSplat` 的试验页 | skip |
| D. 3DGS LoD 流式 | sparkjsdev/spark（已在 refs，2.x 于 2026-04-15 发布） | 3,662 | 2026-09-25 | — | TS+Rust | `.RAD` 流式格式、LoD splat tree、虚拟分页 | 保持 r13 结论 |
| D | **playcanvas/splat-transform** | 1,339 | 2026-09-28 | 2024-12 | TS / MIT | splat 转换与 LOD（Streamed SOG），npm 3.7.0 | **adopt（V0.8）** |
| D | **playcanvas/engine**（`gsplat-unified`） | 16,944 | 2026-09-28 | 2014 | JS / MIT | 统一 splat 渲染、LOD 流式、预算分配 | **port（分配器）** |
| D | playcanvas/supersplat（编辑器） | 10,270 | 2026-09-23 | — | TS | splat 编辑器 | skip |
| D | **manycoretech/aholo-viewer** | 1,076 | 2026-09-24 | 2026-05-12 | TS / MIT | chunk LOD，官方称 10 亿 splat | reference |
| D | reall3d-com/Reall3dViewer | 512 | 2026-09-11 | — | TS / three | three 上的 3DGS 渲染器，大场景 LOD | skip |
| D | mkkellogg/GaussianSplats3D | 2,900 | 2025-10-19 | — | JS / three | 老牌 three 3DGS | skip（已被 Spark 取代） |
| D | huggingface/gsplat.js | 1,667 | 2026-09-23 | — | TS | 自带引擎的 splat 库 | skip |
| D | KeKsBoTer/web-splat | 302 | 2026-03-27 | — | Rust wgpu | WebGPU 3DGS | skip |
| D | Scthe/gaussian-splatting-webgpu | 39 | 2024-06-05 | — | TS | WebGPU 3DGS 教学实现 | skip |
| D | nianticlabs/spz | 924 | 2026-08-05 | — | C++ | SPZ 压缩格式，被 glTF `KHR_gaussian_splatting_compression_spz` 采用 | reference（V0.8 格式） |
| D | BabylonJS/Babylon.js（GS streaming）/ Babylon-Lite | 26,114 / 154 | 2026-09-28 / 2026-09-25 | — | TS | Babylon 8 的 GS 流式 LoD；Babylon-Lite 是只支持 WebGPU 的精简引擎 | skip（引擎不同） |
| D | Visionary-Laboratory/visionary | 526 | 2026-04-17（pushed 06-26） | 2025-12-07 | Python/TS / Apache-2.0 | WebGPU 3DGS/4DGS 加 ONNX 逐帧推理 | reference（V1.0） |
| E. 调试与数据平台 | rerun-io/rerun | 11,503 | 2026-09-27 | — | Rust / MIT+Apache | 机器人数据可视化，Web 版为 wasm | adopt（仅调试，可选） |
| E | pygfx/pygfx | 877 | 2026-09-07 | — | Python wgpu | Python 端的 WebGPU 渲染引擎 | skip |

> 说明：voxelkloud 只有 0 star，但它 2026-08 才出现，代码和 `CHANGELOG.md` 里有大量带测量的设计记录，和本项目栈（three r18x + TSL + React）几乎一一对应，所以按“契合度优先”排在第一。仓库还很年轻，**只 port 算法，不把运行时依赖放进主产品**（见 §6）。

### 1.2 与 refs/ 已有同类仓库的关系

| 已有（refs） | 新发现 | 判断 |
|---|---|---|
| `web3d/potree-core`、`web3d/three-loader`（r12） | voxelkloud | **补充并部分替换**。LOD 核心、调度、驱逐改按 voxelkloud 实现（它是在逐条修正 Potree 参考实现的缺陷）；potree-core 保留为交叉验证页（r12 原结论） |
| `web3d/Potree-Next`（r13） | voxelkloud compute 路径、Bauer 2025 论文 | **补充**。WebGPU 光栅以 voxelkloud 的三遍法为蓝本；Potree-Next 的遍历不看点预算（r13 实测），不采用它的遍历 |
| `web3d/potree`（r12） | 3DTilesRendererJS `PotreePlugin` | **补充**。第三个 Potree v2 对照实现，SSE 口径与 3D Tiles 一致 |
| `web3d/cesium`、`web3d/deck.gl`（r15） | 3DTilesRendererJS | **补充**。在 three 主栈内接 3D Tiles 用 3DTilesRendererJS；Cesium 保留为独立的 GIS 路由 |
| `web3d/spark`、`web3d/supersplat-viewer`（r13） | splat-transform、gsplat-unified、Aholo、Cesium 3DGS（2026-04） | **补充**。3DGS 的离线 LOD 构建与预算分配；互操作格式用 3D Tiles + `KHR_gaussian_splatting` |
| `world/PotreeConverter`、`world/PDAL`（r09） | copc.js、untwine、voxelkloud `wasm-build` | **补充**。COPC 作为归档格式；`wasm-build` 可以在浏览器里把 LAS/LAZ/PLY/PCD/XYZ 建成八叉树（上限 20 M 点） |

---

## 2. 源码结构与关键模块

### 2.1 voxelkloud（重点精读）

**包结构**：

| 包 | 作用 | 依赖 |
|---|---|---|
| `@voxelkloud/core` | 包围盒、属性、传输层、错误类型、Morton 编码、`paged-octree.ts` | 无 |
| `@voxelkloud/loader` | 格式注册表（`registry.ts`），按 URL 选择驱动。不依赖 three 和 DOM，可以放进 worker | core |
| `format-potree` | `metadata.json` / `hierarchy.bin`（每条 22 B）/ `octree.bin`，支持 BROTLI 解码 | core |
| `format-copc` | Range 读取（`range.ts`），解码 worker 池（`decode-pool.ts`） | core |
| `format-ept` / `format-3dtiles` | 3D Tiles 驱动含 `implicit.ts`、`subtree.ts`、`pnts.ts` | core |
| `format-single` | 无索引的单个 LAS/LAZ | core |
| `wasm-core` | LOD 内核的 Rust 版本：视锥提取、AABB 分类、SSE，与 TS 实现做差分测试 | — |
| `wasm-codecs` | laz-rs 编译的 wasm | — |
| `wasm-proj` | proj4rs 编译的 wasm | — |
| `wasm-build` | 浏览器内建八叉树并输出 COPC | — |
| `@voxelkloud/view` | 渲染器 | core + loader，peer three ^0.180 |
| `@voxelkloud/react` | `<PointCloudViewer/>` 与 `usePointCloud` | view |

**`voxelkloud-view/src` 关键文件**（22k 行，含测试）：

| 文件 | 行数 | 关键符号 / 作用 |
|---|---|---|
| `lod/select.ts` | 717 | `selectVisible`（两级预算 best-first）、`resolveLodOptions`、`LodSelection.limitedBy/achievedScreenError/levelCounts`、`BONUS_LEVELS=2`、`DEFAULT_SCREEN_ERROR=1.35`；可选 `LodKernels`（wasm，每 8 个子节点跨一次边界） |
| `lod/metric.ts` | — | `projectionFactorPerspective`、`screenErrorPx`、`suggestNearFar`、`estimateFragments` |
| `lod/heap.ts` | — | 用并行 TypedArray 实现的最大堆（`heapNode/heapKey/heapContainment`），pop 时零分配 |
| `lod/frustum.ts` | 172 | `extractFrustumPlanes`、`classifyAabb`（三态：Inside / Intersecting / Outside）、`intersectsAabb` |
| `quality.ts` | 406 | `QUALITY_LEVELS`（5 档）、`initialQualityIndex(DeviceProfile)`、`QualityController.sample(intervalMs, now)`、`ceilingForOptions`、`AUTO_CEILING_INDEX=3` |
| `stream-policy.ts` | — | `shouldAbortFetch`、`ABORT_OUTSIDE_FRAMES=2`、`ABORT_STALE_FRAMES=8`、`MAX_LOAD_ATTEMPTS=3`、`retryDelayFrames` |
| `cut.ts` | 268 | `OctreeCut.build`：BFS 编码，每项 4 B（`mask, first>>16, first>>8, first`），宽 1024 的 RGBA8 纹理，`MAX_CUT_DEPTH=20`（float32 精度所限） |
| `sink-points.ts` + `points-glsl.ts` | 896 + 308 | WebGL2 光栅器：单缓冲（pos f32×3、color u8×4、pitch f32、meta u32，共 24 B/点）、`vertexAttribIPointer` 读 `aMeta`、liveness 纹理 `R8UI`、容量翻倍（先驱逐，再扩容）、shader 预热 |
| `sink-compute.ts` + `compute-wgsl.ts` | 1680 + 618 | WebGPU 光栅器：`clearPass`、`depthPass(+Compact)`、`colorPass(+Compact)`、`vsResolve/fsResolve`；`BlockAllocator`（首次适配、相邻合并、归还尾部）、`SlotPool`（LIFO 复用，`DEAD_SLOT` 哨兵）、`packNodeMeta`（level 8 位、slot 16 位、class 8 位）、`MAX_SLOTS=65536` |
| `arena.ts` / `sink-arena.ts` | 490 / 212 | three 实例化四边形路径（仅用于组合） |
| `overlay.ts` | 1139 | compute 路径的网格叠加：网格的片元读取点深度缓冲后自行 discard |
| `view.ts` | 3231 | `createPointCloudView`：`stream()`、`drainPending()`、`evict()`、`retryOrFail()`、运动检测、auto quality 接线、设备丢失诊断 |
| `replace.ts` | — | `filterReplacedParents`：3D Tiles REPLACE 细化，只在子节点全部选中且驻留后才隐藏父节点 |
| `capacity.ts` | — | `initialCapacity`：按本云点数截断预留，不再固定按 budget×slack 预留 |
| `gpu-timing.ts`、`pick.ts`、`edl.ts`、`ortho.ts`、`ground-level.ts` | — | GPU 计时、拾取、EDL、正交俯视、地面高度取百分位 |

**默认参数**（`view.ts` L986–993、`select.ts`、`material-options.ts`）：

| 参数 | 默认值 |
|---|---|
| `pointBudget` | 3,000,000 |
| `targetScreenError` | 1.35 px（设备像素） |
| `maxNodes` | 4096 |
| `maxBudgetSkips` | 32 |
| `budgetHeadroom` | 0.15 |
| `minScreenError` | τ/4 |
| `maxConcurrentLoads` | 12 |
| `maxResidentBytes` | 512 MiB |
| `maxAttachBytesPerFrame` | 8 MiB |
| `abortOutsideFrustum` / `abortSuperseded` | true / false |
| `movingBudgetScale` | 1（实测无效，默认关闭） |
| `movingSettleFrames` | 6 |
| `sizeMultiplier` | 1 |
| `minPixelSize` / `maxPixelSize` | 1 / 8 |

**光栅器选择**（README，“Three rasterisers”一节）：
- `sinkMode:"auto"` 的顺序是：WebGPU 上用 compute，WebGL2 上用 points，两者都不可用才用实例化 arena。`view.rasterizer` 会报告实际用了哪一个。
- compute 和 points 两条路径**自己打开 device 或 GL2 context，直接写 swapchain，不经过 three 渲染**。好处是快（`renderer.init()` 的 27 ms 变成 3 ms，主 chunk 从 279.7 kB gzip 降到 118.7 kB）。代价是不能和 three 场景图组合。
- 需要和 gizmo、mesh 互相遮挡时，只能用 arena。在 compute 路径上，作者另写了 `overlay.ts`：网格的片元读取点的深度缓冲，自行判断遮挡。

### 2.2 OpenLiDARViewer（OLV）

**规模与工程约束**：
- 依赖 `three@^0.186`、`@loaders.gl/*@4.5.2`、`laz-perf@0.0.7`；
- 有 `validation/`、`benchmarks/`、Playwright e2e、突变测试（stryker）、bundle 体积预算；
- 版本 0.7.0-alpha.1；
- 渲染用 `THREE.WebGPURenderer` 加 `PointsNodeMaterial` 实例化 sprite（`src/render/viewerRenderBootstrap.ts`、`densityPointSize.ts`），也就是 voxelkloud 测出最慢的那条路。

**值得 port 的“策略层”**（都是纯函数，都有 Node 单测）：

| 文件 | 内容 |
|---|---|
| `render/perf/frameBudgetGovernor.ts` | `frameLoad`：`load = clamp01(max((med−T)/(2T), 0.5·(high−T)/(2T)))`；`SWITCH_THRESHOLDS`：EDL 0.45 关 / 0.25 开，continuity 0.30/0.15，detailedHover 0.55/0.35；`RENDER_SCALE_FLOOR=0.6`、`RENDER_SCALE_STEP=0.2`、`POINT_FRACTION_FLOOR=0.4`、`MIN_COMMIT_SCALE=0.1`；运动压力：桌面 0.25、移动端 0.5；`dprPressure` 以 0.25 为步长向上量化 |
| `render/adaptiveDpr.ts` | `targetPixelRatio`：运动时从 `0.85·maxDpr` 线性降到下限 1.0，角速度 1.2 rad/s 时降到底；按 0.25 量化；降档限速 250 ms，升档立即生效 |
| `render/streaming/fadeDither.ts` | 屏幕门溶解：`keep = fract(i·0.618034) ≤ progress`。点保持不透明，避免透明排序和 z-fighting，EDL 深度仍然精确 |
| `render/streaming/evictionPolicy.ts` | `triggerRatio=1.5`、`releaseRatio=1.15`、`minVisibleDwellMs=1000`；排序：视锥外 → 视锥内但未选中 → 已被更细层取代 → 正在看的；**被取代的节点不享受驻留保护**，防止 LOD 冻结 |
| `render/streaming/streamingScore.ts` | `nodeScore = (depthCap−depth+1)·1000 + min(round(ps·1000·focus), 999)`，严格由粗到细；`depthCapForVelocity`：速度 > 10 时 −3 级（最低 3），> 50 时 −6 级（最低 2）；`projectedBoxCenterWeight` 做视野中心偏置 |
| `render/streaming/streamingBudget.ts` | 桌面 1.5 M / 2.5 M / 8 M，移动端 0.6 M / 1.2 M / 2 M（low / balanced / high） |
| `render/renderBackendChoice.ts` | 真正调用 `requestAdapter()` 再决定 `forceWebGL`。WebKit 上 `navigator.gpu` 存在但 adapter 为 null，会导致崩溃 |
| `docs/architecture/heavy-cloud-native.md` + `src/io/heavy/` | 无索引的大 LAS/LAZ：解析 chunk table，多 worker 并行解压（8 M 点 LAS 1.4：2780 → 864 ms，4 worker），两遍外存建八叉树并溢写到 OPFS，再作为 `OlvTileSource` 流式浏览；驻留上限 1.5 × pointBudget |

### 2.3 3DTilesRendererJS 0.5.3

**核心层 `src/core/renderer/`（与渲染引擎无关）**：
- `tiles/TilesRendererBase.js`：遍历、`calculateTileViewError` 插件链、`errorTarget=16`、`maxDepth=∞`。
- `utilities/LRUCache.js`：`minSize=6000`、`maxSize=8000`、`minBytesSize=0.3 GB`、`maxBytesSize=0.4 GB`、`unloadPercent=0.05`。
- `DownloadPriorityQueue`（`maxJobsPerOrigin=25`）、parse 队列（`maxJobs=5`）、node 队列（`maxJobs=25`）。

**three 层 `src/three/renderer/tiles/TilesRenderer.js`**：
- `sseDenominator = (2/P[5]) / height`（L561）；
- `error = geometricError / (distance·sseDenominator)`（L1062）；
- 正交相机时 `error = geometricError / pixelSize`。

**2026-09-18 新增插件**：
- `plugins/potree/PotreePlugin.js`（420 行）+ `PotreeLoader.js`（479 行）：
  - 根节点 `geometricError = spacing`，子节点逐级 /2，`refine:"ADD"`；
  - `useRecommendedSettings` 时 `tiles.errorTarget = 1`；
  - 点大小为 `spacing × 1.7 × pointScale`（世界单位）；
  - `_updateActiveNodesTexture()`：把活动节点按“层级 + key”排序后写入 RGBA8UI 纹理（mask、首子偏移 16 位、Potree 密度 lodOffset=100），着色器沿纹理走到最深的活动节点来确定点径。
- `plugins/pointcloud/PointCloudMaterial.js`：继承 `PointsMaterial`（`onBeforeCompile` 路线），点形状可选 square / round / sphere（sphere 写凸起深度），`minPointSize=2`、EDL、调试着色。**只能用于经典 WebGLRenderer。**
- `plugins/fade/TilesFadePlugin.js`：瓦片淡入淡出（2026-09 已抽出引擎无关的基类）。

### 2.4 copc.js / laz-perf / loaders.gl copc

- **`copc.js/src/copc/copc.ts`**：
  - `Copc.create(url | Getter)`：先取 64 KiB 前缀缓存，后续 VLR 读取落在这个范围内时直接切片，不再发请求。
  - 解析 LAS Header、VLR、COPC info VLR、WKT（`LASF_Projection` 2112）、Extra Bytes（`LASF_Spec` 4）。
  - 还有 `loadHierarchyPage`、`loadCompressedPointDataBuffer`、`loadPointDataBuffer`（经 laz-perf 调 `Las.PointData.decompressChunk`）、`loadPointDataView`（`Las.View.create`，返回按维度的 getter）。
- **`copc/hierarchy.ts`**：每条 32 B：`d,x,y,z` 各 int32，`offset` u64，`length` i32，`pointCount` i32。`pointCount = -1` 表示这是一个子页（page）。
- **`copc/info.ts`**：cube（center ± halfsize）、`spacing`、`rootHierarchyPage`、`gpsTimeRange`。
- **`utils/key.ts`**：节点 key 为 `"D-X-Y-Z"`；子节点 `step = [d+1, 2x+a, 2y+b, 2z+c]`。
- **voxelkloud `format-copc/src/range.ts`**：
  - 服务器忽略 Range、返回 200 全文件时，64 MiB 以内接受并切片；
  - 用 `Content-Range` 判断文件结尾；
  - 区分“文件本身有问题”和“主机不支持 Range”。
- **loaders.gl `modules/copc/src/copc-source-loader.ts`**（69 KB）：
  - `COPCTileSource` 输出 Arrow `MeshArrowTable`；
  - 选项有 `rangeChunkSize`、`rangeConcurrency`、`decodeConcurrency`（`AsyncSemaphore`）、`colorFormat`（uint8norm / float16 / float32）；
  - 实现了 `PointCloudScanSource`（bounds、LOD、spacing 下推）。

### 2.5 PlayCanvas：splat-transform 与 `gsplat-unified`

- **`splat-transform/src/lib/writers/write-lod.ts`**：
  - 输出 `lod-meta.json`：`{version, asset:{chunkGaussians, chunkExtent, chunkMinGaussians}, count, counts[], lodLevels, lodErrors, filenames[], tree: MetaNode}`；
  - `MetaNode = {bound, children?, lods?: {[lvl]: {file, offset, count}}, errors?}`；
  - 分区只把位置常驻内存（约 12 B/高斯），其他属性按需 gather；
  - `decimate/`、`spatial/`（kd-tree、b-tree、gaussian-bvh、k-means、radix sort）、`voxel/`（稀疏体素、碰撞、洪泛填充）。
- **`engine/src/scene/gsplat-unified/`**（1.4 万行）：
  - `gsplat-octree-instance.js`（覆盖率 `lodCoverage`，L630–650）；
  - `gsplat-budget-balancer.js`（344 行，`NUM_VALUE_BUCKETS=512`）；
  - `gsplat-lod-table.js`（每个节点的升级链：`upgradeCost/upgradeRatio/upgradeToLod`）；
  - `gsplat-projector.js`、`gsplat-interval-compaction.js`（GPU 压缩）、`gsplat-unified-sort-worker.js`。
  - `applyLodChanges` 的加载优先级分三档：还没显示的节点 → 等待切换的节点 → 下一级预取；同档内再按 coverage 排序。

### 2.6 Aholo（egs 内核 `packages/utils/splat-utils/lod/index.ts`，725 行）

- **`LodSplat.tick(camera)`**：
  - 节点权重 `weight = w_node / (1 + 0.1·d²)`，`forwardBox` 以外的背景节点权重乘 `backgroundPenalty=0.5`；
  - 按 `distanceStep`（默认 ≤10 m 时步长 2）分档；
  - 所有节点先放最粗层，剩余预算按分档轮询逐级细化。
- **`flush(isScheduleFrame)`**：
  - 相邻 chunk 如果在同一文件、偏移连续，就合并成一个 proxy（一次 draw）；
  - 与当前状态做 diff，生成 component，按“可见性变化 → 已就绪的降采样 → 已就绪 → 未就绪”的顺序应用；
  - `hysteresisTicks=4`、`schedulerParallelCounts=4`、`schedulerExistingTaskLimit=64`、`schedulerMinDuration=160 ms`、`maxBudget=3M`。
- 渲染内核是自研 egs（WebGL2），与 three 不兼容。

### 2.7 只做快速评估的项目

- **Bauer 2025（TU Wien 学士论文，davbau/Potree-Next fork）**：
  - 纯 WebGPU compute 暴力光栅，三遍，`atomicMin` 深度，颜色在距最近点 1–2% 深度以内时累加；
  - 坐标拆成 10/10/10 位三级精度，远处只读粗位；
  - 数据：Morro Bay 1.36 亿点，1280×720，俯视全屏；
  - 帧率：RX 6600XT 45–54 fps、GTX 1660Ti Max-Q 52、RTX 4080m 64、RTX 2060 71、RTX 3090 145；
  - 最优配置：每批 128 Mbit（约 8 M 点），workgroup 256；
  - 分级精度对性能**没有可测影响**（论文原话）。
- **Rerun**：
  - `@rerun-io/web-viewer`（React 版 `@rerun-io/web-viewer-react`），`new WebViewer().start(rrdUrl | "rerun+http://…/proxy", el)`；
  - **Web 包版本必须与 SDK 版本一致**。
- **Visionary**：
  - WebGPU 渲染 + ONNX Runtime 逐帧推理，TS API 可嵌入 three 项目；
  - 支持 PLY/SPLAT/KSplat/SPZ/SOG 与 ONNX（4DGS / avatar / scaffold-GS）；
  - “Hybrid Rendering Architecture”自动处理高斯与网格的深度合成；
  - README 称需要 Chrome + 独显，“Ubuntu currently unsupported”。
- **Cesium 3DGS**（2026-04-27 博客）：
  - glTF `KHR_gaussian_splatting` 加 `KHR_gaussian_splatting_compression_spz`（SPZ 2.0）；
  - 3D Tiles 层级 LOD，示例：Redmond 园区 1.1 亿 splat、3.7 km²；
  - CesiumJS 用 wasm 排序；计划并入 3D Tiles 2.0 OGC 社区标准。
- **LidarScout**：先读稀疏子采样得到全局概览，再用神经网络做高度图重建（训练仓库 `lidarscout_training`），并按视点优先细化。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 两级预算 best-first LOD 选择（来源：voxelkloud `lod/select.ts`）

**控制量**：投影几何误差，单位是**设备像素**。

```text
H_dev   = canvas.clientHeight × dpr × renderScale        # 必须用设备像素；与着色器里的 viewportSize 一致
pf(d)   = 0.5·H_dev / (tan(fovY/2) · d)                   # 透视；正交时 pf = H_dev / orthoHeightWorld
e(L)    = spacing_root / 2^L                              # 点八叉树：几何误差 = 点间距
d_n     = max(|cam − center_n| − r_n, nearFloor)          # nearFloor = camera.near（有限值，不用 MAX_VALUE）
key(n)  = e(L_n) · pf(d_n)                                # 堆键 = 本节点的投影误差（px）
```

**默认参数**：

| 参数 | 默认值 | 说明 |
|---|---|---|
| τ（`targetScreenError`） | 1.35 px | 点八叉树标定值 |
| τ_min（`minScreenError`） | τ/4 | 最多比 τ 深 2 级 |
| B（`pointBudget`） | 3 M | 本项目会被质量阶梯覆盖，见 §3.6 |
| h（`budgetHeadroom`） | 0.15 | 奖励细化留出的余量 |
| maxNodes | 4096 | |
| maxBudgetSkips | 32 | |

**伪代码**（每帧一次；每帧零分配；节点对象不可变，所有每帧状态存在按 index 寻址的旁路数组里）：

```ts
function selectVisible(tree, cam, opt, S, out): LodSelection {
  const bonusCap = floor(opt.B * (1 - opt.h));
  let heap = push(empty, tree.root, +Inf, INTERSECTING);
  let n = 0, pts = 0, skips = 0, worst = 0;
  let hitNodes = false, hitBudget = false, hitHeadroom = false, hitFloor = false;

  while (heap.size) {
    if (n >= opt.maxNodes) { hitNodes = true; break; }
    const { node, key, cont } = pop(heap);
    const required = key >= opt.tau;                  // 两级：要求的 / 奖励的
    const cap = required ? opt.B : bonusCap;
    if (node !== root && pts + node.ownPoints > cap) {  // ownPoints 是本层点数，不是子树总数
      required ? (hitBudget = true) : (hitHeadroom = true);
      worst = max(worst, key);
      if (++skips > opt.maxBudgetSkips) break;        // 跳过继续找，不要 break
      continue;
    }
    pts += node.ownPoints;
    S.epoch[node.i] = frame;
    out.indices[n++] = node.i;                        // pop 顺序 = 流式优先级，且父节点必然在子节点之前
    minSpacing = min(minSpacing, spacing(node));      // 只统计被接纳节点，用于 near 平面
    if (node.childMask === 0) continue;
    if (node.childMask === undefined && !tree.tryExpandSync(node)) { needsExpand.push(node); continue; }

    for (const c of node.children) {
      const cc = cont === INSIDE ? INSIDE : classifyAabb(planes, c.box);  // 入堆时裁剪；Inside 状态向下传递
      if (cc === OUTSIDE) continue;
      const ck = e(c.level) * pf(max(dist(cam, c.center) - r(c.level), cam.nearFloor));
      if (ck < opt.tauMin) { hitFloor = true; worst = max(worst, ck); continue; }
      if (ck < opt.tau && pts >= bonusCap) { hitHeadroom = true; worst = max(worst, ck); continue; }  // 入堆时剪枝
      heap = push(heap, c, ck, cc);
    }
  }
  if (heap.size) worst = max(worst, heap.topKey);     // 被放弃的区域里误差最大的那个
  out.limitedBy = hitNodes ? 'nodes' : hitBudget ? 'budget' : hitHeadroom ? 'headroom' : hitFloor ? 'error' : 'complete';
  out.achievedScreenError = worst;                    // 画面上最粗区域的误差（px），用于 HUD 和控制器
  return out;
}
```

**用法约定**：
- `limitedBy ∈ {budget, nodes}` 表示**没有达到目标质量**，HUD 标红，控制器可以考虑升档。
- `headroom` 和 `error` 表示目标已达到。
- 选择结果的 pop 顺序**直接作为下载优先级**。
- 在 Potree 2 层级里，“本层点数”就是 `hierarchy.bin` 记录中的 `numPoints`：`type u8, childMask u8, numPoints u32, byteOffset u64, byteSize u64`，共 22 B。
- 子节点编号：bit0 对应 Z，bit1 对应 Y，bit2 对应 X（`voxelkloud-core/src/octree-math.ts::childBoundingBox`，按 Potree 的 `createChildAABB` 固定下来）。

### 3.2 统一的 SSE 口径（点八叉树、3D Tiles 网格、splat 共用）

```text
sse_px(n) = g_n · H_dev / (2 · tan(fovY/2) · d_n)
```

这个公式与 3DTilesRendererJS 的 `g / (d · (2/P5)/H)` 完全等价（P5 = 1/tan(fovY/2)）。

| 内容 | g 的取值 | 细化阈值 τ | 细化方式 |
|---|---|---|---|
| 点八叉树（Potree2 / ANET） | spacing_L | 1.35 px（voxelkloud）；3DTilesRendererJS `PotreePlugin` 取 1 | ADD |
| 3D Tiles 网格 | tileset 的 geometricError | 16 px（3DTilesRendererJS 默认） | REPLACE：父节点只在子节点全部选中**且驻留**后才隐藏（`replace.ts::filterReplacedParents`） |
| splat LoD | 节点误差表（splat-transform `errors`） | 按 §3.9 仲裁 | 按级别替换 |

距离 d：
- voxelkloud 取“到中心的距离减去半径，并下限到 near”；
- 3DTilesRendererJS 取“到包围体的距离，在包围体内部时误差为 ∞”；
- **本项目统一用“到 AABB 的最近距离，下限 near”**。

### 3.3 局部点径：octree cut（来源：voxelkloud `cut.ts`、`points-glsl.ts`、`compute-wgsl.ts::projectMeta`；3DTilesRendererJS `PotreePlugin`）

**CPU 端**：每帧在选择完成后执行一次，只收录**已驻留**的节点。

```ts
// 输出 Uint8 RGBA 纹理，宽 1024；第 i 项 = [selectedResidentChildMask, first>>16, first>>8, first]
function buildCut(root, epoch, frame, resident) {
  q[0] = root; let qn = 1, write = 1;
  for (let qi = 0; qi < qn; qi++) {                 // BFS：队列位置就是 slot，子节点连续存放
    let mask = 0; const first = write;
    for (let c = 0; c < 8; c++) {
      const ch = q[qi].children[c];
      if (ch && epoch[ch.i] === frame && resident.has(ch.i)) { mask |= 1 << c; q[qn++] = ch; write++; }
    }
    data.set([mask, (first >> 16) & 255, (first >> 8) & 255, first & 255], qi * 4);
  }
  tex.needsUpdate = true;
}
```

**着色器端**（GLSL / WGSL / TSL 三套实现同构）：

```glsl
int slot = 0; vec3 bMin = uRootMin, bSize = uRootSize; int depth = 0;
for (int i = 0; i < 20; i++) {                       // MAX_CUT_DEPTH = 20（float32 精度所限）
  uvec4 t = texelFetch(uCut, ivec2(slot & 1023, slot >> 10), 0);
  vec3 h = bSize * 0.5; vec3 c = step(bMin + h, aPos);
  int idx = int(c.x) * 4 + int(c.y) * 2 + int(c.z);  // 与 Potree 的位序一致
  if ((int(t.r) & (1 << idx)) == 0) break;
  slot = int(t.g) * 65536 + int(t.b) * 256 + int(t.a) + bitCount(int(t.r) & ((1 << idx) - 1));
  bMin += c * h; bSize = h; depth++;
  if (depth > aLevel) break;                          // 缩小幅度封顶 1 级，确定后提前退出
}
float shrink = clamp(float(depth) - aLevel, 0.0, 1.0);
float px = (aPitch / exp2(shrink)) * 2.0 * uSizeMul * uProjScale / max(-view.z, 1e-6);
gl_PointSize = clamp(px, uMinPx, uMaxPx);             // 默认 [1, 8] px；uProjScale = 0.5·H_dev·P[1][1]
```

**关键数值**：
- 缩小幅度封顶 1 级，理由见“实现者先读”第 4 条（覆盖率下降 5.4% → 0.7%）。
- 3DTilesRendererJS 的覆盖系数 `SPACING_COVERAGE_FACTOR = 1.7`（与 Potree 一致）；voxelkloud 用 `2·sizeMul` 倍间距作为直径。本项目默认取 **1.7–2.0**，可调。
- 坐标一律用**云局部 float32**（以节点或云原点为原点）。UrbanScene3D 立方体最大 3.2 km，ULP 约 2.4e-4 m，20 级时格子约 3 mm，精度足够。

### 3.4 WebGL2 档：单缓冲单 draw（来源：voxelkloud `sink-points.ts`、`sink-compute.ts::BlockAllocator/SlotPool`）

- **缓冲**：`pos f32×3 | col u8×4(normalized) | pitch f32 | meta u32(level 8 | slot 16 | class 8)`，共 24 B/点。`meta` 用 `vertexAttribIPointer` 按整数绑定：float32 只能精确表示到 2^24，按 float 绑定会把 class 字节舍入进 slot。
- **分配**：`BlockAllocator`（首次适配，释放时与相邻空块合并；尾部空闲时下调 `high` 高水位，否则每帧仍按历史峰值分派）加 `SlotPool`（LIFO 复用 slot，被释放区间写入 `DEAD_META` 哨兵，防止孤儿点借用新节点的 liveness）。
- **容量**：初始值为 `min(本云点数, budget×slack)`（`capacity.ts`，实测 JS 堆从 1.03 GB 降到 178 MB）；满了**先驱逐、再扩容**（`copyBufferSubData` 翻倍，上限 2^26 点）。
- **每帧**：
  - 更新 `uLive`（R8UI 纹理，宽 1024 × 64 行，覆盖 65536 个 slot）；
  - 在 vertex shader 里把未选中、被隐藏类别、超出高度带的点移出裁剪空间（`gl_Position = vec4(2,2,2,1)`，`gl_PointSize = 0`），比 discard 便宜，而且不写深度；
  - **一次** `drawArrays(POINTS, 0, alloc.used)`。
- **拾取**：`FS_PICK` 输出 `gl_VertexID + 1`，编码进 RGBA8。单缓冲意味着 `gl_VertexID` 就是全局点号。
- **预热**：在 `init` 阶段就链接 program，并用一次带 color mask 的 1 顶点 draw 触发驱动真正编译；`addCloud` 从 17–19 ms 降到 6.8 ms。
- **本项目落地**：在经典 `THREE.WebGLRenderer` 里用一个 `THREE.Points` 加 `RawShaderMaterial`（GLSL3）实现同样的结构，这样能和 three 场景正常组合，深度写入正确。其他图层的 TSL 材质通过 r186 的 `WebGLRenderer.setNodesHandler` 在同一渲染器里运行（已在源码 `src/renderers/WebGLRenderer.js:1077` 确认，官方示例 `webgl_tsl_*.html`）。

**关于“运动降载”**：mask 方式只减少片元工作，顶点工作量等于 `alloc.used`。想在运动时降载，要么把本帧选中的块压到连续区间后缩小 draw range（CPU 端做一次 compaction，或者按层级分 slab），要么降 DPR。**不要照搬“运动时缩小选择集”**（voxelkloud 实测对 INP 无效）。

### 3.5 流式调度与驱逐（来源：voxelkloud `view.ts::stream/drainPending/evict/retryOrFail`、`stream-policy.ts`；OLV `evictionPolicy.ts`）

```ts
function stream(h) {
  for (const i of sel.indices) lastSeen[i] = frame;
  // 1) 取消：离开视锥是“事实”，取消；被取代只是“猜测”，仅在饱和时取消
  for (const [i, ctl] of inFlight) {
    const stale = frame - lastSeen[i]; if (stale <= 0) continue;
    const outside = !intersectsAabb(planes, box(i));
    if ((outside && stale >= 2) || (abortSuperseded && inFlight.size >= maxConc && stale >= 8)) { ctl.abort(); inFlight.delete(i); }
  }
  // 2) 分派：严格按选择集的 pop 顺序；首节点落地前并发宽度为 1
  const width = resident.size === 0 ? 1 : maxConc /*12*/;
  for (const i of sel.indices) {
    if (inFlight.size >= width) break;
    if (resident.has(i) || inFlight.has(i) || queued.has(i) || failed.has(i)) continue;
    if (retryAt.get(i) > frame) continue;              // 退避
    if (!hasPayload(i)) continue;
    fetchDecodeInWorker(i).then(d => pending.push(d)).catch(e => {
      if (e.name === 'AbortError') return;             // 主动取消不算失败
      const a = ++attempts[i];
      if (a >= 3) failed.add(i); else retryAt.set(i, frame + 30 * 4 ** (a - 1));  // 30 / 120 / 480 帧
    });
  }
  if (residentBytes > maxResident /*512MiB*/) evict();
}

function drainPending() {                              // 上传预算为每帧 8 MiB，最近被选中的优先
  pending.sort((a, b) => lastSeen[b.i] - lastSeen[a.i]);
  for (let staged = 0; pending.length && staged < 8 << 20;) {
    const p = pending.shift();
    const bytes = sink.attach(p);
    if (bytes > 0) { resident.add(p.i); staged += bytes; }
    else { refused.unshift(p); evict(); }              // 空间不足：已解码的数据不重新下载，放回队首，并触发驱逐
  }
}

function evict() {                                     // LRU，但永远不碰本帧选择集和根节点
  const cands = [...resident].filter(i => i !== root && lastSeen[i] < frame).sort(byLastSeenAsc);
  for (const i of cands) { if (residentBytes <= 0.85 * maxResident) break; sink.detach(i); resident.delete(i); }
  stats.evictionStalled = residentBytes > maxResident; // 工作集本身超限时如实上报，不靠牺牲画面来掩盖
}
```

**OLV 的补充（按点数计的双阈值与排序）**：
- 驱逐从 `resident > 1.5·B` 开始，释放到 `1.15·B`；
- 可见节点驻留不满 1 s 时不驱逐，但**被更细层取代的节点除外**，并且这类节点排在驱逐队列最前；
- 驱逐顺序：视锥外 → 视锥内但未选中 → 被取代 → 正在看的；
- 同类里先驱逐更深、更远的节点。

**层级加载**（voxelkloud `format-potree/src/hierarchy-fetch.ts`）：
- 用一次**不带 Range** 的 GET 拉整个 `hierarchy.bin`（上限 `maxPrefetchBytes`，超过则退回分块 Range），因为 CDN 会压缩 200 响应，但不压缩 206 响应。
- 这样 `tryExpandSync` 总能成功，下钻没有延迟。
- 请求头里**不要**带 `content-type: multipart/byteranges`（参考实现这样做了，它会让请求变成非简单请求，触发 CORS 预检）。

### 3.6 自动画质阶梯（来源：voxelkloud `quality.ts`；OLV `frameBudgetGovernor.ts`、`adaptiveDpr.ts`）

**阶梯**：voxelkloud 原有 5 档；本项目**在下方新增 2 个软件档**（SwiftShader/CI，参数取自 r12 实测：起步 40k，下限 10k，关闭 EDL）。

| idx | name | renderScale | pointBudget | τ（px） | 来源 |
|---|---|---|---|---|---|
| 0 | soft-min | 0.5 | 40,000 | 4.0 | 本项目（r12） |
| 1 | soft | 0.6 | 150,000 | 3.0 | 本项目 |
| 2 | minimum | 0.6 | 750,000 | 2.7 | voxelkloud |
| 3 | low | 0.75 | 1,500,000 | 2.0 | voxelkloud |
| 4 | medium | 1.0 | 3,000,000 | 1.35 | voxelkloud（默认） |
| 5 | high | 1.0 | 6,000,000 | 1.0 | voxelkloud（自动模式上限） |
| 6 | ultra | 1.0 | 12,000,000 | 0.7 | 只能手动选择 |

**初始档**（`initialQualityIndex`）：
- 以 medium 为基准；
- `architecture` 匹配 integrated / swiftshader / llvmpipe 时 −1；
- `vendor` 匹配 software / swiftshader 时**直接进 0 档**；
- 核数 ≤ 2 时 −1，≥ 8 时 +1；
- `deviceMemory < 4 GB` 时 −1；
- 像素数 > 8 M 时 −1，< 1.5 M 时 +1；
- 用户显式给出的 budget 或 τ 构成上限（`ceilingForOptions`），自动模式不会超过用户设定。

**控制器**（采样的是**相邻两次呈现的帧间隔**，不是 frame time）：

```ts
sample(dt, now) {
  if (!(dt > 0) || dt > 400) return HOLD;              // 后台 tab、断点、整堆 GC 都不算
  if (pendingAttach > 0) return HOLD;                  // 解码积压期间的慢帧不算
  T += dt < T ? (dt - T) * 0.25 : (dt - T) * 0.001;    // 用最快的那些帧估计刷新周期
  T = clamp(T, 6, 34);
  if (settle > 0) { settle--; return HOLD; }           // 换档后丢弃 20 帧
  win.push(dt); if (win.length < 90) return HOLD;       // 窗口 90 帧，约 1.5 s
  const p50 = q(win, .5), p95 = q(win, .95);
  if (p50 > 1.35 * T && idx > 0) {                     // 快速降档
    if (lastUpIdx === idx) upDelay = min(upDelay * 2, 120_000);  // 升上来又掉下去：下次升档要等更久
    return move(idx - 1);
  }
  if (idx < ceiling && p95 < 1.1 * T && now - lastChange > upDelay /*初始 5000*/) { lastUpIdx = idx + 1; return move(idx + 1); }
  return HOLD;
}
```

**可选功能的开关**（OLV governor，独立于阶梯，控制 EDL、hover 细节、上传批量）：

```text
load = clamp01( max( (p50 − T)/(2T),  0.5·(pmax − T)/(2T) ) )
EDL:   load ≥ 0.45 关；load ≤ 0.25 开；中间保持原状
DPR 压力 = ceil_0.25( max(load, moving ? 0.25 : 0) )     # 移动端运动压力为 0.5
上传比例 = pending ? max(0.1, (moving ? 0.5 : 1)·(1 − load)) : 1
```

**运动 DPR**（OLV `adaptiveDpr.ts`）：
- `dpr = base + (floor − base)·clamp01(ω/1.2)`，其中 `base = max(floor, 0.85·maxDpr)`，`floor = min(maxDpr, 1.0)`；
- 按 0.25 量化；降档限速 250 ms，停止运动时立即恢复。
- 注意：voxelkloud 刻意**不让自动画质关 EDL**（用户打开的着色模式不应被自动覆盖）。本项目建议：EDL 由用户设置决定，只有在 soft 档（idx ≤ 1）时强制关闭，并在 UI 上明确提示。

### 3.7 渐进显示与“不闪烁”

- **节点淡入**（OLV `fadeDither.ts`）：
  - `keep(i) = fract(i·0.618033988749895) ≤ progress(t)`，`progress` 在 220 ms 内从 0 升到 1（`StreamingRenderer.ts::FADE_MS = 220`，作者注释称 150–250 ms 的中值读起来最顺滑）；
  - 保持不透明、写深度，EDL 正确；
  - TSL 写法：`float(instanceIndex).mul(PHI).fract().lessThanEqual(progress)` 乘到 `sizeNode` 上；
  - GLSL 写法：`gl_PointSize *= keep`。
  - 用于节点首次驻留、父子替换，也用于 UI 的“图层切换”动效，与 transitions.dev 的节奏统一。
- **粗到细**：选择集的 pop 顺序就是下载顺序；首节点单独占用网络宽度；cut 只统计已驻留节点，所以画面先粗后细，不会先出现空洞。
- **滞回**：LOD 等级变化需要稳定 4 个 tick 才提交（Aholo `hysteresisTicks=4`）；PlayCanvas 的分配器遇到第一个放不下的升级就停止，而不是跳过继续，避免相机微动引起等级来回翻转。

### 3.8 WebGPU 档：compute 三遍软光栅（来源：voxelkloud `compute-wgsl.ts`；Bauer 2025；Schütz 2022）

#### 3.8.1 存储布局（受 WebGPU 保证的 `maxStorageBuffersPerShaderStage = 8` 约束）

| binding | 内容 | 说明 |
|---|---|---|
| 0 `pos` | `array<f32>`（xyz） | 云局部坐标 |
| 1 `col` | `array<u32>` | RGBA8，或 scalar 的 f32 位模式（每朵云只有一种着色模式生效） |
| 2 `depth` | `array<atomic<u32>>` | 每像素 1 个 |
| 3 `accum` | `array<atomic<u32>>` | 每像素 4 个：r·w、g·w、b·w、w |
| 4 `U` | uniform | 352 B，必须与 WGSL `struct U` 按字节对齐；否则布局无效，**画面变黑但不报错** |
| 6 `nmeta` | `array<u32>` | level 8 位、slot 16 位、class 8 位 |
| 7 `cut` | `array<u32>` | 与 §3.3 的纹理字节相同 |
| 8 `live` | `array<u32>` | 前 65536 个是 liveness，后 65536 个是节点 pitch 的 f32 位；compact 模式下复用为可见块表 |

#### 3.8.2 三遍法

```wgsl
// clearPass：每线程清 4 个像素（否则 16.8 MP 画布会超过 65535 个 workgroup 的上限）。accum 用 clearBuffer 清零。
atomicStore(&depth[i], 0x7f7fffffu);                  // FLT_MAX 的位模式

// depthPass：每点一个线程，workgroup 256
s = project(i);                                       // 裁剪平面、视锥、NDC 范围（±1.5 的宽松边界）
s.d = bitcast<u32>(clip.w);                           // 非负 float 的位模式单调，可以直接对 u32 做 atomicMin
s.r = clamp(localPitch*2*sizeMul*projFactor, minPx, maxPx)*0.5;   // 同 §3.3
for dy in [-ri..ri]: dxMax = ceil(sqrt(r² - dy²))     // 按行半宽扫描，省掉方框四角约 1/5 的迭代
  for dx in [-dxMax..dxMax]: if dx²+dy² ≤ r²: atomicMin(&depth[y*W+x], s.d)

// colorPass：用同样的覆盖范围
zmin = bitcast<f32>(atomicLoad(&depth[idx]));
if (eye > zmin + max(2*localPitch, 0.005*zmin)) continue;       // 用“世界单位的采样步长”判断是否同一表面，不用 1% 深度
w = max(1u, u32((0.004 + exp(-(dx²+dy²) / (2*0.7²))) * 255));   // 以像素为单位的 σ=0.7 重建滤波，加一个下限
atomicAdd(&accum[4idx+0], r*w); ... atomicAdd(&accum[4idx+3], w);
// u32 上限：每次贡献最大 255·255，同一像素最多约 6.6 万次贡献才会溢出

// resolve（全屏三角形 + 片元着色器）：
c = accum.rgb / accum.w / 255;  if (accum.w == 0) → 背景色
// EDL 并进同一遍，沿用 Potree 口径（edlStrength 可以直接迁移）：
resp = Σ_{k∈8 个方向} max(0, log2(z_c) − log2(z_k)) / 8;  c *= exp(−resp·300·strength)
```

**代价估算**：
- 每点每遍 `(2r+1)²` 次原子操作，所以 renderScale 是最有效的调节杠杆；
- 片元估算 `F = N·π·(s/2)²`：3 M 点、s=2 px 时约 9.4 M 次，接近 1080p（2.07 MP）的 4.5 倍 overdraw。

#### 3.8.3 compact 分派（驻留点远多于绘制点时）

- `live` 改存可见块表 `[(firstSlot, firstOutputIndex)…]`（按输出序升序，`buildVisibleBlocks`）；
- dispatch 数就是本帧绘制点数；
- 每个线程二分查找自己所在的块（最多 log2(4096) = 12 次）；
- 这正是 §3.4 所说“运动降载要缩小 dispatch”的那个杠杆。

#### 3.8.4 与 three 场景组合（本项目改法，voxelkloud 没有这样做）

1. 用 TSL 移植 depth 与 color 两遍：`instancedArray(n,'uint').toAtomic()`，调用 `atomicMin`、`atomicAdd`（`src/nodes/gpgpu/AtomicFunctionNode.js`），`renderer.compute(depthPass)`、`renderer.compute(colorPass)`；在渲染场景之前执行。
2. resolve 改成一个全屏 `Mesh(PlaneGeometry, NodeMaterial)`，设 `renderOrder=-1`、`frustumCulled=false`、`depthWrite=true`：
   - `colorNode` 读取 `accum`；
   - `depthNode = viewZToPerspectiveDepth(-eyeDepth, near, far)`（启用 reversed 或 log depth 时改用对应函数，`NodeMaterial.js` L683–711 有 `depthNode` 分支）；
   - 没有点的像素 discard。
3. 之后无人机、航线、天气粒子按常规方式渲染并做深度测试，得到正确的互相遮挡，**不需要深度拷贝**。
4. 拾取：单独一遍，scissor 限制在 1 像素，只在点击时执行（`overlay.ts` 的做法）。

**Bauer 2025 给出的参考参数**：每批 4–8 M 点；workgroup 256 表现最好；10/10/10 位分级精度没有可测收益，**不用实现**。1.36 亿点暴力光栅在 3090 上 145 fps、在笔记本 1660Ti 上 52 fps，说明本项目单城 5 M 点在独显上远未触及上限。

### 3.9 全局预算仲裁：性价比分配器（来源：PlayCanvas `gsplat-budget-balancer.js`）

用途：多个云（6 城拼接的压力场景）、点云与 splat 并存（V0.8）、以及质量阶梯给出的**总预算**在各数据源之间分配。

```text
coverage_n = (r_n / (r_n + d_n))²                       # 透视投影覆盖率，范围 [1e-12, 1]；正交时为 (r/orthoH)²
value_k    = coverage_n · (err_k − err_{k+1}) / (count_{k+1} − count_k)   # 单级升级每多花一个点换来的误差下降
```

```ts
function balance(instances, budget) {
  if (Σ finest ≤ budget) return allFinest();  if (Σ coarsest ≥ budget) return allCoarsest();
  bucketHead.fill(-1);
  for (node of all) { node.lod = coarsest; push(bucketOf(value(node, firstUpgrade)), node); }   // 512 个桶
  let spent = Σ coarsest;
  for (b = 511; b >= 0; b--) while ((g = popHead(b)) >= 0) {
    const cost = upgradeCost(g); if (spent + cost > budget) return;   // 第一个放不下就停止（不跳过，避免闪烁）
    spent += cost; g.lod = next;
    if (hasNext(g)) push(min(bucketOf(value(g, next)), b), g);        // 后继升级最多回到当前桶，不会跳到已扫过的桶
  }
}
bucketOf(v) = clamp( ((floatBits(v) − floatBits(1e-24)) · 511 / (floatBits(1e3) − floatBits(1e-24))) | 0, 0, 511 )  // float 位模式 ≈ log2，不需要 Math.log
```

**映射到点云**：
- 对点八叉树，“单级升级”就是展开一个节点的子层；
- `err_k` 取 spacing_L，`count` 取子层点数；
- 覆盖率直接用 §3.1 的 pf。

**推荐落地方式**：
- 单云内部仍用 §3.1（逐节点 best-first 更精细）；
- 跨云与跨图层时，先用本算法按“每个数据源的升级链”分配各源预算 B_i，再把 B_i 交给各自的 §3.1。

### 3.10 COPC 读取（copc.js，放在 worker 中）

```ts
import { Copc, Hierarchy } from 'copc';                 // npm copc@0.0.9，依赖 laz-perf@0.0.7
const copc = await Copc.create(url);                     // 64 KiB 前缀缓存；得到 info.cube / info.spacing / rootHierarchyPage / wkt / eb
let { nodes, pages } = await Copc.loadHierarchyPage(url, copc.info.rootHierarchyPage);  // key "D-X-Y-Z"
// 子页按需加载：pages['D-X-Y-Z'] → loadHierarchyPage(url, page)
const view = await Copc.loadPointDataView(url, copc, nodes['0-0-0-0']!, { lazPerf, include: ['X','Y','Z','Red','Green','Blue','Classification'] });
for (let i = 0; i < view.pointCount; i++) { x = view.getter('X')(i) /* 已应用 scale/offset */ }
// → 减去云原点，写入 Float32Array 与 Uint8 颜色，transfer 回主线程
```

- 映射到 `LodTreeView`：`geometricErrorAt(L) = info.spacing / 2^L`；节点包围盒由 key 和 cube 推出（`bounds.ts`）。
- **CORS 注意**：服务端的 `Access-Control-Allow-Headers` 必须包含 `range`，否则带 Range 的 GET 预检失败。voxelkloud 实测：USGS 3DEP EPT 普通 GET 成功，但带 Range 的预检返回 403。
- LAS 1.4 的点格式 6–8 **不能**用 loaders.gl LASLoader 读（它只支持 ≤ 1.3，见 OLV `heavy-cloud-native.md`）；COPC 恰好就是 LAS 1.4，所以必须走 copc.js 或 laz-perf 的 chunk 解码。

### 3.11 near/far 与深度精度（来源：voxelkloud `lod/metric.ts::suggestNearFar`）

```text
near = clamp(10 · minAdmittedSpacing, 0.01, 100)
far  = max(1.5 · viewDepth, near + 10000)
```

- 必须在帧**开始**时应用，否则裁剪用的视锥和渲染用的视锥不一致，形成振荡。
- 用 depth24 时，这条规则在 1 km 处给出 54 倍余量、4.6 km 处 10 倍余量（autzen 测量）。
- 本项目的 FPV 贴近立面时 minAdmittedSpacing 很小，near 会随之变小，这是正确的。WebGPU 档可以开 reversed-Z（r13）进一步加余量。
- **无人机与航线图层共用同一对 near/far**。如果无人机模型比点间距更靠近相机（第三人称跟随），near 取 `min(ruleNear, 0.5 · 跟随距离)`。

### 3.12 UrbanScene3D 实测：盒计数与预算规划（本单元测量，`.cache/research/n01/boxcount.py`）

每个 PLY 约 5.0 M 点，属性为 xyz + 法线。统计立方体 2^L 网格下被占用的格子数（累计）：

| 城市 | 立方体边长 | L7 | L8 | L9 | L10 | L11 | 层间比（L4→L10） | D |
|---|---|---|---|---|---|---|---|---|
| New York | 3166.7 m | 29,720 | 146,720 | 622,498 | 2,136,329 | 3,941,935 | 4.43, 4.88, 5.26, 4.94, 4.24, 3.43 | 2.15–2.40 |
| Shenzhen | 1999.0 m | 21,723 | 97,969 | 431,531 | 1,596,408 | 3,366,979 | 4.06, 4.86, 4.29, 4.51, 4.40, 3.70 | 2.02–2.28 |
| San Francisco | 740.1 m | 20,399 | 82,979 | 350,624 | 1,384,914 | 3,376,558 | 4.13, 4.90, 3.92, 4.07, 4.23, 3.95 | 1.97–2.29 |

**解读与参数**：

1. **城市场景的 D 略大于 2**（立面和高楼），比 voxelkloud 测的航测地表（1.84–2.05）更“立体”，每下降一级点数约 ×4.2–5.3。
2. **八叉树深度**：按 Potree spacing = cube/128，“深度 d 的累计点数 ≈ L(7+d) 的格子数”。以 NY 为例，d=0..4 的累计点数约为 29.7k / 147k / 622k / 2.14M / 3.94M，d=5 时全部 5.0 M 点纳入，**总深度约 5–6 层**。根节点 spacing：NY 24.7 m、SZ 15.6 m、SF 5.8 m。
3. **预算规划**（单城）：

   | 预算 | 相当于 | 适用 |
   |---|---|---|
   | 40k | 根层加少量近景细化 | soft-min 档 |
   | 150k | 前两层全量（均匀间距约 12 m）加近景细化 | soft 档 |
   | 750k | 前三层全量 | minimum 档 |
   | 3M | 大部分场景已不受预算约束 | medium 档，`limitedBy` 会是 `error` 或 `complete` |
   | 6M | 单城全部点 | high 档 |

4. **压力场景**：把 6 城并排拼成约 30 M 点的世界，外加 N 架无人机。它同时考验 §3.9 仲裁、§3.5 驱逐和 §3.6 阶梯，建议作为 e2e 性能用例的**默认场景**。

---

## 4. 在本项目中的落点与复用方式

对应 r12 的 `PointCloudEngine` 架构（Controller / Selector / NodeStore / Cache / Uploader / Fetcher / RenderBackend），本单元给出**替换和增补**：

| 能力 | 来源（文件 / 符号） | 本项目模块 | 版本 | 复用方式 | 相对 r12 的变化 |
|---|---|---|---|---|---|
| 两级预算 LOD 选择、`limitedBy`、`achievedScreenError` | voxelkloud `lod/select.ts`、`heap.ts`、`frustum.ts`、`metric.ts` | `apps/web/src/world/pointcloud/core/Selector.ts` | V0.1 | port（纯 TS，无 three 依赖，可在 worker 和 vitest 中运行） | **替换** r12 的 Selector |
| 统一 SSE（点、瓦片、splat） | voxelkloud `screenErrorPx`、3DTilesRendererJS `calculateTileViewError` | `core/sse.ts` | V0.1 | port | 新增 |
| octree cut 局部点径 | voxelkloud `cut.ts`；3DTilesRendererJS `PotreePlugin._updateActiveNodesTexture` | `core/OctreeCut.ts` + GLSL/TSL 片段 | V0.1 | port | **替换** r12 的“Lite 自适应点大小” |
| WebGL2 单缓冲单 draw、liveness、分配器 | voxelkloud `sink-points.ts`、`points-glsl.ts`、`BlockAllocator`、`SlotPool`、`capacity.ts` | `backends/GlPointsBackend.ts`（经典 WebGLRenderer + RawShaderMaterial） | V0.1 | port | **替换** r12 的每节点一个 Points |
| 流式策略（首节点宽度 1、取消、退避、上传预算、驱逐） | voxelkloud `stream-policy.ts`、`view.ts::stream/drainPending/evict`；OLV `evictionPolicy.ts` | `core/Fetcher.ts`、`core/Cache.ts`、`core/Uploader.ts` | V0.1 | port | 参数与规则按 §3.5 定死 |
| 整体拉取 hierarchy.bin | voxelkloud `format-potree/hierarchy-fetch.ts` | `io/potree2/HierarchyLoader.ts` | V0.1 | port | 新增 |
| 自动画质阶梯 | voxelkloud `quality.ts` | `core/QualityController.ts` | V0.1 | port，并加 2 个软件档 | **替换** r12 的 AIMD 控制器 |
| 可选功能开关、运动 DPR | OLV `frameBudgetGovernor.ts`、`adaptiveDpr.ts` | `core/FrameGovernor.ts` | V0.1 | port | 新增 |
| 节点淡入（Weyl 抖动） | OLV `fadeDither.ts` | 着色器片段 + `core/Fade.ts` | V0.1 | port | **替换** alpha 淡入 |
| 后端探测 | OLV `renderBackendChoice.ts` | `apps/web/src/render/tier.ts` | V0.1 | port | 新增（真实 requestAdapter） |
| WebGPU compute 光栅 + EDL + compact | voxelkloud `compute-wgsl.ts`、`sink-compute.ts`；Bauer 2025 | `backends/ComputePointsBackend.ts`（TSL compute + depthNode resolve） | V0.3（Tier A 默认），V0.6（compact） | port | **替换** r12 的 vertex-pulling quad |
| GPU 诊断（device lost、validation、adapterInfo） | voxelkloud README 诊断节、`gpu-timing.ts` | `render/diagnostics.ts` + HUD | V0.1 | port | 新增 |
| 多源预算仲裁 | PlayCanvas `gsplat-budget-balancer.js`、`gsplat-lod-table.js` | `world/RenderBudgetArbiter.ts` | V0.6 | port | 新增 |
| COPC 读取 | copc.js（+ laz-perf）；voxelkloud `format-copc/range.ts` | `io/copc/CopcSource.ts`（worker） | V0.5 | adopt（copc.js）+ port（range 容错） | 新增 |
| 浏览器内建八叉树（拖入文件） | voxelkloud `wasm-build`；OLV `io/heavy` | `io/local/LocalBuild.ts` | V0.5 | adopt（dev 或可选） | 新增 |
| 3D Tiles（地形、倾斜、Google 3D Tiles） | 3DTilesRendererJS `TilesRenderer`、`TilesFadePlugin`、`ReorientationPlugin` | `world/layers/TilesLayer.ts` | V0.8 | adopt（只用网格瓦片） | 新增 |
| 3DGS LOD 构建 | `@playcanvas/splat-transform`（Streamed SOG）；Spark `build-lod`（r13） | `pipeline/visual/splat_lod`（离线） | V0.8 | adopt（CLI） | 补充 r13 |
| 3DGS 互操作格式 | Cesium 3D Tiles + `KHR_gaussian_splatting(_compression_spz)` | World Package `visual/gaussian/tileset.json` | V0.8 | reference（规范） | 新增 |
| chunk LOD 滞回与合并 draw | Aholo `LodSplat.tick/flush` | `world/layers/GaussianLayer` 调度 | V0.8 | reference | 补充 |
| 仿真日志复盘 | `@rerun-io/web-viewer-react` | `/debug/rerun` 路由（开发专用） | V0.2+ | adopt（可选） | 新增 |
| 对照与基线页 | `@voxelkloud/react`、3DTilesRendererJS `PotreePlugin`、potree-core | `apps/web/dev/oracles/*` | V0.1 | adopt（仅 dev） | 扩充 r12 的交叉验证页 |

**渲染分级（修订版）**：

```text
Tier A  WebGPU（真实 adapter）
        渲染器：WebGPURenderer
        点云：ComputePointsBackend（TSL 三遍 + depthNode resolve）
        其他图层：TSL 材质
Tier B  WebGL2（没有 adapter，或设置了 forceWebGL）
        渲染器：经典 WebGLRenderer + setNodesHandler（其他图层的 TSL 材质照常运行）
        点云：GlPointsBackend（单缓冲 + gl_PointSize）
Tier S  软件渲染（SwiftShader / llvmpipe，headless CI）
        同 Tier B；质量阶梯从 soft-min 起步；关闭 EDL；renderScale 0.5
```

---

## 5. 对比与推荐

### 5.1 点云渲染核心（按推荐度排序）

| 排名 | 方案 | 契合度 | 2026 活跃度 | 社区 | 结论 |
|---|---|---|---|---|---|
| 1 | **voxelkloud**（port） | 同一技术栈（three ≥0.180 + TSL + React），有两级预算、质量阶梯、单 draw、compute 光栅，设计记录完整 | 极高（8–9 月每周发版） | 极小（0 star，单作者） | 算法全部 port；不作为运行时依赖 |
| 2 | potree-core / three-loader（r12，已在 refs） | three 生态，库化 | 中 | 小 | 降级为对照页 |
| 3 | 3DTilesRendererJS `PotreePlugin` | three，但只支持经典 WebGL | 高 | 大（2.5k） | 作为对照与 SSE 口径参考 |
| 4 | OLV | 策略层有价值，渲染层是慢路径 | 极高 | 小 | 策略 port |
| 5 | Potree-Next（r13） | WebGPU 原型，遍历不看预算 | 低（2025-10 起停更） | 小 | 只参考技巧 |

### 5.2 格式与流式

| 排名 | 方案 | 结论 |
|---|---|---|
| 1 | Potree 2.0 兼容格式（+ `ANET_Q16` 扩展） | 运行时主格式：三个查看器可以直接对照 |
| 2 | COPC（copc.js 读，PDAL/untwine 写） | 归档与交换；V0.5 支持直接流式 |
| 3 | 3D Tiles 1.1（3DTilesRendererJS） | V0.8：GIS 底座与点云导出 |
| 4 | loaders.gl copc（v5 alpha） | 等正式发布后再评估 |

### 5.3 3DGS LoD 流式（补充 r13）

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| Spark 2.x（`.RAD`，已在 refs） | three 生态，连续 LoD 树，虚拟分页 | 只支持 WebGLRenderer | r13 结论不变：独立的高保真路由 |
| three r186 `GaussianSplat` | 与 WebGPURenderer 同栈 | LoD 需要自己实现 | 主栈 GaussianLayer（r13） |
| PlayCanvas Streamed SOG + balancer | 离线工具成熟（splat-transform），分配算法优秀 | 引擎不同 | 工具 adopt，算法 port |
| Aholo | 规模最大（官方称 10 亿），chunk LOD | 自研引擎 | reference |
| Cesium 3D Tiles + `KHR_gaussian_splatting` | 标准化，地理配准 | CesiumJS 路线 | 作为 World Package 的互操作格式 |
| Visionary | 支持 4DGS/ONNX 动态 | 仅 Chrome，需独显，文档称不支持 Ubuntu | V1.0 reference |

### 5.4 是否替换 refs/ 中已有仓库

- **不删除**已有仓库。
- 在 02-refs 的 Web 3D 分组中**新增** voxelkloud（P0）、3DTilesRendererJS（P1）、copc.js（P1）、splat-transform（P2）、openlidarviewer（P1，只看策略层）。
- potree-core / three-loader 从 P0 调整为“对照”。
- Potree-Next 从 P1 调整为 P2。

---

## 6. 风险与注意事项

1. **voxelkloud 很年轻**：
   - 组织 2026-08-22 创建，0 star，单作者，一个月内从 0.5 发到 0.8，其中含破坏性变更（`material`、`edl` 挪到子路径导出）；
   - 部分注释是葡萄牙语；
   - README 里的性能数字（INP、Speed Index）是**仓库自报**，本单元没有复测；
   - 对策：只 port 算法并附上出处；用 `@voxelkloud/react` 做 dev 对照页，锁定版本。
2. **voxelkloud 的 compute 和 points 路径不能与 three 场景组合**（直接写 swapchain，自己持有 context）。本项目必须采用 §3.8.4 的 depthNode resolve 和 §3.4 的“经典 WebGLRenderer 内单 Points”改法，所以需要自己写测试，保证 cut 走查、liveness、分配器与原实现一致（它们的 `*.test.ts` 可以移植成我们的单测）。
3. **WebGPU compute 光栅的暗坑**：
   - storage buffer 只保证 8 个；uniform 结构体必须按字节对齐；这两类错误都是**画面变黑、不抛异常**；
   - `device.lost` 同样是静默的；
   - WGSL 源码放在 JS 模板字符串里时，注释中的反引号会截断字符串；
   - 大画布上每线程 1 像素会超过 65535 个 workgroup 的上限，要每线程处理 4 像素；
   - u32 累加存在溢出上限；
   - 必须上报 `adapterInfo`、`gpuErrors`、`gpuWarnings`、`deviceLost`，`renderFrame()` 在设备丢失后要返回 false。
4. **软件渲染的性能**：SwiftShader 上 compute 光栅约 0.7 s / 1 M 点（r13 实测）。CI 只能验证**正确性**；性能基线要分档记录（Tier S 与独显不能混比）。
5. **three 版本**：voxelkloud 的 peer 是 three ^0.180，3DTilesRendererJS 的 dev 依赖是 ^0.185，我们用 r186。3DTilesRendererJS 的 `PointCloudMaterial` 走 `onBeforeCompile`，在 WebGPURenderer 下无效；它的网格瓦片（GLTFLoader 产出的标准材质）在 WebGPURenderer 下能否正常工作，需要在 V0.8 前实测。
6. **OLV 是 AGPL**（本项目科研用途，按要求忽略许可）。它的代码量很大（165 MB 仓库）；渲染层是实例化 sprite（慢路径），**只 port 策略函数**。
7. **COPC 的服务器配置**：CORS 必须允许 `range` 头；206 响应不压缩；不支持 Range 的主机只对 64 MiB 以内的文件退化为整文件读取；laz-perf wasm 堆的内存占用；LAS 1.4 的点格式 6–8 不能用 loaders.gl LASLoader。
8. **3DGS 的渲染上下文冲突**：Spark 只支持 WebGLRenderer，Aholo 是自研引擎，Visionary 自带渲染管线。**同一画布只能选一个渲染上下文**，3DGS 高保真预览应走独立路由（与 r13 一致）。
9. **Rerun**：web-viewer 版本必须与 SDK 版本一致；wasm 包体积大（本单元未测）；只适合 dev 路由。
10. **数据现实**：单城 5 M 点在独显上可以全部驻留，因此“流式与疏密调节”的效果在独显上不明显。演示和测试要使用多城压力场景、Tier S 档或限速网络（voxelkloud 用 2.5 MB/s、20 Mbit 做收敛竞速），否则难以证明机制有效。
11. **UrbanScene3D 的 PLY 没有颜色**（r09、r13）：着色模式默认为高程色带或法线着色，配合 EDL；compute 路径的 `col` 通道写高程色带。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§14“远处 10K 点 / 近处 1M 点”应改成可控的闭环。** 建议写成：
   - “按屏幕空间误差 τ（设备像素）加点预算 B 做两级 best-first 选择；
   - 质量阶梯联动 renderScale、B、τ；
   - 输出 `limitedBy` 和 `achievedScreenError` 遥测”；
   - 同时写入 §3.1、§3.6 的默认值：τ=1.35 px，B 由阶梯决定，h=0.15，最多再深 2 级。
2. **§9、§11、§12、§34 的“WebGPURenderer 为主”需要修正为三级渲染**（Tier A / B / S，见 §4 末）。原文把“WebGPU = Point Rendering”写成默认成立，但有三点事实：
   - WebGPU 的点图元只有 1 px；
   - WebGPURenderer 的 WebGL2 后端把 `gl_PointSize` 写死为 1；
   - WebGL2 没有 compute，也没有 atomics。

   因此点云必须在 WebGPU 档用 compute 光栅，在回退档用经典 WebGLRenderer 的 `gl_PointSize`。
3. **§15 数据格式要明确分层**：
   - 运行时：Potree 2.0 兼容格式（+ `ANET_Q16`）；
   - 归档与交换：COPC（LAS 1.4，保留 CRS 和 GPS 时间，方便 V0.5 的 RTK/时间对齐）；
   - GIS 导出：3D Tiles 1.1；
   - 3DGS：3D Tiles + `KHR_gaussian_splatting(+SPZ)` 作为互操作格式，SOG 或 RAD 作为 Web 运行时格式。
4. **§16 场景结构中的 PointCloud 要注明组合方式**：点云 resolve 先写深度（`renderOrder=-1`），无人机、天气、任务图层随后做深度测试；EnvironmentLayer 的透明粒子最后渲染。
5. **§37 刷新频率**：
   - “60 FPS”改为“呈现间隔 p50 ≤ 1.35×刷新周期（自动降档），p95 ≤ 1.1×刷新周期（允许升档）”；
   - 增加**按需渲染**（dirty 标志）：相机静止、没有遥测更新、没有待上传数据时不渲染，WebSocket 遥测到达时置 dirty；
   - 遥测插值与渲染解耦。
6. **§38 UI 要增加“性能与质量”面板**（shadcn `Sheet` / `Popover` + lieflat 风格表格和迷你图），字段包括：
   - 光栅器（compute / points）、质量档位（可设为自动或手动 7 档）、B、τ；
   - 选中点数与驻留点数、驻留 MB、`limitedBy`、`achievedScreenError`；
   - 飞行中、排队、失败的节点数，GPU 耗时（timestamp-query），adapter 信息，设备丢失提示。

   自动画质移动档位时，用 toast 和图标说明原因（遵循“用户能看到自己在哪一档”的原则）。
7. **§41 World Package 的点云目录应细化为**：
   ```text
   geometry/pointcloud/
   ├── metadata.json / hierarchy.bin / octree.bin   # 运行时
   ├── copc/<name>.copc.laz                          # 归档
   └── stats.json                                    # 每层节点数与点数、D、地面百分位、推荐 τ/B
   ```
   `stats.json` 供质量阶梯的初始档和多源仲裁使用。
8. **§43/§44 的 MVP 应补充三项**：
   - 流式鲁棒性：取消、退避、驱逐滞回、设备丢失诊断；
   - 性能 e2e：收敛竞速（首次出图、过半、稳定三个时间点），外加 INP、p50/p95 呈现间隔，限速网络；
   - 多城压力场景（6 城约 30 M 点加 N 架无人机）。

   V0.1 的验收应包含“Tier S 下 20–30 fps、Tier B 集显下 60 fps”这类分档指标。
9. **§8 Visual World 应加入 2026 年的 3DGS LoD 事实**：
   - Spark 2.0（2026-04，`.RAD`，连续 LoD 树）、PlayCanvas Streamed SOG、Cesium 3DGS 3D Tiles（2026-04）已经成熟；
   - V0.8 的“点云 → 3DGS”应以“离线 LOD 构建 + 流式 + 与点云共用预算仲裁”为设计单位，不再按单个 PLY 设计。
10. **新增“全局渲染预算仲裁”小节**（放在 §14 与 §16 之间）：
    - 质量阶梯给出总预算；
    - 性价比分配器（§3.9）在点云瓦片、多城、splat 之间分配；
    - 各数据源内部仍按 SSE 选择。

    这样才能在 V0.6 多机、V0.8 3DGS 阶段保持“非常流畅”。
11. **§40 相机与 FPV**：near/far 按“被接纳节点的最小间距”在帧开始时计算（§3.11）；第三人称跟随时 near 取 `min(规则值, 0.5·跟随距离)`；WebGPU 档开启 reversed-Z。
12. **§33/§36 可选增加开发工具**：Rerun web viewer 用于后端仿真日志复盘（后端 `rerun-sdk` 记录 DroneState、航线、传感器视锥），挂在 `/debug` 路由，不进主 UI。
13. **原文细节修正**：
    - §34 的“Point Cloud: Custom Octree / Potree concepts”应写明来源：LOD 与调度来自 voxelkloud 和 Potree，WebGL2 渲染用单缓冲，WebGPU 渲染用 compute 光栅；
    - §15 的“后期兼容 3D Tiles”应说明点云用 ADD 细化、`geometricError = spacing`，这是 3DTilesRendererJS `PotreePlugin` 的做法，能与 Potree 八叉树无损互转。

---

*附：本单元所有 star 与日期均为 2026-09-28 实测（`.cache/research/n01/stars1–4.tsv`），voxelkloud 与 OLV 的性能数字引自其仓库文档（未复测），Bauer 2025 的数字引自论文表 5.1，UrbanScene3D 的盒计数为本单元实测。*
