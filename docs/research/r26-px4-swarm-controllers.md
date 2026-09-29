# R26 研究笔记：PX4_Swarm_Controller / PX4-Aerial-Swarm-Reconstruction —— 编队控制律、多机区域覆盖与“扫描揭示”点云可视化

> 研究单元：r26 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §28（DroneState）、§29（多无人机架构）、§30（控制模式：Area Coverage / Formation / Swarm）、§36–§37（实时通信与频率）、§43（MVP）、§49（V0.6 Multi-UAV）
>
> 仓库快照（本地只读 clone）：
> - `refs/swarm/PX4_Swarm_Controller` @ `02c3995`（2024-03-02，★97，ROS2 Humble + PX4 uXRCE-DDS + Gazebo Classic，C++17/Eigen，约 1.4k 行 C++ + 0.2k 行 Python）
> - `refs/swarm/PX4-Aerial-Swarm-Reconstruction` @ `c9a0685`（2024-12-11，★16，ROS2 Humble + Gazebo Classic，约 0.9k 行 C++（含测试）+ 0.1k 行 Python；自带 227 条 `px4_msgs` 快照）
>
> 本机实测产物（`.cache/research/r26/`，只依赖项目 venv 中的 numpy 2.5.3，无 scipy/shapely）：
> - `r26_formation.py` / `_out.json`：编队槽位生成器；Hungarian（e-maxx 向量化实现，已与穷举对拍）；三种控制律（A=仓库原律移植、B=虚拟结构+前馈、C=B+一致性+分离）在统一的“类 PX4 被控对象”上的对比；原仓库 `dt` 计算缺陷的复现。
> - `r26_formation2.py` / `_out.json`：律 A 的“领航机脱连”复现、编队航向滤波、固定领航机与虚拟领航点两种 CAPT 分配的对比。
> - `r26_pidbug.py` / `_out.json`：原仓库 `PID::update()` 与 float32 时间戳叠加后的等效增益分布。
> - `r26_coverage.py` / `_out.json`：UrbanScene3D San Francisco（500 万点）→ 2 m DSM；多边形割草机航线（支持凹多边形）、扫描角搜索、按飞行高度裁剪障碍、BCD-lite 分胞、多机按代价均衡的连续切分 + Hungarian 分配、覆盖率栅格评估；覆盖栅格的流式推送测算。
> - `r26_groupsweep.py`（编队整体扫掠 vs 分区覆盖）、`r26_stamp_scale.py`（100 机 / 2048² 栅格下的打点与快照开销）。
>
> 与其他笔记的分工：
> - 坐标体系（World ENU、Z-up、BODY=FLU）以 **r15 §3.1** 为准，本文只补充 PX4 NED 与 Gazebo ENU 之间的换算坑。
> - Mock 动力学（PX4 级联增益的向量化模型）以 **r19 §3.5** 和 **r24 §3.1–3.2** 为准。本文的控制律实验用的是同类“加速度一阶滞后 + 限幅”被控对象。
> - Gateway / DroneAdapter / Offboard 20 Hz 保活以 **r21 §4** 为准。本文给出的 FormationTracker 和 CoverageExecutor 都挂在 Gateway 的 Tracker 层。
> - 多机避碰、`min_sep`、Safety FSM 以 **r24 §3.8 / §4** 为准。本文只给出编队和覆盖任务内部的分离项与高度分层。
> - 点云 LOD / 八叉树 / 点预算以 **r09、r12** 为准。本文补充“覆盖驱动的密度偏置”（§3.12.4）。

---

## 0. 结论速览

| 仓库 / 资产 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **PX4_Swarm_Controller**（artastier，★97，2024-03 后无提交） | ROS2 + PX4 Offboard 的多机领航-跟随框架。实现 Hou & Fantoni（hal-01180491）的 weighted topology 分布式编队：距离邻接图、PrC（Priority Coefficient）权重、相对位置/速度一致性、逐轴 PID 输出加速度 | **port（算法，已修 bug）**：邻接图 + PrC 权重（改写为 BFS 跳数）、一致性项、NaN 型 setpoint 语义、JSON 驱动的 launch 组织方式。**reference**：`NearestNeighbors<T>` 三钩子模板、`sitl_multiple_run.sh -p` 位姿表。**skip**：ROS2 节点本身、Gazebo Classic 脚本 | V0.6（拓扑与一致性项）；V0.2（Offboard 生命周期语义） | ★★★☆☆ |
| **PX4-Aerial-Swarm-Reconstruction**（UMD 课程项目，★16，2024-12） | 20 架 Iris 在 Gazebo 城市场景中按预设航点“扫街”。一个集中式 `Control` 节点带 N 组 pub/sub，全体到达后同步推进（barrier）。按 3/5/7/10 m 四个高度层分组互不冲突。**点云融合部分未完成**（README 自述 namespace 问题） | **port（概念）**：集中式多机执行器、barrier 同步策略、分组高度分层、按 spawn 偏移处理局部 NED。**reference**：Offboard 预热 50 帧再 arm 的流程、测试/CI 模板。**skip**：`grid_plan.world`、点云部分（不存在） | V0.6（执行器、同步、分层）；MVP mock 演示复用其“多机扫城”剧本 | ★★☆☆☆ |
| **本单元补齐：虚拟结构编队 + 航向滤波 + CAPT 分配**（非仓库代码，已原型验证） | 取代律 A 作为 FormationTracker 主律：虚拟锚点 + 完整前馈（p\*, v\*, a\*）+ 二阶航向滤波 + 可选一致性和分离项；变队形用 Hungarian（平方距离）+ 同步直线插值（CAPT） | **new** | MVP mock 演示 → V0.6 正式 | ★★★★★ |
| **本单元补齐：多机覆盖规划器**（割草机 / BCD-lite / 代价均衡连续切分 / Hungarian 分配 / DSM 高度） | 取代 PASR 手写航点表，给 Mission 模块的 `AREA_COVERAGE` 用 | **new** | MVP mock 演示 → V0.6 正式 | ★★★★★ |
| **本单元补齐：覆盖栅格 + 点云“扫描揭示”着色** | 在不生成新点云的前提下，展示“多机扫城 → 点云逐步融合”，并与点云 LOD 联动 | **new** | MVP | ★★★★★ |

**关键结论（实现者先读这几条）**

1. **两个仓库都已停更（最后提交 2024 年），都绑定 ROS2 Humble + Gazebo Classic（2025-01 EOL），在本机（无 GPU、无 ROS）无法直接运行。** 价值只在算法和工程模式上，**不作为依赖**。进入主链路的是我们自己的 Python/TypeScript 实现，挂在 r21 的 Gateway Tracker 层。
2. **仓库的领航-跟随律（律 A）在中速下会“脱连”，编队随之失效，而且不会触发它自带的兜底。** 复现条件：配置原样（3 机、邻居半径 3 m、P 增益 (1, 2.5, 2.75)），领航机沿 R=20 m 圆周飞行。
   - 1/2/3/4 m/s 时，编队误差 RMS 为 0.06/0.24/0.52/1.13 m。
   - **5 m/s 时，79% 的控制周期里跟随机与领航机图不连通**，两架跟随机互为邻居、一起漂走（RMS 199.8 m）。
   - 原因：律 A 只是相对量一致性，没有领航机加速度前馈，转弯时存在稳态滞后；滞后超过邻居半径就断链。仓库的兜底条件是“邻居为空”，**检测不到“有邻居但与领航机不连通”这种子图漂移**。
   - 同场景下，**律 B（虚拟结构 + 前馈）的 RMS 为 0.01–0.09 m**。
3. **队形固定朝向“随领航机航向”时，急转弯不可行，必须加航向滤波。**
   - 测试条件：7 机 V 字（间距 6 m，半角 35°），5 m/s 过 R=15 m 的 U 形弯。
   - 严格对齐航向：RMS 4.87 m、最大 27.9 m、15.5% 的周期加速度饱和；加一致性和分离项后，最小机间距掉到 **0.24 m**。
   - 对编队航向做二阶滤波（τψ = 1–4 s）后：RMS 0.065–0.071 m，最大 0.18–0.25 m。队形固定朝北（world-fixed）：0.072 m。
   - 根因：直线接圆弧处曲率突变，纵向偏移为 x 的槽位会出现 Δv = V·Δκ·|x| 的速度阶跃。例如 5 m/s、R=15 m、x=14.7 m 时，Δv = 4.9 m/s。
4. **变队形 / 集结必须做槽位分配，且锚点必须是“虚拟领航点”。**
   - 12 机从地面 3×4 停机坪集结到 30 m 高的圆形：按编号直连时最小机间距 0.21 m；Hungarian（平方距离）后为 3.33 m。
   - 9 机在 line/V/grid/circle/column 之间互变（间距 6 m）：若**固定领航机槽位**，4 组变换仍会穿过领航机（最小间距 0.02 m）。改为**虚拟锚点（全部槽位可分配）**后，20 组变换的最小间距 **≥ 4.24 m**。这与 CAPT（Turpin/Michael/Kumar 2014）的无碰保证条件一致。
5. **仓库代码有多处实质性缺陷，移植时必须修（§2.11）。**
   - `PID::update()` 从不更新 `previous_error`，D 项退化为 Kd·e/dt。
   - `compute_command()` 用 `seconds()/1000` 存进 `float` 成员，float32 在 1.79e6 附近的 ULP = 0.125（即 125 s），导致 dt 在 ±0.0625（千秒）之间锯齿跳动，**48% 的周期 dt 为负**。叠加后 z 轴等效比例增益在 0.23% 的周期里为负，积分项退化为随机游走。
   - `vectors_to_Vector3d` 的逗号表达式判断加上 `reserve()` 后直接写入：未定义行为。
   - PrC 的邻域缓存在领航机分支不复位，导致权重串位。
   - 邻域节点“全体位置都到齐才发布”的门控：任一机迟到，全体跟随机本周期都退回位置保持，**Offboard 模式在 ACCELERATION 和 POSITION 之间来回抖**。
6. **覆盖规划在 UrbanScene3D 上完全可以实时做**（numpy 单线程，San Francisco 0.479 km²）。
   - 500 万点 → 2 m DSM 用时 0.9 s。
   - 高于屋顶模式（60 m AGL，重叠 70/80%）：38 条航带、26.75 km，规划 **119 ms**，覆盖率 100%。单机 61.1 min，4 机 18.2 min（均衡度 1.08），8 机 11.2 min。
   - 楼下模式（25 m AGL，避开楼体）：227 段、138 个 BCD 胞，规划 **443–480 ms**，覆盖率 92.0%（剩下的是高于 15 m 的屋顶，需要补一遍高空）。8 机 16.2 min。
7. **“多机扫城 → 点云逐步融合”的可视化不需要真的生成点云。**
   - 做法：服务端维护一张 2.5D 覆盖栅格（u8：首个扫到该格的机号），点云着色器按世界 XY 采样这张纹理，未扫区域用暗灰细点，已扫区域按机号着色。
   - 实测开销：San Francisco 372×360 栅格，8 机 5 Hz 打点 **0.41 ms/tick**；增量 zlib 约 **1.1 kB/s**；**整图快照仅 0.6–4.4 kB**。
   - 极端情况：100 机、2048² 栅格，打点 3.0 ms/tick，整图快照 76 kB / 21 ms。
   - 这张栅格还可以反过来驱动点云 LOD：未扫区域降密，扫过的区域升密（§3.12.4），正好对应“点云疏密自动调节”这一需求。
8. **编队整体扫掠（line-abreast 一起走）和分区覆盖在开阔多边形上耗时接近**：矩形 4 机 16.8–17.3 min（不含往返）对 18.2 min（含往返）。但分区覆盖对障碍、凹形、单机故障和异构速度/电量更稳健。**默认用分区覆盖；编队扫掠只作为演示或通信中继场景的可选项。**
9. **推荐排序：PX4_Swarm_Controller > PX4-Aerial-Swarm-Reconstruction**，都属于“读算法、不引依赖”。真正要实现的是 §3.4–§3.12 的新模块。

---

## 1. 仓库概览

### 1.1 PX4_Swarm_Controller

| 项 | 内容 |
|---|---|
| 作者/来源 | Arthur Astier、Martin Piernas（Ecole Centrale de Nantes 研发选修项目） |
| 目标 | “可扩展的多机仿真，便于在机群上测试控制律”。默认控制律取自 Zhicheng Hou, Isabelle Fantoni, *Distributed leader-follower formation control for multiple quadrotors with weighted topology*（HAL: hal-01180491） |
| 技术栈 | ROS2 Humble（Ubuntu 22.04），PX4-Autopilot SITL + Micro XRCE-DDS Agent（UDP 8888），Gazebo Classic，C++17 + Eigen3 + yaml-cpp，rclpy 启动脚本 |
| 包结构 | `px4_swarm_controller`（ament_cmake + ament_python 混合）+ `custom_msgs`（`Neighbors.msg`、`WeightedTopologyNeighbors.msg`，需手动移到工作区单独编译） |
| 可执行 | `arming`、`waypoint`、`weighted_topology_neighbors`、`weighted_topology_controller`、`simulation_node.py` |
| 配置 | `config/swarm_config.json`（机型/初始位姿/是否领航/轨迹文件）、`config/control_config.json`（邻域节点、邻居距离、队形、控制器与 9 个 PID 增益）、`config/Trajectories/*.yaml`（领航航点，NED z） |
| 已知局限（README 自述） | 只支持 iris（命名空间写死 `/px4_i`）；未迁移到新版 Gazebo；增益只对仓库自带队形调过；不能在线切换控制器/队形 |
| 活跃度 | 仅 1 个快照提交（2024-03-02），2026 年无活动 |

运行时节点图（N=3，1 号为领航机）：

```text
simulation_node.py ──gnome-terminal──► MicroXRCEAgent udp4 -p 8888
                    └─────────────────► PX4-Autopilot/Tools/simulation/gazebo-classic/sitl_multiple_run.sh -s iris:3 -p "x,y|x,y|x,y"
                                           └─ px4 -i {1..N}  ⇄  gzserver（iris_N，mavlink tcp 4560+N / udp 14560+N / sysid N+1）

/px4_1/waypoint ──(sub) /px4_1/fmu/out/vehicle_local_position
                ──(pub) /px4_1/fmu/in/offboard_control_mode, /px4_1/fmu/in/trajectory_setpoint   [position 模式]

/simulation/nearest_neighbors（WeightedTopologyNeighbors，10 Hz）
                ──(sub) /px4_{1..N}/fmu/out/vehicle_local_position        [sensor_data QoS]
                ──(pub) /px4_{i}/fmu/out/nearest_neighbors                 [custom_msgs/WeightedTopologyNeighbors]

/px4_{2,3}/weighted_topology_controller（10 Hz）
                ──(sub) /px4_i/fmu/out/nearest_neighbors
                ──(pub) /px4_i/fmu/in/offboard_control_mode + trajectory_setpoint   [acceleration 模式，p/v 置 NaN]

/simulation/arming（1 Hz 重试，全部 ARMED+OFFBOARD 后 rclcpp::shutdown）
                ──(srv) /px4_{i}/fmu/vehicle_command   [px4_msgs/srv/VehicleCommand]
```

### 1.2 PX4-Aerial-Swarm-Reconstruction

| 项 | 内容 |
|---|---|
| 作者/来源 | Mohammed Munawwar、Apoorv Thapliyal、Kshitij Aggarwal（UMD 课程项目，AIP 分阶段：Phase 0 提案、Phase 1 多机生成、Phase 2 控制扫街） |
| 目标（提案 QuadChart） | 多机协同生成点云、地图化一片区域，面向“大范围地面监视、灾害管理” |
| 实际完成 | Phase 1：Gazebo 城市 `grid_plan.world` 中生成 20 架 Iris，可以取到 local position / IMU / velocity。Phase 2：集中式节点控制 20 机按预设航点“有序扫掠”。**点云部分没有完成**（README：“Due to namespace issues, the pointcloud data is unavailable”） |
| 技术栈 | ROS2 Humble、Gazebo Classic、`ros_gz`（README 要求编 humble 分支，但代码里没用到）、Micro XRCE-DDS、yaml-cpp、GTest + catch_ros2、lcov/gcovr、Doxygen、GitHub Actions（`osrf/ros:humble-desktop` 容器 + Codecov） |
| 包结构 | colcon 工作区：`src/px4_msgs`（整包 vendored，227 msg + `srv/VehicleCommand.srv`）+ `src/px4_swarm_controller`（`libs/Control` 静态库 + `arm` 可执行 + 测试） |
| 活跃度 | 快照提交 2024-12-11，2026 年无活动 |

运行时（N=20）：

```text
px4_multi_sim.launch.py ─► simulation_node.py ─► MicroXRCEAgent + sitl_multiple_run.sh -n 20 -p "37,-18|37,-6|…"   (world 默认 grid_plan)
ros2 run px4_swarm_controller arm ─► ctrl::Control（单节点，10 Hz 定时器）
    for i in 1..N:  pub /px4_i/fmu/in/{offboard_control_mode, trajectory_setpoint, vehicle_command}
                    sub /px4_i/fmu/out/vehicle_local_position   [QoS(10).best_effort()]
```

---

## 2. 源码结构与关键模块

### 2.1 PX4_Swarm_Controller 目录

```text
CMakeLists.txt                     # 4 个 C++ 可执行 + ament_python 安装 simulation_node.py
config/
  swarm_config.json                # swarm{id:{model, initial_pose{x,y}, is_leader}}, trajectory
  control_config.json              # neighborhood{neighbors_exe, neighbor_distance, params{x/y/z_formation}}, controller{controller_exe, leader_follower, params{gains[9]}}
  gains.yaml                       # rqt_ez_publisher 风格的调参面板定义（未被代码使用）
  Trajectories/waypoints_{circular,up_and_down}.yaml
custom_msgs/msg/{Neighbors,WeightedTopologyNeighbors}.msg
include/
  Arming.hpp  ChangeWaypoint.hpp  PID.hpp  NeighborsTraits.hpp
  NearestNeighbors.hpp             # 模板基类：邻域计算节点
  SwarmController.hpp              # 模板基类：控制器节点
  SwarmControllers/WeightedTopology/{WeightedTopologyNeighbors,WeightedTopologyController}.hpp
src/ Arming.cpp  ChangeWaypoint.cpp  SwarmControllers/WeightedTopology/*.cpp
launch/launch_simulation.py
px4_swarm_controller/simulation_node.py
sitl_multiple_run.sh               # 覆盖 PX4 原脚本：增加 -p 位姿表
```

### 2.2 `launch/launch_simulation.py`：配置驱动的节点编排

- `parse_swarm_config()`：
  - 按机型计数，生成 `-s iris:3` 脚本串；
  - 拼出 `-p "x,y|x,y|…"` 初始位姿串；
  - 收集 `is_leader[]` 和轨迹文件名。
- `generate_launch_description()`：
  - 领航机起 `waypoint` 节点，参数为 `wp_path`、`x_init`、`y_init`；
  - 非领航机起 `controller_exe`，参数为 `gains`；
  - 另起全局的 `neighbors_exe`（`namespace='simulation'`），参数为 `nb_drones`、`neighbor_distance`、`x_init[]`、`y_init[]`、`leaders[]`、`x/y/z_formation[]`；
  - 再起 `arming`。
- **坐标换算**：Gazebo spawn 用 ENU，PX4 用 NED，所以 `xs_init.append(initial_pose["y"])`、`ys_init.append(initial_pose["x"])` 交换了 x/y。后面 `ChangeWaypoint::writeWP()` 又按 `N = wp.y − y_init、E = wp.x − x_init、D = wp.z` 写 setpoint。**YAML 航点的 x/y 是 ENU、z 却是 NED（负数为上）**，是一套混合约定。
- 可借鉴的模式：**控制器与邻域算法都由 JSON 指定可执行名**，换控制律不改 launch。我们对应的做法是 Mission/Formation 的 `controller: "vs_ff" | "wt_consensus"` 策略字段（§4.3）。

### 2.3 `simulation_node.py` + `sitl_multiple_run.sh`

- `SimulationScript.__init__`：读取 `nb_vehicles / drone_model / world / script / target / label / initial_pose` 参数并拼成命令行。**用 `gnome-terminal --tab` 分别启动** `MicroXRCEAgent udp4 -p 8888` 和 `cd ~/PX4-Autopilot && sitl_multiple_run.sh …`。退出时 `pkill gnome-terminal`。这要求有桌面会话，**无法 headless 运行**。
- `sitl_multiple_run.sh`（PX4 原脚本的修改版）：
  - `spawn_model(MODEL, N, X, Y)`：`px4 -i N -d $build_path/etc`；用 `jinja_gen.py` 生成 SDF，参数为 `--mavlink_tcp_port 4560+N`、`--mavlink_udp_port 14560+N`、`--mavlink_id 1+N`、`--gst_udp_port 5600+N`、`--video_uri 5600+N`、`--mavlink_cam_udp_port 14530+N`；然后 `gz model --spawn-file … -x X -y Y -z 0.83`。
  - 新增 `-p` 位姿表：`IFS='|'` 切出每机 `x,y`；缺省位姿为 `(0, 3N)`（PASR 版改成 `(32, −16+2N)`）。
  - 上限 255 架（MAVLink sysid 空间）。
  - 实例号从 1 开始，所以 uXRCE-DDS 命名空间是 `px4_1…px4_N`（PX4 多实例约定：实例号 > 0 才加 `px4_<i>` 前缀）。**这也是两个仓库都写死 `/px4_{i+1}` 的原因**，iris 以外的机型没有问题，问题在于实例编号约定。

### 2.4 `Arming`（`src/Arming.cpp`）

- 为每机创建 `px4_msgs::srv::VehicleCommand` 客户端，服务名 `/px4_{i}/fmu/vehicle_command`。这是 uXRCE-DDS 的“服务”形式，需要较新的 PX4 / px4_msgs：PASR 的 px4_msgs 快照里有 `srv/VehicleCommand.srv`，可以作为旁证。
- 1 Hz 定时器：对未 offboard 的机发 `VEHICLE_CMD_DO_SET_MODE(176)`，`param1=1`（MAV_MODE_FLAG_CUSTOM_MODE_ENABLED）、`param2=6`（PX4 custom main mode OFFBOARD）；对未解锁的机发 `VEHICLE_CMD_COMPONENT_ARM_DISARM(400)`，`param1=1`。
- 回调里以 `reply.result == 0` 视为成功；全部成功后 `rclcpp::shutdown()`。
- 注意：本节点自己**不发 setpoint 流**，依赖 `waypoint` 和 controller 节点已经在推流（PX4 要求先有 ≥2 Hz 的 Offboard 心跳才能切 OFFBOARD）。

### 2.5 `ChangeWaypoint`（领航机航点器，`src/ChangeWaypoint.cpp`）

- 每收到一帧 `vehicle_local_position`（事件驱动）：
  - 算到当前航点的三维距离，以及航向误差 `|fmod(ψ − ψd + π, 2π) − π|`；
  - 两项同时低于阈值（`threshold=0.1 m`、`threshold_angle=0.4 rad`）时切到下一个航点，循环往复；
  - 然后发布 `OffboardControlMode{position=true}` 和当前 `TrajectorySetpoint`。
- 仓库自带轨迹非常小：“圆”是 **半径 1 m 的 8 个点**，另一条是 5 m ↔ 1 m 的上下往返。这掩盖了律 A 在中高速下的问题（§3.3）。
- 缺陷：`waypoint.timestamp = seconds()`（单位是秒，PX4 约定是微秒）；0.1 m 的接受半径对真实 PX4 位置控制偏严。

### 2.6 `NearestNeighbors<Neighbors>`（`include/NearestNeighbors.hpp`，模板基类）

- `static_assert(traits::has_neighbors_position_attribute_and_is_VLP_v<Neighbors>)`（`NeighborsTraits.hpp`，用 SFINAE 检查消息里有 `std::vector<VehicleLocalPosition> neighbors_position` 字段）。
- 构造：订阅所有 `/px4_i/fmu/out/vehicle_local_position`（`rmw_qos_profile_sensor_data`），为每机创建 `/px4_i/fmu/out/nearest_neighbors` 发布者，**100 ms 墙钟定时器**。
- `pose_subscriber_callback()`：`local_to_global()` 加上 spawn 偏移（x_init、y_init，已换成 NED），把各机的局部 NED 统一成共同的 NED。
- 定时器：**仅当 `position_received[]` 全为 true 才计算**，然后把标志全部清零。随后 `find_neighbors()`：对每机 `process_position()`，遍历全部机体，满足 `1e-2 ≤ ‖Δp‖ ≤ neighbor_distance` 的算邻居。
- 三个可覆写钩子（这是这个仓库最干净的抽象）：
  - `process_neighbor_position(drone_idx, neighbor_idx, pos, neighbor_pos, neighborhood)`：每发现一个邻居调用一次；
  - `process_neighborhood(drone_idx, neighborhood)`：单机邻域算完后调用；
  - `enrich_neighborhood(neighborhood)`：全体邻域算完后、发布前调用（可以用到全局信息，例如 PrC）。
- 邻域为空的机体**不发布消息**。控制器据此“超时”回退到位置保持（§2.8）。

### 2.7 `WeightedTopologyNeighbors`（PrC 与权重）

- 参数：`leaders[]`，`x/y/z_formation[]`（**每机一个绝对槽位坐标**，NED；README 称之为“相对领航机的期望距离”，实际代码按 f_i − f_j 使用）。
- `process_neighbor_position()`：写入相对量 `Δp_ij = p_i − p_j − (f_i − f_j)`、`Δv_ij = v_i − v_j`，记录 `neighbors_ids`，把 `prcs_neighborhood[j] = prcs[j]` 缓存下来。
- `process_neighborhood()`：领航机 `prc=1`；跟随机 `prc = min(prcs_neighborhood)+1`，上限为 N，然后把缓存重置为 N。**领航机分支不重置缓存**（缺陷，§2.11）。
- `enrich_neighborhood()`：`w_ij = (1/prc_j) / Σ_{k∈N_i} (1/prc_k)`，按行归一化。离领航机跳数越少的邻居权重越大，信息从领航机向外“流”。
- PrC 本质上就是“到领航机的图上跳数 + 1”，通过每周期一次的 Gauss-Seidel 扫描逐步收敛（依赖扫描顺序）。

### 2.8 `SwarmController<Neighbors>` 与 `WeightedTopologyController`

- 基类：发布 `{ns}/fmu/in/offboard_control_mode` 与 `trajectory_setpoint`，订阅 `{ns}/fmu/out/nearest_neighbors`，100 ms 定时器。`publish_offboard_control_mode(CONTROL::{POSITION,VELOCITY,ACCELERATION})`。
- `neighbors_to_matrix()`：把每个邻居的 `[Δx, Δvx, Δy, Δvy, Δz, Δvz]` 乘以 `w_ij`，组成 6×k 矩阵。
- `compute_command()`：
  ```text
  RPVVs = Σ_j w_ij·[Δx, Δvx, Δy, Δvy, Δz, Δvz]          （rowwise().sum()）
  u     = −RPVVs.reshaped(2,3).colwise().sum()            = −Σ_j w_ij [ (p_i − p_j − (f_i − f_j)) + (v_i − v_j) ]   逐轴
  a_cmd = [PID_x(u_x), PID_y(u_y), PID_z(u_z)]            增益来自 gains[9]（NED 轴顺序）
  setpoint.position = setpoint.velocity = NaN ；setpoint.acceleration = a_cmd       （PX4：只控加速度时 p、v 必须为 NaN）
  ```
- `timer_callback()`：本周期收到过邻域消息就进入 ACCELERATION 模式并调用 `compute_command`；否则 `command_tp=0`，切到 POSITION 模式，保持 `default_pose = (0, 0, −5)`（**各机自身局部 NED 原点上方 5 m**）。
- README 说论文里的 σ 饱和算子是 `σ_b(a) = sign(a)·min(|a|, b)`，但**代码没有实现 σ**，改成了“对一致性和做逐轴 PID”。作者声称这样“可以控制 z 轴”。
- 默认增益（`config/control_config.json`，NED 轴）：x `(Kp,Ki,Kd)=(1.0,0,0)`，y `(2.5,0,0)`，z `(2.75,0.1,0.001)`。

### 2.9 `PID.hpp`

- 模板 `PID<Numerical>`，构造参数 `(max_anti_windup, anti_windup_max_saturation)`，控制器里是 `PID{3., 2u}`。
- `update(error, dt)`：`Kp·e + Kd·(e − previous_error)/dt + Ki·∫`（梯形积分）。
- 抗饱和逻辑：积分到达 ±max 时计数，超过 N 次就清零。**到达上限时并不钳位**，积分值会越过上限直到被清零。
- **`previous_error` 从未被赋值**（§2.11）。

### 2.10 `custom_msgs`

```text
Neighbors.msg                 : uint8[] neighbors_ids ; px4_msgs/VehicleLocalPosition[] neighbors_position
WeightedTopologyNeighbors.msg : 同上 + float64[] weights
```

直接复用 `VehicleLocalPosition`（字段多、体积大）来装“相对量”，语义上有些含混。我们在 WebSocket 上的 `swarm.topology` 只发 `(i, j, w_ij)` 三元组（§4.3）。

### 2.11 缺陷清单（移植时逐条修正）

| # | 位置 | 缺陷 | 影响（实测/推理） | 修正 |
|---|---|---|---|---|
| B1 | `include/PID.hpp::update` | 从不执行 `previous_error = error` | D 项恒为 `Kd·e/dt`，等价于一个随 dt 变化的附加 P 增益 | 每次 update 末尾更新；D 项对测量值微分并加一阶低通 |
| B2 | `WeightedTopologyController.cpp::compute_command` L113 | `now_tp = seconds()/1000`，并存进 `float command_tp` | float32 在 1.79e6 附近的 ULP 为 0.125（即 125 s）。dt 在 **[−0.0624, +0.0626]（千秒）锯齿跳动**，真值应为 1e-4；**47.8% 的周期 dt < 0**（`r26_formation_out.json: dt_bug`） | 用 `steady_clock` 或 `get_clock()->now()` 的 double 秒；dt 钳到 `[0.5, 2]·T_ctrl` |
| B1+B2 | 叠加 | z 轴等效比例增益 `Kp + Kd/dt` | 1 小时 36k 个周期中：**0.23% 的周期增益为负（正反馈）**，0.32% 超过 2·Kp；积分步长符号随机，退化为随机游走（`r26_pidbug_out.json`） | 同上 |
| B3 | `WeightedTopologyNeighbors.cpp::vectors_to_Vector3d` L43–49 | `(x.size(), y.size(), z.size()) == (n,n,n)` 是逗号表达式，只比较了 z；`formation.reserve()` 之后 `std::transform` 写入 `begin()` | 未定义行为，`formation.size()` 仍为 0，碰巧能读到数据 | 三项分别比较；`resize` 或 `emplace_back` |
| B4 | 同文件 `process_neighborhood` | 领航机分支不重置 `prcs_neighborhood` | 下一架跟随机的 min 里混入领航机邻居的 PrC，权重串位 | 改为每周期对邻接图做一次 BFS 求跳数（§3.2），与扫描顺序无关 |
| B5 | `NearestNeighbors.hpp` 定时器 | 仅当**全部**机体本周期都上报过位置才计算 | 任一机迟到，**全体**邻域消息本周期缺失，所有跟随机都回退到 POSITION 模式，Offboard 类型在 ACC 和 POS 之间抖动 | 用各机最新位置加陈旧度阈值（例如 300 ms），对陈旧机单独剔除 |
| B6 | `WeightedTopologyController::timer_callback` | 每个 100 ms 周期都重置 `is_neighborhood_empty`；两个墙钟定时器同频但不同相 | 相位漂移时会周期性“漏一帧”，引发模式切换 | 保持上一次指令，连续 3 个周期无邻域才降级 |
| B7 | 同上 | 回退目标 `default_pose=(0,0,−5)` 是各机**局部**原点 | 脱连后各机飞回各自出生点上空，与队形无关 | 回退到 HOLD（原地悬停）并上报 `detached` 事件 |
| B8 | 律 A 本身 | 只有“邻居可见”检测，没有“与领航机连通”检测 | §3.3：5 m/s、R=20 m 时 79% 的周期脱连，子群漂走，兜底不触发 | 每周期做连通性检查；不连通即 `detached` → HOLD 或改用律 B |
| B9 | `ChangeWaypoint::writeWP` | `timestamp` 用秒；航点 x/y 用 ENU、z 用 NED 的混合约定 | 可读性差，易出错 | 统一 World ENU，只在 Adapter 边界转换（r15 R7） |
| B10 | `simulation_node.py` | 依赖 `gnome-terminal` | 无法在 CI 或无头服务器运行 | 用 `subprocess.Popen` 加进程组，或直接用 SIH |

### 2.12 PX4-Aerial-Swarm-Reconstruction 目录

```text
src/px4_msgs/                      # 整包 vendored（227 msg + srv/VehicleCommand.srv），必须与固件版本一致
src/px4_swarm_controller/
  libs/Control/Control.{hpp,cpp}   # 唯一的逻辑：集中式 N 机控制节点
  src/arming.cpp                   # main → spin(ctrl::Control)
  config/config.yaml               # num_drones、initial_positions、setpoints{id: [[N,E,D,yaw],…]}
  launch/px4_multi_sim.launch.py   # 读 config（相对路径 'src/px4_swarm_controller/config/config.yaml'）→ simulation_node.py
  launch/integration_test.launch.yaml  # catch_ros2 集成测试
  worlds/grid_plan.world           # Gazebo Classic SDF 1.7，100×100 m 地面，20 栋楼 + 水塔/吊车/车辆等
  test/{test.cpp, test_level2.cpp, main.cpp}
UML/{initial,revised}/*.pdf        # 类图（Control）+ 活动图（barrier 流程）
.github/workflows/run-unit-test-and-upload-codecov.yml
```

### 2.13 `ctrl::Control`（`libs/Control/Control.cpp`）：集中式多机执行器

- 构造：
  - `YAML::LoadFile("../../install/px4_swarm_controller/share/…/config.yaml")`，**依赖当前工作目录的相对路径**；
  - 为每机建 4 条话题（3 pub + 1 sub，`QoS(10).best_effort()`）；
  - 100 ms 定时器。
- 定时器流程（活动图 `UML/revised/Activity_diagram.pdf`）：
  ```text
  counter < 50（5 s 预热）：每机发 OffboardControlMode{position} + TrajectorySetpoint(0,0,−5,0)
  counter == 50：每机连续 5 次 arm() + offboard_mode()（fire-and-forget 话题，target_system=0）
  之后每周期：publish_default_setpoints()
      for i：发布 setpoints[i][k]；若 ‖p_i − sp_i‖ < 0.3 m，则 reached[i] = true
      if all(reached)：k = min(k+1, K−1)；reached 全部清零        ← 全体 barrier 同步
  ```
- 小缺陷：`publish_default_setpoints()` 写在 `for i` 循环内部，每周期被调用 N 次，因此是 N² 次发布（20 机时每周期 400 条 setpoint）；最后一个航点到达后没有降落或返航。
- `config.yaml` 解读（Gazebo ENU spawn，setpoint 是**各机局部 NED**，原点在 spawn 点）：

| 组 | 机号 | spawn（ENU） | 航点序列（局部 NED） | 飞行方向 | 高度 |
|---|---|---|---|---|---|
| G1 | 1–5 | x=37（东缘），y=−18…32，间距约 12 m | E = 0, −15, …, −75 | 自东向西扫 | 3 m |
| G2 | 6–10 | y=36（北缘），x=20.5…−35，间距约 13.5 m | N = 0, −14, …, −69 | 自北向南扫 | 5 m |
| G3 | 11–15 | x=−37（西缘） | E = 0, +15, …, +73 | 自西向东扫 | 7 m |
| G4 | 16–20 | y=−36（南缘） | N = 0, +16, …, +70 | 自南向北扫 | 10 m |

  即 **4 组 × 5 条平行航带，相向交叉扫掠，靠 4 个高度层（3/5/7/10 m）避免组间冲突**。楼高 11.4–13.9 m（`law_office` 等模型的 `<size>`），所以飞机是在街道峡谷里飞，spawn 位置就是街道中线。这是一种“人工排好的覆盖 + 分层去冲突”，我们在 §3.8–§3.11 把它自动化。
- 点云部分：world 与模型中**没有任何相机或深度传感器**（`grep sensor|camera|depth` 只命中 GUI 相机）。README 说遇到 namespace 问题，推测是多实例下 gazebo_ros 相机插件的话题没有按实例区分命名空间（需要在 `*.sdf.jinja` 里按实例写 `<ros><namespace>`）。所以这个仓库里**没有**点云融合代码可借鉴。

### 2.14 测试与 CI（PASR）

- L1（GTest，`test/test.cpp`）：对 `publish_offboard_control_mode / publish_trajectory_setpoint / arm / offboard_mode / publish_vehicle_command` 做“自发自收”断言；`PublishDefaultSetpoints` 只写了 `SUCCEED()`。
- L2（catch_ros2，`test_level2.cpp`）：起 `arm` 节点，断言 5 s 内能在 `/px4_1/fmu/in/*` 收到消息。
- CI：`osrf/ros:humble-desktop` 容器 + `do-tests-and-coverage.bash`（lcov 合并）+ Codecov。**可参考的是“无 PX4 也能跑的 L2 话题契约测试”思路**。我们对应的做法：Gateway 契约测试用 r21 的 `fake_px4` 或 MockAdapter 进程内跑。

---

## 3. 可复用算法与实现（含伪代码/参数）

### 3.0 坐标约定与边界换算（两个仓库都在这里踩过坑）

```text
World ENU（规范）:   p = (E, N, U)，yaw_enu 从东轴逆时针
PX4 local NED:      p_ned = (N, E, D)，yaw_ned 从北轴顺时针，原点 = 该机 EKF 原点（SITL 下为 spawn 点）
Gazebo（Classic）:  ENU

ned_to_enu(p) = (p.y, p.x, −p.z)           enu_to_ned(p) = (p.y, p.x, −p.z)      （自逆）
yaw_enu = π/2 − yaw_ned                     （归一到 (−π, π]）
p_world = T_world←local_i · ned_to_enu(p_local_ned)，SITL 下 T 只含平移 = spawn_enu_i       （r19 §3.9）
setpoint 下发：p_local_ned = enu_to_ned(p_world − spawn_enu_i)
```

规则：Mission、Formation、Coverage 全部在 World ENU 里规划和计算。**只有 Adapter 做 NED 转换**（r21 结论 4，r15 R7）。PX4_Swarm_Controller 的“YAML 用 ENU x/y + NED z”那种混合约定禁止出现。

### 3.1 Offboard 生命周期（MockAdapter 与 PX4 Adapter 语义一致）

两个仓库合起来，给出了 PX4 Offboard 的完整前置条件。MockAdapter 要**模拟同样的状态机**，这样 V0.2 换成 SITL 时上层不用改：

```text
state PRE_STREAM:
    以 ≥10 Hz（我们用 20 Hz，r21）发送 OffboardControlMode{flags} + TrajectorySetpoint
    持续 ≥ 1 s 后（PASR 用 50 帧 @10Hz = 5 s，偏保守）→ REQUEST
state REQUEST:
    send DO_SET_MODE(176, p1=1, p2=6)，然后 ARM(400, p1=1)
    PX4_Swarm_Controller：服务调用，等待 reply.result==0，每 1 s 重试
    PASR：话题 fire-and-forget，连发 5 次，不确认 ← 不推荐
    我们：MAVSDK action/offboard 或服务调用；以 vehicle_status 观测到 nav_state==OFFBOARD && ARMED 为准；超时 3 s 重试，最多 5 次
state ACTIVE:
    持续推流 ≥ 2 Hz；中断超过 COM_OF_LOSS_T（默认约 1 s）后 PX4 进入失联动作（COM_OBL_RC_ACT）
    Mock：同样实现“推流中断 → HOLD”，并上报 health.offboard_lost 事件（r24 §4.7）
setpoint 类型语义（TrajectorySetpoint，NED）：
    某一项为 NaN = 该阶不受控；OffboardControlMode 的 flag 选择“最高受控阶”
    位置+速度+加速度前馈：三项全填（编队推荐，§3.4）
    纯加速度：p、v 置 NaN（律 A 的用法，对状态估计误差更敏感，不推荐）
```

### 3.2 邻接图与 PrC 权重（port，修正版）

用途：编队一致性项的权重、UI 上的“集群拓扑”叠加层、脱连检测。落点：`swarm/topology.py`（Gateway 内 10 Hz，向量化）。

```python
def topology(p, leaders, r_nb, stale_mask):
    # p: (n,3) World ENU；leaders: bool[n]；stale_mask: 位置超过 300 ms 未更新的机
    D = pairwise_dist(p)                         # n ≤ 50 用 numpy 全矩阵；n > 50 用 3D 均匀网格哈希（cell = r_nb）
    adj = (D <= r_nb) & (D >= 0.01) & ~stale_mask[:, None] & ~stale_mask[None, :]
    hop = bfs_hops(adj, sources=leaders)         # 与扫描顺序无关；不可达 = inf
    prc = where(isinf(hop), n, hop + 1)          # 领航机 prc = 1
    W = adj * (1 / prc)[None, :]
    W /= W.sum(1, keepdims=True).clip(min=eps)   # w_ij = (1/prc_j) / Σ_{k∈N_i} 1/prc_k
    detached = isinf(hop) & ~leaders             # 关键新增：与任一领航点不连通
    return adj, W, prc, detached
```

参数：`r_nb` 取“槽位最大间距 × 1.5”和“通信半径”中的较小值。**不要用仓库的 3 m**：它只适用于 1–2 m 间距的微型队形，稍有滞后就断链。拓扑以 2 Hz 通过 `swarm.topology` 推给 UI，以边（i, j, w_ij）表示。

### 3.3 仓库律 A（weighted topology 一致性）：公式、实验与定位

```text
论文形式（据 README 描述）：u_i = −Σ_{j∈N_i} w_ij [ σ_b1(k_p·e^p_ij) + σ_b2(k_v·e^v_ij) ]，σ_b(a) = sign(a)·min(|a|, b)
仓库实现：                   u_i = −Σ_{j∈N_i} w_ij [ (p_i − p_j − (f_i − f_j)) + (v_i − v_j) ]，a_i = PID_axis(u_i)
                             e^p_ij = p_i − p_j − (f_i − f_j)，f 为各机槽位（世界朝向固定，不随领航机航向旋转）
```

实验（`r26_formation.py` / `r26_formation2.py`）：
- 被控对象：加速度一阶滞后 τ=0.15 s，|a_xy| ≤ 3 m/s²（`MPC_ACC_HOR`），|a_z| ≤ 2，|v_xy| ≤ 12；物理 100 Hz，控制 10 Hz。
- 律 A 用仓库增益的 P 部分，按 ENU 轴映射为 (2.5, 1.0, 2.75)；I/D 因 B1/B2 缺陷而无意义，已剔除。
- 队形：仓库原配置换算成 ENU 后，相对领航机的偏移为 d2 = (0, −1, 0)、d3 = (2, −1, 1)。

| 领航机轨迹 | 律 A（r_nb=3 m） | 律 A（r_nb=∞） | 律 B（虚拟结构+前馈） | 备注 |
|---|---|---|---|---|
| 直线，0→5 m/s 速度阶跃 | RMS 0.05 m | — | — | 直线跟踪没问题 |
| 圆 R=20，1 m/s | 0.058 / max 0.075 | 0.058 | **0.010** | |
| 圆 R=20，3 m/s | 0.52 / 0.69 | 0.52 | **0.039** | 律 A 在弯道稳态滞后 ∝ a_c/K |
| 圆 R=20，4 m/s | 1.13 / 2.32 | — | — | 已接近 3 m 邻居半径 |
| 圆 R=20，5 m/s | **199.8 / 389.6，脱连 79%** | 1.54 / 2.06 | **0.090** | 子群漂走，兜底不触发（B8） |
| 圆 R=50，5 m/s | 0.59 / 0.75 | — | — | |

结论：律 A 可以作为 **V0.6 的“分布式一致性”教学/对照实现**（UI 可切换“consensus”模式并展示拓扑权重）。但**生产主律用律 B**（§3.4）：本项目的 Gateway 是集中式的，领航点状态随时可取，没有理由放弃前馈。律 A 的真正价值在 V1.0：去中心化或通信受限（ANet 中继、链路丢包）的研究场景下作为对照基线。

### 3.4 推荐主律：虚拟结构 FormationTracker（前馈 + 航向滤波 + 一致性 + 分离）

落点：`sim_gateway/core/formation_tracker.py`（r21 Tracker 层，20 Hz，向量化）。输出每机 `(p*, v*, a*, ψ*, ψ̇*)`，由 Adapter 下发。Mock 由 r19 级联模型执行；PX4 用 TrajectorySetpoint 三项全填，或 MAVSDK Offboard 的位置+速度+加速度 setpoint。

```python
class FormationTracker:                               # World ENU，所有量对全体成员向量化
    def __init__(self, spec, members, anchor_path):
        off = formation_slots(spec.shape, n, spec.spacing, **spec.params)       # §3.5，FLU（x 前 y 左 z 上）
        self.off = off - off.mean(0) if spec.anchor == "virtual" else off - off[0]
        self.slot = capt_assign(...)                  # §3.6：成员 → 槽位
        self.psi_f, self.w_f = anchor_path.heading(0), 0.0

    def step(self, t, dt, P, V):                      # P、V：(n,3) 成员当前状态
        pA, vA, aA, psiA, wA = self.anchor(t)         # 虚拟锚点沿路径（PathTracker / pure pursuit，r21 §3.7）；
                                                      # 或领航机遥测（anchor="leader"）
        # 1) 编队航向
        if mode == "aligned":  psi_f, w_f, al_f = psiA, wA, 0
        elif mode == "filtered":                      # 二阶临界阻尼，τψ 默认 2 s
            dpsi = wrap(psiA - self.psi_f)
            al_f = dpsi / tau**2 - 2 * self.w_f / tau
            self.w_f = clip(self.w_f + al_f * dt, -w_fmax, w_fmax); self.psi_f += self.w_f * dt
            psi_f, w_f = self.psi_f, self.w_f
        else:  psi_f = spec.world_heading; w_f = al_f = 0          # "world"：队形朝向固定
        # 2) 刚体参考（带转动前馈）
        r   = Rz(psi_f) @ self.off[self.slot].T                     # (n,3)
        w   = (0, 0, w_f); al = (0, 0, al_f)
        p_s = pA + r
        v_s = vA + cross(w, r)
        a_s = aA + cross(al, r) + cross(w, cross(w, r))
        # 3) 可选修正（在 a* 上叠加，限幅 1.5 m/s²）
        e = P - p_s
        a_c = -kc * (W[..., None] * (e[:, None] - e[None])).sum(1)             # PrC 权重一致性（§3.2 的 W）
        a_c += sum_{d_ij < d_safe} k_rep * (1/max(d_ij,0.3) - 1/d_safe) * unit(p_i - p_j)   # 近距分离
        # 4) 输出
        yaw_i = psi_f if spec.yaw == "formation" else per_slot_yaw
        return Setpoint(p=p_s, v=v_s, a=a_s + clip(a_c, 1.5), yaw=yaw_i, yawspeed=w_f)
```

| 参数 | 默认 | 说明 |
|---|---|---|
| 控制频率 | 20 Hz | 实测 50/20/10/5 Hz 的 RMS 为 0.062/0.063/0.064/0.067 m（V7，R=40，5 m/s），频率不敏感 |
| kp / kv（原型中的外环） | 1.5 / 2.5（ζ≈1.02） | 接 PX4 或 r19 Mock 时由 MPC 增益代替，只下发 p/v/a 参考 |
| τψ（航向滤波） | 2.0 s | R=15 U 形弯：τ=1/2/4 s 时 RMS 为 0.071/0.065/0.070 m，最大 0.25/0.18/0.22 m |
| w_fmax | 由可行性计算（§3.5） | 保证最外槽位的速度和加速度不越界 |
| kc（一致性） | 0.8，r_c = 25 m | 对外部扰动（风）有“抱团”效果；**分离半径必须小于槽位间距** |
| d_safe / k_rep | 2.0 m / 4.0 | 仓库队形间距只有 1 m，与 d_safe=2 m 冲突，导致 RMS 升到 0.28 m（实验已观察到） |
| 规模 | 向量化 | 律 C 每步（含全矩阵距离）耗时：n=10 为 0.49 ms，n=50 为 1.17 ms，**n=200 为 13 ms**。n > 50 时改用网格哈希，或只在 10 Hz 拓扑周期计算 |

**V7 编队弯道对比（`r26_formation_out.json` / `r26_formation2_out.json`）**：

| 场景（5 m/s） | aligned | filtered τ=2 s | world-fixed |
|---|---|---|---|
| 圆 R=40 | 0.064 / 0.08 | — | 0.061 / 0.061 |
| U 形弯 r=30 | 0.29 / 1.97（饱和 2.2%） | 0.055 / 0.090 | 0.060 / 0.089 |
| U 形弯 r=15 | **4.87 / 27.9（饱和 15.5%）** | **0.065 / 0.178** | 0.072 / 0.161 |
| U 形弯 r=15 + 一致性/分离（律 C） | 6.28 / 34.3，**最小间距 0.24 m** | — | 0.070 / 0.158 |

（表中格式为 RMS / 最大值，单位 m。）

### 3.5 队形槽位生成器（line / column / V / echelon / grid / circle）

槽位在 FLU 编队坐标系中定义，0 号槽为锚点。Python（后端）与 TypeScript（前端预览）**各实现一份，用 golden 测试保证一致**。

```text
formation_slots(shape, n, s, half_angle=35°, cols=ceil(√n), radius=None, dz_per_rank=0) → (n,3)
  k = 1..n−1，r = ceil(k/2)，side = +1（k 奇数，左）/ −1（k 偶数，右）
  line    : (0,              side·r·s,           0)        横队，锚点居中，左右交替
  column  : (−k·s,           0,                  0)        纵队
  V       : (−r·s·cosα,      side·r·s·sinα,      0)        楔形，锚点在尖端
  echelon : (−k·s·cosα,      −k·s·sinα,          0)        梯队（右）
  grid    : 行 row = ⌊idx/cols⌋，列 col；按 (row, |col−(cols−1)/2|) 排序后第 0 个为锚点
            (−row·s, ((cols−1)/2 − col)·s, 0)，最后整体减去锚点
  circle  : 锚点居中；m = n−1，R = max(s, s / (2·sin(π/m)))（保证弦长 ≥ s）
            k 号：(R·cos(2π(k−1)/m), R·sin(2π(k−1)/m), 0)
  dz_per_rank（可选）：z += ceil(k/2)·dz，用于纵向去冲突和演示层次感
  anchor="virtual"：off −= mean(off)（虚拟锚点 = 队形形心，CAPT 必需，§3.6）
```

**可行性检查**（Mission 下发前校验；UI 在 Slider 上实时提示“该间距/速度下弯道不可行”）：

```text
给定路径最大曲率 κ_max（= 1/R_min）、巡航速度 V，槽位 i 在编队系中的偏移 (x_i, y_i)：
  aligned 模式：
    内侧速度  v_i = V·(1 − κ·y_i)  需 > 0         ⇒ κ_max·max|y_i| < 1（否则内侧机需要倒飞）
    外侧速度  V·(1 + κ·max|y_i|) ≤ v_max
    向心加速度 V²·κ·(1 + κ·max|y_i|) ≤ a_max
    曲率突变  Δv_i = V·Δκ·|x_i|（直线进弧）       ⇒ 必须用 filtered 模式或路径做回旋线（clothoid）过渡
  filtered 模式：w_f ≤ w_fmax = min_i( a_max / (|r_i|·(ω + …)) ) 近似取 a_max / (V + max|r_i|·ω)，τψ ≥ 1 s
  不可行 ⇒ 提示：降速 / 增大转弯半径 / 改 world-fixed 模式 / 缩小间距
```

### 3.6 槽位分配与集结、变队形（CAPT）

落点：`swarm/formation/assign.py`（Hungarian 实现，见 `r26_formation.py::hungarian`，已与穷举对拍 n=2..7；耗时 n=50 为 10.4 ms，n=200 为 89 ms）。

```python
def capt_assign(P_now, slots_world):            # 全部槽位可分配（虚拟锚点）
    C = sqdist(P_now, slots_world)             # 平方距离：直线同步轨迹无碰撞的关键（Turpin, Michael, Kumar 2014）
    return hungarian(C)

def reshape(members, spec_from, spec_to, T_min=4.0):
    G = anchor + Rz(psi_f) @ slots(spec_to).T
    a = capt_assign(P, G)
    T = max(T_min, max_i |G[a_i] − P_i| / (0.6·v_max))                 # 统一到达时间
    for t in [0, T]: s = smoothstep(t/T)，p*_i = P_i + (G[a_i] − P_i)·s  # 全员同一 s(t)
    # 在 FormationTracker 里实现为“槽位偏移插值”：off_i(t) = off_from_i + (off_to_{a_i} − off_from_i)·s(t)
    # 这样锚点可以一边移动一边变形；v*、a* 需要叠加 d(off)/dt 项

def assemble(members_on_ground, spec, z_form):
    # 1) 原地垂直起飞到 z_form（各机水平位置不变）；地面间距 ≥ 2√2·R_safe
    # 2) capt_assign(P_air, slots_world) → 同步直线飞入槽位
    # 3) 进入 HOLD_FORMATION
```

保证条件：起点之间、终点之间的间距都 > 2√2·R_safe（R_safe 为机体半径加余量，P600 取 1.0–1.5 m，则间距应 ≥ 2.8–4.2 m），且全员同步插值。

实测：
- 停机坪 3×4（4 m）→ 圆（6 m）：按编号直连，最小间距 0.21 m；CAPT 后为 3.33 m；总路径 389.7 m → 365.8 m。
- 5 种队形两两互变（9 机，6 m）：锚点**固定为领航机**时，grid↔circle、circle/column 相关的 4 组变换最小间距只有 0.02 m（有机体直线穿过领航机）；**虚拟锚点**时 20 组变换全部 ≥ 4.24 m。
- 所以：**“领航机”只是一个 UI 角色（显示和跟随相机用），控制上的锚点必须是虚拟点。** 这也让“领航机失效”变得平凡（§3.7）。

### 3.7 领航机失效、成员增减

```text
anchor = virtual：领航机失效 = 普通成员失效；Formation 标记该槽位空缺
    策略 a（默认）：保持队形，空槽保留（便于回补）
    策略 b：compact，n−1 重新生成槽位，CAPT 重新分配（一次 reshape）
UI 角色 leader 转交：选 hop 最小（PrC 最小）或离锚点最近的成员
成员加入：从外部 GOTO 到“新增槽位”附近后，再做一次 CAPT
detached（§3.2）：→ 该机 HOLD，并发出 swarm.detached 事件（Sonner toast + 拓扑边变红）
```

### 3.8 区域覆盖规划器（AREA_COVERAGE）

落点：`swarm/mission/coverage/`（Python，Gateway 同进程或 worker）；前端只画预览，不规划。输入全部是 World ENU。

**3.8.1 传感器几何**

```text
相机（天底）：swath W = 2·h·tan(HFOV/2)；航向长 Lf = 2·h·tan(VFOV/2)
  航带间距  d = (1 − o_side)·W
  拍照间距  b = (1 − o_front)·Lf        （照片数 = Σ ceil(L_lane / b) + 1）
  GSD       = h·pixel_pitch / f
  对高度为 z_s 的表面：W(z_s) = 2·(z_fly − z_s)·tan(HFOV/2)
  → 要让 95% 的表面也满足重叠，取 h_eff = z_fly − P95(DSM∩AOI)
LiDAR：swath 由有效量程 R_eff 与安装角决定。天底锥形：W = 2·h·tan(FOV/2)；
  MID-360（水平安装，垂直 −7°~52°）的地面环带模型见 r04，按“地面环带内径/外径”计算等效 swath
示例相机（原型参数）：4:3，HFOV 73.7°、VFOV 53.1°（tan = 0.75 / 0.5），o_side 70%、o_front 80%，V = 8 m/s，a_max = 2 m/s²
```

**3.8.2 高度模式**

```text
fly_over（默认，城市建图）：z_fly = max(z_g + h_agl, P99.9(DSM∩AOI) + clearance)，所有航带同高，无障碍裁剪
fixed_agl（街道峡谷 / 立面，PASR 的做法）：z_fly = z_g + h_agl；no-fly = dilate(DSM > z_fly − clearance, r_safe) ∪ 禁飞多边形
per_lane（V0.6+ 可选）：z_lane = max(DSM 在航带走廊 ±W/2 内) + clearance，每条航带高度不同，转弯时加爬升/下降
z_g：DTM（若有）；否则取 AOI 内 DSM 的 P1（UrbanScene3D SF 实测 z_g = −26.31，文件坐标，导入时需先归零，见 r10）
```

**3.8.3 扫描角**

```text
候选 Θ = {AOI 凸包各边方向} ∪ {0°, 5°, …, 175°}
θ* = argmin_θ T_single(lanes(AOI, d, θ))        # 代价 = Σ(L/V + V/a) + Σ gap/V
凸多边形的最优方向平行于最小宽度方向对应的边（旋转卡壳）；凹多边形靠采样
```

**3.8.4 航带生成（扫描线 × 边交，支持凹多边形）**

```python
def lanes_for_angle(poly, d, theta):
    P = poly @ R(−theta).T                       # 旋转后航带水平
    ymin, ymax = P[:,1].min(), P[:,1].max()
    nl = ceil((ymax − ymin) / d)
    ys = ymin + (arange(nl) + 0.5) * (ymax − ymin) / nl       # 均匀重排，两端各留半个间距
    for y in ys:
        xs = sort([x_intersect(edge, y) for edge in edges if edge straddles y])   # 半开区间规则防顶点重复
        segs = [(R(theta)@(xs[2k], y), R(theta)@(xs[2k+1], y)) for k]            # 偶奇配对 → 凹形得到多段
    return lanes（按扫掠顺序）
```

**3.8.5 障碍裁剪 + BCD-lite 分胞**

```python
def clip(lanes, blocked, min_len=6.0):          # 沿段以 res/2 采样查 no-fly 栅格，取连续 free 游程，丢弃 < min_len
    ...
def bcd_cells(lanes):
    # 按航带顺序扫：新段与“开放胞”的末段在扫描方向投影上重叠
    # 恰好 1 对 1 重叠 → 续接该胞；否则（split/merge 事件）→ 关闭相关胞并新开胞
    ...
def order_cells(cells, start):                   # 贪心近邻；每个胞有 4 种进入方式（首/末航带 × 方向）
    ...
```

**3.8.6 结果（San Francisco，`r26_coverage_out.json`）**

DSM：500 万点，加载 0.21 s；2 m 栅格用 sort + `maximum.reduceat` 耗时 0.9 s，尺寸 372×360，空格 2.4%；相对高度 P50 10.9 m、P95 20.0 m、最大 53.7 m。

| AOI | 模式 | 航带/段/胞 | 路径 | 照片 | 覆盖率 | 规划 | 1 机 | 2 机 | 4 机 | 6 机 | 8 机 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 矩形 0.479 km² | fly_over 60 m（h_eff 40 m，W 60 m，d 17.9 m，b 8 m） | 38/38/1 | 26.75 km | 3420 | 100% | 119 ms | 61.1 | 33.5 | 18.2 | 14.3 | 11.2 |
| 矩形 | fixed_agl 25 m（W 37.5，d 11.2） | 63/227/138 | 34.48 km | 7255 | 92.0% | 443–480 ms | 104.0 | 54.3 | 29.6 | 21.0 | 16.2 |
| L 形 0.229 km² | fly_over 60 m | 32/32/1 | 12.70 km | 1635 | 100% | 95 ms | 32.0 | 18.6 | 10.7 | 8.4 | 6.4 |
| L 形 | fixed_agl 25 m | 51/136/76 | 14.91 km | 3196 | 90.0% | 231 ms | 50.8 | 28.3 | 15.3 | 11.2 | 8.7 |

（后 5 列为 makespan，单位 min，包含从机库出发和返航。均衡度 max/mean：fly_over 为 1.07–1.21，fixed_agl 为 1.02–1.10。）

观察：
- 单机 61 min 超过 P600 的一次续航，这就是多机的现实动机。
- 楼下模式有 8–10% 覆盖不到，因为高于 z_fly − clearance 的屋顶被裁掉了，需要补一趟 fly_over。
- BCD-lite 在城市网格里会过度分胞（138 胞），但由于各胞按贪心顺序串联、切分以时间为单位，makespan 均衡依然良好。

### 3.9 多机切分与分配（contiguous balanced split + Hungarian）

```python
def split_contiguous(seq, k):                    # seq：全局牛耕顺序下的航段序列
    w_j = L_j/V + V/a_max + gap_j/V               # 每段代价（含到下一段的衔接）
    cs  = cumsum(w)
    cuts = [searchsorted(cs, cs[-1]*m/k) for m in 1..k−1]
    return 连续的 k 块                            # 空间上连续 = 条带状分区，互不交叉
    # 改进（建议实现）：在航段内部按目标代价精确切开（插入切点），把均衡度从 1.2 降到接近 1.0；
    # 实测 6 机 1.206 的主要原因是航段粒度（每条约 1.5 min）

def assign(chunks, homes):
    C[i,j] = min(|home_i − entry_j| + |exit_j − home_i|, 反向同理) / V
    a = hungarian(C)；为每个 chunk 记录是否反向
```

**电量约束**：`k_min = ceil(T_single / (η·T_endurance))`，η = 0.7（30% 余量）。如果某块超出续航，就在块内按续航拆成多个 sortie（中途回巢换电），这放到 V1.0 的 ANet 能力感知里做。

**故障重规划**：失效机未飞完的后缀，加上各机的剩余后缀，按原全局顺序拼接，再 `split_contiguous`，最后从各机当前位置做一次 Hungarian，1 s 内完成。

**异构速度/电量加权**（V1.0，ANet capability）：让切分目标按 `v_i·E_i` 比例分配：`cs[-1]·Σ_{m≤j} c_m / Σ c`。

### 3.10 备选：编队整体扫掠（group sweep）

```text
k 机横队（world-fixed，间距 = d），每一趟覆盖 k 条航带；趟长取 k 条航带的并集跨度；趟末整体横移 k·d
T ≈ Σ_passes (L_pass/V + V/a) + (passes−1)·(k·d/V + V/a)
```

实测（不含往返）：矩形 k=4 为 16.8–17.3 min，k=8 为 8.9–9.1 min；L 形 k=8 为 4.8–5.9 min。和分区覆盖（含往返）的 18.2 / 11.2 / 6.4 min 相近。

但它的前提是全员同步转弯，缺一架就漏一条带，也无法处理障碍裁剪。**适合演示“编队 + 覆盖”的组合，或者要求机间保持中继链路的场景。** 实现上就是 FormationTracker（line、world-fixed）加一条“横移牛耕”的锚点路径，不需要新代码。

### 3.11 任务执行器：同步策略、高度分层、接受半径

来自 PASR 的 barrier，加以泛化。落点：`swarm/mission/executor.py`（事件驱动的 FSM，每机一个游标，r24 的 Safety 层可以否决）。

```text
sync_policy:
  free（默认）：各机独立推进
  barrier：全员到达第 k 个同步点才一起推进到 k+1（PASR 行为；适合编队演示、同步拍照）
           每个同步点带超时 T_b（默认 30 s）；超时后把未到的机标记为 lagging，其余继续
  timed：按计划时间表推进（预计到达时间 ETA，用于回放一致性）
acceptance_radius：r_acc = max(1.0, 0.25·V) m（PASR 用 0.3 m、PSC 用 0.1 m，对真实 PX4 太严，容易“卡点”）
transit_layers（PASR 的分层去冲突泛化）：
  z_transit = max(DSM 在各机转场走廊内) + clearance
  各机转场高度 z_i = z_transit + rank_i·Δz，Δz = 4 m，rank 按 Hungarian 分配结果排序
  作业段在各自分区内同高飞行（分区天然不相交），只有转场/返航用分层；另有 r24 的 min_sep 监控兜底
```

### 3.12 “扫描揭示”点云融合可视化（MVP 核心亮点）

目标：在 mock 中展示“多机扫城 → 点云逐步融合”，同时**不增加渲染负担**，反而用覆盖状态驱动 LOD 降负载。

**3.12.1 服务端覆盖栅格**（落点：`sim_gateway/coverage.py`）

```text
grid_owner : u8[H,W]    0 = 未扫；否则为首个扫到的机号 + 1（最多 254 机，超出取模）
grid_count : u8[H,W]    扫描次数，饱和到 255（重叠热力图、质量指标）
grid_t0    : u16[H,W]   首次扫描时刻（以 0.5 s 为单位，相对任务起点），用于前端“新扫描辉光”（可选）
每个 tick（5 Hz）：
  每机的足迹 = 相机 swath 矩形（按 yaw 旋转）或 LiDAR 圆盘，半径 r = W/2
  在包围盒子窗内打点：new = mask & (owner == 0) → owner = id+1；count += mask
  记录脏瓦片（32×32）
推送：
  增量帧：每 tick 的脏瓦片（zlib）
  快照：订阅、seek 或每 5 s 时发整图 zlib
```

实测开销（Python/numpy）：

| 场景 | 打点 | 脏瓦片/tick | 增量带宽 | 整图快照 |
|---|---|---|---|---|
| SF 372×360@2 m，4 机沿规划飞行 | 0.25 ms/tick | 5.2 | raw 20.5 kB/s → zlib **0.76 kB/s** | 0.6 kB |
| SF，8 机 | 0.41 ms/tick | 6.6 | zlib **1.09 kB/s** | 0.7–4.4 kB |
| 100 机随机，2048² | 3.0 ms/tick | 305 | — | 76 kB / 21 ms |

结论：**SF 规模可以直接以 1–2 Hz 发整图快照**，实现最简单；NY、Shanghai 这类大图用“脏瓦片增量 + 5 s 快照”。

**3.12.2 WebSocket 二进制帧**（与 r15 §3.19 / r21 的帧头风格一致）

```text
u8  type = 0x21 (COVERAGE_TILES) | 0x22 (COVERAGE_SNAPSHOT)
u8  codec (0=raw, 1=zlib) ; u8 layer (0=owner, 1=count) ; u8 reserved
u32 seq ; f64 t_sim_s
u16 grid_w ; u16 grid_h ; f32 x0 ; f32 y0 ; f32 res_m            # World ENU 下的栅格原点与分辨率
u16 tile ; u16 n_tiles ; n_tiles × (u16 tx, u16 ty)
payload = codec( concat(tile_bytes) )                          # snapshot 时 n_tiles=0，payload 为整图
```

**3.12.3 前端着色**（Three.js，WebGL2 与 WebGPU/TSL 两套等价实现）

```glsl
uniform sampler2D uCovOwner;          // R8，NearestFilter，按 dirty 瓦片子区域更新（整图较小时直接整体重传）
uniform vec4  uCovXf;                 // (x0, y0, 1/(W·res), 1/(H·res))
uniform vec3  uPalette[16];           // 分区配色（数据可视化用的分类色；品牌红只用于选中/告警）
uniform float uGhostAlpha;            // 0.18
vec2 uv = (worldPos.xy - uCovXf.xy) * uCovXf.zw;
float owner = texture(uCovOwner, uv).r * 255.0;
if (owner < 0.5) {                    // 未扫：暗灰、小点、低不透明度（或直接 discard 一部分）
    vColor = uGhostGray; vAlpha = uGhostAlpha; gl_PointSize *= 0.6;
} else {                              // 已扫：高度色带与机号色混合，可叠加“新扫描辉光”
    vColor = mix(heightRamp(worldPos.z), uPalette[int(owner-1.0) % 16], 0.35);
}
```

航线层（MissionLayer）：每机分区航线是一条 BufferGeometry 折线，带逐顶点属性 `aArrival`（计划到达时刻）。着色器用 `uTNow` 区分已飞（实线、亮）和未飞（虚线、暗），**每帧 CPU 零开销**。

**3.12.4 覆盖驱动的点云 LOD（与 r12 的点预算联动）**

```text
对每个八叉树节点，用 owner>0 的二值栅格的积分图（SAT）求节点 XY 包围盒的已扫比例 f，O(1)
refine 阈值：sse_node ≥ τ · (f > 0 ? 1 : k_ghost)，k_ghost = 2.5（未扫区域少细分）
点预算优先级：priority = sse · (0.4 + 0.6·f)
效果：任务初期整城“稀疏幽灵点云”，渲染负载低；无人机扫过的区域逐步加密，
     和“渐进加载、疏密自动调节”的需求天然契合。FPS 反馈仍由 r12 的全局点预算控制器负责
```

**3.12.5 升级路径**：V0.5 起，每机的“真实”扫描（r04/r05 的 MID-360 模式 raycast mock，或真机 FAST-LIO 点云）以点块流的形式推送，按机号着色后融合进 World。覆盖栅格保留，作为质量和进度指标。

### 3.13 实测汇总

| 项 | 数值 | 来源 |
|---|---|---|
| Hungarian | n=50：10.4 ms；n=200：89 ms（numpy） | `r26_formation_out.json` |
| 律 A 脱连阈值（仓库配置） | R=20 m、5 m/s 时 79% 脱连 | `r26_formation2_out.json` |
| 律 B 跟踪误差 | 0.01–0.09 m（3 机）；0.06 m（V7，R=40） | 同上 |
| 航向滤波收益 | r=15 U 形弯 RMS 4.87 → 0.065 m | 同上 |
| CAPT（虚拟锚点） | 20 组变形的最小间距 ≥ 4.24 m | 同上 |
| DSM 构建 | 500 万点 0.9 s（2 m） | `r26_coverage_out.json` |
| 覆盖规划 | fly_over 95–120 ms；楼下 230–480 ms | 同上 |
| 覆盖栅格推送 | SF 8 机约 1.1 kB/s | 同上 |

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 能力 | 来源 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| Offboard 生命周期（预热 → set mode → arm → 保活 → 失联 HOLD） | PSC `Arming`、PASR `Control` | `sim_gateway/adapters/mock_adapter.py`（语义），`mavsdk_adapter.py` | V0.1 Mock / V0.2 SITL | port（语义） | Mock 与 PX4 行为一致，UI 和任务层零改动 |
| NaN 型 setpoint 语义（p/v/a 分阶受控） | PSC `compute_command` | `DroneCommand.SETPOINT` schema（r21） | V0.2 | port | 与 PX4 TrajectorySetpoint 一一对应 |
| 邻接图 + PrC（BFS）权重 + 脱连检测 | PSC `WeightedTopologyNeighbors` | `swarm/topology.py`；WS `swarm.topology` | V0.6（MVP 演示可以提前） | port（修 B3/B4/B5/B8） | 拓扑可视化与一致性项 |
| 律 A（weighted-topology 一致性） | PSC | `swarm/formation/laws.py::wt_consensus` | V0.6（对照）/ V1.0（去中心化研究） | port（修 B1/B2） | 作为基线和教学模式 |
| 律 B/C 虚拟结构 FormationTracker | 本单元 | `sim_gateway/core/formation_tracker.py` | MVP 演示 → V0.6 | new | 主律 |
| 槽位生成器 | 本单元（参考 r19 §3.9） | `swarm/formation/slots.py` + `web/src/features/mission/formation/slots.ts` | MVP | new | 前后端一致，前端即时预览 |
| CAPT 分配 / 集结 / 变形 | 本单元 | `swarm/formation/assign.py` | MVP 演示 → V0.6 | new | 无碰保证 |
| 覆盖规划器（DSM、航带、BCD、切分、分配） | PASR 思路 + 本单元 | `swarm/mission/coverage/{dsm,lanes,bcd,partition,plan}.py` | MVP 演示 → V0.6 | new | Mission 模块的 AREA_COVERAGE |
| barrier / free / timed 同步 | PASR `publish_default_setpoints` | `swarm/mission/executor.py` | V0.6 | port（加超时） | 同步演示 |
| 分组高度分层 | PASR `config.yaml` | `swarm/mission/deconflict.py` | V0.6 | port（自动化） | 转场去冲突 |
| 覆盖栅格 + 扫描揭示 + 覆盖驱动 LOD | 本单元 | `sim_gateway/coverage.py`；`web/src/world/pointcloud/coverageMaterial.ts` | **MVP** | new | 视觉亮点 + 降负载 |
| JSON 驱动的控制器/邻域可执行选择 | PSC `launch_simulation.py` | Mission `controller` 策略字段 | V0.6 | reference | 可插拔控制律 |
| `sitl_multiple_run.sh -p` 位姿表、端口规则 | PSC | `simulation/px4/`（如需 Gazebo） | V0.2 | reference | 优先用 SIH 无头多机（r20、r21）；Gazebo Classic 已 EOL |
| `NearestNeighbors<T>` 三钩子 | PSC | `swarm/topology.py` 的策略接口 | V0.6 | reference | 可扩展邻域策略（k-NN / 视距 / 通信图） |
| L2 话题契约测试 + Codecov | PASR | `tests/contract/`（用 fake_px4 / Mock） | V0.2 | reference | 无 PX4 也能跑 CI |
| `grid_plan.world`、Gazebo 相机插件 | PASR | — | — | skip | 用 UrbanScene3D；PASR 没有传感器 |
| ROS2 节点、px4_msgs vendoring | 两者 | — | — | skip | 主链路走 MAVSDK v4（r21）；ROS2 只在对接 ROS 栈时用 |

### 4.2 模块布局与接口（对应 01-design §42）

```text
swarm/
  topology.py                 # topology(p, leaders, r_nb, stale) -> (adj, W, prc, detached)
  formation/
    slots.py                  # formation_slots(shape, n, s, ...) -> (n,3)；feasibility(spec, path) -> Report
    assign.py                 # hungarian(C)；capt_assign(P, G)；plan_reshape(...)
    laws.py                   # vs_ff（律B）、vs_ff_consensus（律C）、wt_consensus（律A，修正版）
  mission/
    spec.py                   # pydantic：FormationMission、CoverageMission、MissionPlan、MissionProgress
    coverage/
      dsm.py                  # build_dsm(world_pkg, res) -> DSM（缓存到 World Package derived/）
      lanes.py                # sensor_geometry、lanes_for_angle、sweep_angle_search
      bcd.py                  # clip(lanes, nofly)、bcd_cells、order_cells
      partition.py            # split_contiguous（含段内精确切分）、assign_chunks、replan_on_failure
      plan.py                 # plan_coverage(spec, world) -> MissionPlan（< 1 s）
    executor.py               # MissionExecutor：sync_policy、acceptance、transit layers、事件
    deconflict.py             # transit_layers、与 r24 Safety 的接口
sim_gateway/core/
  formation_tracker.py        # 20 Hz，输出每机 Setpoint(p,v,a,yaw,yawspeed)
  coverage.py                 # CoverageGrid：stamp(drones, sensors)、dirty_tiles()、snapshot()
web/src/features/mission/
  formation/slots.ts          # 与 slots.py 共用 golden JSON 测试
  coverage/preview.ts         # 画 AOI、航带、分区（只做可视化，调用 REST /missions:plan）
web/src/world/pointcloud/coverageMaterial.ts   # §3.12.3 着色，TSL + GLSL 双实现
```

### 4.3 数据契约（节选）

```ts
type FormationShape = "line" | "column" | "V" | "echelon" | "grid" | "circle";

interface FormationSpec {
  shape: FormationShape; spacing_m: number;             // ≥ 2√2·R_safe
  half_angle_deg?: number; cols?: number; radius_m?: number; dz_per_rank_m?: number;
  heading_mode: "aligned" | "filtered" | "world"; tau_psi_s?: number; world_heading_deg?: number;
  anchor: "virtual" | "leader"; ui_leader_id?: string;
  controller: "vs_ff" | "vs_ff_consensus" | "wt_consensus";
}

interface FormationMission {
  type: "FORMATION"; id: string; members: string[]; spec: FormationSpec;
  path: { waypoints: [number, number, number][]; speed_mps: number; corner_radius_m: number };
  assembly: { altitude_m: number; method: "capt" }; on_member_loss: "keep_slot" | "compact";
}

interface CoverageMission {
  type: "AREA_COVERAGE"; id: string; members: string[];
  aoi: { frame: "world"; polygon: [number, number][]; holes?: [number, number][][] };
  sensor: { kind: "camera"; hfov_deg: number; vfov_deg: number; side_overlap: number; front_overlap: number }
        | { kind: "lidar"; swath_m: number };
  altitude: { mode: "fly_over" | "fixed_agl" | "per_lane"; h_agl_m: number; clearance_m: number; safety_radius_m: number };
  speed_mps: number; sweep_angle_deg: number | "auto";
  partition: "contiguous_balanced" | "group_sweep"; weights?: Record<string, number>;   // V1.0 ANet 能力加权
  sync: "free" | "barrier" | "timed"; transit_layer_step_m: number; rtl: boolean;
}

interface MissionPlan {                                  // POST /missions:plan 的返回，UI 预览与执行共用
  mission_id: string; theta_deg: number; spacing_m: number; swath_m: number; trigger_m: number;
  per_drone: { id: string; route: { p: [number, number, number]; t_s: number; kind: "transit" | "lane" | "turn" }[];
               length_m: number; eta_s: number; photos: number; color_idx: number }[];
  stats: { coverage_pred: number; mean_overlap: number; makespan_s: number; balance: number; plan_ms: number };
}
```

WebSocket 新增通道：
- `mission.progress`：2 Hz，`{mission_id, per_drone:[{id, seg_idx, s_m, eta_s, done_frac}], covered_frac}`；
- `swarm.topology`：2 Hz，`{edges:[[i,j,w]], prc:[…], detached:[…]}`；
- `coverage.tiles`：二进制，§3.12.2；
- `formation.state`：5 Hz，`{psi_f, w_f, err_rms_m, err_max_m, feasible}`。

`DroneState` 增加 `mission: {id, role: "leader"|"member", slot?, chunk?, progress, eta_s}` 和 `formation?: {err_m, detached}`。

### 4.4 MVP mock 演示剧本与验收指标

剧本（San Francisco，归零后的 ENU；6–8 架 mock P600）：
1. **集结**：停机坪 2×4（间距 5 m）→ 起飞 → CAPT 集结成 V 字，高度 70 m。morphicons 的形状图标与 3D 变形同步。
2. **编队巡航**：沿 3 个航点的环线（转角半径 40 m，6 m/s），航向滤波 τψ=2 s。拓扑叠加层显示边权。
3. **变形**：V → line → circle（每次 CAPT，T ≈ 6 s）。
4. **覆盖**：用户在地图上画 AOI → `POST /missions:plan`（< 0.5 s）→ 预览分区（色带）与每机 ETA 表 → 执行。点云“扫描揭示”由暗到亮逐步融合，未扫区域点稀、已扫区域点密。
5. **故障**：随机让一架“失效” → 1 s 内重规划，其余机分担，UI 用 Sonner 弹出事件。

验收指标：

| 指标 | 目标 |
|---|---|
| 编队跟踪 RMS（6 m/s，转角半径 40 m，filtered） | < 0.5 m（Mock） |
| 集结/变形最小机间距 | ≥ 3 m |
| 覆盖规划耗时（≤ 1 km²、≤ 8 机） | < 1 s |
| 覆盖率（fly_over 预测值） | ≥ 99% |
| 分区均衡度 | ≤ 1.15（实现段内切分后） |
| 覆盖栅格推送 | ≤ 5 kB/s |
| 主线程帧时间增量 | < 0.5 ms |

### 4.5 UI 映射（遵循 shadcn 全量、morphicons 图标、lieflat-charts 表格/图表、transitions.dev 动效）

| 功能 | 组件 | 说明 |
|---|---|---|
| 任务面板 | `Sheet`（右侧）+ `Tabs`（编队 / 覆盖） | 面板切换用 transitions.dev 的滑入与交叉淡化 |
| 队形选择 | `ToggleGroup` + morphicons 形状图标 | 切换形状时图标 path-morph。若 morphicons 没有现成的 line/V/grid/circle，按其同拓扑 SVG 规范补 4 个 |
| 间距 / 速度 / 重叠率 / 高度 | `Slider` + `Tooltip` | 不可行时 Slider 轨道变红（品牌红只用于告警），并附 `Alert` 说明原因（§3.5） |
| 航向模式、分区策略、同步策略 | `Select` / `RadioGroup` / `Switch` | |
| AOI 绘制 | 地图工具条 `Toggle` + 3D 场景内多边形编辑 | 顶点拖拽、闭合吸附 |
| 规划结果 | `Card` + lieflat 风格 `Table`（机号 / 分区色 / 路径 km / ETA / 照片数）+ lieflat 条形图（每机 ETA，显示均衡度） | |
| 执行进度 | `Progress`（每机）+ 覆盖率大数字 + 迷你覆盖图（2D canvas 读同一栅格） | |
| 执行确认、中止 | `AlertDialog` | 与 r24 的 HOLD/ELAND 升级键一致 |
| 事件 | `Sonner` | detached、重规划、barrier 超时 |

### 4.6 版本落点

| 版本 | 内容 |
|---|---|
| **V0.1（MVP mock）** | CoverageGrid 与扫描揭示着色；覆盖驱动 LOD；槽位生成器与前端预览；mock 多机演示剧本（Formation 律 B + CAPT，Coverage fly_over） |
| V0.2 | Offboard 生命周期语义对齐 PX4 SITL/SIH；FormationTracker 输出接 MAVSDK 的 p/v/a setpoint |
| V0.4 | 风场进入 Mock → 编队误差与覆盖漂移可视化（律 C 的一致性在阵风下的收益） |
| V0.5 | 真实扫描点块融合（按机号着色），覆盖栅格改为由真实点云命中更新 |
| **V0.6** | 正式 Mission 模块：楼下/逐带高度模式、BCD、故障重规划、barrier/timed 同步、转场分层、拓扑与律 A 对照模式、与 r24 避碰集成 |
| V1.0 | ANet 能力加权切分、拍卖式任务重分配、异构传感器（热成像复核 → 插入“验证航段”）、多 sortie 换电 |

---

## 5. 对比与推荐

| 维度 | PX4_Swarm_Controller | PX4-Aerial-Swarm-Reconstruction |
|---|---|---|
| Stars / 最后提交 / 2026 活跃 | ★97 / 2024-03-02 / 无 | ★16 / 2024-12-11 / 无 |
| 代码规模 | 约 1.4k 行 C++（模板化、有注释）+ 0.2k 行 Python | 约 0.9k 行 C++（半数为测试）+ 0.1k 行 Python |
| 核心算法 | 分布式领航-跟随一致性 + PrC 权重（有论文出处） | 无算法：手写航点表 + barrier + 高度分层 |
| 架构价值 | 邻域/控制器模板分离、JSON 可插拔、每机命名空间 | 集中式单节点多机、测试与 CI 模板 |
| 缺陷 | 多处实质性缺陷（B1–B8），中速脱连 | 相对路径加载配置、N² 次发布、无降落、点云缺失 |
| 构建难度 | 高：ROS2 Humble + PX4 源码编译 + XRCE Agent + Gazebo Classic + 手动移动 custom_msgs + 覆盖 PX4 脚本 | 同左，另需 catch_ros2、gazebo_ros；px4_msgs 必须与固件版本匹配 |
| 与本项目契合 | 中：算法可移植，节点不可用 | 中偏低：只提供任务剧本和工程流程思路 |
| 推荐 | **第 1**：port 拓扑与一致性（修正版），作为 V0.6 对照律 | **第 2**：port barrier/分层概念；“多机扫城”剧本用于 MVP 演示 |

与更大范围的参考对照（仅定位，不展开）：
- 需要**避障轨迹规划**的多机任务（楼下、狭窄街道），用 `refs/swarm/ego-planner-swarm`（r25 单元）。
- **多机避碰与安全层**用 r24 MRS 的 MpcTracker 避碰规则。
- **编队 separation 语义**与 r19 Prometheus 的 `getFormationSeparation` 同源，本文的槽位生成器是它的超集（增加 V/echelon/grid/circle、虚拟锚点和可行性检查）。

---

## 6. 风险与注意事项

| # | 风险 | 说明 | 对策 |
|---|---|---|---|
| R1 | 平台依赖过时 | 两仓库都绑定 ROS2 Humble + Gazebo Classic（2025-01 EOL），依赖 gnome-terminal，本机无 ROS、无 GPU | 不引入依赖；V0.2 多机用 PX4 SIH 无头（r20 实测、r21 接口），或 gz Harmonic 的 gzserver-only |
| R2 | px4_msgs 与固件版本强耦合 | PASR vendored 的 227 条消息必须与 PX4 固件的 uORB 定义完全一致，否则 uXRCE-DDS 静默不通 | 主链路走 MAVLink/MAVSDK v4（r21），不走 DDS |
| R3 | PX4 DDS 话题 QoS | `/fmu/out/*` 是 best-effort，默认 reliable 订阅收不到（两仓库都显式用了 sensor_data 或 best_effort） | 若将来做 Px4DdsAdapter，要写进契约测试 |
| R4 | 多实例命名空间 | 实例 0 无 `px4_` 前缀；两仓库从 1 开始编号来规避。自定义话题被放进 `/px4_i/fmu/out/`，污染了 PX4 命名空间 | 我们的机号与 PX4 实例号解耦，由 Adapter 维护映射 |
| R5 | 律 A 的脱连与模式抖动 | §3.3、B5–B8 | 生产用律 B；律 A 仅作对照，必须带连通性检查 |
| R6 | aligned 队形急弯不可行 | §3.4 实测 | 默认 filtered；下发前做可行性检查（§3.5） |
| R7 | 固定领航机槽位导致变形碰撞 | §3.6 实测 0.02 m | anchor 默认 virtual |
| R8 | 2.5D DSM 的局限 | 无法表达桥下、挑檐、树冠下空间；楼下模式会把这些当成禁飞 | V0.6 起用 World 的 voxel/SDF（01-design §7）做 3D 走廊检查；DSM 只用于快速规划 |
| R9 | UrbanScene3D 尺度/轴向 | Chicago 单位疑似 km，Suzhou 轴向存疑，SF 地面在 z=−26.3（r10） | 规划前必须用归一化后的 World，DSM 缓存带 `import_transform` 哈希 |
| R10 | 覆盖“预测”不等于真实成像 | 天底足迹模型忽略遮挡、立面和 GSD 变化 | 标注为“预测覆盖率”；V0.5 起用真实扫描命中统计 |
| R11 | BCD-lite 在城市网格过度分胞 | SF 楼下模式 138 胞、227 段，转场多 | 合并小胞（面积 < W² 的胞并入相邻胞）；或改用栅格 DARP / 螺旋填充（V1.0 研究） |
| R12 | O(n²) 计算 | 律 C 在 n=200 时每步 13 ms | 拓扑 10 Hz + 网格哈希；物理与控制分频 |
| R13 | 墙钟定时器与仿真时间 | 两仓库都用 `create_wall_timer`，未启用 use_sim_time；快放/回放时控制周期与仿真不一致 | 我们所有 Tracker 以 `t_sim` 驱动（r15 R8） |
| R14 | 浮点时间戳 | B2 的教训 | 规范：时间一律 int64 ns 或 float64 s，**禁止 float32 存绝对时间**；GPU 侧只存相对时间 |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§30 控制模式分层。** 目前把 Takeoff/GoTo 与 Area Coverage/Formation 并列，建议拆成三层：
   - **Command**（单机原子指令，r21）；
   - **Behavior/Tracker**（PathTracker、FormationTracker、Orbit，20 Hz 连续 setpoint）；
   - **Mission**（Planner + Executor：FORMATION、AREA_COVERAGE、SEARCH，一次性规划 + 事件驱动执行）。

   Formation 和 Coverage 各自要有 Spec、Plan、Progress 三个数据契约（§4.3），并显式带上 `sync_policy` 和 `deconfliction` 策略字段。
2. **§29 “每架独立 PX4 SITL + 独立 Controller”只适合 V0.2 之后的高保真模式。** MVP 和大规模演示应该用**集中式、向量化**的 Mock 与 FormationTracker（r19/r24 实测 1000 机 100 Hz 可行）。“每机一个控制器进程”是 ROS 节点思维，两个仓库的问题（B5 的全体门控、B6 的定时器相位）正是分布式节点间时序不一致导致的。**“分布式”应该放到 ANet 的任务协商层，而不是控制环。**
3. **§49 V0.6 的 Formation 要写明算法选型和验收指标**：
   - 虚拟锚点 + 前馈 + 航向滤波（主律），weighted-topology 一致性（对照律）；
   - CAPT 集结与变形；
   - 可行性检查；
   - 指标见 §4.4：RMS < 0.5 m、最小间距 ≥ 3 m、规划 < 1 s。
4. **§43 MVP 链路加一段“多机扫城 mock + 扫描揭示”。** 它同时展示 World（点云）和 Agent（多机），成本很低（§3.12 实测每 tick 不到 1 ms、约 1 kB/s），而且和“点云疏密自动调节”互相增益。建议把 MVP 最终 Demo 改为：“导入城市点云 → 进入场景 → 画区域 → 多机自动分区扫描 → 点云随扫描逐步融合加密”。
5. **§14 点云 LOD 增加“任务/覆盖驱动的密度偏置”**（§3.12.4）。LOD 的输入除相机距离和屏幕尺寸外，再加兴趣度（覆盖状态、选中无人机周围、任务 AOI）。
6. **§7 World Model 的 Geometry 增加 2.5D DSM/DTM 层**，放在点云与 Voxel/SDF 之间，作为规划的快速代理：SF 500 万点构建 0.9 s，查询 O(1)。§41 World Package 增加：
   - `derived/dsm_2m.bin`、`derived/dtm_2m.bin`、`derived/nofly_<alt>.bin`（带源数据哈希）；
   - `missions/`：任务 Spec、Plan 与执行日志，用于回放；
   - `semantic/restricted_areas.geojson`：禁飞多边形。
7. **§28 DroneState 增加任务字段**：`mission{id, role, slot, chunk, progress, eta}`、`formation{err_m, detached}`。§36 WebSocket 增加 `mission.progress`、`swarm.topology`、`coverage.tiles`、`formation.state` 四个通道（§4.3），**Mission/Plan 走 REST，进度走 WS**。
8. **§37 频率表补充 Tracker 层**：FormationTracker 和 PathTracker 20 Hz 足够（实测 5–50 Hz 下误差仅 0.062–0.067 m），拓扑 10 Hz，覆盖栅格 5 Hz 打点、1–2 Hz 推送。这样“物理 100 Hz、控制 20 Hz、推送 10–20 Hz”三档清晰分离。
9. **坐标约定写进架构说明书的硬规则**（r15 R7 的补充）：
   - World ENU 是唯一规划坐标；
   - PX4 的局部 NED 原点因机而异（SITL 为 spawn 点），Adapter 负责 `T_world←local_i`；
   - 禁止配置文件混用 ENU x/y 与 NED z（PSC 的教训）。
10. **§35 Simulation Backend 更新**：Gazebo Classic 已于 2025-01 EOL，“PX4 + Gazebo”应明确为 gz（Harmonic）或 **SIH（无 GPU 服务器首选）**。多机 SITL 的端口与 sysid 规则（4560+i、14560+i、sysid=i+1、上限 255）写进部署文档。
11. **§32 多智能体工作流与覆盖结合**：“Drone A 发现疑似目标 → 请求热成像复核”在覆盖任务里表现为向 Drone B 的剩余航段序列**插入一个验证航段**，然后对剩余序列重新切分（§3.9 的重规划）。建议在 V1.0 任务模型里把“插入航段 / 重切分”定义为 ANet 任务协商的标准动作。
12. **§42 Repo 结构**：`swarm/` 细化为 `topology/`、`formation/`、`mission/coverage/`、`mission/executor`；前后端共享的纯算法（槽位生成、航带预览）用 **golden JSON 测试**保证 Python 与 TypeScript 两份实现一致，并在 CI 中跑。

---

*附：原型脚本均可用 `/data/projs/anet-drone/.venv/bin/python .cache/research/r26/<script>.py` 复现；coverage 脚本读取 `data/raw/urbanscene3d/San Francisco_sampled_5m.ply`。*
