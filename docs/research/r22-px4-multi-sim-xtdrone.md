# R22 研究笔记：PX4 多机仿真编排
## PX4-Multiagent-Simulation / px4_multi_drone_sim / XTDrone / XTDrone2，以及 Simulation Orchestrator（V0.2 / V0.6）设计

> 研究单元：r22 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §26–§30、§33–§37、§45、§49、§50
>
> **仓库快照**（均为 shallow clone，下文路径都相对各仓库根目录）：
>
> | 仓库 | 本地路径 | ★ | 最后提交 |
> |---|---|---|---|
> | TannerGilbert/PX4-Multiagent-Simulation | `refs/sim/PX4-Multiagent-Simulation` @ `b1f44fe` | 11 | 2026-02-09 "Initial public release" |
> | AntonSHBK/px4_multi_drone_sim | `refs/sim/px4_multi_drone_sim` @ `35a6d01` | 26 | 2025-06-15 |
> | robin-shaun/XTDrone | `refs/sim/XTDrone` @ `8e88116` | 1725 | 2025-08-02 |
> | andy-zhuo-02/XTDrone2 | `refs/sim/XTDrone2` @ `15b2ef6` | 115 | 2025-08-02 |
>
> 核对端口、命名空间、SIH、容器镜像等上游事实时，还读了 `refs/sim/PX4-Autopilot` @ `b3e343c`（2026-09-27）的 `ROMFS/px4fmu_common/init.d-posix/{rcS,px4-rc.mavlink,px4-rc.gzsim,px4-rc.sihsim}`、`platforms/posix/src/px4/common/main.cpp`、`src/modules/simulation/simulator_sih/`、`Tools/packaging/containers/`。PX4 本体的详细研究归其他单元，本文只引用和编排有关的部分。
>
> **本机实测**。本机没有 ROS，也没有 GPU。实测用的是官方镜像 `px4io/px4-sitl:v1.18.0-rc1`（SIH 物理，152 MB），跑在独立的 docker 网络上。产物在 `.cache/research/r22/`，没有改动 `refs/`：
> - `mavlite.py`：零依赖的 MAVLink v1/v2 编解码，含 CRC 和 crc_extra，约 110 行。
> - `dockerapi.py`：通过 unix socket 调用 Docker Engine API。
> - `orchestrator.py`：`SimOrchestrator` 原型，负责生命周期、健康状态机、网关和 FrameService。
> - `experiment.py`：4 机和 10 机的全流程测试：拉起、就绪、起飞、Offboard 编队、坠毁注入、重启、销毁。
> - `stream_test.py`：MAVLink 流量裁剪。
> - `wind_test.py`：在运行中向 SIH 注入风。
> - `crc_check.py`：确认所有接收消息类型的 crc_extra 零错误（另外，COMMAND_ACK 也在主实验中校验过）。
> - 结果文件：`results_N4.json`、`results_N4_ff.json`、`results_N10_ff.json`、`stream_test.json`、`wind_test.json`。
>
> **测量条件**：宿主机 8 核，测试期间 load average 在 39–94 之间（其他工作流同时占用 CPU）。所有时延和实时率（RTF）都是**偏悲观**的数值。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **XTDrone**（ROS1 + Gazebo Classic + PX4 1.11–1.13）：`coordination/formation_demo`、`communication/`、`coordination/launch_generator` | 国内使用最广的多机教学/科研平台，分层架构最完整 | **port**（只移植算法：KM 编队重分配、consensus 编队律、切向避碰、SINR 链路模型、指令词汇、端口方案）+ **reference**（架构分层）；**不运行** ROS1 栈 | V0.6（编队/避碰）/ V1.0（SINR、任务分配） | ★★★★☆ |
| XTDrone：`sitl_config/models/livox_mid40/scan_mode/mid360.csv` | Livox MID-360 非重复扫描模式，共 80 万条射线 | **adopt**（直接作为数据资产）：用于 LiDAR 仿真和 SensorLayer 的 FOV 可视化 | V0.5 / V0.6 | ★★★★☆ |
| **PX4-Multiagent-Simulation**（ROS2 Humble + gz Harmonic + PX4 1.15，2026-02） | 现代栈的多机拉起脚手架，支持 xacro 传感器宏、外部里程计，以及摄影测量模型 → Gazebo 世界 | **reference + port**：采用它的传感器宏思路、World 导出步骤和 EKF 参数档；launch 端口公式**不要照搬**（有误） | V0.6（gz 传感器路径）/ V0.5（外部里程计） | ★★★☆☆ |
| **XTDrone2**（ROS2 Jazzy + gz Harmonic + PX4 1.15） | XTDrone 的 ROS2 重写，用 YAML 描述机队，通信节点走 uXRCE-DDS | **reference**：YAML schema、OFFBOARD_STATE 语义、FLU↔NED 公式；代码不能直接用（多机指令会被 PX4 静默丢弃，见 §2.4） | V0.2 / V0.6 | ★★☆☆☆ |
| **px4_multi_drone_sim**（ROS2 Humble + gz Harmonic + PX4 1.15，Docker） | Docker 开发容器，JSON "G-code" 指令 DSL，State 模式控制器 | **port**：G22/G23/G24 轨迹原语（需要修正，见 §3.7），指令队列和中断语义，local↔global 坐标变换思路；**Docker 方案不采用**（单体镜像过大） | V0.1（Mock 任务原语）/ V0.2 | ★★☆☆☆ |
| （补充）官方 `px4io/px4-sitl`（PX4 `Tools/packaging/containers/Dockerfile.sih`，v1.18，2026） | 无 Gazebo 的 PX4 SITL（SIH 物理），CPU 即可运行 | **adopt**：V0.2 SITL 运行时的首选。本文已实测"每机一个容器"的编排 | V0.2 / V0.4 / V0.6 | ★★★★★ |

**关键结论（实现者先读这几条）**

1. **V0.2 不应该从 "PX4 + Gazebo" 起步，应该从 "PX4 SIH 容器 + 我们自己的 World Runtime" 起步。**
   - SIH 在 PX4 进程内部完成刚体动力学、GPS、气压计和磁力计仿真，不需要 Gazebo、GPU 和 ROS。
   - 实测每架约占 **0.17–0.25 个 CPU 核**、**7–19 MB 内存**；从启动容器到收到首个 HEARTBEAT 用 **3.4–6.0 s**，到 READY（pre-arm 通过且有全局位置）用 **6.5–11.7 s**。
   - 世界几何、传感器和环境场都留在我们自己的 World Runtime 里，Gazebo 推迟到 V0.6 的"GPU 传感器仿真"再引入（§4.2、§7）。
2. **编排拓扑建议用"每机一个容器，每个容器都用 `px4 -i 0`，再加一个 MAVLink 网关端口"（已实测 N=4 和 N=10）。**
   - 每架机都有独立的网络命名空间，所以不需要端口算术，也不会触发上游 `px4-rc.mavlink` 中"实例号大于 9 时 offboard 端口统一回落到 14549"的规则。
   - 各机的 `/tmp/px4_lock-<i>` 和 `/tmp/px4-sock-<i>` 天然隔离。
   - 用 `PX4_PARAM_MAV_SYS_ID` 区分 sysid。
   - 容器的 `ExtraHosts: host.docker.internal:<gateway_ip>` 会触发官方 entrypoint 改写 MAVLink 和 DDS 的目标地址，于是所有飞机都把遥测发到网关的 **同一个 UDP 14540**，网关再按 sysid 解复用。
3. **出生位姿必须从 World 锚点推导，并由 FrameService 统一坐标。**
   - SIH 用 `PX4_HOME_LAT/LON/ALT/YAW` 决定出生位姿；Gazebo 用 `PX4_GZ_MODEL_POSE`，同时世界文件的 `<spherical_coordinates>` 要等于锚点。
   - 实测结果：出生点误差 ≤ 0.39 m（这是 GPS 仿真噪声）；**每架机的 `LOCAL_POSITION_NED` 原点都在它自己的出生点**（|x|、|y| ≤ 0.05 m）。
   - 因此多机协同（编队、避碰）**只能在 World ENU 下计算**，不能直接用 local 坐标。px4_multi_drone_sim 的 `DroneLocalityState` 和 XTDrone 的 `uav_bias` 都是在补这个问题。
4. **健康检查分四级，阈值已实测。**
   - L0：容器或进程存活。
   - L1：HEARTBEAT 超过 2.5 s 没来记为 DEGRADED，超过 5 s 记为 LOST。实测 SIGKILL 后 **2.0 s 检出 DEGRADED，4.4 s 检出 LOST**。
   - L2：READY，条件是 `SYS_STATUS.health` 的 bit28（pre-arm）为 1，并且 GLOBAL_POSITION_INT 有效。
   - L3：RTF = Δ`time_boot_ms` / Δwall。SIH 是 lockstep 的，CPU 不够时仿真时间会变慢，负载 94 时实测 RTF 为 0.77。
   - 对同一容器执行 `start` 即可原地重启，**6.7–9.1 s 回到 READY**。
5. **Offboard 有硬约束。**
   - 切换前要预先发送 setpoint，频率大于 2 Hz。实测预发 1.5 s、10 Hz 后，`DO_SET_MODE(1,6)` 收到 ACK 0。
   - 一旦停发，PX4 触发 failsafe 并执行 RTL（实测 STATUSTEXT：`Failsafe activated` → `RTL: start return ...`）。
   - 所以网关必须持续发送 setpoint 保活；或者在停发前先切回 HOLD。
6. **遥测流量必须裁剪。**
   - PX4 onboard 链路默认每架 **414.6 msgs/s**（其中 ATTITUDE 96.6 Hz）。
   - 用 `MAV_CMD_SET_MESSAGE_INTERVAL(511)` 裁剪后降到 **70.6 msgs/s（−83%）**。
   - 纯 Python 解析在负载下单核约 **3.6 万帧/s**：不裁剪时约 87 架就会占满一个核，裁剪后单核可支撑 500 架以上。
7. **XTDrone 编队律在 PX4 SITL 上复现成功。**
   - 4 机、领机 1 m/s：只用 P 控制时平均误差 1.28 m；**加上领机速度前馈后降到 0.55 m**（最大 0.97 m），最小机间距 ≥ 2.2 m。
   - 10 机从 54 m 横队出发，约 27 s 收敛成 V 形。
   - KM 分配已改写成 O(n³) 的 Hungarian 并与穷举对拍通过。
8. **环境到动力学的耦合（V0.4）可以直接用 SIH 实现（已实测）。**
   - 运行时用 `PARAM_SET SIH_WIND_E` 注入风：5 m/s 时横滚 27.5°（理论 27.0°），10 m/s 时 45.0°（理论 45.5°）。
   - 局限一：SIH 每 **1 s** 才读一次参数更新，所以只能做 ≤1 Hz 的准静态风。
   - 局限二：SIH 默认 `SIH_MASS=1 kg`、`SIH_KDV=1 N/(m/s)`，风阻偏大，P600 数字孪生必须重新标定（§3.10）。
9. **四个仓库都有可以复现的缺陷（§2、§6），只能当"思路来源"，不能当依赖。** 最严重的两个：
   - XTDrone2 把 `VehicleCommand.target_system` 写死为 1，导致第 2 架及以后的 ARM/TAKEOFF/OFFBOARD 被 PX4 commander 静默丢弃（`Commander.cpp:865`）。
   - PX4-Multiagent-Simulation 的 MAVROS 端口公式与 PX4 rcS 不一致：i=1 时绑定到 14550，和 QGC 冲突。

---

## 1. 仓库概览

| 维度 | PX4-Multiagent-Simulation | px4_multi_drone_sim | XTDrone | XTDrone2 |
|---|---|---|---|---|
| 作者/背景 | Gilbert Tanner（个人，奥地利 Modelflughafen 数据） | Anton Pisarenko（博士论文项目，俄） | 北大王祥科组 + 社区，论文 ICRAS 2020 / arXiv:2005.01125 | 北大卓安（XTDrone 继任） |
| 仿真栈 | ROS2 Humble + **gz Harmonic**（gz-sim8）+ PX4 **1.15** | ROS2 Humble + **gz Harmonic** + PX4 **v1.14.4/1.15.2** | **ROS1** Melodic/Noetic + **Gazebo Classic** + PX4 **1.11–1.13** + MAVROS | ROS2 **Jazzy** + **gz Harmonic** + PX4 **1.15** + uXRCE-DDS |
| 与 PX4 的通信 | MAVROS（默认）或 Micro XRCE-DDS 二选一 | uXRCE-DDS（px4_msgs） | MAVROS（`/iris_i/mavros/*`） | uXRCE-DDS（px4_msgs） |
| 多机拉起方式 | `launch/multiagent_simulation.launch.py` 读取 `config/robots.yaml` | `launch/test_multi_drone_run.launch.py` 读取 `config/test_params.yaml` | `coordination/launch_generator/generator.py` 交互式生成 roslaunch XML | `xtd2_launch/launch/yaml_demo_launch.py` 读取 `launch_config/default.yaml` |
| 传感器挂载 | xacro 宏：`models/{lidar,camera,depth_camera,rgbd_camera}/model.xacro`，用 `<include merge>` 组合 | 无（直接用 PX4 自带的 x500） | 52 个 SDF 模型，其中 Livox avia/mid40/mid70 使用 CSV 扫描模式 | PX4-gazebo-models 的副本（x500_depth、lidar_2d 等） |
| Offboard 样例 | `control_drone.py`（pymavlink + customtkinter GUI） | `OffboardCommander`（TrajectorySetpoint 10 Hz） | `communication/multirotor_communication.py`（setpoint_raw 30 Hz） | `xtd2_communication/multirotor_communication.py`（20 Hz） |
| 编队/协同 | 无 | 无（只有集中式 CCU 的文档设想） | leader-follower、consensus、KM 重分配、避碰、SINR、MPI 并行 | 只有 `ros2/`，从 XTDrone 移植（未完成） |
| Docker | 无 | `docker/Dockerfile`（Ubuntu 22.04 + ROS2 desktop-full + gz + 在镜像内编译 PX4），外加 compose | 无 | 无 |
| 文件规模 | 98 个文件 / 229 MB（主要是摄影测量贴图） | 104 / 59 MB | 4061 / 2.6 GB（sensing 占 1.6 GB） | 221 / 69 MB |
| 成熟度 | 首次公开发布，Limitations 自述"MAVROS 与 XRCE 不能并存" | "active development"，自带 bug（§2.2） | 成熟，但停在 ROS1 时代 | README 自述"前期开发迭代"，安装文档只有标题 |

---

## 2. 源码结构与关键模块

### 2.1 PX4-Multiagent-Simulation

```text
src/
├── gz_plugins/plugins/pose_sensor.cc            # gz-sim8 System：带噪声的位姿传感器
├── multiagent_simulation/
│   ├── launch/multiagent_simulation.launch.py   # 唯一入口（OpaqueFunction launch_setup）
│   ├── config/robots.yaml                       # 机队清单
│   ├── config/{gps_mode,external_odometry_mode}.params   # 992 行 QGC 参数档
│   ├── config/22000_gz_x500_with_lidar_and_camera        # 自定义 airframe（source 4001_gz_x500）
│   ├── config/multiagent_lidar_camera_bridge.yaml        # ros_gz_bridge 模板（<robot_name> 占位）
│   ├── models/{lidar,camera,depth_camera,rgbd_camera}/model.xacro  # 传感器宏
│   ├── models/x500_lidar_and_camera/model.sdf   # <include merge="true"> 组合 x500 + 传感器 + PoseSensor
│   ├── multiagent_simulation/control_drone.py   # pymavlink GUI（arm/takeoff/offboard/move/land）
│   ├── multiagent_simulation/external_odometry_publisher.py  # nav_msgs/Odometry → VehicleOdometry
│   └── worlds/{modelflughafen/,baylands,rubico,...}.sdf
└── (submodules) ros_gz@humble, sdformat_urdf, px4_msgs@release/1.15, px4_ros_com
```

**launch 流程**（`launch_setup()`）如下：

1. 依次用 xacro 把 4 个传感器模板转成 SDF，通过 `RegisterEventHandler(OnProcessExit)` 串起来。
2. 把 `worlds/`、`models/` 追加到 `GZ_SIM_RESOURCE_PATH` 和 `SDF_PATH`，把插件目录追加到 `GZ_SIM_SYSTEM_PLUGIN_PATH`。
3. 通过 `ros_gz_sim/gz_sim.launch.py` 启动 `gz sim -v0 -s -r <world>`（server），`gui:=true` 时另启 `-g`。
4. 对每个 robot 执行：
   - `ExecuteProcess(./build/px4_sitl_default/bin/px4 -i i)`，环境变量为 `PX4_GZ_STANDALONE=1`、`PX4_SYS_AUTOSTART=<px4_id>`、`PX4_GZ_MODEL_POSE=x,y,z,r,p,y`、`PX4_GZ_WORLD=default`。注释写明：**world 名必须与 SDF 里的 `<world name>` 一致，否则 spawn 会阻塞**。
   - 使用 XRCE 时，延迟 10 s 启动 `MicroXRCEAgent udp4 -p 8888+(i-1)*10`。
   - 使用 MAVROS 时，`fcu_url=udp://:{14540+10i}@localhost:{14557+10i}`，`tgt_system=i+1`，namespace 为 `px4_i/mavros`；30 s 后用 `mav param load -qgc` 加载参数档。
   - 按 robot 分别启动 `robot_state_publisher`、`ros_gz_bridge`（用 `replace_robot_name()` 生成临时 yaml）、`ros_gz_image`（三路相机），以及可选的 `topic_tools relay gz/tf→tf` 和 RViz。
   - 当 `use_micro_xrce` 为真时，`i` 从 1 开始，原因是"0 号实例没有 namespace"（与 PX4 ≤1.18 的 rcS 行为一致）。

**传感器挂载**（`models/lidar/model.xacro`）使用宏 `lidar_sensor(name, pose, horizontal_fov, vertical_fov, horizontal_samples, vertical_samples, update_rate)`：

- 宏生成一个 `gpu_lidar` 传感器，量程 0.5–100 m、分辨率 0.01 m，外加 fixed joint。
- `x500_lidar_and_camera/model.sdf` 用 `<include merge="true"><uri>model://lidar</uri></include>` 把它并入机体。
- 配套的 airframe 文件只有两行：`PX4_SIM_MODEL=${PX4_SIM_MODEL:=x500_lidar_and_camera}` 和 `. ${R}etc/init.d-posix/airframes/4001_gz_x500`。

这就是"**传感器配置 → SDF 生成 → airframe 垫片**"的最小套路，我们 V0.6 的 SensorSpec 可以直接照搬这个结构（§3.9）。

**PoseSensorPlugin**（`pose_sensor.cc`）：
- `Configure()` 解析多个 `<sensor>`，在 `/model/<model>/pose_sensor/<name>` 上发布；`PostUpdate()` 取 `worldPose(link)`，加上偏移和高斯噪声后发布。
- ⚠ 它把噪声**逐分量直接加到四元数上且没有归一化**，结果不是单位四元数。移植时应改成"对旋转向量加噪再指数映射"。

**GPS 模式与视觉模式参数档的差异**（`diff` 结果，是外部定位最有用的参数集）：

| 参数 | gps_mode | external_odometry_mode | 含义 |
|---|---|---|---|
| `EKF2_GPS_CTRL` | 7 | **0** | 关闭 GPS 融合 |
| `EKF2_EV_CTRL` | 15 | **11** | 融合 EV 水平位置、垂直位置和 yaw，不融合 EV 速度 |
| `EKF2_HGT_REF` | 1 (GPS) | **3 (Vision)** | 高度基准 |
| `EKF2_BARO_CTRL` | 1 | **0** | 关闭气压计 |
| `EKF2_EV_DELAY` | 0 | **29 ms** | 视觉延迟补偿 |
| `EKF2_EV_NOISE_MD` | 0 | **1** | 使用参数噪声，不用消息自带的协方差 |
| `EKF2_EVP_NOISE` / `EKF2_EVP_GATE` | 0.1 / 5 | **0.01 / 10000** | 位置噪声和门限（10000 相当于不做门限） |
| `EKF2_MAG_TYPE` | 0 | **5 (None)** | 不用磁力计 |
| `EKF2_RNG_CTRL` | 1 | 0 | — |

V0.5 用 MID-360 + FAST-LIO 给 PX4 提供外部定位时，可以把这组参数当起点。

**自定义世界文档**（`doc/Create_custom_world.md`）写的是摄影测量（WebODM）→ Blender → Gazebo 的流程，要点有三条：
- 模型原点放在地面 z=0，并校核尺度。
- `<visual>` 用高模，`<collision>` 用简化网格。
- `<world>` 里写 `<spherical_coordinates>`（`EARTH_WGS84`、`ENU`、lat/lon/elevation/heading），这样 QGC 地图航点能和摄影测量世界对齐。

这正是我们 World Package → Gazebo 导出的规范（§4.1、§7）。

**control_drone.py**：
- 用 pymavlink 连接 `udp:127.0.0.1:14550`，`source_system=42`。
- 用 `LOCAL_POSITION_NED` 和 `ATTITUDE` 维护状态；以 20 Hz 发送 `SET_POSITION_TARGET_LOCAL_NED`，type_mask 表示只用位置和 yaw。
- 进入 offboard 前先预发 30 帧（每帧间隔 0.05 s），再发 `MAV_CMD_DO_SET_MODE(custom, main=6)`。
- 局限：`master.target_system` 取的是**第一个收到的心跳**，因此只能控制一架；land 靠把 `target_z` 设为 0 实现，属于伪降落。

**缺陷清单（本仓库）**

1. MAVROS 端口公式与 PX4 不一致。PX4 实例 i 的 offboard 链路是"本地 14580+i、发往 14540+i"，launch 却写 `bind 14540+10i`、`send 14557+10i`：
   - i=0 时，MAVROS 能收到遥测，发出的包要靠 libmavconn"从收到的包学习远端端口"才能到达 PX4。
   - i=1 时绑定 **14550**，这是 GCS 端口，所有实例的 GCS 链路都往这里发，结果流量混杂，还和 QGC 冲突。
2. 每架启动一个 XRCE Agent，端口 `8888+(i-1)*10`，但 PX4 客户端默认都连 8888（除非设置 `PX4_UXRCE_DDS_PORT`），所以除第一个外其余 agent 空转。**一个 agent 可以服务多个客户端**（靠 `UXRCE_DDS_KEY=i+1` 区分），XTDrone2 的写法才是对的。
3. `multiagent_lidar_camera_bridge.yaml` 中 `imu_zero_noise` 和 `imu_with_noise` 写死了 `x500_lidar_and_camera_1`，多机时串台。
4. `move_airframe_to_px4.bash` 遍历的是 `./configs/*`，实际目录名是 `config/`，脚本空跑。
5. `external_odometry_publisher.py` 只做了 `q_x180 ⊗ q`（左乘），漏了右乘，没有完成机体系 FLU→FRD 的转换，正确形式是 `q_x180 ⊗ q ⊗ q_x180⁻¹`。另外 topic 写死为 `/px4_1/...`。

### 2.2 px4_multi_drone_sim

```text
docker/{Dockerfile,docker-compose.yml,.env}      # 开发容器
multi_drone/
├── scripts/runner.py          # PX4Process / ControllerNode / get_microxrce_agent_exec / launch_robot
├── scripts/gazebo_server.py   # 下载 PX4-gazebo-models（zip），设置 GZ_SIM_RESOURCE_PATH / GZ_PARTITION / GZ_IP
├── launch/test_multi_drone_run.launch.py        # 读取 config/test_params.yaml，world='walls'
├── controllers/base/{base_controller,base_data,position_transformer}.py
├── controllers/x500/{x500_base,states,x500}.py  # State 模式 FSM + OffboardCommander
├── move_commands/base/{base_commander,base_g_code}.py
├── move_commands/x500/g_code/g00…g24.py         # JSON "G-code"
└── utils/geometry.py
multi_drone_msg/msg/{DroneInformMsg,DroneParamsMsg,LocalAndGlobalCoordinatesMsg,CoordinateDataMsg}.msg
```

**Docker 化**（`docker/Dockerfile`）：
- 基于 `ubuntu:22.04`，装 `ros-humble-desktop` 和 `desktop-full`，**从源码编译** Micro-XRCE-DDS-Agent（跟 master，不锁版本），装 `gz-harmonic`，然后 `git clone PX4-Autopilot && checkout v1.15.2 && make px4_sitl`，镜像内还有 `px4_msgs@release/1.15`。
- compose 只暴露了 `14540/udp` 和 `8888/udp`，`DISPLAY=host.docker.internal:0.0`（X11），使用 NVIDIA runtime 的环境变量。
- 评价：这是**开发机镜像**，不是**运行时镜像**：体积在 10 GB 量级，不可复现（跟随各仓库 master），需要 GUI。我们只借鉴"每机一个 `PX4Process` 描述对象"的抽象，镜像本身不用。

**runner.PX4Process** 用环境变量拼命令行：
- 环境变量有 `PX4_SYS_AUTOSTART`、`PX4_SIMULATOR=GZ`、`PX4_GZ_WORLD`、`PX4_GZ_STANDALONE=1`、`PX4_SIM_MODEL` 或 `PX4_GZ_MODEL_NAME`（二者互斥）、`PX4_GZ_MODEL_POSE`、`PX4_GZ_SIM_RENDER_ENGINE`。
- 命令行是 `px4 -i {id}`，可以选择在 gnome-terminal、xterm、konsole 或 bash 中运行。
- `get_microxrce_agent_exec()` 只起一个 `MicroXRCEAgent udp4 -p 8888`（正确）。

**控制器**：
- `BaseDroneController` 订阅 `px4_{id}/fmu/out/{vehicle_attitude,vehicle_local_position}`（BEST_EFFORT），发布 `px4_{id}/fmu/in/vehicle_command`（RELIABLE），`target_system=id+1`。
- `X500BaseController` 用 7 个状态类（`IDLE→ARMING→TAKEOFF→LOITER→OFFBOARD→LANDING→DISARM`，见 `states.py`），每 0.5 s 调一次 `handle()`。
- `OffboardCommander` 以 10 Hz 发布 `OffboardControlMode` 和 `TrajectorySetpoint`，有 position、velocity、mixed 三种模式。

**坐标变换**（`position_transformer.DroneLocalityState`）对每个量同时维护 local_NED、local_ENU、global_ENU、global_NED 四份：
- global = R(出生朝向) · local_ENU + 出生位置。
- ENU↔NED 用 `(x,y,z) ↦ (y,x,−z)`。

这个思路正确，**我们的 FrameService 就是它的服务端版本**。

**G-code 指令**（通过 `/id_{k}_x500/in/command_json` 发送 `std_msgs/String` JSON，`X500Commander` 以 10 Hz 执行队列）：

| 码 | 类 | 参数 | 语义 |
|---|---|---|---|
| G0 | `G0_Stop` | — | 中断当前指令并清空队列，速度 ≤0.05 m/s 视为完成（特殊指令，立即执行） |
| G1/G2 | Arm / Disarm | — | 设置 `params.arming` 标志，由 FSM 推进 |
| G3/G4 | Takeoff / Land | altitude | — |
| G5/G6 | Hold / Offboard | — | — |
| G20 | `G20_MoveToPoint` | x,y,z,yaw,velocity,coordinate_system | 单航点 |
| G21 | `G21_LinearMove` | start_point,end_point,velocity,yaw | 两个航点 |
| G22 | `G22_CircularTrajectory` | start,end,radius,direction(CW/CCW),points_count | 圆弧离散（**有误**，见 §3.7） |
| G23 | `G23_Orbit` | center,radius,angular_velocity,orbit_direction,yaw_mode,duration | 绕点，每圈 100 个点 |
| G24 | `G24_SpiralTrajectory` | center,radius_start,radius_end,height_change,turns,points_per_turn,yaw_mode | 锥形或柱形螺旋 |

执行机制（`BaseMoveGCommand.is_complete`）：当前航点误差 ≤ **0.5 m** 就切到下一个航点，并且只在 `OffboardState` 下允许执行。**`velocity` 字段从来没有下发**（`execute` 里被注释掉），实际速度由 PX4 的 `MPC_XY_VEL_MAX` 决定，所以轨迹是"走走停停"。

**缺陷清单（本仓库）**

1. `update_orientation()` 中 yaw 的计算：PX4 的 `VehicleAttitude.q` 是 `[w,x,y,z]`，代码却套用 `[x,y,z,w]` 的公式，得到的实际上是 **roll**；`yaw_NED = −yaw_ENU` 也不对，应为 `π/2 − yaw_ENU`。
2. `takeoff()` 发送 `NAV_TAKEOFF(param7 = −3.5)`。param7 是 **AMSL 高度**，这里传入的是 NED 的负高度，结果依赖 PX4 的兜底逻辑。
3. `ArmingState.enter()` 调用 `reset_ekf()`，发出的其实是 `VEHICLE_CMD_PREFLIGHT_REBOOT_SHUTDOWN(param1=1)`，语义是**重启飞控**，在真机上很危险。
4. `G22` 的圆心公式错误（§3.7）。
5. 命名空间写死为 `px4_{id}`。PX4 main 分支（2026-09）的 rcS 已改成 `uav_{i}`（§2.5）。

### 2.3 XTDrone

**分层架构**（`images/architecture_2_cn.png`，论文 arXiv:2005.01125）：

```text
人机交互层  QGC / ROS / Qt 地面站、系统监视器、日志分析器   ←→ 我们：Web UI（React + shadcn）
协同层      ROS：无人机 1..n 之间通信（编队、任务分配）       ←→ 我们：Swarm / Agent Runtime（ANet）
高层控制层  ROS：每机 communication 节点（统一指令语义）      ←→ 我们：Simulation Gateway（DroneCommand）
底层控制层  PX4（MAVROS ↔ MAVLink）                          ←→ 我们：PX4 SITL（SIH）/ Mock / 真机
模拟器层    Gazebo：动力学、传感器、三维场景、其他无人系统    ←→ 我们：World Runtime（+ V0.6 gz）
通信层      MAVLink / ROS / MAVROS                           ←→ 我们：MAVLink UDP / WebSocket / (ROS2 可选)
```

这个分层和 01-design 的 "Browser / Simulation Service / Agent Runtime" 能一一对应，而且**多了"高层控制层 = 每机统一指令语义"这一层**，01-design 里缺这层（§7）。

**多机启动**（`coordination/launch_generator/generator.py`）：交互式输入机型和数量，按 `launch_temp_1.11` 模板逐行替换，生成 `multi_vehicle.launch`。每架的端口和位置如下：

```text
id_in_all = 全局序号；offboard_local = 34580+i；offboard_remote = 24540+i
mavlink_udp_port(SITL) = 18570+i；mavlink_tcp_port(simulator) = 4560+i
MAVROS fcu_url = udp://:24540+i@localhost:34580+i ；tgt_system = 1+i ；namespace = <type>_<id_in_type>
spawn: x = 3*row_in_all + 3*(id%row)，y = 3*(id//row + 1)
```

配套的修改版 rcS（`sitl_config/init.d-posix/rcS` 第 108–115 行）把 offboard 端口改成 `34580+i / 24540+i`，同时**注释掉了上游"i>9 → 14549"的规则**，于是可以一口气开 10 架以上。`single_vehicle_spawn_xtd.launch` 用 `xmlstarlet ed` 在 spawn 前改写模型 SDF 中 `mavlink_interface/mavlink_tcp_port`，然后用 `gazebo_ros spawn_model -sdf -param model_description` 生成模型，PX4 以 `-i $(ID) -w sitl_$(vehicle)_$(ID)` 运行，每机的工作目录互相独立。

**通信节点**（`communication/multirotor_communication.py`，`_enhanced.py` 是增强版）：

- 输入 topic（统一词汇），共 7 个：`/xtdrone/<type>_<id>/{cmd, cmd_pose_flu, cmd_pose_enu, cmd_vel_flu, cmd_vel_enu, cmd_accel_flu, cmd_accel_enu}`。
- `cmd` 字符串词汇：`ARM`、`DISARM`、`OFFBOARD`、`AUTO.TAKEOFF`、`AUTO.LAND`、`AUTO.RTL`、`HOVER`、`mission0..9`（其余字符串直接当 PX4 custom_mode）。
- 输出：以 30 Hz **持续**发布 `mavros/setpoint_raw/local`（`PositionTarget`），这就是保活。
  - `coordinate_frame` 取值：ENU 用 1（`FRAME_LOCAL_NED`，MAVROS 自动转换），FLU 用 8（`BODY_NED`），位置 FLU 用 9（`BODY_OFFSET_NED`）。
  - type_mask 分三类：只用位置和 yaw、只用速度和 yaw_rate、只用加速度和 yaw_rate。
- `hover_state_transition()`：速度指令全部低于 0.02 m/s 且 yaw_rate 低于 0.005 时，锁定当前位置转入 HOVER；否则切回 OFFBOARD。
- `_enhanced.hold_state_transition()` 按轴锁定：某轴速度指令为 0 时，把该轴转成位置 P 控制 `v = −Kp(p − p_hold)`（Kp=1），防止纯速度控制时漂移。

**这套"统一指令词汇 + 帧标注 + 保活 + 零指令即悬停"的做法是本单元最值得移植的高层控制语义**（§3.11）。

- ⚠ 基础版 `cmd_accel_*_callback` 调用 `construct_target(ax=…)`，但函数的形参名是 `afx/afy/afz`，会直接抛 `TypeError`。增强版已修复。

**编队（`coordination/formation_demo/`）**：
- `leader.py`：
  - 接收 `/xtdrone/leader/cmd` 指令。如果是编队名（`formation_dict_{6,9,18}` 中的 `T/diamond/triangle/cube/pyramid/sphere/cuboid`），就执行一次 **KM（Kuhn-Munkres）**，把跟随机重新分配到新阵位，并由新阵型计算**通信拓扑**。
  - 以 200 Hz 发布 `/xtdrone/formation_pattern`（3×(N−1) 的相对偏移）和 `/xtdrone/communication_topology`（N×N）。
- `follower.py`：普通 leader-follower 速度律。
- `follower_consensus.py`：一致性协议，有 vel 和 accel 两种模式。
- `avoid.py`：两两切向避碰，结果发布到 `/xtdrone/<type>_<id>/avoid_vel`。
- 详细算法见 §3.6。

**其他相关模块**：
- `coordination/formation_communication_demo/control/script/communication_verify_1.py`：**SINR 链路可达矩阵**（§3.8），20 机，15 Hz。
- `sim_addleader_1.py`：用 `mpi4py` 让每机一个 rank 并行控制，`uav_bias` 表是出生偏移，用来把 local 坐标变换到 global。
- `sensing/pose_ground_truth/get_local_pose.py`：把 `/gazebo/model_states` 的真值当作 `mavros/vision_pose/pose` 回灌给 EKF。因此 XTDrone 在视觉模式下，各机 local 坐标系就等于 Gazebo 世界坐标系，编队可以直接用 local 位姿；**换成 GPS 模式就不成立了**。
- `motion_planning/3d/ego_planner/plan_manage/launch/run_in_xtdrone.launch`：把 EGO 的 `pose_cmd` 重映射到 `/xtdrone/iris_i/cmd_pose_enu`。**规划器只需要对接通信节点**，这一点值得学。`rosmsg_tcp_bridge/src/bridge_node.cpp` 是 EGO-Swarm 的机间桥：轨迹走 TCP 环形链路，里程计走 UDP 广播。
- `coordination/launch_generator/generator_without_PX4.py` 配合 `control/control_gazebo_vehicles.py`：**无 PX4 的运动学模式**，直接发布 `gazebo/set_model_states` 移动模型。它相当于我们的 MockBackend，XTDrone 也用这种方式做大规模场景的可视化。
- `sitl_config/models/livox_mid40/scan_mode/*.csv` 配合 `livox_avia.sdf` 中的 `liblivox_laser_simulation.so`：Livox 非重复扫描仿真（§3.9）。
- `control/XTDGroundControl/python/`：PyQt5 地面站，含 matplotlib 轨迹图和多机状态文本，只作 UI 信息架构参考。

### 2.4 XTDrone2

```text
xtd2_launch/launch/{yaml_demo_launch.py, xtd2_vehicle_spawn_launch.py, gz_launch.py, launch_config/{default.yaml, ros_gz_bridge.yaml}}
xtd2_launch/xtd2_launch/utils/{px4_launch.py, gazebo_launch.py, bridge_launch.py}   # console_scripts
xtd2_communication/xtd2_communication/multirotor_communication.py                     # 409 行
xtd2_msgs/{msg/XTD2VehicleState.msg, srv/XTD2Cmd.srv}
xtd2_gz_sim/{models/(x500*, lidar_2d_v2, OakD-Lite, gimbal, …), worlds/(default, walls, windy, baylands, …)}
xtd2_control/xtd2_control/keyboard/multirotor_keyboard_control.py
xtd2_test/xtd2_test/communication_test/accel_test.py
```

- **YAML 机队描述**（`default.yaml`）：`world.name` 加上 `vehicle[] {model: gz_x500, id, pose:[x,y,z,r,p,y]}`。`yaml_demo_launch.py` 的流程是：启动 gz、启动**一个** `MicroXRCEAgent udp4 -p 8888`（正确做法），然后对每架机启动 `px4_launch` 和通信节点。
- **px4_launch.py** 的命令：
  ```text
  PX4_UXRCE_DDS_NS={model}_{id} PX4_GZ_WORLD=… PX4_SYS_AUTOSTART={4001|4002} PX4_SIM_MODEL=gz_x500
  PX4_GZ_MODEL_POSE='x,y,z,r,p,y' PX4_GZ_STANDALONE=1
  px4 -d -s …/etc/init.d-posix/rcS …/ROMFS/px4fmu_common -i {id} -w {px4_dir}/build/px4_sitl_default
  ```
  这里显式设置 `PX4_UXRCE_DDS_NS`，**不依赖 PX4 版本的默认命名空间**，这是好做法。但所有实例都用同一个 `-w`。按 `main.cpp:317–323`，**只有在没显式给 `-w` 时才会追加 `/<instance>` 子目录**，所以各实例会共享 `fs/parameters.bson` 和 `log/` 目录，参数写入会互相竞争。
- **bridge_launch.py**：用 `string.Template` 把 `${ros_ns}/${gz_ns}` 填进 bridge yaml（`lazy: true`，只桥接 pose 和 odometry）。
- **通信节点**：
  - 服务 `/xtdrone2/<ns>/cmd`（`XTD2Cmd`：ARM、DISARM、HOVER（空实现）、OFFBOARD、TAKEOFF、LAND、RTL）。
  - 话题 `cmd_pose_local_{ned,flu}`、`cmd_vel_{ned,flu}`、`cmd_accel_{ned,flu}`、`cmd_attitude_flu`。
  - `OFFBOARD_STATE` 取值为 `DISABLED/ENABLED/POSE_LOCAL_NED/…/ATTITUDE_FLU`。收到 OFFBOARD 指令后，才以 20 Hz 发送 `OffboardControlMode` 和 setpoint。
  - FLU→NED 的公式是对的：`v_n = f·cosψ + l·sinψ`，`v_e = f·sinψ − l·cosψ`，`v_d = −u`，`yawspeed_ned = −ω_z`。
  - TAKEOFF 使用 `param5/6 = ref_lat/lon`、`param7 = 10.0`。**param7 是 AMSL，传 10.0 是误用**，仅在海拔约为 0 的世界里恰好成立。
- **致命缺陷**：`publish_vehicle_command()` 写死了 `target_system = 1`。PX4 `Commander::handle_command()`（`src/modules/commander/Commander.cpp:865`）要求 `target_system ∈ {MAV_SYS_ID, 0}`，而实例 i 的 `MAV_SYS_ID = i+1`，**所以 id≥1 的飞机收到的所有指令都被静默丢弃**。这也解释了 README"后续功能开发"里的"实现多机控制"。修复方法：改成 `target_system = id+1`，或者设为 0（广播）。DDS 命名空间已经隔离了各机，所以设 0 是安全的。
- `worlds/windy.sdf` 虽然写了 `<wind><linear_velocity>10 10 10`，但**没有加载 `gz-sim-wind-effects-system`**（只有 Physics、UserCommands、SceneBroadcaster、Contact、Imu、AirPressure、ApplyLinkWrench、NavSat、Sensors），所以风不会作用到机体上。
- `docs/InstallationTutorial.md` 只有章节标题，推荐环境为 Ubuntu 24.04 + ROS2 Jazzy + Gazebo Harmonic。

### 2.5 PX4 上游事实核对（编排必需）

| 事实 | 出处 | 对编排的影响 |
|---|---|---|
| `MAV_SYS_ID = i+1`，`UXRCE_DDS_KEY = i+1` | `rcS:134–135` | sysid 与实例号绑定；在容器模式下用 `PX4_PARAM_MAV_SYS_ID` 覆盖（`rcS:247` 的 env 覆盖先于 `rcS:363` 引入 `px4-rc.mavlink`，已实测生效） |
| DDS 命名空间：i=0 时没有；i>0 时 v1.15/v1.18 为 `px4_i`，**main（2026-09）为 `uav_i`**；设置 `PX4_UXRCE_DDS_NS` 可覆盖，设为空串则清空 | `rcS:296–311`（main）；镜像中 v1.18 为 `rcS:300` | **不要依赖默认值**，编排时一律显式设置 `PX4_UXRCE_DDS_NS=<drone_id>` |
| 端口：GCS 链路本地 `18570+i` → 远端 14550；offboard 链路本地 `14580+i` → 远端 `14540+i`（**i>9 时回落到 14549**）；payload `14280+i→14030+i`；gimbal `13030+i→13280+i`；SIH 显示 `19450+i→19410+i`；DDS agent 端口 8888（`PX4_UXRCE_DDS_PORT` 可覆盖） | `px4-rc.mavlink`、`rcS:320–327` | 在共享网络命名空间的部署里，i>9 时必须按 sysid 在 14549 上解复用，或者改写 rc.mavlink（XTDrone 就是这么做的） |
| 锁文件 `/tmp/px4_lock-<i>`，shell socket `/tmp/px4-sock-<i>`（机器全局） | `main.cpp:85,409`、`sock_protocol.cpp:47` | 同一台主机或同一个 `/tmp` 下，两个会话不能复用同一实例号 → 采用容器隔离，或做全局实例号分配 |
| 只有在没显式给 `-w` 时，工作目录才会追加 `/<instance>` | `main.cpp:317–323` | 显式给 `-w` 时必须让每个实例的目录不同 |
| `PX4_PARAM_<NAME>=value` 可覆盖任意参数 | `rcS:247–253` | 在容器环境变量里写 Digital Twin 参数（SIH_MASS、KDV、风等） |
| gz 模式下，PX4 自己调用 `/world/<w>/create` 生成 `${model}_${i}`，**所以模型名依赖实例号**；启动前会等待 `/world/<w>/scene/info`（最多 30 次，每次 1 s） | `px4-rc.gzsim` | gz 模式下各实例的 `-i` 必须全局唯一，并且要先启动 gz server |
| 设置 `PX4_HOME_LAT/LON/ALT` 后，gz 模式调用 `set_spherical_coordinates`；SIH 模式写入 `SIH_LOC_LAT0/LON0/H0/YAW0` | `px4-rc.gzsim`、`px4-rc.sihsim` | 出生位姿统一从 World 锚点推导 |
| SIH 使用 lockstep：每步 `_current_simulation_time_us += sim_interval_us`，并按 `PX4_SIM_SPEED_FACTOR` 控制墙钟节拍；CPU 不足时仿真时间变慢 | `sih.cpp:94–186` | 监控 RTF，并做 CPU 预留和准入控制 |
| SIH 风：`_v_wind_N = (SIH_WIND_N, SIH_WIND_E, 0)`，`_v_apparent = v + w`，四旋翼阻力 `F = −KDV·v_apparent`；**参数订阅间隔为 1 s** | `sih.cpp:367,725,464`、`sih.hpp:146` | V0.4 可做 ≤1 Hz 的准静态风耦合（§3.10） |
| 官方运行时镜像：`px4io/px4-sitl`（SIH，152 MB，Ubuntu 24.04），`px4io/px4-sitl-gazebo`（Harmonic） | `Tools/packaging/containers/Dockerfile.{sih,gazebo}` | V0.2 直接采用；entrypoint 识别 `host.docker.internal`，并用 sed 改写 MAVLink 的 `-t` 和 DDS 的 `-h` |

---

## 3. 可复用算法与实现（含伪代码/参数）

### 3.1 ID / 端口 / 命名空间分配

**规则（所有 Driver 统一）**

```text
drone_id   : 业务 ID（"uav0"、"p600-01"），也用作 DDS 命名空间和容器名后缀
sysid      : 1..250（同一 MAVLink 网络内唯一；255=GCS，0=广播）→ 超过 250 架时按会话分片、多网关
instance i : 只在"共享网络命名空间"的 Driver 里有意义（LocalProcess / Gazebo-Pod）
dds_key    : = sysid
dds_ns     : = drone_id（一律通过 PX4_UXRCE_DDS_NS 显式设置）
```

**容器模式（推荐，已实测）**：每架都是 `px4 -i 0`，容器内端口固定，**主机上不需要分配任何端口**：

| 方向 | 容器内（PX4） | 网关（同一 docker 网络） |
|---|---|---|
| 遥测和指令（onboard 模式） | 本地 14580 ↔ 远端 `host.docker.internal:14540` | 网关绑定 `0.0.0.0:14540`，按 `(sysid, src_ip)` 解复用；指令发往 `drone_ip:14580` |
| GCS 链路（可选，用于调试 QGC） | 18570 → `host.docker.internal:14550` | 网关可以转发给 QGC（相当于一个 mavlink-router） |
| uXRCE-DDS（可选，V0.6 接 ROS2 算法时） | 客户端 → `host.docker.internal:8888` | 每个会话一个 `MicroXRCEAgent udp4 -p 8888`，由 key 区分客户端 |

**共享命名空间模式**（LocalProcess、Gazebo Pod）用端口公式分配，写成伪代码：

```python
def ports(i):                       # 与 PX4 rcS 一致；base 为会话偏移（多会话同机时用 netns 隔离更好）
    return dict(gcs_local=18570+i, offboard_local=14580+i,
                offboard_remote=(14540+i if i <= 9 else 14549),
                payload_local=14280+i, gimbal_local=13030+i, sih_local=19450+i)

class InstanceAllocator:            # 会话内实例号；/tmp/px4_lock-<i> 是主机全局 → 需要主机级租约
    def __init__(self, lease_dir="/var/lib/anet/px4-leases"): ...
    def acquire(self, session_id) -> int:
        for i in range(0, 64):
            if try_flock(f"{lease_dir}/{i}.lock"):      # 进程崩溃时 flock 自动释放
                write(f"{lease_dir}/{i}.owner", session_id); return i
        raise NoCapacity
```

### 3.2 出生位姿与 FrameService（已实测）

World 锚点为 `(φ0, λ0, h0)`（WGS84）。1 km 以内使用切平面近似，误差在毫米级；更大范围改用 ECEF→ENU（pymap3d / pyproj）。

```text
M = a(1−e²)/(1−e² sin²φ0)^{3/2}     N = a/(1−e² sin²φ0)^{1/2}      (a=6378137, e²=6.69437999014e−3)
ENU→geo: φ = φ0 + n/(M+h0)          λ = λ0 + e/((N+h0) cosφ0)       h = h0 + u
geo→ENU: n = (φ−φ0)(M+h0)           e = (λ−λ0)(N+h0) cosφ0          u = h − h0
```

**每架机的 local 帧**：PX4 的 `LOCAL_POSITION_NED` 原点在它自己的 EKF 原点，也就是出生点。实测 10 架机的 local xy 都在 ±0.05 m 以内，而它们的出生点东向相距 0–54 m。由此得到变换：

```text
T_world←local_k :  p_world_ENU = spawn_ENU_k + R_z(ψ0_k=0) · swap(p_local_NED)
swap(n,e,d) = (e, n, −d)             速度同理（不需要平移）
```

优先用 GLOBAL_POSITION_INT 经 `geo→ENU` 得到世界坐标，这样和机体 EKF 原点无关；在 GPS 拒止的场景下（V0.5 外部定位），改用 `spawn_ENU + swap(local)`。实测出生误差：4 机为 0.09–0.30 m，10 机为 0.05–0.39 m，来源是 SIH 的 GPS 噪声。

### 3.3 健康检查分级与生命周期状态机

```text
                 create            start             first HEARTBEAT        prearm bit & GPOS
  PENDING ─────► PROVISIONING ───► STARTING ───────► BOOTED ─────────────► READY ──arm──► ACTIVE
                                      ▲                 │ hb_age>2.5s          │ hb_age>2.5s
                                      │                 ▼                      ▼
                         RESTARTING ◄─┴──── LOST ◄── DEGRADED ◄────────────────┘
                         (backoff 1,2,4,8s; ≤3 次/60s)  hb_age>5s
                                      │ 预算耗尽
                                      ▼
                                   FAILED              DRAINING(land→disarm) → STOPPED → REMOVED
```

| 级别 | 探针 | 阈值（建议） | 实测 |
|---|---|---|---|
| L0 进程 | Docker `inspect.State.Running` / events `die` | 立即 | kill 后可立即从 docker events 感知（原型未接入，依靠 L1） |
| L1 链路 | HEARTBEAT 距今时长（1 Hz） | >2.5 s → DEGRADED；>5 s → LOST | **2.0 s / 4.4 s**（N=4），2.05 s / 4.48 s（N=10） |
| L2 就绪 | `SYS_STATUS.onboard_control_sensors_health & 0x10000000`（PREARM_CHECK）+ 收到 `GLOBAL_POSITION_INT` | 启动后 30 s 仍未 READY → 视为失败 | 启动后 **6.5–6.7 s**（N=4），8.8–11.7 s（N=10） |
| L3 实时率 | RTF = Δ`time_boot_ms` / Δwall，5 s 滑窗 | <0.95 告警；<0.8 暂停准入 | 负载 94 时为 **0.77–0.79** |
| L4 链路质量 | `SYS_STATUS.drop_rate_comm`、seq 断档、消息速率 | 丢包 >5% 告警 | 裁剪后 70.6 msgs/s |
| 业务 | `EXTENDED_SYS_STATE.landed_state`、STATUSTEXT（≥WARNING） | 映射到 UI 事件 | 捕获到 `Preflight Fail: no heading reference`（启动早期）、`Failsafe activated` 等 |

重启语义：
- 对**同一个容器**执行 start，保留 rootfs 里的 `parameters.bson` 和 ulog，**6.7–9.1 s** 回到 READY。
- SIH 重启后，飞机会在 HOME 点、以地面状态重生，而不是停在空中。所以 Mission 层要收到 `vehicle.respawned` 事件并重新规划；UI 把原轨迹标成中断。

### 3.4 最小 MAVLink 网关（`mavlite.py`，零依赖）

**v2 帧**：`0xFD | len | incompat | compat | seq | sysid | compid | msgid(3B LE) | payload(len，尾部 0 被截断) | crc16 | [signature 13B]`。

**CRC**：X.25（CRC-16/MCRF4XX，初值 0xFFFF），计算范围是 `len..payload`，最后再累加 `crc_extra`：

```python
def x25(data, crc=0xFFFF):
    for b in data:
        t = b ^ (crc & 0xFF); t = (t ^ (t << 4)) & 0xFF
        crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    return crc
```

**crc_extra 与载荷布局**（全部与 PX4 v1.18 实测互通：收方向 CRC 全部校验通过，零错误；发方向命令都拿到了 ACK）：

| msgid | 名称 | crc_extra | struct（小端） |
|---|---|---|---|
| 0 | HEARTBEAT | 50 | `<IBBBBB` custom_mode,type,autopilot,base_mode,system_status,mavlink_version |
| 1 | SYS_STATUS | 124 | `<IIIHHhHHHHHHb`（present,enabled,**health**,load,…,battery_remaining） |
| 22/23 | PARAM_VALUE / PARAM_SET | 220 / 168 | `<fHH16sB` / `<fBB16sB` |
| 30 | ATTITUDE | 39 | `<Iffffff` |
| 32 | LOCAL_POSITION_NED | 185 | `<Iffffff` |
| 33 | GLOBAL_POSITION_INT | 104 | `<IiiiihhhH`（lat/lon 单位 1e-7 度，alt 单位 mm AMSL，relative_alt 单位 mm） |
| 76/77 | COMMAND_LONG / COMMAND_ACK | 152 / 143 | `<fffffffHBBB` / `<HB` |
| 84 | SET_POSITION_TARGET_LOCAL_NED | 143 | `<IfffffffffffHBBB` |
| 245 | EXTENDED_SYS_STATE | 130 | `<BB`（vtol_state,landed_state） |
| 253 | STATUSTEXT | 83 | `<B50s` |

**type_mask**：只用速度时为 `1|2|4|64|128|256|1024|2048 = 3527`；只用位置时为 `8|16|32|64|128|256|2048 = 2552`（与 XTDrone 的 `PositionTarget.IGNORE_*` 组合一致）。

**流量裁剪（已实测）**：默认 onboard 链路发送 ATTITUDE 96.6 Hz、HIGHRES_IMU 48 Hz、GLOBAL_POSITION_INT 48 Hz、ATTITUDE_QUATERNION 48 Hz、LOCAL_POSITION_NED 29 Hz、ODOMETRY 29 Hz，此外还有十几种 9.6 Hz 的消息，合计 **414.6 msgs/s**。READY 后按下表发 `COMMAND_LONG(511, msgid, interval_us)`：

```text
保留: HEARTBEAT 1s, SYS_STATUS 0.5s, GLOBAL_POSITION_INT 50ms, ATTITUDE_QUATERNION 50ms,
      LOCAL_POSITION_NED 50ms, EXTENDED_SYS_STATE 0.5s, BATTERY_STATUS 1s
关闭(-1): 30,105,331,111,141,132,290,291,83,85,36,74,24,87,42,230,514,441,12901,...
```

裁剪后为 **70.6 msgs/s**，各主流 19.4 Hz。30 个 ACK 的结果集合是 {0, 4}：对当前没有推流的 msgid，PX4 返回 FAILED(4)，这是正常的。纯 Python 解析在负载下约 3.6 万帧/s/核，因此：
- **≤100 架**时，单个 asyncio 网关进程足够。
- **>100 架**时，先在报文头按 msgid 过滤再算 CRC；或者按每 50 架分一个网关进程；或者在前面加 mavlink-router（C++）。

### 3.5 Offboard 控制样例与约束

把三个仓库的样例和实测结果合并，得到的**标准时序**如下：

```text
1. READY 后:   COMMAND_LONG(400 ARM, p1=1) → ACK 0         (实测 t≈+1s)
2. 起飞:       COMMAND_LONG(22 NAV_TAKEOFF, p4..p7=NaN) → ACK 0 → AUTO_TAKEOFF → 自动转 LOITER
               (p7=NaN ⇒ MIS_TAKEOFF_ALT=2.5m；实测 7.8–10.5s 到 >2m。不要像 G3/XTDrone2 那样传 NED/相对高度)
3. 预发流:     SET_POSITION_TARGET_LOCAL_NED ≥10Hz 连续 ≥1s（PX4 要求 >2Hz）
4. 切模式:     COMMAND_LONG(176 DO_SET_MODE, p1=1 CUSTOM, p2=6 OFFBOARD) → ACK 0
5. 保活:       只要处于 OFFBOARD 就持续 ≥10Hz（COM_OF_LOSS_T 默认 1s；丢流 ⇒ failsafe ⇒ RTL，已实测）
6. 退出:       先切 AUTO.LOITER (p2=4, p3=3) 再停流；或发 LAND(21)
```

target_system 必须等于 `MAV_SYS_ID`，或者设为 0。XTDrone2 就是在这里出的错。

### 3.6 编队：XTDrone 算法移植（已在 PX4 SIH 上复现）

**(a) 阵位分配（KM → Hungarian）**：XTDrone 的 `leader.py::build_graph/KM/find_path` 以 `w_ij = int(50 − ‖orig_i − new_j‖)` 为权、求最大权匹配，等价于**最小化 Σ 距离**（有取整误差）。我们改用 O(n³) 的 Hungarian，已与穷举对拍 100 组、n=2..6：

```python
def assign(followers_rel_enu, slots_rel_enu):          # 返回 follower i -> slot j
    C = [[dist(p, s) for s in slots] for p in followers]  # 可加：能力不匹配惩罚、ETA、能耗
    return hungarian(C)                                   # 或 scipy.optimize.linear_sum_assignment
```

**(b) 通信拓扑**（`leader.py::get_communication_topology`）。它构造一个**以领机为根的有向无环图**，保证 consensus 收敛：

```text
c = floor(N/2)
leader 连接离它最近的 c 个 follower；把它们放入 BFS 队列
while 队列非空 且 已访问 < N−1:
    k = 出队；对未访问节点按到 k 的距离排序，取最近 c 个，建立 k → j（只保留 k < j 的边，防止成环）
    把新节点入队
任何没有入边的 follower → 补一条来自 leader 的边（防掉队）
A[j][i] = 1 表示 j 监听 i
```

**(c) 编队律**（`follower.py` 与 `follower_consensus.py`）。记号：`d_i` 为阵位偏移（ENU，领机 `d_0=0`），`N_i` 为 i 的邻居集合。

```text
leader-follower (vel):  v_i = Kp·((p_0 + d_i) − p_i) + Ka·a_i                        Kp=1.0, Ka=2.0
consensus (vel):        v_i = (Kp/|N_i|) Σ_{j∈N_i} [(p_j − d_j) − (p_i − d_i)] + Ka·a_i
consensus (accel):      u_i = (Kp/|N_i|) Σ_j [(p_j − d_j) − (p_i − d_i) + γ(v_j − v_i)] + Ka·a_i,  γ = √(4/Kp)
限幅:                   XTDrone 写法是 ‖v‖ > √3·vmax 时缩放到 vmax（阈值与目标不一致，属于 quirk）→ 我们用 ‖v‖ ≤ vmax
本项目改进:             v_i += v_0^{xy}（领机速度前馈）   ← 实测误差减半
```

**(d) 切向避碰**（`avoid.py`）。它不是径向排斥，而是绕行，在相向而行时能避免僵持：

```text
for 每对 (i,j):  d = p_i − p_j,  k = 1 − ‖d‖/r_a   (r_a = 1.5 m)
    if k > 0:
        aux = x̂ if |cos∠(d,x̂)| < |cos∠(d,ŷ)| else ŷ          # 选与 d 更正交的辅助轴
        f = k · (d × aux)/‖d × aux‖
        a_i += f ;  a_j −= f
```

**PX4 SIH 实测**（`experiment.py`，10 Hz 控制环，世界 ENU 由 GLOBAL_POSITION_INT 换算）：

| 场景 | 设置 | 后 10 s 平均误差 | 后 10 s 最大误差 | 最小间距 |
|---|---|---|---|---|
| 4 机，P 控制 | Kp=1，vmax=3，领机北向 1 m/s，CPU 0.6 核/机 | 1.28 m | 1.75 m | 2.61 m |
| 4 机，P + 前馈 | 同上，CPU 1.0 核/机，RTF 0.78 | **0.55 m** | **0.97 m** | 2.82 m |
| 10 机，P + 前馈 | 从 54 m 横队出发，V 字 9 阵位（±4k, −4k） | 收敛过程中（t=29 s 时平均 0.72 m） | — | 2.2 m |

P 控制的稳态滞后约为 `v_leader/Kp`，理论 1 m，实测 1.28 m，多出来的部分是 PX4 速度环的滞后。所以加前馈是必须的。另外，XTDrone 原实现直接用 MAVROS 的 local 位姿，**只在视觉真值回灌（local=world）时才正确**；在 GPS 模式下必须先经过 FrameService。

### 3.7 轨迹原语（来自 px4_multi_drone_sim，经过修正）

**G22 圆弧**：原代码 `center = m + n·r`，得到的圆心到起点 A 的距离是 √(r² + (L/2)²) ≠ r，**生成的弧线不经过 A 和 B**。修正如下：

```text
L = ‖B−A‖, m = (A+B)/2, u = (B−A)/L,  n_cw = (−u_y, u_x, 0), n_ccw = −n_cw
require r ≥ L/2 ;  h = √(r² − (L/2)²) ;  C = m + n·h
θ_A = atan2(A−C), θ_B = atan2(B−C)；CW 时若 θ_B>θ_A 则 θ_B −= 2π；CCW 时若 θ_A>θ_B 则 θ_B += 2π
p(θ) = C + r(cosθ, sinθ), z 线性插值 A_z→B_z
```

**G23 绕点**：`p(t) = c + r(cos(±ωt), sin(±ωt))`，`yaw = atan2(c − p)`（朝向圆心）。**G24 螺旋**：`θ ∈ [0, 2π·turns]`，`r(θ)` 从 r0 线性变到 r1，`z(θ)` 从 z0 线性变到 z0+Δh。

**执行方式改进**：原实现是"航点 + 0.5 m 容差逐点切换"，并且丢掉了速度，因此走走停停。我们改成**弧长参数化 + 位置和速度前馈**：

```python
def sample(path, s):                         # path: 折线/解析曲线, s = v_cmd * t
    p, tangent = path.point_and_tangent(s)
    return dict(pos=p, vel=v_cmd * tangent)  # 同时下发 position + velocity（PX4 两者都用）
```

MockBackend（V0.1）和 PX4 后端（V0.2）共用这组生成器，前端也用它来预览轨迹。

### 3.8 机间通信信道模型（XTDrone SINR）

来自 `communication_verify_1.py`：

```text
h(i,j)  = 1 / (1 + ‖p_i − p_j‖²)                         # 简化路径损耗
SINR_ij = P·h_ij / (N0 + P·Σ_{k∈Tx, k≠j} h_ik)           # 接收机 i，发射机 j，其他发射机作为干扰
C_ij    = B·log2(1 + SINR_ij)
link_ij = 1  iff  C_ij > R,  R = M·rate                    # M = 128·N bit/帧，rate = 15 Hz
参数: B = 1 MHz, N0 = 1e−9 W, P = 0.5 W, ε = 2
```

在本项目里的用法：V1.0 的 ANet 能力发现和消息投递先经过这个链路矩阵过滤，把"通信"做成物理上有依据的东西；UI 可在 DebugLayer 画出链路图。V0.6 可以先用距离阈值版本，再升级到 SINR。

### 3.9 传感器挂载与 LiDAR 扫描模式

**SensorSpec → SDF**（PX4-Multiagent 的 xacro 宏，泛化成我们的数据结构）：

```yaml
sensors:
  - {type: lidar, name: mid360, model: livox_mid360, pose: [0.1, 0, 0.1, 0, 0, 0], rate_hz: 10,
     pattern: assets/lidar/mid360.csv, points_per_frame: 20000, range: [0.1, 70], noise_std: 0.02}
  - {type: camera, name: gimbal_rgb, pose: [0.15,0,-0.05,0,0.5,0], hfov_deg: 80, res: [1280,720], rate_hz: 30}
```

- **SIH 模式（V0.2–V0.5）**：由 World Runtime 在服务端对八叉树或体素做射线投射，产出 LiDAR 数据；PX4 本身不需要这些传感器。
- **Gazebo 模式（V0.6）**：用 Jinja 把 SensorSpec 生成 `gpu_lidar`、`camera` 片段，经 `<include merge>` 并入机体模型，再生成一个 airframe 垫片文件（`PX4_SIM_MODEL=…` 加 `. 4001_gz_x500`）。

**MID-360 扫描模式**（XTDrone `sitl_config/models/livox_mid40/scan_mode/mid360.csv`，16.8 MB，本机已统计）：

- 共 800,000 行，格式为 `index, azimuth(0–360°), zenith(37.8°–97.2°)`，对应仰角 **−7.2°～+52.2°**，与 MID-360 规格的 −7°～52° 一致。
- 按 200 k pts/s、10 Hz 计算，每帧 20,000 条射线，整张表循环一次 4 s。
- 仰角分布基本均匀，每 5° 一档约 5.5–8.9 万点，最顶档 3.7 万。

射线生成方法（与 `livox_laser_simulation` 插件相同）：

```text
frame k: rows [k·S, (k+1)·S) mod 800000,  S = 20000（Web 预览可下采样到 2000）
dir_sensor = (sinζ·cosα, sinζ·sinα, cosζ)   (x 前 z 上)
hit = raycast(world_octree, T_world←sensor · dir, r_max)；r += N(0, σ=0.01–0.02)
环境退化: fog/rain → r_max ← r_max·exp(−β·vis⁻¹)，dropout ~ Bernoulli(p(β))，见 01-design §23
```

其余 CSV（avia 24 万 pts/s、FOV ±35°；mid40 10 万 pts/s、±19°；mid70、horizon、HAP、tele）可以一起纳入传感器库。

### 3.10 环境风注入 SIH（V0.4，已实测）

```text
Gateway 每 1 s:  w = E.query(p_world_i, t).wind  (ENU, 风"去向")
                 SIH 定义为"来向"分量 → SIH_WIND_N = −w_n, SIH_WIND_E = −w_e
                 PARAM_SET(sysid_i, "SIH_WIND_N"/"SIH_WIND_E", …)   ← 仅当 |Δw| > 0.3 m/s 时发送（减少参数落盘）
稳态倾角:        θ ≈ atan(KDV·|w| / (m·g))
```

实测：
- **5 m/s 时横滚 27.5°**（理论 27.0°），位置保持的下风偏移约 0.4 m。
- **10 m/s 时 45.0°**（理论 45.5°），偏移约 2.4 m。
- 撤风后恢复到 0.2°。

**限制**：
1. 参数更新订阅节流为 1 s，只能做 ≤1 Hz 的准静态风。阵风和湍流需要给 SIH 打补丁（新增 uORB `sih_wind` 输入，经 `/fmu/in/…` 或 MAVLink 注入），或者交给我们自己的 6DoF。
2. 默认 `SIH_MASS=1`、`SIH_KDV=1` 时风阻偏大。P600 约 5.5 kg，按线性化 `KDV ≈ ½ρC_dA·v_ref`（取 C_dA≈0.25 m²、v_ref=8 m/s，得 KDV≈1.2），8 m/s 时倾角约 10°，更接近实际。
3. 没有垂直风分量。

### 3.11 指令词汇对照（统一到 r19 的 DroneCommand）

| DroneCommand（本项目） | XTDrone（ROS1） | XTDrone2（ROS2） | px4_multi_drone_sim | PX4 MAVLink 实现 |
|---|---|---|---|---|
| `arm` / `disarm` | `cmd: ARM/DISARM` | srv `ARM/DISARM` | G1 / G2 | `COMMAND_LONG 400` |
| `takeoff(alt_rel)` | `AUTO.TAKEOFF` | `TAKEOFF` | G3 | `22`，p7=home_amsl+alt 或 NaN |
| `hover` | `HOVER`（锁定当前位置并保持 setpoint） | `HOVER`（空实现） | G5 / G0 | offboard 位置保持，或 `176(1,4,3)` LOITER |
| `goto(p_world, v)` | `cmd_pose_enu` | `cmd_pose_local_ned` | G20 | offboard 位置加速度前馈 |
| `velocity(v, frame)` | `cmd_vel_enu/flu`，零指令即悬停 | `cmd_vel_ned/flu` | mixed 模式 | offboard 速度，mask 3527 |
| `follow_path / arc / orbit / spiral` | —（交给 EGO） | — | G21 / G22 / G23 / G24 | 弧长参数化，§3.7 |
| `formation(name)` | `/xtdrone/leader/cmd: <formation>` | — | — | Swarm 服务，§3.6 |
| `land` / `rtl` | `AUTO.LAND` / `AUTO.RTL` | `LAND` / `RTL` | G4 / — | `21` / `20` |
| `stop`（清空队列） | `stop controlling` | — | **G0** | Gateway 内部 |

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 条目 | 来源 | 目标模块 | 版本 | 方式 | MVP |
|---|---|---|---|---|---|
| 统一生命周期状态机与健康分级 | 本文原型，阈值取自 XTDrone 和 PX4 行为 | `apps/simulator/orchestrator/lifecycle.py`，Mock 同样实现 | V0.1（Mock）/ V0.2 | port | ✔ |
| 轨迹原语（修正后的 G22/G23/G24，加弧长参数化） | px4_multi_drone_sim `g22–g24.py` | `simulation/mission/primitives.py` + TS 预览 | V0.1 | port | ✔ |
| 指令队列、中断、G0 语义 | px4_multi_drone_sim `base_commander.py` | Gateway `CommandQueue` | V0.1 | port | ✔ |
| 零指令悬停与按轴保持 | XTDrone `multirotor_communication_enhanced.py` | Gateway 的 velocity 指令处理 | V0.1 / V0.2 | port | ✔ |
| FrameService（ENU↔geo，T_world←local） | px4_multi_drone_sim `DroneLocalityState`，XTDrone `uav_bias` | `world/georef/frames.py` | V0.1 | port | ✔ |
| 官方 SIH 镜像与每机一容器编排 | `px4io/px4-sitl`（PX4 `Dockerfile.sih`） | `simulation/px4/driver_docker.py` | V0.2 | adopt | |
| mavlite / 网关解复用 / 流裁剪 | 本文原型 | `simulation/backends/px4_mavlink.py`（生产用 pymavlink 或 MAVSDK，mavlite 做健康探针） | V0.2 | port | |
| SIH 风注入 | PX4 `simulator_sih` 参数 | `environment/coupling/sih_wind.py` | V0.4 | adopt | |
| EKF 视觉定位参数档 | PX4-Multiagent `external_odometry_mode.params` | `vehicles/p600/px4/vision.params` | V0.5 | reference | |
| 摄影测量 → Gazebo 世界导出规范 | PX4-Multiagent `doc/Create_custom_world.md` | `world/export/gazebo.py` | V0.6 | port | |
| SensorSpec → SDF（xacro 宏思路） | PX4-Multiagent `models/*/model.xacro` | `sensors/sdf_gen/` | V0.6 | port | |
| KM 编队重分配、DAG 拓扑、consensus、切向避碰 | XTDrone `formation_demo/*` | `swarm/formation/` | V0.6 | port | |
| SINR 链路模型 | XTDrone `communication_verify_1.py` | `agent/anet/channel.py` | V0.6 / V1.0 | port | |
| MID-360 等扫描模式 CSV | XTDrone `livox_mid40/scan_mode/*.csv` | `assets/lidar/` + `sensors/lidar/` | V0.5 | adopt | |
| YAML 机队 schema | XTDrone2 `default.yaml`、PX4-Multiagent `robots.yaml`、px4_multi_drone_sim `test_params.yaml` | Session spec（§4.2） | V0.2 | reference | |
| 分层架构（HMI / 协同 / 高层控制 / 底层 / 模拟器 / 通信） | XTDrone 论文图 | 系统架构说明书 | V0.1 文档 | reference | ✔ |
| 规划器只对接通信节点（EGO remap 到 `cmd_pose_enu`） | XTDrone `run_in_xtdrone.launch` | Planner 适配器约定 | V0.6 | reference | |
| 无 PX4 的运动学模式 | XTDrone `control_gazebo_vehicles.py` | 印证 MockBackend 定位（大规模只做可视化） | V0.1 | reference | ✔ |
| ROS1 栈、PyQt 地面站、Docker 开发镜像 | XTDrone / px4_multi_drone_sim | — | — | skip | |

### 4.2 Simulation Orchestrator 设计（V0.2 / V0.6）

**定位**：Orchestrator 是 Simulation Service 的**控制面**，负责"一个仿真会话（Session）= 一个 World + N 个载具 + 可选的 sim server"的**计划、供给、健康、监督、销毁**。数据面（遥测和指令）由 Gateway 或 SimBackend 承担。Orchestrator 把 `FleetEndpoint` 交给 Backend，Backend 不关心容器。

```text
┌───────────────────────────── Simulation Service (FastAPI) ─────────────────────────────┐
│  REST /api/sim/sessions ──► SessionManager ──► Planner(spec→plan: sysid/ns/spawn/ports)  │
│                                   │                                                    │
│                                   ▼                                                    │
│                      Driver 接口 (provision/start/kill/restart/remove/stats/logs)        │
│        ┌──────────────┬───────────────────┬────────────────────┬──────────────────┐     │
│        │ MockDriver   │ DockerSihDriver   │ GazeboPodDriver    │ LocalProcDriver  │ K8s  │
│        │ (V0.1 进程内)│ (V0.2 每机一容器) │ (V0.6 gz+N px4    │ (开发机 px4 -i i)│(V1.0)│
│        │              │  px4io/px4-sitl   │  共享 netns)       │                  │      │
│        └──────┬───────┴─────────┬─────────┴──────────┬─────────┴────────┬─────────┘     │
│               └────── HealthMonitor (L0–L4) ◄── MAVLink/DDS 探针 ──────┘               │
│                        │ lifecycle events                                              │
│                        ▼                                                               │
│  EventBus ─► Gateway(SimBackend: Px4MavlinkBackend / MockBackend / PrometheusGS…)       │
│          ─► WS /ws/sim (sim.session.*, sim.vehicle.*, sim.metrics)                      │
│          ─► ANet Registry（READY 时注册能力，LOST 时撤销）                                │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

**Session Spec（统一三个仓库的 YAML）**

```yaml
session: urban-demo-01
world: {id: urbanscene3d/campus, anchor: {lat: 22.5431, lon: 113.9360, h: 20.0}}
backend: px4-sih-docker        # mock | px4-sih-docker | px4-gz-pod | px4-local | prometheus-sitl
image: px4io/px4-sitl:v1.18.0-rc1
fleet:
  - {id: p600-01, airframe: sihsim_quadx, spawn: {enu: [0, 0, 0], yaw_deg: 0},
     params: {SIH_MASS: 5.5, SIH_KDV: 1.2}, sensors: [mid360, gimbal_rgb], capabilities: [lidar.mapping]}
  - template: {count: 8, prefix: uav, airframe: sihsim_quadx, pattern: grid, spacing_m: 5,
               origin_enu: [10, 0, 0], rows: 2}          # 对应 XTDrone generator 的 row/网格
resources: {cpu_per_vehicle: 0.3, mem_mb: 256, rtf_min: 0.9, max_parallel_spawn: 4}
policies:
  restart: {max: 3, window_s: 60, backoff_s: [1, 2, 4, 8]}
  teardown: {land_first: true, land_timeout_s: 30, collect_ulog: true}
telemetry: {rate_hz: {GLOBAL_POSITION_INT: 20, ATTITUDE_QUATERNION: 20, LOCAL_POSITION_NED: 20,
                      SYS_STATUS: 2, EXTENDED_SYS_STATE: 2, HEARTBEAT: 1, BATTERY_STATUS: 1}}
```

**计划阶段（Planner）**

```python
def plan(spec) -> SessionPlan:
    vehicles = expand_templates(spec.fleet)                 # grid/line/circle 模式展开
    assert len(vehicles) <= 250                             # sysid 上限
    need = len(vehicles) * spec.resources.cpu_per_vehicle
    admission.check(need)                                   # 空闲核数 × 0.8 ≥ need，否则 409 + 建议规模
    for k, v in enumerate(vehicles):
        v.sysid = k + 1
        v.dds_ns = v.id
        lat, lon, h = anchor.enu_to_geo(*v.spawn.enu)
        v.env = {"PX4_SIM_MODEL": v.airframe, "PX4_HOME_LAT": lat, "PX4_HOME_LON": lon, "PX4_HOME_ALT": h,
                 "PX4_HOME_YAW": v.spawn.yaw_deg, "PX4_PARAM_MAV_SYS_ID": v.sysid,
                 "PX4_PARAM_UXRCE_DDS_KEY": v.sysid, "PX4_UXRCE_DDS_NS": v.dds_ns,
                 **{f"PX4_PARAM_{k}": val for k, val in v.params.items()}}
        if spec.backend == "px4-gz-pod":                    # gz 模式: 实例号全局唯一，模型名=${model}_${i}
            v.instance = k; v.env |= {"PX4_GZ_STANDALONE": 1, "PX4_GZ_WORLD": world_sdf_name,
                                      "PX4_GZ_MODEL_POSE": csv(v.spawn.enu, 0, 0, yaw)}
    return SessionPlan(vehicles, network=f"anet-sim-{spec.session}")
```

**供给与启动顺序**

```text
SIH (V0.2):   create network(anet-sim-<sid>) → start Gateway 端点(绑定 14540，在该网络上拿到 IP)
              → 并行(≤4) create+start PX4 容器（ExtraHosts host.docker.internal:<gw_ip>，
                 NanoCpus=cpu_per_vehicle·1e9，Memory，Init=true，Labels anet.session/drone/role，
                 volume anet-ulog-<sid>-<id>:/root/.local/share/px4/rootfs/0/log）
              → 等待 READY（每机 30 s 超时）→ 发 SET_MESSAGE_INTERVAL → Session RUNNING
Gazebo (V0.6): world 导出 SDF(spherical_coordinates=anchor, collision=简化网格, 加载 WindEffects 若需要)
              → 启动 gz server 容器（px4io/px4-sitl-gazebo，GPU 可选）→ 探测 /world/<w>/scene/info
              → 以 --network container:<gz> 启动 N 个 PX4（-i k，k 全局唯一）→ XRCE agent(可选) → ros_gz_bridge(可选)
```

**监督（Supervisor）**

```python
async def supervise(v):
    async for ev in health.events(v):                       # L0..L3
        if ev in ("LOST", "EXITED"):
            if restart_budget(v).allow():
                await asyncio.sleep(backoff(v)); await driver.restart(v)   # 同容器 start：保留参数和 ulog
                bus.emit("sim.vehicle.respawned", v.id)                     # Mission 层重规划
            else:
                set_state(v, "FAILED"); bus.emit("sim.vehicle.failed", v.id)
        elif ev == "RTF_LOW":
            admission.freeze(v.session); bus.emit("sim.metrics.warn", ...)
```

**销毁**

```text
DRAINING: 对空中的机发 LAND(21) → 等 landed_state==ON_GROUND(EXTENDED_SYS_STATE) 或 30s → DISARM(400,p1=0,p2=21196 强制)
STOP:     SIGTERM(t=2s) → remove(force, v) 并行 → 收集 ulog 卷 → 删除网络
GC:       服务启动时 list(label=anet.session=*)，删除 DB 中不存在或已经 STOPPED 的会话残留
实测:     remove 串行 4 机 1.2–1.9 s，10 机 7.5 s（→ 并行化）；残留 0
```

**API 与事件**（给 UI 和 Agent 使用）：

| 接口 | 说明 |
|---|---|
| `POST /api/sim/sessions` | 提交 Session Spec，返回 `202 {session_id}` |
| `GET /api/sim/sessions/{id}` | 返回会话状态和每机状态（`state/sysid/ip/rtf/cpu/mem/last_hb_age/health_bits/events[-20:]`） |
| `POST /api/sim/sessions/{id}/vehicles` | 动态加机 |
| `DELETE /api/sim/sessions/{id}/vehicles/{vid}` | 动态减机（先 DRAINING） |
| `POST /api/sim/sessions/{id}/vehicles/{vid}:restart` / `:kill` | 故障注入 |
| `DELETE /api/sim/sessions/{id}` | 销毁 |
| WS `sim.session.state` | 会话状态：`CREATING/STARTING/RUNNING/DEGRADED/STOPPING/STOPPED/FAILED` |
| WS `sim.vehicle.state` | 每机状态迁移，附带 `{from, to, t, reason}` |
| WS `sim.metrics` | 1 Hz：RTF、CPU、msg/s、丢包 |

**容量规划**：

```text
cores = N·c_px4 + N·c_gw + c_gz(N) + headroom
c_px4 ≈ 0.22 核（实测 0.17–0.25，RTF≈1）
c_gw  ≈ 70 msg/s ÷ 36k frame/s·核 ≈ 0.002 核/机
c_gz  未测（gz server 物理 250 Hz × N，外加 GPU 传感器）
8 核服务器（留 20% 余量）：SIH 约 25 架；32 核：约 100 架；更多 → K8s 横向扩展，每个 Pod 8–16 架，一个网关进程管 ≤50 架
```

**安全**：Orchestrator 持有 docker.sock，等同 root 权限。它必须作为独立进程运行，只暴露上面的受限 API，浏览器不能直连。优先使用 rootless Docker 或 Podman，并通过 label 白名单限制只能操作 `anet.role=px4-sitl` 的容器。

### 4.3 与 r19 SimBackend 的衔接

r19 定义的 `SimBackend` 接口是 `connect / send(DroneCommand) / states() / capabilities`，本文在此之上补两点：

- `Px4MavsdkBackend` 更名为 **`Px4MavlinkBackend`**，内部可以选 MAVSDK、pymavlink 或 mavlite。它的连接信息来自 Orchestrator 的 `FleetEndpoint{drone_id, sysid, addr, dds_ns, T_world←local}`。
- `MockBackend` 实现同一个生命周期状态机，BOOTED 和 READY 的延迟可配置（默认 0.5 s / 1.5 s），并支持 `:kill` 故障注入。这样 **V0.1 的 UI（Fleet 面板、告警、重生）不依赖 PX4 也能完整测试**。

### 4.4 分阶段落地与验收

| 版本 | Orchestrator 能力 | 验收（参考本文实测） |
|---|---|---|
| V0.1 | MockDriver + 生命周期 + FrameService + 轨迹原语 | 50 架 Mock，UI 状态机完整；`kill` 后 3 s 内出现红色告警 |
| V0.2 | DockerSihDriver + mavlite 探针 + Px4MavlinkBackend + 流裁剪 + ulog 收集 | N=4：READY < 15 s；arm/takeoff/goto/land 全部 ACK 0；LOST 检出 < 5 s；重启到 READY < 15 s；销毁后残留 0 |
| V0.4 | SIH 风耦合（≤1 Hz），P600 SIH 参数档 | 10 m/s 风下倾角与理论偏差 < 2°；UI 显示风力矢量 |
| V0.5 | 外部定位参数档 + LocalProc/真机混编（同一 FleetEndpoint 契约） | 虚实混合 N=2+2 同屏 |
| V0.6 | GazeboPodDriver（传感器）+ Swarm 编队服务 + SINR（阈值版） | N=10 编队误差 < 1 m（领机 1 m/s，前馈）；最小间距 > 2 m；RTF ≥ 0.9 |
| V1.0 | K8sDriver + ANet 注册联动 + SINR 链路过滤 | N=100（多节点）；能力随 READY/LOST 自动上下线 |

**UI 相关要点**（供 UI PRD 引用）：
- **Fleet 面板**用 shadcn `Table`、`Badge`、`Tooltip`，每机一行"状态胶囊"。
- 色板：STARTING/BOOTED 用科技灰加 transitions.dev 脉冲；READY 用白；ACTIVE 用白色高亮；DEGRADED 用红描边；LOST/FAILED 用红底 `#E93024`。
- 会话创建用 `Dialog` + `Form`，包含机型 `Select`、数量 `Slider`、阵型 `ToggleGroup`。
- RTF/CPU 指标卡按 lieflat 风格做迷你折线；重启、杀死、销毁按钮使用 morphicons 图标，不用 emoji。

---

## 5. 对比与推荐

| 维度 | PX4-Multiagent-Simulation | px4_multi_drone_sim | XTDrone | XTDrone2 |
|---|---|---|---|---|
| ★ / 活跃度 | 11 / **2026-02** | 26 / 2025-06 | **1725** / 2025-08（ROS1，事实上已停止演进） | 115 / 2025-08 |
| 技术栈新旧 | 新（Humble + Harmonic + 1.15） | 新 | 旧（ROS1 + Classic，Classic 已 EOL） | **最新**（Jazzy + Harmonic） |
| 多机编排质量 | 中（有端口 bug） | 中（有指令 bug） | 高（端口方案完整，但依赖改过的 rcS） | 低（多机指令无效） |
| 传感器 | **强**（宏、多相机、PoseSensor） | 无 | **强**（52 模型，含 Livox 扫描模式） | 中 |
| 协同算法 | 无 | 无 | **强**（编队、KM、consensus、避碰、SINR、EGO 对接） | 弱 |
| Docker | 无 | 有（开发镜像，过重） | 无 | 无 |
| 与本项目契合度（CPU-only、Web、无 ROS 的 MVP） | 中：V0.6 gz 路径和 World 导出 | 中：Mock 任务原语 | 中高：算法与架构 | 低中：schema 参考 |
| **推荐排序** | **3** | **4** | **1**（算法来源） | **5** |

**综合推荐**：

1. **官方 `px4io/px4-sitl`（SIH）加本文的编排原型**：V0.2 的运行时底座，2026 年仍在更新，已实测。
2. **XTDrone**：编队、避碰、链路模型、指令语义和 Livox 模式的算法来源。只移植，不运行。
3. **PX4-Multiagent-Simulation**：V0.6 Gazebo 传感器路径和 World → SDF 导出的参考。它的 launch 结构清晰，但端口部分要按 §2.5 重写。
4. **px4_multi_drone_sim**：修正后的轨迹原语和指令队列语义。
5. **XTDrone2**：只参考 YAML schema 和 OFFBOARD_STATE 设计；等上游修好多机后可以再评估。

---

## 6. 风险与注意事项

| # | 风险 | 表现 / 证据 | 对策 |
|---|---|---|---|
| 1 | **ROS1 依赖**（XTDrone） | Noetic 已 EOL；Ubuntu 24.04 无官方包 | 只移植算法；如果必须运行，用 `ros:noetic` 容器隔离，通过 rosbridge 接入（同 r19） |
| 2 | **Gazebo 需要 GPU** | `gpu_lidar` 和相机需要 ogre2；本机没有 GPU，软件渲染极慢 | V0.2–V0.5 使用 SIH 加服务端射线投射；V0.6 的 gz 传感器节点部署到 GPU 服务器 |
| 3 | **CPU 争用使 RTF 下降** | SIH lockstep：负载 94 时 RTF 为 0.77；编队中领机的墙钟速度下降 | 设置 `NanoCpus`/cpuset 预留；监控 RTF；做准入控制；控制律时间基准改用 `time_boot_ms`，不用墙钟 |
| 4 | **端口与实例号冲突** | 共享 netns 时 i>9 回落 14549；`/tmp/px4_lock-<i>` 全局；显式 `-w` 时工作目录共享 | 每机一容器；共享 netns 时用 flock 租约，并按 sysid 解复用 |
| 5 | **命名空间随版本变化** | v1.15/v1.18 是 `px4_i`，main（2026-09）是 `uav_i` | 一律显式设置 `PX4_UXRCE_DDS_NS` |
| 6 | **target_system 错误导致静默失败** | XTDrone2 写死为 1，Commander 直接 `return false`，不回 ACK | 网关对每条指令等待 ACK（超时 1 s 重试 3 次），把失败上报为 UI 事件 |
| 7 | **Offboard 丢流触发 RTL** | 已实测 `Failsafe activated → RTL` | 网关保活 ≥10 Hz；指令源空闲时先切 HOLD |
| 8 | **local 帧混用** | 各机 local 原点不同（已实测）；XTDrone 编队只在"真值回灌"时成立 | 只在 World ENU 下做协同，统一走 FrameService |
| 9 | **SIH 物理简化** | 线性阻力；默认 1 kg / KDV=1；风只能 ≤1 Hz、只有水平分量；不含地面和障碍碰撞 | P600 参数标定；碰撞检测由 World Runtime（八叉树/SDF）完成，碰撞后触发 kill 并上报 crash 事件 |
| 10 | **gz 跨容器通信** | gz-transport 靠多播发现，需要设置 `GZ_IP`/`GZ_PARTITION`；PX4 以 `${model}_${i}` 命名模型 | gz 模式采用共享 netns 的 Pod，并保证实例号全局唯一（未实测，V0.6 需要验证） |
| 11 | **遥测洪泛** | 默认 414 msgs/s/机 | 用 511 裁剪到约 70；>100 架时分网关进程或加 mavlink-router |
| 12 | **docker.sock 权限** | 等同 root | 独立服务，接口白名单，rootless |
| 13 | **Gazebo 风插件缺失** | XTDrone2 `windy.sdf` 没有加载 WindEffects | 导出世界时，若需要风，自动加入 `gz-sim-wind-effects-system` |
| 14 | **参数落盘抖动** | 高频 PARAM_SET 会写 `parameters.bson` | 风注入加阈值和 1 Hz 上限 |
| 15 | **测量代表性** | 本机高负载，数据偏悲观；gz 路径未实测 | V0.2 在空闲的 8 核机器上重测基线，并写入 CI 性能门限 |

---

## 7. 对设计文档的优化建议

1. **§26 / §35 仿真后端分层需要重写**。原文是"第一阶段 PX4 + Gazebo"，建议改成四级：
   - **Mock（V0.1）**
   - **PX4 SIH 容器（V0.2，只用 CPU，已实测每机约 0.22 核）**
   - **PX4 + Gazebo Harmonic（V0.6，GPU 传感器）**
   - Isaac Sim（V1.0 以后）

   同时改正两处表述：Gazebo 不是"飞控 Backend"（§11 表格的写法有误），PX4 才是飞控，Gazebo 或 SIH 只是动力学和传感器来源；世界几何以 World Runtime 为唯一权威，Gazebo 世界只是 World Package 的一个导出物。
2. **§29 多机架构缺少编排层**。建议新增 "**Simulation Orchestrator**" 模块（§4.2），写明以下规则：
   - 会话、驱动和生命周期状态机。
   - ID、端口、命名空间规则：sysid = k+1，ns = drone_id 显式设置，每机一容器、一个网关端口。
   - 健康分级 L0–L4、重启和销毁策略、容量与准入规则。
3. **补上"高层控制层"**（XTDrone 分层）。§30 的控制模式和 §36 的数据流之间缺少"每机统一指令语义"这一层，包括：
   - 帧标注（ENU/FLU/NED）和保活；
   - 零指令悬停；
   - 队列与中断（G0）；
   - ACK 等待与重试。

   它应该作为 Gateway 的核心职责写进业务逻辑说明书。
4. **§28 DroneState 需要扩充**，新增：
   - `sim{backend, sysid, lifecycle_state, rtf, link{hb_age, msg_rate, drop}, respawn_count}`；
   - `frame{world_enu, local_ned_origin_enu}`；
   - `health.prearm`。

   同时明确 `position` 始终是 World ENU，local NED 只在调试时使用。
5. **§36 数据流 "PX4 → ROS2 → Gateway" 应改成"PX4 → MAVLink (UDP) → Gateway"**。ROS2/uXRCE-DDS 只在 V0.6 接入 ROS 算法（EGO-Swarm 等）时作为可选适配器。原因：
   - 多命名空间 DDS 的发现和 QoS 都很复杂；
   - MVP 不需要 ROS；
   - 实测纯 Python 网关已经够用。
6. **§37 更新频率要补两个硬约束**：
   - Offboard setpoint 必须 ≥10 Hz 持续发送（PX4 要求 >2 Hz，丢流即 RTL）；
   - 遥测要从 PX4 默认的 414 msgs/s/机裁剪到约 70。WebSocket 维持 10–20 Hz 不变。
7. **§19–§20 风场分级与动力学耦合写得不对**。Level 0–1 的"风→飞行"可以直接用 SIH 的 `SIH_WIND_N/E` 实现（≤1 Hz、只有水平分量，已实测）。要实现空间变化的 E(x,y,z,t)，需要网关按机位采样后下发，这是准静态近似。阵风、湍流和垂直风需要给 SIH 打补丁或用自研 6DoF。Gazebo 路径必须加载 WindEffects 插件（XTDrone2 就漏了）。
8. **§27 P600 数字孪生**要落到 SIH 参数上：`SIH_MASS`、`SIH_IXX/IYY/IZZ`、`SIH_T_MAX`、`SIH_Q_MAX`、`SIH_L_ROLL/PITCH`、`SIH_KDV`、`SIH_KDW`，通过 `PX4_PARAM_*` 注入容器，作为 `vehicles/p600/sih.params`。默认 1 kg / KDV=1 会让风效应被夸大 3–4 倍。
9. **§41 World Package 要增加 `simulation/` 导出**，包括：
   - `gazebo/world.sdf`（`<spherical_coordinates>` = 锚点；`<collision>` 用简化网格；原点在 z=0）；
   - `px4/home.json`（锚点和出生点模板）；
   - `sensors/lidar/*.csv`（扫描模式）。

   可参照 PX4-Multiagent 的 `Create_custom_world.md`。
10. **§30 控制模式**：Orbit/FollowPath 采用弧长参数化并带速度前馈，不要用"航点 + 容差"；§3.7 修正了 G22 的圆心公式。第二阶段的 Formation 要写明两点：用 Hungarian 做阵位分配，编队律带领机速度前馈（实测误差减半）。
11. **§31–§32 ANet**：加入**物理信道模型**（先距离阈值，后 SINR，§3.8）。能力注册与 Orchestrator 生命周期联动：READY 时上线，LOST 时下线，重生时刷新。任务分配（Task Assignment）直接复用 Hungarian，代价取 ETA、能力匹配度和电量的组合。
12. **§43–§49 路线图要补量化验收**（见 §4.4 表格）：READY 时延、LOST 检出时延、重启时延、RTF、编队误差和最小间距。同时说明"V0.6 = 10 架（单机）/ 30 架（8 核）/ 100 架（集群）"的规模分级。
13. **§33 技术栈表**：
    - "Simulation: Gazebo" 改为 "PX4 SIH (px4io/px4-sitl) → Gazebo Harmonic (V0.6)"；
    - "Message: ROS2/DDS" 改为 "MAVLink（主）/ ROS2（可选）"；
    - 新增 "Orchestration: Docker Engine API（V0.2）→ Kubernetes（V1.0）"。
14. **§42 仓库结构**：在 `simulation/` 下新增 `orchestrator/`、`drivers/{mock,docker_sih,gz_pod,local,k8s}`、`backends/{px4_mavlink,prometheus_gs,rosbridge,replay}`；在 `swarm/` 下新增 `formation/`、`assignment/`。
15. **回放（§39 Timeline）**：Orchestrator 在销毁时收集每机 ulog（容器卷 `/root/.local/share/px4/rootfs/0/log`），交给 ReplayBackend，使真实飞行和仿真回放共用同一条管线。

---

### 附：实测数据与复现

```bash
# 前置：docker 可用；本机已有 px4io/px4-sitl:v1.18.0-rc1 与 python:3.12-slim
cd /data/projs/anet-drone/.cache/research/r22
docker network create r22net
docker run --rm --network r22net -v /var/run/docker.sock:/var/run/docker.sock -v $PWD:/work -w /work \
  python:3.12-slim python -u experiment.py 4            # 4 机，P 编队，0.6 核/机 → results_N4.json
#                         experiment.py 10 ff           # 10 机，前馈                → results_N10_ff.json
#                         experiment.py 4 ff 1.0        # 4 机，前馈，1 核/机，RTF    → results_N4_ff.json
#                         stream_test.py / wind_test.py → stream_test.json / wind_test.json
docker network rm r22net
```

| 指标 | N=4（0.6 核） | N=10（0.6 核，前馈） | N=4（1.0 核，前馈） |
|---|---|---|---|
| 串行 create+start 总耗时 | 2.55 s | 11.22 s | 4.07 s |
| 首个 HEARTBEAT | 3.40–3.48 s | 4.40–6.01 s | — |
| READY | 6.48–6.71 s | 8.84–11.73 s | — |
| 出生点误差 | 0.09–0.30 m | 0.05–0.39 m | — |
| CPU / 内存（悬停） | 22.7–25.6 % / 17.7–18.9 MB | 18.3–23.1 % / 18.1–19.5 MB | — |
| 起飞到 >2 m | 7.82 s | 10.48 s | 9.66 s |
| 编队误差（后 10 s 平均 / 最大） | 1.28 / 1.75 m | 收敛中（t=29 s 时平均 0.72 m） | **0.55 / 0.97 m** |
| 最小机间距 | 2.61 m | 2.20 m | 2.82 m |
| RTF | — | — | 0.77–0.79（load 94） |
| kill → DEGRADED / LOST | 2.03 / 4.43 s | 2.05 / 4.48 s | 2.24 / 4.62 s |
| restart → READY | 6.67 s | 9.12 s | 7.84 s |
| 串行销毁 / 残留 | 1.91 s / 0 | 7.52 s / 0 | 1.22 s / 0 |
