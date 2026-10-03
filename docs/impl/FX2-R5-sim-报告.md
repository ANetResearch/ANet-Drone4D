# FX2-R5-sim 仿真内核：单步尾部、风暴与并发（D1 第 5 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R5-sim（区域 sim：`python/awr/sim`、`python/awr/environment`、`scenarios` 与 sim 侧工具） |
| 日期 | 2026-10-03 |
| 依据 | `docs/impl/D1-验收报告-第4轮.md`（§3、§4.1、§5、第 6–7 节）；P4-SIM、FX2-R3-sim 报告；AWR-03 §1.3、§8.4、ADR-033、ADR-051、ADR-057、ADR-070、ADR-073；AWR-18 §3（PR-1 至 PR-12）、§7.4、§7.6、§12.3；M08、M10 PRD |
| 分派项 | D1-AC-07（P0，N = 1000 单步 p99 3.051 ms）；D1-AC-27 的 P1 子项（link_drop 单步最大 32.6 ms）；D1-AC-28（P1，客户端并发单步 p99 4.13 ms） |
| 环境 | VMware 虚拟机 8 vCPU（E5-2603 v4 1.7 GHz）；Python 3.12（`.venv`，numba 0.67、numpy 2.5）；Chrome for Testing 151（SwiftShader）。同机有 FX2-R5 的 web-engine、web-ui 等工作包与其他会话，排他锁之外 load 1–14 |
| 约束执行 | 没有安装依赖；没有执行 git 写操作（只读 `git status`）。性能自测都持排他锁 `runs/.perf.lock`（工具内等 load ≤ 4），每次持锁 2.5–7 min（< 10 min）；诊断钩子（逐轮记录器、M10 失败记录、core1 采样器）只在暂存目录，不进仓库。没有改动 UI；无 emoji 与禁用字形；`make lint` 通过（第 5.1 节） |
| 规格变更 | 新增 AWR-03 ADR-074（附录 E 正文与 §7.0 索引）；修订 M08-FR-004 与 §7.1 `ctl/sim-core/{roster,query}` 行、§7.3 `GET /api/fleet/vehicles` 行，M10-FR-069（新增⑤）与 §6.8 coverage 行，AWR-03 §8.4 D1-AC-28 的依据列，AWR-18 §7.4。**不放宽任何阈值、不改 `fleet_ladder` 的口径与测量窗口、不冻结暂定阈值** |

## 1 结论摘要

| AC | 第 4 轮 | 根因（已核实） | 本轮处理 | 自测（排他锁，单次运行，只作修复验证） |
|---|---|---|---|---|
| D1-AC-07（P0） | p99 3.051 ms（3.051、2.891、3.108），各秒 p99 中位 2.69–2.75 ms，越线的都是窗口第 0 秒 | ①工具在 `ladder.steady` 之后、开窗口之前的 `ctl/sim-core/roster` 请求：sim-core 在 drain 内同步重建 1000 个条目、整体编码并经 zenoh 回复，一轮 6.5 ms（不是验收诊断推测的 30 s 转场重试）；②sim-0406 的转场每次都被地理围栏拒绝、每 30 s 重试一次，每次下发与准入约 7 ms，落在窗口第 17、47 秒；③state_ext 兜底片不看本轮轻重，约一半落在剩余预算不足 600 µs 的轮次；④coverage stage 在环绕任务上空转 0.72 ms | roster 回复预编码并交给后台发布线程；转场被拒的记忆；state_ext 兜底片挑轮、拼接移出主线程；coverage 只处理覆盖类任务（ADR-074 第 1、3–6 条） | 最终代码 2 次：**p99 2.89、2.80 ms**，各秒 p99 中位 2.64、2.60 ms，60 s 中越线 0 秒，最大 4.57、2.94 ms，RTF 1.000，CPU 0.534 核，饱和 0。同机其他进程落到 core1 时单秒可越线（第 2.6 节） |
| D1-AC-27 link_drop（P1） | 单步最大 32.6 ms（34.0、32.6、32.4），注入开始约 1.3 s 一次孤立长迭代 | 注入前的 `GET /api/fleet/vehicles`：`fleet/vehicles` 在一次慢任务内构造并编码 1000 架（约 0.73 MB），逐轮 61 ms；故障注入本身每轮 ≤ 11.5 ms | 全表查询分片，拼接与回复交给后台发布线程（ADR-074 第 2 条） | sim 侧复现（Python 客户端）：32.7 → **5.5 ms**；harness `storm.linkdrop`（浏览器，2 次）：**7.30、9.37 ms**，LoAF 0、Toast 3、多渲染 9 行 |
| D1-AC-28（P1） | p99 4.13 ms（4.01、4.13、24.13），21–26 秒越线 | 3 个 SwiftShader 客户端占满 core2–6 时，内存带宽与末级缓存争用使 stage 单次调用的尾部拉长 13–58%（线程 CPU 同步增长、随机落在各 tick 对），不是可调度的固定开销 | sim 侧固定开销已移出（同上）；**单步 p99 降级为已知问题并列入路线图**，阈值不放宽，最大、饱和、tick 数据年龄照常判定（ADR-074 第 7 条） | 2 次：p99 5.15、5.02 ms，各秒中位 2.78、3.06 ms，最大 6.90、6.50 ms，**饱和 0、tick 数据年龄 p99 10.84、10.14 ms、RTF 1.000** |
| 发现：state_ext 刷新率 | — | N = 1000 时 state_ext 每片固定开销约 0.6 ms，逐机上包络使片长停在 8 架、全靠饿死兜底推进，全机约 7 s 刷新一轮，不是 ADR-051 的 2 Hz | D1 不提高刷新率（2 Hz 需约 0.07–0.09 核，会把 D1-AC-07 的 CPU 推到门禁附近），列为已知问题与 V0.2 路线图（ADR-074 第 8 条） | 16 s 内 280 片 × 8 架；拼接移出主线程后主线程不再有 1–5 ms 的发布尖峰 |

## 2 定位与根因

### 2.1 方法

- 生产口径：`tools/bench/fleet_ladder/run.py --n 1000 --dur 60 --runs 1`（真实 supervisor + sim-core，ladder n1000，`ladder.steady` 之后 60 s；工具不钉核，与
  harness 的 `fleet-ladder` 用例相同）与 `--with-recorder --with-checkpoint --clients 3 --with-flight60`（工具在 `taskset -c 2-6` 下，与 harness 的
  `fleet-ladder.concurrent` 相同）。
- 诊断钩子（暂存目录的 `sitecustomize`，不进仓库）：逐轮记录墙钟、线程 CPU、run-queue 等待（`/proc/thread-self/schedstat`）、等锁时长、各 stage、
  各慢任务与各 drain 请求类别（`drain.roster`、`drain.cmd` 等）的墙钟；长迭代另记 M10、M09、CommandEngine 的函数级耗时；M10 轨道的失败结果与转场
  规划结果；core1 线程占用采样器（在 core2–6 上运行）。钩子自身约 3–5% 开销，且在记录结束时落盘（落在窗口末尾的那一秒）。
- link_drop 的 sim 侧复现：live ladder n1000（sim-core + api），`ladder.steady` 后 5 s，Python 客户端按 `storm.spec.ts` 的顺序取 operator token、
  `GET /api/fleet/vehicles`、500 条 `POST /api/fleet/vehicles/{id}/faults {link_drop}`（每批 20 条并发），1 Hz 读 StateRing 头部；生产口径另以 harness
  `storm.linkdrop` 运行 1 次。

### 2.2 D1-AC-07：越线秒是工具自己的 roster 请求

- 第 4 轮 3 次运行的逐秒序列：N = 1000 越线的都是**第 0 秒**（运行 1、3 的该秒 p99 3.051、3.108 ms，最大 3.74、3.67 ms；运行 2 第 0 秒恰好没有第二个
  大迭代），N = 100、200 越线在第 52 秒、N = 500 在第 27 秒（都在约 97–102 s【仿真】，判定规模之外）。
- `fleet_ladder` 见到标记后先调用 `load.roster()`（`ctl/sim-core/roster`）取 id，再采样 `_proc_pid` 与头部、开窗口；头部第一个 1 s 统计段覆盖这次请求。
  sim-core 在步边界 drain 内执行 `roster.snapshot()`（1000 个条目字典）、`msgpack` 整体编码与 zenoh 回复（约 160 KB）。逐轮记录（`base2`）：该轮
  `drain.roster` 6555 µs、3 个 tick、11.1 ms，均摊 3.7 ms，正是该秒的最大值；同秒再有一个重 tick 对即越线。修复编码后（`fix2`）这一项仍 3388 µs，
  其中约 2.7 ms 是 zenoh 回复本身（进程内测得重拼 0.7 ms）。
- 网关在 `roster.changed` 后、recorder 与 agent-runtime 在启动时都走同一路径，所以这是产品路径的问题，不是只修工具：本包修产品路径，工具与口径不变。

### 2.3 D1-AC-07：每 30 s 一次的转场重试

- 逐轮记录在窗口内看到两次"mission 5.3–6.6 ms + ingest 2.0 ms"的迭代（127.7、157.8 s【仿真】，相隔 30.1 s），落在窗口第 17、47 秒；`fix3-b` 第 47 秒
  p99 3.049 ms 即此叠加。
- M10 失败记录（`mlog1`）：sim-0406（l1 层）的转场由 profile 规划器给出长 679 m、**最高点 377.76 m**、335.5 s 的轨迹，follow_path 每次以 **102
  GEOFENCE_REJECT** 失败，按 0.5·2^k s（上限 30 s）退避续飞：62.9、63.5、64.6、66.7、81.2、97.3、127.4、157.5 s……sim-0737 在 45.8、48.8 s 两次 102
  后转场成功。N = 100–500 在约 97 s 的越线秒与这串重试时刻一致。
- 规划器为何把该机的转场抬到约 378 m（出生点附近 DSM 只有 0.2–24 m，高度上限与围栏不一致）属 M10、M04 与 M16，本包没有改规划器与剧本，只让
  确定性的同一转场不再重复下发（第 3.6 节）。

### 2.4 D1-AC-07、28：state_ext 的兜底片与 coverage

- state_ext 每片的固定开销（M13、M09 两个钩子与 M08 字段的十余次小数组 numpy 运算）进程内约 0.42 ms、生产口径约 0.6 ms，逐机约 25–35 µs；8 架一片
  生产口径 0.9–1.2 ms。ADR-073 第 3 条的逐机上包络把固定开销摊进逐机估计（130–150 µs/架），`int(min(48, budget / per))` 在每轮最大预算 1000 µs 下
  恒为 7 以下，于是片长停在下限 8、每片都由 50 ms 饿死兜底强制推进，而兜底此前不看本轮轻重。
  - 无客户端时各秒第 1–3 大的迭代中 78% 含一片 state_ext（`base2`），客户端并发时 36%（P4-SIM 的 `c7`）；它们是单步 p99 的主要来源之一。各片中
    48% 落在剩余预算不足 600 µs 的轮次、26% 落在管线耗时最重的三成 tick 对上；挑轮之后（`fix2`）为 8%、1%，各秒第 2、3 大的迭代中含 state_ext 的
    只剩 9/114（其余为 env 全量所在的 (5, 6)、(35, 36) 与 battery_rtl 所在的 (17, 18) 等重 tick 对本身）。
  - 全机刷新率：16 s 内 280 片 × 8 架，约 7 s 一轮（第 3.8 节）。
  - 末片之后的拼接在主线程上：`_ext_publish` 生产口径 1.0–5.4 ms（约 0.6 MB 的两次大块分配与复制）。
- coverage stage（5 Hz，tick 对 (1, 2)）对 ladder 的 4 个 orbit 任务逐轨道筛选 `WORKING` 轨道（1000 个对象），之后什么也不做，每次约 0.72 ms，
  使 (1, 2) 成为最重的 tick 对（p50 5.2 ms）。

### 2.5 D1-AC-27：link_drop 的长迭代是注入前的全表查询

- sim 侧复现（`ld1`，第 4 轮代码 + roster 修复）：注入后 +1.0 s 单步最大 32.7 ms、饱和 +1；该轮 `slow.query` 61 119 µs（钩子下），即
  `fleet/vehicles` 的同步构造（解码已发布的 state_ext 行、组装 1000 个条目、整体编码约 0.73 MB 并回复）。500 条故障注入本身每轮 9–11.5 ms（`faults`
  2–3.7 ms、`drain.cmd` 1–2.2 ms，均摊 ≤ 5.8 ms），没有超过 12 ms 的。
- 分片之后（`ld2`）最大 6.5 ms，但最后一片之后的拼接与 zenoh 回复仍在主循环内 9.4 ms；改由后台发布线程发出后（`ld3`）全程最大 5.5 ms。

### 2.6 D1-AC-28：争用下的尾部拉长与同机干扰

- 同一代码无客户端（`fix2`）与 3 个 flight60 客户端（`conc1`）的逐轮记录对比（每次调用的平均 / p99，µs）：

  | stage | 无客户端 | 客户端并发 | 平均 / p99 之比 |
  |---|---|---|---|
  | l1 | 813 / 1076 | 815 / 1226 | 1.00 / 1.14 |
  | env | 1012 / 1691 | 1079 / 1840 | 1.07 / 1.09 |
  | mission | 493 / 697 | 528 / 931 | 1.07 / 1.34 |
  | sensors | 660 / 976 | 755 / 1195 | 1.14 / 1.22 |
  | guard | 553 / 784 | 546 / 887 | 0.99 / 1.13 |
  | contact | 121 / 184 | 162 / 291 | 1.34 / 1.58 |
  | cmd_watch | 516 / 752 | 551 / 866 | 1.07 / 1.15 |

  每 tick CPU 的 p99 2.73 → 3.01 ms，离核 > 0.5 ms 的迭代 0.28% → 0.68%；大于 7 ms 的迭代多为单个 stage 的单次调用拉长（env 2.6–4.4 ms、mission
  2.3–6.6 ms、l1 3.3–4.9 ms、sensors 3–9 ms），线程 CPU 与墙钟同步增长，落在任意 tick 对上。各秒 p99 中位 2.69 → 2.92 ms（钩子口径），约 4 成的秒越过
  3 ms。sim 侧能调度的固定开销（state_ext 兜底片、coverage、roster、转场重试）已移出，剩下的不是调相或拆分能解决的（ADR-074 备选⑥）。
- 同机干扰（与 D1-AC-07 的复测相关）：`d1` 在 core2–6 上采样 core1 的线程占用 70 s：sim-core 主线程 36.7 s、拷贝线程 0.31 s，另有其他会话的
  `tsgolint` 共 0.61 s、node 0.07 s。本机没有 `CAP_SYS_NICE`，sim-core 不能提高优先级；`fix1-a`（第 8–10、20、32 秒）、`fix4-a`（第 9 秒）、`fix5`
  （第 12、15 秒）的越线秒都出现在其他工作包活跃（取锁时 load 6–7，工具等到 ≤ 4 才开跑）的时段，不带钩子无法逐轮归因，带钩子的 `fix2`、`d1` 除钩子
  落盘那一秒外都 ≤ 3 ms。验收按 ADR-033 在无其他工作包时运行。

## 3 修复（ADR-074）

1. **roster 回复预编码与后台发送**（`python/awr/sim/core/roster.py`、`runtime/main.py`）：`Roster` 在 `add` 与生命周期转移（`_set`）时生成各条目的
   msgpack 编码并按 slot 缓存，`snapshot_packed()` 拼接映射头与各条目字节，与 `msgpack.packb(snapshot())` 逐字节相同；整份回复按
   (roster_version, lc_gen, 条目数) 缓存；条目编码"可信"（只经本类方法改动）时直接按 slot 序取出，checkpoint 恢复直接改写 `by_slot` 与
   `roster_version`、或直接改写生命周期后 `touch()` 时逐条按对象身份与生命周期校验后重编。drain 只取字节，sim-core 进程中由空闲窗口发布线程
   （`GatedPublisher.submit`）回复。进程内 N = 1000：命中约 3–9 µs、可信时重拼约 0.5 ms、逐条校验约 1.0–1.4 ms。
2. **`fleet/vehicles` 全表查询分片与后台回复**（`runtime/main.py`、`bgpub.py`）：N > 64 时 `_slow_query` 只把查询转入分片作业，新增不可分片慢任务
   `vehicles`（公平轮转、积分与借贷、20 ms 饿死上界），每次按剩余预算与逐机耗时上包络编码 8–64 架，作业存在超过 200 ms 后每次 64 架；每个条目按
   键序拼接 11 个键值对的编码：静态字段（id、agent_no、profile_id、model、backend、home_enu_m）按条目对象缓存，生命周期与 (flight_state, sub) 按
   取值缓存，位置逐次编码，state_ext 直接截取最近一次发布行中的值字节（不解码、不重编），缺失时现算。全部完成后拼接与 zenoh 回复交给
   `GatedPublisher.submit`。进程内 N = 1000：同步构造 + 编码 48 ms → 分片后逐机约 9 µs（64 架一片约 0.6 ms）；回复与同步构造逐字节相同。单机查询与
   N ≤ 64 仍同步；暂停时有作业则主循环不阻塞等待。
3. **state_ext 拼接移出主线程**（`bgpub.py`、`runtime/main.py`）：`GatedPublisher.put_parts(pub, head, parts)`，发布线程在空闲窗口内 `b"".join` 后
   `put`，载荷逐字节不变。
4. **state_ext 兜底片挑轮**（`runtime/main.py`）：饿死后只在本轮剩余预算 ≥ 600 µs（`EXT_FORCE_MIN_US`）的轮次强制最小片，连续 150 ms
   （`EXT_STARVE_HARD_NS`）未推进时不再挑轮。
5. **coverage 只处理覆盖类任务**（`python/awr/sim/mission/coverage.py`）：生成器不属于 helix_scan、lawnmower、corridor、terrain_follow、formation
   时跳过逐轨道筛选。
6. **转场被拒的记忆**（`python/awr/sim/mission/engine.py`，M10-FR-069 ⑤）：转场 follow_path 以 102 失败后记下该转场（规划器、长度、最高点、生效区、
   机体位置）；之后规划出相同的转场（长度与最高点各差 < 1 m、机体离记下的位置 ≤ 1 m、规划器与生效区相同）时不下发、保持 SUSPENDED 并按原退避
   续飞；转场不同或某次转场成功后照常下发并清除记忆。其他失败码不记忆。
7. 阈值与口径：D1-AC-07、27、28 的全部条款，`fleet_ladder` 的"窗口内各秒 p99 的最大值"与测量窗口都没有改；D1-AC-28 的单步 p99 与 state_ext 刷新率
   在 ADR-074 第 7、8 条列为已知问题与路线图，豁免记录（AWR-18 §12.3）由评审人登记，本包不代为批准、没有改 `waivers.yaml`。

## 4 改动文件

- sim-core：`python/awr/sim/core/roster.py`（条目编码缓存、`snapshot_packed`）、`python/awr/sim/runtime/main.py`（roster 回复、`fleet/vehicles` 分片作业与
  `vehicle_items_packed`、`vehicles` 慢任务、state_ext 兜底片挑轮与 `put_parts`、暂停时作业不阻塞）、`python/awr/sim/runtime/bgpub.py`（`put_parts`、
  `submit`）。
- M10：`python/awr/sim/mission/coverage.py`（`_STAMP_GENS`）、`python/awr/sim/mission/engine.py`（`GEOFENCE_REJECT`、`TrackRT.tr_key`、`tr_reject`、
  `_transit_key`）。
- 用例：新增 `tests/sim/test_roster_packed.py`（2 例）、`tests/sim/test_vehicles_sliced.py`（4 例）、`tests/mission/test_coverage_skip.py`（2 例）、
  `tests/mission/test_transit_reject_memo.py`（3 例）；`tests/sim/test_bgpub.py`（+2 例：`put_parts`、`submit`）、`tests/sim/test_state_ext_budget.py`
  （+1 例：兜底片挑轮）。
- 文档：`docs/03-设计基线与决策记录.md`（ADR-074 正文与 §7.0 索引行，§8.4 D1-AC-28 依据列）、`docs/18-性能与测试方案.md`（§7.4 的 D1 状态）、
  `docs/modules/M08-仿真内核与飞行器适配PRD.md`（M08-FR-004，§7.1 roster 与 query 行，§7.3 `GET /api/fleet/vehicles` 行）、
  `docs/modules/M10-任务规划与集群PRD.md`（M10-FR-069 ⑤，§6.8 coverage 行），本报告。

## 5 测试

### 5.1 功能测试

- 新增与修改的用例（单独运行全部通过）：`tests/sim/test_roster_packed.py` 2、`tests/sim/test_vehicles_sliced.py` 4、`tests/sim/test_bgpub.py` 6（+2）、
  `tests/sim/test_state_ext_budget.py` 3（+1）、`tests/mission/test_coverage_skip.py` 2、`tests/mission/test_transit_reject_memo.py` 3；相关的
  `tests/mission/test_coarse_memo.py`、`test_orbit_goto.py`、`test_coverage_grid.py` 通过。
- 整批（共享锁，独立 `NUMBA_CACHE_DIR`）：`pytest -m "not perf" tests/sim tests/safety tests/environment tests/mission tests/sensors tests/planning
  tests/swarm tests/scenarios tests/runtime tests/rt tests/contracts tests/recorder tests/agent tests/e2e/test_scenarios_static.py`：**1813 通过、3 失败**
  （30 min 41 s）。失败的 3 例是 `tests/environment/test_env_e2e.py` 两例（503）与 `tests/sensors/test_pose_chain_e2e.py::test_roster_sensors_pose_topic_and_state_ext`，
  单独运行 3 例全部通过（4.6 s），与 FX2-R3-sim、P4-SIM 报告记录的整批次序依赖相同；其中后者走 roster 与 state_ext 路径，单独运行通过即说明
  预编码回复与原回复一致。
- 排他锁下：`tests/chaos/test_chaos_core.py`（kill -9 api 与 sim-core）、`tests/chaos/test_chaos_ext.py::test_checkpoint_recovery`（checkpoint 恢复直接
  改写 roster 的路径）与 `tests/e2e/test_scenarios.py::test_s1`（多进程，含 ×10 墙钟断言）4 例通过（2 min 36 s）。
- `make lint` 通过（ruff、oxlint、tools/lint 全部规则、harness 注册表 108 个用例），文档改动之后复跑仍通过。
- 已知且与本包无关：perf 标记的 `tests/sim/test_checkpoint.py::test_checkpoint_copy_under_1ms` 在未持锁、load 4.5 时超时（P4-SIM 报告已记录同类现象），
  不在功能门禁内。

### 5.2 自测（排他锁，工具内等 load ≤ 4）

表 1　`fleet_ladder --n 1000 --dur 60 --runs 1`（无客户端，不带诊断钩子；p99 为工具口径"各秒 p99 的最大值"）：

| 运行 | 代码状态 | 取锁时 load（工具等到 ≤ 4 才开跑） | RTF | CPU（核） | p99 / 各秒 p99 中位 / 最大（ms） | 饱和 | 越线的秒（p99，ms） |
|---|---|---|---|---|---|---|---|
| 第 4 轮验收 | 第 4 轮代码，3 次中位 | ≤ 4 | 1.000 | 0.529 | 3.051 / 2.69–2.75 / 5.65 | 0 | 运行 1、3 的第 0 秒 |
| fix1-a、b | 第 3 节第 1（只编码）、2（回复仍在主循环）、3–5 条 | 6.1 | 1.000 | 0.528（2 次中位） | 5.29、2.84 / 2.54、2.62 / 5.92、6.52 | 0 | a：第 8–10、20、32 秒（3.3–5.3） |
| fix3-a、b | + roster 与全表查询后台回复 | 5.9 | 1.000 | 0.538 | 2.80、3.05 / 2.59、2.53 / 6.78、6.22 | 0 | b：第 47 秒 3.05（转场重试同秒） |
| fix4-a、b | + 转场被拒的记忆 | 6.9 | 1.000 | 0.532 | 3.38、2.87 / 2.54、2.61 / 3.94、3.96 | 0 | a：第 9 秒 3.38 |
| fix5 | 同上 | 6.2 | 1.000 | 0.528 | 5.09 / 2.61 / 5.74 | 0 | 第 12、15 秒（5.09、4.10） |
| **fin1-a、b** | **最终代码**（+ 条目编码可信时直接拼接） | 5.5 | 1.000 | 0.534 | **2.89、2.80** / 2.64、2.60 / 4.57、2.94 | 0 | 无 |

带钩子的运行（`fix2`、`d1`，最终代码之前一版）各秒 p99 中位 2.69、2.75 ms（钩子口径），除钩子在窗口末尾落盘的第 58 秒外 0 秒越线；`d1` 中 roster 那一轮
`drain.roster` 2.7 ms（此时还是逐条校验，第 3 节第 1 条的"可信时直接拼接"即为此加入）。

表 2　客户端并发（`--with-recorder --with-checkpoint --clients 3 --with-flight60`，工具在 `taskset -c 2-6` 下，AffinityGuard 每次改回 24 个线程）：

| 运行 | 状态 | RTF | sim-core（核） | p99 / 各秒中位 / 最大（ms） | 越线秒数 | 饱和 | tick 年龄 p99（ms） | api / recorder（核） | 窗口 load 均值 / 最大 |
|---|---|---|---|---|---|---|---|---|---|
| 第 4 轮验收 | 3 次中位 | 1.000 | 0.556 | 4.13 / 2.88–2.93 / 6.95 | 21–26 | 0 | 10.99 | 0.096 / 0.105 | 11.5–12.8 / 16.6–17.7 |
| conc1 | 带钩子（roster 后台回复与转场记忆之前） | 0.9999 | 0.575 | 5.15 / 2.78 / 6.90 | 16 | 0 | 10.84 | 0.147 / 0.103 | 13.3 / 19.4 |
| conc2 | 不带钩子（含转场记忆；条目编码可信时直接拼接之前） | 1.0001 | 0.561 | 5.02 / 3.06 / 6.50 | 35 | 0 | 10.14 | 0.148 / 0.103 | 13.5 / 17.2 |

表 3　link_drop：

| 运行 | 代码状态 | 单步最大（ms） | 说明 |
|---|---|---|---|
| ld1（Python 客户端，带钩子） | 第 4 轮代码 + roster 编码 | 32.7（注入后 +1.0 s） | `slow.query` 61 ms；500 条注入 2.01 s |
| ld2（同上） | + 全表查询分片 | 6.5 | 最后一片之后的拼接与回复 9.4 ms |
| ld3（Python 客户端，不带钩子） | + 后台回复（转场记忆之前） | **5.5** | 全表回复时延 0.74 s；500 条注入 1.93 s |
| harness `storm.linkdrop`（浏览器，`--runs 1`，两次） | 最终代码 | **7.30、9.37**（60 s 窗口 `sim.step_max_us` 最大；第 4 轮 32.4–34.0） | 我方 > 50 ms LoAF 0、0，Toast 3、3，多渲染 9、9 行，RTF 1.000–1.011；浏览器侧 500 条注入 14.9、9.9 s（第 4 轮 6.4–7.9 s，Python 客户端前后都约 2 s，见第 6 节第 4 条） |

## 6 未解决与建议

1. **D1-AC-28（P1）**：按 ADR-074 第 7 条列为已知问题，阈值不变；G4 前需要评审人在 `apps/web/perf/waivers.yaml` 登记豁免（本包没有代为登记）。V0.2 方向：
   降低每 tick 的内存流量（状态数组工作集、tap 与 sensors 的整列写入），或验收时 sim-core 独占物理核（cpuset / `isolcpus`），或以 Tier A 客户端复测。
2. **state_ext 刷新率**（ADR-074 第 8 条）：N = 1000 全机约 7 s 一轮（N = 200 约 1.4 s），兴趣集同周期。V0.2 以列式编码把 2 Hz 的成本降到 ≤ 0.03 核，
   或兴趣集单独成批（需修订 17 §9.3 与 M11 DetailDemux）。
3. **sim-0406 的转场高度**：profile 规划器给出最高点 377.76 m 的转场、地理围栏每次拒绝（102）。本包只停止重复下发，规划器的高度上限与围栏一致性
   请 M10、M04 与 M16 查明；N = 100–500 约 97–102 s 的越线秒很可能是同一类重试（判定规模之外）。
4. **storm.linkdrop 的浏览器侧注入耗时与单步最大的余量**：本包两次 harness 运行注入 14.9、9.9 s（第 4 轮 6.4–7.9 s）；同一后端以 Python 客户端注入
   前后都约 2 s，同窗口 api 的 CPU、事件循环延迟、tick 数据年龄与第 4 轮同一量级，差别在浏览器侧（页面同时渲染 1000 架、处理 500 条链路事件），
   不在本条阈值内，建议验收时记录并与第 4 轮对照。两次的单步最大 7.30、9.37 ms，离 12 ms 有 2.6 ms 以上的余量；约 0.73 MB 的全表回复在空闲窗口
   放不下时 0.25 s 后照常发送，发送期间若主循环需要 GIL 仍会等待（sim 侧复现 ld3 中全程最大 5.5 ms），若验收中逼近 12 ms，下一步把大回复拆成分段
   或改为共享内存交付。
5. **checkpoint 后台拷贝**：约一半的拷贝等不到足够长的空闲窗口（每秒一次约 4.8 ms），是单步最大值的主要来源（D1-AC-07 的最大值阈值 12 ms 有余量）。
6. **同机干扰**：其他进程可被调度到 core1（本机无 `CAP_SYS_NICE`），单秒 p99 可到 3–5 ms；验收须按 ADR-033 在无其他工作包时运行。harness 的
   `fleet-ladder` 用例不钉核（`pin: 'none'`），工具本身也可能落到 core1，可考虑与 `fleet-ladder.concurrent` 一样在 `taskset -c 2-6` 下运行（M16）。
7. **客户端并发时 api 的 CPU**：conc1、conc2 的 api 为 0.147、0.148 核，第 4 轮同一用例为 0.096 核（P4-SIM 自测 0.093–0.096），与第 4 轮 gw-3clients
   的 0.146 核相当；同窗口 state_ext 的发布次数（约 7 s 一次）、roster 请求（稳态没有）与 tick 数据年龄（10–11 ms）都没有变化，本包没有找到与改动
   相关的机制，D1-AC-28 不判定 api CPU，D1-AC-08 的门禁为 ≤ 0.35 核，建议验收时一并对照。
8. **ext_pub 线程的排队**：roster、`fleet/vehicles` 与 state_ext 共用一个空闲窗口发布线程，最坏时延各约 0.25 s 叠加；网关、recorder 与 agent-runtime 的
   roster 调用超时 1 s × 3，`fleet/vehicles` 1.5 s × 2，本轮未见超时。
