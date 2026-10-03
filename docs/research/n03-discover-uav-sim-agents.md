# n03 研究笔记：2025–2026 无人机仿真 / 集群 / 具身多智能体新项目发现（Discovery）

> 研究单元：n03 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §19–20（风场分级）、§26–30（Drone Simulation / 状态 / 多机 / 控制模式）、§31–32（Agent Network）、§35（Simulation Backend）、§37（频率）、§43–50（MVP 与路线）
> 方法：WebSearch 多轮（中英文）发现候选，GitHub API（`stargazers_count`、`created_at`）+ `commits/<branch>.atom` 实测 star 与**最近提交时间**（不是 pushed_at）。精读 6 个仓库，shallow clone 到 `refs/discovery/`：`rotorpy`、`crazyflow`、`PegasusSimulator`、`AerialClaw`、`droneserver`、`CBBA-Python`。另有 2 个仓库只抓了关键文件到 `.cache/research/n03/`：gym-pybullet-drones 的 `BaseAviary.py`、`DSLPIDControl.py`，mavsdk_drone_show 的 `gcs-server/sar/coverage_planner.py`。PX4 SIH 用的是已有的 `refs/sim/PX4-Autopilot`。
> 本机实测：numpy 向量化的 **L2 机群动力学原型**，由 RotorPy 气动力矩 + Dryden 阵风 + PX4 式级联 + gym-pybullet-drones 下洗流移植而成。脚本 `.cache/research/n03/fleet_l2_bench.py`，结果见 §3.10。
> 相关单元：**r20**（PX4 SIH / mock L1 / HIL 端口规则，本文不重复）、r22（多机 SITL）、r26（集群控制器）、r27（实时协议）、**d05**（ANet 能力委派 / 合同网）。本文定位是**补位**：r20 解决"真飞控怎么跑"，本文解决以下四件事：
> 1. mock 物理如何与环境场 E(x,y,z,t) 耦合；
> 2. 机群步进架构怎么搭；
> 3. LLM Agent 如何安全地指挥无人机；
> 4. 多机任务分配用什么算法。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **spencerfolk/rotorpy** 311 stars · 2026-09-07 | 纯 Python 多旋翼 6DOF。有集总气动力矩（旋翼阻力 H 力、诱导入流、平动升力、挥舞力矩），有 Dryden 阵风、SE3 控制器、MinSnap、IMU/Mocap/相机传感器；v3 可经 MAVLink HIL 给 PX4 当物理引擎 | **port**：气动力矩 + Dryden + SE3 向量化后做成 mock **L2**（环境耦合层）；HIL 客户端参考 `px4_multirotor.py` | V0.3（L2 视觉联动）/ V0.4（风力进动力学）/ V0.4–V0.6（HIL） | 5/5 |
| **learnsyslab/crazyflow** 180 stars · 2026-09-26 | JAX 批量可微仿真。状态按 `(n_worlds, n_drones, ·)` 以 SoA 组织，**step pipeline 由命名阶段组成、可插拔**，有系统辨识出的 `so_rpy` 模型（含 `hb_x500` 参数），有 splat/viser web viewer | **port（架构）**：机群 `FleetSim` 采用 SoA + 命名 pipeline；`so_rpy` 作为 L1.5 廉价姿态模型 | MVP / V0.4 | 4/5 |
| **PegasusSimulator** 887 stars · 2026-07-24 | Isaac Sim 上的多旋翼框架。`Backend` 抽象（PX4 MAVLink / ArduPilot / ROS2 / Python），另有 PX4 lockstep HIL 实现和 IMU/GPS/Baro/Mag 噪声模型 | **port（接口）** `Backend` 协议 + 传感器噪声参数；**reference** Isaac 部分（需要 GPU） | V0.4（HIL）/ V0.5+（GPU 节点） | 4/5 |
| **XDEI-Group/AerialClaw** 132 stars · 2026-07-08（2026-03 新建） | LLM 空中智能体框架：brain–skill–runtime、硬/软技能、单步闭环 AgentLoop、安全包线配置、三级集群（commander/coordinator/executor）、设备协议、mock 适配器 | **port（语义）**：Skill 元数据 schema、单步决策 JSON、审批分级、三级任务分解 | V1.0（Agent Runtime） | 4/5 |
| **PeterJBurke/droneserver** 7 stars · 2026-09-20 | MCP 服务器，**把 LLM 视为不可信指挥官**：11 步守卫管线、4 级工具分级、一次性确认令牌、服务端地理围栏（速度指令会前推投影）、只追加审计 | **port**：守卫管线 + Tier 表 + 令牌，做成我方 Agent API / MCP 层的安全中间件 | V0.6（命令网关）/ V1.0（MCP） | 4/5（star 少，但设计最扎实） |
| **zehuilu/CBBA-Python** 130 stars · 2021-04-22 | CBBA 的 Python 实现，支持时间窗和异构兼容矩阵 | **port**：算法骨架（要修正打分以满足 DMG），作为 ANet 断网 / 去中心化场景的分配器 | V1.0 | 3/5 |
| learnsyslab/gym-pybullet-drones 2146 stars · 2026-09-06 | PyBullet 多机 RL 环境 | **port（公式）**：地效、下洗流、阻力三个经验模型 | V0.6 | 3/5 |
| alireza787b/mavsdk_drone_show 325 stars · 2026-09-25 | FastAPI + React 地面站，管理 PX4 SITL 机群，含 QuickScout SAR 覆盖规划、agent_runtime/MCP | **port**：`BoustrophedonPlanner`（多机分区覆盖）；**reference**：地面站运维流程 | V0.6 | 3/5 |
| PX4 SIH / Hawkeye（refs 已有 / PX4/Hawkeye 84 stars） | 真飞控 + 无 GPU 物理 / 原生 3D 回放器 | **adopt**（见 r20）/ **reference**（ULog 多机回放 UI） | V0.2 / V0.6 | 5/5 / 2/5 |
| isaac-sim/IsaacLab 8240 stars · 2026-09-28 | v2.3.2 起 contrib 新增 `Multirotor` + thruster 执行器 + ARL 无人机任务 | **reference**（GPU 节点的 RL 路线） | V1.x | 3/5 |
| Genesis-Embodied-AI/genesis-world 29992 stars · 2026-09-27 | 通用物理 AI 引擎，带 drone 例程（hover_env） | **reference** | V1.x | 2/5 |
| ntnu-arl/aerial_gym_simulator 776 stars · 2026-06-28 | Isaac Gym 上的 GPU 并行多旋翼，Warp 光线投射 LiDAR | **reference**（依赖 GPU） | V1.x | 2/5 |
| robotmcp/ros-mcp-server 1479 stars · 2026-09-27 | rosbridge 型 MCP，接任意 ROS/ROS2 机器人 | **reference**：走 ROS2 链路时的 LLM 接入 | V1.x | 3/5 |
| Cosys-Lab/Cosys-AirSim 433 stars · 2026-09-16（UE 5.8） | 仍在维护的 AirSim 分支 | **reference**：GPU 节点上替代 AirSim 做传感器仿真 | V1.x | 2/5 |
| SHAILAB-IPEC/OpenFly-Platform 369 stars · 2026-01-13 | 空中 VLN 工具链（点云→语义→轨迹→指令） | **reference**：Agent 评测数据的生成思路 | V1.x | 2/5 |
| OmniDrones 583 stars / Flightmare 1425 stars / Agilicious 647 stars | Isaac Sim 4.1 RL / Unity 渲染 / 敏捷飞行 | **skip**（维护者自述难以维护，或 2023 年后无提交） | — | 1/5 |

**实现者先看这 10 条：**

1. **机群物理分层的补位。** r20 的 mock L1 解决"像 PX4 一样飞"。本文的 L2 解决"风、阵风、下洗流、地效真实地作用在机体上"：把 RotorPy 的 `compute_body_wrench` 向量化后，**作为 FleetSim step pipeline 中的一个 stage 插在 L1 控制律之后**，不需要另写一套控制器。
2. **风的正确建模是"均值场 + 每机湍流滤波器"，不是一个 4D 湍流场。** RotorPy 的 Dryden（`dryden_utils.py: GustModelBase`）是二阶 Tustin 离散滤波器，每架机各有一份状态。E(x,y,z,t) 只需返回**均值风 + 湍流谱参数 (σ, L)**。这一条直接修正 01-design §18–20。
3. **时钟由 World 统一步进。** Crazyflow 的 `step_pipeline`（OrderedDict，命名阶段，`insert_fn_before("integration", fn)`）可以直接作为 FleetSim 的骨架。风、下洗流、地效、碰撞、传感器、故障注入都是一个 stage，**所有机体在同一时钟下步进**，这正好避开了 r20 实测的 SIH 多进程时钟漂移。
4. **PX4 HIL 桥有两份可对照的参考实现。** RotorPy `PX4Multirotor.step`（约 40 行）和 Pegasus `PX4MavlinkBackend`（lockstep）。消息集为 `HIL_SENSOR`（位掩码 ACCEL=7 | GYRO=56 | MAG=448 | BARO=6656）、`HIL_GPS`、`HIL_STATE_QUATERNION`，回包为 `HIL_ACTUATOR_CONTROLS`（`controls[i]∈[0,1]` × ω_max）。
5. **坐标系要一次说清。** 四套约定同时存在：RotorPy 用 ENU/FLU、四元数 `[x,y,z,w]`；PX4/MAVLink 用 NED/FRD；Three.js 是 Y-up 右手系；AirSim 是 NED 且 z 取负为高。AerialClaw 的系统提示词里花了整整一节讲"z 越负越高"的常见错误。转换公式见 §3.6，DroneState 必须写明所用约定。
6. **LLM 是不可信指挥官。** droneserver 的守卫顺序是：authenticate → state → tier → authorize → rate-limit → confirm-token → bounds → geofence → preconditions → execute → audit。被拒绝时返回可读的 `status="rejected"` 并附 `remedy`，不抛异常。无论哪个 LLM、经由 MCP 还是 ANet 下发，都统一走这一层。
7. **Agent 单步闭环。** AerialClaw 的 `AgentLoop` 每轮只决定下一步，输出 `{thinking, decision: act|done|stuck, action:{skill, robot, parameters}, reflection, goal_progress}`，并在提示词里注入"连续 3 次失败 / 连续重复同一技能"的纠偏提示。**LLM 延迟是秒级**，只能做任务层决策，不能进控制环。
8. **任务分配分三档。** V0.6 用集中式：打分函数统一，用 Hungarian 或顺序单物品拍卖。V1.0 在 ANet 上用合同网（d05：find→quote→score→delegate）。断网或去中心化时用 CBBA。**三者共用一个打分函数**：`value·e^{-λ·t_start}`，再加兼容矩阵、时间窗、电量可行性约束。
9. **CBBA-Python 的打分不是真正的边际增益。** `scoring_compute_score` 只计算插入任务本身的折扣收益，没计入后续任务被推迟造成的损失，违反 DMG（diminishing marginal gain），动态重规划时可能不收敛。移植时要改成 `Δ = S(path ⊕ j) − S(path)`（§3.9）。
10. **多机覆盖直接移植 QuickScout。** `BoustrophedonPlanner` 的流程是：ENU 投影 → 最长边定扫描角 → 按 `spacing = sweep_width·(1−overlap)` 生成扫描线 → 蛇形连接 → 按长度等分 → 就近分配。把"就近贪心"换成 Hungarian 即可用于 V0.6 的 Area Coverage / Search。

---

## 1. 仓库概览

实测时间 2026-09-28。star 来自 GitHub API；**最近提交**取默认分支 `commits.atom` 第一条的 `<updated>`；"新建"来自 `created_at`。

| 仓库 | stars | 最近提交 | 新建 | 语言 / 依赖 | GPU | 结论 |
|---|---|---|---|---|---|---|
| isaac-sim/IsaacLab | 8240 | 2026-09-28 | 2022-11 | Python / Isaac Sim | 必需 | reference |
| Genesis-Embodied-AI/genesis-world（原 Genesis） | 29992 | 2026-09-27 | 2023-10 | Python / Taichi | 推荐（可以 CPU） | reference |
| learnsyslab/gym-pybullet-drones | 2146 | 2026-09-06 | 2020-08 | Python / PyBullet | 否 | port 公式 |
| uzh-rpg/flightmare | 1425 | 2023-05-15 | 2020-07 | C++ / Unity | 是 | skip |
| rl-tools/rl-tools | 1036 | pushed 2026-09-16 | 2023-11 | C++ header-only | 否 | reference（RAPTOR） |
| PegasusSimulator/PegasusSimulator | 887 | 2026-07-24 | 2023-02 | Python / Isaac Sim 5.1 | 必需 | port 接口 |
| ntnu-arl/aerial_gym_simulator | 776 | 2026-06-28 | 2023-04 | Python / Isaac Gym | 必需 | reference |
| uzh-rpg/agilicious | 647 | 2023-03-07 | 2022-06 | C++ | — | skip |
| ctu-mrs/mrs_uav_system（refs 已有） | 640 | pushed 2026-08-14 | 2020-05 | ROS2 | 否 | 见 r24 |
| btx0424/OmniDrones | 583 | 2026-01-20 | 2023-02 | Isaac Sim 4.1 | 必需 | skip |
| Hub-Tian/UAVs_Meet_LLMs | 509 | 2025-03-26 | 2024-12 | 综述清单 | — | reference |
| Cosys-Lab/Cosys-AirSim | 433 | 2026-09-16 | 2022-08 | C++ / UE5.8 | 必需 | reference |
| aerostack2/aerostack2 | 390 | 2026-09-16 | 2022-12 | ROS2 C++ | 否 | reference |
| SHAILAB-IPEC/OpenFly-Platform | 369 | 2026-01-13 | 2025-02 | Python / UE / 3DGS | 必需 | reference |
| alireza787b/mavsdk_drone_show | 325 | 2026-09-25 | 2023-05 | FastAPI + React + PX4 SITL | 否 | port 覆盖规划 |
| **spencerfolk/rotorpy** | 311 | 2026-09-07 | 2023-03 | Python / numpy / scipy（可选 torch） | 否 | **port** |
| IMRCLab/crazyswarm2 | 259 | 2026-09-11 | 2021-11 | ROS2 | 否 | reference |
| Zhehui-Huang/quad-swarm-rl | 239 | 2025-10-21 | 2020-12 | Python | 推荐 | reference |
| **learnsyslab/crazyflow** | 180 | 2026-09-26 | 2024-11 | Python ≥3.11 / JAX / MuJoCo-MJX | 否（GPU 更快） | **port 架构** |
| nubot-nudt/dynamic_task_allocation | 164 | pushed 2024-07-15 | 2018-12 | ROS1 | 否 | skip |
| gtfactslab/CrazySim | 154 | 2026-04-03 | 2023-10 | Gazebo | 否 | skip |
| **XDEI-Group/AerialClaw** | 132 | 2026-07-08 | **2026-03** | Python / Flask-SocketIO / React | 否 | **port 语义** |
| **zehuilu/CBBA-Python** | 130 | 2021-04-22 | 2021-02 | Python / numpy | 否 | **port 算法** |
| typefly/TypeFly | 117 | 2026-07-08 | 2023-07 | Python | 否 | reference |
| learnsyslab/lsy_drone_racing | 88 | 2026-09-27 | 2024-04 | Python | 否 | skip |
| PX4/Hawkeye | 84 | pushed 2026-09-20 | **2026-03** | C | 否 | reference |
| tau-intelligence/MuJoCo-drones-gym | 54 | 2026-08-25 | **2026-05** | Python / MJX | 可选 | reference |
| keep9oing/consensus-based-bundle-algorithm | 45 | 2022-11-25 | 2022-06 | Python | 否 | reference |
| learnsyslab/swarmGPT | 32 | 2026-08-10 | 2025-10 | Python / JAX | 可选 | reference |
| earth-insights/awesome-uav-vln | 29 | 2026-05-30 | 2026-05 | 清单 | — | reference |
| ion-g-ion/MAVLinkMCP | 24 | 2026-08-31 | 2025-04 | Python / MAVSDK / MCP | 否 | reference |
| rl-tools/l2f | 22 | 2025-10-30 | 2024-07 | C++ | 否 | reference |
| alireza787b/dronesphere | 14 | 2025-09-30 | 2025-06 | Python / MCP | 否 | skip |
| **PeterJBurke/droneserver** | 7 | 2026-09-20 | 2025-11 | Python / FastMCP / MAVSDK | 否 | **port 安全层** |
| deepak61296/mavlink-mcp | 4 | 2026-09-25 | 2026-07 | Python / ArduPilot | 否 | skip |
| naver-ai/DroneCATS | 2 | 2026-08-31 | 2026-08 | 评测基准 | — | reference |
| Yookio-Z/Airsim-Agent / notfeylo/propwash / MavedNahrub/UAV_SWARM_DRONES | 0 | 2026-09 | 2026-08/09 | — | — | skip（太新、无社区） |
| bdathe-lb/CC-OPI-Exp（论文 2609.19208 所给链接） | — | API 404 | — | — | — | 未知，无法评估 |

**观察。** 2025–2026 的新项目集中在两类。

- **LLM/MCP 驱动无人机**：AerialClaw、droneserver、MAVLinkMCP、mavlink-mcp、dronesphere、mavsdk_drone_show 的 agent_runtime、ros-mcp-server。star 普遍不多，但设计在快速收敛到"技能注册 + 安全守卫 + MCP"。
- **GPU/JAX 批量物理**：Crazyflow、MuJoCo-drones-gym、Isaac Lab contrib multirotor。

高 star 的老牌仿真器（Flightmare、Agilicious、OmniDrones）已经停滞。

---

## 2. 源码结构与关键模块

### 2.1 RotorPy（`refs/discovery/rotorpy`，v3.0.0）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `rotorpy/vehicles/multirotor.py` | `Multirotor.step` / `_s_dot_fn` / `compute_body_wrench` / `get_cmd_motor_speeds` / `_handle_vehicle_on_ground` | 状态为 `[x, v, q(xyzw), w, wind, rotor_speeds]`，用 `scipy.integrate.solve_ivp`（RK45）积分。支持 7 种控制抽象：`cmd_motor_speeds / cmd_motor_thrusts / cmd_ctbm / cmd_ctbr / cmd_ctatt / cmd_vel / cmd_acc`，高层指令在内部经 SE3 类律转换成转速。电机为一阶惯性 `ω̇=(ω_cmd−ω)/τ_m` |
| 同上 `BatchedMultirotor(Params)` | torch 批量版 | `rotor_drag_matrix`、`drag_matrix` 等以 `(N,3,3)` 存储 |
| `rotorpy/controllers/quadrotor_control.py` | `SE3Control.update`、`BatchedSE3Control` | Lee 几何控制。默认增益 `kp_pos=[6.5,6.5,15]`，`kd_pos=[4,4,9]`，`kp_att=310`（Hummingbird 为 544），`kd_att=57` |
| `rotorpy/wind/dryden_utils.py` | `GustModelBase.run`、`DrydenWind`、`BatchedDrydenWind` | MIL-F-8785C Dryden，二阶 Tustin 离散（§3.2） |
| `rotorpy/wind/wind_template.py`、`spatial_winds.py` | `update(t, position) -> wind[3]` | 风场接口恰好就是 `environment.query(x,y,z,t)` 的形状；`WindTunnel` 是空间风场示例 |
| `rotorpy/vehicles/px4_multirotor.py` | `PX4Multirotor.step`、`_send_hil_sensor`、`_send_hil_state_quaternion`、`_fetch_latest_px4_control`、`enu_to_geodetic` | PX4 HIL 客户端：`mavlink_url="tcpin:localhost:4560"`，lockstep 时阻塞等待 `HIL_ACTUATOR_CONTROLS`；`FLU→FRD` 取 `(x,−y,−z)` |
| `rotorpy/vehicles/px4_sihsim_quadx_params.py` | quad_params | 与 PX4 `10040_sihsim_quadx` 对齐：`mass=1.0`，`k_eta = SIH_T_MAX/ω_max²`，`tau_m=SIH_T_TAU=0.05` |
| `rotorpy/controllers/raptor.py` | `RaptorFoundationPolicy` | RAPTOR 基础策略（pip `foundation-policy`），与 PX4 `mc_raptor` 同源 |
| `rotorpy/trajectories/minsnap.py` | `MinSnap`、`BatchedMinSnap` | 用 cvxopt QP 求 7 阶多项式 min-snap |
| `rotorpy/world.py`、`sensors/{imu,camera,range_sensors,external_mocap}.py` | `World.closest_points / path_collisions`、`PinholeCamera` | 世界为 AABB 块；相机把"世界特征点"做针孔投影（非真实感渲染） |
| `rotorpy/simulate.py` | `simulate` / `simulate_batch` / `safety_exit` | 固定顺序：wind → controller → vehicle.step → sensors → exit 判定 |

**注意。** 单机 `solve_ivp` 每步都要重新构造闭包，Python 开销很大。要支撑数十到上百架实时，必须像 §3.10 那样改写成 SoA 固定步长。

### 2.2 Crazyflow（`refs/discovery/crazyflow`）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `crazyflow/sim/data.py` | `SimState`、`SimStateDeriv`、`SimControls` | 全部是 `(N_worlds, M_drones, k)` 的 SoA 张量（flax dataclass），带 `force/torque` 外力槽位供插件写入 |
| `crazyflow/sim/sim.py` | `Sim.__init__`（`freq=500`，`state_freq=100`，`attitude_freq=500`）、`build_step_fn`、`build_control_fns`、`select_dynamics_fn` | 控制按频率分级执行（staged_cmd / cmd / steps），step 用 `jax.lax.scan` 融合多步 |
| `crazyflow/sim/pipeline.py` | `append_fn / insert_fn_before / insert_fn_after / replace_fn / remove_fn` | **有序命名管线**，名称唯一，可按锚点插入 |
| `crazyflow/dynamics/{first_principles,so_rpy,so_rpy_rotor,so_rpy_rotor_drag}` | `dynamics`、`dynamics_euler`、`symbolic_dynamics`（CasADi） | `so_rpy` 为辨识得到的二阶 RPY 模型（§3.5）。`params.toml` 含 `[hb_x500]`：mass 2.28、thrust_max 12.13 N/电机 |
| `crazyflow/sim/integration.py` | `euler / rk4 / symplectic_euler` | 默认 euler，可选 rk4 |
| `examples/plugins/{disturbance,ground_effect,action_delay,randomize,estimation}.py` | `disturbance_fn(data)` | 插件是纯函数 `SimData -> SimData`，写入 `states.force/torque` |
| `crazyflow/sim/splat.py`、`sensors/{splat,depth}.py` | `SplatViewer`（splax + viser）、`render_depth`（MJX 射线） | 3DGS 场景 + 机体位姿推流到浏览器，已接近我们 Visual World 的形态 |

### 2.3 PegasusSimulator（`refs/discovery/PegasusSimulator/extensions/pegasus.simulator/pegasus/simulator/logic`）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `backends/backend.py` | `Backend`：`initialize / update_sensor / update_graphical_sensor / update_state / input_reference / update / start / stop / reset` | **每架机可以挂多个 backend**，`backends[0].input_reference()` 决定转速。这就是"控制源可替换"的最小接口 |
| `backends/px4_mavlink_backend.py` | `PX4MavlinkBackendConfig`（默认 `connection_baseport=4560`、`enable_lockstep`、`input_scaling=1000`、`zero_position_armed=100`、`update_rate=250`）、`poll_mavlink_messages`、`send_sensor_msgs`、`send_gps_msgs`、`handle_control` | lockstep 以 do-while 阻塞收 `HIL_ACTUATOR_CONTROLS`。armed 判定用 `mode == MAV_MODE_FLAG_SAFETY_ARMED + 1`（pymavlink 读出来是 129 的历史坑） |
| `backends/tools/px4_launch_tool.py` | `PX4LaunchTool` | 设 `PX4_SIM_MODEL` 环境变量后执行 `px4 ROMFS -s rcS -i <id> -d`，每个实例一个临时 rootfs |
| `vehicles/multirotor.py` | `Multirotor.update`、`force_and_torques_to_velocities` | 分配矩阵用 `pinv`，负值截零，超上限时**按比例归一化**（保持力矩比例） |
| `thrusters/quadratic_thrust_curve.py` | `QuadraticThrustCurve.update` | `F=k·ω²`（默认 k=8.54858e-6，ω_max=1100 rad/s），滚转力矩系数 1e-6，无电机延迟 |
| `dynamics/linear_drag.py` | `LinearDrag.update` | `F_drag = −diag(d)·Rᵀv` |
| `sensors/imu.py`、`gps.py`、`barometer.py`、`magnetometer.py` | 噪声参数 | 陀螺：noise_density 3.39e-4、random_walk 3.88e-5、bias_corr_time 1000 s、turn_on_bias 0.0087；加计：0.004 / 0.006 / 300 s / 0.196 |

### 2.4 AerialClaw（`refs/discovery/AerialClaw`，2026-03 新建）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `brain/agent_loop.py`（939 行） | `AGENT_SYSTEM_PROMPT`、`_build_iteration_prompt`、`_parse_agent_output`、`AgentLoop.run/_safe_return/_update_memory` | 单步闭环。提示词每轮注入：目标、状态、被动感知、WorldModel 障碍物、相似经验（向量检索）、执行历史（含反思）、技能表、软技能摘要。有防重复规则（连续 3 次同一技能即警告）和失败计数 |
| `brain/planner_agent.py`、`chat_mode.py` | 计划模式 / 对话模式 | 三种交互模式 |
| `skills/base_skill.py` | `Skill`（`name / description / skill_type ∈ {hard, soft, perception} / robot_type / preconditions / input_schema / output_schema / cost`）、`SkillResult` | 元数据同时用于技能表和 `skill.md` 文档生成 |
| `skills/motor_skills.py`（1168 行）、`perception_skills.py`、`cognitive_skills.py`、`soft_skill_manager.py`、`dynamic_skill_gen.py` | takeoff / fly_to / orbit / scan_area / observe / report … | 软技能是 `skills/soft_docs/*.md`（如 `area_recon.md`、`search_target.md`），由 LLM 生成和进化 |
| `config/safety_config.yaml` | `safety_level ∈ {strict, standard, permissive}`、whitelist / confirm_required、`flight_envelope`（max_speed 10、max_altitude 120、max_distance 500、min_battery 15、critical 5、heartbeat_timeout 10、max_tilt 35） | 包线**不可被 LLM 修改** |
| `swarm/protocol.py`、`commander.py`、`coordinator.py`、`node.py` | `NodeRole{commander, coordinator, executor}`、`MessageType{register, heartbeat, task_assign, task_status, task_report, query_status, status_response}`、`Commander._decompose_task`（LLM 按 capabilities 拆解）、`_fuse_reports` | 三级层次的自然语言任务树 |
| `adapters/sim_adapter.py`、`mock_adapter.py`、`px4_adapter.py`、`airsim_adapter.py` | `SimAdapter`（connect / get_state / arm / takeoff / land / fly_to_ned / hover / rtl / orbit / goto_waypoints / set_velocity_body） | mock 是"瞬移"，没有物理 |
| `docs/DEVICE_PROTOCOL.md` | 设备协议 v1.0 | REST 注册颁发 token，WebSocket 走 state / sensor / action / heartbeat，5 s 心跳、10 s 判离线 |

### 2.5 droneserver（`refs/discovery/droneserver/src/droneserver`）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `app.py` | `SafeFastMCP(FastMCP)` | 重写 `tool()` 装饰器，**所有工具注册时强制套上 `guard`**，不存在绕过路径 |
| `safety/middleware.py` | `guard`、`_evaluate`、`SafetyLayer` | 11 步固定顺序（docstring 可作规范原文）；check 自身异常时 fail-closed，返回 `guard.internal_error` |
| `safety/tiers.py` | `Tier{READ_ONLY, NORMAL, CRITICAL, EMERGENCY}`、`TOOL_TIERS`、`ESCALATIONS`、`effective_tier` | 未登记的工具一律视为 CRITICAL。按状态升级：空中 disarm、`force` 参数、飞行中清除围栏等 |
| `safety/tokens.py` | `ConfirmationStore.issue/redeem`、`fingerprint(tool,args)=sha256(tool∣sorted-json)[:32]` | 令牌单次使用，绑定 client + tool + 参数，带 TTL。区分 unknown / expired / wrong_client / wrong_tool / arguments_changed |
| `safety/validation.py`、`geofence.py` | `check_position`、`check_mission`、`point_in_polygon`；速度类命令按 `stale_timeout_s`（默认 15 s）**前推投影**后再判围栏 | 围栏由服务端独立执行，不依赖飞控 |
| `safety/audit.py`、`offboard_watchdog.py` | 只追加审计、offboard 看门狗 | 每条记录都带当时生效的 guard 开关 |
| `tools/*.py` | action / mission / offboard / telemetry / camera / gimbal / param / emergency | 约 100 个 MCP 工具（`@mcp.tool` 99 处，`TOOL_TIERS` 登记 98 项），底层为 MAVSDK |

### 2.6 CBBA-Python（`refs/discovery/CBBA-Python/lib`）

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `CBBA.py` | `solve`（外层迭代，收敛判定为 `iter − iter_prev > num_agents`）、`bundle = bundle_remove + bundle_add`、`compute_bid`（逐位置插入求最优）、`scoring_compute_score`（时间窗 + 指数折扣）、`communicate`（Choi 2009 Table 1 的 8 类更新 / 重置 / 保持规则，带 `time_mat` 时间戳） | 全连接图 `graph = ¬I`，同步轮次 |
| `Agent.py` / `Task.py` | `Agent{id, type, availability, x, y, z, nom_velocity, fuel}`、`Task{id, type, value, start_time, end_time, duration, discount λ, x, y, z}` | 用 `compatibility_mat[agent_type][task_type]` 表达异构兼容 |
| `config_example_*.json` | AGENT_TYPES [quad, car]、TASK_TYPES [track, rescue] | 默认值 TASK_VALUE 100、END_TIME 150、DURATION 5–15 |

### 2.7 抓取阅读的补充文件

- **gym-pybullet-drones** `envs/BaseAviary.py`：
  - `_groundEffect`：每桨 `F_ge = k_f·ω²·C_ge·(r_p/(4h))²`，h 截断下限 `GND_EFF_H_CLIP`；
  - `_drag`：`−C_d·Σω·Rᵀv`；
  - `_downwash`：上方机 j 对下方机 i 施加 `−α·exp(−½(d_xy/β)²)`，其中 `α=c1·(r_p/(4Δz))²`，`β=c2·Δz+c3`，仅在 `Δz>0` 且 `d_xy<10 m` 时生效；
  - cf2x 参数：`gnd_eff_coeff=11.37`，`dw_coeff = 2267.18 / 0.16 / −0.11`。
- **mavsdk_drone_show** `gcs-server/sar/coverage_planner.py`：`BoustrophedonPlanner.plan / _compute_sweep_angle / _generate_sweep_lines / _partition_lines / _assign_sectors / _build_waypoints`。依赖 shapely + pymap3d。同仓库 `gcs-server/agent_runtime/` 下有 `approvals.py`、`policy.py`、`mcp_metadata.py`、`tool_registry.py` 等，本次只看了目录结构，未精读。
- **PX4 SIH**（`refs/sim/PX4-Autopilot/src/modules/simulation/simulator_sih/sih.cpp`）：
  - `generate_force_and_torques`：`T_B=(0,0,−T_MAX·Σu)`；`Fa_E=−KDV·v_apparent`；`Ma_B=−KDW·w`；
  - `equations_of_motion`：假地面 + 科氏力 + 梯形位置积分；
  - 已由 r20 详述。另外，`docs/en/sim_sih/index.md` 确认 SIH 在 **UDP 19410+N** 输出 `HIL_STATE_QUATERNION`，供 Hawkeye 等外部可视化使用。

---

## 3. 可复用算法与实现（伪代码 / 参数）

### 3.1 RotorPy 气动力矩 → mock L2 的核心

记号：`R` 为机体到世界的旋转，`v_a^B = Rᵀ(v − w_wind)` 为机体系空速，`r_i` 为第 i 个旋翼在机体系的位置，`ω_i` 为转速。

```
local_i   = v_a^B + ω_body × r_i                                 # 每个桨毂的局部空速
T_i       = (0, 0, k_η·ω_i² + k_h·(local_i.x² + local_i.y²))     # 推力 + 平动升力
H_i       = −ω_i · diag(k_d, k_d, k_z) · local_i                 # 旋翼阻力（H 力）+ 诱导入流
D         = −‖v_a^B‖ · diag(c_Dx, c_Dy, c_Dz) · v_a^B            # 机身寄生阻力
M_flap_i  = −k_flap · ω_i · (local_i × e_z)                      # 挥舞力矩（可置 0）
F^B       = Σ(T_i + H_i) + D
M^B       = Σ r_i × (T_i + H_i) + Σ dir_i·k_m·ω_i²·e_z + Σ M_flap_i
v̇ = (R·F^B)/m + g ;  ẇ = J⁻¹(M^B − w × Jw) ;  ω̇_i = (ω_cmd,i − ω_i)/τ_m
```

- **风进入动力学的唯一入口是 `v_a`。** 这也是 E(x,y,z,t) 与飞行动力学的耦合点。雨、沙可建模为 `c_D` 的倍率与 `k_η` 的效率折损，量级很小。
- **参数来源**：
  - Hummingbird（`hummingbird_params.py`）：`k_d=1.19e-4`、`k_z=2.32e-4`、`k_h=3.39e-3`、`c_D=[5e-3, 5e-3, 1e-2]`；
  - SIH quadx（`px4_sihsim_quadx_params.py`）：`k_eta=5e-6`、`k_m=1e-7`、`tau_m=0.05`。
- **P600 级初值**（**估计值，必须按 r20 的 ULog 方法标定**）：
  - 轴距约 0.6 m，质量 3–4 kg，推重比约 2；
  - 由 `k_η = T_max,motor/ω_max²` 反推，例如 3.5 kg、T/W=2 时 `T_max≈17.2 N/电机`，ω_max 取 900 rad/s，得 `k_η≈2.1e-5`；
  - `k_m ≈ 0.015·k_η`（m），`τ_m 0.03–0.05 s`，`c_D ≈ 0.05–0.1 N/(m/s)²`。
  - 统一放在 `vehicles/p600/p600.yaml`，同时生成 SIH 机架参数、mock 参数和 gz SDF（与 r20 一致）。

### 3.2 Dryden 阵风（Environment 风场 Level 1 的正确形态）

来自 RotorPy `GustModelBase.__init__/run` 与 `DrydenWind.__init__`。

```
# 低空尺度（MIL-F-8785C，高度 h 单位 m，内部换算成 ft）
Lz = 3.281·h ;  Lx = Ly = Lz / (0.177 + 0.000823·Lz)^1.2 ;  L = [Lx,Ly,Lz]/3.281
b = 2√3·L/V ; c = 2L/V ; α = σ·√(2L/(πV)) ; β = α·b ; δ = 2c ; γ = c²
# Tustin 离散（步长 dt）
C1 = 1 + 2δ/dt + 4γ/dt² ; C2 = 2 − 8γ/dt² ; C3 = 1 − 2δ/dt + 4γ/dt²
C4 = α + 2β/dt ; C5 = 2α ; C6 = α − 2β/dt
u_k ~ U(−1,1)
y_k = (C4·u_k + C5·u_{k−1} + C6·u_{k−2} − C2·y_{k−1} − C3·y_{k−2}) / C1
wind_i(t) = W_mean(x_i, t) + y_k^{(i)}          # 每机独立滤波器状态，形状 (N,3)
```

- RotorPy 用 `V=1.0`（名义空速）。更物理的做法是取 `V = max(‖v_a‖, 1)`，并按机体速度实时更新系数。
- **σ 取值建议**（UI 的 "Turbulence" 滑块）：轻度 0.5–1 m/s，中度 1.5–2.5 m/s，强 3–5 m/s；σ_z ≈ 0.5·σ_xy。
- **Web 可视化不要用 Dryden 噪声。** 粒子流用均值场 W_mean + curl-noise 即可（见 r16）。服务器物理和前端视觉**共享均值场，湍流各自处理**。

### 3.3 控制器：SE3 几何律 + PX4 式级联（L2 用）

RotorPy `SE3Control.update`（Lee 2010）：

```
F_des = m(−K_p e_x − K_d e_v + a_ref + g e_z)
u1    = F_des · (R e_z)
b3d   = F_des/‖F_des‖ ; c1 = (cos ψ, sin ψ, 0) ; b2d = norm(b3d × c1) ; b1d = b2d × b3d ; R_d = [b1d b2d b3d]
e_R   = ½ vee(R_dᵀR − RᵀR_d)
u2    = J(−k_R e_R − k_ω (ω − ω_d)) + ω × Jω
[f_1..f_4]ᵀ = TM_to_f · [u1, u2]ᵀ ;  ω_cmd,i = sign(f_i)·√(|f_i|/k_η)
TM_to_f = inv([1…1 ; (r_i × e_z)_{x,y} ; (k_m/k_η)·dir_i])
```

纯 SE3 没有速度和倾角限幅。本机实测 20 m 阶跃时 vmax=16.9 m/s，不像 PX4。原型因此在 `F_des` 前加入 PX4 式级联：

- `v_cmd = v_ref + 0.95·(x_ref − x)`，水平限 12 m/s，垂直限 −1.5/+3 m/s；
- `a = a_ref + K_v·e_v + K_i·∫e_v`，K_v = [1.8, 1.8, 4]，K_i = [0.4, 0.4, 2]；
- 水平加速度限 `g·tan35°`。

**完整 PX4 控制律直接用 r20 的 mock L1**（`PositionControl` 移植），L2 只替换"执行器→力矩→积分"这一段。

### 3.4 多机交互：下洗流与地效（gym-pybullet-drones `BaseAviary`）

```
for i in drones:                           # O(N²) → 用 10 m 网格哈希降到 O(N·k)
  for j in neighbors(i):
    Δz = z_j − z_i ; d = ‖xy_j − xy_i‖
    if Δz > 0 and d < 10:
       α = c1·(r_p/(4Δz))² ; β = c2·Δz + c3
       F_i.z −= α·exp(−½(d/β)²)            # 世界系向下
ground_effect_i = k_η ω_i² · C_ge · (r_p/(4·max(h_i, h_clip)))²   # h = 桨离地（点云地形取 DEM/SDF 高度）
```

- cf2x 原始系数只适用于 27 g 的微型机。P600 需要按桨盘载荷缩放，或用 CFD 标定。原型按质量比粗缩放，只作演示。
- **地效高度 h 必须查 World 的地形 / 楼顶高度**（点云 → 高度图 / SDF），不能用 z=0。这是 UrbanScene3D 城市里在楼顶起降时的真实效应。
- 性能：朴素 O(N²) 在 300 机时占 35 ms/步（§3.10）。改为 **50 Hz 低频更新 + 网格邻域**，再做零阶保持。

### 3.5 Crazyflow 式机群架构（FleetSim 骨架）

```python
# SoA：所有量 (N,·)；多场景并行时再加 world 维 (W,N,·)
class FleetState: x v q w rotor(N,4) force_ext(N,3) torque_ext(N,3) mode(N) battery(N) seed

step_pipeline = OrderedDict([
  ("mission",        mission_fsm),        # GoTo/Path/Orbit/RTL 等产生 setpoint（L1）
  ("controller",     px4lite_or_se3),     # r20 mock L1 控制律 → ω_cmd / thrust+att
  ("env_wind",       env_query_mean_plus_dryden),   # 写 wind(N,3)
  ("aero",           rotorpy_wrench),     # §3.1 → F^B, M^B（L2；L1 模式下跳过）
  ("interaction",    downwash_ground_effect),        # §3.4，50 Hz 抽稀
  ("integration",    semi_implicit_euler_or_rk4),
  ("collision",      world_sdf_collision),           # 点云 voxel/SDF，碰撞后置 CRASHED
  ("sensors",        imu_gps_baro_noise),            # Pegasus 噪声参数
  ("faults",         fault_injection),               # GPS 丢失 / 电机失效（UI 下拉）
  ("battery",        battery_model),
  ("telemetry_tap",  decimate_to_ws_20hz),           # 只抽样，不阻塞
])
insert_fn_before(step_pipeline, "integration", my_plugin)   # 插件纯函数 FleetState -> FleetState
```

**Crazyflow `so_rpy`（L1.5：指令 = [roll, pitch, yaw, 总推力]）的辨识模型：**

```
thrust = acc_coef + cmd_f_coef·F_cmd
v̇ = thrust/m · R(rpy)e_z + g
r̈py = rpy_coef ⊙ rpy + rpy_rates_coef ⊙ ṙpy + cmd_rpy_coef ⊙ rpy_cmd
[hb_x500] m=2.28, cmd_f_coef=0.912, rpy_coef=[−51.4,−51.4,−26.9],
          rpy_rates_coef=[−8.94,−8.94,−8.65], cmd_rpy_coef=[47.3,47.3,25.3]
```

用途：外环接 r20 的 PX4 位置 / 速度级联，内环用这组二阶系统替代完整姿态环 + 电机模型，省去电机、力矩分配和力矩积分，计算量明显低于 L2（未实测倍数）。x500 与 P600 同为 2–4 kg 级，**在 P600 实测辨识出来之前可以用它作占位**。

### 3.6 PX4 HIL 桥（V0.4–V0.6，fleet-wide lockstep）

以 RotorPy `PX4Multirotor.step` 与 Pegasus `PX4MavlinkBackend.update/poll_mavlink_messages` 为蓝本：

```
# 每架 PX4 以 PX4_SIMULATOR=rotorpy/none 启动，作为 TCP client 连 World 的 4560+k（见 r20）
loop every dt=4ms (250 Hz) in World clock:
  for k in fleet:                                     # 并发发送
     sd = statedot(state_k)                            # 需要比力 a−g
     send HIL_SENSOR(t_us, acc_FRD, gyro_FRD, mag, abs_p, diff_p, p_alt, temp, fields=7|56[|448|6656])
     if t % 200ms == 0: send HIL_GPS(fix=3, lat_e7, lon_e7, alt_mm, eph, epv, vel_cms, vn, ve, vd, cog, sats)   # GPS 建议 5–10 Hz
     (optional) send HIL_STATE_QUATERNION (ground truth for logging)
  for k in fleet: wait HIL_ACTUATOR_CONTROLS (timeout 50ms → keep last / mark STALLED)
        if msg.mode & ARMED: ω_cmd = controls[0:4]·ω_max  else 0
  step FleetSim pipeline with ω_cmd （aero/wind/collision 均来自我方）
```

**坐标换算**（全系统唯一实现，放在 `world/georef/frames.py` 与前端 `frames.ts`）：

- 世界系 ENU → NED：`[n, e, d] = [y_enu, x_enu, −z_enu]`，矩阵 `T = [[0,1,0],[1,0,0],[0,0,−1]]`；
- 机体系 FLU → FRD：`B = diag(1, −1, −1)`；
- 姿态：`R_ned_frd = T · R_enu_flu · B`；四元数由矩阵转换，不要手写分量置换；
- 加速度计 / 陀螺（FRD）：`(a_x, −a_y, −a_z)` 和 `(ω_x, −ω_y, −ω_z)`（RotorPy `_imu`）；
- ENU → Three.js（Y-up 右手系）：`three = (x_enu, z_enu, −y_enu)`。可验证 `E×U = −N`，右手性成立；
- ENU → 经纬度：小范围可用等距矩形近似（RotorPy `enu_to_geodetic`，10–20 km 内）。正式实现用 pymap3d 的 ENU<->ECEF<->LLA。

**IMU 噪声**（Pegasus `sensors/imu.py`，离散化公式 `σ_d = σ/√dt`）：

- 陀螺：noise_density 3.39e-4 rad/s/√Hz，random_walk 3.88e-5，τ_bias 1000 s；
- 加计：noise_density 4e-3 m/s²/√Hz，random_walk 6e-3，τ_bias 300 s。

**lockstep 防卡死**：设 50 ms 超时，超时的实例标记为 `STALLED` 并沿用上一帧指令。RotorPy 的做法是没收到就置零，**这在空中会导致坠机**，不可取。

### 3.7 Agent Runtime：单步闭环 + 技能元数据（AerialClaw）

```
SkillSpec = {name, description, skill_type: hard|soft|perception, robot_type[],
             preconditions[], input_schema{}, output_schema{}, cost,
             tier: read_only|normal|critical|emergency,        # 来自 droneserver
             capability_id: "flight.goto" | "thermal.imaging"}  # 对齐 ANet（d05：不带 @device 后缀）

loop (≤ max_iter, 每轮 1–5 s):
  ctx = goal + DroneState摘要 + 感知摘要 + WorldModel障碍 + 相似经验(top-k) + 最近 N 步(含反思) + 技能表 + 软技能摘要
  out = LLM(ctx) → JSON {thinking, decision∈{act,done,stuck}, action{skill,robot,parameters}, reflection, goal_progress}
  parse：去 ``` 包裹 → json.loads → 正则 {…} 兜底 → 失败计入 fail
  if decision==act:  result = Gateway.guard(skill, params)  →  Runtime.execute  →  SkillResult{success, output, error_msg, cost_time}
  if 连续失败≥3 或 同一技能连续3次: 注入纠偏提示；超过上限 → _safe_return（悬停/返航）
  memory.update(reflection)；软技能候选（重复模式）→ 人审后入库
```

- 三级集群（commander / coordinator / executor）的 LLM 分解，对应我们的 **Mission（人）→ Task（ANet）→ Skill（单机）**。
- 但 AerialClaw 的分解**完全依赖 LLM**，没有可行性检查。我们的分配器必须是确定性算法（§3.9），LLM 只负责生成候选任务和解释结果。

### 3.8 安全守卫（droneserver → 我方 Command Gateway 中间件）

```python
def guard(tool, args, ctx):
    client = authenticate(ctx.api_key)                        # scope ∈ {telemetry, control, admin}
    state  = state_tracker.snapshot(drone_id) if needs_state(tool) else None
    tier   = effective_tier(tool, args, state)                # TOOL_TIERS + ESCALATIONS; 未登记 → CRITICAL
    if not client.can(tier):              return reject("auth.scope", remedy=...)
    if tier != EMERGENCY and rate_limited(client, tier):     return reject("rate_limit")
    if tier == CRITICAL:
        tok = args.pop("confirm_token", None)
        if tok is None: return confirmation_required(store.issue(client, tool, args, consequence, ttl=60s))
        ok, why = store.redeem(tok, client, tool, args)       # 单次、绑定参数指纹
        if not ok: return reject(f"confirm.{why}")
    if v := check_bounds(tool, args):                          return reject(v)   # 高度/速度/距离/坐标/任务规模
    if v := geofence.check(project(tool, args, state, horizon=stale_timeout_s)): return reject(v)
    if v := preconditions(tool, state):                        return reject(v)   # 未起飞不能导航；状态未知时只允许"减能量"指令
    result = execute(tool, args)
    audit.append(who, tool, args_hash, tier, allowed, rule, dt, guards_in_force)
    return result          # 任一检查自身抛异常 → reject("guard.internal_error")（fail closed）
```

- 前端 UI 的"确认"对话框（shadcn `AlertDialog`）与 CRITICAL 令牌是同一套协议：**人和 LLM 走同一条守卫路径**。
- 多机改造：`ConfirmationStore` 的键要包含 `drone_id`。原实现是"one server, one drone"（`tokens.py` 注释）。

### 3.9 任务分配：统一打分 + Hungarian / 拍卖 / CBBA

**统一打分**（CBBA-Python `scoring_compute_score` 修正版）：

```
t_arrive(a, path, j, pos) = 前序完成时刻 + dist(prev, j)/v_a
t_start = max(j.start_time, t_arrive) ;  feasible = t_start ≤ j.end_time − j.duration
                                        ∧ compat[a.type][j.type] ∧ battery_ok(a, path⊕j)
S(path) = Σ_{j∈path} value_j·exp(−λ_j·(t_start_j − j.start_time)) − fuel_a·len(path)
bid_{a,j} = max_pos [ S(path ⊕_pos j) − S(path) ]          # 真正的边际增益（修正点）
```

- **V0.6 集中式**：构造 `C[a,j] = −bid_{a,j}`（单任务 / 单轮），用 Hungarian（`scipy.optimize.linear_sum_assignment`）求解；多任务时做"顺序单物品拍卖（SSI）"，每轮把一个任务分给出价最高者，重算出价直到全部分完。
- **V1.0 CBBA**（去中心化，每架机一份 y、z、t 向量）：

```
repeat until (无新出价的轮数 > N_agents):
  # Phase 1 bundle building（每 agent 本地）
  bundle_remove: 若 bundle 中某任务的 winner≠self，则删除它及其后所有任务（级联释放）
  while |bundle| < L_max:
     c_j = bid_{a,j}（§上式）；h_j = [c_j > y_j] ∨ ([c_j == y_j] ∧ a < z_j)
     j* = argmax_j c_j·h_j ; if c_{j*} ≤ 0: break
     插入 path 的最佳位置；y_{j*}=c_{j*}; z_{j*}=a
  # Phase 2 consensus（与邻居 k 交换 y、z、时间戳 s）
  对每个任务 j 按 Choi 2009 Table 1 的 update / reset / leave 规则合并（CBBA.communicate 的 8 类分支）
```

复杂度为每轮 O(N·T·L)，同步轮数上界约为网络直径 × L。
- **多机覆盖**（mavsdk_drone_show `BoustrophedonPlanner`）：

```
ENU ← polygon(latlon) ; θ = atan2(最长边) ; spacing = sweep_w·(1 − overlap%)
lines = clip(polygon, 旋转坐标系中 y = y_min + spacing/2 + k·spacing 的水平线) ; 奇数行反向
partitions = 按累计长度 ≈ total/N 切分（贪心，最后一组收尾）
assign = Hungarian(cost = dist(drone_i, partition_j.entry))   # 原实现为就近贪心，改用 Hungarian
waypoints = [takeoff → entry] + survey legs(is_survey_leg=True) + [exit → RTL]
```

sweep_w 由相机 FOV 与高度决定：`2h·tan(FOV/2)`。

### 3.10 本机实测：L2 机群原型（`fleet_l2_bench.py`）

环境：8 核 Xeon，与其他任务共享（load 6.7），numpy 2.5.3，单线程，dt=4 ms（250 Hz），半隐式欧拉。机型为 2 kg X500 级。

| 场景 | 结果 |
|---|---|
| 单机 20 m 位置阶跃，6 m/s 均值风 + Dryden σ=(1,1,0.5) | vmax 10.5 m/s（限 12），t90 2.64 s；10 s 后误差 0.77 m（阵风扰动），瞬时倾角 14.2° |
| N=1 / 10 / 50（含下洗流） | 1.23 / 1.52 / 2.03 ms/步 → 实时倍率 ×3.2 / ×2.6 / ×2.0 |
| N=100（含下洗流 O(N²)） | 5.9 ms/步（×0.7）。下洗流改 50 Hz 后可实时 |
| N=300（含下洗流） | 35 ms/步，**必须**改网格邻域 + 抽稀 |
| N=1000（不含下洗流） | 7.7 ms/步 → 100 Hz 下可实时（×1.3） |

**结论。** 按 L2 满配置，250 Hz 可实时支撑约 50 架，100 Hz 约 300 架；再往上用 r20 的 L1（1000 架 @250 Hz 3.5 ms）。数值与 r20 同量级，两层可以混用：每机一个 `fidelity` 字段，同一 pipeline 内做 mask 分支。

---

## 4. 在本项目中的落点与复用方式

| 本项目模块（建议目录） | 来源 | 复用方式 | 版本 | 具体做法 |
|---|---|---|---|---|
| `simulation/mock/fleet.py`（FleetSim 骨架） | Crazyflow `sim/pipeline.py`、`sim/data.py` | port（架构） | MVP | SoA + OrderedDict 命名 stage；频率分级（控制 100 Hz / 物理 250 Hz / WS 20 Hz）；每机 `fidelity ∈ {L0, L1, L1.5, L2, SIH, HIL}` |
| `simulation/mock/aero.py`（L2） | RotorPy `compute_body_wrench` | port | V0.3（视觉联动）/ V0.4 | §3.1 向量化，已有原型 |
| `simulation/mock/so_rpy.py`（L1.5） | Crazyflow `dynamics/so_rpy` + `[hb_x500]` | port | V0.2 | 在 P600 辨识前作占位 |
| `environment/wind/turbulence.py` | RotorPy `dryden_utils.py` | port | V0.3 / V0.4 | 每机独立滤波；E-field 只给均值 + (σ, L) |
| `simulation/mock/interaction.py` | gym-pybullet-drones `_downwash/_groundEffect` | port（公式） | V0.6 | 网格邻域；地效查 World 高度图 |
| `simulation/hil/` | RotorPy `px4_multirotor.py`、Pegasus `px4_mavlink_backend.py` | port | V0.4–V0.6 | §3.6；与 r20 端口规则合用 |
| `simulation/backends/base.py` | Pegasus `Backend` | port（接口） | V0.2 | `update_state / update_sensor / input_reference / update / start / stop / reset`；实现 MockBackend、SihBackend、HilBackend，以后加 IsaacBackend |
| `simulation/sensors/noise.py` | Pegasus `imu.py / gps.py / barometer.py` | port（参数） | V0.4 | IMU / GPS / 气压噪声，HIL 与遥测显示共用 |
| `apps/api/gateway/guard.py` | droneserver `safety/*` | port | V0.6（人工命令）/ V1.0（LLM） | §3.8；FastAPI 依赖注入 + MCP 装饰器两处复用 |
| `agent/runtime/loop.py`、`agent/skills/` | AerialClaw `brain/agent_loop.py`、`skills/base_skill.py`、`config/safety_config.yaml` | port（语义） | V1.0 | SkillSpec = ANet capability manifest 子集 |
| `agent/mcp/server.py` | droneserver `app.py SafeFastMCP`；MAVLinkMCP 的计划生命周期（draft→validated→uploaded） | port | V1.x | 工具即技能；计划先验证再上传 |
| `swarm/allocation/{score,hungarian,ssi,cbba}.py` | CBBA-Python `lib/CBBA.py` | port（修 DMG） | V0.6（集中）/ V1.0（CBBA） | §3.9，统一打分 |
| `swarm/coverage/boustrophedon.py` | mavsdk_drone_show `sar/coverage_planner.py` | port | V0.6 | §3.9；依赖 shapely、pymap3d |
| `simulation/scenarios/urbanscene3d_paths.py` | 本地数据 `data/raw/urbanscene3d/paths/{Town,Castle,School,Bridge}/*/overlap_{low,high}/*.txt`、`Oblique.log` | 新增 | **MVP** | 每行 `img, x, y, z, pitch, roll, yaw`，UE 坐标（cm，左手系）。换算成 ENU（m）后作为内置 FollowPath 任务（航测路径回放），每个场景 181 点级别 |
| GPU 节点（可选） | Pegasus（Isaac 5.1）、Isaac Lab ARL drone、Cosys-AirSim | reference | V0.5+/V1.x | 同一 Backend 接口，World Model → USD / UE 资产 |

---

## 5. 对比与推荐

### 5.1 物理 / 仿真后端（无 GPU 服务器为前提）

| 方案 | 无 GPU | 环境场耦合 | 多机规模（本机） | 飞控真实度 | 2026 活跃 | 本项目排序 |
|---|---|---|---|---|---|---|
| 我方 FleetSim（L1 + L2，本文 + r20） | yes | yes 原生（均值 + 湍流 + 下洗） | L2 约 50@250 Hz / 约 300@100 Hz；L1 1000+ | 中高 | — | **1（MVP–V0.6）** |
| PX4 SIH 容器（r20） | yes | no（仅均匀水平风） | ≤8 实时 | 最高 | 5/5 | **2（V0.2）** |
| PX4 + HIL（我方物理，RotorPy / Pegasus 方式） | yes | yes | 约 8–16 | 最高 | 4/5 | **3（V0.4–V0.6）** |
| RotorPy 原样（solve_ivp） | yes | yes | 个位数 | 中 | 4/5 | 仅作参考实现和回归基准 |
| Crazyflow 原样（JAX + MJX） | yes（CPU 3.3 M steps/s @64 worlds，README 数据，7950X） | 插件 | 大 | 中（Crazyflie / x500） | 5/5 | RL / 批量蒙特卡洛时 adopt |
| gym-pybullet-drones | yes | 有限 | 数十 | 中 | 4/5 | 参考公式 |
| Pegasus / Isaac Lab / Aerial Gym / OmniDrones / Genesis | no（Genesis 可 CPU，但慢） | yes | 大 | 高（渲染） | Pegasus、Isaac Lab 活跃；OmniDrones 停滞 | GPU 节点，V1.x |

### 5.2 LLM / Agent 接入

| 方案 | 强项 | 弱项 | 排序 |
|---|---|---|---|
| droneserver | 安全层最完整（分级、令牌、前推围栏、审计、fail-closed），论文有对抗测试 | 单机；star 少 | **1（安全中间件）** |
| AerialClaw | 完整 Agent 形态（单步闭环、技能与软技能、记忆、三级集群、Web 控制台、mock） | 分解全靠 LLM；Flask-SocketIO 单体（server.py 2801 行） | **2（Agent 语义）** |
| mavsdk_drone_show agent_runtime | 与我方栈同构（FastAPI + React + PX4），含 approvals / policy / MCP 元数据 | 未精读；依赖 Gazebo SITL 镜像，体量大 | 3（V1.0 再读） |
| ros-mcp-server | star 高，接 ROS 生态零改动 | 粒度是 topic / service，缺安全语义 | 4（ROS2 路线） |
| MAVLinkMCP | 计划生命周期门控 | HTTP 无认证 | 参考 |
| TypeFly / SwarmGPT | 计划即程序 / LLM + 安全滤波编舞 | 硬件绑定（Tello / Crazyflie） | 参考 |

### 5.3 任务分配

| 方案 | 适用 | 排序 |
|---|---|---|
| 集中式 Hungarian / SSI（统一打分） | V0.6，≤50 机，通信可靠 | **1** |
| ANet 合同网（d05） | V1.0，跨组织 / 异构，秒级延迟可接受 | **2** |
| CBBA（修 DMG） | 断网 / 弱连通、去中心化实验 | **3** |
| OR-Tools / PyVRP（mTSP / VRP，带电量容量约束） | 大量航点、覆盖后排序；14115 stars / 702 stars，2026 活跃 | 候选（V0.6 前先做 PoC 再定） |

---

## 6. 风险与注意事项

| # | 风险 / 坑 | 影响 | 对策 |
|---|---|---|---|
| R1 | Isaac Lab、Pegasus、Aerial Gym、OmniDrones、Cosys-AirSim 都要 NVIDIA GPU；本机无 GPU | 无法本地验证高保真后端 | 只抽取接口和参数；GPU 节点列为 V1.x 可选项；MVP 与 V0.x 全部走 CPU 路线 |
| R2 | RotorPy `solve_ivp` 每机每步构造闭包，批量版依赖 torch | 直接用会很慢，而且引入大依赖 | 已改写为 numpy SoA 固定步长（§3.10）；RotorPy 只作回归基准 |
| R3 | Crazyflow 要求 Python ≥3.11、JAX、MuJoCo-MJX，并锁 `mujoco<3.11`、`jax!=0.10.2` | 版本冲突 | 只移植架构与 `so_rpy` 参数，不作运行时依赖 |
| R4 | 下洗流 O(N²)，300 机时 35 ms/步 | 卡帧 | 网格哈希 + 50 Hz 抽稀 |
| R5 | 坐标系混乱（ENU/NED/Y-up/AirSim 负 z/UE 左手 cm） | 最常见的 bug，LLM 也会写错 z | 全系统唯一的 `frames` 模块和单元测试；DroneState 写明 `frame: "ENU"`；LLM 工具只接受 ENU 米和"离地高度 AGL" |
| R6 | HIL lockstep：World 卡顿 → 所有 PX4 一起卡；RotorPy 收不到指令就置零 | 空中失控 | 50 ms 超时沿用上一帧指令，标记 STALLED 并在 UI 告警 |
| R7 | Pegasus 的 armed 判定 `mode == 129` 属于 pymavlink 实现细节 | 升级后失效 | 改用 `mode & MAV_MODE_FLAG_SAFETY_ARMED` |
| R8 | CBBA-Python 打分违反 DMG；全连接同步假设 | 动态重规划振荡、不收敛 | 边际增益打分；按真实邻接图做异步消息；设轮数上限后回退到集中式 |
| R9 | LLM 延迟 1–5 s；ANet 委派约 1 s（d05 实测） | 不能进控制环 | LLM 只下任务级技能；所有运动由 L1 控制律或 PX4 执行 |
| R10 | 提示注入、伪造 / 重放确认令牌、越权 | 安全事故 | droneserver 守卫；令牌绑定 `drone_id + 参数指纹`；审计只追加 |
| R11 | Dryden 系数中 V 取常数 1 m/s（RotorPy） | 高速时湍流频谱失真 | V = max(‖v_a‖, 1)，每 0.5 s 更新系数 |
| R12 | 新项目 star 很少（droneserver 7 stars、AerialClaw 132 stars），未来维护不确定 | 依赖断更 | 一律按 port（移植语义），不作运行时依赖 |
| R13 | UrbanScene3D 路径是 UE 左手系、单位 cm，pitch=90 表示垂直向下 | 回放方向错 | 转换 `(x, y, z)_UE[cm] → ENU[m] = (x/100, −y/100, z/100)`，yaw 取反，并用场景点云包围盒校验（待与数据单元确认轴向） |
| R14 | 许可证 | — | 科研用途，按要求忽略 |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§26 "不建议自行开发动力学" 需要改成"分层保真 + 统一 Backend 接口"。** 原文把 PX4 SITL 当作唯一的物理层，但 PX4 SIH 只有均匀风和平地面，不认识点云楼宇。建议写成：

   ```
   L0 运动学 → L1 PX4-lite（r20）→ L1.5 so_rpy → L2 气动力矩（RotorPy）
     → SIH（真飞控）→ HIL（真飞控 + 我方物理）→ Isaac/Gazebo（GPU）
   ```

   同时定义 Pegasus 式 `Backend` 接口。"World 统一步进"应写入原则：物理（或至少环境与碰撞）归 World Runtime 所有，飞控可以是外部进程。
2. **§18–20 风场模型有概念错误。** 湍流不是一个可查询的确定性 4D 场，而是每机的随机过程。建议 `environment.query(x,y,z,t)` 返回 `{wind_mean, turbulence: {σ_u, σ_v, σ_w, L_u, L_v, L_w}, visibility, …}`，各机体按 Dryden（§3.2）自行积分。Level 1 明确写成"Dryden / MIL-F-8785C"，Level 2 的地形 / 建筑效应只改均值场与 σ。
3. **§22–24 雨雾沙的 "Flight Drag" 需要量化，否则会过度建模。** 雨对多旋翼的主要效应是传感器退化和电机效率，阻力增量可以忽略。建议只保留两个参数：`c_D` 倍率（默认 1.0–1.05）和 `k_η` 效率（沙尘 0.95–1.0）。重点放在传感器模型上。
4. **§28 DroneState 缺少关键字段和坐标约定。** 建议补充：
   - `frame: "ENU" | "NED"`，`quat_order: "xyzw"`；
   - `sim_time`、`seq`、`source: mock | sih | hil | real`、`fidelity`；
   - `rotor_speeds`、`airspeed / wind_est`、`home`、`geofence_status`；
   - `effect_status`（对齐 d05 的 OK / UNVERIFIED）、`stalled`。

   另外单独给出 ENU<->NED<->Three.js 的换算（§3.6）。
5. **§29 多机架构只写了"每机一个 PX4 SITL"，缺时钟与交互。** 应补充：
   - SIH 多进程时钟漂移（r20）；
   - 机间交互（下洗流、避碰、通信距离）必须在统一 step pipeline 中计算；
   - 容量公式：L2 约 50 机 @250 Hz / 300 机 @100 Hz，本机实测；SIH ≤8 机。
6. **§30 控制模式要补"前置条件、安全分级、覆盖算法"。**
   - 每个模式即一个 SkillSpec（含 preconditions / input_schema / tier）；
   - `Area Coverage / Search` 给出 Boustrophedon + 分区 + Hungarian 的算法（§3.9）；
   - `Formation` 引用 r26。
7. **§31–32 Agent Network 缺安全模型。** 应加入"LLM / 远程 Agent = 不可信指挥官"原则和 droneserver 式守卫（§3.8）。人工 UI 与 Agent 共用同一 Command Gateway、同一审计链。MCP 作为 V1.x 的 LLM 接入面，工具即技能。
8. **§50 "Task Assignment" 需要指定算法与演进。** 统一打分函数（时间窗 + 折扣 + 兼容矩阵 + 电量可行）。V0.6 集中 Hungarian / SSI，V1.0 ANet 合同网，CBBA 作去中心化备选。LLM 只做"意图 → 候选任务"，不做分配决策（AerialClaw 的纯 LLM 分解不可验证）。
9. **§35 Isaac Sim 路线要具体化。** 写明用 Pegasus Simulator（Isaac 5.1，PX4 MAVLink backend），不用 OmniDrones（维护者自述难以维护）。RL 需求走 Isaac Lab contrib multirotor 或 Crazyflow（CPU 也可）。所有 GPU 后端都实现同一 Backend 接口。
10. **§37 频率表按实测修订。** 物理 250 Hz（L2 ≤50 机）或 100 Hz（≤300 机）；控制 100 Hz；下洗流 50 Hz；WS 10–30 Hz；LLM 决策 0.2–1 Hz；ANet 委派约 1 Hz。
11. **§43 MVP 应把"内置场景 + mock 任务"写进验收。** 把 UrbanScene3D 自带的航测路径（`paths/*/overlap_{low,high}`、`Oblique.log`）转换成内置 FollowPath 任务，与 6 个城市点云一起作为开箱即用的演示和压测场景（N 机 × 多路径）。
12. **新增"Scenario / Experiment"一节**（原文缺失）。内容包括：
    - 可复现的随机种子、初始条件、环境参数、故障注入时间表；
    - 批量蒙特卡洛（参考 RotorPy `simulate_batch`、Crazyflow 的 world 维度）；
    - 统一录制与回放格式（事件 + 状态，参考 Hawkeye 的 ULog 多机相关性回放）；
    - 评测指标：任务完成率、路径长度、碰撞 / 围栏违规次数、LLM 调用次数与延迟。
13. **无 GPU 的传感器仿真路线（原文只写 Isaac）。** 分三个阶段：
    - V0.4：用 World 的点云 voxel / SDF 做 CPU 射线投射，模拟 LiDAR 和测距，雾、雨、沙以 dropout 和噪声作用于射线（对应 §23）；
    - 相机先做 RotorPy 式"特征点投影"或前端离屏渲染截图；
    - 真实感 RGB 留给 GPU 节点。
