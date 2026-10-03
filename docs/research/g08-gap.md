# G8 深挖：Mock FleetSim 的组合规格与参数标定（可回归规格 v1）

> 问题：MVP 的无人机运动全部依赖 Mock FleetSim，但各单元的实现并存、口径不一：PX4-lite 级联（r20，已与 SIH 对照）、减速限速与抗饱和（r23）、LineTracker STOP_MOTION 与 Safety 阈值（r24）、Crazyflow 式 pipeline（n03）、风阻力系数 CdA 与 c_rd、Dryden 离散方式（r17 称 dt=0.02 时 σ 偏差约 10%）、250 Hz 还是 100 Hz、1000 架 CPU 预算，以及 P600 占位参数（x500 2.064 kg 与 SDF 1.505 kg 不一致）。本文把它们合成一份**可回归**的规格。回归用例为 SIH 对照的阶跃、GoTo、8 m/s 风。
>
> 日期：2026-09-28 ｜ 相关单元：r20、r23、r24、n03、r19、r17（另引 g04、g06）
>
> 输入：`.cache/research/r20/{mock_px4lite.py, x500_n3/, sih_probe.py, run_sih_x500.sh}`、`.cache/research/r23/{fastphys.py, exp2.py}`、`.cache/research/r24/mrs_mock.py`、`refs/discovery/crazyflow/crazyflow/sim/{pipeline.py, sim.py, integration.py}`、`crazyflow/control/core.py`、`crazyflow/dynamics/so_rpy_rotor_drag/params.toml`（`[hb_x500]`）、`refs/discovery/rotorpy/rotorpy/vehicles/multirotor.py`、`refs/sim/PX4-Autopilot/src/lib/motion_planning/PositionSmoothing.cpp`（2026-09-27）、`src/lib/mathlib/math/TrajMath.hpp`、`src/modules/mc_pos_control/multicopter_autonomous_params.yaml`、`refs/sim/Prometheus/Simulator/gazebo_simulator/gazebo_models/uav_models/p600/p600.sdf`、`Modules/uav_control/launch/uav_control_outdoor.yaml`、`P600_controller_params.yaml`；厂商规格 wiki.amovlab.com（P600 旗舰版技术规格）。
>
> 本文原型与实测（全部在 `.cache/research/g08/`）：
> - `fleetsim_g08.py`：规格的 numpy 参考实现（命名多速率 pipeline + PX4-lite + PositionSmoothing-lite + STOP_MOTION + time_stretch + 组合气动 + 标准化精确 Dryden + FastGuard）
> - `regress_g08.py`：与 PX4 SIH（x500 参数，v1.18.0-rc1）黄金轨迹逐项对照，带容差判定
> - `robust_g08.py`：R4–R8（重定目标、湍流侧风、逆风、推力损失、Dryden σ）
> - `calib_g08.py`：CdA 标定；`dryden_vary_g08.py`：Dryden 变空速对照；`shift_g08.py`：指令链路延迟对齐
> - `bench_g08.py`（numpy 分 stage 计时）、`fleet_nb.py`（numba 融合核，venv 为 scratch 的 `nbvenv`，numba 0.67.0）
> - `vehicles/p600/params.yaml`：P600 参数初版（带来源与置信度列）
>
> 测量环境：Xeon E5-2603 v4 8 核 1.7 GHz，与其他任务共享，**实测时 load average 13–19**，所有耗时都偏悲观。

---

## 0. 结论速览

| # | 议题 | 定稿 | 依据（本文实测） |
|---|---|---|---|
| 1 | 控制律 | **所有 Mock 档位共用一套 PX4-lite 外环**（r20 移植：位置 P → 速度 PID + PX4 原生 ARW → 推力矢量/倾角限制/垂直优先饱和 → bodyzToAttitude），L2 只替换"姿态内环 → 执行器 → 积分"。r23 的 Lee + 条件积分退役为 L2 备选，不再作为第二套 ARW | 阶跃、GoTo、8 m/s 风 3 类共 17 项指标全部落在容差内，guard 零事件（§9） |
| 2 | 参考生成器 | PX4 PositionSmoothing-lite，**补上 PX4 的 `time_stretch`（MPC_XY_ERR_MAX=2、MPC_Z_ERR_MAX=1）**；减速约束直接用 PX4 `computeMaxSpeedFromDistance(j,a,d)`（即 r23 `√(2ηad)` 的 jerk 版），巡航速度取 `min(MPC_XY_CRUISE, MPC_XY_VEL_MAX)` | 不加 time_stretch 时 GoTo 轨迹 RMSE 0.633 m（不过关），加上后 0.337 m；vmax/acc/倾角/t90 与 SIH 的差距同时缩小（§5.2） |
| 3 | STOP_MOTION | **采用**（MRS LineTracker 语义：新目标与当前参考速度夹角 > acos 0.98 且 \|v\|>0.05 时先刹停），**围栏校验改为折线** `[p, p_stop, goal]` | 135° 重定目标：实际航迹距校验折线最大 0.54 m；不刹停时 2.84 m，偏离直线段 7.6 m；代价是到达慢 2.2 s（§5） |
| 4 | 时钟与步长 | **世界主时钟 250 Hz（4 ms，与 PX4 SITL/SIH/HIL lockstep 同频）**；L1 的控制与物理 `every=2` → **125 Hz**；tap（写 G5 的 StateRing）也是 125 Hz，紧跟每次 L1 更新；env 50 Hz、guard 50 Hz、battery 与 fleet_guard 10 Hz，按 phase 错峰 | L1 在 250/125/100/50 Hz 下全部通过回归，步长不是保真度瓶颈；选 125 Hz 是为了与 250 Hz 的 L2/HIL 共用一个时钟（§3） |
| 5 | 实现 | L1 热路径（refgen→pos_ctrl→att→motor→aero→integrate）用 **numba 融合核**；numpy stage 版本保留为参考实现（oracle），两者逐 tick 对拍 | 1000 架：numpy 0.87 核·s/仿真 s（@125 Hz），numba 0.046–0.125（@100–250 Hz）；numpy 单机也要 0.27 核（固定调用开销）（§11） |
| 6 | 气动 | 默认 **组合模型** `F = −½ρ·CdA·|v_r|·v_r − (ΣΩ)·c_rd·v_r⊥`，`ΣΩ = n·ω_max·√T̂`；x500：**CdA = 0.02065 m²**（按 SIH 8 m/s 俯仰 −7.85° 标定）、**c_rd = 8.06428e-5**（gz x500 SDF）；`linear`（K_dv=0.35）只用于 SIH 严格对照 | 两种模型都通过回归；12 m/s 风时组合模型俯仰 13.3°，线性 11.7°（§6） |
| 7 | Dryden | g06 精确离散**必须改用标准化状态**：`Φz = e^{−r}[[1+r, r],[−r, 1−r]]`，`Qz = I − ΦzΦzᵀ`，`y = σ/2·(z₁ + √3·z₂)`，r = dt·V/L | g06 原写法在空速 V 变化时发散：σ 实测 2.54（目标 1.22），出现 39 m/s 尖峰；标准化后 1.22、最大 6.0。dt 取 0.01/0.02/0.05 时 σ 偏差都在 ±1.3% 以内。r17 的"10%"是样本误差（§8） |
| 8 | Safety 阈值 | FastGuard 阈值重新以 PX4-lite 为基准：pos_err 以 time-stretch 参考为基准，**ELAND 3.0 m（持续 0.5 s）/ FAILSAFE 5.0 m**；raw offboard 和速度模式不评 pos_err，改用 250 ms 指令 watchdog；油门饱和改为"推力 ≥0.95·THR_MAX 且低于参考 0.5 m，持续 1 s" | 中度湍流（σ_ref 2.91）加 12 m/s 侧风时 pos_err 的 p99.9 为 1.74 m，r24 的 1.5 m 会误报；推力损失 45% 时 THROTTLE_SAT 在 2.07 s 后触发，所有回归与鲁棒场景零误报（§7） |
| 9 | P600 参数 | **x500 继续作为 SIH 回归机体**（2.064 kg 就是黄金数据注入 SIH 的质量）；**P600 用独立 profile**：质量 3.5 kg（厂商规格）、推力取 SDF（19.23 N/桨，TWR 2.24）、惯量与气动按 x500 标定缩放、限速取 Prometheus 实际写入 PX4 的参数（3 m/s；COMMAND 模式 1 m/s）。**1.505 kg 被一致性检查拒绝**（悬停 0.19、TWR 5.2） | 采用规则与自检见 §10；初版参数文件 `vehicles/p600/params.yaml` |
| 10 | 1000 架预算 | FleetSim 进程目标 ≤1 核（@RTF 1）：numba L1 约 0.06 + env 约 0.08 + guard 约 0.05 + tap 约 0.02 + 其余约 0.05 ≈ **0.26 核**；纯 numpy 约 1.0 核，只适合 ≤300 架 | §11 |

---

## 1. 档位与边界

| 档位 | 内容 | 时钟 | 规模 | 状态 |
|---|---|---|---|---|
| L0 | 运动学插值（回放、超大群体） | 与 tap 同频 | >10⁴ | 不在本文范围 |
| **L1 PX4-lite** | §4 外环 + 理想速率环 + 推力一阶滞后 + 组合气动 + 平动积分 | 主时钟 250 Hz，`every=2`（125 Hz） | 1–2000（numba） | **MVP 默认，本文定稿** |
| L2 | 同一外环（pos_ctrl `every=2`）+ 姿态 P + 速率 PID + 分配 + 转子滞后 + RotorPy 力矩（n03 §3.1）+ 下洗/地效 | `every=1`（250 Hz） | 约 50–300 | V0.3–V0.4，只复用本文的 pipeline、guard、气动与 Dryden |
| HIL | PX4 飞控 + 我方物理，TCP 4560+i lockstep | `every=1`，每 tick 发 HIL_SENSOR | 8–16 | V0.4–V0.6 |
| SIH | 独立容器，自带时钟 | 不进 pipeline，Gateway 镜像 | ≤8/本机 | V0.2；也是本文的黄金数据源 |

同一个 `FleetSim` 里可以混合 L1、L2、HIL 三类机体，各 stage 按 `fidelity` 掩码处理自己负责的子集，全部由同一主时钟推进。所以编队、避碰、ANet 协同不会出现 SIH 多进程那样的时钟漂移（r20 R5）。

---

## 2. 状态与坐标约定

**内部一律用 NED/FRD**（与 PX4 源码逐行对应，移植时不用改符号），**边界统一转换**：

- 输入（ingest）：ENU 目标转 NED 为 `(y_e, x_e, −z_e)`；航向 `ψ_ned = π/2 − ψ_enu`。
- 环境（env）：`Environment.query` 返回 ENU 风（g06 约定为去向矢量）→ 转成 NED "空气速度"。**不要**把 SIH 的 `SIH_WIND_N/E`（来向）语义带进来。
- 输出（tap）：`p_enu = (y, x, −z)`；`R_enu_flu = T·R_ned_frd·B`，其中 `T = [[0,1,0],[1,0,0],[0,0,−1]]`，`B = diag(1,−1,−1)`。然后打包成 g04 的 Full64/Lite32。

**FleetState（SoA，一个 slot 对应一架机，slot 分配后稳定不变，移除的机体只打掩码）**

```python
# sim/fleet/state.py
@dataclass(slots=True)
class FleetState:
    tick: int                         # 主时钟 tick，t_ns = tick * 4_000_000
    active: bool[N]; fidelity: u8[N]  # 1=L1 2=L2 4=HIL
    agent_no: i32[N]; profile_id: u16[N]
    # plant（NED/FRD）
    p: f64[N,3]; v: f64[N,3]; a_meas: f64[N,3]; q: f64[N,4]   # q = (w,x,y,z)
    thrust: f64[N]                    # 归一化总推力 T/T_max
    # PX4-lite 控制器
    mode: u8[N]                       # 0 OFFBOARD_POS 1 GOTO/PATH 2 VELOCITY 3 HOLD
    pos_sp: f64[N,3]; vel_sp_cmd: f64[N,3]; yaw_sp: f64[N]
    target: f64[N,3]; tr_x/tr_v/tr_a: f64[N,3]; stopping: bool[N]
    vel_int: f64[N,3]; thr_sp: f64[N,3]; q_sp: f64[N,4]; pos_ref: f64[N,3]
    # 环境（ZOH）
    wind: f64[N,3]; rho: f32[N]; env_flags: u8[N]; ground_z: f64[N]
    dry_u: f64[N]; dry_v: f64[N,2]; dry_w: f64[N,2]   # 标准化 Dryden 状态（§8）
    # guard
    mode_switch_t: f64[N]; tilt_err_since/pos_err_since/thr_since: f64[N]   # NaN 表示未计时
    last_cmd_t: f64[N]                # 流式指令 watchdog
    # 每个 profile 的参数表：按 profile_id 做 gather（N 架共用少数几种机型）
```

**RNG 与确定性**：每个世界一条 `PCG64(SeedSequence([world_seed, stream_id]))`，stream 为 dryden=1、faults=2、sensors=3。按 slot 升序一次性抽样。给定相同的初始编组和命令序列，结果逐位可复现。编组变化（增删机体）会改变其他机体的噪声序列，这一点可以接受，因为编组本身会写进录制。g06 期望的"每机独立流"留到 numba 版用计数器式 RNG（Philox，key=(seed, agent_no)，counter=tick）实现。

---

## 3. Pipeline：stage 顺序、步长与调度

### 3.1 Crazyflow 式骨架（移植架构，不移植 JAX）

Crazyflow `sim/pipeline.py` 的做法是：用 OrderedDict 保存命名 stage，名称唯一，提供 `append_fn / prepend_fn / insert_fn_before / insert_fn_after / replace_fn / remove_fn`。控制器按各自频率门控，判据是 `controllable(step, freq, control_steps, control_freq) = (step − control_steps) ≥ freq/control_freq`，即每个控制器先把命令暂存（`staged_cmd`），到了触发 tick 才生效。我们保留这套命名与门控，但做三处改动：

1. **门控改为整数 `every/phase`**。Crazyflow 的判据在 `freq/control_freq` 非整数时会悄悄降频：250/100 = 2.5，实际每 3 步才触发一次，即 83 Hz。我们要求所有子频率都能整除主时钟。
2. **stage 原地修改 numpy SoA**，不用函数式 pytree。确定性由固定顺序和 RNG 流规则保证。
3. **增加 `fidelity` 掩码**，让 L1、L2、HIL 共用一条 pipeline。

```python
# sim/fleet/pipeline.py
class Stage(NamedTuple):
    fn: Callable[["FleetState", "StageCtx"], None]
    every: int = 1          # 每 every 个主时钟 tick 执行一次
    phase: int = 0          # (tick - phase) % every == 0 时执行；用来把子频率 stage 错峰
    fidelity: int = 0b111   # 作用于哪些档位（L1|L2|HIL）

class Pipeline(OrderedDict[str, Stage]):
    def append(self, name, stage): ...
    def insert_before(self, anchor, name, stage): ...
    def insert_after(self, anchor, name, stage): ...
    def replace(self, name, stage): ...
    def remove(self, name): ...           # 名称冲突或 anchor 不存在时抛 KeyError（与 Crazyflow 一致）

@dataclass
class StageCtx:
    env: "EnvironmentService"; world: "WorldQuery"; bus: "EventBus"; rng: dict[str, np.random.Generator]
    profiles: "ProfileTable"              # 按 profile_id gather 出的 SoA 参数（§10）
    dt_tick: float = 0.004                # 主时钟步长
    def dt(self, stage: Stage) -> float: return self.dt_tick * stage.every
```

### 3.2 默认 pipeline（定稿）

主时钟 250 Hz，每 tick 4 ms。"每"列里的数字表示每隔几个 tick 执行一次。

| 序 | stage | 每 | phase | 频率 | 作用对象 | 内容 |
|---|---|---|---|---|---|---|
| 00 | `clock` | 1 | 0 | 250 | 全部 | 推进 t；HIL 机体在此处 lockstep 屏障（发 HIL_SENSOR、等 HIL_ACTUATOR_CONTROLS，50 ms 超时则标记 STALLED 并沿用上一帧，n03 §3.6） |
| 10 | `ingest` | 1 | 0 | 250 | 全部 | 取出到期的 operator、agent 和 supervisor 命令（supervisor 优先），staged → active；设置 STOP_MOTION；刷新 `last_cmd_t`；检查流式指令 watchdog（§7.2） |
| 20 | `env` | 5 | 0 | 50 | 全部 | `env.query(pos, t, fields=WIND|THERMO|OPTICS, vel, agent_idx)` → wind、rho、env_flags；湍流 box 或 dryden（§8）；两次查询之间零阶保持 |
| 30 | `refgen` | 2 | 0 | 125 | L1、L2 | PositionSmoothing-lite + STOP_MOTION + time_stretch（§5） |
| 40 | `pos_ctrl` | 2 | 0 | 125 | L1、L2 | PX4 PositionControl（§4），输出 thr_sp、q_sp、pos_ref |
| 50 | `att_ctrl` | 2 / 1 | 0 | 125 / 250 | L1 / L2 | L1：四元数 P + 速率限幅（理想速率环）；L2：再加速率 PID 和分配 |
| 55 | `faults` | 2 / 1 | 0 | — | 全部 | 执行器类故障（推力损失、电机失效），只改 plant 参数 |
| 60 | `motor` | 2 / 1 | 0 | — | L1 / L2 | 推力一阶滞后，精确离散 `a = 1 − e^{−dt/τ}`（原型用的 `min(1, dt/τ)` 在 dt=10 ms 时偏大 18%） |
| 70 | `aero` | 2 / 1 | 0 | — | L1 / L2 | 组合气动（§6）；L2 换成 RotorPy 力矩 |
| 75 | `interaction` | 5 | 0 | 50 | L2 | 下洗流与地效（10 m 网格邻域，n03 §3.4） |
| 80 | `integrate` | 2 / 1 | 0 | — | L1 / L2 | 半隐式 Euler：先更新 v，再更新 p；L2 的姿态用指数映射 |
| 90 | `contact` | 2 | 0 | 125 | 全部 | 地面 `ground_z = −DSM(x,y)` 夹持；与 World voxel/SDF 求碰撞 → CRASHED/COLLISION_WORLD |
| 100 | `sensors` | 5 | 2 | 50 | 全部 | GPS Gauss-Markov、IMU 噪声（r20 §3.7）；传感器类故障（state_drop 等） |
| 110 | `guard` | 5 | 1 | 50 | 全部 | FastGuard 向量化检查（§7）→ `bus.emit(SafetyEvent)`；**不直接修改控制状态** |
| 120 | `battery` | 25 | 3 | 10 | 全部 | `P = P_hover·(T/T_hover)^1.5`，更新 SoC 与剩余时间（r24 §4.x） |
| 130 | `fleet_guard` | 25 | 4 | 10 | 全部 | 多机预测冲突与最小间距（网格哈希） |
| 140 | `tap` | 2 | 0 | 125 | 全部 | 转成 ENU/FLU，写入 G5 的 StateRing（`publish_hz` 由 G5 的 `min(100, physics_hz)` 改为 125，即每次 L1 更新后都发布），由 Gateway 按 60 Hz tick 取最新值再打包 Full64/Lite32；对外 WS 频率仍由 r27 的 rate class 决定 |

**顺序为什么这样定**：
- `env` 排在控制器前面，这样同一 tick 内所有消费者（控制、guard、传感器、tap）看到的是同一个环境样本。PX4 控制律本身不读风，风只通过气动作用到机体上。
- `guard` 发出的事件交给 Flight FSM（事件驱动，同进程）裁决，裁决结果作为 supervisor 命令排到下一个 tick 的 `ingest`。这个固定 1 tick 的延迟保证了确定性。
- 子频率 stage 用 phase 错开：env 在 phase 0，guard 在 1，battery 在 3，fleet_guard 在 4。这样 CPU 峰值被摊平，最坏的 tick 也只多做两个子频率 stage。contact 与 tap 紧跟 L1（phase 0），保证每帧发布的都是刚积分、刚做完地面夹持的状态。

### 3.3 为什么是"250 Hz 主时钟 + L1 每 2 个 tick"

回归结果（§9）表明，L1 的步长**不是**保真度瓶颈：

| L1 步长 | 阶跃 RMSE_x | GoTo RMSE_x | 8 m/s 风俯仰 | 结论 |
|---|---|---|---|---|
| 250 Hz | 0.079 m | 0.403 m | −7.87° | 过 |
| 250 Hz（pos_ctrl 125 Hz） | 0.078 | 0.373 | −7.87° | 过 |
| 125 Hz | 0.097 | 0.369 | −7.87° | 过 |
| 100 Hz | 0.109 | 0.355 | −7.87° | 过 |
| 50 Hz | 0.174 | 0.346 | −7.87° | 过（只作信息，不采用） |

所以步长由另外两个约束决定：
1. **与 L2、HIL 共用一个时钟**。PX4 SITL/SIH 的 `IMU_INTEG_RATE=250`，HIL lockstep 每 4 ms 发一次 HIL_SENSOR，L2 的刚体积分也需要 ≤5 ms（Crazyflow 文档：显式积分的步长超过电机时间常数就会过冲，超过两倍就发散）。250 Hz 是唯一能同时容纳三者的主时钟。100 Hz 不能整除 250 Hz，所以 L1 取 `every=2`，即 125 Hz。
2. **CPU 预算**。L1 在 125 Hz 的成本是 250 Hz 的一半（§11）。

**快进**（Timeline ×N）：物理步长保持 4 ms 不变，只改墙钟节奏（r23）。1000 架用 numba 时，单是 L1 核 ×10 约 0.6 核，全 pipeline 约 2.6 核；sim-core 钉在 1 核时，1000 架最多约 ×3.5（§11.2）。

**回归门禁**：L1 必须在 `every=1` 和 `every=2` 两种配置下都通过 §9 的全部容差。

---

## 4. PX4-lite 外环（定稿，逐级公式）

以 `.cache/research/r20/mock_px4lite.py` 为蓝本（公式见 r20 §3.3），定稿时做以下修订。参数改为**按 profile 读取**，不再写死在类常量里。

| # | 修订 | 原因 |
|---|---|---|
| 1 | GoTo 巡航速度 = `min(MPC_XY_CRUISE, MPC_XY_VEL_MAX)` | Prometheus 会把 `MPC_XY_VEL_MAX` 写成 3（COMMAND 模式下为 1）；PX4 `FlightTaskAuto.cpp` L438 就是 `_mc_cruise_speed = min(_mc_cruise_speed, MPC_XY_VEL_MAX)` |
| 2 | 参考生成器的水平加速度按**范数**限幅（`|a_xy| ≤ MPC_ACC_HOR`），不再逐轴限幅 | 逐轴限幅时斜向加速度可达 √2 倍 |
| 3 | 推力滞后精确离散 `1 − e^{−dt/τ}` | dt=10 ms、τ=30 ms 时，原来的 `dt/τ` 偏大 18% |
| 4 | 加入 PX4 `time_stretch`（§5.2） | 这是 PX4 PositionSmoothing 的一部分，r20 原型漏掉了。补上后 GoTo 的 vmax、acc、倾角、t90 全部向 SIH 靠拢 |
| 5 | 输出 `pos_ref`（time-stretch 后的参考位置） | guard 的 pos_err 以它为基准（§7） |
| 6 | **抗饱和只用 PX4 原生方案**：垂直方向推力饱和时停止积分；水平方向用 tracking ARW `e_v.xy −= (2/Kp)·(a_sp.xy − a_prod.xy)` | r23 的"条件积分"是给 Lee 控制器准备的（Lee 本身没有 ARW）。L1 与 L2 共用 PX4 外环后不再需要第二套方案，否则会出现两种手感 |

**默认参数**（PX4 v1.18 yaml；profile 可以覆盖其中的限幅项）：`MPC_XY_P 0.95`、`MPC_Z_P 1.0`；`MPC_XY_VEL_P/I/D_ACC 1.8/0.4/0.2`；`MPC_Z_VEL_P/I/D_ACC 4.0/2.0/0`；`MPC_XY_VEL_MAX 12`、`MPC_Z_VEL_MAX_UP/DN 3/1.5`；`MPC_TILTMAX_AIR 45°`、`MPC_THR_MIN 0.12`、`MPC_THR_MAX 1.0`、`MPC_THR_XY_MARG 0.3`；`MC_ROLL_P/PITCH_P 4.0`、`MC_YAW_P 2.8`；速率上限 220/220/200 °/s，自动模式 yaw 速率上限 60 °/s；悬停推力取 profile 推导值（假设悬停推力估计器已收敛）。

```python
# sim/fleet/px4lite.py —— numpy 参考实现（oracle）；热路径见 kernels_l1.py（numba，§11）
def pos_ctrl(S, P, dt):                       # P = 每个 slot 的 profile 参数（已 gather 成 SoA）
    g = S.mode == GOTO
    pos_sp = where(g, S.tr_x, S.pos_sp); v_ff = where(g, S.tr_v, 0); a_ff = where(g, S.tr_a, 0)
    v_sp = Kpos*(pos_sp - S.p) + v_ff;  clamp_xy_norm(v_sp, P.XY_VEL_MAX); clip(v_sp.z, -Z_UP, Z_DN)
    S.vel_int.z = clip(S.vel_int.z, -g0, g0)
    e_v = v_sp - S.v;  a_sp = Kvp*e_v + S.vel_int - Kvd*S.a_meas + a_ff
    body_z = normalize(-a_sp.x, -a_sp.y, g0);  limit_tilt(body_z, TILT_MAX)
    coll = min((a_sp.z*Th/g0 - Th)/body_z.z, -THR_MIN);  thr = body_z*coll
    saturate_vertical_priority(thr, THR_MAX, XY_MARG)
    arw_vertical(e_v, thr); arw_tracking_xy(e_v, a_sp, thr*g0/Th, 2/Kvp_xy)
    S.vel_int += Kvi*e_v*dt
    S.q_sp = bodyz_to_attitude(-thr/|thr|, S.yaw_sp); S.coll_cmd = |thr|; S.pos_ref = pos_sp
def att_ctrl_l1(S, dt):                       # 理想速率环
    qe = conj(S.q) ⊗ S.q_sp; qe *= sign(qe.w); w = clip(2*qe.xyz*K_att, ±rate_max); S.q = S.q ⊗ exp(w*dt/2)
```

---

## 5. 参考生成器：减速约束、time_stretch、STOP_MOTION 与围栏折线

### 5.1 PositionSmoothing-lite（GOTO、PATH 航段、RTL 各阶段、HOLD 的目标都走它）

```python
# sim/fleet/setpoint.py
def refgen(S, P, dt, gp):
    d = S.target - S.tr_x; dxy = |d.xy|
    vxy = min(min(P.XY_CRUISE, P.XY_VEL_MAX), vmax_from_dist(P.JERK_AUTO, P.ACC_HOR, dxy))   # 减速约束
    vz  = min(Z_V_AUTO_UP if d.z<0 else Z_V_AUTO_DN, vmax_from_dist(P.JERK_AUTO, P.ACC_UP_MAX, |d.z|))
    v_des = [unit(d.xy)*vxy, sign(d.z)*vz]
    v_des[S.stopping] = 0                                              # STOP_MOTION 阶段
    a_tgt = 2.0*(v_des - S.tr_v); clamp_xy_norm(a_tgt, P.ACC_HOR); clip(a_tgt.z, -ACC_UP_MAX, ACC_DOWN_MAX)
    ts = time_stretch(S, P)                                            # §5.2，逐轴
    dts = dt*ts
    S.tr_a += clip(a_tgt - S.tr_a, ±JERK_AUTO*dts); S.tr_v += S.tr_a*dts; S.tr_x += S.tr_v*dts
    S.stopping &= |S.tr_v| >= 0.05

def vmax_from_dist(j, a, d, vf=0.0):          # PX4 TrajMath::computeMaxSpeedFromDistance
    b = 4*a*a/j; c = -2*a*d - vf*vf; return max(0.5*(-b + sqrt(b*b - 4*c)), vf)
```

`vmax_from_dist` 就是 r23"减速感知限速" `√(2·η·a·d)` 在有限 jerk 下的精确形式：j→∞ 时退化为 `√(2ad)`。r23 的 η=0.7 是给**没有 jerk 限幅参考**的 Lee 控制器留的裕度，在 PX4-lite 中不需要。r23 公式里的 `K_p·d` 项也不需要，因为 PX4 的位置 P 加前馈已经覆盖了它。

**r23 公式在本规格中的唯一保留用途**：`VELOCITY` 子模式（外部直接给速度，没有参考轨迹）下做围栏或障碍的方向限速：`v_dir ≤ vmax_from_dist(j, a, max(d_free − 2 m, 0))`，其中 `d_free` 是沿速度方向到围栏或 World 障碍的距离（射线查询 World SDF，每 2 个 tick 一次）。

### 5.2 time_stretch（照搬 PX4 2026-09 版 `PositionSmoothing::_generateTrajectory`）

```text
e_xy = tr_x.xy − p.xy ;  ts_xy = (e_xy·tr_v.xy ≥ 0) ? 1 − clamp(|e_xy|/MPC_XY_ERR_MAX(2.0), 0, 1) : 1
e_z  = tr_x.z − p.z   ;  ts_z  = (e_z·tr_v.z  ≥ 0) ? 1 − clamp(|e_z| /MPC_Z_ERR_MAX(1.0), 0, 1) : 1
虚拟轨迹按 (ts_xy, ts_xy, ts_z) 逐轴放慢；只在"机体落后于轨迹"时生效
```

实测效果（`regress_g08.py`、`robust_g08.py`）：
- GoTo 50 m：不加 time_stretch 时 vmax 5.25、acc98 3.28、倾角 21.7°、t90 10.42 s、RMSE_x 0.633 m（**不过关**）；加上后依次为 4.97、2.97、19.7°、10.70 s、0.369 m。SIH 对应值为 5.16、2.78、20.1°、10.90 s。
- 逆风 GoTo 150 m：参考与机体的最大偏差在 10 m/s 逆风时从 0.83 m 降到 0.32 m，14 m/s 时从 1.11 m 降到 0.38 m。

所以 **pos_err 的上界由 `MPC_XY_ERR_MAX` 决定**，guard 阈值应当以它为基准（§7）。

### 5.3 STOP_MOTION（MRS LineTracker）与围栏折线校验

```python
# ingest：收到 GOTO/PATH 新目标时
if mode == GOTO:                                # 已在 GOTO：保留参考状态（不重置到机体），避免 pos_err 跳变
    cosang = dot(tr_v, goal - tr_x) / (|tr_v|·|goal - tr_x|)
    stopping = |tr_v| > 0.05 and cosang < 0.98
else:                                           # 从其他模式进入 GOTO：按 PX4 从机体状态重新初始化
    tr_x, tr_v, tr_a = p, v, 0
# 命令准入（sim/safety/geofence.py）：
v = |v_xy| ; d_stop = v²/(2·ACC_HOR) + v·ACC_HOR/(2·JERK_AUTO)       # 刹停距离（含 jerk 段）
p_stop = p + unit(v)·d_stop
admit iff path_valid([p, p_stop, goal], buffer=1.0 m)                  # 折线 + 1 m 缓冲；不通过则返回 409 GEOFENCE_PATH
```

实测（`robust_g08.py` R4：以 5 m/s 向东飞行时改为 135° 反向目标，距离约 45 m）：

| 变体 | 预测刹停距离 | 距直线段 [p, goal] 最大偏离 | 距折线 [p, p_stop, goal] 最大偏离 | 最大 pos_err | 到达时间 |
|---|---|---|---|---|---|
| STOP_MOTION | 6.02 m | 6.56 m | **0.54 m** | 0.53 m | 16.0 s |
| 不刹停（PX4 原生，弧线转弯） | 6.02 m | 7.61 m | 2.84 m | 0.55 m | 13.9 s |

**结论**：采用 STOP_MOTION。原因是准入校验只能校验几何上可预测的路径，STOP_MOTION 让实际航迹落在折线 ±0.6 m 以内，1 m 缓冲足够；不刹停时航迹是一段取决于速度的弧线，校验会漏掉。代价是每次反向重定目标多花约 2 s。
- r24 担心的"切目标瞬间 pos_err 超阈值"，在 PX4-lite 中本来就不会发生：已在 GOTO 时保留参考状态，从其他模式进入时从机体状态初始化，两种情况下 pos_err 都连续。
- PATH（多航点）的航点之间不做 STOP_MOTION，按 PX4 的 L1 交叉点和 `computeMaxSpeedInWaypoint` 过弯限速处理（r20 §3.4）。只有"外部改目标"才触发。

---

## 6. 气动：组合模型与系数

### 6.1 方程（L1；L2 替换为 RotorPy 逐桨力矩）

```text
v_r   = v − w(p,t)                                     # NED；w 来自 env stage（均值 + 阵风 + 湍流）
b3    = R·e_z                                          # 机体 z 轴（向下）
ΣΩ    = n·ω_max·√clip(T̂, 0, 1)                         # T ∝ ω² => 每桨 ω = ω_max·√T̂（L1 各桨推力相同）
F_aero = −½·ρ·CdA·|v_r|·v_r − (ΣΩ·c_rd)·(v_r − (v_r·b3)·b3)
F      = −b3·T̂·T_max·(ρ/ρ0)^k_rho + F_aero + m·g·e_z  # k_rho=1：推力随密度衰减（高原或高温），ρ 取自 env THERMO
```

- 第一项是机身寄生阻力（AirSim FastPhysics 的六面阻力在常用速度下可以简化成 CdA 形式，见 r23 §3.8）。
- 第二项是转子 H 力，即 gz `MulticopterMotorModel` 的 `−|ω|·c_rd·v⊥`，也就是 PX4 x500 在 gz 里风唯一的作用路径（r23）。
- **风进入动力学的唯一入口是 `v_r`**（n03 §3.1）。

### 6.2 标定结果

| 量 | x500（回归机体） | 来源与方法 |
|---|---|---|
| c_rd | **8.06428e-5 N·s/(m·rad)** | gz x500 `model.sdf` `rotorDragCoefficient` |
| 悬停 ΣΩ / 转子阻力 | 3077 rad/s → **0.248 N/(m/s)** | 由 T̂_hover = 0.59 推出 |
| CdA | **0.02065 m²** | 二分标定，使 8 m/s 风悬停俯仰等于 SIH 实测的 −7.85°（`calib_g08.py`） |
| SIH 等效线性 K_dv | 0.35 N/(m/s) | 8 m/s 时组合模型的力与 SIH 线性模型相同（2.8 N） |

两种模型的风致倾角对比（`calib_g08.py`，悬停，逆向风阶跃，稳态俯仰）：

| 风速 | 4 m/s | 8 m/s | 12 m/s | 14 m/s |
|---|---|---|---|---|
| linear（K_dv=0.35） | 3.96° | 7.88° | 11.72° | 13.61° |
| composite（CdA=0.02065、c_rd=8.06e-5） | 3.38° | **7.85°** | 13.31° | 16.36° |

**采用规则**：
- 产品默认 `aero.model = composite`：它有物理量纲，随 ρ 和推力（载荷）变化，能区分机型。
- `linear` 只用于"SIH 严格对照"配置，此时物理方程与 SIH 逐项相同。
- CI 两种都跑（§9）。

**参照点**：Crazyflow `[hb_x500]` 用真机辨识得到的线性阻力是 0.80 N/(m/s)（机体 x/y，2.28 kg），约为 SIH 取值的 2.3 倍。说明真实 x500 级机体的风阻很可能比 SIH 参数大。这也是 P600 的 CdA 和 c_rd 必须在 V0.4 用真机风天悬停倾角重新辨识的原因（§10.4）。

### 6.3 P600 的气动（占位，D 级）

按单位质量等效于 x500 标定来取值：`CdA/m = 0.0100 m²/kg`，`ΣΩ·c_rd/m = 0.120 s⁻¹`。得到 **CdA = 0.035 m²**、**c_rd = 1.05e-4**（悬停时 ΣΩ = 4008 rad/s，转子阻力 0.42 N/(m/s)）。于是 P600 与 x500 的风致倾角曲线在构造上一致（8 m/s 为 7.85°）。未来 `10050_sihsim_p600` 机架文件用的 SIH 等效线性值为 `SIH_KDV ≈ F(8)/8 = 0.59`。

---

## 7. Safety：FastGuard 阈值（以 PX4-lite 为基准重设）

### 7.1 阈值表（`sim/safety/fast_guard.py`，向量化，50 Hz）

| 检查 | 阈值 | 持续 | 宽限（切换后 1 s 内跳过） | 动作（g04 FlightState/子模式） | 依据 |
|---|---|---|---|---|---|
| 绝对倾角 | > 90° | 0 | 否 | 接地则 CRASHED/TILT，否则 DISARMED/KILLED | r24 |
| 绝对倾角 | > 75° | 0 | 是 | ELAND/CONTROLLED | r24 |
| 倾角误差 `acos(b3·b3_sp)` | > 20° | 0.5 s（宽限期内计时清零） | 是 | DISARMED/KILLED（电机或桨失效） | r24。稳态湍流中最大 6.2–6.8°；阶跃起始的 44° 由宽限期覆盖 |
| **pos_err = \|pos_ref − p\|**（只在 GOTO、PATH、ORBIT、RTL、LANDING 时评估） | **ELAND > 3.0 m**（= MPC_XY_ERR_MAX + 1）| 0.5 s | 是 | ELAND/CONTROLLED | 中度湍流加 12 m/s 侧风时 p99.9 为 1.74 m、最大 1.82 m；逆风 14 m/s 时最大 0.38 m |
| 同上 | **FAILSAFE > 5.0 m**（= 2·MPC_XY_ERR_MAX + 1） | 0 | 是 | FAILSAFE/DESCENT | 推力损失 45% 时，在 THROTTLE_SAT 之后 1.6 s（故障后 3.7 s）触发 |
| 油门饱和 | 推力 ≥ 0.95·THR_MAX **且** `p.z − pos_ref.z > 0.5 m`（掉到参考以下） | 1.0 s | 是 | ELAND/CONTROLLED | 推力损失 45% 时 2.07 s 后触发；所有回归场景零误报 |
| 航向误差 | > 90° | 0 | 是 | ELAND | r24 |
| 状态缺失（仅故障注入 `state_drop`） | > 0.1 s | 0 | 否 | FAILSAFE/DESCENT | r24 |
| **流式指令 watchdog**（OFFBOARD_POS、VELOCITY） | 250 ms 内没有新 setpoint | — | — | HOLD/LINK_LOSS | r23；PX4 的 `COM_OF_LOSS_T` 为 1 s，mock 取得更严 |

**与 r24 的差异及原因**：
1. **r24 的 `mock_cascade: {eland 1.5, failsafe 2.5}` 不适用于 PX4-lite**：
   - PX4 的参考带 time_stretch，允许落后到 2 m；
   - 实测中度湍流（σ_ref 2.91，即 MIL "中"档）加 12 m/s 侧风时 pos_err 的 p99.9 为 1.74 m，1.5 m 会误报 ELAND。
   - 新阈值 3.0 m 加 0.5 s 持续，裕度为 1.7 倍；5.0 m 只在真正失控时触发。
   - r24 的 `px4_offboard: {3.5, 4.5}` 保留给 SIH 后端的镜像检查。
2. **r24 的 `max_throttle 0.8` 不能照搬**：MRS 的 throttle 是转速比例。换算到力：`((0.8·6630 + 1170)/7800)² / 0.297 ≈ 2.3` 倍悬停力，而 x500 的悬停推力就是 0.59，这个阈值永远不会触发。所以改用力比例加"掉高"双条件。
3. **raw OFFBOARD_POS 和 VELOCITY 不评 pos_err**：阶跃指令下 pos_err 会是 50 m，没有意义。这两种模式靠倾角、倾角误差、油门、围栏、watchdog 兜底。控制跟踪劣化（`|v_sp − v| > 3 m/s` 持续 2 s）只发 `SAF.CTRL.TRACK_DEGRADED` 警告，不触发动作。

### 7.2 执行链

```text
guard (50 Hz, 向量化) ─> SafetyEvent{code, level, slot, t} ─> bus
Flight FSM（事件驱动，g04 的 14 态白名单，只升不降，锁存）─> 裁决 ─> SupervisorCommand(t_next_tick)
下一 tick 的 ingest 优先执行 supervisor 命令（HOLD = 以当前 tr_x 为目标的 GOTO；ELAND = 下降速度 0.5 m/s 的 LANDING 参考；
FAILSAFE = r24 的前馈下坠：参考冻结，v_ff = (0, 0, +1 m/s NED)）
```

所有计时（持续、宽限）都使用**仿真时间**。watchdog 在暂停时冻结（r24 §7）。

---

## 8. 湍流：Dryden 用标准化状态的精确离散

g06 已定：物理默认用共享冻结盒 `box`；`dryden` 只用于单机 MIL 回归和 SIH 对照。本节只修正 `dryden` 的实现。

### 8.1 问题

g06 §5.5.3 的写法是在物理坐标下保存状态 x，再按每个 env tick 的 `T = L/V`（V = |U_mean − v|，Taylor 冻结假设下的相对空速）重算 Φ 和 Qd。但 x 的平稳尺度 `P∞ = diag(πT³/4, πT/4)` 本身依赖 T：T 一变，旧的 x 相对新的 P∞ 就失配，输出 `y = (K/T²)(x₁ + √3·T·x₂)` 被成倍放大。

实测（`dryden_vary_g08.py`：L = 151 m，σ = 1.22，机体速度在 0–6 m/s 之间以 20 s 周期振荡，dt = 0.02，2000 架）：

| 写法 | 实测 σ | p99.9 \|y\| | 最大 \|y\| |
|---|---|---|---|
| g06 原写法（物理坐标状态） | **2.54**（目标 1.22） | 18.0 m/s | **39.1 m/s** |
| 标准化状态（本文） | 1.22 | 3.9 m/s | 6.0 m/s |

在 FleetSim 原型里，这个问题的表现是：机体从悬停加速到 3 m/s 的 1.9 s 内，横向阵风达到 13.8 m/s，控制随即发散（已修复）。

### 8.2 定稿公式（对任意 dt 和时变的 T、σ 都精确）

```python
# environment/wind/turbulence.py :: DrydenBank（每个 slot 一份状态；标准化，平稳分布方差为 I）
FT = 0.3048
h_ft = max(agl/FT, 10); L_w = h_ft*FT; L_u = L_v = h_ft/(0.177+0.000823*h_ft)**1.2*FT
σ_w = 0.5295*σ_ref;     σ_u = σ_v = σ_w/(0.177+0.000823*h_ft)**0.4          # g06 §5.5.1
V = max(|U_mean − v|, 0.5)
# u 通道（一阶）：a = exp(−V·dt/L_u);  z_u ← a·z_u + √(1−a²)·n;  u = σ_u·z_u
# v、w 通道（二阶，H(s) = σ√(L/πV)·(1+√3Ts)/(1+Ts)²，T = L/V，r = dt/T）：
Φz = e^{−r}·[[1+r, r], [−r, 1−r]]
Qz = [[1 − e^{−2r}(1+2r+2r²), 2r²e^{−2r}], [2r²e^{−2r}, 1 − e^{−2r}(1−2r+2r²)]]      # = I − ΦzΦzᵀ
z ← Φz·z + chol(Qz)·n ;   y = (σ/2)·(z₁ + √3·z₂)                                # Var(y) = σ²·(1+3)/4 = σ²
初值 z ~ N(0, I)（平稳起步）；输出按平均风方向旋转到 NED（u 沿风，v 横向，w 向上 → NED 取 −w）
```

推导：`x = diag(√(πT³/4), √(πT/4))·z` 代入 g06 的 Φ，得到 `Φz = D⁻¹ΦD`，它只依赖 r；平稳协方差为 I，与 T 无关，所以 T 和 σ 可以在每个 tick 更新而不需要重新缩放状态。

### 8.3 σ 回归（`robust_g08.py` R8 与 §8.1 脚本，1000–2000 架集合，h = 50 m，U = 8 m/s，σ_ref = 1.45）

| env dt | σ_u 目标 / 实测 | σ_v 实测 | σ_w 目标 / 实测 |
|---|---|---|---|
| 0.01 | 1.223 / 1.208–1.220 | 1.224–1.234 | 0.768 / 0.766–0.767 |
| 0.02 | 1.223 / 1.213–1.224 | 1.223–1.227 | 0.768 / 0.765–0.767 |
| 0.05 | 1.223 / 1.216–1.224 | 1.221–1.224 | 0.768 / 0.765–0.769 |
| 0.02，V 在 2–14 m/s 之间变化 | 1.223 / 1.224 | 1.226 | 0.768 / 0.767 |

偏差全部在 ±1.3% 以内，**与 dt 无关**。r17"dt=0.02 时 σ 偏高约 10%"已由 g06 判定为单条序列的样本误差，本文结果与之一致。env stage 定为 50 Hz（dt = 0.02）即可。

---

## 9. 回归规格（SIH 黄金对照）

### 9.1 黄金数据

- 来源：r20 `sih_probe.py` + `run_sih_x500.sh`，镜像 `px4io/px4-sitl:v1.18.0-rc1`，SIH 参数为 x500：`SIH_MASS 2.064`、`T_MAX 8.55`、`KDV 0.35`、`T_TAU 0.03`、`L 0.174`、`MPC_THR_HOVER 0.59`。
- 3 个实例，都先悬停在 10 m：
  - inst0：offboard 50 m 北向位置阶跃（仅位置，20 Hz 流）；
  - inst1：`DO_REPOSITION` 北 50 m；
  - inst2：AUTO_LOITER 中 `SIH_WIND_N = 8`（来向为北，即空气向南流动）。
- 数据格式：`LOCAL_POSITION_NED` 与 `ATTITUDE`，约 21 Hz（48 ms）。
- 入库：把 `.cache/research/r20/x500_n3/` 原样复制到 `tests/golden/sih_x500_px4-1.18rc1/`，附一个 `README` 写明上述参数。

### 9.2 对齐

Mock 按 48 ms 采样，从命令时刻起记录。**SIH 的指令链路延迟为 0.12 s**（20 Hz offboard 流的半周期 + EKF 与输出预测器 + 遥测采样），比较时把 Mock 轨迹右移 0.12 s。这个值是扫描 0–0.4 s 后使阶跃 RMSE_x 最小的平移量（`shift_g08.py`：0.12–0.14 s）。不对齐时阶跃 RMSE_x 为 0.66 m，对齐后为 0.08 m。延迟不写进 Mock 本身：ingest 的延迟是后端属性，Mock 默认为 0。

### 9.3 指标与容差（`regress_g08.py::TOL`）

| 用例 | 指标 | SIH | L1 linear @250 | L1 linear @125 | L1 composite @125 | 容差 |
|---|---|---|---|---|---|---|
| 阶跃 | vmax (m/s) | 11.89 | 11.89 | 11.89 | 11.75 | ±3% |
| | acc98 (m/s²，48 ms 差分) | 8.00 | 7.99 | 7.97 | 7.92 | ±10% |
| | 最大倾角 | 44.4° | 44.1° | 44.2° | 44.1° | ±2° |
| | t90 | 5.00 s | 4.85 | 4.85 | 4.90 | ±0.25 s |
| | 进入 0.5 m 带 | 9.60 s | 9.60 | 9.60 | 9.65 | ±1.0 s |
| | 超调 | 1.70 m | 1.67 | 1.64 | 1.54 | ±0.30 m |
| | RMSE_x（0–20 s，右移 0.12 s） | — | 0.079 | 0.097 | 0.104 | ≤0.25 m |
| | RMSE_v | — | 0.110 | 0.112 | 0.133 | ≤0.40 m/s |
| GoTo | 巡航 vmax | 5.16 | 4.96 | 4.97 | 4.97 | ±5% |
| | acc98 | 2.78 | 2.96 | 2.97 | 3.00 | ±20% |
| | 最大倾角 | 20.1° | 19.7° | 19.7° | 19.5° | ±2° |
| | t90 | 10.90 s | 10.75 | 10.70 | 10.66 | ±0.55 s（5%） |
| | 超调 | 0.08 m | 0.16 | 0.16 | 0.12 | ±0.30 m |
| | RMSE_x | — | 0.403 | 0.369 | 0.337 | ≤0.50 m |
| | RMSE_v | — | 0.191 | 0.177 | 0.165 | ≤0.25 m/s |
| 8 m/s 风 | 稳态俯仰（风阶跃 8 s 后均值） | −7.85° | −7.87° | −7.87° | −7.85° | ±0.3° |
| | 最大水平漂移 | 0.82 m | 0.70 | 0.69 | 0.69 | ±0.25 m |
| 全部 | guard 事件数 | — | 0 | 0 | 0 | = 0 |

**全部配置的结果**：linear 在 250、250（pos 125）、125、100、50 Hz 下全部通过；composite 在 250、125、100 Hz 下全部通过。**反例**：composite @125 Hz 关掉 time_stretch 后（配置里同时关了 STOP_MOTION，但该用例从悬停起步，STOP_MOTION 不起作用），GoTo 的 RMSE_x 为 0.633 m，**不通过**。

**不纳入容差的项**：
- GoTo 进入 0.5 m 带的时间：SIH 为 24.1 s，是 EKF 慢漂移造成的 ±0.3 m 蠕动，Mock 为 13.0 s；
- 风测试的横滚：SIH 为 1.62°，是 SIH 的非对称量，Mock 为 0；
- 阶跃的高度下沉：SIH 0.28 m，Mock 0.17 m，只记录不判定。

### 9.4 鲁棒用例（不对照 SIH，只断言）

| ID | 场景 | 断言 | 本文结果 |
|---|---|---|---|
| R4 | 5 m/s 时 135° 重定目标 | STOP_MOTION 开启时距折线 ≤ 1.0 m；无 guard 事件 | 0.54 m；0 |
| R5a | GoTo 80 m + 8 m/s 侧风阶跃 + Dryden σ_ref 1.45，40 架 | pos_err 的 p99.9 < 1.5 m；无事件 | 0.89 m（250 Hz 时 0.96）；0 |
| R5b | 同上，σ_ref 2.91、12 m/s（x500 与 P600） | pos_err 最大 < 3.0 m；无事件 | 1.82 m；0 |
| R6 | 10 m/s 与 14 m/s 逆风 GoTo 150 m | time_stretch 开启时最大 pos_err ≤ 1.0 m | 0.32 / 0.38 m（关闭时 0.83 / 1.11 m） |
| R7 | 悬停中推力损失 45%（TWR 降到 0.93） | 3 s 内 THROTTLE_SAT | 2.07 s；再过 1.6 s 触发 POS_ERR_FAILSAFE |
| R8 | Dryden σ，dt ∈ {0.01, 0.02, 0.05} | 偏差 ±3% | ±1.3% |
| R9 | numba 核与 numpy oracle 对拍 10 s，100 架 | 状态最大差 ≤ 1e-9 | 待实现时加入 |

### 9.5 CI 落点

```text
tests/golden/sih_x500_px4-1.18rc1/{inst0,inst1,inst2}.csv, marks.csv, README.md
tests/sim/test_fleet_sih_parity.py        # 参数化：aero ∈ {linear, composite} × l1_every ∈ {1, 2}，按 §9.3 断言
tests/sim/test_fleet_robust.py            # R4–R9
tools/regen_sih_golden.sh                 # PX4 升级时重录：run_sih_x500.sh + sih_probe.py（r20）；新旧差异进 PR
```

---

## 10. P600 占位参数的采用规则

### 10.1 冲突来源

| 数值 | 出处 | 实际身份 |
|---|---|---|
| 2.064 kg | r20，PX4-gazebo-models x500 base 2.0 + 4×0.016 | **x500**，也是注入 SIH 黄金数据的质量 |
| 2.0 kg | MRS `x500.yaml`；Prometheus `P600_controller_params.yaml`（pid 块，UDE/NE 块为 1.0） | MRS 的 x500；Prometheus 控制器的通用默认值 |
| 2.28 kg | Crazyflow `[hb_x500]`（真机辨识） | Holybro X500 实机 |
| **1.505 kg** | Prometheus `p600.sdf`：base 1.47 + imu 0.015 + 4×0.005 | Iris 模板：推力 19.23 N/桨 → TWR 5.2，悬停 0.19；`model.config` 的描述里甚至还写着 "P250" |
| 3.3 kg / MTOW 4 kg | 厂商 P600 旗舰版技术规格：含 6S 10 Ah 电池（1.2 kg）、G1 吊舱 104 g、Allspark 213 g、RTK | **真实 P600** |
| 22 min | 厂商：P600-MID360 定点悬停续航 | 真实 P600（MID-360 版） |

### 10.2 规则

1. **x500 与 P600 是两个 profile，不互相顶替。**
   - `x500_sih`（linear）与 `x500`（composite）是回归机体，参数永远等于黄金数据注入 SIH 的值；
   - `p600_mid360` 是产品默认机型，UI 上显示为"P600"。
   - 00-index §3.9 的"在此之前 Mock 默认用 x500 参数"改为：**CI 用 x500，产品默认用 p600_mid360（placeholder）**。
2. **每个参数取置信度最高的来源**：A 实测（ULog、称重、推力台）> B 厂商规格 > C 同型号的几何或执行器参数（Prometheus SDF），且需通过 §10.3 的自洽检查 > D 按 x500 标定缩放 > E 拒绝（写进 `rejected`，保留可追溯性）。
3. **加载时必须通过自洽检查**，不通过就拒绝加载。status 为 `placeholder` 时 UI 标注"参数未辨识"。
4. **限速跟着后端语义走**：同一架 P600，在 `px4_default`、`prometheus_outdoor`、`prometheus_command` 三种限速配置下是不同的"手感"。Prometheus 实际向 PX4 写入的是 `MPC_XY_VEL_MAX 3`、`MPC_ACC_HOR 3`、`MC_YAWRATE_MAX 30`，进入 COMMAND_CONTROL 时再改成 1.0 m/s 和 2.0 m/s²。
   - 默认配置为 `prometheus_outdoor`；
   - Prometheus 模拟器后端用 `prometheus_command`。

### 10.3 自洽检查（`sim/fleet/profiles.py::validate`）

| 检查 | 范围 | p600_mid360（3.5 kg） | p600.sdf 原值（1.505 kg） |
|---|---|---|---|
| 悬停推力 `m·g/(n·T_max)` | 0.30–0.65（类比 MRS 的 hover_throttle_range_check） | 0.446 pass | 0.192 fail |
| TWR | 1.6–3.0 | 2.24 pass（MTOW 时 1.96） | 5.2 fail |
| 8 m/s 风悬停倾角 | 5°–15° | 7.85° pass | c_rd=8.06e-4 时约 49° fail |
| J_xx = J_yy（X 型对称），J_zz > J_xx | — | pass | 0.011 ≠ 0.015 fail |
| CdA/m | 0.005–0.02 m²/kg | 0.0100 pass | — |
| m ≤ MTOW | — | pass | — |

### 10.4 初版参数（全文见 `.cache/research/g08/vehicles/p600/params.yaml`，实现时移到 `vehicles/p600/params.yaml`）

| 参数 | 值 | 置信度 | 来源 |
|---|---|---|---|
| 质量 | 3.5 kg（MTOW 4.0） | B | 厂商 3.3 kg，用 MID-360（265 g）换掉 G1（104 g）后约 3.46 kg |
| 臂长（x/y） | 0.2121 m | B | 对角轴距 600 mm；SDF ±0.212132 |
| T_max/桨、ω_max | 19.23 N、1500 rad/s | C | SDF `motorConstant 8.54858e-6 × 1500²`（`rotorVelocitySlowdownSim` 只影响可视关节）；与 Prometheus 的 `hov_percent 0.47` 基本相符 |
| km | 0.016 m → Q_max 0.31 N·m | D | x500；SDF 的 0.06 是 Iris 模板值 |
| τ_motor | 0.04 s | D | r20 估计（13–15 英寸桨） |
| 惯量 | (0.0548, 0.0548, 0.101) kg·m² | D | `J_x500 × (m/2.064) × (0.300/0.246)²` |
| CdA、c_rd | 0.035 m²、1.05e-4 | D | §6.3 |
| 电池 | 6S、10 Ah、1.2 kg；P_hover 约 515 W | B/D | 厂商规格；由 22 min 续航反推 |
| 抗风 | 13.8 m/s（6 级） | B | 厂商规格 |
| 限速配置 | 默认 prometheus_outdoor | B | Prometheus `uav_control_outdoor.yaml` |

P600 在 125 Hz 下的实测：
- `px4_default` GoTo 50 m：巡航 4.98 m/s，t90 10.66 s；
- `prometheus_outdoor`：巡航 3.02 m/s，t90 16.3 s，最大倾角 17.6°；
- `prometheus_command`：1.04 m/s；
- offboard 阶跃：vmax 11.76 m/s，超调 1.54 m；
- 中度湍流加 12 m/s 侧风：pos_err 最大 1.82 m，无 guard 事件。

**辨识流程（V0.4，替换所有 C/D 级参数）**：
1. 称重，得到 m（A）；
2. 从 ULog 读 `hover_thrust_estimate`，得到 T_max（A）；
3. 用 offboard 阶跃和 DO_REPOSITION 各飞 50 m，按 §9.3 的方法对照，得到 τ 和惯量；
4. 在不同风天悬停，用 `tan θ = F(v)/(m·g)` 在两个以上风速下拟合 CdA 和 c_rd；
5. 用续航反推 P_hover。

结果写回 yaml，把 `status` 改为 `identified`，并生成对应的 SIH 机架文件 `10050_sihsim_p600`，让 P600 也有自己的 SIH 黄金数据。

---

## 11. 1000 架 CPU 预算

### 11.1 实测（单位：核·秒 / 仿真秒；host load 13–19 / 8 核，偏悲观）

**numpy 参考实现，全 pipeline**（`bench_g08.py`：composite 气动、Dryden 开启、GoTo 中）：

| 配置 | N=1 | N=100 | N=1000 | N=2000 | 1000 架时的分项（ms/仿真 s） |
|---|---|---|---|---|---|
| L1 @250 Hz | 0.470 | 0.597 | **1.558** | 2.496 | pos_ctrl 696、refgen 323、att 252、aero+integrate 153、env 78、guard 43 |
| L1 @125 Hz | 0.271 | 0.346 | **0.872** | 1.284 | pos_ctrl 343、refgen 177、att 121、env 105、aero+integrate 76、guard 46 |
| L1 @100 Hz | 0.236 | 0.261 | 0.674 | 1.186 | pos_ctrl 272、refgen 136、att 87、env 77、aero+integrate 59、guard 37 |

**numba 融合 L1 核**（`fleet_nb.py`：refgen + pos_ctrl + att + motor + composite 气动 + 积分，逐机循环，不含 env 和 guard）：

| 频率 | N=1 | N=100 | N=1000 | N=2000 | N=5000 |
|---|---|---|---|---|---|
| 250 Hz | 0.0014 | 0.017 | **0.125** | 0.239 | 0.613 |
| 100 Hz | 0.0005 | 0.0045 | **0.046** | 0.082 | 0.218 |

折算约 0.46–0.50 µs/机/步（在负载下测得）。numpy 的固定开销约 1.9–2.4 ms/步，所以即使只有 1 架机也要 0.24–0.47 核。这对笔记本演示不友好，也是 numba 要从 MVP 就上的主要原因。r20 的"1000 架 @250 Hz 3.5 ms/步"只算了控制加积分，折算为 0.875 核；全 pipeline 实测为 6.2 ms/步，折算 1.56 核。00-index §3.9 的这句话应当修正。

### 11.2 预算（1000 架，RTF = 1，FleetSim 进程）

| stage | 频率 | 实现 | 预算（核） |
|---|---|---|---|
| L1 核（refgen…integrate） | 125 Hz | numba | 0.06（实测 100 Hz 为 0.046、250 Hz 为 0.125） |
| env（均值风库 gather + 湍流 box 或 dryden） | 50 Hz | numpy | 0.08（实测 77–105 ms/s） |
| guard | 50 Hz | numpy | 0.05（实测 37–46 ms/s） |
| contact（DSM 双线性 + voxel 碰撞） | 125 Hz | numba | 0.01（估算） |
| sensors、battery、fleet_guard（网格哈希） | 50 / 10 / 10 Hz | numpy | 0.03（估算） |
| tap（ENU 转换 + 写 StateRing） | 125 Hz | numpy | 0.02（G5：1000 架每帧 publish p50 77 µs） |
| **合计** | | | **约 0.26 核**（上限 1.0 核） |

**规则**：
1. MVP 就上 numba 核（`sim/fleet/kernels_l1.py`，`@njit(cache=True)`，首次编译 2–5 s，放在启动阶段预热）。numpy stage 保留为 oracle，CI 用 R9 对拍。
2. 没有 numba 时（例如调试），numpy 只保证 ≤300 架 @125 Hz（约 0.5 核）。
3. 1000 架以上仍用单进程。5000 架 @125 Hz 的 L1 核约 0.3–0.4 核，瓶颈转到 env 与 tap，按 world 分片再多进程（G5）。
4. 快进 ×N 时，预算按 N 线性放大。1000 架 ×10 约 2.6 核，超过单核（sim-core 按 G5 钉在 1 核）时 Timeline UI 降档，并提示"RTF 受限"；快进期间 tap 可降到每 10 个 tick 一次，env 与 guard 不降。

---

## 12. 对既有文档的修正

| 文档 | 原表述 | 修正 |
|---|---|---|
| 00-index §0/§3.9 表 | "1000 架 @250 Hz 单核 3.5 ms" | 控制加积分为 3.5 ms/步（0.875 核）；全 pipeline numpy 为 1.56 核（@250 Hz）或 0.87 核（@125 Hz）；numba 为 0.06 核（@125 Hz）。定稿为 250 Hz 主时钟、L1 每 2 个 tick 执行、numba 核 |
| 00-index §3.9 | 限速 `min(vmax, Kp·d, √(2·0.7·amax·d))` 加条件积分 | L1 用 PX4 `vmax_from_dist(j,a,d)` 加 PX4 原生 ARW 加 time_stretch；r23 公式只用于 VELOCITY 模式的围栏方向限速 |
| 00-index §3.9 | "在此之前 Mock 默认用 x500 参数（2.064 kg）" | CI 用 x500；产品默认 p600_mid360 placeholder（3.5 kg）；限速配置默认 prometheus_outdoor |
| 00-index §3.9 stage 列表 | mission → controller → env_wind → … → safety → telemetry_tap | 按 §3.2 定稿：env 放到控制器之前；guard 只发事件，下一 tick 的 ingest 执行；faults 分成执行器类（plant 前）和传感器类（sensors） |
| r20 §3.10 | mock L1 GoTo：巡航 5.0–5.25、acc 3.3、倾角 22°、t90 10.4 | 那是缺少 time_stretch 的结果；补上后为 4.97、2.97、19.7°、10.70，更接近 SIH |
| r20 §3.6 / r23 §3.8 | 组合气动的 CdA 和 c_rd "待标定" | x500：CdA 0.02065、c_rd 8.06428e-5（§6.2）；P600：0.035、1.05e-4（D 级） |
| r24 §4.x `control.mock_cascade` | eland 1.5 m、failsafe 2.5 m；`max_throttle 0.8` | PX4-lite 下改为 3.0 m（持续 0.5 s）和 5.0 m；油门改为"≥0.95·THR_MAX 且掉高 0.5 m，持续 1 s"（§7.1） |
| g06 §5.5.3 | Dryden 在物理坐标下保存状态，按 env tick 更新 T | 必须用标准化状态（§8.2），否则 V 变化时发散 |
| r17 §3.3(b) | "dt=0.02 时 σ 偏高约 10%" | 样本误差（g06）；标准化精确离散在 0.01–0.05 下为 ±1.3%（§8.3） |
| r19 §3.x、n03 §3.1 | P600 初值取 1.505 kg；或 3.5 kg、T/W=2、ω_max 900 | 按 §10 规则：3.5 kg（B）、19.23 N/桨、ω_max 1500（C）；1.505 kg 被自洽检查拒绝 |
| G5 §3.2、C2 | StateRing `publish_hz = min(100, physics_hz)`；sim-core 用 numpy；1000 架 @250 Hz 不可行，交 G8 决定步长 | 主时钟 250 Hz 时 100 Hz 不能整除，改为每次 L1 更新后发布（125 Hz，约 0.02 核）；L1 热路径用 numba 核后，1000 架全 pipeline 约 0.26 核 |
| n03 §3.5 | FleetSim 频率分级为控制 100 Hz、物理 250 Hz | 100 Hz 不能整除 250 Hz（Crazyflow 门控会退化为每 3 步一次，即 83 Hz）；改为主时钟 250 Hz、L1 每 2 个 tick（125 Hz） |

---

## 13. 实施清单与文件落点

```text
sim/fleet/state.py           FleetState（§2）、slot 分配、profile gather
sim/fleet/pipeline.py        Stage/Pipeline/StageCtx（§3.1），默认 pipeline 工厂 build_default(cfg)（§3.2）
sim/fleet/fleet.py           FleetSim：add/remove/submit/step/snapshot/checkpoint/restore
sim/fleet/px4lite.py         pos_ctrl / att_ctrl_l1 / motor（numpy oracle，§4）
sim/fleet/setpoint.py        refgen（PositionSmoothing-lite + time_stretch + STOP_MOTION，§5）
sim/fleet/aero.py            composite / linear（§6）
sim/fleet/kernels_l1.py      numba 融合核（§11），签名与 oracle 相同
sim/fleet/profiles.py        VehicleProfile 加载、自洽检查、限速配置（§10）
sim/safety/fast_guard.py     §7.1 阈值表（参数化：profile × 后端）
sim/safety/geofence.py       path_valid(polyline, buffer)，STOP_MOTION 折线（§5.3）
environment/wind/turbulence.py  DrydenBank（标准化，§8.2）；TurbBox（g06）
vehicles/x500/params.yaml    回归机体（linear 与 composite 两套气动）
vehicles/p600/params.yaml    §10.4
tests/golden/…、tests/sim/…  §9.5
```

```python
# sim/fleet/fleet.py —— 对外 API（Gateway、Agent Runtime、Safety 通过它访问机群）
class FleetSim:
    def __init__(self, cfg: FleetConfig, env: EnvironmentService, world: WorldQuery, bus: EventBus, seed: int): ...
    def add(self, profile: str, spawn_enu: np.ndarray, yaw_enu: float, agent_no: int,
            fidelity: Literal["L1","L2","HIL"] = "L1") -> int: ...            # 返回 slot
    def remove(self, slot: int) -> None: ...
    def submit(self, cmd: FleetCommand) -> Admission: ...     # GOTO/PATH/HOLD/OFFBOARD_POS/VELOCITY/TAKEOFF/LAND/RTL；
                                                              # 同步准入（围栏折线、FSM 白名单），通过后在下一 tick 生效
    def step(self, n_ticks: int = 1) -> None: ...             # 主时钟 tick（4 ms）
    def snapshot(self) -> StateBatch: ...                     # ENU/FLU，tap 使用
    def checkpoint(self) -> bytes: ...; def restore(self, b: bytes) -> None: ...   # 含 RNG 与 DrydenBank 状态

@dataclass(frozen=True)
class FleetConfig:
    tick_hz: int = 250; l1_every: int = 2; env_every: int = 5; guard_every: int = 5; tap_every: int = 2
    aero_model: Literal["composite", "linear"] = "composite"; turbulence: Literal["box", "dryden", "off"] = "box"
    stop_motion: bool = True; time_stretch: bool = True; stream_watchdog_s: float = 0.25
    kernel: Literal["numba", "numpy"] = "numba"
```

---

## 14. 复现命令

```bash
cd /data/projs/anet-drone/.cache/research/g08
../../../.venv/bin/python regress_g08.py        # §9.3（约 3 min）
../../../.venv/bin/python robust_g08.py         # §5.3、§7.1、§8.3、§9.4（约 8 min）
../../../.venv/bin/python calib_g08.py          # §6.2 CdA 标定与倾角曲线
../../../.venv/bin/python dryden_vary_g08.py    # §8.1
../../../.venv/bin/python bench_g08.py          # §11.1 numpy
<scratch>/nbvenv/bin/python fleet_nb.py         # §11.1 numba（pip install numba==0.67.0）
```
