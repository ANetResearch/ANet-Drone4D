# D2-FX-R2-sim 修复：仿真区域（第 2 次执行：会话成本与 D1 回归）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-sim（修复区域 sim：`python/awr/sim/**` 与 sim 相关的 M07 环境、M13 传感器、M09 安全、M18 识别物、M19 感知；按派工说明允许跨模块修改该区域相关代码） |
| 日期 | 2026-10-12（第 2 次执行）；第 1 次执行（2026-10-07，D2-AC-08、09、32）的报告原文见附录 B |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（2026-10-11 版；§3 D2-AC-02、04、07、08、26、55，§4.1、§4.5）；`.cache/impl/requests/d2-ACC-1-to-M08/M11/M17/M18/M19.md`、`d2-FX-R2-perception-to-M07/M13.md`；AWR-04 §4.4（容量模型）、ADR-033、ADR-070 |
| 分派项 | D2-AC-02（P0，3 个满载会话时创建排队，READY 158 s）、D2-AC-04（P0，并发容量：会话 RTF 最小 0–0.33、单步 p99 93–362 ms，共享展示 RTF 最小 0.727、p99 30.7 ms，sandbox-api lag p99 52.6 ms）、D2-AC-07（P0，×10 只得 ×1）、D2-AC-08（P0，sandbox100 10 架 + 30 识别物 + 感知 0.4645 核 > 0.16）、D2-AC-26（P0，`storm.rtl` 全机 RTL 单步最大中位 13.85 ms > 12）、D2-AC-55（P0，emax 资源：会话饥饿、容量接口 CPU 0.0123 核、内部档 ≥ L2 444 s、场与读数 10.18 ms/仿真秒 > 2） |
| 环境 | VMware 8 vCPU（E5-2603 v4 1.7 GHz）、Python 3.12.3、numba 0.67、numpy 2.5。同机并行运行其他工作包的批处理与测试（6–8 个 Python 进程各约 80% CPU），本次 load 4–17（多数时间 10–12）；**没有排他锁内的正式基准**（并行阶段不跑性能基准），下文 CPU 数字均为修复验证用的诊断自测并标注负载 |
| 约束执行 | 只改 sim 区域相关文件（第 2 节）；全部改动为**逐位等价**的成本削减（第 5 节对拍），跨所有权的改动以知会请求交所有者复核（第 8 节）；没有安装依赖；没有执行 git 写操作；没有放宽或冻结任何阈值；无 emoji 与禁用字形；UI 与颜色未涉及 |
| 结论 | 六项的共同根因是**会话 sim-core 成本为容量模型的 2–3 倍**（验收 §4.1），其中识别物 stage 232.9、感知 stage 66.8 ms/仿真秒是最大两项。本次把识别物运动与判据、TargetLite32 打包、电量积分、计数器 RNG、路径光学厚度与感知平台收集改为 numba 核或缓存（与原实现逐位相同），**同一沙盒组合在生产口径 smoke 中 0.404 → 0.213–0.236 核（约 -45%）、单步 p99 5.6 ms（≤ 8）**，进程内（与生产相同的计时器）为 **0.158–0.160 核**（load 4.6；只含 D1 插件时 0.105 核）；共享展示 S7 进程内 0.331 → 0.270 核、逐 tick CPU p99 5.9 → 3.3 ms。**D2-AC-08 的 0.16 核仍不通过**（最终代码生产口径 smoke 0.2356 核 @ load 1.9）：D1 插件的基础循环在生产口径已约 0.16 核（第 1 轮验收 0.1645），即便识别物与感知降到 0 也只到阈值附近，剩余缺口在 M08 基础循环与实时唤醒开销（第 7 节第 1 条）；D2-AC-02、04、07、55 的会话饥饿随会话成本下降应明显缓解，但只能在验收阶段的 cgroup 模拟中判定；**场与读数服务 9.1 ms/仿真秒（预算 2）、容量接口 CPU、sandbox-api 事件循环延迟、全机 RTL 单步最大（D2-AC-26）本次未修复**，根因数据与建议见第 3、7、8 节。功能测试全部通过，`make lint` 通过 |

## 1 结论摘要

| AC | 第 1 轮 | 根因（本次核实） | 处理 | 自测（诊断） | 判定预期 |
|---|---|---|---|---|---|
| D2-AC-08（P0） | 0.4645 核（≤ 0.16）；识别物 232.9、感知 66.8 ms/仿真秒；基础循环 0.1645 核 | 识别物：逐识别物的 Python 运动推进（每步数十次标量 DSM 查询、多边形判定、`np.linspace`）、逐（识别物，机）对的判据与驻留、每次重写全部表列；感知：每个平台每次重建载荷视图、成像状态、传感器位姿（每次约 0.7 ms）与路径光学厚度的 numpy 小运算；另有 TargetLite32 打包约 0.55 ms/次、电量积分约 0.53 ms/次、计数器 RNG 每次 0.1–0.3 ms | 第 2 节：运动、内外判定、geometric 命中次序、视线缓存清空、打包、电量积分、RNG 外积键、光学厚度改为 numba 核；感知平台收集、成像状态、取值键、读数编码改为缓存或直接编码 | 生产口径 smoke：0.4043（load 约 3）→ 0.2134（load 6.8，主要改动之后）、**0.2356 核**（load 1.9，最终代码），单步 p99 5.6 ms、最大 7.3 ms；进程内（生产计时器，load 4.6）**0.158–0.160 核**，其中 D1 插件部分 0.105 核；识别物 stage 进程内 41.8 → 16.8 ms/仿真秒（同一基线树，第 4.2 节） | **核数仍不通过**（单步 p99 ≤ 8 ms 子项预计通过）；需 M08 降低基础循环 |
| D2-AC-02、04、07、55 会话部分（P0） | 3 个满载会话即 L2/L3，创建排队、RTF 饥饿、×10 被降为 ×1 | 同上：池自报 c_i 0.45–0.63、满载沙盒组 0.61–0.66 核，而容量模型 c_s = 0.23（`N_max = auto` 解析为 4） | 同上（会话成本） | 单会话 sim-core 约减半（上一行）；S7 进程内 0.331 → 0.270 核、逐 tick CPU p99 5.9 → 3.3 ms（load 约 4，第 4.3 节） | 需在 cgroup 模拟中复测；会话 sim-core 已回到模型 0.23 核的量级 |
| D2-AC-55 场与读数（P0） | 10.18 ms/仿真秒（≤ 2） | 逐对读数每帧先组装 QuantFrame 对象再列式量化、每次 `wanted()` 遍历兴趣集、`imaging_state_spec` 每次重建 | 读数直接列式编码（`encode_pairs`，与 `encode_soa(quant_frame(...))` 逐字节相同）、`wanted()` 记忆、成像状态规格记忆 | 11.4 → 9.1 ms/仿真秒（线程 CPU，load 约 11） | **仍不通过**（第 7 节第 2 条、d2-FX-R2-sim-to-M19） |
| D2-AC-55 容量接口 CPU、内部档 ≥ L2；D2-AC-04 sandbox-api lag（P0） | 0.0123 核（≤ 0.01）；444 s（≤ 10 s）；p99 52.6 / 61.6 ms（≤ 20） | 内部档 ≥ L2 与 lag 主要随会话饥饿出现；容量接口已有 `CapacityCache`，剩余为每请求的 ASGI 与序列化开销（sandbox-api，M17/M11） | 本工作包未改 api（不在 sim 区域） | — | 交 M17、M11（d2-FX-R2-sim-to-M17） |
| D2-AC-26（P0） | `storm.rtl` 单步最大 14.78、13.85、6.14 ms（≤ 12，离散度 62%） | 进程内 N = 1000、D1 组合：RTL 后 1 s 内每 tick CPU 最大 7.3 ms（load 11），构成为 mission_engine 1.6（每次处理 16 个任务结果，5 Hz）、fsm 1.1–1.3（`_rtl_phase` 与走廊重算）、l1 0.7–1.2、ingest 0.7 ms；生产口径的 13.85 ms 为墙钟，另含同机抢占与后台线程 | 本次没有改动这些路径（第 7 节第 4 条） | 本次改动不在该 tick 的构成中 | **未修复**，数据与建议交 M09、M10 |

## 2 实现清单

全部改动在 numba 不可用（`AWR_KERNEL=numpy`）时回到原 numpy 或 Python 实现；所有新核由各插件 `install()` 的 warmup 按运行期签名编译或读缓存，
`make numba-warm`（装配 D2 插件）首次 43.9 s、缓存命中后 1.5 s，主循环内不编译（进程内 `SIGCHECK` 诊断确认运行期签名都已预热）。

### 2.1 M18 识别物（M18-FR-010 至 FR-013、FR-020、FR-023，M18-NFR-001；D2-AC-08、04、55）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/targets/kernels_motion.py`（新） | `step_all`：静止、随机游走（区域、平地约束、可通行前视、绕行次序）、路径（三种循环、驻留、越点判据）与 hold/look/move 行为覆盖的完整推进，按行读写运动状态 SoA（23 列）；`dsm_at`、`passable`（与 `DsmWorldQuery.height_dsm` 最近邻取值、`np.linspace` 采样逐位相同）；`py_hypot`（CPython 3.12 `math.hypot` 的逐位复刻，含 Dekker 精确乘积；numba 的 `math.hypot` 走 libm，与 CPython 不同）；`inside_all`（全部启用敏感体 × 参与者的内外与斜距，四种形状，与原 numpy 表达式同一归约次序）；`geo_hits`（geometric 判据的命中次序：每架机取首个包含它的体）；`warmup()` |
| `python/awr/sim/targets/motion.py` | `MotionState` 改为数组承载（`__slots__`，字段为 SoA 行上的属性；`pos` 读出副本、赋值写回；`row=` 时为运行时 SoA 的视图，`detach()` 改为自带行）；`DsmGrid` 与 `dsm_of(world)`（只对 `DsmWorldQuery` 且 float32 栅格给出）；`_ground`、`passable`、`project_into` 走栅格与 Python 浮点；运动参数按配置对象缓存 |
| `python/awr/sim/targets/runtime.py` | `stage()`：识别物按核批量推进（`_motion_batch`，核参数表按配置与区域对象缓存；合成世界与 numba 不可用时逐个 `step_motion`）；静止无覆盖的识别物不进核；声相位用预取的浮点；`_inside_all_r3`（核求内外矩阵与斜距；`_inside_all` 保留原两元返回）；geometric 判据 `_evaluate_geo`（逐对 Python 只在命中的机上执行，命中信息为 (kind, vol_idx, 斜距) 元组，`_hinfo` 按需展开）；FSM 只在有命中、延期或非 UNAWARE 时调用；`_write_rows` 静态列只在表、行或配置版本变化时重写，动态列一次从 SoA 取出；`detail()` 的静态体与有效半径按 (rev, 配置, 机型, 环境键) 记忆；行删除时 `detach` |
| `python/awr/sim/targets/behavior.py` | `occupied()` 用 `tolist()` 取布尔行 |
| `python/awr/sim/targets/kernels_los.py`、`los_cache.py` | `M_HWM`（自上次清空以来取用的最大条目号）与 `clear_used`：只清空用过的部分（感知 full 口径每次调用清空一次，此前整表约 2.5 万次写入）；`LosCache.clear()` 改用之 |
| `python/awr/sim/targets/plugin.py` | `_warm_kernels()`：`kernels_motion`、`kernels_los`、计数器 RNG 外积核、`tap_targets` 打包核的预热 |

### 2.2 M08 实体发布（M08-FR-217；D2-AC-08）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/fleet/stages/tap_targets.py` | `_pack_nb` 打包核（位段布局以 `_NB_BITS` 写死，装载时与 `TARGET_LITE32_BITS` 比对，不一致或列 dtype 不在核所设范围时走原 numpy 实现）；缺列以零列代替；`warmup()` |

### 2.3 M09 电量（M09-FR-050、FR-051；D2-AC-08）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/safety/kernels.py` | `bat_step`：能量积分、soc、显示百分比、平均功率、电流电压、剩余时间的逐机一次遍历（`math.pow(ratio, 1.5)`、`np.round`、float32 写入与 numpy 实现逐位相同），返回保守的"需执行判据"标志（任一档位条件接近、在地面或 RTL 缓存无效）；warmup 覆盖两种悬停表布局 |
| `python/awr/sim/safety/battery.py` | `step()` 有 numba 时走 `bat_step`，后半段抽为 `_finish`（无电池机体固定值、无效 RTL 缓存立即刷新、按标志执行 `_criteria`）；判据结果与每次都执行相同 |

### 2.4 M13 传感器（M13-FR-030、M13-NFR-010；D2-AC-08）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/sensors/kernels_cbrng.py`（新） | 外积键核 `outer_keys`：K[i, j] = key(seed, stream, agent_i, tick, ch_j) 与 Box–Muller 第二键（uint64 回绕，与 numpy 逐位相同） |
| `python/awr/sim/sensors/cbrng.py` | `normal`、`uniform` 在 agent、ch 为一维、tick 为标量且 `kernels_cbrng` 已导入时走外积核；`warmup()` 导入并预热核。**本模块自身不导入 numba**（M13-NFR-010：`import awr.sim.sensors.cbrng` 无副作用，核模块由插件安装时导入） |
| `python/awr/sim/sensors/frames.py` | `quat_to_R`、`gimbal_R` 的单个输入快路径（同一表达式） |
| `python/awr/sim/sensors/payload.py` | `PayloadBank._state` 按（焦距、指令、分辨率、成像模式、照明位、源、关闭集、在用）缓存；列表与名称缓存；`view_key(s)`（载荷取值键：键相同则 `payload_view` 除云台角外逐字段相同） |
| `python/awr/sim/sensors/runtime.py`、`camera.py` | `sensor_world_pose1(s, sensor, pq, Rb)`（单机单传感器位姿，与 `sensor_world_pose` 逐位相同，机体旋转可共用）；`payload_view_key`；`world_pose(..., Rb=None)` |
| `python/awr/sim/sensors/plugin.py` | warmup 增加计数器 RNG 外积核 |

### 2.5 M19 感知（M19-FR-003、FR-060、M19-NFR-002、NFR-007；D2-AC-08、55）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/perception/runtime.py` | `collect()`：机体位姿一次取出（成像与非成像共用），成像传感器位姿走 `sensor_world_pose1` 并共用机体旋转；机型、签名、声功率按 (slot, profile_id, frame_kind) 缓存；成像状态与非成像传感器表按 M13 取值键复用（`_view_entry`，rig 对象同一性校验）；full 口径空片早退；`capture_detail` 用快照的 id 索引；非成像等级表按统计调用与传感器表身份缓存 |
| `python/awr/sim/perception/specs.py` | `imaging_state_spec` 按（规格对象、分析、状态种类、分辨率、照明器、焦距、模式、有效孔径、F 数、抖动、曝光、视场）记忆（NaN 归一） |
| `python/awr/sim/perception/quant.py` | `encode_pairs(target, mode, t_ns, pairs)`：从 `quant_pairs` 结果直接量化为列式 SoA，与 `encode_soa(quant_frame(...))` 逐字节相同 |
| `python/awr/sim/perception/field.py` | `tick` 用 `encode_pairs`；`quant()` 经 `_quant_pairs`；`last_quant` 改为按需展开；`wanted()` 按（兴趣下推、本地订阅、M18 rev 与实体表版本）记忆 |

### 2.6 M07 环境（M07-FR-006、FR-020；D2-AC-08）

| 文件 | 改动 |
|---|---|
| `python/awr/environment/atmosphere/kernels_od.py`（新） | `od_pairs`：逐对路径光学厚度（霾、雾层、降水层、Kim 波长缩放、LWIR 水汽项），与 `optical_depth_arr`、`optical_depth_lambda_arr` 逐元素同式 |
| `python/awr/environment/field.py` | `optical_depth` 有 numba 时走 `od_pairs`；`_sd_at(t)`：稳态关键帧下 (标量, 派生量) 按 (过渡, 剖面) 缓存（与 `_eval_now` 同一判据） |
| `python/awr/environment/stage.py` | warmup 增加 `kernels_od` |

### 2.7 新增测试（等价性守护）

| 文件 | 内容 |
|---|---|
| `tests/targets/test_motion_kernel.py` | 运动核与 `step_motion` 随机对拍（2 个种子 × 40 组配置，无世界与 shenzhen DSM，hold/look/move 覆盖，> 1000 步全部字段逐位相同）；`_inside_all_r3` 核与 numpy 路径（四种形状、随机方位偏移、参与者与识别物重合）逐位相同、`_inside_all` 两元接口；`clear_used` 与 `clear` 结果相同 |
| `tests/sim/test_tap_targets_pack.py` | 打包核与 numpy 实现在随机列值（速度饱和、警觉度越界、位段超宽、空洞行、缺列）下逐字节相同 |
| `tests/safety/test_battery_kernel.py` | 电量核与 numpy 路径 3 个种子 × 400 步：电池状态块逐字节相同、登记事件与仲裁候选相同，且 LOW/CRIT 等判据确有触发 |
| `tests/sensors/test_cbrng_kernel.py` | 子进程中先以 numpy 路径求 301 组参考值、再导入核比较，逐位相同；导入 `cbrng` 时没有 numba |
| `tests/environment/test_od_kernel.py` | 全部 12 个天气预设 × 550/850/905 nm 与 LWIR，含零长度、水平、竖直与穿越雾顶云底的线段，核与 numpy 逐位相同 |
| `tests/perception/test_encode_pairs.py` | 三种规模（12 × 30、3 × 5、1 × 2）、含单机过滤，`encode_pairs` 与 `encode_soa(quant_frame(...))` 的 msgpack 字节相同 |

## 3 定位方法与根因

### 3.1 方法

- **进程内逐 tick 推进**（附录 A，第 1 次执行的方法加识别物放置）：`ladder-shenzhen` n10、sandbox100、`AWR_PLUGINS_EXTRA` 追加 D2 插件、`target/add` 放置 30 个识别物、
  `AWR_PERCEPTION_MODE=full`，伪墙钟逐 tick 推进，40 s 预热后取 3–6 个窗口的最小值；cProfile 只包住单个 stage（`--pstage`），统计采样与类级插桩定位热点。
  **注意**：诊断脚本把 stage 计时器换成 `time.thread_time_ns`（不含被抢占的等待）时，每次计时约 3.3 µs（`perf_counter_ns` 约 0.25 µs），每仿真秒约 2 万次计时会把
  进程 CPU 抬高 0.02–0.03 核；与生产同口径的总 CPU 用 `perf_counter_ns` 计时器另测（第 4.1 节），stage 间比较用前者。
- **生产口径 smoke**：`tools/bench/fleet_ladder/run.py --n 10 --clock sandbox100 --targets 30 --perception full --smoke`（真实 supervisor + sim-core）。
- **逐位对拍**：对每项改动以"基线树"（当前仓库树中把本次改动的文件换回改动前快照，其余文件与并行工作包的改动保持当前）与当前树分别跑同一进程内剧本，比较
  识别物轨迹、感知 PairTable、全部事件（去掉 seq 与墙钟字段）、FleetState 全部数组与插件状态块的哈希（第 5 节）。并行工作包同期改动 M09、M20、M21 与合同，
  只有这种对照能把差异归因到本工作包。

### 3.2 识别物 stage（验收 232.9 ms/仿真秒）

cProfile（10 仿真秒、30 识别物、10 架）：`step_motion` 逐识别物推进占约 70%（每个随机游走识别物每步 10–20 次标量 DSM 查询、`np.linspace` 前视采样、
多边形判定、`np.clip`），`_evaluate` 逐（识别物，机）对约 15%，`_write_rows` 每次重写全部列约 8%。运动的每一步对所有识别物独立，按行批量推进不改变结果；
难点是逐位相同：numba 的 `math.hypot` 走 libm 与 CPython 不同（40 万组随机值中大量 1 ulp 差异），因此实现了 CPython 3.12 `vector_norm` 的逐位复刻；numpy `x**3`
即 `pow(x, 3.0)`（不是 `x*x*x`）；3 × 3 矩阵乘积保留 numpy（BLAS 与朴素循环不同）；`np.linalg.norm` 的归约次序为 ((dx² + dy²) + dz²)；numba 浮点 `%`、
`math.ceil`（返回整数）与除零的行为逐项核对。

### 3.3 感知 stage（验收 66.8 ms/仿真秒）

每次 stage（100 Hz，每次一个平台分片）的 `collect()` 约 0.7 ms：逐传感器 `payload_view`（重建成像状态与非成像表）、`sensor_world_pose`（每传感器重算机体
旋转）、`imaging_state_spec`（每次重建规格）；另有 `EnvironmentField.optical_depth` 每次约 0.24 ms（`scalars` + `derive` 与十余次 numpy 小运算）、计数器 RNG
每次 0.1–0.3 ms、视线缓存每次整表清空。载荷视图的取值键（M13 `view_key`）不变时成像状态与传感器表不变，位姿仍每次按当前姿态与云台角重算。

### 3.4 其余

TargetLite32 打包（10 Hz）每次约 50 次字段赋值与位运算 0.55 ms；电量 stage（10 Hz）每次约 20 次 numpy 小运算与判据 0.53 ms；`m18.detail` 慢任务
（`publish_detail` 约 2.3 ms/次）中的静态体与有效半径重算。

## 4 测量（修复验证用诊断，非正式基准）

### 4.1 D2-AC-08 口径（10 架、30 识别物、`--perception full`、×1）

| 运行 | 负载 | sim-core（核） | 备注 |
|---|---|---|---|
| 第 1 轮验收（生产，3 次中位） | 0.9 | 0.4645 | 识别物 232.9、感知 66.8 ms/仿真秒；只含 D1 插件 0.1645 核 |
| 生产口径 smoke，本次开始前 | 约 3 | 0.4043 | 单步 p99 > 8 ms |
| 生产口径 smoke，主要改动之后 | 6.8 | **0.2134** | 单步 p99 7.5–8.2 ms；墙钟 stage（ms/仿真秒）感知约 44、识别物约 26、env 约 17、sensors 约 15.5、mission 约 11.8、cmd_watch 约 10.4、l1 约 9.7 |
| 生产口径 smoke，最终代码 | 1.9 | **0.2356** | RTF 1.000、单步 p50 / p99 / 最大 2.30 / 5.62 / 7.31 ms；stage（ms/仿真秒）感知 46.5、识别物 26.0、env 17.3、sensors 16.1、mission 11.4、cmd_watch 10.9、l1 9.4、guard 8.9、tap 5.4、collide 4.8、battery_rtl 4.6、fleet_guard 4.1、mission_guard 3.5、contact 3.5、tap_targets 2.1、battery 0.7；慢任务 `m18.detail` 7.3、state_ext 4.2、`m13.payload_pub` 2.8 |
| 进程内，`perf_counter_ns` 计时（与生产同口径），最终代码 | 4.6 | **0.158–0.160** | stage 合计约 124、慢任务约 16、其余约 19 ms/仿真秒 |
| 同上，只含 D1 插件、无识别物 | 4.6 | 0.104–0.105 | 第 1 次执行后的基础循环 |

进程内与生产的差距（实时唤醒的冷缓存、zenoh 与后台发布线程）在第 1 次执行中为 1.4–1.8 倍、本次约 1.5 倍（同一 stage 在生产中贵约 1.4–1.5 倍）。
**D2-AC-08 的核数判据仍不通过**：D1 插件的基础循环在进程内 0.105 核、生产约 0.16 核（第 1 轮验收 0.1645），已等于全部预算（AWR-04 §4.4.1 给基础循环的份额约
0.12 核）；D2 部分（生产口径感知 46.5、识别物 26.0、`m18.detail` 7.3、`tap_targets` 2.1 ms/仿真秒，合计约 0.08 核）仍高于其份额（约 0.03 核）。单步 p99 ≤ 8 ms
子项（第 1 轮 26.3 ms）在最终代码 smoke 中为 5.6 ms。

### 4.2 stage 对照（进程内，`thread_time_ns` 计时，基线树与当前树交替，load 约 4.7）

基线树是本次快照（取于本次最早的识别物改动之后，因此识别物一项已低于验收值）；验收值为生产口径。

| stage（ms/仿真秒） | 第 1 轮验收（生产） | 基线树 | 当前 |
|---|---|---|---|
| perception | 66.8 | 51.3 | **30.2** |
| targets | 232.9 | 41.8 | **16.8** |
| `slow:m18.detail` | 11.9 | 9.1 | **5.0** |
| tap_targets | 5.4（第 1 次执行） | 3.3 | **1.5** |
| battery | — | 3.6 | **0.46** |
| 进程 CPU（核） | — | 0.241 | **0.182** |

单项微基准（热缓存）：电量 stage 530 → 40 µs/次，打包 552 → 94 µs/次，`optical_depth` 240 → 10 µs/次，感知 `collect` 约 700 → 180 µs/次；识别物台架
（进程内 30 识别物，本次开始前的代码）299 → 15.4 ms/仿真秒。

### 4.3 共享展示 S7（D2-AC-04、55 的共享展示部分）

进程内 `scenarios/s7-synthcity-relay.json`、d1_250，基线树与当前树交替（`thread_time_ns` 计时，load 约 4）：

| 树 | 进程 CPU（核） | stage 合计（ms/仿真秒） | 逐 tick CPU p50 / p99（µs） |
|---|---|---|---|
| 基线 | 0.331–0.347 | 234–247 | 837 / 5947 |
| 当前 | **0.270–0.283** | 180–190 | **779 / 3332** |

验收的共享展示单步 p99（6 h 浸泡 12–15 ms、变体 A/D 30.7–33.2 ms）是墙钟并受会话饥饿影响；逐 tick CPU p99 已降到 3.3 ms，满载下 ≤ 3 ms 仍需复测。

### 4.4 场与读数（D2-AC-55，`tests/perception/test_field_budget.py` 的同一场景）

线程 CPU（load 约 11）：开始 11.4、最终 **9.1 ms/仿真秒**（预算 2）。cProfile 构成：逐对读数 `quant_pairs` 约 40%（每帧 12 机 × 声学、视觉、雷达的逐对计算）、
感知体表 `volumes` 约 20%（关键帧未命中时 `volume_table` 与声学半径求解）、剖面 `profiles` 约 15%、编码约 15%。同一场景的 perf 用例在 load 11 时为 18.5 ms/仿真秒
（墙钟，不作判定）。

### 4.5 全机 RTL（D2-AC-26）

进程内 N = 1000、`ladder-shenzhen` n1000、d1_250、D1 插件、规划池为进程模式（与生产相同），RTL 下发后 15 s（load 11）：逐 tick CPU p50 2.34 ms、p99 6.08 ms、
**最大 7.34 ms**（RTL 后 0.83 s），最大的 5 个 tick 均在 RTL 后 1 s 内，构成为 mission_engine 1.6、fsm 1.1–1.3、l1 0.7–1.2、ingest 0.7 ms。mission_engine 每次
（5 Hz）处理 `RESULTS_PER_STAGE = 16` 个任务结果（中止、交还租约、事件），每个约 0.1 ms，1000 个结果分摊约 12 s；fsm 的 `_rtl_phase` 每 tick 至多 16 架的
走廊重算。验收的 14.78、13.85、6.14 ms 是 1 s 窗口内的墙钟最大值（离散度 62%），与进程内 CPU 最大值相差约 2 倍，差额来自同机抢占与后台线程。
另：`tests/safety/test_storm.py::test_storm_1000_transitions_budget`（M09-NFR-004，perf，≤ 4 ms）在 load 11 下基线树与当前树均约 12.7 ms CPU，主要在
`SafetyEventSink.flush` 的 2000 次 emit；本次未改动。

## 5 等价性验证

| 对拍 | 结果 |
|---|---|
| 沙盒组合（ladder-shenzhen n10、30 识别物、`--perception full`、10 仿真秒，基线树 vs 当前树） | 识别物轨迹、感知 PairTable、事件（2587 条）、FleetState 与 battery、fw、mission、payload、safety、sensor_targets、sensors 块哈希**全部相同**（每项改动后重复，最终一次在 cbrng 调整之后） |
| 共享展示 S7（d1_250，6 仿真秒） | 同上**全部相同**（177 条事件） |
| 电量判据变体（一半机体外部耗电，跨 LOW/CRIT/EMERG） | 相同 |
| 场与读数（`test_field_wire` 场景，逐帧发布字节） | 相同 |
| 运动核随机对拍（种子 1–4、7，各 300 组配置） | 0 差异 |
| 新增 6 个测试文件（第 2.7 节） | 全部通过 |

## 6 测试结果

### 6.1 功能测试

| 范围 | 结果 |
|---|---|
| `tests/sensors`、`tests/targets`、`tests/perception`、`tests/environment`、`tests/safety`（一次运行，未加 `-m` 过滤，load 约 11） | 911 passed、1 skipped；6 个失败**全部是 perf 标记的计时用例**（`tests/targets/test_budget.py`、`tests/perception/test_budget.py::test_stage_cost_12x30`、`test_field_budget.py`、`tests/environment/test_stage_bench.py`、`test_turb_box.py::test_generation_time`、`tests/safety/test_storm.py::test_storm_1000_transitions_budget`），高负载下超时，不作判定（并行阶段不跑性能基准；其中 2 项在第 1 轮验收中同样不通过） |
| 新增用例（第 2.7 节，单独运行） | 26 passed |
| `tests/sensors/test_imports.py`（M13-NFR-010） | 3 passed（中途版本的 `cbrng` 在模块顶层导入 numba 使该用例失败，已改为独立核模块，见第 7 节第 6 条） |
| `tests/perception/test_quant.py` | 16 passed（中途版本把 `_inside_all` 改为三元返回使 1000 对一致率用例失败，已恢复两元接口并新增 `_inside_all_r3`） |
| 最终代码复跑（`-m "not perf"`，同机并行负载下） | `tests/sensors`、`tests/targets`、`tests/perception`、`tests/environment`、`tests/safety` 与 `tests/sim/test_tap_targets_pack.py`：**934 passed**、10 deselected；`tests/sim`、`tests/mission`、`tests/swarm`、`tests/catalog`：**591 passed**、12 deselected；`tests/tasking`、`tests/sandbox`、`tests/planning`：**404 passed**、1 deselected；`tests/e2e` 中涉及识别物与感知的 6 个文件（`test_field_sampling_e2e`、`test_sandbox_bench_tools`、`test_sandbox_sdk_e2e`、`test_relay_batch_tool`、`test_sandbox_park`、`test_sandbox_templates`）：**160 passed**、36 skipped（标记跳过） |
| `make numba-warm` | 通过（首次 43.9 s，缓存命中 1.5 s） |

### 6.2 `make lint`

最终代码 `make lint` 退出码 0（ruff、oxlint、tools/lint 全部规则，含 no-emoji、lint-lf、check-units）。

## 7 偏差与遗留

| # | 项 | 说明 |
|---|---|---|
| 1 | D2-AC-08 核数仍不通过 | 最终代码生产口径 smoke 0.2356 核（load 1.9；进程内 0.158–0.160 核）。剩余的大头是 D1 插件的基础循环（生产约 0.16 核，进程内 0.105 核：env、sensors、mission、cmd_watch、l1、guard 等）与生产进程的实时唤醒开销（同一 stage 贵约 1.5 倍）；D2 部分中感知 stage 生产 46.5 ms/仿真秒（`collect` 仍约 40%）、识别物 26.0、`m18.detail` 7.3。要在不放宽阈值的前提下通过，需 M08 把基础循环降到约 0.10 核（生产）并由 M18、M19 继续把 D2 部分降到约 0.05 核。按 ADR-033 复测：`python tools/bench/fleet_ladder/run.py --n 10 --clock sandbox100 --targets 30 --perception full --runs 3` |
| 2 | 场与读数 9.1 ms/仿真秒（预算 2，D2-AC-55） | 需要 M19 按批计算逐对读数（numba）与感知体表的增量更新，超出等价改写的范围；见 d2-FX-R2-sim-to-M19 |
| 3 | 容量接口 CPU、sandbox-api 事件循环延迟、内部档 ≥ L2（D2-AC-55、04） | 不在 sim 区域；内部档 ≥ L2 与 lag 主要由会话饥饿引起，会话成本下降后需复测；见 d2-FX-R2-sim-to-M17 |
| 4 | 全机 RTL 单步最大（D2-AC-26） | 本次未改动相关路径。建议（M10）RTL 风暴期间减少 mission_engine 每次处理的结果数或与 fsm 重 tick 错相，或把租约交还与事件批量化；（M09）`_rtl_phase` 每 tick 重算数与 `SafetyEventSink.flush` 的逐条 emit 批量化。这些改动会改变事件的生效 tick，需所有者决定；见 d2-FX-R2-sim-to-M07-M09-M10-M13-M20 |
| 5 | M18-AC-022 台架（`tests/targets/test_budget.py`，预算 3 ms/仿真秒） | 台架使用合成世界（`FakeWorld`，没有 DSM 栅格），运动仍走逐个 `step_motion`，本次只有判据与写列部分变快（load 11 时约 1.9 ms/次 CPU）。若该台架要代表真实世界，需由 M18 决定是否让合成世界提供栅格协议；见 d2-FX-R2-sim-to-M18 |
| 6 | `cbrng` 的 numba 核 | 中途版本在 `cbrng.py` 顶层导入 numba，违反 M13-NFR-010（导入无副作用）。最终版本把核放在 `sensors/kernels_cbrng.py`，由插件 warmup 导入；`cbrng` 只在该模块已导入时走快路径（结果与 numpy 逐位相同，未导入时性能与此前相同） |
| 7 | `MotionState` 改为数组承载 | `ms.pos` 读出的是副本（此前是可原地修改的数组）；仓库内全部用法均为读取或整体赋值（已检查 `python/`、`tests/`、`tools/`）。其他代码若原地改写 `ms.pos[i]` 将不生效，需改为整体赋值 |
| 8 | 新增测试的位置 | 等价性用例放在各所有者的测试目录（tests/targets、sensors、environment、safety、perception、sim），请所有者在后续改动中保留 |
| 9 | 正式基准未运行 | 并行阶段不跑性能基准；本报告的 CPU 数字均为高负载下的诊断自测，误差约 ±10%（进程内）与 ±20%（生产 smoke） |

## 8 跨所有权请求

本次在下列请求文件中追加了"第 2 次执行（2026-10-12）"一节（`d2-FX-R2-sim-to-M08.md` 为新建）：

| 文件 | 接收方 | 要点 |
|---|---|---|
| `d2-FX-R2-sim-to-M18.md` | M18 | 本次对 `python/awr/sim/targets/**` 的逐位等价改动清单（请复核保留）；`m18.detail` 剩余约 5 ms/仿真秒；台架的合成世界路径；geometric 判据的驻留循环 |
| `d2-FX-R2-sim-to-M19.md` | M19 | 本次对 `python/awr/sim/perception/**` 的改动清单；感知 stage 剩余 30 ms/仿真秒的构成；场与读数 9.1 ms/仿真秒的构成与建议 |
| `d2-FX-R2-sim-to-M07-M09-M10-M13-M20.md` | M07、M09、M10、M13 | M07 光学厚度核与稳态缓存、M09 电量核、M13 计数器 RNG 外积核与位姿、载荷缓存（请复核保留）；D2-AC-26 的 RTL 风暴数据与建议（M09、M10）；M09-NFR-004 风暴用例观测 |
| `d2-FX-R2-sim-to-M08.md`（新） | M08 | 基础循环是 D2-AC-08 的主要缺口（生产口径 stage 构成）；S7 与全机 RTL 的数据；`tap_targets` 打包核与位段布局的同步 |
| `d2-FX-R2-sim-to-M17.md` | M17（抄送 M11） | 容量接口 CPU、sandbox-api 事件循环延迟、内部档 ≥ L2 未在本工作包处理；会话成本下降后 `N_max = auto` 与 c_s 的复核 |
| `d2-FX-R2-sim-to-M16.md` | M16 | 复测清单与命令；诊断计时器的口径提示 |
| `d2-FX-R2-sim-to-文档总编.md` | 文档总编 | 未改阈值与行为；AWR-04 §4.4.1 每会话成本份额表建议在复测后按实测更新（不在本工作包以 ADR 冻结） |

## 附录 A 诊断脚本（暂存目录，不进仓库）

- `sb100.py`：进程内 SimCore（LocalBus/LocalRing、伪墙钟逐 tick 推进、`manual_gc`），参数 `--targets`、`--d2`、`--perception`、`--clock`、`--snapshot`（S7）、
  `--rtl`（全机 RTL 与逐 tick 构成）、`--hash`（逐位对拍哈希）、`--pstage`、`--sample`、`--inst`、`--steps`；环境变量 `WALLTIMER=1` 时用 `perf_counter_ns` 计时器。
- `mkbase.sh`、`cmp.sh`：生成基线树（当前树中把本次改动的文件换回快照）并比较两棵树的哈希。
- `motion_fuzz.py`、`quant_eq.py`、`field_parity.py`、`nbcheck.py`（numba 与 numpy、CPython 数学函数的逐位核对）、`tgt_parity.py`、`field_prof.py`、`storm_t.py`。

## 附录 B 第 1 次执行（2026-10-07）报告原文

以下为第 1 次执行（D2-AC-08 基础循环与工具、D2-AC-09、D2-AC-32）的报告，标题层级下移一级，内容未改。

### B.0 D2-FX-R2-sim 第 2 轮修复：仿真区域（D2-AC-08、09、32）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-sim（修复区域 sim：`python/awr/sim/**` 与 sim 相关的 M07 环境、M10 规划池、M13 传感器、M09 安全、M20 预热、`tools/bench/fleet_ladder/**`、SDK 的限流重试；按派工说明允许跨模块修改该区域相关代码） |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（§3 D2-AC-08、09、32，§4.6、§4.7，§7）；`.cache/impl/requests/d2-ACC-1-to-M08.md` 第 2、5、6 条与 `d2-FX-R2-sandbox-to-M08-M18.md` 第 4 条；AWR-04 §4.4.1、§4.5、§13（D2-AC-08、09、32）、ADR-070、ADR-090、ADR-106；M08 PRD §15（M08-FR-221、FR-222、FR-228、NFR-201、AC-202、AC-211） |
| 分派项 | D2-AC-08（P0，sandbox100 10 架 sim-core 0.255 核 > 0.16，工具未放置识别物）；D2-AC-09（P0，实例增删改 25 次中 3 次 1.09–1.21 s > 1 s）；D2-AC-32（P1，1 h 批处理两次在 20.87 / 20.89 s 分叉，事件含墙钟字段） |
| 环境 | VMware 8 vCPU（E5-2603 v4 1.7 GHz）、Python 3.12.3（`.venv`，numba 0.67、numpy 2.5）。同机并行运行其他 D2-FX-R2 工作包与其测试，本工作包全程 load 4–20（多数时间 7–12）；没有排他锁内的正式基准（并行阶段不跑性能基准），下文全部 CPU 数字是**修复验证用的单次自测**，标注当时负载 |
| 约束执行 | 只改 sim 区域相关文件（第 2 节清单）；跨所有权的改动以知会请求交所有者复核（第 7 节）；没有安装依赖；没有执行 git 写操作（工作目录无 git 元数据）；没有放宽任何阈值、没有冻结暂定阈值；无 emoji 与禁用字形；颜色与 UI 未涉及 |
| 结论 | **D2-AC-09 根因已修复**（SDK 按整秒 `Retry-After` 退避，改为按 problem+json 的 `retry_after_ms`；复测 ≤ 0.853 s）；**D2-AC-32 根因已修复**（M10 规划结果在批处理中按到达时刻生效，改为固定延迟生效、未到即阻塞；墙钟诊断字段置 null；`relay_batch --case b --hours 1 --determinism` 逐字节一致）；**D2-AC-08 工具缺口已修复、基础循环成本下降约 45%**（同一口径 0.255 → 0.140 核，load 约 8），但沙盒组合根下识别物与感知 stage 仍是预算的数十倍（M18、M19，第 4.2 节），**D2-AC-08 在 M18、M19 修复前仍不通过**。功能测试全部通过（第 5 节）；`make lint` 中本工作包文件全部通过，最后一次运行剩余的 2 处违例在接力工作包正在修改的 `tests/tasking/test_relay.py`（第 5.3 节） |

### B.1 结论摘要

| AC | 第 1 轮 | 根因（已核实） | 处理 | 自测 |
|---|---|---|---|---|
| D2-AC-08（P0） | sim-core 0.255 核（≤ 0.16），`n_target = 0` | ①工具：剧本口径（缺省 `ladder-shenzhen`）只在骨架布设时放置识别物，且 supervisor 的 sim-core 组合根只有 D1 插件（M18、M19 未装配），`target/add` 无人受理；②基础循环：10 架时每 tick 的固定开销占主导——每个 numpy 小运算在主循环冷缓存下约 5–30 µs，sandbox100 每 tick 唤醒一次，cmd_watch 小机群走逐行 Python 判据（约 0.6 ms/次）、机间碰撞小机群走 8 套网格广播（约 0.2 ms/次）、整容量（1024 槽）的多步筛选遍布各 stage | 工具：按沙盒组合根追加 D2 插件、剧本口径同样放置识别物、放置不足判无效；基础循环：sandbox100 成对推进（50 Hz 唤醒）、FleetState 容量 64、cmd_watch 与碰撞小机群走 numba 核、整容量多步筛选合并为一次 numba 遍历（M08、M10、M13、M09）、M07 稳态天气的标量求值缓存 | 同一口径（D1 插件、无识别物）**0.255 → 0.140 核**（load 7.6）、单步 p50 2.35 → 1.42 ms；沙盒组合根（D2 插件、无识别物）0.196 核，其中感知 stage 104 ms/仿真秒；30 识别物 + `--perception full` **0.397 核**（识别物 292、感知 106 ms/仿真秒），仍不通过（第 4 节） |
| D2-AC-09（P0） | 25 次中 3 次 1.09–1.21 s | 不是服务端阻塞：诊断以 50 Hz 轮询并在 0.3 s 内连发 5 次实体增删改，超出每会话配额（REST 10/s、实体 5/s），被拒的请求得到 429 `111` 与 `retry_after_ms` 12–66 ms，但 `Retry-After` 头按整秒取整为 1；SDK 只读头部，睡 1.0–1.2 s | SDK 优先按 problem+json 的 `retry_after_ms` 等待，抖动随等待缩放 | 同一诊断 8 组 40 次操作：64 次 429 全部按几十毫秒重试，单次调用 ≤ 0.153 s，**生效 ≤ 0.853 s**（首组删除的是被任务分配起飞的新机，DRAINING → 降落） |
| D2-AC-32（P1） | 1 h × 2 在 20.87 / 20.89 s 分叉，事件数 3583 / 3557 | M10 PlanPoolClient 没有批处理语义：`follow_path` 规划结果"到达即在下一步边界生效"，两次运行的生效 tick 为 2089 与 2092（请求 tick 2084），取决于规划进程的墙钟时延与机器负载；另有 `geo.ready.load_ms`、`plan.*.t_ms` 等墙钟诊断量写入事件 | 批处理中每个规划作业在提交时定下生效 tick `request_tick + lat_ticks(kind)`，结果暂存到期生效、未到即阻塞（60 s 上限，退出码 3）；墙钟诊断量在批处理输出中置 null | `relay_batch.py --case b --hours 1 --determinism`（load 8–11）与 `--case t5 --world synthcity --hours 1 --determinism`（M08-AC-211 的 T5 模板，load 约 20）均 **PASS**：`events.jsonl` 与 `relay_stats.json` 两次逐字节一致；新增 pytest 以人为延迟 0.6 s 的 worker 对拍 |

### B.2 实现清单

#### 2.1 D2-AC-32 批处理确定性（M08-FR-221、FR-222；ADR-106）

| 文件 | 改动 |
|---|---|
| `python/awr/sim/planning/pool.py`（M10） | 批处理模式：`batch_of()` 返回 BatchCtl 时，`submit` 为作业定下 `due = request_tick + BatchCtl.lat_ticks(kind, budget_ms)`（`follow_path` 500 ms → 50 tick、`safe_transit` 50 ms → 5 tick、`path_valid` 100 ms → 10 tick、`warm` 10 s → 1000 tick），作业照常在规划进程（或线程、inline、远端服务）执行；`drain(tick)` 进入 `_drain_batch`：到达的结果经 `_collect` 暂存（已取代或已失败的丢弃，进程池崩溃的首次重交立即处理；暂存期间被同键新作业取代的结果在 `_supersede` 中丢弃），按 (due, 提交序) 生效 due ≤ tick 的作业，未到达的阻塞等待（`BatchCtl.wait_limit_s` 60 s，超过 `fail()` → 退出码 3）；批处理不做墙钟超时判定；`pending_of` 把已到达未生效的作业视为已到达；原 `drain` 的逐项处理抽为 `_take`，实时路径逻辑不变 |
| `python/awr/sim/mission/runtime.py`（M10） | `bind()` 与 `on_reset()` 新建 PlanPoolClient 后绑定 `batch_of = self._batch_ctl`（`ctx.batch`） |
| `python/awr/sim/runtime/batch.py`（M08） | `core.ctx.batch` 在 `core.start()` 之前置位（启动与会话装配期间提交的作业同样按固定延迟）；`WALL_FIELDS`：`geo.ready.load_ms`、`plan.ready / warm / failed / degraded.t_ms` 与 `t_wall_ns` 一样在 `events.jsonl` 中置 null；模块文档按实现更新 |
| `tests/sim/test_batch_determinism.py`（M08） | 新增 `test_pool_batch_order_and_supersede`（无 sim-core：按 (request_tick + lat_ticks, 提交序) 生效 10、11、13 tick；暂存中被取代的作业丢弃）、`test_batch_plan_jobs_apply_at_fixed_latency`（M10 装配、线程池、worker 人为延迟 0 与 0.6 s 两次：事件流逐字节一致，`follow_path` 的 `apply_tick = request_tick + 50`，`t_ms` 为 null，`cmd.running` 出现）与 `test_batch_plan_wait_limit_exit_3`（等待上限 0.3 s、worker 延迟 1.5 s → 退出码 3） |

M08-AC-211 的"人为让规划 worker 延迟 3 倍：结果不变"由上面第一个用例覆盖（延迟 0.6 s，相当于 `follow_path` 预算 500 ms 的 1.2 倍、典型
时延的 10 倍以上）。

#### 2.2 D2-AC-09 实例增删改生效时间

| 文件 | 改动 |
|---|---|
| `tools/sdk/awr_sandbox.py`（M17） | `_Http._error`：`retry_after_s` 优先取 problem+json 的 `retry_after_ms`（毫秒），其次 `Retry-After` 头；`request` 的重试抖动改为 `uniform(0, min(0.2, 0.02 + 0.1·wait))`。接口形状不变（`AwrError.retry_after_s` 可能为小数秒） |
| `tests/sandbox/test_sdk_retry.py`（新增） | MockTransport：429 `retry_after_ms = 40` 时 40–500 ms 内重试成功；body 优先于头部、无 body 字段时取头部 |

#### 2.3 D2-AC-08 工具（M08-FR-228）

| 文件 | 改动 |
|---|---|
| `tools/bench/fleet_ladder/run.py`（M08） | `--d2-plugins {auto,on,off}`（缺省 auto：`--clock sandbox100`、`--targets n` 或 `--perception full` 时追加 `awr.sim.{catalog,fixedwing,targets,perception,tasking}.plugin`，与 sandbox-pool 的组合根一致）；剧本口径在测量起点标记之后同样放置识别物；perf 自报 `n_target` 小于 `--targets` 时本次运行无效；`--n > 48` 时置 `AWR_FLEET_CAPACITY=1024`；结果明细记录 `d2_plugins` |
| `python/awr/sim/runtime/config.py`（M08） | 插件组合根为 `AWR_PLUGINS` 加 `AWR_PLUGINS_EXTRA`（去重、保持顺序；supervisor 按 `procs[].plugins` 注入的组合之外追加，供基准与诊断使用） |

#### 2.4 D2-AC-08 基础循环（M08-NFR-201）

M08（本模块）：

| 文件 | 改动 | 节省（进程内逐 tick 推进、多窗口最小值，第 3.2 节） |
|---|---|---|
| `runtime/clock_profile.py`、`runtime/main.py` | `ClockProfile.pair_wake`：sandbox100 在 ×1 下也成对推进（偶数 tick 到期时唤醒并连推两个 tick，50 Hz 唤醒；此前代码注释"sandbox100 不成对推进"）；`ClockProfile.fleet_capacity`：sandbox100 为 64 | 实时运行约 −8%（4 组交替 A/B）；容量 64 再 −3%～−7% |
| `fleet/fleet.py` | `FleetConfig.from_env`：未显式给容量时取 `AWR_FLEET_CAPACITY`，否则取时钟档容量（sandbox100 为 64），否则 1024 | — |
| `core/command.py` | `VEC_MIN_ROWS_NB = 1`：有 numba 时 cmd_watch 任意在途行数都走融合核（`watch_triage`、`done_scan`、`stall_scan`、`progress_pick`、`max_dev_update`），numpy 向量实现仍按 32 行 | cmd_watch 23.7 → 8.1 ms/仿真秒 |
| `fleet/collide.py` | 有 numba 时任意机数都走排序扫描核 `uav_hits` | collide 5.4 → 2.9 ms/仿真秒 |
| `fleet/kernels_sel.py`（新） | 选择与掩码核：`sel_bits`、`rtl_phase`、`any_bits`、`sel_eq_nonzero`、`take_nonzero`、`nonzero2`、`unequal_finite2`、`sel_ne2_active`、`any_ne_at`、`any_nonzero_at`、`any_eq_at`、`any_bits_at`；`warmup()` 由 `kernels_l1.warmup` 调用 | — |
| `fleet/stages/l1.py`、`fleet/stages/contact.py` | `l1_indices`、`rtl_phase_of` 与 contact 的 FW 位判断改用 kernels_sel；`prepare_inputs` 没有 Velocity 槽位时一次判断后返回 | l1 10.9 → 7.2、contact 4.3 → 2.6 ms/仿真秒 |
| `runtime/warm.py` | `make numba-warm` 同时装配 D2 插件（沙盒与 S7 的组合根）并预热 M20 覆盖核（d2-ACC-1-to-M08 第 5 条） | 消除首个沙盒会话的 tick 内编译 |

M10、M13、M09、M07、M20（跨模块，逐位等价，知会请求见第 7 节）：

| 文件 | 改动 | 节省 |
|---|---|---|
| `sim/mission/tracker.py`（M10） | `Tracker.stage` 的 TRAJ 槽位集合与完成集合改用 kernels_sel（原三步筛选约 45 µs、完成集合约 19 µs/次，每秒 100 次） | mission 12.7 → 9.8 ms/仿真秒 |
| `sim/sensors/payload.py`、`gimbal.py`、`runtime.py`（M13） | `step_modes`、`step_zoom`、`step_sources` 的整容量掩码与 `nonzero` 改用 kernels_sel；`group_by_rig` 按 (`_roster_ver`, slots) 缓存 | sensors 16.6 → 12.0 ms/仿真秒 |
| `sim/sensors/lidar/scan_rt.py`（M13） | `warmup()` 增加只读 float32 DSM 签名（d2-ACC-1-to-M08 第 5 条、d2-FX-R2-sandbox-to-M08-M18 第 4 条） | 消除运行期编译 |
| `sim/safety/flight_fsm.py`、`fast_guard.py`（M09） | FSM 每 tick 的三项活动集判据与 FastGuard 的 FW 位判断改用 kernels_sel | fsm 3.6 → 1.7 ms/仿真秒 |
| `environment/anchors.py`、`environment/field.py`（M07） | 稳态关键帧（`step` 或已过 t1）的锚点速率 `rates()` 与 `_eval_now()` 的 `eval_env` + `derive` 按关键帧对象缓存 | env 16.0 → 12.4 ms/仿真秒 |
| `tasking/kernels_cover.py`（M20） | 新增 `warmup()` | 消除运行期编译 |

测试：`tests/sim/test_kernels_sel.py`（新增：全部选择核与原 numpy 表达式在 n = 1、4、64、1024 的随机输入上逐位相同；`l1_indices`、
`rtl_phase_of` 与原式相同）、`tests/sim/test_contact.py::test_uav_collider_kernel_matches_small_fleet_path`（新增：n = 2、5、12、40 的
小机群核与逐对网格比较同一命中与顺序）；cmd_watch 的核与逐行实现等价由既有 `tests/sim/test_cmd_watch_vec.py`（`VEC_MIN_ROWS = 0` 对
`10**9`）覆盖。

### B.3 定位方法与根因细节

#### 3.1 方法

- **生产口径**：`tools/bench/fleet_ladder/run.py --n 10 --clock sandbox100 --smoke --dur 40`（真实 supervisor + sim-core，`ladder-shenzhen` n10，
  `ladder.steady` 之后的窗口），对照第 1 轮时加 `--d2-plugins off`；`/proc/<pid>/task/*/stat` 采样各线程 CPU（主线程约占 91%，zenoh 与
  后台发布线程合计约 0.02 核）。
- **进程内逐 tick 推进**（附录 A）：同一剧本在进程内以 LocalBus/LocalRing 构造 SimCore，伪墙钟每次 `iterate()` 前推进一个 tick（与批处理
  相同的确定节拍），50 s 预热后取 4–6 个 6 s 窗口、逐 stage 取最小值（同机负载 7–20 下重复性约 ±2%，实时运行约 ±10%）；cProfile 与逐行
  计时（AST 插桩每条顶层语句）定位热点。
- **实时进程内**：同一构造以真实墙钟 `core.run()` 运行，A/B 交替 4 组。

#### 3.2 D2-AC-08：基础循环为什么是 0.255 核

- 第 1 轮 stage 合计 179 ms/仿真秒，另有约 76 ms/仿真秒的主循环、慢任务与进程开销；进程内逐 tick 推进的同一负载为 0.145 核（stage 113
  ms/仿真秒），实时运行为 0.19–0.21 核，生产进程为 0.24–0.255 核。即**同一份工作在实时运行中贵 1.4–1.8 倍**：sandbox100 每 10 ms 唤醒一次，
  其间同机进程把 L1/L2 冲掉，醒来后的每个 numpy 小运算（整容量 1024 槽的布尔运算、花式下标、`flatnonzero`）在主循环中约 5–30 µs
  （单独测量 5–7 µs），而一次 numba 遍历约 2–4 µs。
- 逐行计时（伪墙钟推进，load 约 11）给出的典型项：`l1_indices` 29 µs、`rtl_phase_of` 17 µs、contact 的 `(S.fidelity & 16).any()` 19 µs、
  M10 跟踪器的三步筛选 47 µs 与完成集合 19 µs、M13 `step_zoom` 的三步掩码与 `nonzero` 36 µs、`step_sources` 的 `nonzero` 29 µs、
  `group_by_rig` 的 `np.unique` 29 µs、M09 FSM 的三次花式下标判据 30 µs——都是每秒 50–100 次。cmd_watch 小机群（< 32 行）走逐行 Python
  判据，约 0.47 ms/次（50 Hz）；机间碰撞 ≤ 48 架走 8 套网格的广播比较，约 0.2 ms/次（25 Hz）。
- M07 env stage 每个 50 Hz env tick 推进一个锚点网格步，稳态天气下 `rates()`（`eval_env` + `derive`）与 `_eval_now()` 都在重算同一个
  常量（约 76 + 43 µs）。
- 结果（进程内逐 tick 推进，多窗口最小值，load 7–15）：0.145 → **0.108** 核（容量 1024 时约 0.115）；各 stage（ms/仿真秒）cmd_watch
  23.7 → 9.1、sensors 16.5 → 11.4、env 15.4 → 12.7、mission 12.4 → 10.0、l1 10.6 → 7.2、collide 5.4 → 3.4、contact 4.1 → 2.6、
  fsm 3.2 → 1.6。
- 实时进程内（同一负载交替 4 组，load 6–10）：修复前 0.189–0.203 核，cmd_watch、碰撞与成对推进之后 0.152–0.166 核，再加容量 64 为
  0.147–0.153 核（其余优化之前的中间版本）。

#### 3.3 D2-AC-09：1.1–1.2 s 是客户端退避

插桩诊断（`ac09_diag.py`，与 `.cache/d2acc1/diag_ac09.py` 同一操作序列，另以 httpx 响应钩子记录每个响应的状态、`Retry-After` 与
problem+json）：第 1 次复现 5 组中 2 次调用 1.19、1.06 s，恰好对应 2 个 429（`code 111`、`detail.category = sbx_entity`、`retry_after_ms`
12 与 55、`Retry-After: 1`），其余 65 个请求全部成功且 ≤ 0.12 s。sim-core 侧的增删（进程内探针）在 0.1–0.2 ms 内完成、下一 tick 即从
roster 移除，没有 1 s 级阻塞。服务端的 `Retry-After` 按 RFC 9110 只能是整秒，精确值本来就在 problem+json 的 `retry_after_ms` 中
（`problem.schema.json` 已有该字段），所以修复在 SDK。

首组"删除后从列表消失 0.85–0.99 s"（第 1 轮与本轮都只出现在首组）：首个新增机被 T2 任务分配后起飞，删除时在空中，按 `fleet/remove`
的 L05–L09 走 DRAINING（以 safety 名义降落）→ STOPPED → REMOVED，属正常生命周期，仍 ≤ 1 s。

#### 3.4 D2-AC-32：分叉点

第 1 轮两次运行的第一处差异（去掉墙钟字段后第 74 个事件）：同一 `follow_path`（cid `m20:p600-03:s1:2`，请求 tick 2084）的 `plan.ready`
`apply_tick` 为 2089 与 2092，`cmd.running` 随之在 20.87 / 20.89 s；之后事件数 3583 / 3557。`run_batch` 的文档写明"M10 的 PlanPoolClient
按 `apply_tick = request_tick + lat_ticks` 生效、到期未返回时阻塞等待"，但 PlanPoolClient 没有实现批处理分支（M20 自己的作业已按固定延迟
同步执行）。另：`geo.ready.load_ms`、`plan.warm.t_ms`、`plan.ready.t_ms` 是墙钟量，即使仿真一致也无法逐字节对拍。

### B.4 D2-AC-08 测量（修复验证用单次自测，非正式基准）

#### 4.1 生产口径（真实 supervisor + sim-core，`ladder-shenzhen` n10，`--smoke`，40 s 窗口）

| 运行 | 时刻与负载 | sim-core（核） | RTF | 单步 p50 / p99 / max（µs） | stage 合计（ms/仿真秒） |
|---|---|---|---|---|---|
| 第 1 轮验收（D1 插件，`n_target = 0`） | 安静，load 0.48 | 0.255 | 1.000 | 2348 / 4758 / 5743 | 179 |
| 本工作包开始前（同上） | load 4.1 | 0.241 | 1.000 | 2242 / 8457 / 12210 | 180.5 |
| cmd_watch、碰撞、成对推进之后 | load 6 | 0.185 | 1.000 | 2087 / 5688 / 7786 | 141.7 |
| 全部修复，`--d2-plugins off` | load 7.6 | **0.140** | 1.000 | 1419 / 5269 / 6925 | 115.4 |
| 最终代码（含 FSM、`prepare_inputs` 优化），`--d2-plugins off` | load 16.0 | **0.140** | 1.000 | 1688 / 7137 / 8179 | 120.8 |
| 全部修复，沙盒组合根（D2 插件），无识别物 | load 7.7 | 0.196 | 1.000 | 4634 / 7800 / 10135 | 223（含 perception 104） |
| 全部修复，`--targets 30 --perception full`（D2-AC-08 口径） | load 12.2 | 0.397 | 0.999 | 3329 / 26293 / 30394 | 531（targets 292、perception 106、tap_targets 5.4） |

（单步 p99 受同机负载影响大；本工作包的运行都在 load 4–12 下，不作判定。）

#### 4.2 预算对照（AWR-04 §4.4.1：每会话 sim-core 0.15 核；构建期按档预算 Σ ≤ 0.16）

- 基础循环（不含识别物与感知）：第 1 轮 0.255 核，本轮 0.140 核（load 7.6）。AWR-04 §4.4.1 给基础循环的份额为
  `0.110 + 10%（L1）≈ 0.12` 核；本轮结果接近该份额，安静环境下需正式复测。
- 识别物与感知：份额合计 0.03 核（感知 25、识别物与编排 5 ms/仿真秒）。实测**没有识别物时感知 stage 已约 100 ms/仿真秒**，30 识别物时
  识别物 stage 约 290 ms/仿真秒、感知约 106 ms/仿真秒，合计约 0.26 核——这是 D2-AC-08 剩余超额的全部来源，属 M18、M19（感知区域工作包
  正在修改 `python/awr/sim/{targets,perception}/**`，本工作包未改，见 d2-FX-R2-sim-to-M18、M19）。

#### 4.3 D2-AC-08 其余子项

SIH 17 项与 S1@sandbox100（`tests/sim/test_fleet_sih_parity.py` 的 sandbox100 参数化、全量 `tests/sim`）在本轮修改后通过；本轮没有改动
L1 物理与控制的数学。单步 p99 ≤ 8 ms 在 30 识别物下为 26.3 ms（识别物 10 Hz stage 的单次调用），同属 M18。

### B.5 测试结果

#### 5.1 功能测试（`-m "not perf"`）

| 范围 | 结果 |
|---|---|
| `tests/sim`（含 SIH 平价、调度 golden、cmd_watch 等价、批处理、numba 签名、新增 `test_kernels_sel.py`、`test_contact.py` 小机群用例） | 最终代码 383 passed（10 deselected 为 perf 标记） |
| `tests/planning` | 143 passed（一次全目录运行中 `test_remote.py::test_remote_applies_at_drain_and_logs` 在 load 约 11 时失败，单独与再次全目录运行均通过：墙钟时延用例） |
| `tests/environment` | 非 perf 用例全部通过（`test_stage_bench.py`、`test_turb_box.py::test_generation_time` 为 perf 标记的计时用例，高负载下超时，与本改动无关） |
| `tests/sensors` | 195 passed |
| `tests/safety` | 140 passed |
| `tests/mission`、`tests/targets`、`tests/perception`、`tests/swarm` | 通过（与 `tests/tasking` 同一次运行：546 passed） |
| `tests/tasking` | 89 passed（单独复跑）。首次与上一行同批运行时 20 例失败，错误均为 `TypeError: Snapshot.__init__() got an unexpected keyword argument 'wind_hi_mps'`（接力工作包当时正在改写 `python/awr/sim/tasking/**` 与 `python/awr/tasking/**`，同期 `make lint` 也报这些文件），与本工作包无关 |
| `tests/sandbox` | 141 passed |
| `tests/e2e`（`test_sandbox_sdk_e2e`、`test_sandbox_park`、`test_relay_batch_tool`、`test_s1_energy`、`test_sandbox_bench_tools`、`test_sandbox_templates`） | 155 passed、36 skipped（标记跳过） |
| `tests/sandbox/test_sdk_retry.py`（新增） | 2 passed |
| `make numba-warm` | 通过（装配 D2 插件后首次 22.8 s，缓存命中后 1.9 s） |

#### 5.2 批处理与工具自测

| 命令 | 结果 |
|---|---|
| `relay_batch.py --case b --hours 1 --determinism --jobs 2`（load 8–11） | determinism PASS；两次 RTF 5.98、6.04（第 1 轮 load 14 时 2.98）；用例自身的接力判据仍不通过（间隙、交接重叠，属 M20） |
| `relay_batch.py --case t5 --world synthcity --hours 1 --determinism --jobs 2`（load 约 20） | determinism PASS；RTF 4.58；接力判据不通过（属 M20） |
| `fleet_ladder --n 10 --clock sandbox100 --targets 30 --perception full --smoke` | 识别物 30/30 放置，`n_target = 30`；4 次运行中第 1 次 sim-core 两次重启（第 6 节第 7 条），其余 3 次正常 |
| `ac09_diag.py`（8 组） | 最大生效 0.853 s，单次调用最大 0.153 s |

#### 5.3 `make lint`

本工作包新增与修改的全部文件 `ruff check` 通过、`no-emoji` 无违例（报告与请求文件同样检查）。`make lint` 整体结果随并行工作包的改动
变化：第一次（06:45）失败 4 处，均在并行工作包正在修改的文件（`python/awr/sim/tasking/**`、`python/awr/tasking/**`、`tests/tasking/**`
的 ruff 11 处，`apps/web/src/app/i18n/capacity.ts` 的 oxlint 5 处，`python/awr/sim/perception/**` 的禁用字形 2 处，
`apps/web/src/viewport/sensorView/pass.ts` 的 M06-L-06 1 处）；最后一次（07:25）只剩 `tests/tasking/test_relay.py` 的 ruff 2 处
（接力工作包），本工作包文件无违例。

### B.6 偏差与遗留

| # | 项 | 说明 |
|---|---|---|
| 1 | D2-AC-08 仍不通过 | 基础循环 0.140 核（load 7.6）接近份额；识别物与感知 stage 合计约 0.26 核，待 M18、M19（第 4.2 节）。修复后按 ADR-033 协议复测 `fleet_ladder --n 10 --clock sandbox100 --targets 30 --perception full --runs 3` |
| 2 | 正式基准未运行 | 并行阶段不跑性能基准；第 4 节数字均为高负载下的单次自测 |
| 3 | 批处理时序变化 | 批处理中 M10 规划结果晚到固定延迟（`follow_path` 0.5 s），派遣与路径装入比第 1 轮（到达即生效，通常数十毫秒）晚；接力判据的结果（间隙、交接重叠）属 M20。D2-AC-19、20、35 的批处理需在 M20 修复后复测 |
| 4 | 成对推进的时延 | sandbox100 奇数 tick 的 stage 晚 10 ms【墙钟】执行，命令与 setpoint 最坏生效时延 +10 ms（D2-AC-21 的"命令到可见 ≤ D_global + 150 ms"不受影响）；已提文档总编登记 |
| 5 | 容量 64 与驻留跨版本 | 同一版本内驻留与恢复一致；跨版本恢复（旧版本容量 1024 的驻留）按代码路径会冷启动重建（未实测），已知会 M17 |
| 6 | 内存（D2-AC-28，非本工作包分派项） | 容量 64 使会话 sim-core 匿名页约 −3 MB（120.5 vs 123.6 MB，进程内探针）；d2-FX-R2-sandbox 建议的周期 `malloc_trim` 未采用：单次 1–10 ms 落在主循环会抬高单步 p99（D2-AC-08 ≤ 8 ms） |
| 7 | 一次 sim-core 崩溃 | 30 识别物的第一次 ladder 运行中 sim-core 两次以退出码 1 重启（日志随运行目录删除），之后以 `--keep-runs` 连续 3 次未复现；同期感知工作包正在改写 `python/awr/sim/perception/**`，不排除是进程启动时读到半写入的文件。若复测再现请保留运行目录 |

### B.7 跨所有权请求

| 文件 | 接收方 | 要点 |
|---|---|---|
| `d2-FX-R2-sim-to-M19.md` | M19 | 无识别物时感知 stage 约 100 ms/仿真秒（`collect()` 逐平台收集），建议空表早退与位姿缓存；30 识别物 106 ms/仿真秒 |
| `d2-FX-R2-sim-to-M18.md` | M18 | 30 识别物时识别物 stage 约 290 ms/仿真秒，热点为逐识别物的运动与可通行判定（逐点 DSM 查询） |
| `d2-FX-R2-sim-to-M07-M09-M10-M13-M20.md` | M07、M09、M10、M13、M20 | 本工作包对所属代码的逐位等价改动清单与测试，请复核保留；沙盒容量 64 |
| `d2-FX-R2-sim-to-M17.md` | M17 | SDK 限流重试改动（请复核）、README 补注建议、容量 64 与驻留跨版本 |
| `d2-FX-R2-sim-to-M16.md` | M16 | `fleet_ladder` 的 D2 插件与识别物放置、AC-09 诊断的轮询口径、批处理时序变化、复测提示 |
| `d2-FX-R2-sim-to-文档总编.md` | 文档总编 | sandbox100 成对推进、容量 64、批处理墙钟字段置 null 的登记 |

### B.附录 A 进程内诊断方法（暂存目录脚本，不进仓库）

- `fast_sb100.py`：环境变量与 supervisor 相同（`AWR_CLOCK_PROFILE=sandbox100`、`AWR_SCENARIO=ladder-shenzhen`、`AWR_SCENARIO_PROFILE=n10`、
  D1 或 D1 + D2 插件、`AWR_PLAN_POOL=inline`），`SimCore(cfg, LocalBus, LocalRing, wall_ns=伪墙钟)`，`manual_gc = True`；每次 `iterate()` 前
  伪墙钟推进一个 tick；50 s 预热后取 K 个窗口，逐 stage 与总 CPU（`process_time`）取最小值；可选 `--stage <name>` 只对该 stage 开 cProfile、
  `--line <对象.方法>` 以 AST 插桩逐条语句计时、`--targets n` 以 `target/add` 放置识别物。
- `prof_sb100.py`：同一构造，以真实墙钟 `core.run()` 运行（`pair_ticks`、后台发布线程与进程相同）。
- `ac09_diag.py`：`tools/bench/sandbox/_sbbench.Stack`（不带 cgroup），T2 会话，`diag_ac09` 的操作序列，httpx 响应钩子记录状态与限流字段。
- `rm_probe.py`、`mem_probe.py`：进程内增删耗时与匿名页。
