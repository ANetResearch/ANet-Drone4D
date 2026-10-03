# R21 研究笔记：MAVSDK / MAVROS / MAVLink —— Simulation Gateway 与 DroneAdapter 设计

> 研究单元：r21 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §26–§30（Drone Simulation / 状态 / 多机 / 控制模式）、§33（Backend 技术栈）、§35–§37（Simulation Backend / 实时通信 / 频率）、§45（V0.2）、§48–§49（V0.5 / V0.6）
>
> 仓库快照（本地只读 clone）：
> - `refs/sim/MAVSDK` @ `34d4995`（2026-09-28，939 stars，**v4.0.0**，C++17 + C wrapper + Python/Kotlin/Java 绑定）
> - `refs/sim/mavros` @ `5c68b90`（2026-09-27，1226 stars，`ros2` 分支，**2.16.0**，C++/rclcpp）
> - `refs/backend/mavlink` @ `87da370`（2026-09-28，2442 stars，XML 方言 + 生成器；`pymavlink/` 是空的 git submodule，实际使用 PyPI 上的 `pymavlink==2.4.50`）
>
> 交叉引用的仓库（只看了与本单元接口相关的部分）：`refs/sim/PX4-Autopilot` @ `b3e343c`（2026-09-27），`refs/sim/Prometheus` @ `5dcd8cf`（2025-11-21）。
>
> 本机实测的产物都放在 `.cache/research/r21/`，研究用的 venv 在 `.cache/research/r21/venv`，内装 `mavsdk==4.0.0`、`pymavlink==2.4.50`、`numpy`，没有做任何全局安装：
> - `fake_px4.py`：用 pymavlink 写的 **PX4 风格 MAVLink 自驾仪仿真器**。单个进程可跑 N 架，端口规则与 PX4 SITL 一致；实现了 COMMAND_LONG/INT、Offboard、Mission 上传、Heartbeat 与遥测流。
> - `mavsdk_multi_test.py`：一个 MAVSDK v4 实例同时带 N 架，依次执行 arm → takeoff → offboard。
> - `mavsdk_scale_bench.py`：按线程拆分 CPU，看随架数 N 的扩展性；支持共享端口模式。
> - `control_bench.py`：mission 上传延迟和 offboard setpoint 调用延迟。
> - `starve_test.py`：线程池饥饿复现。
> - `parse_bench.py`：pymavlink 与手写 struct 解码器的吞吐。
> - `msgdump.py` / `msgdump.txt`：关键消息的线格式（由 pymavlink 生成的方言导出）。
> - `frames_check.py`：NED/FRD <-> ENU/FLU 转换的数值验证。
> - `mock_step_bench.py`：向量化 Mock 动力学与 WebSocket 打包的性能。
>
> 与其他笔记的分工：
> - 坐标体系（World ENU、Z-up、BODY=FLU）以 **r15 §3.1** 为准，本文只补充 MAVLink/PX4 边界上的 NED/FRD 转换。
> - WebSocket 二进制帧格式以 r15 §3.19 为基线，本文补充 MAVLink → DroneState 的字段映射，以及大规模机群用的 swarm-lite 量化布局。
> - PX4 SITL/Gazebo 的完整构建与多机脚手架属于 PX4/多机仿真单元。本文只引用端口、SIH、参数这类**接口事实**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **MAVSDK**（C++ 核心 + **v4 原生 Python 绑定** `pip install mavsdk==4.0.0`） | 面向 GCS/伴随计算机的 MAVLink 高层 SDK，提供 Action/Offboard/Mission/Telemetry/Param/MavlinkDirect 以及一组 Server 插件 | **adopt**：`MavsdkAdapter` 直接依赖 `mavsdk==4.0.0`（asyncio API）。**port**：命令重试参数、Offboard 20 Hz 保活与 heartbeat 看门狗、MissionItem → MISSION_ITEM_INT 的转换规则、PX4 custom_mode 编解码、THREADING 里的“可丢弃订阅/必达回调”两类队列，移植进 MockAdapter 和 Gateway | V0.2 adopt（PX4 SITL/SIH）；V0.1 port 语义给 MockAdapter | 5/5 |
| **MAVROS**（ros2 分支 2.16.0） | ROS2 <-> MAVLink 桥：Router + UAS + 66 个插件（核心 26 + extras 40），负责 ENU/NED 与 FLU/FRD 转换，并有 timesync | **reference/port**：`frame_tf` 转换公式（已数值验证）、Router 的地址学习与反向索引路由、`sys_time` 的双指数平滑 timesync 滤波器。**不进 Gateway 主链路**。P600（Prometheus，ROS1 + MAVROS）那一侧是作为**已存在的外部系统**来对接 | V0.5（P600 对接、时钟同步）；V0.6 可选 | 3/5 |
| **MAVLink**（XML 方言 + 生成器；pymavlink 2.4.50） | 协议本体：消息与命令定义、帧格式、CRC、微协议（command/mission/param/timesync） | **adopt**：消息语义作为 DroneState/Command 的“真值表”；用 `pymavlink` 写测试替身 `fake_px4`，以及可选的 **MavlinkFacade**，把 Mock 机暴露给 QGC。**port**：热点消息的手写 struct 解码器，用于被动监听、回放和日志。**reference**：`development.xml` 新条目（EXTERNAL_WIND_ESTIMATE、BATTERY_STATUS_V2、REQUEST_OPERATOR_CONTROL） | V0.1 语义；V0.2 测试替身；V0.3+ Facade | 4/5 |
| mavsdk-grpc / mavsdk_server（旧 MAVSDK-Python 路线） | gRPC 包装，每个 `mavsdk_server` 只服务一架机 | **skip**（只作兼容参考） | — | 1/5 |

**关键结论（实现者先读这几条）**

1. **MAVSDK v4 改变了多机方案，“每机一个 mavsdk_server 端口”已经过时。**
   - 2026 年起，PyPI 上的 `mavsdk`（v4.0.0）是基于 ctypes 调用 `libcmavsdk.so`（46 MB，除 libc/libm/pthread 外无其它依赖）的原生绑定，不再依赖 gRPC，也不再启动 `mavsdk_server` 子进程（`py/mavsdk/README.md`、`docs/en/python/migration.md`）。
   - 旧的 gRPC 版改名为 `mavsdk-grpc`（3.17.4），导入名是 `mavsdk_grpc`。
   - 旧路线为什么必须一机一 server：`mavsdk_server` 里的 `LazyPlugin::maybe_plugin()` 写死了 `_mavsdk.first_autopilot(0)`（`cpp/src/mavsdk/core/lazy_plugin.hpp`），一个 server 只能绑定第一架自驾仪，所以只能每机一个 server、每机一个 gRPC 端口。
   - v4 下**一个 `Mavsdk` 实例可以通过 `get_systems()` 管理 N 架**。本机实测：
     - 20 架各用独立 UDP 端口，1.2 s 内全部被发现；
     - 12 架共用一个 UDP 端口（对应 PX4 第 10 个实例之后统一发往 14549 的规则），也全部被发现，按 sysid 区分。
2. **MAVSDK v4 的每条消息开销不小，必须限频加分片。**
   - `MavsdkImpl` 对**每一条收发消息**都会用 libmav 再解析一次并转成 JSON，用于 JSON interception（`mavsdk_impl.cpp` 约 685–730 行、`deliver_message()`），哪怕没有任何订阅者也照做。
   - 实测 io 线程开销约 **0.24 ms/消息**：20 架 × 50 Hz × 3 条高频消息，不订阅时 io 线程就占 **73%** 核；订阅 position + attitude 后，总 CPU 达到 **129%**。
   - 对照：pymavlink 解析 22 µs/条，手写 struct 解码 1.4 µs/条。
   - 做法：
     - 用 `set_rate_*` 把 PX4 流限到 10–20 Hz（浏览器 WebSocket 本来就只需要 10–30 Hz）；
     - **每个 Gateway worker 进程最多带 16 架**，更多就分进程。
3. **asyncio 包装的两个坑都已复现，必须绕开。**
   - **线程池饥饿**：所有 `XxxAsync` 方法都走 `loop.run_in_executor(None, …)`，默认线程池只有 `min(32, cpu+4)`（本机 12）个线程。一个失联机的 `arm()` 会阻塞 0.5 s × 4 次 = **2.05 s**。16 架失联机同时 arm 时，健康机的 `set_position_ned` 被卡住 **2001 ms**；把默认线程池换成 128 线程后降到 **6 ms**。
   - **无界队列**：`subscribe_*` 内部用无界 `asyncio.Queue`，消费慢就会无限积压。必须包一层 latest-value mailbox（§3.12）。
4. **DroneAdapter 抽象**（§4.1）。Simulation Gateway 只面向一个 `DroneAdapter` 协议，实现有：
   - `MockAdapter`（MVP，进程内 numpy 向量化运动学模型，1000 架 100 Hz 只占 3.6% 核）；
   - `MavsdkAdapter`（V0.2，PX4 SITL/SIH/真机 PX4）；
   - `PrometheusAdapter`（V0.5，P600 的 ROS1 话题 `/uavN/prometheus/{command,state,setup}`）；
   - `ReplayAdapter`（飞行日志回放）；
   - 以及反向的 `MavlinkFacade`（把 Mock 机伪装成 PX4，让 QGC/MAVSDK 能连上）。

   命令结果统一用 MAV_RESULT 语义；状态统一用 World ENU/FLU。
5. **MVP 不需要 PX4 和 ROS，但语义从第一天起就按 MAVLink 来设计。**
   - MockAdapter 直接输出 PX4 的 `custom_mode`、`MAV_LANDED_STATE`、`MAV_RESULT`，默认参数也用 PX4 的：`MIS_TAKEOFF_ALT`=2.5 m、`MPC_XY_VEL_MAX`=12 m/s、`MPC_ACC_HOR_MAX`=5 m/s²、`MPC_Z_VEL_MAX_UP`=3 m/s、`MPC_LAND_SPEED`=0.7 m/s、`COM_DISARM_LAND`=2 s、`COM_OF_LOSS_T`=1 s。
   - 这样 V0.2 换成 PX4 时 UI 与任务层无感。
6. **V0.2 首选 PX4 SIH，Gazebo 延后。**
   - PX4 内置 `sihsim_quadx`（airframe 10040），做纯 CPU 的机载动力学仿真，不需要 Gazebo，也不需要 GPU，本机能跑。
   - 多实例的端口规则：`-o 14540+i`，第 10 个实例之后统一用 14549；`MAV_SYS_ID = i+1`；GCS 端口 18570+i；uXRCE-DDS 走 8888，命名空间 `uav_i`。
   - SIH 的风可以在运行时通过参数 `SIH_WIND_N/E` 修改（`sih.cpp` 在参数更新时重新读取）。这就是 **Environment → PX4 风扰的最短链路**。注意符号：`v_apparent = v + wind`，即参数表示“风从哪边来”，设置时要取 `SIH_WIND_N = −W_north`。
7. **ROS 不进 Gateway 主链路。**
   - 原文 §26/§36 写的是 “PX4 → ROS2 → Gateway”，改为 **“PX4 → MAVLink(UDP) → Gateway(MavsdkAdapter)”**。
   - ROS2 只作为可选旁路：PX4 原生路线用 uXRCE-DDS 的 `/fmu/in|out/*`，MAVROS 用在需要 ROS 生态的时候。
   - P600 的 Prometheus 是 **ROS1** 加闭源的地面站通信库（`libcommunication_*.so`），通过 rosbridge 或 MAVROS 的 `gcs_url` 对接即可（§4.2.3）。
8. **坐标转换在 Adapter 边界一次做完**（公式已数值验证，§3.3）：
   - 位置：`ENU = (y_ned, x_ned, −z_ned)`；
   - 姿态：`q_ENU/FLU = (1/√2)·(w+z, x+y, x−y, w−z)`，由 `q_NED/FRD = (w,x,y,z)` 得到，且**同一公式反向也成立**（它是对合变换）；
   - 欧拉角：`roll'=roll, pitch'=−pitch, yaw'=π/2−yaw`。
   - **目标点一律走全局坐标**（`SET_POSITION_TARGET_GLOBAL_INT` / `goto_location`），不走 local NED。原因是 PX4 local NED 以各机 EKF 原点为基准，采用 azimuthal equidistant 投影，z 取海拔差，与 r15 的 World ENU 切平面存在曲率差 d²/2R（2 km 约 0.31 m）。
9. **命令语义统一，采用 MAVLink 命令微协议。**
   - MAVSDK 的参数：超时 0.5 s，重试 3 次；收到 `IN_PROGRESS` 后超时延长为 3 s；同一 command id 串行执行。
   - Mission 上传是 COUNT → REQUEST_INT → ITEM_INT → ACK 状态机，重试 5 次。
   - Gateway 的命令状态机（§3.5）照搬这套语义，UI 的 toast 和进度直接复用。
10. **多控制源必须仲裁。**
    - 真机上 Prometheus 的 `uav_control` 自己持续发 OFFBOARD setpoint。如果 Gateway 同时发，两边会“打架”。
    - Prometheus 的做法是 `Control_Level`（DEFAULT/ABSOLUTE）；MAVLink 2026 年 `development.xml` 新增了 `MAV_CMD_REQUEST_OPERATOR_CONTROL`。
    - Gateway 引入 **Control Lease**（控制租约）：每机同一时刻只允许一个 owner（UI 用户、任务引擎或 ANet agent）。

**本机实测汇总**（8 核 CPU、无 GPU、回环 UDP、`fake_px4` 作为对端）

| 场景 | 结果 |
|---|---|
| 1 个 MAVSDK v4 实例发现 N 架（独立端口，N=1/5/10/20） | 全部发现，耗时 1.0–1.2 s（轮询间隔 0.2 s） |
| 12 架共用一个 udpin 端口 | 12/12 被发现，CPU 69% |
| arm / takeoff 命令往返（3 架并发） | 中位数 17.8 / 21.3 ms，最大 29 / 34 ms |
| io 线程每条入站消息的 CPU（不订阅） | 0.24 ms；N=20@50 Hz 时 73% 核 |
| N=20，订阅 position + attitude | io 85% + 回调线程 29% + Python 主线程 15%，合计 129% |
| mission 上传（6 个 item 展开为 12 条 MISSION_ITEM_INT） | N=10 并发中位 85 ms；N=30 中位 221 ms，最大 292 ms |
| offboard `set_position_ned` 调用（20 Hz/架） | N=10：中位 1.4 ms，p99 8 ms，总 CPU 35%；N=30：中位 2.2 ms，p99 13 ms，92% |
| 16 架失联时，健康机的 setpoint 调用 | 默认线程池 **2001 ms**；128 线程池 **6 ms** |
| pymavlink 解析 / 编码 | 22 µs/条（45k 条/s）/ 9.4 µs/条 |
| 手写 struct 解码（GLOBAL_POSITION_INT 等 3 种） | 1.37 µs/条；x25 CRC（fastcrc）1.18 µs/帧 |
| 3 种高频消息的平均帧长（MAVLink2 截零后） | 33.3 B |
| Mock 动力学（numpy，每步） | N=100：99 µs；N=1000：363 µs（100 Hz 下 3.6% 核）；N=5000：2.0 ms |
| swarm-lite WebSocket 帧打包 | 30 B/架；N=1000 时 200 µs/帧 |

---

## 1. 仓库概览

| 项 | MAVSDK | MAVROS | MAVLink |
|---|---|---|---|
| 路径 | `refs/sim/MAVSDK` | `refs/sim/mavros` | `refs/backend/mavlink` |
| 最新提交 | 2026-09-28 `py: keep the Configuration usable after creating Mavsdk (#3125)` | 2026-09-27 `2.16.0` | 2026-09-28 `pymavlink: update for 32 bit system_id support (#2622)` |
| stars | 939 | 1226 | 2442 |
| 活跃度 | 非常高。v4 大重构：原生 Python、C wrapper、线程模型重写 | 高。ros2 分支持续发版（2.16.0） | 高。规范仓库，`development.xml` 每月都有新条目 |
| 语言与构建 | C++17、CMake；third_party 自带 asio、libmavlike、mavlink、curl、nlohmann_json、tinyxml2、fmt、cpptrace，mavsdk_server 另需 grpc/protobuf/absl/re2 等；Python 为 ctypes 绑定，wheel 自带 `.so` | ROS2 ament_cmake，colcon；依赖 Eigen、GeographicLib、diagnostic_updater | XML + Python 生成器（mavgen）；CMake 可生成 C/C++ 头文件 |
| 与本项目的关系 | **UAV API 首选**（原文 §33 已写 MAVSDK，本文细化到 v4 原生绑定） | 只在对接 ROS 生态或 P600 时使用 | 协议真值表 + 测试替身 |
| 本机可用性 | `pip install mavsdk==4.0.0` 即装即用，已验证 | 需要 ROS2 环境（本机没有），只读源码 | `pip install pymavlink` 即用，已验证 |

### 1.1 MAVSDK

MAVSDK 的整体结构是：

- 一个 C++ 核心 `libmavsdk`；
- 一层 C wrapper `libcmavsdk`（`c/`）；
- 在 C wrapper 之上分别是 Python（`py/`）、JNI/Kotlin（`jni/`、`kt/`）绑定；
- 另有 gRPC 服务端 `mavsdk_server`（`cpp/src/mavsdk_server/`），供旧 MAVSDK-Python、Swift、Java 使用。

所有插件的 API 都由 `proto/` 子模块（MAVSDK-Proto，在本 clone 中为空）通过模板生成（`tools/generate_from_protos.bash`）。因此 C++、C、Python、Kotlin 四套 API 在语义上一一对应。

v4 的三个关键变化：

1. `py/mavsdk/mavsdk/cmavsdk_loader.py` 用 ctypes 加载 `mavsdk/lib/libcmavsdk.so`，同一个 wheel 里同时提供 `mavsdk`（同步、回调风格）和 `mavsdk.asyncio`（异步）两套接口。
2. `Mavsdk` 与 `System` 分离：`add_any_connection()` 只负责建连，`get_systems()` / `on_new_system()` / `first_autopilot()` 负责发现，于是天然支持多机。
3. 新增 Server 插件（`TelemetryServer`、`ActionServer`、`MissionRawServer`、`CameraServer`、`ParamServer` 等），MAVSDK 可以反过来扮演自驾仪或载荷。

### 1.2 MAVROS

- `ros2` 是默认分支；ROS1 的 `master` 已处于维护状态。
- 包结构：`libmavconn`（传输层，asio 实现 serial/udp/tcp）、`mavros`（Router 与 UAS 两个节点加核心插件）、`mavros_extras`（40 个扩展插件）、`mavros_msgs`（接口定义）。
- 2.x 的一个重要架构变化：`mavros_node` 在同一进程里组合 `mavros_router` 和 `mavros`（UAS）两个节点，并开启 `use_intra_process_comms(true)`，使内部 MAVLink 总线零拷贝（`mavros/src/mavros_node.cpp`）。
- 多机方式：每机一个 `mavros_node`，各自配置 namespace `mavros/uas_N`、`tgt_system` 和 `fcu_url`（`launch/multi_uas.launch`）。

### 1.3 MAVLink

- XML include 链：`minimal.xml` → `standard.xml` → `common.xml` → `development.xml`。`common.xml` 有 233 条消息；`development.xml` 是待进入 common 的暂存区，打了 `<wip/>` 标记。
- `all.xml` 用注释记录各方言的 ID 段。例如 common 的消息段是 300–10000、命令段是 0–39999；ardupilotmega 是 11000–11999。
- 2026 年的动态：
  - 32 位 system id 的支持正在进行中：pymavlink 已更新，但 MAVSDK 的 `Configuration` 注释写明 “extended system ids … not supported yet”，sysid 大于 255 时会直接 abort。
  - `development.xml` 新增 `MAV_CMD_EXTERNAL_WIND_ESTIMATE`（43004）、`BATTERY_STATUS_V2`（369）、`SET_VELOCITY_LIMITS`（354）、`MAV_CMD_REQUEST_OPERATOR_CONTROL`、`GROUP_START/GROUP_END` 等。

---

## 2. 源码结构与关键模块

### 2.1 MAVSDK

```text
MAVSDK/
├── cpp/src/mavsdk/core/            # 核心：连接、系统、协议微服务、线程模型
│   ├── THREADING.md                # * 线程与回调规则（必读）
│   ├── mavsdk_impl.{hpp,cpp}       # 连接表、系统发现、收发路由、JSON interception
│   ├── system_impl.{hpp,cpp}       # 单架系统：heartbeat 超时、飞行模式、参数、命令入口
│   ├── mavlink_command_sender.*    # COMMAND_LONG/INT 重试与 ACK 状态机
│   ├── mavlink_mission_transfer_client.*  # Mission 微协议（上传/下载/清空/设当前）
│   ├── cli_arg.*                   # 连接 URL 解析：udpin/udpout/tcpin/tcpout/serial/raw
│   ├── px4_custom_mode.hpp, flight_mode.cpp   # PX4/ArduPilot 自定义模式编解码
│   ├── call_every_handler.*, timeout_handler.*  # io 线程上的截止时间驱动定时器
│   └── lazy_plugin.hpp             # mavsdk_server 用：first_autopilot(0)
├── cpp/src/mavsdk/plugins/{action,offboard,telemetry,mission,mission_raw,param,mavlink_direct,*_server,...}
├── cpp/src/mavsdk_server/          # gRPC 服务端（旧路线）
├── c/                              # C wrapper（cmavsdk）
├── py/mavsdk/mavsdk/               # v4 Python：ctypes + 同步 plugins/ + asyncio/plugins/
└── docs/en/python/migration.md     # gRPC → 原生的迁移说明
```

**(a) 线程模型（`core/THREADING.md`）**

| 线程 | 职责 |
|---|---|
| **io 线程** | 运行 `asio::io_context::run()`：所有 socket/serial I/O、MAVLink 解析、路由、插件协议状态机、全部定时器 |
| **user-callback 线程** | 从 `LockedQueue<UserCallback>` 取任务并执行用户回调 |
| HTTP loader 线程 | 只用于下载组件元数据 |
| 用户线程 | 阻塞 API 停在 `std::future` 上等待结果 |

核心不变量是 “Shared mutable state is only touched on the io thread. Everything else posts onto it.”

回调分两类：
- `call_user_callback()`：必达，命令结果之类都走这里；
- `call_user_callback_droppable()`：遥测流。队列超过 `MAX_DROPPABLE_USER_CALLBACKS`（`mavsdk_impl.hpp`，值为 100）时，**淘汰最旧的一条**，而不是拒收新的。

这两条语义直接移植到 Gateway：**命令结果必达，遥测只保留最新值**。

**(b) 连接与发现（`mavsdk_impl.cpp`）**

- URL 支持 `udpin://host:port`（服务端，PX4 SITL 用这个）、`udpout://`、`tcpin://`、`tcpout://`、`serial://dev:baud`、`raw://`。`raw://` 的收发由调用方自己完成：`pass_received_raw_bytes()` / `subscribe_raw_bytes_to_be_sent()`，可以把 MAVSDK 挂到任意传输上。
- 收到 `sysid != 0` 的消息且之前没见过时，调用 `make_system_with_component()`。它会忽略 sysid 为 0 的消息。GCS 模式下还会丢弃 sysid 255 / compid 190（QGC）的消息，这是一个 workaround：PX4 会在 mavlink 实例之间转发消息，不加过滤的话容易误连到 QGC。
- 发送：`deliver_message()` 遍历所有连接，`target_system != 0` 时只发给 `has_system_id(target)` 的连接。所以**多端口多机不会相互串扰**，共享端口时按 sysid 区分。
- 默认 ID（`include/mavsdk/mavsdk.hpp`）：GCS 为 sysid 245 / compid 190；伴随计算机为 1/195；相机为 1/100。**同一网络里的多个 GCS 必须使用不同 sysid**，例如 Gateway 用 245，QGC 用 255。
- 超时常量（`mavsdk_impl.hpp`）：`DEFAULT_TIMEOUT_S = 0.5`（命令），`DEFAULT_HEARTBEAT_TIMEOUT_S = 3.0`（判定掉线）。Heartbeat 以 1 Hz 发送（`HEARTBEAT_SEND_INTERVAL_S`）。

**(c) 命令发送（`mavlink_command_sender.{hpp,cpp}`）**

- 默认 `DEFAULT_RETRIES = 3`，每次超时时间为 `timeout_s()`，即 0.5 s。因此**一条无响应的命令最多耗时约 2.0 s**，与实测 2.05 s 吻合。
- 收到 `MAV_RESULT_IN_PROGRESS` 时，把超时重新注册为 3.0 s，并回调带进度的 `InProgress`。
- `do_work()` 遇到同一 command id 仍在飞行（in-flight）时会跳过队列中的后续同 id 命令。因此同一架机上对**同一命令**的调用是串行的。
- `DENIED`、`UNSUPPORTED`、`FAILED`、`TEMPORARILY_REJECTED`、`CANCELLED` 都会立即结束该命令。

**(d) Action（`plugins/action/action_impl.cpp`）的命令映射**

| API | MAVLink |
|---|---|
| `arm()` | `MAV_CMD_COMPONENT_ARM_DISARM(400)`，param1=1。PX4 下若当前处于 Mission/RTL/Land，会先切到 Hold（`need_hold_before_arm`） |
| `arm_force()` / `kill()` | param2 = 21196（魔数，强制执行） |
| `takeoff()` | `MAV_CMD_NAV_TAKEOFF(22)`。PX4 下**不带 param7**，起飞高度由 `MIS_TAKEOFF_ALT` 决定，默认 2.5 m；ArduPilot 需要先切 Guided |
| `land()` | `MAV_CMD_NAV_LAND(21)`，param4=NaN（保持当前航向） |
| `return_to_launch()` | `set_flight_mode(ReturnToLaunch)`，即 `MAV_CMD_DO_SET_MODE(176)` |
| `goto_location(lat, lon, alt_amsl, yaw)` | PX4 下先切 Hold，再发 `MAV_CMD_DO_REPOSITION(192)`，形式为 COMMAND_INT、`MAV_FRAME_GLOBAL_INT`，x/y 以 degE7 表示。**z 是 AMSL 海拔，不是相对高度** |
| `do_orbit(r, v, yaw_behavior, lat, lon, alt)` | `MAV_CMD_DO_ORBIT(34)` |
| `hold()` | `set_flight_mode(Hold)` |
| `set_current_speed(v)` | `MAV_CMD_DO_CHANGE_SPEED(178)` |
| `set_takeoff_altitude()` 等 | 读写参数 `MIS_TAKEOFF_ALT` / `RTL_RETURN_ALT`（PARAM 微协议） |

**(e) Offboard（`plugins/offboard/offboard_impl.cpp`）**

- `SEND_INTERVAL_S = 0.05`：一旦设置了 setpoint，就由 io 线程的 `add_call_every` **自动以 20 Hz 重发**。PX4 要求至少 2 Hz，`COM_OF_LOSS_T` 默认 1.0 s。
- `start()` 前如果没有设置 setpoint，返回 `NoSetpointSet`。
- `process_heartbeat()` 起看门狗作用：如果 heartbeat 里的 custom_mode 不再是 OFFBOARD，并且距 `_watchdog_grace_start` 已超过 **3 s**，就自动 `stop_sending_setpoints()`，防止模式被遥控器切走之后继续灌 setpoint。
- 各模式的 type_mask 与坐标系见 §3.7。

**(f) Telemetry（`plugins/telemetry/telemetry_impl.cpp`）的消息映射**

| MAVLink 消息 | 产出的 Telemetry 类型 | 换算 |
|---|---|---|
| `GLOBAL_POSITION_INT(33)` | `Position`、`VelocityNed`、`Heading` | lat/lon ×1e-7；alt/relative_alt ×1e-3（mm→m）；vx/vy/vz ×1e-2（cm/s→m/s）；hdg ×1e-2，值为 65535 时视为 NaN |
| `ATTITUDE_QUATERNION(31)` | `Quaternion(w=q1,x=q2,y=q3,z=q4)`、`AngularVelocityBody` | 时间戳 `time_boot_ms × 1000`（µs） |
| `ATTITUDE(30)` | `EulerAngle` | 弧度 → 度 |
| `LOCAL_POSITION_NED(32)` | `PositionVelocityNed` | 同时把 `health.local_position` 置为 true |
| `BATTERY_STATUS(147)` | `Battery` | voltages[10] 与 voltages_ext[4] 求和（遇 65535 停止）；current 为 cA，-1 视为 NaN；`remaining_percent` 取值 **0–100**；time_remaining 为 0 视为 NaN |
| `SYS_STATUS(1)` | 各类 Health、RC、`is_armable`，以及电池兜底 | 仅当没有 BATTERY_STATUS 时用 voltage_battery；`global_position_ok = present&enabled&health(GPS)`；`is_armable = health & MAV_SYS_STATUS_PREARM_CHECK` |
| `EXTENDED_SYS_STATE(245)` | `LandedState`、`VtolState` | — |
| `HEARTBEAT(0)` | `Armed`、`FlightMode` | `base_mode & SAFETY_ARMED`；custom_mode 按 §3.4 解码 |
| `HOME_POSITION(242)` | `Home` | 并把 `health.home_position` 置为 true |
| `GPS_RAW_INT`、`ODOMETRY`、`WIND_COV`、`ALTITUDE`、`HIL_STATE_QUATERNION`（ground truth）、`DISTANCE_SENSOR`、`HIGHRES_IMU`… | 对应类型 | — |

`set_rate_xxx(hz)` 发送 `MAV_CMD_SET_MESSAGE_INTERVAL(511)`。同一底层消息被多个订阅需要时，取**最大频率**（`max_rate_hz`）。

**(g) Mission（`plugins/mission/mission_impl.cpp::convert_to_int_items`）**

一个 `MissionItem` 会展开成多条 `MISSION_ITEM_INT`：

1. 有有效位置时，生成 `NAV_WAYPOINT(16)`，坐标系为 `GLOBAL_RELATIVE_ALT_INT`。param1 = 悬停时间，param2 = 接受半径，param4 = yaw。
2. 如果 `speed_m_s` 是有限值，追加一条 `DO_CHANGE_SPEED(178)`。
3. 按需追加云台 `DO_GIMBAL_MANAGER_PITCHYAW` / `CONFIGURE`，以及相机 `IMAGE_START/STOP_CAPTURE`、`VIDEO_*`、`DO_SET_CAM_TRIGG_DIST`。
4. 按需追加 `NAV_LOITER_TIME` 和 `NAV_DELAY`。
5. `VehicleAction` 可以是 Takeoff、Land、TransitionToFW/MC、RTL。

第 0 条的 `current=1`。上传使用 `MavlinkMissionTransferClient::UploadWorkItem`，状态机是 `SendCount → SendItems → 等待 ACK`，`retries = 5`。收到的 `MISSION_REQUEST_INT.seq` 小于期望值时视为重传请求，并计入重试次数。

**(h) Python v4 的 asyncio 包装模式**（`asyncio/plugins/*/…`，自动生成）

```python
# 一次性请求（阻塞 C 调用放进线程池）
async def arm(self):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: self._plugin.arm())   # ← 默认线程池
# 订阅（C 回调线程 → asyncio 队列）
async def subscribe_position(self):
    loop = asyncio.get_running_loop(); queue = asyncio.Queue()              # ← 无界
    def callback(data, _): loop.call_soon_threadsafe(queue.put_nowait, data)
    handle = self._plugin.subscribe_position(callback)
    try:
        while True: yield await queue.get()
    finally: self._plugin.unsubscribe_position(handle)
```

这里的两个坑见 §6 的 R3 和 R4。生命周期方面：`System` 与各插件都持有 C++ 句柄，**绝不能比 `Mavsdk` 活得久**（`~MavsdkImpl` 检测到泄漏会直接 abort）。应当用 `async with Mavsdk(cfg)` 管理生命周期，或显式调用 `destroy()`。

**(i) `mavsdk_server`**：命令行为 `mavsdk_server_bin [-p grpc_port] [--sysid N] [--compid N] udpin://0.0.0.0:14540`。它通过 `ConnectionInitiator` 等待第一架 autopilot，各插件由 `LazyPlugin` 绑定到 `first_autopilot(0)`。这正是旧 MAVSDK-Python 需要“每机一个 server 端口”的原因（`System(port=50051+i)`）。

**(j) MavlinkDirect**：通过 `load_custom_xml()` 在运行时加载自定义方言，按消息名以 JSON 形式收发任意消息（见 `examples/asyncio/mavlink_direct.py` 的 GAS_SENSOR 示例）。前面说的“每条消息都做 JSON 转换”就是为了支撑这个功能。以后若要在 MAVLink 链路上承载 ANet 或环境传感器的自定义消息，可以用它。

### 2.2 MAVROS（ros2 分支）

```text
mavros/
├── libmavconn/                 # asio 传输：serial/udp/tcp，URL: udp://bind@remote, udp-b://, tcp-l:// …
├── mavros/src/mavros_node.cpp  # 组合 Router + UAS，intra-process
├── mavros/include/mavros/
│   ├── mavros_router.hpp       # * Endpoint(fcu/gcs/uas) + Router 路由
│   ├── mavros_uas.hpp          # UAS 节点：插件加载、时间同步、TF、能力
│   ├── plugin.hpp / plugin_filter.hpp   # 插件基类、按 msgid 注册 handler（SystemAndOk/ComponentAndOk/AnyOk 过滤）
│   ├── frame_tf.hpp            # * ENU/NED、FLU/FRD、ECEF 转换
│   ├── setpoint_mixin.hpp      # setpoint 打包
│   └── mission_protocol_base.hpp  # Mission 状态机（IDLE/RXLIST/…/TXWPINT/CLEAR/SET_CUR）
├── mavros/src/lib/{mavros_router,uas_*,ftf_*}.cpp
├── mavros/src/plugins/{sys_status,sys_time,command,setpoint_raw,setpoint_position,local_position,global_position,imu,waypoint,...}.cpp
├── mavros/launch/{px4.launch,px4_config.yaml,px4_pluginlists.yaml,multi_uas.launch}
└── mavros_extras/src/plugins/  # 40 个：odom, vision_pose, obstacle_distance, gps_rtk, trajectory, ...
```

**(a) Router（`src/lib/mavros_router.cpp`）**

- Endpoint 分三类：`fcu`、`gcs`、`uas`。
- **地址学习**：每条入站消息把 `sysid<<8` 和 `(sysid<<8)|compid` 两个地址写入该 endpoint 的 `remote_addrs`（`Endpoint::recv_message`），并把反向索引标记为脏。
- **路由**：
  - 从消息定义里取出 `target_system` / `target_component` 的偏移，拼成 `target_addr`；
  - 在反向索引 `remote_index[addr] → [endpoints]` 中查找；
  - 有目标但没命中时，退化为广播（地址 0）；
  - 不回发给源 endpoint，**同类 endpoint 之间不转发**（fcu<->fcu、gcs<->gcs）。
- 每 30 s 重连一次失效的 endpoint。

这套算法可以直接移植到 Gateway 的 MAVLink 分发器上，用于把 PX4 流同时转给 QGC、Foxglove 和录制器（§3.10）。

**(b) 坐标转换（`src/lib/ftf_frame_conversions.cpp`）**

- `NED_ENU_Q = quaternion_from_rpy(π, 0, π/2)`，`AIRCRAFT_BASELINK_Q = quaternion_from_rpy(π, 0, 0)`。
- 向量用 `NED_ENU_REFLECTION_XY`（交换 x、y）乘以 `NED_ENU_REFLECTION_Z`（z 取反）。
- 协方差用同一组置换和反射做夹乘。
- `imu.cpp` 的姿态转换是 `transform_orientation_aircraft_baselink(transform_orientation_ned_enu(q))`；`setpoint_raw.cpp::local_cb` 在反方向对 yaw 做同样处理。

§3.3 已用 numpy 逐项复现，并验证了闭式公式。

**(c) setpoint 插件**：MAVROS **不会自动重发 setpoint**。用户必须以 2 Hz 以上的频率持续发布 `/mavros/setpoint_raw/local`（`mavros_msgs/PositionTarget`，字段与 SET_POSITION_TARGET_LOCAL_NED 一一对应，坐标用 ENU）。这一点和 MAVSDK 的 20 Hz 自动重发正好相反。

**(d) 时间同步（`src/plugins/sys_time.cpp`）**：基于 TIMESYNC(111) 的双指数平滑滤波，带 sigmoid 增益调度、RTT 门限和跳变检测，参数与伪代码见 §3.9。默认配置（`px4_config.yaml`）：`timesync_rate` 10 Hz，`system_time_rate` 1 Hz，`heartbeat_rate` 1 Hz，`conn_timeout` 10 s。

**(e) Mission（`mission_protocol_base.hpp`）**：列表超时 30 s，单条 WP 超时 1 s，重调度 5 s，重试 3 次。先尝试 MISSION_ITEM_INT，并在确认对端支持后固定使用。

**(f) 多机（`launch/multi_uas.launch`）**：每机一个 `mavros_node`，`fcu_url = udp://:1454i@127.0.0.1:1458i`，`tgt_system = i+1`，namespace 为 `mavros/uas_{i+1}`。

### 2.3 MAVLink 与 pymavlink

**MAVLink 2 帧格式**（`msgdump.txt` 由 pymavlink 生成）：

```text
0      1     2          3          4    5      6      7..9      10..      +2        (+13 可选)
STX=FD LEN   INCOMPAT   COMPAT     SEQ  SYSID  COMPID MSGID(24b) PAYLOAD  CRC16     SIGNATURE
            (0x01=signed)
- PAYLOAD 尾部的 0 字节会被截掉（LEN 为截后长度），接收端需补零到 payload_max
- CRC = CRC-16/MCRF4XX(X.25)，覆盖 LEN..PAYLOAD，最后再累加 1 字节 crc_extra（按 msgid 查表）
- 固定开销 12 B（无签名）
```

**关键消息的线格式**（payload_max / 帧最大长度 / crc_extra，字段按线上顺序排列，即大字段在前）：

| ID | 消息 | payload/帧 | crc_extra | 关键字段（单位） | 本项目用途 |
|---|---|---|---|---|---|
| 0 | HEARTBEAT | 9 / 21 B | 50 | `custom_mode:u32`、`type:u8`、`autopilot:u8`、`base_mode:u8`、`system_status:u8`、`mavlink_version` | 在线判定（1 Hz，3 s 超时）、armed、模式 |
| 1 | SYS_STATUS | 43 / 55 | 124 | sensors present/enabled/health（u32 位图）、`voltage_battery`（mV）、`battery_remaining`（%）、`drop_rate_comm`（c%） | 健康、可解锁、链路丢包 |
| 30 | ATTITUDE | 28 / 40 | 39 | roll/pitch/yaw（rad）、角速度 | 调试 |
| 31 | **ATTITUDE_QUATERNION** | 48 / 60 | 246 | `time_boot_ms`、q1..q4（w,x,y,z，NED←FRD）、rollspeed/pitchspeed/yawspeed、`repr_offset_q[4]` | DroneState.orientation |
| 32 | LOCAL_POSITION_NED | 28 / 40 | 185 | x,y,z（m，EKF 原点）、vx,vy,vz | 平滑的局部位置 |
| 33 | **GLOBAL_POSITION_INT** | 28 / 40 | 104 | lat/lon（degE7）、alt（mm AMSL）、relative_alt（mm）、vx/vy/vz（cm/s NED）、hdg（cdeg，65535 表示未知） | DroneState.position（换算到 World ENU） |
| 44 / 51 / 73 / 47 | MISSION_COUNT / REQUEST_INT / **ITEM_INT** / ACK | 5 / 5 / 38 / 4 | 221 / 196 / 38 / 153 | ITEM_INT：seq、frame、command、current、autocontinue、param1–4、x/y（int32 degE7）、z（float）、mission_type | 航线上传 |
| 42 / 46 | MISSION_CURRENT / MISSION_ITEM_REACHED | 6 / 2 | 28 / 11 | seq、total、mission_state | 任务进度 |
| 75 / 76 / 77 | COMMAND_INT / COMMAND_LONG / COMMAND_ACK | 35 / 33 / 10 | 158 / 152 / 143 | ACK：command、result（MAV_RESULT）、progress、result_param2 | 命令微协议 |
| 84 | **SET_POSITION_TARGET_LOCAL_NED** | 53 / 65 | 143 | time_boot_ms、target_sys/comp、coordinate_frame、**type_mask:u16**、x..z、vx..vz、afx..afz、yaw、yaw_rate | Offboard |
| 86 | SET_POSITION_TARGET_GLOBAL_INT | 53 / 65 | 5 | lat_int/lon_int（degE7）、alt（float m）… | **推荐的 GoTo/路径 setpoint 形式** |
| 82 | SET_ATTITUDE_TARGET | 39 / 51 | 49 | q[4]、body rates、thrust | 高级控制（不进 MVP） |
| 111 | TIMESYNC | 16 / 28 | 34 | tc1、ts1（ns） | 时钟同步 |
| 147 | **BATTERY_STATUS** | 54 / 66 | 154 | voltages[10]（mV）、current（cA）、consumed（mAh）、remaining（%）、time_remaining（s）、charge_state、voltages_ext[4]、fault_bitmask | DroneState.battery |
| 242 | HOME_POSITION | 60 / 72 | 104 | lat/lon/alt、局部 x/y/z、q | Home、RTL 显示 |
| 245 | EXTENDED_SYS_STATE | 2 / 14 | 130 | vtol_state、**landed_state** | 起降状态机 |
| 253 | STATUSTEXT | 54 / 66 | 83 | severity、text[50]、id/chunk_seq | 事件日志 |
| 331 | ODOMETRY | 233 / 245 | 91 | 位置、q、速度、协方差[21]×2、frame_id | V0.5 真机与视觉里程计 |
| 231 | WIND_COV | 40 / 52 | 105 | wind_x/y/z（m/s）、var、accuracy | 自驾仪估计的风，与 Environment 对比 |
| 115 | HIL_STATE_QUATERNION | 64 / 76 | 4 | 真值状态 | SITL 真值（MAVSDK `ground_truth`） |
| 246 / 247 / 340 | ADSB_VEHICLE / COLLISION / UTM_GLOBAL_POSITION | 38 / 19 / 70 | — | 他机、冲突、UTM 位置 | V0.6 空域与避碰 |
| 332 / 330 | TRAJECTORY_REPRESENTATION_WAYPOINTS / OBSTACLE_DISTANCE | 239 / 167 | — | 5 点轨迹、72 扇区距离 | V0.6 规划器接口 |

**关键枚举（精确值）**

- `POSITION_TARGET_TYPEMASK`：X/Y/Z_IGNORE = 1/2/4，VX/VY/VZ = 8/16/32，AX/AY/AZ = 64/128/256，FORCE_SET = 512，YAW_IGNORE = 1024，YAW_RATE_IGNORE = 2048。
- `MAV_FRAME`：LOCAL_NED = 1，GLOBAL_INT = 5，GLOBAL_RELATIVE_ALT_INT = 6，LOCAL_OFFSET_NED = 7，BODY_NED = 8，BODY_OFFSET_NED = 9，GLOBAL_TERRAIN_ALT_INT = 11，BODY_FRD = 12。
- `MAV_MODE_FLAG`：CUSTOM_MODE_ENABLED = 1 … SAFETY_ARMED = 128。
- `MAV_STATE`：STANDBY = 3，ACTIVE = 4，CRITICAL = 5，EMERGENCY = 6。
- `MAV_LANDED_STATE`：UNDEFINED = 0，ON_GROUND = 1，IN_AIR = 2，TAKEOFF = 3，LANDING = 4。
- `MAV_RESULT`：ACCEPTED = 0，TEMPORARILY_REJECTED = 1，DENIED = 2，UNSUPPORTED = 3，FAILED = 4，IN_PROGRESS = 5，CANCELLED = 6，COMMAND_LONG_ONLY = 7，COMMAND_INT_ONLY = 8。注意：`common.xml`（本 clone 第 2986 行）已有 CANCELLED = 6，但 **PyPI 上 pymavlink 2.4.50 预生成的方言里没有它**。预生成方言落后于 XML，需要新条目时，用 `mavgen` 从 `refs/backend/mavlink` 的 XML 重新生成。
- `MAV_MISSION_RESULT`：ACCEPTED = 0 … INVALID_SEQUENCE = 13，DENIED = 14，OPERATION_CANCELLED = 15。
- `MAV_TYPE`：QUADROTOR = 2，HEXAROTOR = 13，GCS = 6，ONBOARD_CONTROLLER = 18。`MAV_AUTOPILOT`：PX4 = 12，ARDUPILOTMEGA = 3，INVALID = 8。

**pymavlink（PyPI 2.4.50，自带 fastcrc）**：`pymavlink.dialects.v20.common` 预生成了方言；`mavutil.mavlink_connection()` 提供连接抽象。它是纯 Python，解析开销约 22 µs/条，适合写测试替身、日志回放和中低频旁路。

---

## 3. 可复用算法与实现（含伪代码、参数）

### 3.1 MAVLink 2 轻量解码器（用于被动监听、回放和日志，port）

适用场景：Gateway 需要被动旁听某条链路（例如 P600 上 MAVROS 的 `gcs_url` 广播，或回放 `.tlog`），只关心几种高频消息，不想为此付出 MAVSDK 每条 0.24 ms 的开销。

```python
# 1.37 us/msg (本机)；CRC 可选 1.18 us/帧
HOT = {  # msgid: (struct, payload_max, crc_extra)
  33: (struct.Struct("<IiiiihhhH"), 28, 104),   # GLOBAL_POSITION_INT
  31: (struct.Struct("<Ifffffff"),  48, 246),   # ATTITUDE_QUATERNION（repr_offset_q 忽略，取前 32B）
  32: (struct.Struct("<Iffffff"),   28, 185),   # LOCAL_POSITION_NED
  0:  (struct.Struct("<IBBBBB"),     9,  50),   # HEARTBEAT: custom_mode,type,autopilot,base_mode,status,ver
  245:(struct.Struct("<BB"),         2, 130),   # EXTENDED_SYS_STATE
}
def iter_frames(buf):                 # buf: bytearray 累积 UDP 数据
    i = 0
    while True:
        j = buf.find(b"\xFD", i)
        if j < 0 or len(buf) - j < 12: break
        plen = buf[j+1]; incompat = buf[j+2]
        flen = 10 + plen + 2 + (13 if incompat & 0x01 else 0)
        if len(buf) - j < flen: break
        yield memoryview(buf)[j:j+flen]; i = j + flen
    del buf[:i]
def decode(fr, check_crc=True):
    plen = fr[1]; sysid, compid = fr[5], fr[6]
    msgid = fr[7] | fr[8] << 8 | fr[9] << 16
    ent = HOT.get(msgid)
    if not ent: return None
    st, pmax, extra = ent
    if check_crc and x25(fr[1:10+plen], extra) != (fr[10+plen] | fr[11+plen] << 8): return None
    payload = bytes(fr[10:10+plen]).ljust(pmax, b"\0")[:st.size]   # 补齐被截掉的尾零
    return sysid, compid, msgid, st.unpack(payload)
```

注意：
- MAVLink 1（STX=0xFE，6 字节头）在现代 PX4 上已不再使用，可以不支持。
- 签名帧多 13 字节。
- 截零规则只对 MAVLink 2 生效。

### 3.2 MAVLink → DroneState 映射（Gateway 统一状态）

DroneState 的定义与 r15 §3.1 对齐：World ENU、Z-up、米；姿态四元数 `[x,y,z,w]` 表示 WORLD←BODY(FLU)。

| DroneState 字段 | MAVLink 来源（PX4） | 换算 | 频率建议 |
|---|---|---|---|
| `position_enu[3]`（f64） | GLOBAL_POSITION_INT.lat/lon/alt | 按 r15 的 `Ellipsoid` 移植，`geodetic→ECEF→ENU(anchor)`，全程 float64 | 10–20 Hz（`set_rate_position`） |
| `velocity_enu[3]` | GLOBAL_POSITION_INT.vx/vy/vz（cm/s NED） | `(vy, vx, −vz)·0.01` | 同上 |
| `orientation[x,y,z,w]` | ATTITUDE_QUATERNION.q1..q4（w,x,y,z，NED←FRD） | §3.3 闭式公式 | 10–30 Hz |
| `angular_velocity_flu` | ATTITUDE_QUATERNION.roll/pitch/yawspeed（FRD） | `(p, −q, −r)` | 同上 |
| `armed` | HEARTBEAT.base_mode & 128 | — | 1 Hz 加变化即推 |
| `flight_mode` | HEARTBEAT.custom_mode | §3.4 | 同上 |
| `landed_state` | EXTENDED_SYS_STATE.landed_state | 枚举直通 | 1–5 Hz |
| `battery{v, a, pct, t_remain}` | BATTERY_STATUS（优先）/ SYS_STATUS（兜底） | 见 §2.1(f) | 1 Hz |
| `health{armable, gps, home, ...}` | SYS_STATUS 位图、HOME_POSITION | 见 §2.1(f) | 1 Hz |
| `gps{fix, sats, eph}` | GPS_RAW_INT | — | 1 Hz |
| `home_enu` | HOME_POSITION | 同 position | 变化即推 |
| `mission{seq, total, state}` | MISSION_CURRENT、MISSION_ITEM_REACHED | — | 变化即推 |
| `wind_est_enu` | WIND_COV（自驾仪估计） | `(wind_y, wind_x, −wind_z)` | 1 Hz（调试面板用） |
| `link{rtt_ms, drop_pct, last_hb_age}` | TIMESYNC RTT、SYS_STATUS.drop_rate_comm、heartbeat 时间 | — | 1 Hz |
| `t_sim_ns` / `t_boot_ms` | time_boot_ms 加 timesync 偏移 | §3.9 | 每帧 |

### 3.3 坐标与姿态转换（已数值验证，`frames_check.py`）

```python
S = 0.7071067811865476
def ned_to_enu_vec(n, e, d):             return (e, n, -d)                    # 对合：enu→ned 同式
def q_nedfrd_to_enuflu(w, x, y, z):      return (S*(w+z), S*(x+y), S*(x-y), S*(w-z))  # (w,x,y,z)
# 同一公式也把 ENU/FLU 转回 NED/FRD（对合）；结果符号可能整体取反（q 与 −q 同一旋转）
def euler_ned_to_enu(roll, pitch, yaw):  return (roll, -pitch, wrap(pi/2 - yaw))
def body_rate_frd_to_flu(p, q, r):       return (p, -q, -r)
```

验证数据：
- NED rpy=(5°, −7°, 120°) 转换后得到 ENU/FLU rpy=(5°, 7°, −30°)；
- 闭式四元数与 MAVROS 的 `NED_ENU_Q ⊗ q ⊗ AIRCRAFT_BASELINK_Q` 结果在 1e-9 以内一致。

**Three.js**：遵循 r15 R1，**场景保持 Z-up**（`Object3D.DEFAULT_UP=(0,0,1)`），可以直接使用 World ENU 和上面的 FLU 四元数，**不再做轴交换**。

如果某个模型资产按 glTF 约定（+Y up、+Z forward）制作，只在模型节点上加一个固定的前置旋转 `R_model = R_x(+90°)·R_z(…)`，在导入资产时一次性烘焙，运行时不再处理。

若确需 Y-up 场景（例如复用第三方 Y-up 组件），映射为 `three = (x_e, z_u, −y_n)`。验证结果：yaw_ned=0（机头朝北）时机头指向 −Z；yaw_ned=90° 时指向 +X。

**目标点的坐标系选择（重要）**

- PX4 的 LOCAL_NED 原点是**各机自己的 EKF 原点**（GPS_GLOBAL_ORIGIN），投影是 azimuthal equidistant（`src/lib/geo/geo.h::MapProjection`），z 取海拔差。
- 因此：
  - **状态**：用 GLOBAL_POSITION_INT 做精确的大地坐标换算，得到 World ENU；
  - **目标**：把 World ENU 转成 lat/lon/alt，再用 `set_position_global`（SET_POSITION_TARGET_GLOBAL_INT）或 `goto_location`（DO_REPOSITION）下发，**不要**自己推算各机的 local NED。
- degE7 的分辨率约为 1.1 cm，足够使用。

### 3.4 PX4 custom_mode 编解码（port）

编码规则：`custom_mode = (main_mode << 16) | (sub_mode << 24)`（`px4_custom_mode.hpp` 中的 union：reserved 为 u16，其后依次是 main 和 sub）。

| 模式 | main | sub | custom_mode（十进制） | MAVSDK FlightMode |
|---|---|---|---|---|
| MANUAL | 1 | 0 | 65536 | Manual |
| ALTCTL | 2 | 0 | 131072 | Altctl |
| POSCTL | 3 | 0 | 196608 | Posctl |
| AUTO.READY | 4 | 1 | 17039360 | Ready |
| AUTO.TAKEOFF | 4 | 2 | 33816576 | Takeoff |
| AUTO.LOITER（Hold） | 4 | 3 | 50593792 | Hold |
| AUTO.MISSION | 4 | 4 | 67371008 | Mission |
| AUTO.RTL | 4 | 5 | 84148224 | ReturnToLaunch |
| AUTO.LAND | 4 | 6 | 100925440 | Land |
| ACRO | 5 | 0 | 327680 | Acro |
| OFFBOARD | 6 | 0 | 393216 | Offboard |
| STABILIZED | 7 | 0 | 458752 | Stabilized |

设模式的方式：`COMMAND_LONG(MAV_CMD_DO_SET_MODE=176, p1=MAV_MODE_FLAG_CUSTOM_MODE_ENABLED(1), p2=main, p3=sub)`。解码时先检查 `base_mode & 1`。MockAdapter 直接输出这套数值，UI 用同一张表显示，本地化文案由前端提供。

### 3.5 命令微协议与 Gateway 命令状态机（port 自 MavlinkCommandSender）

```text
            submit(cmd)                         ACK=ACCEPTED
 IDLE ─────────────────> SENT(t0, tries=0) ───────────────────> DONE(ok)
                          │  │ ACK=IN_PROGRESS(p)                ^
                          │  └──────> PROGRESS(p, timeout=3.0s) ──┘
                          │ timeout(0.5s) & tries<3 → resend, tries++
                          │ timeout & tries==3      → DONE(TIMEOUT)
                          │ ACK∈{DENIED,UNSUPPORTED,FAILED,TEMP_REJECTED,CANCELLED} → DONE(result)
                          └ 同一 vehicle 同一 command id 若已在飞行 → 排队（串行）
```

- 参数：`timeout_s = 0.5`、`retries = 3`、`in_progress_timeout = 3.0`，最坏情况约 2.0 s。对应 UI 设计：命令按钮进入 loading 状态，最长 2.5 s 后回落并弹出 toast，文案由 MAV_RESULT 映射而来。
- MockAdapter 也要**模拟这套语义**：
  - 起飞前未解锁返回 DENIED；
  - 未设 setpoint 就切 OFFBOARD 返回 DENIED；
  - 可以配置 `ack_latency` 与 `drop_rate`，用于 UI 压测。
- 统一的结果类型：`CommandResult{status: ACCEPTED|IN_PROGRESS|DENIED|UNSUPPORTED|FAILED|TEMPORARILY_REJECTED|CANCELLED|TIMEOUT|NO_SYSTEM|BUSY|CONNECTION_ERROR, progress?: float, detail?: str}`。

### 3.6 Mission 微协议与 MissionItem 展开（port）

```text
上传（客户端 → 自驾仪）
 GCS: MISSION_COUNT(count, type)         ──>
                                        <── AP: MISSION_REQUEST_INT(seq=0)
 GCS: MISSION_ITEM_INT(seq=0)            ──>
   ...（AP 按需请求 seq；收到比期望更小的 seq 即视为重传请求，计入 retries）
                                        <── AP: MISSION_ACK(type=ACCEPTED|错误码)
 超时：MAVSDK 用 timeout_s（0.5s）且 retries=5；MAVROS 单条 1s、列表 30s、重试 3 次
执行：MAV_CMD_MISSION_START(300) 或 set_flight_mode(Mission)；进度：MISSION_CURRENT / MISSION_ITEM_REACHED
```

展开规则（`convert_to_int_items`，MockAdapter 的 FollowPath 与 Mission 共用）：

```python
def expand(items):                               # items: 我们的 Waypoint(lat,lon,rel_alt,speed?,hold_s?,accept_r?,yaw?,action?)
    out = []
    for it in items:
        if it.action == "takeoff": out.append(Item(NAV_TAKEOFF, GLOBAL_RELATIVE_ALT_INT, x=lat7, y=lon7, z=rel_alt))
        elif valid(it.lat, it.lon):
            out.append(Item(NAV_WAYPOINT, GLOBAL_RELATIVE_ALT_INT, p1=it.hold_s or 0, p2=it.accept_r or NAV_ACC_RAD,
                            p4=it.yaw_deg if isfinite else NaN, x=round(lat*1e7), y=round(lon*1e7), z=rel_alt))
        if isfinite(it.speed): out.append(Item(DO_CHANGE_SPEED, MISSION, p1=1(ground), p2=speed, p3=-1))
        if it.action == "land": out.append(Item(NAV_LAND, ...)); if it.action == "rtl": out.append(Item(NAV_RETURN_TO_LAUNCH, ...))
    out[0].current = 1
    return out
```

参数：PX4 的 `NAV_ACC_RAD` 默认 10 m，`MPC_XY_CRUISE` 默认 5 m/s。**在城市场景里 10 m 的接受半径太大**，Gateway 下发任务时应当显式设置每个航点的 `acceptance_radius_m`（建议 1–2 m）。

### 3.7 Offboard 流与 FollowPath 策略（port）

MAVSDK 各模式的 type_mask（`offboard_impl.cpp`）：

| API | 消息 / 坐标系 | type_mask（十进制） | 生效字段 |
|---|---|---|---|
| `set_position_ned` | SET_POSITION_TARGET_LOCAL_NED / LOCAL_NED(1) | 2552（0x09F8） | xyz + yaw |
| `set_position_global` | SET_POSITION_TARGET_GLOBAL_INT / GLOBAL_INT、REL_ALT_INT、TERRAIN_ALT_INT | 2552 | lat/lon/alt + yaw |
| `set_velocity_ned` | LOCAL_NED | 2503（0x09C7） | vxyz + yaw |
| `set_position_velocity_ned` | LOCAL_NED | 2496（0x09C0） | xyz + vxyz + yaw（前馈） |
| `set_position_velocity_acceleration_ned` | LOCAL_NED | 2048（0x0800） | xyz + v + a + yaw |
| `set_acceleration_ned` | LOCAL_NED | 3135 | a |
| `set_velocity_body` | BODY_NED(8) | 1479 | vxyz + yaw_rate |
| `set_attitude` / `set_attitude_rate` | SET_ATTITUDE_TARGET | — | q/rates + thrust |

保活与安全：
- MAVSDK 以 20 Hz 自动重发最近一次的 setpoint；PX4 在 `COM_OF_LOSS_T`（1 s）内收不到就触发 failsafe，由 `COM_OBL_RC_ACT` 决定进入 Hold/RTL/Land。
- MAVSDK 的看门狗在模式被切走 3 s 后停止发送。
- 进入 OFFBOARD 的前置条件：先 `set_*` 一次，再 `start()`。

FollowPath 有两种实现，由 Gateway 根据能力选择：

1. **Mission 路线**（稳妥）：航点列表 → §3.6 展开 → upload → start。由自驾仪负责平滑、接受半径与失联处理，网络抖动不影响飞行。适合预规划航线和 UI 画出来的航线。
2. **Offboard 流**（动态）：由 Gateway 的轨迹跟踪器每 50 ms 计算 carrot 点，经 `set_position_velocity_ned` 或 `set_position_global` 下发。适合规划器、ANet 实时改航、编队。

   pure pursuit 取点：`s* = s_closest + L`，其中 `L = clamp(k·|v|, 2 m, 15 m)`，`k = 0.8 s`。前馈速度取 `v_ff = v_des·t̂(s*)`。编队时每机的目标为 `p_i = p_leader + R(yaw_leader)·offset_i`。

Orbit：PX4 直接用 `MAV_CMD_DO_ORBIT`（半径、速度，yaw_behavior 可为朝向圆心等）；Mock 和 Prometheus 用 offboard 流实现：`θ̇ = v/R`，`p = c + R(cosθ, sinθ)`，`v_ff = v(−sinθ, cosθ)`。

### 3.8 Heartbeat、连接看门狗与链路质量

- 发送：Gateway 作为 GCS（sysid 245 / compid 190，与 QGC 的 255 区分）以 1 Hz 发送 HEARTBEAT（`MAV_TYPE_GCS`，`MAV_AUTOPILOT_INVALID`）。PX4 靠它判定 GCS 链路是否丢失（`NAV_DLL_ACT`）。
- 接收：每机记录 `last_hb`，`age > 3 s` 视为 `disconnected`，与 MAVSDK 的默认值一致；MAVROS 的默认值为 10 s。UI 上：
  - 1.5 s 以内显示“在线”；
  - 1.5–3 s 显示“延迟”，状态点变黄；
  - 超过 3 s 显示“失联”，状态点变红，DroneLayer 模型半透明并停在最后位置（不要瞬移）。
- 链路质量：`drop_pct = SYS_STATUS.drop_rate_comm/100`；也可以按 `seq` 的缺口自行统计：`loss = 1 − received/expected`，其中 expected 由相邻两帧 seq 的差值（mod 256）累计得到。RTT 取 TIMESYNC 的测量值。

### 3.9 Timesync 滤波器（port 自 MAVROS `sys_time.cpp`）

用途：V0.5 真机对齐 `time_boot_ms` 与 Gateway 时钟，保证时间轴回放和多机对时一致。

```python
# 参数（MAVROS 默认）：alpha 0.05→0.003, beta 0.05→0.003, convergence_window 500,
# max_rtt_sample 10ms, max_deviation_sample 10ms, max_consecutive_high_deviation 10, rate 10Hz
def on_timesync(tc1, ts1, now_ns):
    if tc1 == 0: send(TIMESYNC(tc1=now_ns, ts1=ts1)); return        # 对端请求，回应
    offset = (ts1 + now_ns - 2*tc1) // 2                                # 对称 RTT 假设
    rtt = now_ns - ts1
    if rtt >= max_rtt: high_rtt += 1; return
    if converged() and abs(offset_est - offset) > max_dev:
        high_dev += 1
        if high_dev > 10: reset()                                       # 时间跳变
        return
    if not converged():                                                 # sigmoid 增益调度
        prog = seq / window; p = 1 - exp(0.5*(1 - 1/(1 - prog)))
        a = p*a_f + (1-p)*a_i; b = p*b_f + (1-p)*b_i
    else: a, b = a_f, b_f
    prev = offset_est
    offset_est = offset if seq == 0 else a*offset + (1-a)*(offset_est + skew)   # 双指数平滑
    skew = b*(offset_est - prev) + (1-b)*skew
    seq += 1
```

### 3.10 MAVLink 分发路由（port 自 MAVROS Router）

Gateway 用它把 PX4 的 MAVLink 同时分发给 MavsdkAdapter、QGC（调试）、录制器和 Foxglove 旁路，替代 mavlink-router 这类外部进程。

```python
addr = lambda sys, comp=0: (sys << 8) | comp
on_rx(ep, msg):
    for a in (addr(msg.sysid), addr(msg.sysid, msg.compid)):
        if a not in ep.remote: ep.remote.add(a); index_dirty = True
    route(ep, msg)
route(src, msg):
    tgt = addr(msg.target_system or 0, msg.target_component or 0) if has_target(msg.msgid) else 0
    if index_dirty: rebuild index: addr -> [endpoints]
    dests = index.get(tgt, []) or index.get(0, [])           # 定向优先，未命中则广播
    for d in dests:
        if d is src or d.kind == src.kind: continue          # 不回发；同类不转发
        d.send(msg)
```

### 3.11 MockAdapter 动力学（MVP 核心，port 自 PX4 默认参数 + fake_px4）

采用向量化的质点运动学模型：位置环加上一阶速度跟踪，并加入限幅。对 UI 与任务层而言，它和 PX4 的“位置控制器 + 飞控”行为足够接近。

```text
状态 (N×3, World ENU, float64): p, v；yaw (N)；armed, mode(custom_mode), landed_state, battery_wh
目标：tgt_p（位置模式）或 tgt_v（速度模式）
v_des = sat_{v_max}( K_p · (tgt_p − p) )                   # 位置环；速度模式下 v_des = tgt_v
       水平 v_max = MPC_XY_VEL_MAX(12)；垂直上升 3.0、下降 1.5（MPC_Z_VEL_MAX_UP/DN）
a     = sat_{a_max}( (v_des − v)/τ  +  c_d·(W(p,t) − v) )  # τ=0.35 s；a_max 水平 5（MPC_ACC_HOR_MAX）
       W(p,t) = Environment.query(x,y,z,t) 的风（V0.4 起接入；V0.1–0.3 取 0）
       c_d = 0.25 1/s：风扰的一阶拖曳耦合，稳态偏差 ≈ c_d·|W|·τ（位置环会把它拉回）
v    += a·dt ; p += v·dt
yaw  += clamp(wrap(tgt_yaw − yaw), ±yaw_rate_max·dt)      # yaw_rate_max = 1.5 rad/s（约 MPC_YAWRAUTO_MAX 量级）
地面约束：z ≥ z_ground(x,y)（V0.1 取 0；V0.3 起用 World 高度图或 voxel 查询）
起降状态机：ON_GROUND ─arm+takeoff→ TAKEOFF(tgt z=+MIS_TAKEOFF_ALT 2.5m, v_up=MPC_TKO_SPEED 1.5)
           ─ z≥0.95h → IN_AIR ─land→ LANDING(v_dn=MPC_LAND_SPEED 0.7) ─ z≤0.05 & |vz|<0.1 → ON_GROUND
           ─ 落地后 COM_DISARM_LAND=2s 自动 disarm
Offboard：未设 setpoint 就 start → DENIED；setpoint 超过 COM_OF_LOSS_T=1s 未更新 → 切 Hold
电池（占位模型，需用 P600 日志标定）：P = P_hover·(1 + k_v·|v_air|²/v_ref²) ；battery_wh −= P·dt/3600
积分步长：物理 100 Hz（dt=10 ms）；状态发布 20–30 Hz；时间倍率 ×1/×2/×5/×10 = 每墙钟 tick 执行 k 次子步
```

实测（`mock_step_bench.py`，numpy 2.5）：N=10 时 78 µs/步（numpy 固定开销为主），N=1000 时 363 µs/步，**100 Hz 下 1000 架只占 3.6% 核**；N=5000 时 2.0 ms/步（20%）。因此 V0.6 的百架乃至千架机群都不需要 C++。

**swarm-lite 帧**（N > 50 时使用；少量飞机仍用 r15 定义的全精度 DroneState 帧）

```text
header: opcode u8 | channel u16 | t_sim_ns u64 | count u16
record (30 B, little-endian):
  id u16 | pos f32×3 (World ENU, 米, 10 km 内误差 < 1 mm, r15) | q i16×4 (snorm, [x,y,z,w]) |
  vel i16×3 (cm/s, ±327 m/s) | battery u8 (%) | mode u8 (本地枚举：0=GROUND,1=TAKEOFF,2=HOLD,3=MISSION,4=OFFBOARD,5=RTL,6=LAND,7=LOST)
```

N=1000 时单帧 30 KB，打包耗时 200 µs（numpy structured array + `tobytes()`）。30 Hz 下带宽约 0.9 MB/s，局域网可用；公网下降到 10 Hz，并对视锥外的飞机降频。

### 3.12 MAVSDK v4 的多机用法（adopt 时必须照做）

```python
import asyncio, concurrent.futures
from mavsdk.asyncio import Mavsdk, Configuration, ComponentType
from mavsdk.asyncio.plugins.telemetry import TelemetryAsync

class Latest:                                   # latest-value mailbox，替代无界 Queue
    __slots__ = ("val", "ev")
    def __init__(self): self.val = None; self.ev = asyncio.Event()
    def put(self, v): self.val = v; self.ev.set()

async def worker(ports, sysid=245):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=8 * len(ports) + 16))  # R3
    cfg = Configuration.create_manual(sysid, 190, True)          # 显式 GCS id，避免与 QGC 冲突
    async with Mavsdk(cfg) as m:
        for p in ports: await m.add_any_connection(f"udpin://0.0.0.0:{p}")   # PX4: 14540+i；≥10 架共用 14549
        vehicles = {}
        async for _ in m.on_new_system():                        # 也可以轮询 get_systems()
            for s in await m.get_systems():
                sid = await s.get_system_id()
                if sid not in vehicles and await s.has_autopilot():
                    vehicles[sid] = VehicleHandle(s)             # 内含 ActionAsync/OffboardAsync/MissionAsync/TelemetryAsync/ParamAsync
                    loop.create_task(vehicles[sid].start_streams())
```

`VehicleHandle.start_streams()` 的要点：
1. 先调 `set_rate_position(15)`、`set_rate_attitude_quaternion(20)`、`set_rate_battery(1)`，再订阅；
2. 每个订阅单独一个 task，`async for x in tel.subscribe_*(): mailbox.put(x)`；
3. 融合器按 Gateway 自己的 tick（例如 30 Hz）读取各 mailbox 的最新值，拼成 DroneState，**绝不在回调里做重活**，也**不在回调里调用阻塞 API**（THREADING.md 的规则）；
4. 在 `System.is_connected_state()` 上监听掉线；
5. 命令统一走 §3.5 的状态机，每个 vehicle 一把 `asyncio.Lock` 串行化，防止同一机同时下发 arm 与 takeoff 这类竞争命令。

分片策略：io 线程开销 0.24 ms/条。按“每架 3 路 × 15 Hz + 慢速约 5 条/s ≈ 50 条/s”估算，每架约占 1.2% 核，单进程 16 架约 20% 核，留足余量。超过 16 架就启动多个 worker 进程，端口分段，通过 ZeroMQ 或 asyncio 队列汇总到 Gateway 主进程。

### 3.13 `fake_px4`：MAVLink 测试替身与 MavlinkFacade 原型（`fake_px4.py`，adopt 为测试基建）

- 单个 asyncio 进程可跑 N 架，每架一个 UDP socket，向 `127.0.0.1:base+i` 发送，并学习对端地址，与 PX4 `-o` 的行为一致。
- 实现：
  - HEARTBEAT（PX4 custom_mode）
  - SYS_STATUS（含 PREARM_CHECK 位，否则 MAVSDK 报 not armable）
  - GLOBAL_POSITION_INT、ATTITUDE_QUATERNION、LOCAL_POSITION_NED、EXTENDED_SYS_STATE、BATTERY_STATUS、HOME_POSITION
  - 命令：ARM/DO_SET_MODE/NAV_TAKEOFF/NAV_LAND/DO_REPOSITION/MISSION_START/SET_MESSAGE_INTERVAL/REQUEST_MESSAGE（HOME、AUTOPILOT_VERSION），其余回 UNSUPPORTED
  - SET_POSITION_TARGET_LOCAL_NED（按 mask 区分位置和速度）
  - Mission 上传，以及 offboard 失联后切 Hold
  - `--mute K`：前 K 架不回命令，用于故障注入
- 用途：
  1. **CI 集成测试**：MavsdkAdapter 不依赖 PX4 就能跑通全链路，已用它完成 §0 的全部实测；
  2. **MavlinkFacade**（V0.3+ 可选）：把 fake_px4 的动力学替换为 MockAdapter 的状态，Mock 机群就能被 QGC、Foxglove、MAVSDK 脚本直接连接，研究人员的外部控制脚本可以在 Mock 与 PX4 之间零改动切换。

  MAVSDK 的 Server 插件（TelemetryServer、ActionServer、MissionRawServer）也能扮演自驾仪，但**没有 Offboard 与 DO_REPOSITION 的服务端**，所以 Facade 仍以 pymavlink 实现为主。

### 3.14 Environment → PX4 风扰（SIH 路线，V0.4）

```python
# 每 0.5–1 s 更新一次（参数写入走 PARAM_SET 微协议，频率不宜过高）
w = environment.query(*drone.position_enu, t)          # World ENU 风矢量（空气运动方向，"吹向"）
await ParamAsync(sys).set_param_float("SIH_WIND_N", -w.north)   # SIH: v_apparent = v + wind_param（"来自"语义）
await ParamAsync(sys).set_param_float("SIH_WIND_E", -w.east)
```

- 依据：`simulator_sih/sih.cpp`：`_v_wind_N = (SIH_WIND_N, SIH_WIND_E, 0)`，`_v_apparent_N = _v_N + _v_wind_N`；`_parameter_update_sub.updated()` 触发 `parameters_updated()`，所以运行时修改有效。
- 局限：
  - 只有水平分量；
  - 每机只有一个均匀值，适合 Level 0/1（常值加阵风，由 Gateway 按机位采样）；
  - Level 2/3 的空间场需要 Gazebo 风插件或自研动力学。
- `MAV_CMD_EXTERNAL_WIND_ESTIMATE`（development.xml 43004）只是**给 EKF 的估计提示**，不是物理风，**不要**拿它注入风扰。

---

## 4. 在本项目中的落点与复用方式

### 4.1 DroneAdapter 接口（Simulation Gateway 的唯一下游抽象）

```python
# apps/api/sim/adapters/base.py
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Protocol, Sequence

class AdapterKind(str, Enum): MOCK="mock"; MAVSDK="mavsdk"; PROMETHEUS="prometheus"; REPLAY="replay"

@dataclass(frozen=True)
class AdapterCaps:
    spawn: bool                 # 能否创建新机（mock / SITL 可以；真机只能 attach）
    offboard_position: bool
    offboard_velocity: bool
    mission: bool
    orbit: bool
    wind_injection: bool        # mock 支持完整 3D 场；SIH 只支持水平均匀；真机不支持
    time_control: bool          # pause / 倍率 / 单步（mock、replay）
    ground_truth: bool          # 能否拿到真值（mock / SITL）
    max_vehicles: int
    state_rate_hz: float

@dataclass
class VehicleSpec:
    vid: str                    # "uav-01"
    model: str = "p600"         # 机型 → 参数表（质量、限速、电池等）
    spawn_enu: tuple[float, float, float] = (0, 0, 0)
    spawn_yaw: float = 0.0
    capabilities: list[str] = field(default_factory=list)   # "rgb.zoom","thermal.imaging",...（ANet 用）
    link: dict = field(default_factory=dict)                # mavsdk: {"url":"udpin://0.0.0.0:14541","sysid":2}

# ---- 命令（与 MAVLink 语义一一对应；坐标全部为 World ENU / 米 / 弧度） ----
@dataclass
class Arm: force: bool = False
@dataclass
class Disarm: force: bool = False
@dataclass
class Takeoff: alt_m: float = 2.5
@dataclass
class Land: pass
@dataclass
class Hold: pass
@dataclass
class ReturnHome: pass
@dataclass
class GoTo: target_enu: tuple[float,float,float]; yaw: float | None = None; speed: float | None = None
@dataclass
class Orbit: center_enu: tuple[float,float,float]; radius_m: float; speed: float; clockwise: bool = True
@dataclass
class FollowPath: waypoints: Sequence["Waypoint"]; mode: str = "auto"   # auto|mission|offboard
@dataclass
class SetSpeed: speed: float
@dataclass
class Kill: pass
Command = Arm | Disarm | Takeoff | Land | Hold | ReturnHome | GoTo | Orbit | FollowPath | SetSpeed | Kill

@dataclass
class Setpoint:                          # 流式控制（offboard），latest-wins，不排队
    pos_enu: tuple | None = None; vel_enu: tuple | None = None; acc_enu: tuple | None = None
    yaw: float | None = None; yaw_rate: float | None = None

class ResultStatus(str, Enum):
    ACCEPTED="accepted"; IN_PROGRESS="in_progress"; DENIED="denied"; UNSUPPORTED="unsupported"
    FAILED="failed"; TEMPORARILY_REJECTED="temporarily_rejected"; CANCELLED="cancelled"
    TIMEOUT="timeout"; NO_VEHICLE="no_vehicle"; BUSY="busy"; LEASE_DENIED="lease_denied"; LINK_ERROR="link_error"

@dataclass
class CommandResult: status: ResultStatus; progress: float | None = None; detail: str = ""; latency_ms: float = 0

class DroneAdapter(Protocol):
    kind: AdapterKind
    caps: AdapterCaps
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def spawn(self, spec: VehicleSpec) -> str: ...          # mock/SITL: 创建；真机: attach + 发现
    async def despawn(self, vid: str) -> None: ...
    def vehicles(self) -> list[str]: ...
    def snapshot(self) -> "StateBatch": ...                        # 非阻塞，最新值，World ENU
    def events(self) -> AsyncIterator["VehicleEvent"]: ...         # connected/lost/mode/landed/mission_item/statustext/failsafe
    async def command(self, vid: str, cmd: Command) -> CommandResult: ...
    async def set_setpoint(self, vid: str, sp: Setpoint) -> None: ...   # 首次调用隐含 offboard 准备；start 由 command 触发
    async def set_wind(self, vid: str, wind_enu: tuple[float,float,float]) -> None: ...  # caps.wind_injection
    async def set_time_scale(self, k: float) -> None: ...          # caps.time_control
```

设计要点：

1. **Adapter 只做翻译，不做决策。** 轨迹跟踪（pure pursuit）、编队、避碰、Control Lease 仲裁、命令重试策略都放在 Gateway 层，这样三个 Adapter 的行为一致。唯一例外是 FollowPath 的 `mode="auto"`：Adapter 根据自身能力选择 mission 还是 offboard。
2. **状态是拉取式（`snapshot()`）+ 事件推送式（`events()`）。** Gateway 的 publisher 按自己的 tick（10–30 Hz）拉取，天然就是“最新值”语义，与 Lichtblick 的 `latest-per-render-tick`（r15）一致。
3. **命令结果统一为 MAV_RESULT 语义**，另加 TIMEOUT、LEASE_DENIED 等传输和仲裁层状态。前端 shadcn 的 `Sonner` toast 与按钮 loading 状态直接基于 `ResultStatus` 映射文案。
4. **所有坐标都是 World ENU。** NED、lat/lon、Prometheus 的 ENU（以各机原点为基准）都在 Adapter 内部转换，转换函数是纯函数，单测覆盖 §3.3 的验证用例。

### 4.2 各 Adapter 的实现要点

#### 4.2.1 MockAdapter（V0.1 MVP，必做）

- 内核：§3.11 的 numpy 向量化模型，一个 asyncio task 以 100 Hz 物理步长推进。与 Environment 服务同进程，直接调用 `environment.query`（V0.4）。
- 命令映射：
  - Arm/Takeoff/Land/Hold/RTL/GoTo/Orbit/FollowPath 全部内部实现，行为参数取 PX4 默认值，结果语义模拟 MAV_RESULT；
  - `ack_latency_ms`（默认 20）、`drop_rate`、`deny_rules` 可以配置，用于 UI 压测与故障演示。
- 能力：`spawn`、`time_control`（暂停、倍率、单步）、`wind_injection`（完整 3D）、`ground_truth` 均为 True；`max_vehicles` = 5000。
- 碰撞：V0.3 起查询 World 的 occupancy/voxel（点云体素化）。碰撞时发出 `event: collision`，并把该机置为 `CRASHED`（UI 显示红色）。

#### 4.2.2 MavsdkAdapter（V0.2，PX4 SITL/SIH/真机 PX4）

- 依赖：`mavsdk==4.0.0`，使用 asyncio API，按 §3.12 的模式使用。
- 进程模型：Gateway 主进程加 K 个 `mavsdk-worker` 子进程，每个最多 16 架，经本地 IPC 汇总。单机 10 架以内可以同进程。
- 命令映射：

| 统一命令 | MAVSDK 调用 | MAVLink | 备注 |
|---|---|---|---|
| Arm / Disarm | `action.arm()` / `disarm()`；force 用 `arm_force()` | CMD 400 | PX4 在 Mission/RTL/Land 下会先切 Hold |
| Takeoff(alt) | `action.set_takeoff_altitude(alt)` → `takeoff()` | PARAM_SET MIS_TAKEOFF_ALT → CMD 22 | alt 为相对高度 |
| Land / Hold / ReturnHome | `land()` / `hold()` / `return_to_launch()` | CMD 21 / DO_SET_MODE / DO_SET_MODE | — |
| GoTo(enu) | `enu→llh`，`goto_location(lat, lon, alt_amsl, yaw_deg)` | CMD_INT 192 DO_REPOSITION | **alt 为 AMSL**：`alt_amsl = anchor_h_msl + z_enu`。V0.5 要处理椭球高与 EGM2008 的差 N，见 r15 |
| Orbit | `do_orbit(r, v, yaw_behavior, lat, lon, alt_amsl)` | CMD 34 | PX4 支持 |
| FollowPath(mission) | `mission.upload_mission(plan)` → `start_mission()` | §3.6 | 显式设接受半径 1–2 m |
| FollowPath(offboard) / Setpoint | `offboard.set_position_global(...)` 或 `set_position_velocity_ned` → `start()` | 86 / 84 | 20 Hz 保活由 MAVSDK 负责 |
| SetSpeed | `action.set_current_speed(v)` | CMD 178 | — |
| set_wind（SIH） | `param.set_param_float("SIH_WIND_N/E", −w)` | PARAM_SET | 仅 SIH；0.5–1 Hz |
| Kill | `action.kill()` | CMD 400，param2 = 21196 | 需要二次确认 |

- 状态：GLOBAL_POSITION_INT、ATTITUDE_QUATERNION、BATTERY、FlightMode、LandedState、Health、Armed，外加 `ground_truth`（HIL_STATE_QUATERNION，仅 SITL，用于 UI 的“估计 vs 真值”对比）。
- PX4 SIH 启动模板（给 PX4 单元参考）：`PX4_SIM_MODEL=sihsim_quadx PX4_HOME_LAT=… PX4_HOME_LON=… PX4_HOME_ALT=… ./bin/px4 -i $i`。第 i 个实例：`MAV_SYS_ID = i+1`，offboard 端口 `14540+i`（i ≥ 10 时统一为 14549），GCS 端口 `18570+i`。
  - **Home 必须设在 World 锚点附近**，否则 World ENU 的曲率误差会增大；多机按 spawn 偏移逐个设置 `PX4_HOME_*`。

#### 4.2.3 PrometheusAdapter（V0.5，P600 真机与 ProSim）

事实：
- Prometheus（阿木，P600 软件栈）是 **ROS1**（`uav_control_node.cpp` 使用 `ros::NodeHandle`），底层通过 MAVROS 连 PX4。
- 地面站通信模块 `Modules/communication` 的协议编解码在闭源的 `shard/libs/libcommunication_{x86_64,aarch64}.so` 里，`MsgId` 枚举见 `shard/include/Struct.hpp`（UAVSTATE=1、HEARTBEAT=6、UAVCOMMAND=108、UAVSETUP=109 等），无法直接实现。

接口（`Modules/common/prometheus_msgs/msg/`）：

| 话题 | 类型 | 要点 |
|---|---|---|
| `/uav{N}/prometheus/command` | `UAVCommand` | `Agent_CMD`：Init_Pos_Hover=1、Current_Pos_Hover=2、Land=3、Move=4、User_Mode=5。`Move_mode`：XYZ_POS=0、XY_VEL_Z_POS=1、XYZ_VEL=2、XYZ_POS_BODY=3、XYZ_VEL_BODY=4、XY_VEL_Z_POS_BODY=5、TRAJECTORY=6、XYZ_ATT=7、LAT_LON_ALT=8。参考量：`position_ref[3]`、`velocity_ref[3]`、`acceleration_ref[3]`、`yaw_ref`、`latitude/longitude/altitude`（f64）。`Control_Level`：DEFAULT=0、ABSOLUTE=1、EXIT_ABSOLUTE=2。**`Command_ID` 必须递增** |
| `/uav{N}/prometheus/state` | `UAVState` | `connected`、`armed`、`mode`（字符串）、`location_source`（含 MID360=10、RTK=5、ProSim=12）、`position[3]`（ENU，MAVROS 局部）、`velocity[3]`、`attitude_q`、`battery_state`（V）、`battery_percetage`（0–1）。**`latitude/longitude` 为 float32**：在合肥或深圳一带经度的量化步长约 0.8 m，定位必须改用局部 ENU 加原点 |
| `/uav{N}/prometheus/setup` | `UAVSetup` | `cmd`：ARMING=0、SET_PX4_MODE=1（`px4_mode` 字符串，如 "OFFBOARD"、"AUTO.LAND"）、REBOOT=2、SET_CONTROL_MODE=3（"COMMAND_CONTROL" 等） |
| `/uav{N}/prometheus/control_state` | `UAVControlState` | INIT、RC_POS_CONTROL、COMMAND_CONTROL、LAND_CONTROL；`failsafe` |

传输有两种方式，推荐 (a)：
- **(a)** 机载或地面 ROS1 主机上运行 `rosbridge_server`，Gateway 用 `roslibpy` 走 WebSocket JSON 收发上面四个话题。
- **(b)** 自写一个 ROS1 小节点，经 ZeroMQ 转发。

遥测还可以另开一条**被动 MAVLink 旁路**：配置 MAVROS 的 `gcs_url`（例如 `udp-b://@14550`），由 §3.1 的轻量解码器或 MavsdkAdapter 以**只读模式**获取 PX4 原始高频数据，用于回放与数据集采集。

命令映射（**需在 ProSim 或真机上验证**）：
- Takeoff：`UAVSetup(ARMING, arming=true)` → `UAVSetup(SET_CONTROL_MODE, "COMMAND_CONTROL")` → `UAVCommand(Init_Pos_Hover)`；
- GoTo：`UAVCommand(Move, XYZ_POS, position_ref = enu − origin_uavN)`；
- Hold：`Current_Pos_Hover`；
- Land：`UAVCommand(Land)`；
- Orbit/FollowPath：由 Gateway 以 10–20 Hz 发 `Move XYZ_POS`，配合前馈 `velocity_ref`，或者使用 TRAJECTORY 模式。

**仲裁**：真机上 Prometheus 的 `uav_control` 是唯一向 PX4 发 OFFBOARD setpoint 的一方，Gateway **只能通过 UAVCommand 下达意图**，不能同时经 MAVSDK 发 offboard（R10）。

#### 4.2.4 ReplayAdapter（V0.2 起，与时间轴一起做）

数据源：
- PX4 `.ulg`（pyulog）；
- MAVLink `.tlog`（§3.1 解码器，或 pymavlink 的 `mavutil.mavlink_connection("x.tlog")`）；
- Gateway 自己录的 MCAP（r15 §3.19）。

`caps.time_control = True`。状态查询走 r15 的 IterablePlayer 语义（seek 时 backfill，按 tick 取时间窗）。命令一律返回 `UNSUPPORTED`。

### 4.3 Simulation Gateway 架构

```text
                     REST (FastAPI)                       WebSocket (binary, r15 协议)
Browser ───────────────────────────> Gateway API <──────────────────────────────── Browser
                                       │   ^ state frames 10–30 Hz / events
                  ┌────────────────────┼───┴──────────────────────────────────────┐
                  │  Vehicle Registry  │  Control Lease (owner, priority, ttl)     │
                  │  Command Engine (§3.5 状态机, per-vehicle lock, idempotency)   │
                  │  Path/Formation Tracker (§3.7, 20 Hz)                          │
                  │  State Fuser (mailbox → DroneState World ENU, §3.2/3.3)        │
                  │  Recorder (MCAP) · Metrics · MAVLink Router (§3.10, 可选)       │
                  └───────┬──────────────┬──────────────────┬──────────────────────┘
                          │              │                  │
                   MockAdapter     MavsdkAdapter        PrometheusAdapter      ReplayAdapter
                   (in-proc numpy) (worker procs ≤16/进程) (roslibpy→rosbridge) (ulog/tlog/mcap)
                          │              │ MAVLink/UDP        │ ROS1 topics
                   Environment      PX4 SITL/SIH ×N       P600 (Prometheus+MAVROS+PX4)
```

**Control Lease**（参考 Prometheus 的 Control_Level 与 MAVLink 的 REQUEST_OPERATOR_CONTROL）：

```python
lease = {vid: Lease(owner="ui:alice" | "mission:42" | "anet:agent-7", priority=0..3, expires=t+ttl)}
acquire(vid, owner, prio): 允许条件：无租约 / 已过期 / 本 owner 续约 / prio > 当前 prio（抢占，通知原 owner）
command(vid, cmd, owner): 非持有者 → LEASE_DENIED；安全类命令（Land/Hold/RTL/Kill）对任何已认证用户放行（安全优先）
setpoint 流：仅持有者可写；lease 过期或丢失 → Tracker 停止输出 → PX4 在 COM_OF_LOSS_T 后自动 Hold
```

### 4.4 版本落点

| 版本 | 本单元交付 |
|---|---|
| **V0.1** | 实现 `DroneAdapter` 协议与 `MockAdapter`（§3.11，PX4 语义）；统一 Command/Result/DroneState；swarm-lite 帧；UI 的命令 toast 与 loading 状态按 `ResultStatus` 实现；`fake_px4.py` 进入 `tests/` |
| **V0.2** | `MavsdkAdapter`（mavsdk 4.0.0 + §3.12 模式）；PX4 SIH 多实例脚本（与 PX4 单元协作）；命令状态机；ReplayAdapter（tlog/ulog） |
| **V0.3** | MavlinkFacade（Mock 暴露为 MAVLink，QGC 可接入）；MAVLink Router 旁路（QGC/Foxglove/录制） |
| **V0.4** | Environment → Mock 风（完整 3D）/ SIH 风（水平均匀，参数注入）；WIND_COV 与环境场对比面板 |
| **V0.5** | PrometheusAdapter（rosbridge）；timesync 滤波器（§3.9）；GCS 旁路被动解码；大地坐标与 AMSL/椭球高处理 |
| **V0.6** | 多 worker 分片（>16 架）；编队、FollowPath offboard 流；ADSB/COLLISION/UTM 消息用于冲突展示；Control Lease 多用户 |
| **V1.0** | ANet agent 作为 lease owner；能力声明（VehicleSpec.capabilities）；如需在 MAVLink 链路上承载自定义消息，用 `MavlinkDirect.load_custom_xml` 加私有方言（ID 段选在 `all.xml` 未分配区间，并登记） |

---

## 5. 对比与推荐

### 5.1 控制与遥测链路选型对比

| 维度 | **MAVSDK v4（原生）** | mavsdk-grpc（旧 MAVSDK-Python） | pymavlink 直连 | MAVROS（ROS2） | PX4 uXRCE-DDS（ROS2 原生） |
|---|---|---|---|---|---|
| 进程与依赖 | 一个 pip 包（46 MB .so） | pip + 每机一个 `mavsdk_server` 子进程 + grpcio | 纯 Python | ROS2 + colcon + GeographicLib | ROS2 + px4_msgs + MicroXRCEAgent |
| 多机 | 一实例 N 架（按 sysid）yes | 每机一个 server 与 gRPC 端口 | 自行实现 | 每机一个 node 与 namespace | 每机一个 namespace（`uav_i`）+ 一个 agent |
| 高层语义（起飞、任务、offboard 保活、重试） | yes 完整 | yes 完整 | no 全部自写 | yes 较全（setpoint **不自动重发**） | no 很底层（`/fmu/in/trajectory_setpoint`、`offboard_control_mode` 须自己以 ≥2 Hz 发送；vehicle_command 自行处理） |
| 每条消息 CPU | 0.24 ms（io）+ 回调开销 | 更高（再加 gRPC 序列化与跨进程） | 22 µs | C++，低 | 低（二进制 CDR，无 MAVLink） |
| 坐标系 | NED（原样） | NED | NED | **ENU/FLU（自动转换）** | NED/FRD（px4_msgs） |
| 适配 PX4 新功能 | 快（官方维护） | 同左 | 需要跟进方言 | 快 | **最快**（PX4 内部 uORB 直出） |
| 适配 ArduPilot | yes | yes | yes | yes | no |
| 在本项目中的角色 | **Gateway 主链路** | 不用 | 测试替身、被动解码、Facade | P600 侧已有组件；ROS 生态对接 | V0.6+ 若引入 ROS2 规划器（ego-planner ros2 分支）时的机载链路 |

### 5.2 推荐排序

1. **MAVSDK（v4 原生绑定）** 5/5：2026-09 刚发布 v4，活跃度最高，与 Python/FastAPI 后端的契合度最高，多机原生支持。风险在于新绑定“hasn’t been tested as much”（`py/README.md`），通过 `fake_px4` 与 PX4 SIH 的双重集成测试兜底。
2. **MAVLink + pymavlink** 4/5：star 最多，是协议真值来源。pymavlink 适合做测试基建、Facade 和旁路解码，不作为主控制链路（高层微协议都要自己写）。
3. **MAVROS** 3/5：ROS2 分支活跃，frame_tf、Router、timesync 三块代码质量高，值得移植。但它会把 ROS 带进 Gateway，与原文“浏览器负责看、服务器负责算，底层可替换”的解耦目标相悖。只在 P600 侧作为既有组件对接。
4. mavsdk-grpc / mavsdk_server 0/5：被 v4 取代，不用。

---

## 6. 风险与注意事项

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| R1 | **MAVSDK v4 Python 绑定很新**（2026-09 发布 4.0.0，README 自述测试不如 gRPC 版充分）；ctypes 回调与 `destroy()` 顺序出错会导致 segfault | Gateway 崩溃 | 锁定 `mavsdk==4.0.0`；全部使用 `async with Mavsdk()`；插件对象随 VehicleHandle 一起释放；CI 跑 `fake_px4` 多机场景；保留 `mavsdk-grpc` 作为降级方案（接口相同的另一实现） |
| R2 | **每条消息 0.24 ms 的 io 开销**（libmav 二次解析加 JSON） | 单实例约 4000 条/s 就会饱和；20 架 × 50 Hz 时 CPU 超过 100% | `set_rate_*` 限到 10–20 Hz；每进程 ≤16 架；高频原始数据改走被动解码旁路 |
| R3 | **asyncio 线程池饥饿**：默认 `min(32, cpu+4)` 个线程；失联机的命令会阻塞 2 s | 健康机的控制延迟被拖到秒级（实测 2001 ms） | worker 启动时 `set_default_executor(ThreadPoolExecutor(8·N+16))`；命令外层再套 `asyncio.wait_for(…, 3.0)`；失联机的命令在 Gateway 层直接快速失败（`NO_VEHICLE`） |
| R4 | **订阅使用无界 `asyncio.Queue`** | 消费慢时内存增长、延迟累积 | latest-value mailbox（§3.12）；融合器按 tick 拉取 |
| R5 | **阻塞 API 在回调里调用会死锁**（THREADING.md 明确禁止） | Gateway 卡死 | 回调里只写 mailbox，命令只在 task 里发 |
| R6 | **sysid 上限 255**（扩展 sysid 尚未支持），同一网络里 GCS 的 sysid 冲突 | 超过约 250 架无法在同一 MAVLink 网络里区分；QGC 与 Gateway 抢同一 sysid | 超过 200 架时按网络或进程分域（每 worker 独立端口段与 sysid 空间），World 层使用字符串 vid；Gateway 固定 245，QGC 保持 255 |
| R7 | **GoTo 的高度基准**：`goto_location` 与 `do_orbit` 用 AMSL，Mission 用相对 home，Prometheus 用局部 ENU | 飞到地下或过高 | Adapter 内部统一由 World ENU 换算；V0.5 再引入 EGM2008 的 N 值（r15：合肥约 −4.6 m） |
| R8 | **local NED 与 World ENU 不等价**（各机 EKF 原点、AE 投影、z 为海拔差） | 多机相对位置出现 0.1–1 m 级偏差 | 状态走 GLOBAL_POSITION_INT 的大地坐标换算；目标走全局 setpoint；PX4 home 设在锚点附近 |
| R9 | **PX4 默认接受半径 NAV_ACC_RAD = 10 m** | 城市街区航线“切角”，擦碰建筑 | 每个航点显式设 1–2 m；或改用 offboard 精确跟踪 |
| R10 | **真机上多个控制源冲突**：Prometheus 的 `uav_control` 与 Gateway 同时发 offboard | 飞行不稳、模式乱跳 | 真机只走 UAVCommand；MAVLink 旁路设为只读；Control Lease |
| R11 | **Prometheus 是 ROS1 且协议闭源**（`libcommunication_*.so`）；`UAVState` 的经纬度为 float32 | 集成需要 ROS1 环境；经纬度精度约 0.8 m | rosbridge 方案；位置使用局部 ENU 加原点；地面站协议不做逆向 |
| R12 | **MAVROS 的 setpoint 不自动重发**（与 MAVSDK 相反） | 如果切到 MAVROS 路线而忘记持续发布，会触发 offboard failsafe | 在 Tracker 层统一 20 Hz 输出，不依赖下游自动重发 |
| R13 | **端口冲突**：多个团队或进程在同一台机器上占用 14540 段 | 收到陌生系统的 ACK（研究过程中确实出现过 “Received ack for not-existing command” 的串扰） | 研究和 CI 用 24540+ 端口段；部署时由 Gateway 统一分配端口；按 sysid 白名单过滤 |
| R14 | **SIH 风注入的局限**：只有水平分量、每机一个值、改参数频率有限 | Level 2/3 空间风场无法体现在 PX4 里 | SIH 用于 Level 0/1；Level 2+ 用 MockAdapter（完整 3D）或 Gazebo 风插件 |
| R15 | **ROS 依赖蔓延**：原文 §26/§36 把 ROS2 放在 Gateway 主路径上 | 部署复杂度上升，CI 需要 ROS 环境 | ROS 只作为可选 Adapter 的依赖，Gateway 本体保持纯 Python |
| R16a | **pymavlink 预生成方言落后于 XML**（2.4.50 缺 `MAV_RESULT_CANCELLED=6` 等新条目） | 测试替身和解码器遇到新枚举值时报 KeyError 或显示为未知 | 解码时对未知枚举值容错；需要时用 `python -m pymavlink.tools.mavgen --wire-protocol 2.0 --lang Python3 common.xml` 从 refs 的 XML 生成 |
| R16 | **MAVLink 2 签名未启用** | 真机链路可被伪造或注入 | V0.5 真机启用 MAVLink2 signing（PX4 `MAV_*_SIGN*`），Gateway 保管密钥；局域网之外使用 VPN 或 zenoh |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§26 链路图有误，需要改写。** 原文为 “Flight Dynamics → PX4 SITL → MAVLink → ROS / MAVSDK → Simulation Service”，建议改为：

   ```text
   PX4 SITL/SIH ──MAVLink/UDP──> Simulation Gateway ─> DroneAdapter{Mock | Mavsdk | Prometheus | Replay}
                         └─(可选) uXRCE-DDS ─> ROS2（规划器/感知）
   ```

   MAVSDK 直接消费 MAVLink，不需要经过 ROS。ROS2 是并行的可选旁路，不是上游。
2. **§33 “UAV API: MAVSDK” 需要细化版本。** 写明 **MAVSDK v4 原生 Python 绑定（`mavsdk==4.0.0`，无 gRPC、无 mavsdk_server）**，并记录多机方案（一实例多 system，≤16 架/进程分片），以免实现者按旧文档去启动 N 个 `mavsdk_server`。
3. **§35 “第一阶段 PX4 + Gazebo” 建议调整为 “V0.1 Mock → V0.2 PX4 SIH → V0.4+ Gazebo”。**
   - SIH 不需要 GPU 或 Gazebo，多实例启动快，也能在 CI 上跑；
   - Gazebo 只在需要传感器仿真（相机、LiDAR）或复杂风场时引入；
   - 原文的 “100~1000 Hz 物理” 在 SIH 中由 PX4 内部完成。
4. **§28 DroneState 字段需补全并规定坐标系**（与 r15 一致）：
   - 位置为 World ENU（f64）；
   - 姿态为 FLU 四元数 `[x,y,z,w]`；
   - `flight_mode` 用 PX4 custom_mode 数值加字符串；
   - 新增 `landed_state`、`armed`、`health{armable,gps,home}`、`link{rtt,drop,hb_age}`、`home_enu`、`mission{seq,total}`、`control_owner`、`source`（mock/sitl/real/replay）、`estimate_vs_truth`（仅 SITL）。
5. **§29 多机需要写明寻址与端口规划：**
   - 第 i 架：`sysid = i+1`，offboard 端口 `14540+i`（i ≥ 10 时为 14549，按 sysid 区分），GCS 端口 `18570+i`，DDS 命名空间 `uav_i`；
   - Gateway 的 GCS id 固定为 245；
   - 超过 200 架时分域；
   - 每架的 “Agent” 与 “Controller” 在 Gateway 层以 **Control Lease** 形式体现。
6. **§30 控制模式应给出实现映射表**（§4.2.2 的表）。另外：
   - FollowPath 区分 mission 与 offboard 两种实现；
   - Orbit 在 PX4 上直接使用 `DO_ORBIT`；
   - Hover 等价于 Hold；
   - 任务航点显式设置接受半径（PX4 默认 10 m 过大）。
7. **§36 数据流 “PX4 → ROS2 → Simulation Gateway → WebSocket” 改为 “PX4 → MAVLink → Gateway(Adapter) → State Fuser → WebSocket”。**
   - WebSocket 按 r15 的二进制帧格式，外加本文的 swarm-lite 30 B/架布局；
   - 补充“命令通道”的语义：REST 或 WS RPC，结果为 `ResultStatus`，异步进度通过事件推送。
8. **§37 频率表需要补充链路侧的实测约束：**
   - PX4 → Gateway 限到 10–20 Hz（`set_rate_*`）；
   - Offboard 由 MAVSDK 自动以 20 Hz 保活，PX4 的 `COM_OF_LOSS_T` 为 1 s；
   - Heartbeat 1 Hz，3 s 判定失联；
   - 命令最坏 2 s 超时；
   - Mock 物理步长 100 Hz。
9. **新增一节 “Simulation Backend 抽象（DroneAdapter）”**，放在 §35 与 §36 之间，内容即 §4.1 的接口与能力声明。这正好落实原文 §4.1 “底层仿真可切换，Web UI 不推倒重做”的原则。原文只有原则，缺少接口。
10. **§47 V0.4 “Wind → Drone Force” 需要明确分级落地：**
    - MockAdapter 支持完整 3D 风场耦合（§3.11 的 `c_d·(W−v)`）；
    - PX4 SIH 通过 `SIH_WIND_N/E` 注入每机的均匀水平风（注意“来自”语义要取负号）；
    - Gazebo 风插件用于 Level 2+；
    - UI 增加 “Environment 风 vs 自驾仪 WIND_COV 估计” 的对比图（lieflat-charts 风格）。
11. **§48 V0.5 需要补充时间同步与安全：**
    - TIMESYNC 滤波（§3.9，MAVROS 参数）；
    - MAVLink2 签名；
    - P600 的集成方式按 §4.2.3：rosbridge 加 UAVCommand，MAVLink 旁路只读；
    - 注意 Prometheus 是 ROS1，原文 §2 说“基于 ROS”，应明确是 ROS1 Noetic 与 MAVROS。
12. **§31–§32 ANet 在车辆链路上的承载需要明确：**
    - 能力声明与任务协商**走 Gateway 或 ANet 自己的消息总线**（WS/zenoh），不走 MAVLink；
    - MAVLink 只承载飞控语义；
    - 若将来确实需要机载 ANet 消息经数传转发，再用 MavlinkDirect 的自定义方言。
13. **原文 §42 repo 结构建议调整：**
    - `simulation/` 下增加 `adapters/{mock,mavsdk,prometheus,replay}`、`gateway/`（command engine、lease、tracker、fuser）、`testing/fake_px4.py`；
    - 删除 `simulation/isaac` 与 `gazebo` 的顶层地位，改为 `backends/px4-sih`、`backends/gazebo`（V0.4+）、`backends/isaac`（V1.0+）。
14. **UI 交互建议（与 UI PRD 联动）：**
    - 命令按钮三态：idle / pending（≤2.5 s）/ result toast；
    - 失联机的模型半透明并冻结，右侧卡片的状态点按在线、延迟、失联分为绿、黄、红（产品色中的红即 logo 红 #E93024）；
    - 控制租约在无人机卡片上显示 “控制者” 徽标，抢占时弹出确认对话框（shadcn `AlertDialog`）；
    - Kill 必须二次确认，并且不在快捷键里暴露。
