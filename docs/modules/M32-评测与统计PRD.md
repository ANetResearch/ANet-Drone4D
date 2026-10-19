# M32 评测与统计 PRD（Evaluation & Statistics Kernel）

## 文档信息

| 项 | 内容 |
|---|---|
| 文档编号 | AWR-M32 |
| 标题 | 评测与统计内核（Evaluation & Statistics Kernel）PRD：指标规格、层次统计与重采样、成对比较与 Holm 校正、并列组与名次区间、容差支配与强连通分量合并、干预对照编排与 Δ 族、资源账本核算、跨保真度审计、榜单复核，以及与 D3 `aggregate.py` 的兼容委托 |
| 版本 | v0.2.1（2026-10-18 文档集集成同步：按 AWR-06 v0.3 §0.5 统一裁定 XR-02 修订 R-M32-84、AC-M32-24 的解释器版本，按 XR-07 修订 R-M32-69 与 F-40 的承诺编码，§13.2 各项状态按本轮同步结果更新；按 XR-02 把接口草案标注改为 Python 3.12） |
| 日期 | 2026-10-18（UTC） |
| 状态 | D4 设计评审稿（第二轮）。v0.2 按评审意见修订，逐条处理结果见 §14；供负责人与 D4 评测工作线（E 线）评审，不构成实现承诺。D4 实现在 D3 封板（G0）之后启动，本文全部周次以"D3 封板后第 N 周"表述 |
| 上游文档 | AWR-06 v0.2（`docs/06-D4-协同实验平台概念设计.md`）：§3.3（M32 职责与首批符号）、§3.4（B-02、B-07、B-08、B-10）、§4.2（`MetricSpec`、`Evaluator`、`ResourceLedger`）、§4.4（Suite 清单 `scoring` 与 `budget`）、§4.6–§4.7（`board.v2`、`ledger.v1`、`fidelity_audit.v1`）、§5.6–§5.9（确定性等级、墙钟隔离与资源账本、跨保真度判据、复现）、§7.0（pilot 判据 PA-1 至 PA-7）、§7.4（X1 指标）、§8.4–§8.12（指标体系、Δ 族、统计协议、种子承诺、复核、偏差裁决）、§9.1–§9.2（研究方向与论文假设）、§10（WP-D4-09、11、12、13、17、19、20、21、22、27、30、38、40、41；AC-D4-13 至 AC-D4-16 等）、ADR-D4-10、15、19、21、22、23、24、25；调研 `docs/research/d4/r03-NS-2经验与排行榜规则.md` §5.2、§5.4、§6，`docs/research/d4/notes/n02-D3评测子系统泛化.md` §1–§8，`docs/research/d4/notes/n01-现有代码映射.md`；负责人前期稿 design-r2（Δ_coop、Benchmark Suite、三个研究方向） |
| 并行文档 | M28 内核（`docs/modules/M28-实验内核PRD.md`）、M29 交互网络（`M29-交互网络PRD.md`）、M30 协同接口与基线（`M30-协同接口与基线PRD.md`）、M31 基准套件（`M31-基准套件PRD.md`）、M33 Trace 与复现（`M33-Trace与复现PRD.md`）、M34 研究者 SDK 与命令行（`M34-研究者SDK与命令行PRD.md`）、Suite 规格 S1/S2/S3/X1（`docs/d4/suites/`）、协作智能排行榜规格（`docs/d4/协作智能排行榜规格.md`，下称 LB）、首篇论文实验设计（`docs/research/d4/r04-首篇论文实验设计.md`，下称 PAPER）。契约草案（下称 CT，规划路径 `docs/d4/契约草案.md`）截至本版尚未建立；CT 建立并定稿前，本文 §7.3 是 `board.v2` 中统计字段的单一来源（LB §13.3 已按此引用），CT 建立时以本文 §7.3 为初稿迁入。并行文档与本文的不一致之处见 §13.2 同步修订清单 |
| 编号约定 | 需求 R-M32-xx；验收 AC-M32-xx；公式 F-xx；校验规则 V-x；数值确定性规则 ND-x；D3 兼容不变量 D3-INV-xx；蒙特卡洛实验 MC-x；风险 RK-M32-xx；待决 Q-M32-xx。v0.1 的编号在 v0.2 中保持不变，v0.2 新增项依次续编（需求自 R-M32-88、验收自 AC-M32-35、公式自 F-41、校验规则自 V-9、实验自 MC-11 起）。引用 AWR-06 的工作包、决策、验收、风险与待决事项时沿用其编号 WP-D4-xx、ADR-D4-xx、AC-D4-xx、RK-xx、Q-D4-xx |
| 引用约定 | 代码路径相对仓库根 `/data/projs/anet-drone`。前缀与 AWR-06 一致：`py:` = `python/awr/`；`sdk:` = `tools/sdk/drone4d/src/drone4d/`；`ct:` = `packages/contracts/`；`web:` = `apps/web/src/`。对现有代码的陈述均经只读核对（2026-10-18），以"文件 + 符号"为准，行号可能漂移 |
| 数值约定 | 标注"缺省""初值""目标值"的数值为设计默认值或待 pilot 校准的目标，不是对现有实现性能的断言 |
| 文献约定 | 本文使用的统计与数值方法（自助法、studentized 自助法、Holm 逐步校正、Fieller 区间、双单侧等价检验、保序回归、强连通分量算法、整数自由度 Student t 分布函数的有限级数、正态分位数的有理逼近）均为教科书方法，本文给出完整定义或算法规格，不引用未经调研报告核实的文献；AWR-06 中标注"待核实"的文献在本文保留该标注 |
| 写作规范 | 中文、企业级与工业级行文；技术名词保留英文；不使用 emoji 与状态字形；下一大版本统一称 D4 |

---

## 0. 摘要

1. **定位**。M32 是 D4 的评测与统计内核，位于 AWR-06 §3.2 的 L5 层，包路径 `py:evaluation/`。核心包与任务语义无关：输入是评测器（M31）产出的逐 Episode 分量、资源账本摘要（M28）与 Suite 清单的 `metrics`/`scoring`/`budget` 声明，输出是带置信区间、判定词、并列组、名次区间、资源–效用支配分层、Δ 族诊断与统计自述块（`stats`）的榜单内容，以及跨保真度审计报告与复核结论。M32 只做纯计算，不执行仿真、不执行干预、不调度评测、不保管密钥。D3 兼容实现 `py:evaluation/compat/` 含 D3 任务语义常量，随仓库存在但不进入 `drone4d` wheel（§8.1）。
2. **指标规格**。`MetricSpec` 以数据声明方向（`max`/`min`/`target`）、取值范围、单位、可空性、主指标与分层指标身份及聚合方式；本文在 AWR-06 §4.2 的字段之上增补 `target`（目标区间）、`level`（Episode 级或场景级）、`epsilon`（分指标实际意义阈值）、`scenario_sets`（场景集作用域）、`penalty`（无界不利端的失败代入值）、`companions`（同一指标的伴随统计量，如 IQM 与 10% 分位）、`precision` 与 `supports_subset`，并给出 12 条一致性校验规则 V-1 至 V-12；校验与构建以（Suite、场景集）视图为单位，使 S2 一个 Suite 内两个场景集各有一个主指标的声明合法。§6.2.3 给出 `suite.v1` 字段到本文数据结构的映射。
3. **层次统计与重采样**。估计量为场景等权均值（F-01 至 F-03）。估计目标按 Suite（可按场景集覆写）声明：`family` 只在场景层有放回重采样并以 bootstrap-t 构造区间；`fixed_set` 只在场景内重采样外生种子并以 percentile 构造区间；不做两层同时重采样。同一分析组内全部条目共用一组重采样下标；场景序号与场景数取自分析组声明的场景集全集，按 `scenario_id` 码点序排列，条目缺失场景只在复本分母中体现（F-07、F-08）。下标种子由公开的域分隔 SHA-256 派生且不含 `entry_id`；下标生成器以 SplitMix64 计数器加乘移位映射完全规定，使第三方可用任意语言独立复算。
4. **成对比较与 Holm**。方向事先规定的比较（条目对 `π_ind`、条目对参考行的支配检验）用单侧 p 值；方向由点估计决定的比较（候选条目之间的并列扫描与 F_dom）用双侧 p 值，避免"按数据选方向的单侧检验"使名义水平翻倍（本版评审外补充修订 S-1）。p 值取 `(b + 1)/(B + 1)` 型复本 p 值；检验族 F_rank、F_dom、F_domref（候选对参考行）、F_rob（X1）与 F_fid（跨保真度审计）分别施加 Holm 逐步校正。`n_boot = min(200,000, max(10,000, ⌈20·m_rank/α⌉))` 只由 F_rank 决定；3 项分层指标下 K ≥ 5 时 F_dom 的复本分辨率不足，改走解析路径：`family` 用 Student t（自由度 n − 1）参照，`fixed_set` 用分层自助方差的闭式解与正态参照（§6.3.1 对照表）。"显著且实际更优"要求 Holm 拒绝且点估计差 > ε（ε = 0.02），由此保证关系无环；"等价"为差值 90% 区间落入 `[−0.05, 0.05]`。
5. **并列组、名次区间与支配分层**。并列组按"与组首比较"扫描生成；名次区间由每个重采样复本内的竞争名次取 2.5%/97.5% 分位；支配关系带容差并要求至少一项在 F_dom 内 Holm 显著，先以迭代 Tarjan 算法合并强连通分量（标注"不确定组"），再在缩点图上逐层剥离；参考行的层级以独立检验族 F_domref 判定。本文指出 AWR-06 中"与组首比较"的检验集合依赖检验结果、与 Holm 需要预先确定的检验集合存在张力，给出可终止的确定性定点算法作为缺省实现，并保留"全部无序候选对"族作为候选方案，由蒙特卡洛实验 MC-2 的族错误率结果在 WP-D4-11 中裁决（Q-M32-01）。
6. **失败、无效与完整性**。策略原因失败与 `invalid_policy = zero` 下的无效运行，按方向把"结果类"指标置为不利端（不利端无界时取 Suite 声明的 `penalty` 或置空）、"消耗类"资源指标保留实际消耗；本文指出 AWR-06 §8.6"全部分量记 0"对越小越好的资源指标构成奖励，须修订（反馈第 2 条）。`invalid_policy = exclude` 带阈值 `invalid_threshold`：有效主评测 Episode 数严格少于 `(1 − θ)` 倍总数时条目 `status = invalid` 且不排名，否则只按有效 Episode 计算；`drone4d-event@1` 取 θ = 0.2，与 D3 行为一致。任一场景有效外生种子少于 `min_valid_seeds`（缺省 3，X1 为 2）的条目或扰动族记 `incomplete`。
7. **干预对照与 Δ 族**。`InterventionPlan` 把"场景集 × 外生种子 × 重复 × 干预"展开为 Episode 规格，保证共同随机数配对，并按 AWR-06 §8.5 的范围分两阶段执行。M32 给出 `Δ_ind`、`Δ_cut`、`Δ_perm`、`R_central`、闭合率 `G`、`η_comm`、X1 的 `S_rob`、`L_deg`、`p_det`/`T̃_detect`、`p_rec`/`T̃_recover` 的估计量、区间与 `n/a` 规则。干预执行的归属定为：M32 提供纯数据的 `CutSpec`/`PermSpec` 与纯函数的供体选择规则；M29 的投递包装层实现 `DeliveryInterceptor` 并执行阻断与置换；置换载荷由 M33 的 `MessageIndex` 从供体 Episode 的 trace 检索；M32 只校验执行记录与估计（§2.2，Q-M32-07，在 WP-D4-01 RFC 中确认）。
8. **资源账本核算**。`LedgerCheck` 以 M28 运行清单 `ledger_summary` 的 `det`/`team`/`measured` 结构为输入，重算比值与违规；预算维度区分 `enforce = refuse`（提交时拒绝，只核对使用量不超额度，不判违规）与 `enforce = violation`（事后记违规）。`ranked_valid` 与 `invalid_reasons` 只由 M28 写入，M32 只做重算比对与 `budget.policy` 到计分的映射；排名且非影子赛道的运行要求 cgroup 计量来源。
9. **跨保真度审计与复核**。`fidelity_audit` 计算 FC-4 至 FC-7，FC-4 明确采用 `fixed_set` 口径并报告等价可达率，FC-6 以独立审计族 F_fid 判定显著差值。`verify_board` 由 `rows.jsonl` 重算 `board.v2` 并以 JCS 规范字节逐字节比对，跨硬件等级亦要求逐字节一致（ND-9 的确定性数学函数消除了 libm 差异）；复跑判据 R-1、R-2b 改用计入原估计与复跑双方抽样误差的预测区间（F-39），并以 MC-12 评估诚实条目误判率与偏差检出率。
10. **D3 兼容委托**。`py:bench/eval/aggregate.py` 的公开符号与行为保持不变，实现委托给 `py:evaluation/compat/d3_board_v1.py`；后者是对 D3 算法的冻结移植（20 条不变量 D3-INV-01 至 D3-INV-20，含随机数消耗顺序、浮点归约顺序与调用方的输入过滤），以 MetricSpec 列表参数化而非常量；`awr.evaluation` 不 import `awr.bench`，版本常量由调用方传入。验收以 G0 黄金 `board.json` 快照与冻结原版的随机化对拍（≥ 1,000 组随机行集）证明逐字节一致（AC-D4-13）；对拍以冻结原版为准，不变量清单只作说明。
11. **验证**。蒙特卡洛验证方案 MC-1 至 MC-13 覆盖区间覆盖率、F_rank 与 F_dom（含解析路径）的 Holm 族错误率、功效与等价可达性、配对增益、支配区分度、Fieller 与比值区间、小样本设计与 FC-4 等价可达率、复跑判据误判率、pilot 上界覆盖率；核心网格 48 个条件、每条件 2,000 次重复，判据与 AC-D4-14 一致；算力估算标注为待实测。
12. **规模**。功能需求 93 条、非功能需求 10 条、验收 39 条、公式 45 条、对 AWR-06 的反馈 30 条、并行文档同步修订 32 项；P0 交付 WP-D4-09、11 与 12、13 中的 E 线评测部分，P1 交付 Δ_perm、X1 聚合、账本 cgroup 核算、跨保真度审计、复核与重放容差，P2 交付 B-LLM 重复协议、FC-8 与论文分析函数。

---

## 1. 背景与目标

### 1.1 约束本模块的事实

| # | 事实 | 对设计的含义 | 依据（只读核对） |
|---|---|---|---|
| F1 | D3 聚合的指标集合为常量 `METRICS = ("ec", "eu", "ao", "ce")`，重采样次数常量 `N_BOOT = 10_000` | D4 须把指标集合提升为 `MetricSpec` 数据；D3 路径以参数传入四项指标以保持行为 | `py:bench/eval/aggregate.py` `METRICS`、`N_BOOT` |
| F2 | 重采样随机数生成器为 `np.random.default_rng(int.from_bytes(sha256("‖".join(parts))[:8], "little"))`，`build_board` 传入 `parts = (entry_id, board_version, lane)`，不含 `track`；每个条目独立派生 | 条目间重采样下标独立，"成对差值区间"实为非配对（AWR-06 偏差 D-6）；D3 路径须原样保留，D4 路径改为共享下标 | `aggregate.py` `_rng`、`lane_metrics`、`build_board` |
| F3 | `_boot` 为两阶段分层自助法：事件按名称排序；每个事件先 `rng.integers(0, n_seeds, (n, n_seeds))` 抽种子，再 `rng.random((n, n_seeds, max_rep))` 乘以被抽中种子的重复数后取整抽重复；事件内对展开后的全部值取 `nanmean`，赛道级为有限事件值的均值 | 随机数消耗顺序与浮点归约顺序决定 D3 输出字节，属兼容不变量（§8） | `aggregate.py` `_boot`、`_nanmean` |
| F4 | 区间为 `np.percentile` 的 2.5%/97.5% 分位，结果 `round(·, 6)`；无值时区间写 `[0.0, 0.0]` | 同上 | `aggregate.py` `lane_metrics` |
| F5 | 并列只比较相邻条目：按 `(−value, entry_id)` 排序，相邻配对差区间含 0 则同组；名次为竞争名次（新组首条目取 `i + 1`） | 链式并列风险（n02 §3.3）；D4 改为"与组首比较" | `aggregate.py` `build_board` |
| F6 | 支配关系：四项点估计均非空且不低于对方，且至少一项配对差区间下界 > 0；逐层剥离；基线为参考行，其层级为"支配它的候选条目的最高层 + 1" | 假设全部指标越大越好且非空；D4 须方向化并加容差与强连通分量合并 | `aggregate.py` `_Pairs.dominates`、`build_board` |
| F7 | 无效运行：在排名运行（`ranked_row` 为真，开发榜除外）中，`ranked_valid = false` 的 Run 占比 > 20%（`len(valid) < 0.8·len(rs)`）的条目 `status = invalid`，指标只按有效 Run 计算，不参与名次与分层；占比不超过 20% 时同样只按有效 Run 计算 | 与设计文档"记 0、不剔除"不一致（偏差 D-2）；D3 路径保留，D4 以 `invalid_policy = exclude` 加阈值复现该行为，D4 缺省 `invalid_policy = zero` | `aggregate.py` `build_board` |
| F8 | 调用方：`py:bench/boardstore.py`（`publish` 调 `build_board` 与 `entry_details`，`compose_board` 调 `compose`）；`py:bench/subworker.py` `process_one`（调 `lane_metrics`，键为 `(f"{team}/{sid}", board_version, lane)`）；`py:bench/eval/__main__.py`（调 `build_board`）；`py:bench/ctl.py` `board compose`（经 `compose_board`） | D3 兼容委托须覆盖全部调用方与公开签名 | 各文件 import 语句 |
| F9 | `boardstore.publish` 以 `json.dumps(board, ensure_ascii=False, indent=1) + "\n"` 写 `board.json`；`append_rows` 以 `sort_keys=True` 逐行写 `rows.jsonl`；`publish` 与 `compose_board` 的输入先经 `_listed` 过滤（基线行加每个团队每个（分轨、赛道）最近完成且公开上榜的提交） | D3 字节一致的比较对象是该序列化结果；`board.v1` 影子重算须先施加同一过滤 | `py:bench/boardstore.py` `publish`、`append_rows`、`_listed` |
| F10 | D3 回归测试含逐字节断言与性能断言：`test_score_version_or_lane_change_needs_new_board_version` 比较 `board.json` 字节；`test_recompute_100_entries_4_lanes_within_60s` 要求 100 条目 × 4 赛道 × 10 种子 × 2 重复在 60 s 内重算 | 兼容委托不得退化性能；D4 路径的性能预算以同等规模为参照 | `tests/d3/board/test_score.py`、`test_rank.py`、`test_compose.py` |
| F11 | 仓库锁定 `numpy==2.5.3`（`requirements.lock`、`requirements-d3.lock`、根 `pyproject.toml`）；SDK `tools/sdk/drone4d/pyproject.toml` 的必选依赖为 `httpx>=0.27`、`msgpack>=1.0`，numpy 只出现在可选附加依赖 `images = ["numpy>=1.26", "pillow>=10"]` 中 | D3 路径的逐字节一致以锁定版本为前提；`awr.evaluation` 进入 wheel 时 numpy 须升为必选依赖或提供降级路径（R-M32-103、反馈第 23 条）；D4 路径须在 numpy 1.26 至锁定版本间输出一致 | 各锁文件；`tools/sdk/drone4d/pyproject.toml` |
| F12 | `py:sim/sensors/cbrng.py` 提供 SplitMix64 终混函数 `sm`（uint64 回绕、双射），只依赖 numpy，不属 `D3_PACKAGES` | 可作为重采样下标生成器的基础构件，跨 numpy 版本与语言可复算 | `cbrng.py` `sm`、`key`；AWR-06 B-10 |
| F13 | `py:sim/events/jcs.py` 实现 RFC 8785 JCS 规范化（数字按 ECMAScript 规则，禁止 NaN 与无穷） | `board.v2` 的规范字节与 `verify_board` 的比较基准 | `jcs.py` `jcs` |
| F14 | D3 协同效率 `Ĝ = clip((V(U) − max_u V({u})) / (1 − 1/|U|), 0, 1)` 比较全编队与最佳单机，子编队投影由 `_restrict` 实现 | 按 ADR-D4-22 归为"扩容收益"，不称协作增益 | `py:bench/eval/ce.py` `collaborative`、`_restrict` |
| F15 | `py:bench/eval/thresholds.py` 定义 `EVALUATOR_VERSION = "0.1.0"`、`SCORE_VERSION = "d3-score-0"`，`build_board` 以其计算 `platform_digest` 与 `evaluator_major` | D3 兼容实现若直接 import 将使 `awr.evaluation` 依赖 `awr.bench`；改为由调用方传入 | `thresholds.py`；`aggregate.py` `build_board` |
| F16 | `ct:bench/board.v1.schema.json` 为 `additionalProperties: false`，`tracks[].physics` 为 `const "realistic"`，`entries[].metrics` 固定四键 | `board.v1` 冻结只读，新榜使用 `board.v2`（ADR-D4-19） | 该 schema |
| F17 | `python/awr/evaluation/` 目录尚不存在 | 本模块为新建包，无存量兼容负担（D3 兼容入口除外） | 目录核对 |
| F18 | `py:bench/subworker.py` `process_one` 计算提交结果时，对该提交的全部行（不经 `ranked_row` 过滤、不剔除无效行）调用 `lane_metrics`，状态取 `"valid" if valid >= 0.8 * len(rows) else "invalid"`；平台原因异常重跑一次并计入 `runs.platform_rerun`，`rows.jsonl` 只追加成功的一次 | 阈值与 `build_board` 互为补集、边界相同，但分母与指标所用行集不同；属 D3 兼容不变量（D3-INV-19）；D4 的行完整性规则须处理重跑（R-M32-92） | `subworker.py` `process_one` |
| F19 | CPython 3.12 `statistics.NormalDist.inv_cdf` 为 Wichura AS241 的纯 Python 实现，`|p − 0.5| > 0.425`（如 p = 0.975）时走调用 `math.log` 的分支；`math.log`、`math.exp`、`math.erf` 由平台 libm 提供，不保证正确舍入 | 判定路径不得直接使用 libm 超越函数；本文以只用 IEEE 基本运算的确定性实现替代（ND-9） | `statistics._normal_dist_inv_cdf` 源码核对（CPython 3.12.3） |

### 1.2 目标

| 编号 | 目标 | 度量 |
|---|---|---|
| G-1 | 提供与任务语义无关的统计内核，任何 Suite 只需声明 `MetricSpec` 与 `scoring` 即可获得区间、判定、名次区间与支配分层 | 3 个 P0 Suite 与 `drone4d-event@1` 不写任何统计代码即可成榜 |
| G-2 | 统计结论可由第三方仅凭 `rows.jsonl`、Suite 清单与 `stats` 自述块独立复算 | AC-M32-19、AC-M32-03、AC-M32-37 |
| G-3 | 统计协议在声明的样本设计下有经过验证的覆盖率与族错误率 | AC-M32-27（AC-D4-14） |
| G-4 | Δ 族在共同随机数与资源对齐下可估计，且每个量的因果解释范围与 `n/a` 规则可执行 | AC-M32-12 至 AC-M32-16 |
| G-5 | D3 输出逐字节不变 | AC-M32-22（AC-D4-13） |
| G-6 | 进入 `drone4d` wheel，第三方依赖只有 numpy，且输出与 numpy 版本（1.26 至锁定版本）无关 | AC-M32-24、AC-M32-25（AC-D4-43） |

### 1.3 非目标

- 不实现任务评测器与指标计算（M31 `Evaluator` 负责，M32 只消费 `components`）。
- 不执行干预：投递阻断与载荷置换由 M29 投递包装层实现的 `DeliveryInterceptor` 执行，置换载荷由 M33 `MessageIndex` 检索；M32 只定义纯数据的 `CutSpec`/`PermSpec` 与供体选择规则，并校验执行记录（§2.2）。
- 不写入 `ranked_valid` 与 `invalid_reasons`（M28 写入），只重算比对。
- 不调度评测、不维护提交状态机与榜单存储（L6 `py:bench/` 的 `subworker`、`submissions`、`boardstore` 负责）。
- 不生成与保管秘密种子与盐（平台密钥存储负责）；M32 只实现承诺的验证算法。
- 不实现 Ladder 式反馈、配额与赛季治理（LB 负责）。
- 不定义跨 Suite 合成排序（ADR-D4-21）；`compose` 只作为显式声明权重时的计算接口。
- 不定义参考策略缓存键（M31 R-M31-45 定义，WP-D4-13 冻结），只引用并校验对齐。
- 不实现前端展示（WP-D4-23）。

### 1.4 术语（本文新增，其余见 AWR-06 术语表）

| 术语 | 定义 |
|---|---|
| 分析组 | `(board_version, track, scenario_set)`。同一分析组内的全部条目共享重采样下标、检验族与排名；不同分析组互不混排 |
| 分析组视图 | Suite 清单的 `metrics[]` 与 `scoring` 在某一场景集上的有效子集与覆写结果（R-M32-88），校验与榜单构建均以视图为单位 |
| 候选条目 | 分析组内 `reference_kind = null` 且 `status = valid` 的条目，参与名次、并列与支配 |
| 参考行 | `reference_kind ∈ {ind, central, random, single, published}` 的条目，不占名次、不影响候选条目的分层，只被标注 |
| 单元格 | 场景 s 与外生种子序号 i 的组合 `(s, i)`；同一单元格对全部条目、全部干预与全部重复使用同一 `run_seed` |
| 行键 | `(entry_id, scenario_set, scenario_id, seed_index, repeat, intervention, perturbation)`；同一行键在 `rows.jsonl` 中可有多次 `attempt`，按 R-M32-92 取一行 |
| 场景值 | 条目在场景 s 上对指标 k 的值 `ȳ_{e,k}(s)`（F-02），是 `family` 估计目标下的统计单位 |
| 复本 | 一次重采样得到的统计量取值，下标 j = 0..B−1 |
| 方向化差值 | `σ_k · (μ_A − μ_B)`，σ_k 对 `max` 取 +1、对 `min` 取 −1；正值恒表示 A 更优 |
| 结果类指标 / 消耗类指标 | 前者为 `kind ∈ {task, coop, robustness, calibration}`，后者为 `kind = resource`；`kind = safety` 单列处理（§4.7） |
| 定向比较 / 选向比较 | 前者的检验方向在看到数据前确定（如"条目优于 `π_ind`"），用单侧 p 值；后者的方向由点估计决定（如并列扫描中"组首优于当前条目"），用双侧 p 值（F-41） |
| 解析路径 | 复本 p 值分辨率不足时以闭式标准误与参照分布计算 p 值的路径（F-14c、F-42），在 `stats.analytic_families` 登记 |
| 判定词 | "显著且实际更优""等价""未判定"三者之一，只在检验族内的比较上给出 |
| 不确定组 | 支配图中规模大于 1 的强连通分量 |
| 对照 Episode | 干预 X 下运行的 Episode，与主评测 Episode 按单元格与重复配对 |
| 供体 Episode | `Δ_perm` 中提供替换载荷的 Episode：同一条目、同一场景、同一重复、外生种子 `σ(i)` 的主评测 Episode |
| 可用对比例 | 两侧 Episode 均存在的 Episode 对中，对照 Episode 无资源违规者的占比（AWR-06 §8.5 第 3 条，F-28） |
| 回退率 `r_fb` | `Δ_perm` 中未能取得供体载荷而以陈旧消息替代的投递占全部置换投递的比例 |

---

## 2. 范围

### 2.1 分阶段范围

| 阶段 | 范围 | 工作包（E 线为主） |
|---|---|---|
| P0（第 1–11 周） | `MetricSpec` 与校验（含场景集视图、`penalty`、`companions`）；行完整性；数据立方体与估计目标；共享下标重采样、bootstrap-t、percentile 与解析路径；确定性数学函数库；成对比较、Holm、判定词；并列组、名次区间；容差支配、F_domref 与强连通分量合并；失败、无效（含 `exclude` 阈值）与完整性规则；`Δ_ind`、`Δ_cut`、`R_central`、`G`、`η_comm`（依赖度口径）；`InterventionPlan` 的主评测、冒烟与 `cut` 展开及 `CutSpec`；`LedgerCheck` 的字节与 CPU（进程内计量标注）核算及 `enforce` 规则；`board.v2` 内容构建与 `verify_board`；D3 兼容委托与 `board.v1` 影子重算；蒙特卡洛验证；本地实验报告与 pilot 功效函数 | WP-D4-09（第 1–3 周）、WP-D4-11（第 3–6 周）、WP-D4-12 的 `board.v2` 内容部分（第 6–9 周）、WP-D4-13 的估计部分（第 8–10 周）、WP-D4-17 的统计部分（第 9–10 周） |
| P1（第 12–21 周） | `PermSpec` 与供体规则、`Δ_perm` 估计、有效性规则与过敏反应审计；X1 的 `S_rob`、`L_deg`、`p_det`/`T̃_detect`、`p_rec`/`T̃_recover` 聚合与 F_rob；`LedgerCheck` 的 cgroup 来源规则与 GPU、token 维度；`verify_commitments`；`fidelity_audit`（FC-3 至 FC-7、F_fid）；R-1、R-2b 判据与 `audit_baselines`；等级 2 重放比对容差；场景级指标（如 S3 ANEES 区间内比例）的复本重算；训练产物交叉自助 | WP-D4-19（聚合部分）、WP-D4-20、WP-D4-21（验证算法）、WP-D4-22（核算部分）、WP-D4-27（统计部分）、WP-D4-30（比对部分） |
| P2（第 22–31 周） | B-LLM 重复协议与等级 3 分布一致性检验；FC-8 度量；论文分析函数（H3 的 Kendall τ 区间、方向 A 的饱和规模与负协作区域、方向 B 的 `B*_θ` 与 `Δ_perm^(c)`）；公开快照复算脚本 | WP-D4-38、WP-D4-33（度量部分）、WP-D4-41、WP-D4-40 |

### 2.2 与其他模块的边界

| 模块 | 向 M32 提供 | 从 M32 获取 | 边界规则 |
|---|---|---|---|
| M28 内核 | 运行清单（`determinism_level`、`seed_commit`、`ranked`、`ranked_valid`、`invalid_reasons`、`measured_verdict`、`ledger_summary`）、`ledger.v1`；`limit_expr` 求值器与求值结果 | `LedgerCheck` 的核算与一致性结论 | `ranked_valid` 与 `invalid_reasons` 只由 M28 按 `budget.policy` 写入（R-M28-69、R-M28-71）；M32 事后重算违规并比对，不改写这两个字段，只把 `budget.policy` 映射为计分（R-M32-57）；`limit_expr` 的语法与求值器只在 M28 实现（`eval_limit_expr`），M32 读取清单中已求值的额度（待 M28 增补，§13.2 第 1 项） |
| M29 交互网络 | `DeliveryInterceptor` 的实现与执行（阻断、置换）、执行记录（阻断与置换的投递计数、回退计数、豁免的控制消息计数） | 纯数据的 `CutSpec`、`PermSpec`（随 Episode 规格下发，M29 不 import `awr.evaluation`） | 干预只作用于接收侧，发送计费与网络内部状态与主评测相同（R-M29-24）；`_ix` 交互控制消息受 `cut` 阻断、不受 `perm` 替换（R-M29-77），由 `PermSpec.exempt_channels` 登记。M29 现行文本（UC-08、R-M29-24、§6 `DeliveryInterceptor` 注释"由 M32 实现"）须按本行修订（§13.2 第 4 项） |
| M30 协同接口与基线 | 参考策略实现（`π_ind`、`π_central`、`π_random`）、`x-awr-role` 字段角色标注（`perm_struct` 的置换字段）、参考策略的 `impl_sha256` 与 `attrs_digest` | 无 | 参考策略版本与实现摘要进入参考缓存键（R-M32-43）；M30 §2.2 中"M32 负责 `Δ_perm`/`perm_struct` 的执行"须改为"M29 执行、M32 定义规格"（§13.2 第 7 项） |
| M31 基准套件 | Suite 清单（`metrics[]`、`scenario_sets[]`、`scoring`、`budget`、`reference_policies`）、`Evaluator` 输出的 `evaluation.v1`、X1 扰动参数、场景级指标重算回调、参考缓存键定义（R-M31-45） | 分析组视图与 `MetricSpec` 校验结果；Δ 族与 X1 的估计 | 评测器只产出分量，不做跨 Episode 统计；场景级指标除外（R-M32-10）；`T_detect`、`T_recover` 的逐对象或逐 Episode 值由评测器按 X1 §8.2–§8.3 给出，M32 只做聚合（R-M32-99） |
| M33 Trace 与复现 | `truth_digest`、离线重算的分量；`Δ_perm` 供体消息索引 `MessageIndex.build/nearest`（R-M33-76） | `verify_board`、`rerun_check` 的判据；等级 2 重放比对的分量容差（R-M32-101，M33 R-M33-65 为需求方） | 由 trace 离线重算分量与供体载荷检索是 M33 的职责；M32 只比较分量与榜单，并规定供体选择规则与匹配键 |
| M34 SDK 与命令行 | `drone4d bench report` 的实验目录 | `analyze_experiment` 的报告数据 | 本地报告不进入榜单，标注"本地分析，非正式成绩"；`drone4d` wheel 的 numpy 依赖按 R-M32-103 处理 |
| L6 `py:bench/`（`subworker`、`boardstore`） | `rows.jsonl`、榜单配置、参考缓存 | `build_board_v2` 产物、`InterventionPlan` 展开结果 | 存储、状态机、发布冻结与 `current` 指针由 L6 负责；M32 函数不做文件写入 |
| LB 排行榜规格 | 赛道集合（含影子赛道标记）、条目状态（`provisional`/`verified`）、赛季规则、Ladder 反馈 | `stats` 自述块、判定词与复核判据 | LB 定义"何时用"，M32 定义"如何算" |
| CT 契约草案（待建） | `board.v2`、`evaluation.v1`、`ledger.v1`、`fidelity_audit.v1`、`suite.v1` 的 schema | 本文 §7.3 列出的字段需求 | CT 建立前本文 §7.3 为统计字段的单一来源；CT 建立后字段名以 CT 定稿为准，本文同步修订 |
| PAPER | H1–H6 判据与实验网格 | §4.8、§6.3 的分析函数 | 论文分析只使用本文定义的估计量与判定词 |

### 2.3 路径所有权（M32）

| 路径 | 所有权 | 说明 |
|---|---|---|
| `python/awr/evaluation/**`（`compat/` 除外） | M32 | 新建；D4 核心包，numpy-only，进入 wheel |
| `python/awr/evaluation/compat/**` | M32 | D3 兼容实现；随仓库与平台镜像存在，wheel 构建时排除（§8.1） |
| `python/awr/bench/eval/aggregate.py` | M32（委托外壳）与 D3 冻结规则共管 | 只允许改为委托外壳，公开签名与输出不变；受 B-07 约束。B-08 的冻结对象为 `py:bench/eval/` 的 EC/EU/AO/CE 评测器，不含 `aggregate.py`（见反馈第 15 条） |
| `tests/d4/evaluation/**` | M32 | 单元、性质、对拍与性能测试 |
| `tests/d4/evaluation/oracle/aggregate_d3_frozen.py` | M32 | D3 `aggregate.py` 在 G0 时刻的只读副本，仅作对拍基准，不被生产代码 import |
| `tools/evaluation/mc/**` | M32 | 蒙特卡洛验证脚本与网格配置；平台侧工具，不进入 wheel |

---

## 3. 用户与用例

| 编号 | 用户 | 用例 | 涉及能力 |
|---|---|---|---|
| UC-1 | 研究者 | 本地运行 `drone4d bench run` 后执行 `drone4d bench report`，得到各算法 `U` 的 95% 区间、与参考策略的配对差及判定词 | `analyze_experiment`、F-01 至 F-17 |
| UC-2 | 平台评测流水线 | SCORING 阶段追加 `rows.jsonl` 后重算分析组，生成 `board.v2` 内容 | `build_board_v2` |
| UC-3 | 外部复核者 | 下载 `rows.jsonl` 与 `board.json`，复算全部区间、名次区间与判定词；赛季末验证种子承诺 | `verify_board`、`verify_commitments`、§7.3 `stats` |
| UC-4 | Suite 作者 | pilot 中估计配对差的 ICC 与 `σ_D` 及其置信上界，按规则确定场景数，检查 PA-2 至 PA-4 | `pilot_power`、`required_scenarios`、`pa_checks` |
| UC-5 | 平台运维 | 每日参考策略抽样复跑比对；`verified` 申请条目的 R-1、R-2b 判据 | `audit_baselines`、`rerun_check` |
| UC-6 | 论文分析者 | 计算 H2–H6 判据、协作增益曲面、最小充分预算与跨保真度一致性 | §4.8、§4.10 的分析函数 |
| UC-7 | D3 运维 | D3 榜单发布、合成与提交结果计算行为与字节不变 | D3 兼容委托 |
| UC-8 | M33 重放工具 | 等级 2 动作重放后按分量容差判定"重放一致" | `replay_tolerance`（R-M32-101） |

---

## 4. 功能需求

"阶段"列为首次交付阶段（P0/P1/P2）；"级别"列取"阻塞"（未满足则所属阶段门不通过）、"应交付"、"可延期"。"依据"列中不带文档名的 §x 指 AWR-06，指本文时写"本文 §x"。v0.2 修订的需求在"依据"列注明"v0.2 修订"，新增需求注明"v0.2 新增"，编号与 §14 评审处理记录对应。

### 4.1 指标规格

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-01 | `MetricSpec` 字段：`id`、`kind`、`direction ∈ {max, min, target}`、`range`（上下界可为 null 表示无界）、`unit`、`nullable`、`primary`、`tier`、`aggregation ∈ {mean, iqm, q10, fail_rate}`；本文增补 `target`（`direction = target` 时必填的目标区间 `[lo, hi]`）、`level ∈ {episode, scenario}`（缺省 `episode`）、`epsilon`（分指标实际意义阈值，缺省见 R-M32-24）、`scenario_sets`（R-M32-88）、`penalty`（R-M32-89）、`companions`（R-M32-90）、`precision`（输出与评测器舍入的小数位数，缺省 6，M31 R-M31-40）、`supports_subset`（评测器是否支持子编队投影，缺省 false，M31 R-M31-42）。增补字段在 CT 建立前以本文为准 | P0 | 阻塞 | AC-M32-01、AC-M32-35 | §4.2；本文 §13 第 5、25 条；v0.2 修订 |
| R-M32-02 | 指标集校验规则 V-1 至 V-12（§6.2.2）在 Suite 登记与榜单构建时按分析组视图执行，违反任一条即拒绝并返回 `why = metric_spec_invalid`、规则号与场景集 | P0 | 阻塞 | AC-M32-01、AC-M32-35 | §4.2、§8.4；v0.2 修订 |
| R-M32-03 | 方向化：`max` 取 σ = +1，`min` 取 σ = −1；`target` 先按 F-04 在 Episode 层变换为"越大越好"的偏离度再取 σ = +1。全部比较、排名与支配只使用方向化差值 | P0 | 阻塞 | AC-M32-01 | §8.4；u-bench §1；v0.2 修订 |
| R-M32-04 | 入库校验（分量层）：分量为 NaN 或无穷、超出 `range`、对不可空指标为 null 时，该行被拒绝并返回 `why ∈ {metric_non_finite, metric_out_of_range, metric_null_not_allowed}`；拒绝视为评测器缺陷，不按策略失败计分。行层与行集层的校验见 R-M32-92 | P0 | 阻塞 | AC-M32-02 | 本文设定 |
| R-M32-05 | 排名模式 `scoring.rank_mode ∈ {primary, per_metric}`（字段名与 M31 R-M31-04 一致）：`primary` 为 D4 缺省，只以主指标排名；`per_metric` 只允许 Suite `drone4d-event@1` 使用（逐指标排名、无主指标），以承接 D3 ADR-191 的四指标并列展示，规则见 R-M32-100 | P0 | 阻塞 | AC-M32-01 | ADR-D4-19、ADR-D4-21；本文 §13 第 6 条；v0.2 修订 |
| R-M32-88 | 场景集作用域：`MetricSpec.scenario_sets` 为该指标适用的场景集 id 列表，null 表示全部场景集；`MetricSet.view(scenario_set)` 返回适用指标的子集；`Scoring.view(scenario_set)` 应用 `suite.v1` `scenario_sets[].estimand` 的覆写（M31 §4.3），v0.2 只允许覆写 `estimand`，其他 `scoring` 字段不得按场景集覆写；V-1 至 V-12 对每个分析组视图分别执行 | P0 | 阻塞 | AC-M32-35 | S2 R-S2-02、§7.1；M31 §4.3；v0.2 新增 |
| R-M32-89 | 失败代入值：`level = episode` 的结果类指标若方向化不利端无界（F-23），须声明 `penalty`（落在 `range` 内的有限值，如 `horizon_s` 上限）或 `nullable = true`（失败 Episode 置空）；二者皆无时违反 V-9。`level = scenario` 的指标由评测器在场景内处理失败 Episode，须在评测器卡中声明处理规则 | P0 | 阻塞 | AC-M32-10、AC-M32-35 | 本文 F-23；v0.2 新增 |
| R-M32-91 | `suite.v1` 映射：Suite 清单的 `metrics[]`、`scoring`、`budget` 与 `evaluation.ci` 字段按 §6.2.3 映射到 `MetricSpec`、`Scoring` 与 `Stats`；映射表是 M31 清单解析器与本文的唯一接口约定；清单中出现映射表以外的 `scoring` 字段时拒绝并返回 `why = scoring_field_unknown` | P0 | 阻塞 | AC-M32-35 | M31 §8.1；S3 §11；v0.2 新增 |

### 4.2 层次与估计目标

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-06 | 数据立方体：按（条目、场景、外生种子序号、重复、干预、扰动）组织逐 Episode 分量；重复在种子内取均值（F-01），种子在场景内取均值（F-02），场景等权得点估计（F-03） | P0 | 阻塞 | AC-M32-02 | §8.5 记号、§8.6 估计量 |
| R-M32-07 | 估计目标由分析组视图的 `estimand` 声明：`family` 只在场景层重采样；`fixed_set` 只在场景内重采样外生种子；不提供两层同时重采样的选项 | P0 | 阻塞 | AC-M32-04 | §8.6、ADR-D4-23 |
| R-M32-08 | 可空指标：场景值只由非空种子值计算；全部为空的场景在该指标上缺失；点估计按非空场景等权；条目卡写出非空场景数 `n`；配对差只在双方均非空的场景上计算 | P0 | 阻塞 | AC-M32-02 | 本文设定 |
| R-M32-09 | 非均值聚合：`iqm`、`q10` 以场景等权的 Episode 加权经验分布计算（每个 Episode 权重 `1/(S·m_s·R)`），`fail_rate` 为加权的 `failure.policy` 比例（R-M32-90）；三者只给 percentile 区间，不进入检验族，不作分层或主指标；IQM 与 q10 对同一指标的输出机制见 R-M32-90 | P0 | 应交付 | AC-M32-02 | §8.4 稳健与尾部；v0.2 修订 |
| R-M32-90 | 伴随统计量：`MetricSpec.companions ⊂ {iqm, q10}` 声明在同一场景值与 Episode 值上派生的伴随统计量，与该指标的均值同时输出于 `entries[].metrics.<id>.companions`（§7.3），各带 percentile 区间；主指标缺省 `companions = [iqm, q10]`，满足 AWR-06 §8.4"IQM 与均值、失败率并报"与 LB §13.3 的主指标要求（LB 字段名 `p10` 统一为 `q10`）。`aggregation = fail_rate` 的指标读取 `Row.failure_policy` 标志，不读取任何 `components` 键；其值不受 `invalid_policy` 影响 | P0 | 应交付 | AC-M32-02 | §8.4；LB §13.3；S1 §7.7；v0.2 新增 |
| R-M32-92 | 行完整性与幂等：(a) 行层校验：缺少统计所需字段（§7.2）返回 `missing_field`；`row_schema` 与分析组声明的版本不符返回 `schema_mismatch`；`components` 含视图外的指标 id 返回 `unknown_metric`；(b) 行集层：同一行键（§1.4）可有多次 `attempt`（自 0 起），取 `failure.infra = false` 的最大 `attempt` 行为该行键的有效行，全部 `attempt` 均为基础设施故障时该行键记缺失并计入 `infra_missing`；同一行键同一 `attempt` 出现两行时，二者 JCS 字节相同则去重，否则拒绝构建并返回 `duplicate_cell`；(c) `build_board_v2` 的输出对行序、行的分块方式与重复的相同行不变 | P0 | 阻塞 | AC-M32-36 | 本文设定；`subworker.py` 平台原因重跑；LB E-11；v0.2 新增 |
| R-M32-10 | 场景级指标（`level = scenario`，如 S3 的跨运行 ANEES 区间内比例）由评测器按场景内全部种子计算；`family` 下直接作为场景值重采样；`fixed_set` 下每个复本按被抽中种子的权重调用评测器回调 `recompute(seed_weights)` 重算；回调不可用时该指标在 `fixed_set` 下不给区间 | P1 | 应交付 | AC-M32-02 | §7.3；本文 §13 第 5 条 |
| R-M32-11 | 训练产物：学习型条目提交 T 个独立训练产物时，场景与训练产物为交叉因子，两因子分别有放回重采样（F-38）；T < 3 时不报告训练稳健性列 | P1 | 应交付 | AC-M32-31 | §8.6 学习型方法 |
| R-M32-12 | B-LLM 重复：每个单元格至少 3 次重复，重复在种子内取均值；`repeat_delta` 为全部单元格上重复极差的最大值；重复不足 3 次的单元格计为缺失 | P2 | 可延期 | AC-M32-32 | §8.3 B-LLM 统计协议 |

### 4.3 重采样与区间

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-13 | 共享下标：同一分析组内全部条目（含参考行与对照 Episode）使用同一组重采样下标；`family` 为场景下标，`fixed_set` 为场景内种子下标；下标种子按 F-07 由公开字符串派生，不含 `entry_id`，不使用服务端密钥 | P0 | 阻塞 | AC-M32-03 | §8.6 配对、ADR-D4-25、偏差 D-6 |
| R-M32-14 | 下标生成器按 F-08 完全规定（SplitMix64 计数器与 32 位乘移位映射），不使用 numpy `Generator`；发布不少于 4 组黄金向量（种子、S 或 m_s、前 32 个下标，其中至少 1 组含条目缺失场景与缺失种子的完整复本均值算例）供第三方实现对拍；本地实验报告使用独立域 `drone4d.experiment.resample.v1` | P0 | 阻塞 | AC-M32-03 | 本文 §13 第 4 条；v0.2 修订 |
| R-M32-93 | 场景与种子索引：`S` 为分析组视图声明的场景集全集的场景数（取自 Suite 清单 `scenario_sets[]`，随 Board Version 冻结），场景序号 s = 0..S−1 按 `scenario_id` 的 Unicode 码点序升序分配；`fixed_set` 下 `m_s` 为承诺种子集中场景 s 的种子数，种子序号 t 按 `seed_index` 升序对应；条目缺失的场景或种子不改变 S、m_s 与下标，只在 F-10、F-11 的分母中体现；X1 场景子集与跨赛道分析的种子派生按 F-07 的专用域（R-M32-52、F-32） | P0 | 阻塞 | AC-M32-03 | 本文 F-07、F-08；X1 §8.5；v0.2 新增 |
| R-M32-15 | 重采样次数：`n_boot = min(200,000, max(10,000, ⌈20 · m_rank / α⌉))`（F-15），α = 0.05，`m_rank` 为分析组内各 F_rank 族检验数的最大值；`n_boot` 是分析组常量（共享下标的前提）。F_dom、F_domref 若 `⌈20 · m / α⌉ ≤ n_boot` 则用复本 p 值，否则走解析路径（R-M32-94）；3 项分层指标下 K ≥ 5 时 F_dom 即走解析路径（§6.3.1 对照表） | P0 | 阻塞 | AC-M32-11 | §8.6 重采样次数；本文 §13 第 8 条；v0.2 修订 |
| R-M32-16 | 区间方法：`family` 下线性统计量（均值、配对差、Δ 族中的差值型量、`S_rob`、`L_deg`）用 bootstrap-t（F-12）；`fixed_set` 下一律 percentile（F-13）；非线性统计量（`G`、`B*_θ`、名次、IQM、q10、相关系数、中位数）一律 percentile 或专用方法（`family` 下 `G` 用 Fieller，`fixed_set` 下 `G` 用复本比值 percentile，R-M32-45）；每个量的方法写入 `stats.ci_methods` | P0 | 阻塞 | AC-M32-04 | §8.6 区间；本文 §13 第 3 条；v0.2 修订 |
| R-M32-17 | 单条目区间按 `MetricSpec.range` 截断显示，并置 `ci_clipped = true`；差值区间不截断 | P0 | 应交付 | AC-M32-04 | 本文设定 |
| R-M32-18 | 退化情形（§6.4.4）：观测标准误为 0、复本标准误为 0、复本内有效场景为 0 等情形按确定规则处理，不得抛出未处理异常或产生 NaN 进入输出 | P0 | 阻塞 | AC-M32-05 | 本文设定 |

### 4.4 成对比较与多重比较

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-19 | 配对差：条目 A、B 在指标 k 上的场景差 `D(s) = σ_k(ȳ_A(s) − ȳ_B(s))` 在共同非空场景上计算，逐复本以同一下标计算 `d*_j`，得到真正的配对差分布 | P0 | 阻塞 | AC-M32-04 | §8.6 配对 |
| R-M32-20 | p 值：定向比较取单侧 p 值；选向比较取双侧 p 值 `p₂ = min(1, 2 p₁)`，p₁ 为点估计较优方向的单侧 p 值（F-41）。单侧 p 值：bootstrap-t 为 `(1 + #{t*_j ≥ t}) / (B + 1)`；percentile 为 `(1 + #{d*_j ≤ 0}) / (B + 1)`；解析路径见 F-14(c) 与 F-42 | P0 | 阻塞 | AC-M32-05 | §8.6 优于检验；本文 §13 第 21 条；v0.2 修订 |
| R-M32-95 | 比较的方向属性：F_rank 中与 `π_ind` 的比较 Rf 为定向比较（检验"条目优于 `π_ind`"，不论点估计方向）；F_rank 中候选条目之间的比较、F_dom、F_fid 与 X1 F_rob 为选向比较；F_domref 为定向比较（检验"候选条目优于参考行"）。方向属性写入 `stats.families.<f>.sidedness` | P0 | 阻塞 | AC-M32-06、AC-M32-27 | 本文 §6.3.2；本文 §13 第 21 条；v0.2 新增（评审外补充 S-1） |
| R-M32-21 | 检验族：F_rank = 每个候选条目与其组首的比较（至多 K − 1 个，选向）加每个候选条目与 `π_ind` 参考行的比较（K 个，定向）；F_dom = 每个分层指标上全部无序候选条目对（选向）；F_domref 见 R-M32-96。分析组无 `π_ind` 参考行时 Rf 为空。族的定义、规模、方向属性与成员摘要写入 `stats.families` | P0 | 阻塞 | AC-M32-06 | §8.6 检验族；v0.2 修订 |
| R-M32-94 | 解析路径：F_dom、F_domref（及超出复本分辨率的其他族）走解析路径时，`family` 下以观测标准误 ŝ 与 Student t（自由度 n − 1）参照计算 p 值；`fixed_set` 下以分层自助方差的闭式解 ŝ_N（F-42）与标准正态参照计算 p 值，不生成逐对复本差值；参照分布函数由确定性数学函数库计算（ND-9）；走解析路径的族与所用参照分布登记于 `stats.analytic_families` | P0 | 阻塞 | AC-M32-11、AC-M32-27 | 本文 §13 第 8 条；v0.2 新增 |
| R-M32-22 | Holm 逐步校正（F-16）分族执行；p 值相同时按检验标识（`family/metric/a/b` 的码点序）稳定排序；输出原始 p 与调整 p | P0 | 阻塞 | AC-M32-06 | §8.6 |
| R-M32-23 | F_rank 的成员依赖并列扫描结果。缺省 `rank_family = head` 按 §6.4.2 的确定性定点算法求解（至多 K 轮，未收敛时退回 Bonferroni 阈值 `α/(2K − 1)` 并登记）；备选 `rank_family = all_pairs` 以全部无序候选对加 Rf 为族，与扫描顺序无关。两者均实现，缺省值在 WP-D4-11 依 MC-2 结果裁决（Q-M32-01） | P0 | 阻塞 | AC-M32-07、AC-M32-27 | §8.6 并列组；本文 §13 第 1 条 |
| R-M32-24 | 实际意义阈值：主指标 ε = 0.02；资源比指标 `ε_r = 0.05`；其他分层指标取 `MetricSpec.epsilon`，缺省 0.02（[0, 1] 尺度）。阈值只作用于点估计，不进入零假设 | P0 | 阻塞 | AC-M32-06 | §8.6 实际意义阈值 |
| R-M32-25 | 判定词：A "显著且实际更优"于 B 当且仅当在所属族内 Holm 拒绝且 `d̂ > ε_k`；"等价"当且仅当差值 90% 区间落入 `[−ε_eq, ε_eq]`（`ε_eq = 0.05`，双单侧检验，不做多重校正并在条目卡注明）；其余为"未判定"。等价判定与显著判定同时成立时以"显著且实际更优"为准（§6.4.5） | P0 | 阻塞 | AC-M32-06 | §8.6 判定词 |
| R-M32-26 | 输出 `comparisons[]`：每个族内比较的 `{family, metric, a, b, d, ci90, ci95, p_raw, p_holm, sidedness, verdict, equivalent, method}`，使条目卡与外部复核可直接读取判定依据 | P0 | 阻塞 | AC-M32-19 | 本文 §13 第 14 条；v0.2 修订 |

### 4.5 并列组与名次

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-27 | 并列组：候选条目按主指标方向化点估计降序、`entry_id` 升序排列；组首为首个条目；逐个扫描，若组首对当前条目"显著且实际更优"则当前条目开新组并成为新组首，否则并入当前组 | P0 | 阻塞 | AC-M32-07 | §8.6 并列组 |
| R-M32-28 | 名次为竞争名次：组内全部条目取该组首条目的位置序号；`tie_group` 自 0 起按组顺序编号；与 `board.v1` 的语义一致 | P0 | 阻塞 | AC-M32-07 | F5 |
| R-M32-29 | 名次区间：每个复本内计算候选条目的竞争名次（F-19），取 2.5% 分位（向下取样本值）与 97.5% 分位（向上取样本值），输出整数区间 `rank_ci95`；条目卡注明"名次未经选择校正" | P0 | 阻塞 | AC-M32-08 | §8.6 名次、§8.7 |
| R-M32-30 | 参考行不占名次、不进入 F_dom、不改变候选条目的并列组；只进入 F_rank 中与 `π_ind` 的比较（作为被比较方）与 F_domref（R-M32-96），并在条目卡展示与各参考行的配对差 | P0 | 阻塞 | AC-M32-07 | §8.11 `reference_kind`；v0.2 修订 |
| R-M32-100 | `per_metric` 模式（仅 `drone4d-event@1`）：(a) 每个指标 k 单独成一个 F_rank 族，候选集合 C_k 为该指标点估计非空的候选条目（D3-INV-13）；(b) `n_boot` 取各族 `m_rank` 的最大值代入 F-15，全组共用；(c) Rf 为空（该 Suite 无 `π_ind`）；D3 `baseline = true` 的行映射为 `reference_kind = published` 并置 `legacy_baseline = true`，只参与 F_domref；(d) 名次、并列组与名次区间（F-19）在 C_k 内计算，条目在某指标上不在 C_k 中时该指标的 `rank`、`tie_group`、`rank_ci95` 为 null；(e) 支配沿用 D3"任一分层指标为空则不支配也不被支配"的语义（V-5 例外） | P0 | 应交付 | AC-M32-39 | ADR-D4-19；D3 ADR-191；v0.2 新增 |

### 4.6 支配分层

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-31 | 支配关系（F-20）：A 支配 B 当且仅当在全部分层指标上 `d̂_{AB,k} ≥ −ε_k`，且至少一项在 F_dom 内 Holm 显著且 `d̂_{AB,k} > ε_k`；缺省 `dominance.tolerance = epsilon` | P0 | 阻塞 | AC-M32-09 | §8.6 支配分层 |
| R-M32-32 | 强连通分量：以迭代 Tarjan 算法（无递归，节点按 `entry_id` 排序）求支配图的强连通分量；规模大于 1 的分量内条目置相同 `uncertain_group` 标识 | P0 | 阻塞 | AC-M32-09 | §8.6 |
| R-M32-33 | 逐层剥离：在缩点图上，第 1 层为无入边的分量，移除后重复；同一分量内条目同层 | P0 | 阻塞 | AC-M32-09 | §8.11 逐层剥离 |
| R-M32-34 | 参考行层级：`1 + max{支配它的候选条目的层级}`，无候选条目支配时为 1；"候选条目支配参考行"按 F-22 以 F_domref 判定；参考行不参与强连通分量计算 | P0 | 阻塞 | AC-M32-09 | F6；v0.2 修订 |
| R-M32-96 | F_domref：对每个参考行 ρ、每个候选条目 A、每个分层指标 k，检验"A 在 k 上优于 ρ"（定向，单侧）；该族独立于 F_dom 施加 Holm；p 值方法同 F_dom（复本或解析路径，按 R-M32-15 判定）；族登记于 `stats.families.dom_ref`，比较写入 `comparisons[]` | P0 | 阻塞 | AC-M32-09、AC-M32-37 | 本文 F-22；v0.2 新增 |
| R-M32-35 | 分层指标只取对全部候选条目测得的指标（V-5）；只对候选与前 k 名测得的 `Δ_perm`、`S_rob` 不进入分层 | P0 | 阻塞 | AC-M32-01 | §8.6 分层指标的可得性 |

### 4.7 失败、无效与完整性

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-36 | 策略原因失败（`failure.policy = true`）：结果类指标置为不利端（F-23），不利端无界时取 `penalty`，未声明 `penalty` 且 `nullable = true` 时置 null；主指标 `U = 0`；消耗类指标保留实际消耗；安全类指标保留实测值；`fail_rate` 由 `failure.policy` 标志计 1 | P0 | 阻塞 | AC-M32-10 | §8.6 失败计分、ADR-D4-24；v0.2 修订 |
| R-M32-37 | 基础设施故障（`failure.infra = true`）：由流水线重跑一次（沿用 `py:bench/subworker.py` 的平台原因重跑），重跑行以 `attempt + 1` 追加；重跑仍为基础设施故障的行键记为缺失，并计入 `infra_missing`（R-M32-92） | P0 | 阻塞 | AC-M32-10 | §8.6；v0.2 修订 |
| R-M32-38 | 无效运行：`invalid_policy = zero`（缺省）时按 R-M32-36 的方向化规则处理 `ranked_valid = false` 的 Episode，不剔除，无阈值，条目不因无效占比而 `invalid`；`invalid_policy = exclude` 须在 Suite 清单给出理由并声明 `invalid_threshold` θ（`drone4d-event@1` 取 0.2），语义按 F-45：有效主评测 Episode 数严格少于 `(1 − θ)` 倍主评测 Episode 总数时条目 `status = invalid`，以有效 Episode 计算并展示指标，但不进入候选集合 C（无 `rank`、`tie_group`、`rank_ci95`、`uncertain_group`，`dominance_tier = null`，不进入 F_rank、F_dom、F_domref）；否则只按有效 Episode 计算并正常排名。本文不采纳 AWR-06 §8.6"全部分量记 0"对消耗类指标的字面含义（反馈第 2 条） | P0 | 阻塞 | AC-M32-10 | §8.6 无效运行、§8.12 偏差 D-2 与"无效条目门"；本文 §13 第 24 条；v0.2 修订 |
| R-M32-39 | 条目完整性：按 F-24，任一场景有效外生种子少于 `min_valid_seeds`（缺省 3）时条目 `status = incomplete`，不参与名次、并列与分层，仍展示点估计与区间；`status` 的优先序为 `invalid` 先于 `incomplete` | P0 | 阻塞 | AC-M32-10 | §8.6 条目完整性；v0.2 修订 |
| R-M32-40 | 每个条目报告无效运行占比、失败占比、`infra_missing` 与平台重跑数（`attempt > 0` 的有效行数），进入条目卡 | P0 | 应交付 | AC-M32-10 | §8.4 安全指标；LB §13.3 `quality`；v0.2 修订 |

### 4.8 干预对照编排与 Δ 族

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-41 | `InterventionPlan.expand` 把"场景集 × 外生种子 × 重复 × 干预（`none`、`cut`、`perm_full`、`perm_struct`、`ind`、`central`）× 扰动"展开为 Episode 规格；同一单元格的 `run_seed` 对全部条目、干预与重复相同；`cut`、`perm_*` 的 Episode 规格携带序列化的 `CutSpec`/`PermSpec`（纯数据），由 M29 读取执行 | P0 | 阻塞 | AC-M32-16 | §8.5、§8.7；v0.2 修订 |
| R-M32-42 | 两阶段展开：第一阶段为冒烟集、主评测与 `cut`（全部提交）；第二阶段为 `perm_*` 与 X1（候选与各赛道前 k 名，缺省 k = 5），在第一阶段 SCORING 完成后依主排名生成；`perm_*` 依赖供体 Episode，须在主评测完成后执行 | P0（第一阶段）、P1（第二阶段） | 阻塞 | AC-M32-16 | §8.5 成本控制 |
| R-M32-43 | 参考缓存：`ind`、`central` 不为提交重跑，读取参考缓存；缓存键采用 M31 R-M31-45 在 WP-D4-13 冻结的定义，本文不自行定义；本文对统一定义的提议见 §13.2 第 10 项（`scenario_digest`、`seed_index`、`reference_ref`（基线标识@精确版本）、`impl_sha256`、`attrs_digest`、`track_id`、`kernel_version`、`intervention`，不含 `suite_digest`）；缺失键时展开为平台侧参考运行请求，计入平台运维预算 | P0 | 阻塞 | AC-M32-16 | §8.5、§10.2 关键路径说明；M31 R-M31-45；M30 R-M30-65；本文 §13 第 30 条；v0.2 修订 |
| R-M32-44 | Δ 估计：`Δ_X = (1/S) Σ_s mean_i [Ū(π; s, i) − Ū(π^X; s, i)]`（F-25），只在共同单元格上配对；区间与 p 值按 R-M32-16 | P0 | 阻塞 | AC-M32-12 | §8.5 |
| R-M32-45 | `R_central = μ(π_central) − μ(π)`（F-26）；`G` 的区间：`family` 下以 Fieller 法（F-27），分母 b 的 95% 区间取 bootstrap-t；`fixed_set` 下取复本比值 `â*_j / b̂*_j` 的 percentile 区间，分母 95% 区间取 `b̂*_j` 的 percentile；同时报告分子与分母的 95% 区间；分母 95% 区间下界 < 0.05、Fieller 区间无界或 `fixed_set` 下 `b̂*_j ≤ 0` 的复本占比 > 2.5% 时记 `n/a`；允许 `G > 1` | P0 | 阻塞 | AC-M32-14 | §8.5 `G`；v0.2 修订 |
| R-M32-46 | 资源对齐校验：对照 Episode 与参考行 Episode 的 `track_id`、`kernel_version`、`attrs_digest`、资源包摘要、网络档参数摘要与预算定义须与主评测 Episode 相同，否则该对记为不可用并给出 `why = resource_misaligned`；与参考缓存键的字段逐项对应 | P0 | 阻塞 | AC-M32-13 | §8.5 资源预算对齐；v0.2 修订 |
| R-M32-47 | 可用对比例 `ρ_use`（F-28）< 0.8 时该条目该项 Δ 记 `n/a`；同时输出"含违规对"与"不含违规对"两版估计作为敏感性分析 | P0 | 阻塞 | AC-M32-13 | §8.5 第 3 条 |
| R-M32-48 | `Δ_perm`：定义 `PermSpec`（供体映射 σ 为场景内种子序号循环移位 `i → (i + 1) mod m_s`，供体为同一条目、同一场景、同一重复的主评测 Episode；匹配键为同 `channel`、同 `schema`、同发送角色、同大小档 `size_class`，取相对时刻最近者，与 M33 `MessageIndex` 的键一致；`L_perm = 30 s` 回退）；由 M29 执行记录计算 `r_fb`（F-29），`r_fb > 0.2` 记 `n/a`；登记字段角色的 schema 以 `perm_struct` 为主口径、`perm_full` 并报，opaque 载荷只有 `perm_full`；边界情形见 R-M32-97 | P1 | 阻塞 | AC-M32-15 | §8.5 `Δ_perm`；M33 R-M33-76；v0.2 修订 |
| R-M32-97 | `Δ_perm` 边界情形：(a) 场景有效种子数 `m_s < 2` 时 σ 为恒等映射，该场景的 `Δ_perm` 不可估计，场景从 `Δ_perm` 的分母中剔除；全部场景均如此时 `Δ_perm` 记 `n/a`（`why = donor_unavailable`）；(b) `PermSpec.exempt_channels`（缺省 `["_ix"]`）中的控制消息在 `perm_*` 下照常投递、不置换，不计入置换投递数，执行记录单列其计数；在 `cut` 下按 `CutSpec` 阻断（R-M29-77）；(c) 供体 Episode 因基础设施故障缺失时，该对照 Episode 仍执行，其全部置换投递按回退规则处理并计入 `r_fb` 分子；(d) 回退亦无同类陈旧消息时该投递按"阻断"处理并单列计数 | P1 | 阻塞 | AC-M32-15 | §8.5；M29 R-M29-77；v0.2 新增 |
| R-M32-49 | 过敏反应审计：`U(π^perm)` 对 `U(π_ind)` 的定向单侧检验（不纳入 F_rank，α = 0.05）显著更低时置 `audit_flag = perm_oversensitive`，只影响条目卡标注 | P1 | 应交付 | AC-M32-15 | §8.5 防护 |
| R-M32-50 | `η_comm`（F-30）：分子取 `max(0, Δ_perm)`，P0 无 `Δ_perm` 时以 `Δ_cut` 代替并标注"依赖度口径"；分母按 `b_tx_ratio` 计算，对数由确定性函数 `ln_d` 计算（ND-9）；`b_tx_ratio` 场景均值为 0 时记 `n/a`；只给 percentile 区间 | P0 | 应交付 | AC-M32-12 | §8.4 通信效率诊断量；v0.2 修订 |
| R-M32-51 | 扩容收益（`U(π) − U(π_single)`，含 D3 `Ĝ`）只以"扩容收益"名称输出，不得出现在"协作增益"字段 | P0 | 应交付 | AC-M32-12 | ADR-D4-22 |
| R-M32-52 | X1 聚合：按扰动族分别计算 `Ū_q`、`S_rob`（F-31）与 `L_deg`；X1 场景子集为独立分析组（`scenario_set` 取 X1 子集 id），估计目标为 `fixed_set`（X1 R-RC-48），下标种子取 X1 §8.5 的域 `drone4d.x1.resample.v1` 并附 `protocol_version`，同一复本的种子下标对全部强度档、全部扰动族、全部条目与参考行相同；95% 区间半宽 > 0.1 时标注"不确定" | P1 | 阻塞 | AC-M32-30 | §7.4、AC-RC-3；X1 §8.5；v0.2 修订 |
| R-M32-98 | X1 判定：对每个条目 A 与每个适用族 f，比较 A 对 `π_ind`、A 对宿主代表性协同基线的 `S_rob^f` 差值（选向，双侧 percentile 复本 p 值）；检验族 F_rob(A) 为条目 A 在全部适用族上的上述比较（至多 14 个），施加 Holm；判定词为"显著更稳健""显著更不稳健""未判定"（X1 §10.5），X1 不做等价判定；比较写入 `comparisons[]`（`family = rob`），族登记于 `stats.families.rob` | P1 | 阻塞 | AC-M32-30 | X1 §10.5；v0.2 新增 |
| R-M32-99 | X1 时间量与完整性：(a) `p_det^f`、`p_rec^f` 为按场景等权的比例（F-43）；`T̃_detect^f`、`T̃_recover^f` 为未删失对象（或 Episode）值的场景等权加权中位数，删失值不代入中位数（X1 R-RC-44、R-RC-47），同时输出删失比例与分辨下限；区间以同一复本的种子权重重算，取 percentile；(b) 族 f 对条目的完整性按 X1 R-RC-54：每个（强度档、场景）格至少 2 个有效种子（X1 视图 `min_valid_seeds = 2`），否则该族 `status = incomplete`，`S_rob^f`、`L_deg^f` 记 `n/a` | P1 | 阻塞 | AC-M32-30 | X1 §8.2、§8.3、R-RC-54；v0.2 新增 |
| R-M32-53 | 研究分析函数：方向 A 的饱和规模 `N*` 与负协作区域、方向 B 的 `B*_θ`（F-32）与 `Δ_perm^(c)`、H3 的 Kendall τ 区间，均以本文估计量与判定词实现 | P2 | 可延期 | AC-M32-33 | §9.1、§9.2 |
| R-M32-54 | `interactive` 模式下 `Δ_cut`、`Δ_perm` 标注"总效应"，不作机制解释 | P0 | 应交付 | AC-M32-12 | §8.5 适用范围 |

### 4.9 资源账本核算

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-55 | `LedgerCheck` 以 M28 运行清单 `ledger_summary`（`{det{<agent>{dim: int}}, team{dim: int}, measured{<agent>{dim: number}}, violations[]}`，M28 §8）为输入，按 F-34 计算团队级 `b_tx_ratio`、`cpu_ratio`、`token_ratio` 与每 Agent 最大比 `cpu_ratio_max`；能量以 M28 的整数 `energy_uwh` 参与额度比较，对外指标 `energy_wh = energy_uwh / 10⁶`；预算维度 `tokens` 定义为 `tokens_in + tokens_out`，Suite 也可分别声明 `tokens_in`、`tokens_out` | P0 | 阻塞 | AC-M32-17 | §8.4 资源指标、§4.2 `ResourceLedger`；M28 §6.3.9；v0.2 修订 |
| R-M32-56 | 违规重算：按 Suite `budget.dims` 的 `per`（`agent`/`team`）、已求值额度与 `enforce` 重算：`enforce = violation` 的维度重算违规集合并与 `ledger_summary.violations` 逐项比对；`enforce = refuse` 的维度只核对使用量不超过额度（超额本应在提交时被拒绝），超额即判平台故障；任一不一致返回 `why = ledger_mismatch`，按基础设施故障处置。`enforce` 缺省：`tx_bytes`、`app_bytes`、`tokens`、`tokens_in`、`tokens_out` 为 `refuse`（M28 R-M28-69：字节硬额度由 M29 以 `TxReceipt(reason = budget)` 执行），`cpu_ms`、`gpu_ms` 为 `violation` | P0 | 阻塞 | AC-M32-17 | §5.7 W-4；S1 §6.4 与其反馈第 7 条；v0.2 修订 |
| R-M32-57 | `budget.policy` 的计分映射：`ranked_valid` 与 `invalid_reasons` 由 M28 写入，M32 由重算的违规集合与 `budget.policy` 推出期望值并与行内记录比对，不一致返回 `ledger_mismatch`；`violation_invalidates` 的期望为 `ranked_valid = false` 且 `invalid_reasons` 含 `budget_violation:<dim>`，M32 随后按 `invalid_policy` 计分；`violation_zero` 的期望为 `ranked_valid` 不因预算改变、`invalid_reasons` 含 `budget_zero:<dim>`，M32 只把主指标置 0，其余分量保留。期望值的最终形式待 M28 确认（§13.2 第 2 项） | P0 | 阻塞 | AC-M32-17 | §4.4 `budget.policy`；M28 R-M28-69、R-M28-71；v0.2 修订 |
| R-M32-58 | 计量来源：`ranked = true` 且所属赛道非影子赛道（LB 的 P0 影子榜）的行要求 `cpu_source = cgroup`，否则判平台故障 `why = ledger_unmetered`；非排名运行与影子赛道允许 `process_time` 来源，并在报告与条目卡中标注"近似计量"；`cpu_source` 由 M28 写入 `ledger_summary`（待 M28 增补，§13.2 第 1 项），缺失时按 `process_time` 处理 | P1 | 阻塞 | AC-M32-17 | §5.7 W-4；LB §8.4；v0.2 修订 |
| R-M32-59 | 额度为 0 的维度（如 B1 的 token）：`enforce = refuse` 时使用量须为 0，比值记 0，使用量 > 0 判 `ledger_mismatch`（拒绝未生效）；`enforce = violation` 时使用量为 0 比值记 0，使用量 > 0 即违规，比值记 null | P0 | 应交付 | AC-M32-17 | §8.3 B1；S1 §6.4；v0.2 修订 |
| R-M32-60 | 条目账本摘要：每个条目输出各维度场景等权均值、P95、违规 Episode 占比、被拒提交数（取自 M29 计数，若行内提供）与 `cpu_source`，写入 `entries[].ledger` | P0 | 应交付 | AC-M32-17 | §4.7 `board.v2`；v0.2 修订 |

### 4.10 跨保真度审计

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-61 | `fidelity_audit` 接收两档配对运行的分量与 M28 提供的 FC-1 至 FC-3 结果，计算 FC-4 至 FC-7，输出 `fidelity_audit.v1`（判据、度量值、阈值、状态 `pass/fail/not_run`、处置建议、样本说明） | P1 | 阻塞 | AC-M32-18 | §5.8 |
| R-M32-62 | FC-4：同一基线两档配对差 `U_low − U_high` 的 90% 区间落入 `[−ε_f, ε_f]`（`ε_f = 0.05`）；估计目标固定为 `fixed_set`（结论限定于审计所选场景），以场景内种子重采样构造 percentile 区间；报告该设计下的等价可达率（MC-9）；区间未落入时报告偏差方向与量级 | P1 | 阻塞 | AC-M32-18 | §5.8 FC-4；本文 §13 第 9、27 条；v0.2 修订 |
| R-M32-63 | FC-5：同一条目集两档 `U` 点估计的 Spearman ρ（平均秩处理并列）、Kendall τ_b 与 Top-k 重合率（k = max(3, ⌈0.25 K⌉)）；ρ < 0.65 置"触发调查" | P1 | 阻塞 | AC-M32-18 | §5.8 FC-5 |
| R-M32-64 | FC-6 与 FC-7：审计族 F_fid 为低保真档中全部审计条目的无序对（选向，Holm），"显著成对差"为 F_fid 内经 Holm 拒绝的比较；FC-6 为其中高保真档点估计符号一致的比例，并列出全部"两档均显著且方向相反"的对（高保真档以同一族定义另行检验）；F_fid 显著对少于 5 个时 FC-6 状态为 `not_run`；FC-7 以时钟档 `d4_quick` 与 `d4_std` 重复同一计算 | P1 | 阻塞 | AC-M32-18 | §5.8 FC-6（"`L1` 上经 Holm 校正显著的成对差值"）、FC-7；本文 §13 第 27 条；v0.2 修订 |
| R-M32-65 | 样本说明：报告场景数、种子数、基线数与每项判据的有效样本；样本低于 §5.8 最低设计时判据状态为 `not_run`，不得以少量样本给出 `pass` | P1 | 阻塞 | AC-M32-18 | §5.8 样本设计 |
| R-M32-66 | FC-8：按 §6.3 F-36 的定义计算 PDR–距离曲线相对 L1 距离与 P95 时延相对差（P2） | P2 | 可延期 | AC-M32-18 | §5.8 FC-8；本文 §13 第 12 条 |

### 4.11 复核与审计

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-67 | `verify_board(rows, config, board)`：由 `rows.jsonl` 与榜单配置重算 `board.v2`，以 JCS 规范字节比对；不一致时输出差异的 JSON Pointer 路径列表；`board.v1` 输入走 D3 兼容路径 | P0 | 阻塞 | AC-M32-19 | §8.10、AC-D4-15 |
| R-M32-68 | `stats` 自述块须完备到第三方可不读本仓库代码独立复算：估计目标、方法、`n_boot`、下标生成器与种子规则、场景排序规则、族定义、规模与方向属性、Holm、ε、`ε_eq`、`rank_family`、解析路径族与参照分布、区间方法、`invalid_policy` 与 `invalid_threshold`、支配参数、数学函数库版本（`dmath_version`）与 `stats.version` | P0 | 阻塞 | AC-M32-19 | §8.6 末段；v0.2 修订 |
| R-M32-69 | `verify_commitments(salt, seeds, rows, board)`：按 CT §3.4.3 的字节编码（十进制字符串、按数值排序、原始 32 字节盐、域分隔前缀 `drone4d.board.seed_tag.v1` 与 `LP`/`u64be` 拼接，TV-01 至 TV-04；AWR-06 v0.3 XR-07）验证 `seed_set_commit`、逐行 `seed_tag` 与行内声称的种子序号一致，并输出需在同硬件等级复跑核对 `truth_digest` 的抽样清单 | P1 | 阻塞 | AC-M32-20 | §8.7、ADR-D4-25、AC-D4-23 |
| R-M32-70 | `rerun_check`：R-1（新种子复跑 n_r = 10 个 Episode）与 R-2b（跨硬件等级抽样）按 F-39 的预测区间判定：复跑均值与原估计之差的绝对值不超过 `z_{1−α_r/2} · sqrt(ŝ_o² + ŝ_r²) + ε`（α_r = 0.01），ŝ_o 为原估计的标准误，ŝ_r 由原条目的 Episode 层标准差除以 `sqrt(n_r)` 得到；输出判定、差值与阈值；诚实条目误判率与偏差检出率由 MC-12 评估 | P1 | 阻塞 | AC-M32-21 | §8.10；本文 §13 第 22 条；v0.2 修订 |
| R-M32-71 | `audit_baselines`：比对参考策略抽样复跑的 `truth_digest` 与分量；任一不一致即返回 `freeze` 结论与差异清单，供 L6 冻结该 Board Version 的新评测 | P1 | 阻塞 | AC-M32-21 | §8.10 平台侧复核工具 |
| R-M32-72 | `board.v1` 影子重算：先施加与 `boardstore._listed` 相同的上榜过滤（F9），再以 `board.v2` 统计协议（`fixed_set`、共享下标、组首并列、`invalid_policy` 两种口径）重算 D3 `rows.jsonl`，输出逐条名次与并列组变化报告，不替换 `board.v1` | P0 | 阻塞 | AC-M32-33 | §10.1 第 (3) 项、RK-13；v0.2 修订 |
| R-M32-101 | 等级 2 重放比对容差（供 M33 R-M33-65）：`replay_tolerance(spec)` 对每个分量给出判据 `|a − b| ≤ τ_abs,k + τ_rel · max(|a|, |b|)`，`τ_abs,k = 10^(−precision_k)`、`τ_rel = 10⁻⁹`（缺省）；null 与非 null 不一致、布尔量与整数计数（`ranked_valid`、`failure.*`、违规集合）不一致一律判不一致；Suite 可按指标声明更宽的 `replay_tol`，须在评测器卡中给出理由；缺省值在 WP-D4-27 以 `L2` + `realistic` 重放实测后确认 | P1 | 应交付 | AC-M32-38 | §5.6 等级 2；M33 R-M33-65；v0.2 新增 |

### 4.12 D3 兼容委托

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-73 | `py:bench/eval/aggregate.py` 保留全部公开符号（`METRICS`、`N_BOOT`、`build_board`、`compose`、`entry_details`、`lane_metrics`、`ranked_row`）及签名；实现委托给 `py:evaluation/compat/d3_board_v1.py` | P0 | 阻塞 | AC-M32-22 | §3.3 M32 行、WP-D4-09 |
| R-M32-74 | 全部 D3 调用方（F8）在委托前后输出逐字节一致：`board.json`、`details.json`、`compose` 返回值、`lane_metrics` 返回值与提交结果 | P0 | 阻塞 | AC-M32-22 | B-07、AC-D4-13 |
| R-M32-75 | `awr.evaluation` 不 import `awr.bench`；D3 版本常量（`EVALUATOR_VERSION`、`SCORE_VERSION`）由 `aggregate.py` 作为参数传入 | P0 | 阻塞 | AC-M32-23 | B-02、B-10；本文 §13 第 15 条 |
| R-M32-76 | D3 兼容实现满足 §8.2 的不变量 D3-INV-01 至 D3-INV-20；对 MetricSpec 参数化，但 D3 调用路径只传入四项 D3 指标；不变量清单与冻结原版不一致时以冻结原版的对拍结果为准，并修订清单 | P0 | 阻塞 | AC-M32-22 | 本文 §8；v0.2 修订 |
| R-M32-77 | Suite `drone4d-event@1` 在 `board.v2` 中的口径：`estimand = fixed_set`、`rank_mode = per_metric`（R-M32-100）、四项指标 `direction = max`、`ce` 可空；`invalid_policy = exclude`、`invalid_threshold = 0.2`，与 M31 R-M31-61"沿用 D3 当前行为"一致；`zero` 口径只出现在影子重算报告中 | P0 | 应交付 | AC-M32-33、AC-M32-39 | ADR-D4-19、§8.6 估计目标；M31 R-M31-61；v0.2 修订 |

### 4.13 本地实验报告与 pilot 功效函数

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-78 | `analyze_experiment(rows, spec)`：为 `drone4d bench report` 输出每个策略的 `U` 与资源指标区间、相对参考策略的配对差与判定词、名次区间；`S < 12` 时给出"场景数低于正式成榜下限"的提示；输出数据结构供 M34 渲染 md/html | P0 | 阻塞 | AC-M32-28 | §4.4、§5.10 |
| R-M32-79 | pilot 函数：`pilot_power` 估计配对差的 ICC（单因素方差分析估计量）与 `σ_D`，并以实例层（场景层）自助法给出二者的单侧 80% 上界（F-37，不使用按独立样本自由度计算的 χ² 上界）；`required_scenarios` 按 F-37 计算最小场景数；`power_table` 复现 §8.6 功效表；`pa_checks` 计算 PA-2 至 PA-4；上界的覆盖率由 MC-13 评估 | P0 | 阻塞 | AC-M32-29 | §7.0、§8.6 功效与样本量；本文 §13 第 10 条；v0.2 修订 |

---

## 5. 非功能需求

| 编号 | 需求描述 | 阶段 | 级别 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-M32-80 | 依赖：第三方只依赖 numpy；判定路径上的超越函数（`ln`、`exp`、`atan`、Φ、Φ⁻¹、整数自由度 Student t 分布函数）由 `numerics.dmath` 自含实现（ND-9），不调用 `math.log`、`math.exp`、`math.erf` 与 `statistics.NormalDist`；不依赖 scipy、jsonschema、numba | P0 | 阻塞 | AC-M32-24、AC-M32-37 | B-02、AC-D4-43；v0.2 修订 |
| R-M32-81 | 数值确定性：满足 §6.5 的 ND-1 至 ND-11；同一输入在不同机器、不同硬件等级、不同 BLAS 线程数、不同 numpy SIMD 派发范围与 numpy 1.26 至锁定版本之间产生逐字节一致的 `board.v2` | P0 | 阻塞 | AC-M32-25 | §5.6；v0.2 修订 |
| R-M32-82 | 性能（目标值）：单个分析组 K = 100 个候选条目、S = 18、m = 5、R = 1、6 项指标（其中 3 项分层）、`rank_family = head`，在 RM-8 单进程上 `build_board_v2`：`family` ≤ 60 s，`fixed_set` ≤ 90 s；`rank_family = all_pairs`（`n_boot = 200,000`）≤ 180 s；峰值内存 ≤ 2 GiB。F_dom 与 F_domref 在解析路径下不生成逐对复本差值（`fixed_set` 用 F-42 闭式方差）；超出时以分块计算满足，不降低 `n_boot` | P0 | 阻塞 | AC-M32-26 | F10；§5.10 RM-8；v0.2 修订 |
| R-M32-83 | 本地报告：北极星配置（4 个算法 × 10 个实例 × 10 个种子）的 `analyze_experiment` ≤ 5 s（RM-8） | P0 | 阻塞 | AC-M32-28 | §5.10 |
| R-M32-84 | 兼容 CPython 3.12（AWR-06 v0.3 XR-02：wheel 声明 `>=3.12,<3.13`，与仓库根一致；numpy 2.5.3 的 `Requires-Python` 为 `>=3.12`） | P0 | 阻塞 | AC-M32-24 | 附录 E 第 41 条 |
| R-M32-85 | 纯函数：核心函数不读墙钟、不读环境变量、不访问网络；文件读写只出现在 `verify` 的可选加载辅助函数中 | P0 | 阻塞 | AC-M32-25 | B-05 |
| R-M32-86 | 版本化：统计算法、缺省参数或 `dmath` 实现的任何变化须提升 `stats.version`（v0.2 定稿后首版 `drone4d.eval.stats.v2@1`），并按 §8.9 发布新 Board Version | P0 | 阻塞 | AC-M32-19 | §8.9；v0.2 修订 |
| R-M32-87 | 文案：全部用户可见文字（判定词、`n/a` 原因、提示）为企业级中文与英文对照键，不使用口语化表述 | P0 | 应交付 | AC-M32-34 | 项目文案规范 |
| R-M32-102 | 确定性数学函数库 `numerics.dmath`：只用 IEEE 754 双精度的加、减、乘、除、开方、比较与 `frexp`/`ldexp` 构造 `ln_d`、`exp_d`、`atan_d`、`phi_d`（Φ）、`phi_inv_d`（Φ⁻¹）、`t_cdf_d`（整数自由度）；运算顺序在本文 §6.5.1 中规定，禁止融合乘加；常用分位数以十六进制浮点字面常量给出；发布每个函数不少于 50 个点的黄金向量，第三方以任意语言按规格实现应逐位一致 | P0 | 阻塞 | AC-M32-37 | 本文 ND-9、ND-10；v0.2 新增 |
| R-M32-103 | numpy 版本无关与依赖声明：`awr.evaluation` 的 D4 路径只使用 numpy 1.26 起即存在且语义未变的接口（逐元素算术、稳定排序、整数位运算、`frexp`/`ldexp`），输出与 numpy 版本无关；`drone4d` wheel 须把 numpy（`>=1.26`）升为必选依赖，或为 `analyze_experiment` 提供纯 Python 降级路径（二者在 WP-D4-01 RFC 中择一，Q-M32-10；本文建议前者） | P0 | 阻塞 | AC-M32-24、AC-M32-25 | F11；AC-D4-43；本文 §13 第 23 条；v0.2 新增 |

---

## 6. 设计方案

### 6.1 组件与数据流

**包结构**（`python/awr/evaluation/`）

| 模块 | 职责 | 首批公开符号 |
|---|---|---|
| `metric.py` | 指标规格、分析组视图、方向化与校验 | `MetricSpec`、`MetricSet`、`validate_metrics`、`orient` |
| `manifest.py` | `suite.v1` 字段到本文结构的映射（§6.2.3） | `from_suite_manifest` |
| `rows.py` | 行层与行集层校验、`attempt` 归并（R-M32-92） | `Row`、`normalize_rows` |
| `cube.py` | 由行构建数据立方体、失败与无效计分、完整性判定 | `Cube`、`build_cube`、`apply_scoring` |
| `estimand.py` | 估计目标与层次 | `Estimand`、`Hierarchy` |
| `resample.py` | 下标种子派生、场景与种子索引、下标生成器、计数矩阵 | `resample_seed`、`scenario_order`、`scenario_indices`、`seed_indices`、`counts` |
| `bootstrap.py` | 复本统计量、bootstrap-t、percentile、解析路径 | `replicate_means`、`paired_ci`、`ci_mean`、`pvalue`、`analytic_se` |
| `numerics.py`、`numerics/dmath.py` | 确定性归约、分位数；确定性超越函数与常量 | `ordered_sum`、`quantile_linear`；`ln_d`、`exp_d`、`atan_d`、`phi_d`、`phi_inv_d`、`t_cdf_d`、`Z_975` 等 |
| `holm.py` | Holm 逐步校正 | `holm` |
| `ranking.py` | 判定词、并列组、名次与名次区间 | `verdict`、`tie_groups`、`rank_intervals` |
| `dominance.py` | 支配图、F_domref、强连通分量、逐层剥离 | `dominance_graph`、`ref_dominance`、`scc`、`dominance_tiers` |
| `intervention.py` | 干预展开、`CutSpec`、`PermSpec`、供体选择、执行记录校验、Δ 族估计、Fieller | `InterventionPlan`、`CutSpec`、`PermSpec`、`donor_cell`、`check_exec_record`、`delta`、`fieller` |
| `robust.py` | X1 聚合、F_rob 与研究分析函数 | `robust_curve`、`s_rob`、`censored_median`、`f_rob`、`b_star`、`saturation` |
| `ledger.py` | 资源账本核算 | `LedgerSummary`、`LedgerCheck` |
| `fidelity.py` | 跨保真度审计 | `fidelity_audit` |
| `board.py` | 分析组编排与 `board.v2` 内容构建 | `build_board_v2`、`Stats` |
| `verify.py` | 榜单复核、承诺验证、复跑判据、参考审计、重放容差 | `verify_board`、`verify_commitments`、`rerun_check`、`audit_baselines`、`replay_tolerance` |
| `report.py` | 本地实验报告 | `analyze_experiment` |
| `pilot.py` | pilot 功效、样本量与 PA 检查 | `pilot_power`、`required_scenarios`、`power_table`、`pa_checks` |
| `compat/d3_board_v1.py` | D3 聚合的冻结移植（§8）；不进入 wheel | `build_board`、`compose`、`entry_details`、`lane_metrics`、`ranked_row` |

**数据流**（一个分析组的榜单构建）

```
rows.jsonl（evaluation.v1 分量 + ledger_summary + 行键 + attempt + seed_tag）
   │  R-M32-04、R-M32-92 入库校验与 attempt 归并
   ↓
Suite 清单 ──(§6.2.3 映射)──→ MetricSet.view / Scoring.view（分析组视图，V-1–V-12）
   │
   ↓
LedgerCheck ──(重算比对、enforce、budget.policy 映射)──→ apply_scoring（失败/无效方向化计分，exclude 阈值）
   │
   ↓
build_cube ──→ Cube[entry, metric, scenario, seed, repeat, intervention]
   │  F-01–F-03 点估计；F-24 完整性；F-45 无效判定
   ↓
resample（F-07 种子、F-08 下标、R-M32-93 索引、F-09 计数）── 全组共享
   │
   ├─→ replicate_means（F-10/F-11）──→ ci_mean（F-12/F-13）
   ├─→ paired_ci + pvalue（F-12–F-14、F-41、F-42）──→ holm（F-16）──→ verdict（F-17）
   │        └─→ tie_groups（§6.4.2 定点）、rank_intervals（F-19）
   ├─→ dominance_graph（F-20）+ ref_dominance（F-22）──→ scc ──→ dominance_tiers（F-21）
   └─→ delta / fieller（F-25–F-30）、robust（F-31、F-43、F_rob）
   ↓
board.v2 内容 + stats 自述块 + comparisons[]  ──(JCS)──→ L6 写入与发布
```

### 6.2 数据结构

#### 6.2.1 Python 草案（Python 3.12）

```python
# py:evaluation/metric.py
@dataclass(frozen=True)
class MetricSpec:
    id: str                              # 字符集 [a-z0-9_]{1,48}
    kind: str                            # task | coop | resource | safety | robustness | calibration
    direction: str                       # max | min | target
    range: tuple[float | None, float | None] | None = None
    unit: str = ""
    nullable: bool = False
    primary: bool = False
    tier: bool = False
    aggregation: str = "mean"            # mean | iqm | q10 | fail_rate
    target: tuple[float, float] | None = None       # 本文增补：direction = target 时必填
    level: str = "episode"               # 本文增补：episode | scenario
    epsilon: float | None = None         # 本文增补：None 时取 R-M32-24 缺省
    scenario_sets: tuple[str, ...] | None = None    # v0.2：None = 全部场景集（R-M32-88）
    penalty: float | None = None         # v0.2：无界不利端的失败代入值（R-M32-89）
    companions: tuple[str, ...] = ()     # v0.2：⊂ {iqm, q10}；主指标缺省 (iqm, q10)（R-M32-90）
    precision: int = 6                   # v0.2：输出与评测器舍入位数（M31 R-M31-40）
    supports_subset: bool = False        # v0.2：子编队投影（M31 R-M31-42）
    replay_tol: float | None = None      # v0.2：等级 2 重放的绝对容差覆写（R-M32-101）

@dataclass(frozen=True)
class MetricSet:
    metrics: tuple[MetricSpec, ...]
    def view(self, scenario_set: str) -> "MetricSet": ...   # 按 scenario_sets 取子集
    def primary(self) -> MetricSpec | None: ...             # 只在视图上调用
    def tier(self) -> tuple[MetricSpec, ...]: ...

# py:evaluation/rows.py
@dataclass(frozen=True)
class Row:                               # rows.jsonl 一行的统计视图
    row_schema: str                      # 例："drone4d.suite.row.v1"
    entry_id: str; team: str; track: str; scenario_set: str
    scenario_id: str; seed_index: int; repeat: int
    intervention: str                    # none | cut | perm_full | perm_struct | perm_class:<c> | ind | central
    perturbation: tuple[str, int] | None # X1：(family, q)，q = 0..4
    attempt: int                         # v0.2：自 0 起；平台原因重跑 + 1（R-M32-92）
    reference_kind: str | None           # ind | central | random | single | published | None
    legacy_baseline: bool                # v0.2：D3 baseline = true 的行（R-M32-100）
    components: Mapping[str, float | None]
    ranked: bool; ranked_valid: bool; invalid_reasons: tuple[str, ...]
    failure_policy: bool; failure_infra: bool
    ledger: "LedgerSummary"
    exec_record: "ExecRecord | None" = None   # M29 干预执行记录摘要
    x1_times: "X1Times | None" = None         # 评测器给出的 T_detect / T_recover 值与删失标志
    training_artifact: str | None = None      # P1
    episode_id: str = ""; seed_tag: str = ""; truth_digest: str = ""
    track_id: str = ""; kernel_version: str = ""; attrs_digest: str = ""
    resource_digest: str = ""; network_digest: str = ""

# py:evaluation/board.py
@dataclass(frozen=True)
class Scoring:                           # 来自 suite.v1 的 scoring 与 budget（§6.2.3）
    estimand: str                        # family | fixed_set（可按场景集覆写，R-M32-88）
    rank_mode: str = "primary"           # primary | per_metric（仅 drone4d-event@1）
    invalid_policy: str = "zero"         # zero | exclude
    invalid_reason: str = ""             # exclude 时必填
    invalid_threshold: float | None = None   # exclude 时必填，θ ∈ (0, 1)；drone4d-event@1 为 0.2
    tie_rule: str = "group_head"
    rank_family: str = "head"            # head | all_pairs（Q-M32-01）
    alpha: float = 0.05
    epsilon_practical: float = 0.02
    epsilon_resource: float = 0.05
    epsilon_equiv: float = 0.05
    n_boot: str | int = "auto"
    n_boot_cap: int = 200_000
    dominance_tolerance: str = "epsilon"
    scc_merge: bool = True
    min_valid_seeds: int = 3             # X1 视图为 2（R-M32-99）
    budget_policy: str = "violation_invalidates"
    budget_dims: tuple["BudgetDim", ...] = ()
    def view(self, scenario_set: str, overrides: Mapping[str, str]) -> "Scoring": ...

@dataclass(frozen=True)
class BudgetDim:
    dim: str; per: str                   # per ∈ {agent, team}
    enforce: str                         # refuse | violation（缺省见 R-M32-56）

@dataclass(frozen=True)
class GroupKey:
    board_version: str; track: str; scenario_set: str

@dataclass(frozen=True)
class Stats:
    version: str                         # "drone4d.eval.stats.v2@1"
    method: str                          # "cluster_bootstrap_t" | "seed_bootstrap_percentile"（与 suite.v1 evaluation.ci.method 同值域）
    estimand: str; alpha: float; n_boot: int; n_boot_cap: int
    index_generator: str                 # "splitmix64-mulhi32@1"
    resample_seed_rule: str              # "sha256(drone4d.board.resample.v1‖board_version‖track‖scenario_set)[0:8] LE"
    scenario_order: str                  # "scenario_id codepoint asc; S = declared set size"
    families: Mapping[str, Mapping[str, object]]   # {rank|dom|dom_ref|rob|fid: {size, sidedness, members_digest}}
    rank_family: str; rank_family_converged: bool
    analytic_families: Mapping[str, str] # 族名 → "student_t" | "normal_closed_form"
    holm: bool; epsilon: float; epsilon_resource: float; epsilon_equiv: float
    ci_methods: Mapping[str, str]        # 量名 → bootstrap_t | percentile | fieller | ratio_percentile
    invalid_policy: str; invalid_threshold: float | None
    dominance: Mapping[str, object]      # {tolerance, scc_merge, metrics}
    dmath_version: str                   # "dmath@1"
    stage2_selection: Mapping[str, object] | None

# py:evaluation/intervention.py
@dataclass(frozen=True)
class CutSpec:                           # 纯数据，随 Episode 规格下发，M29 执行
    scope: str = "all_deliveries"        # 阻断全部投递，含 exempt 以外与 _ix 控制消息
    include_control: bool = True         # R-M29-77：控制消息受 cut 阻断

@dataclass(frozen=True)
class PermSpec:                          # 纯数据，随 Episode 规格下发，M29 执行、M33 MessageIndex 检索
    mode: str                            # perm_full | perm_struct | perm_class:<info_class>
    donor_rule: str = "cyclic_shift@1"   # σ(i) = (i + 1) mod m_s；m_s < 2 时不可估计（R-M32-97）
    match: tuple[str, ...] = ("channel", "schema", "sender_role", "size_class")  # 与 M33 MessageIndex 键一致
    order: str = "nearest_rel_time"
    exempt_channels: tuple[str, ...] = ("_ix",)
    l_perm_s: float = 30.0
    r_fb_max: float = 0.2

def donor_cell(scenario_seeds: Sequence[int], seed_index: int) -> int | None: ...   # 纯函数；None 表示不可估计

@dataclass(frozen=True)
class ExecRecord:                        # M29 执行记录摘要（行内 exec_record）
    intervention: str; blocked: int; permuted: int; fallback: int
    fallback_blocked: int; exempt_delivered: int; donor_episode_id: str | None

@dataclass(frozen=True)
class InterventionPlan:
    group: GroupKey
    interventions: tuple[str, ...]       # 例：("cut",) 或 ("cut", "perm_struct", "perm_full")
    stage2_scope_k: int = 5
    cut: CutSpec = CutSpec()
    perm: PermSpec | None = None
    perturbations: tuple[tuple[str, int], ...] = ()   # X1：(family, q)
    def expand(self, entries: Sequence[str], scenarios: Sequence["ScenarioCells"],
               repeats: int, stage: int) -> list["EpisodeSpec"]: ...

# py:evaluation/ledger.py
@dataclass(frozen=True)
class EvaluatedBudget:                   # 待 M28 在 ledger_summary 中增补（§13.2 第 1 项）
    per: str; limit: int; enforce: str

@dataclass(frozen=True)
class LedgerSummary:                     # 与 M28 运行清单 ledger_summary 结构一致（M28 §8）
    det: Mapping[str, Mapping[str, int]]          # agent → {app_bytes, tx_bytes, rx_bytes, energy_uwh, decisions, tokens_in, tokens_out}
    team: Mapping[str, int]                        # 团队汇总（确定性计量维）
    measured: Mapping[str, Mapping[str, float]]    # agent → {cpu_ms, gpu_ms}
    violations: tuple[str, ...]                    # 内核记录，形如 "cpu_ms:agent-03"
    budgets: Mapping[str, EvaluatedBudget] | None = None   # 待 M28 增补；缺失时 LedgerCheck 拒绝排名构建
    cpu_source: str | None = None                  # 待 M28 增补：cgroup | process_time
```

#### 6.2.2 指标集校验规则

以下规则对每个分析组视图（R-M32-88）分别执行。

| 规则 | 内容 |
|---|---|
| V-1 | `rank_mode = primary` 时视图内恰有一个 `primary = true`；`rank_mode = per_metric` 时视图内不得有 `primary`，且只允许 `suite_id = drone4d-event` |
| V-2 | 主指标须 `kind = task`、`direction = max`、`range = [0, 1]`、`nullable = false`、`level = episode`、`aggregation = mean` |
| V-3 | `direction = target` 须给出 `target = [lo, hi]` 且 `lo ≤ hi`；其他方向不得给出 `target` |
| V-4 | `id` 唯一，符合 `[a-z0-9_]{1,48}`；不含分隔符 `‖` |
| V-5 | `tier = true` 须 `nullable = false`、`aggregation = mean`，且 Suite 声明该指标对全部条目测得（不得为 `Δ_perm`、`S_rob` 等只对前 k 名测得的量）；`drone4d-event@1` 的 `ce` 例外（R-M32-100） |
| V-6 | `aggregation ∈ {iqm, q10, fail_rate}` 的指标不得 `tier = true`、不得 `primary = true`；伴随统计量通过 `companions` 声明（V-11） |
| V-7 | `level = scenario` 的指标须声明评测器回调 `recompute(seed_weights)`，否则只允许 `estimand = family` |
| V-8 | `kind = resource` 的指标须 `direction = min` 且 `range` 下界为 0；`epsilon` 缺省取 `ε_r` |
| V-9 | `level = episode` 的结果类指标，若方向化不利端无界（`max` 且 `range` 下界为 null；`min` 且上界为 null；`target` 且 `range` 任一端为 null），须声明 `penalty` 或 `nullable = true`；`penalty` 须有限、落在 `range` 内，并位于不利侧：`min` 时不小于 Suite 声明的该指标有效上限（如 `horizon_s`），`max` 时不大于有效下限，`target` 时位于目标区间之外且 Suite 在评测器卡中说明取值依据 |
| V-10 | `scenario_sets` 中的每个 id 须存在于 Suite 清单 `scenario_sets[]`；每个场景集的视图须非空且满足 V-1 |
| V-11 | `companions ⊂ {iqm, q10}`，只允许 `level = episode` 且 `aggregation = mean` 的指标声明 |
| V-12 | `precision` 为 0 至 12 的整数；`replay_tol` 若给出须为非负有限值 |

#### 6.2.3 `suite.v1` 字段映射

| `suite.v1` 字段（M31 §8.1、各 Suite 清单） | 本文结构 | 规则 |
|---|---|---|
| `metrics[].{id, kind, direction, range, unit, nullable, primary, tier, aggregation, target, level, epsilon}` | `MetricSpec` 同名字段 | 一一对应 |
| `metrics[].scenario_sets` | `MetricSpec.scenario_sets` | 缺省 null（R-M32-88） |
| `metrics[].penalty`、`companions`、`precision`、`supports_subset`、`replay_tol` | `MetricSpec` 同名字段 | v0.2 新增；需 M31 清单 schema 同步（§13.2 第 11 项） |
| `scoring.estimand`；`scenario_sets[].estimand` | `Scoring.estimand`；`Scoring.view` 覆写 | 只允许按场景集覆写 `estimand` |
| `scoring.rank_mode` | `Scoring.rank_mode` | 统一采用 M31 的字段名 `rank_mode`；本文 v0.1 的 `MetricSet.ranking` 废止 |
| `scoring.hierarchy` | `Estimand.hierarchy` | 只接受 `[scenario, seed, repeat]`（含训练产物时为 `[scenario × training_artifact, seed, repeat]`）；其他值拒绝 |
| `scoring.n_boot` | `Scoring.n_boot` | `auto` 或不小于 F-15 结果的整数 |
| `scoring.invalid_policy`、`invalid_reason`、`invalid_threshold` | `Scoring` 同名字段 | `exclude` 时后两项必填 |
| `scoring.tie_rule`、`rank_family` | `Scoring` 同名字段 | `tie_rule` 只接受 `group_head` |
| `scoring.holm`（布尔）或 `scoring.holm.families`（列表） | `Stats.holm`；族集合 | 列表形式只作声明核对：须等于 R-M32-21、R-M32-96 推出的族集合，否则拒绝 |
| `scoring.epsilon_practical`、`epsilon_equiv` | `Scoring` 同名字段 | 一一对应 |
| `scoring.dominance.{metrics_from, tolerance, scc_merge}` 或扁平 `dominance_tolerance`、`scc_merge` | `Scoring.dominance_tolerance`、`scc_merge` | `metrics_from` 只接受 `tier`（分层指标集合由 `MetricSpec.tier` 决定） |
| `budget.policy`；`budget.dims[].{dim, per, limit, limit_expr, enforce}` | `Scoring.budget_policy`；`BudgetDim` | `limit`/`limit_expr` 由 M28 求值，本文只读求值结果；`enforce` 缺省见 R-M32-56 |
| `evaluation.ci.method` | `Stats.method` | 值域统一为 `cluster_bootstrap_t`（`family`）与 `seed_bootstrap_percentile`（`fixed_set`），须与 `estimand` 匹配，否则拒绝；本文 v0.1 的 `stratified_seed_percentile` 废止 |
| `evaluation.ci.level`、`multiple_comparison` | 只接受 0.95 与 `holm` | 其他值拒绝 |

### 6.3 公式

**记号**。分析组内条目 e；候选条目集合 C，|C| = K；指标 k，方向符号 σ_k；场景序号 s = 0..S−1（R-M32-93）；场景 s 的外生种子序号 i = 0..m_s−1；重复 r；`y_{e,k}(s, i, r)` 为经 §4.7 计分（与 F-04 变换）后的 Episode 值。B 为重采样次数，j = 0..B−1 为复本下标。

```
F-01  种子均值      ȳ_{e,k}(s,i) = (1/|R_{s,i}|) Σ_{r∈R_{s,i}} y_{e,k}(s,i,r)          R_{s,i}：非空重复
F-02  场景值        ȳ_{e,k}(s)   = (1/|I_{e,k,s}|) Σ_{i∈I_{e,k,s}} ȳ_{e,k}(s,i)      I：非空种子
F-03  点估计        μ̂_{e,k}     = (1/|S_{e,k}|) Σ_{s∈S_{e,k}} ȳ_{e,k}(s)            S_{e,k}：非空场景
F-04  目标变换      在 Episode 层、F-01 之前施加：y ← g(y) = − max(lo − y, 0, y − hi)；对 g 取 σ = +1     direction = target
                    失败代入（F-23）先于变换施加于原始量纲
F-05  方向符号      σ_k = +1（max、target 变换后）；σ_k = −1（min）
F-06  配对场景差    D_{AB,k}(s) = σ_k · (ȳ_{A,k}(s) − ȳ_{B,k}(s))，s ∈ S_{AB} = S_{A,k} ∩ S_{B,k}，n = |S_{AB}|
                    d̂ = (1/n) Σ_s D(s)；ŝ = sd(D) / √n，sd 取分母 n − 1
```

**重采样**

```
F-07  下标种子      seed64 = LE_u64( SHA-256( UTF-8( domain ‖ p_1 ‖ … ‖ p_k ) )[0:8] )
                    ‖ 为 U+2016 字符（UTF-8 字节 E2 80 96），与 D3 `_rng` 的连接方式一致；各标识不得含该字符（V-4 及标识模式）
                    主榜：     domain = "drone4d.board.resample.v1"，  (p) = (board_version, track, scenario_set)
                    X1：       domain = "drone4d.x1.resample.v1"，     (p) = (board_version, track, scenario_set, protocol_version)（X1 §8.5）
                    跨赛道：   domain = "drone4d.board.resample.xtrack.v1"，(p) = (board_version, scenario_set, track_1, …, track_L)，track 按码点序
                    本地实验： domain = "drone4d.experiment.resample.v1"，(p) = (experiment_digest)
                    pilot：    domain = "drone4d.pilot.resample.v1"，    (p) = (suite_id, scenario_set, pilot_digest)
F-08  下标生成器    k0 = sm(seed64)
                    family：     z_{j,t}   = sm( sm(k0 ⊕ j) ⊕ t ),            idx_{j,t}   = ((z_{j,t} >> 32) · S)   >> 32,  t = 0..S−1
                    fixed_set：  z_{j,s,t} = sm( sm( sm(k0 ⊕ j) ⊕ s ) ⊕ t ),  idx_{j,s,t} = ((z_{j,s,t} >> 32) · m_s) >> 32, t = 0..m_s−1
                    sm 为 py:sim/sensors/cbrng.py 的 SplitMix64 终混；全部运算为 uint64 回绕；映射偏差 ≤ S / 2^32
                    S、s、m_s、t 的取值按 R-M32-93：S 为声明场景集全集的规模，s 按 scenario_id 码点序，
                    m_s 为承诺种子集中场景 s 的种子数，种子按 seed_index 升序；条目数据缺失不改变下标
F-09  计数矩阵      C_{j,s} = #{t : idx_{j,t} = s}（family）；C_{j,s,i} = #{t : idx_{j,s,t} = i}（fixed_set）
F-10  family 复本   μ*_{e,k,j} = Σ_{s∈S_{e,k}} C_{j,s} ȳ_{e,k}(s) / Σ_{s∈S_{e,k}} C_{j,s}       分母为 0 时该复本记缺失
F-11  fixed_set 复本 μ*_{e,k,j} = (1/|S_{e,k}|) Σ_{s∈S_{e,k}} [ Σ_{i∈I_{e,k,s}} C_{j,s,i} ȳ_{e,k}(s,i) / Σ_{i∈I_{e,k,s}} C_{j,s,i} ]
                    任一 s ∈ S_{e,k} 的内层分母为 0 时该复本记缺失
```

**区间与 p 值**

```
F-12  bootstrap-t   N_j = Σ_{s∈S_{AB}} C_{j,s}；N_j < 2 时该复本记缺失
                    d*_j = Σ_s C_{j,s} D(s) / N_j（s ∈ S_{AB}）
                    ŝ*_j = sqrt( Σ_s C_{j,s} (D(s) − d*_j)² / (N_j − 1) ) / √N_j
                    t*_j = (d*_j − d̂) / ŝ*_j
                    (1−γ) 区间 = [ d̂ − q_{1−γ/2}(t*) · ŝ ,  d̂ − q_{γ/2}(t*) · ŝ ]
                    单条目区间同式，以 ȳ_e(s) 代替 D(s)
F-13  percentile    (1−γ) 区间 = [ q_{γ/2}(d*), q_{1−γ/2}(d*) ]
F-14  单侧 p 值     (a) bootstrap-t： p₁ = (1 + #{j : t*_j ≥ t}) / (B_eff + 1)，t = d̂ / ŝ
                    (b) percentile：  p₁ = (1 + #{j : d*_j ≤ 0}) / (B_eff + 1)
                    (c) 解析路径：    family：    p₁ = 1 − T_{n−1}(d̂ / ŝ)              T_ν 为 Student t 分布函数（t_cdf_d）
                                      fixed_set： p₁ = 1 − Φ(d̂ / ŝ_N)，ŝ_N 按 F-42      Φ 为 phi_d
                    B_eff 为有效（非缺失）复本数
F-15  重采样次数    n_boot = min( n_boot_cap, max(10,000, ⌈20 · m_rank / α⌉) )
                    m_rank = max over F_rank 族 of：(K − 1) + |Rf|（head）或 K(K − 1)/2 + |Rf|（all_pairs），|Rf| = K 或 0
                    m_dom  = |T| · K(K − 1)/2；m_domref = |T| · K · n_ref
                    ⌈20 · m / α⌉ > n_boot 的族走解析路径 F-14(c)
```

`q_γ(·)` 为 B_eff 个有效复本按升序排序后的线性插值分位：`h = (B_eff − 1)γ`，`q = x_(⌊h⌋) + (h − ⌊h⌋)(x_(⌊h⌋+1) − x_(⌊h⌋))`（下标自 0 起），与 numpy `method = "linear"` 一致，但以显式公式实现（ND-4）。

**多重比较与判定**

```
F-16  Holm          族内 m 个检验的 p 值升序 p_(1) ≤ … ≤ p_(m)（同值按检验标识码点序）
                    调整 p：p̃_(i) = max_{l ≤ i} min(1, (m − l + 1) · p_(l))
                    拒绝 H_(i) 当且仅当 p̃_(i) ≤ α
F-17  判定词        A ≻_k B  当且仅当  p̃_{AB,k} ≤ α  ∧  d̂_{AB,k} > ε_k                       "显著且实际更优"
                    A ≈_k B  当且仅当  CI90_{AB,k} ⊂ [−ε_eq, ε_eq]                              "等价"
F-18  并列扫描      e_(1), …, e_(K) 按 (−σ μ̂, entry_id) 排序；h ← e_(1)；g ← 0
                    对 l = 2..K：若 h ≻ e_(l) 则 g ← g + 1，h ← e_(l)；tie_group(e_(l)) = g
                    rank(e) = 组首在序列中的位置（自 1 起）
F-19  名次区间      rank*_{e,j} = 1 + #{e' ∈ C : σ μ*_{e',j} > σ μ*_{e,j}}
                    rank_ci95 = [ x_(⌊0.025(B−1)⌋), x_(⌈0.975(B−1)⌉) ]（排序后的复本名次，取样本值）
                    per_metric 模式下 C 换为该指标的候选集合 C_k（R-M32-100）；复本中 μ*_{e',j} 缺失的 e' 不计入
F-41  双侧 p 值     选向比较：p₂ = min(1, 2 · p₁)，p₁ 取点估计较优方向（d̂ ≥ 0 的方向；d̂ = 0 时按 entry_id 码点序较小者为 A）
                    定向比较：p = p₁（方向事先规定）
                    Holm 与判定词使用 p₂ 或 p；comparisons[].sidedness ∈ {one_sided, two_sided}
```

**支配分层**

```
F-20  支配          A ⊳ B 当且仅当 ∀k ∈ T : d̂_{AB,k} ≥ −ε_k  ∧  ∃k ∈ T : p̃^{dom}_{AB,k} ≤ α ∧ d̂_{AB,k} > ε_k
F-21  分层          以 ⊳ 为有向边（A → B）构图；Tarjan 求强连通分量 Q_1..Q_r；缩点图为有向无环图
                    tier(Q) = 1 + 最长入路径长度（等价于逐层剥离：每轮移除当前无入边的分量）
F-22  参考行层级    A ⊳_ref ρ 当且仅当 ∀k ∈ T : d̂_{Aρ,k} ≥ −ε_k  ∧  ∃k ∈ T : p̃^{domref}_{Aρ,k} ≤ α ∧ d̂_{Aρ,k} > ε_k
                    p̃^{domref} 为 F_domref（定向单侧，独立 Holm）的调整 p 值
                    tier(ρ) = 1 + max{ tier(A) : A ∈ C, A ⊳_ref ρ }（无则为 1）
```

二元环不可能：若 `A ⊳ B`，存在 k 使 `d̂_{AB,k} > ε_k`，即 `d̂_{BA,k} < −ε_k`，B 在 k 上不满足"不劣于"A，故 `B ⊳ A` 不成立。三元及以上的环因容差可能出现，由 F-21 合并。

**失败与无效计分**

```
F-23  不利端        worst_k = range_lo（σ_k = +1）或 range_hi（σ_k = −1）；target 指标取使 g 最小的 range 端点
                    结果类指标（level = episode）：
                      不利端有界：y ← worst_k
                      不利端无界：y ← penalty_k（已声明）；否则 y ← null（nullable = true）；二者皆无为 V-9 错误
                    消耗类指标：y 保留实测；安全类指标：保留实测；fail_rate 由 failure.policy 计 1
F-24  完整性        valid_cell(e,s,i) 当且仅当 单元格 (s,i) 存在 e 的主评测有效行（R-M32-92 归并后未记缺失）
                                         ∧（invalid_policy = exclude 时另须 ranked_valid = true）
                    incomplete(e) 当且仅当 ∃s ∈ 声明场景集 : #{i : valid_cell(e,s,i)} < min_valid_seeds（缺省 3）
                    zero 口径下 ranked_valid = false 的 Episode 计为有效种子（已按 F-23 计分）
F-45  无效阈值      仅 invalid_policy = exclude：n_tot(e) = e 的主评测有效行数（intervention = none，排名运行，R-M32-92 归并后）
                    n_val(e) = 其中 ranked_valid = true 的行数
                    status(e) = invalid 当且仅当 n_val(e) < (1 − θ) · n_tot(e)，以有理数精确比较（θ = 0.2 时为 5 · n_val < 4 · n_tot）
                    与 D3 `len(valid) < 0.8 * len(rs)` 逐例等价（D3-INV-12）
```

**Δ 族**

```
F-25  Δ_X           D^X(s) = (1/|I^X_s|) Σ_{i∈I^X_s} [ Ū(π; s,i) − Ū(π^X; s,i) ]，I^X_s：两者均有的单元格
                    Δ̂_X = (1/S_X) Σ_s D^X(s)，S_X 为 I^X_s 非空的场景数；区间按 F-12（family）或 F-13（fixed_set，以种子为单位重采样）
F-26  R_central     R(s) = Ū(π_central; s) − Ū(π; s)；R̂ = (1/S) Σ_s R(s)
F-27  G             a(s) = Ū(π; s) − Ū(π_ind; s)，b(s) = Ū(π_central; s) − Ū(π_ind; s)；Ĝ = â / b̂
                    family（Fieller）：v_aa、v_bb、v_ab 为场景样本方差与协方差除以 S；z = Z_975（ND-10 常量）
                      解 (b̂² − z² v_bb) G² − 2(â b̂ − z² v_ab) G + (â² − z² v_aa) ≤ 0：
                      若 b̂² − z² v_bb > 0 且判别式 ≥ 0，区间为两根之间；否则区间无界，记 n/a（ratio_unbounded）
                      分母区间：b 的 bootstrap-t 95% 区间
                    fixed_set：G*_j = â*_j / b̂*_j（以 F-11 计算复本），b̂*_j ≤ 0 的复本占比 > 2.5% 时记 n/a；
                      区间为 G*_j 的 percentile；分母区间为 b̂*_j 的 percentile
                    两种口径均另须分母 95% 区间下界 ≥ 0.05，否则记 n/a（denominator_small）
F-28  可用对比例    P = {(s,i,r) : 主评测 Episode 与 π^X Episode 均存在（均未记缺失）}
                    ρ_use = #{(s,i,r) ∈ P : π^X Episode 无资源违规} / |P|
F-29  回退率        r_fb = #{置换投递中以 L_perm 之前同类消息替代者或因供体缺失回退者} / #{置换投递}（exempt 通道不计）
F-30  η_comm        b̄ = (1/S) Σ_s mean_i b_tx_ratio(π; s,i)
                    η_comm = max(0, Δ_perm) / ln_d(1 + max(b̄, 0.05) / 0.05)；b̄ = 0 时 n/a
```

**X1 与研究分析**

```
F-31  S_rob         Ū_q = 场景等权均值（X1 场景子集，强度档 q）；d_0..d_4 = 0, 0.25, 0.5, 0.75, 1
                    S_rob = Σ_{m=0}^{3} (d_{m+1} − d_m) · (Ū_{q=m} + Ū_{q=m+1}) / 2 / (d_4 − d_0)
                    L_deg = Ū_0 − Ū_4
F-43  X1 时间量     对族 f、强度档 q：p_det = (1/S) Σ_s [#{有响应对象 in s} / #{受扰对象 in s}]（受扰对象为 0 的场景不计）
                    T̃_detect = 加权中位数{ T_detect(o) : o 未删失 }，对象权重 w_o = 1 / (S · #{未删失对象 in s(o)})
                    加权中位数：按值升序、同值按 (scenario_id, seed_index, object_id) 码点序，取累计权重首次 ≥ 0.5 的值
                    删失比例 = 1 − p_det（逐场景等权）；p_rec、T̃_recover 同式，以 Episode 为单位
                    复本：以 F-09 的种子计数为对象权重的乘子重算，取 percentile 区间
F-32  B*_θ          Ũ(·) = 对 (B_l, Ū(B_l)) 的保序回归（相邻违序合并，PAVA），B_∞ 取解除预算的档
                    B*_θ = min{ B_l : Ũ(B_l) ≥ θ Ũ(B_∞) + (1 − θ) Ũ(0) }；每个复本内重做保序回归与取最小，percentile 区间
                    跨预算档（不同赛道）的复本对齐：各档须使用相同的场景集与种子序号集，复本下标由 F-07 跨赛道域派生，
                    同一 j 对全部档使用同一下标；任一档缺失某场景时该场景从全部档的计算中剔除
                    仅当 U(B_∞) − U(0) 的 95% 区间下界 > 0.05 时报告
F-33  饱和规模      m(N) = Δ_perm(2N) − Δ_perm(N)；N* = 首个 m(N) 的 90% 区间 ⊂ [−ε_eq, ε_eq] 的 N
                    负协作区域 = { 格点 : Δ_ind 的 95% 区间上界 < 0 }
```

**资源账本**

```
F-34  比值          tx_a = det[a].tx_bytes；cpu_a = measured[a].cpu_ms；tok_a = det[a].tokens_in + det[a].tokens_out
                    b_tx_ratio = Σ_a tx_a / limit(tx_bytes, team)
                    cpu_ratio  = Σ_a cpu_a / Σ_a limit(cpu_ms, a)；cpu_ratio_max = max_a cpu_a / limit(cpu_ms, a)
                    token_ratio = Σ_a tok_a / limit(tokens, team)（limit = 0 时按 R-M32-59）
                    enforce = violation：violation(dim) 当且仅当 usage_scope(dim) > limit_scope(dim)，scope ∈ {agent, team}
                    enforce = refuse：   不产生违规；usage_scope(dim) > limit_scope(dim) 则 ledger_mismatch
                    额度比较以整数进行（字节、μWh、token 计数为整数；cpu_ms 比较前不舍入）
```

**跨保真度**

```
F-35  排名一致      ρ = Spearman（平均秩）；τ_b = (n_c − n_d) / sqrt((n_0 − n_1)(n_0 − n_2))
                    Top-k 重合 = |Top_k(low) ∩ Top_k(high)| / k，k = max(3, ⌈0.25 K⌉)
F-36  FC-8 度量     PDR 曲线相对 L1 距离 = Σ_l |PDR_a(d_l) − PDR_b(d_l)| / Σ_l PDR_b(d_l)（共同距离网格）
                    P95 时延相对差 = |P95_a − P95_b| / P95_b；时延分布另报 W1(F_a, F_b) / mean(F_b)
```

**pilot 与样本量**

```
F-37  ICC 与样本量  对配对差 D(s,i)：MSB、MSW 为单因素方差分析的组间与组内均方，m 为每场景种子数
                    ICC = clip( (MSB − MSW) / (MSB + (m − 1) MSW), 0, 1 )
                    σ̂_D = sd 全部 D(s,i)
                    上界：以 pilot 域（F-07）派生的场景层下标重采样 B_p = 10,000 次，每个复本重算 ICC*_j 与 σ̂*_D,j，
                          ICC_U、σ_D,U 取各自复本的 80% 分位（F-13 分位规则）；不使用 χ² 上界
                    n_eff* = ((Z_99 + Z_80) · σ_D,U / δ)²，α′ = 0.01，δ = 0.05
                    S = max(12, ⌈ n_eff* · (1 + (m − 1) · ICC_U) / m ⌉)；fixed_set 时设计效应取 1
                    功效 ≈ Φ(δ / SE − z_{1−α′})，SE = σ_D / sqrt(S m / (1 + (m − 1) ICC))
                    等价可达 ≈ 2Φ(ε_eq / SE − Z_95) − 1
F-38  交叉自助      μ*_j = Σ_s Σ_t C^S_{j,s} C^T_{j,t} ȳ(s,t) / (Σ_s C^S_{j,s} · Σ_t C^T_{j,t})
                    C^T 为训练产物下标计数，以独立域 "drone4d.board.resample.train.v1" 派生
F-39  复跑判据      Δ_r = μ̂_rerun − μ̂；ŝ_o 为原估计标准误（family：F-06 的 ŝ 型单条目标准误；fixed_set：F-42 型闭式标准误）
                    ŝ_r = σ̂_ep / sqrt(n_r)，σ̂_ep 为原条目全部主评测 Episode 值的样本标准差（分母 N − 1）
                    R-1/R-2b 通过 当且仅当 |Δ_r| ≤ Z_995 · sqrt(ŝ_o² + ŝ_r²) + ε        （α_r = 0.01，ε = 0.02）
F-40  承诺验证      seed_set_commit = hex(SHA-256(salt ‖ JCS([dec(ω) for ω in sorted_numeric(seeds)])))；
                    seed_tag = hex(HMAC-SHA256(salt, LP("drone4d.board.seed_tag.v1") ‖ LP(episode_id) ‖ LP(scenario_id) ‖ u32be(seed_index) ‖ u64be(ω)))
                    （编码与测试向量以 CT §3.4.3 为唯一定义，XR-07）
                    ‖ 的字节编码同 F-07（待 CT 确认）
F-42  闭式方差      fixed_set 下配对差均值的分层自助方差（B → ∞ 的精确值）：
                    D(s,i) 为单元格配对差，I_s 为双方均有值的种子，m'_s = |I_s|，D̄(s) 为其均值
                    v_s = (1/m'_s) Σ_{i∈I_s} (D(s,i) − D̄(s))²（插件方差）
                    ŝ_N² = (1/|S_AB|²) Σ_{s∈S_AB} v_s / m'_s
                    缺失种子使 m'_s < m_s 时为近似值（stats 注明）；ŝ_N = 0 时按 §6.4.4 退化规则
F-44  重放容差      consistent_k(a, b) 当且仅当 (a = b = null) ∨ (a, b 非空 ∧ |a − b| ≤ τ_abs,k + τ_rel · max(|a|, |b|))
                    τ_abs,k = replay_tol_k（若声明）否则 10^(−precision_k)；τ_rel = 1e-9
```

以占位值 `σ_D = 0.10`、`ICC_U = 0.3`、m = 5 代入 F-37：`n_eff* = ((2.326 + 0.842) × 0.10 / 0.05)² ≈ 40.1`，`S = ⌈40.1 × 2.2 / 5⌉ = 18`；S = 18 时 `SE ≈ 0.0156`，α′ = 0.05 的功效约 0.94，等价可达约 0.88，与 AWR-06 §8.6 功效表一致（AC-M32-29 以此为回归值）。

以评审意见所设的 Episode 层标准差 0.25、原估计标准误 0.039 代入 v0.1 的判据（复跑均值落入原 95% 区间）：复跑 10 个 Episode 的均值标准误约 0.079，差值标准差约 `sqrt(0.039² + 0.079²) ≈ 0.088`，原区间半宽约 0.077，诚实条目的失败概率约 `2(1 − Φ(0.077/0.088)) ≈ 0.38`；按 F-39，阈值约 `2.576 × 0.088 + 0.02 ≈ 0.247`，诚实条目在正态近似下的失败概率约 0.5%，偏差 0.3 的检出率约 0.73。正式数值由 MC-12 给出。

#### 6.3.1 `n_boot` 与 F_dom 方法对照（|T| = 3，α = 0.05，`n_boot_cap = 200,000`）

| K | `head`：m_rank / n_boot | `all_pairs`：m_rank / n_boot | m_dom | F_dom 所需 ⌈20·m_dom/α⌉ | F_dom 方法 |
|---|---|---|---|---|---|
| 2 | 3 / 10,000 | 3 / 10,000 | 3 | 1,200 | 复本 p 值 |
| 3 | 5 / 10,000 | 6 / 10,000 | 9 | 3,600 | 复本 p 值 |
| 4 | 7 / 10,000 | 10 / 10,000 | 18 | 7,200 | 复本 p 值 |
| 5 | 9 / 10,000 | 15 / 10,000 | 30 | 12,000 | 解析路径 |
| 6 | 11 / 10,000 | 21 / 10,000 | 45 | 18,000 | 解析路径 |
| 10 | 19 / 10,000 | 55 / 22,000 | 135 | 54,000 | 解析路径 |
| 20 | 39 / 15,600 | 210 / 84,000 | 570 | 228,000 | 解析路径 |
| 30 | 59 / 23,600 | 465 / 186,000 | 1,305 | 522,000 | 解析路径 |
| 100 | 199 / 79,600 | 5,050 / 200,000 | 14,850 | 5,940,000 | 解析路径 |

结论：在 3 项分层指标的典型配置下，K ≥ 5 时 F_dom 的缺省路径即为解析路径，而不是 v0.1 摘要与反馈第 8 条所称的"K > 18 才超限"（该阈值是"若 `n_boot` 按 F_dom 决定则超过 200,000 上限"的条件，与本文实际规则不符，已更正）。后果是 F_dom 的族错误率依赖解析参照分布在 S = 12 至 18 下的准确性：`family` 下以 Student t（ν = n − 1）代替正态参照，以抵消观测标准误在小 S 下的偏乐观；其族错误率由 MC-11 在全零与部分零假设下实测，判据不满足时按 Q-M32-09 处置。

#### 6.3.2 比较方向与名义水平

候选条目之间的比较方向由点估计决定（并列扫描中组首恒为点估计较高者，F_dom 每对只检验点估计较优方向）。若对此类比较使用单侧 p 值，真零假设下"任一方向被判显著"的概率约为 2α，名义水平翻倍。本文规定选向比较使用双侧 p 值 F-41，定向比较（Rf、F_domref、过敏反应审计）保留单侧 p 值。该修订不在评审意见中，作为评审外补充修订 S-1 登记于 §14，并在反馈第 21 条提请 AWR-06 §8.6 明确。`n_boot` 规则 F-15 不变；双侧检验下 Holm 最严阈值处的期望超越复本数由 20 降为 10，MC-2 与 MC-11 一并报告该分辨率下的族错误率。

### 6.4 算法

#### 6.4.1 分析组构建 `build_board_v2`

| 步骤 | 内容 | 失败处置 |
|---|---|---|
| 1 | 映射 Suite 清单（§6.2.3），取分析组视图并执行 V-1 至 V-12 | `metric_spec_invalid`、`scoring_field_unknown` |
| 2 | 行层校验（R-M32-04、R-M32-92 (a)）；按分析组拆分；校验同组全部行的 `kernel_version`、`network_profile`、`clock_profile` 与 Suite 摘要一致 | 拒绝该行；`group_inconsistent` 拒绝构建 |
| 3 | 行集归并：按行键取有效 `attempt`（R-M32-92 (b)） | `duplicate_cell` 拒绝构建 |
| 4 | `LedgerCheck`：重算比值、违规与 `enforce` 核对，比对 `ranked_valid`/`invalid_reasons` 期望值，应用 `budget.policy` 映射 | `ledger_mismatch`、`ledger_unmetered` 按基础设施故障处置 |
| 5 | `apply_scoring`：失败与无效方向化计分（F-23）；`exclude` 下剔除无效 Episode 并按 F-45 判定条目状态 | — |
| 6 | `build_cube`；F-01 至 F-03；完整性（F-24）；确定候选条目集合 C 与参考行 | — |
| 7 | 派生下标种子（F-07）；场景与种子索引（R-M32-93）；按 F-15 确定 `n_boot`；生成下标与计数矩阵（F-08、F-09） | — |
| 8 | 复本均值（F-10/F-11），按指标分块计算；伴随统计量（R-M32-90） | — |
| 9 | F_rank：按 `rank_family` 求族（§6.4.2），计算 p 值（F-14、F-41）与 Holm（F-16），并列与名次（F-18） | 未收敛时 Bonferroni 退回并登记 |
| 10 | 名次区间（F-19） | — |
| 11 | F_dom 与 F_domref：方向化差值与 p 值（复本或解析路径 F-14c、F-42），分别 Holm；支配图、强连通分量与分层（F-20 至 F-22） | — |
| 12 | Δ 族与 X1（若行中含干预或扰动）：执行记录校验（`check_exec_record`），F-25 至 F-31、F-43，F_rob，`n/a` 规则 | — |
| 13 | 账本摘要（R-M32-60）、失败与无效占比（R-M32-40） | — |
| 14 | 组装 `entries[]`、`comparisons[]` 与 `stats`；全部浮点输出 `round(x, 6)`；返回 Python 对象，由调用方以 JCS 序列化 | — |

#### 6.4.2 F_rank 的确定性定点算法（`rank_family = head`）

AWR-06 规定 F_rank 由"每个条目与其组首的比较"组成，而组首由前序比较的判定结果决定；Holm 逐步校正需要一次性给定全部 p 值，因此族成员与判定之间存在循环依赖。本文以如下可终止的确定性算法实现，并在 `stats.rank_family_converged` 中记录是否收敛。

```
输入：候选条目按 (−σ μ̂, entry_id) 排序为 e_(1..K)；Rf = {(e, π_ind) : e ∈ C}（定向："e 优于 π_ind"，单侧）
p(a, b)：按需计算并缓存的 p 值；候选对为选向比较，取 F-41 双侧 p₂
F ← {(e_(1), e_(l)) : l = 2..K} ∪ Rf                         # 初始族：全部与榜首比较
for round = 1..K:
    p̃ ← Holm(F)
    扫描（F-18），判定 h ≻ e_(l) 时使用 p̃(h, e_(l))；
        若 (h, e_(l)) ∉ F，则以 Holm(F ∪ {(h, e_(l))}) 中该比较的调整 p 判定（只用于本轮扫描）
    F' ← {扫描中实际执行的 (h, e_(l))} ∪ Rf
    if F' = F: 收敛，输出扫描结果；break
    F ← F'
未收敛：以 Bonferroni 阈值 α / (2K − 1) 对原始 p 执行一次扫描，rank_family_converged = false
```

族规模恒为 `(K − 1) + |Rf|`，与 AWR-06 的上限一致。该算法不能严格保证族错误率，其实际族错误率由 MC-2 在全零假设与部分零假设下实测；`rank_family = all_pairs` 以全部无序候选对（双侧）加 Rf 为固定族（规模 `K(K − 1)/2 + |Rf|`），关系 ≻ 在任何数据上无环且与扫描顺序无关，族错误率由 Holm 严格控制，代价是 `n_boot` 更大与功效下降。两种方案的取舍见 Q-M32-01 与反馈第 1 条。

#### 6.4.3 强连通分量与逐层剥离

- Tarjan 算法以显式栈迭代实现，节点按 `entry_id` 码点序访问、邻接表按目标 `entry_id` 排序，保证分量编号确定。
- 缩点图的分层以 Kahn 拓扑排序计算：每轮取入度为 0 的分量为当前层，分量内按 `entry_id` 排序输出。
- `uncertain_group` 标识为分量内最小 `entry_id` 的 SHA-256 前 8 个十六进制字符，保证跨重算稳定。

#### 6.4.4 退化情形

| 情形 | 处置 |
|---|---|
| 观测 `ŝ = 0`（或 `ŝ_N = 0`）且 `d̂ = 0` | p = 1；区间 `[0, 0]`；`method_note = degenerate_zero` |
| 观测 `ŝ = 0`（或 `ŝ_N = 0`）且 `d̂ ≠ 0`（全部场景差相同） | 区间 `[d̂, d̂]`；单侧 p 取 `1/(B + 1)`（d̂ > 0）或 1（d̂ < 0），双侧按 F-41；`method_note = degenerate_constant` |
| 复本 `ŝ*_j = 0` | `t*_j = 0`（`d*_j = d̂`）或 `±∞` 按 `d*_j − d̂` 的符号；参与排序与计数 |
| 复本有效场景数为 0 或 `N_j < 2`（可空指标、缺失场景） | 该复本记缺失；有效复本数 < 0.9B 时该量不给区间并标注 `insufficient_replicates` |
| n < 2（共同非空场景不足） | 不给区间与 p 值，判定为"未判定"，标注 `insufficient_scenarios`；解析路径同此 |
| K = 1 | 无 F_rank 候选间比较；只做与 `π_ind` 的比较；`tie_group = 0`、`rank = 1`、`rank_ci95 = [1, 1]` |

#### 6.4.5 判定词的互斥性

"显著且实际更优"要求 `d̂ > ε = 0.02`；"等价"要求 90% 区间位于 `[−0.05, 0.05]`。二者可在 `0.02 < d̂ < 0.05` 时同时成立。本文规定此时输出"显著且实际更优"，并在 `comparisons[]` 中另置 `equivalent = true`，供条目卡说明"差异显著但小于等价边界"。

### 6.5 数值确定性规则

| 编号 | 规则 |
|---|---|
| ND-1 | 复本均值与加权和以"按场景序号顺序的逐元素乘加"累积（`acc = acc + C[:, s] * y[s]`，s 升序，乘与加为两次独立的 ufunc 调用），不使用 BLAS 矩阵乘与 `np.sum` 的多轴归约；逐元素 IEEE 运算在任何 SIMD 派发下结果相同 |
| ND-2 | 一维向量的求和（如场景均值）使用 `numerics.ordered_sum`（顺序累加），不使用 numpy 成对求和 |
| ND-3 | 下标生成只用 uint64 整数运算（F-08），不依赖 numpy `Generator` |
| ND-4 | 分位数以排序加显式线性插值实现；排序为稳定排序（`kind = "stable"`） |
| ND-5 | 判定路径只使用 IEEE 正确舍入的运算（加、减、乘、除、开方）与 ND-9 的确定性函数；超越函数出现的全部位置为：F-14(c) 与 F-42 的 Φ 与 T_ν、F-27 的 z 常量、F-30 的 ln、F-37 的 z 常量与 Φ、F-39 的 z 常量、`G` 的 Fieller 判别式（只用开方）；不调用 `math.log`、`math.exp`、`math.erf`、`statistics.NormalDist`（F19） |
| ND-6 | 判定以未舍入值进行；输出前统一 `round(x, 6)`；JCS 序列化 |
| ND-7 | 迭代顺序全部由排序后的标识决定（码点序），不依赖字典插入顺序或集合迭代顺序；行序与分块方式不影响结果（R-M32-92 (c)） |
| ND-8 | 由 ND-5 与 ND-9，判定路径与平台 libm 无关；`verify_board` 在同硬件等级与跨硬件等级复核时均要求逐字节一致，不设"边界差异"豁免（v0.1 的 ND-8 豁免废止） |
| ND-9 | `numerics.dmath` 算法规格（§6.5.1）：只用基本运算、`sqrt`、`frexp`、`ldexp` 与比较；固定迭代次数或固定项数；禁止融合乘加（C/C++ 实现须 `-ffp-contract=off`，Python 实现天然满足）；`dmath_version` 写入 `stats` |
| ND-10 | 常用分位数为十六进制浮点字面常量：`Z_80 = 0x1.aee8fa73a1335p-1`（0.8416212335729144）、`Z_90 = 0x1.4813c36e26d34p+0`（1.2815515655446008）、`Z_95 = 0x1.a515209676ab8p+0`（1.6448536269514715）、`Z_975 = 0x1.f5c0331eeff82p+0`（1.9599639845400536）、`Z_99 = 0x1.29c5c4630ff0ep+1`（2.3263478740408408）、`Z_995 = 0x1.49b4c64d6915fp+1`（2.5758293035489）；常量值在 CPython 3.12.3 上以 `NormalDist().inv_cdf` 取得并以 `float.hex` 冻结，此后以字面量为准，不在运行时重算 |
| ND-11 | numpy 接口限定：D4 路径只使用 numpy 1.26 起即存在且语义未变的接口（R-M32-103）；不使用依赖 numpy 版本的缺省参数（如 `np.percentile` 的缺省插值方法、`np.sort` 的缺省算法） |

#### 6.5.1 `dmath` 算法规格

| 函数 | 规格 |
|---|---|
| `ln_d(x)` | x > 0；`(f, e) = frexp(x)`，若 `f < √½`（常量 `0x1.6a09e667f3bcdp-1`）则 `f ← 2f, e ← e − 1`；`u = (f − 1)/(f + 1)`，`w = u²`；`P = Σ_{k=0}^{13} w^k/(2k+1)`，以 Horner 法自 k = 13 起计算；结果 `= 2uP + e · LN2`，`LN2 = 0x1.62e42fefa39efp-1` |
| `exp_d(x)` | `|x| ≤ 708`；`n = round_half_even(x / LN2)`，`r = (x − n · LN2_HI) − n · LN2_LO`（Cody–Waite 两段常量：`LN2_HI = 0x1.62e42feep-1`，`LN2_LO = 0x1.a39ef35793c76p-33`）；`p = Σ_{k=0}^{17} r^k/k!` 以 Horner 法计算；结果 `ldexp(p, n)` |
| `atan_d(x)` | 奇对称；`|x| > 1` 时用 `π/2 − atan(1/|x|)`；再以 `atan(y) = 2 atan(y / (1 + sqrt(1 + y²)))` 约化两次后取 24 项 Taylor 级数（Horner 法）；`π/2 = 0x1.921fb54442d18p+0` |
| `phi_d(z)` | `Φ(z) = ½ erfc(−z/√2)`，记 `x = |z| · INV_SQRT2`（`INV_SQRT2 = 0x1.6a09e667f3bcdp-1`），z < 0 与 z ≥ 0 分别按 `erfc(x)` 与 `2 − erfc(x)` 组合；`x < 0.5` 用 erf 的 Taylor 级数 40 项；`x ≥ 0.5` 用 erfc 的连分式（Lentz 法，固定 200 次迭代，不提前终止）乘 `exp_d(−x²)`；`|z| > 38` 时饱和为 0 或 1 |
| `phi_inv_d(p)` | Wichura AS241 有理逼近（与 CPython `statistics` 的系数相同），其中 `log` 与 `sqrt` 分别以 `ln_d` 与 `sqrt` 计算；只用于非常量分位数（如 pilot 报告），判定路径使用 ND-10 常量 |
| `t_cdf_d(t, ν)` | ν 为正整数；`θ = atan_d(t / sqrt(ν))`；ν 为奇数：`A = (2/π)(θ + sin θ cos θ · Σ_{k=0}^{(ν−3)/2} a_k cos^{2k} θ)`，ν = 1 时 `A = 2θ/π`；ν 为偶数：`A = sin θ · Σ_{k=0}^{(ν−2)/2} b_k cos^{2k} θ`；其中 `a_0 = b_0 = 1`，`a_k = a_{k−1} · 2k/(2k+1)`，`b_k = b_{k−1} · (2k−1)/(2k)`；`sin θ = t/sqrt(ν + t²)`，`cos θ = sqrt(ν)/sqrt(ν + t²)`（代数式，不调用三角函数）；`T_ν(t) = ½(1 + A)`（整数自由度 Student t 分布函数的教科书有限级数） |

黄金向量（AC-M32-37）以离线高精度计算的参考值评估误差上界（目标：`ln_d`、`exp_d` 相对误差 ≤ 4 ulp；`phi_d` 在 `[−8, 8]` 上绝对误差 ≤ 1e-15；`t_cdf_d` 在 ν ≤ 200、`|t| ≤ 50` 上绝对误差 ≤ 1e-14）；逐位一致性以规格实现之间的比对为准，与参考值的误差只作精度说明。

### 6.6 干预对照编排时序

```
第一阶段（全部提交，P0）
  VALIDATING：冒烟集 2 场景 × 3 公开种子（none）
  EVALUATING：场景集 × m 个秘密种子 × R 重复 × {none, cut}
              cut：Episode 规格携带 CutSpec；M29 DeliveryInterceptor 阻断全部投递（含 _ix 控制消息）
  参考缓存：ind、central 按 M31 R-M31-45 缓存键读取；缺失键 → 平台参考运行请求
  SCORING：build_board_v2（Δ_ind、Δ_cut、R_central、G、η_comm[依赖度口径]）
第二阶段（候选与各赛道前 k 名，P1）
  选择：第一阶段 SCORING 后按主排名取前 k 名（k = 5）与候选标记条目
  EVALUATING：场景集 × m × R × {perm_struct, perm_full}
              Episode 规格携带 PermSpec 与供体 Episode 标识（donor_cell：同条目、同场景、同重复，σ(i) = (i+1) mod m_s）
              M33 MessageIndex.build(供体 trace) → M29 DeliveryInterceptor 逐投递调用 nearest(key, t_rel) 取替换载荷
              m_s < 2 的场景不展开 perm（R-M32-97 (a)）；_ix 控制消息照常投递（R-M32-97 (b)）
              X1：6 场景 × 3 种子 × 5 强度 × 扰动族（q = 0 共享）
  SCORING：重算分析组（Δ_perm、r_fb、过敏反应审计、S_rob、L_deg、p_det、T̃_detect、p_rec、T̃_recover、F_rob）
```

M32 对第二阶段的选择只依据第一阶段已发布的点估计，不依据第二阶段的任何结果；选择清单写入 `stats.stage2_selection` 以便复核。M29 不 import `awr.evaluation`：`CutSpec`/`PermSpec` 以 JCS 序列化后随 Episode 规格下发，M29 按字段执行；M32 在 SCORING 时以 `check_exec_record` 核对执行记录（计数非负、置换数加回退数等于非豁免投递数、`cut` 下非零阻断等）。

### 6.7 缺省参数

| 参数 | 缺省 | 来源 |
|---|---|---|
| α | 0.05 | §8.6 |
| ε（主指标） | 0.02 | §8.6 |
| `ε_r`（资源比） | 0.05 | §8.6 |
| `ε_eq` | 0.05 | §8.6 |
| `ε_f`（FC-4） | 0.05 | §5.8 |
| `n_boot` 下限 / 上限 | 10,000 / 200,000 | §8.6 |
| `min_valid_seeds` | 3（X1 视图 2） | §8.6；X1 R-RC-54 |
| 无效阈值 θ | `zero`：无阈值（不剔除）；`exclude`：Suite 必填，`drone4d-event@1` 为 0.2 | ADR-D4-24；§8.12"无效条目门"；F-45 |
| 第二阶段 k | 5 | §8.5 |
| `L_perm` | 30 s | §8.5 |
| `r_fb` 上限 | 0.2 | §8.5 |
| `ρ_use` 下限 | 0.8 | §8.5 |
| `G` 分母下界 | 0.05 | §8.5 |
| X1 不确定半宽 | 0.1 | §7.4 |
| FC-5 阈值 | ρ ≥ 0.80，调查 ρ < 0.65；Top-k ≥ 2/3 | §5.8 |
| FC-6 阈值 | ≥ 90%；F_fid 显著对 < 5 时 `not_run` | §5.8；本文 R-M32-64 |
| 等价置信水平 | 90% | §8.6 |
| 复跑判据 α_r | 0.01（`Z_995`） | 本文 F-39 |
| 重放容差 | `τ_abs = 10^(−precision)`，`τ_rel = 1e-9` | 本文 F-44 |
| pilot 上界分位 | 80%（MC-13 覆盖不足时改 90%） | §8.6；本文 F-37 |

---

## 7. 接口

### 7.1 Python API（草案）

```python
# py:evaluation/manifest.py
def from_suite_manifest(manifest: Mapping[str, object]) -> tuple[MetricSet, Scoring, Mapping[str, Mapping[str, str]]]: ...
# 第三项为 scenario_set → 覆写（v0.2 只含 estimand）

# py:evaluation/rows.py
def normalize_rows(rows: Iterable[Mapping[str, object]], *, row_schema: str,
                   metrics: MetricSet) -> tuple[list[Row], list["RowRejection"]]: ...   # R-M32-92

# py:evaluation/board.py
def build_board_v2(rows: Sequence[Row], *, group: GroupKey, metrics: MetricSet, scoring: Scoring,
                   suite_meta: Mapping[str, object], scenario_set_ids: Sequence[str],
                   reference_cache: Mapping[str, Sequence[Row]] | None = None) -> dict: ...
# metrics 与 scoring 为分析组视图；scenario_set_ids 为声明场景集全集（R-M32-93）
# 返回 board.v2 中该分析组的 {entries[], comparisons[], stats}；顶层冻结项由 L6 填写

# py:evaluation/bootstrap.py
def paired_ci(a: "ScenarioValues", b: "ScenarioValues", *, counts: np.ndarray, method: str,
              sidedness: str, level: float = 0.95) -> "PairResult": ...    # d̂, ŝ, ci, p, B_eff, method_note
def analytic_se(a: "CellValues", b: "CellValues", *, estimand: str) -> float: ...   # F-06 ŝ 或 F-42 ŝ_N
def ci_mean(x: "ScenarioValues", *, counts: np.ndarray, method: str, level: float = 0.95,
            clip: tuple[float | None, float | None] | None = None) -> "CIResult": ...

# py:evaluation/resample.py
def resample_seed(domain: str, *parts: str) -> int: ...                  # F-07
def scenario_order(scenario_ids: Sequence[str]) -> tuple[str, ...]: ...  # R-M32-93：码点序
def scenario_indices(seed64: int, n_boot: int, S: int) -> np.ndarray: ... # F-08，形状 (n_boot, S)，uint32
def seed_indices(seed64: int, n_boot: int, m: Sequence[int]) -> list[np.ndarray]: ...
def counts(idx: np.ndarray, S: int) -> np.ndarray: ...                    # F-09

# py:evaluation/numerics/dmath.py
def ln_d(x: float) -> float: ...; def exp_d(x: float) -> float: ...; def atan_d(x: float) -> float: ...
def phi_d(z: float) -> float: ...; def phi_inv_d(p: float) -> float: ...; def t_cdf_d(t: float, nu: int) -> float: ...
Z_80: float; Z_90: float; Z_95: float; Z_975: float; Z_99: float; Z_995: float   # ND-10 字面常量

# py:evaluation/holm.py
def holm(pvalues: Mapping[str, float], alpha: float = 0.05) -> Mapping[str, tuple[float, bool]]: ...

# py:evaluation/ranking.py
def tie_groups(order: Sequence[str], better: Callable[[str, str], bool]) -> Mapping[str, tuple[int, int]]: ...
def rank_intervals(rep_means: np.ndarray, ids: Sequence[str], sigma: int) -> Mapping[str, tuple[int, int]]: ...

# py:evaluation/dominance.py
def ref_dominance(cands: Sequence[str], refs: Sequence[str], tests: "PairTable", alpha: float) -> Mapping[str, Sequence[str]]: ...
def dominance_tiers(cands: Sequence[str], edges: Iterable[tuple[str, str]],
                    refs: Mapping[str, Iterable[str]]) -> tuple[Mapping[str, int], Mapping[str, str]]: ...

# py:evaluation/intervention.py
def donor_cell(scenario_seeds: Sequence[int], seed_index: int) -> int | None: ...
def check_exec_record(spec: CutSpec | PermSpec, rec: ExecRecord) -> tuple[bool, str]: ...
def delta(main: "Cube", ctrl: "Cube", *, metric: str, counts: np.ndarray, method: str) -> "DeltaResult": ...
def fieller(a: np.ndarray, b: np.ndarray, *, level: float = 0.95, denom_floor: float = 0.05) -> "RatioResult": ...

# py:evaluation/robust.py
def censored_median(values: np.ndarray, censored: np.ndarray, weights: np.ndarray) -> float | None: ...   # F-43
def f_rob(entry: str, comparators: Sequence[str], families: Sequence[str], reps: "RobustReplicates",
          alpha: float = 0.05) -> list[dict]: ...                       # R-M32-98

# py:evaluation/ledger.py
class LedgerCheck:
    def __init__(self, budget_dims: Sequence[BudgetDim], policy: str, ranked: bool, shadow: bool) -> None: ...
    def check(self, summary: LedgerSummary, ranked_valid: bool,
              invalid_reasons: Sequence[str]) -> "LedgerVerdict": ...    # ratios, violations, expected, mismatch, why

# py:evaluation/fidelity.py
def fidelity_audit(low: Sequence[Row], high: Sequence[Row], *, axis: str, fc_inputs: Mapping[str, object],
                   metrics: MetricSet, scoring: Scoring) -> dict: ...     # fidelity_audit.v1

# py:evaluation/verify.py
def verify_board(rows_bytes: bytes, config: Mapping[str, object], board_bytes: bytes) -> "VerifyReport": ...
def verify_commitments(salt: bytes, seeds: Mapping[str, Sequence[int]], rows: Sequence[Row],
                       commits: Mapping[str, str]) -> "CommitReport": ...
def rerun_check(original_mean: float, original_se: float, episode_sd: float, rerun_mean: float,
                n_rerun: int, *, epsilon: float = 0.02, alpha_r: float = 0.01) -> "RerunVerdict": ...   # F-39
def audit_baselines(expected: Sequence[Row], observed: Sequence[Row]) -> "AuditReport": ...
def replay_tolerance(metrics: MetricSet) -> Callable[[str, float | None, float | None], bool]: ...       # F-44

# py:evaluation/report.py
def analyze_experiment(rows: Sequence[Row], *, metrics: MetricSet, scoring: Scoring,
                       reference: str | None, experiment_digest: str) -> dict: ...

# py:evaluation/pilot.py
def pilot_power(d: np.ndarray, scenario_ids: np.ndarray, *, upper_q: float = 0.8,
                pilot_digest: str) -> "PilotEstimate": ...
def required_scenarios(est: "PilotEstimate", *, m: int, delta: float = 0.05, alpha_prime: float = 0.01,
                       power: float = 0.8, estimand: str = "family") -> int: ...
```

### 7.2 输入：`rows.jsonl` 行的统计所需字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `row_schema` | string | 行格式版本；与分析组声明不符时 `schema_mismatch` |
| `entry_id`、`team`、`track`、`scenario_set` | string | 分析组与条目标识；不得含 `‖` |
| `scenario_id`、`seed_index`、`repeat` | string、int、int | 单元格与重复；`seed_index` 为承诺种子集中的序号 |
| `intervention` | enum | `none`、`cut`、`perm_full`、`perm_struct`、`perm_class:<c>`、`ind`、`central` |
| `perturbation` | object \| null | X1：`{family, q}` |
| `attempt` | int | 自 0 起；平台原因重跑每次加 1（R-M32-92）。D3 `rows.jsonl` 无此字段，兼容路径按 0 处理 |
| `reference_kind`、`legacy_baseline` | enum \| null、bool | 参考行类别；D3 基线标记 |
| `evaluation` | object | `ct:suite/evaluation.v1`：`components`、`ranked`、`ranked_valid`、`invalid_reasons`、`failure{policy, infra, code}`；X1 宿主另含 `x1_times{detect[{object_id, t, censored}], recover{t, censored, floor}}` |
| `ledger_summary` | object | M28 运行清单同名对象：`det`、`team`、`measured`、`violations`；待 M28 增补 `budgets`、`cpu_source` |
| `exec_record` | object \| null | M29 干预执行记录摘要：`blocked`、`permuted`、`fallback`、`fallback_blocked`、`exempt_delivered`、`donor_episode_id` |
| `track_id`、`kernel_version`、`attrs_digest`、`resource_digest`、`network_digest` | string | 资源对齐校验（R-M32-46） |
| `training_artifact` | string \| null | P1 交叉自助 |
| `episode_id`、`seed_tag`、`truth_digest` | string | 承诺验证与复跑（M32 不解释其含义，只按 F-40 验证） |

### 7.3 输出：`board.v2` 中由 M32 生成的字段

CT 建立前，本表是 `board.v2` 统计字段的单一来源；LB §13 的治理字段在本表之上增补。

| 字段 | 内容 | 状态 |
|---|---|---|
| `entries[].metrics.<id>` | `value`、`ci95`、`ci_method`、`ci_clipped`、`n`、`rank`、`rank_ci95`、`tie_group`、`repeat_delta`、`companions{iqm{value, ci95}, q10{value, ci95}}` | 本文定义；LB 的主指标 `p10` 统一为 `companions.q10` |
| `entries[].coop` | `delta_ind`、`delta_cut`、`delta_perm{struct, full}`、`r_central`、`g`、`eta_comm`、各自 `ci95`、`g_num_ci95`、`g_den_ci95`、`n_a_reason`、`rho_use`、`r_fb`、`exempt_delivered`、`sensitivity{with_violations, without_violations}`、`audit_flag` | 本文定义 |
| `entries[].robust` | 按扰动族的 `u_q[]`、`s_rob`、`l_deg`、`p_det`、`t_detect_median`、`p_rec`、`t_recover_median`、`censored_ratio`、`resolution_floor` 与区间、`uncertain`、`status` | 本文定义 |
| `entries[].ledger` | 各维度均值、P95、违规占比、被拒提交数、`cpu_source` | — |
| `entries[].status` | `valid`、`incomplete`、`invalid`；`invalid` 只在 `invalid_policy = exclude` 下按 F-45 产生，此时 `rank`、`tie_group`、`rank_ci95`、`uncertain_group` 为 null，`dominance_tier = null` | — |
| `entries[].dominance_tier`、`uncertain_group`、`reference_kind`、`legacy_baseline` | 见 §4.6、R-M32-100 | — |
| `entries[].quality` | 无效占比、失败占比、`infra_missing`、`platform_rerun` | — |
| `comparisons[]` | R-M32-26；`family ∈ {rank, dom, dom_ref, rob, fid}` | — |
| `stats` | §6.2.1 `Stats` 全部字段 | 在 AWR-06 §4.7 字段之上增补 `index_generator`、`scenario_order`、`rank_family`、`rank_family_converged`、`analytic_families`、`ci_methods`、`alpha`、`epsilon_resource`、`invalid_policy`、`invalid_threshold`、`dominance`、`dmath_version`、`stage2_selection` |
| `rows_digest` | `rows.jsonl` 规范化后的 SHA-256，使榜单与其权威输入绑定 | LB R-LB-61 已采纳 |

### 7.4 原因码（`why`）

| 代码 | 含义 | 处置 |
|---|---|---|
| `metric_spec_invalid` | 违反 V-1 至 V-12 | 拒绝 Suite 登记或榜单构建 |
| `scoring_field_unknown` | 清单含 §6.2.3 以外的 `scoring` 字段或非法取值 | 拒绝 |
| `metric_non_finite`、`metric_out_of_range`、`metric_null_not_allowed` | 分量入库校验失败 | 拒绝该行，判评测器缺陷 |
| `missing_field`、`schema_mismatch`、`unknown_metric` | 行层校验失败 | 拒绝该行 |
| `duplicate_cell` | 同一行键同一 `attempt` 出现内容不同的两行 | 拒绝构建 |
| `group_inconsistent` | 同组行的内核、网络或时钟档不一致 | 拒绝构建 |
| `ledger_mismatch` | 重算违规、`refuse` 维度超额或 `ranked_valid`/`invalid_reasons` 期望值与记录不一致 | 基础设施故障 |
| `ledger_unmetered` | 排名且非影子赛道的运行非 cgroup 计量 | 基础设施故障 |
| `resource_misaligned` | 对照或参考 Episode 的资源包、网络参数、赛道、内核或属性摘要不一致 | 该对不可用 |
| `exec_record_invalid` | 干预执行记录与 `CutSpec`/`PermSpec` 不符 | 该对照 Episode 按基础设施故障处置 |
| `insufficient_scenarios`、`insufficient_replicates` | 退化情形 | 不给区间，判定"未判定" |
| `ratio_unbounded`、`denominator_small`、`ratio_unstable` | `G` 的 Fieller 区间无界、分母下界 < 0.05、`fixed_set` 下分母非正复本占比 > 2.5% | `G = n/a` |
| `usable_pairs_low`、`fallback_high`、`donor_unavailable` | `ρ_use < 0.8`、`r_fb > 0.2`、全部场景 `m_s < 2` | 对应 Δ 记 `n/a` |
| `invalid_rate_high` | `exclude` 下无效占比超过阈值（F-45） | 条目 `status = invalid` |

### 7.5 调用方

| 调用方 | 调用 | 阶段 |
|---|---|---|
| `py:bench/subworker.py`（泛化后，WP-D4-12） | `InterventionPlan.expand`、`build_board_v2` | P0 |
| `py:bench/boardstore.py`（`board.v2` 发布） | `normalize_rows`、`build_board_v2`；发布前 `verify_board` 自检 | P0 |
| M31 清单解析器 | `from_suite_manifest`、`validate_metrics` | P0 |
| M29 投递包装层 | 不调用 M32；读取 Episode 规格中序列化的 `CutSpec`/`PermSpec` | P0（`cut`）/ P1（`perm`） |
| M33 重放工具 | `replay_tolerance` | P1 |
| `drone4d suite verify`（平台侧，随仓库） | `verify_board` | P0 |
| `drone4d suite audit`（平台侧） | `audit_baselines` | P1 |
| `drone4d bench report`（SDK，M34） | `analyze_experiment` | P0 |
| `py:benchmarks/`（M31，pilot 工具） | `pilot_power`、`required_scenarios`、`pa_checks` | P0 |
| `py:bench/eval/aggregate.py` | `compat.d3_board_v1.*` | P0 |

---

## 8. 与 D3 `aggregate.py` 的兼容委托

### 8.1 方案

| 项 | 规定 |
|---|---|
| 委托外壳 | `py:bench/eval/aggregate.py` 改为：保留模块文档字符串与 `__all__`；`METRICS`、`N_BOOT` 常量原样保留；`build_board`、`compose`、`lane_metrics` 以相同签名调用 `awr.evaluation.compat.d3_board_v1` 的同名函数，并传入 `metrics = METRICS`、`evaluator_version = EVALUATOR_VERSION`、`score_version = SCORE_VERSION`；`entry_details`、`ranked_row` 直接转发 |
| 兼容实现 | `py:evaluation/compat/d3_board_v1.py` 为 D3 算法的冻结移植：逐语句保持 numpy 调用、随机数消耗顺序、浮点归约顺序与舍入；唯一的结构变化是把常量 `METRICS` 与版本常量改为参数。不以 D4 通用原语重写，因通用原语采用不同的随机数生成器（F-08）与归约方式（ND-1），二者在设计上不等价 |
| 发行范围 | `compat/` 含 D3 任务语义常量（`physics = "realistic"`、`event_type` 缺省 `rendezvous_handoff`、`physics_not_ranked` 等，D3-INV-10、D3-INV-15），且只服务平台侧 D3 榜单；随仓库与平台评测镜像存在，`drone4d` wheel 构建时排除 `awr/evaluation/compat/**`（CV-4 检查 wheel 内容）。摘要第 1 点"与任务语义无关"只针对 wheel 内的核心包 |
| 依赖方向 | `awr.bench.eval.aggregate` → `awr.evaluation.compat`；`awr.evaluation` 不 import `awr.bench`（R-M32-75）。B-10 中"`awr.bench` 只允许……`awr.evaluation` 的 D3 兼容入口 import"在本方案下不再需要，建议改为反向约束（反馈第 15 条） |
| 冻结版本 | 逐字节一致以仓库锁定的 `numpy==2.5.3` 为前提（F11）；numpy 升级须重跑 §8.4 全部对拍并经评审，视同 D3 维护变更。D4 路径不受此约束（R-M32-103） |
| 权威 | 对拍以 G0 冻结原版（`tests/d4/evaluation/oracle/aggregate_d3_frozen.py`）的输出为准；§8.2 不变量清单是说明性文档，二者不一致时修订清单 |
| 通用原语的来源 | D4 路径的 `MetricSpec` 参数化、组首并列、方向化支配与共享下标等逻辑以 D3 的 `_boot`、`_Pairs`、`build_board` 为蓝本另行实现于 `py:evaluation/` 的通用模块，D3 兼容实现不调用它们 |

### 8.2 D3 兼容不变量

| 编号 | 不变量（D3 现行行为） |
|---|---|
| D3-INV-01 | `_units`：按 `r["event"]` → `int(r.get("seed", 0))` → 行序列表组织 `evaluation.components` |
| D3-INV-02 | `_point`：事件值为该事件全部种子全部重复中非空值的平铺均值；赛道值为有值事件的等权均值；无值为 `None` |
| D3-INV-03 | `_rng`：`sha256("‖".join(parts).encode())` 前 8 字节小端为种子，`np.random.default_rng` |
| D3-INV-04 | `lane_metrics` 的 `key` 由调用方给出：`build_board` 与 `compose` 传 `(entry_id, board_version, lane)`（不含 `track`）；`subworker` 传 `(f"{team}/{sid}", board_version, lane)` |
| D3-INV-05 | `_boot`：事件按名称升序；每事件先 `integers(0, n_seeds, (n, n_seeds))`，后 `random((n, n_seeds, max_rep))`；重复下标 `floor(u · rp)`；超出被抽种子重复数的位置置 NaN；事件内 `_nanmean` 平铺；赛道值为有限事件值之和除以有限事件数 |
| D3-INV-06 | 四项指标共用同一复本序列（同一 `rng`、同一下标） |
| D3-INV-07 | `ci95`：有限复本的 `np.percentile` 2.5 与 97.5（numpy 缺省线性插值）；`value` 为 `None` 或无有限复本时 `[0.0, 0.0]`；`round(·, 6)` |
| D3-INV-08 | `repeat_delta`：存在重复数 > 1 的种子时为各种子重复极差的最大值（不舍入），否则 `None` |
| D3-INV-09 | `_paired_diff_ci`：两个独立复本序列逐元素相减、取有限值、percentile 2.5/97.5；空时 `(0.0, 0.0)`；不舍入 |
| D3-INV-10 | `ranked_row`：`evaluation.ranked is not False` 且 `physics_not_ranked` 不在 `invalid_reasons` |
| D3-INV-11 | `build_board` 只纳入 `dev or ranked_row(r)` 的行；分组键 `(track, lane, entry_id)`，`sorted` 遍历；`track.dev = true` 时不过滤、不判无效且以全部行计算 |
| D3-INV-12 | 无效判定 `len(valid) < 0.8 · len(rs)`，`rs` 为该组经 D3-INV-11 过滤后的行；无效与否，非开发榜的指标均只按有效 Run 计算；无效条目 `status = invalid`，`dominance_tier = None`，不排名 |
| D3-INV-13 | 排名：候选为非基线有效条目中该指标值非空者，按 `(−value, entry_id)` 排序；相邻配对差区间含 0（`lo ≤ 0 ≤ hi`）同组；新组的 `rank = i + 1`；`tie_group` 自 0 递增 |
| D3-INV-14 | 支配：四项点估计均非空且 `x ≥ y`，且至少一项配对差区间下界 > 0；逐层剥离按 `remaining` 列表顺序；基线层级为支配它的候选最高层 + 1（无则 1）；基线与候选使用同一判定 |
| D3-INV-15 | 顶层字段：`tracks[]` 由所用行的 `track` 排序生成，`physics = "realistic"`、`mode = "exogenous"`、`fleet_ref`、`obs_level`、`agent_class` 取 `track` 配置缺省值；`lanes[]` 的 `event_type` 缺省 `rendezvous_handoff`；`seed_set_commit = sha256(",".join(sorted(seeds)))`；`evaluator_major` 与 `platform_digest` 由版本常量计算 |
| D3-INV-16 | `compose`：权重非负且和为正，否则 `ValueError`；归一化；`value = round(dot(w, vals), 6)`；区间为 `boots @ w` 有限值的 percentile 并舍入；任一指标为 `None` 时 `value = None`、`ci95 = [0.0, 0.0]` |
| D3-INV-17 | `entry_details`：按 `(entry_id, track, lane, event)` 分组；有效 Run 为空时用全部 Run；指标为非舍入均值；`repeat_delta = None` |
| D3-INV-18 | 字典键的插入顺序与当前实现一致（`json.dumps` 不排序键，`board.json` 字节依赖插入顺序） |
| D3-INV-19 | `subworker.process_one` 的提交结果：`lane_metrics` 以该提交的全部行（不经 `ranked_row` 过滤、含无效行）计算；`status = "valid" if valid >= 0.8 * len(rows) else "invalid"`，分母为全部行数；与 D3-INV-12 的比较方向互为补集、边界相同，但分母与计算行集不同；`runs.platform_rerun` 为平台原因重跑次数，失败的尝试不写入 `rows.jsonl` |
| D3-INV-20 | `boardstore._listed`（输入前置条件，不属 `aggregate.py`）：保留全部基线行，以及每个（团队、分轨、赛道）中状态为 `DONE`、`visibility = board` 且 `(done_unix_ns, submission_id)` 最大的提交的行；`publish` 与 `compose_board` 在调用 `build_board`/`compose` 前施加；影子重算须复现该过滤（R-M32-72） |

**`d3-score-1` 的差异（AWR-05 v1.6，B18）**：D3-INV-01 至 D3-INV-20 描述 `d3-score-0`，保持不变。榜单 v1（`configs/bench.d3/board-v1/board.json`，`score_version = d3-score-1`）与之相比的差异如下，影子重算与兼容移植按此分支实现：

| 编号 | 差异 |
|---|---|
| D3-S1-01 | `_rng`：随机数为 `HMAC-SHA256(K_board, entry_id‖board_version‖lane)` 的前 8 字节，取代 D3-INV-03 的 `sha256` 前 8 字节；`K_board` 存于 EvalStore（`drone4d.bench.board_evalstore.v1`） |
| D3-S1-02 | 分轨与赛道取自榜单配置，按配置中的次序排列，取代 D3-INV-15 由行的 `track` 排序生成 |
| D3-S1-03 | 组内行按 `(event, seed, rep, run_id)` 排序后计算；行只携带种子序号 k ∈ [0, K)，携带种子值的行被拒绝 |
| D3-S1-04 | 状态增加 `incomplete`：缺少任一 K × R 运行的条目不给名次与层级 |
| D3-S1-05 | 条目增加 `display_order`，实现缺省显示次序 |
| D3-S1-06 | `seed_set_commit` 取自锁文件（`drone4d.bench.board_lock.v1`，HMAC 承诺），取代 `sha256(seeds)` |

### 8.3 `drone4d-event@1` 在 `board.v2` 中的口径

- D3 榜单继续以 `board.v1` 发布与冻结；`board.v2` 中的 `drone4d-event@1` 视图只用于影子重算与跨 Suite 浏览，不替换 `board.v1`。
- 口径：`estimand = fixed_set`（D3 事件为固定集合）；`rank_mode = per_metric`（R-M32-100）；四项指标 `direction = max`、`range = [0, 1]`；`ce` 为可空指标且允许进入分层（V-5 例外，复现 D3"任一为空则不支配"的语义）。
- `invalid_policy = exclude`、`invalid_threshold = 0.2`（R-M32-77、F-45），与 `board.v1` 可比；影子重算报告并列给出 `zero` 口径；开发榜（`track.dev = true`）不施加阈值（D3-INV-11）。

`per_metric` 模式规则表：

| 项 | 规则 |
|---|---|
| F_rank | 每个指标 k 一个族；成员为 C_k 内组首比较（选向，双侧）；无 Rf |
| `n_boot` | `max_k m_rank,k` 代入 F-15，全组共用一组下标 |
| 候选集合 | C_k = 非参考、`status = valid` 且指标 k 点估计非空的条目 |
| 名次与名次区间 | 在 C_k 内计算；F-19 复本中 μ* 缺失的条目不计入 |
| 参考行 | D3 `baseline = true` → `reference_kind = published`、`legacy_baseline = true`；只进入 F_domref |
| 支配 | 分层指标为四项全部；任一点估计为空的条目不支配也不被支配；F_dom 与 F_domref 按 R-M32-31、R-M32-96 |

### 8.4 验证

| 编号 | 方法 | 通过条件 |
|---|---|---|
| CV-1 黄金快照 | G0 时刻冻结的 D3 `rows.jsonl`、榜单配置与 `board.json`、`details.json`（G0-5）在委托后重新 `publish` | 两个文件逐字节一致 |
| CV-2 随机化对拍 | 以固定种子生成 ≥ 1,000 组随机行集（条目 1–120，赛道 1–4，事件 1–6，种子 1–12，重复 1–3，含空值、无效 Run、基线、`dev` 榜、非排名运行与恰在 80% 边界的有效占比），分别调用冻结原版与委托后的 `aggregate`；另以随机提交行集对拍 `subworker` 的提交结果计算（D3-INV-19） | `build_board`、`compose`、`entry_details`、`lane_metrics` 的返回值经 `json.dumps(indent=1, ensure_ascii=False)` 后逐字节一致；`n_boot ∈ {100, 800, 10,000}` |
| CV-3 调用方回归 | `tests/d3/board/`、`tests/d3/eval/`、`tests/d3/api/test_bench_board_api.py`、`tests/d3/env/test_subworker.py` | 全部通过；`test_recompute_100_entries_4_lanes_within_60s` 取 3 次运行的中位数，不超过该测试自身的 60 s 绝对上限，并记录委托前后中位数比值供评审（不以相对比值作门禁） |
| CV-4 依赖与发行 | 静态检查 `awr.evaluation` 的 import 图；检查 wheel 内容 | 不含 `awr.bench`；只装 numpy 的环境中 import `awr.evaluation.compat.d3_board_v1` 成功；`drone4d` wheel 不含 `awr/evaluation/compat/` |

### 8.5 `board.v1` 影子重算报告

WP-D4-09 对 D3 已发布 `board.v1` 的 `rows.jsonl` 执行 R-M32-72，报告内容：每个（分轨、赛道、指标）下 `board.v1` 名次与 `board.v2` 口径名次的对照；并列组变化；配对差区间宽度在共享下标与独立下标下的比值；`zero` 与 `exclude` 口径的差异。报告作为内部对照发布（AWR-06 §10.1 第 (3) 项），不替换 `board.v1`，名次变化逐条列出并登记于 RK-13 的监测记录。

---

## 9. 蒙特卡洛验证方案

### 9.1 目的与原则

蒙特卡洛验证在 WP-D4-11 中完成，是 AC-D4-14 的执行方案，也是 Q-M32-01（`rank_family`）、Q-M32-09（F_dom 解析参照）与小样本设计裁决的依据。原则：(1) 真值由数据生成模型以大样本数值积分确定，不以渐近公式代替；(2) 被测代码即生产代码 `py:evaluation/`，不另写简化实现；(3) 全部随机数可复现；(4) 判据事先写定，结果不满足时如实报告并触发处置，不调整判据。

### 9.2 数据生成模型

对条目 e（含参考行 `π_ind`）、场景 s、种子 i：

```
η_{e,s,i} = θ_e + a_s + c_{e,s} + ε_{e,s,i}
a_s       ~ 场景难度效应（全部条目共享，配对后抵消）：N(0, τ_a²) 或 τ_a · t₃ / sqrt(3)
c_{e,s}   ~ 条目 × 场景交互（形成配对差的场景间方差）：N(0, τ_c²) 或 τ_c · t₃ / sqrt(3)
ε_{e,s,i} = ρ_ω ζ_{s,i} + sqrt(1 − ρ_ω²) ξ_{e,s,i}，ζ、ξ ~ N(0, τ_ε²)（ζ 为共同随机数带来的共享种子效应）
U_{e,s,i} = 0（概率 p₀，策略失败）；否则 clip(η_{e,s,i}, 0, 1)
```

缺省 `τ_a = 0.15`、`ρ_ω = 0.5`、`θ` 取 0.55 附近；对每个条件以二分法标定 `τ_c` 与 `τ_ε`，使配对差 `D = U_A − U_B` 的标准差 `σ_D = 0.10`、场景间 ICC 达到目标值（标定以 10⁶ 次模拟估计）。估计目标真值 `μ_e` 与 `δ = μ_A − μ_B` 以 10⁷ 次场景抽样数值估计，相对误差 < 10⁻⁴。`π_ind` 参考行与候选条目同模型生成，其 θ 按各实验设定。

### 9.3 条件网格

核心网格：`S ∈ {12, 16, 18, 24}` × `ICC ∈ {0.1, 0.3, 0.5}` × `p₀ ∈ {0.05, 0.15}` × 场景效应分布 `{正态, t₃}`，m = 5，R = 1，共 48 个条件；每条件 2,000 次重复（95% 覆盖率估计的二项标准误约 0.49 个百分点），B 按 F-15（K = 2 时为 10,000）。

### 9.4 实验

| 编号 | 目的 | 设计 | 统计量 | 判据 |
|---|---|---|---|---|
| MC-1 区间覆盖 | `family` bootstrap-t 的覆盖率 | 核心网格；单条目均值与配对差两类区间；同时运行 percentile 作对照 | 95% 区间覆盖 `μ_e` 与 δ 的比例 | bootstrap-t 覆盖率 ∈ [93%, 97%]（AC-D4-14） |
| MC-2 F_rank 族错误率 | F_rank 两种 `rank_family` 的 Holm 族错误率 | K ∈ {6, 10, 20}；S ∈ {12, 18}。全零假设：全部候选与 `π_ind` 的 θ 相同，K − 1 个候选间比较与 K 个 Rf 比较均为真零假设；部分零假设：候选前半 θ 相同、后半依次高 0.05，`π_ind` 的 θ 等于前半，前半的 Rf 比较与前半内部比较为真零假设、其余为假零假设；Rf 为定向单侧，候选间为双侧（F-41） | 至少一个真零假设被判"显著且实际更优"的比例；另报不使用 F-41（v0.1 单侧口径）时的族错误率作对照 | 点估计 ≤ 0.05 + 1.645 × sqrt(0.05 × 0.95 / 2,000) ≈ 5.8%；`head` 不满足而 `all_pairs` 满足时，缺省改为 `all_pairs`（Q-M32-01） |
| MC-3 功效与等价 | 判定词在缺省设计下的可达性 | 核心网格，δ ∈ {0, 0.05}，K = 6 | δ = 0.05 时各 Holm 步的功效；δ = 0 时等价可达率 | 报告；S = 18、ICC = 0.3 的值与 §6.3 解析值差 < 0.05，否则复核 F-37 |
| MC-4 配对增益 | 共享下标相对独立下标的区间宽度 | 核心网格；同一数据分别以共享下标与按条目独立下标计算配对差区间 | 宽度比的均值与 95% 区间 | 宽度比 95% 区间上界 < 1 |
| MC-5 支配区分度 | 支配分层的信息量 | K ∈ {10, 20}；3 项分层指标（U 与两项资源比，相关系数 −0.3 至 0.3）；典型噪声；含 1 个 `π_ind` 参考行 | 非第 1 层条目占比、不确定组占比、真支配对的检出率、参考行层级的正确率 | 报告（无阈值）；解析路径与复本路径（以 `n_boot = 200,000` 强制）对照报告 |
| MC-6 固定集合 | `fixed_set` percentile 的覆盖率 | S ∈ {4, 8}（固定场景），m ∈ {5, 10}；真值为固定场景上的均值 | 覆盖率 | ∈ [93%, 97%]；m = 5 不满足时在 `validity.md` 声明 |
| MC-7 比值区间 | `G` 区间覆盖率与 `n/a` 率 | 分母真值 ∈ {0.05, 0.10, 0.20}；核心网格子集；`family`（Fieller）与 `fixed_set`（复本比值 percentile，S ∈ {4, 8}、m ∈ {5, 10}）两种口径 | 覆盖率、`n/a` 率 | 覆盖率 ∈ [93%, 97%]（分母真值 ≥ 0.10，两种口径分别判定）；其余报告 |
| MC-8 名次区间 | 名次区间覆盖真实名次的比例 | K ∈ {6, 10, 20}，θ 等间隔 0.02 与 0.05 | 覆盖率 | 报告（名次区间未经选择校正，不设阈值） |
| MC-9 小样本设计 | X1（6 场景 × 3 种子）与 FC-4（6 场景 × 3 种子）的区间性质与 FC-4 可达性 | 两种设计各在 `family` bootstrap-t 与 `fixed_set` percentile 下运行；FC-4 另设真实两档偏差 δ_f ∈ {0, 0.03} | 覆盖率、半宽分布；FC-4 在 δ_f = 0 时的等价可达率（90% 区间 ⊂ [−0.05, 0.05] 的比例） | 覆盖率 < 90% 的口径不得用于 AC-RC-3 与 FC-4（反馈第 9 条）；FC-4 等价可达率 < 50% 时，在反馈中提请提高 FC 最低样本设计或把 FC-4 改为只报告区间（反馈第 27 条） |
| MC-10 解析与复本一致 | F-14(c)、F-42 与复本 p 值的一致性 | K = 20，F_dom 全部对，以 `n_boot = 200,000` 计算复本 p 值作对照；`family` 与 `fixed_set` | 判定一致率、p 值相对差 | 判定一致率 ≥ 99% |
| MC-11 F_dom 族错误率 | 解析路径下 F_dom 与 F_domref 的 Holm 族错误率 | K ∈ {5, 10, 20}，|T| = 3；S ∈ {12, 16, 18}；全零假设（全部 θ 相同）与部分零假设（半数相同）；`family` 下比较正态参照与 Student t（ν = n − 1）参照；`fixed_set` 以 S ∈ {4, 8} 运行闭式方差路径 | 至少一个真零假设上判"显著且实际更优"的比例（逐指标与跨指标） | 点估计 ≤ 5.8%；Student t 参照满足而正态参照不满足时确认 t 为缺省；二者均不满足时按 Q-M32-09 处置（增大 `n_boot` 上限或缩小 F_dom） |
| MC-12 复跑判据 | F-39 的误判率与检出率 | 以核心网格中 S ∈ {12, 18} 的条件生成原条目，按 R-1 设计复跑 10 个 Episode（新种子）；偏差条目的复跑均值加偏移 Δ_b ∈ {0, 0.05, 0.1, 0.2, 0.3}；同时运行 v0.1 判据作对照 | Δ_b = 0 时的失败率（诚实条目误判率），Δ_b > 0 时的检出率 | 诚实条目误判率 ≤ 2%；报告检出率曲线；不满足时调整 α_r 并发布新 `stats.version` |
| MC-13 pilot 上界 | ICC 与 `σ_D` 的场景层自助上界覆盖率 | S_pilot ∈ {8, 12, 16}，m ∈ {3, 5}，ICC ∈ {0.1, 0.3, 0.5} | 80% 上界覆盖真值的比例；据此计算的 S 达到目标功效 0.8 的比例 | 覆盖率 ≥ 75%；不满足时缺省改为 90% 上界并在 `pilot_power` 中登记 |

### 9.5 复现与执行

- 种子：每次重复的数据生成种子为 `resample_seed("drone4d.eval.mc.v1", condition_id, str(rep))`，数据生成以 `cbrng` 的 `normal_elem`/`uniform_elem` 取样；被测代码的重采样种子按 F-07 由合成的 `board_version = "mc-<condition_id>-<rep>"` 派生。
- 产物：`mc_report.v1.json`（每条件每实验的统计量、蒙特卡洛标准误、判据结论、代码与网格摘要）与中文摘要报告；作为 WP-D4-11 交付物入档。
- 算力估算（待测）：v0.1 给出的单次重复耗时（MC-1、MC-3、MC-4 约 5 ms，MC-2 `all_pairs`、K = 20 约 0.3 s）为未经推导的数量级估计，不作计划依据。WP-D4-11 第 1 周以生产代码在 RM-8 上实测每个实验的单次重复耗时（取 50 次重复的中位数），据此更新本节与执行计划；实测前按"全部实验单核合计不超过 48 小时、8 进程并行不超过 8 小时"预留算力。
- CI：`py:evaluation/` 的统计模块变更时运行缩减网格（4 个条件 × 200 次重复，MC-1、MC-2、MC-4、MC-11），判据放宽为覆盖率 ∈ [90%, 99%]、族错误率点估计 ≤ 9%，用于发现回归；完整网格在 WP-D4-11 交付与每次 `stats.version` 变更前运行。

---

## 10. 测试与验收

### 10.1 测试布局

| 层 | 路径 | 内容 |
|---|---|---|
| 单元 | `tests/d4/evaluation/test_{metric,manifest,rows,cube,resample,bootstrap,holm,ranking,dominance,ledger,intervention,robust,fidelity,verify,report,pilot,numerics,dmath}.py` | 构造用例与手算值 |
| 性质 | `tests/d4/evaluation/test_properties.py` | 无环性、对条目顺序与行序不变性、分块不变性、方向对称性（交换 A、B 后差值取负）、下标与 `entry_id` 无关 |
| 黄金向量 | `tests/d4/evaluation/golden/{resample_vectors,stats_examples,dmath_vectors}.json` | F-08 下标（含缺失场景算例）、典型分析组的完整输出、`dmath` 函数值 |
| D3 对拍 | `tests/d4/evaluation/test_d3_compat.py`、`oracle/aggregate_d3_frozen.py` | §8.4 CV-1 至 CV-4 |
| 确定性 | `tests/d4/evaluation/test_determinism.py` | `NPY_DISABLE_CPU_FEATURES` 两种设置、`OPENBLAS_NUM_THREADS ∈ {1, 8}`、numpy 1.26 与 2.5.3 子进程比对 |
| 性能 | `tests/perf/evaluation/test_board_v2_perf.py` | R-M32-82、R-M32-83 |
| 蒙特卡洛 | `tools/evaluation/mc/` | §9 |

### 10.2 验收

| 编号 | 度量与阈值 | 方法 | 对应 AC-D4 |
|---|---|---|---|
| AC-M32-01 | V-1 至 V-8 每条至少一个正例与反例；方向化后 `min` 指标的排名与支配与"取负后按 `max`"一致；`per_metric` 只对 `drone4d-event` 放行 | 单元 | — |
| AC-M32-02 | 立方体聚合与手算值一致（含不等重复数、可空指标、场景级指标、`target` 的 Episode 层变换）；入库校验三种原因码各有用例；IQM、q10 与伴随统计量输出与参考实现一致（相对误差 < 1e-12）；`fail_rate` 只读 `failure.policy` | 单元 | AC-D4-08（离线重算的输入侧） |
| AC-M32-03 | F-08 黄金向量 4 组逐值一致（含缺失场景与缺失种子算例）；同组不同 `entry_id` 的条目使用同一下标；场景序号按码点序；以独立实现（测试内以纯 Python 整数重写 F-07、F-08）复算一致 | 单元、黄金向量 | AC-D4-15 |
| AC-M32-04 | 复本均值的顺序累加实现与"`y[idx].mean(axis=1)`"参考实现相对误差 < 1e-12；bootstrap-t（含 `N_j` 随复本变化的情形）与 percentile 区间与手算小例（S = 3、B = 枚举全部 27 种下标）一致；`ci_clipped` 置位正确 | 单元 | AC-D4-14 |
| AC-M32-05 | §6.4.4 每种退化情形有用例且输出无 NaN、无异常；p 值公式（单侧、双侧 F-41）在枚举小例上与定义一致 | 单元 | — |
| AC-M32-06 | Holm 与手算例一致（含 p 值并列）；调整 p 单调；判定词三类与 §6.4.5 的重叠情形均有用例；各族 `sidedness` 与 R-M32-95 一致 | 单元 | AC-D4-14 |
| AC-M32-07 | 链式用例（A≈B、B≈C、A ≻ C）在 `head` 与 `all_pairs` 下均得到 {A, B}、{C}；`all_pairs` 结果对输入顺序不变（1,000 次随机置换）；`head` 定点算法在 10,000 组随机输入上的收敛率被记录；参考行不占名次 | 性质 | — |
| AC-M32-08 | 名次区间：已知完全分离的条目集得到退化区间 `[r, r]`；全同条目集的区间覆盖 `[1, K]` | 单元 | — |
| AC-M32-09 | 性质测试：任意随机数据上支配图无二元环；构造的三元环被合并为一个不确定组且同层；参考行层级按 F-22 且以 F_domref 判定；层级对条目输入顺序不变 | 性质 | AC-D4-14（支配区分度报告） |
| AC-M32-10 | 失败与无效计分：`max`、`min`、`target`、可空、不可空与声明 `penalty` 六类指标的方向化处理与 F-23 一致；`zero` 下资源比保留实测；`exclude` 下有效占比恰为 80%（不判无效）与 79%（判无效）的边界用例与 D3 一致，`invalid` 条目不进入 C；有效种子 2 个的场景使条目 `incomplete`（`exclude` 下 `ranked_valid = false` 不计有效）；基础设施故障重跑后仍失败计入 `infra_missing` | 单元 | — |
| AC-M32-11 | `n_boot` 规则：K ∈ {1, 5, 50, 100, 300} 时与 F-15 一致；§6.3.1 对照表逐行复现；走解析路径的族登记于 `analytic_families` 且参照分布正确 | 单元 | — |
| AC-M32-12 | 合成策略：零通信策略的 `Δ_cut` 95% 区间含 0；"完全依赖通信"策略的 `Δ_cut` 与构造值差 < 0.01；`Δ_ind`、`R_central` 回收构造值；扩容收益不出现在协作字段；`interactive` 下标注"总效应" | 合成 | AC-D4-16 |
| AC-M32-13 | 可用对比例：构造 25% 违规对时 `ρ_use = 0.75` 且该项 Δ 为 `n/a`；分母只含两侧均存在的对；敏感性分析两版均输出；资源包、赛道、内核或属性摘要不一致的对被判不可用 | 合成 | AC-D4-16 |
| AC-M32-14 | `G`：`family` 下分母显著为正时 Fieller 区间有界且覆盖构造值；`fixed_set` 下复本比值区间可计算；分母下界 < 0.05 时 `n/a`；MC-7 判据满足 | 合成、MC | — |
| AC-M32-15 | `Δ_perm`：合成策略回收构造值（差 < 0.01）；`r_fb = 0.25` 时 `n/a`；`perm_struct` 主口径选择正确；过敏反应用例置 `audit_flag`；边界用例：`m_s = 1` 的场景被剔除、全部场景 `m_s = 1` 时 `donor_unavailable`；`_ix` 控制消息在 `perm` 下照常投递且不计入置换数、在 `cut` 下被阻断；供体 Episode 缺失时全部置换投递计入 `r_fb`；`check_exec_record` 对不符记录返回 `exec_record_invalid` | 合成 | AC-D4-26 |
| AC-M32-16 | `InterventionPlan`：展开计数与 AWR-06 §8.5 成本表一致（主评测 90、冒烟 6、`cut` 90、`perm` 90/180、X1 每族 90）；同单元格 `run_seed` 跨条目、干预、重复相同；供体映射为循环移位；`CutSpec`/`PermSpec` 序列化进 Episode 规格；第二阶段选择只依赖第一阶段结果 | 单元 | AC-D4-04（配对前提） |
| AC-M32-17 | 账本：以 M28 `ledger_summary` 结构的样例为输入，比值与 F-34 一致；`enforce = violation` 维度构造的违规与内核记录一致时通过、不一致时 `ledger_mismatch`；`enforce = refuse` 维度超额判 `ledger_mismatch` 而非违规；`budget.policy` 两个取值下 `ranked_valid`/`invalid_reasons` 期望值比对与计分映射与 R-M32-57 一致；排名非影子赛道的 `process_time` 来源被拒绝、影子赛道放行并标注；token 额度为 0 的两种 `enforce` 用例；`energy_uwh` 到 `energy_wh` 换算 | 单元 | AC-D4-24（计量侧） |
| AC-M32-18 | 跨保真度：合成两档数据上 FC-4 至 FC-7 的度量值与解析值一致；FC-4 以 `fixed_set` 计算；FC-6 以 F_fid 判定显著对；Kendall τ_b 处理并列正确；样本不足或显著对 < 5 时状态为 `not_run`；输出通过 `fidelity_audit.v1` 校验 | 合成 | AC-D4-27 |
| AC-M32-19 | `verify_board`：参考提交生成的 `board.v2` 复核逐字节一致；篡改 `rows.jsonl` 任一分量、删除任一行或修改 `stats` 任一字段均被检出并给出 JSON Pointer；`stats` 自述块字段齐全（R-M32-68）；在两台不同硬件等级的机器上复核亦逐字节一致 | 集成 | AC-D4-15 |
| AC-M32-20 | `verify_commitments`：模拟赛季末公开盐与种子后承诺与逐行 `seed_tag` 验证通过；篡改种子值或序号被检出 | 单元 | AC-D4-23、AC-D4-42 |
| AC-M32-21 | `rerun_check` 判据与 F-39 一致（含阈值计算的手算例）；MC-12 判据满足；`audit_baselines` 对摘要不一致返回 `freeze` | 单元、MC | AC-D4-29、AC-D4-30 |
| AC-M32-22 | D3 兼容：§8.4 CV-1、CV-2、CV-3 全部通过 | 对拍 | AC-D4-13、B-07 |
| AC-M32-23 | `awr.evaluation` import 图不含 `awr.bench`；lint 规则覆盖 | 静态 | AC-D4-44 |
| AC-M32-24 | 只装 numpy（分别为 1.26 与 2.5.3）的干净环境（CPython 3.12）中 import `awr.evaluation` 全部模块后 `sys.modules` 不含 scipy、jsonschema、numba、open3d；`drone4d` wheel 的依赖声明满足 R-M32-103 的 RFC 结论；wheel 不含 `compat/` | CI | AC-D4-43 |
| AC-M32-25 | 同一输入在 `NPY_DISABLE_CPU_FEATURES` 两种设置、BLAS 线程数 1 与 8、numpy 1.26 与 2.5.3 的组合下产生逐字节一致的 JCS 输出（`board.v2` 与 `analyze_experiment` 两条路径）；核心函数不读墙钟与环境变量（静态检查）；判定路径不调用 `math.log`、`math.exp`、`math.erf`、`statistics.NormalDist`（静态检查） | 子进程比对、静态 | AC-D4-05（统计侧） |
| AC-M32-26 | R-M32-82 的性能与内存预算在 RM-8 上满足（`family`、`fixed_set`、`all_pairs` 三项分别测量，各取 3 次中位数） | 性能 | — |
| AC-M32-27 | MC-1 至 MC-13 按 §9 执行并入档；MC-1、MC-2（所选 `rank_family`）、MC-4、MC-6、MC-10、MC-11、MC-12 判据满足 | 蒙特卡洛 | AC-D4-14 |
| AC-M32-28 | 北极星配置的 `analyze_experiment` ≤ 5 s；S < 12 时输出提示；报告数据结构被 M34 渲染测试使用 | 性能、集成 | AC-D4-11、AC-D4-12 |
| AC-M32-29 | `required_scenarios` 以占位值得 S = 18；`power_table` 复现 AWR-06 §8.6 功效表（差 < 0.01）；ICC 与 `σ_D` 的场景层自助上界在合成数据上的覆盖率满足 MC-13 判据，不满足时 `upper_q` 改为 0.9 并登记；`pa_checks` 与手算一致 | 单元、MC | AC-D4-20 |
| AC-M32-30 | X1：`S_rob`、`L_deg`、`Ū_q` 的 `fixed_set` 区间可由 `rows.jsonl` 复算；半宽 > 0.1 时标注"不确定"；`p_det`、`T̃_detect`、`p_rec`、`T̃_recover` 与 12 个人工标注用例的聚合值一致（删失值不进入中位数）；F_rob 的 Holm 与判定词与手算例一致；每格有效种子 < 2 时该族 `incomplete` | 合成 | AC-D4-25（AC-RC-3、AC-RC-4） |
| AC-M32-31 | 交叉自助：T = 3、5 时区间可计算；T < 3 不报告；以枚举小例验证 F-38 | 单元 | — |
| AC-M32-32 | B-LLM：重复不足 3 次的单元格记缺失；`repeat_delta` 计算正确；回放模式下结果逐字节一致 | 单元 | AC-D4-39 |
| AC-M32-33 | `board.v1` 影子重算报告生成（先施加 `_listed` 过滤，含两种 `invalid_policy` 口径与名次变化清单）；研究分析函数（`N*`、负协作区域、`B*_θ`（含跨赛道复本对齐）、Kendall τ 区间）在合成数据上回收构造值 | 集成、合成 | §10.1 第 (3) 项、DL-D4-03 |
| AC-M32-34 | 全部用户可见文字通过项目文案核对清单，无口语化表述 | 评审 | — |
| AC-M32-35 | 分析组视图与清单映射：S2 两个场景集各一个主指标的清单通过 V-1、V-10；`scenario_sets` 引用不存在的场景集被拒绝；V-9 至 V-12 每条至少一个正例与反例（含 S1 `resp_mean_s` 无 `penalty` 且不可空的反例、S3 `conserv_ratio` 的 target 无界用例）；§6.2.3 映射对 S1、S2、S3、X1 清单样例逐字段正确，`stratified_seed_percentile`、未知 `scoring` 字段被拒绝；`scenario_sets[].estimand` 覆写生效 | 单元 | — |
| AC-M32-36 | 行完整性：`missing_field`、`schema_mismatch`、`unknown_metric`、`duplicate_cell` 各有用例；`attempt` 归并取最后一次非基础设施故障行；完全相同的重复行被去重；性质测试：随机打乱行序、随机分块、插入重复相同行后 `build_board_v2` 输出逐字节不变 | 单元、性质 | — |
| AC-M32-37 | `dmath`：黄金向量（每函数 ≥ 50 点）逐位一致；与离线高精度参考值的误差在 §6.5.1 目标内；以第二种语言（C，`-ffp-contract=off`）按规格实现的结果逐位一致；F_domref 与解析路径 p 值在黄金分析组样例中逐字节复现 | 黄金向量 | AC-D4-15 |
| AC-M32-38 | `replay_tolerance`：容差边界两侧用例；null 不一致与布尔、计数不一致判不一致；`replay_tol` 覆写生效；M33 重放测试以本函数判定 | 单元、集成 | AC-D4-30（配合） |
| AC-M32-39 | `per_metric`：`ce` 为空的条目在 `ce` 上无名次且不支配、不被支配；各指标候选集合不同时名次区间在 C_k 内计算；`n_boot` 取各族最大；D3 基线映射为 `published` 并只进入 F_domref；`exclude` 阈值 0.2 生效 | 单元 | AC-D4-13（配合） |

---

## 11. 风险与待决

### 11.1 风险

| 编号 | 风险 | 触发指标 | 缓解 |
|---|---|---|---|
| RK-M32-01 | numpy 升级破坏 D3 兼容的逐字节一致 | CV-2 对拍失败 | 锁定 `numpy==2.5.3`；升级视同 D3 维护变更并重跑 §8.4 |
| RK-M32-02 | `head` 族的族错误率超标 | MC-2 点估计 > 5.8% | 缺省改为 `all_pairs`，`stats.version` 加一（Q-M32-01） |
| RK-M32-03 | F_dom 在条目数增大后功效过低，分层退化为"几乎全部第 1 层" | MC-5 非第 1 层占比 < 10%（K = 20） | 在 RFC 中裁定 F_dom 的族定义（反馈第 8 条）；分层视图标注"区分度有限" |
| RK-M32-04 | 小样本设计（X1、FC）区间失真或等价不可达 | MC-9 覆盖率 < 90% 或 FC-4 等价可达率 < 50% | 改用 `fixed_set` 口径（已采纳）或增加场景与种子数（反馈第 9、27 条） |
| RK-M32-05 | 性能不达标 | R-M32-82 超时 | 分块与缓存；解析路径不生成逐对复本；不降低 `n_boot` |
| RK-M32-06 | F_dom 解析路径的族错误率在小 S 下超标 | MC-11 点估计 > 5.8% | Student t 参照（已为缺省）；仍超标时按 Q-M32-09 增大 `n_boot` 上限或缩小 F_dom |
| RK-M32-07 | 契约字段与 CT 不一致 | CT 建立时字段名或结构变化 | CT 建立前本文 §7.3 为单一来源；CT 以其为初稿迁入，变化时本文同步修订 |
| RK-M32-08 | Δ 族被误读为协作因果量 | 外部材料把 `Δ_ind` 称为协作增益 | 条目卡固定标注因果解释范围（ADR-D4-22）；R-M32-51 |
| RK-M32-09 | 并行文档未按 §13.2 同步，实现者读到互相矛盾的规则 | WP-D4-01 RFC 时 §13.2 未关闭项 > 0 | RFC 逐项裁定 §13.2；各文档修订后在本文 §13.2 标注关闭 |
| RK-M32-10 | `dmath` 自含实现存在精度缺陷 | AC-M32-37 误差超出目标 | 增加级数项数或改用更优约化；`dmath_version` 加一并发布新 Board Version |
| RK-M32-11 | numpy 升为 wheel 必选依赖不被接受 | RFC 否决 | 为 `analyze_experiment` 提供纯 Python 降级路径（性能目标另定），`build_board_v2` 只在平台侧运行（Q-M32-10） |

### 11.2 待决

| 编号 | 问题 | 建议 | 裁决时点 |
|---|---|---|---|
| Q-M32-01 | F_rank 的族定义取 `head`（定点算法）还是 `all_pairs` | 以 MC-2 结果裁决：`head` 族错误率满足判据则保留，否则改为 `all_pairs` | WP-D4-11（第 6 周内核门前） |
| Q-M32-02 | 无效与失败运行对消耗类指标的处置 | 采纳 R-M32-36/38 的方向化规则，修订 AWR-06 §8.6 措辞，并按 §13.2 同步 S1、S3 与 LB 的相应条款 | WP-D4-01 RFC |
| Q-M32-03 | `drone4d-event@1` 在 `board.v2` 视图中的 `invalid_policy` | 已决（待 RFC 确认）：`exclude`、θ = 0.2，与 M31 R-M31-61 一致；`zero` 只在影子重算报告中并列 | WP-D4-01 RFC |
| Q-M32-04 | 下标生成器与 `‖` 编码 | 采纳 F-07、F-08；`stats.index_generator` 登记 | WP-D4-01 RFC |
| Q-M32-05 | `limit_expr` 求值器归属 | 已决：置于 M28（`eval_limit_expr`，R-M28-69）；M32 读取清单中的求值结果 | — |
| Q-M32-06 | `board.v2` 是否增加 `comparisons[]` 与 `rows_digest` | 增加（`rows_digest` 已被 LB R-LB-61 采纳） | WP-D4-12 前 |
| Q-M32-07 | 干预执行归属 | 采纳 §2.2：M32 定义纯数据规格与供体规则，M29 实现 `DeliveryInterceptor` 并执行，M33 `MessageIndex` 检索载荷；同步修订 M29、M30（§13.2） | WP-D4-01 RFC |
| Q-M32-08 | 参考缓存键的统一定义 | 以 M31 R-M31-45 为基础，增补 `impl_sha256`、`attrs_digest`、`intervention`（§13.2 第 10 项），同步修订 AWR-06 §10.2 关键路径说明与 M30 R-M30-65 | WP-D4-13 缓存格式冻结前 |
| Q-M32-09 | F_dom 解析路径的参照分布与 MC-11 不满足时的处置 | 缺省 Student t（ν = n − 1）；MC-11 仍不满足时二选一：(a) 把 `n_boot_cap` 提高到使 F_dom 可用复本 p 值的规模（K = 20 时 228,000）；(b) F_dom 改为按指标分族 | WP-D4-11 |
| Q-M32-10 | `drone4d` wheel 的 numpy 依赖 | 建议升为必选依赖 `numpy>=1.26`；备选为 `analyze_experiment` 纯 Python 降级 | WP-D4-01 RFC |

---

## 12. 追溯

| 上游 | 本文需求 | 本文验收 | AWR-06 验收 / 工作包 |
|---|---|---|---|
| R-D4-04 协作智能排行榜 | R-M32-13–R-M32-35、R-M32-67、R-M32-68、R-M32-88–R-M32-96、R-M32-100 | AC-M32-03–AC-M32-11、AC-M32-19、AC-M32-35、AC-M32-36、AC-M32-39 | AC-D4-14、AC-D4-15；WP-D4-11、WP-D4-12 |
| R-D4-02 可复现与 trace 后处理分离 | R-M32-67–R-M32-71、R-M32-81、R-M32-85、R-M32-101、R-M32-102 | AC-M32-19–AC-M32-21、AC-M32-25、AC-M32-37、AC-M32-38 | AC-D4-15、AC-D4-29、AC-D4-30；WP-D4-30 |
| R-D4-12 三个研究方向 | R-M32-41–R-M32-54、R-M32-97–R-M32-99 | AC-M32-12–AC-M32-16、AC-M32-30、AC-M32-33 | AC-D4-16、AC-D4-25、AC-D4-26；WP-D4-13、WP-D4-19、WP-D4-20、WP-D4-41 |
| R-D4-16 北极星 | R-M32-78、R-M32-83、R-M32-103 | AC-M32-28、AC-M32-24 | AC-D4-11；WP-D4-08 |
| R-D4-17 D3 不回归 | R-M32-72–R-M32-77、R-M32-100 | AC-M32-22、AC-M32-23、AC-M32-33、AC-M32-39 | AC-D4-13；WP-D4-09；B-07 |
| R-D4-08 保真度分档 | R-M32-61–R-M32-66 | AC-M32-18 | AC-D4-27、AC-D4-35；WP-D4-27、WP-D4-33 |
| R-D4-18 首篇论文 | R-M32-53、R-M32-79 | AC-M32-29、AC-M32-33 | DL-D4-03；WP-D4-17、WP-D4-41 |
| ADR-D4-10 墙钟隔离与资源账本 | R-M32-55–R-M32-60 | AC-M32-17 | AC-D4-24；WP-D4-22 |
| ADR-D4-15 正式成绩物理档与审计 | R-M32-61–R-M32-65、R-M32-101 | AC-M32-18、AC-M32-38 | AC-D4-27 |
| ADR-D4-19 层次模型与 `board.v2` | R-M32-05、R-M32-26、R-M32-77、R-M32-88、R-M32-91 | AC-M32-01、AC-M32-19、AC-M32-35 | WP-D4-12 |
| ADR-D4-21 主排名与合成 | R-M32-05、R-M32-27–R-M32-35、R-M32-100 | AC-M32-07–AC-M32-09、AC-M32-39 | — |
| ADR-D4-22 Δ 族 | R-M32-41–R-M32-51、R-M32-54、R-M32-97 | AC-M32-12–AC-M32-16 | AC-D4-16、AC-D4-26 |
| ADR-D4-23 统计协议 | R-M32-06–R-M32-31、R-M32-93–R-M32-96 | AC-M32-02–AC-M32-09、AC-M32-27 | AC-D4-14 |
| ADR-D4-24 失败与无效计分 | R-M32-36–R-M32-40、R-M32-89、R-M32-92 | AC-M32-10、AC-M32-36 | — |
| ADR-D4-25 种子承诺与公开重采样种子 | R-M32-13、R-M32-14、R-M32-69、R-M32-93 | AC-M32-03、AC-M32-20 | AC-D4-23、AC-D4-42；WP-D4-21 |
| B-02、B-10、AC-D4-43、AC-D4-44 依赖闭包 | R-M32-75、R-M32-80、R-M32-103 | AC-M32-23、AC-M32-24 | WP-D4-01、WP-D4-44 |
| B-LLM 统计协议（§8.3） | R-M32-12 | AC-M32-32 | AC-D4-39；WP-D4-38 |
| 公开快照（§8.13） | R-M32-67、R-M32-68 | AC-M32-19 | AC-D4-40；WP-D4-40 |
| X1 协议（`docs/d4/suites/X1-RobustCoord协议.md`） | R-M32-52、R-M32-98、R-M32-99 | AC-M32-30 | AC-D4-25 |

---

## 13. 反馈与同步

### 13.1 对 AWR-06 的反馈

以下各条只在本文提出，不修改 AWR-06；"位置"指 AWR-06 的章节或条目。第 1 至 20 条为 v0.1 提出（第 4、8、10 条在 v0.2 修订），第 21 至 30 条为 v0.2 新增。

| 编号 | 位置 | 问题 | 建议 | 理由 |
|---|---|---|---|---|
| 1 | §8.6 检验族与并列组；ADR-D4-23 | F_rank 由"每个条目与其组首的比较"组成，而组首由前序比较的判定决定；Holm 逐步校正要求事先给定全部 p 值，族成员与判定循环依赖，族错误率控制无严格保证 | 二选一并写入 ADR-D4-23：(a) 保留"与组首"并采用本文 §6.4.2 的确定性定点算法，以 MC-2 实测族错误率作为采纳条件；(b) 改为固定族"全部无序候选对 + 与 `π_ind`"，以 ≻ 关系做组首扫描。建议以 (b) 为后备，由 MC-2 裁决 | 条目卡与论文 H2 的"显著"结论需要有效的族错误率；(b) 的 ≻ 关系无环且与顺序无关，代价是 `n_boot` 上升（K = 30 时约 186,000）与功效下降 |
| 2 | §8.6 无效运行；ADR-D4-24 | "`ranked_valid = false` 的 Episode 全部分量记 0"对越小越好的资源指标（`b_tx_ratio`、`cpu_ratio`）等于给出最优值，无效运行在支配分层中反而获益；与同表"失败计分：资源按实际消耗"不一致 | 改为方向化规则：结果类指标置不利端（主指标为 0；不利端无界时取 Suite 声明的 `penalty` 或置空），消耗类指标保留实际消耗，安全类保留实测；同步修订引用原措辞的 Suite 规格（本文 §13.2 第 13、16、18 项） | D3 四项指标均为越大越好，原措辞在 D3 无害；D4 引入方向后须修订 |
| 3 | §8.6 区间；§8.5 `G`；§9.1 `B*_θ` | 规定 `family` 一律用 bootstrap-t，但 studentized 统计量只对均值型（线性）统计量有直接定义；`G`、`B*_θ`、名次、IQM、相关系数无自然的标准误 | 明确"线性统计量用 bootstrap-t，非线性统计量用 percentile 或专用方法（`family` 下 `G` 用 Fieller，`fixed_set` 下用复本比值 percentile）"，并在 `stats.ci_methods` 中逐量登记 | 避免实现者为非线性量构造嵌套自助法，成本高且未经验证 |
| 4 | §8.6 配对；ADR-D4-25 | 只规定下标种子 `SHA-256("drone4d.board.resample.v1" ‖ …)`，未规定 (a) `‖` 的字节编码（直接拼接存在歧义）；(b) 由种子生成下标的算法；(c) 场景与种子的排序及场景数 S 的取法 | 采纳本文 F-07（以 U+2016 连接，与 D3 `_rng` 一致，并限制标识字符集）、F-08（SplitMix64 计数器加 32 位乘移位）与 R-M32-93（S 为声明场景集全集，按码点序），在 `stats` 中增加 `index_generator` 与 `scenario_order` | "任何人可由 `rows.jsonl` 复算"的承诺需要完全规定、与语言和库版本无关的生成器与索引规则（v0.2 删去 v0.1 中未经核实的 NumPy NEP 19 引用） |
| 5 | §4.2 `MetricSpec`；§7.3 ANEES | `direction = target` 缺少目标区间字段；S3 的 ANEES 区间内比例是跨种子（跨运行）计算的场景级量，`MetricSpec` 无层级字段，`fixed_set` 下无法按种子重采样 | 增加 `target: [lo, hi]`、`level ∈ {episode, scenario}` 与分指标 `epsilon`；场景级指标须提供 `recompute(seed_weights)` 回调 | 使方向化支配与 S3 一致性诊断可执行 |
| 6 | §7.0 主效用、§8.4；ADR-D4-19、ADR-D4-21 | "每个 Suite 恰有一个 primary"与 `drone4d-event@1`（四项指标、无合成，ADR-191）冲突；"分层指标须对全部条目测得且非空"与 D3 `ce` 可空冲突 | 明确 `drone4d-event@1` 例外：`rank_mode = per_metric`、无主指标、`ce` 可空且沿用 D3"任一为空则不支配"语义 | 否则 D3 榜单无法按 ADR-D4-19 登记为 Suite |
| 7 | §4.4 `budget.policy` | 取值 `violation_invalidates` 与 `violation_zero` 未定义差异；§8.5 第 3 条只说明前者导致 `U = 0` | 定义：`violation_invalidates` 置 `ranked_valid = false` 后按 `invalid_policy` 处理；`violation_zero` 只把主指标置 0，其余分量保留；两者对 `ranked_valid`/`invalid_reasons` 的写入由 M28 负责 | 两者对资源分量与无效占比统计的影响不同，需在 Suite 清单冻结前确定 |
| 8 | §8.6 重采样次数与检验族 F_dom | (a) 同一分析组的复本由全部族共享，按族分别取 `n_boot` 不可行；(b) `n_boot` 由 F_rank 决定时，F_dom（规模 `|T|·K(K−1)/2`）在 3 项分层指标下 K ≥ 5 即超出复本分辨率（K = 5 时需 12,000 > 10,000），解析路径成为缺省路径；正态参照在 S = 12 至 18 下偏乐观，与 §8.6 选用 bootstrap-t 的理由相同；(c) Holm 在数千个检验上功效很低，分层可能退化为几乎全部第 1 层 | (a) 改为"`n_boot` 由 F_rank 决定，F_dom 超出分辨率时走解析路径"；(b) 解析路径在 `family` 下以 Student t（ν = n − 1）为参照，`fixed_set` 下以分层自助闭式方差与正态参照，并以 MC-11 实测族错误率作为采纳条件；(c) 在 RFC 中评估是否把 F_dom 限定为"全部指标均不劣于"的候选对（须说明这是数据依赖的族）或改为按指标分族，以 MC-5 的区分度结果决定 | 共享下标是配对的前提，`n_boot` 只能是组级常量；支配分层需要可用的区分度与有效的族错误率（v0.2 更正：v0.1 所称"K > 18 时超过 200,000"是按 F_dom 自定 `n_boot` 的条件，与实际规则不符，§6.3.1） |
| 9 | §7.4 X1 样本；§5.8 样本设计 | X1 与跨保真度审计均为 6 场景 × 3 种子；在 `family` 估计目标下以 6 个场景做场景层 bootstrap-t，覆盖率缺乏依据（§8.6 的模拟只覆盖 S ≥ 12） | 为 X1 与 FC 设计显式声明估计目标（建议 `fixed_set`，结论限定于所选场景；X1 已采纳，本文 FC-4 已采纳），或提高场景数；以 MC-9 结果决定 | AC-RC-3 与 FC-4 的结论可信度取决于区间性质 |
| 10 | §8.6 功效、样本量与模拟 | `σ_D` 的 χ² 置信上界按独立样本自由度计算，而同场景的种子相关（ICC > 0），自由度被高估、上界偏低 | 以实例层（场景层）自助法给出 `σ_D` 上界，与 ICC 上界的做法一致（本文 F-37 已采纳，v0.1 的 F-37 仍使用 N − 1 自由度，已在 v0.2 更正），并以 MC-13 报告上界覆盖率 | 偏低的上界使场景数规则偏乐观 |
| 11 | §8.6 统计协议的验证；AC-D4-14 | "Holm 族错误率 ≤ 5%（含蒙特卡洛误差）"未给出可执行判据 | 明确为"点估计 ≤ 0.05 + 1.645·sqrt(0.05·0.95/R)"（R = 2,000 时约 5.8%） | 验收判据须可执行 |
| 12 | §5.8 FC-8 | "Wasserstein-1 相对距离"的归一化对象未定义（PDR–距离曲线不是分布） | 按本文 F-36：PDR 曲线用相对 L1 距离，时延分布用 `W1 / mean`，P95 时延用相对差 | 使 FC-8 可实现、可复核 |
| 13 | §4.4 `budget.dims.limit_expr`；§8.4 `η_comm` | (a) `limit_expr` 的表达式语法与求值归属未规定；(b) `η_comm` 以字节 B 与 `B_floor = 0.05·B_cap` 定义，而 `B_cap` 随场景的 `n_agents`、`duration_s` 变化 | (a) 在 CT 中规定受限文法（数字、`+ − × ÷`、括号、登记变量），求值器置于 M28（M28 R-M28-69 已采纳）；(b) 以 `b_tx_ratio` 的场景均值定义（本文 F-30），与原式在 `B_cap` 恒定时等价 | 避免跨场景混合不同额度的字节量 |
| 14 | §4.7 `board.v2` | 缺少：成对判定的明细（`comparisons[]`）、权威输入摘要（`rows_digest`）、`stats` 中的下标生成器、`rank_family`、解析路径族、逐量区间方法与每指标非空场景数 `n` | 按本文 §7.3 增补 | 外部复核者需要判定依据与输入绑定，否则只能复算数值而无法核对结论 |
| 15 | §3.4 B-08、B-10 | (a) B-10 允许"`awr.evaluation` 的 D3 兼容入口 import `awr.bench`"，而 §3.3 规定 `aggregate.py` 委托 `py:evaluation/`，双向 import 会形成循环，且 `awr.evaluation` 进入 wheel 后不能依赖不在 wheel 中的 `awr.bench`；(b) B-08 的冻结区写为"`py:bench/eval/` 的 EC/EU/AO/CE 评测器"，`aggregate.py` 位于同目录，归属不够明确 | (a) 改为"`awr.evaluation` 不得 import `awr.bench`；`awr.bench.eval.aggregate` 可 import `awr.evaluation.compat`；`compat/` 不进入 wheel"；(b) 明示 `aggregate.py` 不在冻结区，但受 B-07 约束且只允许改为委托外壳 | 与 WP-D4-09 的实施方式一致，避免 lint 规则与 wheel 打包冲突 |
| 16 | §8.6 判定词 | "显著且实际更优"（d̂ > 0.02）与"等价"（90% 区间 ⊂ [−0.05, 0.05]）在 0.02 < d̂ < 0.05 时可同时成立，规则未给出优先级 | 规定以"显著且实际更优"为准，并标注"差异显著但小于等价边界" | 避免条目卡出现矛盾判定 |
| 17 | §8.6 名次与等价 | 等价判定未做多重校正，名次区间未做同时置信校正，文中只对名次注明"未经选择校正" | 条目卡对等价判定同样注明"逐对判定，未做多重校正" | 防止把多对等价判定合读为"整组等价" |
| 18 | §10.2 WP-D4-09、§10.1 G0-5 | D3 兼容的逐字节一致依赖仓库锁定的 numpy 版本（`numpy==2.5.3`），G0 清单未把该版本列为冻结项 | 在 G0-5 冻结项中加入 D3 运行环境锁文件（`requirements-d3.lock`）摘要 | numpy 的随机数与归约实现变化会使 B-07 回归门误报或漏报 |
| 19 | §8.5 `Δ_perm` | 供体映射 `ω' = σ(ω)` 未规定具体置换；供体 Episode 的条目归属（同条目主评测）只隐含在"同一场景、另一外生种子"中 | 规定 `σ` 为场景内种子序号循环移位 `i → (i + 1) mod m_s`，供体为同一条目同一重复的主评测 Episode | 使 `Δ_perm` 可复核，并保证供体集合在条目间对称 |
| 20 | §8.5 成本控制、§8.10 | 第二阶段（`Δ_perm`、X1）只对"候选与前 k 名"执行，选择依据于第一阶段结果；文中未要求记录选择清单 | 要求把第二阶段选择清单与所依据的第一阶段 Board 状态摘要写入 `stats`，第二阶段结果不得反向影响选择 | 防止选择过程不可复核，或第二阶段结果被用于调整入选名单 |
| 21 | §8.6 优于检验 | 规定单侧检验 `H0: μ_A − μ_B ≤ 0`，而并列扫描与支配检验的方向由点估计决定（"点估计较高者优于较低者"）；按数据选向的单侧检验在真零假设下任一方向判显著的概率约为 2α | 区分定向比较（与 `π_ind`、候选对参考行，单侧）与选向比较（候选条目之间，双侧 `p₂ = min(1, 2p₁)`），写入 ADR-D4-23 | Holm 的族错误率保证以各检验的名义水平为前提；本条为评审外补充修订 S-1 |
| 22 | §8.10 R-1、R-2b | 判据"复跑均值落入原 95% CI，或差值 < ε"把总体均值的置信区间当作复跑均值的接受域，未计入复跑样本自身的抽样误差；10 个 Episode 的均值标准误与原区间半宽同量级，诚实条目的失败概率可达约三分之一（§6.3 算例约 0.38），ε = 0.02 远小于抽样噪声 | 改为预测区间判据：`|μ̂_rerun − μ̂| ≤ z_{1−α_r/2}·sqrt(SE_原² + SE_复跑²) + ε`（本文 F-39，α_r = 0.01），或等价检验；以蒙特卡洛评估诚实条目误判率与偏差检出率（本文 MC-12） | `verified` 申请的复核结论须有可控的误判率，否则诚实团队被频繁驳回 |
| 23 | §3.4 B-02；AC-D4-43 | 要求 `awr.evaluation` 进入 `drone4d` wheel 且第三方依赖只有 numpy，而现行 SDK 的必选依赖为 `httpx`、`msgpack`，numpy 只在可选附加依赖 `images` 中；`drone4d bench report` 须 import `awr.evaluation` | 明确 wheel 把 numpy（`>=1.26`）升为必选依赖，或为本地报告提供纯 Python 降级路径；并在 AC-D4-43 中加入 numpy 最低版本与锁定版本的输出一致性检查 | 否则干净环境安装 SDK 后本地报告不可用，或输出随 numpy 版本变化 |
| 24 | §8.6 无效运行；§8.12"无效条目门" | `invalid_policy = exclude` 的语义只写"Suite 可显式声明"，未规定阈值、比较方向、无效条目是否排名与分层；§8.12 提到"20% 阈值 按 `invalid_policy` 声明"但未落到字段 | 在 `scoring` 中增加 `invalid_threshold`，规定 `exclude` 下"有效数严格少于 `(1 − θ)` 倍总数则 `status = invalid`、不排名、`dominance_tier = null`，否则只按有效 Episode 计算"（本文 F-45），`drone4d-event@1` 取 0.2 | 使 `drone4d-event@1` 的 `board.v2` 视图与 `board.v1` 可比，并消除"沿用 D3"与"待裁定"的并存 |
| 25 | §4.2 `MetricSpec`；§7.0 | (a) 结果类指标的不利端可能无界（如响应时间 `range = [0, null]`），失败计分无法置为不利端；(b) 同一 Suite 不同场景集可有不同主指标（S2 搜索与跟踪），"每个 Suite 恰有一个 primary"无法表达；(c) §8.4 要求 IQM 与均值并报，但单值 `aggregation` 字段无法让同一指标输出多个统计量 | 增加 `penalty`（或强制 `nullable`）、`scenario_sets` 作用域（主指标唯一性按场景集判定）与 `companions`（伴随统计量），见本文 R-M32-88 至 R-M32-90 | 使失败计分、多场景集 Suite 与稳健统计量可登记、可校验 |
| 26 | §4.4 `budget.dims`；§5.7 | 预算维度未区分"提交时拒绝"（字节、平台计数的 token，可在提交时精确判定）与"事后记违规"（CPU、GPU 实测）；与 `budget.policy` 并列时读作超额即整局无效 | 在 `budget.dims[]` 增加 `enforce ∈ {refuse, violation}`，`refuse` 维度不产生违规、只核对使用量不超额度（S1 规格反馈第 7 条同此建议；本文 R-M32-56） | 两类维度的后果与可判定时点不同，混用会使字节超额被双重处置 |
| 27 | §5.8 FC-4、FC-6 与样本设计 | (a) FC-4 未指明估计目标；在 `family` 下以 6 场景 × 3 种子估计，90% 区间半宽约 0.06（σ_D = 0.10、ICC = 0.3），即使两档无偏差也几乎不可能落入 `[−0.05, 0.05]`；(b) FC-6"经 Holm 校正显著的成对差值"未定义检验族，若取 F_rank 则显著对数量过少 | (a) FC-4 明确采用 `fixed_set` 口径（该口径下等价可达率约 0.6，以 MC-9 实测），实测低于 50% 时提高 FC 最低样本设计或改为只报告区间；(b) FC-6 以审计条目的全部无序对为独立检验族（本文 F_fid），显著对少于 5 个时判 `not_run` | 判据须在声明的最低样本下可达，否则审计结论恒为 `fail` 或无意义 |
| 28 | §8.5 `Δ_perm` | 未规定：(a) 场景内只有 1 个种子时循环移位为恒等映射；(b) 交互控制消息在 `cut`/`perm` 下的处理；(c) 供体 Episode 因基础设施故障缺失时的计数 | (a) `m_s < 2` 的场景不可估计 `Δ_perm`；(b) 控制消息受 `cut` 阻断、不受 `perm` 替换（与 M29 R-M29-77 一致）；(c) 供体缺失时置换投递全部按回退计入 `r_fb`（本文 R-M32-97） | 小种子数的 X1 与本地实验常见，规则缺失会产生无意义的 `Δ_perm` |
| 29 | §8.11 参考行与支配分层 | 参考行层级为"支配它的候选最高层 + 1"，而支配要求至少一项在 F_dom 内 Holm 显著；参考行不在 F_dom 内，无 p 值，支配关系无法定义 | 增设独立检验族"候选 × 参考行 × 分层指标"（定向单侧，独立 Holm），以其判定候选是否支配参考行（本文 F_domref、F-22） | 使参考行层级可计算并受族错误率控制 |
| 30 | §10.2 关键路径说明；§8.5 参考缓存 | 缓存键写为"（Suite 版本、场景配置摘要、种子序号、参考策略版本）"：含 Suite 版本会使新增赛道等次版本变化使缓存全部失效，且不含赛道、内核版本、实现摘要与属性摘要，存在误命中风险；与 M30 R-M30-65、M31 R-M31-45 的定义互不兼容 | 在 WP-D4-13 冻结缓存格式前统一为一份定义：`(scenario_digest, seed_index, reference_ref, impl_sha256, attrs_digest, track_id, kernel_version, intervention)`，不含 `suite_digest`（本文 §13.2 第 10 项） | 缓存误命中会使 Δ 族与 `G` 的参考值错配，且无法从榜单上察觉 |

### 13.2 并行文档同步修订清单

以下各项为本文 v0.2 与并行文档的不一致之处，本文不修改并行文档，由各文档负责人在 WP-D4-01 RFC 前处理；处理后在本表"状态"列标注。

| 编号 | 文档与位置 | 不一致之处 | 建议修订 | 状态 |
|---|---|---|---|---|
| 1 | M28 §8 运行清单 `ledger_summary` | 缺少 M32 核算所需的已求值额度与计量来源 | 增补 `budgets{dim: {per, limit: int, enforce}}`（`limit_expr` 求值后）与 `cpu_source ∈ {cgroup, process_time}` | 已同步（M28 v0.2 运行清单含已求值额度与 `cpu_source`，CT §4.5.2） |
| 2 | M28 R-M28-69、R-M28-71 | `budget.policy = violation_zero` 下 `ranked_valid` 是否改变、`invalid_reasons` 的取值未明确；`enforce = refuse` 维度不应产生违规 | 明确：`violation_invalidates` 置 `ranked_valid = false` 并写 `budget_violation:<dim>`；`violation_zero` 不改 `ranked_valid`，写 `budget_zero:<dim>`；`refuse` 维度不写违规 | 转 WP-D4-01 RFC（AWR-06 v0.3 附录 F） |
| 3 | M28 §6.3.9 `energy_uwh` 与 `det{agent}` | 能量按实体计量，`ledger_summary.det` 以 Agent 为键，能量归属规则未写 | 明确 `det[agent].energy_uwh` 为该 Agent 控制实体能耗之和，未受控实体计入 `team` | 转 WP-D4-01 RFC（AWR-06 v0.3 附录 F） |
| 4 | M29 UC-08、R-M29-24、§6 `DeliveryInterceptor` 注释"由 M32 实现" | 与本文 §1.3、§2.2 的执行归属矛盾 | 改为"M29 实现 `DeliveryInterceptor`，按 Episode 规格中的 `CutSpec`/`PermSpec` 执行；置换载荷经 M33 `MessageIndex.nearest` 检索；M29 不 import `awr.evaluation`" | 已同步（M29 v0.2.1，XR-16） |
| 5 | M29 执行记录 | 本文 `exec_record` 字段（`blocked`、`permuted`、`fallback`、`fallback_blocked`、`exempt_delivered`、`donor_episode_id`）未在 M29 定义 | M29 在真值层与运行清单中输出该摘要 | 已同步（M29 v0.2.1 R-M29-24） |
| 6 | M29 R-M29-77 与 `PermSpec.exempt_channels` | 控制消息豁免在 M29 以通道名硬编码 | 以 `PermSpec.exempt_channels` 为准，缺省 `["_ix"]` | 已同步（M29 v0.2.1 R-M29-24、R-M29-77） |
| 7 | M30 §2.2 边界表 M32 行 | 写"M32 负责 `Δ_perm`/`perm_struct` 的执行" | 改为"M32 定义 `PermSpec` 与供体规则，M29 执行；M30 提供 `x-awr-role` 字段角色标注" | 已同步（M30 v0.2 §2.2） |
| 8 | M30 R-M30-65 参考缓存键 | 含 `suite_id@version`，不含 `track_id` | 按第 10 项统一 | 已同步（M30 v0.2.1，XR-15） |
| 9 | M31 R-M31-04、V-S-02 | "每个 Suite 恰有一个 primary"与 S2 两场景集各一个主指标冲突 | 改为"每个场景集视图恰有一个有效 primary"（本文 R-M32-88、V-1） | 已同步（M31 v0.2.1 R-M31-04、V-S-02） |
| 10 | M31 R-M31-45；AWR-06 §10.2；M30 R-M30-65 | 参考缓存键三处定义互不兼容 | 统一为 `(scenario_digest, seed_index, reference_ref, impl_sha256, attrs_digest, track_id, kernel_version, intervention)`，不含 `suite_digest`；M31 为定义方，本文与 M30 引用 | 已同步（XR-15：M31 R-M31-45 为唯一定义） |
| 11 | M31 §8.1 清单 schema `metrics[]` | 缺 `scenario_sets`、`penalty`、`companions`、`precision`、`supports_subset`、`replay_tol` 字段；`scoring` 缺 `invalid_threshold` | 按本文 §6.2.3 增补 | 已同步（CT §5.1.2；M31 v0.2.1 §8.1 引用） |
| 12 | M31 R-M31-61 | "`invalid_policy` 沿用 D3 当前行为"未落到字段 | 写为 `invalid_policy = exclude`、`invalid_threshold = 0.2`（本文 R-M32-77） | 已同步（M31 v0.2 R-M31-61） |
| 13 | S1 §6.4 与 R-S1-24、AC-S1-12 | 写"违规按 `violation_invalidates` 使 Episode `U = 0`/计 0"，未区分结果类与消耗类分量 | 改为"`ranked_valid = false`，按 `invalid_policy = zero` 方向化计分（主指标为 0，资源分量保留实测）" | 已同步（S1 v0.2.1） |
| 14 | S1 §7.7 `MetricSpec` 登记 | 无 `nullable` 列；`resp_mean_s`、`j_max_s`、`realloc_latency_s` 在 `T_done = ∅` 时为空但未声明可空；`resp_mean_s`、`j_max_s`、`j_sum_m`、`energy_wh`、`reassign` 为 `min`、`range = [0, null]`，失败计分无定义（V-9） | 增加 `nullable` 列；对上述指标声明 `penalty`（如以 `horizon_s` 为时间量上限）或 `nullable = true` | 已同步（S1 v0.2.1 §7.7 可空性说明） |
| 15 | S1 §7.7 `jain_fair` | `direction = target` 未给 `target` 区间，违反 V-3；§7.3 又标为"诊断" | 改为 `direction = max`（Jain 指数越接近 1 越公平），或给出 `target` 区间 | 已同步（S1 v0.2 登记为 `max`） |
| 16 | S3 §8.6 失败与违规计分表 | "`violation_invalidates`：`ranked_valid = false`，全部分量记 0"沿用 AWR-06 原措辞 | 按第 13 项同样改写 | 已同步（S3 v0.2 §8.6） |
| 17 | S3 §8.1 指标清单 | `aoi_c_mean_s`、`aoi_c_p95_s`、`disagree_w2_m` 为 `min`、`range = [0, null]` 且未声明可空；`conserv_ratio` 为 `target`、`range = [0, null]`（不利端无界，但为场景级指标） | 前三项声明 `penalty` 或 `nullable = true`；`conserv_ratio` 在评测器卡中声明场景内失败处理规则（R-M32-89） | 部分同步（S3 v0.2 已声明 `aoi_c_*` 可空；`conserv_ratio` 的场景内失败处理转 S3 下一版） |
| 18 | S3 §11 清单 `scoring` | 嵌套结构 `hierarchy`、`holm.families`、`dominance.metrics_from` 与本文扁平字段不同 | 以本文 §6.2.3 映射为准；`holm.families` 应为 `[rank, dom, dom_ref]` | 已同步（S3 v0.2.1 族名） |
| 19 | S2 §7.1、R-S2-02 | 主指标按场景集区分，需 `scenario_sets` 作用域 | `u_cs` 声明 `scenario_sets = [搜索场景集]`，`u_cs_trk` 声明 `scenario_sets = [跟踪场景集]` | 不再需要（S2 v0.2 采用单一主指标 `u_cs`） |
| 20 | S2 `cs-handoff-mesh`（`fixed_set`） | `G` 在 `fixed_set` 下的区间方法未写 | 引用本文 R-M32-45（复本比值 percentile） | 已同步（S2 v0.2.1） |
| 21 | X1 §10.5 F_rob | 选向比较使用单侧 percentile p 值 | 改为双侧（本文 F-41、R-M32-98） | 已同步（X1 v0.2 双侧） |
| 22 | X1 §11 清单 `ci.method = seed_bootstrap_percentile` | 本文 v0.1 用 `stratified_seed_percentile` | 以 `seed_bootstrap_percentile` 为统一值（本文已改） | 本文已对齐 |
| 23 | S1 §11 清单 `ci.method = cluster_bootstrap_t` | 与本文一致 | — | 无需修订 |
| 24 | LB §13.3 `entries[].metrics` 主指标 `p10`、`iqm` | 字段名与结构与本文 `companions{iqm, q10}` 不同 | 改为 `companions.iqm`、`companions.q10` | 已同步（LB v0.2 `companions`） |
| 25 | LB §8.4 计量来源 | "进程内计量只用于本地运行与 P0 影子榜"需赛道级标记 | 赛道定义增加 `shadow: bool`，供本文 R-M32-58 判定 | 已同步（LB v0.2 标签 `shadow`） |
| 26 | LB §13 契约引用 | 以"待 CT 定稿"标注，而 CT 尚未建立 | CT 建立前以本文 §7.3 为统计字段单一来源 | 已同步（CT v0.2 已建立） |
| 27 | LB 失败与无效计分相关条款 | 若引用 AWR-06"全部分量记 0" | 按第 13 项改写 | 已核对：LB v0.2 未沿用该措辞；AWR-06 v0.3 §8.6 已改为方向化规则 |
| 28 | LB E-11 `rows.jsonl` 字段 | 无 `attempt`、`row_schema` | 增补（本文 R-M32-92） | 已同步（LB v0.2.1 E-11） |
| 29 | M33 R-M33-65 | 等级 2 容差"由 M32 定义"，本文 v0.1 未提供 | 引用本文 R-M32-101、F-44 | 本文已补 |
| 30 | M33 R-M33-76 `MessageIndex` 键 | 键为 `(channel, schema, sender_role, size_class)`，本文 v0.1 的匹配键含 `nearest_rel_time`、`size_bucket` | 以 M33 键为准，相对时刻为排序规则（本文 `PermSpec` 已改） | 本文已对齐 |
| 31 | M34 `drone4d` wheel 依赖声明 | `tools/sdk/drone4d/pyproject.toml` 的必选依赖不含 numpy | 按 Q-M32-10 的 RFC 结论修订；wheel 构建排除 `awr/evaluation/compat/**` | 已同步（AWR-06 v0.3 XR-02：numpy、msgpack、PyYAML 为必选依赖） |
| 32 | CT（待建） | 文件不存在，本文 v0.1 多处"待 CT 确认"无法落地 | CT 建立时以本文 §7.3、§7.2、§6.2.3 为初稿 | 已同步（CT v0.2） |

---

## 14. 评审处理记录

v0.2 对第一轮评审的 29 条意见逐条核实（核实对象为本文 v0.1、并行文档与代码的只读内容，2026-10-18），处理结论如下；另列 1 条评审外补充修订。

| 编号 | 评审位置 | 级别 | 核实结论 | 处理 | 本文修改位置 |
|---|---|---|---|---|---|
| RV-01 | §1.3、§2.2、§7.5 干预执行边界 | 高 | 属实。M29 UC-08"M32 以拦截钩子执行 `cut` 与 `perm`"、R-M29-24"M32 可注册 `DeliveryInterceptor`"、§6 注释"由 M32 实现"；M30 §2.2 写 M32 负责 `Δ_perm` 执行；M33 R-M33-76 的 `MessageIndex` 未出现在本文边界表 | 采纳：执行归属定为 M29 实现与执行、M33 检索载荷、M32 只定义纯数据规格与供体规则并校验；新增 `CutSpec`、`donor_cell`、`check_exec_record`、`ExecRecord`；M29、M30 的修订列入同步清单 | §0 第 7 点、§1.3、§2.2、§6.2.1、§6.6、§7.5、Q-M32-07、§13.2 第 4–7 项 |
| RV-02 | R-M32-36、F-23、V 规则与 S1、S3 指标声明 | 高 | 属实。S3 §8.1 的 `aoi_c_mean_s` 等与 `conserv_ratio`、S1 §7.7 的 `resp_mean_s` 等为不利端无界且未声明可空；F-23 引用的"V 规则错误"在 V-1 至 V-8 中不存在；S1 `jain_fair` 为 `target` 无目标区间 | 采纳：新增 `penalty` 字段与 V-9；`level = scenario` 指标由评测器声明处理规则；S1、S3 修订列入同步清单 | R-M32-89、V-9、F-23、R-M32-36、§13.2 第 14、15、17 项 |
| RV-03 | R-M32-01、V-1、V-2 与 S2 主指标 | 高 | 属实。S2 §7.1、R-S2-02 按场景集各取一个主指标；本文 v0.1 无场景集作用域；M31 §4.3 已允许按场景集覆写 `estimand` | 采纳：新增 `scenario_sets` 与分析组视图，V-1 按视图判定，新增 V-10；`Scoring.view` 只允许覆写 `estimand` | R-M32-88、V-1、V-10、§6.2.1、§13.2 第 9、19 项 |
| RV-04 | `invalid_policy = exclude` 未定义 | 高 | 属实。D3 `build_board` 为 `len(valid) < 0.8 * len(rs) and not dev`；本文 v0.1 的 `exclude` 无阈值、`status = invalid` 无判据；R-M32-77 写"由 RFC 裁定"而 M31 R-M31-61 写"沿用 D3" | 采纳：新增 `invalid_threshold` 与 F-45（有理数精确比较，与 D3 逐例等价），写明 `invalid` 条目的完整语义；`drone4d-event@1` 取 0.2，Q-M32-03 改为已决 | R-M32-38、R-M32-77、F-45、§6.7、§7.3、§8.3、Q-M32-03 |
| RV-05 | R-M32-57、R-M32-59、F-34 与 S1 §6.4、M28 | 高 | 属实。S1 §6.4 的 `tx_bytes`、`tokens` 为提交时拒绝且不记违规；M28 R-M28-69 已按 `budget.policy` 写 `invalid_reasons`，R-M28-71 实测维违规改变 `ranked_valid`；本文 v0.1 重复施加 | 采纳：新增 `enforce ∈ {refuse, violation}` 及缺省；`ranked_valid`/`invalid_reasons` 只由 M28 写入，M32 比对期望值并映射计分；R-M32-59 写出 `refuse` 下的行为 | R-M32-56、R-M32-57、R-M32-59、F-34、§2.2、§13.1 第 26 条、§13.2 第 2 项 |
| RV-06 | `LedgerSummary` 与 M28 结构；R-M32-58 范围 | 中 | 属实。M28 §8 为 `{det{agent{dim:int}}, team{dim:int}, measured{agent{dim:number}}, violations[]}`，维度名为 `energy_uwh`、`tokens_in`、`tokens_out` 等，无 `cpu_source` 与已求值额度；LB §8.4 允许 P0 影子榜用进程内计量 | 采纳：按 M28 结构重写 `LedgerSummary`；`cpu_source`、`budgets` 列为 M28 待增补；统一能量单位（内部 μWh 整数、对外 Wh）与 token 合并规则；R-M32-58 限定为排名且非影子赛道 | §6.2.1、R-M32-55、R-M32-58、§13.2 第 1、3、25 项 |
| RV-07 | R-M32-43 参考缓存键 | 高 | 属实。本文 v0.1、M30 R-M30-65、M31 R-M31-45 三种定义互不兼容；AWR-06 §10.2 与本文 v0.1 相同 | 采纳：本文不再自定义，引用 M31 R-M31-45 的冻结定义；统一提议列入同步清单与反馈；R-M32-46 的对齐校验字段与键逐项对应 | R-M32-43、R-M32-46、Q-M32-08、§13.1 第 30 条、§13.2 第 8、10 项 |
| RV-08 | R-M32-30、R-M32-34、F-20、F-22 参考行支配 | 中 | 属实。参考行不在 F_dom 内，F-20 所需的调整 p 值不存在 | 采纳：新增独立检验族 F_domref（定向单侧、独立 Holm），F-22 改以其判定 | R-M32-96、F-22、R-M32-30、R-M32-34、§13.1 第 29 条 |
| RV-09 | R-M32-15、F-15、摘要第 4 点、反馈第 8 条 | 中 | 属实。经复算，|T| = 3 时 K ≥ 5 即走解析路径；v0.1 所称"K > 18"是按 F_dom 自定 `n_boot` 超上限的条件 | 采纳：更正摘要与反馈第 8 条，新增 §6.3.1 对照表并说明后果 | §0 第 4 点、§6.3.1、§13.1 第 8 条 |
| RV-10 | §9.4 MC-2、MC-10、AC-M32-27 | 高 | 属实。v0.1 未测 F_dom 的族错误率；正态参照在小 S 下偏乐观；MC-2 未说明 `π_ind` 的生成与 Rf 的零假设真值 | 采纳：F_dom 解析路径在 `family` 下改用 Student t 参照；新增 MC-11（正态与 t 对照）；MC-2 写明 `π_ind` 生成与真值；AC-M32-27 纳入 MC-11 | F-14(c)、R-M32-94、MC-2、MC-11、AC-M32-27、Q-M32-09 |
| RV-11 | R-M32-70、F-39 复跑判据 | 高 | 属实（数值依赖评审所设的 Episode 层标准差 0.25；按该假设复算诚实条目失败概率约 0.38）。v0.1 判据只计原估计区间，未计复跑抽样误差 | 采纳：改用预测区间判据（α_r = 0.01）；新增 MC-12；反馈第 22 条提请修订 AWR-06 §8.10 | R-M32-70、F-39、§6.3 算例、MC-12、AC-M32-21、§13.1 第 22 条 |
| RV-12 | F-07、F-08、F-10、F-11 场景索引 | 高 | 属实。v0.1 未规定场景排序、S 的取法与缺失场景的处理，X1 子集与跨预算档的种子来源未定 | 采纳：新增 R-M32-93（S 为声明场景集全集、码点序、缺失只入分母）；F-07 列出 X1（采用 X1 §8.5 的域）、跨赛道、pilot 域；F-32 写明跨档复本对齐；黄金向量增加缺失场景算例 | R-M32-93、R-M32-14、F-07、F-08、F-11、F-32、AC-M32-03 |
| RV-13 | ND-5、ND-8、R-M32-81、R-M32-80 确定性 | 中 | 属实。经核对 CPython 3.12.3，`NormalDist.inv_cdf(0.975)` 走调用 `math.log` 的分支；F-30 的 ln、F-37 的 Φ 与 χ² 分位数亦使用超越函数；ND-8 的"边界差异"豁免与逐字节一致承诺冲突 | 采纳：新增确定性数学函数库 `dmath`（只用 IEEE 基本运算，算法规格 §6.5.1）与十六进制常量（ND-10）；ND-5 列全超越函数位置；废止 ND-8 豁免；χ² 上界随 RV-19 取消 | R-M32-80、R-M32-81、R-M32-102、ND-5、ND-8、ND-9、ND-10、§6.5.1、AC-M32-25、AC-M32-37 |
| RV-14 | R-M32-80、R-M32-84、AC-M32-24/25 与 SDK 依赖 | 中 | 属实。`tools/sdk/drone4d/pyproject.toml` 必选依赖为 `httpx>=0.27`、`msgpack>=1.0`，numpy 只在 `images` 附加依赖中 | 采纳：新增 R-M32-103 与 ND-11；AC-M32-24、AC-M32-25 增加 numpy 1.26 与 2.5.3 对拍；反馈第 23 条；Q-M32-10 | F11、R-M32-103、ND-11、AC-M32-24、AC-M32-25、§13.1 第 23 条、§13.2 第 31 项 |
| RV-15 | R-M32-09、V-2、V-6 与 LB 条目卡 | 中 | 属实。LB §13.3 主指标另加 `p10`、`iqm`；v0.1 无同一指标输出多统计量的机制；`fail_rate` 的读取来源未写 | 采纳：新增 `companions`（主指标缺省 `[iqm, q10]`）与 V-11；`fail_rate` 只读 `failure.policy`；§7.3 写出输出字段；LB 字段名统一列入同步清单 | R-M32-90、R-M32-09、V-11、§7.3、§13.2 第 24 项 |
| RV-16 | §6.2.1 与 M31、S3 字段假设 | 中 | 属实。M31 用 `scoring.rank_mode`、要求 `precision` 与 `supports_subset`；S3 `scoring` 为嵌套结构；X1 用 `seed_bootstrap_percentile` | 采纳：新增 §6.2.3 映射表与 R-M32-91；字段名统一为 `rank_mode`；`MetricSpec` 增补 `precision`、`supports_subset`；`ci.method` 值域统一，废止 `stratified_seed_percentile` | §6.2.1、§6.2.3、R-M32-01、R-M32-05、R-M32-91 |
| RV-17 | R-M32-52、F-31 与 X1 | 中 | 属实。X1 期望 M32 提供 F_rob、时间量的删失聚合与每格 2 个种子的完整性规则，v0.1 未覆盖 | 采纳：新增 R-M32-98（F_rob，双侧）与 R-M32-99、F-43（`p_det`、删失中位数、`min_valid_seeds = 2`）；`stats.families` 增加 `rob`；R-M32-52 改为 X1 子集独立分析组、`fixed_set` | R-M32-52、R-M32-98、R-M32-99、F-43、§7.3、AC-M32-30 |
| RV-18 | R-M32-38 与 S1、S3"记 0"表述 | 中 | 属实。S3 §8.6 写"全部分量记 0"，S1 §6.4、AC-S1-12 写"`U = 0`/计 0"，v0.1 未列需同步的下游文档 | 采纳：新增 §13.2 同步修订清单，列出 S1、S3、LB 相应条款；Q-M32-02 引用 | §13.2 第 13、16、27 项、Q-M32-02 |
| RV-19 | F-37、R-M32-79、反馈第 10 条 σ_D 上界 | 中 | 属实。v0.1 的 F-37 仍用 N − 1 自由度，与本文反馈第 10 条自相矛盾 | 采纳：F-37 改为场景层自助上界（取消 χ²）；新增 MC-13 与 AC-M32-29 覆盖率判据及不足时的处置 | F-37、R-M32-79、MC-13、AC-M32-29、§13.1 第 10 条 |
| RV-20 | R-M32-62、R-M32-64、R-M32-65 FC-4 与 FC-6 | 中 | 部分属实。`family` 口径下 FC-4 的 90% 半宽约 0.06，确不可达；`fixed_set` 口径下半宽约 0.03，等价可达率约 0.6；v0.1 未指明 FC-4 的估计目标，故问题成立。FC-6 取 F_rank 显著对的样本过少，属实 | 采纳：FC-4 固定为 `fixed_set` 并报告可达率，MC-9 增加可达率与 50% 处置线；FC-6 改以独立审计族 F_fid 判定，显著对 < 5 时 `not_run` | R-M32-62、R-M32-64、MC-9、§6.7、§13.1 第 27 条 |
| RV-21 | R-M32-82、AC-M32-26、§9.5 算力 | 中 | 属实。`fixed_set` 下 F_dom 的 ŝ_N 需逐对复本差值（K = 100 时约 1.2×10⁹ 个元素）；§9.5 的耗时无推导；CV-3 的 110% 相对门禁易受负载影响 | 采纳：新增 F-42 闭式方差，解析路径不生成逐对复本；R-M32-82 分列 `family`、`fixed_set`、`all_pairs` 预算；§9.5 耗时标注待测并给出实测计划；CV-3 改为绝对上限与 3 次中位数 | F-42、R-M32-82、AC-M32-26、§9.5、§8.4 CV-3 |
| RV-22 | R-M32-04、R-M32-37、ND-7 行完整性 | 中 | 属实。v0.1 无重复行、冲突行、未知指标、schema 不符的规则，`Row` 无 `attempt`；`subworker` 的重跑只追加成功的一次（D3-INV-19） | 采纳：新增 R-M32-92（`attempt` 归并、四个原因码、行序与分块不变性）与 AC-M32-36；`Row` 增加 `attempt`、`row_schema` | R-M32-92、R-M32-37、§6.2.1、§7.2、§7.4、ND-7、AC-M32-36、§13.2 第 28 项 |
| RV-23 | R-M32-48、R-M32-42 与 M29 R-M29-77 边界情形 | 中 | 属实。`m_s = 1` 时循环移位为恒等映射；`_ix` 控制消息的处理未写；供体缺失未规定 | 采纳：新增 R-M32-97 与 `PermSpec.exempt_channels`；AC-M32-15 增加三种用例 | R-M32-97、§6.2.1、§6.6、AC-M32-15、§13.1 第 28 条 |
| RV-24 | R-M32-45、R-M32-16、F-27 `fixed_set` 下的 `G` | 低 | 属实。Fieller 使用场景间方差，`fixed_set` 下不适用；分母下界的区间方法未写 | 采纳：`fixed_set` 下 `G` 用复本比值 percentile，分母下界用同一复本；`family` 下分母区间取 bootstrap-t；MC-7 覆盖 `fixed_set` | R-M32-45、R-M32-16、F-27、MC-7、AC-M32-14 |
| RV-25 | F-04、F-12、F-24、F-28 定义歧义 | 低 | 属实（四处均可产生不同实现） | 采纳：F-04 在 Episode 层先变换；F-12 改用 `N_j` 并规定 `N_j < 2` 的处理；F-24 的"有效"定义为未记缺失（`exclude` 下另需 `ranked_valid`）；F-28 分母只含两侧均存在的对 | F-04、F-12、F-24、F-28 |
| RV-26 | R-M32-05、R-M32-21、§8.3 `per_metric` | 低 | 属实。v0.1 只写"F_rank 逐指标成族"，`n_boot`、候选集合、Rf 与 D3 基线映射未定 | 采纳：新增 R-M32-100 与 §8.3 规则表、AC-M32-39；F-19 说明候选集合随指标变化的口径 | R-M32-100、F-19、§8.3、AC-M32-39 |
| RV-27 | M33 R-M33-65 数值容差 | 低 | 属实。M33 把等级 2 重放容差交给 M32，v0.1 未提供 | 采纳：新增 R-M32-101、F-44、`replay_tolerance` 与 AC-M32-38；§2.2 标注 M33 为需求方 | R-M32-101、F-44、§2.2、§7.1、AC-M32-38 |
| RV-28 | §7.5、D3-INV 清单、`compat` 位置 | 低 | 部分属实。经核对，`subworker` 的 `valid >= 0.8 * len(rows)` 与 `build_board` 的 `len(valid) < 0.8 * len(rs)` 互为补集，比较边界相同，评审所称"边界不同"不成立；实际差异在于分母与计算行集（`subworker` 不经 `ranked_row` 过滤、以全部行计算指标），清单确未列出；`boardstore._listed` 过滤未列出，属实；`compat` 含 D3 任务语义常量而整包进入 wheel，属实 | 采纳属实部分：新增 D3-INV-19（按核实后的事实表述）、D3-INV-20；规定对拍以冻结原版为准；`compat/` 不进入 wheel；影子重算先施加 `_listed` 过滤 | F7、F9、F18、§2.3、§8.1、§8.2、§8.4 CV-2/CV-4、R-M32-72、R-M32-76 |
| RV-29 | 文档头上游文档与引用约定 | 低 | 部分属实。CT（`docs/d4/契约草案.md`）确不存在，属实；PAPER（`docs/research/d4/r04-首篇论文实验设计.md`）存在，评审所称"`docs/research/d4/` 下没有 r04"不成立；反馈第 4 条的 NumPy NEP 19 引用未经调研报告核实，属实 | 采纳属实部分：标注 CT 待建并规定 CT 建立前本文 §7.3 为统计字段单一来源；PAPER 保留引用；删去 NEP 19 引用，反馈第 4 条论据改为"完全规定的生成器" | 文档信息、§2.2、§7.3、§13.1 第 4 条、§13.2 第 26、32 项 |
| S-1 | 评审外补充：§8.6 比较方向 | — | 本文 v0.1 的 F_rank 候选间比较与 F_dom 以"点估计较优方向"作单侧检验，真零假设下名义水平约为 2α | 修订：区分定向与选向比较，选向比较用双侧 p 值；MC-2、MC-11 报告族错误率；反馈第 21 条 | R-M32-20、R-M32-95、F-41、§6.3.2、§6.4.2、MC-2、§13.1 第 21 条、§13.2 第 21 项 |
