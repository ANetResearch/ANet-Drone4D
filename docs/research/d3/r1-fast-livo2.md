# r1 研究笔记：FAST-LIVO2 深入研究与"沙盘模拟传感器 → FAST-LIVO2 → GLB 真值评测"闭环

> 研究单元：r1-fast-livo2 ｜ 对应：用户设计讨论 `design-r1.md` 第六节（"让 LiDAR 真正在沙盘内重建地图""验证完整 SLAM 必须提供有噪声的 IMU"）、建议模块 M25 Sensor Generation、M26 Agent Observation SDK（私有地图）、M27 Benchmark Runtime；阶段 P1（Mesh LiDAR + 私有地图）与 P3（批量 Benchmark）。
> 上游：hku-mars/FAST-LIVO2 `main@0d2c034`（2026-03-08，本次 `git fetch --depth 1 && reset --hard` 后即此提交，上游无更新；仓库只有 main 一个分支，ROS1 catkin）；依赖 xuankuzcr/rpg_vikit `6c886c8`（2025-03-24）、strasdat/Sophus `a621ff`（2012，非模板版）。D1 先行研究见 `docs/research/r05-fastlio-livo-r3live.md`（本笔记不重复其中的模块落点与 LIO 输出契约，只补源码细节、实测与 D3 闭环）。
> 实验机：dmax（64 核、1 TB 内存；本单元只用 CPU，未占 GPU）；容器一律 `d3-r1-fast-livo2-*`，`--cpuset-cpus` 4–16 核并加 `--memory` 限额；工作目录 `/data/anet-drone4d-d3/r1-fast-livo2/`；实验结束无残留容器。
> 镜像（保留）：`d3-r1-fast-livo2:noetic`（4.05 GB，FAST-LIVO2 可运行环境；r4 单元已借用）、`d3-r1-simgen:py311`（Python 3.11 + open3d 0.19 + rosbags 0.10.7 + trimesh，模拟器与评测）。
> 脚本：`.cache/research/d3/r1-fast-livo2/scripts/`；配置：`.cache/research/d3/r1-fast-livo2/configs/`；原始结果：`.cache/research/d3/r1-fast-livo2/results/`；Docker 构建上下文：`.cache/research/d3/r1-fast-livo2/docker/`。
> 本研究未修改项目代码与 `docs/research/d3/` 以外的文档。

---

## 0. 结论速览

| 问题 | 实测结论 | 证据 | 建议 |
|---|---|---|---|
| FAST-LIVO2 能否在 dmax 上稳定构建与运行 | 能。`ros:noetic-perception-focal` + Sophus a621ff（一行补丁）+ xuankuzcr/rpg_vikit，catkin 48 s、镜像 4.05 GB；上游无新提交（`0d2c034`），仅 ROS1 | §3.1 | **adopt（容器化、独立进程）**；GPL-2.0 不入库 |
| 真实无人机数据表现 | MARS-LVIG HKisland03（Avia@90 m，1.8 km）：对 RTK 的 ATE 1.02 m（0.056%），LIO 74 ms + VIO 14 ms/帧，3.7 核，峰值 12.7 GB；`filter_size_surf 0.5` 时 LIO −23%、CPU −40%、精度不变 | §3.3 | 作为 sim2real 回归基线；航测高度放大扫描降采样 |
| 模拟传感器 → bag → FAST-LIVO2 闭环 | 成立。GLB 小镇 883 m 环线：clean 档锚定漂移最大 0.46 m、ATE 0.14 m、RPE100 0.08%，地图到网格距离均值 9.6 cm、颜色 MAE 5.8；realistic 档 5 个种子（LIO）RPE100 0.25 ± 0.02%、ATE 0.60 ± 0.16 m、最大漂移 2.0 ± 0.5 m；harsh 档与 12 个动态目标均可跟踪 | §4.5 | **adopt** 为 D3 "SLAM-in-the-loop" 轨道与数据产品生成器 |
| 模拟器必须满足的输入假设 | **Livox CustomMsg 必须保留无回波点**（0,0,0）：丢弃后触发 FAST-LIVO2 级联"时间跳变修正"，漂移 ×3、加湍流 3/3 发散；修复后全部正常。另：点时间单调、IMU g 单位与起飞前静止、三路同一时基、driver2 类型名可直接用 | §2.10、§4.6 | 写进 M25 输出契约与合同测试 |
| 标定/外参/话题/频率 | 外参由 `rig.json` 自动生成 FAST-LIVO2 yaml（LiDAR→IMU、LiDAR→相机）；`/livox/lidar` 10 Hz×20 000 点、`/livox/imu` 200 Hz（g）、`/left_camera/image/compressed` 10 Hz；160 s bag 814 MB | §4.2–4.3 | 冻结为 M25 契约 |
| 能否在 dmax 批量运行 | 能。FAST-LIVO2 队列不丢数据，4× 倍速结果与 1× 一致；8 实例 × 2 核 × 4× 在 122 s 内跑完 8 × 159 s 航迹（16 核约 10× 实时，64 核外推约 40×）；每实例 ≤ 5.3 GB | §4.7 | M27 增 `slam_eval` 作业；SLAM/评测走 CPU 池 |
| 链路瓶颈 | CPU 光线投射相机 196–222 ms/帧（比实时慢约 2×；带 12 个动态目标 729 ms/帧、慢 7×）；LiDAR 36–75 ms/帧 | §4.7 | 相机改 GPU 光栅化/3DGS（r3），LiDAR 改 Warp（r4） |
| 与 Open3D/TSDF 的分工 | GT 位姿下 TSDF 表面精度 2 cm、F@0.2 0.85，换成 FAST-LIVO2 位姿后降到 0.23–0.33：瓶颈是定位不是表达；TSDF 0.15 m/80 m 曾吃到约 400 GB 内存 | §4.5、§4.8 | L0 真值位姿私有图（r4）+ L1 FAST-LIVO2 + L2 TSDF/3DGS 展示，同一评测口径 |

一句话结论：

1. **FAST-LIVO2 可以作为 D3 中"无人机自己估计位姿与地图"的真实感知链路**：用本研究的模拟器从 GLB 沙盘生成 MID-360 + 内置 IMU + 相机 bag，FAST-LIVO2 不改代码即可跑通，漂移水平（RPE100 0.08–0.29%）与它在真实无人机数据上的表现（0.06% 量级）同一数量级。它应以独立容器作业的形式消费每架无人机的私有传感器流，产出私有轨迹与彩色地图，**不进入仿真内核**。
2. **"跑真实 SLAM"本身就是传感器模拟的验收手段**：本研究的模拟器第一版丢弃了 MID-360 的无回波点，B1（已知位姿建图）毫无异常，只有 FAST-LIVO2 暴露出漂移放大与发散；类似的隐含假设（点序、单位、静止初始化、时基）应全部写进 M25 合同测试。
3. **基准必须报告分布**：同一 bag 的重复运行有 cm–dm 级差异，不同种子（IMU 上电偏置实现）使 ATE 在 0.34–0.74 m 间变化；推荐以 RPE100 为主指标（种子间 0.24–0.29%），ATE、最大锚定漂移与地图 F-score 给分布。
4. **算力不是问题，渲染才是**：FAST-LIVO2 每架约 1 核、≤ 5 GB，可 4 倍速批量；评测约 1 分钟/次；真正的瓶颈是 CPU 相机渲染，D3 应把传感器生成放到 GPU（r3、r4），SLAM 与评测留在 CPU。

---

## 1. 上游现状与依赖

| 项 | 现状（2026-10-11 核对） | 对 D3 的含义 |
|---|---|---|
| 仓库 | hku-mars/FAST-LIVO2，4729 stars / 866 forks / 325 open issues；`main@0d2c034`（2026-03-08，只改了数据集链接），代码自 2025-01 开源后基本稳定 | 可冻结在该提交；以容器形式长期使用 |
| 许可证 | **GPL-2.0**（商用需另谈） | 只能作为独立进程/容器"聚合"使用，不得把源码或链接库并入 Drone4D（ANet 修改版 Apache）；Drone4D 只交付 bag/配置生成与评测脚本 |
| ROS | 仅 ROS1 catkin（Noetic 已于 2025-05 EOL） | 用 `ros:noetic-perception-focal` 容器；ROS2 只有社区移植：Vulcan-YJX/FAST-LIVO2_ROS2（humble，60 stars，2025-02）、davidakhihiero/FAST-LIVO2-ROS2（humble）、U-AMC/FAST-LIVO2-ROS2（2026-05）、freshleesh/fast_livo2（jazzy + 重定位，2026-06）等，均非官方，D3 不依赖 |
| Sophus | 必须用非模板版 `a621ff`（`#include <sophus/se3.h>`，链接 `libSophus.so`） | GCC 9 下 `so2.cpp` 对 `std::complex::real()` 赋值编译失败，需打一行补丁并去掉 `-Werror`（见 §3.1） |
| rpg_vikit | 必须用 xuankuzcr 分叉（与 FAST-LIVO1 的不同）：相机模型 Pinhole(+radtan d0–d3) / EquidistantCamera(k1–k4) / ATAN / PolynomialCamera / Ocam(omni)，`camera_loader::loadFromRosNs("laserMapping")` 读参 | 模拟相机用 Pinhole、零畸变最省事；鱼眼可用 EquidistantCamera |
| livox_ros_driver | **不需要编译驱动**：仓库内置了 v1 `livox_ros_driver/CustomMsg` 的生成头文件（md5 `e4d6829bdfe657cb6c21a746c86b21a6`） | MID-360 真机走 `livox_ros_driver2`，其 CustomMsg 定义逐字相同、md5 相同；实测把 bag 中类型改名为 `livox_ros_driver2/CustomMsg` 后 FAST-LIVO2 照常订阅（ROS1 握手只比 md5），见 §4.6 |
| 配套 | FAST-Calib（LiDAR-相机外参一秒标定）、LIV_handhold（硬同步手持设备 STM32 方案）、Global-LVBA（离线 LiDAR-视觉 BA，数据集链接在其 README） | 仿真中外参与同步是"已知真值"，不需要标定；真机 P600 才需要 |
| 官方数据集 | FAST-LIVO2-Dataset 在 Google Drive（20 个 bag + calibration.yaml，0.38–21.4 GB）；整文件下载触发"Quota exceeded"，HTTP Range 分块可下 200 MB 左右后同样被限 | 不适合作为 CI 数据源；改用 HuggingFace 镜像上的 MARS-LVIG 无人机序列（§3.2） |

---

## 2. 源码精读（`src/` 约 5.9 k 行 C++）

### 2.1 数据流与线程

```
livox_pcl_cbk / standard_pcl_cbk ─┐  (Preprocess：转 PointXYZINormal，curvature = 点时间 ms)
imu_cbk ──────────────────────────┼─> 三个 deque（mtx_buffer）──> run(): 5 kHz 轮询 sync_packages()
img_cbk（cv_bridge → BGR8 Mat） ──┘                                   │
                                                                     ├─ processImu(): IMU 前向传播 + 逐点去畸变（Process2/UndistortPcl）
                                                                     └─ stateEstimationAndMapping():
                                                                          LIO：降采样 → VoxelMap ESIKF（点到面）→ 更新体素地图
                                                                          VIO：稀疏直接法光度 ESIKF（含曝光）→ 视觉地图点增删 → 着色发布
```

- 单线程主循环 + OpenMP（`MP_PROC_NUM` 编译期固定为 min(4, 核数)）只用于残差构建和光度雅可比；实测每实例平均 1.2–1.8 核（MID-360 模拟、1× 回放）、2.2–3.7 核（Avia 72 k 点/帧）。
- 回调里只做缓存；队列长度 200000，**不丢数据**，处理不过来时只会滞后——这使"倍速回放 + 排空等待"成为可行的批处理方式（§4.7）。

### 2.2 时间模型与同步要求（`LIVMapper::sync_packages`）

| 模式 | 触发条件 | 更新时刻 | 关键约束 |
|---|---|---|---|
| ONLY_LO（无 IMU） | 有 LiDAR 帧 | 帧末 = header.stamp + 最后一点 `offset_time` | 匀速模型 |
| ONLY_LIO（`img_en: 0`） | LiDAR 帧 + IMU 覆盖到帧末 | 帧末 | IMU 时间戳必须 ≥ 帧末才处理（等待 IMU） |
| LIVO | **以图像时刻切分 LiDAR 流**：把 offset 早于图像时刻的点放进本次 LIO，其余留到下一次；随后立即在同一时刻做 VIO | 图像曝光时刻 `img_stamp + img_time_offset + exposure_time_init` | 图像时刻必须落在已有 LiDAR 与 IMU 覆盖范围内；早于上次 LIO 时刻的图像丢弃；相邻图像 < 20 ms 丢弃（相机 ≤ 50 Hz）；第一帧 LiDAR 到达前的 IMU 与图像都丢弃 |

- 每点时间：Livox 用 `offset_time`(ns)/1e6 存进 `curvature`(ms)，**帧末时刻取最后一个点的 offset**（不排序），因此点必须按时间升序。
- IMU：`imu_time_offset` 从 IMU 时间戳中减去；`ros_driver_bug_fix` 会把 IMU 时间按与 LiDAR 的整秒差取整修正（老 Livox 驱动的整秒跳变 bug）。
- 相机：`img_time_offset` 为全局固定偏移（MARS-LVIG 各序列取 −0.1/0/+0.1 s，说明其数据的相机与 LiDAR 时基并未硬同步对齐）。FAST-LIVO2 **不在线估计相机时间偏移**，`exposure_time_init` 只是把曝光中点加到图像时刻上。作者的硬同步方案（LIV_handhold）用 STM32 同时触发相机与 LiDAR 的 PPS。
- **结论（对仿真）**：模拟器给出的三路时间戳必须同一时基、点时间严格单调、相机时刻落在 LiDAR 帧内；若要研究"未同步"的影响，用 `--imu-time-offset/--cam-time-offset` 显式注入，而不是依赖算法自带修正。

### 2.3 状态与 ESIKF

- 状态 19 维（`common_lib.h` `StatesGroup`）：R(3)、p(3)、**逆曝光时间 τ⁻¹(1)**、v(3)、b_g(3)、b_a(3)、g(3)，全部表示 IMU 系在"世界"中的状态；世界 = **首帧 IMU 系**（`IMU_init` 令 R = I，重力由前 `imu_int_frame` 个 IMU 样本均值估计，`gravity_align_en: true` 时才旋到 z 轴向下；与 FAST-LIO 一样，输出不是 ENU）。
- 传播（`UndistortPcl`）：中值积分，加速度按 `G_m_s2 / |mean_acc|` 归一化（所以 IMU 加速度单位可以是 g 也可以是 m/s²）；协方差含 b_g、b_a、g、τ⁻¹ 的过程噪声。**`b_acc_cov`/`b_gyr_cov` 两个 YAML 键在源码中并不读取**（`set_gyr_bias_cov/acc_bias_cov` 硬编码 1e-4），初始偏置协方差也硬编码 1e-5。
- LIO 量测（`VoxelMapManager::StateEstimation`）：点到面，残差方差 = 平面 6×6 不确定度投影 + 点协方差（`calcBodyCov`：测距方差 `dept_err²` + 两向角度方差 `sin²(beam_err)`）在法向上的投影；只观测 R、p 两块（H 为 N×6），迭代最多 `lio/max_iterations` 次，收敛判据 0.01° / 0.015 cm，"重匹配"两次后更新协方差。
- VIO 量测（`VIOManager::updateState`）：在 4 层金字塔（`patch_pyrimid_level`）上由粗到细，残差 `τ⁻¹_cur · I_cur(warp(p)) − τ⁻¹_ref · P_ref`，H 为 N×7（R、p、τ⁻¹），每像素量测方差 `img_point_cov`；误差不降即回退。LIO 与 VIO 是**同一时刻的两次顺序更新**（先 LIO 后 VIO），不是联合优化。

### 2.4 体素地图（`voxel_map.cpp`）

- 根体素 `lio/voxel_size`（哈希，int64 坐标），内部自适应八叉树最多 `max_layer` 层；点数超过 `layer_init_num` 时做 PCA，最小特征值 < `min_eigen_value` 判为平面，否则继续切分；平面点数到 `max_points_num` 后冻结（释放点、不再更新）。
- 每个点保存 `pointWithVar`（7 个 3×3 double 矩阵 + 3 个向量，约 430 B），因此内存随"被扫过的表面积"线性增长：MID-360 模拟 160 s、约 900 m 航线时进程 RSS 2.2–5.3 GB；Avia 真机 320 s 为 11.6–12.7 GB（含着色点云缓存）。
- `local_map/map_sliding_en` 只裁剪 LIO 体素地图；VIO 的视觉点哈希 `feat_map` **从不删除**，着色点云 `pcl_wait_save` 也一直累积到退出——长航时必须关 `pcd_save_en` 或分段运行。

### 2.5 视觉地图与光度更新（`vio.cpp`）

- 视觉地图点来自**有平面法向的 LiDAR 点**：按网格（`grid_size`，默认由 `grid_n_height=17` 推出，512 px 高时 30 px 一格）每格选 Shi-Tomasi 分数最高的点，存 8×8 参考块（`patch_size`）、观测位姿与当时的 τ⁻¹；视觉点放在 0.5 m 的哈希体素里。
- 取图：把当前 LiDAR 点投影成稀疏深度图，候选视觉点邻域深度差 > 0.5 m 视为遮挡跳过；参考块按"与其他观测光度差最小"选择；`normal_en` 时用平面法向做单应仿射 warp，否则用深度做仿射 warp；`ncc_en`（未开放到 YAML）与 `outlier_threshold` 剔除离群。
- `raycast_en` 时对没有地图点的网格沿视线 0.1–3 m 采样补点（适合近距离手持，无人机高空无意义）。
- **代码陷阱**：`updateReferencePatch` 与 raycast 里查 LIO 平面时把体素尺寸**硬编码为 0.5 m**，与 `lio/voxel_size` 不一致时（MARS-LVIG 配置为 2.0、本研究模拟配置为 1.0）法向细化基本失效；不影响主流程但会降低 VIO 精度。

### 2.6 曝光估计

- τ⁻¹ 作为状态量，VIO 雅可比第 7 列为当前像素值；参考块记录提取时的 τ⁻¹，因此光度残差对全局增益变化不敏感。传播噪声 `inv_expo_cov`（默认 0.1–0.2）。
- **着色不做曝光归一化**（`publish_frame_world` 中乘 τ⁻¹ 的代码被注释），地图颜色直接取当前帧像素：自动曝光起伏会原样变成地图上的明暗斑（模拟中 ±20% 增益时颜色误差见 §4.5）。

### 2.7 输入话题与消息格式

| 话题（默认名） | 类型 | 字段与单位 | 频率/规模 | 备注 |
|---|---|---|---|---|
| `/livox/lidar` | `livox_ros_driver/CustomMsg`（`lidar_type: 1`） | header.stamp = 帧首时刻；`timebase`(u64 ns)；`point_num`；每点 `offset_time`(u32 ns)、x/y/z(f32 m，**传感器系**)、`reflectivity`(u8)、`tag`(u8)、`line`(u8) | 10 Hz；MID-360 每帧 20 000 点（含无回波点） | 预处理只按 `line < scan_line` 过滤（MID-360 取 4），**不看 tag**；按 `blind` 去近点；按 `point_filter_num` 抽稀 |
| 同上（其他雷达） | `sensor_msgs/PointCloud2` | Velodyne(`time`)、Ouster(`t`)、Hesai XT32/Pandar128(`timestamp`)、Robosense(`timestamp` f64 绝对秒)、L515（无点时间） | — | `lidar_type` 2–7；Robosense 分支按时间排序，可作为"通用 PointCloud2 + 绝对时间戳"的接入口 |
| `/livox/imu` | `sensor_msgs/Imu` | 角速度 rad/s；加速度 g 或 m/s²（内部按均值模长归一化到 9.81） | MID-360 内置 200 Hz | 起始 `imu_int_frame` 个样本必须静止（初始化重力），无初始姿态输入 |
| `/left_camera/image` | `sensor_msgs/Image`（BGR8；`cv_bridge` 转换） | 内参在 `camera_*.yaml`，可带 `scale` 缩放（MARS-LVIG 2448×2048 用 0.25） | 10 Hz 典型 | 压缩图像需 `image_transport republish compressed→raw`（官方 launch 已带） |

### 2.8 参数与标定文件

- `config/<数据集>.yaml`（rosparam 根命名空间）+ `config/camera_*.yaml`（加载到 `laserMapping` 节点命名空间）。
- 外参约定：`extrin_calib/extrinsic_T, extrinsic_R` = **LiDAR → IMU**（p_I = R·p_L + T）；`Rcl, Pcl` = **LiDAR → 相机**（p_C = Rcl·p_L + Pcl）。相机-IMU 外参由二者推出（`Rci = Rcl·Rli`）。MID-360 内置 IMU 的 `extrinsic_T` 典型为 [−0.011, −0.02329, 0.04412]、R = I（与 `vehicles/p600/sensors/mid360.yaml` 的 `lidar_in_imu` 一致）。
- 关键参数（本研究模拟配置 / 官方 Avia 配置）：`preprocess.filter_size_surf` 0.3 / 0.1、`blind` 0.5 / 0.8、`scan_line` 4 / 6；`lio.voxel_size` 1.0 / 0.5、`max_layer` 2、`min_eigen_value` 0.0025、`dept_err` 0.02、`beam_err` 0.05；`imu.acc_cov` 0.5、`gyr_cov` 0.3、`imu_int_frame` 30；`vio.img_point_cov` 100、`patch_pyrimid_level` 4、`exposure_estimate_en` true。

### 2.9 输出

- 文件全部写到**编译期常量 `ROOT_DIR/Log/`（源码目录）**：`Log/result/<seq>.txt`（`evo/pose_output_en`，TUM：LIO 更新时刻、IMU 位姿）、`Log/pcd/lidar_poses.txt`、`Log/pcd/all_raw_points.pcd`（退出时保存；LIVO 为 XYZRGB，LIO 为 XYZI）及 LIVO 时另写的 `all_downsampled_points.pcd`（`filter_size_pcd` 体素）、`Log/Colmap/sparse/0/{cameras,images,points3D}.txt` + 去畸变图像（`colmap_output_en`，可直接作 3DGS 初始化）、`Log/mat_pre.txt`/`mat_out.txt`（每帧状态，诊断发散极有用）。容器中把宿主目录挂到 `/ws/src/FAST-LIVO2/Log`。
- 话题：`/cloud_registered`（LIVO 时为当前帧彩色点）、`/aft_mapped_to_init`、`/path`、`/mavros/vision_pose/pose`、`/LIVO2/imu_propagate`（`uav/imu_rate_odom`，4 ms 定时器 IMU 递推，给飞控用）。**所有发布消息的时间戳是 `ros::Time::now()`（墙钟），不是数据时刻**——离线评测只能用 `Log/result/*.txt`。
- 着色地图**只包含相机视场内的点**（`publish_frame_world` 只在 VIO 步把累积点投影到当前图像取色，出视场的点丢弃），因此 LIVO 地图的完整度天然低于 LIO 地图（§4.5 实测 0.68 vs 0.999@0.5 m）。

### 2.10 代码级陷阱清单（均已在实测中触发或核实）

1. **Livox "时间跳变修正"会被稀疏点触发**（`preprocess.cpp:183`）：相邻两点 `offset_time` 差 ≥ 1 ms 时把该点时间改为"前一点 + 0.0042 ms"，而且是**级联**的——一旦触发，本帧其后所有点的时间都被压扁。真实 Livox 驱动输出整帧点（无回波点为 0,0,0），相邻点只差几 µs，不会触发；但若模拟器/转换器**丢掉无回波点**，MID-360 倒装 30 m 时每帧约 7 处 ≥ 1 ms 的间隙，必然触发：实测去畸变与 LIO/VIO 切分出错，平稳飞行时最大漂移 1.38 m（修复后 0.46 m），加 0.3 m 湍流后 3/3 次失败（终点误差 68–78 m），再叠加 realistic 档的 IMU/LiDAR 噪声则整体发散到数百公里。FAST_LIO（FAST-LIO2）没有这段代码。**D3 的 Livox 输出必须保留无回波点**。
2. **PCL VoxelGrid int32 溢出**：范围/叶尺寸 的三轴乘积超过 2³¹ 时 PCL 打印 "Leaf size is too small… Integer indices would overflow" 并**原样返回输入**。HKisland03 的 90 m 航高单帧范围大，`filter_size_surf 0.1` 时 126 次迭代触发；退出时 `filter_size_pcd 0.15` 对整张地图必然溢出（`all_downsampled_points.pcd` 与 raw 字节数相同）。离线降采样改用 Open3D 哈希体素。
3. `b_acc_cov`/`b_gyr_cov` 未被读取；VIO 体素 0.5 m 硬编码（§2.5）；`publish/*` 时间戳为墙钟（§2.9）；`Log/` 位于源码目录（容器内需挂载）。
4. 世界系为首帧 IMU 系、不重力对齐；倒装 MID-360 + 内置 IMU 时世界系是"倒过来"的——与真值比较必须用初始真值位姿锚定（§4.4），不能直接当 ENU。
5. 内存只增不减（着色点、视觉点）；进程退出时才写 PCD，异常退出即丢图。

---

## 3. dmax 上的可运行环境与公开数据实测

### 3.1 镜像构建（`docker/livo/Dockerfile`）

- 基座 `ros:noetic-perception-focal`（自带 PCL 1.10、OpenCV 4.2、cv_bridge、image_transport、pcl_ros），apt 换 TUNA 镜像，追加 `ros-noetic-cmake-modules / tf / eigen-conversions / image-transport-plugins / compressed-image-transport`。
- Sophus `a621ff`：`sed` 把 `so2.cpp` 的 `unit_complex_.real() = 1.; unit_complex_.imag() = 0.;` 改为 `unit_complex_ = std::complex<double>(1.,0.);`，并去掉 CMake 的 `-Werror`，`make install`（12 s）。
- catkin 工作区 `/ws/src/{rpg_vikit, FAST-LIVO2}`，叠加 `extra/launch/run_bag.launch`（无 rviz、可选 compressed→raw 转发、`sim_time` 开关），`catkin_make -j16`：**48 s，峰值 RSS 1.8 GB，0 个 warning**；整镜像 4.05 GB，构建总耗时 75 s。
- dmax 上的坑：legacy builder（`DOCKER_BUILDKIT=0`）容器内 DNS 为 "invalid IP"，无法 apt；用 BuildKit 默认构建即可（BuildKit 不支持 `--cpuset-cpus`，并行度由 `-j` 控制）。

```bash
# dmax
cd /data/anet-drone4d-d3/r1-fast-livo2/docker/livo && docker build --progress plain --build-arg JOBS=16 -t d3-r1-fast-livo2:noetic .
# 回放（容器内脚本 scripts/run_livo_bag.sh：起 roslaunch、采样 RSS/CPU、rosbag play、等待处理排空、SIGINT 触发存图）
docker run --rm --name d3-r1-fast-livo2-hk --memory 32g --cpuset-cpus 40-47 \
  -v $PWD/datasets:/data:ro -v $PWD/configs:/cfg:ro -v $PWD/scripts:/scripts:ro \
  -v $PWD/runs/hk/Log:/ws/src/FAST-LIVO2/Log -v $PWD/runs/hk/out:/out \
  -e CFG=/cfg/HKisland03.yaml -e CAM=/ws/src/FAST-LIVO2/config/camera_MARS_LVIG.yaml \
  -e BAG=/data/HKisland_GNSS03.bag -e START=72 -e RATE=1 -e COMPRESSED=true \
  -e "TOPICS=/livox/lidar /livox/imu /left_camera/image/compressed" \
  d3-r1-fast-livo2:noetic bash /scripts/run_livo_bag.sh
```

### 3.2 数据获取

| 来源 | 结果 |
|---|---|
| FAST-LIVO2-Dataset（Google Drive，本机可访问、dmax 不可访问） | 整文件：Quota exceeded；Range 分块：Retail_Street 拿到 200 MB、Bright_Screen_Wall 16 MB 后被限，放弃 |
| NTU-VIRAL（NTU Data Repository） | 本机与 dmax 均超时 |
| **MARS-LVIG HKisland03（HuggingFace `swd123456/HKisland_GNSS03_Dataset_from_MARS-LVIG`，10.04 GB）** | dmax 经 `hf-mirror.com`：单连接约 2 MB/s；**12 路 HTTP Range 并行 204 s 下完（约 49 MB/s）**，脚本 `scripts/par_range_dl.sh`。本机→dmax 的 ZeroTier rsync 实测 2.3 MB/s，大文件不宜中转；ModelScope 未需要 |

HKisland03：DJI M300 搭载 Livox Avia（非重复扫描、三回波 72 000 点/帧、10 Hz）+ 2448×2048 全局快门相机（压缩图像 10 Hz）+ Avia 内置 IMU（约 209 Hz，单位 g）+ RTK（5 Hz）；390 s，起飞爬升到约 90 m 航高后绕岛飞行约 1.8 km，返航降落。FAST-LIVO2 自带此序列配置（`MARS_LVIG.yaml`：`img_time_offset −0.1`、`voxel_size 2.0`、`acc_cov 2.0`，表中注明从 72 s 悬停处开始播放）。

### 3.3 运行结果（`results/hkisland03_r1/`、`results/runs/hkisland03_fs05/`）

真值：从 bag 抽取 `/dji_osdk_ros/rtk_position`（NavSatFix）转本地 ENU（`scripts/extract_rtk.py`），按时间插值关联、Umeyama 刚体对齐（不估尺度）；RTK 天线到 IMU 的杆臂未补偿（数据集未给），误差中含 0.1–0.3 m 量级的杆臂与 RTK 噪声。

| 指标 | 官方配置（`filter_size_surf 0.1`） | `filter_size_surf 0.5` |
|---|---|---|
| 关联时长 / 航迹长度 | 301 s / 1801 m | 同左 |
| ATE RMSE（位置） | **1.02 m（0.056%）** | 1.01 m |
| ATE 最大 / 终点误差 | 1.66 m / 0.46 m | 1.66 m / 0.58 m |
| LIO 每次更新耗时 均值 / p95 / 最大 | 74 / 116 / 524 ms | 57 / 92 / 471 ms |
| VIO 每次更新耗时 均值 / p95 | 14 / 25 ms | 12 / 20 ms |
| 每帧点数（驱动输入 → 预处理输出）；每次 LIO 更新（去畸变后 → 降采样后） | 72 000 → 14 918；15 771 → 14 769 | 72 000 → 14 918；15 903 → 10 550 |
| 进程 CPU（总 CPU 秒 / 回放秒） | 1191 s / 320 s = 3.7 核 | 782 s / 349 s = 2.2 核 |
| 峰值 RSS | 12.7 GB | 11.6 GB |
| 地图（着色点，退出时写盘） | 4683 万点，PCD 749 MB | 4683 万点 |
| PCL 体素溢出告警次数 | 126 | 1 |

- LIO+VIO 合计约 88 ms/帧，已逼近 10 Hz 实时上限（p95 超过 100 ms），实时性依赖不丢数据的队列；`filter_size_surf` 放到 0.5 m 后 LIO 降 23%、CPU 降 40%，精度不变——**航测高度下 0.1 m 的扫描降采样是浪费**。
- 着色地图顶视图（对齐到 RTK ENU 后渲染，红线为 RTK 轨迹）：`results/hkisland03_r1/map_top.png`。
- 这次实测的意义：同一镜像、同一脚本、同一评测口径在真实无人机数据上给出 0.06% 量级的漂移，构成后面"仿真数据上跑出的数值是否可信"的参照系。

---

## 4. 闭环：沙盘模拟传感器 → ROS1 bag → FAST-LIVO2 → 彩色点云 → 与 GLB 真值比较

### 4.1 数据流

```
GLB（小镇 200 万三角形，r2 的 8.4 m/单位配准）──> World（ENU、任务区原点平移、Open3D RaycastingScene + 16 张 4096² 纹理）
                                     │
轨迹（多旋翼微分平坦：推力方向 = a − g；悬停 5 s 供 IMU 初始化 → 加速 → 1.1 圈环线 → 减速 → 悬停；
      地形跟随 30 m AGL / 楼顶净空 12 m；可叠加带限湍流）
                                     │
          ┌──────────────────────────┼──────────────────────────────┐
     MID-360（逐点时刻、逐点位姿）     内置 IMU 200 Hz（g 单位）        全局快门相机 10 Hz（逐像素求交 + 纹理）
          └────────────── sim.bag（/livox/lidar CustomMsg、/livox/imu、/left_camera/image/compressed）──────────────┘
                                     │                                    真值侧车（Agent 不可见）：
                                     │                                    gt_imu.tum、gt_cam.tum、gt_lidar_world.ply、
                                     │                                    frames/（GT 深度+RGB）、rig.json
                       FAST-LIVO2 容器（livo.yaml / camera.yaml 由 rig 自动生成）
                                     │
               Log/result/sim.txt（TUM）+ Log/pcd/all_raw_points.pcd（XYZRGB）
                                     │
            eval_sim.py：轨迹（锚定漂移、ATE、RPE）+ 地图（到 GLB 网格的距离、完整度、Chamfer、F-score、颜色）
            + 基线 B1 真值位姿 LiDAR 累积、B2/B3 Open3D TSDF（GT 位姿 / FAST-LIVO2 位姿）
```

### 4.2 传感器模型、外参与时间戳（`scripts/sim_livo_bag.py`）

| 项 | 取值 / 做法 | 来源与理由 |
|---|---|---|
| 机体系 | base_link FLU；IMU = MID-360 内置 IMU | 与 D2 `vehicles/p600` 一致；用内置 IMU 才与真机 FAST-LIO/LIVO 部署一致 |
| LiDAR 挂载 | `inverted_mapping` 预设：xyz [0,0,−0.08]、rpy [180,0,0] | `vehicles/p600/sensors/mid360.yaml` 的 `mount_presets`；倒装后视场 −52°~+7°，正下方有半角 38° 的盲锥（与 r4 结论一致） |
| LiDAR→IMU | R = I，t = [−0.011, −0.02329, 0.04412] | mid360.yaml `lidar_in_imu`；写入 `extrinsic_T/R` |
| 扫描花样 | D1/D2 已验收的 `python/awr/sim/sensors/lidar/pattern.py`（表驱动 MID-360，20 000 点/帧，点间隔 5 µs，line = i & 3） | 复用，零改动拷贝为 `scripts/mid360_pattern.py` |
| 逐点几何 | 每条射线按自身时刻插值机体位姿（1 kHz 轨迹 + Slerp），Open3D 求交；**输出点在该时刻的传感器系**（即真实运动畸变） | 让 FAST-LIVO2 的 IMU 去畸变有事可做；r4 实测 8 m/s 时忽略畸变误差中位 0.47 m |
| 量程/反射率 | ρ = 0.05 + 0.6·亮度（纹理），R_max = min(100, 40·(ρ/0.1)^0.269)，盲区 0.1 m | mid360.yaml `range` |
| 噪声 | 测距 σ 3 cm（<20 m）/ 2 cm；角度 σ 0.1°（扰动真实射线方向、报告名义方向） | mid360.yaml `noise` |
| **无回波点** | **输出 (0,0,0)、reflectivity 0、tag 0，保持整帧 20 000 点与 5 µs 等间隔** | 与 livox 驱动一致；丢弃会触发 FAST-LIVO2 时间修正缺陷（§2.10-1、§4.5） |
| IMU | 比力 f = Rᵀ(a_I − g)，a_I 含杆臂项；ω 由相邻姿态差分；噪声密度/偏置随机游走/上电偏置取 `vehicles/p600/sensors/imu.yaml`（陀螺 3.39e-4 rad/s/√Hz、上电 0.5°/s；加计 4e-3 m/s²/√Hz、上电 0.196 m/s²）；输出单位 g | 自检：无噪声时 5 s 纯惯导积分与真值差 3.7 mm / 0.003°（`check_imu_consistency.py`） |
| 相机 | 640×512、HFOV 80°（fx = 381.4）、零畸变、俯 45°，挂载 [0.12, 0, −0.08]；全局快门，时刻 = LiDAR 帧首 + 50 ms；纹理双线性采样（即摄影测量的烘焙光照）；可选自动曝光增益 1 ± A·sin(2πt/17 s) 与像素高斯噪声；JPEG q95 压缩 | 与倒装 LiDAR 视场在前下方重叠；FAST-LIVO2 用 `compressed:=true` 转 raw |
| 动态目标 | `--actors N`：行人盒 0.5×0.5×1.8 m（1.4 m/s）与车辆盒 4.5×1.8×1.5 m（8 m/s），沿航线附近两点往返，同时进入 LiDAR 与相机 | 检验事件实体在 LIVO 地图中的影响；放置高度取 DSM 顶面（会站上屋顶），正式版应走 r2 的 walkable/navmesh |
| 时间基 | 全部消息同一时基（Unix 1.76e9 s 起），可用 `--imu-time-offset/--cam-time-offset` 注入同步误差 | — |
| 自动配置 | 生成 `livo.yaml`（外参、`lidar_type 1`、`scan_line 4`、`blind 0.5`、`filter_size_surf 0.3`、`voxel_size 1.0`、`pose_output_en`、`pcd_save_en`）与 `camera.yaml` | Rcl/Pcl 由 T_C_L = T_C_B·T_B_L 推出 |

噪声档位（`--noise`，可 `--set key=value` 单项覆盖）：

| 档位 | IMU | LiDAR | 相机 | 运动 |
|---|---|---|---|---|
| clean | 噪声 ≈ 0、无偏置 | 2–3 mm、无角噪声 | 无噪声、恒定曝光 | 平滑 |
| realistic | imu.yaml 全套（含上电偏置） | 2–3 cm、0.1° | σ 2 DN、曝光 ±20% | 0.3 m 带限湍流 |
| harsh | 同上 + 97 Hz 桨叶振动 1.5 m/s² | 同上 | σ 4 DN、曝光 ±40% | 1.0 m 湍流（陀螺峰值 2.2 rad/s） |

### 4.3 话题、格式与频率（D3 传感器生成服务对 SLAM 的输出契约）

| 话题 | 类型 | 频率 | 单帧规模 | 160 s 体积 |
|---|---|---|---|---|
| `/livox/lidar` | `livox_ros_driver/CustomMsg`（与 driver2 md5 相同） | 10 Hz | 20 000 点 × 19 B = 380 KB（其中约 28% 有回波） | 约 600 MB |
| `/livox/imu` | `sensor_msgs/Imu`（加速度 g，orientation_covariance[0] = −1） | 200 Hz | 325 B | 10 MB |
| `/left_camera/image/compressed` | `sensor_msgs/CompressedImage`（jpeg） | 10 Hz | 约 130 KB | 200 MB |

ROS1 字节直接拼装（numpy 结构体 19 B/点），写 bag 用 `rosbags`（纯 Python，宿主机/任意容器均可，无需 ROS），写后自检反序列化首帧并断言 md5。整 bag 160 s 约 814 MB。MCAP/ROS2 只需换 `rosbags` 的 Writer。

### 4.4 真值与评测口径（`scripts/eval_sim.py`）

- **锚定**：FAST-LIVO2 世界 = 首帧 IMU 系。用真值在首个估计时刻的 IMU 位姿 T_W_I0 锚定：T_W_F = T_W_I0 · T_F_I0⁻¹。锚定后的误差就是 Agent 自己"以为"的位置与真实位置之差（纯里程计漂移），这是事件观测基准真正关心的量。另给 SE3 Umeyama 对齐后的 ATE（与文献可比）。
- **轨迹**：锚定漂移（终点、最大）、对齐 ATE RMSE、每 100 m 相对平移误差 RPE（在起点机体系比较，去掉航向漂移的影响）、终点姿态误差。
- **地图**（0.1 m 哈希体素降采样后）：精度 = 点到 GLB 网格的无符号距离（`RaycastingScene.compute_distance`）；完整度 = "可观测参考点"（真值位姿下 LiDAR 实际命中点，0.2 m 体素，约 50 万点，代表"这次飞行理论上能看到的表面"）到地图的最近距离 < τ 的比例；Chamfer = 两向均值和（另给 1 m 截断版）；F-score@τ（τ = 0.1/0.2/0.5 m）；颜色 = 地图点 RGB 与最近网格点纹理色的 MAE/PSNR（距网格 < 0.3 m 的点），并给单标量增益补偿后的 MAE（区分曝光与错位）。地图同时在"锚定"与"Umeyama 对齐"两种坐标下评测。
- **基线**：B1 真值位姿 + 同一 bag 的 LiDAR（"已知位姿建图"上限，也用于校验外参/时间约定）；B2 Open3D `ScalableTSDFVolume`（0.3 m 体素、4 体素截断、深度 ≤ 45 m）融合 GT 深度 + RGB、GT 相机位姿（每 4 帧 1 帧，398 帧）；B3 同 B2 但相机位姿取 FAST-LIVO2 估计（T_W_C = T_W_F·T_F_I(t)·T_I_C）。

### 4.5 实测结果（小镇 GLB，883 m 环线 / 159 s，6 m/s，地形跟随 30 m AGL；原始数据 `results/runs/*/summary.json`）

场景：s2c = clean；s5r = realistic 但无湍流；s1r = realistic（默认档，含 0.3 m 湍流）；s3r = harsh；s4r = realistic + 12 个动态目标（9 人 3 车）；s2 = clean 但**丢弃无回波点**（复现 §2.10-1 缺陷）。`livo` = 相机+LiDAR+IMU，`lio` = `img_en: 0`；`_b/_c` 为同一 bag 的重复运行，`_r2/_r4` 为 2/4 倍速回放。地图指标在"锚定"坐标下计算，"对齐后"列去掉了全局刚体漂移。

| 运行 | 漂移最大 m | 终点 m | ATE(对齐) m | RPE100 % | 地图点 | 精度均值/中位 m | 离群>0.5 m | 完整度@0.2/@0.5 | F@0.2 | 对齐后精度均值 m / F@0.2 | 颜色 MAE / PSNR dB | LIO / VIO ms | CPU 核 | RSS GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s2c_livo_a | 0.46 | 0.03 | 0.14 | 0.08 | 2.10 M | 0.10 / 0.08 | 0.0% | 0.48 / 0.68 | 0.63 | 0.04 / 0.74 | 5.8 / 27.8 | 34 / 9 | 1.72 | 2.5 |
| s2c_lio_a | 0.45 | 0.05 | 0.12 | 0.07 | 5.40 M | 0.16 / 0.12 | 0.0% | 0.60 / 1.00 | 0.61 | 0.04 / 0.99 | - | 36 / - | 1.32 | 2.2 |
| s5r_livo_a | 2.16 | 0.37 | 0.68 | 0.23 | 2.49 M | 0.41 / 0.27 | 27.7% | 0.23 / 0.50 | 0.29 | 0.21 / 0.51 | 22.3 / 18.5 | 41 / 7 | 1.83 | 5.3 |
| s5r_lio_a | 2.21 | 0.12 | 0.68 | 0.24 | 6.87 M | 0.35 / 0.23 | 23.3% | 0.51 / 0.82 | 0.49 | 0.20 / 0.71 | - | 43 / - | 1.53 | 5.1 |
| s1r_livo_a | 0.98 | 0.08 | 0.26 | 0.20 | 2.44 M | 0.34 / 0.22 | 30.0% | 0.27 / 0.50 | 0.34 | 0.10 / 0.67 | 18.8 / 19.9 | 34 / 9 | 1.51 | 5.2 |
| s1r_livo_b | 0.98 | 0.08 | 0.23 | 0.18 | 2.43 M | 0.34 / 0.22 | 29.4% | 0.27 / 0.50 | 0.35 | 0.10 / 0.69 | 18.6 / 20.0 | 33 / 7 | 1.47 | 5.2 |
| s1r_livo_c | 1.20 | 0.08 | 0.34 | 0.22 | 2.44 M | 0.36 / 0.24 | 32.0% | 0.26 / 0.48 | 0.33 | 0.12 / 0.64 | 19.2 / 19.7 | 34 / 7 | 1.49 | 5.2 |
| s1r_lio_a | 1.23 | 0.10 | 0.34 | 0.24 | 6.78 M | 0.39 / 0.27 | 33.4% | 0.45 / 0.71 | 0.44 | 0.12 / 0.84 | - | 34 / - | 1.17 | 5.0 |
| s1r_livo_r2 | 1.41 | 0.07 | 0.40 | 0.25 | 2.44 M | 0.34 / 0.23 | 30.3% | 0.27 / 0.50 | 0.34 | 0.14 / 0.60 | 18.9 / 19.8 | 30 / 7 | 2.63 | 5.2 |
| s1r_livo_r4 | 0.98 | 0.08 | 0.23 | 0.18 | 2.43 M | 0.34 / 0.22 | 29.3% | 0.27 / 0.50 | 0.35 | 0.10 / 0.69 | 18.6 / 20.0 | 26 / 7 | 4.42 | 5.3 |
| s3r_livo_a | 0.77 | 0.10 | 0.14 | 0.13 | 2.39 M | 0.26 / 0.17 | 18.8% | 0.35 / 0.68 | 0.43 | 0.07 / 0.80 | 28.8 / 17.0 | 42 / 7 | 1.82 | 5.1 |
| s3r_lio_a | 0.92 | 0.07 | 0.22 | 0.17 | 6.97 M | 0.30 / 0.19 | 25.8% | 0.53 / 0.79 | 0.52 | 0.09 / 0.92 | - | 45 / - | 1.56 | 4.9 |
| s4r_livo_a | 0.82 | 0.07 | 0.17 | 0.16 | 2.43 M | 0.27 / 0.19 | 20.5% | 0.30 / 0.57 | 0.38 | 0.08 / 0.72 | 17.5 / 20.8 | 42 / 7 | 1.84 | 5.2 |
| s4r_lio_a | 1.10 | 0.06 | 0.31 | 0.22 | 6.78 M | 0.38 / 0.26 | 34.0% | 0.47 / 0.70 | 0.46 | 0.11 / 0.86 | - | 43 / - | 1.53 | 5.0 |
| s2_livo_a（丢无回波点） | 1.38 | 0.04 | 0.42 | 0.34 | 2.44 M | 0.38 / 0.30 | 34.3% | 0.23 / 0.45 | 0.29 | - | 11.6 / 21.9 | 44 / 12 | 1.90 | 4.8 |
| s2_lio_a（丢无回波点） | 2.06 | 0.04 | 0.77 | 0.67 | 6.81 M | 0.50 / 0.37 | 41.2% | 0.40 / 0.65 | 0.37 | - | - | 50 / - | 1.56 | 4.6 |

基线（同一批 bag / 帧）：

| 基线 | 场景 | 点/面 | 精度均值 / 中位 m | 离群>0.5 m | 完整度@0.2 / @0.5 | F@0.2 | 颜色 MAE / PSNR | 构建耗时 |
|---|---|---|---|---|---|---|---|---|
| B1 真值位姿 LiDAR 累积 | clean | 531 万点 | 0.0009 / 0.0007 | 0 | 1.00 / 1.00 | 1.00 | - | 31 s（含解码） |
| B1 真值位姿 LiDAR 累积 | realistic | 671 万点 | 0.045 / 0.036 | 0 | 0.999 / 1.00 | 0.998 | - | 33 s |
| B1 真值位姿 LiDAR 累积 | +12 动态目标 | 671 万点 | 0.046 / 0.036 | **0.06%** | 0.999 / 1.00 | 0.997 | - | 34 s |
| B2 TSDF（GT 深度 + GT 位姿，0.3 m） | realistic（s1r 帧） | 120 万点 / 240 万三角形 | 0.021 / 0.006 | 0.5% | 0.75 / 0.81 | 0.85 | 13.6 / 23.5 | 398 帧 26.8 s（67 ms/帧） |
| B3 TSDF（GT 深度 + FAST-LIVO2 位姿） | realistic（s1r） | 120 万点 | 0.36 / 0.27 | 32% | 0.27 / 0.54 | 0.33 | 17.6 / 20.4 | 26.6 s |
| B2 / B3 | realistic 无湍流（s5r） | 115 / 118 万点 | 0.021 / 0.44 | 0.5% / 31% | 0.73 / 0.19（@0.2） | 0.83 / 0.23 | 13.8 / 22.8 | 22 s |

读数要点：

1. **闭环成立**：在 clean 档，FAST-LIVO2（LIVO）对 900 m 环线的锚定漂移最大 0.46 m（0.05%）、对齐 ATE 0.14 m、RPE 0.08%，彩色地图点到 GLB 网格的距离均值 9.6 cm、中位 7.7 cm，几乎无离群；地图颜色与纹理真值 MAE 5.8（PSNR 27.8 dB）。与真机 HKisland03 的 0.056% 量级一致——模拟出的数据"像真数据一样难"，而不是更容易或更难一个数量级。
2. **丢弃无回波点会把结果系统性做坏**（s2 vs s2c）：同一轨迹同一噪声，漂移最大 1.38→0.46 m、ATE 0.42→0.14 m、地图精度均值 0.38→0.10 m；叠加 0.3 m 湍流时 3/3 次失败（`ab_turb_only_lio*`，终点误差 68–78 m），再叠加 realistic 的 IMU/LiDAR 噪声时整体发散到数百公里（旧版 `s1_*` 运行）。定位到 `preprocess.cpp` 的级联时间修正后，模拟器改为输出整帧点，全部场景恢复正常（§2.10-1）。这类"模拟器与算法输入假设不一致"的缺陷，只有跑真实 SLAM 才会暴露。
3. **噪声档位与随机实现共同决定精度**：realistic 档 ATE 0.23–0.74 m（锚定漂移最大 0.8–2.4 m），比 clean 差 2–5 倍。表中 s1r（种子 1）恰好是 5 个种子里最好的一个；无湍流的 s5r（0.68 m）与种子 2–5（0.58–0.74 m）处于同一区间，说明差异主要来自 IMU 上电偏置等随机实现，而不是湍流本身（湍流的随机数在 IMU 噪声之前消耗，换档会改变偏置实现）。种子扫描见 §4.5.1。**基准必须固定并公开噪声档位与种子，且至少做多种子统计**。
4. **重复运行近乎确定但不逐位相同**：同一 realistic bag，a/b/r4 三次几乎一致（ATE 0.23–0.26 m），c 与 r2 偏离到 0.34–0.40 m；§4.7 的 8 次并行 4× 运行终点相差 < 8 cm（旧 clean bag 的三次 LIO 重复终点相差 < 3 cm）。原因是 ROS 回调到达时序与 OpenMP 归约顺序；对"同一事件、不同策略"的比较，SLAM 侧的随机性要用重复运行的分布而不是单次数值来表达。
5. **LIVO vs LIO**：噪声越大，视觉更新的收益越明显（harsh：ATE 0.14 vs 0.22；actors：0.17 vs 0.31；clean 两者相当）。但 LIVO 着色地图只含相机视场内的点，完整度@0.5 为 0.5–0.68，LIO 地图为 0.70–1.00；要"既有颜色又完整"，应在离线用 LIO 位姿 + 全部图像做多视着色（r05 §3.8 的 R3LIVE 式贝叶斯融合），而不是直接用 FAST-LIVO2 的 `all_raw_points.pcd`。
6. **颜色误差主要来自位姿与曝光**：clean 5.8 → realistic 18.6–22.3 → harsh 28.8；增益补偿后几乎不降（例如 s1r 18.8 → 18.6），说明主因是位姿误差导致的错位取色，而非全局曝光（FAST-LIVO2 曝光估计对定位有效，但着色未用它归一化）。
7. **动态目标**：12 个移动目标只占 LiDAR 回波的 0.06%（B1 离群率），对 LIO 无可测影响；但 FAST-LIVO2 不做动态剔除，鬼影会留在累积地图里。事件基准若要从私有地图中"读出"目标，需要在 Agent 侧另做动态分割，而不是指望 SLAM 地图。
8. **TSDF 基线说明分工**：给定真值深度与真值位姿，0.3 m TSDF 的表面精度（2 cm）与 F@0.2（0.85）远好于任何 SLAM 地图；一旦换成 FAST-LIVO2 的位姿（B3），F@0.2 掉到 0.23–0.33，与 FAST-LIVO2 自己的点云相当。**地图质量的瓶颈是定位，不是建图表达**。

#### 4.5.1 种子扫描（realistic 档、LIO 模式、同一航线，`seed{1..5}_realistic`）

| 种子 | 陀螺上电偏置 °/s (x, y, z) | 加计上电偏置 m/s² (x, y, z) | 漂移最大 m | 终点 m | ATE(对齐) m | RPE100 % | 地图精度均值 m（锚定 / 对齐） | F@0.2（锚定 / 对齐） |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.33, −0.26, −0.83 | −0.48, 0.02, −0.01 | 1.23 | 0.10 | 0.34 | 0.24 | 0.39 / 0.12 | 0.44 / 0.84 |
| 2 | −0.09, 0.43, −0.57 | −0.14, 0.24, −0.05 | 2.31 | 0.10 | 0.73 | 0.24 | 0.49 / 0.21 | 0.39 / 0.68 |
| 3 | 0.34, −1.42, 0.51 | 0.07, 0.07, −0.20 | 2.38 | 0.22 | 0.74 | 0.25 | 0.46 / 0.21 | 0.34 / 0.69 |
| 4 | 0.48, −0.49, −0.40 | 0.23, 0.09, 0.16 | 2.03 | 0.11 | 0.61 | 0.26 | 0.29 / 0.18 | 0.56 / 0.73 |
| 5 | −0.55, −0.68, 0.11 | −0.31, 0.09, 0.20 | 2.04 | 0.18 | 0.58 | 0.29 | 0.56 / 0.17 | 0.32 / 0.75 |
| 均值 ± 标准差 | | | 2.00 ± 0.46 | 0.14 | 0.60 ± 0.16 | 0.25 ± 0.02 | 0.44 / 0.18 | 0.41 / 0.74 |

- 终点误差都很小（0.1–0.2 m）而中途漂移达 2 m：航线最后 10% 回到起点区域，FAST-LIVO2 的体素地图未滑窗（`map_sliding_en: false`），回到老地图后点到面匹配把状态"拉回"，相当于一次隐式回环。因此**终点误差不能作为漂移指标**，应看最大锚定漂移与 RPE；这也意味着在"反复巡逻同一区域"的事件任务里，FAST-LIVO2 的私有地图会自动保持一致，而"一去不回"的追踪任务会累积漂移。
- RPE100 在 5 个种子间非常稳定（0.24–0.29%），适合作为"传感器/算法组合"的主指标；ATE 与地图 F-score 受偏置实现影响更大，报告时给分布。

### 4.6 与真机/驱动的兼容性

- **driver2 类型名**：`scripts/bag_retype.py` 把 bag 中的 `livox_ros_driver/CustomMsg` 改写成 `livox_ros_driver2/CustomMsg`（定义与 md5 相同），FAST-LIVO2 不改代码照常订阅（30 s 片段收到 300 帧、输出 297 个位姿）。因此 D3 模拟器输出 v1 类型名即可同时兼容 FAST-LIVO2 与真机 driver2 录包。
- **无回波点**：必须保留（§2.10-1）。代价是 LiDAR 话题体积约 ×3.5（每帧 380 KB），160 s bag 从 396 MB 增至 814 MB；若要省空间，宁可在写 bag 前**保留占位点但把坐标置零**（已是此做法），也不要删点。
- **MID-360 的高度窗口**：倒装 30 m AGL 时每帧有效回波约 5 700/20 000（28.6%）；r4 给出 45 m 时只剩约 3%、60 m 以上几乎为 0。FAST-LIVO2 在本场景（约 5.7 k 点/帧）能稳定跟踪；航测 90 m 量级应换 Avia/Mid-70 类前视长量程模型（HKisland03 即 Avia@90 m）。这需要写进 D3 的机型/任务说明，而不是让策略去"学会"。
- **初始化**：FAST-LIVO2 需要起飞前静止（本研究 5 s 悬停，`imu_int_frame: 30` 只用 0.15 s）；事件任务中无人机从机巢/地面起飞自然满足，若在空中出生（spawn in air）则必须给出静止窗口或改用 `gravity_align_en` + 外部初值。
- **模拟器自检（本研究踩过的第二个坑）**：首版湍流用线性斜坡淡入，位置的一阶导在起飞瞬间有折角，1 kHz 样条的二阶导因此出现尖峰（按折角估算为数十 m/s²），由此推出的推力方向与角速度也随之出现尖峰；FAST-LIVO2 的状态日志（`Log/mat_out.txt`）显示起飞瞬间横滚估计跳到 47°、速度跳到 1524 m/s，随即发散。改为 C2 连续的 smootherstep 后消失；生成器现在对真值断言 |ω| < 5 rad/s、|a| < 30 m/s²，并用 `check_imu_consistency.py` 做 5 s 纯惯导回代。M25 的轨迹/动力学输出也应带同样的连续性断言。
- **真机 P600**：仿真里外参/同步是真值；真机需要 FAST-Calib 标定 LiDAR-相机外参、内置 IMU 外参取 Livox 出厂值，相机若无硬同步需离线网格搜索 `img_time_offset`（r05 §6 第 5 条）。

### 4.7 批量运行能力（dmax 实测）

| 环节 | 实测 | 说明 |
|---|---|---|
| 场景装载 | GLB 解析 + 16 张 4096² 纹理解码 + Open3D 建 BVH：5–9 s，RSS 约 2–3 GB | 每个仿真进程一次 |
| LiDAR 生成（CPU，16 核） | 1591 帧 57–120 s（36–75 ms/帧，其中求交 3 ms、纹理反射率 8 ms，其余为逐点位姿插值与打包） | r4 的 Warp GPU 后端核函数 0.07 ms/帧，生产应切 GPU |
| 相机生成（CPU 光线投射，640×512） | 1589 帧 311–352 s（196–222 ms/帧：求交 24 ms + 纹理 120 ms + 噪声/JPEG/深度 PNG）；带 12 个动态目标时 1158 s（逐目标 AABB 与 DSM 查询未向量化） | **CPU 渲染是整条链路的瓶颈（比实时慢约 2–7 倍）**；应换 r3 的 GPU 光栅化（nvdiffrast）或 3DGS 渲染器 |
| 只生成 LiDAR+IMU（LIO 消融） | 68–80 s / 159 s 航迹 | 已快于实时 2 倍 |
| FAST-LIVO2（1×，4 核） | CPU 188–275 s / 159 s 航迹，RSS 2.2–5.3 GB；LIO 26–45 ms、VIO 7–12 ms 每次更新 | 1× 实时有大量余量 |
| FAST-LIVO2（4× 倍速，4 核） | 回放 40.9 s，结果与 1× 相当（ATE 0.23 m；1× 的 a/b 为 0.26/0.23 m） | 队列不丢数据，只需等待排空 |
| **8 实例并行，每实例 2 核、4× 倍速** | **8 × 159 s 航迹在 122 s 墙钟内全部完成**（每实例 CPU 约 123 s），8 次终点相互差 < 8 cm | 16 核约 10× 实时；外推到 dmax 64 核约 40× 实时（内存 ≤ 5.3 GB/实例） |
| 评测（无 TSDF / 含 TSDF） | 约 60 s / 约 110 s；峰值 RSS 4.6 GB（TSDF 0.3 m） | **首版 TSDF 用 0.15 m 体素、80 m 深度上限，约 1 km 航线时内存涨到约 400 GB，已人工中止**；评测容器一律加 `--memory` |

外推一个事件基准批次：3 架 × 4 种策略 × 5 个种子 × 300 s = 18 000 s 航迹。FAST-LIVO2 侧在 64 核上约 7–8 分钟；评测约 60 次 × 1 分钟可并行；而 CPU 相机渲染需要约 10 小时（16 核，约 18 万帧 × 0.2 s）——**结论是 SLAM 与评测都可以放心批量化，相机渲染必须上 GPU**。

### 4.8 与 Open3D / TSDF 等轻量重建的对比与分工

| 层 | 推荐实现 | 位姿来源 | 输出 | 时延与成本 | 用途 |
|---|---|---|---|---|---|
| L0 已知位姿建图（Agent 私有地图默认） | r4 的 GPU log-odds / 投影 TSDF（无 GPU 时 Open3D `VoxelBlockGrid`） | 仿真真值或"真值 + GNSS 噪声" | 占据/ESDF/表面 | 每 tick（ms 级） | Frontier、避障、策略输入；P1 私有地图 |
| L1 SLAM-in-the-loop（真实定位） | **FAST-LIVO2 容器**（LIO 模式更稳；LIVO 出彩色点） | 算法自估（IMU + LiDAR + 相机） | TUM 轨迹、彩色点云、体素平面、COLMAP 工程 | 1× 实时或离线 4×；约 1–2 核、2–5 GB / 架 | "Agent 不知道自己在哪"的基准轨道；传感器真实性回归；生成带真实漂移的数据集产品 |
| L2 稠密表面 / 展示 | Open3D TSDF（按瓦片、限深度 ≤ 45 m、体素 ≥ 0.3 m）；或用 FAST-LIVO2 的 COLMAP 导出训练 3DGS（D1 r03） | L0 真值或 L1 估计 | 网格 / 高斯 | 离线 | 浏览器展示、完整度评测、事件回放的"观测者视角世界" |
| 评测（共享） | `eval_sim.py`：Open3D `RaycastingScene.compute_distance` 到 GLB、可观测参考点完整度 | — | eval.json | 约 1 分钟 / 次 | 所有层统一口径 |

要点：TSDF 与 FAST-LIVO2 不是二选一。给定真值位姿，TSDF 的表面精度 2 cm、F@0.2 0.85，明显优于 SLAM 点云；但把位姿换成 FAST-LIVO2 估计后 F@0.2 掉到 0.23–0.33。也就是说，**L0 用真值位姿时衡量的是"建图与观测策略"，L1 才衡量"真实感知链路"**；基准两条轨道都要有，并在报告里明确区分（design-r1 第六节"验证 Mapping 与验证完整 SLAM 的区别"）。

---

## 5. 可复用实现（`.cache/research/d3/r1-fast-livo2/scripts/`）

| 文件 | 作用 | 可迁入 D3 的位置（建议） |
|---|---|---|
| `sim_livo_bag.py` | GLB → 轨迹（多旋翼微分平坦、地形跟随、湍流、悬停初始化）→ MID-360（逐点时刻/位姿、无回波占位、反射率量程、噪声）+ 内置 IMU（g 单位、杆臂、偏置/随机游走/振动）+ 全局快门相机（纹理求交、曝光/噪声、JPEG）+ 动态目标 → ROS1 bag + 真值侧车 + FAST-LIVO2 配置；`--noise/--set/--no-camera/--camera-from/--drop-noreturn/--*-time-offset` | M25 的 ROS1/Livox 导出器（打包与 rig→yaml 部分）；传感器物理换用 r4/r3 的 GPU 后端 |
| `glbworld.py` | GLB 解析（复制自 r2） | M22 |
| `mid360_pattern.py` | MID-360 花样（复制自 `python/awr/sim/sensors/lidar/pattern.py`，未改） | 已在 D2 中 |
| `eval_sim.py` | 锚定/对齐轨迹指标、地图到网格精度、可观测完整度、Chamfer/F-score、颜色、B1/B2/B3 基线 | M27 评测器 |
| `diag_traj.py`、`check_imu_consistency.py` | 误差分解（沿航迹/横向/垂直、最佳时间偏移）；IMU 与轨迹一致性自检 | M25 合同测试 |
| `run_livo_bag.sh`、`run_sim_livo.sh` | 容器内回放 + 资源采样 + 排空检测 + 存图；宿主机按 cpuset/内存限额并行调度，`cfg_patch` 做参数扫描 | M27 作业执行器 |
| `analyze_real.py`、`extract_rtk.py` | 真机 bag 对 RTK 的 ATE、分阶段耗时、地图顶视图 | 真机回归 |
| `bag_retype.py` | Livox 消息类型改名（driver ↔ driver2） | 工具 |
| `par_range_dl.sh`、`gdrive_range_dl.sh` | 并行 Range 下载（hf-mirror 49 MB/s） | 运维 |
| `collect_run.py`、`make_tables.py` | 汇总与出表 | — |
| `docker/livo/`、`docker/simgen/` | 两个镜像的构建上下文（含 Sophus 补丁、无 rviz launch） | 部署 |

---

## 6. 对 D3 架构的建议

1. **把 FAST-LIVO2 定位为"L1 SLAM-in-the-loop 估计器 + 离线数据产品生成器"，不进仿真内核。** 它是 GPL-2.0、ROS1、单机进程，适合以容器作业方式消费 M25 产出的每机传感器流（离线 bag 为主，必要时在线 ROS 桥），产出每机私有的轨迹与彩色地图；Agent 策略通过 M26 只看到这些"自己估计的"量。
2. **M25 Sensor Generation 输出契约冻结为 §4.3 三话题 + `rig.json` + 真值侧车**，并附合同测试：CustomMsg md5、整帧 20 000 点且 `offset_time` 单调、无回波占位为 0、IMU 单位 g 且起飞前静止窗口 ≥ 0.5 s、三路同一时基、相机时刻落在 LiDAR 覆盖内、`check_imu_consistency` 5 s 纯惯导误差 < 5 cm。`rig.json` 自动生成 FAST-LIVO2（及 FAST-LIO2）yaml，避免人工抄外参。
3. **M27 Benchmark Runtime 增加 `slam_eval` 作业类型**：输入（世界版本、事件 tape、机队、策略、噪声档位、种子）→ 生成 bag → N 次 FAST-LIVO2（4× 倍速、2 核、≤ 6 GB）→ `eval_sim` → `eval.json`。默认报告 RPE100、最大锚定漂移、对齐 ATE、地图 F@0.2（锚定与对齐两种）、颜色 MAE，并以"种子 × 重复运行"的分布呈现；`event_truth` 与真值侧车只对评测器可见。
4. **事件与动态实体**：事件 tape 中的人/车在 M25 每帧注入求交场景（本研究用 AABB，生产用 r4 的实例化网格），同时进 LiDAR 与相机；FAST-LIVO2 不剔除动态点，"事件是否被看到"应由像素/点级可见性真值（M27 的 Event Capture）判定，而不是从 SLAM 地图里找。
5. **坐标锚定**：基准评测用真值初始位姿锚定（本研究口径）；Agent 自身若需要世界坐标，用起飞点 GNSS + 罗盘给 T_W_F（4-DoF），并在 `session.json` 写明 `gravity_aligned`（r05 §4.3 契约照用）。
6. **噪声档位作为一等公民**：clean / realistic / harsh 三档（§4.2）连同种子写进 run manifest；真机数据（MARS-LVIG、自采 P600）用同一镜像与评测脚本定期回归，用"有效特征数、每帧耗时、RPE"对齐仿真与真机的难度（sim2real 校准），避免模拟数据比真实数据系统性地更容易。
7. **计算资源规划**：SLAM 与评测走 CPU 池（每 64 核约 30 实例），传感器生成走 GPU 池（LiDAR 用 r4 Warp、相机用 r3 光栅化/3DGS）；所有容器带 `--memory`，TSDF 必须分瓦片、限深度。
8. **MID-360 任务说明**：建图类任务 AGL ≤ 30–40 m 且倒装；高空观察任务不要指望 MID-360 建图（可只给相机或换长量程雷达模型）。

---

## 7. 风险与注意事项

1. **许可证**：FAST-LIVO2 为 GPL-2.0，只能独立分发容器、通过文件/ROS 消息交互；Drone4D 仓库内不得包含其源码或链接产物（Dockerfile 只在构建时拉取）。
2. **ROS1 EOL 与依赖链**：Noetic、非模板 Sophus（需补丁）、vikit 分叉都已停滞；镜像应长期保存（已保留 `d3-r1-fast-livo2:noetic`）并在 D3 制品库存档。ROS2 移植均为社区版，切换前需用本闭环回归。
3. **模拟与算法的隐含假设**：本研究已踩到"丢无回波点 → 时间修正级联"这一类问题；其他潜在差异（Livox `tag` 语义、多回波、点序、IMU 单位、相机滚动快门与运动模糊、HDR/自动曝光曲线）都可能让结果"好得不真实"或"差得不真实"。合同测试 + 真机回归是唯一的防线。
4. **场景真实度**：小镇网格中位边长约 3 m（8.4 m/单位，r2 配准），屋檐、树冠、电线等细结构缺失，纹理为烘焙光照（无昼夜/阴影变化）；这让 VIO 比真实环境容易、LIO 在大平面上更容易退化。尺度 8.4 m/单位是 r2 的估计值（置信度 C），改尺度后需要重跑。
5. **非确定性**：同一 bag 重复运行有 cm–dm 级差异（realistic 档 ATE 0.23–0.40 m），对比策略时必须用分布与显著性，而非单次数值。
6. **内存与崩溃**：FAST-LIVO2 的着色点云与视觉点只增不减、只在正常退出时写盘；长航时（> 10 min）需关 `pcd_save_en`、改为周期存段或只用轨迹，并在作业层捕获异常退出。
7. **评测参考集**：完整度以"真值位姿下 LiDAR 实际命中的表面"为参考，衡量的是"这次飞行可观测部分被重建了多少"，不是整座城镇；跨航线比较时需改用统一的感兴趣区域（ROI）参考集。
8. **数据源可用性**：FAST-LIVO2 官方数据集（Google Drive）配额受限、NTU-VIRAL 仓库超时；HuggingFace 镜像上的 MARS-LVIG 序列为第三方上传（README 指向原始 Google Drive），作为回归数据需在 D3 存储内保留副本（dmax `/data/anet-drone4d-d3/r1-fast-livo2/datasets/HKisland_GNSS03.bag`，10.04 GB）。

---

## 附录 A：dmax 上的目录与镜像

- 工作目录：`/data/anet-drone4d-d3/r1-fast-livo2/`（共 47 GB：`datasets/` 9.4 GiB、`sim/` 各场景 bag 与帧 11 GB、`runs/` 各次运行输出含 PCD 27 GB）。本机 `.cache/research/d3/r1-fast-livo2/results/` 只保留小文件（各运行 summary/eval/轨迹/资源采样、场景 rig 与日志、样例图，22 MB）。
- 镜像（保留）：`d3-r1-fast-livo2:noetic`（4.05 GB）、`d3-r1-simgen:py311`；基础镜像 `ros:noetic-perception-focal` 已拉取。
- 容器：全部以 `--rm` 或实验后删除，无残留。

## 附录 B：关键命令

```bash
# 生成场景（dmax，16 核，~8 min 含相机；LiDAR-only ~70 s）
docker run --rm --name d3-r1-fast-livo2-sim --memory 32g --cpuset-cpus 48-63 \
  -v /data/anet-drone4d-d3/data:/glb:ro -v $PWD/scripts:/scripts:ro -v $PWD/sim:/sim d3-r1-simgen:py311 \
  python /scripts/sim_livo_bag.py --glb /glb/an_entire_small_town_scanned_by_a_drone.glb \
    --out /sim/s1r_realistic --noise realistic --seed 1 --save-depth-every 4
# 跑 FAST-LIVO2（LIVO；LIO 加 "common/img_en=0"；倍速第 3 参数）
scripts/run_sim_livo.sh s1r_realistic s1r_livo_a 1 16-19
scripts/run_sim_livo.sh s1r_realistic s1r_lio_a 4 28-31 "common/img_en=0"
# 评测（含 TSDF 基线）
docker run --rm --name d3-r1-fast-livo2-eval --memory 48g --cpuset-cpus 16-31 -e VOX_TSDF=0.3 -e DEPTH_TRUNC=45 \
  -v /data/anet-drone4d-d3/data:/glb:ro -v $PWD/scripts:/scripts:ro -v $PWD/sim:/sim:ro -v $PWD/runs:/runs d3-r1-simgen:py311 \
  python /scripts/eval_sim.py /sim/s1r_realistic /runs/s1r_livo_a/out /glb/an_entire_small_town_scanned_by_a_drone.glb /runs/s1r_livo_a/eval.json
```
