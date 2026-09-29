# R23 研究笔记：AirSim / gz-sim（高保真仿真与 Gazebo）
## FastPhysics 与 SimpleFlight 的可移植方程、gz WindEffects / EnvironmentPreload / Levels，以及 World Package → Gazebo / Isaac 导出器设计

> 研究单元：r23 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §11、§17–§27、§35–§37、§41、§45–§49
>
> 仓库快照（均为 shallow clone，只有 1 个提交）：
> - `refs/sim/AirSim` @ `1ca93f6`（2026-09-15，"pin-actions" 维护性提交），★18516，1460 个文件，489 MB。UE 4.27 插件与平台无关的 C++ 核心 `AirLib`。
> - `refs/sim/gz-sim` @ `08b3b70`（2026-09-25，"Use floating-point aspect ratio for thermal camera range"），★1520，2090 个文件，398 MB。`project(gz-sim VERSION 11.0.0)`，`VERSION_SUFFIX pre1`，也就是 Jetty（gz-sim 10，2025-09-30 发布）之后的 main 分支。
>
> 本机实测产物都在 `.cache/research/r23/`，没有改动 `refs/`：
> - `fastphys.py`：把 AirSim `FastPhysicsEngine`、`RotorActuator`、`MultiRotorParams::setupFrameGenericQuad`、SimpleFlight 级联控制和 Mixer 移植为 numpy SoA 向量化实现；同时移植了 gz `LeeVelocityController` 和 gz `WindEffects` 阵风生成器。
> - `exp2.py`：比较 Lee 的几种变体（加减速感知限速、抗饱和积分），扫描积分步长的稳定性，并对比三种风力模型。
> - `exp3_path.py`：在移植的动力学上跑 AirSim `moveOnPath` 的 carrot 路径跟随，对比原版和我们修正后的终点处理。
> - `export_gz_proto.py`：World Package → gz-sim world 导出器原型，用 UrbanScene3D Shenzhen 点云（500 万点）实测。产物在 `gz_export/Shenzhen/`。
>
> 与其他单元的分工：PX4 SITL 与 gz_bridge 由 r20 负责；Prometheus 以及 Level A 运动学 Mock 由 r19 负责；网页天气特效由 r16 负责；MID-360 射线模型由 r04/r05 负责。本文只讲 AirSim 和 gz-sim 本体，以及它们能给我们的 Mock 动力学、环境场、传感器模型和导出器提供什么。

---

## 0. 结论速览

| 仓库 / 模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **gz-sim（整体）** | Gazebo 新架构（ECS + System 插件），PX4 官方 SITL 仿真器 | **adopt**：作为 V0.2+ 的 PX4 SITL 物理后端，由我们的导出器生成 world | V0.2 / V0.5 / V0.6 | ★★★★★ |
| gz `WindEffects` | 全局均匀风：一阶低通 + 正弦 + 高斯噪声的阵风生成器；`F = k·m·(w − v)` 施加在 link 上 | **port** 阵风生成器（Environment Level 1）；**adopt** 为 gz 后端的风插件，但 **k 必须标定** | V0.3 / V0.4 | ★★★★★ |
| gz `EnvironmentPreload` + `EnvironmentalSensorSystem` | 从 CSV 加载时变体网格，传感器按位姿三线性查值，支持 4 种坐标变换 | **port** 数据模式：Environment 场 `E(x,y,z,t)` 的交换格式和查询语义 | V0.4 | ★★★★☆ |
| gz `MulticopterMotorModel` + `MulticopterVelocityControl`（Lee 几何控制） | 转子推力 `kf·ω²`、转子阻力、滚转力矩；SE(3) 几何速度控制 | **port**：给 Level B Mock 用的控制器和转子风阻模型 | V0.4 | ★★★★☆ |
| gz `CpuLidar`（仅 gz-sim 11） | 用 DART + Bullet 的批量射线求交做 LiDAR，**不需要 GPU** | **adopt**（11.0 发布后）：无 GPU 服务器上的 LiDAR 仿真 | V0.5 | ★★★★☆ |
| gz `Levels/Performers` | 按 performer 的 AABB 与 level 的外扩 buffer 做滞回加载/卸载 | **reference**：World Service 分区激活，以及 Web 端 tile 卸载的滞回 | V0.1（思想）/ V0.6 | ★★★☆☆ |
| gz `websocket_server` | gzweb 协议：`op,topic,type,payload` 四段帧，含 sub/unsub/throttle/req 等 13 种操作 | **reference**：我们 WS 协议的订阅与节流语义 | V0.1 | ★★★☆☆ |
| **AirSim（整体）** | UE4 高保真仿真，**2022 年起宣布归档**，现在只有维护性提交 | **不运行**（skip）。**port** `AirLib` 的纯 C++ 算法 | — | ★★★☆☆ |
| AirSim `FastPhysicsEngine` + `RotorActuator` + `MultiRotorParams` | 6 自由度刚体、转子一阶滞后、六面阻力、ISA 空气密度、地面锁 | **port**：Level B Mock 动力学。已用 numpy 移植并验证（§3.1–§3.6） | V0.3 / V0.4 | ★★★★★ |
| AirSim SimpleFlight（级联 PID + Mixer） | 位置 P → 速度 PI → 角度 P → 角速率 P → QuadX 混控 | **reference + 部分 port**：增益表和 Mixer 可以直接用；性能不如 Lee（超调 1.58 m） | V0.4 | ★★★☆☆ |
| AirSim `MultirotorApiBase::moveOnPath` | carrot 追踪、自适应前视、50 Hz 下发速度 | **port** 并修正终点处理：GoTo 和交互式 FollowPath | V0.1 / V0.2 | ★★★★☆ |
| AirSim `api_goal_timeout`（60 ms 看门狗） | 超过 60 ms 收不到 API 指令，就在当前位置悬停 | **port**：Gateway 和 Mock 的指令看门狗 | V0.1 | ★★★★☆ |
| AirSim 天气 API（`WeatherParameter` 0–1） | 纯视觉的材质参数集合，**不影响传感器和物理** | **reference**：只借鉴 UI 语义，不照搬"无物理量"的设计 | V0.3 | ★★☆☆☆ |
| AirSim LiDAR / IMU / GPS / Baro 模型 | LiDAR 是理想射线（无噪声、无强度、不受天气影响）；IMU 有 ARW + bias 随机游走；GPS **没有位置噪声** | **port** 参数模型和 IMU 噪声；LiDAR 和 GPS 的噪声、天气退化要我们自己补（§3.10） | V0.4 / V0.5 | ★★★☆☆ |

**关键结论（实现者先读这几条）**

1. **FastPhysics 的移植已经验证可用，而且足够快，可以作为 Level B Mock。**
   - Generic Quad（F450）的参数：T_max = 4.1794 N/转子，Q_max = 0.05556 N·m，悬停油门 0.5866，推重比 1.70，I = diag(0.006721, 0.008041, 0.014279) kg·m²。
   - 积分器是半隐式梯形（平均加速度），**步长从 2 ms 到 20 ms 结果几乎不变**（超调 1.57 → 1.61 m）。所以可以用 100 Hz 物理，不必照搬 AirSim 默认的 3 ms（333 Hz）。
   - numpy 单核：N ≤ 100 时约 2.5 ms/步，N = 1000 时约 5.5 ms/步。按 100 Hz 算，100 架占单核 25%，1000 架占 55%（§3.6）。
2. **控制器选 Lee 几何控制，加上"减速感知限速 + 抗饱和积分"，不选 SimpleFlight。** 10 m GoTo 的实测对比：

   | 控制器 | 超调 | 最大倾角 | 8 m/s 侧风稳态误差 |
   |---|---|---|---|
   | SimpleFlight | 1.58 m | 29.6° | 0.27 m（xy 速度环只有 P） |
   | Lee + 限速 + 积分 | 0.34 m | 13° | 0 |

   朴素地给 Lee 加积分（没有抗饱和）会积分饱和，**不收敛**（§3.5）。
3. **风力模型有三种，量级差两个数量级，必须标定。**
   - gz `WindEffects` 默认 `force_approximation_scaling_factor = 1`，含义是 `F = 1·m·(w − v)`：8 m/s 风对 1 kg 机体产生 8 N（0.8 g）。实测机体被吹出 **143 m**。
   - AirSim 的六面二次阻力在 8 m/s 时只有 0.42 N，等效 k ≈ 0.053 s⁻¹。
   - PX4 的 x500 模型**没有设置 `enable_wind`**，风只通过 `MulticopterMotorModel` 的转子阻力 `−|Ω|·c_rd·v⊥` 进入（§3.8）。
   - 我们统一用"k 或 C_d·A 标定到参考风速"的做法。导出到 gz 时写入标定后的 k（例如 0.05），不用默认值 1。
4. **gz-sim 的 `EnvironmentPreload` 就是 01-design §18 `E(x,y,z,t)` 的现成交换格式。**
   - 格式：CSV，列为 `timestamp, x, y, z, <var…>`，加载进 `InMemoryTimeVaryingVolumetricGrid`，由 `EnvironmentalSensor` 做时空插值查询，支持 `ADD_VELOCITY_LOCAL` 等 4 种坐标变换。
   - 但 `WindEffects` 本身**只支持全局均匀风**，不读取这个网格。网格风要作用到 gz 里的机体上，需要写一个自定义 System（可以用 `PythonSystemLoader` 写成 Python，§4.3）。
5. **gz-sim 11 的 `CpuLidar` 解决了"本机无 GPU"的 LiDAR 仿真问题。** 它要求 `physics type="dart"` 且 `collision_detector=bullet`。但它**只在 main（11.0.0 pre1）里有**，PX4 v1.15/v1.16 使用的 Harmonic（gz-sim 8）没有这个插件。
6. **AirSim 的天气是纯视觉的。**
   - `simSetWeatherParameter(Rain|Snow|Fog|Dust|…, 0..1)` 只写 UE 的 `MaterialParameterCollection`，传感器和动力学都不受影响。
   - 风有两套，互不相干：物理风 `simSetWind`（NED，m/s，只进阻力）；材质风（-1..1，只驱动粒子方向）。
   - **我们要反过来做**：Environment 用物理量描述天气（能见度 m、降雨 mm/h、风 m/s），一个消光系数 σ 同时驱动 Web 雾、相机退化和 LiDAR 衰减（§3.10）。
7. **导出器原型已在 Shenzhen 点云（1848 × 1999 × 391 m）上跑通。** 流程：DSM/DTM → heightmap(1025²) + LOD1 楼块（按 100 m 分块，154 个非空块）→ `world.sdf`（spherical_coordinates、dart+bullet、WindEffects、EnvironmentPreload、Levels）。

   | DSM 分辨率 / 高度量化 | 楼块数 | 三角形 | 包大小 | 耗时 |
   |---|---|---|---|---|
   | 2 m / 1 m | 42,899 | 51.5 万 | 15.7 MB | 5 s |
   | 4 m / 3 m | 5,248 | 6.3 万 | 2.5 MB | 2 s |
   | 对照：1 m DSM 直接三角化 | — | 740 万 | — | — |

   推荐的碰撞预算：**每块 ≤ 2 万三角形，整城 ≤ 50 万**（§4.3）。

---

## 1. 仓库概览

### 1.1 microsoft/AirSim

| 项 | 内容 |
|---|---|
| 状态 | `project_airsim.md` 写明"将于来年归档，不再更新"，后续转向商业化的 Project AirSim（IAMAI 另有 MIT 开源版 `iamaisim/ProjectAirSim`，**未 clone，未核实**）。最近提交 2026-09-15 只是 CI actions 版本固定，核心代码自 2022 年起冻结。 |
| 语言与构建 | C++17（AirLib，header-only 为主，依赖 Eigen、rpclib/msgpack）+ UE **4.27** 插件（`Unreal/Plugins/AirSim`）+ Python RPC 客户端（`PythonClient/airsim/client.py`，114 个 API）。Linux 构建流程：`setup.sh` 装 clang 8、cmake、Eigen，然后 `build.sh`，最后把插件拷进 UE 工程。**需要 GPU 和 UE**。 |
| 架构 | 客户端 msgpack-RPC（默认端口 41451）→ `RpcLibServerBase` → `VehicleApiBase` / `WorldSimApiBase` → `SimModeWorldBase`，由 `PhysicsWorld` 异步线程驱动 → `FastPhysicsEngine` 或 `ExternalPhysicsEngine`，以及 Unreal 的传感器与渲染。 |
| 对本项目 | 引擎本身不跑（UE4 + GPU + 归档）。价值在 `AirLib` 里**与引擎无关**的算法：动力学、转子、阻力、控制器、路径跟随、传感器噪声、时钟，以及 API 与 settings 的设计。 |

### 1.2 gazebosim/gz-sim

| 项 | 内容 |
|---|---|
| 状态 | 非常活跃。Changelog 显示 10.1.1 发布于 2026-02-03，main 已是 11.0.0 pre1。Harmonic 对应 gz-sim 8（PX4 v1.15+ 默认），Ionic 对应 9，Jetty 对应 10。 |
| 语言与构建 | C++17，CMake（gz-cmake）+ Bazel（`MODULE.bazel`）。依赖 gz-physics（dartsim/bullet-featherstone）、gz-sensors、gz-rendering（ogre2）、gz-transport、sdformat、Qt6（GUI）。Ubuntu 24.04 可以直接 `apt install gz-harmonic` 或 `gz-jetty`。 |
| 架构 | ECS：`EntityComponentManager` 加上 System 插件，每步三个阶段，`PreUpdate`（读写）→ `Update`（物理）→ `PostUpdate`（只读，多线程并行）；执行顺序由 `<priority>` 控制（`include/gz/sim/System.hh`）。Server 可以无头运行（`gz sim -s -r`）；带相机的传感器需要 ogre2 渲染（可用 EGL 无头）。 |
| 对本项目 | (1) PX4 SITL 的物理后端（r20 负责 gz_bridge）；(2) 导出器的目标格式；(3) 一批可移植的系统：WindEffects、EnvironmentPreload、MulticopterMotorModel、Lee 控制、CpuLidar、Levels、websocket_server。 |

---

## 2. 源码结构与关键模块

### 2.1 AirSim（`AirLib/`，路径相对于仓库根）

| 路径 | 关键类 / 函数 | 作用 |
|---|---|---|
| `AirLib/include/physics/FastPhysicsEngine.hpp` | `updatePhysics`、`getNextKinematicsNoCollision`、`getDragWrench`、`getBodyWrench`、`getNextKinematicsOnCollision`、`computeNextPose` | 刚体积分、阻力、碰撞冲量、地面锁（§3.1） |
| `AirLib/include/physics/PhysicsWorld.hpp` | `PhysicsWorld(…, update_period_nanos = 3000000LL)` | 物理异步线程，默认 **3 ms** 一步；提供 `pause` / `continueForTime` / `continueForFrames` |
| `AirLib/include/physics/Environment.hpp` + `common/EarthUtils.hpp` | `Environment::updateState`、`getStandardPressure`、`getAirDensity`、`getGravity` | 按海拔计算 ISA 气温、气压、密度和重力 |
| `AirLib/include/vehicles/multirotor/RotorParams.hpp` | `calculateMaxThrust()` | `T = C_T·ρ·n²·D⁴`，`Q = C_P·ρ·n²·D⁵/(2π)`，GWS 9×5 桨：C_T = 0.109919，C_P = 0.040164，6396.667 rpm |
| `.../multirotor/RotorActuator.hpp` | `setControlSignal`、`setOutput`、`setWrench` | 控制量 u ∈ [0,1] 经一阶滤波（τ = 5 ms）后，推力 = u·T_max·ρ/ρ₀ |
| `.../multirotor/MultiRotorParams.hpp` | `initializeRotorQuadX/HexX/OctoX`、`computeInertiaMatrix`、`setupFrameGenericQuad/Flamewheel/Blacksheep` | 机架几何、惯量、默认参数 |
| `.../multirotor/MultiRotorPhysicsBody.hpp` | `createDragVertices`、`updateSensorsAndController` | 六面阻力顶点；每个物理步之后依次更新传感器和控制器 |
| `.../firmwares/simple_flight/firmware/` | `Params`、`CascadeController`、`PositionController`、`VelocityController`、`AngleLevelController`、`AngleRateController`、`PidController`、`StdPidIntegrator`、`Mixer`、`OffboardApi` | 内置飞控（§3.4） |
| `AirLib/src/vehicles/multirotor/api/MultirotorApiBase.cpp` | `takeoff`、`land`、`moveOnPath`、`moveToPathPosition`、`setNextPathPosition`、`getAutoLookahead`、`emergencyManeuverIfUnsafe` | 任务级 API 与 carrot 路径跟随（§3.7） |
| `AirLib/include/sensors/{lidar,imu,gps,barometer,magnetometer,distance}/*Simple*.hpp` | `ImuSimple::addNoise`、`GpsSimple::addOutputToDelayLine`、`LidarSimpleParams::initializeFromSettings` | 传感器参数与噪声（§3.10） |
| `Unreal/Plugins/AirSim/Source/UnrealSensors/UnrealLidarSensor.cpp` | `createLasers`、`getPointCloud`、`shootLaser` | UE 射线实现 LiDAR 扫描模式 |
| `Unreal/Plugins/AirSim/Source/Weather/WeatherLib.{h,cpp}` | `EWeatherParamScalar`、`setWeatherParamScalar`、`setWeatherWindDirection` | 天气：写 `/AirSim/Weather/WeatherFX/WeatherGlobalParams` 材质参数集合 |
| `Unreal/Plugins/AirSim/Source/WorldSimApi.cpp` | `createVoxelGrid` | 用 `OverlapBlockingTestByChannel` 逐格判定占据，输出 binvox（RLE） |
| `AirLib/include/common/AirSimSettings.hpp`（1408 行） | `loadSettings`、`createSensorSetting` | `settings.json` 的完整解析（§4.6） |
| `AirLib/include/common/{SteppableClock,ScalableClock}.hpp` | `step()`、`stepBy()` | 可步进时钟（默认 20 ms 一步）与可缩放时钟（`ClockSpeed`） |
| `GazeboDrone/src/main.cpp` | 订阅 `~/pose/local/info` → `simSetVehiclePose` | 用 Gazebo 当飞行动力学、AirSim 当渲染和传感器（配合 `ExternalPhysicsEngine`）。这正是"物理与渲染分离"的先例 |

### 2.2 gz-sim（`src/systems/`，共 77 个 System 目录）

| System | 源码 | 作用 / 关键参数 |
|---|---|---|
| `WindEffects` | `src/systems/wind_effects/WindEffects.cc`（774 行） | `UpdateWindVelocity`：对种子风速的幅值、方向、垂直分量分别做一阶低通，再加正弦和噪声，写入 wind 实体的 `WorldLinearVelocity`。`ApplyWindForce`：对 `enable_wind=true` 的 link 施加 `m·k(pos)·(w − v)`。`k(pos)` 可以是分片的可加可分多项式场（`<when xlt=… ><px/qy/rz>`）。话题：`/world/<w>/wind`（设置种子）和 `/world/<w>/wind_info`（发布真值）。 |
| `EnvironmentPreload` | `environment_preload/EnvironmentPreload.cc` | 从 CSV 读入 `common::DataFrame<string, InMemoryTimeVaryingVolumetricGrid<double>>`，挂到 world 实体的 `components::Environment` 上。支持 `<ignore_time>`、球面坐标（`<reference>`、`<units>`），带 `VisualizationTool` |
| `EnvironmentalSensorSystem` | `environmental_sensor_system/EnvironmentalSensorSystem.cc` + `TransformTypes.hh` | 按传感器位姿查询网格（`StepTo` 推进时间会话 + `LookUp` 查值）。`transform_type` ∈ {`ADD_VELOCITY_LOCAL`, `ADD_VELOCITY_GLOBAL`, `LOCAL`, `GLOBAL`}，前两种会减去传感器自身速度（风速计、空速）。`environment_variable[_x/_y/_z]` 映射到列名 |
| `MulticopterMotorModel` | `multicopter_motor_model/MulticopterMotorModel.cc` | 推力 `kf·ω²`；转子阻力 `−|ω|·c_rd·v⊥rel`（其中 `v_rel = v − wind`）；反扭 `−dir·km·T`；滚转力矩 `−|ω|·c_rm·v⊥rel`。电机转速一阶滤波，上升和下降时间常数不同（1/80 s，1/40 s），`rotorVelocitySlowdownSim = 10` |
| `MulticopterVelocityControl` / `LeeVelocityController` | `multicopter_control/*.cc` | Lee 等人 SE(3) 几何控制（速度模式），加伪逆分配矩阵。增益按惯量归一化（§3.5） |
| `CpuLidar` | `cpu_lidar/CpuLidar.cc` | `PreUpdate` 生成射线，写入 `components::RaycastData` 和 `NeedsRaycast`；Physics 系统在 `UpdateRayIntersections` 里用 `GetBatchRayIntersectionFromLastStepFeature` 批量求交；`PostUpdate` 把结果交给 `gz-sensors::CpuLidarSensor` 发布。示例 `examples/worlds/cpu_lidar_sensor.sdf` 注明"不需要 GPU 或渲染引擎"，要求 DART + Bullet |
| `Sensors` | `sensors/Sensors.cc` | 相机、深度相机、GPU LiDAR、热成像、分割、包围盒、广角相机都依赖 ogre2 渲染（`render_engine` 默认 ogre2，可无头） |
| Levels（核心，不是 System） | `src/LevelManager.cc::UpdateLevelsState` | performer 的 AABB 与 level 区域相交就加载；已激活的 level 只要 performer 还在"level + buffer"外扩区内就保持激活，离开外扩区才卸载，即**滞回** |
| `WebsocketServer` | `websocket_server/WebsocketServer.cc` + `combined.proto` | gzweb 协议：帧为 `operation,topic,type,payload`，操作有 sub、pub、unsub、topics、topics-types、protos、particle_emitters、asset、worlds、scene、image、throttle、req。可配 `publication_hz`、鉴权 key 与 admin key、`max_connections`、SSL |
| `SceneBroadcaster` | `scene_broadcaster/SceneBroadcaster.cc` | `dynamic_pose_hertz` 默认 60，`state_hertz` 默认 60 |
| `PythonSystemLoader` | `python_system_loader/` | 用 Python 模块的 `get_system()` 实现 `configure/pre_update/update/post_update/reset` |
| `ColladaWorldExporter` | `collada_world_exporter/` | 把整个 world 的几何合并导出成一个 DAE，可作为转 Isaac 的中间件 |
| `FreeSpaceExplorer` | `free_space_explorer/` | 用 2D LiDAR 自主探索并发布占据栅格图像 |
| Blender 导出脚本 | `examples/scripts/blender/sdf_exporter.py` | 输出 `model.dae` + `model.sdf` + `model.config` 的标准模型目录结构 |

PX4 的 gz 服务端插件清单在 `refs/sim/PX4-Autopilot/src/modules/simulation/gz_bridge/server.config`：Physics、UserCommands、SceneBroadcaster、Contact、Imu、AirPressure、AirSpeed、ApplyLinkWrench、**WindEffects（不带任何参数）**、NavSat、Magnetometer、Sensors（ogre2），另有两个自定义插件 OpticalFlow 和 GstCamera。PX4 的 world 只写 `<wind><linear_velocity>`（例如 `windy.sdf` 里是 `5 2 0`）。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

坐标约定：本节公式与 AirSim 源码一致，用 **NED 世界系 + FRD 机体系**，四元数写作 (w,x,y,z)，这样便于和源码逐行比对。在 Gateway 边界转换到 World ENU + FLU 的公式如下，已在 1000 个随机姿态上与四元数乘法逐一核对：

```text
p_ENU = (p_y, p_x, −p_z)          v_ENU 同理
ω_FLU = (ω_x, −ω_y, −ω_z)
q_ENU/FLU = √½ · (w+z, x+y, x−y, w−z)      # q_NED/FRD = (w,x,y,z)；NED yaw=0（机头朝北）→ ENU yaw=+90°
```

### 3.1 FastPhysics 刚体方程（`FastPhysicsEngine.hpp`）

```text
输入：u_i ∈ [0,1]（i=0..3，控制器输出），全局风 w（NED，m/s），dt
状态：p, v, a（线加速度）, q, ω（机体系）, α（角加速度，机体系）, grounded

1) 转子（RotorActuator::update）
   ũ_i ← ũ_i·e^{−dt/τ_m} + u_i·(1 − e^{−dt/τ_m})        τ_m = control_signal_filter_tc = 0.005 s
   T_i = ũ_i·T_max·ρ/ρ₀         Q_i = ũ_i·Q_max·ρ/ρ₀·d_i     (d = −1 CCW，+1 CW)
   F_b = Σ (0,0,−T_i)                                         # 法向 (0,0,−1)，推力朝上
   τ_b = Σ r_i × (0,0,−T_i) + Σ (0,0,−Q_i)                   # 反扭 = normal × torque_scaler

2) 地面锁（grounded）：|R·F_b|² ≥ (m·g)² 时解锁；锁定期间 v = ω = α = 0

3) 空中积分（getNextKinematicsNoCollision）
   v̄ = v + a·dt/2 ;  ω̄ = ω + α·dt/2
   (F_d, τ_d) = Drag(q, v̄, ω̄, w)                             # 见 §3.3
   a' = (R·F_b + F_d)/m + (0,0,g)
   α' = I⁻¹·(τ_b + τ_d − ω̄ × (I·ω̄))                          # 欧拉方程
   v' = v + (a + a')·dt/2 ;  ω' = ω + (α + α')·dt/2           # 梯形（源码注释叫 Verlet）
   p' = p + v̄·dt ;  q' = normalize(q ⊗ exp(ω̄·dt))             # 机体系角增量右乘

4) 碰撞响应（getNextKinematicsOnCollision，Chris Hecker 冲量法）
   n: 碰撞法向，r: 接触点 − 质心；若 n·v' ≥ 0（正在远离）则忽略
   若是地面碰撞（机体系法向 |n_z|≈1 ± 0.25 且正在下落）：e = 0，μ = 1
   否则 e = restitution = 0.55，μ = friction = 0.5
   j = −(1+e)·(v_c·n) / (1/m + ((I⁻¹(r×n))×r)·n)
   v' = v̄_c + n·j/m ;  ω' += r×n·j ;  再按 μ 施加切向摩擦冲量 ;  ω' *= 0.9
   地面锁：roll = pitch = 0，保留 yaw，v = ω = 0，grounded = true
```

**参数（`setupFrameGenericQuad`，F450 机架）**

| 参数 | 值 | 备注 |
|---|---|---|
| m | 1.0 kg | 源码注释：必须在 max_thrust·4/10 与 idle·max_thrust·4/10 之间 |
| 臂长 L / rotor_z | 0.2275 m / 0.025 m | QuadX，转子 0 = 右前 CCW，1 = 左后 CCW，2 = 左前 CW，3 = 右后 CW |
| 电机组件质量 | 0.055 kg | 计入惯量 |
| 机体盒 | 0.18 × 0.11 × 0.04 m | 盒体惯量 + 电机点质量 |
| C_T / C_P / rpm_max / D | 0.109919 / 0.040164 / 6396.667 / 0.2286 m | 推导得 T_max = 4.1794 N，Q_max = 0.05556 N·m |
| 线阻力系数 C_lin | 1.3/4 = 0.325 | 角阻力系数取同一值 |
| τ_m | 0.005 s | 转子一阶滞后 |
| 物理步长 | 3 ms（`PhysicsWorld` 默认） | 实测到 20 ms 仍稳定（§3.6） |
| 实测推导 | I = diag(0.006721, 0.008041, 0.014279)；悬停 u = 0.5866；推重比 1.70 | `fastphys.py` 打印 |

**高原修正（对 01-design"山区"场景很重要）**：推力按 ρ/ρ₀ 缩放。ISA 下 1000 m 为 0.907，2000 m 为 0.822，3000 m 为 0.742。起飞点在 3000 m 时推重比会从 1.70 降到约 1.26。Mock 必须按 World Package 的海拔 `coordinate.json.origin.h` 加上当前高度，逐步计算 ρ。

### 3.2 其他机架与 P600 参数化

`MultiRotorParams` 已经带 Hex/Octo 的转子布局（`initializeRotorHexX`、`initializeRotorOctoX`），并提供按"臂长数组 + 臂角数组 + 转向数组"生成的通用函数 `initializeRotors`，以及 `computeInertiaMatrix`（盒体 + 电机点质量）。P600 数字孪生的参数化，建议直接采用这组字段：

```json
{ "frame": "quad_x", "mass": 3.2, "arm_lengths": [0.3,0.3,0.3,0.3], "arm_angles_deg": [45,225,315,135],
  "rotor_dirs": ["CCW","CCW","CW","CW"], "rotor_z": 0.05, "body_box": [0.25,0.25,0.12], "motor_mass": 0.12,
  "prop": {"C_T": 0.11, "C_P": 0.047, "max_rpm": 7000, "D": 0.3048, "tc": 0.02},
  "drag": {"C_lin": 0.325, "k_rotor_drag": 8.06e-5} }
```

P600 的数值是**占位值**，必须用真机 ulog 辨识（r19 §3.7 给出了辨识方法：悬停油门对应 m·g/(4·T_max)，阶跃响应对应 τ_m）。

### 3.3 六面阻力与风（`getDragWrench`、`createDragVertices`）

```text
A_prop = π·D²      (源码原样；注意这是桨盘面积 π(D/2)² 的 4 倍，属于物理高估，Mock 建议改成 π(D/2)²)
A_xs   = π·D·h     (桨"侧面"截面，h = 0.01 m)
c_x = (b_y·b_z + 4·A_xs)·C_lin/2   c_y = (b_x·b_z + 4·A_xs)·C_lin/2   c_z = (b_x·b_y + 4·A_prop)·C_lin/2
六个面 k：位置 s_k = ±b/2（沿各轴），法向 n_k = ±e_axis，系数 c_k
Drag(q, v̄, ω̄, w):
   v_rel = Rᵀ·(v̄ − w)                          # 机体系相对空速（风从这里进入）
   for k: v_k = v_rel + ω̄ × s_k ; c = n_k·v_k
          if c > 0.1:  f_k = −n_k·c_k·ρ·c² ; τ_k = s_k × f_k      # 只有迎风面产生阻力
   return (R·Σf_k, Στ_k)
```

实测（Generic Quad，ρ = 1.225）：c_x = 0.005383，c_y = 0.005838，c_z = 0.10993 m²。

| 迎风风速 | 阻力 | 配平倾角 | 等效线性 k = F/(m·v) |
|---|---|---|---|
| 5 m/s | 0.165 N | 0.96° | 0.033 s⁻¹ |
| 8 m/s | 0.422 N | 2.46° | 0.053 s⁻¹ |
| 10 m/s | 0.659 N | 3.85° | 0.066 s⁻¹ |
| 15 m/s | 1.484 N | 8.60° | 0.099 s⁻¹ |

局限：AirSim 的风是全局均匀向量（`setWind`），只作用在阻力上，不影响转子推力、诱导阻力或桨盘入流。

### 3.4 SimpleFlight 级联控制（`simple_flight/firmware/`）

每个物理步都会更新一次（`MultiRotorPhysicsBody::updateSensorsAndController`）。PID 输出统一截断到 [−1, 1]。

```text
Axis4 映射：axis0 = roll 通道 ↔ y（机体 vy），axis1 = pitch 通道 ↔ x（机体 vx），axis2 = yaw，axis3 = throttle ↔ z
PositionWorld:   v_goal[a] = clip(0.25·(p_goal − p)[a]) · 6.0          (x, y, z 都是 P = 0.25；速度上限 6 m/s)
VelocityWorld:   e_b = Rᵀ·v_goal − Rᵀ·v                                (目标和测量都转到机体系，含 yaw)
   roll_goal  = +clip(0.2·e_b.y)·(π/5.5)      pitch_goal = −clip(0.2·e_b.x)·(π/5.5)    (xy 只有 P，没有 I)
   I_z = clip(I_z·0.9999 + dt·e_b.z·2.0) ;  pid_z = clip(2.0·e_b.z + I_z) ;  thr = max((1 − pid_z)/2, 0.3)
AngleLevel:      rate_goal = clip(2.5·wrap(angle_goal − angle)) · 2.5 rad/s
AngleRate:       ctl = clip(0.25·(rate_goal − ω))                       (roll, pitch, yaw)
Mixer（QuadX）:   u_i = thr + roll·Mr_i + pitch·Mp_i + yaw·My_i
   Mr = [−1, +1, +1, −1] ;  Mp = [+1, −1, +1, −1] ;  My = [+1, +1, −1, −1]      顺序：FR, RL, FL, RR
   若 min(u) < 0：所有 u 整体加 (−min)；若 max(u) > 1：所有 u 除以 max ；最后 clip 到 [0,1]
   若 thr < min_angling_throttle (0.05)：所有 u = thr
看门狗（OffboardApi::update）：已起飞且 now − goal_timestamp > api_goal_timeout (60 ms)
   → goal_mode = Position，goal = 当前位置（"API call was not received, entering hover mode for safety"）
```

**特征**：xy 速度环只有 P → 有风时有稳态误差；角度上限 32.7°、角速率上限 2.5 rad/s → 机动激进；z 积分器带泄漏（每步乘 0.9999）；起飞高度 `takeoff_z = −2 m`（NED）。

### 3.5 Lee 几何速度控制（gz `LeeVelocityController.cc`）与我们的改进

```text
# 以 gz 源码为准，改写为 NED；ENU 下把 g 的符号反过来即可
a_des = clip(K_v ∘ (v_cmd − v), a_max) [+ I]          # gz 里写作 acc = K_v∘(v − R·v_cmd)/m + g，然后 b3 = −acc/|acc|
F     = m·(a_des − g_NED)                               # g_NED = (0,0,+g)，F 指向上
b3d   = −F/|F| ;  b1c = 当前机头 R[:,0]（gz：用 cmd_vel.angular.z 控 yaw 角速率）
b2d   = normalize(b3d × b1c) ;  b1d = b2d × b3d ;  R_d = [b1d b2d b3d]
T     = −F·(R·e3)                                        # 推力投影到当前 b3
e_R   = ½·vee(R_dᵀR − RᵀR_d) ;  e_ω = ω − RᵀR_d·ω_d      (ω_d = (0,0,ψ̇_cmd))
τ     = I·(−K_R'∘e_R − K_ω'∘e_ω)                          # K' = K/I，也就是"按惯量归一化"的增益
分配矩阵（calculateAllocationMatrix，Lee eq.1）：
   A[:,i] = [sin(θ_i)·L_i·kf, −cos(θ_i)·L_i·kf, −dir_i·kf·km, kf]ᵀ     ω² = A†·[I·α_des; T] ，A† = Aᵀ(AAᵀ)⁻¹
   rank(A) < 4 时报错，说明构型不可控
```

gz 示例的增益：X3 机体 m = 1.5 kg，I = (0.0347563, 0.07, 0.0977)，`velocityGain 2.7`，`attitudeGain 2 3 0.15`，`angularRateGain 0.4 0.52 0.18`，`maximumLinearAcceleration 2 2 2`。按惯量归一化后，K_R' = (57.6, 42.9, 1.54) s⁻²，K_ω' = (11.5, 7.43, 1.84) s⁻¹，K_v/m = 1.8 s⁻¹。这组归一化增益可以直接搬到任意惯量的机体上。

**我们的改进（Mock 的 GoTo / Hover 用，也适用于 r19 的 Level A）**

```text
d = p_goal − p ;  dist = |d| ;  û = d/dist
v_lim = min( v_max,  K_p·dist,  √(2·η·a_max·dist) )     # η = 0.7；按剩余距离做减速感知限速
v_cmd = û·v_lim
e_v = v_cmd − v ;  a_raw = K_v·e_v
I  ← I + K_i·e_v·dt，只在 |a_raw| ≤ a_max 的轴上积分（条件积分抗饱和）；|I| ≤ I_max
a_des = clip(a_raw, a_max) + I                          # K_i = 0.6，I_max = 1.5 m/s²
```

### 3.6 移植验证实测（`fastphys.py`、`exp2.py`）

场景：从 (0,0,−5) 悬停，GoTo (10,0,−5)，物理步长 3 ms，风向为 NED +y（东向侧风）或 −x（逆风）。

| 控制器 | 风 | 进入 0.2 m 误差带 | 超调 | 最大倾角 | 末 5 s 横向误差 |
|---|---|---|---|---|---|
| SimpleFlight | 0 | 6.89 s | **1.58 m** | **29.6°** | 0 |
| SimpleFlight | 东 5 m/s | 6.96 s | 1.57 m | 29.6° | 0.106 m |
| SimpleFlight | 东 8 m/s | 无法进入 | 1.57 m | 29.6° | **0.272 m**（手算估计 0.25 m） |
| SimpleFlight | 东 8 m/s，gz 阵风 | 无法进入 | 1.57 m | 29.6° | 0.242 m |
| Lee（纯 P 外环 K_p = 1） | 0 | 6.79 s | 1.95 m | 13.3° | 0 |
| Lee（纯 P） | 东 8 m/s | 无法进入 | 1.97 m | 13.5° | 0.254 m |
| Lee + 朴素积分（K_i = 1，无抗饱和） | 0 | **不收敛**（末端误差 2.1 m，倾角饱和 29.6°） | 4.69 m | — | — |
| Lee + 限速 | 0 | **5.88 s** | 0.57 m | 12.9° | 0 |
| Lee + 限速 + 条件积分 | 0 | 6.77 s | **0.34 m** | 13.2° | 0 |
| Lee + 限速 + 条件积分 | 东 8 m/s | 6.81 s | 0.34 m | 13.4° | **0** |
| Lee + 限速 + 条件积分 | 逆风 8 m/s | 8.63 s | 0 | 12.9° | 0 |

**步长扫描**（无风）：SimpleFlight 在 2/3/5/8/10/20 ms 下超调分别为 1.57/1.58/1.58/1.59/1.59/1.61 m；Lee + 限速分别为 0.56–0.60 m，全部稳定。结论：**Level B Mock 用 10 ms（100 Hz）完全足够**。

**阵风生成器**（gz `wind.sdf` 参数，种子风速 8 m/s）：|w| 在 1/5/10/20/60/120 s 时依次为 0.77/3.23/5.28/7.22/7.98/8.00 m/s，这就是一阶上升，τ = `time_for_rise` = 10 s。方向带 ±5° 正弦（周期 20 s）加 0.03° 噪声。

**性能**（numpy 2.5.3 单核，每物理步，含控制器）：

| N | SimpleFlight | Lee | 100 Hz 时单核占用 |
|---|---|---|---|
| 1 | 2.34 ms | 2.19 ms | 23% |
| 10 | 2.11 ms | 2.17 ms | 22% |
| 100 | 2.49 ms | 2.56 ms | 25% |
| 1000 | 5.55 ms | 6.35 ms | 55–64% |

N 较小时，耗时主要是 numpy 的调用开销。如果以后需要 333 Hz 或 1 万架以上，可以把 `step()` 用 numba 或 Rust（PyO3）重写；本机 venv 没有装 numba，本文未测。

### 3.7 carrot 路径跟随（`MultirotorApiBase::moveOnPath`）与修正

```text
参数：T_cmd = 1/50 s（SimpleFlightApi::getCommandPeriod）；dist_acc = 0.5 m（getDistanceAccuracy）
lookahead = max(v·T_cmd·(adaptive>0 ? 30 : 40), 1.5·dist_acc)          # getAutoLookahead；v = 5 时为 3.0 m
braking_dist = clip(v·vel_to_breaking_dist, min, max)
   SimpleFlight 的构造函数把 vel_to_breaking_dist 和 min_breaking_dist 都设为 0，所以 SimpleFlight 下没有终点减速
path3d = [当前位置] + path ；PathSegment 记录 (起点, 单位向量, 长度, 累计长度)，最后补一个零长段
cur = (seg=0, off=0) ; next = advance(cur, lookahead)
loop（每个 T_cmd 一次）直到 next 位于最后一段且 goal_dist ≤ 0：
   d = next.pos − p
   vel = |d| < dist_acc ? 0 : ( |d| ≥ v·T_cmd ? d̂·v : d̂·|d|/T_cmd )    # "Too close dest" 分支
   若 |p_z − next_z| ≤ dist_acc：vel_z = 0                              # 0.5 m 高度死区
   moveByVelocity(vel)
   gv = next.pos − cur.pos ;  goal_dist = (p − cur.pos)·ĝv              # 投影前进量，只进不退
   la_err = |(p − cur.pos) − ĝv·goal_dist|·adaptive                    # 横向偏差加到前视距离上
   cur = advance(cur, max(goal_dist, 0)) ;  next = advance(cur, lookahead + la_err)
advance(loc, dist)：沿段累加；超过最后一段就停在终点，并返回溢出量
```

**实测**（20 m 正方形航线，dt = 10 ms，`exp3_path.py`；水平横向误差与高度误差分开统计）：

| 控制器 / 变体 | v | 水平横向误差（最大 / 平均） | 最大高度偏差 | 终点误差 |
|---|---|---|---|---|
| SimpleFlight / AirSim 原版 | 3 | 1.30 / 0.23 m | 0.87 m | 0.52 m |
| SimpleFlight / AirSim 原版 | 5 | 1.99 / 0.38 m | **1.51 m** | 0.57 m |
| SimpleFlight / 修正版 | 5 | 1.91 / 0.36 m | 1.42 m | 0.30 m |
| Lee（a_max = 2）/ 原版 | 5 | 6.42 / 1.27 m | 0.06 m | 0.13 m |
| Lee / 修正版 | 5 | 6.43 / 1.11 m | 0.02 m | 0.10 m |
| Lee / 修正版 | 8 | **13.92** / 3.39 m | 0.06 m | 0.02 m |

结论：
- carrot 不会预判拐角。拐角超调由加速度上限决定：拐弯半径约 v²/a_max，5 m/s、2 m/s² 时就是 12.5 m。
- SimpleFlight 靠 30° 大倾角拐弯，但 z 环没有做倾角推力补偿，会掉高 1.5 m。
- AirSim 原版终点处理的问题：没有刹车；"too close" 分支会要求 |d|/T_cmd 的速度，0.5 m 距离就是 25 m/s；再加上 0.5 m 死区，终点误差约等于 dist_acc。
- **我们的做法**：
  - carrot 只用于交互式 GoTo 和实时编辑的航线。
  - 修正版速度取 `min(v, √(2·a_brake·剩余路径长) + 0.2, |d|/T_cmd)`，并去掉 z 死区。
  - 正式的 FollowPath 用时间参数化轨迹（梯形或 min-jerk，r19 §3.8），每个航点的过弯速度限制为 `v_corner = √(a_lat,max·r_eff)`，其中 `r_eff = 转角半径 = w_turn / tan(Δψ/2)`，`w_turn` 是转弯平滑距离。

### 3.8 风：阵风生成器与三种风力模型

**gz `UpdateWindVelocity`（Level 1 阵风，直接移植）**

```text
k_M = dt/τ_M ; k_D = dt/τ_D ; k_V = dt/τ_V                            # 默认 τ = 1 s；wind.sdf 用 τ_M = 10 s，τ_D = 30 s
|w|_mean ← (1−k_M)·|w|_mean + k_M·|seed_xy|
|w| = |w|_mean·(1 + A_%·sin(2πt/P_M)) + N(0, σ_M)                     # 0.05，60 s，0.0002
dir_mean ← 首次取种子方向，之后 (1−k_D)·dir_mean + k_D·atan2(seed_y, seed_x)
dir = dir_mean + A_dir·sin(2πt/P_D) + N(0, σ_D)                        # 5°，20 s，0.03°
w_z_mean ← (1−k_V)·w_z_mean + k_V·seed_z ;  w_z = w_z_mean + N(0, σ_V)  # σ_V = 0.03
w = (|w|·cos dir, |w|·sin dir, w_z)                                   # gz 为 ENU，发布到 /world/<w>/wind_info
```

**三种风力模型对比（`exp2.py (d)`：Lee + 限速 + 积分，悬停，8 m/s 侧风）**

| 模型 | 公式 | 8 m/s、1 kg 时的力 | 实测 |
|---|---|---|---|
| AirSim 六面二次阻力 | Σ −n_k·c_k·ρ·(n_k·v_rel)² | 0.42 N | 1.97 s 回到 0.2 m 误差带，倾角 4.0° |
| gz WindEffects，默认 k = 1（PX4 server.config 就是默认） | F = k·m·(w − v)，作用在 link 质心 | **8 N** | 被吹出 **143 m**（a_max = 2 < 8 m/s²） |
| gz WindEffects，标定 k = 0.057 | 同上 | 0.46 N | 2.03 s，倾角 4.1°，与 AirSim 一致 |
| gz MulticopterMotorModel 转子阻力（x500 实际走的路径） | 每转子 −\|Ω_i\|·c_rd·v⊥rel，c_rd = 8.06428e-5 | x500（2 kg）悬停 Ω ≈ 757 rad/s 时，4 转子合计 0.244 N/(m/s)，8 m/s 为 1.95 N | 未测（来自源码推导） |

**建议的 Mock 风力模型（Level B）**：两项叠加，即二次机身阻力加上线性转子诱导阻力：

`F_wind = −½ρ·C_D·A(v̂)·|v_rel|·v_rel − (Σ|Ω_i|)·c_rd·(v_rel − (v_rel·b3)·b3)`

其中 `v_rel = v − W(p,t)`，W 来自 Environment 场。C_D·A 和 c_rd 用真机悬停抗风时的倾角来标定（倾角 θ 满足 tan θ = F/(m·g)）。

### 3.9 Environment 场的交换格式（EnvironmentPreload / EnvironmentalSensor）

```text
CSV 列：timestamp, x, y, z, <var1>, <var2>, ...        （首行为列名；可以 ignore_time，也可以用球面坐标 lat/lon/alt 加单位）
加载：DataFrame<string, InMemoryTimeVaryingVolumetricGrid<double>>，每个变量一张 4D 网格（t 之外可以是非均匀网格）
查询：session = grid.CreateSession() ; session = grid.StepTo(session, t) ; val = grid.LookUp(session, pos)   # 时空插值
变换（TransformTypes.hh::transformFrame）：
   ADD_VELOCITY_LOCAL : R⁻¹·(reading − v_sensor)     → 机体系相对风速，风速计和空速管用
   ADD_VELOCITY_GLOBAL: reading − v_sensor
   LOCAL              : R⁻¹·reading                   → 例如磁场
   GLOBAL             : reading
```

这正对应 01-design §18 的 `environment.query(x,y,z,t)`。我们的 Environment Service 应该：
1. 内部用 **numpy / zarr 的规则网格**（风：u, v, w；消光系数 σ；降雨 mm/h；温度；气压）。
2. **导出**时可以写成 gz 的 CSV（供 gz 后端使用），也可以写成 VDB 或 NPZ（供 Web 可视化使用）。
3. 查询 API 直接采用上面 4 种 transform 语义，`query(x,y,z,t, frame="ADD_VELOCITY_LOCAL", pose, vel)`。

### 3.10 传感器模型：AirSim 有什么、缺什么、我们补什么

**LiDAR（`LidarSimpleParams` + `UnrealLidarSensor::getPointCloud`）**

```text
默认参数（VLP-16）：channels = 16，range = 100 m，PPS = 100000，rot = 10 Hz，HFOV = [0, 359]°，
  VFOV 无人机默认 [−45, −15]°（车辆为 [−10, +10]°），update = 10 Hz，DataFrame 为 VehicleInertialFrame 或 SensorLocalFrame
每 tick：N = round(PPS·Δt)（上限 1e5）；n_laser = N/channels ；Δθ_h = rot·360·Δt/n_laser
  laser_k 俯仰 = VFOV_upper − k·(VFOV_upper − VFOV_lower)/(channels−1)
  for k, i: θ_h = (θ_cur + i·Δθ_h) mod 360 ；不在 HFOV 内就跳过
            ray_q = q_vehicle ∘ q_lidar ∘ Euler(pitch = 俯仰, yaw = θ_h) ；end = start + front·range
            命中则输出点（带 segmentation id），未命中不输出
  θ_cur += rot·360·Δt
缺失：没有测距噪声、没有强度、没有多回波、没有运动畸变补偿，也不受天气影响
```

MID-360 这类非重复扫描的模式由 r04/r05 负责。本单元只贡献"按 PPS·Δt 分帧"的通用调度框架，以及下面的天气退化模型。

**IMU（`ImuSimple::addNoise`，MPU-6000 参数）**

```text
σ_g = ARW/√dt ;  ω_meas = ω + N(0, σ_g) + b_g ;  b_g += N(0, (BS_g/√τ_g)·√dt)
σ_a = VRW/√dt ;  a_meas = a + N(0, σ_a) + b_a ;  b_a += N(0, (BS_a/√τ_a)·√dt)
ARW = 0.30°/√h，BS_g = 4.6°/h，τ_g = 500 s ；VRW = 0.24 mg，BS_a = 36 µg，τ_a = 800 s ；dt 下限 1 ms
```

**GPS（`GpsSimple`）**：`eph` 和 `epv` 经一阶滤波从 100 收敛到 0.1（τ = 0.9 s）；`eph` ≤ 3 为 3D fix，≤ 4 为 2D fix；延迟线 0.2 s；50 Hz；启动延迟 1 s。**位置直接取真值，没有噪声**。我们需要自己补：一阶 Gauss-Markov 位置误差（σ_h 约 1.5 m 或 RTK 0.02 m，τ 约 60 s），外加城市峡谷里的多路径跳变。

**气压计（`BarometerSimple`）**：压强因子是 Gauss-Markov 过程，σ = 0.0365/20，τ = 3600 s（约每小时 10 m 漂移）；另加不相关噪声 σ = 2.7 Pa（MS5611）。

**相机**：`ImageType` 包括 Scene、DepthPlanar、DepthPerspective、DepthVis、DisparityNormalized、Segmentation、SurfaceNormals、Infrared、OpticalFlow、OpticalFlowVis。`NoiseSettings` 是后处理噪声（RandContrib 0.2、HorzWave、HorzNoiseLines、HorzDistortion）。这些都依赖 UE 渲染，我们只借用枚举作为 Sensor API 的类型表。

**天气到传感器的退化（AirSim 和 gz 都没有，我们新增；V0.4，系数需要标定）**

```text
统一主参数：消光系数 σ(x,y,z,t) [1/m]，由能见度 V（MOR）换算：σ = 3.912/V（Koschmieder，对比度阈值 2%）
  雾：V 取自 Environment；雨：σ_rain = a·R^b（R 单位 mm/h；近红外的 a、b 需要按文献或实测标定，量级约为 0.1–1 /km）
  沙尘：用能见度直接换算
Web 雾（视觉）：   f = 1 − exp(−σ·d)     # 自定义 fog factor；不要用 three.js 的 FogExp2（它是 exp(−(ρd)²)，与物理不一致）
相机：            I = J·e^{−σd} + A·(1 − e^{−σd})          # 大气散射模型，A 为天空光
LiDAR（每条射线）：SNR(R) = SNR₀·ρ_t·e^{−2σR}·(R_ref/R)² ;  P_hit = sigmoid(ln SNR / s) ;  σ_r = σ_r0·(1 + k·(R/R_max)²)
                   另有雾中近距离假回波：P_clutter(r∈[1,10] m) ∝ 1 − e^{−σ·r}
LiDAR 有效距离：   解 e^{−2σR}·(R_ref/R)²·SNR₀ = 1，得到 R_max(σ) → UI 上显示 LiDAR FOV 锥体的实际长度
```

设计要点：**Web 雾、相机退化、LiDAR 衰减共用同一个 σ 场**。滑杆可以是 0–1，但 UI 同时显示物理量（能见度 m、降雨 mm/h）。

### 3.11 Levels 滞回（`LevelManager::UpdateLevelsState`）

```text
对每个 performer（AABB = pose ± size/2）和每个 level（box = pose ± size/2，外扩 buffer）：
  if box ∩ performer ≠ ∅                       → 加载（加入 levelsToLoad）
  elif level 已激活 and (box ⊕ buffer) ∩ performer ≠ ∅ → 保持
  elif level 已激活                             → 卸载
加载集合与卸载集合先去重；同时在两个集合里的 level 从卸载集合中剔除；实体按名字去重（多个 level 可以共享实体）
```

用法：
- **Web 端** tile/octree 节点的卸载要用滞回：加载阈值 SSE > τ，卸载阈值 SSE < 0.5τ，或者等价地给距离加 buffer。这样可以避免相机在边界抖动时反复请求（r12/r13 的 LRU 之外，再加一层防抖）。
- **服务端** World Service 以每架无人机为 performer，只激活它周围的碰撞 tile。用于 V0.6 的百架规模，以及导出到 gz 的 `<level>`。

### 3.12 时间控制与看门狗

- AirSim 的 `ClockType`：`ScalableClock`（`ClockSpeed` 倍速）或 `SteppableClock`（每步 20 ms，用于锁步）；`simPause` / `simContinueForTime(s)` / `simContinueForFrames(n)`。
- 这些正好对应 01-design §39 Timeline 的 Pause / Play / ×N / Step。我们的 Sim Service 应暴露 `clock.set_rate(r)`、`clock.step(n)`、`clock.pause()`，物理步长保持不变，只改变墙钟节奏。
- 看门狗：60 ms 收不到指令就悬停，这是 Gateway 的必备安全逻辑。Web 控制通道（WebSocket）断开或卡顿时，Mock 和真机都应自动进入 Hold。r19 已指出 Prometheus 真机的心跳在 5 s 后触发降落，两者分层互补。

### 3.13 gz websocket_server 协议要点（给我们的 WS 协议做参考）

- 帧格式为 `op,topic,type,payload`；`throttle` 按话题限频；`image` 单独订阅；`asset` 按 URI 取文件；`scene` 和 `worlds` 返回场景元数据；支持鉴权与 `max_connections`（超限时返回关闭码 1008，reason 为 "max_connections"）。
- 我们采用相同的**语义**，但编码换成 JSON 控制帧加二进制（msgpack 或 FlatBuffers）状态帧：
  - `sub/unsub(topic)`；
  - `throttle(topic, hz)`：前端按 FPS 反馈下调遥测频率，例如后台标签页降到 2 Hz；
  - `req(service, payload)`：请求-应答式调用；
  - `scene`：返回 World Package 清单。

---

## 4. 在本项目中的落点与复用方式

### 4.1 模块与版本矩阵

| 可复用项 | 来源 | 目标模块 | 方式 | 版本 | MVP |
|---|---|---|---|---|---|
| 减速感知限速 + 条件积分 | 本文 §3.5（基于 gz Lee 与 AirSim 的教训） | `simulation/mock`（Level A 与 B 都用） | port | V0.1 | 是 |
| carrot GoTo（修正版） | AirSim `moveOnPath` | `simulation/mock/mission` | port | V0.1 | 是 |
| 指令看门狗 | AirSim `OffboardApi` 60 ms | `apps/api/gateway`、mock | port | V0.1 | 是 |
| 阵风生成器 | gz `WindEffects::UpdateWindVelocity` | `environment/wind`（Level 1） | port | V0.1（Mock 风）/ V0.3 | 是 |
| 标定后的风力 k 或 C_D·A | 本文 §3.8 | `simulation/mock` | port | V0.1 | 是 |
| Environment 查询语义（4 种变换） | gz `TransformTypes.hh` | `environment/api` | port | V0.1（接口）/ V0.4（网格） | 是 |
| Levels 滞回 | gz `LevelManager` | `apps/web` tile 调度、`world/service` | reference | V0.1 | 是 |
| WS 订阅与限频语义 | gz `websocket_server` | `apps/api/ws` | reference | V0.1 | 是 |
| 时钟与回放控制 | AirSim clocks | `simulation/clock` | port | V0.1 | 是 |
| NED ↔ ENU 转换公式 | 本文 §3 开头 | `apps/api/gateway` | port | V0.1 | 是 |
| FastPhysics 6 自由度 + 六面阻力 + ISA 密度 | AirSim `FastPhysicsEngine` | `simulation/mock`（Level B） | port | V0.3 / V0.4 | 否 |
| Lee 几何控制 + 分配矩阵 | gz `multicopter_control` | `simulation/mock`（Level B） | port | V0.4 | 否 |
| 转子诱导阻力 | gz `MulticopterMotorModel` | `simulation/mock`（Level B） | port | V0.4 | 否 |
| IMU / Baro 噪声，GPS eph/epv | AirSim sensors | `sensors/imu`、`sensors/gnss` | port | V0.4 | 否 |
| LiDAR 参数与调度 | AirSim `LidarSimple` | `sensors/lidar` | port | V0.4 | 否 |
| 天气到传感器退化（σ 场） | 本文 §3.10 | `environment` + `sensors` | 自研 | V0.4 | 否（V0.3 先统一视觉雾的 σ） |
| World → gz 导出器 | 本文 §4.3 原型 | `simulation/gazebo/exporter` | 自研 | V0.2 | 否 |
| 网格风 gz System | gz `PythonSystemLoader` + EnvironmentPreload | `simulation/gazebo/plugins` | adopt + 自研 | V0.4 | 否 |
| CpuLidar | gz-sim 11 | gz 后端 | adopt | V0.5（待 11.0 发布） | 否 |
| World → Isaac（USD）导出 | 本文 §4.4 | `simulation/isaac/exporter` | 自研 | V1.0 | 否 |

### 4.2 Mock 动力学分级（与 r19 合并后的建议）

| 级别 | 实现 | 用途 | 频率 | 成本 |
|---|---|---|---|---|
| **A 运动学级联** | r19 的 `mock_uav.py`（fake_uav 加 PX4 增益加一阶姿态滞后），**加上本文 §3.5 的限速与抗饱和** | MVP 默认；百到千架流畅演示；Timeline 倍速 | 100 Hz 物理，WS 10–50 Hz | 1000 架 2.2 ms/步 |
| **B 刚体转子** | 本文 `fastphys.py`：FastPhysics + Lee + 六面阻力 + 转子诱导阻力 + ISA | V0.3/V0.4 的"风到机体力"、高原推力衰减、姿态真实感（FPV 抖动） | 100 Hz（已验证 ≤ 20 ms 稳定） | 100 架 2.5 ms/步 |
| **C PX4 SITL + gz** | r20 的 gz_bridge + 本文导出的 world | V0.2+ 的飞控真值、任务与 failsafe 验证 | 250 Hz（4 ms） | 每架一个 PX4 进程 |

三个级别共用同一套 `DroneState` / `DroneCommand` 契约（r19 §4.2），Gateway 按 `backend: mock_a | mock_b | px4_gz | p600` 路由。Level A/B 的切换只影响 `simulation/mock` 里的 `DynamicsModel` 接口：

```python
class DynamicsModel(Protocol):
    def step(self, dt: float, cmd: CommandBatch, env: EnvSampler) -> None: ...   # SoA 批量
    def state(self) -> StateBatch: ...                                           # 统一输出 World ENU + FLU
```

### 4.3 World Package → Gazebo（gz-sim）导出器设计

**目标**：把一个 World Package 一键生成 PX4 可直接加载的 gz world：`PX4_GZ_WORLD=<id> make px4_sitl gz_x500`，或者 `gz sim -s -r worlds/<id>.sdf`。

**输出目录**

```text
exports/gazebo/<world_id>/
├── worlds/<world_id>.sdf                     # 主 world
├── models/
│   ├── terrain/{model.config, model.sdf, dtm_<2^n+1>.png | dtm.tif, textures/…}
│   ├── <world_id>_tile_XX_YY/{model.config, model.sdf, meshes/lod1.obj (碰撞), meshes/visual.glb (外观)}
│   └── buildings_lod1/…（可选：用 footprints 的 <polyline> 挤出）
├── environment/env_field.csv                 # EnvironmentPreload（风、σ、降雨……）
├── plugins/grid_wind.py                      # PythonSystemLoader 网格风（V0.4）
├── server.config.snippet                     # 需要合入 PX4 server.config 的插件（WindEffects 参数等）
└── README.md                                 # 设置 GZ_SIM_RESOURCE_PATH，以及 PX4 启动命令
```

**映射表**

| World Package | gz SDF | 规则与注意事项 |
|---|---|---|
| `coordinate.json` 原点（WGS84 lat/lon/h） | `<spherical_coordinates>`：EARTH_WGS84，`world_frame_orientation=ENU`，heading 0 | 我们的 World ENU 与 gz world 帧**完全等同**，不需要任何变换；NavSat 由此得到经纬度 |
| DTM（PDAL 地面分类后栅格化，r09） | `<heightmap>`：PNG 16 bit，边长 (2ⁿ+1)，或 GeoTIFF DEM（经 GDAL） | 需要 `<physics type="dart">` 加 `collision_detector=bullet`。`<size>` 取 (2ⁿ·res, 2ⁿ·res, z 范围)，`<pos>` 取中心；超出部分用边缘值填充 |
| 碰撞几何（按 tile） | 每个 tile 一个 static `<model>`，内含 `<collision><geometry><mesh><uri>`（OBJ、DAE、STL、GLB 都支持） | 预算：每 tile ≤ 2 万三角形，整城 ≤ 50 万；**不要**开 `optimization="convex_decomposition"`（静态三角网格在 bullet 里走 BVH 就可以） |
| 外观（mesh 或 3DGS） | `<visual><mesh>` GLB（带顶点色或纹理） | gz 不能原生渲染点云 visual，只能用 mesh；无头运行且没有相机传感器时，可以省略 visual |
| 语义建筑 footprint + 高度 | `<polyline><point>…</point><height>h</height></polyline>` | SDF 原生挤出多边形（`examples/worlds/polylines.sdf` 里就用于 collision），是 LOD1 最省的表达 |
| 禁飞区 | 不进 SDF；作为 Gateway 的 geofence | gz 没有 geofence 语义 |
| Environment Level 0/1（常值风 + 阵风） | `<wind><linear_velocity>` 作种子 + WindEffects 参数（time_for_rise、sin、noise） | `force_approximation_scaling_factor` **写标定值**（§3.8），不能用默认的 1。PX4 的 server.config 不带参数，必须覆盖 |
| Environment Level 2（地形或建筑风） | ① 粗略做法：WindEffects 的 `<when xlt=… ><k/px/qy/rz>` 分片多项式缩放；② 正规做法：EnvironmentPreload CSV + 自定义 GridWind System | WindEffects 的风向全局一致，只能缩放大小。网格风需要在 `PreUpdate` 里给每个 link 查询 W(p,t) 再施力。注意 `MulticopterMotorModel` 只读全局 wind 实体，所以转子阻力仍然是均匀风，这是已知偏差（§6） |
| σ、降雨等传感器环境 | EnvironmentPreload 列 + `EnvironmentalSensor`（可选） | gz 的相机和 LiDAR 不会读取这些值；gz 后端的天气退化要么在 PX4 或 ROS 侧做后处理，要么只在 Mock 后端生效 |
| 大场景分区 | `<plugin name="gz::sim" filename="dummy">` 下的 `<performer>` 和 `<level>`（带 `<buffer>`） | 需要用 `gz sim --levels` 启动；每架 UAV 一个 performer |
| 传感器 | 无 GPU：`CpuLidar`（gz-sim 11）；有 GPU：`gpu_lidar`、`camera` | Harmonic 没有 CpuLidar，要么装 EGL 软件渲染，要么放弃 LiDAR |

**导出流程（伪代码，对应 `export_gz_proto.py`）**

```text
export_gazebo(wp: WorldPackage, profile = {res: 2.0, zq: 1.0, tile: 100, tri_budget_tile: 20000}):
  P  = wp.pointcloud.read_level(max_points = 2e7)             # 从 octree 取中等层级即可
  DSM = grid_max(P, res)，空洞用 3×3 max 迭代填充
  DTM = opening(DSM, 40 m)  或  wp.terrain.dtm（有就用）     # 灰度开运算：先 min 滤波，再 max 滤波
  nDSM = DSM − DTM ;  B = nDSM > 3 m
  heightmap = pad_to_pow2_plus1(DTM) → PNG16，z 归一化，保存 z 范围
  for tile in grid(tile):
     Q = round(nDSM[tile]/zq)·B
     rects = greedy_merge(Q)                                  # 同高度的矩形合并
     若 rects·12 > tri_budget_tile：zq *= 2 或 res *= 2，重算这个 tile
     write OBJ(rects → 8 顶点 / 12 三角的棱柱) ；写 model.sdf 和 model.config
  若 wp.semantic.footprints 存在：优先改用 <polyline> 挤出（更精确，也更少三角形）
  world.sdf = physics(dart + bullet, 4 ms) + systems + <wind> + WindEffects(k_cal, gust)
            + EnvironmentPreload(env.csv) + spherical_coordinates + terrain + tiles + levels(performers)
  env.csv  = wp.environment.fields → 按 (t, z, y, x) 重排后写 CSV（Level 1 可以只写 2 个时间片）
  验证：XML 可解析 ；（CI）gz sdf -k ；gz sim -s -r --iterations 100 ；PX4 起飞 hover 30 s 不报错
```

**实测**（Shenzhen，500 万点，1848 × 1999 × 391 m，单位为米，z 轴朝上）

| DSM 分辨率 | 高度量化 | 非空 tile | LOD1 楼块 | 三角形 | 直接三角化 DSM | 包大小 | 耗时 |
|---|---|---|---|---|---|---|---|
| 1 m | 1 m | 156 / 399 | 250,638 | 3,007,656 | 7,403,700 | 93.9 MB | 23.6 s |
| 2 m | 1 m | 154 / 399 | 42,899 | 514,788 | 1,853,852 | 15.7 MB | 5.1 s |
| 2 m | 3 m | 154 / 399 | 27,152 | 325,824 | 1,853,852 | 10.0 MB | 4.3 s |
| 4 m | 3 m | 145 / 399 | 5,248 | 62,976 | 464,928 | 2.5 MB | 2.1 s |

2 m 网格的统计：空格 9.6%，建筑格 12.3%，最大 nDSM 为 374 m，DTM 范围 0.6–36.6 m，heightmap 为 1025²。**默认档位建议取 2 m / 3 m**（33 万三角形，每 tile 平均约 2100 个），它兼顾了楼宇外轮廓精度和 bullet 的求交速度。本机没有安装 gz，因此只验证了 XML 结构；gz 实际加载与 PX4 起飞留作 V0.2 的 CI 用例（docker 镜像 `gazebo:harmonic` 或 r20 的环境）。

### 4.4 World Package → Isaac Sim（USD）导出器设计（草案，未实测）

| World Package | USD | 说明 |
|---|---|---|
| 坐标原点 | `/World` 的 `customData: {wgs84_origin, enu}`；`metersPerUnit = 1`，`upAxis = Z` | Isaac 没有原生地理参考，由我们的 Gateway 负责换算 |
| 地形与碰撞 tile | `UsdGeom.Mesh`，加 `UsdPhysics.CollisionAPI` 与 `MeshCollisionAPI(approximation = "none")`（静态三角网格） | 复用 §4.3 同一批 OBJ，转成 USD 即可（`usd-core` 的 Python 包或 Omniverse 的 asset converter） |
| 外观 | 带纹理的 mesh 用 `UsdGeom.Mesh` 加 `UsdShade` 材质；3DGS 用 Isaac Sim 5.x 的神经渲染或 USD 扩展（**需要另外核实**） | 点云可以用 `UsdGeom.Points` 预览，不参与 RTX 传感器求交 |
| 语义 | 用 `Semantics.SemanticsAPI` 给 prim 打类别（building、tree、road） | 供 Replicator 做分割真值 |
| 风、环境 | 自定义 OmniGraph 节点或 Python 扩展：读取 env NPZ，按 prim 位姿施加 `PhysxForceAPI` | Isaac 没有内建风场 |
| 传感器 | RTX LiDAR 配置 JSON（MID-360 模式来自 r04）、相机 | V1.0 的高保真传感器 |

也可以走中间格式：gz 的 `ColladaWorldExporter` 把整个 world 导出成 DAE，再转 USD。但 DAE 会丢失语义和分块信息，**只适合作为兜底**。

### 4.5 World Package → AirSim / Colosseum（可选，低优先级）

不建议投入。如果确实需要，最小映射是：
- 生成 `settings.json`：`SimMode: Multirotor`，`OriginGeopoint` 取自 `coordinate.json`，`Wind` 为 NED 向量，`Vehicles{UAV_i: {VehicleType: SimpleFlight 或 PX4Multirotor, X, Y, Z, Yaw, Sensors…}}`。PX4 多机时端口按 `TcpPort = 4560 + i`、`ControlPortLocal = 14540 + i`、`ControlPortRemote = 14580 + i` 分配。
- 场景几何需要导入 UE 工程，无法做到自动化。
- 天气映射为 `Rain = clip(R/50 mm·h⁻¹)`、`Fog = clip(1 − V/5000 m)`、`Dust = clip(PM10/1000)`（**都是经验映射**）。

### 4.6 场景配置（借鉴 AirSim settings.json）

AirSim 的 `settings.json` 有三点值得借鉴：
1. 顶层分 `SimMode`、`ClockSpeed`、`PhysicsEngineName`、`Wind`、`OriginGeopoint`；
2. 以 `Vehicles{name: {...}}` 为键的多机声明；
3. 每机的 `Sensors{name: {SensorType, Enabled, X,Y,Z, Roll,Pitch,Yaw, 参数…}}`（SensorType：1 Baro，2 IMU，3 GPS，4 Mag，5 Distance，6 LiDAR）。

我们的 `scenarios/<id>.json` 建议写成：

```json
{
  "version": 1, "world": "shenzhen", "backend": "mock_a", "clock": {"rate": 1.0, "physics_hz": 100, "telemetry_hz": 20},
  "environment": {"preset": "breezy", "wind": {"speed": 6, "dir_deg": 250, "gust": {"rise_s": 10, "amp_pct": 0.05, "period_s": 60}},
                  "visibility_m": 8000, "rain_mmh": 0},
  "vehicles": {
    "P600-01": {"model": "p600", "spawn_enu": [0, 0, 0], "yaw_deg": 90,
                "sensors": {"lidar": {"type": "mid360", "pose": [0, 0, 0.1, 0, 0, 0], "hz": 10},
                            "cam": {"type": "rgb", "fov_deg": 90, "res": [1280, 720]}}}
  },
  "missions": [{"vehicle": "P600-01", "type": "follow_path", "speed": 5, "waypoints_enu": [[0,0,30],[200,0,30]]}]
}
```

---

## 5. 对比与推荐

| 维度 | gz-sim | AirSim |
|---|---|---|
| 2026 年活跃度 | 高（main 到 11.0 pre1，2026-09-25 仍有提交） | 低（已宣布归档，只有 CI 维护） |
| Star | 1.5k | 18.5k |
| 与 PX4 / P600 的契合 | PX4 官方 SITL 后端（gz_bridge） | PX4 SITL 可用，但依赖 UE4 + Windows 生态 |
| 无 GPU 环境 | 服务端可以无头运行；物理、IMU、NavSat、CpuLidar（11.0）都不需要 GPU | 必须有 UE 和 GPU |
| 环境场 | WindEffects（均匀风）+ EnvironmentPreload（4D 网格） | 全局风（只进阻力）+ 纯视觉天气 |
| 可移植算法 | Lee 控制、电机模型、阵风、Levels | FastPhysics、SimpleFlight、carrot、传感器噪声、时钟 |
| 我们怎么用 | **adopt** 为 Level C 后端和导出目标；port 阵风生成器和 Lee | **port** AirLib 算法，**不运行**引擎 |

**推荐排序**：
1. **gz-sim**：V0.2 起接 PX4 SITL；V0.4 起提供网格风插件；V0.5 使用 CpuLidar。
2. **AirSim（AirLib 算法）**：V0.1 用 carrot、看门狗、时钟；V0.3/V0.4 用 Level B 动力学和传感器噪声。
3. AirSim 引擎本体、Project AirSim、Colosseum 都不纳入路线；高保真渲染与传感器按 01-design 走 Isaac Sim（V1.0）。

---

## 6. 风险与注意事项

1. **gz 的风力默认值很危险。** PX4 的 `server.config` 以默认 k = 1 加载 WindEffects。一旦给某个 link 设置了 `enable_wind`，风力就是 m·(w − v)，8 m/s 风会吹飞机体。x500 默认不设 `enable_wind`，风只经转子阻力进入。导出器必须显式写入标定后的 k，并在 README 里说明。
2. **WindEffects 只支持全局均匀风**；`MulticopterMotorModel` 读的是全局 wind 实体。网格风需要自定义 System，而且在不打补丁的情况下，转子阻力仍然用均匀风，多机不同位置的风无法区分。可选方案：fork `MulticopterMotorModel`，改为按 link 位置查 EnvironmentalData。这属于 V0.4 的工作量。
3. **版本错位**：CpuLidar 只在 gz-sim 11（尚未发布）里有；PX4 v1.15/1.16 默认 Harmonic（gz-sim 8）。插件名（例如 `gz-sim-cpu-lidar-system`）和 SDF 版本（1.9 与 1.10/1.11）要由导出器按目标版本生成；PX4 自带的 world 用 `sdf version='1.9'`。
4. **gz 的 heightmap** 要求 (2ⁿ+1) 的正方形；大城市 1 m DTM 超过 2049² 后，内存和加载时间都会激增。建议地形 DTM 用 2–4 m 分辨率，近地面需要精细碰撞的区域改用 mesh tile。
5. **碰撞预算**：1 m DSM 直接三角化是 740 万三角形，PX4 以 250 Hz 运行时 bullet 会成为瓶颈（本文没有实测 gz 的步进耗时，这是估计）。必须用 LOD1 或 polyline，并控制每个 tile 的三角形数量。
6. **AirSim 移植的坑**：
   - 源码里 `A_prop = π·D²` 高估了 4 倍；
   - 阻力面在法向速度 < 0.1 m/s 时截断；
   - SimpleFlight 的 xy 没有积分项，有风时存在稳态误差；
   - moveOnPath 在 SimpleFlight 下没有刹车，终点有 0.5 m 死区；
   - GPS 没有位置噪声，LiDAR 没有噪声；
   - 风只进阻力。

   全部照搬会让 Mock 显得"过于理想"或"过于激进"。
7. **帧约定混用**：AirSim 用 NED/FRD，gz 和我们的 World 用 ENU/FLU，Prometheus 也用 ENU。统一在 Gateway 边界转换（§3 开头的公式），Mock 内部用什么帧由实现决定，但**输出必须是 World ENU**。
8. **性能**：numpy Mock 在 N 较小时有约 2 ms 的固定调用开销；如果 Timeline 需要 ×10 快进（100 Hz 物理变成 1000 步/秒），单核会超载。可以用子步批处理（一次调用推进多步，积分循环放进 numba 或 Rust），或者快进时把物理降到 50 Hz（已验证 20 ms 步长稳定）。
9. **AirSim 的 GPU 与平台限制**：UE 4.27、clang 8、Windows 优先，本机没有 GPU。**不要**尝试在本机构建 AirSim。
10. **Isaac 导出未实测**；3DGS 进入 Isaac 的路线需要单独调研（r03 或 r13 可以补充）。

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§26 与 §35 "无人机物理层不建议自行从零开发，采用 PX4 SITL"：要补上分级。**
   - MVP 走 Level A 运动学 Mock（r19），V0.3/V0.4 走 Level B 刚体 Mock（本文），V0.2+ 可选 Level C（PX4 SITL + gz）。
   - 三者共享 `DroneState` / `DroneCommand` 契约，由 Gateway 按 `backend` 路由。
   - 否则 MVP 会被 PX4 + gz 的部署拖住，而本机无 GPU 的约束下 gz 的相机传感器也跑不起来。
2. **§19–§20 风场分级要写清楚"风怎样变成力"。**
   - 当前文档只描述 W(x,y,z,t)，没有写力模型。
   - 建议增加 §20.5"风到机体力模型"：二次机身阻力 + 线性转子诱导阻力（§3.8 的公式），参数用真机抗风倾角标定。
   - 同时写明 gz WindEffects 的默认 k = 1 不可用。
3. **§20 Level 1 的"gust / turbulence"要给出具体模型**：
   - 采用 gz 的一阶低通 + 正弦 + 高斯噪声（参数 τ、A%、P、σ），以及 Dryden 湍流（留作 V0.4）；
   - Level 2 用 WindNinja 网格（r16/r17 负责），Level 3 的 CFD 输出统一写成 Environment 网格，交换格式兼容 gz 的 EnvironmentPreload CSV 和 VDB/NPZ。
4. **§22–§24 雨、雾、沙要改成"物理量优先 + 单一消光场"。**
   - 以能见度 V（m）、降雨 R（mm/h）、沙尘浓度为主参数，换算成统一的 σ(x,y,z,t)；
   - σ 同时驱动 Web 雾、相机退化和 LiDAR 衰减（§3.10）；
   - UI 的 0–1 滑杆只是物理量的映射；
   - 不要走 AirSim 那种"0–1 纯视觉参数"的路。
5. **§18 的 `environment.query(x,y,z,t)` 要补上"帧与相对运动"参数**：`frame ∈ {GLOBAL, LOCAL, ADD_VELOCITY_LOCAL, ADD_VELOCITY_GLOBAL}`，并传入 pose 和速度。空速计、风速计、相对风阻力都需要这些。
6. **§35 "Gazebo 与 Isaac 共用同一个 World Model"要落到导出器上**。在 §41 World Package 下新增：
   - `exports/{gazebo, isaac, airsim}`；
   - `geometry/collision/index.json`（tile 网格、AABB、三角形数）；
   - `geometry/terrain/dtm.tif`；
   - `semantic/footprints.geojson`（直接导出为 SDF `<polyline>`）；
   - `vehicles/p600.json`（§3.2 的参数化）。

   同时明确"World ENU = gz world 帧"，原点由 `<spherical_coordinates>` 表达。
7. **§37 的频率要修正。** 文档写"Physics 100~1000 Hz"，而实测表明 Mock 用 100 Hz 就足够（20 ms 仍稳定）。建议分开写：Mock 100 Hz；PX4 + gz 为 250 Hz（4 ms，PX4 的 world 默认值）；WebSocket 10–50 Hz，支持按话题 `throttle`（参考 gz 的 websocket_server）；渲染 60 FPS。
8. **§39 Timeline 要补"仿真时钟"定义。** 物理步长固定，×N 倍速只改变墙钟节奏；要支持 Step（单步 N 帧，对应 AirSim 的 `continueForFrames`）和锁步模式（PX4 lockstep 或 SteppableClock）；Pause 时 WebSocket 保持心跳。
9. **§30 控制模式要补"指令看门狗"与"安全层"。** Web 控制流超过 T_wd（Mock 建议 250 ms，真机按 Prometheus 的 5 s 心跳策略）没有新指令，就自动进入 Hold；GoTo 要做加减速感知限速；FollowPath 要做过弯限速（§3.7）；另外还要有 geofence（World Package 的禁飞区）。
10. **§27 的 P600 数字孪生参数要加上"海拔与空气密度"**：推力按 ρ(h)/ρ₀ 缩放，3000 m 时降到 0.742。这对"山区"场景是一阶效应，Mock 必须实现。
11. **§14 的点云 LOD 要补"卸载滞回"**：加载阈值和卸载阈值分开，借鉴 gz Levels 的 buffer，避免相机抖动造成反复请求。
12. **§11 的表格要更新**：
    - "Gazebo：飞控 Backend"一栏应写明是 **gz-sim（Harmonic 8 或 Jetty 10）**，不是 Gazebo Classic，并注明 CpuLidar 要求 ≥ 11；
    - "Unreal Engine：高保真 Backend"一栏补充"AirSim 已归档，不建议选"。
