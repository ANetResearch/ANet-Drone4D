# S1 TaskAlloc 规格（Dynamic Multi-Robot Task Allocation Suite Specification）

## 文档信息

| 项 | 内容 |
|---|---|
| 文档编号 | AWR-D4-S1 |
| 标题 | S1 TaskAlloc（动态多机器人任务分配，含联盟任务高阶层 TA-C）Suite 规格：形式化定义、实例字段与生成器、难度与规模分级、运行模式与通信档组合、指标与公式、基线清单与预期排序、验收、实例样例 |
| 版本 | v0.2.1（2026-10-18 文档集集成同步：按 AWR-06 v0.3 §0.5 统一裁定 XR-09 修订 §2.4 承诺仲裁细则的状态名与对应列；按 XR-14 把预算违规的"计 0"表述改为方向化计分（§6.4、R-S1-24、AC-S1-12）；按 XR-23 更正 §7.7 评测通道的出处说明；按 XR-12 把实验配置样例中的进程内模块引用改为 `module:`） |
| 日期 | 2026-10-18（UTC） |
| 修订记录 | v0.1（2026-10-18）：首版。v0.2（2026-10-18）：按评审意见 18 条逐条核实后修订，处理记录见 §16。主要变化：时间窗以相对偏移存储并在运行期统一截断到 H，单任务与前置可行性改为与到达实现无关的充分条件（G-06、G-07）；故障对象约束（G-08）；时效折扣终点改为服务开始时刻；`d0` 参考模型的 `t_tr` 精确定义并重算 `J*`；闭环基线统一联盟汇合约定 `rv-slot@1`；参与者定义拆为资格与归属两步；`realloc_latency_s`、`reassign`、`invalid_claim_rate` 定义修订；实例包参考文件移入 `hidden/`；X1 接入声明（§6.6）；TA-C 机巢、能量与成员变化规则；对 AWR-06 的反馈增至 24 条，另设对并行文档的提请（§15.2） |
| 状态 | D4 设计评审稿。D4 处于深入调研与概念设计阶段，D3 完整收尾并封板（G0）后才启动实现；本文全部阶段与周次以"D3 封板后第 N 周"表述，不含日历日期承诺；本文不构成实现承诺 |
| 上游文档 | [AWR-06](../../06-D4-协同实验平台概念设计.md)（v0.2）：§1.2、§4.1、§4.2、§4.4、§4.6、§5.1、§5.3、§5.5、§5.6、§5.10、§5.11、§6、§7.0、§7.1、§7.4、§7.5、§7.6、§8.3–§8.7、§9.1、§9.2、§10、ADR-D4-15、16、17、20、22、23、24、27、29；调研 [r02](../../research/d4/r02-协同问题族与标准问题库.md) §2、§3.1、§3.2、§6、§7（TA 与 CF 形式化、实例字段、指标、基线、CAPL 组织）、[r03](../../research/d4/r03-NS-2经验与排行榜规则.md)（R15 有效性声明、R31 基线治理）、[n01](../../research/d4/notes/n01-现有代码映射.md)、[n02](../../research/d4/notes/n02-D3评测子系统泛化.md)、[n03](../../research/d4/notes/n03-外部模型讨论纪要.md) §2.9、§3.4（S1 标准实例初值）；负责人前期稿 design-r2 §五（TaskAlloc、DynamicCoalition） |
| 并行文档 | [M28 实验内核](../../modules/M28-实验内核PRD.md)、[M29 交互网络](../../modules/M29-交互网络PRD.md)、[M30 协同接口与基线](../../modules/M30-协同接口与基线PRD.md)、[M31 基准套件](../../modules/M31-基准套件PRD.md)、[M32 评测与统计](../../modules/M32-评测与统计PRD.md)、[M33 Trace 与复现](../../modules/M33-Trace与复现PRD.md)、[M34 研究者 SDK 与命令行](../../modules/M34-研究者SDK与命令行PRD.md)、[S2](S2-CoopSearch-Handoff规格.md)、[S3](S3-InfoShare规格.md)、[X1](X1-RobustCoord协议.md)、[协作智能排行榜规格](../协作智能排行榜规格.md)、[契约草案](../契约草案.md)、[r04 首篇论文实验设计](../../research/d4/r04-首篇论文实验设计.md)。上述文档并行撰写，本文对其引用以章节主题表述，编号以各文档定稿为准 |
| 编号约定 | Suite 需求 `R-S1-yy`；Suite 验收 `AC-S1-yy`（两位序号；AC-S1-01 至 AC-S1-06 与 AWR-06 §7.1 的 AC-S1-1 至 AC-S1-6 一一对应并细化）；Suite 内规则 `P-yy`（参与与完成判定）、`G-yy`（生成器）、`X-yy`（执行器）；风险 `RK-S1-yy`；待决 `Q-S1-yy`。引用 AWR-06 的需求、决策、工作包、验收分别写作 `R-D4-xx`、`ADR-D4-xx`、`WP-D4-xx`、`AC-D4-xx`；模块需求写作 `R-Mxx-yy` |
| 引用约定 | 沿用 AWR-06：`py:` = `python/awr/`；`sdk:` = `tools/sdk/drone4d/src/drone4d/`；`ct:` = `packages/contracts/`。对现有代码的陈述均已打开文件核对（核对基准为 2026-10-18 工作区），以"文件 + 符号"为准，行号可能漂移。`py:benchmarks/`、`py:coordination/`、`py:kernel/` 等 D4 新路径在本文撰写时均不存在 |
| 文献约定 | 只引用调研报告中已列出的条目，核实状态原样保留：`[已核]` 为 r02 §0 联网核实的条目；`[待核实]`、`[待核实页码]` 在进入论文或正式文档前须按 r02 TODO-1 复核 |
| 数值约定 | 标注"初值""目标值""缺省"的数值为设计默认值或待 pilot（WP-D4-17）校准的目标，不是对现有代码性能的断言。本文样例中的参考值为按本文定义离线计算的示意值，物化时以平台参考求解器重算为准 |
| 写作规范 | 中文、技术名词保留英文；企业级行文；表格与编号清晰；不使用 emoji 与状态字形 |

---

## 0. 摘要

1. **问题**。S1 评测"在受限通信下，一组智能体如何在线地把公开到达的任务分配给自身并按时、按需组队完成"。任务带位置、服务时长、能力需求、所需智能体数、同步窗口、时间窗、价值与前置关系；任务公开到达、无需搜索（与 S2 正交）。Gerkey–Matarić 意义下 `d0`–`d1` 为 ST-SR-TA，`d2` 起为 ST-MR-TA；iTax 依赖类别覆盖 ID（`d0`–`d1`）与 XD（`d2` 的联盟同步与跨机前置），TA-C 高阶层（`d3`）加深 XD（§2.7）。
2. **主指标**。`U_TA = Σ_j v_j · q_j · ψ_j · δ_j / Σ_j v_j ∈ [0, 1]`，价值加权、同步质量与时效折扣后的完成率；分母为实例全部任务；时效折扣自最早可服务时刻起算、至服务开始时刻止（与 `path_score` 口径一致）。本文对 AWR-06 §7.1 的记号做了消歧（到达时刻、时间窗、服务半径、时效因子分别改记为 `t^a_j`、`[e_j, l_j]`、`r_srv`、`δ_j`，对照见 §1.4），并补齐未完成任务、参与者判定、早到等待与服务中断的规则（§2.4）。
3. **判定规则**。完成由评测器依据真值判定：智能体须持有对该任务的有效承诺（`task.claim`）、位于服务圆内且实体处于 `ACTIVE`，才具备参与资格（P-01），再按唯一归属规则确定参与者（P-02）；覆盖能力需求且到场时刻极差不超过同步窗口的参与者集合连续停留满服务时长即完成（P-04 至 P-07）。承诺只对承诺方可见，不构成免计费的信息通道（P-10）；任务的完成与过期状态以固定延迟向全队公开（P-11）。
4. **实例与生成器**。Family `ta-core`，生成器 `gen/ta_poisson@1`。实例固定世界、实体、任务池（位置、需求、价值、相对时间窗、前置关系）与实例内在扰动（`d2` 的定时载荷失效与外生分区）；Poisson 到达的具体时刻由运行期 `task` 随机域按外生种子实现，跨策略严格配对（§4.6）。时间窗在运行期统一截断到 H；生成器以与到达实现无关的充分条件保证每个任务在任意到达实现与实例内故障下均可由初始编制完成（G-06 至 G-08）。实例包格式、单位量化与视图分级遵循 M31（参考文件位于 `hidden/`）。
5. **分级**。难度 `d0`–`d4` 只由实例内在旋钮定义：任务/智能体比、能力异构度、联盟比例与同步窗口、硬时间窗比例与紧度、前置比例、障碍覆盖率、实例内故障与外生分区、机巢与能量约束（§5.1）。网络档是赛道坐标，不进入难度档（与 M31 OR-3、OR-4 一致）。规模 `n4`–`n64` 恒密度缩放，另设北极星配置 `d1/n20`。P0 冻结 `d0`–`d2` × `n4`–`n16`。
6. **运行模式与通信档**。算法隔离模式（研究者只提供 `Solver`，Suite 执行器 `ta-exec@1` 按 M31 `PlanExecutor` 协议负责路径跟随、承诺与同步等待）与闭环模式（`Policy` 同时决定运动、承诺与消息）分别成榜；全部平台基线以统一的零消息联盟汇合约定 `rv-slot@1` 处理同步到场（R-S1-28）。主榜 `dc-l1-b1-radio`（`abstract-radio@1 default`、`d4_std`）；`dc-l1-b1-ideal` 为对照榜；`cr-l1-b1-radio` 为集中式参考列；`abstract-disk@1` 只用于北极星、教学与训练（§6）。
7. **基线**。与 M30 §6.7.1 一致：`static_partition`（`π_ind`）、`nearest_greedy`、`hungarian_periodic`、`ssi`、`contract_net`、`cbba`（PA-2 代表性协同基线）、`rolling_milp`（`π_central`），另登记 `random`（`π_random`）与 `d2_greedy`（算法隔离附加基线）；TA-C 层（P1）另有 `kmeans_group`、`spectral_group`、`csg_greedy`。预期排序分为"验收约束"与"pilot 预期"两级，前者为 AC-S1-02 的硬条件，后者违反时须给出机制解释（§8.3）。
8. **验收**。AC-S1-01 至 AC-S1-21：生成器确定性与 `d0` 穷举最优锚点、基线序关系、区分度与余量、非饱和、确定性、成本与北极星、留出集校准、判定语义单元测试、承诺私有性、零通信不变性、共同随机数、预算执行、子编队投影、执行器语义、实例包校验、指标单元测试、TA-C、X1 接入、有效性声明、训练框架适配、联盟汇合（§10）。
9. **对 AWR-06 的反馈**。共 24 条，集中在记号冲突、未完成任务与参与者判定缺失、时效折扣终点、iTax 分类、网络档与难度档耦合、承诺可见性、字节与 CPU 预算的执行方式与表述、集中式参考命名不一致、无实体规划 Agent 的通信端点与分区归属、`d2` 故障与分区语义、基线预算可行性、WP-D4-14 工作量、`d3` 风场依赖、PA-4 的基线范围、PA-7 与 §6.5 网络参数调整的冲突、pilot 覆盖等（§15.1）；对 M28、M29、M30、M31、X1 与契约草案的提请见 §15.2。

---

## 1. 定位与范围

### 1.1 问题陈述

S1 承载 R-D4-03 中的"任务分配"与"动态组队与聚类"两个算法族（AWR-06 §7.6），在同一问题族中提供由易到难的连续阶梯：

| 层 | 档位 | 研究对象 | 分类定位 |
|---|---|---|---|
| 基础层 | `d0`–`d1` | 在线到达任务的单机分配与时间扩展调度；通信受限下的冲突消解 | ST-SR-TA；iTax ID |
| 标准层 | `d2` | 异构能力、联盟任务（两种互补载荷同步到场）、时间窗与前置关系、外生分区、定时载荷失效后的重分配 | ST-MR-TA；iTax ID + XD |
| 高阶层 TA-C | `d3`（P1，单列榜） | 动态组队：`m_j ∈ {2, 3}`、成员加入与退出、实体失效、机巢争用与能量约束 | ST-MR-TA；iTax XD（加深） |
| 极限层 | `d4`（P2） | 近容量上界的规模与负载 | 同 `d3` |

S1 不包含任务发现（S2）、信息融合（S3）与路径冲突消解（MAPF 储备 Suite）：任务公开到达，实体按高度层分层飞行，智能体之间不发生碰撞（§2.2）。

### 1.2 与其他文档的边界

| 文档 | 承担 | 本文承担 |
|---|---|---|
| AWR-06 §7.0、§7.1 | Suite 共同约定、S1 概要定义、AC-S1-1 至 AC-S1-6 | 完整形式化、判定规则、生成器、档位参数、指标公式、基线约束、扩展验收 |
| M28 实验内核 | 时钟、事件队列、随机域、`planar@1`、`PlanarWorld`、`DecisionValidator`、资源账本；预登记 `task.arrive`、`task.expire` 事件类型（R-M28-19） | S1 的任务监视器 `TaskMonitor`（参与与完成判定）：按 M31 R-M31-93 以 `SuiteRuntime.phase_participants()` 登记为 `WORLD_UPDATE` 阶段参与者，Suite 事件 `suite.taskalloc.*` 取 rank 100–199；`task.arrive`/`task.expire` 的处理器由 S1 绑定 |
| M29 交互网络 | 网络档模型与参数、`InteractionArbiter` | S1 对 `task.claim`、`task.release`、`commit.revoke` 的仲裁语义与可见性 |
| M30 协同接口与基线 | 插入点协议、`ProblemView`/`Plan` 信封、基线实现与基线卡 | S1 视图与计划的 `body` schema、基线在 S1 上的参数约束、预期排序 |
| M31 基准套件 | Suite/Family/Generator/Instance 协议、实例包格式与校验、划分与种子；`SuiteRuntime` 与 `PlanExecutor` 协议（R-M31-93 至 R-M31-96） | `ta-core` 的参数空间、生成步骤、S1 字段与视图注记、参数区块；`TaskMonitor` 与执行器 `ta-exec@1` 的实现（WP-D4-14） |
| M32 评测与统计 | 统计协议、排名、支配分层、Δ 管线 | S1 的 `MetricSpec` 登记与评测器公式 |
| 协作智能排行榜规格 | 赛道、划分、榜单版本、安全降级规则 | S1 场景集与赛道组合、安全指标与初值阈值 |
| X1 协议 | 扰动族、强度与指标 | S1 对 F、C、P、L、J、H（P1）与 M（P2）族的适用、`U_cum(t)`、`Held(o)`、响应操作与参考策略行为（§6.6） |

### 1.3 分阶段范围

| 阶段 | 范围 | 工作包 |
|---|---|---|
| P0（D3 封板后第 1–11 周） | `ta-core` 生成器与评测器；`d0`–`d2` × `n4`–`n16` 与北极星 `d1/n20`；执行器 `ta-exec@1`；9 个基线登记项（7 个 AWR-06 基线加 `random`、`d2_greedy`）；`d0` 穷举参考求解器；参考策略缓存；pilot；`dev`/`validation` 场景集 | WP-D4-14（主）、WP-D4-43（竖切）、WP-D4-13（参考缓存）、WP-D4-17（pilot）、WP-D4-18（P0 门） |
| P1（第 12–21 周） | `holdout` 冻结；`Δ_perm`；TA-C（`d3`，机巢、成员变化、联盟可行性检查、3 个分组基线）；`n32`；X1 F/P/L/H 族；PettingZoo/BenchMARL 适配下的算法隔离模式训练 | WP-D4-19、20、21、24、26、51 |
| P2（第 22–31 周） | `d4` 与 `n64`；异构实体（UGV/USV）编队实例；B-LLM 预算档；论文实验 E2、E3、E5–E8 | WP-D4-35、38、41、52 |

### 1.4 术语与记号

本文对 AWR-06 §7.1 的记号做如下消歧（理由见 §15 第 1 条）：

| 本文记号 | 含义 | AWR-06 §7.1 记号 | 单位 |
|---|---|---|---|
| `A`，`n = |A|` | 执行任务的智能体集合（每个控制 1 个任务实体） | `A` | — |
| `p_i⁰`、`v_i`、`a_i` | 实体 i 的初始位置、最大速度、最大加速度 | `p_i⁰`、`v_i` | m、m/s、m/s² |
| `c_i ∈ {0,1}^K` | 能力向量（载荷类型） | `c_i` | — |
| `T`，`j ∈ T` | 任务集合 | `T` | — |
| `t^a_j` | 到达时刻 | `a_j`（第一处） | s |
| `[e_j, l_j]` | 时间窗：开窗与截止（`l_j` 即 AWR-06 的 `b_j`） | `[a_j, b_j]` | s |
| `t0_j = max(t^a_j, e_j)` | 时效折扣起点（最早可服务时刻；P0 的 `e^off_j = 0`，故 `t0_j = t^a_j`） | 未定义 | s |
| `e^off_j`、`l^off_j` | 开窗与截止相对到达时刻的偏移（实例字段） | 未定义 | s |
| `t_reach_j` | 单任务可达时间（G-06） | 未定义 | s |
| `x_j`、`s_j` | 任务位置、服务时长 | 同 | m、s |
| `r_j ∈ {0,1}^K`、`m_j`、`w_j` | 能力需求、所需智能体数、同步窗口 | 同 | —、—、s |
| `v_j ∈ {1, 3, 5}` | 价值 | 同 | — |
| `D` | 前置关系有向无环图 | 同 | — |
| `r_srv` | 服务半径 | `ρ`（第一处） | m |
| `ρ_T = |T| / n` | 任务/智能体比 | `ρ`（第二处） | — |
| `λ_a`、`λ_d` | 到达率、时效衰减率 | `λ`、`λ_d` | s⁻¹ |
| `τ^s_j`、`τ_j` | 服务开始时刻、完成时刻 | `τ_j` | s |
| `Δt_sync,j` | 计入联盟成员的到场时刻极差 | 同 | s |
| `δ_j` | 时效因子 | `d_j` | — |
| `H`、`H_arr` | Episode 时长、最晚到达时刻 | `horizon_s` | s |
| `B_cap`、`CPU_cap` | 团队字节额度、团队 CPU 额度 | 同 | B、CPU-ms |
| `t_tr(d)` | `planar@1` 由静止起步驶向距离 d 的目标、首次进入服务圆的时间（§9.4 精确定义） | 未定义 | s |

其余术语（Suite、Family、Instance、场景配置、划分、赛道、Δ 族、`π_ind`、`π_central`、CRN）见 AWR-06 术语表与 M31 §1.4。

---

## 2. 形式化定义

### 2.1 实例

实例 `I = (W, A, H_st, T, D, Φ, Π, H)`：

| 要素 | 内容 |
|---|---|
| `W` | 平面世界：矩形边界 `[0, L_n] × [0, L_n]`、简单多边形障碍集合（`blocks_motion = blocks_los = true`），M28 `PlanarWorld` |
| `A` | 任务实体（`kind = multirotor`）及其控制 Agent；`d3` 起含机巢 `N`（每座同时仅容 1 架充电） |
| `H_st` | 地面站集合：`kind = edge_node` 的静止实体，只用于承载无实体规划 Agent 的通信端点（§6.2），不具任务能力 |
| `T`、`D` | 任务池与前置关系 |
| `Φ` | 实例内在故障计划（`d2` 起） |
| `Π` | 实例内在外生分区计划（`d2` 起） |
| `H` | Episode 时长，P0 全部档位 `H = 300 s` |

运行期外生随机量（到达时刻实现、导航噪声、信道与包级随机）不属于实例，由场景配置的外生种子经 AWR-06 §5.5 的随机域派生（§4.6）。

### 2.2 智能体与实体

| 编号 | 规则 |
|---|---|
| R-S1-01 | 每个任务 Agent 在初始编制中以 `FULL` 范围控制恰好 1 个任务实体（`agents_template` 一机一 Agent）；集中式拓扑由一个决策主体持有全部 Assignment（AWR-06 §4.1）；`d3` 的计划加入实体由预声明的待命 Agent（`role = standby`，自 Episode 开始运行、不控制实体、通信端点下线）在加入时刻接管（§5.5），集中式拓扑中由集中决策主体接管 |
| R-S1-02 | 实体运动由 `planar@1` 承载（M28 R-M28-51 至 R-M28-55）：`v_i = 10 m/s`、`a_i = 2 m/s²`（初值，同构，`d3` 起引入速度异构）；每个实体分配互不相同的高度层 `z_i = 30 + 5·((i − 1) mod 8)` m（初值），实体之间不做碰撞（M28 R-M28-54），S1 不评测机间避碰 |
| R-S1-03 | 能力词表取自 `ct:agent/capability_catalog.json`：`d0`–`d1` 同构，全部实体具备 `rgb.capture`；`d2` 起 K = 3，载荷类型为 `rgb.zoom`、`thermal.imaging`、`lidar.scan`，每个实体恰具其一，按实体序号轮转分配，使各类型数量相差不超过 1 |
| R-S1-04 | 能量：`d0`–`d2` 能量不构成约束（`energy_wh` 取足以覆盖 2H 的值），只记录消耗；`d3` 起能量约束生效，规则见 §5.5：实体须在耗尽前停靠机巢充电；耗尽实体置 `DEPLETED`（M28 R-M28-55），在本 Episode 内不可恢复，计入 `depleted_count` |
| R-S1-05 | 观测（`L1`）：`self_state` 为带噪导航解（M28 R-M28-59，`sensor` 域，`object_id = "nav:" + entity_id`）；`tasks` 为任务公告板视图（P-11）；`sensors` 为空（S1 不含感知）；Agent 不能经观测得知队友位置与承诺，只能经交互网络的消息获得 |

### 2.3 任务

任务 `j = (t^a_j, x_j, s_j, r_j, m_j, w_j, [e_j, l_j], v_j, pred_j)`，`pred_j ⊆ T` 为 `D` 中 j 的直接前驱：

| 编号 | 规则 |
|---|---|
| R-S1-06 | 单机任务 `m_j = 1`，`|r_j| = 1`，`w_j` 不适用；联盟任务 `m_j ≥ 2`，`r_j` 恰含 `m_j` 个互不相同的能力位（P0 的 `d2` 为 `m_j = 2`、两种互补载荷），`w_j > 0` |
| R-S1-07 | 时间窗以相对到达时刻的偏移存储（§3.3）；运行期按到达实现换算并统一截断到 H：`e_j = t^a_j + e^off_j`、`l_j = min(t^a_j + l^off_j, H)`，硬截止与软截止同一规则；软截止任务（`deadline_type = soft`）的 `l^off_j = H`，故 `l_j = H`。P0 全部档位 `e^off_j = 0`。由此 `l_j ≤ H` 对任意到达实现成立；截断不改变实例内容，只影响运行期的有效紧度，评测器逐 Episode 报告 `kappa_eff_mean`（§7.2） |
| R-S1-08 | 前置关系只从初始任务（`initial = true`，到达序号最小的一段）指向到达序号更大的任务（保证无环且前驱到达时刻恒为 0，使前置可行性与到达实现无关，G-07）；任务 j 只有在 `pred_j` 全部完成后才能开始服务；前驱过期或未完成时 j 不可完成，但仍计入 `U_TA` 分母 |
| R-S1-09 | 任务在 `t^a_j` 由 `task.arrive` 事件（`WORLD_UPDATE`，rank 50）进入公告板，到达前不可见、不可承诺；在 `l_j` 未完成者由 `task.expire`（rank 60）关闭 |

### 2.4 参与与完成判定

完成判定由 Suite 的任务监视器（`TaskMonitor`，按 M31 R-M31-93 登记的 `WORLD_UPDATE` 阶段参与者）在线执行，并由评测器以同一纯函数对 trace 离线重算（AC-S1-01(c)）。判定在物理边界 `t = k·Δp` 上进行（`d4_std` 下 Δp = 20 ms）。每个边界按"资格（P-01）→ 归属（P-02）→ 到场时刻（P-03）→ 覆盖与开始（P-04、P-05）"的次序求值，后一步只依赖前一步与上一边界的状态，因此判定对第三方实现唯一。

| 编号 | 规则 |
|---|---|
| P-01 资格 | 智能体 i 在 t 时刻对任务 j 具备参与资格（记 `i ∈ E_j(t)`），当且仅当：(a) i 的实体为 `ACTIVE` 且未停靠机巢（`d3` 起，R-S1-30）；(b) `‖p_i(t) − x_j‖ ≤ r_srv`（`r_srv = 5 m`，初值；二维投影距离）；(c) i 在 t 时刻持有对 j 的有效承诺；(d) `c_i(t) ∩ r_j ≠ ∅`（`c_i(t)` 为 t 时刻的能力位）；(e) `e_j ≤ t ≤ l_j` 且 `pred_j` 全部已完成。资格只依赖 i 与 j 自身的状态，不引用其他任务 |
| P-02 归属与参与者 | 对每个 i，记 `J_i(t) = {j : i ∈ E_j(t)}`。`J_i(t) = ∅` 时 i 无归属；否则 i 归属于 `a_i(t) ∈ J_i(t)`，按以下次序唯一确定：(1) 连续性：若 i 在上一边界 `t − Δp` 是某任务 j' 的计入成员（`i ∈ S*_{j'}`，P-04）且 `j' ∈ J_i(t)`，则 `a_i(t) = j'`；(2) 否则取 i 对其承诺授予时刻最早的任务；(3) 授予时刻相同时取 `task_id` 升序最小者。任务 j 在 t 时刻的参与者集合 `P_j(t) = {i : a_i(t) = j}` |
| P-03 到场时刻 | `t^in_{i,j}` 为 i 最近一次进入 `P_j` 的物理边界时刻（即 `i ∈ P_j(t)` 且 `i ∉ P_j(t − Δp)` 的最近边界）；i 离开服务圆、承诺失效、实体失能或改归属其他任务后再次进入 `P_j` 时，到场时刻更新 |
| P-04 覆盖与服务开始 | 集合 `S ⊆ P_j(t)` 覆盖 j，当且仅当 `|S| = m_j` 且 `∪_{i∈S} c_i(t) ⊇ r_j`；其极差 `spread(S) = max_{i∈S} t^in_{i,j} − min_{i∈S} t^in_{i,j}`。服务未进行时，在首个满足"存在覆盖集合 S 使 `spread(S) ≤ w_j`"（单机任务恒满足）的物理边界开始，`τ^s_j` 取该时刻；计入成员 `S*_j` 取 `spread` 最小的覆盖集合，并列时取成员 `agent_id` 升序字典序最小者；`Δt_sync,j = spread(S*_j)`。服务进行中 `S*_j` 不变（P-07） |
| P-05 早到 | 早到者在服务圆内等待会使其到场时刻固定在较早值，`spread` 随等待增大；`spread > w_j` 时服务不能开始，早到者须退出服务圆后再进入以更新到场时刻。平台基线与执行器统一以汇合约定 `rv-slot@1` 处理（R-S1-28、X-04） |
| P-06 唯一计入 | P-02 的归属唯一性保证同一智能体在同一时刻至多是一个任务的参与者，因而至多是一个任务的计入成员；服务进行中的计入成员因 P-02(1) 的连续性不被服务圆重叠的其他任务抢走 |
| P-07 完成与中断 | 自 `τ^s_j` 起，`S*_j` 的全部成员在每个物理边界上持续属于 `P_j(t)`，累计达 `s_j` 时任务完成，`τ_j = τ^s_j + s_j`；任一成员中途不属于 `P_j(t)` 时服务中断，累计清零，重新按 P-04 判定 |
| P-08 截止 | 只有 `τ_j ≤ l_j` 的完成有效；`l_j` 时刻未完成者过期，此后不再计入服务 |
| P-09 冗余参与 | 服务期间 `|P_j(t)| > m_j` 的额外参与者不影响完成，计入重复执行率（§7.2） |
| P-10 承诺私有性 | `task.claim`、`task.release`、`commit.revoke` 由仲裁器只记录不仲裁冲突（AWR-06 §6.7）；其 `InteractionEvent` 只进入承诺方自身的下一次观测，不进入其他 Agent 的观测、公告板或收件箱，不消耗网络预算；承诺信息若需共享，须由策略经交互网络自行发送并计费 |
| P-11 公告板 | 公告板 `obs.tasks` 只含已到达且未关闭的任务（`status = open`）与最近关闭的任务（`status ∈ {done, expired}`）；完成与过期状态在 `τ_j` 或 `l_j` 之后经固定延迟 `status_pub_delay = 5 s`（初值）对全队公开；服务进行中的状态、参与者与承诺方不公开。公告板对全部策略（含 `π_ind`）相同，是平台提供的、不计费的低速公共信息，写入有效性声明 |

承诺仲裁细则（与 M29 R-M29-75 对齐；状态名按 AWR-06 v0.3 XR-09 定稿）：

| 操作 | 条件（按行序判定，首个命中者生效） | 结果状态（S1 采用） | `detail` | M29 R-M29-75 / 契约草案对应 |
|---|---|---|---|---|
| `task.claim{task_id}` | 任务未到达或 id 不存在 | `DENIED_INVALID` | `not_arrived` 或 `unknown` | M29 R-M29-75；契约草案 §4.3.4 |
| 同上 | 任务已完成或已过期 | `DENIED_INVALID` | `closed` | 同上 |
| 同上 | 承诺方实体非 `ACTIVE` | `DENIED_INVALID` | `actor_inactive` | 同上（v0.2.1 已纳入 R-M29-75） |
| 同上 | 能力不相交或实体已失去该能力 | `DENIED_CAPABILITY` | — | M29 `DENIED_CAPABILITY` |
| 同上 | 承诺方当前有效承诺数已达 `L_claim = 3`（初值） | `DENIED_BUSY` | `claim_limit` | 同上（v0.2.1 已纳入 R-M29-75）；`DENIED_BUSY` 为 AWR-06 §4.2 既有枚举 |
| 同上 | 其余（任务 `open` 且上述均不命中） | `RECORDED`（已登记，不表示仲裁通过） | — | M29 R-M29-75；契约草案 §4.3.4（FB-CT-06） |
| `task.release` / `commit.revoke` | 承诺方持有该承诺 | `RECORDED`；二者对归属的效果相同，trace 中分别记录，`commit.revoke` 供 X1 识别主动撤销（§6.6） | — | 同上 |
| 同上 | 承诺方不持有该承诺 | `DENIED_INVALID` | `not_held` | 同上 |
| 隐式失效 | 任务完成或过期 | 全部承诺自动失效，不产生事件；承诺方实体失能时其承诺不自动失效（失效检测是被测对象） | — | — |

### 2.5 决策与交互

| 运行模式 | Agent 输出 | 平台负责 |
|---|---|---|
| 闭环模式（`closed_loop`） | `EntityCommand`（`goto_w`、`follow_path`、`hold`、`vel_w`）、`InteractionOp`（承诺类）、`SendRequest` | 物理、网络、仲裁、判定 |
| 算法隔离模式（`algorithm_isolated`） | `Solver.solve(view, budget) → Plan`（M30 R-M30-18），计划步为有序 `{task, t_start_s?}` | 执行器 `ta-exec@1`（§6.2）把计划转为指令与承诺；状态报告与计划推送协议（`planner` 宿主）由平台提供并计费 |

决策周期 Δd 与建模决策时延 `ℓ_model` 按时钟档（AWR-06 §5.1）；一跳请求–应答往返下限 `2 × Δd + 网络时延`（AWR-06 §5.4）写入 S1 的 `validity.md`，同周期多轮拍卖不可表达。`wake_on_rx`（P1）在 S1 中允许，只能发送消息与改写承诺，开启与否为不同赛道条件。

### 2.6 目标

在资源包 `R`（实体集合与初始状态、地图、公告板、`B_cap`、`CPU_cap`、token 额度 0、能量）与安全约束下最大化 `U_TA`（§7.1）。通信、计算与能耗另报，不进入 `U_TA`。

### 2.7 分类定位

| 档位 | Gerkey–Matarić（IJRR 23(9), 2004 [待核实页码]） | iTax（Korsah, Stentz, Dias, IJRR 32(12):1495–1512, 2013 [已核]） | 时间与顺序约束（Nunes 等, RAS 90, 2017 [待核实页码]） | 说明 |
|---|---|---|---|---|
| `d0` | ST-SR-TA（任务数为智能体数的 2 倍，需时间扩展调度） | ID：智能体对任务的效用依赖其自身调度中的其他任务（路径与时效折扣） | 无时间窗 | 可穷举求最优（§9.4） |
| `d1` | ST-SR-TA，在线到达 | ID | 软截止 | 动态到达使问题成为在线问题 |
| `d2` | ST-MR-TA（25% 联盟任务） | ID + XD：联盟任务的同步约束与跨智能体前置使一个智能体的效用依赖其他智能体的调度 | 硬时间窗、前置、同步 | AWR-06 §7.1 将 XD 归入 TA-C，本文修正（§15 第 4 条） |
| `d3`（TA-C） | ST-MR-TA，`m_j ∈ {2, 3}` | XD（加深）：成员加入与退出、失效后联盟重组 | 硬时间窗、前置、同步、机巢资源 | 动态组队与聚类算法族的首个承载 |
| 全部 | 不含 MT（每个智能体同一时刻至多服务一个任务，P-06） | 不含 CD（任务分解方式固定，不由分配器选择） | — | — |

联盟形成的特征函数取任务绑定的加性形式：联盟 C 服务任务 j 的价值为 `v_j · ψ_j · δ_j`（满足能力覆盖时），联盟结构价值为被服务任务价值之和（r02 §3.2 的"任务绑定型"）。

---

## 3. 实例字段与实例包

### 3.1 实例包文件与 S1 字段

实例包目录与文件规则遵循 M31 §7.4；下表给出 S1 在各文件中的字段。字段名以秒、米书写处，在实例包中以 `_ms`、`_mm` 等后缀的整数存储（M31 §7.3.3），`Instance` 访问器向上层返回浮点值。

| 文件 | 视图 | S1 字段 | 对应 AWR-06 §7.1 / r02 §3.1 字段 |
|---|---|---|---|
| `manifest.json` | — | M31 §8.2 全部字段（`files[]` 只登记内容文件，参考文件登记于 `reference_files[]` 并计入 `reference_sha256`）；`world_kind = planar`；`difficulty{}` 为 §4.5 的客观难度量 | `instance_id`、`generator`、`difficulty` |
| `brief.json` | `brief` | `region{bounds_mm}`、`obstacles_ref`、`entities_public[{id, kind, pos0_mm, z_layer_mm, v_max_mm_s, a_max_mm_s2, capabilities[]}]`、`stations[{id, pos_mm}]`、`nests[{id, pos_mm, capacity}]`（`d3` 起）、`arrival_process{kind, horizon_arr_ms}`、`cost_model`、`status_pub_delay_ms`、`claim_max`、`service_radius_mm` | `agents[]` 的公开部分、`cost_model`、`arrival_process` |
| `world/planar.json` | `brief` | `bounds_mm`、`obstacles[{id, vertices_mm[], blocks_motion, blocks_los}]` | `cost_model = planar_obstacle` 的几何 |
| `agents.json` | `brief` | `entities[{id, kind, pos0_mm, z_layer_mm, v_max_mm_s, a_max_mm_s2, capabilities[], energy_wh}]`、`agents_template[{agent_id, controls[{entity, scope}], role}]`（`role ∈ {task, planner}`） | `agents[{id, kind, pos0, speed, capacity, capability_vec, energy}]`；`capacity` 在 S1 恒为 1（ST），不单列 |
| `tasks.json` | `scheduled` | `tasks[{id, arrival_ordinal, arrival_ms, initial, pos_mm, service_ms, required_capability[], required_agents, sync_window_ms, window{open_offset_ms, close_offset_ms}, deadline_type, value, predecessors[]}]`；`arrival_ms` 对 `static` 与 `scripted` 为定值，对 `poisson` 为 `null`（运行期实现，§4.6） | `tasks[{id, arrival_s, pos, service_s, required_capability, required_agents, sync_window_s, time_window, deadline_type, value}]`、`precedence_edges` |
| `comm.json` | `brief` | `comm_zones[]`（P0 为空，保留）、`partitions_public[]`（S1 恒为空：S1 的分区均不公告）、`relays[]`（P0 为空） | `communication_profile` 移除（网络档为赛道坐标，§6.3）；地面站在 `agents.json` 与 `brief.json` 中给出 |
| `objective.json` | `brief` | `primary = u_ta`、`lambda_d_per_s`、`partial_credit`、`horizon_ms`、`termination = horizon`、辅助与资源指标 id | `objective = u_ta`、`horizon_s` |
| `hidden/truth.json` | `truth` | `nonce`（M31 PK-07）、`faults[{id, t_ms, entity, kind, params}]`、`membership[]`（`d3` 起） | `faults[]` |
| `hidden/comm_truth.json` | `truth` | `nonce`、`partitions[{id, t_start_ms, duration_ms, groups[[agent_id…], …], unassigned[agent_id…]}]`（`unassigned` 为不属于任一组、与两组均断开的端点，S1 为规划 Agent，§6.2 X-01）、`link_mask[]`（S1 恒为空） | 分区不公告，按 M31 §7.4.1 置于 `hidden/comm_truth.json` |
| `perturbation.json` | `brief` | 仅 `stress` 场景集与 X1 运行：X1 §12.3 结构 `{schema, protocol, families[], q_levels[], host{n_nominal, horizon_ns, decision_period_ns, candidates[], nests[], standby_slots}, early_stop_invariant, response_ops_ref, u_cum_ref}`；强度 `q` 不写入实例包，只写入运行清单 `perturbation{family, q}`；`candidates[]` 排除实例内故障对象（§6.6） | `perturbation_ref` |
| `hidden/reference/reference.json` | `truth`（参考文件） | `{nonce, kind ∈ {J_star, BKS, LB}, value, solver, solver_version, timing_model, tolerance_rel, source}`；`d0` 为 `J_star` | `reference{J_star | BKS | LB, source}` |
| `eval/binding.json` | `brief` | `{evaluator: "taskalloc@1", thresholds_ref}` | — |
| `hidden/eval/expected.json` | `truth`（参考文件，仅锚点） | `{nonce, evaluator, cases[]}`：锚点实例的期望分量与容差 | — |

### 3.2 规范标识与约束

| 编号 | 规则 |
|---|---|
| R-S1-10 | 实例标识 `capl:ta-core/<block>-<index>/d<k>/n<S>@<rev>`（M31 OR-1）；实体 id `uav_NNNNN`、地面站 `gs_NNNNN`、机巢 `nest_NNNNN`、Agent `agt_NNNNN`、任务 `task_NNNNN`（M31 PK-04），任务 id 按到达序号单调编号 |
| R-S1-11 | 实例只含实例坐标与外生通信特征，不含网络档 id 与网络参数集（M31 OR-3、V-I-17）；同一实例在 `ideal@1` 与 `abstract-radio@1` 下的运行构成配对对照 |
| R-S1-12 | 视图按文件划分（M31 R-M31-26、§7.4.3）：`tasks.json` 为 `scheduled`；`hidden/` 下全部文件（`truth.json`、`comm_truth.json`、`reference/reference.json`、`eval/expected.json`）为 `truth`，且 `truth` 文件只在 `hidden/` 下；其余为 `brief`；文件 schema 内字段的 `x-visibility` 与文件视图一致（V-I-09）。`validation` 分发 `brief` 与 `scheduled`，因此 `validation` 参与者可离线获知任务池，但不能获知 Poisson 到达的具体时刻（由秘密外生种子实现）、故障、分区与参考值（`J_star` 只随 `dev` 锚点分发）；`holdout` 两者均不分发 |
| R-S1-13 | S1 实例校验在 M31 V-I 规则之外增加：任务位置不在障碍内且距障碍边 ≥ `r_wait = r_srv + 10 m`（G-05）；`r_j` 位数与 `m_j` 一致；`predecessors` 只引用初始任务且到达序号更小；`open_offset_ms = 0`（P0）；`close_offset_ms − open_offset_ms ≥ service_ms`；以生成器同一实现重算 `t_reach_j`，复核 G-06 的紧度条件与水平可行性条件、G-07 的前置可行性条件与 G-08 的故障对象约束；地面站位置不在障碍内；`agents_template` 中 `role = task` 的 Agent 恰控制 1 个 `multirotor` |

### 3.3 单位与量化

| 量 | 字段后缀 | 量化（M31 §7.3.3） | 说明 |
|---|---|---|---|
| 位置、长度 | `_mm` | `floor(x·1000 + 0.5)` | 区域边长按米取整后乘 1000 |
| 时刻、时长 | `_ms` | `floor(t·1000 + 0.5)` | 服务时长生成时取整秒 |
| 速度、加速度 | `_mm_s`、`_mm_s2` | 同长度 | `_mm_s2` 已由 M31 §7.3.3 登记（M31 §13 第 16 项） |
| 价值、计数、序号 | 整数 | — | — |
| 衰减率、比例常量 | 浮点 | 声明常量，不由随机数导出 | `lambda_d_per_s`、`partial_credit` |

---

## 4. 生成器 `gen/ta_poisson@1`

### 4.1 参数空间

`Generator.params_space()` 返回下表参数；难度预设（`difficulty_presets`）与区块参数（`blocks[]`）按 M31 R-M31-14 叠加解析，解析结果写入 `manifest.json.generator.params`。

| 参数 | 类型 | 含义 | 取值域 |
|---|---|---|---|
| `n` | int | 任务实体数 | {4, 8, 16, 20, 32, 64} |
| `L_ref_m`、`n_ref` | int | 参考边长与参考规模 | 600、8 |
| `rho_T` | float | 任务/智能体比 | [1, 6] |
| `arrival.kind` | enum | `static`、`poisson`、`scripted` | — |
| `arrival.initial_frac` | float | 在 t = 0 到达的任务占比 | [0, 1] |
| `arrival.horizon_arr_s` | int | 最晚到达时刻 `H_arr` | ≤ H − 60 |
| `arrival.profile` | enum | `steady`（条件 Poisson：到达时刻为 [0, H_arr] 上的次序统计量）、`bursty`（两段强度，强度比 `burst_ratio`） | — |
| `layout.kind` | enum | `uniform`、`clustered`（`k_cl` 个高斯簇，σ = `cl_sigma_frac · L_n`） | — |
| `obstacles.kind`、`obstacles.cover` | enum、float | `blocks`（轴对齐矩形网格扰动）、`scatter`（随机凸多边形）；覆盖率 `o` | `o ∈ [0, 0.2]` |
| `cap.K`、`cap.demand` | int、enum | 能力类型数；需求分布 `balanced`、`skewed`（一类需求占 50%） | K ∈ {1, 3} |
| `service_s` | [int, int] | 服务时长整秒均匀区间 | — |
| `value_probs` | [float×3] | `v ∈ {1, 3, 5}` 的概率 | 缺省 (0.5, 0.3, 0.2) |
| `coalition.frac`、`coalition.m`、`coalition.w_s`、`coalition.service_s` | — | 联盟任务占比 `f_C`、所需数、同步窗口、服务时长 | — |
| `window.hard_frac`、`window.kappa` | float、[float, float] | 硬截止占比 `f_hard`、紧度区间 | — |
| `prec.frac`、`prec.max_chain` | float、int | 有前驱的任务占比 `f_prec`、链长上限 | — |
| `window.open_offset_s` | int | 开窗偏移 `e^off_j` | P0 恒为 0 |
| `faults[]` | list | `{kind, t_s, count}`；`d2` 为 `{payload_loss, 150, 1}` | — |
| `partitions[]` | list | `{t_start_range_s, duration_s, kind = balanced_bipartition}` | — |
| `nests`、`energy_ratio` | int、float | `d3` 起：机巢数与能量预算比（§5.5） | — |
| `membership[]` | list | `d3` 起：`{kind ∈ {join, retire}, t_range_s, count}` | — |
| `start.kind` | enum | `stratified`（初始位置按 √n × √n 分层抽样，层内均匀） | P0 只用 `stratified` |

### 4.2 生成器随机子域

生成器随机流按 M31 §7.3.2 派生：`(k_hi, k_lo) = BLAKE2b-128(instance_seed ‖ generator_digest ‖ sub_domain ‖ object_id)`，经流 23 的 `cbrng` 五元键取样。子域在 `ct:coord/rng_domains.v1.json` 中以 `instance/ta-core/<sub_domain>` 登记：

| 子域 | `object_id` | `draw` 含义与上界 | 用途 |
|---|---|---|---|
| `layout` | `obstacle/<k>`、`cluster/<k>` | 顶点与中心坐标，≤ 2¹⁶ | 障碍与任务簇 |
| `start` | `uav/<i>` | 层内坐标，2 | 初始位置 |
| `capability` | `demand` | 任务序号 × 4，≤ 2²⁰ | 需求类型 |
| `task` | `task/<j>` | 位置、服务时长、价值、窗口紧度、联盟标记；拒绝采样序号 ≤ 64（GD-06） | 任务池 |
| `precedence` | `prec` | 任务序号，≤ 2²⁰ | 前置边 |
| `fault` | `fault/<k>` | 实体选择，1 | 实例内故障对象 |
| `partition` | `partition/<k>` | 起始时刻与分组置换，≤ n + 1 | 外生分区 |

### 4.3 生成步骤

| 编号 | 步骤 |
|---|---|
| G-01 | 区域边长 `L_n = round(L_ref · √(n / n_ref))` m（`n4` 424 m、`n8` 600 m、`n16` 849 m、`n20` 949 m、`n32` 1200 m、`n64` 1697 m）；任务数 `|T| = round(ρ_T · n)`（`d0` 例外，固定 `|T| = 8`，§5.1） |
| G-02 | 障碍：按 `obstacles.kind` 生成，直至覆盖率达到 `o`（容差 ±0.5 个百分点）；障碍两两不相交、不自交、距边界 ≥ 10 m；障碍之间留出宽度 ≥ 20 m 的通道，保证自由空间连通（以 5 m 栅格洪泛检查，不连通则重抽，序号计入 GD-06 上界） |
| G-03 | 初始位置：`stratified` 分层抽样，位于自由空间且两两距离 ≥ 20 m；地面站 `gs_00001` 置于距区域中心最近的自由空间点 |
| G-04 | 能力：按 R-S1-03 分配实体能力；任务需求按 `cap.demand` 抽取；联盟任务按 `coalition.frac` 标记（取整到最近整数且 ≥ 1，当 `f_C > 0`），需求为 `m_j` 个互不相同的类型 |
| G-05 | 任务属性：位置按 `layout` 抽取（落在障碍内或距障碍边 < `r_wait = r_srv + 10 m` 时重抽，使等待环与环到中心的直线段不与障碍相交，R-S1-28；`d0` 另要求任务之间、任务与初始位置之间距离 ≥ 20 m，§9.4）；`s_j` 整秒均匀；`v_j` 按 `value_probs`；`initial_frac` 部分在 t = 0 到达（`initial = true`），取到达序号 1 至 `N_init`；其余按 `arrival.profile` 在 (0, H_arr] 内到达，取到达序号 `N_init + 1` 至 `|T|`；序号在两段内各按位置无关的随机置换赋予。G-08 须先于 G-06 执行（G-06 依赖故障对象） |
| G-06 | 单任务可行性（与到达实现无关）。(1) 可达时间：记 `A_k = {i : c_i,k = 1 且 i 不是 Φ 中任何故障的对象}` 为整局保持能力 k 的实体集合，`t_reach_j = max_{k ∈ r_j} min_{i ∈ A_k} t_tr(‖p_i⁰ − x_j‖_path)`（单机任务即最近的保持能力实体；联盟任务取各所需能力最近者中的最晚者），`t_tr` 按 §9.4 的精确模型，`‖·‖_path` 为 `cost_model` 下的路径长度。(2) 紧度：硬截止任务 `l^off_j = e^off_j + s_j + κ_j · max(t_reach_j, 20 s)`，`κ_j ~ U[κ_lo, κ_hi]`（`κ_lo ≥ 1`）；软截止任务 `l^off_j = H`。(3) 水平可行性：以参考到达时刻 `t^a_ref,j` 检验 `t^a_ref,j + e^off_j + t_reach_j + s_j ≤ H`，不满足时重抽该任务的位置；`t^a_ref,j` 对 `static` 与初始任务为 0，对 `poisson` 的非初始任务为 `H_arr`（可能的最晚到达），对 `scripted` 为脚本值。(4) 保证：由 R-S1-07 的截断规则，对任意到达实现 `t^a_j ≤ t^a_ref,j`，有 `l_j − t0_j − s_j ≥ t_reach_j`——未截断时左式为 `κ_j · max(t_reach_j, 20 s) ≥ t_reach_j`，截断时左式为 `H − t^a_j − e^off_j − s_j ≥ H − t^a_ref,j − e^off_j − s_j ≥ t_reach_j`。`t_reach_j` 以初始位置为参照，是"初始编制可完成"的实例层保证，不是对运行期任意状态的保证 |
| G-07 | 前置关系（与到达实现无关）：对 `f_prec` 比例的任务 j，从初始任务中到达序号更小者里均匀选取 1 个前驱 i，候选须满足 `EC_i + t_tr(‖x_i − x_j‖_path) + s_j ≤ min(l^off_j, H)`，其中 `EC_i` 为 i 的最早完成时刻：i 无前驱时 `EC_i = t_reach_i + s_i`，有前驱 i' 时 `EC_i = max(t_reach_i, EC_{i'} + t_tr(‖x_{i'} − x_i‖_path)) + s_i`；链长 ≤ `max_chain`。因前驱到达时刻恒为 0 而 `t^a_j ≥ 0`，下述调度对任意到达实现可行：i 由 G-06 意义下的最近保持能力实体在 `EC_i` 完成；j 由其最近保持能力实体服务，与 i 共用的实体先服务 i 再前往 j；j 的完成时刻不晚于 `max(t^a_j + e^off_j + t_reach_j, EC_i + t_tr(‖x_i − x_j‖_path)) + s_j`，由 G-06(3)、G-06(4) 与本式知其 ≤ `l_j`。该条件是单链可行性，不保证全部任务联合可行（联合调度正是被测问题）；无合格候选时 j 不设前驱，`f_prec` 的实现值写入 `difficulty.f_prec` |
| G-08 | 故障与分区：`payload_loss`（`d2`）与 `entity_fail`、`retire`（`d3`）的对象实体按 `fault` 子域在合格集合中均匀选取；合格集合为"移除该对象（及已选对象）后，每种能力类型仍至少有 1 个持有者"的实体，使任一任务的能力覆盖在全部实例内故障之后仍可满足（联盟任务所需的能力位互不相同，每类 1 个持有者即足够）；合格集合为空时生成失败（R-S1-16）。例：`n4`、K = 3 的分配为 2/1/1，只有持有重复类型的两个实体合格。分区组为任务 Agent 集合的均衡二分（`partition` 子域置换后前 ⌊n/2⌋ 个为第一组，其余为第二组），起始时刻在 `t_start_range_s` 内整秒均匀；规划 Agent 的归属见 X-01。分区以 Agent 身份而非位置定义，与策略无关，保证跨策略配对 |
| G-09 | 写出前全部浮点量量化（GD-05）；集合按规范 id 排序后遍历（GD-03）；计算 §4.5 的客观难度量写入 `manifest.json.difficulty` |

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 |
|---|---|---|---|---|
| R-S1-14 | 生成器为纯函数（M31 GD-01 至 GD-06）；同 `(generator_digest, resolved_params, instance_seed)` 生成的实例 JCS 摘要逐字节一致 | P0 | P0 | AC-S1-01 |
| R-S1-15 | 生成器只依赖 numpy 与标准库，进入核心 wheel；`d1/n20` 的 10 个 `dev` 实例本地生成总墙钟 ≤ 2 s（目标值，RM-8，与 M31 R-M31-N03 一致） | P0 | P0 | AC-S1-06 |
| R-S1-16 | 拒绝采样超出 GD-06 上界时生成失败并报告参数，不静默放宽约束 | P0 | P0 | AC-S1-15 |

### 4.4 区块与划分隔离

S1 为不依赖真实地理的问题族，按 AWR-06 §8.7 以"参数区块"隔离划分。区块是 `(layout, obstacles, arrival.profile, cap.demand)` 的命名组合：

| 区块 id | `layout` | `obstacles.kind` | `arrival.profile` | `cap.demand` | 划分 |
|---|---|---|---|---|---|
| `pu` | `uniform` | `blocks` | `steady` | `balanced` | `dev` |
| `pc` | `clustered`（k_cl = 4） | `scatter` | `steady` | `balanced` | `dev` |
| `ns` | `uniform` | `blocks` | `steady` | `balanced` | `dev`（北极星专用，`d1/n20`） |
| `vb` | `clustered`（k_cl = 3） | `blocks` | `bursty` | `skewed` | `validation` |
| `h1`、`h2` | 隐藏 | 隐藏 | 隐藏 | 隐藏 | `holdout`（只公开 `params_commit`，M31 R-M31-49） |
| `ac`（P1） | `uniform` | `blocks`（`o = 0`） | `static` | `balanced` | `dev`（`d0` 档内的 TA-C 联盟锚点区块：小规模联盟任务，可穷举，供 `opt_ratio`） |

`stress` 不设区块：`stress` 场景集是 `holdout` 场景的子集（M31 R-M31-47、V-S-08），由 X1 协议选取（§6.6）。

`holdout` 区块须至少在两个维度上与 `dev`、`validation` 区块不同（如簇数与强度比），参数区间在冻结前以参考策略做难度校验（AC-S1-07）。

### 4.5 客观难度量 `Generator.difficulty(inst)`

| 量 | 定义 | 用途 |
|---|---|---|
| `rho_T` | `|T| / n` | 任务负载 |
| `load_W` | `Σ_j m_j (s_j + t̄_nn) / (n · H_arr)`，`t̄_nn` 为任务位置最近邻距离均值除以 `v` | 智能体时间负载；`d4` 目标 ≥ 0.9 |
| `kappa_mean` | 硬截止任务名义紧度 `κ_j = (l^off_j − e^off_j − s_j) / max(t_reach_j, 20 s)` 的均值（截断前，与到达实现无关） | 时间窗紧度（AWR-06 §7.1 的 κ）；运行期截断后的有效值由评测器逐 Episode 报告为 `kappa_eff_mean`（§7.2） |
| `trunc_frac_ref` | 硬截止任务中 `t^a_ref,j + l^off_j > H` 的占比（以最晚可能到达为参照，运行期截断占比的上界） | 截断对紧度的影响 |
| `f_C`、`f_hard`、`f_prec` | 联盟、硬截止、有前驱任务的占比（实现值） | 结构约束 |
| `cap_scarcity` | `min_k (#{i: c_i,k = 1} / max(1, #{j: r_j,k = 1}))`（初始编制） | 能力稀缺度 |
| `cap_scarcity_post` | 同上，`c_i,k` 取全部实例内故障之后的能力（G-08 保证分子 ≥ 1） | 故障后能力稀缺度 |
| `u_feasible` | 满足 G-06 与 G-07 的任务价值之和 / `Σ_j v_j`；G-06 至 G-08 使 P0 全部档位恒为 1，作为生成器自检量写出 | 故障后可完成价值上界（实例层）；运行期 X1 扰动下的对应量为评测器诊断 `u_ub_cap`（§7.2） |
| `obstacle_cover` | 障碍面积 / 区域面积 | 绕行与遮挡 |
| `detour_ratio` | 任务对之间路径长度与欧氏距离之比的均值（抽样 256 对） | 代价非欧程度 |
| `partition_frac` | 分区时长之和 / H | 外生通信中断 |
| `fault_count` | 实例内故障数 | 鲁棒性 |

### 4.6 运行期外生随机量

| 量 | 随机域（AWR-06 §5.5） | 对象键 | `time_index` | 说明 |
|---|---|---|---|---|
| Poisson 到达时刻 | `task` | `ta-core/arrivals` | 到达序号 | 到达次序由实例固定，到达时刻按外生种子实现：非初始任务的到达时刻为 (0, H_arr] 上（`bursty` 为分段强度下）的次序统计量，依到达序号赋予；保证同一场景不同种子的任务池相同而时序不同 |
| 导航噪声 | `sensor` | `nav:<entity_id>` | `capture_tick` | M28 R-M28-59 |
| 信道与包级随机 | `channel`、`packet` | `link_id`、`message_id` | 固定网络时间、0 | M29 |
| X1 扰动 | `failure` | `entity_id` | 扰动事件序号 | 仅 `stress` 与 X1 运行 |

实例内故障与分区的时刻与对象由实例固定（M31 R-M31-31），在全部运行中出现，不随外生种子变化。S1 只有 `exogenous` 事件模式：任务不对智能体行为作出反应。

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 |
|---|---|---|---|---|
| R-S1-17 | 同一 `(instance, exogenous_seed)` 下，到达时刻、故障时刻、分区掩码与导航噪声在不同策略之间逐位一致，与策略的消息数量、承诺次序无关（RN-1、RN-2） | P0 | P0 | AC-S1-11 |

---

## 5. 难度与规模分级

### 5.1 难度档

难度档只由实例内在旋钮定义（M31 OR-4）。网络档与时钟档是运行坐标，见 §6。表中数值均为初值，pilot 按 AWR-06 PA-7 只调整生成器公开参数（任务到达率、截止期紧度、联盟比例、遮挡率即障碍覆盖率与布局、预算比），每个配置至多 3 轮；网络参数集不属于 S1 可调整的范围（R-S1-18）。

| 旋钮 | `d0` 教学 | `d1` 基础 | `d2` 标准 | `d3` 困难（P1，TA-C 单列榜） | `d4` 极限（P2） |
|---|---|---|---|---|---|
| 规模覆盖 | 仅 `n4`（`|T| = 8`） | `n4`–`n16`、`n20` | `n4`–`n16` | `n4`–`n32` | `n32`–`n64` |
| `ρ_T` | 2（固定 8 个任务） | 3 | 4 | 4 | 5 |
| 到达 | `static`（全部 t = 0） | `poisson` `steady`，`initial_frac = 0.25`，`H_arr = 240 s` | 同 `d1` | 同 `d1` 加 `bursty` | 同 `d3` |
| 能力 | 同构 K = 1 | 同构 K = 1 | 异构 K = 3 | 异构 K = 3，速度 {8, 12} m/s | 同 `d3` |
| 联盟任务 | 0 | 0 | `f_C = 0.25`，`m_j = 2`，`w_j = 3 s`，`s_j = 20 s` | `f_C = 0.40`，`m_j ∈ {2, 3}`，`w_j = 3 s` | 同 `d3` |
| 服务时长 | 10 s | U{10..30} s | U{10..30} s | U{10..30} s | 同 `d3` |
| 时间窗 | 无（软截止，`l_j = H`） | 软截止 | `f_hard = 0.5`，`κ ∈ [2, 6]` | `f_hard = 0.7`，`κ ∈ [1.5, 4]` | `f_hard = 0.7`，`κ ∈ [1.5, 3]` |
| 前置关系 | 无 | 无 | `f_prec = 0.15`，链长 ≤ 2 | `f_prec = 0.20`，链长 ≤ 3 | 同 `d3` |
| 障碍覆盖率 `o` | 0 | 0.05 | 0.10 | 0.15 | 0.15 |
| 实例内故障 | 无 | 无 | `t = 150 s` 一架 `payload_loss`（对象按 G-08 约束） | 1–2 架静默失效（`entity_fail`）、成员加入 1 架与计划退出 1 架（对象按 G-08 约束，§5.5） | 同 `d3` |
| 外生分区 | 无 | 无 | 1 次，60 s，均衡二分，起始 U{90..150} s | 2 次 | 同 `d3` |
| 机巢与能量 | 无 | 无 | 无 | 3 座机巢（每座 1 机位）、`energy_ratio = 0.7`（§5.5） | 同 `d3` |
| 目标 `load_W` 区间 | — | 0.3–0.5 | 0.4–0.7 | 0.6–0.9 | ≥ 0.9 |

故障语义：

| 故障 | 语义 | 可观测性 |
|---|---|---|
| `payload_loss` | 实体能力位在 `t_f` 全部清零，实体仍可飞行与通信，已持有的承诺保留；不能再成为任何任务的参与者（P-01(d)） | 控制该实体的 Agent 在下一次密封的 `self_state.capabilities` 中得知；其他 Agent 不可观测，只能经消息得知 |
| `entity_fail`（`d3`） | 实体静默失效：置 `FAILED`，停止运动与通信，不发送任何告知 | 其他 Agent 只能经消息缺失推断 |
| `membership`（`d3`） | 实体加入（新出生 id，单调编号，I-06，由待命 Agent 接管，R-S1-01）或计划退出（置 `RETIRED`） | 加入在加入时刻、计划退出提前 `retire_notice_s = 10 s`（初值）经公告板公开 |

风场不列入 `d3`：`planar@1` 无风场模型，在 §15.1 第 15 条裁定前不进入任何已开放档位。

### 5.2 规模档与恒密度

| 规模 | `L_n`（m） | `|T|`（`d1` / `d2`） | `B_cap`（团队，300 s，4 KiB/s/实体） | 宿主预算（AWR-06 §5.11） | 开放 |
|---|---|---|---|---|---|
| `n4` | 424 | 12 / 16 | 4.9 MB | ≤ 20 s | P0 |
| `n8` | 600 | 24 / 32 | 9.8 MB | ≤ 45 s（PA-6） | P0 |
| `n16` | 849 | 48 / 64 | 19.7 MB | ≤ 90 s | P0 |
| `n20`（北极星） | 949 | 60 / — | 24.6 MB | 北极星分项预算（§5.3） | P0 |
| `n32` | 1200 | 96 / 128 | 39.3 MB | ≤ 240 s | P1（WP-D4-51） |
| `n64` | 1697 | 192 / 256 | 78.6 MB | ≤ 600 s | P2（WP-D4-52） |

任务数随 n 同比放大，区域边长按 √n 放大，保持任务密度与智能体密度不变（AWR-06 §7.0）。观测与收件箱上限（PB-5）：`Tmax = 128`（公告板任务项，按 `task_id` 升序截断，P0 档位不触发截断）、`inbox_max = 256`；`n32` 起按实测重定。

### 5.3 北极星配置

| 项 | 取值（固化于 `tests/perf/northstar/config.yaml`，AWR-06 §5.10） |
|---|---|
| 实例 | `capl:ta-core/ns-00000` 至 `ns-00009/d1/n20@1`，10 个 `dev` 实例 |
| 种子 | 外生种子 0–9，10 × 10 = 100 个配对单元 |
| 算法 | `static_partition`、`nearest_greedy`、`cbba`、`NearestTask`（AWR-06 §4.2 示例） |
| 运行坐标 | `closed_loop`、`d4_quick`、`planar@1`、`abstract-disk@1`（`net/abstract-disk-default@1`，标签 `synthetic-stress`）、`L1`、trace `minimal` |
| 产物 | 带 95% CI 的指标表、相对 `π_ind` 的配对差、可回放 trace、报告 |

北极星配置不是排名配置：`d4_quick` 与 `abstract-disk@1` 均禁止排名（AWR-06 §4.4、§8.3）。

### 5.4 档位开放与版本

档位以 `{id, status, opened_in}` 登记于 Suite 清单（M31 第 8 条反馈的处置）；开放 `d3`、`n32`（P1）与 `d4`、`n64`（P2）为 Suite 次版本，产生新 Board Version，不改变已开放档位的实例与分量。

### 5.5 TA-C 层的机巢、能量与成员变化规则（`d3` 起，P1）

| 编号 | 规则 |
|---|---|
| R-S1-29 | 能量参照：巡航功率 `P_cruise = P0 + P1 · v_max`（M28 R-M28-55 的能量模型在 `‖v‖ = v_max` 处取值，`P0`、`P1` 为按实体种类登记的属性，待 S1 pilot 校准）；初始能量 `energy_wh = energy_ratio · H · P_cruise / 3600`，`energy_ratio = 0.7` 即满能量可支撑 0.7 H 的连续巡航；悬停与服务期间按同一模型以实际速度扣减 |
| R-S1-30 | 停靠协议：实体距机巢中心 ≤ `r_dock = 5 m`（初值）时，其控制 Agent 可提交 `nest.dock.req{nest_id}`，仲裁按 M29 R-M29-72（机位空闲且供电时 `GRANTED`，机位满 `DENIED_BUSY`，超距 `DENIED_DISTANCE`）；机巢表与停靠半径由 S1 的 `ArbiterRules` 登记（M29 R-M29-76、M31 R-M31-95）。停靠期间实体运动指令被拒（回执 `docked`），不能成为任何任务的参与者（P-01(a) 以 `ACTIVE` 且未停靠计）；`nest.undock.rel` 释放机位后恢复运动。机位争用不排队，被拒者须重试或改去其他机巢 |
| R-S1-31 | 充电模型：停靠且机巢供电时以 `P_charge = 4 · P_cruise`（初值）充电至初始能量上限，充满后保持；断电（X1 H 族）期间充电速率为 0（M29 R-M29-72）。充电在内核能量积分中实现，需 M28 增补充电项（§15.2） |
| R-S1-32 | 耗尽：`energy_uwh ≤ 0` 的实体置 `DEPLETED`（M28 R-M28-55），在本 Episode 内不可恢复，其持有的承诺不自动失效（与 `entity_fail` 同为被测对象），计入 `depleted_count` |
| R-S1-33 | 成员变化下的资源包：`B_cap = 4096 B/s × Σ_e presence_s(e)`，`presence_s(e)` 为任务实体 e 依实例计划在场的时长（初始实体自 0 起，加入实体自加入时刻起，计划退出实体至退出时刻止；`entity_fail` 不缩短），以 M28 ledger `limit_expr` 的实例公开变量表达（R-M28-109）；`d0`–`d2` 时等于 `n · H · 4096`。CPU 额度按 §6.4，待命 Agent 计入 N 与团队额度，其 CPU 自 Episode 开始计入账本。`U_TA` 的分母不变；`jain_fair` 的 `x_i` 遍历在 Episode 内控制过任务实体的全部 Agent |
| R-S1-34 | 可见性：机巢位置与机位数在 `brief.json` 公开；机位占用状态不公开，只能经停靠回执与消息获得；成员加入与计划退出按 §5.1 经公告板公开 |

上述参数（`r_dock`、`P_charge`、`retire_notice_s`、`P0`、`P1`）均为初值，于 WP-D4-26 的 TA-C pilot 中校准后随 `d3` 档位开放一并冻结。

---

## 6. 运行模式与通信档组合

### 6.1 运行模式与场景集

| 场景集 id | 划分 | 运行模式 | 档位 | 场景数 × 外生种子 | 角色 |
|---|---|---|---|---|---|
| `ta-core.dev.cl`、`ta-core.dev.ai` | `dev` | 闭环 / 算法隔离 | `d0`–`d2` × `n4`–`n16` | 每档 ≥ 20 实例 × 公开种子 0–9 | 开发 |
| `ta-core.dev.smoke` | `dev` | 闭环 | `d1/n8`、`d2/n8` | 2 × 3 | 冒烟与 CI（M31 OR-8） |
| `ta-core.anchor` | `dev` | 闭环与算法隔离 | `d0/n4`（区块 `pu`、`pc`；P1 起加联盟锚点区块 `ac`） | 每区块 2 个锚点 × 1 | 评测器单元测试与黄金回归（M31 OR-7） |
| `ta-core.validation.cl`、`.ai` | `validation` | 闭环 / 算法隔离 | `d1`、`d2` × `n8`、`n16` | 按 §9.2 | 聚合反馈 |
| `ta-core.holdout.cl`、`.ai` | `holdout` | 闭环 / 算法隔离 | `d1`、`d2` × `n8`、`n16`（P1 起加 `n32`） | 按 §9.2 | 成榜 |
| `tac.holdout.cl`（P1） | `holdout` | 闭环 | `d3` × `n8`、`n16` | 按 §9.2 | TA-C 单列榜 |
| `ta-core.stress`、`tac.stress`（P1） | `stress` | 闭环 | 宿主 `holdout` 子集（族的分配见 §6.6） | 6 × 3 × 5 强度（X1） | X1 |

闭环与算法隔离为不同场景集，分别成榜（ADR-D4-17）；`d0` 只用于锚点与教学，不成榜。

### 6.2 算法隔离执行器 `ta-exec@1`

| 编号 | 规则 |
|---|---|
| X-01 | 求解器宿主 `solver_host`（M30 R-M30-19）：`local`（每个任务 Agent 以本地视图求解，只作用于自身，可选 `plan_share`）、`planner`（规划 Agent 绑定地面站 `gs_00001` 的 `edge_node` 实体作为通信端点，经交互网络收集 `common.solver.state_report.v1` 并以 `common.solver.plan_push.v1` 单播计划，全部计费）、`global`（仅 `cr-*` 赛道）。分区归属：排名运行中规划 Agent 不属于任一分区组，分区期间与两组均断开（`station_side = isolated`，与 X1 §5.5 对无实体与固定站址端点的口径一致），模拟地面回传链路与机群同时中断的情形；该约定使 `planner` 宿主在每次分区期间完全失去协调能力，属对宿主选择的已知系统性影响，写入 `validity.md` 与相关基线卡，E-05 的机制分析单列该项。研究运行可设 Suite 属性 `/suite/taskalloc/station_side = joined`（规划 Agent 归入规模为 ⌈n/2⌉ 的第二组），作为分离"算法降幅"与"宿主隔离降幅"的对照条件，不排名 |
| X-02 | 重规划节拍：周期 `replan_period_s = 10`（初值）；事件触发为任务到达、公告板状态变化、本机能力变化与 Assignment 变化；求解只在决策 tick 上发生（M30 R-M30-22；清单 `replan{period_s, triggers[]}`，M31 R-M31-96） |
| X-03 | 计划步 `{task, t_start_s?}`：执行器只对每个 Agent 的当前步（首个未完成计划步）发出 `task.claim`（若未持有），不预先承诺后续计划步（承诺对他人不可见、不带来协调收益，P-10；预承诺只会使计划变更表现为无效承诺）；随后发出 `goto_w(x_j)`（`cost_model = planar_obstacle` 时为可视图最短路的 `follow_path`），进入服务圆后保持对 `x_j` 的 `goto_w` 直至完成，实体按 R-M28-52 的减速曲线停于距中心 `r_arrive` 以内；当前步因计划变更被替换时释放其承诺；完成或任务关闭后释放承诺并转下一步 |
| X-04 | 同步等待：联盟任务的计划步带 `t_start_s` 时，执行器使实体在半径 `r_wait = r_srv + 10 m` 的等待环上距目标最近处保持，并在 `t_start_s − t_tr(r_wait)` 之后的首个决策 tick 由环上驶向中心；未带 `t_start_s` 的联盟任务按汇合约定 `rv-slot@1`（R-S1-28）执行，不再"到达即进入" |
| X-05 | 计划无效（引用未知或已关闭任务、能力不满足）时整份拒绝，继续执行上一份有效计划（M30 R-M30-21）；执行器行为只由计划内容与仿真状态决定，与墙钟无关 |
| X-06 | 执行器协议由 M31 规定（`SuiteRuntime.executor()` 返回 `PlanExecutor`，R-M31-96、§7.2.6），S1 的实现 `ta-exec@1` 由本 Suite 提供（属 WP-D4-14，M31 §9.4 E-08 将其列为各 Suite 规格的工作量），全部求解器共用，版本冻结于 Suite 清单；执行器语义变化为 Suite 主版本。M30 R-M30-22 与 §6.4 写作"执行器（M31 提供）"，与 M31 的划分不一致，提请统一（§15.2） |

`ProblemView.body`（`drone4d.suite.taskalloc.view.v1`）与 `Plan.body`（`drone4d.suite.taskalloc.plan.v1`）的字段见 §11.5。

### 6.3 通信档组合

网络档为赛道坐标（AWR-06 §8.3），不进入实例与难度档。S1 允许的组合如下：

| 网络档（参数集） | 时钟档 | 用途 | 赛道 / 场景 | 可排名 | 标签 |
|---|---|---|---|---|---|
| `none@1` | `d4_std` | `π_ind` 的运行与 `Δ_ind` 参考 | 参考策略缓存 | 否（参考） | — |
| `ideal@1` | `d4_std` | 对照榜；锚点实例；"通信约束代价"诊断 `U(ideal) − U(radio)` | `dc-l1-b1-ideal`、`ta-core.anchor` | 对照榜（不与主榜混排） | — |
| `ideal-unlimited@1` | `d4_std` | 上界诊断 | 研究运行 | 否 | — |
| `abstract-disk@1`（`default`） | `d4_quick` | 北极星、教学、本地快速迭代、MARL 训练 | `dev` 本地 | 否 | `synthetic-stress` |
| `abstract-radio@1`（`default`） | `d4_std` | 主榜；集中式参考列 | `dc-l1-b1-radio`（主）、`cr-l1-b1-radio`（参考列） | 是 | `synthetic-stress` |
| `abstract-radio@1`（`severe`） | `d4_std` | 网络档对照（E3）、负协作区域（E8、方向 A） | 研究赛道（P2，`dc-l1-b1-radio-severe`，见 §15 第 5 条） | 否（P2 前） | `synthetic-stress` |
| `ns3@1` | `d4_std` | 校准与细粒度通信研究 | 研究赛道（P2） | 否 | — |
| `fspl-nlos@1` | — | 不适用（仅 `drone4d-event@1`） | — | — | — |

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 |
|---|---|---|---|---|
| R-S1-18 | 平面世界中障碍阻挡视线（`blocks_los = true`），`abstract-radio@1` 以障碍求交判定 NLoS（M28 R-M28-50）；NLoS 苛刻度在 pilot 中确认（`default` 档 NLoS 约 52 m 断链，AWR-06 §6.5）。若使 `d2` 的协作基线全部退化为零通信水平，S1 只按 PA-7 调整实例旋钮：先降低 `d2` 的障碍覆盖率 `o`（下限 0.05）或改用通道更宽的布局，结果记录于 pilot 报告。网络参数集（含 AWR-06 §6.5 所述 `A_NLoS`、`n_NLoS`）是全部 Suite 共享的赛道坐标，S1 不单方面调整；确需调整时由 WP-D4-17 汇总三个 Suite 的 pilot 证据提请负责人裁定，按网络参数集版本变更处理（§15.1 第 20 条） | P0 | P0 | AC-S1-03 |
| R-S1-19 | 拓扑可见性 `topology_hint` 缺省关闭；开启为不同赛道条件，P0 不开放 | P0 | P0 | — |
| R-S1-20 | 干预：`Δ_cut`（P0）与 `Δ_perm`（P1）适用于 S1 全部场景集；`perm_struct` 只置换 schema 中标注 `x-awr-role: world_state` 的字段（S1 平台基线消息中为 `pos`、`energy_wh`、`status`，M30 §6.5.3），保留出价、胜者表、迭代号等控制字段；承诺不是消息，不受干预影响（P-10） | P0 | P0/P1 | AC-S1-10 |

### 6.4 资源包与预算

| 维 | 额度（B1） | 执行方式 | 说明 |
|---|---|---|---|
| `tx_bytes`（团队） | `B_cap = n · H · 4096 B`，n 只计任务实体，不计地面站与规划 Agent（`d3` 成员变化下按 R-S1-33） | 提交时拒绝：超额消息返回 `TxReceipt.reason = budget`，不记违规（AWR-06 §6.3 `ideal@1` 的丢弃规则同此） | 含头部、分片、转发；广播展开为 n−1 条计费单播（ADR-D4-14） |
| `cpu_ms` | 每个任务 Agent `H · 100 CPU-ms`；团队额度 `CPU_cap = N · H · 100 CPU-ms`，N 为任务 Agent 数（含 `d3` 待命 Agent，不含规划 Agent）；规划 Agent（`planner` 宿主）与集中式决策主体（`π_central` 及 `cr-*` 条目）不设单独额度，其用量计入并受团队额度约束，即 `π_central` 与分布式条目使用同一团队额度 | 超额（任一任务 Agent 超出其额度，或团队合计超出 `CPU_cap`）记违规，按 `budget.policy = violation_invalidates` 处置（`ranked_valid = false`，按 `invalid_policy = zero` 方向化计分：`U_TA = 0`，结果类指标置不利端，资源分量保留实测，M32 R-M32-36、R-M32-38） | 容器 cgroup 实测（W-4） |
| `tokens` | 0 | 提交时拒绝 | B-LLM 档（P2）另行定义 |
| `energy_wh` | `d3` 起按 `energy_ratio` | 物理语义（`DEPLETED`），不是预算违规 | — |

字节额度按"拒绝"而非"违规"执行的理由：字节使用由策略主动决定且可被内核在提交时精确判定，拒绝使超额行为的后果落在策略自身（消息丢失）而不是整局清零，与 `TxReceipt` 的 `budget` 原因码一致；CPU 超额无法在调用前判定，只能事后记违规。该处置提请 AWR-06 明确（§15 第 7 条）。

### 6.5 赛道与时钟档

| 赛道 | 拓扑 | 观测 | 预算 | 网络 | 时钟 | 角色 |
|---|---|---|---|---|---|---|
| `dc-l1-b1-radio` | decentralized | L1 | B1 | `abstract-radio@1 default` | `d4_std` | 主榜 |
| `dc-l1-b1-ideal` | decentralized | L1 | B1 | `ideal@1` | `d4_std` | 对照榜 |
| `cr-l1-b1-radio` | centralized | L1 | B1（团队总额） | `abstract-radio@1 default` | `d4_std` | 集中式参考列，不参与分布式排名 |
| `tm-l1-b1-radio`（P1） | decentralized | L1 | B1 | `abstract-radio@1 default` | `d4_std`，`timing = measured` | 研究赛道，不排名 |
| `dc-l1-bllm-radio`（P2） | decentralized | L1 | B-LLM | `abstract-radio@1 default` | `d4_std`，Δd 1–5 s | B-LLM 赛道 |

`ranked_physics = [planar@1]`（P1 起视 FC 结果加 `kinematic`）；排名运行只使用 `d4_std`。集中式参考列中决策主体持有全部 Assignment，不经网络传递指令，其结果对网络档不敏感（AC-S1-10 的推论）。

### 6.6 X1 接入声明（P1）

本节给出 X1 §2.2 要求宿主规格提供的内容；扰动族语义、强度、`T_detect` 与 `T_recover` 的计算以 X1 为准。

| 项 | S1 的声明 |
|---|---|
| 适用族 | F、C、P、L、J、H（P1，H 仅含机巢的 `d3` 宿主）与 M（P2）；S 不适用（X1 §2.3） |
| 宿主与族的分配（与实例内扰动互斥） | `ta-core.stress`（宿主为 `ta-core` 的 `holdout` 子集，`d1`、`d2`）承载 F、C、P、L、J 与 M 族；`tac.stress`（宿主为 TA-C 的 `holdout` 子集，`d3`）只承载 H、P、L、J 族。`d3` 的实例内 `entity_fail` 与 `membership` 与 X1 的 F、M 族语义相近，为避免同一实体类扰动叠加且使 X1 的 `k(q, n)` 与退化曲线可解释，F、C、M 族不施加于 `d3` 宿主 |
| 受扰对象候选 | `perturbation.json.host.candidates` 排除实例内故障对象（`d2` 的 `payload_loss` 对象），使实体族扰动不作用于已受实例内故障影响的实体；R-RC-20 的 `noop_instance_prior` 规则作为兜底 |
| 分区族 | X1 P 族按 `pos0` 投影二分（X1 §5.5）；规划 Agent 不入组，与 X-01 的排名口径一致；与实例内分区的掩码按逐边逻辑与叠加（R-RC-19） |
| `U_cum(t)` | `U_cum(t) = Σ_{j : t_book,j ≤ t} v_j · q_j · ψ_j · δ_j / Σ_j v_j`，入账时刻 `t_book,j`：完成任务为 `τ_j`，未完成且 `q_j > 0` 的任务为其关闭时刻 `l_j`；`q_j = 0` 的任务不入账。离散型，事件为任务完成（及部分得分任务的关闭）。评测器以 `(t_book,j, task_id)` 升序累加计算 `U_TA`，即 `U_TA ≡ U_cum(H)`，二者逐位一致（X1 R-RC-46） |
| `Held(o)` | X1 §8.2 的 S1 口径：`t_o` 时刻受扰实体的控制 Agent 持有有效承诺且仍为 `open` 的任务；H 族为在 `[t_o, t_o + 60 s)` 内对受扰机巢提交过被拒停靠请求的 Agent 集合。`realloc_latency_s` 的受影响任务集合 `A_f`（§7.2）是 `Held(o)` 中失去覆盖的子集 |
| 响应操作 `x1.response_ops` | 实体族：行动者对 `Held(o)` 中任务的 `task.claim`（结果为"已登记"，§2.4）；M 族另含 `a_o` 对 `Held(o)` 中任务的 `task.release` 与 `commit.revoke`；H 族：目标为非受扰机巢的 `nest.dock.req`，且发起者此前对受扰机巢有被拒请求。路径改变属运动结果，不作为响应操作（X1 §8.2） |
| 提前终止 | S1 只有 `horizon` 终止，`x1.early_stop_invariant = true` |
| `π_ind`（`static_partition`）在扰动下的行为 | 零消息，不感知队友故障：F、C 下受扰实体所在剖分区的任务无人接管；M 族的替补实体在离开者的 `pos0` 加入，继承其剖分区；P、L、J 不影响；H 族下就近停靠，被拒后改去次近机巢 |
| `π_central`（`rolling_milp`）在扰动下的行为 | 只在 F、M、H 族下运行，C、P、L、J 记 `n/a`（X1 §9）。F：集中决策主体经受扰实体的指令回执 `entity_inactive` 得知失效（M28 R-M28-46(a)），于下一次重规划将其移出可用集合；M：经 `assignment.change` 得知预告，替补实体加入后由集中决策主体接管（R-S1-01）；H：经停靠回执得知断电并改选机巢 |
| 运行期可完成上界 | X1 实体族扰动可使部分任务在扰动后不可满足能力覆盖；评测器逐 Episode 报告诊断量 `u_ub_cap`（§7.2），PA-4 与 X1 的退化曲线解读以其为参照 |

---

## 7. 指标与公式

### 7.1 主指标 `U_TA`

```
U_TA = Σ_{j∈T} v_j · q_j · ψ_j · δ_j  /  Σ_{j∈T} v_j

q_j  = 1                         若 j 按 P-07 完成且 τ_j ≤ l_j
     = partial_credit · σ_j/s_j  否则；σ_j 为 j 在关闭前单次连续服务的最大累计时长（P-07 意义下）
ψ_j  = 1                         若 m_j = 1
     = max(0, 1 − Δt_sync,j / w_j)  若 m_j ≥ 2（完成时取计入成员的极差；未完成时取最大累计服务段的极差）
δ_j  = 1[τ_j ≤ l_j] · exp(−λ_d · max(0, τ^s_j − t0_j))   完成时（τ^s_j 为完成所在服务段的开始时刻，P-04）
     = exp(−λ_d · max(0, t^s_σ,j − t0_j))                  未完成且 q_j > 0 时，t^s_σ,j 为最大累计服务段的开始时刻
t0_j = max(t^a_j, e_j)
```

| 常量 | 初值 | 冻结 | 说明 |
|---|---|---|---|
| `λ_d` | 0.01 s⁻¹ | pilot 后、Suite v1.0 发布前 | 与 `cbba` 路径得分的折扣率一致（M30 §6.7.1） |
| `partial_credit` | 0 | 同上 | 缺省不给部分得分：避免"开始而不完成"的激励，并保持 `d0` 穷举最优的精确性；pilot 若出现 PA-4 下限饱和，可在冻结前改为 ≤ 0.5 的常量。改动 `U_TA` 定义属 Suite 主版本 |
| `r_srv` | 5 m | 同上 | 服务半径 |

性质：`U_TA ∈ [0, 1]`；分母为实例全部任务（包括因前驱失败而不可完成的任务）；G-06 至 G-08 保证每个任务在任意到达实现与全部实例内故障之后仍能由初始编制完成（能力覆盖、时间窗与前置可行，§4.3），因此分母中不存在实例层面结构性不可完成的任务（`difficulty.u_feasible = 1`）；X1 扰动造成的运行期不可完成由 `u_ub_cap` 报告（§7.2）。无效 Episode 的 `U_TA = 0`（AWR-06 §8.6）。

时效折扣以 `t0_j` 而非到达时刻为起点，使开窗前的必然等待不受惩罚；以服务开始时刻 `τ^s_j` 而非完成时刻为终点，使服务时长不构成任何策略都无法避免的常数损失——以完成时刻为终点时，`λ_d = 0.01`、`s_j ∈ [10, 30] s` 下即使到达即开始服务也有 `δ_j ≤ 0.74…0.90`，`U_TA` 的可达区间被结构性压缩，PA-4 的上饱和检验无实际意义。该口径与平台 `py:swarm/allocation/score.py` `path_score` 一致（`t_start = max(t_open, 到达时刻)`，折扣 `exp(−λ · (t_start − t_open))`），基线以 `t_open = t0_j`、原始价值 `v_j` 调用即与 `U_TA` 的目标一致（R-S1-25）。本定义与 AWR-06 §7.1 的 `d_j`（以完成时刻为终点）不同，见 §15.1 第 1 条。

### 7.2 任务辅助指标

| id | 定义 | 方向 | 可空 |
|---|---|---|---|
| `cr` | 完成率 `|T_done| / |T|`（`T_done` 为按 P-07 完成且 `τ_j ≤ l_j` 的任务；含 `ψ_j = 0` 的完成） | max | 否 |
| `vr` | 硬截止违约率 `|{j ∈ T_hard : j ∉ T_done}| / |T_hard|` | min | `T_hard = ∅` 时为空 |
| `cr_coal` | 联盟任务完成率 `|T_done ∩ T_C| / |T_C|`，`T_C` 为联盟任务集合 | max | `T_C = ∅` 时为空 |
| `sync_q` | 已完成联盟任务的 `ψ_j` 均值 | max | 无联盟任务完成时为空 |
| `kappa_eff_mean` | 硬截止任务运行期有效紧度 `(l_j − t0_j − s_j) / max(t_reach_j, 20 s)` 的均值（`l_j` 为 R-S1-07 截断后的值） | 诊断 | `T_hard = ∅` 时为空 |
| `u_ub_cap` | 运行期可完成价值上界：`Σ_{j 可满足} v_j / Σ_j v_j`，j 可满足当且仅当存在 `m_j` 个实体，在 `[t0_j, l_j − s_j]` 内某一时刻均为 `ACTIVE` 且能力覆盖 `r_j`，并保持至该时刻后 `s_j`；只计实体状态与能力，不计可达性 | 诊断 | 否 |
| `resp_mean_s` | 平均响应时间 `mean_{j∈T_done}(τ_j − t0_j)` | min | `T_done = ∅` 时为空 |
| `j_max_s` | 工期 `max_{j∈T_done} τ_j` | min | 同上 |
| `j_sum_m` | 全部任务实体总路程 | min | 否 |
| `realloc_latency_s` | 对实例内故障 f（`payload_loss` 或 `entity_fail`，发生于 `t_f`，对象实体由 Agent i 控制）。记 `C_j(t)` 为 t 时刻 j 的有效承诺方中实体 `ACTIVE` 且 `c_k(t) ∩ r_j ≠ ∅` 的 Agent 集合；`C_j(t)` 覆盖 j 当且仅当存在 `S ⊆ C_j(t)`，`|S| = m_j` 且 `∪_{k∈S} c_k(t) ⊇ r_j`。受影响任务 `A_f` = 在 `t_f` 之前一刻 i 持有承诺、`t_f` 时仍为 `open`、且 `C_j(t_f)` 不覆盖 j 的任务（只含因 f 失去覆盖或从未覆盖、且 i 为其承诺方的任务）。每个 `j ∈ A_f` 的延迟 = 自 `t_f` 至首个时刻 t，t 时刻有一项在 `t_f` 之后授予的承诺使 `C_j(t)` 覆盖 j；至 `l_j` 仍未发生者以截尾值 `l_j − t_f` 计入；取全部 `(f, j)` 的均值。联盟搭档或冗余承诺方在 `t_f` 前已持有的承诺不构成重分配 | min | 无故障或 `A_f` 全空时为空 |
| `realloc_censored` | 上项中截尾的占比 | min | 同上 |
| `reassign` | `Σ_t |{j : owner_t(j) ≠ ∅，owner_{t−Δd}(j) ≠ ∅，owner_{t−Δd}(j) ⊄ owner_t(j)}|`，`owner_t(j)` 为决策 tick t 时 j 的有效承诺方集合（r02 §3.1）：只计"任务仍有承诺方、但原承诺方中有人退出"的变化；首次承诺（∅ → 非空）、联盟成员的单纯增补与任务关闭导致的承诺失效（非空 → ∅）不计 | min | 否 |
| `claim_first` | 首次承诺次数：`Σ_t |{j : owner_{t−Δd}(j) = ∅，owner_t(j) ≠ ∅}|` | 诊断 | 否 |
| `conflict_rate` | `Σ_j ∫ 1[|owner_t(j)| > m_j] dt / Σ_j ∫ 1[|owner_t(j)| ≥ 1] dt`（按决策 tick 离散） | min | 无承诺时为空 |
| `dup_exec_rate` | 服务期间出现冗余参与者（P-09）的任务数 / 进入服务的任务数 | min | 无任务进入服务时为空 |
| `invalid_claim_rate` | 无效承诺数 / 承诺数；承诺 c（i 于 `t_c` 承诺 j）无效，当且仅当在 `[t_c, min(t_release, 关闭时刻, H)]` 内 i 从未成为 j 的参与者。算法隔离模式下执行器只承诺当前计划步（X-03），故该指标度量求解器计划的不稳定性而非执行器行为；闭环模式下度量策略自身的承诺纪律（平台基线只承诺当前目标，§8.1） | min | 无承诺时为空 |
| `energy_wh` | 全部任务实体能耗之和 | min | 否 |
| `jain_fair` | Jain 指数 `(Σ x_i)² / (n Σ x_i²)`，`x_i = Σ_{j∈T_done, i∈S*_j} v_j q_j ψ_j δ_j / m_j`，i 遍历 Episode 内控制过任务实体的 Agent（Jain 等, DEC-TR-301, 1984 [待核实]） | 诊断（登记为 `max`） | 全部 `x_i = 0` 时为空 |

### 7.3 资源指标

| id | 定义 | 方向 | 进入支配分层 |
|---|---|---|---|
| `b_tx_ratio` | `B_tx / B_cap`，`B_tx` 为团队无线发送字节（含头部、分片、转发），被 `budget` 拒绝的消息不计入 | min | 是 |
| `cpu_ratio` | `CPU_ms / CPU_cap`（团队） | min | 是 |
| `bytes_per_task` | `B_tx / |T_done|` | min | 否（`T_done = ∅` 时为空） |
| `bytes_by_class` | 按 `info_class` 分解的 `B_tx` | 诊断 | 否 |
| `budget_refusals` | 被 `budget` 拒绝的提交次数 | 诊断 | 否 |

### 7.4 安全指标

| id | 定义 | 初值阈值与处置 |
|---|---|---|
| `contact_count` | `entity.contact` 事件数（障碍与边界接触，M28 R-M28-54） | Episode 级 > 20 次标记 `safety_flag`；条目在 > 10% 的 Episode 上被标记时，条目卡标注"安全降级"，处置规则由排行榜规格定义（不剔除、不改变 `U`） |
| `depleted_count`（`d3`） | 能量耗尽实体数 | 同上，阈值 1 |
| `fail_rate` | `failure.policy` 的 Episode 占比 | 条目卡展示 |

S1 不评测机间避碰（R-S1-02）；机间最小水平间距只作诊断，不设阈值。

### 7.5 TA-C 层指标（P1）

| id | 定义 |
|---|---|
| `reorg` | 联盟重组次数：对每个联盟任务，自其承诺方集合首次覆盖 `r_j` 起，承诺方集合每变化一次计 1，全部联盟任务求和 |
| `coal_conn` | 联盟内通信连通率：联盟任务的承诺方集合在拓扑快照 `connected_ij`（AWR-06 §6.5 第 8 项）下连通的时间占比，按任务–时间加权 |
| `opt_ratio` | 相对最优比 `U_TA / J*`，只在带 `J_star` 的 `d0` 锚点实例（区块 `pu`、`pc`、`ac`）上计算 |
| `coal_size_mean` | 已完成联盟任务的计入成员数均值 |

### 7.6 Δ 族与子编队投影

| 量 | S1 的适用 |
|---|---|
| `Δ_ind` | `π_ind = static_partition@1` 在 `none@1` 下运行（同资源包），由平台预计算缓存（WP-D4-13） |
| `Δ_cut` | 适用于全部场景集；承诺与公告板不受阻断（P-10、P-11），故 `Δ_cut` 只度量对交互网络消息的依赖 |
| `Δ_perm`（P1） | `perm_full` 与 `perm_struct`（R-S1-20）；供体规则按 AWR-06 §8.5 |
| `R_central`、`G` | `π_central = rolling_milp@1`，在 `cr-l1-b1-radio` 下运行并缓存；`G` 的分母 CI 下界 < 0.05 时记 `n/a` |
| 子编队投影 | `Evaluator.evaluate(..., subset = S)`：`U_TA^(S) = Σ_{j∈T_done, S*_j ⊆ S} v_j q_j ψ_j δ_j / Σ_{j∈T} v_j`（分母不变）。对智能体集合的任一划分 `{S_1, …, S_k}`，`Σ_k U_TA^(S_k) ≤ U_TA`，当没有已完成联盟任务的计入成员跨越不同部分时取等号；`π_ind` 的零通信运行据此做一致性检查（AC-S1-13） |

### 7.7 `MetricSpec` 登记

| id | kind | direction | range | primary | tier | aggregation |
|---|---|---|---|---|---|---|
| `u_ta` | task | max | [0, 1] | 是 | 是 | mean |
| `cr`、`cr_coal`、`sync_q` | task | max | [0, 1] | 否 | 否 | mean |
| `vr`、`conflict_rate`、`dup_exec_rate`、`invalid_claim_rate`、`realloc_censored` | task | min | [0, 1] | 否 | 否 | mean |
| `resp_mean_s`、`j_max_s`、`realloc_latency_s` | task | min | [0, null] | 否 | 否 | mean |
| `j_sum_m`、`energy_wh`、`reassign` | task | min | [0, null] | 否 | 否 | mean |
| `jain_fair`、`u_ub_cap` | task | max | [0, 1] | 否 | 否 | mean |
| `claim_first` | task | min | [0, null] | 否 | 否 | mean |
| `kappa_eff_mean` | task | max | [0, null] | 否 | 否 | mean |
| `b_tx_ratio`、`cpu_ratio` | resource | min | [0, null] | 否 | 是 | mean |
| `bytes_per_task` | resource | min | [0, null] | 否 | 否 | mean |
| `contact_count`、`depleted_count` | safety | min | [0, null] | 否 | 否 | mean |
| `fail_rate` | safety | min | [0, 1] | 否 | 否 | fail_rate |
| `reorg`（P1） | coop | min | [0, null] | 否 | 否 | mean |
| `coal_conn`、`opt_ratio`（P1） | coop | max | [0, 1] | 否 | 否 | mean |

可空性（AWR-06 v0.3 XR-14，M32 §13.2 第 14 项）：`resp_mean_s`、`j_max_s`、`realloc_latency_s`、`bytes_per_task` 在 `T_done = ∅` 时无定义，登记为 `nullable = true`；上述指标与 `j_sum_m`、`energy_wh`、`reassign` 均不进入 `primary` 与 `tier`，策略原因失败与无效运行下按 M32 R-M32-36 的方向化规则处理（结果类置空或置不利端，消耗类保留实测）。

诊断量（`jain_fair`、`u_ub_cap`、`claim_first`、`kappa_eff_mean`）的 `direction` 只为满足 M32 R-M32-01 的必填字段而登记，不使用 `target`（`target` 须带目标区间，诊断量没有规范的目标区间），不进入 `primary` 与 `tier`。

评测器 `taskalloc@1` 所需真值通道：`tasks`、`interaction`、`net`、`agent_state`、`ledger`（通道词表以 CT §5.1.2 与 M33 R-M33-69 的 9 个通道为准，AWR-06 v0.3 XR-23）。

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 |
|---|---|---|---|---|
| R-S1-21 | 评测器只读声明的通道，不读 Agent 自报的 `compute_units`、Belief 或计划作为完成依据 | P0 | P0 | AC-S1-16 |
| R-S1-22 | 可空指标按 `nullable = true` 登记；空值不以 0 代入均值，聚合按 M32 的可空规则处理 | P0 | P0 | AC-S1-16 |

---

## 8. 基线与参考策略

### 8.1 基线清单

基线标识、算法出处、关键参数、实现来源与分发包以 M30 §6.7.1、§6.7.2 为准；本表只列 S1 视角的角色与约束。

| 标识（`baseline:taskalloc/…`） | 角色 | 出处（核实状态） | S1 适用档位 | 运行模式 | 分发 | S1 约束 |
|---|---|---|---|---|---|---|
| `static_partition@1` | `π_ind` | 平台定义（初始位置 Voronoi 剖分 + 本区最近任务；联盟任务按公开约定取能力满足者中初始距离最近的 `m_j` 个） | 全部 | 闭环 | 核心 | 零消息；联盟任务按 `rv-slot@1` 汇合（R-S1-28，零消息） |
| `nearest_greedy@1` | 分布式基线 | 通用启发式 | 全部 | 闭环 | 核心 | 广播已承诺集，冲突按 `(距离, agent_id)` 裁决；`refresh_s = 5`；联盟任务按 `rv-slot@1` 汇合 |
| `hungarian_periodic@1` | 集中式基线（`planner` 宿主于 `dc-*`） | Kuhn 1955；Munkres 1957 [待核实] | 全部 | 算法隔离 | `ref`（scipy `_lsap`，`py:swarm/allocation/hungarian.py` `_lsa`） | 联盟任务复制 `m_j` 列，计划步不带 `t_start_s`，同步由执行器按 `rv-slot@1` 完成（X-04）；`period_s = 10`；`planner` 宿主在分区期间完全隔离（X-01） |
| `ssi@1` | 分布式基线 | Koenig, Tovey, Lagoudakis 等, AAAI 2006, pp.1625–1629 [已核，据评审检索] | 全部 | 闭环 | 核心 | 出价复用 `py:swarm/allocation/score.py` `marginal_bid`；联盟任务按单项拍卖逐成员出价，中标成员按 `rv-slot@1` 汇合 |
| `contract_net@1` | 分布式基线 | Smith, IEEE Trans. Computers C-29(12), 1980 [待核实页码] | 全部 | 闭环 | `ref` | 适配 `py:agent/runtime/allocator.py` `ContractNetAllocator`（M30 R-M30-38）；`manager_rule = hash_ring`；联盟任务按 `rv-slot@1` 汇合 |
| `cbba@1` | 分布式基线；PA-2 代表性协同基线 | Choi, Brunet, How, IEEE T-RO 25(4):912–926, 2009 [已核] | 全部 | 闭环 | 核心 | 基线卡标注其收敛依赖连通网络与可靠送达；束长 `L = 3`（不超过 `L_claim`），只对束首任务发出 `task.claim`；联盟任务按 `rv-slot@1` 汇合 |
| `rolling_milp@1` | `π_central` | 平台定义（ADR-D4-27）；求解器 `scipy.optimize.milp`（`pyproject.toml` 锁定 `scipy==1.18.1`） | 全部 | 集中式（`global`） | `ref` | 联盟覆盖、能力与同步约束；计划步带 `t_start_s`（X-04）；`node_limit` 截断后贪心修复；不称最优上界 |
| `random@1` | `π_random` | 平台定义 | 全部 | 闭环 | 核心 | 零消息；联盟任务按 `rv-slot@1` 汇合 |
| `d2_greedy@1` | 算法隔离附加基线 | D2 缺省分配器 `py:tasking/greedy.py` `GreedyAllocator`（经 `AllocatorSolver`） | `d0`–`d1` 全部任务；`d2` 只承载单机且无前驱的子集 | 算法隔离 | `ref` | 联盟与有前驱任务返回 `adapter_unsupported`（M30 R-M30-37），按未完成计入 `U_TA`，基线卡报告被拒任务的数量与价值占比；排名模式以 `max_evals` 替代墙钟截断（偏差 D-4） |

全部平台基线只对当前目标任务发出 `task.claim`：承诺对他人不可见（P-10），预先承诺不带来协调收益，只会使计划变更表现为无效承诺（§7.2 `invalid_claim_rate`）。
| TA-C：`kmeans_group@1`、`spectral_group@1` | 几何分组下界基线 | Lloyd 1982；MacQueen 1967；Shi & Malik 2000；Ng, Jordan, Weiss 2001 [待核实页码] | `d3` | 闭环 | 核心 | 须经统一组队可行性检查（§8.4）；r02 §3.2 六项局限写入基线卡 |
| TA-C：`csg_greedy@1` | 联盟形成基线 | Shehory & Kraus, AIJ 101(1–2), 1998 [待核实页码] | `d3` | 闭环 | 核心 | `k_max = 3` |

`NearestTask`（AWR-06 §4.2 的示例策略）不是平台基线，只用于北极星与 DP-11 的"不超过 20 行"判据。北极星的 3 个平台基线只依赖 numpy（M30 R-M30-47）。

### 8.2 基线在 S1 上的参数约束

| 编号 | 约束 | 理由 |
|---|---|---|
| R-S1-23 | 预算可行性：全部官方基线在 `d2/n16`、`abstract-radio@1 default` 下的 `b_tx_ratio ≤ 0.8` 且 `budget_refusals = 0`（pilot 的 `d2/n16` 可行性子集实测，§9.2；未满足者调整其共享周期，参数变更写入基线卡） | `B_cap` 折合每实体每决策 tick（100 ms）约 410 B；广播展开为 n−1 条计费单播（每片头部 64 B），n16 时一次 200 B 的广播约耗 3.96 KB，即每实体约 1 次/秒的广播。逐 tick 一轮共识的 CBBA 不可行，`cbba` 的共识消息周期须 ≥ 1 s 并采用增量编码 |
| R-S1-24 | CPU 可行性：全部官方基线与参考策略在 `d2/n16` 下 CPU 违规率为 0（pilot 的 `d2/n16` 可行性子集实测，§9.2）。`rolling_milp` 只设 `node_limit` 与 `mip_rel_gap`、不设 `time_limit`（M30 §6.7.1），节点内 LP 的耗时没有先验上界，`node_limit` 不能保证 CPU 不超额；pilot 报告单次求解 CPU 的实测分布与最大值，最大值超过"10 s 重规划周期内团队额度的 50%"时按序回退：下调 `node_limit`（2000 → 1000 → 500）、缩短 `horizon_s`（60 → 40 s），每步复测；回退只改变确定性参数，不引入以墙钟或 CPU 时间为条件的截断（保持确定性等级），最终参数写入基线卡 | 违规按 `violation_invalidates` 处置（`ranked_valid = false`，方向化计分使 `U_TA = 0`），参考策略违规会使 Δ 族与 `G` 失真 |
| R-S1-25 | 折扣一致：使用 `path_score` 的基线以 `λ = λ_d`、原始价值 `v_j`、`t_open = t0_j` 调用（§7.1；`path_score` 以服务开始时刻为折扣终点，与 `U_TA` 同口径） | 使基线优化目标与 `U_TA` 一致，避免基线因目标错位而被低估 |
| R-S1-28 | 联盟汇合约定 `rv-slot@1`（全部平台基线与执行器统一采用，零消息）：(1) 选定联盟任务 j 为当前目标的成员驶向半径 `r_wait = r_srv + 10 m` 的等待环上距其来向最近的点并保持；(2) 汇合槽为 `t_k = k · T_rv`（`T_rv = 10 s`，初值，为 Δd 的整数倍），成员在首个满足"已在环上且 `t_k − t_tr(r_wait) ≥ 当前时刻`"的槽的出发时刻 `t_k − t_tr(r_wait)`（向下对齐到决策 tick）由环上驶向中心，于 `t_k` 附近进入服务圆；(3) 若 `t_k + w_j` 时服务仍未开始（搭档未在同一槽进入），成员退回等待环，于下一槽重试（P-05 的到场时刻因此更新）；(4) 同一槽进入的成员在同一决策 tick 出发，起点到中心的距离与 `r_wait` 之差不超过 `r_arrive = 1 m`（环上保持点按 R-M28-52 停于目标 `r_arrive` 以内），按 §9.4 的 `t_tr` 模型，`t_tr(14 m) = 3.10 s`、`t_tr(15 m) = 3.30 s`、`t_tr(16 m) = 3.48 s`，故 `spread ≤ 0.4 s + Δp`，远小于 `w_j = 3 s`（设计目标 `spread ≤ 0.5 s`，AC-S1-21 验证）。该约定只依赖公开时钟与实例公开参数，`π_ind` 可用；它使各基线在联盟任务上的差异只来自"谁去、何时去"的分配决策，而非是否实现了同步协议。代价是每个联盟任务至多 `T_rv` 的额外等待，写入基线卡与 `validity.md`。研究者条目可自行设计同步协议（含经网络发送就绪消息并计费），不受本约定约束 | `w_j = 3 s` 下若无统一约定，闭环基线的联盟任务完成率主要反映是否偶然同步到场，`d2` 约 25% 的价值对基线近乎不可达，PA-2 的配对差只来自单机任务 |
| R-S1-26 | 公开约定：`static_partition` 的联盟组队约定只依赖公开信息（初始位置、能力、公告板），不得使用队友的实时状态 | 保持 `π_ind` 的零通信属性 |
| R-S1-27 | 基线治理：每个基线附基线卡（论文来源、参数、与原文的复现差异、未复现到原文水平的事实，r03 R31）；CBBA 在 `ideal@1` 的 `d0` 实例上收敛为无冲突分配作为复现检验（M30 R-M30-48） | AWR-06 §7.0 基线治理 |

### 8.3 预期排序

记 `Ū(π)` 为场景等权均值（`d2`，`n8`–`n16`，主赛道条件，算法隔离与闭环分别考察各自适用的基线）。

**验收约束**（AC-S1-02，违反即不冻结，须给出机制解释并复测）：

| 编号 | 关系 | 判据 |
|---|---|---|
| E-01 | `Ū(rolling_milp) ≥ Ū(cbba)` | 点估计（集中式参考列对分布式主榜条目） |
| E-02 | `Ū(cbba) > Ū(static_partition)` | 配对差 95% CI 下界 > 0 |
| E-03 | `Ū(static_partition) > Ū(random)` | 配对差 95% CI 下界 > 0 |

**pilot 预期**（不作为冻结条件；与预期不符时须在 pilot 报告与基线卡中给出机制解释，M30 R-M30-49）：

| 编号 | 预期 | 机制 |
|---|---|---|
| E-04 | `cbba ≥ ssi ≥ nearest_greedy ≥ static_partition`（`ideal@1`） | 前两者显式优化时间扩展路径；最近任务贪心只做单步决策；静态分区无法应对负载不均；联盟任务的同步由统一约定 `rv-slot@1` 承担（R-S1-28），排序差异来自分配质量 |
| E-05 | `abstract-radio@1` 相对 `ideal@1` 的降幅：`hungarian_periodic`、`cbba` 最大，`nearest_greedy` 居中，`static_partition` 为 0 | `planner` 宿主的状态报告老化，以及分区期间规划 Agent 与两组完全隔离（X-01，宿主约定的影响，以 `station_side = joined` 对照条件单独估计）；CBBA 在丢包与分区下出现重复分配与任务遗漏（r02 §3.1 新难点第 3 条）；零通信策略不受网络影响。pilot 报告分别给出 `hungarian_periodic` 在 `isolated` 与 `joined` 下的降幅 |
| E-06 | `contract_net` 与 `ssi` 同量级，在 `d2` 上低于 `cbba` | 二者均为市场方法；合同网的报价截止 `t_quote_s = 3 s` 与管理者轮转增加时延 |
| E-07 | `d2_greedy` 在 `d2` 上显著低于 `hungarian_periodic`（二者均为算法隔离、`planner` 宿主） | `d2` 中联盟任务占任务数 25%、有前驱任务约 15%，二者的并集期望约 36%（价值分布与任务类型独立，故约占价值总量的 36%），适配器对其返回 `adapter_unsupported`（R-M30-37）；基线卡报告被拒任务的实际价值占比 |
| E-08 | `d0` 上 `rolling_milp`、`cbba`、`ssi` 的 `opt_ratio ≥ 0.95` | `d0` 规模小、无时间窗与通信约束（锚点运行在 `ideal@1`） |
| E-09 | 网络档间排名变化：至少一对分布式基线在 `ideal@1` 与 `abstract-radio@1` 之间排名反转 | 论文 H3 的 S1 部分；pilot 只做探索性观察，不作为确认性证据 |

数值预期不写入本文：在无实测依据时给出预期分数会误导 pilot（n03 §2.9 对外部方案"预期得分"的处理）。

### 8.4 TA-C 统一组队可行性检查（P1）

联盟候选 `(C, j)` 可行，当且仅当：

| 编号 | 条件 |
|---|---|
| CF-1 | 能力覆盖：`∪_{i∈C} c_i ⊇ r_j` 且 `|C| = m_j` |
| CF-2 | 成员状态：全部成员 `ACTIVE`，未计划在服务区间内退出 |
| CF-3 | 可达：每个成员自当前位置的 `t_tr(路径长度)` 满足 `now + t_tr ≤ l_j − s_j` |
| CF-4 | 能量（`d3`）：每个成员到达、服务并抵达最近机巢的能量 ≤ 剩余能量 |
| CF-5 | 不重叠：成员已承诺的其他任务的预计服务区间与本任务的预计服务区间不重叠 |
| CF-6 | 前置：`pred_j` 已完成或其预计完成时刻 ≤ 预计服务开始时刻 |

几何分组基线（K-Means、谱聚类）的分组结果先经 CF-1 至 CF-6 筛选，不可行组拆分后按贪心重组；检查实现由平台提供，全部 TA-C 基线共用，使差异只来自分组策略（AC-D4-33）。

---

## 9. 评测协议

### 9.1 估计目标与统计单位

| 项 | S1 的取值 |
|---|---|
| 估计目标 | `family`（AWR-06 §8.6）：场景配置由生成器在固定难度档与规模档下以 CSPRNG 抽取的 `instance_seed` 生成，视为来自该生成分布的独立样本；只在场景层有放回重采样，bootstrap-t 区间 |
| 统计单位 | 场景配置 = 实例 × 运行模式 |
| 层次 | `[scenario, seed, repeat]`；场景内对外生种子与重复取均值 |
| 判定 | `ε = 0.02`、`ε_eq = 0.05`、Holm 检验族 F_rank 与 F_dom（AWR-06 §8.6） |
| 失败计分 | `invalid_policy = zero`；基础设施故障原样重跑一次；有效外生种子少于 3 个的场景使条目 `status = incomplete` |

### 9.2 样本规模

| 阶段 | 配置 | 说明 |
|---|---|---|
| pilot（WP-D4-17，自第 9 周） | `d1/n20` 与 `d2/n8` 各 10 实例 × 10 外生种子，全部官方基线与参考策略 | AWR-06 §7.0 的 S1 pilot 配置；估计配对差的 ICC 与 `σ_D` 及其单侧 80% 置信上界 |
| pilot 可行性子集（本文增补） | `d2/n16` 5 实例 × 2 外生种子，全部官方基线与参考策略（约 90 Episode × ≤ 90 s ≈ 2.3 宿主小时/轮） | 只用于 R-S1-23、R-S1-24 的预算与 CPU 可行性及 `rolling_milp` 单次求解 CPU 分布；不用于 ICC 估计。`holdout` 的 `d1/n8`、`d1/n16`、`d2/n16` 未被 ICC 估计覆盖，样本量规则取两个已估计配置上界中的较大者，该外推假设写入 pilot 报告，并在 AC-S1-07 的留出集校准中以 `holdout` 实测复核；复核所得设计效应高于假设时按规则重定 S。完整覆盖的成本与取舍提请 AWR-06（§15.1 第 21 条） |
| 成榜缺省（pilot 前占位） | 每场景集 18 个场景配置 × 5 个外生种子 = 90 Episode | pilot 后按 `S = max(12, ⌈n_eff* · (1 + (m − 1)·ICC_U) / m⌉)` 冻结 |
| 档位分配 | `holdout` 的 18 个场景按 `{d1, d2} × {n8, n16}` 分层，各 4–5 个（`d2` 多分配 1 个）；P1 加入 `n32` 时重算 | 分层只用于覆盖，不改变估计目标 |

### 9.3 训练与学习型方法

S1 算法隔离模式是 MARL 的首个承载（E7、AC-D4-28）：PettingZoo 映射中 `tasks: Box(Tmax, G) + mask` 的特征 `G` 为 `(x, y, s_j, r_j 位, m_j, w_j, l_j − now, v_j, 前驱已满足)`，`claim: Discrete(Tmax + 1)`；奖励为训练接口自定义，评测恒用 `U_TA`。训练种子与产物按 AWR-06 §8.6"学习型方法"规则登记。

### 9.4 `d0` 穷举参考 `ref/ta_d0_dp@1`

`d0` 为静态、同构、单机、无时间窗、无障碍的实例（`t0_j = 0`）。对单个智能体而言，指数折扣使后继价值按起始时刻等比缩放，因此"从某出发点出发、服务集合 S 的最大折扣价值"具有最优子结构。以服务开始时刻为折扣终点（§7.1），自出发点驶向 j 用时 `t_tr`、服务 `s_j` 后自 j 出发：

```
g(∅, k)  = 0
g(S, k)  = max_{j∈S} exp(−λ_d · t_tr(‖x_k − x_j‖)) · (v_j + exp(−λ_d · s_j) · g(S \ {j}, j))
f_i(S)   = max_{j∈S} exp(−λ_d · t_tr(‖p_i⁰ − x_j‖)) · (v_j + exp(−λ_d · s_j) · g(S \ {j}, j))
J*       = max_{S_1 ⊔ … ⊔ S_n = T} Σ_i f_i(S_i) / Σ_j v_j
```

参考时序模型（`timing_model = planar1-enter@1`）：

| 项 | 定义 |
|---|---|
| 运动 | `planar@1` 的离散积分（M28 R-M28-52、R-M28-53、§6.5.3 第 1–4 步），`v_max = 10 m/s`、`a_max = 2 m/s²`、`Δp = 20 ms`（`d4_std`），`goto_w` 期望速度 `min(v_max, √(2·a_max·max(d − r_arrive, 0)))`，`r_arrive = 1 m`（M28 属性 `/motion/planar/r_arrive_m` 缺省值），含减速曲线 |
| `t_tr(d)` | 实体自静止、距目标中心 d 处起步，以 `goto_w(x_j)` 沿直线驶向中心（静止起步时加速度指向目标，轨迹为直线，故按一维积分计算），`t_tr(d) = Δp · min{k ≥ 1 : d − s_k ≤ r_srv}`，`s_k` 为第 k 个物理边界的行进距离；即以首次在物理边界上进入服务圆（P-01(b)）为终点，在 `d0` 中该边界即服务开始时刻 `τ^s_j`（承诺已持有、无开窗与前驱约束） |
| 服务期间 | 实体继续 `goto_w(x_j)`（X-03），按减速曲线停于距中心 `r_arrive` 以内（10 s 服务内必然停稳；`d ≥ 80 m` 时停止点距中心约 0.80 m） |
| 后继出发点 | 模型取任务中心 `x_j`，出发时刻为完成时刻 `τ_j`，自静止起步；实际出发点与中心的偏差 ≤ `r_arrive`，计入容差 |
| 不含 | 决策周期与建模时延造成的空等（每次指令变更 ≤ Δd + `ℓ_model` = 200 ms） |

`|T| = 8`、`n = 4` 时，单机子集动态规划为 `2⁸ × 8 × 8` 次运算，跨智能体的子集划分枚举为 `4⁸` 次，单实例计算量可忽略。`d0` 生成时另要求任务之间、任务与初始位置之间的距离 ≥ 20 m（G-05），此时 `t_tr` 对距离的斜率不超过 0.16 s/m（`d = 20 m` 处的数值，`d ≥ 80 m` 时为 `1/v_max = 0.10 s/m`）。

容差分析（AC-S1-01 的 1%）：执行器运行与参考模型的差异只有两项——每跳的指令时延 ≤ 0.2 s，与出发点偏差 ≤ 1 m 造成的 `t_tr` 差 ≤ 0.16 s。对 §11.1 的锚点实例，以参考实现在每跳附加 0.3 s 时延并把每跳距离加 1 m 重算，得 `J = 0.80094`，相对 `J* = 0.80486` 的偏差为 0.49%；单附加 0.2 s 时延时为 0.24%。出发点偏差可正可负，指令时延只降低效用，因此 1% 容差在两侧均有约 2 倍余量。每个锚点的 `hidden/eval/expected.json` 另写入以同一方法（每跳 +0.3 s、+1 m）计算的偏差上界 `dev_bound_rel`；AC-S1-01 的判据为 1%，偏差超出 `dev_bound_rel` 但仍在 1% 内时评测器测试输出警告，提示执行器或运动模型可能偏离参考时序模型。

---

## 10. 验收

| 编号 | 内容 | 阶段 | 对应 |
|---|---|---|---|
| AC-S1-01 | PA-1：同 `params + instance_seed` 生成的实例 JCS 摘要 100 次逐字节一致（子进程与多进程池、`PYTHONHASHSEED` 三种取值下）；`ta-core.anchor` 的每个 `d0` 锚点：(a) `ref/ta_d0_dp@1` 按 §9.4 参考时序模型重算的 `J*` 与 `hidden/reference/reference.json` 一致（相对差 ≤ 10⁻⁹）；(b) 以其最优计划经执行器 `ta-exec@1` 在 `ideal@1`、`d4_std` 下运行，评测器 `U_TA` 与 `J*` 的相对偏差 < 1%（§9.4 容差分析）；(c) 评测器对该 trace 的离线重算与在线 `TaskMonitor` 的 `suite.taskalloc.task_state` 事件逐项一致 | P0 | AWR-06 AC-S1-1；R-S1-14 |
| AC-S1-02 | 基线序关系 E-01 至 E-03 在 `d2`（`dev` pilot 与冻结前 `holdout` 校准）上成立；违反时 pilot 报告给出机制解释并复测后方可冻结 | P0 | AWR-06 AC-S1-2 |
| AC-S1-03 | PA-2（代表性协同基线 `cbba` 与 `π_ind` 的配对差 ≥ 0.05 且 95% CI 不含 0；至少两对基线经 Holm 校正后显著且 > 0.05）与 PA-3（`U_central − U_ind ≥ 0.15`）在 `d1`、`d2` 的 `dev` pilot 配置上成立；R-S1-18 的 NLoS 苛刻度确认记录入档 | P0 | AWR-06 AC-S1-3 |
| AC-S1-04 | PA-4：在 ≥ 75% 的实例上，`π_ind`、`π_central` 与主榜官方基线（`nearest_greedy`、`hungarian_periodic`、`ssi`、`contract_net`、`cbba`）中没有一个的 `U_TA > 0.95` 或 `< 0.05`；`random` 与 `d2_greedy` 为附加基线，其下饱和是预期行为（随机选择在时间窗约束下大量过期；适配器拒绝联盟与前置任务），不参与判定，只在 pilot 报告中列出。该范围写入基线卡，并提请 AWR-06 明确（§15.1 第 19 条） | P0 | AWR-06 AC-S1-4 |
| AC-S1-05 | PA-5：固定制品 100 次重跑 trace 事件哈希一致（等级 1b），覆盖闭环与算法隔离两种模式、`ideal@1` 与 `abstract-radio@1` | P0 | AWR-06 AC-S1-5 |
| AC-S1-06 | PA-6：单 Episode（`L1`、`n8`、300 s、`d4_std`、`abstract-radio@1`）宿主墙钟 ≤ 45 s、trace ≤ 200 MB；北极星配置（§5.3）满足 AWR-06 §5.10 的分项预算；R-S1-15 的生成耗时目标 | P0 | AWR-06 AC-S1-6；AC-D4-11 |
| AC-S1-07 | 留出集校准：`holdout` 与未参与调参的 `validation` 实例在冻结前以参考策略与全部官方基线（不使用任何提交）执行 PA-2 至 PA-4 与 E-01 至 E-03，不满足者不冻结；`U_central − U_ind` 的 95% CI 下界 ≥ 0.10 作为 H2 的预注册判据之一记录 | P1 | AWR-06 §7.0、§9.2 H2；WP-D4-21 |
| AC-S1-08 | 判定语义单元测试（手工构造 trace）：单机完成；联盟任务在 `spread ≤ w_j` 时开始、`> w_j` 时不开始、早到者退出再进入后开始；服务中断清零；P-02 的归属裁决——两任务服务圆重叠时按承诺授予时刻、授予时刻相同时按 `task_id`、三个任务服务圆两两重叠且承诺授予时刻并列、服务进行中的计入成员进入另一任务服务圆且对其承诺更早时保持原归属（连续性）；开窗前不计服务；前驱未完成不计服务；截止后完成无效；`payload_loss` 后不再具备资格；冗余参与者不影响完成；停靠机巢的实体不具备资格（`d3`） | P0 | P-01 至 P-09 |
| AC-S1-09 | 承诺私有性：泄露审计确认承诺事件只出现在承诺方的观测与 Agent 可见 trace 中；公告板只含 P-11 允许的字段与延迟；构造"以承诺编码信息"的对照策略，其接收方观测与无承诺时逐字节一致 | P0 | P-10、P-11；AWR-06 §8.8 |
| AC-S1-10 | 零通信不变性：`static_partition` 与 `random` 在 `none@1`、`ideal@1`、`abstract-radio@1` 下同实例同种子的 `U_TA` 与真值层 trace 逐字节一致；其 `Δ_cut` 的每个 Episode 对差值为 0；`rolling_milp`（`global`）的结果对网络档不变 | P0 | AWR-06 AC-D4-16；R-S1-20 |
| AC-S1-11 | 共同随机数：同 `(instance, exogenous_seed)` 下，改变 `nearest_greedy` 的 `refresh_s`（1、5、20 s），到达时刻、故障时刻、分区掩码与导航噪声序列逐位一致 | P0 | R-S1-17；AC-D4-05 |
| AC-S1-12 | 预算执行：超额字节返回 `budget` 且不记违规；任务 Agent 个体超额与团队合计超额（含规划 Agent 与集中决策主体的用量）均记违规并按 `violation_invalidates` 处置（`ranked_valid = false`，主指标为 0，资源分量保留实测；M32 R-M32-38）；全部官方基线满足 R-S1-23、R-S1-24 | P0 | §6.4 |
| AC-S1-13 | 子编队投影：对 `π_ind` 的零通信运行，按"每智能体一部分"的划分，`Σ_i U_TA^({i})` 与 `U_TA` 之差等于跨部分联盟任务的贡献（`d0`–`d1` 为 0），误差 ≤ 1 × 10⁻¹² | P0 | §7.6 |
| AC-S1-14 | 执行器语义：X-01 至 X-05 的用例（只承诺当前计划步、当前步被替换时释放承诺、等待环与定时进入、无 `t_start_s` 的联盟任务按 `rv-slot@1` 执行、无效计划沿用上一份、`d2_greedy` 对联盟与前置任务返回 `adapter_unsupported`、`planner` 宿主的报告与推送计费、分区期间规划 Agent 在 `isolated` 与 `joined` 下的连通性）；`hungarian_periodic` 与 `d2_greedy` 的 `Plan.digest()` 在同一视图与 `max_evaluations` 下逐字节一致；执行器经 M31 `SuiteRuntime.executor()` 装载（R-M31-96） | P0 | M30 AC-M30-05、12；M31 AC-M31-31 |
| AC-S1-15 | 实例包校验：全部 `dev` 实例通过 M31 V-I-01 至 V-I-28 与 R-S1-13（含 G-06 至 G-08 的复核）；对每个 `dev` 实例抽取 100 个外生种子实现到达时刻，逐任务验证 `l_j ≤ H`、`l_j − t0_j − s_j ≥ t_reach_j` 与 G-07 的单链调度可行，违例数为 0；`dev` 摘要列表在支持矩阵全部平台上比对一致；生成失败用例（含 G-08 合格集合为空）按 R-S1-16 报告 | P0 | M31 AC-M31 系列；G-06 至 G-08 |
| AC-S1-16 | 指标单元测试：§7.2 至 §7.5 每个指标至少一个手工构造 trace 的期望值用例，并含以下反例：`realloc_latency_s` 的截尾、联盟搭档在故障前已持有承诺（不得记 0 延迟，受影响任务须等到 `t_f` 之后新授予的承诺恢复覆盖）、冗余承诺方在故障前已持有承诺且覆盖未丢失（任务不进入 `A_f`）；`reassign` 对首次承诺、联盟成员单纯增补、任务关闭导致的承诺失效均不计数，对承诺方替换计 1；`invalid_claim_rate` 在算法隔离模式下不因计划后续步变化而增加；`conflict_rate` 的离散化；`U_cum(H)` 与 `U_TA` 逐位一致；可空指标的空值；评测器不读取未声明通道 | P0 | R-S1-21、22 |
| AC-S1-17 | TA-C：CF-1 至 CF-6 单元测试；R-S1-29 至 R-S1-34 的用例（停靠成功、满位、超距、停靠期间不具备参与资格、充电至上限、耗尽不可恢复、成员变化下 `B_cap` 的计算）；`d3` 区分度门（PA-2 至 PA-4，代表性协同基线为 `cbba`，分组基线须低于 `cbba` 或给出机制解释）；`reorg`、`coal_conn` 用例 | P1 | AC-D4-33；WP-D4-26 |
| AC-S1-18 | X1 接入（§6.6）：F、C、P、L、J 族（`ta-core` 宿主）与 H 族（`tac` 宿主）在 P1、M 族在 P2 注入后，X1 AC-RC-01 的配对一致性成立；`T_detect` 按 X1 §8.2 只以 `x1.response_ops` 计（对 `Held(o)` 的 `task.claim` 接管；M 族另含 `a_o` 的 `task.release`、`commit.revoke`；H 族为改选机巢的停靠请求），不以路径改变计；`U_cum(H)` 与 `U_TA` 逐位一致（X1 R-RC-46）；`perturbation.json.host.candidates` 不含实例内故障对象；`d3` 宿主不施加 F、C、M 族 | P1 | X1 协议；WP-D4-19 |
| AC-S1-19 | 有效性声明：`docs/suites/taskalloc/validity.md` 覆盖 §12 的全部条目，并以摘要绑定进入 Board Version | P0 | AWR-06 §7.0；r03 R15 |
| AC-S1-20 | 训练框架适配：PettingZoo `parallel_api_test` 在 S1 算法隔离模式通过；BenchMARL MAPPO 以固定种子完成训练与评测 | P1 | AC-D4-28；WP-D4-24 |
| AC-S1-21 | 联盟汇合（R-S1-28）：(a) 单元用例——两个成员按 `rv-slot@1` 以到环时差 0、3、7、12、25 s 到达，服务均能开始且 `Δt_sync,j ≤ 0.5 s`，额外等待不超过 `T_rv` 加一次退回重试的耗时；(b) pilot 判据（`ideal@1`、`d2/n8` 的 `dev` pilot 配置，冻结前须满足）——`cbba` 与 `ssi` 的 `cr_coal ≥ 0.5 · cr_single`（`cr_single` 为单机任务完成率）且 `sync_q ≥ 0.8`（目标值；`Δt_sync,j ≤ 0.5 s` 时 `ψ_j ≥ 0.83`）；不满足时先检查基线是否正确实现约定，再按 PA-7 调整 `d2` 的联盟比例或服务时长并复测，仍不满足者给出机制解释，`d2` 不冻结 | P0 | R-S1-28；RK-S1-05 |

---

## 11. 实例样例

### 11.1 `d0` 锚点实例（完整）

坐标：`capl:ta-core/pu-00000/d0/n4@1`。区域 424 m × 424 m，4 架同构多旋翼，8 个静态单机任务，`s_j = 10 s`，无障碍。参考值 `J* = 0.80486` 按 §9.4 的参考时序模型 `planar1-enter@1` 与 §7.1 的折扣口径以参考实现离线重算（最优划分为每架 2 个任务：{task_00001, task_00003}、{task_00002, task_00008}、{task_00004, task_00007}、{task_00005, task_00006}）；作为一致性核对，同一模型下以完成时刻为折扣终点（v0.1 口径）重算得 0.72827，与 v0.1 所列 0.7284 在四舍五入范围内一致，最优划分不变。物化时以 `ref/ta_d0_dp@1` 的正式实现重算并写入，`sha256` 与 `nonce` 字段在物化时计算，样例以占位表示。

`manifest.json`：

```json
{
  "schema": "drone4d.suite.instance.v1",
  "instance_id": "capl:ta-core/pu-00000/d0/n4@1",
  "suite": {"suite_id": "taskalloc", "major": 1},
  "family": "ta-core",
  "coordinates": {"family": "ta-core", "block": "pu", "index": 0, "difficulty": "d0", "scale": "n4", "rev": 1},
  "generator": {
    "id": "gen/ta_poisson", "version": 1,
    "params": {"n": 4, "L_ref_m": 600, "n_ref": 8, "rho_T": 2, "arrival": {"kind": "static"},
               "layout": {"kind": "uniform"}, "obstacles": {"kind": "blocks", "cover": 0},
               "cap": {"K": 1}, "service_s": [10, 10], "value_probs": [0.5, 0.3, 0.2]},
    "params_provenance": "difficulty_presets.d0 + blocks.pu"
  },
  "instance_seed_ref": {"value": "4127730598810214311"},
  "difficulty": {"rho_T": 2.0, "load_W": null, "kappa_mean": null, "trunc_frac_ref": null, "f_C": 0.0,
                 "f_hard": 0.0, "f_prec": 0.0, "cap_scarcity": 0.5, "cap_scarcity_post": 0.5,
                 "u_feasible": 1.0, "obstacle_cover": 0.0, "detour_ratio": 1.0,
                 "partition_frac": 0.0, "fault_count": 0},
  "world_kind": "planar",
  "files": [
    {"path": "agents.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"},
    {"path": "brief.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"},
    {"path": "comm.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"},
    {"path": "eval/binding.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"},
    {"path": "objective.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"},
    {"path": "tasks.json", "sha256": "<sha256>", "bytes": 0, "view": "scheduled"},
    {"path": "world/planar.json", "sha256": "<sha256>", "bytes": 0, "view": "brief"}
  ],
  "reference_files": [
    {"path": "hidden/eval/expected.json", "sha256": "<sha256>", "bytes": 0, "view": "truth"},
    {"path": "hidden/reference/reference.json", "sha256": "<sha256>", "bytes": 0, "view": "truth"}
  ],
  "reference_rev": 1,
  "reference_sha256": "<sha256>",
  "units": {"suffix": {"_mm": "mm", "_ms": "ms", "_mm_s": "mm/s", "_mm_s2": "mm/s^2"},
            "map": {"arrival_s": "arrival_ms", "service_s": "service_ms", "pos": "pos_mm"}},
  "license": {"spdx": "LicenseRef-ANet-Open-Source"},
  "build": {"created_unix_ns": "0", "tool_version": "drone4d-suite-build/0.1", "host_class": "hwc-x86-64-v3",
            "impl_sha256": "<sha256>"}
}
```

`world/planar.json`：

```json
{"bounds_mm": [0, 0, 424000, 424000], "obstacles": []}
```

`agents.json`：

```json
{
  "entities": [
    {"id": "uav_00001", "kind": "multirotor", "pos0_mm": [150000, 150000], "z_layer_mm": 30000,
     "v_max_mm_s": 10000, "a_max_mm_s2": 2000, "capabilities": ["rgb.capture"], "energy_wh": 500.0},
    {"id": "uav_00002", "kind": "multirotor", "pos0_mm": [274000, 150000], "z_layer_mm": 35000,
     "v_max_mm_s": 10000, "a_max_mm_s2": 2000, "capabilities": ["rgb.capture"], "energy_wh": 500.0},
    {"id": "uav_00003", "kind": "multirotor", "pos0_mm": [150000, 274000], "z_layer_mm": 40000,
     "v_max_mm_s": 10000, "a_max_mm_s2": 2000, "capabilities": ["rgb.capture"], "energy_wh": 500.0},
    {"id": "uav_00004", "kind": "multirotor", "pos0_mm": [274000, 274000], "z_layer_mm": 45000,
     "v_max_mm_s": 10000, "a_max_mm_s2": 2000, "capabilities": ["rgb.capture"], "energy_wh": 500.0},
    {"id": "gs_00001", "kind": "edge_node", "pos0_mm": [212000, 212000], "z_layer_mm": 2000,
     "v_max_mm_s": 0, "a_max_mm_s2": 0, "capabilities": [], "energy_wh": null}
  ],
  "agents_template": [
    {"agent_id": "agt_00001", "controls": [{"entity": "uav_00001", "scope": "FULL"}], "role": "task"},
    {"agent_id": "agt_00002", "controls": [{"entity": "uav_00002", "scope": "FULL"}], "role": "task"},
    {"agent_id": "agt_00003", "controls": [{"entity": "uav_00003", "scope": "FULL"}], "role": "task"},
    {"agent_id": "agt_00004", "controls": [{"entity": "uav_00004", "scope": "FULL"}], "role": "task"},
    {"agent_id": "agt_00005", "controls": [{"entity": "gs_00001", "scope": "FULL"}], "role": "planner"}
  ]
}
```

`role = planner` 的 Agent 只在 `solver_host = planner` 的场景中实例化；其余场景中地面站为无 Agent 的被动实体。

`tasks.json`：

```json
{
  "arrival_process": {"kind": "static", "horizon_arr_ms": 0},
  "tasks": [
    {"id": "task_00001", "arrival_ordinal": 1, "arrival_ms": 0, "initial": true, "pos_mm": [60000, 80000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 5, "predecessors": []},
    {"id": "task_00002", "arrival_ordinal": 2, "arrival_ms": 0, "initial": true, "pos_mm": [380000, 60000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 1, "predecessors": []},
    {"id": "task_00003", "arrival_ordinal": 3, "arrival_ms": 0, "initial": true, "pos_mm": [205000, 40000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 3, "predecessors": []},
    {"id": "task_00004", "arrival_ordinal": 4, "arrival_ms": 0, "initial": true, "pos_mm": [30000, 300000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 3, "predecessors": []},
    {"id": "task_00005", "arrival_ordinal": 5, "arrival_ms": 0, "initial": true, "pos_mm": [400000, 350000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 5, "predecessors": []},
    {"id": "task_00006", "arrival_ordinal": 6, "arrival_ms": 0, "initial": true, "pos_mm": [212000, 390000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 1, "predecessors": []},
    {"id": "task_00007", "arrival_ordinal": 7, "arrival_ms": 0, "initial": true, "pos_mm": [100000, 212000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 1, "predecessors": []},
    {"id": "task_00008", "arrival_ordinal": 8, "arrival_ms": 0, "initial": true, "pos_mm": [330000, 212000],
     "service_ms": 10000, "required_capability": ["rgb.capture"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 3, "predecessors": []}
  ]
}
```

`comm.json`、`objective.json`、`brief.json`（摘录）：

```json
{"comm_zones": [], "partitions_public": [], "relays": []}
```

```json
{
  "primary": "u_ta", "lambda_d_per_s": 0.01, "partial_credit": 0.0, "horizon_ms": 300000,
  "termination": "horizon", "service_radius_mm": 5000, "status_pub_delay_ms": 5000, "claim_max": 3,
  "aux": ["cr", "resp_mean_s", "j_max_s", "j_sum_m", "reassign", "conflict_rate", "dup_exec_rate",
          "invalid_claim_rate", "energy_wh", "jain_fair"],
  "resource": ["b_tx_ratio", "cpu_ratio", "bytes_per_task"],
  "safety": ["contact_count", "fail_rate"]
}
```

```json
{
  "region": {"bounds_mm": [0, 0, 424000, 424000]}, "obstacles_ref": "world/planar.json",
  "stations": [{"id": "gs_00001", "pos_mm": [212000, 212000]}], "nests": [],
  "arrival_process": {"kind": "static", "horizon_arr_ms": 0}, "cost_model": "euclid",
  "status_pub_delay_ms": 5000, "claim_max": 3, "service_radius_mm": 5000
}
```

`hidden/reference/reference.json` 与 `hidden/eval/expected.json`（参考文件，`truth` 视图，只随 `dev` 锚点分发）：

```json
{
  "nonce": "<nonce>",
  "kind": "J_star", "value": 0.80486, "solver": "ref/ta_d0_dp@1", "solver_version": "1.0.0",
  "timing_model": {"id": "planar1-enter@1", "physics": "planar@1", "dp_ms": 20, "v_max_mps": 10, "a_max_mps2": 2,
                   "r_arrive_m": 1.0, "r_srv_m": 5.0, "endpoint": "first_boundary_inside_service_circle",
                   "successor_departure": "task_center_at_completion", "decision_latency": "excluded",
                   "discount_end": "service_start"},
  "tolerance_rel": 0.01, "source": "platform reference solver (S1 spec §9.4)",
  "partition": [["task_00001", "task_00003"], ["task_00002", "task_00008"],
                ["task_00004", "task_00007"], ["task_00005", "task_00006"]]
}
```

```json
{
  "nonce": "<nonce>",
  "evaluator": "taskalloc@1",
  "cases": [
    {"policy": "oracle:ref/ta_d0_dp@1", "network": "ideal@1", "clock": "d4_std", "seed": 0,
     "expect": {"u_ta": {"value": 0.80486, "tol_rel": 0.01, "dev_bound_rel": 0.0049},
                "cr": {"value": 1.0, "tol_abs": 0.0}, "conflict_rate": {"value": 0.0, "tol_abs": 0.0}}}
  ]
}
```

### 11.2 `d2` 实例摘录

坐标：`capl:ta-core/pc-00017/d2/n8@1`。区域 600 m × 600 m，8 架异构多旋翼（K = 3），32 个任务（其中 8 个联盟任务），障碍覆盖率 10%，Poisson 到达。以下只摘录 4 个任务、故障与分区。

```json
{
  "arrival_process": {"kind": "poisson", "profile": "steady", "initial_frac": 0.25, "horizon_arr_ms": 240000},
  "tasks": [
    {"id": "task_00003", "arrival_ordinal": 3, "arrival_ms": 0, "initial": true, "pos_mm": [118400, 402150],
     "service_ms": 14000, "required_capability": ["thermal.imaging"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 3, "predecessors": []},
    {"id": "task_00011", "arrival_ordinal": 11, "arrival_ms": null, "initial": false, "pos_mm": [455900, 231700],
     "service_ms": 20000, "required_capability": ["lidar.scan", "rgb.zoom"], "required_agents": 2, "sync_window_ms": 3000,
     "window": {"open_offset_ms": 0, "close_offset_ms": 162000}, "deadline_type": "hard", "value": 5, "predecessors": []},
    {"id": "task_00019", "arrival_ordinal": 19, "arrival_ms": null, "initial": false, "pos_mm": [301250, 77600],
     "service_ms": 22000, "required_capability": ["rgb.zoom"], "required_agents": 1, "sync_window_ms": null,
     "window": {"open_offset_ms": 0, "close_offset_ms": 118000}, "deadline_type": "hard", "value": 1, "predecessors": ["task_00003"]},
    {"id": "task_00024", "arrival_ordinal": 24, "arrival_ms": null, "initial": false, "pos_mm": [52300, 540800],
     "service_ms": 20000, "required_capability": ["rgb.zoom", "thermal.imaging"], "required_agents": 2, "sync_window_ms": 3000,
     "window": {"open_offset_ms": 0, "close_offset_ms": 300000}, "deadline_type": "soft", "value": 3, "predecessors": []}
  ]
}
```

`hidden/truth.json` 与 `hidden/comm_truth.json`：

```json
{
  "nonce": "<nonce>",
  "faults": [
    {"id": "fault_00001", "t_ms": 150000, "entity": "uav_00006", "kind": "payload_loss", "params": {}}
  ],
  "membership": []
}
```

```json
{
  "nonce": "<nonce>",
  "partitions": [
    {"id": "part_00001", "t_start_ms": 117000, "duration_ms": 60000,
     "groups": [["agt_00001", "agt_00003", "agt_00004", "agt_00008"],
                ["agt_00002", "agt_00005", "agt_00006", "agt_00007"]],
     "unassigned": ["agt_00009"]}
  ],
  "link_mask": []
}
```

能力按 R-S1-03 轮转：`uav_00001`、`00004`、`00007` 为 `rgb.zoom`，`uav_00002`、`00005`、`00008` 为 `thermal.imaging`，`uav_00003`、`00006` 为 `lidar.scan`。故障对象 `uav_00006` 满足 G-08：失能后 `lidar.scan` 仍由 `uav_00003` 持有（`cap_scarcity_post` 的 `lidar.scan` 分子为 1）；G-06 中需 `lidar.scan` 的任务（如 `task_00011`）的 `t_reach_j` 只以 `uav_00003` 计算。

`task_00019` 的前驱 `task_00003` 为初始任务（到达时刻 0，到达序号更小），按 G-07 校验：`EC_00003 + t_tr(‖x_00003 − x_00019‖) + s_00019 ≤ min(l^off_00019, H)`，其中两任务相距约 372.5 m，`t_tr ≈ 40.1 s`，`s = 22 s`，`l^off = 118 s`，要求 `EC_00003 = t_reach_00003 + 14 s ≤ 55.9 s`。非初始任务的时间窗以到达时刻为基准，运行期按外生种子实现到达时刻后换算为绝对时刻并截断到 H（R-S1-07）；`task_00011` 为非初始硬截止任务，`l^off = 162 s`，若到达时刻晚于 138 s 则 `l_j` 截断为 300 s，G-06(3) 以 `t^a_ref = 240 s` 检验 `t_reach + 20 s ≤ 60 s` 保证截断后仍可完成。规划 Agent `agt_00009`（绑定地面站 `gs_00001`，只在 `planner` 宿主场景实例化）不属于任何分区组，排名运行中分区期间与两组均断开（X-01）。

### 11.3 场景集声明（Suite 清单摘录）

```json
{
  "scenario_sets": [
    {"id": "ta-core.holdout.cl", "family": "ta-core", "split": "holdout", "run_mode": "closed_loop",
     "event_mode": "exogenous",
     "tiers": [{"difficulty": "d1", "scale": "n8", "count": 4}, {"difficulty": "d1", "scale": "n16", "count": 4},
               {"difficulty": "d2", "scale": "n8", "count": 5}, {"difficulty": "d2", "scale": "n16", "count": 5}],
     "blocks": ["h1", "h2"], "members_ref": "server:ta-core/holdout/cl", 
     "exogenous_seeds": {"count": 5, "seed_set_commit": "sha256:<commit>"},
     "estimand": "family", "role": "ranked"},
    {"id": "ta-core.dev.smoke", "family": "ta-core", "split": "dev", "run_mode": "closed_loop",
     "event_mode": "exogenous",
     "tiers": [{"difficulty": "d1", "scale": "n8", "count": 1}, {"difficulty": "d2", "scale": "n8", "count": 1}],
     "blocks": ["pu"], "members_ref": "dev/ta-core/smoke.json",
     "exogenous_seeds": {"public": [0, 3]}, "estimand": "family", "role": "smoke"}
  ]
}
```

### 11.4 实验配置（研究者本地，`dev`）

```yaml
schema: drone4d.coord.experiment.v1
name: ta-d2-n8-cbba-vs-baselines
suite: {id: taskalloc, version: 1.0.0}
split: dev
instances: {family: ta-core, block: pc, difficulty: d2, scale: n8, count: 10}
track: {execution_topology: decentralized, observation_profile: L1, budget_profile: B1, network_profile: abstract-radio@1}
run_mode: closed_loop
mode: exogenous
seeds: {exogenous: {range: [0, 10]}, repeats: 1}
clock_profile: d4_std
physics: planar@1
network: {params_ref: net/abstract-radio-default@1}
policies:
  - {name: static_partition, ref: "baseline:taskalloc/static_partition@1"}
  - {name: nearest_greedy,   ref: "baseline:taskalloc/nearest_greedy@1"}
  - {name: cbba,             ref: "baseline:taskalloc/cbba@1"}
  - {name: mine,             ref: "module:my_pkg.policy:MyPolicy"}
reference: {independent: static_partition, central: rolling_milp}
interventions: [ind, cut]
evaluation: {compare: paired, ci: {method: cluster_bootstrap_t, estimand: family, n_boot: auto, level: 0.95}, multiple_comparison: holm}
outputs: {dir: runs/ta-d2-n8, trace_level: standard, report: [md, html]}
```

### 11.5 算法隔离视图与计划

`ProblemView.body`（`drone4d.suite.taskalloc.view.v1`，`solver_host = planner`，信息年龄见信封 `info_age_ns`）：

```json
{
  "now_ms": 63400,
  "agents": [
    {"agent_id": "agt_00001", "entity": "uav_00001", "pos_mm": [214350, 380020], "capabilities": ["rgb.zoom"],
     "status": "ACTIVE", "claims": ["task_00007"], "report_t_ms": 63000},
    {"agent_id": "agt_00003", "entity": "uav_00003", "pos_mm": [455100, 238900], "capabilities": ["lidar.scan"],
     "status": "ACTIVE", "claims": [], "report_t_ms": 62000}
  ],
  "tasks": [
    {"id": "task_00011", "status": "open", "pos_mm": [455900, 231700], "service_ms": 20000,
     "required_capability": ["lidar.scan", "rgb.zoom"], "required_agents": 2, "sync_window_ms": 3000,
     "e_ms": 51200, "l_ms": 213200, "value": 5, "predecessors_done": true}
  ],
  "obstacles_ref": "world/planar.json"
}
```

`Plan.body`（`drone4d.suite.taskalloc.plan.v1`）：

```json
{
  "per_agent": {
    "agt_00001": [{"task": "task_00007"}, {"task": "task_00011", "t_start_s": 98.0}],
    "agt_00003": [{"task": "task_00011", "t_start_s": 98.0}]
  },
  "infeasible": [],
  "compute_units": {"evaluations": 1840}
}
```

两个计划步带相同的 `t_start_s`，执行器按 X-04 使两机在等待环上保持并同时进入服务圆，满足 `w_j = 3 s` 的同步窗口。

---

## 12. 有效性声明要点（`validity.md`）

| 条目 | 内容 |
|---|---|
| 模型不反映的现实因素 | 机间避碰与空域冲突（高度层分层，R-S1-02）；任务需搜索发现与虚警（由 S2 承担）；载荷作业的物理过程（服务以停留时长抽象）；风场（未进入任何档位，§15.1 第 15 条）；通信模型未经实测校准（`synthetic-stress`） |
| 平台提供的公共信息 | 公告板（P-11）：任务到达立即公开、完成与过期状态延迟 5 s 公开；该信息不计费，对全部策略相同，使 `Δ_cut` 只度量对交互网络消息的依赖 |
| 缺省参数来源 | 实体速度与加速度、服务时长、价值分布、`λ_d`、同步窗口取自 n03 §3.4 的初值与本文设定，pilot 校准记录见 pilot 报告 |
| 生成分布的条件 | 任务位置以"最晚可能到达时仍可由初始编制完成"为条件抽取（G-06(3)），远离初始编制的位置被部分拒绝；前置关系只从初始任务引出（R-S1-08、G-07）；实例内故障对象以"故障后每种能力仍有持有者"为条件选取（G-08）。三者保证分母中不存在结构性不可完成的任务，代价是生成分布不是无条件的均匀或簇状分布 |
| 宿主约定的影响 | 分区期间规划 Agent 与两组均断开（X-01）：`planner` 宿主的基线（`hungarian_periodic`、`d2_greedy`）在每次分区期间完全失去协调能力，其在 `abstract-radio@1` 与分区下的降幅含宿主隔离成分，以研究对照条件 `station_side = joined` 单独估计；平台基线统一采用联盟汇合约定 `rv-slot@1`，每个联盟任务至多增加 `T_rv` 的等待 |
| 已知局限 | 一跳往返下限 `2 × Δd + 网络时延`，同周期多轮拍卖不可表达；`d0` 参考值不含决策周期空等（容差分析见 §9.4）；`static_partition` 的联盟组队为公开约定而非最优；Contract Net 与 D2 贪心为既有实现的适配，与原文设定的差异见基线卡；`d2_greedy` 在 `d2` 上不承载联盟与有前驱任务 |
| 协作增益的筛选性 | PA-2、PA-3 要求协作有利，`Δ` 族量级只在 S1 内可比，不跨 Suite 比较；负协作区域结论限定于声明的参数区间（AWR-06 §7.0） |
| 估计目标 | `family`：结论推断至生成分布，不推断至生成器参数空间之外 |

---

## 13. 工作包与追溯

### 13.1 工作包落点

| 工作包 | 本文内容 | 周次 |
|---|---|---|
| WP-D4-14 | 生成器 `gen/ta_poisson@1`、评测器 `taskalloc@1`、`TaskMonitor`、执行器 `ta-exec@1`、`ref/ta_d0_dp@1`、9 个基线登记项、北极星配置；第 3 周末先交付 `d0`/`d1` 生成器、`nearest_greedy` 与 `U_TA` 评测器 | 第 1–4 周（工作量风险见 §15 第 14 条） |
| WP-D4-43 | `d1/n20` 竖切 | 第 4–5 周 |
| WP-D4-13 | 第 8 周冻结参考缓存格式并缓存 `static_partition`、`rolling_milp` | 第 8–10 周 |
| WP-D4-17 | S1 pilot 与 `d2/n16` 可行性子集（§9.2）、PA-1 至 PA-7、R-S1-18、R-S1-23、R-S1-24、AC-S1-21(b) | 第 9–10 周 |
| WP-D4-21 | `holdout` 区块冻结、AC-S1-07 | 第 14–17 周 |
| WP-D4-19、20 | X1 接入（§6.6、AC-S1-18：`U_cum`、`Held(o)`、`x1.response_ops`、候选排除与族分配）、`Δ_perm`（R-S1-20） | 第 12–14 周 |
| WP-D4-26 | TA-C：`d3`、CF-1 至 CF-6、3 个分组基线、机巢、能量与成员变化规则（§5.5，AC-S1-17） | 第 14–16 周 |
| WP-D4-24 | 算法隔离模式的 PettingZoo/BenchMARL 映射（AC-S1-20） | 第 15–17 周 |
| WP-D4-51、52 | `n32`、`d4`、`n64` | 第 17–19 周、第 22–23 周 |
| WP-D4-35 | 异构实体编队实例（UGV 等加入 S1） | 第 24–26 周 |

### 13.2 追溯

| 本文条目 | 上游依据 |
|---|---|
| §2 形式化、§2.7 分类 | AWR-06 §7.1；r02 §3.1、§3.2；R-D4-03、R-D4-09 |
| §3 实例字段 | AWR-06 §4.6、§7.1；r02 §3.1 实例字段；M31 §7.3、§7.4、§8.2 |
| §4 生成器 | AWR-06 §5.5、§7.0；M31 §7.3、§7.7；r02 §6.1 |
| §5 分级 | AWR-06 §7.0、§7.1、§5.10、§5.11；n03 §3.4 |
| §6 运行模式与通信档 | AWR-06 §4.3、§5.1、§5.4、§6、§8.3；ADR-D4-14、17、20；M30 R-M30-18 至 R-M30-22；M31 R-M31-93 至 R-M31-96；X1 §2.2、§5、§8.2、§8.3、§9、§12.3 |
| §7 指标 | AWR-06 §7.1、§8.4、§8.5；r02 §2.2、§3.1、§3.2；M32 R-M32-01；`py:swarm/allocation/score.py` `path_score` |
| §8 基线 | AWR-06 §7.1；M30 §6.7.1、§6.7.2；ADR-D4-27；r03 R31 |
| §9 评测协议 | AWR-06 §7.0、§8.6；ADR-D4-23、24 |
| §10 验收 | AWR-06 AC-S1-1 至 AC-S1-6、AC-D4-05、11、16、17、28、33 |
| §12 有效性声明 | AWR-06 §7.0；r03 R15 |

---

## 14. 风险与待决

### 14.1 风险

| 编号 | 风险 | 触发指标 | 处置 |
|---|---|---|---|
| RK-S1-01 | `abstract-radio@1 default` 的 NLoS 苛刻度使 `d2` 协作基线退化至零通信水平，PA-2 不满足 | pilot 中 `cbba − static_partition` 配对差 < 0.05 | 按 R-S1-18 先在 PA-7 范围内调整 `d2` 的障碍覆盖率与布局并记录；网络参数集的调整只能由 WP-D4-17 汇总三个 Suite 证据后提请负责人裁定（§15.1 第 20 条），S1 不单方面调整 |
| RK-S1-02 | 4 KiB/s/实体的字节额度使 `cbba`、`ssi` 频繁被拒绝发送，基线表现主要反映预算而非算法 | R-S1-23 不满足 | 调整基线共享周期与编码；仍不满足时 pilot 报告提出 `B_cap` 调整建议，由负责人裁定（`B_cap` 属资源包，变更为 Suite 主版本） |
| RK-S1-03 | `rolling_milp` 在 `n16`、`d2` 下 CPU 超额或解质量差，`π_central` 低于 `cbba`，E-01 失败 | pilot 中 E-01 违反或 R-S1-24 不满足 | 按 R-S1-24 的回退序列调整 `node_limit` 与 `horizon_s`；给出机制解释；条目卡标注求解质量（ADR-D4-27） |
| RK-S1-04 | `λ_d = 0.01 s⁻¹` 使 `U_TA` 整体偏低，PA-4 下限饱和 | ≥ 25% 实例上有 AC-S1-04 范围内的基线 `U < 0.05` | 冻结前调整 `λ_d` 或 `partial_credit`（主版本前允许） |
| RK-S1-05 | `rv-slot@1` 的槽等待与退回重试使联盟任务的时效损失过大，或基线未正确实现约定，`d2` 区分度主要来自单机任务 | AC-S1-21(b) 不满足 | 先以 AC-S1-21(a) 的用例核对基线实现；再按 PA-7 调整联盟比例或服务时长；`T_rv` 只可在冻结前按 pilot 证据下调至 5 s（须同时满足 Δd 整数倍与退回重试耗时约束），`w_j` 保持 3 s |
| RK-S1-07 | G-06(3) 的水平可行性条件在 `n16` 的稀疏能力分布下拒绝率过高，任务位置分布明显偏向初始编制附近 | 生成器统计的位置重抽占比 > 20% | pilot 报告各档位重抽占比；超标时下调 `H_arr`（如 240 → 220 s）以放宽 `H − H_arr`，属生成器公开参数调整（PA-7） |
| RK-S1-06 | WP-D4-14 工作量不足（§15 第 14 条） | 第 3 周末竖切所需项未交付 | 按 §15 第 14 条的拆分顺延 SSI、合同网、周期匈牙利与 MILP 至第 8 周前 |

### 14.2 待决

| 编号 | 问题 | 建议（未答复时按此执行） |
|---|---|---|
| Q-S1-01 | 公告板的完成状态公开延迟取 5 s 还是不公开 | 取 5 s；不公开会使全部策略长时间追逐已完成任务，公开且无延迟会使通信价值过低 |
| Q-S1-02 | `partial_credit` 是否在 v1.0 启用 | 不启用（0），pilot 证据充分时再议 |
| Q-S1-03 | 已关闭。M31 v0.2 已按视图按文件划分：不公告的分区置于 `hidden/comm_truth.json`，参考文件置于 `hidden/reference/`、锚点期望值置于 `hidden/eval/`（M31 §7.4.1、R-M31-26、32、33），`_mm_s2` 已登记（M31 §7.3.3）；本文 §3.1 与 §11 已按此修订。`validation` 只分发 `brief` 与 `scheduled`，不含 `J_star` | — |
| Q-S1-04 | 北极星配置是否施加 `B_cap` | 施加（与主榜同一资源包定义），`abstract-disk@1` 的 128 kbit/s 上行速率另行生效 |

---

## 15. 对 AWR-06 的反馈与对并行文档的提请

本文正文中"§15 第 N 条"均指 §15.1 第 N 条。

### 15.1 对 AWR-06 的反馈

| 编号 | 问题 | 建议 | 理由 |
|---|---|---|---|
| 1 | §7.1 记号冲突：`a_j` 同时表示到达时刻与时间窗起点（`t_j = (a_j, …, [a_j, b_j], v_j)`）；`ρ` 同时表示服务半径与任务/智能体比；`d_j` 与难度档 `d` 同名；`λ` 同时表示到达率与衰减率 | 采用本文 §1.4 的记号：`t^a_j`、`[e_j, l_j]`、`r_srv`、`ρ_T`、`δ_j`、`λ_a`、`λ_d`；时效折扣起点改为 `t0_j = max(t^a_j, e_j)`，终点由完成时刻 `τ_j` 改为服务开始时刻 `τ^s_j`（v0.2 增补），即 `δ_j = 1[τ_j ≤ l_j] · exp(−λ_d · max(0, τ^s_j − t0_j))` | 消除歧义；以开窗时刻为起点避免惩罚开窗前的必然等待；以服务开始时刻为终点避免服务时长成为任何策略都无法避免的常数损失（`λ_d = 0.01`、`s_j ∈ [10, 30] s` 时以完成时刻为终点的 `δ_j` 上界仅 0.74–0.90，PA-4 的上饱和检验因此失去意义）；二者均与 `py:swarm/allocation/score.py` `path_score` 的口径一致 |
| 2 | `U_TA` 中 `q_j`"服务时长完成比例"只对未完成任务有意义，而未完成任务的 `τ_j` 无定义，`d_j` 无法计算；服务是否须连续、中断如何处理亦未规定 | 采用 §7.1：完成时 `q_j = 1`；未完成时 `q_j = partial_credit · σ_j / s_j`，缺省 `partial_credit = 0`，时效因子取最大服务段结束时刻；服务须连续，中断清零（P-07） | 使主指标对任意 trace 有定义；缺省不给部分得分以免"开始而不完成"的激励，并保持 `d0` 穷举最优的精确性 |
| 3 | §7.1 完成判定缺参与者规则：未规定是否须持有承诺、多个任务服务圆重叠时如何计入、早到者等待对 `Δt_sync` 的影响、超额参与者如何处理 | 采用 §2.4 的 P-01 至 P-09（须持有有效承诺；唯一计入；`spread > w_j` 时不能开始，须退出再进入；冗余参与只计入重复执行率） | 判定规则决定 `U_TA` 与"无效承诺率""重复执行率"的含义，且须可被第三方独立实现 |
| 4 | §7.1 称"iTax 依赖类别 P0 覆盖 ND 与 ID，XD 在 TA-C"，但 `d2` 已含要求两种互补载荷 3 s 内同步到场的联盟任务与前置关系，属跨调度依赖 XD；`d0`–`d1` 每个智能体须服务多个任务，效用依赖其自身调度，属 ID 而非 ND | 改为"`d0`–`d1` 为 ID，`d2` 为 ID + XD，TA-C 加深 XD；不覆盖 ND 单独情形与 CD"；Gerkey–Matarić 条目保留 [待核实页码] | 分类定位与 iTax 定义（Korsah 等 2013 [已核]）一致，避免论文中出现可被审稿人指出的分类错误 |
| 5 | §7.1 难度表把网络档写入难度档（`d0` 用 `ideal@1`、`d1` 用 `abstract-disk@1` 或 `abstract-radio@1`、`d2` 用 `abstract-radio@1 default`、`d4` 用 `abstract-radio@1 severe`），标准实例字段含 `communication_profile`；而网络档是赛道坐标，主榜恒为 `abstract-radio@1 default` | 难度档只定义实例内在旋钮（§5.1）；表中网络档改为"锚点运行或教学用法"；删除实例字段 `communication_profile`；`severe` 作为 P2 研究赛道 `dc-l1-b1-radio-severe`，不作为 `d4` 的属性 | 与 M31 OR-3、OR-4 及其第 7 条反馈一致；保持五维正交，使同一实例可在多网络档下配对运行 |
| 6 | §6.7 规定任务承诺"平台只记录，不仲裁冲突"，未规定承诺对其他 Agent 是否可见；若可见，承诺即成为不计费的信息通道，可绕过团队字节预算并使 `Δ_cut` 失真；任务完成状态是否公开亦未规定 | 在 §6.7 明确：承诺类交互的事件只回传承诺方；任务完成与过期状态以 Suite 声明的固定延迟经公告板公开（S1 取 5 s），并写入有效性声明 | 封堵侧信道；公告板对全部策略相同，其存在影响通信价值的量级，须显式声明 |
| 7 | 字节预算的执行方式不一致：§6.3 与 `TxReceipt.reason = budget` 表明超额消息在提交时被拒绝，而 §4.4 Suite 清单的 `budget.policy: violation_invalidates` 与 `dims[tx_bytes]` 并列，读作超额即整局无效 | 在 `budget.dims[]` 增加 `enforce ∈ {refuse, violation}`：`tx_bytes` 与 `tokens` 为 `refuse`（拒绝且不记违规），`cpu_ms` 为 `violation` | 字节使用可在提交时精确判定，拒绝使后果落在策略自身；CPU 超额只能事后判定 |
| 8 | 集中式参考命名不一致：§4.4 实验配置为 `reference: {central: rolling_hungarian_central}`，Suite 清单为 `ref/ta_rolling_milp@1`，§7.1 与 M30 为滚动 MILP（`rolling_milp@1`） | 统一为 `rolling_milp`（基线标识 `baseline:taskalloc/rolling_milp@1`，参考策略 `ref/ta_rolling_milp@1`） | 参考策略是 Δ 族与 `G` 的锚点，命名不一致会导致缓存键与条目卡错配 |
| 9 | 无实体 Agent（规划者、地面站）在 `abstract-radio@1` 下的通信端点位置未定义，而路损与 NLoS 判定需要端点位置；`B_cap = n_agents · duration · 4096` 会因增加规划 Agent 而放大团队额度 | 规定无实体规划 Agent 须绑定一个 `edge_node` 实体作为通信端点，位置由实例给出（S1 为地面站 `gs_00001`）；`B_cap` 只按任务实体数计算 | 使 `planner` 宿主的通信受同一物理模型约束，避免"增加一个 Agent 换取预算"的漏洞 |
| 10 | `d2`"`t = 150 s` 一架失去任务执行能力"的语义未定义：是否静默、是否仍能飞行与通信、已持有承诺如何处理 | 采用 §5.1 的 `payload_loss` 语义：能力位清零，仍可飞行与通信，承诺保留，本机经 `self_state` 得知，他机只能经消息得知；静默整机失效归 `d3` 与 X1 F 族 | 故障语义决定 `realloc_latency_s` 的含义与 `Δ_cut` 的大小 |
| 11 | `d2`"外生分区"的时长、次数与分组方式未定义；若按位置划分，分组随策略变化，破坏共同随机数 | 以 Agent 身份做均衡二分，60 s，起始时刻 U{90..150} s，由实例固定（G-08）；地面站（规划 Agent）不属于任一组，并在 §7.1 注明该约定使 `planner` 宿主在分区期间完全隔离，要求各 Suite 在 `validity.md` 与基线卡中声明、在研究运行中提供"站点归入一组"的对照条件（S1 为 `station_side = joined`） | 保证跨策略配对（RN-2）与 X1 P 族口径一致；宿主约定对 `planner` 型基线的系统性影响须显式声明并可单独估计，否则 E-05 一类降幅排序会把宿主约定误读为算法差异 |
| 12 | §5.5 把任务到达列为运行期 `task` 随机域，而 §4.6 与 M31 把任务流放入实例的 `scheduled` 视图，二者对"到达时刻属于实例还是属于外生种子"的规定不一致 | 采用 §4.6：任务池（位置、需求、价值、相对时间窗、前置、到达次序）属实例；Poisson 到达的具体时刻由 `task` 域按外生种子实现；`static` 与 `scripted` 到达为实例定值。相应地在 §7.1 规定时间窗以相对偏移存储、运行期统一截断到 H（`l_j = min(t^a_j + l^off_j, H)`），生成期的可行性检验只使用与到达实现无关的充分条件（本文 G-06、G-07：以最晚可能到达时刻为参照的水平可行性；前置关系只从初始任务引出） | 既保持 `d0` 锚点的可穷举性，又使同一场景的不同外生种子产生时序变化；同时使 `validation`"输入公开"不泄露具体到达时刻。到达时刻在生成期未知，依赖绝对时刻的可行性检验在生成期无法执行 |
| 13 | §5.10 北极星分项预算假设 `cbba`"每个决策周期每 Agent 至多一轮捆绑构建与一轮冲突消解"，而主赛道 `d4_std` 的 Δd = 100 ms、`B_cap` 折合每实体每 tick 约 410 B，广播展开为 n−1 条计费单播后逐 tick 共识不可行 | 在 §7.1 增加"基线须满足预算可行性"的约束（本文 R-S1-23），`cbba` 的共识消息周期 ≥ 1 s；北极星与主赛道的 `cbba` 参数分别登记 | 否则 PA-2 的代表性协同基线在主赛道上主要反映预算拒绝而非算法 |
| 14 | WP-D4-14 在第 1–4 周以 3.0 人周交付生成器、评测器、7 个基线（含合同网适配与滚动 MILP）与北极星配置，本文另需执行器、在线任务监视器与 `d0` 穷举参考；合同网适配（M30 R-M30-38 的门面、网络适配与确定性排空）工作量较大 | 拆分：第 1–4 周交付生成器、评测器、`TaskMonitor`、执行器（含 v0.2 增补的汇合约定 `rv-slot@1`）、`static_partition`、`nearest_greedy`、`cbba`、`random` 与 `d0` 参考；`ssi`、`contract_net`、`hungarian_periodic`、`rolling_milp`、`d2_greedy` 顺延至第 8 周前（WP-D4-13 缓存 `π_central` 之前）；或将 WP-D4-14 估算上调至 4.0 人周 | 北极星与竖切只依赖前一组；第 8 周前完成后一组不影响关键路径 |
| 15 | `d3` 列入"风场"，但 `planar@1`（M28 R-M28-51 至 R-M28-55）无风场模型 | 二选一并在 P1 前裁定：在 `planar@1` 增加由 `weather` 域驱动的地速修正（属内核语义变化，须登记为新物理档版本 `planar@2`）；或将风场移至 `kinematic` 档的 TA-C 扩展场景集 | 避免 `d3` 档位依赖一个未定义的物理能力 |
| 16 | AC-S1-2 只要求 `d2` 上 `U_central ≥ U_CBBA` 的点估计，而 `π_central` 在集中式参考列、CBBA 在分布式主榜，二者的网络与拓扑条件不同；`π_central` 不经网络传递指令，其结果对网络档不敏感 | 在 §7.1 注明 E-01 比较的是"集中式参考列对主榜条目"，并增加"`π_central` 对网络档不变"的一致性检查（本文 AC-S1-10） | 使序关系检查的对象与条件明确，并为 `R_central` 的解释提供前提 |
| 17 | `InteractionEvent.status` 枚举（`GRANTED`、`DENIED_BUSY`、`DENIED_DISTANCE`、`DENIED_CAPABILITY`、`PREEMPTED`、`EXPIRED`）缺少"任务未到达或已关闭"的拒绝原因；§6.7 的任务承诺"只记录"，但 `GRANTED` 易被读作仲裁通过；也未规定承诺数上限 | v0.2 修订：与 M29 R-M29-75 对齐，采用单一状态 `DENIED_INVALID` 并以 `InteractionEvent.detail` 区分 `not_arrived`、`unknown`、`closed`、`actor_inactive`、`not_held`（不再单列 v0.1 提议的 `DENIED_UNKNOWN`、`DENIED_CLOSED`）；承诺数上限沿用既有 `DENIED_BUSY`（`detail = claim_limit`）；承诺登记成功的状态名由契约草案 FB-CT-06（`RECORDED`）定稿。S1 在定稿前按 §2.4 的临时映射表执行 | 区分策略缺陷（承诺未到达任务）与正常竞争，便于诊断与无效承诺统计；一个拒绝原因用一个状态加细节字段表达，避免各模块各自增补枚举 |
| 18 | §7.1 `d0`"典型参数（参考规模 n8）"列写"4 智能体、8 个静态单机任务"，与 n8 参考规模及恒密度规则矛盾；n8 的穷举（16 任务、8 智能体）计算量不可接受 | 明确 `d0` 只在 `n4` 定义且固定 `|T| = 8`，不随规模缩放；`d0` 不成榜 | 保持"可手算或穷举"的定义可执行 |
| 19 | §7.0 PA-4"没有基线的 `U > 0.95` 或 `U < 0.05`"未限定基线范围；`π_random` 在时间窗约束下大量过期、只承载子集的适配器基线（S1 的 `d2_greedy`）按设计返回不可行，二者的下饱和是预期行为，按字面执行会使 PA-4 恒不满足 | 把 PA-4 的对象限定为 `π_ind`、`π_central` 与主榜官方基线，`π_random` 与附加基线只在 pilot 报告中列出；各 Suite 在基线卡中声明范围 | 非饱和检验的目的是保证有意义的基线落在可区分区间，不应被构造上必然饱和的对照基线触发 |
| 20 | §6.5 量级自检规定 NLoS 苛刻度不满足时"下调 `A_NLoS` 或 `n_NLoS`（WP-D4-17）"，而 §7.0 PA-7 只允许调整生成器公开参数（到达率、截止期、联盟比例、遮挡率、目标速度比、预算比），二者对 pilot 是否可调整网络参数的规定冲突；网络参数集是全部 Suite 共享的赛道坐标，由单个 Suite 的 pilot 调整会改变 S2、S3 的主榜条件 | 在 PA-7 中明确：Suite pilot 只调整实例旋钮（含遮挡率与布局）；网络参数集的调整列为 PA-7 的例外，只能由 WP-D4-17 汇总全部 Suite 的 pilot 证据后由负责人裁定，按网络参数集版本变更处理，并同步复核 S2、S3 的区分度 | 保持网络档为跨 Suite 共享的赛道坐标；避免 S1 单方面改变其他 Suite 的主榜条件 |
| 21 | §7.0 的 S1 pilot 配置只有 `d1/n20` 与 `d2/n8`，而 S1 的预算与 CPU 可行性约束（本文 R-S1-23、R-S1-24）须在最大 P0 规模 `d2/n16` 下确认，`holdout` 的 `{d1, d2} × {n8, n16}` 中 3 个档位的 ICC 与 `σ_D` 未被估计，样本量规则依赖其上界 | 在 S1 pilot 中增加 `d2/n16` 可行性子集（5 实例 × 2 种子，全部官方基线与参考策略，约 2.3 宿主小时/轮），只用于预算与 CPU 可行性；ICC 以已估计配置的上界外推并在留出集校准中复核。若负责人要求完整覆盖，`d2/n16` 按 10 × 10 执行约需 22.5 宿主小时/轮（n16 单 Episode ≤ 90 s，约 9 个基线），3 轮约 67.5 宿主小时，须相应调整 §8.5 的 pilot 算力预算（现为至多约 135 宿主小时，覆盖 3 个 Suite） | pilot 覆盖不到的规模档上，"预算可行"与"样本量充分"只是假设 |
| 22 | §8.3 预算档 B1 写"CPU 时间总额 ≤ 每仿真秒 100 CPU-ms（团队额度为 N 倍）"，对集中式参考与规划 Agent 的额度存在两种读法（团队额度的 N 倍即 N² 倍，或与团队额度相同） | 改写为"每个任务 Agent 每仿真秒 100 CPU-ms；团队额度 = N × 每仿真秒 100 CPU-ms，N 为任务 Agent 数；集中式参考与无实体规划 Agent 不设单独额度，计入团队额度" | 集中式与分布式条目须在同一资源包下比较（AWR-06 §8.5 资源预算对齐），否则 `G` 与 `R_central` 失去可比性 |
| 23 | §7.1 的 S1 基线清单未规定联盟任务的同步到场如何实现；`w_j = 3 s` 下若各基线各自处理或不处理同步，`d2` 联盟任务（约 25% 价值）对多数闭环基线近乎不可达，PA-2 的配对差只来自单机任务，AC-S1-2 的序关系也受同步实现质量而非分配质量支配 | 在 §7.1 注明：平台基线统一采用 Suite 声明的零消息联盟汇合约定（S1 为 `rv-slot@1`，本文 R-S1-28），并在 pilot 判据中增加联盟任务完成率与同步质量的验收（本文 AC-S1-21） | 使基线间差异来自分配决策；同步协议的设计空间留给研究者条目 |
| 24 | §7.1"分母含全部应完成任务"中"应完成"的范围不明确；`d2` 的定时载荷失效若使某类能力失去全部持有者，部分任务在故障后结构性不可完成且随实例随机波动，`U` 的可达上界因实例而异 | 明确分母为实例全部任务，并要求生成器保证：每个任务在任意到达实现与全部实例内故障之后仍能由初始编制完成（能力覆盖、时间窗与前置可行）；实例层可完成价值上界写入客观难度量（S1 为 `u_feasible`），运行期扰动下的上界作为诊断量报告（S1 为 `u_ub_cap`） | 避免实例间丢分由故障对象选择的随机性决定，使 PA-4 的饱和判断有统一参照 |

### 15.2 对并行文档的提请

| 编号 | 对象 | 条款 | 提请内容 | 理由 |
|---|---|---|---|---|
| FS-01 | M28 | R-M28-55；§6.5.3 第 10 步 | 能量积分增补充电项：实体处于停靠且机巢供电时 `energy_uwh' = min(energy_cap_uwh, energy_uwh + round(P_charge · Δp_s / 3600 · 10⁶))`，`P_charge` 为 Suite 属性；停靠期间运动类指令回执 `docked`；停靠状态写入真值层 | S1 `d3` 的 R-S1-30、R-S1-31 依赖；M29 R-M29-72 已引用"充电速率由 M28 能量模型置 0"，但 M28 未定义充电 |
| FS-02 | M28 | `/motion/planar/r_arrive_m` | 把 `r_arrive` 的缺省值（1.0 m）列为 `planar@1` 版本语义的一部分，变更须递增物理档版本 | S1 `d0` 参考时序模型（§9.4）与 `rv-slot@1` 的同步误差界都以该值计算 |
| FS-03 | M29 | R-M29-75；§7.3 状态表 | 任务承诺增加：承诺方实体非 `ACTIVE` 时拒绝（`DENIED_INVALID`，`detail = actor_inactive`）；Suite 可声明承诺数上限 `claim_max`，达到上限时 `DENIED_BUSY`（`detail = claim_limit`）；`DENIED_INVALID` 的 `detail` 取值集合含 `not_arrived`、`unknown`、`closed`、`not_held` | 与 S1 §2.4 对齐；避免各 Suite 自定义枚举 |
| FS-04 | M30 | R-M30-22；§6.4 | "执行器（M31 提供）"改为"执行器协议由 M31 规定（R-M31-96），实现由各 Suite 提供" | 与 M31 R-M31-96 及 §9.4 E-08 的工作量划分一致 |
| FS-05 | M30 | §6.7.1 | (1) 登记联盟汇合约定 `rv-slot@1`（S1 R-S1-28）为全部 S1 平台基线的共用组件，实现置于 `motion` 公共模块，与执行器 X-04 同一实现；(2) 注明全部 S1 平台基线只对当前目标任务发出 `task.claim`；(3) `d2_greedy` 的适用范围改为"`d0`–`d1` 全部、`d2` 单机且无前驱子集"；(4) `rolling_milp` 增补 R-S1-24 的确定性参数回退序列；(5) `cbba` 的"每决策 tick 一轮共识"与 S1 R-S1-23 的"共识消息周期 ≥ 1 s"不一致，主赛道参数以 R-S1-23 为准 | 基线卡与本文 §8 一致 |
| FS-06 | M31 | R-M31-93；§5.11 | S1 以 `phase_participants()` 登记 `TaskMonitor`（`WORLD_UPDATE`），以 `register_events()` 登记 `suite.taskalloc.task_state`（rank 110，载荷 `{task_id, state ∈ {open, in_service, interrupted, done, expired}, members[], t_ns}`）；以 `attr_defaults()` 登记 `/suite/taskalloc/station_side`（缺省 `isolated`，排名运行不得覆盖）；`ArbiterRules` 登记机巢表与 `r_dock` | S1 运行时接口的完整清单 |
| FS-07 | M31 | §13 第 14–17 项 | 已在本文 v0.2 处理：`instance_seed_ref.value` 为十进制字符串、`generator.digest` 删除、参考文件移入 `hidden/` 并加 `nonce`（§11.1）；不公告分区移入 `hidden/comm_truth.json`（§3.1、§11.2）；`d0c` 改为 `d0` 档内的锚点区块 `ac`（§4.4、§6.1、§7.5）；`s1` 区块删除（§4.4） | 回执 |
| FS-08 | X1 | §8.2 S1 行；§9；§12.6 第 16 条 | (1) 确认 S1 的 `Held(o)` 保持 X1 现口径，`realloc_latency_s` 的 `A_f` 为其失去覆盖的子集，二者不必同口径（X1 §8.2 S1 行"与 `realloc_latency_s` 的受影响任务集合同口径"一句请改为"`A_f ⊆ Held(o)`"）；(2) 确认 S1 的族分配：`d3`（TA-C）宿主只施加 H、P、L、J 族，F、C、M 族只施加于 `d1`、`d2` 宿主；(3) 确认 `π_central` 在 M 族下由集中决策主体接管替补实体；(4) X1 §12.6 第 16 条已在本文处理（AC-S1-18 族集合、删去"路径改变"、`perturbation.json` 结构、`U_cum(t)` 与 `Held(o)` 声明） | X1 R-RC-49 要求宿主声明参考策略行为 |
| FS-09 | 契约草案 | §4.3.4；FB-CT-06 | S1 撤回 v0.1 提议的 `DENIED_UNKNOWN`、`DENIED_CLOSED`，改为 `DENIED_INVALID` 加 `detail`（§15.1 第 17 条）；请在 `interaction.v1` 中登记 `detail` 字段与 S1 的取值；登记 `suite.taskalloc.task_state` 载荷 schema 与 `drone4d.suite.perturbation.v1` 的 S1 用法 | 契约草案 §4.3.4 当前同时列入两套提议 |

---

## 16. 评审处理记录

核实方法：逐条打开所引条款核对（AWR-06 v0.2、M28、M29、M30、M31、M32、X1、契约草案的当前工作区版本，`py:swarm/allocation/score.py` `path_score`）；涉及数值的意见以本文 §9.4 的参考时序模型离线重算。

| 序号 | 意见（章节，严重度） | 核实结论 | 处理 | 修改位置 |
|---|---|---|---|---|
| 1 | 硬截止时间窗在生成期不可检验（§2.3、§4.3 G-06/G-07、§11.2；high） | 属实。`poisson` 非初始任务的到达时刻在生成期未知，v0.1 的"`l_j ≤ H` 不满足时重抽"与 G-07 的前驱窗口检验均依赖到达时刻；`κ_hi = 6` 时偏移本身可超过 H | 采用方案 (a) 的截断形式：运行期 `l_j = min(t^a_j + l^off_j, H)` 对硬、软截止统一适用；G-06 改为与到达实现无关的充分条件——紧度条件只用相对量，水平可行性以参考到达时刻（非初始 `poisson` 任务取 `H_arr`）检验，并给出对任意到达实现 `l_j − t0_j − s_j ≥ t_reach_j` 的证明；G-07 的前驱只从初始任务（到达时刻恒为 0）中选取，条件只含相对量；`kappa_mean` 明确为截断前名义值，新增 `trunc_frac_ref` 与运行期 `kappa_eff_mean`；R-S1-13 与 AC-S1-15 增加复核与 100 个到达实现的抽样验证；§11.2 样例的前驱改为初始任务并给出校验算式 | §0、§1.4、R-S1-07、R-S1-08、R-S1-13、§4.1、G-05 至 G-07、§4.5、§7.2、AC-S1-15、§11.2、§12、RK-S1-07、§15.1 第 12 条 |
| 2 | `d2` 故障后部分任务结构性不可完成（§7.1、G-06、G-08、R-S1-03；medium） | 属实。`n4` 的 2/1/1 分配下独占载荷实体失能后，150 s 后到达的需该载荷任务必然为 0；X1 F 族同理 | G-08 约束故障对象：移除全部实例内故障对象后每种能力仍至少有 1 个持有者（联盟任务所需能力位互不相同，每类 1 个即保证单任务可行；评审建议的"至少 `max_m` 个"强于必要条件，未采用）；G-06 的 `t_reach_j` 只在整局保持能力的实体上计算；`difficulty{}` 增加 `cap_scarcity_post` 与 `u_feasible`；运行期 X1 扰动下的上界以诊断量 `u_ub_cap` 报告，PA-4 与 X1 退化曲线以其为参照；X1 候选对象排除实例内故障对象 | G-06、G-08、§4.5、§7.1 性质段、§7.2、§6.6、§11.2、§15.1 第 24 条 |
| 3 | 以完成时刻折扣使 PA-4 上饱和检验空真（§7.1、AC-S1-04；medium） | 属实。`path_score` 以服务开始时刻为折扣终点（`t_start = max(t_open, 到达时刻)`），v0.1 与之相差 `exp(−λ_d s_j)`，`s_j ∈ [10, 30] s` 时上界 0.74–0.90 | 折扣终点改为服务开始时刻 `τ^s_j`（未完成任务取最大服务段的开始时刻）；R-S1-25 的价值换算删除；`d0` 动态规划递推相应修改；偏离 AWR-06 §7.1 的部分写入反馈 | §0、§7.1、R-S1-25、§9.4、§11.1、§15.1 第 1 条 |
| 4 | 闭环基线无同步协调协议，联盟任务近乎不可达（§8.1、§8.3、RK-S1-05；high） | 属实。M30 §6.7.1 对各基线均无同步规则，v0.1 只有算法隔离执行器的等待环 | 新增 R-S1-28 统一汇合约定 `rv-slot@1`（零消息、基于公开时钟的汇合槽与退回重试，`π_ind` 可用），给出同步误差界；全部平台基线与执行器（X-04）统一采用；新增 `cr_coal` 与 AC-S1-21（单元用例与 pilot 判据，冻结前须满足）；RK-S1-05 改为针对约定的风险；评审建议的"就绪消息计通信费"方案未作为平台约定（会使联盟同步质量随网络档变化，混入通信效应），保留为研究者条目的设计空间；提请 M30 登记 | R-S1-28、P-05、X-04、§8.1、E-04、§7.2、§7.7、AC-S1-21、RK-S1-05、§12、§15.1 第 23 条、§15.2 FS-05 |
| 5 | `ref/ta_d0_dp@1` 的 `t_tr` 定义不完整，1% 容差不可验（§9.4、AC-S1-01、§11.1；high） | 属实（定义缺失）。补充核实：以本文选定的精确模型（`planar@1` 离散积分含减速曲线、`r_arrive = 1 m`、以首次在物理边界进入服务圆为终点、后继自任务中心出发）重算，v0.1 口径下得 0.72827，与 v0.1 所列 0.7284 在四舍五入范围内一致，说明原值对应该模型但未写明；评审所列 0.7097–0.7362 为不同口径的结果，印证了口径缺失的影响 | §9.4 给出参考时序模型 `planar1-enter@1` 的完整定义（运动、`t_tr` 终点、服务期间行为、后继出发点、不含项）；以新折扣口径重算 `J* = 0.80486`（最优划分不变）；容差分析改为从该模型出发（每跳 +0.3 s、+1 m 时偏差 0.49%），`expected.json` 增加 `dev_bound_rel`；AC-S1-01 增加参考值重算核对；`d0` 增加最小间距约束；X-03 改为服务期间保持 `goto_w` 以与模型一致 | §1.4、§9.4、AC-S1-01、X-03、G-05、§11.1 |
| 6 | `realloc_latency_s` 在联盟与冗余承诺下产生虚假 0 延迟（§7.2；medium） | 属实 | `A_f` 只含 i 为承诺方、且故障后承诺方集合失去覆盖的任务；延迟终点为 `t_f` 之后新授予的承诺使覆盖恢复的时刻；覆盖按 P-04 的能力与人数定义；AC-S1-16 增加联盟搭档与冗余承诺两类反例；与 X1 `Held(o)` 的关系写入 §6.6 | §7.2、§6.6、AC-S1-16、§15.2 FS-08 |
| 7 | 规划 Agent 在分区中完全隔离，系统性惩罚 `planner` 宿主（§6.2 X-01、P-10、§11.2；medium） | 属实（影响属实）。核实 X1 §5.5 对无实体与固定站址端点采用相同约定 | 采用评审给出的"说明依据后保留"选项：排名运行保留隔离约定（与 X1 P 族一致，模拟地面回传与机群同时中断），在 `validity.md` 与基线卡显式声明，E-05 的机制单列该项；增加研究对照条件 `station_side = joined`（规划 Agent 归入规模 ⌈n/2⌉ 的组）以分离宿主隔离降幅；`hidden/comm_truth.json` 以 `unassigned` 显式记录 | X-01、E-05、§3.1、§11.2、§12、AC-S1-14、§15.1 第 11 条、§15.2 FS-06 |
| 8 | R-S1-18 下调网络参数与 PA-7 冲突（§8.2/§6.3 R-S1-18、RK-S1-01；medium） | 部分属实。PA-7 的可调参数确不含网络参数，S1 §5.1 首段亦自相矛盾；但 AWR-06 §6.5 量级自检本身规定 NLoS 苛刻度不满足时"下调 `A_NLoS` 或 `n_NLoS`（WP-D4-17）"，v0.1 的写法有上游依据，冲突根源在 AWR-06 内部 | 回归 PA-7：S1 只调整 `d2` 的障碍覆盖率与布局；网络参数集调整由 WP-D4-17 汇总三个 Suite 证据后提请负责人裁定，S1 不单方面决定；§5.1 首段改正；反馈 AWR-06 统一 §6.5 与 PA-7 | §5.1、R-S1-18、RK-S1-01、§15.1 第 20 条 |
| 9 | 参考文件标为 `truth` 却位于 `hidden/` 之外（§3.1，M31 R-M31-26/V-I-09；medium） | 属实（对 v0.1）。核实 M31 当前版本已裁决（M31 §7.4.1、R-M31-32、R-M31-33、§13 第 14、16 项）：参考文件移入 `hidden/reference/`、锚点期望值移入 `hidden/eval/`，不公告分区移入 `hidden/comm_truth.json` | 按 M31 修订：路径改为 `hidden/reference/reference.json` 与 `hidden/eval/expected.json`，登记于 `reference_files[]`，`truth` 文件加 `nonce`；分区移入 `hidden/comm_truth.json`；`validation` 只分发 `brief` 与 `scheduled`，留出集与 `validation` 不下发 `J_star`；Q-S1-03 关闭 | §3.1、R-S1-12、§3.3、§11.1、§11.2、Q-S1-03、§15.2 FS-07 |
| 10 | 执行器与 `TaskMonitor` 在 M31、M28 无对应需求（§6.2、§1.2；medium） | 部分属实。不属实部分：M31 当前版本含 R-M31-93（`phase_participants()` 阶段参与者与 Suite 事件登记）、R-M31-96（`executor()` 返回 `PlanExecutor`）与 §7.2.6 的协议定义，M28 R-M28-19 预登记 `task.arrive`/`task.expire` 并由 Suite 绑定处理器，检索"执行器"在 M31 有多处命中。属实部分：S1 未引用这些需求号；S1 与 M30 写作"执行器（M31 提供）"，与 M31 §9.4 E-08"属各 Suite 规格的工作量"不一致；`TaskMonitor` 未作为阶段参与者登记 | X-06 改为"协议由 M31 规定（R-M31-96），实现由 S1 提供（WP-D4-14）"；`TaskMonitor` 按 R-M31-93 登记为阶段参与者，Suite 事件改名 `suite.taskalloc.task_state`（rank 区段按 R-M28-19）；AC-S1-14 引用 R-M31-96；提请 M30 改正"M31 提供"，向 M31 列出 S1 运行时接口清单 | §1.2、§2.4、X-06、AC-S1-01、AC-S1-14、§15.2 FS-04、FS-06 |
| 11 | 承诺拒绝状态与 M29 不一致（§2.4、§15 第 17 条、M29 R-M29-75；medium） | 部分属实。状态不一致属实（M29 为 `DENIED_INVALID`，S1 v0.1 暂用 `EXPIRED` 并提议另两个状态，M29 无承诺数上限与 `ACTIVE` 检查）；"契约草案文件不存在"不属实：`docs/d4/契约草案.md` 存在，其 §4.3.4 与 FB-CT-06 已收录 M29 与 S1 的提议并建议增设 `RECORDED` | 与 M29 对齐为 `DENIED_INVALID` 加 `detail`；承诺数上限沿用 `DENIED_BUSY`；§2.4 给出带 M29 与契约草案对应列的临时映射表；提请 M29 增补上限与 `ACTIVE` 检查，提请契约草案撤除 S1 旧提议 | §2.4、§15.1 第 17 条、§15.2 FS-03、FS-09 |
| 12 | `reassign` 与 `invalid_claim_rate` 系统性放大（§7.2；low） | 属实 | `reassign` 只计"仍有承诺方且原承诺方有人退出"的变化，另报 `claim_first`；执行器与全部平台基线只承诺当前目标（承诺私有、预承诺无协调收益），`invalid_claim_rate` 因此度量求解器或策略的计划不稳定性；AC-S1-16 增加用例 | X-03、§7.2、§7.7、§8.1、AC-S1-14、AC-S1-16 |
| 13 | 参与者定义循环（P-01(f) 与 P-06；low） | 属实 | 拆为资格（P-01，只依赖 i 与 j 自身状态）与归属（P-02，连续性优先、再按承诺授予时刻与 `task_id`）两步，再定义参与者集合；给出每个边界的求值次序；原 P-03、P-04 合并为 P-04；AC-S1-08 增加三方重叠、授予时刻并列与服务中连续性用例 | §0、§2.4、AC-S1-08 |
| 14 | pilot 未覆盖 `d2/n16`（§9.2、R-S1-23/24；medium） | 属实 | 增加 `d2/n16` 可行性子集（5 × 2，约 2.3 宿主小时/轮）用于预算与 CPU 可行性；ICC 外推假设写入 pilot 报告并在留出集校准中复核；完整覆盖的成本（约 22.5 宿主小时/轮）提请 AWR-06 | §9.2、R-S1-23、R-S1-24、§13.1、§15.1 第 21 条 |
| 15 | `d2_greedy`、PA-4 对象与 R-S1-24"保证"三处不一致（§8.1、E-07、R-S1-24；low） | 属实。核实 M30 §6.7.1 `rolling_milp` 不设 `time_limit`、R-M30-37 同时拒绝联盟与前置任务 | `d2_greedy` 适用范围改为 `d0`–`d1` 全部与 `d2` 单机无前驱子集，E-07 的机制改为联盟与有前驱任务并集约 36%；AC-S1-04 限定为 `π_ind`、`π_central` 与主榜官方基线；R-S1-24 改为实测上界加确定性参数回退序列，不使用"保证" | §8.1、E-07、AC-S1-04、R-S1-24、RK-S1-03、RK-S1-04、§15.1 第 19 条、§15.2 FS-05 |
| 16 | `d3` 能量与机巢规则不完整（§5.1、R-S1-04、§12；low） | 属实。核实 M28 无充电模型，R-M28-55 的 `P0`、`P1` 待 S1 pilot 校准，M29 R-M29-72 只有停靠仲裁；另发现 v0.1 R-S1-04"耗尽实体须经机巢充电"与耗尽后不能运动矛盾 | 新增 §5.5（R-S1-29 至 R-S1-34）：巡航功率参照、停靠协议、充电模型、耗尽不可恢复、成员变化下的 `B_cap`（按计划在场时长积分）与 Jain 遍历范围、可见性；风场从 `d3` 表移除；R-S1-01 增加待命 Agent；R-S1-04 改正；提请 M28 增补充电项 | §5.1、§5.5、R-S1-01、R-S1-04、P-01(a)、§6.4、§7.2、AC-S1-17、§12、§15.2 FS-01 |
| 17 | 未提供 X1 要求的宿主内容（§10 AC-S1-18、X1 §2.2；medium） | 属实。核实 X1 §2.2、§8.2、§8.3、§9 与 §12.6 第 16 条 | 新增 §6.6：适用族、宿主与族的分配（`d3` 宿主只施加 H、P、L、J，避免与实例内 `entity_fail`、`membership` 叠加）、候选对象排除、`U_cum(t)` 精确定义（含部分得分任务在关闭时入账，评测器按同一次序累加使 `U_cum(H) ≡ U_TA` 逐位一致）、`Held(o)`、响应操作、`π_ind` 与 `π_central` 在各族下的行为；AC-S1-18 按 X1 改写（删去"路径改变"）；`perturbation.json` 采用 X1 §12.3 结构 | §1.2、§3.1、§6.1、§6.6、AC-S1-16、AC-S1-18、§13.1、§15.2 FS-08 |
| 18 | `jain_fair` 的 `target` 缺目标区间；CPU 额度表述歧义（§7.7、§6.4；low） | 属实。核实 M32 R-M32-01 与 AWR-06 §8.3 B1 | `jain_fair` 改为 `direction = max` 的诊断量（非 primary、非 tier），并说明诊断量不使用 `target`；§6.4 改写为"每个任务 Agent `H · 100 CPU-ms`，团队额度 `N · H · 100 CPU-ms`，规划 Agent 与集中决策主体计入团队额度"；反馈 AWR-06 改写 B1 | §6.4、§7.2、§7.7、AC-S1-12、§15.1 第 22 条 |

关联修订（非评审意见，核实上游文档时发现，一并处理）：

| 序号 | 内容 | 依据 | 修改位置 |
|---|---|---|---|
| A-1 | `manifest.json` 样例：`instance_seed_ref.value` 改为十进制字符串、删除 `generator.digest`、`files[]` 只登记内容文件并按路径排序、增加 `reference_files[]`、`reference_rev`、`reference_sha256`、`build.impl_sha256`，时刻为字符串 | M31 §8.2、ENC-01、§13 第 14 项 | §11.1 |
| A-2 | `d0c` 改为 `d0` 档内的锚点区块 `ac`；删除 `s1` 区块（`stress` 为 `holdout` 子集） | M31 §13 第 15、17 项；R-M31-47、V-S-08 | §4.4、§6.1、§7.5 |
| A-3 | §11.5 样例中 `agt_00002` 标为 `lidar.scan`，与 R-S1-03 的能力轮转不符（`uav_00002` 为 `thermal.imaging`），改为 `agt_00003`/`uav_00003` | R-S1-03 | §11.2、§11.5 |
| A-4 | v0.1 引用的 `R-S1-40` 在本文中不存在，删除；Suite 事件 `task.state` 按 M28 R-M28-19 的 rank 区段改名 `suite.taskalloc.task_state` | M28 R-M28-19；M31 R-M31-93 | §2.4、AC-S1-01 |
| A-5 | P-01(e) 增加 `t ≤ l_j`，使截止后不再具备资格，与 P-08 一致；G-05 的障碍间距由 `r_srv` 改为 `r_wait`，使等待环不与障碍相交 | P-08；R-S1-28 | §2.4、G-05、R-S1-13 |
