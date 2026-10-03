# r06 研究笔记：Open3D（点云处理、配准融合与 Geometry World 查询核心库）

> 研究单元：r06 ｜ 对应设计文档：01-design.md §6（LiDAR 与视觉融合）、§7–8（World Model / Geometry World）、§14–15（点云 LOD 与格式）、§22–24（环境对传感器的影响）、§33（后端栈）、§41（World Package）、§43–48（MVP 与 V0.5）
> 源码位置：`/data/projs/anet-drone/refs/lidar/Open3D`（v0.20.0，commit `b6c5e19`，2026-09-16 “Fix release packaging and CI uploads for v0.20 release”）
> 本机实测环境：Intel Xeon E5-2603 v4（8 核 1.7 GHz），62 GB RAM，**无 GPU**；项目 venv 中的 `open3d==0.20.0`（CUDA 版 wheel，CPU 回退运行）。
> 实测脚本与原始结果：`/data/projs/anet-drone/.cache/research/r06/{bench1..6.py, lod_proto.py, *.json}`；运行方式见 §4.2（本地解包 libEGL，不做全局安装）。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **isl-org/Open3D**（14005 stars，最后提交 2026-09-16，v0.20.0，MIT） | C++17 + Python 的 3D 数据处理库：点云预处理、配准、TSDF、网格、光线投射、最近邻/哈希 | **adopt**（Python 直接依赖，pin `open3d==0.20.0`） | V0.1 起 | 5/5 |
| ├ `t.geometry.RaycastingScene`（Embree 4） | 光线求交 / 距离 / 有符号距离 / 占据 / 视线 | **adopt**：Geometry World 的查询内核、虚拟 LiDAR 与深度相机 | **V0.2（MVP 即可上）** | 5/5 |
| ├ `t.geometry.PointCloud` 预处理 | 读写、裁剪、离群点剔除、法向、DBSCAN、平面分割、MLS 平滑、指标 | **adopt**：World Ingest 离线流水线 | V0.1 | 4/5 |
| ├ `t/pipelines/registration`（ICP 家族） + legacy RANSAC/FGR/GICP/NDT/PoseGraph | 配准与多段地图合并 | **adopt**（精配、QA）；全局配准用 legacy RANSAC | V0.5 | 4/5 |
| ├ `t.geometry.VoxelBlockGrid`（稀疏 TSDF） | 体素融合、网格 / 点提取、体渲染 | **adopt**（V0.5 网格化 / 自定义属性融合）；MVP 不用 | V0.5 | 3/5 |
| ├ 表面重建（Poisson / BPA / Flying Edges / 二次简化） | 点云 → 网格（碰撞 / Gazebo 世界） | **adopt**（离线） | V0.5 | 3/5 |
| ├ `core.HashSet / HashMap`、`core.nns` | 体素键哈希、KNN / 半径 / 混合近邻 | **adopt**（占据查询、密度统计） | V0.2 | 4/5 |
| ├ legacy `geometry.Octree` / `VoxelGrid` | 指针式八叉树、稀疏体素 | **skip**（不适合 Web LOD：shared_ptr 节点 + 内部节点存全量索引） | — | 1/5 |
| ├ `t.geometry.PointCloud.voxel_down_sample` 作 LOD | 均值体素降采样 | **reference**（LOD 建树改用排序式“每格取一点”，见 §3.3） | — | 2/5 |
| ├ Filament 渲染 / WebRTC visualizer / Gaussian Splat 渲染 | 桌面与远程可视化 | **skip**（前端自研 React + Three.js）；3DGS 的 PLY/SPLAT/SPZ IO 作 V1.0 转换工具 **reference** | V1.0 | 2/5 |

一句话结论：

1. **Open3D 在本项目中最有价值的不是 ICP，而是 `RaycastingScene`。** 用 UrbanScene3D 点云生成 2 m DSM 高度场网格（460 万三角形，numpy 构建约 1.5 s，BVH 提交 4–5 s，内存约 530 MB），就能在服务端拿到一个精确的 Geometry World：MID-360 类 LiDAR 每帧 2 万条光线约 **6 ms**（朝地面、命中型光线约 3 M rays/s；朝天空的光线 6–11 M rays/s），640×480 深度图 26 ms，1 万条航段碰撞检测 2 ms，10 万点距离查询 0.27 s。比 r04/r05 的 numpy 球面 z-buffer 方案（每帧 9–80 ms）快一个量级，且有真实遮挡。**建议把 Geometry World 提前到 MVP（V0.2）**，而不是原文隐含的 V0.5 / Isaac 阶段。
2. **Open3D 的“点云 → Web LOD”能力很弱**：legacy `Octree` 不可用；张量 `voxel_down_sample` 在 5M 点上需要 4–9 s，而且输出的是均值点（跨墙角平均会产生“幽灵点”）。本笔记给出一个 numpy 排序式、Potree 2 风格的分层采样建树原型（5M 点 15.8 s，401 个节点，每节点中位数 9.1k 点）。Open3D 只负责 IO、清洗、法向和 QA。
3. **UrbanScene3D 六个城市的坐标系并不统一**（实测）：Chicago 是归一化单位（范围约 4.2×8.0×0.6，推测 1 单位 ≈ 1 km），Suzhou 是 **Y-up**，其余是 Z-up、单位米。World Ingest 必须做“轴 / 单位 / 原点”规范化（算法见 §3.1），否则 LOD、碰撞和风场都会错。
4. 配准实测（300 m×300 m 街区，合成扰动）：张量 `multi_scale_icp` 点到面 0.7 s 收敛到毫米级；FPFH+RANSAC 在 60° / 47 m 的初值下 3.8 s 收敛到 0.14° / 2.8 cm，再接 ICP 可达 0.6 mm；FGR 较差（0.76° / 4.2 m）；0.20 新增的 NDT 用默认参数失败。**V0.5 用法**：Open3D 负责最终精配、多航次 PoseGraph 和 QA 指标（Chamfer / F-score）；高频 LiDAR 里程计交给 FAST-LIO / small_gicp（与 r05 结论一致）。
5. **部署**：0.20 wheel 硬链接 `libEGL.so.1`（新增 `EGLOffscreenContext.cpp`），无头服务器需要 `apt install libegl1 libgl1`；无 root 时可以像本研究一样把 deb 解包后用 `LD_LIBRARY_PATH` 加载。`open3d-cpu` 在 PyPI 上最新只有 0.19.0（pybind 216 MB，无需 EGL），但缺少 0.20 的 Symmetric ICP、NDT、MLS 平滑、SourceRotation 检查器和 SPZ。
6. **进程模型**：`RaycastingScene` 的 pybind 绑定**不释放 GIL**（`cpp/pybind/t/geometry/raycasting_scene.cpp` 中没有 `gil_scoped_release`），不能放进 FastAPI / uvicorn 的事件循环进程里，必须放在独立的仿真进程，并按 tick 批量合并所有无人机的光线。

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/isl-org/Open3D ，14005 stars，最后提交 2026-09-16（v0.20.0 发布），MIT |
| 语言 / 构建 | C++17 + pybind11；CMake（`CMakeLists.txt`，另支持 vcpkg `vcpkg.json`）；关键选项：`BUILD_CUDA_MODULE`、`BUILD_SYCL_MODULE`、`BUILD_GUI`、`BUILD_WEBRTC`、`WITH_IPP`、`BUILD_ISPC_MODULE` |
| 关键第三方 | Eigen、**oneTBB**（0.20 起取代 OpenMP，PR #6626）、**Embree 4**（光线投射）、nanoflann（CPU KNN）、stdgpu / SlabHash（CUDA 哈希）、PoissonRecon、Qhull、VTK（Flying Edges 等值面、布尔运算）、UVAtlas、Filament（渲染）、IPP / NPP |
| 两代 API | legacy：`open3d.geometry / io / pipelines`（Eigen、float64、仅 CPU）；**tensor**：`open3d.core / t.geometry / t.io / t.pipelines`（float32 默认，CPU / CUDA / SYCL）。0.20 官方方向是“tensor 优先”（wheel 自带 `open3d/agent_skills/open3d-python/SKILL.md` 明确写了这点） |
| Wheel | `open3d`（Linux x86_64 为 CUDA 12.6 构建，安装后 907 MB，`libOpen3D.so` 768 MB）；`open3d-cpu`（PyPI 最新仅 0.19.0）；`open3d-xpu`（SYCL）；`open3d[ml]` 可选 |
| 0.20 新特性（与 0.19 cpu wheel 符号比对确认） | Symmetric ICP（legacy + tensor，PR #7276）、**3D NDT**（PR #7517）、点云平滑 MLS / Laplacian / Taubin / Bilateral（PR #7419）、`CorrespondenceCheckerBasedOnSourceRotation`（PR #7461）、Poisson 暴露 `full_depth / samples_per_node / point_weight`（PR #7430）、`compute_ambient_occlusion`、3DGS 的 SPZ 压缩 IO、SYCL 版哈希 / NNS / 配准、TBB 取代 OpenMP（`o3d.utility.set_max_threads()` 取代 `OMP_NUM_THREADS`）、vcpkg |
| 本机状态 | `import open3d` 报错 `ImportError: libEGL.so.1`（`readelf -d libOpen3D.so.0.20` 显示 NEEDED：libtbb、**libEGL**、libX11、libudev、libusb-1.0、libidn2、libGL）。修复方式见 §4.2 |

---

## 2. 源码结构与关键模块

| 目录 | 内容 | 与本项目相关度 |
|---|---|---|
| `cpp/open3d/t/geometry/RaycastingScene.{h,cpp}` | Embree 4 场景：`CastRays / TestOcclusions / CountIntersections / ListIntersections / ComputeClosestPoints / ComputeDistance / ComputeSignedDistance / ComputeOccupancy / CreateRaysPinhole` | 5/5 |
| `cpp/open3d/t/geometry/PointCloud.{h,cpp}`、`kernel/PointCloud*.cpp`、`kernel/PCAPartition.cpp` | 张量点云：降采样、离群点、法向、平滑、DBSCAN、平面、HPR、投影、指标、PCA 分块 | 4/5 |
| `cpp/open3d/t/pipelines/registration/` | `ICP / MultiScaleICP / SymmetricICP`、`TransformationEstimation{PointToPoint, PointToPlane, Symmetric, ForColoredICP, ForDopplerICP}`、`RobustKernel`、`Feature.cpp`（FPFH、特征对应） | 4/5 |
| `cpp/open3d/pipelines/registration/` | legacy：`Registration.cpp`（RANSAC）、`CorrespondenceChecker`、`FastGlobalRegistration`、`GeneralizedICP`、`NormalDistributionsTransform`、`ColoredICP`、`PoseGraph / GlobalOptimization` | 4/5 |
| `cpp/open3d/t/geometry/VoxelBlockGrid.{h,cpp}`、`kernel/VoxelBlockGridImpl.h` | 稀疏体素块 TSDF：`GetUniqueBlockCoordinates / Integrate / RayCast / ExtractPointCloud / ExtractTriangleMesh / Save / Load` | 3/5 |
| `cpp/open3d/geometry/SurfaceReconstruction{Poisson,BallPivoting,AlphaShape}.cpp`、`t/geometry/TriangleMesh.h` | 表面重建、`CreateIsosurfaces`（Flying Edges）、`SimplifyQuadricDecimation`、`Boolean*`、`FillHoles`、`ComputeUVAtlas` | 3/5 |
| `cpp/open3d/core/hashmap/`（`CPU/TBBHashBackend.h`、`CUDA/SlabHashBackend.h`）、`core/nns/` | 张量哈希、KNN / 半径 / 混合 / 多半径近邻 | 4/5 |
| `cpp/open3d/geometry/Octree.{h,cpp}`、`VoxelGrid.{h,cpp}` | legacy 八叉树与稀疏体素 | 1/5 |
| `cpp/open3d/t/io/file_format/` | PLY / PCD / PTS / NPZ / XYZ* / **SPLAT / SPZ**；**不支持 LAS/LAZ**（需要 laspy / PDAL） | 3/5 |
| `cpp/pybind/**` | Python 绑定（注意 GIL 策略，见 §2.1） | 4/5 |
| `examples/python/geometry/ray_casting_*.py`、`t_reconstruction_system/integrate_custom.py`、`pipelines/*_registration.py` | 可直接照搬的示例 | 4/5 |

### 2.1 RaycastingScene（重点）

- **后端**：Embree 4（`#include <embree4/rtcore.h>`）。构造函数 `RaycastingScene(int64_t nthreads=0, Device=CPU:0)` 调用 `rtcNewDevice("threads=n")`，并设置 `RTC_SCENE_FLAG_ROBUST | RTC_SCENE_FLAG_FILTER_FUNCTION_IN_ARGUMENTS`。**只支持 CPU 和 SYCL，不支持 CUDA**（头文件注释 “This class supports only the CPU device”；SYCL 分支是 `SYCLImpl`）。
- **几何**：`AddTriangles(V{N,3} float32, F{M,3} uint32)` 通过 `rtcSetNewGeometryBuffer` 拷贝进 Embree，返回 geometry id。**没有删除几何、没有实例变换的 API**，场景只能追加或重建。
- **惰性提交**：`Impl::CommitScene()` 在第一次查询时调用 `rtcCommitScene`（支持时用 `rtcJoinCommitScene`）。每次 `AddTriangles` 都会把 `scene_committed_` 置为 false，所以“加几何后的第一次查询”会承担全部 BVH 构建时间（实测 4.6M 三角形约 4–5 s）。
- **CPU 查询**：`CPUImpl::CastRays` 用 `tbb::parallel_for` 按 `BATCH_SIZE=1024` 分块，每条光线调用 `rtcTraversableIntersect1`；输出 `t_hit / geometry_ids / primitive_ids / primitive_uvs / primitive_normals`。光线格式是 `[ox,oy,oz,dx,dy,dz]`，**方向不必归一化，`t_hit` 以方向向量长度为单位**。利用这一点，把方向设为 `p1−p0`、判断 `t_hit<1`，就是**线段碰撞检测**。内部有一个 `line_intersection` 分支，但没有暴露给 Python。
- **有符号距离 / 占据**：`VoteInsideOutside()` 以固定种子 `mt19937(42)` 生成 `1±0.001` 的抖动方向，对每个查询点发射 `nsamples` 条光线，用 `CountIntersections` 数交点奇偶；`ComputeSignedDistance = ComputeDistance × (inside ? −1 : +1)`。**前提是网格水密且互不相交**。DSM 高度场网格不是闭合体，所以符号要用高度场自己判断（见 §3.6）。
- **相机光线**：`CreateRaysPinhole(K, T_cw, w, h)` 或 `(fov, center, eye, up, w, h)`，逐像素 `dir = Rᵀ·K⁻¹·[x+0.5, y+0.5, 1]`。
- **GIL**：`cpp/pybind/t/geometry/raycasting_scene.cpp` 没有 `py::call_guard<py::gil_scoped_release>`（对比 `t/pipelines/registration/registration.cpp` 里的 `icp` 就有）。查询虽然在 C++ 内用 TBB 多线程跑，但整个调用期间一直持有 GIL。

### 2.2 张量点云 `t.geometry.PointCloud`

- `VoxelDownSample(voxel, "mean")`（`PointCloud.cpp:496`）：先 `floor(p/voxel)` 转成 Int64 键，`core::HashSet::Insert`，再 `Find` 取回 point→voxel 映射，最后用 `IndexAdd_` 按体素累加后求均值。**所有属性都会被平均**：LOD 需要保留真实点，均值会跨表面平均。单一哈希加上多次 IndexAdd 的开销在 CPU 上偏重（实测见 §3.0）。
- `PCAPartition(max_points)`（`kernel/PCAPartition.cpp`）：递归地沿协方差最大特征向量、在投影区间中点二分，直到每份不超过 `max_points`，写出 `partition_ids` 属性。适合“等点数”分块（ICP 分块、并行处理），**不适合**视锥裁剪与 LOD（块不规则、没有层级）。
- `RemoveStatisticalOutliers(k, std_ratio)`：每点求 k 近邻平均距离 d̄ᵢ，阈值 `μ + std_ratio·σ`。`RemoveRadiusOutliers(n, r)`：半径 r 内邻居少于 n 的点剔除。`EstimateNormals(max_nn, radius)`：混合近邻求协方差，legacy 用 `FastEigen3x3`（`geometry/EstimateNormals.cpp:122`）解析求最小特征向量。
- 0.20 新增 `SmoothMLS / SmoothLaplacian / SmoothTaubin / SmoothBilateral`（`geometry/PointCloudSmoothing.cpp` + 张量版）。
- 其他常用：`Crop(AABB/OBB)`、`SegmentPlane`（RANSAC）、`ClusterDBSCAN`、`HiddenPointRemoval`（Katz 球面翻转 + 凸包）、`ProjectToDepthImage`、`ComputeMetrics`（Chamfer / Hausdorff / F-score）、`FarthestPointDownSample`（O(N·k)，大规模不可用）。

### 2.3 配准

- **张量 ICP**（`t/pipelines/registration/Registration.cpp`）：`ICP()`、`MultiScaleICP()` 先用 `InitializePointCloudPyramidForMultiScaleICP` 按 `voxel_sizes` 逐级 `VoxelDownSample`，每级构建 `NearestNeighborSearch::HybridIndex(max_corr)`，在 `DoSingleScaleICPIterations` 中按 `relative_fitness / relative_rmse / max_iteration`（默认 1e-6 / 1e-6 / 30）判断收敛。估计器有 PointToPoint、PointToPlane、Symmetric（残差 `(p−q)ᵀ(n_p+n_q)`）、ColoredICP、DopplerICP（FMCW）。鲁棒核有 L2 / L1 / Huber / Cauchy / GM / Tukey / Generalized。**返回的 transformation 总是 CPU Float64**，Python 端 `voxel_sizes` 必须传 `o3d.utility.DoubleVector`。
- **legacy 全局配准**（`pipelines/registration/Registration.cpp`）：`RegistrationRANSACBasedOnFeatureMatching` 用 TBB 并行 RANSAC，自适应迭代上限 `k = log(1−confidence) / log(1 − w^n)`（`est_k_global` 原子取最小，`w` 为当前内点率，`n=ransac_n`），默认 `max_iteration=100000, confidence=0.999`；校验器有 `EdgeLength(0.9)`、`Distance(d)`、`Normal(θ)`，以及 0.20 新增的 `SourceRotation([rx,ry,rz])`（SO(3) log 各分量阈值，<0 表示不约束）。`FastGlobalRegistration` 默认参数 `division_factor=1.4, max_corr=0.025, iteration=64, tuple_scale=0.95`。**tensor 侧只有 FPFH 与特征对应，没有 RANSAC**。
- **legacy 局部配准**：`GeneralizedICP`（`epsilon=1e-3`）、`NDT`（`voxel_size=1, min_points_per_voxel=6, covariance_regularization=1e-3, max_iteration=30, outlier_threshold=9 (Mahalanobis²), neighbor_search_type=1`（中心体素加 6 邻域））。
- **多段合并**：`PoseGraph` 配合 `GlobalOptimization`（LM / GN；`GlobalOptimizationOption(max_corr=0.075, edge_prune_threshold=0.25, preference_loop_closure=1.0, reference_node=-1)`），`GetInformationMatrixFromPointClouds` 提供边的信息矩阵。

### 2.4 VoxelBlockGrid（稀疏 TSDF）

- 结构：全局 `core::HashMap`，键是块坐标 Int32×3，值是 `block_resolution³` 的 SoA 属性（如 `tsdf:float32, weight:uint16/float32, color:uint16×3`）。
- 内置 `Integrate` 只接收**深度图**（pinhole）。核心公式在 `kernel/VoxelBlockGridImpl.h:262–302`：`sdf = depth − z_c`，丢弃 `sdf < −trunc`，截断后归一化 `sdf/trunc`，加权平均 `tsdf ← (w·tsdf + sdf)/(w+1)`，`w ← w+1`。`trunc = voxel_size × trunc_voxel_multiplier`（默认 8）。
- **LiDAR / 点云没有内置 Integrate**，但 `examples/python/t_reconstruction_system/integrate_custom.py` 演示了“`compute_unique_block_coordinates` → `hashmap().activate` → `voxel_coordinates_and_flattened_indices` → 张量运算自写更新”这条路径。本研究据此实现了“点到平面 TSDF”（§3.8），也可以写 log-odds 占据。
- `ExtractTriangleMesh(weight_threshold=3.0)` 默认要求至少 3 次观测，单帧融合时要调低（常见坑）。

### 2.5 legacy Octree（为什么不用）

- `geometry/Octree.cpp:52–73`：子节点索引 `child = x + 2y + 4z`（**Potree 是 `(x<<2)|(y<<1)|z`，两者顺序相反**，混用会出错）。
- 节点是 `std::shared_ptr`，`OctreeInternalPointNode` 在**每个内部节点保存其子树的全部点索引**，内存约 O(N·depth)。`ConvertFromPointCloud` 单线程逐点 `InsertPoint`，没有“每层抽样”的 LOD 概念。可以用来做 `LocateLeafNode` 等小规模查询，不能用来建 Web LOD。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.0 实测基准汇总（UrbanScene3D New York 5,000,065 点，除注明外）

| 类别 | 操作 | 耗时 | 备注 |
|---|---|---|---|
| IO | `t.io.read_point_cloud` / legacy | 1.52 s / 2.32 s | 二进制 PLY xyz + 法向 |
| 近邻 | `core.nns` KNN 建索引 5M | 3.9 s | 平均最近邻间距 1.08 m（中位 1.02，P95 2.07） |
| 降采样 | 张量 `voxel_down_sample` 0.5 / 2 / 8 / 16 m | 8.7 / 7.7 / 4.2 / 3.8 s | 输出 4.88M / 3.38M / 370k / 83k |
| | legacy `voxel_down_sample` 1 m / 4 m | 10.4 / 2.5 s | 输出越多越慢 |
| | **numpy 排序式“每格取一点” 4 m** | **0.95 s** | 对比 Open3D 同尺寸 4.9 s |
| 清洗 | 统计离群（k=20, 2σ）3.38M 点：张量 / legacy | 8.8 / 6.2 s | 剔除 2.2% |
| | 半径离群（n=4, r=6 m） | 6.7 s | |
| 法向 | `estimate_normals`（30, r=6 m）3.38M：张量 / legacy | 6.0 / 7.1 s | |
| 分块 | `pca_partition(max=100k)` | 0.79 s | 75 块 |
| 采样 | `farthest_point_down_sample` 370k→5k | **311 s** | 禁用 |
| **碰撞代理** | DSM 4 m / 2 m（numpy）栅格化 + 建网格 | 0.6 / 1.5 s | 116 万 / 464 万三角形；空洞率 8% / 37%（需补洞） |
| | BVH 提交（首次查询）116 万 / 464 万三角形 | 0.97 / 4.0–5.1 s | 464 万三角形 RSS 增加 534 MB |
| | Shanghai DSM 4 m（600 万三角形） / 2 m（2400 万三角形） | 提交 6.4 s / **46 s**；RSS +0.65 / **+2.36 GB** | 大城市用 4 m 或分块 |
| **LiDAR** | 2 万条光线（天空方向为主） / 10 万条 | 3.2 / 8.9 ms | 6–11 M rays/s |
| | 倒装朝下 MID-360 图案：10 架 × 2 万 / 50 架 × 2 万 | 61 / 327 ms | ≈3.1–3.3 M rays/s（命中型） |
| 相机 | 640×480 深度图（`create_rays_pinhole` + `cast_rays`） | 15 + 26 ms | |
| 查询 | `compute_distance` 10 万点 / 100 点 | 270 / 2 ms | |
| | 局部 ESDF 64³ @1 m（262k 点） | 167 ms | 32³ 约 21 ms |
| | 航段碰撞 1 万条（`cast_rays` t<1） / 视线 `test_occlusions` 1 万条 | 2 / 3 ms | |
| | 高度场占据查表 10 万点（numpy） | 6 ms | |
| 占据 | `core.HashSet` 插入 338 万体素键 / 查询 10 万 | 1.36 s / 9 ms | numpy `searchsorted` 查询 22 ms |
| | legacy `VoxelGrid.create_from_point_cloud` 2 m | 14.7 s | 不推荐 |
| 动态障碍 | 每 tick 重建 N 个无人机球体（120 三角形 / 个）小场景 + 投射 | N=10：13.5 ms；N=50：65 ms | 解析求交更省（§3.5） |

配准与重建见 §3.7、§3.8。所有耗时都是本机 8 核慢速 Xeon 的数字，现代 16 核桌面 CPU 预计快 2–4 倍。

### 3.1 World Ingest 规范化：轴 / 单位 / 原点（MVP 必做）

实测 6 个城市（`data/raw/urbanscene3d/*_sampled_5m.ply`，只有 xyz + 法向，**没有颜色**）：

| 城市 | 范围（x×y×z） | 推断 |
|---|---|---|
| New York | 2928×3167×292 | Z-up，米 |
| San Francisco | 740×717×55 | Z-up，米（整体低平，z 为 −27…27） |
| Shenzhen | 1848×1999×391 | Z-up，米 |
| Shanghai | 7736×6211×645 | Z-up，米（范围最大） |
| **Suzhou** | 4407×**155**×686 | **Y-up**（平均法向 y=+0.147，z≈0） |
| **Chicago** | **4.17×8.04×0.62** | **归一化单位**，推测 ×1000 → 米（高度 616 m，与超高层城市量级吻合，需人工复核） |

另外，UrbanScene3D 仿真器使用 UE 的**左手 Z-up、厘米**坐标（README “Path format”）。采样点云是否已经转成右手系无法从数据本身确认，入库时应提供 `mirror_y` 开关，用路网 / 地标方向人工核对一次。

```python
def normalize_world(pcd, meta_override=None):
    P, N = pcd.point.positions.numpy(), pcd.point.normals.numpy()
    # 1) up 轴：平均“有符号”法向绝对值最大的轴（地面/屋顶法向同向，墙面正负抵消）
    up = int(np.argmax(np.abs(N[::50].mean(0))))           # Chicago→z(0.668)，SF→z(0.742)，Suzhou→y(0.147)
    sign = np.sign(N[::50, up].mean())
    R = axis_swap_to_z_up(up, sign)                        # 3x3 置换/翻转矩阵，保持右手系
    # 2) 单位：override 优先；否则启发式 —— 最大水平范围 < 50 视为 km 归一化
    scale = meta_override.get("scale") or (1000.0 if extent_xy_max < 50 else 1.0)
    # 3) 原点：水平取中心、竖直取 1% 分位（近似地面），float64 存入 coordinate.json
    origin = np.array([cx, cy, np.percentile(z, 1)], np.float64)
    T = compose(scale, R, -origin)
    pcd.transform(o3c.Tensor(T))                           # 局部 ENU，float32 足够
    return pcd, {"up_axis": "xyz"[up], "scale": scale, "origin_enu": origin.tolist(), "mirror_y": False}
```

**精度约束**：float32 有 24 位尾数，坐标值为 x 时的分辨率约为 `x·2⁻²⁴`。7 km 处约 0.4 mm，足够；但真实数据如果直接用 UTM（北向约 3.5×10⁶ m），分辨率约 0.25 m，会产生可见抖动。**所有几何必须以局部 ENU（原点 float64 存 `coordinate.json`）存成 float32**，前端 Three.js 也用同一个原点（RTC 相对中心渲染）。

### 3.2 预处理流水线（Open3D，离线，V0.1）

```python
pcd = o3d.t.io.read_point_cloud(src)                               # PLY/PCD；LAS/LAZ 先用 laspy 转 numpy
pcd, coord = normalize_world(pcd)                                  # §3.1
pcd, _ = pcd.remove_non_finite_points()
pcd, _ = pcd.remove_duplicated_points()
s = median_nn_spacing(pcd)                                         # core.nns knn k=2，抽样 2 万点；NY≈1.0 m
pcd, m = pcd.remove_statistical_outliers(nb_neighbors=20, std_ratio=2.0)   # 真实 LiDAR/视觉点才需要；UrbanScene3D 剔除约 2%
if not pcd.has_normals: pcd.estimate_normals(max_nn=30, radius=max(3*s, 0.5))
pcd.orient_normals_to_align_with_direction([0,0,1])                # 航拍：法向朝上
# 视觉稠密点（LingBot-Map）噪声较大时：pcd = pcd.smooth_mls(radius=3*s, max_nn=30)   # 0.20
qa = {"n": n, "spacing": s, "outlier_ratio": 1-m.mean(), "bbox": ..., "density_hist": ...}
```

参数经验：法向半径取 2–3 倍点间距；统计离群 `k=20, std_ratio=2.0`；半径离群 `r=3–6×s, n=4–8`。实测平滑效果（600 m 裁剪，叠加 σ=0.3 m 噪声）：MLS（r=3 m）0.40 s，Chamfer 0.918→0.870，F@0.3 m 21.8→27.5；Bilateral 基本没有改善；**Taubin 在稀疏点云上会变差**（1.70 / 7.0，且耗时 15 s）。

### 3.3 Web LOD 建树（Open3D 做 IO / 清洗 + numpy 分层采样）

不要用 Open3D `Octree`，也不要用均值 `voxel_down_sample`。推荐 Potree 2 风格的**无放回分层采样**：点先随机打乱，第 L 层在网格间距 `s_L = S / (G·2^L)` 下“每格取第一个（即随机）点”，被选中的点移出剩余集合；某节点剩余点数 ≤ `MAX_LEAF` 时，剩余点全部放入该节点，不再细分。

```python
S = max_extent * 1.0001; G = 128; MAX_LEAF = 20000; MAX_DEPTH = 10
U = (P[perm] - mn) / S                                # 随机置换后归一化到 [0,1)^3
remaining = arange(N)
for L in range(MAX_DEPTH + 1):
    n = 1 << L
    nk = min(floor(U[rem] * n), n-1)                  # 节点坐标
    node_key = pack(nk);  cnt = bincount_by(node_key) # 节点剩余人口
    leaf = cnt[node_key] <= MAX_LEAF or L == MAX_DEPTH
    cell = pack(min(floor(U[rem] * n * G), n*G-1))
    first = unique(cell, return_index=True)[1]        # 每格一个随机代表点
    take = zeros; take[first] = True; take |= leaf
    assign(rem[take] → (L, node_key));  rem = rem[~take]
```

实测（NY 5M 点，`.cache/research/r06/lod_proto.py`）：总耗时 **15.8 s**；根节点边长 S=3167 m；各层间距 24.7 / 12.4 / 6.2 / 3.1 / 1.5 / 0.77 m；各层点数 29.7k / 147k / 617k / 2.00M / 2.17M / 33k；共 401 个节点，每节点点数中位数 9,136、最大 92,718。城市扁平（z 只占 S 的约 9%），立方体八叉树第 1 层只有 4 个非空子节点，属正常现象。

**节点命名与布局**（建议对齐 Potree 2.0，以便复用 potree-core / three-loader；最终格式由 Potree 研究单元定稿）：
- 名称 `r` + 子索引串，子索引 `idx = (x<<2)|(y<<1)|z`（注意与 Open3D 的 `x+2y+4z` 不同）。
- `hierarchy.bin`：每节点 22 B：`type u8, childMask u8, numPoints u32, byteOffset i64, byteSize i64`。
- `octree.bin`：节点负载拼接。位置用**节点包围盒内 u16×3 量化**（`q = round((p−min)/size·65535)`，根节点误差 3167/65535 ≈ 4.8 cm，第 5 层 < 2 mm），6 B/点；法向用八面体编码 u8×2；颜色 u8×3（UrbanScene3D 没有颜色，前端按高度 / 法向着色）。约 8–11 B/点，5M 点约 50 MB。

**SSE 与点预算**（前端，供 Web 单元对照）：

```
d      = max(|c_node − cam| − r_node, near)          # r_node = √3·size/2
ρ_px   = s_L · (H_px/2) / (d · tan(fov_y/2))         # 该节点点间距投影到屏幕上的像素数
细分条件  ρ_px > τ（默认 1.5 px）；优先级 = ρ_px（相机在节点内时为 ∞）
预算选择  优先队列 best-first，累计点数 ≤ B 时加入并压入子节点
自适应    ema_ms = 0.9·ema_ms + 0.1·frame_ms
         ema_ms > 1.15·T → B *= 0.85, τ *= 1.1；连续 30 帧 ema_ms < 0.75·T → B *= 1.05, τ /= 1.05
         B ∈ [3e5, 8e6]，τ ∈ [1, 4]；相机运动中冻结最深两层的加载，停止 200 ms 后恢复
点大小    size_px = clamp(k · ρ_px(node), 1.5, 8)       # 稀疏数据（≈1 m 间距）靠放大点补洞
```

### 3.4 Geometry World：DSM 碰撞代理 + RaycastingScene（**建议 MVP 上线**）

城市航拍场景的 MVP 碰撞代理用 **2.5D DSM 高度场网格**：构建快、BVH 小，从上方看遮挡正确。代价是悬挑（桥下、树冠下）被当成实心，墙面变成一个栅格宽度的斜坡。

```python
class GeometryWorld:
    """静态几何查询：高度场 + Embree 场景 + 体素占据。只在仿真进程内使用（持 GIL）。"""
    def __init__(self, P, cell=2.0):
        self.mn = P.min(0); self.cell = cell
        self.h = dsm_grid(P, cell)                     # np.maximum.at 取每格最高点；3×3 最大值膨胀补洞 ≤8 次；剩余空洞填地面
        V, F = heightfield_mesh(self.h, self.mn, cell) # 每格 2 个三角形
        self.scene = o3d.t.geometry.RaycastingScene(nthreads=0)
        self.scene.add_triangles(o3c.Tensor(V), o3c.Tensor(F.astype(np.uint32)))
        self.scene.test_occlusions(o3c.Tensor([[0,0,1e4,0,0,-1]], o3c.float32))   # 预热：触发 BVH 提交
        self.occ = sorted_voxel_keys(P, 2.0)           # int64 打包键，np.searchsorted 查询

    def ground_z(self, xy):      return bilinear(self.h, xy)                # O(1)
    def inside(self, p):         return p[:, 2] < self.ground_z(p[:, :2])  # 高度场占据
    def distance(self, p):       return self.scene.compute_distance(T(p)).numpy()
    def sdf(self, p):            return np.where(self.inside(p), -1, 1) * self.distance(p)
    def segment_free(self, a, b):
        th = self.scene.cast_rays(T(np.hstack([a, b - a])))["t_hit"].numpy()
        return ~(th < 1.0)                                                  # 未归一化方向，t<1 即线段内命中
    def los(self, a, b):         return ~self.scene.test_occlusions(T(np.hstack([a, b - a])), 0.0, 1.0).numpy()
    def raycast(self, rays):     return self.scene.cast_rays(T(rays))
    def closest(self, p):        return self.scene.compute_closest_points(T(p))   # 点、法向、三角形 id
```

参数：城区用 `cell=2 m`（NY 为 464 万三角形，提交约 5 s）；范围 > 5 km 的城市（Shanghai）用 4 m，或按 2 km 分块、每块一个场景，只加载无人机所在块及其 8 邻域。**DSM 网格和 BVH 无法用 Open3D 序列化**，世界包中缓存 `geometry/collision/dsm_{cell}.npz`（高度栅格 + 原点 + 格宽），启动时重建网格（≤1.5 s）并提交 BVH。

同一份 DSM 网格可以 `o3d.t.io.write_triangle_mesh` 导出 OBJ/STL，作为 **PX4 + Gazebo 的 world 碰撞体**，Web、仿真与 Gazebo 共用一套几何（对应原文 §35 “共同使用同一个 World Model”）。

### 3.5 虚拟 LiDAR（MID-360 类）与深度相机

```python
PHI1, PHI2 = 0.6180339887, 0.7548776662                  # 黄金分割 / R2 低差异序列 → 近似“非重复扫描”
def mid360_dirs(n, frame, R_mount):
    i = arange(n) + frame * n
    az = 2π · frac(i·PHI1)
    u  = frac(i·PHI2);  s0, s1 = sin(−7°), sin(52°)
    el = arcsin(s0 + u·(s1 − s0))                         # 在 −7°~52° 带内按立体角均匀
    d  = [cos el·cos az, cos el·sin az, sin el] @ R_mountᵀ   # P600 倒装：R = diag(1,−1,−1)（外参按 r05 §3.7）
    return d

def lidar_tick(drones, world, env, budget_rays_per_s=1.0e6, hz=10):
    n_i = allocate(budget_rays_per_s/hz, drones, weight=selected?4:1)   # 全局光线预算，按权重分给各机（上限 2 万/机/帧）
    rays = concat([hstack([p_i.repeat(n_i), mid360_dirs(n_i, frame, R_i)]) for i])
    th = world.raycast(rays)["t_hit"].numpy()                          # 所有无人机合成一次调用（TBB 吃满多核）
    th = min(th, ray_vs_drones(rays, drones, r=0.6))                   # 动态障碍：解析球求交，只测 R_max 内的邻机
    β  = env.extinction(p_i)                                            # 雾：β = 3.912 / V（Koschmieder）；雨 / 沙：β = f(强度)，按 r05 §3.6 标定
    Rmax = solve(ρ·exp(−2βR)/R² = ρ_ref/R_ref²)                         # R_ref = 40 m @ ρ_ref = 0.1；迭代 R ← R_ref·√(ρ/ρ_ref)·exp(−βR)
    keep = (th > 0.1) & (th < Rmax) & (rand > p_drop(β, th))
    r_noisy = th + N(0, σ_r(β, th))                                     # σ_r = σ0·(1 + k_r·β·r)，σ0 = 0.02 m
    return per_drone_split(points = o + d·r_noisy, normals = primitive_normals)   # 法向可作反射率 / 入射角代理
```

- **预算**：本机命中型光线约 3 M rays/s。满规格 MID-360（10 Hz × 2 万）每机约 6.5 ms / 帧，10 架约 61 ms，占一个 100 ms tick 的 60%。建议全局预算 1 M rays/s（约 33% CPU）：选中机满规格，其余机降到 5k 光线 / 帧或 5 Hz。浏览器端每机每帧只下发 2–5k 点（与 r04 §3.3 一致）。
- **对比 r04 / r05 的 numpy 方案**：球面 z-buffer 每帧 9–80 ms，需要预降采样和膨胀补洞，遮挡也只是近似；Embree 方案每帧约 6 ms、遮挡精确、没有补洞参数。numpy 方案仍可作为“无 Open3D 环境”的回退。
- **动态障碍**：Open3D 场景不能移动几何。每 tick 重建一个只含 N 个无人机的小场景要 13–65 ms，不划算。直接解析求交更便宜：`b = (o−c)·d, disc = b² − (|o−c|² − r²), t = −b − √disc`，先按距离筛出量程内的邻机，每机只测这些。
- **深度 / RGB 相机**：`create_rays_pinhole(K, T_cw, w, h)` 生成光线再 `cast_rays`；640×480 约 41 ms，可支撑 5–10 Hz 的 FPV 深度 / 语义掩码（`geometry_ids` 可映射语义层）。

### 3.6 碰撞、间隙、SDF 与避障（mock 动力学直接可用）

- **100 Hz 物理步**：只做高度场查表 `z < h(x,y) + r_drone`，10 万点 6 ms，每机近似零成本。触发时再用 `segment_free(p_prev, p_now)` 精确确认，避免高速穿模。
- **10–20 Hz 间隙遥测**：`compute_closest_points(p)` → `d = |p−q|`，`n̂ = (p−q)/d`，推送 `min_clearance` 到 UI。
- **人工势场避障**（V0.2 mock 控制器、V0.6 多机避障的基线）：

  `F_rep = k_rep·(1/d − 1/d₀)·(1/d²)·n̂`（d < d₀，否则为 0），默认 `d₀ = 8 m, k_rep = 30`；与跟踪力 `F_att = k_p·(p_ref − p) + k_d·(v_ref − v)` 相加后限幅 `a_max = 4 m/s²`。

- **局部 ESDF 块**（给 Fast-Planner / EGO 类规划器）：32³ @1 m 分块惰性计算，符号来自高度场。`compute_distance` 在 32³ 块上约 21 ms，按 LRU 缓存 256 块。
- **视线 / 通信遮挡**（ANet 中继、V1.0）：两两 `los(a,b)`；50 架共 1225 对，低于 1 ms。
- **占据体素**：2 m 体素键用 int64 打包排序后 `searchsorted`（22 ms / 10 万，纯 numpy，易序列化进世界包），或 `core.HashSet`（9 ms / 10 万，但构建 1.4 s 且不可直接序列化）。A* / 覆盖规划用它；legacy `VoxelGrid` 构建 14.7 s，不用。

### 3.7 配准（V0.5 Real World Fusion）

测试设置：NY 300 m×300 m 裁剪（6.9 万点），源点云为目标的 70% 随机子集，加 5 cm 噪声后施加已知变换。**合成数据偏乐观**，真实的视觉<->LiDAR 跨模态配准会更难。

| 场景 | 方法 | 参数 | 耗时 | 误差（旋转 / 平移） |
|---|---|---|---|---|
| 小扰动（3°, 2.6 m），类似 RTK 初值 | 张量 `multi_scale_icp` 点到面 | voxel [2,1,0.5] m；max_corr [6,3,1.5] m；iter [30,20,10] | 0.71 s | 0.0002° / 0.9 mm |
| | 张量 `multi_scale_icp` 点到点 / 点到面 + Tukey(1.0) | 同上 | 1.00 / 1.05 s | ≈0 / ≤1 mm |
| | 张量 `icp` 点到面单尺度 / `registration_symmetric_icp` | max_corr 6 m | 0.23 / 0.28 s | ≈0 / 1 mm |
| | legacy GICP / legacy 点到面 ICP | max_corr 6 m | 0.15 / 0.10 s | ≈0 / 1 mm |
| | legacy **NDT**（0.20 新） | voxel 4 m，其余默认 | 0.19 s | **2.15° / 1.72 m（失败，需调参）** |
| 大扰动（60°, 47 m），无先验 | FPFH（2 m 体素，r=10 m）×2：legacy / 张量 | max_nn 100 | 0.74 / 0.32 s | — |
| | legacy RANSAC（mutual，EdgeLength 0.9 + Distance 3 m） | ransac_n 3，1e5 次，0.999 | 3.8 s | 0.14° / 2.8 cm |
| | + `SourceRotation([5°, 5°, free])` | 用 IMU 重力约束 roll/pitch | 4.4 s | 0.16° / 31 cm |
| | legacy FGR | max_corr 3 m | 5.9 s | 0.76° / 4.2 m |
| | RANSAC → 张量 multi_scale ICP 精修 | 同第一行 | 0.68 s | 0.0002° / 0.6 mm |

推荐流程（与 r05 的“轨迹先行”一致，Open3D 负责最后两步）：LIO 轨迹 + RTK → Sim(3) 对齐 LingBot-Map 轨迹 → **Open3D `multi_scale_icp`（点到面 + Tukey，或 GICP）修残差**；没有先验的航次合并用 FPFH + RANSAC（FPFH 半径取 5×体素，法向半径取 2–3×体素，RANSAC max_corr 取 1.5×体素，带 SourceRotation）→ `PoseGraph + global_optimization`（LM，`edge_prune_threshold=0.25`）。每条边输出 `fitness / inlier_rmse / get_information_matrix`，写入世界包 `qa/registration.json`，供 UI 标注世界可信度。

### 3.8 网格与 TSDF（V0.5+，离线）

| 方法 | 参数 | 结果（6.9 万点裁剪） | 适用 |
|---|---|---|---|
| Poisson | depth 8 / 9，`n_threads=-1` | 10.2 s → 39.8 万三角形 / 12.3 s → 45.8 万 | 闭合但会“幻觉”封口，需按 densities 剔除 5% 低密度顶点 |
| Ball Pivoting | radii [1.5, 3] m，1 m 体素，6.3 万点 | 1.8 s → 10.7 万三角形 | 保形但有孔 |
| VBG 点到平面 TSDF | 1 m 体素，8³ 块，trunc 3 m；`sdf = n_q·(v − q)`（q 为最近点） | 融合 7.5 s（9,886 块 / 506 万体素）+ 提取 0.41 s → 96 万三角形 | 可控、可增量，支持 SDF 查询 |
| 二次简化 | `simplify_quadric_decimation(0.8)` | 15.7 s → 19.3 万三角形 | 导出碰撞体 / Gazebo |

点到平面 TSDF 的核心（改自 `integrate_custom.py`）：

```python
vbg = VoxelBlockGrid(("tsdf","weight"), (f32,f32), ((1),(1)), voxel_size=1.0, block_resolution=8, block_count=200000)
blocks = vbg.compute_unique_block_coordinates(pcd, trunc_voxel_multiplier=3.0)
vbg.hashmap().activate(blocks); buf, _ = vbg.hashmap().find(blocks)
v, idx = vbg.voxel_coordinates_and_flattened_indices(buf)
i, d2 = nns.knn_search(v, 1)                          # core.nns，已建 knn_index
sdf = ((v − P[i]) * N[i]).sum(1);  valid = sqrt(d2) < trunc
tsdf[idx[valid]] = clip(sdf, −trunc, trunc)/trunc;  weight[idx[valid]] = 1   # 多帧时改为加权平均
mesh = vbg.extract_triangle_mesh(weight_threshold=0.5)
```

同一框架把属性换成 `log_odds`，即可做 LiDAR 占据融合（V0.5 真机建图的增量障碍层），更新式为 `L ← clamp(L + l_hit/l_miss, L_min, L_max)`。

### 3.9 融合质量评估（V0.5 QA，V0.1 起记录基线）

`pcd.compute_metrics(ref, [ChamferDistance, HausdorffDistance, FScore], MetricParameters(fscore_radius=[0.1, 0.3, 1.0]))`。输出写入 `qa/metrics.json`，用于比较 LingBot-Map 视觉点与 LiDAR 地图、不同航次地图、网格采样点与原始点，UI 用 lieflat 风格的指标表展示。

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块落点表

| 本项目模块（01-design.md） | Open3D API | 版本 | 方式 | 说明 |
|---|---|---|---|---|
| `world/georef`、World Ingest（§41） | `t.io.read_point_cloud`、`transform`、`remove_*`、`estimate_normals`、`compute_metrics` | V0.1 | adopt | §3.1–3.2；LAS/LAZ 由 laspy / PDAL 负责 |
| `world/pointcloud`（Web LOD） | 仅 IO / 清洗；分层采样自写 numpy | V0.1 | port（自写） | §3.3；legacy Octree、均值降采样不用 |
| `world/geometry`：Geometry World 服务 | `RaycastingScene`、`core.nns`、`core.HashSet` | **V0.2** | adopt | §3.4、§3.6；仿真进程内库，不走 HTTP |
| `sensors/lidar`、`sensors/rgb`（仿真） | `cast_rays`、`create_rays_pinhole` | V0.2（几何） → V0.4（环境退化） | adopt | §3.5 |
| `swarm/avoidance`、`swarm/planning` | `compute_closest_points`、ESDF 块、`test_occlusions` | V0.2 基线 → V0.6 | adopt | §3.6 |
| `reconstruction/registration`、`fusion` | 张量 ICP 家族、legacy RANSAC / PoseGraph、FPFH | V0.5 | adopt | §3.7；高频里程计用 FAST-LIO / small_gicp |
| `world/voxel`、`world/sdf`、`geometry/collision` | `VoxelBlockGrid`、Poisson / BPA、`simplify_quadric_decimation`、`CreateIsosurfaces` | V0.5 | adopt | §3.8；导出 Gazebo 世界 |
| `world/semantic`（粗分割） | `segment_plane`（地面：1.0 s / 25.9 万点）、`cluster_dbscan`（0.25 s / 17.6 万点 → 766 簇）、`get_oriented_bounding_box` | V0.3 | adopt | 地面 / 建筑实例粗分，给语义层与禁飞区编辑器做底 |
| `visual/gaussian`（3DGS） | `t.io` 的 PLY / SPLAT / SPZ 读写 | V1.0 | reference | 转码给 Web splat 渲染器 |
| 可视化 / WebRTC / GUI | `visualization.*` | — | skip | 前端自研 |

### 4.2 无头服务器部署

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libegl1 libgl1 libx11-6 libudev1 libusb-1.0-0 libidn2-0 && rm -rf /var/lib/apt/lists/*   # = libOpen3D.so NEEDED 列表
RUN pip install --no-cache-dir open3d==0.20.0 numpy          # 或 open3d-cpu==0.19.0（216 MB，无 EGL，缺 0.20 特性）
ENV EGL_PLATFORM=surfaceless                                  # 仅在需要离屏渲染时有用；纯计算不需要
```

- **无 root 的本机方案（本研究已验证）**：`apt-get download libegl1 libegl-mesa0` → `dpkg -x *.deb root/` → `LD_LIBRARY_PATH=root/usr/lib/x86_64-linux-gnu python ...`。脚本见 `.cache/research/r06/run.sh`。后续的实现 agent 可以直接复用这套本地 lib，或者在 Makefile 的 `dev-setup` 里自动完成这一步。
- 线程：仿真进程调用 `o3d.utility.set_max_threads(cores−1)`，给 API 进程留一个核；`RaycastingScene(nthreads=…)` 也可以单独限制 Embree 线程数。
- **进程拓扑**：`api`（FastAPI / uvicorn，REST + WebSocket 扇出）与 `sim-core`（`multiprocessing.Process`：100 Hz 动力学 + 10 Hz 传感器 + GeometryWorld）分开，二者之间用 `multiprocessing.Queue` 或 ZeroMQ PUB/SUB 传遥测和指令。原因见 §2.1 的 GIL 问题。**不要用 `asyncio.to_thread` 包 Open3D 查询**：持有 GIL 的 C++ 调用会卡住事件循环。
- 内存：每个进程加载 `libOpen3D` 约 0.8 GB 映射，加上场景（NY 2 m 约 0.5 GB）。不要让多个 uvicorn worker 各自 import open3d。
- CI：import 失败时 Geometry World 自动降级到纯 numpy 实现（高度场 + 排序体素 + r04 的 z-buffer LiDAR），保证 MVP 能在没有 EGL 的环境里跑通。

### 4.3 World Package 增补（对应 §41）

```
worlds/<id>/
  coordinate.json            # origin_enu(float64), up_axis, scale, mirror_y, crs
  geometry/pointcloud/octree/{metadata.json, hierarchy.bin, octree.bin}
  geometry/collision/dsm_2m.npz        # 高度栅格 float32 + origin + cell（启动时重建网格和 BVH）
  geometry/collision/mesh_lod{0,1}.obj # V0.5：TSDF / Poisson 简化网格；同时供 Gazebo 使用
  geometry/voxel/occ_2m.npy            # 排序 int64 体素键
  geometry/sdf/chunks/…                # 可选：预计算 ESDF 块
  qa/{metrics.json, registration.json, ingest.json}
```

---

## 5. 对比与推荐

本单元只有一个仓库，下面按功能把 Open3D 与项目内其他候选（其他研究单元的仓库）及自研方案对比：

| 功能 | Open3D | 替代 | 推荐 |
|---|---|---|---|
| 光线投射 / 距离 / SDF | `RaycastingScene`（Embree 4，CPU / SYCL，实测 3–11 M rays/s） | numpy z-buffer（r04 / r05，9–80 ms/帧，近似）；trimesh + embreex（功能少）；Isaac Sim / Warp（需 GPU） | **Open3D**，numpy 作回退 |
| 局部配准 | 张量 ICP 家族、GICP、Symmetric、NDT | small_gicp（更快、依赖少、多线程）、fast_gicp（CUDA） | 离线精配和 QA 用 Open3D；在线 / 大批量用 small_gicp |
| 全局配准 | FPFH + RANSAC（含 0.20 的旋转先验）、FGR | TEASER++（未在 refs 中） | Open3D RANSAC |
| 点云 IO / 重投影 | PLY / PCD / PTS / NPZ，无 LAS / 坐标系 | PDAL、laspy | PDAL / laspy 负责 LAS 与 CRS，Open3D 负责几何 |
| Web LOD 建树 | 无可用实现 | PotreeConverter 2、自研 numpy（§3.3） | PotreeConverter 或自研；不用 Open3D |
| TSDF / 网格 | VBG、Poisson、BPA、二次简化、Flying Edges | nvblox / OpenVDB（需 GPU / 另构建） | Open3D（离线） |
| 占据查询 | HashSet、VoxelGrid | numpy 排序键、OpenVDB | numpy 排序键（可序列化），HashSet 次之 |

推荐排序（综合 star、2026 活跃度、契合度）：**Open3D（14k stars，2026-09 发布 0.20，Geometry World 与离线流水线的核心依赖）> small_gicp（在线配准）> PDAL / laspy（IO 与 CRS）> PotreeConverter / 自研（LOD）**。

---

## 6. 风险与注意事项

1. **EGL 依赖**：0.20 的 Linux wheel 硬依赖 `libEGL.so.1`，最小化容器或 CI 会 import 失败；`open3d-cpu` 停在 0.19。缓解：Docker 装 `libegl1 libgl1`；本地解包；numpy 降级路径。
2. **GIL**：`RaycastingScene` 持有 GIL（ICP 等部分函数会释放）。与 Web 服务同进程时，遥测会卡顿数十毫秒。必须独立进程，并按 tick 批量调用。
3. **不支持 CUDA 光线投射，也不能移动或删除几何**：动态物体用解析求交或每 tick 重建小场景；世界增量更新需要重建场景（464 万三角形约 5 s，要在后台线程构建新场景再原子替换）。
4. **BVH 规模**：Shanghai 2 m DSM 为 2400 万三角形，提交 46 s、内存 +2.36 GB。大城市用 4 m 或分块，也可以对 DSM 做 RTIN / 二次简化（平屋顶和地面可大量合并）。
5. **SDF / 占据要求水密**：DSM 不闭合，`compute_signed_distance / compute_occupancy` 的奇偶投票会出错，要用高度场判断符号；Poisson 网格封口会在开阔城区“造墙”，必须按密度裁剪。
6. **降采样陷阱**：`voxel_down_sample` 输出均值点，会跨表面平均；张量版在 CPU 上 4–9 s / 5M 点；`farthest_point_down_sample` 是 O(N·k)（37 万→5000 需 311 s）；legacy `VoxelGrid` 构建 14.7 s。
7. **API 两代并存**：legacy 默认 float64、tensor 默认 float32；Python dtype 是小写 `o3c.float32`；`multi_scale_icp` 要求 `DoubleVector`；RANSAC / FGR / PoseGraph / Octree / Poisson 只有 legacy；不同 geometry 类型要用 `from_legacy / to_legacy` 显式转换。
8. **算法调参**：NDT 默认参数在 2.6 m 初值下失败；FGR 精度明显低于 RANSAC；MLS 在稀疏点云上收益有限，Taubin 反而更差；`extract_*` 的 `weight_threshold=3` 在单帧融合时会得到空结果。
9. **数据异构**：UrbanScene3D 各城市单位 / 轴向不同（Chicago 归一化、Suzhou Y-up），且可能是 UE 左手系镜像；数据只有 xyz + 法向，没有颜色，平均点距约 1 m（稀疏），前端需要按高度着色并放大点尺寸。
10. **精度**：直接使用 UTM / WGS84 大坐标的 float32 会带来 0.25 m 级误差，必须局部 ENU 化。
11. **体积与冷启动**：wheel 约 0.9 GB；世界加载（读 PLY 1.5 s + DSM 1.5 s + BVH 5 s）约 8 s，应在 `sim-core` 启动时预热，并在 UI 上显示“几何就绪”状态。
12. **许可**：Open3D 本身是 MIT；UrbanScene3D 数据仅限非商业（本项目为科研，可忽略，但不要把数据打进公开发行包）。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§6 Open3D 的职责要重新界定**。原文把 Open3D 定位为“ICP / Registration / Voxelization / Mesh Reconstruction”的主力。建议改为：“Open3D = **离线几何流水线 + 在线几何查询内核**”。在线部分（RaycastingScene）是它对本项目最大的价值；高频 LiDAR 里程计与在线配准交给 FAST-LIO / small_gicp；Open3D ICP 只做最终精配、PoseGraph 合并和 QA。原文“后续引入 PCL / CUDA Point Cloud Kernels”可以删掉：Open3D 张量 API 已经覆盖 CUDA / SYCL，配准加速选 small_gicp / fast_gicp 即可。
2. **§8 Geometry World 需要分层与接口契约**。建议写明碰撞 / 查询代理的分层：L0 DSM 高度场（V0.2）→ L1 占据体素（V0.2）→ L2 TSDF / Poisson 网格（V0.5）→ L3 ESDF 块（V0.6 规划）。统一查询接口 `raycast / segment_free / los / distance / sdf / occupied / closest / lidar_scan / depth_image`（§3.4），所有仿真后端（mock、PX4 + Gazebo、Isaac）和传感器模拟共用这一套接口。
3. **§7 World Model 缺“坐标规范化”与“精度”**。应增加 `coordinate.json` 字段：`origin_enu(float64)`、`up_axis`、`scale`、`mirror_y`、`crs`；并规定“几何一律用局部 ENU float32，原点用 float64”。UrbanScene3D 实测已经出现单位和轴向不一致（§3.1）。
4. **§14–15 点云 LOD 要落到可实现的规格**：明确“无放回分层采样 + Potree 2 兼容节点布局 + u16 节点局部量化 + SSE 阈值 τ + 点预算 B + FPS 反馈控制”（§3.3）。同时写明不采用 Open3D Octree 和均值体素降采样，并注明子节点索引顺序（Potree `(x<<2)|(y<<1)|z` ≠ Open3D `x+2y+4z`）。
5. **§22–24 环境 → 传感器的影响不必等 Isaac Sim**。有了 RaycastingScene，V0.3–V0.4 就能在服务端做“几何精确 + Beer–Lambert 退化”的 LiDAR 和深度相机仿真（§3.5）。Isaac Sim 退为高保真外观（RGB / 材质）选项，不再是“传感器物理”的唯一来源。
6. **§35 Simulation Backend 要共享几何**：原文说 Gazebo 与 Isaac “共同使用同一个 World Model”，但没说怎么做。建议由 Open3D 导出 DSM / 简化网格（OBJ / STL）作为 Gazebo world 的碰撞体，保证 Web 所见、mock 碰撞、SITL 碰撞三者一致。
7. **§37 频率表缺“查询预算”**：增加一行“传感器仿真 10 Hz，全局光线预算 ≈1 M rays/s（8 核慢速 CPU 约占 33%）”和“碰撞：100 Hz 高度场查表 + 触发时线段精检”，并规定多机时按选中 / 优先级分配光线数。
8. **§33 后端栈补充部署约束**：`open3d==0.20.0` 加系统库 `libegl1 libgl1`；独立 `sim-core` 进程（GIL）；`o3d.utility.set_max_threads`；import 失败时降级到 numpy。
9. **§41 World Package 增加 `geometry/collision/`、`geometry/voxel/` 缓存和 `qa/`**（§4.3）。`qa/metrics.json` 里的 Chamfer / F-score / 配准 fitness 同时作为 UI 上“世界可信度”徽标的数据源。
10. **§43 MVP 链路要补一环**：原 MVP 是 `Point Cloud → Octree → Three.js → Drone Movement`，缺“几何”。建议改为 `Point Cloud → [Ingest 规范化] → Octree (Web) + DSM/RaycastingScene (Server) → Drone Movement with collision/clearance → mock LiDAR`。这一环构建成本不到 10 s，却让无人机 mock 从“飘在点云里”变成“会撞墙、有间隙、有传感器”，直接支撑 V0.2–V0.4 的演示价值。
11. **§29–30 多机与控制**：给出 V0.2 的避障基线（人工势场 + 最近点，§3.6），并把 `min_clearance`、`los_ok` 放进 `DroneState.health`，作为 UI 告警与 ANet 协作（中继 / 视线）的共同数据源。
