# r08 研究笔记：faster_lio_localization（LIO-Lite）/ FASTLIO2_ROS2 —— 无人机 MID-360 建图、存图、回环与重定位

> 研究单元：r08 ｜ 对应设计文档：01-design.md §2、§6、§7、§26–29、§36–37、§41、§48（V0.5）、§49（V0.6）
> 源码位置：`/data/projs/anet-drone/refs/lidar/faster_lio_localization`（包名 `lio_lite`，代码在 `src/LIO-Lite/`）、`/data/projs/anet-drone/refs/lidar/FASTLIO2_ROS2`（ROS2 五个包：`interface / fastlio2 / pgo / localizer / hba`）
> 交叉参考：`refs/sim/Prometheus/Modules/{FAST_LIO, uav_control}`（P600 官方栈如何把 LIO 喂给 PX4）、r05 笔记（FAST-LIO2 IEKF / ikd-Tree 内核已在 r05 详述，本笔记不重复，只讲“建图 → 存图 → 回环 → 重定位 → 与 World Package 对接”这条链）
> 验证脚本（本机 numpy 实测，无 ROS/GPU）：`/data/projs/anet-drone/.cache/research/r08/{grav_check.py, bounds.py, scan_budget.py, reloc_proto.py}`，第二轮重定位原型输出 `reloc_run2.txt`

---

## 0. 结论速览

| 仓库 | stars / 最后提交 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|---|
| liangheming/FASTLIO2_ROS2 | 761 / 2026-08-10 | ROS2 Humble 重构的 FAST-LIO2（ikd-Tree + 21 维 IESKF）+ **关键帧回环 PGO（GTSAM iSAM2）** + **两阶段 ICP 重定位（map→odom TF）** + **BLAM/HBA 一致性地图优化** + 存图服务（patches + poses.txt + map.pcd） | **adopt**（V0.5 机载/离线 ROS2 主干，需先打 §6.1 的 8 个补丁并按 §4.2 重调无人机参数）＋ **port**（map→odom 重定位模式、关键帧/回环流程、BLAM 平面厚度作为地图 QA） | MVP：移植 mock 定位状态机与数据契约；V0.5a 建图 / V0.5b 重定位；V0.6 多机共图 | 5/5 |
| Liansheng-Wang/faster_lio_localization（LIO-Lite） | 212 / 2024-11-28（代码实质停在 2024-01） | ROS1 单节点 faster-lio（iVox）改版：建图 / 定位双模式、**地图 2D 分块 + 3×3 动态加载**、NDT→ICP 全局初始化、**冻结先验地图的紧耦合定位**、直接发布 `/mavros/vision_pose/pose` | **reference + port**（iVox 哈希 + LRU、分块/动态加载、紧耦合先验定位思路）；仅在“原厂 Prometheus ROS1 栈上快速验证重定位”时临时 adopt | MVP：移植 iVox LRU 与分块调度；V0.5 参考 | 3/5 |

一句话结论：

1. **两个仓库恰好代表两种重定位范式**。LIO-Lite 是“先验地图冻结进 iVox，IEKF 直接对先验地图做点到面更新”（紧耦合，输出即 map 系位姿，平滑、零跳变，但飞出已建图区域就没有有效点、只能靠 IMU 漂移）；FASTLIO2_ROS2 是“LIO 永远自建局部图，独立 `localizer_node` 以 1 Hz 做 scan-to-map ICP，只发布 `map→lidar(odom)` TF”（松耦合，飞出地图仍能继续飞，但 TF 有阶跃）。**P600 推荐以 FASTLIO2_ROS2 的松耦合为主 + 本笔记 §3.2 的门控/平滑/多假设改进**；PX4 吃连续的 odom 系位姿，任务/多机规划在 map 系，二者用 `T_map_odom` 转换（§3.10）。
2. **“先建图后重定位”所需的全部 ROS2 服务在 FASTLIO2_ROS2 里都有**：`/pgo/save_maps`（关键帧 patch + 位姿 + 拼接地图）→ `/hba/refine_map`（离线 BA 精化）→ `/localizer/relocalize` + `/localizer/relocalize_check`。但默认参数是给地面/手持设备调的（关键帧 0.5 m、回环搜索半径 1 m、里程计因子 z 方差比 xy 小 100 倍、每个关键帧存全分辨率点云），**无人机直接用会内存爆、找不到回环、高度漂移修不掉**。
3. **MID-360 的能力边界决定了建图航线**（本机实测，§3.11）：MID-360 竖直视场 −7°~+52°，40 m@10% 反射率。在 UrbanScene3D San Francisco 上把虚拟雷达放在离地 30 m、正装：视场内可见点 **中位数为 0**；倒装时仅 48.6°–52° 一条窄带能打到 40 m 内的地面。地面可见的高度上限 `h ≤ R·sin(θ_down)`：正装前倾 20° 约 18 m，倒装约 31 m（R=40 m）。结论：**LIO 建图/重定位只适用于低空（≤20–30 m AGL）或城市峡谷（立面在 40 m 内）**，高空飞行的定位源仍是 RTK，LIO 地图不能当城市级 World 几何主来源——与 r05 结论一致并给出了量化依据。
4. **对 MVP（Web + mock）直接有用的三块**：① iVox 体素哈希 + LRU（浏览器“实时扫描累积层”与后端空间查询的有界内存结构，§3.5）；② LIO-Lite 的 2D 分块 + 邻域加载（修正其卸载 bug 后，可直接作为后端定位图/前端实时层的分块调度器，§3.4）；③ “map→odom”定位 mock 状态机 + WS 契约（§3.8），让 UI 在没有真机时就能展示“真值 / LIO 估计 / 重定位修正 / 定位状态”。本机用 numpy 复现了“体素哈希 NN + 金字塔点到面 ICP”（§3.2.4）：初值误差 ≤2 m/5° 时 9/10 收敛到厘米级，5 m/15° 仅 3/10，且稀疏图上 fitness 无法区分对错——据此给出“起降点/RTK 初值 + 三级金字塔 + 内点率与时间一致性门控”的参数。
5. **源码中确认的缺陷**：LIO-Lite 4 处（其中“重力对齐初始化时重力状态未旋转”在传感器倾斜 20° 时产生 3.4 m/s² 的虚假加速度，数值验证见 §6.1）；FASTLIO2_ROS2 8 处（IMU 加速度硬编码 ×10、10000 点定长缓冲越界、`lock_guard` 临时对象不加锁、PCL VoxelGrid 在城市级地图上整型溢出导致降采样静默失效、重定位欧拉角 Z-X-Y 组合顺序等）。adopt 前必须 fork 打补丁。

---

## 1. 仓库概览

### 1.1 faster_lio_localization（LIO-Lite）

| 项 | 内容 |
|---|---|
| 来源 | 在 gaoxiang12/faster-lio 基础上改（README_CN 自称“faster-lio 纯度 98%”），参考 FAST-LIO2、LIO-SAM、《自动驾驶中的 SLAM 技术》 |
| 构建 | catkin（ROS1 Melodic/Noetic），C++17，`-O3`；依赖 glog、gflags、Eigen、PCL ≥1.8、yaml-cpp、**TBB**（`std::execution::par_unseq` 依赖；仓库内附 `3rdparty/tbb2018_*.tgz`，`CUSTOM_TBB_DIR` 可指向） |
| Livox 消息 | `3rdparty/livox_ros_driver` 只含 v1 `CustomMsg/CustomPoint` 定义（包名 `livox_ros_driver`），MID-360 实机驱动是 `livox_ros_driver2`，类型名不同，需要改包名或转发 |
| 可执行 | `run_mapping_online`（建图）、`run_location_online`（重定位）、`test_split` / `test_load`（分块测试）、`src/test/pcd_to_bird_eye.cc`（未加入 CMake，BEV 俯视图工具） |
| 雷达类型 | `preprocess/lidar_type`：1=Livox（含 MID-360）、2=Velodyne32、3=Ouster64、4=Hesai XT16 |
| 分支说明 | README：优化版分支在 Orin NX 上 <5 Hz，**滤波（ESKF）版在 NX 上 CPU 30%–40%**；本地 shallow clone 只有 `main`（滤波版） |
| 更新记录 | 2023-10 地图分块 + 增量加载；2023-11 重力约束定义水平面；2023-11 适配 Hesai XT16；2024-01 视觉着色拆到 `LIO-Lite-Vison` 另一个仓库 |

### 1.2 FASTLIO2_ROS2

| 项 | 内容 |
|---|---|
| 环境 | Ubuntu 22.04 + ROS2 Humble（ament_cmake，C++17，`-O3`） |
| 依赖 | PCL、Eigen、**Sophus 1.22.10**（`SOPHUS_USE_BASIC_LOGGING` 去 fmt 依赖）、**GTSAM**（pgo、hba）、`livox_ros_driver2`、yaml-cpp、OpenMP（`MP_PROC_NUM=2`） |
| 包 | `interface`（5 个 srv）、`fastlio2`（`lio_node`）、`pgo`（`pgo_node`）、`localizer`（`localizer_node`）、`hba`（`hba_node`） |
| 输入 | **只接 `livox_ros_driver2/msg/CustomMsg`**（无 PointCloud2 分支），即只支持 Livox |
| 活跃度 | 2026-08 仍在合并外部 PR（#43，本地 shallow clone 仅见该合并提交）；README 明确提示：timer/subscriber/service 回调都在单线程 executor 上，性能不足时会阻塞，建议自行拆线程 |
| 数据 | 作者在百度网盘提供示例 bag |

---

## 2. 源码结构与关键模块

### 2.1 LIO-Lite（`src/LIO-Lite/`）

| 文件 / 符号 | 作用与要点 |
|---|---|
| `src/run_mapping_online.cc::main` | 5 kHz 轮询 `LaserMapping::Run()`；SIGINT → `Finish()` 存图 |
| `src/run_location_online.cc::main` | 先 `Load_map()` 再轮询 `Run_location()` |
| `include/laser_mapping.h::LaserMapping` | 唯一核心类：`IVox<3,DEFAULT,PointXYZINormal>` 局部图、`esekf<state_ikfom,12,input_ikfom>`（23 维，含 S2 重力、在线外参）、定位相关成员 `global_map_ / hold_map_ / map_data_index_` |
| `src/laser_mapping.cc::LoadParams` | 参数：`location_mode`、`split_map`、`sub_grid_resolution`、`load_g_map / load_f_map`、`load_eaf_size`、`init_trans / init_rpy`（度）、`ivox_grid_resolution / ivox_nearby_type / ivox_capacity`；注意 `preprocess/blind2`、`max_range2` 是**距离平方** |
| `src/pointcloud_preprocess.cc::AviaHandler` | 取 `line<num_scans`、`tag&0x30 ∈ {0x00,0x10}`、`i % point_filter_num == 0`、与前一点不重合、`blind2 < r² < max_range2`；`curvature = offset_time/1e6`（ms） |
| `::SyncPackages` | `lidar_end_time = bag_time + 最后一点 curvature/1000`，异常时用滑动平均扫描时长 |
| `include/imu_processing.hpp::IMUInit / SetInitPose / UndistortPcl` | 前 20 帧静止初始化；`SetInitPose` 用罗德里格斯把测得重力方向转到 −z（“2023-11 重力约束”）；逐 IMU 中值积分 + 逐点反向去畸变 |
| `::Run`（建图） | 去畸变 → `VoxelGrid(filter_size_surf=0.5)` → `update_iterated_dyn_share_modified` → `MapIncremental` → 发布/累积 |
| `::ObsModel` | iVox 取 5 近邻 → `esti_plane`（QR 最小二乘平面，阈值 0.1 m）→ 有效条件 `|p_body| > 81·pd2²`（即 FAST-LIO 的 `1−0.9|pd2|/√|p| > 0.9`）→ 12 列雅可比 `[n, (p_I)^×Rᵀn, (外参旋转项), Rᵀn]` |
| `::MapIncremental` | 体素中心规则（与 FAST-LIO `map_incremental` 相同，r05 §3.1）；体素为空时点直接加入 |
| `::Finish` | `maps/GlobalMap.pcd`（累积的每帧降采样世界点）、`maps/FeatureMap.pcd`（**每帧参与了点到面匹配的有效点**，比全局图干净）、`SplitMap(FeatureMap)` |
| `::SplitMap` | 2D 网格键 `gx = floor((x − R/2)/R)`，`R = sub_grid_resolution`（默认 80 m），写 `maps/split_map/{gx}_{gy}.pcd` + `map_index.txt`（先 `rm -rf` 目录） |
| `::Load_map` | 读 GlobalMap（仅可视化）与 FeatureMap → `VoxelGrid(load_eaf_size=0.5)`；`split_map=false` 时整图进 iVox，否则只读分块索引 |
| `::initialpose` | 单帧去畸变点云 vs **整幅** GlobalMap：`pcl::NDT(res 0.8, 40 it)` → `pcl::ICP(max_corr 40 m, 100 it)`；`fitness ≤ 0.25` 视为成功 → `kf_.change_x(pos, rot)` → 释放全局图 → `DynamicLoadMap`；初值来自 yaml `init_trans/init_rpy` 或 RViz `/initialpose`（**z 被强制为 0.2 m**，地面机器人假设） |
| `::Run_location` | 与 `Run` 相同的 IEKF，但观测模型换成 `ObsModel_location` 且**不调用 `MapIncremental`（先验图冻结）**；每帧 `DynamicLoadMap(pos)` |
| `::DynamicLoadMap` | 当前位置所在块 + 8 邻块，若在索引中且未加载则 `loadPCDFile` → `ivox_->AddPoints`；与当前块欧氏距离 > 3 块的从 `hold_map_` 删除（**但点没有从 iVox 删除**，见 §6.1） |
| `::PublishOdometry` | `/Odometry`（frame `map`→`body`，协方差按 `[rot,pos]` 重排，与 ROS `[pos,rot]` 相反，r05 已指出）＋ **`/mavros/vision_pose/pose`**（直接喂 PX4 EV）＋ TF `map→body` |
| `include/ivox3d/ivox3d.h::IVox` | 体素哈希 `unordered_map<Key, list::iterator>` + `list<pair<Key,Node>>` 作为 **LRU**：命中则 `splice` 到表头，新体素插表头，`size ≥ capacity` 时淘汰表尾；`Pos2Grid = round(p/res)`；哈希 `(x·73856093 ^ y·471943 ^ z·83492791) % 1e7`；邻域 CENTER/6/18/26 |
| `include/ivox3d/ivox3d_node.hpp` | `IVoxNode`：体素内线性数组，KNN 用 `nth_element`；`IVoxNodePhc`：体素内再按 Hilbert 曲线（`hilbert.hpp`，order 6）分小立方体 |
| `include/register/ndt.hpp::P2P::Ndt3d` | 手写 NDT（体素 1 m、SVD 求逆协方差、NEARBY6），`initialpose2` 使用，默认未启用 |

LIO-Lite 话题：`/cloud_registered`、`/cloud_registered_body`、`/cloud_registered_effect_world`、`/Odometry`、`/path`、`/mavros/vision_pose/pose`；定位模式另有 `/initialpose`（订阅）、`/global_map`、`/feature_map`（2 s 定时，仅在有订阅者时发）。

### 2.2 FASTLIO2_ROS2 节点图

```text
/livox/lidar (livox_ros_driver2/CustomMsg, 10 Hz)   /livox/imu (sensor_msgs/Imu, 200 Hz, 单位 g)
                    │                                        │
                    v                                        v
        ┌────────────────────── fastlio2/lio_node (timer 20 ms) ─────────────────────┐
        │ MapBuilder: IMU_INIT → MAP_INIT → MAPPING                                  │
        │ IMUProcessor(初始化/前向传播/去畸变) + LidarProcessor(ikd-Tree + IESKF)     │
        └───┬───────────────┬──────────────────┬─────────────────────┬───────────────┘
            │/fastlio2/     │/fastlio2/         │/fastlio2/lio_path   │TF lidar→body
            │body_cloud     │lio_odom           │/fastlio2/world_cloud│（world_frame="lidar"=odom 语义）
            v               v
   ApproximateTime(cloud, odom) 同步
      ├──────────────> pgo/pgo_node (timer 50 ms)            → TF map→lidar，/pgo/loop_markers
      │                  srv /pgo/save_maps{file_path, save_patches}
      │
      └──────────────> localizer/localizer_node (timer 10 ms, 1 Hz 配准) → TF map→lidar，/localizer/map_cloud
                         srv /localizer/relocalize{pcd_path,x,y,z,yaw,pitch,roll}
                         srv /localizer/relocalize_check{code} → valid

   hba/hba_node（离线）：srv /hba/refine_map{maps_path} → /hba/map_points；srv /hba/save_poses{file_path}
```

注意：`pgo_node` 与 `localizer_node` **都发布 `map→lidar` TF**，二者是“建图模式”与“重定位模式”的互斥组件，不能同时运行（TF 冲突）。

#### 2.2.1 `fastlio2`（LIO）

| 文件 / 符号 | 要点 |
|---|---|
| `src/lio_node.cpp::LIONode` | `imuCB`：**`acc × 10.0`**（假设 Livox 内置 IMU 以 g 为单位）；`lidarCB`：`Utils::livox2PCL`（步长 `lidar_filter_num`，`line<4`，tag 过滤，`min/max_range`，`curvature = offset_time/1e6` ms）；`syncPackage`：按 curvature 排序、`cloud_end_time = start + 最后一点/1000`；`timerCB`（50 Hz）→ `MapBuilder::process` → 发布 odom（仅 pose + 机体系速度，**无协方差**）、TF、`body_cloud`（**去畸变后、未体素降采样**、IMU 系）、`world_cloud`、path（有订阅者才发） |
| `map_builder/map_builder.cpp::process` | 三态机：`IMU_INIT`（累计 `imu_init_num=20` 个 IMU 样本）→ `MAP_INIT`（首帧直接 `ikdtree.Build`）→ `MAPPING` |
| `map_builder/imu_processor.cpp::initialize` | `bg = mean(gyro)`；`gravity_align=true` 时 `r_wi = FromTwoVectors(−acc_mean, −z)`、`g = (0,0,−9.81)`，**世界系水平**；P 初值：外参块 1e-5、bg/ba 块 1e-4 |
| `map_builder/ieskf.{h,cpp}` | 21 维误差态 `[r_wi, t_wi, r_il, t_il, v, bg, ba]`（**重力不估计**，比 FAST-LIO 的 23 维少 S2）；`predict` 用右雅可比；`update`：`H = JᵀP⁻¹J + Σ JᵢᵀR⁻¹Jᵢ`，`b = JᵀP⁻¹δ + Σ JᵢᵀR⁻¹rᵢ`，`δ = −H⁻¹b`，收敛条件 `|δr| < 0.01°` 且 `|δt| < 0.015 cm`，`P = L H⁻¹ Lᵀ` |
| `map_builder/lidar_processor.cpp::trimCloudMap` | 局部立方体（`cube_len=300`、`det_range=60`、`move_thresh=1.5`）：距边界 ≤ `1.5·60=90 m` 时平移 `max((300−180)·0.45, 60·0.5)=54 m`，`Delete_Point_Boxes` |
| `::updateLossFunc` | ikd-Tree 5 近邻，第 5 近邻距离² ≤ 5 m²，`esti_plane(0.1)`，`s>0.9`，`J = [−nᵀR(R_il p + t_il)^×, nᵀ, (外参列)]`，权重 `lidar_cov_inv=1000`（σ≈3.2 cm） |
| `::incrCloudMap` | 体素中心规则，`map_resolution=0.3` |
| 定长缓冲 | 构造函数把 `m_cloud_down_world / m_norm_vec / m_nearest_points / m_point_selected_flag` 固定为 **10000**（见 §6.1） |

默认 `config/lio.yaml`：`lidar_filter_num 6`、`lidar_min/max_range 0.5/30`、`scan_resolution 0.15`、`map_resolution 0.3`、`na/ng 0.01`、`nba/nbg 1e-4`、`ieskf_max_iter 5`、`r_il=I`、`t_il=[-0.011,-0.02329,0.04412]`（MID-360 厂商外参）、`lidar_cov_inv 1000`。

#### 2.2.2 `pgo`（关键帧 + 回环 + iSAM2）

| 符号 | 要点 |
|---|---|
| `pgos/simple_pgo.cpp::isKeyPose` | 与上一关键帧相比 `Δt > key_pose_delta_trans(0.5 m)` 或 `Δθ > key_pose_delta_deg(10°)` |
| `::addKeyPose` | 初值 `T_global = T_offset · T_local`；idx 0 加 `PriorFactor`（方差 1e-12）；其余加 `BetweenFactor(T_{i-1}^{-1} T_i)`，**方差 `[1e-6,1e-6,1e-6 | 1e-4,1e-4,1e-6]`（gtsam Pose3 切空间顺序为 [rot, trans]，即 z 平移比 xy 紧 100 倍）**；关键帧保存全分辨率 `body_cloud` |
| `::searchForLoopPairs` | 关键帧 ≥10；距上次回环 ≥ `min_loop_detect_duration(5 s)`；在所有历史关键帧 **全局位置**上建 KD 树，`radiusSearch(loop_search_radius=1.0 m)`，取第一个时间差 > `loop_time_tresh(60 s)` 的候选；target = 候选 ±`loop_submap_half_range(5)` 帧拼接子图（0.1 m 体素），source = 当前帧（全局系）；ICP（max_corr 10 m、50 it）`fitness ≤ loop_score_tresh(0.15)` 通过 |
| `::smoothAndUpdate` | 回环因子噪声 `Variances(ones·score)`；iSAM2 `update` 1 次（有回环时再 +4 次）；更新全部关键帧全局位姿；`T_offset = T_global,last · T_local,last⁻¹` |
| `pgo_node.cpp::timerCB` | 50 ms 一次，**只取队首一帧并清空队列**（丢弃其余）；非关键帧只重发 TF |
| `::saveMapsCB` | 目录需已存在；`patches/{i}.pcd`（IMU 系点云，binary）、`poses.txt`（`name x y z qw qx qy qz`，全局位姿）、`map.pcd`（全部 patch 变换后拼接，**不降采样**） |

#### 2.2.3 `localizer`（两阶段 ICP 重定位）

| 符号 | 要点 |
|---|---|
| `localizers/icp_localizer.cpp::loadMap` | 整幅 PCD → 两份体素化目标：rough 0.25 m、refine 0.1 m（`pcl::VoxelGrid`） |
| `::setInput / align` | 源点云同样两份体素化；rough ICP（5 it，`fitness ≤ 0.2`）→ refine ICP（10 it，`fitness ≤ 0.1`）；**未设置 `setMaxCorrespondenceDistance`**（PCL 默认近似无穷） |
| `localizer_node.cpp::timerCB` | 10 ms 定时；每 `1/update_hz`（默认 1 s）配准一次；初值：收到 relocalize 服务后一直用服务给的初值直到成功，否则用 `T_offset · T_odom,last`；成功则 `T_offset = T_map_body · T_odom_body⁻¹`；每个 tick 都用“最后一帧点云时间戳”发 TF `map→local_frame` |
| `::relocCB` | 加载 `pcd_path`，初值 `R = Rz(yaw)·Rx(roll)·Ry(pitch)`（弧度，**Z-X-Y 顺序**），`service_received=true` |
| `::relocCheckCB` | `code==1` 恒返回 true，否则返回 `localize_success` |

#### 2.2.4 `hba`（BLAM / HBA 一致性优化，离线）

| 符号 | 要点 |
|---|---|
| `hba_node.cpp::refineMapCB` | 读 `maps_path/poses.txt` + `patches/*.pcd`，每个 patch 0.1 m 体素化后 `HBA::insert`；置位后 `mainCB` 连跑 `hba_iter=5` 次 `HBA::optimize` |
| `hba/hba.cpp::calcLevels` | `L = ⌊ 0.5·ln(3N²(s³−s)/w³) / ln s ⌋`（w=window 20，s=stride 10）：N=100→1、300/1000→2、3000/10000→3 |
| `::constructHierarchy` | 每层以窗口 20、步长 10 滑动，每窗一个 `BLAM` 做局部 BA；上层把下层每窗压成一个“帧”（首帧位姿 + 窗内平面点） |
| `::getAllFactors / optimize` | 各层相邻帧的相对位姿 + BLAM Hessian 块作为信息矩阵 → GTSAM `BetweenFactor` → `LevenbergMarquardtOptimizer`（无先验因子，靠 LM 阻尼固定规范） |
| `hba/blam.cpp::OctoTree` | 体素 0.5 m，自适应八叉细分 `max_layer 3`，`λ_min(Cov) < plane_thresh(0.01 m²)` 判为平面；代价 **Σ λ_min**（平面厚度²之和），解析一阶/二阶导 `dp / dp2 / fp`；LM（u=0.01，v=2） |
| `::writePoses` | 只写 `x y z qw qx qy qz`（**没有文件名列，与 pgo 的 poses.txt 格式不一致**） |

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 两种重定位范式与选型

| 维度 | 紧耦合先验图（LIO-Lite `Run_location`） | 松耦合 map→odom（FASTLIO2_ROS2 `localizer`） |
|---|---|---|
| 估计器 | 同一 IEKF，观测直接对冻结的先验图 | LIO 自建局部图；另起 ICP 估 `T_map_odom` |
| 输出频率 / 平滑 | 10 Hz，连续 | LIO 10 Hz 连续 + 修正 1 Hz 阶跃 |
| 飞出先验图 | 有效点骤减 → “No Effective Points” → IMU 积分发散 | LIO 照常，修正停更，状态降级 |
| 环境变化（施工、树叶、车辆） | 先验不更新，残差大，易退化 | LIO 局部图吸收变化；ICP 只要足够重叠 |
| 实现复杂度 | 低（改观测模型即可） | 中（多一个节点 + TF 管理） |
| 对多机共图 | 每机都在同一 map 系，天然共享 | 每机 `T_map_odom_i`，规划在 map 系、控制在 odom 系 |

**推荐（P600）**：主链用松耦合（安全：地图外仍可飞）；在 `TRACKING` 状态且离地图边界 > 2 块时，可选“先验注入”——把先验分块点以低权重加入 LIO 的 ikd-Tree（LIO-Lite 思想），既抑制漂移又不丢失出图能力。PX4 只接 odom 系连续位姿（§3.10）。

### 3.2 松耦合重定位：map→odom（FASTLIO2_ROS2 localizer，改进版）

#### 3.2.1 坐标关系

记 LIO 输出 `T_odom_body(t)`（`/fastlio2/lio_odom`，odom 即 `world_frame="lidar"`），重定位 ICP 得到 `T_map_body(t)`，则

```text
T_map_odom = T_map_body(t) · T_odom_body(t)⁻¹
R_off = R_mb · R_obᵀ ,  t_off = t_mb − R_mb · R_obᵀ · t_ob      (localizer_node.cpp::timerCB)
下一次初值：T_guess = T_map_odom · T_odom_body(t_now)
任意时刻 map 系位姿：T_map_body = T_map_odom · T_odom_body     （连续 × 分段常数）
```

#### 3.2.2 原实现的问题

1. PCL ICP 不设最大对应距离，粗配时远处错误对应拉偏；只看 fitness（内点均方距离），**没有内点率与退化检测**，走廊/开阔地/水面会“收敛”到错误位置。
2. 服务初值只有一个假设；无人机上电朝向误差（磁罗盘 10°–30°）足以让 5 次迭代的粗 ICP 失败。
3. 成功就直接替换 `T_offset`，UI/控制看到阶跃；失败时没有任何状态上报（`relocalize_check` 只有布尔）。
4. 整图加载 + 全图 VoxelGrid：城市级地图在 0.1–0.5 m 体素上 int32 溢出（本机实测 UrbanScene3D 6 城中 5 城溢出，§6.1），降采样静默失效。

#### 3.2.3 改进伪代码（移植到我们的 `reconstruction/lidar/localizer` 与 mock）

```python
class MapOdomLocalizer:
    # 参数（无人机、户外、MID-360）；金字塔三级，数值经 §3.2.4 原型验证
    UPDATE_HZ      = 2.0      # 1–2 Hz
    PYRAMID        = [(3.0, 6.0, 10),   # (体素 m, max_corr m, 迭代)：粗级决定捕获半径
                      (1.0, 2.0, 10),
                      (0.3, 0.6, 15)]   # 真机稠密图可降到 0.2 / 0.4
    YAW_HYP        = 12       # 初始化/丢失时 30° 间隔多假设（只在最粗级跑）
    XY_HYP_STEP    = 3.0      # m，丢失恢复时在 ±6 m 内再加平移网格假设（25×12 个，最粗级很便宜）
    INLIER_OK      = 0.88     # 精级内点率下限（原型：正确解中位数 0.92–0.93，错误解 0.60–0.89）
    FIT_RATIO_OK   = 1.10     # 精级 fitness ≤ 1.10 × 地图“颗粒度基线”（同体素下地图自配准的均方 NN 距离）
    CONSIST_OK     = (0.3, 0.5)   # m, deg：连续两次成功的 T_map_odom 必须一致才进入/保持 TRACKING
    DEG_OK         = 0.02     # 平移 Hessian 最小/最大特征值比（点到面法向协方差）
    JUMP_RESET     = 2.0      # m，超过则视为重置（reset_counter++）
    V_SMOOTH, W_SMOOTH = 0.5, 5.0   # m/s, deg/s，UI/规划用平滑速率

    def on_scan(self, cloud_body, T_odom_body, t):
        self.last = (cloud_body, T_odom_body, t)             # 只保留最新帧

    def tick(self, now):
        if now - self.t_last_run < 1/self.UPDATE_HZ: return
        cloud, T_ob, t = self.last
        tiles.ensure_loaded(self.predict_map_pos(T_ob))      # §3.4 分块调度
        if self.state == "WAIT_INIT":
            guesses = [self.init_guess.rotz(k*2π/YAW_HYP) for k in range(YAW_HYP)]
        elif self.state == "LOST":
            guesses = [self.last_good.shift(dx, dy).rotz(k*2π/YAW_HYP)
                       for dx in (-6,-3,0,3,6) for dy in (-6,-3,0,3,6) for k in range(YAW_HYP)]
        else:
            guesses = [self.T_map_odom @ T_ob]
        vox0, mc0, it0 = PYRAMID[0]
        best = argmin(guesses, key=lambda G: icp(cloud@vox0, G, mc0, it0).score)  # score = fit / max(inlier,1e-3)
        T = best.T
        for vox, mc, it in PYRAMID[1:]:
            T, fit, inl, deg = icp(cloud@vox, T, mc, it)                   # 点到面，Huber
        ok = inl > INLIER_OK and fit < FIT_RATIO_OK * self.map_grain[PYRAMID[-1][0]] and deg > DEG_OK
        if ok:
            T_meas = T @ inv(T_ob)
            if not consistent(T_meas, self.T_prev_meas, *CONSIST_OK):      # 单次成功不可信（原型：fitness/内点率区分度弱）
                self.T_prev_meas = T_meas; return
            self.T_prev_meas = T_meas
            jump = dist(T_meas, self.T_map_odom)
            if self.state != "TRACKING" or jump.t > JUMP_RESET:
                self.T_map_odom = T_meas; self.reset_counter += 1          # 硬切
            else:
                self.T_target = T_meas                                     # 软切：由 smooth() 逼近
            self.t_ok = t; self.last_good = T; self.state = "TRACKING"
        self.state = self.degrade(now - self.t_ok)   # TRACKING <3 s < DEGRADED <10 s < LOST
        publish_tf("map", "odom", self.T_map_odom, stamp=t)
        publish_status(self.state, fit, inl, deg, jump, self.reset_counter)

    def smooth(self, dt):   # 50 Hz 调用：平移限速、旋转 slerp 限速
        self.T_map_odom = step_towards(self.T_map_odom, self.T_target, V_SMOOTH*dt, W_SMOOTH*dt)
```

点到面 ICP 单次迭代（左扰动，`q = R p + t`，目标点 `m`、法向 `n`）：

```text
r_i = nᵢᵀ (qᵢ − mᵢ)
J_i = [ (qᵢ × nᵢ)ᵀ , nᵢᵀ ]            # 1×6，对 [δθ, δt]
H = Σ wᵢ J_iᵀ J_i ,  g = Σ wᵢ J_iᵀ r_i ,  δ = −H⁻¹ g
T ← Exp(δ) · T
Huber：wᵢ = 1 (|rᵢ| < c) 否则 c/|rᵢ|，c = 0.5·max_corr
退化度量：deg = λ_min(Σ nᵢnᵢᵀ)/λ_max(Σ nᵢnᵢᵀ)（平移可观性，<0.02 视为退化）
```

实现选择：Python 后端/离线用 **small_gicp**（`refs/lidar/small_gicp`，2026-08 仍活跃，有 `pip install small_gicp` 与 VGICP/GICP/点到面，无需 ROS）；机载 C++ 直接把 `ICPLocalizer` 换成 `small_gicp::align`。

#### 3.2.4 本机原型结果（numpy，San Francisco，地图/扫描为互斥的 50% 随机点）

脚本 `.cache/research/r08/reloc_proto.py`：体素哈希一格一代表点（体素质心 + 平均法向，UrbanScene3D 自带法向），27 邻域向量化 NN（`np.searchsorted` 于排序键），多级点到面 ICP（Gauss-Newton + Huber），虚拟雷达离地 25 m、全向 60 m 取 2 万点、加 3 cm 噪声，初值为真值加平移/偏航扰动。

金字塔 `(3.0 m, 6 m) → (1.0 m, 2 m) → (0.3 m, 0.6 m)`，每组 10 次：

| 初值误差（平移 / 偏航） | 多假设 | 成功（<0.5 m 且 <1.5°） | 误差中位数 | 精级 fitness 成功/失败（m²） | 精级内点率 成功/失败 | numpy 单次耗时 |
|---|---|---|---|---|---|---|
| 2 m / 5° | 否 | **9/10** | 0.01 m / 0.01° | 0.085 / 0.094 | 0.93 / 0.89 | 1.8 s |
| 5 m / 15° | 否 | 3/10 | 2.85 m / 7.8° | 0.086 / 0.116 | 0.92 / 0.69 | 2.5 s |
| 8 m / 30° | 否 | 1/10 | 6.90 m / 27° | 0.085 / 0.118 | 0.93 / 0.60 | 3.3 s |
| 8 m / 30° | 12 偏航 | 4/10 | 2.32 m / **0.21°** | 0.084 / 0.106 | 0.93 / 0.76 | 4.9 s |
| 15 m / 90° | 12 偏航 | 3/10 | 11.0 m / 1.6° | 0.085 / 0.108 | 0.92 / 0.69 | 4.4 s |

同一脚本的第一轮（粗级 1.0 m 体素、只两级）在 2 m/5° 下只有 6/12 成功、5 m/15° 为 0/12，结论：

1. **体素哈希 27 邻域 NN 的捕获半径约为 1.5 × 体素**：粗级体素必须 ≥ 预期初值误差 / 1.5，否则远处根本找不到对应（这也是 FASTLIO2_ROS2 rough 0.25 m + 不限对应距离的另一个极端）。三级金字塔后 2 m/5° 以内几乎总能收敛到厘米级。
2. **平移捕获盆地约 2–3 m**：偏航多假设能把旋转修好（8 m/30° 时旋转误差中位数 0.21°），但平移仍卡在局部极小——低层、重复的城区结构下需要平移网格假设（上面伪代码 `XY_HYP_STEP`）或全局描述子（ScanContext/BEV 位置识别，V0.6+）。因此**冷启动初值必须来自起降点或 RTK（≤2 m / 5°）**，Web 手动拖拽只作兜底。
3. **绝对 fitness 阈值不可用**：稀疏地图上正确解的 fitness 本身就有 0.085 m²（由地图点距决定），与错误解（0.094–0.118）重叠；FASTLIO2_ROS2 的 `refine_score_thresh 0.1` 在这里会放行大量错误解。门控改为“内点率 ≥ 0.88 + fitness ≤ 1.1 × 地图颗粒度基线 + 连续两次一致”。
4. 性能：numpy 版 1.8–4.9 s/次，仅用于离线验证；在线（1–2 Hz × 多机）必须用 small_gicp（C++ 核心，预计 10–50 ms/次）。

### 3.3 全局初始化（冷启动）

LIO-Lite `initialpose()`：单帧 vs 整图，NDT(0.8 m) → ICP(max_corr 40 m)，`fitness ≤ 0.25`。对无人机的改进：

1. **初值精度要求 ≤ 2 m / 5°**（§3.2.4 实测平移捕获盆地约 2–3 m）。**初值来源优先级**：① 起降点（World Package 里登记的 `pads[]`，精确到 0.1 m / 2°）；② RTK 固定解 + `T_enu_map` 反算的位置 + 飞控航向（±10°）；③ Web 上用户拖拽设定（替代 RViz `/initialpose`，**不得像 LIO-Lite 那样强制 z=0.2 m**，而应取该 xy 处地图地面高度 + 机体离地高度）。
2. **起飞前在地面完成重定位**并作为解锁前置条件（`relocalize_check` 返回 true 且连续 3 次 `TRACKING`）；空中丢失才用多假设。
3. 冷启动只加载初值周围 3×3 块 + 一份 1 m 体素的全局粗图（`localization/global_rough.bin`，用于多假设粗配），不加载整图。

### 3.4 地图分块与动态加载（LIO-Lite `SplitMap / DynamicLoadMap`，修正版）

原实现：键 `k = floor((p − S/2)/S)`（S=80 m），加载 `k` 的 3×3 邻域，`‖k_held − k‖₂ > 3` 时从 `hold_map_` 移除——**但 iVox 里的点没删**（只能等 LRU 容量淘汰），再次进入该块会重复加点。修正后的通用调度器（后端定位图、前端实时层、mock 都可用）：

```ts
// 通用 2D 分块驻留集调度（带迟滞），World Package: localization/tiles/{i}_{j}.bin
type Key = `${number}_${number}`;
const S = 64;                      // m，取 2 的幂，便于与 Web 八叉树某一层的节点边界对齐
const R_LOAD = 1.5 * S;            // 覆盖 MID-360 60–70 m 量程
const R_UNLOAD = R_LOAD + S;       // 迟滞，防止边界抖动反复加载
function tileKey(x: number, y: number): [number, number] { return [Math.floor(x / S), Math.floor(y / S)]; }
function tileCenter(i: number, j: number) { return [(i + 0.5) * S, (j + 0.5) * S]; }
function distToTile(p: Vec2, i: number, j: number) {          // 点到方块最近距离，比中心距更合理
  const dx = Math.max(i * S - p.x, 0, p.x - (i + 1) * S);
  const dy = Math.max(j * S - p.y, 0, p.y - (j + 1) * S);
  return Math.hypot(dx, dy);
}
function update(p: Vec2, v: Vec2 /*速度*/) {
  const q = { x: p.x + v.x * 2.0, y: p.y + v.y * 2.0 };        // 2 s 预取
  const want = new Set<Key>();
  const r = Math.ceil(R_LOAD / S);
  const [ci, cj] = tileKey(p.x, p.y);
  for (let i = ci - r - 1; i <= ci + r + 1; i++) for (let j = cj - r - 1; j <= cj + r + 1; j++)
    if (index.has(`${i}_${j}`) && Math.min(distToTile(p, i, j), distToTile(q, i, j)) <= R_LOAD) want.add(`${i}_${j}`);
  for (const k of want) if (!held.has(k)) loadAsync(k);        // 异步读盘/网络，完成后 map.insertTile(k, pts)
  for (const k of held) if (!want.has(k) && distToTile(p, ...parse(k)) > R_UNLOAD) map.removeTile(k);  // 真删除
}
```

`removeTile` 的三种实现：① ikd-Tree `Delete_Point_Boxes(tile_bbox)`（FASTLIO2_ROS2 已有 ikd-Tree，最干净）；② iVox 体素上记 `tileId`，删除该 tile 的全部体素；③ 双缓冲：后台线程用驻留块重建新 iVox 再原子替换（9 块 × 5 万点约几十 ms）。

参数：S=64 m、R_LOAD=96 m、R_UNLOAD=160 m；每块定位点 0.2–0.3 m 体素；机载内存上限按 `驻留块数 × 每块点数 × 16 B` 预算（25 块 × 20 万点 ≈ 80 MB）。

### 3.5 iVox：体素哈希 + LRU（移植到 TS / Python）

用途：① 浏览器“实时扫描累积层”（无人机 mock 雷达/真机 `world_cloud` 流进来，按体素去重、容量有界、久未触达的体素自动淘汰）；② 后端 World Service 的近邻/占据查询（mock 避障、最近障碍距离、定位 mock 的可见点计数）。

```ts
class VoxelLRU {
  constructor(public res = 0.3, public capacity = 400_000, public perVoxel = 1) {}
  private map = new Map<number, Node>();           // key → 节点（Map 保持插入序，可当 LRU：delete+set 移到尾）
  key(x: number, y: number, z: number) {           // 与 iVox 同式；JS 用 32 位整数运算
    const i = Math.round(x / this.res), j = Math.round(y / this.res), k = Math.round(z / this.res);
    return (Math.imul(i, 73856093) ^ Math.imul(j, 471943) ^ Math.imul(k, 83492791)) >>> 0;   // 冲突时 Node 内再比 (i,j,k)
  }
  add(x: number, y: number, z: number, attr: number) {
    const h = this.key(x, y, z); let n = this.map.get(h);
    if (n) { this.map.delete(h); this.map.set(h, n); if (n.count < this.perVoxel) n.push(x, y, z, attr); return; }
    n = new Node(x, y, z, attr); this.map.set(h, n);
    if (this.map.size > this.capacity) this.map.delete(this.map.keys().next().value!);   // 淘汰最久未访问
  }
  // 渲染：每 N 帧把 dirty 体素写入预分配的 Float32Array/Int16Array 环形缓冲，drawRange 更新，避免重建 BufferGeometry
}
```

关键参数：实时层 `res=0.3 m`、`capacity=40 万体素`（约 40 万点，Web 端占总 point budget 的 ~20%，其余给离线八叉树）；后端查询 `res=0.5 m`、`NEARBY6` 近邻（LIO-Lite 定位模式所用）足够。注意 iVox 的 `Pos2Grid` 用 `round`（体素中心在整数格点），与 FAST-LIO/ikd-Tree 的 `floor+0.5` 不同，混用时必须统一。

### 3.6 关键帧 + 回环 + PGO（SimplePGO，无人机参数与 RTK 扩展）

```python
def on_odom(T_odom_body, cloud_body, t):
    if not is_key(T_odom_body): return                     # Δt > KF_TRANS or Δθ > KF_DEG
    kf = KeyFrame(T_local=T_odom_body, T_global=T_off @ T_odom_body,
                  cloud=voxel(cloud_body, KF_VOX), t=t)     # 改：存盘前降采样
    graph.add(Prior(0, kf.T_global, var=1e-12) if first else
              Between(i-1, i, T_prev_local⁻¹ @ T_odom_body, var=[1e-6]*3 + [ODO_T_VAR]*3))   # 改：xyz 同方差
    if rtk_fix(t): graph.add(GPSFactor(i, T_enu_map⁻¹ · p_rtk_enu − R_i·lever_arm, σ=σ_rtk))  # 新增：RTK 一元因子
    cand = radius_search(kf.T_global.t, LOOP_RADIUS, exclude=|Δt| < LOOP_DT)
    for c in cand[:3]:                                      # 改：多候选，按距离
        tgt = submap(c, ±LOOP_HALF, SUB_VOX); src = kf.cloud → global
        T, fit, inl = gicp(src, tgt, max_corr=LOOP_MAXCORR)
        if fit < LOOP_FIT and inl > 0.3:
            graph.add(Between(c, i, T_rel, var=fit)); break
    isam2.update(graph); if loop: isam2.update() * 4
    T_off = kf.T_global_opt @ kf.T_local⁻¹                  # 发布 map→odom
```

| 参数 | 原默认 | 无人机建议 | 理由 |
|---|---|---|---|
| `key_pose_delta_trans / deg` | 0.5 m / 10° | **1.5–2.0 m / 15°** | 5 m/s 巡航下原值每帧都是关键帧；内存 ∝ 关键帧数 |
| 关键帧点云 | 全分辨率 `body_cloud` | **0.2 m 体素后再存** | 默认 `lidar_filter_num=6` 时每帧约 3.3k 点 × 32 B，10 kf/s 约 1 MB/s，10 min 约 0.6 GB（`filter_num=3` 翻倍） |
| `loop_search_radius` | 1.0 m | **5–10 m**（≈ 飞行距离 × 1%–2%） | LIO 漂移大于 1 m 就永远搜不到 |
| `loop_time_tresh` | 60 s | 30–60 s | |
| `loop_submap_half_range` | 5 | 10–15 | 空中扫描稀疏，需要更多帧拼子图 |
| `submap_resolution` | 0.1 m | 0.25 m | 与雷达点距匹配，同时规避 VoxelGrid 溢出 |
| ICP `max_corr` | 10 m | 2–3 m（GICP） | |
| 里程计因子平移方差 | xy 1e-4，z 1e-6 | **xyz 均 1e-4** | 无人机高度漂移需要被回环修正 |

ENU 对齐（重力已对齐 → 4-DoF：yaw + t），用于 `coordinate.json` 的 `T_enu_map`：

```text
给定配对 (aᵢ = 关键帧天线位置 in map, bᵢ = RTK in ENU)，权重 wᵢ（固定解 1，浮点解 0.05）
μa = Σwᵢaᵢ/Σwᵢ，μb 同理；a'ᵢ = aᵢ − μa，b'ᵢ = bᵢ − μb
ψ = atan2( Σwᵢ(a'ₓb'ᵧ − a'ᵧb'ₓ), Σwᵢ(a'ₓb'ₓ + a'ᵧb'ᵧ) )
R = Rz(ψ)，t = μb − R μa；RMSE 写入 coordinate.json.qa
若 gravity_align=false（FAST_LIO 主仓/LIVO2 默认），改用 6-DoF Umeyama
```

### 3.7 BLAM / HBA：平面厚度 BA 与地图质量指标

- 原理：把所有关键帧点按 0.5 m 体素 + 自适应八叉（3 层）聚成平面片；一片平面上的点 `p = R_i l + t_i`（来自不同位姿 i），代价为该片点协方差最小特征值 `λ_min`（厚度²）；对位姿求解析梯度/Hessian，LM 求解；大规模时分层窗口（HBA）→ 位姿图。
- **我们用它的两处**：① 离线精化（V0.5a，直接调 `/hba/refine_map`）；② **地图 QA 指标**：`thickness = √λ_min` 在所有平面片上的 P50/P95（优化前后对比），写入 `localization/manifest.json.qa`，UI 用 lieflat 风格直方图展示“地图清晰度”；P95 > 0.10 m 的地图不允许发布为重定位地图。
- 注意 `hba_node` 的 `save_poses` 不写文件名、重复调用 `refine_map` 会在内存中追加（无 clear），我们的 ingest 脚本应自己按行号与 pgo 的 `poses.txt` 对齐。

### 3.8 MVP：定位 mock（无 ROS，后端 Python，前端可视化）

目的：在只有 UrbanScene3D + 虚拟 P600 的 MVP 中，把 V0.5 的“LIO + 重定位”语义提前做进 DroneState/UI，保证真机上线时只替换数据源。

```python
class MockLocalization:                       # 每架 UAV 一个；sim 100 Hz 调 step，1–2 Hz 调 reloc
    K_DRIFT   = 0.01     # 平移漂移 = 1% 飞行距离（MID-360 FAST-LIO 户外典型 0.3%–1%）
    YAW_DRIFT = 0.05     # deg / s 随机游走 σ
    RELOC_SIG = (0.05, 0.3)   # m, deg 成功时的测量噪声
    GRAIN     = 0.085         # m²，地图颗粒度基线（精级 0.3 m 体素下地图自配准均方 NN 距离；SF 实测，导入时按图计算）
    def step(self, T_map_body_true, v, dt, env):
        g = feature_gain(env, n_visible)            # 雾/沙尘 → 量程缩短 → 可见点少 → 漂移放大
        self.d_t += rand_unit_slow() * self.K_DRIFT * norm(v) * dt * g + N(0, (0.02*g)**2 * dt)
        self.d_yaw += N(0, (self.YAW_DRIFT * g)**2 * dt)
        self.T_odom_body = self.T_odom_map0 @ Drift(self.d_t, self.d_yaw) @ T_map_body_true
    def reloc(self, T_map_body_true, env, now):
        n = voxel_lru.count_in_fov(T_map_body_true, range=env.lidar_range(), fov=MID360_FOV(mount))
        p_ok = sigmoid((n - 1500) / 400) * in_map_coverage(T_map_body_true) * (1 - env.dropout)
        if rand() < p_ok:
            T_meas = noise(T_map_body_true, *self.RELOC_SIG) @ inv(self.T_odom_body)
            fit = self.GRAIN * (1.0 + 0.08 * (1 - p_ok) + abs(N(0, 0.03))); inl = 0.86 + 0.08 * p_ok   # 与 §3.2.4 实测分布同量级
            self.loc.accept(T_meas, fit, inl, now)      # 复用 §3.2.3 的门控/平滑/状态机
        self.loc.tick(now)
    def estimate(self): return self.loc.T_map_odom @ self.T_odom_body
```

WebSocket（与 r05 `lio.*` 通道并列，1–2 Hz 状态 + 10–20 Hz 位姿）：

```json
{"ch":"loc.P600-01.state","t":1759050000.12,"source":"LIO+MAP","status":"TRACKING",
 "map":{"id":"sf-demo","ver":"1.0.0"},"fitness":0.086,"fit_ratio":1.02,"inlier":0.93,"deg":0.18,
 "corr":{"dx":0.12,"dy":-0.05,"dz":0.01,"dyaw":0.3},"reset":2,"last_ok_ms":420,"n_scan":6400,"n_match":4100}
{"ch":"loc.P600-01.pose","t":1759050000.15,"est":[x,y,z,qx,qy,qz,qw],"odom":[...],"truth":[...]}
```

UI 落点：右侧 DRONES 卡片加“定位”行（shadcn `Badge`：TRACKING 中性灰、DEGRADED 红色描边、LOST 红色实底；morphicons 在状态间形变，禁止 emoji）；选中无人机后 3D 中同时画真值（白）、估计（灰）轨迹，重定位修正用短红色箭头闪现（transitions.dev 淡出）；“定位质量”面板用 lieflat 风格折线（fitness、内点率、修正量），DEGRADED/LOST 时段在底部 Timeline 上标红；“重定位”按钮 → shadcn `Dialog`，在 3D 地面拖拽设定位置 + 航向 → `POST /api/uavs/{id}/relocalize`。

### 3.9 存图产物 → World Package 转换（ingest 伪代码）

```python
def ingest_pgo_maps(maps_dir, world, session_id, hba_poses=None):
    rows = [l.split() for l in open(maps_dir/"poses.txt")]         # name x y z qw qx qy qz
    if hba_poses:                                                    # hba 只有 7 列，按行号对齐
        rows = [[r[0], *h] for r, h in zip(rows, map(str.split, open(hba_poses)))]
    kf_dir = world/f"reconstruction/lidar/{session_id}/keyframes"
    write_tum(kf_dir/"poses.tum", rows)                             # 统一 TUM：t x y z qx qy qz qw（注意四元数顺序变换）
    copy_patches(maps_dir/"patches", kf_dir/"patches")              # IMU(body) 系 XYZI
    cloud = concat(transform(load(p), T) for p, T in rows)           # 不用 map.pcd（未降采样、可能巨大）
    cloud = voxel(cloud, 0.2); cloud = statistical_outlier(cloud, k=15, std=1.5)  # LIO-Lite script/remove.py 同参
    T_enu_map, qa = align_4dof(rows, rtk_log)                        # §3.6
    for key, pts in split_tiles(cloud, S=64):                        # §3.4
        write_tile(world/f"localization/tiles/{key}.bin", pts)       # 头 + int16 量化（相对块原点，1 cm）
    write(world/"localization/global_rough.bin", voxel(cloud, 1.0))
    write_manifest(world/"localization/manifest.json", map_id, ver, qa, sha256s, algo_commit)
    write_coordinate(world/"coordinate.json", T_enu_map, qa)
    enqueue_octree_build(cloud → world/"geometry/pointcloud")        # Web 八叉树由 world 单元负责
```

### 3.10 PX4 外部视觉桥接（真机 V0.5b）

- LIO-Lite / Prometheus 做法：把 LIO 位姿**原样**发 `/mavros/vision_pose/pose`（Prometheus `uav_estimator.cpp::timercb_pub_vision_pose`，50 Hz，`location_source=10 (MID360)` 订阅 `/Odometry`）。风险：① 位姿是 IMU（body）系而不是飞控 FCU 系——若用 MID-360 内置 IMU 且雷达倾斜安装（Prometheus Gazebo 外参约 20°），必须乘静态 `T_body_fcu`；② 无协方差。
- 推荐：发 `mavros/odometry/out`（ROS1）或 XRCE-DDS `/fmu/in/vehicle_visual_odometry`（ROS2，`px4_msgs/VehicleOdometry`，NED/FRD），带位置/姿态方差（来自 IESKF P 与重定位状态）、`reset_counter`（map→odom 硬切时 +1）；PX4 端 `EKF2_EV_CTRL` 开水平/垂直位置与航向、`EKF2_EV_DELAY` 按实测（10 Hz 扫描 + 处理约 30–60 ms）、`EKF2_EV_NOISE_MD=0` 使用消息方差、`EKF2_EV_POS_X/Y/Z` 填 FCU→body 杆臂。
- 帧转换：ENU/FLU → NED/FRD：`p_ned = (y, x, −z)`，`q_ned_frd = q_(ENU→NED) ⊗ q_enu_flu ⊗ q_(FLU→FRD)`。
- **控制在 odom 系、规划在 map 系**：PX4/EGO-Planner 接收连续 odom 位姿；任务航点（map/ENU）经 `T_odom_map = T_map_odom⁻¹` 实时转换为 odom 系设定点，重定位修正不会让飞控看到跳变。

### 3.11 MID-360 可见性预算（本机实测，UrbanScene3D）

脚本 `.cache/research/r08/scan_budget.py`（每组 20 个随机位置，统计虚拟雷达 R 内地图点数中位数；“正装视场”按 −7°~+52°）：

| 城市（米制） | 离地 | R | 正装视场内 | 全向（等价倒装/多雷达上限） |
|---|---|---|---|---|
| San Francisco（740×717×55 m，9.4 点/m²） | 30 m | 40 m | 0 | 42 911 |
| | 30 m | 70 m | 0 | 126 915 |
| | 60 m | 40 / 70 m | 0 / 0 | 0 / 93 573 |
| | 100 m | 70 m | 0 | 0 |
| Shenzhen（1848×1999×391 m，1.35 点/m²） | 30 m | 40 / 70 m | 861 / 5 303 | 9 185 / 36 037 |
| | 60 m | 40 / 70 m | 0 / 0 | 699 / 17 536 |
| | 100 m | 70 m | 0 | 0 |

几何上限：地面可见需俯角 `θ ≥ asin(h/R)`。R=40 m：h=20 m 需 30°，h=30 m 需 48.6°；正装前倾 20° 的最大俯角 27° → **h ≤ 18 m**；倒装最大俯角 52° → **h ≤ 31.5 m**（且仅边缘一圈）。R=70 m（80% 反射率）时倒装 h ≤ 55 m。这组数也用于 mock：`count_in_fov` 直接决定 §3.8 的重定位成功率与漂移增益。

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块落点表

| 算法 / 实现 | 源（文件::符号） | 目标模块（01-design §42 目录） | 方式 | 版本 | MVP |
|---|---|---|---|---|---|
| iVox 体素哈希 + LRU | LIO-Lite `ivox3d/ivox3d.h::IVox::AddPoints/GetClosestPoint` | `apps/web` 实时扫描层；`world/voxel` 后端查询 | port（TS / numpy） | MVP | 是 |
| 2D 分块 + 邻域动态加载（修正卸载） | LIO-Lite `laser_mapping.cc::SplitMap/DynamicLoadMap` | `world/pointcloud` 定位瓦片；`apps/api` 瓦片服务；`apps/web` 实时层分块 | port | MVP（调度器）/ V0.5（定位瓦片） | 是 |
| map→odom 定位状态机 + 门控 + 平滑 | FASTLIO2_ROS2 `localizer_node.cpp::timerCB` + 本笔记 §3.2.3 | `simulation/estimators/localization.py`（mock）；`reconstruction/lidar/localizer`（真机） | port + 改进 | MVP（mock）/ V0.5b | 是 |
| 三级金字塔点到面 ICP + 偏航/平移多假设 | FASTLIO2_ROS2 `icp_localizer.cpp::align`、LIO-Lite `initialpose`（改进见 §3.2.3–3.2.4） | `reconstruction/registration`（small_gicp 实现） | port / adopt small_gicp | V0.5b | 否（原型已验证） |
| LIO 前端（MID-360） | FASTLIO2_ROS2 `fastlio2` | P600 机载（ROS2）/ 离线复算 | adopt（打补丁） | V0.5a | 否 |
| 关键帧 + 半径回环 + iSAM2 | FASTLIO2_ROS2 `pgo/simple_pgo.cpp` | `reconstruction/lidar/pgo` | adopt（改参数 + RTK 因子） | V0.5a | 否 |
| 存图格式（patches + poses） | FASTLIO2_ROS2 `pgo_node.cpp::saveMapsCB` | World Package `reconstruction/lidar/<session>/keyframes` | adopt 格式（与 r05 契约一致） | 契约 MVP 冻结 | 契约 |
| BLAM/HBA 精化 + 平面厚度 QA | FASTLIO2_ROS2 `hba/blam.cpp`、`hba.cpp` | `reconstruction/lidar/qa`、离线精化 | adopt（离线）+ port（QA 指标） | V0.5a | 否 |
| 4-DoF ENU 对齐 | 本笔记 §3.6 | `world/georef` | 自研 | V0.5a | 否 |
| PX4 EV 桥接 | LIO-Lite `PublishOdometry`、Prometheus `uav_estimator.cpp` | `simulation/px4`、P600 机载 | reference | V0.2（SITL 验证）/ V0.5b | 否 |
| 紧耦合先验图定位 | LIO-Lite `ObsModel_location` + `Run_location` | P600 机载可选模式 | reference | V0.6+ | 否 |
| 定位质量 UI | 本笔记 §3.8 | `apps/web`（shadcn + lieflat + morphicons） | 自研 | MVP | 是 |

### 4.2 P600“先建图后重定位”运行模式

#### 4.2.1 部署形态

| 选项 | 条件 | 做法 |
|---|---|---|
| 方案一：原厂栈快速验证 | Orin NX 为 JetPack 5 / Ubuntu 20.04，Prometheus V2（ROS1 Noetic）不动 | 建图：Prometheus 自带 `Modules/FAST_LIO` + 录 bag；重定位：LIO-Lite `run_location_online`（改 `livox_ros_driver2` 消息名），其 `/mavros/vision_pose/pose` 直接接 PX4。只做演示，不作为长期方案 |
| 方案二：目标架构（推荐） | JetPack 6 / Ubuntu 22.04 + ROS2 Humble（或 JetPack 5 上 `ros:humble` Docker，`--net=host`） | FASTLIO2_ROS2（fork 打补丁）+ `livox_ros_driver2`（ROS2）+ PX4 uXRCE-DDS；Prometheus 控制/规划如仍为 ROS1，用 `ros1_bridge` 只桥接 `odom`、`setpoint` 少量话题 |

#### 4.2.2 模式 A：建图会话（Mapping Session）

```text
地面准备：LI-Init / 厂商外参 → session.json；PTP/GPS 授时；RTK 基站；起降点登记
机载：livox_ros_driver2 → fastlio2/lio_node → pgo/pgo_node → rosbag2(mcap: lidar, imu, odom, rtk, 飞控日志, 吊舱视频时间戳)
航线规范（由 §3.11 推出）：
  · AGL ≤ 18 m（正装前倾 20°）/ ≤ 30 m（倒装）；城市峡谷沿街道飞、立面 ≤ 40 m
  · 速度 ≤ 5 m/s、转弯 ≤ 30°/s（去畸变与 ikd-Tree 更新余量）
  · 航线闭合：每 300–500 m 回到已飞区域一次（保证 loop_search_radius 5–10 m 内有回环）
  · 起飞前静止 ≥ 2 s（imu_init_num=20 @200 Hz 只需 0.1 s，但留余量给 bias）
降落后：ros2 service call /pgo/save_maps "{file_path: /data/sessions/<sid>/pgo, save_patches: true}"
离线（地面站/服务器，CPU）：
  1) 可选：用 bag 复算 LIO+PGO（参数固定、可复现）
  2) /hba/refine_map → /hba/save_poses → 行号对齐
  3) 4-DoF ENU 对齐（RTK）→ T_enu_map，RMSE < 0.10 m 通过
  4) 拼图 → 体素 0.2 m → 离群剔除 → 64 m 分块 → global_rough
  5) QA：回环数、平面厚度 P50/P95、覆盖率 → manifest.json
  6) 发布 World Package 新版本（map_id@semver，sha256），Web 八叉树并行构建
```

#### 4.2.3 模式 B：重定位会话（Localization Session）

```text
任务下发：Web 选择 world + map_id@ver → 机载拉取 localization/（tiles + manifest + global_rough）并校验 sha256
上电：lio_node + localizer_node(改进版) + tile_scheduler + px4 bridge
地面重定位：初值 = 起降点 / RTK+航向 / Web 拖拽 → relocalize → 连续 3 次 TRACKING → 允许解锁
飞行中：
  TRACKING  → 正常；map→odom 平滑修正；任务航点 map→odom 转换
  DEGRADED  → (3–10 s 无成功) 降速 ≤ 2 m/s，UI 告警，继续 LIO
  LOST      → (>10 s 或连续 5 次门控失败) 悬停 → 多假设重定位 30 s → 仍失败则 RTK 返航/原地降落
  OUT_OF_MAP → (离最近瓦片 > R_LOAD) 只用 LIO/RTK，禁止进入需地图的任务段
可选：定位同时增量建图（pgo 另存为新 session），用于地图更新（下一版本合并）
```

#### 4.2.4 状态机（mock 与真机共用）

```text
NO_MAP ──load ok──> WAIT_INIT ──guess──> ALIGNING ──fine ok ×3──> TRACKING
                                   ^          │fail×N                 │ Δt_ok>3s
                                   │          v                       v
                                   └──── LOST <── Δt_ok>10s ──── DEGRADED ──ok──> TRACKING
OUT_OF_MAP：任意状态下离瓦片覆盖 > R_LOAD 时进入，回到覆盖区后回 ALIGNING
```

### 4.3 World Package 对接

在 01-design §41 与 r05 §4.3 的 `reconstruction/lidar/<session>/` 基础上，新增**独立的 `localization/` 层**（与 Web 显示用 `geometry/pointcloud/` 分开：前者追求配准稳定与机载小体积，后者追求视觉效果与 LOD）：

```text
worlds/<world_id>/
├── coordinate.json            # + frames: map(gravity_aligned) → enu → wgs84；T_enu_map；method: rtk_4dof；rmse
├── localization/
│   ├── manifest.json          # 见下
│   ├── global_rough.bin       # 1.0 m 体素全图（多假设粗配、UI 小地图）
│   ├── tiles/index.json       # [{key:[i,j], bbox:[xmin,ymin,zmin,xmax,ymax,zmax], n, file, sha256}]
│   ├── tiles/{i}_{j}.bin      # 头(原点 f64×3, 比例 0.01, n) + int16×3 [+ uint8 intensity]
│   └── pads.json              # 起降点：[{id, p_map, yaw_deg, radius}]
└── reconstruction/lidar/<session_id>/   # r05 §4.3 契约；keyframes/ 直接来自 pgo save_maps
```

`manifest.json`：

```json
{"map_id":"sf-demo","version":"1.0.0","frame":"map","gravity_aligned":true,
 "tile":{"size_m":64,"voxel_m":0.2,"quant_m":0.01,"key":"floor(x/S),floor(y/S)"},
 "sources":[{"session":"2026-10-01T09-12-00Z","lio":"FASTLIO2_ROS2@7baa59f+r08patches","pgo":{"loops":14},"hba":{"iters":5}}],
 "qa":{"plane_thickness_p50_m":0.028,"plane_thickness_p95_m":0.071,"enu_rmse_m":0.06,"coverage_m2":182000},
 "sensor":{"lidar":"MID-360","mount":"inverted|tilt20","range_m":40},
 "sha256":{"global_rough.bin":"…","tiles/index.json":"…"}}
```

REST/WS（Gateway）：`GET /api/worlds/{id}/localization/manifest`、`GET /api/worlds/{id}/localization/tiles/{i}_{j}`（机载与 Web 共用）、`POST /api/uavs/{id}/relocalize {x,y,z,yaw_deg,frame:"enu"|"map"}`（Gateway 转 `map` 系、转弧度、**按 Z-X-Y 组合**或改用四元数后调 ROS2 srv）、WS `loc.{uav}.state / loc.{uav}.pose`（§3.8）。`DroneState` 增加 `localization{source,status,map_id,fitness,inlier,reset_counter}` 与 `pose_truth`（仿真）/`pose_est` 双位姿。

---

## 5. 对比与推荐

| 维度 | FASTLIO2_ROS2 | faster_lio_localization（LIO-Lite） |
|---|---|---|
| stars / 活跃度 | 761 / 2026-08 合并 PR | 212 / 代码 2024-01 后停更 |
| ROS | ROS2 Humble | ROS1 Melodic/Noetic |
| LIO 内核 | FAST-LIO2 重写：ikd-Tree、21 维 IESKF（Sophus，重力固定） | faster-lio：iVox、23 维 IKFoM（在线外参、S2 重力） |
| 输入 | 仅 `livox_ros_driver2` CustomMsg | Livox v1 CustomMsg / Velodyne / Ouster / Hesai |
| 回环 / PGO | 有：半径搜索 + ICP + iSAM2 | 无 |
| 一致性优化 | BLAM / HBA（离线服务） | 无 |
| 存图 | patches + poses.txt + map.pcd（服务触发，可重优化） | GlobalMap / FeatureMap / 分块（退出时写，无位姿） |
| 重定位 | 独立节点，两阶段 ICP，1 Hz 发 map→odom，服务接口 | 同节点，NDT→ICP 一次性初始化 + 冻结先验紧耦合 |
| 大图 | 整图加载（城市级 VoxelGrid 溢出） | **分块 + 3×3 动态加载**（有卸载 bug） |
| 无人机接口 | 无（需自己桥接 PX4） | 直接发 `/mavros/vision_pose/pose` |
| Jetson 性能 | 未给数据；OpenMP 2 线程；单线程 executor 阻塞风险 | README：NX 上 CPU 30%–40% |
| 代码质量 | 模块清晰、易读；多处并发/边界 bug | 单大类、宏开关、硬编码路径（`ROOT_DIR/maps`） |
| 与本项目契合 | 高（ROS2、服务化、存图契约、PGO/HBA） | 中（算法片段价值高，整体不宜部署） |

**推荐排序**：FASTLIO2_ROS2（adopt 为 V0.5 主干，fork + 补丁 + 无人机参数）＞ LIO-Lite（port iVox/分块/紧耦合思路；仅原厂 ROS1 栈上临时 adopt）。

单元外的横向建议（仅供总体选型，详见对应单元）：GLIM（koide3，2026-09 仍活跃，带 GNSS 因子与 GPU 可选，适合作为离线建图的对照/替代）；small_gicp（重定位与回环配准算子，Python 可用）；hdl_graph_slam（GPS 约束 PGO 的参考实现）。

---

## 6. 风险与注意事项

### 6.1 源码缺陷清单（adopt 前必须修）

| # | 仓库 · 位置 | 问题 | 影响 | 修复 |
|---|---|---|---|---|
| 1 | LIO-Lite `imu_processing.hpp::IMUInit` | 开启“重力定义水平面”后 `rot` 已把机体重力转到 −z，但 `init_state.grav` 仍设为**机体系**重力方向 | 静止时虚假加速度 `2g·sin(tilt/2)`：本机数值验证 5°→0.86、20°→3.41、30°→5.08 m/s²（`grav_check.py`）；雷达倾斜安装的 P600 起步漂移/发散 | `init_state.grav = S2(0,0,−G)`（与 FASTLIO2_ROS2 `initGravityDir(0,0,−1)` 一致，验证残差 1e-15） |
| 2 | LIO-Lite `::DynamicLoadMap` | 卸载只删 `hold_map_` 键，不删 iVox 中的点；回到该块会重复加点 | 内存增长、近邻被重复点污染 | §3.4 三种真删除方案之一 |
| 3 | LIO-Lite `::ObsModel_location` | `valid_corr` 失败时未把 `point_selected_surf_[i]` 置 false（建图版有），且 `resize(cur_pts,false)` 不重置旧元素 | 上一次迭代的无效点可能以旧残差进入 H | 显式置 false，`assign` 替代 `resize` |
| 4 | LIO-Lite `::initialpose_callback` | 初值 z 强制 0.2 m；NDT/ICP 对整幅全局图 | 空中重定位不可用；大图慢 | z 取地图地面高度 + AGL；只用邻域块 |
| 5 | FASTLIO2_ROS2 `lio_node.cpp::imuCB` | 加速度 `×10.0` 硬编码 | 用 Livox 内置 IMU 有 1.9% 比例误差（靠 ba 吸收）；**改用飞控 IMU（m/s²）时直接 ×10 → 发散** | 初始化时按 `9.81/‖acc_mean‖` 归一（同 LIO-Lite/FAST-LIO） |
| 6 | FASTLIO2_ROS2 `lidar_processor.cpp` 构造 | 世界点/法向/近邻/选择标志定长 10000 | 降采样后 > 1 万点（`lidar_filter_num=1` 且 `scan_resolution=0` 等）越界写 | 在 `process` 中按 `size` 动态 `resize` |
| 7 | FASTLIO2_ROS2 `localizer_node / pgo_node` | `std::lock_guard<std::mutex>(m);` 构造临时对象，立即析构 | 实际不加锁；单线程 executor 下无害，一改多线程即数据竞争 | `std::lock_guard<std::mutex> lk(m);` |
| 8 | FASTLIO2_ROS2 `icp_localizer.cpp` / `simple_pgo.cpp` | 整图 `pcl::VoxelGrid`：体素数 > 2³¹ 时 PCL 打印警告并**原样输出** | UrbanScene3D 6 城中 5 城在 0.1 m 溢出，Shenzhen/NY/Suzhou/Shanghai 在 0.5 m 仍溢出（`bounds.py`）→ ICP 用全分辨率大图，极慢 | 分块体素化（§3.4）或改用 small_gicp 的哈希体素 |
| 9 | FASTLIO2_ROS2 `localizer` | ICP 无最大对应距离、只看绝对 fitness（0.2/0.1 m²）；初值欧拉角 `Rz·Rx·Ry`（弧度） | 稀疏图上正确解 fitness 已达 0.085 m²，错误解 0.094–0.118 m² 也能通过 0.1 阈值（§3.2.4）；UI 按常见 ZYX 传参会得到错误初值 | §3.2.3 三级金字塔 + 相对门控 + 时间一致性；服务改收四元数 |
| 10 | FASTLIO2_ROS2 `pgo` | z 方差 1e-6、回环半径 1 m、关键帧 0.5 m、全分辨率 patch、队列只取队首 | 无人机上找不到回环、高度漂移修不掉、内存 GB 级 | §3.6 参数表 |
| 11 | FASTLIO2_ROS2 `hba` | `save_poses` 无文件名列；重复 `refine_map` 追加 | 产物无法直接回灌 | ingest 按行号对齐（§3.9），节点侧 `clear()` |
| 12 | 两仓 | 发布的是 IMU（body）位姿；LIO-Lite 协方差 `[rot,pos]` 顺序；FASTLIO2_ROS2 无协方差 | 接 PX4 时帧/方差错误 | §3.10 统一桥接 |

### 6.2 构建与平台

1. **本机无 ROS/GPU**：两仓都是纯 CPU 算法，但本机无法编译运行；验证路线 = `ros:humble` / `ros:noetic` Docker（不在宿主安装 ROS），bag 用 Python `rosbags` 转换。本笔记所有数值结论来自 numpy 复现。
2. **GTSAM on Jetson**：用 4.2 源码编译，`-DGTSAM_USE_SYSTEM_EIGEN=ON -DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF`，否则与 PCL/Eigen 对齐不一致导致运行时崩溃；Sophus 固定 1.22.10 + `SOPHUS_USE_BASIC_LOGGING`。
3. **LIO-Lite 的 TBB**：`std::execution::par_unseq` 在 GCC 下需要链接 TBB；附带的是 2018 版 tgz，Ubuntu 22.04 的 oneTBB 2021 与之 ABI 不同，需统一。
4. **硬编码路径**：LIO-Lite `ROOT_DIR` 编译期写死源码目录，`SplitMap` 会 `rm -rf maps/split_map/*`；部署时必须改为参数化输出目录。
5. **ROS2 单线程 executor**：`lio_node` 的点云转换在订阅回调里、主处理在 20 ms timer 里，重负载时 IMU 回调被阻塞 → 丢 IMU；改 `MultiThreadedExecutor` + 回调组，同时修 #7。

### 6.3 无人机场景特有风险

1. **视场/量程**（§3.11）：高空无点 → LIO 退化。安装方式（倒装 / 前倾）必须写进 `session.json.sensor.mount`，mock 雷达也按同一视场生成。
2. **螺旋桨/机臂自遮挡**：`lidar_min_range=0.5` 不够，需按 P600 机架加机体包围盒裁剪（否则桨叶点进地图成为“随机体素”）。
3. **退化场景**：开阔地、水面、单一立面、隧道 → 平移不可观；必须输出退化度量（§3.2.3 `deg`）并触发 DEGRADED。
4. **动态物体与季节变化**：车辆、行人、树叶；定位图建议剔除地面以上 0.3–2.5 m 的低置信点（或用多 session 一致性过滤），并记录建图日期，超过 N 个月提示更新。
5. **时间同步**：LiDAR（PTP/GPS 授时）、IMU、飞控 `timesync`、RTK 必须同一时基；`EKF2_EV_DELAY` 需实测。
6. **地图版本**：重定位强依赖“哪一版地图”，map_id@version + sha256 必须随任务下发并在遥测里回显，否则多机共图时各机坐标系不一致。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§6 缺少“LiDAR SLAM 后端”这一层**：原文只写“Open3D 承担 ICP/Registration”。Open3D 是算子库，不是 SLAM 后端。建议把 LiDAR 链明确为：`LIO（FAST-LIO2 系）→ 关键帧 → 回环 + RTK 因子 PGO → BLAM/HBA 精化 → ENU 对齐 → 地图发布`，由 FASTLIO2_ROS2（机载/在线）+ GLIM 或自研 GTSAM 脚本（离线）承担，Open3D/small_gicp 只作为配准与滤波算子。
2. **引入“会话（Session）”与“地图版本（Map Release）”两个一等概念**：建图会话产出可重优化的原始资产（keyframes、poses、QA），地图发布是经过 QA 的不可变版本（map_id@semver + sha256）。World Package 应能容纳多个 session、多个 release；重定位、多机共图、回放都引用 release。
3. **§7/§8 World Model 增加第三种表达：Localization World**。现有“Geometry World / Visual World”二分不够：定位图需要稳定结构、小体积、分块、体素 0.2 m、与显示用 LOD 八叉树不同。建议写成 `Geometry（碰撞/SDF）/ Visual（LOD 点云/3DGS）/ Localization（分块配准图 + 关键帧库 + 描述子）`，并在 §41 目录中增加 `localization/`（§4.3）。
4. **§7 Geographic 补坐标系链与 odom/map 语义**：`lidar → body(imu) → odom（每次上电新建，重力对齐）→ map（地图 release 的参考系）→ ENU → WGS84`，明确“控制在 odom、规划/多机/显示在 map/ENU”。当前文档只有 WGS84/UTM/ENU/RTK Origin，缺 odom/map 两层，会导致 V0.5/V0.6 的真机接入出错。
5. **§28 DroneState 增加定位字段与双位姿**：`localization{source: RTK|LIO|LIO+MAP|VIO|MOCK, status: NO_MAP|WAIT_INIT|ALIGNING|TRACKING|DEGRADED|LOST|OUT_OF_MAP, map_id, fitness, inlier, reset_counter, cov}`，仿真里同时推 `pose_truth` 与 `pose_est`。这是“环境 → 传感器 → 定位 → 控制”因果链可视化的基础，也是本平台区别于普通模拟器的卖点（r05 建议 6 的具体化）。
6. **§26–27/§35 补“真机定位接入 PX4”章节**：外部视觉（EV）融合、`EKF2_EV_*` 参数、ENU/NED 转换、`reset_counter`、EV 延迟；以及 Prometheus 现状（`location_source=10` 直接转发 `/Odometry` 到 `vision_pose`）与我们的差异。
7. **§2/§48 给 MID-360 一个可计算的能力边界**：`h_max = R·sin(θ_down,max)`（正装前倾 20°≈18 m、倒装≈31 m @40 m 量程），并据此写“建图航线规范”（§4.2.2）。原文“metric 级 World”的前提应改为“低空/峡谷 LiDAR 建图 + RTK 全域定位 + 视觉外观”。
8. **§29/§49 多机的前提是“共图重定位”**：多架 P600 各自上电的 odom 系不同，EGO-Swarm/编队需要共同参考系。V0.6 应显式包含“每机加载同一 map release → 各自 `T_map_odom_i` → 规划在 map 系”，而不是只写 Swarm/Formation。
9. **§44–48 版本拆分**：V0.5 拆为 V0.5a（建图会话：LIO+PGO+HBA+ENU 对齐+发布 release+QA）与 V0.5b（重定位会话：瓦片加载、状态机、PX4 EV、Web 定位面板）；MVP 阶段先冻结 `localization/` 与 `reconstruction/lidar/` 契约并用 §3.8 mock 跑通 UI。
10. **§36–37 频率表补定位层**：重定位 1–2 Hz、map→odom 平滑 50 Hz、定位状态 WS 1–2 Hz、瓦片调度 1 Hz / 2 s 预取；点云类大包（实时扫描、瓦片）走二进制通道，避免阻塞遥测。
11. **§33 Backend 技术栈补 ROS2 侧依赖与桥**：GTSAM、Sophus、small_gicp、`livox_ros_driver2`、uXRCE-DDS；ROS2 <-> Web 建议 foxglove ws-protocol / 自研 Gateway（二进制），不要用 rosbridge JSON 传点云。
12. **§43 MVP 增加“伪 V0.5”演示**：UrbanScene3D + 虚拟 P600 + MockLocalization（§3.8）+ 定位质量面板 + 重定位对话框；注意 UrbanScene3D 各城单位不一致（Chicago 包围盒约 4×8×1，疑似千米；其余为米），World Package `metadata.json` 必须带 `unit_scale` 并在导入时做建筑高度分布校验（与 r05 风险 7 一致）。
