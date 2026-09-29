# R19 研究笔记：amov-lab/Prometheus（P600 软件栈）
## 控制话题、控制状态机、地面站协议，以及 Simulation Gateway 对接方案

> 研究单元：r19 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §2、§26–§30、§33、§35–§37、§43–§49
>
> 仓库快照：`refs/sim/Prometheus` @ `5dcd8cf`（2025-11-21，提交信息 "communication simulation timestamp modification"，★3265，shallow clone 只有 1 个提交，共 4807 个文件）。
> 技术栈：ROS1 **Noetic**（catkin + roscpp）+ PX4（Amov fork `prometheus_px4`，目标 `amovlab_sitl_default`）+ mavros（Amov fork `prometheus_mavros`）+ Gazebo Classic。
> 4 个 gitee 子模块（`swarm_control`、`swarm_formation`、`searching_pkg`、`matlab_bridge`）**clone 下来是空目录**。
>
> 本机实测产物在 `.cache/research/r19/`，都没有改动 `refs/`：
> - `enc.cpp`：链接官方 `libcommunication_x86_64.so`，dump 编码后的帧。
> - `send.cpp` + `listen.py`：探测实际的收发端口。
> - `j.cpp` / `j2.cpp`：dump 各结构体的 JSON 形状。
> - `codec.py`：纯 Python 编解码器，已与 .so **逐字节一致**。
> - `dec.cpp`：用官方 `decodeMsg` 反向校验 Python 编出的帧。
> - `mock_uav.py`：Mock 动力学原型和规模基准。
> - `hb.cpp`：验证心跳 TCP 每失败一次 `disconnect_num` 加 1（实测 1→2→3）。
>
> 与其他单元的分工：PX4 SITL、MAVSDK、mavros 本体、上游 ZJU ego-planner-swarm、XTDrone 由 sim/swarm 组的其他单元负责。本文只讲 **Prometheus 这一层**，包括它的数据契约、控制语义、地面站协议，以及 P600 真机和 Prometheus SITL 怎么接进我们的 World Runtime。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **amov-lab/Prometheus（整体）** | P600 机载软件栈：状态估计、控制、规划、地面站通信 | **reference + port**。不在我们这边运行 ROS1，只移植契约、语义和协议 | V0.1–V0.6 | ★★★★☆ |
| `communication`：地面站协议，以及闭源的 `libcommunication_{x86_64,aarch64}.so` | 机载端与地面站之间的 TCP/UDP 通道，载荷是 JSON | **port**。Python 纯实现编解码，约 60 行，已逐字节验证，作为 Gateway 的 `PrometheusGSBackend` | V0.2（SITL）/ V0.5（真机） | ★★★★★ |
| `common/prometheus_msgs`：UAVState / UAVCommand / UAVControlState / UAVSetup / SwarmCommand / TextInfo | 控制与状态的数据契约 | **port**。字段语义映射到我们的 `DroneState` / `DroneCommand`（§4.2、§4.3） | V0.1 定义，V0.2 实现 | ★★★★★ |
| `uav_control`：`UAV_controller` 状态机、failsafe、`set_command_des`、PID/UDE/NE | 控制权状态机，以及"指令 → setpoint"的语义 | **port**。Mock 后端复刻同一套状态机和指令语义，这样切换后端时 UI 行为不变 | V0.1（Mock）/ V0.2 | ★★★★★ |
| `simulator_utils`：`Fake_UAV`、22 维四旋翼 ODE、map_generator | 不依赖 PX4 的运动学/动力学替身 | **port**。fake_uav 的级联控制用作 MVP Mock；刚体 ODE 用于 V0.4 的风扰 | V0.1 / V0.4 | ★★★★☆ |
| `tutorial_demo` + `uav_control/include/controller_test.h` | 编队 separation 矩阵；圆、8 字、阶跃、直线测试轨迹 | **port** | V0.2 / V0.6 | ★★★☆☆ |
| `ego_planner_swarm`（Amov fork） | 多机 B 样条局部规划，加上接入 Prometheus 的 traj_server | **reference**（上游版本另有单元），另外 **port** de Boor 求值用于前端画轨迹 | V0.6 | ★★★☆☆ |
| `motion_planning`：A*、APF、VFH、min-snap | 经典的全局和局部规划 | **port**。APF 和 A* 给 Mock 做"带避障的 GoTo" | V0.6 | ★★★☆☆ |
| `Simulator/gazebo_simulator`：`p600.sdf`、多机 SITL launch | P600 仿真模型和端口分配规则 | **reference**。参数只作 Digital Twin 的初值，端口公式写进 Gateway | V0.2 | ★★★☆☆ |
| `FAST_LIO`（fork）/ `uav_control_fmt` / `future_aircraft` | MID-360 的 LIO 配置、FMT 飞控测试、竞赛 demo | reference / skip | V0.5 / — | ★★☆☆☆ |
| `swarm_control` / `swarm_formation` / `searching_pkg` / `matlab_bridge` | 集群、编队、搜索（gitee 子模块） | **skip**。clone 为空，只能从 `SwarmCommand` 反推语义 | — | ☆ |

**关键结论（实现者先读这几条）**

1. **P600 现役软件栈是 ROS1 Noetic，不是 ROS2。**
   - 全仓 `package.xml` 都是 `catkin` + `roscpp`，脚本里写的是 `source /opt/ros/noetic/setup.bash`。
   - 01-design §33 写的 "Message: ROS2 / DDS" 与 P600 实际不符。
   - 好消息是**地面站协议与 ROS 无关**。Gateway 可以用纯 Python asyncio 直接和真机 P600 或 Prometheus SITL 通信，我们这边完全不需要装 ROS（§4.5）。
2. **地面站协议已完全还原并验证（§2.3、§3.1）。**
   - 帧格式：`"am"`（0x61 0x6D）+ `u32 LE payload_len` + `u8 msg_id` + `u8 robot_id` + JSON（nlohmann `dump()`，key 按字母序）+ `u16 LE CRC-16/ARC`（覆盖帧头和载荷），固定开销 10 B。
   - 端口（数据流动方向）：
     - 地面站到机载端的指令：发到**机载端 TCP 55555**，每条指令**单独建一次 TCP 连接**。
     - 机载端到地面站的状态：发到**地面站 UDP 8889**。
     - 机载端到地面站的心跳：发到**地面站 TCP 55556**，1 Hz。
   - 已在本机用官方 .so 双向对拍通过。
3. **Gateway 是安全关键组件。** 机载端每秒通过 TCP 往地面站 55556 发一次心跳，**连续 5 次（`try_connect_num`）发送失败，就自动给无人机下 Land**（集群模式下是通知集群模块）。所以 Gateway 必须常驻监听 55556；Gateway 一旦崩溃，真机就会降落，这是期望行为，但必须写进产品文档。
4. **控制语义比 01-design §30 更完整，建议直接采用为底层"控制权层"。**
   - 4 状态控制权 FSM：`INIT → RC_POS_CONTROL → COMMAND_CONTROL → LAND_CONTROL`。
   - `UAVCommand` 由 `Agent_CMD × Move_mode × Control_Level × Command_ID` 组合而成，还有 geofence 和 odom failsafe。
   - 我们的 `FlightMode`（任务级）叠在它上面（§3.3、§4.4）。
5. **Mock 动力学直接移植 `Fake_UAV`，再补上 PX4 级联增益和一阶姿态滞后（§3.5）。** 实测结果：
   - 10 m 定点：4.25 s 进入 0.2 m 误差带，超调 0.30 m。
   - 8 m/s 侧风：稳态误差 1.6 cm（加速度域积分）。
   - numpy SoA 单核：N=1000 时 2.2 ms/step，**1000 架也能跑 100 Hz 物理**。
6. **遥测体积（§3.13）。**
   - Prometheus 的 UAVState JSON 每条 456 B；我们的二进制 DroneState 每架 **40 B**。
   - 100 架 × 30 Hz：Prometheus JSON 约 1.33 MiB/s，二进制约 **118 KiB/s**。
   - Web 端默认用二进制帧，调试时可切换成 JSON。
7. **字段层面的坑（§6）。**
   - `latitude/longitude` 在 ROS msg 和线上结构体里都是 **float32**：经度量化 0.72 m、纬度 0.21 m，完全不够 RTK 用。
   - 字段名 `battery_percetage` 是拼写错误。
   - 线上 `attitude_q` 是 `[x,y,z,w]` 数组。
   - **`UAVControlState.control_state` 枚举在 ROS msg（0..3）和线上 Struct（0..4）里不一致。**
   - PX4_ORIGIN 模式下的 TRAJECTORY 指令会**丢掉加速度前馈**。
   - 位置坐标是"1 号机起飞点 ENU"，不是 World 坐标。
8. **EGO-Swarm（Amov fork）把 `/uav1/...` 话题写死了**（`ego_replan_fsm.cpp`、`traj_server_for_prometheus.cpp`），多机时要靠 namespace 重映射。它的 grid_map 是整块预分配，每个体素 15 B：100×100×30 m、0.15 m 分辨率就要 **1.24 GiB**。所以在 UrbanScene3D 这种城市尺度下，只能按任务区域裁剪地图后再喂给它（§6）。
9. **安全问题。** 协议里的 `ModeSelection.mode=CUSTOMMODE` 会把 `cmd` 字符串直接交给 `system()` 执行，而且**没有任何认证**；桥接节点还会把"第一个连上来的地面站"认定为控制方。真机网络必须隔离，Web UI **永远不能**透传这类指令（§6）。
10. **`p600.sdf` 的参数是 Iris 级别的占位值**：质量 1.505 kg，臂长 0.30 m，`motorConstant` 8.54858e-6，`maxRotVelocity` 1500 rad/s，算下来推重比 5.2，悬停转速 657 rad/s。只能作为 Digital Twin 的初值，V0.4 之前要用真机日志做辨识（§3.7）。

---

## 1. 仓库概览

| 项 | 值 |
|---|---|
| 定位 | "自主无人机开源项目"。机载电脑（Jetson NX / Orin NX）上的 ROS 软件栈，负责定位融合、控制、规划、识别和地面站通信 |
| 活跃度 | 最后一次提交 2025-11-21；2026 年到目前没有提交。ProSim / 新版地面站都是闭源安装包 |
| 语言 / 构建 | C++14 为主（少量 Python），每个模块单独 `catkin_make --source Modules/<m> --build build/<m>`（见 `compile_all.sh`、`compile_control.sh` 等） |
| 依赖 | Eigen、GeographicLib、boost（odeint、thread）、mavros（Amov fork）、PX4（Amov fork）、Gazebo 11、OctoMap（EGO）、PCL（规划）、nlohmann/json（vendored）、**闭源 `libcommunication_*.so`** |
| 许可 | Apache-2.0，附加"仅限个人使用"条款（按本项目约定忽略） |

**目录结构（只列与本项目相关的部分）**

| 路径 | 内容 | 本文章节 |
|---|---|---|
| `Modules/common/prometheus_msgs` | 43 个 msg、3 个 srv、1 个 action | §2.1 |
| `Modules/common/include` | `math_utils.h`、`geometry_utils.h`（四元数/欧拉、rotz 等） | — |
| `Modules/uav_control` | `uav_control_node.cpp`，内含 `UAV_estimator` 和 `UAV_controller`；另有 `pos_controller_{PID,UDE,NE}.h`、`rc_input.h`、`controller_test.h` | §2.2 |
| `Modules/communication` | `communication_bridge.cpp`、`uav_basic_topic.cpp`、`swarm_control_topic.cpp`；`shard/include/{communication.hpp,Struct.hpp,jsonconverter.h,CRC.hpp}`；`shard/libs/*.so` | §2.3 |
| `Modules/ego_planner_swarm` | EGO-Swarm fork，加了 `src_for_prometheus/traj_server_for_prometheus.cpp`、`launch_for_prometheus/*` | §2.4 |
| `Modules/motion_planning` | `global_planner`（A*）、`local_planner`（APF/VFH）、`min_snap_trajectory` | §2.5 |
| `Modules/simulator_utils` | `fake_odom/fake_uav.cpp`、`quadrotor_dynamics`、`map_generator` | §2.6 |
| `Modules/tutorial_demo` | 起降、ENU/机体系/经纬度控制、航点、圆轨迹、编队、YOLO/ArUco 跟踪 | §2.7 |
| `Simulator/gazebo_simulator` | 各机型 SDF（p230/p450/p600*）、多机 SITL launch、world | §2.8 |

**运行时数据流（单机，PX4_ORIGIN 控制器）**

```text
 PX4 (EKF2, MC pos/att ctrl) <--MAVLink--> mavros (/uavN/mavros/*)
      ^ vision_pose 50Hz (外部定位时)          | state / local_position / imu / battery / global_position
      |                                        v
      |                             UAV_estimator ──50Hz──> /uavN/prometheus/state (UAVState)
      |                                        |                    |
 setpoint_raw/{local,global,attitude}   UAV_controller  <── /uavN/prometheus/command (UAVCommand)
      ^───────────────100Hz──────────────── mainloop     <── /uavN/prometheus/setup   (UAVSetup)
                                               |──> /uavN/prometheus/control_state (UAVControlState)
                                               |──> /uavN/prometheus/text_info      (TextInfo, 变化才发)
 communication_bridge  <── subscribe state/control_state/text_info ──>  UDP  → GS:8889（默认 10Hz）
                       ──> publish command/setup/param_settings    <──  TCP  ← GS→drone:55555
                       ──> Heartbeat 1Hz                           ──>  TCP  → GS:55556
```

---

## 2. 源码结构与关键模块

### 2.1 消息契约（`Modules/common/prometheus_msgs/msg`）

**UAVState.msg**：由 `UAV_estimator::timercb_pub_uav_state` 以 50 Hz 发布，话题 `/uavN/prometheus/state`。

| 字段 | 类型 | 单位 / 语义 | 来源（mavros 话题） |
|---|---|---|---|
| `header` | Header | ROS 时间 | — |
| `uav_id` | uint8 | 从 1 开始编号 | 参数 |
| `connected` / `armed` / `mode` | bool / bool / string | **FCU 链路**（不是地面站链路）/ 是否解锁 / PX4 模式字符串（"OFFBOARD"、"POSCTL"、"AUTO.LAND"、"AUTO.RTL"…） | `mavros/state` |
| `location_source` | uint8 | 0 MOCAP、1 T265、2 GAZEBO、3 FAKE_ODOM、4 GPS、5 RTK、6 UWB、7 VINS、8 OPTICAL_FLOW、9 viobot、10 MID360、11 BSA_SLAM、12 ProSim | 参数 |
| `odom_valid` | bool | 定位是否可信（见 §3.12） | `check_uav_odom()` |
| `gps_status` / `gps_num` | uint8 | MAVLink `GPS_FIX_TYPE`（0..8，6 = RTK_FIXED）/ 卫星数 | `gpsstatus/gps1/raw`、`global_position/raw/satellites` |
| `position[3]` | float32 | m，**ENU**，加了 `offset_pose`（多机统一到 1 号机坐标系时） | `local_position/pose` |
| `range` | float32 | m，定高雷达（**线上 JSON 里没有这个字段**） | `distance_sensor/hrlv_ez4_pub` |
| `latitude/longitude/altitude` | **float32** | 度 / 度 / m。mavros 的 NavSatFix 高度是椭球高 | `global_position/global` |
| `rel_alt` | float32 | 相对 home 的高度（**线上 JSON 里没有这个字段**） | `global_position/rel_alt` |
| `velocity[3]` | float32 | m/s，ENU | `local_position/velocity_local` |
| `attitude[3]` / `attitude_q` | float32 / Quaternion | rad，roll/pitch/yaw（**ENU 下 yaw：0 = 东，逆时针为正**）/ FLU→ENU | `imu/data` |
| `attitude_rate[3]` | float32 | rad/s，机体 FLU | `imu/data.angular_velocity` |
| `battery_state` / `battery_percetage` | float32 | V / 0–1（字段名拼写错误，**保持原样**） | `mavros/battery` |

**UAVCommand.msg**：话题 `/uavN/prometheus/command`，也就是 `COMMAND_CONTROL` 状态下的唯一指令入口。

| 字段 | 取值 |
|---|---|
| `Agent_CMD` | 1 `Init_Pos_Hover`（起飞点上方 `Takeoff_height` 悬停，**兼做起飞**），2 `Current_Pos_Hover`，3 `Land`，4 `Move`，5 `User_Mode` |
| `Control_Level` | 0 `DEFAULT_CONTROL`，1 `ABSOLUTE_CONTROL`（加锁，不再响应 DEFAULT 指令），2 `EXIT_ABSOLUTE_CONTROL` |
| `Move_mode` | 0 `XYZ_POS`，1 `XY_VEL_Z_POS`，2 `XYZ_VEL`，3 `XYZ_POS_BODY`，4 `XYZ_VEL_BODY`，5 `XY_VEL_Z_POS_BODY`，6 `TRAJECTORY`，7 `XYZ_ATT`，8 `LAT_LON_ALT` |
| 参考量 | `position_ref[3]`、`velocity_ref[3]`、`acceleration_ref[3]`、`yaw_ref`、`Yaw_Rate_Mode`、`yaw_rate_ref`、`att_ref[4]`（rpy + 油门 0–1）、`latitude/longitude/altitude`（**float64**） |
| `Command_ID` | uint32，要求单调递增。**BODY 类模式只在 ID 增大时执行一次**，否则会不断叠加位移 |

**其他相关消息**

| 消息 | 要点 |
|---|---|
| `UAVControlState` | `control_state`：0 INIT、1 RC_POS_CONTROL、2 COMMAND_CONTROL、3 LAND_CONTROL；`pos_controller`：0 PX4_ORIGIN、1 PID、2 UDE、3 NE；`failsafe` |
| `UAVSetup` | 用来"模拟遥控器"。`cmd`：0 ARMING（配合 `arming`）、1 SET_PX4_MODE（配合 `px4_mode`）、2 REBOOT_PX4、3 SET_CONTROL_MODE（配合 `control_state` 字符串） |
| `SwarmCommand` | 集群指令。`Swarm_CMD`：Ready/Init/Start/Hold/Stop/Formation/…/Follow/Search/Attack；另有 `leader_pos`、`leader_vel[2]`、`swarm_size`、`swarm_shape`（一字/三角/方形/圆）、搜索矩形、攻击点、`formation_poses[]`。**这就是 V1.0 任务类型的现成清单** |
| `TextInfo` | `MessageType`：INFO/WARN/ERROR/FATAL + 文本，用作事件流 |
| `OffsetPose` / `GPSData` | 多机坐标统一的偏移量和原点经纬度（§3.9） |
| `Bspline` / `MultiBsplines` | EGO 轨迹：`order`、`knots[]`、`pos_pts[]`、`yaw_pts`、`yaw_dt`、`traj_id`、`start_time` |
| `GimbalState` / `GimbalControl` / `DetectionInfo` / `MultiDetectionInfo` / `Target` | 吊舱和检测结果，对应 V1.0 的传感能力和 ANet 发布的发现事件 |
| `ParamSettings` / `CustomDataSegment` | 远程改 ROS 参数，以及自定义 KV 透传 |
| `UGVState` / `UGVCommand` | 无人车（Hold / Direct_Control_BODY/ENU / Point / Path），对应 V1.0 的异构 Agent |

### 2.2 `uav_control`：估计、控制状态机与 failsafe

**进程模型**（`src/uav_control_node.cpp`）：
- 同一个节点里跑 `UAV_estimator` 和 `UAV_controller`，主循环 `ros::Rate(100)`。
- 启动时 `while(!connected)` 每 4 s 检查一次，**不连上 PX4 就不继续**。

**`UAV_estimator`**（`src/uav_estimator.cpp`）：
- 按 `location_source` 选择外部定位输入：MOCAP 用 `/vrpn_client_node/uavN/pose`，MID360 用 **`/Odometry`（没有 namespace）**，GAZEBO 用 `/uavN/prometheus/ground_truth`，FAKE_ODOM 用 `/uavN/prometheus/fake_odom`，等等。
- 以 50 Hz 往 `mavros/vision_pose/pose` 发外部定位，喂给 EKF2。
- 以 50 Hz 发布 `UAVState`，以 20 Hz 发布 RViz 轨迹和 mesh。
- 定位超时阈值：MOCAP 0.35 s、GAZEBO 1.1 s、T265 0.3 s、MID360 1.0 s、UWB 0.1 s、GPS 1.0 s、VINS 0.35 s。
- `set_local_pose_offset_cb` 用 GeographicLib 把 GPS 原点换算为 ECEF 再转 ENU，得到多机偏移。

**`UAV_controller` 状态机**（`src/uav_controller.cpp::mainloop`、`px4_rc_cb`、`uav_setup_cb`）：

```text
            RC ch6 低位 / SETUP                    RC ch6 中位（要求 odom_valid）
   ┌──────────────┐ ◄──────────────────────── ┌──────────────────────┐
   │     INIT     │ ────────────────────────► │   RC_POS_CONTROL     │  悬停点 = 当前 odom；
   │ 不发任何指令  │                          │ (OFFBOARD，摇杆积分   │  摇杆按 1.5 m/s(xy)、1.3 m/s(z)、
   │ 强制 POSCTL  │                          │  移动悬停点)          │  1.5 rad/s(yaw) 积分
   └──────▲───────┘                          └─────┬─────────▲──────┘
          │ 上锁后                                  │ ch6 高位 且 mode==OFFBOARD
          │（恢复 MC_YAWRATE_MAX）                  ▼         │ ch6 中位
   ┌──────┴───────┐   Agent_CMD=Land / ch8 /   ┌─────────────┴────────┐
   │ LAND_CONTROL │ ◄───────────────────────── │   COMMAND_CONTROL    │ ◄── UAVSetup(SET_CONTROL_MODE,
   │ GPS/RTK:     │   failsafe(geofence|odom|  │ 只在该状态接收         │     "COMMAND_CONTROL")，只要求 armed
   │  AUTO.LAND   │   RC 丢失>1.5s 且非 sim)    │ UAVCommand            │
   │ 其他: 先把    │                            │ 进入时 MPC_XY_VEL_MAX=1.0,
   │ MC_YAWRATE_MAX│                           │ MPC_ACC_HOR=2.0      │
   │ 设 0 再 LAND  │                            └──────────────────────┘
   └──────────────┘
```

关键行为（按源码核对）：
- **在 RC_POS 和 COMMAND 两个状态下，每个循环都会跑 `check_failsafe()`**：
  - `connected==false` 返回 −1，只等待，不下指令；
  - 非 sim 模式下 RC 超过 1.5 s 没数据，返回 3，转 LAND；
  - 超出 geofence 返回 1，转 LAND；
  - odom 无效返回 2，转 LAND 并置 `quick_land`。
  - 另外，只要 PX4 模式不是 OFFBOARD，就会一直请求切到 OFFBOARD。
- **起飞点**在 `uav_state_cb` 检测到 armed 上升沿时锁定为 `Takeoff_position`。`Init_Pos_Hover` 就是飞到 `Takeoff_position + (0,0,Takeoff_height)`，Prometheus 用这种方式实现"起飞"。
- **`uav_cmd_cb` 的优先级锁**：
  - 如果当前是 ABSOLUTE，而新指令既不是 ABSOLUTE 也不是 EXIT，就直接丢弃，并发布 `/stop_control_state=true`，通知规划器停下。
  - `ABSOLUTE + Current_Pos_Hover` 就是"急停悬停"。
- **`set_command_des()`** 负责把 `Move_mode` 翻译成期望值：
  - `XYZ_POS`：减去 `offset_pose`；
  - `XYZ_POS_BODY`：用 `rotation_yaw(uav_yaw)` 把机体系位移转成 ENU 后累加，`yaw_des = yaw_ref + uav_yaw`；
  - `XY_VEL_Z_POS`：z 用绝对高度；
  - `TRAJECTORY`：取 p/v/a；
  - `XYZ_ATT`：要求 `enable_external_control=true`，否则退回 Init_Pos_Hover；
  - `LAT_LON_ALT`：高度用 `FRAME_GLOBAL_REL_ALT`，即相对 home。
  - 内置的 PID/UDE/NE 控制器**只支持 XYZ_POS 和 TRAJECTORY**，其他模式会退回 Current_Pos_Hover。

**发往 PX4 的 `setpoint_raw/local` type_mask**（mavros 位定义：PX=1、PY=2、PZ=4、VX=8、VY=16、VZ=32、AFX=64、AFY=128、AFZ=256、YAW=1024、YAW_RATE=2048）：

| 函数 | type_mask | 实际控制量 | 用于 |
|---|---|---|---|
| `send_pos_setpoint` | `0b100111111000` | xyz + yaw | 悬停、XYZ_POS、RC_POS |
| `send_vel_setpoint` | `0b100111000111`（见下方轴保持） | vxyz + yaw | XYZ_VEL |
| `send_vel_setpoint_yaw_rate` | `0b010111000111` | vxyz + yaw_rate | XYZ_VEL + Yaw_Rate_Mode |
| `send_vel_xy_pos_z_setpoint` | `0b100111000011` | vx vy + z + yaw | XY_VEL_Z_POS |
| `send_pos_vel_xyz_setpoint` | `0b100111000000` | xyz + vxyz + yaw | **TRAJECTORY（acc 前馈丢失）**、LAND |
| `send_acc_xyz_setpoint` | `0b100000111111` | axyz + yaw | 未被调用 |
| `send_attitude_setpoint` | AttitudeTarget，忽略 body rates | 四元数 + thrust | PID/UDE/NE 输出、XYZ_ATT |
| `send_idle_cmd` | `0x4000` | idle | 未用 |

**速度模式下的"零速轴锁位"技巧**（`send_vel_setpoint`，1142–1403 行）：
- 某一轴的 |v_sp| ≤ `Speed_decision_range = 0.09 m/s` 时，把该轴当作"锁住"。锁定位置 `current_pos` 在进入速度模式或运动方向改变时采样。
- 如果该轴漂移超过 `vel_control_grap = 0.04 m`，就下发 `v = −1.8·(p − p_lock)` 把它拉回来。
- z 轴锁定时改为 z 位置控制（`0b100111000011`）；三轴都锁定时退化成位置悬停。
- yaw 有 2°（0.0349 rad）的死区。
- 这就是"键盘/摇杆速度遥控不漂"的实现方式。我们 Web 端的 WASD 飞行控制也应当采用（§3.4）。

**遥控器映射**（`include/rc_input.h`）：
- ch1–4：`(pwm−1500)/500`，5% 死区。
- ch5（按 0.75/0.25 阈值检测边沿）：解锁 / 上锁。
- ch6（三段）：低位 INIT、中位 RC_POS、高位 COMMAND；位置变化超过 0.4 时才触发。
- ch7 上沿超过 0.25：KILL，走 `MAV_CMD_COMPONENT_ARM_DISARM(400)`，param2=21196 强制执行。
- ch8 上沿超过 0.75：LAND。
- 摇杆组合 ch1、ch2、ch3 都 < 1100 且 ch4 > 1900：重启 PX4。
- 仿真时从 `/uavN/prometheus/fake_rc_in` 读取（由 `joy_node` 发布）。

**位置环控制器**（`include/Position_Controller/*.h`，内置控制器时只把姿态和油门发给 PX4）。三者输出同一种量：

```text
PID:  a_des = a_ref + Kp·e_p + Kv·e_v + Kvi·∫e_p   （e_p 超过 3 m 时截断为 ±1 m；e_v 超过 3 m/s 时截断为 ±2 m/s；
                                                       xy 在 |e|<0.2 m、z 在 |e|<0.5 m 且 OFFBOARD 时才积分）
UDE:  u_l = a_ref + Kp·e_p + Kd·e_v ;  u_d = −(Kp·∫e_p + Kd·e_p + e_v)/T_ude ;  a_des = u_l − u_d
NE:   在 UDE 基础上加噪声估计 LPF(v)+HPF(p0−p)，以及 LeadLag 滤波
共同：F = m·a_des + m·g·ẑ ;  Fz 截断到 [0.5, 2]·m·g ;  |Fx/Fz|、|Fy/Fz| ≤ tan(tilt_max)
      F_c = Rz(ψ)ᵀF ;  roll = atan2(−F_cy, F_cz) ;  pitch = atan2(F_cx, F_cz) ;  yaw = ψ_des
      throttle = (F·z_b) / (m·g/hov_percent)，截断到 [0.1, 1]
默认参数：PID Kp=2, Kv=2, Kvi=0.3, tilt 10°；UDE Kp=0.5, Kd=2, T_ude=1, tilt 20°；m=1.0, hov=0.5
```

注意：主循环是 100 Hz，但 `update(200.0)` 用的是 1/200 做积分，**积分量实际偏小一半**，属于源码 bug。

**配置**（`launch/uav_control_outdoor.yaml`）：
- `Takeoff_height` 1.5 m，`Land_speed` 0.2 m/s，`Disarm_height` 0.1 m；
- `maximum_safe_vel_xy` 5 m/s，`maximum_safe_vel_z` 4 m/s（超过就判定 odom 无效）；
- `COMMAND_MPC_XY_VEL_MAX` 1.0 m/s，`COMMAND_MPC_ACC_HOR` 2.0 m/s²；
- 写入 PX4 的参数：`MPC_XY_VEL_MAX` 3、`MPC_ACC_HOR` 3、`MC_YAWRATE_MAX` 30°/s；
- geofence 为 ±1000 m、z ∈ [−50, 1000]。

### 2.3 `communication`：地面站协议（本单元的核心）

源码分工：
- `communication_bridge.cpp`：CommunicationBridge 继承自 Communication，负责会话、心跳和模式切换。
- `uav_basic_topic.cpp`：UAVBasic，做单机 ROS 话题和协议之间的双向转换。
- `swarm_control_topic.cpp`：做集群的 MultiUAVState 聚合和 SwarmCommand 转发。
- 编解码、socket 和 CRC 都在**闭源的 `shard/libs/libcommunication_{x86_64,aarch64}.so`** 里，只有 `communication.hpp` 和 `Struct.hpp` 两个头文件。

**帧格式**：通过反汇编 `encodeMsg<T>` / `decodeMsg` / `checksum` 得到，并用 .so 实测确认。

```text
offset  size  字段
0       2     magic = 0x61 0x6D ("am")
2       4     payload_len (uint32, little-endian) = JSON 字节数
6       1     msg_id (uint8, 见下表)
7       1     robot_id (uint8, encodeMsg 的 id 参数，为 0 时取 init() 设置的 ROBOT_ID)
8       N     payload = nlohmann::json::dump()，紧凑格式，key 按字典序（std::map）
8+N     2     CRC-16/ARC (poly 0xA001 reflected, init 0x0000) over bytes [0, 8+N)，little-endian
总开销 10 B；上限 BUF_LEN = 1 MiB（编码时检查 payload < 0xFFFF6）
实测样例（Heartbeat, robot 3）：61 6d 1a 00 00 00 06 03 {"count":7,"message":"hb"} 7a d5
```

**MsgId**（`Struct.hpp::MsgId`）：
- 机载端发给地面站：1 UAVSTATE、3 TEXTINFO、4 GIMBALSTATE、5 VISIONDIFF、6 HEARTBEAT、7 UGVSTATE、8 MULTIDETECTIONINFO、9 UAVCONTROLSTATE、10 POSESTAMPED。
- 地面站发给机载端：101 SWARMCOMMAND、102 GIMBALCONTROL、103 GIMBALSERVICE、104 WINDOWPOSITION、105 UGVCOMMAND、106 GIMBALPARAMSET、107 IMAGEDATA、108 UAVCOMMAND、109 UAVSETUP、110 PARAMSETTINGS（双向）、111 BSPLINE、112 MULTIBSPLINES、113 CUSTOMDATASEGMENT_1。
- 会话类：201 CONNECTSTATE、202 MODESELECTION、255 GOAL。
- 230–236 是 RViz 数据（UGV 激光、点云、TF、Marker）。

**端口与方向**（按 `launch/bridge.launch` 的默认值，并用 `send.cpp` + `listen.py` 实测）：

| 方向 | 传输 | 目标 | 内容 | 频率 |
|---|---|---|---|---|
| 地面站 → 机载端 | **TCP，每条指令建一次连接**：连接、发一帧、关闭；机载端 `recv` 读到 EOF 或 5 s 超时后才解码 | 机载端 `tcp_port` **55555** | ModeSelection / UAVSetup / UAVCommand / ParamSettings / SwarmCommand / Gimbal*… | 按需 |
| 机载端 → 地面站 | UDP 单播 | 地面站 `udp_port` **8889**：UAVState 发往 `multicast_udp_ip`（launch 里设成地面站 IP，默认组播组是 224.0.0.88），其余发往 `ground_station_ip` | UAVState、UAVControlState、UAVCommand（回显）、TextInfo、ParamSettings、GimbalState、CustomDataSegment | UAVState 和 UAVControlState 按 `uav_basic_hz`（默认 **10 Hz**，设为 0 则按订阅频率 50 Hz） |
| 机载端 → 地面站 | TCP（每次建连） | 地面站 `tcp_heartbeat_port` **55556** | Heartbeat，内容是 `{"count":n,"message":"CPUUsage:..,CPUTemperature:..,rosnode:/a,rosnode:/b,…"}` | 1 Hz |
| 集群仿真 | UDP 组播接收 | 机载端 8889 | 其他机的 UAVState，聚合为 MultiUAVState | — |

**会话建立**（`serverFun` / `createMode`）：
1. **第一个**发来 TCP 帧的客户端 IP 被记为 `ground_station_ip`；如果 `multicast_udp_ip` 与地面站 IP 相同，也一并改写。之后其他 IP 发来的帧会被拒绝，并回一条 ERROR TextInfo。只有原地面站心跳丢失后，才允许新客户端接管。
2. 客户端必须先发 `ModeSelection{mode:1 UAVBASIC, use_mode:0 CREATE, selectId:[ids], is_simulation, swarm_num, cmd:""}`，机载端这时才会创建 `UAVBasic` 话题桥、置 `is_heartbeat_ready_`，并把 `uav_control` 参数通过 ParamSettings 回传。**在这之前发的 UAVCommand 会被静默丢弃。**
3. **仿真模式**（`is_simulation=1`）下，一个桥接节点可以代理多架飞机，`UAVCommand` 按**帧头的 `robot_id` 字节**路由到 `uavs_[robot_id]`。真机模式下只有一个 `uav_`。
4. UGV 在 `selectId` 里用负数表示。

**心跳与失联保护**（`toGroundHeartbeat`、`checkHeartbeatState`）：
- 每秒往地面站 55556 发一次 Heartbeat。
- 发送失败计数 `disconnect_num` 达到 `try_connect_num`（launch 默认 5）时：单机调用 `triggerUAV()`，下发 `Agent_CMD=Land`；集群调用 `communicationStatePub(false)`，由集群模块处理。随后停发心跳，直到地面站重新发 ModeSelection。
- `swarm_data_update_timeout`（5 s）内 UAVState 的时间戳没有变化，就回报 "data update timeout"。

**JSON 形状的坑**（用 `j.cpp` / `j2.cpp` 实际 dump）：
- UAVState：`{"altitude":…,"armed":…,"attitude":[r,p,y],"attitude_q":[x,y,z,w],"attitude_rate":[…],"battery_percetage":…,"battery_state":…,"connected":…,"gps_num":…,"gps_status":…,"latitude":…,"location_source":…,"longitude":…,"mode":"OFFBOARD","nsecs":…,"odom_valid":…,"position":[…],"secs":…,"topic_name":"/uav1/prometheus/state","uav_id":…,"velocity":[…]}`。**没有 `range` 和 `rel_alt`**，`attitude_q` 是数组。
- **结构体向量会被拍平**：`formation_poses[0].x`、`pos_pts[0].x`、`params[0].param_name`，再加一个 `<name>_num` 计数字段。基本类型数组（`knots`、`yaw_pts`、`selectId`）保持 JSON 数组。
- 解码端按 key 取值、缺省用默认值，所以**只发部分字段也能被接受**（已用 `dec.cpp` 验证）。
- `Struct.hpp::UAVControlState::ControlState` 的枚举是 `INIT=0, MANUAL_CONTROL=1, HOVER_CONTROL=2, COMMAND_CONTROL=3, LAND_CONTROL=4`，**而 ROS msg 是 0..3**。`UAVBasic::controlStateCb` 原样拷贝数值，所以线上值其实是 ROS 语义：2 = COMMAND。**解码时必须按 ROS 语义解释。**

### 2.4 `ego_planner_swarm`（Amov fork）

- 相对上游 ZJU `ego-planner-swarm`（`refs/swarm/ego-planner-swarm` @ `92fe9f7`），改动了 `ego_replan_fsm.cpp`、`planner_manager.cpp`、`traj_server.cpp`、`bspline_optimizer.cpp`、`ego_planner_node.cpp`，新增了 `src_for_prometheus/traj_server_for_prometheus.cpp`、`launch_for_prometheus/*`、`drone_detect`、`su17_*`（深度压缩）。
- FSM 状态是 `INIT / WAIT_TARGET / GEN_NEW_TRAJ / REPLAN_TRAJ / EXEC_TRAJ / EMERGENCY_STOP / SEQUENTIAL_START`，目标类型分 MANUAL / PRESET / REFENCE_PATH。
- Prometheus 加的 gating 逻辑：订阅 `control_state`，**只有 `==2`（COMMAND）时才规划**。退出 COMMAND 时清空轨迹，重新进入时对上次终点重规划；同时订阅 `stop_control_state`（ABSOLUTE 锁）。目标点 `x == y == 99.99` 表示原地悬停。
- **`traj_server_for_prometheus`**：
  - 以 100 Hz 对 B 样条做 de Boor 求值，得到 p/v/a；
  - yaw 取前视 `time_forward = 1 s` 方向的 atan2，限速 π rad/s；
  - 发布 `UAVCommand{Move, TRAJECTORY}`（`control_flag=1` 时发 XYZ_POS）。
- **写死的话题**：`/uav1/prometheus/control_state`、`/uav1/prometheus/command`、`/uav1/prometheus/stop_control_state`、`/uav1/prometheus/param_settings`、`/uav1/prometheus/motion_planning/goal`。多机时必须每机一个 `ns` 再做 remap。
- 多机轨迹共享：`rosmsg_tcp_bridge` 在 TCP 8080 上把 MultiBsplines 环形转发给下一架，在 UDP 8081 上广播 odom 和单条轨迹。
- 轨迹话题 `/uav{id}_planning/swarm_trajs` 形成环链（id−1 → id）。
- P600 参数（`advanced_param_p600.xml`）：
  - grid 分辨率 0.15 m，局部更新范围 20×20×5 m，障碍膨胀 0.6 m；
  - `lambda_smooth`=1、`collision`=0.5、`feasibility`=0.1、`fitness`=1，`dist0`=0.5，`swarm_clearance`=0.5；
  - SITL 默认 `max_vel`=0.8、`max_acc`=0.8，`planning_horizon`=7.5。

### 2.5 `motion_planning`

- `global_planner/src/A_star.cpp`：三维栅格 A*。`f = g + λ_heu·h_eucl + λ_cost·cost(occupancy)`，`lambda_heu`=2.0，tie_breaker = 1 + 1/max_search_num。地图来自 `occupy_map.cpp`（PCL 点云转栅格并膨胀）。
- `local_planner/src/apf.cpp`：人工势场。
  - 引力 `k_att·clip(goal−p, 5 m)`，不考虑 z。
  - 斥力对每个局部点：`k_rep·(1/d − 1/R)/d²·(−dir)`，其中 d = |p_obs| − 0.2（膨胀）；d < 0.2 时取 0.2/1.5；R = 5 m。距目标不足 1 m 时斥力按距离缩放，最后对障碍点数取平均，再转到世界系。
  - 输出期望速度，z 置 0。有超过 10 个点距离小于 0.15 m 时判定为危险。
  - 默认参数 `k_rep`=0.8、`k_att`=0.4。
- `vfh.cpp`：向量场直方图。
- `min_snap_trajectory`：多项式 min-snap 加 so3 四旋翼模拟器（KumarRobotics 系）。

### 2.6 `simulator_utils`

- **`fake_odom/fake_uav.cpp`**：用来替代 PX4 SITL，**是我们 MVP Mock 的最佳参考**。
  - 订阅 `setpoint_raw/local`，按 type_mask 做质点级联控制：`u = k_vel·(k_pos·(p_sp−p) [+v_sp] − v)`，`k_pos`=`k_vel`=0.8。
  - 推力计算与 PID 控制器相同（重力补偿，Fz 截断到 [0.5, 2]mg，倾角上限 25°）。
  - 假设姿态瞬时响应；a = F/m − g，50 Hz 欧拉积分，地面约束 z ≥ 0。
  - 也支持 AttitudeTarget 输入（油门/悬停油门 × mg）。
  - **问题**：fake_odom 不模拟 `mavros/state`，而 `uav_control_node` 启动时会阻塞等待 `connected`，所以当前代码里 fake_odom 这条链路**跑不通**。
- **`quadrotor_dynamics/quadrotor_dynamics.cpp`**：22 维刚体 ODE（KumarRobotics so3_quadrotor_simulator 的中文注释版）。
  - 状态为 p、v、R（9 维）、ω、4 个电机转速。
  - 用 odeint 积分，每步做 LLT 正交化。
  - 电机一阶滞后 1/30 s，二次阻力 `0.1·π·L²·|v|²`，可设置外力和外力矩（风扰）。
- `map_generator`：生成方块/柱体/墙的点云地图，发布 global_cloud（1 Hz）和按 odom 裁剪的 local_cloud（10 Hz），是规划仿真的"世界"。

### 2.7 `tutorial_demo` 与测试轨迹

- `advanced/formation_control/src/formation_control.cpp::getFormationSeparation`：生成一字形、三角形编队的偏移矩阵（N×4），见 §3.9。多机统一坐标系的做法是：以 1 号机 UAVState 的经纬度为原点，给每架机发 `GPSData`，触发 §3.9 的偏移计算。
- `uav_control/include/controller_test.h`：Circle / Eight / Step / Line 四种轨迹，均带解析的 v 和 a 前馈（§3.8）。
- 其他 demo：`uav_control/utils/uav_command_pub.cpp`（交互式指令）、`basic/*`（ENU/机体系/经纬度定点、航点、圆轨迹、起降）。

### 2.8 `Simulator/gazebo_simulator`

- `gazebo_models/uav_models/p600/p600.sdf`：
  - 机体 1.47 kg，惯量 (0.011, 0.015, 0.021) kg·m²，碰撞盒 0.30×0.30×0.09 m；
  - 4 个旋翼位于 (±0.2121, ±0.2121, 0.243)，即臂长 0.30 m、轴距 600 mm，桨半径 0.128 m；
  - `motorConstant` 8.54858e-6，`momentConstant` 0.06，`maxRotVelocity` 1500 rad/s；
  - `timeConstantUp` 0.0125 s，`timeConstantDown` 0.025 s，`rotorDragCoefficient` 8.06428e-4。
  - 另有 `p600_mid360`、`p600_monocular`、`p600_2Dlidar` 等变体。
- **多机 SITL 端口公式**（`launch_basic/sitl_px4_outdoor.launch`），设 `ID = uav_id − 1`：
  - MAVLink sysid = uav_id；
  - 模拟器端口：UDP 14560+ID，TCP 4560+ID；
  - **offboard 端口：14540+ID**，本地端口 14580+ID；mavros 的 `fcu_url = udp://:14540+ID@localhost:14580+ID`；
  - 每机一个 namespace `/uavN`，PX4 工作目录 `sitl_amov_ID`；
  - 4 机 P600 的默认初始 x 为 4.5 / 1.5 / −1.5 / −4.5 m，z = 0.15 m。

### 2.9 其他

- `FAST_LIO`（fork）：`config/mid360.yaml`（`lidar_type=1` Livox，`scan_line=4`，`blind=0.5`，外参 T=[−0.011, −0.023, 0.044]），另有 airsim / gazebo 变体。输出话题 `/Odometry` 会被 estimator 的 MID360 源订阅。细节见 lidar 组的单元。
- `uav_control_fmt`：FMT（Firmament）飞控用 QEMU 做 SITL 的测试。`future_aircraft`：竞赛用（椭圆检测降落）。`ugv_control`：无人车的控制和估计。
- 4 个子模块为空。其中 `swarm_control` 是**真正的集群控制器**，只能在阿木 gitee 上获取。

---

## 3. 可复用算法与实现（伪代码、参数）

### 3.1 Prometheus 地面站协议编解码（Python，已验证）

```python
# apps/api/sim_gateway/backends/prometheus/codec.py （.cache/research/r19/codec.py 已测）
import json, struct
def crc16_arc(b: bytes) -> int:                       # poly 0xA001 (reflected 0x8005), init 0
    c = 0
    for x in b:
        c ^= x
        for _ in range(8): c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c
def encode(msg_id: int, robot_id: int, obj: dict) -> bytes:
    pl = json.dumps(flatten(obj), separators=(",", ":")).encode()
    body = b"am" + struct.pack("<IBB", len(pl), msg_id, robot_id) + pl
    return body + struct.pack("<H", crc16_arc(body))
def decode(buf: bytes) -> tuple[int, int, dict]:
    assert buf[:2] == b"am"
    n, mid, rid = struct.unpack_from("<IBB", buf, 2)
    (crc,) = struct.unpack_from("<H", buf, 8 + n)
    if crc != crc16_arc(buf[:8 + n]): raise ValueError("crc")
    return mid, rid, unflatten(json.loads(buf[8:8 + n]))
# flatten: {"params":[{...}]} -> {"params[0].param_name":..., "params_num":1}
# 适用字段 formation_poses / pos_pts / params / datas / detection_infos
```

- 实测：Python 编出的 Heartbeat 与 .so 输出**逐字节相同**。Python 编出的**部分字段** UAVCommand 和 UAVSetup 能被官方 `decodeMsg` 正确解析（decode_state=0，各字段值一致），`flatten(ParamSettings)` 与 .so 的 `toJson` 结果完全相同。
- CRC 用纯 Python 算，每帧不到 5 µs（JSON ≤ 0.5 KB）；需要更快可以换 `crcmod` 的 `crc-16` 预设（等价于 ARC）。
- **UDP 一个数据报就是一帧；TCP 要读到 EOF 才算一帧**（机载端就是这么实现的），所以不需要做流式拆帧。为了健壮，建议解码时仍按 `8 + n + 2` 校验长度。

### 3.2 Gateway 侧地面站会话（伪代码）

```python
class PrometheusGSBackend(SimBackend):            # 我们扮演"Prometheus 地面站"
    async def start(self, drones: list[DroneCfg]):
        self.udp = await loop.create_datagram_endpoint(StateProto(self), local_addr=("0.0.0.0", 8889))
        self.hb  = await asyncio.start_server(self.on_heartbeat, "0.0.0.0", 55556)   # 必须常驻，否则 5 s 后真机自动 Land
        for d in drones:                                # SITL 下一个 bridge 对应多机；真机下一台一个 bridge
            await self.tcp_send(d.ip, 202, d.robot_id, {"mode": 1, "use_mode": 0, "selectId": [d.robot_id],
                                                         "is_simulation": d.sim, "swarm_num": 0, "cmd": ""})
    async def tcp_send(self, ip, msg_id, robot_id, obj):   # 每帧一次连接，与机载实现一致
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, 55555), 1.0)
        w.write(encode(msg_id, robot_id, obj)); await w.drain(); w.close(); await w.wait_closed()
    def datagram_received(self, data, addr):
        mid, rid, obj = decode(data)
        if   mid == 1:  self.bus.publish(to_drone_state(rid, obj))        # §4.2
        elif mid == 9:  self.authority[rid] = ROS_CONTROL_STATE[obj["control_state"]]   # 按 ROS 语义 0..3 解释
        elif mid == 3:  self.bus.event(rid, TEXT_LEVEL[obj["MessageType"]], obj["Message"])
        elif mid == 110: self.params[rid].update(unflatten(obj))
    async def on_heartbeat(self, r, w):               # 读到 EOF
        mid, rid, obj = decode(await r.read()); self.gs_link[rid] = now(); self.companion[rid] = parse_kv(obj["message"])
```

### 3.3 控制权状态机（移植给 Mock，保证与真机语义一致）

```python
class Authority(Enum): INIT=0; MANUAL=1; COMMAND=2; LAND=3        # 与 ROS UAVControlState 数值一致
def tick(s: MockDrone, dt):
    if s.authority in (MANUAL, COMMAND):
        f = failsafe(s)            # 顺序与 check_failsafe 相同：fcu_link → rc_timeout(非 sim) → geofence → odom_valid
        if f: s.authority, s.failsafe = LAND, f; emit(ERROR, f.msg)
    match s.authority:
        case INIT:    s.setpoint = None                                  # 不下发任何指令
        case MANUAL:  s.hover += stick * [1.5,1.5,1.3] * dt ; s.hover.z = max(s.hover.z, 0.2); s.setpoint = hover
        case COMMAND: s.setpoint = command_to_setpoint(s, s.cmd)          # §3.4
        case LAND:    s.setpoint = land_profile(s)                        # vz = -Land_speed(0.2) 或 PX4 AUTO.LAND 曲线
                      if s.on_ground and s.armed: s.armed = False
                      if not s.armed: s.authority, s.cmd = INIT, Init_Pos_Hover
def on_setup(s, m):                          # UAVSetup
    if m.cmd == ARMING: s.armed = m.arming and s.on_ground; s.takeoff_pos = s.pos.copy() if s.armed else s.takeoff_pos
    if m.cmd == SET_CONTROL_MODE and m.control_state == "COMMAND_CONTROL" and s.armed: s.authority = COMMAND
def on_command(s, c):                        # UAVCommand
    if s.authority != COMMAND: s.cmd = Init_Pos_Hover; return REJECTED
    if s.level == ABSOLUTE and c.level not in (ABSOLUTE, EXIT_ABSOLUTE): return IGNORED   # 同时发布 stop_control_state
    s.level = ABSOLUTE if c.level == ABSOLUTE else DEFAULT
    s.cmd = c; return ACCEPTED
```

MVP 里没有遥控器，所以 `sim_mode=True`：跳过 RC 超时检查，`MANUAL` 状态由 Web 的虚拟摇杆（WASD）驱动。

### 3.4 指令语义（`set_command_des` 的移植版）

```python
def command_to_setpoint(s, c) -> Setpoint:           # 输出统一的 (p?, v?, a?, yaw | yaw_rate, mask)
    if c.agent == Init_Pos_Hover:   return pos(s.takeoff_pos + [0,0,P.takeoff_height], yaw=0)
    if c.agent == Current_Pos_Hover:
        if s.last_cmd.agent != Current_Pos_Hover: s.hold = (s.pos.copy(), s.yaw)
        return pos(*s.hold)
    if c.agent == Land: s.authority = LAND; return None
    m = c.move_mode
    if m == XYZ_POS:        return pos(c.p, c.yaw)
    if m == XY_VEL_Z_POS:   return vel_xy_pos_z(c.v[:2], c.p[2], c.yaw)
    if m == XYZ_VEL:        return vel_axis_hold(s, c.v, c.yaw, c.yaw_rate if c.yaw_rate_mode else None)
    if m in BODY_MODES and c.cmd_id <= s.last_cmd.cmd_id: return s.last_sp   # 只执行一次
    if m == XYZ_POS_BODY:   d = rotz(s.yaw) @ [c.p[0], c.p[1], 0]; return pos(s.pos + d + [0,0,c.p[2]], s.yaw + c.yaw)
    if m == XYZ_VEL_BODY:   return vel_axis_hold(s, rotz(s.yaw) @ c.v, s.yaw + c.yaw)
    if m == TRAJECTORY:     return pva(c.p, c.v, c.a, c.yaw)          # Mock 保留 a 前馈（PX4_ORIGIN 会丢掉，§6）
    if m == LAT_LON_ALT:    return pos(lla_to_world(c.lat, c.lon, home_alt + c.alt), c.yaw)   # alt = REL_ALT
def vel_axis_hold(s, v, yaw, yaw_rate=None, EPS=0.09, GAP=0.04, KP=1.8):
    locked = abs(v) <= EPS                                             # 每轴判断
    if orient(v) != s.prev_orient: s.lock = s.pos.copy(); s.prev_orient = orient(v)
    drift = s.pos - s.lock
    v_cmd = where(locked & (abs(drift) >= GAP), -KP * drift, where(locked, 0, v))
    return vel(v_cmd, yaw, yaw_rate) if not locked[2] else vel_xy_pos_z(v_cmd[:2], s.lock[2], yaw)
```

### 3.5 MVP Mock 动力学（`.cache/research/r19/mock_uav.py`，numpy SoA 向量化）

Mock 的做法：fake_uav 的推力分配和倾角/推力限幅，加上 PX4 的级联增益、速度/加速度限幅、一阶姿态滞后、线性阻力（风扰）和加速度域积分。坐标系为 World ENU，yaw 用 ENU 约定。

```text
v_sp = v_ff + diag(Kp_xy,Kp_xy,Kp_z)·(p_sp − p)          [XYZ_VEL 时直接取指令 v_sp]
v_sp_xy 按范数截断到 vmax_xy；v_sp_z 截断到 [−vmax_dn, vmax_up]
e_v = v_sp − v ;  I += diag(Ki_xy,Ki_xy,Ki_z)·e_v·dt ，|I_xy| ≤ acc_max_xy，|I_z| ≤ 0.5g
a_sp = a_ff + diag(Kv_xy,Kv_xy,Kv_z)·e_v + I ；|a_sp_xy| ≤ acc_max_xy
F = m(a_sp + g ẑ) ；Fz 截断到 [0.5mg, 2mg] ；|Fx|,|Fy| ≤ Fz·tan(tilt_max)
(roll,pitch)_des = (atan2(−F_cy, F_z), atan2(F_cx, F_z))，F_c = Rz(ψ)ᵀF
(roll,pitch) += ((roll,pitch)_des − (roll,pitch))·dt/(τ_att+dt) ；ψ 以 yaw_rate_max 限速逼近 ψ_des
z_b = R(roll,pitch,ψ)·ẑ ；T = F·z_b
a = z_b·T/m − g ẑ + c_d·(w(p,t) − v)/m          ← 风场 E(x,y,z,t) 从这里接入
v += a·dt ; p += v·dt ；z < 0 时贴地（fake_uav）；throttle = T/(mg/hover_thr) 截断到 [0.1,1]（给 HUD 显示）
```

| 参数 | 值 | 来源 |
|---|---|---|
| m | 1.505 kg（占位，V0.4 前实测替换） | p600.sdf |
| Kp_xy / Kp_z | 0.95 / 1.0 | PX4 `MPC_XY_P` / `MPC_Z_P` 默认值 |
| Kv_xy / Kv_z | 1.8 / 4.0 | `MPC_XY_VEL_P_ACC` / `MPC_Z_VEL_P_ACC` |
| Ki_xy / Ki_z | 0.4 / 2.0 | `MPC_XY_VEL_I_ACC` / `MPC_Z_VEL_I_ACC` |
| vmax_xy / up / dn | 3 / 3 / 1 m/s（COMMAND 状态下为 1 m/s） | uav_control_outdoor.yaml |
| acc_max_xy | 3 m/s² | `MPC_ACC_HOR` |
| tilt_max | 25° | fake_uav |
| τ_att | 0.08 s | 经验值（fake_uav 取 0） |
| c_d | 0.35 N/(m/s) | 经验值，用于风扰 |
| yaw_rate_max | 30°/s | `MC_YAWRATE_MAX` |
| dt | 0.01 s（100 Hz 物理），推送 20 Hz | — |

**实测**（单机从原点飞到 (10,0,5)）：

| 北向风 | 进入 0.2 m 误差带 | 最大超调 | 20 s 末误差 | 峰值速度 |
|---|---|---|---|---|
| 0 m/s | 4.25 s | 0.30 m | 3 mm | 3.79 m/s |
| 5 m/s | 6.86 s | 0.30 m | 10 mm | 3.80 m/s |
| 8 m/s | 8.55 s | 0.30 m | 16 mm | 3.82 m/s |

积分器最初是速度域、限幅 ±1.5，8 m/s 风下稳态误差达到 0.74 m；改成加速度域、限幅 3 m/s² 后降到 1.6 cm。

**规模**（numpy 2.5.3，单核，无 GPU）：

| N | 每步耗时 | 最高物理频率 |
|---|---|---|
| 10 | 0.74 ms | 1345 Hz |
| 100 | 0.78 ms | 1285 Hz |
| 1000 | 2.19 ms | 456 Hz |

结论：MVP 的 Mock **不需要** PX4，也不需要多进程。

### 3.6 推力向量到姿态/油门（用于 HUD 和 FPV 相机的姿态显示）

Mock 给出的 `rp`、`throttle` 可以直接当作 DroneState 的姿态字段。前端 FPV 相机按 `orientation` 渲染机体倾斜，风大时画面就会"斜着飞"，这是 V0.3/V0.4 里最直观的视觉反馈。

### 3.7 刚体动力学（V0.4 "Wind → Drone Force"，移植 `quadrotor_dynamics.cpp`）

```text
状态 X = [p(3), v(3), R(9), ω(3), rpm(4)]
T = kf·Σ rpm_i² ;  M = [kf·(rpm3²−rpm4²)·L,  kf·(rpm2²−rpm1²)·L,  km·(rpm1²+rpm2²−rpm3²−rpm4²)]
ṗ = v ;  v̇ = −g ẑ + T·R ê3/m + F_ext/m − 0.1·π·L²·|v|²·v̂/m
Ṙ = R·[ω]× ;  ω̇ = J⁻¹(M − ω×Jω + M_ext) ;  ṙpm = (rpm_cmd − rpm)/τ_m ；每次求导前对 R 做 LLT 正交化
F_ext = ½ρ·Cd·A·|w_rel|·w_rel （w_rel = W(p,t) − v），M_ext = r_cp × F_ext（阵风力矩）
```

P600 初值取自 SDF：kf=8.54858e-6、km=0.06·kf、L=0.30 m、J=diag(0.011, 0.015, 0.021)、ω_max=1500 rad/s、τ_m=0.0125/0.025 s。
- 由此算出悬停 ω=657 rad/s，推重比 5.21，悬停推力占比 0.19。
- 这组数与真实 P600（整机加电池加 MID-360 加吊舱）明显不符，**必须辨识**。辨识方法：用真机 ulog 里的悬停油门、阶跃响应，拟合 m、kf、τ_att。

### 3.8 内置轨迹生成器（给 Mock 和 Gateway 的 FOLLOW_PATH / ORBIT 用）

```text
Circle(t): ω = dir·|v_lin/r| ; p = c + r[cosωt, sinωt, 0] ; v = rω[−sinωt, cosωt, 0] ; a = −rω²[cosωt, sinωt, 0]
Eight(t):  θ = ω_8 t ; p = cosθ·r̂ + sinθcosθ·(â×r̂) + (1−cosθ)(â·r̂)â + o ，其中 r̂=[R,0,0]，â=[0,0,2]
           v = ω_8(−sinθ·r̂ + cos2θ·(â×r̂) + sinθ(â·r̂)â)
Step(t):   x = ±L，按 floor(t/T_step) 的奇偶交替 ；Line(t)：只取 Circle 的 y 分量（来回往返）
```

ORBIT 就是 Circle 加 `yaw = atan2(c − p)`（机头朝向圆心）。FOLLOW_PATH 分两种实现：
- Mock 或 rosbridge 后端：用 min-jerk 或梯形速度曲线，以 50 Hz 流式下发 TRAJECTORY；
- **Prometheus 地面站协议后端**：每条指令都要新建 TCP 连接，所以改为到点切换的 `XYZ_POS` 航点序列（1–2 Hz），见 §4.5。

### 3.9 编队 separation 与多机坐标统一

```text
getFormationSeparation(shape, size, N) -> N×(dx,dy,dz,dyaw)，最后整体乘以 size
  一字形（N 奇数）：dy_i = i − (N−1)/2 ；（N 偶数）：dy_i = i − (N/2 − 0.5)
  三角形（N 奇数，k=(N−1)/2）：i ≤ k 时 (dx,dy)=(i, i−k)，镜像 (dx,dy)_{N−1−i}=(i, k−i)
  三角形（N 偶数，k=N/2）：(dx,dy)_i=(i, i+1−k)，镜像 dy=k−i−1，i+1=k 时 dx=0
set_local_pose_offset_cb(origin_gps{lat,lon,alt,x,y}):
  e = ECEF(uav_lla) − ECEF(origin_lla) ;  enu = R_ecef→enu(origin)·e
  offset += enu − uav_local_xy + origin_xy       # 带累计修正，可以重复调用
  state.position = px4_local + offset ;  command.pos_ref −= offset   # 所有机器统一到 1 号机坐标系
```

**我们的改法**：不用"1 号机坐标系"，统一到 **World ENU 锚点**（r15 §3.1）。Gateway 为每架机维护 `T_world←local_i`：
- GPS/RTK 机：平移 = 该机 EKF 原点（home）LLA 在 World 锚点下的 ENU 坐标，不需要旋转。
- MID-360/SLAM 机：用 V0.5 的地图配准结果。
- SITL：用 spawn 位姿。

### 3.10 B 样条求值（前端渲染 EGO 规划轨迹，对应 msg 111/112）

```ts
// 均匀 B 样条：knots 由 msg 给出（setUniformBspline：u_i = (−p+i)·Δt）；定义域 [u_p, u_{m−p}]
function deBoor(u: number, P: Vec3[], U: number[], p: number): Vec3 {
  const ub = clamp(u, U[p], U[U.length - 1 - p]); let k = p; while (U[k + 1] < ub) k++;
  const d = P.slice(k - p, k + 1).map(v => [...v]);
  for (let r = 1; r <= p; r++) for (let i = p; i >= r; i--) {
    const a = (ub - U[i + k - p]) / (U[i + 1 + k - r] - U[i + k - p]);
    d[i] = lerp(d[i - 1], d[i], a);
  }
  return d[p];
}
// 导数控制点 Q_i = p·(P_{i+1}−P_i)/(U_{i+p+1}−U_{i+1})，得到 p−1 阶 B 样条（用于速度着色）
// yaw：dir = pos(t+1s) − pos(t)；|dir|>0.1 时 yaw = atan2(dir.y, dir.x)；角速度限幅 π rad/s（traj_server）
```

前端按 20–40 个采样点生成 `Line2`（线宽固定），按速度大小做颜色映射，最多支持 8 条轨迹。

### 3.11 避障（Mock 的"带避障 GoTo"，V0.6）

- **APF**：参数按 §2.5；障碍点来自 World 的 `occupancy`（用 UrbanScene3D 点云降到 0.5 m 体素），查询该机 5 m 半径内的体素中心。每机每步 O(k)，k ≤ 200，并按 §3.5 的 SoA 做向量化。APF 输出的 `desired_vel` 作为 XYZ_VEL 喂回控制器，z 用定高。
- **A\***：在 World 体素上做全局规划，`λ_heu=2`，代价 = g + 2h + λ_cost·(距离场代价)。输出航点后交给 FOLLOW_PATH。
- 两者都是 CPU 上的轻量实现，EGO-Swarm 则留给 ROS 后端（V0.6）。

### 3.12 健康检查（映射到 `DroneState.health`）

| 检查项 | 规则（`check_uav_odom`） | 我们的 `failsafe_reason` |
|---|---|---|
| 定位超时 | 各定位源分别设阈值（0.1–1.1 s） | `LOC_TIMEOUT` |
| 速度过大 | \|v_xy\| > 5 或 v_z > 4 m/s | `ODOM_DIVERGED` |
| 外部定位与 PX4 不一致 | vision_pose 误差超过 `maximum_vel_error_for_vision`=2.0 | `ODOM_MISMATCH` |
| 外部定位跳变 | 相邻两帧差超过 0.8 | `ODOM_JUMP` |
| MID-360 协方差 | `covariance_error` | `LIO_DEGRADED` |
| GPS/RTK | fix < 3D 时无效；RTK 未 FIXED 时降级为 WARN | `GNSS_NO_FIX` / `RTK_FLOAT`（WARN） |
| geofence / RC / FCU | `check_failsafe` | `GEOFENCE` / `RC_LOST` / `FCU_LOST` |
| 地面站心跳 | 5 次失败 | `GS_LOST`（自动 Land） |

Mock 里要能**注入**这些故障（UI 的 Debug 面板提供"定位丢失"、"RTK 退化"、"通信中断"开关），用来验证 UI 告警和任务降级。

### 3.13 WebSocket 二进制遥测帧（建议）

```text
Header (18 B): magic "AD"(2) | ver u8 | type u8 (1=DroneStateBatch) | seq u32 | t_sim f64 | count u16
Record (40 B/drone, little-endian):
  u16 agent_no | u8 authority(0..3) | u8 flags(bit0 armed,1 in_air,2 odom_valid,3 failsafe,4 gs_link,5 fcu_link)
  f32 pos[3] (World ENU, m；10 km 范围内 float32 误差 ≤0.7 mm，见 r15)
  i16 vel_cm_s[3] | i16 quat_snorm[4] (x,y,z,w ×32767) | i16 omega_mrad_s[3]
  u8 battery_pct | u8 flight_mode | u16 mission_seq
低频字段（gps、电压、文本事件、mission 细节）走 JSON 的 "state_ext" 消息，1–2 Hz 或变化时才发
```

| 规模 | Prometheus JSON | 紧凑 JSON | 二进制 |
|---|---|---|---|
| 10 架 @20 Hz | 91 KiB/s | 30 KiB/s | 8.2 KiB/s |
| 50 架 @20 Hz | 455 KiB/s | 150 KiB/s | 39 KiB/s |
| 100 架 @30 Hz | 1365 KiB/s | 451 KiB/s | 118 KiB/s |
| 300 架 @20 Hz | 2731 KiB/s | 902 KiB/s | 235 KiB/s |

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 项 | 用途 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| 地面站协议编解码 + 会话 | 连接 P600 真机和 Prometheus SITL | `apps/api/sim_gateway/backends/prometheus/` | V0.2（SITL）、V0.5（真机） | port | 协议已还原；纯 Python，不依赖 ROS 和 .so |
| Prometheus 机载端仿真器 | 让 Mock 伪装成 P600 机载端，用于 Gateway 回环测试，也能让阿木官方地面站连进我们的世界 | `backends/prometheus/emulator.py` | V0.2 | port | 同一套编解码的镜像实现，CI 可测 |
| UAVState / UAVCommand 语义 | DroneState / DroneCommand 的契约 | `packages/contracts`（pydantic + TS 生成） | V0.1 | port | §4.2、§4.3 |
| 控制权 FSM + failsafe | Mock 与真机行为一致 | `sim_gateway/core/authority.py` | V0.1 | port | §3.3 |
| `set_command_des` + 轴锁位 | 指令转 setpoint | `sim_gateway/core/command.py` | V0.1 | port | §3.4 |
| fake_uav + PX4 级联 | MVP Mock 物理 | `sim_gateway/backends/mock/dynamics.py` | V0.1 | port | §3.5，已验证 |
| 22 维刚体 ODE | 风力、力矩、阵风 | 同上，`rigid.py` | V0.4 | port | §3.7 |
| 测试轨迹、编队 separation | ORBIT / FOLLOW_PATH / Formation | `sim_gateway/core/trajgen.py`、`swarm/formation.py` | V0.2 / V0.6 | port | §3.8、§3.9 |
| B 样条求值 | 前端画规划轨迹 | `apps/web/src/layers/mission/bspline.ts` | V0.6 | port | §3.10 |
| APF / A* | Mock 避障 | `swarm/avoidance/` | V0.6 | port | §3.11 |
| EGO-Swarm fork | 真规划 | ROS1 docker 后端 | V0.6 | reference | 写死 `/uav1`、内存问题；优先用上游版本 |
| p600.sdf + SITL 端口公式 | Digital Twin 初值；Gateway 连接 PX4 | `vehicles/p600/params.yaml`、`backends/px4` | V0.2 | reference | §2.8 |
| SwarmCommand 枚举 | 任务类型清单（Search / Follow / Formation / Attack） | `agent/mission-types` | V1.0 | reference | 子模块为空，只借用语义 |
| `libcommunication_*.so` | — | — | — | skip | 闭源，只有 x86_64/aarch64，Python 端不需要 |

### 4.2 `DroneState` 统一模型与 Prometheus 字段映射

建议的统一模型（TS 形式，Python 端用 pydantic 同构）。坐标约定：**World ENU（米），机体 FLU，yaw ENU（0 = 东，逆时针为正）**，与 Prometheus 相同，也与 r15 的规范一致。

```ts
interface DroneState {
  id: string; agent_no: number; vehicle: "p600" | string; backend: "mock" | "prometheus" | "px4" | "replay";
  seq: number; t: number;                       // 会话相对仿真时间 s（float64）
  position: Vec3; orientation: Quat /*[x,y,z,w]*/; velocity: Vec3; acceleration: Vec3; angular_velocity: Vec3;
  geo?: { lat: number; lon: number; alt_ellipsoid: number; rel_alt?: number; fix: GpsFix; sats: number };
  agl?: number;
  battery: { voltage: number; percent: number; remaining_s?: number };
  armed: boolean; in_air: boolean;
  flight_mode: FlightMode;                      // 任务级：IDLE|TAKEOFF|HOVER|GOTO|FOLLOW_PATH|ORBIT|RTL|LAND|MANUAL|FORMATION|EMERGENCY
  autopilot_mode: string;                       // 原始 PX4 模式字符串
  control: { authority: "INIT"|"MANUAL"|"COMMAND"|"LAND"; level: "DEFAULT"|"ABSOLUTE";
             controller: "PX4"|"PID"|"UDE"|"NE"|"MOCK"; last_cmd_id: number; last_cmd_result: "ACCEPTED"|"REJECTED"|"IGNORED" };
  health: { fcu_link: boolean; gs_link: boolean; odom_valid: boolean; loc_source: LocSource;
            failsafe: FailsafeReason | null; companion?: { cpu: number; temp_c: number; nodes?: string[] } };
  mission?: { id: string; state: "IDLE"|"RUNNING"|"PAUSED"|"DONE"|"ABORTED"; seq: number; progress: number; target?: Vec3 };
  env?: { wind: Vec3 };                         // 仿真真值：机体所在处的风
}
```

| DroneState | Prometheus（ROS msg / 线上 JSON） | 转换 | 备注 |
|---|---|---|---|
| `agent_no` / `id` | `uav_id` / 帧头 `robot_id` | `id = f"p600-{n:02d}"` | 仿真多机路由靠 `robot_id` |
| `t` | `header.stamp` / `secs, nsecs` | 减去会话起点 | 机载时钟可能不同步，Gateway 另记接收时间 `t_rx` |
| `position` | `position[3]` | `T_world←local_i · (p − offset)` | Prometheus 输出的是"起飞点或 1 号机"的 ENU |
| `orientation` | `attitude_q`（ROS 是 x,y,z,w 字段；JSON 是 `[x,y,z,w]`） | 旋转 `R_world←local_i` | FLU→ENU |
| `(roll,pitch,yaw)` 派生 | `attitude[3]` | yaw 加上 θ_i | 只用于展示 |
| `velocity` | `velocity[3]` | 旋转 | ENU |
| `acceleration` | **无** | 对 v 做差分，再过 LPF（τ=0.1 s） | 缺失字段 |
| `angular_velocity` | `attitude_rate[3]` | — | 机体系 |
| `geo.lat/lon` | `latitude/longitude`（**float32**） | 不作为几何来源 | 量化误差 0.72 m / 0.21 m，只用于展示 |
| `geo.alt_ellipsoid` | `altitude` | — | mavros 的 NavSatFix 高度是椭球高 |
| `geo.rel_alt` / `agl` | `rel_alt` / `range`（只在 ROS 里有） | 地面站协议下为空 | rosbridge 后端才有 |
| `geo.fix` / `sats` | `gps_status` / `gps_num` | 枚举 0..8 | 6 = RTK_FIXED |
| `battery.voltage` / `percent` | `battery_state` / `battery_percetage` | — | 注意拼写 |
| `armed` | `armed` | — | — |
| `in_air` | 无 | armed、z − z_home > 0.3 m、\|v\| 共同判定 | 派生 |
| `autopilot_mode` | `mode` | 原样保留 | "OFFBOARD" 等 |
| `control.authority` | `UAVControlState.control_state` | **按 ROS 语义 0..3** | 线上 Struct 枚举是错的 |
| `control.controller` | `pos_controller` | 0..3 | — |
| `health.fcu_link` | `connected` | — | **不是**地面站链路 |
| `health.gs_link` | Heartbeat 到达时间 | 3 s 内收到则为 true | — |
| `health.odom_valid` / `loc_source` | `odom_valid` / `location_source` | 枚举 0..12 | — |
| `health.failsafe` | `UAVControlState.failsafe` + 最近一条 ERROR 级 TextInfo | 按文本匹配得到 reason | 例如 "Out of the geo fence" 对应 GEOFENCE |
| `health.companion` | Heartbeat.message | 按 `,` 和 `:` 解析 KV | CPU、温度、ROS 节点列表 |
| `flight_mode` | 无（Prometheus 没有任务层） | Gateway 根据当前生效的 DroneCommand 和 authority 推导 | §4.4 |
| `mission` | 无 | Gateway 维护 | — |
| 事件流 | `TextInfo{MessageType, Message}` | INFO/WARN/ERROR/FATAL 对应 UI toast 或 Alert | 只在变化时发送 |

### 4.3 `DroneCommand` 与 Prometheus 指令映射

```ts
type DroneCommand = { cmd_id: number; drone: string; source: "ui"|"mission"|"agent"|"swarm"|"safety";
                      priority: "NORMAL"|"OVERRIDE"|"RELEASE"; t_issue: number } & (
  | { type: "ARM" } | { type: "DISARM" } | { type: "TAKEOFF"; alt: number } | { type: "HOVER"; at: "HERE"|"HOME" }
  | { type: "LAND" } | { type: "RTL" } | { type: "KILL" }
  | { type: "GOTO"; target: Vec3; frame: "WORLD"|"BODY"; yaw?: number } | { type: "GOTO_LLA"; lat: number; lon: number; rel_alt: number; yaw?: number }
  | { type: "VELOCITY"; v: Vec3; frame: "WORLD"|"BODY"; yaw?: number; yaw_rate?: number; hold_alt?: number }
  | { type: "SETPOINT"; p: Vec3; v?: Vec3; a?: Vec3; yaw?: number }
  | { type: "FOLLOW_PATH"; waypoints: Vec3[]; speed: number } | { type: "ORBIT"; center: Vec3; radius: number; speed: number; cw?: boolean }
  | { type: "FORMATION"; shape: "LINE"|"TRIANGLE"|"SQUARE"|"CIRCLE"; size: number; leader: string }
  | { type: "ATTITUDE"; rpy: Vec3; thrust: number } | { type: "SET_PARAM"; params: Record<string, string|number|boolean> } );
```

| DroneCommand | Prometheus 实现（msg_id / 字段） | 备注 |
|---|---|---|
| 会话建立（隐式） | 202 `ModeSelection{mode:1, use_mode:0, selectId:[n]}` | 每次连接或重连都要发 |
| `ARM` / `DISARM` | 109 `UAVSetup{cmd:0, arming:true/false}` | DISARM 只能在地面执行（PX4 规则） |
| `TAKEOFF{alt}` | ① 110 `ParamSettings{param_module:1, params:[{param_name:"/uav_control_main_N/control/Takeoff_height", param_value:alt, type:4}]}` ② 109 `UAVSetup{cmd:3, control_state:"COMMAND_CONTROL"}`（要求已解锁）③ 108 `UAVCommand{Agent_CMD:1}` | 等价于 NO_RC 脚本 `arm_and_command.sh` 加上 Init_Pos_Hover |
| `HOVER{HERE}` / `{HOME}` | 108 `Agent_CMD:2` / `Agent_CMD:1` | — |
| `LAND` | 108 `Agent_CMD:3` | 进入 LAND_CONTROL，由 PX4 AUTO.LAND 执行 |
| `RTL` | 109 `UAVSetup{cmd:1, px4_mode:"AUTO.RTL"}` | 会绕过 Prometheus 状态机，之后要重新 SET_CONTROL_MODE |
| `KILL` | **地面站协议不提供**（只能用 RC ch7） | 真机上 UI 置灰；Mock 和 rosbridge 可以实现 |
| `GOTO{WORLD}` | 108 `{Agent_CMD:4, Move_mode:0 XYZ_POS, position_ref:T⁻¹·p, yaw_ref}` | Gateway 做 World 到机体局部坐标的逆变换 |
| `GOTO{BODY}` | 108 `{Move_mode:3, position_ref:d, Command_ID:++}` | **ID 必须递增** |
| `GOTO_LLA` | 108 `{Move_mode:8, latitude, longitude(float64), altitude:rel_alt}` | 高度是 REL_ALT |
| `VELOCITY{WORLD}` | 108 `{Move_mode:2, velocity_ref, yaw_ref \| Yaw_Rate_Mode+yaw_rate_ref}`；设了 `hold_alt` 时用 `Move_mode:1` | 零速轴会被锁位 |
| `VELOCITY{BODY}` | 108 `{Move_mode:4 或 5, Command_ID:++}` | — |
| `SETPOINT` | 108 `{Move_mode:6 TRAJECTORY, p, v, a}` | PX4_ORIGIN 下 a 会被丢掉；只建议在 rosbridge 下流式发送 |
| `FOLLOW_PATH` / `ORBIT` | Gateway 生成：地面站协议下用 XYZ_POS 航点序列（到点距离 < 0.5 m 时切下一点）；rosbridge 下 50 Hz 流式 TRAJECTORY | §3.8 |
| `FORMATION` | Gateway 用 separation 计算每机 GOTO；V1.0 可以对接 101 SwarmCommand（需要 swarm_control 模块） | §3.9 |
| `ATTITUDE` | 108 `{Move_mode:7, att_ref}`，要求 `enable_external_control=true` | 只在调试时开放 |
| `SET_PARAM` | 110 ParamSettings（`param_name` 必须包含 `/uav_control_main_N/`） | 例如 geofence、速度上限 |
| `priority: OVERRIDE / RELEASE` | `Control_Level: 1 / 2` | 对应 "Safety Stop" 按钮：`ABSOLUTE + Current_Pos_Hover` |

### 4.4 模式映射（统一 `FlightMode` 与 Prometheus 控制权）

| 我们的 `FlightMode` | authority | 由哪个指令进入 | UI 标签 / 颜色 token |
|---|---|---|---|
| `IDLE` | INIT（未解锁） | — | Ready，灰色 |
| `TAKEOFF` | COMMAND | Init_Pos_Hover，且 z < Takeoff_height − 0.2 | Takeoff，白色 |
| `HOVER` | COMMAND | Current_Pos_Hover，或到达目标 | Hover，白色 |
| `GOTO` / `FOLLOW_PATH` / `ORBIT` / `FORMATION` | COMMAND | Move 系列 | Mission，白色高亮 |
| `MANUAL` | RC_POS（MANUAL） | 虚拟摇杆 | Manual，灰色 |
| `LAND` / `RTL` | LAND / PX4 AUTO.RTL | Land / RTL | Landing，灰色 |
| `EMERGENCY` | LAND + failsafe，或 ABSOLUTE 锁 | failsafe / Safety Stop | Alert，**红色 #E93024** |

### 4.5 Simulation Gateway 对接 Prometheus 的方案

```text
                ┌──────────────────── Simulation Gateway (FastAPI + asyncio) ────────────────────┐
 Browser ⇄ WS ⇄ │ Session/Auth → CommandArbiter(优先级: safety>ui-override>ui>agent>mission>swarm) │
 (二进制 20Hz)  │      │                         ▲ DroneState bus (per-drone ring buffer, 100Hz 内部)   │
                │      ▼                         │                                                    │
                │ SimBackend 接口: connect / send(DroneCommand) / states() / capabilities           │
                │  ├─ MockBackend        (V0.1) 本文 §3.3–3.5，内置 100Hz 物理 + 环境场 E(x,y,z,t)    │
                │  ├─ PrometheusGSBackend(V0.2 SITL / V0.5 真机) 地面站协议 §3.1–3.2，无 ROS           │
                │  ├─ RosbridgeBackend   (V0.6) roslibpy ↔ rosbridge_websocket(ROS1 docker)           │
                │  │      订阅 /uavN/prometheus/{state,control_state,text_info}, /uavN/planning/bspline │
                │  │      发布 /uavN/prometheus/command (50Hz 流式), /uavN/prometheus/motion_planning/goal│
                │  ├─ Px4MavsdkBackend   (V0.2 备选) MAVSDK udp://:14540+ID，控制权 FSM 在 Gateway 内   │
                │  └─ ReplayBackend      (V0.2) MCAP/ulog 回放                                         │
                │ FrameService: World ENU 锚点 ↔ 每机 local（GPS: ECEF→ENU 平移；SLAM: 配准；SITL: spawn）│
                └────────────────────────────────────────────────────────────────────────────────────┘
```

**分阶段落地**

| 阶段 | 后端组合 | 验收 |
|---|---|---|
| V0.1 | MockBackend（N ≤ 50，100 Hz），WS 二进制 20 Hz | 在 UrbanScene3D 场景里完成起飞、GoTo、Orbit、降落；failsafe 注入后 UI 能正确告警 |
| V0.2 | 加上 `PrometheusDroneEmulator`（Mock 伪装成机载端，监听 55555，发往 8889 和 55556）和 `PrometheusGSBackend`，做回环 CI；另外可选 ROS1 docker（`ros:noetic` + Prometheus + PX4 SITL，每机 offboard 端口 14540+ID），用 `simulation_bridge.launch`（is_simulation=1）代理多机 | 同一套 UI 操作在 Mock、Emulator、SITL 三种后端下行为一致（状态序列与事件完全相同） |
| V0.5 | 真机 P600：机载端运行 `bridge.launch`（真机模式，一机一桥），Gateway 连 N 个 IP；World 锚点与 RTK 配准 | 心跳监控、单一控制方、Safety Stop 可用；lat/lon 不作为几何来源 |
| V0.6 | 加上 RosbridgeBackend 和 EGO-Swarm（上游版本加 Prometheus 的 traj_server）：World 点云**按任务区裁剪**后以 0.3 m 体素发成 `global_cloud` | 多机规划轨迹（MultiBsplines）在 Web 端渲染 |

**Gateway 的关键规则**

1. **单一控制方**：Gateway 是唯一连接机载端 55555 的地面站。多个浏览器会话由 Gateway 内的 `CommandArbiter` 仲裁，不允许浏览器直连机载端。
2. **心跳**：Gateway 的 55556 监听器必须和服务同进程启动。退出时，先按策略给所有在空中的飞机发 `Current_Pos_Hover`（或 Land），再关闭。"Gateway 挂了真机会自己 Land"写进运维手册。
3. **Command_ID**：每机一个 u32 单调计数器，重连后不能归零（取 `max(本地, 最近一次 UAVState 回显的 ID) + 1`）。
4. **频率**：机载端的 `uav_basic_hz` 建议设为 20，UI 就能拿到 20 Hz。设为 0 时是 50 Hz，但 UDP 流量会翻倍（每帧 456 B）。
5. **路径类指令不走 55555 流式发送**：每条都要新建 TCP 连接，20 Hz × N 架会产生大量 TIME_WAIT 连接。改为航点序列，或者用 rosbridge。
6. **坐标**：只用 `position` 加 `T_world←local_i`；`latitude/longitude` 只用于显示；高度保留椭球高和相对高两个字段。
7. **事件**：TextInfo 映射到 UI 通知；ERROR/FATAL 自动置顶，用红色描边。

---

## 5. 对比与推荐

本单元只有一个仓库。下表把它放在 sim 组的横向对比里，只看与本项目的契合度，其他仓库的细节见对应单元：

| 维度 | Prometheus | PX4 + MAVSDK 直连 | XTDrone / XTDrone2 | mrs_uav_system |
|---|---|---|---|---|
| 与 P600 真机一致性 | **最高**（就是机载软件本身） | 中（缺少 Prometheus 状态机和定位源） | 低 | 低 |
| 我方依赖 | 地面站协议：无；rosbridge：ROS1 docker | MAVSDK-Python | ROS1/ROS2 + Gazebo | ROS1 |
| 控制语义丰富度 | 高（控制权 FSM、Control_Level、机体系、经纬度、failsafe） | 中（offboard + action） | 中 | 高 |
| 2026 活跃度 | 低（2025-11 后无提交） | 高 | 中 / 中 | 中 |
| star | 3.3k（本单元实测） | 见 PX4 / MAVSDK 单元 | 见 XTDrone 单元 | 见对应单元 |
| 适合的阶段 | V0.2 SITL 对齐、V0.5 真机 | V0.2 纯 SITL | 教学参考 | 参考 |

**推荐排序**：
1. Prometheus 的**协议、契约、控制语义**，作为 P600 对齐的唯一依据，必须采用；
2. PX4 + MAVSDK，作为不依赖 ROS 的 SITL 后端；
3. EGO-Swarm 采用上游版本，只在 V0.6 使用；
4. 其余只作参考。

Prometheus 内部子模块的优先级：`communication` 等于 `prometheus_msgs` 等于 `uav_control`，其次 `simulator_utils`，再次 `tutorial_demo`，然后 `motion_planning`，最后 `ego_planner_swarm`（fork）和 `Simulator`。

---

## 6. 风险与注意事项

| 风险 | 影响 | 应对 |
|---|---|---|
| **ROS1 Noetic 已于 2025-05 EOL**，只支持 Ubuntu 20.04 | 无法在本机或新系统原生构建 | 我方不装 ROS；SITL 和 rosbridge 统一用 `ros:noetic` docker；本机没有 GPU，Gazebo 只能 headless 运行，而且很慢，所以 MVP 只用 Mock |
| 闭源 `libcommunication_*.so`（只有 x86_64 和 aarch64），`PROTOCOL_VERSION 1` **不在帧里** | 阿木升级协议时我们会静默失配 | CI 里保留 `.cache/research/r19` 的 C++ 对拍程序（链接 .so）作为契约测试；帧里的 `topic_name` 当作版本线索 |
| `control_state` 枚举不一致（ROS 0..3，Struct 0..4） | 状态显示错误 | 固定按 ROS 语义解码，并写契约测试 |
| **lat/lon 是 float32** | 0.2–0.7 m 量化误差 | 几何只用 ENU position；UAVCommand 的经纬度是 float64，下发不受影响 |
| TRAJECTORY 在 PX4_ORIGIN 下丢加速度前馈 | 跟踪有滞后 | 高速轨迹改用内置 PID 控制器（`pos_controller=1`）或 rosbridge 流式发送；Mock 保留前馈 |
| 地面站协议每条指令一次 TCP 连接 | 不适合高频流式发送；连接失败会增加延迟 | 1 s 超时，失败重试 1 次；路径类指令改为航点序列 |
| 心跳丢失后真机 **5 s 自动 Land** | Gateway 重启或网络抖动会导致降落 | Gateway 高可用（进程守护、55556 优先启动）；UI 显示"链路倒计时" |
| **`CUSTOMMODE` 会直接 `system(cmd)`**，而且第一个连上的地面站就是控制方 | 同一网络内任何人都可能在机载电脑上执行命令，或抢走控制权 | 机载网络隔离，只放行 Gateway 的 IP；Gateway 白名单只允许 202 的 UAVBASIC，**禁止** CUSTOMMODE、REBOOTNX、EXITNX；Web 端不暴露原始协议 |
| RC 失联判定（非 sim 时 1.5 s） | 真机没有遥控器时进入 COMMAND 后会立即 LAND | 真机必须开着遥控器（也是安全要求）；UI 在 `sim=false` 时提示 |
| EGO fork 写死 `/uav1`；grid_map 整块预分配，每体素 15 B | 多机串线；城市场景内存爆（200×200×60 m@0.15 m = 9.9 GiB） | 每机 ns + remap；地图按任务区裁剪，分辨率 0.3–0.5 m；优先用上游版本 |
| fake_odom 链路缺 `mavros/state` | Prometheus 自带的"无 PX4 仿真"跑不通 | 不依赖这条链路；我们的 Mock 完全在 Gateway 内 |
| p600.sdf 参数是 Iris 级 | 动力学与真机偏差大 | 只作占位；V0.4 前用 ulog 辨识 m、kf、τ |
| LAND 的 workaround：`MC_YAWRATE_MAX` 置 0 | 降落时机头锁死；参数恢复失败会影响后续飞行 | Gateway 在 LAND 结束后读回参数做校验 |
| 4 个子模块为空（swarm_control 等） | 集群、搜索的实现看不到 | 只借用 `SwarmCommand` 语义；集群逻辑自研（Gateway 内） |
| 内置 PID 积分用 1/200，而循环是 100 Hz | 积分增益减半 | 移植时用真实 dt |
| MID360 定位源订阅的 `/Odometry` 没有 namespace | 多机 LIO 串线 | launch 里 remap |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§2、§33 技术栈更正**：
   - P600 现役的 Prometheus 是 **ROS1 Noetic + mavros + PX4**，地面站走私有的 **TCP/UDP + JSON** 协议（帧格式见 §2.3）。建议把"Message: ROS2/DDS"改成"机载 ROS1（Prometheus）；平台内部统一 DroneState/DroneCommand；ROS2 仅作为未来可选后端"。
   - §2 里的 `:chatgpt-content-reference{…}` 残留标记要清除。
2. **§26、§35 仿真后端分层**：
   - 把"PX4 SITL + Gazebo"从 MVP 必需项降为 V0.2 的可选后端。MVP 用 **Mock（移植 fake_uav + PX4 级联）**，它和真机共用同一套控制权 FSM 与指令语义。本机没有 GPU，而 1000 架 Mock 单核就能跑 100 Hz 物理。
   - 新增 `SimBackend` 接口和 5 种实现（§4.5），落实"UI 不因后端切换而重做"的原则（§4.1）。
3. **§28 DroneState 补全**：
   - 按 §4.2 增加 `control.{authority, level, last_cmd_id, last_cmd_result}`、`health.{fcu_link, gs_link, odom_valid, loc_source, failsafe}`、`autopilot_mode` 与 `flight_mode` 分离、`geo.alt_ellipsoid/rel_alt`、`seq/t`。
   - **明确坐标系、单位和四元数顺序**（World ENU、FLU、[x,y,z,w]），这一点原文没有写。
   - 原文的 `gps` 应该拆成"几何（ENU）"和"展示（LLA，float64）"两部分。
4. **§30 控制模式补全**：
   - 在任务级模式下面加一层 **控制权层**（INIT/MANUAL/COMMAND/LAND）和 **优先级**（DEFAULT/OVERRIDE/RELEASE），对应 Prometheus 的 `Control_Level`。
   - 第一阶段补上 `SafetyStop`（ABSOLUTE 悬停）、`Pause/Resume`、`VelocityControl`（WASD，带零速轴锁位）、`GotoLLA`。
   - `FollowPath` 和 `Orbit` 明确由 Gateway 生成（Prometheus 没有原生支持）。
5. **§29 多机**：
   - 写明 SITL 端口规则（offboard 14540+ID，sysid = ID+1，每机 namespace `/uavN`）。
   - 多机坐标**统一到 World ENU 锚点**，而不是 Prometheus 的"1 号机坐标系"。每机维护 `T_world←local_i`，按 GPS、SLAM、SITL 三种来源分别求取。
6. **§36 数据流更正与安全**：
   - 真机链路是 `PX4 → mavros(ROS1) → Prometheus uav_control → communication_bridge ⇄(TCP 55555 / UDP 8889 / TCP 55556) ⇄ Simulation Gateway → WebSocket → Browser`。
   - 新增"**链路与安全**"一节：单一控制方、心跳（5 s 后自动 Land）、禁止透传 CUSTOMMODE、网络隔离、指令 ACK（ACCEPTED/REJECTED/IGNORED）。
7. **§37 频率**：
   - 补上 Prometheus 的实际频率：控制 100 Hz，UAVState 50 Hz，地面站转发默认 10 Hz（建议调到 20 Hz）。
   - WebSocket 默认 20 Hz 二进制帧（每机 40 B，100 架 30 Hz 约 118 KiB/s），**前端用 v 做 Hermite 插值渲染到 60 FPS**，选中的飞机可以提到 50 Hz。
8. **§27 P600 Digital Twin**：增加"参数来源与置信度"表（SDF 初值和真机辨识结果对照），以及 `vehicles/p600/params.yaml`（质量、惯量、kf/km、臂长、τ_motor、τ_att、推重比、`MPC_*` 限幅、`Takeoff_height` 等）。
9. **§22–§24、§47 环境到动力学**：接入点统一定义为加速度项 `c_d·(W(p,t) − v)/m`（Mock），V0.4 换成 `F_ext/M_ext`（刚体 ODE）。环境场查询接口按 SoA 向量化（一次传入 N 个位置），否则 1000 架乘以 100 Hz 的逐点查询会成为瓶颈。
10. **§31–§32、§50 ANet / 任务**：直接借用 `SwarmCommand` 的任务谱系（Formation / Follow / Search 矩形 / Attack 点 / Hold / Stop）和 `MultiDetectionInfo` / `Target` 字段，作为 capability 和发现事件的初版 schema；UGV 的 `UGVCommand` 作为 V1.0 异构 Agent 的第一个非无人机实例。
11. **§38–§40 UI**：
    - 右侧 DRONES 卡片增加控制权徽章（INIT/MANUAL/COMMAND/LAND）、链路状态（FCU/GS 两个点）、定位源和 odom 状态、failsafe 红色告警条；
    - 底部 Timeline 叠加 TextInfo 事件标记；
    - "接管"按钮对应 OVERRIDE，"释放"按钮对应 RELEASE。
12. **§43 MVP 范围**：MVP 用内置 UrbanScene3D 加 Mock 完成闭环；真实 P600 视频重建和 PX4 SITL 都移出 MVP 的关键路径。Demo 验收标准增加"在 Mock、Prometheus Emulator 两种后端下 UI 行为一致"。
