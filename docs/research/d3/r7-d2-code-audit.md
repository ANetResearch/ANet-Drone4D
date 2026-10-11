# r7 研究笔记：D2 代码审计——面向 D3"固定事件 + 主动观测"沙盘的复用、扩展、冲突与改造清单

> 研究单元：D3 / r7-d2-audit　　日期：2026-10-11　　性质：只读审计（未修改项目代码与 `docs/research/d3/` 以外的文档）
>
> 输入：用户设计讨论 `/data/projs/anet-drone-data/design-r1.md`（World/Event/Physics/Agent 权威分离、GLB 派生多表达、Canonical Event Tape、M22–M27、P0–P3）；D2 文档 `docs/03`、`docs/04`（v1.3，含 D2.1）、`docs/16`、`docs/17`、`docs/20`、`docs/modules/M17–M20`、`docs/impl/D2-*`（含 D2 验收第 1 轮、D2-FX-R2 七个修复区、D2-INT 第 2 轮）；代码 `python/awr/**`（150,059 行，2,596 个测试函数）与 `apps/web/src/**`（85,089 行）。交叉引用 D3 同期单元 r1（FAST-LIVO2）、r2（GLB 世界）、r3/r10（相机渲染）、r4（网格 LiDAR）、r5/r9（Gazebo）、r6（事件运行时与基准）、r8（Rerun）。
>
> 代码基线：工作目录 `/data/projs/anet-drone`（不是 git 仓库，无提交号），2026-10-11 07:00–09:00 的状态。D2 仍在另一工作流中验收修复（第 1 轮 55 项通过 26 项；D2-INT 第 2 轮报告正在写），本文的判定针对接口与语义，不针对 D2 的验收结论。
>
> 实验环境：本机（Xeon E5-2603 v4，8 vCPU，62 GB；实验期间机器负载 20–25，与其他研究单元共享）；项目 `.venv`（Python 3.12.3、numpy 2.5.3、numba 0.67.0、trimesh 5.1.0），embreex 4.4.0 以符号链接借用 r2 单元的 venv（`pylib/`）。全部实验只用 CPU、数分钟内完成，**未使用 dmax，未创建容器，未传输数据**；GLB 舞台直接复用 r6 单元从 oryahovo 裁出的米制网格。
>
> 脚本与原始结果：`.cache/research/d3/r7-d2-audit/`（`scripts/`、`results/`、`pylib/`）。

---

## 0. 结论速览

| # | 主题 | 结论 | 关键证据 | 落点 |
|---|---|---|---|---|
| 1 | 总体可复用度 | D2 Python 代码按行数计：**45% 原样复用**（时钟与内核、命令与安全、规划与集群算法、环境、契约、运行时基础设施），**39% 只需加接口**（世界导入与查询、传感器、录制、沙盒、接口层、智能体），**12.5% 与 D3 语义冲突、需隔离后复用**（M18 识别物反应、M19 真值关联感知、M20 特权编排），3% 与 D3 无关（M01 重建） | E1 静态审计（`results/e1_loc_classes.json`） | §3、§6 |
| 2 | 扩展机制已经齐备 | sim-core 的登记表（stage、实体列、会话段、查询、命令、批处理输出、快照导出、设定点过滤）、World 构建的派生器登记表、World 图层类型枚举（已含 `mesh`、`gaussian`、`collision`、`voxel`、`sdf`）、`SimBackend` / `SensorBackend` 协议，使 M22–M27 可以"插入"而不是"改写"。必须触及 D2 的只有 14 处（约 20 个文件，P0 占 12 处），全部是加字段、加枚举、加分派的小改，缺省路径不变 | `sim/fleet/stages/registry.py`、`world/package/derivers.py`、`world.schema.json` | §7.3 |
| 3 | **识别物反应破坏固定事件（实测）** | 同一事件、5 种机队策略：D2 缺省反应（人：注视 10 s 后背离 100 m，并向 50 m 内同类广播）下，一架机悬停在出发点附近就让 A 偏离 53.9 m、晚到 38.5 s；盘旋或跟踪时 A 根本到不了终点（偏离 386–490 m），B 偏离 409–570 m。车辆类缺省 `ignore`，不受影响 | E2a（`results/e2/e2_summary.json`） | M23 Exogenous |
| 4 | Exogenous 隔离可行且便宜 | 把反应改为 `ignore` 且不广播后，5 种策略的三名演员轨迹 sha256 完全相同（`a3d9f2c7…`），而"被发现"事件照常计算（0、0、1、8、3 次），可以直接作为"隐蔽观测"指标。说明 M18 的判据、状态机与运动可以拆开：运动由事件磁带给，判据留作度量 | E2b | E4 改造项 |
| 5 | D2 运动模型不能表达事件 | `path` 航点没有时刻（开阔地到点比设计早 1.3–1.5 s，无法写"t = 120 s 到广场"）；直线段被房子挡住时**永久卡在墙前**；在 GLB 小镇上沿磁带路径（每 30 s 取一点）行走，300 s 内 78–82% 的步被判"台阶 > 0.5 m"阻挡，只走了 74–91 m（磁带 384 m），并且地面类按 DSM 顶面取高（可能走上屋顶或树冠） | E2d、E4（M18 垫片段） | M23 用 NavMesh 时间线（r6） |
| 6 | 计数器 RNG 以主时钟 tick 为键 | 同一种子、同一 `stage_dt = 0.1 s`，只把 tick 步长从 10（sandbox100）改为 25（d1_250），random_walk 在 0.6 s 后分叉，120 s 内平均偏差 13.9 m、最大 27.6 m。识别物运动、M19 量测、GNSS/IMU 噪声、检测器都以 tick 为键（16 处），只有 LiDAR 用帧序号。事件磁带与 D3 传感器噪声必须改用"事件时间索引 / 传感器帧序号"取键 | E2c、E1 `rng_sites` | M23、M25 |
| 7 | 2.5D DSM 视线不能当真值 | 在 GLB 小镇事件上 6,087 个机–人/车观测对 × 3 条射线：D2 `los_batch` 核在 2 m DSM 上与网格精确求交**不一致 13.3–13.6%，几乎全是"误判遮挡"**；0.5 m DSM 降到 2.9–4.0%（仍有 0.2–0.6% 误判可见）。17.8% 的演员脚点低于 2 m DSM 0.5 m 以上（墙边、屋檐、树下），被 2.5D 语义当成"在障碍物里" | E4 | M19 只做 Fast 档；真值走网格 / 渲染 |
| 8 | WorldQuery 可以"换芯" | 104 处 WorldQuery 调用中，93 处是高度、走廊、贴地类 2.5D 语义（`ground_dtm` 23、`height_dsm` 22、`heightmap_top_along` 12 …），保持"网格派生 DSM/DTM"即可；只有 11 处（`los_batch` 8、`free_distance` 2、`ray_hit` 1）需要网格精确求交。MeshWorldQuery 垫片（同签名）装到 D2 M18 台架上**不改一行代码即可运行**，`LosCache` 令牌桶照常工作 | E1、E4 | W6 |
| 9 | M19 是"模型化感知 + 真值关联" | 感知帧逐项带真值 id（`"target": snap.ids[i]`）与 `n_in_fov`；航迹"每识别物一条、融合我方各机量测"（按 target_no 关联、全机队共享）。这是理想数据关联 + 全局融合，违背"每机无全局视野"。只能作为 L1 Fast 档观测（经匿名化与按机私有化）或评测端的理论 oracle | `sim/perception/runtime.py:247`、`tracks.py` 文档串 | A2、§5 C3 |
| 10 | SensorBackend v1 不够用 | v1 只有静态世界、单一机体位姿、无逐传感器触发、payload 为无类型 dict；且从未有生产实现（只有测试替身 FakeBackend）。v2 草案（逐传感器请求、PoseWindow、DynamicSource、观测与真值分离）+ 原型 MeshLidarBackend：**原样复用 D2 的 MID-360 花样、`ray_aabb` 核、计数器 RNG 与 `lidar_frame.v1` 编码**，20 帧校验 0 错误、两次逐字节相同；逐点位姿与 D2"整帧单一位姿"相比，8 m/s 平飞 p95 差 0.37 m，90°/s 偏航 p95 差 6.9 m | E5（`scripts/sensor_backend_v2.py`） | S1 |
| 11 | 录制回放缺事件维度 | 37 个实时 topic 中 18 个 D2 topic（识别物、感知、LiDAR、任务、场视图…）**不进 MCAP**；公开站与沙盒会话不开录制器；回放不含识别物；写入器满队列时丢弃 Full64 与块消息（对数据集不可接受） | E1、`recorder/app.py:146–153`、`configs/runtime.yaml` public 档 | B2、B5 |
| 12 | 沙盒 API ≠ Agent SDK | 沙盒 token 可读全部真值（`GET /targets/{id}/state`、`/tracks`、`swarm/target/state`），权限只到角色与席位，接口是实时 WS 与 REST。它适合作为"上帝视角的研究沙盒"，不能作为 Benchmark 的 Agent 接口 | `api/rest/targets.py:199`、`api/rest/perception.py:123`、`api/rt/session.py` | M26 |
| 13 | 前端 | 点云引擎已有 Source 着色（col.rgb 原样显示）且 `world.json` 允许 `defaultColorMode = rgb`：GLB 表面采样成彩色点云后**前端零改动**即可显示（需 RawCloud 加 rgb）；纹理网格、3DGS、人群、服务端渲染视角需要新图层与面板；识别物前后端容量均为 32 | `pointMaterial.ts`、`decode.ts:8`、`entities.py:25` | W2、F2–F4 |
| 14 | 工作量 | 改造清单 48 项，合计约 **78.5 人周**：P0 44.5、P1 14.5、P2 12.0、P3 7.5（±40%）。P0 关键路径约 12 周（W1→W4→E1→E2→E3→A1→B3），4 人并行约 11–12 周 | `results/workload.json` | §6 |
| 15 | D2 不受影响的做法 | D2 先发布打标签；D3 在独立分支，只新增包（`awr/world/glb`、`awr/world/mesh`、`awr/sim/events`、`awr/sensorsvc`、`awr/agentsdk`、`awr/bench`、`awr/gz`）与契约目录；对 D2 的 14 处扩展点小改全部"缺省逐位不变"，以"D3 插件已安装但未加载事件时，D2 批处理事件流、resim 帧摘要、M18 确定性用例逐字节一致"作为回归门；D3 进程只进 GPU 实验池，公开站组合根不变 | §7 | §7 |

**实现者先看这 10 条：**

1. **事件演员不走 D2 的运动模型。** M23 用 NavMesh 时间线生成事件磁带（r6），运行时把磁带作为 M18 的一种运动源（`motion.mode = timeline`），M18 的判据与状态机照跑、只当度量，反应在 Exogenous 下一律不作用于运动。D2 的 `path` / `random_walk` 与反应只留给 Interactive 模式和背景游走。
2. **D2 的 `ReplayBackend` / `KinematicStage` 的 `Track.build + hermite` 可以直接当磁带播放器。** 1 Hz 磁带带速度时插值误差 ≤ 4.5 cm、2 Hz ≤ 1.2 cm、5 Hz ≤ 0.4 cm，每个样点约 1 µs（E3）。传感器按曝光中点或逐点时刻向磁带取状态，不依赖 sim-core 的 stage 次序。
3. **任何进入数据集或评测的随机量都不能以主时钟 tick 取键。** 新登记 RNG 流（19 起），键为 `(seed, stream, actor/uav, event_tick 或 frame_seq, ch)`；D2 已有的流保持原样（D2 行为不变）。
4. **WorldQuery 分两半实现。** 高度、走廊、贴地、规划栅格继续是 2.5D（由网格派生 0.5 m DSM/DTM，M09/M10/M20 行为与 D2 一致）；视线与射线改为网格精确求交，并新增 `raycast_batch`、`walkable_height`、`nav_path`。`open_world_query` 按世界包是否有 `geometry.mesh` 图层分派，D2 七个世界走原路径。
5. **2 m DSM 视线只能做公开站的快速预览。** 数据集真值的可见度来自网格渲染（r3 的实例图）或网格射线；M19 的 Johnson/TTPF 判据保留为 L1 Fast 档与理论 oracle。
6. **观测出口只有一个：M26 的观测过滤器。** M19 帧去真值 id、去 `n_in_fov`，航迹按机私有；LiDAR 的 `tag` 不再带"命中识别物"（D2 LidarScan8 的 bit0 是真值泄漏）；实例图、逐点实例只进 TruthFrame。
7. **SensorBackend 升 v2，v1 保留。** v2 与 v1 并列（`V1Adapter` 让 v2 后端装在 D2 调用点上），帧契约复用 D2 的 FLU/RDF/COLMAP 约定与 `lidar_frame.v1`（点数上限 22,000 需在 v2 放开）。
8. **Benchmark 跑 lockstep，不跑沙盒实时会话。** Env 复用 D2 批处理的组装方式（`SimCore` + `LocalBus` + `LocalRing` + `BatchCtl` 的"固定延迟生效"），把 Agent 决策当作一种有固定时延的作业；沙盒 REST/WS 只用于交互、编辑与提交。
9. **录制分两层。** D2 的 MCAP 录制器不改；D3 用独立的无损写入器（新通道采用 Foxglove schema，r8），事件磁带作附件，数据集与 `.rrd` 由它导出。
10. **D2 先发布再动 D3。** D3 对 D2 文件的每一处改动都要带"缺省逐位不变"的对拍用例（D2.1 已有先例：`hearing_band_adj_db` 缺省不修正、判据逐位不变）。

---

## 1. 范围、方法与判定口径

### 1.1 审计对象

| 层 | 路径 | 行数 | 本文章节 |
|---|---|---|---|
| 识别物（M18） | `python/awr/sim/targets` | 4,411 | §3.1 |
| 感知（M19） | `python/awr/sim/perception` | 7,845 | §3.2 |
| 传感器（M13） | `python/awr/sim/sensors`（含 `lidar/scan_rt.py`、`backends/base.py`） | 7,623 | §3.3 |
| 环境（M07） | `python/awr/environment` | 4,969 | §3.4 |
| 仿真内核（M08、M09、M21） | `sim/fleet`、`sim/core`、`sim/runtime`、`sim/backends`、`sim/safety`、`sim/catalog`、`sim/fixedwing` | 28,652 | §3.5 |
| 任务与集群（M10、M20） | `tasking`、`sim/tasking`、`sim/mission`、`sim/planning`、`swarm` | 23,320 | §3.6 |
| 沙盒（M17） | `python/awr/sandbox` | 6,448 | §3.7 |
| 录制与回放（M12） | `python/awr/recorder` | 4,131 | §3.8 |
| 世界（M03、M04） | `world/ingest`、`world/geometry`、`world/package`、`world/pointcloud` 等 | 11,153 | §3.9 |
| 接口与契约（M11、M00） | `api/**`、`contracts`、`packages/contracts` | 27,063 | §3.10 |
| 智能体（M14） | `python/awr/agent` | 6,800 | §3.11 |
| 前端（M05、M06、M15 与领域图层） | `apps/web/src` | 85,089 | §3.12 |

### 1.2 方法

1. 逐文件读模块文档串、接口签名与关键实现，记录"证据位置（文件:行）"。
2. E1 静态审计：用 `ast` 统计各组行数、包间依赖、WorldQuery 调用面、真值读取面、计数器 RNG 的键、容量常量，以及实时 topic 与录制通道的覆盖差（不 import 被审计代码）。
3. E2–E5 实验：只读调用 D2 代码（`tests/targets/tlib.py` 的 M18 台架、M04 `los_batch` numba 核、M08 `Track`/`hermite`、M13 MID-360 花样与 `ray_aabb` 核、`lidar_frame.v1` 编解码与校验），验证复用、冲突与扩展点的判断。
4. 以 r3、r4、r6、r8、r9、r10 的实测作为 D3 新组件的性能与可行性依据，本文不重复测。

### 1.3 判定口径

| 判定 | 含义 |
|---|---|
| 原样复用 | D3 直接调用，不改代码（可能增加调用方） |
| 扩展接口 | 保留现有实现与缺省行为，增加字段、枚举、钩子或并列的新版本接口 |
| 隔离复用 | 语义与 D3 的基本约束冲突（固定事件、无全局视野、真值隔离），只能在特定档位或评测端使用，需要隔离层 |
| 不适用 | D3 首期不依赖 |

---

## 2. D3 需求到 D2 承载的映射总表

| D3 能力（design-r1） | D2 现有承载 | 差距 | 判定 | D3 落点 |
|---|---|---|---|---|
| GLB 沙盘导入、米制、多表达（外观、碰撞、射线、DSM、体素、NavMesh、锚点） | World Package v1（点云 Potree + `awr-pts@1` + 2 m DSM / 10 m DTM + zones + env）；`IngestAdapter` 协议；派生器登记表；图层枚举已含 `mesh`、`gaussian`、`collision`、`voxel`、`sdf` | 没有网格输入与网格图层实现；RawCloud 无颜色；无 NavMesh、锚点图层；GLB 非米制需标定 | 扩展接口 | M22（W1–W6） |
| 几何查询（视线、射线、贴地、规划） | `WorldQuery` 协议 + `DsmWorldQuery`（柱体 DSM，`los_batch` numba） | 2.5D 视线误判 13%；无 3D 射线、无可行走面 | 扩展接口 | W6 MeshWorldQuery |
| 固定事件（人、车按时刻经过锚点；天气、通信区随时间变化） | M18 识别物（≤ 30，`static`/`path`/`random_walk`，反应、声源）；M07 关键帧与定时操作；剧本 `at_s` 事件 | 航点无时刻、无寻路、反应改事件、RNG 以 tick 为键、容量 32 | 隔离复用 + 扩展 | M23（E1–E6） |
| Exogenous / Interactive 两种模式 | M18 反应状态机 | 无"关闭反应但保留判据"的开关 | 扩展接口 | E4 `reaction_policy` |
| 物理与位姿权威 | FleetSim（SoA、PX4-lite、风经相对空速、DSM 接触）；SimBackend/DroneAdapter 协议；PX4 SIH、Replay 后端 | 与网格的接触仍为 DSM；Gazebo 未实现 | 原样复用（Fast 档）/ 扩展（Realistic 档） | M24（G1–G2） |
| 真实传感器数据（RGB/Depth/Seg/LiDAR，与事件同一时刻） | M13：相机几何、云台、GNSS/IMU 噪声、MID-360 花样、2 Hz DSM 扫描（每会话 1 机）；SensorBackend v1 协议（无实现） | 没有图像；LiDAR 基于 DSM 且单一位姿；无动态世界接口 | 扩展接口 | M25（S1–S7） |
| 每机无全局视野、观测分级 | 无（沙盒与 WS 均为全局视野） | 需要观测过滤与按机作用域 | 新增 | M26（A1–A6） |
| 动作空间 | `uav/{id}/cmd/*`（takeoff、goto、follow_path、orbit、velocity、loiter、gimbal、zoom、sensor/mode…）经 CommandEngine 准入与安全 | 无 | 原样复用 | A3 |
| 协同与通信 | M14 合同网、黑板（OR-Set + HLC）、MockNetwork（键控时延 U(0.9, 1.1) s）；M20 Allocator 协议（greedy、external） | 时延与距离、遮挡、带宽无关；无干扰区 | 扩展接口 | A5 |
| 同一事件反复采集、批量运行 | 批处理自由运行（固定规划时延、逐字节确定，D2-FX-R2 已修复 AC-32）；输入日志 + resim；沙盒批处理（P1） | 无 Agent 回路；无 GPU 资源池 | 扩展接口 | A1、B1 |
| 事件磁带、数据集、评测 | MCAP 录制（D1 通道）、sidecar 索引、回放 worker；场景成功谓词 | D2 topic 不入录、回放无识别物、写入器有损；无数据集导出与评测 | 扩展接口 + 新增 | M27（B1–B5） |
| 浏览器展示 | three.js 点云引擎、图层登记表、识别物与感知图层、FPV/传感器视图、时间引擎与回放 | 无纹理网格 / 3DGS 图层；识别物容量 32；视角为客户端渲染 | 扩展接口 + 新增 | F1–F5 |

---

## 3. 逐模块审计

每节给出：现状（证据）、复用判定、扩展点、冲突、对应改造项（§6 的 ID）。

### 3.1 识别物 `sim/targets`（M18）

**现状**

- 实体表 `EntityTables["target"]`，容量 32、会话上限 30（`sim/core/entities.py:25`），列由 `register_entity_block(kind: Literal["target"], …)` 登记（`sim/fleet/stages/registry.py:476`），一字段一写者。
- 类别 `person, uav, machine, dog, vehicle, object`（`model.py:42`）；运动模式只有 `static`、`path`、`random_walk`（`model.py` `validate_motion`），`path` 至多 64 个航点（`MAX_PATH`），**航点没有时刻**，只有统一速度、`dwell_s` 与 `loop`。
- 运动（`motion.py:233 step_motion`）：10 Hz，pure pursuit 前视 `max(2 m, 1.5·v)`，按加速度与转向率限幅；地面类贴 DSM 顶面（`z = height_dsm`，可落在屋顶）；前视 DSM 高差 > 0.5 m 视为台阶，按 ±30°/60°/90° 局部绕行，**没有全局寻路**。
- 反应（`behavior.py:142 override`）：UNAWARE → SUSPICIOUS → REACTING（onset：none / freeze / look_at）→ ALERTED（response：ignore / flee / approach / return / emit / hide）→ CALMING；`alert_others` 向 `alert_radius_m` 内同类广播并让其注视 5 s。类别缺省（`packages/contracts/target/class_defaults.json`）：人 `look_at 10 s + flee 2.5 m/s 100 m`、广播；狗 `emit + approach`；无人机 `return`；车、机械、物体 `ignore`。
- 判据：geometric（在敏感体内驻留）与 physical（视觉角尺寸、对比度、DSM 视线；声学带余量；雷达 SNR）；视线经共享 `LosCache`（每会话 ≤ 250 条/s，M18 子配额 25 条/s）。
- 随机数：流 17 `targets_motion`，键 `(seed, 17, target_no, tick, ch)`（`runtime.py:546`）。
- 只读接口：`view()`、`config(id)`、`state1(id)`、`keepout(id)`、`quant_view(id)`；发布 `swarm/target/state`（TargetLite32，10 Hz）。

**复用判定：隔离复用 + 扩展接口。**

| 可直接复用 | 需要扩展 | 与 D3 冲突 |
|---|---|---|
| 实体表与 roster、TargetLite32 线格式、敏感体几何与判据（作为"隐蔽观测"度量）、声源模型、禁入体与软围栏、`LosCache`（换芯后）、命令与 REST、会话段装配 | `motion.mode = timeline`（运动源钩子）、`reaction_policy = exogenous`、新 RNG 流的取键方式、容量 > 32（人群走新实体种类 `actor`） | 反应改事件轨迹（E2a）；航点无时刻、卡墙（E2d、E4）；以 tick 为键（E2c）；地面高度取 DSM 顶面 |

**改造项**：E3、E4、E6；F2。

### 3.2 感知 `sim/perception`（M19）

**现状**

- stage `perception`（order 175）按片轮转，每机 5 Hz；成像传感器（EO、NIR、LWIR）：视锥门 → 像素门（N ≥ 3.5）→ 按波长光学厚度 → 三条视线（共享 `LosCache`）→ Johnson/TTPF 等级与捕获迟滞；雷达（Swerling、地杂波）、MID-360 点数判据、声阵列方位。
- 感知帧逐项带真值 id：`{"target": snap.ids[i], "cls_est": …, "level": …, "meas_enu_m": …}`（`runtime.py:247`），帧头带 `n_in_fov`。
- 航迹：每识别物一条恒速 Kalman，按 `target_no` 关联、**融合我方全部机体的量测**（`tracks.py` 文档串），M20 只读航迹。
- 随机数：流 18，键 `(seed, 18, agent_no, tick, ch)`。
- 纯函数：`perception_level`、`feasible_view`、`feasibility_window`、`zoom_for_level`（M20 用）。
- 成本：D2-FX-R2 把 12 × 30 的 stage 成本从 490.6 降到约 24 ms/仿真秒，但沙盒组合根实测仍约 95–106 ms/仿真秒（M13 读取占一半）。

**复用判定：隔离复用。**

- **作为 L1 Fast 档观测**：保留判据与等级，但必须经 M26 的观测过滤：真值 id 换成按机的匿名检测 id，去掉 `n_in_fov`，航迹改为每机私有（或交给 Agent 自己做）。
- **作为评测 oracle**：`perception_level`、`feasible_view` 给出"理论上能否看到、能看清到哪一级"，用于 Active Observation 指标的"错失机会"（r6 §12）。
- **不能作为数据集真值**：视线基于 2.5D DSM（§4.4 显示 2 m DSM 误判 13%）；等级是统计模型而非像素。数据集可见度真值以网格渲染的实例图为准（r3）。

**改造项**：A2；B4（oracle 指标）。

### 3.3 传感器 `sim/sensors`（M13）

**现状与判定**

| 子模块 | 现状 | 判定 |
|---|---|---|
| `frames.py`、`camera.py`、`gimbal.py`、`mounts.py`、`spec.py`、`describe.py` | FLU 机体系、RDF 光学系、COLMAP 像素中心 0.5、`T_world_base · T_base_mount · R_gimbal`；r3 证明 5 种渲染器可无损落地 | 原样复用（D3 相机内外参的唯一来源） |
| `cbrng.py` | SplitMix64 逐级派生，键 `(seed, stream, agent, tick, ch)`，通道登记表防重叠 | 原样复用算法；**D3 新流不用 tick 作键**（E2c） |
| `gnss.py`、`imu.py`、`nav_pub.py`、`baro.py` | Gauss-Markov、白噪声、上电偏置；以 tick 取键 | 扩展（SLAM 档按帧序号或事件时间取键，S6） |
| `detector.py`、`thermal_mock.py` | D1 S3 剧本的 Mock 检测器与热图 | 不适用 |
| `lidar/pattern.py` | MID-360 表驱动花样，20,000 点/帧，与参考公式误差 ≤ 1e-6° | 原样复用（E5 已复用） |
| `lidar/frame.py` | `awr.sensor.lidar_frame.v1`（20 B/点，逐点 `offset_time`）与 `LidarScan8` | 复用；`point_count ≤ 22000` 需 v2 放开（r4）；LidarScan8 的 `tag bit0 = 命中识别物` 是真值泄漏，不能给 Agent |
| `lidar/scan_rt.py` | 慢任务，≤ 2 Hz、每会话只为 1 架机生成，快进 > 5 暂停；2 m DSM 柱体 DDA + ≤ 30 个识别物 AABB；整帧单一位姿（帧开始位姿按速度外推 50 ms） | 保留给公开站预览；**不得用于数据集**（r4：与网格测距差中位约 1 m，街道低空 41% 射线命中判错）；`ray_aabb` 核可复用（E5） |
| `backends/base.py` | `SensorBackend` 协议（`open(world, rigs)`、`request(slots, poses_enu, t_sim_ns)`、`poll()`）与测试替身 `FakeBackend`；**全仓没有生产实现**；`SensorCaps.raycast ∈ {none, dsm, geo, gz, isaac}` | 扩展：v2 并列（S1） |

**SensorBackend v1 的四个缺口**（E5 原型验证）：

1. 没有动态世界：`open` 只给静态 world，请求不带"该时刻的演员状态"，后端看不到人和车。
2. 一个请求只有一个机体位姿（`poses_enu[n, 7]`）：LiDAR 逐点时刻、相机曝光窗口都需要时间窗内多个位姿。
3. 没有逐传感器的触发与云台、变焦：同一机体 RGB 15 Hz、LiDAR 10 Hz 无法分别请求（FakeBackend 甚至只用缺省俯仰角）。
4. `payload: dict` 无类型：数据集导出、MCAP 通道、观测与真值分离都需要固定帧契约。

**改造项**：S1–S7。

### 3.4 环境 `environment`（M07）

**现状**：`EnvironmentService`（`query(pos, t, fields)`、关键帧、`apply(op, apply_tick)`、checkpoint）；预设与三种过渡（step、smooth、exp）；锚点积分（雨后湿润、积水）按 20 ms 网格，与前端 `engine/environment` 逐行对应；风 = 剖面 × 平均风 + 阵风锋面（流 6）+ 湍流盒（流 1）；天气图（种子派生的 512² 噪声）；日历、太阳、照度、按波长光学厚度、声学背景；全量求值节拍按 20 ms 仿真时间网格 `k = t_ns // 20 ms`（**与时钟档无关**，这一点比 M18 的取键方式好）。

**复用判定：原样复用 + 小扩展。**

- 事件 DSL 的环境轨（`{t, wind_mps, rain_mm_h, visibility_m, …}`）编译为一串 `env/set`、`env.gust` 定时操作，在网格点生效，服务端与浏览器的天气表现自动一致。
- 缺口：① 没有"作者指定的局部区域"天气（只有噪声天气图）——首期可接受，局部雨区留到 P2；② 通信干扰区不属于 M07，交给 M26 的链路模型（r6 磁带的 `comms.zones`）；③ 风场不感知建筑（r4/r9 也未做），无人机近楼气流不在首期范围。
- 渲染器的天气后处理（r3：雾 0.3 ms、雨 1.7–2.4 ms）以 `sky_sample` / `query` 的快照为输入，保证图像与物理用同一天气。

**改造项**：E3（环境轨编译）。

### 3.5 仿真内核 `sim/fleet`、`sim/core`、`sim/runtime`、`sim/backends`（M08，及 M09、M21）

**现状**

- `FleetSim`：SoA 机体表（1024 行）、PX4-lite 控制、气动（风只经相对空速进入）、半隐式积分、DSM 接触与机间碰撞；numba 融合核与 numpy oracle 两条路径对拍。
- 扩展点登记表（`stages/registry.py`）：`register_stage(hz=, order=)`、`register_entity_block`、`register_session_loader/snapshot_exporter`、`register_query`、`register_command_handler(vehicle=True)`、`register_slow_task`、`register_batch_output`、`register_setpoint_filter` 等；构建期校验 order 区段（`budgets.py:62`，空闲区段 125–127、195–199）、包前缀所有者、Σ预算 ≤ 0.40 核、一字段一写者。
- 时钟档 `d1_250` / `sandbox100`（进程内不可变）；checkpoint；输入日志 `inputs.msgpack` 与 `--resim`（同内核同输入逐位一致）；批处理自由运行（`sim/runtime/batch.py`：`SimCore` + `LocalBus` + `LocalRing`，规划结果按 `request_tick + lat_ticks` 固定延迟生效，D2-FX-R2 修复后 1 h 两次逐字节一致）。
- 后端协议（`sim/backends/base.py`）：`SimBackend`（attach、spawn、despawn、dispatch_batch、step、snapshot、restore）、`EntityAdapter`、`DroneAdapter`、`EnvironmentService`、能力声明 `rt/caps/<backend>.json`；实现：mock、px4_sih、prometheus、**replay**（L0 运动学回放，`Kind` 已含 `human`、`vehicle`，`stages/kinematic.py` 的 `Track.build`：位置三次 Hermite、姿态 slerp）。
- M09 安全（地理围栏、电池、故障、FSM）、M21 机型目录与固定翼。

**复用判定：原样复用（Fast 档的物理权威），批处理与后端协议扩展。**

- Fast 档直接用 FleetSim；Realistic 档由 M24 实现 `SimBackend/DroneAdapter`（r9 已验证 gz-sim 外部时钟锁步与逐位确定）。
- `Track`/`hermite` 作为事件磁带插值器（E3）；ReplayBackend 也可把一条已录的机队航迹作为"幽灵机"重放（多 Run 对比可视化）。
- 批处理的组装方式与"固定延迟生效"规则是 lockstep Env 的现成骨架（A1）：Agent 决策等价于一种 `lat_ticks(kind = "agent")` 的作业。新入口放在 `awr/bench`，不改 `batch.py`。
- 需要的登记表增补：M22–M27 的 order 区段与包前缀、`register_entity_block` 的 kind 加 `actor`（P1 人群）。

**冲突**：无直接冲突；需要注意 stage 次序——识别物 stage 在 order 170、传感器在 100–109，D2 中传感器读到的是上一 tick 的识别物位置。D3 的传感器服务按曝光时刻直接查磁带，不受该次序影响。

**改造项**：A1、I2、E6、G1。

### 3.6 任务编排与集群（M10 `sim/mission`、`sim/planning`；M20 `tasking`、`sim/tasking`；`swarm`）

**现状**：M10 航线生成器（lawnmower、helix_scan、orbit、expanding_square、corridor、terrain_follow、formation、follow_path）、2.5D A*、平滑与去冲突、规划进程池；`swarm` 的覆盖分割、条带、匈牙利分配、CAPT 编队；M20 六类任务、机巢与换电、GSD 扫描反推、贪心分配、7×24 接力；`Allocator` 协议（`tasking/allocator.py:27`，greedy、external；anet_cnp、llm_agent 为桩）。

**复用判定**

- M10 生成器、`swarm` 算法：**原样复用为基线飞行策略**（"Coverage First""Lawnmower" 等基线，r6 已用其思路做对照）。
- M20：**隔离复用**。它的 `Snapshot.keepouts` 来自识别物真值、`state1(id)` 被调用 11 处、读 M19 的全局航迹与捕获状态——这是"已知目标位置的特权集中式编排"。可作为 Benchmark 的 **L0 特权基线**（上界参考）与"已知目标"类任务，但不能出现在"未知目标搜索"任务的 Agent 侧（design-r1 §7 的要求）。
- `Allocator` 协议可以作为 M26 "机队级策略"的接口形态（集中式策略一次给出全部分配），与"每机 `DroneAgent.step`"（分布式策略）并列。

**改造项**：A4（基线打包）。

### 3.7 沙盒 `sandbox`（M17）

**现状**：每会话一个 sandbox100 sim-core 进程；池进程负责排队、配额、倍速治理、降级阶梯、空闲回收、状态落盘与进程接管；场景模板与配置快照（`awr.sandbox.snapshot.v1`，`additionalProperties: false`，段：env、models、nests、vehicles、targets、tasks）；共享规划服务；本机批处理（P1，`POST /sessions/{sid}/batch` 另起 `--batch` 进程，公开站关闭）。组合根插件为 `D1_PLUGINS + D2_PLUGINS`（`spawner.py:66`）。会话上限 `vehicles 12、targets 30`（`configs/runtime.yaml:61`）；容量模型按 emax CPU 设计；公开档不开录制器。

**复用判定：扩展接口。**

- 进程管理（spawn、cgroup、接管、配额、排队）可复用为 M27 的 GPU 实验池，但**实验池与公开沙盒池分开**：D3 的渲染与 LiDAR 需要 GPU Worker，公开站 emax 只有 CPU（AWR-04 §4.4 容量模型不适用于 D3）。
- 会话段：快照 schema 需加可选段 `event`（事件引用与模式）、`agents`（策略绑定），会话装配用 `register_session_loader("event", order=45)`。
- 沙盒 API 是"上帝视角"，适合事件设计、可视检查与交互演示；Benchmark 的 Agent 不经它（§3.10）。

**改造项**：B1、P2、I1。

### 3.8 录制与回放 `recorder`（M12）

**现状**：MCAP + awr 编码（RecPrefix8）；写入线程、64 MB 有界队列（75% 先丢 Full64，100% 丢块消息；事件、环境、roster、时钟永不丢）；1 s flush；`.ovw`、`.evx` sidecar；按布局哈希判定兼容；replay-worker 把 MCAP 写回放环、Gateway 切换数据源。订阅只有 state_ext、safety、sensor pose、env、mission、agents、tasks 与事件（`app.py:146–153`），契约 `rec/mcap_channels.json` 共 11 个通道。

**问题**

1. 18 个 D2 topic 不入录：`swarm/target/state`、`target/{id}/detail`、`uav/{id}/perception`、`uav/{id}/sensor/lidar/scan`、`uav/{id}/sensor/nav`、雷达与声阵列、`task/*`、`relay/*`、`nest/*`、`field/*`、`target/{id}/quant`、`sandbox/session`（E1）。因此 D2 回放里没有识别物。
2. 公开档与沙盒会话不运行录制器（`configs/runtime.yaml` public 档注释）。
3. 有损丢弃策略适合"演示录像"，不适合数据集。

**复用判定：扩展接口。** MCAP 容器、sidecar、seek 算法、回放 worker 都可复用；D3 新增独立的无损写入器（同一容器与前缀格式，新通道用 Foxglove schema 以便 Rerun 直接读，r8），事件磁带作 MCAP 附件；回放 worker 增加"从事件磁带发布演员状态"的数据源，使任意 Run 回放时演员与观测同步。

**改造项**：B2、B3、B5。

### 3.9 世界 `world/ingest`、`world/geometry`、`world/package`（M03、M04）

**现状**

- Ingest 十步流水线（单位、上方向、调平、北向、DTM 与原点、法线、HAG 与规则分类、统计、QA 门禁）经 `IngestAdapter` 协议（`config()`、`load() -> RawCloud`、`provenance()`）；`RawCloud` 只有 `xyz`、`normal`、`class`（`types.py:90`），**没有颜色**；点云写出 `col.rgb = baked-height`。
- 构建流水线：ingest → grid → tile → derive → package → validate → publish；派生器登记表（`configs/worldpkg.yaml`，`derive(ctx) -> list[LayerSpec]`）；`contentVersion` 覆盖全部内容文件；校验 V-W-*。
- 图层枚举（`world.schema.json`）已有 `mesh`、`gaussian`、`collision`、`voxel`、`sdf`、`tiles3d`；目录规范预留 `visual/mesh/`、`visual/gaussian/`、`geometry/{collision,voxel,sdf}/`、`dynamic/`（AWR-16 §3.1）。
- `WorldQuery` 协议（`query.py:84`）与 `DsmWorldQuery`：DSM 2 m（柱体最近格）、DTM 10 m，`los_batch`（≤ 64 对，step 1 m、eps 0.5 m，numba 与 numpy 逐位一致），派生缓存（Grid25 等，memmap）。

**复用判定：扩展接口。**

- GLB 进入的最短路径：`GlbAdapter(IngestAdapter)` 在网格表面按纹理采样彩色点（RawCloud 加 `rgb`），整条十步流水线、点云容器、前端点云引擎都不变；同时由派生器写出网格图层（外观 GLB、求交网格、0.5 m DSM/DTM、NavMesh、锚点）。
- `MeshWorldQuery`：同一协议，高度类方法由网格派生的 DSM/DTM 提供（M09/M10/M20 行为与 D2 一致），视线与射线类方法走网格求交（E4 垫片已验证接得上）。
- GLB 非米制（r2、r3、r6：town 1 单位约 8.4 m、crop 约 20.9 m），标定必须在 ingest 的"单位"步完成，`coordinate.json` 的 `scaleStatus` 由 `assumed` 变为 `calibrated`。

**冲突**：地面类识别物高度取 DSM 顶面（E4 中 17.8% 的磁带脚点低于 2 m DSM 0.5 m 以上），D3 的演员高度来自 NavMesh 的可行走面（`walkable_height`）。

**改造项**：W1–W6、I1。

### 3.10 接口与实时协议（M11、M00）

**现状**：show-api 与 sandbox-api；GatewayHub 按会话实例化；实时协议 `awr.rt.v1` 37 个 topic（`packages/contracts/rt/topics.json`），兴趣集按需生成（`api/rt/interest.py`：感知、导航、雷达、声阵列、LiDAR 只为被订阅机体生成，LiDAR 每会话至多 1 个）；权限为角色（admin、operator、viewer）+ 席位（`api/rt/session.py`），**没有按机体的观测作用域**；沙盒 REST v1（`/api/sandbox/v1/sessions/{sid}/…`：vehicles、targets、perception、tasking、env、batch …）与 Python SDK（`tools/sdk/awr_sandbox.py`：`targets.state(tid)`、`perception(vid)`、WS `send_setpoint`）。契约版本 1.2.0，规则是 1.x 只增可选字段。

**复用判定：扩展接口。**

- 新 topic（只增）：`swarm/actor/state`（人群）、`uav/{id}/sensor/{name}/image|depth|seg`（blob）、`uav/{id}/sensor/lidar/frame`（v2）、`event/status`；兴趣集机制原样复用（只为被订阅的机体推送渲染帧）。
- 新 REST（独立前缀、开关之后）：`/events`、`/runs`、`/datasets`、`/bench`。
- **Agent 不走这条链路**：WS 是实时、尽力而为的；角色级权限无法保证"每机只看到自己的观测"。Agent 侧使用 M26 的进程内（或容器 RPC）lockstep 接口；网页只做观察与对比。

**改造项**：P1、P2、I1。

### 3.11 智能体 `agent`（M14）

**现状**：每机一个 `DroneAgent`（持有 AID，能力处理器只拿到 `HandlerCtx`：只读机体视图、经 `TrustedGuard` 的 `CommandPort`、估价、检出订阅）；合同网委派、回执校验；协调者黑板（OR-Set + HLC，claim、intent、evidence、conclusion、retraction）；`MockNetwork` 时延为键控 U(0.9, 1.1) s（流 5）；LLM 网关与审计。

**复用判定：扩展接口。**

- `TrustedGuard` / `CommandPort` 原样作为 Agent 动作的安全闸（禁入、包络、限速）。
- 黑板与合同网是"ANet 协同策略"的一种实现，放在 M26 的链路模型之上（r6：通信只在 Env 内发生，ANet 不是旁路）。
- `MockNetwork` 的时延与距离、遮挡、带宽无关，需要由链路模型替换投递时刻与丢包（A5）。
- `SimBridge` 经总线与 StateRing 读全量机体状态（含其他机），在 Benchmark 中 Agent 只能拿到本机的视图，需按机过滤。

**改造项**：A3、A5。

### 3.12 前端 `apps/web`（M05、M06、M15 与领域图层）

**现状**

- 点云引擎（`engine/pointcloud`，5.3 k 行）：Potree 2.0 + ANET_Q16，着色模式 Height、HAG、Normal、Class、**Source（`uColorMode = 4`，col.rgb 原样显示，`engine/pointcloud/render/pointMaterial.ts`）**、Intensity；`world.json` 的 `render.defaultColorMode` 枚举含 `rgb`。
- 视口（M06）：WebGL2 经典路径 + TSL 节点材质，图层登记表（`viewport/layers/registry.ts`，`LayerId` 联合类型、所有者联合类型），严格的 UBO、MRT、着色器动物园规则（`viewport/README.md`）；传感器视图（FPV、夜视、LWIR）由客户端把世界图层渲染到传感器目标。
- 识别物图层：TargetLite32 解码，`TARGET_CAP = 32`（`engine/targets/decode.ts:8`），敏感体网格。
- 时间引擎与回放（`engine/time`：Hermite 插值环、sidecar、播放控制）。
- 只有机体 hero 模型用 GLTFLoader；**没有纹理网格世界图层，也没有 3DGS 图层**。

**复用判定：扩展接口 + 新增。**

- P0：GLB 采样成彩色点云，`defaultColorMode = rgb`，前端零改动显示小镇；≤ 30 名脚本演员复用识别物图层（TargetLite32 与类别图标）；新面板显示服务端渲染的无人机视角（JPEG 帧流），客户端传感器视图保留为预览。
- P1–P2：人群实例化图层；纹理网格图层（3D Tiles / glTF LOD、KTX2；须遵守 M06 规则 9、12 的 UBO 与 MRT 约束）；事件编辑器（锚点、演员时间线、环境关键帧）。
- P3：3DGS 图层（D1 r13 的 spark 路线）与 Rerun Web Viewer 嵌入（r8，双向时间联动已验证）。

**改造项**：F1–F5。

---

## 4. 实测

### 4.1 E1 静态审计

命令：

```bash
python3 .cache/research/d3/r7-d2-audit/scripts/e1_static_audit.py /data/projs/anet-drone \
    .cache/research/d3/r7-d2-audit/results/e1_static_audit.json
```

**规模与复用分类**（`results/e1_loc_classes.json`）：

| 分类 | 行数 | 占比 | 包含 |
|---|---|---|---|
| 原样复用 | 68,168 | 45.4% | 契约、M08 fleet/core、awr.runtime、M09、M10、M20 算法库、M21、swarm、M02、jobs、M03 terrain/semantic、M11 其他 |
| 扩展接口 | 58,696 | 39.1% | M13、M11 api.rt/rest、M14、M17、M08 runtime/backends、M07、M12、M03 package/ingest/pointcloud、M04 |
| 隔离复用 | 18,687 | 12.5% | M18、M19、M20 sim.tasking |
| 不适用 | 4,508 | 3.0% | M01 重建 |

"扩展接口"指保留实现、加字段或并列新接口，不是重写。

**WorldQuery 调用面**（104 处，15 个组）：

| 方法 | 次数 | 语义 | D3 处理 |
|---|---|---|---|
| `ground_dtm` | 23 | 地面高 | 网格派生 DTM |
| `height_dsm` | 22 | 表面高 | 网格派生 DSM（0.5 m） |
| `heightmap_top_along` | 12 | 航线走廊最高点 | 2.5D 保持 |
| `column_max_within` | 9 | 半径内柱顶 | 2.5D 保持 |
| `path_coarse_check`、`path_valid`、`safe_transit_profile`、`terrain_profile`、`probe`、`grid_2p5d`、`dtm_grid`、`dsm_grid`、`clearance` | 27 | 规划、校验、贴地 | 2.5D 保持 |
| `los_batch` | 8 | 视线 | **网格求交** |
| `free_distance` | 2 | 射线自由距离 | **网格求交** |
| `ray_hit` | 1 | 射线命中 | **网格求交** |

按组：M10 mission 26、M04 22、M10 planning 9、M18 9、M09 6、M20 lib 6、M13 4、M08 runtime 4，其余 ≤ 3。

**真值读取面**：`state1` 被 M20 调用 11 次、M18 2 次；`keepout` M18 2、M20 1；`capture_state`、`capture_detail`、`quant_view`、`tracks_wire` 在 M19、M20；字符串列名 `tgt_pos` 等出现在 M13（7 次，LiDAR 扫描读识别物包围盒）、M08（tap_targets）、M18。依赖：M19 → M18 35 处 import，M20 sim.tasking → M19 10 处。

**计数器 RNG 取键**（18 处调用）：M18 运动、M19 量测、M13 检测器、GNSS、IMU、导航发布共 16 处以 `tick` 为键；LiDAR 扫描以帧序号为键；热图 Mock 以常数为键。

**容量与频率常量**：`scan_rt.MAX_TARGETS = 30`、`FRAME_PERIOD_NS = 500 ms`；`entities.CAPACITY = 32`；`TARGET_CAP = 32`（前端）；`MAX_PATH = 64`；`MAX_LOS_BATCH = 64`；`BODIES_SESSION_MAX = 64`；`WARN_TOTAL_CORE = 0.40`；`Raycast = Literal["none", "dsm", "geo", "gz", "isaac"]`。

**录制覆盖**：37 个实时 topic、11 个 MCAP 通道，18 个 D2 topic 不入录（§3.8）。

**测试**：`tests/` 下 492 个测试文件、2,596 个测试函数（静态计数，参数化会展开更多），其中 sim 256、sensors 167、perception 158、rt 146、sandbox 141、world 139、runtime 131、agent 128、contracts 120、environment 110、world_query 105、targets 93。这些是 D3 期间 D2 回归门的主体（§7.6）。

### 4.2 E2 识别物反应 vs 固定事件；Exogenous；时钟档 RNG；path 时刻

命令：

```bash
cd /data/projs/anet-drone
PYTHONPATH=python .venv/bin/python .cache/research/d3/r7-d2-audit/scripts/e2_exogenous.py \
    .cache/research/d3/r7-d2-audit/results/e2
```

**设置**：D2 M18 台架（`tests/targets/tlib.py` 的 `Bench`：隔离登记表、真实 `targets` stage、sandbox100 的 tick 编号、`stage_dt = 0.1 s`）；合成世界 600 m 范围、三栋长方体房子；三名演员：A 人（4 航点、Y 点驻留 20 s、1.4 m/s）、B 人（3 航点、驻留 30 s、1.3 m/s）、C 车（2 航点、8 m/s）；类别缺省的敏感体与反应；仿真 600 s。机队策略：`none`（机在地面）、`standoff`（2 机 900 m 外）、`hover_x`（1 机悬停在 A 出发点 X 水平 206 m、高 120 m）、`orbit`（1 机半径 250 m、高 120 m 盘旋）、`track_a`（1 机紧跟 A，水平偏 30 m、高 60 m）。

**E2a/E2b 结果**：

| 策略 | D2 缺省：A 最大偏离 / 到达 Z | D2 缺省：B 最大偏离 | D2 缺省：被发现次数 | Exogenous：轨迹 sha256 前 8 位 | Exogenous：被发现次数 |
|---|---|---|---|---|---|
| none | 0 / 461.7 s | 0 | 0 | a3d9f2c7 | 0 |
| standoff | 0 / 461.7 s | 0 | 0 | a3d9f2c7 | 0 |
| hover_x | 53.9 m / 500.2 s（晚 38.5 s） | 0 | 1 | a3d9f2c7 | 1 |
| orbit | 489.5 m / 未到达 | 569.7 m | 9 | a3d9f2c7 | 8 |
| track_a | 386.3 m / 未到达 | 408.5 m | 5 | a3d9f2c7 | 3 |

- 车辆 C 在所有策略下偏离 0（类别缺省 `ignore`）；track_a 策略下它的状态机照样被触发并进入 CALMING（被发现 1 次），只是不改变运动。
- Exogenous 的设置只有一处：每名演员 `reaction = {onset: none, response: [ignore], alert_others: false}`。说明"判据 + 状态机"与"运动"在 M18 中可以分离，做法见 §8.2。
- 被发现次数在两种模式下不同（orbit 9 vs 8、track_a 5 vs 3），因为缺省模式下演员逃离改变了后续几何。这正是 design-r1 §5.2 所说的"Interactive 运行之间不能声称事件相同"。

**E2c 时钟档**：同一 random_walk 人、同一种子，Exogenous，120 s：sandbox100 两次逐字节一致；sandbox100 与 d1_250（tick 步长 25）**不一致**，0.6 s 后开始分叉，平均偏差 13.9 m、最大 27.6 m。原因：`cbrng.normal(seed, 17, target_no, tick, ch)` 的 `tick` 是主时钟 tick 编号（`runtime.py:546`）。

**E2d path 时刻**（只放 A，Exogenous）：

| 航点 | 设计到达（距离 / 1.4 m/s + 驻留） | 开阔地实际到达 | 一栋房子挡住第 1 段 |
|---|---|---|---|
| Y（驻留 20 s） | 178.6 s | 177.3 s | 未到达 |
| 中间点 | 320.0 s | 318.7 s | 未到达 |
| Z | 462.9 s | 461.4 s | 未到达 |

挡墙情形 A 停在 (-61.6, -51.8)（墙西侧），最后 60 s 只移动 2.7 m，局部绕行在 ±30°/±60° 之间来回。

### 4.3 E3 用 D2 Track/hermite 播放事件磁带

命令：

```bash
PYTHONPATH=python .venv/bin/python .cache/research/d3/r7-d2-audit/scripts/e3_tape_interp.py \
    .cache/research/d3/r7-d2-audit/results/e2 .cache/research/d3/r7-d2-audit/results/e3_tape_interp.json
```

以 E2 的 10 Hz 轨迹为真值，抽稀为 5 / 2 / 1 Hz 磁带，用 `sim/fleet/stages/kinematic.py` 的 `Track.build` + `hermite` 插值回 10 Hz，水平误差最大值（m）：

| 轨迹 | 5 Hz 中心差分 | 5 Hz 带速度 | 2 Hz 中心差分 | 2 Hz 带速度 | 1 Hz 中心差分 | 1 Hz 带速度 |
|---|---|---|---|---|---|---|
| A 步行 + 驻留 | 0.0037 | 0.0019 | 0.0287 | 0.0073 | 0.1439 | 0.0325 |
| C 车 8 m/s | 0.0025 | 0.0012 | 0.0180 | 0.0043 | 0.0735 | 0.0151 |
| B 逃离（D2 orbit） | 0.0061 | 0.0039 | 0.0373 | 0.0116 | 0.1685 | 0.0447 |

每个插值样点约 0.4–3.6 µs（Python 向量化，含负载波动）。结论：磁带存位置与速度、10 Hz（r6 的取值）对于"人走、车开"是厘米级以下的保守选择；传感器在任意曝光时刻取演员状态时用 D2 现成插值即可。注意这只覆盖质心轨迹，步态与肢体动作不在其中。

### 4.4 E4 2.5D DSM 视线 vs 网格；MeshWorldQuery 垫片接入 D2 M18

命令：

```bash
W=.cache/research/d3/r7-d2-audit
PYTHONPATH=python:$W/pylib .venv/bin/python $W/scripts/e4_mesh_worldquery.py \
    .cache/research/d3/r6-event-benchmark/out/stage1 $W/results/e4
```

**数据**：r6 的 `stage1`（oryahovo 裁切 600 m × 600 m，476,536 三角形，比例 20.9 m/单位）、0.5 m 顶面 DSM、事件磁带 `tapeK_bg0`（A、B、C，10 Hz，360 s）。2 m DSM 由 0.5 m DSM 4×4 取最大（D2 柱体语义）；DTM 用 30 m 窗口最小值近似。

**观测几何**：每 10 s、每名在场演员，水平距离 {30, 60, 120, 200} m × 地面以上 {30, 60, 120} m × 8 个方位，去掉出界点后 6,087 对；目标点为脚点上方 {0.95, 0.5, 0.05}·h（M18 `LosCache` 的顶、中、底三条射线，人 h = 1.75 m、车 1.5 m）。

**视线判定对比**（以网格精确求交为参照）：

| 判定 | 可见率（顶射线） | 不一致率（顶 / 中 / 底） | 误判遮挡 | 误判可见 | 三射线可见数一致率 |
|---|---|---|---|---|---|
| D2 `los_batch` 核，2 m DSM，step 1 m，eps 0.5 m | 49.2% | 13.6% / 13.3% / 13.6% | 13.3–13.6% | 0–0.02% | 81.0% |
| 同一核，0.5 m DSM，step 0.25 m | 60.2% | 2.9% / 3.5% / 4.0% | 2.7–3.5% | 0.2–0.6% | 94.2% |
| 网格精确求交（embree，两端各裁 0.5 m） | 62.7% | — | — | — | — |

按高度与距离分层（中心射线，2 m DSM 的不一致率）：地面以上 30 m 时 3.2%（200 m）到 14.6%（30 m）；60 m 时 11.6–17.5%；120 m 时 12.3–17.9%。不一致并不随高度下降，因为 2 m 柱体把墙边、屋檐下的整格抬高，演员一旦靠墙就被"埋"进柱体：**17.8% 的磁带脚点低于 2 m DSM 0.5 m 以上**（0.5 m DSM 为 4.4%）。

**成本**：D2 核 2 m DSM 3.9 µs/射线、0.5 m DSM 13.6 µs/射线；本实验的 embree 经 trimesh 包装 200 µs/射线（Python 开销，不代表生产；r4 实测 Warp GPU 250–790 M 射线/s、Open3D Embree 16 线程 11–20 M 射线/s）。M19 的 250 条/s 视线预算对网格求交毫无压力。

**MeshWorldQuery 垫片接入 M18**：垫片只实现 `bounds_m`、`height_dsm`、`ground_dtm`、`los_batch` 四个方法（高度用网格派生 0.5 m DSM/DTM、视线用网格），装到 D2 M18 台架上，A 用 physical 判据、沿磁带路径（每 30 s 取一点作航点）行走 300 s、一架机在 A 东侧 80 m、地面以上 40 m 跟随：

| 世界 | 运行 | 被发现 | LosCache 射线 | 被阻挡的步 | 里程（磁带 0–300 s 为 383.6 m） | 终点距磁带位置 |
|---|---|---|---|---|---|---|
| 2 m DSM（D2 口径） | 正常 | 1 | 58 | 78.3% | 90.7 m | 167.5 m |
| 网格垫片 | **正常，D2 代码零改动** | 1 | 20 | 82.3% | 74.1 m | 187.4 m |

两点结论：① WorldQuery 协议足以让 D2 消费者在网格世界上运行（鸭子类型，令牌桶与缓存照常）；② D2 运动模型在摄影测量地表上基本走不动（地表起伏、路缘、车辆在 3 m 前视内常有 > 0.5 m 的高差），即使航点取自 NavMesh 路径。演员运动必须由 M23 的 NavMesh 时间线给出。

### 4.5 E5 SensorBackend v2 原型：网格 + 事件磁带的 MID-360

命令：

```bash
PYTHONPATH=python:$W/pylib .venv/bin/python $W/scripts/e5_mesh_lidar_backend.py \
    .cache/research/d3/r6-event-benchmark/out/stage1 $W/results/e5_mesh_lidar_backend.json
```

**原型**（`scripts/sensor_backend_v2.py` + `scripts/e5_mesh_lidar_backend.py`）：`MeshLidarBackend` 实现 v2 协议的 `open / submit / poll / close`；静态几何用 embree，动态演员用 D2 的 `ray_aabb` numba 核（按 1 ms 子片取片中原点与演员位置），花样用 D2 `mid360_pattern`，测距噪声用 D2 `cbrng`（键为帧序号），输出经 D2 `encode_lidar_frame` / `decode_lidar_frame` / `validate_lidar_frame`；演员状态来自 `TapeSource(DynamicSource)`。两架机各 10 帧（t = 100.0–101.0 s）：uav-1 以 8 m/s 平飞（地面以上约 15 m，测绘倒装）、uav-2 以 90°/s 原地偏航。

| 指标 | uav-1（8 m/s 平飞） | uav-2（90°/s 偏航） |
|---|---|---|
| 有效回波率 | 47.7% | 41.1% |
| 10 帧命中演员的点 | 28 | 31 |
| 逐点位姿 vs 整帧单一位姿（D2 语义）的点坐标差 p50 / p95 / max | 0.21 / 0.37 / 49.0 m | 0.66 / 6.95 / 47.1 m |
| `lidar_frame.v1` 校验 | 0 错误 0 警告（400,237 B/帧） | 同左 |
| 观测帧中的真值字段 | 无（实例与世界坐标只在 TruthFrame） | 同左 |

两次重算同一请求逐字节相同；每帧 158 ms（本机负载 20+，embree 单线程经 Python，动态子片循环 46 ms）——性能以 r4 的 Warp 实现为准（每机每帧约 0.07 ms 核函数）。逐点位姿误差与 r4（8 m/s 中位 0.47 m、90°/s p95 3.5 m）同量级，max 为几何边缘处单一位姿打到远处背景的离群点。

---

## 5. 冲突清单与隔离方案

| # | 冲突 | 位置 | 后果 | 隔离方式 | 改 D2 代码？ |
|---|---|---|---|---|---|
| C1 | 识别物反应改变事件 | `sim/targets/behavior.py:142`、`runtime.py _fsm/_broadcast` | 同一事件在不同策略下不同（E2a） | `reaction_policy = exogenous`（缺省 `interactive`）：判据、状态机、事件照常，运动覆盖不生效、不广播；Exogenous 下演员由磁带驱动 | 小改 M18（E4），缺省逐位不变 |
| C2 | 航点无时刻、无寻路、贴 DSM 顶面 | `motion.py:186 _path_desire`、`233 step_motion` | 事件无法按时刻编排，摄影测量地表上卡住（E2d、E4） | `motion.mode = timeline`：位置、航向、速度、状态取自运动源（磁带），跳过 `step_motion` | 小改 M18（E4） |
| C3 | M19 帧带真值 id、全机队共享航迹 | `perception/runtime.py:247`、`tracks.py` | Agent 获得理想数据关联与全局视野 | M26 观测过滤：匿名检测 id、去 `n_in_fov`、按机私有航迹；M19 原输出只进评测端 | 不改 |
| C4 | 2.5D 视线作为可见度真值 | `world/geometry/query.py los_batch`、`LosCache` | 2 m DSM 误判 13%（E4） | 真值用网格渲染实例图（r3）或网格射线；`MeshWorldQuery` 让 M18/M19 的视线也走网格 | 不改（新类） |
| C5 | RNG 以主时钟 tick 取键 | 16 处 `cbrng` 调用 | 时钟档或后端不同则事件、噪声不同（E2c） | D3 新流以事件时间索引或帧序号取键；磁带一次生成、多次播放，不在运行期抽样 | 不改（新流登记） |
| C6 | 沙盒 API、WS 暴露全部真值 | `api/rest/targets.py:199`、`perception.py:123`、`swarm/target/state` | Agent 可绕过观测 | Benchmark Agent 不连沙盒 API；M26 Env 进程内或容器 RPC，按 principal 的 topic 作用域只给网页观察者 | 不改（新接口） |
| C7 | LiDAR `tag bit0 = 命中识别物` | `sim/sensors/lidar/scan_rt.py` 文档串、`_finish` | 观测里夹带真值 | v2 观测帧 `tag` 为 0 或 Livox 语义，逐点实例只进 TruthFrame（E5） | 不改（scan_rt 保留给预览） |
| C8 | 扫描每会话 1 机、≤ 2 Hz、整帧单一位姿 | `scan_rt.py` | 多机建图不可能，LIO 地图漂移（r4：8 m/s 时 ATE 43 cm） | M25 MeshLidar（GPU）独立服务 | 不改 |
| C9 | 识别物 stage 在传感器之后 | order 170 vs 100–109 | 传感器读上一 tick | D3 传感器按曝光时刻查磁带 | 不改 |
| C10 | 录制器有损、不录 D2 topic、公开档不录 | `recorder/writer.py`、`app.py`、runtime.yaml | 数据集缺帧、回放缺演员 | D3 独立无损写入器 + 新通道；回放从磁带补演员 | 不改 M12 主体（回放可选源小改，B5） |
| C11 | 公开沙盒容量模型（emax CPU） | AWR-04 §4.4、`sandbox/admission.py` | GPU 渲染与扫描放进公开池会挤垮 D2 | GPU 实验池独立部署；公开站组合根不加 D3 插件 | 不改 |
| C12 | 容量 32（实体表、TargetLite32、前端） | `entities.py:25`、`decode.ts:8` | 背景人群放不下 | 脚本演员 ≤ 30 复用；人群用新实体种类 `actor` 与新 topic | 小改登记表 kind 字面量（E6，P1） |
| C13 | M20 依赖真值（禁入体、state1、全局航迹） | `sim/tasking/runtime.py` | 作为 Agent 违反信息权限 | 只作为 L0 特权基线与"已知目标"任务 | 不改 |
| C14 | GLB 非米制 | 数据本身 | 速度、像素、通信距离全错（r6 §0 第 1 条） | M22 ingest 单位步强制标定，未标定的世界不能载入事件 | 不改（新适配器） |

---

## 6. 改造清单与工作量

口径：人周（有经验的工程师，含单测与文档，不含验收轮次），估计误差 ±40%；"参考"列为已有原型或研究依据。

| ID | 模块 | 改造内容 | 类型 | 触及的 D2 文件 | 人周 | 阶段 | 参考 |
|---|---|---|---|---|---|---|---|
| W1 | M22 | GLB 导入：读取、单位/上方向/北向/原点、米制标定（控制点或 OSM 配准）、几何不完整掩膜 | 新增 | 无（新包 awr/world/glb） | 2 | P0 | r2、r6 |
| W2 | M22/M03 | RawCloud 增加 rgb；点云写出 col.rgb = srgb 路径（GLB 表面采样成彩色点云，前端零改动显示） | 扩展 | world/ingest/types.py、world/pointcloud/writer.py、world/package/build.py | 0.5 | P0 | AWR-16 §4.4 |
| W3 | M22 | 派生器：visual/mesh（规范化 GLB，去 Draco/meshopt）、geometry/mesh（求交网格 + BVH 缓存键）、0.5 m DSM/DTM | 新增 | configs/worldpkg.yaml（派生器登记） | 1.5 | P0 | r3、r4、r9 |
| W4 | M22 | NavMesh 派生（人、车两套 profile，锚点连通性剪除）与 walkable_height 查询 | 新增 | 无 | 1.5 | P0 | r6 |
| W5 | M22 | anchors.json（建筑出入口、道路节点、区域、事件点）文件格式与校验；编辑器在 F1 | 新增 | 契约 world.schema 图层 type 增补 | 0.5 | P0 | r6 |
| W6 | M22/M04 | MeshWorldQuery：实现 WorldQuery 全部方法（高度类走网格派生 DSM/DTM，视线/射线类走网格精确求交），新增 raycast_batch、walkable_height；open_world_query 按图层分派 | 扩展 | world/geometry/query.py（分派入口，缺省路径不变） | 1.5 | P0 | 本文 E4 |
| W7 | M24 | Gazebo 导出：World→SDF、碰撞网格减面、actor/运动学实体 | 新增 | 无 | 1.5 | P2 | r5、r9 |
| W8 | M22 | 3DGS 外观图层（真实航拍重建资产） | 新增 | 无 | 2 | P3 | r3、r10 |
| E1 | M23 | 事件 DSL drone4d.event.v1 与三道编译门（静态、几何预检、生成后验证） | 新增 | 契约新目录 event/ | 2 | P0 | r6 |
| E2 | M23 | 事件磁带生成与格式（按演员分列 + 差分 + zstd，内容哈希与出处分离）、读取器（D2 Track/hermite 插值） | 新增 | 无 | 1.5 | P0 | r6、本文 E3 |
| E3 | M23 | EventRuntime sim-core 插件：timeline 运动源、环境轨编译为 M07 EnvOp、通信区交 M26 | 新增 | budgets.py 区段与包前缀登记 | 1.5 | P0 | 本文 §8.2 |
| E4 | M18 | 识别物增加 motion.mode = timeline 与 reaction_policy = exogenous（缺省 interactive，D2 行为逐位不变）；新 RNG 流按事件时间索引取键 | 扩展 | sim/targets/{model,motion,runtime}.py、契约 target.schema、rng_streams | 1 | P0 | 本文 E2 |
| E5 | M23 | 语义验证器与真值：event_truth.json、event_graph.json、可观测性 oracle | 新增 | 无 | 1.5 | P0 | r6 |
| E6 | M23 | 背景人群（DetourCrowd）与实体种类 actor（容量 > 32） | 新增/扩展 | registry.register_entity_block 的 kind、sim/core/entities.py | 2 | P1 | r6 |
| E7 | M23 | Interactive 模式：沿用 D2 反应，记录闭环事件带供反事实分析 | 扩展 | 无（复用 M18） | 1 | P2 | design-r1 §5.2 |
| S1 | M25 | SensorBackend v2 契约（逐传感器触发、PoseWindow、DynamicSource、Obs/Truth 分离）与 V1Adapter；帧契约 rgb/depth/seg 与 lidar_frame v2（点数上限） | 扩展 | sim/sensors/models.py（Raycast 字面量）；backends/base.py 不改，v2 并列 | 1 | P0 | 本文 E5 |
| S2 | M25 | 光栅相机服务（OpenGL/EGL）：RGB、深度、实例、语义一次光栅化；天气后处理；ISP | 新增 | 无 | 2.5 | P0 | r3、r10 |
| S3 | M25 | 传感器调度与 GPU Worker：曝光中点时间戳、按世界/分辨率合批、在线闭环与离线重渲染两种模式 | 新增 | 无 | 1.5 | P0 | r3、r10 |
| S4 | M25 | 可见度真值 visibility.json（可见比、遮挡物类别） | 新增 | 无 | 1 | P0 | r3 |
| S5 | M25 | MeshLidar（GPU 网格逐射线、逐点位姿、动态实体、反射率、雨雾） | 新增 | 无（scan_rt 保留给公开站预览） | 2.5 | P1 | r4 |
| S6 | M25/M13 | SLAM 档：IMU/GNSS 噪声键改为帧序号/事件时间；Livox CustomMsg bag 导出与 FAST-LIVO2 回归 | 扩展 | 无（新流登记） | 1.5 | P1 | r1、r4 |
| S7 | M25 | 3DGS RGB pass + 网格深度合成（C3） | 新增 | 无 | 2 | P3 | r3、r10 |
| G1 | M24 | GzSimBackend（SimBackend/DroneAdapter）、Drone4DClock 锁步、状态桥 | 新增 | 契约 rt/caps/gz.json | 3 | P2 | r9 |
| G2 | M24 | Gazebo 传感器经 SensorBackend v2 输出；PX4 SITL 档 | 新增 | 无 | 3.5 | P2 | r9、r20–r22 |
| A1 | M26 | lockstep Env：reset/step、决策时延模型（推广 D2 批处理的固定延迟生效） | 新增 | 无（新入口，不改 sim/runtime/batch.py） | 2 | P0 | r6、D2-FX-R2-sim |
| A2 | M26 | 观测分级与隔离过滤：L0–L3；M19 输出匿名化（去真值 id、按机私有航迹） | 新增 | 无 | 1.5 | P0 | 本文 §3.2 |
| A3 | M26 | 动作映射：uav/{id}/cmd/*（含 gimbal、zoom、sensor/mode）经 CommandEngine 与 TrustedGuard | 复用/扩展 | 无 | 1 | P0 | M14 guard |
| A4 | M26 | Python SDK、Agent 容器协议、示例与基线（贪心跟踪、M10/swarm 覆盖、M20 特权 oracle） | 新增 | tools/sdk 增加独立 agentsdk，不改 awr_sandbox.py | 1.5 | P0 | r6 |
| A5 | M26 | 通信链路模型（距离、遮挡、丢包、时延、带宽、事件干扰区）；M14 ANet 作为其上的协同策略 | 扩展 | agent/anet_mock 只读复用 | 2 | P1 | r6 |
| A6 | M26 | 每机私有地图助手（稀疏 TSDF/占据、Frontier） | 新增 | 无 | 1.5 | P1 | r4 |
| B1 | M27 | Run 规格、队列与 GPU 实验池（复用 M17 池与批处理的进程管理，不进公开池） | 扩展 | 无（新进程入口） | 1.5 | P0 | M17 |
| B2 | M27 | 无损录制档与新 MCAP 通道（Foxglove schema），事件磁带作为附件 | 扩展 | 契约 rec/mcap_channels.json 增补；recorder 不改（独立 D3 写入器） | 1.5 | P0 | r8 |
| B3 | M27 | 数据集导出（event-xxx/ 目录、manifest）与 .rrd 派生导出 | 新增 | 无 | 1.5 | P0 | r6、r8 |
| B4 | M27 | 评测：Event Capture（P0），Understanding、Active Observation、Collaborative Efficiency（P1） | 新增 | 无 | 2.5 | P0/P1 | r6 |
| B5 | M27/M12 | 回放：replay-worker 从事件磁带发布演员状态，多 Run 同步对比 | 扩展 | recorder/replay.py（可选源） | 1 | P0 | M12 |
| F1 | M15 | 事件编辑器（锚点、演员时间线、环境关键帧、注释） | 新增 | 无（新 ui/event） | 3 | P1 | r6 |
| F2 | M18/M06 | 演员图层：≤ 32 名脚本演员复用 TargetLite32 与识别物图层；人群用新实例化图层 | 复用/新增 | viewport/layers/registry.ts（LayerId 增补，P1） | 1.5 | P0/P1 | — |
| F3 | M15/M06 | 无人机视角面板（服务端渲染帧流）、多 Run 对比时间轴 | 新增 | 无 | 2 | P0 | r3、r8 |
| F4 | M06 | 纹理网格世界图层（3D Tiles / glTF LOD，KTX2） | 新增 | registry.ts LayerId 增补 | 3 | P2 | r2、D1 r10 |
| F5 | M15 | Run 与数据集浏览、Rerun Web Viewer 嵌入 | 新增 | 无 | 1.5 | P3 | r8 |
| P1 | M11 | 实时协议增补：actor/state、sensor 图像/深度/LiDAR v2 blob、event/status；按 Agent principal 的 topic 作用域 | 扩展 | 契约 rt/topics.json 增补；api/rt 新 channel 生产者 | 1.5 | P0 | — |
| P2 | M11/M17 | REST：/events、/runs、/datasets、/bench（实验 API，挂在独立前缀与开关之后） | 新增 | api/sandbox_main.py 或新 bench-api 入口 | 1.5 | P0 | — |
| I1 | M00 | 契约 1.3.0（只增）：event、tape、bench、sensor v2、world 图层 navmesh/anchors、snapshot 可选段 | 扩展 | packages/contracts/** | 1 | P0 | AWR-17 §16 |
| I2 | M08 | 登记表增补：ORDER_RANGES、PACKAGE_OWNER、BUDGET_CORE 为 M22–M27 加行 | 扩展 | sim/fleet/stages/budgets.py | 0.5 | P0 | — |
| I3 | 全局 | D2 零影响回归门：import 方向检查、未加载事件时 D2 批处理与 resim 逐字节一致 | 新增 | tests/（新增用例） | 0.5 | P0 | 本文 §7 |
| I4 | 运维 | GPU 实验 Worker 部署（容器、GPU 绑定、数据目录） | 新增 | tools/deploy 新目录 | 1 | P0 | r9 |
| I5 | 文档 | 05-D3 设计增补与 ADR、M22–M27 PRD | 新增 | docs/（新文件） | 2 | P0 | — |
| X1 | M27 | 批量 Benchmark：多事件 × 多策略矩阵、排行榜按观测档分组、提交容器服务端运行 | 新增 | 无 | 2 | P3 | r6 |

**汇总**

| 阶段 | 人周 | 退出标准（design-r1 §10，结合本文） |
|---|---|---|
| P0：GLB + 网格 + 固定事件 + 多机 RGB/Depth | 44.5 | 同一事件（磁带哈希不变）、≥ 3 种机队配置产生不同视角的 RGB/Depth/Seg，遮挡与时间正确；同配置两次运行观测逐字节一致（CPU 档）；数据集可导出并按 Event Capture 评测；D2 回归门全绿 |
| P1：网格 LiDAR + 私有地图 + 通信 + 人群 + 编辑器 | 14.5 | 3 机各自地图不同、共享后覆盖率提高；FAST-LIVO2 回归 ATE 在 r4 基线内；通信干扰区影响可测 |
| P2：Gazebo 档 + Interactive + 网格图层 | 12.0 | Fast 与 Gazebo 用同一磁带与接口，差异有容差报告 |
| P3：3DGS + 批量 Benchmark | 7.5 | 无人值守产出标准化数据集与评测 |
| 合计 | **78.5** | |

按模块：M25 12.0、M27 10.0、M22 9.5、M23 9.5、M26 9.5、M24 8.0、M15 6.5、M06 3.0、M11 3.0、M18 2.5、文档 2.0、其他 3.0。

**P0 关键路径**：W1（2）→ W4（1.5）→ E1（2）→ E2（1.5）→ E3（1.5）→ A1（2）→ B3（1.5）≈ 12 人周串行；渲染支线（S1–S4，6 人周）、前端支线（F2–F3）、接口与契约（P1、P2、I1）可并行。4 人约 11–12 周，6 人约 8 周。r3、r4、r6、r10 已有原型的条目（S2、S5、E1、E2、E5、B3、B4）实际工作量可能再低 20–30%。

---

## 7. 保持 D2 不受影响的分层方式

### 7.1 发布次序与分支

1. D2（含 D2.1）先完成验收，打 D2 发布标签（版本号按 ADR-118 的单一来源），发布到 GitHub `main` 与 drone4d.agentnetwork.org.cn（用户要求）。
2. D3 自该标签拉 `d3-dev` 分支。D2 的验收修复若在 D3 启动后继续，只在 `d2` 维护分支修、再合入 `d3-dev`，不反向合并。
3. D3 对 D2 文件的改动（§7.3）集中在一个"扩展点"工作包里先做、先过回归门，其余 D3 工作包只新增文件。

### 7.2 包与依赖方向

```text
D2（不变）                                   D3（新增包，只依赖 D2，D2 不 import D3）
awr.world.{ingest,package,geometry,...}  <-- awr.world.glb        GLB 适配器、标定、派生器（M22）
                                         <-- awr.world.mesh       MeshWorldQuery、NavMesh、锚点（M22）
awr.sim.{fleet,core,runtime,backends}    <-- awr.sim.events       EventRuntime 插件、磁带读写（M23）
awr.sim.targets（加 timeline/exogenous） <-- awr.sim.events
awr.sim.sensors（v1 不变）               <-- awr.sensorsvc        SensorBackend v2、相机、MeshLidar、调度（M25）
awr.sim.backends.base（协议）            <-- awr.gz               GzSimBackend（M24）
awr.agent、awr.tasking、awr.swarm        <-- awr.agentsdk         Env、观测过滤、链路模型、SDK（M26）
awr.recorder、awr.sandbox                <-- awr.bench            Run、队列、无损写入、数据集、评测（M27）
apps/web/src/{engine,viewport,ui}        <-- engine/actors、viewport/layers/worldMesh.tsx、ui/{event,bench,droneview}
packages/contracts（1.2.0）              --> 1.3.0：新目录 event/、bench/、sensor/v2，其余只增可选字段
```

依赖方向用一个新测试固定：扫描 `python/awr` 下 D2 包，断言不 import `awr.{world.glb,world.mesh,sim.events,sensorsvc,gz,agentsdk,bench}`（与 D2 现有的 M08 不 import 插件的规则同一写法）。

### 7.3 必须触及的 D2 文件（扩展点小改）

| # | 文件 | 改动 | 缺省路径不变的证明 | 阶段 |
|---|---|---|---|---|
| 1 | `sim/fleet/stages/budgets.py` | `ORDER_RANGES` 给 M23 分配 195–199（或从 M18 区段让出），`PACKAGE_OWNER` 加 D3 包前缀，`BUDGET_CORE` 加 D3 stage 行 | D2 组合根不登记 D3 stage，Σ预算与构建结果不变；`tests/sim` 构建用例 | P0 |
| 2 | `sim/targets/model.py` | `motion.mode` 枚举加 `timeline`；顶层可选字段 `reaction_policy ∈ {interactive, exogenous}` | 缺省 `interactive`；`tests/targets/test_schema.py`、`test_determinism.py` 逐字节 | P0 |
| 3 | `sim/targets/motion.py`、`runtime.py` | `timeline` 时位置等取自已登记的运动源，跳过 `step_motion` 与行为覆盖；`exogenous` 时 `override` 返回 `(None, True)` 且不广播 | 两个分支只在新取值下进入；M18 全部 93 个测试 | P0 |
| 4 | `packages/contracts/target/target.schema.json`、`rt/rng_streams.json` | 同上字段；新流 19–2x | 只增可选字段；`make test-contracts` | P0 |
| 5 | `sim/sensors/models.py` | `Raycast` 字面量加 `mesh`、`render` | 类型注解，运行期无影响 | P0 |
| 6 | `world/ingest/types.py`、`world/pointcloud/writer.py`、`world/package/build.py` | `RawCloud.rgb` 可选；有 rgb 时写 `col.rgb = srgb` | 七个世界无 rgb，`contentVersion` 不变（`make validate` 对拍） | P0 |
| 7 | `world/geometry/query.py` | `open_world_query` 在世界包有 `geometry.mesh` 图层且开关打开时返回 `MeshWorldQuery` | 七个世界无该图层；`tests/world_query` 105 个测试 | P0 |
| 8 | `packages/contracts/schemas/world/world.schema.json` | 图层 type 加 `navmesh`、`anchors`（或以 `format` 区分，不加 type） | 只增枚举 | P0 |
| 9 | `packages/contracts/sandbox/snapshot.schema.json` | 可选段 `event`、`agents` | `additionalProperties: false` 下只增可选属性 | P0 |
| 10 | `packages/contracts/rt/topics.json`、`rec/mcap_channels.json` | 新 topic 与新通道 | 只增；兴趣集与录制按需 | P0 |
| 11 | `recorder/replay.py` | 可选的"磁带演员源" | 未给磁带时不启用 | P0 |
| 12 | `api/sandbox_main.py`（或新 bench-api 入口） | 在开关后挂 D3 路由 | 公开档开关关闭 | P0 |
| 13 | `sim/fleet/stages/registry.py`、`sim/core/entities.py` | `register_entity_block` 的 kind 加 `actor`，容量按 kind | 只有 `target` 时行为不变 | P1 |
| 14 | `apps/web/src/viewport/layers/registry.ts` | `LayerId`、owner 联合类型增补 | 类型增补；`make lint-m06`、`m06.gpu-limits` | P1 |

不触及：`sim/runtime/batch.py`、`sim/runtime/main.py`、`sandbox/spawner.py` 的 `default_plugins()`、`recorder/app.py`/`writer.py`、`sim/sensors/lidar/scan_rt.py`、`sim/sensors/backends/base.py`、`sim/perception/**`、`sim/tasking/**`、`api/rt/gateway.py`、`tools/sdk/awr_sandbox.py`。

### 7.4 进程、部署与配置分层

- **组合根**：D3 插件（`awr.sim.events.plugin` 等）只出现在新的 `D3_PLUGINS` 与 `runtime.yaml` 的 `d3` profile 中；`sandbox/spawner.py` 的 `D1_PLUGINS + D2_PLUGINS` 与公开 profile 不变。
- **资源**：GPU 实验池（渲染、MeshLidar、Gazebo）与公开站 emax 分开部署；公开站继续运行 D2 的 S7 共享展示与 CPU 沙盒。实验池的进程管理代码复用 M17（spawn、cgroup、接管），但用独立的配置段与状态目录。
- **开关**：`AWR_D3_ENABLED`（缺省 0）控制 D3 路由、`open_world_query` 的网格分派与 D3 topic 的生产者。
- **数据**：D3 世界包（GLB 派生）放在新世界 id 下（如 `oryahovo-stage1`），不改七个 D2 世界的包与派生缓存。

### 7.5 契约与文档分层

- 契约 1.2.0 → 1.3.0：只增目录与可选字段（AWR-17 的 1.x 规则）；ADR 从 ADR-127 起编号；文档新增 `docs/05-D3-设计增补与决策记录.md` 与 `docs/modules/M22–M27`，D2 文档只在"对既有模块的影响"表中加行。
- 契约生成物（`packages/contracts/gen`）重新生成后，D2 前端只因类型增补而变，`make lint` 与类型检查必须通过。

### 7.6 回归门（D3 每个工作包合入前）

1. D2 Python 测试全集（492 个文件，`pytest -m "not perf"`）与前端单测、契约测试通过。
2. **零影响对拍**：在安装了 D3 包、`AWR_D3_ENABLED = 0` 与 `= 1 但不加载事件` 两种条件下，`relay_batch.py --case b --hours 1 --determinism` 的 `events.jsonl`、`relay_stats.json` 与 D2 基线逐字节一致；`--resim` 帧摘要序列一致；`tests/targets/test_determinism.py` 轨迹哈希一致。
3. D2 性能用例子集（flight60、fleet-ladder、targets/perception 预算用例）在同一机器上与基线差 ≤ 5%。
4. 前端 `make lint-m06`、`m06.gpu-limits`（新增图层后着色器与 UBO 上限）通过。
5. import 方向测试（§7.2）通过。

---

## 8. 对 D3 架构的建议：M22–M27 与 D2 的接合

### 8.1 分层总图

```text
                           浏览器（three.js 沙盘 + 多 Run 对比 + 无人机视角面板 + Rerun Viewer）
                                  ^ 实时 topic（只读观察，按 principal 作用域）
--------------------------------------------------------------------------------------------------
 Experiment Controller (M27)    Run 规格 = World(contentVersion) + Event(tape sha) + Fleet + Policy + Tier + Seed
   |                            队列 -> GPU 实验池（复用 M17 进程管理） -> 无损写入 -> 数据集 / .rrd -> 评测
   v
 Env (M26, lockstep)            reset/step；观测过滤 L0-L3；链路模型；动作 -> CommandEngine + TrustedGuard
   |                            （骨架 = D2 批处理：SimCore + LocalBus + LocalRing + BatchCtl 固定延迟）
   v
 sim-core（D2 内核，Fast 档） 或 GzSimBackend（M24，Realistic 档，实现 D2 SimBackend/DroneAdapter）
   |   stages：FleetSim(L1) / M09 安全 / M07 环境（磁带环境轨 -> EnvOp） / M18（timeline + exogenous，判据作度量）
   |   / M19（L1 Fast 档观测，经过滤） / M23 EventRuntime 插件（磁带、事件状态、通信区）
   v
 Sensor Service (M25)           SensorBackend v2：光栅相机（RGB/D/Seg）、MeshLidar、Gazebo 传感器、3DGS pass
   |                            输入 = 位姿窗口 + 磁带 DynamicSource + 环境快照；输出 = ObsFrame / TruthFrame
   v
 World Package (M22)            点云（彩色，D2 容器）+ visual/mesh + geometry/mesh + DSM/DTM 0.5 m + NavMesh + anchors
                                MeshWorldQuery（2.5D 方法 = 网格派生栅格；3D 方法 = 网格求交）
```

### 8.2 M23 接入 sim-core：磁带作为 M18 的运动源

```python
# awr/sim/events/plugin.py（M23，只在 d3 组合根中出现）
from awr.sim.fleet.stages import registry as R
from awr.sim.targets import plugin as m18

def install():
    R.register_session_loader("event", load_event, owner="M23", order=45)   # vehicles(40) 之后、targets(50) 之前
    R.register_query("event/status", query_status)
    R.register_slow_task("m23.publish", publish_event_status)                # 事件阶段、环境轨、通信区（1 Hz）
    R.register_batch_output("event_run", write_event_run)                    # 磁带哈希、演员与机队的相遇统计

def load_event(items, api):
    tape = Tape.open(items["tape"])                                          # 内容哈希校验；世界 contentVersion 校验
    m18.runtime().set_motion_source(TapeMotionSource(tape))                  # E4 新钩子
    for a in tape.scripted_actors():                                         # ≤ 30 名脚本演员进 M18 实体表
        api.submit("target/add", {"target": {"id": a.id, "class": a.cls, "pos": a.pos0,
                   "motion": {"mode": "timeline"}, "reaction_policy": tape.mode}})   # exogenous | interactive
    for op in compile_env_track(tape.environment):                           # 环境轨 -> M07 定时操作
        api.submit("env/set", op)
```

M18 stage 中的分支（E4，伪代码）：

```python
if cfg["motion"]["mode"] == "timeline":
    pos, yaw, vel, state = self.motion_source.pose(T.id, t_s)     # Track/hermite 插值（E3）
    T.ms.pos, T.ms.yaw, T.ms.speed = pos, yaw, norm(vel)           # 不调用 step_motion，不取 RNG
else:
    ov, base = T.override(...) if policy != "exogenous" else (None, True)
    step_motion(...)
# 判据与状态机照常：discovered 事件 = "演员感知到无人机"的度量（E2b）；exogenous 下不广播、不改运动
```

这样 M19、M20、前端识别物图层、TargetLite32、REST 都不需要知道磁带的存在；而 M25 的传感器服务直接从磁带取曝光时刻的演员状态（不经实体表，不受 stage 次序影响）。

### 8.3 MeshWorldQuery

```python
class MeshWorldQuery(DsmWorldQuery):            # awr/world/mesh/query.py
    """2.5D 方法继承（栅格来自网格派生的 0.5 m DSM / DTM 与派生缓存），3D 方法覆盖为网格求交。"""
    def los_batch(self, A, B, step_m=1.0, eps_m=0.5): ...      # 网格线段求交，两端裁 eps（与 D2 语义一致，≤ 64 对）
    def ray_hit(self, origin, direction, max_range_m=5000.0): ...
    def free_distance(self, origins, dirs, max_m): ...
    # D3 新增
    def raycast_batch(self, o, d, tmax): ...                    # -> (t, prim_id, inst_id)；大批量交给 M25 的 GPU 服务
    def walkable_height(self, xy, profile="person"): ...        # NavMesh 可行走面高度（演员贴地）
    def nav_path(self, a, b, profile="person"): ...             # 事件编译的几何预检
    def semantic_of(self, prim_id): ...                          # 地面 / 立面 / 高起物 / 标注类别（r3）
```

CPU 实现用 Open3D 或 Embree（r4：11–20 M 射线/s，与 GPU 结果逐射线一致 ≤ 0.3 mm），GPU 大批量在 M25。网格派生 DSM 的分辨率建议 0.5 m：E4 中它让 2.5D 方法的误差降到 3–4%，规划与安全（M09、M10、M20）仍按 2.5D 保守语义工作。

### 8.4 SensorBackend v2

见 `.cache/research/d3/r7-d2-audit/scripts/sensor_backend_v2.py`（`PoseWindow`、`SensorRequest`、`ObsFrame`、`TruthFrame`、`BackendCapsV2`、`DynamicSource`、`SensorBackendV2`、`V1Adapter`）。要点：

- 请求按（机体，传感器，帧序号）逐条给，时间戳为曝光中点（相机）或扫描中点（LiDAR），`seq` 作计数器 RNG 的键。
- 位姿窗口由 Env 从 FleetSim（或 Gazebo）的状态历史给出；LiDAR 逐点插值，相机可做运动模糊与卷帘。
- 动态世界由 `DynamicSource` 提供（磁带读取器；Interactive 模式下为 M18 实体表快照）。
- 输出分 `ObsFrame`（给 Agent，无真值 id）与 `TruthFrame`（实例、可见度、逐点实例，只进评测与数据集真值层）。
- 后端：`RasterCameraBackend`（moderngl/EGL，r3）、`MeshLidarBackend`（Warp，r4）、`GzSensorBackend`（r9）、`GsCameraBackend`（gsplat + 网格深度，r3/r10）；`V1Adapter` 让任何 v2 后端装到 D2 的 v1 调用点。

### 8.5 M26 Env 与 D2 批处理的关系

- 组装：照搬 `run_batch` 的 `SimCore(cfg, LocalBus, LocalRing)` + `BatchCtl`，主循环按 `SLICE` 推进；在每个决策周期（如 1–5 Hz）调用 Agent，把动作作为命令注入 `CommandEngine`，生效 tick = `request_tick + lat_ticks("agent")`（与 D2-FX-R2 修复 AC-32 的"固定延迟生效、未到即阻塞"同一规则），因此 LLM 类慢策略不会拖慢或扰动仿真。
- 观测：传感器服务的 `ObsFrame` + 本机状态（带 GNSS/IMU 噪声）+ 收到的消息；按观测档过滤（L1：M19 匿名化；L2：渲染与 LiDAR；L3：SITL）。
- 通信：链路模型在 Env 内（距离、遮挡用 MeshWorldQuery、丢包、时延、带宽桶、磁带干扰区），M14 的合同网与黑板作为一种协同策略运行在其上。
- 公平性：同一 Run 规格两次运行，CPU 档观测逐字节一致；GPU 档按容差（design-r1 §9）。

### 8.6 数据流与录制

- 运行期：sim-core 状态（D2 MCAP 通道原样）+ D3 新通道（演员、相机帧、深度、分割、LiDAR v2、Agent 动作与消息）→ 无损写入器 → `runs/<run>/`；事件磁带与世界清单作 MCAP 附件。
- 运行后：导出 `dataset/event-xxx/runs/<policy>/uav-xx/{rgb,depth,lidar,state.jsonl}`、`annotations/`（演员轨迹、实例、visibility、event_graph）、`evaluation/results.json`（r6 §11 的目录）与 `.rrd`（观测层、真值层分文件，r8）。
- 浏览器回放：replay-worker 读 MCAP，演员从磁带补出，多 Run 时间轴同步。

### 8.7 运行档位与 D2 组件的对应

| 档位 | 物理 | 传感器 | 观测档 | D2 复用 |
|---|---|---|---|---|
| Fast | FleetSim | M19（L1，匿名化）+ MeshWorldQuery 视线 | L1 | 全部 D2 sim-core 组件 |
| Mesh | FleetSim | 光栅相机 + MeshLidar（GPU） | L2 | sim-core + v2 后端 |
| Realistic | Gazebo（GzSimBackend） | 同一 M25 服务（缺省）；gz 原生传感器只作交叉验证（r5：gz 每次传感器更新约 11 ms，EGL 光栅化 1.4–1.6 ms） | L2 | 协议、Env、磁带、录制；`ClockCaps.mode` 的 lockstep / free_running / slaved_realtime 三值直接对应 r5 的三种同步方式 |
| SITL | Gazebo + PX4 | 同上 | L3 | `SessionSpec.backends[]` 信封、PX4 SIH 经验 |
| Photoreal | 任一 | 3DGS 背景 + 网格前景（C3） | L2+ | 同上 |

---

## 9. 风险与待决问题

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | D2 尚未通过验收（第 1 轮 26/55，D2-INT 第 2 轮进行中），D3 起点可能移动 | D3 扩展点小改与 D2 修复冲突 | D2 打标签后再开 D3 扩展点工作包；D2 修复只在维护分支，定期合入 |
| 2 | D2 的感知与识别物 stage 曾超预算 4–20 倍，沙盒组合根仍约 0.1 核/仿真秒 | D3 再加负载会挤占 Σ预算 0.40 | 重计算全部放在 sim-core 之外的 GPU 服务；D3 插件在 sim-core 内只做磁带插值与状态发布（每 tick 微秒级） |
| 3 | 真值泄漏有多条通道（M19 帧、航迹、LiDAR tag、沙盒 REST、WS 全局 topic、M20 快照、SimBridge 全量状态） | Benchmark 结果失真 | 观测只经 M26 过滤器出口；加"泄漏红队"测试：对 Agent 收到的全部字节搜索演员 id、真值坐标；正式测试集用服务端运行的提交容器 |
| 4 | 计数器 RNG 以 tick 为键 | Fast 与 Gazebo、不同时钟档之间不可比 | D3 新流不用 tick；跨档比较只比磁带与评测，不比噪声逐值 |
| 5 | GLB 尺度与配准不确定（±20%，r3） | 事件速度、像素尺寸、通信距离 | M22 强制标定步与 `scaleStatus`；未标定世界禁止加载事件 |
| 6 | 摄影测量网格的实心树冠、缺失立面 | 可见度偏保守、LiDAR 无穿透 | `semantic_of` 标注高起物类别；评测报告分层统计 |
| 7 | 前端纹理网格图层受 M06 规则约束（UBO、MRT、着色器动物园） | 工作量与性能不确定 | P0 用彩色点云；网格图层放 P2 并先做 `m06.gpu-limits` 预检 |
| 8 | 工作量估计 ±40% | 计划偏差 | 按 P0 关键路径排期，原型复用项先做 |
| 9 | 待决：M23 的 order 区段（195–199 还是从 M18 区段让出）、`navmesh`/`anchors` 是加图层 type 还是只加 format | 契约 | 在 D3 扩展点工作包的 ADR 中裁决 |
| 10 | 待决：Interactive 模式的"反事实"口径（同初值、不同闭环） | 论文叙事与评测 | 按 design-r1 §5.2：只比较初始条件相同的闭环结果，不声称事件相同 |

---

## 10. 复现与文件清单

| 文件 | 内容 |
|---|---|
| `.cache/research/d3/r7-d2-audit/scripts/e1_static_audit.py` | 静态审计（ast），输出 `results/e1_static_audit.json` |
| `.cache/research/d3/r7-d2-audit/results/e1_loc_classes.json` | 复用分类与行数 |
| `.cache/research/d3/r7-d2-audit/scripts/e2_exogenous.py` | 反应 vs 固定事件、Exogenous、时钟档 RNG、path 时刻；输出 `results/e2/e2_summary.json` 与 10 份轨迹 npz |
| `.cache/research/d3/r7-d2-audit/scripts/e3_tape_interp.py` | D2 Track/hermite 播放磁带误差；`results/e3_tape_interp.json` |
| `.cache/research/d3/r7-d2-audit/scripts/e4_mesh_worldquery.py` | DSM vs 网格视线、MeshWorldQuery 垫片接入 M18；`results/e4/e4_summary.json` |
| `.cache/research/d3/r7-d2-audit/scripts/sensor_backend_v2.py` | SensorBackend v2 协议草案与 V1Adapter（可复用实现） |
| `.cache/research/d3/r7-d2-audit/scripts/e5_mesh_lidar_backend.py` | v2 原型 MeshLidarBackend（复用 D2 花样、ray_aabb、cbrng、lidar_frame.v1）；`results/e5_mesh_lidar_backend.json` |
| `.cache/research/d3/r7-d2-audit/scripts/workload.py` | 改造清单与工作量汇总；`results/workload.json`、`results/workload_table.md` |
| `.cache/research/d3/r7-d2-audit/pylib/` | embreex 的符号链接（指向 r2 单元 venv），供 E4、E5 使用 |

全部实验在本机完成（CPU，单次 < 5 分钟）；未使用 dmax，未创建容器与镜像，未传输数据。
