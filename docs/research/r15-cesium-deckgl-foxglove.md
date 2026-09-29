# R15 研究笔记：CesiumJS / deck.gl / Foxglove Studio（→ Lichtblick + foxglove-sdk）

> 研究单元：r15 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §7（Geographic）、§11、§14–16、§28、§36–41、§43
>
> 仓库快照（路径相对 `refs/web3d/`）：
> - `cesium` @ `b3155a8`（2026-09-25，★15781，`cesium@1.145.0` / `@cesium/engine@26.3.0`，JS + GLSL，WebGL2）
> - `deck.gl` @ `ba16261`（2026-09-25，★14615，master 为 `9.4.0-beta.4`，9.4 正式版发布于 2026-09-05，luma.gl 9.4，TS + GLSL/WGSL）
> - `studio` @ `a8a589b`（2024-03-11，★92，**GitHub 已 archived**）。这个 clone **只有一个 README**：Foxglove 在 2024 年闭源后清空了仓库并重写了历史，已经没有源码可读。
>
> 为了能读到真实代码，本单元额外 clone 了两个仓库（只读，放在 `.cache/research/r15/`）：
> - **lichtblick-suite/lichtblick** @ `590774d`（2026-09-25，★1141，v1.29.1，MPL-2.0）。它是 Foxglove Studio 最后一个开源版本（v1.87）的社区 fork，由 Bosch 等维护，2026 年仍在活跃开发。Player、MessagePipeline、面板 API、时间轴、3D 面板这些架构都和原版一脉相承。
> - **foxglove/foxglove-sdk** @ `dcbc667`（2026-09-24，★311，PyPI `foxglove-sdk==0.27.0`，Rust 内核，提供 Python/C++/TS 绑定）。这是 Foxglove 目前开源的部分，包括 WebSocket server、MCAP 写入和 PlaybackControl 能力。
>
> 本机实测产物在 `.cache/research/r15/bench/`：
> - `coord_bench.py`：移植 Cesium 的椭球与 ENU 数学，用 pyproj 校验，并量化 float32、平面近似、UTM、Web Mercator 和航向约定带来的误差
> - `fg_mcap_bench.py`：foxglove-sdk 写 MCAP 的吞吐
> - `m4_bench.mjs`：Lichtblick 的时序降采样算法移植后的性能
> - `gcj_offset.py`：GCJ-02 偏移量
>
> 与已有笔记的分工：3D Tiles 的 SSE、遍历、点预算、implicit 格式，以及 3DTilesRendererJS 已经在 **r10** 讲透，本文不重复，只补充 Cesium 在**点云着色（attenuation/EDL）、优先级打包、飞行预取、时间系统、地理坐标**这几方面的实现。WebGL/WebGPU 点渲染的实测见 r11/r12。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **CesiumJS** | 地球级 GIS 引擎：WGS84 椭球、3D Tiles、地形、时间动态数据（CZML/Clock） | **port**（MVP 就要）：椭球与 ENU 数学（`Ellipsoid` + `FixedFrameTransforms`，约 150 行）、点尺寸衰减公式、EDL shader、瓦片优先级位打包、飞行目的地预取、`Clock` 与 `SampledProperty` 插值、Timeline 刻度算法、跟随相机的三种参考系、`requestRenderMode` 按需渲染。**adopt**（V0.5+，可选）：独立的"GIS/地球模式"视图，加载同一份 World Package 3D Tiles | V0.1 port；V0.5 起可选 adopt | ★★★★☆ |
| **Foxglove Studio → Lichtblick + foxglove-sdk** | 机器人数据回放与调试工作台：Player/DataSource 抽象、消息管线、面板 API、时间轴、3D 面板（TF 树） | **port**：`IterablePlayer` 状态机与 tick 算法、seek-backfill、两级缓存（BlockLoader + 预读缓冲）、渲染屏障（render barrier）、面板 `onRender(state, done)` 协议、M4 时序降采样、TF 插值、decay-time 点云累积、播放性能 HUD。**adopt**：`foxglove-sdk`（Python）做仿真服务的**调试旁路**和 **MCAP 录制**，Foxglove/Lichtblick 直接连上去就能调试。**skip**：Lichtblick 的 UI（MUI 体系，和 shadcn 冲突） | V0.2（回放、遥测面板、录制） | ★★★★☆ |
| **deck.gl** | 大规模地理数据图层（React 友好），WebGPU 实验支持 | **port**：`TripsLayer` 的 GPU 时间窗轨迹、`DataFilterExtension` 的 GPU 软范围过滤、`PointCloudLayer` 在 WebGPU 下用实例化三角形画点的做法、offset/RTC 精度模式、React 与命令式引擎的同步方式。**adopt**（V0.5+，可选）：2D 地图/小地图面板（MapView + MapLibre/天地图 + TripsLayer + IconLayer） | V0.2 port；V0.5 起可选 adopt | ★★★☆☆ |
| `refs/web3d/studio`（原仓库） | 空壳，只有 README | **skip** | — | ☆ |

**关键结论（实现者先读这几条）**

1. **坐标系统一方案（§3.1）**：World Runtime 的**唯一规范坐标系是"World ENU"**：以 World Package 锚点为原点的局部东北天切平面，Z-up，单位米，右手系，CPU 侧用 float64。WGS84/CGCS2000 大地坐标、ECEF、UTM/高斯-克吕格、NED/FRD（PX4）、Three.js 渲染坐标都只作为**边界上的转换**。ENU 与 ECEF 之间是严格刚体变换（Cesium `eastNorthUpToFixedFrame`）。移植实现与 pyproj 的最大差异为 **1.0e-9 m**（`coord_bench.py`）。
2. **量化出来的精度坑（本机实测）**：
   - float32 直接存 ECEF 的误差约 **0.30 m**，存 10 km 范围内的 ENU 只有 **0.69 mm**，50 km 范围 2.7 mm。
   - 切平面 z 与大地高的差是 d²/2R：5 km 处 **1.96 m**，10 km 处 **7.8 m**。
   - UTM 50N 在合肥的尺度因子 k=0.999606，每 km 差 **−394 mm**；CGCS2000 3° 高斯-克吕格 CM117E 在合肥只有 **+5.7 mm/km**。
   - Web Mercator 的"米"在合肥被放大 **1.177 倍**，旧金山 1.265 倍。
   - GCJ-02（高德等国内底图）相对 WGS84 在合肥偏 **约 564 m**。
   - EGM2008 大地水准面差距：合肥 N≈**−4.6 m**，旧金山 **−32.2 m**。

   结论：**物理量一律在 World ENU 下计算；不允许把 Web Mercator 或 UTM 坐标当米用；PX4 的 AMSL 高和 RTK 的椭球高必须分字段存储**。
3. **航向约定三套不同，必须写死换算**：
   - PX4/NED 的 yaw：0 = 北，顺时针为正。
   - ENU/REP-103 的 yaw：0 = 东，逆时针为正，`yaw_enu = π/2 − yaw_ned`。
   - Cesium 的 HPR heading：绕 −Z 旋转，0 = 东，顺时针为正，`heading_cesium = yaw_ned − π/2`（源码见 `Quaternion.fromHeadingPitchRoll`，结果见 §3.1.5 实测表）。
4. **点尺寸衰减是"疏密自动调节"的视觉补偿（§3.3）**。Cesium 的公式是 `pointSize = min(GE·scale · H / (depth · 2tan(fovy/2)), maxAttenuation·dpr)`，也就是**点直径 = 该瓦片自身的 SSE（像素）**。点预算把深层节点截掉后，叶子的 GE 变大，点会自动变大，表面仍然闭合，这样"点少了但画面不破"。ADD 细化时默认上限 5 px。Cesium 没有下限，我们补一个 `max(…, 1px)`。
5. **UrbanScene3D 没有颜色，EDL 是刚需（§3.4）**。Cesium 的 EDL 只采样上下左右 4 个邻居：`response = mean(max(0, log2(d) − log2(d_nbr)))`，`shade = exp(−300·strength·response)`，默认 strength 1、radius 1·dpr。一次 MRT（颜色 + 打包深度）加一个全屏 quad 就能完成，开销很小。
6. **回放 Player 直接移植 Lichtblick 的 `IterablePlayer`（§3.7）**：
   - 状态机：`preinit → initialize → start-play → idle ⇄ play`，另有 `seek-backfill` 和 `reset-playback-iterator`。
   - 每个 tick 读取的时间窗 `range = min(Δt_wall·speed, 300 ms)`，并做 EMA 平滑（0.9/0.1）。
   - seek 时先取每个 topic 在目标时刻之前的最后一条消息（backfill）。100 ms 内没完成，就先发一帧空消息并标记 BUFFERING 作为反馈。
   - 两级缓存：BlockLoader（≤100 块、每块 ≥0.1 s，给曲线图用全量预载）和 BufferedIterableSource（预读 10 s、至少 1 s，给播放用）。
   - 倍速档位 `[0.01,0.02,0.05,0.1,0.2,0.5,0.8,1,2,3,5]`。方向键步进默认 100 ms，Alt 为 500 ms，Shift 为 10 ms。
7. **渲染屏障防止"数据比画面快"（§3.8）**。Player 发出一帧状态后要等 UI 调用 `done()`，才会发下一帧；面板可以用 `pauseFrame()` 挂起异步渲染，最长等 5 s。我们的 WebSocket 客户端同样要做"按渲染 tick 合并、每个 topic 只取最新一条"，Lichtblick 2026 年新加的 `samplingRequest: latest-per-render-tick` 就是这个语义。
8. **轨迹回放用 GPU 时间窗（§3.10）**。deck.gl `TripsLayer` 的做法：每个顶点带时间戳，片元里插值出 `vTime`，满足 `vTime > now` 或 `vTime < now − trail` 就 discard，alpha 按 `1 − (now − vTime)/trail` 淡出。每帧只改一个 uniform，完全不重建几何。float32 时间必须以会话起点为基准：8192 s 时 ulp≈0.98 ms，24 h 时 7.8 ms，若用 Unix 秒则 ulp 达 128 s，完全不可用。
9. **仿真服务加一个 foxglove-sdk 调试旁路，并全程录 MCAP（§3.19）**。本机实测：20 架 × 50 Hz × 60 s 共 123,000 条消息，写入耗时 6.1 s，约 20k msg/s，**9.9 倍实时**，文件 5.8 MB，平均 **47 B/条**。Lichtblick/Foxglove 可以直接连 `ws://…:8765`，免费得到曲线、TF、3D、日志等调试面板。前端主 UI 仍走我们自己的 shadcn 数字沙盘。
10. **deck.gl 和 Cesium 都不进 MVP 主渲染路径**。两者都要独占一个 WebGL 上下文，无法和 Three.js WebGPURenderer 共用画布。Cesium 目前**没有 WebGPU 后端**（源码里 grep 不到 `webgpu`）；deck.gl 的 WebGPU 官方仍标为 *experimental, not recommended for production*。MVP 只移植它们的算法和数据契约，V0.5 地理配准完成后，再按需把它们作为"GIS 模式"或"2D 地图面板"的**独立视图**接入。

---

## 1. 仓库概览

| 项 | CesiumJS | deck.gl | Foxglove Studio（原仓库） | Lichtblick（fork） | foxglove-sdk |
|---|---|---|---|---|---|
| 最后提交 | 2026-09-25 | 2026-09-25 | 2024-03-11（archived） | 2026-09-25 | 2026-09-24 |
| Star | 15,781 | 14,615 | 92（重建后的空仓） | 1,141 | 311 |
| 版本 | 1.145.0 / engine 26.3.0 | 9.4.0（v9 系列最后一版，v10 将换 luma.gl v10 和 loaders.gl v5） | v2.9.0 tag（只有 README） | 1.29.1 | 0.27.0 |
| 语言与构建 | ES Module，gulp + esbuild，monorepo（`packages/engine`、`packages/widgets`、`packages/sandcastle`） | TS monorepo（lerna/yarn），`modules/*` 共 17 个包 | — | TS monorepo（yarn workspaces，web + electron） | Rust 核心，Python 用 maturin wheel，另有 C/C++/TS |
| GPU 后端 | WebGL2 | WebGL2 为主，WebGPU 实验（9.4 宣称官方图层目录全部可跑） | — | three@0.156（打过补丁），WebGL | — |
| UI 框架 | 自带 Widgets（knockout） | `@deck.gl/react`，框架无关 | — | React 18 + MUI 7 + react-mosaic + zustand 4 + chart.js 4 + leaflet | — |
| 与本项目关系 | 地理数学、点云着色、时间系统的参考实现；V0.5 以后的 GIS 模式 | 轨迹与过滤 shader 的参考；V0.5 以后的 2D 地图 | 无代码 | **回放与面板架构的主要参考** | **后端调试旁路和录制，直接依赖** |

**CesiumJS**：源码在 `packages/engine/Source/`，下分 `Core`（数学、椭球、时间、插值）、`Scene`（3D Tiles、Model、点云、体素、高斯、相机）、`DataSources`（Entity、CZML、Property 时间插值）、`Renderer`（WebGL 封装）、`Shaders`（GLSL）、`Workers`。Widgets（`Animation`、`Timeline`）在 `packages/widgets/Source/`。2026 年的主要变化有：Gaussian splat（SPZ、>16M splat 修复）、`BufferPointCollection`、`BufferPolylineCollection`、`BufferPolygonCollection`（面向大批量动画点线面的高性能原语），以及 MVT/GeoJSON 贴合到 3D Tiles 上。

**deck.gl**：9.4 的亮点有三个。第一，所有官方图层（包括 `Tile3DLayer` 点云）都支持 WebGPU；第二，`GlobeView` 支持 pitch/bearing；第三，新增 `ViewLayout` 多视图布局、多画布（`canvasId`），以及 `@deck.gl/maplibre`（支持 MapLibre v4–v6）。点云相关代码在 `modules/layers/src/point-cloud-layer`，轨迹在 `modules/geo-layers/src/trips-layer`，GPU 过滤在 `modules/extensions/src/data-filter`，投影和精度在 `modules/core/src/shaderlib/project`。

**Foxglove / Lichtblick**：`foxglove/studio` 从 v2 起闭源，开源仓库被清空。Lichtblick 由最后一个 MPL 版本 fork 而来，保持原有架构，并在 2025–2026 年持续演进，新增了 `samplingRequest`、视频 GOP backfill、OpenTelemetry 等。foxglove-sdk 是 Foxglove 现在开源的"数据侧"：WebSocket server（ws-protocol v1，含 2026 年新增的 `PlaybackControl` 能力）、MCAP writer、标准 schema（`FrameTransform`、`LocationFix`、`PointCloud`、`SceneUpdate`、`VoxelGrid` 等）。

---

## 2. 源码结构与关键模块

### 2.1 CesiumJS：本项目要读的部分

| 文件（相对 `packages/engine/Source/`） | 关键函数/类 | 要点 |
|---|---|---|
| `Core/Ellipsoid.js` | `Ellipsoid.WGS84 = (6378137, 6378137, 6356752.3142451793)`、`cartographicToCartesian`、`geodeticSurfaceNormal`、`cartesianToCartographic`（先 `scaleToGeodeticSurface` 投到椭球面，再用法线求经纬、有符号距离求高） | 大地 ↔ ECEF 的参考实现。`cartographicToCartesian` 的公式：`n` 为大地法线，`k = R²∘n`，`γ = √(n·k)`，结果为 `k/γ + h·n` |
| `Core/FixedFrameTransforms.js` | `localFrameToFixedFrameGenerator(first, second)`、`eastNorthUpToFixedFrame`、`northEastDownToFixedFrame`、`headingPitchRollToFixedFrame`、`headingPitchRollQuaternion`、`fixedFrameToHeadingPitchRoll`、`rotationMatrixFromPositionVelocity` | ENU 帧：`up = normalize(p∘(1/R²))`，`east = normalize(−p.y, p.x, 0)`，`north = up × east`；矩阵各列依次为 E、N、U、原点。极点和原点有退化处理。同一个生成器还能产出 NED/NWU 等任意组合，缓存在 `localFrameToFixedFrameCache` |
| `Core/Quaternion.js` | `fromHeadingPitchRoll`：先 roll 绕 +X，再 pitch 绕 **−Y**，最后 heading 绕 **−Z** | 所以 Cesium 的 heading 是"0 = 局部 +X（东），顺时针为正"，和 PX4 yaw 相差 90° |
| `Core/EncodedCartesian3.js` + `Shaders/Builtin/Functions/translateRelativeToEye.glsl` | `encode`：`high = floor(v/65536)·65536`，`low = v − high`；GPU 端计算 `(high − camHigh) + (low − camLow)` | GPU RTE（relative-to-eye），用两个 float 模拟 double。本机实测误差 0.56 mm |
| `Scene/PointCloudShading.js` | `attenuation=false`、`geometricErrorScale=1`、`maximumAttenuation`（未设置时 ADD 取 5，REPLACE 取 `memoryAdjustedSSE`）、`baseResolution`、`eyeDomeLighting=true`、`eyeDomeLightingStrength=1`、`eyeDomeLightingRadius=1`、`backFaceCulling=false`、`normalShading=true` | 点云着色参数 |
| `Scene/Model/PointCloudStylingPipelineStage.js` + `Shaders/Model/PointCloudStylingStageVS.glsl` | uniform `model_pointCloudParameters = (maxSize·dpr, GE·scale, H/sseDenominator, timeSinceLoad)`；`getPointSizeFromAttenuation`；`getGeometricError`（瓦片 GE 优先，其次 `baseResolution`，否则用 `cbrt(包围盒体积/点数)` 估算） | 点尺寸衰减公式（§3.3） |
| `Scene/PointCloudEyeDomeLighting.js` + `Shaders/PostProcessStages/PointCloudEyeDomeLighting.glsl` | 用 `FramebufferManager` 建两个颜色附件（颜色 + `czm_packDepth` 深度），派生 "EC" shader 写 MRT，最后用全屏 quad 合成，保留原深度 | EDL（§3.4） |
| `Scene/Cesium3DTile.js` | `updatePriority`、`isolateDigits`、`priorityNormalizeAndClamp`、`getPriorityReverseScreenSpaceError`、`priorityDeferred` | 多因子优先级按十进制位打包（§3.5） |
| `Scene/Cesium3DTileset.js` | `preloadFlightDestinations=true`、`cullRequestsWhileMoving`、`foveatedScreenSpaceError`、`progressiveResolutionHeightFraction` 等（参数表见 r10） | 飞行途中按目的地相机预遍历，走 `Cesium3DTilePass.PRELOAD_FLIGHT` |
| `Scene/TimeDynamicPointCloud.js` | `getNextInterval`（`t + avgLoadTime·multiplier`）、`updateAverageLoadTime`（最近 5 次加载耗时的滑动平均，初值 0.05 s）、`getNearestReadyInterval`、`maximumMemoryUsage=256 MB` | 逐帧点云（例如 LiDAR 扫描序列）的回放预取（§3.11） |
| `Scene/VoxelPrimitive.js` + `Shaders/Voxels/*` | GPU octree（`Octree.glsl`）+ megatexture（`Megatexture.glsl`）+ 光线步进（`VoxelFS.glsl`，`STEP_COUNT_MAX=1000`，步长 = 沿光线最小体素尺寸 × `stepSize`）；`screenSpaceError=4` | 环境场 E(x,y,z,t) 体渲染的参考（V0.3–V0.4） |
| `Scene/Scene.js` | `requestRenderMode`、`maximumRenderTimeChange`，`shouldRender = 相机变化 ∨ 显式请求 ∨ |Δ仿真时间| > 阈值 ∨ …` | 按需渲染（§3.16） |
| `Scene/CameraFlightPath.js` | 默认时长 `min(ceil(dist/1e6) + 2, 3) s`，飞行过程中禁用输入 | 相机过渡 |
| `Core/Clock.js`、`ClockStep`、`ClockRange` | `tick()`：`TICK_DEPENDENT` 每帧 +multiplier 秒，`SYSTEM_CLOCK_MULTIPLIER` 按 wall Δt×multiplier 推进，`SYSTEM_CLOCK` 直接取当前时间；`CLAMPED` / `LOOP_STOP` / `UNBOUNDED` | 仿真时钟（§3.6） |
| `DataSources/SampledProperty.js`（+ `SampledPositionProperty`、`LagrangePolynomialApproximation`、`HermitePolynomialApproximation`、`LinearApproximation`） | `getValue`：二分查找，取以当前时刻为中心、`degree+1` 个样本的窗口插值；前向/后向外推有 `NONE`、`HOLD`、`EXTRAPOLATE` 三种，另有 `forwardExtrapolationDuration`；`removeSamples(interval)` | 遥测插值与外推（§3.9） |
| `DataSources/EntityView.js` + `Core/TrackingReferenceFrame.js` | `AUTODETECT`、`ENU`、`INERTIAL`（按实体姿态）、`VELOCITY`（x 沿速度，z 朝上正交化） | 跟随相机的三种参考系（§3.15） |
| `packages/widgets/Source/Timeline/Timeline.js` | `timelineTicScales`（0.001 s … 1000 年的"好看"刻度）、`_makeTics`：主刻度取第一个大于理想值的档位，次刻度取能整除主刻度的最大档位，最小刻度要求 ≥ `smallestTicInPixels=7px` | 时间轴刻度（§3.14） |
| `packages/widgets/Source/Animation/AnimationViewModel.js` | `defaultTicks` 倍速梯（0.001 … 数千倍，正负对称），shuttle ring 角度与倍速之间按对数映射 | 倍速控件参考 |

Cesium 的 3D Tiles 遍历（`Cesium3DTilesetBaseTraversal.js`、`Cesium3DTilesetSkipTraversal.js`）、SSE（`Cesium3DTile.getScreenSpaceError`）、`memoryAdjustedScreenSpaceError` 和动态 SSE 已经在 r10 §2–§3 逐项核对过，这里不再展开。

### 2.2 deck.gl：本项目要读的部分

| 文件（相对 `modules/`） | 关键点 |
|---|---|
| `layers/src/point-cloud-layer/point-cloud-layer.ts` | 属性：`instancePositions`（float64，`fp64: use64bitPositions()` 时拆成 `instancePositions64Low`）、`instanceNormals`、`instanceColors`（unorm8）。几何是**一个外接单位圆的等边三角形**（3 个顶点，半径 2），实例化绘制。`sizeUnits` 可选 `'pixels'`/`'meters'`/`'common'`，`pointSize` 默认 10，`antialiasing` 可选。`normalizeData` 直接吃 loaders.gl 的 `{header, attributes: {POSITION, NORMAL, COLOR_0}}` 二进制布局 |
| `…/point-cloud-layer-vertex.glsl.ts`、`…-fragment.glsl.ts`、`point-cloud-layer.wgsl.ts` | 顶点：`offset = positions.xy · project_size_to_pixel(radius, units)`，`gl_Position.xy += project_pixel_size_to_clipspace(offset)`。开启抗锯齿时顶点半径乘以 `1 + 1/(dpr·r)`，片元用 `fwidth` 做 smoothedge。片元：`length(unitPosition) > 1` 时 discard。**WebGPU 里没有 `gl_PointSize`（point-list 只能画 1px），所以必须用实例化图元**。WGSL 版本在 `point-cloud-layer.wgsl.ts` |
| `geo-layers/src/trips-layer/trips-layer.ts`（+ `trips-layer.wgsl.ts`） | 继承 `PathLayer`。GLSL 用 shader injection：`vs:#main-end` 里算 `vTime = t_i + (t_{i+1} − t_i)·vPathPosition.y / vPathLength`；`fs:DECKGL_FILTER_COLOR` 里做 discard 和淡出。WebGPU 下时间戳打包成 `vec2`（`packTripTimestamps`）。props 有 `currentTime`、`trailLength=120`、`fadeTrail=true`、`getTimestamps` |
| `extensions/src/data-filter/{data-filter-extension.ts, shader-module.ts, aggregator.ts}` | `filterSize` 1–4 维、`filterRange`、`filterSoftRange`（在硬范围内、软范围外时用 smoothstep 淡化，可同时缩小尺寸 `filterTransformSize` 和降低透明度 `filterTransformColor`）、`categorySize`（类别位掩码）、`fp64`，还可通过 `onFilteredItemsChange` 在 GPU 上计数 |
| `core/src/lib/constants.ts`、`core/src/shaderlib/project/{viewport-uniforms.ts, project.glsl.ts}` | `COORDINATE_SYSTEM`：`LNGLAT`、`METER_OFFSETS`、`LNGLAT_OFFSETS`、`CARTESIAN`；`PROJECTION_MODE`：`WEB_MERCATOR`、`WEB_MERCATOR_AUTO_OFFSET`、`GLOBE`、`IDENTITY`。`getOffsetOrigin`：offset 模式下原点取视口中心（`Math.fround`），`originCommon` 在 JS 里用 float64 算好再下发，从而避开 GLSL float32 的加法误差。`project_offset_` 用 `commonUnitsPerWorldUnit + commonUnitsPerWorldUnit2·dy` 做纬向一阶修正 |
| `react/src/deckgl.ts` | 命令式 `Deck` 实例放在 `useRef` 中。`_customRender(redrawReason)` 由 Deck 在 rAF 中调用；**只有 viewports 变化时才 `forceUpdate()`**，让 React 子节点重新定位，其余情况绕过 React 直接 `_drawLayers`。JSX 写的 layer 由 `extractJSXLayers` 收集后走 `setProps` 浅比较 |
| `core/src/transitions/*`、`core/src/controllers/transition-manager.ts` | `FlyToInterpolator`（van Wijk–Nuij，`curve=1.414`、`speed=1.2`、`screenSpeed`、`maxDuration`）、`LinearInterpolator`、CPU/GPU spring（Verlet：`next = cur + v + (dest−cur)·stiffness − v·damping`，帧率相关）、GPU 属性插值过渡（transform feedback） |
| `geo-layers/src/tile-3d-layer/tile-3d-layer.ts` | 依赖 `@loaders.gl/tiles` 的 `Tileset3D`。点云瓦片交给 PointCloudLayer，**`pointSize` 固定（默认 1），没有衰减和 EDL**。在点云质量上不如 Cesium 和 3DTilesRendererJS |

### 2.3 Foxglove Studio / Lichtblick：本项目要读的部分

文件路径相对 `lichtblick/packages/`：

| 文件 | 关键点 |
|---|---|
| `suite-base/src/players/types.ts` | `Player` 接口：`setListener`、`setSubscriptions`、`setPublishers`、`publish`、`callService`，可选的 `startPlayback`/`pausePlayback`/`seekPlayback`/`playUntil`/`setPlaybackSpeed`，以及 `getBatchIterator`、`getBackfillMessages`。`PlayerState = {presence, progress, capabilities, profile, activeData}`。`PlayerPresence` 取值为 `NOT_PRESENT`、`INITIALIZING`、`RECONNECTING`、`BUFFERING`、`PRESENT`、`ERROR`。`activeData = {messages, currentTime, startTime, endTime, isPlaying, speed, lastSeekTime, topics, topicStats, datatypes, totalBytesReceived…}`。`Progress = {fullyLoadedFractionRanges, messageCache: BlockCache, memoryInfo}`。`SubscribePayload = {topic, fields?, preloadType: 'full'|'partial', samplingRequest?}` |
| `suite-base/src/players/constants.ts` | `PLAYER_CAPABILITIES`：`advertise`、`assets`、`callServices`、`setSpeed`、`playbackControl`、`getParameters`、`setParameters`。**能力位决定 UI 显示什么**：实时源没有 `playbackControl`，时间轴就变成只读 |
| `suite-base/src/players/IterablePlayer/IIterableSource.ts` | 数据源只需实现 `initialize() → {start, end, topics, topicStats, datatypes, profile…}`、`messageIterator({topics, start, end, consumptionType})`（异步迭代，产出 `message-event`、`alert`、`stamp` 三种结果）和 `getBackfillMessages({topics, time})`，可选 `getMessageCursor`（带 `nextBatch`、`readUntil`） |
| `suite-base/src/players/IterablePlayer/IterablePlayer.ts` | 状态机和 `#tick()`（§3.7）。常量：`DEFAULT_CACHE_SIZE_BYTES=1e9`（注释说超过 1.5 GB 会让 Linux 渲染进程崩溃）、`START_DELAY_MS=100`、`MIN_MEM_CACHE_BLOCK_SIZE_NS=0.1e9`、`MAX_BLOCKS=100`、`SEEK_ON_START_NS=99ms` |
| `…/BlockLoader.ts` | 块时长 `max(minBlockDurationNs, total/maxBlocks)`；从第一个缺数据的块开始，把"缺同一组 topic"的连续块合并成一次游标读取；超过 cacheSize 时先剔除不再订阅的 topic，仍超则报 `cache-full` 并停止预载 |
| `…/BufferedIterableSource.ts` | 预读缓冲：`readAheadDuration=10s`、`minReadAheadDuration=1s`，在 `readHead` 前方持续填充 |
| `…/ulog/UlogIterableSource.ts` | **PX4 ULog 数据源**（依赖 `@lichtblick/ulog`），可直接回放真机或 SITL 日志 |
| `suite-base/src/components/MessagePipeline/{store.ts, index.tsx, pauseFrameForPromise.ts}` | zustand store：合并所有面板的订阅（`subscriberIdsByTopic`），按订阅者分发消息（`messageEventsBySubscriberId`），新订阅者立即补发该 topic 的最后一条（`lastMessageEventByTopic`）。**渲染屏障**：listener 返回 Promise，React layout effect 调 `renderDone` 后，再等 `msPerFrame − 已用时间`（`msPerFrame = 1000/messageRate`，默认 60），然后等所有 `pauseFrame` 的 Promise（上限 `MAX_PROMISE_TIMEOUT_TIME_MS=5000`），最后 resolve，Player 才能发下一帧 |
| `suite/src/index.ts` | 面板扩展 API：`PanelExtensionContext` 提供 `watch(field)`、`onRender(renderState, done)`、`subscribe([{topic, preload}])`、`advertise`/`publish`、`callService`、`seekPlayback`、`setPreviewTime`、`saveState`、`setVariable`、`setSharedPanelState`、`updatePanelSettingsEditor(SettingsTree)`、`unstable_subscribeMessageRange`、`getMessageAtTime`。`RenderState` 包含 `currentFrame`、`allFrames`（只含 preload 的 topic）、`didSeek`、`currentTime`/`startTime`/`endTime`、`previewTime`（悬停时间轴时的预览时刻）、`colorScheme`、`variables`、`topics` |
| `suite-base/src/context/CurrentLayoutContext/actions.ts` | `LayoutData = {configById, layout: MosaicNode<string>, globalVariables, playbackConfig, userNodes, version}`，布局树与面板配置分开存（IndexedDB：`IdbLayoutStorage.ts`） |
| `suite-base/src/components/PlaybackControls/*`、`PlaybackSpeedControls.tsx` | `SPEED_OPTIONS=[0.01,0.02,0.05,0.1,0.2,0.5,0.8,1,2,3,5]`；`sharedHelpers.jumpSeek`：默认 100 ms，`alt` 500 ms，`shift` 10 ms；空格播放/暂停，←/→ 步进；`ProgressPlot` 画已加载区间，`EventsOverlay` 在时间轴上画事件，`PlaybackBarHoverTicks` 显示悬停刻度，`RepeatAdapter` 实现循环 |
| `suite-base/src/components/TimeBasedChart/downsample.ts` | M4 式降采样（§3.13）：`MINIMUM_PIXEL_DISTANCE=3`、`POINTS_PER_INTERVAL=4`、`MAX_POINTS=5000`，支持增量（`continueDownsample` 带状态） |
| `suite-base/src/panels/Plot/{ChartRenderer.worker.ts, OffscreenCanvasRenderer.ts, PlotCoordinator.ts}` | chart.js 在 Worker 里的 OffscreenCanvas 上渲染，主线程只转发数据和交互 |
| `suite-base/src/panels/ThreeDeeRender/transforms/{TransformTree.ts, CoordinateFrame.ts, Transform.ts}` | 每个 frame 保存一段有序的 `(time, transform)` 历史，默认容量 `DEFAULT_MAX_CAPACITY_PER_FRAME=10000`；写满时一次剔除 25%，或剔除 `maxStorageTime` 之前的全部（取两者中较多的）。`findClosestTransforms` 先二分查找，越界时在 `maxDelta` 内钳到首尾。`Interpolate` 对平移做 lerp、对旋转做 slerp。`apply` 路径：srcFrame@srcTime → root(fixed) → dstFrame@dstTime |
| `…/Renderer.ts` | `fixedFrameId`（世界固定帧）、`followFrameId`（跟随帧），渲染帧 = 跟随帧（不存在时退回固定帧）；`queueAnimationFrame()` 按需渲染；每帧对 `sceneExtension.startFrame(currentTime, renderFrameId, fixedFrameId)` |
| `…/renderables/pointExtensionUtils.ts`（`RenderObjectHistory`）、`PointClouds.ts`、`colorMode.ts` | `decayTime>0` 时保留历史帧，每帧按各自消息时刻的位姿重新放置，超时即出队并 dispose（§3.12）。`colorMode` 取值 flat、gradient、colormap（turbo LUT 或 rainbow）、rgb、rgba、rgba-fields，`min/maxValue` 为空时自动统计范围。`pointShape` 为 circle 或 square，默认点大小 1.5 |
| `suite-base/src/panels/PlaybackPerformance/index.tsx` | 5 s 窗口的 sparkline：×realtime（播放时间增量/墙钟增量）、帧率、每帧推进的播放时长、Mbps |
| `suite-base/src/players/FoxgloveWebSocketPlayer/index.ts` | 服务端有 `time` 能力时，以服务端时钟为准；时钟回退视为重置并清空队列；两次 emit 之间的消息队列上限 400 MB（`CURRENT_FRAME_MAXIMUM_SIZE_BYTES`），超了就从头丢到 80%；`#emitState` 用 `debouncePromise` 防重入 |
| `message-path/src/{grammar.ne, types.ts}` | 消息路径语法 `/topic.field[0].x{id==3}.@derivative`：由 name、slice、filter 组成，可带修饰符，支持 `$变量` |

foxglove-sdk（相对 `foxglove-sdk/python/`）：
- `foxglove-sdk-examples/ws-server/main.py`：`foxglove.start_server(...)`、`Channel(topic, message_encoding="json")`、`ServerListener.on_subscribe/on_unsubscribe`，以及类型化通道 `FrameTransformsChannel`、`LocationFixChannel`、`RawImageChannel`。
- `foxglove-sdk-examples/ws-playback-control-mcap/{main.py, playback_source.py}`：**服务端回放控制**。`Capability.PlaybackControl` 配合 `playback_time_range`，`on_playback_control_request(req)` 收到 `seek_time`、`playback_speed`、`playback_command`，返回 `PlaybackState(current_time, playback_speed, status, did_seek, request_id)`；`server.broadcast_time(t)`。`PlaybackSource` 抽象基类有 `time_range`、`play`、`pause`、`seek`、`set_playback_speed`、`status`、`current_time`、`log_next_message`。
- `write-mcap-file/`：`foxglove.open_mcap(path)` 打开后，所有 Channel 的 `log()` 会自动写入 MCAP。
- `schemas/jsonschema/*.json`：标准消息 schema。

---

## 3. 可复用算法与实现（含伪代码与参数）

### 3.1 坐标系统一方案（WGS84 / CGCS2000 / ECEF / UTM / ENU / 本地 / 机体）

#### 3.1.1 坐标系清单

| ID | 名称 | 定义 | 用途 | 精度与存储 |
|---|---|---|---|---|
| **GEO** | 大地坐标 | WGS84（EPSG:4979，经度/纬度/**椭球高 h**）。国内 RTK/CORS 常用 CGCS2000（EPSG:4480），与 WGS84 的差异为厘米级，可视化层面视为相同 | RTK/GNSS 输入、外部 GIS 交换、UI 显示 | float64；角度用度（显示）或弧度（计算） |
| **ECEF** | 地心地固 | EPSG:4978 | 3D Tiles `root.transform`、Cesium、多 World 拼接 | float64；**禁止在 GPU 上直接使用** |
| **PROJ** | 平面投影 | UTM（EPSG:326xx）或 CGCS2000 3° 高斯-克吕格（EPSG:4534–4554，例如合肥用 4548 CM117E） | 与测绘/PDAL/LAS 成果交换、DEM 栅格 | 尺度因子不为 1（§3.1.4），**不作为物理坐标** |
| **WORLD** | **World ENU（规范坐标系）** | 以 `coordinate.json` 锚点 O(lon₀, lat₀, h₀) 为原点的东北天切平面，X=东、Y=北、Z=上，米，右手系 | **所有**几何、物理、规划、环境场 E(x,y,z,t)、DroneState、WebSocket 负载 | CPU 与服务端 float64；GPU 端用瓦片局部 float32 或量化值 |
| **TILE** | 瓦片局部 | 3D Tiles 瓦片内容坐标，相对瓦片中心，与 WORLD 之间只有平移（刚体、无缩放，见 r10 §6） | 点云、网格、3DGS 的 GPU 顶点 | float32 或 `KHR_mesh_quantization` uint16 |
| **RENDER** | 渲染坐标 | Three.js 场景坐标 = WORLD（设 `Object3D.DEFAULT_UP=(0,0,1)`，保持 Z-up，不做轴交换） | 相机、控制器、后处理 | 由 JS float64 算出 `modelView` 再下发 float32，天然是 RTC |
| **NED/FRD** | PX4 约定 | 世界 NED（北东地），机体 FRD（前右下） | PX4 SITL、MAVLink、ULog | 在网关边界换成 ENU/FLU |
| **BODY** | 机体（ROS REP-103） | FLU（前左上） | DroneState 姿态、传感器外参 | 四元数 `[x,y,z,w]`，表示 WORLD←BODY |
| **SENSOR** | 传感器 | camera_optical（RDF）、lidar（MID-360 FLU） | 外参链 BODY←SENSOR | 静态 TF |

帧树（沿用 Foxglove/ROS TF 语义，§3.1.6）：

```text
earth(ECEF) ──static── world(ENU @anchor) ─┬─ map(=world, 可带重定位漂移校正)
                                           ├─ uavNN/odom ── uavNN/base_link(FLU) ─┬─ uavNN/camera_optical
                                           │                                        ├─ uavNN/lidar
                                           │                                        └─ uavNN/gimbal ...
                                           └─ tiles/<id>（静态平移）
```

#### 3.1.2 规则（写进系统架构说明书）

- **R1** World Runtime 内部只有一个度量坐标系：WORLD（ENU，米，Z-up）。环境场查询 `environment.query(x,y,z,t)` 的 x、y、z 即 WORLD 坐标。
- **R2** GEO↔WORLD 一律走 ECEF 做严格变换：`p_ecef = geodeticToEcef(lon,lat,h)`，`p_world = M_enu⁻¹·p_ecef`。**禁止**用 `Δlat·111320` 这类局部线性近似。线性近似在 5 km 处会带来米级误差，而且不可逆。
- **R3** 高程分三个字段：`h_ellipsoid`（RTK 与 GNSS 原生）、`h_msl`（PX4 `altitude_msl_m`，基于 EGM96/EGM2008）、`z_world`（切平面）。换算关系：`h_msl = h_ellipsoid − N(lon,lat)`，`z_world ≈ h_ellipsoid − h₀ − d²/(2R)`（精确值需走 ECEF）。
- **R4** 不把 UTM、高斯-克吕格、Web Mercator 当米使用。它们只用于交换、栅格对齐和底图。
- **R5** 国内底图坐标：天地图为 CGCS2000，可直接使用；高德、腾讯为 GCJ-02，百度为 BD-09，必须在底图层做反偏（偏移约 0.5–0.6 km），**不能**把这个偏移写进 World。
- **R6** GPU 上只允许出现"相对量"：瓦片局部坐标（加上 float64 算出的矩阵）、相机相对坐标。shader 里**禁止用大数值的 worldPosition 做运算**。例如雾、风场采样用的 `positionWorld` 必须基于 ENU 小数值，不能基于 ECEF。
- **R7** 姿态四元数统一表示 WORLD(ENU)←BODY(FLU)。PX4 网关负责 NED/FRD → ENU/FLU 的换算。航向显示用"北为 0°、顺时针"（航空习惯），计算时用 ENU yaw。
- **R8** 时间统一为 int64 纳秒（`t_sim_ns`）。渲染端使用 float64 秒，以会话起点为基准；GPU 端使用 float32 秒，以会话或块起点为基准（§3.10）。
- **R9** 虚拟数据集（UrbanScene3D 六城）**没有真实地理参考**：`coordinate.json` 用 `anchor.kind = "synthetic"`，指定一个示意锚点（例如 San Francisco 数据集锚到旧金山），并标注 `georeferenced: false`，UI 显示"示意坐标"。导入时先把单位和轴向归一到 ENU 米（Chicago 疑似 km，Suzhou 轴向存疑，见 r10）。
- **R10** 所有跨系统的接口（WebSocket、REST、MCAP、World Package）都显式携带 `frame_id`。

#### 3.1.3 `coordinate.json`（World Package）建议 schema

```json
{
  "version": 1,
  "world_frame": "world",
  "anchor": {
    "kind": "rtk",                     // "rtk" | "survey" | "synthetic"
    "datum": "CGCS2000",               // "WGS84" | "CGCS2000"
    "lon_deg": 117.2272, "lat_deg": 31.8206, "h_ellipsoid_m": 30.0,
    "epoch": "2026.70"                 // 可选：板块运动历元（RTK 厘米级对齐时需要）
  },
  "georeferenced": true,
  "vertical": { "geoid": "EGM2008", "N_at_anchor_m": -4.63 },
  "enu_to_ecef": [ /* 16 个 float64，列主序 = eastNorthUpToFixedFrame(anchor) */ ],
  "projection_hint": { "epsg": 4548, "name": "CGCS2000 / 3-degree GK CM 117E" },
  "import_transform": {                // 原始数据 → WORLD 的刚体 + 单位 + 轴向
    "unit_scale": 1.0, "axis": "ENU", "rotation_quat_xyzw": [0,0,0,1], "translation_m": [0,0,0]
  },
  "extent_world_m": { "min": [-2500,-4000,-10], "max": [2500,4000,620] }
}
```

#### 3.1.4 实测数字（`coord_bench.py`，锚点合肥 117.2272E 31.8206N）

| 项目 | 结果 | 结论 |
|---|---|---|
| Cesium `cartographicToCartesian` / ENU 移植与 pyproj EPSG:4979→4978 的差 | **1.04e-9 m** | 移植正确，可直接用 |
| float32 存 ECEF 绝对坐标 | 最大 **300–304 mm** | GPU 上不能存 ECEF |
| float32 存 ENU（范围 500 m / 2 km / 10 km / 50 km） | 0.025 / 0.086 / **0.69** / 2.7 mm | 单城市 World 直接用 ENU float32 足够 |
| Cesium RTE（high/low 双 float） | 0.56 mm | 需要地球级时才用 |
| 切平面 z 与大地高之差（距离 0.5/1/5/10/20 km） | 0.02 / 0.078 / **1.96** / 7.8 / 31.3 m | 高程必须走 ECEF 精确换算；风场和 DEM 叠加时要考虑 |
| UTM 50N 尺度因子 k（合肥） | 0.999606，每 km **−394 mm** | 不能把 UTM 距离当真实距离 |
| UTM 49N（深圳） | 1.000822，每 km **+827 mm** | 同上 |
| CGCS2000 3° GK CM117E（合肥） | 1.0000057，每 km **+5.7 mm** | 国内测绘交换优先用 GK 3° |
| Web Mercator 尺度 1/cos φ | 合肥 **1.177**，旧金山 1.265，深圳 1.083 | deck.gl/MapLibre 的"米"必须换算 |
| GCJ-02 相对 WGS84 的偏移 | 合肥 **564 m**，深圳 606 m，上海 481 m | 只能在底图层处理 |
| EGM2008 大地水准面差距 N | 合肥 **−4.63 m**，旧金山 −32.16 m，深圳 −3.25 m | AMSL 与椭球高必须分字段 |
| float32 时间 ulp（600 s / 3600 s / 8192 s / 24 h / Unix 1.79e9 s） | 0.06 / 0.24 / 0.98 / 7.8 ms / **128 s** | GPU 时间必须以会话起点为基准 |

#### 3.1.5 可移植实现（TS，约 120 行；Python 端用同一套公式或 pyproj）

```ts
// apps/web/src/geo/wgs84.ts —— 逐行对应 Cesium Ellipsoid / FixedFrameTransforms
const A = 6378137.0, B = 6356752.3142451793;
const R2 = [A*A, A*A, B*B], INV_R2 = [1/(A*A), 1/(A*A), 1/(B*B)];

export function geodeticToEcef(lonDeg: number, latDeg: number, h: number): Vec3 {
  const lon = lonDeg*DEG, lat = latDeg*DEG, cl = Math.cos(lat);
  const n = [cl*Math.cos(lon), cl*Math.sin(lon), Math.sin(lat)];     // geodeticSurfaceNormalCartographic
  const k = [R2[0]*n[0], R2[1]*n[1], R2[2]*n[2]];
  const g = Math.sqrt(n[0]*k[0] + n[1]*k[1] + n[2]*k[2]);
  return [k[0]/g + h*n[0], k[1]/g + h*n[1], k[2]/g + h*n[2]];
}

export function enuToEcefMatrix(o: Vec3): Mat4 {                     // eastNorthUpToFixedFrame
  const up = normalize([o[0]*INV_R2[0], o[1]*INV_R2[1], o[2]*INV_R2[2]]);
  const east = normalize([-o[1], o[0], 0]);                           // 极点(o.x=o.y=0)时需特殊处理，照抄 Cesium 退化分支
  const north = cross(up, east);
  return colMajor(east, north, up, o);                                // [E N U O]
}

// ECEF→大地：Cesium 使用 scaleToGeodeticSurface（牛顿迭代）；这里给出 Bowring 闭式（误差 < 1 mm @ |h|<10 km）
export function ecefToGeodetic(p: Vec3): [lonDeg: number, latDeg: number, h: number] { /* ... */ }

export class WorldFrame {                                             // 单例：由 coordinate.json 构造
  readonly M: Mat4; readonly Minv: Mat4;                              // ENU→ECEF 及其逆（刚体，逆 = 转置旋转 + 平移）
  toWorld(lon: number, lat: number, h: number): Vec3 { return xform(this.Minv, geodeticToEcef(lon, lat, h)); }
  toGeo(p: Vec3) { return ecefToGeodetic(xform(this.M, p)); }
}

// PX4 NED/FRD ↔ ENU/FLU（MAVROS 约定）
export const nedToEnu = ([n, e, d]: Vec3): Vec3 => [e, n, -d];
// q_enu_flu = Q_NED2ENU ⊗ q_ned_frd ⊗ Q_FRD2FLU（MAVROS 约定，见 refs/sim/mavros/mavros/src/lib/ftf_frame_conversions.cpp）
// Q_NED2ENU = quaternion_from_rpy(π, 0, π/2)（NED_ENU_Q）；Q_FRD2FLU = quaternion_from_rpy(π, 0, 0)（AIRCRAFT_BASELINK_Q）
export const yawNedToEnu     = (y: number) => wrapPi(Math.PI/2 - y);   // 实测：0→90，90→0，180→-90
export const yawNedToCesium  = (y: number) => wrapPi(y - Math.PI/2);   // 实测：0→-90，90→0，180→90
export const yawEnuToCompass = (y: number) => wrap360(90 - y*RAD);     // UI 显示：北 0°，顺时针
```

Python 端（`world/georef/frames.py`）用同样的公式实现一份，并用 pyproj 做单元测试。本单元脚本已经验证过两者一致。

#### 3.1.6 TF 缓冲（移植 Lichtblick `CoordinateFrame`）

```ts
class FrameHistory {                 // 每个 frame_id 一条
  times: Float64Array; tfs: Transform[];   // 按时间有序（环形缓冲或有序数组）
  cap = 10_000; maxStorageNs = 30e9;
  add(t, tf) { upsert(t, tf); if (size >= cap) removeBefore(max(at(floor(cap*0.25)).t, maxT - maxStorageNs)); }
  lookup(t, maxDeltaNs, out): boolean {
    const i = binarySearch(times, t);
    if (i >= 0) return copy(out, tfs[i]);
    const hi = ~i, lo = hi - 1;
    if (hi >= size) return (t <= times[size-1] + maxDeltaNs) && copy(out, tfs[size-1]);  // 越界时在 maxDelta 内钳位（HOLD）
    if (lo < 0)     return (times[0] + maxDeltaNs >= t) && copy(out, tfs[0]);
    const f = (t - times[lo]) / (times[hi] - times[lo]);
    out.p = lerp(tfs[lo].p, tfs[hi].p, f); out.q = slerp(tfs[lo].q, tfs[hi].q, f); return true;
  }
}
// 变换链：src@srcTime → root(fixed=world) → dst@dstTime（Foxglove apply 语义）。decay 历史点云依赖这一点（§3.12）
```

### 3.2 渲染精度：选 RTC，不选 RTE

- **Cesium**：地球级场景必须用 GPU RTE（`EncodedCartesian3` + `czm_translateRelativeToEye`），或瓦片 RTC（`CESIUM_RTC`、`root.transform`）。
- **deck.gl**：`WEB_MERCATOR_AUTO_OFFSET` 把视口中心作为 offset 原点，在 JS 里用 float64 算出 `originCommon` 和 `projectionCenter`；需要时再加 `fp64` 的 `*64Low` 属性。
- **本项目（Three.js）**：World 是城市级（≤ 10 km），ENU float32 误差 < 1 mm，**不需要 RTE**。只需要：
  1. 每个瓦片的顶点存局部坐标（相对瓦片中心），`tile.matrixWorld` 在 JS 中用 float64 表示，Three 在 CPU 端算 `modelViewMatrix = camera.matrixWorldInverse · matrixWorld`，再截断为 float32 上传。这本身就是 RTC。
  2. 无人机、轨迹这类动态对象同理。轨迹几何用"块起点"做局部原点。
  3. V0.5 以后如果要加载地球级 3D Tiles（Google/Cesium ion），可以采用 3DTilesRendererJS 的 `ReorientationPlugin`，把 ECEF 瓦片集整体变换到 ENU（r10 已述），或者做相机锚定的浮动原点：相机离原点超过 20 km 时整体平移场景。

### 3.3 点尺寸衰减（Cesium attenuation）：疏密调节的视觉补偿

Cesium 源码（`PointCloudStylingStageVS.glsl`）：

```glsl
// model_pointCloudParameters = (maxAttenuation·dpr, GE·geometricErrorScale, H / (2·tan(fovy/2)), time)
float getPointSizeFromAttenuation(vec3 positionEC) {
  float depth = -positionEC.z;
  return min((geometricError / depth) * depthMultiplier, pointSize);
}
```

这个式子就是 `SSE_px = GE·H/(d·2tan(fovy/2))`（r10 §3.1），也就是说**点的像素直径等于它所属瓦片的屏幕空间误差**。对于 ADD 细化的点云，某层的 GE 等于该层点间距 spacing，所以投影后的点直径约等于投影后的点间距，点与点正好相接，表面闭合。

在本项目中的用法（TSL，WebGPU 与 WebGL2 共用）：

```ts
// apps/web/src/world/pointcloud/material.ts
const uGE        = uniform(0);           // 每个节点：该节点的 spacing（米）；点预算截断后的"叶子"仍用自己的 spacing
const uDepthMul  = uniform(0);           // 每帧：drawingBufferHeight / (2·tan(fovy/2))
const uMaxPx     = uniform(5 * dpr);     // Cesium 在 ADD 下默认 5；建议 4–8·dpr
const uMinPx     = uniform(1 * dpr);     // Cesium 没有下限；3DTilesRendererJS 默认 2，这里取 1
const uScale     = uniform(1.0);         // geometricErrorScale：自适应控制器的"膨胀系数"，范围 0.7–1.6
const depth = positionView.z.negate();
const sizePx = clamp(uGE.mul(uScale).div(depth).mul(uDepthMul), uMinPx, uMaxPx);
// WebGPU 下没有 gl_PointSize，必须用实例化四边形或三角形（§3.17），sizePx 直接作为 billboard 半径输入
```

**与点预算和 FPS 控制器联动（r10 §3.2 的补充）**：控制器除了调 errorTarget 和预算，还可以调 `uScale`。帧时超标时，先减预算（点变少），同时把 `uScale` 调到 1.2–1.4（点变大，维持表面闭合）；帧时有余量时反向调整。点预算已经截掉了深层节点，而叶子的 `uGE` 用的是自身 spacing，所以点**自动变大**。"疏"不等于"破"。

### 3.4 EDL（Eye-Dome Lighting，Cesium 版本）

UrbanScene3D 只有 xyz 和法线（见 r10），EDL 加上高度色带是形体可读性的主要来源。Cesium 的实现（`PointCloudEyeDomeLighting.glsl`）：

```glsl
// pass 1：点云写 MRT —— out0 = 颜色，out1 = packDepth(gl_FragCoord.z 或 log depth)
// pass 2：全屏 quad，只对点云像素处理（stencil 标记 3D Tile 位）
float d   = -eyeZ(depthTex, uv);                         // 当前像素的视空间深度
float l2d = log2(d);
vec2  acc = vec2(0);
for (o in [(-1,0),(1,0),(0,-1),(0,1)]) {                 // 只取 4 邻域
    // 在 floor(radius) 与 ceil(radius) 两处采样后按 fract(radius) 插值，得到亚像素半径
    float dn = mix(depth(uv + o*floor(r)*texel), depth(uv + o*ceil(r)*texel), fract(r));
    if (dn == clear) continue;                           // 背景不参与，也不计数
    acc += vec2(max(0.0, l2d - log2(-eyeZ(dn))), 1.0);
}
float response = acc.x / acc.y;
color.rgb *= exp(-response * 300.0 * strength);          // strength 默认 1；r = radius·dpr，默认 1
gl_FragDepth = 原深度;                                   // 保留深度，后续无人机、轨迹仍能正确遮挡
```

参数建议：strength 取 0.6–1.2，radius 取 1–1.5·dpr。低端设备（SwiftShader）可以降为"半分辨率深度 + 4 邻域"，或者直接关闭 EDL，退回法线着色。r11 已经测过 Three.js WebGL 与 WebGPU 两条 EDL 路径（见 r11 笔记）。本节给出的是 Cesium 的**对数深度差 + 指数衰减**公式，和 Potree 的 8 邻域版本相比采样数少一半，更适合软件渲染。

### 3.5 瓦片请求优先级：十进制位打包 + 飞行目的地预取

Cesium 的 `Cesium3DTile.updatePriority` 把多个因子编码进一个数，**数值越小优先级越高**：

```text
priority = preloadFlight(1 位) | foveatedDefer(1) | foveated(4 位) | progressiveRes(1) | preferredSorting(4 位) . depth(小数部分)
  preloadFlight   = 当前 pass 为 PRELOAD_FLIGHT ? 0 : 10^10     // 飞行目的地的瓦片排最前
  foveatedDefer   = 视轴锥外且相机刚停下不足 0.2 s ? 10^9 : 0
  foveated        = isolateDigits(norm(foveatedFactor), 4, 4)        // 占第 4–7 位
  progressiveRes  = 属于低分辨率预载 ? 0 : 10^8
  preferredSorting= isolateDigits(norm(REPLACE 且不跳级 ? 距离 : 反向 SSE), 4, 0)
  depth           = norm(depth)（preferLeaves 时取 1 − depth）
isolateDigits(x, n, shift) = floor(x·10^n) · 10^shift；norm 取本帧所有候选的 min/max 归一化
```

移植到本项目的请求队列（`world/pointcloud/scheduler.ts`）：

```ts
function priority(n: Node, f: FrameCtx): number {
  let p = 0;
  if (!n.inFlightPreload)                   p += 1e10;   // 相机过渡目的地预取（Follow/FlyTo 时）
  if (n.outsideFovCone && f.sinceStop<0.2)  p += 1e9;    // 注视点延迟
  if (!n.progressivePreload)               p += 1e8;    // 低分辨率先行（progressiveResolutionHeightFraction）
  p += isolate(norm(n.fovFactor, f.fovMin, f.fovMax), 4, 4);
  p += isolate(norm(1 / n.sse, f.rsMin, f.rsMax), 4, 0); // ADD 点云按 SSE 排序（大 SSE 先加载）
  p += norm(n.level, 0, f.maxLevel);                     // 小数部分：同分时先加载浅层
  return p;
}
// FlyTo / 切换跟随目标时：在过渡开始的那一帧，用"目的地相机"再做一次遍历，把命中的节点标记为 inFlightPreload
// Cesium 默认 preloadFlightDestinations=true；它的相机飞行时长为 min(ceil(dist/1e6)+2, 3) s，城市尺度下固定约 2–3 s，
// 这段时间足够把目的地附近的 1–2 层节点取回来
```

### 3.6 时间系统：SimClock（Cesium Clock 与 Foxglove Player 的合体）

```ts
// apps/web/src/time/clock.ts
type ClockMode  = 'live' | 'replay';                  // live：跟随服务端仿真时间；replay：本地或服务端回放
type ClockRange = 'clamped' | 'loop' | 'unbounded';   // 对应 Cesium CLAMPED / LOOP_STOP / UNBOUNDED
class SimClock {
  startNs: bigint; endNs: bigint; epochNs: bigint;    // epoch = 会话起点，渲染端时间 = Number(t - epoch)/1e9（float64 秒）
  currentS = 0; multiplier = 1; playing = false; range: ClockRange = 'clamped';
  tick(wallDtS: number) {                             // Cesium SYSTEM_CLOCK_MULTIPLIER 语义
    if (!this.playing) return;
    this.currentS += wallDtS * this.multiplier;
    if (this.range === 'clamped') this.currentS = clamp(this.currentS, 0, this.durS);
    else if (this.range === 'loop' && this.currentS > this.durS) this.currentS %= this.durS;
  }
}
// live 模式下显示时间 = 服务端时间 − 插值延迟（§3.9），不做本地推进，避免和服务端漂移；
// 服务端下发 time 消息时校准（Foxglove ws-protocol 的 time 能力；时钟回退视为重置）
// 倍速档位（UI）：取 Foxglove 的 [0.1,0.2,0.5,1,2,3,5] 再加 [10,20,50]，与原设计 §39 的 ×1/×2/×5/×10 对齐
```

### 3.7 回放 Player（移植 Lichtblick `IterablePlayer`）

数据源抽象（前后端共用一套语义）：

```ts
interface IterableSource {
  initialize(): Promise<{startNs, endNs, topics: TopicInfo[], stats}>;
  messageIterator(a: {topics: Set<string>, startNs, endNs?}): AsyncIterableIterator<
      {type:'msg', ev: MsgEvent} | {type:'stamp', tNs: bigint} | {type:'alert', alert}>;
  getBackfill(a: {topics, tNs}): Promise<MsgEvent[]>;        // 每个 topic 在 tNs 之前（含）的最后一条
}
// 实现：McapSource（录制回放，Worker 中解析 MCAP）、UlogSource（PX4 日志，V0.5）、LiveWsSource（实时，无 seek 能力）
```

状态机与 tick：

```ts
states: preinit → initialize → start-play → (idle ⇄ play), 任意状态 → seek-backfill → idle|play, close
setState(s) { if (next==='close') return; next = s; abort?.abort(); runLoop(); }   // 单线程串行执行，新状态会中止旧状态

async tick() {                                           // 在 play 状态下循环调用
  const now = performance.now();
  const dt  = lastTick ? now - lastTick : 20; lastTick = now;
  let range = Math.min(dt * speed, 300);                 // 一个 tick 最多读 300 ms 的数据，防止雪崩
  range = lastRange !== undefined ? lastRange*0.9 + range*0.1 : range;   // EMA 平滑，偶发的慢帧不会拖慢下一帧
  lastRange = range;
  const end = clamp(current + ms(range), start, untilTime ?? endT);
  const buf = carryOver ? [carryOver] : [];              // 上个 tick 多读的那条消息
  const t500 = setTimeout(() => emit({presence:'BUFFERING'}), 500);
  for await (const r of it) {                            // 读到 end（含）为止
    if (r.type==='stamp' && r.tNs >= end) { lastStamp = r.tNs; break; }
    if (r.type==='msg') { if (r.ev.t > end) { carryOver = r.ev; break; } buf.push(r.ev); }
  }
  clearTimeout(t500);
  await renderBarrier;                                   // §3.8：等上一帧渲染完成
  current = end; emit({messages: buf, currentTime: end, presence:'PRESENT'});
}

async seekBackfill(target) {
  const ack = setTimeout(() => emit({messages: [], currentTime: target, presence:'BUFFERING'}), 100);   // 100 ms 内给出反馈
  const msgs = await source.getBackfill({topics: all, tNs: target, signal});
  clearTimeout(ack);
  emit({messages: msgs, currentTime: target, lastSeekTime: Date.now()});   // lastSeekTime 变化 → 面板清空历史（didSeek）
  resetIterator(target);
  if (playing) { await renderBarrier; lastTick = lastRange = undefined; }  // 先把 seek 帧画出来，再继续播放
}
```

两级缓存参数（照抄即可）：

| 缓存 | 服务对象 | 参数 |
|---|---|---|
| BlockLoader（全量预载） | 遥测曲线、状态迁移条带（需要整条时间线） | 块数 ≤ 100，每块 ≥ 0.1 s，`cacheSize = 1 GB`（浏览器端建议 256–512 MB），超限时先剔除没有订阅者的 topic；进度用 `fullyLoadedFractionRanges` 画在时间轴上 |
| 预读缓冲 | 3D 播放（partial） | `readAhead = 10 s`，`minReadAhead = 1 s` |
| 实时队列 | Live WS | 两次 emit 之间最多 400 MB（我们建议 32 MB），超了从头丢到 80% |

### 3.8 渲染屏障与帧合并

```ts
// Player → UI 的背压：Player await listener(state)，listener 在 UI 画完后才 resolve
async function listener(state) {
  store.set(state);                                    // React/zustand 更新
  await new Promise<void>(res => {
    renderDoneCallback = () => setTimeout(async () => {  // 给面板留出本帧剩余时间来登记 pauseFrame
      await Promise.race([Promise.all(pausePromises.splice(0)), sleep(5000)]);
      res();
    }, Math.max(0, msPerFrame - (Date.now() - t0)));
  });
}
// 3D 视图：onRender(state, done) 中只设 needsRender，在下一次 rAF 画完后调用 done()（Lichtblick ThreeDeeRender 的做法）
// Live WS 客户端：socket.onmessage 只写环形缓冲；rAF 里对每个 topic 取"本 tick 最新一条"（latest-per-render-tick），
// 曲线类 topic 例外，要全量追加（它们需要完整序列）
```

原则：**高频遥测不进 React state**。只把"本帧快照引用"放进 zustand，数值面板用 `useSyncExternalStore` 按 10 Hz 节流读取。3D 场景直接在 rAF 里读缓冲，绕过 React。这和 deck.gl `deckgl.ts` 的思路一致：命令式引擎持有渲染循环，只在视口变化时 `forceUpdate`。

### 3.9 遥测插值与平滑

- **Snapshot interpolation**：显示时刻 `t_disp = t_server_latest − D`，其中 `D = 2/f_ws`（WS 为 20 Hz 时 D=100 ms，50 Hz 时 D=40 ms），再加上抖动估计 `+ 2σ_jitter`。
- 在 `(t_k, t_{k+1})` 区间内插值：位置用 **Hermite**（端点位置和速度都已知，DroneState 自带速度，Cesium `HermitePolynomialApproximation` 同理），姿态用 slerp（Foxglove `Transform.Interpolate`）。
- 缺包时按 Cesium `ExtrapolationType.EXTRAPOLATE` 做有限外推，外推时长 `forwardExtrapolationDuration = 3/f_ws`，超时转为 `HOLD`，并在 UI 上标"信号延迟"。
- **不要**用 deck.gl 的逐帧 Verlet spring 做位置平滑：它依赖帧率，而且会引入相位滞后。spring 只适合相机和 UI 数值的动效。

```ts
function sampleDrone(buf: Ring<State>, tDisp: number, out: Pose) {
  const [a, b] = buf.bracket(tDisp);                 // 二分查找
  if (!b) return extrapolate(a, tDisp, 3/fws) ?? hold(a, out);
  const h = b.t - a.t, s = (tDisp - a.t) / h;
  out.p = hermite(a.p, a.v*h, b.p, b.v*h, s);        // h00·p0 + h10·m0 + h01·p1 + h11·m1
  out.q = slerp(a.q, b.q, s);
}
```

### 3.10 轨迹 GPU 时间窗（deck.gl TripsLayer → Three.js TSL）

数据布局（每架无人机一条折线，按块追加）：

```text
chunk = { originWorld: dvec3 (float64, CPU),                 // 块局部原点（RTC）
          t0: float64 秒（相对会话 epoch）,
          positions: Float32Array[N*3] (相对 originWorld),
          times:     Float32Array[N]   (相对 t0, 秒) }         // 每块 ≤ 2 h，保证 ulp ≤ 1 ms
```

TSL 材质（线用 `Line2NodeMaterial` 或自绘 ribbon，WebGPU 与 WebGL2 通用）：

```ts
const uNow   = uniform(0);      // = clock.currentS − chunk.t0
const uTrail = uniform(120);    // 尾迹时长（秒），0 表示整条显示（全轨迹模式）
const vT = attribute('time');   // 顶点属性，经 varying 做透视校正插值，与 TripsLayer 的 vPathPosition/vPathLength 等价
material.colorNode = Fn(() => {
  If(vT.greaterThan(uNow), () => Discard());
  If(uTrail.greaterThan(0).and(vT.lessThan(uNow.sub(uTrail))), () => Discard());
  const a = select(uTrail.greaterThan(0), float(1).sub(uNow.sub(vT).div(uTrail)), 1.0);
  return vec4(baseColor.rgb, baseColor.a.mul(a));
})();
```

用途：
- 回放时"尾迹"与"未来航线预览"：对未来段反向判断，用虚线显示。
- 多架无人机的历史轨迹：一次上传，seek 时零成本。
- 真实飞行与仿真的"幽灵对比"：两条轨迹共用 `uNow`，再加时间偏移 uniform。

**DataFilter 软范围**（deck.gl `filterSoftRange`）用于点云按时间或属性过滤，例如累积的仿真 LiDAR 点带 `t` 属性：

```text
w = smoothstep(min, softMin, v) · (1 − smoothstep(softMax, max, v))；w=0 时 discard，否则 alpha·=w、size·=mix(0.5,1,w)
```

### 3.11 逐帧点云回放预取（Cesium TimeDynamicPointCloud）

适用场景：V0.5 以后回放真机 MID-360 扫描帧，或 SITL 仿真 LiDAR 帧（每帧 1–2 万点，10 Hz）。

```ts
avgLoad = mean(last 5 load times) || 0.05 s
next = intervals.indexOf(clock.t + avgLoad * multiplier)    // 按"加载完成时刻"的播放位置预取，而不是下一帧
if (next === current) next += sign(multiplier)
render(nearestReady(prev → current))                        // 当前帧没好，就回退到最近一帧已加载的帧，不闪空
load(next)
if (memory > 256 MB) unload(frames not in {prev, current, next})
```

### 3.12 Decay time：仿真 LiDAR 与点云累积（Lichtblick RenderObjectHistory）

```ts
onScan(msg) { history.push({renderable: build(msg), msgTime: msg.t, frameId: msg.frame}); }
onFrame(now) {
  while (history.length > 1 && history[0].msgTime < now - decay) history.shift().dispose();
  for (e of history) e.renderable.matrix = tf.lookup(fixed ← e.frameId @ e.msgTime) ∘ (render ← fixed @ now);
}
```

要点：每帧扫描的位姿取**消息时刻**的 TF，而不是当前时刻。这样无人机运动时，累积出的点云在世界中保持静止，形成"扫描建图"效果。decay 建议 1–5 s，V0.4 用来可视化"雾导致 LiDAR 量程下降"。

### 3.13 遥测曲线 M4 降采样（Lichtblick downsample.ts）

算法：x 方向按 `width/3px` 分桶，每桶保留 first、min、max、last 四个点（去掉重复的 y 像素），流式处理，可以带状态增量追加。本机 Node 22 实测（`m4_bench.mjs`）：90,000 个样本（50 Hz × 30 min）→ 800 px 宽时保留 952 点，**耗时 4.4 ms**；1600 px 宽时保留 1905 点，3.5 ms。V0.2 的遥测面板（高度、速度、电量、风速）直接用它，配合 lieflat-charts 的发丝线风格渲染。曲线多于 4 条时，建议放进 Worker + OffscreenCanvas（Lichtblick Plot 的做法）。

### 3.14 Timeline 刻度与交互

```ts
const SCALES = [0.001,0.002,0.005,0.01,0.02,0.05,0.1,0.25,0.5,1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200,14400,21600,43200,86400];
function ticks(spanS: number, widthPx: number, labelPx = 64, smallestPx = 7) {
  const ideal = Math.min(labelPx / widthPx, 1) * spanS;
  const main = SCALES.find(s => s > ideal) ?? SCALES.at(-1)!;
  const sub  = [...SCALES].reverse().find(s => s < main && isMultiple(main, s));
  const tiny = SCALES.find(s => s < (sub ?? main) && isMultiple(sub ?? main, s) && widthPx*s/spanS >= smallestPx);
  return {main, sub, tiny};                 // 只绘制 ≥ 3px 的层级（Cesium _makeTics）
}
```

交互规范（取 Foxglove）：
- 空格：播放/暂停；←/→：步进 100 ms，Alt 为 500 ms，Shift 为 10 ms；也支持按帧步进。
- 悬停显示 `previewTime`，面板可以随悬停预览。
- 时间轴底层画 loaded ranges（缓冲进度），上层画 events overlay（起飞、任务切换、告警）。
- 支持循环播放（RepeatAdapter）和 `playUntil`（播放到某事件后暂停）。

### 3.15 跟随相机（Cesium TrackingReferenceFrame）与 UI 相机模式映射

| UI 模式（原设计 §40） | 参考系 | 实现 |
|---|---|---|
| Bird Eye / 北朝上跟随 | `ENU` | `camera.position = target.p + offset_enu`，lookAt target，up = +Z |
| Third Person / 追尾 | `VELOCITY` | x = 速度方向，z = 上方正交化，y = z × x；offset 用机体后上方 (−15, 0, 6) m；低速（< 0.5 m/s）时退回 yaw 方向，避免抖动 |
| FPV / 机体锁定 | `INERTIAL`（姿态） | 相机挂到 `base_link` 或 `camera_optical` 帧，完全跟随姿态；可选"仅 yaw 跟随 + 云台稳定" |
| Free Camera | — | OrbitControls（Z-up） |

切换时的过渡：用 van Wijk–Nuij 曲线（deck.gl `FlyToInterpolator`，curve=1.414），或简单地做 2 s ease-in-out，并同时触发目的地预取（§3.5）。

### 3.16 按需渲染（Cesium requestRenderMode + Lichtblick queueAnimationFrame）

```ts
needsRender = cameraChanged || dataArrived || tilesLoaded || uiDirty || (clock.playing && |t − tLastRender| > maxRenderTimeChange)
// 暂停且相机静止时不渲染，CPU/GPU 占用归零。这对无 GPU 的 SwiftShader 演示机至关重要
// 播放中或 live 模式下每帧渲染，但点云 LOD 遍历与 EDL 可以降频（遍历 10 Hz，EDL 跟随渲染）
```

### 3.17 WebGPU 下画大点：实例化图元（deck.gl PointCloudLayer）

- WebGPU 的 `point-list` 只能画 1 px 的点，**没有 `gl_PointSize`**。deck.gl 的做法是每个点实例化 **1 个外接单位圆的等边三角形**（3 个顶点），再在片元中按 `length(uv) > 1` discard。
- 取舍：三角形面积 `3√3·r² ≈ 5.20·r²`，四边形（6 顶点或 4 顶点 + index）面积 `4·r²`。三角形顶点数少 25–50%，但片元过绘多 30%。**点径 ≤ 3 px 的远景以顶点为瓶颈，用三角形；近景大点用四边形**（Three.js `InstancedPointsNodeMaterial` 或 sprite 路径）。WebGL2 路径直接用 `gl.POINTS` + `gl_PointSize`（r12 已实测）。
- 抗锯齿：半径放大 `1 + 1/(dpr·r)`，alpha 取 `smoothedge(edgePixels)`，`edgePixels = (1−dist)/fwidth(dist)`。

### 3.18 面板架构（Foxglove 式，落到 shadcn）

```ts
// apps/web/src/panels/types.ts
interface PanelDescriptor<Cfg> {
  type: 'telemetry.chart' | 'telemetry.gauge' | 'drone.list' | 'mission' | 'env' | 'map2d' | 'events' | 'perf';
  title: string; icon: MorphIconName;                   // morphicons，禁止 emoji
  defaultConfig: Cfg; settings: SettingsTree<Cfg>;       // 声明式设置 → 自动生成 shadcn 表单
  render(ctx: PanelContext<Cfg>): ReactNode;
}
interface PanelContext<Cfg> {
  subscribe(subs: {topic: string; preload?: boolean; fields?: string[]}[]): void;
  watch(field: 'currentFrame'|'allFrames'|'currentTime'|'didSeek'|'previewTime'): void;
  onRender(cb: (s: RenderState, done: () => void) => void): void;   // 必须调用 done()，否则 Player 会阻塞在渲染屏障
  seek(tNs: bigint): void; setPreviewTime(tNs?: bigint): void;
  publish?(topic: string, msg: unknown): void;            // 控制指令（GoTo/Takeoff）
  saveConfig(p: Partial<Cfg>): void;
}
// 布局：LayoutData = {tree: ResizableTree(shadcn Resizable / react-resizable-panels), configById, playback: {speed, loop}}
// 存 localStorage；预置"数字沙盘"布局（左 World/Env，右 Drones，底 Timeline）和"调试"布局（多曲线 + 事件 + 性能）
// 字段绑定采用 message path 子集：`/uav01/state.pos[2]`、`/uav*/state.battery`、`.@derivative`
```

### 3.19 WebSocket 协议与调试旁路（ws-protocol 语义 + foxglove-sdk）

主协议（前端 ↔ 仿真服务），借鉴 foxglove ws-protocol 的概念：

| 概念 | ws-protocol | 我们的做法 |
|---|---|---|
| 服务端能力 | `serverInfo.capabilities`：time、clientPublish、services、parameters、playbackControl | `hello{caps, world_id, frame:'world', epoch_ns, sim_rate}` |
| 通道广告与订阅 | `advertise{channels}`、`subscribe{id, channelId}` | 同样；topic 命名为 `/uavNN/state`、`/env/wind/grid`、`/events` |
| 数据帧 | 二进制：`opcode(1) + subId(4) + logTime(8) + payload` | 二进制：`opcode(1) + channel(2) + t_sim_ns(8) + payload`；DroneState 用定长 struct（位置 f64×3、速度 f32×3、四元数 f32×4、电量 f32 ……），约 100 B/架/帧，替代 JSON |
| 时钟 | `time{timestamp}` 广播 | 10 Hz 广播 `t_sim_ns`，客户端据此校准 §3.6 |
| 回放控制 | `PlaybackControlRequest{seek_time, playback_speed, playback_command}` → `PlaybackState{current_time, speed, status, did_seek, request_id}` | 完全照搬字段语义（服务端回放 MCAP 或"时间倒带"的 mock 仿真） |

调试旁路（V0.2，服务端可选开启 `--foxglove`）：

```python
# apps/simulator/debug_bridge.py —— foxglove-sdk 0.27（Rust wheel，本机 pip 安装可用）
import foxglove
from foxglove.channels import FrameTransformsChannel, LocationFixChannel
server = foxglove.start_server(host="0.0.0.0", port=8765, capabilities=[foxglove.Capability.Time])
mcap = foxglove.open_mcap(f"recordings/{run_id}.mcap")      # 与 server 同时生效：一次 log，两处输出
tf_ch  = FrameTransformsChannel("/tf")                     # world→uavNN/base_link（ENU/FLU）
fix_ch = {i: LocationFixChannel(f"/uav{i:02d}/fix") for i in ids}
st_ch  = {i: foxglove.Channel(f"/uav{i:02d}/state", message_encoding="json") for i in ids}
def on_step(t_ns, drones):                                 # 由仿真主循环以 50 Hz 调用（和发给 Web 的数据同源）
    tf_ch.log(FrameTransforms(transforms=[...]), log_time=t_ns)
    for d in drones: fix_ch[d.id].log(LocationFix(...), log_time=t_ns); st_ch[d.id].log(d.as_dict(), log_time=t_ns)
    server.broadcast_time(t_ns)
```

本机实测：20 架 × 50 Hz × 3 条消息，Python 单线程约 20k msg/s，占用约 **15% 单核**，MCAP 平均 47 B/条（zstd）。一个 10 分钟、20 架的任务录制约 58 MB，可以直接用 Lichtblick 或 Foxglove 打开回放。前端的 `McapSource`（§3.7）也读这份文件，实现"仿真回放"与"真机回放"同构。

---

## 4. 在本项目中的落点与复用方式

| # | 能力 | 来源（文件/函数） | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | WGS84/ECEF/ENU 数学、HPR 与 NED/ENU 换算 | cesium `Core/Ellipsoid.js`、`Core/FixedFrameTransforms.js`、`Core/Quaternion.js` | `apps/web/src/geo/`、`world/georef/frames.py` | V0.1 | **port** | 约 150 行，逻辑确定；不值得为此引入 3 MB 的 Cesium |
| 2 | `coordinate.json` 与坐标规则 R1–R10 | 本文 §3.1 | World Package、架构说明书 | V0.1 | 设计 | 统一 Reality→World 的坐标契约 |
| 3 | TF 历史缓冲（lerp+slerp，maxDelta 钳位，25% 剔除） | lichtblick `ThreeDeeRender/transforms/CoordinateFrame.ts`、`TransformTree.ts` | `apps/web/src/world/tf.ts` | V0.2 | **port** | 多机、传感器外参、回放插值都要用 |
| 4 | 点尺寸衰减（点径 = 瓦片 SSE） | cesium `PointCloudStylingStageVS.glsl` + `PointCloudStylingPipelineStage.js` | `apps/web/src/world/pointcloud/material.ts`（TSL） | V0.1 | **port** | 疏密调节的视觉补偿 |
| 5 | EDL（4 邻域，对数深度，exp 衰减） | cesium `PointCloudEyeDomeLighting.glsl` | `apps/web/src/world/pointcloud/edl.ts` | V0.1 | **port** | 数据无颜色时必须有 |
| 6 | 请求优先级位打包 + 飞行预取 | cesium `Cesium3DTile.updatePriority`、`Cesium3DTileset.preloadFlightDestinations` | `apps/web/src/world/pointcloud/scheduler.ts` | V0.1 | **port** | 跟随或切换无人机时不出现"空洞" |
| 7 | 按需渲染 | cesium `Scene.requestRenderMode`；lichtblick `Renderer.queueAnimationFrame` | `apps/web/src/render/loop.ts` | V0.1 | **port** | 静止时零占用 |
| 8 | SimClock（倍速、钳位、循环） | cesium `Core/Clock.js` | `apps/web/src/time/clock.ts` | V0.2 | **port** | Timeline 核心 |
| 9 | 回放 Player 状态机、tick、seek-backfill | lichtblick `IterablePlayer.ts`、`IIterableSource.ts` | `apps/web/src/time/player/` | V0.2 | **port** | 久经打磨的回放语义，边界情况处理完整 |
| 10 | 两级缓存（BlockLoader、预读） | lichtblick `BlockLoader.ts`、`BufferedIterableSource.ts` | 同上 | V0.2 | **port** | 曲线全量预载与 3D 播放分离 |
| 11 | 渲染屏障 / latest-per-render-tick | lichtblick `MessagePipeline/index.tsx`、`samplingGuard.ts` | `apps/web/src/net/pipeline.ts` | V0.2 | **port** | 防止高频遥测拖垮 React |
| 12 | 面板 API 与布局 JSON | lichtblick `suite/src/index.ts`、`CurrentLayoutContext/actions.ts` | `apps/web/src/panels/` | V0.2 | **port（接口）** / **skip（MUI 实现）** | UI 必须用 shadcn |
| 13 | M4 降采样 | lichtblick `TimeBasedChart/downsample.ts` | `apps/web/src/charts/downsample.ts` | V0.2 | **port** | 遥测曲线的流畅性 |
| 14 | 播放性能 HUD（×realtime、fps、Mbps） | lichtblick `panels/PlaybackPerformance` | `apps/web/src/panels/perf/` | V0.1 | **port** | 流畅性测试的指标面板 |
| 15 | Timeline 刻度、键盘步进、倍速档 | cesium `widgets/Timeline/Timeline.js`；lichtblick `PlaybackControls/*` | `apps/web/src/time/Timeline.tsx` | V0.2 | **port** | 与原设计 §39 对齐 |
| 16 | 遥测插值（Hermite + slerp + 有限外推） | cesium `SampledProperty.getValue`、`HermitePolynomialApproximation` | `apps/web/src/drones/interp.ts` | V0.2 | **port** | 把 10–50 Hz 平滑到 60 fps |
| 17 | 跟随相机三种参考系 | cesium `EntityView.js`、`TrackingReferenceFrame`、`rotationMatrixFromPositionVelocity` | `apps/web/src/camera/modes.ts` | V0.2 | **port** | 对应原设计 §40 |
| 18 | 轨迹 GPU 时间窗 | deck.gl `trips-layer.ts` | `apps/web/src/drones/trail-material.ts` | V0.2 | **port** | 回放零重建 |
| 19 | GPU 软范围过滤 | deck.gl `data-filter/shader-module.ts` | 点云与 LiDAR 材质 | V0.4 | **port** | 按时间或强度过滤 |
| 20 | 实例化三角形画点（WebGPU） | deck.gl `point-cloud-layer.ts` + `.wgsl.ts` | 点云材质（WebGPU 分支） | V0.1 | **reference** | 与 r12 的实测结果对照后选型 |
| 21 | 逐帧点云预取 | cesium `TimeDynamicPointCloud.js` | `apps/web/src/sensors/lidar-replay.ts` | V0.5 | **port** | 真机扫描回放 |
| 22 | decay-time 历史点云 | lichtblick `pointExtensionUtils.ts` `RenderObjectHistory` | `apps/web/src/sensors/lidar-accum.ts` | V0.4 | **port** | 仿真 LiDAR 可视化 |
| 23 | 体素光线步进（环境场） | cesium `VoxelPrimitive.js`、`Shaders/Voxels/VoxelFS.glsl` | `apps/web/src/environment/volume.ts` | V0.3–V0.4 | **reference** | 风场、雾密度体渲染的步长与 octree 思路 |
| 24 | foxglove-sdk 调试旁路 + MCAP 录制 | foxglove-sdk `ws-server`、`write-mcap-file`、`ws-playback-control-mcap` | `apps/simulator/debug_bridge.py`、`recordings/` | V0.2 | **adopt** | 零成本获得专业调试工具；录制格式标准化 |
| 25 | ws-protocol 概念（能力、广告、time、PlaybackControl） | foxglove ws-protocol v1、foxglove-sdk | `apps/api/ws/protocol.md` | V0.2 | **reference** | 协议设计少走弯路 |
| 26 | ULog 回放 | lichtblick `players/IterablePlayer/ulog/*`（`@lichtblick/ulog`） | `apps/web/src/time/player/UlogSource.ts`（或服务端 pyulog 转 MCAP） | V0.5 | **reference / adopt（npm 包）** | 回放真实 P600 飞行 |
| 27 | GIS/地球模式视图 | CesiumJS 整库 | `apps/web/src/views/globe/`（独立路由） | V0.5+ | **adopt（可选）** | 地形、影像、GIS 分析；与 Three 主视图不共享画布 |
| 28 | 2D 地图小窗 | deck.gl `MapView` + `@deck.gl/maplibre` + TripsLayer/IconLayer | `apps/web/src/panels/map2d/` | V0.5+ | **adopt（可选）** | 多机态势的俯视图（需要地理配准和底图） |
| 29 | Cesium Timeline/Animation widget、Lichtblick UI 组件 | — | — | — | **skip** | 与 shadcn / morphicons / transitions.dev 视觉体系冲突 |
| 30 | deck.gl `Tile3DLayer` 点云 | — | — | — | **skip** | 点大小固定、无衰减和 EDL，不如 3DTilesRendererJS 或自研 |

---

## 5. 对比与推荐

| 维度 | CesiumJS | deck.gl | Foxglove Studio（Lichtblick + SDK） |
|---|---|---|---|
| Star / 活跃度（2026） | 15.8k / 极活跃（1.145，每月发版） | 14.6k / 极活跃（9.4 于 2026-09 发布，v10 在路上） | 原仓已归档；Lichtblick 1.1k 活跃；SDK 活跃 |
| 渲染后端 | WebGL2（无 WebGPU） | WebGL2 + WebGPU（实验） | WebGL（three 0.156） |
| 点云能力 | 衰减、EDL、样式表达式、裁剪、时间动态点云、3DGS，**最强** | PointCloudLayer（实例化三角形）；Tile3DLayer 点大小固定 | PointCloud2 着色模式与 decay 历史 |
| 地理坐标 | 椭球、ENU/NED/HPR、RTE，**最完整** | Web Mercator、offset/fp64，Globe 实验 | 纯 TF 树（无地理），Map 面板用 Leaflet |
| 时间与回放 | Clock、Timeline、SampledProperty、CZML | 仅 `currentTime` prop（TripsLayer） | **Player 状态机、缓存、屏障、面板时间 API，最完整** |
| 与 React/shadcn 的契合 | 低（widgets 用 knockout） | 高（`@deck.gl/react`） | 架构高、UI 低（MUI） |
| 与 Three.js 主栈的契合 | 低（独立引擎与上下文） | 低（独立 luma.gl 上下文） | 中（3D 面板本身基于 three，但版本旧） |
| MVP 直接价值 | 数学、着色、优先级、时钟 | 轨迹时间窗、过滤、WebGPU 画点 | 回放、面板、遥测曲线、录制 |
| 推荐 | **1**（port 最多，V0.5 可作为 GIS 模式） | **3**（port 少量 shader，V0.5 可作为 2D 地图） | **2**（架构移植 + SDK 直接用） |

综合排序：**CesiumJS ≈ Lichtblick/foxglove-sdk > deck.gl ≫ 原 foxglove/studio 仓库**。前两者分别负责"空间与点云"和"时间与数据流"两条主线，都要在 MVP 期移植核心算法。deck.gl 的价值集中在 TripsLayer 和 DataFilter 的 shader 思路，以及以后的 2D 地图面板。

---

## 6. 风险与注意事项

1. **Cesium 与 deck.gl 不能和 Three.js 共享画布**：各自独占 WebGL 上下文和渲染循环。叠加显示要么用两个 canvas 叠层（深度不一致，遮挡错误），要么用独立视图。MVP 严禁"Three + Cesium 同屏混合"。
2. **Cesium 没有 WebGPU**，包体约 3–4 MB（gzip 后仍 > 1 MB），而且 Workers 和 Assets 需要额外部署。Cesium ion、Google Photorealistic 3D Tiles 需要 token，在国内网络下不可用或很慢。国内 GIS 底图用天地图（需要 key，CGCS2000）或自建瓦片。
3. **GCJ-02 陷阱**：用高德、腾讯底图时约 0.5–0.6 km 的偏移会让 RTK 轨迹"整体错位"。只能在底图层纠偏，World 与 RTK 数据保持 WGS84/CGCS2000。
4. **高程基准混用**：PX4 同时给出 AMSL 和椭球高，RTK 给椭球高，DEM 常用 1985 国家高程基准或 EGM。合肥 N≈−4.6 m，旧金山 −32 m，混用就会"飞机钻地"或"悬空"。
5. **航向约定**：NED yaw、ENU yaw、Cesium heading 三者 0° 方向和旋转方向各不相同（§3.1.5 实测表）。在网关边界统一换算，禁止在前端到处写 `±90°` 补丁。
6. **UTM 或 Web Mercator 当米用**：deck.gl `METER_OFFSETS` 本身正确，但若把 MapLibre 的 Mercator 坐标直接喂给物理模块，会有 8–27% 的尺度误差。
7. **float32 时间**：GPU 上用 Unix 秒会有 128 s 的量化，必须以会话或块起点为基准。JS 的 BigInt 在热循环里慢，所以线上传输与存储用 int64 ns，渲染端用 float64 相对秒（Foxglove 用 `{sec,nsec}` + bigint 的开销值得警惕）。
8. **Lichtblick 工程体量**：yarn monorepo + electron + MUI 7 + three 0.156 补丁版，**只移植逻辑，不引入依赖**。它的 ROS 数据类型（`RosDatatypes`）和 message-definition 体系对我们来说过重；我们的 schema 用 JSON Schema 或 FlatBuffers，可直接对齐 foxglove-sdk 的 `schemas/`。
9. **foxglove-sdk 是 Rust wheel**（maturin），本机 Python 3.12 可以直接 `pip install foxglove-sdk==0.27.0`（已在隔离 venv 中验证）。远程访问（`start_gateway`）需要带 feature 编译，我们不需要。Foxglove 商业客户端需要账号；**Lichtblick 不支持服务端 `PlaybackControl` 能力**（只有 Foxglove 商业版支持），所以服务端回放控制要靠我们自己的前端。
10. **渲染屏障可能反向拖慢仿真**：屏障只作用于"Player → UI"，**绝不能反压到仿真主循环**。仿真端按固定步长运行，WS 发送端对慢客户端做丢帧（保留最新），录制与发送解耦。
11. **deck.gl WebGPU 仍为实验**，v10 将有破坏性升级（luma.gl v10、loaders.gl v5）。V0.5 如果采用，锁定 9.4.x 并用 WebGL2 分支（`visgl:webgl-only` 导出条件可以减小包体）。
12. **SwiftShader（无 GPU 演示机）**：EDL 全分辨率 MRT、每像素对数运算在软件渲染下开销明显。要提供"性能档"开关（EDL 关闭或半分辨率、点径上限 3 px、DPR=1），并配合按需渲染。
13. **虚拟城市没有地理参考**：UrbanScene3D 的"经纬度"只是示意，在 UI 和导出中必须标注，避免被误用为真实测绘成果。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§7 World Model → Geographic**：现在只列了 `WGS84 / UTM / ENU / RTK Origin`。建议改为：
   - 规范坐标系为 **World ENU（锚点切平面，Z-up，米）**；
   - 增加 **ECEF**（3D Tiles/Cesium 必需）、**CGCS2000**（国内 RTK/CORS）、**高程基准**（椭球高、EGM2008、1985 国家高程基准）、**GCJ-02 仅限底图**；
   - 并附上 §3.1 的规则 R1–R10 和 `coordinate.json` schema。UTM 降级为"交换用"，国内优先 CGCS2000 3° 高斯-克吕格。
2. **§11 技术对比表**："CesiumJS：后续增加"不够具体。建议拆成三行：
   - **Cesium 算法**：MVP 移植（椭球与 ENU、点云衰减、EDL、优先级、时钟）；
   - **Cesium 引擎**：V0.5+ 可选的"GIS/地球模式"独立视图；
   - 新增 **deck.gl**（V0.5+ 可选 2D 地图面板；MVP 移植 TripsLayer 思路）和 **Foxglove/Lichtblick**（MVP 移植回放与面板架构；foxglove-sdk 做调试旁路与 MCAP）。
3. **§14 点云 Web 渲染**：LOD 只提到"相机位置、FOV、距离、屏幕尺寸"。建议补充：
   - **点尺寸衰减**（点径 = 瓦片 SSE，§3.3），这是疏密自动调节在视觉上成立的前提；
   - **EDL**（无颜色数据必需）；
   - **请求优先级与飞行预取**（§3.5）；
   - **按需渲染**（§3.16）；
   - 与 r10 的 SSE、点预算、FPS 反馈合在一起，组成完整的"自适应密度控制器"。
4. **§16 Three.js 场景结构**：缺少"坐标帧"这个维度。建议增加 `FrameTree`（world → uavNN/odom → base_link → sensors），DroneLayer 和 SensorLayer 的节点都挂在帧上，由 TF 缓冲按时间驱动（§3.1.6）。DebugLayer 的 `Coordinate` 细化为"帧轴显示 + 锚点 + 指北针"。
5. **§28 DroneState**：建议补全字段并明确约定：
   - `t_sim_ns`（int64）、`frame_id: "world"`；
   - `position_enu[3]`（f64），另附 `geo{lat, lon, h_ellipsoid, h_msl}`；
   - `orientation_q_xyzw`（ENU←FLU）、`velocity_enu`、`angular_velocity_flu`、`heading_deg_compass`（仅用于显示）；
   - `flight_mode`、`health{gps_fix, rtk_status, link_rssi, latency_ms}`；
   - 数值单位写进 schema。
6. **§36 前后端通信**：只写了"REST + WebSocket"。建议明确 WS 协议：
   - **能力协商、通道广告与订阅、二进制帧**（DroneState 定长 struct，替代 JSON）；
   - **服务端 time 广播**；
   - **PlaybackControl 请求与状态**（字段照搬 foxglove-sdk）；
   - 慢客户端丢帧策略；
   - 数据流改为"仿真 → (WS 主通道 + foxglove-sdk 调试旁路 + MCAP 录制)"三路同源输出。
7. **§37 更新频率**：补充**显示插值延迟** `D = 2/f_ws`，以及 Hermite + slerp + 有限外推（§3.9）；并说明 "WebSocket 10–50 Hz 与渲染 60 FPS" 之间靠插值衔接，而不是靠提高 WS 频率。
8. **§38 UI 设计**：数字沙盘布局固定为"左 World/Env、右 Drones、底 Timeline"是对的。建议增加**可停靠面板体系**（Foxglove 面板 API + shadcn Resizable）：
   - 遥测曲线（M4 降采样，lieflat 风格）、状态迁移条带（飞行模式时间线）、事件日志、2D 小地图、**性能 HUD**（×realtime、fps、帧时 p95、WS Mbps、渲染点数、瓦片数）；
   - 布局 JSON 可保存和切换（"演示/调试/回放"）。
9. **§39 Timeline**：只有播放、暂停、倍速和 seek。建议补充：
   - **区分仿真时间与墙钟时间**（示例 `14:32:00` 需要标明是哪一种）；
   - 缓冲进度（loaded ranges）、事件标记叠加、悬停预览（previewTime）；
   - 键盘步进（100/500/10 ms）、循环、`playUntil`；
   - Live 模式下时间轴只读并自动跟随最新；
   - seek 超过 100 ms 未完成时显示 BUFFERING；
   - 倍速档位建议 `0.1/0.2/0.5/1/2/5/10/20`。
10. **§40 Drone Interaction**：相机模式与参考系的对应关系要写清楚（§3.15）：Bird Eye=ENU、Third Person=VELOCITY、FPV=姿态锁定。切换使用过渡动画（transitions.dev 规范）并触发目的地瓦片预取。
11. **§41 World Package**：
    - `coordinate.json` 的内容要定义出来（§3.1.3）；
    - 新增 `recordings/`（MCAP，含仿真与真机）和 `layouts/`（UI 布局预设）；
    - `reconstruction/trajectory/` 建议也存为 MCAP（`/tf`、`/fix`），这样重建相机轨迹、真机飞行和仿真回放共用一套 Player。
12. **§43 MVP**：建议在链路末端加上"**录制 → 回放**"。仿真运行的同时录 MCAP，Timeline 可以回放任意一次运行。这几乎不增加成本（foxglove-sdk 实测约 15% 单核），却能极大方便流畅性测试和演示（可重复）。
13. **§45 V0.2**：除了 PX4 SITL，建议显式列出"**mock 动力学仿真 + foxglove-sdk 调试旁路**"作为 V0.2 前半段的交付。前端和协议先跑通，之后再接 SITL，降低耦合风险。
14. **§48 V0.5 Real World Fusion**：增加**地理配准验收项**：锚点精度、高程基准转换、底图纠偏；ULog 真机回放（Lichtblick 已有 ULog 源可参考）；GIS 模式（Cesium 独立视图）或 2D 地图（deck.gl）二选一。
15. **§12/§34 的 WebGPU 表述**："Point Rendering 由 WebGPU 承担"要补充一条约束：**WebGPU 没有可变点大小，需要实例化图元**（§3.17），而且本机和演示机的 headless 环境里 WebGPU 大概率不可用。所以 WebGL2 路径必须是一等公民（Cesium 和 deck.gl 生产环境至今都以 WebGL2 为主）。
