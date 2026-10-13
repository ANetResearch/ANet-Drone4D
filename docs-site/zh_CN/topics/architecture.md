# 架构与坐标系

## 总体结构

服务器负责"算世界"：物理、安全、任务、感知与环境都在无 GPU 的 Linux 主机上运行；浏览器负责"看世界"：流式加载点云并绘制无人机、轨迹、
区域、视锥与天气。所有进程由 supervisor 监管（心跳、退避重启与熔断），经 zenoh 总线交换命令、事件与查询，经共享内存状态环交换高频状态。

```text
World Package（八叉树、DSM 与 DTM、区域与语义）
  │
  ↓
服务器（无 GPU）
  sim-core：250 Hz 时钟；FleetSim、Safety、Mission、Environment、Sensors
    ├──→ StateRing（共享内存，125 Hz）──→ recorder、replay-worker（MCAP）
    └──→ zenoh 总线（命令、事件、查询）←──→ agent-runtime
  api / Gateway：读取 StateRing（60 Hz），提供 REST、静态文件与 awr.rt.v1，经总线下发命令
  sandbox-pool ──→ 每个会话一个 sim-core（sandbox100）
  sandbox-api：每个会话一个 Gateway，沿用同一套接口与协议
  │
  ├── HTTP Range：点云与世界文件
  └── WebSocket：状态流与命令
  ↓
浏览器
  PointCloudEngine（APH 选择器、CAS 控制器）──→ 视口与渲染后端（WebGL2）
  rt.worker（awr.rt.v1 客户端）──→ 视口与 UI 壳（React 19、shadcn）
```

| 进程 | 职责 |
|---|---|
| sim-core | 仿真内核：FleetSim 机群、飞行状态机与安全、任务执行、环境场、传感器与感知；按登记的 pipeline stage 逐 tick 运行，并把状态发布到 StateRing |
| api（show-api） | Gateway：REST 接口、前端与世界静态文件、`awr.rt.v1` 实时连接；按每个客户端的兴趣集与配额下发状态 |
| recorder、replay-worker | MCAP 录制与回放（回放帧与录制逐字节一致） |
| agent-runtime | 智能体运行时（V0.1 为进程内 Mock ANet），经总线与 sim-core 协作 |
| job-worker | 后台作业，例如世界构建与 Mock 重建 |
| sandbox-pool | 沙盒会话的准入、排队、驻留与回收；治理各会话的倍速；运行共享规划服务 |
| sandbox-api | 按会话实例化 Gateway，对外沿用同一套接口与实时协议 |

## 一次交互的数据流

1. **打开。** 访问 `/world/<id>` 时加载世界清单与八叉树层级；每个八叉树根一次 HTTP Range 请求取回首屏层级，Web Worker 把 ANET_Q16 点解包进 GPU 点池。
2. **流式。** 每一帧由 APH 选择器在点预算内挑选八叉树节点；CAS 控制器根据实测帧时间调整预算，并在 7 档画质之间换档。
3. **仿真。** sim-core 以 4 ms 为一个 tick，用一个 numba 内核推进全部无人机（L1 控制每两个 tick 执行一次），把安全、任务、环境与传感器作为
   已登记的 stage 运行，并把状态发布到 StateRing。
4. **服务。** Gateway 以 60 Hz 读取状态环，按客户端兴趣集与配额发送二进制数据：整个机群使用 32 字节的 Lite32 记录，焦点无人机使用 64 字节的 Full64 记录。
5. **绘制。** 浏览器在唯一的渲染时刻上插值，在点云之上绘制无人机、轨迹、区域、视锥与天气。
6. **操作。** 点击地面在 DSM 上执行一次射线求交查询；飞往命令经过准入（租约、参数、能力、围栏），依次返回受理、执行中与成功。

## 时钟

| 量 | 共享展示（d1_250） | 沙盒（sandbox100） |
|---|---|---|
| 主时钟 | 250 Hz（4 ms） | 100 Hz（10 ms） |
| 多旋翼控制与物理 | 125 Hz | 100 Hz |
| 固定翼与垂起运动学 | — | 50 Hz |
| 环境与快速守卫 | 50 Hz | 50 Hz |
| 状态环发布 | 125 Hz | 50 Hz |
| 识别物运动与敏感判定 | — | 10 Hz |
| 感知 | — | 5 Hz |
| 编排与接力 | — | 1 Hz（重计算提交共享规划服务） |

时间轴在仿真时钟上运行：暂停、单步、倍速 ×0.25 至 ×10、录制与回放。时间在线上与存储中一律为 int64 `t_sim_ns`（从运行起点开始计），墙钟时间
`t_wall_ns` 只用于显示与审计；间隔计算只使用单调时钟。

## 坐标系

`world` 是全系统唯一的度量帧：锚点处的 ENU 切平面，X 向东、Y 向北、Z 向上，单位米，右手系。几何、物理、规划、环境场、无人机状态与实时载荷都使用它。

| 帧 | 定义 | 出现位置 |
|---|---|---|
| `world` | 锚点处 ENU（米，右手，+Z 上） | 全部度量计算与接口载荷，例如 `pos_enu_m`、`home_enu_m`、`vel_enu_mps` |
| `earth` | ECEF（WGS84） | `coordinate.json` 的 `T_ecef_world`（派生，只用于校验与导出，禁止上 GPU） |
| `source` | 数据集原始帧 | `coordinate.json` 的 `source.T_world_source` |
| `uavNN/base_link` | 机体 FLU（前、左、上；REP-103） | 姿态、角速度、传感器挂载，例如 `omega_flu_rad_s` |
| `uavNN/<sensor>_optical` | 相机光学帧 RDF | 传感器仿真与内参投影 |
| `three` | three.js 渲染帧，Y 向上 | 只在浏览器渲染层；映射为 `(x, y, z) = (E, U, −N)` |
| `ned`、`frd` | PX4 的 NED 与 FRD | 只在 PX4 适配器边界（FleetSim 内部为唯一例外，在发布时转换） |
| `uavNN/local` | 外部飞控的局部 NED 帧，原点为该机 EKF 原点 | PX4 与 Prometheus 适配器边界；`T_world_local` 写入 `state_ext.frames` |

约定：

| 项 | 约定 |
|---|---|
| 矩阵 | `T_<to>_<from>` 满足 `p_to = T · [p_from; 1]`；JSON 中为嵌套行主序 4×4 |
| 四元数 | `[x, y, z, w]`（Hamilton，单位范数），语义为 WORLD←BODY(FLU)，字段如 `q_xyzw` |
| 偏航 | 线上 `yaw_rad` 为 ENU 偏航角：东为 0，逆时针为正，单位弧度 |
| 航向显示 | 度，北为 0，顺时针：`heading_deg = (90° − ψ_enu) mod 360` |
| 风向 | 气象惯例：风的来向 |
| 单位后缀 | 字段名带单位后缀：`_m`、`_mps`、`_rad`、`_ns`、`_ms`（`_ms` 只表示毫秒）等 |
| 精度 | GPU 上只使用相对坐标；世界坐标禁止使用 float16 |

**锚点与经纬度。** 真实采集数据以 RTK 或测量控制点为原点。合成与数据集世界（包括合成演示城市与 UrbanScene3D 六城）的原点取包围盒中心与
地面中位高度，`anchor.kind = "synthetic"`，由地标反推的经纬度只作示意：用于太阳位置、天空与界面显示，不得用于真实导航，界面标注为"示意坐标"。
北向的可信度记录在 `trueNorth.confidence` 中（`exact`、`verified`、`assumed` 或 `unknown`），为 `unknown` 时界面不显示指北针。

换算公式只有唯一实现：Python 为 `awr/world/georef/frames.py` 与 `awr/environment/conventions.py`，TypeScript 为
`apps/web/src/engine/geo/frames.ts` 与 `engine/environment/state/conventions.ts`，两端以 golden 用例对拍。

## 契约

接口与文件格式以契约为先：`packages/contracts` 中的 75 份 JSON Schema 生成 Python 与 TypeScript 类型，二进制布局、原因码与单位字典也在此登记，
golden 文件约束两端一致。REST 接口的 OpenAPI 快照（`packages/contracts/rest/*.snapshot.json`）同样由契约工具生成，本文档的端点表即取自这些快照。
凡是改变契约的变更（文件格式、`awr.rt.v1`、REST 接口、原因码）先在设计文档中规定，再修改代码。
