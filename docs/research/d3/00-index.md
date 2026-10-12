# 00-index：D3 研究总览——从"无人机仿真"到"可反复观测的 4D 事件沙盘"

> 项目：ANet Drone4D（github.com/ANetResearch/ANet-Drone4D）｜ 阶段：D3 研究整合 ｜ 日期：2026-10-11 ｜ 作者：D3 首席研究整合
>
> 输入：
> - 用户诉求：固定沙盘、固定事件（A、B、C 沿建筑与道路从 X 到 Y 再到 Z，期间有遮挡、风速变化、下雨）；机队数量、飞行策略、协同策略都可变，同一事件可反复采集；沙盘基于 3DGS + mesh；物理引擎决定位姿与视角，渲染出"无人机当时的真实视角"；每机没有全局视野；在 GLB 沙盘里模拟 LiDAR 做重建与采集；深入研究 FAST-LIVO2。
> - 用户设计讨论 `/data/projs/anet-drone-data/design-r1.md`（下称 design-r1）。
> - D3 研究单元 r1–r10 的笔记（本目录）与实验目录 `.cache/research/d3/<单元>/`；r11（Skyfall-GS 研读与生成式高斯外观层实验）于 2026-10-12 补入，结论见 §10；r12（SuperSplat、SuperSplat Viewer 与 splat-transform 研读与实验）于同日补入，结论与用户第二次决定的落点见 §11。
> - D2 文档 `docs/03`、`docs/04`、`docs/16`、`docs/17`、`docs/19`、`docs/20` 与代码审计（r7）。
>
> 约束：本研究没有修改项目代码，也没有修改 `docs/research/d3/` 以外的文档。D2 正在另一工作流中验收修复，本文全部建议都以"D2 先发布并打标签，D3 在新分支上只做新增"为前提。
>
> 阅读顺序：§0（十条结论）→ §3（对 design-r1 的确认与修正）→ §4（推荐架构）→ §5（阶段计划）→ §6（待用户决策）→ §7（风险）。§1.2 的尺度修正表是引用任何米制数字前必须先看的内容；§2 用于查实测数字。

---

## 0. 十条总结论（先读）

1. **"同一世界 + 同一事件 + 不同机队策略 ⇒ 不同观测"已在原型上跑通。** r6 在 crop 小镇上裁出 600 m × 600 m 舞台，编排了 360 s 的三人一车事件（含 40 名背景行人、风雨与通信干扰区）。在同一条事件磁带上，6 种机队策略各跑 12 个种子，事件捕获率 EC 从 0.25（覆盖扫描）到 1.00（5 机共享跟踪）不等。事件磁带在两台机器上逐字节相同，背景人数从 0 增加到 1000 时脚本演员的轨迹哈希不变。dmax 上 72 次运行用 10 个进程 60 s 跑完。

2. **用户提供的三份 GLB 是带烘焙光照的摄影测量三角网格，不是 3DGS，单位也不是米。** r2、r3、r4、r5 四个单元独立确认了这一点。经 OSM 配准与三条独立证据核对：town 每 glTF 单位 8.374 m，crop（oryahovo）20.935 m，hill 2.49–2.53 m；模型还有 1.1°–1.25° 的系统倾斜。对这批资产来说，"3DGS + mesh 沙盘"应理解为：**网格既是几何真值，也是外观真值；3DGS 是可选的外观层。** 用网格渲染图训练出的 3DGS 上限就是网格本身：同分布视角 34 dB，低空掉 6–7 dB，瓦片外为空。只有对真实航拍重建的资产，3DGS 才带来额外的真实感。

3. **"无人机当时的真实视角"由服务端硬件光栅化生成，渲染速度不是瓶颈。** OpenGL/EGL 用 MRT 一遍输出 RGB、平面深度、实例与语义，A40 上 720p 每帧 1.4–1.6 ms（含读回）。遮挡、可见度与标签必须取自网格深度：若用 3DGS 期望深度判断遮挡，会把 14.3% 本应被挡住的像素画出来（演员"穿墙"）。1080p 多机批量时，先成为瓶颈的是 CPU 编码。

4. **LiDAR 采用 GPU 网格逐射线求交，并按逐点时刻采样位姿（Warp）；生成的数据能直接驱动真实 SLAM。** 单张 A40 可实时支撑约 500 架 MID-360。D2 的 2 m DSM 扫描器只保留给公开站预览：按正确尺度测得，2 m DSM 的视线判定与网格精确求交有 13% 不一致，几乎全是"误判遮挡"。模拟的 MID-360、内置 IMU 和相机写成 bag 后，FAST-LIVO2 不改代码即可运行：clean 档 RPE100 为 0.08%，与真机数据（MARS-LVIG，ATE 0.056%）同一量级。但输入合同有一处做错，结果就会差 3 倍乃至发散。合同包括：每帧 20 000 点全部输出、无回波点以零占位、点时间单调、IMU 用 g 单位、起飞前有静止窗口。

5. **物理分三档。** Fast 档用 FleetSim，是主力；Realistic 档用 Gazebo；SITL 档用 PX4 + Gazebo。**Gazebo 只替换动力学与接触，不替换传感器：** 各档把位姿送进同一个 Sensor Generation Service，因此不同档之间的观测差异只来自动力学。Gazebo 原生传感器只作交叉验证，原因是每个进程约 90 次传感器更新/s，瓶颈在 CPU 渲染线程，且 gpu_lidar 不支持 MID-360 的非重复花样与逐点时刻。Gazebo 进程内锁步在同一镜像、同型号 GPU 上逐位确定。PX4 SITL 的时钟从动于 Gazebo，结果不确定，只用于飞控验证。

6. **事件真值只来自 Canonical Event Tape。**
   - 脚本演员沿 NavMesh 按时刻做运动学推进。若改用人群模拟推进，1000 名背景人时事件根本不会发生。
   - 背景人群用 DetourCrowd，并主动避让脚本演员。
   - 相遇、上车、进入建筑、遮挡等语义事件由验证器从磁带与网格计算，作者的声明要接受核对。原型中 5 条声明有 1 条不成立。
   - D2 的识别物反应会改写事件：一架机悬停，A 就偏离 53.9 m。Exogenous 模式必须关闭反应，只保留判据作度量。

7. **"每机没有全局视野"要靠唯一的观测出口来保证。** D2 有多条真值泄漏通道：M19 感知帧带真值 id、航迹全队融合、LiDAR tag bit0 标记识别物命中、沙盒 REST 可读全部真值。D3 的 M26 Env 统一处理三件事：
   - 观测按 L0–L3 分级过滤；
   - Benchmark 采用 lockstep，配合决策时延模型，正式评测时 Agent 放在隔离容器中；
   - Agent 之间的通信只经 Env 的链路模型。

8. **评测必须看实际拿到的证据，不能只看机位。**
   - 覆盖扫描的"机位得分"为 0.92，实际证据得分为 0。
   - 共享目击并不总是更好：上车捕获率从 0.58 升到 1.00，但进入建筑的捕获率从 1.00 降到 0.50。
   - 冗余度必须一起报告。
   - 事件理解 F1（0.53–0.65）明显低于捕获率（0.88–1.00），说明需要 Re-ID 与时序推理。
   - SLAM 轨道以 RPE100 为主指标，按"种子 × 重复运行"报告分布。

9. **D2 的复用度高、改动面小。** 按 Python 行数计：45% 原样复用，39% 只需加接口，12.5% 需要隔离后复用。必须触及 D2 的只有 14 处扩展点（约 20 个文件），全部保证缺省行为逐位不变。工作量约 78.5 人周（±40%），其中 P0 44.5 人周，关键路径约 12 周。

10. **部署分工。**
    - 公开站 emax（4 核、无 GPU）继续运行 D2；D3 在公开站只增加只读视图：预构建世界包、事件回放、数据集浏览。
    - 渲染、MeshLidar、Gazebo、3DGS 训练放在 dmax GPU Worker。
    - FAST-LIVO2、评测、L1 批量运行、世界构建放在 dmax CPU 池。
    - 一个世界的网格 BVH 约 300 MB，超过 D2 沙盒每会话 140–155 MB 的内存预算，不能进入公开沙盒池。
    - dmax 是共享机，GPU 争用下吞吐波动可达 3 倍；生产 Worker 需要独占 GPU。

---

## 1. 研究单元、资源与口径

### 1.1 单元索引

| 单元 | 笔记 | 主题 | 运行处 | 保留镜像（dmax） | 状态 |
|---|---|---|---|---|---|
| r1 | [r1-fast-livo2.md](r1-fast-livo2.md) | FAST-LIVO2 源码精读、真机数据实测，以及"沙盘传感器 → bag → FAST-LIVO2 → 对 GLB 真值评测"闭环 | dmax CPU | `d3-r1-fast-livo2:noetic`（4.05 GB）、`d3-r1-simgen:py311`（2.12 GB） | 完成 |
| r2 | [r2-glb-world.md](r2-glb-world.md) | GLB → World Package：米制配准、调平、BVH、DSM、碰撞、体素、NavMesh、锚点、浏览器 LOD、几何不完整标注、world.json 扩展 | 本机 + dmax | `d3-r2-glb-world:0.1`（2.59 GB） | 完成 |
| r3 | [r3-camera-render.md](r3-camera-render.md) | 服务端相机渲染后端对比、帧契约、动态前景深度合成、可见度、天气、3DGS 路径、Sensor Service 设计 | dmax GPU | `d3-r3-camera-render:v1`（19 GB） | 完成 |
| r4 | [r4-mesh-lidar.md](r4-mesh-lidar.md) | 网格 LiDAR（Warp、OptiX、Embree）、逐点时刻、动态实体、天气、Livox 格式、FAST-LIVO2 闭环、每机私有地图 | dmax GPU | `d3-r4-lidar:0.3`（2.63 GB） | 完成 |
| r5 | [r5-gazebo-realistic-backend.md](r5-gazebo-realistic-backend.md) | Gazebo Harmonic 作为 Realistic 档：GLB 载入、碰撞与物理、磁带驱动 actor、传感器吞吐、同步、ROS 2、PX4 SITL | dmax GPU | `d3-r5-gazebo:harmonic`（3.36 GB）、`docker.1ms.run/px4io/px4-sitl-gazebo:latest`（2.18 GB） | 完成 |
| r6 | [r6-event-runtime-benchmark.md](r6-event-runtime-benchmark.md) | 事件 DSL、NavMesh 行人与车辆、语义验证、Canonical Event Tape、观测隔离、通信模型、Agent SDK、数据集与指标、2025–2026 基准调研 | 本机 + dmax CPU | `d3-r6-event-benchmark:py311`（549 MB） | 完成 |
| r7 | [r7-d2-code-audit.md](r7-d2-code-audit.md) | D2 代码审计：复用、扩展、冲突与改造清单、工作量、不影响 D2 的分层方式 | 本机 | — | 完成 |
| r8 | [r8-rerun.md](r8-rerun.md) | Rerun 数据模型、`.rrd`、catalog 查询、Web Viewer、MCAP 互通 | 本机（后续上 dmax） | — | 完成（2026-10-12 补记：§9–§11 实测章节已落盘，结论与建议见 r8-rerun.md §0 与 §10；本索引 §2.8 的数字以笔记为准） |
| r9 | [r9-gz-sim.md](r9-gz-sim.md) | gz-sim 源码机制、外部权威时钟锁步（C++ `Drone4DClock`）、确定性、速度控制修正、GPU 选择、Realistic 后端接口 | dmax GPU | `d3-r9-gz-sim:harmonic`（与 r5 共享层）、`d3-r9-gz-sim:dev`（3.52 GB） | 完成 |
| r10 | [r10-rotorpy-camera-sensor.md](r10-rotorpy-camera-sensor.md) | RotorPy 与其 v3.0 相机、照片级相机原型（时间语义、效果链、宿主适配、服务化）、3DGS 吞吐、Rerun 记录 | dmax GPU | `d3-r10-rotorpy:0.1`（17 GB） | 完成 |
| r11 | [r11-skyfall-gs.md](r11-skyfall-gs.md)（插图 `r11-assets/`） | Skyfall-GS 论文与代码研读、许可审查；"网格 → 3DGS → 扩散逐级细化"高斯外观层的适配方案（D3-SKY）与实测（crop 150 m 街区，网格蒸馏基线与 4 个课程式细化变体），决策门 G-3DGS 对照，与 P600 实拍重建路线的比较 | dmax GPU 2、3 | `d3-sky:v1`（19.5 GB，基于 `d3-r3-camera-render:v1`）；模型权重 41 GB 与实验缓存 17 GB 在 `/data/anet-drone4d-d3/sky/` | 完成（2026-10-12 补入；结论见 §10） |
| r12 | [r12-supersplat.md](r12-supersplat.md)（插图与结果 `r12-assets/`） | SuperSplat 编辑器（3.5.2 与 v2 分支 2.32.5）、SuperSplat Viewer 1.37.0、splat-transform 3.10.1 与 PlayCanvas engine 的源码研读与实测：高斯资产体检、清理、格式（PLY、压缩 PLY、SOG、流式 SOG、SPZ）与精度、坐标约定、浏览器渲染规模（1.12M–17.85M 高斯，Viewer 与 Spark）、编辑操作与相机时间轴的无头自动化、splat-transform 光栅化器；对 M22 发布流水线、内部编辑工具、Lab 检视视图、Spark 共渲染与事件编辑器选择交互的采用建议 | 本机（SwiftShader）+ dmax GPU 2、3 | 未构建新镜像（使用已有 `mcr.microsoft.com/playwright:v1.47.0-jammy`）；工作目录 `/data/anet-drone4d-d3/r12-supersplat/`（6.8 GB，可由脚本重建） | 完成（2026-10-12 补入；结论见 §11） |

D1/D2 前序研究仍然有效：`r03`（3DGS/gsplat）、`r05`（FAST-LIO/LIVO/R3LIVE）、`r06`（Open3D）、`r10`（3D Tiles）、`r13`（Potree-Next/splats）、`r20`–`r23`（PX4、Gazebo/AirSim）、`n03`（无人机仿真与 Agent）。

### 1.2 统一尺度口径与跨单元修正（引用米制数字前必读）

各单元并行开展时尚未统一尺度，**以 r2 的配准结果为唯一口径**：town 8.374 m/单位、crop 20.935 m/单位、hill 2.49 m/单位（单位指应用全部 glTF 节点矩阵之后的场景单位）。

| 单元 | 实际使用的尺度 | 与 r2 的偏差 | 需要按正确尺度复测的结论 | 与尺度无关、可直接采信的结论 |
|---|---|---|---|---|
| r1 | town 8.4 m/单位 | 一致 | — | 全部 |
| r2 | 基准 | — | — | — |
| r3 | town ×4.0（4 m/单位），hill ×2.0 | town 低估 2.09 倍，hill 低估约 1.25 倍 | 纹素、边长、"纹理受限距离"（town 实际约 29 cm 纹素，720p、HFOV 60° 时临界斜距约 320 m，不是 153 m）；3DGS 瓦片实际约 335 m 见方，训练航高约 63–188 m，"低空"约 25–52 m；动态演员相对场景被放大约 2 倍，可见度百分比需复测（真实尺度下遮挡可能更多） | 渲染耗时、显存、后端一致性、C1/C2/C3 合成正确性的对比结论、编码耗时 |
| r4 | town 直接以场景单位当米（535 m 宽，比真实小约 8.4 倍） | 小约 8.4 倍 | 航高与有效点率表、DSM 与网格偏差的绝对值（2 m DSM 相当于真实 16.7 m 网格）、FAST-LIVO2 ATE 1.7 / 4.0 cm 的绝对值、私有地图覆盖率与 TSDF 误差 | 求交吞吐、管线各项开销、后端一致性、"逐点位姿比单一位姿好约 10 倍"、"只发有效点会触发时间修正"这两个相对结论 |
| r5 | town 用场景单位；事件舞台 stage1 按 20.9 m/单位 | town 小 8.4 倍；stage1 一致 | town 场景中的米数 | 物理步长、传感器吞吐、actor 偏差（stage1）、GLB 对齐规则 |
| r6、r7 | crop 20.9 m/单位（舞台未做 168° 真北偏航） | 一致（差 0.2%） | 舞台坐标轴与真北差 168°（事件内部不受影响） | 全部 |
| r9 | town 沿用 r5 的对齐（场景单位） | 小 8.4 倍 | 航高 45 m 等米数 | 锁步、确定性、吞吐 |
| r10 | town 场景单位（535 × 320 × 28） | 小 8.4 倍 | 相机位姿高度 | 吞吐、画质对比 |

另有两处交叉印证：r1 在正确尺度下测得倒装 MID-360 于 30 m AGL 的有效回波约 28.6%，r4 在缩小世界中测得 11%；r7 在正确尺度下测得 2 m DSM 的视线不一致率为 13.3–13.6%，r4 在缩小世界中测得的命中误判率为 41%。定性结论（LiDAR 建图需要低空；DSM 不能当真值）不变，数值以正确尺度为准。

### 1.3 dmax 资源核查（2026-10-11 17:24 CST）

| 项 | 状态 |
|---|---|
| 容器 | 无任何 `d3-*` 容器残留 |
| GPU 2、3 | 显存占用 6 MiB，利用率 0%（其余 GPU 属其他服务，未触碰） |
| 保留镜像 | 见 §1.1，名义合计约 60 GB（r5/r9 有共享层） |
| 工作目录 `/data/anet-drone4d-d3/` | 合计约 61 GB：r1 47 GB（含 MARS-LVIG HKisland03 10 GB 与各场景 bag、PCD）、r4 4.1 GB（12 个模拟 bag，`sim_*_z.bag` 为正确合同）、r8 2.2 GB（传输中）、r3 2.1 GB、r10 2.0 GB、r6 1.1 GB、r2 0.87 GB、r5 0.38 GB、r9 0.18 GB |
| 共享数据 | `/data/anet-drone4d-d3/data/` 下三份 GLB，共 634 MB，sha256 已由 r3 核对 |

---

## 2. 关键实测数字

除特别注明外，"dmax"指 A40 48 GB、容器 `--cpus 16`；"本机"指 8 核 Xeon E5-2603 v4、无 GPU。GPU 2、3 在实验期间被多个单元共用，争用条件下的数值已在各笔记单列，下表取安静条件下的复测值。

### 2.1 世界导入与几何查询（r2、r6、r7）

| 项 | town | crop | hill |
|---|---|---|---|
| 三角形 / 纹理 | 2.00M / 16 × 4096² | 2.88M / 16 × 4096² | 3.00M / 1 × 8192² |
| 米制尺度（m/单位） | 8.374（±0.5%） | 20.935（继承 town 的局部坐标） | 2.49–2.53 |
| 真北偏航 / 倾斜 | 168.04° / 1.25°（墙面法 0.88°） | 同 town | 316° / 1.12° |
| 范围 / 边长中位数 / 纹素 | 4.6 × 2.8 km / 3.3 m / 28 cm | 2.5 × 1.7 km / 1.9 m / 19 cm | 122 × 145 m / 13 cm / 2.5 cm |
| 扫描覆盖 / 包围盒 | — | 61%（覆盖内另有 3% 洞、147 个开放边界环） | 74% |
| 单世界构建（不含配准，dmax） | 约 6 分钟 | 约 6 分钟 | 约 3 分钟 |
| NavMesh 行人烘焙（单线程 WASM，本机） | 225 s | 237 s，最大连通岛 1.13 km²（街道网） | 12 s（10% 网格） |
| BVH 建树：Open3D（dmax）/ Warp LBVH | 1.1 s / 17 ms | 1.3 s / 38 ms | 1.4 s / 25 ms |
| gltfpack `-cc -tc` | 68.8 MB | 74.2 MB | 32.1 MB（`-tl 4096` 时 21.9 MB） |

| 其他 | 数值 | 来源 |
|---|---|---|
| Open3D Embree 射线吞吐 | dmax 16 核持续 18–33 M/s；640 × 480 每帧 6.6–7.4 ms；与 Warp 命中 100% 一致，深度差 < 1 cm 的占 99.9999% | r2 |
| 每世界 BVH 常驻内存 | 约 300 MB（D2 沙盒每会话预算 140–155 MB） | r2 |
| DSM 2 m / 0.5 m（town，dmax） | 8.7 s / 34 s；本机 0.5 m 峰值 RSS 23.5 GB，需分块流式 | r2 |
| 碰撞减面（meshopt 10%） | hill 9.0 s，p95 4.1 cm；crop 10.3 s，p95 1.28 m（与原网格自身分辨率相当） | r2 |
| 车辆 NavMesh | 碎成 2221 块，不可用，改用 OSM 道路图 | r2 |
| 语义代价 + OSM 先验寻路 | 路径落在 OSM 道路走廊的比例从 37% 升到 74%，经过植被的比例从 14% 降到 2% | r2 |
| OSM 自动锚点（crop） | 151 个路口、70 栋建筑、30 个 POI；吸附到 NavMesh 的中位距离 0.84 m | r2 |
| SwiftShader（公开站 Tier S） | 每秒约 0.6–0.7 M 三角形，3M 三角形每帧约 4 s，只能显示网格采样的彩色点云 | r2 |
| 事件舞台 stage1（600 m × 600 m，47.7 万三角形） | 行人 NavMesh 在 dmax 上 24.7 s，车辆 11.3 s；两台机器 sha256 相同 | r6 |
| 2.5D DSM 视线 vs 网格（正确尺度，6,087 对 × 3 条射线） | 2 m DSM 不一致 13.3–13.6%，0.5 m DSM 降到 2.9–4.0%；17.8% 的演员脚点低于 2 m DSM 0.5 m 以上 | r7 |
| D2 WorldQuery 调用面 | 104 处调用中 93 处是 2.5D 语义，可继续使用网格派生的 DSM/DTM；只有 11 处需要网格精确求交 | r7 |

### 2.2 相机渲染（r3、r10）

| 后端（town，RGB + 深度 + 实例，含读回） | 720p | 1080p | 结论 |
|---|---|---|---|
| moderngl / EGL，MRT 一遍 | **1.37–1.59 ms**（其中 draw 0.34–0.38 ms） | **2.86–3.04 ms**（draw 0.40–0.48 ms） | 产品默认 |
| nvdiffrast 0.4.0，B=1 / B=8 | 6.83 / 4.53 ms | 10.9 / 13.9 ms | 许可仅限研究与评估，只作原型与对拍 |
| nvdiffrast 原型 + 畸变 + ISP + nvjpeg（r10），B ≥ 8 | 101–112 fps | — | 640 × 480 时 184–196 fps |
| three.js r186 无头 Chromium（ANGLE GL-EGL） | 5.2 + 3.7 ms（经 CDP 导出 240–460 ms/帧） | — | 只作前端一致性校验；默认参数会退回 SwiftShader |
| pyrender / Open3D Filament | 10.7 / 26.3 ms | 19.9 / 39–44 ms | 不采用：前者深度为 24 位非线性缓冲，后者色彩管线不直通 |
| Blender EEVEE / Cycles OptiX 16 spp（开 persistent data） | 190–250 / 130–160 ms | — | 只做离线金标准 |
| Gazebo Ogre2（r5、r9） | 每次更新约 11–13 ms，瓶颈在 CPU 渲染线程 | — | 只作交叉验证 |

| 其他 | 数值 |
|---|---|
| 跨后端一致性 | 与 nvdiffrast 相比，平面深度差中位数 0.008–0.32 mm，整体像素偏移 ≤ 0.06 px |
| 动态前景合成（60 个视角 × 5 个实体，720p） | C1（网格背景 + 网格深度）IoU 均值 0.9997；C2（3DGS 颜色 + 3DGS 期望深度）IoU 0.892，误显示像素占真值的 14.3%；C3（3DGS 颜色 + 网格深度）与 C1 完全相同 |
| 可见度标签（以演员为焦点的视角；尺度见 §1.2） | 完全可见 1.7%，部分遮挡 70%，完全遮挡 28%；被挡像素 51% 来自屋顶与树冠，47% 来自立面 |
| 天气与效果（720p，GPU） | 雾 0.3 ms；雨（消光 + 雨线 + 湿润）1.7–2.4 ms；动态实体阴影 4.0–4.7 ms；传感器噪声 < 0.5 ms |
| CPU 编码（单线程） | 720p：JPEG 3.0 ms、深度 PNG16 23 ms、实例 PNG16 16 ms；1080p 每帧合计约 82 ms |
| 预算 | P0（3 机 × 10 Hz × 720p，全输出）：约 0.1 GPU + 1.3 核；10 机 × 15 Hz × 1080p：约 0.5 GPU，但需要约 12 核编码，要改用 nvJPEG 与更轻的深度编码 |
| 显存（单世界常驻） | GL 路径估算约 1.5 GB；nvdiffrast 用 16384² 浮点图集 4.2 GB，降到 8192² 为 1.2 GB 且速度不变 |
| RotorPy v3.0 相机 | 不是渲染器，而是特征点投影 + 方块溅射；把小镇采样成 1M–16M 个特征点后 PSNR 只有 6.8–18.5 dB，不能承担照片级观测 |

### 2.3 LiDAR（r4、r1）

| 项 | 数值 |
|---|---|
| Warp `mesh_query_ray`（cubql） | 建树 0.26 s；核函数 250–790 M 射线/s；单架 MID-360 一帧（2 万射线）的核函数约 0.07 ms |
| 完整管线，100 架 × 1 帧 | 基线 5.34 ms；逐点位姿 +1%；纹理反射率 +6%；64 个动态实体 +18%；天气开销不可测 |
| 单卡持续吞吐（扫描 + GPU 压缩 + 回传有效点） | 每 0.1 s tick：1 / 100 / 200 / 400 / 800 架分别 1.96 / 11.3 / 21.3 / 44.3 / 191 ms；实时上限约 500–600 架 |
| OptiX（Mitsuba/Dr.Jit） | 批量 ≥ 200 万射线时 2.3–5.2 G 射线/s；2 万射线小批量只有 80–190 M/s |
| Open3D CPU（16 线程） | 11–20 M 射线/s；与 GPU 后端的距离差 ≤ 0.28 mm，4 万射线中至多 1 条命中判定不同 |
| nvdiffrast 立方体深度近似 | 每架每帧约 20 ms，比精确求交慢约 280 倍，不采用 |
| 运动畸变（缩小世界，相对结论可用） | 8 m/s 时误差中位 0.47 m，90°/s 偏航时 p95 3.5 m；FAST-LIVO2 在 8 m/s 时逐点位姿 ATE 4.0 cm，单一位姿 43 cm |
| 天气（905 nm） | 10 mm/h 雨量程只降约 3%，主要影响是近场假回波；V = 200 m 的浓雾量程降约 30% |
| 输出格式 | ROS1 Livox CustomMsg 手工拼装 1.03 ms / 2 万点，380 KB，md5 `e4d6829b…` 与 livox_ros_driver 及 driver2 一致；`awr.sensor.lidar_frame.v1` 的 22,000 点上限需要升到 v2 |
| 私有地图（Warp 稠密 log-odds + TSDF） | 积分 0.14 ms/帧，Frontier 提取 0.7 ms；3 机各覆盖 22–31%，合并后 63%，两两 Jaccard 0–0.24；稠密存储 831 MB/机，稀疏块估算 32–43 MB |
| 有效点率（正确尺度，r1） | 倒装 MID-360 在 30 m AGL 时约 28.6%；正下方有半角 38° 的盲锥 |
| 数据量 | 有效点约 2 MB/s/机；ROS 全点流 3.8 MB/s/机；100 架 5 分钟约 57 GB（未压缩） |

### 2.4 3DGS（r3、r10）

| 项 | 数值 |
|---|---|
| 从网格训练（town 瓦片，360 张 720p 训练图） | 只初始化（1.5M 面元）23.1 dB；`scene_scale = 10` 时 2k 步 33 s 达 31.8 dB；30k 步 710 s，同分布 34.0 dB、低空 31.4 dB、极低空 28.3 dB；训练峰值显存 7.8 GB |
| 学习率陷阱 | gsplat 默认按相机分布半径缩放位置学习率，航拍场景 7k 步只有 22.7 dB，低于不训练 |
| 渲染（3M 高斯，SH0） | 720p 2.4–3.0 ms（B=8 时 1.6–2.1 ms/帧）；1080p 2.9–3.8 ms；RGB+ED 多约 0.5 ms |
| 更大规模（r10） | 4M 高斯 320–480 fps；16M 高斯 92–156 fps；"3DGS 背景 + 网格前景 + ISP"，720p、B=8：4M 时 184 fps，16M 时 124 fps |
| 存储与覆盖 | 每瓦片 160 MB；覆盖全镇需 100 个以上瓦片、约 16 GB 参数、数十 GPU 小时；必须同时驻留相邻瓦片，否则视野里会出现空洞 |

### 2.5 Gazebo（r5、r9）

| 项 | 数值 |
|---|---|
| 无头渲染 | Docker + A40 + EGL 可用，需补 `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`；Ogre-Next 固定使用 0 号 EGL 设备，只能在容器层面绑定 GPU |
| GLB 载入 | Assimp 直接读取；三份 GLB 首帧 7.3–13.4 s（town 13.4 s，r9 测得 13–16 s），RSS 2.0–3.0 GB，显存 1.3–2.4 GB；不支持 Draco、meshopt、KTX2，导出前需规范化；需在 pose 上加 roll = +π/2；对齐后深度与 DSM 的差值中位数 0.6 mm |
| 物理 | 10 架 x500，碰撞网格从 2 万到 200 万三角形，每步都在 1.75–1.97 ms；每步约 0.7 + 0.12·N ms，单进程 250 Hz 实时上限约 25 架 |
| 原生传感器 | 每进程约 90 次更新/s；全套传感器下 1 / 3 / 5 / 10 架的 RTF 为 2.58 / 0.84 / 0.62 / 0.25；同卡 4 个进程总吞吐 3.3 倍 |
| 锁步（r9，C++ `Drone4DClock` 进程内门控） | 3 架 ×（720p RGB + 900 × 40 LiDAR）RTF 0.95；自由运行 1.18；纯物理 8.26；6 架 0.45；同卡双实例总吞吐 1.58 倍；单实例约 3 核、2 GB 显存 |
| 确定性 | 同镜像、同型号 GPU（GPU 2 与 3 之间）位姿逐位一致、帧逐字节一致（需 `--seed`）；有无渲染传感器不影响物理 |
| 进程外 Python 锁步 | 0.37–0.46 RTF；Python `request()` 持有 GIL，会丢应答 |
| actor | SDF 轨迹偏差中位数 0.29 m；进程内驱动校正后 0.57 m，每步 0.87–0.95 ms/43 个实体；无碰撞、无重力（20 kg 箱子被穿过只移动 0.010 m）；`<loop>false</loop>` 加任意渲染传感器会卡死在首帧（gz-sim 8.15 缺陷） |
| 速度控制 | 上游 `MulticopterVelocityControl` 按完整机体姿态解释速度指令，机动时掉高；分叉加 `commandFrame=world` 后，定高误差从 −87 m 降到 +1.4 m 以内 |
| PX4 SITL | 1 / 3 / 5 / 10 架在 9–11 s 内就绪，起飞后 RTF 1.00 / 1.00 / 0.99 / 0.955；每架 0.22–0.34 核；需要 GCS 心跳与平整起降垫 |
| 雨 | 粒子发射器只影响 0.06% 的 LiDAR 射线，却让 RTF 降一半；雨雾应在 Sensor Service 中实现 |

### 2.6 FAST-LIVO2（r1、r4）

| 项 | 数值 |
|---|---|
| 上游与依赖 | `main@0d2c034`（2026-03-08），GPL-2.0，只有 ROS1；Sophus `a621ff` 需一行补丁；vikit 用 xuankuzcr 分叉；catkin 48 s，镜像 4.05 GB |
| 真机数据（MARS-LVIG HKisland03，Avia @ 90 m，1.8 km） | 对 RTK 的 ATE 1.02 m（0.056%）；LIO 74 ms + VIO 14 ms/帧；3.7 核；峰值 12.7 GB；`filter_size_surf 0.5` 时 LIO 降 23%、CPU 降 40%，精度不变 |
| 模拟闭环 clean 档（town 8.4 m/单位，883 m 环线，30 m AGL） | 最大锚定漂移 0.46 m；ATE 0.14 m；RPE100 0.08%；彩色地图到网格距离均值 9.6 cm；颜色 MAE 5.8 |
| realistic 档 5 个种子（LIO） | RPE100 0.25 ± 0.02%；ATE 0.60 ± 0.16 m；最大漂移 2.0 ± 0.5 m |
| 合同陷阱 | 丢弃无回波点会触发 `preprocess.cpp:183` 的级联时间修正：最大漂移从 0.46 m 变成 1.38 m；加 0.3 m 湍流后 3 次全部发散；轨迹二阶导不连续会让估计在起飞瞬间跳到 47° 横滚 |
| 批量 | 8 实例 × 2 核 × 4 倍速回放，8 段 159 s 航迹 122 s 跑完（16 核约 10 倍实时）；每实例 RSS 2.2–5.3 GB；64 核约可并行 30 个实例 |
| 建图基线 | 真值位姿 TSDF F@0.2 为 0.85；换成 FAST-LIVO2 位姿后降到 0.23–0.33，瓶颈在定位而不在地图表达；首版 TSDF（0.15 m、80 m 深度）内存涨到约 400 GB，被人工中止 |
| 链路瓶颈 | CPU 光线投射相机每帧 196–222 ms（比实时慢 2–7 倍），必须改用 GPU 渲染 |

### 2.7 事件运行时与 Benchmark（r6、r7）

| 项 | 数值 |
|---|---|
| 事件磁带生成（360 s，3600 帧，运动学方式） | 背景 0 / 40 / 200 / 1000 人时本机 1.2 / 4.2 / 13.4 / 58.9 s；dmax 背景 40 人 1.2 s |
| 磁带体积 | 按演员分列 + 差分 + zstd 后约 17–21 B/演员/s（43 名演员 360 s 共 0.27 MB） |
| 几何预检 | design-r1 式凭直觉填写的 8 段路线中 5 段不可行或不连通，所需速度最高 7.27 m/s（街道路径比直线长 1.3–2 倍） |
| 人群推进 vs 运动学推进 | 人群方式：背景 200 人时 B 迟到 45 s，1000 人时 8 个计划到达只实现 3 个；运动学方式：到达误差 0 |
| 语义验证 | 作者 5 条声明 4 条通过，并发现 1 条未声明的遮挡事件；可观测性 oracle 44.7 万条射线约 0.5 s |
| 6 种策略 × 12 个种子（L1） | EC：覆盖扫描 0.25，单机悬停 0.50，3 机分守 0.75，3 机跟踪 0.88–0.90，5 机共享跟踪 1.00；覆盖扫描 AO_pos 0.92 而 AO_real 0；U-F1 0.53–0.65 |
| 运行速度 | 单次 360 s 仿真 4.2–7.7 s（47–86 倍实时）；72 次运行用 10 个进程 60 s 完成 |
| 机队规模（L1，单进程） | N = 1 / 3 / 10 / 30 / 60 时仿真 4.3 / 9.1 / 29.3 / 78.4 / 194.4 s；投递次数约 370–410 × N²；链路状态缓存 1 s 后 N = 30 时耗时降 39% |
| D2 识别物反应（r7） | 缺省反应下，1 架悬停使 A 偏离 53.9 m、晚到 38.5 s；盘旋或跟踪时 A 到不了终点；关闭反应后 5 种策略的轨迹 sha256 相同 |
| 以 tick 为键的随机数（r7） | 只把时钟档从 sandbox100 换成 d1_250，random_walk 在 0.6 s 后分叉，120 s 内平均偏差 13.9 m |
| D2 Track/hermite 作为磁带播放器（r7） | 磁带带速度时，1 Hz 插值误差 ≤ 4.47 cm，2 Hz ≤ 1.16 cm，5 Hz ≤ 0.39 cm；每个样点 0.4–3.6 µs |
| 数据集体积 | L1 3 机 360 s：raw.mcap 439 KB；L2 3 机 360 s 估算 5–7 GB |

### 2.8 录制与数据量（r8、r10）

| 项 | 数值 |
|---|---|
| Rerun 写入 | 3 机 × 60 s（RGB/深度/分割 640 × 480 @ 5 Hz，LiDAR @ 10 Hz）14.0 s，其中 SDK 3.9 s；10 机 × 300 s 用 279 s，编码是瓶颈；`send_columns` 让 SDK 时间从 12.3 s 降到 3.9 s |
| 体积 | JPEG 档每机每分钟 192 MB，其中深度占 67%；推荐档（H.264 + PNG/RVL 深度）约 104 MB/机·分钟 |
| Web Viewer | Wasm 49 MB（gzip 后 16 MB）；内存上限 2.5 GB；与 three.js 沙盘双向时间联动已验证 |
| r10 记录开销 | 每帧 1.3 ms；162 MB 载荷写成 88 MB 的 `.rrd` |

### 2.9 数据传输与环境（各单元）

| 项 | 实测 |
|---|---|
| 本机 → dmax（ZeroTier rsync） | 1.75–2.3 MB/s，GLB 三份共 664 MB 约 5 分钟；未用 ModelScope |
| dmax 外网下载 | hf-mirror 单连接约 2 MB/s；12 路 HTTP Range 并行约 49 MB/s（10 GB 用 204 s）；FAST-LIVO2 官方 Google Drive 数据集配额受限；NTU-VIRAL 超时 |
| 容器网络 | Docker 默认 DNS 无效，需要 `--dns 223.5.5.5` 或 host 网络；legacy builder 无 DNS，BuildKit 正常；Docker Hub 超时，PX4 镜像经 `docker.1ms.run` 拉取；本地缓存的 `python:3.12-slim` 是 arm64 镜像 |
| 公共 OSM 服务 | Overpass 三个端点超时或返回 500；生产应改用 Geofabrik 离线抽取并记录 sha256 |

---

## 3. 对 design-r1 的确认与修正

### 3.1 实测成立的建议

| design-r1 建议 | 实测证据 | 结论 |
|---|---|---|
| 三种权威分离（World/Event、Flight/Physics、Agent） | 关闭反应后 5 种策略的事件轨迹哈希相同（r7）；Gazebo actor 不参与物理（r5）；Sensor Service 是纯函数（r3） | 成立，作为 D3 的第一条架构约束 |
| GLB 派生多种表达（外观、碰撞、射线、DSM、体素、NavMesh、锚点） | r2 实现了 13 个构建阶段，dmax 上每个世界 3–6 分钟 | 成立；补充 quality（几何不完整）、semantic（逐三角形语义）、navgraph（车辆路网）三层 |
| Canonical Event Tape 与 Exogenous 主模式 | 磁带跨机器逐字节相同，且与背景密度解耦（r6） | 成立 |
| Exogenous / Interactive 区分 | D2 缺省反应让演员偏离 53.9–570 m（r7） | 成立；Interactive 放到 P2 |
| 语义事件由世界状态计算，不凭声明成立 | 5 条声明 1 条不成立，另发现 1 条未声明的事件（r6） | 成立；strict 模式下声明不成立即编译失败 |
| RGB/Depth/Seg/LiDAR 对应同一时刻 | gz 同一渲染批次时间戳差为 0（r9）；Sensor Service 按曝光中点或逐点时刻从磁带取演员状态（r3、r4、r10） | 成立；需要避开 D2 的 stage 次序（识别物 stage 170 在传感器 100–109 之后） |
| 3DGS 背景 + 动态前景按深度合成 | C3 与网格真值完全一致，C2 误显示 14.3%（r3） | 成立，但"深度"必须是网格深度 |
| Gazebo actor 不参与刚体碰撞 | 箱子只位移 0.010 m（r5、r9） | 成立 |
| 区分建图与完整 SLAM | 真值位姿 TSDF F@0.2 0.85，FAST-LIVO2 位姿 0.23–0.33（r1） | 成立；分成 L0（已知位姿建图）与 L1（SLAM 在环）两条轨道 |
| 不要求跨 GPU 逐像素一致，按容差判定 | 同型号 GPU 逐位一致（r9）；CPU/GPU 求交深度差 < 1 cm 的占 99.9999%（r2）；磁带跨机逐字节一致（r6） | 成立；细化为三级：磁带与 CPU 档逐字节，同镜像同型号 GPU 逐字节，跨环境按容差 |
| 高保真运行需要独立 GPU Worker，不进公开沙盒 | BVH 约 300 MB 超过每会话预算；emax 无 GPU（r2、r7） | 成立 |
| 事件 DSL 采用"锚点 + 时刻" | r6 采用并补全为 `drone4d.event.v1` | 成立，但需要几何预检与自动排时（见 §3.2） |
| 先跑通 M22、M23、M25 的最小闭环 | r7 的 P0 关键路径正是 W1→W4→E1→E2→E3→A1→B3 | 成立 |
| 浏览器保持为统一产品界面 | three.js 与服务端深度一致到 0.05 mm（r3）；彩色点云可零改动接入 M05（r2、r7） | 成立 |
| Gazebo 默认物理引擎是 DART，Bullet 是可选碰撞配置 | DART + bullet 检测器表现最好，物理步长与碰撞网格规模无关（r5） | 成立 |
| 不应一开始就支持百架级全分辨率 | 相机 720p 单卡在线实时约 7 架（r10，nvdiffrast 原型）；gz 每进程约 2.5 架实时（r5） | 成立；L1 档可以跑数十架（60 架 1.9 倍实时），L2 档按 GPU 预算限额 |

### 3.2 需要调整的建议

| # | design-r1 的说法 | 实测发现 | 修正 |
|---|---|---|---|
| 1 | GLB 是"带颜色、带结构"的点云或 3DGS 沙盘 | 三份都是带烘焙光照的纹理三角网格，非米制、有倾斜（r2、r3、r4、r5） | M22 必须做尺度、真北、调平标定并留存证据；未标定的世界不能加载事件。"3DGS + mesh"改为"网格是几何与外观真值，3DGS 是可选外观层" |
| 2 | Photorealistic 档 = 3DGS 相机渲染 | 从网格训练的 3DGS 不比网格真实，低空掉 6–7 dB，瓦片外为空（r3） | P0–P2 用网格渲染；3DGS 只用于真实航拍重建的资产（P3，需另行获取资产），并且一律采用 C3（3DGS 颜色 + 网格深度） |
| 3 | Realistic 档 = Gazebo 物理 + Gazebo 传感器；第一版 RGB 用 Gazebo Mesh RGB | gz 每次传感器更新约 11 ms，CPU 渲染线程是瓶颈；EGL 光栅 1.4 ms；gpu_lidar 只能输出均匀网格 | 第一版相机就用 Sensor Service 的 EGL 光栅；Gazebo 只提供动力学与接触，原生传感器作为交叉验证与 ROS 生态出口 |
| 4 | LiDAR 高保真版用 Gazebo GPU LiDAR 加扫描花样 | gpu_lidar 不支持非重复花样与逐点时刻；单一位姿数据会让 LIO 的 ATE 恶化约 10 倍（r4、r9） | 两档都用 Warp MeshLidar（逐点时刻，K = 11 个位姿样本/帧）；gz 射线表分叉只作为可选项 |
| 5 | NavMesh 由"地表几何与障碍提取"得到 | 树冠、屋顶、水面被当作可走面；直接删除树冠会把街道网打碎；车辆 NavMesh 碎成 2221 块（r2） | Recast 烘焙 → 种子洪泛裁剪 → 语义区域代价 → OSM 道路先验 → 编辑器人工补 off-mesh 连接；车辆改用 OSM 道路图 |
| 6 | 事件 DSL 中直接写到达时刻 | 凭直觉写的时刻多数不可行（r6） | 编译器做三道门：静态校验、几何预检（给出建议时刻）、生成后语义验证；编辑器实时调用预检 |
| 7 | 复用 M18 TargetRuntime 推进事件实体 | D2 航点没有时刻、遇墙永久卡住、按 DSM 顶面取高；GLB 小镇上 78–82% 的步被判为阻挡（r7） | 脚本演员走运动学 NavMesh 时间线（磁带），M18 增加 `motion.mode = timeline` 与 `reaction_policy = exogenous`，判据只作度量；背景人群走 DetourCrowd |
| 8 | M19 Johnson/TTPF 用于快速感知 | M19 帧带真值 id，航迹全队融合（r7） | 只能作为 L1 观测，且须经匿名化与按机私有化；Event Capture 真值取自渲染可见度（`visibility.json`） |
| 9 | Active Observation 看机队是否在合适时间选了有信息价值的位置 | 只看机位会严重高估（0.92 vs 0）（r6） | 采用 AO_real：以实际拿到的证据质量除以 oracle 最优质量 |
| 10 | Collaborative Efficiency 只看收益与成本 | 跟踪策略冗余度高（同时观察的机数 1.4–1.6，60 机时 27），协同收益只有 0–0.06（r6） | CE 同时报告冗余度、边际收益曲线、km 与 KB 成本 |
| 11 | 通信 `Message = f(…)` 作为可实验变量 | 全员广播的投递次数随机队规模平方增长；ANet 委派是秒级（r6、D1） | 链路层只在 Env 中实现一份，链路状态按 1 s 缓存；M14 ANet 作为运行在链路层之上的协同策略 |
| 12 | 沿用 D2 沙盒 REST/WS 接入 Agent | 沙盒 token 能读全部真值，接口是实时 pub/sub（r7） | Benchmark 走 M26 的 lockstep Env 与 `/api/bench/v1`；沙盒 REST/WS 只用于交互、演示与编辑 |
| 13 | 两个演示场景：小镇事件、山地协同探索 | 现有资产中没有山谷级场景；hill 只有 122 × 145 m；crop 比 town 更细（纹素 19 vs 28 cm）（r2） | crop 作为城镇级事件主场景；hill 作为街巷级近距离与 Re-ID 场景；town 作为大范围 LiDAR 探索与远景。山地场景需另行获取资产（§6） |
| 14 | P1 才做 LiDAR | MeshLidar 原型、FAST-LIVO2 闭环与输出合同都已验证（r1、r4） | 保持 P1 交付，但 P0 就冻结 LiDAR/IMU 输出合同与时间约定；是否把静态网格 LiDAR 提前到 P0 交由用户决定（§6） |
| 15 | 可复现即"种子一样" | D2 有 16 处随机数以主时钟 tick 为键，换时钟档就分叉（r7）；FAST-LIVO2 同一 bag 重复运行有 cm–dm 级差异（r1） | D3 新增随机流 19–21 以事件时间索引或帧序号为键；SLAM 结果按"种子 × 重复运行"报告分布 |
| 16 | 传感器生成"实际数据"即可 | MID-360 倒装 30 m 以上有效点迅速减少，正下方有 38° 盲锥（r1、r4） | 把机型与任务约束写进任务说明：建图类任务 AGL ≤ 30–40 m；高空观察任务不依赖 MID-360（只用相机，或另配长量程雷达模型） |
| 17 | PX4 SITL 作为一个档位 | 可跑 10 架，但时钟从动、结果不确定（r5、r9） | SITL 档只用于飞控验证，不进入正式 Benchmark 排名 |

### 3.3 单元之间的分歧与裁决

| 分歧 | 各方 | 裁决 |
|---|---|---|
| town 的米制尺度 | r3 估计 ×4（2140 m 宽）；r4 认为场景单位 ×1 看起来合理；r2 用 OSM 配准得 8.374 m/单位，有三条独立证据 | 采用 r2 的结果；r3、r4 的米制数字按 §1.2 重算 |
| Realistic 档的传感器来源 | r9 主张 gz-sim 负责基于 GLB 的传感器渲染；r5 主张默认共用 M25 Sensor Service | 采用 r5 的方案：gz 只提供位姿与接触，M25 统一渲染；gz 原生传感器作为可选的交叉验证（同一位姿下逐像素比对）与 ROS 生态出口。理由：吞吐（11 ms vs 1.4 ms）、MID-360 花样，以及 Fast 与 Realistic 的差异可以干净地归因于动力学 |
| 默认场景分工 | r3：town 用于中高空事件，hill 用于近距离；r2：crop 作为事件主场景 | crop 为事件主场景（街道网完整、OSM 锚点丰富、几何更细），town 用于大范围与 LiDAR 探索，hill 用于近距离；r6 已在 crop 上完成原型 |
| 车辆运动 | r6 用车辆 NavMesh（坡度 20°、可跨 0.3 m）但会走捷径；r2 认为车辆 NavMesh 不可用 | P0 允许用车辆 NavMesh 加锚点约束；P1 改为 OSM 车道图加跟驰模型 |
| 产品中是否嵌入 Rerun | D1 PRD NG-09 不把 Rerun 嵌入产品 UI；r8 建议把 Web Viewer 用于数据检视面板 | 主沙盘仍由 three.js 承担；Rerun 作为数据集与 Run 检视工具，是否嵌入网页由用户决定（§6） |
| 反射率编码 | D2 LidarScan8 用 ρ·255；r4 用 Livox 口径（漫反射 0–150） | D3 统一到 Livox 口径；LidarScan8 预览保持 D2 不变 |

---

## 4. 推荐的 D3 总体架构

### 4.1 总图

```text
 浏览器（three.js 沙盘、事件编辑器、无人机视角面板、多 Run 对比、数据集浏览）
    ^ 公开站：只读静态制品与 D2 实时 topic             ^ 实验 API（dmax，鉴权）：事件编译、Run 提交、Agent 连接
 ------------------------------------------------------------------------------------------------------------
 M27 Benchmark Runtime   Run 规格 = 世界 contentVersion + 磁带 content_sha256 + 机队 + 策略 + 物理档 + 观测档
                         + 噪声档 + 种子 → 队列 → dmax 池 → 无损写入（MCAP）→ 评测 → 数据集 / .rrd 导出
        |
 M26 Env（lockstep）      reset / step；观测过滤 L0–L3（唯一出口）；链路模型；动作 → CommandEngine + TrustedGuard
        |                 Agent 进程或容器：DroneAgent.step(obs, inbox)；私有地图（L0）；FAST-LIVO2 容器（L1）
        |
 物理权威（M24 档位）      Fast：FleetSim（D2 sim-core）｜ Realistic：gz-sim + Drone4DClock ｜ SITL：gz + PX4
        |  位姿窗口（10 ms 样本，含云台）
        v
 M25 Sensor Service       SensorBackend v2：EGL 光栅相机（RGB / 深度 / 实例 / 语义 / 可见度）、Warp MeshLidar、
 （dmax GPU Worker）       IMU / GNSS、天气效果链、3DGS 着色（P3）；ObsFrame → Agent，TruthFrame → 评测库
        ^ 磁带 DynamicSource（同一时刻的演员状态与环境）       ^ 世界资产（每 GPU 常驻 2–3 个世界）
 M23 Event Runtime        DSL → 三道门 → Canonical Event Tape（运动学脚本演员 + DetourCrowd 背景）→ 语义真值与事件图
        ^ NavMesh / navgraph / anchors / quality
 M22 World Asset Import   GLB → 米制世界包 1.1.0 / d3-glb：raycast、collision、DSM / DTM、quality、semantic、occupancy、
                          navmesh、navgraph、anchors、appearance（服务端）、彩色点云与 3D Tiles（浏览器）、可选 3dgs
```

**权威划分**

| 权威 | 负责 | 实现 | 不负责 |
|---|---|---|---|
| 世界与事件 | 静态世界、演员状态、环境时间线、语义真值 | M22 世界包 + M23 磁带（只读，所有运行共用） | 不感知无人机（Exogenous 模式） |
| 飞行与物理 | 机体动力学、接触、风对机体的作用、实际位姿 | FleetSim 或 gz-sim | 不决定事件发生了什么 |
| 观测 | 只按本机传感器的实际位姿与内参生成数据 | M25，纯函数 `O = Render(World, Tape(t), Pose(t), Rig, Env(t), seed)` | 不读 Agent 状态，不改变世界 |
| Agent | 基于本机观测与收到的消息做决策 | M26 Env 中的 Agent 进程或容器 | 不能读取磁带、真值、全局地图 |
| 评测 | 事件真值、可见度真值、oracle、评分 | M27 Evaluator（离线） | 运行期对 Agent 不可见 |

### 4.2 M22 World Asset Import

- **CLI**：采用 r2 的 `worldpkg glb inspect / register / build / export-gazebo / anchors`。构建分 13 个阶段，每个阶段可单独重跑；复用 M03 的 staging、原子发布、contentVersion 与 validate；新增 `GlbAdapter` 与 V-GLB 校验规则组。
- **必做步骤**：
  - 米制尺度、真北与调平。有 OSM 时用道路或建筑轮廓 FFT 配准；公里级场景用 DEM 回归调平，小场景用墙面竖直度。证据写入 `coordinate.json`，并带 `scaleStatus`。
  - 逐三角形粗语义：地面、路面、建筑、植被、水体、车辆。
  - 分辨率体检：纹素、边长、纹理受限距离，写入 `sensorHints`。
  - 太阳方位估计：烘焙光照无法重算，只能让动态实体按同一太阳方向着色。
- **world.json**：采用 `schemaVersion 1.1.0` + `profile = d3-glb` 做加法式扩展。保留 D1 必需图层（彩色点云、`dsm_2m`、`dtm_10m`、zones、env），D2 运行时不改代码即可加载 GLB 世界；新增 `navmesh`、`navgraph`、`anchors`、`quality` 四种图层类型与 6 个格式注册名。
- **派生资产**：

| 资产 | 格式 | 消费者 |
|---|---|---|
| `mesh.raycast`（焊接后的世界帧米制网格，加载时建 BVH，不序列化） | `awr-trimesh@1` | MeshWorldQuery、M25、评测 |
| `mesh.collision`（meshopt 减到 10%，大世界分块，每块 ≤ 5 万面） | `awr-trimesh@1` + STL | Gazebo |
| `dsm_2m`、`dtm_10m`、`dsm_2m_n`、`dsm_0p5m` | D1 格式 + 新层 | D2 WorldQuery、Grid25、规划、围栏（原样） |
| `semantic.quality`（洞、拉伸面、覆盖外、多层柱） | 位掩码栅格 + GeoJSON | 全链路的"不完整即未知"规则 |
| `occupancy` + ESDF | `awr-occ@1`（16³ 稀疏砖块，四态） | 评测真值、规划器 |
| `nav.person`（Detour 二进制）、`nav.graph`（OSM 道路图） | `detour-navmesh@1`、`awr-navgraph@1` | M23 编译期；浏览器编辑器预览 |
| `semantic.anchors`（OSM 播种，编辑器确认，id 跨重建保持稳定） | `awr-anchors@1` | 事件 DSL 与世界之间的唯一接口 |
| `appearance.glb`（只在服务端使用）、3D Tiles（meshopt + KTX2，按瓦片图集重排）、网格采样的彩色点云（`col.rgb = srgb`） | glTF、3dtiles@1.1、ANET_Q16 | M25；浏览器 Tier B/A；浏览器 Tier S |
| `appearance.3dgs`（可选，只用于真实航拍重建资产） | PLY / SPZ | M25 混合着色（P3） |

- **MeshWorldQuery**：继承 `DsmWorldQuery`，2.5D 方法改用网格派生的 0.5 m 栅格；`los_batch`、`ray_hit`、`free_distance` 覆盖为网格精确求交；新增 `raycast_batch`、`walkable_height`、`nav_path`、`semantic_of`。`open_world_query` 只在世界包有网格图层且 `AWR_D3_ENABLED` 打开时分派到它。CPU 用 Open3D/Embree，大批量交给 M25 的 GPU。
- **共享几何服务**：每个活跃世界一个进程持有 BVH，sim-core 会话通过 IPC 批量提交射线，不在每个会话内建树。
- **"不完整即未知"贯穿全链路**：occupancy 标为 unknown；`zones.border` 改用扫描覆盖多边形内缩（小世界 5 m、城镇级 20 m）；LiDAR 打到无数据区的点打 void 标记；相机输出无效掩码；评测只在可观测区计分。

### 4.3 M23 Event Runtime

- **DSL `drone4d.event.v1`**（草案 `.cache/research/d3/r6-event-benchmark/schema/event.v1.schema.json`）包含：
  - 演员与路线，路线用"锚点 + 到达时刻"表达；
  - 动作：spawn、wait、board、enter_building 等；
  - 背景人群与车流；
  - 环境关键帧：风、阵风、雨、能见度；
  - 通信干扰区；
  - 声明式注释与事件关系。

  速度不写，由编译器根据路径长度反算。
- **编译三道门**：
  1. 静态校验：schema、引用、时间单调、上车窗口。
  2. 几何预检：每段 NavMesh 路径长度除以可用时间得到所需速度，超出步态上限即不可行，并给出按步态中值排出的建议时刻。
  3. 生成后语义验证：从磁带与网格重新计算相遇、上车、进入建筑、区域经过、遮挡等事件，与声明对比。strict 模式下不一致即失败；未声明但实际发生的显著事件以 `declared: false` 写入真值。
- **磁带生成**：
  - 脚本演员沿 NavMesh 路径按时刻做运动学推进；
  - 背景人群用 DetourCrowd，为每名脚本演员设代理障碍，让背景避让；
  - 多人共用 zone 锚点时分配环形站位；
  - 生成器用 WASM（recast-navigation 0.43.1），位置做毫米定点化，保证跨机逐字节一致。
- **磁带格式 `drone4d.tape.v1`**：
  - 10 Hz；每演员每帧记录位置、朝向、速度、状态（缺席、移动、等待、在车内、在建筑内、离场）与容器；
  - 按演员分列 + 差分 + zstd 存储，每 10 s 一个关键帧块；
  - `content_sha256` 只覆盖规范头与帧数据，生成器版本、Node 版本、耗时等出处写进 `provenance`；
  - 骨骼动画给出动画片段与相位（P1），由渲染端做 LBS。
- **运行期**：
  - 磁带播放器复用 D2 `Track.build + hermite`（1 Hz 带速度时误差 ≤ 4.5 cm）。
  - M18 增加 `motion.mode = timeline` 运动源与 `reaction_policy = exogenous`：判据与状态机照跑，"被发现"作为隐蔽度量，运动不受影响。
  - 环境轨编译为 M07 定时操作，通信区交给 M26。
  - 传感器按曝光中点或逐点时刻直接查磁带，不经实体表，从而避开 stage 次序。
- **真值产物**：`event_truth.json`、`event_graph.json`（可导出 OpenLABEL）、`observability.npz`（关键时刻的视点质量 oracle）。
- **随机流**：新登记流 19 `event_runtime`、20 `comm_link`、21 `obs_fast`，键为"谁、第几个、哪一帧"，不用调用次数，也不用主时钟 tick。
- **Interactive 模式（P2）**：沿用 M18 反应，但 flee、hide 的目标点要落在 NavMesh 上；每次运行产出"实现磁带"，单独验证与评分；只比较同一初始条件下的结果分布。

### 4.4 M24 物理后端档位

| 档位 | 飞行权威 | 与网格的接触 | 时钟模式（D2 `ClockCaps.mode`） | 可复现 | 规模 | 用途 |
|---|---|---|---|---|---|---|
| Fast | FleetSim（D2 SoA、PX4-lite，D1 实测 1000 架 @ 250 Hz 单核 3.5 ms） | 用 MeshWorldQuery 做净空与碰撞判定（D2 目前只有 DSM 接触，需要补） | lockstep / 倍速 | CPU 档逐字节 | 数十至数百架 | Benchmark 主体、策略搜索、批量数据集 |
| Realistic | gz-sim（Harmonic 原型，生产评估 Jetty）：DART + bullet 网格碰撞、x500 旋翼模型、`Drone4DVelocityControl`（世界系指令）、标定后的 WindEffects | 有（10 万–50 万面 STL） | lockstep（进程内 C++ `Drone4DClock`）或 free_running | 同镜像、同型号 GPU 逐位 | 物理约 25 架/进程；锁步 3 架 RTF 0.95 | 高可信复跑、撞楼、贴地、抗风，以及标定 Fast 档的动力学误差 |
| SITL | PX4 + gz（官方镜像） | 有 | slaved_realtime | 不保证 | 10 架 RTF 0.96 | 飞控交互与失效保护，不进排行榜 |

- **共同规则**：
  - 三档都把位姿窗口送进同一个 M25；指令语义统一为世界系速度加偏航角速度，高层指令在 Drone4D 侧由同一个路径跟踪器换算。
  - 事件磁带在 gz 中由 `Drone4DClock`/TapeSystem 在 PreUpdate 中写入：actor 无脚本，`<pose>` 为单位位姿，`loop=true` 并在末尾加驻留路点；车辆用运动学模型。
  - gz 进程在 PostUpdate 把位姿镜像到 StateRing，网关、UI、录制链路不改。
- **导出器** `awr/world/export/gz.py`：
  - 视觉网格用 `appearance.glb`，roll = +π/2，并做米制缩放；
  - 碰撞用 `collision.stl`（world ENU 米制）；
  - 同时写出 `spherical_coordinates` 与 Label 插件；
  - 导出后自动验收：深度与 DSM 差值中位数 < 1 cm，actor 与磁带偏差 < 0.6 m。
- **风**：Fast 档沿用 D2 M07 的相对空速风模型，由磁带环境关键帧驱动；gz 档用 WindEffects，k 值取标定值（缺省 k = 1 会把机体吹飞）。雨与雾不交给物理后端。

### 4.5 M25 Sensor Generation Service

- **协议**：SensorBackend v2 与 v1 并列（r7 草案 `scripts/sensor_backend_v2.py`），包含：
  - `PoseWindow`：物理步之间做 lerp + slerp 插值，云台按 `T_world_base · T_base_mount · R_gimbal`；
  - `DynamicSource`：磁带读取器；Interactive 模式下改为 M18 实体表快照；
  - 逐传感器请求，以 `(uav, sensor, seq)` 为键，`seq` 同时作为计数器随机数的键；
  - 输出分为 `ObsFrame`（给 Agent，不含真值 id）与 `TruthFrame`（实例、可见度、逐点实例，只进评测库）；
  - `V1Adapter`：让任何 v2 后端装到 D2 的 v1 调用点上。
- **相机**（P0）：
  - OpenGL/EGL MRT 一遍输出 RGB8、R32F 平面深度、R16UI 实例与语义。原型为 moderngl，产品化用原生 C++ 或 Vulkan。
  - 帧契约 `drone4d.frame.v1`：FLU 机体、RDF 光学帧、COLMAP 像素中心 0.5、平面深度、时间戳取曝光中点；`agent_view` 与 `ground_truth` 分开存放。
  - 效果链依次为：畸变重映射 → 雾与雨 → 渐晕 → 曝光与运动模糊 → Poisson-Gaussian 噪声 → ISP → 编码。雨与雾的消光系数与 LiDAR 共用同一条环境时间线。
  - 可见度真值：每帧每名演员给出 `px_visible`、`px_unoccluded`、`visible_ratio`、bbox 与遮挡物类别；植被遮挡单独统计。
- **LiDAR**（P1）：
  - 采用 Warp MeshLidar（r4 `warp_lidar.py`）：MID-360 花样在 GPU 上生成；每帧 11 个位姿样本；≤ 64 个近邻实体用实例化，更多实体或骨骼动画用合并网格 refit；纹理亮度近似 905 nm 反射率并乘入射角；逻辑斯蒂检测模型；雨雾雪消光与近场假回波；计数器随机数。
  - 帧在 100 ms 网格上生成，帧末之后的那个 tick 输出。
  - 输出 `awr.sensor.lidar_frame.v2`（去掉 22,000 点上限）与隐藏的 `awr.sensor.lidar_gt.v1`。
  - 无 GPU 的部署回退到独立进程中的 Open3D。
- **IMU / GNSS**：
  - 噪声参数取自 `vehicles/p600/sensors/imu.yaml`，随机数键改为帧序号；
  - IMU 200 Hz、单位 g，起飞前留静止窗口；
  - 轨迹必须 C2 连续，并断言角速度与加速度上限。
- **SLAM 导出合同**（冻结，r1 + r4）：
  - 话题：`/livox/lidar` 为 `livox_ros_driver/CustomMsg`，10 Hz，每帧 20,000 点，无回波点为零，`offset_time = i·5000 ns`，`header.stamp = timebase = 帧首`；`/livox/imu` 为 200 Hz、单位 g；`/left_camera/image/compressed` 为 10 Hz、同一时基。
  - 附带 `rig.json`（自动生成 FAST-LIVO2 与 FAST-LIO 的 yaml）和隐藏的真值侧车：`gt_imu.tum`、`gt_cam.tum`、`gt_lidar_world.ply`、GT 深度。
  - 合同测试项：md5、点数、时间单调、IMU 单位、5 s 纯惯导回代误差 < 5 cm、C2 连续性。
- **3DGS**（P3）：采用 C3 方案，3DGS 只替换静态像素的颜色，深度、实例、语义与遮挡全部来自网格。训练视角必须覆盖 Agent 的飞行包络，`scene_scale` 按瓦片尺度设置，相邻瓦片同时驻留。
- **调度**：
  - 每块 GPU 一个 Worker，常驻 2–3 个世界（GL 路径），按 `(world, backend, 分辨率, 输出集合, 事件时间桶)` 合批；
  - 三级队列：交互预览 > 在线闭环 > 离线重渲染；在途数有上限，超限时向内核施加背压；
  - 读回与编码流水化：固定页内存异步拷贝，RGB 用 nvJPEG，深度用 f16 + zstd 或低压缩级别的 PNG16。
- **两种模式**：在线闭环只渲染 Agent 需要的通道；离线重渲染用于数据集的高分辨率、补充标签与 Blender 金标准。Agent 实际看到的帧必须原样存档，不能用重渲染替代。
- **Gazebo 原生传感器**：`GzNativeSensorBackend` 可选，按同一帧契约输出，用于交叉验证与 ROS 用户。

### 4.6 M26 Agent Observation SDK

- **接口**：`DroneAgent.reset(config)` / `step(obs, inbox) -> action`；Env 对齐 PettingZoo Parallel API，`drone4d.bench.make(world, event, fleet, obs_level, comm, mode, seed)`。
- **骨架**：照搬 D2 批处理的组装方式，即 `SimCore + LocalBus + LocalRing + BatchCtl`，动作按"固定延迟生效"规则执行。Benchmark 默认 lockstep；LLM 这类慢决策用决策时延模型，即动作在仿真时刻 t + 时延才生效，仿真不等 Agent。
- **观测分级**（与物理档正交）：
  - L0 Oracle：只用于调试与评测端；
  - L1 检测级：Fast 档，M19 匿名化或 r6 的视锥 + 网格 LOS + 像素阈值 + 透过率；
  - L2 渲染级：RGB/Depth/Seg 与 LiDAR；
  - L3：SITL/HIL。

  每个任务声明允许的最低档，排行榜按档分开；L1 的像素阈值用 L2 分割像素数标定。
- **信息权限**：Agent 只拿到以下内容：
  - 本机观测与带噪位姿（GNSS 0.5 m）；
  - 实际送达的消息；
  - 任务简报：公开锚点、目标外观描述；
  - 仿真时钟。

  `_truth`、`_vis`、`_ident` 字段在出口处剥离；M19 输出去掉真值 id 与 `n_in_fov`，航迹按机私有；LiDAR tag 不再带"命中识别物"位。另配一个泄漏红队测试：在 Agent 收到的全部字节中搜索演员 id 与真值坐标。
- **动作**：速度设定、航点、偏航速率、云台 look-at、变焦、传感器模式、消息、事件声明（`report`），一律经 CommandEngine 与 TrustedGuard，按 M21 机型限幅。
- **通信**：
  - 链路层只在 Env 中实现一份：2.4 GHz 路径损耗，NLOS 由网格射线判定后加 20 dB，丢包率是余量的逻辑斯蒂函数，另有时延、令牌桶与事件干扰区；链路状态按 1 s 缓存。
  - 会话层（可靠传输、主题）是 SDK 的可选库。
  - M14 ANet 的委派与黑板作为跑在链路层之上的协同策略。
- **私有地图**：
  - L0 轨道：已知位姿（真值或真值加 GNSS 噪声），用 GPU 稀疏 8³ 块 log-odds + TSDF，只保留活动窗口；提供 `integrate`、`frontiers(min_cluster)`（含聚类与可达性过滤）、`export_delta` 和 `merge`（log-odds 相加）；Marching Cubes 按权重掩膜出面。
  - L1 轨道：FAST-LIVO2 容器独立运行，GPL-2.0 隔离，每机一个实例，消费本机传感器流，产出估计轨迹与彩色地图；离线以 bag 为主，在线经 ROS 桥（P2 起）。
- **隔离**：进程内运行只用于开发。正式评测时每个 Agent 放在独立的无网络容器中，经本地 socket 或 `WS /api/bench/v1/runs/{rid}/agents/{aid}` 交换 obs 与 act，超时沿用上一动作。

### 4.7 M27 Benchmark Runtime

- **Run 规格**：世界 contentVersion、磁带 `content_sha256`、机队、策略镜像或 SDK 端点、物理档、观测档、噪声档（clean / realistic / harsh）、种子集合，全部写入 `run_manifest.json`。
- **作业类型**：
  - `l1_run`：CPU，数十倍实时；
  - `l2_run`：GPU 渲染与 LiDAR；
  - `slam_eval`：先生成 bag，再以 4 倍速、2 核、≤ 6 GB 跑 N 次 FAST-LIVO2，最后执行 `eval_sim`；
  - `world_build`、`event_compile`、`render_offline`（离线重渲染）。
- **录制**：D3 用独立的无损写入器，D2 的 M12 主体不改。新通道采用 Foxglove schema，便于 Rerun 与 Foxglove 双向读取；磁带与世界清单作为 MCAP 附件；隐藏真值另存文件（`truth/run_<id>.mcap`）。
- **数据集目录**：`dataset/<event>/{world_manifest.json, event/, truth/（隐藏）, runs/<run_id>/}`。可导出 LeRobot（parquet + mp4）、OpenLABEL、ROS bag（Livox 全点流）、PCD。`.rrd` 由导出器从 MCAP 派生：事件对应 dataset，Run 对应 segment，观测层与真值层分为不同 layer；`application_id` 不能含点号；Rerun 版本锁定 0.38.x。
- **评测器**：
  - EC 及其覆盖；
  - EU：P/R/F1，正式版由 Agent 自己提交声明；
  - AO_real；
  - CE：协同收益、冗余度、km 与 KB 成本、丢包。

  每个配置至少 10 个种子；评测器要去重并按时间分桶。SLAM 轨道报告 RPE100（主指标）、最大锚定漂移、对齐 ATE、地图 F@0.2（锚定与对齐两种）、颜色 MAE。所有几何指标统一用 `eval_sim`（到 GLB 网格的距离、可观测表面完整度）。
- **数据划分**：dev 集公开磁带与真值；test 集只公开世界包、简报与观测档，参赛者提交 Agent 容器，由服务端运行；事件族变体（Scenic 式分布）中留出一部分只在 test 中出现。

### 4.8 浏览器前端

| 档 | 世界外观 | 来源 |
|---|---|---|
| Tier S（SwiftShader，公开站兜底） | 网格采样的彩色点云，经现有 ANET_Q16 管线显示；M05/M06 零改动，RawCloud 需增加 rgb 流 | r2、r7 |
| Tier B/A（硬件 GPU） | 3D Tiles 网格瓦片（meshopt + KTX2，按瓦片重排图集，纹理 ≤ 4096）；新图层需先通过 `m06.gpu-limits` 检查 | r2 |
| 演员 | ≤ 32 名脚本演员复用 TargetLite32 与识别物图层；背景人群用新的实例化图层与 `actor` 实体种类 | r7 |
| 无人机视角面板 | 服务端渲染的帧流（实时预览低分辨率；回放用预渲染视频） | r3、r7 |
| 事件编辑器 | 在浏览器加载同一份 Detour 二进制，交互式拖放锚点、预览路径，实时预检并给出建议时刻 | r2、r6 |
| 多 Run 对比与数据检视 | three.js 时间轴；可选嵌入 Rerun Web Viewer 并做双向时间联动 | r8 |

### 4.9 部署分工：公开站 emax 与 dmax GPU Worker

| 位置 | 硬件 | 运行 | 不运行 |
|---|---|---|---|
| emax（`https://drone4d.agentnetwork.org.cn`） | 4 核、约 2.6–2.8 GB 可用内存、无 GPU，与 ANet 生产服务共用 | D2 全部功能（S7 共享展示、CPU 沙盒，容量模型不变）。D3 只读内容均为静态文件：GLB 世界的彩色点云或 3D Tiles、事件磁带回放（插值为微秒级）、Run 与数据集浏览（预导出的观测层片段与视频）、预渲染的无人机视角回放 | BVH、渲染、MeshLidar、Gazebo、FAST-LIVO2、批量评测、世界构建；公开站组合根不加载 D3 插件（`AWR_D3_ENABLED = 0`） |
| dmax GPU Worker（当前只允许用 GPU 2、3） | A40 48 GB | M25 Sensor Service（每 GPU 一个 Worker）、MeshLidar、gz-sim 实例（每实例约 3 核、2 GB 显存）、3DGS 训练（P3） | 不承接公网匿名流量 |
| dmax CPU 池（每容器 ≤ 16 核，必须设 `--memory`） | 64 核、1 TB | FAST-LIVO2（每实例约 2 核、≤ 6 GB）、评测（约 1 分钟/次）、L1 批量 Env、世界构建（3–6 分钟/世界）、NavMesh 与磁带编译 | — |
| dmax 存储 | `/data`，49 TB 空闲 | 世界包、事件包、runs、数据集（L2 3 机 360 s 约 5–7 GB；100 次运行约 0.5–0.7 TB） | — |

- **数据流向**：dmax 产出的制品（世界包的浏览器层、事件包、观测层数据集、预渲染视频）单向同步到公开站的静态目录或对象存储。test 集真值不出 dmax。
- **实验 API**：`/api/bench/v1`（事件编译、Run 提交、Agent 连接）部署在 dmax 侧，经鉴权与配额开放，是否对公众开放待用户决定（§6）。
- **隔离与命名**：每个容器带 `--memory` 与 GPU 绑定（Ogre-Next 固定使用 0 号 EGL 设备）；gz 实例使用独立的 `GZ_PARTITION`；容器命名延续 `d3-<单元>` 规范。

### 4.10 与 D2 的接合（保证 D2 不受影响）

1. **发布次序**：D2（含 D2.1）完成验收后打标签，发布到 GitHub `main` 与公开站（用户要求，正在另一工作流中进行）。D3 自该标签拉 `d3-dev` 分支；D2 的后续修复只在 `d2` 维护分支修，再合入 `d3-dev`。
2. **只新增包**：`awr.world.glb`、`awr.world.mesh`、`awr.sim.events`、`awr.sensorsvc`、`awr.gz`、`awr.agentsdk`、`awr.bench`；前端新增 `engine/actors`、`viewport/layers/worldMesh.tsx`、`ui/{event,bench,droneview}`。用 import 方向测试保证 D2 包不 import D3 包。
3. **14 处扩展点小改**（r7 §7.3，P0 占 12 处），集中在一个工作包里先做，并且先过回归门。涉及：
   - `budgets.py`；
   - M18 的 `model`、`motion`、`runtime`；
   - target 与 rng 契约；
   - `sensors/models.py`（Raycast 字面量）；
   - RawCloud.rgb；
   - `open_world_query` 分派；
   - world schema 图层枚举；
   - snapshot 可选段；
   - topics 与 mcap_channels；
   - replay 的磁带演员源；
   - API 开关；
   - P1 才改的实体种类 `actor` 与前端 LayerId。
4. **回归门**（每个 D3 工作包合入前）：
   - D2 全部测试通过；
   - 在安装了 D3 包、开关关闭以及"开关打开但不加载事件"两种条件下，批处理事件流、resim 帧摘要、M18 确定性用例逐字节一致；
   - 性能子集与基线相差 ≤ 5%；
   - `make lint-m06` 与 `m06.gpu-limits` 通过。
5. **契约与文档**：
   - 契约从 1.2.0 升到 1.3.0，只增不改；新增 `event/`、`tape`、`bench/`、`sensor/v2`、`lidar_frame.v2`、`lidar_gt.v1`、`frame.v1`、`render_req.v1`、`caps/gz.json`、`caps/px4_gz.json`，以及 rng 流 19–21 与 MCAP 新通道。
   - 新增 `docs/05-D3-设计增补与决策记录.md`（ADR 从 127 起编号）与 `docs/modules/M22`–`M27` 的 PRD；D2 文档只在"对既有模块的影响"表中加行。

---

## 5. 分阶段计划与退出标准

工作量引自 r7 §6（有经验的工程师，含单测与文档，±40%）。r1、r3、r4、r6、r10 已有原型的条目实际工作量可能再低 20–30%。

### P-1 前置（约 1–2 周，与 D2 收尾并行）

| 事项 | 产出 |
|---|---|
| D2 完成验收、打标签、发布（另一工作流） | D2 标签；GitHub `main`；公开站更新 |
| 拉 `d3-dev`；实施扩展点工作包（14 处中的 12 处）并通过回归门 | 回归门用例与对拍基线 |
| 统一尺度复测：按 r2 的尺度重跑 r3 的纹素与可见度、r4 的航高窗口与 DSM 偏差，以及 FAST-LIVO2 在正确尺度下的 CI 基线 bag | 正确尺度下的基线数字（替换 §1.2 中标为"需复测"的项） |
| ADR 与 PRD：`docs/05`、M22–M27 PRD；契约 1.3.0 草案 | 文档与契约草案 |

### P0：米制 GLB 世界 + 固定事件 + 多机 RGB/深度/分割（44.5 人周；4 人约 11–12 周）

**范围**：
- M22：crop 世界包，在 dmax 上构建；
- M23：DSL、三道门、运动学磁带、语义验证、事件图；
- M18：timeline 运动源与 exogenous；
- M25：SensorBackend v2、EGL 相机、可见度真值、GPU Worker 调度；冻结 LiDAR/IMU 输出合同，但不交付 MeshLidar；
- M26：L1/L2 Env、观测过滤、动作映射、SDK 与基线策略；
- M27：Run 规格、队列、无损写入、EC 评测、数据集导出、回放；
- 前端：彩色点云世界、演员图层、无人机视角面板。

**退出标准**（全部可测）：
1. crop 世界包构建可重复（同一输入得到相同 contentVersion），`scaleStatus = landmark`，V-GLB 校验通过；D2 运行时不改代码即可加载它的 DSM、DTM 与点云。
2. 示例事件为 3 名脚本演员 + 1 辆车 + ≥ 40 名背景行人，360 s，含风雨与通信干扰区，三道门编译通过。故意写错的声明使 strict 编译失败。磁带 `content_sha256` 在两台机器上相同；背景 0 / 40 / 200 人时脚本演员列的哈希相同。
3. 在同一磁带上，≥ 3 种机队配置（1 / 3 / 5 架；悬停、扫描、跟踪）产出视角各不相同的 RGB、深度、实例与语义帧。帧时间戳取曝光中点，位姿经插值；同一时刻帧中的演员位置与磁带一致；分两遍合成与联合 z-buffer 的实例 IoU 均值 ≥ 0.999。
4. CPU 档同配置两次运行，观测逐字节一致；GPU 档在同镜像、同型号 GPU 上逐字节一致，跨环境按容差判定（深度 p90 ≤ 1 mm，像素偏移 ≤ 0.1 px）。
5. 泄漏红队测试为 0：Agent 收到的全部字节中不含演员 id 与真值坐标。
6. EC 评测、数据集导出（event / truth / runs 三层 + run_manifest）与浏览器回放可用；6 种参考策略的指标与 r6 同一量级。
7. 性能：3 机 × 10 Hz × 720p 全输出的 L2 运行在单张 A40 上不慢于实时；L1 运行 ≥ 40 倍实时。
8. D2 回归门全绿。

### P1：网格 LiDAR + SLAM 轨道 + 私有地图 + 通信 + 人群 + 编辑器（14.5 人周）

**范围**：
- MeshLidar：静态网格、逐点位姿、纹理反射率、实体实例化与 refit、天气；`lidar_frame.v2` 与真值侧车；
- IMU/GNSS 按帧序号取随机数；Livox bag、MCAP 与 PCD 导出；`slam_eval` 作业；
- 私有地图（稀疏块）与 Frontier 聚类；ANet 增量共享；
- 链路模型，含 1 s 缓存与空间分块；
- 背景人群的 `actor` 实体种类（容量 > 32）；
- 事件编辑器；OSM 车道图；EU、AO_real、CE 指标；骨骼动画相位。

**退出标准**：
1. LiDAR/IMU 合同测试全部通过：md5、整帧 20,000 点、`offset_time` 单调、零点占位、IMU 用 g 且有静止窗口、C2 连续、5 s 纯惯导回代误差 < 5 cm。
2. FAST-LIVO2 回归（正确尺度的 town 环线）：clean 档 RPE100 ≤ 0.1%、ATE ≤ 0.2 m；realistic 档给出 5 个种子的分布；"逐点位姿优于单一位姿"可复现。
3. 3 机私有地图两两 Jaccard ≤ 0.3，共享后的合并覆盖率 ≥ 最佳单机的 1.5 倍；私有地图存储 ≤ 50 MB/机·分钟。
4. 60 架 L1 运行 360 s 单核 ≤ 2 分钟；通信干扰区对丢包与 EC 的影响可测。
5. 编辑器能显示 NavMesh 与锚点，拖动锚点时实时给出预检结果与建议时刻。

### P2：Gazebo Realistic 档 + Interactive 模式 + 网格图层 + PX4 SITL（12.0 人周）

**范围**：
- 世界包 → SDF 导出器与自动验收；
- C++ 版 `Drone4DClock`（合并单条控制消息协议）、TapeSystem、PoseTap、Command System、`Drone4DVelocityControl`；
- `GzSimBackend` 与 `caps/gz.json`；M25 接入 gz 位姿源；
- Interactive 模式（反应沿 NavMesh、实现磁带）；
- 浏览器 Tier B/A 网格瓦片图层；PX4 SITL 档；FAST-LIVO2 在线 ROS 桥（可选）。

**退出标准**：
1. 同一磁带与 Run 规格在 Fast 档与 Realistic 档都能运行；给定相同位姿时 M25 的输出逐字节相同（差异只来自动力学）；轨迹差异报告带容差。
2. gz 导出自动验收：深度与 DSM 差值中位数 < 1 cm，actor 与磁带偏差 < 0.6 m；同镜像、同 GPU 逐位确定。
3. 独占 A40 时，3 架 RGB + LiDAR 锁步 RTF ≥ 0.9。
4. Interactive 运行可经 inputlog 逐位重放，并输出事件偏离度。
5. 同一运行中 L1 与 L2 的识别判定一致率 ≥ 90%。
6. 网格瓦片图层通过 `m06.gpu-limits`。

### P3：3DGS 外观层 + 批量 Benchmark + 公开测试集（7.5 人周 + 资产获取）

**范围**：
- 3DGS 混合着色（C3），前提是获得真实航拍重建资产；
- 事件族生成（Scenic 式分布）；test 集容器提交与排行榜（按观测档分组）；
- LeRobot、OpenLABEL、`.rrd` 导出；Rerun catalog 浏览。

**退出标准**：
1. 无人值守地完成 ≥ 100 次运行，自动评分并导出标准化数据集。
2. 3DGS 混合：实例掩膜与网格真值的 IoU ≥ 0.999；训练视角覆盖飞行包络时，低空 PSNR 相比同分布视角的下降 ≤ 3 dB。
3. 提交的容器在无网络沙箱中运行，结果出现在排行榜上。
4. 数据集发布带 CC-BY-4.0 与 ODbL 署名，test 集真值不外泄。

**工作量汇总**：P0 44.5、P1 14.5、P2 12.0、P3 7.5，合计约 78.5 人周（±40%）。P0 的关键路径为 W1 → W4 → E1 → E2 → E3 → A1 → B3，约 12 人周串行；渲染、前端、契约三条支线可以并行；4 人约 11–12 周，6 人约 8 周。

---

## 6. 待用户决策的问题

| # | 问题 | 选项 | 建议 | 影响 |
|---|---|---|---|---|
| 1 | 现有 GLB 不是 3DGS。"真实的 3DGS 渲染"诉求如何落地 | A. P0–P2 用网格渲染，P3 另行获取真实航拍重建资产（例如用 P600 或其他机型对选定街区自采航拍，或采购、合作获得 3DGS 城市数据）；B. 直接用网格训练 3DGS（实测不增加真实感） | A | 决定 P3 的范围与资产成本；B 只适合用于 Web 展示 |
| 2 | 默认沙盘分工 | crop 作为事件主场景，town 用于大范围 LiDAR 探索，hill 用于近距离与 Re-ID | 采纳 | 关系到 P0 世界包与示例事件放在哪里 |
| 3 | 山地协同探索场景（design-r1 中的 Langtang 示例） | 另行获取山谷级 GLB 或 DEM 资产；或先用 town 做大范围探索 | 先用 town，P2 前确定资产 | 关系到第二个演示场景 |
| 4 | Realistic 档的传感器来源 | 共用 M25（推荐）；或 gz 原生传感器 | 共用 M25，gz 原生只作交叉验证 | 关系到吞吐、MID-360 保真度与对照实验的可归因性 |
| 5 | LiDAR 是否提前到 P0 | 保持在 P1；或在 P0 交付静态网格 MeshLidar（原型已成熟） | 若用户最看重 LiDAR 重建，可提前，P0 约增加 2–3 人周 | 关系到 P0 的工期 |
| 6 | 首个 Benchmark 的观测档 | 只开 L1；L1 + L2 | L1 + L2，排行榜分开 | 关系到 GPU 预算与评测口径 |
| 7 | FAST-LIVO2 的 GPL-2.0 | 只作为独立容器（不入库、不链接）；是否对外发布该镜像，以及发布其产出的数据集产品 | 容器隔离；镜像只在内部使用；发布数据时注明生成工具 | 涉及许可合规 |
| 8 | OSM 派生的 anchors 与道路图属于 ODbL | 接受署名与共享协议；或只在内部使用 | 接受，并单独标注许可 | 关系到世界包的发布方式 |
| 9 | 公开站上 D3 的开放范围 | 只读（回放、数据集浏览）；或开放带配额的 Run 提交（任务在 dmax 上执行） | P0–P1 只读；P3 开放容器提交 | 涉及安全、配额，以及 dmax 共享机的压力 |
| 10 | dmax 的 GPU 配额 | 维持只用 GPU 2、3 的共享方式；或为 D3 Worker 独占 1–2 张卡 | 生产期独占至少 1 张 | 争用下吞吐波动可达 3 倍，可能导致性能类退出标准无法验收 |
| 11 | 测试集与提交模式 | 公开全部真值；或采用隐藏 test 集 + 容器提交 | 隐藏 test 集 + 容器提交 | 关系到排行榜的可信度 |
| 12 | 是否在产品网页中嵌入 Rerun Web Viewer | 保持 D1 NG-09，不嵌入；或嵌入数据检视面板 | 嵌入数据检视面板，主沙盘仍用 three.js | 涉及 PRD 的修订 |
| 13 | Gazebo 版本 | Harmonic（LTS 至 2028）；Jetty（新增批量射线与 CPU LiDAR） | 原型用 Harmonic，P2 前评估 Jetty | 关系到插件 ABI 与维护成本 |
| 14 | 动态实体资产 | Khronos 样例（CesiumMan 等，CC-BY）；或采购带骨骼动画的人物、车辆资产库 | P0 用样例，P1 起采购或制作 | 关系到 Re-ID 与动作识别类事件能否被视觉识别 |
| 15 | 人力与工期 | 4 人约 11–12 周完成 P0；6 人约 8 周 | 视资源而定 | 关系到整体排期 |
| 16 | 高空观察任务的传感器 | 只给相机；或增加 Avia/Mid-70 类长量程雷达模型 | 增加一个长量程模型（真机 HKisland03 数据可做回归） | 关系到机型目录 M21 的扩展 |

---

## 7. 风险

| # | 风险 | 影响 | 缓解 | 来源 |
|---|---|---|---|---|
| 1 | GLB 尺度与配准错误，或各单元尺度不一致 | 速度、像素、量程、通信距离全部失真；已有单元的米制数字偏差 2–8 倍 | M22 强制标定并留存证据，未标定的世界禁止加载事件；按 §1.2 复测 | r2、r3、r4、r6 |
| 2 | 摄影测量网格的缺陷：实心树冠、屋顶与水面可走、建筑内部与背面缺失、洞、拉伸面 | 可见度判定偏保守；NavMesh 出现孤岛或错误通道；LiDAR 无法穿透植被 | quality 层与"不完整即未知"规则；植被语义与遮挡分开统计；进入建筑只做状态切换；编辑器人工修正 | r2、r3、r4、r6 |
| 3 | 光照烘焙在纹理里，阴影固定 | 无法模拟昼夜变化；雨天只能整体变暗；演员光照与背景不一致 | 估计太阳方位，演员按同一方向着色；作为已知限制写入文档 | r2、r3 |
| 4 | 城镇级网格粗（边长 1.9–3.3 m，纹素 19–28 cm） | 常规航高下图像背景比真实航拍模糊，检测器可能学到伪特征 | 发布纹理受限距离（`sensorHints`）；近距离任务用 hill 或真实航拍资产 | r2、r3 |
| 5 | 模拟器与算法之间的隐含假设（无回波点、点序、IMU 单位、静止初始化、时基、卷帘快门） | SLAM 结果"好得不真实"或"差得不真实"，差距可达一个数量级 | 合同测试；用真机数据（HKisland03、自采 P600）定期回归 | r1、r4 |
| 6 | 真值通过多条通道泄漏给 Agent | Benchmark 结果失真 | 唯一观测出口；泄漏红队测试；正式评测在服务端运行提交的容器 | r7、r6 |
| 7 | D2 尚未通过验收，D3 的起点可能移动 | 扩展点小改与 D2 修复冲突 | 等 D2 打标签后再动扩展点；D2 修复只在维护分支进行 | r7 |
| 8 | sim-core 预算已紧（D2 的感知与识别物 stage 曾超预算 4–20 倍） | D3 若把负载加进 sim-core 会挤占 0.40 核的总预算 | 重计算全部放在外部 GPU 服务；sim-core 内只做磁带插值与状态发布（每 tick 微秒级） | r7 |
| 9 | 共享机 dmax 的 GPU 争用 | 计时波动最高可达 3 倍；可能影响其他服务 | 生产 Worker 独占 GPU；基准测试记录同卡其他负载；所有容器加 `--memory`（曾出现 TSDF 吃到约 400 GB 内存） | r1、r3、r4、r5、r9 |
| 10 | 跨硬件不可逐位复现 | 复现争议 | 三级标准：磁带与 CPU 档逐字节；同镜像、同型号 GPU 逐字节；跨环境按容差（距离 ≤ 1 mm，命中不一致 ≤ 1e-4，深度与像素容差见 §5 P0 第 4 条）；运行清单记录 GPU 型号、驱动、镜像摘要与种子 | r2、r4、r9 |
| 11 | 许可 | nvdiffrast 只限研究；FAST-LIVO2 为 GPL-2.0；GLB 为 CC-BY-4.0；OSM 派生数据为 ODbL | 产品走 EGL/Vulkan；FAST-LIVO2 只作容器；NOTICE 与 UI 署名；ODbL 层单独标注 | r3、r10、r1、r2 |
| 12 | 3DGS 的局限 | 视角外失真、瓦片空洞；用期望深度判遮挡会让演员"穿墙" | 只用于真实航拍资产；一律采用 C3；训练视角覆盖飞行包络 | r3、r10 |
| 13 | 数据量与 CPU 编码 | 100 架 5 分钟 LiDAR 约 57 GB；1080p 多机时 CPU 编码先成为瓶颈 | MCAP zstd 分块、nvJPEG、深度 f16 + zstd、按任务降采样 | r3、r4、r8 |
| 14 | 机队规模带来平方级复杂度 | 60 机时 133 万次投递；朴素评测器在 30 机时超过 25 分钟 | 链路缓存与空间分块；评测器去重并按时间分桶 | r6 |
| 15 | 指标被刷分 | 例如长期悬停在简报给出的锚点上，或利用只看机位的 AO | 严格控制简报内容；事件族变体与隐藏 test 集；报告 AO_real | r6 |
| 16 | 工具链停滞 | ROS1 Noetic 已 EOL；Sophus 需补丁；Harmonic 2028 年停止维护；Python gz 绑定有 GIL 缺陷 | 镜像归档；核心链路用 C++；规划 Jetty 迁移；ROS2 社区移植版须经本闭环回归后才可替换 | r1、r9 |
| 17 | 骨骼动画尚未实现 | 动作类事件在视觉上不可识别 | 磁带输出动画相位，渲染端做 LBS；采购带动画的资产 | r3、r5 |
| 18 | 工作量估计 ±40% | 计划偏差 | 先做已有原型的条目；按 P0 关键路径排期 | r7 |
| 19 | 外部数据源与网络不稳定 | 世界构建或依赖下载失败，不可复现 | 离线 OSM 抽取并记录 sha256；并行 Range 下载；镜像与数据在 dmax 留副本 | r1、r2、r5 |
| 20 | r8 单元仍在进行中 | Rerun 实测章节尚未完整 | 本文只引用其 §0；完成后补入 §2.8 | r8 |

---

## 8. 可复用实现索引

| 去向（模块） | 原型（`.cache/research/d3/…`） | 说明 |
|---|---|---|
| M22 世界导入 | `r2-glb-world/scripts/`：`glbworld.py`（零依赖 GLB 解析）、`worlds_cfg.py`（调平、共帧变换）、`e03_*`（OSM FFT 配准）、`e05_pipeline.py`（焊接、BVH、DSM）、`e06_decimate.py`、`e07_voxel.py`、`navmesh_bake.mjs` / `nav_prune_query.mjs`、`e12_anchors.py`、`e09_incomplete.py`、`e14_mesh2points.py`；`r6-event-benchmark/node/build_navmesh.mjs` | 按 r2 §10 的去向迁入 `awr/world/glb`、`awr/world/mesh` |
| M23 事件运行时 | `r6-event-benchmark/`：`schema/event.v1.schema.json`、`events/*.yaml`、`scripts/event_compile.py`、`node/tape_gen.mjs`、`scripts/tape.py`、`scripts/semantics.py`；`r7-d2-audit/scripts/e3_tape_interp.py` | 编译器、磁带生成器、验证器、播放器 |
| M18 扩展 | `r7-d2-audit/scripts/e2_exogenous.py` | 验证 timeline 与 exogenous 两个开关 |
| M25 相机 | `r3-camera-render/`：`common.py`（GLB 解析、坐标与投影约定）、`exp3_gl_bench.py`（moderngl MRT）、`nvr.py`（nvdiffrast 研究版）、`fx.py`（雾、雨、阴影、噪声）、`exp6_composite.py`（C1/C2/C3）；`r10-rotorpy/scripts/d4cam.py`（相机模型、触发时钟、位姿插值、效果链、宿主适配） | 产品化用 EGL；nvdiffrast 只用于对拍 |
| M25 LiDAR | `r4-mesh-lidar/scripts/`：`warp_lidar.py`、`bench_backends.py`、`make_bag.py`（ROS1 字节拼装）、`recon.py`（私有地图）；`r7-d2-audit/scripts/sensor_backend_v2.py`、`e5_mesh_lidar_backend.py` | `MeshLidarBackend` 与 SensorBackend v2 |
| M25/M27 SLAM | `r1-fast-livo2/scripts/`：`sim_livo_bag.py`、`eval_sim.py`、`check_imu_consistency.py`、`diag_traj.py`、`run_livo_bag.sh`、`run_sim_livo.sh`、`bag_retype.py`、`par_range_dl.sh`；`docker/livo/`（Sophus 补丁、无 rviz 的 launch） | `slam_eval` 作业、合同测试、真机回归 |
| M24 Gazebo | `r9-gz-sim/plugin/src/Drone4DClock.cc`、`Drone4DVelocityControl.cc`、`scripts/gen_world.py`、`d4d_lockstep.py`；`r5-gazebo/scripts/`（SDF 导出、TapeSystem 的 Python 原型、PX4 启动器）、`docker/Dockerfile` | C++ 锁步与磁带驱动 |
| M26 Env 与 SDK | `r6-event-benchmark/scripts/bench.py`（Env、`DroneAgent`、链路模型、4 类参考策略）、`sweep.sh` | 进程内原型；正式版需加容器隔离 |
| M27 评测与数据集 | `r6-event-benchmark/scripts/bench.py` 中的 `evaluate()`、`export_dataset.py`、`aggregate.py`；`r1-fast-livo2/scripts/eval_sim.py`；`r8-rerun/scripts/d3rr.py`、`d3scene.py`、`mcap_interop.py`、`query_bench.py` | EC/EU/AO/CE 指标与 SLAM 指标；`.rrd` 导出 |
| 镜像（dmax） | `d3-r1-fast-livo2:noetic`、`d3-r1-simgen:py311`、`d3-r2-glb-world:0.1`、`d3-r3-camera-render:v1`、`d3-r4-lidar:0.3`、`d3-r5-gazebo:harmonic`、`d3-r6-event-benchmark:py311`、`d3-r9-gz-sim:dev`、`d3-r10-rotorpy:0.1`、`px4io/px4-sitl-gazebo:latest`（经 `docker.1ms.run`） | 复现用；进入生产前改为项目自有的构建流水线 |
| 回归数据（dmax） | `r1-fast-livo2/datasets/HKisland_GNSS03.bag`（10 GB）；`r4-mesh-lidar/out/bag/sim_*_z.bag` | sim2real 与 CI 基线（r4 的 bag 尺度偏小，需按 §1.2 重生成） |
| M22/M25 高斯外观层（2026-10-12 补入） | `r11-skyfall-gs/scripts/`：`sky_world.py`（crop 米制变换、按 AGL 与俯角取景、掩膜、面元采样）、`sky_gs.py`（gsplat 训练、网格深度一致性、指标、PLY 导出、帧率）、`s2_train_base.py`（网格蒸馏基线）、`s4_eval.py`、`s5_gate.py`（留出评估与决策门指标）；`s3_idu.py`、`refine_cn.py`、`refine_flowedit.py`（生成式细化，只作研究工具）；`docker/Dockerfile`（`d3-sky:v1`） | M22 `appearance.gs` 构建阶段与 M25 `gs_eval` 的参照实现；生成式细化与扩散模型不进入产品镜像（AWR-05 ADR-187） |
| M22/M15/M27 高斯资产工具链（2026-10-12 补入，r12） | `r12-supersplat/scripts/`：`analyze_base.py`、`st_bench.sh`（体检、清理、格式、坐标变换、LOD）、`precision.py`（编码精度与坐标回读）、`gen_settings.py`（Viewer 设置文件与动画轨）、`bshot.mjs`（WebGL2 上限模拟、截图、离屏截帧、基准）、`editor_setup.js`、`editor_video.js`（编辑器无头自动化与视频导出）、`dmax_prep.sh`、`st_render.sh`、`eval_psnr.py` | AWR-05 §6.5 的高斯发布流水线、内部编辑工具与检视视图的参照实现与升级回归脚本（ADR-192） |

---

## 9. 附记：用户决定的落点（2026-10-12）

本节只登记用户对 §6 问题的答复与 2026-10-12 交互式确认的设计决定在设计基线中的落点，不修改 §0 至 §8 的研究结论与实测数字。设计基线以 [AWR-05](../../05-D3-设计增补与决策记录.md) 为准（v1.3：§3.3、§3.5、§19；v1.4 并入 r11：§6.4、ADR-187、§20；v1.4 第二次交互式讨论与 r12：§3.6、§6.4.7 至 §6.5、ADR-188 至 ADR-192、§21）。

| 用户决定 | 与本研究建议的关系 | 基线落点 |
|---|---|---|
| 交付重心为基准排行优先（原为数据集工厂优先） | §5 的阶段计划以数据集与观测能力为主线，排行榜在 P3；改为首个里程碑即有评测与内部排行榜原型 | AWR-05 ADR-174、ADR-175、§10 |
| gz-sim 提前为 P0 缺省后端，正式成绩只在 gz-sim 锁步档产生，FleetSim 为快速档 | §0 第 5 条与 §4.4 以 Fast 为主力、Realistic 在 P2；锁步与确定性结论（§2.5，r9）不变，用途与阶段改变 | AWR-05 ADR-176 至 ADR-178 |
| 相机第一阶段增加卷帘快门、热成像（实体温度与发射率）、1080p 多机输出 | §2.2 的 1080p 数字与"CPU 编码先成瓶颈"的结论直接用于编码约束；热成像为新增能力，本研究未实测，参数为暂定值 | AWR-05 ADR-179、ADR-180、§5.4.10 |
| 第一批事件四类：Re-ID、多人交汇与交接、车辆追踪、未知位置目标搜索 | r6 原型事件（三人一车）作为首个固定事件；其余三类为新增范围 | AWR-05 ADR-181、§5.2.8 |
| 主得分由事件捕获率、事件理解、主动观测效率、协同效率共同构成 | §0 第 8 条的四类指标保留，并合成为版本化主得分；第二次交互式讨论改为暂不确定合成权重，按赛道分别报告四项指标（见 §11） | AWR-05 ADR-182、ADR-191、§5.6.9 |
| 仅内部排行与提交，公开站只读展示样例 | 取代 §6 第 9 题"P3 开放带配额的提交" | AWR-05 ADR-183、ADR-184 |
| 智能体：Python 研究算法优先，大模型指挥层与 ANet 委派在 P1 | 与 §4.6 的 SDK 设计一致 | AWR-05 ADR-185 |
| 首个里程碑为最小基准闭环，首个场景为 crop | 与 §0 第 1 条的原型闭环一致，增加 gz-sim 锁步与内部排行榜 | AWR-05 ADR-175 |
| 高斯外观层：尽量使用 3DGS，参照 Skyfall-GS 研究"网格 → 3DGS → 扩散逐级细化"，评估后决定是否从 P3 提前到 P1 | §0 第 2 条与 §2.4"从网格训练的 3DGS 上限即网格"的结论只针对无扩散细化的训练；扩散细化路线需要新的实验证据。评估由并行进行的 Skyfall-GS 实验提供，实验笔记入库到本目录后由 AWR-05 ADR-186 引用；评估协议与决策门见 AWR-05 §6.4。2026-10-12 补记：实验笔记已入库为 [r11](r11-skyfall-gs.md)，扩散细化未带来低空观感增益，裁决建议为结论 C（维持 P3），见 §10 | AWR-05 ADR-186、ADR-187、§6.4 |

---

## 10. 附记：r11 生成式高斯外观层实验的结论与落点（2026-10-12）

r11 在本索引首版（r1–r10）之后补入。本节摘录其结论与对 §0、§2.4 的补充，不修改 §0 至 §8 的原文；数字以 [r11-skyfall-gs.md](r11-skyfall-gs.md) 为准，统一尺度口径沿用 §1.2（crop 20.935 m/单位，r2 的 `T_world_source`）。

### 10.1 关键实测（crop 150 m × 150 m 街区，留出视角中空 AGL 64–115 m / 低空 11–29 m）

| 项 | 数值 |
|---|---|
| 网格直接训练 3DGS（网格蒸馏，产物 A） | 4M 高斯，30k 步 17.2 min、峰值 4.7 GB，SH0 PLY 259 MB；对网格 PSNR 33.0 / 33.1 dB；期望深度残差中位数 0.20 / 0.25 m；gsplat 单视角 720p 291、1080p 225 帧/s；低空 Tenengrad 为网格的约 72%，29/29 个低空视角的锐度与 MUSIQ 低于网格 |
| 课程式扩散细化，不加约束（b1） | 一致性逐级降至 21.6 / 19.9 dB；期望深度残差中位数 3.66 / 3.38 m；低空 57% 像素出现前景浮点 |
| 频带分离加几何锁定（b4） | 32.1 / 32.3 dB；残差中位数 0.18 / 0.22 m；720p 366 帧/s；低空 MUSIQ 31.8（产物 A 33.6），只有 5/29 个低空视角高于产物 A |
| 细化器 | SDXL + ControlNet-Union 4.6–8.1 s/张、14.2 GB；强度 ≤ 0.55 几乎不增细节，≥ 0.70 出现照片级纹理与语义幻觉；FlowEdit + FLUX.1 [schnell] 15.6 s/张、几乎不改变输入 |
| 机理 | 同一视角两个扩散样本之间只有 19–21 dB；高斯最大轴尺度中位数 10.6 cm，18 m 级像素足迹约 3–4 cm |
| 成本 | 单 150 m 瓦片约 1.2–1.4 GPU·h（A40）；600 m 舞台约 20 个瓦片，约 24–28 GPU·h |
| 许可 | Skyfall-GS 核心训练代码与光栅化器按 Inria 非商业许可对待，只复用 MIT 的 FlowEdit；FLUX.1 [dev] 许可禁止生产用途与监视相关研发，未运行 |

### 10.2 对 §0 第 2 条与 §2.4 的补充

1. "用网格渲染图训练出的 3DGS 上限就是网格本身"在正确米制尺度下复现：网格蒸馏 3DGS 与网格的一致性约 33 dB，低空清晰度低于网格。
2. 以图像扩散模型逐级细化并回训，没有把低空观感提高到网格蒸馏基线以上：逐视角生成的细节不具三维一致性，被 3DGS 平均掉，或在不锁定几何时转为前景浮点；网格的粗几何（多面体树团、平面化立面）还会诱发语义幻觉。
3. "只有对真实航拍重建的资产，3DGS 才带来额外的真实感"这一判断维持不变；r11 建议把 P600 小范围实拍参照集提前到 P1，使任何外观方案的"更真实"可以用 KID 度量。

### 10.3 设计基线落点

| 结论 | 落点 |
|---|---|
| 决策门首轮裁决为结论 C：生成式高斯外观不提前到 P1，维持 P3 并保留为研究工具 | AWR-05 §6.4.1、ADR-187；用户已确认为结论 C0（ADR-188，Q-D3-14 关闭） |
| 方案 C1：P1 以网格蒸馏 3DGS 先行验证外观层基础设施，并采集真实航拍参照集（+2.0 人周） | 未采纳（AWR-05 ADR-188，Q-D3-18 关闭）；网格蒸馏验证并入 P3，参照集改用公开数据集（ADR-189） |
| 外观来源分档 `aerial`、`mesh_distilled`、`generative`；C3-G 合成；出处清单 | AWR-05 §5.4.4；M22 v1.2 §15；M25 v1.2 §16；M27 §14.9；17 §18.23.7 |
| 门限修订 G-3DGS v2 与再评估触发条件 | AWR-05 §6.4.4、§6.4.5 |
| 扩散模型白名单与 Skyfall-GS 组件许可 | AWR-05 §14 |

---

## 11. 附记：r12 SuperSplat 研究与第二次用户决定的落点（2026-10-12）

r12 与用户第二次交互式讨论的决定在本索引首版（r1–r10）与 r11 之后补入。本节摘录 r12 的结论并登记第二次决定的落点，不修改 §0 至 §8 的原文；数字以 [r12-supersplat.md](r12-supersplat.md) 为准。

### 11.1 r12 关键实测（资产为 r11 的网格蒸馏 3DGS 与 2 × 2 至 4 × 4 平铺）

| 项 | 数值 |
|---|---|
| 资产体检与清理 | r11 的 4M 高斯中 58% 不透明度低于 1/255、36% 位于街区盒体外（最远 508 km）；盒体裁剪加不透明度 ≥ 0.004 后剩 1.116M（−72%），对"只裁剪"版本的渲染 PSNR 61–85 dB |
| 格式与精度（1.116M 高斯，SH0） | 浮点 PLY 62.5 MB；SOG 14.1 MB（位置误差 p99 4.1–9.7 mm）；压缩 PLY 18.2 MB（p99 13 mm）；SPZ v4 19.7 MB（p99 0.19 mm） |
| 坐标约定 | splat-transform 与 PlayCanvas 把 PLY 存储视为绕 Z 轴 180° 的空间；世界 ENU 到网页 y 向上坐标的变换经回读校验残差为 0 |
| 浏览器渲染（A40，1080p，WebGL2） | Viewer：17.85M 单文件 GPU 8.5 ms、CPU 排序 474 ms；流式 LOD 预算 4M 时 GPU 2.5 ms、排序 95 ms；Spark 2.2：17.85M GPU 15.6 ms |
| 编辑器 | v3 为 WebGPU 专用（只有 WebGL2 时启动失败）；v2（WebGL2）在 A40 上可编辑 1,785 万个高斯，全场景盒体求交 28–81 ms |
| 无头 GPU 浏览器 | dmax 容器内 WebGPU core 不可用，WebGL2 可走 ANGLE GL-EGL 或 Vulkan；WebGPU 路径须在带显示的工作站验收 |
| splat-transform 光栅化器 | 1.12M 高斯 720p 17 ms/帧，17.85M 112 ms/帧；gsplat 4M 720p 2.1–3.4 ms |
| 许可 | 四个仓库均为 MIT；依赖 mediabunny 为 MPL-2.0（文件级） |

### 11.2 第二次用户决定与 r11、r12 结论的设计基线落点

| 用户决定或研究结论 | 与本研究的关系 | 基线落点 |
|---|---|---|
| 高斯外观层维持 P3（结论 C0）：第一阶段不做高斯，只用网格渲染；不采纳 C1 的 P1 前移 | 与 §0 第 2 条、§10.2 一致：网格蒸馏 3DGS 不比网格更真实，生成式细化无增益 | AWR-05 ADR-188、§6.4.3、§6.4.7 |
| G-3DGS 保留为 P3 入口门；真实航拍参照集暂时只用公开数据集（MARS-LVIG 等），不安排实地拍摄 | 取代 r11 建议的 P600 参照集；r1 已用 MARS-LVIG HKisland03 做 sim2real 回归 | AWR-05 ADR-189、§6.4.8（公开数据集清单、用途与许可） |
| FLUX.1 [dev] 允许在内部研究中使用（许可解释由用户负责），用于 P3 复测 Skyfall-GS 原版细化 | 补全 r11 §6.4 所列"未复现原版细化强度"的局限 | AWR-05 ADR-190、§6.4.9 |
| 暂不确定合成权重；排行榜按事件类型或研究任务分赛道，各赛道分别报告四项指标 | 与 §0 第 8 条一致：不同策略在 EC、EU、协同收益上的排序不一致（r6 §12.3） | AWR-05 ADR-191、§5.6.9 |
| SuperSplat 系高斯采纳项（splat-transform 发布流水线、内部编辑器、Lab 检视视图、Spark 共渲染）随外观层移至 P3；网格选择交互复用于事件编辑器（P1） | r12 §4、§5 的建议阶段为 P1 或 P2；工作量与风险按 r12 §5、§6 登记 | AWR-05 ADR-192、§6.5、§10.6、§12.1（WP-D3-39 至 WP-D3-41） |
| M25 运行期维持 gsplat，借用 SOG 与 SPZ 格式和 `fillRatio` 发布门 | r12 §4.4 | AWR-05 §6.5.6；M25 v1.3 §17 |
