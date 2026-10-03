# R04 研究笔记：Livox-SDK2 / livox_ros_driver2 / LiDAR_IMU_Init —— MID-360 接入、标定与虚拟雷达建模

> 研究单元：r04　｜　日期：2026-09-28　｜　范围：MID-360(S) 数据包格式、点结构、非重复扫描、ROS2 话题；LiDAR-IMU 外参与时延标定；Sensor Simulation 中 MID-360 虚拟雷达的扫描模型参数（用于 mock 仿真）。
>
> 仓库（只读，未修改）：
> - `refs/lidar/Livox-SDK2`（516 stars，HEAD c0796f0，2026-09-21，SDK v1.5.2）
> - `refs/lidar/livox_ros_driver2`（853 stars，HEAD 2144554，2026-09-21，driver v1.2.8）
> - `refs/lidar/LiDAR_IMU_Init`（1515 stars，HEAD 66b157a，2026-04-30）
>
> 交叉参考（同样只读）：`refs/sim/XTDrone/sitl_config/models/livox_mid40/scan_mode/mid360.csv`（MID-360 扫描花样表）、`refs/sim/Prometheus/Simulator/livox_laser_gazebo_plugins`（Livox Gazebo 插件）、`refs/sim/Prometheus/.../p600_mid360.sdf.jinja`（P600 + MID-360 安装位姿）、`refs/lidar/FAST_LIO/config/mid360.yaml`（MID-360 内置 IMU 外参）。
>
> 本笔记中的实验脚本位于 `/data/projs/anet-drone/.cache/research/r04_*.py`（扫描花样拟合、覆盖率验证、虚拟雷达 z-buffer 基准、Livox 包编解码），可直接复跑。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| livox_ros_driver2 | MID-360/360S/360L 官方 ROS1/ROS2 驱动，发布 `/livox/lidar`（CustomMsg 或 PointCloud2 XYZRTLT）与 `/livox/imu` | **adopt**（真机，P600 机载，锁版本 1.2.8 + 本地补丁）；**port**（CustomMsg/XYZRTLT 数据契约、分帧与逐点时间戳逻辑进入我们的 Sensor Gateway 与 mock） | V0.5（真机）；数据契约 V0.2 起 | 5/5 |
| Livox-SDK2 | UDP 协议栈（设备发现、KV 参数、点云/IMU 包），C 风格 API | **adopt**（作为 driver2 的依赖，不直接编程）；**port**（点云包二进制布局 → Python/TS codec，用于回放/虚拟设备） | V0.5；codec V0.5，虚拟设备 V0.6 | 4/5 |
| LiDAR_IMU_Init (LI-Init) | 无靶标 LiDAR-IMU 外参 + 时延 + 重力 + bias 初始化（FAST-LO → 批优化 → FAST-LIO 在线精修） | **adopt**（离线工具，ROS1 Docker，真机标定）；**port**（标定数学 → Python/scipy，用于标定服务与 mock 自检；激励度评估 → UI 标定向导） | V0.5 | 4/5（真机必做，MVP 不需要） |

核心结论：

1. **MVP（V0.1–V0.3）运行时不依赖这三个仓库**，但它们定义了 MID-360 的“数据语义”——我们的 mock 必须按这个语义产出数据：`timebase(u64 ns) + offset_time(u32 ns)`、`x/y/z(m, float32)`、`reflectivity(u8, 0–150 漫反射/151–255 高反)`、`tag(u8)`、`line = i % 4`、点频 200 kHz、帧 10 Hz、IMU 200 Hz（加速度单位 **g**）。这样 V0.5 接真机时前端/后端零改动。
2. **MID-360 扫描花样已被量化**：从 XTDrone/Prometheus 使用的 `mid360.csv`（80 万行 = 4 s @200 kHz）拟合出一个 7 系数傅里叶载波 + 4 光束旋转扇的**参数化模型**（§3.1），覆盖率曲线与真实花样误差 ≤ 5 个百分点、俯仰直方图误差 ≤ 1 个百分点，可直接写成 TS/WGSL。
3. **虚拟雷达在服务器侧用“球面 z-buffer（range image）”实现**（§3.2），在 UrbanScene3D Shenzhen 5M 点云上单帧（2 万射线）≈ 30–80 ms（纯 numpy，含裁剪），足够 mock；浏览器只画返回点（环形 GPU 缓冲 + 余晖衰减），不做射线求交。
4. **安装朝向决定可用性**：MID-360 竖直视场仅 −7°~+52°，正装时只有 15.1% 的光束低于水平面；我们的基准显示 30 m AGL 正装命中率 34%、倒装 75%。数字孪生里必须建模“安装位姿预设”（Prometheus P600 仿真为前倾 20° 正装，位于 base_link (0.13, 0, 0.23) m）。
5. **发现 3 个实坑**：driver2 默认外参矩阵笔误 `{0,1,1}`（未在 `lidar_configs` 登记的雷达会被施加 y'=y+z）；未对时（time_type=0）时 driver2 用主机收包时刻打时间戳；IMU 加速度单位是 g 而非 m/s²。LI-Init 为 ROS1 + Ceres 2.0 + livox_ros_driver(v1) 消息，接 MID-360 需要改消息命名空间。

---

## 1. 仓库概览

| 项 | Livox-SDK2 | livox_ros_driver2 | LiDAR_IMU_Init |
|---|---|---|---|
| 版本 | 1.5.2（`include/livox_lidar_def.h` 宏） | 1.2.8（`CHANGELOG.md`） | 无版本号，最后提交 “Fix Robosense support (#139)” |
| 最后提交 | 2026-09-21 | 2026-09-21 | 2026-04-30 |
| 语言/构建 | C++11，CMake，产出 `liblivox_lidar_sdk_static.a` + `liblivox_lidar_sdk_shared.so`，默认 `make install` 到 `/usr/local` | C++14，ROS1 catkin / ROS2 colcon（`build.sh ROS1|ROS2|humble|jazzy`），`find_library(... /usr/local/lib)` 硬编码 | C++14，ROS1 catkin（melodic/noetic），依赖 PCL≥1.8、Eigen、**Ceres 2.0**、OpenMP、PythonLibs（matplotlibcpp） |
| 设备支持 | HAP、Mid-360（dev_type 9）、**Mid-360S（35）**、Mid-360L（41）、Avia2（40）、PA | 同 SDK；Mid-360/360S `line_num=4`，HAP=6，其余=1 | 任意 LiDAR（Livox 走 `livox_ros_driver::CustomMsg`，其余 PointCloud2） |
| 平台 | Linux x86/ARM、Windows、macOS（kqueue） | Ubuntu 20.04/22.04/24.04，ROS2 Humble/Jazzy | Ubuntu 18.04/20.04 + ROS Melodic/Noetic |
| 许可 | MIT | MIT | GPLv2（本项目科研用途，忽略） |
| 对本项目价值 | 协议真相源（包格式、端口、KV 键） | 数据契约真相源（话题、字段、时间戳语义） | 真机标定工具 + 可移植的标定数学 |

P600 相关：P600 配 **MID-360S**。源码对比 `sdk_core/command_handler/mid360_command_handler.cpp` 与 `mid360s_command_handler.cpp`，差异仅为类名与 `kLivoxLidarTypeMid360s`，端口常量（`sdk_core/comm/define.h`）完全一致；driver2 的 `pub_handler.cpp` 对 360 与 360S 同样使用 `kLineNumberMid360=4`。因此 **MID-360S 与 MID-360 协议兼容**，增量能力是 `SetLivoxLidarEscMode`（电机转速 Normal/Slow）、PPS 同步模式、`SetLivoxLidarImuRange`（IMU 量程/输出率）。本笔记的扫描模型默认按 MID-360 规格，360S 的 FOV/点频需以数据手册核对（见 §6）。

---

## 2. 源码结构与关键模块

### 2.1 Livox-SDK2

```text
include/
  livox_lidar_api.h     C 风格 API（~60 个函数）
  livox_lidar_def.h     全部数据结构/枚举（#pragma pack(1)）
sdk_core/
  livox_lidar_sdk.cpp   API 实现入口
  device_manager.cpp    设备发现（1 Hz 广播）、socket 建立、按源端口路由（OnData）
  data_handler/         点云/IMU 包分发给回调与 observers
  command_handler/      各型号命令处理器（mid360/mid360s/mid360l/hap/avia2）+ build_request + 状态解析
  comm/                 SdkProtocol 帧封装（CRC16/CRC32）、端口与命令常量（define.h）
  logger_handler/ debug_point_cloud_handler/ upgrade/   日志、调试点云、固件升级
samples/                quick_start、logger、rmc_time_sync、ip_set、info_get、multi_lidars_upgrade
```

**生命周期**（`include/livox_lidar_api.h`）：`LivoxLidarSdkInit(config.json, host_ip)` → `SetLivoxLidarPointCloudCallBack` / `SetLivoxLidarImuDataCallback` / `SetLivoxLidarInfoChangeCallback` → `LivoxLidarSdkStart()` → 在 InfoChange 回调里下发 `SetLivoxLidarWorkMode(kLivoxLidarNormal)`、`SetLivoxLidarPclDataType`、`SetLivoxLidarScanPattern`、`EnableLivoxLidarImuData` 等 → `LivoxLidarSdkUninit()`。所有设置都是异步 + `LivoxLidarAsyncControlCallback(ret_code, error_key)`。

**句柄**：`handle` 就是雷达 IPv4 地址的 `uint32`（网络字节序），见 `DeviceManager::OnData`（`sdk_core/device_manager.cpp:451`）。

**端口表**（`sdk_core/comm/define.h`，MID-360/360S/360L 相同）：

| 通道 | 雷达端口 | 主机端口 | 说明 |
|---|---|---|---|
| 设备发现 | 56000（广播 255.255.255.255） | 56001 | 主机每 1 s 发 `kCommandIDLidarSearch(0x0000)`（`DeviceManager::DetectionLidars`） |
| 控制命令 | 56100 | 56101 | KV 参数读写、重启、升级 |
| 状态推送 | 56200 | 56201 | `kCommandIDLidarPushMsg(0x0102)`，解析见 `parse_lidar_state_info.cpp` |
| 点云数据 | 56300 | 56301 | UDP，`LivoxLidarEthernetPacket` |
| IMU 数据 | 56400 | 56401 | UDP，同一包头，`data_type=0` |
| 日志 | 56500 | 56501 | 固件日志 |

`OnData` 按 **源 IP + 源端口** 路由：来自雷达 56300/56400 的包进 `DataHandler::Handle`，其余进 `GeneralCommandHandler`。这对“虚拟 Livox 设备”很关键（§3.4）：模拟器必须从 56300 源端口、以与主机不同的 IP 发包。

**控制帧 `SdkPacket`**（`sdk_core/comm/sdk_protocol.h`，24 字节头，已编译验证 `offsetof(LivoxLidarCmdPacket,data)=24`）：

| 偏移 | 字段 | 说明 |
|---|---|---|
| 0 | sof u8 | 固定 0xAA |
| 1 | version u8 | 0 |
| 2 | length u16 | 整帧长度 |
| 4 | seq_num u32 | 序号 |
| 8 | cmd_id u16 | 0x0000 搜索、0x0100 WorkModeControl（KV 写）、0x0101 GetInternalInfo（KV 读）、0x0102 PushMsg、0x0200 重启、0x0202 PPS 同步、0x0400–0x0403 升级 |
| 10 | cmd_type u8 | 0=CMD，1=ACK |
| 11 | sender_type u8 | 0=主机，1=雷达 |
| 12 | rsvd[6] | |
| 18 | crc16_h u16 | 对前 18 字节做 CRC-16/CCITT-FALSE（poly 0x1021，init 0xFFFF，`FastCRC16::ccitt`） |
| 20 | crc32_d u32 | 对 data 做标准 CRC32（data 为空时为 0） |
| 24 | data[] | KV 列表：`u16 key_num, u16 rsvd, {u16 key, u16 len, value[len]}...` |

**关键 KV 键**（`ParamKeyName`）：`0x0000 pcl_data_type`、`0x0001 pattern_mode`（0 非重复/1 重复/2 重复低帧率）、`0x0004 lidar_ip`、`0x0006 点云目的地址`、`0x0007 IMU 目的地址`、`0x0012 install_attitude`（roll/pitch/yaw 度 + x/y/z mm）、`0x001A work_mode`、`0x001C imu_data_en`、`0x0021 esc_mode`、`0x0022 fov_mode`、`0x0024 echo_mode`、`0x0026 time_filter_mode`、`0x0028 fog_noise_filter`（0 关/1 雨/2 雾）、`0x0029 pcl_freq_mode`（80k/50k/100k，360L）、`0x002B imu_range`；只读：`0x8000 SN`、`0x8006 cur_work_state`、`0x8007 core_temp`、`0x800A last_sync_time`、`0x800B time_offset`、`0x800C time_sync_type`、`0x800D status_code`、`0x8011 hms_code`。

**点云/IMU 数据包 `LivoxLidarEthernetPacket`**（`include/livox_lidar_def.h`，已用 g++ 编译验证 `offsetof(data)=36`）：

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | version | u8 | |
| 1 | length | u16 | 包总长 |
| 3 | time_interval | u16 | **整包**时间跨度，单位 0.1 µs |
| 5 | dot_num | u16 | 本包点数（MID-360 典型 96，以实测为准） |
| 7 | udp_cnt | u16 | 包计数（丢包检测） |
| 9 | frame_cnt | u8 | |
| 10 | data_type | u8 | 0 IMU / 0x01 笛卡尔 32bit / 0x02 笛卡尔 16bit / 0x03 球坐标 / 0x11 双回波 |
| 11 | time_type | u8 | 0 未同步（上电计时）/ 1 PTP(gPTP) / 2 GPS |
| 12 | rsvd[12] | | |
| 24 | crc32 | u32 | SDK 收包侧不校验点云包 CRC |
| 28 | timestamp | u8[8] | 首点时间，u64 ns（小端） |
| 36 | data[] | | 点数组 |

点结构：

| data_type | 结构 | 字节/点 | 字段 |
|---|---|---|---|
| 0x01 | `LivoxLidarCartesianHighRawPoint` | 14 | x/y/z int32 **mm**，reflectivity u8，tag u8 |
| 0x02 | `LivoxLidarCartesianLowRawPoint` | 8 | x/y/z int16 **cm**，reflectivity，tag |
| 0x03 | `LivoxLidarSpherPoint` | 10 | depth u32 mm，theta u16（天顶角 0.01°），phi u16（方位角 0.01°），refl，tag；x=r·sinθcosφ，y=r·sinθsinφ，z=r·cosθ |
| 0x11 | `LivoxLidarDoubleEchoRawPoint` | 28 | 两组 (x,y,z,refl,tag) |
| 0x00 | `LivoxLidarImuRawPoint` | 24 | gyro_x/y/z float **rad/s**，acc_x/y/z float **g** |

带宽估算（MID-360，0x01，96 点/包）：36 + 96×14 = 1380 B/包；200 kHz → 2083 包/s → 2.88 MB/s ≈ 23 Mbit/s（100BASE-T1 足够）。IMU：60 B × 200 Hz。

**时间同步**：`time_type` 由雷达在包内声明；`SetLivoxLidarRmcSyncTime(handle, "$GPRMC...", len)` 注入 GPRMC（`samples/livox_lidar_rmc_time_sync/` 演示串口读 GPS 并配合 PPS）；PTP 由雷达固件支持（IEEE 1588-2008）。状态键 `0x800C time_sync_type`、`0x800B time_offset` 可用于健康监测。

### 2.2 livox_ros_driver2

```text
src/
  livox_ros_driver2.cpp   DriverNode（ROS2 参数、两个轮询线程）
  lds_lidar.cpp           读 JSON、LivoxLidarSdkInit/Start、登记外参 AddLidarsExtParam
  call_back/livox_lidar_callback.cpp   InfoChange → 下发 pcl_data_type/pattern_mode/blind_spot/dual_emit/install_attitude/WorkMode Normal/Enable IMU
  comm/pub_handler.cpp    【核心】原始包 → 标准点（单位、外参、逐点时间）→ 分帧
  comm/ldq.cpp            无锁环形队列（2^n）
  lds.cpp                 StoragePointData/StorageImuData + 信号量
  lddc.cpp                发布：CustomMsg / PointCloud2(XYZRTLT) / PCL(仅ROS1) / Imu
msg/CustomMsg.msg, CustomPoint.msg
config/MID360_config.json, MID360s_config.json ...
launch_ROS2/msg_MID360_launch.py, msg_MID360s_launch.py ...
```

**数据流**（线程模型）：

```text
SDK IO 线程 ──PubHandler::OnLivoxLidarPointCloudCallback──> raw_packet_queue_ (deque+condvar)
                 （IMU 包直接走 imu_callback_ → Lds::StorageImuData）
RawDataProcess 线程 ──LidarPubHandler::PointCloudProcess（mm→m、外参、逐点时间）──> points_clouds_
                    ──CheckTimer(id)（分帧）──> PointFrame 回调 → Lds::StoragePointData → LidarDataQueue(ring)
DriverNode::PointCloudDataPollThread ──Lddc::DistributePointCloudData──> publish CustomMsg / PointCloud2
DriverNode::ImuDataPollThread ──Lddc::DistributeImuData──> publish sensor_msgs/Imu
```

**逐点时间**（`pub_handler.cpp`）：

```text
point_interval_ns = time_interval * 100 / dot_num          // time_interval 单位 0.1 µs
t_i = packet_timestamp_ns + i * point_interval_ns          // u64 ns 绝对时间
CustomMsg.timebase = 帧内首点 t；CustomPoint.offset_time = (u32)(t_i - timebase)
PointCloud2.timestamp(float64) = t_i                       // 注意 float64 对 1.7e18 ns 只有 256 ns 分辨率
```

`GetEthPacketTimestamp`：`time_type ∈ {1,2}` 时用包内时间；**`time_type=0`（未同步）时直接用主机 `high_resolution_clock::now()`**，即“主机处理时刻”而不是雷达时刻——含网络与队列抖动。这是 LiDAR-IMU 时延标定存在的根本原因之一。

**分帧**（`PubHandler::CheckTimer`）：对时模式下，当最近点时间（ms）是 `publish_interval_ms` 的整数倍且帧跨度 ≥ `interval − 1 ms` 时出帧——帧边界对齐绝对时间网格（10 Hz → 每个 100 ms 整点），多雷达天然对齐；未对时模式按主机定时器出帧。

**`line` 字段**：`point.line = i % line_num`（MID-360 为 4）。这不是物理测量值而是包内序号取模；我们用 CSV 验证了 MID-360 的 4 束激光确实严格交织（第 i 个点属于光束 i%4），所以该字段可信。

**消息**：

```text
CustomMsg:  header, uint64 timebase, uint32 point_num, uint8 lidar_id, uint8[3] rsvd, CustomPoint[] points
CustomPoint: uint32 offset_time(ns), float32 x,y,z(m), uint8 reflectivity, uint8 tag, uint8 line   // 19 B/点
PointCloud2 (xfer_format=0, PointXYZRTLT, point_step=26):
  x f32@0, y f32@4, z f32@8, intensity f32@12, tag u8@16, line u8@17, timestamp f64@18
```

**话题与参数**（`lddc.cpp`、`livox_ros_driver2.cpp`）：`multi_topic=0` → `/livox/lidar`、`/livox/imu`；`=1` → `/livox/lidar_192_168_1_12`（IP 点换下划线）。参数：`xfer_format`（0 PointCloud2 / 1 CustomMsg / 2 PCL 仅 ROS1）、`publish_freq`（ROS2 夹在 [0.5, 100] Hz）、`frame_id`（点云帧；**IMU frame_id 硬编码 "livox_frame"**）、`user_config_path`、`data_src`（ROS2 仅支持 0 实时雷达，lvx 回放不可用）。FAST-LIO/LI-Init 需要 CustomMsg（`xfer_format=1`）以拿到逐点 offset_time。

**配置 JSON**：`lidar_summary_info.lidar_type=8`；`MID360.host_net_info` 指定主机 IP 与端口；`lidar_configs[]` 按雷达 IP 设 `pcl_data_type`、`pattern_mode`、`extrinsic_parameter{roll,pitch,yaw(度), x,y,z(mm)}`。外参旋转 `R = Rz(yaw)·Ry(pitch)·Rx(roll)`（`LidarPubHandler::SetLidarsExtParam`），与 LI-Init 的 `RotMtoEuler`（ZYX）一致。

**源码中发现的问题**（写入 §6 风险）：

1. `src/comm/pub_handler.h` 默认 `extrinsic_` 的旋转第二行为 `{0, 1, 1}`（应为 `{0,1,0}`）。`lds_lidar.cpp:179` 会为 `lidar_configs` 中登记的雷达调用 `AddLidarsExtParam` 覆盖它；但 `LidarInfoChangeCallback` 对“未在配置中登记的雷达”只分配槽位不登记外参 → 所有点被施加 `y' = y + z`。swarm 目录中 vendored 的 1.2.4 版同样存在，是长期潜伏笔误。
2. 外参既通过 `SetLivoxLidarInstallAttitude` 写入雷达，又在主机 `pub_handler` 中应用；需在真机上确认固件是否也变换点云，避免双重施加。建议：驱动外参全置 0，所有外参统一由我们的 TF/标定文件管理。
3. `parse_livox_lidar_cfg.cpp` 把 `blind_spot_set` 转 `int8_t`（>127 溢出，仅影响 HAP）。
4. IMU 加速度单位 g 原样写入 `sensor_msgs/Imu.linear_acceleration`，违反 REP-145（m/s²）。

### 2.3 LiDAR_IMU_Init

```text
src/laserMapping.cpp        主节点：订阅 → FAST-LO（纯 LiDAR 里程计）→ 激励评估 → LI_Initialization → 切 FAST-LIO 在线精修 → 写 result/
src/preprocess.cpp          Livox 分帧 process_cut_frame_livox（一帧切 cut_frame_num 子帧提高里程计频率）
src/IMU_Processing.hpp      Forward_propagation_without_imu（常速模型）/ IMU 前向传播 + 去畸变
include/LI_init/LI_init.{h,cpp}   标定核心：插值、零相位滤波、互相关、Ceres 三个代价函数
include/ikd-Tree/           增量 KD 树
config/mid360.yaml, launch/livox_mid360.launch
```

**状态向量**（`include/common_lib.h::StatesGroup`，24 维）：`rot_end, pos_end, offset_R_L_I, offset_T_L_I, vel_end, bias_g, bias_a, gravity`。纯 LiDAR 阶段 `bias_g` 被**复用为 LiDAR 角速度**（`Forward_propagation_without_imu` 用 `Exp(state.bias_g, dt)` 做常角速度传播），`push_Lidar_CalibState(state.rot_end, state.bias_g, state.vel_end, t)` 取的就是它。

**流程**（`laserMapping.cpp` 主循环 + `LI_Init::LI_Initialization`）：

1. 静止 ≥ 5 s 积累初始地图；位移 > 0.05 m 判定开始运动（`move_start_time`）。
2. 每帧记录 LiDAR 姿态/角速度/线速度；`data_sufficiency_assess` 每秒评估激励度（旋转雅可比特征值），三轴进度条都 > 99% 才开始批优化。
3. 批优化：
   - `downsample_interpolate_IMU`：IMU 线性插值到 LiDAR 时刻；加速度 5 点均值滤波。
   - `zero_phase_filt`：6 阶 Butterworth（ωc=0.15，系数写死在 `LI_init.h::Butterworth`）前向 + 反向（filtfilt），IMU 与 LiDAR 同样处理；`normalize_acc` 用前 10 个样本均值归一到 9.81。
   - `xcorr_temporal_init`：|ω_I| 与 |ω_L| 去均值互相关，取峰值 lag → `time_lag_1 = lag / (orig_odom_freq·cut_frame_num)`（Livox 默认 10×5 = 50 Hz → 20 ms 分辨率）。
   - `central_diff`：中心差分得 α_I、α_L、a_L；二次零相位滤波。
   - `solve_Rotation_only`：`min Σ‖R_LI ω_L − ω_I‖²`（四元数参数化）。
   - `solve_Rot_bias_gyro`：`min Σ‖R_LI ω_L − ω_I − (Δt_i + t_d) α_I + b_g‖²`，同时解 R_LI、b_g、连续时延 t_d（`time_lag_2`）。
   - `acc_interpolate` 后 `solve_trans_biasacc_grav`：`min Σ‖R_Lk R_LIᵀ a_I − R_Lk b_a + R_GL0 g − a_L − R_Lk([ω_L]ײ + [α_L]×) p_IL‖²`，b_a 限幅 ±0.01；输出 `p_LI = −R_LI p_IL`、`b_a^I = R_LI b_a`、`g_L0 = R_GL0 g`。
4. 切换 FAST-LIO：`state.offset_R_L_I/offset_T_L_I` 进入 IEKF 状态，协方差 `Rot_LI_cov/Trans_LI_cov` 控制在线精修（`online_refine_time`，默认 20 s），再写一次结果。

**时延合成**：`total = timediff_imu_wrt_lidar（首条消息时间差 > 1 s 时的“硬”偏移）+ time_lag_1 + time_lag_2`；语义为“从 IMU 时间戳**减去**该值，或给 LiDAR 时间戳**加上**该值”。`result/Initialization_result.txt` 中出现 1716257158.9 s 的时延，正是雷达上电计时 vs 系统纪元时间的硬偏移例子。

**MID-360 配置**（`config/mid360.yaml`）：`imu_topic: /mavros/imu/data_raw`、`mean_acc_norm: 9.805`（用飞控 IMU；若用雷达内置 `/livox/imu` 必须改为 **1**）、`cut_frame_num: 5`、`orig_odom_freq: 10`、`data_accum_length: 500`、`online_refine_time: 20`、`scan_line: 6`（建议改 4）、`blind: 1`。

**源码问题**：ROS1-only；`#include <livox_ros_driver/CustomMsg.h>`（v1 驱动命名空间，driver2 发布的是 `livox_ros_driver2/CustomMsg`，需改包名或加 relay）；使用 `ceres::LocalParameterization`（Ceres 2.2 已移除，需锁 2.0/2.1 或改 `Manifold`）；`frame_num % orig_odom_freq * cut_frame_num == 0` 运算符优先级错误（实际每 `orig_odom_freq` 帧评估一次，无害）；`G_m_s2=9.81` 写死；结果写进 package 源码目录。

---

## 3. 可复用算法与实现（含伪代码/参数）

### 3.1 MID-360 虚拟雷达扫描模型（Sensor Simulation 核心交付）

#### 3.1.1 规格参数（Livox 公开规格，mock 默认值）

| 参数 | 取值 | mock 用法 |
|---|---|---|
| 水平 FOV | 360° | 方位角全周 |
| 垂直 FOV | −7° ~ +52°（CSV 实测 −7.21° ~ +52.16°） | 俯仰裁剪 |
| 点频 | 200,000 点/s（首回波） | 每点 5 µs |
| 帧率 | 10 Hz（20,000 点/帧） | driver `publish_freq` |
| 激光束 | 4 束交织（点序 i → 光束 i%4） | `line` 字段 |
| 探测距离 | 40 m @10% 反射率；70 m @80%（100 klx） | 反射率相关量程（下） |
| 近距盲区 | 0.1 m | r < 0.1 丢弃 |
| 测距精度 1σ | ≤ 2 cm @10 m；≤ 3 cm @0.2 m | σ_r(r) |
| 角精度 1σ | < 0.15° | 默认 0.1° |
| 虚警率 | < 0.01%（100 klx） | 1e-4 随机噪点 |
| 内置 IMU | ICM40609，200 Hz，gyro rad/s，acc **g** | IMU mock |
| 同步 | IEEE 1588-2008 PTPv2、GPS（PPS+GPRMC） | `time_type` |
| 功耗/重量 | 6.5 W；265 g；65×65×60 mm | 数字孪生质量/功耗 |
| 包 | 96 点/包，36 B 头 + 14 B/点 | codec/带宽 |
| IMU→LiDAR 出厂外参 | LiDAR 原点在 IMU 系 `T = [-0.011, -0.02329, 0.04412] m`，`R = I`（`refs/lidar/FAST_LIO/config/mid360.yaml`） | IMU mock 位置 |

#### 3.1.2 真实扫描花样的量化分析

数据源：`refs/sim/XTDrone/sitl_config/models/livox_mid40/scan_mode/mid360.csv`（800,000 行，列 `Time/s, Azimuth/deg, Zenith/deg`，第一列实为序号）。Prometheus 的 `livox_points_plugin.cpp` 以同一文件驱动 Gazebo：`zenith_rad = zenith_deg·π/180 − π/2` 作为 pitch，`line = i % 4`，`offset_time = 1e9/200000·i`、`tag = 0x10` → 证实 CSV 按 200 kHz 时序排列，800k 行 = 4 s。坐标约定：方位角从 +X 逆时针到 +Y，俯仰 `el = 90° − zenith`，方向向量 `(cos el cos az, cos el sin az, sin el)`，即 Livox 雷达系 FLU（x 前、y 左、z 上）。

分析结果（脚本 `r04_mid360_pattern*.py`、`r04_mid360_model*.py`）：

| 特征 | 数值 | 含义 |
|---|---|---|
| 光束交织 | 严格 i%4；4 束在同一“样本”上组成一把扇 | `line` 字段可信 |
| 方位步进 | 每束每样本 −1.3134°（σ 0.06°） | 扫描头以 ≈182.4 rev/s 顺时针（俯视）旋转；每圈 274.1 个束样本 |
| 光束扇 | 俯仰幅度 ±2.765°（外侧两束）/ ±0.921°（内侧两束），扇随扫描头每圈旋转一周 | 相邻束间隔 ≈1.84° |
| 俯仰载波 | 4 束均值俯仰按**严格 0.1 s 周期**在 −3.97° ~ +47.56° 间往复；6 阶余弦级数拟合残差 0.02° | 每帧恰好一次上下扫 |
| 非重复性 | 每帧 18.24 圈（非整数），相邻帧 0.5° 网格 Jaccard 仅 0.10–0.14 | 花样逐帧错位，积分越久越密 |
| 俯仰分布 | −5°~0°：13.2%；0°~50° 每 5° 约 7–9%；el<0：15.1%；el<−5°：1.9%；el>45°：9.3% | 正装无人机“看地”很少 |

**覆盖率 vs 积分时间**（FOV 网格被命中比例，分母为 4 s 并集）：

| 积分时间 | 1° 网格 实测 | 1° 网格 模型 | 0.5° 网格 实测 | 0.5° 网格 模型 |
|---|---|---|---|---|
| 0.05 s | 41.9% | 43.1% | 11.8% | 12.1% |
| 0.1 s（1 帧） | 64.6% | 66.6% | 21.9% | 22.9% |
| 0.2 s | 84.4% | 82.7% | 38.5% | 38.8% |
| 0.5 s | 99.1% | 97.9% | 68.5% | 67.3% |
| 1.0 s | 99.9% | 99.7% | 86.7% | 81.9% |
| 2.0 s | 100% | 99.9% | 98.3% | 95.8% |

这条曲线就是 UI 里“积分时间”滑杆的物理依据（§7）。

#### 3.1.3 参数化模型（可直接移植 TS / WGSL / numpy）

```ts
// MID360Pattern v1 —— 拟合自 mid360.csv（200 kHz），误差见 §3.1.2 表
const PT_RATE = 200_000;          // 点/s
const ROT_PERIOD = 274.1;         // 束样本/圈（≈182.4 Hz）
const AZ_STEP = -1.31341;         // 度/束样本
const CARRIER_PERIOD = 5000;      // 束样本 = 0.1 s
const C = [20.4535, 23.5232, 1.1247, 2.3002, 0.2232, 0.1565, 0.0330]; // 载波俯仰余弦级数（度）
const S_EL = [2.765, 0.921, -0.921, -2.765];  // 光束扇俯仰幅度（度）
const S_AZ = [1.248, 0.617, -0.617, -1.248];  // 光束扇方位幅度（度，未除 cos）
const D2R = Math.PI / 180;

function mid360Ray(i: number): { az: number; el: number; line: number; tOffsetNs: number } {
  const k = i & 3, n = i >> 2;                        // 光束号、束样本号
  const ph = 2 * Math.PI * n / CARRIER_PERIOD;
  let ec = C[0];
  for (let h = 1; h <= 6; h++) ec += C[h] * Math.cos(h * ph);   // 载波俯仰
  const th = 2 * Math.PI * n / ROT_PERIOD;                        // 扇旋转相位
  const el = ec + S_EL[k] * Math.cos(th + 1.7 * D2R);
  let az = 270.08 + AZ_STEP * n + S_AZ[k] * Math.sin(th) / Math.cos(ec * D2R);
  az = ((az % 360) + 360) % 360;
  return { az, el, line: k, tOffsetNs: i * 5000 };
}
// 方向（雷达系 FLU）：d = [cos(el)cos(az), cos(el)sin(az), sin(el)]
```

实现要点：

- **长时间运行的精度**：`n` 会无界增长。按帧（20,000 点）在 CPU 侧用 float64 维护三个相位累加器（载波相位 mod 2π、扇相位 mod 2π、方位 mod 360°），shader/numpy 内只处理帧内偏移，避免 f32 精度崩坏。
- **两种模式**：A）参数化（上面公式，无需资产，Web 与后端通用，推荐 MVP 默认）；B）回放表（把 CSV 前 1 s 压成 200k×2×uint16 ≈ 800 KB，按 `i mod 200000` 取，每循环一次（1 s = 50,000 束样本 = 182.415 圈）整表方位旋转 50000 × −1.31341° mod 360 ≈ −150.5°，与真实连续扫描衔接，避免逐秒完全重复；帧内相邻帧的错位则来自每帧 18.24 圈的小数部分），用于需要逐点保真的后端评测。
- **重复扫描模式**（`pattern_mode=1`）mock：固定一个 0.1 s 帧的射线表循环使用即可。

#### 3.1.4 回波物理模型（量程、反射率、噪声、环境）

```text
输入：射线方向 d（世界系）、命中距离 r、表面法向 n、材质漫反射率 ρ∈(0,1]、环境能见度 V(m)、雨强 R(mm/h)
1) 入射衰减：        ρ_eff = ρ · max(|n·d|, 0.05)
2) 大气两程衰减：     σ = 3.912 / V  （Koschmieder，V 取自 Environment E(x,y,z,t).visibility）
                     ρ_eff ← ρ_eff · exp(-2 σ r)
3) 反射率相关量程：   r_max(ρ) = 70 · (ρ/0.8)^0.269      // 过 (0.1,40 m) 与 (0.8,70 m) 两个规格点
4) 检测概率：         P_det = 1 / (1 + exp((r - r_max(ρ_eff)) / (0.05 · r_max)))；r < 0.1 m 丢弃
5) 测距噪声：         r' = r + N(0, σ_r(r))，σ_r(r) = 0.02 + 0.01·exp(-r/2)  (m)
6) 角度噪声：         az' = az + N(0, 0.1°)，el' = el + N(0, 0.1°)
7) 反射率输出：       漫反射 refl_u8 = clamp(round(150·ρ_eff), 0, 150)；回反射材质映射到 151–255
8) 标签 tag：         正常点 0x00；雾/尘/雨杂波点置 bit[1:0]=01（空间位置判定噪声），
                     近窗口附着噪声置 bit[3:2]=01（强度判定噪声）；bit[5:4] 保持 00
                     （FAST-LIO/LI-Init 过滤条件为 (tag & 0x30) ∈ {0x00, 0x10}，见 preprocess.cpp）
9) 杂波注入：         每条射线以 p_clutter = min(0.3, σ·5) 概率产生一个 r ~ Exp(1/σ) 截断到 [0.1, 10] m 的假回波；
                     若 fog_noise_filter≠0（设备键 0x0028），以 90% 概率剔除带 tag 噪声位的点
10) 虚警：            以 1e-4 概率在 [0.1, r_max] 均匀生成随机点
```

第 3 步是按两个规格点拟合的经验幂律，不是激光雷达方程推导（后者 r ∝ √ρ 会把 10% 反射率量程低估到 25 m）。

#### 3.1.5 IMU mock（与雷达同源时钟）

200 Hz；gyro(rad/s) = R_IB·ω_body + b_g + N(0, 7e-4)；acc(**g**) = (R_IB·(a_body − g_world))/9.80665 + b_a + N(0, 1e-3)；b_g、b_a 随机游走 1e-5/√s。以上噪声值是 MEMS 量级的工程默认，不是 ICM40609 数据手册值，放在设置里可调。IMU 与雷达共用仿真时钟，`time_type=1`（模拟 PTP 已同步），可选注入固定时延 + 抖动来测试标定链路。

#### 3.1.6 安装位姿预设（P600 数字孪生）

| 预设 | base_link→livox 平移 (m) | 旋转 | 来源/用途 |
|---|---|---|---|
| `p600_prometheus_sim` | (0.13, 0, 0.23)，传感器原点再 +0.05 z | pitch = 0.35 rad（≈20° 前倾），正装 | `refs/sim/Prometheus/.../p600_mid360.sdf.jinja`、`Modules/FAST_LIO/config/mid360_gazebo.yaml`（extrinsic_R 为 20° 俯仰） |
| `inverted_mapping` | (0, 0, −0.08) | roll = 180°（倒装） | 航测建图，FOV 变为 −52°~+7° |
| `custom` | 用户输入 | 用户输入 | 以 LI-Init 真机结果覆盖 |

基准（`r04_raycast_bench3.py`，Shenzhen 城市最密 20 m 格，30 m AGL，一帧 2 万射线）：正装命中率 34%（中位距离 27 m），倒装 75%（中位 33 m）。正装大部分射线打向天空——对“观测地面”的任务，这个预设差异比任何渲染优化都重要。

#### 3.1.7 Mock 参数卡（建议落地为 `vehicles/p600/sensors/mid360.yaml`）

```yaml
sensor: livox_mid360
dev_type: 9                 # MID-360S 用 35，协议相同
frame_id: livox_frame       # FLU
pattern: {mode: parametric, pattern_mode: 0, point_rate_hz: 200000, beams: 4}
frame_rate_hz: 10
fov: {h_deg: 360, v_min_deg: -7.2, v_max_deg: 52.2}
range: {blind_m: 0.1, r_at_rho10_m: 40, r_at_rho80_m: 70, exponent: 0.269, hard_max_m: 70}
noise: {range_sigma_far_m: 0.02, range_sigma_near_m: 0.03, angle_sigma_deg: 0.1, false_alarm: 1.0e-4}
reflectivity: {diffuse_max_u8: 150, retro_min_u8: 151}
packet: {points_per_packet: 96, header_bytes: 36, point_bytes: 14, data_type: 1, time_type: 1}
imu: {rate_hz: 200, acc_unit: g, gyro_unit: rad_s,
      lidar_in_imu: {t: [-0.011, -0.02329, 0.04412], rpy_deg: [0, 0, 0]}}
mount: {preset: p600_prometheus_sim, xyz: [0.13, 0.0, 0.28], rpy_deg: [0, 20, 0]}
env_coupling: {visibility_from: environment.visibility, fog_noise_filter: 0}
output: {ros_like: custom_msg, web_decimation_pts_per_frame: 4000}
```

### 3.2 虚拟雷达射线求交：球面 z-buffer（服务器端）

世界是点云（UrbanScene3D 采样云只有 xyz+法向，平均表面间距 ≈ 0.82 m，1 m 体素内 1.5 点），直接逐射线找最近点不可行。采用“把点投影到传感器球面深度图，再用射线查表”的方案（与 MARSIM 类 UAV 雷达模拟器思路一致），全向量化：

```python
def lidar_frame(world_index, T_world_sensor, rays_az_el, res_deg=0.5, dilate=3, r_max=70.0):
    # 1. 裁剪：用 World 的 xy 瓦片索引（20 m 格）取 r_max 内的点；无人机移动 < 10 m 时复用上帧裁剪结果
    P = world_index.gather_cells(center=T_world_sensor.t, radius=r_max)          # (M,3)
    Q = (P - T_world_sensor.t) @ T_world_sensor.R                                 # 世界→传感器系
    r = norm(Q, axis=1); keep = (r > 0.1) & (r < r_max); Q, r = Q[keep], r[keep]
    # 2. 球面深度图（range image）
    W, H = round(360/res_deg), round(180/res_deg)
    ia = ((atan2(Q.y, Q.x) + pi) / res) % W ;  ie = ((asin(Q.z / r) + pi/2) / res)
    zbuf = full(H*W, inf); minimum.at(zbuf, ie*W + ia, r)                         # 每格最近点
    # 3. 空洞填补：点云稀疏 → 最小值膨胀（等效把每个点 splat 成盘）
    zbuf = min_filter(zbuf.reshape(H, W), size=dilate, wrap_azimuth=True)
    # 4. 查表：MID-360 射线（§3.1.3）→ 距离；inf = 无回波
    hit_r = zbuf[el_to_row(rays.el), az_to_col(rays.az)]
    # 5. 回波物理（§3.1.4）+ 反变换回 FLU xyz，按 96 点打包 / 按帧组 CustomMsg
```

实测（numpy 单线程，本机 CPU，Shenzhen 5M 点）：

| 配置 | 视场内点数 | 裁剪 | z-buffer | 射线查表 | 命中率（正装/倒装） |
|---|---|---|---|---|---|
| 0.5°，膨胀 1 | 62,840 | 24–34 ms | 5 ms | 0.7 ms | 10% / 32% |
| 0.5°，膨胀 3×3 | 62,840 | 36–38 ms | 12–50 ms | ≤1 ms | 34% / 75% |
| 0.25°，膨胀 5×5 | 62,840 | 14–39 ms | 100–130 ms | 1 ms | 29% / 70% |

结论：推荐 **0.5° + 3×3 膨胀**。单机每帧 30–80 ms，可支撑 1–2 架 10 Hz 或 5 架 2 Hz 的 mock；更多无人机时改用 numba/多进程，或共享裁剪瓦片缓存。膨胀核应随“点间距/距离”自适应：`k = clamp(ceil(spacing / (r·res)), 1, 5)`（按距离分 2–3 档分层膨胀）。精确替代方案：1 m 占用体素 + 3D-DDA（Amanatides–Woo）；有网格后（V0.5+）改用 Open3D `RaycastingScene`。

### 3.3 Web 侧：扫描点层（只显示，不求交）

保持原设计“Browser = Visualization Runtime”：

- **下行数据**：后端把每帧抽稀到 2k–5k 点/机（`output.web_decimation_pts_per_frame`），量化为 int16 cm（传感器系，±327 m）×3 + refl u8 + (tag|line) u8 = **8 B/点**，4000 点 ≈ 32 KB/帧，10 Hz ≈ 320 KB/s/机。帧头携带 `timebase` 用 `(sec:u32, nsec:u32)` 而不是 JS Number（ns 纪元 ≈1.7e18 超过 2^53；float64 分辨率只有 256 ns，§6）。
- **GPU 环形缓冲**：预分配容量 K（如 200k 点）的单个 BufferGeometry（位置 + 出生时间 + 反射率），每帧用 `bufferSubData` 写到环形偏移，`drawRange` 不变；顶点着色器 `alpha = 1 − (now − birth)/window`，window = UI 积分时间滑杆（0.1–2 s，对应 §3.1.2 覆盖曲线）。不重建几何、无 GC 抖动。
- **点预算**：扫描层纳入全局 point budget，预留 5–10%（例：总预算 3M 时扫描层 ≤ 200k）；FPS 反馈降档时先缩短余晖窗口再降世界点云 LOD。
- **FOV 可视化**：SensorLayer 的 LiDAR FOV 画成“环形壳”（两个圆锥面 el=−7° 与 +52° 之间的 360° 带），不是锥体；可选“花样动画”：在顶点着色器用 §3.1.3 公式只画射线方向（不求交）。

### 3.4 Livox 包 codec 与虚拟 Livox 设备（SIL）

已验证的 Python codec（`.cache/research/r04_livox_codec.py`，CRC16 校验值 0x29B1 通过，96 点包 = 1380 B，往返误差 < 0.5 mm）：

```python
HDR = struct.Struct('<BHHHHBBB12sI8s')       # 36 B：version,length,time_interval,dot_num,udp_cnt,frame_cnt,data_type,time_type,rsvd,crc32,timestamp
PT_HIGH = np.dtype([('x','<i4'),('y','<i4'),('z','<i4'),('refl','u1'),('tag','u1')])   # 14 B
def decode(buf):
    ver, length, tint, dot, udp, frame, dtype, ttype, _, crc, ts = HDR.unpack_from(buf, 0)
    t0 = struct.unpack('<Q', ts)[0]
    if dtype == 0: return imu(struct.unpack_from('<6f', buf, 36))           # gyro rad/s, acc g
    pts = np.frombuffer(buf, PT_HIGH, count=dot, offset=36)
    t = np.int64(t0) + np.round(np.arange(dot) * tint * 100 / dot).astype(np.int64)   # int64 ns
    return xyz_m = stack(pts.x, pts.y, pts.z)/1000, refl, tag, line = arange(dot) % 4, t
```

用途：（1）V0.5 的“无 ROS 旁路网关”：Jetson 上直接收 56301 UDP → 转发 World Runtime（绕过 ROS，调试用）；（2）真机 pcap 回放；（3）**虚拟 Livox 设备**：仿真服务按真实协议对外发包，让真实的 `livox_ros_driver2 → FAST-LIO → EGO-Planner` 栈在我们的仿真世界里原样运行（软件在环）。虚拟设备需实现的最小握手（依据 `device_manager.cpp`、`general_command_handler.cpp`、`build_request.cpp`）：

1. 监听 56000，收到 `cmd_id=0x0000` 后回 ACK：`DetectionData{ret_code=0, dev_type=9/35, sn[16], lidar_ip[4], cmd_port=56100}`，帧头 CRC16/CRC32 正确。
2. 在 56100 上：对 `0x0101`（查询，含 `kKeyFwType` 以判断 loader 模式）回 KV；对 `0x0100`（写主机 IP/端口 3 个 KV、work_mode、pcl_data_type、pattern_mode、install_attitude、imu_data_en）一律回 `{ret_code=0, error_key=0}`。
3. 从 **源端口 56300** 向 `host_ip:56301` 发点云包，从 56400 向 56401 发 IMU 包；源 IP 必须与主机 IP 不同（`OnData` 会丢弃来自 `detection_host_ip_` 的包），单机上可用 `127.0.0.x` 别名或 netns。
4. 可选：56200 周期推送 `0x0102` 状态（work_state、core_temp、time_sync_type）。

### 3.5 分帧与时间戳规范（Sensor Gateway / mock 共用）

```text
LidarFrame {
  sensor_id, frame_seq,
  timebase: {sec:u32, nsec:u32}            // 帧首点时间（对时后为 PTP/仿真时钟）
  time_sync: 'none'|'ptp'|'gps'|'sim'      // 对应 time_type 0/1/2 + 仿真
  points: SoA { x,y,z: f32 (m, 雷达系 FLU) | int16 cm（Web）, offset_ns: u32, refl: u8, tag: u8, line: u8 }
  extrinsic_ref: "vehicles/p600-01/calib/lidar_imu.yaml#version"
}
分帧规则：对时时帧边界 = floor(t / 100ms) 网格（与 driver2 CheckTimer 一致，多机多雷达帧天然对齐）；
          未对时时按接收时钟，并在 DroneState.sensors.lidar.time_sync 标红。
单位规则：网关边界把 IMU 加速度 ×9.80665 转为 m/s²，在消息里记录 acc_unit。
```

### 3.6 LI-Init 标定数学的 Python 移植（标定服务 + mock 自检）

```python
def li_init(lidar_states, imu_raw, f_odom=10, cut=5, acc_norm=1.0):
    # lidar_states: [(t, R_L0_Lk, ω_L, v_L)] 来自纯 LiDAR 里程计（FAST-LO / FAST-LIO LO 模式 / 仿真真值）
    imu = interp(imu_raw, at=lidar_states.t)                 # 线性插值 ω, a；a *= 9.81/acc_norm
    imu.a = moving_mean(imu.a, 5)
    imu, lid = filtfilt(butter(6, 0.15), imu), filtfilt(butter(6, 0.15), lid)
    imu.a *= 9.81 / norm(mean(imu.a[:10]))
    lag1 = argmax_xcorr(|ω_I| - mean, |ω_L| - mean) / (f_odom * cut)       # 粗时延
    shift(imu, lag1); α_I = cdiff(ω_I); α_L = cdiff(ω_L); a_L = cdiff(v_L); 二次 filtfilt
    R_LI = argmin Σ ||R ω_L - ω_I||²                                         # SO(3)，scipy least_squares + 旋转向量参数化
    R_LI, b_g, td = argmin Σ ||R ω_L - ω_I - (Δt_i + td) α_I + b_g||²       # 细时延
    shift(imu, td); a_I = interp_acc(at=lidar.t)
    R_GL0, b_a, p_IL = argmin Σ ||R_Lk R_LIᵀ a_I - R_Lk b_a + R_GL0 g - a_L - R_Lk([ω_L]×² + [α_L]×) p_IL||²,  |b_a| ≤ 0.01
    return R_LI, p_LI = -R_LI p_IL, dt_total = hard + lag1 + td, b_g, b_a_I = R_LI b_a, g_L0 = R_GL0 g

def excitation(ω_L_history, data_accum_length=500):      # UI 标定向导三根进度条
    J = vstack([skew(ω) for ω in ω_L_history])           # 3N×3
    λ, V = eig(J.T @ J); s = λ / data_accum_length
    pct = [s[1]*s[2], s[0]*s[2], s[0]*s[1]]              # 某轴的激励度 = 另两个特征值之积
    axis_of = argmax(V**2, axis=0)                        # 特征向量对应的雷达轴
    return {axis: min(p, 1.0)}  # 三轴都 > 0.99 → 可开始批优化
```

自检用法（V0.4/V0.5）：mock 生成已知外参（如 R=Rz(90°)，p=(0.05,0.03,0.16)）+ 已知时延（如 +35 ms）的 LiDAR 位姿/IMU 流，跑移植版，要求恢复误差 < 1°/< 2 cm/< 2 ms——这能同时验证我们的 IMU mock、时钟、坐标约定。scipy 需加入 venv（当前 venv 没有 scipy）。

### 3.7 时间同步落地（V0.5 真机）

优先级：PTP（Jetson Orin NX 上 `ptp4l` 作 grandmaster，网口硬件时间戳）> GPS（RTK 接收机 PPS 接雷达 + `SetLivoxLidarRmcSyncTime` 注入 GPRMC）> 无同步 + LI-Init/FAST-LIO `time_offset_lidar_to_imu`。验收：抓包确认 `time_type≠0`，`/livox/lidar.header.stamp` 与 `/mavros/imu` 同一时基。

---

## 4. 在本项目中的落点与复用方式

| # | 可复用项 | 来源（文件/函数） | 目标模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | MID-360 参数化扫描花样 | 拟合自 `XTDrone/.../mid360.csv`；约定来自 `Prometheus/.../livox_points_plugin.cpp` | `sensors/lidar/mid360_pattern.{py,ts}` | V0.2（MVP mock） | port | 无资产、覆盖率可信，Web/后端共用 |
| 2 | 球面 z-buffer 虚拟雷达 | 自研（MARSIM 思路） | `simulation/sensors/lidar_raycast.py` | V0.2–V0.3（MVP） | 自研 | 点云世界下最快的向量化方案 |
| 3 | 回波物理（量程/反射率/噪声/雾雨） | Livox 规格 + tag 语义（`preprocess.cpp` 过滤条件）+ `kKeySetFogNoiseFilter` | `simulation/sensors/lidar_physics.py` <-> `environment/` | V0.3 视觉 / V0.4 物理 | 自研 | 对应原设计 §22–24 “Fog→LiDAR” |
| 4 | CustomMsg / XYZRTLT 数据契约 | `msg/CustomMsg.msg`、`lddc.cpp::InitPointcloud2MsgHeader` | `shared/contracts/sensor_lidar.ts/.py` | V0.2 起 | adopt（语义） | V0.5 接真机零改动 |
| 5 | 逐点时间戳与分帧 | `pub_handler.cpp::CheckTimer`、`GetEthPacketTimestamp` | Sensor Gateway / mock | V0.2 起 | port | 多机帧对齐、时间语义一致 |
| 6 | Web 扫描层（环形缓冲 + 余晖） | 自研 | `apps/web/layers/SensorScanLayer` | V0.2（MVP） | 自研 | 不打断全局 LOD、零 GC |
| 7 | livox_ros_driver2 真机驱动 | 整仓 | P600 机载（ROS2 Humble/Jazzy 或 ROS1） | V0.5 | adopt（锁 1.2.8 + 补丁：默认外参笔误、IMU 单位注释） | 官方、活跃、支持 360S |
| 8 | Livox-SDK2 | 整仓 | driver2 依赖 | V0.5 | adopt（间接） | 不直接调用 C API |
| 9 | Livox 包 codec | `livox_lidar_def.h` 结构、`sdk_protocol.cpp` | `sensors/lidar/livox_codec.py` | V0.5 | port | 旁路网关/回放 |
| 10 | 虚拟 Livox 设备（SIL） | `device_manager.cpp`、`general_command_handler.cpp`、`build_request.cpp` | `simulation/sil/livox_device.py` | V0.6 | port | 真实 ROS 栈跑在仿真世界 |
| 11 | LI-Init 真机标定 | 整仓 + ROS1 Docker（`docker/Dockerfile`） | `tools/calibration/li_init/` | V0.5 | adopt（离线） | 无靶标外参 + 时延 |
| 12 | LI-Init 数学移植 | `LI_init.cpp` 三个代价函数、`xcorr_temporal_init`、`Butter_filt` | `services/calibration/li_init.py` | V0.5 | port | 标定服务、mock 自检 |
| 13 | 激励度评估 → 标定向导 UI | `LI_Init::data_sufficiency_assess` | `apps/web/calibration/ExcitationMeter` | V0.5 | port | 引导飞手三轴激励 |
| 14 | 时间同步（PTP/GPRMC） | `SetLivoxLidarRmcSyncTime`、`samples/livox_lidar_rmc_time_sync` | P600 机载配置 | V0.5 | adopt | 融合质量前提 |
| 15 | 设备状态 KV → 健康面板 | `parse_lidar_state_info.cpp`（0x8006/0x8007/0x800C/0x8011） | `DroneState.sensors.lidar` | V0.5 | reference | 右侧无人机面板“传感器健康” |
| 16 | 固件升级、日志、调试点云、HAP/Avia2 | `upgrade/`、`logger_handler/`、`debug_point_cloud_handler/` | — | — | skip | 与本项目无关 |

---

## 5. 对比与推荐

| 维度 | livox_ros_driver2 | Livox-SDK2 | LiDAR_IMU_Init |
|---|---|---|---|
| Stars | 853 | 516 | **1515** |
| 2026 活跃度 | 高（2026-09 支持 360L） | 高（2026-09，v1.5.2） | 中（2026-04 修 Robosense） |
| 与 P600/MID-360S 契合 | 直接可用（ROS2，360S 已支持） | 底层必需 | 需改消息命名空间，ROS1 |
| 对 MVP（mock）价值 | 高：数据契约与时间语义 | 中：包格式、带宽估算 | 低：MVP 无真机 |
| 对 V0.5 真机价值 | 必需 | 必需（间接） | 必需（一次性标定） |
| 构建难度 | 低–中（`/usr/local/lib` 硬编码） | 低 | 中–高（Ceres 2.0、ROS1、matplotlib） |
| 风险 | 外参笔误、未对时用主机时钟 | 几乎无 | GPL、ROS1、Ceres API 变更 |

**推荐排序**：① livox_ros_driver2（真机主路径 + 数据契约来源）② Livox-SDK2（协议真相源；codec/虚拟设备移植依据）③ LiDAR_IMU_Init（star 最多，但只在 V0.5 真机标定阶段用；其数学值得移植）。

**同类替代**：只标外参时可直接用 FAST-LIO `extrinsic_est_en: true` 在线估计（见 r05），但它不估时延；LI-Init 的优势是时延 + 外参 + 重力 + bias 一次性给出，且带激励引导。MID-360 的扫描花样没有官方开源生成器，社区都用 CSV 回放（XTDrone、Prometheus 插件），本笔记的参数化模型可代替。

---

## 6. 风险与注意事项

1. **默认外参笔误**（`livox_ros_driver2/src/comm/pub_handler.h`：`{0, 1, 1}`）：雷达 IP 未写入 `lidar_configs` 时（改 IP、多雷达自动发现）点云会发生 y'=y+z 剪切，现象像“场景斜切”，极难排查。对策：本地补丁改为 `{0,1,0}`；启动时校验每台雷达都已登记。
2. **外参可能被双重施加**：install_attitude 写入设备 + 主机再变换。对策：驱动外参全 0，外参只存在于我们的标定文件/TF。
3. **未对时时间戳 = 主机收包时刻**（`GetEthPacketTimestamp`）：抖动 + 队列延迟；帧由主机定时器切分。对策：PTP/GPS 同步；否则必须 LI-Init/FAST-LIO 时延补偿。
4. **IMU 加速度单位 g**：直接喂给按 m/s² 设计的算法（包括我们的 EKF/可视化）会差 9.8 倍。对策：网关统一换算并标注 `acc_unit`；LI-Init/FAST-LIO 用 `mean_acc_norm=1`。
5. **时间精度**：ns 纪元约 1.7e18 在 float64 下分辨率 256 ns（PointXYZRTLT 的 `timestamp` 字段即 f64），在 JS Number 下超过 2^53 丢精度。对策：线协议用 `(sec,nsec)` 或 `u64 timebase + u32 offset`；前端只用相对毫秒。
6. **LI-Init 构建**：ROS1-only；`livox_ros_driver::CustomMsg` 与 driver2 不兼容（改 include/命名空间或加 relay 节点）；Ceres ≥2.2 移除 `LocalParameterization`，需锁 Ceres 2.0/2.1；依赖 Python matplotlib。建议只在 Docker（ros:noetic + ceres 2.1）里跑。
7. **LI-Init 使用条件**：启动后静止 >5 s；需要三轴充分旋转激励（手持或低空机动），多旋翼上做大角度 roll/pitch 有安全风险——建议在无人机装机前手持整机标定，或用吊架旋转。`mean_acc_norm` 与 IMU 来源必须匹配（飞控 IMU 9.805 / 雷达内置 1）。
8. **MID-360S 规格待核**：协议兼容已由源码确认，但 FOV/点频/量程可能与 MID-360 不同，mock 参数卡需按实际型号更新；ESC Slow 模式会改变扫描节奏（需重新拟合花样）。
9. **扫描花样来源**：`mid360.csv` 是社区 Gazebo 插件使用的花样表，非 Livox 官方数据手册；参数化模型仅保证统计特性（覆盖率、俯仰分布、交织、帧率），不保证逐点复现（逐点方位误差会随时间累积，这对 mock 无影响）。有真机后用静止室内录制（全点有回波）反解角度表替换。
10. **性能**：虚拟雷达 z-buffer 的瓶颈在裁剪（Python 循环 + concatenate），多机时需瓦片缓存/多进程；UrbanScene3D 5M 采样云稀疏，膨胀核过大会“糊墙”、过小会漏检。浏览器侧绝不做射线求交（无 GPU 的 headless Chromium 下 WebGL2 软件渲染尤其如此）。
11. **网络**：MID-360 单机 ≈ 23 Mbit/s；多机实采集中到一台地面站需千兆交换；Web 端必须抽稀（§3.3）。
12. **构建路径**：driver2 CMake `find_library(... /usr/local/lib)` 硬编码，而本项目禁止全局安装——需 `-DCMAKE_PREFIX_PATH`/补丁或在机载镜像中安装，不在开发机上构建。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§2 硬件平台**：统一写作“MID-360S（Livox dev_type 35，与 MID-360 协议兼容）”，并补充传感器关键约束：垂直 FOV −7°~+52°、200 kHz、10 Hz、4 束非重复扫描、内置 IMU（acc 单位 g）。这些约束直接影响任务规划（正装几乎不看正下方）。
2. **§27 P600 Digital Twin**：增加 `Sensor Mount`（安装位姿，含“前倾 20° 正装 / 倒装 / 自定义”预设）与 `Sensor Model`（§3.1.7 参数卡）两项；外参与时延属于**载具**资产而不是 World 资产，建议新增 Vehicle Package：`vehicles/p600-01/{model.yaml, sensors/*.yaml, calib/lidar_imu.yaml, calib/history/}`。
3. **§8 Geometry World 的 “Sensor Simulation”**：原文把 LiDAR 仿真推迟到 Isaac Sim（§35 第二阶段）。建议 V0.2 就提供轻量虚拟 MID-360（服务器端球面 z-buffer + 参数化花样），因为 MVP 的“无人机在真实世界里飞”如果没有传感器视角，就只是轨迹动画；Isaac Sim 保留为高保真后端。
4. **§16 SensorLayer**：LiDAR FOV 应画成 360° 环形壳而非锥体；增加 “Live Scan” 子层（环形缓冲 + 余晖）与“积分时间”滑杆（0.1–2 s），直观展示非重复扫描随时间变密（覆盖率曲线见 §3.1.2）——这也是一个很好的演示卖点。
5. **§23 Fog / §22 Rain / §24 Sand → LiDAR**：给出可计算模型而不是列表：Beer–Lambert 两程衰减 + Koschmieder 能见度 + 杂波注入 + Livox tag 位 + 设备侧 `fog_noise_filter(0x0028)` 开关（§3.1.4）。这样 Environment E(x,y,z,t) 的 `visibility` 字段就有了明确的传感器消费方。
6. **§28 DroneState**：`sensors` 字段结构化：`lidar{model, status(work_state), time_sync(none/ptp/gps/sim), time_offset_ns, point_rate, pkt_loss, core_temp, hms_code}`、`imu{rate, acc_unit}`；映射 Livox 推送 KV（0x8006/0x8007/0x800C/0x8011）。
7. **§36 通信**：在 “PX4 → ROS2 → Simulation Gateway → WebSocket” 之外，补一条 **传感器通道**：`livox_ros_driver2 → Sensor Gateway（抽稀/量化/对时检查）→ 二进制 WebSocket（独立通道，带背压）`；录制格式用 ROS2 bag（MCAP），不要依赖 lvx（driver2 的 ROS2 版不支持 lvx 回放）。
8. **§37 频率表**：补充 LiDAR 10 Hz 帧 / 200 kHz 点、IMU 200 Hz；Web 端扫描点下行 ≤ 5k 点/帧/机。
9. **§48 V0.5 Real World Fusion**：显式拆出三个子里程碑：(a) 时间同步（PTP/GPS，验收 time_type≠0）；(b) LiDAR-IMU 标定（LI-Init，结果入 Vehicle Package，带版本）；(c) 标定质量回归（Python 移植版 + mock 注入已知外参/时延做自检）。否则 V0.5 的 “metric 级 World” 缺少可验证前提。
10. **§33 技术栈表**：把 “LiDAR | Livox MID-360” 拆成：SDK = Livox-SDK2 1.5.2；Driver = livox_ros_driver2 1.2.8（本地补丁）；Calibration = LI-Init（ROS1 Docker）/ FAST-LIO 在线外参；Time Sync = linuxptp。
11. **坐标与单位约定（缺失）**：架构说明书应一次性定义：Livox 雷达系 FLU、毫米整型原始值；ROS ENU/FLU（REP-103）；Three.js Y-up 右手系；给出唯一的 ENU→Three 变换矩阵，并规定 RPY 一律 ZYX（与 driver2、LI-Init 一致）、时间一律 `u64 ns` 或 `(sec,nsec)`。
12. **MVP 范围（§43）**：在 “WebSocket → Drone Movement” 之后加一步 “Virtual MID-360 Scan → Live Scan Layer”，成本低（本笔记已给出公式与基准），但能让 Demo 体现“真实世界 + 真实传感器”这一核心定位。
