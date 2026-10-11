# r4 研究笔记：网格激光雷达仿真与每机私有重建（MeshLidar）

> 研究单元：r4-mesh-lidar ｜ 对应：用户设计讨论 `design-r1.md` 第六节（传感器生成实际数据、让 LiDAR 真正在沙盘内重建地图）、建议模块 M25 Sensor Generation（LiDAR 部分）与 M26 Agent Observation SDK（私有地图）、阶段 P1（Mesh LiDAR + 私有地图 + 协同探索）
> D2 现状代码：`python/awr/sim/sensors/lidar/scan_rt.py`（MID-360 花样、2 m DSM 柱体 DDA、识别物 AABB、反射率量程、计数器 RNG）、`sensors/lidar/pattern.py`、`sensors/lidar/frame.py`、`sensors/backends/base.py`（`SensorBackend` 协议）。本研究未改动项目代码。
> 实验机：dmax（Ubuntu 22.04，驱动 580.95.05 / CUDA 13.0，A40 48 GB）。只用 GPU 2、3（**两卡与其他 D3 单元共用**，实测期间 GPU 2 曾被他人进程占满 48 GB，因此后段实验改在 GPU 3），容器 `--cpus 16 --cpuset-cpus 48-63`，容器名 `d3-r4-mesh-lidar-*`，实验结束无残留容器。
> 镜像：`d3-r4-lidar:0.3`（python:3.12-slim + warp-lang 1.18.0、mitsuba 3.9.1 / drjit 1.5.0、open3d 0.20.0、embreex 4.4.0、trimesh 5.1.1、numba 0.68、rosbags 0.11.6，2.63 GB，保留）。另借用（只读、自起容器）r1 的 `d3-r1-fast-livo2:noetic` 跑 FAST-LIVO2，借用 r3 的 `d3-r3-camera-render:v1`（torch 2.7.1 + nvdiffrast 0.4.0）跑光栅化近似对照。
> 脚本：`.cache/research/d3/r4-mesh-lidar/scripts/`；原始结果：`.cache/research/d3/r4-mesh-lidar/results/`；dmax 工作目录 `/data/anet-drone4d-d3/r4-mesh-lidar/`（12 个模拟 bag 共约 2.2 GB 留在 `out/bag/`，其中 `sim_*_z.bag` 为正确的全点合同，供 r1 复用）。

---

## 0. 结论速览

| 方案 / 组件 | 实测要点（town 小镇 200 万三角形，MID-360 每帧 2 万射线） | 结论 | 推荐度 |
|---|---|---|---|
| **NVIDIA Warp `mesh_query_ray`（GPU，软件 BVH）** | BVH 构建 0.26 s（cubql）/ 1.2 s（SAH）；核函数 250–790 M 射线/s（视场景与命中率）；完整传感器管线（逐点位姿 + 纹理反射率 + 32 个动态实体 + 雨雾）100 架 × 1 帧 = 6.0 ms；单卡端到端（含 GPU 压缩与回传有效点）400 架 44 ms/tick | **adopt**：D3 MeshLidar 默认后端（纯 pip、Python 内核、实体实例化、refit、Marching Cubes 一站式） | 5/5 |
| OptiX（经 Mitsuba 3 / Dr.Jit `cuda_ad_rgb`，RT Core） | 核函数 2.3–5.2 G 射线/s（≥200 万射线批），比 Warp 快 4–11 倍；但 2 万射线小批只有 80–190 M/s，且主机往返占主导 | **reference**：千架级或每帧 20 万点稠密雷达时再做 C++ OptiX 模块；不建议把 Mitsuba 当生产依赖 | 3/5 |
| Open3D `RaycastingScene`（Embree 4，CPU 16 线程） | 11–20 M 射线/s；构建 1.0–1.6 s；与 GPU 结果逐射线一致（距离差 ≤ 0.3 mm）；**调用期间持 GIL** | **adopt（回退）**：无 GPU 部署（公开站沙盒）与单元测试的金标准 | 4/5 |
| embreex（Embree，单线程） / trimesh 原生 | 3–7 M 射线/s；trimesh 原生 7.7–9.4 射线/s（不可用） | **skip** | 1/5 |
| D2 2 m DSM 柱体 DDA（numba） | 6–15 M 射线/s（串行），17–40 M（并行）；**相对网格测距误差中位 0.7–1.7 m，街道低空 41% 射线命中与否判错** | 只保留给公开站 2 Hz 预览；**不得用于数据集** | — |
| nvdiffrast 立方体深度近似（r3 镜像） | 每机每帧 20 ms（6 面×光栅化 200 万三角形），2048² 时误差 p99 0.23 m，256² 时 p99 3.1 m | **skip**（比精确光线求交慢约 280 倍且有边缘混合误差）；光栅化留给 RGB/深度相机 | 1/5 |
| 运动畸变（逐点位姿，K=11 样本/帧） | 开销 +1%；8 m/s 时把真实畸变当"单一位姿"使用误差中位 0.47 m、90°/s 偏航时 p95 3.5 m；FAST-LIVO2 在 8 m/s 时逐点 4.0 cm vs 单一位姿 43 cm | **adopt**：逐点时刻求交是真实 LiDAR 输出，默认开 | 5/5 |
| 动态实体（局部系网格 + 包围球剔除 / 合并网格 refit） | 64 个实体相对无实体 +11%；256 个实体合并网格 refit 0.27 ms | **adopt** 两种模式（≤64 近邻实例化，更多用 refit） | 5/5 |
| 反射率（纹理亮度 → 905 nm 近似 × 入射角） | +6%；回波 u8 中位 23–46（常数 ρ = 0.2 时为 30，均按 Livox 0–150 漫反射编码） | **adopt**（置信度 C，待真机标定） | 4/5 |
| 多回波（波束足印子射线 M 条） | 成本 ×M；街道低空 7–20% 命中射线出现 ≥ 2 个回波（几何边缘） | **option**（MID-360 规格为单回波；摄影测量网格无植被穿透） | 2/5 |
| 雨雾雪消光 + 雨滴假回波 | 905 nm 下 10 mm/h 雨量程只降约 3%，主要影响是近场假回波；V=200 m 浓雾量程降约 30% | **adopt**（经验参数，置信度 C） | 4/5 |
| 输出：Livox CustomMsg（ROS1/ROS2）、PCD、PLY、`awr.sensor.lidar_frame.v1` | 自写 ROS1 字节拼装 1.0 ms/2 万点，与 rosbags 序列化逐字节一致、md5 `e4d6829b…` 与 livox_ros_driver 一致；rosbags 对象路径 145 ms | **adopt**；`lidar_frame` schema 的 `point_count ≤ 22000` 需升 v2 | 5/5 |
| **与 FAST-LIVO2 闭环**（LIO 模式，r1 镜像） | 全点流 bag（含无回波零点，与 livox_ros_driver2 一致）：ATE RMSE 1.7 cm（4 m/s，234 m）、4.0 cm（8 m/s，468 m）；同样航迹若用 D2 式单一位姿，8 m/s 时 ATE 43 cm（约 11 倍）；若只发有效点，FAST-LIVO2 的"时间跳变修复"会改坏点时刻，ATE 恶化到 7.8 / 36 cm；每实例 1.5–1.8 核、2.0–3.9 GB | **adopt**：bag 合同已验证可直接回放 | 5/5 |
| 每机私有地图（Warp 稠密 log-odds + 投影 TSDF + Frontier） | 积分 0.14 ms/帧，Frontier 0.7 ms；3 机各 60 s：单机覆盖 22–31%，合并 63%，两两 Jaccard 0–0.24；占据体素中位距真值 0.13 m | **adopt 原型**，生产改稀疏块（单机 60 s 约 32–43 MB vs 稠密 831 MB） | 4/5 |

一句话结论：

1. **D3 的 LiDAR 应改为"GPU 网格逐射线求交 + 逐点时刻 + 传感器物理"的 MeshLidar 后端，不再沿用 D2 的 2 m DSM。** 在小镇 GLB 上，D2 的 DSM 与真实网格的测距差中位约 1 m、p99 达 3–12 m，街道低空近一半射线的命中判断就是错的；而 Warp 网格求交每架每帧只要约 0.07 ms（核函数），完整传感器模型也只多 10–20%。单张 A40 端到端（含有效点压缩与回传）可实时支撑约 500 架 MID-360（实测 400 架 44 ms/tick）。
2. **扫描数据已能驱动真实 SLAM，而且逐点时刻必不可少**：GPU 模拟的 MID-360 + 内置 IMU 写成 Livox CustomMsg bag，只改配置文件，FAST-LIVO2（LIO 模式）在 63 s 航迹上 ATE 1.7 cm（4 m/s）、4.0 cm（8 m/s）。同样的航迹若按 D2 语义用单一位姿生成，8 m/s 时 ATE 变成 43 cm——LIO 的 IMU 去畸变会把不存在的畸变"纠正"进地图。另一个必须遵守的细节：每帧要像 livox_ros_driver2 那样输出全部 2 万个点（无回波为零点），只发有效点会触发 FAST-LIVO2 `avia_handler` 对 > 1 ms 时间间隔的"修复"，ATE 恶化 4–9 倍。这是 design-r1 "如果要验证完整 SLAM，还必须提供有噪声的 IMU，让算法自己估计位姿" 一节的可执行版本，可作为 r1 闭环与 CI 回归基线。
3. **MID-360 的视场决定了策略空间**：倒装（测绘预设，视场 −52°~+7°）正下方有半角 38° 的盲锥；街道上空 5–15 m 有效点率 34–43%，30 m 降到 11%，45 m 只剩 3%，60 m 以上几乎为 0；P600 缺省正装前倾 20° 在 15 m 以上不到 3%。"高空看全局、低空做 LiDAR 建图"是物理约束而不是策略选择，事件观测基准需要把它写进机型/任务说明。
4. **私有地图天然各不相同**：3 架无人机在小镇不同区域各飞 60 s，单机覆盖真值表面 22–31%，两两覆盖重叠 Jaccard 0–0.24，合并后 63%——这正是 design-r1 P1 的退出标准"彼此地图不同；共享数据后能够提高探索效率"的可度量版本。

---

## 1. D2 现状与差距

### 1.1 `scan_rt.py` 的语义（逐项）

| 项 | D2 实现 | D3 需要 |
|---|---|---|
| 几何 | M04 2 m DSM 柱体（Amanatides–Woo 网格遍历），识别物 ≤ 30 个 AABB | GLB 三角网格（200–300 万三角形）+ 动态实体真实网格 |
| 频率与规模 | 慢任务 ≤ 2 Hz、每会话只 1 架 active、每片 2500 射线 ≤ 300 µs | 10 Hz、N 架同时、批量 GPU |
| 位姿 | 帧首位姿 + 速度外推 50 ms 的单一位姿（"已去畸变"近似） | 逐点时刻位姿（真实 LiDAR 输出带运动畸变） |
| 反射率 | 地物常数 0.2、识别物 0.3 或 `reflect_905` | 纹理/材质 → 905 nm 反射率 × 入射角 |
| 环境 | 无 | 雨、雾、雪消光与假回波 |
| 噪声 | 测距 0.03/0.02 m（20 m 为界），计数器 RNG 流 3 | 保留；加角度噪声、虚警；随消光增大 |
| 输出 | `awr.LidarScan8.v1`（Web 下行，cm 量化） | 另加 `lidar_frame` v2（无点数 22 000 上限）、Livox CustomMsg、真值侧车 |

### 1.2 DSM 与网格的实测偏差（同一批射线，`results/dsm_vs_mesh.json`）

| 场景 / 工况 | DSM 命中率 | 网格命中率 | 命中判断不一致 | \|Δr\| 中位 | p90 | p99 | \|Δr\| > 2 m |
|---|---|---|---|---|---|---|---|
| town，倒装，地形最低点上方 40–70 m | 38.5% | 37.7% | 0.7% | 1.04 m | 2.50 m | 5.07 m | 17.5% |
| town，倒装，DSM 上方 30–60 m | 33.4% | 32.3% | 1.1% | 0.90 m | 2.23 m | 3.87 m | 13.3% |
| town，倒装，**街道上方 5–15 m** | 79.6% | 38.7% | **40.9%** | 1.07 m | 6.11 m | 70.3 m | 29.3% |
| town，正装前倾 20°，DSM 上方 30–60 m | 2.9% | 2.8% | 0.1% | 0.69 m | 2.65 m | 5.39 m | 18.9% |
| oryahovo（290 万三角形），25–45 m | 23.2% | 18.8% | 4.3% | 0.82 m | 1.54 m | 2.82 m | 3.6% |
| hilltop（300 万三角形），25–40 m | 14.1% | 11.1% | 3.0% | 1.26 m | 5.81 m | 12.1 m | 36.8% |

DSM 偏差系统性为负（柱体取格内最高点，射线提前命中），街道低空时 DSM 把屋檐、树冠、立面都当成实心柱，命中率翻倍。结论：D2 扫描器保留作公开站预览即可，数据集与私有建图必须用网格。

---

## 2. 实验数据与预处理

| GLB | 三角形 / 顶点 | 纹理 | 世界尺寸（ENU，米，见注） | 载入 + 展平 + 纹理解码 |
|---|---|---|---|---|
| `an_entire_small_town_scanned_by_a_drone.glb`（town） | 1 996 759 / 3 087 397 | 16 × 4096² JPEG（unlit） | 535 × 320 × 28 | 5.2 s |
| `oryahovo_-_crop_1_-_metashape_texture_problems.glb`（oryahovo） | 2 879 110 / 4 432 293 | 16 × 4096² | 119 × 76 × 10 | 5.1 s |
| `anciolina_3d_hilltop_village_scan_snowy.glb`（hilltop） | 2 999 995 / 2 444 257 | 1 × 8192² | 57 × 48 × 18 | 3.2 s |

- 预处理脚本 `glb_prep.py`：trimesh 载入场景图，按节点矩阵展平，glTF Y-up → ENU Z-up（`(x, y, z) → (x, −z, y)`），平移到包围盒底面中心；输出 `V f32、F u32、UV f32、FM u16（每面材质号）` 与每材质 RGB 纹理。
- **尺度未定（交给 r2）**：town 的 Sketchfab 根节点含 ×2.5 缩放，展平后 535 m 长、高差 28 m，量级合理；oryahovo 与 town 同作者同地区但没有缩放节点，高差只有 10 m，很可能也需要 ×2.5；hilltop 只有 57 m 宽，偏小。LiDAR 的有效点率、量程截断都依赖真实尺度，本笔记的数值以 town 为准，其余两个场景只用于吞吐（吞吐只与三角形数和命中分布有关）。
- 中位边长：town 0.39 m、oryahovo 0.09 m、hilltop 0.05 m。MID-360 单帧平均角密度约 3500 点/sr（平均角间距约 1°），20 m 处点间距约 0.35 m，与 town 网格中位边长同量级；积分 1 s 后（0.5° 网格覆盖 87%，r04 §3.1.2）间距降到 0.1–0.2 m，此时 town 网格的几何细节先于传感器成为瓶颈。

---

## 3. 求交后端吞吐实测

### 3.1 方法

`bench_backends.py <backend> <scene>`：按场景包围盒或 DSM 采样 N 架无人机位姿（随机偏航、小倾角），每架一帧 MID-360 花样（`pattern.mid360_pattern`，帧号互异），世界系射线批量求交，`tmax = 100 m`（`hard_max_m`）。批规模 1 / 10 / 100 / 500 架（2 万 / 20 万 / 200 万 / 1000 万射线）。GPU 后端分别记录：端到端（numpy → GPU → numpy）、核函数、以及 **GPU 上直接由"花样表 × 每机姿态"生成射线** 的核函数（生产形态，`gen`）。每配置取 5 次中位数。参考射线（2 架 × 1 帧）存盘做后端一致性核对。

命令示例（dmax）：

```bash
cd /data/anet-drone4d-d3/r4-mesh-lidar/work
bash run.sh glb_prep.py /glb/an_entire_small_town_scanned_by_a_drone.glb cache/town
bash run.sh bench_backends.py warp:cubql town --sizes 1,10,100,500 --z 40,70 --tag main
bash run.sh bench_backends.py open3d town --sizes 1,10,100 --z 5,15 --mode street --tag low2
# run.sh = docker run --gpus '"device=2,3"' --cpus 16 --cpuset-cpus 48-63 ... d3-r4-lidar:0.3 python <script>
```

### 3.2 结果（town，倒装，40–70 m；单位 M 射线/s）

GPU 行的格式为"GPU 端生成射线的核函数 / numpy 端到端"，CPU 行为端到端。

| 后端 | 构建 | 内存 | 2 万 | 20 万 | 200 万 | 1000 万 |
|---|---|---|---|---|---|---|
| Warp cubql（gen 核函数 / 端到端） | 0.26 s | GPU 573 MB | 124 / 35 | 274 / 84 | 293 / 64 | 301 / 64 |
| Warp SAH | 1.21 s | GPU 304 MB | 84 / 24 | 226 / 50 | 250 / 48 | 258 / 58 |
| Warp median | 1.01 s | GPU 304 MB | 63 / 21 | 169 / 46 | 183 / 36 | 190 / 56 |
| Warp LBVH | 0.01 s | GPU 338 MB | 47 / 22 | 94 / 49 | 99 / 35 | 101 / 34 |
| OptiX（Mitsuba `cuda_ad_rgb`，核函数 / 端到端） | 0.37 s | 进程 RSS 611 MB | 80 / 17 | 521 / 36 | 2296 / 62 | 3252 / 37 |
| Embree（Mitsuba `llvm_ad_rgb`，16 核） | 0.46 s | 560 MB | 54 / 25 | 52 / 25 | 55 / 25 | — |
| Open3D RaycastingScene（16 线程） | 1.13 s | 396 MB | 9.7 | 11.0 | 11.8 | — |
| embreex（单线程） | 2.73 s | 218 MB | 3.4 | 2.4 | 3.2 | — |
| trimesh 原生（200 条射线） | 9.9 s | 686 MB | 7.7 射线/s | — | — | — |
| D2 DSM DDA 并行（numba prange） | 0.86 s | 157 MB | 23.7 | 27.2 | 17.3 | — |
| D2 DSM DDA 串行（与 scan_rt 相同） | 0.92 s | 155 MB | 7.2 | 5.9 | 5.9 | — |

其他场景与工况（200 万射线批，核函数）：

| 场景 / 工况 | Warp cubql | Warp SAH | OptiX | Open3D | embreex |
|---|---|---|---|---|---|
| hilltop 300 万三角形 | 708 | 573 | 2884 | 15.8 | 7.1 |
| oryahovo 290 万三角形 | 421 | 306 | 2267 | 14.4 | 4.6 |
| town 街道上方 5–15 m（命中 44%） | 318 | 304 | 2958 | 16.0 | 3.7 |
| town 正装前倾 20°（几乎全是天空射线） | 3805 | 3054 | 4609 | 23.0 | 12.8 |

一致性：所有网格后端对同一批射线的命中判断完全一致（4 万射线中至多 1 条不同），距离差 ≤ 0.28 mm，命中三角形编号一致率 ≥ 99.98%（差异是共享边上的平局）。**选哪种后端不改变数据**，可以用 Open3D 做 CPU 金标准、Warp 做生产。

### 3.3 解读与容量换算

- GPU 端到端数字（35–84 M/s）被 numpy ↔ GPU 拷贝主导，不代表生产形态。生产中射线由 GPU 按花样表生成，结果留在 GPU 上直接喂给私有建图，只把压缩后的有效点拷回（§4.4）。
- 按核函数换算单卡容量（只算求交）：

| 传感器 | 每机射线/s | Warp（约 300 M/s） | OptiX（2.3–5.2 G/s） | Open3D 16 核（12–16 M/s） | D2 DSM 串行 |
|---|---|---|---|---|---|
| MID-360（2 万点/帧 × 10 Hz） | 20 万 | 约 1500 架 | 1 万架以上 | 60–80 架 | 30–70 架（几何错误） |
| 稠密雷达（20 万点/帧 × 10 Hz） | 200 万 | 约 150 架 | 1100–2600 架 | 6–8 架 | 3–7 架 |

- OptiX 的优势只在大批量（≥ 200 万射线/launch）时出现；MID-360 规模下 Warp 已经远超需求，而且 Warp 能写任意 Python 内核（实体实例化、纹理、TSDF 积分）。
- Open3D 的 pybind 绑定持 GIL（r06 §2.1 已指出），CPU 回退必须放在独立的仿真进程。

---

## 4. MeshLidar GPU 管线原型（`scripts/warp_lidar.py`）

### 4.1 结构

单个 Warp 核函数 `k_lidar`，一个线程 = 一条射线，`tid = k·20000 + i`（k 为"机×帧"批序号）：

1. **花样在 GPU 上算**：与 `pattern.mid360_pattern` 同式。8 张每帧不变的 2 万长常量表（`SEL、CD、SD、EC、SAZ_SEC、SD0、CD0、AZL`）上传一次；每机每帧只传 float64 约化后的 3 个标量 `(cos θ0, sin θ0, az0)`。GPU 方向与 CPU 花样的角度差 ≤ 0.027°（float32）。
2. **逐点位姿**：每机每帧 K 个位姿样本（缺省 K = 11，10 ms 间隔），按点时刻 `i·5 µs` 线性插值位置、nlerp 四元数（CPU 端保证同半球）。K = 1 即 D2 的单一位姿语义。
3. **静态网格**：`wp.mesh_query_ray(static, p, d, hard_max)`。
4. **动态实体**：每个实体有类网格 id（局部系）、帧首/帧末位姿（位置 + 偏航，按点时刻插值）、包围球。先做射线–球剔除，再把射线变换到实体局部系查询，取最近者。实体号写入 `inst`（真值标签）。
5. **反射率**：静态命中按 `(face, u, v)` 插值 UV，查每材质的 905 nm 反射率图（u8，载入时由 RGB 预计算）；实体用 `reflect_905`。`ρ_eff = ρ · max(|n·d|, 0.05) · exp(−2βr)`。
6. **检测**：`R_max(ρ_eff) = min(hard_max, 40·(ρ_eff/0.1)^0.269)`（D2 / r04 同式），r04 §3.1.4 的逻辑斯蒂检测概率（可切回 D2 硬截断），盲区 0.1 m。
7. **环境与噪声**：雨滴/雪花近场假回波（比真实回波近才可见，`inst = −2`，tag 低两位 = 01），虚警，测距噪声 `σ·(1 + k·β·r)`，角度噪声。RNG 为 `wp.rand_init(seed(drone, frame), i)`：同种子同帧逐点可复现，与线程调度无关（对应 D2 的计数器 RNG 流 3）。
8. **输出**：传感器系（点时刻）xyz、range、refl u8、tag、inst、face、世界系命中点、射线起点。点时刻传感器系就是真实 LiDAR 的原始输出，运动畸变"自然"出现。

关键片段（实体实例化部分）：

```python
for e in range(n_ent):
    c = ent_p0[e] * (1.0 - tau) + ent_p1[e] * tau          # 点时刻实体位置
    yaw = ent_y0[e] * (1.0 - tau) + ent_y1[e] * tau
    oc = p - (c + wp.vec3(0.0, 0.0, ent_hc[e]))
    b = wp.dot(oc, d); disc = b * b - (wp.dot(oc, oc) - ent_r[e] * ent_r[e])
    if disc < 0.0 or -b - wp.sqrt(disc) > best:            # 包围球剔除 + 已有更近命中
        continue
    o_l, d_l = rotz(-yaw) * (p - c), rotz(-yaw) * d          # 射线进实体局部系
    qe = wp.mesh_query_ray(ent_mesh[e], o_l, d_l, best)
    if qe.result and qe.t < best:
        best = qe.t; inst = e; face = qe.face; nrm = rotz(yaw) * qe.normal
```

### 4.2 正确性核对（`bench_pipeline.py` 的 check 事件）

| 项 | 结果 |
|---|---|
| GPU 花样 vs `pattern.py` | 角度差最大 0.027°、p99 0.019° |
| 无噪声距离 vs 独立 `mesh_query_ray`（CPU 方向） | 距离差 p99 0.7 mm、最大 1 cm（方向 float32 误差在 100 m 处的放大），命中判断 0 差异 |
| 重心坐标约定（`mesh_eval_position(face,u,v)` vs `o + d·t`） | 中位 5 µm，最大 32 µm；UV 插值按 `u·uv0 + v·uv1 + (1−u−v)·uv2` |

### 4.3 功能开销（town，DSM 上方 30–60 m，100 架 × 1 帧 = 200 万射线，`pipeline.jsonl`）

| 配置 | 耗时 | M 射线/s | 相对基线 |
|---|---|---|---|
| 静态网格、单一位姿、常数反射率 | 5.34 ms | 374 | — |
| + 逐点位姿 K = 11 | 5.40 ms | 370 | +1% |
| + 纹理反射率 + 入射角 | 5.67 ms | 353 | +6% |
| + 8 / 32 / 64 个动态实体 | 5.76 / 5.97 / 6.31 ms | 347 / 335 / 317 | +8% / +12% / +18% |
| 32 实体 + 雾（V = 500 m）+ 10 mm/h 雨 + 假回波 + 虚警 + 角度噪声 | 6.01 ms | 333 | +13% |

实体第二方案（合并网格 + 每 10 ms 切片 refit）：8 / 64 / 256 个实体（0.6 万 / 4.6 万 / 18 万三角形）refit 0.07 / 0.13 / 0.27 ms，上传 0.1–0.36 ms。实体数 > 64 或需要骨骼动画（行人摆臂）时用 refit；≤ 64 用实例化。

### 4.4 多机持续吞吐（`sustain.py`，GPU 3，街道上方 8–20 m，5 m/s + 20°/s 偏航，32 个行人，纹理反射率）

每 tick（0.1 s 仿真）：向量化生成 N×11 个位姿 → 扫描 → GPU 前缀和压缩有效点 → 只拷回有效点（xyz、refl、tag、序号、实体号）。

| 架数 | 位姿准备 | 扫描 | 压缩 | 回传 | 合计 | 有效点/机/帧 | 实时倍数 |
|---|---|---|---|---|---|---|---|
| 1 | 0.35 ms | 0.96 ms | 0.29 ms | 0.35 ms | 1.96 ms | 6863 | 51× |
| 10 | 0.51 | 1.57 | 0.45 | 0.51 | 3.04 | 6700 | 33× |
| 50 | 0.88 | 4.01 | 0.97 | 1.02 | 6.84 | 6898 | 15× |
| 100 | 1.20 | 6.70 | 1.58 | 1.49 | 11.3 | 6152 | 8.9× |
| 200 | 1.95 | 12.4 | 3.14 | 2.72 | 21.3 | 5998 | 4.7× |
| 400 | 4.09 | 26.5 | 6.16 | 4.73 | 44.3 | 5780 | 2.3× |
| 800 | 10.7 | 52.0 | 86.1 | 41.9 | 191 | 5775 | 0.52× |

单卡实时上限约 500–600 架 MID-360；800 架时压缩与回传（1600 万槽位的前缀和与大块拷贝）成为瓶颈。双卡并发实验因 GPU 2 被他人占满而跳过；两卡各跑独立世界副本、无跨卡通信，预期可线性扩展（推断，未实测）。

### 4.5 光栅化近似对照（nvdiffrast，`nvdr_lidar.py`，20 个位姿 × 1 帧）

| 立方体每面分辨率 | 每机每帧 | 距离误差中位 | p90 | p99 | 误差 > 0.1 m | 命中不一致 |
|---|---|---|---|---|---|---|
| 256² | 19.5 ms | 3.1 cm | 0.32 m | 3.1 m | 24.5% | 0.12% |
| 512² | 19.6 ms | 1.5 cm | 0.13 m | 1.7 m | 13.1% | 0.06% |
| 1024² | 20.4 ms | 0.7 cm | 0.06 m | 0.69 m | 6.1% | 0.04% |
| 2048² | 23.2 ms | 0.4 cm | 0.03 m | 0.23 m | 2.4% | 0.02% |

光栅化成本由"每面变换并光栅化 200 万三角形"决定，与射线数无关，比 Warp 精确求交（约 0.07 ms/机/帧）慢约 280 倍，还有边缘混合像素误差。PyTorch3D 的光栅器属于同一类方法，未单独实测。**稀疏非重复扫描用光线求交，稠密相机用光栅化/3DGS**，两条管线分开。

---

## 5. 传感器物理

### 5.1 有效点率与航高、安装（town，50 个街道位置，`sweep_alt.jsonl`）

| 航高（街道/地面以上） | 倒装：纹理 + 入射角 + 逻辑斯蒂 | 倒装：D2 硬截断 ρ = 0.2 | 倒装：只看几何（100 m） | 正装前倾 20°：纹理模型 |
|---|---|---|---|---|
| 5 m | 43.2% | 44.6% | 47.2% | 10.2% |
| 10 m | 33.6% | 35.8% | 40.6% | 6.6% |
| 15 m | 34.9% | 38.4% | 48.1% | 2.5% |
| 20 m | 21.1% | 24.1% | 35.3% | 1.2% |
| 30 m | 11.0% | 13.5% | 31.6% | 0.23% |
| 45 m | 2.8% | 2.7% | 24.7% | 0.02% |
| 60 m | 0.47% | 0.05% | 15.7% | 0 |
| 90 m | 0 | 0 | 2.3% | 0 |

- 倒装视场为 −52°~+7°，正下方是半角 38° 的盲锥（`results/frame30_topdown_refl.png` 中央空洞，半径约 0.78 × 航高）。
- 量程截断（ρ = 0.2 时约 48 m）是高空失效的主因；"只看几何"一列说明 100 m 截止下几何上还能打到。
- 纹理反射率模型下回波 u8 中位 23–46（常数 ρ = 0.2 时为 30），分布更宽，可供强度类特征学习。本原型按 Livox 约定编码（漫反射 0–150 对应 0–100%，151–255 留给回反射体，r04 §3.1.4）；D2 的 `LidarScan8` 用 `ρ·255`（地物 51），两者口径不同，D3 应统一到 Livox 口径。

### 5.2 运动畸变（town，街道上方 10 m，20 个位置，`distortion.jsonl`）

A：真实 LiDAR 输出（逐点位姿）被当作"帧末单一位姿"直接拼图时的误差，即畸变本身；B：D2 式单一位姿数据再被 FAST-LIO 类算法用 IMU 去畸变时**引入**的误差。

| 运动 | 倒装 A 中位 / p95 / 最大 | 倒装 B 中位 / p95 | 前倾 20° A 中位 / p95 |
|---|---|---|---|
| 静止 | 0 | 0 | 0 |
| 3 m/s | 0.18 / 0.29 / 0.30 m | 0.11 / 0.15 m | 0.15 / 0.28 m |
| 8 m/s | 0.47 / 0.78 / 0.80 m | 0.29 / 0.39 m | 0.40 / 0.75 m |
| 15 m/s | 0.86 / 1.46 / 1.50 m | 0.54 / 0.73 m | 0.74 / 1.41 m |
| 偏航 30°/s | 0.45 / 1.18 / 2.42 m | 0.28 / 0.44 m | 0.68 / 1.36 m |
| 偏航 90°/s | 1.35 / 3.55 / 7.05 m | 0.83 / 1.34 m | 2.05 / 4.05 m |
| 8 m/s + 90°/s | 1.23 / 3.74 / 7.74 m | 0.92 / 1.41 m | 2.11 / 4.16 m |

动态实体拖影：车 10 m/s 横穿、12 m 外静止无人机，逐点实体位姿时车点云沿运动方向长 4.87 m，帧内快照 4.40 m（真值 4.5 m）。真实 LiDAR 看运动车辆就是拉长的，逐点时刻模拟才能复现。

### 5.3 多回波（`multiecho.py`，50 个街道位置，波束足印内均匀 M 条子射线，间隔 > 0.5 m 记为新回波）

| 安装 | M | 足印（°） | 波束/s | 命中中 ≥ 2 回波 | 首末回波间隔中位 |
|---|---|---|---|---|---|
| 倒装 | 1 | — | 320 M | 0 | — |
| 倒装 | 4 / 8 / 16 | 0.28 × 0.03 | 84 M / 42 M / 21 M | 7.4% / 8.1% / 8.2% | 1.4 / 1.8 / 2.0 m |
| 倒装 | 4 / 8 / 16 | 0.28 × 0.28 | 83 M / 42 M / 21 M | 9.0% / 9.9% / 10.1% | 1.5 / 1.8 / 2.1 m |
| 前倾 20° | 4 / 8 / 16 | 0.28 × 0.03 | 235 M / 120 M / 61 M | 16.8% / 16.7% / 15.5% | 1.4 / 1.9 / 2.2 m |

MID-360 规格是首回波 20 万点/s（r04 §3.1.1），多回波只作为其他型号或研究选项（足印 0.28° × 0.03° 借用 Avia 公开值，置信度 C）。摄影测量网格的树冠是实心壳，多回波只来自几何边缘，**不能模拟植被穿透**；需要穿透时用 r2 的程序化植被（alpha 叶片）或体密度场。

### 5.4 雨雾雪（905 nm，`warp_lidar.beta_*`）

消光系数 β（1/m）与等效最大量程（解 `r = 40·(ρ·e^{−2βr}/0.1)^0.269`，100 m 截止）：

| 天气 | β | ρ = 0.1 | ρ = 0.2 | ρ = 0.5 |
|---|---|---|---|---|
| 晴 | 0 | 40.0 m | 48.2 m | 61.7 m |
| 小雨 2.5 mm/h | 4.6e-4 | 39.6 | 47.6 | 60.8 |
| 中雨 10 mm/h | 1.2e-3 | 39.0 | 46.8 | 59.4 |
| 暴雨 50 mm/h | 3.4e-3 | 37.4 | 44.4 | 55.7 |
| 雪 5 mm/h（水当量） | 5.5e-3 | 35.9 | 42.5 | 52.7 |
| 雾 V = 500 m | 7.8e-3 | 34.6 | 40.6 | 50.0 |
| 浓雾 V = 200 m | 2.0e-2 | 29.4 | 33.8 | 40.3 |
| 浓雾 V = 50 m | 7.8e-2 | 18.4 | 20.4 | 23.3 |

- 雾用 Kim 模型 `β = 3.912/V·(λ/550)^{−q}`；雨用近红外经验式 `α[dB/km] = 1.076·R^0.67`；雪用常见经验式（只作量级参考）。这些是经验参数（置信度 C），须用真机或公开雨雾舱数据标定。
- 由于 D2 量程拟合指数只有 0.269，雨对量程影响很小；雨天真实 LiDAR 的主要退化是近场雨滴回波与窗口附着。原型用"每射线假回波概率 `ghost_p` + 指数距离分布（尺度 1.5 m）"表示，tag 低两位置 01（与 Livox 噪声标记一致），10 mm/h 时取 1%，实测每帧约 2% 的点为假回波。
- 事件中的天气时间线（design-r1 的 `environment: [{t, wind_mps, rain_mm_h}]`）直接映射到每帧的 `beta / ghost_p / k_noise`；同一帧内取常数。

---

## 6. 输出格式与时间戳

### 6.1 时间约定（与 livox_ros_driver2 一致，所有格式共用）

- `timebase = header.stamp = 帧首点时刻`（仿真时钟 ns，帧边界对齐 100 ms 网格，`sync_type = sim`）；
- `offset_time = 点时刻 − timebase`（ns，u32，帧内非递减；MID-360 第 i 条射线为 `i·5000`）；
- `line = i & 3`（束号）；`tag`：bit[1:0] 空间噪声（01 = 雨雾假回波/虚警），bit[5:4] 回波序号（单回波 00）；
- **每帧输出全部 20 000 个点**：无回波点写 `(0, 0, 0)`、反射率 0、tag 0，`offset_time` 照常 `i·5000`。这与 livox_ros_driver2 一致（`pub_handler.cpp::ProcessCartesianHighPoint` 按包内序号原样拷贝每个点、`offset_time = 包时间 + i·点间隔`，不过滤零点）；下游用盲区距离剔除零点。
- 只发有效点是一个实测过的陷阱：FAST-LIVO2 `preprocess.cpp::avia_handler`（非特征模式）在相邻两点 `offset_time` 相差 ≥ 1 ms 时，把后一点时刻改成"前一点 + 0.004167 ms"（按 Avia 24 万点/s 假设的修复逻辑）。倒装 MID-360 每帧中段约 20 ms 朝天无回波，删掉零点后间隔超阈值，后半帧所有点的时刻被压缩到前半帧，帧末时刻从 +100 ms 变成约 +55 ms，去畸变出错（§7.2）。

### 6.2 各格式实测（`io_bench.jsonl`）

| 格式 | 2 万点体积 | 编码耗时（2 万点 / 20 万点） | 用途 |
|---|---|---|---|
| ROS1 `livox_ros_driver/CustomMsg`（自写字节拼装） | 380 047 B（19 B/点） | 1.03 ms / 11.0 ms | FAST-LIO2 / FAST-LIVO2（ROS1 noetic） |
| 同上，rosbags 对象序列化 | 380 047 B（逐字节一致） | 145 ms / — | 只做校验 |
| ROS2 `livox_ros_driver2/msg/CustomMsg`（CDR） | 400 047 B | 160 ms（rosbags 对象路径） | ROS2 / MCAP；生产也应自写拼装 |
| PCD（x y z intensity tag line timestamp，PointXYZRTLT 同名字段） | 520 225 B（26 B/点） | 1.8 ms / 18.5 ms | 通用点云工具 |
| PLY（Livox 点结构原样） | 380 212 B | 0.9 ms / 8.8 ms | 通用 |
| `awr.sensor.lidar_frame.v1`（JSON 头 + SoA 20 B/点） | 400 282 B | 0.9 ms / 7.8 ms | 项目内部；已用 `awr.sim.sensors.lidar.frame.decode/validate` 往返核对，2.5 万点时触发 schema `point_count ≤ 22000` 与规则 ⑦ |

- 写出的 ROS1 bag 中 `/livox/lidar` 连接的 md5 为 `e4d6829bdfe657cb6c21a746c86b21a6`，与 FAST-LIVO2 自带的 `livox_ros_driver/CustomMsg.h` 一致；ROS2 版写成 MCAP（`results/bag/sim_clear_ros2/`，前 50 帧）。
- 数据量：MID-360 实际每帧约 0.6–1.0 万有效点 → 内部格式只存有效点（保留真实 `offset_time`）约 0.2 MB/帧、2 MB/s/机；导出给 ROS 的全点 CustomMsg 为 380 KB/帧、3.8 MB/s/机（63 s bag 244 MB，零点在 zstd 下压缩率高）。100 架 5 分钟约 57 GB（有效点、未压缩），录制需 MCAP zstd 分块与按需降采样。
- 自写字节拼装的关键：

```python
def custom_msg_ros1_fast(fr, t_ns, seq=0, frame_id='livox_frame', lidar_id=0):
    a = frame_points(fr)          # 结构化数组 <u4 f4 f4 f4 u1 u1 u1>（19 B/点，按 offset_time 排序）
    fid = frame_id.encode()
    head = struct.pack('<III', seq, t_ns // 10**9, t_ns % 10**9) + struct.pack('<I', len(fid)) + fid
    body = struct.pack('<QIB3sI', int(t_ns), len(a), lidar_id, b'\x00\x00\x00', len(a))
    return head + body + a.tobytes()
```

---

## 7. 与 FAST-LIVO2 的闭环（衔接 r1）

### 7.1 bag 合同（`make_bag.py` 产出，r1 可直接用）

| 项 | 约定 |
|---|---|
| 话题 | `/livox/lidar`（`livox_ros_driver/CustomMsg`，10 Hz）；`/livox/imu`（`sensor_msgs/Imu`，200 Hz，`frame_id = livox_frame`） |
| 点流 | 每帧全部 20 000 点，无回波为零点（`make_bag.py --zeros`，文件名后缀 `_z`）；`header.stamp = timebase = 帧首`，`offset_time = i·5000 ns` |
| IMU | MID-360 内置 ICM40609 语义：加速度单位 g、角速度 rad/s；白噪声 acc 1e-3 g、gyro 7e-4 rad/s，偏置随机游走 1e-5/√s（r04 §3.1.5 默认值） |
| 外参 | IMU 与 LiDAR 同姿态，LiDAR 原点在 IMU 系 `[-0.011, -0.02329, 0.04412]`（FAST-LIVO2 `extrinsic_T`），`extrinsic_R = I`；LiDAR 倒装于机体下 0.10 m |
| 起始 | 3 s 悬停供 IMU 初始化，之后 3 s 平滑加速 |
| 真值 | `gt_imu_tum_<tag>.txt`（IMU 系 200 Hz，TUM）、`gt_lidar_tum_<tag>.txt`（帧末 LiDAR 系）；首帧 30 的 `pcd/ply/_gt.npz`（含世界系点、实体号、面片号） |
| 配置 | `fastlivo2_sim_mid360_<tag>.yaml`：`img_en: 0`、`lidar_type: 1`、`scan_line: 4`、`blind: 0.5`，其余同 avia.yaml |
| 航迹 | town 中部跑道形闭环，高度 = 18 m 邻域最高点 + 10 m，偏航沿切向；8 个动态实体（行人 1.3 m/s、车 8 m/s，往返） |
| 生成耗时 | 每帧扫描 1.6 ms、序列化 0.8 ms；63 s 全点 bag 244 MB（只含有效点的旧版 128 MB） |

### 7.2 实测（r1 镜像 `d3-r1-fast-livo2:noetic`，r1 的 `run_livo_bag.sh` 副本，实时回放）

全点流（正确合同，`*_z`）：

| bag | 速度 / 最大偏航角速度 | 跟踪帧 | ATE RMSE | ATE 最大 | 旋转误差中位 | 终点漂移（首帧对齐） | 10 m 段相对误差中位 | CPU / 内存 |
|---|---|---|---|---|---|---|---|---|
| clear_z（逐点位姿） | 4 m/s / 16°/s | 627/630 | **1.7 cm** | 3.7 cm | 0.036° | 0.06 m（0.02%） | 0.06% | 184% / 2.0 GB |
| clear_k1_z（D2 式单一位姿） | 同上 | 627/630 | 2.8 cm | 6.4 cm | 0.050° | 0.13 m（0.06%） | 0.14% | 159% / 2.6 GB |
| rain_z（10 mm/h，假回波 1%） | 同上 | 627/630 | 2.0 cm | 3.7 cm | 0.032° | 0.43 m（0.18%） | 0.21% | 150% / 2.1 GB |
| clear_v8_z（逐点位姿） | 8 m/s / 32°/s | 627/630 | **4.0 cm** | 9.6 cm | 0.031° | 0.01 m（0.003%） | 0.09% | 159% / 2.9 GB |
| clear_k1_v8_z（单一位姿） | 同上 | 627/630 | **43 cm** | 84 cm | 0.27° | 0.27 m（0.06%） | 1.17% | 152% / 3.9 GB |
| rain_v8_z | 同上 | 627/630 | 3.5 cm | 8.6 cm | 0.033° | 0.02 m（0.005%） | 0.19% | 151% / 3.0 GB |

只发有效点（错误合同，作为陷阱记录；输出时间戳实测为帧首 + 53–57 ms，见 §6.1）：

| bag | ATE RMSE | ATE 最大 | 10 m 段相对误差中位 |
|---|---|---|---|
| clear（逐点位姿，4 m/s） | 7.8 cm | 26 cm | 0.25% |
| clear_k1（单一位姿，4 m/s） | 8.0 cm | 27 cm | 0.26% |
| rain（4 m/s） | 7.6 cm | 26 cm | 0.46% |
| clear_v8（逐点位姿，8 m/s） | 36 cm | 61 cm | 0.75% |
| clear_k1_v8（单一位姿，8 m/s） | 39 cm | 64 cm | 0.75% |
| rain_v8（8 m/s） | 31 cm | 49 cm | 0.87% |

- 全部 12 组都完整跟踪，无发散；FAST-LIVO2 进程 1.5–1.8 核、常驻内存 2.0–3.9 GB，一台 64 核 dmax 可并行实时跑约 30–40 个单机 LIO。
- 逐点位姿在全点合同下效果显著：4 m/s 时单一位姿 ATE 多 65%，8 m/s / 32°/s 时多约 10 倍（4.0 cm → 43 cm）。原因是 LIO 用 IMU 把每个点"去畸变"到帧末，而单一位姿数据本身没有畸变，等于反向注入了 §5.2 B 列量级的误差。**D2 的单一位姿扫描不能用于任何 SLAM / 去畸变类基准。**
- 只发有效点时，时间跳变修复把一半点的时刻改错，逐点与单一位姿的差别被淹没（7.8 vs 8.0 cm），这也解释了"错误合同下逐点位姿看起来没用"的假象。
- 雨（1% 近场假回波）对 ATE 影响很小（FAST-LIVO2 的 0.5 m 体素平面拟合对零散近场点鲁棒），但 4 m/s 时终点漂移由 0.06 m 增到 0.43 m，雨天基准应同时看漂移与地图噪声。
- 结果偏乐观：IMU 无振动、无时间偏移、外参完美、网格表面平滑。正式基准应加机架振动谱、`imu_time_offset` 抖动、外参扰动，并加入相机（r3 渲染）跑完整 LIVO。

---

## 8. 每机私有地图（`scripts/recon.py`）

### 8.1 原型

- 稠密局部栅格（town 全域 0.5 m：1070 × 641 × 101 = 6927 万体素），三张 float32：log-odds、TSDF 加权和、权重。
- `k_integrate`：每个有效点从射线起点（点时刻位姿）到命中点做 3D-DDA，途经体素 `L += −0.4`（miss），命中体素 `+0.85`（hit），每 10 帧夹到 [−2, 3.5]；距命中点 3 个体素内做投影 TSDF 加权平均。雨滴假回波与虚警（`inst < −1`）不入图，相当于按 tag 过滤。
- `k_frontier`：自由体素（L ≤ −0.39）且 6 邻域有未知体素（L == 0）。
- 评估：在 GLB 真值表面按面积采样 200 万点，覆盖率 = 3×3×3 邻域内有占据体素的比例；精度 = 占据体素中心到真值网格的距离（`mesh_query_point_no_sign`）。
- TSDF 网格：`wp.MarchingCubes`。

### 8.2 实测（3 架，town 西 / 中 / 东三块区域跑道航线，5 m/s，60 s，高度 = 邻域最高点 + 10 m，倒装）

| 指标 | 机 0 | 机 1 | 机 2 |
|---|---|---|---|
| 航线长度 | 317 m | 339 m | 315 m |
| 每帧扫描 / 积分（中位） | 0.70 / 0.14 ms | 0.72 / 0.14 ms | 0.73 / 0.14 ms |
| Frontier 提取（全栅格） | 0.73 ms | 0.64 ms | 0.64 ms |
| 占据 / 自由体素 | 11.3 万 / 132 万 | 14.3 万 / 157 万 | 15.1 万 / 178 万 |
| 触及的 8³ 块 → 稀疏存储估算 | 5172 块 → 32 MB | 6374 块 → 39 MB | 6955 块 → 43 MB |
| 稠密存储 | 831 MB | 831 MB | 831 MB |
| 真值表面覆盖率 | 21.6% | 29.0% | 31.3% |
| 占据体素到真值距离中位 / p90 | 0.13 / 0.26 m | 0.13 / 0.25 m | 0.13 / 0.26 m |

- 三机覆盖的两两 Jaccard：0.16（0–1）、0（0–2）、0.24（1–2）；三张私有地图 log-odds 相加后的合并图覆盖 63.2%（等于三者覆盖之并，合并无信息损失）。`results/recon_occ_topdown.png` 为三机占据体素俯视图（按机着色）。
- TSDF → Marching Cubes：0.67 s，59 万三角形，顶点到真值距离中位 0.33 m、p90 1.46 m，只有 57% 在一个体素内——误差来自"未观测体素 = +1"与观测负值交界处的伪面，生产需按权重掩膜（8 个角点权重都 > 0 才出面）。
- Frontier 数（15.6–21 万体素）偏多：表面附近自由体素紧贴未观测的建筑内部也被算作前沿。生产需加连通聚类、最小簇尺寸、只保留"未知侧可达"（未知体素不在已占据表面背后）的前沿。

### 8.3 结论

GPU 上每机每帧建图不到 0.2 ms，计算不是瓶颈；**瓶颈是内存**——稠密全域栅格 831 MB/机，100 架就是 83 GB。生产改为 8³ 稀疏块哈希（60 s 约 32–43 MB/机；可选 Open3D `VoxelBlockGrid` CUDA、NanoVDB/fVDB 或自写 Warp 块哈希），并只保留"无人机周围 ±200 m"的活动块，远处块落盘。

---

## 9. 对 D3 架构的建议

### 9.1 模块落点

| 建议 | 落点 | 说明 |
|---|---|---|
| 新增 `MeshLidarBackend`，`caps.raycast = "mesh"` | M25；`sensors/backends/mesh_lidar.py`（提案）；`models.Raycast` 增加 `"mesh"` | 实现现有 `SensorBackend` 协议：`open(world, rigs)` 载入 World Package 的 `raycast` 资产与实体类网格；`request(slots, pose_samples, t_frame_ns)` 非阻塞地把本 tick 到期的所有机×帧打成一次 GPU launch；`poll()` 返回 `SensorFrame(payload={points, gt_ref})` |
| 帧调度 | M25 | 所有雷达帧边界对齐 100 ms 网格（仿真 PTP）；帧 [t, t+100 ms) 在仿真时钟越过 t+100 ms 后的那个 tick 生成，需要该窗口内 11 个机体位姿样本（物理权威按 10 ms 记录）与实体位姿（事件带 Canonical Event Tape 读取）；输出晚 1 帧，与真实驱动一致 |
| World Package 资产 | M22（r2） | `raycast/mesh.npz`（V、F、UV、每面材质号，米、ENU）+ `raycast/refl905_<mat>.png`（u8，载入时由 RGB 预计算，可被人工材质表覆盖）+ `raycast/semantic_<mat>.png`（可选）。**不需要序列化 BVH**：GPU 上 0.3–1.2 s 现建 |
| 事件实体 | M23 | 每类实体一个局部系网格（人、车、狗；可带简单骨骼）+ `reflect_905` + 语义类；Event Tape 给每 10 ms 位姿。≤ 64 个近邻实体用实例化，更多或骨骼动画用合并网格 refit |
| 传感器参数 | `vehicles/sensor_models/mid360.yaml` 增字段（提案） | `motion: per_point`、`pose_samples: 11`、`reflectivity: {model: texture_luma_v1, incidence: true}`、`detection: logistic`、`weather: {source: event.environment, ghost_rain_per_mmh: 0.001}`、`multi_echo: {enabled: false, M: 4, divergence_deg: [0.28, 0.03], gap_m: 0.5}` |
| 帧契约 | `packages/contracts/sensor/` | 新增 `awr.sensor.lidar_frame.v2`：去掉 22 000 点上限（≤ 2 000 000），加 `return_mode`、`pattern_id`、`weather_ref`、`event_tape_ref`；新增**隐藏的** `awr.sensor.lidar_gt.v1`（逐点世界坐标、实例号、语义类、面片号，与 `(sensor_id, frame_seq)` 一一对应），只进评测端，不给 Agent |
| 录制 | M12 `rec/mcap_channels.json` | 增 `/uav/{id}/sensor/{name}/points`（awr-raw lidar_frame.v2，zstd）与 `/gt/uav/{id}/sensor/{name}/labels`（隐藏分区）；数据集导出器再转 ROS1 bag / ROS2 MCAP（Livox CustomMsg，**全点流**：无回波补零点，见 §6.1）、PCD |
| Web 预览 | M11 / apps/web | 继续用 `awr.LidarScan8.v1`（GPU 端抽稀到 4000 点/帧），D2 前端不改 |
| 私有地图 | M26 Agent Observation SDK | `PrivateMap.integrate(frame)`、`frontiers(min_cluster)`、`export_delta(since)`（供 ANet 消息共享）、`merge(delta)`（log-odds 相加）；地图只由本机帧构建，Agent 拿不到 GLB 真值 |
| LIO 回归 | CI / r1 | 固定两份 town 全点 bag（clear_z 4 m/s、clear_v8_z 8 m/s，各 63 s），FAST-LIVO2 LIO 模式 ATE ≤ 5 cm / 8 cm 作为 MeshLidar 改动的回归门槛（当前 1.7 / 4.0 cm） |

### 9.2 数据流（每 tick）

```
物理权威（FleetSim / Gazebo）── 10 ms 位姿样本 ──┐
事件权威（Event Tape）────── 实体 10 ms 位姿 ───┼─> MeshLidar（GPU，一次 launch：N 机 × 到期帧）
环境（Event environment）── β / ghost_p ────────┘        │
                                                         ├─> GPU 私有地图（每机，不出显存）
                                                         ├─> 压缩有效点 → 录制（lidar_frame.v2）/ 导出（CustomMsg）
                                                         ├─> 抽稀 → LidarScan8 → Web
                                                         └─> 真值侧车（实例号、世界点）→ 评测端（隐藏）
```

传感器与物理解耦：Fast 模式与 Gazebo 模式只要给出相同的位姿样本，MeshLidar 输出就逐点相同（不同 GPU / CPU 后端的距离差 ≤ 0.3 mm、命中判断一致），满足 design-r1 第九节"条件可核对、误差可容忍"的复现标准。Gazebo 的 `gpu_lidar` 只支持均匀网格扫描，Livox 非重复花样需要自写插件（r04 已述 Prometheus 的 CSV 回放插件走 CPU 射线），因此建议 Gazebo 模式也调用同一 MeshLidar 服务，而不是用 Gazebo 自带雷达（Gazebo 侧细节见 r5/r9 单元）。

### 9.3 运行档位

| 档位 | LiDAR 后端 | 规模 |
|---|---|---|
| 公开站网页沙盒（无 GPU） | D2 DSM 扫描器（预览，2 Hz，1 路）或 Open3D CPU（独立进程） | 1–数架 |
| 研究 GPU Worker（单 A40） | Warp MeshLidar | 实时约 500 架 MID-360；批量离线不受实时约束 |
| 千架级 / 稠密 20 万点雷达 | C++ OptiX 模块（RT Core），接口同 MeshLidar | 1000+ 架 |

### 9.4 阶段落地（对应 design-r1 P0–P3）

1. P0 不依赖 LiDAR，本单元无阻塞项。
2. P1-a：World Package `raycast` 资产（r2）+ `MeshLidarBackend`（静态网格 + 逐点位姿 + 纹理反射率）+ `lidar_frame.v2` + 真值侧车 + MCAP 通道。
3. P1-b：事件实体实例化 / refit、天气耦合、bag/PCD 导出器、FAST-LIVO2 回归。
4. P1-c：私有地图（稀疏块）+ Frontier 聚类 + SDK + ANet 增量共享，验收"三机地图互不相同、共享后覆盖率提升"。
5. P2/P3：Gazebo 模式共用 MeshLidar；可选 OptiX 后端、多回波、植被体密度。

---

## 10. 风险与注意事项

1. **GLB 尺度**：Sketchfab 导出不是度量的。town 的 ×2.5 根节点缩放看起来合理，oryahovo、hilltop 尺度可疑；LiDAR 量程截断、有效点率、点密度都直接依赖尺度，必须以 r2 的尺度标定为准再生成正式数据。
2. **摄影测量网格的几何缺陷**：树冠、栏杆是实心壳或缺失，立面背面与屋檐下有洞，"texture problems" 版本还有融化几何；没有材质语义，反射率只能由纹理亮度近似（置信度 C）；玻璃、水面的镜面反射（实际应无回波或多径）未建模。洞会让射线穿到不该到的地方，World Package 应标注"几何不完整区域"（design-r1 第四节），并在真值侧车里给这些点打标。
3. **MID-360 视场与高度**：倒装 38° 盲锥、正装高空几乎无回波，策略若想用 LiDAR 就必须低空，与建筑碰撞风险上升；需要和 r5/r10 的物理碰撞一起验证。
4. **共享 GPU**：dmax 的 GPU 2/3 被多个 D3 单元同时使用，本笔记数字可能受干扰（偏保守），生产需要独占 GPU Worker 与容量模型。
5. **OptiX 路径的依赖重量**：Mitsuba/Dr.Jit 是渲染器，JIT 编译与内存占用较大，不宜作为生产依赖；真要用 RT Core 需维护 C++ OptiX 模块。Warp 没有两级 BVH，实体只能靠核内循环或 refit。
6. **天气参数是经验值**：雨对 905 nm 量程的影响在本模型中很小，主要靠假回波；雪、雾、溅水、窗口附着都需要真机或公开数据标定。
7. **跨硬件逐位一致不可期**：共享边上的平局会让 0.02% 的命中三角形编号不同；复现标准应定义为"距离差 ≤ 1 mm、命中不一致 ≤ 1e-4"，而不是字节相等。
8. **合成 bag 偏乐观**：IMU 无振动、无时间偏移、外参完美，ATE 1.7–4.0 cm 不代表真机水平；正式基准要加扰动。另外 bag 合同细节（全点流、时间基准、IMU 单位）会让同一算法的 ATE 相差一个数量级（§7.2），合同必须写进契约并有回归测试。
9. **数据量**：每机约 2 MB/s（CustomMsg），百机级 5 分钟就是数十 GB；录制要压缩并允许按任务降采样。
10. **稠密地图内存**：全域稠密栅格每机 831 MB，百机不可行，必须稀疏化；Frontier 需聚类过滤，否则数量虚高、误导探索策略。
11. **多回波模型的局限**：子射线足印法只能在几何边缘产生多回波，不代表植被、雨幕中的真实波形；MID-360 本身是单回波，默认关闭。

---

## 附录 A：复现

```bash
# dmax，工作目录 /data/anet-drone4d-d3/r4-mesh-lidar/work（脚本与 .cache/research/d3/r4-mesh-lidar/scripts 同步）
bash run_backends.sh; bash run_backends3.sh                 # 后端吞吐 → out/bench_backends.jsonl
bash run.sh bench_pipeline.py town                          # 管线正确性与功能开销 → out/pipeline.jsonl
bash run.sh sweep_alt.py                                    # 有效点率 vs 航高、DSM vs 网格 → out/sweep_alt.jsonl
bash run.sh distortion.py; bash run.sh distortion.py smear  # 运动畸变与实体拖影 → out/distortion.jsonl
bash run.sh multiecho.py                                    # 多回波 → out/multiecho.jsonl
bash run.sh io_bench.py                                     # 输出格式 → out/io_bench.jsonl
bash run_stage3.sh                                          # 只发有效点的 6 个 bag + FAST-LIVO2（陷阱对照）
bash run_stage4.sh                                          # 全点流 6 个 bag（--zeros）+ FAST-LIVO2（r1 镜像）→ out/bag、out/livo_*_z
python eval_lio.py livo_clear_z/r4_sim_clear_z.txt bag/gt_imu_tum_clear_z.txt   # ATE（本地 .venv 即可）
bash run.sh recon.py 0.5 60                                 # 三机私有地图 → out/recon.jsonl
bash run_sustain.sh                                         # 多机持续吞吐 → out/sustain.jsonl
bash run.sh nvdr_ref.py && docker run --gpus '"device=3"' ... d3-r3-camera-render:v1 python3 nvdr_lidar.py   # 光栅化对照
python viz.py results                                       # 俯视图 PNG（纯 numpy + zlib）
```

## 附录 B：文件清单

| 文件 | 内容 |
|---|---|
| `scripts/warp_lidar.py` | MeshLidar 原型（GPU 花样、逐点位姿、实体实例化、纹理反射率、天气、RNG、β 函数） |
| `scripts/lidar_io.py` | CustomMsg（ROS1 快速拼装 / rosbags）、PCD、PLY、`lidar_frame.v1` 编码 |
| `scripts/make_bag.py` | GLB → MID-360 + IMU → ROS1 bag（+ ROS2 MCAP 小样）、真值、FAST-LIVO2 配置 |
| `scripts/recon.py` | Warp log-odds + TSDF + Frontier + Marching Cubes + 真值评估 |
| `scripts/bench_backends.py`、`dda_copy.py` | 后端基准；D2 `dda_dsm` 逐字副本与并行版 |
| `scripts/sustain.py`、`nvdr_ref.py`、`nvdr_lidar.py`、`distortion.py`、`multiecho.py`、`sweep_alt.py`、`io_bench.py`、`eval_lio.py`、`viz.py`、`glb_prep.py`、`common.py` | 其余实验 |
| `scripts/run.sh`、`run_backends*.sh`、`run_stage2/3/4.sh`、`run_sustain.sh`、`run_livo_bag_r1copy.sh`（r1 脚本副本） | dmax 上的批量运行入口 |
| `results/*.jsonl`、`results/livo_*/ate.json`、`results/bag/`（不含 .bag）、`results/*.png` | 原始结果 |
