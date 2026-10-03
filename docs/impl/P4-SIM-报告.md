# P4-SIM 仿真内核：单步尾部与并发（D1 第 4 轮性能打磨）

| 项 | 内容 |
|---|---|
| 工作包 | P4-SIM（区域：`python/awr/sim`、`python/awr/environment`、`python/awr/runtime` 的 checkpoint 与总线、`tools/bench` 的 sim 工具、`configs/runtime.yaml` 的 sim-core 段） |
| 日期 | 2026-10-02 至 2026-10-03 |
| 依据 | `docs/impl/D1-验收报告-第3轮.md`（§3、§4.1、§4.1c、§5）；FX2-R3-sim、FX2-R3-other、FX2-R3-gateway 报告；AWR-03 §1.3、§8.4、ADR-017、ADR-033、ADR-065、ADR-068、ADR-070；AWR-10 §10.4；AWR-18 PR-6、§7.4、§7.6；M07–M11、M14 PRD |
| 分派项 | D1-AC-07（P0，N = 1000 单步 p99 ≤ 3 ms、最大 ≤ 12 ms、RTF ≥ 0.99、CPU ≤ 0.6 核、饱和 0）、D1-AC-08（P0，3 个 SwiftShader 客户端时 tick 数据年龄 p99 ≤ 15 ms）、D1-AC-27（全机 RTL 与 link_drop 单步最大 ≤ 12 ms）、D1-AC-28（P1，客户端并发）；D1-AC-16 的 sim 侧前提（ADR-068 第 4 条剧本开局屏障，×1 与 ×10 accepted 时刻差 ≤ 1.0 s） |
| 环境 | VMware 虚拟机 8 vCPU（E5-2603 v4 1.7 GHz，8 个单核 socket，无超线程）；Python 3.12（`.venv`，numba 0.67、numpy 2.5、eclipse-zenoh 1.10.1）。同机有 P4-WEB、P4-UI 等工作包与其他会话，排他锁之外 load 1–10 |
| 约束执行 | 没有安装依赖；只用只读 git 命令（`status`、`archive` 到暂存目录做对照）。性能自测都持排他锁 `runs/.perf.lock`、工具内等 load ≤ 4，每次持锁 3.5–4.5 min（< 10 min）；诊断钩子（逐轮记录器、cProfile、`/proc` 采样）只在暂存目录，不进仓库。**例外（如实记录）**：22:40 的一次自测（v16）开跑时本包的一个进程内诊断仍在运行（约 1 核），该次的最大值 13.1 ms 不采用。没有改动 UI；无 emoji 与禁用字形；`make lint` 通过 |
| 规格变更 | ADR-073 正文（附录 E，§7.0 索引行同步）；修订 M07-FR-017，M08-FR-001、FR-003、FR-004、FR-083 与 §6.4.1 stage 表，M09-FR-052、FR-092 与 §5.2 相位安排，M10-FR-030、§6.4.1、NFR-015 并新增 M10-FR-069，M11-FR-016，M14 §14 第 23 条，AWR-10 §10.4，AWR-18 PR-6，AWR-19 §6.2 配置示例；`configs/runtime.yaml` 的 sim-core `env.AWR_SIM_AUX_CPUS`。**不改任何 D1 阈值与 `fleet_ladder` 的 p99 口径（各秒 p99 的最大值）**；不冻结暂定阈值 |

## 1 结论摘要

| AC | 第 3 轮 | 根因（已核实） | 本轮处理 | 自测（排他锁，单次运行，只作修复验证） |
|---|---|---|---|---|
| D1-AC-07（P0） | RTF 1.000、CPU 0.563、p99 4.96 ms（5.12、3.85、4.96）、最大 7.16 ms、饱和 0 | ①每秒一次的"gen2 + checkpoint 拷贝"在主循环内约 9 ms；②写线程在主循环醒来前后持有 GIL；③最重 tick 对（battery_rtl、env 全量）5.0–5.6 ms；④确定性失败的转场按 30 s 退避续飞，每次重做 2.5 ms 粗校验并在 12 tick 后准入 1000 航点的 follow_path；⑤state_ext 片长估计偏低 | 空闲窗口门控；gc 与 checkpoint 合并并移到后台线程、在空闲窗口内持状态锁拷贝（含主循环等待余量）；相位按 tick 对均衡、env 全量融合核；粗校验记忆、输入日志快速路径；state_ext 上包络、发布顺延与空闲窗口发布（第 2.1 节） | 最终代码 2 次（不带诊断钩子）：RTF 1.000、1.000，CPU 0.529、0.522 核，单步 p50 2.09、2.12 / **p99 2.90、2.86 ms** / 最大 5.46、5.92 ms，饱和 0、0；60 s 中 p99 > 3 ms 的秒数 0、0。**五项全部满足** |
| D1-AC-08（P0） | tick 数据年龄 p99 16.66 ms；api 0.126 核 | 3 个 flight60 客户端的 SwiftShader（marl）工作线程自行把亲和性设为全部 CPU，越出 `taskset -c 2-6` 跑到 core1，主循环逐轮在 run-queue 中等待数毫秒（第 2.2 节） | 性能工具对客户端进程树做亲和性守护（PR-6 的落实）；sim 侧同第 1 行 | 守护之后 4 次：**tick 数据年龄 p99 10.57、9.79、12.06、12.15 ms**，api 0.093–0.096 核；守护之前（同一 sim 代码）16.81、17.14 ms |
| D1-AC-27（P0 RTL / P1 link_drop） | RTL 单步最大 12.2 ms；link_drop 32.6–39.2 ms | link_drop：M09 故障逐条 Python 循环（500 个活动故障约 2.5 ms/次）、M10 对数百条挂起轨道逐 stage 续飞并各做一次粗校验、20 条并发命令同一轮准入；RTL：风暴中 gen2 约 10 ms | 故障按组向量化；M10 每次 stage 至多 2 次粗校验、FCU 链路丢失不续飞、汇总分片；drain 墙钟 ≤ 1.5 ms；风暴中 gen2 只冻结（第 2.3 节） | 进程内墙钟（成对推进）：link_drop 每轮最大 31.3 → 10.5 ms（单步 5.3 ms）；RTL 每轮最大 11.2 ms（单步 5.6 ms）。未做生产口径的风暴用例（需 web harness） |
| D1-AC-28（P1） | p99 10.57、最大 18.2 ms、饱和 57、tick 年龄 16.49 ms | 同 D1-AC-08；另有内存带宽与缓存竞争下个别 stage 的偶发拉长 | 同上 | 守护之后 4 次：p50 2.06–2.13、**p99 3.85–5.94 ms**、最大 6.15–7.61 ms、饱和 0、RTF 0.9999–1.000、sim-core 0.567–0.578 核、recorder 0.104–0.106 核。**p99 仍不通过**（第 6 节第 1 条） |
| D1-AC-16（sim 侧前提） | accepted 差 12.08 s；`test_s3_start_rate_invariant` XFAIL | 开局 plan-pool 作业按墙钟到达 | 剧本开局屏障（SimClock 内部保持 + 设置期逐 tick 推进 + M10 登记开局作业，第 2.4 节） | `AWR_E2E_FULL=1`：`test_s3_start_rate_invariant` 通过（由 XFAIL 转正），`test_s3_rate_equivalence` 通过（差 ≤ 1.0 s），合计 13 min 42 s |

## 2 根因与修复

### 2.1 D1-AC-07：N = 1000 稳态单步尾部

测量方法：生产口径为 `tools/bench/fleet_ladder/run.py --n 1000 --dur 60 --runs 1`（真实 supervisor + sim-core，ladder n1000，`ladder.steady` 之后 60 s）；诊断时以暂存目录中的 `sitecustomize` 记录器逐轮记录墙钟、线程 CPU、run-queue 等待、等锁时长与各 stage 墙钟（预分配数组，窗口后在后台线程落盘），并对尖峰轮次累计 M10、M08 准入等函数的耗时。起点（第 3 轮代码）：各秒 p99 中位 2.99 ms，记录窗口内一半的秒超过 3 ms。

1. **checkpoint 与写线程**（ADR-073 第 1、2 条）
   - 写线程被 `save()` 唤醒后立即编码首段、并在窗口末尾开始不可中断的段：`IdleGate` 改为带截止时刻的窗口，写线程开始编码前与每个让出点（TOC 每 32 个数组、元数据区写入前）都等"打开且距截止 ≥ 0.4 ms"的窗口。checkpoint 之后一轮的离核时间 p99 1121 → 227 µs。
   - gen2 并入 checkpoint（gen2 → 拷贝 → 冻结），风暴中只冻结；主循环内的整块"gen2 + 拷贝"约 9 ms 移到后台线程：慢任务只提出请求，线程分 gen2 与拷贝两步，各等一个足够长的空闲窗口，持仿真状态锁执行（主循环每轮迭代持同一把锁，计时在取锁之前，等锁计入单步）。
   - 拷贝在生产口径约 4.5 ms，而成对推进的空闲窗口通常只剩 3–4.5 ms：只按窗口本身判断时 101/109 次都是 0.5 s 后强制执行（主循环醒来后等锁）。加入**主循环等待余量**：主循环按 tick % 50 相位维护迭代耗时上包络，窗口另带 slack = max(0, 2 × 2.6 ms − 下一轮相位的上包络)，拷贝允许越过窗口末尾，只要被推迟的那一轮仍在 2 × 2.6 ms 之内。强制执行 101/109 → 20/133（客户端并发的逐轮记录中等锁 p99 5 µs）。
   - 拷贝线程留在主循环核（`AuxPinner.keep`）：在 core7 上与 plan-pool 争核并跨核取数，中位 4.9 ms，在 core1 上 4.5 ms。
   - 总线键匹配改为模块级函数（不再每次构造引用自身的递归闭包，减少 gc 压力）。
2. **调度均衡与固定开销**（第 3 条）
   - battery_rtl 相位 43 → 17、coverage 23 → 1，任何 tick 对内至多一个重的低频 stage，env 全量 tick 所在的对内没有（`test_worst_tick_sets`）；拆 battery_rtl 为两次 n/10 的尝试因固定开销不减而撤回。
   - env 全量求值的非风字段改为 numba 融合核 `kernels_rows.full_rest`（六种天气预设 × 有无风场查询逐字节对拍），进程内 1.01 → 0.70 ms/次。
   - state_ext 逐机耗时由 0.7/0.3 指数平均改为上包络（片长 2.1–2.5 ms 的片此前落在只剩约 0.8 ms 预算的轮次上）；末片之后剩余预算放不下发布时顺延一次调用。
3. **事件驱动尖峰**（第 5 条）
   - 逐轮记录显示：每 30 s 同一 tick（多次运行一致）出现"mission_engine 约 3 ms（`_try_resume` → `coarse_proven` 2.5 ms）+ 12 tick 后 mission 4.7 ms + ingest 2 ms"，来自两架转场确定性失败、按 30 s 退避续飞的 ladder 机体。两个尖峰落在同一秒，后者成为该秒的第二大值。
   - 输入日志 `_plain` 对纯数值列表走快速路径（msgpack 字节不变），1000 航点的 follow_path 3.55 → 0.54 ms；follow_path 航点数组按参数对象缓存（每次 `np.asarray` 约 0.5 ms）。
   - 粗校验不通过的记忆（M10-FR-069 ①）：同一入圆点、同一组生效区、机体移动 ≤ 1 m 时不重做，直接走 plan-pool 转场。之后每次续飞只剩 12 tick 后的一次准入尖峰，单独落在一秒中，不进入该秒 p99。
4. **结果**：最终代码 2 次 p99 2.90、2.86 ms（第 5.2 节表 1），60 s 中没有超过 3 ms 的秒。

### 2.2 D1-AC-08、28：客户端并发

- 守护之前（c2、c3，sim 侧已含第 2.1 节的修复）：单步 p50 2.92–3.20、p99 8.95–10.72 ms、饱和 7–15，tick 数据年龄 p99 16.8–17.1 ms。逐轮记录显示离核时间 p99 9.4 ms，其中 84–98% 是 run-queue 等待（`/proc/thread-self/schedstat`），等锁为 0。
- `/proc/*/task/*/stat` 每 100 ms 采样 55 s（`processor` 字段为 1 的线程的 CPU 增量）：sim-core 主线程 27.8 s、拷贝线程 0.26 s，另有 Chromium 的 `Thread<00>`–`Thread<07>`（三个 GPU 进程的 SwiftShader marl 工作线程）合计约 15 s。marl 创建工作线程时把亲和性设为全部 CPU，不继承 harness 的 `taskset -c 2-6`。
- 处理（ADR-073 第 6 条）：`tools/bench/ipc/_common.py` 新增 `AffinityGuard`，`fleet_ladder --clients` 与 `bench_state --clients` 启动客户端后每 0.25 s 扫描其进程树，把亲和性越出本工具集合的线程改回（本工具未钉核时不做任何事），改回数写入明细 `client_affinity`（每次运行 24 个线程）。这是 AWR-18 PR-6 已写明"含 GPU 进程中的 SwiftShader 线程"的落实，不改测量口径。
- 守护之后 core1 上只剩 sim-core（主线程 29.7 s、拷贝线程 0.27 s）与少量其他会话的进程（< 0.6 s）。4 次：p50 2.06–2.13 ms（与无客户端相同），p99 3.85–5.94 ms，最大 6.15–7.61 ms，饱和 0，tick 数据年龄 p99 9.79–12.15 ms。
- state_ext 发布：客户端运行时 api 与 recorder 都订阅 `state/sim-core/ext`，单次 zenoh `put` 中位 4.2 ms、最大 6.7 ms（无远端订阅者时约 0.3 ms；独立测量显示 put 的复制与分片大部分时间持有 GIL）。新增 `awr/sim/runtime/bgpub.py`（`GatedPublisher`）：主线程只拼接载荷，后台线程等剩余 ≥ 估计 × 1.25 + 0.6 ms 的空闲窗口再发（至多 0.25 s，只发最新一份）。

### 2.3 D1-AC-27：风暴

- link_drop（500 架、每 100 ms 20 条注入）：M09 `FaultInjector.step` 按故障分组向量化（到期转移按登记顺序，同槽重复时退回逐条；机上侧分组提交；版本号缓存），进程内每次 stage 平均 2553 → 323 µs、最大 10.7 → 3.1 ms；mission_engine 平均 5788 → 529 µs、最大 19.1 → 1.6 ms；M10 挂起轨道在 FCU 链路丢失期间不续飞（`fcu_link_ok`），每次 stage 至多 2 次粗校验，大编组状态汇总分片（≤ 64 条轨道/次）；drain 每轮墙钟 ≤ 1.5 ms。进程内每轮最大 31.3 → 10.5 ms（成对推进，单步 15.7 → 5.3 ms）；剩余的峰值是 `faults` 2.5–3.1 ms（活动故障增至 500 时）与 fsm 1.6–1.9 ms 同轮。
- 全机 RTL：准入已分片（ADR-065）；风暴中 gen2（约 10 ms）改为只冻结（上次估计 > 3 ms 或自上次以来准入 ≥ 64 条）。进程内每轮最大 11.2 ms（单步 5.6 ms，不含测试台中仍内联的 checkpoint 轮）。
- 生产口径的风暴用例（`storm.rtl`、`storm.linkdrop`）属 web harness，本包未运行；按进程内与生产口径约 1.3–1.5 倍的比例推算单步最大约 7–8 ms。

### 2.4 D1-AC-16：剧本开局屏障（ADR-068 第 4 条）

- SimClock：`hold(reason, timeout_s, ready)`、`release`、`held`；保持期间 `steps_due` 返回 0 并重锚（不欠账），对外状态仍为 PLAYING，主循环照常写心跳、处理总线与慢任务；`reset` 解除全部保持；`per_tick` 时主循环逐 tick 推进，某个 tick 的 stage 置下保持后本轮不再推进（保持在置下它的 tick 之后立即生效，×1 与 ×10 相同）。
- M10：剧本加载后开 1 s【仿真】设置期，`pool.on_submit` 登记期间提交的作业，有未到达的作业即保持（就绪判据 `pool.pending_of` 并先 `dispatch()` 排队作业），全部到达或 10 s【墙钟】超时放行（超时发 `mission.warning SETUP_BARRIER_TIMEOUT`）；`AWR_SCENARIO_BARRIER=0` 关闭；崩溃恢复复用进程时第一次加载不设屏障。plan-pool 增加 `on_submit`、`_arrived` 与 `pending_of`。
- 用例：`tests/mission/test_start_barrier.py`（线程池、慢 worker 40 ms，每轮 1 个与 50 个 tick 两种推进下开局作业生效 tick 相同）；`tests/sim/test_clock.py` 两例；e2e 见第 5.1 节。

## 3 规格变更

ADR-073（附录 E 正文，§7.0 索引）。逐条：

- M08-FR-001（内部保持）、FR-003（状态锁、窗口与等待余量、drain 限时、成对推进不变）、FR-004（state_ext 片长上包络、发布顺延与空闲窗口发布，gen2 与 checkpoint 合并、风暴中只冻结）、FR-083（空闲窗口与后台拷贝，`AWR_SIM_CKPT_BG`）、§6.4.1 stage 表（battery_rtl 17、coverage 1，tick 对错峰规则）。
- M07-FR-017（全量 tick 的融合核）；M09-FR-052、§5.2（battery_rtl 相位）、FR-092（故障分组向量化）。
- M10-FR-030（开局屏障）、§6.4.1、NFR-015；新增 M10-FR-069（大编组任务的主循环开销上界：粗校验额度与记忆、FCU 链路丢失不续飞、汇总分片）。
- M11-FR-016（CheckpointStore 的 `IdleGate`）；M14 §14 第 23 条（开局屏障已实施）。
- AWR-10 §10.4（core1 上的拷贝线程、core2–6 的 SwiftShader 线程守护、core7 的发布线程）；AWR-18 PR-6（`AffinityGuard`）；AWR-19 §6.2 配置示例（`AWR_SIM_AUX_CPUS`）。
- 没有改动任何阈值、门禁或测量口径。

## 4 改动文件

- 运行时：`python/awr/runtime/checkpoint.py`（`IdleGate` 截止时刻与等待余量、写线程首段等窗口与让出点）、`python/awr/runtime/bus.py`（键匹配模块级函数）。
- sim-core：`python/awr/sim/runtime/main.py`（状态锁、相位迭代耗时上包络与窗口余量、drain 限时、gc 与 checkpoint 合并、风暴中只冻结、state_ext 上包络与顺延、后台线程装配、perf 字段 `gc_gen2_skipped`、`ckpt_bg`、`ext_pub_bg`）、`ckpt.py`（`_BgCapture`、调用元数据内联）、`clock.py`（内部保持、`per_tick`）、`cpuaff.py`（`keep`）、`inputlog.py`（`_plain` 快速路径）、新增 `bgpub.py`；`python/awr/sim/core/command.py`（航点数组缓存）。
- M07：`python/awr/environment/kernels_rows.py`（`full_rest_nb` 与预热）、`field.py`（全量求值走融合核）。
- M09：`python/awr/sim/safety/faults.py`（分组向量化）、`service.py`（移除机体时 `faults.touch()`）、`__init__.py` 与 `battery.py`（battery_rtl 相位）。
- M10：`python/awr/sim/mission/engine.py`（粗校验额度与记忆、FCU 续飞判据、汇总分片）、`runtime.py`（`fcu_link_ok`、开局屏障）、`__init__.py`（coverage 相位）；`python/awr/sim/planning/pool.py`（`on_submit`、`pending_of`、`dispatch`）。
- 工具与配置：`tools/bench/ipc/_common.py`（`AffinityGuard`）、`tools/bench/fleet_ladder/run.py`、`tools/bench/ipc/bench_state.py`；`configs/runtime.yaml`（`AWR_SIM_AUX_CPUS: "7"`）。
- 用例：新增 `tests/mission/test_start_barrier.py`、`tests/mission/test_coarse_memo.py`、`tests/sim/test_bgpub.py`、`tests/sim/test_state_ext_budget.py`；修改 `tests/sim/test_clock.py`（保持两例）、`tests/runtime/test_checkpoint.py`（`IdleGate` 截止时刻与等待余量）、`tests/sim/test_checkpoint.py`（后台拷贝在空闲窗口内）、`tests/environment/test_wind_fused.py`（全量融合核对拍）、`tests/sim/test_schedule_golden.py` 与 `tests/golden/m08_schedule/schedule_50_{numba,numpy}.json`（相位调整后重新生成，新增 tick 对不变量）、`tests/e2e/test_scenarios.py`（移除 `test_s3_start_rate_invariant` 的 xfail）。
- 文档：见第 3 节。

## 5 测试

### 5.1 功能测试

- 修改期间分批：`tests/sim`、`tests/mission`（76 例）、`tests/safety`、`tests/environment`、`tests/runtime`、`tests/planning` 等均通过；新增与修改的用例全部通过（`test_coarse_memo` 2、`test_state_ext_budget` 2、`test_bgpub` 3、`test_start_barrier`、`test_clock`、`test_checkpoint` 等）。
- 最终整批：`pytest -m "not perf" tests/sim tests/safety tests/environment tests/mission tests/sensors tests/planning tests/swarm tests/scenarios tests/runtime tests/rt tests/contracts tests/e2e/test_scenarios_static.py tests/e2e/test_scenarios.py`：1529 通过、11 跳过、3 失败（32 min 47 s）。失败的 3 例是 `tests/environment/test_env_e2e.py` 两例（503）与 `tests/sensors/test_pose_chain_e2e.py` 一例，单独运行（3 通过）与随 `tests/sensors` 整目录运行（112 通过）均通过，与 FX2-R3-sim 报告第 5.1 节记录的整批次序依赖相同，不是本包引入。此后新增的 `test_coarse_memo`、`test_state_ext_budget` 与 `test_checkpoint` 的等待余量断言单独运行通过。
- `AWR_E2E_FULL=1 pytest tests/e2e/test_scenarios.py -k "test_s3_rate_equivalence or test_s3_start_rate_invariant"`：2 通过（13 min 42 s；pytest 的 5 min faulthandler 提示是长用例的线程转储，不是失败）。
- 已知且非本包引入：`tests/sim/test_state_ext_packed.py` 与 `tests/sim/test_service_surface.py` 同批按此次序运行时 `test_agent_lease_acquire_release` 失败（"preempted by safety"），单独运行通过；用 `git archive HEAD` 导出的原始代码在同一次序下同样失败（全局登记表的次序依赖）。`tests/runtime/test_checkpoint.py::test_checkpoint_copy_under_1ms`（perf 标记）在机器负载下超时，与本包无关。
- `make lint` 通过；`make numba-warm` 在最后一次修改核文件后重新预热，`tests/sim/test_numba_signatures.py` 通过。

### 5.2 自测（排他锁，工具内等 load ≤ 4，每次持锁 3.5–4.5 min）

表 1　`fleet_ladder --n 1000 --dur 60 --runs 1`（无客户端；p99 为工具口径"各秒 p99 的最大值"）：

| 运行 | 代码状态 | 取锁时 load（工具等到 ≤ 4 才开跑） | RTF | CPU（核） | p50 / p99 / 最大（ms） | 饱和 | > 3 ms 的秒数 |
|---|---|---|---|---|---|---|---|
| base1 | 第 3 轮代码 | ≤ 4 | 0.994 | 0.564 | 2.18 / 5.04 / 7.51 | 1 | —（记录窗口 29 s 中 14） |
| v3 | + 空闲窗口门控、env 融合核、相位调整 | ≤ 4 | 0.994 | 0.564 | 2.13 / 3.88 / 6.82 | 1 | — |
| v10 | + 输入日志快速路径、航点缓存、后台拷贝（首版） | 2.2 | 1.000 | 0.538 | 2.13 / 3.30 / 5.60 | 0 | — |
| v13 | + 拷贝估计与强制周期修正（强制 101/109） | 2.4 | 0.9999 | 0.551 | 2.17 / 3.93 / 6.37 | 0 | — |
| v14 | + 拷贝线程留在 core1 | 2.5 | 1.000 | 0.554 | 2.15 / 3.37 / 6.63 | 0 | 4 |
| v18 | + 主循环等待余量（强制 20/133）、粗校验记忆、state_ext 上包络 | 1.1 | 1.000 | 0.541 | 2.12 / 2.88 / 3.66 | 0 | 0 |
| v19 | + state_ext 发布顺延 | 0.9 | 1.000 | 0.546 | 2.09 / 2.90 / 5.66 | 0 | 0 |
| v20 | 最终代码（+ 空闲窗口发布），不带诊断钩子 | 0.4 | 1.000 | 0.529 | 2.09 / 2.90 / 5.46 | 0 | 0 |
| v21 | 最终代码（重复），不带诊断钩子 | 0.5 | 1.000 | 0.522 | 2.12 / 2.86 / 5.92 | 0 | 0 |

运行间差异仍存在（同机负载、事件驱动尖峰是否同秒）；验收按 ADR-033 取 3 次中位。中间代码的 v15（取锁时 load 2.3）p99 3.93 ms 由"粗校验 + 转场准入"同秒造成，促成了第 2.1 节第 3 条的处理；v17（取锁时 load 5.3，带函数级计时钩子）p99 5.19 ms、最大 16.5 ms，最大的一轮不在被计时的 M10、M08 函数中，未定位。

表 2　`fleet_ladder --n 1000 --dur 60 --runs 1 --with-recorder --with-checkpoint --clients 3 --with-flight60`（工具在 `taskset -c 2-6` 下，与 harness 相同）：

| 运行 | 状态 | RTF | sim-core（核） | p50 / p99 / 最大（ms） | 饱和 | tick 年龄 p50 / p99（ms） | api / recorder（核） | 窗口 load 均值 / 最大 |
|---|---|---|---|---|---|---|---|---|
| c2 | 无守护 | 0.9992 | 0.498 | 2.92 / 8.95 / 14.2 | 7 | 3.74 / 16.81 | 0.092 / 0.107 | 11.2 / 17.5 |
| c3 | 无守护 | 0.9986 | 0.513 | 3.20 / 10.72 / 15.9 | 15 | 3.76 / 17.14 | 0.085 / 0.109 | 13.1 / 17.3 |
| c4 | 守护 | 1.000 | 0.573 | 2.13 / 4.34 / 6.93 | 0 | 3.67 / 10.57 | 0.096 / 0.104 | 11.5 / 17.4 |
| c5 | 守护 | 0.9999 | 0.578 | 2.12 / 3.85 / 7.17 | 0 | 4.08 / 9.79 | 0.095 / 0.105 | 12.5 / 17.2 |
| c6 | 守护 + 空闲窗口发布 | 1.000 | 0.577 | 2.09 / 5.94 / 7.61 | 0 | 3.75 / 12.06 | 0.093 / 0.105 | 12.3 / 18.0 |
| c7 | 同 c6（最终代码） | 0.9999 | 0.567 | 2.06 / 4.13 / 6.15 | 0 | 3.82 / 12.15 | 0.095 / 0.106 | 13.1 / 18.8 |

窗口 load 均值 ≥ 6，工具按 PR-5 把 CPU 类判定记为"不判定"。

## 6 未解决与建议

1. **D1-AC-28 单步 p99（P1）**：守护之后 3.85–5.94 ms。剩余的尖峰有两类：①线程 CPU 与墙钟同步增长的 stage 偶发拉长（env、sensors、fsm、guard 单次 3–6 ms，无客户端时 0.6–1.5 ms），与 5 个核满载的 SwiftShader 光栅化争内存带宽与末级缓存有关；②少量 run-queue 等待（其他会话的进程偶尔调度到 core1）。sim 侧的进一步方向是降低每 tick 的内存流量（状态数组的工作集、tap 与 sensors 的逐字段拷贝），或由验收运行协议保证 core1 独占（`isolcpus` 或 cgroup cpuset，需要管理员权限）。recorder 0.104–0.109 核也略高于 0.1（P1，M12）。
2. **web harness 的 SwiftShader 线程**：`apps/web/perf/harness` 启动的 Chromium 同样会让 SwiftShader 线程越出 2–6（同一原因），影响带 live 后端的 Tier S 用例（storm、flight60 live、latency）对 api 与 sim-core 的干扰。建议 web 侧 harness 对浏览器进程周期执行 `taskset -a -p -c 2-6 <pid>`（与 `AffinityGuard` 同义），属 P4-WEB / M16。
3. **D1-AC-27 生产口径未测**：本包只有进程内数据（单步 5.3–5.6 ms）。link_drop 中 `faults` stage 仍随活动故障数增长（20 → 500 个时 2.5 → 3.1 ms/次），若生产口径接近 12 ms，下一步把到期转移与机上侧提交改为增量（只处理本 tick 新到期的故障）。
4. **M09 逆风分量从未生效（发现，未修改）**：`SafetyService.wind_head` 以 `env.query(P, None, fields=1)` 查询，`t_sim_ns=None` 在 `EnvironmentServiceImpl.query` 中 `int(None)` 抛 TypeError，被 except 吞掉，逆风分量恒为 0，M09-FR-052 中 `v_c`、`t_rtl` 的逆风修正实际不起作用。修正（传入当前 tick 的 `t_sim_ns`）会改变 RTL 触发时刻与剧本结果，需要 M09 连同 S1–S6 与电量用例一起回归，本包未改动。
5. **确定性失败的转场**：ladder n1000 中仍有两架机体每 30 s 重试一次转场（`plan.ready` 之后 follow_path 执行失败），每次 12 tick 后的准入尖峰约 4.7 ms（生产口径），本包只消除了重复的粗校验。根因（direct 规划器的转场高度与细校验的不一致，FX2-R3-sim 报告第 6 节第 3 条）仍待 M10 与 M16 查明。
6. **`nice -5` 不生效**：本机 sim-core 进程没有 `CAP_SYS_NICE`（supervisor 日志 "nice not permitted, using 0"），AWR-10 §10.4 的优先级设定在本机验收中不存在。
7. **测量噪声**：同机其他会话使开跑前 load 在 0.1–10 之间变化（工具等到 ≤ 4 才开始）；单次运行的 p99 受"尖峰是否同秒"影响，v13–v19 的中间代码在 2.88–3.93 ms 之间波动。验收按 ADR-033 取 3 次中位。
