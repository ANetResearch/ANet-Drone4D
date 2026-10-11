# r9 · gz-sim 源码研究与 Drone4D Realistic 后端落地设计

> 研究单元：r9-gz-sim　｜　日期：2026-10-11　｜　范围：Gazebo Sim（gz-sim）及其配套库的源码级研究、dmax 上的锁步原型实测、作为 D3 Realistic 后端的接口设计
>
> 协调说明：r5（dmax 上的 Gazebo Harmonic 实验）已覆盖 GLB 载入耗时、碰撞网格与物理引擎对比（DART/Bullet/ODE、1–20 架）、事件磁带驱动 actor（Python System）与传感器套件扫描。本笔记不重复上述实验，直接引用其结论；r9 聚焦源码机制、外部权威时钟锁步、确定性、命令通道语义、渲染管线成本拆分、GPU 选择与并发实例，以及面向 D3 的接口落地。

---

## 0. 结论速览

| # | 议题 | 结论 | 依据 |
|---|------|------|------|
| 1 | 版本选型 | 原型采用 Harmonic（gz-sim 8.15，ROS 2 Jazzy vendor 包，LTS 至 2028）；生产目标建议 Jetty（gz-sim 10，LTS），其新增批量射线求交与 CPU LiDAR；main（11.0.0~pre2）引入 EnTT ECM、`Server::EcmScope`、Python ECM 绑定，暂不用于生产 | §1、§2.1、§2.5 |
| 2 | 外部权威时钟锁步 | 可行且已验证。自研 `Drone4DClock` System 在 `PreUpdate` 处门控每一次迭代，Drone4D 控制器按 tick 授予步数；`step_done` 返回该 tick 末的真值位姿 | §3.1、§3.3 |
| 3 | 确定性 | 同一镜像、同型号 GPU 下，300 tick（7500 物理步、1800 帧）两次运行的位姿逐位一致，RGB 与 LiDAR 帧逐字节一致（需 `--seed`）；GPU 2 与 GPU 3 之间同样逐位一致；有无渲染传感器不影响物理轨迹（逐位一致） | §3.3.2 |
| 4 | 命令通道语义 | gz-transport 话题为异步投递，无法与步进严格排序；需改为同步服务或进程内 ECM 写入。上游 `MulticopterVelocityControl` 将线速度指令按完整机体姿态（含横滚、俯仰）解释，机动时会把水平指令转为垂向分量，导致掉高；分叉版本 `Drone4DVelocityControl` 增加 `world`/`heading` 指令坐标系后定高误差由 −87 m 降至 +1.4 m 以内 | §2.8、§3.4 |
| 5 | 多传感器时间一致性 | 同一渲染批次内所有传感器取自同一份 ECM 快照，RGB/Depth/Seg/LiDAR 时间戳与 tick 时刻完全相等（实测差值 0）；渲染为异步流水线，须按时间戳等待帧；无订阅者的传感器会被跳过渲染；渲染初始化异步，需"传感器就绪屏障" | §2.5、§3.4 |
| 6 | 性能（A40，安静 GPU） | 3 机 ×（1280×720 RGB + 900×40 LiDAR，10 Hz）锁步 RTF 0.95，自由运行 1.18；纯物理 RTF 8.3；渲染线程以 CPU 为瓶颈（GPU 利用率 < 15%）；同卡双实例总吞吐 1.58 倍 | §3.3.3 |
| 7 | GPU 选择 | Ogre-Next EGL 无头模式枚举全部 EGL 设备并固定使用 #0，gz 未暴露设备选项；须以容器 / `NVIDIA_VISIBLE_DEVICES` 级别绑定 GPU | §2.4 |
| 8 | GLB 支持 | gz-common 经 Assimp 读取 GLB，支持内嵌纹理、PBR 金属度/粗糙度、alpha mask、蒙皮动画；不支持 Draco/meshopt/KTX2，须在 World Package 导出时规范化；小镇 GLB（200 万三角面）首帧约 13–16 s，单实例显存约 2.0 GB | §2.3、§3.3 |
| 9 | Actor 与碰撞 | actor 的 link 被物理系统忽略：无碰撞、无重力，但对相机、深度、分割与 gpu_lidar 可见（已实测 LiDAR 命中与实例分割）；需要物理交互的车辆应使用带碰撞体的运动学模型 | §2.6、§3.3.4 |
| 10 | MID-360 非重复扫描 | gpu_lidar 为均匀网格（立方体贴图 + 二次采样纹理），不支持自定义花样；推荐分叉 `Ogre2GpuRays::CreateSampleTexture` 接受射线表（改动约百行），过渡期以密集网格重采样；Jetty 的 `RaycastData` 批量射线可走物理几何但看不到 actor | §2.5 |
| 11 | 风与天气 | WindEffects 为全场均匀风（可按区域分段缩放）+ 一阶滤波 + 正弦 + 噪声，力 = 质量 × 系数 ×（风速 − 连杆速度）；Drone4DClock 直接写入风种子分量，确定性可控；降雨无物理模型，仅 ParticleEmitter 视觉与散射 | §2.7 |
| 12 | Python 接入 | `gz.sim8` 可在进程内托管 Server 并以 `run(blocking, N)` 步进（已实测）；`gz.transport13` 的 Python `request()` 阻塞期间不释放 GIL，与订阅回调并发时会丢失应答（已复现，main 分支同样存在），生产链路应使用 C++ 或独立进程 | §2.11、§3.4 |
| 13 | PX4 集成 | PX4 `gz_bridge` 以 `/world/<w>/clock` 驱动自身时钟（时间从动），Gazebo 不等待 PX4 输出，因此 SITL 档无法逐位确定，仅用于飞控验证档 | §2.8 |
| 14 | 总体建议 | 采纳 gz-sim 作为 D3 Realistic 后端：Drone4D 持有时钟与事件磁带，gz-sim 负责刚体动力学、旋翼模型与基于 GLB 网格的传感器渲染；通过 `GzSimBackend`（实现既有 `SimBackend`/`DroneAdapter`/`SensorBackend` 协议）与 FleetSim Fast 档对齐 | §4 |

---

## 1. 研究范围、版本与方法

### 1.1 源码版本

| 仓库 | 位置 | 提交 | 版本 / 说明 |
|------|------|------|-------------|
| gz-sim | `refs/sim/gz-sim`（已更新至上游最新） | `e08ab4e`（2026-10-09） | 11.0.0~pre2；另取 `origin/gz-sim8`（`a9c03d6`）与 `origin/gz-sim10`（`9c5211b`）用于对照 |
| gz-sensors | `refs/d3/gz-sensors` | `ab16088`（2026-10-06） | 11.0.0；对照分支 `gz-sensors8` |
| gz-rendering | `refs/d3/gz-rendering` | `a83a085`（2026-10-08） | 11.0.0；对照分支 `gz-rendering8` |
| gz-physics | `refs/d3/gz-physics` | `4e2af8c`（2026-10-08） | 10.0.0；对照分支 `gz-physics7`；main 已含 MuJoCo 插件骨架 |
| gz-transport | `refs/d3/gz-transport` | `41913b9`（2026-10-07） | 16.0.0；15.x 起提供 Zenoh 实现；对照分支 `gz-transport13` |
| sdformat | `refs/d3/sdformat` | `f75541b`（2026-10-06） | 17.0.0~pre2（Harmonic 对应 sdformat14） |
| gz-common | `refs/d3/gz-common` | `09f7b41`（2026-10-06） | 8.0.0；对照分支 `gz-common5`（Harmonic） |
| gz-msgs / gz-math | `refs/d3/` | `89c0c3d` / `1316ed3` | 13.0.0 / 10.0.0 |
| ros_gz | `refs/d3/ros_gz` | `b285fd6`（2026-10-09） | 4.0.1（ros2 分支）；对照 `jazzy` 分支 |
| PX4-Autopilot | `refs/sim/PX4-Autopilot` | `b3e343c`（2026-09-27） | 仅阅读 `src/modules/simulation/gz_bridge` |

全部为浅克隆、只读研究，位于 gitignore 范围内。

### 1.2 运行时

- 实验机 dmax（主机名 gpu2，实测 8 块 A40，本单元仅使用 GPU 2、3），容器 `d3-r9-gz-sim`，CPU 上限 16 核，工作目录 `/data/anet-drone4d-d3/r9-gz-sim/`，GLB 复用 `/data/anet-drone4d-d3/data/`。
- 镜像：复用 r5 的 `d3-r5-gazebo:harmonic`（Ubuntu 24.04 + ROS 2 Jazzy vendor 包：gz-sim 8.15.0、gz-rendering 8.2.3、Ogre-Next 2.3.3、sdformat14），重新打标签为 `d3-r9-gz-sim:harmonic`，在其上叠加 C++ 工具链得到 `d3-r9-gz-sim:dev`。
- 收尾：实验容器 `d3-r9-gz-sim` 已删除；镜像标签 `d3-r9-gz-sim:harmonic`（与 r5 共享层）与 `d3-r9-gz-sim:dev`（增量约 160 MB）保留以便复现。
- 共享 GPU 说明：实验期间 GPU 2、3 均有其他研究单元的计算负载，部分时段利用率达 86–100%。下文以"安静"（其他租户利用率接近 0）与"争用"两类条件分别报告，争用条件下的数值仅作参考。

### 1.3 方法

1. 源码阅读：以 main 为主，对 Harmonic 实际运行的分支逐项核对差异，关键结论均标注文件与行号。
2. 原型：编写两个 C++ System 插件与一个外部时钟客户端，在 dmax 容器中加载小镇 GLB 世界、放置 3 架 x500 多旋翼与 1 个沿道路行走的 actor，读取 gpu_lidar 与相机帧并以外部时钟单步推进。
3. 对照：传感器组合拆分、规模扩展、自由运行、进程内 Python 托管、双实例并发、跨 GPU 确定性。

---

## 2. 源码结构与关键实现

### 2.1 ECS 架构与 System 生命周期

**实体与组件。** 实体为整数 ID，组件为类型化数据（`components::Pose`、`Actor`、`WorldPoseCmd` 等），由 `EntityComponentManager`（ECM）集中存储，系统通过 `Each<...>`、`EachNew`、`EachRemoved` 视图访问；组件的变更状态（`OneTimeChange`/`PeriodicChange`）驱动场景广播与渲染同步。main 分支已将 ECM 改为基于 EnTT 的实现（#3447），并新增 `Server::EcmScope()` 返回持有执行互斥锁的 `EcmGuard`，供宿主进程在步与步之间安全读写 ECM（#3936、#3964）。

**System 接口**（`include/gz/sim/System.hh`）：`ISystemConfigure`、`ISystemConfigurePriority`、`ISystemConfigureParameters`、`ISystemReset`、`ISystemPreUpdate`、`ISystemUpdate`、`ISystemPostUpdate`。执行顺序由优先级控制（`gz:system_priority`，L96；预置常量 `kUserCommandsPriority=-16384`、`kPrePhysicsPriority=-128`、`kPhysicsPriority=-64`、`kPostPhysicsSensorPriority=-32`），数值越小越先执行。

**单步流程**（`src/SimulationRunner.cc`）：

```
Run() 循环（L810）
  UpdatePhysicsParams → UpdateCurrentInfo（simTime += dt）
  Step()（L1098）
    ProcessNewWorldControlState → PublishStats（/clock 每步发布；/stats 限流 10 Hz，L862）
    → LevelManager → ProcessSystemQueue → ProcessPendingEntitySystems
    → UpdateSystems()（L712）
         PreUpdate：按优先级串行（可写 ECM）
         Update：按优先级串行（Physics 在此积分）
         PostUpdate：只读 ECM；Harmonic 默认在线程池中并行（屏障同步），main 默认改为串行，
                    可由 <gz:policies><parallel_postupdates> 配置
  RTF 节拍：desiredRtf < 1e-9 时 updatePeriod = 0，即"尽可能快"（L208）
```

无论并行与否，同一迭代内全部 PostUpdate 都在下一次 PreUpdate 之前完成（并行时彼此无序）——Drone4DClock 正是利用这一点：在下一次 PreUpdate 入口处判定"上一批已完成"。

**插件形态。** C++ 插件以 `GZ_ADD_PLUGIN` 注册，通过 `GZ_SIM_SYSTEM_PLUGIN_PATH` 查找；Python System 由 `gz-sim-python-system-loader-system` 加载，模块需提供 `get_system()`，对象实现 `configure/pre_update/update/post_update`。r5 已用 Python System 驱动事件磁带；r9 选用 C++ 以获得无 GIL 的门控与指令通道。

### 2.2 服务器与 GUI 分离、无头运行

- 服务器（`gz sim -s`）与 GUI（`gz sim -g`）为独立进程，GUI 通过 SceneBroadcaster 发布的 `/world/<w>/scene/info`、`/state`、`/pose/info` 与服务器同步，因此批量实验可完全不启动 GUI；SceneBroadcaster 本身也可在纯批处理世界中省略以节省 CPU。
- `--headless-rendering` 经 `RenderEngineServerHeadless` 组件传入 Sensors 系统，RenderUtil 以 `params["headless"]="1"` 初始化 ogre2。即使未传该参数，`Ogre2RenderEngine::CreateContext` 在 `XOpenDisplay` 失败时也会自动切换无头模式（gz-rendering8 `Ogre2RenderEngine.cc` L549）——这正是进程内 Python 托管（`gz.sim8` 的 `ServerConfig` 在 Harmonic 中未绑定 `SetHeadlessRendering`）仍能渲染的原因。

### 2.3 World/SDF 加载与 GLB/GLTF

- **SDF 解析**：sdformat14 负责 `<include>`、`merge`、frame 语义图校验；`SdfEntityCreator` 把 `sdf::World/Model/Link/Actor` 转为实体与组件。模型 URI 通过 `GZ_SIM_RESOURCE_PATH` 与 Fuel 解析。
- **一个 Server 多个 World**：`ServerPrivate` 为 SDF 中每个 `<world>` 建立一个 `SimulationRunner`，多 runner 时在线程池中并行；但渲染引擎为进程级单例，多世界共用渲染存在资源竞争，D3 不采用此方式，统一按"一世界一进程"部署。
- **网格加载**（gz-common5 `MeshManager.cc`）：`.gltf/.glb/.fbx` 一律交给 `AssimpLoader`（L184），`GZ_MESH_FORCE_ASSIMP` 可令 DAE/OBJ/STL 也走 Assimp。Assimp 导入标志含 `JoinIdenticalVertices | Triangulate | GenNormals | FlipUVs | PopulateArmatureData` 等（`AssimpLoader.cc` L873）。
- **材质与纹理**：支持 GLB 内嵌纹理（以 `*` 前缀标识，从内存解码）、glTF 金属度/粗糙度合并纹理拆通道、lightmap/AO、`alphaMode=MASK`、透射系数；变更记录中修复了 GLB 内 JPEG 压缩纹理与根节点变换问题。未见 Draco、meshopt、KTX2/BasisU 支持，World Package 导出时须解压缩几何并转为 PNG/JPEG 纹理。
- **碰撞几何**：dartsim 以 `CustomMeshShape`（继承 `dart::dynamics::MeshShape`）承载网格，碰撞检测器可选 bullet/ode/fcl/dart；bullet-featherstone 支持 SDF 1.11 的 `<optimization>convex_decomposition|convex_hull</optimization>`（gz-physics `bullet-featherstone/src/SDFFeatures.cc` L1135）。摄影测量网格不宜直接作为碰撞体；r5 实测以 10 万面减面网格作碰撞体时，3 机纯物理 RTF 3.45、10 机 2.21（DART + bullet 检测器）。
- **网格射线**：Harmonic 已有 `components::RaycastData`，Physics 系统逐射线查询；Jetty 起改为批量射线接口并据此提供 `CpuLidar` 系统（`src/systems/cpu_lidar`）。

### 2.4 渲染后端、EGL 无头与 GPU 选择

- 后端：ogre2（Ogre-Next 2.3，PBS 材质，默认）、ogre1（遗留）、optix（停滞）；Sensors 系统可选体素锥追踪全局光照（`<global_illumination type="vct">`）。全仓未发现 Gaussian Splatting 支持，3DGS 必须由外部渲染器承担。
- 无头：Ogre-Next 的 `EglPBufferSupport` 通过 `eglQueryDevicesEXT` 枚举全部 EGL 设备，逐一试初始化，配置项 `Device` 默认取第一个（`OgreEglPBufferSupport.cpp` L163）。gz-rendering 从不设置该项。实测 `ogre2.log`：

  ```
  Found Num EGL Devices: 3
  EGL Device: EGL_NV_device_cuda ... #0
  EGL Device: EGL_NV_device_cuda ... #1
  EGL Device: EGL_MESA_device_software ... #2
  GL_RENDERER = NVIDIA A40/PCIe/SSE2
  ```

  容器可见两块 GPU 时，渲染始终落在 #0，另一块仅产生约 4 MiB 的探测上下文；`CUDA_VISIBLE_DEVICES` 不影响 EGL。**结论：GPU 绑定必须在容器或 `NVIDIA_VISIBLE_DEVICES` 层面完成，一个 gz 进程对应一块指定 GPU。**

### 2.5 传感器系统

**渲染流水线**（gz-sim `src/systems/sensors/Sensors.cc`）：

1. 首个 PostUpdate 检测到相机类组件后，置 `doInit` 唤醒渲染线程完成初始化——**初始化不阻塞仿真线程**，在此期间仿真照常推进但不产出帧。
2. 之后每个 PostUpdate：等待上一次渲染完成（L1016 `renderCv.wait(!updateAvailable)`），把当前 ECM 快照写入 RenderUtil，再唤醒渲染线程；因此渲染与下一批物理步重叠，形成"滞后一拍"的流水线。
3. `RunOnce`：一次 `scene->PreRender()` 后对本批所有到期传感器执行 `sensorManager.RunOnce(updateTimeApplied)`——**同一批次的所有传感器共享同一份场景状态与同一时间戳**；`SetCameraPassCountPerGpuFlush(6)`（L362）。
4. **无订阅者的渲染传感器会被临时停用**（L480 `!rs->HasConnections()`）：Sensor Service 必须先订阅再期待产出。

**传感器类型与要点**（gz-sensors）：

| 类型 | 输出话题 | 要点 |
|------|----------|------|
| camera | `<topic>`、`camera_info` | 高斯/量化噪声、Brown 畸变、`<triggered>` + `trigger_topic` 按需触发（Harmonic 已支持） |
| depth_camera | `<topic>`（R_FLOAT32）、`/points` | 与 RGB 同管线 |
| rgbd_camera | `/image`、`/depth_image`、`/points` | 单传感器多输出 |
| segmentation | `/colored_map`、`/labels_map` | semantic/instance，标签来自 Label 系统（支持 actor） |
| boundingbox_camera | `<topic>`（2D/3D 框）、`/image` | 可直接生成检测标注 |
| thermal_camera | L8/L16 | 需 Thermal 系统为实体赋温度或热纹理 |
| wide_angle_camera | — | 鱼眼/全景 |
| gpu_lidar | LaserScan、`/points`（xyz+intensity+ring） | 见下 |
| cpu_lidar（Jetty 起） | 同上 | 物理几何射线，无 gz-rendering 依赖 |
| imu / navsat / magnetometer / altimeter / air_pressure / air_speed / logical_camera / dvl / force_torque | — | logical_camera 可作"预言机"检测，但 Benchmark Agent 不得使用 |

**gpu_lidar 实现与 MID-360 可行性**（gz-rendering `ogre2/src/Ogre2GpuRays.cc`）：

- 第一遍渲染覆盖视场所需的立方体贴图面（每面 90°），面分辨率按采样数取 2 的幂并钳制在 [128, 1024]（L782）；第二遍按 `cubeUVTexture`（每条射线一个像素，存 u、v、面索引）从立方体贴图采样距离。
- `CreateSampleTexture()`（L826）按 `min/max angle` 与 `samples` 生成**均匀网格**方向，无法表达 Livox 非重复扫描；整帧在同一瞬间渲染，无逐点时间戳与运动畸变。
- 噪声为距离高斯噪声，由 `gz::math::Rand` 驱动，可由 `gz sim --seed` 固定。
- MID-360 的 FOV（360° × −7°~52°）需渲染 5 个立方体面。实测 900×40 时第一遍面分辨率为 256²，1800×128 时为 512²。

可选路线：

| 路线 | 做法 | 优点 | 局限 |
|------|------|------|------|
| A 密集网格重采样 | gpu_lidar 输出密集距离图，Sensor Service 按 MID-360 花样表最近邻/双线性取样，并按时间片做运动畸变 | 无需改上游 | 成本高：3 机 1800×128 锁步 RTF 0.61（安静）/ 0.19（争用） |
| B 分叉 GpuRays | 以射线方向表替换 `CreateSampleTexture` 的均匀网格（每帧约 2 万射线，可按时间片分批） | 成本接近原生，花样精确 | 需维护 gz-rendering 分叉（改动集中、约百行） |
| C 物理射线 | Jetty `RaycastData` 批量射线、任意射线集 | 任意花样、与碰撞几何一致 | 仅碰撞几何（通常为减面网格）、看不到 actor、CPU 成本 |

建议：原型期用 A，生产期在 Realistic 档采用 B；r4 的网格射线后端（BVH）承担 Fast 档与离线高精度。

### 2.6 Actor 与轨迹动画

- SDF `<actor>` 包含 `<skin>`、`<animation>`、`<script>`；脚本轨迹由 RenderUtil 按 `simTime` 插值。
- **手动驱动**：当实体同时具有 `AnimationName` 与 `AnimationTime` 组件时，RenderUtil 改用这两个组件计算骨骼，并以 `TrajectoryPose` 覆盖轨迹位姿（gz-sim8 `RenderUtil.cc` 中 actor 分支）。`FollowActor` 系统与 Python `Actor.set_trajectory_pose/set_animation_time` 均走此路径。Drone4DClock 以相同方式把事件磁带写入 actor（§3.1）。
- **与物理的关系**：Physics 系统显式跳过 actor 下的 link（main `Physics.cc` L3684 "ignore links from actors for now"），故 actor 无碰撞、无重力、不会被物理射线（cpu_lidar、RaycastData）击中；但会被 RGB、深度、实例分割与 gpu_lidar 观测到（§3.3.4 实测）。需要阻挡无人机或被推动的车辆，应建模为带碰撞体的运动学模型，以 `WorldPoseCmd` 逐步设定位姿。
- main 分支已允许 glTF/GLB 作为 actor 动画来源（#3911），便于统一资产格式。

### 2.7 风与大气

- **WindEffects**（`src/systems/wind_effects/WindEffects.cc`）：风实体携带 `WorldLinearVelocitySeed`（目标风）与 `WorldLinearVelocity`（当前风）。每步对水平幅值、方向、垂向分别做一阶滤波（`time_for_rise`）、叠加正弦与高斯噪声；对所有 `enable_wind` 的连杆施加 `F = m · k(pos) · (v_wind − v_link)`（L586–L624），`k(pos)` 可按区域分段多项式缩放（如按高度分层）。外部可经 `/world/<w>/wind` 话题异步修改。
- **大气**：`<atmosphere type="adiabatic">` 提供温度/气压递减率，供气压计、高度计使用。
- **降雨/雾**：无物理模型；ParticleEmitter 系统提供粒子视觉，gz-rendering 对深度相机与 gpu_lidar 有粒子散射噪声监听（`Ogre2ParticleNoiseListener`），可用于"雨雾导致的回波噪声"近似。
- Drone4D 用法：事件磁带中的风时间线由 Drone4DClock 直接写入种子分量（同步、确定），不经异步话题。

### 2.8 多旋翼插件与 PX4 集成

- **MulticopterMotorModel**：推力 `T = k_f · ω²`（沿转子轴），反扭矩 `−dir · T · k_m`，转子阻力 `−|ω| · c_d · v⊥`，滚转力矩，一阶电机时间常数，`maxRotVelocity` 截断，`rotorVelocitySlowdownSim` 仅用于关节可视化转速。指令来源有二：`command/motor_speed` 话题（PX4 使用），或模型实体上的 `components::Actuators`（同进程系统写入，同步）。
- **MulticopterVelocityControl**：Lee 速度控制器（`LeeVelocityController.cc`），输出写入 `Actuators` 组件（同步）。两点需要注意：
  1. 指令入口是 Twist 话题订阅（异步），锁步下无法保证在下一步之前生效；gz-sim 最新提交"Wait for the thruster command before stepping in the thruster test"（#4011）也印证了这一类竞态。
  2. `velocityError = v_world − R · v_cmd`（L118）：指令线速度被视为**完整机体系**（含横滚、俯仰）。机体以 15° 俯仰前飞 5 m/s 时，`R · v_cmd` 自带约 1.3 m/s 的垂向分量，控制器据此"追踪"出持续掉高。实测 3 机在 30 s 内分别掉高 23 m 与 87 m（§3.4）。
- **PX4**（`src/modules/simulation/gz_bridge/GZBridge.cpp`）：订阅 `/world/<w>/clock` 并在每条消息上 `px4_clock_settime`（L342），订阅 IMU、磁力计、NavSat、气压、里程计、激光等传感器话题，经 `/<model>/command/motor_speed` 输出电机转速；`server.config` 预置 Physics、Sensors、WindEffects、NavSat 等系统。PX4 时间从动于 Gazebo，但 Gazebo 不等待 PX4 计算完成，故 SITL 档不具备逐位确定性。

### 2.9 gz-transport

- ZeroMQ 发布/订阅与请求/应答，UDP 多播发现；`GZ_PARTITION` 隔离命名空间，`GZ_IP` 绑定网卡（容器内用 127.0.0.1 可避免跨容器串扰）。15.x 起可经 `GZ_TRANSPORT_IMPLEMENTATION=zenoh` 切换 Zenoh 实现（`src/NodeSharedPrivate.hh` L87）。
- **同进程发布为异步**：本地订阅者的消息先入发布队列，由独立线程分发（gz-transport13 `Node.cc` L491）；跨进程消息由接收线程直接调用回调。
- **服务回调**在接收线程中、NodeShared 互斥锁之外执行（`NodeShared.cc` L743），因此服务处理函数不应长时间阻塞，否则会阻塞该进程全部入站消息。Drone4DClock 的 `step` 服务据此设计为"立即应答 + 话题回报完成"。
- **Python 绑定的 GIL 问题**：`request_raw` 阻塞等待应答时未释放 GIL（gz-transport13 与 main 均如此），而订阅回调在接收线程中需获取 GIL（`_gz_transport_pybind11.cc` L285）。若阻塞请求期间恰有订阅消息到达，接收线程卡在 GIL 上，应答无法处理，请求只能超时。§3.4 已复现。

### 2.10 ros_gz 桥

- `ros_gz_bridge`：YAML 配置双向映射（Image↔`sensor_msgs/Image`、PointCloudPacked↔PointCloud2、Pose_V↔TFMessage、Clock、Actuators、AnnotatedAxisAligned2DBox_V↔`vision_msgs/Detection2DArray` 等），支持 `lazy` 与 QoS 配置。
- `ros_gz_sim`：`create`/`remove`/`set_entity_pose`，以及 ROS 2 `simulation_interfaces` 标准实现（StepSimulation、SimulateSteps、SetEntityState、SpawnEntity、ResetSimulation、GetEntitiesStates 等）。其中 SimulateSteps 通过 `WorldControl{pause, step, multi_step=N}` 下发，再轮询 `/stats` 的 iterations 判定完成（`simulate_steps.cpp` L108），源码注释指出 `stepping` 标志存在缺陷。
- 定位：D3 核心链路不依赖 ROS；ros_gz 仅作为面向 ROS 生态用户的可选出口。

### 2.11 Python 绑定

| 模块 | Harmonic 能力 | main 增量 |
|------|---------------|-----------|
| `gz.sim8` | `Server.run(blocking, iterations, paused)`（释放 GIL）、`TestFixture.on_pre_update/on_update/on_post_update`、`World/Model/Link/Actor/Joint/Light/Sensor` 便捷类（如 `Model.set_world_pose_cmd`、`Actor.set_trajectory_pose`、`Link.add_world_wrench`）；`ServerConfig` 仅 `set_sdf_file` | 通用 ECM 操作（`create_entity`、`set_component_data` 等，#3957）、`ServerConfig.set_sdf_string` |
| Python System 加载器 | `module_name` + `get_system()` | — |
| `gz.transport13` | `Node.advertise/subscribe/request` | 同（GIL 问题未修复） |
| `gz.msgs10`、`gz.math7`、`sdformat14` | protobuf 消息与数学类型 | — |

注意：使用 `Link.world_pose` 等返回 `gz.math` 类型的接口前须先 `import gz.math7`，否则 pybind11 类型转换失败并在仿真线程中触发 `std::terminate`（§3.4）。

### 2.12 确定性与锁步

| 机制 | 说明 |
|------|------|
| 步长 | `<max_step_size>` 固定步长，本原型为 4 ms |
| 实时因子 | `<real_time_factor>0` 表示不节拍、尽可能快；锁步时由外部时钟决定推进 |
| 暂停与单步 | `/world/<w>/control`：`pause`、`step`、`multi_step`、`run_to_sim_time`、`reset`；服务立即返回，实际推进异步完成；`seed` 字段不支持运行时修改 |
| 时钟 | `/clock` 每步发布（在系统更新之前），`/stats` 限流 10 Hz |
| 随机数 | `gz sim --seed N` → `math::Rand::Seed`，覆盖传感器噪声与风噪声 |
| 非确定来源 | 异步话题指令；渲染线程与仿真线程并行导致的帧到达时刻（时间戳本身确定）；GPU 型号/驱动差异；多线程物理（本原型均为单线程） |

### 2.13 多世界与并发实例

- 同一 Server 多 World 可行但共享渲染单例，不推荐。
- 分布式仿真（`--network-role` primary/secondary + levels）仍在代码中，但与渲染传感器配合有限，不作为 D3 扩展手段。
- 推荐：**一事件实例 = 一个 gz 进程（独立 `GZ_PARTITION`）**，多进程共享或分占 GPU；§3.3.3 实测同卡双实例总吞吐提升至 1.58 倍。

---

## 3. 原型：Drone4D Lockstep Bridge

### 3.1 组成

| 组件 | 文件 | 职责 |
|------|------|------|
| `Drone4DClock`（世界级 System） | `.cache/research/d3/r9-gz-sim/plugin/src/Drone4DClock.cc` | ① 锁步门控：`PreUpdate` 入口在步数预算为 0 时阻塞，先发布上一批完成消息；② 事件磁带回放：按仿真时间纯函数插值，写入 actor 的 `TrajectoryPose`/`AnimationTime`、运动学模型的 `WorldPoseCmd`、风实体的 `WorldLinearVelocitySeed`；③ `PostUpdate` 采集被跟踪实体的真值位姿 |
| `Drone4DVelocityControl`（模型级 System） | `plugin/src/Drone4DVelocityControl.cc`（由上游 `MulticopterVelocityControl` 分叉，保留 Apache-2.0 头） | 指令改为同步服务（应答前已写入缓冲）；新增 `<commandFrame>world|heading|body</commandFrame>`，在每个物理步把世界系指令换算为当前机体系，使 `R · v_cmd` 恰为期望速度；控制器与 `Actuators` 输出沿用上游 |
| 世界生成器 | `scripts/gen_world.py` | 小镇 GLB（沿用 r5 对齐位姿）、N 架 x500（4 × MulticopterMotorModel + Drone4DVelocityControl + 载荷连杆上的 RGB/Depth/Seg/MID-360 类 gpu_lidar）、actor、WindEffects、Label |
| 事件磁带 | `scripts/make_tape.py` → `worlds/tape_town.csv` | `kind,name,t,x,y,z,yaw`；person_a 沿道路 9 个路点以 1.3 m/s 行走 57 s，风种子 2 → 6.7 → 3 m/s |
| 外部时钟客户端 | `scripts/d4d_lockstep.py` | 权威时钟：每 tick 下发三机指令 → 授予 25 步 → 等待 `step_done` → 等待 (t_{k−1}, t_k] 内全部传感器帧；记录耗时、时间戳、位姿、帧摘要与资源 |
| 进程内对照 | `scripts/py_inproc.py` | `gz.sim8.TestFixture` 托管 Server，`run(True, 25, False)` 步进 |

**协议**（世界名 W）：

```
客户端（Drone4D 权威时钟）                        gz-sim 进程
  ── /<uav>/d4d/cmd_vel  (Twist → Boolean) ──▶  Drone4DVelocityControl 写入指令缓冲后应答
  ── /d4d/W/step         (UInt32 n → Boolean) ─▶ Drone4DClock 预算 += n，立即应答
                                                 迭代 1..n：PreUpdate(磁带) → Update(物理) → PostUpdate(采样/渲染交接)
  ◀── /d4d/W/step_done   (Pose_V, stamp = t_k) ── 第 n+1 次 PreUpdate 入口：发布后阻塞
  ◀── /d4d/<uav>/rgb、/depth、/seg/*、/lidar/points（stamp = t_k）── 渲染线程异步发布
```

指令先于授权到达且已入缓冲，`step_done` 只在全部 PostUpdate 完成后发布，二者共同保证同一输入序列产生同一轨迹。

### 3.2 实验配置

- 世界：小镇 GLB（`an_entire_small_town_scanned_by_a_drone.glb`，230 MB，200 万三角面，仅视觉，无碰撞网格；碰撞成本见 r5）。
- 机队：3 架 x500，初始高度 45 m；uav0 跟随 actor（偏移可配），uav1 往复航线 5 m/s，uav2 绕广场 35 m 半径盘旋 6 m/s；偏航角速度上限 0.3 rad/s。
- 传感器（每机）：E1 基准为 RGB 1280×720（hfov 1.5 rad，下俯 0.6 rad）+ MID-360 类 gpu_lidar 900×40（−7°~52°，倒装，0.1–70 m，σ=0.02 m），均 10 Hz；全套另加 640×360 深度与实例分割。
- 时序：步长 4 ms，tick 0.1 s（每 tick 25 步），外部时钟客户端为 Python。

### 3.3 实测结果

#### 3.3.1 启动

| 指标 | 数值 |
|------|------|
| 进程启动至锁步服务可用 | 0.7–0.9 s |
| 进程启动至全部传感器首帧 | 13–16 s（GLB 载入与渲染初始化；r5 实测小镇首帧 13.4 s） |
| 单实例显存（进程级，`nvidia-smi -q -d PIDS`） | 1.9–2.1 GB，基本不随传感器数量变化（由 GLB 纹理主导） |
| 单实例常驻内存 | 2.5–2.9 GB；无渲染传感器时 0.2 GB（视觉网格不被加载） |

#### 3.3.2 确定性与时间一致性

| 检查 | 结果 |
|------|------|
| 同配置两次运行（GPU 2，`--seed 42`，300 tick） | 3 机 + actor 位姿逐位一致；RGB 900/900、LiDAR 900/900 帧摘要一致 |
| 同配置未设种子 | RGB 900/900 一致；LiDAR 仅 14/900 一致（均为无回波帧，噪声随机数未固定） |
| GPU 2 与 GPU 3 之间 | 位姿逐位一致；抽查 tick 0/150/299 全部 6 路帧逐字节一致（同型号 A40、同驱动、同镜像） |
| 有无 6 路渲染传感器 | 物理轨迹逐位一致（渲染不扰动物理） |
| 事件磁带回放精度 | actor 真值与磁带插值最大偏差 0.0 m（每 tick 校验） |
| 帧时间戳与 tick 对齐 | 所有运行中帧时间戳减 tick 时刻恒为 0；300 tick 无一缺帧 |

#### 3.3.3 性能（锁步，3 机，10 Hz；安静条件为 GPU 3 其他租户利用率约 0%）

| 配置（每机） | 锁步 RTF | tick 耗时 | 步进 p50 | 等帧 p50 | 服务器 CPU 核 |
|--------------|----------|-----------|----------|----------|---------------|
| 无传感器（纯物理 + 磁带 + 风） | **8.26** | 12.1 ms | 9.6 ms | — | 1.0 |
| RGB 640×480 | 2.41 | 41.4 ms | 10.8 ms | 27.9 ms | 1.9 |
| RGB 1280×720 | 1.91 | 52.4 ms | 10.6 ms | 34.4 ms | 2.5 |
| LiDAR 900×40 | 1.23 | 81.3 ms | 20.7 ms | 61.1 ms | 1.9 |
| **E1：RGB 720p + LiDAR 900×40** | **0.95** | 105.7 ms | 16.3 ms | 78.8 ms | 2.3 |
| LiDAR 1800×128（重采样源） | 0.61 | 163.1 ms | 15.6 ms | 143.7 ms | 1.4 |
| 6 机 ×（RGB 480p + LiDAR 900×40） | 0.45 | 222.2 ms | 28.5 ms | 183.0 ms | 2.4 |
| 全套（RGB 720p + 深度 + 分割 + LiDAR），争用条件 | 0.42–0.61 | 163–237 ms | 14–22 ms | 126–164 ms | 2.2–2.6 |

| 对照 | 结果 |
|------|------|
| E1 自由运行（无门控、RTF=0） | RTF 1.18，294 迭代/s，每路传感器墙钟 11.8 Hz，CPU 2.7 核；锁步相对开销约 20% |
| E1 进程内 Python 托管（争用条件） | RTF 0.81；指令调用 0.26 ms（同进程服务直调），每批 25 步 25 ms，等帧 98 ms |
| 同卡双实例并发（E1 × 2） | 0.85 + 0.74，总吞吐 1.58 倍于单实例 |
| 争用条件下的波动 | 同一配置在其他租户占满 GPU 时显著下降：E1 0.65–0.72，LiDAR 1800×128 由 0.61 降至 0.19，6 机由 0.45 降至 0.16 |

解读：

1. 渲染线程是主瓶颈，且以 CPU 为主：安静条件下 GPU 利用率仅 8–15%，单个 720p 相机约 11 ms/帧，单个 900×40 LiDAR 约 20 ms/帧（5 个立方体面各渲染一次全场景再加二次采样），1800×128 约 48 ms/帧。
2. 每个 tick 的 RPC 与同步开销约 2–3 ms，相对渲染可忽略；进程内托管与外部锁步在同一量级。
3. 吞吐靠多进程横向扩展：一块 A40 显存可容纳 10 个以上小镇实例，实际受 CPU 核与 GPU 时间片约束；建议按每实例 3 核、2 GB 显存规划，再以实测校准。

#### 3.3.4 观测内容核验

- RGB：基于 GLB 网格与原始摄影测量纹理渲染，航拍视角质感接近原始扫描（`results/fig_s_full_uav0_t99.jpg`）。
- actor 在 RGB、实例分割（独立标签，75 像素 @ 640×360、距离约 25 m）与深度中均可见（`results/fig_g3_show_uav0_t199.jpg`）。
- LiDAR：uav0 单帧约 2.5 万点（倒装 MID-360 视场）；actor 周围 0.8 m 内命中 13–20 点，高度覆盖 10.3–12.4 m，与 actor 髋部高度 11.4–11.8 m、地面 10.6–11.2 m 吻合（`results/g3_show_actor_lidar.json`）。

### 3.4 原型中发现的问题与处理

| 问题 | 现象 | 根因 | 处理 / 生产建议 |
|------|------|------|-----------------|
| 机动时持续掉高 | 30 s 内 uav1 掉高 23 m，uav2 穿入地形（−42 m）；调大增益无效 | 上游 Lee 控制器把线速度指令视为完整机体系 | `Drone4DVelocityControl` 增加 `commandFrame=world`，定高误差降至 +0.5 / +1.4 m；指令语义统一为 ENU 世界系 + 偏航角速度 |
| 话题指令与步进竞态 | 理论上下一步可能看不到新指令 | gz-transport 投递异步 | 指令走同步服务；生产版合并为"单条控制消息 = 指令集 + 步数授权" |
| 首段无帧 | 前约 13 s 推进但无帧 | 渲染初始化异步 | 客户端增加"传感器就绪屏障"后才开始回合；回合时间零点取屏障之后 |
| 部分传感器不出帧 | 未订阅的话题无数据 | Sensors 系统跳过无订阅者的传感器 | Sensor Service 启动即订阅全部声明的传感器 |
| 请求偶发超时 | `cmd_vel`/`step` 请求 2–5 s 超时，订阅流越多越频繁 | Python `request()` 持有 GIL，订阅回调阻塞接收线程 | 原型以幂等重试规避；生产 Sensor Service 与控制端使用 C++ 或拆分为独立进程，并可向上游提交释放 GIL 的修复 |
| 残留服务器串扰 | 一次失败运行后，下一次客户端连到旧服务器 | 同分区同世界名 | 每次运行独立 `GZ_PARTITION`；客户端以 `finally` 回收进程组 |
| 进程内回调崩溃 | `Link.world_pose` 返回值转换失败导致 `std::terminate` | 未导入 `gz.math7` | 先导入类型模块，回调内全部异常就地捕获 |
| LiDAR 帧不可复现 | 未设种子时 LiDAR 摘要全部不同 | 噪声随机数未固定 | 运行清单记录并固定 `--seed` |

---

## 4. 在 D3 中的采用方式与接口设计

### 4.1 定位

| 档位 | 后端 | 权威划分 | 用途 |
|------|------|----------|------|
| Fast | FleetSim + 网格射线（r4）+ 快速相机（r3、r10） | Drone4D 全权 | 大规模策略搜索、批量数据集 |
| **Realistic** | **gz-sim + Drone4D Bridge** | 时钟、事件：Drone4D；刚体与旋翼动力学、碰撞、GLB 网格传感器：gz-sim | 少量无人机的高可信实验，Fast 档动力学与传感器差异标定 |
| Photorealistic | Realistic 或 Fast + 3DGS 相机（r3） | 静态背景由 3DGS 渲染，动态前景由 gz 渲染并按深度合成 | VLM、视觉搜索、Re-ID |
| SITL | gz-sim + PX4 | PX4 时钟从动于 gz，非确定 | 飞控交互验证 |

与 RotorPy 的关系（r10）：RotorPy 的快速相机服务视觉算法迭代；gz-sim 的相机本身即一种"基于 GLB 的光栅相机"，单路 720p 约 11 ms/帧，适合作为 Realistic 档的标准相机与 3DGS 合成所需的动态层、深度与分割来源。最终的"快速照片级相机"应作为 Sensor Service 中与后端无关的渲染器，同时服务 Fast 与 Realistic 两档。

### 4.2 World Package → SDF 导出

| World Package 资产 | SDF 映射 | 规范化要求 |
|--------------------|----------|------------|
| `world_manifest.json`（CRS、原点、尺度） | `<spherical_coordinates>`、静态模型位姿 | 世界系统一为 ENU、米；glTF Y-up 到 Z-up 的旋转与 r2 配准尺度烘焙进导出 GLB，避免 SDF 中出现嵌套变换 |
| `appearance.glb` | 静态模型 `<visual><mesh><uri>` | 解压 Draco/meshopt、纹理转 PNG/JPEG、按需分块（单块不超过数百 MB）；记录内容哈希 |
| `collision.mesh` | 同一模型 `<collision>` | 减面至约 10 万面量级（r5 数据），或 bullet-featherstone 的 `convex_decomposition`；标注"几何不完整"区域 |
| `anchors.json`、`walkable.navmesh` | 不进入 SDF | 由 Event Runtime 消费，生成事件磁带 |
| 事件实体（人、车、狗） | 人：`<actor>`（蒙皮）；车：带碰撞体的运动学模型 | 每个事件实体挂 `Label`（实例 ID 与事件实体 ID 建立映射表），供分割与标注 |
| 机队 | `<include>` 机型模板 + 传感器挂载 | 机型模板来自 Drone4D 机型目录；指令语义统一为世界系速度 |
| 天气 | `<wind>` + WindEffects；可选 ParticleEmitter | 风时间线由磁带驱动 |
| 系统清单 | Physics（dartsim + bullet 检测器）、Sensors（ogre2）、Imu/NavSat 等、WindEffects、Label、Drone4DClock | 批处理模式省略 SceneBroadcaster 与 GUI |

导出产物：`world.sdf`、`models/`、`tape.csv`（或二进制磁带）、`gz_manifest.json`（gz 版本、插件版本、资产哈希、种子、步长）。导出后以 `gz sdf -k` 校验并做一次探针渲染。

### 4.3 与权威时钟、事件磁带的同步

1. **权威**：Drone4D Experiment Controller 持有 `t_sim_ns` 与 tick 序号；gz-sim 只在被授予步数时推进，tick 周期必须是物理步长的整数倍（本原型 0.1 s = 25 × 4 ms）。
2. **每 tick 流程**：Agent 观测 → 策略动作 → DroneAdapter 写入指令（同步）→ 授予步数 → `step_done`（真值位姿，写入 StateRing）→ Sensor Service 收齐 (t_{k−1}, t_k] 帧后放行下一 tick。
3. **事件磁带**：在 Drone4DClock 的 `PreUpdate` 中按仿真时间纯函数回放（actor、运动学车辆、风、天气开关），保证与 FleetSim Fast 档使用同一份 Canonical Event Tape；Exogenous 模式下磁带只读；Interactive 模式由 Event Runtime 每 tick 计算增量后经同一通道下发，锁步保证闭环结果可复现。
4. **回合边界**：传感器就绪屏障之后的第一个 tick 定为回合零点；回合重置采用"重启实例 + 重新生成"，不依赖 ECM 状态快照（`WorldControlState` 的 `SerializedState` 恢复覆盖不完整）。
5. **生产化协议**：把"指令集 + 步数授权"合并为单条控制消息（自定义 protobuf，经同一 ZeroMQ 套接字有序投递），`step_done` 扩展为携带速度与角速度的里程计数组，彻底消除跨通道排序问题与 Python GIL 风险。

### 4.4 传感器帧回传 Sensor Service

**帧契约**（与 `awr.sim.sensors.backends.base.SensorFrame` 对齐并扩展）：

| 字段 | 来源 |
|------|------|
| `run_id`、`uav_id`、`slot`、`sensor`、`kind`（rgb/depth/seg/lidar/imu…） | 运行清单 |
| `t_sim_ns`、`tick`、`seq` | gz 帧头时间戳（已验证等于 tick 时刻） |
| `pos_enu_m`、`R_ws` | `step_done` 真值位姿 × 挂载外参（与 FakeBackend 同一公式） |
| 内参、分辨率、编码、FOV、扫描花样 ID | 传感器规格 |
| `payload` | 共享内存或文件引用（大帧不进消息总线） |
| `backend="gz:ogre2"`、`backend_version`、`asset_hash`、`seed` | 运行清单 |

**数据通路**：gz-transport（容器内 127.0.0.1、独立分区）→ C++ Sensor Bridge 进程（订阅全部声明的传感器、按 tick 汇齐、写共享内存、生成帧契约）→ Drone4D Sensor Service：① 写 MCAP 原始录制与数据集目录（`runs/<policy>/<uav>/rgb|depth|lidar/`）；② 经 ANet 按机私有通道发给对应 Agent（每机仅收本机观测）；③ 以 `sim_time` 时间线记录到 Rerun（与 r8 对齐，实体路径如 `/world/uav0/camera/rgb`）。

**与既有 `SensorBackend` 协议的映射**：`open(world, rigs)` → 校验 SDF 中的传感器与 rig 一致并订阅；`request(slots, poses, t)` → 对常开传感器为空操作（位姿由物理决定），对按需传感器转为 `<triggered>` 触发（同样走同步控制消息）；`poll()` → 返回已汇齐的帧。`SensorCaps` 声明 `raycast="gpu"`、`render="raster-glb"`。

**3DGS 合成**：同一 tick 的相机位姿与内参送入 3DGS 渲染器得到背景 RGB 与深度；gz 端为该相机增设一路只渲染动态实体的相机（静态小镇设置 `visibility_flags`，相机设置 `visibility_mask`），提供前景 RGB、深度与实例掩码，按深度逐像素合成，分割标签直接继承。

### 4.5 统一 DroneAdapter

新增 `packages/contracts/rt/caps/gz_sim.json`（建议值）：

```json
{
  "backend": "gz_sim",
  "version": 1,
  "kinds": ["uav", "vehicle", "human"],
  "max_vehicles": 8,
  "clock": {"mode": "lockstep", "pausable": true, "max_speed": 1.0, "steppable": true},
  "cmd": {
    "velocity": "native",
    "hover": "native",
    "goto": {"impl": "emulated", "via": "velocity"},
    "follow_path": {"impl": "emulated", "via": "velocity"},
    "orbit": {"impl": "emulated", "via": "velocity"},
    "takeoff": {"impl": "emulated", "via": "velocity"},
    "land": {"impl": "emulated", "via": "velocity"},
    "kill": "native"
  },
  "ack": {"native": true, "timeout_s": 1.0, "retries": 0},
  "trust_ceiling": 3,
  "truth": true
}
```

`GzSimBackend`（实现 `SimBackend`）：

| 方法 | 实现 |
|------|------|
| `attach(world, clock, bus, ring)` | 在指定 GPU 的工作容器中启动 gz 进程（独立分区），等待锁步服务与传感器就绪屏障 |
| `spawn(spec)` / `despawn(id)` | 优先在导出时预置机位；运行时经 UserCommands 的 `/world/<w>/create`、`/remove` 服务 |
| `dispatch_batch(cmds)` | 高层指令在 Drone4D 侧统一由路径跟踪器转为世界系速度 + 偏航角速度，与 FleetSim 共用同一实现；只下发 `velocity` |
| `step(tick)` | 授予 `tick_period / dt` 步，等待 `step_done`，写入 StateRing（`Pose(t_sim_ns, pos, q_xyzw, vel, omega)`） |
| `snapshot()` / `restore()` | 仅记录运行清单与 tick；恢复采用重放 |

`DroneAdapter.pose()` 读取 StateRing，`derive_state()` 与 Fast 档同一派生逻辑；两档使用同一指令语义、同一 tick 周期与同一事件磁带，从而支持"同策略、两后端"的轨迹与观测差异对比。

### 4.6 部署规格

| 项 | 规格 |
|----|------|
| 镜像 | Ubuntu 24.04 + Gazebo Harmonic（ROS Jazzy vendor 或 OSRF 官方源）+ Drone4D Bridge 插件，约 3.5 GB；Jetty 迁移后同构 |
| 单实例资源 | 显存约 2 GB；CPU 2–3 核；内存约 3 GB（小镇级 GLB） |
| GPU 绑定 | 每容器 `--gpus device=N`（EGL 固定使用可见设备 #0） |
| 隔离 | `GZ_PARTITION=<run_id>`、`GZ_IP=127.0.0.1`、每实例独立世界名 |
| 吞吐基线 | 3 机 ×（720p RGB + 900×40 LiDAR）@10 Hz：单实例约 1× 实时；多实例近线性扩展，需在目标 GPU 上校准 |
| 可观测性 | `/stats`、tick 耗时分解（指令/步进/等帧）、帧缺失计数、请求超时计数写入运行日志 |

### 4.7 实施路线（对应 design-r1 的 M24 · Gazebo Adapter）

1. **M24-a**：以本原型为基础固化 `Drone4DClock` 与 `Drone4DVelocityControl`（单条控制消息协议、里程计回报、单元测试）。
2. **M24-b**：World Package → SDF 导出器（与 r2 的 GLB 规范化、r5 的碰撞网格流程对接）。
3. **M24-c**：C++ Sensor Bridge 与 `GzSimBackend`，接入 StateRing、MCAP、Rerun。
4. **M24-d**：MID-360 射线表（gz-rendering 分叉）与运动畸变；动态层相机，用于 3DGS 合成。
5. **M24-e**：Jetty 迁移评估（批量射线、CpuLidar、性能），以及 PX4 SITL 档。

### 4.8 对外表述建议（UI、网站、README）

- 档位名称：快速仿真（Fast）、高保真仿真（Realistic, Gazebo Sim）、照片级渲染（Photorealistic）、飞控在环（SITL）。
- 能力描述示例："高保真仿真档由 Gazebo Sim 提供刚体动力学、旋翼模型与基于 GLB 场景网格的传感器渲染，在 Drone4D 权威时钟下逐步推进，同一实验条件下可逐位复现。"
- 避免口语化措辞与未经验证的性能承诺；性能数值一律注明硬件、分辨率、传感器组合与测量条件。

---

## 5. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 渲染线程单线程、以 CPU 为主 | 多机多传感器时 RTF 线性下降（6 机 0.45） | 降低分辨率与频率、减少 LiDAR 立方体面（射线表方案）、多进程横向扩展；跟踪 Jetty 与 main 的渲染改进 |
| 共享 GPU 争用 | 同配置吞吐波动可达 3 倍 | 生产 GPU Worker 独占或配额化；Benchmark 只承诺"结果可复现"，不承诺墙钟耗时 |
| 确定性边界 | 跨 GPU 型号、驱动、镜像版本的帧不保证逐字节一致 | 运行清单记录 GPU 型号、驱动、镜像摘要、种子；跨环境比较采用容差标准（与 design-r1 第九节一致） |
| 上游控制器语义与维护 | 分叉 `MulticopterVelocityControl`、gz-rendering 射线表需随版本维护 | 分叉改动保持最小并以补丁形式管理；评估向上游提交 `commandFrame` 与射线表接口 |
| Python 绑定 GIL 缺陷 | 控制与订阅同处 Python 进程时请求超时 | 核心链路用 C++；Python 仅用于研究脚本，且控制与订阅分进程 |
| actor 无物理 | 人不会被车辆或无人机阻挡，cpu_lidar 看不到人 | 需要交互的实体改用运动学碰撞模型；LiDAR 使用 gpu_lidar 或网格射线后端 |
| GLB 兼容性与载入时间 | Draco/KTX2 不受支持；首帧 13–16 s；单实例约 2 GB 显存 | 导出时规范化与分块；实例常驻、按回合复用；记录资产哈希 |
| GLB 尺度 | 摄影测量 GLB 单位未必为米 | 依赖 r2 的 OSM 配准结果烘焙尺度后再导出 |
| PX4 SITL 非确定 | SITL 档结果不可逐位复现 | SITL 档仅用于飞控验证，不进入正式 Benchmark 排名 |
| 版本更替 | Harmonic 2028 年停止维护；main 的 EnTT ECM 与 API 变更影响插件 ABI | 插件按主版本构建；在 Jetty 上建立持续集成后再切换 |
| 许可 | gz 系列 Apache-2.0，Ogre-Next MIT，Assimp BSD | 分叉代码保留原版权头，纳入 THIRD_PARTY_NOTICES |

---

## 6. 附录

### 6.1 产物清单

| 路径（相对 `/data/projs/anet-drone/.cache/research/d3/r9-gz-sim/`） | 内容 |
|------|------|
| `docker/Dockerfile` | 在 `d3-r9-gz-sim:harmonic` 上叠加 g++、Eigen 的开发镜像 |
| `plugin/CMakeLists.txt`、`plugin/src/Drone4DClock.cc`、`plugin/src/Drone4DVelocityControl.{cc,hh}`、`plugin/src/{Common,LeeVelocityController}.*`、`plugin/src/upstream/` | 原型插件与上游对照源码 |
| `plugin/build_out/libDrone4DClock.so`、`libDrone4DVelocityControl.so` | 针对 gz-sim 8.15（Jazzy vendor）构建的插件二进制 |
| `scripts/gen_world.py`、`scripts/make_tape.py`、`scripts/d4d_lockstep.py`、`scripts/py_inproc.py`、`scripts/compare_runs.py`、`scripts/run.sh`、`scripts/batch{1,2,3}.sh` | 世界生成、事件磁带、锁步客户端、进程内对照、确定性比对、批量脚本 |
| `worlds/cfg_*.json`、`worlds/*.sdf`、`worlds/tape_town.csv` | 实验配置与生成的世界 |
| `models/x500_base`、`models/actor/walk.dae` | 机型（来自 r5 的 PX4 模型副本）与 actor 蒙皮（Fuel Mingfei/actor） |
| `results/*.json`、`*_ticks.json`、`*_poses.json`、`*_digests.json`、`*_server.log` | 每次运行的汇总、逐 tick 耗时、位姿、帧摘要与服务器日志；`batch1.log`（GPU 2）、`batch2.log`（GPU 3）、`batch3.log` |
| `results/e1_determinism.json`、`results/g3_show_actor_lidar.json` | 确定性比对与 actor LiDAR 核验 |
| `results/fig_s_full_uav0_t99.jpg`、`results/fig_g3_show_uav0_t199.jpg` | RGB / 深度 / 实例分割示例 |
| `assets/ogre-next/OgreEglPBuffer*.cpp` | EGL 设备选择源码（Ogre-Next v2.3.3） |

### 6.2 复现命令

```bash
# dmax：构建开发镜像与插件
docker build -t d3-r9-gz-sim:dev /data/anet-drone4d-d3/r9-gz-sim/docker
docker run -d --name d3-r9-gz-sim --gpus '"device=3"' --cpus 16 --shm-size 8g \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -v /data/anet-drone4d-d3/r9-gz-sim:/work -v /data/anet-drone4d-d3/data:/glb:ro \
  d3-r9-gz-sim:dev sleep infinity
docker exec d3-r9-gz-sim bash -lc 'source /opt/ros/jazzy/setup.bash && cd /work/plugin && mkdir -p build && cd build && \
  cmake .. -DCMAKE_PREFIX_PATH="/opt/ros/jazzy/opt/gz_sim_vendor;/opt/ros/jazzy/opt/gz_plugin_vendor" && make -j16'

# 锁步基准与确定性
docker exec d3-r9-gz-sim /work/scripts/run.sh e1_lock_a worlds/town3.sdf d4d_town --ticks 300 --hash-all --seed 42
docker exec d3-r9-gz-sim /work/scripts/run.sh e1_lock_b worlds/town3.sdf d4d_town --ticks 300 --hash-all --seed 42
python3 scripts/compare_runs.py results/e1_lock_a results/e1_lock_b
```

### 6.3 关键源码索引

| 主题 | 位置 |
|------|------|
| 单步与系统调度 | gz-sim `src/SimulationRunner.cc` L208、L712、L810、L862、L1098 |
| 渲染流水线与订阅门控 | gz-sim `src/systems/sensors/Sensors.cc` L362、L395、L480、L936、L1016 |
| actor 不参与物理 | gz-sim `src/systems/physics/Physics.cc` L3684 |
| 速度控制器机体系指令 | gz-sim `src/systems/multicopter_control/LeeVelocityController.cc` L118 |
| 风力 | gz-sim `src/systems/wind_effects/WindEffects.cc` L586–L624 |
| 系统优先级 | gz-sim `include/gz/sim/System.hh` L96 |
| 无头回退 | gz-rendering8 `ogre2/src/Ogre2RenderEngine.cc` L549 |
| LiDAR 立方体贴图与射线方向 | gz-rendering `ogre2/src/Ogre2GpuRays.cc` L782、L826 |
| EGL 设备选择 | Ogre-Next v2.3.3 `OgreEglPBufferSupport.cpp` L163 |
| GLB 加载 | gz-common5 `graphics/src/MeshManager.cc` L184；`graphics/src/AssimpLoader.cc` L873 |
| 同进程异步投递、服务回调 | gz-transport13 `src/Node.cc` L491；`src/NodeShared.cc` L743 |
| Python 请求 GIL | gz-transport `python/src/transport/_gz_transport_pybind11.cc` `request_raw`、L285 |
| ROS 标准步进接口 | ros_gz `ros_gz_sim/src/gz_simulation_interfaces/actions/simulate_steps.cpp` L108 |
| PX4 时钟从动 | PX4 `src/modules/simulation/gz_bridge/GZBridge.cpp` L342 |
