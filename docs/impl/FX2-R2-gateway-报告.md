# FX2-R2-gateway 修复报告（网关、回放、IPC 工具）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R2-gateway（区域 gateway：`python/awr/api`、`python/awr/runtime`、`python/awr/recorder`、`tools/bench/ipc`；为本区域验收项的测量口径另改了 M12 的性能用例驱动与 `tools/bench/rec`，见第 6 节） |
| 日期 | 2026-10-01 |
| 依据 | D1 验收报告第 1 轮（§3 D1-AC-08、10、18 行，§4.7、§4.8、§7）；INT-1 §3、§7；03 §1.3、§8.4、ADR-033、ADR-040；17 §9.4、§9.5；18 §3.1 PR-5、PR-6、PR-12，§7.2，§8.7(2)，§9.4，§9.5；M11、M12、M16 PRD |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chromium 151（SwiftShader）；Node 22.12；Python 3.12（`.venv`）；修复期间有其他工作包并行（load 2–13，自测时段见第 5 节） |
| 约束执行 | 没有安装依赖；没有使用 git；修复阶段没有跑性能基准与 perf 标记用例的正式判定。诊断用的是不进判定的短时测量（Python 客户端，不开浏览器）；修复后的快速自测在排他锁 `runs/.perf.lock` 内分 5 次进行（另有 1 次约 1 min 的工具冒烟），每次持锁 146–417 s（均 < 10 min，含开跑前 load 等待），测试构建沿用第 1 轮的 `.cache/acc1/dist-test`，便于与第 1 轮对比 |

## 1 结论摘要

| 验收项 | 根因 | 本包处理 | 自测结果（不作判定） | 剩余 |
|---|---|---|---|---|
| D1-AC-10（P0）丢弃未补齐 | ① 测量口径：`bench_cmd` 在排空期仍注入丢弃，pump 一停，最后约 70 ms 内丢的那条消息必然"未补齐"；② 产品缺口：尾部批次被丢且生产者此后静默时，缺口无从检出 | 修正口径；`EventSubscriber` 增加尾部探测；bus 查询改为 consolidation NONE | 两次短跑丢弃 54–55 次、补齐最大 68–74 ms、未补齐 0；尾部丢弃 1 s 内补齐（新用例） | 无。验收阶段按原命令复测 |
| D1-AC-08（P0）工具未实现 | `bench_state --clients 3 --with-flight60` 只解析参数；另外前端没有实现 18 §9.5 的 `?rt=1` | 实现客户端口径：真实后端（钉核）+ 3 个 headless Chromium 跑 flight60（MS5 口径 `scene=full&n=1000`）、`/proc`、R60 与订阅核对、PERF-E008 判无效 | api 0.059 核（阈值 0.35）；tick 年龄 p99 16.7 ms（sim 单步 p99 21.9 ms）；ladder n1000 一次因 sim-core 重启判无效 | tick 年龄取决于 N = 1000 下 sim-core 的稳定性与单步时延（sim 区域，8.2）；CPU 门禁在 3 个浏览器下按 PR-5 恒为"不判定"（8.3） |
| D1-AC-18（P1）seek 尾部、20× HOLD | ① M12 两个性能用例用 ci profile 起后端，不钉核，与 SwiftShader 抢 core2–6；② bus 查询的回复被 zenoh 合并扣到 ResponseFinal，偶发拖到 2.0 s 超时；③ 每次 seek 回复都带约 90 KB 名册，网关每次重建；④ 前端：帧率 4–8 fps、回放打开后的启动瞬态，以及恢复播放时 hz_eff 未知导致外推上限退回 40 ms | ①–③ 已修；④ 定位到代码行并提请求（web-engine） | 服务端 seek 29–44 ms（第 1 轮未分段；修前偶发 2003 ms）；20× 从 play 起第 1 个样本 104 ms 到达、间隔 100 ± 3 ms；浏览器侧 seek p50 272 ms、最大 1083 ms（首个 seek），20× HOLD 11%（全部在 play 后前 4 s） | 前端问题见 8.1；10 min 录制已改用例口径（`--sim-s 600` + 缺口判定），未在本包跑 |

## 2 D1-AC-10（P0）：丢弃的事件有 2、3 条没有补齐

### 2.1 现象与定位

第 1 轮补充测量（`supp/bench_cmd_drop-{1,2,3}`）：3 次中 2 次各剩 2、3 条丢弃事件，订阅端 `gaps_reported 0`、`probes 0`。逐项核对：

| 现象 | 数据 | 说明 |
|---|---|---|
| 未补齐的条数 | run 1 剩 2 条，run 3 剩 3 条 | 都等于同一条消息内的事件数（每步约 2.3 条事件，按 category 合批为一条消息） |
| 缺口检出与补齐 | run 1：检出 3246、补齐 3245；run 3：检出 3451、补齐 3450；run 2：相等 | 未补齐的两次各有 1 个"已检出、未补齐"的缺口，没有任何缺口被报告 |
| 补拉次数与丢弃次数 | run 3：丢弃 193 次、补拉 192 次 | 最后一次丢弃还没来得及发出补拉 |

结论：不是 `_replay` 漏补，而是测量口径问题。`bench_cmd.py` 用同一个 `counting` 标志控制计数与丢弃注入，60 s 窗口后的 1.5 s 排空期内仍在注入，
排空结束时 tick 立即停止 pump。补齐一次丢弃需要"等后续消息检出缺口 + 20 ms 交错等待 + 下一个 16.7 ms tick + RTT"，实测 p50 50 ms、最大 68 ms，
所以最后约 70 ms 内丢掉的那条消息必然计为"未补齐"。

同时核实了一个真实的产品缺口：缺口只能靠后续 seq 检出，生产者最后一批消息被丢、此后不再发事件时，订阅端永远不知道有缺口（现有的 `probe` 只针对尚未见过的辅助生产者，不覆盖这种情况）。
570 条/s 压测中生产者一直在发，不会出现；但 `cmd.succeeded` 之类的零星事件恰好是一批的最后一条时，在 DROP 语义下理论上会永久丢失。

### 2.2 修复

| 改动 | 文件 |
|---|---|
| 测量口径：丢弃只在 60 s 窗口内注入；窗口结束后停止注入，tick 与 pump 继续运行 `--drain` 秒（缺省 1.5 s，大于 1 s 补齐时限），之后仍未交付的丢弃事件才计为未补齐；已经交付（被先到的补拉回复带回）的事件，即使其原消息随后被丢弃也不计入；输出增加 `unrecovered_oldest_ms`；生产者进程的运行时长随 `--drain` 延长 | `tools/bench/ipc/bench_cmd.py` |
| 尾部探测：已跟踪、无未决缺口、距最近一条实时消息 ≥ 0.5 s 的生产者，每 0.5 s 以 `since` = 已交付序号查询一次 `_replay`（不重试，超时 0.5 s）；有新事件即按序交付（`truncated` 时与缺口同样报告）；回复错误或"`truncated` 且无事件"（生产者不可达或纪元已变）时间隔按 2 倍退避，上限 8 s；收到实时消息即恢复 0.5 s。持续发事件的生产者不触发；请求与回复格式不变；只在订阅了总线时启用。新增统计 `tail_probes`、`tail_recovered`、`tail_errors`，与 `replays` 分开 | `python/awr/runtime/events.py` |
| bus 查询 consolidation NONE（见 4.2，同时缩短补拉与命令回复的尾部） | `python/awr/runtime/bus.py` |

开销：空闲生产者每 0.5 s 一次查询，生产者侧 `replay(since == last_seq)` 直接返回空列表，不遍历环。按 3 个订阅者（api、recorder、agent-runtime）×
5 个生产者估算，全系统约 30 次/s 小查询，sim-core 每秒不到 10 次，相对 ≤ 3 ms 的单步预算可以忽略。尾部丢弃的补齐时延上界为 0.5 s + RTT。

### 2.3 验证

- 新增功能用例（`tests/runtime/test_events.py`，LocalBus 与 ZenohBus 各一遍）：
  - `test_tail_drop_recovered_by_quiet_probe`：最后一批消息（seq 4–5）被丢弃、生产者此后静默；断言没有缺口可检出（`gaps_detected 0`），
    1 s 内经尾部探测按序补齐（`tail_recovered == 2`，`replays == 0`）；
  - `test_tail_probe_quiet_producer_and_backoff`：每 20 ms 发一次时不探测；静默生产者按周期探测、无新事件时不交付；生产者不提供
    `_replay` 时退避，收到实时消息后恢复。
- `bench_cmd.py --secs 8 --drop-every 50` 两次短跑（功能自检，不进判定）：每次注入 54–55 次丢弃，补齐 p50 50–51 ms、最大 68–74 ms，
  未补齐 0，未报告缺口 0。

## 3 D1-AC-08（P0）：网关容量工具未实现

### 3.1 定位

- `bench_state.py --clients 3 --with-flight60` 只检查 `awr.api.main` 能否导入，然后照样跑合成写者与读环，第 1 轮记到的 `api_cpu_core` 是读环的 CPU。
- 18 §8.7(2) 规定 MS4 用 `scene=pc&rt=1`、MS5 起用 `scene=full&n=1000`。核对前端：`?rt=1` 没有实现，`scene=pc` 下 `viewport/layers/drones.tsx`
  不订阅 `swarm/state`、`fleet/roster`、`env/state`（`pcOnlyScene()`），这样的客户端只产生 Range 流量，不能代表网关负载。D1 验收在 MS5 之后，应当用 MS5 口径。

### 3.2 实现（`tools/bench/ipc/bench_state.py` 客户端口径、新增 `tools/bench/ipc/flight60_clients.mjs`）

- `--clients K` 时不再用合成写者，改为真实后端：supervisor `--profile perf --only sim-core,api`（PR-6 钉核：api core0、sim-core core1），
  临时 `AWR_RUNS_DIR`、空闲端口；机群缺省为 `--scenario ladder-shenzhen --scenario-profile n1000`，等剧本标记 `ladder.steady`
  （与 gw-10clients 同一负载）；`--scenario none --n N` 时改为 `AWR_SIM_N` 骨架布设（不加载剧本），等 `sim.n_active ≥ N`。
- `--with-flight60`：`flight60_clients.mjs` 启动 K 个独立的 headless Chromium（标志集 C1 与视口取自 `perf/harness/browser.mjs`，
  `PW_CHROME` 可覆盖），各自打开 `/world/<city>?bench=flight60&source=live&scene=full&n=1000`，遮罩揭开、1 s 预热后
  `__perf.reset('all')` 与 `mark('flight.start')`；K 个都开始飞行时打印 `FLIGHT_START`，测量窗口（`--secs`，缺省 60 s）从这一刻起算；
  客户端之间错开 1.5 s 启动，避开首编译与首屏 Range 叠加的启动尖峰。不带 `--with-flight60` 时改用 `rt_client.mjs` 的 K 个轻量客户端。
- 窗口内的度量：api 与 sim-core 的 CPU 取 `/proc/<pid>/stat`（pid 取自 `GET /api/sys/procs`，18 §9.4 第 3 条）；tick 数据年龄取
  `GET /api/sys/perf?window_s=<窗口>` 的 `api.tick_age_p99_ms`（各秒 p99 的窗口 p99，与 harness 同一口径），另报 p50、事件循环延迟 p99、
  编码次数与 sim 字段；窗口结束时取 `GET /api/rt/inspect` 的各连接订阅集与下行速率，核对客户端确实订阅了默认订阅集；窗口内 api 或
  sim-core 重启则本次无效（PERF-E008）；后端或机群起不来时本次记为无效并给出原因，全部无效时退出码 1。
- 门禁：`cpu_api_core ≤ 0.35`（CPU 类，运行内 load 均值 ≥ 6 时"不判定"，PR-5）、`tick_age_ms_p99 ≤ 15`；事件循环延迟 p99 ≤ 10 ms 只报告（P1）。
  `bench-result.json` 记录 `clients`、`with_flight60`、`api_cpu_core`、`tick_age_ms`，与 harness `gw-3clients` 的提取器（`n = 1000`、
  `clients = 3`）对齐，并通过 `awr.bench.result.v1` 校验。

### 3.3 自测

见 5.2。

## 4 D1-AC-18（P1）：seek 尾部时延、20× HOLD、10 min 录制

### 4.1 定位

分段测量（工具：真实 supervisor + Python WS 客户端测服务端；浏览器用例里加了逐次明细与服务端日志，见 4.2 最后一行）：

| 环节 | 数据 | 结论 |
|---|---|---|
| worker 侧 `McapSource.seek`（N = 1000、600 s 合成录制、与用例同一 LCG 序列 20 次） | 14–29 ms | 不是瓶颈 |
| 服务端全程（发出 seek → 收到新纪元首个 BATCH，perf profile 钉核，Python 客户端） | p50 44.5 ms，p95 55.9 ms，最大 56.5 ms | 正常情况下服务端只占 45 ms 左右 |
| 浏览器内服务端分段（api 日志 `playback seek`，修复前，2 次钉核运行 25 条） | 总计 32–73 ms，但有 1 次 `call_ms 2003`（worker 侧 41 ms） | 偶发：回复被扣到 2.0 s 查询超时 |
| 20× 服务端节奏（Python 客户端从 play 起计） | 第 1 个样本 104 ms 到达；间隔 p50 100 ms、p99 103 ms、最大 104 ms；样本仿真间隔 2.00 s（1.92–2.08）；replay-worker 0.37 核、api 0.12 核 | 服务端从播放开始就规整 |

根因：

1. **M12 的两个性能用例没有按 PR-6 钉核**。`perf/m12/common.ts` 的 `startReplayBackend` 用 `--profile ci` 起 supervisor，ci profile 是
   `cpu.pin: off`。harness 把 Playwright runner 包在 `taskset -c 2-6` 里，后端由 spec 进程派生，api、sim-core、replay-worker 全部继承
   core2–6，与 SwiftShader 抢同一组核。M16 PRD §14 第 19 条早已记录"harness 在 ci profile 下不满足 PR-6"。
2. **zenoh 的回复合并**。`ZenohBus._start_query` 用缺省 consolidation（AUTO，对普通查询即 LATEST），zenoh 会把回复扣到查询结束
   （queryable 的 ResponseFinal）才交付。用最小实验核实：queryable 回复后推迟 1.5 s 才 drop，缺省模式下查询方 1532–1556 ms 才拿到回复，
   NONE 模式 1.9–2.0 ms。seek 回复约 169 KB（N = 1000），ResponseFinal 迟到或在 DROP 拥塞下丢失时，回复就要等到 2.0 s 查询超时——
   与观测到的 `call_ms 2003`（worker 侧只用 41 ms）以及第 1 轮最大值 1.4–2.4 s 一致。这一机制对全部 `bus.call`/`call_cb` 都成立（命令准入、
   事件补拉、回放控制）。
3. **每次 seek 都重建名册**。seek 回复的 backfill 每次都带 N = 1000 的名册（约 90 KB，占回复一半），网关每次 `_apply_roster`，
   `load_ms` 10–24 ms。
4. **前端（不在本区域，8.1 提请求）**：浏览器内分段显示服务端回复在 66–192 ms 内到达页面（少数 375 ms），而首帧数据要等主线程下一帧拉取：
   - 回放打开后有数秒的启动瞬态（2–3 fps），首个 seek 的首帧 1.1–2.9 s，第 2 个 0.68–0.71 s；之后 4–8 fps，单次 seek 74–544 ms；
   - 20× HOLD 全部集中在 play 之后的前 3–4 个 1 s 窗口（HOLD 1、1、1、0.333，其后为 0，偶有 0.2）。逐窗口诊断显示这几秒 `hzEff` 为 0
     （0、0、0.2、0.5、1 Hz），页面 2–3 fps。`engine/time/register.ts` 第 193–199 行：推进中且 `hz > 0` 才更新外推上限
     `eMaxMs = rate × 3 × 1000 / hz`，`hz == 0` 时沿用冻结时的下限（回放为块间隔 40 ms 仿真），而 20× 下相邻样本相隔 2 s 仿真，全部机体判为 HOLD；
     `hzEff` 由主线程每帧 ingest 的样本计，低帧率下既起得慢又低于订阅频率（稳态 3.4–4.8 Hz，订阅为 10 Hz、服务端实发 10 Hz）。
5. **10 min 录制未测**：`m12.bench-write` 用的是 `--sim-s 60`，工具没有"缺口为 0"的判定。

### 4.2 修复

| 改动 | 文件 |
|---|---|
| `startReplayBackend` 增加 `profile` 参数（缺省 ci，功能用例 `tests/e2e/timeline.spec.ts` 不变）；seek-latency 与 replay20x 两个性能用例传 `profile: 'perf'`，api、sim-core、replay-worker 分别钉 core0、core1、core7，与 Chromium 的 core2–6 分开；诊断用环境变量 `M12_BACKEND_PROFILE`（临时换 profile）与 `M12_KEEP_LOGS`（保留进程日志） | `apps/web/perf/m12/common.ts`、`seek-latency.spec.ts`、`replay20x.spec.ts` |
| bus 查询以 `consolidation = NONE` 发起：回复到达即交付，不等 ResponseFinal；每个 key 只有一个 queryable、调用方只取第一条回复，不需要合并；无回复检测仍依赖查询结束 | `python/awr/runtime/bus.py` |
| seek 回复的名册与上一次 open/seek 回复相同则置 null（网关对 null 保留当前名册，原有逻辑），回复由约 169 KB 降到约 79 KB，网关 `load_ms` 由 10–24 ms 降到 5–10 ms；open 总是带名册 | `python/awr/recorder/replay.py` |
| 服务端分段可观测：api 每次 seek 记 `playback seek{call_ms, load_ms, total_ms, worker_ms, worker_queue_ms}`；replay-worker 回复带 `queue_ms`（请求在收件箱的等待），处理 ≥ 100 ms 的控制请求记 `replay control slow` | `python/awr/api/rt/playback.py`、`python/awr/recorder/replay.py` |
| replay20x 自带后端，harness 拿不到 pid，AC-18 的"20× 时 api ≤ 0.35 核、tick 数据年龄 p99 ≤ 15 ms"第 1 轮没有测。用例现在在播放前后读 api 的 `/proc/<pid>/stat`，并取 R60 窗口的 `api.tick_age_p99_ms`，写入注解（CPU 按 PR-5 由读者判定，只报告不断言）；另输出逐窗口 HOLD 与时间引擎状态（帧数、hzEff、dWall、dGlobal、maxAge、state4） | `apps/web/perf/m12/replay20x.spec.ts`、`common.ts`（`procTicks`、`viewerAuth`、`perfWindow`） |
| seek-latency 输出逐次明细：首帧时延、`playbackState` 回复到达时刻、目标时刻、api 事件循环延迟（R60），用来区分服务端与页面 | `apps/web/perf/m12/seek-latency.spec.ts` |
| `bench_write.py` 增加缺口判定 `rec_gaps`：`meta.json` 的 `gaps` 为空，块消息数 = 仿真秒 × 25 + 1，相邻块间隔 ≤ 60 ms（经逐 channel 索引，不解压）；`m12.bench-write` 用例改为 `--sim-s 600`（N = 1000、×1、10 min，与 D1-AC-18、M12-AC-034 一致） | `tools/bench/rec/bench_write.py`、`apps/web/perf/m12/cases.mjs` |

### 4.3 验证

- `tests/runtime/test_bus.py::test_reply_delivered_before_query_final`（新增）：queryable 回复后推迟 1.5 s 才 drop，`call` 与 `call_cb`
  都在 0.5 s 内拿到回复；把 consolidation 改回 AUTO 时该用例失败（已核对）。
- 名册省略：`tests/recorder`、`tests/rt/test_playback*.py` 全部通过（回放端到端 `test_replay.py` 的逐字节一致、seek 后逐机 channel 与名册均不变）。
- 浏览器自测见 5.3、5.4。

## 5 自测（排他锁内，快速，不作判定）

### 5.1 时段与条件

| 次 | 内容 | 持锁 | 开跑前 load | 说明 |
|---|---|---|---|---|
| A | seek（perf）、replay20x（240 s 录制）、seek（ci 对照） | 340 s | 3.9 | 其他工作包并行，load 4–7 |
| B | seek（perf，带明细）、replay20x（600 s 录制） | 372 s | 4.0 | 修复 consolidation 之前 |
| C | gw-3clients（ladder n1000）、seek（perf） | 417 s | 3.8 | 修复 consolidation 之前；gw3 运行内 load 13 |
| D | seek（perf）、replay20x（600 s 录制，带逐窗口诊断） | 269 s | 2.1 | consolidation 与名册省略之后 |
| E | gw-3clients（骨架 N = 1000） | 146 s | 3.8 | gw3 运行内 load 均值 14.9 |

### 5.2 D1-AC-08（3 个 headless Chromium 跑 flight60 `scene=full&n=1000`，窗口 60 s，各 1 次）

| 次 | 机群 | api CPU（`/proc`） | tick 年龄 p50 / p99 | sim 单步 p99 / 最大，追帧饱和，RTF | 事件循环延迟 p99 | 运行内 load 均值 | 判定（工具） |
|---|---|---|---|---|---|---|---|
| C | ladder `n1000`（146 s 达到 `ladder.steady`） | 0.042 核 | —（窗口内 sim-core 重启，p99 9789 ms） | 168 ms / 213 ms，38 次，0.91 | 14.0 ms | 13.4 | 无效（PERF-E008：sim-core 重启） |
| E | 骨架 `AWR_SIM_N=1000`（地面静置，不加载剧本） | 0.059 核 | 3.8 / 16.7 ms | 21.9 ms / 61.1 ms，252 次，0.947 | 15.0 ms | 14.9 | CPU 不判定（load ≥ 6）；tick 年龄不通过 |

- 网关本身余量很大：3 个整景客户端、N = 1000 时 api 0.059 核（阈值 0.35），编码 16.9 次/s（同一 swarm 帧各连接共享编码）。
- 窗口末取 `/api/rt/inspect` 核对订阅：每个客户端订阅 `swarm/uav/state@10`、`fleet/roster@10`、`env/state@10`、`event`、`perf/server@1`、
  `sys/procs@1`、`mission/*/status@2`、`uav/*/path@2`，下行约 1.3 Mbit/s；`credit_skips` 约 3600（客户端 4–8 fps，ack 跟着帧走）。
- tick 年龄超标来自 sim-core 发布节奏：单步 p99 21.9 ms、追帧饱和 252 次时，125 Hz 发布出现十几毫秒的空档，网关在 tick 时取到的最新帧就旧了
  （8.2）。其他工作包同时在跑，钉在 core0、core1 的进程也会被别的进程挤占，验收阶段的干净环境下需要复测。
- E 中 3 个页面在负载下遮罩揭开相差约 20 s（TTFP 16–38 s），先飞完的客户端在窗口后段断开，窗口末只剩 2 个连接。已在
  `flight60_clients.mjs` 增加 `--hold`（`bench_state` 传窗口长度 + 2 s）：先飞完的客户端重新载入再飞，保证整个窗口内都有 3 个客户端。

### 5.3 seek（N = 1000，240 s 合成录制，20 次）

| 次 | profile | 首帧时延（ms，按次序） | p50 / p95 / 最大 | 服务端 total（api 日志） |
|---|---|---|---|---|
| A | ci（对照） | 2121、714、316、297、378、178、508、301、219、291、276、471、396、362、155、2493、151、235、257、150 | 301 / 2121 / 2493 | 未记录；第 16 次回复 2097 ms |
| B | perf | 582、467、353、346、389、472、2216、117、448、261、279、297、174、433、222、577、830、366、502、258 | 389 / 830 / 2216 | 32–73 ms，第 7 次 `call_ms 2003`（worker 41 ms） |
| C | perf | 2911、680、439、239、235、218、414、210、164、111、281、292、477、226、395、207、196、444、249、261 | 281 / 680 / 2911 | 32–60 ms；回复到达页面 68–177 ms；api 事件循环延迟最大 7.4 ms |
| D | perf，修复后 | 1083、689、272、330、310、291、354、194、186、308、193、219、74、155、106、198、191、299、544、209 | 272 / 689 / 1083 | 29–44 ms（worker 11–26 ms、排队 ≤ 3 ms、装入 5–10 ms）；回复到达页面 66–375 ms |

说明：B 第 7 次与 A 第 16 次约 2.0 s 的回复在服务端：worker 侧只用 41 ms，回复却在 2.0 s 查询超时时刻才交付，与 4.1 第 2 条的回复合并机制一致（该现象是偶发的，C 中未出现；修复后的 D 中也未出现，修复的效果由 4.3 的回归用例确定性地覆盖）；A、C、D 的首个 seek 与 A 的第 2 个在页面侧（回复几十毫秒就到了，首帧要等页面恢复出帧）。修复后服务端只占
30–45 ms，剩余时延来自页面帧节奏，属于 D1 验收报告 4.2 的同一根因，见 8.1。

### 5.4 20× 回放（N = 1000）

| 次 | 录制 | HOLD 均值 | 逐窗口 HOLD | api CPU | tick 年龄 p99 | 说明 |
|---|---|---|---|---|---|---|
| A | 240 s | 12.7% | — | — | — | 只有 12 个窗口，启动瞬态占比大 |
| B | 600 s | 16.4% | 1、1、1、0.333、0.75、0、1、0、0.333，其后全 0 | 0.090 核 | 11.3 ms | 修复前 |
| D | 600 s | 11.0% | 1、1、1、0.333，其后 0（两次 0.2） | 0.088 核 | 7.3 ms | 逐窗口：前 4 s `hzEff` 0–1 Hz、页面 2–3 fps；稳态 4–8 fps、`hzEff` 3.4–4.8 Hz、`dWall` 300 ms、BUFFERING 0 |

AC-18 的"20× 时 api ≤ 0.35 核、tick 数据年龄 p99 ≤ 15 ms"两项在单客户端下满足；HOLD 全部来自 play 之后的前端启动瞬态，稳态为 0（8.1）。
第 1 轮 2.5% 低于本包自测，是因为第 1 轮机器上没有其他工作包；本包自测时 load 2–13，页面帧率更低，启动瞬态拉得更长。

## 6 改动文件清单

| 文件 | 所有者 | 改动 |
|---|---|---|
| `python/awr/runtime/events.py` | M11 | `EventSubscriber` 尾部探测（2.2） |
| `python/awr/runtime/bus.py` | M11 | 查询 consolidation NONE（4.2） |
| `python/awr/api/rt/playback.py` | M11 | seek 服务端分段日志与 `seek_ms`（4.2） |
| `python/awr/recorder/replay.py` | M12（recorder，本区域） | seek 回复名册不变则省略；`queue_ms`；慢请求日志（4.2） |
| `tools/bench/ipc/bench_cmd.py` | M11 | 丢弃注入只在窗口内、排空期、已交付事件不计入、`--drain`（2.2） |
| `tools/bench/ipc/bench_state.py` | M11 | 客户端口径：真实后端 + K 个客户端、`/proc`、R60 与 inspect 采集、门禁与输出（3.2） |
| `tools/bench/ipc/flight60_clients.mjs`（新增） | M11 | K 个 headless Chromium 跑 flight60 的编排（3.2） |
| `tests/runtime/test_events.py` | M11 | 尾部探测两个用例（2.3） |
| `tests/runtime/test_bus.py` | M11 | 回复先于 ResponseFinal 交付的回归用例（4.3） |
| `apps/web/perf/m12/common.ts` | M12 性能用例驱动（越区：D1-AC-18 的测量环境，理由见 4.1 第 1 条） | `profile` 参数、诊断环境变量、`procTicks`、`viewerAuth`、`perfWindow` |
| `apps/web/perf/m12/seek-latency.spec.ts` | 同上 | perf profile；逐次明细与 api 事件循环延迟 |
| `apps/web/perf/m12/replay20x.spec.ts` | 同上 | perf profile；api CPU、tick 年龄、逐窗口诊断 |
| `apps/web/perf/m12/cases.mjs` | 同上 | `m12.bench-write` 改为 10 min |
| `tools/bench/rec/bench_write.py` | M12 recorder 基准 | 缺口判定 `rec_gaps` |
| `docs/17-接口与实时协议规范.md` | 文档 | §9.4 查询 consolidation NONE；§9.5 尾部探测；§6.11 补充约定第 3 条 seek 名册可为 null |
| `docs/18-性能与测试方案.md` | 文档 | §8.7(2) 3 个浏览器行、§9.5 `?rt=1` 行 |
| `docs/modules/M11-实时网关PRD.md` | 文档 | M11-FR-006、FR-009、AC-004 |
| `docs/modules/M12-时间轴录制与回放PRD.md` | 文档 | M12-FR-045、AC-034、AC-063、AC-065 |
| `docs/modules/M16-演示数据剧本与流畅性测试PRD.md` | 文档 | §14 第 19 条现状 |
| `docs/impl/FX2-R2-gateway-报告.md` | 文档 | 本报告 |

## 7 规格与文档变更

- 没有改动任何阈值，没有新增 ADR。依 03 §1.3，行为与测量口径的变化都在对应规范与 PRD 中修订：
  - 17 §9.4：bus 查询一律 consolidation NONE（依据：4.1 第 2 条的实验与 2003 ms 观测）；
  - 17 §9.5、M11-FR-009、M11-AC-004：尾部探测与 `bench_cmd` 丢弃注入口径（依据：2.1）；
  - 17 §6.11 补充约定第 3 条、M12-FR-045：seek 回复名册不变时为 null（依据：4.1 第 3 条）；
  - 18 §8.7(2)、§9.5：D1 验收起 D1-AC-08 只用 MS5 口径（依据：3.1，前端未实现 `rt=1`）；
  - M12-AC-034、AC-063、AC-065：10 min 录制与缺口判定、性能用例钉核；M16 §14 第 19 条现状。
- D1-AC-18 的 seek ≤ 500 ms 与 HOLD < 1% 不是"暂定"阈值，本包不建议放宽：服务端已降到 30–45 ms，剩余部分是前端缺陷（8.1），不是可达上限。

## 8 遗留问题与建议

### 8.1 提给 web-engine（`apps/web/src/engine/time`、帧节奏）

1. **恢复播放时外推上限退回块间隔**（D1-AC-18 20× HOLD 的全部来源）：`engine/time/register.ts` 第 193–199 行，`clock.advancing` 且 `hz == 0`
   时不更新 `interp.eMaxMs`，沿用冻结时的下限（回放为 `blockMs` 40 ms 仿真）。play 之后 `hzEff` 要数秒才大于 0（低帧率下由每帧 ingest
   计数），这几秒内 20× 的样本相隔 2 s 仿真，全部机体判为 HOLD。建议：`hz` 未知时按订阅的名义频率（Tier S 10 Hz）计算
   `eMaxMs = rate × extrapIntervals × 1000 / 名义频率`，或以 Worker 收到样本的时刻（`swarmRecvMainMs`，与帧率无关）而不是主线程 ingest 计
   `hzEff`。按 5.4 D 的逐窗口数据，修复后 HOLD 应接近 0。
2. **回放打开后的启动瞬态**：openReplay 之后数秒页面只有 2–3 fps，首个 seek 的首帧 1.1–2.9 s。服务端此时已空闲（open 272 ms 完成，
   seek 30–45 ms）。需要页面侧剖析（N = 1000 回放名册装入、机体实例与 hero 档、着色器首编译等）。
3. **帧节奏**：稳态 4–8 fps 决定了 seek 首帧时延（等下一帧拉取）与 `hzEff`。同 D1 验收报告 4.2。

### 8.2 提给 sim（D1-AC-08 的 tick 年龄）

- tick 数据年龄度量的是"tick 时刻 − 最新帧发布时刻"，sim-core 以 125 Hz 规整发布时上界约 8 ms；超过 15 ms 意味着 sim-core 发布不规整。
  本包 ladder n1000 自测中 sim-core 在窗口内重启（判无效），重启前单步 p99 168 ms、追帧饱和 38 次。D1-AC-08 能否达标取决于 N = 1000 下
  sim-core 的稳定性与单步时延（D1 验收报告 4.1、4.4、4.6）。

### 8.3 其他

- `perf/skeleton.server.ts` 仍用 ci profile 起后端（不钉核），属于 M16，建议同样改为 perf profile（M16 §14 第 19 条）。
- `ipc.cmd` 用例不带 `--drop-every`，D1-AC-10 的"缺口 1 s 内经 replay 补齐"第 1 轮是补充测量。建议 M16 增加一个 `--drop-every 100` 的用例
  （或在 `ipc.cmd` 中加上；带丢弃注入时 RTT 与 570 条/s 的缺口、乱序判据不变）。
- PR-5 与 D1-AC-08：3 个 SwiftShader 浏览器本身就会把运行内 load 推到 6 以上（本包 ladder 自测运行内均值 13.4），CPU 门禁按 PR-5 将始终"不判定"。
  api 钉在 core0、只与本用例竞争，建议验收阶段确认该口径；若需判定，可在 03 以 ADR 为 D1-AC-08 规定"以开跑前 load（PR-2）为准"，本包不擅自改。
- `make lint` 当前失败的 5 处都不在本区域，均为其他工作包进行中的改动：ruff 4 处（`python/awr/sim/fleet/kernels_l1.py`、`kernels_watch.py` 两处、`stages/tap.py`）、`check_py_imports` 3 处（`python/awr/sim/runtime/warm.py` 的 import 边界）、`m06-lint` 1 处（`apps/web/src/viewport/backend/webgl2.ts` 同步 `readPixels`）。本区域全部文件 ruff 通过，M12 性能用例 oxlint 与 tsc 通过（第 9 节）。

## 9 测试

功能测试都在共享锁下运行（与性能用例互斥），python 代码的最后一次修改之后全部重跑：

| 范围 | 命令 | 结果 |
|---|---|---|
| runtime、rt、recorder | `pytest -m "not perf" tests/runtime tests/rt tests/recorder` | 272 通过（3 个 perf 用例按规则排除） |
| agent、重建、jobs、契约、bus、事件、supervisor | `pytest -m "not perf" tests/agent tests/reconstruction tests/jobs tests/contracts tests/runtime/test_bus.py tests/runtime/test_events.py tests/runtime/test_supervisor.py` | 808 通过、2 失败：失败的是 `tests/contracts/test_lint_tools.py` 的两个全仓 lint 用例，违规点在 `python/awr/sim/core/command.py:182`（编辑中的语法错误）与 `python/awr/sim/runtime/warm.py:54`（import 边界），均为 sim 工作包进行中的改动，与本区域无关 |
| 工具冒烟（不进判定） | `bench_cmd.py --secs 8 --drop-every 50`（2 次）；`bench_state.py --clients 1 --scenario none --n 10`（rt_client 与 flight60 各 1 次，后者含 `--hold`）；`bench_write.py --n 50 --sim-s 5` | 全部跑通，输出通过 `awr.bench.result.v1` 校验 |
| ruff | `ruff check python/awr/runtime python/awr/api python/awr/recorder tools/bench/ipc tools/bench/rec tests/runtime tests/rt tests/recorder` | 通过 |
| oxlint、tsc | `npx oxlint --type-aware perf/m12/`、`npx tsc --noEmit` | 通过 |
| make lint | `make lint` | 本区域通过；整体失败于其他区域进行中的改动（8.3） |
