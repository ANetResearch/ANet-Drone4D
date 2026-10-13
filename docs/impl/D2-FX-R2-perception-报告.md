# D2-FX-R2-perception 第 2 轮修复：感知区域（D2-AC-13）

| 项 | 内容 |
|---|---|
| 工作包 | D2-FX-R2-perception（修复区域 perception：`python/awr/sim/perception/**` 与感知相关的 M18 共享视线缓存 `python/awr/sim/targets/{los_cache,kernels_los}.py`、`tests/perception/**`；按派工说明允许跨模块修改该区域相关代码） |
| 日期 | 2026-10-07 |
| 依据 | `docs/impl/D2-验收报告-第1轮.md`（§3 D2-AC-13、§4.1 第 1 条、§7）；`.cache/impl/requests/d2-ACC-1-to-M19.md` 第 1、3 条；`d2-FX-R2-sim-to-M19.md` 第 1、2 条；AWR-04 §6.2–§6.4、§13（D2-AC-13）；M18-M19 PRD §6.7–§6.12、M19-NFR-001 至 004、M19-AC-010、015、020；ADR-033、ADR-070（numba 核与预热约定） |
| 分派项 | D2-AC-13（P0）：`tests/perception/test_budget.py::test_stage_cost_12x30` 12 × 30 感知 stage 490.6 ms/仿真秒（≤ 25）、单 tick p99 9.51 ms（≤ 1）；判据正确性 196 例已通过 |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.7 GHz）、Python 3.12.3（`.venv`，numba 0.67.0、numpy 2.5.3）。并行阶段同机有仿真、沙盒、编排、前端等修复工作包与浏览器测试，本工作包期间 load 多为 10–28、8 个 vCPU 全部 96–100% 忙；只在开始时（load 0.07）与末尾的安静窗口持排他锁自测 |
| 约束执行 | 只改上列路径（第 3 节清单；M18 的 `los_cache.py` 属"感知区域相关代码"，已知会 M18）；跨所有权的发现写入请求（第 9 节）；没有安装依赖；没有执行 git 写操作（工作目录无 git 元数据）；没有改动 `test_budget.py` 的用例、阈值与台架替身（`plib.py`）；没有放宽或冻结任何阈值；无 emoji 与禁用字形；未涉及 UI |
| 结论 | 见第 1 节 |

## 1 结论摘要

（第 6 节的数字为修复验证用自测，不是 ADR-033 的正式基准。）

| 项 | 第 1 轮 | 根因（已核实） | 处理 | 自测 |
|---|---|---|---|---|
| D2-AC-13 成本：12 × 30 感知 stage | 490.6 ms/仿真秒，单 tick p99 9.51 ms | 一次（机体，成像传感器）评估约 7.7 ms（开始时 load 0.07 自测 505 ms/仿真秒、评估 tick 中位 7.65 ms）：①逐量测、逐分量的纯 Python 计数器随机数（每次 `_noise3` 约 0.19 ms）；②逐识别物 6 × 6 矩阵 Kalman 与 `np.linalg.inv`（约 60 µs/次，约 850 次/仿真秒）；③逐对标量 TTPF 与 `level_req_of`；④数组核 `level.evaluate` 在 30 对上约 100 次小数组 numpy 运算（本机每次 1–4 µs，合计约 0.29 ms）；⑤逐对 Python：测量 dict、`_step_pair`、`OrderedDict` 视线缓存（约 3.8 µs/对）、每 tick 扫描全部对状态、汇总 O(识别物 × 对)（脏时约 0.3 ms/tick）、每次评估即时展开帧（8 项 × 约 20 字段）、10 Hz 快照重建约 0.19 ms、每 tick 的 M18 绑定约 20 µs | 一次评估只进入 numba 三次（视锥门；判据 + 视线请求次序 + 共享视线缓存查表；射线回写 + 等级 + 量测 + 对状态机 + 单一来源时的航迹）；对状态改为数组表；共享视线缓存改为 numba 数组表；航迹改为逐轴闭式 Kalman 并在核内更新；帧读取时才展开；汇总一次核遍历；快照、绑定、分片、清理的固定开销按版本缓存，空片调用不绑定、不取快照（第 3 节） | 用例同口径 3 次独立运行中位 23.9 ms/仿真秒（load 6.4）与 24.9（load 8.4，超出协议门限）、单 tick p99 0.64–0.89 ms；逐 tick 取最小 21.3（第 6 节） |
| D2-AC-13 判据正确性 | 196 例通过 | — | 全部保持；新增 18 例对拍（第 4、5 节） | `tests/perception` 214 例通过（`AWR_KERNEL=numpy` 208 例通过、6 例为 numba 专用跳过） |
| 发现：真实组合的感知成本（D2-AC-08 等） | sim 工作包测得沙盒组合根感知约 100–106 ms/仿真秒 | 台架之外的成本：声阵列 2 Hz 对全部机体 × 全部识别物的逐对 Python（10 × 30 约 25 ms/次）、雷达逐对标量、M13 平台读取（`sensor_world_pose`、`world_pose`、`payload_view`）与 M07 `optical_depth` | 声阵列与雷达向量化（逐位相同）、没有识别物时不算非成像位姿、修复 `_nonimg_spec` 不缓存 None；M13、M07 部分写入请求 | 真实 M13 组合 10 架、30 识别物：148 → 95 ms/仿真秒（0 识别物 42），其中 M13 读取约一半（第 6.3 节） |

## 2 定位与根因

### 2.1 方法

- 台架：`test_stage_cost_12x30` 的同一场景（12 架、30 识别物、4.8 mm 广角视锥全覆盖、`_CountWorld` 全部可见、`FakeEnv`、sandbox100 每 tick
  一次 perception stage、20 片轮转），先 1 s 预热再计 1000 tick。自测脚本放在会话暂存目录：墙钟与线程 CPU、5 次运行逐 tick 取最小值
  （滤掉同机干扰的尖峰）、cProfile、函数级计时包装与语句级探针（重写方法 AST 插入计时点）。
- 负载校准：以本工作包未改动的 `level.evaluate`（30 对）为参照，安静时 285 µs，前期忙时（load 10–28）323–706 µs，末段（load 4–7）277–314 µs。
  该参照是热循环，测不出缓存争用：本用例的循环内各函数耗时是单独热循环的 2–3 倍（同一函数），末段 3 次独立运行相差约 ±5%。
- 真实组合：`tests/perception/test_m13_integration.py` 的 Rig（隔离登记表装配真实 M13、M18、M19；FleetState + ProfileTable，10 架
  awr_q9 沙盒缺省挂载：eo、lwir、雷达、声阵列、MID-360），d1_250 节奏，0 与 30 个识别物，线程 CPU 计时与 cProfile。

### 2.2 台架热点（第 1 轮代码，cProfile 与逐项计时）

| 位置 | 每次 | 频次（每仿真秒） | 说明 |
|---|---|---|---|
| `runtime._noise3` → `cbrng.normal` | 约 186 µs | 约 850 次（每个量测 3 个分量，各走一遍 5 级 SplitMix 与 Box–Muller，0 维 numpy 并逐次进入 `errstate`） | 验收 cProfile 的约 40% |
| `tracks.update`（6 × 6 矩阵、`np.linalg.inv`） | 约 60 µs | 约 850 次 | |
| `level.evaluate`（30 对） | 约 285 µs | 60 次 | 约 100 次小数组 numpy 运算 |
| `_eval_cand` 其余（几何、门控、逐对 dict、逐对 `ttpf`、逐对 `n_req_of`） | 约 2.6 ms | 60 次 | |
| `LosCache.get_batch`（30 对） | 约 185 µs | 60 次 | 逐对取键、`LosResult` 冻结 dataclass、`OrderedDict` |
| `_step_pair` | 约 6 µs | 约 1600 次 | 关键字参数调用、`level_req_of`、每次重建 `ps.last` |
| `_summary` | 约 300 µs | 约 23 次 | 每个识别物调用一次 `best_of`，各自遍历全部 360 对 |
| 帧（每次评估即时展开前 8 项） | 约 70 µs | 60 次 | 约 160 次 `rnd` |
| `targets()` 重建 | 约 190 µs | 10 次 | |
| `bind`（每 tick `trt.bind`：行对齐与 M13 接线） | 约 20 µs | 100 次 | |

### 2.3 真实组合的热点（修复前，10 架、30 识别物）

`_acoustic`（2 Hz）每次约 25 ms：对每机、每个有声源的识别物逐个 `np.linalg.norm`、`_peek_center`（`OrderedDict` 取键）、
`acoustic_snr_bands`（每次 6–8 次 24 带 numpy 运算）；`collect`（每次评估一个平台）约 0.5 ms，主要在 M13 的 `sensor_world_pose`、
`camera.world_pose`（`quat_to_R` 约 25 次 0 维 numpy 运算）、`payload_view`（每次重建成像状态）；`_radar` 逐候选标量 SNR 与杂波门限。

## 3 实现清单

| 文件 | 改动（FR/AC） |
|---|---|
| `python/awr/sim/perception/kernels.py`（新增，M19） | numba 核（`cache=True`、`fastmath=False`；numba 不可用或 `AWR_KERNEL=numpy` 时 `HAVE_NUMBA = False`）：`fov_gate`（距离与视锥门）；`imaging_core`（方向投影关键维度、偏轴与云台跟踪、垂直视线相对速度、`level.evaluate` 的 N、GSD、k_c、k_light（含 NIR 照明器二分不动点）、k_blur、n_max、LWIR ΔT_app；像素与环境门；视线请求次序（target_no 升序、起点轮转）与格键）；`eval_a` = `imaging_core` + `kernels_los.query_scan`；`post_fsm`（k_los、N_eff、limiting、等级、捕获条件、TTPF、量测与计数器 RNG、对状态行的写入与 `fsm_row` 推进、单一量测来源时的航迹更新）；`eval_b` = `apply_jobs` + `post_fsm`；`fsm_row`（`state.step` 的数组版）、`fov_rows`、`init_row`、`summary_rows`/`summary_cols`（汇总与初始化，只有成像对时直接写实体表四列）、`trk_update_row`（逐轴闭式 Kalman）、`normals3`；路径透过率在核内由 M07 光学厚度取 `exp(−od)`（与 numpy 路径同一 libm `exp`）；对状态机行的标志位 3 标出有转移的行；计数器 RNG 前缀的 Python 整数实现（`prefix3`、`prefix_tick`、`uniform_ch`，与 `cbrng.key` 逐位相同）；`warmup()`（M19-FR-003、FR-013 至 FR-019；M19-NFR-001、004） |
| `python/awr/sim/perception/pairs.py`（新增，M19） | 对状态数组表 `PairTable`（PI 整数列：等级、升降计时等级、捕获态、reason、limiting、类别、level_req、是否有测量、识别物、机体、建立序号；PF 浮点列：计时起点、since、t_last_ok、t_eval、N_req 与最近测量；空位栈）；`PairView`（`PairState` 同名属性的读写视图，`active`、`hold_s`）；`_SlotRec`/`last_dict`（`last` 的同键读取）（M19-FR-013、FR-014） |
| `python/awr/sim/perception/runtime.py`（M19） | 评估改为 `_imaging_nb`（numba）与 `_imaging_py`（numpy 判据 `_eval_np` + Python `state.step`，参考路径）；新对、转移（事件与检测环）、汇总标记与删除在 `_after_rows`，转移由 `_tr_rows` 按对行一次读出 PI/PF 直接构造事件与检测环记录（键序与取值同 `_transitions`，range_m 取视锥门同式算出的距离）；视锥外推进 `_fov_steps`（`fov_rows`）；对状态的机体、识别物、（机体，传感器）索引与 target_no → 行的 numba 可查表；帧读取时展开（`_SensorFrame`、`_frame`，键序与内容不变）；汇总 `_summary_nb`（一次核遍历，再并入雷达、MID-360、声阵列）；`_prune` 在识别物集合与在场机体未变化时跳过；快照（`targets`）改为在 M18 状态可能变化时重建（键为 M18 stage 调用计数、rev、实体表版本与身份，另有 1 s 兜底），按（rev、实体表版本）缓存识别物列表与静态列，动态量向量化（与 M18 `_vel`、`_occlusion` 逐位相同）；`bind` 的 M18 查找与 `trt.bind` 按（实体表、世界、机型表、1 s 时间片）缓存（身份比较）；stage 先算本次工作（`_sched`：本片机体、声阵列周期、航迹维护），空片且无汇总要写时不绑定、不取快照；环境样本为同一对象（M07 按 1 s 量化缓存）或取值不变时复用同一 `EnvSample`；光学厚度的起点数组复用；没有兴趣订阅时 `_publish` 直接返回；分片按周期缓存（机体数 ≤ 128 时纯 Python 计数排序，取用时切片）；N_req 与 TTPF 的 N50 按识别物向量化（任务覆盖时按 reqs 版本缓存）；传感器与环境标量参数缓存（按（机体，传感器）记住上次结果，同一 EnvSample、同一规格对象且状态值相同时直接复用）；评估输出数组按（机体，传感器）双缓冲（`_obufs`）；声阵列向量化（`NI.acoustic_snr_matrix`、`LosCache.blocked_batch`）；雷达预处理向量化；`M13Platforms.collect`：没有识别物时不算非成像位姿、`_nonimg_spec` 缓存 None、`lw_bands` 按机型缓存；`_range` 改为标量 `sqrt` |
| `python/awr/sim/perception/tracks.py`（M19） | 逐轴闭式 Kalman（6 × 6 协方差恒为 `[[a·I, b·I], [b·I, c·I]]`，精确分解为三个共用 (a, b, c) 的二维滤波）；状态存于槽位数组 `TF`、`TI`、`HIST` 与 `TMAP`；`update_batch`（numba）；API（`tr`、`update`、`tick`、`get`、`state_of`、`remove`、`clear`、`dropped`）不变（M19-FR-019、M19-AC-015） |
| `python/awr/sim/perception/nonimaging.py`（M19） | 新增 `acoustic_snr_matrix`（按识别物向量化，与逐个 `acoustic_snr_bands` 逐位相同）（M19-FR-008） |
| `python/awr/sim/perception/wire.py`（M19） | `DetectionRing.push_rec`（首键为 seq 的记录原地写序号，省一次复制） |
| `python/awr/sim/perception/field.py`（M19） | `_reverse` 经 `prt._frame` 读取展开后的帧（帧改为读取时展开） |
| `python/awr/sim/perception/plugin.py`（M19） | 装配时预热 `kernels`、`tracks` 的 numba 核（运行期签名；M08-FR-005、ADR-070） |
| `python/awr/sim/targets/kernels_los.py`（新增，M18 区域） | 共享视线缓存的 numba 表：开放寻址索引（线性探测、后移删除）、LRU 双向链表、空位链；`query_scan`（命中、令牌、旧值、延期、射线端点，含"同一时刻令牌请求失败后不少于该数的请求不再查桶"）、`apply_jobs`（回写与淘汰）、`peek_rows`、`find`、`pop_front`、`clear`；令牌桶 `[tokens, rate, cap, t_ns, has_t]` 的补充与扣减与 `_Bucket` 相同（M18-FR-023、M19-NFR-002） |
| `python/awr/sim/targets/los_cache.py`（M18 区域） | 存储改为上述数组表；`d` 为只读视图；`stats` 为只读属性；新增 `query_raw`、`query`、`query_lists`、`kernel_tables`、`cast_jobs`、`blocked_batch`、`deferred_total`；`get`、`get_batch`、`peek` 语义不变（已知会 M18） |
| `tests/perception/test_kernels.py`（新增） | 18 例：视线缓存与原 OrderedDict 实现对拍（4 组随机序列 + 调用方给键 + 后移删除）；计数器 RNG 与 `cbrng` 逐位相同；成像判据核与 numpy 路径对拍（EO、NIR 照明器、LWIR 日与夜：离散量、事件、检测环逐项相等，连续量 ≤ 1e-12）；闭式航迹与 6 × 6 矩阵 Kalman 一致（≤ 1e-9）、numba 批量与 Python 标量逐位相同；汇总与 `best_of` 相同；快照动态量与 M18 定义逐位相同；声阵列矩阵与逐个计算逐位相同、批量遮挡与逐对 `peek` 相同；分片缓存与 `shard_slots` 逐次相同；运行期不新增 numba 签名 |
| `tests/perception/ref_los.py`（新增） | 原 `LosCache`（OrderedDict）实现的逐字副本，只作对拍参考 |

## 4 正确性与确定性

- **判据与次序**：核内运算次序与 numpy 实现逐项相同（左结合乘加、`np.maximum`/`np.clip`/`np.argmin` 的 NaN 语义、`np.minimum(N_D, N_req)`
  的 NaN 传播）；本机 numba 的 `math.log/exp/cos/sin/atan2/acos/asin/pow/hypot/log10` 与 numpy 逐位相同（20 万样本核对）；唯一差别来自
  numpy 的 `d @ R` 走 BLAS，只影响视锥与跟踪判定的恰好边界。逐对处理次序、视线请求的轮转与令牌消耗次序、事件与检测环的次序、
  对状态的插入次序（汇总的并列判定以建立序号复现）均与此前相同。12 × 30 场景下共享视线缓存的统计（rays 2418、hits 3552、stale 4165、
  deferred 9477、requests 18000）、事件（278 条 level）与检测环（360 条）与改写前完全一致。
- **两条路径对拍**：numba 路径与 numpy + Python `state.step` 路径在四种成像场景下事件、检测环逐项相等（`test_imaging_kernel_matches_numpy_path`）；
  `AWR_KERNEL=numpy` 下全部功能用例通过。
- **确定性**（M19-NFR-004、M19-AC-020）：同一种子两次运行逐字节一致、换种子量测不同（`test_determinism.py`）。计数器 RNG 的键与
  Box–Muller 运算不变（与 `cbrng.normal` 逐位相同）。
- **数值差异（可接受，见第 7 节）**：量测 sigma 的平方改为 `x·x`（此前 Python `**2` 走 libm `pow`，约 0.1% 的值差 1 ulp）；航迹为闭式
  分解（与矩阵形式只差舍入）。

## 5 测试结果

| 范围 | 结果 |
|---|---|
| `tests/perception`（`-m "not perf"`） | 214 passed（含新增 18 例）；`AWR_KERNEL=numpy`：208 passed、6 skipped（numba 专用对拍） |
| `tests/targets` | 152 passed（与 `tests/perception` 同跑 366 passed） |
| `tests/tasking`、`tests/sensors`（M20 使用 `capture_state`、`track`、`set_requirement`、`set_cue`；M13 传感器） | 同跑 293 passed、1 failed（`tests/sensors/test_pose_chain_e2e.py` 的 2 s 握手超时，同机 load 8 时）；该例单独重跑与 `tests/sensors` 全量重跑（199 passed）通过 |
| `tests/sim/test_entities.py`、`test_clock_profiles.py`、`tests/rt/test_d2_topics.py`、`tests/contracts/test_d2_{stubs,contracts}.py`、`tests/fixtures/test_d2_fake_gw.py`、`tests/environment/test_d2_env.py`、`tests/e2e/test_field_sampling_e2e.py` | 87 passed（中期运行） |
| `make lint` | 通过（退出码 0：ruff 全仓库、oxlint、`tools/lint` 全部规则、m06-lint、生成物检查） |

同机高负载下偶发、与本工作包无关的失败：`tests/targets/test_api.py` 的 < 1 s 墙钟断言；`tests/targets/test_wire.py::test_lite32_and_detail`（M18 detail 慢任务在主循环剩余墙钟预算内执行，load 8 时 cfg 变化后 0.5 s 内送达的断言偶发不满足；该用例不装配 M19。为排除共享视线缓存改写的影响，以原 OrderedDict 实现替换后同条件交替各跑 16 次：原实现 4/16 失败、改写后 3/16 失败，load 8.3–8.7）。均重跑通过。

## 6 性能自测（修复验证，非正式基准）

### 6.1 条件

- 机器同表头；Python 3.12.3、numba 0.67.0（核已预热，缓存命中）。本工作包末段同机一直有其他工作包的长时批处理（3–7 个
  `relay_batch`/沙盒工作进程各占一个 vCPU），1 分钟 load 4–9；会话暂存目录的等待脚本以 load ≤ 4 为条件持排他锁等待 2 h，没有等到。
  因此下列数字都是在 ADR-033 门限（开跑前 load ≤ 4、运行内均值 < 6）之外取得的自测，没有持 `runs/.perf.lock`。
- 台架：与 `test_stage_cost_12x30` 相同的场景与循环（`tests/perception/test_budget.py::_fleet`，1 s 预热后 1000 tick，每 tick 用
  `perf_counter_ns` 计 `stage` 的墙钟，求和 / 10 得 ms/仿真秒）；"用例同口径"为每个进程只跑一次、3 个独立进程取中位（PR-3 的做法）；
  "逐 tick 取最小"为同一进程 5 次运行逐 tick 取最小值后求和（滤掉抢占尖峰，用于比较改动前后）。
- 参照：未改动的 `level.evaluate`（30 对）末段 277–314 µs（安静时 285 µs）。它是热循环，测不出缓存争用；本用例循环内各函数的耗时是
  单独热循环的 2–3 倍。

### 6.2 台架结果

| 口径 | 第 1 轮 | 修复后 |
|---|---|---|
| 用例同口径（3 个独立进程，中位） | 490.6 ms/仿真秒，p99 9.51 ms（本工作包开始时 load 0.07 自测 505） | load 6.4：23.8、23.9、24.1，中位 23.9 ms/仿真秒，p99 0.64–0.68 ms（其后又做了第 3 节最后几项：评估输出双缓冲、按传感器复用标量参数）；load 8.4（8 个 vCPU 全满）：24.0、24.9、25.7，中位 24.9，p99 0.79–0.89 ms |
| 逐 tick 取最小（5 次） | — | load 8.1：21.3 ms/仿真秒；评估 tick（600 个，每个处理一架）均值 341 µs、中位 317 µs；空片 tick（400 个）均值 20 µs |
| 单 tick 最大值 | — | 通常 0.8–1.2 ms；同机抢占时偶有 3–7 ms 的单个尖峰（不影响 p99） |

每次评估（一架、一个成像传感器、30 对）的循环内耗时（函数级计时包装，load 约 9）：

| 部分 | 耗时 | 说明 |
|---|---|---|
| M07 光学厚度（台架替身 `FakeEnv.optical_depth`） | 46 µs | 替身内 `np.linalg.norm` 与 Kim 换算 |
| 环境样本（替身 `FakeEnv.sky_sample` 15 µs + 本模块 9 µs） | 24 µs | |
| 平台（替身 `PBench._platforms`） | 21 µs | 每次 `dataclasses.replace` 重建成像状态 |
| `eval_a`、`eval_b`、`fov_gate`（numba，含进入开销） | 39、43、8 µs | 进入开销约占一半（约 60 个数组与标量参数，每个 0.3–0.4 µs） |
| 射线投射（`cast_jobs`，含替身 `los_batch`） | 约 18 µs | |
| 转移（`_tr_rows`，每个转移；10 s 内 360 个） | 45 µs | 事件、检测环、汇总标记 |
| 其余 Python（光学厚度与环境的包装、帧、对登记等） | 约 60 µs | |

台架替身（`FakeEnv`、`PBench._platforms`、`FakeS.active_idx`、`_CountWorld.los_batch`）本身合计约 6.4 ms/仿真秒，约占总数的四分之一；
本模块自身约 17–18 ms/仿真秒。

### 6.3 真实组合（台架之外）

`tests/perception/test_m13_integration.py` 的 Rig（真实 M13、M18、M19；10 架 awr_q9 沙盒缺省挂载，d1_250 节奏，环境用 `FakeEnv`），
感知 stage 线程 CPU 时间（load 约 9）：

| 识别物数 | 修复前 | 修复后 |
|---|---|---|
| 0 | — | 42 ms/仿真秒 |
| 30 | 148 ms/仿真秒 | 95 ms/仿真秒 |

修复后其中约一半是 M13 平台读取（`collect` → `sensor_world_pose`、`camera.world_pose`、`payload_view`，cProfile 份额约 50%），
其余为成像评估、雷达与 2 Hz 声阵列。M13 与 M07 部分已写入请求（第 9 节）。

## 7 偏差

1. **量测 sigma 的平方**：`√((R·1 mrad)² + GSD² + sigma_hold²)` 的平方由 Python `**2`（libm `pow`）改为 `x·x`（正确舍入），numba 与 numpy
   两条路径一致；与此前输出相比约 0.1% 的 sigma 差 1 ulp，量测与航迹差在 1e-15 量级，线上字段按 2–3 位小数取整后不可见。
2. **航迹滤波**：6 × 6 矩阵形式改为数学等价的逐轴闭式形式；与原实现只差舍入（≤ 1e-9，对拍用例）。`TrackTable.tr` 的值由内部
   对象改为槽位号（仓库内只用到键）。
3. **对状态对象**：`PerceptionRuntime.pairs` 的值由 `PairState` dataclass 改为 `PairView`（同名属性可读写，`last` 返回同键记录）；
   `state.PairState` 与 `state.step` 保留，作为参考实现与 Python 路径。
4. **共享视线缓存**（M18 文件）：`d` 改为只读视图、`stats` 改为只读快照属性；行为与次序不变（对拍用例），已知会 M18。
5. **帧在读取时展开**：`frames[uav]` 在评估时只记下标量、评估数组与前 8 对的对状态，`frame_for`、`vehicle`、兴趣下推与 `field._reverse`
   读取时展开；展开内容、键序与此前即时展开相同。直接读取 `frames[uav]["sensors"]` 的外部代码需改用 `frame_for`（仓库内唯一一处
   `field.py` 已改）。
6. **汇总**：numba 路径每次脏 tick 以一次核遍历重算全部识别物的四列（结果与逐识别物 `best_of` 相同，对拍用例）；numpy 路径按识别物
   增量重算。
7. **没有识别物时不计算非成像传感器位姿**：雷达、MID-360、声阵列在识别物表为空时不读取位姿（`NonImaging.p/R` 为 None），输出不变。
8. **清理**：识别物集合（快照静态键）与在场机体的 agent_no 都未变化时 `_prune` 直接返回（此时没有可删的项）。
9. **识别物快照的重建时机**：此前按 100 ms【仿真】时间片重建（M18 未推进时重复重建；M18 推进后最多晚一个时间片才看到新位置，
   sandbox100 下 M18 在 tick ≡ 1（mod 10）推进而时间片在 tick ≡ 0 切换，即平时用的是上一步的位置）；现在每次 M18 stage 推进后
   （调用计数 + 1）立即重建，另以 1 s 时间片兜底。运动识别物的感知因此使用最新位置，结果与此前在逐 tick 层面可能不同（确定性不变）。
10. **M18 绑定刷新**：`trt.bind`（行对齐、M13 接线、机型表）在实体表、世界、机型表变化时立即执行，兜底刷新由每 100 ms 改为每 1 s。
11. **空片调用不绑定、不取快照**：本次调用没有要处理的机体、声阵列周期与航迹维护，且没有汇总要写时，stage 只计数并推进射线窗口，
    不执行 `bind` 与快照检查；实体表、世界、机型表与 M18 状态的变化在下一次有工作的调用（≤ 一个分片周期，沙盒为 200 ms【仿真】，
    没有机体时为航迹维护周期 200 ms）中照常处理。只读接口（`capture_state`、`capture_detail`、`track`）用到的快照因此最多晚一个分片周期
    看到新增的识别物（此前在 M18 推进后的下一 tick）。判定、事件与检测环不受影响（对拍与确定性用例）。
12. **传感器标量参数的复用条件**：此前要求同一 `SensorState` 对象，现在同一规格对象且（f、模式、分辨率、照明器开关）取值相同即复用
    （`SensorState` 为冻结 dataclass，平台读取每次新建）；结果相同。
13. **路径透过率在核内计算**：numba 路径把 M07 的光学厚度直接交给判据核，核内 `exp(−od)`；本机 numba 与 numpy 的 `exp` 逐位相同，
    两条路径对拍不变（若在 numpy 使用 AVX-512 向量 `exp` 的机器上，两者可能差 1 ulp，与核内其他超越函数的情况相同）。
14. **评估输出双缓冲**：同一（机体，成像传感器）的评估结果数组（`_Eval` 的 cand、core、res、codes、z、dfr、pst）隔一次评估复用；
    仓库内只有该机最新的帧在下一次评估前引用它们（帧读取时展开为值）。测试钩子 `ev_hook` 若要跨评估保留数组须自行复制
    （现有用例已复制）。无测量的行 z 仍为未定义值（此前为 `np.empty`，现在为上上次的值），读取方照旧按 has_meas 判断。

## 8 遗留

1. **D2-AC-13 的正式判定**须由验收按 ADR-033 协议复测（第 6 节为自测）；本工作包期间同机始终有其他工作包的长时批处理（load 4–28），没有等到 load ≤ 4 的安静窗口。自测中位数在阈值以内但余量只有约 5–10%，其中约 6 ms/仿真秒是台架替身（`FakeEnv`、`PBench._platforms`、`FakeS.active_idx`、`_CountWorld.los_batch`）本身的开销（第 6.2 节）；若正式复测超限，下一步可做的是把每次评估的三次 numba 进入合并为更少的参数（标量打包为参数数组，单次进入约省 3–6 µs）与检测环记录延迟构造，预计再降 0.5–1 ms/仿真秒。
2. **真实组合的感知成本**（D2-AC-08、04、02、07、55 的感知份额）：修复后 M19 自身的逐对与逐平台计算已降到真实组合的约一半以下，
   其余主要是 M13 平台读取（每次评估约 0.4–0.6 ms，沙盒约 50 次/仿真秒）与 M07 `optical_depth`（约 190 µs/次），已写入请求；识别物
   stage（M18，sim 工作包测得约 290 ms/仿真秒）不在本工作包范围。
3. **场与读数服务**（D2-AC-55 的"场与读数 ≤ 2 ms/仿真秒"，第 1 轮 9.7 ms/仿真秒）本轮未处理（不在分派项；`field.py` 只按帧展开方式
   改了一处读取）。
4. **numba 冷编译**：缓存为空时首次装配编译感知核约 10–40 s（视负载）；装配即预热（插件 `install()`）、`make numba-warm` 与任一次功能
   测试都会写入 `NUMBA_CACHE_DIR`，缓存命中后增量加载约 0.25 s（numba 已初始化的进程内）。

## 9 请求

| 文件 | 对象 | 内容 |
|---|---|---|
| `d2-FX-R2-perception-to-M18.md` | M18 | 共享视线缓存改写的知会与复核（语义不变、接口变化、新接口） |
| `d2-FX-R2-perception-to-M13.md` | M13 | 真实组合下 `sensor_world_pose`、`world_pose`、`payload_view` 的成本与建议（批量 numba 位姿、成像状态复用、轻量读取） |
| `d2-FX-R2-perception-to-M07.md` | M07 | `optical_depth` 约 190 µs/次，建议复用 `derive` 并 numba 化 |
| `d2-FX-R2-perception-to-M16.md` | M16 | D2-AC-13 成本子项按 ADR-033 复测的方法与口径说明 |
