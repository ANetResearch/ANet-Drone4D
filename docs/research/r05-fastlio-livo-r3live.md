# r05 研究笔记：FAST-LIO2 / FAST-LIVO2 / R3LIVE（LIO 建图与着色）

> 研究单元：r05 ｜ 对应设计文档：01-design.md §2、§6、§7–8、§15、§23、§41、§48（V0.5 Real World Fusion）
> 源码位置：`/data/projs/anet-drone/refs/lidar/{FAST_LIO, FAST-LIVO2, r3live}`（另交叉参考 `refs/sim/Prometheus/Modules/FAST_LIO`、`refs/lidar/FASTLIO2_ROS2`、`refs/lidar/livox_ros_driver2`）
> 注：`FAST_LIO/include/ikd-Tree` 为未拉取的 git submodule（空目录），ikd-Tree 源码以 Prometheus 内置副本 `refs/sim/Prometheus/Modules/FAST_LIO/include/ikd-Tree/ikd_Tree.{h,cpp}`（fast_lio 分支）为准。

---

## 0. 结论速览

| 仓库 | stars / 最后提交 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|---|
| hku-mars/FAST_LIO（FAST-LIO2） | 5225 / 2024-07-23 | LiDAR-IMU 紧耦合 IEKF + ikd-Tree 增量地图，MID-360 事实标准；P600 的 Prometheus 栈已内置 | **adopt**（真机/离线直接跑，ROS1 Docker 或其 ROS2 移植 FASTLIO2_ROS2）＋ **port**（ikd-Tree 降采样规则、局部地图立方体、观测性度量） | MVP 移植若干算法；V0.5 作为 LIO 主干 | 5/5 |
| hku-mars/FAST-LIVO2 | 4689 / 2026-03-08 | LiDAR-IMU-Visual 直接法紧耦合（体素平面图 + 稀疏直接 VIO），带 RGB 着色与 COLMAP 导出 | **reference**（V0.5 离线着色可条件性 adopt：前提是相机与 LiDAR 硬同步 + 标定） | V0.5 离线着色 / V1.0 3DGS 输入 | 4/5 |
| hku-mars/r3live | 2460 / 2022-08-25 | LIO + VIO，RGB 地图贝叶斯颜色融合、离线网格重建 | **port**（仅移植 RGB 点颜色融合、z-buffer 选择、分块发布思路；不部署） | V0.5 离线着色模块 | 3/5 |

一句话结论：
1. **LIO 在本项目中的正确定位不是"建出整个世界"，而是"给世界提供 metric 骨架"**：高频可信的轨迹（控制与尺度锚点）+ 近场高精度几何。MID-360 标称 40 m@10% 反射率，P600 在 80–120 m 航高下几乎打不到地面，城市级外观/几何仍需视觉重建（LingBot-Map），LIO 轨迹用于给视觉重建定尺度、定 ENU。
2. MVP（纯 Web + mock）阶段不跑任何 ROS，但以下算法可直接移植到 Python/TS：ikd-Tree 的"体素中心最近点"降采样规则（八叉树 LOD 采样 + 实时建图去重）、FAST-LIO 的局部地图立方体迟滞平移（资源驻留集管理）、`calcBodyCov` 激光噪声模型（环境对 LiDAR 退化）、R3LIVE 的二级哈希 + 活跃体素集（实时建图增量推流）、基于法向的平移可观性矩阵（mock LIO 漂移与退化）。
3. V0.5 需要冻结一个 **LIO 输出契约**（关键帧 patch + 位姿 + 协方差 + QA），而不是 FAST-LIO 默认的单个 `scans.pcd`。

---

## 1. 仓库概览

### 1.1 FAST_LIO（FAST-LIO2）

- 论文：FAST-LIO2: Fast Direct LiDAR-inertial Odometry（T-RO 2022），`doc/Fast_LIO_2.pdf`。
- 构建：catkin（ROS1 Melodic/Noetic），C++14，依赖 PCL ≥1.8、Eigen ≥3.3.4、`livox_ros_driver`（v1 消息）、PythonLibs（matplotlibcpp 画时间日志）。`CMakeLists.txt` 在 x86 且核数>4 时开 `MP_EN`、`MP_PROC_NUM=3`（OpenMP 近邻搜索并行）。
- 入口：`src/laserMapping.cpp::main`，单文件单节点 `fastlio_mapping`；支持 Livox（AVIA=1，含 MID-360）、Velodyne、Ouster、MARSIM（最后一次提交即"Support MARSIM simulator"——一个基于点云地图渲染 LiDAR 的无人机模拟器，对我们"用 UrbanScene3D 模拟 LiDAR"很有参考价值）。
- 主分支是 ROS1；ROS2 在 `refs/lidar/FASTLIO2_ROS2`（liangheming，2026-08 仍活跃，带 PGO 回环 / 重定位 / HBA，原生 `livox_ros_driver2`）。

### 1.2 FAST-LIVO2

- 论文：FAST-LIVO2（T-RO 2024），另有资源受限平台版本与 FAST-Calib（LiDAR-相机外参一秒标定）。代码 2025-01 开源，2026-03 仍在更新。
- 构建：catkin（README 写 Ubuntu 18.04–20.04），C++17，`-O3 -march=native`（ARM 下 `-mcpu=native`，对 Jetson 友好）；依赖 PCL、OpenCV、**Sophus（非模板版，需特定 commit）**、**rpg_vikit（作者 fork，相机模型）**、可选 mimalloc。许可 GPLv2（本项目科研用途，忽略）。
- 入口：`src/main.cpp` → `LIVMapper::run()`；三个库：`vio`（`vio.cpp frame.cpp visual_point.cpp`）、`lio`（`voxel_map.cpp`）、`pre`（`preprocess.cpp`）。
- 运行模式由 `img_en/lidar_en/imu_en` 决定：`LIVO / ONLY_LIO / ONLY_LO`（`LIVMapper.cpp:113`）。

### 1.3 r3live

- 论文：R3LIVE（ICRA 2022）、R3LIVE++（补充材料在 `supply/`）。最后提交 2022-08，基本停更。
- 构建：catkin ROS1，依赖 OpenCV ≥3.3（README 专门警告版本不一致导致崩溃）、CGAL（网格）、Ceres、自带 Sophus/vcg/OpenMVS 片段；内存 <12 GB 会告警（`Global_map::Global_map` 预留 1e8–1e9 点）。
- 两个节点：`r3live_LiDAR_front_end`（输出 `/laser_cloud_flat`）+ `r3live_mapping`（LIO + VIO 双线程），离线工具 `r3live_reconstruct_mesh`。

---

## 2. 源码结构与关键模块

### 2.1 FAST_LIO 关键路径

| 文件 / 符号 | 作用 |
|---|---|
| `src/laserMapping.cpp::livox_pcl_cbk / imu_cbk` | 缓冲 Livox CustomMsg 与 IMU；时间回退清缓冲；`time_sync_en` 自同步 |
| `::sync_packages(MeasureGroup&)` | 取一帧 LiDAR，`lidar_end_time = beg + 最后一点 curvature/1000`（curvature 字段被复用为点内时间偏移，单位 ms），收齐到 end_time 的 IMU |
| `src/IMU_Processing.hpp::ImuProcess::IMU_init` | 前 `MAX_INI_COUNT=10` 帧静止初始化：均值加速度定重力方向、均值角速度定 `bg`；`acc * G_m_s2 / |mean_acc|` 兼容以 g 为单位的 Livox IMU |
| `::UndistortPcl` | 前向传播（逐 IMU 中值积分 `kf_state.predict`，记录 `IMUpose`）+ 反向逐点去畸变（见 §3.3） |
| `::lasermap_fov_segment` | 局部地图立方体平移，`ikdtree.Delete_Point_Boxes` 删除移出区域（§3.2） |
| `::h_share_model` | 量测模型：ikd-Tree 5 近邻 → `esti_plane` 平面拟合 → 点到面残差与 12 列雅可比（§3.4） |
| `include/IKFoM_toolkit/esekfom/esekfom.hpp::update_iterated_dyn_share_modified` | 流形上迭代误差状态卡尔曼滤波（IESKF），信息矩阵形式求增益 |
| `include/use-ikfom.hpp` | 状态流形 `state_ikfom{pos, rot, offset_R_L_I, offset_T_L_I, vel, bg, ba, grav(S2)}`（23 维切空间），`get_f / df_dx / df_dw` |
| `::map_incremental` | 按体素中心规则决定哪些点入 ikd-Tree（§3.1） |
| `::publish_frame_world` + 结尾保存 | `pcd_save_en` 时把**每帧未降采样的去畸变点**（`feats_undistort`）转世界系累加到 `pcl_wait_save`，`interval=-1` 时退出时写 `PCD/scans.pcd`（全量驻留内存） |
| `::publish_odometry` | `/Odometry`（frame `camera_init`→`body`），协方差由 `kf.get_P()` 按 `k = i<3 ? i+3 : i-3` 重排——IKFoM 状态序为 [pos, rot]，重排后消息里实际是 **[rot, pos] 顺序，与 ROS 约定 [pos, rot] 相反**，ingest 时必须交换 |
| 话题 | `/cloud_registered`（世界系单帧）、`/cloud_registered_body`、`/Odometry`、`/path`、`/Laser_map`（默认关闭） |

MID-360 配置（`config/mid360.yaml` + `launch/mapping_mid360.launch`）：

| 参数 | 值 | 含义 |
|---|---|---|
| `preprocess/lidar_type` / `scan_line` / `blind` | 1 / 4 / 0.5 m | Livox 驱动格式、4 线、盲区 |
| `point_filter_num` | 3 | 每 3 点取 1（MID-360 20 万点/秒 → 约 6.7 万点/秒） |
| `filter_size_surf` / `filter_size_map` | 0.5 / 0.5 m | 扫描降采样 / 地图体素 |
| `max_iteration` | 3 | IEKF 迭代 |
| `cube_side_length` / `det_range` / `fov_degree` | 1000 m / 100 m / 360° | 局部地图边长、探测距离 |
| `acc_cov / gyr_cov / b_acc_cov / b_gyr_cov` | 0.1 / 0.1 / 1e-4 / 1e-4 | 过程噪声 |
| `extrinsic_T` | [-0.011, -0.02329, 0.04412] | LiDAR 在内置 IMU 系下的位置 `offset_T_L_I`（厂商值），`extrinsic_est_en: false` |

交叉印证：Prometheus 的 P600 Gazebo 配置 `refs/sim/Prometheus/Modules/FAST_LIO/config/mid360_gazebo.yaml` 中 `extrinsic_R` 为绕 y 轴约 20° 的旋转（0.9397/0.342），`extrinsic_T=[-0.13,0,-0.23]`，IMU 取自飞控 `/uav1/mavros/imu/data` —— 说明 P600 的 MID-360 是**倾斜安装**，我们的虚拟 P600 传感器挂载必须建模该外参。Prometheus 版还把消息换成 `prometheus_msgs/LivoxCustomMsg`、加了 `/Stop_Odom` 暂停发布与里程计日志。

### 2.2 ikd-Tree（Prometheus 副本）

- 节点 `KD_TREE_NODE`：`point`、`division_axis`、`TreeSize`、`invalid_point_num`、`down_del_num`、惰性标记 `point_deleted / tree_deleted / point_downsample_deleted / need_push_down_to_left/right`、包围盒 `node_range_x/y/z[2]`、父子指针。
- `BuildTree`：取包围盒最长轴，`nth_element` 中位数分割（平衡树）。
- `Criterion_Check`：`α_del = invalid/size > delete_param(0.5)` 或 `α_bal = |left|/(size-1)` 不在 `[1-β, β]`（构造默认 β=0.6）→ 重建；`TreeSize ≥ 1500` 的子树交给后台线程 `multi_thread_rebuild`，期间的增删写入 `Rebuild_Logger` 操作日志回放。
- `Add_Points(points, downsample_on)`：下采样模式下对每个新点取其所在 `downsample_size` 立方体，`Search_by_range` 找盒内已有点，**保留离体素中心最近的那一个**，其余 `Delete_by_range(..., is_downsample=true)`。
- `Nearest_Search`：手写大顶堆 `MANUAL_HEAP` + `calc_box_dist` 剪枝的 kNN；`Delete_Point_Boxes` 盒删除为惰性标记；`acquire_removed_points` 可取回被删除点（FAST_LIO 中这些点被丢弃——对我们而言正是应"冷存储"落盘的点）。

### 2.3 FAST-LIVO2 关键路径

| 文件 / 符号 | 作用 |
|---|---|
| `LIVMapper::sync_packages`（LIVO 分支） | **以图像曝光时刻切分 LiDAR 流**：把 `offset < img_time` 的点放 `pcl_proc_cur`，其余进 `pcl_proc_next`，于是每次 LIO 更新的时刻恰为图像时刻，随后立刻做一次 VIO 更新（`lio_vio_flg` 在 LIO/VIO 间交替） |
| `ImuProcess::Process2 / UndistortPcl / Forward_without_imu` | IMU 前向传播 + 去畸变；无 IMU 时匀速模型 |
| `VoxelMapManager::StateEstimation` | LIO 更新：逐点 `calcBodyCov` → 世界系协方差 → `BuildResidualListOMP` 概率平面匹配 → 6 维（旋转+平移）量测的 IEKF，状态 `DIM_STATE=19`（rot, pos, inv_expo, vel, bg, ba, g） |
| `VoxelOctoTree::{init_plane, cut_octo_tree, UpdateOctoTree}` | 哈希体素（`voxel_size=0.5 m`）内的自适应八叉树：点数够时 PCA 拟合平面，最小特征值 < `min_eigen_value` 则为平面并计算 6×6 平面不确定度 `plane_var_`；否则切分至 `max_layer` |
| `VoxelMapManager::mapSliding / clearMemOutOfMap` | 以 `half_map_size`（体素数）为半径裁剪哈希表，移动超 `sliding_thresh` 米触发 |
| `VIOManager::processFrame` | `retrieveFromVisualSparseMap`（投影深度图做遮挡剔除、网格选点）→ `computeJacobianAndUpdateEKF`（逐像素光度残差，含曝光时间 `inv_expo_time` 估计）→ `generateVisualMapPoints` → `updateVisualMapPoints` → `updateReferencePatch` |
| `LIVMapper::publish_frame_world` | **着色**：累计的世界系点投影到当前帧图像，`getInterpolatedPixel` 双线性取 BGR，`pf.norm() > blind_rgb_points` 才保留；`pcd_save` 时累加 `PointXYZRGB` |
| `LIVMapper::savePCD` | 退出时写 `Log/pcd/all_raw_points.pcd` 与 `filter_size_pcd`（0.15 m）体素降采样版；`colmap_output_en` 时写 `Log/Colmap/sparse/0/{cameras,images,points3D}.txt` 与去畸变图像（`VIOManager::dumpDataForColmap`）——**可直接作为 3DGS/nerfstudio 训练输入** |
| `LIVMapper::imu_prop_callback` | 4 ms 定时器，以最新 EKF 状态为起点做 IMU 递推，发布 `/LIVO2/imu_propagate`（配置 `uav/imu_rate_odom`，专为无人机控制） |
| `LIVMapper::publish_mavros` | 发布 `/mavros/vision_pose/pose` → PX4 EKF2 外部视觉融合 |
| `scripts/mesh.py` | vdbfusion TSDF（voxel 0.02、trunc 0.1）→ 网格 → KDTree 最近点给顶点上色 |

### 2.4 R3LIVE 关键路径

| 文件 / 符号 | 作用 |
|---|---|
| `rgb_map/pointcloud_rgbd.hpp::RGB_pts` | 每点 `m_pos[3]`、`m_rgb[3]`、`m_cov_rgb[3]`、`m_N_rgb`、`m_obs_dis`（最佳观测距离）、`m_last_obs_time` |
| `::RGB_Voxel` / `Global_map` | 两级哈希：`m_hashmap_3d_pts`（`minimum_pts_size` 细网格去重，配置 0.01 m）+ `m_hashmap_voxels`（`voxel_resolution=0.1 m` 粗体素，存点列表）；`m_voxels_recent_visited` 为最近被 LiDAR 命中的活跃体素集 |
| `Global_map::append_points_to_global_map` | 新 LIO 帧每 `step=4` 点插入；细网格已存在则拒绝；更新活跃体素集 |
| `RGB_pts::update_rgb` | 逆方差贝叶斯颜色融合 + 距离门限（§3.8） |
| `render_pts_in_voxels_mp` | 线程池把活跃体素内所有点投到当前帧取色（**无遮挡检测**，仅靠距离门限） |
| `Global_map::selection_points_for_projection` | 以 `minimum_dis` 像素为格的 2D 深度掩码（每格留最近点）选跟踪点——本质是稀疏 z-buffer |
| `r3live_vio.cpp::vio_esikf / vio_photometric` | 帧间 PnP-ESIKF + 帧到地图光度 ESIKF（残差 = 观测 RGB − 地图点 RGB，权重 = 点颜色协方差之逆，Huber），状态 29 维含相机外参、时延、内参在线标定 |
| `::service_pub_rgb_maps` | 按 `m_N_rgb ≥ 1` 过滤后每 1000 点一个话题 `/RGB_map_i` 分块发布，话题数 ≥45 后块大小 ×1.5 |
| `offline_map_recorder` + `r3live_reconstruct_mesh.cpp` | 序列化 `.r3live`（点 + 图像位姿 + 可视关系）→ Delaunay/graph-cut 网格 → `texture_mesh` 用 kNN（`smooth_factor=10`）平均点色给顶点上色 |

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 体素中心最近点降采样（ikd-Tree `Add_Points` + FAST_LIO `map_incremental`）

用途：(a) 离线八叉树每层 LOD 采样；(b) 实时建图流的去重；(c) mock LIO 地图。优点：确定性、可增量、空间均匀，且"新点替换旧点"只发生在更靠近体素中心时，天然稳定不闪烁。

```python
# cell = 当前 LOD 层网格边长 s_L；key 用 int64 哈希
def insert_downsampled(grid: dict, p: np.ndarray, s: float):
    key = tuple(np.floor(p / s).astype(np.int64))
    c = (np.array(key) + 0.5) * s                      # 体素中心
    old = grid.get(key)
    if old is None or np.sum((p - c)**2) < np.sum((old - c)**2):
        grid[key] = p                                   # 保留最接近中心的点
        return True                                      # 产生一次 "delta"
    return False

# 向量化批量版本（numpy，适合 5M 点离线构建）：
def voxel_center_sample(P, s):
    k = np.floor(P / s).astype(np.int64)
    d = np.sum((P - (k + 0.5) * s) ** 2, axis=1)
    kid = (k[:,0] * 73856093) ^ (k[:,1] * 19349663) ^ (k[:,2] * 83492791)   # 或 np.unique(k, axis=0)
    order = np.lexsort((d, kid))                          # 同体素内按距离升序
    first = np.r_[True, kid[order][1:] != kid[order][:-1]]
    return order[first]                                   # 每体素一个代表点的索引
```

八叉树 LOD 采样建议：节点包围盒边长 `L_n`，每节点目标网格 `g=128`（每轴），则层内采样边长 `s = L_n / g`；先对全体点做 `voxel_center_sample` 得到根节点代表点，剩余点下沉到子节点递归（与 Potree 的"逐层抽稀"兼容，区别在于用中心最近点而非随机/泊松盘）。FAST_LIO 的 `map_incremental` 额外有一个快速判据：若最近邻都不在本体素（三轴距离中心均 > s/2），直接加入不做盒搜索（`PointNoNeedDownsample`），批量时可用"哈希未命中即加入"等价实现。

### 3.2 局部地图立方体迟滞平移（FAST_LIO `lasermap_fov_segment`）

原式（`MOV_THRESHOLD=1.5`，`DET_RANGE=R`，边长 `L`）：

- 触发：`∃ 轴 i：|p_i − box_min_i| ≤ 1.5R 或 |p_i − box_max_i| ≤ 1.5R`
- 平移量：`m = max( 0.9·(L − 2·1.5R)/2 , (1.5 − 1)·R )`；MID-360 配置 `L=1000, R=100` → `m = max(315, 50) = 315 m`
- 被移出的板状区域 `Delete_Point_Boxes`。

在本项目的推广（驻留集 / 缓存驱逐，带迟滞，避免抖动）：

```ts
// Web 端：点云驻留区域随相机移动；服务端：实时建图会话内存上界（同 FAST-LIVO2 mapSliding）
function updateResidentBox(box: AABB, c: Vec3, R: number, L: number, k = 1.5): AABB[] /*evict slabs*/ {
  const evict: AABB[] = [];
  for (const i of [0, 1, 2]) {
    const dMin = c[i] - box.min[i], dMax = box.max[i] - c[i];
    if (dMin > k * R && dMax > k * R) continue;
    const m = Math.max(0.9 * (L - 2 * k * R) / 2, (k - 1) * R);
    const dir = dMin <= k * R ? -1 : +1;
    evict.push(slabOpposite(box, i, dir, m));   // 被抛弃的一侧
    box.min[i] += dir * m; box.max[i] += dir * m;
  }
  return evict;  // 对应节点/瓦片进入 LRU "冷" 队列，而非立即释放 GPU buffer
}
```

参数建议：Web 驻留 `R = 相机远裁剪面或 3×当前 SSE 截断距离`，`L = 4R`；实时建图服务端 `R = 70 m`（MID-360 高反射率量程），`L = 600 m`。

### 3.3 IMU 前向传播与逐点去畸变（`ImuProcess::UndistortPcl`、FAST-LIVO2 `prop_imu_once`）

前向（中值积分，`in.acc = ½(a_k + a_{k+1})·G/|ā_init|`）：

```
ω = ω_m − b_g ;  a_w = R(a_m − b_a) + g
R ← R · Exp(ω Δt) ;  p ← p + v Δt + ½ a_w Δt² ;  v ← v + a_w Δt
P ← F P Fᵀ + G Q Gᵀ           （Q = diag(cov_gyr, cov_acc, cov_bg, cov_ba)）
```

反向去畸变（点 i 在时刻 t_i，帧末 e）：

```
R_i = R_head · Exp(ω_tail · dt) ;  T_ei = p_head + v_head dt + ½ a_tail dt² − p_e
P_comp = R_LIᵀ ( R_eᵀ ( R_i (R_LI P_i + t_LI) + T_ei ) − t_LI )
```

对本项目：(1) mock 仿真的 `DroneState` 积分器直接用上式（与 FAST-LIVO2 `prop_imu_once` 一致）；(2) 若 V0.4/V0.5 模拟"运动中扫描"，按点时间戳用相同公式施加/去除畸变；(3) **遥测平滑**：FAST-LIVO2 在 EKF 状态上以 IMU 频率（4 ms）递推发布 `/LIVO2/imu_propagate`。我们的 Simulation Service 物理 100–400 Hz、WebSocket 10–50 Hz，浏览器端按同一运动学对每架无人机做 dead-reckoning 外推 + 收到新帧后 150 ms 内指数收敛（`x_disp ← x_disp + (x_pred − x_disp)·(1 − e^{−Δt/τ})`，τ=0.08 s），可在 10 Hz 遥测下保持 60 FPS 平滑。

### 3.4 FAST-LIO2 点到面 IEKF 量测（`h_share_model` + `esti_plane` + esekfom）

1. 点变换：`p_W = R(R_LI p_L + t_LI) + p`。
2. ikd-Tree 取 `NUM_MATCH_POINTS=5` 近邻；若不足 5 或第 5 近邻平方距离 > 5 m² → 丢弃。
3. 平面拟合：解最小二乘 `A n = −1`（A 为 5×3 近邻坐标），`n̂ = n/|n|`，`d = 1/|n|`；任一近邻 `|n̂·q + d| > 0.1` → 非平面。
4. 残差 `r = n̂·p_W + d`；选择权重 `s = 1 − 0.9|r| / sqrt(|p_L|)`，`s > 0.9` 才采用（远处点容忍更大残差）。
5. 雅可比行（12 列）：`[ n̂ᵀ , (p_I^∧ Rᵀ n̂)ᵀ , (p_L^∧ R_LIᵀ Rᵀ n̂)ᵀ , (Rᵀ n̂)ᵀ ]`（后两块仅在外参在线估计时非零），量测 `z = −r`，`R = LASER_POINT_COV = 0.001`。
6. 更新（状态维 23 ≤ 量测维时用信息形式）：
   `K = (HᵀH + (P/R)⁻¹)⁻¹ Hᵀ`；`δx = K z + (K H − I) J⁻¹ (x̂ ⊟ x_prop)`；`x̂ ← x̂ ⊞ δx`；
   收敛判据 `|δx_i| < 0.001`；**只有在上一次收敛后才重新做近邻搜索（rematch）**，最多 `max_iteration` 次；结束时 `P ← (I − K H) P`（含 SO3/S2 切空间雅可比修正）。
   更干净的无模板实现见 `refs/lidar/FASTLIO2_ROS2/fastlio2/src/map_builder/ieskf.cpp`（21 维，Sophus，`H += JᵀP⁻¹J; b += JᵀP⁻¹δ; δ = −H⁻¹b`）——若 V0.5 需要 Python 版 mock LIO，以它为移植源。

**可观性 / 退化度量（本项目 mock 与 QA 的关键移植点）**：雅可比位置块就是 `n̂ᵀ`，所以平移信息矩阵 `M = Σ_i w_i n̂_i n̂_iᵀ`（3×3）。UrbanScene3D 点云自带法向（PLY 属性 `nx ny nz`），因此模拟一帧扫描后可直接计算：

```python
def translational_observability(normals, weights=None):
    W = np.ones(len(normals)) if weights is None else weights
    M = (normals * W[:, None]).T @ normals          # Σ w n nᵀ
    lam, V = np.linalg.eigh(M)                      # 升序
    return lam / max(len(normals), 1), V            # 归一化特征值与方向
# 判据（经验值，待真机数据标定）：
#   n_eff < 200            → "特征不足"
#   lam_min/lam_max < 0.02 → "沿 V[:,0] 方向退化"（空旷地面、长走廊、纯立面）
```

### 3.5 FAST-LIVO2 概率体素平面图（`voxel_map.cpp`）

- 体素键：`loc = floor(p / 0.5)`（负数先减 1），哈希 `((z·P) mod N + y)·P mod N + x`，`P=116101`，`N=1e10`。
- `init_plane`：`Σ = E[ppᵀ] − μμᵀ`，特征分解 `λ_min<λ_mid<λ_max`；`λ_min < min_eigen_value(0.0025)` 则为平面，法向 = 最小特征向量，`radius = sqrt(λ_max)`；平面参数不确定度 `Σ_plane = Σ_i J_i Σ_{p_i} J_iᵀ`（6×6，[n, q]）。
- 残差门限：`σ_l = J_nq Σ_plane J_nqᵀ + n̂ᵀ Σ_p n̂`，接受 `|dist| < 3√σ_l` 且点到平面中心投影距离 ≤ 3·radius；权重 `R⁻¹ = 1/(0.001 + σ_l + n̂ᵀ Σ_p n̂)`。
- 收敛：`|δθ| < 0.01°` 且 `|δt| < 0.015 cm`，最多 `max_iterations=5`。
- 对本项目：离线 QA 的"地图厚度 / 平面性"指标（每体素 `λ_min`、`trace(Σ_plane)`）与 Geometry World 的"平面片 + 占据"表示（V0.6 规划/避障可直接用平面片做碰撞）。可视化上它用 `mapJet(trace^0.2)` 给平面上色；我们改用品牌灰→红单色阶（高不确定度为红 `#E93024`）。

### 3.6 LiDAR 测量噪声模型（`calcBodyCov`）与环境退化扩展

原式：点 `p`，距离 `r=|p|`，方向 `d̂=p/r`，`N=[b1 b2]` 为垂直于 d̂ 的正交基，

```
Σ_p = d̂ σ_r² d̂ᵀ + A σ_θ² Aᵀ ,  A = r · [d̂]_× · N ,  σ_θ² = sin²(beam_err)
FAST-LIVO2 默认：σ_r = dept_err = 0.02 m ，beam_err = 0.05°
```

本项目扩展（V0.3 视觉 → V0.4 物理：Environment → Sensor；雾/雨/沙尘统一用消光系数 β[1/m]）：

```
回波功率 ∝ ρ·exp(−2βr)/r²   →   最大量程 R_max(β) 满足 ρ·exp(−2βR)/R² = ρ_ref·/R_ref²
σ_r(β, r) = σ_r0 · (1 + k_r · β · r)                       # k_r ≈ 5（待标定）
p_drop(β, r) = 1 − exp(−2βr) · (1 − p_drop0)                # 丢点概率
p_ghost(rain_rate) = c_g · rain_mmph · exp(−r / 5 m)         # 近场雨滴/沙尘假点，放在 0.5–5 m
雾的能见度 V[m] 与 β：β ≈ 3.912 / V（Koschmieder）
```

每个仿真 LiDAR 点：按 `Σ_p(β)` 采样高斯噪声，按 `p_drop` 丢弃，按 `p_ghost` 注入近场散射点。MID-360 驱动 `CustomPoint.tag` 字段携带点置信/回波信息（FAST_LIO 仅保留 `(tag & 0x30) ∈ {0x00, 0x10}`），仿真时同样写入 tag，便于下游按真机方式过滤。

### 3.7 用 UrbanScene3D 模拟 MID-360 扫描（MVP 可做，移植自 R3LIVE 深度掩码 + MARSIM 思路）

输入：已加载的城市点云（xyz + 法向，5M 点）、无人机位姿 `T_WB`、安装外参 `T_BL`（P600 参考：绕 y 轴约 20°、`t=[-0.13,0,-0.23]`）。MID-360 规格（官方数据，需用 P600 实机 MID-360S 复核）：水平 360°，垂直 −7°~+52°，量程 40 m@10% / 70 m@80%，最小 0.1 m，20 万点/秒，10 Hz。

```python
AZ_BINS, EL_BINS = 720, 118          # 0.5° × 0.5°（非重复扫描的积分近似）
EL_MIN, EL_MAX = np.deg2rad(-7), np.deg2rad(52)
R_MAX, PTS_PER_FRAME = 40.0, 20000       # 70.0 仅用于高反射率场景

def simulate_mid360_frame(grid_index, T_WL, beta=0.0, rng=np.random):
    c = T_WL[:3, 3]
    P, N = grid_index.query_ball(c, R_MAX)           # 预建 10 m 体素哈希 → 候选点
    q = (P - c) @ T_WL[:3, :3]                        # 世界 → LiDAR 系
    r = np.linalg.norm(q, axis=1)
    az = np.arctan2(q[:,1], q[:,0]); el = np.arcsin(q[:,2] / np.maximum(r, 1e-6))
    m = (r > 0.1) & (r < Rmax_fog(beta)) & (el >= EL_MIN) & (el <= EL_MAX)
    q, r, az, el, N = q[m], r[m], az[m], el[m], N[m]
    b = ((az + np.pi) / (2*np.pi) * AZ_BINS).astype(np.int32) * EL_BINS \
        + ((el - EL_MIN) / (EL_MAX - EL_MIN) * (EL_BINS - 1)).astype(np.int32)
    zb = np.full(AZ_BINS * EL_BINS, np.inf, np.float32)
    np.minimum.at(zb, b, r)                           # 球面 z-buffer：每个角度格最近距离（遮挡）
    idx = np.flatnonzero(r <= zb[b] + 1e-4)           # 只留每格最近点（比 lexsort 快约 5 倍）
    if len(idx) > PTS_PER_FRAME: idx = rng.choice(idx, PTS_PER_FRAME, replace=False)
    q, r = q[idx], r[idx]
    q += sample_body_cov_noise(q, sigma_r(beta, r), beam_err=0.05)   # §3.6
    keep = rng.random(len(q)) > p_drop(beta, r)
    return q[keep], N[idx][keep], r[keep]
```

每帧输出同时计算 §3.4 的可观性指标 → 驱动 mock LIO 漂移（§3.10）与 UI 的"LIO 健康"。浏览器端只接收"增量体素"（§3.9），不接收原始扫描。

**实测基准**（本机 CPU、纯 numpy，`San Francisco_sampled_5m.ply`，离地约 25 m，倒装 + 20° 俯仰，脚本 `.cache/research/r05_mock_mid360_bench*.py`）：

| 方案 | 候选点 P50 | 每帧输出点 | 单帧耗时 P50 / P95 |
|---|---|---|---|
| 原始 5M 点，R=70 m，lexsort 选最近 | 207,636 | 20,000 | 201 / 350 ms（不满足 10 Hz） |
| 先按 §3.1 规则降采样到 0.5 m（5.0M→2.44M，一次性 6.8 s），R=70 m，`np.minimum.at` z-buffer | 95,989 | 20,000 | 37 / 98 ms |
| 同上，R=40 m（MID-360 10% 反射率量程） | 36,255 | 12,485 | **9.3 / 22.7 ms** |

结论：mock LiDAR 必须用预降采样的"传感器专用 LOD"（0.5 m）+ 10 m 格网索引 + 先用 `sin(el)` 过滤再算 `arctan2`；默认量程取 40 m，70 m 仅作高反射率可选项。多机时每机每帧 ~10 ms，单进程可支撑约 5 架 10 Hz；更多机型需多进程或降频到 5 Hz。

### 3.8 RGB 着色：FAST-LIVO2 直接投影 + R3LIVE 贝叶斯融合（V0.5 离线移植）

FAST-LIVO2 做法：LIO 在图像时刻更新 → 该帧点投影 `pc = w2c(p_w)`，`pf.z > 0` 且在图像内（边界 3 px），`getInterpolatedPixel` 双线性取色；单帧单次、无遮挡判断、无多视融合，边缘易"串色"（相机与 LiDAR 视差）。

R3LIVE 做法（`RGB_pts::update_rgb`，`image_obs_cov=15`，`process_noise_sigma=0.1`）：

```
若 obs_dis > 1.2 · best_dis：拒绝（只信任更近的观测）
首次：rgb = obs ; σ = σ_obs
否则：σ_prev = σ + q · (t − t_last)                       # 注意源码把噪声加在 σ 而非 σ² 上
      σ_new = ( 1/σ_prev² + 1/σ_obs² )^(−1/2)
      rgb   = σ_new² · ( rgb/σ_prev² + obs/σ_obs² )
发布/保存门限：N_rgb ≥ pub_pt_minimum_views (3)
```

本项目离线着色器（`reconstruction/fusion/colorize.py`）= 两者合并 + 补上遮挡：

```python
for k, (img, T_cw, t_k) in enumerate(keyframes):            # 关键帧：旋转>10° 或平移>0.15 m（R3LIVE 网格工具默认）
    pc = (P_w @ T_cw[:3,:3].T) + T_cw[:3,3]; z = pc[:,2]
    v = (z > 0.3) & (z < 200)
    uv = project(K, dist, pc[v])
    inb = in_image(uv, border=3)
    ids = np.flatnonzero(v)[inb]; uv = uv[inb]; zz = z[ids]
    zbuf = zbuffer_min(uv // 2, zz)                            # 2 px 网格深度掩码（R3LIVE selection 的稠密版）
    vis = zz <= zbuf[uv // 2] * 1.01 + 0.05
    ids, uv, zz = ids[vis], uv[vis], zz[vis]
    obs = bilinear(img, uv)                                    # FAST-LIVO2 getInterpolatedPixel
    bayes_update(ids, obs, dist=zz, t=t_k, sigma_obs=15, q=0.1, gate=1.2)
save_ply(P_w[n_obs >= 3], rgb[n_obs >= 3])
```

曝光一致性：FAST-LIVO2 估计了 `inv_expo_time`，若使用其导出结果，建议颜色乘以 `inv_expo_time / median(inv_expo_time)` 再融合（源码中该行被注释掉，是已知的明暗斑来源）。

### 3.9 实时建图增量推流（R3LIVE 二级哈希 + 活跃体素 + 自适应分块）

服务端（Gateway/Simulation Service）维护：细网格去重 `s_fine=0.15 m`（§3.1 规则）、瓦片 `S=32 m` 的"脏瓦片"集合、每点观测计数 `nObs`；仅当 `nObs ≥ 2`（R3LIVE 的 `pub_pt_minimum_views` 思想，mock 可取 1）才推送，抑制噪点闪烁。

二进制消息 `lio.mapDelta`（小端，一个 WS 帧内可串接多个瓦片记录）：

| 偏移 | 类型 | 字段 |
|---|---|---|
| 0 | u32 | magic `'LMD1'` |
| 4 | u16 | version = 1 |
| 6 | u16 | flags：bit0 intensity，bit1 rgb，bit2 nObs |
| 8 | u32 | seq（单调，断线重连按 seq 补发） |
| 12 | f64 | t_sensor（秒） |
| 20 | i32×3 | tile_ix, tile_iy, tile_iz（瓦片键，世界 ENU / S） |
| 32 | u32 | count |
| 36 | f32 | scale = S / 65535 |
| 40 | u16×3×count | 相对瓦片最小角的量化坐标 |
| … | u8×count | intensity（可选） |
| … | u8×3×count | rgb（可选） |
| … | u8×count | nObs（可选） |

每点 7–11 字节。自适应分块：初始每批 2000 点，若一次推送的记录数 > 45（R3LIVE 的话题上限）则批大小 ×1.5，保证单次 WS 推送帧数有界；推送频率 2 Hz（实时建图），与 `lio.odom`（10–20 Hz JSON）解耦。浏览器侧每瓦片一个 `THREE.Points` 追加缓冲（预分配容量，`drawRange` 增长），纳入全局 point budget。

视觉建议：最新一帧扫描点以品牌红 `#E93024` 绘制，1.5 s 内线性淡出为科技灰（顶点属性 `tAdd`，shader 中 `mix(red, gray, clamp((now − tAdd)/1.5, 0, 1))`），累积地图为灰阶按高度或 nObs 映射。

### 3.10 Mock LIO 估计器（V0.2–V0.4 仿真中的"定位源"）

```python
class MockLIO:   # 位置漂移 = 行程比例随机游走 + 退化放大；航向漂移单独建模
    def __init__(s, drift_pct=0.003, yaw_drift_deg_per_100m=0.2):
        s.bias = np.zeros(3); s.yaw_bias = 0.0
    def step(s, p_true, v, dt, obs_eig, obs_vec, n_eff):
        ds = np.linalg.norm(v) * dt
        sig = s.drift_pct * np.sqrt(ds) * np.ones(3)
        if n_eff < 200 or obs_eig[0] / max(obs_eig[2], 1e-9) < 0.02:
            sig += 10 * s.drift_pct * np.sqrt(ds) * np.abs(obs_vec[:, 0])   # 沿退化方向放大
        s.bias += np.random.normal(0, sig)
        s.yaw_bias += np.random.normal(0, np.deg2rad(s.yaw_drift/100) * np.sqrt(ds))
        cov_pos = np.diag(sig**2 + 1e-4)
        return p_true + s.bias, s.yaw_bias, cov_pos   # 推给 PX4-SITL 的 EV 融合或直接作为 est 状态
```

UI 同时绘制"真值轨迹（灰）/估计轨迹（红虚线）"，并在 LIO 健康面板显示 `n_eff`、`λ_min/λ_max`、漂移估计。V0.5 替换为回放真实 FAST-LIO 输出即可，接口不变。

### 3.11 关键参数总表（落地时的默认值）

| 场景 | 参数 | 建议值 | 来源 |
|---|---|---|---|
| 真机 MID-360 LIO | point_filter_num / filter_size_surf / filter_size_map / max_iteration | 3 / 0.5 / 0.5 / 3（低空城市可 0.3/0.3/4） | FAST_LIO mid360 launch |
| 真机 MID-360 LIO | blind / det_range / cube_len | 0.5 / 100 / 1000 | 同上 |
| 离线高质量 LIO | voxel_size / max_layer / min_eigen / dept_err / beam_err | 0.5 / 2 / 0.0025 / 0.02 / 0.05 | FAST-LIVO2 avia.yaml |
| 地图导出 | 稠密地图体素 | 0.05–0.15 m（FAST-LIVO2 `filter_size_pcd=0.15`） | FAST-LIVO2 |
| 着色 | σ_obs / q / 距离门限 / 最少视角 | 15 / 0.1 / 1.2× / 3 | R3LIVE |
| mock 扫描 | 角分辨率 / 每帧点数上限 / 频率 / 量程 / 源点云 LOD | 0.5° / 2 万 / 10 Hz / 40 m / 0.5 m | MID-360 规格折算 + 本机实测 |
| 增量推流 | 去重网格 / 瓦片 / 推流频率 | 0.15 m / 32 m / 2 Hz | 本笔记设计 |

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块落点表

| 算法 / 实现 | 源 | 目标模块（01-design §42 目录） | 方式 | 版本 |
|---|---|---|---|---|
| 体素中心最近点降采样 | ikd-Tree `Add_Points`、FAST_LIO `map_incremental` | `world/pointcloud`（八叉树构建）、`apps/api` 实时建图 | port（numpy / TS） | MVP |
| 局部立方体迟滞平移 | FAST_LIO `lasermap_fov_segment`、FAST-LIVO2 `mapSliding` | `apps/web` 点云驻留集、`apps/simulator` 会话内存 | port | MVP |
| 球面 z-buffer mock LiDAR | R3LIVE `selection_points_for_projection` 思路 + MARSIM | `sensors/lidar`（mock MID-360） | port | MVP（可选演示）/ V0.4 |
| 噪声模型 + 环境退化 | FAST-LIVO2 `calcBodyCov` | `sensors/lidar` + `environment/fog, rain, sand` | port + 扩展 | V0.4（MVP 可先做可视化） |
| 二级哈希增量推流 | R3LIVE `Global_map`、`service_pub_rgb_maps` | `apps/api` WS `lio.mapDelta` | port | MVP |
| IMU 递推 / dead-reckoning | FAST-LIVO2 `prop_imu_once / imu_prop_callback` | `apps/simulator` 积分器、`apps/web` 遥测平滑 | port | MVP |
| 平移可观性 / 退化 | FAST_LIO 雅可比结构 | `simulation`（MockLIO）、`reconstruction/lidar` QA | port | MVP（mock）/ V0.5（QA） |
| FAST-LIO2 全管线 | FAST_LIO / Prometheus FAST_LIO / FASTLIO2_ROS2 | `reconstruction/lidar`、P600 机载 | adopt（Docker ROS1 或 ROS2 移植版） | V0.5 |
| IESKF | esekfom / FASTLIO2_ROS2 `ieskf.cpp` | `simulation/estimators`（若需真算法 mock） | reference → port | V0.5+ |
| 概率体素平面图 | FAST-LIVO2 `VoxelOctoTree` | `world/geometry`（平面片、QA 厚度） | reference / port | V0.5–V0.6 |
| RGB 直接投影着色 + COLMAP 导出 | FAST-LIVO2 `publish_frame_world / dumpDataForColmap` | `reconstruction/fusion`、`visual/gaussian` | adopt（有硬同步时）/ reference | V0.5 / V1.0 |
| 贝叶斯多视颜色融合 | R3LIVE `update_rgb` | `reconstruction/fusion/colorize.py` | port | V0.5 |
| 关键帧 patch + poses 保存格式 | FASTLIO2_ROS2 `PGONode::saveMapsCB` | World Package `reconstruction/lidar/` | adopt 格式 | V0.5（契约 MVP 期冻结） |
| EV 位姿桥接 | FAST-LIVO2 `publish_mavros` | `simulation/px4` | reference | V0.2 / V0.5 |
| 时间日志 / 分阶段耗时 | FAST_LIO `fast_lio_time_log.csv`、FAST-LIVO2 计时表 | UI "LIO 健康"（lieflat 图表） | port | V0.5 |

### 4.2 V0.5 Real World Fusion 中 LIO 的角色

```text
[P600 机载 / Jetson Orin NX / Prometheus(ROS1)]
 MID-360(S) ─livox_ros_driver2─> /livox/lidar(CustomMsg,10Hz) + /livox/imu(200Hz)
                                  │
                                  v
                   FAST-LIO2（Prometheus Modules/FAST_LIO）
                   ├─ /Odometry 10Hz(+cov) ─> mavros /vision_pose ─> PX4 EKF2（EV 融合，GNSS 弱时主定位）
                   ├─ 0.5 m 体素地图增量 ─> 地面站 TCP/UDP ─> Gateway ─> WS lio.* ─> Web "实时建图"
                   └─ rosbag：raw lidar + imu + 吊舱图像 + RTK + 飞控日志（离线的唯一真值数据面）

[离线重建服务器（CPU 即可，无需 GPU）]
 (1) LIO 复算：FAST-LIO2（默认）/ FAST-LIVO2（仅当相机硬同步 + FAST-Calib 外参）
 (2) 关键帧化：patch(body 系去畸变点) + poses + 6×6 协方差 + 每帧 QA
 (3) 位姿图：回环 + RTK/GNSS 因子（FASTLIO2_ROS2 pgo / GLIM / hdl_graph_slam，见其它研究单元）→ ENU 位姿
 (4) 地图拼装：patch × 优化位姿 → Open3D 统计滤波 + 0.05–0.15 m 体素 →（可选）动态物体剔除
 (5) 着色：§3.8（无同步相机时仅做离线多视融合，时间偏移用网格搜索标定）
 (6) 视觉融合：LingBot-Map 相机轨迹 <-> LIO 轨迹按时间戳 Umeyama Sim(3) 对齐（定尺度+ENU）→ small_gicp 点到面精配
 (7) 写 World Package：geometry/pointcloud（八叉树瓦片）、geometry/voxel（占据/平面片）、reconstruction/lidar/<session>/、coordinate.json（坐标系链）
```

LIO 在 V0.5 输出的三件事：**metric 轨迹**（给控制与视觉尺度）、**近场几何**（低空 <40 m 的建筑立面、树、线缆）、**质量证据**（QA 指标决定哪些区域可信）。

### 4.3 LIO 输出接口契约（建议 MVP 期即冻结，V0.5 实现）

```text
worlds/<world_id>/reconstruction/lidar/<session_id>/
├── session.json          # 传感器与算法元数据（见下）
├── trajectory/
│   ├── lio_odom.tum      # t x y z qx qy qz qw（IMU 系，frame=lio_world）
│   ├── lio_odom_cov.npy  # N×6×6，统一存为 ROS 约定 [pos, rot]（FAST_LIO /Odometry 原始为 [rot, pos]，写入前交换）
│   └── imu_prop.tum      # 可选：IMU 频率递推位姿
├── keyframes/
│   ├── poses.txt         # patch_name x y z qw qx qy qz（FASTLIO2_ROS2 PGO 格式）
│   └── patches/000123.pcd# body 系去畸变点 XYZI（+ 点时间偏移）
├── maps/
│   ├── map_raw.laz       # 世界系稠密点（intensity）
│   ├── map_rgb.laz       # 可选：着色结果
│   └── planes.parquet    # 可选：体素平面片 center/normal/λ/Σ_trace
└── qa/
    ├── frames.csv        # t, n_raw, n_down, n_eff, res_mean, t_down, t_icp, t_map, degenerate, λmin/λmax
    └── report.json       # ATE vs RTK、平面厚度 P50/P95、覆盖率、退化时段
```

`session.json` 关键字段：`sensor_rig{lidar_model:"MID-360S", imu_source:"internal|fc", T_imu_lidar, T_lidar_cam, K, dist, time_offsets{lidar_imu, cam_imu}}`、`algorithm{name:"FAST-LIO2", repo, commit, config_hash, params}`、`frames{lio_world:{gravity_aligned:bool}, T_enu_lio_world, T_enu_source:"rtk_4dof|pgo"}`。注意：FAST_LIO 的 `camera_init` 是首帧 IMU 位姿（**未必水平**，重力在 S2 状态中估计），FAST-LIVO2 需 `uav/gravity_align_en: true` 才对齐重力，FASTLIO2_ROS2 默认 `gravity_align: true`——契约里必须显式声明。

实时通道（WS，JSON 除 mapDelta）：`lio.{uav}.odom`（10–20 Hz：t、p、q、v、cov 对角）、`lio.{uav}.health`（1 Hz：n_eff、res_mean、分阶段耗时、degenerate、λ比、map_pts）、`lio.{uav}.mapDelta`（2 Hz，二进制 §3.9）。REST：`POST /api/worlds/{id}/lidar-sessions`（登记 bag / 上传产物）、`GET /api/worlds/{id}/lidar-sessions/{sid}`（状态 + QA 报告）。

### 4.4 与 UI / 视觉规范的对接

- "LIO 健康"卡片（shadcn `Card` + `Tabs`）：n_eff 与残差走势、分阶段耗时堆叠条（lieflat-charts 风格，灰阶 + 红色告警阈值线），退化时段在底部 Timeline 上以红色区段标注。
- 图层开关：真值轨迹 / LIO 轨迹 / 最新扫描 / 累积地图 / 不确定度热图，切换过渡用 transitions.dev 的淡入/交叉溶解，图标用 morphicons（扫描、轨迹、图层、告警），禁止 emoji。

---

## 5. 对比与推荐

| 维度 | FAST_LIO（FAST-LIO2） | FAST-LIVO2 | R3LIVE |
|---|---|---|---|
| 传感器 | LiDAR + IMU | LiDAR + IMU + 相机（可降级 LIO/LO） | LiDAR + IMU + 相机 |
| 地图结构 | ikd-Tree（0.5 m 下采样点） | 哈希体素 + 自适应八叉树平面（带不确定度）+ 视觉稀疏点 | ikd-Tree（LIO）+ 两级哈希 RGB 点图 |
| 着色 | 无（intensity） | 单帧直接投影 | 多视贝叶斯融合 + 离线网格贴色 |
| 同步要求 | LiDAR-IMU（MID-360 内置 IMU 天然同步） | **需相机硬同步**（作者开源 LIV_handhold STM32 方案）+ 精确外参 | 需同步，驱动需改时间基 |
| 算力 | 最低（Jetson/树莓派可跑） | 中（ARM 有专门优化论文） | 高（≥12 GB 内存建议） |
| MID-360 就绪度 | 自带 mid360 配置；Prometheus 已集成 | 需自配 Livox 参数（scan_line 4） | 仅 Avia/Ouster 示例 |
| ROS | ROS1（ROS2 用 FASTLIO2_ROS2） | ROS1 | ROS1 |
| 离线产物 | scans.pcd、pos_log | raw/降采样 RGB PCD、TUM 轨迹、COLMAP 工程、TSDF 网格脚本 | .r3live、RGB PCD、纹理网格 |
| 活跃度（2026） | 主仓 2024 停更，但生态（Prometheus、FASTLIO2_ROS2 2026-08）活跃 | 2026-03 更新 | 2022 停更 |
| 与本项目契合 | P600 原生、最高 | 高（着色/3DGS 输入），但依赖硬件条件 | 中（只取算法思想） |

推荐排序：**FAST-LIO2（主干，adopt）> FAST-LIVO2（条件 adopt / 着色与 3DGS 参考）> R3LIVE（仅移植颜色融合）**。ROS2 部署时以 FASTLIO2_ROS2 替代 FAST_LIO 主仓（同算法、原生 driver2、带 PGO/重定位/存图服务）。

---

## 6. 风险与注意事项

1. **ROS1 绑定**：三仓均为 catkin ROS1；Ubuntu 20.04/Noetic 已 EOL。方案：离线处理统一跑在 `ros:noetic` Docker；新开发走 ROS2（FASTLIO2_ROS2）；ROS1 bag <-> ROS2 bag 用 Python `rosbags` 转换，不在宿主机安装 ROS。
2. **Livox 消息版本**：MID-360/360S 只被 Livox-SDK2 / `livox_ros_driver2` 支持（driver2 已含 `MID360s_config.json`，`pub_handler.cpp` 识别 `kLivoxLidarTypeMid360s`），而 FAST_LIO 主仓订阅 `livox_ros_driver::CustomMsg`（v1）——需改包名/头文件或像 Prometheus 那样转自定义消息。`offset_time` 为 ns，FAST_LIO 转成 ms 存进 `curvature`，自研工具必须遵守同一约定。
3. **量程与安装**：MID-360 垂直视场偏上（−7°~+52°），40 m@10%；P600 倾斜安装（Prometheus Gazebo 外参约 20°）。高空航拍几乎无地面点 → LIO 退化、地图空洞。V0.5 采集规范需规定"建图航线 AGL ≤ 30–40 m，或倒装/加大俯角"，城市级外观靠视觉。
4. **内存**：`pcd_save/interval: -1` 把全部稠密点堆在内存（MID-360 约 1200 万点/分钟），Orin NX 长航时必崩。机载只存 bag，离线关键帧化。
5. **FAST-LIVO2 同步与曝光**：P600 吊舱通常非硬同步；无同步时 VIO 更新会破坏 LIO 精度。策略：机载只跑 LIO；离线着色用 §3.8 并做相机时间偏移网格搜索（±50 ms，以重投影颜色方差最小为准）。
6. **依赖地狱**：FAST-LIVO2 需特定 Sophus（非模板）+ rpg_vikit fork；R3LIVE 对 OpenCV 版本极敏感、CGAL、内存。建议只容器化 FAST-LIVO2，不部署 R3LIVE。
7. **坐标系**：LIO 世界系 = 首帧 IMU 系，未必水平、航向任意；必须由 RTK（4-DoF：yaw + t，若已重力对齐）或 PGO 给出 `T_enu_lio`，不能直接当 ENU。UrbanScene3D 也存在单位/轴向不一致（实测：Chicago 包围盒约 4.2×8×0.6，疑似千米单位；其余为米，量级 700–7700 m），ingest 时必须归一化到 ENU z-up 米制并写入 `coordinate.json`。
8. **UrbanScene3D 无颜色**：6 个 `*_sampled_5m.ply` 只有 `x y z nx ny nz`，没有 RGB/intensity。MVP 渲染需伪彩（高度、法向光照、nObs/置信度）；R3LIVE/LIVO2 的着色路线要等真机数据。
9. **本机环境**：无 GPU/ROS——这些算法都是 CPU 算法，但本地无法编译运行原仓；MVP 只做 Python/TS 移植，真管线验证放到带 ROS 的容器或实机。
10. **许可证**：FAST-LIVO2 为 GPLv2，FAST_LIO GPLv2，科研用途按用户要求忽略；若未来商用需替换。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§6 融合路线纠偏**：原文"LingBot-Map + MID-360 + RTK → Open3D ICP"把配准押在点云 ICP 上。视觉稠密点与 MID-360 稀疏点密度、噪声、覆盖差异大，直接 ICP 条件数差。建议改为"**轨迹先行**"：LIO 轨迹 +RTK → PGO 得 ENU 轨迹；LingBot-Map 相机轨迹按时间戳做 Umeyama Sim(3) 对齐（解尺度）；最后 small_gicp 点到面精配只修残差。
2. **§2/§48 补充 LIO 的真实能力边界**：MID-360 是导航级而非测绘级 LiDAR（40 m@10%，垂直视场偏上）。V0.5 的"metric 级 World"应定义为"metric 轨迹 + 低空近场几何 + 视觉外观"，并写入采集规范（航高、安装俯角、航线重叠）。
3. **§7/§41 World Package 增加 `reconstruction/lidar/<session>/` 与 `qa/`**（§4.3 契约），以关键帧 patch + 位姿保存，便于 RTK/回环重优化后重新拼图，不必重跑 LIO。
4. **§7 Geographic 增加"坐标系链"**：`lidar → imu(body) → lio_world → enu → utm/wgs84` 与 `camera → imu`，每条边带外参、时间偏移、来源（标定/估计）与不确定度。LIO 世界系是否重力对齐必须显式记录。
5. **新增"时间同步"章节**：MID-360 支持 PTP/GPS 授时（以 Livox 官方文档为准），相机需触发或至少测得时间偏移；LI-Init（LiDAR_IMU_Init）标定 LiDAR-IMU 时延与外参、FAST-Calib 标定 LiDAR-相机外参，纳入 P600 真机的"必做标定"清单。
6. **§26–28 仿真中加入"定位源"抽象**：`DroneState` 同时保存 `truth` 与 `estimate`（来源 GT / GNSS / RTK / LIO-mock），UI 画两条轨迹；V0.4 的环境→传感器影响（雾雨沙尘）通过 §3.6 模型作用到 mock LiDAR，再经 §3.4 可观性指标影响 LIO 漂移，形成"环境 → 传感器 → 定位 → 控制"的因果链，这正是本平台区别于普通模拟器的卖点。
7. **§23 Fog/LiDAR 退化给出可计算模型**：用消光系数 β 统一雾、雨、沙尘，量程 `R_max(β)`、噪声 `σ_r(β, r)`、丢点 `p_drop(β, r)`、近场假点 `p_ghost`（§3.6），而不是只写"range ↓ dropout ↑ noise ↑"。
8. **§36–37 频率表补 LIO 层**：LiDAR 10 Hz、IMU 200 Hz、LIO 里程计 10 Hz、IMU 递推 200–250 Hz、PX4 EV 融合 30–50 Hz、实时建图增量 1–2 Hz；WebSocket 通道拆分 `odom`（高频小包）与 `mapDelta`（低频二进制大包），避免大包阻塞遥测。
9. **§14–15 Web 点云补充"实时层"**：除离线八叉树瓦片外，需要一个追加式实时点层（§3.9），按瓦片合并进全局 point budget，会话结束后由离线管线重建为正式瓦片。
10. **§25/§8 Visual World 升级路径**：FAST-LIVO2 可直接导出 COLMAP 工程（位姿来自 LIO，metric 尺度），比"先 SfM 再 3DGS"更稳，建议写入 V1.0 3DGS 路线；R3LIVE 只取颜色融合思想。
11. **§42 仓库结构**：`reconstruction/lidar/` 下分 `onboard/`（Prometheus FAST_LIO 配置与 launch）、`offline/`（Docker、复算、关键帧化、PGO）、`qa/`；`sensors/lidar/` 放 mock MID-360（§3.7）与退化模型，保证仿真与真机共用同一 `CustomPoint` 语义（`offset_time` ns、`tag`、`line`）。
12. **§43 MVP 可增加一条低成本"伪 V0.5"演示链**：UrbanScene3D + 虚拟 P600 + mock MID-360 扫描 + 实时建图增量推流 + LIO 健康面板，全部 CPU / numpy 可实现，提前验证 V0.5 的数据契约与 UI，而不必等真机数据。
13. **参考清单补充**：`hku-mars/MARSIM`（基于点云地图的无人机 LiDAR 仿真，FAST_LIO 已原生支持 `lidar_type=4`）、`hku-mars/FAST-Calib`、`xuankuzcr/LIV_handhold`（同步硬件）、`vdbfusion`（FAST-LIVO2 `scripts/mesh.py` 所用 TSDF 网格化）。
