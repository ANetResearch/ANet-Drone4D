# S3 InfoShare 规格（受限通信下的分布式信息融合 Benchmark Suite）

## 文档信息

| 项 | 内容 |
|---|---|
| 文档编号 | AWR-D4-S3 |
| 标题 | S3 InfoShare 规格：受限通信下的分布式信息融合（Distributed Information Fusion under Constrained Communication） |
| 版本 | v0.2.2（2026-10-25 命名同步：按 AWR-06 v0.4 XR-24 与 ADR-D4-36，产品名改为 ANet4D，研究者可见的包名、命令行、SDK 路径与文档站路径改为 `anet4d`；草案契约标识与域分隔常量保持现写法，在 WP-D4-01 契约定稿时改为 `anet4d` 前缀；其余内容不变）；v0.2.1（2026-10-18 文档集集成同步：按 AWR-06 v0.3 §0.5 统一裁定 XR-14 修订 §8.1 清单样例的 Holm 检验族名；按 XR-11 把 AWR-06 调度规则引用改为 SR-1 至 SR-8；按 XR-12 把实验配置样例中的进程内模块引用改为 `module:`） |
| 日期 | 2026-10-18（UTC） |
| 状态 | D4 设计评审稿（v0.2 按第一轮评审意见修订，处理记录见文末"评审处理记录"）。供负责人评审；不构成实现承诺。D4 处于深入调研与概念设计阶段，实现在 D3 封板之后启动，本文周次一律以"D3 封板后第 N 周"表述 |
| 上游 | AWR-06 v0.2（`docs/06-D4-协同实验平台概念设计.md`）§4.2、§5.5、§6、§7.0、§7.3、§7.4、§8.3–§8.6、§9.1 方向 B 与方向 C、§10.2 WP-D4-15 与 WP-D4-17；ADR-D4-07、14、16、17、21、22、23、24 |
| 调研依据 | r02（`docs/research/d4/r02-协同问题族与标准问题库.md`）§0、§3.5、§6；n01（`notes/n01-现有代码映射.md`）G-IL-1、G-MO-3；n03（`notes/n03-外部模型讨论纪要.md`）"S3 InfoShare"条；负责人前期稿 design-r2（`/data/projs/anet-drone-data/design-r2.md`）Benchmark Suite 表中的 InfoShare 行 |
| 并行文档 | M28 实验内核、M29 交互网络、M30 协同接口与基线、M31 基准套件、M32 评测与统计、M33 Trace 与复现、M34 研究者 SDK 与命令行（`docs/modules/`）；S1、S2、X1 规格（`docs/d4/suites/`）；协作智能排行榜规格（下称 LB）、契约草案（下称 CT）（`docs/d4/`）；r04 首篇论文实验设计。并行文档的引用以章节职责为准，字段名以 CT 定稿为准；v0.2 已对照 CT §4.2.3、§4.3.5、§4.4.2、§5.1 与 X1 §2.3、§5，M28、M29、M30、M31、M32 的引用条目逐条打开核实（§1.4 表 B） |
| 适用范围 | Suite `infoshare` 1.x：问题族 IF 的场景族 `is-fixed`（P0，算法隔离模式）与 `is-active`（P1，闭环模式）；问题定义、外生注入、生成器与难度档、指标与评测器、基线、赛道与统计、接口、实例样例、验收 |
| 体例 | 需求 `R-S3-yy`、验收 `AC-S3-yy`（两位序号，与 S1、S2、X1 规格一致）、风险 `RK-S3-yy`、待确认事项 `Q-S3-yy`；AC-S3-01 至 AC-S3-05 与 AWR-06 §7.3 的 AC-S3-1 至 AC-S3-5 同号对应，判据在本文细化（见 §15 第 6、11 条），AC-S3-06 起为本文新增；引用 AWR-06 的工作包 `WP-D4-xx`、决策 `ADR-D4-xx`、验收 `AC-D4-xx` 时保留 AWR-06 原编号 |
| 引用约定 | 代码路径前缀沿用 AWR-06：`py:` = `python/awr/`，`sdk:` = `tools/sdk/anet4d/src/anet4d/`，`ct:` = `packages/contracts/`。`py:kernel/`、`py:interaction/`、`py:benchmarks/`、`py:coordination/`、`py:evaluation/` 为 D4 新包，仓库中尚不存在（已核对）；对现有代码的陈述均已打开文件核实（§1.4）。文献只引用调研报告中出现的条目并原样保留其核实标注 |
| 数值约定 | 标注"初值""缺省""目标值"的数值为设计默认值或待 pilot 校准的目标，不是对实现性能的断言 |

---

## 0. 摘要

1. **研究对象**。S3 隔离"信息共享与融合"这一协同组件：传感节点的运动由实例固定，节点只能决定发送什么、何时发送、发给谁以及如何融合；每个节点在每个估计周期必须对每个目标提交位置估计与协方差（必交报告），评测器以真值评定精度与一致性。问题族编码 IF（r02 §3.5）。
2. **估计问题**。外生目标按近匀速模型运动；节点量测在世界坐标下表示，叠加各节点的配准偏差 `β_i`（量级参照 GNSS 漂移），`β_i` 是相关系数为 `ρ` 的一阶 Gauss–Markov 过程，使"各节点误差独立"的假设失效。`β_i` 不经自身状态、简报或任何其他 Agent 可见通道暴露（§2.9），只能由量测推断；`d2` 起叠加外生分区、环形拓扑掩码与隐藏的节点噪声退化。
3. **主指标**。`U_IS = 1 − mean_{i,m,k} min(‖p̂ − p‖ / e_max, 1)`，`e_max = 20 m`，缺失或无效估计取 1（AWR-06 §7.3）。`U_IS` 只衡量精度；一致性以跨独立运行的 ANEES 区间内比例 `anees_in_band` 考察，进入资源–效用支配分层，不进入排名键。
4. **诊断**。截断饱和诊断（`p_trunc` 及按"信息可达性"的归因拆分、`e_max` 敏感度）；两种信息年龄：由交付记录以向量时钟计算的因果 AoI 下界 `AoI^c`（不依赖 Agent 声明）与基于内核核验的 `source_sample_time_ns` 的一跳声明 AoI `AoI^d`；认知分歧 `D̄`（高斯 2-Wasserstein 闭式）；保守度；按 `info_class` 分解的通信字节。
5. **基线**。P0 为 AWR-06 §7.3 的 6 个基线与 `π_random`：`local_kf`（`π_ind`）、`naive_fusion`、`ci_fusion`（PA-2 代表性协同基线）、`gossip_dedup`、`avg_consensus`（信息形式共识，即共识型分布式 Kalman 的 P0 形态）、`central_kf`（`π_central`）、`random_share`；P1 增补 Kalman 共识滤波 `kcf` 与事件触发共享 `event_share`。全部基线共用同一滤波构件，差异只来自融合与通信策略。
6. **难度与规模**。P0 冻结 `d0`–`d2` × `n4`–`n16`；难度档只由实例内在旋钮与外生通信特征（分区、拓扑掩码）定义，网络档由赛道决定（与 M31 的五维正交约定一致）；目标数 `M = round(0.75 n)`；恒密度缩放下保持不变的覆盖量是"每目标期望观测节点数" `ō`（观测重叠度 `ov = ō/n` 随规模下降，只作报告）；通信预算以参考需求的比值 `κ_B` 表达并按节点均分为每 Agent 硬额度，使规模扩档不改变通信难度。
7. **验收**。AC-S3-01 至 AC-S3-05 与 AWR-06 同号对应；本文新增 AC-S3-06 至 AC-S3-19，覆盖基线序关系、ANEES 评测器正确性锚点、偏差注入的统计与配对、AoI、截断诊断、泄露（含偏差不可观测性）、离线重算、成本、P1 闭环族、生成约束、网络掩码回放、分歧闭式对拍、预算对账与基线约定。S3 由 WP-D4-15（第 5–8 周，S 线 2.0 人周）实现，pilot 于第 10 周（WP-D4-17）。
8. **对 AWR-06 的反馈**共 23 条（§15），主要有：分布式 Kalman 的覆盖声明与基线表不一致；AoI 定义在多跳下无法归属来源；ANEES 的方向字段、缺失处理与种子数；`d0` 解析判据的可检验形式；网络档写入难度档；环形拓扑的生成机制；需求编号 `SR-3` 与调度规则同名；`limit_expr` 不能表达随实例变化的预算；无效运行"全部分量记 0"奖励越小越好的资源指标；相关偏差若经导航解暴露即可被扣除；"来源管理方法在 ANEES 上受益"的表述须区分过度自信率与区间内比例。

---

## 1. 定位、范围与依据

### 1.1 定位

| 项 | 内容 |
|---|---|
| 研究问题 | 在带宽、时延、丢包与分区约束下，节点之间应共享哪类信息、以何种节律与拓扑传播、如何在未知互相关下融合，才能使全体节点的估计既准确又不过度自信（design-r2 InfoShare 行："什么信息值得分享，何时分享"） |
| 被隔离组件 | 融合规则、发送内容与节律、目的端选择、来源管理；感知与运动固定（ADR-D4-17 算法隔离模式） |
| 区分度来源 | 共同来源与循环转发：错误的独立性假设使朴素融合过度自信，来源管理方法应在一致性上受益（AWR-06 §7.3；n03）；受限预算下信息新鲜度与精度的权衡 |
| 与其他 Suite 的关系 | 与 S1、S2 正交：S3 不含任务分配与搜索决策；X1 的 C、P、L、J、S 五族施加于 S3，F、M、H 不适用（X1 §2.3，以 X1 为准）；方向 B（最小任务充分信息）与方向 C（持续认知分歧）以 S3 为主要载体（AWR-06 §9.1、E6） |

### 1.2 分阶段范围

| 阶段 | 内容 | 工作包 |
|---|---|---|
| P0（D3 封板后第 1–11 周） | 场景族 `is-fixed`；`d0`–`d2` × `n4`、`n8`、`n16`；生成器、相关偏差与外生注入、评测器（`U_IS`、跨运行 ANEES、AoI、截断饱和诊断、分歧、保守度）、6 个基线与 `π_random`；Suite 清单与 `validity.md`；pilot | WP-D4-15（主）、WP-D4-13（参考策略缓存）、WP-D4-17（pilot） |
| P1（第 12–21 周） | `Δ_perm`（`perm_struct` 作用于估计字段）；X1 的 C、P、L、J、S 族入榜（C、J 可随 X1 顺延 P2）；`kcf`、`event_share` 基线；`d3` 档与 `n32`；场景族 `is-active`（闭环主动选位，自行关联）；`belief.v1` 接入（S3 的必交估计即 Belief，不另行要求） | WP-D4-19、20、29、51 |
| P2（第 22–31 周） | `d4` 档、`n64`；`abstract-radio@1 severe` 苛刻档；论文实验 E6 | WP-D4-41、52 |

### 1.3 非目标

| 编号 | 非目标 | 说明 |
|---|---|---|
| NG-01 | 数据关联与多目标跟踪 | 算法隔离模式提供已关联检测；关联、杂波与基数估计属 CoopTracking（AWR-06 §7.5，P2） |
| NG-02 | 协同定位与 SLAM | 节点不估计自身位姿；导航误差只作为量测偏差源 |
| NG-03 | 真实无线环境的结论 | 网络参数集标 `synthetic-stress`（AWR-06 §6.5）；环形拓扑以掩码施加（§2.7） |
| NG-04 | 将一致性并入主分 | 主排名只用单一主效用（ADR-D4-21）；一致性进入支配分层与条目卡 |
| NG-05 | 规定载荷语义 | 平台只计量 `info_class` 与校验 `source_sample_time_ns`，不解释载荷（AWR-06 §9.1 方向 B） |

### 1.4 事实核对（对现有代码的陈述）

| # | 事实 | 对本规格的含义 | 依据（已打开核实） |
|---|---|---|---|
| F1 | 归一化一阶 Gauss–Markov 的精确离散化 `z ← φ·z + √(1 − φ²)·n`，`φ = e^{−Δt/τ}`，物理量 = `sigma·z`，与步长无关；只依赖 numpy | 相关偏差的离散化直接调用该模块的 `phi`（`np.exp`）与 `step`（`np.sqrt`），不另写同式实现，使 AC-S3-08 的逐位一致检验成立；该模块只导入 `math` 与 numpy，满足 numpy-only 内核约束 | `py:sim/sensors/gm.py` 模块说明与 `phi`、`step` |
| F2 | 计数器随机数 `cbrng.key(seed, stream, agent, tick, ch)` 及 `normal`、`uniform`；模块只导入 numpy | 偏差、量测噪声与目标运动的抽样经 `RandomOracle` 包装 `cbrng`（AWR-06 §5.5），不另写取样算法 | `py:sim/sensors/cbrng.py` `key`、`normal`、`uniform` |
| F3 | 现有 D2 识别物航迹为三维恒速 Kalman，按"分块标量协方差"精确分解为逐轴闭式更新，以避免逐次 6 × 6 矩阵与 `np.linalg.inv` 的开销；该模块从 `.kernels` 导入，`.kernels` 在可用时导入 numba | S3 滤波构件只借用其闭式更新思路，不 import 该模块（numpy-only 内核约束，AWR-06 B-02）；S3 的协方差非分块标量（偏差与各向异性噪声），须用通用小矩阵实现 | `py:sim/perception/tracks.py` 模块说明；`py:sim/perception/kernels.py` 第 34–39 行的 numba 条件导入 |
| F4 | D3 `L1Model` 的地面位置量测"以本机导航误差为参照（真值 + e_xy + sigma(R) 噪声）"，检测键使用流 21 | S3 保留"量测含节点自身系统偏差"的结构，但不沿用"偏差即导航解误差"的耦合：若导航解 `ŝ_i = s_i + β_i` 对 Agent 可见而真值部署又公开，`β_i = ŝ_i − s_i` 可被直接扣除（§2.9）。S3 的 `β_i` 定义为不经任何 Agent 可见通道暴露的配准偏差；随机键改用 D4 键控命名空间，不使用遗留流 20–22（RN-7） | `py:agentsdk/env/l1.py` 模块说明第 7–8 行 |
| F5 | D3 机型的 GNSS SINGLE 档 Gauss–Markov 参数为 `sigma_h_m = 1.2`、`tau_s = 60` | S3 偏差相关时间缺省取 60 s；`d2` 的幅度 3 m 为压力参数，高于机型缺省，标 `synthetic-stress` | `vehicles/awr_f1/sensors/gnss.yaml`、`vehicles/awr_q9/sensors/gnss.yaml` 第 12 行 |
| F6 | 事件磁带提供 `TapeReader.comm_zones_at(t_ns)` | 通信区只在 `abstract-radio@1` 下生效（M29 R-M29-57）；S3 `d0`–`d2` 不使用通信区，外生通信特征只用分区与链路掩码 | `py:sim/events/tape.py` `TapeReader.comm_zones_at` |
| F7 | D3 `_Pairs.dominates` 假设各指标"越大越好"且非空 | S3 分层指标 `anees_in_band` 取 `direction = max`，`b_tx_ratio`、`cpu_ratio` 取 `min`，须经 D4 方向化支配（M32）处理 | `py:bench/eval/aggregate.py` `dominates` |
| F8 | JCS 规范化实现存在 | 实例包与报告以 JCS 规范化 | `py:sim/events/jcs.py` |
| F9 | 平台没有交互图对象，也不知道消息承载的世界信息，无法计算 AoI 或分歧 | 本 Suite 依赖 M29 拓扑快照、`info_class` 计量与 `source_sample_time_ns` 核验；分歧以必交估计计算 | n01 G-IL-1、G-MO-3 |

表 B 为 v0.2 对并行设计文档的核对（文档陈述，非代码），本文的接口与计分规则以此为前提：

| # | 事实 | 对本规格的含义 | 依据（已打开核实） |
|---|---|---|---|
| B1 | 平面 `L1` 传感器的随机布局：`sensor` 域、`object_id = sensor_id`、`time_index = capture_tick`、`draw_index = 8·r + j`（`j = 0` 检出伯努利，`j = 1, 2` 为 x、y 噪声）；带噪导航解 `self_state` 取同域 `object_id = "nav:" + entity_id`、`time_index = tick_id`，逐 tick 独立抽样 | S3 量测的检出与白噪声沿用该布局（§3.2）；`self_state` 的导航噪声与 `β_i` 键空间不相交，二者统计独立 | M28 R-M28-59 |
| B2 | Suite 可在清单 `rng_domains[]` 声明以 `suite.<suite_id>.` 为前缀的运行期域，随 Suite 随机摘要冻结；生成器子域登记为 `instance/<family>/<sub_domain>` | 偏差过程使用 `suite.infoshare.bias` 域（§3.2）；生成器子域按 M31 登记 | M28 R-M28-27；M31 R-M31-18、R-M31-98、V-S-21 |
| B3 | `limit_expr` 求值器允许引用实例在公开简报中声明的数值变量（明列 S3 的 `b_node_Bps`）；但 M31 R-M31-12、V-S-10 与 CT V-SU-06 仍只允许登记变量 | §8.1 清单在 M31、CT 同步修订前不能通过校验；§4.3 给出不依赖该变更的备选写法，跨文档依赖登记于 §11 | M28 R-M28-69；M31 R-M31-12、V-S-10；CT §5.1.3 V-SU-06 |
| B4 | `Observation.ledger` 只含确定性计量维的剩余量，未规定 `per: team` 维对单个 Agent 的可见形式；受理字节在受理时扣减、之后不退还 | 通信额度改为 `per: agent`（§4.3、BC-4）；`b_tx_ratio` 以受理字节计（§5.7） | M28 R-M28-70；M29 R-M29-14 |
| B5 | 实例包视图按文件划分：不公告的计划分区与链路掩码位于 `hidden/comm_truth.json`（`truth` 视图），`comm.json` 为 `brief` 视图；`tasks.json` 为 `scheduled` 视图 | 链路掩码与分区移入 `hidden/comm_truth.json`（§4.4） | M31 OR-3、§7.4.1、R-M31-26、V-I-09；M31 §13 第 19 条 |
| B6 | 消息 schema 位置为 `ct:coord/msg/<namespace>.<name>.v<k>.schema.json`（单级目录）；`ReportRequest.report` 为按登记 schema 校验的映射（非字节），`report_schema` 须在清单 `schemas.reports[]` 中登记 | 报告 schema 独立登记为 `infoshare.estimate_report.v1`（§8.2） | CT §4.2.3、§4.3.5、§5.1.1 |
| B7 | trace `kind` 登记表含 `sensor.capture`，级别为 full，Suite 声明 `ident` 通道时为 minimal | S3 声明 `ident` 通道（§8.1），`sensor.capture` 在 minimal 级别写出 | CT §4.4.2；M33 §6.9 `ident` 行 |
| B8 | 赛道 `role ∈ {main, control, reference, research, audit}`；`Family.evaluator` 为 `{id, version}` | §8.1 清单按 CT 枚举书写 | CT §5.1.2 |
| B9 | 策略原因失败：结果类指标置不利端，消耗类资源指标保留实际消耗；`invalid_policy = zero` 的无效运行按同一方向化规则处理，不采纳"全部分量记 0"的字面含义 | §5.9 按此计分 | M32 R-M32-36、R-M32-38、R-M32-89 |
| B10 | `CentralPolicy.step` 以同一密封时刻全部所控 Agent 的观测与收件箱为输入，返回按 `agent_id` 键控的决策映射；`solver_host = global` 的视图为"全队合法私有观测的并集"，仅 `centralized` 拓扑可用 | 集中式赛道的观测汇聚不经网络、不计费，各节点报告由集中式主体按 Agent 键分别提交（§7.1） | M30 R-M30-15、R-M30-16、R-M30-19 与 §6.4 `solver_host` 表 `global` 行 |
| B11 | X1 适用矩阵：S3 适用 C、P、L、J、S，F、M、H 不适用；S 族受扰对象为前 `⌈n/2⌉` 个实体、区段 `[t_S, t_S + 0.3T)`、`P_d·(1 − 0.075q)`、`σ·(1 + 0.25q)`，"S3 解释"由 S3 声明 | §3.5 以 X1 为准 | X1 §2.3、§5.1–§5.9、§12.6 第 18 条、Q-RC-07 |

### 1.5 记号

| 记号 | 含义 | 单位 |
|---|---|---|
| `n`、`i, u, v ∈ {1..n}` | 传感节点数与节点下标；每个节点由一个 `sensor_agent` 持有 | — |
| `M`、`m ∈ {1..M}` | 目标数与目标下标 | — |
| `x_m = [p_m; v_m]` | 目标状态（平面位置与速度） | m、m/s |
| `T`、`Δe`、`t_k = k·Δe` | 时域、估计周期（缺省 1 s）、第 k 个估计时刻，`k = 1..K`，`K = ⌊T/Δe⌋` | s |
| `T_s` | 传感器采样周期（缺省 500 ms） | s |
| `σ_{z,i}` | 节点 i 的名义量测噪声标准差（每轴） | m |
| `β_i(t)` | 节点 i 的量测偏差（导航误差） | m |
| `σ_β`、`τ_β`、`ρ` | 偏差幅度（每轴标准差）、相关时间、节点间相关系数 | m、s、— |
| `(p̂_{i,m,k}, P_{i,m,k})` | 节点 i 在 `t_k` 对目标 m 提交的位置估计与 2 × 2 位置协方差（从提交的状态估计中取位置块） | m、m² |
| `e_max` | 截断半径（缺省 20 m） | m |
| `ω ∈ Ω_s`、`m_s = |Ω_s|` | 场景 s 的外生种子与种子数（官方缺省 5） | — |
| `κ_B` | 通信预算比 `B_cap / B_need` | — |
| `ō`、`ov` | 每目标期望观测节点数 `ō = mean_{m,t} |{i : 目标 m 在 i 视场内}|` 与观测重叠度 `ov = ō / n` | — |
| `v̄_m` | 目标 m 的先验速度均值（公开） | m/s |
| `k_0` | 计分起始估计时刻序号 `k_0 = max(1, ⌊t_burn / Δe⌋ + 1)` | — |
| `V_u(t)[v]` | 向量时钟：t 时 u 因果可得的、v 的最晚采样时刻 | s |

### 1.6 编号与引用约定

本文需求编号 `R-S3-yy` 为两位序号，与 AWR-06 §5.2 调度规则（AWR-06 v0.3 起改称 SR-1 至 SR-8，原 R-S1 至 R-S8，如"SR-3 不回写"）不是同一编号空间（反馈第 18 条，已采纳）。基线标识与 M30 §6.7.4 一致（`baseline:infoshare/<name>@<major>`）。文献出处原样保留 r02 的核实标注：`[已核]` 条目可作肯定陈述；`[待核实]` 条目只作算法思想来源，进入正式文档或论文前按 r02 TODO-1 复核。

---

## 2. 估计问题定义

### 2.1 实例与运行模式

实例 `I = (W, N, X, S, C, O)`：平面世界 `W`（边界与障碍多边形）；节点集合 `N`（每节点的平台运动 `s_i(t)`、传感器参数、名义噪声）；目标集合 `X`（初始先验与运动模型，真值轨迹由外生种子实现）；外生随机结构 `S`（偏差模型、噪声退化）；外生通信特征 `C`（链路掩码、分区）；评测目标 `O`（`e_max`、`Δe`、`T`、预算比）。场景配置为（实例, 运行模式）。

| 运行模式 | 场景族 | 运动 | 关联 | 阶段 |
|---|---|---|---|---|
| 算法隔离（主） | `is-fixed` | 固定部署（`fixed_sensor`）或脚本轨迹（`multirotor`，不受 Agent 控制） | 平台给出已关联检测（目标标识） | P0 |
| 闭环 | `is-active` | Agent 经 `EntityCommand` 主动选位 | 匿名检测，Agent 自行关联 | P1 |

两种模式为不同场景集，分别成榜（ADR-D4-17）。

### 2.2 目标运动

- 状态 `x_m = [p_x, p_y, v_x, v_y]ᵀ`，近匀速模型，白噪声加速度功率谱密度 `q`（m²/s³）。离散步长 `Δ_x = 100 ms`：`x(t + Δ) = F(Δ) x(t) + w`，`F = [[I, Δ I], [0, I]]`，`w ~ N(0, Q(Δ))`，`Q(Δ) = q · [[Δ³/3 · I, Δ²/2 · I], [Δ²/2 · I, Δ · I]]`；每轴二维块以 Cholesky 闭式取样。
- 先验（公开，经简报下发）：`x̄_{m,0} = [p̄_m; v̄_m]` 与 `P_0 = diag(σ_p0², σ_p0², σ_v0², σ_v0²)`。先验速度均值由生成器确定：`v̄_m = s_m · [cos θ_m, sin θ_m]ᵀ`，大小 `s_m ~ U[speed_mps]`、方向 `θ_m ~ U[0, 2π)`，二者取自 `instance` 域，属实例内容（不随外生种子变化）。真值初态 `x_{m,0} = x̄_{m,0} + P_0^{1/2} n`，即 `p_{m,0} = p̄_m + σ_p0·n_{1:2}`、`v_{m,0} = v̄_m + σ_v0·n_{3:4}`，`n ~ N(0, I₄)` 取自 `target` 域时间序号 0，随外生种子变化。由此真值初态在跨种子意义下恰好服从公开先验 `N(x̄_{m,0}, P_0)`，模型匹配的滤波器在早期时刻的 NEES 服从 `χ²(d)`，§5.4 的 χ² 区间成立。
- 边界处理：位置越出区域边界时对该轴作镜面反射（位置与速度分量同时反射）。反射是对近匀速模型的已知失配，在 `validity.md` 中声明。
- `d0` 为静止目标：`q = 0`、`σ_v0 = 0`，简报声明 `motion_model = static`。

### 2.3 传感节点与平台运动

| 项 | 规定 |
|---|---|
| 实体 | `fixed_sensor`（恒静止，M28 R-M28-42）或 `multirotor`（脚本轨迹：实例给定闭合航线与恒定速度，由内核按轨迹推进，不接受 `EntityCommand`） |
| Agent | 每节点一个 `AgentSpec(kind = sensor_agent)`，以 `ControlScope.NONE` 持有其实体的 Assignment（只读观测）；算法隔离模式下提交 `EntityCommand` 一律被拒绝并计入审计，不判失败 |
| 视场 | 半径 `r_s` 的扇区（缺省 360°，即圆域），可选平面障碍遮挡（`d2` 起）；目标位于视场内且视线未被遮挡时可见 |
| 检测 | 可见目标以概率 `P_d` 被检测（`d0` 为 1，`d1`–`d2` 为 0.9）；无虚警；检测带目标标识（算法隔离模式） |
| 采样 | 采样周期 `T_s = 500 ms`，采样时刻为 `T_s` 的整数倍，在 `SENSOR_CAPTURE` 阶段记录 `capture_time_ns`（AWR-06 §5.2） |
| 自身状态 | `Observation.self_state` 按平台 `L1` 规则给出带噪导航解（M28 R-M28-59：逐 tick 独立的白噪声，键为 `sensor` 域 `nav:<entity_id>`）；其误差与 `β_i` 相互独立，`ŝ_i − s_i` 不含 `β_i` 的任何信息。部署位置与脚本航线公开（§2.9），`self_state` 对 S3 的估计问题不提供新信息 |

### 2.4 量测模型

节点 i 在采样时刻 t 对可见且被检测到的目标 m 得到世界坐标量测：

```
z_{i,m}(t) = p_m(t) + β_i(t) + v_{i,m}(t),     v_{i,m}(t) ~ N(0, σ_{z,i}(t)² · I₂)
σ_{z,i}(t) = σ_{z,i} · (g_deg  若 i = i_deg 且 t ≥ t_deg；否则 1)
```

检出与白噪声 `v_{i,m}` 按平台 `L1` 规则抽样（表 B1），`β_i(t)` 由 S3 在 `SENSOR_CAPTURE` 阶段以登记的阶段参与者叠加到检测位置上（§3.1），不经 `self_state`。量测保留"含节点自身系统偏差"的结构（F4），但偏差与导航解解耦（§2.9）。Agent 收到 `{target, z, sigma_nominal_m, capture_time_ns}`：`sigma_nominal_m` 恒为名义值 `σ_{z,i}`，噪声退化（`g_deg`、`i_deg`、`t_deg`）不告知 Agent，属隐藏量。

### 2.5 相关偏差模型

```
β_i(t) = σ_β · ( √ρ · c(t) + √(1 − ρ) · b_i(t) )
c(t), b_i(t)：相互独立的二维单位方差一阶 Gauss–Markov 过程，相关时间 τ_β
离散（步长 Δ_β = 100 ms）：z ← φ·z + √(1 − φ²)·n，φ = e^{−Δ_β/τ_β}；初值取平稳分布 N(0, I₂)
```

由此 `Var(β_i,axis) = σ_β²`，`Corr(β_i, β_j) = ρ`（i ≠ j），自相关 `e^{−|Δt|/τ_β}`。离散化调用 F1 的 `phi` 与 `step`。共同分量 `c(t)` 对全部节点的全部量测施加同一平移，量测本身无法辨识它，唯一的约束来自目标先验（`σ_p0 = 15 m`，远大于 `√ρ · σ_β ≈ 2.1–2.7 m`），因此每轴 `√ρ · σ_β` 是任何融合的近似误差下限；独立分量 `b_i` 可经节点间量测比对部分辨识并随节点数平均而下降，这属于合法的协同估计。该下限成立的前提是 `β_i` 不经任何 Agent 可见通道暴露（§2.9 的信息结构保证）。简报公开 `σ_β` 与 `τ_β`；`ρ` 在 `d2` 隐藏（简报只声明 `ρ ∈ [0, 1)`），以保持 CI 所针对的"互相关未知"前提（Q-S3-01）。

### 2.6 噪声退化

`d2` 起，实例指定一个节点 `i_deg` 自 `t_deg = 120 s` 起量测噪声标准差乘以 `g_deg = 3`（AWR-06 §7.3"t = 120 s 起某节点观测噪声升高"）。退化不告知任何 Agent；可由新息（NIS）监测等方法自行发现。

### 2.7 外生通信特征

| 特征 | 定义 | 档位 | 实现 |
|---|---|---|---|
| 链路掩码 `L` | 静态无向邻接（`d2` 为环：节点按环序与左右邻居相连，环秩 `|E| − |V| + 1 = 1`）；掩码外的链路不可达 | `d2` 起 | 以 M29 的有向掩码 `M_ij` 施加（R-M29-55）；`ideal@1` 的连通判据同样受掩码约束（M29 §6.3.1 与 §6.5：`connected_ij = up_i ∧ up_j ∧ M_ij`；R-M29-31 的"拓扑恒为全连通"措辞待 M29 统一） |
| 分区 | 时间窗 `[t_p, t_p + T_p]` 内把环切为两段连续弧，段间掩码为 0；`t_p ~ U[30, 60] s`，`T_p ~ U[60, 90] s` | `d2` 起 | 实例脚本经 `PartitionCtl` 注入；与链路掩码按位与 |
| 通信区 | 不使用 | — | 只在 `abstract-radio@1` 生效，会使对照赛道失去配对意义（F6） |

**环形拓扑的生成机制**。`abstract-radio@1 default` 在视距下 `S_min` 对应约 1.5 km（AWR-06 §6.5 量级自检），400 m × 400 m 区域内的节点在视距下全部互连，环不能由传播几何自然产生，故以链路掩码施加，代表"中继链或环形部署"。生成器同时约束环上相邻节点之间在全时域保持视距（§4.5 G-4），使主赛道下环边不因非视距断开。掩码与分区为隐藏量，Agent 只能经收发回执与收件推断邻居（`topology_hint` 缺省关闭，AWR-06 §6.2）。

### 2.8 决策、消息与必交估计

| 项 | 规定 |
|---|---|
| 决策周期 | `d4_std`：`Δd = 100 ms`、`ℓ_model = 100 ms`（AWR-06 §5.1）；`Δe` 为 `Δd` 的整数倍 |
| 消息 | Agent 可在任一决策 tick 经 `SendRequest` 发送任意登记 schema 或 `opaque` 载荷；平台基线消息 schema 见 M30 §6.5.3（`infoshare.estimate.v1`、`infoshare.gossip.v1`、`infoshare.consensus.v1`）；广播展开为计费单播（ADR-D4-14） |
| `source_sample_time_ns` | 发送方声明载荷所依据的最晚样本采集时刻；内核拒绝晚于该 Agent 已收到样本最晚采集时刻的声明（M28 R-M28-64） |
| 必交估计 | 在每个 `sealed_at_ns = t_k` 的决策中，Agent 须提交恰好一个 `ReportRequest(report_schema = "infoshare.estimate_report.v1")`（§8.2；AWR-06 §7.3 写作 `infoshare.estimate.v1`，见 §15 第 17 条），覆盖全部 M 个目标；估计的有效时刻定义为 `t_k`（与 `ℓ_model` 无关）；`evidence_ids` 为空元组 |
| 报告的性质 | 必交输出，不是消息，不经交互网络、不计通信预算、不回传其他 Agent；计入 CPU 账本 |
| 估计内容 | 状态 `x̂ ∈ R²`（只位置）或 `R⁴`（位置与速度），协方差同维；评测只使用位置块 |

### 2.9 信息结构与泄露边界

| 信息 | 真值层 | 简报（全部 Agent） | 观测（本节点） |
|---|---|---|---|
| 目标真值轨迹 | 是 | 否 | 否 |
| 目标先验 `x̄_{m,0}`、`P_0`、运动模型与 `q` | 是 | 是 | — |
| 全部节点的部署位置或脚本轨迹、视场、名义噪声 | 是 | 是（公开部署） | — |
| 本节点导航解 `ŝ_i` | 是 | — | 是（`self_state`，误差与 `β_i` 独立，表 B1） |
| 偏差实现 `β_i(t)` | 是 | 否 | 否（不进入 `self_state`、回执或任何字段；只以叠加形式出现在量测 `z` 中） |
| `σ_β`、`τ_β` | 是 | 是 | — |
| `ρ`、`i_deg`、`t_deg`、`g_deg` | 是 | `d0`–`d1` 公开（`ρ = 0`，无退化）；`d2` 隐藏 | 否 |
| 链路掩码与分区 | 是 | 否 | 否（只见回执与收件） |
| 其他节点的必交估计 | 是 | 否 | 否 |

**偏差不可观测性的设计选择**。v0.1 令 `self_state = s_i + β_i` 且公开部署，节点以 `ŝ_i − s_i` 即得 `β_i`，相关偏差、CI 所需的"互相关未知"前提与 `d2` 的区分度机制同时失效。v0.2 在两种修正中选择"公开部署、偏差不经导航解"：(1) 部署与脚本航线是实例内容，公开它们使算法隔离模式的运动语义无歧义，固定节点在现实中通常也有测绘坐标（§12）；(2) 若改为隐藏部署，固定节点的 `ŝ_i(t) − ŝ_i(0) = β_i(t) − β_i(0)` 仍泄露偏差增量，脚本航线已知时多旋翼节点同理，修正不彻底。因此 `β_i` 解释为节点的量测配准偏差（坐标变换、安装与时间同步误差的合成，量级与相关时间参照 GNSS 漂移，F5），与平台导航噪声分开建模；该解释与现实中"导航误差同时出现在自身定位与量测中"的耦合不同，在 `validity.md` 中声明（§12）。修正通道已关闭的检验见 AC-S3-11 第 (iii)–(v) 项：键空间不相交、`ŝ_i − s_i` 与 `β_i` 的独立性统计检验，以及以 `ŝ_i − s_i` 修正偏差的变异策略 `nav_correct` 在 `d2` 上不取得优势。

### 2.10 需求

| 编号 | 需求 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|
| R-S3-01 | 目标运动按 §2.2 的近匀速离散模型生成，步长 100 ms，取样经 `target` 域；先验速度均值 `v̄_m` 属实例内容，真值初态按公开先验 `N(x̄_{m,0}, P_0)` 随外生种子抽取 | P0 | AC-S3-01、AC-S3-08 | AWR-06 §7.3；RN-2 |
| R-S3-02 | 传感节点为 `fixed_sensor` 或脚本轨迹 `multirotor`；Agent 以 `ControlScope.NONE` 持有实体，算法隔离模式下的 `EntityCommand` 被拒绝并审计，不判失败 | P0 | AC-S3-11 | AWR-06 §4.1、§7.3 |
| R-S3-03 | 量测按 §2.4 生成：世界坐标、含节点配准偏差 `β_i` 与白噪声，检出与白噪声按 M28 R-M28-59 的布局抽样；Agent 只收到名义噪声 | P0 | AC-S3-01、AC-S3-08 | AWR-06 §7.3；F4；M28 R-M28-59 |
| R-S3-04 | 偏差按 §2.5 生成，离散化与 `py:sim/sensors/gm.py` 同式；`Var` 与 `Corr` 满足 `σ_β²` 与 `ρ` | P0 | AC-S3-08 | AWR-06 §7.3；F1 |
| R-S3-05 | 噪声退化按 §2.6 注入且不告知 Agent | P0 | AC-S3-08、AC-S3-11 | AWR-06 §7.3 |
| R-S3-06 | 链路掩码与分区按 §2.7 经 M29 掩码接口注入，二者按位与；`ideal@1` 下同样受掩码约束；不使用通信区 | P0 | AC-S3-16 | AWR-06 §6.5 第 7 项、§7.3；M29 R-M29-55 |
| R-S3-07 | 每个估计时刻每个节点须提交覆盖全部目标的 `infoshare.estimate_report.v1` 报告，有效时刻为 `t_k`；报告不经网络、不回传 | P0 | AC-S3-05、AC-S3-11 | AWR-06 §7.3 |
| R-S3-08 | 简报与观测的字段范围按 §2.9；`truth` 视图的量只存在于真值层与 `hidden/` 子树 | P0 | AC-S3-11 | AWR-06 §8.8；M31 §7.4、R-M31-26 |
| R-S3-09 | 闭环族 `is-active` 提供匿名检测（不带目标标识），Agent 自行关联；与 `is-fixed` 分别成榜 | P1 | AC-S3-14 | ADR-D4-17 |
| R-S3-36 | 配准偏差 `β_i` 不进入 `self_state`、简报、回执或任何 Agent 可见字段；`self_state` 的导航噪声与 `β_i` 的随机键空间不相交（§2.9） | P0 | AC-S3-11 | 本文 §2.9；M28 R-M28-59 |

---

## 3. 相关偏差与外生扰动注入

### 3.1 注入点与阶段

| 量 | 演化时刻 | 处理阶段 | 写入 |
|---|---|---|---|
| 目标真值 `x_m` | 每 100 ms | `WORLD_UPDATE` | 真值层 `state` |
| 偏差 `c(t)`、`b_i(t)` | 每 100 ms（`Δ_β`），在物理边界上推进到该边界所在步 | `WORLD_UPDATE` | 真值层 `state`（`subject = infoshare.bias.<id>`） |
| 噪声退化 | `t_deg` | `WORLD_UPDATE` | 真值层 `fault` |
| 分区掩码 | `t_p`、`t_p + T_p` | `WORLD_UPDATE`（M29 `apply_control`） | `net/` 层 `topo.partition` |
| 量测 | 每 `T_s` | `SENSOR_CAPTURE`（平台 `L1` 抽样后由 S3 阶段参与者叠加 `β_i`） | 真值层 `sensor.capture`（§3.3） |

全部注入只修改下一步将被消费的输入，不改写已积分状态（AWR-06 §5.2"SR-3 不回写"）。

### 3.2 随机域与共同随机数

| 随机量 | 域 | `object_id` | `time_index` | `draw_index` |
|---|---|---|---|---|
| 实例生成（部署、先验均值与 `v̄_m`、`ρ`、`i_deg`、分区时刻与分段） | `instance/is-fixed/<sub_domain>`（`layout`、`target_prior`、`bias`、`degrade`、`partition`、`obstacle`） | 实例规范 id | 0 | 按字段登记序号 |
| 目标初态与过程噪声 | `target` | `target_id` | 目标步序号（初态为 0） | 轴 × 分量（0–3） |
| 偏差共同分量 `c` | `suite.infoshare.bias` | `common` | 偏差步序号 | 轴（0–1） |
| 偏差独立分量 `b_i` | `suite.infoshare.bias` | `sensor_id` | 偏差步序号 | 轴（0–1） |
| 检出伯努利 | `sensor`（平台 `L1`） | `sensor_id` | `capture_tick` | `8 · r + 0`（r 为目标行号） |
| 量测白噪声 | `sensor`（平台 `L1`） | `sensor_id` | `capture_tick` | `8 · r + 1`、`8 · r + 2` |
| 导航解噪声（`self_state`） | `sensor`（平台 `L1`） | `nav:<entity_id>` | `tick_id` | 0–1 |

运行期域 `suite.infoshare.bias` 在清单 `rng_domains[]` 中声明（M28 R-M28-27、M31 R-M31-98），`object_id` 正则为 `^(common|[a-z][a-z0-9_]{0,31}_[0-9]{5})$`，`time_index` 上界为 `⌈T / Δ_β⌉`、`draw_index` 上界为 1；生成器子域在清单与 `ct:coord/rng_domains.v1.json` 中以 `instance/is-fixed/<sub_domain>` 登记（M31 R-M31-18）。平台 `L1` 的检出与噪声布局沿用 M28 R-M28-59，不另行定义。偏差与量测噪声全部属外生量，在同一（实例, 外生种子）下跨策略逐位相同（RN-2、AC-D4-05）；策略改变发送行为不改变任何偏差、噪声或目标轨迹。偏差键与导航噪声键分属不同域，二者的独立性由键空间不相交保证（R-S3-36）。

### 3.3 生成与记录算法

```python
# py:benchmarks/suites/infoshare/inject.py（草案；numpy-only）
from awr.sim.sensors.gm import phi as gm_phi, step as gm_step   # F1：直接复用，不另写同式实现

BIAS_DOMAIN = "suite.infoshare.bias"                              # 清单 rng_domains[] 声明（R-M28-27）

class BiasField:
    """c(t) 与 b_i(t) 的状态；推进只取决于 (随机根, 域, object_id, 步序号)，与策略无关。"""
    def __init__(self, oracle, sensor_ids, sigma_b, tau_b, rho, dt_b_ns=100_000_000):
        self.phi = gm_phi(dt_b_ns / 1e9, tau_b)
        self.sc, self.si = sigma_b * math.sqrt(rho), sigma_b * math.sqrt(1.0 - rho)
        self.sc_stream = oracle.stream(BIAS_DOMAIN, "common")
        self.si_stream = {s: oracle.stream(BIAS_DOMAIN, s) for s in sensor_ids}
        self.zc = np.array([self.sc_stream.normal(0, a) for a in (0, 1)])          # 平稳初值
        self.zi = {s: np.array([st.normal(0, a) for a in (0, 1)]) for s, st in self.si_stream.items()}
        self.k = 0
    def advance_to(self, k: int) -> None:
        while self.k < k:
            self.k += 1
            self.zc = gm_step(self.zc, self.phi, np.array([self.sc_stream.normal(self.k, a) for a in (0, 1)]))
            for s, st in self.si_stream.items():
                self.zi[s] = gm_step(self.zi[s], self.phi, np.array([st.normal(self.k, a) for a in (0, 1)]))
    def beta(self, s: str) -> np.ndarray:
        return self.sc * self.zc + self.si * self.zi[s]
```

- `ρ = 0` 或 `σ_β = 0` 时 `BiasField` 退化为零偏差或独立偏差，生成器在 `d0`–`d1` 以 `σ_β = 0` 实例化。
- 每次采样写真值层记录 `sensor.capture{sensor_id, capture_time_ns, seen[target], detected[target]}`；该记录是评测器计算 AoI 与信息可达性所需的最小集。CT §4.4.2 已登记该 `kind`（级别 full，Suite 声明 `ident` 通道时为 minimal），S3 在清单中声明 `ident` 通道（§8.1），使其在 `minimal` 级别写出（表 B7；AWR-06 §4.5 的 `kind` 枚举仍无此项，反馈第 16 条）。

### 3.4 机制对照实例（反事实变体）

为 AC-S3-03 的机制检验，生成器对任一 `d2` 实例提供确定性变体 `variant ∈ {base, nobias, nobias_brief, noring}`，每个变体只改变一个因素，其余全部字段与随机键不变：

| 变体 | 外生注入 | 简报 | 改变的因素 |
|---|---|---|---|
| `base` | `σ_β = 3 m` | 声明 `σ_β = 3 m` | — |
| `nobias` | 注入偏差置零（`BiasField` 以 `σ_β = 0` 实例化） | 仍声明 `σ_β = 3 m`（与 `base` 逐字节相同） | 只改外生量：偏差是否真实存在 |
| `nobias_brief` | 注入偏差置零 | 声明 `σ_β = 0` | 在 `nobias` 之上再改信息量：基线是否被告知偏差（BC-2 的 consider 参数随之为 0） |
| `noring` | 同 `base` | 同 `base` | 链路掩码由环改为全连通（分区保留），只改拓扑 |

`noring` 不消除重复计入：朴素融合在任何允许双向交换的拓扑上都会重复计入（A 发给 B，B 把含 A 信息的融合估计发回 A），全连通拓扑的环更多。因此 `noring` 度量的是"拓扑约束对重复计入强度的影响"，不是"关闭循环转发"；重复计入本身的份额由 `nobias` 变体度量（§10 AC-S3-03 的分解）。变体标识计入 `instance_id` 后缀（`…/d2/n8@1#nobias`）。变体只用于 `dev` 与 pilot，不进入成榜场景集。

### 3.5 与 X1 RobustCoord 的叠加

适用族、参数、时刻与受扰对象一律以 X1 §2.3 与 §5 为准，本节只声明 S3 侧的解释与合成规则（X1 §12.6 第 13、18 条）。

| X1 族 | X1 参数（摘引，以 X1 为准） | S3 解释与合成 | 对 `avail`、`AoI^c` 的影响 | 阶段 |
|---|---|---|---|---|
| C 失联 | 前 `k(q, n)` 个实体；`t_e = slot(0.3T, 0.6T, v_e)`；区间 `[t_e, t_e + 0.2T)`（`T = 180 s` 时 36 s） | 失联节点照常采样与提交必交报告（报告经评测通道，不受影响，X1 §5.3）；其发出的消息照常受理计费、在出队时以 `endpoint_down` 丢弃，发往它的副本发射后丢弃 | 区间内失联节点的向量时钟不再与他人交换：他人关于它的 `AoI^c` 线性增长，它关于他人的 `AoI^c` 同样增长；`avail` 相应下降，归入 `p_trunc_unavail` | P1（可随 X1 顺延 P2） |
| P 网络分区 | 按初始位置在整数方向上的投影二分；`t_P = slot(0.25T, 0.45T, ·)`；持续 `15·q s` | 与实例分区掩码、环形链路掩码三者按位与；S3 `fixed_sensor` 与脚本航线节点均以实例 `pos0` 参与投影 | 跨组 `AoI^c` 在区间内单调增长 | P1 |
| L 链路退化 | 全网 GE 参数与令牌速率按 `q/4` 向 `severe` 插值；区段 `[t_L, t_L + 0.3T)` | 只作用于网络参数，不改实例；预算额度不变 | 丢包与排队使交付减少，`AoI^c` 增大 | P1 |
| J 延迟尖峰 | 3 个 10 s 尖峰，附加时延 `200·q ms`、抖动 `100·q ms` | 不降低吞吐，只使消息陈旧与乱序；BC-5 的时间对齐处理陈旧估计 | 尖峰内交付推迟，`AoI^c` 增大约附加时延 | P1（可随 X1 顺延 P2） |
| S 传感器退化 | 前 `⌈n/2⌉` 个实体；区段 `[t_S, t_S + 0.3T)`；`P_d ← P_d·(1 − 0.075q)`、`σ ← σ·(1 + 0.25q)` | **S3 解释**：`P_d` 解释为"视场内且视线通畅的目标产生量测的概率"（§2.3 检测行）；`σ` 作用于真实噪声 `σ_{z,i}(t)`，`sigma_nominal_m` 不变（退化不告知 Agent）。与 §2.6 单节点退化的叠加为乘法：`σ_{z,i}(t) = σ_{z,i} · g_deg^{[i = i_deg, t ≥ t_deg]} · (1 + 0.25q)^{[i ∈ S, t ∈ 区段]}`；`β_i` 不受影响 | `P_d` 下降使检测采样减少，`avail` 下降；`AoI^c` 以全部采样为基准，不受影响 | P1 |
| F 节点静默失效 | — | 不适用（X1 §2.3）：失效后其单元按 `e = 1` 计分，度量的是计分规则而非融合算法的鲁棒性；X1 Q-RC-07 允许 P2 评估"只对存活节点计分"的诊断变体，S3 届时在条目卡以诊断量形式提供，不改变宿主 `U_IS` | — | — |
| M、H | — | 不适用（X1 §2.3） | — | — |

X1 运行属 `stress` 划分，独立报告，扰动时刻与对象由 X1 的派生规则给出并按外生种子跨策略配对（AC-RC-01）。

### 3.6 需求

| 编号 | 需求 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|
| R-S3-10 | 全部外生量按 §3.2 的域与键派生；`suite.infoshare.bias` 在清单 `rng_domains[]` 声明，生成器子域按 `instance/is-fixed/<sub_domain>` 登记；同一（实例, 外生种子）下跨策略逐位相同 | P0 | AC-S3-08 | RN-1、RN-2、RN-4；ADR-D4-07；M28 R-M28-27 |
| R-S3-11 | 偏差与目标在 `WORLD_UPDATE` 推进，量测在 `SENSOR_CAPTURE` 采样；注入不回写已积分状态 | P0 | AC-S3-08 | AWR-06 §5.2 |
| R-S3-12 | 每次采样写 `sensor.capture` 真值记录，`minimal` 级别亦写出 | P0 | AC-S3-09、AC-S3-12 | 本文 §5.5 |
| R-S3-13 | 生成器提供 `nobias`、`nobias_brief`、`noring` 确定性变体（§3.4，每个变体只改变一个因素），只用于 `dev` 与 pilot | P0 | AC-S3-03 | AWR-06 AC-S3-3 |
| R-S3-14 | X1 的 C、P、L、J、S 族可施加于 S3，参数以 X1 为准，S3 解释与合成按 §3.5 | P1 | AC-RC-01 | AWR-06 §7.4；X1 §2.3、§5 |

---

## 4. 实例生成器、难度档与规模档

### 4.1 生成器参数空间

生成器 `gen/is_fixed@1`（`py:benchmarks/suites/infoshare/generator.py`）实现 `Generator` 协议（AWR-06 §4.2）。

| 参数 | 类型 | 含义 | `d0` | `d1` | `d2` |
|---|---|---|---|---|---|
| `n` | int | 节点数 | 4 | 规模档 | 规模档 |
| `region_side_m` | float | 区域边长，`L = 400 · √(n/8)` | 200 | 恒密度 | 恒密度 |
| `M` | int | 目标数，`round(0.75 n)` | 1 | 规模档 | 规模档 |
| `motion` | enum | `static` / `cv` | `static` | `cv` | `cv` |
| `q` | float | 加速度功率谱密度 | 0 | 0.05 | 0.05 |
| `speed_mps` | 区间 | 先验速度均值的大小 `|v̄_m|`（方向由 `instance` 域均匀抽取，§2.2）；真值初速为 `v̄_m + σ_v0·n` | 0 | [2, 6] | [2, 6] |
| `sigma_p0_m`、`sigma_v0_mps` | float | 先验标准差 | 30、0 | 15、2 | 15、2 |
| `deploy` | enum | 部署：`fixed` / `ring_mixed`（环上交替固定与环绕脚本航线） | `fixed` | `fixed` | `ring_mixed` |
| `r_s_m` | float | 视场半径 | 1000（全覆盖） | 120 | 120 |
| `P_d` | float | 检测概率 | 1 | 0.9 | 0.9 |
| `sigma_z_m` | 区间 | 各节点名义噪声 | {2, 3, 4, 5} | [2, 5] | [2, 5] |
| `obstacles` | int | 矩形障碍数（遮挡感知） | 0 | 0 | [2, 4] |
| `sigma_b_m`、`tau_b_s`、`rho` | float | 偏差 | 0、—、0 | 0、—、0 | 3、60、[0.5, 0.8] |
| `deg` | 结构 | 噪声退化 `{t_deg, g_deg}` | 无 | 无 | `{120 s, 3}` |
| `mask` | enum | 链路掩码 | 全连通 | 全连通 | `ring` |
| `partition` | 结构 | 分区窗 | 无 | 无 | `t_p ∈ [30, 60] s`、`T_p ∈ [60, 90] s` |
| `kappa_B` | float | 预算比（初值；`d2` 的 1 为零余量紧约束，pilot 扫描 {1, 1.5, 2}，§4.3） | 4 | 2 | 1 |
| `horizon_s` | float | 时域 | 60 | 180 | 180 |
| `e_max_m` | float | 截断半径（Suite 主版本内各档相同） | 20 | 20 | 20 |

### 4.2 难度档（客观旋钮）

难度档只定义实例内在旋钮与外生通信特征；网络档与参数集由赛道决定，不写入实例（与 M31 §13 第 7 条一致，反馈第 7 条）。表中"锚点运行"一栏只说明该档用于单元测试或教学时的推荐网络档，不构成实例字段。

| 档 | 客观旋钮 | 典型参数（参考规模 n8） | 锚点运行 |
|---|---|---|---|
| `d0` | 静止目标、独立噪声、无偏差、全覆盖、`P_d = 1` | 4 节点、1 个目标、200 m × 200 m、60 s；可解析求最优融合方差（§9.1） | `ideal@1` |
| `d1` | 近匀速目标、独立噪声、无偏差、全连通、部分观测重叠 | 400 m × 400 m、6 个目标、180 s、`κ_B = 2`；每目标期望观测节点数 `ō ∈ [1.5, 3.0]`（各规模档相同；`n8` 时对应 `ov ≈ 0.19–0.38`） | 主赛道 |
| `d2` | `d1` + 相关偏差（`σ_β = 3 m`、`τ_β = 60 s`、`ρ ∈ [0.5, 0.8]`，隐藏）+ 环形掩码 + 60–90 s 分区 + `t = 120 s` 单节点噪声 ×3（隐藏）+ 一半节点脚本运动 + 遮挡障碍 | `κ_B = 1`（`n4`–`n16` 约对应 0.5–2.1 kB/s/节点，按 §4.3 的估算，以参考编码器实测为准）；`ō ∈ [1.5, 3.0]`；环秩 1；分区时长占比 `T_p / T ∈ [0.33, 0.5]` | 主赛道 |
| `d3`（P1） | `d2` + `κ_B = 0.5` + 多偏差簇（两组节点各有共同分量）+ 闭环主动选位族 `is-active` | 高密度；`n32` | 主赛道 |
| `d4`（P2） | `d3` + `n32`–`n64` | 近容量上界 | 主赛道（`severe` 对照） |

**客观难度量**（`Generator.difficulty(inst)` 的输出，写入实例 `difficulty{}`；隐藏旋钮的数值只写入 `hidden/`）：

| 量 | 定义 | 公开 |
|---|---|---|
| `o_bar` | 每目标期望观测节点数 `ō = mean_{m,t} |{i : 目标 m 在 i 视场内}|`（以先验均值轨迹与名义视场计算）；恒密度缩放下的不变量，G-1 约束的对象 | 是 |
| `ov` | 观测重叠度 `ō / n`；随规模约按 `1/n` 下降，只作报告，不作约束 | 是 |
| `cov_any` | 至少一个节点可见的（目标, 时刻）占比 | 是 |
| `kappa_B` | `B_cap / B_need` | 是 |
| `cycle_rank` | 链路掩码图的环秩 `|E| − |V| + c` | 否 |
| `part_frac` | `T_p / T` | 否 |
| `rho`、`g_deg` | 偏差相关系数、退化增益 | 否 |
| `speed_ratio` | `mean_m |v̄_m| · T_s / mean_i σ_{z,i}`（目标在一个采样周期内的名义位移与量测噪声之比；`d1`–`d2` 典型值约 `4 × 0.5 / 3.5 ≈ 0.57`） | 是 |

### 4.3 规模档与预算

| 规模档 | `n` | `M` | 区域边长 | 阶段 |
|---|---|---|---|---|
| `n4` | 4 | 3 | 283 m | P0 |
| `n8` | 8 | 6 | 400 m | P0 |
| `n16` | 16 | 12 | 566 m | P0 |
| `n32` | 32 | 24 | 800 m | P1 |
| `n64` | 64 | 48 | 1131 m | P2 |

恒密度规则：区域边长 ∝ √n，目标数与节点数同比（AWR-06 §7.0）；`r_s` 不随规模变化。由于视场面积不变而区域面积 ∝ n，保持不变的是每目标期望观测节点数 `ō ≈ π r_s² · n / L² = π · 120² · 8 / 400² ≈ 2.26`（无边界效应时），观测重叠度 `ov = ō / n` 约按 `1/n` 下降（`n4` 0.57、`n8` 0.28、`n16` 0.14）。v0.1 以 `ov ∈ [0.25, 0.35]` 为约束，只在 `n8` 附近可满足，v0.2 改以 `ō` 为约束（G-1），`ov` 只作报告。

以名义视场与静止位置粗算的 `ō` 与 `cov_any`（目标在 `[0.1L, 0.9L]²` 内均匀、每档 300 次部署，仓库 `.venv` 中 numpy 计算；未计目标运动、遮挡与脚本航线，pilot 以生成器实际输出为准）：

| 规模 | `fixed` 部署：`ō` / `ov` / `cov_any` | `ring_mixed` 部署：`ō` / `ov` / `cov_any` |
|---|---|---|
| `n4` | 1.68 / 0.42 / 0.94 | 1.99 / 0.50 / 1.00 |
| `n8` | 1.90 / 0.24 / 0.95 | 2.20 / 0.28 / 0.99 |
| `n16` | 2.08 / 0.13 / 0.96 | 2.58 / 0.16 / 0.90 |
| `n32`（P1） | 2.18 / 0.07 / 0.96 | 2.93 / 0.09 / 0.77 |

边界效应使小规模档的 `ō` 偏低，`ring_mixed` 的 `ō` 随规模上升而 `cov_any` 下降；G-1 的初值区间 `[1.5, 3.0]` 覆盖 P0 三档的两种部署，`n32` 的 `ring_mixed` 在 P1 开放前须调整环半径规则（PA-7）。

**通信预算**。参考需求 `B_need` 定义为"每个节点每个估计周期向两个邻居各发送一条覆盖全部 M 个目标的 `infoshare.estimate.v1` 参考消息"所需的团队线上字节：

```
B_need   = n · 2 · ⌈T / Δe⌉ · W_ref(M)
W_ref(M) = 参考编码器对含 M 个目标的 infoshare.estimate.v1 消息（M30 §6.5.3：f4 均值 4 维与协方差上三角 10 维）
           给出的线上字节 W = A + 64 · ⌈A / 1200⌉，A = 载荷 + 元数据（M30 §6.5.4、M29 §6.2.1；AWR-06 §6.5 第 3 项）
B_cap    = κ_B · B_need                     团队总额，只作报告与 b_tx_ratio 的分母
B_cap,i  = B_cap / n = b_node_Bps · T       每 Agent 硬额度，per: agent；超额副本以 TxReceipt(reason = budget) 拒绝（M29 R-M29-14）
```

`W_ref(M)` 的估算：每目标载荷 56 B（14 个 f4），M30 §6.5.4 给出 `n16`（`M = 12`）时 `A ≈ 1.0 kB`，对应 `κ_B = 1` 时 `b_node_Bps = 2 · W_ref / Δe ≈ 2.1 kB/s`。区间下端只计载荷与 64 B 头部，上端按 M30 的 `n16` 量级反推固定元数据约 330 B，据此 `κ_B = 1` 时 `n4`（`M = 3`）、`n8`（`M = 6`）、`n16`（`M = 12`）的 `b_node_Bps` 分别约 0.46–1.1、0.8–1.5、1.5–2.1 kB/s。正式值以参考编码器实测为准，并由 AC-S3-18 对账。

| 预算设计项 | 规定 | 理由 |
|---|---|---|
| 额度维度 | `tx_bytes` 取 `per: agent`、`limit = duration_s · b_node_Bps`；团队总额 `B_cap` 不作硬额度 | `Observation.ledger` 只给出本 Agent 剩余量（M28 R-M28-70），不泄露他人发送量；避免团队池被先发节点耗尽而使吞吐取决于调度先后（表 B4） |
| 集中式赛道 | 集中式主体的团队预算为单 Agent 额度 × n（M30 R-M30-15），各 Agent 的额度仍分别执行 | 与分布式赛道对齐 |
| `κ_B = 1` 的含义 | 额度恰等于参考需求，不含参考消息之外的任何余量（广播回退的多副本、`prov`、`deg` 等附加字段、探测）；发送类基线在 `d2` 上受 BC-4 节流约束是设计意图 | 紧约束档使"何时发、发什么"成为被测对象 |
| pilot 校准 | 扫描 `κ_B ∈ {1, 1.5, 2}`；报告各发送类基线的有效发送率（实际发送周期数 / 名义周期数）；若 `κ_B = 1` 时全部发送类基线的有效发送率同时 < 0.9，按 PA-7 上调 `d2` 的 `κ_B` | 防止紧约束使基线间差异只来自节流规则（RK-S3-06） |

以比值 `κ_B` 而非固定 KiB/s 表达预算，使规模扩档时每节点的相对通信紧度不变（M 随 n 增长，消息长度随之增长）。生成器把 `b_node_Bps = B_cap / (n · T)` 写入实例 `objective.json`。

**`limit_expr` 的跨文档依赖与备选写法**。首选写法 `limit_expr: "duration_s * b_node_Bps"` 已被 M28 R-M28-69 的求值器接受（实例公开数值变量），但 M31 R-M31-12、V-S-10 与 CT V-SU-06 仍只允许登记变量，清单在二者修订前不能通过校验（表 B3）。若 M31、CT 在 WP-D4-10 的 `suite.v1` 定稿（第 4–6 周）前未采纳，P0 采用备选写法：`limit_expr: "duration_s * 2 * (64 + a_0 + 0.75 * w_t * n_agents)"`（只引用三份文档共同允许的 `n_agents`、`duration_s`；S3 每节点一个 Agent，`n_agents = n`，`M = 0.75 n` 在 `n4`–`n64` 上恰为整数；`w_t` 为参考编码器实测的每目标字节数，`a_0` 为固定元数据与控制字段字节数，二者作为常数随 Suite 主版本冻结；P0 三档 `W_ref` 不超过单片 1200 B，表达式无需分片项，`n32` 起须分片，届时依赖平台变更），即对全部难度档取 `κ_B = 1`，`d0`、`d1` 的较宽预算在 P0 暂不实现，作为降级在 pilot 报告中登记。该依赖列入 §11 的前置。

### 4.4 实例字段与实例包映射

AWR-06 §7.3 的标准实例字段在 M31 §7.4 实例包中的落位：

| AWR-06 字段 | 实例包位置 | 可见性 | 说明 |
|---|---|---|---|
| `nodes[{id, trajectory_ref, sensor{fov, range, noise_cov, bias_model}}]` | `agents.json`（实体与 Agent 模板）、`sensing.json`（视场、名义噪声、`bias_model{sigma_m, tau_s}`） | `brief` | `bias_model.rho` 在 `d2` 起移至 `hidden/truth.json` |
| `targets[{motion_model, trajectory_ref}]` | `tasks.json`（目标标识、先验 `x̄_{m,0}` 与 `P_0`、运动模型与 `q`；全部目标在 `t = 0` 释放） | `scheduled`（`t = 0` 释放，内容与 `brief.json` 的目标节一致） | 真值轨迹不入包，由种子在运行中实现；v0.1 的 `hidden/targets.json` 删除（§2.2 方式下无先验以外的生成参数） |
| `common_noise{process, gnss_bias{corr_time_s, sigma_m}}` | `sensing.json.bias_model` + `hidden/truth.json.bias{rho}` | 部分 `brief`、部分 `truth` | — |
| `partitions[]` | `hidden/comm_truth.json.partitions[]` | `truth` | 不公告的计划分区（M31 OR-3、§7.4.1）；`comm.json` 不含任何 S3 字段（`comm_zones[]`、`partitions_public[]`、`relays[]` 均为空） |
| — | `hidden/comm_truth.json.link_mask` | `truth` | 本文新增字段 |
| `degradations[]` | `hidden/truth.json.degradations[]` | `truth` | — |
| `estimate_period_s`、`horizon_s` | `objective.json` | 公开 | 以 `_ms` 整数量化（M31 量化规则） |
| `network_profile` | 移除 | — | 网络档由赛道决定（反馈第 7 条） |
| `target_aggregate` | 移除 | — | r02 §3.5 标量共识测试的字段，S3 不使用 |
| — | `objective.json{primary: u_is, e_max_mm, t_burn_ms, w_inf_ms, b_node_Bps, kappa_B}` | `brief` | 本文新增 |
| — | `brief.json` | `brief` | §8.3 |

视图按文件划分（M31 §7.4.3）：`brief` 文件在复位时下发，`scheduled` 文件由内核按事件时刻释放，`truth` 文件只位于 `hidden/` 并只供内核、网络注入器与评测器读取。S3 的全部隐藏旋钮（`ρ`、`g_deg`、`i_deg`、`t_deg`、链路掩码、分区）均为 `truth` 视图。

### 4.5 生成算法与约束

```
1. 以 instance 域抽取 n、M 外的全部生成参数（区间内取值），区域边长按 4.3 计算。
2. 部署：fixed 时以 Poisson 圆盘采样放置节点（最小间距 0.6 r_s）；ring_mixed 时节点按环序均匀置于半径 0.35 L 的圆上，
   节点编号从 0 起，偶数号为 fixed_sensor，奇数号为 multirotor，绕其环位以半径 30 m、速度 4 m/s 环绕（脚本航线）。
3. 目标先验：位置均值在区域内以 instance 域均匀抽取，距边界不小于 0.1 L；速度均值 v̄_m 按 §2.2 抽取大小与方向。
4. 障碍（d2）：2–4 个矩形，避开节点位置与环边视线（约束 G-4）。
5. 计算 ō、ov、cov_any；不满足约束则以下一 draw_index 重抽（至多 64 次，失败报生成错误）；重抽次数写入 manifest 的生成诊断。
6. 计算 B_need、B_cap、b_node_Bps；写出实例包并计算 instance_sha256。
```

| 约束 | 内容 |
|---|---|
| G-1 | `d1`–`d2`：`ō ∈ [1.5, 3.0]`、`cov_any ≥ 0.85`（初值，各规模档相同，pilot 按规模档报告 `ō` 分布与重抽率后校准；`ov` 不作约束） |
| G-2 | 每个目标在时域内至少被一个节点可见 30 s |
| G-3 | `d2`：退化节点 `i_deg` 不是任何目标的唯一可见节点（避免退化使某目标完全失去可用量测） |
| G-4 | `d2`：环上相邻节点在全时域保持视距（脚本航线计入），使 `abstract-radio@1 default` 下环边不因非视距断开 |
| G-5 | 生成结果只取决于 `(params, instance_seed)`；JCS 摘要逐字节可复现（PA-1） |

### 4.6 需求

| 编号 | 需求 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|
| R-S3-15 | 生成器实现 `params_space`、`generate`、`difficulty`；同输入生成的实例 JCS 摘要逐字节一致 | P0 | AC-S3-01 | AWR-06 §7.0 PA-1 |
| R-S3-16 | 难度档按 §4.2 的客观旋钮定义，网络档不写入实例；同一实例可在不同网络档赛道下配对运行 | P0 | AC-S3-15 | AWR-06 §7.0；M31 OR-3 |
| R-S3-17 | 规模按 §4.3 恒密度缩放；预算以 `κ_B` 表达，按 `per: agent` 执行并写出 `b_node_Bps` | P0 | AC-S3-13、AC-S3-18 | AWR-06 §7.0、§7.3 |
| R-S3-18 | 生成约束 G-1 至 G-5 在生成时检查，不满足者重抽或报错；重抽次数写入生成诊断 | P0 | AC-S3-15 | 本文 §4.5 |
| R-S3-19 | `truth` 视图的量（`ρ`、`g_deg`、`i_deg`、`t_deg`、链路掩码、分区）只出现在 `hidden/` 文件与真值层，不得出现在 `brief`、`scheduled` 文件及任何 Agent 可见通道（观测、回执、`init.v1`） | P0 | AC-S3-11 | AWR-06 §8.8；M31 R-M31-26、OR-3 |
| R-S3-20 | `d3` 档与 `n32` 在 P1 开放，`d4` 与 `n64` 在 P2 开放；开放前须满足 PA-1 至 PA-7 | P1/P2 | AC-D4-50 | AWR-06 §7.0、WP-D4-51 |

---

## 5. 指标

### 5.1 指标登记

S3 评测器 `infoshare@1`（`py:benchmarks/suites/infoshare/evaluator.py`）声明的 `MetricSpec`（字段含 M32 增补的 `target`、`level`、`epsilon`）：

| `id` | `kind` | `direction` | `range` | `level` | `primary` | `tier` | 说明 |
|---|---|---|---|---|---|---|---|
| `u_is` | task | max | [0, 1] | episode | 是 | 是 | 主指标（§5.2） |
| `p_trunc` | task | min | [0, 1] | episode | — | — | 截断占比，含缺失（§5.3） |
| `p_trunc_avail` | task | min | [0, 1] | episode | — | — | 信息可达而仍截断的占比 |
| `p_miss` | task | min | [0, 1] | episode | — | — | 缺失或无效估计占比 |
| `err_p50_m`、`err_p90_m` | task | min | [0, null] | episode | — | — | 有效估计的未截断误差中位数与 90% 分位 |
| `anees_in_band` | calibration | max | [0, 1] | scenario | — | 是（`epsilon = 0.05`） | 跨运行 ANEES 区间内比例（§5.4） |
| `anees_over`、`anees_under` | calibration | min | [0, 1] | scenario | — | — | 过度自信率、过度保守率 |
| `conserv_ratio` | calibration | target（`[0.8, 1.25]`） | [0, null] | scenario | — | — | 保守度 |
| `aoi_c_mean_s`、`aoi_c_p95_s` | coop | min | [0, null] | episode | — | — | 因果 AoI 下界（§5.5） |
| `aoi_d_mean_s`、`aoi_d_p95_s` | coop | min | [0, null] | episode | — | — | 一跳声明 AoI |
| `disagree_w2_m` | coop | min | [0, null] | episode | — | — | 认知分歧（§5.6）；方向字段只满足登记规则，不表示优劣 |
| `b_tx_ratio` | resource | min | [0, 1] | episode | — | 是 | 受理字节 `B_acc / B_cap`（与硬额度同口径，§5.7） |
| `cpu_ratio` | resource | min | [0, null] | episode | — | 是 | `CPU_ms / CPU_cap` |
| `fail_rate` | safety | min | [0, 1] | episode | — | — | 策略失败 Episode 占比（场景内） |

`err_p50_m`、`err_p90_m`、`aoi_*`、`disagree_w2_m` 的不利端无界，声明 `nullable = true`（M32 R-M32-89）：无有效估计、零通信策略（`aoi_c_*` 与 `aoi_d_*`，§5.5）、集中式赛道（`disagree_w2_m`、`aoi_*`，§7.1）与策略失败的 Episode 置 null，条目卡显示 `n/a` 并注明原因。场景级指标对失败 Episode 的处理为"全部单元记 `miss`"（§5.9），在评测器卡中声明。

另在 `diagnostics.infoshare{}` 中输出：`u_is_emax{10, 20, 40}`、`p_trunc_unavail`、按 `info_class` 与 `flow_id` 分解的 `B_app`/`B_acc`/`B_tx`/`B_rx` 与 `B_tx / B_cap`、`aoi_c` 与 `aoi_d` 的逐对均值矩阵、`aoi_c_ts`（§5.5）、零通信策略以 `t` 计的 `aoi_c` 参考值、`anees_t` 时间序列（场景级）、`submit_rate`、`rejected_cmds`、各发送类基线的有效发送率。

资源–效用支配分层向量为 `(u_is↑, b_tx_ratio↓, cpu_ratio↓, anees_in_band↑)`（AWR-06 §8.6"分层指标的可得性"）。

### 5.2 主指标 `U_IS`

```
单元集合 C = {(i, m, k) : i = 1..n, m = 1..M, k = k_0..K}，k_0 = max(1, ⌊t_burn / Δe⌋ + 1)
          即只计 t_k > t_burn 的估计时刻，t_k ≤ t_burn 的单元不计入（缺省 t_burn = 0，k_0 = 1；t_burn = 10 s、Δe = 1 s 时 k_0 = 11）
          ANEES 的单元集合 C_s（§5.4）、AoI 的时间平均区间（§5.5）、截断诊断（§5.3）与分歧（§5.6）使用同一 k_0
e_{i,m,k} = min( ‖p̂_{i,m,k} − p_m(t_k)‖ / e_max , 1 )    若 (i, k) 的报告有效且含目标 m 的有效估计
          = 1                                              否则（缺失、无效、迟交）
U_IS = 1 − (1/|C|) Σ_{(i,m,k)∈C} e_{i,m,k}
```

| 规则 | 内容 |
|---|---|
| 有效估计 | 数值有限；`x̂` 维数为 2 或 4 且与 `P` 一致；`P` 对称（相对差 ≤ 1e-9）、位置块最小特征值 ≥ 1e-4 m²、最大特征值 ≤ 1e8 m²；目标标识属于实例 |
| 迟交 | 估计时刻 `t_k` 的报告只认 `sealed_at_ns = t_k` 的决策；其他 tick 提交的估计报告忽略并计入 `diagnostics.late_reports` |
| 重复 | 同一决策中多个估计报告或同一目标多条估计，判该报告无效 |
| 等权 | 全部节点、目标、时刻等权；节点与目标数随规模变化不改变 `U_IS` 的尺度 |
| 只评位置 | 速度分量不评分；提交 4 维估计时只取位置块 |

`U_IS` 取值 [0, 1]、越大越好、可由 trace 离线重算（`truth` 层目标状态 + Agent 层报告）。设计量级（初值，pilot 确认）：`d2/n8` 上独立参考约有七成单元处于"无可用信息"状态而截断，集中式参考受共同偏差下限 `√ρ·σ_β` 约束，二者之差预期显著大于 PA-3 的 0.15；任何方法的误差都受偏差下限约束，`U_IS > 0.95` 在 `d1`–`d2` 不可达，满足 PA-4 上侧。

### 5.3 截断饱和诊断

截断使误差超过 `e_max` 的单元不再区分程度。当多种算法同时大量截断时，`U_IS` 的差异只反映"可得信息覆盖"而非融合质量（AWR-06 §7.3、PA-4）。本 Suite 以三项诊断识别并归因：

**(a) 截断占比与归因拆分**

```
avail(i, m, k) = 1  当且仅当  存在节点 v 与 v 对目标 m 的检测采集时刻 s，满足
                 t_k − W_inf ≤ s ≤ V_i(t_k)[v]           （v = i 时 V_i(t_k)[i] 为 i 自身最晚采集时刻）
p_trunc        = |{c : e_c = 1}| / |C|
p_trunc_avail  = |{c : e_c = 1 且 avail(c) = 1}| / |C|     信息因果可达而仍截断：算法或预算使用问题
p_trunc_unavail= |{c : e_c = 1 且 avail(c) = 0}| / |C|     无可达信息的结构性截断：由生成器参数与网络决定
```

`W_inf` 缺省 10 s；`V_i` 为 §5.5 的向量时钟。`avail` 只依赖交付记录与采集记录，与载荷内容无关，因此对全部策略可比。`V_i(t_k)[v]` 以 v 的全部采样时刻为基准，`avail` 只取其中的检测采样：向量时钟单调且因果，`V_i(t_k)[v] ≥ s` 表示 v 在时刻 s 及之前的全部采样对 i 因果可达，因此两者口径一致。

**集中式赛道的口径**。`centralized` 拓扑下全部节点的观测在同一密封时刻汇聚给集中式主体（表 B10），汇聚不经网络、不计费。评测器对 `execution_topology = centralized` 的运行取 `V_i(t)[v] = v 的最晚采集时刻`（对全部 i），即：

```
avail_cr(i, m, k) = 1  当且仅当  存在节点 v 与 v 对目标 m 的检测采集时刻 s，满足 t_k − W_inf ≤ s ≤ t_k
```

`avail_cr` 与 i 无关。TS-2 只在该口径下对 `π_central` 使用；分布式赛道的 `p_trunc_unavail` 不与之直接比较。

**(b) `e_max` 敏感度**。评测器同时计算 `e_max ∈ {10, 20, 40} m` 下的 `U_IS`（诊断，不成榜），pilot 报告基线序关系在三者之间的 Kendall τ。

**(c) PA-4 的 S3 判据**

| 编号 | 判据 | 处置 |
|---|---|---|
| TS-1 | 任一 pilot 配置上全部基线的 `p_trunc` 同时 > 0.5 | 不冻结该配置（AWR-06 PA-4 扩展） |
| TS-2 | `π_central` 在集中式赛道上按 `avail_cr` 计算的 `p_trunc_unavail` > 0.3 | 结构性饱和：调整 `ō`、`cov_any` 或区域边长（生成器公开参数），不调 `e_max` |
| TS-3 | 代表性协同基线的 `p_trunc_avail` > 0.3 且三档 `e_max` 下 Kendall τ < 0.6 | 截断掩盖融合质量差异：在 `dev` 上调整 `e_max`（全部难度档同步，Suite 主版本内只调一次） |

调整次序为先生成器、后 `e_max`，均计入 PA-7 的至多 3 轮相变扫描并写入 pilot 报告；`e_max` 冻结后不随难度档变化，以保持 `U_IS` 在档间的含义一致（反馈第 15 条）。

### 5.4 跨运行 ANEES

按 AWR-06 §7.3 与 r02 §3.5：NEES 跨独立蒙特卡洛运行（不同外生种子）逐时刻平均，不使用单次运行内的时间平均，也不使用逐样本 `|ln(NEES/d)|`。

```
对场景 s、单元 c = (i, m, k)，外生种子 ω ∈ Ω_s（m_s 个相互独立的运行）：
  ε_ω(c) = (p̂ − p)ᵀ P_pp⁻¹ (p̂ − p)                      d = 2（只位置块）
  ANEES(c) = (1/m_s) Σ_ω ε_ω(c)                           仅当全部 m_s 个种子在 c 上均有有效估计
  区间 [lo, hi] = [χ²_{0.025}(m_s·d), χ²_{0.975}(m_s·d)] / m_s
  类别：in（lo ≤ ANEES ≤ hi）| over（> hi）| under（< lo）| miss（任一种子缺失或无效）
anees_in_band = |in| / |C_s|；anees_over = |over| / |C_s|；anees_under = |under| / |C_s|；|miss| / |C_s| 记入诊断
```

| 种子数 `m_s` | `d = 2` 时的区间 `[lo, hi]` |
|---|---|
| 3 | [0.412, 4.816] |
| 4 | [0.545, 4.384] |
| 5（官方缺省） | [0.649, 4.097] |
| 10（pilot） | [0.959, 3.417] |

（以 χ² 分位数计算，经仓库环境 scipy 核算。）

| 规则 | 内容 |
|---|---|
| 独立单位 | 只把不同外生种子视为独立运行；同一运行内各节点共享相关偏差，不视为独立（AWR-06 §7.3） |
| 缺失 | 任一种子缺失或无效即记 `miss`，计入分母而不计入 `in`：选择性不提交不能改善一致性 |
| 种子数变化 | 基础设施重跑后有效种子数 `m_s < 5` 时以实际 `m_s` 取区间；`m_s < 3` 的场景该条目已为 `incomplete`（AWR-06 §8.6），不计算 |
| 场景级 | `anees_in_band` 为 `level = scenario` 指标；S3 估计目标为 `family`，场景值直接参与场景层重采样（M32 R-M32-10） |
| 期望值 | 对线性高斯且模型匹配的一致滤波器，`anees_in_band` 的期望约为 0.95；该性质用作评测器正确性锚点（AC-S3-07） |
| 保守度 | `conserv_ratio = Σ_{c,ω} tr(P_pp) / Σ_{c,ω} ‖p̂ − p‖²`（有效单元），1 为理想，> 1 保守 |

### 5.5 信息年龄

AWR-06 §7.3 定义 `AoI_{u←v}(t) = t − τ_{u←v}(t)`，`τ_{u←v}(t)` 为 u 在 t 时已收到的、源自 v 的最新样本采集时刻，由内核核验的 `source_sample_time_ns` 得到。内核的核验只保证声明值不晚于发送方已有的最晚样本（M28 R-M28-64），既不能把多跳转发的信息归属到最初来源 v，也不能防止发送方以较新的时刻声明陈旧内容。本 Suite 因此同时计算两种量（反馈第 2 条）：

**因果 AoI 下界 `AoI^c`（主口径）**。以交付记录与采集记录构造向量时钟，与载荷内容和 Agent 声明无关：

```
初值：V_u[v] = 0（时刻 0 的公开简报对全体可得），V_u[u] = u 的最晚采集时刻
按 trace 全序处理事件：
  采集 (u, s)：            V_u[u] = s
  消息提交（由 u 在 sealed 时刻 t_seal 的决策产生）：快照 S_msg = V_u(t_seal)
  消息交付给 w（delivered_at ≤ t）：V_w = max(V_w, S_msg)，在 w 的下一密封时刻生效
AoI^c_{u←v}(t_k) = t_k − V_u(t_k)[v]，u ≠ v
```

`AoI^c` 是"在实际通信模式下 u 关于 v 的信息至少有多陈旧"：任何策略的真实信息年龄不低于此值。复杂度为每次交付 `O(n)`，`n16` 时可忽略。

| 口径规则 | 内容 |
|---|---|
| 时钟推进 | `AoI^c` 的向量时钟由任何消息的交付推进，与载荷是否携带 v 的信息无关，这是"因果可达性下界"的定义所要求的；群发空载或无关小消息可人为压低 `AoI^c`，因此它不代表载荷实际使用 |
| 载荷类别口径 | 评测器另计诊断量 `aoi_c_ts`：只有 `info_class ∈ {target_state, raw_obs}` 的消息交付才推进时钟。`info_class` 由发送方声明、平台计量（M30 §6.5.2），`aoi_c_ts` 同样只是下界 |
| 条目卡标注 | `aoi_c_*` 标注"可达性下界，不代表载荷使用"，并与 `aoi_d_*`、`aoi_c_ts` 及按 `info_class` 分解的受理字节成对展示；方向 B 的对照（§9.1）以三者并报 |
| 零通信策略 | 未发送任何消息的运行（如 `local_kf`）`AoI^c_{u←v}(t_k) = t_k` 恒成立，均值由时域而非算法决定：`aoi_c_*` 与 `aoi_d_*` 置 null（条目卡 `n/a`，原因 `no_comm`），以 `t` 计的参考值只写入诊断 |
| 集中式赛道 | 观测在密封时刻汇聚，`V_u[v]` 取 v 的最晚采集时刻（§5.3），`AoI^c ≤ T_s` 只反映采样周期；`aoi_*` 置 null（原因 `centralized`） |

**一跳声明 AoI `AoI^d`（AWR-06 原口径）**：`τ^d_{u←v}(t_k) = max{source_sample_time_ns(msg) : msg 由 v 发给 u 且 delivered_at ≤ t_k}`，无则取 0；`AoI^d_{u←v}(t_k) = t_k − τ^d`。`AoI^d` 的来源样本可能来自 v 转发的第三方，因此 `AoI^d` 与 `AoI^c_{u←v}` 之间没有恒定大小关系；评测器检查的不变量是：每条消息的 `source_sample_time_ns ≤ max_w S_msg[w]`（声明值不晚于发送方在密封时刻因果可得的最晚样本），违反即判内核故障（内核核验 M28 R-M28-64 的离线复核）。

两者均报告全部有序对 `(u ≠ v)` 与全部 `t_k` 上的均值与 P95；逐对均值矩阵写入诊断，供方向 B 的"四类代表策略 AoI 分布"对照（AWR-06 §9.1）。

### 5.6 认知分歧

AWR-06 §9.1 方向 C 的分歧度在 S3 取必交估计计算。二维高斯的 2-Wasserstein 距离有闭式：

```
W₂²(N(μ₁, P₁), N(μ₂, P₂)) = ‖μ₁ − μ₂‖² + tr P₁ + tr P₂ − 2 √( tr(P₁P₂) + 2 √(det P₁ · det P₂) )
D_m(k) = (2 / (n(n−1))) Σ_{u<v} W₂(b_u^m(k), b_v^m(k))            只取双方均有效的对
disagree_w2_m = D̄ = mean_{m,k} D_m(k)
```

闭式只用 2 × 2 矩阵的迹与行列式（`tr √A = √(tr A + 2√det A)` 对 2 × 2 对称正定矩阵成立），不依赖矩阵平方根数值例程，结果确定。

### 5.7 资源与通信

| 量 | 定义 |
|---|---|
| `b_tx_ratio` | 团队受理字节 `B_acc = Σ_i B_acc,i` / `B_cap`。硬额度按受理字节扣减且不退还（M29 R-M29-14），`B_acc` 与额度同口径，`b_tx_ratio = 1` 当且仅当全部 Agent 额度耗尽；实际发射字节 `B_tx`（TTL 过期、分区与端点下线时 `B_tx < B_acc`）以 `B_tx / B_cap` 写入诊断 |
| 分类字节 | `B_app`、受理字节 `B_acc`、`B_tx`、`B_rx` 按 `info_class` 与 `flow_id` 分解（M29 R-M29-14） |
| `cpu_ratio` | 团队 CPU 时间 / 团队额度（B1：每 Agent 每仿真秒 100 CPU-ms） |
| 报告开销 | 必交报告不计通信字节，计 CPU |

### 5.8 Δ 族在 S3 的适用

| 量 | S3 的构造 | 说明 |
|---|---|---|
| `Δ_ind` | 对照 `π_ind = local_kf@1`（`none@1`，零通信） | 相对独立团队的净收益；`local_kf` 与全部基线共用滤波构件，`Δ_ind` 只反映共享与融合 |
| `Δ_cut` | 阻断全部投递 | 融合类策略预期 `Δ_cut ≈ Δ_ind` |
| `Δ_perm`（P1） | `perm_struct` 只置换 `x-awr-role: world_state` 字段（估计均值与协方差、信息对），保留 `seq`、`prov`、`deg`、`iter` 等控制字段（M30 §6.5.3） | 衡量对消息内容的敏感度；`opaque` 载荷只有 `perm_full` |
| `Δ_perm^(c)`（P1，E6） | 只置换 `info_class = c` 的载荷 | 方向 B 的分类别内容敏感度 |
| `R_central`、`G` | 对照 `π_central = central_kf@1` | `central_kf` 在相关偏差下不是上界，`R_central` 可为负、`G` 可大于 1（AWR-06 §8.5） |

### 5.9 失败与无效计分

失败与无效分两类处理，二者对资源指标的处理相同：资源是实际消耗，记 0 会使越小越好的 `b_tx_ratio`、`cpu_ratio` 取得最优值，崩溃或失活的策略将在资源轴上被评为最优并在支配分层中获得不应有的非支配位置（表 B9）。

| 情形 | `U_IS` 与结果类指标 | 资源指标 | 一致性 | 依据 |
|---|---|---|---|---|
| 某估计时刻报告缺失或无效 | 该节点该时刻全部单元 `e = 1` | 按实际消耗 | 该单元记 `miss` | AWR-06 §7.3；AC-S3-05 |
| 策略崩溃、失活（watchdog）、非法动作（`failure.policy = true`） | 该 Episode `U_IS = 0`（全部单元 `e = 1`）；`p_trunc`、`p_miss` 置 1；可空指标（§5.1）置 null；`fail_rate` 计 1 | `b_tx_ratio`、`cpu_ratio` 按实际消耗 | 该 Episode 全部单元记 `miss` | AWR-06 §8.6"失败计分"（资源按实际消耗）；ADR-D4-24；M32 R-M32-36 |
| 账本违规（`cpu_ms`） | `budget.policy = violation_invalidates`：`ranked_valid = false`；按 `invalid_policy = zero` 计分，方向化规则同上一行（`U_IS = 0`，结果类置不利端） | 按实际消耗（不采用"全部分量记 0"） | 该 Episode 全部单元记 `miss` | AWR-06 §8.6"无效运行"；M32 R-M32-38；反馈第 21 条 |
| 通信超额 | 不产生违规：超额副本以 `budget` 拒绝 | 受理字节不超过额度 | 不影响 | M29 R-M29-14 |
| 基础设施故障 | 原样重跑一次 | — | — | AWR-06 §8.6 |

### 5.10 需求

| 编号 | 需求 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|
| R-S3-21 | 评测器按 §5.1 登记 `MetricSpec`，`u_is` 为唯一 `primary`，分层指标为 `u_is`、`b_tx_ratio`、`cpu_ratio`、`anees_in_band`；可空指标声明 `nullable` | P0 | AC-S3-15 | AWR-06 §7.0、§8.6；M32 R-M32-01、R-M32-89 |
| R-S3-22 | `U_IS` 按 §5.2 计算，有效性、迟交、重复规则可单元测试 | P0 | AC-S3-05 | AWR-06 §7.3 |
| R-S3-23 | 截断诊断按 §5.3 输出 `p_trunc`、`p_trunc_avail`、`p_trunc_unavail` 与三档 `e_max` 的 `U_IS`；pilot 执行 TS-1 至 TS-3 | P0 | AC-S3-10 | AWR-06 §7.3、PA-4 |
| R-S3-24 | ANEES 按 §5.4 跨种子逐单元计算，缺失记 `miss`，区间按实际种子数取 | P0 | AC-S3-02、AC-S3-07 | AWR-06 §7.3；r02 §3.5 |
| R-S3-25 | AoI 按 §5.5 同时计算 `AoI^c` 与 `AoI^d`，并离线复核 `source_sample_time_ns ≤ max_w S_msg[w]` | P0 | AC-S3-09 | AWR-06 §7.3、§9.1 方向 B |
| R-S3-26 | 认知分歧按 §5.6 闭式计算 | P0 | AC-S3-17 | AWR-06 §9.1 方向 C |
| R-S3-27 | 全部指标可由 trace 离线重算，结果与在线评测逐字节一致 | P0（P1 由 `bench verify` 暴露） | AC-S3-12 | AWR-06 §4.5；WP-D4-07 |
| R-S3-28 | 失败与无效计分按 §5.9：资源指标在两类情形下均按实际消耗 | P0 | AC-S3-05 | ADR-D4-24；M32 R-M32-36、R-M32-38 |
| R-S3-29 | Δ 族按 §5.8 适配，`perm_struct` 只置换 `world_state` 字段 | P0（`Δ_perm` P1） | AC-D4-16、AC-D4-26 | AWR-06 §8.5；ADR-D4-22 |

---

## 6. 基线

### 6.1 基线总表

| 标识（`baseline:infoshare/…`） | 角色 | 算法与出处 | 消息 | 关键参数（初值） | 预期行为 | 阶段 |
|---|---|---|---|---|---|---|
| `local_kf@1` | `π_ind` | 本地 Kalman 滤波，不通信 | 无 | 过程与量测模型取简报 | 只对视场内目标准确；一致 | P0 |
| `naive_fusion@1` | 基线 | 朴素信息融合：广播当前融合估计，接收方以信息形式累加并视为独立（重复计入） | `infoshare.estimate.v1` | `share_period_s = 1` | 精度接近融合类，存在环与重复交换时过度自信，`d2` 起因相关偏差加剧 | P0 |
| `ci_fusion@1` | 基线（PA-2 代表性协同基线） | Covariance Intersection（Julier & Uhlmann, Proc. 1997 ACC, vol.4, pp.2369–2373 **[已核]**）：`P⁻¹ = ω P₁⁻¹ + (1 − ω) P₂⁻¹`，ω 以固定 30 次黄金分割搜索最小化 `tr(P)`；对 4 × 4 状态融合，按（目标, 来源）对批量化（同一 tick 内全部对的黄金分割并行推进，4 × 4 逆以 2 × 2 分块 Schur 补闭式计算，BC-1；与 M30 §6.7.4 一致） | `infoshare.estimate.v1` | `omega_iters = 30`、`share_period_s = 1` | 对未知互相关不过度自信（`P_CI` 构成保守包络）；独立噪声下偏保守，ANEES 以 `under` 为主（§6.3） | P0 |
| `gossip_dedup@1` | 基线 | 带来源去重的 gossip（Kempe, Dobra, Gehrke, FOCS 2003 [待核实] 的思想）：每轮向一个已知邻居推送估计与来源样本集合；来源不相交时信息形式合并，重叠时 CI 合并 | `infoshare.gossip.v1` | `fanout = 1`、`prov_max = 64` | 字节最省；`AoI^c` 较大 | P0（K-2 降级时顺延） |
| `avg_consensus@1` | 基线（共识型分布式 Kalman 的 P0 形态） | 信息形式平均共识（consensus on information）：本地量测更新后，对信息对 `(y, Y)` 以 Metropolis 权重与邻居平均（Olfati-Saber & Murray, IEEE TAC 49(9):1520–1533, 2004 [待核实页码]；Xiao & Boyd, SCL 53(1):65–78, 2004 [待核实]） | `infoshare.consensus.v1` | `iters_per_period = 1`、不按 n 放大 | 无放大时偏保守；分区时各分量独立收敛 | P0 |
| `central_kf@1` | `π_central` | 集中式 Kalman：一个决策主体（`CentralPolicy`）顺序处理全部合法量测；各节点偏差按 BC-2 作为 consider 参数处理，节点间偏差相关按 0（即"按独立假设"处理节点间相关，与 M30 §6.7.4 的表述同义，措辞统一见 §8.2 末段）；每个密封时刻为每个 Agent 提交同一份融合估计 | 无（`centralized` 拓扑，观测汇聚不经网络，表 B10） | — | 精度最高或接近最高；`ρ > 0` 时过度自信，不是上界 | P0 |
| `random_share@1` | `π_random` | 随机选择伙伴与发送时刻转发本地估计，朴素融合 | `infoshare.estimate.v1` | `p_send = 0.5` | 下界参照 | P0 |
| `kcf@1` | 基线 | Kalman 共识滤波（Olfati-Saber, CDC 2007 [待核实]；Carli 等, IEEE JSAC 2008 [待核实]）：本地更新后对估计均值做一步一致性校正 | 新登记 `infoshare.kcf.v1` | 一致性增益取文献缺省并在基线卡登记 | 与 `avg_consensus` 对照"共识于估计"与"共识于信息" | P1 |
| `event_share@1` | 基线 | 事件触发共享：本地估计相对上次发送值的马氏距离或迹变化超过阈值时发送（design-r2 InfoShare 行的"阈值发送、事件驱动发送"） | `infoshare.estimate.v1` | 阈值 `χ²_{0.9}(2)`、最长静默 5 s | 方向 B 的"何时共享"对照 | P1 |

P0 基线数为 6（AWR-06 §7.3 Suite 总览）另加 `π_random`，与 M30 §6.7.4 一致。r02 §3.5 所列分布式 Kalman 的另一条文献（Battistelli & Chisci, Automatica 2014 [待核实]）为"共识于信息"类方法的思想来源，`avg_consensus` 的基线卡注明其与该类方法的关系与差异，不声称复现原文。

### 6.2 统一约定

| 编号 | 约定 |
|---|---|
| BC-1 | 全部基线使用同一滤波构件 `baselines/common/kf`（M30 §6.7.0）：近匀速预测与更新、信息形式变换、CI 融合、NEES；4 × 4 及以下矩阵的求逆与分解以固定次序的闭式或 Cholesky 实现，不依赖 LAPACK 线程行为（F3 的闭式化先例）。按目标批量化：一条消息内 M 个目标的预测、融合与 CI 的 ω 搜索以形如 `(M, 4, 4)` 的数组逐元素闭式计算（4 × 4 逆取 2 × 2 分块 Schur 补），每次黄金分割迭代对全部目标只做一轮向量化运算，使单条消息的融合开销与 M 近似无关；成本以 WP-D4-15 第 7 周微基准确认（AC-S3-13） |
| BC-2 | 偏差处理：各节点把自身偏差 `β_i` 作为 consider 参数（不估计，只传播其协方差与互协方差），参数取简报的 `σ_β`、`τ_β`，节点间相关系数按 0 处理；`d0`–`d1` 中 `σ_β = 0`，该机制不起作用 |
| BC-3 | 邻居发现：以最近 5 s 内收到其消息的 Agent 为邻居；邻居集合为空时以广播发送（按副本计费）；不使用拓扑提示 |
| BC-4 | 预算节流：`tx_bytes` 为 `per: agent` 额度（§4.3），`Observation.ledger` 只给出本 Agent 的剩余受理额度（M28 R-M28-70），不含其他 Agent 的任何量；发送前按本 Agent 剩余额度与剩余时域计算可用速率（`剩余字节 / 剩余秒数`），超出时降频而不是被网络拒绝；节流规则对全部发送类基线相同，并在基线卡登记 |
| BC-5 | 时间对齐：收到的估计先按其 `t_ns` 预测到本地当前时刻再融合 |
| BC-6 | `source_sample_time_ns` 取本节点融合状态所依据的最晚样本采集时刻（含经消息获得的样本）；`gossip_dedup` 另在 `prov` 中列出来源样本 |
| BC-7 | 必交报告提交本地当前融合状态（4 维）在 `t_k` 的预测 |
| BC-8 | 不使用墙钟；随机性只经 `agent_seed` 派生的流（RN-3） |

### 6.3 预期序关系与机制

| 关系 | 预期 | 机制 | 检验 |
|---|---|---|---|
| 精度 | `U_central ≥ U_ci ≈ U_naive ≈ U_consensus > U_gossip > U_random ≥ U_ind`（`d2`） | 共享扩大可用信息覆盖；`gossip` 每轮只推一个邻居，信息年龄较大 | AC-S3-06 |
| 一致性 | 过度自信率 `anees_over`：`naive`、`central` 在 `d2` 高，`ci`、`gossip_dedup` 低；区间内比例 `anees_in_band` 对保守方法不一定高（见下表） | 重复计入与相关偏差使独立性假设的方法过度自信；CI 以保守换取不过度自信 | AC-S3-02 |
| 机制分解 | `nobias` 变体下 `naive` 与 `ci` 的过度自信率之差缩小 | 关闭相关偏差只留重复计入一种失真来源 | AC-S3-03 |
| 字节 | `gossip_dedup` < `avg_consensus` ≈ `ci` ≈ `naive` | 每轮单邻居推送 | 诊断 |

序关系与预期不符时（例如 `U_central < U_ci`），须给出机制解释后方可冻结（AWR-06 §7.0）。

**一致性三分类的预期（pilot 前占位，`m_s = 10`，pilot 后以实测替换）**。`anees_in_band` 是双侧区间内比例，保守滤波的 ANEES 低于下界时计入 `under`，因此进入支配分层的 `anees_in_band` 同时惩罚过度自信与过度保守。

| 基线 | `d1`：in / over / under | `d2`：in / over / under | 说明 |
|---|---|---|---|
| `local_kf` | 0.90–0.98 / < 0.05 / < 0.05 | 0.3–0.7 / 0.3–0.6 / < 0.1 | `d1` 模型匹配，为评测器锚点（AC-S3-07）；`d2` 因偏差与退化部分过度自信 |
| `naive_fusion` | 0.2–0.6 / 0.4–0.8 / < 0.05 | < 0.3 / > 0.7 / < 0.05 | 重复计入 |
| `ci_fusion` | 0.1–0.5 / < 0.05 / 0.5–0.9 | 0.2–0.6 / < 0.2 / 0.3–0.7 | 独立噪声下 CI 保守，`under` 为主 |
| `gossip_dedup` | 0.4–0.8 / < 0.1 / 0.2–0.5 | 0.3–0.6 / < 0.3 / 0.2–0.5 | 来源去重下以信息形式合并，重叠时退回 CI |
| `avg_consensus` | 0.2–0.6 / < 0.1 / 0.4–0.8 | 0.2–0.5 / < 0.3 / 0.3–0.6 | 无放大的平均共识低估信息量 |
| `central_kf` | 0.90–0.98 / < 0.05 / < 0.05 | < 0.3 / > 0.6 / < 0.05 | 节点间偏差相关按 0 处理 |
| `random_share` | 0.2–0.6 / 0.3–0.7 / < 0.1 | < 0.4 / > 0.5 / < 0.1 | 朴素融合 |

分层口径的取舍：保留 `anees_in_band`（双侧）进入分层，不改为只惩罚过度自信的 `1 − anees_over`。理由是后者可被平凡策略利用：任何策略把提交的 `P` 放大若干倍即可使 `anees_over = 0`，而 `u_is` 与资源指标不受影响，支配分层将系统性奖励夸大协方差。双侧口径对保守方法的惩罚是"一致性"的应有含义；`anees_over`、`anees_under` 与 `conserv_ratio` 在条目卡并报，使读者区分"保守"与"过度自信"。由此，AWR-06 §7.3"来源管理方法应在 ANEES 上受益"应理解为"在过度自信率上受益"（反馈第 22 条）。M30 v0.2 已把 R-M30-48 改为"CI 融合在独立噪声下满足保守性"，§6.9.4 的 `d1` 判据为 `anees_over ≤ 0.05` 与 `conserv_ratio ≥ 1`，与本文一致。

### 6.4 基线复现检验

| 基线 | 检验 | 判据 |
|---|---|---|
| `central_kf`、`local_kf` | `d0` | 协方差与 §9.1 闭式后验逐时刻一致（相对偏差 < 1%）；估计值与由 trace 量测计算的加权最小二乘解之差 < 0.01 σ（§9.1） |
| `ci_fusion` | `d0` | (1) 保守包络：`d0` 上每个节点在每个 `t_k` 的融合协方差满足 `P_CI − P_central(t_k) ⪰ 0`（`P_central` 为 §9.1 闭式集中式后验，是同一信息下任何估计的协方差下界；判据为最小特征值 ≥ −1e-9 · tr P_central）；另以单元测试对随机生成的联合协方差 `[[P₁, P₁₂], [P₂₁, P₂]]` 验证 `P_CI` 不小于 CI 估计按真实互协方差计算的均方误差矩阵（CI 一致性性质）；(2) ω 精确性：给定两路输入，`tr(P_CI)` 与独立实现的高精度数值最优 ω（区间 [0, 1] 上 1e-12 容差的有界一维极小化）所得 `tr(P)` 的相对差 < 1%；(3) 退化性质：各向同性不等协方差输入 `P₁ = aI`、`P₂ = bI`（`a < b`）时输出等于较优输入 `P₁`（ω = 1），相同输入时输出等于输入（幂等）。§9.1 的集中式闭式不作为 `ci_fusion` 的判据 |
| `local_kf` | `d1`（独立噪声、模型匹配） | `anees_in_band ∈ [0.90, 0.98]`（AC-S3-07） |
| `ci_fusion` | `d1` | `anees_over ≤ 0.05`（不过度自信）；`anees_under` 落入 §6.3 的预期区间并报告（保守性），不要求落入双侧区间 |
| `naive_fusion` | `d2` | 过度自信率显著高于 `ci_fusion`（AC-S3-02 的前提） |
| 全部 | 黄金决策摘要 | 固定实例与种子的决策 JSONL 摘要稳定（M30 §6.9.3）；BC-3、BC-4、BC-6 另有专用黄金用例（AC-S3-19） |
| `local_kf`、`random_share` 外的发送类 | 预算 | `d2` 全部 Episode 无 `budget` 拒绝或拒绝占比 < 1%（BC-4 生效） |

### 6.5 需求

| 编号 | 需求 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|
| R-S3-30 | P0 交付 §6.1 的 6 个基线与 `π_random`，标识与 M30 一致；每个基线附基线卡（出处与核实状态、参数、与原文的复现差异） | P0 | AC-S3-06 | AWR-06 §7.0 基线治理、§7.3 |
| R-S3-31 | 全部基线遵守 BC-1 至 BC-8 | P0 | AC-S3-13、AC-S3-19 | R-M30-44 |
| R-S3-32 | `π_ind`、`π_central`、`π_random` 由平台版本化并在每个 Board Version 预计算缓存 | P0 | AC-D4-16 | AWR-06 §7.0、WP-D4-13 |
| R-S3-33 | `ci_fusion` 为 PA-2 的代表性协同基线 | P0 | AC-S3-06 | AWR-06 §7.3 |
| R-S3-34 | P1 增补 `kcf`、`event_share`，增补后基线集合升 Suite 次版本，不改变既有 Board Version | P1 | AC-S3-14 | AWR-06 §7.6 |
| R-S3-35 | K-2 降级（CP-2 未达标）时 `gossip_dedup` 顺延至 P1，P0 基线减为 5 个 | P0 | — | AWR-06 §10.2 K-2 |

---

## 7. 赛道、划分与统计

### 7.1 赛道

| 赛道 id | 拓扑 | 观测 | 预算 | 网络 | 角色 |
|---|---|---|---|---|---|
| `dc-l1-b1-radio` | decentralized | L1 | B1 | `abstract-radio@1 default` | 主榜（CT `role = main`） |
| `dc-l1-b1-ideal` | decentralized | L1 | B1 | `ideal@1` | 对照榜，"通信约束代价"（CT `role = control`；其主榜 `dc-l1-b1-radio` 由 LB 以 `control_of` 声明，CT V-SU-03） |
| `cr-l1-b1-radio` | centralized | L1 | B1（单 Agent 额度 × n，M30 R-M30-15） | `abstract-radio@1 default` | 集中式参考列（CT `role = reference`），不参与分布式排名 |

`ideal@1` 下链路掩码与分区仍生效（M29 连通判据含掩码），因此 S3 的"通信约束代价"度量的是时延、丢包、容量与队列的代价，不含拓扑约束的代价；`validity.md` 如实说明。

**集中式赛道的规则**（表 B10）：

| 项 | 规定 |
|---|---|
| 观测汇聚 | 集中式主体在每个密封时刻收到全部所控 Agent 的观测，汇聚不经交互网络、不计通信字节；集中式主体若另发消息，照常计费 |
| 报告归属 | 集中式主体在每个 `t_k` 的决策映射中为每个 Agent 键各提交一份 `infoshare.estimate_report.v1`（可为同一融合估计）；缺失某 Agent 键视为该节点该时刻报告缺失（M30 R-M30-16，§5.9 第一行），单元集合 `C` 不变 |
| `avail` 与截断归因 | 按 §5.3 集中式口径 `avail_cr` |
| `AoI` | 不适用，`aoi_*` 置 null（§5.5） |
| `disagree_w2_m` | 提交同一估计时恒为 0，不具信息量，置 null（原因 `centralized`） |
| 一致性 | `anees_*` 照常计算（每 Agent 键的报告分别计入单元） |

### 7.2 划分与场景集

| 场景集 | 内容（pilot 前占位） | 用途 |
|---|---|---|
| `is-core` | 18 个场景：`d1` × {n4, n8, n16} × 2 + `d2` × {n4, n8, n16} × 4 | 主榜（`dev`、`validation`、`holdout` 各一组，区块隔离） |
| `is-anchor` | `d0` 锚点实例 3 个 | 评测器单元测试与教学；只在 `dev`，不成榜 |
| `is-mech` | `d2/n8` 的 `base`、`nobias`、`nobias_brief`、`noring` 变体各 10 个 | AC-S3-03 机制检验与来源分解；只在 `dev` |
| `is-stress`（P1） | `is-core` 的 6 场景子集 × X1 C、P、L、J、S 族（选取规则以 X1 §6.1 为准） | `stress` 划分 |

场景数在 pilot 后按 AWR-06 §8.6 的功效规则冻结（不少于 12）；`d1` 与 `d2` 的比例可在 pilot 中按配对差的 `σ_D` 调整。

### 7.3 统计

| 项 | S3 取值 |
|---|---|
| 估计目标 | `family`（场景层 bootstrap-t） |
| 样本 | 每场景 5 个外生秘密种子；`anees_in_band` 为场景级量，以场景内全部种子计算 |
| 实际意义阈值 | `u_is`：ε = 0.02；`b_tx_ratio`、`cpu_ratio`：`ε_r = 0.05`；`anees_in_band`：0.05 |
| 检验族 | F_rank、F_dom 按 AWR-06 §8.6 |
| pilot | `d1/n8` 与 `d2/n8` 各 10 实例 × 10 种子（AWR-06 §7.0）；ANEES 区间按 `m_s = 10` |

### 7.4 pilot 验收（S3 落实）

| 判据 | S3 的执行方式 |
|---|---|
| PA-1 | 生成器摘要逐字节一致；`is-anchor` 上 AC-S3-01 |
| PA-2 | 代表性协同基线 `ci_fusion` 与 `local_kf` 的配对差 ≥ 0.05 且 CI 不含 0；至少两对基线 Holm 校正后显著且 > 0.05 |
| PA-3 | `U_central − U_ind ≥ 0.15` |
| PA-4 | ≥ 75% 实例上无基线 `U > 0.95` 或 `U < 0.05`；TS-1 至 TS-3（§5.3） |
| PA-5 | 固定制品 100 次重跑 trace 事件哈希一致 |
| PA-6 | 单 Episode（`n8`、180 s、`d4_std`）宿主墙钟 ≤ 45 s；trace ≤ 200 MB |
| PA-7 | 可调公开参数：`ō` 区间（G-1，各规模档同一区间；`ov` 不作调参对象）、`cov_any`、`κ_B`（扫描 {1, 1.5, 2}，§4.3）、分区时长区间、`ρ` 区间、`speed_mps`、`ring_mixed` 的环半径系数；`e_max` 只按 TS-3 调整；至多 3 轮 |

---

## 8. 接口

### 8.1 Suite 清单（草案）

清单按 CT §5.1 书写；`tier`、`lifecycle`、`requires`、`limits`、`features`、`license` 等与本 Suite 语义无关的必填字段从略。

```yaml
schema: drone4d.suite.suite.v1
suite_id: infoshare
version: 1.0.0
families:
  - id: is-fixed
    kind: generated
    generator: {id: gen/is_fixed, version: 1}
    evaluator: {id: infoshare, version: 1}
    world_kind: planar
    channels: [ident, agent_state, net, reports, ledger]   # ident 使 sensor.capture 进入 minimal（CT §4.4.2）；reports、ledger 为 CT §5.1.2 与 M33 R-M33-69 的通道
    modes: [algorithm_isolated]
  - id: is-active                                    # P1
    kind: generated
    generator: {id: gen/is_active, version: 1}
    evaluator: {id: infoshare, version: 1}
    world_kind: planar
    channels: [ident, agent_state, net, reports, ledger]
    modes: [closed_loop]
schemas:
  messages: [infoshare.estimate.v1, infoshare.gossip.v1, infoshare.consensus.v1]
  reports: [infoshare.estimate_report.v1]
rng_domains:                                         # M31 R-M31-98；CT §5.1.1 顶层字段表尚未列出，见 §11 前置
  - {name: suite.infoshare.bias, object_id: "^(common|[a-z][a-z0-9_]{0,31}_[0-9]{5})$", time_index_max: 1800, draw_max: 1}   # time_index 上界 ⌈T / Δ_β⌉（T = 180 s）
  - {name: instance/is-fixed/layout}
  - {name: instance/is-fixed/target_prior}
  - {name: instance/is-fixed/bias}
  - {name: instance/is-fixed/degrade}
  - {name: instance/is-fixed/partition}
  - {name: instance/is-fixed/obstacle}
difficulty: [d0, d1, d2]                             # d3 于 P1、d4 于 P2 开放
scales: [n4, n8, n16]                                # n32 于 P1、n64 于 P2 开放
splits:
  - {id: dev, visibility: public}
  - {id: validation, visibility: aggregate_only, instance_set_commit: "sha256:..."}
  - {id: holdout, visibility: hidden, instance_set_commit: "sha256:...", seed_set_commit: "sha256:..."}
tracks:
  - {id: dc-l1-b1-radio, execution_topology: decentralized, observation_profile: L1, budget_profile: B1, network_profile: abstract-radio@1, network_params_ref: net/abstract-radio-default@1, timing: modeled, role: main}
  - {id: dc-l1-b1-ideal, execution_topology: decentralized, observation_profile: L1, budget_profile: B1, network_profile: ideal@1, network_params_ref: net/ideal@1, timing: modeled, role: control}
  - {id: cr-l1-b1-radio, execution_topology: centralized,   observation_profile: L1, budget_profile: B1, network_profile: abstract-radio@1, network_params_ref: net/abstract-radio-default@1, timing: modeled, role: reference}
ranked_physics: [planar@1]
ranked_clock: d4_std
metrics:
  - {id: u_is, kind: task, direction: max, range: [0, 1], primary: true, tier: true}
  - {id: p_trunc, kind: task, direction: min, range: [0, 1]}
  - {id: p_trunc_avail, kind: task, direction: min, range: [0, 1]}
  - {id: anees_in_band, kind: calibration, direction: max, range: [0, 1], level: scenario, tier: true, epsilon: 0.05}
  - {id: anees_over, kind: calibration, direction: min, range: [0, 1], level: scenario}
  - {id: conserv_ratio, kind: calibration, direction: target, target: [0.8, 1.25], range: [0, null], level: scenario}
  - {id: aoi_c_mean_s, kind: coop, direction: min, range: [0, null], nullable: true}
  - {id: aoi_c_p95_s, kind: coop, direction: min, range: [0, null], nullable: true}
  - {id: disagree_w2_m, kind: coop, direction: min, range: [0, null], nullable: true}
  - {id: b_tx_ratio, kind: resource, direction: min, range: [0, 1], tier: true}
  - {id: cpu_ratio, kind: resource, direction: min, range: [0, null], tier: true}
  - {id: fail_rate, kind: safety, direction: min, range: [0, 1], aggregation: fail_rate}
budget:
  policy: violation_invalidates
  dims:
    - {id: tx_bytes, per: agent, limit_expr: "duration_s * b_node_Bps"}   # 待 M31 R-M31-12/V-S-10 与 CT V-SU-06 变更（表 B3）；备选写法见 §4.3
    - {id: cpu_ms, per: agent, limit_expr: "duration_s * 100"}
    - {id: tokens, per: team, limit: 0}
reference_policies: {ind: ref/is_local_kf@1, central: ref/is_central_kf@1, random: ref/is_random_share@1}
scoring:
  estimand: family
  hierarchy: [scenario, seed, repeat]
  n_boot: auto
  invalid_policy: zero
  tie_rule: group_head
  holm: {families: [rank, dom, dom_ref]}       # 检验族名以 M32 R-M32-21、R-M32-96 与 CT Scoring 为准（XR-14）
  epsilon_practical: 0.02
  epsilon_equiv: 0.05
  dominance: {metrics_from: tier, tolerance: epsilon, scc_merge: true}
validity_statement: {path: docs/suites/infoshare/validity.md, sha256: "..."}
```

### 8.2 必交估计报告 `infoshare.estimate_report.v1`

v0.1 称报告"与基线消息同 schema"，但字段与 M30 §6.5.3 的消息 `infoshare.estimate.v1`（`est` 为 `f4[·, 14]` 的 `x-awr-ndarray`，另有 `tg_ix`、`t_ms`、`seq`，允许目标子集）不同：报告以顶层 `valid_at_ns` 代替逐目标 `t_ns`、要求恰好覆盖全部目标并升序、只用 binary64。同名 schema 下两套字段规则会使校验与审计无法按名分派，v0.2 把报告登记为独立 schema `infoshare.estimate_report.v1`，在清单 `schemas.reports[]` 中声明（CT §4.3.5、§5.1.1）；消息 `infoshare.estimate.v1` 保持 M30 定义不变。报告 schema 的契约文件位置 CT 尚未规定，建议与消息 schema 同一命名规则（`ct:coord/msg/` 之外另设 `ct:coord/report/infoshare.estimate_report.v1.schema.json`），由 CT 裁定（§11 前置）。

`ReportRequest.report` 为按登记 schema 校验的映射（CT §4.3.5、FB-CT-04），数值数组以 JSON 数字（binary64）书写，不使用消息方言的 `x-awr-ndarray` base64 编码。

| 字段 | 类型 | 约束 |
|---|---|---|
| `valid_at_ns` | int64（按 CT 的大整数编码规则） | 等于该决策的 `sealed_at_ns`，且为 `Δe` 的整数倍 |
| `est[]` | 数组 | 恰好覆盖实例全部目标、按 `target` 码点序升序、无重复 |
| `est[].target` | string | 实例目标规范 id（如 `tgt_00003`） |
| `est[].x` | number[2] 或 number[4] | 位置（及速度），世界坐标，m 与 m/s |
| `est[].P` | number[2][2] 或 number[4][4] | 与 `x` 同维，对称正定（§5.2） |

报告经 `ReportRequest(report_schema = "infoshare.estimate_report.v1", report = {…}, evidence_ids = [])` 提交。

`central_kf` 的描述在 M30 §6.7.4 为"按独立假设顺序处理全部合法量测"，本文 §6.1 为"偏差按 consider 参数处理、节点间相关按 0"；二者同义（节点间偏差按独立处理），本文表述更具体，建议 M30 的基线表采用本文措辞（§11 跨文档依赖）。

### 8.3 简报 `brief.json`（Agent 可见）

```json
{
  "schema": "drone4d.suite.brief.infoshare.v1",
  "region": {"xmin_mm": 0, "ymin_mm": 0, "xmax_mm": 400000, "ymax_mm": 400000},
  "estimate_period_ms": 1000, "horizon_ms": 180000, "e_max_mm": 20000,
  "report_schema": "infoshare.estimate_report.v1",
  "targets": [{"id": "tgt_00000", "prior_mean": [112.4, 287.9, 2.83, -2.83], "prior_cov_diag": [225.0, 225.0, 4.0, 4.0]}],
  "motion_model": {"kind": "cv", "q_m2ps3": 0.05},
  "nodes": [{"id": "node_00000", "kind": "fixed_sensor", "pos_m": [200.0, 60.0],
             "sensor": {"range_m": 120.0, "fov_deg": 360.0, "sigma_nominal_m": 3.2, "period_ms": 500, "P_d": 0.9}}],
  "bias_model": {"kind": "gauss_markov", "sigma_m": 3.0, "tau_s": 60.0, "rho": null},
  "budget": {"kappa_B": 1.0, "b_node_Bps": 1280, "per": "agent"}
}
```

数值为示意，测试以生成器输出为准，不得把本例数值当作验收样例。`prior_mean` 的速度分量为 `v̄_m`（本例 `|v̄_m| = 4 m/s`，§2.2）；`b_node_Bps = 1280` 为 `n8`、`κ_B = 1` 下的占位值，落在 §4.3 估算区间 0.8–1.5 kB/s 内，正式值以参考编码器实测为准（AC-S3-18）。简报公开全部节点的部署位置（§2.9 的设计选择），不含 `β_i` 的任何信息。

### 8.4 评测器接口

```python
# py:benchmarks/suites/infoshare/evaluator.py（草案）
class InfoShareEvaluator:                      # 实现 AWR-06 §4.2 Evaluator 协议
    id = "infoshare"; version = "1"
    channels = ("ident", "agent_state", "net", "reports", "ledger")
    def evaluate(self, run, instance, subset=None) -> "RunResult": ...
        # Episode 级：u_is、p_trunc*、err_p*、aoi_*、disagree_w2_m、b_tx_ratio、cpu_ratio、diagnostics.infoshare
    def evaluate_scenario(self, runs: "Sequence[RunView]", instance) -> Mapping[str, float]: ...
        # 场景级：anees_in_band、anees_over、anees_under、conserv_ratio、anees_t（诊断）
    def recompute(self, runs, instance, seed_weights) -> Mapping[str, float]: ...
        # M32 R-M32-10 回调；S3 估计目标为 family，正式成绩不调用，保留供 fixed_set 研究使用
```

`subset` 参数（子编队投影）对 S3 只按节点子集计算单元集合 `C`，用于条目卡的分组诊断。

### 8.5 依赖的平台能力

| 能力 | 提供方 | 本 Suite 的要求 |
|---|---|---|
| `fixed_sensor` 实体与脚本轨迹实体 | M28 | 脚本轨迹实体按实例航线推进、不受 Agent 控制、轨迹为外生量（反馈第 14 条） |
| 键控随机域、Suite 级域 `rng_domains[]` 与 `rng_domains.v1` 登记 | M28、M31、CT | §3.2 的域与 `object_id` 格式（M28 R-M28-27、M31 R-M31-98；CT 清单顶层字段待补） |
| 平面 `L1` 传感器与 Suite 阶段参与者 | M28 | 检出与噪声按 R-M28-59；S3 在 `SENSOR_CAPTURE` 叠加 `β_i` 与退化增益 |
| 链路掩码与分区 `PartitionCtl` | M29 | 静态掩码与分区按位与；`ideal@1` 亦受掩码约束（R-M29-31 的"拓扑恒为全连通"措辞待 M29 统一） |
| `source_sample_time_ns` 核验、`info_class` 计量 | M28、M29 | §5.5、§5.7 |
| `ReportRequest` 评测通道 `reports`；报告 schema 登记 | M28、M31、CT | 报告进入 Agent 层 trace，不回传；`infoshare.estimate_report.v1` 的契约文件位置待 CT 规定 |
| `sensor.capture` 真值记录 | CT、M33 | 声明 `ident` 通道后在 `minimal` 级别写出（表 B7） |
| `level = scenario` 指标、方向化支配与失败计分 | M32 | `anees_in_band` 进入分层；§5.9 按 R-M32-36、R-M32-38 |
| `limit_expr` 实例变量 | M28（已支持）、M31 与 CT（待变更） | `b_node_Bps`；备选写法见 §4.3 |
| `per: agent` 字节额度与 `Observation.ledger` | M28、M29 | 只暴露本 Agent 剩余量 |
| `CentralPolicy` 一控多与按 Agent 键提交报告 | M30 | §7.1 集中式赛道规则 |
| 滤波构件 `common/kf`、消息 schema | M30 | §6.2 |

---

## 9. 实例样例

### 9.1 `d0` 锚点：`capl:infoshare/fixed-anchor/d0/n4@1`

4 个固定节点全覆盖观测 1 个静止目标；名义噪声 `σ_z = (2, 3, 4, 5) m`；先验 `σ_p0 = 30 m`；采样 2 Hz（`t = 0.5 j s`，`j ≥ 1`）；`P_d = 1`；无偏差；60 s；锚点运行 `ideal@1`。每轴后验方差有闭式：

```
集中式：  Var_c(t) = ( 1/σ_p0² + 2t · Σ_i 1/σ_{z,i}² )⁻¹，Σ_i 1/σ_{z,i}² = 0.463611 m⁻²
本地 i：  Var_i(t) = ( 1/σ_p0² + 2t / σ_{z,i}² )⁻¹
期望截断误差（各向同性二维高斯，截断概率可忽略）：E[e] = √(π/2) · √Var / e_max
```

| t（s） | 集中式每轴标准差（m） | 本地标准差（m），节点 1–4 |
|---|---|---|
| 1 | 1.0379 | 1.4126、2.1160、2.8159、3.5112 |
| 2 | 0.7341 | 0.9994、1.4981、1.9956、2.4914 |
| 5 | 0.4644 | 0.6323、0.9482、1.2638、1.5789 |
| 10 | 0.3284 | 0.4472、0.6707、0.8940、1.1173 |
| 30 | 0.1896 | 0.2582、0.3873、0.5163、0.6453 |
| 60 | 0.1341 | 0.1826、0.2738、0.3651、0.4564 |

期望主指标（`k = 1..60`）：`E[U_IS](central_kf) ≈ 0.9847`；`E[U_IS](local_kf)` 节点均值 ≈ 0.9636（各节点 0.9792、0.9688、0.9584、0.9480）。CI 在各向同性不等协方差输入下退化为取较优输入（ω = 1），故 `ci_fusion` 各节点的协方差在节点 1 的本地协方差附近（受交换时延与本地量测更新影响），与集中式之差是 CI 保守性的直接体现。因此上式闭式只是 `central_kf` 与 `local_kf` 的判据，对 `ci_fusion` 只作为保守包络的下界（§6.4）。

**AC-S3-01 的可检验形式**。蒙特卡洛估计的均方误差在有限种子下的相对标准误约为 `√(2/(N·d))`，"误差与解析值偏差 < 1%"若按经验均方误差检验需要上万次运行，不可行（反馈第 6 条）。本文对 `central_kf` 与 `local_kf` 采用三项确定性检验：(i) 滤波协方差与上式闭式逐时刻相对偏差 < 1%（线性高斯滤波的协方差与量测值无关）；(ii) 估计值与由 trace 中实际量测计算的加权最小二乘解之差 < 0.01 σ；(iii) 评测器对 (ii) 的估计序列计算的 `U_IS` 与手算一致（逐位）。另以 1,000 次种子运行检查经验 `U_IS` 均值与期望值之差落在其 95% 蒙特卡洛区间内。`ci_fusion` 在 `d0` 上执行 §6.4 的三项检验（保守包络 `P_CI ⪰ P_central`、ω 与高精度数值最优解之差 < 1%、各向同性不等协方差输入的退化与幂等）；CI 的 ω 不存在闭式解，v0.1"闭式 CI 解"的措辞删除，判据中的参照指"给定输入下的高精度数值最优 ω"。

### 9.2 标准实例：`capl:infoshare/fixed-ring/d2/n8@1`（节选）

```json
// objective.json
{"primary": "u_is", "e_max_mm": 20000, "estimate_period_ms": 1000, "horizon_ms": 180000,
 "t_burn_ms": 0, "w_inf_ms": 10000, "kappa_B": 1.0, "b_node_Bps": 1280}

// agents.json（8 个节点；环上交替：偶数号 fixed_sensor，奇数号脚本航线 multirotor，§4.5）
{"entities": [
   {"entity_id": "node_00000", "kind": "fixed_sensor", "initial_state": {"pos_mm": [200000, 60000]}},
   {"entity_id": "node_00001", "kind": "multirotor", "script": {"orbit_center_mm": [298995, 101005],
     "radius_mm": 30000, "speed_mmps": 4000, "phase_deg": 37.0}}],
 "agents_template": [{"agent_id": "a_node_00000", "kind": "sensor_agent", "controls": ["node_00000"], "scope": "NONE"}]}

// sensing.json
{"nodes": [{"id": "node_00000", "range_mm": 120000, "fov_deg": 360, "sigma_nominal_mm": 3200, "period_ms": 500, "P_d": 0.9}],
 "bias_model": {"kind": "gauss_markov", "sigma_mm": 3000, "tau_ms": 60000}}

// comm.json（brief 视图；S3 无公开通信特征）
{"comm_zones": [], "partitions_public": [], "relays": []}

// hidden/comm_truth.json（truth 视图；M31 OR-3、§7.4.1）
{"link_mask": {"kind": "undirected_edges",
   "edges": [[0,1],[1,2],[2,3],[3,4],[4,5],[5,6],[6,7],[7,0]]},
 "partitions": [{"t_start_ms": 47300, "duration_ms": 72000, "groups": [[0,1,2,3],[4,5,6,7]]}],
 "nonce": "..."}

// hidden/truth.json（truth 视图）
{"bias": {"rho": 0.68},
 "degradations": [{"node": "node_00005", "t_ms": 120000, "sigma_gain": 3.0}],
 "nonce": "..."}

// manifest.json 节选
{"instance_id": "capl:infoshare/fixed-ring/d2/n8@1", "family": "is-fixed",
 "generator": {"id": "gen/is_fixed", "version": 1},
 "difficulty": {"o_bar": 2.2, "ov": 0.28, "cov_any": 0.95, "kappa_B": 1.0, "speed_ratio": 0.57}}
```

数值为示意，测试以生成器输出为准，本例不作验收样例；`o_bar`、`ov` 取 §4.3 粗算表的 `n8`、`ring_mixed` 量级，`speed_ratio` 按 §4.2 的定义取典型值。`difficulty` 只含公开旋钮，`cycle_rank = 1`、`part_frac = 0.40`、`rho = 0.68`、`g_deg = 3.0` 只写入 `hidden/`。

### 9.3 实验配置样例

```yaml
schema: drone4d.coord.experiment.v1
name: is-d2n8-fusion-baselines
suite: {id: infoshare, version: 1.0.0}
split: dev
instances: {family: is-fixed, difficulty: d2, scale: n8, count: 10}
track: {execution_topology: decentralized, observation_profile: L1, budget_profile: B1, network_profile: abstract-radio@1}
run_mode: algorithm_isolated
mode: exogenous
seeds: {exogenous: {range: [0, 10]}, repeats: 1}
clock_profile: d4_std
physics: planar@1
network: {params_ref: net/abstract-radio-default@1}
policies:
  - {name: local_kf,      ref: "baseline:infoshare/local_kf@1"}
  - {name: naive_fusion,  ref: "baseline:infoshare/naive_fusion@1"}
  - {name: ci_fusion,     ref: "baseline:infoshare/ci_fusion@1"}
  - {name: gossip_dedup,  ref: "baseline:infoshare/gossip_dedup@1"}
  - {name: avg_consensus, ref: "baseline:infoshare/avg_consensus@1"}
  - {name: mine,          ref: "module:my_pkg.fusion:MyFusion"}
reference: {independent: local_kf, central: central_kf}
interventions: [ind, cut]
evaluation: {compare: paired, ci: {method: cluster_bootstrap_t, estimand: family, n_boot: auto, level: 0.95}, multiple_comparison: holm}
outputs: {dir: runs/is-d2n8, trace_level: standard, report: [md, html]}
```

### 9.4 最小策略样例（草案接口）

```python
from anet4d.coord import Policy, Decision, ReportRequest, SendRequest
from anet4d.coord.kf import CVBank, ci_fuse, encode_estimates, report_estimates   # common/kf 的公开入口（草案）

class CiShare(Policy):
    def reset(self, ctx):
        self.bank = CVBank.from_brief(ctx.brief)                 # 每目标一个近匀速滤波器，含偏差 consider 项
        self.period_ns = ctx.brief.estimate_period_ns
    def step(self, obs, inbox):
        self.bank.predict(obs.sealed_at_ns)
        self.bank.update(obs.sensors)                            # 已关联检测
        for m in sorted(inbox, key=lambda m: (m.delivered_at_ns, m.message_id)):
            self.bank.fuse_message(m, rule=ci_fuse)              # 先预测到当前时刻再 CI 融合
        if obs.sealed_at_ns % self.period_ns:
            return Decision(tick_id=obs.tick_id)
        report = report_estimates(self.bank, obs.sealed_at_ns)        # 映射：{valid_at_ns, est[...]}，binary64
        body = encode_estimates(self.bank, obs.sealed_at_ns)          # 字节：infoshare.estimate.v1 消息载荷
        return Decision(tick_id=obs.tick_id,
            reports=(ReportRequest("infoshare.estimate_report.v1", report, ()),),
            messages=(SendRequest((), "est", "infoshare.estimate.v1", body, 2_000_000_000,
                                  info_class="target_state",
                                  source_sample_time_ns=self.bank.latest_sample_ns),))
```

`destinations = ()` 为广播并按副本计费；实际基线按 BC-3 只发给已知邻居。

---

## 10. 验收标准

| 编号 | 内容 | 判据 | 阶段 | 承接 |
|---|---|---|---|---|
| AC-S3-01 | PA-1；`d0` 解析一致性 | 生成器摘要逐字节一致；`is-anchor` 上 `central_kf` 与 `local_kf` 满足 §9.1 的三项确定性检验（协方差相对偏差 < 1%、估计与加权最小二乘解之差 < 0.01 σ、`U_IS` 逐位一致）；`ci_fusion` 满足 §6.4 的三项检验（`P_CI ⪰ P_central`、ω 与高精度数值最优解所得 `tr P` 相对差 < 1%、各向同性不等协方差输入退化为较优输入且相同输入幂等）；1,000 次种子运行的经验 `U_IS` 均值落在期望值的 95% 蒙特卡洛区间 | P0 | AWR-06 AC-S3-1（判据细化，反馈第 6 条） |
| AC-S3-02 | 一致性诊断的区分度 | pilot `d2/n8`（10 实例 × 10 种子）上 `anees_over(naive_fusion) − anees_over(ci_fusion)` 的场景层配对 95% CI 下界 > 0；声明 `U_IS` 只衡量精度；PA-3 成立 | P0 | AWR-06 AC-S3-2（判据细化） |
| AC-S3-03 | 机制检验与来源分解 | 记 `D(v) = over(naive) − over(ci)`（变体 v 上，§3.4）。分解：相关偏差的增量贡献 `S_bias = D(base) − D(nobias)`，无偏差时的重复计入贡献 `S_rep = D(nobias)`，信息因素 `S_info = D(nobias) − D(nobias_brief)`，拓扑因素 `S_topo = D(base) − D(noring)`；份额 `S_bias / D(base)`、`S_rep / D(base)` 以场景层配对 bootstrap 给出 95% CI。判据：(i) `S_bias` 的配对 95% CI 下界 > 0；(ii) `S_rep`、`S_info`、`S_topo` 及两项份额的点估计与 CI 写入 pilot 报告；(iii) 若 `S_topo` 的 CI 不含 0 且符号与 §3.4 的预期相反（预期全连通下重复计入更强，即 `S_topo < 0`），须给出机制解释后方可冻结 | P0 | AWR-06 AC-S3-3（判据细化，反馈第 11 条） |
| AC-S3-04 | PA-4、PA-5、PA-6 | 按 §7.4；TS-1 至 TS-3 的检查结果写入 pilot 报告 | P0 | AWR-06 AC-S3-4 |
| AC-S3-05 | 缺失、失败与无效计分 | 单元测试覆盖：报告缺失、目标缺项、`P` 非正定、维数不符、迟交、重复报告、策略崩溃、`cpu_ms` 违规、集中式赛道缺失 Agent 键；各情形 `e = 1` 且一致性单元记 `miss`；崩溃与违规两类情形下 `b_tx_ratio`、`cpu_ratio` 等于实际消耗（不为 0），可空指标为 null | P0 | AWR-06 AC-S3-5 |
| AC-S3-06 | 基线序关系与 PA-2 | `d2` 上 `U_central ≥ U_ci` 与 `U_ci > U_ind`（后者配对 95% CI 下界 > 0）；PA-2 以 `ci_fusion` 为代表性协同基线；违反时给出机制解释后方可冻结 | P0 | 本文新增（对应 S1 规格 AC-S1-02、S2 规格 AC-S2-02） |
| AC-S3-07 | ANEES 评测器正确性锚点 | pilot `d1/n8` 上 `local_kf` 的 `anees_in_band ∈ [0.90, 0.98]`；人为把 `P` 缩小 4 倍的变异策略 `anees_over > 0.5`；`ci_fusion` 在 `d1/n8` 上 `anees_over ≤ 0.05`，`anees_under` 落入 §6.3 的预期区间（pilot 后以实测替换占位），并在 pilot 报告中说明双侧分层口径对保守方法的影响 | P0 | 本文新增 |
| AC-S3-08 | 外生注入的统计性质与配对 | (i) 同实例同种子下改变策略发送频率，偏差、量测噪声、检测与目标轨迹逐位一致；(ii) 确定性单元测试：`BiasField` 的一步递推与 `py:sim/sensors/gm.py` 的 `phi`、`step` 逐位相同；(iii) 集合统计检验：≥ 2,000 个独立外生种子，每个种子在 `t ∈ {0, 3τ_β, 6τ_β}` 取样（相隔 ≥ 3τ_β），检验每轴方差与 `σ_β²`、节点间相关与 `ρ`、滞后 `τ_β` 的自相关与 `e^{−1}` 落入模拟 95% 区间，并以注入 10% 幅度缩放与 `φ` 误用（步长取 1 s）两种变异验证检验能拒绝；(iv) 退化在 `t_deg` 生效 | P0 | 本文新增（对应 AC-D4-05） |
| AC-S3-09 | AoI | 三类人工标注用例（一跳、两跳、分区期间）上 `AoI^c` 与手算一致；全部消息满足 `source_sample_time_ns ≤ max_w S_msg[w]`；分区窗内跨分量的 `AoI^c` 单调增长；零通信与集中式运行的 `aoi_*` 为 null；`aoi_c_ts` 在只含 `info_class = intent` 消息的合成运行中不被推进 | P0 | 本文新增 |
| AC-S3-10 | 截断诊断 | `p_trunc = p_trunc_avail + p_trunc_unavail`（含缺失单元按 `avail` 归类）；集中式运行按 `avail_cr` 归类；三档 `e_max` 的 `U_IS` 与 Kendall τ 写入条目卡诊断与 pilot 报告 | P0 | 本文新增 |
| AC-S3-11 | 泄露与偏差不可观测性 | (i) 简报、`scheduled` 文件、观测、`init.v1` 与回执中不含 `ρ`、`i_deg`、`t_deg`、`g_deg`、链路掩码、分区、偏差实现与真值轨迹；(ii) `hidden/comm_truth.json` 与 `hidden/truth.json` 的每个字段值在 `agent_brief` 包与 Agent 层 trace 中均不出现（M33 泄露审计按字段值扫描）；(iii) 键空间检验：`suite.infoshare.bias` 的全部键与 `sensor` 域 `nav:<entity_id>` 键不相交；(iv) 统计检验：`d2/n8` 上 `(ŝ_i − s_i)` 与 `β_i` 的样本相关落入零相关的模拟 95% 区间；(v) 变异策略 `nav_correct`（以 `ŝ_i − s_i` 扣除量测偏差的 `ci_fusion`）在 `d2` `dev` 上相对 `ci_fusion` 的 `U_IS` 配对差 95% CI 上界 < 0.02；(vi) `EntityCommand` 被拒并审计；报告不出现在任何其他 Agent 的观测中 | P0 | 本文新增（对应 AC-D4-08） |
| AC-S3-12 | 离线重算 | 由 trace 重算的全部 Episode 级与场景级指标（含 `AoI^c` 与 `D̄`）与在线评测逐字节一致 | P0 | 本文新增（对应 AC-D4-08） |
| AC-S3-13 | 成本 | (i) 第 7 周微基准：`ci_fusion`、`avg_consensus` 在 `n16`（`M = 12`）下单条消息融合与单次 `step` 的 CPU 时间分布，按（目标, 来源）对批量化实现（BC-1；M30 §6.7.4 `ci_fusion` 行）；(ii) 主判据（与 M30 R-M30-67 同口径）：`d2/n16`、`d4_std` 下每 Agent 每仿真秒累计 CPU 时间的 p95 ≤ 50 CPU-ms、最大值 ≤ 100 CPU-ms（B1 额度）；(iii) 单次 `step` CPU p99 ≤ 50 ms，p95 ≤ 10 ms 为设计目标，微基准表明不可达时以 (ii) 为准并向 M30 登记；(iv) 评测器单 Episode（`n16`）耗时 ≤ 5 s；(v) `d2` 发送类基线 `budget` 拒绝占比 < 1% | P0 | 本文新增 |
| AC-S3-14 | P1 扩展 | `kcf`、`event_share` 基线卡与复现检验通过；`is-active` 族满足 PA-1 至 PA-7；`d3`、`n32` 满足 PA-1 至 PA-7 或从 P1 交付范围删除并登记（AC-D4-50） | P1 | 本文新增 |
| AC-S3-15 | 生成约束、难度档与指标登记 | (i) `n4`、`n8`、`n16` × `d1`、`d2` 各生成 ≥ 200 个实例，报告 G-1 至 G-5 的满足率、`ō`、`ov`、`cov_any` 分布与重抽次数分布；重抽失败率 < 1%，任一档失败率超限即按 PA-7 调整 G-1；(ii) 同一实例在 `ideal@1` 与 `abstract-radio@1` 下装载，实例包逐字节相同、`instance_sha256` 不变（网络档不进入实例）；(iii) 清单 `metrics[]` 通过 M32 `MetricSpec` 校验，分层向量恰为 `(u_is, b_tx_ratio, cpu_ratio, anees_in_band)`，可空指标声明 `nullable` | P0 | 本文新增 |
| AC-S3-16 | 网络掩码回放 | 单元测试：环形链路掩码 × 分区 × `ideal@1` 的合成发送序列回放，验证 (i) 掩码外链路与分区跨组链路零交付；(ii) 掩码与分区按位与（三者叠加 X1 P 族时同样成立）；(iii) `ideal@1` 下仍受掩码约束；(iv) 分区结束后环边恢复 | P0 | 本文新增 |
| AC-S3-17 | 分歧闭式对拍 | 10,000 对随机二维对称正定矩阵（条件数覆盖 1–1e6）上，§5.6 的 `W₂` 闭式与以 `scipy.linalg.sqrtm` 计算的数值解之相对差 < 1e-9；闭式结果跨平台逐位一致 | P0 | 本文新增 |
| AC-S3-18 | 预算对账 | 参考编码器对 `n4`、`n8`、`n16` 的参考消息实测线上字节 `W_ref(M)` 与生成器写入的 `b_node_Bps` 满足 `b_node_Bps = κ_B · 2 · W_ref(M) / Δe`（整数取整规则一致）；`per: agent` 额度下 `Observation.ledger` 只含本 Agent 剩余量；`b_tx_ratio` 以受理字节计且 `≤ 1`；pilot 报告 `κ_B ∈ {1, 1.5, 2}` 的扫描结果与各发送类基线的有效发送率 | P0 | 本文新增 |
| AC-S3-19 | 基线约定 | BC-3（邻居发现与广播回退）、BC-4（按本 Agent 剩余额度节流）、BC-6（`source_sample_time_ns` 取值）各有黄金决策摘要用例，固定实例与种子下决策 JSONL 摘要稳定；BC-1 的闭式小矩阵运算跨构建逐位一致 | P0 | 本文新增 |

AWR-06 AC-D4-19 当前映射 AWR-06 的 AC-S3-1 至 AC-S3-5（即本文 AC-S3-01 至 AC-S3-05）；本文 AC-S3-06 至 AC-S3-13、AC-S3-15 至 AC-S3-19 建议并入 AC-D4-19，AC-S3-14 并入 AC-D4-50（反馈第 10 条）。

---

## 11. 工作包与排期

| 周（D3 封板后） | S 线份额 | 内容 | 前置 | 产出 |
|---|---|---|---|---|
| 第 5 周 | 0.25 | 生成器 `gen/is_fixed@1`、实例包写出（含 `hidden/comm_truth.json`）、`rng_domains[]` 声明与登记、`BiasField` 与注入；生成约束统计 | WP-D4-10（`suite.v1`、`Generator` 协议）；跨文档前置 X-1、X-3 | AC-S3-01（生成部分）、AC-S3-08、AC-S3-15 |
| 第 6 周 | 0.5 | 评测器：`U_IS`、截断诊断（含集中式口径）、跨运行 ANEES、`AoI^c`/`AoI^d`/`aoi_c_ts`、分歧、保守度；单元测试 | WP-D4-07（trace）；X-4 | AC-S3-05、AC-S3-09、AC-S3-10、AC-S3-17 |
| 第 7 周 | 1.0 | 6 个基线与 `π_random`（`common/kf` 与 M30 共建，按目标批量化）；CPU 微基准；与 WP-D4-03（实体、脚本轨迹、`planar@1`）集成；与 WP-D4-05（掩码、分区）集成 | WP-D4-03、05；X-2、X-5 | AC-S3-06 的前置、AC-S3-13、AC-S3-16、AC-S3-18、AC-S3-19 |
| 第 8 周 | 0.25 | `is-anchor` 与 `is-mech`（四个变体）；`validity.md`；AC 回归进 CI | — | AC-S3-01、AC-S3-11、AC-S3-12 |
| 第 8–10 周 | WP-D4-13 内 | 参考策略缓存（`local_kf`、`central_kf`、`random_share`） | WP-D4-13 | AC-D4-16 |
| 第 10 周 | WP-D4-17 内 | pilot：`d1/n8`、`d2/n8` 各 10 × 10；TS-1 至 TS-3；`κ_B` 扫描；场景数冻结 | WP-D4-17 | AC-S3-02、AC-S3-03、AC-S3-04、AC-S3-06、AC-S3-07；AC-D4-20 |
| 第 12–14 周（P1） | WP-D4-19、20 内 | X1 C、P、L、J、S 族（C、J 可随 X1 顺延 P2）；`Δ_perm` 的 `perm_struct` | WP-D4-19、20 | AC-RC-01 至 AC-RC-04；AC-D4-26 |
| 第 17–19 周（P1） | WP-D4-51 内 | `d3`、`n32`；`kcf`、`event_share`；`is-active` | WP-D4-51 | AC-S3-14 |

**跨文档前置**（本文不修改并行文档，以下依赖由对应文档的负责人裁定）：

| 编号 | 依赖 | 涉及文档与条目 | 需要的变更 | 不满足时的处置 |
|---|---|---|---|---|
| X-1 | `limit_expr` 引用实例公开变量 | M31 R-M31-12、V-S-10；CT V-SU-06（M28 R-M28-69 已支持） | 变量白名单扩展为"登记变量 + 实例公开简报中声明的数值变量"，三份文档一致 | 采用 §4.3 的备选写法（全部难度档 `κ_B = 1`），pilot 报告登记降级 |
| X-2 | 报告 schema 的契约位置 | CT §4.2.3、§4.3.5 | 规定 `schemas.reports[]` 所列报告 schema 的文件位置与元校验 | 暂置于 `ct:coord/report/`，CT 定稿后迁移 |
| X-3 | 清单顶层 `rng_domains[]` | CT §5.1.1（未列）；M31 R-M31-98、M28 R-M28-27（已列） | CT 顶层字段表增加 `rng_domains[]` | 以 M31 的字段定义为准实现 |
| X-4 | `sensor.capture` 进入 minimal | CT §4.4.2、M33 §6.9（已支持，以声明 `ident` 通道为条件） | 无（S3 声明 `ident`）；M31 R-M31-41 的通道词表须与 CT、M33 的 9 个通道一致 | — |
| X-5 | 基线表述与复现检验 | M30 §6.7.4 `central_kf` 行；M30 §6.9.4 的 `central_kf`、`ci_fusion` `d0` 行 | `central_kf` 采用本文 §6.1 措辞；`ci_fusion` 的 `d0` 判据由"闭式 CI 解""等协方差输入时输出等于较优输入"改为 §6.4 三项检验（M30 v0.2 的 R-M30-48、R-M30-67 与 `d1` 判据已与本文一致） | 以本文判据执行，并向 M30 登记差异 |
| X-6 | `self_state` 与 Suite 量测偏差的解耦 | M28 R-M28-59（导航噪声键 `nav:<entity_id>`） | 无；S3 依赖"导航噪声逐 tick 独立、与 Suite 域不相交"这一现行规定保持不变 | 若 M28 改为相关导航误差，S3 须复核 §2.9 并重跑 AC-S3-11 |

WP-D4-15 合计 2.0 人周（AWR-06 §10.2 逐周负荷：第 5 周 0.25、第 6 周 0.5、第 7 周 1.0、第 8 周 0.25），本表与之一致。降级：K-2 时 `gossip_dedup` 顺延（R-S3-35）；降级顺序 ③ 下 S2 顺延而 P0 收缩为 S1 + S3，S3 不在降级对象之列（AWR-06 §10.2）。

---

## 12. 有效性声明（`validity.md` 要点）

| 项 | 声明 |
|---|---|
| 模型不反映的现实因素 | 数据关联与杂波（算法隔离模式给定标识）；三维几何与高度；传感器指向与扫描；固定节点通常有测绘坐标而非 GNSS 定位 |
| 偏差建模 | `β_i` 为不经任何 Agent 可见通道暴露的量测配准偏差（坐标变换、安装与时间同步误差的合成），量级与相关时间参照 GNSS 漂移；与平台导航解噪声独立建模。现实中导航误差会同时出现在自身定位与量测中，二者耦合；S3 有意解耦，以保证相关偏差不能由导航解直接扣除（§2.9），结论不外推到"导航误差可观测"的系统 |
| 拓扑 | 环形拓扑以链路掩码施加，不由传播几何产生；`ideal@1` 对照赛道保留掩码与分区 |
| 参数来源 | 偏差相关时间取 D3 机型 GNSS SINGLE 档的 60 s（F5）；`d2` 的偏差幅度 3 m 为压力参数；网络参数集 `synthetic-stress` |
| 模型失配 | 目标在边界镜面反射，近匀速滤波存在已知失配 |
| 指标局限 | `U_IS` 只衡量精度且截断于 `e_max`；`anees_in_band` 在 5 个种子下区间较宽，对轻度过度自信的分辨力有限，且双侧口径同时惩罚保守方法；`AoI^c` 是可达性下界，任何消息交付都推进时钟，不反映载荷是否实际利用了可达信息；零通信与集中式运行不报告 AoI |
| 预算 | `d2` 的 `κ_B = 1` 为零余量紧约束，发送类基线受统一节流规则约束；每 Agent 额度为团队额度的静态均分，不研究额度的动态分配 |
| 协作增益的范围 | 协作有利是经难度设计筛选的（PA-2、PA-3）；Δ 族的量级只在本 Suite 内可比（AWR-06 §7.0） |

---

## 13. 风险与待确认事项

### 13.1 风险

| 编号 | 风险 | 触发信号 | 处置 |
|---|---|---|---|
| RK-S3-01 | 5 个种子的 ANEES 区间过宽，`anees_in_band` 在条目间差异不显著，分层失去区分力 | pilot 中 `anees_in_band` 的配对差 CI 普遍含 0 | 报告 `anees_over` 并在条目卡标注分辨力；评估把 S3 的种子数提高到 8 的算力代价（§8.5 预算） |
| RK-S3-02 | 截断饱和使 `U_IS` 只反映信息覆盖 | TS-1 至 TS-3 触发 | §5.3 的调整次序 |
| RK-S3-03 | 相关偏差的增量贡献 `S_bias` 不显著（重复计入在 `nobias` 下已很强，差中差被掩盖） | `is-mech` 上 `S_bias` 的 CI 含 0 | 报告 `S_rep`、`S_info`、`S_topo` 的份额与 CI（AC-S3-03），给出机制解释后再裁决 AC-S3-03 的表述；必要时提高 `is-mech` 的实例数 |
| RK-S3-04 | `central_kf` 精度低于 `ci_fusion`（偏差处理差异） | AC-S3-06 第一项不成立 | 机制解释：consider 处理的保守性与信息量的权衡；必要时把 `central_kf` 的偏差处理改为增广估计（升参考策略版本） |
| RK-S3-05 | 小矩阵运算的跨构建差异破坏等级 1b | PA-5 失败 | BC-1 的固定次序闭式实现；`hw_class` 内复跑 |
| RK-S3-06 | 预算节流规则成为隐含的被测对象 | 基线间字节差异主要来自节流；`κ_B = 1` 时全部发送类基线有效发送率 < 0.9 | 节流规则对全部基线相同并在基线卡登记；pilot `κ_B` 扫描（§4.3） |
| RK-S3-07 | `limit_expr` 的白名单在 M31、CT 中未扩展 | WP-D4-10 定稿时 V-S-10、V-SU-06 仍拒绝 `b_node_Bps` | §4.3 备选写法，`d0`、`d1` 的较宽预算延后（X-1） |
| RK-S3-08 | 生成约束 G-1 在某规模档重抽失败率高 | AC-S3-15 失败率 ≥ 1% | 按 PA-7 调整 `ō` 区间或部署规则；`n32` 的 `ring_mixed` 环半径规则在 P1 前复核 |
| RK-S3-09 | 平台改变 `self_state` 的导航误差模型（如改为相关过程）使其与 `β_i` 相关 | M28 R-M28-59 修订 | 复核 §2.9，重跑 AC-S3-11（X-6） |

### 13.2 待确认事项

| 编号 | 事项 | 缺省处理 |
|---|---|---|
| Q-S3-01 | `d2` 是否公开 `ρ` | 隐藏，只声明 `ρ ∈ [0, 1)` |
| Q-S3-02 | `U_IS` 是否计入速度误差 | 不计入，只评位置 |
| Q-S3-03 | `t_burn` 是否非零 | 缺省 0，pilot 可设至多 10 s |
| Q-S3-04 | `B_need` 的参考邻居数取 2 是否适用于全连通的 `d1` | 取 2，`κ_B = 2` 补偿 |
| Q-S3-05 | 闭环族 `is-active` 的关联接口与评测是否改用 OSPA | P1 立项时与 CoopTracking 统一 |
| Q-S3-06 | X1 F 族是否施加于 S3 | 关闭：以 X1 §2.3 与 Q-RC-07 为准，P1 不施加；P2 若需要，以"只对存活节点计分"的诊断变体接入，不改变宿主 `U_IS` |
| Q-S3-07 | `d2` 的 `κ_B` 初值取 1 还是 1.5 | 取 1，pilot 扫描后裁定（§4.3） |
| Q-S3-08 | 报告 schema 的契约文件位置 | 暂置 `ct:coord/report/`，待 CT 裁定（X-2） |

---

## 14. 追溯

| AWR-06 条目 | 本文位置 | 需求 | 验收 |
|---|---|---|---|
| §7.3 形式化定义（固定轨迹、外生目标、相关偏差、120 s 噪声升高、必交估计、已关联检测） | §2 | R-S3-01 至 R-S3-09、R-S3-36 | AC-S3-01、AC-S3-05、AC-S3-08、AC-S3-11 |
| §7.3 标准实例字段 | §4.4 | R-S3-16、R-S3-19 | AC-S3-11、AC-S3-15 |
| §7.3 难度与规模 | §4.2、§4.3 | R-S3-15 至 R-S3-20 | AC-S3-01、AC-S3-04、AC-S3-13、AC-S3-15、AC-S3-18 |
| §7.3 主指标与截断占比 | §5.2、§5.3 | R-S3-22、R-S3-23 | AC-S3-04、AC-S3-05、AC-S3-10 |
| §7.3 跨运行 ANEES | §5.4 | R-S3-24 | AC-S3-02、AC-S3-07 |
| §7.3 AoI 矩阵 | §5.5 | R-S3-25 | AC-S3-09 |
| §7.3 认知分歧；§9.1 方向 C | §5.6 | R-S3-26 | AC-S3-12、AC-S3-17 |
| §7.3 经典基线 | §6 | R-S3-30 至 R-S3-35 | AC-S3-06、AC-S3-13、AC-S3-14、AC-S3-19 |
| §7.3 所需平台能力 | §8.5、§11 跨文档前置 | — | — |
| §6.5 第 7 项（链路掩码与分区） | §2.7 | R-S3-06 | AC-S3-16 |
| §7.0 PA-1 至 PA-7 | §7.4 | — | AC-S3-01、AC-S3-04 |
| §7.4 X1 对 S3 | §3.5（以 X1 §2.3、§5 为准） | R-S3-14 | AC-RC-01 |
| §8.5 Δ 族 | §5.8 | R-S3-29 | AC-D4-16、AC-D4-26 |
| §8.6 统计协议；失败计分与无效运行 | §7.3、§5.9 | R-S3-28 | AC-D4-14、AC-S3-05 |
| §9.1 方向 B | §5.5、§5.7、§6.1 `event_share` | R-S3-25 | AC-S3-09 |
| §10.2 WP-D4-15、17 | §11 | — | AC-D4-19、AC-D4-20 |
| ADR-D4-07、14、16、17、21、22、23、24 | §3.2、§2.8、§1.1、§2.1、§5.1、§5.8、§7.3、§5.9 | — | — |

---

## 15. 对 AWR-06 的反馈

| # | 位置 | 问题 | 建议 | 理由 |
|---|---|---|---|---|
| 1 | §7.6 算法族覆盖；§7.3 经典基线 | §7.6 称 S3 在 P0 覆盖"共识、CI、gossip、分布式 Kalman"，而 §7.3 基线表无分布式 Kalman 条目 | 在 §7.3 注明 `avg_consensus` 为信息形式共识（consensus on information），即共识型分布式 Kalman 的 P0 形态；Kalman 共识滤波 `kcf` 列为 P1 增补；或把 §7.6 的表述改为"分布式 Kalman（P1）" | 覆盖声明须与基线表一致，否则论文与榜单的算法族覆盖无法核对 |
| 2 | §7.3 信息新鲜度 | AoI 以 `source_sample_time_ns` 得到 τ，但内核只核验声明值不晚于发送方已有的最晚样本，无法把多跳转发的信息归属到最初来源 v，也不能防止以新时刻声明旧内容 | 主口径改为由交付记录与采集记录以向量时钟计算的因果 AoI 下界 `AoI^c`；原定义保留为一跳声明 AoI `AoI^d`，并以向量时钟离线复核声明值（本文 §5.5） | `AoI^c` 不依赖 Agent 声明、对全部策略可比、可由 trace 离线重算；方向 B 的 AoI 分布对照需要可比口径 |
| 3 | §7.3 校准性；§4.2 `MetricSpec` | 校准性标为 `direction = target`，但进入分层的是区间内比例，属 `max`；`MetricSpec` 无目标区间与层级字段 | 区分"`ANEES_t` 以区间为优"与"`anees_in_band` 越大越好"：后者登记为 `direction = max`、`level = scenario`；`direction = target` 的指标须带 `target` 区间（与 M32 反馈第 5 条一致） | 使方向化支配与分层可执行 |
| 4 | §7.3 ANEES；§7.0 统计单位 | 官方缺省每场景 5 个种子时 χ² 区间为 `[0.649, 4.097]`，对轻度过度自信分辨力有限；AC-S3-2 未指明以何种种子数检验 | AC-S3-2、AC-S3-3 在 pilot（10 种子，区间 `[0.959, 3.417]`）上检验；正式成绩同时报告 `anees_over`；在 §8.5 预算中评估 S3 提高到 8 个种子的代价 | 区分力取决于种子数，判据须与样本设计绑定 |
| 5 | §7.3 主指标 | 只规定"未提交有效估计取 1"，未规定缺失对一致性诊断的处理 | 任一种子在某单元缺失或无效即记 `miss`，计入分母不计入区间内 | 否则策略可在困难时刻不提交以改善一致性 |
| 6 | §7.3 AC-S3-1 | "误差与解析值偏差 < 1%"若以经验均方误差检验，有限种子下的蒙特卡洛误差远大于 1%；且 CI 按设计保守，其协方差不等于集中式闭式后验，CI 估计也不是加权最小二乘解，把 CI 融合与集中式滤波放在同一解析判据下自相矛盾；CI 的 ω 无闭式解 | 集中式与本地滤波改为确定性检验：协方差与闭式后验相对偏差 < 1%、估计值与 trace 量测的加权最小二乘解之差 < 0.01 σ、评测器 `U_IS` 与手算逐位一致；CI 融合改为三项检验：保守包络 `P_CI ⪰ P_central`、ω 与高精度数值最优解所得 `tr P` 的相对差 < 1%、各向同性不等协方差输入退化为较优输入（本文 §6.4、§9.1、AC-S3-01） | 使 AC-S3-1 可在 CI 中稳定执行，且判据与 CI 的保守性质一致 |
| 7 | §7.1–§7.3 难度表；§7.3 实例字段 | 难度档写入网络档（`ideal@1`、`abstract-disk@1`、丢包 5%、`abstract-radio@1 default`），实例字段含 `network_profile`，与"网络档为赛道坐标"冲突 | 难度档只定义实例内在旋钮与外生通信特征；网络档改写为"锚点运行"；移除实例字段 `network_profile`（与 M31 反馈第 7 条一致） | 同一实例可在多网络档下配对运行，五维正交 |
| 8 | §7.3 `d2` 环形拓扑 | `abstract-radio@1 default` 视距下约 1.5 km 内连通，400 m 区域内环形拓扑不能由几何产生；未说明生成机制，也未说明 `ideal@1` 对照赛道是否保留拓扑约束 | 明确以外生链路掩码施加环形拓扑，并约束环边全时域视距；说明 `ideal@1` 亦受掩码与分区约束，"通信约束代价"不含拓扑代价 | 避免"环形拓扑"在不同网络档下语义不一 |
| 9 | §7.4 扰动族适用表 | S3 的适用族与 X1 协议 §2.3 不一致：AWR-06 列 P、L、S；X1 增补 C、J，F、M、H 对 S3 不适用；S 族的受扰范围、区段与分档由 X1 §5.9 给出 | §7.4 引用 X1 §2.3 的适用矩阵与 §5 的参数，不再在 AWR-06 中重复列出；F 对 S3 不适用的理由（失效节点按 `e = 1` 计分，度量的是计分规则）写入 §7.4 | 适用族与参数只在一处定义，避免宿主规格与 X1 再次分歧（v0.1 本条建议"F 族 P1 起适用 S3"，v0.2 撤回，以 X1 Q-RC-07 为准） |
| 10 | §7.3 验收；§10.6 AC-D4-19 | S3 缺少与 S1、S2 基线序关系验收（S1 规格 AC-S1-02、S2 规格 AC-S2-02）对应的条目，也缺少 ANEES 评测器的正确性锚点、AoI、泄露、离线重算、生成约束、网络掩码、预算对账与基线约定的验收 | 增加本文 AC-S3-06 至 AC-S3-13、AC-S3-15 至 AC-S3-19（§10）并入 AC-D4-19；AC-S3-14 并入 AC-D4-50；AWR-06 的 AC-S3-1 至 AC-S3-5 与本文 AC-S3-01 至 AC-S3-05 同号对应，判据以本文为准 | 三个 Suite 的验收口径一致 |
| 11 | §7.3 AC-S3-3 | "关闭相关偏差时差异显著缩小"把两种失真来源（相关偏差、循环转发中的重复计入）合在一处，后者在无偏差时仍存在，判据可能不成立 | 以 `base`、`nobias`（只置零注入偏差、简报不变）、`nobias_brief`（简报也置零）、`noring` 四个确定性变体做分解，分别量化相关偏差、重复计入、信息与拓扑因素（本文 §3.4、AC-S3-03）；`noring` 不消除重复计入，不能单独代表"循环转发" | 机制检验应能归因 |
| 12 | §5.5 随机域表 | 域表没有承载"非某一传感器所有"的共同偏差过程的位置；M28 R-M28-27 已提供 Suite 级域机制，AWR-06 未提及 | §5.5 注明 Suite 可在清单 `rng_domains[]` 声明 `suite.<suite_id>.` 前缀的运行期域（M28 R-M28-27、M31 R-M31-98），S3 的偏差过程使用 `suite.infoshare.bias` | v0.1 本条建议借用 `sensor` 域或新增 `common_noise` 域，v0.2 撤回，改为采用既有机制，避免 `object_id` 格式与 `sensor_id` 正则互相影响 |
| 13 | §4.4 Suite 清单 `limit_expr` | `limit_expr` 只能引用 `n_agents`、`duration_s` 等，无法表达随目标数、难度档与预算比变化的通信预算；M28 R-M28-69 已允许实例公开数值变量，而 M31 R-M31-12、V-S-10 与 CT V-SU-06 仍不允许，三份文档不一致 | AWR-06 §4.4 明确 `limit_expr` 的变量范围为"登记变量 + 实例公开简报中声明的数值变量"，并要求 M31、CT 同步；S3 在变更落地前采用只含登记变量的备选写法（本文 §4.3） | S3 以 `κ_B` 定义预算，使规模扩档不改变通信紧度；平台只有一处求值器，变量白名单须一致 |
| 14 | §7.3 形式化定义；§4.1 对象模型 | 提到"脚本运动"，但对象模型与 Assignment 语义未定义不受 Agent 控制的脚本轨迹实体 | 在内核实体模型中定义外生脚本轨迹（实例航线、恒速、作为外生量参与 CRN），Agent 以 `ControlScope.NONE` 持有 | 算法隔离模式的运动固定须有内核语义支撑 |
| 15 | §7.3 主指标"pilot 调整 `e_max`" | `e_max` 是评分参数，按档调整会使 `U_IS` 在难度档之间含义不一；PA-7 的可调参数清单不含 `e_max` 与 S3 的旋钮 | 先调生成器参数、后调 `e_max`；`e_max` 在 Suite 主版本内对全部难度档相同、只在 `dev` pilot 中调整一次；PA-7 清单补入 S3 的 `ov`、`κ_B`、分区时长、`ρ` 区间 | 评分口径稳定 |
| 16 | §4.5 trace `kind` 枚举 | AWR-06 §4.5 的 `kind` 枚举无传感器采集记录，AoI 与信息可达性归因无法从 trace 计算；CT §4.4.2 已登记 `sensor.capture`（Suite 声明 `ident` 通道时为 minimal） | AWR-06 §4.5 的枚举与 CT §4.4.2 对齐，增加 `sensor.capture` 及其进入 minimal 的条件 | R-4 离线重算要求；S3 已按 CT 声明 `ident` 通道 |
| 17 | §7.3 必交估计 | 未规定报告的有效时刻（密封时刻还是生效时刻）、迟交与重复的处理，也未说明 `ReportRequest.evidence_ids` 对 S3 是否适用；报告与消息共用 schema 名 `infoshare.estimate.v1`，而二者字段不同（报告须覆盖全部目标、只用 binary64、以 `valid_at_ns` 代替逐目标时间） | 有效时刻取 `sealed_at_ns = t_k`；只认该决策的报告；`evidence_ids` 为空；报告 schema 独立命名为 `infoshare.estimate_report.v1`，在 Suite 清单 `schemas.reports[]` 登记（CT §4.3.5） | 评分规则须无歧义；同名 schema 下两套字段规则会使校验与审计无法按名分派 |
| 18 | §5.2 调度规则命名 | 调度规则"R-S1"至"R-S8"与 Suite 文档的需求编号前缀（如 `R-S3-yy`）同形 | 调度规则改称 `SR-1` 至 `SR-8`，或 Suite 需求改用 `R-IS-yy` 等问题族前缀 | 避免追溯时混淆 |
| 19 | §7.3 实例字段 `target_aggregate` | 来自 r02 §3.5 标量共识测试的字段，在 S3 的目标估计问题中无定义 | 从 S3 实例字段中移除 | 字段须有消费者 |
| 20 | §7.3 规模；§7.0 预算档 | `d2` 的"1–4 KiB/s/节点"为固定值，而目标数 `M = 0.75n` 使消息长度随规模增长，固定速率会使大规模档的通信难度隐性上升 | 以预算比 `κ_B = B_cap / B_need` 定义预算档，KiB/s 只作 `n8` 参考值 | 恒密度缩放应同时保持通信紧度 |
| 21 | §8.6 失败计分与无效运行 | "失败计分"行规定资源按实际消耗，而"无效运行"行规定 `invalid_policy = zero` 时全部分量记 0；对越小越好的 `b_tx_ratio`、`cpu_ratio`，记 0 等于满分，账本违规的运行会在资源轴上被评为最优，进入支配分层后获得不应有的非支配位置 | "无效运行"行改为与"失败计分"同一方向化规则：主指标与结果类指标置不利端，资源指标按实际消耗（与 M32 R-M32-38 及 M32 反馈第 2 条一致） | 两行处理方向相反会奖励违规；方向化规则已在 M32 落实，AWR-06 宜同步 |
| 22 | §7.3"区分度来自……来源管理方法应在 ANEES 上受益" | CI 等来源管理方法按设计保守，独立噪声下 ANEES 系统性低于区间下界，双侧区间内比例 `anees_in_band` 可能低于朴素融合；"在 ANEES 上受益"若理解为区间内比例更高，与方法性质不符 | 改为"来源管理方法应在过度自信率上受益"；分层保留双侧 `anees_in_band`（只惩罚过度自信的口径可被放大协方差的平凡策略利用），条目卡并报 `anees_over`、`anees_under`、`conserv_ratio`（本文 §6.3） | 预期与指标口径一致，避免 pilot 结果被误读为评测器错误 |
| 23 | §7.3 形式化定义"`β` 为公共 GNSS 漂移" | 若把 `β` 解释为导航误差，而 Agent 又能见到含该误差的导航解与公开的真值部署，`β_i = ŝ_i − s_i` 可直接扣除，相关偏差、CI 所需的"互相关未知"前提与 `d2` 的区分度同时失效 | §7.3 注明：`β` 为不经任何 Agent 可见通道暴露的量测配准偏差（量级参照 GNSS 漂移），平台导航解噪声与之独立；在"所需平台能力"中列出"Suite 量测偏差与导航解解耦" | 问题定义的核心机制依赖偏差不可直接观测，须在概念设计层面写明（本文 §2.9） |

---

## 评审处理记录

v0.2 对第一轮评审意见（21 条）逐条核实后处理。"核实"列给出核实所依据的文件与位置；"属实"为是、部分或否；只对属实部分修改。评审期间 M30 已修订为 v0.2（R-M30-48、R-M30-67、§6.5.3、§6.9.4 有变化），本表按核实时的版本记录。

| 序 | 评审位置 | 意见要点 | 核实 | 属实 | 处理 | 修改位置 |
|---|---|---|---|---|---|---|
| 1 | §2.3、§2.5、§2.9 偏差可观测性 | `self_state = s_i + β_i` 且公开部署，`β_i = ŝ_i − s_i` 可直接扣除，相关偏差、CI 前提与 `d2` 区分度失效 | v0.1 §2.3 自身状态行、§2.9 部署公开行、§8.3 `nodes[].pos_m`；另核实 M28 R-M28-59：平台导航解噪声为逐 tick 独立白噪声（`nav:<entity_id>` 键），与 Suite 偏差无关 | 是 | 采用"公开部署、偏差不经导航解"：`β_i` 定义为量测配准偏差，`self_state` 按平台规则且与 `β_i` 独立；论证隐藏部署方案对固定节点仍泄露偏差增量；新增 R-S3-36；AC-S3-11 增键空间不相交、独立性统计检验与 `nav_correct` 变异策略；`validity.md` 声明解耦；共同分量下限改述为"近似下限"并说明前提 | §0 第 2 条；§1.4 F4、表 B1；§2.3；§2.4；§2.5；§2.9；R-S3-03、R-S3-36；§3.2；AC-S3-11；§12；RK-S3-09；反馈第 23 条 |
| 2 | §4.1–§4.3 G-1 `ov` 随规模缩放 | 恒密度下 `ov ∝ 1/n`，G-1 只在 `n8` 附近可满足 | 按 §4.3 区域边长与 `r_s = 120 m` 核算 `π r_s²/L²`：`n4` 0.565、`n8` 0.283、`n16` 0.141；另以仓库 `.venv` numpy 做 300 次部署的蒙特卡洛，含边界效应时 `n8` `fixed` 部署 `ov` 仅 0.24 | 是 | 约束对象改为 `ō = n · ov`（初值 `[1.5, 3.0]`，各规模档相同），`ov` 只作报告；给出两种部署在 `n4`–`n32` 的 `ō`、`ov`、`cov_any` 粗算表；`cov_any` 初值改为 ≥ 0.85；PA-7 调参对象改为 `ō` 与环半径系数；新增 AC-S3-15 按规模档统计约束满足率与重抽率 | §0 第 6 条；§1.5；§4.2；§4.3；§4.5 G-1；§7.4 PA-7；§9.2；AC-S3-15；RK-S3-08 |
| 3 | §10 AC-S3-1、§9.1、§6.4 CI 解析锚点 | CI 与集中式共用闭式判据自相矛盾；"闭式 CI 解"未定义；"等协方差输入输出较优输入"自相矛盾 | v0.1 AC-S3-1 同时点名 `central_kf` 与 `ci_fusion`；§9.1 末段承认 CI 趋于节点 1 本地协方差；§6.1 ω 为 30 次黄金分割，无闭式 | 是 | 闭式判据只对 `central_kf`、`local_kf`；`ci_fusion` 改为三项检验：`P_CI ⪰ P_central`（加随机联合协方差下的 CI 一致性单元测试）、ω 与高精度数值最优解所得 `tr P` 相对差 < 1%、各向同性不等协方差输入退化为较优输入且相同输入幂等；删除"闭式 CI 解"；修订反馈第 6 条 | §6.4；§9.1；AC-S3-01；反馈第 6 条；X-5 |
| 4 | §5.3(a)、§5.5、§7.1 集中式赛道 | 集中式无交付事件，`avail` 退化为自身检测，TS-2 必然触发；`AoI^c` 退化；报告归属与观测汇聚计费未规定 | v0.1 §5.3、§5.5 向量时钟只随交付推进；§6.1 `central_kf` 无消息；M30 R-M30-15、R-M30-16：`CentralPolicy` 以全部所控 Agent 的观测为输入、按 Agent 键返回决策，`global` 视图为全队观测并集 | 是 | 定义集中式口径 `avail_cr`（任一节点在窗口内有检测即可达），TS-2 注明只在该口径下使用；§7.1 增集中式赛道规则：观测汇聚不经网络、不计费，按 Agent 键各提交一份报告，缺键按缺失计分，AoI 与分歧置 null | §5.3；§5.5 口径规则表；§7.1；表 B10；AC-S3-05、AC-S3-10 |
| 5 | §3.5、§1.2、Q-S3-06 与 X1 一致性 | 适用族与 S 族参数与 X1 冲突（F 不适用；应含 C、J；S 为前 `⌈n/2⌉` 个实体、区段 0.3T、分 5 档） | X1 §2.3 适用矩阵与不适用理由段、§5.3、§5.5–§5.9、§12.6 第 18 条、Q-RC-07 | 是 | §3.5 以 X1 为准重写：适用 C、P、L、J、S，F、M、H 不适用；摘引 X1 参数；补写 X1 要求的"S3 解释"（`P_d` 的含义、`σ` 作用于真实噪声、与 §2.6 退化的乘法叠加）；说明各族对 `avail` 与 `AoI^c` 的影响；关闭 Q-S3-06；反馈第 9 条撤回原建议并改述 | §1.1；§1.2；§3.5；R-S3-14；§7.2 `is-stress`；§11；Q-S3-06；表 B11；反馈第 9 条 |
| 6 | §5.9 失败计分 | 崩溃与失活"全部分量记 0"使 min 方向资源指标取满分；失败计分与无效运行被合并 | AWR-06 §8.6"失败计分"行（资源按实际消耗）与"无效运行"行（全部分量记 0）；M32 R-M32-36、R-M32-38、R-M32-89 已采用方向化规则，消耗类指标保留实际消耗 | 是 | §5.9 分两类：崩溃、失活、非法动作按 M32 R-M32-36（`U_IS = 0`、结果类置不利端、资源按实际消耗）；账本违规为 `ranked_valid = false`，按 M32 R-M32-38 同一方向化规则。未采纳意见中"无效运行全部分量记 0"的写法，理由是它同样奖励 min 方向资源指标（意见自身指出的漏洞）；可空指标声明 `nullable`；新增反馈第 21 条 | §5.1；§5.9；R-S3-28；AC-S3-05；表 B9；反馈第 21 条 |
| 7 | §4.3、§5.7、§6.2 BC-4、§8.1 团队级通信额度 | `per: team` 额度对 Agent 的可见形式未定义（旁路或无归属）；先发节点耗尽团队池；`κ_B = 1` 零余量；`b_tx_ratio` 分子与硬额度口径不同 | M28 R-M28-70（`Observation.ledger` 只含确定性计量维剩余量，未规定 team 维）；M29 R-M29-14（受理字节扣减、不退还） | 是 | `tx_bytes` 改为 `per: agent`、`limit = duration_s · b_node_Bps`，`Observation.ledger` 只暴露本 Agent 剩余量；BC-4 改按本 Agent 额度节流；`b_tx_ratio` 改为受理字节 / `B_cap`，`B_tx` 写诊断；说明 `κ_B = 1` 的零余量含义，pilot 扫描 `κ_B ∈ {1, 1.5, 2}` 并以有效发送率判定；新增 AC-S3-18、Q-S3-07 | §4.1；§4.3 预算设计项表；§5.1；§5.7；§6.2 BC-4；§8.1；表 B4；AC-S3-18；RK-S3-06；Q-S3-07 |
| 8 | §8.1、§4.3 `limit_expr` 引用 `b_node_Bps` | M28、M31 只允许登记变量，清单不能通过校验 | M28 R-M28-69（v0.2）已允许实例公开数值变量并点名 S3 的 `b_node_Bps`；M31 R-M31-12、V-S-10 与 CT V-SU-06 仍只允许登记变量 | 部分 | M28 部分不属实；M31 与 CT 部分属实。清单旁标注"待 M31、CT 变更"；§4.3 给出只用 `n_agents`、`duration_s` 的备选写法（P0 全部难度档 `κ_B = 1`，降级登记）；登记为跨文档前置 X-1 与风险 RK-S3-07；修订反馈第 13 条，指出三份文档不一致 | §4.3；§8.1；§8.5；§11 X-1；表 B3；RK-S3-07；反馈第 13 条 |
| 9 | §2.2、§4.1、§4.2、§8.3 先验与初速 | 真值初速来自零均值先验，`speed_mps` 无来源；`speed_ratio` 定义与生成方式不一致 | v0.1 §2.2 先验抽取、§4.1 `speed_mps`、§4.2 `speed_ratio`、§8.3 `prior_mean` 速度为 0 | 是 | 采用方式 A：`v̄_m` 的大小取自 `speed_mps`、方向由 `instance` 域抽取，属实例内容并经简报公开，真值 `v = v̄ + σ_v0 · n`；说明跨种子仍服从公开先验，χ² 区间成立；`speed_ratio` 以 `|v̄_m|` 重新定义；删除 `hidden/targets.json` | §1.5；§2.2；§3.2；§4.1；§4.2；§4.4；§4.5；§8.3；R-S3-01 |
| 10 | §3.4、§10 AC-S3-3 `nobias` 混杂 | `nobias` 同时改变外生偏差与简报信息；`noring` 无判据 | v0.1 §3.4、BC-2（consider 参数取简报 `σ_β`）、§8.3 `bias_model.sigma_m`、AC-S3-3 | 是 | `nobias` 改为"注入置零、简报不变"；新增 `nobias_brief` 量化信息因素；AC-S3-03 给出 `S_bias`、`S_rep`、`S_info`、`S_topo` 分解与份额 CI，并给出 `noring` 的判据；另说明 `noring` 不消除重复计入；修订反馈第 11 条与 RK-S3-03 | §3.4；R-S3-13；§7.2；AC-S3-03；RK-S3-03；反馈第 11 条 |
| 11 | §6.3、§6.4、§5.4 CI 保守性与 `anees_in_band` | 保守方法在双侧区间内比例上被惩罚，与预期叙述及 M30 R-M30-48 冲突；无 `under` 预期 | v0.1 §6.1、§6.3、§5.1；M30 v0.2 R-M30-48 已改为"CI 融合在独立噪声下满足保守性"，§6.9.4 `d1` 判据为 `anees_over ≤ 0.05`、`conserv_ratio ≥ 1` | 是 | §6.3 增各基线 in/over/under 预期占位表；AC-S3-07 增 `ci_fusion` 的 `anees_over` 与 `anees_under` 判据；分层保留双侧口径并说明理由（只惩罚过度自信可被放大协方差的平凡策略利用）；新增反馈第 22 条；与 M30 v0.2 对齐 | §6.1；§6.3；§6.4；AC-S3-07；§12；反馈第 22 条 |
| 12 | §4.4、§9.2、R-S3-19、AC-S3-11 隐藏字段落位 | `comm.json` 内放隐藏字段，与 R-S3-19 及 M31 视图规则不自洽 | M31 v0.2 OR-3、§7.4.1（不公告的分区与链路掩码在 `hidden/comm_truth.json`）、R-M31-26、V-I-09、§13 第 19 条 | 是 | 按 M31 现行布局，链路掩码与分区移入 `hidden/comm_truth.json`（`truth` 视图），未采纳意见中"标为 `scheduled`"的建议，因 M31 已裁定按文件划分且该类字段属 `truth`；R-S3-19 改写为 `truth` 视图量不得出现在 `brief`、`scheduled` 文件与任何 Agent 可见通道；AC-S3-11 增对 `hidden/` 文件逐字段值的泄露审计 | §4.4；§9.2；R-S3-08、R-S3-19；AC-S3-11；表 B5 |
| 13 | §5.2 `k_0` 定义 | `⌈t_burn/Δe⌉` 在 `t_burn = 0` 时为 0，与"缺省 1"不符 | v0.1 §5.2 公式与 §1.5 `k = 1..K` | 是 | 改为 `k_0 = max(1, ⌊t_burn/Δe⌋ + 1)`，规定 `t_k ≤ t_burn` 的单元不计入；ANEES、AoI、截断诊断与分歧使用同一 `k_0` | §1.5；§5.2 |
| 14 | §10 AC-S3-8 偏差统计检验功效 | 3,600 s 单序列的有效独立样本约 30，功效不足 | v0.1 AC-S3-8；按 `T/(2τ)` 粗估有效样本数 30 与样本方差相对误差约 26%，量级属实 | 是 | 改为 ≥ 2,000 个独立种子、相隔 ≥ 3τ_β 取样的集合检验，并以 10% 幅度缩放与 `φ` 误用两种变异验证检验能拒绝；增加 `BiasField` 与 `gm.py` `phi`、`step` 逐位一致的确定性单元测试（实现改为直接调用 `gm.py`） | §1.4 F1；§3.3；AC-S3-08 |
| 15 | §10 验收与需求追溯 | 多条需求无可检验的验收或映射不覆盖 | 逐条核对 v0.1 R-S3-06、16、17、18、21、26、31 的验收列与 AC-S3-1 至 AC-S3-14 的判据 | 是 | 新增 AC-S3-15（生成约束、难度档与网络档解耦、`MetricSpec` 登记）、AC-S3-16（掩码 × 分区 × `ideal@1` 回放）、AC-S3-17（`W₂` 闭式与 `scipy.linalg.sqrtm` 对拍）、AC-S3-18（预算对账）、AC-S3-19（BC-3、BC-4、BC-6 黄金用例）；相应需求改映射 | §2.10；§4.6；§5.10；§6.5；§10；§14 |
| 16 | §8.2 报告 schema 与 M30 §6.5.3 | 报告与消息同名 schema 而字段不同；`central_kf` 描述两处不一致 | M30 v0.2 §6.5.3（消息 `est` 为 `f4[·, 14]`，另有 `tg_ix`、`t_ms`、`seq`）、§6.7.4 `central_kf` 行；CT §4.3.5（`ReportRequest.report` 为映射，`report_schema` 在 `schemas.reports[]` 登记） | 是 | 报告独立登记为 `infoshare.estimate_report.v1`（映射、binary64），契约位置待 CT 裁定（X-2、Q-S3-08）；`central_kf` 措辞在 §6.1 统一为"consider 参数、节点间相关按 0"，说明与 M30 同义并列为跨文档依赖 X-5；修订反馈第 17 条 | §2.8；R-S3-07；§6.1；§8.1 `schemas`；§8.2；§8.3；§9.4；§11 X-2、X-5；反馈第 17 条 |
| 17 | §10 AC-S3-13、§6.1、BC-1 CPU 预算 | 逐对逐目标 30 次黄金分割的纯 Python 实现可能超出 p95 ≤ 10 ms | 量级为评审粗估，文中无估算；M30 v0.2 R-M30-67 已改为以每 Agent 每仿真秒累计 CPU 为主判据，`ci_fusion` 要求按批量矩阵向量化 | 部分 | 风险属实，具体数值未经测量。BC-1 与 §6.1 规定按（目标, 来源）对批量化与 2 × 2 分块闭式；AC-S3-13 改为第 7 周微基准加 M30 v0.2 同口径的主判据（每仿真秒累计 CPU p95 ≤ 50 ms、最大 ≤ 100 ms），单步 p95 ≤ 10 ms 降为设计目标 | §6.1；§6.2 BC-1；AC-S3-13；§11 |
| 18 | §5.5 `AoI^c` 口径限制 | 任意消息推进时钟可被群发小消息压低；与 `avail` 的基准不一致；零通信策略恒为 t | v0.1 §5.5、§12 指标局限、§5.3 `avail` 定义 | 部分 | 可被压低与零通信问题属实；与 `avail` 口径不一致不属实（向量时钟单调因果，`V ≥ s` 即 s 之前全部采样可达，`avail` 取其中检测采样，二者一致），补写说明。条目卡标注"可达性下界"并与 `AoI^d`、`aoi_c_ts`、分类字节并报；新增按 `info_class` 推进的诊断量 `aoi_c_ts`；零通信与集中式运行置 null | §5.1；§5.3；§5.5；AC-S3-09；§12 |
| 19 | §3.2、反馈第 12 条随机域登记 | 应采用 M28 R-M28-27 的 Suite 级域 | M28 R-M28-27、M31 R-M31-98、V-S-21 | 是 | 偏差改用 `suite.infoshare.bias` 域并在清单 `rng_domains[]` 声明；生成器子域按 `instance/is-fixed/<sub_domain>` 登记；检出与噪声布局改为沿用 M28 R-M28-59（核实中发现 v0.1 的 `draw_index` 自定义布局与之冲突，一并修正）；反馈第 12 条撤回原建议并改述；CT 清单顶层字段缺 `rng_domains[]` 登记为 X-3 | §3.2；§3.3；§8.1；§11 X-3；表 B1、B2；反馈第 12 条 |
| 20 | §9.2、§4.5、§8.3 示例数值 | 奇偶编号相反；`b_node_Bps = 2048` 更接近 `n16`；`speed_ratio = 1.3` 与定义不符 | v0.1 §4.5 部署规则与 §9.2 `agents.json`；按 §4.3 公式与 M30 v0.2 §6.5.4 的编码估算 `n8` 约 0.8–1.5 kB/s；按定义 `4 × 0.5 / 3.5 ≈ 0.57` | 是 | 编号从 0 起、偶数号 `fixed_sensor`；`b_node_Bps` 改为落在估算区间内的占位值 1280 并注明以实测为准；`speed_ratio` 改为 0.57、`ov` 与 `o_bar` 取粗算表量级；示例明确标注不作验收样例 | §4.2；§4.3；§4.5；§8.3；§9.2 |
| 21 | 编号体例与追溯引用 | 验收应为两位序号；AC-S1-2、AC-S2-2、AC-RC-1 为旧号；"同号同义"已不成立；CT 路径无法核对 | S1 规格 AC-S1-02、S2 规格 AC-S2-02、X1 AC-RC-01 均为两位；CT（`docs/d4/契约草案.md`）在核实时已存在，意见所称"尚未出现"已过时 | 部分 | 编号改为 AC-S3-01 起，引用 AWR-06 时保留其原号；"同号同义"改为"同号对应，判据在本文细化"；AC-S1-02、AC-S2-02、AC-RC-01 改用两位号。CT 部分不属实，改为补做对照：消息 schema 路径改为单级目录、`ReportRequest.report` 改为映射、声明 `ident` 通道使 `sensor.capture` 进入 minimal、赛道 `role` 与 `Family.evaluator` 按 CT 书写（表 B6–B8） | 文档信息"体例"；§0 第 7 条；§1.4 表 B；§8.1；§8.2；§10；§14；全文 AC 编号 |
