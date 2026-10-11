# r5 研究笔记：Gazebo Harmonic 作为 D3 Realistic 后端的可行性

## GLB 世界、事件磁带驱动的 actor、传感器帧率、多机与 PX4 SITL、与 FleetSim / World Runtime 的状态同步

> 研究单元：D3 / r5-gazebo ｜ 日期：2026-10-11 ｜ 性质：只读研究，未改动项目代码与 `docs/research/d3` 以外的文档
>
> 输入：用户设计讨论 `/data/projs/anet-drone-data/design-r1.md`（第三节 Fast / Realistic 档、第四节 GLB 派生多表达、第六节 actor 不参与刚体碰撞、第九节 Canonical Event Tape、M24 Gazebo Adapter、P2 阶段）；D2 代码 `python/awr/sim/backends/base.py`（`SimBackend` / `ClockCaps`）、`python/awr/sim/sensors/backends/base.py`（`SensorBackend`）、`python/awr/sim/runtime/clock.py`（`SimClock`）、`python/awr/sim/sensors/lidar/scan_rt.py`；既有研究 `docs/research/r23-airsim-gzsim.md`（World Package → gz 导出器原型、WindEffects 标定）、`r20-px4.md`、`r22-px4-multi-sim-xtdrone.md`。
>
> 同期 D3 单元的交叉引用：r2（GLB 尺度与配准）、r3（服务端相机渲染，Fast 档相机数字）、r4（网格 LiDAR，Fast 档 LiDAR 数字）、r6（Event Runtime 与 `drone4d.tape.v1` 磁带，本文直接消费其 stage1 磁带）、r9（gz-sim 源码、C++ 时钟门控锁步、确定性；本文不重复 r9 的源码分析，只在结论处对齐）。
>
> 实验机：dmax（主机名 gpu2；实测 8 块 A40 48 GB，驱动 580.95.05；本单元只用 GPU 2、3）。容器 `d3-r5-gazebo`（`--gpus device=2,3 --cpus 16 --memory 96g`）与 `d3-r5-px4`（同配额），工作目录 `/data/anet-drone4d-d3/r5-gazebo/`，GLB 复用 `/data/anet-drone4d-d3/data/`（三个文件已由其他单元传好，大小与本地一致，本单元未重复传输，也未使用 modelscope）。实验结束两个容器均已停止并删除。
>
> 保留的镜像：`d3-r5-gazebo:harmonic`（`af7921ce39c0`，3.36 GB；Ubuntu 24.04 + ROS 2 Jazzy vendor 包内的 Gazebo Harmonic，Dockerfile 见 `.cache/research/d3/r5-gazebo/docker/Dockerfile`；r9 复用了该镜像）与 `docker.1ms.run/px4io/px4-sitl-gazebo:latest`（`70584c9eb1e9`，2.18 GB，PX4 官方 SITL+Gazebo 镜像，2026-09-11 构建）。
>
> 脚本与原始结果：`/data/projs/anet-drone/.cache/research/d3/r5-gazebo/`（`scripts/`、`results/`、`worlds/`、`models/`、`col/`、`docker/`）。dmax 上另有 `col/`（含 100 MB 的全分辨率碰撞 STL）与 `r6/`（r6 磁带与网格副本）。

---

## 0. 结论速览

| 议题 | 实测结论 | 对 D3 的含义 | 依据 |
|---|---|---|---|
| 无头 GPU 渲染 | Docker + A40 + EGL（`--headless-rendering`）可用；需要容器内存在 `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`（官方 PX4 镜像里没有，要自己补） | Realistic 档可放在无显示器的 GPU 节点上运行 | §2.1 |
| GLB 载入 | gz-common 的 Assimp 加载器能直接读三个 Sketchfab GLB（200–300 万三角形，内嵌 4K 纹理）；从启动到首帧 7.3–13.4 s，服务端 RSS 2.0–3.0 GB，显存 1.3–2.4 GB | GLB 可直接作视觉，不必转 DAE/OBJ | §2.1 |
| GLB 坐标 | **gz 会应用 glTF 节点矩阵，但不做 Y-up → Z-up 转换**，必须在 `<pose>` 上加 roll = +π/2；按此规则与 r6 的 ENU 网格对齐后，深度图与 r6 DSM 的高差中位数 0.6 mm（MAD 2.3 cm）；放大 20.9 倍的 stage1 区域中位数 -7 mm | World Package → SDF 导出器的变换公式已验证（§3.1） | §2.2 |
| 碰撞网格 | 二次误差减面到 2 万 / 10 万 / 50 万三角形，每级 3–4 s；相对全网格的竖向误差中位数 0.16 / 0.09 / 0.03 单位；**10 架 x500 的物理步长几乎不随碰撞网格规模变化**（1.75–1.97 ms/步，全量 200 万三角形 1.87 ms），只影响内存（0.42 GB → 1.16 GB） | 碰撞网格默认 10 万–50 万三角形、bullet 检测器；DART + bullet、DART + ODE、bullet-featherstone 都能用 | §2.3 |
| 多机物理 | x500（4 个 MulticopterMotorModel + MulticopterVelocityControl）：1 / 3 / 10 / 20 架分别 0.81 / 1.16 / 1.82 / 3.16 ms/步（4 ms 步长）；单进程 250 Hz 实时上限约 25 架 | Realistic 档面向 1–20 架的高可信实验，不面向百架规模 | §2.3 |
| actor（SDF 轨迹） | 由 r6 磁带导出 `<trajectory>`：渲染位置与磁带的偏差中位数 0.29 m（沿航向偏 +0.27 m，主要是包围框中心与骨盆的形体偏差）；路点从 1 Hz 加密到 10 Hz **精度不变，但 RTF 从 1.60 降到 0.37**；`<loop>false</loop>` 加任意渲染传感器会**卡死在首帧**（gz-sim 8.15 缺陷） | 磁带导出用 1 Hz 路点 + `loop=true` + 末尾超长驻留路点 | §2.4 |
| actor（进程内磁带驱动） | 用 PythonSystemLoader 在每个 PreUpdate 写 `TrajectoryPose` / `AnimationTime`：43 个实体每步 0.87–0.95 ms（约 20 µs/实体）；需补偿蒙皮根节点 0.66 m 的前向偏移；校正后偏差中位数 0.57 m | 适合"磁带为唯一真值、Gazebo 只渲染"的锁步模式；生产版改 C++ | §2.4 |
| actor 的物理限制 | 已证实：actor 穿过 20 kg 刚体箱时箱体位移 0.01 m（即没有接触力）；actor 不受重力（z 完全由磁带给出）；actor 对 RGB / 分割 / 2D 框 / gpu_lidar 可见，Label 插件对 actor 有效 | 需要物理交互的车辆、人必须用运动学模型；事件真值永远来自磁带而不是 Gazebo | §2.4 |
| 传感器吞吐 | 单个 gz 服务端每秒约 90 次传感器更新（混合负载平均 11 ms/次，约 110 MB/s）；1280×720 RGB 单独约 13 ms/帧，500×40 的 gpu_lidar 约 17.5 ms/帧；GPU 利用率 < 30%，瓶颈在 CPU 侧的渲染线程。每架机 RGB+深度+分割+LiDAR（10/10/5/10 Hz）时：1 架 RTF 2.58，3 架 0.84，5 架 0.62，10 架 0.25 | Realistic 档的传感器容量约"每进程 2.5 架实时"；扩容靠多进程并行 | §2.5 |
| 并行实例 | 同一块 A40 上 4 个服务端（各 3 架）同时运行：每个 RTF 0.69，总吞吐 362 MB/s（单实例的 3.3 倍），每实例约 3 核 | 批量数据集生产按"每次运行一个 gz 进程"横向扩展 | §2.5 |
| 进程外锁步 | 经 gz-transport：`set_pose_vector` 0.65 ms、`multi_step` 请求 0.35 ms，但每个 0.1 s 节拍要等渲染完成，3 架带 LiDAR 时 219 ms/节拍（RTF 0.46），10 架相机时 272 ms（RTF 0.37）；帧内容对应本节拍注入的位姿（误差 0.11 m，滞后一拍时为 1.78 m） | 调试与小规模可用；生产锁步用 r9 的进程内时钟门控（r9 实测 RTF 0.95） | §2.6 |
| 进程内从动 | 进程内 Python System 按"FleetSim 供给"逐步写 3 架机体位姿：Gazebo 回报位姿与供给值逐位相同（误差 0）；3 路相机 + 42 个 actor 时 RTF 1.15 | "Gazebo 当渲染器、FleetSim 当飞行权威"的从动模式可行 | §2.6 |
| ROS 2 桥 | `ros_gz_bridge`（Jazzy 1.0.24）：1280×720 图像 10 Hz（26 MB/s）、深度 9.8 Hz、点云 9.2 Hz、里程计 50 Hz、时钟 250 Hz，时间戳为仿真时间 | ROS 2 只作为对外生态接口，不进 Drone4D 主链路 | §2.7 |
| 降雨 | 粒子发射器能出画面，但发射体朝向与尺寸语义需逐世界调参；在 8k / 20k 粒子/s 下 LiDAR 射线受影响比例只有 0.06% / 0%，RTF 下降约一半 | 雨雾不交给 Gazebo，统一在 Sensor Service 做物理参数化退化（r3、r4 已实现） | §2.8 |
| PX4 SITL 多机 | 官方镜像（gz 8.15）在小镇 GLB 世界上 1 / 3 / 5 / 10 架 x500 全部在 9–11 s 内 "Ready for takeoff"，起飞到 2.5 m；RTF 1.00 / 1.00 / 0.99 / 0.76–0.96；每架 PX4 0.22–0.34 核、RSS 约 20 MB；gz 0.35–1.33 核 | SITL 档可行；需要 GCS 心跳与平整起降垫（§2.9） | §2.9 |

**关键结论（实现者先读）**

1. **Gazebo 适合作为 D3 的 Realistic 物理档，不适合作为默认的传感器档。** 刚体动力学（DART + bullet 网格碰撞、旋翼模型、WindEffects）和 PX4 SITL 都能在 GLB 世界上跑起来，物理开销与碰撞网格规模几乎无关；但 gz 传感器每次更新约 11 ms（RGB 13 ms、gpu_lidar 17.5 ms），而 r3 的 EGL 光栅化 720p RGB+深度+实例只要 1.4–1.6 ms，r4 的 Warp 网格 LiDAR 每帧核函数约 0.07 ms。**推荐：两档共用同一个 Sensor Generation Service（M25），Gazebo 只提供位姿；gz 原生传感器作为可选的"生态 / 交叉验证"模式。** 这样 Fast 与 Realistic 的观测差异只来自飞行动力学，便于做对照实验（design-r1 P2 的退出标准）。
2. **事件真值只来自 Canonical Event Tape。** actor 不参与碰撞、不受重力，Gazebo 里的人和车只是"可被传感器看到的皮肤"。磁带进入 Gazebo 有两条路：离线导出 SDF 轨迹（偏差约 0.3 m，零运行时代码）和进程内 TapeSystem（逐步精确采样）。锁步数据集生产用后者。
3. **GLB → SDF 有一条容易踩的坑：roll = +π/2。** 不加这个旋转，整个小镇会竖起来；节点矩阵（含 Sketchfab 的 2.5 倍缩放）gz 会照常应用。米制尺度另由 M22 标定（r2、r3 认为小镇应再放大约 4 倍），导出器只需要 `scale` 与平移。
4. **同步方式分三种，对应 D2 `ClockCaps.mode` 的三个取值**：`lockstep`（Drone4D 门控每一步，数据集与可复现实验）、`free_running`（Gazebo 尽快跑，Drone4D 跟随 gz 时钟，批量探索）、`slaved_realtime`（PX4 SITL，RTF 锁 1）。进程外 gz-transport 锁步只有 0.37–0.46 RTF，生产链路应在 gz 进程内门控（r9 的 C++ `Drone4DClock`，或本文的 Python System 原型）。
5. **容量规划按传感器更新次数，而不是按机体数。** 每个 gz 进程约 90 次传感器更新/s；一次 300 s、3 架、10 Hz 全套传感器的事件复跑约需 6 分钟墙钟；一块 A40 上可并行 4 个进程（实测 3.3 倍）。同样任务在 Fast 档按 r3/r4 的数字约 0.5–1 分钟，所以 Realistic 档用于少量高可信复跑，不用于大规模策略搜索。
6. **gz-sim 8.15（Harmonic）的两个已复现缺陷要写进导出器**：`<loop>false</loop>` 的 actor 会让带渲染传感器的世界卡死在首帧；actor 无脚本时，`TrajectoryPose` 会与 actor 自身 `<pose>` 相乘（`<pose>` 必须为单位位姿）。

---

## 1. 研究范围与环境

### 1.1 版本

| 组件 | 版本 | 来源 |
|---|---|---|
| gz-sim | 8.15.0（Harmonic） | `ros-jazzy-gz-sim-vendor 0.0.13`（TUNA 镜像的 ROS 2 Jazzy 源；osrf apt 源在 dmax 上只有约 29 KB/s） |
| gz-rendering / Ogre-Next | 8.2.3 / `gz-ogre-next-vendor 0.0.5` | 同上 |
| gz-physics / DART | 7.8.0 / `gz-dartsim-vendor 0.0.3` | 同上 |
| gz-sensors / gz-common / gz-transport / sdformat | 8.2.2 / 5.9.0 / 13.6.0 / 14.9.0 | 同上 |
| ros_gz_bridge | 1.0.24 | 同上 |
| Python 绑定 | `gz.sim8`、`gz.transport13`、`gz.msgs10`、`gz.math7` | vendor 包自带，无需编译 |
| PX4 + Gazebo | `px4io/px4-sitl-gazebo:latest`（Ubuntu 24.04，gz 8.15.0，构建于 2026-09-11） | Docker Hub 直连超时，DaoCloud 镜像不在白名单，经 `docker.1ms.run` 拉取成功 |
| x500 模型 | PX4-gazebo-models `e5997f4`（2026-10-04），稀疏克隆 | 去掉 PX4 专用的 `MotorFailurePlugin` 后另存为 `models/x500_vc` |
| actor 蒙皮 | Fuel `Mingfei/actor` 的 `walk.dae`（2,277,322 B） | 容器内无 DNS，在宿主机下载后挂载 |
| 减面 | `fast-simplification 0.2.0`、`trimesh 5.1.1` | pip（TUNA） |

### 1.2 数据与坐标

- town = `an_entire_small_town_scanned_by_a_drone.glb`（199.7 万三角形，16 张 4096² 纹理）；oryahovo = `oryahovo_-_crop_1_-_metashape_texture_problems.glb`（287.9 万）；hilltop = `anciolina_3d_hilltop_village_scan_snowy.glb`（300.0 万，1 张 8192²）。
- town 实验使用 r6 的居中 ENU 帧（`out/smalltown/mesh_enu.npz`，glTF 节点矩阵之后的单位，未做米制标定）。事件实验使用 r6 的 stage1 区域：oryahovo 按 20.9 m/单位放大，以区域中心为原点，配套 r6 生成的磁带 `tape_abc`（`drone4d.tape.v1`，10 Hz，3600 拍，42 人 + 1 车）。
- 尺度提醒：r3 认为 town 还需放大约 4 倍才是米制，r2 在做 OSM 配准；本文 town 的米数均为"r6 帧单位"，stage1 的米数为 r6 的米制假设。

### 1.3 共享机条件（影响部分数字）

- GPU 2、3 与 r1/r3/r4/r9 等单元共用。E4 的 `full_n1`、`full_n3` 采集时 GPU 2 有他人进程占用 48 GB 显存但利用率低；`full_n10` 首次采集时 GPU 2 利用率 62%（结果 RTF 0.134），GPU 空闲后复测为 0.254（`full_n10_b`），下文采用复测值。
- E3 第一次运行卡死后，有一个 `gz sim` 进程（被 §2.4 的缺陷卡在首帧，单核 100%）一直残留到收尾才被发现并随容器删除。它只占 16 核配额中的 1 核、不发布任何话题；E3–E5 的结果可能因此略偏保守。

---

## 2. 实验与实测

### 2.1 E1：GLB 作为视觉 + 无头 EGL 渲染

方法：世界里只放一个静态 GLB 视觉模型和 4 个探针相机（正射 RGB 1280×800、正射深度 640×400、斜视、街景），测从启动到 4 路图像全部到达的时间、服务端 RSS、显存和 RTF。

```bash
# 镜像构建（dmax，约 4 分钟）
cd /data/anet-drone4d-d3/r5-gazebo/docker && docker build -t d3-r5-gazebo:harmonic .
docker run -d --name d3-r5-gazebo --gpus '"device=2,3"' --cpus 16 --memory 96g --shm-size 8g \
  -e NVIDIA_DRIVER_CAPABILITIES=all -v /data/anet-drone4d-d3/r5-gazebo:/work \
  -v /data/anet-drone4d-d3/data:/glb:ro d3-r5-gazebo:harmonic sleep infinity
# E1（pose = x y z roll pitch yaw；roll=+pi/2 见 2.2）
docker exec d3-r5-gazebo bash -lc "source /opt/ros/jazzy/setup.bash; cd /work; \
  python3 scripts/e1_load.py town /glb/an_entire_small_town_scanned_by_a_drone.glb 25.769 111.8435 27.1156 1.5707963 0 0"
```

| GLB | 三角形 | 纹理 | 首个 /stats | 4 路图像全部到达 | 服务端 RSS | 显存 | RTF（250 Hz） |
|---|---|---|---|---|---|---|---|
| town | 199.7 万 | 16 × 4096² | 2.2 s | 13.4 s | 2.56 GB | 2.22 GB | 1.00 |
| oryahovo | 287.9 万 | 16 × 4096² | 2.2 s | 13.3 s | 3.01 GB | 2.41 GB | 1.00 |
| hilltop | 300.0 万 | 1 × 8192² | 2.2 s | 7.3 s | 2.05 GB | 1.29 GB | 1.00 |

说明：

- 首帧时间主要花在纹理上传（hilltop 只有一张纹理，快一半）。服务端在首次渲染完成前会阻塞首个需要渲染的仿真步，因此任何锁步控制器都要先做一次"传感器就绪屏障"（r9 也有同样结论）。
- EGL 需要 glvnd 能找到 NVIDIA 的 ICD。nvidia-container-toolkit 只挂载 `libEGL_nvidia.so`，不会自动生成 `10_nvidia.json`；本镜像在 Dockerfile 里写入了该文件，PX4 官方镜像需要手工补（§2.9）。
- 渲染样例：`results/e1_town_top.png`（正射，北向上，与 r6 正射图一致）、`results/e1_town_street.png`（25 m 斜视，屋顶与树木清晰）、`results/e1_town_r0_oblique.png`（不加 roll 时只看到竖立的"墙"）。

### 2.2 E1b：GLB 坐标规则与对齐验证

观察：pose 不加旋转时，town 变成一面立在 x–z 平面上的墙（正射深度 387–673 m，全部挤在图像南缘）；加上 roll = +π/2 后，纹理、朝向与 r6 正射图一致。由此确定 gz 的 GLB 处理方式：**节点矩阵（包括 Sketchfab 根节点的旋转与 2.5 倍缩放）照常应用，但 glTF 的 Y-up 不转换**。

对齐公式（r6 的居中 ENU：`V_enu = (x_g, -z_g, y_g) - ctr`；区域帧再乘尺度 S、减区域中心与 z0）：

```text
gz <visual><mesh><scale> = S
gz <pose>                = (t_x, t_y, t_z, +pi/2, 0, 0)
t                        = -S * (ctr + [cx, cy, 0]) - [0, 0, z0]
town  （S=1，无区域）     : t = ( 25.769,  111.8435,  27.1156)
stage1（S=20.9，oryahovo）: t = (-93.0206, 1584.655, 176.0592)   # ctr=(-0.5493,-70.8208,-10.7492), c=(5,-5), z0=48.5996
```

验证：用正射深度相机反算地面高程，与 r6 用 Embree 对全网格射线求交得到的 DSM 逐像素比较（`scripts/e1_align.py`）。

| 世界 | 像素数 | 高差中位数 | MAD | 平移搜索的最优偏移 |
|---|---|---|---|---|
| town（600 m 高，0.5 m DSM） | 106,332 | +0.6 mm | 2.3 cm | (0, 0) |
| stage1（120 m 高，×20.9） | 307,200 | -7.1 mm | 6.1 mm | (0, 0) |

p90 绝对差（town 0.22 m、stage1 4.6 m）来自立面与树冠处深度像素足印和 0.5 m DSM 网格的不一致，不是配准误差。结论：**gz 深度相机输出的是平面深度（沿光轴的 z），可以直接用于 Sensor Service 的帧契约对照**。

### 2.3 E2：减面碰撞网格与物理

#### 2.3.1 减面

从 r6 的 town 网格（199.7 万面，焊接 UV 接缝后 100.3 万顶点）做二次误差减面（`scripts/mk_collision.py`），再用 Embree 在 1 m 网格上向下射线求交，与 r6 的全网格 DSM 比较竖向误差（`scripts/e2_dsm_err.py`，r2 的 venv）。

| 级别 | 面数 | 减面耗时 | STL | 竖向误差中位数 | p90 | p99 | >1 m 比例 |
|---|---|---|---|---|---|---|---|
| 20k | 20,000 | 3.8 s | 1 MB | 0.164 | 0.501 | 1.006 | 1.02% |
| 100k | 99,999 | 3.3 s | 5 MB | 0.089 | 0.305 | 0.803 | 0.52% |
| 500k | 500,000 | 2.8 s | 25 MB | 0.030 | 0.114 | 0.443 | 0.15% |
| full | 1,996,759 | — | 100 MB | 0 | 0 | 0 | 0 |

单位为 r6 帧单位；若按 r3 的 ×4 米制换算，100k 的竖向误差中位数约 0.36 m。r2 是正式的减面碰撞网格负责单元，本文的 STL 只作实验用。

#### 2.3.2 物理开销与接触

世界：碰撞网格 + N 架 x500（PX4 的 4 个 MulticopterMotorModel + gz `MulticopterVelocityControl`，悬停于 DSM 上方 15 m）+ 6 个 0.6 m、1 kg 的箱体从 8 m 高处落下；`rtf=0`（尽快运行），无传感器。`uav_0` 在 8 s 后以 2 m/s 下降，用于验证机体与网格的接触（`scripts/e2_physics.py`、`scripts/e2_sweep.sh`）。

| 配置 | 检测器 / 引擎 | N | ms/步 | 步/s | RTF | 核 | RSS |
|---|---|---|---|---|---|---|---|
| 无碰撞网格（对照） | bullet / dartsim | 10 | 1.60 | 627 | 2.51 | 1.50 | 0.38 GB |
| 20k | bullet / dartsim | 10 | 1.97 | 513 | 2.05 | 1.52 | 0.39 GB |
| 100k | bullet / dartsim | 10 | 1.82 | 552 | 2.21 | 1.56 | 0.42 GB |
| 500k | bullet / dartsim | 10 | 1.75 | 573 | 2.29 | 1.59 | 0.59 GB |
| full 200 万 | bullet / dartsim | 10 | 1.87 | 548 | 2.19 | 1.49 | 1.16 GB |
| 100k | ode / dartsim | 10 | 2.08 | 482 | 1.93 | 1.48 | 0.43 GB |
| 500k | ode / dartsim | 10 | 2.15 | 469 | 1.88 | 1.60 | 0.60 GB |
| full 200 万 | ode / dartsim | 10 | 2.22 | 515 | 2.06 | 1.57 | 1.28 GB |
| 100k | bullet-featherstone | 10 | 1.50 | 672 | 2.69 | 1.65 | 0.40 GB |
| 100k | bullet / dartsim | 1 | 0.81 | 1242 | 4.97 | 1.34 | 0.23 GB |
| 100k | bullet / dartsim | 3 | 1.16 | 863 | 3.45 | 1.34 | 0.28 GB |
| 100k | bullet / dartsim | 20 | 3.16 | 318 | 1.27 | 1.68 | 0.63 GB |
| 100k + GLB 视觉（无传感器） | bullet / dartsim | 10 | 1.62 | 623 | 2.49 | 1.69 | 0.43 GB |
| 100k，`rtf=1` | bullet / dartsim | 10 | 4.04 | 250 | 1.00 | 1.12 | 0.42 GB |

接触验证：

- `uav_0` 下降后停在 z = 16.49，该处 DSM 为 16.45：机体与网格接触正常（无碰撞网格时坠到 -59 m）。
- 落在平坦处、水平漂移 < 0.5 m 的箱体，静止高度与 DSM + 0.3 m 的差为 -0.21–+0.09 单位（100k），与减面误差同量级；落在屋顶或坡面上的箱体会滑走 2–6 m，属于正常物理行为。
- 结论：飞行中的机体只在宽相位（AABB）层面碰到网格，三角形数量几乎不影响步长；网格规模只决定内存和"贴地 / 撞楼"时的窄相位开销。x500 单机边际成本约 0.12 ms/步，固定开销约 0.7 ms/步，单进程 250 Hz 实时上限约 25 架（只算物理）。

### 2.4 E3：事件磁带驱动的 actor

世界：stage1（oryahovo ×20.9 视觉 + 10 万面碰撞网格），r6 磁带中 42 个行人作为 actor，1 辆车作为运动学箱体；广场（100, -95）上方 120 m 放 1280×960 的 2D 包围框相机、RGB、深度、实例分割探针；30 m 高处放一个倒装的 500×40 gpu_lidar；在 person_A 第 30 s 的位置放一个 1 m、20 kg 的刚体箱。仿真 60 s，`rtf=0`（`scripts/e3_actor.py`、`scripts/tape2sdf.py`、`scripts/tape_system.py`）。

精度指标：每一帧 2D 包围框（Label 插件给每个 actor 唯一标签）中心反投影到地面，与同一仿真时刻磁带位置（脚底 + 0.9 m）比较；同时分解为沿航向 / 垂直航向两个分量。

| 方式 | 运行时额外代码 | SDF 大小 | RTF | 偏差中位数 | p90 | 沿航向均值 ± 标准差 | 垂直航向 ± 标准差 | LiDAR 命中 actor 点数 |
|---|---|---|---|---|---|---|---|---|
| SDF 轨迹，1 Hz 路点（2,815 个） | 无 | 274 KB | 1.60 | 0.289 m | 0.489 m | +0.274 ± 0.139 | -0.012 ± 0.128 | 7,219 |
| SDF 轨迹，10 Hz 路点（27,384 个） | 无 | 2.39 MB | **0.37** | 0.287 m | 0.489 m | — | — | 7,233 |
| 进程内 TapeSystem（未校正） | Python System | 25 KB | 1.52 | 0.995 m | 1.67 m | +0.938 ± 0.514 | +0.057 ± 0.340 | 1,706 |
| 进程内 TapeSystem（根节点前移 0.66 m 校正 + 航向插值） | Python System | 25 KB | 1.62 | 0.568 m | 1.05 m | +0.271 ± 0.519 | +0.059 ± 0.342 | 3,921 |

要点：

1. **SDF 轨迹足够准，代价在路点数。** 两种路点密度精度相同，说明 0.29 m 的偏差几乎全部来自"包围框中心 ≠ 骨盆"的形体偏差（行走时手臂与前倾使框中心沿航向前移约 0.27 m）。但 gz 在渲染侧逐帧求轨迹，路点加密 10 倍后 RTF 从 1.60 降到 0.37。导出器默认 1 Hz 路点，并在状态切换处插一对间隔 1 ms 的路点。
2. **`<loop>false</loop>` 会让世界卡死。** 二分实验（`scripts/e3_bisect.py`，以及 `worlds/e3min_A..G.sdf` 七个最小世界）：同一个 actor，`loop=true` 正常，`loop=false` 只要世界里有任何渲染传感器，主线程就以单核 100% 卡在首个渲染步，`/clock` 不再推进；没有相机时不卡。规避：导出 `loop=true`，并在末尾追加一个"86400 s 后仍在原地"的路点，保证一次运行内不会回绕。
3. **进程内驱动是精确的，但要校正蒙皮。** `gz.sim8` 的 `Actor.set_trajectory_pose` / `set_animation_time` 可以在 PreUpdate 中直接写组件。两个坑：(a) actor 被 World System 配置时还没有创建，实体要在第一次 PreUpdate 时再解析；(b) 渲染位置 = actor 的 `<pose>` × `TrajectoryPose`，所以 actor 的 `<pose>` 必须是单位位姿（最初给了 z = -500 的"停放"位姿，结果所有 actor 都跑到了地下）。另外 walk.dae 绑定姿态的根节点把身体放在 `TrajectoryPose` 前方约 0.66 m，需按航向补偿。校正后沿航向偏差与 SDF 轨迹方式一致（+0.27 m），但标准差较大（0.52 m），来自按里程映射动画时间时的步态相位抖动；生产版应按 FollowActor 的做法从骨架求动画的 x 向速度。
4. **actor 不参与物理。** person_A 在 25–35 s 穿过刚体箱，箱体在这 10 s 内位移 0.010 m（`results/e3_s10_p42_pass_30s.png` 可见人体与箱体互相穿插）；actor 的 z 完全来自磁带，不受重力。需要与环境交互（推门、上车、被车撞）的实体必须用运动学或刚体模型，交互结果也必须由 Event Runtime 决定。
5. **actor 对传感器可见。** 包围框相机看到 10 个 actor 标签（Label 插件对 actor 有效），实例分割、RGB 可见，gpu_lidar 在 602 帧里有 7,219 个点落在 actor 身上。
6. 磁带中不可见的状态（ABSENT / BOARDED / INSIDE / GONE）导出为"停放在 z = -500"，在网格下方，俯视相机与 LiDAR 都看不到。

### 2.5 E4：传感器帧率、吞吐与并行

世界：town GLB 视觉 + 10 万面碰撞；N 架 x500 在 DSM 上方 30 m 以 3 m/s 画圈；每架挂 RGB 1280×720、深度 640×360、实例分割 640×360、500×40 gpu_lidar（MID-360 视场倒装），云台下俯 0.6 rad；`rtf=0`，稳定后统计 30 s（`scripts/e4_sensors.py`、`scripts/e4_sweep.sh`）。所有传感器的仿真频率都严格等于配置值，下表的"墙钟频率"即每架机每秒实际产出的帧数。

| 配置 | N | RTF | RGB 墙钟 Hz | LiDAR 墙钟 Hz | 传感器更新/s | 每次更新 | 总吞吐 | 核 | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| 全套（10/10/5/10 Hz） | 1 | 2.576 | 25.7 | 25.7 | 90 | 11.1 ms | 112 MB/s | 3.1 | |
| 全套 | 3 | 0.844 | 8.4 | 8.4 | 89 | 11.3 ms | 111 MB/s | 3.0 | |
| 全套 | 5 | 0.619 | 6.2 | 6.2 | 108 | 9.2 ms | 136 MB/s | 3.9 | |
| 全套 | 10 | 0.254 | 2.6 | 2.5 | 89 | 11.2 ms | 112 MB/s | 3.4 | GPU 空闲时复测 |
| 全套，`rtf=1` | 10 | 0.266 | 2.7 | 2.7 | 93 | 10.7 ms | 118 MB/s | 3.7 | 跑不到实时 |
| 仅 RGB 30 Hz | 10 | 0.249 | 7.5 | — | 75 | 13.3 ms | 209 MB/s | 4.6 | 1280×720 |
| 仅 LiDAR 10 Hz | 10 | 0.572 | — | 5.7 | 57 | 17.5 ms | 18 MB/s | 3.2 | 2 万射线/帧 |
| 4 实例并行（各 3 架全套） | 4×3 | 0.69 ×4 | 6.9 | 6.9 | 4×72 | 13.8 ms | **362 MB/s** | 4×3.0 | 同一块 GPU |

解读：

- 单个 gz 服务端的传感器容量是一个常数：**约 90 次更新/s（约 110 MB/s）**，与机体数无关。所有传感器在一个渲染线程里串行执行（渲染 → 回读 → 格式转换 → 序列化发布），GPU 利用率 7–41%，所以瓶颈在 CPU 侧的单线程流水线。由此 RTF ≈ 90 /（每架每仿真秒的传感器更新数 × N）：全套每架 35 次/仿真秒，3 架时预测 0.86、实测 0.84；10 架时预测 0.26、实测 0.25。
- **扩容靠多进程。** 同一块 GPU 上 4 个服务端并行，总吞吐 3.3 倍，每个进程约 3 核；dmax 的 16 核配额下 4–5 个进程是合理上限，GPU 不是瓶颈。
- 与 Fast 档对比（同一 town、同量级分辨率；数字来自 r3、r4）：EGL 光栅化 720p RGB+深度+实例 1.4–1.6 ms/帧，约快 8 倍；Warp 网格 LiDAR 2 万射线核函数约 0.07 ms/帧，100 架完整管线 6 ms，比 gz gpu_lidar 快两个数量级以上，而且 gpu_lidar 只能产生均匀网格扫描，无法表达 MID-360 的非重复花样（r9 §2.5、r4）。
- 样例：`results/e4_full_n1__uav_0_cam.png`、`..._depth.png`、`..._seg_labels_map.png`、`e4_full_n1_uav0_lidar_ranges.npy`（40×500 距离图）。

### 2.6 E5：与外部权威的同步

#### 2.6.1 进程外锁步（gz-transport）

模式：服务端以暂停状态启动；控制器每个 0.1 s 节拍依次 ① `set_pose_vector`（N 架机体 + 1 个以 8 m/s 运动的目标箱）② `/world/e5/control multi_step` ③ 等 `/world/e5/clock` 到达节拍末 ④ 等本节拍内所有相机（和 LiDAR）帧到齐。机体为无重力、无碰撞的动力学刚体（`kinematic_dyn`），由位姿命令驱动（`scripts/e5_lockstep.py`）。

| 配置 | set_pose_vector | control 请求 | 步进到时钟 | 时钟后等帧 | 每节拍合计（p95） | 节拍/s | 等效 RTF | 一致性 |
|---|---|---|---|---|---|---|---|---|
| 3 架，相机 640×360 + LiDAR，4 ms × 25 步/节拍 | 0.65 ms | 0.35 ms | 42.6 ms | 175.1 ms | 218.7 ms（252.6） | 4.57 | 0.457 | 0.117 m（滞后一拍时 1.78 m） |
| 3 架，同上，100 ms × 1 步/节拍（纯渲染器） | 0.66 ms | 0.36 ms | 38.9 ms | 186.4 ms | 226.4 ms | 4.42 | 0.442 | 0.116 m（1.78 m） |
| 10 架，仅相机，100 ms × 1 步/节拍 | 0.89 ms | 0.43 ms | 47.7 ms | 223.4 ms | 272.4 ms | 3.67 | 0.367 | 0.105 m（1.82 m），60/60 拍命中 |

结论：

- 状态注入与渲染是一致的：帧时间戳落在注入节拍内时，画面中的目标与本节拍注入的位姿一致（偏差约 0.1 m，即像素量化与框中心误差），与上一拍位姿相差 1.8 m，排除了滞后一拍。3 架 LiDAR 配置中有 36% 的节拍没拿到目标框，原因是 2D 框相机的帧时间戳偶尔落在节拍边界外（时间戳相对节拍末有 0 和 8 ms 两种），是本实验匹配逻辑的局限，不影响结论。
- 进程外锁步的成本几乎全部是"等渲染"：命令通道本身约 1 ms，但锁步消除了仿真与渲染的流水线重叠，每个节拍的传感器更新串行完成后才能进入下一拍，所以比自由运行（§2.5）慢约 2 倍。
- r9 的提醒同样适用：`gz.transport13` 的 Python `request()` 阻塞时不释放 GIL，与订阅回调并发时可能丢应答（r9 已复现）；本实验 100 拍全部成功，但生产链路不应依赖 Python 的进程外请求。

#### 2.6.2 进程内从动（PythonSystemLoader）

模式：`tape_system.py` 作为 World System 运行在 gz 进程内，每个 PreUpdate 按当前仿真时间写入 42 个 actor、1 辆车和 K 架"FleetSim 供给"的机体位姿（这里用解析圆轨迹代替 FleetSim），Gazebo 只负责渲染（`scripts/e5b_inserver.py`）。

| K | 传感器 | RTF | 相机仿真频率 | 机体位姿（gz 回报）与供给值之差 | PreUpdate 开销 |
|---|---|---|---|---|---|
| 3 | 3 × 640×360 RGB 10 Hz + 42 个 actor | 1.146 | 10.0 Hz | 1,077 个样本全部为 0（逐位一致） | 0.87 ms/步（46 个实体） |

结论：进程内写 ECM 不存在排序问题，位姿在同一步的 PostUpdate 渲染快照中生效。Python 每实体约 20 µs/步；4 ms 步长下 46 个实体占 0.87 ms，规模再大时应改 C++，或只在传感器到期的步写入（10 Hz 磁带下最多 0.1 s 陈旧）。r9 用 C++ System 做了同样的门控（每步等待 Drone4D 授权），3 架 RGB+LiDAR 锁步 RTF 0.95，并验证了跨运行、跨 GPU 的逐位确定性。

### 2.7 E6：ROS 2 Jazzy 桥

```bash
ros2 run ros_gz_bridge parameter_bridge \
  /uav_0/cam@sensor_msgs/msg/Image[gz.msgs.Image  /uav_0/depth@sensor_msgs/msg/Image[gz.msgs.Image \
  /uav_0/lidar/points@sensor_msgs/msg/PointCloud2[gz.msgs.PointCloudPacked \
  /uav_0/odom@nav_msgs/msg/Odometry[gz.msgs.Odometry  /clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock \
  /uav_0/cmd_vel@geometry_msgs/msg/Twist]gz.msgs.Twist
```

1 架机、`rtf=1`、town 世界（`scripts/e6_ros2.sh`，`results/e6_ros2.txt`）：

| 话题 | ROS 2 侧频率 | 带宽 |
|---|---|---|
| `/uav_0/cam`（1280×720 rgb8） | 9.99 Hz | 25.97 MB/s |
| `/uav_0/depth`（640×360 32FC1） | 9.84 Hz | — |
| `/uav_0/lidar/points`（2 万点） | 9.23 Hz | 6.12 MB/s |
| `/uav_0/odom` | 50.0 Hz | — |
| `/clock` | 249.7 Hz | — |

`frame_id` 为 `uav_0/payload/cam`，时间戳为仿真时间。可满足 ROS 生态用户（FAST-LIVO2、RTAB-Map 等）直接订阅；D2 的决策（ROS 不进主链路，ADR-020）不需要改变：桥只在 Realistic 档作为可选的对外出口。

### 2.8 E7：降雨（粒子发射器）

在静态探针（相机 + 深度 + 倒装 LiDAR）上方放箱形粒子发射器，比较有无雨时的 LiDAR 逐射线距离（`scripts/e7_rain.py`）：

| 配置 | 发射率 | `particle_scatter_ratio` | 缩短 > 1 m 的射线 | 丢失射线 | RTF（无雨 → 有雨） | 画面 |
|---|---|---|---|---|---|---|
| r8k | 8,000 /s | 0.65 | 0.06% | 0 | 5.16 → 2.78 | 一条稀疏的白色方块柱（无纹理的公告板） |
| r20k（改发射体尺寸轴向） | 20,000 /s | 0.65 | 0% | 0 | 5.17 → 2.28 | 画面中看不到粒子 |

结论：Gazebo 的雨是纯视觉粒子，发射体尺寸 / 朝向的语义需要逐世界调参，无纹理时不像雨，对 LiDAR / 深度的影响可以忽略，却让渲染变慢一半。**雨雾雪统一在 Sensor Service 中按物理量（降雨 mm/h、能见度 → 消光系数 σ）做后处理**：r3 已有 720p 雨线 + 消光 1.7–2.4 ms 的实现，r4 已有 905 nm 消光与近场假回波模型；两档共用，参数来自磁带的环境时间线。Gazebo 侧只负责风对机体的力（WindEffects，系数按 r23 标定，不能用默认 k = 1）。

### 2.9 E8：PX4 SITL 多机（可选项，已完成）

方法：gz 服务端先启动 town 世界（**世界级不写任何 `<plugin>`**，于是 gz 加载 PX4 的 `server.config`：Physics、UserCommands、SceneBroadcaster、Contact、Imu、AirPressure、AirSpeed、ApplyLinkWrench、NavSat、Magnetometer、Sensors 和两个 PX4 自定义系统）；世界写入 `<spherical_coordinates>`；在 DSM 平坦处（3.5 m 窗口内起伏 < 0.4）每个出生点放 1.6 m × 1.6 m 起降垫。然后启动 N 个 `px4 -i k`（`PX4_GZ_STANDALONE=1 PX4_SYS_AUTOSTART=4001 PX4_SIM_MODEL=gz_x500 PX4_GZ_WORLD=d3town PX4_GZ_MODEL_POSE=x,y,z`），等待 "Ready for takeoff" 后逐架 `px4-commander --instance k takeoff`（`scripts/e8_mkworld.py`、`scripts/e8_px4.py`）。

| N | 全部就绪 | 地面 RTF | 起飞后 RTF | PX4 合计核（每架） | gz 核 | 爬升（目标 2.5 m） | PX4 RSS/架 |
|---|---|---|---|---|---|---|---|
| 1 | 9.0 s | 1.000 | 1.000 | 0.23（0.23） | 0.40 → 0.34 | 2.49 | 19 MB |
| 3 | 9.0 s | 0.999 | 0.999 | 1.01（0.34） | 0.86 → 0.69 | 2.54–2.69 | 19 MB |
| 5 | 9.0 s | 0.994 | 0.997 | 1.50（0.30） | 1.02 → 0.90 | 2.49–2.64 | 20 MB |
| 10 | 11.0 s | 0.762 | 0.955 | 2.47（0.25） | 1.33 → 1.29 | 2.49–2.73 | 20 MB |

踩坑记录：

- 第一次运行 arming 被拒（"No connection to the GCS"、"Attitude failure (roll)"）。前者要有 GCS 心跳：本实验用 r22 的 `mavlite` 以 1 Hz 向每个实例的 GCS 端口（UDP 18570 + k）发 HEARTBEAT；后者是机体出生在摄影测量网格的斜面或树冠上，加平整起降垫后消失（这也正是"机巢"在 D2 M20 里的语义）。
- PX4 以 gz `/clock` 为时间源（lockstep_scheduler），gz 不等待 PX4 输出，因此 SITL 档只能做"实时从动"（`slaved_realtime`），不能逐位复现（r9 §2.8 同结论）。
- 10 架落地等待时 RTF 0.76，起飞后回到 0.96：起降垫上的接触求解比空中贵。

---

## 3. 可复用实现

全部在 `.cache/research/d3/r5-gazebo/scripts/`，可直接作为 M24 Gazebo Adapter 的原型。

| 文件 | 作用 | 落点 |
|---|---|---|
| `gen_world.py` | World Package 原型 → SDF：GLB 视觉（scale + roll 修正）、碰撞 STL、x500 + 传感器载荷（相机 / 深度 / 分割 / 2D 框 / gpu_lidar）+ 速度控制 + 里程计、actor（有无脚本两种）、运动学体、探针、Label、PythonSystemLoader | `awr/world/export/gz.py`（ADR-048 已规划的路径） |
| `tape2sdf.py` | `drone4d.tape.v1` → actor 路点：可见性、停放、骨盆高度、状态切换处的成对路点、`loop=true` + 超长驻留路点 | 同上 |
| `tape_system.py` | 进程内磁带驱动：懒解析实体，逐步写 actor `TrajectoryPose` / `AnimationTime`、车辆与从动机体位姿；根节点偏移补偿；航向圆插值 | `awr/sim/backends/gz/systems/`（生产版改 C++） |
| `mk_collision.py`、`e2_dsm_err.py` | 焊接 UV 接缝 → 二次误差减面 → STL；竖向误差评估 | M22 World Import |
| `e1_align.py` | gz 深度 vs DSM 的配准验收（中位数 / MAD / 平移搜索） | M22 导出验收测试 |
| `gzutil.py`、`runsim.py` | Python gz-transport 订阅计数、帧时间戳、WorldControl、进程组启动 / 计 CPU / 计 RSS、分区隔离 | 测试工具 |
| `e5_lockstep.py` | 进程外锁步参考实现（set_pose_vector + multi_step + 按时间戳等帧 + 一致性检查） | 调试工具 |
| `e8_mkworld.py`、`e8_px4.py`、`mavlite.py` | PX4 兼容世界（去世界级插件、球面坐标、起降垫）、多实例启动、GCS 心跳、起飞与测量 | SITL 档启动器 |
| `docker/Dockerfile` | Ubuntu 24.04 + ROS 2 Jazzy vendor Gazebo Harmonic + EGL ICD | 部署 |

关键片段一：GLB 视觉模型（glTF Y-up 修正）

```xml
<model name="world_visual"><static>true</static>
  <pose>25.769 111.8435 27.1156 1.5707963 0 0</pose>   <!-- t = -S*(ctr+[cx,cy,0]) - [0,0,z0]; roll=+pi/2 -->
  <link name="l"><visual name="v"><geometry><mesh>
    <uri>/glb/an_entire_small_town_scanned_by_a_drone.glb</uri><scale>1 1 1</scale>
  </mesh></geometry></visual></link>
  <plugin filename="gz-sim-label-system" name="gz::sim::systems::Label"><label>1</label></plugin>
</model>
<model name="world_collision"><static>true</static><link name="l"><collision name="c">
  <geometry><mesh><uri>/work/col/town_100k.stl</uri></mesh></geometry></collision></link></model>
```

关键片段二：磁带 actor（无脚本，由 TapeSystem 驱动；`<pose>` 必须为单位位姿）

```xml
<actor name="person_A"><pose>0 0 0 0 0 0</pose>
  <skin><filename>/work/models/actor_walk/meshes/walk.dae</filename></skin>
  <animation name="walk"><filename>/work/models/actor_walk/meshes/walk.dae</filename><interpolate_x>true</interpolate_x></animation>
  <plugin filename="gz-sim-label-system" name="gz::sim::systems::Label"><label>10</label></plugin>
</actor>
<plugin filename="gz-sim-python-system-loader-system" name="gz::sim::systems::PythonSystemLoader">
  <module_name>tape_system</module_name></plugin>
```

```python
# tape_system.pre_update（节选）：t = info.sim_time；k0 = floor(t/dt)；a = t/dt - k0
p = (1 - a) * tp.pos[k0, i] + a * tp.pos[k0 + 1, i]
yaw = y0 + a * atan2(sin(y1 - y0), cos(y1 - y0))
z = p[2] + 1.0 if visible else -500.0                               # 骨盆高度 / 停放
act.set_trajectory_pose(ecm, Pose3d(p[0] - 0.66*cos(yaw), p[1] - 0.66*sin(yaw), z, 0, 0, yaw))
act.set_animation_time(ecm, timedelta(seconds=cum_dist / 1.0))
model.set_world_pose_cmd(ecm, Pose3d(...))                          # 车辆与从动机体
```

关键片段三：PX4 多实例（每个进程独立环境变量）

```bash
gz sim -s -r --headless-rendering -v1 /work/worlds/d3town.sdf &      # 世界级无 <plugin> → 用 PX4 server.config
for k in $(seq 0 $((N-1))); do
  (cd /tmp/px4_$k && PX4_GZ_STANDALONE=1 PX4_SYS_AUTOSTART=4001 PX4_SIM_MODEL=gz_x500 \
   PX4_GZ_WORLD=d3town PX4_GZ_MODEL_POSE="$X,$Y,$Z,0,0,0" /opt/px4-gazebo/bin/px4 -i $k -d &)
done
# GCS 心跳：UDP 127.0.0.1:(18570+k)，1 Hz；就绪后：/opt/px4-gazebo/bin/px4-commander --instance $k takeoff
```

---

## 4. 对 D3 架构的建议

### 4.1 Fast 与 Realistic 的分工

| 维度 | Fast（FleetSim + 网格射线 / 光栅） | Realistic（Gazebo） | SITL（Gazebo + PX4） |
|---|---|---|---|
| 飞行权威 | FleetSim L1（PX4-lite，numba，千架级） | gz 刚体：x500 旋翼模型 + 速度控制（或自研 `Drone4DVelocityControl`，见 r9） | PX4 飞控 + gz 刚体 |
| 与网格碰撞 | 无（或射线近似） | 有（10 万–50 万面 STL，bullet） | 有 |
| 事件实体 | Event Runtime（磁带） | 磁带 → actor（TapeSystem）+ 运动学车辆 | 同左 |
| 相机 / 深度 / 分割 | Sensor Service（EGL 光栅，r3） | **默认同一个 Sensor Service**，以 gz 位姿为输入；可选 gz 原生传感器 | 同左 |
| LiDAR | Sensor Service（Warp 网格射线，r4；MID-360 花样、逐点时刻） | 默认同上；可选 gpu_lidar（均匀网格） | 同左 |
| 天气 | 磁带环境线 → Sensor Service 退化；风进 FleetSim 动力学 | 风：WindEffects（标定 k）；雨雾：Sensor Service | 同左 |
| 时钟模式 | `lockstep` / 倍速 | `lockstep`（数据集）或 `free_running`（探索） | `slaved_realtime`（RTF 1） |
| 可复现 | 逐位（D2 已有） | 同镜像同型号 GPU 逐位（r9） | 不保证逐位 |
| 规模（单进程） | 数百至上千架（动力学）；传感器按 GPU 批量 | 物理约 25 架实时；gz 传感器约 2.5 架实时；用 Sensor Service 时取决于 GPU 批量 | 10 架 RTF 0.96 |
| 典型用途 | 大规模策略搜索、Benchmark 主体、公开站沙盒 | 高可信复跑、撞楼 / 贴地 / 抗风验证、Fast 档动力学误差标定 | 飞控交互、任务与失效保护 |

**核心建议**：Realistic 档的定位是"换掉动力学，不换传感器"。默认把 gz 当飞行与接触的权威，位姿每个传感器时刻送进与 Fast 档相同的 Sensor Service，这样：(1) 输出帧契约、标签、天气退化与 Fast 档逐项一致；(2) 传感器吞吐不受 gz 单线程渲染限制；(3) "Fast 与 Realistic 使用同一事件真值与接口，能够比较传感器及飞行模型差异"（design-r1 P2 退出标准）中的"差异"被限定在动力学一项，可以干净地归因。gz 原生传感器保留为可选项，用于 ROS 生态对接和对 Sensor Service 的交叉验证（同一位姿下 RGB / 深度可逐像素比对，§2.2 的方法）。

### 4.2 接口

1. **`GzSimBackend`（实现 D2 的 `SimBackend` / `DroneAdapter`）**：`BackendCaps` 新增 `caps/gz.json`，`kinds = [uav]`，`clock.mode ∈ {lockstep, free_running}`，`max_vehicles = 20`，`truth = true`；PX4 档另立 `caps/px4_gz.json`（`slaved_realtime`，不可暂停、不可单步，沿用 ADR-045 的规则：存在非锁步机体时 SimClock 锁定 ×1）。命令映射：GoTo / Velocity → 速度控制话题（经进程内 System 写入，避免 Python 请求丢应答）；PX4 档 → MAVLink（r21/r22）。
2. **`GzPoseSource` → `SensorBackend`**：gz 进程内 System 在每个传感器到期时刻把所有机体与云台位姿（以及磁带实体位姿，便于核对）发布到 zenoh（与 D2 的 StateRing / `awr.rt.v1` 一致），由 M25 Sensor Service 渲染。`SensorBackend.request(slots, poses_enu, t_sim_ns)` 的语义不变，只是 `poses_enu` 的来源从 FleetSim 换成 gz。
3. **`GzNativeSensorBackend`（可选）**：订阅 gz 的 `/uav_k/{cam,depth,seg,lidar}`，按时间戳打包为同一帧契约。用于交叉验证与 ROS 用户。
4. **World Package → SDF 导出器（M22/M24 共用，`awr/world/export/gz.py`）**，在 r23 映射表基础上补充 GLB 规则：

| World Package 资产 | gz SDF | 规则 |
|---|---|---|
| `appearance.glb` | 静态模型 `<visual><mesh><uri>` | pose roll = +π/2；`scale` = M22 标定的米制尺度；平移按 §2.2 公式；导出后自动跑深度 vs DSM 验收（中位数 < 1 cm） |
| `collision.mesh` | 静态模型 `<collision><mesh>` STL | 10 万–50 万面（大世界按 100–200 m 分块，每块 ≤ 5 万面）；`<dart><collision_detector>bullet</collision_detector>` |
| `raycast.bvh`、`terrain.dsm`、`occupancy.voxels` | 不导出 | 只给 Fast 档与 Sensor Service 使用 |
| `anchors.json`、`walkable.navmesh` | 不导出 | 只给 Event Runtime 使用 |
| Event Tape（人） | `<actor>`（无脚本）+ TapeSystem；或离线 `<trajectory>`（1 Hz、`loop=true`、末尾驻留） | actor `<pose>` 为单位位姿；骨盆高度 +1.0 m；不可见状态停放 z = -500；Label = 实体编号 |
| Event Tape（车） | 运动学模型（无重力；需要被撞时加碰撞体） | 由 TapeSystem 写位姿 |
| 环境时间线 | `<wind>` + WindEffects（标定 k）；`<spherical_coordinates>` | 雨雾不导出 |
| UAV 机型（M21） | x500 / P600 模型 + 载荷 link（固定关节）+ 速度控制 / PX4 | 传感器只在"gz 原生传感器"模式下导出 |

### 4.3 状态同步设计

```text
                      Drone4D Experiment Controller（权威时钟 t_sim, seed, run_id）
                                     │ grant(k ticks) / step_done(t)
          ┌──────────────────────────┼──────────────────────────────┐
          │                          │                              │
   Event Runtime（磁带）        gz-sim 进程（Realistic）           Agent Runtime
   tape(t) 只读                 ├─ Drone4DClock System：门控每一步     观测 → 策略 → 动作
          │                     ├─ TapeSystem：PreUpdate 写 actor/车    │
          └──── tape 文件 ─────▶ ├─ 物理：DART+bullet、旋翼、WindEffects  │
                                ├─ PoseTap System：传感器时刻发布位姿 ──▶ Sensor Service（M25）
                                └─ Command System：动作 → 速度控制  ◀──── DroneAdapter
                                     │ PostUpdate 位姿镜像
                                     ▼
                         FleetSim 镜像 / StateRing → Gateway → 浏览器（不变）
```

- **权威划分**（对应 design-r1 的三种权威）：事件真值 = 磁带（Event Runtime 生成，gz 只读）；飞行与碰撞 = gz（Realistic）或 FleetSim（Fast）；观测 = Sensor Service 按 `(World, tape(t), Pose_i(t), Rig_i, Env(t), seed)` 纯函数生成；Agent 只拿观测与收到的消息。
- **时钟**：`lockstep` 模式下 Drone4D 持有时钟，gz 每一步在 PreUpdate 被门控（r9 的 C++ 实现已测 RTF 0.95；本文的 Python 进程内 System 证明 ECM 写入与渲染快照同步）。`free_running` 模式下 gz 以 `rtf=0` 运行，Drone4D 以 gz 的 `/clock` 为准，SimClock 处于从动状态，UI 显示实际 RTF。
- **启动屏障**：首次渲染（纹理上传 7–13 s）完成前不开始计时；控制器在收到所有传感器第一帧后才授予第一个节拍。
- **镜像回写**：gz 的位姿在 PostUpdate 发布到 StateRing，FleetSim 进入"镜像"角色（不积分），Gateway、UI、录制与回放链路不需要改动。
- **一致性检查**：每次运行抽样核对"传感器帧时间戳 = 注入位姿的时刻"（§2.6 的方法）和"actor 渲染位置与磁带的偏差 < 0.6 m"（§2.4 的方法），写入运行报告。

### 4.4 代价与容量模型

```text
gz 原生传感器：  RTF ≈ 90 / Σ_i Σ_s f_s           （f_s：每架每仿真秒的传感器更新数；单进程，A40）
                 每次更新约 11 ms（RGB 1280×720 约 13 ms，2 万射线 gpu_lidar 约 17.5 ms），每进程约 3–4 核
物理（x500）：   ms/步 ≈ 0.7 + 0.12·N（4 ms 步长）→ 实时上限约 25 架/进程，约 1.5 核
PX4 SITL：       每架 0.22–0.34 核 + gz 约 0.35 + 0.1·N 核
并行：           同一块 A40 上 4 个 gz 进程总吞吐 3.3 倍；以 CPU 配额为上限（每进程约 3 核）
```

示例（design-r1 P0 的 5 分钟事件，3 架，RGB+深度+分割+LiDAR 10/10/5/10 Hz）：gz 原生传感器单进程约 6 分钟墙钟；Fast 档按 r3/r4 的单帧成本约 0.5–1 分钟（含 JPEG 编码）。因此推荐的作业形态是：Fast 档跑完整的策略 × 种子矩阵，Realistic 档对每个策略抽少量种子复跑以标定动力学差异。

### 4.5 与 D2 代码的衔接

- `python/awr/sim/backends/base.py`：`ClockCaps.mode` 已含 `lockstep` / `slaved_realtime` / `free_running`，`BackendCaps` 从 `packages/contracts/rt/caps/<backend>.json` 加载，新增 `gz.json`、`px4_gz.json` 即可。
- `python/awr/sim/sensors/backends/base.py`：`SensorBackend` 的 docstring 已预留 "Gazebo（V0.6）"；按 §4.2 第 2、3 条，Gazebo 不必实现 `SensorBackend` 本体，只需提供位姿源；可选的 `GzNativeSensorBackend` 才实现它。
- `python/awr/sim/sensors/lidar/scan_rt.py`：Realistic 档不改变它；D3 的 MeshLidar（r4）取代 DSM 柱体后两档共用。
- ADR-045 / P-09（仿真永不阻塞）：锁步 gz 进程与 D2 的 sim-core 主循环分离，作为独立实例运行，与 SITL-EXT 的处理方式相同。

### 4.6 落地顺序（对应 design-r1 P2）

1. M22 导出器：GLB → SDF（视觉 + 碰撞 + 球面坐标 + Label），自动配准验收。
2. TapeSystem C++ 版（含骨架动画速度、根节点偏移标定、可见性停放）与 PoseTap / Command System；合并 r9 的 `Drone4DClock`。
3. `GzSimBackend` + `caps/gz.json`；Sensor Service 接入 gz 位姿源；Fast / Realistic 同事件对照的一键脚本。
4. 1–3 架验收后扩展到 10 架；再接 PX4 SITL 档（官方镜像 + 起降垫 + GCS 心跳）。

---

## 5. 风险与注意事项

1. **`<loop>false</loop>` actor 卡死**（gz-sim 8.15 已复现）：导出器必须生成 `loop=true` + 末尾驻留路点，或改用进程内驱动。升级到 Jetty 前需复测。
2. **GLB 朝向与尺度**：gz 不做 Y-up 转换；Sketchfab 资产不是米制（r2、r3）。导出器漏掉 roll 或尺度时，世界会竖起来或比例错误，必须有自动配准验收。
3. **actor 不是物理实体**：无碰撞、无重力；任何"相遇、上车、遮挡"都必须由 Event Runtime 在磁带中计算，不能从 Gazebo 推断。
4. **actor 渲染偏差**：SDF 轨迹约 0.3 m，进程内驱动校正后约 0.6 m（含步态抖动）；做像素级身份标注时以 Sensor Service 的实例图为准，不以 gz 的 actor 骨架为准。不同蒙皮的根节点偏移需逐个标定。
5. **gz 传感器吞吐**：单线程渲染流水线约 90 次更新/s，GPU 利用率低；不要指望增加 GPU 来提速，要么多进程，要么用 Sensor Service。
6. **gpu_lidar 只能均匀网格**，不能表达 MID-360 的非重复花样与逐点时刻（r4、r9）；以 gz LiDAR 驱动 FAST-LIVO2 会丢失运动畸变这一关键特征（r4 实测单一位姿在 8 m/s 时 ATE 恶化约 11 倍）。
7. **速度控制器的指令坐标系**：上游 `MulticopterVelocityControl` 按完整机体姿态解释线速度，机动时会掉高（r9 已分叉修正）；本文 E4 中画圈的机体可能存在高度漂移，不影响传感器吞吐结论。
8. **Python 绑定的 GIL 问题**：进程外 `request()` 可能丢应答（r9）；生产链路的命令与门控走进程内 C++ System。
9. **天气**：Gazebo 无物理雨模型，粒子雨对传感器几乎无影响却拖慢渲染；风的默认系数 k = 1 会把机体吹飞（r23），必须写标定值。
10. **PX4 档**：PX4 1.16+ 无 GCS 心跳不允许解锁；摄影测量网格上出生会因倾斜而 "Attitude failure"；以 gz 时钟从动，不能逐位复现。
11. **共享 GPU 与 EGL 设备选择**：Ogre-Next 的 EGL 无头模式固定使用 0 号设备（r9），只能在容器层面绑定 GPU；dmax 上 GPU 2、3 常被其他单元占用，基准测试要记录 GPU 状态并在空闲时复测。
12. **容器网络**：本次容器内没有 DNS，Fuel 模型（actor 蒙皮）与 pip 包需在宿主机下载后挂载；Docker Hub 直连超时，PX4 镜像经 `docker.1ms.run` 拉取（DaoCloud 镜像拒绝 px4io）。
13. **残留进程**：卡死的 gz 服务端不会自行退出，测试脚本必须按进程组 kill，并在容器层面做收尾检查（本次就残留了一个，见 §1.3）。

---

## 6. 复现

```bash
# 本地 → dmax
rsync -a .cache/research/d3/r5-gazebo/{scripts,models,docker} dmax:/data/anet-drone4d-d3/r5-gazebo/
# r6 资产（磁带、网格、DSM）放到 /data/anet-drone4d-d3/r5-gazebo/r6/（文件名见 scripts 中的引用）
# 镜像与容器：见 §2.1；actor 蒙皮：宿主机 curl Fuel 的 walk.dae 到 models/actor_walk/meshes/
docker exec d3-r5-gazebo bash -lc "source /opt/ros/jazzy/setup.bash; cd /work; \
  python3 scripts/mk_collision.py r6/smalltown_mesh_enu.npz town 20000 100000 500000; \
  scripts/e2_sweep.sh; \
  python3 scripts/e3_actor.py s10_p42 /work/r6/tape_abc 10 42 60 0 traj; \
  python3 scripts/e3_actor.py tapesys_cal_p42 /work/r6/tape_abc 10 42 60 0 tapesys; \
  scripts/e4_sweep.sh; \
  GZ_PARTITION=e5a python3 scripts/e5_lockstep.py n3_s004 3 0.004 100 --lidar; \
  python3 scripts/e5b_inserver.py k3 3 20; scripts/e6_ros2.sh; python3 scripts/e7_rain.py r8k 8000 0.65; \
  python3 scripts/e8_mkworld.py"
docker run -d --name d3-r5-px4 --gpus '"device=2,3"' --cpus 16 --memory 64g -e NVIDIA_DRIVER_CAPABILITIES=all \
  -v /data/anet-drone4d-d3/r5-gazebo:/work -v /data/anet-drone4d-d3/data:/glb:ro \
  --entrypoint bash docker.1ms.run/px4io/px4-sitl-gazebo:latest -c "sleep infinity"
# PX4 镜像需补 /usr/share/glvnd/egl_vendor.d/10_nvidia.json（内容见 docker/Dockerfile）
docker exec d3-r5-px4 bash -c "cd /work; for n in 1 3 5 10; do python3 scripts/e8_px4.py \$n; done"
docker rm -f d3-r5-gazebo d3-r5-px4
```

原始结果：`results/e1_*.json|png`、`e1_town_topdepth_align.json`、`e2_*.json`、`e2_collision_*.json`、`e2_dsm_err.json`、`e3_*.json|png`（含逐检测的 `*_dets.json`）、`e3b_*.log`、`e4_*.json|out|png`、`e5_*.json`、`e5b_k3.json`、`e6_ros2.txt`、`e7_*.json|png`、`e8_px4_n{1,3,5,10}.json` 与每实例 PX4 日志；生成的世界在 `worlds/`（含 `e3min_A..G.sdf` 七个用于复现 actor 卡死的最小世界）。
