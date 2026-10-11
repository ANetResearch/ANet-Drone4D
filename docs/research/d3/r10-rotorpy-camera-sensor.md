# r10 研究笔记：RotorPy 多旋翼仿真与快速相机传感器，以及 Drone4D 快速照片级相机传感器设计

> 研究单元：D3 / r10-rotorpy　　日期：2026-10-11　　性质：只读研究，不修改项目代码
>
> 参考仓库：`refs/discovery/rotorpy`（github.com/spencerfolk/rotorpy，`main` = `40b0969`，2026-09-07 "The Camera Update (#45)"，tag `v3.0.0`；本轮 `git pull` 无新提交，已加深历史 40 个提交用于追溯）。对照阅读：`refs/discovery/crazyflow`（`f9ce54d`，2026-09-26，`crazyflow/sim/sensors/splat.py`）、`refs/sim/gz-sim`（`e08ab4e`，2026-10-09，`src/systems/python_system_loader`）、`refs/discovery/rerun`（0.39.0-alpha，2026-09-27）、D2 代码 `python/awr/sim/sensors/{camera.py, backends/base.py, cbrng.py}`、r3 单元脚本 `.cache/research/d3/r3-camera-render/{common.py, nvr.py}`。
>
> 实验环境：dmax，GPU 2（A40 48 GB；同卡另有 r4 单元的小负载），容器 `d3-r10-rotorpy`（镜像 `d3-r10-rotorpy:0.1`：CUDA 12.6 devel、torch 2.7.1+cu126、nvdiffrast 0.4.0、gsplat 1.5.3、rerun-sdk 0.38.1；`--cpus 16`）。场景：`an_entire_small_town_scanned_by_a_drone.glb`（复用 `/data/anet-drone4d-d3/data/`）。
>
> 脚本与结果：`.cache/research/d3/r10-rotorpy/`（`scripts/d4cam.py` 为传感器原型，`exp1`–`exp4` 为实验脚本，`results/` 为 JSON 与样张，`logs/` 为原始日志，`docker/Dockerfile`）。dmax 工作目录 `/data/anet-drone4d-d3/r10-rotorpy/`。

---

## 0. 结论速览

| 主题 | 结论 | 对 D3 的含义 |
|---|---|---|
| RotorPy 定位 | 纯 Python 多旋翼 6DOF 仿真：集总气动力矩、一阶电机、7 种控制抽象、SE3 几何控制、Dryden 阵风、IMU / 动捕 / 2D 测距 / 相机传感器、torch 批量版、Gymnasium 向量环境、PX4 / ArduPilot HIL。MIT 许可 | 作为 **算法开发宿主与动力学参考** 采用；气动与风模型的移植结论沿用 n03（§2.1、§3.1–3.2），本文不重复 |
| v3.0 相机的本质 | **不是渲染器**，是"世界特征点的针孔投影 + 方块溅射"。几何只来自轴对齐方块（AABB）表面上生成的特征点；遮挡只对 AABB 做 slab 判定；像面上没有 z-buffer，后写的特征覆盖先写的 | 适合 VO / VIO 与视觉策略的**数据流与接口验证**（自带特征点真值、描述子、可见性），不能承担 GLB 沙盘的照片级观测 |
| RotorPy 相机速度（A40 / 16 核） | GPU 批量：14.5 万点 1.2 ms/帧，145 万点 4.5 ms/帧（无遮挡块）；遮挡块 50 / 200 / 400 个时为 14–21 / 53–72 / 132–160 ms/帧，且**不随批量摊薄**（逐机、逐块 Python 循环）。numpy 单机 36 ms – 30 s/帧 | 原生相机的成本由"特征点数 × 遮挡块数 × 机数"决定，与分辨率几乎无关；城市尺度场景下反而不快 |
| RotorPy 相机画质（以 GLB 网格渲染为参考） | 把小镇表面采样成 1M / 4M / 16M 个特征点交给 RotorPy：PSNR 6.8–18.5 dB、SSIM 0.01–0.42（16M 点、溅射半径 1 时最好，113 ms/帧）。同样点数初始化、未经训练的 3DGS 面元：20.8 / 22.7 / 24.0 dB，1.1 / 2.7 / 7.6 ms/帧；r3 训练的 3M 高斯在训练分布视角为 33.6 dB | 照片级相机必须换渲染范式：网格光栅化或 3DGS |
| RotorPy 实现缺陷（v3.0.0） | ① 共享世界且没有方块时，`_render_shared_world` 把 `not_occluded` 初始化为全 False，**所有特征不可见**；② `simulate()` / `simulate_batch()` 只在物理步网格上采帧，且下一帧时刻从实际采样时刻起算：100 Hz 物理下请求 30 Hz 实得 25 Hz、40 Hz 实得 33.3 Hz、60 Hz 实得 50 Hz；③ 帧取步末瞬时位姿，无曝光、运动模糊、卷帘；④ 全部帧在内存累积到仿真结束；⑤ 每次渲染把整个特征数组从 numpy 重新上传 GPU；⑥ IMU / 动捕 / Dryden 使用全局 `np.random`，并行批量下不可按种子复现 | 我方相机的时间语义需重新定义（绝对网格触发、曝光中点时间戳、物理步间位姿插值），随机数采用计数器式（D2 `cbrng.py`） |
| 照片级相机原型（GLB 2.0M 三角形，16384² 图集，单 A40） | 一次光栅化同时输出 RGB / 平面深度 / 实例 / 语义。640×480：原始 229–235 fps，含畸变 + ISP 199–212 fps，含 nvjpeg 编码与回读 184–196 fps；1280×720：原始 146–148 fps，含畸变 + ISP 120–125 fps，含编码与回读 101–112 fps。4 子帧运动模糊约为单帧的 1/4 | 相机侧实时容量：640×480 / 15 Hz 约 12 架机，720p / 15 Hz 约 7 架机；离线批量时 300 s、10 架机、15 Hz、720p 的一次回放约 7.5 分钟/卡。光栅化占网格路径耗时 85% 以上，视锥裁剪与硬件光栅化是主要优化方向（§3.7、§4.6） |
| 3DGS 路径（gsplat 1.5.3，批量视角） | 4M 高斯：320–480 fps（640×480 与 720p 基本一致）；16M 高斯：92–156 fps；"3DGS 背景 + 网格动态前景按深度合成 + ISP"（720p，B=8）：16M 时 124 fps，4M 时 184 fps | 3DGS 不比网格慢。遮挡判定应采用 r3 的结论：以网格深度判定前景遮挡（假可见 0.009%），而不是 3DGS 期望深度（假可见 14.3%） |
| 端到端瓶颈 | RotorPy 批量动力学每步 GPU 约 20–36 ms、CPU 约 16–20 ms，与机数基本无关（受 Python 与内核启动开销支配），均达不到 100 Hz 实时；16–64 架机闭环的实时倍率 0.13–0.30，小批量时物理占 64%，大批量时相机与回读占 70% | Fast 档位的动力学采用 FleetSim 的 SoA 实现（n03 §3.10），RotorPy 只作参考；相机服务独立成 GPU Worker，按卡横向扩展 |
| 宿主复用 | 原型 `BatchedPhotorealCamera` 与 `BatchedPinholeCamera` 同签名，已作为 `rotorpy.simulate.simulate_batch(camera=...)` 的相机直接运行（8 架机、10 Hz、21 帧，输出 `(21, 8, 480, 640, 3)`）；同一对象实现 D2 `SensorBackend` 的 `open / request / poll` | 一套相机同时服务 RotorPy、FleetSim、gz-sim（经 Python System 插件，§4.9） |
| Rerun | 相机观测以 `Transform3D` + `Pinhole(RDF)` + `EncodedImage(JPEG)` + `DepthImage` + `SegmentationImage` 写入 `.rrd`：160 帧每帧记录开销 1.3 ms，载荷 162 MB 写成 88 MB；事件真值放在独立子树 | 实体路径与 r8 单元统一为 `/fleet/{uav}/cam0/*` 与 `/truth/*`（§4.11）；导出参赛数据时裁掉 `/truth` |
| 许可 | RotorPy MIT；gsplat Apache-2.0；Rerun MIT / Apache-2.0；gz-sim Apache-2.0；**nvdiffrast 为 NVIDIA Source Code License（1-Way Commercial），第 3.3 条限定仅限研究与评估用途** | 网格光栅化的产品化实现换用 EGL / OpenGL（moderngl，MIT；r3 实测 720p 单相机 1.37 ms/帧）、Vulkan 或 gz-sim Ogre2；nvdiffrast 仅用于研究原型与基准。nvdiffrast 0.4.0 已移除 OpenGL 光栅器 |

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 仓库 | spencerfolk/rotorpy（UPenn Kumar Lab），`pyproject.toml` 版本 3.0.0，`Development Status :: 4 - Beta` |
| 规模 | 包内 Python 约 1.24 万行，示例与测试约 0.67 万行；`sensors/camera.py` 1,621 行，`tests/test_camera.py` 1,584 行，`examples/visual_servoing_policy.py` 1,023 行 |
| 依赖 | 必需：numpy、scipy、matplotlib、pandas、cvxopt、gymnasium；批量：torch、torchdiffeq、roma、opt-einsum；学习：stable-baselines3；HIL：pymavlink |
| 最近演进 | 2025-09 PX4 SITL（lockstep HIL）→ 2026-03 RAPTOR 基础策略 → 2026-04 torch 改为可选 → 2026-08 Crazyflie 调参 → **2026-09-07 相机更新（v3.0）** |
| 许可 | MIT（Copyright 2023 RotorPy Authors） |

---

## 2. 源码结构与关键实现

### 2.1 模块总览

| 路径 | 关键类 / 函数 | 要点 |
|---|---|---|
| `vehicles/multirotor.py` | `Multirotor`、`BatchedMultirotor(Params)` | 状态 `[x, v, q(xyzw), w, wind, rotor_speeds]`；单机 `solve_ivp`（RK45），批量 `torchdiffeq.odeint`（`dopri5` / `rk4`）；气动力矩与地面接触。详见 n03 §2.1、§3.1 |
| `vehicles/{crazyflie, crazyfliebrushless, hummingbird, px4_sihsim_quadx}_params.py` | `quad_params` | 30 g / 44 g / 0.5 kg / 1.0 kg 四种机型；`px4_sihsim_quadx` 与 PX4 SIH 参数对齐，最接近我方 1–2 kg 级机型 |
| `vehicles/{px4, ardupilot}_multirotor.py` | `PX4Multirotor.step` | MAVLink HIL：`HIL_SENSOR` / `HIL_STATE_QUATERNION` 下行，`HIL_ACTUATOR_CONTROLS` 上行，lockstep 阻塞等待 |
| `controllers/quadrotor_control.py`、`raptor.py` | `SE3Control`、`BatchedSE3Control`、`RaptorFoundationPolicy` | Lee 几何控制；RAPTOR 为 pip `foundation-policy` 的预训练策略 |
| `trajectories/*` | `MinSnap`、`BatchedMinSnap`、`CircularTraj`、`HoverTraj` 等 | `update(t)` 返回 flat outputs（位置至 snap、偏航及其导数） |
| `wind/*` | `NoWind`、`ConstantWind`、`SinusoidWind`、`LadderWind`、`DrydenGust(LP)`、`WindTunnel` 及批量版 | 接口 `update(t, position) -> wind(3)`；Dryden 为二阶 Tustin 离散 |
| `sensors/imu.py`、`external_mocap.py`、`range_sensors.py`、`camera.py` | `Imu`、`MotionCapture`、`TwoDRangeSensor`、`PinholeCamera`、`BatchedPinholeCamera` | 统一 `measurement(state, ...)` 范式 |
| `world.py`、`world_features.py` | `World`、`WorldFeatureGenerator` 注册表 | 世界 = 边界 + AABB 方块；v3 新增"视觉特征"层 |
| `simulate.py`、`environments.py` | `simulate`、`simulate_batch`、`Environment.run / save_camera_data` | 固定顺序主循环；相机按 `frame_rate` 抽帧 |
| `learning/quadrotor_environments.py` | `QuadrotorEnv(VecEnv)` | SB3 向量环境；观测为 13 维状态，**未接入相机** |

### 2.2 动力学、控制与风（摘要）

- 动力学与气动：旋翼推力 `k_eta·ω²`、偏航力矩 `k_m·ω²`，旋翼阻力（H 力）`-ω·diag(k_d,k_d,k_z)·v_local`、挥舞力矩 `k_flap`、平动升力 `k_h`、机身寄生阻力 `c_D·|v|·v`，一阶电机 `τ_m`；不建模机身升力、风沿机身的空间变化。逐桨集总力矩可直接移植（n03 §3.1 已给出向量化版本）。
- 控制抽象：`cmd_motor_speeds / cmd_motor_thrusts / cmd_ctbm / cmd_ctbr / cmd_ctatt / cmd_vel / cmd_acc`，高层指令在车辆模型内部用 SE3 类律转换为转速——这使"策略只出速度指令"的视觉策略可以不经外环直接闭环（`visual_servoing_policy.py` 即如此）。
- 风：单机 Dryden 为每机一套滤波器状态；批量版有 `BatchedNoWind / BatchedConstantWind / BatchedSinusoidWind / BatchedLadderWind / BatchedDrydenGust` 及通用包装 `BatchedWind`。注意 `BatchedNoWind` 的张量固定在 CPU，GPU 批量需自行提供同设备风对象（本文实验已绕过）。
- 随机数：IMU 噪声与偏置游走、动捕噪声与伪影、Dryden 激励、电机噪声全部使用全局 `np.random` / `torch.normal`，无实例级种子。固定事件、反复采集的 Benchmark 需要计数器式随机数（D2 已有 `python/awr/sim/sensors/cbrng.py`）。

### 2.3 v3.0 相机传感器（重点）

**渲染方式。** `PinholeCamera.render` 的流程为：取世界全部特征点 → 世界系到相机系（`p_c = R_WC (p_W − p_WC)`，`R_WC = R_BC · R_WBᵀ`）→ OpenCV 畸变投影 → 近平面与画幅裁剪 → 对 AABB 方块做 slab 射线判定 → 把可见特征以 `(2r+1)²` 的方块"溅射"到图像。批量版用 `scatter_reduce(amax)` 选出每个像素"特征序号最大"的写者后一次 `index_put_`，因此**同一像素上的胜者由特征在数组中的顺序决定，而不是深度**。背景是纯色（默认 0.9 灰）。

**几何来源。** `world_features.py` 的生成器注册表：`regular`（面网格）、`random` / `density`（按面密度随机撒点）、`edge_uniform`、`edge_random`；或在世界 JSON 的 `features` 段中直接嵌入 `points / colors / descriptors / blocks`（本文即用此方式把 GLB 表面采样点交给 RotorPy）。特征颜色默认取所属方块颜色加高斯抖动；描述子是 L2 归一化的随机向量（模拟 SIFT / ALIKED），只透传、不参与成像。

**内参与畸变。** `intrinsics = {fx, fy, cx, cy, width, height, dist_coeffs=[k1,k2,p1,p2,k3]}`，OpenCV 径向 + 切向前向模型直接作用于点（点投影不需要逆畸变）；像素坐标按 `round()` 取整，即**像素中心为整数**，与 D2 / COLMAP 的 0.5 约定差半个像素。外参 `extrinsics = {position, orientation}` 为机体到相机的平移与四元数 `[x,y,z,w]`。批量版支持逐机内外参、近平面、溅射半径、噪声参数（域随机化），`randomize_camera_params()` 按种子批量采样；分辨率须全批共享。

**帧率与同步。** 相机只记录 `frame_rate`；`simulate()` 在每个物理步末检查 `t ≥ next_camera_time`，采样后令 `next_camera_time = t + 1/frame_rate`。因此帧时刻必然落在物理步网格上，且周期被量化为 `ceil(T/dt)·dt`（实测见 §3.2）。帧对应步末瞬时位姿，没有曝光时间、运动模糊、卷帘快门、触发延迟。

**输出格式。** `image (H,W,3) float32 [0,1]`、`keypoints (M,2)`、`keypoint_depths (M,)`、`visible_features (M,3)`、`visible_mask (N,)`、`projected (N,2)`、`depth (N,)`、`colors`、`descriptors` 及其可见子集；`feature_output ∈ {all, rgb, descriptors}` 控制返回体积。`simulate()` 合并为 `uint8` 图像栈与逐帧列表，`Environment.save_camera_data()` 存为 `npz`（可选 PNG）。注意这里的 `depth` 是**逐特征**的相机系 z，不是深度图。

**与视觉算法的接口。** 对 VO / VIO 而言，RotorPy 相机直接给出"像素坐标 ↔ 世界坐标 ↔ 描述子"的真值对应与可见性，等价于理想特征检测与匹配，便于把估计器与前端解耦测试；`visual_servoing_policy.py` 则演示了两种用法：闭环时在控制器 `update()` 内按相机帧率渲染并维护图像历史（CNN + MLP 输出 `cmd_v`），离线时先跑 `simulate_batch` 记录状态、再按帧率从日志位姿**重渲染**（"不让相机拖慢动力学"）。后一种"物理先行、渲染离线"的分工与 D3 的批量数据集生产一致。

**噪声。** `noise_params = {feature_rate, splat_radius, intensity, seed}`：每帧按 Poisson 注入随机色块（模拟伪特征），只改图像，不改真值数组。没有光度噪声、曝光、模糊、压缩伪影。

**缺陷与限制（v3.0.0，已在本单元复现）。**

| 编号 | 位置 | 现象 | 影响 / 规避 |
|---|---|---|---|
| C1 | `camera.py` `_render_shared_world`（约 1347–1359 行） | `not_occluded = torch.zeros_like(base)`，仅当 `blocks` 非空时才填值；共享世界无方块时 `visible_mask` 全 False，图像为纯背景 | 单机 numpy 版与逐机世界路径初始化为全 True，行为不一致。规避：放一个远处哑块；建议向上游提交修复 |
| C2 | `simulate.py` `capture_camera` | 帧时刻 = 实际采样时刻 + 周期，量化到物理步 | 30 Hz@100 Hz 实为 25 Hz；数据集帧率与标称不符 |
| C3 | `simulate_batch` | 全部帧（GPU 张量）留在列表中直到仿真结束再合并 | 长时、多机采集会耗尽显存与内存；需流式落盘 |
| C4 | `BatchedPinholeCamera.render` | 每次调用对世界特征执行 `torch.tensor(np.asarray(...))`，整表上传 | 1,600 万点时单次调用 113 ms，其中包含整表类型转换与主机到设备传输（未单独拆分计时）；应改为世界常驻显存 |
| C5 | 遮挡 | 逐机 `for b in range(B)` × 逐块 `for block` 的 Python 双循环 | 成本 ∝ B × 块数，批量无收益 |
| C6 | `examples/camera_demo.py` | 安装四元数为上仰 45°（注释写明 "slight upward tilt"）；下视需改为 `+45` | 示例照搬时注意 |

### 2.4 Gym 环境与批量向量化

- `QuadrotorEnv` 继承 SB3 `VecEnv`，内部是 `BatchedMultirotor(integrator="rk4")`，动作空间按控制抽象缩放到 [−1, 1]，观测 13 维，奖励函数可替换，`reset_options` 支持随机初始状态与参数。没有相机观测，视觉策略需包装层（我方相机的 `BatchedPhotorealCamera.render(states)` 可直接作为观测函数）。
- `simulate_batch` 把动力学、控制、轨迹、IMU 全部张量化，并支持逐机世界与逐机终止；不支持动捕与估计器。README 称 CPU 上 >1000 架机相对串行约 25 倍加速。本文实测（§3.8）在 GPU 上每步约 20–36 ms、CPU 上约 16–20 ms，且与机数基本无关，说明在数百架机以内的规模下由 Python 与内核启动开销支配。
- `examples/parallel_data_collection.py` 用 `multiprocessing` 跑多个单机仿真采数据，是另一条横向扩展路线。

### 2.5 许可

RotorPy 为 MIT，可直接在产品中移植代码（保留版权声明）。其可选依赖：stable-baselines3（MIT）、torchdiffeq（MIT）、roma（BSD-3）、foundation-policy（RAPTOR，需单独确认许可后再进入产品依赖）。

---

## 3. 实测

### 3.1 环境与方法

- 硬件：dmax GPU 2（A40 48 GB），容器 CPU 上限 16 核。实验 1 的 GPU 部分在 GPU 3 上执行，期间该卡有 r3 单元的 3DGS 训练任务并行，GPU 数值可能偏高；量级与趋势不受影响。实验 1c、2、3、4 均在 GPU 2 上执行。
- 计时：`torch.cuda.synchronize()` 包围，取中位数；批量吞吐 = 批量 / 单次调用耗时。
- 小镇场景：1,996,759 三角形、3,087,397 顶点、16 张 4096² 纹理拼为 16384² 图集（sRGB → 线性），包围盒约 535 m × 320 m × 28 m；GLB 解析与 JPEG 解码约 6 s，缓存为 npz 后 1.8 s；上卡后显存 4.1 GB。
- 相机位姿：在场景中心 60% 区域内随机取点，高于局部最高点 25–70 m，偏航随机，相机前视下俯 45°，水平视场 84°。

### 3.2 RotorPy 帧率量化（实验 1b，`simulate()`，2 s）

| 物理频率 | 请求帧率 | 实际帧间隔 | 实际帧率 |
|---|---|---|---|
| 100 Hz | 10 / 25 Hz | 100 / 40 ms | 10 / 25 Hz |
| 100 Hz | 30 Hz | 40 ms | **25 Hz** |
| 100 Hz | 40 Hz | 30 ms | **33.3 Hz** |
| 100 Hz | 60 Hz | 20 ms | **50 Hz** |
| 250 Hz | 30 Hz | 36 ms | **27.8 Hz** |
| 250 Hz | 40 Hz | 28 ms | **35.7 Hz** |

### 3.3 RotorPy 相机速度（实验 1a，ms/帧，A40 / 16 核）

场景：60 m × 60 m 内随机立柱（2 m × 2 m × 4–12 m），特征按面密度随机撒布；"0 块"时以一块地板承载特征。

| 遮挡块 | 特征点 N | numpy 单机 | torch CPU B=1 | CUDA B=1 | CUDA B=16 | CUDA B=64 |
|---|---|---|---|---|---|---|
| 0 | 14.5 万 | 36.4 | 18.4 | 4.2 | 1.27 | 1.15 |
| 0 | 145 万 | 468 | 49.0 | 10.0 | 4.75 | 4.67 |
| 50 | 7.3 万 | 415 | 30.5 | 18.9 | 16.7 | 15.8 |
| 50 | 73 万 | 4,569 | 103.5 | 20.4 | 16.7 | 16.0 |
| 200 | 29 万 | 7,535 | 213 | 61.7 | 59.0 | 53.1 |
| 400 | 57 万 | 27,043 | 500 | 142.5 | 143.9 | 137.7 |

（640×480；1280×720 的数值与之相差约 ±20%，分辨率不是主要成本。GPU 数值受同卡并行负载影响，见 §3.1。）

### 3.4 画质：同一位姿下三种表示的对比（实验 2 quality，1280×720，4 个视角）

参考图为 GLB 网格光栅化（无畸变、无 ISP，4.5 ms/帧）；指标只在参考图有覆盖的像素上统计。RotorPy 使用 GLB 表面面积加权采样点（颜色取纹理），3DGS 用同一批采样点初始化为贴合法向的扁平面元（**未经训练**，仅用于验证管线与吞吐）。

| 表示 | 点 / 高斯数 | ms/帧 | PSNR（dB） | SSIM |
|---|---|---|---|---|
| RotorPy，溅射半径 0 | 1M / 4M / 16M | 11.0 / 34.7 / 117 | 6.8 / 7.7 / 9.9 | 0.01 / 0.04 / 0.14 |
| RotorPy，溅射半径 1 | 1M / 4M / 16M | 9.7 / 35.6 / 113 | 8.8 / 12.4 / 18.5 | 0.07 / 0.21 / 0.42 |
| 3DGS 面元（gsplat，未训练） | 1M / 4M / 16M | 1.1 / 2.7 / 7.6 | 20.8 / 22.7 / 24.0 | 0.47 / 0.56 / 0.65 |
| 3DGS 期望深度相对误差（中位 / P90） | 1M / 4M / 16M | — | 0.46% / 0.24% / 0.16% | P90 1.45% / 0.86% / 0.57% |

样张：`results/exp2_quality_crops.jpg`（全分辨率局部裁切）、`results/exp2_quality_compare.jpg`。RotorPy 路径已用远处哑块规避 C1。1M 点时像面覆盖率极低，图像几乎全黑；16M 点、半径 1 时可辨认街区，但存在空洞、颗粒，以及因无深度排序而出现的远近串色。作为对照，r3 单元以网格渲染为监督训练的 3M 高斯 3DGS 在训练分布视角达到 33.6 dB / SSIM 0.95，低空与超低空视角为 29.5 / 26.9 dB（`.cache/research/d3/r3-camera-render/results/gs/town_eval.json`），说明照片级相机的画质上限由资产决定，而非渲染范式本身。

### 3.5 照片级相机原型：网格光栅化吞吐（实验 2 thr，fps，括号内为 ms/帧）

输出为 RGB + 平面深度 + 实例 + 语义（同一次光栅化）。模式：`raw` 无畸变无 ISP、数据留在 GPU；`isp` 含 OpenCV 畸变（k1 = −0.12、k2 = 0.03，先渲染像素数约为输出 1.27 倍的针孔图再重映射）、雾、渐晕、自动曝光、Poisson-Gaussian 噪声、sRGB；`isp+rb` 再加 uint8 图像、float16 深度、int16 实例回读到主机；`isp+jpeg` 改为 nvjpeg GPU 编码后回读码流与 float16 深度；`blur4` 为曝光窗口内 4 子帧运动模糊（含 ISP）。

| 分辨率 | B | raw | isp | isp+rb | isp+jpeg | blur4 | 峰值显存 GB |
|---|---|---|---|---|---|---|---|
| 640×480 | 1 | 108（9.30） | 99（10.09） | 93（10.70） | 88（11.41） | 27（37.25） | 4.4 |
| 640×480 | 8 | 213（4.69） | 180（5.54） | 183（5.46） | 181（5.51） | 50（19.91） | 5.2 |
| 640×480 | 32 | 235（4.25） | 212（4.73） | 203（4.92） | 196（5.10） | 54（18.61） | 8.0 |
| 640×480 | 64 | 234（4.28） | 199（5.03） | 167（5.98） | 191（5.25） | 53（18.77） | 11.6 |
| 640×480 | 128 | 229（4.36） | 204（4.90） | 161（6.19） | 184（5.45） | 57（17.67） | 18.9 |
| 1280×720 | 1 | 99（10.11） | 82（12.19） | 82（12.22） | 80（12.49） | 24（42.23） | 4.6 |
| 1280×720 | 8 | 146（6.85） | 120（8.31） | 110（9.12） | 112（8.89） | 33（30.31） | 6.2 |
| 1280×720 | 32 | 148（6.78） | 125（7.97） | 81（12.33） | 101（9.90） | 34（29.50） | 11.6 |
| 1280×720 | 64 | 148（6.76） | 120（8.31） | 74（13.58） | 102（9.77） | 31（31.92） | 18.7 |

- JPEG（质量 90）平均 104–123 kB/帧（640×480）、301–360 kB/帧（720p）。
- 720p 大批量下 `isp+rb` 明显低于 `isp+jpeg`：未使用锁页内存的同步 `.cpu()` 只有约 0.5 GB/s；生产实现应使用锁页缓冲 + 异步拷贝，或直接在 GPU 上编码。
- 样张：`results/exp2_thr_*_{raw,isp,blur4}.jpg`、深度与语义 `results/exp2_aux_*`。

### 3.6 3DGS 路径吞吐（实验 2 gs，gsplat `rasterization(..., render_mode="RGB+ED", packed=True)`）

| 高斯数 | 分辨率 | B=1 | B=8 | B=32 | B=64 | 峰值显存 GB（B=64） |
|---|---|---|---|---|---|---|
| 4M | 640×480 | 431 | 424 | 320 | 334 | 12.7 |
| 4M | 1280×720 | 432 | 481 | 318 | 337 | 11.5 |
| 16M | 640×480 | 129 | 124 | 92 | 96 | 37.3 |
| 16M | 1280×720 | 150 | 156 | 101 | 108 | 29.6 |

"3DGS 背景 + 网格动态实体（3 人 1 车 1 犬，程序化代理网格）按深度合成 + 畸变 + ISP"，1280×720、B=8：4M 高斯 184 fps，16M 高斯 124 fps（`results/exp2_gs_composite_N*.jpg`）。高斯数决定成本，分辨率影响很小；B ≥ 32 时吞吐反而低于 B = 8，3DGS 路径的最佳批量约为 8。

### 3.7 分阶段耗时与优化空间（实验 3）

nvdiffrast CUDA 光栅化，小镇 2.0M 三角形，单位 ms/批（不含动态实体、天空与 ISP）：

| 阶段 | 640×480，B=32 | 1280×720，B=8 |
|---|---|---|
| 顶点变换（B × 309 万顶点） | 7.5 | 2.0 |
| 光栅化（含屏幕导数） | 91.2 | 25.4 |
| UV 插值 | 1.0 | 0.7 |
| 纹理（三线性 mip） | 3.8 | 1.6 |
| 深度插值 | 0.5 | 0.3 |
| 实例 / 语义查表 | 0.6 | 0.4 |
| 合计（每帧） | 104.7（3.27） | 30.4（3.80） |
| 像面覆盖率 | 71% | 56% |

- **光栅化占 85% 以上**，且与三角形数线性相关：随机保留 100% / 50% / 25% 三角形时光栅化为 107 / 55 / 28 ms（640×480，B=32）。纹理、插值、深度、标签合计不足 10%。
- 图集边长 16384 / 8192 / 4096 对总耗时无影响（纹理阶段 1.1–3.9 ms），显存分别为 3.0 / 0.75 / 0.19 GB——图集可降到 8192² 而不损失吞吐，换取多世界同时驻留。
- 逐相机视锥内三角形占比：均值 31%，P10 8%，P90 64%（640×480，84° 视场，64 个随机位姿）。以分块视锥裁剪 + LOD 只提交可见三角形，光栅化阶段有约 3 倍的理论收益。
- nvdiffrast 0.4.0 已移除 OpenGL 光栅器：`RasterizeGLContext` 仅为 `RasterizeCudaContext` 的弃用别名（实测两者耗时一致）。r3 单元同场景的 moderngl（EGL / OpenGL 硬件光栅化，MRT 一次输出 RGB + 线性 z + 实例号，含 `glReadPixels`）为 1280×720 单相机 1.37 ms/帧（729 fps），而 nvdiffrast 单相机为 5.9 ms/帧（`.cache/research/d3/r3-camera-render/results/gl/town_moderngl.json`、`nv/town_full_bench.json`）。在几何受限的航拍场景中，硬件光栅化相对 CUDA 软件光栅化约有 4 倍优势，这也是 §4.6 产品化路线的主要依据。

### 3.8 端到端闭环（实验 1c、实验 2 e2e）

**(a) RotorPy 原生链路：`simulate_batch` + `BatchedPinholeCamera`**（实验 1c；SE3 悬停，恒定风 2 m/s，100 Hz，2 s；相机 640×480、15 Hz，世界含 50 个遮挡块与 7.3 万特征点）

| 设备 | B | 无相机：墙钟 s（机·秒 / 墙钟秒） | 有相机：墙钟 s（机·秒 / 墙钟秒） |
|---|---|---|---|
| CUDA | 16 | 7.2（4.5） | 19.2（1.7） |
| CUDA | 64 | 6.3（20.2） | 57.7（2.2） |
| CUDA | 256 | 6.8（75.5） | 200.2（2.6） |
| CPU（16 核） | 16 | 3.2（10.1） | 26.2（1.2） |
| CPU（16 核） | 64 | 3.2（40.4） | 97.1（1.3） |
| CPU（16 核） | 256 | 3.9（130.2） | 507.8（1.0） |

- 动力学：`simulate_batch` 每步 GPU 约 32–36 ms、CPU 约 16–20 ms，几乎不随机数变化——在 ≤ 256 架机的规模下由 Python 与内核启动开销决定，CPU 反而更快。
- 原生相机使吞吐下降 3–130 倍，原因是 §3.3 的"逐机 × 逐块"遮挡循环；在有遮挡物的世界中，RotorPy 原生相机不是"快速"相机。

**(b) RotorPy GPU 动力学 + 照片级相机原型**（实验 2 e2e；`px4_sihsim_quadx`，`BatchedMultirotor(rk4)` + `BatchedSE3Control` 手动步进，圆轨迹 4–8 m/s、风 (3, 1, 0) m/s；相机含畸变与 ISP，曝光 8 ms，物理步间插值位姿；每帧 nvjpeg 编码并回读码流与 float16 深度；3 s 仿真）

| B | 相机 | 墙钟 s | 实时倍率 | 相机帧 / 墙钟秒 | 物理 ms/步 | 相机 ms/触发 | 编码回读 ms/触发 | 墙钟构成 |
|---|---|---|---|---|---|---|---|---|
| 16 | 640×480，15 Hz | 10.0 | 0.30 | 72 | 21.2 | 76.6（4.8/帧） | 4.1 | 物理 64%，相机 35% |
| 64 | 640×480，15 Hz | 21.5 | 0.14 | 134 | 21.8 | 296.9（4.6/帧） | 35.3 | 物理 30%，相机 + 回读 70% |
| 16 | 1280×720，30 Hz | 17.8 | 0.17 | 81 | 19.4 | 126.1（7.9/帧） | 6.8 | 物理 33%，相机 + 回读 67% |
| 64 | 1280×720，10 Hz | 23.9 | 0.13 | 80 | 21.6 | 491.1（7.7/帧） | 90.8 | 物理 27%，相机 + 回读 73% |

- 小批量时动力学是瓶颈（约 21 ms/步，等于 100 Hz 实时预算的两倍）；大批量时相机成为瓶颈，此时单 A40 的稳态能力约为 640×480 每秒 190 帧、720p 每秒 100 帧（含编码与回读）。
- 15 Hz、30 Hz 的触发时刻不落在 100 Hz 物理网格上：若沿用 RotorPy"取最近物理步"的做法，平均时间误差 2.2 ms（10 Hz 时为 0）；原型按插值位姿渲染，误差为 0。
- 由此得到实时容量（相机侧）：640×480、15 Hz 约 12 架机，720p、15 Hz 约 7 架机；超过即需多卡或离线模式。动力学侧需换为 FleetSim SoA（n03 §3.10）或为 RotorPy 批量步录制 CUDA Graph 后才能达到 100 Hz 实时。

### 3.9 Rerun 记录（实验 4）

rerun-sdk 0.38.1；4 架机绕小镇中心飞行 4 s，相机 640×480、10 Hz，共 160 帧；每帧记录相机位姿、JPEG 码流（`EncodedImage`，不在 CPU 解码）、毫米深度（`DepthImage` uint16）、语义（`SegmentationImage`）、轨迹线；事件真值实体每触发记录一次 `Boxes3D`；场景参考点云 30 万点（静态）。

| 指标 | 数值 |
|---|---|
| 记录开销 | 每次触发（4 帧 + 真值）中位 5.2 ms，即每帧 1.3 ms（Python 端调用耗时，写盘由 SDK 后台线程完成） |
| 载荷 | 原始载荷 161.6 MB（JPEG + uint16 深度 + uint8 语义） |
| `.rrd` 文件 | 87.6 MB（dmax：`/data/anet-drone4d-d3/r10-rotorpy/out/r10_camera_demo.rrd`，可直接用 Rerun Viewer 打开） |

注意事项：应用 ID 不能含点号（`drone4d.r10.photoreal_camera` 被自动迁移为 `drone4d-r10-photoreal_camera7400`），产品中应使用连字符命名；0.38 依赖 numpy ≥ 2，本实验以 `--target` 隔离安装，避免影响其余基准的 numpy 1.26 环境。

### 3.10 作为 RotorPy 相机直接接入（实验 2 dropin）

`BatchedPhotorealCamera(rotorpy_compat=True)` 传入 `simulate_batch(camera=...)`：8 架 `px4_sihsim_quadx` 绕圈、2 s、10 Hz，得到 `camera_measurements['image'].shape == (21, 8, 480, 640, 3)`，帧时刻 0.0、0.1、…、2.0 s。兼容层只需补齐 RotorPy 合并函数要求的逐特征键（空数组），深度图另存为 `depth_map`。

---

## 4. 在 D3 中的采用方式与接口设计

### 4.1 RotorPy 在 D3 中的角色

| 用途 | 方式 | 说明 |
|---|---|---|
| 动力学参考与回归基准 | 移植气动力矩与 Dryden 到 FleetSim（n03 已定），用 RotorPy 单机 `Multirotor` 生成对拍轨迹 | 不把 RotorPy 作为 FleetSim 的运行时依赖 |
| 视觉算法开发宿主 | 研究者可在 RotorPy 中直接使用我方照片级相机（同签名、可作为 `simulate_batch` 的 `camera`） | 降低算法团队的接入门槛；Gym 向量环境只需加观测包装 |
| 特征级快速档位（T0） | 保留 RotorPy 式"特征点投影"作为最快一档，但特征点由我方深度图与位姿派生（§4.4），不再依赖 AABB 世界 | 用于 VO 前端解耦测试、控制回归、CI |
| HIL 参考 | `PX4Multirotor` 的 MAVLink lockstep 实现作为 FleetSim HIL 桥的对照 | 见 n03 §3.6 |

### 4.2 快速照片级相机传感器：目标与指标

| 指标 | 目标（产品版，单 A40） | 现状（研究原型实测） | 说明 |
|---|---|---|---|
| 640×480 RGB + 深度 + 实例 + 语义，含畸变与 ISP，批量 ≥ 32 | ≥ 400 fps | 199–212 fps | 网格路径；优化项见 §4.6 |
| 1280×720 同上 | ≥ 250 fps | 120–125 fps | r3 的 moderngl 单相机 720p 已达 729 fps（不含 ISP），目标留有余量 |
| 3DGS 着色 + 网格深度判遮挡 + 动态前景，720p | ≥ 200 fps（≤ 8M 高斯） | 4M：184 fps；16M：124 fps（原型以 3DGS 期望深度判遮挡） | 需增加一遍网格深度（GL 路径代价约 1 ms/帧） |
| 运动模糊（4 子帧） | ≥ 1/3 单帧吞吐 | ≈ 1/4 | 子帧可降分辨率渲染后合成；高空平移时模糊亚像素级，主要作用于机体旋转与低空 |
| 端到端（物理 + 相机 + 编码落盘） | 16 机 × 15 Hz × 720p 实时（240 fps） | 相机侧：640×480 约 12 机、720p 约 7 机实时；动力学侧未达 100 Hz 实时（§3.8） | 物理换 FleetSim SoA、渲染换硬件光栅化后复测 |
| 时间对齐 | 帧时间戳 = 曝光中点，误差 ≤ 1 µs（整数纳秒）；位姿取物理步间插值 | 已实现 | 不再受 C2 量化影响 |
| 可复现 | 同一（世界版本，事件磁带，种子，相机配置）逐帧几何一致；噪声按计数器式随机数可复现 | 几何一致；噪声尚用 `torch.randn` | 接入 D2 `cbrng` |

容量估算：一段 300 s 事件、10 架机、15 Hz、720p 共 45,000 帧；按 100 fps（含编码落盘）计算，单卡 7.5 分钟完成一次策略回放的全部渲染；GPU 2、3 两卡可并行两次回放。

### 4.3 总体结构

```
宿主适配层                     相机服务（同进程或 GPU Worker）                         渲染后端
┌───────────────────┐        ┌──────────────────────────────────────────┐        ┌──────────────────────────┐
│ RotorPy           │ states │ PoseHistory（物理步位姿环形缓冲，GPU）      │  view  │ MeshRaster：GLB + 图集     │
│  render/measurement├──────►│ TriggerClock（绝对网格，曝光中点）          ├───────►│  （产品：EGL / Vulkan；     │
│ FleetSim / D2     │ request│ 子帧位姿插值（lerp + slerp）                │        │   研究：nvdiffrast）       │
│  SensorBackend    ├──────►│ 实体位姿 ← 事件磁带（同一时刻）               │        ├──────────────────────────┤
│ gz-sim            │ post_  │ 批处理器：按（世界版本，后端，分辨率）合批     │        │ Hybrid：3DGS 着色（gsplat）│
│  Python System    │ update │ 效果链：畸变重映射 → 雾 / 雨 → 渐晕 →         │◄───────┤  + 网格深度判遮挡与前景    │
└───────────────────┘        │        曝光 → 噪声 → 色调 → 编码              │ layers ├──────────────────────────┤
                             │ 输出：CameraFrame → 落盘（MCAP / rrd / 图像）│        │ Gazebo Ogre2（参考档位）    │
                             └──────────────────────────────────────────┘        └──────────────────────────┘
```

### 4.4 相机模型与帧契约

坐标与时间约定沿用 D2 M13：世界 ENU、机体 FLU、光学帧 RDF、四元数 `[x,y,z,w]`、像素中心 0.5、整数纳秒。从 RotorPy 内参导入时 `cx, cy` 各加 0.5。

```python
@dataclass(frozen=True)
class CameraModel:
    width: int; height: int
    fx: float; fy: float; cx: float; cy: float          # 像素中心 0.5 约定
    dist: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0)   # OpenCV k1 k2 p1 p2 k3
    near_m: float = 0.3; far_m: float = 2000.0
    fps: float = 15.0
    exposure_s: float = 0.008                            # 帧时间戳 = 曝光中点
    readout_s: float = 0.0                               # 卷帘读出时长（0 = 全局快门）
    blur_subframes: int = 1
    isp: IspConfig = IspConfig()                         # 自动曝光、EV、色调、gamma、JPEG 质量
    noise: NoiseConfig = NoiseConfig()                   # Poisson-Gaussian：shot_k、read_sigma；计数器式随机数

@dataclass
class CameraFrame:
    uav_id: str; sensor_id: str
    t_mid_ns: int; exposure_ns: int; frame_index: int
    T_world_cam: np.ndarray        # 4×4，ENU ← RDF
    K: np.ndarray; dist: tuple
    rgb: bytes | np.ndarray        # JPEG 码流或 uint8 H×W×3（sRGB）
    depth: np.ndarray | None       # 平面深度（光轴 z），float32 m 或 uint16 mm；无覆盖为 0
    instance: np.ndarray | None    # uint16，0 = 背景 / 静态场景
    semantic: np.ndarray | None    # uint8，类别表与 D2 targets 一致
    features: FeatureTrack | None  # 可选：由深度 + 位姿派生的特征真值（RotorPy 语义：keypoints / visible_features / depth）
    meta: dict                     # world_version、asset_hash、backend、renderer_version、seed
```

`features` 字段把 RotorPy 相机对 VO 的价值保留下来：在图像上取角点或网格点，用深度反投影得到世界坐标真值与可见性，输出与 RotorPy `keypoints / keypoint_depths / visible_features` 同义的数组，但图像是照片级的。

### 4.5 时间对齐

1. 触发：`t_k = t0 + k · round(1e9 / fps)`（整数纳秒，绝对网格），不从实际采样时刻起算，避免 C2 的量化与漂移。
2. 时间戳：帧时间戳取曝光中点；曝光窗口 `[t_k − e/2, t_k + e/2]` 内的 `blur_subframes` 个子帧各自采样位姿与事件实体位姿；卷帘时第 j 个子帧只贡献第 j 条行带。
3. 位姿来源：物理每步把 `(t, x, q)` 推入 GPU 环形缓冲；任意时刻用相邻两步的线性插值与球面插值求位姿。15 Hz 与 30 Hz 在 100 Hz 物理下"取最近步"会带来平均 2.2 ms 的时间误差，插值后为 0。
4. 何时可渲：曝光结束时刻 ≤ 物理已推进时刻即可渲染；与 D2 "请求在步内发出、结果在下一步顶部锁存" 的语义兼容。
5. 事件一致性：动态实体的位姿取自事件磁带同一时刻（`actor_source(t)`），确保 RGB、深度、实例、LiDAR 对应同一世界状态。

### 4.6 渲染路径 A：GLB 网格光栅化

- 资产：`appearance.glb` → 三角形、UV、材质图集（sRGB 转线性，生成 mip）；实例 / 语义按三角形查表（静态场景按 r3 的地面 / 立面 / 高架分类，动态实体按实例号）。
- 每批：`clip = P · V_b · X`（B 台相机共享几何）→ 光栅化 → 插值 UV 与屏幕导数 → 三线性 mip 采样 → 深度插值 → 实例 / 语义查表；动态实体与静态场景进入同一 z-buffer。
- 已实现的效果：OpenCV 畸变（针孔放大画幅渲染 + 预计算重映射，RGB 双线性、深度与标签最近邻；渲染像素比上限 1.35，超出时等比降低渲染焦距）、Koschmieder 雾、cos⁴ 渐晕、自动曝光（EMA）、Poisson-Gaussian 噪声、sRGB、天空 / 远景渐变、运动模糊、卷帘（行带）。
- 优化路线（按收益排序，依据 §3.7）：逐相机视锥裁剪与分块 LOD（小镇视角平均只覆盖部分三角形）、图集降为 8192² 或改为按材质分页纹理、CUDA Graph 固化每批调用、锁页内存异步回读与 GPU 编码、深度与标签按需输出。
- 产品化：nvdiffrast 许可不允许商用，且 0.4.0 已无 OpenGL 光栅器。产品版本采用 EGL 上的 OpenGL（moderngl，MIT；或原生 C++），以 MRT 一次输出 RGB、线性深度与实例号：r3 实测小镇 720p 单相机 1.37 ms/帧（含 `glReadPixels`），与 nvdiffrast 深度差中位 15 µm、覆盖 IoU 1.0、像素偏移 < 0.02 像素，可视为几何等价。批量化方式为单上下文多帧缓冲轮转或纹理数组分层渲染；畸变重映射与 ISP 仍在 CUDA 上执行（通过 CUDA-GL 互操作共享纹理，避免回读）。nvdiffrast 保留为研究基准与回归对拍工具。

### 4.7 渲染路径 B：3DGS 与动态前景合成

- 静态背景：训练好的 3DGS（PLY，r3 单元产出）按世界版本加载；原型中以 GLB 面元初始化代替，仅用于吞吐与接口验证。
- 批量：gsplat `rasterization(means, quats, scales, opacities, colors, viewmats[B], Ks[B], W, H, render_mode="RGB+ED", packed=True)`；期望深度在不透明度累计 ≥ 0.5 处有效。
- 动态前景：同一批相机用网格路径渲染动态实体与静态网格深度。原型以 3DGS 期望深度作遮挡判据（720p、B=8 时 124–184 fps）；r3 单元的 60 视角对照表明，该判据会产生 14.3% 的假可见像素（实体 IoU 0.89），改用**网格 z-buffer 判遮挡、3DGS 只提供颜色**后与联合真值一致（IoU 0.9997，假可见 0.009%），且合成后 PSNR 更高（29.4 dB 对 28.7 dB，`.cache/research/d3/r3-camera-render/results/comp/town_composite.json`）。产品版采用后者：深度、实例、语义、遮挡全部来自网格，3DGS 只替换静态像素的颜色。
- 待解决：高斯边缘的半透明与前景硬边的混合（建议以 alpha 与深度联合合成）、实体阴影与地面接触（简易方案：投影圆盘暗化）、动态实体的重光照一致性（以 3DGS 背景的局部平均亮度调制前景）。

### 4.8 传感器效果链

顺序：几何（含畸变重映射）→ 大气（雾、雨幕）→ 光学（渐晕）→ 曝光（自动曝光 / EV、运动模糊在线性域累积）→ 传感器噪声（Poisson-Gaussian，计数器式随机数按 `(run_seed, uav_id, sensor_id, frame_index)` 取流）→ ISP（色调、gamma）→ 量化与编码。雨在 D3 事件中随时间变化：降雨强度 → 消光系数与雨线屏幕层密度，同时提供给 LiDAR 单元（r4）的衰减模型，保证两类传感器使用同一环境时间线。

### 4.9 Python API 与宿主适配

```python
from drone4d.sensors.camera import CameraModel, CameraRig, PhotorealCamera, RenderService

cam = PhotorealCamera(
    world="smalltown-01@3",                 # World Package 版本（appearance.glb / appearance.3dgs / anchors）
    backend="mesh",                         # "mesh" | "gs" | "hybrid"（3DGS 背景 + 网格前景）| "feature"（T0）
    model=CameraModel.from_hfov(1280, 720, 84.0, fps=15, exposure_s=0.008, dist=(-0.12, 0.03, 0, 0, 0)),
    rig=CameraRig(mount_t=(0.10, 0.0, -0.05), mount_rpy_deg=(0.0, 45.0, 0.0)),   # FLU，下俯 45°
    outputs=("rgb", "depth", "instance", "semantic"),
    event_tape=tape,                        # 动态实体位姿 f(t_ns)
)
frames = cam.capture(t_mid_ns, pose_fn)     # pose_fn(t_ns) -> (x[B,3], q[B,4])；返回 list[CameraFrame]
```

| 宿主 | 适配方式 | 状态 |
|---|---|---|
| RotorPy | `BatchedPhotorealCamera.render(worlds, states)` / `measurement(...)` 与 `BatchedPinholeCamera` 同签名；`frame_rate` 属性供 `simulate_batch` 抽帧；单机版包装为 `PinholeCamera` 同签名 | 原型已在 `simulate_batch` 中运行 |
| FleetSim / D2 | 实现 `SensorBackend`：`open(world, rigs)`、`request(slots, poses_enu, t_sim_ns) -> int`（非阻塞，提交到 GPU Worker）、`poll() -> list[SensorFrame]`（下一步顶部锁存，`payload` 携带 `CameraFrame`） | 原型提供同名方法（同步实现） |
| gz-sim（Harmonic） | gz-sim 的 `PythonSystemLoader` 插件（`src/systems/python_system_loader`）在 `post_update(info, ecm)` 中读取各无人机模型世界位姿与 `info.sim_time`，推入 `PoseHistory` 并提交到期帧；lockstep 时阻塞至渲染完成，非 lockstep 时异步。gz 原生 Ogre2 相机保留为参考档位，用同一帧契约输出 | 设计；r9 单元的 gz-sim 实测确认插件加载与实时因子后实施 |
| Gym / 学习 | `VecEnv` 观测包装：每 `k` 个物理步调用一次 `render(states)`，返回 `(B, H, W, 3)` 张量（不出 GPU） | 设计 |

### 4.10 批量渲染服务化（dmax GPU Worker）

- 作业：`RenderJob{job_id, world_version, backend, model_hash, items: [(uav_id, sensor_id, t_mid_ns, pose_samples, actor_samples)], outputs, sink}`；位姿与实体位姿由调用方在提交时给定，Worker 无状态、可重放。
- 合批：Worker 常驻加载世界资产（每世界每卡一次，小镇约 4 GB 显存，图集降为 8192² 后约 1.1 GB），按（世界版本，后端，分辨率，输出集合）聚合。实测吞吐在 640×480 约 B = 32、720p 与 3DGS 约 B = 8 时饱和，更大批量只增加显存与延迟；不满批时最多等待 20 ms。
- 资源：仅用 GPU 2、3，每卡一个 Worker 进程、两个 CUDA 流（渲染与编码 / 回读重叠）；容器 `d3-<单元>`、CPU ≤ 16。
- 背压：队列深度上限按"每卡吞吐 × 2 s"设置；超出时 `request()` 返回拒绝码，FleetSim 降低相机频率或进入离线模式。
- 落盘：原生时序记录为 MCAP（与 D2 录制一致），图像 JPEG / PNG、深度 PNG16（毫米）、标签 PNG；同时可写 Rerun `.rrd` 供研发浏览。
- 离线批量生产（数据集）：沿用 RotorPy 示例中"物理先行、离线重渲染"的分工——先以 Fast 档位跑完一次策略回放并保存位姿与事件磁带，再把渲染作业整批投递，渲染与仿真彼此解耦、可单独重跑。

### 4.11 Rerun 融入

记录模式（已在实验 4 验证）：

实验 4 验证了下列原型（archetype）的组合与开销；实体路径按 r8 单元（`.cache/research/d3/r8-rerun/scripts/d3rr.py`）的约定统一，实验脚本中的 `world/uav{id}/cam`、`gt/actors` 在产品中分别对应 `/fleet/{uav}/cam0`、`/truth/actors`。

| 实体路径（产品） | 原型 | 说明 |
|---|---|---|
| `/world` | `ViewCoordinates.RIGHT_HAND_Z_UP`；`/` 上挂 `AnnotationContext`（静态） | 语义类别表 |
| `/world/basemap` | `Points3D`（静态） | 场景参考 |
| `/fleet/{uav}` | `Transform3D`（机体位姿，每物理记录帧） | 与 r8 一致 |
| `/fleet/{uav}/cam0` | `Transform3D`（机体 → 光学帧，云台变化时更新）、`Pinhole(image_from_camera=K, camera_xyz=RDF)`（静态） | 相机外参与内参 |
| `/fleet/{uav}/cam0/rgb` | `EncodedImage(media_type="image/jpeg")`，或 r8 采用的 `VideoStream(H264)` | 直接写 GPU 编码码流，不在 CPU 解码 |
| `/fleet/{uav}/cam0/depth` | `DepthImage(uint16, meter=1000)` | 毫米 |
| `/fleet/{uav}/cam0/instance`、`semantic` | `SegmentationImage` | |
| `/truth/actors/{id}`、`/truth/events`、`/truth/env` | `Transform3D` / `Boxes3D`、`TextLog`、`Scalars` | 事件真值，导出参赛数据时整棵删除 |

时间线统一为 `sim_time`（`rr.set_time("sim_time", duration=t_mid)`，与帧时间戳同源）与 `tick`（序列号），与 r8 一致。rerun-sdk 0.38 要求 numpy ≥ 2，0.39 起不再支持 Python 3.10，D3 运行时应以 Python ≥ 3.11 为基线。

### 4.12 与 r3 单元的衔接

本文撰写时 `docs/research/d3/` 下尚无 r3 笔记，衔接依据 r3 的脚本与结果文件（`.cache/research/d3/r3-camera-render/`）：

| r3 结论（结果文件） | 对本设计的影响 |
|---|---|
| moderngl（EGL）720p 单相机 1.37 ms/帧，1080p 2.86 ms/帧，MRT 一次输出 RGB + 线性 z + 实例号；pyrender 10.7 ms、Open3D 26.3 ms（`results/gl/*.json`） | 产品网格后端选 moderngl / 原生 EGL；本文 nvdiffrast 原型的吞吐是下限 |
| nvdiffrast 720p：B=1 5.9 ms/帧，B=4 4.3 ms/帧（`results/nv/town_full_bench.json`） | 与本文 §3.5 的 raw 模式一致（B=8 为 6.9 ms/帧，含动态实体与天空），两单元数据互相印证 |
| 以网格渲染为监督训练的 3M 高斯：训练分布 33.6 dB、低空 29.5 dB、超低空 26.9 dB；720p B=8 RGB+ED 1.94 ms/帧（`results/gs/town_eval.json`） | 3DGS 画质足以作为照片级背景；本文面元初始化（4M，2.1 ms/帧）的吞吐与之一致，§3.6 的结论可直接沿用 |
| 遮挡判据：网格 z-buffer IoU 0.9997，3DGS 期望深度 IoU 0.89、假可见 14.3%（`results/comp/town_composite.json`） | 混合后端改为"网格判遮挡、3DGS 供颜色"（§4.7） |
| 合成各项耗时（720p）：静态网格 5.1 ms、动态实体 2.1 ms、3DGS 3.1 ms、合成 0.2 ms、阴影 4.7 ms、雨 2.4 ms、雾 0.3 ms（同上） | 效果链预算：阴影与雨是 ISP 之外的主要额外开销，需按任务开关 |
| 编码：720p JPEG q90 3.0 ms / 118 kB，PNG 65–88 ms，深度 PNG16 22.9 ms（`results/encode_bench.json`，CPU） | 落盘格式：RGB 用 GPU JPEG（本文 nvjpeg 实测已含在 `isp+jpeg` 模式中），深度用 PNG16 时须多线程编码或改存 float16 原始数据 |

约定一致：r3 `common.py` 与本文 `d4cam.py` 采用同一 GLB 解析（glTF Y-up → ENU、局部原点取包围盒中心与 2% 高度分位）、同一投影矩阵（像素中心 0.5、行 0 为图像顶部），数值可直接对拍。分工：r3 负责渲染器选型、3DGS 训练与合成判据；本单元负责传感器层（相机模型、时间语义、效果链、宿主适配、服务化）。待对齐：静态场景语义分类表（r3 `SEM`：ground / facade / elevated / person / vehicle / dog）作为 `CameraFrame.semantic` 的类别表；动态实体资产（r3 的 CesiumMan / CesiumMilkTruck / Fox）替换本文的程序化长方体代理。

### 4.13 实施建议与工作量

| 阶段 | 内容 | 工作量 |
|---|---|---|
| S1 | `drone4d.sensors.camera`：`CameraModel`、`CameraFrame`、`PoseHistory`、`TriggerClock`、效果链、计数器式噪声；单元测试（投影与 D2 `CameraGeom` 对拍、RotorPy 内参换算、时间网格） | 4–5 人日 |
| S2 | 网格后端（研究版 nvdiffrast；产品版 EGL / OpenGL）+ 动态实体（事件磁带）+ 实例 / 语义 | 6–8 人日 |
| S3 | GPU Worker 服务（合批、背压、MCAP / rrd / 图像落盘）+ FleetSim `SensorBackend` 适配 | 5–6 人日 |
| S4 | 3DGS 混合后端（接 r3 训练成果）+ 深度合成边缘处理 | 4–5 人日 |
| S5 | 宿主适配：RotorPy（含 Gym 观测包装）、gz-sim Python System | 3–4 人日 |
| S6 | 性能优化（视锥裁剪与 LOD、CUDA Graph、锁页异步回读）至 §4.2 目标 | 4–6 人日 |

---

## 5. 风险

| 风险 | 说明 | 应对 |
|---|---|---|
| 许可 | nvdiffrast 第 3.3 条仅限研究与评估；产品不可依赖 | 产品化走 EGL / Vulkan / Ogre2；nvdiffrast 仅存在于研究镜像与基准脚本 |
| 资产质量 | 摄影测量 GLB 建筑背面、底部缺失，近地视角纹理拉伸；`oryahovo` 资产本身标注了纹理问题 | 世界包标注"几何不完整"区域；近地视角任务优先 3DGS 背景 |
| 3DGS 与网格的一致性 | 3DGS 期望深度与网格深度在边缘、薄结构处不一致；以期望深度判遮挡时假可见 14.3%（r3） | 遮挡、深度、标签一律取自网格，3DGS 只供静态像素颜色；颜色与几何不一致处（高斯边缘渗色）以 alpha 混合并在帧元数据中记录后端 |
| 动态实体真实感 | 代理网格与 3DGS 背景的光照、阴影、尺度不一致，会被检测器利用 | 实体资产库（r3）+ 光照调制 + 接地阴影；Benchmark 报告中声明合成方式 |
| 性能目标 | 小批量时瓶颈在动力学（RotorPy 批量步 GPU 约 20–36 ms），大批量时在光栅化与回读 | FleetSim SoA 动力学；视锥裁剪与 LOD、硬件光栅化、锁页异步回读；相机与物理分卡 |
| 共享 GPU | dmax GPU 2、3 由多个 D3 单元共用，实验期间存在并行负载 | 服务化后按卡独占 Worker，基准测试安排独占时段复测 |
| 可复现 | GPU 光栅化与高斯排序在不同驱动 / 卡型上不保证逐位一致 | 按设计讨论稿第九节：固定仿真时间、位姿、资产版本与配置，以误差容忍度判定 |
| RotorPy 上游演进 | v3.0 相机刚发布（2026-09-07），接口可能调整；C1 缺陷尚未修复 | 适配层只依赖 `render(worlds, states)` / `measurement` / `frame_rate` 三个约定；向上游提交 C1、C2 的问题与修复 |
| Python 版本 | rerun-sdk 0.39 起不支持 Python 3.10；gsplat 首次使用需 JIT 编译 | 运行时基线 Python ≥ 3.11；镜像内预编译 gsplat 扩展 |

---

## 6. 文件清单

| 路径 | 内容 |
|---|---|
| `.cache/research/d3/r10-rotorpy/docker/Dockerfile` | 实验镜像（与 r3 共享前两层缓存） |
| `.cache/research/d3/r10-rotorpy/scripts/d4cam.py` | 照片级相机传感器原型：相机模型、畸变重映射、网格与 3DGS 后端、深度合成、效果链、位姿插值、触发时钟、RotorPy 与 D2 适配 |
| `.cache/research/d3/r10-rotorpy/scripts/exp1_rotorpy_camera.py` | RotorPy 相机速度、帧率量化、`simulate_batch` 端到端 |
| `.cache/research/d3/r10-rotorpy/scripts/exp2_photocam.py` | 原型吞吐、画质对比、3DGS、端到端闭环、`simulate_batch` 接入 |
| `.cache/research/d3/r10-rotorpy/scripts/exp3_breakdown.py` | 网格路径分阶段耗时、图集尺寸、三角形规模、视锥占比；GL 对比项因 nvdiffrast 0.4.0 的 GL 上下文已退化为 CUDA 别名而无效 |
| `.cache/research/d3/r10-rotorpy/scripts/dbg_rp.py` | 缺陷 C1 的最小复现（无方块共享世界全部不可见；加远处哑块后恢复） |
| `.cache/research/d3/r10-rotorpy/scripts/exp4_rerun.py` | Rerun 记录模式与开销 |
| `.cache/research/d3/r10-rotorpy/results/` | `exp1_e1a_e1b.json`（速度与帧率量化）、`exp1_e1c.json`（端到端；`exp1_rotorpy_camera.json` 为同一次运行的副本）、`exp2_photocam_run2_all.json`（e2e / gs / dropin）、`exp2_photocam_run3_thr_quality.json`（吞吐与画质的最终数据，`exp2_photocam.json` 为其副本）、`exp3_breakdown.json`、`exp4_rerun.json`、样张（`.rrd` 留在 dmax，见 §3.9） |
| `.cache/research/d3/r10-rotorpy/logs/` | 原始日志（`exp2_run1_badremap.log` 为畸变重映射修正前的作废数据，仅留档） |
