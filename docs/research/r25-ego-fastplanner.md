# R25 研究笔记：EGO-Swarm / Fast-Planner / Fast-LIO2_Ego-Planner（轨迹规划）
## 服务端 Planning 模块接口、MVP 规划器（A* + B-spline + 速度规划）与多机互避

> 研究单元：r25 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §8（Geometry World）、§26–§30（Drone Simulation / 控制模式）、§36–§37（通信与频率）、§42（repo 的 `swarm/planning`、`swarm/avoidance`）、§45（V0.2）、§49（V0.6）
>
> 仓库快照（都是 shallow clone，**`refs/` 未做任何修改**）：
> - `refs/swarm/ego-planner-swarm`：@`92fe9f7`（2025-03-08，★2193），master 分支，ROS1
> - `refs/swarm/Fast-Planner`：@`41be219`（2024-10-24，★3414），ROS1
> - `refs/swarm/Fast-LIO2_Ego-Planner`：@`f9581e8`（2024-12-10，★1），ROS1 Noetic，emNavi 的集成仓
>
> 为补齐分支差异，额外拉取到 `.cache/research/r25/`：
> - `ego-swarm-ros2`：`ros2_version` 分支，@`23a8d5a`，2025-03-08
> - `ego-swarm-notimesync`：`no_time_sync` 分支，2021
> - 后继仓库的最新提交日期：`EGO-Planner-v2` 为 2023-03；`GCOPTER` 为 2023-06；`Primitive-Planner` 为 **2026-08**（论文 TRO 2025）
>
> 本机实测原型都在 `.cache/research/r25/`：
> - `proto/world.py`：点云 → 规划栅格，包括 DSM 挤出和 ESDF
> - `proto/astar.py`：numba 版 A*，带 round-stamp 节点池和 LOS 剪枝
> - `proto/bspline.py`、`proto/bench_opt.py`：B-spline 初始化与 L-BFGS 优化
> - `proto/velprof.py`：TOPP-lite 速度规划
> - `proto/orca3d.py`、`proto/orca_test2.py`：RVO2-3D 的 numba 移植
> - `proto/priority.py`：优先级规划 + 4D 预约
> - `bridge/enc.cpp` + `bridge/codec.py`：EGO UDP 轨迹报文的 Python 编解码，已与 C++ **逐字节一致**
>
> 实测数据：UrbanScene3D Shenzhen 采样点云，共 5,000,141 点，范围 1848×1999×391 m。
>
> 分工边界：
> - Prometheus 的 EGO fork（control_state gating、traj_server_for_prometheus）见 r19 §2.4。
> - 前端 de Boor 求值（TS 版）见 r19 §3.10。
> - FAST-LIO 本体见 r05/r08。
> - 本文聚焦三件事：**规划算法本身**、**服务端 Planning 模块**、**多机互避**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **ZJU-FAST-Lab/ego-planner-swarm**（master，ROS1） | 分布式、异步的多机局部规划。流程为：局部 GridMap → A* 引导 → **{p,v} 对碰撞项（ESDF-free）** → 均匀 B-spline + L-BFGS → rebound 重优化；多机之间做轨迹广播和椭球互避 | **port**：代价函数、B-spline 契约、FSM 语义、swarm 代价、UDP 轨迹报文移植到 Python/numba 的 Planning 服务。**adopt**：V0.6 以 ROS2 分支作为 SITL/真机规划后端 | V0.2（契约 + FSM + 单机规划）；V0.6（多机、EGO 后端） | ★★★★★ |
| ego-planner-swarm `ros2_version` | 同一套算法机械移植到 rclcpp（Humble），launch 改为 `.py`，注释汉化 | **adopt**，作为 V0.6 的 ROS2 规划后端。要求 CycloneDDS，时钟有坑（见 §6） | V0.6 | ★★★★☆ |
| **HKUST-Aerial-Robotics/Fast-Planner** | kinodynamic A*（OBVP 启发式）+ **ESDF** B-spline（NLopt）+ 非均匀 B-spline 时间重分配 + 拓扑 PRM + yaw B-spline | **port**：ESDF 距离代价、Felzenszwalb 1D EDT（直接换成 `scipy.ndimage.distance_transform_edt`）、`reallocateTime`、`planYaw`、边界状态 → 控制点公式。**reference**：kino A*、topo PRM | V0.2–V0.6 | ★★★★☆ |
| spirit0609/**Fast-LIO2_Ego-Planner**（emNavi 改） | 链路为 MID-360 → livox_ros_driver2 → FAST-LIO(SC-PGO) → livox2pointcloud → EGO（`USE_MID360_CLOUD` 模式） | **reference**：真机链路拓扑、`REMOTE_TARGET`/`REMOTE_START` 外部目标接口、master/slave UDP bridge。代码质量低，不直接依赖 | V0.5（真机/回放）；V0.6 | ★★☆☆☆ |
| （后继，仅供参考）Primitive-Planner（TRO 2025，2026-08 仍有提交） | 离线运动基元库 + TOPPRA，超轻量的大规模 swarm 规划 | **reference**：V1.0 百架规模时再评估 | V1.0 | ★★★☆☆ |

**核心判断（给实现者）：**

1. **服务端场景与 EGO 的原始约束不同。**
   - EGO 不用 ESDF，是为了照顾机载算力：地图是未知、局部、0.1 m 分辨率的。
   - 我们的 Planning 服务手里有**完整的先验世界**（World Package），而且在服务器上运行。所以 MVP 直接用 **全局 A* + ESDF 梯度代价（Fast-Planner 风格）的 B-spline 优化**，更简单、更稳。
   - EGO 的 {p,v} rebound 作为可选项保留，只在"动态发现的障碍、局部未知地图"模式（V0.6 探索演示）中启用。
2. **实测性能足够 MVP 使用**（单线程）。
   - 城市级 4 m 栅格：463×500×77 = 17.8M voxel。
     - numba A*（w_heu=1.5）：典型 **0.1–20 ms**，低空复杂航线最长约 160 ms；
     - LOS 剪枝：< 10 ms；
     - B-spline（45–390 个控制点）L-BFGS：**25–80 ms**。
   - 一条 0.25–2.8 km 的航线，端到端 < 250 ms。
   - **必须把 BLAS 设成单线程**。不这样做，scipy L-BFGS-B 每次迭代要 5 ms，总耗时会慢 40 倍（1024 ms → 23 ms）。
3. **UrbanScene3D 采样点云太稀**（约 1.35 pts/m²）。
   - 1 m DSM 有 **45.6% 的空洞**，2 m 为 9.5%，4 m 为 0.2%。
   - 规划栅格要用 **2–4 m 分辨率**，外加 **DSM 挤出（屋顶以下视为实心）**和闭运算，否则 A* 会从稀疏的立面"钻"进楼里。
4. **多机互避分三层**：
   - **战略层（计划时）**：优先级 + 4D 预约，冲突时用"起飞延迟 / 高度层"消解。实测 100 架随机 OD 耗时 0.63 s，平均延迟 0.5 s。
   - **轨迹层**：EGO 椭球 swarm 代价，z 方向 2 倍间距以防下洗。
   - **战术层（20–50 Hz）**：ORCA-3D 的 numba 移植。随机任务下 100 架 1.6 ms/tick、0 违规；300 架 16 ms/tick，最小间距 1.89 m（要求 2.0 m）。
   - ORCA 在**加速度受限**时，对"对心汇聚"这类极端场景**不保证安全**（实测最小间距 0.2–0.7 m）。因此战略层不能省。
5. **虚实混合 swarm 可行**。EGO 的 UDP 8081 `ONE_TRAJ` 报文是 packed 小端格式，Python 编解码已与 C++ 逐字节验证。V0.6 起，数字孪生里的虚拟机可以把自己的 B-spline 广播给真实的 EGO-Swarm 无人机，让真机主动避让虚拟机。前提是时钟同步误差 < 0.25 s。

---

## 1. 仓库概览

| 项 | ego-planner-swarm | Fast-Planner | Fast-LIO2_Ego-Planner |
|---|---|---|---|
| 论文 | EGO-Swarm（ICRA 2021），EGO-Planner（RA-L 2021） | RAPTOR（T-RO 2021）、Robust & Efficient（RA-L 2019）、Topo（ICRA 2020） | 无，属于工程集成 |
| Stars / 最后提交 | ★2193 / 2025-03-08（README 指向 ROS2 分支） | ★3414 / 2024-10-24（PR 合并） | ★1 / 2024-12-10 |
| 分支 | master、**ros2_version**、no_time_sync、multithread_map、changing_vel、drone_detect、benchmark | master | main |
| 语言 / 构建 | C++14、catkin；ROS2 分支为 ament/colcon | C++、catkin | catkin |
| 依赖 | Eigen、PCL、OpenCV/cv_bridge、Armadillo（仅 uav_simulator）；`local_sensing` 可选 CUDA | Eigen、PCL、**NLopt 2.7.1（需源码编译）**、Armadillo | Ceres 2.1、GTSAM、Livox-SDK2、Eigen 3.3.7、PCL |
| 求解器 | 自带 `lbfgs.hpp`（liblbfgs 移植，1450 行，More-Thuente 线搜索） | NLopt：LD_LBFGS(11) / LD_TNEWTON(15) | 同 EGO |
| 规模 | planner 部分约 14k 行，共 596 个文件 | 约 17k 行（含 4.5k 行 backward.hpp） | 847 个文件（含驱动、LIO、PGO） |
| 仿真 | `uav_simulator`：`fake_drone`（指令即里程计）、`so3_quadrotor_simulator`、`so3_control`、`local_sensing`、`map_generator`/`mockamap` | 同源 uav_simulator，另有 `so3_disturbance_generator` | 继承 EGO 的 uav_simulator |
| GPU | 不需要（`local_sensing` 的 CUDA 深度渲染可选） | 不需要 | 不需要 |

---

## 2. 源码结构与关键模块

### 2.1 ego-planner-swarm（`src/planner/*`）

```text
plan_env/        GridMap（log-odds 占据 + 膨胀缓冲，局部更新）、raycast、obj_predictor（动态物体，默认未启用）
path_searching/  dyn_a_star：26 邻域 A*，只在碰撞段上调用
bspline_opt/     BsplineOptimizer（核心，1732 行）、UniformBspline、lbfgs.hpp
plan_manage/     EGOPlannerManager(reboundReplan) / EGOReplanFSM / traj_server / launch 参数
traj_utils/      Bspline.msg / MultiBsplines.msg、PolynomialTraj（"minSnap"）、plan_container
rosmsg_tcp_bridge/  跨机转发：TCP 8080 环链 MULTI_TRAJ、UDP 8081 广播 ODOM / ONE_TRAJ / STOP
drone_detect/    在深度图里找其他无人机，估计相对位姿误差
```

#### 2.1.1 `plan_env/grid_map.{h,cpp}`：GridMap

- **数据**：两份稠密一维数组，下标 `toAddress = x·Ny·Nz + y·Nz + z`（x-major）。
  - `occupancy_buffer_`：`vector<double>`，存 log-odds，**每个 voxel 8 B**；
  - `occupancy_buffer_inflate_`：`vector<char>`。
  - `posToIndex = floor((p − origin)/res)`；voxel 中心为 `(i + 0.5)·res + origin`。
- **更新**：
  - 深度图 + 位姿走 `projectDepthImage → raycastProcess`；log-odds 参数为 `p_hit=0.65, p_miss=0.35, p_min=0.12, p_max=0.90, p_occ=0.80`。
  - 更新后执行 `clearAndInflateLocalMap`，按 `ceil(inflation/res)` 做立方体膨胀。
  - 另有虚拟天花板 `virtual_ceil_height`。
- **点云模式** `cloudCallback`（L755）**不做 raycast**：
  1. 每帧先把局部盒 `camera_pos ± local_update_range` 清零；
  2. 再把盒内点按 xy 方向 `inf_step`、z 方向 ±1 膨胀后置 1。
  - 这种做法等于"只信最新一帧"，没有概率融合。
- **查询**：
  - `getInflateOccupancy(p)` 返回 −1（地图外）/0/1，是所有碰撞检查的唯一入口；
  - `getOdomDepthTimeout()` 用于"深度丢失 → 急停"。
- **默认参数**（`advanced_param.xml`）：`resolution 0.1`、`local_update_range 5.5×5.5×4.5`、`obstacles_inflation 0.099`、`max_ray_length 4.5`、`virtual_ceil 2.9`；仿真地图 42×30×5 m。

#### 2.1.2 `path_searching/dyn_a_star.{h,cpp}`：`AStar`

- **节点池**：预分配 `GridNodeMap_[100][100][100]`，共 10⁶ 个节点。池中心取 `(start + end)/2`，**步长在调用处写死为 0.1 m**（`AstarSearch(0.1, in, out)`）。所以它只能覆盖约 **10 m 的立方体**，超出就报 "Ran out of pool"。这是 EGO 只能做局部规划的硬约束。
- **免重置技巧**：`rounds` 戳记。每次调用 `++rounds_`，节点是否被访问过用 `node->rounds == rounds_` 判断。这样不必每次重置 10⁶ 个节点。**本文原型沿用了这个技巧。**
- **搜索**：
  - 26 邻域；
  - 启发式 `getDiagHeu`（3D octile）× `tie_breaker = 1 + 1e-4`；
  - 超时 0.2 s；
  - 起点或终点在障碍里时，会沿 start−end 方向往外挪。
- **小瑕疵（不要照抄）**：
  - `constexpr double inf = 1 >> 20;` 的值其实是 **0**。之所以没出错，是因为有 rounds 判断兜底。
  - 开放集里的节点 g 值变小时**没有重新入堆**（`priority_queue` 不支持 decrease-key），堆序会变陈旧。
  - 本文原型改成"重复入堆 + closed 惰性删除"。

#### 2.1.3 `bspline_opt/bspline_optimizer.cpp`：`BsplineOptimizer`（核心）

**变量**：均匀三次 B-spline（`order_=3`）的控制点 `Q ∈ R^{3×N}`。

- 前 3 个控制点固定，用来保证起始 p/v/a 连续。
- `rebound_optimize` 是"自由末端"：`end_id = cps_.size`，末端靠 `calcTerminalCost` 拉向 local target。

**代价**（`combineCostRebound`，L1672）：

```text
J = λ_s·J_smooth + λ_c'·J_dist + λ_f·J_feas + λ_c'·J_swarm + λ_c·J_terminal
λ_s=1.0  λ_c=0.5（λ_c' 每次 restart ×2）  λ_f=0.1  λ_fit=1.0（只在 refine 中使用）
```

| 项 | 函数（行号） | 公式 | 梯度 |
|---|---|---|---|
| 光滑（jerk） | `calcSmoothnessCost`（L935） | `Σ‖Q_{i+3} − 3Q_{i+2} + 3Q_{i+1} − Q_i‖²` | 系数 (−1, 3, −3, 1)·2j |
| **{p,v} 碰撞** | `calcDistanceCostRebound`（L864） | `d_ij = (Q_i − p_ij)·v_ij`，`e = s_f − d`：e ≤ 0 时为 0；0 < e < s_f 时为 e³；否则为 `3s_f·e² − 3s_f²·e + s_f³`（C² 过渡） | −3e²·v，或 −(6s_f·e − 3s_f²)·v |
| 可行性 | `calcFeasibilityCost`（L994，默认分支） | 逐轴计算 `v = (Q_{i+1} − Q_i)/ts`，超出 v_max 的部分取平方后乘 `1/ts²`；`a = (Q_{i+2} − 2Q_{i+1} + Q_i)/ts²`，超出 a_max 的部分取平方 | 见源码 |
| 终点 | `calcTerminalCost`（L976） | `‖(Q_{N−3} + 4Q_{N−2} + Q_{N−1})/6 − p_target‖²` | 系数 1/6、4/6、1/6 |
| **swarm** | `calcSwarmCost`（L776） | 见下 | 见下 |
| 拟合（refine） | `calcFitnessCost`（L905） | 各向异性：`(x·v)²/25 + ‖x×v‖²/1`。沿切向允许 5 倍偏差 | 见源码 |

swarm 项的计算方式：

- 控制点 `Q_i` 对应的时刻取 `t = t_now + (i−1)·ts`（i 从 `order_` 起）；
- 在他机轨迹上求值得到 `O`；
- 椭球距离 `ρ = sqrt((dx² + dy²)/1² + dz²/2²)`；
- 代价为 `(2·swarm_clearance − ρ)²`，仅在大于 0 时计入；
- **z 方向要求 2 倍间距**，用来防下洗。

**{p,v} 的生成**（`initControlPoints`，L434；`check_collision_and_rebound`，L1198）：

1. 以 `res/1.5` 为步长扫描控制多边形。只检查前 2/3 段，并用 `ENOUGH_INTERVAL=2` 做迟滞，找出穿障段 `[in_id, out_id]`。
2. 对每个穿障段，从 `Q_in` 到 `Q_out` 跑一次 A*。
3. 对段内每个 `Q_j`，以切向 `Q_{j+1} − Q_{j−1}` 为法向作平面，求它与 A* 路径的交点 `X`。然后令 `v = normalize(X − Q_j)`，从 `X` 往 `Q_j` 方向回走；遇到的第一个占据点再退一格，得到 `p`，也就是障碍表面点。
4. 段内没有得到 {p,v} 的控制点，复制邻居的 {p,v}。
5. 优化过程中，若迭代数 > 3 且平均光滑代价 < 0.1，就再做一次 `check_collision_and_rebound`。发现新的穿障点时，追加 {p,v}，并通过 `earlyExit` 中止 L-BFGS（`STOP_FOR_REBOUND`），然后重启优化，最多 rebound 20 次。
6. 收敛后，按 `res/速度` 步长采样轨迹前 2/3。若有碰撞，就 `λ_c' ×= 2` 并重来，最多 3 次。若碰撞发生在第一个 `ts` 内，直接判定失败。

**L-BFGS 参数**：rebound 阶段 `mem_size=16, max_iterations=200, g_epsilon=0.01`；refine 阶段 `g_epsilon=0.001`。

**拓扑多解**（`distinctiveTrajs`，L45）：

- 当 `use_distinctive_trajs=true` 时，对最多 `log₂8 = 3` 个穿障段各生成一个"反向"的 {p,v}；做法是沿 −v 向外搜索空闲点，最远 `5·CTRL_PT_DIST`。
- 共得到 2^k ≤ 8 条初值，分别优化后取代价最小的一条。
- 这相当于 Fast-Planner topo PRM 的廉价替代。

**小瑕疵**：`min_ellip_dist_` 记录的是所有 (i, id) 对里 `CLEARANCE − ρ` 的**最小值**，而且包括没有违规的对。所以 `min_ellip_dist_ > swarm_clearance_` 这个"swarm 太近就重启"的条件几乎不会触发，实际靠 FSM 的 `checkCollision` 兜底。移植时应改成最大违规量。

#### 2.1.4 `plan_manage/src/planner_manager.cpp`：`EGOPlannerManager::reboundReplan`（L49）

- **STEP1 INIT**，取 `ts = ctrl_pt_dist / max_vel × 1.5`（×1.5 是因为按满速算"太紧"）。初值有三种来源：
  - (a) 首次或新目标：`PolynomialTraj::one_segment_traj_gen`，即五次多项式，边界为 p/v/a。时长为梯形估计：`d ≥ v²/a` 时 `T = (d − v²/a)/v + 2v/a`，否则 `T = sqrt(d/a)`。
  - (b) 失败后：在中点插入随机点，扰动幅度随连续失败次数增大，再调用 `minSnapTraj`。
  - (c) 重规划：沿当前轨迹从 `t_cur` 起，用伪弧长以 `ctrl_pt_dist` 重采样，末端接一段五次多项式到 local target。点数不足 7 时，把 `ctrl_pt_dist` 除以 1.5 再采。
  - 最后调用 `UniformBspline::parameterizeToBspline`：K 个点加 4 个边界导数，构成 `A ∈ R^{(K+4)×(K+2)}`，行向量为 `[1 4 1]/6`、`[−1 0 1]/(2ts)`、`[1 −2 1]/ts²`，用 `colPivHouseholderQr` 求解。
- **STEP2 OPTIMIZE**：`initControlPoints` → 单解或多解 `BsplineOptimizeTrajRebound`。
- **STEP3 REFINE**：**只对 `drone_id ≤ 0` 生效**，swarm 模式下禁用，因为改时间会破坏广播轨迹的时间对齐。流程是 `checkFeasibility(ratio)` → `reparamBspline`（`lengthenTime`）→ `refine_optimize`（fitness 项）。
- `EmergencyStop`：6 个相同控制点，即原地悬停的 B-spline。
- `checkCollision(id)`：在两条轨迹的时间交集内以 0.03 s 步长采样，距离 < `swarm_clearance` 即判为冲突。

**注意**：`traj_utils/polynomial_traj.cpp::minSnapTraj` 名字叫 min-snap，但每段只有 **6 个系数（五次）**，Q 矩阵只取 `i, j ≥ 3` 的三阶导项。它实际上是 **minimum-jerk** 的闭式解（Richter 的 C/R 矩阵法）。

#### 2.1.5 `ego_replan_fsm.cpp`：`EGOReplanFSM`

- **状态**：`INIT → WAIT_TARGET → SEQUENTIAL_START → EXEC_TRAJ ⇄ REPLAN_TRAJ`，另有 `GEN_NEW_TRAJ` 和 `EMERGENCY_STOP`。
- **执行定时器**：`execFSMCallback`（L431）以 100 Hz 运行，重规划触发条件为：
  - 距上次规划超过 `thresh_replan_time = 1.0 s`；
  - 或者 local target 已到全局终点，且剩余距离 > `thresh_no_replan_meter = 1.0 m`。
- **安全定时器**：`checkCollisionCallback`（L677）以 20 Hz 运行。
  - 在当前轨迹 [t_cur, 2/3·T] 上以 0.01 s 步长检查占据，并检查他机距离（< swarm_clearance）。
  - 发现碰撞时，先尝试 `planFromCurrentTraj`。失败且碰撞点距现在 < `emergency_time = 1.0 s` 时进入 `EMERGENCY_STOP`，否则进入 `REPLAN_TRAJ`。
  - 深度超时 → 急停，并关闭 fail_safe。
- **`getLocalTarget`**（L894）：沿全局 min-jerk 轨迹找到第一个距离 ≥ `planning_horizon = 7.5 m` 的点作为 local target。如果离终点 < v²/(2a)，终点速度置 0，否则取全局轨迹上的速度。
- **目标类型**：1 = RViz 2D Nav Goal（强制 z = 1.0）；2 = 预设航点，最多 50 个。

#### 2.1.6 Swarm 通信与互避

- **顺序启动（环链）**：
  - drone_i 订阅 `/drone_{i−1}_planning/swarm_trajs`（`MultiBsplines`，包含 0..i−1 的全部轨迹）；
  - 收到之后，它才在 `SEQUENTIAL_START` 规划自己的首条轨迹，再把"0..i"的轨迹发给 i+1。
  - **本质就是按 drone_id 的优先级规划**。
- **运行期**：每次成功规划后，都向 `/broadcast_bspline` 发布单条轨迹。`BroadcastBsplineCallback`（L245）的处理规则：
  - 丢弃 `|now − start_time| > 0.25 s` 的轨迹，**所以需要时钟同步**；
  - 丢弃起点距本机 > `planning_horizon × 4/3 = 10 m` 的轨迹，只清空对应槽位；
  - `swarm_trajs_buf_[id]` 按 id 下标存放，下标必须等于 id；
  - 收下后立即做 `checkCollision(id)`，冲突则进入 `REPLAN_TRAJ`。
- **跨机 bridge**（`rosmsg_tcp_bridge/src/bridge_node.cpp`）：
  - TCP 8080 把 MULTI_TRAJ 转给 `next_drone_ip`；
  - UDP 8081 向 `broadcast_ip` 广播 ODOM（≤ `odom_max_freq=70` Hz）、ONE_TRAJ 和 STOP；
  - 报文是**原生内存 dump**：`enum` 4 B，`size_t` 8 B，packed。布局见 §3.10。

#### 2.1.7 `traj_server.cpp` 与 `uav_simulator`

- **`traj_server`**：
  - 以 100 Hz 对 B-spline 及其 1、2 阶导数做 de Boor 求值，发布 `PositionCommand{p, v, a, yaw, yaw_dot}`；
  - yaw 取 `atan2(p(t + time_forward) − p(t))`，其中 `time_forward = 1 s`，位移 < 0.1 m 时保持不变；
  - 限速 **π rad/s**，再做一次 0.5 的朴素低通；
  - 轨迹结束后悬停在终点。
- **`fake_drone/poscmd_2_odom.cpp`**：
  - 直接把指令 p/v 当作里程计输出；
  - 姿态由**微分平坦**得到：`z_B = normalize(a + g·e_z)`，`x_C = (cosψ, sinψ, 0)`，`y_C = (−sinψ, cosψ, 0)`，`x_B = normalize(y_C × z_B)`，`y_B = z_B × x_B`。
  - **这就是我们 Mock 渲染机体倾角的最省事做法。**
- **`so3_control/SO3Control.cpp`**：
  - `F = m·g·e_z + K_x(p_d − p) + K_v(v_d − v) + m·a_d`（另有小的加速度误差项）；
  - 限制推力与竖直方向的夹角；
  - `b3 = F/‖F‖`，`b2 = b3 × b1d`；
  - 默认增益 `kx = (5.7, 5.7, 6.2)`，`kv = (3.4, 3.4, 4.0)`，`kR = (1.5, 1.5, 1.0)`，`kOm = (0.13, 0.13, 0.1)`，`mass = 0.98`；
  - 与 r19 §3.7 的刚体模型互补。
- **`so3_quadrotor_simulator/Quadrotor.cpp`**：`v̇ = −g·e_z + T·R·e_3/m + F_ext/m − 阻力`。`external_force_` 可以直接接 **风力（V0.4）**。

#### 2.1.8 `ros2_version` 分支（`.cache/research/r25/ego-swarm-ros2`）

- 算法文件与 master 逐行对应（`diff -w` 只有 rclcpp API 差异和中文注释），`declare_parameter/get_parameter` 替代 `nh.param`。launch 改成 Python：`single_run_in_sim / swarm / swarm_large.launch.py`，带 `use_mockamap` 和 `use_dynamic` 参数。
- README 明确说**默认 FastDDS 会严重卡顿**，要求改用 `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`，依赖 Humble、PCL、VTK。
- 时钟一律用 `rclcpp::Clock().now()`，即**系统时钟，忽略 `use_sim_time`**。我们的时间轴支持 ×N 倍速（§39），和它冲突（见 §6）。

### 2.2 Fast-Planner（`fast_planner/*`）

#### 2.2.1 `path_searching/kinodynamic_astar.cpp`：`KinodynamicAstar::search`

- **状态与扩展**：
  - 状态 `x = [p, v] ∈ R⁶`，转移 `stateTransit`：`p' = p + vτ + ½uτ²`，`v' = v + uτ`；
  - 输入离散为 `u ∈ {−a, −a/2, 0, a/2, a}³`，共 125 个；
  - `τ = max_tau = 0.6 s`（`time_res = 1` 时每步只有一个时长）；
  - 首次扩展改用当前加速度，时长 `init_max_tau = 0.8` 的 1/20 到 20/20。
- **代价**：`g += (‖u‖² + w_time)·τ`，其中 `w_time = 10`。
- **启发式** `estimateHeuristic`：双积分器 OBVP 的闭式代价。
  - `J(T) = 12‖Δp‖²/T³ − 12(v₀ + v₁)·Δp/T² + 4(‖v₀‖² + v₀·v₁ + ‖v₁‖²)/T + ρT`；
  - 对 `dJ/dT = 0` 这个四次方程求根（`quartic` / `cubic` 为 Ferrari / Cardano 解法），并以 `T ≥ ‖Δp‖∞ / (0.5·v_max)` 为下界，取最小值；
  - 最后乘以 `λ_heu = 5`（加权 A*）。
- **终止**：
  - 到达终点邻域（±1/res 个格子）时尝试 `computeShotTraj`：三次多项式一步解析连接，检查占据，成功返回 `REACH_END`；
  - 离起点超过 `horizon = 7 m` 时返回 `REACH_HORIZON`。
- **剪枝**：同一父节点扩展出的子节点落在同一 voxel 时，只保留 f 值更小的那个。
- **数据结构**：节点池 `allocate_num = 100000`；`NodeHashTable` 是 `unordered_map<Vector3i / Vector4i>`，动态模式下 key 带时间索引；每个基元做 `check_num = 5` 次采样碰撞检查。
- **输出**：`getSamples` 按 `ts = ctrl_pt_dist / v_max` 采样，并给出边界 v/a。

#### 2.2.2 其余模块

- **`plan_env/sdf_map.cpp::updateESDF3d`**：
  - 依次沿 z → y → x 做三遍 `fillESDF`，即 Felzenszwalb–Huttenlocher 1D 平方距离变换（下包络抛物线）；
  - 正、负距离各算一次，合成**有符号** ESDF；
  - `getDistWithGradTrilinear` 用 8 个邻域 voxel 做三线性插值，同时给出梯度。
  - Python 里等价于 `scipy.ndimage.distance_transform_edt`（同一算法族）。
- **`bspline_opt/bspline_optimizer.cpp`**：
  - 代价码 `SMOOTHNESS | DISTANCE | FEASIBILITY | ENDPOINT | GUIDE | WAYPOINTS`；
  - 距离项为 `Σ (d(Q_i) − d₀)²·[d < d₀]`，梯度 `2(d − d₀)·∇d / ‖∇d‖`；
  - 可行性项为 `(v_j² − v_m²)²`（四次型）；
  - 参数 `λ1 = 10, λ2 = 5, λ3 = 1e−5, λ4 = 0.01, λ7 = 100, d₀ = 0.4`；
  - 控制点有 ±10 m 的 box 约束；
  - NLopt 调用约束 `maxeval / maxtime`，例如 `max_iteration_time2 = 5 ms`。
- **`bspline/non_uniform_bspline.cpp::reallocateTime`**（L220）：
  - 对超速或超加速度的**局部区间**，只拉伸相应的 knot 区间；
  - 比例 `ratio = min(v_max_i / v_lim, limit_ratio = 1.1)`，加速度则用开方；
  - `kinodynamicReplan` 里最多迭代 3 次，`tn/to > 3` 时报错。
  - 比 EGO 的整体 `lengthenTime` 更精细，但 knot 会变得不均匀。
- **`plan_manage/src/planner_manager.cpp::planYaw`**（L607）：
  - 以 `dt_yaw = 0.3 s` 分段，每段取前视 2 s 的方向角作为 WAYPOINTS 约束，经 `calcNextYaw` 做 unwrap；
  - 边界状态到控制点用 **`states2pts = [[1, −dt, dt²/3], [1, 0, −dt²/6], [1, dt, dt²/3]]`**，这个公式本文 §3.4 复用，已数值验证。
- **`topo_prm.cpp`**：可见性 PRM（guard / connector）→ `pruneEquivalent`（同伦判定）→ `shortcutPaths` → 每条拓扑路径作为 GUIDE 做并行优化 → `selectBestTraj`。
- **FSM**：`kino_replan_fsm.cpp` 的状态为 `INIT / WAIT_TARGET / GEN_NEW_TRAJ / REPLAN_TRAJ / EXEC_TRAJ / REPLAN_NEW`，`thresh_replan = 1.5`，`thresh_no_replan = 2.0`。

### 2.3 Fast-LIO2_Ego-Planner（`src/*`）

**数据链路**：

```text
Mid360 ──livox_ros_driver2(CustomMsg /livox/lidar, /livox/imu)
   ├─ FAST_LIO_SLAM/FAST-LIO (config/mid360.yaml: lidar_type=1, scan_line=4, blind=0.5, fov=360, det_range=100,
   │                          extrinsic_T=[-0.011,-0.02329,0.04412], R=I) → /Odometry, /cloud_registered(_body)
   ├─ FAST_LIO_SLAM/SC-PGO（ScanContext 回环 + GTSAM 位姿图）
   ├─ livox2pointcloud → /livox/pointcloud2（传感器系 PointCloud2）
   └─ ego-planner-swarm-v1 (plan_env 编译宏 USE_MID360_CLOUD) + traj_server + foxglove_bridge
```

**相对上游的改动**（逐文件 diff）：

- **`ego_replan_fsm.cpp`**：新增两种目标类型。
  - `REMOTE_TARGET = 4`：订阅 `/swarm_command`，格式为 `Float32MultiArray [drone_id, x, y, z]`，与上次目标相距 > 0.5 m 才触发；
  - `REMOTE_START = 5`：订阅 `/swarm/ego_trigger`，格式为 `[n, id1, …]`，收到后按预设航点起飞。
  - **这正好是我们 Gateway 向真机下发目标的最小协议。**
- **`grid_map.cpp`**（`USE_MID360_CLOUD`）：
  - 新增 `cloudOdomCallback`，用 `ApproximateTime` 同步点云与 `/Odometry`，然后变换到世界系，走 raycast + log-odds；
  - **问题一**：它只保留 `pt.x > 0.1` 的点，把 360° 雷达当成前向相机用，丢掉了后半球；
  - **问题二**：假设 LiDAR 系等于 body 系，忽略了外参。
  - 另外，`real_env/swarm_all_in_one.launch` 把 `depth_topic` 指向深度图话题，而 `cloud_topic=/livox/pointcloud2`。实际生效的是**无 raycast 的 `cloudCallback`**，配置自相矛盾。
- **`rosmsg_tcp_bridge`**：重构为 `msg_base<T>` 模板，新增 `TAKEOFF / LAND / COMMAND / POINTCLOUD(_SEGMENT)` 报文和 master/slave 模式。本机发布 `/broadcast_bspline`，他机轨迹回灌到 `/broadcast_bspline2`，避免回环。
- 参数 `param.xml`：`res 0.10`、`inflation 0.199`、`max_ray 5`、`virtual_ceil 2.0`、`v = a = 1.0`，其余同上游。
- `drone_detect.cpp`：在深度图上**写死像素矩形置零**，用来遮挡机体。纯现场补丁，没有复用价值。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

> 以下都在 `.cache/research/r25/proto/` 里跑通过。机器为 8 核 CPU，无 GPU。numba 0.67、scipy 1.18 装在 `.cache/research/r25/pylib`，**没有动项目 venv**。

### 3.1 规划栅格构建（World → Planning Grid）

实测数据（Shenzhen，5M 点）：

| 分辨率 | 维度（高度带 地面−5 ~ +300 m） | voxel 数 | 原始占据 | DSM 空洞率 | 全图 EDT 耗时 / 内存 |
|---|---|---|---|---|---|
| 4 m | 463×500×77 | 17.8M | 2.3%（挤出后 4.1%） | 0.2% | **5.6 s** / 70 MB（f32） |
| 2 m | 925×1000×153 | 141.5M | 1.1% | 9.5% | 54 s / 566 MB |
| 1 m | 1849×2000×305 | 1128M | 0.3% | **45.6%** | 不可行 |

```python
def build_plan_grid(P, res=4.0, band=(-5, 300), r_infl=6.0):
    ground = percentile(P.z, 5)                 # 地面估计；有 DEM/语义时直接用
    lo = P.min(0); lo.z = ground + band[0]
    idx = floor((P - lo) / res)                 # 保留 0 <= iz < nz 的点
    occ = zeros(nx, ny, nz, bool); occ[idx] = True
    dsm = full((nx, ny), -1); maximum.at(dsm, (ix, iy), iz)
    dsm = grey_closing(dsm, 3x3)                # 补屋顶稀疏空洞
    occ |= arange(nz)[None, None, :] <= dsm[..., None]   # 2.5D 挤出：屋顶以下实心（城市无悬挑时成立；桥/树冠可语义排除）
    esdf = distance_transform_edt(~occ) * res   # 米；到最近原始障碍
    occ_infl = esdf <= r_infl                   # 膨胀 = ESDF 阈值，不用再做形态学
    save(worlds/<id>/geometry/voxel/plan_r{res}.npz, occ_infl(bitpack), esdf(f16), dsm(i16), origin, res, ground)
```

**参数建议：**

| 场景 | res | r_infl | 说明 |
|---|---|---|---|
| UrbanScene3D 演示 | 4 m（全局）；2 m（局部窗） | 6–8 m | 城市演示用 |
| P600 真实 MID-360 地图 | 0.2–0.5 m（局部 100×100×40 m 窗） | 0.6–1.0 m | 与 Prometheus 的 0.15 m / 0.6 m 对齐 |

**局部窗的做法**：以轨迹走廊（A* 路径 ± 20 m）为范围裁出子块，按 2 m 重新体素化并做 EDT。子块在 10⁶ 量级时，EDT 约 0.3 s，可以缓存。

### 3.2 3D A*（numba，round-stamp 节点池）

```python
class AStarPool:                        # 进程级单例，按世界栅格维度预分配一次
    g: f64[N]; parent: i64[N]; stamp: i32[N]; closed: i32[N]; heap_k: f64[4M]; heap_v: i64[4M]; round: int

@njit
def astar(occ, esdf, s, g, w_heu=1.5, w_cost=2.0, d_safe=12.0, max_expand=2e6, zmax=-1, pool...):
    round += 1; stamp[s] = round; g[s] = 0; push(s, w_heu * octile3(s - g))
    while heap:
        cur = pop()
        if closed[cur] == round: continue            # 惰性删除（替代 decrease-key）
        closed[cur] = round
        if cur == g: return reconstruct(parent)
        for d in 26 neighbours:
            n = cur + d
            if out_of_bounds(n) or n.z >= zmax or occ[n]: continue
            if stamp[n] != round: stamp[n] = round; g[n] = inf; closed[n] = 0
            elif closed[n] == round: continue
            c = |d|                                    # 1, √2, √3
            if w_cost > 0 and esdf[n] < d_safe: c += w_cost * (d_safe - esdf[n]) / d_safe   # 远离障碍的软代价
            if g[cur] + c < g[n]: g[n] = g[cur] + c; parent[n] = cur; push(n, g[n] + w_heu * octile3(n - g))
octile3(dx, dy, dz): 排序得 a >= b >= c，结果 = (√3 − √2)·c + (√2 − 1)·b + a        # 等价于 EGO getDiagHeu
```

实测（4 m 全城栅格，13 次随机 OD，距离 184–1858 m）：

| 设置 | 扩展节点数 | 耗时 |
|---|---|---|
| `w_heu = 1.0`（最优） | 77–62k | 0.7–124 ms |
| `w_heu = 1.5` | 44–5.5k | **0.1–10 ms**（端到端基准中低空长航线加剪枝，最长 160 ms） |
| 纯 Python heapq（对照） | 45.8k | **5.83 s**（约 8k expansions/s），只能用于教学 |

- 路径长度比最优长 0–8%。
- 不用 round-stamp 时，每次查询要分配 17.8M 规模的数组，额外开销 650 ms。
- `zmax` 用来模拟限高：设 32 m 时迫使路径绕楼而不是爬升。**建议作为请求约束 `alt_max`。**

### 3.3 LOS 剪枝（后处理版 lazy Theta*）

```python
@njit
def los(occ, a, b):                     # Amanatides–Woo 3D DDA，体素中心到体素中心
    t_max = 0.5 / |d| per axis; t_delta = 1 / |d|
    loop: if occ[x, y, z]: return False
          if (x, y, z) == b: return True
          沿 t_max 最小的轴步进
def shortcut(occ, path):                 # 贪心：从 i 出发找最远可见 j
    out = [path[0]]; i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1 and not los(occ, path[i], path[j]): j -= 1
        out.append(path[j]); i = j
```

实测：110–405 个栅格点剪到 2–17 个拐点，耗时 < 10 ms。剪枝后的折线就是 B-spline 的初值骨架，也可以直接作为 UI 的"航点"展示。

### 3.4 B-spline 初始化与优化（EGO 契约 + Fast-Planner ESDF 代价）

**均匀三次 B-spline（矩阵形式，与 `UniformBspline` 等价）**：

```text
区间 i、局部参数 s = t/ts − i ∈ [0,1)：
p(t) = [1 s s² s³]·M·[Q_i..Q_{i+3}]ᵀ
M = 1/6·[[1,4,1,0],[−3,0,3,0],[3,−6,3,0],[−1,3,−3,1]]
v 取 [0,1,2s,3s²]/ts，a 取 [0,0,2,6s]/ts²，j 取 [0,0,0,6]/ts³
定义域 [0, (N−3)·ts]
knot 数组约定：u_k = (k − p)·ts（EGO setUniformBspline），与 msg 的 knots 兼容
导数控制点：V_i = (Q_{i+1} − Q_i)/ts，A_i = (Q_{i+2} − 2Q_{i+1} + Q_i)/ts²（凸包性质 ⇒ 用它们做保守的可行性检查）
```

**O(1) 初值（替代 lstsq）**。直接用 Fast-Planner 的 `states2pts` 由边界状态确定首尾各 3 个控制点：

```text
Q0 = p0 − ts·v0 + ts²/3·a0
Q1 = p0 − ts²/6·a0
Q2 = p0 + ts·v0 + ts²/3·a0          ⇒ p(0) = p0，v(0) = v0，a(0) = a0（已数值验证）
中间控制点 = 剪枝折线按 ctrl_pt_dist 等弧长重采样
```

- 对照：EGO 的 `parameterizeToBspline` 是稠密 QR，K = 386 时要 700 ms，所以不要照搬；如果确实需要插值，改用带状求解。
- 静止起点或终点时，`v0 = a0 = 0`，三个控制点重合；悬停状态（`EmergencyStop`）也是这样。

**优化**（变量为中间控制点，首尾各 3 个固定；numba 代价 + `scipy.optimize.minimize(method='L-BFGS-B', jac=True)`）：

```python
J = w_s·Σ‖Q_{i+3} − 3Q_{i+2} + 3Q_{i+1} − Q_i‖²                   # 光滑（jerk）
  + w_d·Σ_{i∈free} (d(Q_i) − d0)²·[d(Q_i) < d0]                    # ESDF 三线性插值；梯度用 2(d − d0)·∇d/‖∇d‖
  + w_f·Σ_axis [ max(|V|−vmax, 0)²/ts² + max(|A|−amax, 0)² ]       # 逐轴、控制点凸包
  + w_sw·Σ_i Σ_other max(0, C − ρ(Q_i − O(t_i)))²,  t_i = t0 + (i−1)·ts   # EGO swarm 椭球项（§3.9）
ts = ctrl_pt_dist / vmax × 1.5
后处理：ratio = max(max|V|/vmax, sqrt(max|A|/amax), 1)；ts ← ts·ratio（整体拉伸，即 EGO lengthenTime 的均匀版）
验收：以 0.1 s 采样全轨迹，查 occ_infl，有碰撞时 w_d ×2 重试（最多 3 次），仍失败就返回 A* 折线 + 速度规划（§3.5）作为降级
```

**城市级默认参数**（实测可用）：`ctrl_pt_dist = 6 m`、`vmax = 10`、`amax = 3`、`w_s = 1`、`w_d = 5`、`w_f = 0.1`、`d0 = 10 m`（当 `r_infl = 6 m` 时）、`maxiter = 200`、`maxcor = 16`。

**尺度无关化（建议）**：先把控制点除以 `ctrl_pt_dist` 再优化。这样各项代价与场景尺度无关；同一组权重既能用于 0.4 m（EGO 室内），也能用于 6 m（城市）。

实测（8 条城市航线，248–2835 m，45–390 个控制点，**单线程 BLAS**）：

| 指标 | 结果 |
|---|---|
| 优化耗时 | **25–78 ms**（200 次迭代） |
| 速度峰值 \|v\| | 9.4–9.8 m/s |
| 加速度峰值 \|a\| | 1.6–2.1 m/s² |
| 可行性 ratio | 1.00 |
| 采样碰撞 | 0 |
| 最小净空 | 由初值的 6.2–8.0 m 提升到 8.0–9.6 m |

多线程 BLAS 下同一任务要 870–1710 ms，原因见 §6。

### 3.5 速度规划（TOPP-lite，用于 FOLLOW_PATH / 降级路径）

```python
def velocity_profile(P, vmax, a_tan, a_lat, v0=0, v1=0, vz_max=None):
    s = cumulative arc length; κ = ‖P' × P''‖ / ‖P'‖³
    vlim = min(vmax, sqrt(a_lat / κ))                      # 转弯限速
    if vz_max: vlim = min(vlim, vz_max / |dz/ds|)          # 爬升率限速
    v = vlim; v[0] = min(v[0], v0); v[-1] = min(v[-1], v1)
    for i in 1..n-1:  v[i] = min(v[i], sqrt(v[i-1]² + 2·a_tan·Δs))      # 前向（加速）
    for i in n-2..0:  v[i] = min(v[i], sqrt(v[i+1]² + 2·a_tan·Δs))      # 反向（减速）
    t = cumsum(Δs / mean(v_i, v_{i+1}))
```

- 验证用例：100 m 直线接 R = 30 m 半圆，爬升 10 m，参数 `a_lat = 3`、`a_tan = 2`、`vz_max = 2`。
- 结果：弧段 v = 9.54 m/s（理论值 √90 = 9.49），\|dv/dt\|max = 2.00，全程 T = 25.6 s。
- P600 建议值：`vmax = 10–12 m/s`，`a_tan = 3`，`a_lat = 4`（约 22° 倾角），`vz_max = 3`（上升）/ `2`（下降）。

### 3.6 五次最小 jerk 单段（GoTo / 初值 / 重规划衔接）

```text
由 6 个边界条件 {p0, v0, a0, p1, v1, a1} 与时长 T 求 c5..c0
（EGO one_segment_traj_gen：6×6 线性系统，逐轴求解）
T 的梯形估计：d ≥ v²/a 时 T = (d − v²/a)/v + 2v/a，否则 T = 2·sqrt(d/a)
（EGO 源码此处写的是 sqrt(d/a)，只有一半，偏乐观）
```

- 多航点的闭式 min-jerk/min-snap 可移植 `PolynomialTraj::minSnapTraj`（C/R 矩阵，约 150 行）。
- 注意它实际是 **min-jerk**；要做真正的 min-snap，需要每段 8 个系数（七次）。
- MVP 的航点任务推荐"A* + 剪枝 + B-spline"或"折线 + §3.5 速度规划"。

### 3.7 Yaw 规划

- **MVP**：照搬 `traj_server::calculate_yaw`。
  - 目标 yaw = `atan2(p(t + 1 s) − p(t))`；
  - 限速 π rad/s，用 `wrap(Δψ)` 处理 ±π 跳变；
  - 做一次 0.5 低通；
  - 悬停时保持原值。
- **进阶**（V0.6，FPV / 相机任务）：Fast-Planner `planYaw`。以 `dt_yaw = 0.3 s`、前视 2 s 的方向作为 WAYPOINTS，λ7 = 100，做一维 B-spline 优化；需要注视目标时换成 `atan2(target − p)`。

### 3.8 Mock 端的 receding-horizon FSM（EGO 语义精简版）

```text
状态：IDLE → PLANNING → EXEC ⇄ REPLAN；EMERGENCY（悬停 B-spline）
EXEC 每 tick：t = t_sim − traj.t0
  - t > T − 0.01 且已到终点 → IDLE（HOVER）
  - (t > replan_period=1.0 s 且距终点 > 1·horizon) 或 地图/他机轨迹版本变化 → REPLAN
safety_check @20 Hz：采样 [t, min(T, t + 2/3·horizon_time)]，步长 0.05 s
  - 占据命中，或与他机距离 < clearance：先 REPLAN（从当前轨迹的 p/v/a 出发，即 planFromCurrentTraj）
  - 失败且碰撞点 < emergency_time=1.0 s → EMERGENCY（6 个重合控制点）
全局模式（先验地图）：horizon = ∞（一次规划整条），只在 他机/动态障碍/地图更新 时 REPLAN
局部模式（V0.6 "未知环境"演示）：只把 sensing_radius（例如 40 m）内的 occupancy 揭示给规划器，horizon = 1.5×sensing_radius（EGO：7.5 = 1.5×5）
```

### 3.9 多机互避（三层）

**(1) 战略层：优先级 + 4D 预约**（计划阶段，Mission 下发时运行）

```python
cands = sorted([(delay, dz) for delay in 0..60 s step 1 for dz in (0, +10, −10, +20)], key=delay + 0.5·|dz|)
for agent in sorted(agents, key=priority):            # 优先级：任务等级 > 电量低 > 先到先得（EGO SEQUENTIAL_START 即 drone_id 顺序）
    for (delay, dz) in cands:
        X = plan(agent, alt_offset=dz) 按 dt=0.1 采样
        if all(not conflict(X, t0 + delay, Y, tY) for Y in reserved): reserve(X, t0 + delay); break
conflict: 取两机都在空中的时间交集，ρ = ‖(Δx, Δy, Δz/2)‖ < clearance（EGO 椭球，z 两倍）
```

实测：

| 场景 | 仅用延迟 | 延迟 + 高度层 |
|---|---|---|
| 20 架对心环 | 总延迟 370 s，max 37 s，145 ms | **总延迟 59 s，max 9 s**，115 ms |
| 100 架随机 OD（同一高度） | 40 架被延迟，max 12 s，0.70 s | **27 架被延迟，max 4 s，0.63 s** |

降级：用 EGO 的 swarm 代价重新优化轨迹（`w_sw`，`C = 2 × swarm_clearance`，椭球 a = 2（z）、b = 1），代价是一次额外优化，约 50 ms。

**(2) 轨迹层**：规划时把已预约的他机轨迹当作时变障碍，带入 §3.4 的 `w_sw` 项。

**(3) 战术层：ORCA-3D**（运行期，Mock 20–50 Hz；`orca3d.py`，移植自 RVO2-3D 的 `computeNewVelocity` 和 `linearProgram1–4`）

```text
对 k=10 个近邻（≤ 40 m）逐一构造 ORCA 平面（rel = pB − pA，vrel = vA − vB，R = rA + rB，τ = 4–6 s）：
  未碰撞：w = vrel − rel/τ
    若 w·rel < 0 且 (w·rel)² > R²‖w‖² → 截断球：n = ŵ，u = (R/τ − ‖w‖)·ŵ
    否则 → 锥面：t = (rel·vrel + sqrt(disc)) / ‖rel‖²；ww = vrel − t·rel；n = ŵw；u = (R·t − ‖ww‖)·ŵw
  已碰撞：w = vrel − rel/Δt；n = ŵ；u = (R/Δt − ‖w‖)·ŵ
  平面：point = vA + u/2，normal = n                                      （互惠各承担一半）
求解：lp3（‖v‖ ≤ vmax 球内，最小化 ‖v − v_pref‖）；不可行时 lp4（最小化最大违规）
v_pref = 指向当前轨迹参考点的速度（跟踪 B-spline），不是直接指向终点
```

实测（dt = 0.05，r = 1 m，margin 0.5 m，amax = 4 m/s²）：

| 场景 | min_sep（要求 ≥ 2r） | 违规 pair·tick | 耗时 |
|---|---|---|---|
| 100 架随机 OD，3 个高度层 | **3.00** | 0 | 1.6 ms/tick |
| 300 架随机 OD | 1.89 | 3 | 15.8 ms/tick（kNN O(N²) 占主导） |
| 50 架对心环，不限加速度 | 1.83 | 0 | 0.9 ms/tick |
| 50 架对心环，限加速度 4–7 m/s²（各种 margin/τ/Δv 变体） | **0.20–0.72** | 149–413 | ≈1 ms/tick |

- 结论：ORCA 假设速度能瞬时改变，**加速度受限的高密度汇聚场景必须靠 (1)(2) 事先消解**，ORCA 只处理残余偏差。
- 在 Δv 空间求解（以当前速度为球心、半径 `amax·Ta`）也不能解决这个问题，实测 min_sep 0.27–0.36。
- 工程参数：`r_eff = r_body + 0.5 m`、`τ = 5 s`、`k = 10`、邻居半径 40 m；z 方向不做缩放。实测 z 缩放 = 2 反而更差（0.05 m），因为 ORCA 的推导基于各向同性度量。
- 残余风险兜底：`d < r_eff` 时，低优先级一方执行"刹停 + 垂直错层（±3 m）"。

**(4) 虚实混合**：见 §3.10。真实 EGO 无人机把虚拟机当作普通 swarm 成员来避让。

### 3.10 EGO 跨机报文（Python 编解码，已逐字节验证）

```text
UDP 8081 广播（little-endian，packed；enum = int32；size_t = uint64）：
  ONE_TRAJ = 890：i32 type | i32 drone_id | i32 order(=3) | f64 start_time(ROS 秒) | i64 traj_id | f64 yaw_dt
                   | u64 nK | f64[nK] knots | u64 nP | f64[3nP] pos_pts | u64 nY | f64[nY] yaw_pts
  ODOM = 888：u64 len + child_frame_id("drone_<id>") | u64 len + frame_id | u32 seq | f64 stamp | 7×f64 pose | 36×f64 cov | 6×f64 twist | 36×f64 cov
  STOP = 891：仅 type
TCP 8080 环链：MULTI_TRAJ = 889：i32 type | i32 drone_id_from | u64 n | n×(ONE_TRAJ 去掉 type 字段)
```

`codec.py`（`enc_one_traj / dec_one_traj / enc_multi`）与按 `serializeOneTraj` 原样复刻的 C++ 输出**逐字节一致**（284 B 样例）。

注入虚拟机时的约束：

- `start_time` 必须是接收方时钟的"现在"，误差 ≤ 0.25 s；
- 起点距真机 ≤ 10 m 才会被接收；
- 虚拟机的 `drone_id` 必须和真机不冲突，而且真机缓冲区会扩到 `max_id + 1`；
- 坐标要换到**真机的 odom 世界系**。每架真机都需要 `T_world←local_i`，见 r19 §2.5。

### 3.11 仅作参考：kinodynamic A*

它的价值在于"高速中途重规划时，初值自带动力学可行性"。我们的服务端可以用 `states2pts` 保留当前的 p/v/a，再配合 §3.4 的优化。所以 **MVP 不移植**。V1.0 若出现 30 m/s 级的固定翼或复合翼，再评估移植 OBVP 启发式（§2.2.1 的公式约 40 行）。

---

## 4. 在本项目中的落点与复用方式

### 4.1 (a) 服务端 Planning 模块接口

**目录**（对应 §42 的 `swarm/planning`、`swarm/avoidance`，放在 simulator 服务里）：

```text
apps/simulator/planning/
  grid.py        PlanGrid：构建/加载 plan_r{res}.npz；is_occupied / distance(+grad) / local_window / version
  search.py      AStarPool（numba）、los、shortcut
  bspline.py     eval / derivative / states2pts / BsplineOptimizer（smooth + esdf + feas + swarm）/ time stretch
  velocity.py    TOPP-lite
  fsm.py         ReplanFSM（§3.8）
  deconflict.py  优先级 + 4D 预约（§3.9-1）
  orca.py        ORCA-3D（numba）
  backends/ego_ros2.py  V0.6：/drone_i/goal ↔ ego_planner(ros2_version)，订阅 planning/bspline
  backends/ego_udp.py   V0.6：§3.10 编解码，虚实混合
  api.py         FastAPI 路由 + WS 消息
```

**Python 协议：**

```python
class OccupancyMap(Protocol):
    res: float; origin: np.ndarray; shape: tuple[int, int, int]; version: int
    def occupied(self, p: np.ndarray) -> np.ndarray: ...                              # (n,3) → bool(n)
    def distance(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...         # ESDF(m) 与 ∇
    def window(self, lo: np.ndarray, hi: np.ndarray, res: float) -> "OccupancyMap": ...

@dataclass
class BoundaryState: p: Vec3; v: Vec3 = 0; a: Vec3 = 0; yaw: float | None = None

@dataclass
class PlanConstraints:
    v_max=10.0; a_max=3.0; vz_up=3.0; vz_down=2.0; yaw_rate=math.pi
    clearance=6.0; alt_min=20.0; alt_max=120.0; geofence: list[Polygon] = ()
    no_fly: list[Volume] = ()

@dataclass
class PlanRequest:
    agent_id: str; start: BoundaryState; goal: BoundaryState; waypoints: list[Vec3] = ()
    constraints: PlanConstraints; t0_sim: float
    planner: Literal["astar_bspline", "polyline", "ego_ros2"] = "astar_bspline"
    swarm: Literal["none", "priority", "cost"] = "priority"; priority: int = 0
    time_budget_ms: int = 300; params: dict = {}   # 高级参数覆盖（§3 的参数表）

class TrajectoryPlanner(Protocol):
    def plan(self, req: PlanRequest, world: OccupancyMap, reserved: list["TrajRef"]) -> "PlanResult": ...
```

**REST / WS 契约**（坐标一律为 World ENU，米；时间一律为 `t_sim` 秒）：

```jsonc
// POST /api/v1/worlds/{world_id}/plan      （Mission 批量规划：POST /api/v1/worlds/{world_id}/plan:batch）
{ "agent_id": "p600-01", "t0_sim": 123.4,
  "start": {"p": [x,y,z], "v": [0,0,0]}, "goal": {"p": [x,y,z]}, "waypoints": [],
  "constraints": {"v_max": 10, "a_max": 3, "clearance": 6, "alt_max": 120},
  "planner": "astar_bspline", "swarm": "priority", "time_budget_ms": 300 }
// 200
{ "status": "ok",   // ok | degraded(折线+速度规划) | no_path | timeout | start_in_collision | goal_in_collision | infeasible
  "traj": { "traj_id": 17, "type": "bspline", "order": 3, "ts": 0.9, "t0": 123.4, "duration": 252.0,
            "ctrl_pts": "<base64 f32[N*3]>", "yaw": {"mode": "lookahead", "t_fwd": 1.0} },
  "path": { "astar": "<base64 f32>", "pruned": [[x,y,z], ...] },
  "stats": { "length_m": 1960, "v_peak": 9.7, "a_peak": 1.8, "min_clearance_m": 9.6,
             "t_search_ms": 12, "t_opt_ms": 78, "expanded": 5466, "feas_ratio": 1.0,
             "delay_s": 0, "alt_offset_m": 0 },
  "conflicts": [] }
```

- WS `traj.update`（服务端 → 浏览器）：发生重规划时推送同样的 `traj` 结构，渲染方式见 r19 §3.10。
- WS `plan.progress`：用于 batch，推送进度。
- 可选 WS `plan.debug`：包含 ESDF 切片和 A* 扩展点，给 DebugLayer 用。
- 与 EGO `Bspline.msg` 一一对应（`order / traj_id / start_time / knots / pos_pts / yaw_pts`），ROS2 后端可以直接转换。

### 4.2 (b) MVP 规划器端到端伪代码

```python
def plan_astar_bspline(req, world, reserved, P=DEFAULTS):
    threadpool_limits(1)                                     # 关键，见 §6
    s, g = world.w2i(req.start.p), world.w2i(req.goal.p)
    if world.occ[s]: s = nearest_free(s)        # 沿 s−g 方向外推（EGO ConvertToIndexAndAdjustStartEndPoints）
    if world.occ[g]: return status("goal_in_collision", suggest=nearest_free(g))
    zmax = world.z_index(req.constraints.alt_max)
    exp, cost = POOL.search(world.occ, world.esdf, s, g, P.w_heu, P.w_cost, P.d_safe, P.max_expand, zmax)
    if not finite(cost): return status("no_path")
    pts = resample(world.i2w(shortcut(world.occ, extract(...))), P.ctrl_pt_dist)
    for dz in candidate_alt_offsets(req):                    # 战略层（§3.9-1）
        ts = P.ctrl_pt_dist / c.v_max * 1.5
        Q0 = [states2pts(req.start, ts), pts[1:-1] + dz, states2pts(req.goal, ts)]
        Q  = lbfgs(cost_nb, Q0, fixed=head3 + tail3, others=reserved)       # §3.4
        ts *= feas_ratio(Q, ts, c)
        if sample_ok(Q, ts, world): break
    else: return degraded(polyline=pts, profile=velocity_profile(pts, c))    # §3.5
    delay = first_conflict_free_delay(Q, ts, reserved)       # §3.9-1
    return ok(traj=Bspline(Q, ts, t0=req.t0_sim + delay), stats=...)
```

**默认参数表：**

| 参数 | 城市演示（UrbanScene3D） | P600 真实地图（V0.5+） | EGO 原值 |
|---|---|---|---|
| grid res / 膨胀 | 4 m / 6 m | 0.2 m / 0.8 m | 0.1 / 0.099 |
| A* `w_heu`、`w_cost`、`d_safe` | 1.5、2.0、12 m | 1.5、2.0、3 m | 1.0001 / – |
| `ctrl_pt_dist` | 6 m | 0.5 m | 0.4 |
| `v_max` / `a_max` | 10 / 3 | 3–5 / 2 | 2.0 / 3.0 |
| `w_s`/`w_d`/`w_f`/`d0` | 1 / 5 / 0.1 / 10 m | 1 / 5 / 0.1 / 1.0 m | 1 / 0.5 / 0.1 / 0.5 |
| swarm clearance（xy; z） | 4 m; 8 m | 1.0; 2.0 m | 0.5 → C = 1.0（z 两倍） |
| `replan_period` / `safety_hz` / `emergency_time` | 1 s / 20 Hz / 1 s | 同左 | 同左 |
| `time_budget_ms` | 300 | 100 | –（A* 超时 0.2 s） |

### 4.3 版本落点与复用清单

| 能力 | 来源 | 目标模块 | 复用方式 | 版本 | MVP |
|---|---|---|---|---|---|
| 轨迹契约（B-spline：order 3、ts、ctrl_pts、t0） | EGO `Bspline.msg`、`UniformBspline` | `planning/bspline.py`、WS `traj.update`、前端 MissionLayer | port | V0.2 | 是 |
| 规划栅格：体素 + DSM 挤出 + ESDF | Fast-Planner `sdf_map`（EDT）、EGO `GridMap`（膨胀/天花板） | `planning/grid.py`、World Package `geometry/voxel/` | port（scipy） | V0.1 构建，V0.2 使用 | 是 |
| 26 邻域 A* + round-stamp | EGO `dyn_a_star` | `planning/search.py` | port（numba） | V0.2 | 是 |
| ESDF 距离代价 + L-BFGS | Fast-Planner `calcDistanceCost` + EGO 光滑/可行性项 | `planning/bspline.py` | port | V0.2 | 是 |
| `states2pts` 边界 → 控制点 | Fast-Planner `planYaw` | `planning/bspline.py` | port | V0.2 | 是 |
| 时间拉伸 / 局部重分配 | EGO `lengthenTime`；FP `reallocateTime` | `planning/bspline.py` | port | V0.2（均匀）/ V0.6（局部） | 是 |
| 前视 yaw + 限速 | EGO `traj_server::calculate_yaw` | Mock 控制器 | port | V0.2 | 是 |
| 平坦性姿态（a + g → R） | EGO `fake_drone` | Mock 动力学 / 渲染 | port | V0.2 | 是 |
| receding-horizon FSM | EGO `EGOReplanFSM` | `planning/fsm.py` | port（精简） | V0.2（全局）/ V0.6（局部感知） | 是 |
| 优先级 + 4D 预约 | EGO `SEQUENTIAL_START` 思想 + `checkCollision` | `planning/deconflict.py` | port + 扩展 | V0.6（V0.2 可先做单机） | 否 |
| swarm 椭球代价 | EGO `calcSwarmCost` | `planning/bspline.py` | port | V0.6 | 否 |
| ORCA-3D | RVO2-3D（按记忆移植，本文原型已测） | `planning/orca.py` | port（numba） | V0.6 | 否 |
| {p,v} rebound + 拓扑多解 | EGO `initControlPoints` / `distinctiveTrajs` | `planning/rebound.py` | port（可选） | V0.6（未知环境演示） | 否 |
| EGO 真后端 | `ros2_version` 分支 | `backends/ego_ros2.py` | adopt | V0.6 | 否 |
| 虚实混合 UDP | `rosmsg_tcp_bridge` | `backends/ego_udp.py` | port（已验证） | V0.6 | 否 |
| 真机目标下发 | Fast-LIO2_Ego-Planner `REMOTE_TARGET` | Gateway | reference | V0.5 | 否 |
| MID-360 → 占据更新 | 同上 `cloudOdomCallback`（修正外参和半球问题） | World Service 的增量体素 | reference | V0.5 | 否 |
| kino A* / topo PRM / Primitive-Swarm | FP / ZJU | – | reference | V1.0 | 否 |

### 4.4 UI 挂钩（简述）

- **MissionLayer**：
  - 规划轨迹用 `Line2`，按速度着色；
  - 剪枝后的航点用 morphicons 的 waypoint 图标（禁止 emoji）；
  - 冲突点用红色 `#E93024` 标记。
- **DebugLayer**：
  - A* 扩展点云（`Points`，≤ 50k）；
  - ESDF 水平切片（DataTexture 热力图，配色遵循 lieflat-charts）；
  - `occ_infl` 用体素 InstancedMesh，只显示轨迹走廊。
- **Mission 面板**（shadcn 的 `Card / Table / Badge / Progress`）：列出每架机的 `delay_s`、`alt_offset`、`min_clearance`、规划耗时。status badge 显示 ok / degraded / no_path。

---

## 5. 对比与推荐

| 维度 | EGO-Swarm | Fast-Planner | Fast-LIO2_Ego-Planner |
|---|---|---|---|
| 与"服务端 + 先验地图"的契合 | 中：设计目标是机载局部地图、ESDF-free | **高**：ESDF 代价在服务端最自然 | 低：纯真机集成 |
| 多机 | **原生**：环链优先级 + 广播 + swarm 代价 | 无 | 继承 EGO，外加 master/slave 命令 |
| 算法可移植性 | 高（代价函数都是闭式） | 高（EDT → scipy） | – |
| 2026 活跃度 / 生态 | README 更新于 2025-03；ROS2 分支；Amov P600 同源 | 2024-10 合并最后一个 PR；FUEL/RACER 基座 | 2024-12，★1 |
| 构建难度 | 中（catkin/colcon，PCL） | 中偏高（NLopt 源码编译） | 高（Ceres、GTSAM、SDK2，脚本依赖 gnome-terminal） |
| 与 P600 的一致性 | **最高**（Prometheus 内置其 fork） | 低 | 中（同为 MID-360 + EGO） |

**推荐排序**：

1. **EGO-Swarm**。它提供契约、FSM、swarm 机制和 V0.6 真后端，与 P600 同源。
2. **Fast-Planner**。它的 ESDF 代价、EDT、时间重分配和 yaw 规划是 MVP 服务端规划器的主要算法来源。
3. **Fast-LIO2_Ego-Planner**。只参考真机链路和外部目标接口。

---

## 6. 风险与注意事项

1. **BLAS 线程陷阱**（实测最严重）。
   - scipy L-BFGS-B 每次迭代都会调用 BLAS 小矩阵运算。在 8 核多线程下，同一个 45 控制点的问题耗时 1024 ms，单线程只要 23 ms。
   - Planning worker **必须**满足以下之一：
     - 设置 `OMP_NUM_THREADS=1` 或 `OPENBLAS_NUM_THREADS=1`；
     - 或者用 `threadpoolctl.threadpool_limits(1)`。
   - 并发靠多进程或 `ProcessPoolExecutor` 解决，每个进程常驻一份 `PlanGrid`（mmap 共享）和一份 `AStarPool`。
2. **ROS1 Noetic 已 EOL**（2025-05）。
   - EGO master、Fast-Planner、Fast-LIO2_Ego-Planner 都只支持 Noetic（Ubuntu 20.04）。
   - 服务器侧如果要跑真规划器，只选 `ros2_version`（Humble + CycloneDDS）。放进 Docker，不要污染主机。
3. **EGO 的规模硬约束**：
   - A* 节点池为 100³、步长 0.1 m，只能覆盖约 10 m 的立方体；
   - `GridMap` 是稠密的 `double` 数组。200×200×30 m 在 0.15 m 分辨率下约 3.6×10⁸ voxel，需要约 3.2 GB。
   - **不能把 EGO 当城市级全局规划器用**，必须由我们的全局规划器喂 local target 或航点。
4. **时钟**：
   - EGO 用墙钟（ROS1 为 `ros::Time::now()`，ROS2 为 `rclcpp::Clock()`，后者忽略 sim time）；
   - swarm 轨迹的时间窗只有 0.25 s；
   - 我们的 Timeline 支持 ×2 / ×5 / ×10 快进。**Mock 规划器一律用 `t_sim`**。接入 EGO 后端时，只能 ×1 实时运行，并且主机之间要用 chrony/PTP 同步。
5. **坐标**：
   - EGO 每架机都在自己的 odom 世界系下工作，swarm 要求各机世界系一致；
   - Gateway 负责在 World ENU 与各机 odom 之间做变换（r19 §2.5）。广播轨迹的**控制点逐点变换**即可，因为 B-spline 在仿射变换下保持不变。
6. **稀疏点云**：UrbanScene3D 采样云在 1 m 分辨率下有大量空洞，规划必须用 ≥ 2 m 分辨率加 DSM 挤出。DSM 挤出会把桥下、树冠下的空间判为实心，这是保守的；如需保留，由语义层标注后豁免。
7. **ORCA 局限**：
   - 加速度受限时不保证安全（§3.9 的实测）；
   - 各向异性缩放会破坏它的推导；
   - kNN 目前是 O(N²)，N > 300 时需要改用 KD-tree（`scipy.spatial.cKDTree`）或空间哈希。
8. **EGO 代码层面的坑**（移植时不要照抄）：
   - `inf = 1 >> 20 = 0`；
   - 开放集不重排；
   - `min_ellip_dist_` 取的是最小值；
   - `minSnapTraj` 实为 min-jerk；
   - 梯形时长公式在短距离分支少了一个系数 2；
   - swarm 模式禁用 refine；
   - 只检查前 2/3 段。
9. **Fast-LIO2_Ego-Planner 的坑**：
   - 丢掉 `x ≤ 0.1` 的点，即只用前半球；
   - LiDAR 与 body 外参被忽略；
   - launch 里 depth 和 cloud 话题配置矛盾；
   - `drone_detect` 写死像素遮罩；
   - 脚本依赖 `gnome-terminal`。
   - 只能作为参考，不能直接部署。
10. **依赖**：numba 0.67、llvmlite 0.49 与 numpy 2.5 实测兼容。首次 JIT 约 0.5 s，要在服务启动时预热，并设 `cache=True`。如果不想引入 numba，A* 可以退回纯 Python，但只适合 < 10⁴ 次扩展的小场景，因为速度约慢 700 倍。
11. **License**：三个仓库的 LICENSE 文件都是 GPLv3（Fast-Planner 源文件头写的是 LGPLv3）。本项目是科研用途，按约定忽略；如果将来开源，需要注意移植代码的衍生作品问题。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **在 §3 总体架构中补一个一等公民模块：Planning Service。**
   - 现在规划只出现在 §31 的 "Task Planner"（Agent Runtime）和 §42 的 `swarm/planning` 目录里。
   - 建议在 Simulation Service 内显式列出 `Planning（全局 A* + B-spline 优化 + 速度规划 + 互避）`，采用 §4.1 的接口。
   - Agent/ANet 层（§31–§32）只输出**目标和任务**，轨迹一律由 Planning 生成，这样才能保证"换后端不改 UI"（§4.1 原则）。
2. **§8 Geometry World 要补规划栅格规格。**
   - 现在只写了 "Voxel / SDF / Occupancy Grid"，没有给分辨率、内存和更新策略。
   - 建议写入：
     - "Planning Grid Pyramid"：4 m 全局加 ESDF，走廊内 1–2 m 局部窗，真机地图 0.2 m 局部；
     - DSM 挤出规则；
     - 版本号 `grid.version`，地图更新后触发 REPLAN；
     - World Package 路径 `geometry/voxel/plan_r{res}.npz`。
3. **§43/§45 MVP 要带上最简避障规划。**
   - 原 MVP 链路止于 "Drone Movement"。在城市点云里，直线 GoTo 会**穿楼**，演示可信度会大打折扣。
   - 建议 V0.2 的 GoTo/FollowPath 默认走 `astar_bspline`，失败时降级为折线加速度规划。实测 < 250 ms，成本很低。
4. **§30 控制模式要定义"轨迹契约"。**
   - `FollowPath / Orbit / GoTo` 的输出统一为 B-spline（order 3），Mock、PX4 SITL offboard（位置 / 速度 / 加速度 setpoint）和 EGO 后端共用。
   - UI 按同一契约渲染和回放。
   - DroneState（§28）增加 `traj_id` 和 `traj_t`，便于回放对齐。
5. **§37 频率表补充规划相关频率。**
   - 全局规划：按需 / 1 Hz 重规划；
   - safety check：20 Hz；
   - traj server 采样：100 Hz；
   - ORCA：20–50 Hz；
   - `traj.update` 推送：事件驱动。
   - 同时写明"规划与动力学使用 `t_sim`；快进时规划也按仿真时间推进"。
6. **§49 V0.6 "Swarm / Formation / Planning / Avoidance" 需要细化成三层互避**（§3.9）：
   - 战略层：优先级、4D 预约、延迟与高度层；
   - 轨迹层：swarm 椭球代价；
   - 战术层：ORCA 加刹停错层。
   - 另外列出 EGO `ros2_version` 作为可选真后端，并把**虚实混合 swarm**（§3.10）作为 V0.6 的亮点演示：真机避让数字孪生中的虚拟机。
7. **§2 / §35 需要纠正一个隐含假设。**
   - 原文说 "PX4 + Gazebo 负责 Planning"，但 PX4 本身不做城市级 3D 避障规划，P600 的避障来自 Prometheus 内置的 EGO fork（r19 §2.4）。
   - 建议写明：真机规划 = EGO（机载，局部）；平台规划 = 本 Planning Service（服务端，全局加任务级）。两者用 `REMOTE_TARGET` 式的航点或 local target 衔接。
8. **§41 World 文件结构**：在 `geometry/voxel/` 下补 `plan_r4.npz`（bitpack occ、f16 esdf、i16 dsm、元数据）；在 `semantic/` 下补 `restricted_area.geojson`，由 Planning 读取为禁飞体。§7 的 Semantic "Restricted Area" 应明确"进入规划约束"。
9. **§47 V0.4 物理环境补一条"风场感知规划"。**
   - 有阵风时：`clearance += k·gust`，`v_max_eff = v_max − |w_head|`；
   - 用风场 `E(x,y,z,t)` 在走廊内的最大值修正可行性约束；
   - 能耗代价（逆风段）作为 A* 附加代价。
10. **§38–§40 的 UI 需要补"规划可视化与诊断"。**
    - 轨迹按速度着色；
    - 冲突时间轴，嵌在 Timeline 上；
    - 每机规划状态 badge；
    - ESDF 切片和 A* 扩展调试层（默认关闭）。
    - 这对科研平台很关键，因为调参和论文复现都依赖它。
