# FX2-R3-other 报告：D1-AC-16（S3 ×1 与 ×10 一致）根因定位与 agent-runtime 侧修复

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R3-other（修复区域 other：`python/awr/agent/**`、`tests/**`、`docs/**` 等，不含 sim、gateway、web-engine、web-ui、world 区域） |
| 分派项 | D1-AC-16（P1）：第 2 轮 `test_s3` 通过，`test_s3_rate_equivalence` 决策一致但 accepted 时刻差 10.18 s（阈值 ≤ 1.0 s） |
| 日期 | 2026-10-02（05:38 至 07:05） |
| 依据 | D1 验收报告第 2 轮 第 1、3 节；INT-1 §3、§7.6；03 §8.4（D1-AC-16）、§1.3、ADR-036、ADR-039、ADR-045、ADR-049、ADR-057、ADR-065；M14 PRD §6.11–§6.13、FR-049–051、AC-026、AC-027、§14；M10 PRD §6.4.1、NFR-015；M16 §6.4.5；FX2-R2-sim 报告第 6 节第 4 条 |
| 环境 | 8 核 E5-2603 v4、无 GPU；Python 3.12（`.venv`）；六城 `worlds/` 已生成。本阶段有其他工作包并行（sim、web-engine 等），诊断期间 1 分钟 load 1–9，没有使用排他锁，没有运行性能基准与 perf 标记用例 |
| 结论 | 根因有两部分。①**sim 侧（主因，约 8.9 s 并经检出放大）**：M10 开局的 plan-pool 预热与 generator 作业按墙钟到达的 tick 生效，×10 时开局任务晚约 9 s【仿真】，搜索机轨迹平移后 M13 按 tick 键控的检出抽样使首检再变化数秒。②**agent-runtime 侧（×10 时约 1.6 s）**：协作计时以"处理事件时的当前时刻"为起点，报价与执行前复核按串行 sim-core 往返计时。本包修复了 ②（仿真时间锚点、只读查询合并与并行、委派在途预取，ADR-068 第 1–3 条）；① 属 sim 区域（M10、M08），本包按区域规则**未修改** sim 代码，给出经两个诊断原型验证的开局屏障设计（ADR-068 第 4 条、M14 §14 第 23 条）并新增快速核对用例。加上非阻塞开局屏障原型后多进程 accepted 差 0.26–0.42 s（4 次诊断运行）且判定用例通过；不加时判定用例仍不通过（10.04 s）。**D1-AC-16 仍阻塞于 sim 侧开局屏障** |
| 规格 | 阈值不变（M14-FR-050 ≤ 1.0 s、D1-AC-16 全部条款）。计时语义修订：M14 §6.11.1、§6.12.2、§6.13（规则 ⑥）、§6.18、§7.8、FR-050、AC-027、§14 第 23 条；追加 ADR-068 |
| 测试 | `pytest tests/agent -m "not perf"` 232 通过（新增 7 个）；`AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3` 通过（144 s）；`test_s3_rate_equivalence` 不加屏障不通过（10.04 s）、加屏障原型通过；新增 `test_s3_start_rate_invariant` 不加屏障 XFAIL、加屏障原型 XPASS；`ruff` 对本包改动文件通过，`make lint` 的 tools/lint 全部规则通过，失败项全部在其他工作包进行中的文件（第 7 节） |

## 1 结论摘要

- 第 2 轮的 10.18 s 不是 agent-runtime 锁步本身的问题，而是多进程时间线在 sim 侧开局就已分叉：×1 的开局任务在 1.43 s 生效、×10 在 10.33 s 生效，首次检出 62.29 s 对 77.89 s。agent-runtime 再在 ×10 时额外贡献约 1.6 s 的滞后，其中报价晚到 1.24 s、执行前复核 0.93 s、驻留起点 0.29 s、事件到提交 0.12 s。
- 本包在 agent-runtime 侧加入四类改动（ADR-068 第 1–3 条、M14 §6.13 规则 ⑥），使 find、报价收齐、委派投递三个时刻在 ×1 与 ×10 下逐毫秒相同，租约申请在委派投递的同一时刻发出：
  1. 检出触发任务的 find 自检出事件的仿真时刻起算；
  2. 观测点高度缓存与合并、任务提交时预热，环境查询与估价并行、同目标合并；
  3. 长任务的只读准备（观测点、执行前复核估价）在委派在途期间预取；
  4. 驻留自 goto 终态事件起算，结果回传自 hover 终态事件起算（其后的 returning 进度同样锚定）。
- 开局屏障是使 D1-AC-16 通过的必要条件，属 sim 区域。两个诊断原型（`sitecustomize` 注入，未入库）：阻塞式（mission stage 内等待）可验证结论，但 load 8 时 sim-core 停顿超过 2.0 s 被判挂死；非阻塞式（设置期每轮至多 1 tick，hold 时 `steps_due` 返回 0 并重锚）保持 2.1 s 加 0.3 s 不触发挂死，开局与首检两种倍速逐 tick 相同。
- 剩余的不确定性来自 sim-core 一侧的服务时延（×10 时估价、`env/query`、租约与命令锁存每次 0.2–0.9 s【仿真】，hover 锁存 0.3 s）。在 S3 几何下 b1 沿待命航线朝观测点方向飞行，goto 锁存的延后大部分被到站用时抵消（锁存差 1.8–3.0 s 对到站差 0.04–0.18 s），accepted 差由 hover 锁存与 hover 稳定时长主导。

## 2 诊断方法

真实 supervisor（ci profile，`sim-core,api,agent-runtime` 三进程），S3 加一个 `diag` profile（rate 1 或 10），轮询 `/api/events` 转储全部事件（诊断脚本在 scratchpad，未入库），逐段比较 ×1 与 ×10 的仿真时刻。原型以 `PYTHONPATH` 上的 `sitecustomize` 注入 sim-core 进程，不改仓库代码。

### 2.1 第 2 轮现象复现（修复前代码）

| 事件（仿真 s） | ×1 | ×10 | 差 | 说明 |
|---|---|---|---|---|
| `plan.warm` | 1.028 | 7.844 | 6.82 | plan-pool 预热（spawn、导入、GeoWorld）墙钟约 0.8–1.0 s |
| m-search 开始（a1 起飞） | 1.432 | 10.332 | 8.90 | generator 作业排在预热之后，按到达的 tick 生效 |
| a1 follow_path 开始 | 14.244 | 23.076 | 8.83 | 起飞后 safe_transit 作业 8–90 ms 墙钟 |
| 首检（t1 suspect，a1） | 62.288 | 77.888 | 15.60 | 轨迹平移 8.9 s 后按 tick 键控的检出抽样再变化 6.7 s |
| 提交 | 62.292 | 78.008 | | 事件滞后 0.004 对 0.12 s |
| find | 62.492 | 78.208 | | 自提交起算 0.2 s |
| 报价收齐 | 63.504 | 80.458 | | ×10 三个报价依次在 find 后 1.05、1.65、2.25 s 到 |
| 委派投递（provider 侧 awarded） | 64.514 | 81.469 | | t_send + L − d_resp，确定 |
| 进入租约阶段 | 64.528 | 82.408 | | 执行前复核（两次高度、一次估价）0.014 对 0.94 s |
| goto 锁存 | 64.656 | 83.608 | | 租约与 goto 各一次往返 |
| 到站（goto 终态） | 137.496 | 147.716 | 10.22 | b1 待命航线上的位置不同，到站用时 72.8 对 64.1 s |
| orbit 锁存 | 137.512 | 148.008 | | 驻留自此起算 |
| hover 终态 | 150.036 | 160.716 | | |
| accepted | 150.954 | 161.714 | 10.76 | 第 2 轮验收 10.18 s，同一现象 |

×10 运行时 sim-core 受 RTF 限制（`sim.rtf_limited`，S3 715 s 仿真用 132.6 s 墙钟，有效倍速约 5.4；每轮至多推进 50 tick），sim-core 的估价、`env/query` 属不可分片慢任务（ADR-057），一次往返折合 0.2–0.9 s【仿真】。

### 2.2 根因拆解

| 组成 | ×10 相对 ×1 的影响 | 所在 | 本包处理 |
|---|---|---|---|
| 开局任务生效 tick 随墙钟（预热 + generator） | +8.9 s，并经检出抽样放大到首检 +15.6 s | M10（plan-pool 结果按到达 tick 生效，ADR-039）、M08（SimClock 无保持机制） | 设计与原型（第 4 节），未改 sim 代码 |
| 检出抽样按 tick 键控 | 把任何轨迹平移放大为数秒的首检变化 | M13（按设计，ADR-049） | 不需改：开局确定后轨迹逐 tick 相同，首检即相同 |
| 事件到提交的滞后 | +0.12 s | M14 | 锚点：find 自检出时刻起算 |
| 报价串行往返（每候选两次高度、估价、环境） | +1.24 s | M14 | 高度缓存与合并、提交时预热、估价与环境并行 |
| 执行前复核（两次高度、估价） | +0.93 s | M14 | 委派在途预取 |
| 驻留自 orbit 锁存之后起算 | +0.29 s | M14 | 锚点：驻留自 goto 终态事件起算 |
| 结果回传含交还租约往返、returning 进度未锚定 | +0.07–0.25 s | M14 | 锚点：结果与之后的进度自 hover 终态事件起算 |
| 租约、goto、hover 命令锁存 | +0.8–1.8 s（goto 前）、+0.3 s（hover） | M08（命令在到达时锁存，ADR-049） | 不能在 agent-runtime 消除；S3 几何下 goto 前的延后大部分被到站用时抵消 |

## 3 agent-runtime 侧修复（本区域，已入库）

| 编号 | 改动 | 文件 |
|---|---|---|
| A1 | 检出触发任务的锚点：`TriggerEngine` 以检出事件的 `t_sim_ns` 调 `TaskManager.submit(anchor_ns=…)`，`Task.t_anchor_ns` 记录；分配首次 find 调 `AgentNetwork.find(…, t0_ns=…)`，Mock 在 t0 + 0.2 s 返回（重新分配从当前时刻起算；真 ANet 忽略该参数） | `runtime/scenario.py`、`runtime/tasks.py`、`runtime/types.py`、`runtime/allocator.py`、`runtime/network.py`、`anet_mock/network.py`、`anet_bridge/network.py` |
| A2 | `SharedReads`：观测点 `ground_dtm`、`height_dsm` 按 (op, x, y) 缓存（上限 4096 条，sim-core 重启、纪元变化、剧本重置时清空），并发同键合并、失败不缓存；`env/query` 并发同键合并、不跨时间缓存；任务提交时预热目标处高度；`HandlerCtx.station` 两项高度并行查询 | `runtime/drone_agent.py`、`runtime/core.py`、`runtime/tasks.py` |
| A3 | 报价处理器中估价与环境查询并行发出（估价回复缺 `env_target` 时用环境结果） | `runtime/handlers/meta.py` |
| A4 | 委派在途预取：Mock 长任务委派发出时调用提供方 `DroneAgent.prepare`，`prepare_observe` 计算观测点并做执行前复核估价；投递后处理器 `take_prepared` 取用（未完成则等待，失败则现场重算），包络登记、健康、忙、租约与命令仍在投递后执行；预取保留 60 s【仿真】后丢弃。估价次数不变 | `runtime/drone_agent.py`、`runtime/handlers/observe.py`、`runtime/handlers/__init__.py`、`anet_mock/network.py` |
| A5 | 驻留与结果锚点：`CallResult.t_sim_ns`（命令终态事件的仿真时刻）；`observe` 自 goto 终态时刻起 `sleep_until(t_on + dwell_s)`；hover 终态时刻经 `HandlerCtx.event_time` 写入 `RecordingSink.t_event_ns`，Mock 网络以它为最终结果与之后 returning 进度的起点；`t_exec_s` 取至该时刻 | `guard/pipeline.py`、`runtime/drone_agent.py`、`runtime/handlers/observe.py`、`runtime/network.py`、`anet_mock/network.py` |

锚点只改变计时起点，不改变决策输入（候选集、报价内容、打分、谓词）；锚点已经过期时计时器立即触发，退化为原行为。锁步驱动下锚点与当前时刻相同，`test_lockstep_bitwise_deterministic`（证据链 id 逐位一致）不变。

## 4 sim 区域阻塞项：剧本开局屏障（请求 M10、M08）

### 4.1 设计（ADR-068 决策第 4 条，M14 §14 第 23 条）

1. M08 `SimClock` 增加内部保持（`hold(reason, deadline)`、`release(reason)`）与设置期批量上限：保持期间 `steps_due()` 返回 0 并重锚（`wall0 = now`、`tick0 = tick`，不欠账）；TIME 状态仍为 PLAYING，主循环照常迭代、写心跳、处理 bus 与慢任务，因而不触发 ADR-065 的 2.0 s 挂死判定，也不阻塞 sim-core ready（M10-NFR-015 不变）。
2. M10 在剧本加载（绑定时、autoplay 之前）进入设置期：每轮至多推进 1 tick，使 mission stage 置下的保持在下一 tick 生效（也可改为在 `fleet.step` 内逐 tick 检查）；mission stage 发现开局作业（预热与开局任务的 generator）未完成时置保持；以每轮执行的检查（M10 登记的慢任务或 `steps_due` 钩子）在全部作业的 future 就绪后放行（必要时先 `_dispatch()` 排队作业），下一 tick 由 mission stage 统一生效；开局任务全部开始后退出设置期。墙钟上限 10 s（与 123 WORLD_NOT_READY 一致），超时放行并发告警事件。
3. 影响面：S1、S2、S4–S6 开局多保持约 2 s 墙钟；ladder 的开局作业较多时保持时长受 10 s 上限约束（`ladder.steady` 以仿真时间计，不受影响）；崩溃恢复（checkpoint 恢复，不重新加载剧本）不进入设置期，D1-AC-11b 不受影响。

### 4.2 原型验证

| 运行（S3，多进程，仿真 s） | 开局 ×1 / ×10 | 首检 ×1 / ×10 | find | 报价收齐 | 到站 ×1 / ×10 | accepted ×1 / ×10 | 差 |
|---|---|---|---|---|---|---|---|
| 修复前 | 1.432 / 10.332 | 62.288 / 77.888 | 62.492 / 78.208 | 63.504 / 80.458 | 137.496 / 147.716 | 150.954 / 161.714 | 10.76 |
| 阻塞式屏障原型，agent 为修复前代码 | 0.932 / 0.932 | 62.288 / 62.288 | 62.500 / 62.620 | 63.512 / 64.670 | 137.036 / 137.216 | 150.502 / 150.926 | 0.42 |
| 阻塞式屏障原型，加 A1–A3 与 A5 的驻留、结果锚点（早于 A4 与进度锚定入库） | 0.932 / 0.932 | 62.288 / 62.288 | 62.488 / 62.488 | 63.500 / 63.500 | 137.036 / 137.076 | 151.186 / 150.926 | 0.26 |
| 非阻塞屏障原型，加本包全部改动 | 0.932 / 0.932 | 62.288 / 62.288 | 62.488 / 62.488 | 63.500 / 64.326 | 137.036 / 137.176 | 150.502 / 150.902 | 0.40 |

- 阻塞式原型在 sim-core 主循环内停顿 1.53–1.75 s（预热）加 0.22–0.35 s（generator）；load 8 时一次复测中两种倍速的 sim-core 都因超过 2.0 s 被判挂死、反复重启，`/api/health/ready` 一直 503（`sim: restarting`），因此只能作验证。
- 非阻塞原型 sim-core 在 tick 1–5 保持 2.07–2.09 s、在 tick 35 保持 0.31 s，两次运行的 `proc.state` 只有启动时的 STARTING、RUNNING，没有重启。该次 ×10 的报价晚到（find 后 1.84 s，×1 为 1.01 s）来自 sim-core 慢任务时延（第 6 节），委派投递后立即进入租约阶段（预取生效）。
- 判定用例：加阻塞式原型与本包改动时 `test_s3_rate_equivalence` 通过（850 s）；`test_s3_start_rate_invariant` 加阻塞式原型 XPASS（22 s）。

非阻塞原型的核心逻辑（诊断用，供 sim 区域参考；正式实现应按 4.1 写进 `SimClock` 与 M10，而不是猴子补丁）：

```python
# steps_due 包装：设置期（剧本已加载、t < 1 s）
if hold:
    if pool_ready(rt.pool) or now > hold_until:      # pool_ready：先 _dispatch()，无 QUEUED，RUNNING 的 future 均已完成
        hold = False; clock._anchor(now)
    else:
        clock._anchor(now); return 0                  # 不推进、不欠账；主循环照常迭代
n = orig_steps_due(now)
if n > 1:                                             # 设置期每轮至多 1 tick（重锚）
    clock.wall0 = now - int((clock.tick + 1 - clock.tick0) * TICK_NS / clock.rate); return 1
return n
# mission stage（st_tracker）末尾：设置期内 pool.pending() 且未保持 → hold = True，hold_until = now + 10 s
```

### 4.3 快速核对用例

新增 `tests/e2e/test_scenarios.py::test_s3_start_rate_invariant`（`AWR_E2E_FULL=1`，`ext`，`xfail(strict=False)`）：S3 以 ×10 与 ×1 各运行到 4 个开局任务进入 RUNNING（约 20 s 墙钟），断言各任务生效时刻差 ≤ 8 ms。当前 XFAIL；sim 侧实施开局屏障后应 XPASS，届时可去掉 xfail 标记，再跑完整的 `test_s3_rate_equivalence`。

## 5 测试与验证

| 项 | 命令 | 结果 |
|---|---|---|
| agent 功能测试 | `pytest tests/agent -m "not perf"` | 232 通过（原 225 加新增 7），3 个 perf 用例按规则未纳入（其中 `test_snapshot_4096_under_5ms` 在高负载下偶发超时，与本包无关） |
| 新增锚点用例 | `pytest tests/agent/test_rate_anchor.py` | 7 通过：SharedReads 缓存与合并、find 锚点、报价准时、预取、驻留与结果锚点、多进程时序模型下的倍速一致（×10 取往返 0.4 s、事件滞后 0.3 s，accepted 差 0.70 s；往返 0.2 s、滞后 0.1 s 时 0.34 s） |
| 锁步与既有 S3 用例 | `pytest tests/agent/test_s3_lockstep.py` | 全部通过（证据链逐位一致、×1 与 ×10 推进粒度一致、报价来自估价、eta 与到站用时相符） |
| 引用 agent 代码的其他用例 | `pytest tests/sim/test_service_surface.py tests/ops/test_skeleton.py tests/recorder/test_sidecar_reindex.py tests/contracts -m "not perf"` | 325 通过 |
| S3 端到端 | `AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3` | 通过（144 s） |
| S3 倍速一致（当前仓库） | `AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3_rate_equivalence` | 不通过：决策一致，accepted 161.08 s 对 151.04 s（差 10.04 s），根因为 sim 侧开局漂移 |
| S3 倍速一致（加开局屏障原型） | 同上，`PYTHONPATH` 注入原型 | 通过 |
| 开局核对 | `AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py::test_s3_start_rate_invariant` | XFAIL（17 s）；加原型 XPASS（22 s） |
| lint | `ruff check python tools tests`；`make lint` | 本包改动文件全部通过；`make lint` 的 tools/lint 规则（no-emoji、no-hex、motion、icons、raw-controls、brand、deps、units、py-imports、py-callbacks、perf-flags、thresholds、m06、m15、m16）全部 ok；失败项见第 7 节 |

## 6 剩余风险与建议

1. **（阻塞，sim 区域）开局屏障**：按第 4 节实施后复测 `test_s3_start_rate_invariant` 与 `test_s3_rate_equivalence`。不实施时 D1-AC-16 在任何负载下都不会通过（开局差约 9 s，且检出抽样放大的方向随机）。
2. **（sim 区域）×10 下的 sim-core 服务时延**：S3 ×10 时 sim-core 受 RTF 限制，估价与 `env/query` 慢任务、租约与命令一次往返折合 0.2–0.9 s【仿真】；空闲 ×1 下经 REST 的 `env/query` 也要 65–75 ms 墙钟。M08-NFR-006（入队到回复 p99 ≤ 20 ms）在 ×10 下值得复核；估价回复增加可选 `env_target`（M14 §14 第 2 条，待 17、M08 登记）可省去报价中的环境查询。agent-runtime 已把报价压到"估价 ∥ 环境"一次往返，若这次往返超过约 0.85 s【仿真】，报价仍会晚到。
3. **hover 锁存与稳定时长**：hover 命令到达后要等 sim-core 本轮结束才锁存（×10 时约 0.3 s），hover 终态时长随环绕相位变化（2.5–3.2 s）；两者构成开局确定后的主要残差（0.26–0.42 s），在阈值内但余量约 0.6 s。若需进一步收紧，可考虑由 sim-core 提供"按仿真时刻生效"的命令字段（M08 协议级，D1 不做）。
4. **ADR 编号**：sim 区域进行中的代码注释引用 ADR-067（`python/awr/sim/mission/*.py`、`python/awr/sim/sensors/*.py`、`tests/sensors/test_gimbal.py`），而 03 附录 E 的 ADR-067 已由 FX2-R3-web-engine 登记；本包登记 ADR-068。请负责人合并时核对编号。

## 7 make lint 状态

本包改动的文件全部通过 ruff。`make lint` 当前因其他工作包进行中的文件失败（不属本区域，未修改）：

- `ruff`：`python/awr/sim/mission/runtime.py:444`（RUF046）、`python/awr/sim/sensors/kernels_gimbal.py:87`（SIM109），sim 区域；
- `oxlint`：`apps/web/src/viewport/dev/featMatrixWgpu.ts` 4 处 no-floating-promises，web-engine 区域。

## 8 规格变更

- **ADR-068**（03 附录 E 与 §7.0 索引）：多进程倍速一致：agent-runtime 的仿真时间锚点与只读查询合并，剧本开局屏障（请求 M10、M08）。含背景数据、决策 1–5、备选 6 项、实测与后果。
- **M14 PRD**：§6.11.1 处理器伪代码与"时间锚点"条目；§6.12.2 时序块（find 起点 t0、结果起点 t_event、委派在途预取）与多进程实测修正段；§6.13 规则 ⑥ 与 sim 侧前提段；§6.18 新增"委派在途预取保留时长 60 s【仿真】"；§7.8 时钟域登记表新增同名计时器；M14-FR-050 增加锚点要求（阈值不变）；M14-AC-027 增加 `tests/agent/test_rate_anchor.py`；§14 新增第 23 条（开局屏障，待 M10、M08 登记，D1-AC-16 阻塞项）。
- 没有改变任何验收阈值；没有新增依赖。

## 9 改动文件清单

代码（other 区域，M14）：

- `python/awr/agent/guard/pipeline.py`
- `python/awr/agent/runtime/drone_agent.py`
- `python/awr/agent/runtime/core.py`
- `python/awr/agent/runtime/tasks.py`
- `python/awr/agent/runtime/types.py`
- `python/awr/agent/runtime/scenario.py`
- `python/awr/agent/runtime/allocator.py`
- `python/awr/agent/runtime/network.py`
- `python/awr/agent/runtime/handlers/__init__.py`
- `python/awr/agent/runtime/handlers/meta.py`
- `python/awr/agent/runtime/handlers/observe.py`
- `python/awr/agent/anet_mock/network.py`
- `python/awr/agent/anet_bridge/network.py`

测试：

- `tests/agent/fakes/fake_sim.py`（多进程时序模型 `rtt_ns`、`event_lag_ns`，缺省 0 即原锁步行为）
- `tests/agent/test_rate_anchor.py`（新增）
- `tests/e2e/test_scenarios.py`（新增 `test_s3_start_rate_invariant`，xfail）

文档：

- `docs/03-设计基线与决策记录.md`（ADR-068 与 §7.0 索引行）
- `docs/modules/M14-智能体运行时与ANet-PRD.md`
- `docs/impl/FX2-R3-other-报告.md`（本报告）

没有修改 sim、gateway、web-engine、web-ui、world 区域的任何文件；诊断脚本与两个原型只在 scratchpad 中，未入库。
