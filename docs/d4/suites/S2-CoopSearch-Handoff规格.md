# S2 CoopSearch-Handoff 规格（协同搜索、目标确认与跨平台交接 Benchmark Suite）

## 文档信息

| 项 | 内容 |
|---|---|
| 文档编号 | AWR-D4-S2 |
| 标题 | S2 CoopSearch-Handoff（协同搜索、目标确认与跨平台交接）Suite 规格 |
| 版本 | v0.2.1（2026-10-18 文档集集成同步：按 AWR-06 v0.3 §0.5 统一裁定 XR-14 补 §7 `G` 在 `fixed_set` 下的区间方法） |
| 日期 | 2026-10-18（UTC） |
| 修订记录 | v0.1（2026-10-18）：首版。v0.2（2026-10-18）：按评审意见 19 条逐条核实后修订，处理记录见 §18。主要变化：主指标收敛为单一 `u_cs`（跟踪权重 `w_t` 按实例声明，搜索场景 `w_t = 0`）；`c_k` 增最小有效窗口 `G_min` 与不可观测区间约束；交接增状态迁移表、语法校验与边界情形，合法性判据改为"要约方为当前持有方且令牌为其当前令牌"；报告配额改为每 Agent 独立额度；`d2` 起高价值目标数按实例抽样；锚点改为整数纳秒精确比对；高度层有界循环复用；网格族增观测适配层、载体时间区间映射、黄金运行与成榜最低条件；新增机巢与能量、X1 宿主接入、规模预算三组需求；通道、Suite 标识与基线标识按并行文档现行版本对齐；新增对并行文档的反馈（§17.2） |
| 状态 | D4 设计评审稿。D4 处于深入调研与概念设计阶段，实现在 D3 封板（G0）之后启动；本文全部周次以"D3 封板后第 N 周"表述，数值标注"初值"者为待 pilot 校准的设计缺省，不是对现有代码或性能的断言 |
| 上游 | [AWR-06 D4 协同实验平台概念设计](../../06-D4-协同实验平台概念设计.md) v0.2（§1.4、§1.7、§3.4、§4、§5、§6.7、§7.0、§7.2、§7.4、§8.3–§8.8、§9、§10；ADR-D4-03、11、12、14、15、16、17、21、22、23、24、26、33）；调研 [r02 协同问题族与标准问题库](../../research/d4/r02-协同问题族与标准问题库.md)（§2、§3.4、§3.6、§6、§7）、[r03 NS-2 经验与排行榜规则](../../research/d4/r03-NS-2经验与排行榜规则.md)、[n01 现有代码映射](../../research/d4/notes/n01-现有代码映射.md)、[n02 D3 评测子系统泛化](../../research/d4/notes/n02-D3评测子系统泛化.md)、[n03 外部模型讨论纪要](../../research/d4/notes/n03-外部模型讨论纪要.md)（§2.9、§3.4）；负责人前期稿 design-r1（`/data/projs/anet-drone-data/design-r1.md`）、design-r2（`/data/projs/anet-drone-data/design-r2.md`，§五 CoopSearch 行） |
| 并行文档 | [M28 实验内核](../../modules/M28-实验内核PRD.md)、[M29 交互网络](../../modules/M29-交互网络PRD.md)、[M30 协同接口与基线](../../modules/M30-协同接口与基线PRD.md)、[M31 基准套件](../../modules/M31-基准套件PRD.md)、[M32 评测与统计](../../modules/M32-评测与统计PRD.md)、[M33 Trace 与复现](../../modules/M33-Trace与复现PRD.md)、[M34 研究者 SDK 与命令行](../../modules/M34-研究者SDK与命令行PRD.md)、[S1](S1-TaskAlloc规格.md)、[S3](S3-InfoShare规格.md)、[X1](X1-RobustCoord协议.md)、[协作智能排行榜规格](../协作智能排行榜规格.md)（下称 LB）、[契约草案](../契约草案.md)（下称 CT）、[r04 首篇论文实验设计](../../research/d4/r04-首篇论文实验设计.md)（下称 PAPER）。并行文档的引用以章节主题表述，字段名以 CT 定稿为准 |
| 标识 | Suite `suite_id = coopsearch-handoff`（与 M34 发布矩阵一致，满足 M31 R-M31-02 的模式），Python 包目录 `coopsearch_handoff`（M31 §7.1），Suite 级随机域前缀 `suite.coopsearch-handoff.`（M31 R-M31-98）；报告与消息 schema 沿用 `coopsearch.` 前缀（M30 消息表），评测器标识 `coopsearch@1` |
| 编号体例 | 需求 `R-S2-yy`，验收 `AC-S2-yy`，风险 `RK-S2-yy`，待决 `Q-S2-yy`；工作包引用 AWR-06 的 `WP-D4-xx`，决策引用 `ADR-D4-xx`，平台验收引用 `AC-D4-xx`。AWR-06 §7.2 的 AC-S2-1 至 AC-S2-6 在本文以 AC-S2-01 至 AC-S2-06 承接，含义不变（§11 对照） |
| 引用约定 | 代码路径相对仓库根 `/data/projs/anet-drone`。前缀 `py:` = `python/awr/`，`sdk:` = `tools/sdk/drone4d/src/drone4d/`，`ct:` = `packages/contracts/`。对现有代码的陈述以 2026-10-18 本稿核对的工作副本为准，引用以"文件 + 符号"为准；文献只引用调研报告已列条目并保留其核实标注 |
| 称谓 | 下一大版本统一称 D4；D3 指已在收尾封板的 v0.3.0 |

---

## 0. 摘要

1. **研究问题**。S2 评测"多个平台在受限通信下协同搜索未知位置目标、确认目标身份并维持对高价值目标的持续跟踪"的能力。它在 S1（任务公开、无需搜索）之前补上"信息尚未获得"的环节，与 S3（信息融合）共同构成首批"分配—搜索—融合"三环（r02 §7）。
2. **两个场景族**。`cs-planar`（P0）：平面多边形世界、`planar@1` 物理档、内核平面 `L1` 检测模型，承担主榜与问题族泛化估计；`cs-handoff-mesh`（P1）：D3 crop 网格世界包与 Canonical Event Tape、`kinematic` 物理档、经观测适配层接入的 D3 `L1Model`，承担跨保真度审计（FC-3 至 FC-7，AWR-06 AC-D4-27）与固定场景集估计；满足 §4.3 成榜最低条件前只作审计、不成榜。两族共用指标定义与评测规则。
3. **与 D3 crop-handoff 的承接**。D3 的 crop-handoff 事件任务以 Suite `drone4d-event@1` 原样延续，EC/EU/AO/CE 指标、`board.v1` 与 B-07 回归门不变；S2 承接其世界包、事件磁带、匿名检测与证据审核机制、报告守卫与 `track_share` 协议思想，但不承接其任务语义。两者"handoff"一词含义不同：D3 的 `rendezvous_handoff` 是被观测对象之间的人员交汇与交接事件，S2 的 Handoff 是观测平台之间的跟踪交接（§2.2）。
4. **主指标**。Suite 只有一个主指标 `u_cs`（AWR-06 §7.0、M31 R-M31-04）：`U_CS = (1/|K_hv|) Σ_k u_k`，`u_k = (1 − w_t) s_k + w_t c_k`，`s_k = 1[τ_k ≤ T_c] · (1 − τ_k / (2T_c))`，`τ_k` 为高价值目标 k 的首次有效确认生效时刻，`c_k` 为跟踪持有方在确认后 60 s 窗口内的跟踪覆盖率。`w_t` 由实例声明：无跟踪规则的实例（`d0`–`d2`）`w_t = 0`，式子退化为纯搜索形式；含跟踪规则的实例（`d3` 起）`w_t = 0.5`，在预注册中依研究问题取定，pilot 只做敏感性检查。可观测时间不足 `G_min` 的目标只按 `s_k` 计分。取值 [0, 1]，虚报不做线性扣分，以每 Agent 报告配额与速率限制约束。
5. **确认与交接的可审计定义**。有效确认要求报告位置在门限内、证据多数表决归属该目标、含识别级证据（`d3` 起须为长焦角色），沿用 D3 证据审核的思想但在 D4 内自含实现；交接以"跟踪持有表"（custody）为真值层对象，合法性判据为"要约方是当前持有方且引用其当前令牌"，只在真值层生效，对 Agent 可见的结果与合法性无关，避免形成免费真值查询（AWR-06 §8.8 A-01）；与 M29 R-M29-73 现行文本一致，状态迁移与边界情形见 §3.7。
6. **难度与规模**。`d0`–`d2` 在 P0 冻结，`d3`（广角/长焦异构、交接、警觉逃逸、机巢与能量）在 P1 开放，`d4` 在 P2 开放；`cs-planar` 按恒密度规则缩放（`n4`–`n16` 于 P0），`cs-handoff-mesh` 为固定世界、固定规模标签（`n4`、`n8`）。难度档只定义实例内在旋钮，网络档由赛道决定（与 M31 OR-4 一致）。
7. **基线**。7 个基线加 `π_random`：`independent_lawnmower@1`（`π_ind`）、`partition_lawnmower@1`、`shared_visited_greedy@1`、`frontier_ig@1`（PA-2 代表性协同基线）、`voronoi_lloyd@1`、`track_share_cs@1`（`d3` 与网格族，P1）、`central_belief@1`（`π_central`）、`random@1`（`π_random`）。除 `track_share_cs@1` 与公共 `custody` 构件外，标识与实现归属与 M30 §6.7.3、§6.7.0 一致；`track_share_cs@1` 为本文新增要求：D3 SDK 的 `track_share` 不产生目标确认报告，原样投影在 S2 中无法得分（§8.3），M30 §6.7.3 表的对应修订见 §17.2。
8. **规模**。需求 76 条（R-S2-01 至 R-S2-76），验收 24 条（AC-S2-01 至 AC-S2-24），实例样例 3 个（`d0` 锚点含整数纳秒期望值、`cs-planar` `d2/n8`、`cs-handoff-mesh` 基于 `crop-handoff-001`），对 AWR-06 的反馈 27 条（§17.1），对并行文档的反馈 19 条（§17.2），评审处理记录 19 条（§18）。

---

## 1. 定位与范围

### 1.1 研究问题与问题族归属

| 项 | 内容 |
|---|---|
| 问题族（r02 编码） | CS 协同覆盖与搜索为主；`d3` 起叠加 TR 协同跟踪中的"持续跟踪与跨平台交接"子问题（r02 §3.4、§3.6） |
| 研究问题 | (1) 搜索：在先验 `b_0(x)`、遮挡、检测概率随距离衰减与虚警下，团队如何分配搜索努力使高价值目标尽早被发现；(2) 确认：检测与身份确认分离时（广角可检测、近距或长焦方可确认），团队如何调度确认动作并控制虚报；(3) 交接：目标移动、平台能量与视场受限时，团队如何以显式交接维持对目标的连续责任跟踪；(4) 通信：上述三者在受限通信、分区与丢包下的退化方式 |
| 与 S1 的关系 | S1 的任务公开、无需先搜索（AWR-06 §7.1），S2 的目标位置未知、须先发现；二者正交 |
| 与 S3 的关系 | S3 只评融合与通信、运动不受控；S2 的运动与感知动作是被测对象。S2 中的检测转发与目标状态共享属于 S3 的研究对象在闭环中的体现，S2 不对融合一致性评分 |
| 与 X1 的关系 | X1 RobustCoord 按其 §2.3 适用矩阵施加于 S2：F（节点静默失效）、C（失联）、P（网络分区）、L（链路退化）、J（延迟尖峰）、S（传感器退化）为 P1 首批，M（成员变化）于 P2，H（机巢断电）在 P2 前不适用；宿主接入项见 §9.4 |
| 研究方向支撑 | 方向 A（规模与协作增益曲面，E8）；方向 B（检测转发、已搜栅格、意图三类 `info_class` 的最小充分信息）；方向 C（多机目标信念分歧，经自愿 Belief 通道）；H5 跨保真度假设以 `cs-handoff-mesh` 为对象（AWR-06 §9） |

### 1.2 场景族总览

| 场景族 | 世界 | 物理档 | 观测档 | 外生事件源 | 估计目标 | 阶段 | 工作包 |
|---|---|---|---|---|---|---|---|
| `cs-planar` | 平面多边形区域，含遮挡障碍、禁入区 | `planar@1`（主赛道排名档） | 内核平面 `L1`（M28 R-M28-59，由本文 §3.4 覆盖参数） | 实例脚本（目标轨迹、分区计划） | `family` | P0（`d0`–`d2`）；P1（`d3`、`n32`）；P2（`d4`、`n64`） | WP-D4-16、51、52 |
| `cs-handoff-mesh` | D3 crop 网格世界包 `oryahovo-crop1` | `kinematic`（`KinematicTier`）；`realistic` 只用于 `L2` 抽样审计 | D3 `L1Model` 经观测适配层 `mesh_obs_adapter@1`（`hosts/hifi`）；`L2` 审计 | D3 Canonical Event Tape（脚本演员、背景人群、环境轨、通信区） | `fixed_set` | P1 | WP-D4-25、27 |

### 1.3 范围与非目标

| 编号 | 非目标 | 理由或去向 |
|---|---|---|
| NG-S2-01 | 不评测 D3 事件理解任务（交汇、上车、进楼等事件的报告与匹配） | 由 `drone4d-event@1` 原样承载（AWR-06 §1.7） |
| NG-S2-02 | 不对多目标跟踪精度（OSPA、HOTA、MOTA）评分 | 属 CoopTracking 储备 Suite（AWR-06 §7.5，P2） |
| NG-S2-03 | 不对融合一致性（ANEES）评分 | 属 S3 |
| NG-S2-04 | 不在 P0 交付网格世界族与 `L2` 档 | AWR-06 §1.6 第 3 条；ADR-D4-15 |
| NG-S2-05 | 不建模目标外观的视觉识别（`cs-planar`） | 平面族以"识别级检测 + 签名混淆概率"抽象识别；视觉识别只在网格族 `L2` 审计中出现 |
| NG-S2-06 | 不以线性扣分惩罚虚报 | AWR-06 §7.0 主效用约定；以报告配额与速率限制替代（§3.6） |

### 1.4 术语（本文新增）

| 术语 | 定义 |
|---|---|
| 高价值目标 `K_hv` | 须被确认的目标集合；主指标只对其计分 |
| 干扰目标 `K_d` | 真实存在、可被检测、但不属于 `K_hv` 的对象，用于诱发错误确认 |
| 检测 | 一次传感器采集中对某对象（或虚警）的匿名观测，带噪位置与内核签发的 `det_id` |
| 识别级检测 | 满足识别条件的检测：`cs-planar` 为平面距离（传感器与对象位置的水平欧氏距离，下同）≤ `r_id` 的检测，携带签名属性；`cs-handoff-mesh` 为 D3 `L1Model` 判定可识别（无遮挡投影高度 ≥ 16 px 且可见比例 ≥ 0.5，`py:agentsdk/env/l1.py` `L1Params.ident_px`、`ident_vis`）、因而输出 `appearance` 字段的检测 |
| 确认报告 | `ReportRequest`，`report_schema = coopsearch.confirm.v1`，声明某位置存在一个高价值目标并附证据 |
| 有效确认 | 经评测器按 §3.6 判定成立的确认报告；对某目标的第一次有效确认的生效时刻记为 `τ_k` |
| 跟踪持有（custody） | 真值层对象：在任一时刻，对每个已确认高价值目标至多有一个持有方 Agent；持有方由首次有效确认建立，只经被授予的合法交接转移 |
| 持有令牌 | Agent 侧对"本 Agent 认为自己持有某跟踪"的引用，只有两种形式：本 Agent 某条确认报告的引用 `<agent_id>@r<seq>`，或本 Agent 某次 `handoff.accept` 操作的 `op_id`（`<agent_id>@<op_seq>`）。令牌本身不表明真值层持有是否成立；真值层为每个已确认目标维护唯一的当前令牌 `tok_k(t)`（§3.7） |
| 交接 | `handoff.offer` → `handoff.accept` 交互（AWR-06 §6.7；M29 R-M29-73），合法时把持有方从要约方转移到接受方 |
| 可观测区间 `O_k` | 目标 k 在世界中可被任一传感器观测的时间并集：在场（磁带状态 moving 或 waiting）且不在建筑内；登车后以载体继承（§3.3） |
| 确认截止 `T_c` | 有效确认计分的截止时刻；无跟踪规则的实例等于 `horizon`，含跟踪规则的实例（`d3` 起）为 `horizon − W_trk` |
| 最小有效窗口 `G_min` | 跟踪窗口与可观测区间交集的最短时长（初值 10 s）；交集短于 `G_min` 的目标不计 `c_k` 项，只按 `s_k` 计分（§3.7） |
| 载体 | 网格族中被高价值目标登乘的车辆演员；登乘区间内该车辆的检测归属该目标（§3.6 V1） |
| 角色 | `wide`（广角搜索）与 `tele`（长焦确认）；`d0`–`d2` 全部为 `wide` 且 `r_id > 0` |

### 1.5 代码与资产事实核对

本表只列本文规则直接依赖的事实，均已在工作副本中打开核对。

| 编号 | 事实 | 位置 | 本文用途 |
|---|---|---|---|
| CF-01 | `crop-handoff-001`：`event_type = rendezvous_handoff`，`world = oryahovo-crop1`，360 s，`mode = exogenous`，3 名脚本演员（`person_A`、`person_B`、`vehicle_C`）与 40 名背景行人；关键事件 `encounter_AB`（195–250 s）、`board_BC`（240–275 s）、`enter_AZ`（290–320 s）；`person_B` 于 255 s 登车，`person_A` 于 290 s 进入 Z 楼，`vehicle_C` 于 330 s 驶离；简报只给出 `person_A` 的外观与 2 个公开锚点 | `configs/events.d3/crop-handoff-001/event.yaml` | §2、§12.3 |
| CF-02 | 任务区锚点 `ev_town_center` 为 310 m × 280 m 矩形（x ∈ [−115, 195]，y ∈ [−110, 170]） | `configs/events.d3/crop-handoff-001/anchors.json` | 网格族区域与规模 |
| CF-03 | D3 原型榜运行规格：3 架 `x500`、`physics = realistic`、`obs_level = L1` 或 `L2`、`comm.model = fspl-nlos@1`（`bucket_Bps = 125000`、`msg_max_B = 4096`）、种子 1–5、`ranked = true`；起降点 (76, −78)、(76, −82)、(76, −86) | `configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json`、`…l2-3uav.run_spec.json` | 网格族起始位姿 |
| CF-04 | 任务契约：高度包络 [30, 120] m AGL；人员与车辆安全间距各 10 m；预算每机 10 km、2,000 kB；报告类型映射 `REPORT_OF` 含 `target_found → found` | `py:agentsdk/task.py` `task_from_event`、`REPORT_OF` | 网格族安全与 legacy 报告映射 |
| CF-05 | `task.v1.event_type` 枚举为 `reid_occlusion`、`rendezvous_handoff`、`vehicle_track`、`target_search`；工作副本的 Python 代码中未见 `target_search` 的事件生成实现（只在契约与生成类型中出现）；M23 将其列为 P0b 交付 | `ct:bench/task.v1.schema.json`；`docs/modules/M23-事件运行时PRD.md` §15 | 网格族实例来源（§4.2） |
| CF-06 | `L1Model`：每帧匿名 `det_id`（键含帧序号），检测列表不携带演员序号；可识别条件 `h_unocc ≥ 16 px` 且 `fv ≥ 0.5` 时给出带噪外观；斜距上限 400 m；随机键 `(seed, 21, uav_no·65536 + actor_no, seq, ch)`；真值侧记录 `det_id → actor`、无遮挡像素高度、可见比例、是否可识别 | `py:agentsdk/env/l1.py` `L1Model.frame`、`L1Params` | 网格族观测与证据解析 |
| CF-07 | 证据多数表决：L1 按 `det_id` 经检测真值映射，L2 按同帧可见演员框 IoU ≥ 0.3；平票为未解析；EU 匹配在函数内导入 `scipy.optimize.linear_sum_assignment` | `py:bench/eval/eu.py` `resolve`、`event_understanding`；`py:bench/eval/thresholds.py` `TH.eu_iou` | §3.6 证据规则 |
| CF-08 | 报告守卫：类型不在任务契约中拒绝（663 `report_type`）；引用本机未收到的检测拒绝（663 `evidence_unknown`）；转述他机证据放行，由评测端按该机交付记录解析 | `py:agentsdk/env/guard.py` `ActionGuard._known` | §3.6 转述证据 |
| CF-09 | `track_share`：`sight`/`claim`/`reported` 三类广播；声明方丢失后"最近空闲机"接力；报告后广播抑制同类型 30 s | `sdk:baselines/track_share.py` `TrackShare` | §8.3 |
| CF-10 | SDK 基线的报告规则只有 `meet`、`board`、`enter_building` 三类，不产生 `found` 报告；目标匹配以上衣颜色 RGB 欧氏距离 < 45 | `sdk:baselines/common.py` `BaselineAgent.reports`、`target_tracks` | §8.3、反馈 4 |
| CF-11 | `coverage_scan`：任务区外接框沿 x 按机数切条带，割草机航线，航带间距按地面足迹 70% 重叠，分区固定、不通信 | `sdk:baselines/coverage_scan.py` `CoverageScan` | §8 |
| CF-12 | 航带生成与扫描角搜索（`best_sweep_angle`、`lanes_for_angle`、`boustrophedon`）；多机等代价切分 `split_balanced` | `py:swarm/coverage/lanes.py`、`py:swarm/coverage/partition.py` | §8 |
| CF-13 | 事件运行时在 P0a 拒绝 `interactive` 模式（原因码 611） | `py:sim/events/runtime.py` | 网格族不开放 `interactive`（§4.2） |
| CF-14 | D2 识别物感知态 UNAWARE/SUSPICIOUS/REACTING/ALERTED/CALMING 与 `flee` 响应；该包含 numba 核（`kernels_motion.py`、`kernels_los.py` 等）并经 `awr.contracts._paths.contracts_root` 读取仓库契约 | `py:sim/targets/behavior.py`、`py:sim/targets/model.py` | 警觉逃逸规则自含实现（§3.3、反馈 8） |
| CF-15 | 磁带时间定点 `TICK_NS = 100 ms`；`TapeReader.comm_zones_at`、`actor_states`、`actors_info`；读取路径在函数内导入 `zstandard` | `py:sim/events/tape.py` | 网格族外生事件与通信区 |
| CF-16 | 网格查询类为 `MeshWorldQuery`（继承 `DsmWorldQuery`）；三维运动学档为 `KinematicTier`；D3 主循环为 `BenchEnv` | `py:world/mesh/query.py`、`py:phys/kinematic.py`、`py:agentsdk/env/core.py` | 网格族后端 |
| CF-17 | `CameraModel` 缺省 1920 × 1080、HFOV 60°、10 Hz、`model_ref = d4d_eo1080@1`，`fx = (width/2)/tan(HFOV/2)` | `py:agentsdk/env/camera.py` | 网格族角色参数 |
| CF-18 | 机型目录中 EO-Z8 变焦相机 HFOV 64.6°–9.10° | `docs/20-机型与传感器目录.md` §6.2 | 网格族长焦角色的参考取值 |
| CF-19 | `L1Model.frame` 的 Agent 可见检测字典字段为 `det_id`、`sensor_id`、`seq`、`t_mid_ns`、`class`、`bbox_xywh`、`h_px`、`slant_range_m`（带 2% 相对噪声）、`ground_xy_noisy`，可识别时另含 `appearance{top_rgb, bottom_rgb?}`（RGB 噪声 σ = 12）；地面位置噪声标准差 `sig0_m + sig_per_m · R`（0.4 m、0.008）并叠加导航误差；不输出 `ident`、`sig`、`cov_xy`；`_appearance` 对车辆取 `body_rgb` 为 `top_rgb` | `py:agentsdk/env/l1.py` `L1Model.frame`、`L1Params`、`_appearance` | 网格族观测适配层（§3.4） |
| CF-20 | `crop-handoff-001` 中 `vehicle_C` 外观为 `{preset: sedan, body_rgb: [210, 210, 214]}`，150 s 出现、235 s 起在路缘等候 30 s、330 s 驶离；`person_B` 于 255 s 执行 `board`；简报只描述 `person_A` | `configs/events.d3/crop-handoff-001/event.yaml` | 载体映射与简报披露（§3.3、§3.6） |
| CF-21 | 并行文档现行版本：M28 R-M28-106 的 `initial_state` 含 `vel0_mps`（缺省 `[0, 0]`）；M28 R-M28-53 积分与 `d4_std` 的 Δp = 20 ms；M29 R-M29-73 与 §6.6.2 已规定交接合法性只写真值层、交接不用任何 `DENIED_*`、接受通知经网络承载且通知丢失时要约方见 `EXPIRED`；M31 R-M31-41 通道词表为 `tasks`、`tape`、`ident`、`net`、`agent_state`、`belief` 六个（报告与交互记录经 `tasks`，账本经 `RunView.ledger()`），M33 R-M33-69 与 §6.9 为九个（另含 `reports`、`interaction`、`ledger`）；M31 OR-7 与 V-S-15 规定固定型 Family 以至少 2 个黄金运行代替锚点；M30 §6.7.3 表只有 `track_share@1`（legacy），§6.7.0 无 `custody`，而 M30 摘要第 6 条与 P1 范围行已列 `track_share_cs` 与 `custody` | `docs/modules/M28-实验内核PRD.md`、`M29-交互网络PRD.md`、`M30-协同接口与基线PRD.md`、`M31-基准套件PRD.md`、`M33-Trace与复现PRD.md` | §3.7、§4.1、§8、§12.1、§17.2 |

---

## 2. 与 D3 crop-handoff 事件任务的承接关系

### 2.1 承接原则

| 编号 | 原则 |
|---|---|
| CP-1 | D3 不回归：`drone4d-event@1` 由 `BenchEnv` 原样运行，`board.v1` 冻结只读，B-07 回归门持续守护；S2 的任何工作包不修改 D3 冻结区（B-08） |
| CP-2 | 承接资产与机制，不承接任务语义：S2 网格族复用世界包、事件磁带、匿名检测、证据审核思想与基线协议，但其任务（搜索、确认、持续跟踪）与评分（`U_CS`）独立于 D3 的 EC/EU/AO/CE |
| CP-3 | 只扩不改：S2 所需的报告 schema、交接交互与持有表进入 `ct:coord/`、`ct:suite/` 新契约，D3 契约原位不变（B-06） |
| CP-4 | 依赖边界：S2 包不 import `awr.bench`；网格族对 D3 构件的 import 只在 `kernel/hosts/hifi/` 下且限于 B-10 白名单 |
| CP-5 | 指标不换算：S2 不把 EC/EU/AO/CE 换算为 `U_CS`，也不把 `U_CS` 报告为 D3 指标；二者只在条目卡上分栏并列 |

### 2.2 名称辨析：两种"handoff"

| 维度 | D3 `rendezvous_handoff`（crop-handoff） | S2 Handoff |
|---|---|---|
| 交接的主体 | 被观测对象（人员之间交汇、人员登上车辆） | 观测平台（Agent 之间转移对目标的跟踪责任） |
| 任务形态 | 主动观测既定事件并以报告描述事件（`meet`、`board`、`enter_building`） | 发现未知位置目标、确认身份、维持持续跟踪 |
| 真值对象 | 事件标注（类型、参与者、区间） | 目标轨迹、检测真值映射、跟踪持有表 |
| 计分 | EC、EU、AO、CE 四项逐项排名（D3 ADR-191） | 单一主效用 `U_CS` |
| 平台交互 | 无显式交接操作；`track_share` 以广播声明隐式接力 | `handoff.offer`/`handoff.accept` 显式交互，经网络承载并由仲裁器记录 |

文档、契约与界面文案中，S2 的交接一律写作"跨平台交接"或"跟踪交接"，D3 的事件类型写作"人员交汇与交接"，避免混用。

### 2.3 承接矩阵

| D3 元素 | 位置 | S2 处置 | S2 落点 | 阶段 |
|---|---|---|---|---|
| crop 世界包 `oryahovo-crop1` 与网格查询 | `py:world/mesh/query.py` `MeshWorldQuery` | 原样复用为网格族世界与视线判定；只在 `hosts/hifi` | §4.2；R-S2-53 | P1 |
| Canonical Event Tape（脚本演员、背景人群、环境关键帧、通信区） | `py:sim/events/tape.py` `TapeReader` | 原样复用为外生目标运动、干扰对象、天气与通信区来源；`comm_zones_at` 进入 `abstract-radio@1` 分区掩码（AWR-06 §6.5 第 7 项） | §4.2；R-S2-54 | P1 |
| 简报公开锚点与目标外观 | `event.yaml` `brief`；`py:agentsdk/task.py` `brief_for_agent` | 重新投影为 S2 简报：公开锚点生成目标先验，`K_hv` 的外观由 S2 实例声明（可多于 D3 简报所列目标） | §6.3；R-S2-55 | P1 |
| `target_search` 事件的搜索先验（`search.prior`，GMM 或栅格） | M23 §15（P0b） | D3 封板时若已交付，直接作为网格族的 `target_prior` 与目标位置抽样来源；未交付时网格族只用 `rendezvous_handoff` 类事件 | §4.2；Q-S2-01 | P1 |
| `L1Model` 匿名检测与真值侧记录 | `py:agentsdk/env/l1.py` | `L1Model` 本体不修改，其输出经观测适配层 `mesh_obs_adapter@1` 映射为 S2 统一检测字段后作为网格族 `L1` 观测；遗留随机键流 21 不经 BLAKE2b 派生（RN-7） | §3.4；R-S2-56、73 | P1 |
| 证据多数表决 | `py:bench/eval/eu.py` `resolve` | 在 S2 评测器内自含重写同一规则（B-10 不允许 S2 import `awr.bench`），以共享测试向量对拍 | §3.6；R-S2-57 | P0（规则）/ P1（网格族对拍） |
| 报告守卫与转述证据规则 | `py:agentsdk/env/guard.py` `ActionGuard._known` | 语义沿用：自身证据须确实收到；转述证据按产生方交付记录解析（M28 R-M28-63） | §3.6 | P0 |
| `track_share` 协议（目击、声明、接力） | `sdk:baselines/track_share.py` | 作为 `track_share_cs@1` 的协议来源，增补确认报告与显式交接；原实现不修改 | §8.3；R-S2-49 | P1 |
| `coverage_scan` 分区割草机 | `sdk:baselines/coverage_scan.py` | 思想进入 `partition_lawnmower@1`（实现复用 `py:swarm/coverage/`） | §8 | P0 |
| CE 的 `Ĝ`（全编队对最佳单机） | `py:bench/eval/ce.py` `collaborative` | 不进入 S2；S2 协作诊断使用 Δ 族（AWR-06 §8.5），`Ĝ` 归入"扩容收益"类 | §7.5 | — |
| `fspl-nlos@1` 链路模型 | `py:agentsdk/env/link.py` `LinkModel` | 不用于 S2；S2 网格族使用 `abstract-radio@1`（NLoS 判定复用 `py:world/` 查询，AWR-06 §6.5 第 1 项） | §9.1 | — |
| 安全规则（演员最小间距、穿越即无效） | `py:bench/eval/safety.py`；`task.v1.safety` | 语义沿用并在 S2 评测器内自含实现：人员与车辆 10 m 间距计违规时间；穿越演员包围体 `ranked_valid = false` | §7.4；R-S2-45 | P1 |
| 三机 `x500` 起降点 | CF-03 | 作为网格族起始位姿样例；新增起降点须按同一判据重新核验 | §12.3 | P1 |

### 2.4 不承接项

| D3 元素 | 不承接的理由 |
|---|---|
| EC/EU/AO/CE 评测器与 `eval_thresholds.v1` | D3 任务指标，保留于 `drone4d-event@1`（AWR-06 §1.7 第 1 项）；B-08 冻结 |
| `board.v1` 的 `lanes[].event_type`、逐项排名 | `board.v1` 冻结；S2 只在 `board.v2` 中登记 |
| `realistic` 为唯一正式成绩档的规则（D3 ADR-177、`run_spec.v1.x-rules` 的 `ranked_physics`） | 只对 `drone4d-event@1` 生效（AWR-06 §8.12 D-7、ADR-D4-15） |
| `step_budget_s` 超时沿用上一指令 | 开发档行为；D4 评测宿主以屏障与建模时延替代（AWR-06 §5.7 W-1 至 W-3） |
| `interactive` 事件模式 | D3 事件运行时在 P0a 拒绝该模式（CF-13）；网格族在 D3 交付前不开放 |

### 2.5 回归约束

S2 的全部工作包合入后须满足 AC-S2-06（承接 AWR-06 AC-S2-6）：`drone4d-event@1` 下 crop-handoff 全部封板基线 × 封板种子的运行、评测与 `board.v1` 重算与封板基线逐字节一致。S2 网格族对 D3 构件的使用以只读库调用实现，不得改变 D3 构件的随机键、缓存行为与输出（RN-7、AWR-06 §6.9）。

---

## 3. 形式化定义

### 3.1 实例

实例 `I = (W, A, K, M, S, N_ex, R, H)`：

| 要素 | 内容 |
|---|---|
| `W` | 世界：区域多边形 `Q`、障碍集合（每个障碍声明是否阻挡运动、是否阻挡视线）、禁入区（阻挡运动、不阻挡视线）、机巢 |
| `A` | 实体与 Agent 模板：`n` 个平台实体，角色 `wide` 或 `tele`，限速 `v_max`、限加速度 `a_max`、高度层、能量预算；每个 Agent 控制 1 个实体（P0） |
| `K` | 目标：`K = K_hv ∪ K_d`，每个目标的运动模型与轨迹（外生）或反应参数（`interactive`）；先验 `b_0(x)` 公开 |
| `M` | 事件模式 `exogenous` 或 `interactive`（标签，进入 `scenario_id`，M31 OR-5） |
| `S` | 传感：每个角色的传感器模型参数（§3.4） |
| `N_ex` | 外生通信特征：计划分区 `partitions[]`、通信区 `comm_zones[]`（不含网络档，M31 OR-3） |
| `R` | 规则：报告门限 `g`、速率限制、每 Agent 报告配额、跟踪窗口 `W_trk`、最小有效窗口 `G_min`、交接窗口、确认角色要求、机巢与能量参数（`d3` 起） |
| `H` | 时间：`horizon_s`、确认截止 `T_c` |

目标：在资源包与安全约束下最大化 `U_CS`（§7.1；跟踪权重 `w_t` 由实例声明）。

### 3.2 世界与区域

- `cs-planar`：`PlanarWorld`（M28 R-M28-48）；障碍为简单多边形，`blocks_motion` 与 `blocks_los` 分别声明；禁入区为 `blocks_motion = true`、`blocks_los = false` 的多边形，实体无法进入（M28 R-M28-54），因而不存在禁入区侵入违规。视线判定为二维线段相交（`los_rule = 2d`，M28 R-M28-50）。
- `cs-handoff-mesh`：`oryahovo-crop1` 网格世界经 `MeshWorldQuery` 求视线；区域为实例声明的多边形（缺省 `ev_town_center`）；高度包络 [30, 120] m AGL（CF-04）。

**遮挡率** `o`：在区域内均匀布置的评测栅格点对（`cs-planar` 取栅格边长 `r_s / 4` 的点阵中、距离不超过 `r_s` 的点对）中，视线被阻挡的比例。由 `Generator.difficulty` 计算并写入实例 `difficulty{}`。

### 3.3 目标与运动

| 运动模型 | 定义 | 随机域 | 档位 |
|---|---|---|---|
| `static` | 位置固定 | `instance`（位置抽样） | `d0`–`d1` |
| `random_walk` | 局部航点游走（参数 `v_tgt`、`leg_s`，`leg_s` 即 `Δ_w`，初值 10 s）。第 j 个航点 `w_j` 在以当前位置 `x(t_j)` 为圆心、半径 `R_w = v_tgt · leg_s` 的圆盘与可行区域之交上均匀抽取（极坐标 `r = R_w √u_1`、`θ = 2π u_2`）；抽中点位于不可行区域或线段 `x(t_j) → w_j` 与阻挡运动障碍相交时，以同一航点序号的下一对 draw 重抽，至多 32 次，仍失败则原地停留 `leg_s` 后进入下一航点序号。目标以 `v_tgt` 沿直线移动，到达 `w_j` 的时刻 `t_{j+1} = t_j + ‖w_j − x(t_j)‖ / v_tgt` 立即抽取下一航点；位置在 100 ms 定点网格上按闭式插值 | `target`（`object_id = target_id`，`time_index` = 航点序号 j，`draw_index = 2·attempt + {0, 1}`） | `d1`（慢速）起 |
| `exogenous_path` | 实例或事件磁带给出的轨迹，按 100 ms 定点插值 | 无（数据给定） | `d2` 起；网格族全部 |
| `evade`（`interactive`） | 在 `random_walk` 或 `exogenous_path` 之上叠加警觉逃逸：任一平台在 `r_alert` 内且视线通过持续 ≥ `t_alert`，目标进入 ALERTED，背离最近平台以 `v_flee` 移动 `t_flee` 后平复并恢复原运动 | 独立域 `suite.coopsearch-handoff.react`（`object_id = target_id`），不使用 `target` 域 | `d3` 起，只在 `cs-planar` |

规则：

1. `exogenous` 模式下全部目标轨迹由实例与 `target` 域决定，与策略无关，跨策略严格配对（RN-2）；Δ 族的因果解释只在此模式成立（AWR-06 §8.5"适用范围"）。
2. `interactive` 模式的反应只改变目标运动，不改变初态、外生驱动与反应参数；反应参数以独立域派生，使 `exogenous` 部分在两种模式之间保持一致。警觉逃逸规则在 S2 包内以 numpy 自含实现，语义参照 D2 感知态的 ALERTED 与 `flee` 响应（CF-14），不 import `py:sim/targets/`。
3. 载体继承（网格族）：脚本演员经磁带 `board` 操作登上车辆后，在登乘区间 `[t_board, t_alight)`（未下车时 `t_alight` 取载体驶离区域或磁带结束时刻）内，该目标的可观测区间与位置由载体车辆继承；该区间内对载体的检测按 §3.6 V1 的时间区间映射归属该目标，对载体的识别级检测视为对该目标的识别级检测。载体在登乘区间之外按其自身类别计入 `K_d`。
4. 建筑内（磁带状态非 moving、waiting）的区间不属于 `O_k`。
5. 运动模型公开：`brief.target_motion` 公开运动模型类别与参数（`random_walk` 的 `v_tgt`、`leg_s`；`exogenous_path` 只公开"给定轨迹"这一类别与速度上限），研究者可据此做贝叶斯预测；航点与轨迹本身属真值。`random_walk` 的局部航点抽样只在边界与障碍附近产生反射性偏置，其对先验 `b_0` 在 `horizon` 内失配程度的影响由生成器在 `difficulty{}` 中报告 `prior_drift_m`（全部目标在 `T_c` 时刻相对初始位置的均方根位移），并作为已知局限写入 `validity.md`（§13）。
6. 载体关系的披露：简报公开第 3 条规则本身（"目标可能登乘车辆，登乘期间以载体为该目标"），不公开具体载体、登乘时刻与地点。

### 3.4 传感与识别

**`cs-planar` 平面 `L1`（覆盖 M28 R-M28-59 的初值）**

| 参数 | 含义 | `wide` 初值 | `tele` 初值（`d3` 起） |
|---|---|---|---|
| `fov_deg` | 视场（扇形；360 表示全向圆盘） | 360（近正下视的地面足迹抽象） | 20，指向由 `sensor_point` 控制 |
| `r_s` | 探测半径（平面距离） | 30 m（`d0` 为 40 m；对应 60 m 量级高度、HFOV 约 60° 的近正下视足迹半径量级；平面世界只取该足迹半径，不计高度带来的斜距） | 150 m |
| `P_d(d)` | 检测概率 `P_d0 · exp(−d² / (2σ_d²))`，`d` 为平面距离，`d ≤ r_s` 且视线通过时适用，否则为 0；`σ_d = null` 表示 `σ_d = ∞`，即 `P_d ≡ P_d0` | `P_d0` 与 `σ_d` 按档位（§5.1） | `P_d0 = 0.9`，`σ_d = 150 m` |
| `λ_fa` | 每次采集的虚警数服从 Poisson(`λ_fa`)，位置在视场内均匀 | 按档位 | 0.005 |
| `σ_pos(d)` | 位置噪声标准差 `σ_0 + k_σ · d` | `σ_0 = 1.0 m`，`k_σ = 0.01` | `σ_0 = 0.5 m`，`k_σ = 0.005` |
| `r_id` | 识别半径：平面距离 ≤ `r_id` 的检测为识别级 | `d0`–`d1`：`r_id = r_s`；`d2`：`0.5 r_s`；`d3` 起：0（广角不可识别） | 150 m |
| `p_conf` | 识别级检测的签名混淆概率（真高价值目标报为 `other` 或干扰目标报为 `hv` 的概率） | `d0`–`d1`：0；`d2` 起：0.05 | 0.02 |
| `rate_hz` | 采集频率，采集时刻为 `1/rate_hz` 的整数倍 | 10 | 10 |

**检测字段（Agent 可见）**。两族共用核心字段集合 `{det_id, sensor_id, capture_ns, xy, cov_xy, range_m, ident: bool}`，另有按族登记的扩展字段（`brief.sensing_public.det_ext[]` 公开声明）：

| 字段 | `cs-planar` | `cs-handoff-mesh`（经 `mesh_obs_adapter@1`） |
|---|---|---|
| `det_id` | 内核以 `sensor` 域派生的 64 位不可预测标识，每次采集重新生成 | `L1Model` 的 `det_id`（D3 键打包，CF-06） |
| `sensor_id` | 实体声明的传感器 | 同 `L1Model` |
| `capture_ns` | 采集时刻 | `t_mid_ns` |
| `xy` | 带噪位置 | `ground_xy_noisy` |
| `cov_xy` | `σ_pos(d)² · I`，`d` 为平面距离 | `σ_g² · I`，`σ_g = sig0_m + sig_per_m · slant_range_m`（以带噪读数计算，`L1Params` 缺省 0.4 m、0.008）；不含导航误差项，导航误差的公开参数另见 `sensing_public.nav` |
| `range_m` | 平面距离（无噪声；`sensing_public.range_kind = planar`） | `slant_range_m`（带噪斜距读数；`range_kind = slant`） |
| `ident` | 平面距离 ≤ `r_id` | `appearance` 字段存在 |
| 扩展：`sig` | `"hv"` 或 `"other"`，只在 `ident = true` 时出现，按 `p_conf` 混淆 | 不提供 |
| 扩展：`class`、`bbox_xywh`、`h_px`、`appearance` | 不提供 | 原样转出；`appearance` 只在可识别时出现，高价值判别由 Agent 将其与 `brief.hv_signature` 比对完成 |

`det_id` 不携带对象身份（与 CF-06 的匿名做法一致）；虚警检测与真实对象的检测在输出格式上不可区分。真值侧记录 `det_id → object_id | "fa"`、平面距离（网格族另记斜距、无遮挡像素高度与可见比例）与是否识别级，只写入真值层。适配层为纯函数，不改变 `L1Model` 的随机键、调用次序与输出；FC-2 的"字段集合相同"在网格族的 `L1` 与 `L2` 两档之间判定（`L2` 多出的只为帧引用），两族之间只要求核心字段集合相同。

随机键沿用 M28 R-M28-59：`sensor` 域，`object_id = sensor_id`，`time_index = capture_tick`，`draw_index` 按对象行与分量编号；签名混淆抽样占用同域的独立 `draw_index` 块（块起点在 CT 的域登记中声明）。

**`cs-handoff-mesh`**：相机参数按角色声明：`wide` 取 `CameraModel` 缺省（HFOV 60°）；`tele` 取 HFOV 9.1°（参考 EO-Z8 长焦端，CF-18），其余同缺省。识别级判据为 `L1Model` 输出 `appearance` 字段（CF-19）。检测的匿名 `det_id`、外观噪声与导航误差均沿用 D3 键打包（流 21、22），不受 D4 域派生影响。

**单元规模与复杂度**。传感器模型在每个采集周期对全部（传感器 × 目标）对做一次向量化距离预筛，只对 `d ≤ r_s` 且位于视场内的对做视线判定；`n64` 下每采集周期约 64 × 96 ≈ 6,100 对的距离预筛与其中少量对的视线判定，预算见 §7.7。

### 3.5 决策空间

| 决策 | 载体 | 说明 |
|---|---|---|
| 运动 | `EntityCommand`：`goto_w`、`follow_path`、`vel_w`、`hold`（M28 R-M28-52）；网格族经 `TierAdapter` 映射到 `KinematicTier` | 算法隔离模式下由平台路径跟随器执行 |
| 传感器指向 | `sensor_point{sensor_id, heading_rad \| target_xy}` | `wide` 全向时无效果；`tele` 与网格族云台有效 |
| 确认报告 | `ReportRequest`（`coopsearch.confirm.v1`，§7.6） | 受速率限制与每 Agent 配额约束 |
| 消息 | `SendRequest`；平台基线的消息 schema 见 M30（`coopsearch.visited.v1`、`coopsearch.detection.v1`、`coopsearch.goal.v1`、`coopsearch.pos.v1`） | 字节计入团队预算 |
| 交接 | `InteractionOp`：`handoff.offer{target_ref, to}`、`handoff.accept{op_ref}` | `d3` 起有计分意义；`d0`–`d2`（`w_t = 0`）允许但不影响 `U_CS` |
| 机巢 | `nest.dock.req{nest_id}`、`nest.undock.rel{nest_id}`（M29 R-M29-72） | `d3` 起；规则见 §3.10 |
| Belief | `BeliefSnapshot`（`belief.target_gauss.v1`，自愿） | 只用于诊断与方向 C（AWR-06 §9.1），不进入 `U_CS` |

### 3.6 报告与有效确认

**报告约束（由 `DecisionValidator` 在密封段执行，M28 R-M28-60）**

| 规则 | 内容 | 违反时 |
|---|---|---|
| 速率限制 | 每个 Agent 相邻两条确认报告的 `t_apply` 间隔 ≥ `rate_limit_s`（初值 2 s） | 拒绝，回执原因 `rate_limited` |
| 每 Agent 配额 | 每个 Agent 每个 Episode 被接受的确认报告数 ≤ `quota_agent`（初值 2）；团队总额隐含为 `n · quota_agent`，与目标数无关 | 拒绝，回执原因 `quota` |
| 证据数 | `1 ≤ |evidence_ids| ≤ 8` | 拒绝，回执原因 `evidence_count` |
| 自身证据 | 本 Agent 产生的证据须为其在 `t_sealed` 之前确实收到的检测（M28 R-M28-63） | 拒绝，回执原因 `evidence_unknown` |
| 转述证据 | 他机产生的证据不在密封段拒绝，由评测器按产生方交付记录解析（CF-08 语义） | — |

配额按 Agent 独立计量的理由：团队配额会使"剩余配额"或 `quota` 回执随队友的报告变化，构成零延迟、不受分区与 `cut` 干预影响、不计费的带外信道，向全队泄露"有队友在报告"（AWR-06 §8.8 A-01）；每 Agent 配额只取决于本 Agent 自身的历史，观测与回执不含任何他机信息，同一 tick 内也不存在跨 Agent 竞争最后名额的裁决问题。上述四项规则全部只读本 Agent 的状态，按 M28 R-M28-60 在密封段对每个 Agent 独立执行，结果与 Agent 的处理次序无关。

被接受的报告只得到 `applied` 回执，回执与报告是否有效无关；平台不提供任何可推断报告有效性或他机活动的反馈（AWR-06 §8.8 A-01）。

**有效确认判定（评测器，真值层）**。确认报告 r 的生效时刻为 `t_apply(r)`，内容 `{pos_xy, t_ref_ns, evidence_ids}`。r 对目标 k 构成有效确认，当且仅当：

| 条件 | 内容 |
|---|---|
| V1 归属 | 证据多数表决（与 CF-07 同规则）：每条证据 e 按其真值映射投票给对象 id 或"虚警"；得票最多且不平票的对象为 r 的归属对象 `k(r)`；"虚警"得票最多或平票时 r 无归属。网格族的对象映射带时间区间：`obj(e) = k`，若 `actor(e)` 是 k 的载体且 `t_cap(e) ∈ [t_board,k, t_alight,k)`（`hidden/targets.json` `carriers[]`）；否则 `obj(e) = actor(e)` 经 `actor_map` 映射的目标，或该演员自身（属 `K_d`）。登乘前对车辆的证据投票给车辆（`K_d`），登乘后对车辆的证据投票给目标；一条报告的证据跨越 `t_board` 时，两类证据分别投票，按上述多数规则裁决 |
| V2 目标类别 | `k(r) ∈ K_hv` |
| V3 位置门限 | `‖pos_xy − x_k(t_ref)‖ ≤ g`（初值 10 m），`x_k` 为真值位置（载体继承时取载体位置） |
| V4 时间一致 | `t_ref ∈ [min_e t_cap(e), t_sealed(r)]`，且 `t_sealed(r) − max_e t_cap(e) ≤ L_stale`（初值 10 s） |
| V5 识别证据 | 至少一条归属 `k(r)` 的证据为识别级；`d3` 起该证据还须由 `tele` 角色的传感器产生（`confirm.require_role = tele`） |
| V6 转述可解析 | 他机证据须存在于产生方的已交付检测记录中，且 `t_cap(e) ≤ t_sealed(r)` |

`τ_k = min{t_apply(r) : r 是对 k 的有效确认}`，取内核签发的报告生效时刻（`t_apply = t_sealed + ℓ_model`），不取提交或密封时刻。同一 `t_apply` 上有多条对 k 的有效确认时，按 `agent_id` 码点序取首条作为"首次有效确认"（决定持有方）。`t_apply > T_c` 的有效确认不产生 `τ_k`（`s_k = 0`、`c_k = 0`），也不建立持有关系，只计入诊断。其余报告分类为：重复确认（对已确认目标的有效确认）、错误确认（归属干扰目标）、无归属报告（虚警或平票）、门限外报告（V3 或 V4 不满足）。分类只用于诊断指标，不扣减 `U_CS`；配额使无差别报告具有机会成本。

### 3.7 跟踪持有与跨平台交接（`d3` 起计分）

**持有表**（真值层，`truth/` 中的 `custody` 记录）：对每个已确认高价值目标 k，真值层维护持有方 `h_k(t)` 与当前令牌 `tok_k(t)`。

1. 建立：目标 k 的首次有效确认（`τ_k ≤ T_c`）的报告方成为持有方 `h_k(τ_k)`，该报告的引用 `"<agent_id>@r<seq>"` 成为 `tok_k`。重复确认不改变 `h_k` 与 `tok_k`；重复确认的报告引用不是令牌。
2. 要约：`handoff.offer{target_ref, to}`，`target_ref` 为要约方自己的令牌（文法见 §7.6）。要约按 M29 R-M29-73 一律登记，并以交互控制消息经网络发往 `to`（计费计入要约方，受丢包、分区、端点下线与 `cut` 约束）；送达后 `to` 观测到 `OFFERED`。
3. 接受：`to` 在交接窗口内（`accept.t_apply ≤ offer.t_apply + handoff_window_s`，`handoff_window_s` 初值 10 s）对已送达的要约提交 `handoff.accept{op_ref}`，接受方即得 `GRANTED`（其自身操作的结果）；接受通知以交互控制消息发往要约方（计费计入接受方），在窗口结束前送达则要约方见 `GRANTED`，否则见 `EXPIRED`。窗口内未被接受的要约，要约方与已见 `OFFERED` 的 `to` 均见 `EXPIRED`。
4. **合法性判据（`validate_handoff`，只写真值层）**：一次接受在其 `RESOURCE_ARBIT` 处理时刻 `t_a`（取处理该接受之前的真值状态）合法，当且仅当下列各项全部成立：(a) 要约所指目标 k 已确认（`τ_k` 存在）；(b) 要约方 `= h_k(t_a)`；(c) `target_ref = tok_k(t_a)`，即要约方引用的正是其当前令牌（首次确认的报告引用，或其最近一次合法接受的 `op_id`）；(d) 接受方 ≠ 要约方，且接受方所控实体在 `t_a` 处于 `ACTIVE`（含停靠中，§3.10）；(e) 要约方所控实体在要约的 `t_apply` 处于 `ACTIVE`（要约发出后要约方失效不使要约不合法，见状态迁移表 E5）。合法时 `h_k ← 接受方`、`tok_k ← 该 accept 的 op_id`，记 `custody{cause: handoff}`；不合法时真值层记 `null_handoff`，`h_k`、`tok_k` 不变。非持有方以其有效重复确认的报告引用发起要约，不满足 (c)，记 `null_handoff`。
5. **合法性不改变 Agent 可见状态**：对 Agent 可见的事件序列（`OFFERED`、`GRANTED`、`EXPIRED`）只由送达与窗口决定，与第 4 条的判定结果无关；仲裁器不以任何 `DENIED_*` 回应交接（M29 R-M29-73、§6.6.2），以免形成"确认是否有效"的免费查询。
6. 并发：同一令牌可被多个要约引用（同一要约方重复要约或要约给不同接受方）。全部接受按 M29 R-M29-70 的顺序键 `(t_apply, priority, actor_id, op_seq)` 依次处理，S2 `ArbiterRules.priority()` 对交接操作恒取 0；首个满足第 4 条的接受生效，其后引用同一（已失效）令牌的接受因不满足 (c) 记 `null_handoff`，对 Agent 均显示 `GRANTED`。不同边界上的接受按边界先后处理。
7. 交接成功（诊断）：合法接受后 `reacq_s`（初值 10 s）内，接受方产生对该目标的识别级真阳性检测；否则记交接失败（持有方仍已转移）。
8. 持有方失效：持有方实体 `FAILED`（X1 F 族）或 `DEPLETED`（§3.10）后，持有关系保持在该 Agent 上，直到经合法交接转出；失效实体不再采集，其交互控制消息因端点下线而不能送达（M28 R-M28-46），因此其持有的跟踪在本 Episode 内不再产生覆盖。重新确认不产生新持有方（第 1 条只对首次确认生效），使"失效前主动交接"与"失效后由其他平台重新确认"成为可比较的策略差异。

> 第 8 条的后果是：持有方静默失效且未事先交接时，该目标的 `c_k` 只累计到失效时刻。重新确认不能恢复持有（避免"失效后任何平台再报一次即可接管"使交接失去意义），这一设计在 `validity.md` 中声明为 Benchmark 约定，由 X1 的 `T_detect`、`T_recover` 在条目卡上补充刻画（§9.4）。

**交接状态迁移表**（与 M29 §6.6.2、§6.6.3 一致；"语法校验"由 S2 `SuiteRule` 在密封段执行，只读公开的 Agent 名册与要约方自身的引用文法，不读真值）：

| 编号 | 情形 | 对要约方可见 | 对 `to` 可见 | 真值层 |
|---|---|---|---|---|
| E1 | 要约语法不合法：`to` 等于要约方、`to` 不在 Agent 名册中、`target_ref` 不符合 §7.6 文法或其 `agent_id` 前缀不是要约方 | 回执 `rejected`，原因 `handoff_syntax`（要约不登记） | 无 | 不记录交接 |
| E2 | 要约控制消息在受理事务中被拒、在网络中丢失、被 `cut` 阻断，或 `to` 的端点下线（含 `to` 已静默失效） | 窗口结束时 `EXPIRED` | 无 | 记要约未送达 |
| E3 | 要约送达晚于窗口结束 | 窗口结束时 `EXPIRED` | 不产生 `OFFERED`（送达事件被忽略） | 记要约迟到 |
| E4 | 要约送达，`to` 未在窗口内接受 | `EXPIRED` | `OFFERED`，窗口结束时 `EXPIRED` | 无转移 |
| E5 | 要约送达，`to` 在窗口内接受，接受通知于窗口结束前送达 | `GRANTED` | `OFFERED`、`GRANTED` | 按第 4 条判定：转移或 `null_handoff` |
| E6 | 同 E5，但接受通知丢失、被阻断或迟于窗口结束（含要约方在要约后失效、端点下线） | `EXPIRED` | `OFFERED`、`GRANTED` | 按第 4 条判定（以接受为准）；若判定为转移，记 `split_view`（要约方视图与真值不一致） |
| E7 | 接受的提交者不是该要约的 `to`、要约未送达该提交者、已过窗口或 `op_ref` 不存在 | — | 提交者见 `EXPIRED`（M29 §6.6.2"前置不满足"行） | 记无效接受 |
| E8 | 同一令牌在窗口内被重复要约或要约给多个接受方 | 各要约分别按 E2–E6 | 各自分别 | 第 6 条：至多一个接受合法 |
| E9 | 要约方在要约 `t_apply` 时已 `FAILED` 或 `DEPLETED` | 控制消息不能送达，按 E2 | 无 | `null_handoff`（第 4 条 (e)）或未送达 |

E6 中的视图分歧是协议层面应由研究者处理的现象（例如以普通消息确认），平台不纠正；`split_view_count` 作为诊断指标输出（§7.2）。

**跟踪覆盖率** `c_k`（只在含跟踪规则的实例中计算）：

```
W_k   = [τ_k, τ_k + W_trk]                     W_trk 初值 60 s；τ_k ≤ T_c = horizon − W_trk，故 W_k 完全落在 Episode 内
G_k   = W_k ∩ O_k 上以 100 ms 为步的时刻集合；|G_k| 以秒计为 0.1 · #G_k
C_k   = { t ∈ G_k : 持有方 h_k(t) 在 (t − h_s, t] 内有对 k 的识别级真阳性检测 }，h_s 初值 2 s
c_k   = |C_k| / |G_k|              若 τ_k 存在且 |G_k| ≥ G_min（初值 10 s）
u_k   = (1 − w_t) · s_k + w_t · c_k   若 τ_k 存在且 |G_k| ≥ G_min
u_k   = s_k                         若 τ_k 存在且 |G_k| < G_min（该目标的权重全部置于 s_k）
u_k   = 0                           若 τ_k 不存在（含仅在 T_c 之后才被有效确认）
```

`|G_k| < G_min` 的处置使"确认后立即不可观测"的目标不能以空集或极短窗口取得 `c_k = 1`，消除 v0.1 中 `|G_k| = 0` 时 `c_k = 1` 对晚确认的奖励；短于 `G_min` 的窗口方差过大，也不作为跟踪证据。为使该情形在 `cs-planar` 中不出现，生成器约束任一目标不可观测区间的起点 `> T_c`（R-S2-12），从而对任意 `τ_k ≤ T_c` 有 `|G_k| ≥ min(W_trk, horizon − T_c) = W_trk`；网格族由固定集审定逐事件登记每个目标 `O_k` 与 `[0, T_c + W_trk]` 的关系及 `|G_k| < G_min` 的确认时刻区间（§12.3），`validity.md` 声明该情形的占比。pilot 检验"延迟确认"策略（在首次具备确认条件后推迟确认）的 `U_CS` 不高于即时确认（AC-S2-15）。

### 3.8 运行模式

| 模式 | 平台固定的组件 | 被测组件（研究者提供） | 场景集后缀 |
|---|---|---|---|
| 算法隔离（`algorithm_isolated`） | 运动执行（路径跟随）、传感器指向规则（`wide` 正下视、`tele` 指向分配目标）、检测聚合与确认报告（公共 `report` 构件，规则 RC：门限 `g` 内 5 s 内 ≥ 2 次同签名检测且含 ≥ 1 次识别级检测即报告）、`d3` 的交接执行（持有方在能量低于阈值或目标将离开其可达区域时向 `Solver` 指定的接受方要约） | `Solver`：每个重规划周期（初值 2 s）为本 Agent 给出 `Plan{goal_xy, role_job ∈ {search, confirm, track}, target_hint?, handoff_to?, messages[]}`；分布式赛道中每个 Agent 一个 `Solver` 实例，只见自身视图与收件箱（`Solver` 的执行位置以 M30 定稿为准） | `.ai` |
| 闭环（`closed_loop`） | 无（只保留 `DecisionValidator` 与仲裁器） | `Policy`：运动、指向、报告、消息、交接全部由 Agent 决定 | `.cl` |

两种模式为不同场景集，分别成榜（ADR-D4-17）。算法隔离模式下确认环节由平台固定，`U_CS` 的差异来自搜索与指派；闭环模式下确认与交接也是被测对象。

### 3.9 终止

Episode 在 `horizon_s` 到达时终止；全部高价值目标已确认且（含跟踪规则的实例）全部跟踪窗口已结束时可提前终止，提前终止不改变任何指标（`s_k` 与 `c_k` 的定义不依赖终止时刻）。清单据此声明 `x1.early_stop_invariant = true`；X1 运行（含 `q = 0`）一律关闭提前终止、在 `horizon` 终止（X1 R-RC-59）。

### 3.10 机巢与能量（`d3` 起）

| 项 | 规则 |
|---|---|
| 能量模型 | M28 R-M28-55：`energy_uwh' = energy_uwh − round((P0 + P1·‖v'‖)·Δp_s/3600·10⁶)`；`d3` 只对 `tele` 角色施加能量约束，`wide` 的 `energy_wh = null`（M28 R-M28-106 第 4 项，不扣减） |
| 参数初值 | `tele`：`capacity_wh = 12`、`P0_w = 200`、`P1_w_per_mps = 4`（悬停续航 216 s，10 m/s 巡航续航 180 s）；充电功率 `charge_w = 400`（由空至满 108 s）；`wide`：无约束 |
| `energy_ratio` 目标区间 | `tele` 单次续航（按 `v_max` 巡航计）/ `horizon` ∈ [0.4, 0.7]（`horizon = 360 s` 时初值 0.50）；超出区间的实例被生成器拒绝 |
| 机巢 | 2 座，每座 `slots = 1`、`dock_radius_m = 5`；位置在可行区域内、距区域边界 ≤ `0.15 L`、两座间距 ≥ `L/3`；自全部起始位置与区域内可行点均可达（可行区域连通，R-S2-09）；机巢不在阻挡运动障碍内 |
| 停靠 | `nest.dock.req` 按 M29 R-M29-72 仲裁（距离 ≤ `dock_radius_m`、机位空闲、机巢供电）；停靠中实体保持 `ACTIVE`、速度指令被忽略（实体保持在机位）、传感器停采、以 `charge_w` 充电；`nest.undock.rel` 后恢复 |
| 停靠中的持有 | 停靠中实体的 Agent 保持持有关系，可发出与接受交接（其端点在线）；因传感器停采，停靠期间不产生覆盖，促使"停靠前交接" |
| `DEPLETED` | 能量耗尽的实体置 `DEPLETED`（M28 R-M28-55），在本 Episode 内不可恢复；运动与传感器指令回执 `entity_inactive`（只进入控制者）；持有关系按 §3.7 第 8 条处理；S2 视同失效：其要约记 `null_handoff`（§3.7 第 4 条 (e)），其接受不合法（第 4 条 (d)） |
| 公开性 | `brief.nests[]` 与 `brief.energy` 公开；剩余能量只在本 Agent 的 `self_state` 中可见 |
| 与 X1 H 族 | X1 §2.3 规定 H 族（机巢断电）对 S2 在 P2 前不适用；S2 `d3` 的机巢不接受 `nest_outage` 扰动，P2 评估时另行登记 |
| 充电模型 | M28 R-M28-55 只规定能量扣减，未规定停靠充电；充电按 `energy_uwh' = min(cap, energy_uwh + round(charge_w·Δp_s/3600·10⁶))` 在 M28 能量模型中实现，停靠期间不扣减 `P0`、`P1` 项；提请 M28 登记（§17.2） |

---

## 4. 场景族

### 4.1 `cs-planar`

| 项 | 规定 |
|---|---|
| Family id | `cs-planar` |
| 生成器 | `gen/cs_planar@1`：`params_space()` 声明 §5.1 的旋钮；`generate(params, instance_seed)` 产出 §6 的实例包；`difficulty(inst)` 输出 §5.3 的客观难度量 |
| 评测器 | `coopsearch@1`（`cs-planar` 与 `cs-handoff-mesh` 共用，网格族的检测真值与视线后端经 `RunView` 注入） |
| 世界类型 | `planar` |
| 评测通道 | 按 M31 R-M31-41 的六通道词表声明 `tasks`、`ident`、`agent_state`、`net`（网格族另加 `tape`）：目标真值轨迹经 `agent_state` 的 `state` 记录（目标为内核实体；网格族经 `tape`）读取；检测真值映射与证据经 `ident`（`sensor.capture`、`report.evidence_ids`）读取；确认报告与交接、机巢交互记录经 `tasks` 读取；资源账本经 `RunView.ledger()` 读取。M33 R-M33-69 另列 `reports`、`interaction`、`ledger` 三个通道，与 M31 不一致，以 M31 为准并提请统一（§17.2）；不使用 `truth.*` 等词表外名称 |
| 运行模式 | `algorithm_isolated`、`closed_loop` |
| 事件模式 | `exogenous`（主榜）；`interactive`（`d3` 起扩展场景集，单列） |
| 估计目标 | `family` |
| 排名物理档 | `planar@1` |
| 锚点实例 | `d0` 至少 2 个（M31 OR-7、V-S-15）：A（单目标解析首次确认时刻，§12.1）与 B（同刻重复确认的去重与持有方规则）；锚点恒属 `dev`。锚点的起始布局为解析计算而设，不受 §5.1 `d0` 行起点生成规则的约束 |

### 4.2 `cs-handoff-mesh`

| 项 | 规定 |
|---|---|
| Family id | `cs-handoff-mesh` |
| 实例来源 | 固定实例清单（`kind = fixed`，无生成器）：以 D3 封板时冻结的事件版本目录为输入，由 S2 转换工具 `tools/suites/coopsearch/mesh_from_event.py`（新写）生成实例包；转换只读事件目录，不修改 D3 资产 |
| 首批事件 | `rendezvous_handoff` 类：`crop-handoff-001` 及 D3 P0b 交付的同类 dev/test 变体；`target_search` 类：D3 封板时若已交付则纳入（CF-05、Q-S2-01） |
| 高价值目标 | 由转换规则从脚本演员中选取：`rendezvous_handoff` 取全部脚本人员演员（载体继承规则适用于登车者）；`target_search` 取事件 `search.target`。`K_d` 为背景人群与其余脚本演员；被登乘的车辆演员在登乘区间之外属 `K_d`，在登乘区间内按 §3.6 V1 的时间区间映射归属被载目标，任一时刻只属一类 |
| 目标先验 | `rendezvous_handoff`：区域内均匀分量与以公开锚点为中心（σ = 30 m）的高斯分量混合，权重 0.5/0.5（初值）；`target_search`：事件 `search.prior` 原样 |
| 世界与后端 | `oryahovo-crop1`、`MeshWorldQuery`、`KinematicTier`、`L1Model`，均只在 `hosts/hifi`（`drone4d[hifi]`）中装载 |
| 物理档 | `kinematic`（排名档以 Q-D4-12 在 P1 门裁决）；`realistic` + `L2` 只做抽样审计 |
| 事件模式 | 只 `exogenous`（CF-13） |
| 运行模式 | P1 只开放 `closed_loop`；`algorithm_isolated` 在网格族路径跟随器与 `KinematicTier` 对拍完成后评估 |
| 机巢与能量 | P1 不含（全部实体 `energy_cap_wh = null`）；`d3` 标签在网格族中指广角/长焦异构、确认须长焦与跟踪规则 |
| 黄金运行 | 固定型 Family 以至少 2 个黄金运行代替锚点（M31 OR-7、V-S-15）：GR-1 为 `crop-handoff-001` × `independent_lawnmower@1` × 种子 1，检验转换、观测适配与覆盖计算；GR-2 为 `crop-handoff-001` × 测试专用脚本策略 `scripted_track@test`（一架长焦按 `person_B` 磁带轨迹的已知偏移跟随，于已知时刻确认并向另一长焦交接一次），检验载体映射、`c_k` 与持有转移。期望分量写入 `golden/` 并进入 `members` 摘要 |
| 规模 | 固定世界不适用恒密度规则；规模标签 `n4`（3 `wide` + 1 `tele`）与 `n8`（5 `wide` + 3 `tele`），`n_tele = round(n/3)` |
| 估计目标 | `fixed_set`（AWR-06 §8.6；结论不外推到集合之外） |
| 划分 | crop 世界只有一个地理区，`dev` 与 `holdout` 无法满足"同一建筑及相邻区块不跨划分"（AWR-06 §8.7）。处置：`holdout` 优先使用 D3 交付的 town 世界上的事件；不具备时以 crop 内不相交的区域块划分，并在 `validity.md` 与条目卡上标注"留出集与开发集同一地理区"（反馈 15）；此时该场景集不成榜（§4.3 成榜最低条件 MC-3） |
| 首要用途 | 跨保真度审计：同一批基线在 `L1 + kinematic` 与 `L2 + realistic` 上配对运行，执行 FC-1、FC-2 与 FC-3 至 FC-7（AWR-06 §5.8、AC-D4-27；H5、E4） |

### 4.3 场景集声明

| 场景集 id | Family | 划分 | 运行模式 | 事件模式 | 档位（P0） | 角色 |
|---|---|---|---|---|---|---|
| `cs-planar.dev.smoke` | `cs-planar` | `dev` | `closed_loop` | `exogenous` | `d1/n4` × 2 场景 × 3 公开种子 | `smoke` |
| `cs-planar.dev.ai`、`cs-planar.dev.cl` | `cs-planar` | `dev` | 两种 | `exogenous` | `d0`–`d2` × `n4`–`n16` | 开发 |
| `cs-planar.validation.ai`、`.cl` | `cs-planar` | `validation` | 两种 | `exogenous` | `d1`、`d2` × `n8` | 聚合反馈 |
| `cs-planar.holdout.ai`、`.cl` | `cs-planar` | `holdout` | 两种 | `exogenous` | `d2` × `n8` 为主榜；`d1` × `n4`–`n16` 为规模阶梯 | `ranked` |
| `cs-planar.holdout.cl.trk`（P1） | `cs-planar` | `holdout` | `closed_loop` | `exogenous` | `d3` × `n8` | `ranked` |
| `cs-planar.holdout.cl.ia`（P1） | `cs-planar` | `holdout` | `closed_loop` | `interactive` | `d3` × `n8` | 扩展场景集，单列 |
| `cs-planar.stress`（P1） | `cs-planar` | `stress` | 两种 | `exogenous` | X1 子集 6 场景：取自宿主成榜场景集的 `holdout`，`n ≥ 8`，`d1`–`d2`（`d3` 开放后含 `d3`），按 X1 R-RC-21 分层选取 | `stress` |
| `cs-handoff-mesh.dev.cl`（P1） | `cs-handoff-mesh` | `dev` | `closed_loop` | `exogenous` | `d3` × `n4`、`n8` | 开发与审计 |
| `cs-handoff-mesh.holdout.cl`（P1） | `cs-handoff-mesh` | `holdout` | `closed_loop` | `exogenous` | `d3` × `n4`、`n8` | 满足 MC-1 至 MC-3 时 `ranked`（`fixed_set`），否则只作审计（`audit`），不进入主榜 |

全部场景集的主指标均为 `u_cs`（§7.1）；同一公式按实例声明的 `w_t` 计算：`d0`–`d2` 实例 `w_t = 0`，`d3` 起实例（含 `.trk`、`.ia` 与网格族）`w_t = 0.5`；`stress` 子集中各实例沿用其自身的 `w_t`。

每个场景集的场景数由 WP-D4-17 pilot 按 AWR-06 §8.6 功效规则确定（缺省 18 个场景 × 5 个外生种子，不少于 12 个场景）。网格族受事件数量限制，以实际可用的固定场景数登记并在条目卡上注明样本规模；其 `holdout` 场景集成榜须同时满足：

| 编号 | 成榜最低条件（网格族） | 理由 |
|---|---|---|
| MC-1 | 场景数 ≥ 6（与 AWR-06 §5.8 每 Suite ≥ 6 个场景、E4 的 6 场景 × 5 种子一致） | 场景内种子重采样的区间只反映种子变异，场景过少时固定集均值本身不稳定 |
| MC-2 | 每场景外生种子 ≥ 5 | `fixed_set` 以场景内种子 percentile 重采样估计；AWR-06 §8.6 设计阶段模拟显示 percentile 区间在小样本下覆盖率约 91%–92%，种子再少则区间不可靠 |
| MC-3 | `holdout` 与 `dev` 不在同一地理区（AWR-06 §8.7） | crop 内区块划分不能保证隔离，同区留出不构成留出证据 |

不满足任一条件时该场景集登记为 `audit`（场景集角色词表的增补见 §17.2），结果只进入跨保真度审计报告与条目卡的诊断栏，`validity.md` 记录未满足的条件。

---

## 5. 难度与规模

### 5.1 `cs-planar` 难度档

难度档只取实例内在旋钮与外生通信特征（M31 OR-4）；"锚点网络档"列为该档在教学、锚点与单元测试中的缺省运行配置，不约束赛道。

| 档 | 客观旋钮（实例内在） | 典型参数（参考规模 n8；`d0` 为 n4） | 锚点网络档 |
|---|---|---|---|
| `d0` | 静止目标、无障碍、`P_d0 = 1`、`σ_d = ∞`（`null`）、`λ_fa = 0`、`r_id = r_s`、无干扰目标、目标数公开 | 4 架、200 m × 200 m、`r_s = 40 m`、2 个静止高价值目标、`horizon = 120 s`；生成实例的起点位于区域中心 20 m 范围内（锚点实例为特例，见 §4.1）；`sweep_time_ratio = 40,000 / (4 · 80 · 10) / 120 ≈ 0.10` | `ideal@1` |
| `d1` | 遮挡障碍（遮挡率 `o ≈ 0.10`）、`P_d0 = 0.8`、`σ_d = r_s`、`λ_fa = 0.005`、慢速目标（速度比 `ρ_v = v_tgt / v_max = 0.05`，`random_walk`）、`r_id = r_s`、无干扰目标、目标数公开 | 600 m × 600 m、`|K_hv| = n/2`、300 s；单机面积 `|Q|/n = 45,000 m²` | `abstract-disk@1`、`abstract-radio@1` |
| `d2` | 移动目标（`ρ_v = 0.15`）、`P_d0 = 0.8`、`σ_d = r_s`、干扰目标 `|K_d| = n`、`r_id = 0.5 r_s`、`p_conf = 0.05`、`λ_fa = 0.01`、遮挡率 `o ≈ 0.15`、非均匀先验（3 分量高斯混合）、子团队分区一次（时长在 90–120 s 内抽样，起点按 §5.2 抽样）、目标数不公开且按实例抽样（§5.2） | 800 m × 800 m、`E|K_hv| = n/2`、300 s；团队预算 8,192 B/s/节点；单机面积 80,000 m² | `abstract-radio@1 default` |
| `d3`（P1） | `d2` + 广角/长焦异构（`n_tele = round(n/3)`）、`r_id(wide) = 0`、确认须长焦证据、持续跟踪窗口 60 s（`T_c = horizon − 60 s`）、`w_t = 0.5`、交接窗口 10 s、能量预算（长焦单次续航短于 `horizon`，须机巢换班与交接，参数见 §3.10）、2 座机巢；不可观测区间起点晚于 `T_c`；扩展场景集加警觉逃逸（`interactive`） | `horizon = 360 s`、`T_c = 300 s`；机巢 2 座；`energy_ratio ≈ 0.50`；逃逸灵敏度 `r_alert = 60 m`、`t_alert = 2 s` | `abstract-radio@1 default` |
| `d4`（P2） | `d3` + `n32`、能量约束收紧、分区两次 | 近容量上界 | `abstract-radio@1 severe` |

### 5.2 规模与恒密度缩放（`cs-planar`）

| 量 | 规则 |
|---|---|
| 区域边长 | `L(n) = L(8) · √(n/8)`（`d1`：`L(8) = 600 m`；`d2`、`d3`：`L(8) = 800 m`），即单机面积不变 |
| 目标数 | `d0`–`d1`（目标数公开）：`|K_hv| = n/2`；`d2` 起（目标数不公开）：`|K_hv|` 在 `{⌈3n/8⌉, …, ⌊5n/8⌋}` 上以 `instance` 域均匀抽样（`n4` 为 {2}、`n8` 为 {3, 4, 5}、`n16` 为 {6, …, 10}），期望 `n/2`，使"不公开"不被规模档直接推出；`|K_d| = n`（`d2` 起） |
| 障碍数 | 与区域面积成正比，遮挡率 `o` 保持在档位目标值 ± 0.02（生成器以拒绝抽样实现） |
| 报告配额 | 每 Agent `quota_agent = 2`（团队隐含总额 `2n`），与 `|K_hv|` 无关 |
| 团队通信预算 | `tx_bytes ≤ n · horizon_s · 8192 B` |
| 分区 | 子团队按 `agent_id` 次序对半划分；时长与起点按档位旋钮抽样 |
| 规模档状态 | `n4`、`n8`、`n16` 于 P0 冻结；`n32` 于 P1（WP-D4-51）；`n64` 于 P2（WP-D4-52） |

`cs-handoff-mesh` 不按此规则缩放（§4.2）。

### 5.3 客观难度量

`Generator.difficulty(inst)` 输出以下量并写入实例 `difficulty{}`，用于相变扫描（PA-7）与 `validity.md`：

| 量 | 定义 |
|---|---|
| `area_per_agent_m2` | `|Q_free| / n`（`Q_free` 为扣除阻挡运动障碍后的面积） |
| `occlusion_rate` | §3.2 的 `o` |
| `sweep_time_ratio` | `|Q_free| / (n · 2 r_s · v_max) / T_c`：理想无重叠扫描全区所需时间与确认截止的比值 |
| `prior_entropy_ratio` | `H(b_0) / log|cells|`（栅格边长 `r_s/4`） |
| `speed_ratio` | `ρ_v` |
| `distractor_ratio` | `|K_d| / |K_hv|` |
| `ident_ratio` | `r_id / r_s`（`d3` 起按角色分别给出） |
| `partition_fraction` | 计划分区总时长 / `horizon` |
| `quota_ratio` | `n · quota_agent / |K_hv|`（真值量，只写入 `difficulty{}` 的隐藏部分；`d2` 起实例的 `difficulty{}` 公开部分不含该量与 `|K_hv|`，见 §6.1） |
| `prior_drift_m` | 全部目标在 `T_c` 时刻相对初始位置的均方根位移（§3.3 第 5 条） |
| `tele_fraction` | `n_tele / n`（`d3` 起） |
| `energy_ratio` | 长焦单次续航 / `horizon`（`d3` 起） |

相变扫描只在 `dev` 上调整生成器公开参数，S2 的可调清单为：`ρ_v`、`o`、`r_id`、`p_conf`、`λ_fa`、`distractor_ratio`、`partition_fraction`、先验分量、区域边长 `L(8)`；每个配置至多 3 轮，参数与 PA-2 至 PA-4 前后分数写入 pilot 报告（AWR-06 PA-7）。评分与规则参数（`w_t`、`G_min`、`W_trk`、`h_s`、`g`、`L_stale`、`rate_limit_s`、`quota_agent`、`handoff_window_s`）不在可调清单内：它们在预注册文档中依研究问题与物理依据取定，pilot 只报告其敏感性，不以区分度为标准选取。AWR-06 PA-7 列举的可调参数以 S1 为例，未覆盖 S2 的感知类旋钮，按 Suite 声明可调清单的修订见 §17.1 反馈 24。

---

## 6. 标准实例字段

实例包目录布局遵循 M31 §7.4.1；本节规定 S2 各文件的字段与可见性（`x-visibility`：`brief`、`scheduled`、`truth`）。数值字段采用 M31 的整数量化与单位后缀规则（如 `_mm`、`_ms`）；下表为便于阅读写出语义单位。

### 6.1 `manifest.json`（S2 增补部分）

| 字段 | 类型 | 可见性 | 说明 |
|---|---|---|---|
| `instance_id` | string | brief | `capl:<family>/<block>-<index>/d<k>/n<S>@<rev>`（M31 OR-1） |
| `family` | enum | brief | `cs-planar`、`cs-handoff-mesh` |
| `generator` | object | brief | `{id: gen/cs_planar, version, digest}`；网格族为 `{kind: fixed, converter: mesh_from_event@1, event_ref}` |
| `instance_seed_ref` | object | brief | `dev` 为 `{value}`，隐藏划分为 `{commit_ref}`（M31 PK-05） |
| `difficulty` | object | brief（`d2` 起由 `|K_hv|` 推出的 `quota_ratio`、`distractor_ratio` 移入 `hidden/difficulty.json`，属 truth） | §5.3 |
| `files[]` | array | brief | 内容文件登记（M31 PK-02） |

### 6.2 `world/planar.json`（`cs-planar`）与 `world/ref.json`、`tape/ref.json`（网格族）

| 字段 | 类型 | 可见性 | 说明 |
|---|---|---|---|
| `bounds` | `[x_min, y_min, x_max, y_max]` | brief | 区域外接矩形 |
| `region` | polygon | brief | 搜索区域多边形 `Q` |
| `obstacles[]` | `{id, polygon, blocks_motion, blocks_los}` | brief | 遮挡与阻挡运动障碍 |
| `no_fly[]` | `{id, polygon}` | brief | 禁入区（编译为 `blocks_motion = true`、`blocks_los = false`） |
| `world/ref.json` | `{world_id, contentVersion, tiles[], region_ids[]}` | brief | 网格族世界引用（M31） |
| `tape/ref.json` | `{event_ref, tape_content_sha256}` | brief | 网格族事件磁带引用；磁带内容属真值 |

### 6.3 `brief.json`（Agent 可见）

| 字段 | 类型 | 说明 |
|---|---|---|
| `region`、`obstacles`、`no_fly` | 同上 | 地图与公开情报 |
| `target_prior` | `{type: uniform \| gmm \| grid, components[{xy, sigma_m, weight}] \| grid_ref}` | 先验 `b_0(x)` |
| `target_count` | int \| null | `d0`–`d1` 公开，`d2` 起为 null |
| `target_motion` | `{model, params}` | 目标运动模型类别与参数（§3.3 第 5 条） |
| `hv_signature` | object | `cs-planar`：`{sig: "hv"}`；网格族：各高价值目标的外观描述 `{label, class, appearance{top_rgb, bottom_rgb}}`（沿用 `brief_for_agent` 的外观字段，CF-04），另含公开规则 `carrier_rule: true`（§3.3 第 6 条，不披露具体载体） |
| `roles[]` | `{role, count, sensor_ref}` | 角色组成与公开传感器参数引用 |
| `sensing_public` | object | 各角色的 `fov_deg`、`r_s`、`P_d0`、`σ_d`（`number \| null`，null 表示 ∞）、`λ_fa`、`σ_pos`、`r_id`、`p_conf`、`rate_hz`；`range_kind ∈ {planar, slant}`；`det_ext[]`（本族扩展字段名，§3.4）；网格族另含 `L1Params` 公开项与 `nav`（导航误差参数） |
| `nests[]` | `{id, xy, slots, dock_radius_m}` | `d3` 起（§3.10） |
| `energy` | `{per_role: {capacity_wh \| null, P0_w, P1_w_per_mps, charge_w}}` | `d3` 起；能量模型同 M28 R-M28-55，充电见 §3.10 |
| `report` | `{gate_m, rate_limit_s, quota_agent, stale_s}` | 报告规则（§3.6） |
| `tracking` | `{window_s, g_min_s, hold_s, handoff_window_s, reacq_s, require_role}` \| null | `d3` 起；null 表示无跟踪规则（`w_t = 0`） |
| `horizon_s`、`confirm_deadline_s` | number | `H` |
| `start` | `{entity_id: {pos_m, heading_rad, z_layer_m, role}}` | 起始位姿的公开投影；权威来源为 `agents.json` 的 `entities[].initial_state`，V-I 校验二者一致 |
| `partitions_public` | bool | 计划分区是否以公告形式提前告知（缺省 false） |

### 6.4 `agents.json`

| 字段 | 说明 |
|---|---|
| `entities[]` | `{entity_id, kind: multirotor, role, limits{v_max_mps, a_max_mps2}, z_layer_m, sensors[sensor_id], energy_cap_wh, initial_state{pos_m, heading_rad, vel0_mps?, energy_wh?}}`（字段与 M28 R-M28-106 一致；`initial_state` 为起始位姿的权威来源）；`cs-planar` 初值 `v_max = 10 m/s`、`a_max = 3 m/s²`。高度层有界：共 4 层，`z_layer_m = 60 + 8 · (k mod 4)`（60、68、76、84 m），k 为实体按 `entity_id` 码点序的序号；层间距 8 m 沿用 `sdk:baselines/common.py` `LAYER_M = 8.0`，但不随 n 线性增高。同层实体之间不做物理隔离，最小间距由 §7.4 `sep_violation_ratio` 度量 |
| `agents_template[]` | `{agent_id, controls: [entity_id], scope: FULL}`；P0 一控一 |

### 6.5 `sensing.json`、`comm.json`、`tasks.json`、`objective.json`

| 文件 | 字段 | 可见性 | 说明 |
|---|---|---|---|
| `sensing.json` | `sensors[{sensor_id, role, fov_deg, r_s, P_d0, sigma_d, lambda_fa, sigma0, k_sigma, r_id, p_conf, rate_hz}]`；网格族为 `{sensor_id, camera{width, height, hfov_deg, rate_hz}, l1_params_ref}` | brief | 与 `brief.sensing_public` 同源 |
| `comm.json` | `partitions[{groups[[agent_id]], t_start_s, t_end_s}]`、`comm_zones[]`（网格族由磁带提供，此处只登记引用） | `partitions` 为 truth（除非 `partitions_public = true`） | 不含网络档（M31 OR-3、V-I-17） |
| `tasks.json` | `announcements[]`（可选：如"新目标进入区域"的无位置公告） | scheduled | S2 缺省为空；目标出现计划属真值，不进入 `scheduled` |
| `objective.json` | `{primary: u_cs, w_t, g_min_s, metrics[], horizon_s, confirm_deadline_s, termination}` | brief | `primary` 恒为 `u_cs`；`w_t` 由档位决定（`tracking = null` 时为 0，`d3` 起为 0.5），在预注册中取定，进入实例摘要 |

### 6.6 `hidden/targets.json`（真值）

| 字段 | 说明 |
|---|---|
| `targets[]` | `{target_id, class: hv \| distractor, motion: static \| random_walk \| exogenous_path, params{v_tgt, leg_s}, xy0, trajectory_ref?, unobservable[[t0, t1]]}` |
| `react` | `interactive` 场景的反应参数 `{r_alert_m, t_alert_s, v_flee_mps, t_flee_s}`（`interactive` 模式时生效） |
| `actor_map` | 网格族：`{target_id: tape_actor_id}` |
| `carriers[]` | 网格族：`{target_id, carrier_id, t_board_s, t_alight_s?}`，§3.6 V1 时间区间映射的输入 |
| `confirm_profile` | 网格族：每个目标 `|G_k| < G_min` 的确认时刻区间与 `O_k` 摘要（固定集审定登记，§3.7） |

### 6.7 `reference/`、`eval/`

| 文件 | 内容 |
|---|---|
| `reference/reference.json` | `d0` 锚点：`{kind: analytic, tau_k, u_cs, source}`；其余实例 `{kind: none}`（CS 无可证明最优，集中式参考不称上界） |
| `eval/binding.json` | `{evaluator: coopsearch@1, thresholds_ref}` |
| `eval/expected.json` | 仅锚点：期望分量与容差（§12.1） |

---

## 7. 指标

### 7.1 主指标

```
s_k   = 1[τ_k ≤ T_c] · (1 − τ_k / (2 T_c))                         ∈ {0} ∪ [0.5, 1]
u_k   = (1 − w_t) · s_k + w_t · c_k      τ_k 存在且 |G_k| ≥ G_min
      = s_k                              τ_k 存在且 |G_k| < G_min
      = 0                                τ_k 不存在
U_CS  = (1 / |K_hv|) · Σ_{k ∈ K_hv} u_k                             唯一主指标 u_cs
w_t   = 0（实例 tracking = null，d0–d2）；0.5（d3 起）
```

| 性质 | 说明 |
|---|---|
| 唯一主指标 | 清单 `metrics[]` 只有一个 `primary = true` 的指标：`MetricSpec{id: u_cs, kind: task, direction: max, range: [0, 1], primary: true, tier: true}`，满足 AWR-06 §7.0、M31 R-M31-04 与 V-S-02 的常规判定，不申请例外。`w_t` 是实例参数而非指标身份：`w_t = 0` 时 `u_k = s_k`，式子即纯搜索形式。跨 Suite 摘要 `S_task` 对 S2 取单一 `Ê[U]`（AWR-06 §8.4），口径为主榜场景集，其余场景集的取舍见 §17.1 反馈 23 |
| 诊断分解 | 另报非 primary 指标 `u_search`（同一 Episode 上 `w_t = 0` 的值，即 `(1/|K_hv|) Σ s_k`）与 `track_cov_mean`，使跟踪项的贡献可分离 |
| `w_t` 的取定 | 依研究问题取 0.5：S2 `d3` 的研究对象是"发现并确认"与"确认后维持责任跟踪"两个同等地位的环节（§1.1 研究问题 (1)–(3)），等权使任一环节失败都使该目标至多得一半分；该值写入预注册文档，pilot 只报告 `w_t ∈ {0.3, 0.7}` 下的排名敏感性（Kendall τ 与 PA-2 至 PA-4 是否仍满足），不据区分度改选 |
| 时间折扣 | 确认于 `T_c` 的目标仍得 0.5（`w_t = 0` 时），未确认得 0；该不连续使"确认"本身是主要信号、时间是次要信号（n03 §3.4 采纳的简化形式） |
| 不扣分 | 虚报、重复报告不扣减；以每 Agent 配额与速率限制形成机会成本 |
| 来源 | AWR-06 §7.2 主指标；`τ_k` 以报告生效时刻 `t_apply` 定义（本文明确，反馈 16）；`T_c`、`G_min` 与 `w_t` 的跟踪形式为本文细化（反馈 5、6） |

### 7.2 任务辅助指标

| id | 定义 | 方向 | 说明 |
|---|---|---|---|
| `conf_rate` | `|{k : τ_k ≤ T_c}| / |K_hv|` | max | 确认完成率 |
| `t_conf_median_s` | 已确认目标 `τ_k` 的中位数；无确认时 null | min | nullable |
| `t_det_median_s` | 首次真阳性检测时刻 `t_det,k`（任一 Agent）的中位数 | min | 搜索阶段效率 |
| `conf_latency_s` | 已确认目标 `τ_k − t_det,k` 的均值 | min | 确认阶段效率 |
| `auc_undetected` | `(1/T_c) ∫_0^{T_c} (1 − F_det(t)) dt`，`F_det(t)` 为 `t` 时已被检测的高价值目标占比 | min | 累积未发现概率曲线下面积（r02 §3.4） |
| `coverage_final` | `CR(T_c) = |∪_i seen_i(T_c)| / |Q_free|` | max | `seen_i(t)` 为 Agent i 至 t 的传感器视域（视线通过、`d ≤ r_s`、视场内）覆盖的评测栅格单元；栅格边长 `r_s/4`（网格族 5 m，1 Hz 采样） |
| `t90_s` | 首个 `CR(t) ≥ 0.9` 的时刻；未达到时记 `T_c` 并置截尾标志 | min | 移动目标下仅作诊断 |
| `redundancy` | `RD = Σ_i |seen_i(T_c)| / |∪_i seen_i(T_c)| − 1` | min | 冗余度 |
| `false_conf_rate` | 错误确认与无归属报告数 / 被接受报告数 | min | 虚报 |
| `dup_conf_rate` | 重复确认数 / 被接受报告数 | min | |
| `quota_used` | 被接受报告数 / `(n · quota_agent)`；另报逐 Agent 最大值 | min | |
| `u_search` | `(1/|K_hv|) Σ_k s_k` | max | 非 primary 诊断（§7.1） |
| `track_cov_mean` | 对 `|G_k| ≥ G_min` 的已确认目标取 `mean_k c_k`（`d3` 起）；无此类目标时 null | max | nullable |
| `short_window_frac` | 已确认目标中 `|G_k| < G_min` 的占比（`d3` 起） | — | `validity.md` 与条目卡披露 |
| `split_view_count` | §3.7 状态迁移表 E6 中真值转移而要约方见 `EXPIRED` 的次数 | — | 诊断 |
| `handoff_success_rate` | 交接成功数 / 合法 `GRANTED` 数；无合法交接时 null | max | 诊断，不进入 `U_CS^trk`（反馈 5） |
| `handoff_latency_s` | 合法交接从要约提交到 `GRANTED` 的均值 | min | |
| `null_handoff_rate` | `null_handoff` 数 / 全部 `GRANTED` 数 | min | 诊断协议缺陷（如以无效令牌要约） |
| `custody_loss_s` | `Σ_k (#G_k − #C_k) · 0.1 s`（只对 `|G_k| ≥ G_min` 的目标） | min | 持有方未跟住的时间总量 |
| `dock_time_frac` | 长焦实体停靠时长 / （长焦数 × `horizon`）（`d3` 起） | — | 机巢使用诊断 |
| `depleted_count` | 进入 `DEPLETED` 的实体数（`d3` 起） | min | |

### 7.3 资源指标

| id | 定义 | 方向 | 分层 |
|---|---|---|---|
| `b_tx_ratio` | 团队无线发送字节（含头部、分片、转发、交互控制消息）/ 团队额度 | min | tier |
| `cpu_ratio` | 团队 cgroup CPU 时间 / 团队额度 | min | tier |
| `bytes_by_class` | 按 `info_class`（`detection`、`target_state`、`task_summary`、`intent`、`ack`、`opaque`）分解的字节数 | — | 方向 B 诊断 |
| `energy_wh` | 团队能耗（`d3` 起） | min | 诊断 |

### 7.4 安全与有效性

| id | 定义 | 处置 |
|---|---|---|
| `sep_violation_ratio` | 存在任两平台三维距离 < 5 m 的时间占比（物理边界采样）。`cs-planar` 的三维距离由高度层与平面距离合成：异层实体层差 ≥ 8 m，恒不违规；同层实体（4 层循环复用，§6.4）即平面距离 < 5 m。网格族取 `KinematicTier` 三维位置 | 单独列示；超阈值（初值 1%）条目卡降级标注 |
| `actor_clearance_ratio` | 网格族：平台与人员或车辆演员的距离 < 10 m 的时间占比（CF-04） | 单独列示 |
| `actor_penetration` | 网格族：平台穿越演员包围体 | `ranked_valid = false`，原因码 `actor_penetration`（语义沿用 D3） |
| `fail_rate` | 失败 Episode 占比（策略崩溃、失活、非法动作） | 按 AWR-06 §8.6 失败计分（`U = 0`） |

### 7.5 协作诊断（Δ 族）

| 量 | S2 中的对照构造 | 说明 |
|---|---|---|
| `Δ_ind` | `π_ind = independent_lawnmower@1`，`none@1`，同资源包 | 系统级净收益 |
| `Δ_cut` | 同一镜像，阻断全部投递（含 `_ix` 交互控制消息，M29 R-M29-77）；发送照常计费 | `d3` 起阻断使交接要约无法送达，交接相关差异计入 `Δ_cut` |
| `Δ_perm`（P1） | `perm_struct` 只置换 `x-awr-role: world_state` 字段：`coopsearch.detection.v1.dets`、`coopsearch.visited.v1.cells`、`coopsearch.pos.v1.pos`；保留 `coopsearch.goal.v1` 的控制字段（M30 §6.6 消息表） | 交互控制消息不受置换（M29 R-M29-77） |
| `R_central` | `π_central = central_belief@1` | 非因果，可为负 |
| `G` | 闭合率，分母 95% CI 下界 ≥ 0.05 时报告；`family` 估计目标下以 Fieller 法、`fixed_set`（`cs-handoff-mesh`）下以复本比值 percentile 构造区间（M32 R-M32-45） | |
| 适用范围 | 因果解释只在 `exogenous`；`interactive` 场景集只报告总效应（AWR-06 §8.5） | |

### 7.6 报告与交接载荷 schema

**`coopsearch.confirm.v1`**（`ReportRequest.report_schema`；`$id = drone4d.coord.msg.coopsearch.confirm.v1`，位置与命名规则以 M30 消息 schema 方言为准）

| 字段 | 类型 | 约束 |
|---|---|---|
| `pos_xy` | `[number, number]` | m，ENU；有限值 |
| `t_ref_ns` | int64 | §3.6 V4 |
| `claim` | const `"hv"` | 预留扩展 |
| `evidence_ids` | 由 `ReportRequest.evidence_ids` 承载 | 1–8 条 |
| `conf` | number ∈ [0, 1]，可选 | 只作诊断，不参与判定 |

**`handoff.offer` 参数**：`{target_ref: string, to: AgentId}`，`target_ref` 文法 `<agent_id>@r<seq>`（要约方自己的确认报告引用）或 `<agent_id>@<op_seq>`（要约方自己此前提交的 `handoff.accept` 操作的 `op_id`，与 M29 R-M29-70 一致）；两种形式的 `<agent_id>` 均须等于要约方，`to` 须为名册中的其他 Agent，否则按 §3.7 状态迁移表 E1 在密封段以 `handoff_syntax` 拒绝。**`handoff.accept` 参数**：`{op_ref: string}`，`op_ref` 为所接受要约的 `op_id`。

**legacy 报告映射**（网格族中经 `wire.v1` 接入的 legacy Agent）：`act.v1.report` 中 `type = found` 的报告映射为 `coopsearch.confirm.v1`：`pos_xy` 取报告位置的水平分量，`t_ref_ns` 取报告窗口终点，证据取全部参与者证据（CF-04、CF-07）。legacy Agent 无法提交交接操作，其持有关系不能转移。

### 7.7 评测器实现要点

| 编号 | 要点 |
|---|---|
| EV-1 | 评测器只读声明的通道；以 trace 离线重算的全部分量与在线评测逐位一致（AWR-06 §5.9、AC-D4-08） |
| EV-2 | 证据多数表决、门限与时间判定为纯函数，输入为报告、检测真值映射与目标真值轨迹；与 `py:bench/eval/eu.py` `resolve` 以共享测试向量对拍（L1 按 `det_id` 的分支） |
| EV-3 | 覆盖指标（`coverage_final`、`t90_s`、`redundancy`）两族统一以 1 Hz 降采样计算：每个整秒时刻取各传感器的位姿，对视域内栅格单元做一次向量化视线判定；`cs-planar` 的视线判定与内核 `PlanarWorld` 同一实现，网格族在 `hosts/hifi` 中以 `MeshWorldQuery` 求视线。`cs-planar` 另可按"障碍摘要 + 栅格边长"缓存格到格可见性表（实例包内 `cache/vis.npz`，以障碍集合摘要为键），平台位置量化到所在栅格单元中心后查表；缓存只是加速手段，与直接判定的结果差异须在 AC-S2-24 的对拍容差内，否则禁用缓存 |
| EV-4 | `c_k` 在 100 ms 网格上计算；`τ_k`、`t_det,k`、交接时刻均取内核签发的整数纳秒时间 |
| EV-5 | 子编队投影（`subset`）：`U_CS` 对子编队重算时只计该子编队的报告与检测（用于 `Δ_ind` 一致性检查与扩容收益诊断） |
| EV-6 | 评测器不 import `awr.bench`、scipy、jsonschema、numba；网格族的后端依赖（open3d、zstandard）只在 `drone4d[hifi]` 中延迟导入 |
| EV-7 | 复杂度与预算：`d4_std` 下 300 s Episode 的采集次数为 `n · 10 Hz · 300 s`（`n64` 约 1.9 × 10⁵ 次），按 EV-3 以 1 Hz 降采样后覆盖判定次数降为 1/10；`tele`（`r_s = 150 m`、视场 20°）每次采集覆盖约 `π·150²·(20/360) / (r_s,wide/4)²` ≈ 70 个 7.5 m 栅格，与 `wide`（≈ 50 个）同量级。预算见下表，计入 AWR-06 §5.11 各规模的宿主预算 |

| 规模（`cs-planar`，`d2`/`d3`，300 s，RM-8 单核） | 区域边长 | 评测栅格数（边长 7.5 m） | 评测器墙钟 | 传感器模型（内核侧，整 Episode） | AWR-06 §5.11 宿主预算 |
|---|---|---|---|---|---|
| `n4` | 566 m | ≈ 5.7 × 10³ | ≤ 1.5 s | ≤ 2 s | ≤ 20 s |
| `n8` | 800 m | ≈ 1.1 × 10⁴ | ≤ 3 s | ≤ 4 s | ≤ 45 s（PA-6） |
| `n16` | 1,131 m | ≈ 2.3 × 10⁴ | ≤ 6 s | ≤ 9 s | ≤ 90 s |
| `n32`（P1） | 1,600 m | ≈ 4.6 × 10⁴ | ≤ 16 s | ≤ 24 s | ≤ 240 s |
| `n64`（P2） | 2,263 m | ≈ 9.1 × 10⁴ | ≤ 40 s | ≤ 60 s | ≤ 600 s |
| 网格族 `n4`、`n8`（`kinematic`，360 s） | 310 m × 280 m | 5 m 栅格 ≈ 3.5 × 10³ | ≤ 30 s | 由 `L1Model` 决定，不另设 | 不适用 PA-6；单 Episode 宿主墙钟 ≤ 180 s（初值，WP-D4-25 实测冻结） |

表中预算为初值，由 WP-D4-17（`n4`–`n16`）、WP-D4-51（`n32`）、WP-D4-52（`n64`）实测后冻结；任一规模超出且经优化仍不达标时按 AWR-06 §5.11 末段处置（该档不开放并登记）。

---

## 8. 基线与参考策略

### 8.1 基线清单

除 `track_share_cs@1` 外，标识、算法出处、参数与实现来源与 M30 §6.7.3 一致；本表给出 S2 规格层面的角色、适用场景族与预期序关系。M30 §6.7.3 表目前登记的是 legacy `track_share@1`（"沿用原实现常量"，只适用网格族），而其摘要第 6 条与 P1 范围行已列 `track_share_cs` 与 `custody`；本文以 `track_share_cs@1` 取代 `track_share@1` 在 S2 中的位置并使之同时适用 `cs-planar` `d3` 与网格族，M30 表的对应修订见 §17.2。出处核实状态沿用 r02 与 M30 的标注。

| 标识（`baseline:coopsearch/…`） | 角色 | 算法要点 | 通信 | 适用 | 阶段 |
|---|---|---|---|---|---|
| `independent_lawnmower@1` | `π_ind` | 全区牛耕式航带序列，起点按 `index/n` 错相（公开约定的静态时间分区），之后沿序列继续扫描；复用 `py:swarm/coverage/lanes.py` | 0 | 两族 | P0（网格族 P1） |
| `partition_lawnmower@1` | 基线 | 航带序列按等时长切分（`split_balanced`），按 `agent_id` 次序分配，本块扫完后在本块内重复；思想同 `sdk:baselines/coverage_scan.py` | 0 | 两族 | P0 |
| `shared_visited_greedy@1` | 基线 | 共享已搜栅格，前往最近未搜单元 | `coopsearch.visited.v1` | `cs-planar` | P0 |
| `frontier_ig@1` | 基线；PA-2 代表性协同基线 | 概率栅格贝叶斯更新，以"期望信息增益 / 旅行代价"选候选，队友已宣告目标附近折扣（Yamauchi, CIRA 1997 [待核实]；Burgard 等, IEEE T-RO 21(3), 2005 [待核实页码]） | `coopsearch.detection.v1`、`coopsearch.goal.v1` | 两族 | P0 |
| `voronoi_lloyd@1` | 基线 | Lloyd 覆盖控制，密度取未发现概率后验（Cortés, Martínez, Karatas, Bullo, IEEE T-RA 20(2), 2004 [待核实页码]） | `coopsearch.pos.v1` | `cs-planar` | P0 |
| `track_share_cs@1` | 闭环基线（`d3`、网格族） | D3 `track_share` 的目击、声明与"最近空闲机"接力协议（CF-09），增补：(a) 对外观或签名匹配的识别级轨迹按公共 `report` 构件规则提交确认报告；(b) 声明方丢失或能量低时以 `handoff.offer` 显式交接给最近空闲长焦平台，接受方以 `handoff.accept` 承接 | 目击、声明经 `coopsearch` 方言登记 | 两族 `d3` | P1 |
| `central_belief@1` | `π_central` | 集中式联合信念栅格，逐 Agent 顺序贪心指派信息增益目标；`d3` 起集中调度长焦确认与换班交接 | 集中式（`cr-*` 赛道团队算力对齐） | 两族 | P0（网格族 P1） |
| `random@1` | `π_random` | 随机航点游走，航段长 100 m | 0 | 两族 | P0 |

基线数：7 个基线（含 `π_ind`、`π_central`）加 `π_random`，与 AWR-06 §7.0 总览的"7"一致。

### 8.2 公共构件与公平性

| 构件 | S2 中的用途 | 约束 |
|---|---|---|
| `report`（M30 §6.7.0） | 全部 S2 基线的检测聚合、证据组装与报告节流，规则 RC（§3.8） | 使基线差异只来自搜索、指派与交接策略（M30 R-M30-44） |
| `custody`（本文新增，S2 专用，M30 §6.7.0 尚未登记，见 §17.2） | `d3` 起闭环基线的持有令牌维护与交接触发规则：持有方能量低于 25%、计划停靠、目标预计 10 s 内离开本机可达或视场范围时，向最近的空闲长焦平台要约；以普通消息确认接受，处理 §3.7 E6 的视图分歧 | 全部 `d3` 闭环基线共用，`track_share_cs@1` 与 `central_belief@1` 只替换"选择接受方"的规则 |
| `motion` | 绕障路径与路径跟随 | 与算法隔离模式的平台执行器同一实现 |

### 8.3 `track_share` 的处置说明

D3 SDK 的 `track_share` 以 `sight`/`claim`/`reported` 协议实现跟踪与接力（CF-09），但其报告规则只产生 `meet`、`board`、`enter_building` 三类事件报告，不产生 `found`（CF-10），且 `act.v1` 不承载交互操作。若经 legacy-v1 投影原样进入 S2，其确认报告数为 0，`U_CS` 恒为 0，失去基线意义。处置：

1. 原 `track_share` 不修改，继续作为 `drone4d-event@1` 的参考基线与 legacy 投影回归对象（M30 F10；AC-D4-09）。
2. S2 新增 `track_share_cs@1`，以原实现的协议常量（目击周期 1 s、声明有效期 3 s、外推 10 s、站位 50 m）为缺省，增补确认报告与显式交接，实现位于 `awr.coordination.baselines.coopsearch`，以 `wire.v2` 接入。
3. 条目卡注明 `track_share_cs@1` 与 D3 `track_share` 的差异，满足基线治理要求（AWR-06 §7.0，r03 R31）。

### 8.4 预期序关系与机制假设

序关系记号的可检验定义（场景等权均值的配对差 `D = U_B − U_A`，区间按 AWR-06 §8.6 的估计目标与重采样规则计算）：`A < B` 指 `D` 的 95% CI 下界 > 0；`A ≤ B` 指 `D` 的点估计 ≥ −ε（`ε = 0.02`）；`A ≈ B` 指 `D` 的 90% CI 落入 `[−ε_eq, ε_eq]`（`ε_eq = 0.05`，双单侧检验）；`A ≈ B` 不成立时只报告 `D` 及其区间，不视为预期被违反。

| 档 | 预期（pilot 检验，不是结论） | 机制 |
|---|---|---|
| `d0` | `frontier_ig`、`central_belief` 的 `U_CS ≥ 0.9` | 区域小、静止目标、`P_d = 1`；`sweep_time_ratio ≈ 0.10`（§5.1），理想扫描约 12.5 s，远早于 `T_c = 120 s`，按 `s_k ≥ 1 − 0.10/2 = 0.95` 量级可得 `U ≥ 0.9` |
| `d1` | `U_random < U_ind ≤ U_partition < U_shared ≈ U_voronoi < U_frontier < U_central` | 遮挡与 `P_d < 1` 使"一次扫过即可"的割草机遗漏，信念更新带来的回访有利 |
| `d2` | `U_central > U_frontier > U_ind`（AC-S2-02） | 移动目标与干扰目标使确认调度与检测转发的价值上升；分区期间无通信策略不受影响、通信依赖策略退化 |
| `d3` | `U_central > U_track_share_cs > U_ind`（`w_t = 0.5`） | 长焦稀缺使确认与持有成为瓶颈；无交接策略的 `c_k` 受长焦续航限制 |
| `d2`（构造效度对照） | 对照策略 `sig_greedy@test`（闭环；任一识别级 `sig = hv` 检测即按公共 `report` 构件立即报告，搜索同 `frontier_ig`）与 `frontier_ig` 的 `U` 差、`false_conf_rate`，以及同一批实例去除干扰目标后 `sig_greedy@test` 的 `U` 变化 `ΔU_d` | 检验干扰目标与 `p_conf` 是否构成实际的确认难度（RK-S2-02）：若 `ΔU_d` 的 90% CI 落入 `[−ε_eq, ε_eq]`，判定干扰旋钮无效，按 §5.3 可调清单在 `dev` 上收紧（`p_conf` 改为随距离增大 `p_conf(d) = p_conf0 · (1 + d / r_id)`，或提高 `distractor_ratio`），结果写入 `validity.md` 已知局限 |

基线序与预期不符时（如 `U_central < U_frontier`），由 M30 在基线卡中给出机制解释与参数核查记录后方可冻结（AWR-06 §7.0；M30 R-M30-49）。

---

## 9. 赛道、预算与评分

### 9.1 赛道与物理档

| 赛道 id（AWR-06 §8.3） | 角色 | S2 适用 |
|---|---|---|
| `dc-l1-b1-radio` | 主榜 | `cs-planar` 全部排名场景集；`cs-handoff-mesh`（P1，`kinematic`） |
| `dc-l1-b1-ideal` | 对照榜 | 通信约束代价诊断；H3（E3） |
| `cr-l1-b1-radio` | 集中式参考列 | `central_belief@1` 与集中式提交 |
| `tm-l1-b1-radio`（P1） | Timing-Measured 研究赛道 | 不排名 |
| `dc-l2-b2-radio`（P2） | `L2` 审计榜 | 只 `cs-handoff-mesh`，抽样证据，独立发布 |

清单 `ranked_physics`：`cs-planar` 为 `planar@1`；`cs-handoff-mesh` 为 `kinematic`（以 Q-D4-12 的 P1 门裁决为准）。`ranked_clock = d4_std`（Δd = 100 ms，`ℓ_model = 100 ms`）。网格族的 `abstract-radio@1` NLoS 判定使用网格世界查询（AWR-06 §6.5 第 1 项，P1 起），磁带通信区叠加到容量与丢包项（第 7 项）。

### 9.2 预算维（Suite 清单 `budget.dims`）

| 维 | 粒度 | 额度表达式（初值） | 超额处置 |
|---|---|---|---|
| `tx_bytes` | 团队 | `n_agents · horizon_s · 8192` | `budget.policy = violation_invalidates` |
| `cpu_ms` | Agent | `horizon_s · 100`（B1） | 同上 |
| `tokens` | 团队 | 0（B1、B2） | 同上 |
| `reports` | Agent | `quota_agent`（初值 2） | 超额报告被拒绝（§3.6），不判违规 |
| `energy_wh` | 实体 | 实例 `energy` 声明（`d3` 起） | 耗尽实体置 `DEPLETED`（M28 R-M28-55），不判违规 |

### 9.3 评分

| 项 | `cs-planar` | `cs-handoff-mesh` |
|---|---|---|
| `scoring.estimand` | `family`（场景层 bootstrap-t） | `fixed_set`（场景内种子重采样，percentile） |
| 统计单位 | 场景配置 | 场景内外生种子 |
| `invalid_policy` | `zero` | `zero` |
| 失败计分 | AWR-06 §8.6 | 同左 |
| 分层指标 | `u_cs`、`b_tx_ratio`、`cpu_ratio` | 同左 |
| 成榜条件 | §4.3 场景集角色 | MC-1 至 MC-3（§4.3）；不满足时 `audit` |
| 并列、检验族、ε | AWR-06 §8.6（`ε = 0.02`、`ε_eq = 0.05`，组首并列，Holm） | 同左 |
| 有效性声明 | `docs/suites/coopsearch-handoff/validity.md`（M31 目录约定 `docs/suites/<suite_id>/`；`{path, sha256}` 进入 `suite_digest`，M31 R-M31-05） | 同一文件分节 |

### 9.4 X1 RobustCoord 在 S2 上的落点

扰动族的适用性、参数与阶段以 X1 §2.3 的适用矩阵为准（X1 声明三份宿主规格与其不一致之处以 X1 为准）：

| 扰动族 | X1 §2.3 对 S2 | 阶段 | S2 中的作用对象与说明 |
|---|---|---|---|
| F 节点静默失效 | 适用 | P1 | 平台实体；持有方失效时适用 §3.7 第 8 条 |
| C 失联 | 适用 | P1（可顺延 P2） | 通信端点；持有方失联期间不能要约，交接控制消息按 §3.7 E2 处理 |
| M 成员变化 | 适用 | P2 | 实体离开与替补加入；`a_o` 在预告期内对 `Held(o)` 的主动交接计为响应（X1 §8.2） |
| P 网络分区 | 适用 | P1 | 与实例自带的计划分区叠加：有向掩码取逻辑与（反馈 21） |
| L 链路退化 | 适用 | P1 | `abstract-radio@1` 参数 |
| J 延迟尖峰 | 适用 | P1（可顺延 P2） | 交接控制消息与普通消息同受影响；尖峰可使要约或接受通知迟于窗口（§3.7 E3、E6） |
| H 机巢断电 | 不适用（P2 前） | — | S2 `d3` 机巢不接受 `nest_outage`（§3.10）；P2 评估时另行登记 |
| S 传感器退化 | 适用 | P1 | `P_d0` 下降、`σ_pos` 放大，幅度以 X1 §5.9 参数为准；`r_id` 不变；通过 `sensor` 域属性覆写施加，保持 CRN |

**宿主接入声明**（X1 §8.2、§8.3、R-RC-43、R-RC-59；清单 `x1{}` 字段）：

| 项 | S2 声明 |
|---|---|
| `Held(o)` | `t_o` 时刻真值层 `h_k(t_o) = a_o` 的已确认目标集合（§3.7 第 1、4 条） |
| `x1.response_ops` | (1) 确认报告 `coopsearch.confirm.v1`，关联谓词 `related_host(x, o) ⇔ k(x) ∈ Held(o)`（`k(x)` 为 §3.6 V1 的真值归属；该报告为"对失效实体所持目标的确认"，登记为 S2 的目标承诺操作）；(2) `handoff.offer`，关联谓词为其 `target_ref` 所指目标属于 `Held(o)`；(3) `handoff.accept`，关联谓词为其所接受要约的目标属于 `Held(o)`。M 族另含 `a_o` 对 `Held(o)` 中目标的 `handoff.offer` |
| 诊断量（不进入 `T_detect`） | 其他 Agent 对 `Held(o)` 中目标的首次识别级检测时刻；其他 Agent 视域首次覆盖失效实体失效前 60 s 内未覆盖、且位于其最近活动区域内的栅格单元的时刻（X1 §8.2 判定二者无法做孪生对应） |
| 累积效用 `U_cum(t)` | `U_cum(t) = (1/|K_hv|) Σ_k u_k(t)`，其中 `u_k(t) = 0`（`t < τ_k` 或 `τ_k` 不存在）；`u_k(t) = s_k`（`t ≥ τ_k` 且 `|G_k| < G_min`）；否则 `u_k(t) = (1 − w_t) · s_k + w_t · |C_k ∩ [0, t]| / |G_k|`。`u_k(t)` 单调不减，`U_cum(0) = 0`，且 `t ≥ T_c + W_trk` 时 `u_k(t) = u_k`，故 `U_cum(T) = U_CS` 逐位一致（以同一整数计数与同一除法次序计算）。`w_t = 0` 时 `U_cum` 为离散型（事件 = 首次有效确认）；`w_t > 0` 时 `s_k` 部分为离散增量、`c_k` 部分按 100 ms 步累积 |
| 提前终止 | `x1.early_stop_invariant = true`（§3.9） |
| `stress` 子集 | 按 X1 R-RC-21 自宿主成榜场景集的 `holdout` 选取，`n ≥ 8`（不选 `n4`，因 `k` 恒为 1），`d1`–`d2`（`d3` 开放后含 `d3`），§4.3 |
| 无持有情形 | `d0`–`d2` 无跟踪规则时持有表仍按 §3.7 第 1 条建立（只作诊断），`Held(o)` 照常定义；若 pilot 中合格对象比例 < 50%（X1 RK-RC-04），以 `T_recover` 为 S2 的主时间指标 |

---

## 10. 功能需求

"优先级"：P0 阻塞、P1 应交付、P2 可延期；"阶段"为首次交付阶段。

### 10.1 Suite 清单与契约

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-01 | Suite 清单 `suite_id = coopsearch-handoff`，按 `drone4d.suite.suite.v1` 声明 `families[cs-planar, cs-handoff-mesh]`、§4.3 场景集、§9 赛道与预算、`reference_policies{ind: independent_lawnmower@1, central: central_belief@1, random: random@1}` | P0 | P0 | AC-S2-07 | AWR-06 §4.4、§7.0；M31 R-M31-01、08 |
| R-S2-02 | 清单 `metrics[]` 恰有一个 primary：`u_cs`（§7.1）；跟踪权重 `w_t` 与 `G_min` 为实例 `objective.json` 参数（`tracking = null` 时 `w_t = 0`），不形成第二个 primary；`u_search`、`track_cov_mean` 与 §7.2 至 §7.4 的其余辅助指标按 `MetricSpec` 声明方向、范围与可空性，`primary = false` | P0 | P0 | AC-S2-07 | AWR-06 §7.0；M31 R-M31-04、V-S-02 |
| R-S2-03 | 档位以 `{id, status, opened_in}` 登记：`d0`–`d2`、`n4`–`n16` 为 `frozen`；`d3`、`n32` 为 P1 开放；`d4`、`n64` 为 P2 开放 | P0 | P0 | AC-S2-07 | M31 R-M31-09 |
| R-S2-04 | 契约新增：`coopsearch.confirm.v1` 报告 schema（§7.6）；交接参数 schema（`target_ref`、`op_ref` 文法）；`custody` 真值记录 schema（`{t_ns, target_id, holder, cause: confirm \| handoff \| null_handoff, op_ref}`）；均进入 `ct:coord/` 或 `ct:suite/`，不改 `ct:bench/` | P0 | P0 | AC-S2-07 | B-06；CP-3 |
| R-S2-05 | S2 包位于 `py:benchmarks/suites/coopsearch_handoff/`（M31 §7.1）；不 import `awr.bench`、`awr.sim.targets`；`cs-planar` 的全部模块在只装 numpy 的环境中可导入 | P0 | P0 | AC-S2-08 | B-02、B-10；ADR-D4-33 |
| R-S2-06 | `validity.md` 至少含 §13 列出的条目；未声明的模型局限不得出现在对外材料的结论中 | P0 | P0 | AC-S2-07 | AWR-06 §7.0；r03 R15 |

### 10.2 生成器（`cs-planar`）

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-07 | `gen/cs_planar@1` 以 `(params, instance_seed)` 确定性生成实例包；同输入两次生成的 `instance_sha256` 逐字节一致 | P0 | P0 | AC-S2-01 | PA-1；M31 V-I-24 |
| R-S2-08 | 生成器只使用 `instance` 域随机数，`random_walk` 目标的运行期航点使用 `target` 域；生成器不读墙钟、不依赖调用顺序 | P0 | P0 | AC-S2-01 | RN-1、RN-2 |
| R-S2-09 | 障碍生成以拒绝抽样满足：多边形简单且不自交、两两不相交、位于边界内；起点、目标初始位置与机巢不在阻挡运动障碍内；遮挡率在档位目标值 ± 0.02 内；可行区域连通 | P0 | P0 | AC-S2-01 | M31 V-I-13 |
| R-S2-10 | 目标位置按先验 `b_0` 抽样，且须位于可行区域；`exogenous_path` 轨迹不穿越阻挡运动障碍 | P0 | P0 | AC-S2-01 | §3.3 |
| R-S2-11 | 恒密度缩放按 §5.2；`Generator.difficulty` 输出 §5.3 全部量，V-I-14 重算一致 | P0 | P0 | AC-S2-01 | AWR-06 §7.0 |
| R-S2-12 | 目标不可观测区间（`cs-planar` 只在 `d3` 起出现，用于模拟遮蔽建筑）的起点晚于 `T_c`，使任意 `τ_k ≤ T_c` 的 `|G_k| ≥ W_trk`；网格族不满足者逐事件登记于 `hidden/targets.json` `confirm_profile` | P1 | P1 | AC-S2-15 | §3.7 |
| R-S2-13 | 干扰目标的运动模型与高价值目标同分布，仅类别不同：生成器先为 `|K_hv| + |K_d|` 个目标抽样运动与初始位置，再以 `instance` 域的独立置换指定类别，使"仅凭运动区分类别"不可行 | P0 | P0 | AC-S2-01 | §1.4 |
| R-S2-14 | 计划分区按 §5.2 生成并写入 `comm.json`；`partitions_public = false` 时分区不进入 `brief` 与 `scheduled`；`d2` 起 `|K_hv|` 按 §5.2 抽样，由其推出的难度量不进入 `brief` | P0 | P0 | AC-S2-01 | M31 §4.4；§5.2 |

### 10.3 传感与观测

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-15 | 平面 `L1` 按 §3.4 覆盖 M28 R-M28-59 的参数：距离相关 `P_d(d)`、Poisson 虚警、距离相关位置噪声、识别半径 `r_id` 与签名混淆 `p_conf`；参数作为属性登记 | P0 | P0 | AC-S2-09 | M28 R-M28-59；M28 反馈 17 |
| R-S2-16 | `det_id` 每次采集重新派生且不可由 Agent 预测；虚警检测与真实检测输出格式一致；检测不携带对象身份 | P0 | P0 | AC-S2-09 | CF-06；A-01 |
| R-S2-17 | 检测真值映射、真值距离与识别级判定依据只写入真值层；两场景族的 Agent 可见检测共用 §3.4 的核心字段集合，各族扩展字段在 `brief.sensing_public.det_ext[]` 中声明；网格族的 `L1` 与 `L2` 两档 Agent 可见字段集合相同（`L2` 多出的只为帧引用，FC-2） | P0 | P0 | AC-S2-09 | DP-05；FC-2 |
| R-S2-18 | 同外生种子下改变 Agent 的通信或运动，目标外生轨迹与传感器噪声序列逐位不变（`exogenous` 模式） | P0 | P0 | AC-S2-10 | RN-2；AC-D4-05 |
| R-S2-19 | `Observation.tasks` 的 S2 视图只含：本 Agent 的剩余报告配额（`quota_agent` 减本 Agent 已被接受的报告数）、本 Agent 已提交报告的引用与回执、本 Agent 自己的令牌（Agent 侧视角）、待处理交接事件；不含任何报告有效性信息，也不含任何随他机活动变化的量 | P0 | P0 | AC-S2-11 | A-01 |
| R-S2-20 | 每 Agent 每次密封的检测条目数上限 `det_max`（初值 64，超出时按 `(capture_ns, sensor_id, det_id)` 升序保留前 `det_max` 条并在真值层记截断数）与收件箱上限 `inbox_max` 由清单固定 | P0 | P0 | AC-S2-09 | PB-5 |

### 10.4 报告与确认

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-21 | `DecisionValidator` 对确认报告执行 §3.6 的速率限制、每 Agent 配额、证据数与自身证据检查，回执原因码 `rate_limited`、`quota`、`evidence_count`、`evidence_unknown`；各项只读本 Agent 状态，结果与 Agent 处理次序无关 | P0 | P0 | AC-S2-11 | M28 R-M28-60、63、108 |
| R-S2-22 | 被接受报告的回执与报告有效性无关；不存在可推断有效性的观测字段、回执码或时序差异 | P0 | P0 | AC-S2-11 | A-01 |
| R-S2-23 | 评测器按 V1–V6 判定有效确认，`τ_k = t_apply` 的最小值；同刻多条有效确认按 `agent_id` 码点序取首条；`t_apply > T_c` 的有效确认不产生 `τ_k` | P0 | P0 | AC-S2-12 | §3.6 |
| R-S2-24 | 证据多数表决规则与 `py:bench/eval/eu.py` `resolve` 的 `det_id` 分支等价，以共享测试向量（含平票、虚警多数、混合归属）对拍 | P0 | P0 | AC-S2-12 | CF-07 |
| R-S2-25 | 转述证据按产生方已交付检测记录解析；产生方记录中不存在或 `t_cap > t_sealed` 的证据不计票 | P0 | P0 | AC-S2-12 | CF-08；M28 R-M28-63 |
| R-S2-26 | 报告分类（有效、重复、错误、无归属、门限外）全部写入评测诊断，不进入 `U_CS` | P0 | P0 | AC-S2-12 | §3.6 |
| R-S2-27 | 算法隔离模式的平台确认规则 RC 固定并版本化（`report_rule@1`）；规则变化即 Suite 主版本变化 | P0 | P0 | AC-S2-13 | ADR-D4-17；M31 反馈 18 |
| R-S2-28 | legacy 报告映射按 §7.6 执行，只对网格族开放 | P1 | P1 | AC-S2-19 | CF-04 |

### 10.5 跟踪持有与交接

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-29 | S2 提供 `ArbiterRules`：`handoff_window_ns()`、`validate_handoff()` 与 `priority()`（交接操作恒为 0）；`validate_handoff` 按 §3.7 第 4 条判据 (a)–(e) 只读真值持有表 `(h_k, tok_k)` 与实体状态，判定结果只写真值层 | P0（规则）| P0 | AC-S2-05 | M29 R-M29-73、76 |
| R-S2-30 | 不合法要约对 Agent 的可见事件序列与合法要约相同（`OFFERED`、`GRANTED`、`EXPIRED`），真值层记 `null_handoff`；仲裁器不以 `DENIED_*` 回应交接（与 M29 R-M29-73、§6.6.2 现行文本一致） | P0 | P0 | AC-S2-05 | A-01；反馈 7 |
| R-S2-31 | 要约与接受通知均经网络以交互控制消息承载（计费、受丢包与分区、受 `cut` 干预阻断）；对要约方与 `to` 的可见结果按 §3.7 状态迁移表 E2–E9 产生，真值层以接受为准；视图分歧（E6）计入 `split_view_count` | P0 | P0 | AC-S2-05 | M29 R-M29-73、77；§6.6.3 |
| R-S2-32 | 同一令牌的并发接受按 M29 R-M29-70 顺序键 `(t_apply, priority, actor_id, op_seq)` 依次判定，只有首个满足 §3.7 第 4 条者合法 | P0 | P0 | AC-S2-05 | §3.7 第 6 条 |
| R-S2-33 | 持有表随 `custody` 记录写入真值层 trace；`c_k` 按 §3.7 计算 | P1 | P1 | AC-S2-14 | §3.7 |
| R-S2-34 | 持有方失效后持有关系不自动转移；重新确认不产生新持有方 | P1 | P1 | AC-S2-14 | §3.7 第 7 条 |
| R-S2-35 | 交接成功、失败与 `null_handoff` 分别计数；`handoff_success_rate` 不进入 `U_CS^trk` | P1 | P1 | AC-S2-14 | 反馈 5 |
| R-S2-36 | `d0`–`d2`（`w_t = 0`）允许交接操作但不影响 `U_CS`；交接相关诊断照常输出 | P0 | P0 | AC-S2-05 | §3.5 |
| R-S2-76 | 交接语法校验（S2 `SuiteRule`，密封段）：`to` 等于要约方或不在 Agent 名册中、`target_ref` 不符合 §7.6 文法或其 `agent_id` 前缀不是要约方时，以 `handoff_syntax` 拒绝该要约且不登记；校验只读公开名册与引用文法，不读真值，不对失效、失联或不存在持有关系的情形作出区分 | P0 | P0 | AC-S2-05 | §3.7 E1；A-01 |

### 10.6 评测器

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-37 | 评测器 `coopsearch@1` 实现 `Evaluator` 协议，输出 `drone4d.suite.evaluation.v1`，`components` 含 §7.1–§7.4 全部指标，`diagnostics.coopsearch` 含报告分类、持有表摘要、覆盖曲线 | P0 | P0 | AC-S2-12 | AWR-06 §4.2、§4.7 |
| R-S2-38 | 同一输入的评测结果逐字节一致；由 trace 离线重算与在线评测逐位一致 | P0 | P0 | AC-S2-04 | EV-1；AC-D4-08 |
| R-S2-39 | `d0` 锚点实例的评测结果与 `eval/expected.json` 一致：时间量（`τ_k`、`t_det,k`）以整数纳秒精确相等，`u_cs` 绝对误差 ≤ 1e-9；以 `t_sealed` 或提交时刻代替 `t_apply` 计算 `τ_k` 的实现必须判为不一致 | P0 | P0 | AC-S2-01 | PA-1；§3.6 |
| R-S2-40 | 覆盖栅格、`T_90`、冗余度与 `auc_undetected` 按 §7.2 计算；覆盖计算的视线判定与内核 `PlanarWorld` 同一实现 | P0 | P0 | AC-S2-12 | R-M28-50 |
| R-S2-41 | 子编队投影 `subset` 可用，单机投影下 `U_CS` 只计该机报告与证据 | P0 | P0 | AC-S2-12 | EV-5 |
| R-S2-42 | 评测器只读 §4.1 声明的通道（M31 R-M31-41 词表内名称）；读取未声明通道时报 `evaluator_channel_undeclared` | P0 | P0 | AC-S2-08 | M31 R-M31-41 |
| R-S2-43 | `cs-planar` `n8`、300 s 的单 Episode 评测墙钟 ≤ 3 s（RM-8 单核，初值），计入 PA-6 的 45 s 宿主预算；其余规模与网格族按 R-S2-75 | P0 | P0 | AC-S2-04 | PA-6；AWR-06 §5.11 |
| R-S2-44 | 评测器版本号与阈值版本（`coopsearch_thresholds@1`：`g`、`L_stale`、`h_s`、`W_trk`、`reacq_s`、栅格边长）写入评测结果 | P0 | P0 | AC-S2-12 | M27 先例 `eval_thresholds.v1` |
| R-S2-45 | 安全指标按 §7.4；网格族的演员间距与穿越判定在 S2 内自含实现，语义与 `py:bench/eval/safety.py` 一致，以共享用例对拍 | P1 | P1 | AC-S2-19 | CP-4 |
| R-S2-46 | 条目卡展示 `U_CS` 的同时并列 `conf_rate`、`false_conf_rate`、`b_tx_ratio` 与（`d3` 起）`track_cov_mean`、`handoff_success_rate` | P1 | P1 | AC-S2-20 | LB 条目卡 |

### 10.7 基线与参考策略

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-47 | §8.1 的 P0 基线（`independent_lawnmower`、`partition_lawnmower`、`shared_visited_greedy`、`frontier_ig`、`voronoi_lloyd`、`central_belief`、`random`）在 `cs-planar` 两种运行模式下可运行，动作流同种子逐字节一致 | P0 | P0 | AC-S2-03、04 | M30 §6.7.3 |
| R-S2-48 | 全部 S2 基线使用公共 `report` 构件；`d3` 闭环基线使用公共 `custody` 构件 | P0 / P1 | P0 / P1 | AC-S2-03 | M30 R-M30-44 |
| R-S2-49 | `track_share_cs@1` 按 §8.3 实现；原 `sdk:baselines/track_share.py` 不修改 | P1 | P1 | AC-S2-17 | CF-09、CF-10 |
| R-S2-50 | `π_ind`、`π_central`、`π_random` 在每个 Board Version 上预计算缓存，缓存键不含 `suite_digest`（M31 R-M31-45 的约定） | P0 | P0 | AC-S2-03 | AWR-06 §8.5 |
| R-S2-51 | 每个基线附基线卡：出处与核实状态、参数、与原文的复现差异、未达到原文水平的说明 | P0 | P0 | AC-S2-07 | AWR-06 §7.0；r03 R31 |
| R-S2-52 | 基线序关系按 §8.4 在 pilot 中检验；不符时须给出机制解释后方可冻结 | P0 | P0 | AC-S2-02 | AWR-06 §7.0 |

### 10.8 网格族与 D3 承接

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-53 | 网格族在 `hosts/hifi` 中以 `MeshWorldQuery` 装载 `oryahovo-crop1`；S2 包的其余模块不 import `awr.world.mesh`、`awr.agentsdk.env.l1`、`awr.agentsdk.env.camera` | P1 | P1 | AC-S2-08 | B-10 |
| R-S2-54 | 网格族以 `TapeReader` 读取冻结事件版本的磁带，演员状态、环境关键帧与通信区按磁带 100 ms 定点驱动；磁带内容与 `tape_content_sha256` 一致 | P1 | P1 | AC-S2-18 | CF-15；M31 V-I-15 |
| R-S2-55 | 转换工具 `mesh_from_event@1` 由事件版本目录确定性生成实例包（高价值目标选取、先验、载体继承、起始位姿）；转换只读，不改写事件目录 | P1 | P1 | AC-S2-18 | §4.2 |
| R-S2-56 | 网格族观测为经适配层 `mesh_obs_adapter@1`（R-S2-73）映射后的 `L1Model` 输出；`L1Model` 本体、随机键与调用次序不变，遗留随机键不经 BLAKE2b 派生，与 D4 新档的键空间不重叠 | P1 | P1 | AC-S2-18 | RN-7 |
| R-S2-57 | 网格族的证据解析以 `L1Model` 真值侧记录为输入，与 D3 `resolve` 对拍（L1 分支）；`L2` 审计时证据解析按同帧可见演员框 IoU ≥ 0.3 | P1 | P1 | AC-S2-18 | CF-07 |
| R-S2-58 | 网格族满足 FC-1、FC-2；FC-3 至 FC-7 按每 Suite ≥ 6 个场景 × 3 个种子 × 全部官方基线执行（网格族场景数不足 6 时以全部可用场景执行并标注） | P1 | P1 | AC-S2-05 | AWR-06 §5.8；AC-D4-27 |
| R-S2-59 | S2 任一工作包合入后，`drone4d-event@1` 下 crop-handoff 运行、评测与 `board.v1` 重算与封板基线逐字节一致 | P0 | P0 | AC-S2-06 | B-07 |
| R-S2-60 | 网格族 `holdout` 的地理隔离按 §4.2 处置并在 `validity.md` 与条目卡上标注 | P1 | P1 | AC-S2-18 | AWR-06 §8.7 |

### 10.9 运行模式、事件模式与治理

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-61 | 算法隔离模式的 `ProblemView` 与 `Plan` 字段按 §3.8 提供；平台执行器与公共构件同一实现 | P0 | P0 | AC-S2-13 | ADR-D4-17 |
| R-S2-62 | `interactive` 场景集（`d3` 起）使用独立随机域 `suite.coopsearch-handoff.react`；同实例同种子下 `exogenous` 与 `interactive` 的初态与外生驱动逐位一致 | P1 | P1 | AC-S2-16 | §3.3；AWR-06 §8.5 |
| R-S2-63 | 难度旋钮只在 `dev` 上调整，只限 §5.3 的可调清单，至多 3 轮相变扫描；评分与规则参数（`w_t`、`G_min`、`quota_agent` 等）在预注册中取定，pilot 只报告敏感性；`holdout` 冻结前以参考策略与官方基线执行 PA-2 至 PA-4 检查 | P0 | P0 | AC-S2-03 | PA-7；AWR-06 §7.0 留出集校准 |
| R-S2-64 | 带 `synthetic-stress` 标签的网络参数集得出的 S2 结论在报告与条目卡上携带该标签 | P0 | P0 | AC-S2-04 | AC-D4-21 |

### 10.10 机巢与能量（`d3` 起）

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-65 | 生成器按 §3.10 放置 2 座机巢：位于可行区域内且不在阻挡运动障碍内、距区域边界 ≤ `0.15 L`、两座间距 ≥ `L/3`、自全部起始位置可达；`slots = 1`、`dock_radius_m = 5` 写入 `brief.nests[]` 与 `ArbiterRules` 机巢表 | P1 | P1 | AC-S2-23 | M29 R-M29-72、76；§3.10 |
| R-S2-66 | `d3` 能量参数按 §3.10 初值写入实例（只约束 `tele`）；`Generator.difficulty` 输出 `energy_ratio`，超出 [0.4, 0.7] 的实例被拒绝 | P1 | P1 | AC-S2-23 | M28 R-M28-55、106；§5.3 |
| R-S2-67 | 停靠中实体：速度指令被忽略、传感器停采、按 `charge_w` 充电且不扣减 `P0`、`P1` 项；其 Agent 保持持有关系，可发出与接受交接；停靠期间不产生覆盖 | P1 | P1 | AC-S2-23 | §3.10 |
| R-S2-68 | `DEPLETED` 实体在本 Episode 内不可恢复；其持有关系按 §3.7 第 8 条保持；其要约与接受在真值层不合法（§3.7 第 4 条 (d)、(e)）；`depleted_count` 写入诊断 | P1 | P1 | AC-S2-23 | M28 R-M28-55、62 |

### 10.11 X1 宿主接入

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-69 | 清单声明 X1 适用族与阶段，取值引用 X1 §2.3（F、C、P、L、J、S 于 P1；M 于 P2；H 在 P2 前不适用）；`stress` 子集按 X1 R-RC-21 选取（`n ≥ 8`） | P1 | P1 | AC-S2-22 | X1 §2.3、R-RC-21 |
| R-S2-70 | 清单声明 `Held(o)`、`x1.response_ops` 与关联谓词 `related_host`（§9.4）的实现引用；确认报告登记为 S2 的目标承诺操作；识别级检测与视域覆盖只作诊断量 | P1 | P1 | AC-S2-22 | X1 §8.2、R-RC-43 |
| R-S2-71 | 评测器提供 `U_cum(t)`（§9.4）：单调不减、`U_cum(0) = 0`、`U_cum(T)` 与 `U_CS` 逐位一致；`w_t > 0` 时 `c_k` 部分按 100 ms 步累积 | P1 | P1 | AC-S2-22 | X1 §8.3 |
| R-S2-72 | 清单声明 `x1.early_stop_invariant = true`；X1 运行关闭提前终止 | P1 | P1 | AC-S2-22 | X1 R-RC-59；§3.9 |

### 10.12 网格族观测适配、成榜条件与规模预算

| 编号 | 需求描述 | 优先级 | 阶段 | 验收 | 依据 |
|---|---|---|---|---|---|
| R-S2-73 | `mesh_obs_adapter@1`（位于 `hosts/hifi`）按 §3.4 字段表把 `L1Model` 检测映射为核心字段与扩展字段：`ident` 由 `appearance` 是否存在推出，`cov_xy` 由 `L1Params` 公式与带噪斜距读数给出，`appearance` 原样转出、不提供 `sig`；适配为纯函数，对同一输入逐字节确定 | P1 | P1 | AC-S2-18 | CF-19；R-S2-17 |
| R-S2-74 | 网格族登记至少 2 个黄金运行（§4.2 GR-1、GR-2）；`holdout` 场景集只在满足 §4.3 MC-1 至 MC-3 时以 `ranked` 登记，否则登记为 `audit` 且不进入主榜 | P1 | P1 | AC-S2-18 | M31 OR-7、V-S-15；AWR-06 §8.6、§8.7 |
| R-S2-75 | 评测器与传感器模型满足 §7.7 EV-7 的分规模预算；覆盖指标以 1 Hz 降采样；`cs-planar` 可选的可见性缓存以障碍摘要为键，与直接判定对拍 | P0（`n4`–`n16`）/ P1（`n32`、网格族）/ P2（`n64`） | P0 / P1 / P2 | AC-S2-24 | AWR-06 §5.11、PB-1、PB-5 |

---

## 11. 验收标准

| 编号 | 优先级 | 阶段 | 内容 | 测试位置（建议） | 对应 AWR-06 | 需求 |
|---|---|---|---|---|---|---|
| AC-S2-01 | P0 | P0 | (a) PA-1：同 `params + instance_seed` 生成的实例 JCS 摘要逐字节一致；生成器不读墙钟、调用次序打乱后结果不变（R-S2-08）。(b) 生成器统计检验（`d1`、`d2` × `n4`–`n16` 各 200 个 `dev` 实例）：全部障碍多边形简单、两两不相交、位于边界内，起点、目标初始位置与机巢不在阻挡运动障碍内，可行区域连通（逐实例判定，100%）；遮挡率全部落入档位目标值 ± 0.02；`difficulty{}` 经 V-I-14 重算一致。(c) R-S2-13：`K_d` 与 `K_hv` 的初始位置到最近先验分量中心的距离、`T_c` 时刻位移两项统计量的两样本 KS 检验 p ≥ 0.005（两项 Bonferroni，族 α = 0.01）。(d) R-S2-14：`partitions_public = false` 的实例 `brief` 与 `scheduled` 中不含分区；`d2` 起 `brief` 不含 `|K_hv|` 及由其推出的难度量，`|K_hv|` 的经验分布与 §5.2 的均匀分布一致。(e) 锚点 A、B 的评测结果与 `eval/expected.json` 一致：`τ_k`、`t_det,k` 整数纳秒精确相等，`u_cs` 绝对误差 ≤ 1e-9；反例：以 `t_sealed` 计 `τ_k` 的变体评测器在锚点 A 上必须判为不一致。(f) `d0` 生成实例上 `frontier_ig` 或 `central_belief` 的 `U_CS ≥ 0.9`（正确性锚点） | `tests/d4/suites/coopsearch/test_generator.py`、`test_generator_stats.py`、`test_anchor.py` | AC-S2-1 | R-S2-07–11、13、14、39 |
| AC-S2-02 | P0 | P0 | 基线序关系：`d2/n8` 上场景等权均值 `U_central > U_frontier > U_ind`，两个配对差的 95% CI 下界 > 0；违反时有机制解释记录 | pilot 报告（WP-D4-17） | AC-S2-2 | R-S2-52 |
| AC-S2-03 | P0 | P0 | PA-2（代表性协同基线 `frontier_ig`，对 `π_ind` 配对差 ≥ 0.05 且 CI 不含 0；至少两对基线经 Holm 校正后显著且 > 0.05）、PA-3（`U_central − U_ind ≥ 0.15`）、PA-4（≥ 75% 实例上无基线 `U > 0.95` 或 `U < 0.05`）；§8.4 `d2` 构造效度对照（`sig_greedy@test`）的结果与处置写入 pilot 报告；调参只限 §5.3 可调清单且有逐轮记录；`holdout` 冻结前参考策略复核通过 | pilot 报告 | AC-S2-3 | R-S2-47、48、50、63 |
| AC-S2-04 | P0 | P0 | PA-5：固定制品 100 次重跑 trace 事件哈希一致（等级 1b）；R-S2-47 的 P0 基线在两种运行模式下同种子两次运行的动作流（`decisions_digest`）逐字节一致；PA-6：`cs-planar` `L1`、`n8`、300 s 单 Episode 宿主墙钟 ≤ 45 s、trace ≤ 200 MB，评测器 ≤ 3 s；离线重算与在线评测逐位一致；`synthetic-stress` 标签传递 | `tests/d4/suites/coopsearch/test_determinism.py`；`tests/perf/` | AC-S2-4 | R-S2-38、43、47、64 |
| AC-S2-05 | P0（规则）/ P1（`d3`、网格族） | P0 / P1 | 交接语义单元测试（对照 §3.7 状态迁移表）：(a) 窗口内完成（E5）；(b) 超时 `EXPIRED`（E4）；(c) 同令牌并发接受只有按顺序键首个者合法，含同一边界多个接受与跨边界两种构造（E8，第 6 条）；(d) 不合法要约对 Agent 的事件序列与合法要约逐字节相同、真值层记 `null_handoff`；(e) 分区期间要约未送达（E2）；(f) `cut` 干预阻断要约（E2）；(g) `d0`–`d2`（`w_t = 0`）实例中执行交接与不执行交接的两次运行 `U_CS` 逐位相等；(h) 非持有方以其有效重复确认的报告引用要约 → `null_handoff`；(i) 语法拒绝：`to` 为自身、不在名册、`target_ref` 前缀不是要约方 → `handoff_syntax`（E1）；(j) 接受由非 `to` 提交 → 提交者见 `EXPIRED`、真值层记无效接受（E7）；(k) 要约后要约方静默失效、`to` 接受 → 真值层转移、要约方侧通知未送达、计入 `split_view_count`（E6）；(l) `to` 已静默失效 → 要约方 `EXPIRED`，与丢包情形对 Agent 不可区分（E2）；(m) 要约送达晚于窗口 → `to` 不见 `OFFERED`（E3）；(n) 合法转移后 `tok_k` 为接受操作的 `op_id`，原令牌再次要约 → `null_handoff`。P1：网格族满足 FC-1、FC-2，并完成 FC-3 至 FC-7 审计 | `tests/d4/suites/coopsearch/test_handoff.py`；`fidelity_audit.v1` 报告 | AC-S2-5 | R-S2-29–32、36、58、76 |
| AC-S2-06 | P0 | P0 | D3 回归门 B-07：`drone4d-event@1` 下 crop-handoff 运行与 `board.v1` 重算与封板基线逐字节一致 | CI 黄金运行 | AC-S2-6 | R-S2-59 |
| AC-S2-07 | P0 | P0 | Suite 清单按 V-S-01 至 V-S-21 常规判定通过，不适用任何例外（V-S-02：`metrics[]` 恰有一个 primary `u_cs`，采用 §7.1 的单一主指标方案）；`suite_id = coopsearch-handoff` 符合 R-M31-02；契约 schema 通过元校验且与 `ct:bench/` 无冲突；基线卡齐全；`validity.md` 覆盖 §13 条目 | `tests/d4/suites/coopsearch/test_manifest.py` | AC-D4-18 | R-S2-01–04、06、51 |
| AC-S2-08 | P0 | P0 | 依赖边界：只装 numpy 的环境中导入 `cs-planar` 全部模块，`sys.modules` 不含 `awr.bench`、`awr.sim.targets`、scipy、open3d、zstandard、jsonschema、numba；评测器读取未声明通道时报 `evaluator_channel_undeclared`；清单声明的通道全部属 M31 R-M31-41 词表 | `tests/d4/suites/coopsearch/test_imports.py` | AC-D4-43 | R-S2-05、42、53 |
| AC-S2-09 | P0 | P0 | 传感模型：在平面距离 `d ∈ {0, 0.25, 0.5, 0.75, 1.0} · r_s` 五个取样点各做 10⁵ 次采集，检测频率与 `P_d(d)` 解析值之差落入二项精确置信区间（每点置信水平 1 − 0.01/5，五点 Bonferroni，族 α = 0.01）；`σ_d = null` 时五点频率与 `P_d0` 一致；虚警数的均值与方差符合 Poisson(`λ_fa`)；识别级比例与 `r_id` 一致，签名混淆比例符合 `p_conf`；检测输出不含对象身份；`det_id` 跨采集不重复；`det_max` 上限用例（构造 > 64 条检测，保留前 64 条且次序按 R-S2-20）与 `inbox_max` 上限用例 | `test_sensing.py` | — | R-S2-15–17、20 |
| AC-S2-10 | P0 | P0 | CRN：同外生种子下，改变 Agent 消息频率与航线，目标轨迹与传感器噪声序列逐位不变 | `test_crn.py` | AC-D4-05 | R-S2-18 |
| AC-S2-11 | P0 | P0 | 报告守卫：速率、每 Agent 配额、证据数、`evidence_unknown` 四类拒绝用例；泄露用例：(a) 有效与无效报告的回执、观测与后续事件逐字节相同；(b) 改变队友的报告数量与时刻，本 Agent 的观测（含剩余配额）与回执逐字节不变；(c) 同一 tick 多个 Agent 同时报告，结果与 Agent 处理次序无关 | `test_report_guard.py` | A-01 | R-S2-19、21、22 |
| AC-S2-12 | P0 | P0 | 有效确认判定：V1–V6 各至少 2 个正反用例；证据多数表决与 D3 `resolve` 共享向量全部一致；报告分类正确；覆盖、`T_90`、冗余度、`auc_undetected` 对构造轨迹与手算一致；子编队投影正确；阈值版本写入结果 | `test_evaluator.py` | — | R-S2-23–26、37、40、41、44 |
| AC-S2-13 | P0 | P0 | 算法隔离模式：示例 `Solver`（不超过 30 行）在 `cs-planar` `d1/n8` 跑通；平台确认规则 RC 与公共 `report` 构件同一实现（共享用例） | `test_isolated.py` | ADR-D4-17 | R-S2-27、61 |
| AC-S2-14 | P1 | P1 | 跟踪覆盖：构造用例（持有方全程跟住 → `c_k = 1`；持有方失效未交接 → `c_k` 截止于失效时刻；合法交接后接受方跟住 → 覆盖连续；`null_handoff` 后接受方跟住 → 不计覆盖；`|G_k| < G_min` → `u_k = s_k`；`|G_k| = 0` → `u_k = s_k`，不得取 `c_k = 1`；`t_apply > T_c` 的确认 → `u_k = 0` 且不建立持有）；`h_s` 边界：最后一次识别级检测距 t 恰为 `h_s` 时不计、略小于 `h_s` 时计；`reacq_s` 边界：接受方在 `reacq_s` 内、外首次识别级检测分别记交接成功与失败；交接成功率不进入 `U_CS` | `test_custody.py` | — | R-S2-33–35 |
| AC-S2-15 | P1 | P1 | `d3` 档（`n8`）满足 PA-1 至 PA-7 与 §5.11 预算，或不满足者从 P1 交付范围删除并登记；生成实例全部满足不可观测区间起点晚于 `T_c`；延迟确认检验：对每个官方 `d3` 闭环基线构造其"延迟确认"变体（首次具备确认条件后推迟 `δ ∈ {20, 60} s` 再报告），变体的 `U_CS` 不显著高于原基线（配对差 95% CI 下界 ≤ 0）；`w_t ∈ {0.3, 0.7}` 敏感性结果写入 pilot 报告 | pilot 报告（WP-D4-51） | AC-D4-50 | R-S2-12 |
| AC-S2-16 | P1 | P1 | `interactive` 场景集：同实例同种子下初态与外生驱动与 `exogenous` 逐位一致；警觉逃逸状态转移对构造用例正确；Δ 族在该场景集只报告总效应 | `test_interactive.py` | §8.5 | R-S2-62 |
| AC-S2-17 | P1 | P1 | `track_share_cs@1` 在 `cs-planar` `d3` 与网格族上产生确认报告与合法交接；原 `track_share` 动作流与 D3 运行逐字节一致（legacy 回归） | `tests/d4/baselines/test_track_share_cs.py` | AC-D4-09 | R-S2-49 |
| AC-S2-18 | P1 | P1 | 网格族：转换工具对 `crop-handoff-001` 两次转换的 `instance_sha256` 一致；磁带摘要校验通过；`L1Model` 输出与 D3 同种子同位姿下逐字节一致，适配层输出字段符合 §3.4 字段表（核心字段齐全、`ident` 与 `appearance` 存在性一致、`cov_xy` 与公式一致、无 `sig`）；证据解析与 D3 `resolve` 对拍一致；载体边界用例：`vehicle_C` 在 `t_board` 前的检测投票给车辆（`K_d`），之后投票给 `person_B` 对应目标，证据跨 `t_board` 的报告按多数规则裁决；黄金运行 GR-1、GR-2 的分量与期望一致；`holdout` 不满足 MC-1 至 MC-3 时登记为 `audit` | `tests/d4/suites/coopsearch/test_mesh.py`（`drone4d[hifi]` 环境） | AC-D4-22 旁证 | R-S2-53–57、60、73、74 |
| AC-S2-19 | P1 | P1 | 网格族安全判定与 `py:bench/eval/safety.py` 共享用例一致（构造穿越用例 `ranked_valid = false`）；legacy `found` 报告映射正确 | `test_mesh_safety.py` | — | R-S2-28、45 |
| AC-S2-20 | P1 | P1 | 条目卡字段齐全（§10.6 R-S2-46）；`board.v2` 中 S2 场景集可浏览 | 前端回归（WP-D4-23） | AC-D4-15 | R-S2-46 |
| AC-S2-21 | P1 | P1 | `Δ_perm` 的 `perm_struct` 只置换登记为 `world_state` 的字段，交互控制消息不被置换；合成策略上回收构造值 | `test_delta_perm_coopsearch.py` | AC-D4-26 | §7.5 |
| AC-S2-22 | P1 | P1 | X1 在 S2 上：P1 首批 F、C、P、L、J、S 六族扰动按外生种子配对，H 族被拒绝；S 族退化保持 CRN；`Held(o)`、`x1.response_ops` 与关联谓词按 §9.4 声明，S2 的 `T_detect` 黄金用例（F、C 各 2 个，X1 AC-RC-04）仅凭受扰与孪生 trace 计算并与人工标注一致；`U_cum(t)` 单调不减、`U_cum(T)` 与 `U_CS` 逐位一致（含 `w_t = 0` 与 `w_t = 0.5` 两类实例）；关闭提前终止前后 `U` 逐位相等；`stress` 子集不含 `n4` | `tests/d4/protocols/robustcoord/test_coopsearch.py` | AC-RC-1、AC-RC-4、AC-RC-22 | R-S2-69–72；§9.4 |
| AC-S2-23 | P1 | P1 | 机巢与能量：生成实例的机巢位置满足 R-S2-65 全部约束（逐实例 100%）；`energy_ratio` 落入 [0.4, 0.7]；构造用例：停靠中实体传感器停采、不产生覆盖、按 `charge_w` 充电且不扣减；停靠中持有方要约并被合法接受；`DEPLETED` 实体的要约与接受记 `null_handoff`、其持有的目标不再产生覆盖；`nest.dock.req` 的 `GRANTED`、`DENIED_BUSY`、`DENIED_DISTANCE` 与 M29 R-M29-72 一致 | `tests/d4/suites/coopsearch/test_nest_energy.py` | — | R-S2-65–68 |
| AC-S2-24 | P0（`n4`–`n16`）/ P1 / P2 | P0 / P1 / P2 | 规模预算：`cs-planar` 各规模与网格族的评测器墙钟与传感器模型耗时满足 §7.7 EV-7 表（RM-8 单核，各 5 次取中位）；覆盖指标以 1 Hz 计算；启用可见性缓存时，覆盖指标与直接判定的结果之差 ≤ 0.005（绝对值），否则禁用缓存 | `tests/perf/test_coopsearch_scale.py` | AC-D4-50、AC-D4-51 旁证 | R-S2-43、75 |

**与 AWR-06 AC-S2-1 至 AC-S2-6 的对照**：AC-S2-01 至 AC-S2-06 分别承接，内容按本文细化（AC-S2-01 增加评测器解析锚点与生成器统计检验；AC-S2-05 增加泄露、分区、`cut` 与状态迁移边界用例，P1 部分按 AWR-06 AC-D4-27 执行 FC-3 至 FC-7）。AC-D4-18 的通过条件以 AWR-06 为准：AC-S2-01 至 AC-S2-04、AC-S2-06。AC-S2-05 的 P0 部分（(a)–(n)）与 AC-S2-07 至 AC-S2-13 为 S2 内部的 P0 交付条件（WP-D4-16 验收），是否并入 AC-D4-18 由 AWR-06 裁决（§17.1 反馈 25）。

---

## 12. 实例样例

样例中的数值均为初值；实际实例以 JCS 规范化 JSON 写出并按 M31 的量化规则存储，此处为可读展开。

### 12.1 `d0` 锚点 A：解析首次确认时刻

**用途**：评测器单元测试与黄金 trace 回归。策略为测试专用的脚本策略 `scripted_waypoints@test`（不是基线）：`uav_00000` 以初速 10 m/s 起飞并每个决策 tick 下达 `goto_w{x_m: 200, y_m: 20, speed_mps: 10}`，检测到识别级 `sig = hv` 检测即在本决策 tick 提交确认报告；其余 3 架每 tick 下达 `hold{}`。初速使用 M28 R-M28-106 的 `initial_state.vel0_mps`，不依赖任何近似。

```yaml
# capl:cs-planar/anchor-00001/d0/n4@1（dev）
manifest: {family: cs-planar, coordinates: {block: anchor, index: 1, difficulty: d0, scale: n4, rev: 1}}
world/planar.json:
  bounds: [0, 0, 200, 200]
  region: [[0, 0], [200, 0], [200, 200], [0, 200]]
  obstacles: []
  no_fly: []
agents.json:
  entities:
    - {entity_id: uav_00000, role: wide, limits: {v_max_mps: 10, a_max_mps2: 3}, z_layer_m: 60, energy_cap_wh: null,
       initial_state: {pos_m: [0, 20], heading_rad: 0, vel0_mps: [10, 0]}}
    - {entity_id: uav_00001, role: wide, limits: {v_max_mps: 10, a_max_mps2: 3}, z_layer_m: 68, energy_cap_wh: null, initial_state: {pos_m: [0, 180]}}
    - {entity_id: uav_00002, role: wide, limits: {v_max_mps: 10, a_max_mps2: 3}, z_layer_m: 76, energy_cap_wh: null, initial_state: {pos_m: [200, 180]}}
    - {entity_id: uav_00003, role: wide, limits: {v_max_mps: 10, a_max_mps2: 3}, z_layer_m: 84, energy_cap_wh: null, initial_state: {pos_m: [200, 20]}}
sensing.json:
  sensors: [{role: wide, fov_deg: 360, r_s: 40, P_d0: 1.0, sigma_d: null, lambda_fa: 0, sigma0: 0, k_sigma: 0, r_id: 40, p_conf: 0, rate_hz: 10}]
brief.json:
  target_count: 2
  report: {gate_m: 10, rate_limit_s: 2, quota_agent: 2, stale_s: 10}
  tracking: null
  horizon_s: 120
  confirm_deadline_s: 120
objective.json: {primary: u_cs, w_t: 0, horizon_s: 120, confirm_deadline_s: 120}
hidden/targets.json:
  targets:
    - {target_id: tgt_00000, class: hv, motion: static, xy0: [103.5, 20]}
    - {target_id: tgt_00001, class: hv, motion: static, xy0: [150, 150]}
run: {clock_profile: d4_std, physics_step_ms: 20, decision_period_ms: 100, modeled_latency_ms: 100, network_profile: ideal@1}
```

**手算**（全部时刻为内核整数纳秒；位置按 M28 R-M28-53 积分）：

| 步骤 | 计算 |
|---|---|
| 运动 | 初速等于期望速度（`goto_w` 的期望速度 `min(10, v_max, √(2·a_max·(d − r_arrive)))` 在 `d − r_arrive ≥ 16.7 m` 时为 10 m/s，`t ≤ 6.5 s` 时 `d ≥ 135 m`），每步 `a = clip_norm((v_des − v)/Δp, a_max) = 0`，故 `x(t) = 10t`（双精度累加误差在 10⁻¹² m 量级，远小于下列判定余量 0.5 m） |
| 进入探测半径 | 与 `tgt_00000` 的平面距离 ≤ 40 m 当且仅当 `x ≥ 63.5`，即 `t ≥ 6.35 s` |
| 首次检测 | 采集时刻为 100 ms 的整数倍：`t = 6.3 s` 时 `x = 63`、距离 40.5 m，不检测；`t = 6.4 s` 时 `x = 64`、距离 39.5 m，检测（`P_d = 1`，`39.5 ≤ r_id`，识别级，`sig = hv`）；`t_det,0 = 6,400,000,000 ns` |
| 密封与决策 | 6.4 s 为决策边界，观测含 `capture_time_ns ≤ 6.4 s` 的检测（M28 R-M28-57）；策略在该 tick 提交确认报告，`pos_xy = (103.5, 20)`（噪声为 0），`t_ref = 6.4 s` |
| 生效 | `t_apply = t_sealed + ℓ_model = 6.4 + 0.1 s`，`τ_0 = 6,500,000,000 ns` |
| 第二个目标 | `tgt_00001` 到全部平台位置与航线的最小平面距离：到 `uav_00002` 为 √(50² + 30²) ≈ 58.3 m > 40 m；到 y = 20 航线 ≥ 130 m；不可被检测，`u_1 = 0` |
| 主指标 | `w_t = 0`，`u_0 = s_0 = 1 − 6.5/240 = 233.5/240 = 0.972916666…`；`U_CS = 0.486458333…` |
| 辅助 | `conf_rate = 0.5`；`conf_latency = 0.1 s`；`false_conf_rate = 0`；`sep_violation_ratio = 0` |

`eval/expected.json`：`{u_cs: 0.4864583333333333, tol_abs: 1e-9, tau_ns: {tgt_00000: 6500000000}, t_det_ns: {tgt_00000: 6400000000}, conf_rate: 0.5, tol_time_ns: 0}`。时间量要求精确相等：若实现误把 `t_sealed`（6.4 s）当作 `τ`，得 `U_CS = (1 − 6.4/240)/2 = 0.486666…`，与期望之差 2.08 × 10⁻⁴，远大于 `tol_abs`，判为不一致（AC-S2-01 (e) 的反例用例）。`x = 63.5 m` 不与采集时刻的位置重合，边界判定不受浮点累加影响。

**锚点 B**（同刻重复确认，`capl:cs-planar/anchor-00002/d0/n4@1`）：在锚点 A 中令 `uav_00001` 的 `initial_state = {pos_m: [0, 25], vel0_mps: [10, 0]}`，每 tick 下达 `goto_w{x_m: 200, y_m: 25, speed_mps: 10}`，与 `uav_00000` 同速平行飞行。其与 `tgt_00000` 的平面距离 ≤ 40 m 当且仅当 `x ≥ 103.5 − √1575 ≈ 63.81`，首个满足的采集同为 `t = 6.4 s`（距离 √(39.5² + 5²) ≈ 39.82 m）；两机在同一 tick 报告。期望：`τ_0 = 6,500,000,000 ns` 不变；首次有效确认按 `agent_id` 码点序归 `agent_00000`，其报告引用成为 `tok_0`；另一条记为重复确认，`dup_conf_rate = 0.5`；`U_CS` 与锚点 A 相同；两机层差 8 m，三维距离 ≈ 9.43 m，`sep_violation_ratio = 0`。

### 12.2 `cs-planar` `d2/n8` 标准实例

```yaml
# capl:cs-planar/b03-00042/d2/n8@1（参数区块 b03，validation 划分）
manifest:
  family: cs-planar
  generator: {id: gen/cs_planar, version: 1, digest: "sha256:…"}
  instance_seed_ref: {commit_ref: "split:validation#instance_set_commit"}
  difficulty:
    area_per_agent_m2: 75200          # 扣除障碍后
    occlusion_rate: 0.148
    sweep_time_ratio: 0.42            # 见注
    prior_entropy_ratio: 0.81
    speed_ratio: 0.15
    ident_ratio: 0.5
    partition_fraction: 0.35
    prior_drift_m: 71                # 估算：每段位移均方 R_w²/2 = 112.5 m²、平均段时长约 6.7 s，300 s 约 45 段
  # distractor_ratio、quota_ratio 由 |K_hv| 推出，d2 起移入 hidden/difficulty.json（本例 |K_hv| = 4：2.0、4.0）
world/planar.json:
  bounds: [0, 0, 800, 800]
  region: [[0, 0], [800, 0], [800, 800], [0, 800]]
  obstacles:                           # 12 个，略
    - {id: obs_00000, polygon: [[120, 140], [180, 140], [180, 210], [120, 210]], blocks_motion: true, blocks_los: true}
  no_fly:
    - {id: nfz_00000, polygon: [[420, 600], [520, 600], [520, 700], [420, 700]]}
agents.json:
  entities: [{entity_id: uav_0000k, role: wide, limits: {v_max_mps: 10, a_max_mps2: 3}, z_layer_m: 60 + 8·(k mod 4), energy_cap_wh: null, initial_state: {pos_m: [100k + 50, 20], heading_rad: 1.5708}}]   # k = 0..7；4 层循环复用
  agents_template: [{agent_id: agent_0000k, controls: [uav_0000k], scope: FULL}]
sensing.json:
  sensors: [{role: wide, fov_deg: 360, r_s: 30, P_d0: 0.8, sigma_d: 30, lambda_fa: 0.01, sigma0: 1.0, k_sigma: 0.01, r_id: 15, p_conf: 0.05, rate_hz: 10}]
brief.json:
  target_prior: {type: gmm, components: [{xy: [210, 620], sigma_m: 120, weight: 0.4}, {xy: [600, 250], sigma_m: 150, weight: 0.4}, {xy: [400, 400], sigma_m: 400, weight: 0.2}]}
  target_count: null
  hv_signature: {sig: hv}
  target_motion: {model: random_walk, params: {v_tgt: 1.5, leg_s: 10}}
  report: {gate_m: 10, rate_limit_s: 2, quota_agent: 2, stale_s: 10}
  tracking: null
  horizon_s: 300
  confirm_deadline_s: 300
  start: {uav_00000: {pos_m: [50, 20], heading_rad: 1.5708, z_layer_m: 60, role: wide}, …}       # 南侧起降带；agents.json initial_state 的公开投影
  partitions_public: false
comm.json:
  partitions: [{groups: [[agent_00000, agent_00001, agent_00002, agent_00003], [agent_00004, agent_00005, agent_00006, agent_00007]], t_start_s: 120, t_end_s: 225}]
objective.json: {primary: u_cs, w_t: 0, horizon_s: 300, confirm_deadline_s: 300}
hidden/targets.json:
  targets:
    - {target_id: tgt_00000, class: hv, motion: random_walk, params: {v_tgt: 1.5, leg_s: 10}, xy0: [233, 571]}
    - …                                                           # 共 4 个 hv（本实例由 {3, 4, 5} 抽得 4）
    - {target_id: tgt_00004, class: distractor, motion: random_walk, params: {v_tgt: 1.5, leg_s: 10}, xy0: [612, 288]}
    - …                                                           # 共 8 个 distractor
budget: {tx_bytes: 19660800, cpu_ms_per_agent: 30000, reports_per_agent: 2}
```

注：`sweep_time_ratio = |Q_free| / (n · 2 r_s · v_max) / T_c`，`|Q_free| ≈ 601,600 m²`、`n = 8`、`2 r_s = 60 m`、`v_max = 10 m/s`，理想无重叠扫描时间约 125.3 s，与 `T_c = 300 s` 之比约 0.42；正式实例的难度量由 `Generator.difficulty` 计算写入并经 V-I-14 校验。考虑 `P_d(d)` 随距离衰减后有效扫描宽度小于 `2 r_s`，实际一次全覆盖所需时间更长；pilot 中若 PA-4 不满足，优先以 `r_s`、区域边长与 `ρ_v` 做相变扫描。团队通信额度 `8 × 300 × 8192 = 19,660,800 B`；CPU 额度每 Agent `300 × 100 = 30,000 ms`。

### 12.3 `cs-handoff-mesh`：基于 `crop-handoff-001` 的跟踪交接实例

```yaml
# capl:cs-handoff-mesh/crop-00001/d3/n4@1（dev 划分）
manifest:
  family: cs-handoff-mesh
  generator: {kind: fixed, converter: mesh_from_event@1, event_ref: "crop-handoff-001@1"}
world/ref.json: {world_id: oryahovo-crop1, contentVersion: "…", region_ids: [ev_town_center]}
tape/ref.json: {event_ref: "crop-handoff-001@1", tape_content_sha256: "…"}
agents.json:
  entities:
    - {entity_id: uav_00000, kind: multirotor, vehicle_ref: x500, role: wide, energy_cap_wh: null, initial_state: {pos_m: [76.0, -78.0, 0.0], heading_rad: 1.5708}}
    - {entity_id: uav_00001, kind: multirotor, vehicle_ref: x500, role: wide, energy_cap_wh: null, initial_state: {pos_m: [76.0, -82.0, 0.0], heading_rad: 1.5708}}
    - {entity_id: uav_00002, kind: multirotor, vehicle_ref: x500, role: wide, energy_cap_wh: null, initial_state: {pos_m: [76.0, -86.0, 0.0], heading_rad: 1.5708}}
    - {entity_id: uav_00003, kind: multirotor, vehicle_ref: x500, role: tele, energy_cap_wh: null, initial_state: {pos_m: [76.0, -90.0, 0.0], heading_rad: 1.5708}}   # 第 4 个起降点待核验
sensing.json:
  sensors:
    - {role: wide, camera: {width: 1920, height: 1080, hfov_deg: 60, rate_hz: 10}, l1_params_ref: "L1Params@d3"}
    - {role: tele, camera: {width: 1920, height: 1080, hfov_deg: 9.1, rate_hz: 10}, l1_params_ref: "L1Params@d3"}
brief.json:
  region: ev_town_center                       # 310 m × 280 m
  target_prior: {type: gmm, components: [{xy: [13, -41], sigma_m: 30, weight: 0.25}, {xy: [70, -56], sigma_m: 30, weight: 0.25}, {uniform_over: region, weight: 0.5}]}
  hv_signature:
    - {label: target_1, class: person, appearance: {top_rgb: [42, 93, 176], bottom_rgb: [40, 40, 40]}}
    - {label: target_2, class: person, appearance: {top_rgb: [176, 52, 44], bottom_rgb: [70, 70, 80]}}
  carrier_rule: true                           # 公开规则：目标可能登乘车辆；不披露载体、时刻与地点
  sensing_public: {range_kind: slant, det_ext: [class, bbox_xywh, h_px, appearance]}
  report: {gate_m: 10, rate_limit_s: 2, quota_agent: 2, stale_s: 10}
  tracking: {window_s: 60, g_min_s: 10, hold_s: 2, handoff_window_s: 10, reacq_s: 10, require_role: tele}
  horizon_s: 360
  confirm_deadline_s: 300
  altitude_envelope_agl_m: [30, 120]
objective.json: {primary: u_cs, w_t: 0.5, g_min_s: 10, horizon_s: 360, confirm_deadline_s: 300}
hidden/targets.json:
  actor_map: {tgt_00000: person_A, tgt_00001: person_B}
  targets:
    - {target_id: tgt_00000, class: hv, motion: exogenous_path, unobservable: [[290.0, 360.0]]}    # 进入 Z 楼
    - {target_id: tgt_00001, class: hv, motion: exogenous_path}
  carriers: [{target_id: tgt_00001, carrier_id: vehicle_C, t_board_s: 255.0, t_alight_s: 330.0}]     # 未下车，t_alight 取载体驶离时刻
  distractors: {source: tape.background, count: 40, plus: [vehicle_C]}                               # vehicle_C 在登乘区间外属 K_d
  confirm_profile:
    tgt_00000: {observable: [[0.0, 290.0]], short_window_confirm: [[280.0, 300.0]]}                   # τ ∈ (280, 300] 时 |G_k| < 10 s，u_k = s_k
    tgt_00001: {observable: [[60.0, 330.0]], short_window_confirm: []}                                # τ ≤ 300 时 |G_k| ≥ 30 s
```

| 说明项 | 内容 |
|---|---|
| 公开锚点到先验 | `ev_plaza` 质心 (13, −41) 与 `ev_building_z_door` (70, −56) 为高斯分量中心（CF-01、CF-02） |
| 高价值目标 | `person_A`、`person_B`；D3 简报只描述 `person_A`，S2 实例另给出 `person_B` 外观，D3 任务契约不受影响 |
| 可观测区间 | `person_A`：出生（0 s）至 290 s；`person_B`：60 s 出生，255 s 登车后以 `vehicle_C` 继承，至 330 s 驶离（CF-20） |
| 简报披露 | 简报只给出两名人员的外观与公开规则 `carrier_rule`；Agent 只有在观测到登乘过程或依外观以外的线索推断时，才能把对 `vehicle_C`（`appearance.top_rgb ≈ [210, 210, 214]`，CF-19）的检测作为 `person_B` 的证据 |
| 交接需求 | `person_B` 登车后速度可达 11 m/s（磁带 `gait.speed_mps` 上限），长焦平台以近距离跟随时易丢失，形成交接与换位需求；`person_A` 290 s 进楼后不再计覆盖 |
| 短窗口与晚确认 | `person_A` 的不可观测区间起点 290 s 早于 `T_c = 300 s`，不满足 `cs-planar` 的 R-S2-12 约束，因而逐事件登记于 `confirm_profile`：`τ ∈ (230, 280]` 时 `|G_k|` 在 10–60 s 之间，`c_k` 按交集计算；`τ ∈ (280, 300]` 时 `|G_k| < G_min`，`u_k = s_k ≈ 0.51–0.53`，低于早确认可得的分值（例如 `τ = 100 s`、`c_k = 0.7` 时 `u_k ≈ 0.77`），不再出现 v0.1 中 `c_k = 1` 奖励晚确认的情形。`(230, 280]` 区间内仍存在以短窗口取得较高 `c_k` 的可能，但 Agent 不知道目标将于何时进楼（磁带属真值），该激励无法被定向利用；pilot 以 AC-S2-15 的延迟确认检验复核，结果写入 `validity.md` |
| 机巢与能量 | 网格族 P1 不含机巢与能量约束（全部实体 `energy_cap_wh = null`）；长焦的稀缺只来自数量与视场 |
| 第 4 起降点 | (76, −90) 未经核验；须按 CF-03 运行规格注释的判据（网格面 ±0.7 m 内高差 ≤ 3 cm，距全部演员路径 ≥ 15 m）复核后方可使用 |
| 识别距离量级 | `wide`（HFOV 60°）`fx ≈ 1662.8 px`，身高 1.7 m 的人员满足 16 px 识别条件的斜距约 ≤ 177 m；`tele`（HFOV 9.1°）`fx ≈ 12,064 px`，同条件斜距超过 `L1Model` 的 400 m 上限。因此网格族中"确认须长焦"是规则约束（`require_role = tele`），不是物理不可能（反馈 19） |

---

## 13. 有效性声明要点（`validity.md` 必含条目）

| 类别 | 条目 |
|---|---|
| 模型不反映的现实因素 | 平面族视线为二维、不含障碍高度与地形；地面足迹以圆盘或扇形抽象，不含高度—分辨率折中；检测概率只依赖距离，不含角度、光照与目标尺寸；目标运动为随机游走或脚本轨迹，不代表真实行为分布；警觉逃逸为简化规则 |
| 缺省参数来源 | 全部传感、报告、跟踪与交接参数为设计初值，经 WP-D4-17 与 WP-D4-51 pilot 校准后冻结；网络参数集标 `synthetic-stress` |
| Benchmark 约定 | 跟踪持有（custody）规则与"要约方为当前持有方"的合法性判据、持有方失效后不可经重新确认恢复、`d3` 起确认须长焦证据、每 Agent 报告配额 `quota_agent = 2`、`w_t = 0.5` 的取值及其依据（§7.1）、`G_min = 10 s` 的短窗口处置、停靠期间传感器停采，均为评测约定而非物理规律 |
| 已知局限 | 协作增益经难度设计筛选（PA-2、PA-3 要求协作有利），Δ 族量级只在本 Suite 内可比；网格族为固定场景集，结论不外推；网格族留出集与开发集可能同一地理区，此时只作审计（§4.2、§4.3 MC-3）；`short_window_frac`（`|G_k| < G_min` 的占比）；`random_walk` 在边界与障碍附近的反射性偏置及 `prior_drift_m`；`d2` 构造效度对照（§8.4）的结论，包括干扰目标与 `p_conf` 对排名的实际影响；交接视图分歧（`split_view_count`）的出现频率；`w_t ∈ {0.3, 0.7}` 的排名敏感性 |
| 承接说明 | 本 Suite 与 `drone4d-event@1` 的指标不可换算；`track_share_cs@1` 与 D3 `track_share` 的差异；网格族观测经适配层接入，`L1Model` 本体不变 |

---

## 14. 工作包与里程碑映射

| 工作包（AWR-06） | 周次 | S2 内容 | 本文需求 | 本文验收 |
|---|---|---|---|---|
| WP-D4-06 | 第 6–7 周 | 交接仲裁规则（S2 `ArbiterRules`，含不泄露真值的处置、状态迁移表 E1–E9、语法校验） | R-S2-29–32、36、76 | AC-S2-05（P0 部分） |
| WP-D4-10 | 第 4–6 周 | `drone4d-event@1` 包装；S2 清单框架 | R-S2-01–03、59 | AC-S2-06、07 |
| WP-D4-13 | 第 8–10 周 | S2 参考策略缓存 | R-S2-50 | AC-S2-03 |
| WP-D4-16 | 第 7–9 周 | `cs-planar` 生成器、传感覆盖参数、评测器、报告守卫、P0 基线、算法隔离执行器 | R-S2-04–11、13–27、37–44、47、48、51、61、64、75（`n4`–`n16`） | AC-S2-01、04、07–13、24（P0 部分） |
| WP-D4-17 | 第 10 周（S2） | 区分度 pilot（含 §8.4 构造效度对照）、场景数冻结；评分与规则参数按预注册取定，只报告敏感性 | R-S2-52、63 | AC-S2-02、03 |
| WP-D4-19 | 第 12–13 周 | X1 在 S2 上的落点：适用族、`Held(o)`、`x1.response_ops`、`U_cum(t)`、提前终止开关 | R-S2-69–72；§9.4 | AC-S2-22 |
| WP-D4-20 | 第 12–14 周 | S2 消息字段角色登记与 `perm_struct` | §7.5 | AC-S2-21 |
| WP-D4-21 | 第 14–17 周 | S2 留出集冻结与区块隔离 | R-S2-60、63 | AC-S2-03（留出集复核） |
| WP-D4-23 | 第 18–20 周 | S2 条目卡 | R-S2-46 | AC-S2-20 |
| WP-D4-25 | 第 12–14 周 | `cs-handoff-mesh`：转换工具、`hosts/hifi` 装载、观测适配层、证据对拍（含载体映射）、黄金运行与成榜条件 | R-S2-28、45、53–57、73、74 | AC-S2-18、19 |
| WP-D4-27 | 第 14–17 周 | 网格族跨保真度审计 FC-3 至 FC-7 | R-S2-58 | AC-S2-05（P1 部分） |
| WP-D4-51 | 第 17–19 周 | S2 `d3` 档、`n32`、持有与跟踪覆盖、机巢与能量、`interactive`、`track_share_cs@1`、公共 `custody` 构件、`n32` 规模预算 | R-S2-12、33–35、49、62、65–68、75（`n32`） | AC-S2-14–17、23、24（P1 部分） |
| WP-D4-52 | 第 22–23 周 | S2 `n64` 规模预算与 `d4` 档 | R-S2-75（`n64`） | AC-S2-24（P2 部分） |
| WP-D4-41 | 第 22–30 周 | PAPER E4（网格族跨保真度）、E8（S2 鲁棒性与规模） | — | PAPER |

降级关联：AWR-06 §10.2 降级 ② 顺延 S2 闭环场景集（`.cl`）至 P1，保留 `.ai`；降级 ③ 顺延 S2 整体。触发 ② 时 AC-S2-02、AC-S2-03 只在 `.ai` 场景集上判定，AC-S2-11 的泄露用例保留（算法隔离模式同样提交报告）。

---

## 15. 风险与待决

### 15.1 风险

| 编号 | 风险 | 触发指标 | 应对 |
|---|---|---|---|
| RK-S2-01 | 区分度坍塌：覆盖时间远小于 `T_c` 使全部基线 `U_CS` 接近上限，或遮挡与虚警使全部基线接近 0 | PA-4 不满足；`sweep_time_ratio` 偏离 0.2–1.5 | 以 `sweep_time_ratio`、`ρ_v`、`r_id` 做相变扫描（PA-7） |
| RK-S2-02 | 测到的是确认规则技巧而非协同；或干扰目标与 `p_conf` 不构成实际的确认难度 | 算法隔离与闭环场景集排名 Spearman < 0.5；`false_conf_rate` 与 `U_CS` 高度相关；§8.4 构造效度对照中 `ΔU_d` 的 90% CI 落入 `[−ε_eq, ε_eq]` | 算法隔离模式固定确认规则；条目卡并列 `false_conf_rate`；按 §5.3 可调清单在 `dev` 上收紧 `p_conf`（随距离增大）或提高 `distractor_ratio`；结论写入 `validity.md` |
| RK-S2-03 | 持有规则被视为人为约定而影响外部接受度 | 外部复现团队（WP-D4-30）反馈 | `validity.md` 明示；`c_k` 的"任一长焦覆盖"口径作为诊断并列报告，供对比 |
| RK-S2-04 | 网格族样本不足：D3 交付的事件数少于 6 个，FC-5 秩相关不稳定 | 可用场景 < 6；FC-5 的 ρ 95% CI 宽 > 0.5 | 以全部可用场景执行并标注；增加同一事件的外生种子数；H5 结论限定 |
| RK-S2-05 | 网格族评测成本：覆盖栅格的网格视线判定开销 | 单 Episode 评测 > 30 s（§7.7 EV-7 表的网格族评测器预算；网格族不适用 PA-6 的 45 s，其单 Episode 宿主预算另列） | 1 Hz 降采样、5 m 栅格、只对视域内单元判定；覆盖指标标为诊断 |
| RK-S2-06 | 网格族自身在 `L1 + kinematic` 与 `L2 + realistic` 两档之间的配对差系统偏差过大（FC-4 比较的是同一族同一基线在两档上的配对差，不是平面族对网格族） | FC-4 90% CI 不落入 [−0.05, 0.05] | 标注"保真度敏感"；以 `L2` 审计样本回标定网格族 `L1` 档的检测与识别判据（以非缺省的 `L1Params` 实例经 `L1Model(seed, world, tape, cam, params)` 构造参数传入，`py:agentsdk/env/l1.py` `L1Model.__init__`；不改 `L1Params` 缺省值，D3 运行不受影响），回标定后的参数作为网格族的新参数版本登记；平面族 `P_d(d)` 不因 FC-4 调整 |
| RK-S2-07 | WP-D4-16 工作量不足 | 第 8 周末 S2 关键用例通过率低于计划 | 按反馈 14 调整估算；`voronoi_lloyd@1` 与 `shared_visited_greedy@1` 可顺延至 P1 第 1 周（S 线余量） |

### 15.2 待决

| 编号 | 问题 | 建议 |
|---|---|---|
| Q-S2-01 | 网格族是否纳入 `target_search` 类事件 | D3 封板时已交付则纳入；未交付则 P1 只用 `rendezvous_handoff` 类，P2 再评估 |
| Q-S2-02 | `w_t` 的取值（v0.2 已关闭） | 依研究问题取 0.5 并写入预注册（§7.1）；pilot 只报告 `w_t ∈ {0.3, 0.7}` 的排名敏感性，不据区分度改选 |
| Q-S2-03 | `c_k` 口径（持有方口径或任一长焦口径） | 主指标采用持有方口径；任一长焦口径作为诊断并列，pilot 后复核 |
| Q-S2-04 | 每 Agent 配额 `quota_agent = 2` 是否合适 | 配额是规则参数，不在 PA-7 可调清单内；pilot 只统计 `quota_used` 与逐 Agent 最大值的分布并写入 pilot 报告。若 P95 的逐 Agent 用量 = 2 且被拒报告中含有效确认的比例 > 10%，提请在预注册阶段（`holdout` 冻结前、以 `dev` 证据）修订，并记入 Suite 主版本变更 |
| Q-S2-05 | 网格族的算法隔离模式 | 待 `KinematicTier` 路径跟随与平面执行器语义对齐后评估（P2） |
| Q-S2-06 | 平面族是否开放 `2.5d` 视线 | M28 Q-M28-07 建议 `d3` 需要时 P1 评估；S2 `d3` 先以 `2d` 交付 |

---

## 16. 追溯

| AWR-06 条目 | 本文落点 |
|---|---|
| §7.2 场景族表 | §1.2、§4 |
| §7.2 形式化定义 | §3 |
| §7.2 标准实例字段 | §6 |
| §7.2 难度与规模 | §5 |
| §7.2 核心指标 | §7 |
| §7.2 经典基线 | §8 |
| §7.2 所需平台能力 | §3.4、§3.7、§4、§10 |
| §7.2 验收 AC-S2-1 至 AC-S2-6 | AC-S2-01 至 AC-S2-06 |
| §7.0 共同约定、PA-1 至 PA-7 | §5、§5.3 可调清单、§7.1 唯一主指标、R-S2-02、63、AC-S2-01 至 AC-S2-04、AC-S2-07 |
| §7.4 X1 | §9.4、R-S2-69 至 R-S2-72、AC-S2-22 |
| §5.11 性能与规模预算 | §3.4、§7.7 EV-7、R-S2-43、75、AC-S2-24 |
| §8.4 指标体系与 `S_task` | §7.1（唯一主指标、`u_search` 诊断） |
| §1.7、§3.4 B-06、B-07、B-08、B-10 | §2、R-S2-05、53、59 |
| §5.5 RN-2、RN-7 | §3.3、§3.4、R-S2-18、56 |
| §5.8 FC-1 至 FC-7；AC-D4-27 | §4.2、R-S2-58、AC-S2-05 |
| §6.7 非网络交互 | §3.7（含状态迁移表）、§3.10、R-S2-29 至 R-S2-36、R-S2-65 至 R-S2-68、R-S2-76 |
| §8.5 Δ 族 | §7.5 |
| §8.6 统计协议 | §9.3 |
| §8.7 划分与隔离 | §4.2、§4.3、R-S2-60 |
| §8.8 A-01、A-10 | §3.6、§3.7、R-S2-22、30 |
| §9.1 方向 A、B、C；§9.2 H5、E4、E8 | §1.1、§14 |
| §10 WP-D4-06、10、13、16、17、19、20、21、23、25、27、41、51、52 | §14 |
| ADR-D4-11、14、15、16、17、21、22、23、24、26、33 | §3、§7、§9 |

---

## 17. 对 AWR-06 的反馈

### 17.1 对 AWR-06 的反馈

| 编号 | 问题 | 建议 | 理由 |
|---|---|---|---|
| 1 | §7.2 形式化定义称"`cs-planar` 的 `d0`–`d2` 中 `K_hv = K`"，同节难度表 `d2` 行又列出"干扰目标"，二者矛盾 | 改为"`d0`–`d1` 中 `K_hv = K`；`d2` 起引入干扰目标与识别半径 `r_id`"（本文 §5.1） | 干扰目标是"确认"区别于"检测"的前提；无干扰目标时确认退化为检测，`d2` 的确认难度旋钮失效 |
| 2 | §7.2 基线表把 `π_ind` 写作"独立 lawnmower（各自全区扫描）"，与 §7.0、§8.5 对 `π_ind` 的定义"静态分区 + 单机（最强）策略的公开版本"不一致；若各机无约定地重复全区扫描，`π_ind` 偏弱，`Δ_ind` 被高估 | §7.2 改为"全区航带序列按 `index/n` 错相起点（公开约定的静态时间分区），零通信"，与 M30 `independent_lawnmower@1` 一致 | `Δ_ind` 的解释是"比一个合理的独立团队多获得多少效用"，对照组须是合理的独立团队 |
| 3 | §7.2 称 `cs-handoff-mesh` 为"D3 crop-handoff 的直接泛化"，但 crop-handoff 的 handoff 是被观测对象之间的人员交汇与交接（`rendezvous_handoff`），S2 的 Handoff 是平台之间的跟踪交接，任务语义不同 | 改述为"复用 D3 crop-handoff 的世界包、事件磁带、匿名检测与证据审核机制及 `track_share` 协议，任务语义为搜索、确认与跟踪交接"，并在术语表区分两种 handoff | 避免读者误以为 S2 成绩可与 D3 交汇与交接赛道成绩对照 |
| 4 | §7.2 把 `track_share` 列为 S2 闭环基线、实现来源为 `sdk:baselines/track_share.py`；该基线的报告规则只有 `meet`、`board`、`enter_building`（`sdk:baselines/common.py` `reports`），不产生 `found` 报告，`act.v1` 也不承载交接操作，原样投影时 `U_CS` 恒为 0 | 新增 `track_share_cs@1`：保留原协议常量，增补确认报告与显式交接；原 `track_share` 只作 `drone4d-event@1` 参考基线与 legacy 回归对象 | 基线须能在本 Suite 上得分才有对照意义；原实现不修改符合 DP-12 |
| 5 | §7.2 规定 `d3` 起闭环场景集把"持续跟踪覆盖率与交接成功率"按 pilot 权重并入 `U_CS`。若覆盖率按持有方计算，交接成功已体现在覆盖率中，再计入交接成功率构成重复计分；且交接成功率可通过频繁的无意义交接抬高 | `U_CS^trk = (1/|K_hv|) Σ [(1 − w_t) s_k + w_t c_k]`，交接成功率只作诊断（本文 §7.1、§7.2） | 主效用应只含任务结果；过程指标进入主效用会被直接优化 |
| 6 | 跟踪窗口若在 Episode 终点截断，临近终点的确认只需跟踪很短时间即可得到高 `c_k`，形成"推迟确认"的激励 | 引入确认截止 `T_c = horizon − W_trk`，跟踪窗口完全落在 Episode 内（本文 §3.7） | 消除截断偏差，使 `c_k` 在各目标间可比 |
| 7 | §6.7 交接行只写"双方在 `handoff_window` 内完成；超时 `EXPIRED`；交接成功写入真值层"，未规定合法性判定是否影响对 Agent 可见的结果；若不合法要约得到 `DENIED_*` 回应，Agent 可借此推断其确认报告是否有效，构成 §8.8 A-01 所禁止的"某动作是否有效"免费查询。M29 R-M29-73 与 §6.6.2 现行文本已按本条处理（合法性只写真值层、交接不用任何 `DENIED_*`、接受通知经网络承载） | §6.7 交接行与通用规则补记：合法性只在真值层生效（`null_handoff`），对 Agent 可见的事件序列与合法性无关；与 M29 R-M29-73 对齐 | 防止经交互仲裁旁路获取真值；使 06 与 M29 现行文本一致 |
| 8 | §7.2"所需平台能力"引用 `py:sim/targets/` 实现 `interactive` 警觉逃逸；该包不在 B-10 白名单内，且含 numba 核（`kernels_motion.py` 等）并经 `awr.contracts._paths.contracts_root` 读取仓库契约，无法进入 numpy-only 核心与 wheel | 警觉逃逸在 S2 包内以 numpy 自含实现，语义参照 D2 感知态；§7.2 的能力列表删除 `py:sim/targets/` 或改为"语义参照" | 与 B-02、B-10、ADR-D4-33 一致 |
| 9 | §7.2 规定 `cs-handoff-mesh` 沿用 D3 证据审核（`py:bench/eval/eu.py`），而 B-10 只允许 `awr.benchmarks.suites.drone4d_event` 与 `awr.evaluation` 的 D3 兼容入口 import `awr.bench`；`eu.py` 还在函数内导入 scipy | 明确 S2 自含实现证据多数表决并以共享测试向量与 `resolve` 对拍（本文 R-S2-24、57） | 避免 S2 包违反白名单或把 scipy 带入核心 |
| 10 | §7.2 难度表把网络档写入难度档（`d0` 用 `ideal@1`、`d1` 双档、`d2` 用 `abstract-radio@1 default`），标准实例字段含 `comm_model`；网络档是赛道坐标，不应进入实例（与 M31 反馈第 7 条同） | 难度表的网络档改为"锚点网络档"注释；实例字段 `comm_model` 改为 `partitions[]` 与 `comm_zones[]`（外生通信特征） | 同一实例须能在多网络档下配对运行（H3、E3） |
| 11 | §7.2、§3.4 B-02 与 WP-D4-25 使用 `MeshWorld` 一名，代码中的类为 `MeshWorldQuery`（`py:world/mesh/query.py`，继承 `DsmWorldQuery`） | 统一为 `MeshWorldQuery`，或在 RFC 中声明 `MeshWorld` 为待新建的包装类 | 名称与代码不一致会使依赖闭包审计与白名单条目失准 |
| 12 | 网格族的实例供给依赖 D3 P0b 的四类事件；工作副本中 `target_search` 只在契约枚举与生成类型中出现，未见事件生成实现 | 在 §10.1 G0 检查清单中登记"D3 封板时交付的事件类型与数量"，并规定网格族在 `target_search` 缺失时的降级范围（只用 `rendezvous_handoff` 类） | 网格族的场景数直接决定 FC-5、H5 的统计强度 |
| 13 | §7.0 规模档的恒密度规则（区域边长 ∝ √n）无法用于固定世界的网格族；`d3` 的 2:1 广角/长焦配比在 `n4`、`n8` 下不是整数 | 网格族声明固定规模标签并豁免恒密度规则；长焦数取 `round(n/3)`（本文 §4.2） | 使规模档登记规则对固定世界族有确定含义 |
| 14 | WP-D4-16 的 1.5 人周覆盖生成器、带视线的覆盖评测器、报告守卫与公共 `report` 构件、算法隔离执行器与 7 个基线（其中 5 个新写），明显少于 S1（WP-D4-14，3.0 人周）；WP-D4-06 的交接仲裁 0.5 人周未含不泄露真值的处置与持有表 | WP-D4-16 调整为 2.5 人周，或把 `voronoi_lloyd@1`、`shared_visited_greedy@1` 列为可顺延至 P1 第 1 周的项；WP-D4-06 增 0.25 人周 | 估算偏低会在第 8 周触发降级 ②，使 S2 闭环场景集顺延 |
| 15 | §8.7 要求同一建筑及相邻区块不跨划分；网格族只有 crop 一个地理区，M23 FR-082 的 test 事件也在 crop 上，`holdout` 无法满足地理隔离 | 网格族 `holdout` 优先使用 D3 town 世界；不具备时在 `validity.md` 与条目卡上标注"同一地理区留出"，并降低该榜的隔离等级声明 | 隔离规则是留出集可信度的核心，例外须显式声明 |
| 16 | §7.2 主指标中"首次有效确认时刻 `τ_k`"未指明取报告提交时刻、密封时刻还是生效时刻 | 取报告生效时刻 `t_apply = t_sealed + ℓ_model`（本文 §3.6） | 不同取法在 `ℓ_model` 消融中会改变 `U_CS`，须唯一 |
| 17 | §7.0 规定"虚警与违规以速率限制与违规规则处理"，§7.2 只给出 `report{gate_m, rate_limit}` 字段，未规定速率、配额与超限处置；无配额时策略可对每个检测立即报告而不付代价 | 规定每 Agent 速率限制与每 Agent 配额（`quota_agent = 2`，与目标数无关），超限报告被拒绝而不判违规（本文 §3.6）。v0.1 建议的团队配额 `2n` 撤回：剩余团队配额与 `quota` 回执随队友报告变化，构成不受分区与 `cut` 约束、不计费的带外信道 | 使"不线性扣分"的前提下虚报仍有机会成本，配额既不泄露目标数，也不泄露他机活动（§8.8 A-01） |
| 18 | §7.2 `d3` 行把"警觉逃逸（`interactive`）"与"`cs-handoff-mesh` 族"并列；D3 事件运行时在 P0a 拒绝 `interactive`（`py:sim/events/runtime.py`，原因码 611） | 明确 `interactive` 在 P1 只用于 `cs-planar`；网格族待 D3 实现 `interactive` 后再评估 | 避免 P1 交付范围包含不可用的上游能力 |
| 19 | §7.2 规定 `d3` 起"须由长焦平台确认"；在网格族中 D3 `L1Model` 的广角相机（HFOV 60°）对 1.7 m 人员满足 16 px 识别条件的斜距约 177 m，广角平台物理上可完成识别 | 把长焦要求明确为评测规则 `confirm.require_role = tele`，并在 `validity.md` 中声明其为取证标准约定 | 规则与物理能力的区别须对研究者透明 |
| 20 | §9.2 E4 写作"S2 `cs-handoff-mesh` 的 6 个基线"，但 §7.2 未规定网格族的基线子集（`shared_visited_greedy`、`voronoi_lloyd` 依赖平面栅格，未声明适用网格族） | 网格族基线为 `independent_lawnmower`、`partition_lawnmower`、`frontier_ig`、`track_share_cs`、`central_belief` 与 `random`（本文 §8.1），E4 的基线数据此登记 | E4 的样本量与成本估算依赖基线数 |
| 21 | X1 的 P 族分区与 S2 实例自带的计划分区（`d2` 的 90–120 s 子团队分区）可能同时作用，§7.4 未规定叠加规则 | 有向掩码取逻辑与；X1 规格在宿主场景已有分区时声明扰动强度的计量方式 | 叠加规则不定会使 `S_rob` 在不同实现间不一致 |
| 22 | §7.2 的形式化定义中持续跟踪"≥ 60 s"与"跟踪中通常需要跨平台交接"未给出可审计的跟踪责任对象，评测器无法区分"一机持续跟踪"与"多机冗余围观" | 引入真值层跟踪持有表（custody）与持有令牌，覆盖率只计持有方的识别级检测（本文 §3.7）；在 §6.7 交接行补充"持有表由 Suite 规则定义" | 使交接成为有计分意义的协同行为，并与 M29"双方跟踪持有表更新"的表述衔接 |
| 23 | §8.4 的跨 Suite 摘要 `S_task = 100 · (1/|S|) Σ_s Ê[U_s]` 对每个 Suite 只取一个 `Ê[U_s]`，但一个 Suite 可有多个成榜场景集（S2：`cs-planar.holdout.ai`、`.cl`、`.cl.trk` 与网格族），未规定取哪一个 | 各 Suite 清单声明 `summary_scenario_set`（S2 建议取 `cs-planar.holdout.cl` 的 `d2/n8` 主榜部分），`S_task` 只取该场景集；其余场景集在条目卡上分列 | 主指标唯一（§7.0）只保证每个场景集的 `U` 有唯一定义，不保证 Suite 级摘要唯一；不声明时不同实现会得到不同的 `S_task` |
| 24 | §7.0 PA-7 列举的可调参数（任务到达率、截止期、联盟比例、遮挡率、目标速度比、预算比）以 S1 为例，不含 S2 的识别半径、签名混淆、干扰目标比例等感知类旋钮；也未明确评分函数参数（如 S2 的 `w_t`）与规则参数（配额、门限）是否允许按区分度调整 | PA-7 改为"只调整 Suite 在 pilot 计划中声明的生成器公开参数"，各 Suite 列出可调清单（S2 见本文 §5.3）；明确评分与规则参数不得以区分度为标准选取，只在预注册中依研究问题取定、pilot 报告敏感性 | 以评分权重追求基线可分会使 PA-2、PA-3 失去验证意义；可调清单显式化便于审计 |
| 25 | §11 AC-D4-18 只要求 AC-S2-1 至 AC-S2-4、AC-S2-6；S2 的 P0 交付还依赖清单校验、依赖边界、传感模型、CRN、泄露防护、评测器判定与算法隔离模式（本文 AC-S2-07 至 AC-S2-13）以及交接规则的 P0 部分（AC-S2-05 (a)–(n)），这些未进入任何平台级验收 | AC-D4-18 增列"AC-S2-05 的 P0 部分与 AC-S2-07 至 AC-S2-13"，或在 WP-D4-16 的验收列中引用 | 泄露防护与依赖边界是 P0 门的必要条件，只在 Suite 规格内部登记时可能在 P0 门评审中被遗漏 |
| 26 | §7.2 场景族表与 AC-S2-5 写作"FC-3 至 FC-6"，而 §5.8 增设 FC-7（`d4_quick` 与 `d4_std` 的时钟档一致），AC-D4-27 与 WP-D4-27 要求 FC-3 至 FC-7 | §7.2 的两处统一改为"FC-3 至 FC-7" | 避免 Suite 级验收与平台级验收的范围不一致 |
| 27 | §7.2 `d2` 规定"目标数不公开"，但若 `|K_hv| = n/2` 由规模档确定，目标数可由公开的 `n` 直接推出，"不公开"不起作用；由 `|K_hv|` 推出的难度量（如配额比、干扰目标比）若写入公开的 `difficulty{}` 也会泄露目标数 | `d2` 起 `|K_hv|` 按实例在期望值 `n/2` 附近抽样（本文 §5.2），由其推出的难度量写入隐藏部分 | 使目标数不公开的设计意图在实例层面成立 |

### 17.2 对并行文档的反馈

下列各条是 S2 v0.2 依赖、但并行文档现行版本未覆盖或互不一致的事项；S2 不修改这些文档，以本节提请对应作者处理。

| 编号 | 对象 | 问题 | 建议 | 理由 |
|---|---|---|---|---|
| P-01 | M28 R-M28-55 | 能量模型只规定扣减，未规定停靠充电，S2 `d3` 的机巢换班无法计算 | 增充电项 `energy_uwh' = min(cap, energy_uwh + round(charge_w·Δp_s/3600·10⁶))`，停靠期间不扣减 `P0`、`P1` 项；`charge_w` 为按种类登记的属性，可由实例覆盖；`nest_outage` 期间按 R-M29-72 置 0 | S2 §3.10、R-S2-67；与 M29 R-M29-72"充电速率由 M28 能量模型置 0"的表述衔接 |
| P-02 | M28 R-M28-59、R-M28-106 | 未规定停靠中实体的运动与传感器行为 | 内核为停靠中实体提供状态标志（或 Suite 钩子），使速度指令被忽略、传感器停采；该标志写入真值状态列 | S2 R-S2-67 依赖"停靠期间不产生覆盖"；由 Suite 在观测后过滤会使检测的随机抽样与 CRN 口径不清 |
| P-03 | M28 R-M28-46、R-M28-62 | `DEPLETED` 实体的通信端点是否下线未规定（`silent_failure` 已规定下线） | 明确 `DEPLETED` 时端点状态；S2 建议与 `FAILED` 相同（下线） | S2 §3.7 状态迁移表 E9 与 R-S2-68 的可见结果依赖该规定 |
| P-04 | M28 R-M28-107 | 脚本轨迹驱动只有 `loop_path`；S2 `random_walk` 目标需在运行期以 `target` 域抽取局部航点（§3.3），`exogenous_path` 需按 100 ms 定点插值 | 登记 S2 目标运动的推进方式：增设脚本类型 `waypoint_walk{v_tgt, leg_s}` 与 `timed_path{points[t, x, y]}`，或明确由 S2 `SuiteRuntime` 的阶段参与者推进 | 目标真值轨迹须跨策略逐位一致（RN-2），推进方式影响 `pose_digest` 是否覆盖目标 |
| P-05 | M29 §6.6.2、§6.6.3 | 只规定了接受通知"窗口后送达时忽略"，未规定要约控制消息迟于窗口结束送达时的处理 | 规定迟到要约不产生 `OFFERED`（S2 §3.7 E3） | 否则 `to` 可能对已过期要约看到 `OFFERED`，与"前置不满足"行冲突 |
| P-06 | M29 R-M29-73、M28 R-M28-108 | S2 在密封段以 Suite 规则对交接要约做语法校验（`to` 为自身或不在名册、引用前缀不是要约方），回执 `rejected/handoff_syntax`；与"交接不用任何 `DENIED_*`"的关系未写明 | 明确该语法拒绝属 `DecisionValidator` 的条目级回执、不经仲裁器、不读真值，登记原因名 `handoff_syntax` | 语法校验不泄露真值；不登记会与 M29"一律登记"的表述冲突 |
| P-07 | M29 `ArbiterRules.validate_handoff(view, op) -> bool` | S2 的合法性判据需要同时读取要约与接受两个操作、真值持有表与双方实体状态，现签名只有一个操作 | 签名改为 `validate_handoff(view, offer_op, accept_op) -> bool`，在接受的 `RESOURCE_ARBIT` 处理时调用；`ArbiterView` 暴露 Suite 真值表（custody）与实体状态 | S2 §3.7 第 4 条 (a)–(e) |
| P-08 | M29 R-M29-70 | S2 规定合法转移后接受方的新令牌为其 `handoff.accept` 的 `op_id`；须确认 Agent 在提交时即可确定自身操作的 `op_id`（`op_seq` 由 Agent 侧单调分配或回执返回） | 在 R-M29-70 或 CT `decision.v1` 中写明 `op_seq` 的分配方与 Agent 可见时刻 | 否则接受方无法在后续要约中引用自己的令牌 |
| P-09 | M30 §6.7.3 | 表中只有 `track_share@1`（legacy，沿用原实现常量，只适用网格族），无 `track_share_cs@1`；而摘要第 6 条与 P1 范围行已列 `track_share_cs` | §6.7.3 以 `track_share_cs@1`（新写，`awr.coordination.baselines.coopsearch`，`wire.v2`，P1，`cs-planar` `d3` 与网格族）取代该行；`track_share@1` 只保留为 `drone4d-event@1` 的 legacy 基线 | 原实现不产生 `found` 报告，在 S2 中 `U_CS` 恒为 0（本文 §8.3） |
| P-10 | M30 §6.7.0 | 公共构件表无 `custody` | 增 `custody` 构件（持有令牌维护、交接触发、接受确认消息），使用者为 S2 全部 `d3` 闭环基线 | 本文 §8.2、R-S2-48 |
| P-11 | M30 §6.7 末段基线计数表 | CS 行"1（网格族：`track_share`）" | 改为"1（`d3` 与网格族：`track_share_cs`）" | 与 P-09 一致 |
| P-12 | M31 R-M31-41 与 M33 R-M33-69、§6.9 | M31 现行为六通道（报告与交互经 `tasks`，账本经 `RunView.ledger()`），M33 现行为九通道（另含 `reports`、`interaction`、`ledger`），二者互相引用对方为依据 | 两份文档统一为同一词表；S2 v0.2 按 M31 六通道声明，统一后若采用九通道，S2 只改通道声明、不改评测逻辑 | 评测器读取未声明通道即报错（R-M31-41），词表不一致会使 S2 评测器在其中一份规范下失败 |
| P-13 | M31 场景集角色 | 角色词表未见"只作审计、不成榜"的角色 | 增 `audit`：成员可运行与评测，结果只进入审计报告与条目卡诊断栏，不进入主榜排序 | S2 网格族不满足成榜最低条件时须有合法登记方式（本文 §4.3） |
| P-14 | M31 OR-7 与固定型 Family | 固定型 Family 只要求黄金运行，未规定成榜的最低样本与隔离条件 | 增固定型 Family 的成榜最低条件（场景数 ≥ 6、每场景种子 ≥ 5、留出集地理隔离），不满足者只能登记为 `audit` | 本文 §4.3 MC-1 至 MC-3；AWR-06 §8.6 关于小样本 percentile 区间覆盖率约 91%–92% 的模拟结果 |
| P-15 | 契约草案（CT） | CT 已列 `handoff.offer{target_ref, to}` 的操作类型，但未登记 S2 的 `coopsearch.confirm.v1` 报告 schema、`custody` 真值记录 schema、`target_ref`/`op_ref` 文法、原因名 `quota`、`rate_limited`、`evidence_count`、`handoff_syntax`，以及清单 `x1{}` 宿主接入字段 | 按本文 §7.6、R-S2-04、R-S2-21、R-S2-76、§9.4 登记 | 本文中"以 CT 定稿为准"的字段须在 CT 中有落点 |
| P-16 | X1 §8.3 | S2 的 `U_cum(t)` 写作"截至 t 已首次有效确认目标的贡献之和 / |K_hv|"，只在 `w_t = 0` 时与 `U_CS` 一致；`w_t > 0` 时 `c_k` 在 `τ_k` 之后 60 s 内才累积 | 改为引用本文 §9.4 的定义（`s_k` 部分为离散增量，`c_k` 部分按 100 ms 累积） | 否则 `U_cum(T) = U` 不成立，`T_recover` 的增量比较失准 |
| P-17 | X1 §8.2 S2 行 | `Held(o)` 以 custody 定义；S2 `d0`–`d2` 无跟踪规则时持有表仍按首次确认建立（只作诊断），X1 宜确认在这些档位上接受该口径 | 在 S2 行注明"`d0`–`d2` 的持有表为诊断口径，`T_detect` 照常计算" | 与 X1 RK-RC-04 的删失风险评估衔接 |
| P-18 | X1 §4.7 宿主自带扰动段 | 写作"S2 `d2` 的 90–120 s 子团队分区"，可被理解为分区区间 | 改为"S2 `d2` 的一次子团队分区（时长 90–120 s）" | 与 S2 §5.1、§12.2 一致 |
| P-19 | M32 `fixed_set` 估计 | 场景内种子 percentile 重采样未规定最小种子数 | 规定 `fixed_set` 成榜时每场景种子 ≥ 5，不足者只报告点估计与种子范围 | 与 P-14、本文 MC-2 一致 |

---

## 18. 评审处理记录

v0.1 评审意见 19 条，逐条打开对应文件核实后处理如下。"核实结论"中"部分属实"指意见的一部分与并行文档的现行版本不符，其余部分属实。

| 编号 | 章节 | 严重度 | 意见摘要 | 核实结论与依据 | 处置 | 修改位置 |
|---|---|---|---|---|---|---|
| 1 | §7.1、R-S2-02、AC-S2-07 | high | S2 声明两个 primary（`u_cs`、`u_cs_trk`），违反 06 §7.0 与 M31 R-M31-04、V-S-02 的"每个 Suite 恰有一个主指标" | 属实：06 §7.0"每个 Suite 恰有一个主指标"；M31 R-M31-04、V-S-02 只对 `rank_mode = per_metric` 设例外 | 采用方案一：只保留 `u_cs`，跟踪权重 `w_t` 作为实例参数（搜索实例 `w_t = 0`）；AC-S2-07 明确按 V-S 常规判定、无例外；`S_task` 的场景集口径提请 06（反馈 23） | §0 第 4 条、§1.4、§4.3、§6.5、§7.1、§7.2、R-S2-02、AC-S2-07、§12、§17.1 反馈 23 |
| 2 | §3.7、R-S2-12、§12.3 | high | `|G_k| = 0` 时 `c_k = 1` 奖励晚确认；生成器约束早了 `W_trk`；`τ_k > T_c` 未定义 | 属实：按 §12.3 数据 `τ = 292 s` 时 `u ≈ 0.757`，`τ = 100 s`、`c = 0.7` 时约 0.767；约束"不早于 `T_c − W_trk`"不能保证 `|G_k| > 0` | 增 `G_min = 10 s`，不足者 `u_k = s_k`；R-S2-12 改为起点 `> T_c`；明确 `τ_k > T_c` 时 `u_k = 0` 且不建立持有；网格族逐事件登记 `confirm_profile`；AC-S2-15 增延迟确认检验 | §1.4、§3.6、§3.7、§6.6、§7.1、R-S2-12、AC-S2-14、AC-S2-15、§12.3 |
| 3 | §3.7 规则 4/5、R-S2-30/31 | high | 与 M29 R-M29-73、§6.7 表冲突（`DENIED_UNREACHABLE` 前置）；接受与通知路径、失效中途、非法 `to`、重复要约未定义；并发未对齐顺序键 | 部分属实：M29 现行 R-M29-73 与 §6.6.2 已改为合法性只写真值层、交接不用任何 `DENIED_*`、接受通知经网络承载（CF-21），意见所引 v0.1 文本已不存在，第 (1) 点无需再提修订；第 (2)–(4) 点属实 | 增交接状态迁移表 E1–E9（含通知路径、要约方中途失效、非法 `to`、非 `to` 接受、重复要约、迟到要约）；第 6 条与 R-M29-70 顺序键逐项对齐；R-S2-76 语法校验；对 M29 的剩余事项（迟到要约、语法拒绝的归属、`validate_handoff` 签名、`op_id` 可见性）列入 §17.2 P-05 至 P-08；AC-S2-05 增 (g)–(n) | §3.7、R-S2-29–32、76、AC-S2-05、§17.1 反馈 7、§17.2 |
| 4 | §3.7 规则 4 | medium | 判据缺"要约方为当前持有方"，非持有方的有效重复确认令牌可通过 | 属实：v0.1 判据"令牌对应的确认有效、未被此前的交接转出"对重复确认同样成立 | 判据改为 (a)–(e)：要约方 `= h_k`、`target_ref = tok_k`；令牌定义收紧为两种形式；增单元测试 (h)、(n) | §1.4、§3.7 第 1、4 条、AC-S2-05 |
| 5 | R-S2-19、§3.6 | medium | 团队配额经"剩余配额"与 `quota` 回执构成带外信道；同 tick 竞争未裁决 | 属实：剩余团队配额随队友报告递减，零延迟、不受分区与 `cut` 影响、不计费 | 改为每 Agent 配额 `quota_agent = 2`（团队隐含总额不变），观测只给本人余额；不存在跨 Agent 竞争；AC-S2-11 增 (b)、(c) 泄露用例；撤回 v0.1 反馈 17 的团队配额建议 | §3.6、§5.2、§6.3、§9.2、R-S2-19、21、AC-S2-11、§17.1 反馈 17 |
| 6 | §12.1、R-S2-39、AC-S2-01 | medium | 锚点容差过松（`tol_time_s = 0.1` 不能区分 `t_sealed` 与 `t_apply`，`tol_rel = 1%` 约容许 2.3 s）；依赖不存在的 `vel0`；回退方案与 `a_max = 3` 矛盾 | 部分属实：容差问题与回退方案矛盾属实；M28 现行 R-M28-106 已有 `initial_state.vel0_mps`（并以 S2 §12.1 为依据），初速字段可用 | 锚点改用 `initial_state.vel0_mps`，删除 `a_max = 100` 回退；时间量整数纳秒精确相等、`u_cs` 绝对误差 ≤ 1e-9；增"以 `t_sealed` 计 `τ`"的反例用例；锚点 B 同步改写 | §12.1、R-S2-39、AC-S2-01 (e) |
| 7 | §7.4、§6.4、§12.2 | medium | 8 m 层间隔使 5 m 间隔违规恒为 0；层高随 n 线性增长；"斜距"与平面检测不一致 | 属实：v0.1 层高 `60 + 8k`，任两机层差 ≥ 8 m；`n64` 约 564 m | 高度层改为 4 层循环复用（60–84 m），同层平台的平面距离 < 5 m 即违规；`cs-planar` 全文改为"平面距离" | §1.4、§3.4、§6.4、§7.4、§12.1、§12.2 |
| 8 | §3.4、R-S2-17、R-S2-56、§3.3、§4.2 | medium | 网格族"`L1Model` 原样输出"与"字段集合相同"矛盾；hv 判别依赖 `appearance` 而非 `sig`；`vehicle_C` 类别归属矛盾；简报是否披露登车未规定 | 属实：`L1Model.frame` 输出字段见 CF-19，无 `ident`、`sig`、`cov_xy`；另核实 FC-2 的"字段集合相同"是同一族两档之间的要求，v0.1 R-S2-17 把它误用于两族之间 | 增观测适配层 `mesh_obs_adapter@1` 与字段表；R-S2-17 改为两族共用核心字段、扩展字段公开声明，FC-2 只在网格族两档间判定；V1 增带时间区间的载体映射；简报只公开载体规则、不披露具体载体；AC-S2-18 增载体边界用例 | §1.4、§2.3、§3.3 第 3、6 条、§3.4、§3.6 V1、§4.2、§6.3、R-S2-17、56、73、AC-S2-18、§12.3 |
| 9 | §5.1 d3、§3.5、需求表 | medium | 机巢与能量无需求、无验收、参数缺失 | 属实：v0.1 R-S2-01 至 R-S2-64 无机巢条目，`brief.energy` 只有字段名 | 新增 §3.10 与 R-S2-65 至 R-S2-68（生成约束、能量初值与 `energy_ratio` 区间、停靠中的检测与持有、`DEPLETED` 衔接）、AC-S2-23；映射 WP-D4-51；X1 H 族不适用的说明；充电与停靠行为提请 M28（P-01 至 P-03） | §3.10、§5.1、§6.3、R-S2-65–68、AC-S2-23、§14、§17.2 |
| 10 | §9.4 | medium | 族范围与 X1 §2.3 不符；未声明 `x1.response_ops`、关联谓词与 `U_cum(t)`；`stress` 未写 `n ≥ 8` | 属实：X1 §2.3 对 S2 适用 F、C、M、P、L、J、S，H 在 P2 前不适用；X1 §8.2、§8.3、R-RC-43 要求宿主声明上述各项 | §9.4 改为引用 X1 矩阵并给出宿主接入声明；新增 R-S2-69 至 R-S2-72；`U_cum(t)` 按 `s_k` 与 `c_k` 逐时刻累积；`stress` 子集 `n ≥ 8`；X1 侧的 `U_cum` 表述提请修订（P-16） | §1.1、§3.9、§4.3、§9.4、R-S2-69–72、AC-S2-22、§17.2 P-16 至 P-18 |
| 11 | §10 / §11 追溯 | medium | 若干 AC 不检验其对应需求（R-S2-09/13/14、20、36、47）；AC-S2-09 取样点与多重比较未规定；§8.4"≈""≤"无可测定义；`d0`"远小于 0.2 T_c"无依据 | 属实 | AC-S2-01 增生成器统计检验 (b)–(d)；AC-S2-09 规定五个取样距离与 Bonferroni 处理并增上限用例；AC-S2-05 增 (g)；R-S2-47 的动作流一致性移入 AC-S2-04；§8.4 增序关系记号定义；`d0` 给出 `sweep_time_ratio ≈ 0.10` 的计算 | §5.1、§8.4、R-S2-13、20、47、AC-S2-01、04、05、09 |
| 12 | §8.1、§8.3 与 M30、M31、M34 | low | 与 M30 §6.7.3、§6.7.0 不一致却声称一致；通道名不在 M31 词表；Suite 标识三种写法；CT 不存在 | 部分属实：前三点属实（CF-21）；CT（`docs/d4/契约草案.md`）现已存在，但未登记 S2 的报告、custody 与交接参数契约 | §8.1 改为"除 `track_share_cs@1` 外与 M30 一致"；通道按 M31 六通道声明并指出 M31 与 M33 的分歧；`suite_id` 统一为 `coopsearch-handoff`（随机域与 `validity.md` 路径随之调整）；§17.2 增 P-09 至 P-12、P-15 | 文档信息"标识"行、§0 第 7 条、§4.1、§8.1、§8.2、§9.3、R-S2-01、42、62、AC-S2-08、§17.2 |
| 13 | Q-S2-02、§14、PA-7、R-S2-63 | low | 以区分度选取 `w_t` 违背调参纪律；配额与阈值是否可调未说明 | 属实：06 PA-7 只允许调整生成器公开参数 | `w_t` 依研究问题取 0.5 并写入预注册，pilot 只报告敏感性；§5.3 列出 S2 可调清单并排除评分与规则参数；Q-S2-02 关闭、Q-S2-04 改写；PA-7 可调清单按 Suite 声明的修订提请 06（反馈 24） | §5.3、§7.1、R-S2-63、§14、Q-S2-02、Q-S2-04、§17.1 反馈 24 |
| 14 | §3.4、§3.6、§5.1 d2 | low | `d2` 的确认难度旋钮作用可能很弱，"见 `sig = hv` 即报告"几乎无风险 | 属实（为待验证的风险）：`p_conf = 0.05`、虚报不扣分、v0.1 配额比 4:1；另查出 `|K_hv| = n/2` 使"目标数不公开"可由 `n` 推出 | §8.4 增构造效度对照 `sig_greedy@test` 与判定处置（收紧 `p_conf` 或提高干扰比例）；`d2` 起 `|K_hv|` 按实例抽样，由其推出的难度量移入隐藏部分；RK-S2-02 与 `validity.md` 同步；目标数推出问题提请 06（反馈 27） | §5.2、§5.3、§6.1、§8.4、§12.2、§13、RK-S2-02、R-S2-14、§17.1 反馈 27 |
| 15 | §4.2、§13、OR-7、§9.3 | low | 网格族缺 d0 锚点；无 holdout 时如何排名未规定；同地理区留出能否 `ranked` 未规定 | 部分属实：M31 OR-7 与 V-S-15 对固定型 Family 以至少 2 个黄金运行代替锚点，无需豁免，但 v0.1 未登记黄金运行；其余两点属实 | 登记黄金运行 GR-1、GR-2；增成榜最低条件 MC-1 至 MC-3（场景 ≥ 6、种子 ≥ 5、地理隔离），不满足者登记为 `audit`；`audit` 角色与固定族成榜条件提请 M31、M32（P-13、P-14、P-19） | §4.2、§4.3、§9.3、R-S2-74、AC-S2-18、§17.2 |
| 16 | §3.4、§5.2、R-S2-43 | low | 性能预算只覆盖 `n8`，规模与网格族缺可检验预算 | 属实：06 §5.11 的规模预算未拆分到 S2；量级估算核对一致。意见所称"`tele` 每次采集格数是 `wide` 的数倍"不属实：`tele` 视场 20°，约 70 格，与 `wide` 约 50 格同量级 | §7.7 EV-3 统一 1 Hz 覆盖降采样与可选可见性缓存，新增 EV-7 分规模预算表（含网格族，并说明不适用 PA-6）；新增 R-S2-75、AC-S2-24；§3.4 增传感器侧复杂度约束；RK-S2-05 说明与宿主预算的关系 | §3.4、§7.7、R-S2-43、75、AC-S2-24、RK-S2-05、§14 |
| 17 | §3.3、§6.6、§5.1 | low | `random_walk` 定义不完整、存在中心漂移；参数名三处不一；运动模型是否公开未规定 | 属实 | 改为以当前位置为圆心、半径 `v_tgt · leg_s` 的局部航点游走，到达即重抽，写出抽样与重抽规则；参数名统一为 `leg_s`；`brief.target_motion` 公开运动模型；增 `prior_drift_m` 并写入已知局限 | §3.3、§5.3、§6.3、§6.6、§12.2、§13 |
| 18 | §3.4、§5.1、§6.3、§12 | low | `d0` 起点与锚点不一致；`start` 所属文件不一；`d2` 缺 `P_d0`、`σ_d`；`σ_d = ∞` 的类型；"d3 的非跟踪场景集"不存在、部分场景集 primary 未指明；`reacq_s`、`hold_s` 无检验 | 属实 | `d0` 行注明锚点为特例；起始位姿以 `agents.json` `initial_state` 为权威、`brief.start` 为投影；`d2` 补 `P_d0 = 0.8`、`σ_d = r_s`；`σ_d` 类型 `number \| null`；删除"非跟踪场景集"，全部场景集 primary 为 `u_cs`、`w_t` 按实例；AC-S2-14 增 `h_s`、`reacq_s` 边界用例 | §3.4、§4.1、§4.3、§5.1、§6.3、§6.4、§7.1、§12、AC-S2-14 |
| 19 | R-S2-58、RK-S2-06、§11 末段、AC-S2-08 | low | FC-3 至 FC-6 应为 FC-3 至 FC-7；RK-S2-06 混淆了 FC-4 的比较对象；AC-D4-18 被扩大且未提交反馈；AC-S2-08 漏 jsonschema、numba | 属实：06 AC-D4-27 要求 FC-3 至 FC-7；FC-4 为同一 Suite 同一基线两档配对差；AC-D4-18 只含 AC-S2-1 至 4、6；AC-D4-43 列 open3d、scipy、jsonschema、numba | R-S2-58、AC-S2-05、§4.2、WP-D4-27 改为 FC-3 至 FC-7；RK-S2-06 改为网格族自身两档偏差并以 `L1Params` 构造参数回标定；§11 末段恢复 AWR-06 口径并把扩大部分提交反馈 25；06 内部 FC 范围不一致提交反馈 26；AC-S2-08 补全模块 | §0 第 2 条、§4.2、R-S2-58、AC-S2-05、AC-S2-08、§11 末段、RK-S2-06、§14、§17.1 反馈 25、26 |
