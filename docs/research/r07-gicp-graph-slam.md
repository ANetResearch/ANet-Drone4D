# r07 研究笔记：fast_gicp / small_gicp / hdl_graph_slam / GLIM（配准与图优化 → World Fusion）

> 研究单元：r07 ｜ 对应设计文档：01-design.md §6（LiDAR 与视觉融合）、§7–8（World Model）、§14–15（点云 LOD）、§33（Backend 技术栈）、§41（World Package）、§48（V0.5 Real World Fusion）
> 源码位置：`/data/projs/anet-drone/refs/lidar/{fast_gicp, small_gicp, hdl_graph_slam, glim}`；GLIM 的 GNSS 模块在独立仓库 glim_ext，已 shallow clone 到 `/data/projs/anet-drone/.cache/research/r07/glim_ext`（只读参考，未改 refs/）。
> 实测脚本与原始输出：`/data/projs/anet-drone/.cache/research/r07/{bench.py, bench2.py, bench3.py, bench_pgo.py, density.py, mock_fusion.py, *_out.*}`。运行方式：`PYTHONPATH=.cache/research/r07/pylib .venv/bin/python ...`。small_gicp 1.0.1 与 gtsam 4.3.0 用 `pip install --target` 装在研究目录，没有动项目 venv。Open3D 0.20 借用 r06 解包的 libEGL（`LD_LIBRARY_PATH=.cache/research/r06/root/usr/lib/x86_64-linux-gnu`）。
> 测试机：8 核 CPU、无 GPU，和其他研究任务共用，所以耗时数字**偏保守**，看相对量级即可。
> 约定：Sim(3) 统一写成 `x_b = s·R·x_a + t`，和 r01/r02 保持一致（COLMAP `Sim3d` 也是这个约定）。**注意 gtsam `Similarity3` 的约定不同，是 `x_b = s·(R·x_a + t)`**，见 §3.8 和 §6。

---

## 0. 结论速览

| 仓库 | stars / 最后提交 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|---|
| koide3/small_gicp | 1045 / **2026-08-31**（新增 Colored ICP） | header-only C++17 精配准库：ICP / 点到面 / GICP / VGICP / Colored-ICP，预处理（体素降采样、KdTree、协方差）全部并行。**pip 有 manylinux x86_64 / aarch64 wheel（1.0.1）**，只依赖 Eigen | **adopt**：World Fusion 服务的核心算子（预处理、刚体 GICP/VGICP、KdTree 近邻、GaussianVoxelMap）；MVP 离线切片器的体素降采样和密度估计。**port**：在它的 KdTree 和协方差之上加 7 自由度的 Sim(3)-GICP（我们自己的扩展） | MVP（切片 / 密度 / Fusion 演示任务）→ V0.5（生产） | 5/5 |
| koide3/glim | 1848 / **2026-09-06**（v1.2.x，GTSAM 4.3a0，CUDA 13.1，Jetson Orin） | 基于因子图的 range-inertial 建图框架：子图 + 全局「配准误差因子」+ iSAM2，带 GNSS 扩展、多航次合并和地图编辑器 | **reference**（架构、子图/重叠判定、自适应体素、GNSS 4-DoF 初始化、dump 格式）；V0.5 可以在 GPU 服务器或 Jetson 上把 GLIM（Docker `koide3/glim_ros2`）当作**离线 LiDAR 建图工具**直接用 | V0.5 | 4/5 |
| koide3/hdl_graph_slam | 2339 / 2024-07-16（ROS1，维护结束，作者推荐转 GLIM） | 经典 LiDAR 图优化 SLAM：scan-to-keyframe 里程计 + g2o 位姿图 + GPS / IMU / 地面平面约束 + 回环 | **port**（只移植思路和公式：GPS 先验边、关键帧与 GNSS 按时间关联、fitness→信息矩阵、回环候选规则、地面平面边），不部署 | V0.5 | 3/5 |
| koide3/fast_gicp | 1700 / 2025-04-24（README 已声明被 small_gicp 取代） | PCL 接口的 GICP / VGICP，带 CUDA 版 VGICP 和 D2D-NDT | **skip**（CPU 场景 small_gicp 全面更好）；只在 V1.0 需要 **GPU 实时**配准时回看 `FastVGICPCuda` / `NDTCuda` | — | 2/5 |

**一句话结论**

1. **World Fusion 的核心不是 ICP，而是尺度。** 实测（Shenzhen 5M 点，300 m×300 m 航区）：视觉点云只要带 1% 的尺度误差，刚体 GICP/VGICP 就会留下约 1 m 平移误差，而且无法自己修正。"刚体 GICP + 最近邻 Umeyama 重估尺度"交替迭代收敛极慢（1% → 0.955%）。只有把尺度并进优化变量（**7-DoF Sim(3)-GICP**）才能一步到位：1% / 3% / 5% 的尺度误差都收敛到 **0.001% 尺度、0.001° 旋转、1 mm 平移**。
2. **鲁棒核是 Sim(3)-GICP 的必要条件。** 初值 3%·5°·5 m 时，纯 L2 会停在 3.45° / 6.4 m；加 Huber(δ=3，作用在马氏距离上）后收敛到 1 mm。10% 尺度或 15° 偏航超出了 [2,1,0.5] m 金字塔的收敛域，必须先用轨迹做 Sim(3)（RTK 下 p95 尺度误差 0.0024%，SPP GNSS 下 0.25%），或者用 FPFH+RANSAC 做全局初值。
3. **推荐流水线：** 轨迹级 RANSAC-Umeyama Sim(3)（r01/r02 已给出）→ 点云级多尺度 Sim(3)-GICP（small_gicp 做预处理和近邻，7×7 LM）→ GNSS 先验 + 配准边的联合因子图（gtsam 4.3 pip，**两阶段鲁棒**：先 Huber，再用 χ² 门限剔除异常，最后 L2 重解）→ 融合 → QA。
4. **GNSS 进图的正确姿势**（hdl 和 GLIM 都做得不够）：本地 ENU（不要用 UTM 网格距离）、杆臂补偿、按 fix 类型给协方差、**信息矩阵取 1/σ²**（hdl 写成了 1/σ）、两阶段鲁棒。实测 240 关键帧的漂移轨迹（航位推算 ATE 9.47 m）：RTK 下 3.5 cm；RTK 混入 5% 多路径跳点时纯 L2 被拉坏到 **4.37 m**，两阶段法在 15% 异常率下仍保持 3.8 cm；SPP 约 0.7–0.8 m。
5. **对 MVP 直接有用的三件事：** ① small_gicp 的 21 bit×3 体素键 + 排序降采样处理 5M 点只要 0.8 s（8 线程），Open3D `voxel_down_sample` 要 14.7 s，**快 18 倍**，适合做八叉树 LOD 的逐层采样算子；② 用 KdTree 估中位点距离，得到每个场景 / 节点的 `spacing`，前端据此自动定点大小和 LOD 细分阈值。**6 个内置城市的点距差 1000 倍（0.002–1.86 m）**，硬编码参数一定翻车（Chicago 疑似以 km 为单位、Suzhou 是 Y-up，与 r02/r06 的发现一致，本文独立复核）；③ 用内置点云合成一个"视觉重建"，跑一次真实的 Fusion 任务（CPU、秒级、WS 推进度），在 UI 里完整演示 Reality→World 链路。

---

## 1. 仓库概览

### 1.1 small_gicp（koide3，MIT）

- **定位**：fast_gicp 的重写版（作者：AIST 的 Kenji Koide），JOSS 2024 论文。README 自述单线程 GICP 比 fast_gicp 快约 1.9 倍，比 PCL GICP 快约 2.4 倍，多线程扩展性更好，输出和 fast_gicp 基本一致。
- **依赖**：只强制依赖 Eigen；nanoflann 和 Sophus 已内置（`include/small_gicp/ann/kdtree.hpp`、`util/lie.hpp`）。OpenMP / TBB / PCL 可选。**没有 GPU 实现**。
- **构建**：header-only。`CMakeLists.txt` 可选编 helper 库（`src/small_gicp/registration/registration_helper.cpp`）、Python 绑定（pybind11 + scikit-build-core，`pyproject.toml`）、benchmark、test。CI 在 Linux、macOS、Windows 上用 cibuildwheel 出 wheel（manylinux_2_28 x86_64 和 aarch64，也就是说 **Jetson Orin 上可以直接 `pip install`**，已用 `pip download --platform manylinux_2_28_aarch64` 验证）。
- **2026 年动态**：最后一次提交 2026-08-31，内容是 "Add colored icp variant (#137)"，新增 `factors/colored_icp_factor.hpp` 和 `util/color_gradient*.hpp`（Park 2017 Colored ICP，和 Open3D 的实现等价）。**PyPI 上的 1.0.1 还没有这个功能**：实测 `small_gicp.align(..., registration_type='COLORED_ICP')` 不可用，也没有 `estimate_color_gradients`，要用得从源码 `pip install .`。
- **Python API**（`src/python/*.cpp`，已在 1.0.1 wheel 上逐个验证）：`PointCloud`、`KdTree(points, num_threads)`（`nearest_neighbor_search`、`knn_search`、`batch_nearest_neighbor_search`、`batch_knn_search`）、`GaussianVoxelMap(leaf)`（`insert(points, T)`、`set_lru`、`voxel_points/normals/covs`）、`IncrementalVoxelMap{,Normal,Cov,NormalCov}`、`voxelgrid_sampling`、`estimate_{normals,covariances,normals_covariances}`、`preprocess_points`、`align`（3 个重载：numpy / 预处理后的点云 + KdTree / GaussianVoxelMap）、`ICPFactor`、`PointToPlaneICPFactor`、`GICPFactor`（`.linearize()` 可以逐点拿到 H、b、e）、`DistanceRejector`、`RegistrationResult`（`T_target_source`、`converged`、`iterations`、`num_inliers`、`H`、`b`、`error`）、`read_ply`（仅供测试）。

### 1.2 fast_gicp（SMRT-AIST / koide3，BSD）

- **定位**：GICP 家族的 PCL 插件式实现，提供 `FastGICP`（多线程）、`FastGICPSingleThread`、`FastVGICP`、`FastVGICPCuda`、`NDTCuda`，都继承 `LsqRegistration`（最终是 `pcl::Registration`）。
- **依赖**：PCL、Eigen、OpenMP，CUDA 可选（`thirdparty/nvbio` 做 GPU 暴力 kNN，`thirdparty/Sophus`）。ROS1 用 catkin，也可以纯 CMake 构建。Python 绑定 `pygicp` 走 `setup.py`，需要 PCL，不能 pip 直装。
- **状态**：最后提交 2025-04-24，只改了 issue 模板。README 第一行就写着 "New faster library is released → small_gicp"。hdl_graph_slam 的 `registration_method=FAST_GICP / FAST_VGICP` 依赖它。
- **仍有价值的部分**：`FastVGICPCuda`（README 称约 120 FPS）和 `NDTCuda`（约 500 FPS）是这 4 个仓库里仅有的 GPU 配准实现；VGICP 体素的三种累积模式（`fast_vgicp_voxel.hpp`）也值得参考。

### 1.3 hdl_graph_slam（koide3，BSD-2）

- **定位**：2018 年的经典 3D LiDAR 图优化 SLAM（ROS1 nodelet）。4 个 nodelet：`prefiltering` → `scan_matching_odometry` → `floor_detection` → `hdl_graph_slam`（后端）。
- **依赖**：ROS1（kinetic、melodic、noetic）、PCL、g2o（`cmake/FindG2O.cmake`）、`fast_gicp`、`ndt_omp`、`geodesy`（UTM 转换）、`nmea_msgs`，可选 `msf_updates`。Docker 文件只有 ROS1。
- **状态**：最后提交 2024-07-16（只改 README），README 推荐迁移到 GLIM。**不部署**，但它的 GPS 边和信息矩阵等实现短小清晰，是学习"怎么把 RTK 钉进点云图"的最佳入门材料。

### 1.4 GLIM（koide3，MIT；glim_ext 各模块许可不同）

- **定位**：通用 range-inertial 建图框架（RAS 2024）。核心思想是**直接在因子图里最小化多帧、多子图之间的配准误差**（配准误差因子，由 gtsam_points 实现），而不是先配准再把相对位姿当边。支持旋转式 LiDAR、Livox MID-360 这类非重复扫描、固态 LiDAR 和 RGB-D。
- **依赖**：Eigen、nanoflann、**GTSAM 4.3a0**、**gtsam_points 1.2**（作者自己的点云因子库）；可选 CUDA（12.2 / 12.6 / 13.1）、OpenCV、OpenMP、ROS1/ROS2、Iridescence（可视化）。README 称已在 Jetson Orin（Jetpack 6.1）上测试。
- **2026 年动态**：v1.2.0（2026-01，兼容 GTSAM 4.2a9 / 4.3a0，支持 CUDA 13.1 和强度可视化）、v1.2.1（2026-02，背面剔除）、v1.2.2（2026-07，修 GTSAM 4.3a1 编译错误）；HEAD 为 2026-09-06 的 "Cov (#324)"（shallow clone 看不到 diff；从提交名看，改动应与协方差估计有关。当前 `common/cloud_covariance_estimation.cpp` 提供 PLANE / NORMALIZED_MIN_EIG / FROBENIUS 三种正则，以及可选的高斯核邻域加权：`w=exp(−d²/2r²)+offset`）。glim_ext 最后提交 2026-08-31。
- **模块**（运行时按 `config/*.json` 里的 `so_name` 动态加载，见 `util/load_module.hpp`）：
  - 里程计：`odometry_estimation_{cpu,gpu,ct,imu}`（CPU 版是 GICP + iVox；GPU 版是多分辨率 VGICP；CT 是无 IMU 的连续时间版本）。
  - 子图：`sub_mapping`（按重叠度选关键帧，再合成子图）、`sub_mapping_passthrough`。
  - 全局：`global_mapping`（配准误差因子 + iSAM2，可选 GPU）、`global_mapping_pose_graph`（CPU 轻量版：回环候选 + 位姿图）。
  - 扩展：`GlobalMappingCallbacks::on_insert_submap / on_smoother_update` 等全局回调槽（`include/glim/mapping/callbacks.hpp`），glim_ext 的 GNSS、ScanContext、DBoW、flat_earther 都是通过回调往因子图里注入约束。
  - 工具：offline_viewer（多航次合并、手动回环、Bundle Adjustment 弹窗）、map_editor（点选删除）。

---

## 2. 源码结构与关键模块

### 2.1 small_gicp

| 路径（相对仓库根） | 关键类 / 函数 | 作用 |
|---|---|---|
| `include/small_gicp/registration/registration.hpp` | `template<PointFactor, Reduction, GeneralFactor=NullFactor, CorrespondenceRejector=DistanceRejector, Optimizer=LevenbergMarquardtOptimizer> struct Registration::align(target, source, target_tree, init_T)` | 策略模板：逐点因子、并行归约、全局附加因子、剔除器、优化器，五个维度都能单独替换 |
| `registration/optimizer.hpp` | `GaussNewtonOptimizer`（`lambda=1e-6`）、`LevenbergMarquardtOptimizer`（`max_iterations=20, max_inner_iterations=10, init_lambda=1e-3, lambda_factor=10`） | LM：线性化 → `(H+λI).ldlt().solve(-b)` → `T←T·se3_exp(δ)`（**右乘扰动**）→ 误差下降则 `λ/=10`、否则 `λ*=10` |
| `registration/termination_criteria.hpp` | `TerminationCriteria{translation_eps=1e-3, rotation_eps=0.1°}` | 收敛判据：`‖δ_rot‖≤0.1° && ‖δ_trans‖≤1 mm` |
| `registration/rejector.hpp` | `DistanceRejector{max_dist_sq=1.0}`、`NullRejector` | 最近邻距离门限（**默认 1 m**，大尺度航测必须调） |
| `registration/reduction_omp.hpp` / `_tbb.hpp` | `ParallelReductionOMP::linearize()` | 每线程各自累加 6×6 的 H、6 维 b 和误差 e，最后求和；`schedule(guided, 8)` |
| `factors/gicp_factor.hpp` | `GICPFactor::linearize()` | 分布到分布残差，公式见 §3.1 |
| `factors/icp_factor.hpp`、`plane_icp_factor.hpp`、`colored_icp_factor.hpp`（2026 新增） | 点到点 / 点到面 / Colored-ICP（`lambda_geometric=0.968`，光度项权重 0.032） | 可替换的逐点因子 |
| `factors/robust_kernel.hpp` | `Huber{c=1}`、`Cauchy{c=1}`、`RobustFactor<Kernel,Factor>` | 用 `w(√e)` 缩放 H、b、e（IRLS） |
| `factors/general_factor.hpp` | `RestrictDoFFactor`（`lambda=1e9`，按轴掩码冻结 rx..tz） | **适合只估 4-DoF（xyz+yaw）的场景**：LIO 已重力对齐时，把 roll 和 pitch 锁住 |
| `ann/kdtree.hpp`、`kdtree_omp.hpp` | `KdTree`、`UnsafeKdTree`、`KdTreeBuilderOMP` | nanoflann 派生，支持并行构建 |
| `ann/incremental_voxelmap.hpp` | `IncrementalVoxelMap<VoxelContents>`：`insert(points, T)`、LRU（`lru_horizon=100, lru_clear_cycle=10`）、`set_search_offsets(1/7/27)`；全局索引 = `voxel_id<<32 \| point_id` | 增量哈希体素，可以当点云，也可以当近邻结构 |
| `ann/gaussian_voxelmap.hpp` | `GaussianVoxel`（累加均值和 `T·C·Tᵀ`，`finalize()` 时除以 N）、`GaussianVoxelMap` | VGICP 的目标地图；**可增量插入 + LRU 淘汰 = scan-to-model 地图** |
| `ann/flat_container.hpp` | `FlatContainer{max_num_points_in_cell=10, min_sq_dist_in_cell=0.01}` | iVox 风格：每格最多 10 点、点间距至少 0.1 m |
| `util/downsampling.hpp`、`_omp.hpp`、`_tbb.hpp` | `voxelgrid_sampling(points, leaf)`：每轴 21 bit 量化后打包成 uint64，排序后按段求质心；OMP 版先并行 `quick_sort_omp`，再按 1024 点一块并行求和 | 精确体素质心（跨块的格子会多出一个点，这就是文档里说的"轻微非确定性"）；坐标范围 ±2^20·leaf |
| `util/normal_estimation.hpp`、`_omp.hpp` | `estimate_local_features<Setter>`：kNN（默认 k=20）→ 协方差 → `SelfAdjointEigenSolver::computeDirect`；`CovarianceSetter` 把特征值替换成 `(1e-3, 1, 1)`；邻居少于 5 个时写单位阵（法向为 0） | **平面化正则**（GICP 原论文的做法），和 fast_gicp 的 `RegularizationMethod::PLANE` 等价 |
| `util/lie.hpp` | `skew`、`so3_exp`、`se3_exp(a)`（`a=[ω;v]`，**旋转在前**） | 李代数工具 |
| `registration/registration_helper.hpp/.cpp` | `preprocess_points`、`create_gaussian_voxelmap`、`RegistrationSetting{type, voxel_resolution=1, downsampling_resolution=0.25, max_correspondence_distance=1, rotation_eps, translation_eps, num_threads=4, max_iterations=20}`、`align(...)` | Python 绑定用的高层接口；**VGICP 必须传 `GaussianVoxelMap` 重载** |
| `src/example/kitti_odometry.py` | `ScanToScanMatchingOdometry`、`ScanToModelMatchingOdometry`（`GaussianVoxelMap(1.0)` + `set_lru(100,10)`） | 两种里程计范式的 Python 模板，mock LiDAR 定位可以直接照抄 |

**要点：**
- `Registration` 的 H 固定是 6×6，扰动顺序 `[ω, v]`、右乘 `T·exp(δ)`，**和 gtsam `Pose3` 的切空间约定完全一致**（gtsam `Pose3::Retract` 也是右乘、旋转在前）。所以 `result.H` 可以直接当 gtsam `BetweenFactorPose3` 的信息矩阵用（GLIM `create_between_factors` 正是这么做的，要先按 §3.5 缩放）。
- 7-DoF 的 Sim(3) 没法直接塞进这个模板：`Factor::linearize` 的签名写死了 6×6。扩展办法见 §3.3。
- Python 的 `align(target, source, target_tree, ...)` 只接受 ICP / PLANE_ICP / GICP（以及源码版的 COLORED_ICP）；VGICP 要用 `align(GaussianVoxelMap, PointCloud, ...)`。

### 2.2 fast_gicp

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `include/fast_gicp/gicp/lsq_registration.hpp`、`impl/lsq_registration_impl.hpp` | `LsqRegistration::step_lm`：`λ₀ = factor·max(abs(diag H))`；增益比 `ρ=(y0−yi)/(dᵀ(λd−b))`；成功时 `λ *= max(1/3, 1−(2ρ−1)³)`，失败时 `λ *= ν、ν *= 2`（Nielsen 策略） | **比 small_gicp 的 ×10/÷10 更平滑**，Sim(3)-GICP 的 LM 可以借用 |
| `gicp/impl/fast_gicp_impl.hpp` | `calculate_covariances`：`RegularizationMethod{NONE, MIN_EIG, NORMALIZED_MIN_EIG, PLANE(默认), FROBENIUS}`；`update_correspondences` 用 `(C_B + T C_A Tᵀ)⁻¹`；`linearize` 用**左乘扰动** `J=[skew(Tp), −I]` | 视觉点云噪声各向异性很强时，`NORMALIZED_MIN_EIG` 保留真实形状，比强行平面化更稳，可以作为可选项 |
| `gicp/fast_vgicp_voxel.hpp` | `GaussianVoxelMap`：`VoxelAccumulationMode{ADDITIVE, ADDITIVE_WEIGHTED, MULTIPLICATIVE}`；`neighbor_offsets(DIRECT1/7/27)` | MULTIPLICATIVE 是信息形式融合：`Σ⁻¹=ΣCᵢ⁻¹`，`μ=Σ·ΣCᵢ⁻¹μᵢ` |
| `gicp/impl/fast_vgicp_impl.hpp` | 每个源点对邻域内所有体素建立对应，权重 `w=√N_voxel` | small_gicp 的 VGICP 只取最近的一个体素、不加权，更简洁 |
| `cuda/*.cu` | `FastVGICPCuda`（邻域方法：DIRECT1/7/27/RADIUS、`gpu_rbf_kernel`）、`NDTCuda`（D2D） | GPU 实时配准的唯一参考 |
| `src/python/main.cpp` | `pygicp.align_points(target, source, method=..., ...)`，method 取 GICP / VGICP / VGICP_CUDA / NDT_CUDA；`downsample` 用的是 `pcl::ApproximateVoxelGrid` | ApproximateVoxelGrid 会多出最多 2 倍的伪点（small_gicp README 的对比结论） |

### 2.3 hdl_graph_slam

```
apps/prefiltering_nodelet.cpp        距离裁剪 + VOXELGRID + RADIUS/STATISTICAL 去噪
apps/scan_matching_odometry_nodelet.cpp   scan-to-keyframe：关键帧门限代码默认 0.25 m / 0.15 rad / 1 s（launch_400 改为 1.0 m / 1.0 rad）；
                                          transform_thresholding（max_acceptable_trans / angle）防跳变
apps/floor_detection_nodelet.cpp     高度裁剪 [h−1, h+1] m + 法向过滤（20°）+ RANSAC 平面，内点 ≥ 512
apps/hdl_graph_slam_nodelet.cpp      后端：关键帧队列、GPS/IMU/地面队列、回环、g2o 优化、地图发布、save_map/dump
include/hdl_graph_slam/
  keyframe_updater.hpp               关键帧门限 2 m / 2 rad，累计里程 accum_distance
  loop_detector.hpp                  候选：累计里程差 ≥ accum_distance_thresh 且 XY 距离 ≤ distance_thresh
                                     且距上次回环 ≥ min_edge_interval（代码默认 8/5/5 m，launch_400 为 25/15/15 m）；对候选逐一配准，取 fitness 最小者；
                                     fitness ≤ fitness_score_thresh（代码默认 0.5，launch_400 为 2.5）才接受；初值 guess(2,3)=0（去掉 z）
  information_matrix_calculator.hpp  fitness → 方差映射（公式见 §3.5）
  graph_slam.hpp / .cpp              g2o 封装：add_se3_node / add_se3_edge / add_se3_prior_{xy,xyz,quat,vec}_edge /
                                     add_plane_node / add_se3_plane_edge / add_robust_kernel；求解器 lm_var_cholmod
include/g2o/edge_se3_prior{xy,xyz,quat,vec}.hpp   一元先验边（GPS 用 XY/XYZ，IMU 姿态用 quat，重力方向用 vec）
include/g2o/edge_se3_plane.hpp, edge_plane_*.hpp  地面平面边
```

**GPS 流程**（`hdl_graph_slam_nodelet.cpp::flush_gps_queue`，第 290–358 行）：
1. 三个入口 `/gps/geopoint`、`/gpsimu_driver/nmea_sentence`（GPRMC，没有高度→`NAN`）、`/gps/navsat` 统一转成 `GeoPointStamped`，时间戳加上 `gps_time_offset`。
2. 对每个关键帧，在队列里找时间**最近**的 GPS 数据；`|Δt|>0.2 s` 就跳过（**不插值**）。
3. `geodesy::fromMsg → UTMPoint`，第一个 GPS 点作为 `zero_utm` 原点，后面都减去它。
4. 高度是 NaN 时加 `EdgeSE3PriorXY`，否则加 `EdgeSE3PriorXYZ`。信息矩阵写成 `I / gps_edge_stddev_xy`、`(2,2) /= gps_edge_stddev_z`，**实际是 1/σ 而不是 1/σ²**（launch 默认 σxy=20、σz=5，只是弱约束，所以问题不明显）。可选鲁棒核，默认 NONE。
5. 首节点锚定：`fix_first_node_stddev="10 10 10 1 1 1"`，`fix_first_node_adaptive=true`（锚点每轮跟着首节点移动，不跟 GPS 冲突）。
6. `save_map` 支持输出 UTM 坐标（`req.utm` 时每个点加回 `zero_utm`），另外写一个 `.utm` 原点文件。

IMU 约束：`EdgeSE3PriorQuat`（姿态）和 `EdgeSE3PriorVec`（用加速度方向约束重力 −Z）。地面：固定的平面节点 `(0,0,1,0)` + 每个关键帧一条 `EdgeSE3Plane`。

### 2.4 GLIM

**数据流**（`src/glim/...`）：

```
preprocess/cloud_preprocessor.cpp   距离裁剪 → 随机格网降采样（默认目标 10000 点）→ 可选离群点去除 → kNN(k=10)
odometry/odometry_estimation_cpu.cpp   GICP + iVox（ivox_resolution=1.0、min_dist=0.1、LRU=100），固定滞后平滑器（smoother_lag=5 s）+ IMU 预积分
odometry/odometry_estimation_gpu.cpp   多分辨率 VGICP（GPU）
mapping/sub_mapping.cpp          关键帧策略 OVERLAP（max_keyframe_overlap=0.6），每 15 个关键帧合成一个子图（下采样 0.3 m）
mapping/global_mapping.cpp       子图节点 X(i)；
   insert_submap():  由上一子图估计 + 端点间里程计推出新子图初值
                     → create_between_factors（GICP，max_corr=0.5，H+1e6·I 当信息矩阵）
                     → create_matching_cost_factors：对 max_implicit_loop_distance(100 m) 以内、
                        overlap_auto ≥ min_implicit_loop_overlap(0.2) 的历史子图，逐个加 IntegratedVGICPFactor(X(i), X(cur))
                        （这就是"隐式回环"：不需要回环检测，重叠就是约束）
                     → 如果和上一子图的重叠 < max(0.25, min_overlap)，补一条强 BetweenFactor，防止子图孤立
                     → IMU：子图两端 E/V/B 变量 + ImuFactor
                     → Callbacks::on_smoother_update（扩展模块在这里注入 GNSS 等因子）→ iSAM2
   insert_submap(current, submap):  自适应体素分辨率（公式见 §3.4）+ 多级 GaussianVoxelMap（levels、scaling_factor=2）
   save(): graph.bin / values.bin（GTSAM 序列化，配准误差因子不序列化，只记录在 graph.txt 里）、
           odom_lidar.txt / traj_lidar.txt / odom_imu.txt / traj_imu.txt（TUM 格式）、每个子图一个目录
mapping/global_mapping_pose_graph.cpp   轻量版：min_travel_dist=50 m、max_neighbor_dist=5 m 生成候选，
                     后台线程每轮评估 loop_candidate_eval_per_thread×num_threads 个候选，inlier_fraction ≥ 0.5 才接受，
                     loop_factor_stddev=0.1、robust_width=1.0，odom_factor_stddev=1e-3
```

**glim_ext/modules/mapping/gnss_global**（`include/glim_ext/gnss_global_module.hpp`，**作者自注 "very naive"**）：
1. 订阅 `PoseWithCovarianceStamped`（**要求上游已经把经纬度转成平面坐标**，协方差字段读了但完全没用）。
2. 后台线程：用子图中间帧的时间，对前后两个 GNSS 点做**线性插值**（比 hdl 的最近邻更好）。
3. 当首尾子图间距超过 `min_baseline`（10 m）时，初始化 `T_world_utm`：对子图原点和 GNSS 点做 **2D（只算 yaw）SVD / Umeyama**，平移取均值差。这隐含假设 LIO 世界系已经重力对齐，所以只需 4-DoF。
4. 之后每个新子图加一个 `gtsam::PoseTranslationPrior<Pose3>(X(id), T_world_utm·p_gnss, Isotropic::Information(diag(1e3,1e3,0)))`，**高度信息是 0，也就是完全不约束 z**；没有杆臂、没有鲁棒核。
5. `geodetic.cpp` 提供 `wgs84_to_ecef`、`ecef_to_wgs84`（Zhu 1994 闭式解）、`calc_T_ecef_nwz`（ECEF→局部北西天系）。`harversine()` 把度当弧度用了（小 bug，这个模块没有调用它）。

**flat_earther/LevelFactor**：`residual = z_i − z_j`（两个位姿的高度差），适合地面车辆防 z 漂移；无人机一般不需要，但**多航次拼接时，同一地面区域的高度一致性**可以借这个思路。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 GICP：统一记号与 small_gicp 实现要点

源点 `p_i`（协方差 `C_i^S`），目标最近邻 `q_i`（协方差 `C_i^T`），当前位姿 `T=(R,t)`：

```
r_i  = q_i − (R p_i + t)                                   # 残差（3 维，small_gicp 用齐次 4 维）
M_i  = (C_i^T + R C_i^S Rᵀ)⁻¹                              # 融合协方差的逆（GICP 的核心）
e    = ½ Σ_i r_iᵀ M_i r_i
J_i  = [ R·[p_i]×  ,  −R ]      (3×6, 右乘扰动 T←T·Exp([ω;v]))  # small_gicp gicp_factor.hpp
H    = Σ J_iᵀ M_i J_i ,  b = Σ J_iᵀ M_i r_i
δ    = −(H + λI)⁻¹ b ;  T ← T·se3_exp(δ)                   # LM：误差下降则 λ/=10，否则 λ*=10（最多 10 次内循环）
```

协方差估计（`normal_estimation.hpp::CovarianceSetter`）：kNN（k=10–20）求样本协方差，特征分解后把特征值替换成 `(1e-3, 1, 1)`（最小特征值方向即法向），也就是"平面化"。这让 GICP 在结构化场景里近似"平面到平面"：沿表面方向的误差几乎不受罚，所以**对两份点云采样模式不同**（视觉的稠密立面和 LiDAR 的环形扫描线）不敏感。这正是跨模态配准选 GICP 而不是点到点 ICP 的原因。

**参数经验值（本项目航测尺度）**：
| 参数 | small_gicp 默认 | 建议（以地图中位点距 `s` 为单位，见 §3.4） |
|---|---|---|
| `downsampling_resolution` | 0.25 m | 每层金字塔 `res_L ∈ {8s, 4s, 2s, 1s}`，最细一层不小于 `s` |
| `num_neighbors` | 10（`preprocess_points`）/ 20 | 20（视觉点噪声大时用 30） |
| `max_correspondence_distance` | 1.0 m | `3·res_L`（粗层可放宽到 `4·res_L`） |
| `max_iterations` | 20 | 粗层 30，细层 10–20 |
| `rotation_eps / translation_eps` | 0.1° / 1 mm | 保持默认 |
| `num_threads` | 1（Python 默认！） | **必须显式传** `num_threads=os.cpu_count()` |

### 3.2 VGICP（体素化 GICP）

目标端不再存每个点，而是存高斯体素 `(μ_v, Σ_v)`：`GaussianVoxel::add` 累加 `μ += T·p`、`Σ += T·C·Tᵀ`，`finalize` 时除以 N。源点变换后落到哪个格子（DIRECT1，或 DIRECT7/27 邻域）就和哪个格子的高斯配对，**省掉了 kNN 搜索**。

```python
vm = small_gicp.GaussianVoxelMap(2*res)          # 体素边长取点距的 2–4 倍，保证每格 ≥ 5 点
vm.insert(target_pc)                           # target_pc 需先 estimate_covariances
res = small_gicp.align(vm, source_pc, T_init, max_correspondence_distance=3*res, num_threads=8)
```

实测（0.5 m 层，源 95.7k 点、目标 416.6k 点，8 线程，同一初值）：**small_gicp VGICP 171 ms**（建体素图 407 ms，只需一次），small_gicp GICP 872 ms（单线程 3218 ms），Open3D 0.20 legacy 点到面 ICP 585 ms、GICP 683 ms（另需估法向 1027 ms），fitness 都是 0.971。也就是说，**small_gicp 的优势在预处理（快 18 倍）和 VGICP（比 Open3D 点到面快约 3.4 倍）**；单层刚体 GICP 的迭代速度和 Open3D 相当，不是选型理由。选它的真正理由是：预处理全并行、GaussianVoxelMap 可增量、可以逐点取到 H 和 b（Sim(3) 扩展和不确定度都靠这一点），以及 pip 包只有 378 KB。但 VGICP 的收敛域更窄：初值 1%·3°·10 m 时，GICP 收敛到 1.1 m，VGICP 停在 7.7 m（见 §3.3 表）。**建议：粗层用 GICP（收敛域大），细层用 VGICP（快）**；如果目标地图会被反复配准（多航次、多 chunk），VGICP 的体素图可以缓存。

### 3.3 Sim(3)-GICP（7-DoF，本项目扩展，已验证）

**动机**：视觉重建（LingBot-Map、VGGT、GLOMAP）没有度量尺度（r01/r02 已证明）。轨迹级 Umeyama 虽然能给出尺度，但精度受 GNSS 质量、轨迹几何和视觉漂移限制。点云级精配准需要把尺度一起优化。

**实测证据：刚体 GICP 无法吸收尺度误差**（`bench2.py`，Shenzhen ROI，视觉点云模拟：35% 采样、8 cm 噪声、3% 漂浮噪点）：

| 初值扰动（尺度 / 旋转 / 平移） | 刚体 GICP 金字塔 [2,1,0.5] m | + 最近邻 Umeyama 重估尺度 ×2 轮 | 刚体 VGICP |
|---|---|---|---|
| 0.2% / 0.5° / 0.3 m（RTK 级） | 0.2% / 0.028° / 0.117 m | **0.004% / 0.001° / 3 mm** | 0.2% / 0.027° / 0.119 m |
| 1% / 2° / 2 m（GNSS 级） | 1.0% / 0.157° / 1.12 m | 0.955% / 0.152° / 1.06 m（几乎没改善） | 1.0% / 0.456° / 0.38 m |
| 3% / 5° / 5 m | 3.0% / 3.0° / 8.9 m（失败） | 失败 | 失败 |
| 5% / 2° / 2 m | 5.0% / 0.2° / 6.7 m | 失败 | 失败 |

最近邻 Umeyama 重估尺度收敛慢的原因：最近邻对应在曲面上会"就近滑动"，它总是偏向当前估计，每轮只能修正一小部分尺度（经典的 scaled-ICP 偏置问题）。

**做法**：状态 `S=(s,R,t)`，左乘扰动 `S ← Exp([ω, v, σ])·S`，扰动后的点 `p' = (1+σ)(I+[ω]×)p + v`（p 为当前已变换的源点）：

```
∂p'/∂[ω, v, σ] = [ −[p]× ,  I ,  p ]                    # 3×7
r_i = q_i − p_i ,   J_i = ∂r/∂x = [ [p_i]× , −I , −p_i ]
M_i = (C_i^T + s² R C_i^S Rᵀ)⁻¹                          # 源协方差按 s² 放大
w_i = Huber(√(r_iᵀ M_i r_i); δ=3)                        # IRLS 权重，必须加
H = Σ w_i J_iᵀ M_i J_i (7×7),  g = Σ w_i J_iᵀ M_i r_i
Δ = −(H + λ·diag(H))⁻¹ g                                 # Marquardt 缩放阻尼，λ=1e-4
S ← [ e^{Δσ}·Exp(Δω) , Δv ] · S
收敛：‖Δω‖<1e-5 且 ‖Δv‖<1e-4 且 |Δσ|<1e-6
```

参考实现（numpy 向量化，近邻和协方差复用 small_gicp，`bench3.py::sim3_gicp`）：

```python
def sim3_gicp(tgt_pc, tgt_tree, src_xyz, src_covs, S, max_corr, iters=30, huber=3.0):
    tp, tcov = tgt_pc.points()[:, :3], np.asarray(tgt_pc.covs())[:, :3, :3]
    for _ in range(iters):
        s = np.cbrt(np.linalg.det(S[:3, :3])); R = S[:3, :3] / s
        p = src_xyz @ S[:3, :3].T + S[:3, 3]
        idx, sq = map(np.asarray, tgt_tree.batch_nearest_neighbor_search(p, num_threads=8))
        m = sq < max_corr**2
        p, q = p[m], tp[idx[m]]
        M = np.linalg.inv(tcov[idx[m]] + s*s * (R @ src_covs[m] @ R.T) + 1e-6*np.eye(3))
        e = q - p
        J = np.concatenate([skew(p), -np.broadcast_to(np.eye(3), (len(p), 3, 3)), -p[:, :, None]], 2)
        Me = (M @ e[..., None])[..., 0]; chi = (e * Me).sum(1)
        w = np.where(np.sqrt(chi) < huber, 1.0, huber / np.sqrt(chi))
        Jt = J.transpose(0, 2, 1)
        H = ((w[:, None, None] * Jt) @ (M @ J)).sum(0)
        g = (w[:, None] * (Jt @ Me[..., None])[..., 0]).sum(0)
        d = np.linalg.solve(H + 1e-4 * np.diag(np.diag(H)), -g)
        D = np.eye(4); D[:3, :3] = np.exp(d[6]) * so3_exp(d[:3]); D[:3, 3] = d[3:6]
        S = D @ S
        if converged(d): break
    return S
```

**实测**（`bench3.py`，同一数据，金字塔 [2,1,0.5] m / max_corr [6,3,1.5] m）：

| 初值扰动 | Sim(3)-GICP 纯 L2 | Sim(3)-GICP + Huber(3) | 各层迭代次数（Huber） |
|---|---|---|---|
| 1% / 2° / 2 m | 0.0012% / 0.0007° / 1.0 mm | **0.0013% / 0.0006° / 1.1 mm** | 12 / 3 / 2 |
| 3% / 5° / 5 m | 0.10% / **3.45°** / 6.4 m（失败） | **0.0011% / 0.0008° / 1.0 mm** | 30 / 18 / 2 |
| 5% / 2° / 2 m | 0.08% / 3.45° / 6.4 m（失败） | **0.0011% / 0.0006° / 1.0 mm** | 17 / 3 / 2 |
| 10% / 3° / 3 m | 10.3% / 4.7° / 6.2 m | 0.04% / 2.38° / 4.5 m（失败） | 30 / 25 / 17 |
| 1% / 15° 偏航 / 3 m | 失败 | 失败（14.3°） | 30 / 30 / 30 |
| 1% / 3° / 10 m | 0.0011% / 0.0008° / 1.2 mm | **0.0013% / 0.0007° / 1.0 mm** | 24 / 3 / 2 |

结论与参数：
- **收敛域**（金字塔最粗 2 m / max_corr 6 m 时）：尺度 ≤5%、旋转 ≤5°、平移 ≤10 m。SPP GNSS 轨迹 Umeyama 的 p95（0.25% / 0.48° / 0.76 m，§3.7 表）远在收敛域内，所以**只要有 GNSS，就不需要特征级全局配准**。
- 超出收敛域时：加一层 `8s`（4 m）粗层，或者先用 FPFH+RANSAC（Open3D，r06 §3.7）求刚体初值，再做 Sim(3)-GICP。15° 偏航的实测结果见 §3.3.1。
- **耗时**：numpy 版在共享的 8 核机器上，约 10 万源点 × 3 层需要 13–30 s，瓶颈在 Python 端的 `n×3×7` 小矩阵乘法。生产版有两条路：(a) 每层随机抽样到 2–3 万源点（GLIM `randomsampling_rate=0.2` 的思路），numpy 版就能降到约 3–6 s；(b) 写约 200 行 pybind11 扩展，复用 small_gicp 的 KdTree 和协方差，把 `ParallelReductionOMP` 和 `LevenbergMarquardtOptimizer` 按 7×7 复制一份（对照 `reduction_omp.hpp`、`optimizer.hpp`，只改矩阵维度和 `se3_exp` → `sim3_exp`），预计比 numpy 快 20–50 倍。V0.5 先用 (a)，性能不够时再做 (b)。
- **只估 4/5 自由度**：当 LIO 地图已经重力对齐、视觉也已经用 IMU 重力对齐时，可以把 roll 和 pitch 锁住（仿照 small_gicp `RestrictDoFFactor`：`H += 1e9·diag(mask)`），只估 `[yaw, t, s]`，收敛域更大、也更稳。

#### 3.3.1 无先验时的全局初值（FPFH + RANSAC）

实测（`bench3.py`，偏航 35°、平移 7 m，Open3D 0.20 legacy：2 m 体素，法向半径 4 m，FPFH 半径 10 m / max_nn 100，mutual 过滤，`ransac_n=3`，检查器 EdgeLength(0.9) + Distance(3 m)，1e5 次迭代 / 0.999 置信度）：源 53k、目标 120k 点；FPFH 5.8 s，RANSAC 59 s（机器被其他任务占用；r06 在更小的点集上测得 3.8 s）；RANSAC 后误差 0.77° / 1.10 m（fitness 0.939），**再接 Sim(3)-GICP → 0.001% / 0.0007° / 1.1 mm**（9 s）。

FPFH 不具备尺度不变性，所以**全局初值只能在已知大致尺度时使用**。尺度未知时有三个来源，按优先级：① GNSS / RTK 轨迹 Umeyama（首选）；② 飞控气压计或 GNSS 的高度差与视觉相机高度差之比（竖直方向一维 Umeyama）；③ 两份点云的中位点距或包围盒对角线之比（误差 10–30%，只能粗估，之后还要做尺度扫描：`s ∈ s0·{0.8, 0.9, 1.0, 1.1, 1.25}` 各跑一次 FPFH+RANSAC，取 fitness 最大的那个）。

### 3.4 自动分辨率：密度估计与多尺度金字塔

**密度估计**（ingest 阶段每个点云都要做，结果写入元数据）：

```python
def estimate_spacing(xyz, n_sample=20000, k=8, threads=8):
    tree = small_gicp.KdTree(xyz, num_threads=threads)            # 5M 点约 3–4.7 s（8 线程）
    q = xyz[rng.choice(len(xyz), min(n_sample, len(xyz)), replace=False)]
    _, d2 = tree.batch_knn_search(q, k + 1, num_threads=threads)  # 第 0 个是自身
    d = np.sqrt(np.asarray(d2))
    return dict(nn1_median=np.median(d[:, 1]), nn1_p90=np.percentile(d[:, 1], 90), knn8_median=np.median(d[:, k]))
```

内置 6 个城市的实测（`density.py`；5M 点，坐标先平移到包围盒中心）：

| 场景 | 点数 | 包围盒（x×y×z，文件原始单位） | NN1 中位 | NN1 p90 | 8NN 中位 | 备注 |
|---|---|---|---|---|---|---|
| Chicago | 5.00M | 4.2 × 8.0 × 0.6 | 0.002 | 0.003 | 0.005 | **单位疑似 km**（×1000 后为 4.2 km×8 km×620 m，NN1 约 2 m），需要人工确认 |
| New York | 5.00M | 2928 × 3167 × 292 | 1.006 | 1.824 | 3.229 | |
| San Francisco | 5.00M | 740 × 717 × 55 | 0.187 | 0.328 | 0.582 | 最密 |
| Shenzhen | 5.00M | 1848 × 1999 × 391 | 0.524 | 1.049 | 1.665 | 本文基准数据 |
| Suzhou | 5.00M | 4407 × 156 × 686 | 0.347 | 0.627 | 1.115 | **Y-up**（见下） |
| Shanghai | 5.00M | 7736 × 6211 × 645 | 1.858 | 3.438 | 5.899 | 最稀 |

**上轴检测**（与 r06 §3.1 同一问题；r06 用「平均有符号法向绝对值最大的轴」，Suzhou 的 y 分量只有 0.147，区分度偏弱。这里给一个区分度更高的打分，可以作为 r06 算法的校验项）：对每个轴统计 `|n_axis|>0.9` 的比例 `f_a`，以及这些点法向符号的均值 `m_a`（屋顶和地面朝上，立面左右对称）。`up = argmax_a f_a·|m_a|`，方向取 `sign(m_a)`。实测 Suzhou 的 y 轴得分 0.19×0.72=0.137，z 轴 0.471×0.01≈0，**是 Y-up**；其余 5 个城市都是 Z-up（z 轴 `m_z` 为 0.38–1.0）。两种方法结论一致；单位（Chicago ×1000）的依据见 r02 §6 第 12 条、r06 §3.1。

**GLIM 的自适应体素分辨率**（`global_mapping.cpp::insert_submap`）：

```
d_med = median_distance(submap, 256)                # 点到传感器的中位距离
p     = clamp((d_med − dmin) / (dmax − dmin), 0, 1)
res   = res_min + p·(res_max − res_min)             # 远距离 → 粗体素
voxelmap_L = GaussianVoxelMap(res · scaling_factor^L),  L = 0..levels−1（scaling_factor=2）
```

我们的做法是把"到传感器的距离"换成"地图点距 `s`"（航测点云没有单一的传感器位置）：

```
s      = max(nn1_median(target), nn1_median(source))     # 两份点云里较稀的那份决定
levels = [(8s, 24s, 30), (4s, 12s, 30), (2s, 6s, 20), (s, 3s, 10)]   # (res, max_corr, iters)
         首层视初值质量决定：GNSS SPP 从 8s 开始，RTK 从 2s 开始
Shenzhen: s≈0.5 m → [4, 2, 1, 0.5] m，与 §3.3 实验一致
```

### 3.5 配准结果的 H 矩阵：退化检测、协方差、信息矩阵

small_gicp 的 `result.H` 是最后一次线性化的 `Σ JᵀMJ`（6×6，顺序 `[ω; v]`）。它有三个用途：

**① 退化检测**（航测常见：大面积平坦地面、单一立面、长直道路）：
```
H_rr = H[0:3,0:3], H_tt = H[3:6,3:6]                # 旋转块与平移块的量纲不同，要分开看
λ_t = eig(H_tt) 升序；  cond_t = λ_t[0] / λ_t[2]
if cond_t < 1e-3:   退化轴 = eigvec(H_tt)[:,0]        # 例：纯地面 → 水平两轴退化
λ_r = eig(H_rr)；   cond_r 同理（纯平面场景下绕法向的旋转不可观）
```
Shenzhen 城区实测，完整 6×6 的 `eig(H) = [1.0e6, 1.4e6, 1.3e7, 2.9e10, 9.7e10, 1.3e11]`。特征值分成相差约 4 个数量级的两组，大致分别对应平移和旋转：旋转被点到原点的杠杆臂（百米级）放大了。所以**不能直接对整个 6×6 求条件数**，要按块算（或者先把旋转列乘以特征半径 `r̄` 做无量纲化）。按这组数估算，平移方向的条件数约 0.08，属于良态。

**② 位姿协方差**（给因子图和 UI 置信度用）：
```
σ̂² = 2·e / (3·N_inlier − 6)        # 用残差估计单位权方差（GICP 协方差是"形状"，不是真实噪声尺度）
Σ_T = σ̂² · H⁻¹                     # 6×6
Σ_T = Σ_T + diag(σ_floor²)         # 下限：σ_rot ≥ 0.01°，σ_trans ≥ 1 cm（忽略点间相关性的 GICP 协方差普遍过于乐观）
```

**③ 当作因子图边的信息矩阵**：GLIM `create_between_factors` 直接用 `H + 1e6·I` 作为 `BetweenFactor<Pose3>` 的信息矩阵，相当于完全信任 GICP 的 Hessian（1e6 是正则项，防止奇异）。hdl_graph_slam 的做法是用 fitness 映射到方差：

```
fitness = mean_i min(‖T p_i − q_nn‖², max_range)             # 平均平方最近邻距离
w(x) = min_var + (max_var − min_var) · (1 − e^{−a x}) / (1 − e^{−a x_max})
       a=var_gain_a=20, x_max=fitness_score_thresh=0.5
var_x = w(fitness; 0.1², 5.0²),  var_q = w(fitness; 0.05², 0.2²)
Info = diag(1/var_x ×3, 1/var_q ×3)                           # 这里是 1/σ²，正确
```

**本项目建议**：用②的 `Σ_T⁻¹` 作为信息矩阵（同时保留方向信息和量纲），再乘一个保守因子 `κ=0.1–0.3`（事后用 QA 校准）。

**UI 表达**：DebugLayer 在每个 chunk / 子图中心画平移协方差椭球 `Σ_T[3:6,3:6]` 的 3σ 椭球，退化方向用红色（产品色 #E93024）高亮；世界面板的 QA 表格（lieflat 风格）列出 `cond_t`、`σ_trans`、`inlier_fraction`。

### 3.6 重叠度与配准候选对（移植 GLIM `overlap_auto` 思路）

```python
def overlap(target_voxelmap_keys: set, src_xyz, T, res):
    """源点变换后落入目标已占用体素的比例（GLIM 用高斯体素图，这里用哈希集合即可）"""
    p = src_xyz @ T[:3, :3].T + T[:3, 3]
    keys = pack21(np.floor(p / res).astype(np.int64))     # 与 small_gicp voxelgrid 同一种 21bit×3 打包
    return np.isin(keys, target_voxelmap_keys).mean()

# 候选对生成（视觉 chunk × LiDAR 子图，或多航次子图之间）
for a in chunks:
    for b in lidar_submaps:
        if ‖center(a) − center(b)‖ > max_pair_dist (GLIM: 100 m):  continue
        ov = overlap(keys(b), sample(a, 5000), T_init_ab, res=2s)
        if ov >= min_overlap (GLIM: 0.2，隐式回环；pose_graph 版要求 inlier_fraction ≥ 0.5): 加入配准任务
```

GLIM 还有一个值得照搬的保险机制：如果新子图和上一个子图的重叠 < `max(0.25, min_overlap)`，就补一条强 `BetweenFactor`（里程计相对位姿），**防止图断开**。对应到我们这里：视觉 chunk 之间要保留 r01 的窗口 Sim(3) 拼接边，即使配准失败，图也保持连通。

### 3.7 GNSS / RTK 约束进图

**坐标链**（不要照搬 hdl 的 UTM）：
```
(lat, lon, h_ellipsoid) --wgs84_to_ecef--> X_ecef --R_enu_ecef(lat0,lon0), 减去 X0--> x_enu   # 局部切平面，米
```
- UTM 的网格距离与地面真实距离差一个比例因子 k（中央经线处 0.9996，带边约 1.0010），同时存在子午线收敛角。1 km 航区就可能差出 0.4–1 m，**用 ENU 切平面就能避免**。代价是地球曲率：离原点 d 处高程偏差 `d²/2R`，1 km 为 7.8 cm，2 km 为 31 cm，对单个园区或街区可以接受。更大的场景按区块分别设原点，导出 3D Tiles 时再乘 `T_ecef_enu`。
- ENU 原点取航区中心附近的第一个 RTK FIX 点。**所有 float32 数据（Web 渲染、点云瓦片）都相对这个原点存储**，hdl 的 `zero_utm` 也是这个用意。原始 UTM 坐标的量级（5×10⁵、3.5×10⁶）超出 float32 的精度（24 bit 尾数），量化步长（ULP）为 0.03 m（5×10⁵ 处）到 0.25 m（3.5×10⁶ 处）。在 GPU 顶点着色器里还会叠加矩阵乘法误差，点云会出现肉眼可见的抖动和条纹。
- ECEF<->WGS84<->ENU 以 r02 §3.6 的实现为准（Python 和 TS 共用），glim_ext `geodetic.cpp`（Zhu 1994 闭式解）可作交叉校验。

**关键帧和 GNSS 的时间关联**：GLIM 式线性插值优于 hdl 的最近邻（0.2 s 门限）。无人机速度 10 m/s、GNSS 频率 5 Hz 时，最近邻的时间误差最多 0.1 s，折合 1 m。
```
t_k   = keyframe 时间 + time_offset（GNSS 与飞控 / LiDAR 的时钟差，先用互相关或飞控日志估计）
g(t_k)= lerp(g_left, g_right, (t_k − t_l)/(t_r − t_l))，要求 t_r − t_l ≤ 0.5 s，否则跳过
p_ant = T_world_body(t_k) · lever_arm                    # 天线相位中心 ≠ 机体原点，P600 需要实测杆臂
残差   r = p_ant − g_enu(t_k)
```

**先验因子**：
```
σ 按 fix 类型：RTK_FIX (0.02, 0.02, 0.04) m；RTK_FLOAT (0.3, 0.3, 0.6)；DGPS (0.8, 0.8, 1.6)；SPP (2, 2, 4)；NO_FIX → 丢弃
若接收机提供协方差，取 max(接收机协方差, 上表下限)（接收机协方差普遍偏乐观）
factor = PoseTranslationPrior3D(X(k), g_enu, Diagonal.Sigmas(σ))     # 信息 = 1/σ²（hdl 写成了 1/σ）
```

**初始化**（GLIM 4-DoF 思路）：LIO 地图已经重力对齐，所以世界系和 ENU 之间只差 `yaw + t`。取基线 ≥ 10 m 后的所有配对点，做 2D Umeyama（`cov = Σ(g−ḡ)(x−x̄)ᵀ` 取左上 2×2 做 SVD，检查行列式符号）。**视觉世界没有重力对齐也没有尺度**，所以要用 r01/r02 的完整 7-DoF RANSAC-Umeyama，并加入 IMU 重力"虚拟点"防止直线航带退化。

**两阶段鲁棒**（本项目建议，已验证）：
```
Stage 1: 所有 GNSS 先验加 Huber(k=1.345)，LM 求解
Stage 2: 计算每个先验的白化残差 χ² = Σ(r/σ)²；χ² > χ²₃(0.99) = 11.34 的剔除（多路径、跳变、fix 类型误报）
Stage 3: 剩下的先验用 L2 重新求解，初值取 Stage 1 结果
```

**实测**（`bench_pgo.py`，gtsam 4.3：240 个关键帧的割草机航线，LIO 漂移 = 0.4% 尺度 + 0.03°/关键帧偏航，航位推算 ATE **9.47 m**；里程计边 σ=0.05°/0.05 m）：

| 配置 | ATE | 求解耗时 |
|---|---|---|
| RTK FIX，L2，信息 1/σ² | **0.036 m** | 26 ms |
| RTK FIX，hdl 式信息 I/σ | 0.047 m | 19 ms |
| SPP，L2 | 0.772 m | 18 ms |
| SPP + 5% 多路径跳点（±15 m），L2 | 0.825 m | 117 ms |
| SPP + 5% 跳点，Cauchy(1.0) | 1.527 m（**反而更差**：初值离得远，Cauchy 把正常残差也一起压低了） | 62 ms |
| RTK + 5% 跳点，L2 | **4.371 m**（被跳点拉坏） | 35 ms |
| RTK + 5% 跳点，Cauchy(1.0) | 0.035 m | 279 ms |
| **两阶段** SPP，0% / 5% / 15% 跳点 | 0.821 / 0.678 / 0.803 m（剔除 2 / 19 / 32 个） | 75–151 ms |
| **两阶段** RTK，0% / 5% / 15% 跳点 | **0.035 / 0.035 / 0.038 m**（剔除 0 / 17 / 31 个） | 150–278 ms |

注：两阶段这两行和上面几行用的 GNSS 噪声随机种子不同，同一行内可比，跨行只看量级。

轨迹级 Sim(3) Umeyama 的精度（`bench2.py`，240 个位姿，覆盖 240 m×240 m，50 次蒙特卡洛，p95）：RTK FIX 为 0.0024% / 0.0049° / 7 mm；RTK FLOAT 为 0.034% / 0.081° / 12 cm；SPP 为 0.25% / 0.48° / 0.76 m。和 r02 的结论一致。

### 3.8 World Fusion 联合因子图

```
变量：
  L_j ∈ SE(3)    LiDAR 子图 j 的位姿（ENU）                        —— GLIM/FAST-LIO 子图或关键帧
  V_c ∈ Sim(3)   视觉 chunk c 的 T_enu_chunk（engine gauge → ENU）   —— LingBot 窗口 / VGGT chunk
因子：
  (a) LiDAR 里程计   BetweenFactorPose3(L_j, L_j+1, ΔT_odom, Σ_odom)
  (b) LiDAR 回环/重叠 BetweenFactorPose3(L_i, L_j, T_gicp, Σ_T(§3.5))       —— small_gicp GICP，候选来自 §3.6
  (c) GNSS 先验       PoseTranslationPrior3D(L_j, g_enu(t_j) − R_j·lever, σ_fix)   —— §3.7，两阶段鲁棒
  (d) 视觉 chunk 间   BetweenFactorSimilarity3(V_c, V_c+1, S_window, Σ_win)       —— r01 窗口拼接结果
  (e) 视觉<->LiDAR    自定义 Sim3-SE3 因子：S_meas = Sim(3)-GICP(chunk_c → map ∪ {L_j})，
                      残差 Log( S_meas⁻¹ · (L_j ⊕ V_c) )，信息取 7×7 的 σ̂²H⁻¹ 的逆
                      —— 简化实现：先把 L 冻结，再把 Sim(3)-GICP 的结果当作 PriorFactorSimilarity3(V_c) 加进去
  (f) 视觉轨迹 GNSS   对 V_c 内每帧相机中心：‖V_c·c_f − g_enu(t_f)‖，用 r02 的 RANSAC-Umeyama 结果作初值
  (g) 重力           视觉帧 IMU 姿态 → Rot3 先验（只约束 roll/pitch），或者用 r02 的"重力虚拟点"
求解：先解 {L}（(a)(b)(c)），再固定 {L} 解 {V}（(d)(e)(f)(g)），最后可选联合 LM。
     规模（100 个子图 + 50 个 chunk）在 gtsam 下是毫秒到百毫秒级，不需要 iSAM2。
```

**gtsam 实现注意**：`gtsam.Similarity3(R, t, s)` 的变换是 `s·(R·x + t)`（已实测：`transformFrom([1,0,0])` 的结果是 `1.5·(R·p + t)`），和我们 `x' = s·R·x + t` 的约定不同。**转换：`t_gtsam = t_ours / s`**。`PriorFactorSimilarity3`、`BetweenFactorSimilarity3`、`TrajectoryAlignerSim3` 在 gtsam 4.3 pip 版中都能用。

### 3.9 World Fusion 服务：算法流水线、状态机、QA 门限

```
QUEUED
  → F0 INGEST        读入（PLY/LAS/PCD、LingBot 会话、GLIM dump、FAST-LIO scans.pcd+轨迹、GNSS: NMEA GGA / ULog / CSV / NavSatFix）
                     单位与上轴规范化（r06 §3.1 + 本文 §3.4 校验）、平移到局部原点、密度 s、统计去噪（k=20，2σ）、
                     视觉点：置信度 ≥ τ_conf（LingBot conf）、去天空（mask_sky）、深度截断
  → F1 GEO_ANCHOR    fix 分级过滤、ENU 原点、杆臂、时钟差；产出 gnss_enu(t) 插值器
  → F2 LIDAR_GRAPH   (有 LiDAR 时) 4-DoF 初始化 → 位姿图 (a)(b)(c) 两阶段鲁棒 → ENU 下的 LiDAR 地图 + 每个子图的 Σ
  → F3 VIS_COARSE    每个 chunk：RANSAC-Umeyama Sim(3)（相机中心 <-> gnss_enu）+ 重力虚拟点（r02）
                     无 GNSS：有 LiDAR 时走 FPFH+RANSAC+尺度扫描，都没有则 scale_status='relative'
  → F4 VIS_FINE      每个与 LiDAR 地图重叠 ≥ 0.2 的 chunk：Sim(3)-GICP 金字塔（§3.3/§3.4）+ Huber(3)
  → F5 JOINT         联合图 (d)(e)(f)(g)（§3.8），得到最终 V_c
  → F6 FUSE          视觉点 → ENU；Geometry World：LiDAR 优先，没有 LiDAR 覆盖的地方用视觉补（按 source 标记）；
                     Visual World：视觉 RGB 点 + LiDAR 强度点；21 bit 体素键去重（每格保留置信度最高的点）；
                     重叠区中偏离 LiDAR 表面 > 3σ 的视觉点标记为动态 / 伪影（车辆、行人），从 Geometry 中剔除
  → F7 QA            Chamfer / F-score@{0.1,0.3,1.0} m（Open3D compute_metrics，r06 §3.9）、覆盖率、
                     每个瓦片的残差中位数（给 UI 着色用）、各阶段指标 → qa/registration.json
  → F8 EXPORT        alignment.json、coordinate.json（T_enu_world、scale_status）、fused.{ply,laz} → 交给 Tiler 任务切八叉树
DONE | FAILED(stage, reason) | CANCELLED
任一 QA 门限不过 → NEEDS_REVIEW：UI 提供 Gizmo 手动粗对齐（参照 GLIM offline_viewer 的 Merge sessions 交互），
                   提交后从 F4 继续
```

**QA 门限（初始值，事后用真机数据校准）**：

| 阶段 | 指标 | 通过条件 | 不通过时 |
|---|---|---|---|
| F1 | RTK FIX 占比、GNSS 缺口 | FIX ≥ 60%（否则 scale_status 降为 'gnss'）；最长缺口 ≤ 10 s | 降级 |
| F3 | Umeyama 内点率、RMSE、轨迹共线度 `λ₂/λ₁`（相机中心协方差） | 内点率 ≥ 0.6；RMSE ≤ 3σ_fix；共线度 ≥ 0.05，否则必须加重力 | NEEDS_REVIEW |
| F4 | 最细层 inlier_fraction（corr ≤ 1.5·res）、相对 F3 的修正量、平移块条件数 `cond_t` | ≥ 0.5；Δrot ≤ 2°、Δt ≤ 3σ_F3 + 1 m、Δs ≤ 2%；`cond_t ≥ 1e-3` | 退回 F3 的结果，标记 chunk 为 low_confidence |
| F5 | 先验被剔除比例 | ≤ 20% | NEEDS_REVIEW |
| F7 | 视觉<->LiDAR 的 Chamfer 中位数、F-score@0.3 m | ≤ 2·s；≥ 0.6 | 结果照常输出，UI 标红 |

**参数总表（默认值）**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `pyramid` | auto：`[8s,4s,2s,s]`，按初值质量截掉前几层 | §3.4 |
| `max_corr_factor` | 3.0 | `max_corr = 3·res` |
| `method` | 粗层 GICP，细层 VGICP（仅刚体步骤）；尺度步骤 Sim(3)-GICP | §3.2/§3.3 |
| `robust` | Huber δ=3.0（马氏距离） | §3.3 |
| `subsample_per_level` | 30000 源点 | 控制 numpy 版的耗时 |
| `k_neighbors` | 20 | 协方差估计 |
| `num_threads` | `os.cpu_count()` | **small_gicp 的 Python 默认值是 1** |
| `gnss_sigma_table` | FIX/FLOAT/DGPS/SPP 见 §3.7 | |
| `gnss_robust` | two_stage（Huber 1.345 → χ²₃ 0.99 → L2） | §3.7 |
| `min_overlap` | 0.2 | §3.6 |
| `cov_floor` | 0.01° / 1 cm | §3.5 |
| `enu_origin` | auto（第一个 RTK FIX 点）或手动指定 | §3.7 |

**耗时预算**（本机 8 核、被其他任务占用；300 m×300 m 航区、每个 chunk 约 10 万视觉点、LiDAR 45 万点）：F0 读取 5M 点 PLY 0.2–1.3 s、体素降采样 0.8 s、密度估计 3–5 s；F3 小于 1 ms；F4 每个 chunk 3–6 s（抽样）或 13–30 s（全量）；FPFH 兜底 6–60 s；F5 小于 0.3 s；F6 约 2 s/5M 点；F7 数秒。**单航区全流程 < 1 分钟（CPU）**，不需要 GPU。

### 3.10 接口设计

**REST（FastAPI，`apps/api`）**

```
POST /api/v1/worlds/{worldId}/fusion-jobs                → 202 {jobId}
     body: FusionJobSpec
GET  /api/v1/fusion-jobs/{jobId}                         → FusionJobStatus
POST /api/v1/fusion-jobs/{jobId}/cancel
POST /api/v1/fusion-jobs/{jobId}/manual-align            body {chunkId, T_init: Sim3}  → 从 F4 继续
GET  /api/v1/worlds/{worldId}/alignment                  → alignment.json
GET  /api/v1/worlds/{worldId}/qa/registration            → qa/registration.json
POST /api/v1/registration/align                          同步工具接口（≤ 50 万点，UI 里"对选中区域重新配准"）
     body {targetRef, sourceRef|points, T_init, method: 'gicp'|'vgicp'|'sim3', pyramid?} → RegistrationReport
```

**WebSocket**：并入主实时通道，topic 为 `fusion/{jobId}`（与遥测共用连接和心跳）。

```jsonc
{"topic":"fusion/7f3a","type":"stage","stage":"VIS_FINE","state":"running","progress":0.42,"chunk":"c03","level":1}
{"topic":"fusion/7f3a","type":"metric","stage":"VIS_FINE","chunk":"c03","key":"inlier_fraction","value":0.87}
{"topic":"fusion/7f3a","type":"preview","chunk":"c03","T":{"s":1.3699,"q":[0,0,0.317,0.948],"t":[12.0,-7.5,3.2]},"iter":7}
{"topic":"fusion/7f3a","type":"done","result":"/worlds/shenzhen/alignment.json"}
{"topic":"fusion/7f3a","type":"failed","stage":"VIS_COARSE","reason":"collinear_trajectory"}
```

`preview` 消息每层、每 N 次迭代推一次（≤ 5 Hz），前端据此播放源点云从 `T_init` 平滑过渡到 `T_final` 的动画（transitions.dev 缓动，Sim(3) 插值：`s` 取对数线性插值、`q` 用 slerp、`t` 线性插值）。

**Python 服务内部 API（`reconstruction/fusion`）**

```python
@dataclass
class Sim3:                      # x_b = s·R·x_a + t（与 r02 相同）
    s: float; q: np.ndarray; t: np.ndarray        # q = xyzw
    def to_gtsam(self) -> gtsam.Similarity3: return gtsam.Similarity3(Rot3(q), t / s, s)   # 注意 t/s

class MapTarget:                 # 目标地图金字塔缓存（多个 chunk、多次调用复用）
    @classmethod
    def build(cls, xyz, spacing, levels=None, num_threads=None) -> "MapTarget": ...
    levels: list[tuple[float, small_gicp.PointCloud, small_gicp.KdTree, small_gicp.GaussianVoxelMap | None]]

def estimate_spacing(xyz) -> SpacingStats
def normalize_scene(xyz, normals=None) -> tuple[np.ndarray, SceneNorm]     # units / up_axis / origin
def traj_sim3(src_centers, dst_enu, weights=None, gravity=None, ransac_thresh=None) -> tuple[Sim3, AlignReport]
def align_rigid(target: MapTarget, src_xyz, T_init, method="gicp", restrict_dof=None) -> RegistrationReport
def align_sim3(target: MapTarget, src_xyz, S_init: Sim3, huber=3.0, subsample=30000) -> RegistrationReport
def global_init_fpfh(target_xyz, src_xyz, voxel, scale_candidates=(1.0,)) -> tuple[np.ndarray, float]
def overlap(target: MapTarget, src_xyz, T, res) -> float
def build_fusion_graph(lidar, chunks, gnss, pairs) -> FusionGraph          # gtsam
def solve_two_stage(graph, chi2_thresh=11.34) -> FusionSolution
```

**RegistrationReport / alignment.json 的数据契约**

```ts
type Sim3 = { s: number; q: [number, number, number, number]; t: [number, number, number] }; // x_b = s·R·x_a + t
interface LevelLog { res: number; maxCorr: number; iters: number; inlierFraction: number; ms: number }
interface RegistrationReport {
  method: 'gicp' | 'vgicp' | 'sim3-gicp' | 'fpfh-ransac' | 'traj-umeyama';
  T: Sim3; converged: boolean; levels: LevelLog[];
  inlierFraction: number; rmseM: number; chamferMedianM?: number; fscore03?: number;
  H: number[][];                 // 6×6 或 7×7，顺序 [ω, v, (σ)]
  covDiag: number[];             // σ̂²H⁻¹ 加下限之后的对角
  degeneracy: { condTrans: number; condRot: number; degenerateAxes: [number, number, number][] };
  confidence: number;            // 0–1，由门限综合（UI 着色用）
}
interface Alignment {            // worlds/<id>/alignment.json
  version: 1;
  enuOrigin: { lat: number; lon: number; hEllipsoid: number };
  T_ecef_enu: number[];          // 4×4 行主序，float64
  scaleStatus: 'relative' | 'gnss' | 'rtk' | 'lidar';        // 与 r02 一致
  lidar?: { T_enu_lidar: Sim3; method: 'gnss-4dof+pgo' | 'glim' | 'none'; submaps: number; gnssRejected: number };
  visual?: { gauge: 'engine'; chunks: { id: string; frames: [number, number]; T_enu_chunk: Sim3;
             coarse: RegistrationReport; fine?: RegistrationReport; status: 'ok' | 'low_confidence' | 'manual' }[] };
  qa: { chamferMedianM?: number; fscore?: Record<string, number>; coverage?: number };
}
```

### 3.11 对 MVP 直接有价值的部分

**(a) 八叉树 LOD 的逐层采样算子（切片器，离线）**。small_gicp 的体素键排序降采样可以直接拿来当"每个节点按目标间距抽样"的算子：

```python
def pack21(ijk):                         # 与 small_gicp downsampling.hpp 一致：每轴 21 bit，偏移 2^20
    ijk = ijk + (1 << 20)
    return (ijk[:, 0] & 0x1FFFFF) | ((ijk[:, 1] & 0x1FFFFF) << 21) | ((ijk[:, 2] & 0x1FFFFF) << 42)

def voxel_pick(xyz, cell, origin):
    """每个体素保留离体素中心最近的原始点（不求质心，保留原始颜色/强度/分类），返回索引"""
    ijk = np.floor((xyz - origin) / cell).astype(np.int64)
    key = pack21(ijk)
    center = (ijk + 0.5) * cell + origin
    d = ((xyz - center) ** 2).sum(1)
    order = np.lexsort((d, key))                     # 先按 key，再按到中心的距离
    first = np.r_[True, key[order][1:] != key[order][:-1]]
    return order[first]

# 自顶向下：level L 的目标间距 s_L = s_root / 2^L，s_root = cube_size / G（G=128，与 Potree 相近）
# 节点内：picked = voxel_pick(node_pts, s_L)；没被选中的点下放给 8 个子节点；
#         节点点数 ≤ max_leaf（2–5 万）时停止；每个节点在元数据中记录 spacing=s_L、point_count、aabb
```
r06 §3.3 推荐的是 Potree 2 风格的「先随机打乱、每格取第一个点」（无偏、最便宜）。`voxel_pick` 取离格心最近的点，空间分布更均匀，代价是多一次排序。两者都用同一个 21 bit 键，切片器可以用参数切换，**默认沿用 r06**。要求质心时（纯几何、不带属性，比如配准和碰撞代理）直接调用 `small_gicp.voxelgrid_sampling(xyz, cell, num_threads=8)`，实测 5M 点 0.8 s（8 线程）/ 1.9 s（单线程），Open3D `voxel_down_sample` 要 14.7 s。**注意 21 bit 的范围**：`cell=0.01 m` 时坐标必须在 ±10.5 km 以内，超出的点会被丢掉（small_gicp 会打印警告）。内置场景最大的 Shanghai 包围盒是 7.7 km，按 0.01 m 量化仍在范围内；但更大的区域，或者没有平移到局部原点的真实 UTM / ECEF 坐标，就会越界。所以切片前一律先平移到局部原点，最细一层的 cell 不小于 1 cm（cell=0.1 m 时范围是 ±105 km）。

**(b) 场景规范化（ingest，内置数据必做）**：
```
scene.json（每个内置场景）：
  { "id": "shenzhen", "source": "UrbanScene3D/virtual", "units_scale": 1.0, "up_axis": "+z",
    "origin_offset": [...], "spacing": {"nn1_median": 0.524, "nn1_p90": 1.049, "knn8_median": 1.665},
    "bbox": [...], "point_count": 5000141, "has_normals": true, "has_color": false }
  Chicago: units_scale = 1000（推断，待核实：×1000 后 NN1≈2 m、包围盒约 4.2 km×8 km，符合城市尺度）
  Suzhou:  up_axis = "+y" → 切片前旋转 (x, y, z) → (x, −z, y)
```
UrbanScene3D 的 6 个采样点云都**没有颜色**（只有 xyz 和法向），前端需要按高度、法向或 EDL 着色；法向可以直接用来做光照 / EDL 增强。

**(c) spacing 驱动的点大小和细分阈值（前端，一句话公式）**：
```
P = viewportHeightPx / (2·tan(fovY/2))                # 投影系数
pointSizePx = clamp(k_size · spacing_L · P / depth, 1, 8)            # k_size ≈ 1.2，自适应密度
nodeSSEpx   = spacing_L · P / distance(camera, node.boundingSphere)  # 屏幕空间误差
refine(node) <=> nodeSSEpx > τ（≈1.5 px） 且 预算未满（按 nodeSSEpx 降序进优先队列）
```
spacing 用每个场景实测的 `nn1_median`，而不是写死的常数，这样同一套参数在 San Francisco（0.19 m）和 Shanghai（1.86 m）上都能用。点预算和 FPS 反馈控制属于 web3d 研究单元，这里只提供 `spacing` 的来源。

**(d) Fusion 演示任务（MVP 可做，CPU 秒级）**：用内置点云合成一份"视觉重建"，完整跑一遍 F3→F4，用 WS 推进度，在 UI 里演示 Reality→World。
```python
def mock_visual_recon(city_xyz, roi_half=150, keep=0.35, noise=0.08, floaters=0.03, S_gt=random_sim3(scale∈[0.6,1.6], yaw∈U(0,360°), tilt≤5°)):
    roi = crop(city_xyz, roi_half); vis = subsample(roi, keep) + N(0, noise); vis = vstack(vis, uniform_floaters)
    traj = lawnmower(roi, alt=80, lanes=6, per_lane=40); gnss = traj + N(0, σ_fix)
    return S_gt⁻¹·vis, S_gt⁻¹·traj, gnss            # 视觉系点云、视觉系相机中心、GNSS 观测
job: traj_sim3 → MapTarget.build(城市 ROI + 30 m 余量, levels=[2,1]) → align_sim3(抽样 2 万点) → report
```
实测（`mock_fusion.py`，Shenzhen，SPP GNSS）：读 PLY 1.31 s，目标金字塔 1.05 s，轨迹 Sim(3) 小于 1 ms（初值 0.019% / 0.19° / 0.15 m），**两层 Sim(3)-GICP（2 万点）2.98 s → 0.0008% / 0.0011° / 2 mm**。整个任务约 5 s，正好适合在 UI 中播放一段对齐动画，再用 lieflat 风格的表格展示各阶段指标。

**(e) Mock 定位（V0.4，可选）**：仿真无人机用 r05/r06 的方法（RaycastingScene）生成 LiDAR 扫描，套用 small_gicp `ScanToModelMatchingOdometry` 模式（`GaussianVoxelMap(1.0)` + `set_lru(100,10)`，每帧 `align` 后 `insert`），得到"估计位姿 + Σ_T"，推给 UI 的无人机卡片显示定位置信度，雾、雨环境场下可以演示退化。实测（Shenzhen，200 m 半径局部地图 45.7 万点，建 `GaussianVoxelMap(1.0)` 用 0.68 s，只需一次；每帧 2 万点、4 线程）：预处理 21.5 ms + VGICP 31.5 ms，**约 53 ms/帧**，平移误差中位数 4 mm。多机时按每机 2–5 Hz 限频，或者只给选中的无人机开启。

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块落点表

目标模块路径沿用 01-design §42 的仓库结构。

| 能力 | 来源（文件 / 函数） | 目标模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| 体素键排序降采样（LOD 逐层采样、融合去重） | small_gicp `util/downsampling{,_omp}.hpp::voxelgrid_sampling`；pip `small_gicp.voxelgrid_sampling` | `world/pointcloud/tiler` | **MVP** | adopt（pip）+ port（numpy `voxel_pick`，保留原始属性） | 比 Open3D 快 18 倍，依赖只有一个 378 KB 的 wheel |
| 密度估计 + 单位 / 上轴 / 原点规范化 | small_gicp `KdTree.batch_knn_search`；本文 §3.4 | `world/pointcloud/ingest` | **MVP** | adopt | 内置数据存在单位和上轴差异，必须先规范化 |
| Fusion 演示任务（mock 模式） | 本文 §3.11(d) | `reconstruction/fusion`（mode=mock）+ `apps/api` | **MVP** | adopt + port | CPU 约 5 s，在 UI 里演示 Reality→World |
| 刚体 GICP / VGICP、KdTree、GaussianVoxelMap | small_gicp `align` 三个重载 | `reconstruction/registration` | V0.5 | adopt | 核心算子 |
| Sim(3)-GICP | 本文 §3.3（numpy，复用 small_gicp 的近邻和协方差） | `reconstruction/registration/sim3_gicp.py`，之后改为 C++ 扩展 | V0.5（MVP 演示中已使用） | port | 视觉点云本身没有尺度 |
| 4-DoF 约束（锁 roll 和 pitch） | small_gicp `RestrictDoFFactor` | 同上 | V0.5 | adopt / port | LIO 和视觉都已重力对齐时使用 |
| FPFH + RANSAC 全局初值 | Open3D legacy（r06） | `reconstruction/registration` | V0.5 | adopt | 没有 GNSS，或超出收敛域时的兜底 |
| WGS84<->ECEF<->ENU | r02 §3.6（COLMAP `gps.cc` 移植，Python/TS 共用）；glim_ext `geodetic.cpp` 作交叉校验 | `world/georef` | **V0.1** | port | 世界坐标系的地基；以 r02 的实现为准 |
| GNSS 先验、时间插值、杆臂、fix 分级、两阶段鲁棒 | hdl `flush_gps_queue`、glim_ext `GNSSGlobal::backend_task` + 本文改进 | `world/georef`、`reconstruction/fusion/graph` | V0.1（轨迹 Sim3，r01）/ V0.5（位姿图） | port | 两个参考实现都偏简陋 |
| 4-DoF yaw-only 初始化 | glim_ext `GNSSGlobal`（2D SVD） | `world/georef` | V0.5 | port | LiDAR 地图接入 ENU |
| 重叠度判定与候选对 | GLIM `create_matching_cost_factors`、`find_overlapping_submaps` | `reconstruction/fusion` | V0.5 | port | 决定哪些 chunk 和哪些子图要配准 |
| H→协方差、退化检测、置信度 | small_gicp `RegistrationResult.H`、GLIM `create_between_factors`、hdl `InformationMatrixCalculator` | `reconstruction/registration/qa` | V0.5 | port | 图优化的权重和 UI 置信度 |
| 联合因子图 | gtsam 4.3（pip）：`BetweenFactorPose3`、`PoseTranslationPrior3D`、`BetweenFactorSimilarity3`、`PriorFactorSimilarity3` | `reconstruction/fusion/graph` | V0.5 | adopt | pip 可装，毫秒级求解 |
| LiDAR 离线建图（工具） | GLIM（Docker `koide3/glim_ros2`，CPU 配置或 GPU 配置） | `reconstruction/lidar` | V0.5 | adopt-as-tool（可选，主路线仍是 r05 的 FAST-LIO） | 带 GNSS 扩展、多航次合并和地图编辑 GUI |
| 多航次合并 | GLIM offline_viewer "Merge sessions" 流程 | `reconstruction/fusion/multi_session` | V0.6+ | reference | 同一园区多次飞行的地图累积 |
| scan-to-model mock 定位 | small_gicp `kitti_odometry.py::ScanToModelMatchingOdometry` | `sensors/lidar/mock_localization` | V0.4 | adopt | 实测约 53 ms/帧 |
| Colored ICP | small_gicp master `colored_icp_factor.hpp`（2026-08） | `reconstruction/registration` | V0.5+ | adopt（源码构建） | 视觉 chunk 之间、彩色 LiDAR 地图（FAST-LIVO2）之间的配准 |
| GPU 实时配准 | fast_gicp `FastVGICPCuda` / `NDTCuda`；GLIM GPU 模块 | — | V1.0 | reference | 只在需要机载或实时时回看 |
| 地面平面边 | hdl `floor_detection_nodelet` + `EdgeSE3Plane` | — | — | skip | 无人机不贴地飞；多航次高度一致性借用 LevelFactor 的思路即可 |

### 4.2 版本路线里的位置

```
MVP / V0.1  ingest 规范化（单位 / 上轴 / 原点 / spacing）→ 切片器用体素键采样
            Fusion 演示任务（mock：合成视觉重建 + 轨迹 Sim3 + Sim(3)-GICP，WS 推进度，UI 播放对齐动画）
            world/georef：WGS84/ECEF/ENU、coordinate.json（与 r01 一起：GNSS 轨迹 Sim3 提前到 V0.1）
V0.4        mock LiDAR 定位（scan-to-model VGICP），雾、雨环境下演示定位退化
V0.5        真实 World Fusion：F0–F8 全流程；LiDAR 走 FAST-LIO（r05）或 GLIM；GNSS 两阶段鲁棒位姿图；
            视觉<->LiDAR Sim(3)-GICP；QA 与 NEEDS_REVIEW 人工对齐
V0.6+       多航次合并（GLIM 流程）、Colored ICP、增量更新（新航次只重算受影响的 chunk）
V1.0        可选 GPU 实时配准（机载重定位）
```

### 4.3 服务形态

- small_gicp 的 Python 绑定**不释放 GIL**（`src/python/*.cpp` 里没有 `gil_scoped_release`；实测两个线程并行跑 `voxelgrid_sampling`，耗时和串行一样：1.81 s 对 1.89 s）。所以 **Fusion 任务不能在 FastAPI 的事件循环或线程池里跑**，必须放到独立的 worker 进程：`ProcessPoolExecutor`，或者 arq / RQ / Celery worker。进度通过 Redis pub/sub 或 multiprocessing Queue 回传给 WS 网关。worker 进程里设 `OMP_NUM_THREADS`，并把 `num_threads` 显式传给每个调用。
- 目标地图金字塔（`MapTarget`）按 `world_id + 版本` 缓存在 worker 内存（约 45 万点 × 3 层，几十 MB），同一世界的多个 chunk、多次手动重配都复用它。
- 依赖：`small_gicp==1.0.1`、`gtsam==4.3.0`、`open3d==0.20.0`（需要 libEGL，见 r06），都有 x86_64 wheel；small_gicp 还有 aarch64 wheel（Jetson）。

### 4.4 UI 对接（遵循视觉规范）

- **World → Fusion 面板**：shadcn `Card` + 阶段步进条（用 `Progress` 和 `Badge` 组合，F0…F8 状态为 queued / running / ok / warn / failed），图标用 morphicons（运行 <-> 完成状态用 morph 过渡，禁止 emoji）。
- **指标表**：各阶段的 inlier_fraction、rmse、Δs、cond_t、Chamfer、F-score，用 lieflat-charts 的表格视觉；超出门限的单元格用产品红 #E93024 标注，正常值用科技灰。
- **对齐动画**：收到 WS `preview` 后，源点云按 Sim(3) 插值过渡（transitions.dev 缓动，约 600 ms），最终着色切换为"残差热度"（灰→红），同样用淡入淡出过渡。
- **NEEDS_REVIEW**：3D 视图里出现 Gizmo（drei `TransformControls`），shadcn `Dialog` 提示"手动粗对齐后继续"，确认后调用 `POST /manual-align`。
- **世界可信度徽章**：顶栏用 `Badge` 显示 `scaleStatus`（relative / gnss / rtk / lidar）和整体 confidence。

---

## 5. 对比与推荐

| 维度 | small_gicp | GLIM | hdl_graph_slam | fast_gicp |
|---|---|---|---|---|
| Stars / 最后提交 | 1045 / 2026-08-31 | 1848 / 2026-09-06 | 2339 / 2024-07-16 | 1700 / 2025-04-24 |
| 2026 活跃度 | 高（Colored ICP） | 高（v1.2.x，GTSAM 4.3，CUDA 13.1） | 无（维护结束） | 无（已被取代） |
| 构建 / 安装 | **pip wheel**（x86_64 / aarch64）；header-only | 源码：GTSAM 4.3a0 + gtsam_points + (CUDA) + ROS2；或 Docker | ROS1 catkin + g2o + PCL + fast_gicp + ndt_omp | PCL + (CUDA + nvbio)；pygicp 需要编译 |
| GPU | 无 | 可选（VGICP GPU） | 通过 fast_gicp 可选 | 有（VGICP / NDT CUDA） |
| Python | 完整绑定 | 无（C++ / ROS） | 无 | 有限（pygicp） |
| 配准算法 | ICP / 点到面 / GICP / VGICP / Colored-ICP | 配准误差因子（gtsam_points） | 通过 PCL / fast_gicp / ndt_omp | GICP / VGICP / NDT |
| 尺度（Sim3） | 无（本文扩展） | 无 | 无 | 无 |
| GNSS | — | glim_ext（简陋：4-DoF 初始化 + 平移先验，z 不约束） | GPS XY/XYZ 先验边（最近邻时间关联，信息阵写成 1/σ） | — |
| 图优化 | 无 | iSAM2 + 配准误差因子、IMU 因子 | g2o 位姿图 | 无 |
| 本项目契合度 | **最高**（World Fusion 服务直接嵌入） | 高（离线 LiDAR 建图工具 + 架构参考） | 中（入门参考） | 低 |

**推荐排序**：
1. **small_gicp**：唯一一个既在 2026 年活跃、又能 pip 直接装进 Python 服务的配准库，覆盖 MVP（切片、密度、演示任务）到 V0.5（生产融合）。
2. **GLIM**：2026 年最活跃的全栈建图框架。它的"子图 + 重叠即约束 + 回调注入外部因子"架构，是我们 Fusion 因子图的设计蓝本；V0.5 可以直接当离线建图工具，和 r05 的 FAST-LIO 二选一或互为对照。
3. **hdl_graph_slam**：star 最多，但已经停止维护。GPS 边、信息矩阵、回环规则写得清楚，适合移植公式，不部署。
4. **fast_gicp**：被 small_gicp 全面取代，只保留 CUDA 实现作为 V1.0 参考。

---

## 6. 风险与注意事项

1. **尺度是头号风险，而且失败时没有报警。** 刚体 ICP 在 1% 尺度误差下仍然"收敛"（`converged=True`，inlier 率也很高），但会留下约 1 m 误差（§3.3）。凡是有视觉点云参与的配准，一律走 Sim(3)，并且在 QA 里检查 Δs。
2. **收敛域有限**：尺度 ≤5%、旋转 ≤5°、平移 ≤10 m（金字塔最粗 2 m）。15° 偏航和 10% 尺度都会失败。必须先做轨迹 Sim(3)（r01/r02）；没有 GNSS 时用 FPFH+RANSAC 加尺度扫描。
3. **跨模态差异**：视觉点云集中在纹理丰富的立面和屋顶，带漂浮噪点和动态物体（车、人）；MID-360 在 80–120 m 航高下几乎打不到地面（r05：40 m@10% 反射率）。**视觉和 LiDAR 的重叠可能很小**，所以要：只在重叠 ≥0.2 的区域配准；视觉点按置信度过滤、去天空；必要时安排低空 LiDAR 补飞。
4. **退化**：大面积草地、广场、单一立面会让平移块病态（`cond_t`）。退化方向要靠 GNSS 先验来约束，UI 要能显示出来。
5. **GICP 协方差过于乐观**：它忽略了点与点之间的相关性。直接用 H 当信息矩阵会让配准边压过 GNSS。按 §3.5 做 σ̂² 缩放、加下限、乘 κ。
6. **small_gicp Python 的默认值很坑**：`num_threads=1`、`max_correspondence_distance=1.0`、`downsampling_resolution=0.25`；21 bit 坐标范围（`cell=0.01 m` 时只有 ±10.5 km）；并行降采样在块边界处有轻微非确定性（黄金测试用单线程，或者允许容差）；**不释放 GIL**；PyPI 1.0.1 没有 Colored ICP。
7. **numpy 版 Sim(3)-GICP 慢**（全量 13–30 s / chunk）：先靠抽样压到数秒，性能不够时再写 C++ 扩展（§3.3）。
8. **gtsam 约定**：`Similarity3` 是 `s(Rx+t)`；`Pose3` 切空间是 `[ω, v]`，和 small_gicp 的 H 顺序一致；鲁棒核的选择很关键：Cauchy 从远处初值出发反而更差，用两阶段法（§3.7）。
9. **GLIM 的部署成本**：GTSAM 4.3a0 和 gtsam_points 都要源码编译，GNSS 模块只支持 ROS2，而且依赖上游提供平面坐标；glim_ext 部分模块依赖 GPL / CC-NC 库（科研用途可以忽略许可）。建议只用官方 Docker 镜像。
10. **hdl_graph_slam 只支持 ROS1**（Noetic 已于 2025 年 EOL），不要部署；**fast_gicp 的 CUDA 构建**锁定老版本 CUDA 和 nvbio，不要投入。
11. **坐标与精度**：不要用 UTM 网格做度量仿真（比例因子会带来 0.4–1 m/km 的误差）；ENU 切平面超过 2 km 时，曲率带来的高程误差达到 dm 级，要分块设原点；所有 float32 数据都要相对局部原点存储。
12. **时间同步和杆臂**：10 m/s 飞行时，100 ms 时钟差就是 1 m 误差；P600 的 RTK 天线杆臂约 0.2–0.3 m（需实测）。两者都必须进 Vehicle Package（r04 的标定版本管理）。
13. **内置数据异常**（与 r02/r06 一致）：Chicago 疑似以 km 为单位，Suzhou 是 Y-up，6 个场景的点距差 1000 倍，而且都没有颜色。ingest 不规范化的话，LOD、点大小、物理尺度、风场都会出错。
14. **Open3D 0.20 依赖 libEGL**（r06）：服务镜像要装 `libegl1 libgl1`，否则 import 失败。FPFH 和 QA 指标都依赖它。
15. **资源争用**：small_gicp 的 OpenMP 线程和 Open3D 的 TBB 线程同时用满 CPU 时会互相拖慢（本研究的 RANSAC 用时 59 s，r06 为 3.8 s；两者点数不同，但机器争用是主要原因之一）。fusion worker 要独占核心，或者限制并发任务数（默认 1）。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§6 融合链路要写出尺度这一步。** 现在写的是 "LingBot-Map + MID-360 + RTK → World Fusion"，建议改成：`视觉（engine gauge，无尺度、无重力）→ 轨迹 RANSAC-Umeyama Sim(3)（GNSS/RTK + IMU 重力）→ 点云 Sim(3)-GICP（对 LiDAR 地图）→ GNSS 先验 + 配准边联合因子图 → 融合 → QA`，并明确"**视觉与 LiDAR 之间是 Sim(3)，不是 SE(3)**"。原文"尺度可能存在误差"应改为"尺度本来就不存在"（与 r02 一致）。
2. **§6 / §33 技术选型要重新分工。** 原文让 Open3D 承担 "ICP / Registration" 全部，后续再引入 "PCL / CUDA Point Cloud Kernels / cuVSLAM"。建议改为：**small_gicp** 负责预处理、GICP/VGICP 和 Sim(3)-GICP（核心算子，pip 安装；体素降采样比 Open3D 快 18 倍，VGICP 比 Open3D 点到面 ICP 快约 3.4 倍，可以取到 H 用于不确定度）；**Open3D** 负责 FPFH+RANSAC 全局初值、QA 指标和 RaycastingScene（r06）；**gtsam（pip）** 负责因子图；**PCL 不需要**；cuVSLAM 和 CUDA 点云核在融合链路里都用不上，删掉。
3. **§7 World Model 的 Geographic 要写出变换链和精度约定**：`WGS84 → ECEF → ENU(origin)`，内部一律用 ENU 米制 + float64 变换 + float32 相对坐标；UTM 只作为 GIS 导入导出格式。补充 `T_ecef_enu`、`T_enu_world`（Sim3）、`scaleStatus`、`confidence`。
4. **§41 World Package 增加**：`alignment.json`（§3.10 的契约，与 r02 的 `alignment.json` 合并成同一份 schema）；`qa/registration.json`；`reconstruction/fusion/graph/`（节点、边、先验的 JSON，可重算、可审计）；`geometry/pointcloud/scene.json`（`units_scale`、`up_axis`、`origin_offset`、`spacing`）。
5. **§48 V0.5 拆成可验收的子里程碑**：V0.5a GNSS 两阶段鲁棒位姿图（LiDAR 地图接入 ENU，验收：ATE ≤ 5 cm@RTK）；V0.5b 视觉<->LiDAR Sim(3)-GICP（验收：Δs ≤ 0.1%、Chamfer 中位数 ≤ 2s）；V0.5c QA 与人工复核闭环。多航次合并放到 V0.6。
6. **§43 / §44 MVP 链路里补两步**："Point Cloud → **Ingest 规范化（单位、上轴、原点、spacing）** → Octree"；另外建议 MVP 就内置"Fusion 演示任务"（CPU 约 5 s），让 Reconstruction→World 这一层在 Demo 里真实可见，而不是只有静态点云。
7. **§14 点云 Web 渲染**：LOD 选择要基于**每个节点的实测 spacing**（屏幕空间误差 = spacing·P/distance），而不是只写 "Camera Position / FOV / Distance / Screen Size"。点大小同样由 spacing 驱动（§3.11c）。这是"点云疏密自动调节"能在不同密度数据上都成立的前提。
8. **§4 浏览器职责边界补充**：配准和融合都在 worker 进程里跑（small_gicp 不释放 GIL），浏览器只接收 `preview` 变换做动画；"LiDAR registration" 这条已经在原文的"不负责"清单里，保持不变。
9. **§36 实时通信**：WebSocket 的负责项里加上 "Job Progress（fusion / tiling）"，用 topic 区分，和遥测共用一条连接。
10. **§16 DebugLayer 增加**：配准协方差椭球、退化方向、残差热度着色。这对科研用户判断世界可信度很有价值。
11. **§2 采集清单补充**：RTK 的 **fix 类型和协方差**（不只是位置）、**天线杆臂**、**各传感器的时间同步方式**（PPS / PTP），以及飞控日志里的 GNSS 原始数据（ULog `vehicle_gps_position`）。缺了这些，§3.7 的门限和权重都无从设置。
12. **§5 LingBot-Map 的定位**：建议注明输出按窗口分 chunk，每个 chunk 有独立的 Sim(3)（r01），World Fusion 以 chunk 为单位配准和优化。单一全局 Sim(3) 在长航线上会被视觉漂移拉坏。
